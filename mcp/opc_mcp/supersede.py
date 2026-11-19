"""素材版本取代：MCP 侧切块/卡片回收 + 已内化知识条目回收（owner 库 + vault md）。

内化条目 id 规则（prototype/material_content.py::contentize）：
    kb_vault_mat_ + sha1(素材落盘路径)[:10]
据此可从被取代的素材精确找到它内化出的 knowledge 行与 vault md，
实现「报告更新后旧版不再被默认召回」（含 opc.knowledge 检索源）。

安全口径：
- 素材行与磁盘文件一律保留（status=superseded + superseded_by），可审计可恢复；
- 回收的 vault md 移动到 <user_root>/mcp/backup/superseded/<日期>/，不硬删；
- owner 库写入只删 knowledge / knowledge_relations 中该 kb id 的行，别的不碰。
"""

import hashlib
import shutil
import sqlite3
import time

from . import config


def internalized_id(material_path):
    """素材落盘路径 → 它内化出的 knowledge 条目 id。"""
    return "kb_vault_mat_" + hashlib.sha1(str(material_path).encode("utf-8")).hexdigest()[:10]


def retract_internalized(phone, material_path):
    """删除该素材内化出的 knowledge 行/关系，并把对应 vault md 移到备份目录。

    返回 {"kb_id", "deleted", "md_backup", "error"}；error="未内化" 表示
    该素材没有内化条目（例如内容化队列未跑或非文本素材），属正常情况。
    """
    kb_id = internalized_id(material_path)
    out = {"kb_id": kb_id, "deleted": False, "md_backup": "", "error": ""}
    db = config.phone_root(phone) / "opc.db"
    if not db.is_file():
        out["error"] = "未内化（owner 库不存在）"
        return out
    rel = ""
    try:
        c = sqlite3.connect(str(db), timeout=10)
        row = c.execute("SELECT id, path FROM knowledge WHERE id=?", (kb_id,)).fetchone()
        if not row:
            c.close()
            out["error"] = "未内化"
            return out
        rel = row[1] or ""
        c.execute("DELETE FROM knowledge WHERE id=?", (kb_id,))
        try:
            c.execute("DELETE FROM knowledge_relations WHERE src=? OR dst=?", (kb_id, kb_id))
        except sqlite3.Error:
            pass                                  # 老库可能没有关系表
        c.commit()
        c.close()
    except sqlite3.Error as e:
        out["error"] = "owner 库写入失败：%s" % e
        return out
    if rel:
        md = config.user_vault(phone) / rel
        if md.is_file():
            bdir = config.user_dir(phone, "backup/superseded/" + time.strftime("%Y-%m-%d"))
            try:
                bdir.mkdir(parents=True, exist_ok=True)
                dst = bdir / md.name
                i = 1
                while dst.exists():
                    dst = bdir / ("%s.%d%s" % (md.stem, i, md.suffix))
                    i += 1
                shutil.move(str(md), str(dst))
                out["md_backup"] = str(dst)
            except OSError as e:
                out["error"] = "md 备份失败：%s" % e
    out["deleted"] = True
    return out


def supersede(store, phone, material_id, superseded_by, retract=True):
    """把 material_id 标记为被 superseded_by 取代：回收 MCP 切块/卡片 + 内化条目。"""
    mat = store.material(material_id)
    if not mat or mat["phone"] != phone:
        return {"material_id": material_id, "ok": False,
                "error": "素材不存在或不属于当前账号"}
    if material_id == superseded_by:
        return {"material_id": material_id, "ok": False, "error": "不能取代自身"}
    res = store.supersede_material(material_id, superseded_by) or {}
    out = {"material_id": material_id, "ok": True,
           "deleted_chunks": res.get("deleted_chunks", 0),
           "deleted_cards": res.get("deleted_cards", 0),
           "retracted_kb": [], "md_backups": [], "warnings": []}
    if retract:
        r = retract_internalized(phone, mat["path"])
        if r.get("deleted"):
            out["retracted_kb"].append(r["kb_id"])
            if r.get("md_backup"):
                out["md_backups"].append(r["md_backup"])
        elif r.get("error") and "未内化" not in r["error"]:
            out["warnings"].append(r["error"])
    return out


def supersede_older_same_name(store, phone, filename, new_id, keep_history=False):
    """同名旧版默认取代（保留最新一条召回）；keep_history=true 时不动作。"""
    if keep_history:
        return []
    new = store.material(new_id) or {}
    olds = store.all(
        "SELECT id FROM materials WHERE phone=? AND filename=? AND id!=? "
        "AND status!='superseded' AND created_at<=? ORDER BY created_at",
        (phone, filename, new_id, new.get("created_at") or 0))
    return [supersede(store, phone, m["id"], new_id) for m in olds]
