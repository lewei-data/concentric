#!/usr/bin/env python3
"""个人 obsidian 全库内化机制（知识库管理岗位 kb_manager 的职责工具）。

把 vault 中流水线机制覆盖不到的部分（公司制度 / 素材 / 作品集 / 创意灵感 / 动态）
提炼成知识条目，写入 OPC/知识库/*.md（kb_graph 召回真源）并同步 owner 库 knowledge 表
（selfcheck 一致性口径：vault 条目数 == db kb_ 条目数）。

三才归位（sancai.py 口径）：
  地(di)=素材事实底座   天(tian)=纲领取势/外部分析   人(ren)=制度决策与人工产出

屏蔽口径与 server.py::_vault 完全一致，绝不内化：
  登录凭据.md、隐藏目录、运维手册/ 路径、文件名以 台账/凭据/拓扑 结尾的 md。

策略：
  - 单目录 md ≤ PER_DIR_FULL 篇 → 逐篇内化（摘要：标题+章节+首段）
  - 超过 → 按一级子目录聚合一条（文件数/类型分布/代表文件）
  - 条目 id = kb_vault_<sha1(relpath)[:10]>，重跑幂等（upsert 覆盖）
  - --cron 增量：只处理 mtime 晚于上次运行（ops/vault_digest_state.json）的文件

CLI：
  python3 vault_digest.py --full [--dry-run]     # 全量
  python3 vault_digest.py --cron [--quiet]       # 增量（crontab 用）
  python3 vault_digest.py --verify               # 召回自检
"""
import hashlib
import json
import os
import re
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
KB_DIR = Path(VAULT) / "OPC" / "知识库"
STATE = Path(BASE_DIR).parent / "ops" / "vault_digest_state.json"
TZ = timezone(timedelta(hours=8))

# 内化范围（白名单；OPC/ 下仅收「热点采集」——流水线归档与每日自动采集的产出，
# 其余 OPC 内容已有 trace 归档机制不重复处理；hermes_*/workbuddy_* 是系统副本跳过）
SCOPE = ["公司", "素材", "作品集", "创意灵感", "动态", "数字人口播形象",
         os.path.join("OPC", "热点采集")]
# 纯素材目录（图片/视频/音频，无 md）：按目录聚合内化登记，不遗漏小狗照片这类资产
ASSET_SCOPE = {
    "dogs": {"tags": ["狗大人", "犬种照片"], "sancai": "di"},
    "苟大人与狗茅品牌": {"tags": ["狗大人", "品牌KV"], "sancai": "di"},
    "数字人口播形象": {"tags": ["数字人", "口播形象"], "sancai": "di"},
    "videos": {"tags": ["视频素材"], "sancai": "di"},
    "背景音乐": {"tags": ["BGM", "音乐素材"], "sancai": "di"},
}
PER_DIR_FULL = 40          # 超过此数的目录改为按子目录聚合
DIGEST_CHARS = 220
AGGR_MEMBER_CAP = 20       # 聚合条目 members 最多列出的文件数
OM_ROOT = os.environ.get("OPC_OM_ROOT", "/opt/sefs/OpenMontage")
OM_PROJECT_TAGS = (        # OM 项目 → 中文项目标签（按 slug 前缀）
    (("tian-gou", "dog-park", "gou-"), "狗大人"),
    (("wenren-",), "文人莫克"),
)

# 屏蔽：与 server.py::_vault 同口径 + 模板/隐藏
def blocked(rel: str) -> bool:
    parts = rel.split(os.sep)
    if any(p.startswith(".") for p in parts):
        return True
    if rel == "公司/登录凭据.md":
        return True
    if "运维手册" in rel:
        return True
    if re.search(r"(台账|凭据|拓扑)\.md$", rel):
        return True
    name = parts[-1]
    return name.startswith("_") or name.startswith(".")

