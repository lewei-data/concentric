#!/usr/bin/env python3
"""素材打分与有序淘汰（素材官岗位的职责工具，与 vault_digest.py 同一套真源）。

问题：素材有三类来源（materials.json 卡片 / kb_vault_* 素材聚合条目 / OM 产物登记），
有好有坏，不淘汰会把检索预算和存储耗在死料上。

方案——五行五维打分模型（每维 0~1，加权合成分 0~100）：
  火·引用(35%)   作品贡献：素材标识（id/路径/标题/成员文件名）在流水线真实痕迹
                 （pipeline_runs.state、trace_events.data、product_crafts、知识库正文）
                 中被引用的次数——权重最高，被用过的素材才算活素材。
  金·质量(25%)   描述完整度：有无摘要/描述、成员清单、章节、标签。
  木·新鲜度(20%) mtime 线性衰减，当年内衰减到 0（一年未动≈死料）。
  水·检索(15%)   可检索性：标签数、标题信息量、有明确路径。
  土·底座(5%)    不可再生性保护：数字人形象/品牌KV/BGM 等底座素材 1.0，
                 纯素材(地)0.8、方案(人)0.6、情报(天)0.4。

档位：≥75 优质 / 50-74 可用 / 30-49 待观察 / <30 淘汰候选。
有序淘汰（绝不直接删文件）：
  候选 = 分数<30 且引用=0 → 标记 candidate_since（30 天冷静期）→
  冷静期内被引用自动解除 → 满 30 天仍 <30 且零引用 → 进「淘汰待批」清单，
  等老板审批后人工归档。底座类（protected）档位保底「待观察」，永不进候选。

--apply 落地动作：
  1. materials.json 条目写 score/grade 字段（前端徽章可见，写前自动备份）
  2. knowledge 表 tags 追加「评分·档位」（先清旧档位标签）
  3. vault md frontmatter tags 同步（评分 cron 在 digest 之后跑，覆盖后自愈）
  4. ops/asset_scores.json 状态 + vault OPC/素材评分报告-<date>.md
  5. 有满冷静期候选时写 ops/asset_retire_list.json（淘汰待批清单）

CLI：
  python3 asset_score.py            # dry-run：打分出分布，不动任何数据
  python3 asset_score.py --apply    # 落地评分（materials.json/knowledge/md/状态）
  python3 asset_score.py --json     # 输出机读摘要
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VAULT = os.environ.get("OPC_VAULT",
                       "/mnt/sefs/conring/users/18911789286/obsidian")
OWNER_DB = os.environ.get("OPC_OWNER_DB",
                          "/mnt/sefs/conring/users/18911789286/opc.db")
MATERIALS_JSON = os.environ.get("OPC_MATERIALS_JSON",
                                "/mnt/sefs/conring/users/18911789286/materials.json")
KB_DIR = Path(VAULT) / "OPC" / "知识库"
OPS_DIR = Path(BASE_DIR).parent / "ops"
STATE = OPS_DIR / "asset_scores.json"
TZ = timezone(timedelta(hours=8))

WEIGHTS = {"ref": 35, "quality": 25, "fresh": 20, "ret": 15, "base": 5}
GRADES = [(75, "优质"), (50, "可用"), (30, "待观察"), (0, "淘汰候选")]
RETIRE_AFTER_DAYS = 30          # 冷静期：候选满 30 天仍零引用才进待批清单
RECENT_REF_DAYS = 30            # 30 天内被引用给 ref 维度加成
COOLDOWN_BONUS_MINUTES = 0      # 预留

# 底座类关键词（tags/分类命中即受保护，不可再生）
PROTECTED_KW = ("数字人", "口播形象", "品牌KV", "BGM", "音乐素材", "犬种照片", "狗大人")
# 情报类（热点/内化索引）底座分低
GENERIC_WORDS = set()           # 标识符黑名单占位：过短的标识不参与计数


def grade_of(score: float, protected: bool = False) -> str:
    for th, g in GRADES:
        if score >= th:
            if protected and g == "淘汰候选":
                return "待观察"
            return g
    return "淘汰候选"


# ---------------- 素材对象收集（三类来源） ----------------

def _kb_material_entries():
    """knowledge 表 kb_vault_* 里的素材类条目（聚合登记/OM产物/数字人形象）。"""
    con = sqlite3.connect(f"file:{OWNER_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT id,title,path,tags,summary,raw,updated_at,sancai "
        "FROM knowledge WHERE id LIKE 'kb_vault_%'").fetchall()
    con.close()
    out = []
    for id_, title, path, tags, summary, raw, updated, sancai in rows:
        t = title or ""
        if not (t.startswith(("素材登记", "OM素材", "OM方案"))
                or "内化·素材" in t or "vault·数字人口播形象" in t
                or "vault·videos" in t or "vault·背景音乐" in t
                or "vault·dogs" in t):
            continue
        try:
            raw_d = json.loads(raw) if raw else {}
        except Exception:
            raw_d = {}
        try:
            tags_l = json.loads(tags) if tags else []
        except Exception:
            tags_l = []
        out.append({
            "kind": "kb", "key": id_, "id": id_, "title": t, "path": path or "",
            "tags": tags_l, "sancai": sancai or raw_d.get("sancai", "di"),
            "summary": summary or "", "sections": raw_d.get("sections", []),
            "members": raw_d.get("members", []), "updated": updated or "",
        })
    return out


def _material_items():
    """materials.json 卡片条目（web 素材页真源）。"""
    try:
        d = json.loads(Path(MATERIALS_JSON).read_text(encoding="utf-8"))
    except Exception:
        return []
    items = d.get("items", []) if isinstance(d, dict) else d
    out = []
    for it in items:
        out.append({
            "kind": "card", "key": f"card:{it.get('id')}", "id": it.get("id", ""),
            "title": it.get("title", ""), "path": it.get("url", ""),
            "tags": [it.get("category", "")], "sancai": "di",
            "summary": it.get("desc", ""), "meta": it.get("meta", ""),
            "sections": [], "members": [], "updated": it.get("captured_at", ""),
        })
    return out


def subjects():
    return _material_items() + _kb_material_entries()


# ---------------- 引用语料与计数（火） ----------------

def _table_texts(con, table):
    try:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        if not cols:
            return [], []
        sel = ",".join(cols)
        rows = list(con.execute(f"SELECT {sel} FROM {table}"))
        return [str(r) for r in rows], rows
    except Exception:
        return [], []


def ref_corpus():
    """全部引用语料 + 近 30 天语料（trace_events.at 有时间列）。"""
    con = sqlite3.connect(f"file:{OWNER_DB}?mode=ro", uri=True)
    texts = []
    for table in ("pipeline_runs", "trace_events", "product_crafts", "knowledge"):
        ts, _ = _table_texts(con, table)
        texts += ts
    cut = (datetime.now(TZ) - timedelta(days=RECENT_REF_DAYS)).isoformat()
    recent = []
    try:
        recent = [r[-1] or "" for r in con.execute(
            "SELECT data FROM trace_events WHERE at >= ?", (cut,))]
    except Exception:
        pass
    con.close()
    return texts, recent


def identifiers_of(sub):
    """素材标识：id/路径/标题（足够长才计，防误命中）/成员文件名。"""
    idents = []
    if sub.get("id"):
        idents.append(sub["id"])
    if sub.get("path"):
        idents.append(sub["path"])
        idents.append(os.path.basename(sub["path"].rstrip("/")))
    t = (sub.get("title") or "").split("（")[0].strip()
    if len(t) >= 8:
        idents.append(t)
    for m in sub.get("members", []):
        base = os.path.basename(str(m))
        if len(base) >= 8 and not base.startswith("…"):
            idents.append(base)
    return [i for i in dict.fromkeys(idents) if i and len(i) >= 6]


def count_refs(texts, idents, exclude_self_ids=()):
    """一次拼接全文计数；knowledge 语料里含素材自身 body 的跳过（防自引用虚增）。"""
    n = 0
    for text in texts:
        if any(sid and sid in text for sid in exclude_self_ids):
            continue
        for ident in idents:
            n += text.count(ident)
    return n


# ---------------- 五维打分 ----------------

def score_subject(sub, refs, recent_refs, now_ts):
    idents = identifiers_of(sub)
    # 火·引用
    if refs <= 0:
        ref = 0.0
    elif refs >= 8:
        ref = 1.0
    elif refs >= 3:
        ref = 0.7
    else:
        ref = 0.4
    if refs > 0 and recent_refs > 0:
        ref = min(1.0, ref + 0.1)
    # 金·质量（字段完整度）
    q = 0.0
    if (sub.get("summary") or sub.get("desc")):
        q += 0.4
    if len(sub.get("summary", "")) > 60 or sub.get("meta"):
        q += 0.2
    if sub.get("members"):
        q += 0.25
    if len(sub.get("tags", [])) >= 2 or sub.get("sections"):
        q += 0.15
    q = min(1.0, q)
    # 木·新鲜度（mtime 线性衰减，一年归零）
    mt = _mtime_of(sub)
    days = max(0.0, (now_ts - mt) / 86400) if mt else 365
    fresh = max(0.0, 1.0 - days / 365.0)
    # 水·检索（标签数/标题信息量/有路径）
    r = min(1.0, 0.3 * min(len(sub.get("tags", [])), 3) / 3
            + (0.3 if 8 <= len(sub.get("title", "")) <= 60 else 0.1)
            + (0.3 if sub.get("path") else 0.0)
            + (0.1 if sub.get("members") else 0.0))
    # 土·底座（不可再生保护）
    tags_txt = "、".join(sub.get("tags", [])) + (sub.get("meta") or "")
    protected = any(k in tags_txt for k in PROTECTED_KW)
    if protected:
        base = 1.0
    else:
        base = {"di": 0.8, "ren": 0.6, "tian": 0.4}.get(sub.get("sancai"), 0.8)
    total = sum(WEIGHTS[k] * v for k, v in
                (("ref", ref), ("quality", q), ("fresh", fresh),
                 ("ret", r), ("base", base)))
    return round(total, 1), protected, refs, recent_refs, {
        "火引用": round(ref, 2), "金质量": round(q, 2), "木新鲜": round(fresh, 2),
        "水检索": round(r, 2), "土底座": round(base, 2)}


def _mtime_of(sub):
    """kb 条目取 vault md mtime；卡片条目取 materials.json mtime 或 captured_at。"""
    if sub.get("kind") == "kb":
        hits = list(KB_DIR.glob(f"*{sub['id']}.md"))
        if hits:
            try:
                return hits[0].stat().st_mtime
            except Exception:
                pass
    if sub.get("updated"):
        try:
            d = datetime.fromisoformat(sub["updated"]).timestamp()
            if d > 946684800:
                return d
        except Exception:
            pass
    try:
        return Path(MATERIALS_JSON).stat().st_mtime
    except Exception:
        return 0


# ---------------- 落地动作 ----------------

GRADE_TAG_RE = re.compile(r"评分·(优质|可用|待观察|淘汰候选)")


def apply_card_grades(scored, backup=True):
    """materials.json 条目写 score/grade 字段。"""
    p = Path(MATERIALS_JSON)
    d = json.loads(p.read_text(encoding="utf-8"))
    if backup:
        shutil.copy2(p, str(p) + ".bak-asset-score-"
                     + datetime.now(TZ).strftime("%Y%m%d-%H%M%S"))
    changed = 0
    for it in d.get("items", []):
        s = scored.get(f"card:{it.get('id')}")
        if s and (it.get("score") != s["score"] or it.get("grade") != s["grade"]):
            it["score"], it["grade"] = s["score"], s["grade"]
            changed += 1
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


def apply_kb_tags(scored):
    """knowledge 表 tags + vault md frontmatter 追加档位标签。"""
    con = sqlite3.connect(OWNER_DB)
    changed_db = changed_md = 0
    for sub in _kb_material_entries():
        s = scored.get(sub["key"])
        if not s:
            continue
        gtag = f"评分·{s['grade']}"
        tags = sub["tags"]
        tags = [t for t in tags if not GRADE_TAG_RE.match(str(t))]
        if gtag not in tags:
            tags.append(gtag)
        con.execute("UPDATE knowledge SET tags=? WHERE id=?",
                    (json.dumps(tags, ensure_ascii=False), sub["id"]))
        changed_db += 1
        for md in KB_DIR.glob(f"*{sub['id']}.md"):
            try:
                raw = md.read_text(encoding="utf-8")
            except Exception:
                continue
            m = re.search(r"^tags: (.+)$", raw, re.M)
            if not m:
                continue
            old = [t.strip() for t in m.group(1).split("、") if t.strip()]
            new = [t for t in old if not GRADE_TAG_RE.match(t)]
            if gtag not in new:
                new.append(gtag)
            if new != old:
                md.write_text(re.sub(r"^tags: .+$", "tags: " + "、".join(new),
                                     raw, count=1, flags=re.M), encoding="utf-8")
                changed_md += 1
    con.commit()
    con.close()
    return changed_db, changed_md


def write_report(scored, now, retire_ready):
    """vault OPC/ 素材评分报告（不以「台账」结尾，避开 _vault 屏蔽口径）。"""
    buckets = {}
    for s in scored.values():
        buckets.setdefault(s["grade"], []).append(s)
    lines = [f"# 素材评分报告（{now:%Y-%m-%d %H:%M}）", "",
             "机制：五行五维打分（火引用35/金质量25/木新鲜20/水检索15/土底座5），"
             "档位 ≥75优质 / 50-74可用 / 30-49待观察 / <30淘汰候选；"
             f"候选满 {RETIRE_AFTER_DAYS} 天冷静期仍零引用才进待批清单，绝不直接删文件。", "",
             "## 分布", ""]
    for _, g in GRADES:
        rows = buckets.get(g, [])
        lines.append(f"- {g}：{len(rows)} 项")
        for s in sorted(rows, key=lambda x: -x["score"])[:8]:
            lines.append(f"  - {s['score']} {s['title'][:40]}"
                         f"（引用 {s['refs']}｜{s['subs']}）")
    if retire_ready:
        lines += ["", f"## 淘汰待批（冷静期已满 {RETIRE_AFTER_DAYS} 天，等老板审批后人工归档）", ""]
        for s in retire_ready:
            lines.append(f"- {s['score']} {s['title'][:40]}｜路径：{s['path']}")
    report = Path(VAULT) / "OPC" / f"素材评分报告-{now:%Y-%m-%d}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run(apply=False, as_json=False, quiet=False):
    now = datetime.now(TZ)
    now_ts = now.timestamp()
    subs = subjects()
    texts, recent_texts = ref_corpus()
    try:
        old_state = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        old_state = {}
    scored, retire_ready = {}, []
    for sub in subs:
        idents = identifiers_of(sub)
        self_ids = (sub["id"],) if sub.get("kind") == "kb" else ()
        refs = count_refs(texts, idents, self_ids)
        rref = count_refs(recent_texts, idents, self_ids)
        score, protected, refs, rref, dims = score_subject(sub, refs, rref, now_ts)
        grade = grade_of(score, protected)
        st = dict(old_state.get("items", {}).get(sub["key"], {}))
        if grade == "淘汰候选" and refs == 0 and not protected:
            st.setdefault("candidate_since", now.strftime("%Y-%m-%d"))
            try:
                since = datetime.strptime(st["candidate_since"], "%Y-%m-%d")
                if (now.date() - since.date()).days >= RETIRE_AFTER_DAYS:
                    retire_ready.append({**{"score": score, "grade": grade,
                                            "title": sub["title"],
                                            "path": sub["path"]},
                                         "candidate_since": st["candidate_since"]})
            except Exception:
                pass
        else:
            st.pop("candidate_since", None)     # 冷静期内被引用/分数回升自动解除
        scored[sub["key"]] = {"key": sub["key"], "kind": sub["kind"],
                              "title": sub["title"], "path": sub["path"],
                              "score": score, "grade": grade, "refs": refs,
                              "recent_refs": rref, "dims": dims,
                              "subs": sub.get("kind", "")}
        old_state.setdefault("items", {})[sub["key"]] = st
    ops = {}
    for s in scored.values():
        ops[s["grade"]] = ops.get(s["grade"], 0) + 1
    if as_json:
        print(json.dumps({"updated": now.isoformat(), "total": len(scored),
                          "grades": ops, "retire_ready": len(retire_ready),
                          "items": [{k: s[k] for k in
                                     ("key", "score", "grade", "refs")}
                                    for s in scored.values()]},
                         ensure_ascii=False, indent=1))
        return 0
    if apply:
        n_card = apply_card_grades(scored)
        n_db, n_md = apply_kb_tags(scored)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(
            {"updated": now.isoformat(), "grades": ops,
             "items": old_state["items"]}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        report = write_report(scored, now, retire_ready)
        if retire_ready:
            (OPS_DIR / "asset_retire_list.json").write_text(
                json.dumps(retire_ready, ensure_ascii=False, indent=1),
                encoding="utf-8")
        print(f"[asset-score] 落地：卡片 {n_card} 条 / knowledge tags {n_db} / "
              f"md {n_md}；报告 {report.name}；淘汰待批 {len(retire_ready)} 项",
              flush=True)
        if not quiet:
            print(f"[asset-score] 分布 {json.dumps(ops, ensure_ascii=False)}")
    else:
        print(f"[asset-score] dry-run 共 {len(scored)} 项，分布 "
              f"{json.dumps(ops, ensure_ascii=False)}；淘汰待批候选 "
              f"{len(retire_ready)} 项（--apply 落地）")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    run(apply="--apply" in args, as_json="--json" in args,
        quiet="--quiet" in args)
