"""资产盘点：数据库/表/数据量 → 产品与工厂产出 → 素材库/知识库 → 问题清单 → 每日凌晨报告。

知识库管理（kb_manager）的自动盘点链路：
  collect() → find_issues() → kb_manager 智能分析（不可用时纯规则）→ 渲染 Markdown
  → 落盘 <vault>/OPC/汇报/资产盘点/ → 邮件发老板 → emit report 进前端报表。

只读设计：所有 SQLite 均以 mode=ro 打开，不写业务库；报告只写 vault 汇报目录。
"""
import glob
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import unquote

from opc_paths import (BASE_DIR, TZ, VAULT, USERS_DIR, USERS_DB, OWNER_DATA_DIR,
                       MATERIALS_FILE, MATERIALS_ROOT, BOSS_MAIL)

REPO_ROOT = os.path.normpath(os.path.join(BASE_DIR, ".."))
HERMES_ROOT = os.environ.get("OPC_HERMES_HOME", "/opt/sefs/hermes")
REPORT_DIR = os.path.join(VAULT, "OPC", "汇报", "资产盘点")
DELIVERABLE_ROOT = os.path.join(VAULT, "OPC", "交付物")
KB_ROOT = os.path.join(VAULT, "OPC", "知识库")
MATERIALS_VAULT = os.path.join(VAULT, "OPC", "素材库", "materials.json")
OM_PROJECTS = os.environ.get("OPC_OM_PROJECTS", "/opt/sefs/OpenMontage/projects")
INVENTORY_AT = os.environ.get("OPC_INVENTORY_AT", "02:30")     # 每日凌晨执行时刻
OWNER_DB = os.path.join(OWNER_DATA_DIR, "opc.db")
_SKIP_SUFFIX = ("_data", "_idx", "_docsize", "_config", "_content", "_segments", "_segdir")


def _log(msg):
    print(f"[inventory] {msg}", flush=True)


def _now():
    return datetime.now(TZ)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _kb(size):
    return int((size or 0) / 1024)


# ---------------------------------------------------------------------------
# 一、数据库盘点
# ---------------------------------------------------------------------------
def _open_ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=8)


def _tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def _is_shadow(name, names):
    """过滤 SQLite 内部表与 FTS 影子表（真实业务表已在表清单中）。"""
    if name.startswith("sqlite_") or name.endswith("_fts"):
        return True
    for suf in _SKIP_SUFFIX:
        if name.endswith(suf) and name[:-len(suf)] in names:
            return True
    return False


def _schema(conn, name):
    try:
        cols = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
    except sqlite3.Error:
        return ""
    parts = [f"{c[1]}:{c[2] or 'TEXT'}" for c in cols]
    if len(parts) > 10:
        return "，".join(parts[:10]) + f" …共{len(parts)}列"
    return "，".join(parts)


def audit_db(path, label="", count_rows=True):
    """单个库：大小 + 表清单（行数 + 字段结构）。库不可读时返回 error 而不抛异常。"""
    item = {"path": path, "label": label or os.path.basename(path), "sizeKb": 0,
            "tables": [], "tableCount": 0, "rows": 0, "error": ""}
    if not os.path.exists(path):
        item["error"] = "文件不存在"
        return item
    try:
        item["sizeKb"] = _kb(os.path.getsize(path))
        conn = _open_ro(path)
    except (sqlite3.Error, OSError) as exc:
        item["error"] = f"{type(exc).__name__}: {exc}"[:120]
        return item
    try:
        names = _tables(conn)
        for name in names:
            if _is_shadow(name, names):
                continue
            t = {"name": name, "rows": None, "cols": _schema(conn, name)}
            if count_rows:
                try:
                    t["rows"] = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                    item["rows"] += t["rows"]
                except sqlite3.Error as exc:
                    t["rows"] = f"读取失败({str(exc)[:24]})"
            item["tables"].append(t)
        item["tableCount"] = len(item["tables"])
    finally:
        conn.close()
    return item


