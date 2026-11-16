"""知识图谱检索器（只读、零第三方依赖）：把简化 ontology 变成可复用的检索能力。

两个入口共用同一套打分与渲染：
1. 内核提示词注入：kernel.recall_kb() → brief()，把子图压成一小段高密度上下文；
2. agent 按需深挖：python3 kb_graph.py brief/show/stats，用到再取，不预先塞满上下文。

设计取向（效率优先）：
- 只给有证据的节点：词面命中（标题/标签/摘要/正文）或与种子节点存在关系边；
- 每条压成一行：标题 + 摘要 + 标签 + 出处路径，便于 agent 直接 read_file 原文；
- 整个块有硬性字符预算，超了就截断，绝不无限膨胀。
"""
import argparse
import json
import os
import re
import sqlite3
import sys

# 与 kernel 保持一致的召回参数（阈值/预算可按环境变量微调）
DEFAULT_LIMIT = 5            # 种子节点数
DEFAULT_BUDGET = 900         # 注入块字符预算
DEFAULT_SHOW_CHARS = 1200    # show 子命令的正文上限
MIN_SCORE = 4                # 词面低于此分不算命中（单个中文 bigram 不做数）

# 摘要首行常是论文式元信息，对回答问题没有价值，渲染前剥掉这些前缀
_META_PREFIX = ("选题定稿", "来源热点", "项目：", "项目:", "trace", "Trace", "kb_id", "归档")
_vault_cache = {"at": 0.0, "vault": "", "index": {}}


_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9_\-.]{1,}")


def parts(text):
    """特征拆两类：中文按字符 bigram，英文/数字按整词。

    纯字符 bigram 会让 ComfyUI 的 co/om 命中 CCO 之类的无关词；英文改用整词后，
    只有词本身一致才算命中，中文仍保持免分词的粗粒度匹配。
    """
    raw = str(text or "")
    s = re.sub(r"[\s\W_]+", "", raw).lower()
    grams = {s[i:i + 2] for i in range(len(s) - 1)}
    grams = {g for g in grams if not g.isascii()}
    return grams, set(_ASCII_WORD.findall(raw.lower()))


def features(text):
    """两类特征的并集（外部只想判空时用）。"""
    g, w = parts(text)
    return g | w


def core_title(title):
    """去掉副标题与括注，得到标题核心。"""
    s = str(title or "")
    s = re.sub(r"[（(].*$", "", s)
    s = re.sub(r"\s*[—–]\s*.*$", "", s)
    return re.sub(r"\s+", "", s)


def default_db():
    """库路径：显式环境变量 > 与本文件同目录的 opc.db（单机默认布局）。"""
    for key in ("OPC_KB_DB", "OPC_DB_PATH"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "opc.db")


def _connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _as_list(raw):
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []
    except Exception:
        return []


def _vault_files(vault):
    """vault 内知识条目文件索引：{kb_id 或标题: 相对路径}，60 秒缓存。

    历史库里 path 列为空，靠文件名反查把「出处」补回来 —— agent 拿到路径就能
    read_file 原文，不必自己 search_files 猜。
    """
    import time as _t
    vault = vault or os.environ.get("OPC_VAULT") or "/opt/sefs/obsidian"
    now = _t.time()
    if _vault_cache["vault"] == vault and now - _vault_cache["at"] < 60:
        return _vault_cache["index"]
    index = {}
    root = os.path.join(vault, "OPC", "知识库")
    for base, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".md") or name.startswith("00-"):
                continue
            p = os.path.join(base, name)
            rel = os.path.relpath(p, vault)
            stem = os.path.splitext(name)[0]
            index.setdefault(stem, rel)
            try:
                head = open(p, encoding="utf-8").read(800)
            except OSError:
                continue
            m = re.search(r"^kb_id:\s*(\S+)", head, re.M)
            if m:
                index.setdefault(m.group(1).strip(), rel)
            m = re.search(r"^title:\s*(.+)$", head, re.M)
            if m:
                index.setdefault(re.sub(r"\s+", "", m.group(1).strip()), rel)
    _vault_cache.update({"at": now, "vault": vault, "index": index})
    return index


