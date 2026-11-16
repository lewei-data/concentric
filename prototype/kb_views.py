"""知识库视图层（C1 第二刀）：分类/分页/详情/图谱边/提示词召回。

从 kernel.Kernel 抽出。全部函数以 Kernel 实例为第一参数（纯读或只写 db），
不 import kernel，因此 kernel 可以单向依赖本模块而不成环。

依赖的 kernel 属性仅：lock / knowledge / hotspots / db / vault_dir / DL。
"""
import json
import os
import re
from urllib.parse import parse_qs

import sancai as SC
from obs import log_event
from utils import _hotkey, _to_int


KB_CATEGORIES = [
    {"key": "analysis", "label": "分析调研", "element": "土", "icon": "⛰️",
     "slogan": "厚基、存真、稳态",
     "description": "做厚事实底座，保障素材可信度与完整性；作为全体系的「土壤」，保持相对稳定",
     "color": "#b45309",
     "tags": [
         {"key": "copywriting", "label": "文案分析"}, {"key": "short-video", "label": "短视频分析"},
         {"key": "market", "label": "市场调研"}, {"key": "competitor", "label": "竞品分析"},
         {"key": "trend", "label": "趋势洞察"},
         {"key": "mgmt-research", "label": "管理层调研"},
     ]},
    {"key": "graph", "label": "知识图谱", "element": "水", "icon": "🌊",
     "slogan": "通联、溯源、无滞",
     "description": "核心价值在「连通」而非「堆节点」，建设实体间的关联链路与因果传导路径",
     "color": "#1e40af",
     "tags": [
         {"key": "data-source", "label": "数据源关联"}, {"key": "concept", "label": "概念图谱"},
         {"key": "workflow", "label": "流程图谱"}, {"key": "domain", "label": "领域模型"},
         {"key": "mgmt-benchmark", "label": "管理层对标"},
     ]},
    {"key": "skill", "label": "技能", "element": "金", "icon": "⚔️",
     "slogan": "精专、规整、耐用",
     "description": "像铸造器物一样打磨每个 Skill，边界清晰、输入输出明确、异常处理完备",
     "color": "#a16207",
     "tags": [
         {"key": "hermes", "label": "Hermes 工具"}, {"key": "OpenMontage", "label": "OpenMontage 工艺"},
         {"key": "profile", "label": "Profile 软技能"}, {"key": "pipeline", "label": "流水线"},
         {"key": "ops", "label": "运维"}, {"key": "自定义", "label": "自定义"},
         {"key": "mgmt-audit", "label": "管理层盘点"}, {"key": "mgmt-retire", "label": "淘汰标记"},
     ]},
    {"key": "decision", "label": "决策模型", "element": "火", "icon": "🔥",
     "slogan": "明断、守序、不妄",
     "description": "清晰的判断逻辑与优先级秩序，规则明确、边界清晰；顶层范式不轻易变动",
     "color": "#dc2626",
     "tags": [
         {"key": "domain-model", "label": "领域模型"}, {"key": "strategy", "label": "战略决策"},
         {"key": "risk", "label": "风险评估"}, {"key": "principle", "label": "原则框架"},
         {"key": "heuristic", "label": "经验法则"},
         {"key": "mgmt-insight", "label": "战略洞察"},
     ]},
    {"key": "reflection", "label": "反馈迭代", "element": "木", "icon": "🌿",
     "slogan": "循序、日新、有度",
     "description": "遵循生长规律，小步持续优化；控制迭代节奏与灰度范围，避免拔苗助长",
     "color": "#059669",
     "tags": [
         {"key": "retrospective", "label": "复盘总结"}, {"key": "optimization", "label": "优化方案"},
         {"key": "lesson", "label": "经验教训"}, {"key": "feedback", "label": "用户反馈"},
         {"key": "iteration", "label": "迭代记录"},
         {"key": "mgmt-evolve", "label": "技能进化"},
     ]},
]


KB_CATEGORY_MAP = {c["key"]: c for c in KB_CATEGORIES}