def audit_databases():
    """全部库：业务过程库（含各用户工作区）+ 系统库 + Hermes 员工库（汇总）。"""
    out = {"business": [], "system": [], "profiles": {"count": 0, "tables": 0, "sizeKb": 0,
                                                      "nonEmpty": 0, "list": []},
           "total": {"dbs": 0, "tables": 0, "rows": 0, "sizeKb": 0}}
    # 老板工作区过程库（业务口径的主库）
    if os.path.exists(OWNER_DB):
        out["business"].append({**audit_db(OWNER_DB, "过程库（老板工作区）"), "role": "owner"})
    # 其它用户工作区过程库
    for path in sorted(glob.glob(os.path.join(USERS_DIR, "*", "opc.db"))):
        if os.path.abspath(path) == os.path.abspath(OWNER_DB):
            continue
        uid = os.path.basename(os.path.dirname(path))
        out["business"].append({**audit_db(path, f"过程库（用户 {uid}）"), "role": "user"})
    # 系统库
    for path, label in ((USERS_DB, "账号库"), (os.path.join(REPO_ROOT, "ops", "nodes.db"), "节点库"),
                        (os.path.join(HERMES_ROOT, "dispatch", "dispatch.db"), "派发库")):
        if os.path.exists(path):
            out["system"].append(audit_db(path, label))
    # Hermes 运行库
    for path in sorted(glob.glob(os.path.join(HERMES_ROOT, "*.db"))) + \
            sorted(glob.glob(os.path.join(HERMES_ROOT, "cron", "*.db"))):
        out["system"].append(audit_db(path, f"Hermes · {os.path.basename(path)}"))
    # 员工 profile 库：只汇总规模，不逐表计数（体量大且非业务口径）
    for path in sorted(glob.glob(os.path.join(HERMES_ROOT, "profiles", "*", "*.db"))):
        try:
            size = os.path.getsize(path)
            conn = _open_ro(path)
            try:
                tcount = sum(1 for n in _tables(conn) if not n.startswith("sqlite_"))
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            continue
        out["profiles"]["count"] += 1
        out["profiles"]["tables"] += tcount
        out["profiles"]["sizeKb"] += _kb(size)
        if size > 64 * 1024 and tcount:
            out["profiles"]["nonEmpty"] += 1
        out["profiles"]["list"].append({"path": os.path.basename(os.path.dirname(path)) + "/" +
                                        os.path.basename(path), "sizeKb": _kb(size), "tables": tcount})
    for group in ("business", "system"):
        for db in out[group]:
            out["total"]["dbs"] += 1
            out["total"]["tables"] += db["tableCount"]
            out["total"]["rows"] += db["rows"]
            out["total"]["sizeKb"] += db["sizeKb"]
    out["total"]["dbs"] += out["profiles"]["count"]
    out["total"]["tables"] += out["profiles"]["tables"]
    out["total"]["sizeKb"] += out["profiles"]["sizeKb"]
    return out


# ---------------------------------------------------------------------------
# 二、业务盘点：产品、工厂产出、用量
# ---------------------------------------------------------------------------
def _scalar(conn, sql, default=0):
    try:
        row = conn.execute(sql).fetchone()
        return row[0] if row and row[0] is not None else default
    except sqlite3.Error:
        return default


def _dist(conn, sql):
    try:
        return {str(k or "未标注"): int(v or 0) for k, v in conn.execute(sql).fetchall()}
    except sqlite3.Error:
        return {}