def load(path=None, vault=None):
    """读全图：节点（知识条目）+ 边（knowledge_relations）。库不可用时返回空图。"""
    path = path or default_db()
    nodes, edges = {}, []
    try:
        conn = _connect(path)
    except sqlite3.Error:
        return nodes, edges
    files = None
    try:
        for row in conn.execute(
                "SELECT id,title,summary,body,tags,path,category,raw FROM knowledge"):
            nid, title = str(row[0] or ""), str(row[1] or "").strip()
            if not nid or not title:
                continue
            src = str(row[5] or "")
            if not src:                       # path 列历史为空：raw 里的 source 也不可靠
                try:
                    src = str((json.loads(row[7] or "{}") or {}).get("source") or "")
                except Exception:
                    src = ""
            if not src:
                files = files if files is not None else _vault_files(vault)
                src = files.get(nid) or files.get(re.sub(r"\s+", "", title)) or ""
            nodes[nid] = {"id": nid, "title": title,
                          "summary": _clean_summary(row[2]),
                          "body": str(row[3] or ""),
                          "tags": _as_list(row[4])[:8],
                          "source": src, "category": str(row[6] or "")}
        for row in conn.execute(
                "SELECT src,dst,kind,label,weight FROM knowledge_relations"):
            src, dst = str(row[0] or ""), str(row[1] or "")
            if src in nodes and dst in nodes:
                edges.append({"src": src, "dst": dst, "kind": str(row[2] or "tag"),
                              "label": str(row[3] or ""), "weight": float(row[4] or 1)})
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return nodes, edges