# 三才/五行归位规则：前缀目录 → (category, sancai)
def classify(rel: str):
    top = rel.split(os.sep)[0]
    sub = rel.split(os.sep)[1] if os.sep in rel else ""
    if top == "OPC" and sub == "热点采集":
        # 采集=外部情报归「天」；素材工单/回执是人工过程资产归「人」
        return ("analysis", "ren") if "素材" in rel else ("analysis", "tian")
    if top == "素材":
        return "analysis", "di"
    if top == "创意灵感":
        return "analysis", "tian"
    if top == "作品集":
        return "graph", "ren"
    if top == "动态":
        return "reflection", "ren"
    if top == "公司":
        if any(k in rel for k in ("事故与复盘", "技术变更", "技术备忘")):
            return "reflection", "ren"
        if any(k in sub for k in ("员工名册", "组织编制", "项目配置", "项目与调性",
                                  "内核与编排", "岗位职责", "管理层绩效")):
            return "decision", "ren"
        return "skill", "ren"
    if top in ("苟大人品牌", "数字人口播形象"):
        return "analysis", "tian"
    return "analysis", "di"


def _strip_fm(text: str):
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    return text[m.end():] if m else text, (m.group(0) if m else "")


def digest_one(path: Path, rel: str):
    """单文件 → 条目 dict（不落库）。摘要只取标题/章节/首段，不复制敏感正文细节。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return None
    body, _fm = _strip_fm(raw)
    title = ""
    m = re.search(r"^#\s+(.+)$", body, re.M)
    if m:
        title = m.group(1).strip()
    if not title:
        title = path.stem
    sections = [s.strip()[:40] for s in re.findall(r"^##\s+(.+)$", body, re.M)][:8]
    first = ""
    for para in body.split("\n"):
        p = para.strip()
        if p and not p.startswith("#") and not p.startswith(("-", ">", "|", "!", "[")):
            first = p
            break
    summary = f"{title}：{first[:DIGEST_CHARS]}" if first else title
    if sections:
        summary += f"｜章节：{'、'.join(sections)}"
    cat, san = classify(rel)
    return {
        "id": "kb_vault_" + hashlib.sha1(rel.encode()).hexdigest()[:10],
        "title": f"{title}（vault·{rel.split(os.sep)[0]}）"[:90],
        "path": rel,
        "category": cat, "sancai": san,
        "summary": summary,
        "sections": sections,
        "mtime": path.stat().st_mtime,
        "tags": ["vault内化", rel.split(os.sep)[0], {"di": "地", "tian": "天", "ren": "人"}[san]],
    }


def _file_group(name: str) -> str:
    """无子目录的大目录按文件名类型分组：剥日期前缀取首段（如「素材缺单回执」）；
    纯日期文件归入「每日热点简报」。"""
    base = re.sub(r"^20\d{2}-\d{2}-\d{2}-?", "", name)
    if base.endswith(".md"):
        base = base[:-3]
    if not base or re.fullmatch(r"20\d{2}-\d{2}-\d{2}", base):
        return "每日热点简报"
    return base.split("-")[0][:14]


def _om_project_tag(slug: str) -> str:
    for prefixes, tag in OM_PROJECT_TAGS:
        if slug.startswith(prefixes):
            return tag
    return ""


def collect_om():
    """OpenMontage 制作产物登记：每项目两条——素材产物（地）与方案资产（人）。

    素材（图/视频/音频）与分镜脚本（STORYBOARD/script.json/scene_plan.json）都是
    可复用资产：条目记产物统计、代表文件与绝对路径，agent 召回后按路径直接取用。
    """
    import json as _json
    root = Path(OM_ROOT) / "projects"
    if not root.is_dir():
        return []
    out = []
    for pj in sorted(root.glob("*/project.json")):
        slug = pj.parent.name
        try:
            meta = _json.loads(pj.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        ptag = _om_project_tag(slug) or str(meta.get("title") or slug)[:10]
        files = [p for p in pj.parent.rglob("*") if p.is_file()
                 and "history" not in p.parts]
        imgs = [p for p in files if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
        vids = [p for p in files if p.suffix.lower() in (".mp4", ".mov", ".webm")]
        auds = [p for p in files if p.suffix.lower() in (".wav", ".mp3", ".m4a", ".flac")]
        plans = [p for p in files if p.name in ("STORYBOARD.md", "script.json",
                                                "scene_plan.json", "research_brief.json",
                                                "proposal_packet.json") or "STORYBOARD" in p.name]
        rel = lambda p: str(p)                      # noqa: E731 OM 在 vault 外，保留绝对路径
        mt = max((p.stat().st_mtime for p in files), default=0)
        # ① 素材产物（地）
        if imgs or vids or auds:
            dist = {"图": len(imgs), "视频": len(vids), "音频": len(auds)}
            reprs = [rel(p) for p in (imgs[:3] + vids[:2] + auds[:2])]
            members = [rel(p) for p in (imgs[:8] + vids[:6] + auds[:6])]
            out.append({
                "id": "kb_vault_" + hashlib.sha1(f"om/{slug}/assets".encode()).hexdigest()[:10],
                "title": f"OM素材·{ptag}/{slug}（图{len(imgs)} 视频{len(vids)} 音频{len(auds)}）"[:90],
                "path": str(pj.parent) + "/",
                "category": "analysis", "sancai": "di",
                "summary": f"OpenMontage 项目 {slug}（{meta.get('title') or slug}）的制作产物登记，"
                           f"可复用：{json.dumps(dist, ensure_ascii=False)}；代表文件：{'、'.join(reprs)}；"
                           "复用方式：agent 按路径直接取用（跨项目复用图/音频时复制到新项目 assets/）",
                "sections": [], "mtime": mt,
                "tags": ["vault内化", "OpenMontage素材", "可复用"] + ([ptag] if ptag else []),
                "count": len(imgs) + len(vids) + len(auds), "members": members,
            })
        # ② 方案资产（人）：分镜/脚本/提案——同类选题可直接复用改写
        if plans:
            members = [rel(p) for p in plans[:AGGR_MEMBER_CAP]]
            out.append({
                "id": "kb_vault_" + hashlib.sha1(f"om/{slug}/plans".encode()).hexdigest()[:10],
                "title": f"OM方案·{ptag}/{slug}（分镜/脚本/提案 {len(plans)} 件）"[:90],
                "path": str(pj.parent) + "/artifacts/",
                "category": "analysis", "sancai": "ren",
                "summary": f"OpenMontage 项目 {slug} 的创作方案资产，可复用："
                           f"STORYBOARD 分镜、script.json 台词脚本、scene_plan 画面规格、"
                           f"research_brief/proposal_packet 调研与提案；代表文件："
                           f"{ '、'.join(rel(p) for p in plans[:5]) }；"
                           "同类选题可整段复用或改写（含逐镜时长/台词/画面规格）",
                "sections": [], "mtime": mt,
                "tags": ["vault内化", "OM方案", "分镜脚本", "可复用"] + ([ptag] if ptag else []),
                "count": len(plans), "members": members,
            })
    return out


def collect():
    """返回 (逐篇条目列表, 聚合条目列表)。大目录按子目录/文件类型聚合。"""
    singles, aggrs = [], []
    # 纯素材目录（无 md 的图片/视频/音频资产）：按目录聚合登记，素材不失联
    for top, meta in ASSET_SCOPE.items():
        root = Path(VAULT) / top
        if not root.is_dir():
            continue
        files = [p for p in root.rglob("*") if p.is_file() and not blocked(str(p.relative_to(VAULT)))]
        if not files:
            continue
        exts = {}
        for p in files:
            e = p.suffix.lstrip(".").lower() or "-"
            exts[e] = exts.get(e, 0) + 1
        reprs = [str(p.relative_to(VAULT)) for p in files[:6]]
        members = [str(p.relative_to(VAULT)) for p in files[:AGGR_MEMBER_CAP]]
        if len(files) > AGGR_MEMBER_CAP:
            members.append(f"…等 {len(files)} 个文件")
        aggrs.append({
            "id": "kb_vault_" + hashlib.sha1(f"asset/{top}".encode()).hexdigest()[:10],
            "title": f"素材登记·{top}（{len(files)} 个文件聚合）"[:90],
            "path": f"{top}/",
            "category": "analysis", "sancai": meta["sancai"],
            "summary": f"{top}/ 目录 {len(files)} 个素材文件的登记索引（图片/视频/音频资产，原文件为真源）；"
                       f"类型分布 {json.dumps(exts, ensure_ascii=False)}；代表文件：{'、'.join(reprs)}",
            "sections": [], "mtime": max(p.stat().st_mtime for p in files),
            "tags": ["vault内化"] + meta["tags"], "count": len(files), "members": members,
        })
    for top in SCOPE:
        root = Path(VAULT) / top
        if not root.is_dir():
            continue
        mds = [p for p in root.rglob("*.md") if not blocked(str(p.relative_to(VAULT)))]
        if len(mds) <= PER_DIR_FULL:
            for p in mds:
                it = digest_one(p, str(p.relative_to(VAULT)))
                if it:
                    singles.append(it)
            continue
        # 大目录：逐篇登记进聚合条目（不逐篇生成 md，防爆库）
        subs = {}
        for p in mds:
            rel = str(p.relative_to(VAULT))
            inner = rel[len(top) + 1:] if rel.startswith(top + os.sep) else rel
            parts = inner.split(os.sep)
            key = parts[0] if len(parts) > 1 else _file_group(parts[0])
            subs.setdefault(key, []).append((p, rel))
        for sub, rows in sorted(subs.items()):
            it = digest_one(rows[0][0], rows[0][1]) or {}
            exts = {}
            for p, rel in rows:
                exts[rel.split(os.sep)[-1].rsplit(".", 1)[-1] if "." in rel else "-"] = \
                    exts.get(rel.split(os.sep)[-1].rsplit(".", 1)[-1] if "." in rel else "-", 0) + 1
            reprs = [r[1].replace(top + os.sep, "") for r in rows[:6]]
            members = [r[1] for r in rows[:AGGR_MEMBER_CAP]]
            if len(rows) > AGGR_MEMBER_CAP:
                members.append(f"…等 {len(rows)} 篇")
            aggrs.append({
                "id": "kb_vault_" + hashlib.sha1(f"{top}/{sub}".encode()).hexdigest()[:10],
                "title": f"内化·{top}/{sub}（{len(rows)} 篇聚合）"[:90],
                "path": f"{top}/{sub}/",
                "category": classify(f"{top}/{sub}")[0],
                "sancai": classify(f"{top}/{sub}")[1],
                "summary": f"{top}/{sub} 下 {len(rows)} 篇 md 的聚合索引；类型分布 "
                           f"{json.dumps(exts, ensure_ascii=False)}；代表文件：{'、'.join(reprs)}",
                "sections": [], "mtime": max(r[0].stat().st_mtime for r in rows),
                "tags": ["vault内化", top.split(os.sep)[-1], "聚合索引"],
                "count": len(rows), "members": members,
            })
    return singles, aggrs + collect_om()


def write_entry(k, dry=False):
    """vault md（kb_graph 真源）+ owner db 双写。返回 md 路径。"""
    now = datetime.now(TZ).isoformat(timespec="seconds")
    date = now[:10]
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", k["title"])[:60].strip("-")
    md = KB_DIR / f"{date}-{safe}-{k['id']}.md"
    lines = [f"---", f"kb_id: {k['id']}", f"title: {k['title']}",
             "project: 全公司", f"category: {k['category']}", f"sancai: {k['sancai']}",
             f"tags: {'、'.join(k['tags'])}", "trace:", f"archived: {now}", "---", "",
             f"> 出处：`{k['path']}`（vault 内化条目，原文件为真源，本条是检索摘要）", ""]
    lines += [k["summary"], ""]
    if k.get("sections"):
        lines += ["## 章节", *[f"- {s}" for s in k["sections"]], ""]
    if k.get("members"):
        lines += ["## 包含文件", *[f"- `{m}`" for m in k["members"]], ""]
    lines += [f"## 三才归位", f"- category={k['category']}｜sancai={k['sancai']}"
              f"（{'地=事实底座' if k['sancai']=='di' else '天=纲领取势' if k['sancai']=='tian' else '人=制度与产出'}）", "",
              f"- 标签：{'、'.join(k['tags'])}", ""]
    if not dry:
        for old in KB_DIR.glob(f"*{k['id']}.md"):   # 幂等：同 id 旧文件（如标题调整后的残留）先清
            if old != md:
                old.unlink()
        md.write_text("\n".join(lines), encoding="utf-8")
        con = sqlite3.connect(OWNER_DB)
        con.execute("""INSERT INTO knowledge(id,title,path,tags,updated_at,raw,category,
                       sancai,summary,project_id,trace_id,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET title=excluded.title,path=excluded.path,
                         tags=excluded.tags,updated_at=excluded.updated_at,raw=excluded.raw,
                         category=excluded.category,sancai=excluded.sancai,
                         summary=excluded.summary,project_id=excluded.project_id,
                         trace_id=excluded.trace_id,body=excluded.body""",
                    (k["id"], k["title"], k["path"],
                     json.dumps(k["tags"], ensure_ascii=False), now,
                     json.dumps(k, ensure_ascii=False), k["category"], k["sancai"],
                     k["summary"], "", "", "\n".join(lines)[:4000]))
        con.commit()
        con.close()
    return md


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8")).get("since", 0)
    except Exception:
        return 0


def save_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"since": time.time(),
                                 "at": datetime.now(TZ).isoformat(timespec="seconds")},
                                ensure_ascii=False), encoding="utf-8")


def run(full=False, dry=False, quiet=False):
    singles, aggrs = collect()
    since = 0 if full else load_state()
    todo_s = [k for k in singles if k["mtime"] > since]
    todo_a = [k for k in aggrs if k["mtime"] > since]
    if not todo_s and not todo_a:
        if not quiet:
            print(f"[vault-digest] 无增量（上次 {datetime.fromtimestamp(since, TZ):%m-%d %H:%M}）")
        return 0
    n = 0
    for k in todo_s + todo_a:
        write_entry(k, dry)
        n += 1
        if not quiet:
            print(f"  + {k['id']} {k['title'][:50]} [{k['category']}/{k['sancai']}]")
    if not dry:
        save_state()
    print(f"[vault-digest] 内化 {n} 条（逐篇 {len(todo_s)} / 聚合 {len(todo_a)}）"
          f"{' [dry-run]' if dry else ''}", flush=True)
    return n


def verify():
    """召回自检：三条代表性查询必须能命中内化条目。"""
    sys.path.insert(0, BASE_DIR)
    import kb_graph
    ok = True
    for q, want in (("员工名册 项目配置", "kb_vault_"),
                    ("素材 沉默的分母", "kb_vault_"),
                    ("作品集 狗大人", "kb_vault_")):
        hits = kb_graph.brief(q) if hasattr(kb_graph, "brief") else ""
        hit = want in str(hits)
        ok = ok and hit
        print(f"  {'PASS' if hit else 'MISS'} 「{q}」")
    print("[vault-digest] 召回自检", "通过" if ok else "未通过", flush=True)
    return ok


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--verify" in args:
        sys.exit(0 if verify() else 1)
    run(full="--full" in args, dry="--dry-run" in args, quiet="--quiet" in args)
