#!/usr/bin/env python3
"""CCO「创意灵感」聚合：把散落在热点简报、视频拆解、知识库与评审会里的
可用角度收敛成一份只读清单，供新版工作台 /app/inspiration 展示。

数据源（全部真实文件 / 内核快照，不造数据）：
  1. 热点分析：`KERNEL.hotspot_page()` 的热点线索 + Obsidian `OPC/热点采集/*热点简报*.md`
     的「分项目可用角度」表；
  2. 视频分析：Obsidian `素材/**` 下的拆解、给 CCO 的一页纸、分镜文案，以及仓库根目录
     的「同心圆-*」创意说明（按章节切成可复用要点）；
  3. 蒸馏启发：知识库归档条目的「一句话结论 / 可复用点」+ 选题评审会结论里的定稿清单。

只读、带 60s 缓存；`?refresh=1` 可强制重建。
"""

import hashlib
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(BASE_DIR, ".."))
VAULT = os.environ.get("OPC_VAULT", "/opt/sefs/obsidian")
CACHE_TTL = 60
_CACHE = {"at": {}, "data": {}}   # vault → 缓存（多用户隔离：每个 vault 一份）
_CACHE_LOCK = threading.Lock()

CAT_HOTSPOT = "热点分析"
CAT_VIDEO = "视频分析"
CAT_DISTILL = "蒸馏启发"

# 视频拆解文档：只挑真正含分析结论的文件，避免把素材说明/依赖树扫进来
_VIDEO_NAMES = ("拆解", "给CCO", "汇总", "文案与分镜", "节奏")
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", "remotion", ".venv"}
_SECTION_RE = re.compile(r"^(?:#{1,3}\s+(.+?)|【([^】]+)】)\s*$")
_BRIEF_HEAD_RE = re.compile(r"^#{2,3}\s*[^\n]*分项目可用角度[^\n]*$", re.M)
# 只保留真正给出「可复用结论」的章节，避免把脚本台词、发布参数也当灵感
_USEFUL_RE = re.compile(
    r"判断|结论|机制|公式|模板|复用|迁移|启发|蒸馏|为什么|拆解|分析|"
    r"要点|选题|创意|评估|淘汰|备选|置信|可信|结构|节奏|定位|角度|盘点|"
    r"评审|口径|规律|方法|能立住|一句话")


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _file_time(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), TZ).isoformat(timespec="seconds")
    except OSError:
        return ""


def _id(prefix: str, *parts) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return f"{prefix}_{hashlib.md5(raw.encode('utf-8')).hexdigest()[:10]}"


def _read(path: str, limit: int = 30000) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(limit)
    except (OSError, UnicodeDecodeError):
        return ""


def _clean(text, limit: int = 0) -> str:
    s = re.sub(r"[`*_>#]+", "", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] if limit else s


def _vault_link(rel: str) -> str:
    return "/vault/" + quote(rel.replace(os.sep, "/"), safe="/")


def _item(category, sub_kind, title, summary, detail, tags, source, link,
          at="", score=None, origin=""):
    return {
        "id": _id("idea", category, sub_kind, title, source),
        "category": category,
        "subKind": sub_kind,
        "title": _clean(title, 110) or "未命名灵感",
        "summary": _clean(summary, 220),
        "detail": str(detail or "").strip()[:620],
        "tags": [str(t) for t in (tags or []) if str(t).strip()][:12],
        "source": source,
        "link": link,
        "at": at,
        "score": score,
        "origin": origin,
    }


def _hotspot_ideas(kernel, vault):
    items = []
    # 1) 运行时热点线索（内存快照 + Obsidian 热点简报恢复）
    try:
        rows = kernel.hotspot_page("page=1&size=100").get("items") or []
    except Exception:
        rows = []
    for h in rows[:40]:
        title = h.get("title") or ""
        if not title:
            continue
        tags = list(h.get("tags") or [])
        detail = "\n".join([
            f"热点：{title}",
            f"来源：{h.get('source') or '热点采集'}",
            f"标签：{'、'.join(tags) or '—'}",
            f"适配度：{h.get('fit') or '—'}",
            f"traceId：{h.get('traceId') or '—'}",
        ])
        items.append(_item(
            CAT_HOTSPOT, "热点线索", title,
            f"来自{h.get('source') or '热点采集'}，热度 {h.get('score', '—')}",
            detail, tags, f"热点采集 · {h.get('source') or '运行时快照'}",
            "/app/hotspots", h.get("at") or "", h.get("score"), "KERNEL.hotspot_page()"))

    # 2) 简报里的「分项目可用角度」表：这才是 CCO 真正能下手的选题角度
    brief_dir = os.path.join(vault, "OPC", "热点采集")
    brief_count = 0
    try:
        names = sorted(os.listdir(brief_dir))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".md") or "热点简报" not in name:
            continue
        path = os.path.join(brief_dir, name)
        text = _read(path)
        m = _BRIEF_HEAD_RE.search(text)
        if not m:
            continue
        tail = text[m.end():]
        nxt = re.search(r"^#{2,3}\s", tail, re.M)
        block = tail[:nxt.start()] if nxt else tail
        rel = os.path.join("OPC", "热点采集", name)
        rows = []
        for line in block.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 4 or cells[0] in ("项目", "") or set(cells[0]) <= {"-", ":"}:
                continue
            if re.fullmatch(r"[-:\s]+", cells[1] or ""):
                continue
            rows.append(cells)
        if not rows:
            continue
        brief_count += 1
        fit_score = {"高": 0.9, "中高": 0.8, "中": 0.6, "低": 0.35}
        for cells in rows[:8]:
            project, fit, angle, risk = cells[0], cells[1], cells[2], cells[3]
            detail = (f"项目：{project}\n适配度：{fit}\n可做的角度：{angle}\n"
                      f"风险与红线：{risk}\n简报：{name}")
            items.append(_item(
                CAT_HOTSPOT, "简报角度", f"{project}｜{angle}", angle, detail,
                [project, fit, "热点简报"], f"热点简报 · {name}", _vault_link(rel),
                _file_time(path), fit_score.get(fit, 0.6), "Obsidian/OPC/热点采集"))
    return items, brief_count