def _infer_category(item):
    """根据已有 tags / projectId 推断旧条目的分类，未命中默认归入知识图谱。"""
    tags_lower = " ".join(str(t) for t in (item.get("tags") or [])).lower()
    title_lower = str(item.get("title") or "").lower()
    combined = tags_lower + " " + title_lower
    # 管理层学习标签优先级最高（精确匹配，避免被通用关键词抢先）
    if "mgmt-evolve" in combined or "技能进化" in combined:
        return "reflection"
    if "mgmt-insight" in combined or "战略洞察" in combined:
        return "decision"
    if "mgmt-benchmark" in combined or "管理层对标" in combined:
        return "graph"
    if "mgmt-research" in combined or "管理层调研" in combined:
        return "analysis"
    if "mgmt-audit" in combined or "mgmt-retire" in combined or "管理层盘点" in combined or "淘汰标记" in combined:
        return "skill"
    # 调研/研究报告统一进「分析调研」，否则标题里没有"调研"二字的技术报告会落到知识图谱
    if "研究报告" in combined or "技术研究" in combined:
        return "analysis"
    # 通用关键词（注意：「技能进化」已在上面精确匹配，此处「技能」不会误判）
    if any(k in combined for k in ("技能", "skill", "方法", "sop")):
        return "skill"
    if any(k in combined for k in ("分析", "调研", "拆解", "短视频", "文案")):
        return "analysis"
    if any(k in combined for k in ("决策", "判断", "原则", "模型", "框架")):
        return "decision"
    if any(k in combined for k in ("反思", "复盘", "优化", "迭代", "反馈", "教训")):
        return "reflection"
    return "graph"


