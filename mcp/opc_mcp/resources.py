"""统一资源访问层：以 key 归属的 phone 为唯一边界，打通 MCP 素材 / OPC 库 / Obsidian vault。

- 只读，绝不写 owner 库；所有路径从 config.phone_root(phone) 出发并做 realpath 越界校验。
- 屏蔽口径与 prototype/server.py::_vault、prototype/vault_digest.py::blocked 一致：
  登录凭据.md、隐藏段、运维手册路径、文件名以 台账/凭据/拓扑 结尾的 md、下划线开头文件。
- owner 库用 SQLite 只读连接（file:...?mode=ro），不干扰 Web 服务的 WAL 写入。
"""

import json
import os
import re
import sqlite3
from pathlib import Path

from . import config, knowledge as kb

_SHIELD_MD = re.compile(r"(台账|凭据|拓扑)\.md$")
MAX_READ_BYTES = 2 * 1024 * 1024

# ---------------------------------------------------------------- 全局共享资源
# 这些不属于任何单个手机号，Web 端本就对所有用户可见（/materials/、/downloads/、
# 共享热点流、owner 共享 vault 内化的 全公司 知识）。任意 key 都可访问。
OWNER_PHONE = os.environ.get("OPC_OWNER_PHONE", "18911789286")
SHARED_DB = config.USER_ROOT / OWNER_PHONE / "opc.db"
SHARED_VAULT = config.USER_ROOT / OWNER_PHONE / "obsidian"
SHARED_MATERIALS = config.USER_ROOT / OWNER_PHONE / "materials.json"
PUBLIC_MATERIALS_ROOT = Path(os.environ.get("OPC_PUBLIC_MATERIALS_ROOT", "/mnt/sefs/github-hotspots"))
PUBLIC_DOWNLOADS = Path(os.environ.get("OPC_PUBLIC_DOWNLOADS", "/mnt/sefs/conring/downloads"))
GLOBAL_DB = Path(os.environ.get("OPC_GLOBAL_DB",
                                str(Path(__file__).resolve().parents[2] / "prototype" / "opc.db")))


def _shared_conn():
    if not SHARED_DB.is_file():
        return None
    try:
        c = sqlite3.connect("file:%s?mode=ro" % SHARED_DB, uri=True, timeout=3)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def _shared_rows(sql, args=()):
    c = _shared_conn()
    if not c:
        return []
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        c.close()


def _materials_items():
    """共享素材清单（materials.json），绝对路径独立于任何手机号。"""
    if not SHARED_MATERIALS.is_file():
        return []
    try:
        data = json.loads(SHARED_MATERIALS.read_text(encoding="utf-8"))
        return data.get("items", []) if isinstance(data, dict) else (data or [])
    except (OSError, ValueError):
        return []


def shared_counts():
    """全局共享资源总览：所有 key 可见，不受手机号隔离。"""
    out = {"sharedDbExists": SHARED_DB.is_file(), "sharedKnowledge": 0,
           "sharedVaultMd": 0, "sharedMaterials": len(_materials_items()),
           "publicMaterialsRoot": str(PUBLIC_MATERIALS_ROOT),
           "publicMaterialsExists": PUBLIC_MATERIALS_ROOT.is_dir(),
           "publicDownloads": str(PUBLIC_DOWNLOADS),
           "publicDownloadsExists": PUBLIC_DOWNLOADS.is_dir(),
           "globalHotspotFeed": 0}
    c = _shared_conn()
    if c:
        try:
            out["sharedKnowledge"] = c.execute(
                "SELECT COUNT(*) FROM knowledge WHERE project_id='全公司'").fetchone()[0]
        except sqlite3.Error:
            pass
        c.close()
    if SHARED_VAULT.is_dir():
        try:
            out["sharedVaultMd"] = sum(1 for p in SHARED_VAULT.rglob("*.md")
                                       if not shield(str(p.relative_to(SHARED_VAULT))))
        except OSError:
            pass
    if GLOBAL_DB.is_file():
        try:
            g = sqlite3.connect("file:%s?mode=ro" % GLOBAL_DB, uri=True, timeout=3)
            out["globalHotspotFeed"] = g.execute("SELECT COUNT(*) FROM hotspot_feed").fetchone()[0]
            g.close()
        except sqlite3.Error:
            pass
    return out


