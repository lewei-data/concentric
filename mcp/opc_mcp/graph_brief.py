"""ask(with_brief) 的知识图谱简报适配层：复用 kb_graph 的打分/图边，但按 MCP 口径加工。

与工作台的 kb_graph.brief() 三处不同：
1. 只按当前 key 的手机号读用户 opc.db + vault，且逐条套 resources.shield 屏蔽；
2. 按五行(category)/三才(sancai)分桶轮转取种子，避免单一分类吃满预算；
3. 层级(level 0/1/2) + 硬字符预算 + 随规模自适应的单条压缩，库变大也不会膨胀。

kb_graph 是纯 stdlib 只读模块，本文件不修改它的默认行为（工作台注入口径保持不变）。
"""

import re
import sqlite3
import sys
from pathlib import Path

from . import config, resources

_PROTO = Path(__file__).resolve().parents[2] / "prototype"
_SANCAI_LABEL = {"tian": "天", "di": "地", "ren": "人"}
_DIMS = ("tian", "di", "ren", "other")
_EMPTY = {"hit": False, "text": "", "chars": 0, "level": 0, "nodes": 0, "edges": 0,
          "groups": {d: 0 for d in _DIMS}, "items": [], "budget": 0, "query": "",
          "mode": "off", "reason": ""}

# 对外口径：知识库正文里大量内网地址/服务端路径，远程 agent 既用不了也不该看到。
# 只替换内网形式，公网入口（ai.boraydata.com）与 vault 相对路径（可配合 opc_resource_read）保留。
_PUBLIC_RULES = (
    (re.compile(r"https?://(?:localhost|(?:192\.168|10|172\.(?:1[6-9]|2\d|3[01])|127)\.[\d.]+)"
                r"(?::\d+)?[^\s，。；、）)`]*", re.I), "<内网服务地址>"),
    (re.compile(r"(?:192\.168|10|172\.(?:1[6-9]|2\d|3[01]))\.[\d.]+(?::\d+)?"
                r"|\blocalhost(?::\d+)?", re.I), "<内网地址>"),
    (re.compile(r"/(?:mnt/sefs|opt/sefs|home/boray)[^\s，。；、）)`]*"), "<服务端路径>"),
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}\b"), "<内网服务>"),
    (re.compile(r"\b\d{1,3}:\d{4,5}\b"), "<内网服务>"),   # 206:9105 这类内网主机简写
)


def public_text(value):
    """把正文/标题里的内网地址与服务端路径替换为对外可读占位，不动公网 URL。"""
    s = str(value or "")
    for pattern, repl in _PUBLIC_RULES:
        s = pattern.sub(repl, s)
    return s


def _kb():
    if str(_PROTO) not in sys.path:
        sys.path.insert(0, str(_PROTO))
    try:
        import kb_graph
        return kb_graph
    except Exception:
        return None


def available():
    return _kb() is not None


def _norm_sancai(value):
    v = str(value or "").strip().lower()
    if v in ("tian", "天", "heaven"):
        return "tian"
    if v in ("di", "地", "earth"):
        return "di"
    if v in ("ren", "人", "human"):
        return "ren"
    return "other"


def _meta_by_id(path):
    """知识条目的五行/三才：优先 vault frontmatter，库只作兜底（见 _node_dims）。"""
    out = {}
    if not Path(path).is_file():
        return out
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3)
        for r in c.execute("SELECT id,category,sancai FROM knowledge"):
            out[str(r[0])] = (str(r[1] or ""), str(r[2] or ""))
        c.close()
    except sqlite3.Error:
        pass
    return out


def _node_dims(kb, nid, node, meta, vault):
    cat, sc = meta.get(nid, ("", ""))
    src = str(node.get("source") or "")
    if src and vault:
        p = vault / src
        if p.is_file():
            try:
                fm, _ = kb._frontmatter(p.read_text(encoding="utf-8", errors="replace")[:1200])
                cat = str(fm.get("category") or "") or cat
                sc = str(fm.get("sancai") or "") or sc
            except OSError:
                pass
    return _norm_sancai(sc), cat