def _age_hours(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return round((_now() - dt).total_seconds() / 3600, 1)


def audit_business():
    """过程库业务口径：内容单（产品）、发布回执、流水线、知识索引、用量。"""
    biz = {"ok": False, "contents": {}, "knowledge": {}, "pipeline": {}, "traces": {},
           "release": {}, "usage": {}, "llm": {}, "hotspots": 0, "reports": 0}
    if not os.path.exists(OWNER_DB):
        return biz
    try:
        conn = _open_ro(OWNER_DB)
    except sqlite3.Error:
        return biz
    try:
        biz["ok"] = True
        stage = _dist(conn, "SELECT stage, COUNT(*) FROM contents GROUP BY stage")
        biz["contents"] = {
            "total": sum(stage.values()),
            "titles": _scalar(conn, "SELECT COUNT(DISTINCT title) FROM contents"),
            "stages": stage,
            "projects": _dist(conn, "SELECT project_id, COUNT(*) FROM contents GROUP BY project_id"),
            "blockedBy": {},
        }
        # 阻塞归因：contents.raw 里的 blockedBy（流程缺陷排查的关键口径）
        try:
            rows = conn.execute(
                "SELECT raw FROM contents WHERE stage='blocked'").fetchall()
            for (raw,) in rows:
                try:
                    by = (json.loads(raw or "{}") or {}).get("blockedBy") or "未标注"
                except (ValueError, TypeError):
                    by = "未标注"
                biz["contents"]["blockedBy"][by] = biz["contents"]["blockedBy"].get(by, 0) + 1
        except sqlite3.Error:
            pass
        biz["knowledge"] = {
            "rows": _scalar(conn, "SELECT COUNT(*) FROM knowledge"),
            "noPath": _scalar(conn, "SELECT COUNT(*) FROM knowledge WHERE path IS NULL OR path=''"),
            "categories": _dist(conn, "SELECT category, COUNT(*) FROM knowledge GROUP BY category"),
            "updated7d": _scalar(conn, "SELECT COUNT(*) FROM knowledge WHERE updated_at >= "
                                      "datetime('now','-7 day')"),
        }
        biz["pipeline"] = {
            "status": _dist(conn, "SELECT status, COUNT(*) FROM pipeline_runs GROUP BY status"),
            "stale": [{"trace": t, "stage": s, "updatedAt": u, "ageHours": _age_hours(u)}
                      for t, s, u in conn.execute(
                          "SELECT trace_id, stage, updated_at FROM pipeline_runs "
                          "WHERE status='running' ORDER BY updated_at DESC LIMIT 8")],
        }
        biz["traces"] = {"status": _dist(conn, "SELECT status, COUNT(*) FROM traces GROUP BY status")}
        biz["release"] = {
            "ledger": _scalar(conn, "SELECT COUNT(*) FROM release_ledger"),
            "published": stage.get("published", 0),
            "approved": stage.get("approved", 0),
            "blocked": stage.get("blocked", 0),
            "crafts": _scalar(conn, "SELECT COUNT(*) FROM product_crafts"),
            "dailyPublished": _scalar(conn, "SELECT COALESCE(SUM(published),0) FROM stats_daily "
                                            "WHERE day >= date('now','-7 day')"),
            "dailyTraces": _scalar(conn, "SELECT COALESCE(SUM(traces),0) FROM stats_daily "
                                         "WHERE day >= date('now','-7 day')"),
        }
        biz["usage"] = {
            "tokens": _scalar(conn, "SELECT COALESCE(SUM(total_tokens),0) FROM token_usage"),
            "cost": round(float(_scalar(conn, "SELECT COALESCE(SUM(cost),0) FROM token_usage")), 4),
            "topAgents": _dist(conn, "SELECT agent, COUNT(*) FROM token_usage GROUP BY agent "
                                     "ORDER BY COUNT(*) DESC LIMIT 5"),
            "last24h": _scalar(conn, "SELECT COALESCE(SUM(total_tokens),0) FROM token_usage "
                                     "WHERE at >= datetime('now','-1 day')"),
        }
        biz["llm"] = {
            "total": _scalar(conn, "SELECT COUNT(*) FROM llm_models"),
            "disabled": _scalar(conn, "SELECT COUNT(*) FROM llm_models WHERE status!='active'"),
        }
        biz["hotspots"] = _scalar(conn, "SELECT COUNT(*) FROM hotspots")
        biz["reports"] = _scalar(conn, "SELECT COUNT(*) FROM reports")
    finally:
        conn.close()
    return biz


# ---------------------------------------------------------------------------
# 三、文件资产盘点：素材库、知识库、交付物、OpenMontage 工程
# ---------------------------------------------------------------------------
def _load_materials(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(data, list):
        return {"root": "", "updated": "", "items": data}
    return data if isinstance(data, dict) else None


def audit_materials():
    """素材盘点：服务端在用的工作区清单 + vault 副本，各自核对 root 与文件可达性。"""
    out = {"sources": [], "total": 0, "categories": {}, "missing": [], "rootExists": os.path.isdir(MATERIALS_ROOT)}
    candidates = ((os.path.join(OWNER_DATA_DIR, "materials.json"), "工作区素材清单（服务端在用）"),
                  (MATERIALS_VAULT, "vault 素材库副本"),
                  (MATERIALS_FILE, "工作台素材清单"))
    seen = set()
    for path, name in candidates:
        if not os.path.exists(path) or os.path.realpath(path) in seen:
            continue
        data = _load_materials(path)
        if data is None:
            continue
        seen.add(os.path.realpath(path))
        items = data.get("items") or []
        root = str(data.get("root") or "")
        root_ok = bool(root) and os.path.isdir(root)
        eff_root = root if root_ok else MATERIALS_ROOT
        cats, missing = {}, []
        for it in items:
            cat = str(it.get("category") or "未分类")
            cats[cat] = cats.get(cat, 0) + 1
            url = str(it.get("url") or "")
            if url.startswith("/materials/") and (root_ok or os.path.isdir(MATERIALS_ROOT)):
                real = os.path.join(eff_root, unquote(url[len("/materials/"):]))
                if not os.path.exists(real):
                    missing.append(it.get("title") or url)
        entry = {"name": name, "path": path, "count": len(items),
                 "updated": data.get("updated") or "", "root": root,
                 "rootOk": root_ok, "categories": cats, "missing": missing}
        out["sources"].append(entry)
        out["total"] = max(out["total"], len(items))
        for k, v in cats.items():
            out["categories"][k] = out["categories"].get(k, 0) + v
        if root_ok:
            out["missing"] += missing
    return out


def audit_knowledge_files():
    out = {"files": 0, "archived": 0, "recent7d": 0, "weekly": [], "index": False,
           "sizeKb": 0, "oldest": "", "newest": ""}
    out["index"] = os.path.exists(os.path.join(KB_ROOT, "00-索引.md"))
    if not os.path.isdir(KB_ROOT):
        return out
    now = _now()
    stamp = []
    for path in glob.glob(os.path.join(KB_ROOT, "**", "*.md"), recursive=True):
        if os.path.basename(path).startswith("00-索引"):
            continue
        try:
            st = os.stat(path)
            head = open(path, encoding="utf-8", errors="ignore").read(800)
        except OSError:
            continue
        out["files"] += 1
        out["sizeKb"] += _kb(st.st_size)
        if "archived: true" in head.replace(" ", "").lower():
            out["archived"] += 1
        mtime = datetime.fromtimestamp(st.st_mtime, TZ)
        stamp.append(mtime)
        if (now - mtime).days <= 7:
            out["recent7d"] += 1
    if stamp:
        out["oldest"], out["newest"] = _iso(min(stamp)), _iso(max(stamp))
    # 近 7 天按日新增（知识流通趋势）
    buckets = {}
    for mtime in stamp:
        if (now - mtime).days <= 7:
            day = mtime.strftime("%m-%d")
            buckets[day] = buckets.get(day, 0) + 1
    out["weekly"] = [{"day": k, "files": v} for k, v in sorted(buckets.items())]
    return out


def audit_outputs():
    """交付物目录与 OpenMontage 工程：工厂实际出片口径。"""
    out = {"deliverables": {}, "projects": [], "renders": 0, "artifacts": 0}
    trace_dirs = [d for d in glob.glob(os.path.join(DELIVERABLE_ROOT, "*")) if os.path.isdir(d)]
    previews = glob.glob(os.path.join(DELIVERABLE_ROOT, "*", "*", "05-preview.mp4"))
    out["deliverables"] = {
        "traces": len(trace_dirs),
        "previews": len(previews),
        "noPreview": len([d for d in trace_dirs
                          if not glob.glob(os.path.join(d, "*", "05-preview.mp4"))]),
        "sizeMb": int(sum(os.path.getsize(p) for p in previews if os.path.exists(p)) / 1048576),
    }
    for proj in sorted(glob.glob(os.path.join(OM_PROJECTS, "*"))):
        if not os.path.isdir(proj):
            continue
        renders = glob.glob(os.path.join(proj, "renders", "**", "*"), recursive=True)
        renders = [r for r in renders if os.path.isfile(r)]
        arts = [a for a in glob.glob(os.path.join(proj, "artifacts", "*")) if os.path.isfile(a)]
        out["renders"] += len(renders)
        out["artifacts"] += len(arts)
        out["projects"].append({"name": os.path.basename(proj), "renders": len(renders),
                                "artifacts": len(arts),
                                "updated": _iso(datetime.fromtimestamp(os.path.getmtime(proj), TZ))})
    return out


def collect():
    return {"at": _iso(_now()), "db": audit_databases(), "biz": audit_business(),
            "materials": audit_materials(), "knowledgeFiles": audit_knowledge_files(),
            "outputs": audit_outputs()}


# ---------------------------------------------------------------------------
# 四、问题发现（规则引擎；kb_manager 在此之上补充分析）
# ---------------------------------------------------------------------------
def find_issues(inv):
    issues = []

    def add(level, area, title, detail, suggestion):
        issues.append({"level": level, "area": area, "title": title,
                       "detail": detail, "suggestion": suggestion})

    biz = inv["biz"]
    kf = inv["knowledgeFiles"]
    mat = inv["materials"]
    out = inv["outputs"]
    db = inv["db"]

    # 1) 知识索引与落盘不一致（孤儿索引 / 未登记条目）
    krow = (biz.get("knowledge") or {}).get("rows") or 0
    no_path = (biz.get("knowledge") or {}).get("noPath") or 0
    if krow and abs(krow - kf["files"]) > max(10, kf["files"]):
        add("high", "知识库",
            f"知识索引与落盘严重不一致（索引 {krow} 条 / 文件 {kf['files']} 篇）",
            f"孤儿/失效索引约 {max(0, krow - kf['files'])} 条，其中无 path 字段 {no_path} 条；检索会命中已不存在的条目。",
            "按 path 字段回扫磁盘做一次重建：保留可解析文件，标记失效条目 archived，并在 00-索引.md 补全目录。")
    if kf["files"] and not kf["index"]:
        add("mid", "知识库", "知识库缺少总索引 00-索引.md",
            f"磁盘有 {kf['files']} 篇条目但没有总索引文件，人工查找只能靠文件名。",
            "生成 00-索引.md（按分类+标题+日期），并由每日盘点任务自动校验索引覆盖。")

    # 2) 内容单阻塞（工厂进料口堵塞）
    c = biz.get("contents") or {}
    total = c.get("total") or 0
    blocked = c.get("blockedBy") or {}
    blocked_n = (c.get("stages") or {}).get("blocked", 0)
    if total and blocked_n and blocked_n / total >= 0.5:
        detail = "；".join(f"{k} {v} 条" for k, v in sorted(blocked.items(), key=lambda x: -x[1])[:4])
        add("high", "生产",
            f"内容单大面积阻塞：{blocked_n}/{total}（{round(blocked_n * 100 / total)}%）",
            (f"阻塞归因：{detail or '未标注 blockedBy'}。" if detail else "阻塞单均未写 blockedBy，无法定位卡点。"),
            "按 blockedBy 分类处置：审核未过的回炉返工，等待审批的集中向老板发起，缺素材的补采；"
            "超过 3 天的阻塞单进入归档或重开。")

    # 3) 工厂产出断点（有内容单、有样片，但无发布回执）
    rel = biz.get("release") or {}
    if total >= 20 and (rel.get("published") or 0) <= 1 and (rel.get("ledger") or 0) == 0:
        add("high", "生产",
            f"工厂产出转化断点：产品 {total} 单、已发布 {rel.get('published', 0)} 单、发布回执 {rel.get('ledger', 0)} 条",
            f"交付物目录已有 {out['deliverables'].get('previews', 0)} 个成片预览，但发布台账为空，"
            "说明成片停留在审批/发布环节，未形成对外产出。",
            "核对审批单与发布动作：能上线的补齐审批后由发布岗执行；不能上线的写清撤回原因并归档，避免长期占用在制库存。")

    # 4) 流水线滞留
    stale = [x for x in ((biz.get("pipeline") or {}).get("stale") or [])
             if isinstance(x.get("ageHours"), (int, float)) and x["ageHours"] > 24]
    if stale:
        add("high", "流水线",
            f"流水线滞留 {len(stale)} 条 running 超过 24 小时",
            "；".join(f"{x['trace']}({x.get('stage') or '-'}，{x['ageHours']}h)" for x in stale[:4]),
            "由 CTO 核对进程与断点续跑开关（OPC_RESUME_AUTO），无法续跑的标记 timeout 并释放资源。")

    # 5) 过程库空表（结构存在但无数据）
    for dbx in db["business"]:
        if dbx.get("role") != "owner":
            continue
        empty = [t["name"] for t in dbx["tables"] if t.get("rows") == 0]
        if empty:
            add("mid", "数据库", f"过程库有 {len(empty)} 张空表",
                "、".join(empty[:10]) + ("…" if len(empty) > 10 else ""),
                "确认是否为未启用的功能：长期空表在文档里标注「未启用」，避免盘点口径被误读。")

    # 6) 空壳用户库
    empty_users = [x["label"] for x in db["business"]
                   if x.get("role") == "user" and (x.get("rows") or 0) == 0]
    if empty_users:
        add("low", "数据库",
            f"{len(empty_users)} 个用户工作区库为空壳（仅建表无数据）",
            "、".join(empty_users[:6]),
            "确认是测试账号还是注册后从未使用；僵尸账号清理，避免备份与统计口径膨胀。")

    # 7) 素材库：清单多副本不一致 / 根目录不可达 / 文件缺失
    if len(mat["sources"]) >= 2 and len({s["count"] for s in mat["sources"]}) > 1:
        add("mid", "素材库", "素材清单存在多份不一致副本",
            "；".join(f"{s['name']} {s['count']} 条（root={s['root'] or '未标注'}）"
                     for s in mat["sources"]),
            "以工作区 materials.json 为唯一事实源，vault 副本定期同步或删除，避免按过期清单取数。")
    bad_root = [s for s in mat["sources"] if not s["rootOk"]]
    if bad_root:
        add("mid", "素材库", f"{len(bad_root)} 份素材清单的 root 不可达",
            "；".join(f"{s['name']} root={s['root']}" for s in bad_root),
            "修正 root 为服务端实际路径（MATERIALS_ROOT），否则该副本条目无法回源核对。")
    if mat["sources"] and not mat["rootExists"]:
        add("high", "素材库", "素材库根目录不可达",
            f"MATERIALS_ROOT={MATERIALS_ROOT} 不存在，素材条目无法回源核对。",
            "恢复挂载或修正 MATERIALS_ROOT 环境变量。")
    if mat["missing"]:
        add("mid", "素材库", f"素材条目缺文件 {len(mat['missing'])} 条",
            "、".join(mat["missing"][:6]),
            "按 url 回源补齐文件，或把条目标记失效；每日盘点持续跟踪缺失数量变化。")

    # 8) 用量与成本集中
    usage = biz.get("usage") or {}
    top = list((usage.get("topAgents") or {}).items())
    if top and usage.get("tokens"):
        name, calls = top[0]
        share = round(calls * 100 / sum(v for _, v in top), 1)
        if share >= 50 and calls >= 50:
            add("low", "成本", f"调用量集中于 {name}（{calls} 次，Top5 占比 {share}%）",
                f"知识库/系统库累计 {usage.get('tokens')} tokens，金额 ${usage.get('cost')}。",
                "核对是否为合理的高频岗位；若为轮询/重试导致，收敛调用频率或加缓存。")

    # 9) 交付物缺成片
    if out["deliverables"].get("noPreview"):
        add("mid", "交付物",
            f"{out['deliverables']['noPreview']} 个 trace 目录没有成片预览",
            f"交付物目录 {out['deliverables']['traces']} 个 trace，其中成片 {out['deliverables']['previews']} 个。",
            "确认是制作中还是废弃 trace；废弃的清入 _归档，制作中的补挂进度。")
    return issues


# ---------------------------------------------------------------------------
# 五、kb_manager 智能分析（模型不可用时纯规则兜底）
# ---------------------------------------------------------------------------
_ANALYSIS_SCHEMA = ("summary(≤80字，直接向老板汇报规模与最要紧的问题), "
                    "findings(3-6条字符串，每条「问题—影响」), "
                    "suggestions(2-4条字符串，可执行), evidence(2-4条，引用给定事实字段), "
                    "confidence(0到1)")


def _facts(inv, issues):
    biz = inv["biz"]
    return {
        "盘点时间": inv["at"],
        "数据库": inv["db"]["total"],
        "员工库": {"库数": inv["db"]["profiles"]["count"], "表数": inv["db"]["profiles"]["tables"]},
        "产品（内容单）": {"总数": (biz.get("contents") or {}).get("total"),
                       "去重标题": (biz.get("contents") or {}).get("titles"),
                       "阶段分布": (biz.get("contents") or {}).get("stages")},
        "工厂产出": biz.get("release"),
        "交付物": inv["outputs"]["deliverables"],
        "OpenMontage": {"工程数": len(inv["outputs"]["projects"]),
                        "成片文件": inv["outputs"]["renders"],
                        "中间产物": inv["outputs"]["artifacts"]},
        "素材库": {"条目": inv["materials"]["total"], "分类": inv["materials"]["categories"],
                 "缺文件": len(inv["materials"]["missing"])},
        "知识库": inv["knowledgeFiles"],
        "用量": biz.get("usage"),
        "规则发现": [f"{i['level']}/{i['area']}:{i['title']}" for i in issues],
    }


def _analysis(kernel, inv, issues):
    fallback = {"summary": (f"共盘点 {inv['db']['total']['dbs']} 个库 / {inv['db']['total']['tables']} 张表 / "
                            f"{inv['db']['total']['rows']} 行数据；产品 {(inv['biz'].get('contents') or {}).get('total', 0)} 单、"
                            f"已发布 {(inv['biz'].get('release') or {}).get('published', 0)} 单；"
                            f"知识库 {inv['knowledgeFiles']['files']} 篇；发现问题 {len(issues)} 项。"),
                "findings": [i["title"] for i in issues[:6]],
                "suggestions": [i["suggestion"] for i in issues[:4]],
                "evidence": ["规则引擎直接统计过程库与 vault 落盘"],
                "confidence": 0.6, "engine": "rule"}
    if kernel is None:
        return fallback
    try:
        data, meta = kernel.call_agent(
            "kb_manager",
            "你是知识库管理，负责每日资产盘点。基于给定真实事实，写资产盘点结论：规模概况、最要紧的问题及其影响、可执行建议。"
            "只允许使用给定事实里的数字，不得编造；规则发现已列出的问题不要重复罗列，做归纳和排序。",
            json.dumps(_facts(inv, issues), ensure_ascii=False),
            "", _ANALYSIS_SCHEMA)
    except Exception as exc:
        _log(f"kb_manager 分析失败：{type(exc).__name__}: {exc}")
        return fallback
    if not data or not str(data.get("summary") or "").strip():
        return fallback
    merged = {**fallback, **{k: data.get(k) for k in
                             ("summary", "findings", "suggestions", "evidence", "confidence")
                             if data.get(k)}}
    merged["engine"] = meta.get("engine") or "hermes"
    return merged


# ---------------------------------------------------------------------------
# 六、报告渲染与落盘
# ---------------------------------------------------------------------------
def _fmt_rows(n):
    if isinstance(n, int):
        return f"{n:,}"
    return str(n if n is not None else "-")


def _table(header, rows):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _db_section(db):
    lines = [f"总计 **{db['total']['dbs']}** 个库 / **{db['total']['tables']}** 张表 / "
             f"**{_fmt_rows(db['total']['rows'])}** 行数据 / **{db['total']['sizeKb'] / 1024:.1f} MB**", ""]
    for group, title in (("business", "业务过程库（按用户工作区）"),
                         ("system", "系统与运行库"),
                         ("profiles", "Hermes 员工 profile 库（汇总）")):
        lines.append(f"### {title}")
        if group == "profiles":
            p = db["profiles"]
            lines.append(f"{p['count']} 个库、{p['tables']} 张表、{p['sizeKb'] / 1024:.1f} MB，"
                         f"其中有数据（>64KB）的 {p['nonEmpty']} 个。")
            lines.append("")
            continue
        for d in db[group]:
            head = f"**{d['label']}** · {d['path']} · {d['sizeKb'] / 1024:.1f} MB · {d['tableCount']} 表 · {_fmt_rows(d['rows'])} 行"
            lines.append(head)
            if d.get("error"):
                lines.append(f"- 读取失败：{d['error']}")
                lines.append("")
                continue
            rows = [(f"`{t['name']}`", _fmt_rows(t["rows"]), t["cols"] or "-") for t in d["tables"]]
            lines.append(_table(["表", "数据量", "结构（字段:类型）"], rows))
            lines.append("")
    return "\n".join(lines)


def render(inv, issues, analysis, day=None):
    day = day or _now().strftime("%Y-%m-%d")
    biz = inv["biz"]
    c = biz.get("contents") or {}
    rel = biz.get("release") or {}
    kf = inv["knowledgeFiles"]
    mat = inv["materials"]
    out = inv["outputs"]
    high = [i for i in issues if i["level"] == "high"]
    mid = [i for i in issues if i["level"] == "mid"]
    low = [i for i in issues if i["level"] == "low"]
    L = []
    L.append(f"# OPC 资产盘点日报 · {day}")
    L.append("")
    L.append(f"> 生成时间 {inv['at']}｜盘点人 祁南（知识库管理）｜引擎 {analysis.get('engine', 'rule')}"
             f"｜口径：过程库只读 + vault 落盘 + OpenMontage 工程")
    L.append("")
    L.append("## 一、总览")
    L.append("")
    L.append(_table(["指标", "数值"], [
        ["数据库", f"{inv['db']['total']['dbs']} 个库 / {inv['db']['total']['tables']} 张表 / {_fmt_rows(inv['db']['total']['rows'])} 行 / {inv['db']['total']['sizeKb'] / 1024:.1f} MB"],
        ["产品（内容单）", f"{c.get('total', 0)} 单 / 去重标题 {c.get('titles', 0)} 个"],
        ["工厂产出", f"已发布 {rel.get('published', 0)} 单｜发布回执 {rel.get('ledger', 0)} 条｜成片预览 {out['deliverables'].get('previews', 0)} 个｜工程渲染 {out['renders']} 个文件"],
        ["素材库", "；".join(f"{s['name']} {s['count']} 条" for s in mat["sources"]) +
         f"｜缺文件 {len(mat.get('missing') or [])} 条" if mat["sources"] else "未找到素材清单"],
        ["知识库", f"{kf['files']} 篇（近 7 天新增 {kf['recent7d']} 篇，归档 {kf['archived']} 篇）｜索引表 {(biz.get('knowledge') or {}).get('rows', 0)} 条"],
        ["问题", f"高危 {len(high)} 项 / 中等 {len(mid)} 项 / 提示 {len(low)} 项"],
    ]))
    L.append("")
    L.append("## 二、数据库盘点")
    L.append("")
    L.append(_db_section(inv["db"]))
    L.append("## 三、产品与工厂产出")
    L.append("")
    if c.get("stages"):
        L.append("**产品阶段分布**")
        L.append("")
        L.append(_table(["阶段", "数量"],
                        [(k, v) for k, v in sorted(c["stages"].items(), key=lambda x: -x[1])]))
        L.append("")
    if c.get("projects"):
        L.append("**按项目**：" + "；".join(f"{k} {v} 单" for k, v in c["projects"].items()))
        L.append("")
    if c.get("blockedBy"):
        L.append("**阻塞归因**：" + "；".join(f"{k} {v} 单" for k, v in
                                         sorted(c["blockedBy"].items(), key=lambda x: -x[1])))
        L.append("")
    L.append(_table(["产出指标", "数值"], [
        ["交付物 trace 目录", out["deliverables"].get("traces", 0)],
        ["成片预览 05-preview.mp4", out["deliverables"].get("previews", 0)],
        ["预览体积", f"{out['deliverables'].get('sizeMb', 0)} MB"],
        ["OpenMontage 工程", f"{len(out['projects'])} 个（渲染文件 {out['renders']}，中间产物 {out['artifacts']}）"],
        ["近 7 天日汇总发布", rel.get("dailyPublished", 0)],
        ["近 7 天日汇总流水线", rel.get("dailyTraces", 0)],
    ]))
    L.append("")
    if out["projects"]:
        L.append(_table(["工程", "渲染文件", "中间产物", "最近更新"],
                        [(p["name"], p["renders"], p["artifacts"], p["updated"]) for p in out["projects"]]))
        L.append("")
    L.append("## 四、素材库")
    L.append("")
    if mat["sources"]:
        L.append(_table(["来源", "条目", "分类数", "更新时间", "缺文件"],
                        [(s["name"], s["count"], len(s["categories"]), s["updated"] or "-", len(s["missing"]))
                         for s in mat["sources"]]))
        L.append("")
        L.append("**分类分布**：" + "；".join(f"{k} {v}" for k, v in
                                        sorted(mat["categories"].items(), key=lambda x: -x[1])))
    else:
        L.append("未找到素材库清单（materials.json）。")
    L.append("")
    L.append("## 五、知识库")
    L.append("")
    L.append(_table(["指标", "数值"], [
        ["知识条目文件", kf["files"]],
        ["体积", f"{kf['sizeKb']} KB"],
        ["近 7 天新增", kf["recent7d"]],
        ["归档条目", kf["archived"]],
        ["总索引 00-索引.md", "存在" if kf["index"] else "缺失"],
        ["索引表记录", (biz.get("knowledge") or {}).get("rows", 0)],
        ["索引无 path 记录", (biz.get("knowledge") or {}).get("noPath", 0)],
    ]))
    L.append("")
    if kf["weekly"]:
        L.append("**近 7 天日增**：" + "；".join(f"{w['day']} {w['files']} 篇" for w in kf["weekly"]))
        L.append("")
    L.append("## 六、问题清单")
    L.append("")
    if issues:
        rows = [(i["level"], i["area"], i["title"]) for i in issues]
        L.append(_table(["级别", "领域", "问题"], rows))
        L.append("")
        for i in issues:
            L.append(f"- **[{i['level']}] {i['title']}**")
            L.append(f"  - 详情：{i['detail']}")
            L.append(f"  - 建议：{i['suggestion']}")
    else:
        L.append("本轮未发现异常。")
    L.append("")
    L.append("## 七、智能分析（祁南）")
    L.append("")
    L.append(f"**结论**：{analysis.get('summary') or '-'}")
    L.append("")
    if analysis.get("findings"):
        L.append("**发现**：")
        L += [f"- {x}" for x in analysis["findings"]]
        L.append("")
    if analysis.get("suggestions"):
        L.append("**建议**：")
        L += [f"- {x}" for x in analysis["suggestions"]]
        L.append("")
    if analysis.get("evidence"):
        L.append("**依据**：" + "；".join(str(x) for x in analysis["evidence"]))
        L.append("")
    L.append("---")
    L.append("> 本报告由 OPC 工作台每日凌晨自动盘点生成（过程库只读），口径变化请同步更新 asset_audit.py。")
    return "\n".join(L)


def _save(inv, md):
    os.makedirs(REPORT_DIR, exist_ok=True)
    day = _now().strftime("%Y-%m-%d")
    md_path = os.path.join(REPORT_DIR, f"{day}-资产盘点日报.md")
    json_path = os.path.join(REPORT_DIR, f"{day}-资产盘点快照.json")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in inv.items() if k in ("at", "db", "biz", "materials",
                                                             "knowledgeFiles", "outputs")},
                      f, ensure_ascii=False, indent=1)
    except OSError as exc:
        _log(f"快照写入失败：{exc}")
    latest = os.path.join(REPORT_DIR, "最新.md")
    try:
        with open(latest, "w", encoding="utf-8") as f:
            f.write(md)
    except OSError as exc:
        _log(f"最新报告副本写入失败：{exc}")
    return {"markdown": md_path, "snapshot": json_path, "latest": latest}