def _shared_search(q, k, phone=None):
    """在全局共享知识/素材清单/热点流里检索，命中带 source=shared.*。

    owner 自己访问时，个人源已覆盖本库全部 knowledge（含全公司条目），
    故跳过共享 knowledge 以免重复；共享素材/热点是独立来源，始终保留。
    """
    scored = []
    if str(phone) != OWNER_PHONE:
        for row in _shared_rows(
                "SELECT id,title,summary,body,path,category FROM knowledge "
                "WHERE project_id='全公司' LIMIT 1000"):
            if shield(row.get("path") or "") or shield(row.get("title") or ""):
                continue
            text = "%s %s %s" % (row.get("title") or "", row.get("summary") or "", row.get("body") or "")
            h = _hit("shared.knowledge", row["id"], row.get("title"), text, q, row.get("path") or "")
            if h:
                scored.append(h)
    for item in _materials_items():
        if not isinstance(item, dict):
            continue
        text = "%s %s %s" % (item.get("title") or "", item.get("desc") or "", item.get("meta") or "")
        h = _hit("shared.material", item.get("id"), item.get("title"), text, q, item.get("url") or "")
        if h:
            scored.append(h)
    if GLOBAL_DB.is_file():
        try:
            g = sqlite3.connect("file:%s?mode=ro" % GLOBAL_DB, uri=True, timeout=3)
            g.row_factory = sqlite3.Row
            for row in g.execute("SELECT id,title,source,url FROM hotspot_feed ORDER BY id DESC LIMIT 500"):
                r = dict(row)
                h = _hit("shared.hotspot", r["id"], r.get("title") or "",
                         "%s %s" % (r.get("title") or "", r.get("source") or ""), q, r.get("url") or "")
                if h:
                    scored.append(h)
            g.close()
        except sqlite3.Error:
            pass
    return scored[:max(1, int(k))]


def shield(rel):
    """与 Web _vault / vault_digest.blocked 同口径，判断 vault 相对路径是否屏蔽。"""
    rel = str(rel).replace("\\", "/")
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts):
        return True
    if rel == "公司/登录凭据.md":
        return True
    if "运维手册" in rel:
        return True
    if _SHIELD_MD.search(rel):
        return True
    return parts[-1].startswith("_") or parts[-1].startswith(".")


def root(phone):
    return config.phone_root(phone)


def db_path(phone):
    return root(phone) / "opc.db"


def vault(phone):
    return config.user_vault(phone)


def _conn(phone):
    p = db_path(phone)
    if not p.is_file():
        return None
    try:
        c = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=3)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def _rows(phone, sql, args=()):
    c = _conn(phone)
    if not c:
        return []
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        c.close()


def counts(phone, store):
    """资源总览：OPC 库各表 + vault 顶层 + MCP 自有素材，全部限定在本手机号。"""
    c = _conn(phone)
    out = {
        "phone": phone,
        "dataDir": str(root(phone)),
        "vault": str(vault(phone)),
        "ownerDb": str(db_path(phone)),
        "ownerDbExists": bool(c),
        "knowledge": 0, "reports": 0, "contents": 0,
        "chatMessages": 0, "pipelineRuns": 0, "approvals": 0,
        "vaultDirs": [], "mcpMaterials": 0, "mcpChunks": 0, "mcpCards": 0,
    }
    if c:
        for key, table in (("knowledge", "knowledge"), ("reports", "reports"),
                           ("contents", "contents"), ("chatMessages", "chat_messages"),
                           ("pipelineRuns", "pipeline_runs"), ("approvals", "approvals")):
            try:
                out[key] = c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            except sqlite3.Error:
                pass
        c.close()
    v = vault(phone)
    if v.is_dir():
        out["vaultDirs"] = sorted(p.name for p in v.iterdir()
                                  if p.is_dir() and not p.name.startswith("."))[:60]
    if store is not None:
        try:
            out["mcpMaterials"] = store.one("SELECT COUNT(*) n FROM materials WHERE phone=?", (phone,))["n"]
            out["mcpChunks"] = store.one("SELECT COUNT(*) n FROM chunks WHERE phone=?", (phone,))["n"]
            out["mcpCards"] = store.one("SELECT COUNT(*) n FROM cards WHERE phone=?", (phone,))["n"]
        except Exception:
            pass
    out["shared"] = shared_counts()
    return out