def build(store, phone, question, level=None, budget=None):
    """生成简报。有命中返回 hit=True + text；零命中 text=''（对提示词零开销）。"""
    out = dict(_EMPTY)
    out["query"] = str(question or "")
    kb = _kb()
    if not kb:
        out["reason"] = "kb_graph 不可用"
        return out
    level = config.BRIEF_LEVEL if level is None else max(0, min(2, int(level)))
    budget = config.BRIEF_BUDGET if budget is None else max(200, min(config.BRIEF_HARD_MAX, int(budget)))
    out["level"], out["budget"] = level, budget
    qg, qw = kb.parts(question)
    if len(qg) + len(qw) < 2:
        out["reason"] = "问题特征不足"
        return out

    db, vault = resources.db_path(phone), config.user_vault(phone)
    try:
        nodes, edges = kb.load(str(db), vault=str(vault))
    except Exception as e:
        out["reason"] = "load failed: %s" % type(e).__name__
        return out
    out["nodes"], out["edges"] = len(nodes), len(edges)
    if not nodes:
        out["reason"] = "知识图为空"
        return out

    meta = _meta_by_id(db)
    scored, seen = [], set()
    for nid, node in nodes.items():
        if resources.shield(str(node.get("source") or "")) or resources.shield(str(node.get("title") or "")):
            continue
        s = kb._score(qg, qw, node)
        if s < kb.MIN_SCORE:
            continue
        key = re.sub(r"\s+", "", str(node.get("title") or ""))
        if not key or key in seen:                       # 同标题只留最高分，避免占预算
            continue
        seen.add(key)
        dim, cat = _node_dims(kb, nid, node, meta, vault)
        scored.append((s, nid, dim, cat))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        out["reason"] = "无词面命中"
        return out

    buckets = {d: [] for d in _DIMS}
    for item in scored:
        buckets[item[2]].append(item)

    scale = min(1.0, budget / 900.0)
    limit = max(1, min(5 if level <= 1 else 3, int(budget / 180)))
    max_group = min(config.BRIEF_MAX_GROUP, limit)
    seeds = []
    while len(seeds) < limit and any(buckets.values()):
        progressed = False
        for dim in _DIMS:                                # 天→地→人→未标注 轮转，五行三才均衡
            if len(seeds) >= limit:
                break
            if sum(1 for x in seeds if x[2] == dim) >= max_group or not buckets[dim]:
                continue
            seeds.append(buckets[dim].pop(0))
            progressed = True
        if not progressed:
            break
    if not seeds:
        out["reason"] = "分组后无种子"
        return out

    # 预算分层：头尾固定留白；层级越高，给种子正文的份额越少。
    head_reserve = int(90 * scale) + 20
    tail_reserve = int(60 * scale) + 20
    nb_reserve = int(30 * scale) * len(seeds) if level >= 1 else 0
    ext_reserve = int(160 * scale) if level >= 2 else 0
    seed_budget = max(40, budget - head_reserve - tail_reserve - nb_reserve - ext_reserve)
    per_seed = max(50, seed_budget // len(seeds))
    title_n = max(10, min(40, int(per_seed * 0.28)))
    sent_n = max(30, min(110, per_seed - title_n - 22))

    indeg = {}
    for e in edges:
        indeg[e["dst"]] = indeg.get(e["dst"], 0) + 1
    shown, body = set(), []
    per_nb = max(0, nb_reserve // max(1, len(seeds)))

    def neighbor_lines(nid):
        lines = []
        for oid, e in kb._neighbors(nid, edges):
            if len(lines) >= 2 or not per_nb:
                break
            other = nodes.get(oid)
            if not other or oid in shown or oid == nid or indeg.get(oid, 0) >= kb.HUB_DEGREE:
                continue
            if resources.shield(str(other.get("source") or "")) or resources.shield(str(other.get("title") or "")):
                continue
            if kb._is_meta_doc(other):
                continue
            if e["kind"] != "tag" and kb._score(qg, qw, other) <= 0:
                continue
            tag = "引用" if e["kind"] == "ref" else str(e.get("label") or "关联")[:8]
            lines.append("  ↳ 《%s》(%s)" % (kb._trim(public_text(other.get("title")), 12), tag))
        return [kb._trim(x, per_nb) for x in lines]

    for s, nid, dim, cat in seeds:
        n = nodes[nid]
        bits = ["●", "[%s]" % _SANCAI_LABEL.get(dim, "·"),
                "《%s》" % kb._trim(public_text(n.get("title")), title_n)]
        sent = public_text(kb._best_sentence(n, qg, qw, limit=sent_n))
        if sent:
            bits.append(sent)
        if n.get("tags"):
            bits.append(" ".join("#" + kb._trim(t, 6) for t in n["tags"][:2]))
        if n.get("source"):
            bits.append("→" + kb._trim(n["source"], 40))
        body.append(kb._trim("｜".join(bits), per_seed))
        shown.add(nid)
        if level >= 1:
            body.extend(neighbor_lines(nid))

    if level >= 2 and ext_reserve:
        extra, seen_extra = [], set()
        for s, nid, *_ in seeds:
            for oid, e in kb._neighbors(nid, edges):
                other = nodes.get(oid)
                if not other or oid in shown or oid in seen_extra or indeg.get(oid, 0) >= kb.HUB_DEGREE:
                    continue
                if resources.shield(str(other.get("source") or "")) or kb._is_meta_doc(other):
                    continue
                if e["kind"] != "tag" and kb._score(qg, qw, other) <= 0:
                    continue
                seen_extra.add(oid)
                extra.append((s * 0.35 + e["weight"], oid))
                if len(extra) >= 3:
                    break
            if len(extra) >= 3:
                break
        for _, oid in sorted(extra, key=lambda x: -x[0])[:2]:
            n = nodes[oid]
            bits = ["○", "《%s》" % kb._trim(public_text(n.get("title")), title_n)]
            sent = public_text(kb._best_sentence(n, qg, qw, limit=max(30, sent_n // 2)))
            if sent:
                bits.append(sent)
            if n.get("source"):
                bits.append("→" + kb._trim(n["source"], 36))
            body.append("（图扩展）" + kb._trim("｜".join(bits), min(per_seed, ext_reserve // 2)))

    counts = {d: 0 for d in _DIMS}
    for _, _, dim, _ in seeds:
        counts[dim] += 1
    head = kb._trim("【知识图谱·五行三才】%d 条 / 图 %d 节点 %d 边（天%d 地%d 人%d）" % (
        len(seeds), len(nodes), len(edges), counts["tian"], counts["di"], counts["ren"]), head_reserve)
    tail = kb._trim("完整出处见 items[].source / citations[].path；正文 → 为截断显示，勿直接读取。", tail_reserve)
    text = "\n".join([head, *body, tail])
    if len(text) > budget:                               # 硬上限：库再大也不越过预算
        text = text[:budget - 1].rstrip() + "…"

    out.update({
        "hit": True, "text": text, "chars": len(text), "mode": "graph",
        "groups": counts,
        "items": [{"id": nid, "title": public_text(nodes[nid].get("title")), "category": cat,
                   "sancai": dim, "source": public_text(nodes[nid].get("source")),
                   "score": round(s, 2), "rels": len(kb._neighbors(nid, edges))}
                  for s, nid, dim, cat in seeds],
    })
    return out


def merged_context(store, phone, brief, budget=None):
    """图谱简报 + MCP 独有源（素材卡片/片段/共享），opc.knowledge 由简报覆盖。"""
    budget = budget or config.CONTEXT_BUDGET
    lines, used = [], 0
    text = brief.get("text") or ""
    if text:
        lines.append(text)
        used += len(text)
    for card in (store.cards(phone, limit=3) if store is not None else []):
        block = public_text("【素材卡片】%s\n%s" % (card.get("filename") or card.get("title") or "",
                                                str(card.get("summary") or "")[:200]))
        if used + len(block) > budget:
            break
        lines.append(block)
        used += len(block)
    for hit in resources.search(store, phone, brief.get("query") or "", k=8):
        if hit["source"] == "opc.knowledge":             # 已被图谱简报按更高密度覆盖
            continue
        block = public_text("【%s】%s\n%s" % (hit["source"], hit["title"] or hit["id"],
                                          str(hit["snippet"])[:160]))
        if used + len(block) > budget:
            break
        lines.append(block)
        used += len(block)
    return "\n\n".join(lines)