def _send(kernel, inv, issues, analysis, md):
    day = _now().strftime("%Y-%m-%d")
    high = sum(1 for i in issues if i["level"] == "high")
    subject = (f"[OPC 资产盘点] {day} · {inv['db']['total']['dbs']}库/"
               f"{inv['db']['total']['tables']}表/产品{(inv['biz'].get('contents') or {}).get('total', 0)}单/"
               f"高危{high}项")
    try:
        if kernel is not None:
            return kernel.mail_boss(subject, md, "kb_manager")
        import mail as MAILM
        return MAILM.send_internal_mail("kb_manager", [BOSS_MAIL], subject, md)
    except Exception as exc:
        _log(f"盘点报告发送失败：{type(exc).__name__}: {exc}")
        return {"ok": False, "error": str(exc)}


def _emit(kernel, inv, issues, analysis):
    if kernel is None:
        return
    try:
        kernel.emit("report", "", {
            "id": f"rp_inventory_{_now().strftime('%Y%m%d')}",
            "kind": "inventory",
            "title": f"{_now().strftime('%Y-%m-%d')} 资产盘点日报",
            "conclusion": analysis.get("summary") or "",
            "evidence": analysis.get("evidence") or [],
            "issues": len(issues),
            "engine": analysis.get("engine") or "rule",
            "createdAt": _iso(_now()),
        })
    except Exception as exc:
        _log(f"盘点事件推送失败：{type(exc).__name__}: {exc}")