def _hit(source, ident, title, text, q, path=""):
    low = text.lower()
    s = kb._score(q, text)
    if s <= 0:
        return None
    matched = sum(1 for t in q if t in low)
    pos = min((low.find(t) for t in q if t in low), default=0)
    return {"source": source, "id": str(ident), "title": title or "", "path": path or "",
            "score": round(s, 2), "matched": matched,
            "snippet": text[max(0, pos - 80):pos + 240].replace("\n", " ")}


def search(store, phone, query, k=8):
    """跨源检索：OPC 知识库/报告/内容 + MCP 素材片段，按同一打分口径合并。"""
    q = kb.terms(query)
    if not q:
        return []
    scored = []
    for row in _rows(phone, "SELECT id,title,summary,body,path,category FROM knowledge LIMIT 1000"):
        if shield(row.get("path") or "") or shield(row.get("title") or ""):
            continue
        text = "%s %s %s" % (row.get("title") or "", row.get("summary") or "", row.get("body") or "")
        h = _hit("opc.knowledge", row["id"], row.get("title"), text, q, row.get("path") or "")
        if h:
            scored.append(h)
    for row in _rows(phone, "SELECT id,title,kind,conclusion,evidence,trace_id FROM reports LIMIT 300"):
        text = "%s %s %s %s" % (row.get("title") or "", row.get("conclusion") or "",
                                row.get("evidence") or "", row.get("kind") or "")
        h = _hit("opc.report", row["id"], row.get("title"), text, q, row.get("trace_id") or "")
        if h:
            scored.append(h)
    for row in _rows(phone, "SELECT id,title,stage,raw,trace_id FROM contents LIMIT 500"):
        text = "%s %s %s" % (row.get("title") or "", row.get("stage") or "", row.get("raw") or "")
        h = _hit("opc.content", row["id"], row.get("title"), text, q, row.get("trace_id") or "")
        if h:
            scored.append(h)
    for row in _rows(phone, "SELECT id,source,substr(content,1,2000) c FROM chat_messages ORDER BY id DESC LIMIT 400"):
        content = row.get("c") or ""
        if "LLM 通道" in content and ("先给知识库命中" in content or content.lstrip().startswith("[降级]")):
            continue                                     # 旧降级回答不参与检索，避免层层嵌套
        h = _hit("opc.message", row["id"], row.get("source") or "", content, q)
        if h:
            scored.append(h)
    if store is not None:
        try:
            for hit in kb.search(store, phone, query, k=max(1, int(k))):
                mat = store.material(hit["material_id"]) or {}
                scored.append({"source": "mcp.material", "id": hit["material_id"],
                               "title": mat.get("filename") or hit["material_id"],
                               "path": str(mat.get("path") or ""), "score": hit["score"],
                               "snippet": hit["snippet"]})
        except Exception:
            pass
    try:
        scored.extend(_shared_search(q, k, phone))
    except Exception:
        pass
    scored.sort(key=lambda x: -x["score"])
    return scored[:max(1, int(k))]


def context(store, phone, query, budget=4000):
    """给 ask 拼上下文：跨源命中片段 + MCP 素材卡片，受预算约束。"""
    lines, used = [], 0
    try:
        for card in (store.cards(phone, limit=4) if store is not None else []):
            block = "【素材卡片】%s\n%s" % (card.get("filename") or card.get("title") or "",
                                        card.get("summary") or "")
            if used + len(block) > budget * 0.4:
                break
            lines.append(block)
            used += len(block)
    except Exception:
        pass
    for hit in search(store, phone, query, k=8):
        block = "【%s】%s\n%s" % (hit["source"], hit["title"] or hit["id"], hit["snippet"])
        if used + len(block) > budget:
            break
        lines.append(block)
        used += len(block)
    return "\n\n".join(lines)


