#!/usr/bin/env python3
"""作品档案：把 Obsidian 交付物目录统一成「名称 + 时间」可管理的资产记录。

真源仍是磁盘：<vault>/OPC/交付物/<trace>/<pid>-<cid>/
本模块只做只读归一，不复制、不改写作品文件：
  - 编号：W-<trace 短码>-<项目码>-<内容短码>，用于对外引用与检索
  - 名称：07-data.json 的 title（缺失时回退到 01-topic.md 的 H1）
  - 时间：createdAt 取目录内最早文件 mtime，updatedAt 取 07-data.json mtime
  - 管理资料：阶段 / 状态 / 负责人 / 项目 / 文件清单 / 评审结论 / 评估结论
  - 处理流程：按各阶段文件 mtime 还原时间线（选题 → 文案 → 分镜 → 封面 → 样片 → 评估 → 评审）
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

VAULT = os.environ.get("OPC_VAULT", "/opt/sefs/obsidian")
ROOT = os.path.join(VAULT, "OPC", "交付物")
URL_PREFIX = "/vault/OPC/交付物"
TZ = timezone(timedelta(hours=8))

PROJECT_NAMES = {
    "wenren": "产品一组",
    "gou": "产品二组",
    "tongxinyuan": "产品三组",
    "all": "全公司",
}
PROJECT_CODES = {"wenren": "WR", "gou": "GD", "tongxinyuan": "TXY", "all": "ALL"}

# 处理流程按此顺序展示；文件不存在时保留占位，方便看出缺哪一环
STEPS = [
    ("topic", "01-topic.md", "选题"),
    ("copy", "02-copy.md", "文案"),
    ("storyboard", "03-storyboard.md", "分镜剧本"),
    ("cover", "04-cover.png", "封面"),
    ("preview", "05-preview.mp4", "预览样片"),
    ("final", "05-final.mp4", "最终成片"),
    ("assessment", "08-assessment.md", "制作评估"),
    ("review", "09-review.md", "评审记录"),
    ("data", "07-data.json", "制作数据"),
    ("illustrations", "10-illustrations.json", "插图配置"),
]

STAGE_LABELS = {
    "topic": "选题",
    "copy": "文案",
    "video": "视频",
    "review": "待评审",
    "approved": "待发布",
    "published": "已发布",
    "archived": "已归档",
}

# 车间工位 → 阶段成果文件（制片工厂工位详情内嵌展示用）：
# 交付目录里存在哪个就展示哪个；md 附带正文，图片/视频走 /vault 只读发布
STAGE_ARTIFACTS = {
    "topic": [("01-topic.md", "选题文稿", "md")],
    "assessment": [("08-assessment.md", "制作评估", "md")],
    "copy": [("02-copy.md", "文案成稿", "md")],
    "sample": [("03-storyboard.md", "分镜剧本", "md")],
    "render": [("05-preview.mp4", "预览样片", "video")],
    "sample_review": [("05-preview.mp4", "预览样片", "video")],
    "final": [("05-final.mp4", "最终成片", "video"), ("04-cover.png", "封面图", "image")],
    "review": [("09-review.md", "评审记录", "md")],
}

STATUS_MAP = {
    "topic": ("制作中", "doing"),
    "copy": ("制作中", "doing"),
    "video": ("制作中", "doing"),
    "review": ("待评审", "review"),
    "approved": ("待发布", "approved"),
    "published": ("已发布", "published"),
    "archived": ("已归档", "archived"),
}

# 工厂车间生命周期状态（与制片工厂页一致）：出厂=质检审核通过
FACTORY_STATUS = {
    "shipped": ("已出厂", "shipped"),
    "doing": ("生产中", "doing"),
    "completed": ("已完工", "completed"),
    "interrupted": ("已中断", "interrupted"),
}


def _factory_view(f, step_status=None):
    """工厂车间记录（product_crafts）→ 档案的工厂段字段，状态随工厂生命周期。"""
    shipped = bool(f.get("shippedAt"))
    run = str(f.get("runStatus") or "")
    if shipped:
        status, key = FACTORY_STATUS["shipped"]
    elif run == "running":
        status, key = FACTORY_STATUS["doing"]
    elif run == "completed":
        status, key = FACTORY_STATUS["completed"]
    elif run:
        status, key = FACTORY_STATUS["interrupted"]
    else:
        status, key = FACTORY_STATUS["doing"]
    steps = []
    for st in f.get("steps") or []:
        steps.append({**st, "status": (step_status or {}).get(st.get("opcStage")) or "pending"})
    done = sum(1 for s in steps if s["status"] in ("done", "stopped", "failed"))
    return {
        "serialNo": f.get("serialNo") or "",
        "workshopNo": f.get("workshopNo") or "",
        "craftId": f.get("craftId") or "",
        "craftName": f.get("craftName") or "",
        "decidedAt": f.get("decidedAt") or "",
        "shippedAt": f.get("shippedAt") or "",
        "craftSteps": steps,
        "craftProgress": f"{done}/{len(steps)}" if steps else "",
        "factoryStatus": status,
        "factoryStatusKey": key,
    }


def _folder_for(trace, pid, item_id):
    """按 (trace, 内容单 id) 找交付目录：命名约定 <pid>-<item_id>，后缀匹配兜底。"""
    tdir = os.path.join(ROOT, str(trace or ""))
    if not item_id or not os.path.isdir(tdir):
        return None, ""
    want = f"{pid}-{item_id}"
    if os.path.isdir(os.path.join(tdir, want)):
        return os.path.join(tdir, want), want
    try:
        for sub in sorted(os.listdir(tdir)):
            if sub.endswith(f"-{item_id}") and os.path.isdir(os.path.join(tdir, sub)):
                return os.path.join(tdir, sub), sub
    except OSError:
        pass
    return None, ""


def _factory_record(trace, f, fv):
    """有工厂记录但尚未落盘交付目录的作品：以工厂字段为准的占位档案。"""
    status, status_key = fv["factoryStatus"], fv["factoryStatusKey"]
    body = {k: v for k, v in fv.items() if k not in ("factoryStatus", "factoryStatusKey")}
    created = f.get("decidedAt") or ""
    return {
        "id": _work_id(trace, f.get("itemId") or ""),
        "code": f.get("serialNo") or _code(trace, f.get("projectId"), f.get("itemId")),
        "title": f.get("title") or "",
        "projectId": f.get("projectId") or "",
        "projectName": _project(f.get("projectId")),
        "traceId": trace,
        "contentId": f.get("itemId") or "",
        "stage": "factory",
        "stageName": fv.get("craftName") or "工厂",
        "status": status,
        "statusKey": status_key,
        "owner": "", "toneProfile": "",
        "createdAt": created,
        "updatedAt": f.get("shippedAt") or created,
        "folder": "", "files": [], "assetCount": 0, "completeness": 0,
        "reviewNote": "", "reviewPass": None, "assessment": "",
        "render": {"ok": False, "video": "", "reason": ""},
        "renderCheck": {"pass": False, "reasons": []},
        "hasFinal": False, "hasPreview": False,
        "approvals": [], "timeline": [],
        **body,
    }


def _iso(ts):
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds") if ts else ""


def _read(path, limit=200000):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(limit)
    except (OSError, UnicodeDecodeError):
        return ""


def _section(text, title):
    """取 markdown 二级标题下的正文，直到下一个二级标题。"""
    m = re.search(r"^##\s*" + re.escape(title) + r"\s*$(.*?)(?=^##\s|\Z)", text,
                  re.M | re.S)
    return m.group(1).strip() if m else ""


def _line(text, label):
    m = re.search(r"^-\s*" + re.escape(label) + r"\s*[：:]\s*(.+?)\s*$", text, re.M)
    return m.group(1).strip() if m else ""


def _title_from_topic(text):
    m = re.search(r"^#\s*(?:选题\s*·\s*)?(.+?)\s*$", text, re.M)
    return m.group(1).strip() if m else ""


def _project(pid):
    return PROJECT_NAMES.get(str(pid or ""), str(pid or "全公司"))


def _code(trace, pid, cid):
    return "W-{}-{}-{}".format(
        str(trace or "").replace("tr_", "").upper()[:8] or "0",
        PROJECT_CODES.get(str(pid or ""), "XX"),
        str(cid or "").split("_")[-1].upper()[:8] or "0",
    )


def _work_id(trace, sub):
    return f"{trace}__{sub}"


def _file_entry(folder, key, name, label, trace, sub):
    path = os.path.join(folder, name)
    if not os.path.isfile(path):
        return {"key": key, "label": label, "exists": False, "size": 0,
                "at": "", "url": ""}
    try:
        st = os.stat(path)
    except OSError:
        return {"key": key, "label": label, "exists": False, "size": 0,
                "at": "", "url": ""}
    return {
        "key": key, "label": label, "exists": True, "size": st.st_size,
        "at": _iso(st.st_mtime),
        "url": f"{URL_PREFIX}/{trace}/{sub}/{name}",
    }


def _approvals_for(trace, approvals):
    out = []
    for a in approvals or []:
        if str(a.get("traceId") or "") != str(trace or ""):
            continue
        out.append({
            "id": a.get("id"),
            "action": a.get("action"),
            "risk": a.get("risk"),
            "status": a.get("status"),
            "approver": a.get("approver"),
            "createdAt": a.get("createdAt"),
            "decidedAt": a.get("decidedAt"),
            "title": a.get("title"),
        })
    return out


def _build(folder, trace, sub, approvals=None, factory=None, step_status=None):
    data_path = os.path.join(folder, "07-data.json")
    data = {}
    if os.path.isfile(data_path):
        try:
            with open(data_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    files = []
    for key, name, label in STEPS:
        files.append(_file_entry(folder, key, name, label, trace, sub))
    asset_dir = os.path.join(folder, "assets")
    asset_count = 0
    if os.path.isdir(asset_dir):
        try:
            asset_count = sum(1 for n in os.listdir(asset_dir)
                              if os.path.isfile(os.path.join(asset_dir, n)))
        except OSError:
            asset_count = 0
    if not asset_count and isinstance(data.get("assets"), list):
        asset_count = len(data["assets"])

    topic_text = _read(os.path.join(folder, "01-topic.md"))
    review_text = _read(os.path.join(folder, "09-review.md"))
    assess_text = _read(os.path.join(folder, "08-assessment.md"))

    title = str(data.get("title") or "").strip() or _title_from_topic(topic_text) \
        or sub.replace("-", " · ")
    pid = str(data.get("projectId") or (sub.split("-")[0] if "-" in sub else "") or "")
    stage = str(data.get("stage") or "")
    status, status_key = STATUS_MAP.get(stage, (STAGE_LABELS.get(stage, "制作中"), "doing"))

    stamps = [datetime.fromisoformat(f["at"]).timestamp() for f in files if f["exists"]]
    created = min(stamps) if stamps else (os.path.getmtime(folder) if os.path.isdir(folder) else 0)
    updated = 0
    try:
        updated = os.path.getmtime(data_path if os.path.isfile(data_path) else folder)
    except OSError:
        updated = created

    timeline = [{"key": f["key"], "label": f["label"], "at": f["at"],
                 "exists": f["exists"]} for f in files]
    timeline = [t for t in timeline if t["exists"]]
    timeline.sort(key=lambda t: t["at"])

    core_keys = {"topic", "copy", "storyboard", "cover", "assessment", "review", "data"}
    have = {f["key"] for f in files if f["exists"]}
    completeness = round(len(core_keys & have) / len(core_keys) * 100)

    review_note = str(data.get("reviewNote") or "").strip() or _line(review_text, "终审意见")
    review_pass = None
    if review_text:
        if re.search(r"未通过|驳回", review_text):
            review_pass = False
        elif "通过" in review_text:
            review_pass = True
    assessment = str((data.get("assessment") or {}).get("summary") or "").strip() \
        or _section(assess_text, "评估结论")
    render = data.get("render") or {}
    render_check = data.get("renderCheck") or {}

    return {
        "id": _work_id(trace, sub),
        "code": _code(trace, pid, data.get("id") or sub.split("-")[-1]),
        "title": title,
        "projectId": pid,
        "projectName": _project(pid),
        "traceId": trace,
        "contentId": data.get("id") or sub.split("-")[-1],
        "stage": stage,
        "stageName": STAGE_LABELS.get(stage, stage or "制作中"),
        "status": status,
        "statusKey": status_key,
        "owner": str(data.get("owner") or "").strip(),
        "toneProfile": str(data.get("toneProfile") or "").strip(),
        "createdAt": _iso(created),
        "updatedAt": _iso(updated),
        "folder": f"{URL_PREFIX}/{trace}/{sub}",
        "files": files,
        "assetCount": asset_count,
        "completeness": completeness,
        "reviewNote": review_note[:500],
        "reviewPass": review_pass,
        "assessment": assessment[:800],
        "render": {"ok": bool(render.get("ok")),
                   "video": render.get("video") or "",
                   "reason": render.get("reason") or ""},
        "renderCheck": {"pass": bool(render_check.get("pass")),
                        "reasons": (render_check.get("reasons") or [])[:6]},
        "hasFinal": "final" in have,
        "hasPreview": "preview" in have,
        "approvals": _approvals_for(trace, approvals),
        "timeline": timeline,
    }
    # 工厂车间数据为准：注入流水号/车间/工艺/工序进度，阶段状态随工厂生命周期
    if factory:
        fv = _factory_view(factory, step_status)
        rec.update(fv)
        rec["stage"] = "factory"
        rec["stageName"] = fv["craftName"] or rec["stageName"]
        rec["status"] = fv["factoryStatus"]
        rec["statusKey"] = fv["factoryStatusKey"]
        if fv["serialNo"]:
            rec["code"] = fv["serialNo"]
        if fv["shippedAt"]:
            rec["updatedAt"] = fv["shippedAt"]
    return rec


def _iter_folders():
    try:
        traces = sorted(d for d in os.listdir(ROOT) if d.startswith("tr_"))
    except OSError:
        return
    for trace in traces:
        tdir = os.path.join(ROOT, trace)
        if not os.path.isdir(tdir):
            continue
        try:
            subs = sorted(os.listdir(tdir))
        except OSError:
            continue
        for sub in subs:
            folder = os.path.join(tdir, sub)
            if not os.path.isdir(folder) or sub.startswith("."):
                continue
            yield folder, trace, sub


def scan(approvals=None, factory=None, step_status=None):
    """扫描作品，按出厂/更新时间倒序返回。

    工厂过程库启用时以工厂为准：只列 product_crafts 有流水号的作品，
    阶段/状态随工厂生命周期（进厂→工序→质检出厂）；无工厂记录的历史交付
    不再进档案（历史清掉，以最新为准）。factory=None 表示过程库未启用，
    回退旧的磁盘扫描行为。
    """
    items = []
    if factory is not None:
        for (trace, item_id), f in factory.items():
            fv = _factory_view(f, step_status)
            folder, sub = _folder_for(trace, f.get("projectId"), item_id)
            try:
                if folder:
                    items.append(_build(folder, trace, sub, approvals, f, step_status))
                else:
                    items.append(_factory_record(trace, f, fv))
            except Exception:
                continue
        items.sort(key=lambda x: str(x.get("shippedAt") or x.get("updatedAt") or ""),
                   reverse=True)
        return items
    for folder, trace, sub in _iter_folders():
        try:
            items.append(_build(folder, trace, sub, approvals))
        except Exception:
            continue
    items.sort(key=lambda x: str(x.get("updatedAt") or ""), reverse=True)
    return items


def _facets(items):
    by_status, by_project = {}, {}
    for it in items:
        by_status[it["status"]] = by_status.get(it["status"], 0) + 1
        by_project[it["projectName"]] = by_project.get(it["projectName"], 0) + 1
    return {
        "total": len(items),
        "byStatus": by_status,
        "byProject": by_project,
        "withFinal": sum(1 for it in items if it["hasFinal"]),
        "withReview": sum(1 for it in items if it["reviewNote"]),
    }


def page(query_params, approvals=None, factory=None, step_status=None):
    """GET /api/works：分页 + 关键词 + 项目 / 阶段 / 状态过滤。"""
    def one(key, default=""):
        vals = query_params.get(key) or [default]
        return (vals[0] or default).strip()

    try:
        page_no = max(1, int(one("page", "1")))
    except ValueError:
        page_no = 1
    try:
        size = min(100, max(1, int(one("size", "20"))))
    except ValueError:
        size = 20
    q = one("q").lower()
    project = one("project")
    stage = one("stage")
    status = one("status")
    sort = one("sort", "updated")

    items = scan(approvals, factory, step_status)
    facets = _facets(items)
    if q:
        items = [it for it in items if q in " ".join([
            it.get("title") or "", it.get("code") or "", it.get("owner") or "",
            it.get("projectName") or "", it.get("traceId") or "",
            it.get("contentId") or "", it.get("status") or "",
        ]).lower()]
    if project:
        items = [it for it in items if it.get("projectId") == project]
    if stage:
        items = [it for it in items if it.get("stage") == stage]
    if status:
        items = [it for it in items if it.get("status") == status]
    if sort == "name":
        items.sort(key=lambda it: str(it.get("title") or ""))
    elif sort == "created":
        items.sort(key=lambda it: str(it.get("createdAt") or ""), reverse=True)

    total = len(items)
    start = (page_no - 1) * size
    rows = items[start:start + size]
    return {"ok": True, "total": total, "page": page_no, "size": size,
            "items": rows, "facets": facets}


def detail(work_id, approvals=None, factory=None, step_status=None):
    """GET /api/works/<id>：档案主记录 + markdown 原文，供作品档案详情页展示。"""
    if factory is not None:
        for (trace, item_id), f in factory.items():
            fv = _factory_view(f, step_status)
            folder, sub = _folder_for(trace, f.get("projectId"), item_id)
            # 列表 id 用的是目录名（sub），无目录时回退 itemId，两种都需匹配
            if _work_id(trace, sub or item_id) != work_id:
                continue
            if not folder:
                return _factory_record(trace, f, fv)
            rec = _build(folder, trace, sub, approvals, f, step_status)
            docs = {}
            for key, name, label in STEPS:
                if not name.endswith(".md"):
                    continue
                text = _read(os.path.join(folder, name))
                if text:
                    docs[key] = {"label": label, "text": text}
            rec["docs"] = docs
            return rec
        return None
    for folder, trace, sub in _iter_folders():
        if _work_id(trace, sub) != work_id:
            continue
        rec = _build(folder, trace, sub, approvals)
        docs = {}
        for key, name, label in STEPS:
            if not name.endswith(".md"):
                continue
            text = _read(os.path.join(folder, name))
            if text:
                docs[key] = {"label": label, "text": text}
        rec["docs"] = docs
        return rec
    return None


def _manifest_restore(folder, trace, sub, stage_id):
    """md 交付文件没落盘时，从 _render/manifest.json 还原该环节的文本成果。

    copy=各镜台词成稿；sample=分镜表（镜号/类型/时间轴/台词摘要）。
    """
    path = os.path.join(folder, "_render", "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError):
        return None
    scenes = man.get("scenes") or []
    if not scenes:
        return None
    if stage_id == "copy":
        lines = [str(s.get("line") or "").strip() for s in scenes if s.get("line")]
        if not lines:
            return None
        text = "\n\n".join(f"**{i + 1}.** {ln}" for i, ln in enumerate(lines))
        label = "文案成稿（按渲染记录还原）"
    elif stage_id == "sample":
        rows = ["| 镜号 | 类型 | 起 | 止 | 台词摘要 |", "| --- | --- | --- | --- | --- |"]
        for i, s in enumerate(scenes):
            tin = float(s.get("in_seconds") or 0)
            tout = float(s.get("out_seconds") or 0)
            line = (str(s.get("line") or "").strip()[:40] or "-").replace("|", "／")
            rows.append(f"| {i + 1} | {s.get('type') or '-'} | {tin:.1f}s | {tout:.1f}s | {line} |")
        text = "\n".join(rows)
        label = "分镜剧本（按渲染记录还原）"
    else:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"name": "_render/manifest.json", "label": label, "kind": "md",
            "size": st.st_size, "at": _iso(st.st_mtime),
            "url": f"{URL_PREFIX}/{trace}/{sub}/_render/manifest.json",
            "text": text}


def step_artifacts(trace, stage_id, factory):
    """车间工位阶段成果：交付目录里该环节对应的文件（图片/视频/md 正文）。

    同一 trace 可能有多件产品（不同内容单），逐件查目录后合并；
    md 类成果没落盘时回退到 _render/manifest.json 还原文本，保证工位能看到阶段产出。
    """
    out = []
    for (t, item_id), f in (factory or {}).items():
        if t != trace:
            continue
        folder, sub = _folder_for(t, f.get("projectId"), item_id)
        if not folder:
            continue
        for name, label, kind in STAGE_ARTIFACTS.get(stage_id, []):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                if kind == "md":
                    restored = _manifest_restore(folder, t, sub, stage_id)
                    if restored:
                        out.append(restored)
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            entry = {"name": name, "label": label, "kind": kind,
                     "size": st.st_size, "at": _iso(st.st_mtime),
                     "url": f"{URL_PREFIX}/{t}/{sub}/{name}"}
            if kind == "md":
                entry["text"] = _read(path, 60000)
            out.append(entry)
    return out