def run_inventory(kernel=None, send_mail=True):
    """一次完整盘点：采数 → 规则问题 → 智能分析 → 报告落盘 → 邮件 → 事件。"""
    t0 = time.time()
    inv = collect()
    issues = find_issues(inv)
    analysis = _analysis(kernel, inv, issues)
    md = render(inv, issues, analysis)
    paths = _save(inv, md)
    mail = _send(kernel, inv, issues, analysis, md) if send_mail else {"ok": False, "skipped": True}
    _emit(kernel, inv, issues, analysis)
    _log(f"盘点完成：{len(issues)} 项问题，用时 {round(time.time() - t0, 1)}s，报告 {paths['markdown']}")
    return {"day": _now().strftime("%Y-%m-%d"), "dbs": inv["db"]["total"]["dbs"],
            "tables": inv["db"]["total"]["tables"], "rows": inv["db"]["total"]["rows"],
            "issues": issues, "analysis": analysis, "paths": paths, "mail": mail,
            "costMs": int((time.time() - t0) * 1000)}


_RUN_LOCK = threading.Lock()


def trigger(kernel=None, send_mail=True):
    """手动触发一次盘点（后台线程，防重入）；与每日凌晨定时任务共用同一条链路。"""
    if not _RUN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "盘点进行中，请稍后再试"}
    def work():
        try:
            run_inventory(kernel, send_mail=send_mail)
        except Exception as exc:
            _log(f"手动盘点失败：{type(exc).__name__}: {exc}")
        finally:
            _RUN_LOCK.release()
    threading.Thread(target=work, daemon=True, name="inventory-manual").start()
    return {"ok": True, "started": True, "at": _iso(_now())}