_LIST_SQL = {
    "knowledge": "SELECT id,title,category,sancai,path,updated_at FROM knowledge ORDER BY updated_at DESC LIMIT ?",
    "reports": "SELECT id,kind,title,conclusion,created_at FROM reports ORDER BY created_at DESC LIMIT ?",
    "contents": "SELECT id,trace_id,stage,title,created_at FROM contents ORDER BY created_at DESC LIMIT ?",
    "messages": "SELECT id,thread_id,source,substr(content,1,200) c,created_at FROM chat_messages ORDER BY id DESC LIMIT ?",
    "approvals": "SELECT id,trace_id,kind,status,created_at FROM approvals ORDER BY created_at DESC LIMIT ?",
}


def list_kind(phone, kind, limit=20):
    if kind in _LIST_SQL:
        return {"kind": kind, "items": _rows(phone, _LIST_SQL[kind], (int(limit),))}
    if kind == "shared_knowledge":
        return {"kind": kind, "items": _shared_rows(
            "SELECT id,title,category,sancai,path,updated_at FROM knowledge "
            "WHERE project_id='全公司' ORDER BY updated_at DESC LIMIT ?", (int(limit),)),
                "scope": "global-shared"}
    if kind == "shared_materials":
        items = []
        for m in _materials_items()[:int(limit)]:
            if isinstance(m, dict):
                items.append({k: m.get(k) for k in
                              ("id", "category", "title", "desc", "url", "meta", "score", "grade")})
        return {"kind": kind, "items": items, "scope": "global-shared",
                "fileRoot": str(PUBLIC_MATERIALS_ROOT)}
    if kind == "shared_hotspots":
        items = []
        if GLOBAL_DB.is_file():
            try:
                g = sqlite3.connect("file:%s?mode=ro" % GLOBAL_DB, uri=True, timeout=3)
                g.row_factory = sqlite3.Row
                items = [dict(r) for r in g.execute(
                    "SELECT id,source,title,url,rank,score,collected_at FROM hotspot_feed "
                    "ORDER BY id DESC LIMIT ?", (int(limit),))]
                g.close()
            except sqlite3.Error:
                pass
        return {"kind": kind, "items": items, "scope": "global-shared"}
    if kind == "downloads":
        items = []
        if PUBLIC_DOWNLOADS.is_dir():
            for p in sorted(PUBLIC_DOWNLOADS.iterdir())[:int(limit) * 3]:
                if p.name.startswith(".") or p.is_dir():
                    continue
                items.append({"name": p.name, "size": p.stat().st_size})
                if len(items) >= int(limit):
                    break
        return {"kind": kind, "items": items, "scope": "global-shared",
                "note": "对应 Web 的 /downloads/ 公开下载区；只列文件名，读取用 shared_download:<文件名>"}
    if kind == "vault":
        v = vault(phone)
        items = []
        if v.is_dir():
            for p in sorted(v.rglob("*.md"))[:int(limit) * 3]:
                rel = str(p.relative_to(v)).replace("\\", "/")
                if shield(rel):
                    continue
                items.append({"path": rel, "size": p.stat().st_size})
                if len(items) >= int(limit):
                    break
        return {"kind": kind, "items": items,
                "note": "仅列出未屏蔽的 md；敏感文件（凭据/台账/拓扑/运维手册/隐藏段）已过滤"}
    choices = sorted(_LIST_SQL) + ["vault", "shared_knowledge", "shared_materials",
                                   "shared_hotspots", "downloads"]
    return {"kind": kind, "items": [], "error": "未知资源类型，可选：%s" % ", ".join(choices)}


_READ_SQL = {
    "knowledge": "SELECT id,title,category,sancai,path,summary,body FROM knowledge WHERE id=?",
    "report": "SELECT id,title,kind,conclusion,evidence,decisions,raw FROM reports WHERE id=?",
    "content": "SELECT id,title,stage,trace_id,raw FROM contents WHERE id=?",
    "approval": "SELECT * FROM approvals WHERE id=?",
}


