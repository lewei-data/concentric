#!/usr/bin/env python3
"""素材投料器：把工作台补素材产出投料到 OpenMontage 项目 assets。

规约（见知识库 kb_materials_sky）：流水线在正式进入工厂（render/成片段）前，
素材必须先入 OpenMontage 项目库——『素材进厂先入库』。

- 目标：/opt/sefs/OpenMontage/projects/<slug>/assets/images/from-workbench/
- slug 解析：item.omProject > item.plan.omProject > 标题匹配 projects/*/project.json
  （去标点全等）> 兜底 _inbox/<traceId>_<itemId>/（待归位，不丢料）
- 每次投料登记 manifest.json（来源 trace/item/prompt/时间），可溯源
- 只做复制与登记，不修改 OpenMontage 项目任何已有文件

CLI：
    python3 asset_feed.py <traceId> <itemId>     # 手动补投一个 item 的素材
"""
import json
import os
import re
import shutil
import time
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_MAT = os.path.join(BASE_DIR, ".cache", "materials")
OM_ROOT = os.environ.get("OPC_OM_ROOT", "/opt/sefs/OpenMontage")
OM_INBOX = os.path.join(OM_ROOT, "projects", "_inbox")

_SLUG_STRIP = re.compile(r"[^\w\u4e00-\u9fff]+")


def _norm(title):
    return _SLUG_STRIP.sub("", str(title or "")).lower()


def _slug_index():
    """{归一化标题: slug}，读每个 project.json 的 title。"""
    idx = {}
    root = Path(OM_ROOT) / "projects"
    if not root.is_dir():
        return idx
    for pj in root.glob("*/project.json"):
        try:
            meta = json.loads(pj.read_text(encoding="utf-8"))
            t = _norm(meta.get("title") or "")
            if t:
                idx.setdefault(t, pj.parent.name)
        except Exception:
            continue
    return idx


def resolve_slug(item):
    """item → OpenMontage slug；解析不到返回 None。"""
    slug = (item.get("omProject") or (item.get("plan") or {}).get("omProject") or "").strip()
    if slug:
        return slug
    idx = _slug_index()
    return idx.get(_norm(item.get("title")))


def collect_files(item_id):
    """该 item 的补素材产出：gap*.png 优先，其次任意 png/wav。"""
    d = Path(CACHE_MAT) / item_id
    if not d.is_dir():
        return []
    files = sorted(d.glob("gap*.png")) or sorted(d.glob("*.png"))
    return [str(p) for p in files]


def feed(item, trace_id="", files=None):
    """投料主函数。返回 {ok, slug, dest, copied:[], manifest}；解析不到 slug 落 _inbox。"""
    item_id = str(item.get("id") or "")
    srcs = list(files or []) or collect_files(item_id)
    if not srcs:
        return {"ok": False, "reason": "无可投料文件", "copied": []}
    slug = resolve_slug(item)
    if slug:
        dest_dir = Path(OM_ROOT) / "projects" / slug / "assets" / "images" / "from-workbench"
    else:
        slug = None
        dest_dir = Path(OM_INBOX) / f"{trace_id or 'unknown'}_{item_id}".strip("_")
    dest_dir.mkdir(parents=True, exist_ok=True)

    copied = []
    for src in srcs:
        src = Path(src)
        if not src.is_file():
            continue
        dst = dest_dir / src.name
        shutil.copy2(src, dst)
        copied.append(dst.name)

    manifest_path = dest_dir / "manifest.json"
    manifest = []
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = []
    mats = (item.get("materials") or {}).get("generated") or []
    why_by_name = {}
    for m in mats:
        p = str(m.get("path") or "")
        if p:
            why_by_name[os.path.basename(p)] = str(m.get("why") or "")
    manifest.append({
        "fedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "traceId": trace_id, "itemId": item_id,
        "title": str(item.get("title") or "")[:80],
        "files": [{"name": n, "why": why_by_name.get(n, "")} for n in copied],
        "slug": slug or "_inbox",
    })
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": bool(copied), "slug": slug, "dest": str(dest_dir),
            "copied": copied, "manifest": str(manifest_path)}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, BASE_DIR)
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    trace, item_id = sys.argv[1], sys.argv[2]
    import sqlite3
    con = sqlite3.connect(os.environ.get(
        "OPC_OWNER_DB", "/mnt/sefs/conring/users/18911789286/opc.db"))
    con.row_factory = sqlite3.Row
    state = json.loads(con.execute(
        "SELECT state FROM pipeline_runs WHERE trace_id=?", (trace,)).fetchone()["state"])
    item = (state.get("items") or {}).get(item_id) or {}
    if not item:
        raise SystemExit(f"state.items 里找不到 {item_id}")
    r = feed(item, trace)
    print(json.dumps(r, ensure_ascii=False, indent=1))
