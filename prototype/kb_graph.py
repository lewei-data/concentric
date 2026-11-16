"""知识图谱检索器（只读、零第三方依赖）：把简化 ontology 变成可复用的检索能力。

两个入口共用同一套打分与渲染：
1. 内核提示词注入：kernel.recall_kb() → brief()，把子图压成一小段高密度上下文；
2. agent 按需深挖：python3 kb_graph.py brief/show/stats，用到再取，不预先塞满上下文。

设计取向（效率优先）：
- 只给有证据的节点：词面命中（标题/标签/摘要/正文）或与种子节点存在关系边；
- 每条压成一行：标题 + 最相关正文句 + 标签 + 出处路径，便于 agent 直接 read_file 原文；
- 整个块有硬性字符预算，超了就截断，绝不无限膨胀。

实测（2026-11-16，32 次真实调用，报告见 OPC/技术研究/2026-11-16_知识图谱注入AB实测报告.md）：
- 库内有答案时注入把 token 降 63%~74%、推理轮次减 2~4 轮；库内无答案时只有持平；
  命中为空时注入串为空，零开销。收益来自"省掉 agent 自己翻 vault"，不是"知道得更多"。
- 四条硬规则（改动前必读）：
  1) 流水产物（FLOW_TAGS）直接 0 分：三项目各存一份、互链成伪枢纽，纯噪音；
  2) 入度 >= HUB_DEGREE 的节点不参与图扩展；
  3) 沿 ref 边扩展时邻居自身必须与问题词面相关（tag 边不受限）；
  4) 摘要必须取"与问题最相关的一句"（跳过标题复读行）；提示语里不写可执行命令，
     否则会诱导有 terminal 权限的角色再跑一轮脚本，实测能把 -35% 反转成 +56%。
复跑：python3 tools/kb_ab_bench.py
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
HUB_DEGREE = 4               # 入度 >= 此值视为枢纽节点：图扩展不借它扩散
# 流水产物（选题定稿/日榜/热点）在三个项目各存一份、互相引用，形成度 10 的伪枢纽，
# 内容对"回答问题"没有价值 —— 直接不进候选，避免白占预算还把真知识挤掉。
FLOW_TAGS = ("选题定稿", "日榜", "热点/GitHub")
SOFT_TAGS = ("知识内化",)     # 学习笔记：可召回，但降权，别压过技能/规范条目
SOFT_WEIGHT = 0.5

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


_kb_cache = {"key": None, "at": 0.0, "items": []}


def _frontmatter(text):
    """极简 frontmatter：只取 `key: value` 行，正文原样返回（与 deliverables 解析口径一致）。"""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = {}
            for line in parts[1].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            return fm, parts[2].lstrip("\n")
    return {}, text


def _vault_nodes(vault=None):
    """直接读 OPC/知识库 的 md（Obsidian 是唯一真源，60 秒缓存）。

    db 的 knowledge 表只 upsert 不删：被移走的条目会变成"死节点"，正文也滞后于 md。
    CLI 读 db、内核读内存这两套口径不同步，正是"注入了过时知识"的来源之一。
    """
    import time as _t
    vault = vault or os.environ.get("OPC_VAULT") or "/opt/sefs/obsidian"
    root = os.path.join(vault, "OPC", "知识库")
    total, latest = 0, 0.0
    for base, _d, files in os.walk(root):
        for name in files:
            if name.endswith(".md") and not name.startswith("00-"):
                total += 1
                try:
                    latest = max(latest, os.path.getmtime(os.path.join(base, name)))
                except OSError:
                    pass
    key = (root, total, round(latest, 3))
    now = _t.time()
    if _kb_cache["key"] == key and now - _kb_cache["at"] < 60:
        return _kb_cache["items"]
    items = []
    for base, _d, files in os.walk(root):
        for name in files:
            if not name.endswith(".md") or name.startswith("00-"):
                continue
            p = os.path.join(base, name)
            try:
                text = open(p, encoding="utf-8").read()
            except OSError:
                continue
            fm, body = _frontmatter(text)
            title = (fm.get("title") or "").strip()
            if not title:
                m = re.search(r"^#\s+(.+)$", body, re.M)
                title = m.group(1).strip() if m else os.path.splitext(name)[0]
            if not title:
                continue
            kept = []
            for line in body.splitlines():
                if line.strip().startswith("- 标签："):
                    break
                kept.append(line)
            full = "\n".join(kept).strip()
            summary = ""
            for line in full.splitlines():
                s = line.strip().lstrip("#").strip()
                if s and not s.startswith("|") and not s.startswith("-"):
                    summary = s[:120]
                    break
            items.append({"id": (fm.get("kb_id") or "").strip(), "title": title,
                          "summary": summary, "body": full,
                          "tags": re.findall(r"[\w\u4e00-\u9fa5/\-]+", fm.get("tags") or "")[:8],
                          "source": os.path.relpath(p, vault)})
    _kb_cache.update(key=key, at=now, items=items)
    return items


def load(path=None, vault=None):
    """读全图：节点（以 vault 的 md 为准）+ 边（knowledge_relations）。库不可用时只有词面召回。"""
    path = path or default_db()
    nodes, edges = {}, []
    conn = None
    try:
        conn = _connect(path)
    except sqlite3.Error:
        conn = None
    if conn is not None:
        files = None
        try:
            for row in conn.execute(
                    "SELECT id,title,summary,body,tags,path,category,raw FROM knowledge"):
                nid, title = str(row[0] or ""), str(row[1] or "").strip()
                if not nid or not title:
                    continue
                src = str(row[5] or "")
                if not src:                   # path 列历史为空：raw 里的 source 也不可靠
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

    # 内容以 md 为准：id 沿用 db（保证边能对上），db 里已不存在的条目自然消失
    items = _vault_nodes(vault)
    if not items:
        return nodes, edges
    by_src = {n["source"]: nid for nid, n in nodes.items() if n.get("source")}
    by_title = {re.sub(r"\s+", "", n["title"]): nid for nid, n in nodes.items()}
    fresh = {}
    for it in items:
        nid = (by_src.get(it["source"])
               or by_title.get(re.sub(r"\s+", "", it["title"])) or it["id"])
        if not nid:
            nid = "kb_" + re.sub(r"[^\w\u4e00-\u9fa5]+", "_", it["title"])[:40]
        it["id"] = nid
        it["summary"] = _clean_summary(it["summary"]) or it["summary"]
        fresh[nid] = it
    edges = [e for e in edges if e["src"] in fresh and e["dst"] in fresh]
    return fresh, edges


def _clean_summary(text):
    """摘要取第一句有信息量的话：剥掉「选题定稿/来源热点」等元信息前缀。"""
    s = re.sub(r"[*`]{1,3}", "", str(text or ""))     # 去掉 md 强调符：注入块按纯文本用
    s = re.sub(r"\s+", " ", s).strip()
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
    if any(t in node["tags"] for t in FLOW_TAGS):
        return 0
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
    score = strong + weak
    return score * SOFT_WEIGHT if any(t in node["tags"] for t in SOFT_TAGS) else score


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


# 元文档（审计/复盘/审批原则）几乎每篇都会提到，挂到任何种子下面都不提供新信息
_META_DOC = ("审计报告", "复盘", "审批原则")


def _is_meta_doc(node):
    t = str(node.get("title") or "")
    return any(k in t for k in _META_DOC)


def _trim(text, n):
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _best_sentence(n, qg, qw, limit=110):
    """取与问题最相关的一句正文，而不是固定首句。

    注入块只有一句话的空间：给"最有用的那句"能直接回答问题，agent 就不用再
    read_file 原文（实测翻原文是 token 暴涨的主因）；给标题式摘要则会诱发二次深挖。
    """
    text = (n.get("summary") or "") + "。" + (n.get("body") or "")[:1200]
    tg, tw = parts(n.get("title"))
    best, best_sc = "", -1
    for s in re.split(r"[。；;!?！？\n]+", text):
        s = s.strip()
        if len(s) < 8 or s.startswith(_META_PREFIX):
            continue
        g, w = parts(s)
        # 正文首行常是标题复读，跟问题重合度最高却零信息，直接跳过
        if len(g & tg) + len(w & tw) >= max(1, (len(g) + len(w)) // 2):
            continue
        sc = len(qg & g) * 2 + len(qw & w) * 3
        if sc > best_sc:
            best, best_sc = s, sc
    if best_sc <= 0:
        best = n.get("summary") or ""
    return _trim(best, limit)


def _line(n, mark="●", qg=None, qw=None):
    """单节点一行：标题 + 最相关正文句 + 标签 + 出处。出处让 agent 能直接读原文。

    一条约 200 字，900 字预算放 3 条 —— 宁可少而准，不要多而空。
    """
    parts = [f"{mark} 《{_trim(n['title'], 40)}》"]
    if qg is not None:
        sent = _best_sentence(n, qg, qw or set())
    else:
        sent = _trim(n["summary"], 46)
    if sent:
        parts.append(sent)
    if n["tags"]:
        parts.append(" ".join("#" + _trim(t, 8) for t in n["tags"][:2]))
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
        line = _line(nodes[nid], qg=qg, qw=qw)
        rels = [(oid, e) for oid, e in _neighbors(nid, edges)
                if oid not in shown and not _is_meta_doc(nodes[oid])]
        if rels:
            # 邻居只列 2 个短标题：它的作用是提示"还有相邻主题"，不是正文
            label = "、".join(f"《{_trim(nodes[o]['title'], 16)}》"
                             f"({'引用' if e['kind'] == 'ref' else e['label'] or '关联'})"
                             for o, e in rels[:2])
            line += "\n   ↳ " + label
        if used + len(line) > budget:
            break
        body.append(line)
        used += len(line)
        shown.add(nid)

    # 图扩展：种子的一跳邻居，只在预算还有余量时补 1-2 个，用低权重标记。
    # 跳过枢纽节点（入度 >= HUB_DEGREE）：被到处引用的多是审计报告/流水记录，
    # 顺着它扩散只会把无关条目塞进提示词。
    if expand and used < budget:
        indeg = {}
        for e in edges:
            indeg[e["dst"]] = indeg.get(e["dst"], 0) + 1
        extra, seen_extra = [], set()
        for s, nid in seeds:
            for oid, e in _neighbors(nid, edges):
                if oid in shown or oid in seen_extra or indeg.get(oid, 0) >= HUB_DEGREE:
                    continue
                # 引用边（ref）只说明"这两篇互相提到过"，邻居本身可能与本问题毫无关系
                # （审计报告、审批原则这类被到处引用）。要求邻居自身也有词面相关才补。
                if e["kind"] != "tag" and _score(qg, qw, nodes[oid]) <= 0:
                    continue
                seen_extra.add(oid)
                extra.append((s * 0.35 + e["weight"], oid))
            if len(extra) >= 3:
                break
        added = 0
        for sc, oid in sorted(extra, key=lambda x: -x[0]):
            if added >= 2:
                break
            line = _line(nodes[oid], mark="○", qg=qg, qw=qw) + "（图扩展）"
            if used + len(line) + 1 > budget:
                break
            body.append(line)
            used += len(line) + 1
            added += 1

    head = (f"【知识图谱命中】{len(body)} 条 / 图 {len(nodes)} 节点 "
            f"{len(edges)} 边（词面+引用边自动检索；与本任务无关可忽略）")
    # 提示语里不写具体命令：写了会诱导有 terminal 权限的角色去跑脚本再深挖一轮，
# 实测 cto/kb_manager 的调用反而因此变贵。
    tail = "需要原文时按上面出处路径 read_file，不必全库搜索。"
    return "\n".join([head, *body, tail])


def show(nid, path=None, chars=None):
    """展开单条知识 + 它的邻居，供 agent 按需深挖（比读整篇 md 更省）。"""
    chars = chars or DEFAULT_SHOW_CHARS
    nodes, edges = load(path)
    n = nodes.get(str(nid or ""))
    if not n:
        return f"未找到知识条目：{nid}"
    lines = [f"《{n['title']}》", f"id: {n['id']}",
             f"分类: {n.get('category') or '-'}｜标签: {'、'.join(n.get('tags') or []) or '-'}",
             f"出处: {n.get('source') or '-'}"]
    if n.get("summary"):
        lines.append(f"摘要: {_trim(n.get('summary'), 200)}")
    rels = _neighbors(n["id"], edges)
    if rels:
        lines.append("关系: " + "；".join(
            f"{'→' if e['kind'] == 'ref' else '—'}《{_trim(nodes[o]['title'], 24)}》"
            f"（{e['label'] or e['kind']}）" for o, e in rels[:8]))
    body = re.sub(r"\n{3,}", "\n\n", n.get("body") or "").strip()
    lines.append("正文: " + (body[:chars] + ("…" if len(body) > chars else "") if body else "-"))
    return "\n".join(lines)


def stats(path=None):
    nodes, edges = load(path)
    kinds, cats = {}, {}
    for e in edges:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    for n in nodes.values():
        # load() 的节点没有 category 键（分类是 Web 端 _infer_category 才补的），这里必须容错
        cat = n.get("category") or "-"
        cats[cat] = cats.get(cat, 0) + 1
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