def read(phone, ref, store=None):
    """读取单个资源。ref 形如 knowledge:<id> / report:<id> / vault:<相对路径> / material:<id>。"""
    kind, sep, key = str(ref or "").partition(":")
    if not sep or not key:
        return None, "ref 格式应为 <类型>:<标识>，如 knowledge:kb_xxx 或 vault:OPC/知识库/a.md"
    if kind == "shared_knowledge":
        row = _shared_rows(
            "SELECT id,title,category,sancai,path,summary,body FROM knowledge "
            "WHERE id=? AND project_id='全公司'", (key,))
        if not row:
            return None, "共享知识条目不存在"
        item = row[0]
        if shield(item.get("path") or "") or shield(item.get("title") or ""):
            return None, "该条目按安全策略屏蔽"
        return {"kind": "shared_knowledge", "scope": "global-shared", "item": item}, None
    if kind == "shared_material":
        for m in _materials_items():
            if isinstance(m, dict) and str(m.get("id")) == key:
                return {"kind": "shared_material", "scope": "global-shared", "item": m}, None
        return None, "共享素材不存在"
    if kind == "shared_hotspot":
        if not GLOBAL_DB.is_file():
            return None, "共享热点库不存在"
        try:
            g = sqlite3.connect("file:%s?mode=ro" % GLOBAL_DB, uri=True, timeout=3)
            g.row_factory = sqlite3.Row
            r = g.execute("SELECT id,source,title,url,rank,score,extra,collected_at "
                          "FROM hotspot_feed WHERE id=?", (key,)).fetchone()
            g.close()
        except sqlite3.Error:
            return None, "共享热点查询失败"
        return ({"kind": "shared_hotspot", "scope": "global-shared", "item": dict(r)}, None) \
            if r else (None, "共享热点不存在")
    if kind == "shared_download":
        name = Path(key).name
        if name != key or name.startswith("."):
            return None, "非法文件名"
        p = PUBLIC_DOWNLOADS / name
        if not p.is_file():
            return None, "下载文件不存在"
        if p.stat().st_size > MAX_READ_BYTES:
            return None, "文件超过 %dKB，仅支持列出文件名" % (MAX_READ_BYTES // 1024)
        return {"kind": "shared_download", "scope": "global-shared",
                "path": str(p), "size": p.stat().st_size}, None
    if kind == "vault":
        v = vault(phone)
        try:
            p = (v / key).resolve()
            base = v.resolve()
        except OSError:
            return None, "路径解析失败"
        if base not in p.parents and p != base:
            return None, "越界：只能读取本手机号 vault 内的文件"
        rel = str(p.relative_to(base)).replace("\\", "/")
        if shield(rel):
            return None, "该文件按安全策略屏蔽，不予读取"
        if not p.is_file():
            return None, "文件不存在"
        if p.stat().st_size > MAX_READ_BYTES:
            return None, "文件超过 %dKB，请用 kb_search 检索片段" % (MAX_READ_BYTES // 1024)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return None, "读取失败：%s" % e
        return {"kind": "vault", "path": rel, "text": text}, None
    if kind == "material" and store is not None:
        mat = store.material(key)
        if not mat or mat["phone"] != phone:
            return None, "素材不存在或不属于当前账号"
        p = Path(mat["path"])
        if not p.is_file():
            return None, "素材文件已丢失"
        if p.stat().st_size > MAX_READ_BYTES:
            return None, "素材超过 %dKB，请用 kb_search 检索片段" % (MAX_READ_BYTES // 1024)
        text, reason = kb.extract_text(p)
        if not text:
            return None, reason or "无可抽取文本"
        return {"kind": "material", "path": str(p), "text": text}, None
    if kind in _READ_SQL:
        row = _rows(phone, _READ_SQL[kind], (key,))
        if not row:
            return None, "资源不存在或不属于当前账号"
        item = row[0]
        if kind == "knowledge" and (shield(item.get("path") or "") or shield(item.get("title") or "")):
            return None, "该条目按安全策略屏蔽"
        return {"kind": kind, "item": item}, None
    return None, "未知资源类型：%s" % kind