KB_INJECT_ON = (os.environ.get("OPC_KB_INJECT", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")


KB_INJECT_LIMIT = _to_int(os.environ.get("OPC_KB_INJECT_N"), 5) or 5


KB_INJECT_CHARS = _to_int(os.environ.get("OPC_KB_INJECT_CHARS"), 900) or 900


def _rel_key(a, b):
    """无序边键：同一对条目无论方向只存一条。"""
    x, y = (a, b) if a < b else (b, a)
    return f"{x}|{y}"


def _core_title(title):
    """去掉副标题与括注，得到用于「正文引用」匹配的标题核心。"""
    s = str(title or "")
    s = re.sub(r"[（(].*$", "", s)
    s = re.sub(r"\s*[—–]\s*.*$", "", s)
    return re.sub(r"\s+", "", s)


def _bigrams(text):
    """字符 bigram 集合：中文无需分词即可做粗粒度相关性打分。"""
    s = re.sub(r"[\s\W_]+", "", str(text or "")).lower()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _kb_from_row(row):
    """SQLite knowledge 行归一化。

    历史写库只落了 raw（拆分列全空），且混入过无 id/title 的阶段事件行：
    以 raw 为准补齐摘要 / 正文 / 标签 / 时间，非知识条目直接丢弃。
    """
    r = dict(row)
    try:
        raw = json.loads(r.get("raw") or "{}")
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    kid = str(r.get("id") or raw.get("id") or "").strip()
    title = str(r.get("title") or raw.get("title") or "").strip()
    if not kid or not title:
        return None
    try:
        tags = json.loads(r.get("tags")) if r.get("tags") else []
    except Exception:
        tags = []
    if not tags:
        tags = raw.get("tags") or []
    if not isinstance(tags, list):
        tags = [tags]
    tags = [str(t).strip() for t in tags if str(t).strip()][:12]
    body = str(r.get("body") or raw.get("body") or "")
    summary = str(r.get("summary") or raw.get("summary") or "").strip()
    if not summary:
        summary = body[:120] or title[:80]
    category = str(r.get("category") or raw.get("category") or "").strip() \
        or _infer_category({"title": title, "tags": tags})
    sancai = SC.norm(r.get("sancai") or raw.get("sancai")) or SC.infer(
        {"title": title, "summary": summary, "category": category, "tags": tags})
    return {
        "id": kid,
        "title": title,
        "summary": summary,
        "category": category,
        "sancai": sancai,
        "projectId": r.get("project_id") or raw.get("projectId") or "",
        "traceId": r.get("trace_id") or raw.get("traceId") or raw.get("sourceTrace") or "",
        "tags": tags,
        # 早期归档把「落笔说明」写进了 at，用它排序会把条目挤到最前，改回 ISO 时间戳
        "at": str(raw.get("at") or "") if re.match(r"^\d{4}-\d{2}-\d{2}", str(raw.get("at") or ""))
               else str(r.get("updated_at") or raw.get("at") or ""),
        "body": body or summary,
    }


def _hotspot_view(kn, limit=5):
    """同一标题只保留最新一条，避免“简报 + 注入”出现两行重复。"""
    latest = {}
    for h in sorted(kn.hotspots, key=lambda x: str(x.get("at") or "")):
        if h.get("title"):
            latest[_hotkey(h.get("title"))] = h
    return sorted(latest.values(), key=lambda x: str(x.get("at") or ""))[-limit:]


def _page_params(kn, query="", default_size=20, max_size=100):
    """解析 ?page=&size=&q=，供列表接口做服务端分页与关键词过滤。"""
    qs = parse_qs(query)
    try:
        page = max(1, int(qs.get("page", ["1"])[0]))
    except (TypeError, ValueError):
        page = 1
    try:
        size = min(max_size, max(1, int(qs.get("size", [str(default_size)])[0])))
    except (TypeError, ValueError):
        size = default_size
    q = (qs.get("q", [""])[0] or "").strip().lower()
    return page, size, q


def _knowledge_all(kn):
    """合并内存 + SQLite 的全量知识条目（去重、剔除无标题的阶段事件行）。"""
    with kn.lock:
        mem = [k for k in kn.knowledge if k.get("id") and k.get("title")]
    mem_ids = {str(k.get("id")) for k in mem}
    db_items = []
    if kn.db:
        try:
            rows = kn.db.fetch("SELECT id,title,summary,category,sancai,project_id,trace_id,tags,updated_at,body,raw FROM knowledge ORDER BY updated_at DESC")
            for r in rows:
                k = _kb_from_row(r)
                if k and k["id"] not in mem_ids:
                    mem_ids.add(k["id"])
                    db_items.append(k)
        except Exception as e:
            print(f"[kb] SQLite 补充失败：{type(e).__name__}: {e}", flush=True)
    return mem + db_items


def knowledge_categories(kn):
    """返回 5 大分类定义 + 条目数 + 实际标签统计（标签筛选以真实标签为准）。"""
    items = _knowledge_all(kn)
    counts, tag_buckets = {}, {}
    san_counts = {key: 0 for key in SC.SANCAI_KEYS}
    san_cross = {}
    for k in items:
        cat = k.get("category") or _infer_category(k)
        counts[cat] = counts.get(cat, 0) + 1
        sk = SC.norm(k.get("sancai")) or SC.infer(k)
        k["sancai"] = sk
        san_counts[sk] = san_counts.get(sk, 0) + 1
        row = san_cross.setdefault(cat, {key: 0 for key in SC.SANCAI_KEYS})
        row[sk] += 1
        bucket = tag_buckets.setdefault(cat, {})
        for t in (k.get("tags") or []):
            s = str(t).strip()
            if s:
                bucket[s] = bucket.get(s, 0) + 1
    result = []
    for c in KB_CATEGORIES:
        entry = dict(c)
        entry["count"] = counts.get(c["key"], 0)
        entry["sancaiCounts"] = san_cross.get(c["key"], {key: 0 for key in SC.SANCAI_KEYS})
        entry["tagStats"] = [
            {"key": t, "label": t, "count": n}
            for t, n in sorted(tag_buckets.get(c["key"], {}).items(),
                               key=lambda kv: (-kv[1], kv[0]))[:18]
        ]
        result.append(entry)
    sancai_rows = [dict(c, count=san_counts.get(c["key"], 0)) for c in SC.SANCAI]
    return {"ok": True, "items": result, "total": len(result), "page": 1, "size": len(result),
            "sancai": sancai_rows, "sancaiByCat": san_cross,
            "sancaiTotal": len(items), "schema": SC.describe()}


def knowledge_page(kn, query=""):
    """知识库分页列表：支持 ?category=&tag= 过滤，返回卡片摘要。"""
    page, size, q = _page_params(kn, query)
    qs = parse_qs(query)
    cat_filter = (qs.get("category", [""])[0] or "").strip()
    tag_filter = (qs.get("tag", [""])[0] or "").strip()
    sancai_filter = SC.norm((qs.get("sancai", [""])[0] or "").strip())
    # 知识图谱需要正文才能推导「正文引用」边；默认不返回 body，避免列表变重
    with_body = (qs.get("with_body", [""])[0] or "").strip().lower() in ("1", "true", "yes")
    items = _knowledge_all(kn)
    # 确保每条都有 category / sancai 字段（旧数据懒推断）
    for k in items:
        if not k.get("category"):
            k["category"] = _infer_category(k)
        k["sancai"] = SC.norm(k.get("sancai")) or SC.infer(k)
    if cat_filter and cat_filter in KB_CATEGORY_MAP:
        items = [k for k in items if k.get("category") == cat_filter]
    if sancai_filter:
        items = [k for k in items if k.get("sancai") == sancai_filter]
    if tag_filter:
        items = [k for k in items if tag_filter in (k.get("tags") or [])]
    if q:
        items = [k for k in items if q in
                 f"{k.get('title') or ''} {k.get('summary') or ''} {k.get('body') or ''} "
                 f"{k.get('projectId') or ''} {' '.join(k.get('tags') or [])}".lower()]
    items.sort(key=lambda k: str(k.get("at") or ""), reverse=True)
    total = len(items)
    start = (page - 1) * size
    rows = []
    for k in items[start:start + size]:
        row = {"id": k.get("id"),
               "title": k.get("title") or k.get("summary") or "",
               "summary": k.get("summary") or (k.get("title") or "")[:80],
               "category": k.get("category"), "sancai": k.get("sancai"),
               "projectId": k.get("projectId"), "traceId": k.get("traceId"),
               "tags": k.get("tags") or [], "at": k.get("at"),
               "hasBody": bool(k.get("body"))}
        if with_body:
            row["body"] = k.get("body") or ""
        rows.append(row)
    return {"ok": True, "total": total, "page": page, "size": size, "items": rows}


def hotspot_page(kn, query=""):
    """热点分页列表：沿用 _hotspot_view 的同标题去重。"""
    page, size, q = _page_params(kn, query)
    items = sorted(_hotspot_view(kn, 10 ** 6), key=lambda h: str(h.get("at") or ""), reverse=True)
    if q:
        items = [h for h in items if q in
                 f"{h.get('title') or ''} {h.get('source') or ''} "
                 f"{' '.join(h.get('tags') or [])}".lower()]
    san_filter = SC.norm(parse_qs(query).get("sancai", [""])[0])
    # 热点天生属「天」：外部信号。显式标注优先，其余按关键词推断
    rows = []
    for h in items:
        row = dict(h)
        row["sancai"] = SC.norm(h.get("sancai")) or SC.infer(h, "hotspot")
        if san_filter and row["sancai"] != san_filter:
            continue
        rows.append(row)
    items = rows
    total = len(items)
    start = (page - 1) * size
    return {"ok": True, "total": total, "page": page, "size": size,
            "items": items[start:start + size]}


def knowledge_detail(kn, kid):
    with kn.lock:
        hit = next((k for k in kn.knowledge if k.get("id") == kid), None)
    if hit and hit.get("title"):
        return kn._kb_norm(hit)
    # 从 SQLite 查找：同样走 raw 归一化，否则老数据返回的正文/摘要为空
    if kn.db:
        try:
            row = kn.db.fetch_one(
                "SELECT id,title,summary,category,project_id,trace_id,tags,updated_at,body,raw "
                "FROM knowledge WHERE id=?", (kid,))
            if row:
                return _kb_from_row(row)
        except Exception as e:
            log_event("knowledge_detail.db", e, level="warn", kid=kid)
    return None


def hotspot_detail(kn, hid):
    with kn.lock:
        hit = next((h for h in kn.hotspots if h.get("id") == hid), None)
    if not hit:
        return None
    row = dict(hit)
    row["sancai"] = SC.norm(hit.get("sancai")) or SC.infer(hit, "hotspot")
    return row


def _backfill_knowledge(kn):
    """已禁用自动补登。

    流水线选题(deliverable items, 均带 traceId)不属于知识库/知识图谱，
    不应自动沉淀为知识条目。真 KB 由 OPC/知识库 目录的 md 经 load_knowledge
    加载；交付物目录(OPC/交付物/tr_*)只用于历史交付清单，不回填知识库。
    """
    return


def rebuild_relations(kn):
    """用启发式重建关系边并落库：共享标签 + 正文引用（与前端原规则一致）。

    边入库后前端只读不算；后续 AI 抽取的边走 origin='ai'，不被此处覆盖。
    """
    with kn.lock:
        items = [k for k in kn.knowledge
                 if str(k.get("id") or "").strip() and str(k.get("title") or "").strip()]
    by_id = {str(k["id"]): k for k in items}
    pairs = {}
    # 1) 共享标签：只取出现 2~4 次的标签，避免泛标签把全图连死
    tag_groups = {}
    for k in items:
        for t in {str(x).strip() for x in (k.get("tags") or []) if str(x).strip()}:
            tag_groups.setdefault(t, []).append(str(k["id"]))
    for tag, ids in tag_groups.items():
        if not 2 <= len(ids) <= 4:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                p = pairs.setdefault(_rel_key(ids[i], ids[j]),
                                     {"src": ids[i], "dst": ids[j], "tags": [],
                                      "ref": False, "from": "", "to": ""})
                p["tags"].append(tag)
    # 2) 正文引用：A 的正文出现 B 的标题核心 → A 引用 B（有向）
    meta = [(str(k["id"]), re.sub(r"\s+", "", str(k.get("body") or "")).lower())
            for k in items]
    for aid, body in meta:
        if not body:
            continue
        for bid, _ in meta:
            if aid == bid:
                continue
            core = _core_title(by_id[bid].get("title")).lower()
            if len(core) < 4 or core not in body:
                continue
            p = pairs.setdefault(_rel_key(aid, bid),
                                 {"src": aid, "dst": bid, "tags": [],
                                  "ref": False, "from": "", "to": ""})
            p["ref"] = True
            if not p["from"]:
                p["from"], p["to"] = aid, bid
    rows = []
    for key, p in pairs.items():
        tags = sorted(set(p["tags"]))
        src, dst = p["src"], p["dst"]
        if not tags and p["from"]:      # 纯引用边保留真实方向
            src, dst = p["from"], p["to"]
        rows.append({"id": key, "src": src, "dst": dst,
                     "kind": "tag" if tags else "ref",
                     "label": "、".join(f"#{t}" for t in tags) if tags else "引用",
                     "weight": len(tags) + (1 if p["ref"] else 0),
                     # detail 保留引用方向：同一对既共享标签又互引时，kind 会被合并成 tag，
                     # 但入度统计仍需真实 from→to
                     "detail": json.dumps({"tags": tags, "from": p["from"],
                                           "to": p["to"]}, ensure_ascii=False)})
    if kn.db:
        kn.db.replace_relations(rows)
        kn.db.flush()
    print(f"[kb] 关系边重建：{len(rows)} 条 / 条目 {len(items)}", flush=True)
    return rows


def knowledge_relations(kn):
    """读取关系边（图谱页与提示词召回共用）；过滤掉指向已删除条目的孤儿边。"""
    if not kn.db:
        return {"ok": True, "items": []}
    keep = {str(k.get("id")) for k in kn.knowledge if k.get("id")}
    items = [{"id": r["id"], "src": r["src"], "dst": r["dst"],
              "kind": r.get("kind") or "tag", "label": r.get("label") or "",
              "weight": r.get("weight") or 1,
              "origin": r.get("origin") or "heuristic",
              "confirmed": bool(r.get("confirmed")),
              "detail": r.get("detail") or ""}
             for r in kn.db.list_relations()
             if r["src"] in keep and r["dst"] in keep]
    return {"ok": True, "items": items}


def recall_kb(kn, text, limit=None):
    """知识图谱召回：标题/标签词面 → 一跳引用扩展 → 压成有预算的提示词块。

    实现放在 kb_graph.py，与 agent 手工调用的 CLI 共用同一套打分与渲染，
    避免「提示词里一套、命令行里一套」漂移。无命中返回空串（对提示词零影响）。
    """
    if not KB_INJECT_ON:
        return ""
    try:
        import kb_graph
        return kb_graph.brief(
            text,
            path=getattr(kn.db, "path", None),
            vault=kn.vault_dir,
            limit=limit or KB_INJECT_LIMIT,
            budget=KB_INJECT_CHARS,
        )
    except Exception as e:      # 召回只是增益，失败绝不能拖垮任务
        print(f"[kernel] 知识图谱召回异常（忽略）：{type(e).__name__}: {e}", flush=True)
        return ""


def kb_brief(kn, text, limit=None):
    """给外部（HTTP/CLI）的图谱简报，语义同 recall_kb。"""
    return recall_kb(kn, text, limit=limit)