# ---------------------------------------------------------------------------
# 七、每日凌晨定时任务
# ---------------------------------------------------------------------------
def next_run(at=None, now=None):
    at = at or INVENTORY_AT
    try:
        hh, mm = [int(x) for x in str(at).split(":")[:2]]
    except ValueError:
        hh, mm = 2, 30
    now = now or _now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def start(kernel_getter=None, stop_event=None, at=None):
    """启动每日凌晨盘点线程；kernel_getter 返回当前 owner 内核（可为 None → 纯规则）。"""
    if os.environ.get("OPC_INVENTORY", "1") == "0":
        _log("已通过 OPC_INVENTORY=0 关闭")
        return None
    at = at or INVENTORY_AT
    stop = stop_event or threading.Event()

    def loop():
        _log(f"每日资产盘点已启动，执行时刻 {at}（下一次 {_iso(next_run(at))}）")
        while not stop.is_set():
            target = next_run(at)
            while not stop.is_set():
                remain = (target - _now()).total_seconds()
                if remain <= 0:
                    break
                if stop.wait(min(remain, 300)):
                    return
            if stop.is_set():
                return
            try:
                k = kernel_getter() if kernel_getter else None
            except Exception:
                k = None
            try:
                run_inventory(k)
            except Exception as exc:
                _log(f"本轮盘点失败：{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=loop, daemon=True, name="asset-inventory")
    thread.start()
    return thread


if __name__ == "__main__":
    import sys
    dry = "--dry" in sys.argv
    result = run_inventory(None, send_mail=not dry)
    print(json.dumps({"dbs": result["dbs"], "tables": result["tables"], "rows": result["rows"],
                      "issues": [i["title"] for i in result["issues"]],
                      "paths": result["paths"], "mail": result["mail"],
                      "costMs": result["costMs"]}, ensure_ascii=False, indent=1))