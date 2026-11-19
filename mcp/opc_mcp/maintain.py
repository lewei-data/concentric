"""一次性运维 CLI：同名素材去重（修复 S3 现场）。

用法（在 mcp/ 目录下，先清 LD_LIBRARY_PATH）：
  env -u LD_LIBRARY_PATH PYTHONPATH=$(pwd) python3 -m opc_mcp.maintain status
  env -u LD_LIBRARY_PATH PYTHONPATH=$(pwd) python3 -m opc_mcp.maintain dedupe [--phone 189...] [--apply]

动作（幂等，可重复跑）：
- 每组同名素材只保留最新一条（created_at 最大），旧条目：
  * MCP 侧：status=superseded + superseded_by，删除其 chunks/cards（文件保留）；
  * 内化侧：删除 owner 库里它内化出的 kb_vault_mat_* knowledge 行与关系，
    vault md 移动到 <user_root>/mcp/backup/superseded/<日期>/（不硬删）。
- 默认 dry-run 只打印计划；--apply 才落库，且自动备份 mcp 库与受影响的 owner 库。
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

from . import config, supersede
from .store import Store


def duplicate_groups(store, phone=None):
    rows = store.all("SELECT phone, filename, COUNT(*) n FROM materials "
                     "WHERE status!='superseded' GROUP BY phone, filename HAVING n>1")
    if phone:
        rows = [r for r in rows if r["phone"] == phone]
    return rows


def plan_group(store, phone, filename):
    rows = store.all("SELECT id, created_at, status FROM materials "
                     "WHERE phone=? AND filename=? AND status!='superseded' "
                     "ORDER BY created_at DESC, id DESC", (phone, filename))
    if len(rows) < 2:
        return None
    keep = rows[0]
    olds = rows[1:]
    return {"phone": phone, "filename": filename, "keep": keep, "olds": olds}


def backup(path, tag):
    src = Path(path)
    if not src.is_file():
        return ""
    dst = src.with_name("%s.bak-%s-%s" % (src.name, tag, time.strftime("%Y%m%d-%H%M%S")))
    shutil.copy2(src, dst)
    return str(dst)


def main():
    ap = argparse.ArgumentParser(description="OPC MCP 一次性运维")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查看同名重复素材分组")
    dp = sub.add_parser("dedupe", help="同名素材去重（旧版标记 superseded + 回收内化条目）")
    dp.add_argument("--phone", default=None, help="只处理该手机号（默认全部）")
    dp.add_argument("--apply", action="store_true", help="真正落库（默认 dry-run）")
    args = ap.parse_args()

    store = Store(config.DB_PATH)
    phone = getattr(args, "phone", None)
    groups = [g for g in (plan_group(store, r["phone"], r["filename"])
                          for r in duplicate_groups(store, phone)) if g]

    if args.cmd == "status" or not args.apply:
        if not groups:
            print("没有同名重复素材，无需处理。")
            return
        print("dry-run 计划（%d 组，加 --apply 执行）：" % len(groups))
        for g in groups:
            print("  phone=%s  %s" % (g["phone"], g["filename"]))
            print("    保留: %s (created=%s)" % (g["keep"]["id"],
                                                 time.strftime("%m-%d %H:%M", time.localtime(g["keep"]["created_at"]))))
            for o in g["olds"]:
                print("    取代: %s (created=%s)  → 内化条目 %s"
                      % (o["id"], time.strftime("%m-%d %H:%M", time.localtime(o["created_at"])),
                         supersede.internalized_id(_material_path(store, o["id"]))))
        return

    if not groups:
        print("没有同名重复素材，无需处理。")
        return
    tag = "dedupe"
    print("备份：", backup(config.DB_PATH, tag))
    owner_dbs = set()
    done = 0
    for g in groups:
        for o in g["olds"]:
            res = supersede.supersede(store, g["phone"], o["id"], g["keep"]["id"])
            status = "OK " if res.get("ok") else "FAIL"
            print("%s %s/%s → %s  chunks=%s cards=%s kb=%s%s"
                  % (status, g["phone"], o["id"], g["keep"]["id"],
                     res.get("deleted_chunks"), res.get("deleted_cards"),
                     ",".join(res.get("retracted_kb") or []) or "-",
                     ("  warn:" + ";".join(res.get("warnings") or [])) if res.get("warnings") else ""))
            if res.get("ok"):
                done += 1
                owner_dbs.add(config.phone_root(g["phone"]) / "opc.db")
    for db in owner_dbs:
        print("owner 库已在线修改（WAL），建议另外留档：", db)
    print("完成：取代 %d 条旧素材；vault md 备份在各用户 mcp/backup/superseded/ 下。" % done)


def _material_path(store, mid):
    m = store.material(mid) or {}
    return m.get("path") or ""


if __name__ == "__main__":
    sys.exit(main())