def _clean_summary(text):
    """摘要取第一句有信息量的话：剥掉「选题定稿/来源热点」等元信息前缀。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return ""
    for seg in re.split(r"[；;]", s):
        seg = seg.strip()
        if seg and not seg.startswith(_META_PREFIX):
            return seg
    return s


def _score(qg, qw, node):
    """词面相关度：标题 > 标签 > 摘要 > 正文。

    准入只看标题/标签（strong）：泛词（模型/内容/方法）在长摘要与正文里必然命中，
    若让它们决定入围，召回就会变成噪声。strong 不足 MIN_SCORE 直接判为无关。
    """
    tg, tw = parts(node["title"])
    strong = len(qg & tg) * 3 + len(qw & tw) * 4
    for t in node["tags"]:
        g, w = parts(t)
        strong += len(qg & g) * 2 + len(qw & w) * 3
    if strong < MIN_SCORE:
        return 0
    sg, sw = parts(node["summary"])
    bg, bw = parts(node["body"][:1500])
    weak = len(qg & sg) * 1.0 + len(qw & sw) * 1.5
    weak += min(len(qg & bg) * 0.3 + len(qw & bw) * 0.6, 2.0)
    return strong + weak


def _neighbors(nid, edges):
    """一跳邻居：返回 [(对方 id, 边)]，权重高的在前。"""
    out = []
    for e in edges:
        if e["src"] == nid:
            out.append((e["dst"], e))
        elif e["dst"] == nid:
            out.append((e["src"], e))
    out.sort(key=lambda x: -x[1]["weight"])
    return out


def _trim(text, n):
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _line(n, mark="●"):
    """单节点一行：标题 + 摘要 + 标签 + 出处。出处让 agent 能直接读原文。

    每个字段都有字数上限：一条 130~160 字，900 字预算能放进 5 条左右。
    """
    parts = [f"{mark} 《{_trim(n['title'], 40)}》"]
    if n["summary"]:
        parts.append(_trim(n["summary"], 46))
    if n["tags"]:
        parts.append(" ".join("#" + _trim(t, 10) for t in n["tags"][:3]))
    if n["source"]:
        parts.append("→" + n["source"])
    return "｜".join(parts)


def brief(query, path=None, limit=None, budget=None, expand=True, vault=None):
    """把问题相关的子图压成一段提示词文本；无命中返回空串（对提示词零影响）。"""
    budget = budget or DEFAULT_BUDGET
    limit = limit or DEFAULT_LIMIT
    qg, qw = parts(query)
    if len(qg) + len(qw) < 2:
        return ""
    nodes, edges = load(path, vault=vault)
    if not nodes:
        return ""
    scored = sorted(((_score(qg, qw, n), nid) for nid, n in nodes.items()),
                    key=lambda x: -x[0])
    # 同标题重复条目只留分最高的一条：重复条目会白占预算、还会让模型以为是两件事
    seen_titles, seeds = set(), []
    for s, nid in scored:
        if s < MIN_SCORE:
            break
        key = re.sub(r"\s+", "", nodes[nid]["title"])
        if key in seen_titles:
            continue
        seen_titles.add(key)
        seeds.append((s, nid))
        if len(seeds) >= max(1, limit):
            break
    if not seeds:
        return ""

    body = []
    used = 0
    shown = set()
    for s, nid in seeds:
        line = _line(nodes[nid])
        rels = [(oid, e) for oid, e in _neighbors(nid, edges) if oid not in shown]
        if rels:
            label = "、".join(f"《{_trim(nodes[o]['title'], 20)}》"
                             f"({'引用' if e['kind'] == 'ref' else e['label'] or '关联'})"
                             for o, e in rels[:3])
            line += "\n   ↳ " + label
        if used + len(line) > budget:
            break
        body.append(line)
        used += len(line)
        shown.add(nid)

    # 图扩展：种子的一跳邻居，只在预算还有余量时补 1-2 个，用低权重标记
    if expand and used < budget:
        extra, seen_extra = [], set()
        for s, nid in seeds:
            for oid, e in _neighbors(nid, edges):
                if oid in shown or oid in seen_extra:
                    continue
                seen_extra.add(oid)
                extra.append((s * 0.35 + e["weight"], oid))
            if len(extra) >= 3:
                break
        added = 0
        for sc, oid in sorted(extra, key=lambda x: -x[0]):
            if added >= 2:
                break
            line = _line(nodes[oid], mark="○") + "（图扩展）"
            if used + len(line) + 1 > budget:
                break
            body.append(line)
            used += len(line) + 1
            added += 1

    head = (f"【知识图谱命中】{len(body)} 条 / 图 {len(nodes)} 节点 "
            f"{len(edges)} 边（词面+引用边自动检索；与本任务无关可忽略）")
    tail = "深挖单条：按上面出处 read_file 原文，或 kb_graph.py show <id>"
    return "\n".join([head, *body, tail])


def show(nid, path=None, chars=None):
    """展开单条知识 + 它的邻居，供 agent 按需深挖（比读整篇 md 更省）。"""
    chars = chars or DEFAULT_SHOW_CHARS
    nodes, edges = load(path)
    n = nodes.get(str(nid or ""))
    if not n:
        return f"未找到知识条目：{nid}"
    lines = [f"《{n['title']}》", f"id: {n['id']}",
             f"分类: {n['category'] or '-'}｜标签: {'、'.join(n['tags']) or '-'}",
             f"出处: {n['source'] or '-'}"]
    if n["summary"]:
        lines.append(f"摘要: {_trim(n['summary'], 200)}")
    rels = _neighbors(n["id"], edges)
    if rels:
        lines.append("关系: " + "；".join(
            f"{'→' if e['kind'] == 'ref' else '—'}《{_trim(nodes[o]['title'], 24)}》"
            f"（{e['label'] or e['kind']}）" for o, e in rels[:8]))
    body = re.sub(r"\n{3,}", "\n\n", n["body"] or "").strip()
    lines.append("正文: " + (body[:chars] + ("…" if len(body) > chars else "") if body else "-"))
    return "\n".join(lines)


def stats(path=None):
    nodes, edges = load(path)
    kinds, cats = {}, {}
    for e in edges:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    for n in nodes.values():
        cats[n["category"] or "-"] = cats.get(n["category"] or "-", 0) + 1
    deg = {}
    for e in edges:
        deg[e["src"]] = deg.get(e["src"], 0) + 1
        deg[e["dst"]] = deg.get(e["dst"], 0) + 1
    orphans = [n["title"] for nid, n in nodes.items() if not deg.get(nid)]
    return {"nodes": len(nodes), "edges": len(edges), "edgeKinds": kinds,
            "categories": cats, "orphans": len(orphans),
            "orphanTitles": orphans[:10], "db": path or default_db()}


def main(argv=None):
    ap = argparse.ArgumentParser(description="OPC 知识图谱检索（只读）")
    ap.add_argument("--db", default=None, help="知识库 sqlite 路径（默认同目录 opc.db）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("brief", help="按问题召回子图（提示词注入用）")
    b.add_argument("query")
    b.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    b.add_argument("--chars", type=int, default=DEFAULT_BUDGET)
    s = sub.add_parser("show", help="展开单条知识及其关系")
    s.add_argument("id")
    s.add_argument("--chars", type=int, default=DEFAULT_SHOW_CHARS)
    sub.add_parser("stats", help="图谱规模与质量概览")
    args = ap.parse_args(argv)

    if args.cmd == "brief":
        print(brief(args.query, path=args.db, limit=args.limit, budget=args.chars))
    elif args.cmd == "show":
        print(show(args.id, path=args.db, chars=args.chars))
    else:
        print(json.dumps(stats(args.db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())