def _sections(text):
    """按 Markdown 标题或【n. 小节】切段，返回 (标题, 正文) 列表。"""
    out, title, buf = [], "", []

    def flush():
        body = "\n".join(buf).strip()
        if title and len(body) >= 40:
            out.append((title, body))

    for line in text.splitlines():
        m = _SECTION_RE.match(line.strip())
        if m:
            flush()
            title = _clean(m.group(1) or m.group(2), 90)
            buf = []
        else:
            buf.append(line)
    flush()
    return out


def _video_ideas(vault, include_repo_docs=True):
    items = []
    docs = []
    video_dir = os.path.join(vault, "素材")
    if os.path.isdir(video_dir):
        for dirpath, dirnames, filenames in os.walk(video_dir):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                if name.endswith(".md") and any(k in name for k in _VIDEO_NAMES):
                    docs.append((os.path.join(dirpath, name), True))
    # 仓库根目录的 CCO 创意说明（首发创意说明、得令风格三方案、方案一视频脚本）
    # 属于共享产品样例：仅存量公司空间（owner）读取，注册用户工作区不混入
    if include_repo_docs:
        try:
            for name in os.listdir(REPO_ROOT):
                if name.startswith("同心圆-") and name.endswith(".md"):
                    docs.append((os.path.join(REPO_ROOT, name), False))
        except OSError:
            pass

    for path, in_vault in docs:
        rel = (os.path.relpath(path, vault) if in_vault
               else os.path.relpath(path, REPO_ROOT))
        link = _vault_link(rel) if in_vault else ""
        stem = os.path.splitext(os.path.basename(path))[0]
        tags = ["视频拆解" if in_vault else "创意说明", stem]
        noise = {"opt", "sefs", "home", "boray", "opc-workbench", "素材"}
        for part in os.path.dirname(path).split(os.sep)[-2:]:
            if part and part not in _SKIP_DIRS and part not in noise:
                tags.append(part)
        sections = _sections(_read(path))
        useful = [s for s in sections if _USEFUL_RE.search(s[0]) or _USEFUL_RE.search(s[1][:200])]
        chosen = (useful or sections)[:5]
        for sec_title, body in chosen:
            summary = _clean(body.split("\n\n")[0], 180) or _clean(body, 180)
            items.append(_item(
                CAT_VIDEO, "拆解要点" if in_vault else "创意蒸馏", sec_title,
                summary, body, tags[:8],
                ("Obsidian/素材" if in_vault else "仓库文档") + f" · {os.path.basename(path)}",
                link, _file_time(path), None,
                "Obsidian/素材" if in_vault else "REPO_ROOT"))
    return items, len(docs)


