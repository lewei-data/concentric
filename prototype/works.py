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
    "wenren": "文人莫克",
    "gou": "狗大人",
    "tongxinyuan": "同心圆",
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

STATUS_MAP = {
    "topic": ("制作中", "doing"),
    "copy": ("制作中", "doing"),
    "video": ("制作中", "doing"),
    "review": ("待评审", "review"),
    "approved": ("待发布", "approved"),
    "published": ("已发布", "published"),
    "archived": ("已归档", "archived"),
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


def _build(folder, trace, sub, approvals=None):
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


def scan(approvals=None):
    """扫描全部作品，按更新时间倒序返回。"""
    items = []
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


def page(query_params, approvals=None):
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

    items = scan(approvals)
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


def detail(work_id, approvals=None):
    """GET /api/works/<id>：档案主记录 + markdown 原文，供作品档案详情页展示。"""
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