def _distill_ideas(kernel, vault):
    items = []
    kb_dir = os.path.join(vault, "OPC", "知识库")
    # kb_id → md 相对路径，给「查看原文」用
    kb_paths = {}
    try:
        for name in os.listdir(kb_dir):
            if not name.endswith(".md") or name.startswith("00-"):
                continue
            text = _read(os.path.join(kb_dir, name), 1200)
            m = re.search(r"^kb_id:\s*(\S+)", text, re.M)
            if m:
                kb_paths[m.group(1)] = os.path.join("OPC", "知识库", name)
    except OSError:
        pass

    try:
        with kernel.lock:
            entries = [dict(k) for k in kernel.knowledge][-120:]
    except Exception:
        entries = []
    creative_tag = ("选题定稿", "选题", "创意", "分镜", "文案", "金句", "脚本", "角度", "调性")
    ops_title = ("核验结论", "事故报告", "技术复核", "缺件回执", "授权链")
    kept = 0
    for k in entries:
        body = str(k.get("body") or "")
        tags = list(k.get("tags") or [])
        title = str(k.get("title") or k.get("summary") or "")
        has_tag = any(any(word in str(t) for word in creative_tag) for t in tags)
        if not (has_tag or "可复用点" in body):
            continue
        # CTO 事故/授权链核验类归档不是 CCO 创意灵感，排除
        if not has_tag and any(word in title for word in ops_title):
            continue
        kept += 1
        if kept > 36:
            break
        conclusion, reusable = "", []
        m = re.search(r"^#\s*一句话结论\s*$([\s\S]{0,900}?)(?=^#{1,2}\s|\Z)", body, re.M)
        if m:
            conclusion = _clean(m.group(1), 420)
        reusable = [_clean(x, 300) for x in re.findall(r"^[-*]\s*可复用点[：:]\s*(.+)$", body, re.M)]
        detail = "\n".join([x for x in [conclusion] + [f"可复用点：{r}" for r in reusable[:3]] if x])
        kb_id = str(k.get("kb_id") or k.get("id") or "")
        rel = kb_paths.get(kb_id, "")
        items.append(_item(
            CAT_DISTILL, "知识库归档", k.get("title") or k.get("summary") or "知识条目",
            conclusion or k.get("summary") or k.get("title") or "",
            detail or body[:700], tags[:8] + ["知识库蒸馏"],
            f"知识库归档 · {kb_id or k.get('id') or ''}",
            _vault_link(rel) if rel else "/app/knowledge",
            k.get("at") or "", None, "KERNEL.knowledge"))

    # 评审会定稿结论：CCO 从 9 条里蒸馏出的 6 条选题
    meeting_count = 0
    try:
        history = (kernel.meeting_payload() or {}).get("history") or []
    except Exception:
        history = []
    for rec in history[:5]:
        concl = str(rec.get("conclusion") or "").strip()
        if not concl:
            continue
        meeting_count += 1
        titles = re.findall(r"《([^》]+)》", concl)
        at = rec.get("startedAt") or rec.get("endedAt") or ""
        if titles:
            for t in titles[:8]:
                items.append(_item(
                    CAT_DISTILL, "评审会定稿", t, f"{rec.get('title') or '选题评审会'} 定稿选题",
                    concl, ["CCO 定稿", rec.get("title") or "选题评审会"],
                    f"选题评审会 · {(at or '')[:10]} · {rec.get('title') or ''}",
                    "/app/meeting", at, None, "KERNEL.meeting_payload()"))
        else:
            items.append(_item(
                CAT_DISTILL, "评审会结论", rec.get("title") or "评审会结论", concl, concl,
                ["CCO 定稿"], f"选题评审会 · {(at or '')[:10]}", "/app/meeting",
                at, None, "KERNEL.meeting_payload()"))
    return items, kept, meeting_count


def build(kernel, refresh: bool = False, vault: str = None):
    """返回 /api/inspiration 的响应体。"""
    vault = vault or VAULT
    with _CACHE_LOCK:
        cached = _CACHE["data"].get(vault)
        fresh = cached is not None and (time.time() - _CACHE["at"].get(vault, 0)) < CACHE_TTL
    if fresh and not refresh:
        return dict(cached, cached=True)

    hotspot_items, brief_count = _hotspot_ideas(kernel, vault)
    video_items, video_docs = _video_ideas(
        vault, include_repo_docs=getattr(kernel, "is_owner", True))
    distill_items, kb_entries, meeting_count = _distill_ideas(kernel, vault)
    items = hotspot_items + video_items + distill_items
    items.sort(key=lambda x: (str(x.get("at") or ""), str(x.get("title") or "")), reverse=True)

    counts = {CAT_HOTSPOT: len(hotspot_items), CAT_VIDEO: len(video_items),
              CAT_DISTILL: len(distill_items)}
    data = {
        "ok": True,
        "generatedAt": _now(),
        "cached": False,
        "counts": {"total": len(items), **counts},
        "sources": {
            "hotspotBriefs": brief_count,
            "videoDocs": video_docs,
            "knowledgeEntries": kb_entries,
            "meetingConclusions": meeting_count,
            "hotspotRows": len(hotspot_items),
        },
        "items": items[:220],
        "notes": [
            "热点线索来自 /api/hotspots（内存快照 + Obsidian 热点简报），简报角度来自「分项目可用角度」表。",
            "视频分析来自 Obsidian 素材拆解 / 给 CCO 的一页纸，以及仓库根目录的创意说明，按章节切分。",
            "蒸馏启发来自知识库归档的一句话结论 / 可复用点，以及选题评审会的定稿清单。",
        ],
    }
    with _CACHE_LOCK:
        _CACHE["at"][vault] = time.time()
        _CACHE["data"][vault] = data
    return data
