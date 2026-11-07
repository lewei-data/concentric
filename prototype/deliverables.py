#!/usr/bin/env python3
"""交付物落盘：把每条内容的选题 / 文案 / 分镜剧本 / 封面 / 样片写成实体文件。

全部写入 OPC_VAULT（默认 /opt/sefs/obsidian），不落临时目录：
  <vault>/OPC/交付物/<traceId>/00-index.md        这轮交付清单
  <vault>/OPC/交付物/<traceId>/06-log.md          过程记录（消息流水）
  <vault>/OPC/交付物/<traceId>/<pid>-<cid>/01-topic.md
  .../02-copy.md  .../03-storyboard.md  .../04-cover.png
  .../05-preview.mp4  .../07-data.json

样片是「按分镜自动生成的预览片」（animatic），不是实拍素材，片头与页脚都写明。
依赖：Pillow（必需，画封面与分镜帧）、ffmpeg（可选，来自 imageio-ffmpeg 或系统 PATH）。
"""
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VAULT = os.environ.get("OPC_VAULT", "/opt/sefs/obsidian")
ROOT = os.path.join(VAULT, "OPC", "交付物")
FONT_FILE = os.path.join(BASE_DIR, "assets", "fonts", "NotoSansSC-Regular.otf")
TZ = timezone(timedelta(hours=8))
W, H = 1080, 1920
URL_PREFIX = "/vault/OPC/交付物"

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except Exception:                     # 无 Pillow 时只写 markdown
    PIL_OK = False

_lock = threading.Lock()
_queue = None
_thread = None


def now():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _slug(s):
    return re.sub(r"[^a-zA-Z0-9_.-]", "", str(s)) or "x"


def plan(trace, pid, cid):
    """确定性路径：先算 URL，渲染可以后跑。"""
    rel = f"{_slug(trace)}/{_slug(pid)}-{_slug(cid)}"
    base = f"{URL_PREFIX}/{rel}"
    return {
        "dir": base, "rel": rel,
        "topic": f"{base}/01-topic.md", "copy": f"{base}/02-copy.md",
        "story": f"{base}/03-storyboard.md", "cover": f"{base}/04-cover.png",
        "video": f"{base}/05-preview.mp4", "final": f"{base}/05-final.mp4",
        "data": f"{base}/07-data.json",
        "assessment": f"{base}/08-assessment.md", "review": f"{base}/09-review.md",
        "traceIndex": f"{URL_PREFIX}/{_slug(trace)}/00-index.md",
        "traceLog": f"{URL_PREFIX}/{_slug(trace)}/06-log.md",
    }


MEET_DIR = os.path.join(VAULT, "OPC", "会议纪要")
MAIL_DIR = os.path.join(VAULT, "OPC", "邮件")


def export_meeting(trace, data):
    """写选题评审会纪要与邮件草稿（不代发，只落盘）。"""
    os.makedirs(os.path.join(MEET_DIR, _slug(trace)), exist_ok=True)
    os.makedirs(os.path.join(MAIL_DIR, _slug(trace)), exist_ok=True)
    final = data.get("final") or {}
    lines = [str(data.get("minutes") or "").strip(), "", "## 定稿清单（每项目 2 条）", ""]
    for pid, topics in final.items():
        for t in topics:
            lines.append(f"- {pid} · {t.get('title')}")
    lines += ["", f"- 生成时间：{now()}"]
    body = "\n".join(lines) + "\n"
    m_url = f"/vault/OPC/会议纪要/{_slug(trace)}/meeting-minutes.md"
    a_url = f"/vault/OPC/邮件/{_slug(trace)}/email-draft.md"
    with _lock:
        open(_local(m_url), "w", encoding="utf-8").write(body)
        mail = "\n".join([
            "# 选题评审会 · 邮件草稿", "",
            "- 状态：**草稿，尚未发送**（当前未接入邮件服务）",
            f"- 主题：选题评审会纪要 · {(data.get('hotspot') or {}).get('title', trace)}",
            f"- 收件人：{'、'.join(data.get('recipients') or [])}",
            f"- 抄送：{'、'.join(data.get('cc') or [])}",
            "", "## 正文", "", body,
        ])
        open(_local(a_url), "w", encoding="utf-8").write(mail)
    return {"minutes": m_url, "mail": a_url}


MEET_INDEX = os.path.join(MEET_DIR, "index.json")
MEET_INDEX_MD = os.path.join(MEET_DIR, "00-会议索引.md")


def _meeting_slug(rec):
    return _slug(rec.get("id") or rec.get("traceId") or "meeting")


def _minute_ts(value):
    return str(value or "").replace("T", " ")[:19] or "—"


def _people_text(rec):
    names = []
    for p in rec.get("participants") or []:
        if isinstance(p, dict):
            names.append(f"{p.get('name', p.get('id', ''))}（{p.get('role', '')}）")
        else:
            names.append(str(p))
    return "、".join(names) or "—"


def save_meeting_record(rec):
    """把一场会议的完整发言与结论写入 Obsidian，并维护会议索引。

    目录：OPC/会议纪要/<meetingId>/meeting-transcript.md
    索引：OPC/会议纪要/index.json（机器读）+ 00-会议索引.md（人读）
    """
    slug = _meeting_slug(rec)
    folder = os.path.join(MEET_DIR, slug)
    os.makedirs(folder, exist_ok=True)
    md_path = os.path.join(folder, "meeting-transcript.md")
    url = f"/vault/OPC/会议纪要/{slug}/meeting-transcript.md"

    speeches = rec.get("speeches") or []
    lines = [
        "---",
        f"title: {rec.get('title') or '会议'}",
        f"meeting_id: {rec.get('id') or slug}",
        f"trace: {rec.get('traceId') or ''}",
        f"status: {rec.get('status') or 'ended'}",
        f"started: {rec.get('startedAt') or ''}",
        f"ended: {rec.get('endedAt') or ''}",
        f"participants: {len(rec.get('participants') or [])}",
        "tags: [opc, 会议纪要]",
        "---",
        "",
        f"# {rec.get('title') or '会议'}",
        "",
        "| 项 | 内容 |",
        "|---|---|",
        f"| 会议 ID | `{rec.get('id') or slug}` |",
        f"| 关联 Trace | `{rec.get('traceId') or '—'}` |",
        f"| 开始 | {_minute_ts(rec.get('startedAt'))} |",
        f"| 结束 | {_minute_ts(rec.get('endedAt'))} |",
        f"| 参会 | {_people_text(rec)} |",
        f"| 状态 | {'进行中' if rec.get('status') == 'live' else '已结束'} |",
        "",
        "## 议题",
        "",
    ]
    agenda = rec.get("agenda") or []
    lines += [f"- {a}" for a in agenda] or ["- —"]
    lines += ["", "## 发言记录", ""]
    if speeches:
        for i, sp in enumerate(speeches, 1):
            who = f"{sp.get('speaker', '')} · {sp.get('role', '')}".strip(" ·")
            lines += [f"### {i}. {who} · {_minute_ts(sp.get('at'))[11:]}", "",
                      f"> {str(sp.get('text') or '').replace(chr(10), chr(10) + '> ')}", ""]
    else:
        lines += ["- 本次会议未记录到发言。", ""]
    lines += ["## 会议结论", "", rec.get("conclusion") or "（未形成结论）", "",
              f"- 归档时间：{now()}", ""]

    with _lock:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        records = []
        try:
            with open(MEET_INDEX, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                records = loaded
        except Exception:
            records = []
        rec = dict(rec)
        rec["transcriptUrl"] = url
        records = [r for r in records if _meeting_slug(r) != slug]
        records.insert(0, rec)
        records = records[:50]
        with open(MEET_INDEX, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=1)

        rows = []
        for r in records:
            rs = _meeting_slug(r)
            title = str(r.get("title") or "会议").replace("|", "\\|")
            concl = str(r.get("conclusion") or "—").replace("\n", " ").replace("|", "\\|")[:60]
            rows.append(f"| [[OPC/会议纪要/{rs}/meeting-transcript\\|{title}]] | {_minute_ts(r.get('endedAt') or r.get('startedAt'))} | {len(r.get('participants') or [])} | {concl} |")
        index_md = "\n".join([
            "---", "title: 会议纪要索引", "tags: [opc, 会议纪要, 索引]", f"updated: {now()}", "---", "",
            "# 会议纪要索引", "",
            f"> [!info] 共 {len(records)} 场会议，由 3D 会议室自动归档。", "",
            "| 会议 | 结束时间 | 参会 | 结论摘要 |", "|---|---|---|---|",
            *(rows or ["| — | — | — | — |"]), "",
        ])
        with open(MEET_INDEX_MD, "w", encoding="utf-8") as f:
            f.write(index_md)
    return {"transcriptUrl": url, "minutesUrl": rec.get("minutesUrl") or ""}


def load_meeting_records(limit=40):
    """读取会议历史：优先 index.json；同时补上磁盘上更早的会议纪要目录。"""
    records = []
    try:
        with open(MEET_INDEX, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, list):
            records = [r for r in loaded if isinstance(r, dict)]
    except Exception:
        records = []
    seen = {_meeting_slug(r) for r in records}
    try:
        names = sorted(os.listdir(MEET_DIR))
    except OSError:
        names = []
    for name in names:
        folder = os.path.join(MEET_DIR, name)
        if name.startswith(".") or not os.path.isdir(folder) or name in seen:
            continue
        transcript = os.path.join(folder, "meeting-transcript.md")
        minutes = os.path.join(folder, "meeting-minutes.md")
        f = transcript if os.path.isfile(transcript) else (minutes if os.path.isfile(minutes) else "")
        if not f:
            continue
        try:
            text = open(f, encoding="utf-8").read()
        except OSError:
            continue
        title = ""
        for ln in text.splitlines():
            if ln.startswith("# "):
                title = ln[2:].strip()
                break
        mt = datetime.fromtimestamp(os.path.getmtime(f), TZ)
        ts = mt.strftime("%Y-%m-%dT%H:%M:%S")
        records.append({"id": name, "traceId": name, "title": title or "会议",
                        "status": "ended", "participants": [], "agenda": [], "speeches": [],
                        "startedAt": ts, "endedAt": ts, "conclusion": "", "legacy": True,
                        "transcriptUrl": f"/vault/OPC/会议纪要/{name}/{os.path.basename(f)}"})
    records.sort(key=lambda r: str(r.get("endedAt") or r.get("startedAt") or ""), reverse=True)
    return records[:limit]


def load_deliverable_items(limit=240):
    """从 Obsidian 交付物目录恢复历史条目。

    每个条目目录下的 07-data.json 是当前阶段的完整快照，刷新首页或服务重启后
    用它回填内容流水线与交付清单，避免“历史记录全部消失”。
    """
    items = []
    try:
        traces = [d for d in os.listdir(ROOT) if d.startswith("tr_")]
    except OSError:
        return []
    for trace in traces:
        tdir = os.path.join(ROOT, trace)
        if not os.path.isdir(tdir):
            continue
        try:
            subs = os.listdir(tdir)
        except OSError:
            continue
        for sub in subs:
            f = os.path.join(tdir, sub, "07-data.json")
            if not os.path.isfile(f):
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    item = json.load(fh)
            except Exception:
                continue
            if not isinstance(item, dict) or not item.get("id") or not item.get("projectId"):
                continue
            files = item.get("files") or {}
            video = files.get("video")
            if not item.get("assets") and video and os.path.isfile(_local(video)):
                item["assets"] = [video]
            try:
                item["_savedAt"] = os.path.getmtime(f)
            except OSError:
                item["_savedAt"] = 0
            items.append(item)
    items.sort(key=lambda x: x.get("_savedAt", 0))
    items = items[-limit:]
    for item in items:
        item.pop("_savedAt", None)
    return items


def load_recent_messages(limit=120, traces=6):
    """从最近几轮 06-log.md 过程记录恢复事件流与会议纪要的历史消息。"""
    files = []
    try:
        for tr in os.listdir(ROOT):
            if not tr.startswith("tr_"):
                continue
            f = os.path.join(ROOT, tr, "06-log.md")
            if os.path.isfile(f):
                try:
                    files.append((os.path.getmtime(f), tr, f))
                except OSError:
                    continue
    except OSError:
        return []
    files.sort()
    out = []
    for _, trace, f in files[-traces:]:
        try:
            lines = open(f, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        for line in lines:
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 5:
                continue
            at, sender, recipient, mtype, summary = cells[:5]
            if not at or at == "时间" or set(at) <= set("-: "):
                continue
            out.append({"id": f"msg_{len(out):05d}", "traceId": trace, "type": mtype,
                        "sender": sender, "recipient": recipient, "at": at, "ttl": 20,
                        "priority": 1, "projectId": None, "payload": {"text": summary}})
    out.sort(key=lambda m: str(m.get("at") or ""))
    return out[-limit:]


HOT_DIR = os.path.join(VAULT, "OPC", "热点采集")
KB_DIR = os.path.join(VAULT, "OPC", "知识库")
ARCHIVE_DIR = os.path.join(VAULT, "OPC", "_归档")


def load_hotspots(limit=20):
    """从 OPC/热点采集 的结构化「热点简报」恢复历史热点。

    只认含 `- 热点：` 行的简报；GitHub Trending 原始榜单等文件不是业务热点，跳过。
    """
    out = []
    try:
        names = os.listdir(HOT_DIR)
    except OSError:
        return []
    for name in names:
        if not name.endswith(".md"):
            continue
        path = os.path.join(HOT_DIR, name)
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        m = re.search(r"^- 热点：\s*(.+?)\s*$", text, re.M)
        if not m:
            continue
        score = re.search(r"^- 热度：\s*([0-9.]+)", text, re.M)
        tags = re.search(r"^- 标签：\s*(.+?)\s*$", text, re.M)
        try:
            at = datetime.fromtimestamp(os.path.getmtime(path), TZ).isoformat(timespec="seconds")
        except OSError:
            at = ""
        out.append({
            "id": "hs_" + _slug(os.path.splitext(name)[0]),
            "title": m.group(1).strip(),
            "score": float(score.group(1)) if score else 0.8,
            "tags": [x.strip() for x in re.split(r"[/、,，]", tags.group(1)) if x.strip()] if tags else [],
            "source": "热点采集/" + name,
            "fit": {},
            "at": at,
        })
    out.sort(key=lambda h: str(h.get("at") or ""))
    return out[-limit:]


DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def load_collection_status():
    """读取苏寒最近一次 GitHub Trending 采集，返回“采集进程状态”。

    原始榜只用于证明采集在跑，不作为业务热点；业务热点仍走 `- 热点：` 简报。
    """
    try:
        names = [n for n in os.listdir(HOT_DIR) if DATE_FILE_RE.match(n)]
    except OSError:
        return None
    if not names:
        return None
    name = max(names, key=lambda n: os.path.getmtime(os.path.join(HOT_DIR, n)))
    path = os.path.join(HOT_DIR, name)
    try:
        text = open(path, encoding="utf-8").read()
        at = datetime.fromtimestamp(os.path.getmtime(path), TZ).isoformat(timespec="seconds")
    except OSError:
        return None

    picked = None
    for sec in reversed(list(re.finditer(r"^##\s+(\d{2}:\d{2})\s*$", text, re.M))):
        tail = text[sec.end():]
        rows = []
        for line in tail.splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 5:
                continue
            if cells[0] == "排名" or set(cells[0]) <= set("-: "):
                continue
            rows.append({"rank": cells[0], "repo": cells[1], "stars": cells[2],
                         "lang": cells[3], "desc": cells[4]})
        if rows:
            picked = (sec, tail, rows)
            break
    if not picked:
        return None

    sec, tail, rows = picked
    m = re.search(r"页面实际返回\s*(\d+)\s*条", tail)
    return {"date": name[:-3], "time": sec.group(1),
            "count": int(m.group(1)) if m else len(rows), "rows": len(rows),
            "top": rows[0], "items": rows, "at": at, "file": "热点采集/" + name,
            "source": "GitHub Trending"}


def _frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm = {}
    for line in text[3:end].splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip().strip('"')
    return fm, text[end + 4:]


def load_knowledge(limit=40):
    """从 OPC/知识库（含 _归档）恢复知识条目，供首页「热点与知识库」展示。"""
    paths = []
    try:
        for name in os.listdir(KB_DIR):
            if name.endswith(".md") and not name.startswith("00-"):
                paths.append(os.path.join(KB_DIR, name))
    except OSError:
        pass
    try:
        for root, _dirs, files in os.walk(ARCHIVE_DIR):
            if os.path.basename(root) != "知识库":
                continue
            for name in files:
                if name.endswith(".md") and not name.startswith("00-"):
                    paths.append(os.path.join(root, name))
    except OSError:
        pass

    out = []
    for path in paths:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        fm, body = _frontmatter(text)
        title = fm.get("title") or ""
        if not title:
            m = re.search(r"^#\s+(.+)$", body, re.M)
            title = m.group(1).strip() if m else os.path.splitext(os.path.basename(path))[0]
        title = re.sub(r"^知识条目\s*·\s*", "", title)
        project = fm.get("project") or "全公司"
        pid = "全公司"
        for name, key in (("文人莫克", "wenren"), ("狗大人", "gou"), ("同心圆", "tongxinyuan")):
            if name in project:
                pid = key
                break
        tags = re.findall(r"[\w\u4e00-\u9fa5/\-]+", fm.get("tags") or "")
        # body 取落盘全文（去掉文件尾部的“- 标签：”行）；summary 只是首行前 120 字，供列表预览
        kept = []
        for line in body.splitlines():
            if line.strip().startswith("- 标签："):
                break
            kept.append(line)
        full_text = "\n".join(kept).strip()
        summary = ""
        for line in full_text.splitlines():
            s = line.strip().lstrip("#").strip()
            if s and not s.startswith("|") and not s.startswith("-"):
                summary = s[:120]
                break
        out.append({
            "id": fm.get("kb_id") or ("kb_" + _slug(os.path.splitext(os.path.basename(path))[0])),
            "projectId": pid,
            "title": title,
            "body": full_text,
            "summary": summary,
            "tags": tags[:8],
            "sourceTrace": fm.get("trace") or "",
            "at": fm.get("archived") or "",
            "source": os.path.relpath(path, VAULT),
        })
    out.sort(key=lambda k: str(k.get("at") or ""))
    return out[-limit:]


PROJECT_DISPLAY = {"wenren": "文人莫克", "gou": "狗大人", "tongxinyuan": "同心圆",
                   "all": "全公司", "全公司": "全公司"}


def _kb_filename(entry):
    title = str(entry.get("title") or "").strip()
    day = str(entry.get("at") or now())[:10].replace("/", "-")
    slug = re.sub(r"[^\w\u4e00-\u9fa5-]+", "-", title).strip("-")[:60] or "kb"
    kid = str(entry.get("id") or "kb").split("_")[-1]
    return f"{day}-{slug}-{kid}.md"


def save_knowledge(entry):
    """知识条目写回 OPC/知识库：Obsidian 是唯一数据源，Web 归档即落盘（同 id 覆盖同一文件）。"""
    title = str(entry.get("title") or "").strip()
    if not title:
        return ""
    os.makedirs(KB_DIR, exist_ok=True)
    path = os.path.join(KB_DIR, _kb_filename(entry))
    tags = "、".join(str(t) for t in (entry.get("tags") or []) if t)
    body = str(entry.get("body") or "").strip() or title
    lines = [
        "---",
        f"kb_id: {entry.get('id') or ''}",
        f"title: {title}",
        f"project: {PROJECT_DISPLAY.get(entry.get('projectId'), '全公司')}",
        f"tags: {tags}",
        f"trace: {entry.get('traceId') or ''}",
        f"archived: {entry.get('at') or now()}",
        "---",
        "",
        body,
        "",
        f"- 标签：{tags or '-'}",
    ]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


def knowledge_stamp():
    """知识库指纹（文件数 + 最新 mtime），60 秒轮询用它判断是否要重扫。"""
    total, latest = 0, 0.0
    for root, _dirs, files in os.walk(KB_DIR):
        for name in files:
            if not name.endswith(".md") or name.startswith("00-"):
                continue
            try:
                latest = max(latest, os.path.getmtime(os.path.join(root, name)))
            except OSError:
                continue
            total += 1
    return (total, round(latest, 3))


def _local(url_path):
    return os.path.join(VAULT, url_path[len("/vault/"):].replace("/", os.sep))


# ---------- 分镜归一化 ----------
def _sec_of(s, default=6.0):
    if isinstance(s, dict):
        d = str(s.get("时长") or s.get("duration") or s.get("time") or "")
    else:
        d = str(s)
    m = re.search(r"(\d+(?:\.\d+)?)\s*[–\-—~至]\s*(\d+(?:\.\d+)?)", d)
    if m:
        return max(1.0, float(m.group(2)) - float(m.group(1)))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(s|秒)", d)
    if m:
        return max(1.0, float(m.group(1)))
    return default


_SHOT_HEAD = re.compile(
    r"^\s*(?:镜|第)?\s*\d+\s*镜?\s*"
    r"(\d+(?:\.\d+)?)\s*[–\-—~至]\s*(\d+(?:\.\d+)?)\s*(?:s|秒)?\s*")
_LINE_LABEL = re.compile(r"^(旁白|台词|口播|字幕|VO|画外音)\s*[①②③④⑤⑥⑦⑧⑨⑩0-9]*\s*[：:=＝]\s*")
_LINE_ANY = re.compile(r"(旁白|台词|口播|字幕|VO|画外音)\s*[①②③④⑤⑥⑦⑧⑨⑩0-9]*\s*[：:=＝]\s*")


def _string_shot(text, i, dur=None):
    """形如「1｜8s｜黑白照片特写｜旁白：一句话」的整行，拆成镜号/时长/画面/台词。

    也兼容「镜1 0–6s 手机屏特写，…｜AI 给我爷爷修了个微笑」这种只在首段带镜号
    与时间码、末段没有「旁白：」前缀的写法；以及画面与台词写在同一个片段里的
    「…；旁白起。台词：底片只有一个版本…」。
    """
    parts = [p.strip() for p in re.split(r"[｜|]", str(text)) if p.strip()]
    visual, line, sec = [], "", dur
    for p in parts:
        m = _SHOT_HEAD.match(p)
        if m:
            sec = float(m.group(2)) - float(m.group(1))
            p = p[m.end():].strip()
            if not p:
                continue
        if re.fullmatch(r"[镜第]*\s*\d+[\s镜]*", p):
            continue
        if re.fullmatch(r"\d+(\.\d+)?\s*(s|秒)", p):
            sec = float(re.findall(r"\d+(?:\.\d+)?", p)[0])
        elif _LINE_LABEL.match(p):
            line = _LINE_LABEL.sub("", p).strip()
        else:
            ml = _LINE_ANY.search(p)
            if ml and not line:
                line = p[ml.end():].strip()
                p = p[:ml.start()].strip()
                if not p:
                    continue
            visual.append(p)
    # 没有显式「旁白：」标签的两段式分镜：末段按台词处理
    if not line and len(visual) >= 2:
        line = visual.pop()
    return {"no": i + 1, "dur": sec,
            "visual": "｜".join(visual) or str(text), "line": line}


def normalize_storyboard(raw, total_sec=45):
    """把模型给的三种形态（字符串 / 中文字段对象 / 英文字段对象）统一成镜头列表。"""
    if not raw:
        return []
    # 模型偶尔把整段分镜按字符拆成数组（每个元素 1 个字），先拼回整段文本
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw) and len(raw) > 10:
        if sum(len(x) for x in raw) / len(raw) <= 3:
            joined = "".join(raw)
            if "\n" in joined:
                raw = joined
    if isinstance(raw, str):
        lines = [x.strip() for x in raw.split("\n") if x.strip()]
        each = round(total_sec / max(1, len(lines)), 1)
        return [_string_shot(x, i, each) for i, x in enumerate(lines)]
    if not isinstance(raw, list):
        return []
    out = []
    for i, s in enumerate(raw):
        if isinstance(s, str):
            out.append(_string_shot(s, i, _sec_of(s, 0)))
            continue
        if not isinstance(s, dict):
            continue
        pick = lambda *ks: next((str(s[k]) for k in ks if s.get(k) not in (None, "")), "")
        raw_ill = s.get("插图") or s.get("illustration") or s.get("插图物件") or s.get("objects")
        if isinstance(raw_ill, dict):
            ill = raw_ill
        elif isinstance(raw_ill, (list, str)) and raw_ill:
            ill = {"objects": raw_ill}
        else:
            ill = None
        out.append({"no": pick("镜", "shot", "shotNo", "no", "序号") or str(i + 1),
                    "dur": _sec_of(s, 0), "visual": pick("画面", "visual", "picture", "image", "scene"),
                    "line": pick("台词", "line", "dialogue", "voiceover", "audio"),
                    "illustration": ill})
    # 没有时长的镜头，按剩余秒数均分
    miss = [x for x in out if not x["dur"]]
    if miss:
        used = sum(x["dur"] for x in out if x["dur"]) or 0
        each = max(2.0, (total_sec - used) / len(miss)) if len(miss) else 6
        for x in miss:
            x["dur"] = round(each, 1)
    return out


# ---------- 绘图 ----------
def _font(size):
    return ImageFont.truetype(FONT_FILE, size)


def _wrap(text, font, maxw):
    lines = []
    for para in str(text).split("\n"):
        cur = ""
        for ch in para:
            if font.getlength(cur + ch) <= maxw:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        lines.append(cur)
    return [x for x in lines if x.strip()]


def _hex(c, fallback=(79, 70, 229)):
    try:
        c = str(c).lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def _bg(draw, top, bottom):
    for y in range(H):
        t = y / (H - 1)
        col = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        draw.line([(0, y), (W, y)], fill=col)


def _brand(draw, project, item, tag):
    draw.text((72, 96), "同心圆 · " + project.get("name", ""), font=_font(34), fill=(255, 255, 255, 200))
    draw.text((72, 148), tag, font=_font(26), fill=(255, 255, 255, 130))
    draw.text((W - 72, 100), str(item.get("traceId") or ""), font=_font(24),
              fill=(255, 255, 255, 120), anchor="ra")


def _footer(draw, text):
    draw.line([(72, H - 190), (W - 72, H - 190)], fill=(255, 255, 255, 40), width=2)
    for i, line in enumerate(_wrap(text, _font(28), W - 144)):
        draw.text((72, H - 160 + i * 40), line, font=_font(28), fill=(255, 255, 255, 135))


def make_cover(item, project, path):
    if not PIL_OK:
        return False
    img = Image.new("RGB", (W, H), (15, 17, 24))
    d = ImageDraw.Draw(img, "RGBA")
    base = _hex(project.get("color"))
    _bg(d, tuple(int(c * 0.55) for c in base), (10, 12, 18))
    for x in range(0, W, 90):          # 同心圆水印
        for y in range(0, H, 90):
            d.ellipse([x, y, x + 60, y + 60], outline=(255, 255, 255, 8), width=1)
    _brand(d, project, item, "封面 · 45 秒竖屏")
    y = 640
    for line in _wrap(item.get("title") or "未命名", _font(66), W - 144)[:5]:
        d.text((72, y), line, font=_font(66), fill=(255, 255, 255))
        y += 96
    y += 40
    for line in _wrap(item.get("cover") or "", _font(44), W - 144)[:4]:
        d.text((72, y), line, font=_font(44), fill=(255, 220, 150))
        y += 64
    _footer(d, "SAMPLE · 按分镜自动生成的封面，非实拍")
    img.save(path, "PNG")
    return True



def _md_topic(item, project, tone):
    return "\n".join([
        f"# 选题 · {item.get('title') or '未命名'}", "",
        f"- 项目：{project.get('name')}（{item.get('projectId')}）",
        f"- 负责人：{item.get('owner') or '—'}",
        f"- 风格：{tone or item.get('toneProfile') or '—'}",
        f"- 阶段：{item.get('stage')}", f"- trace：{item.get('traceId')}",
        f"- 记录时间：{now()}", "",
        "## 选题角度", "", item.get("title") or "—", "",
    ])


def _md_copy(item):
    lines = [f"# 文案 · {item.get('title') or '未命名'}", "",
             f"- 负责人：{item.get('owner') or '—'}", f"- 阶段：{item.get('stage')}", "",
             "## 成稿文案", "", item.get("copy") or "（尚未生成）", ""]
    if item.get("reviewNote"):
        lines += ["## 审核意见", "", item["reviewNote"], ""]
    return "\n".join(lines)


def _md_story(item, shots, total_sec):
    lines = [f"# 分镜剧本 · {item.get('title') or '未命名'}", "",
             f"- 镜数：{len(shots)}", f"- 时长：约 {total_sec:.0f} 秒",
             f"- 封面文案：{item.get('cover') or '—'}", ""]
    if shots:
        lines += ["| 镜 | 时长 | 画面 | 台词 / 口播 |", "|---|---|---|---|"]
        for s in shots:
            cells = [f"镜{s['no']}", f"{s['dur']:.1f}s" if s.get("dur") else "—",
                     (s.get("visual") or "—").replace("|", "／").replace("\n", " "),
                     (s.get("line") or "—").replace("|", "／").replace("\n", " ")]
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines += ["（模型只返回了镜数，没有逐镜剧本）"]
    v = item.get("video") or {}
    if v.get("summary"):
        lines += ["", "## 成片方案", "", v["summary"]]
    if v.get("evidence"):
        lines += ["", "## 依据", ""] + [f"- {e}" for e in v["evidence"]]
    lines.append("")
    return "\n".join(lines)


def _md_assessment(item):
    a = item.get("assessment") or {}
    p = item.get("plan") or {}
    lines = [f"# 制作评估报告 · {item.get('title') or '未命名'}", "",
             f"- 项目：{item.get('projectId')}", f"- 负责人：{item.get('owner') or '—'}",
             f"- 预估时长：{p.get('durationSec', item.get('durationSec', '—'))} 秒",
             f"- 配音/TTS：{(p.get('tts') or {}).get('voice', '—')} / {(p.get('tts') or {}).get('style', '—')}",
             f"- 阶段：{item.get('stage')}", ""]
    if p.get("copyBrief"):
        lines += ["## 文案要求", "", p["copyBrief"], ""]
    if p.get("storyboardNotes"):
        lines += ["## 分镜要求", "", p["storyboardNotes"], ""]
    if p.get("materials"):
        lines += ["## 素材清单", ""] + [f"- {m}" for m in p["materials"]] + [""]
    if a.get("assetGaps"):
        lines += ["## 素材缺口（已报苏寒）", ""] + [
            f"- {g.get('item', '—')}：{g.get('why', '')}" if isinstance(g, dict) else f"- {g}"
            for g in a["assetGaps"]] + [""]
    if item.get("assetResolution"):
        lines += ["## 素材工单结论", ""] + [
            f"- {g.get('item', '—')}：{g.get('status', '')} {g.get('alternative', '')}".strip()
            if isinstance(g, dict) else f"- {g}" for g in item["assetResolution"]] + [""]
    if a.get("summary"):
        lines += ["## 评估结论", "", a["summary"], ""]
    return "\n".join(lines)


def _md_review(item):
    lines = [f"# 评审记录 · {item.get('title') or '未命名'}", "",
             f"- 阶段：{item.get('stage')}", f"- 终审意见：{item.get('reviewNote') or '—'}", ""]
    for role, r in (item.get("sampleReviews") or {}).items():
        lines += [f"## 样片评审 · {role}", "",
                  f"- 结论：{'通过' if r.get('pass') else '未通过'}",
                  f"- 理由：{'；'.join(r.get('reasons') or []) or '—'}", ""]
    if item.get("finalCut"):
        lines += ["## 最终精剪", "", item["finalCut"], ""]
    if item.get("finalAssets"):
        lines += ["## 交付物", ""] + [f"- {a}" for a in item["finalAssets"]] + [""]
    return "\n".join(lines)


def export_item(item, project, tone=""):
    """写条目文档 + 封面；样片由 om_render 真实渲染后落到同目录。"""
    f = plan(item["traceId"], item["projectId"], item["id"])
    d = _local(f["dir"])
    os.makedirs(d, exist_ok=True)
    shots = normalize_storyboard(item.get("storyboard"), 45)
    total = sum(s["dur"] or 0 for s in shots) or 45
    with _lock:
        open(_local(f["topic"]), "w", encoding="utf-8").write(_md_topic(item, project, tone))
        open(_local(f["copy"]), "w", encoding="utf-8").write(_md_copy(item))
        open(_local(f["story"]), "w", encoding="utf-8").write(_md_story(item, shots, total))
        open(_local(f["assessment"]), "w", encoding="utf-8").write(_md_assessment(item))
        open(_local(f["review"]), "w", encoding="utf-8").write(_md_review(item))
        open(_local(f["data"]), "w", encoding="utf-8").write(
            json.dumps(item, ensure_ascii=False, indent=2))
    out = {"video_written": False, "error": ""}
    if not PIL_OK:
        out["error"] = "Pillow 不可用，只写了文档"
        return out
    with _lock:
        make_cover(item, project, _local(f["cover"]))
    out["video_written"] = os.path.exists(_local(f["video"]))
    return out


def export_trace(trace, contents, messages, approvals, projects_names, meta=None):
    """写本轮交付清单 00-index.md 与过程记录 06-log.md。"""
    d = os.path.join(ROOT, _slug(trace))
    os.makedirs(d, exist_ok=True)
    meta = meta or {}
    idx = [f"# 交付清单 · {trace}", "", f"- 生成时间：{now()}", ""]
    if meta:
        idx += ["## 本轮状态", "",
                f"- 状态：{meta.get('status') or '—'}",
                f"- 开始：{meta.get('startedAt') or '—'}",
                f"- 收口：{meta.get('closedAt') or '—'}", ""]
    for c in contents:
        sub = f"{_slug(c['projectId'])}-{_slug(c['id'])}"
        idx += [f"## {projects_names.get(c['projectId'], c['projectId'])} · {c.get('title') or '未命名'}",
                "", f"- 阶段：{c.get('stage')}", f"- 负责人：{c.get('owner') or '—'}", "",
                f"[选题]({sub}/01-topic.md) [文案]({sub}/02-copy.md) "
                f"[分镜剧本]({sub}/03-storyboard.md) [封面]({sub}/04-cover.png) "
                f"[样片]({sub}/05-preview.mp4) [制作评估]({sub}/08-assessment.md) "
                f"[评审记录]({sub}/09-review.md)", ""]
    if approvals:
        idx += ["## 审批", ""]
        for a in approvals:
            idx += [f"- {a.get('title')} · {a.get('status')} · 审批人 {a.get('approver')}"]
    open(os.path.join(d, "00-index.md"), "w", encoding="utf-8").write("\n".join(idx) + "\n")

    log = [f"# 过程记录 · {trace}", "", f"- 导出时间：{now()}",
           "- 说明：本文件由工作台自动写入，记录本轮的消息流水与阶段推进。"]
    if meta:
        log.append(f"- 本轮状态：{meta.get('status') or '—'}（开始 {meta.get('startedAt') or '—'} / "
                   f"收口 {meta.get('closedAt') or '—'}）")
    log += ["", "| 时间 | 发起 | 接收 | 类型 | 摘要 |", "|---|---|---|---|---|"]
    for m in messages:
        p = m.get("payload") or {}
        r = p.get("result") or {}
        text = (r.get("summary") or p.get("text") or p.get("task") or p.get("verdict") and
                "审核" + ("通过" if p["verdict"].get("pass") else "驳回") or "")
        if isinstance(text, dict):
            text = str(text)
        text = str(text).replace("|", "／").replace("\n", " ")[:90]
        log.append(f"| {m.get('at', '')} | {m.get('sender', '')} | {m.get('recipient', '')} | "
                   f"{m.get('type', '')} | {text} |")
    open(os.path.join(d, "06-log.md"), "w", encoding="utf-8").write("\n".join(log) + "\n")


def submit(fn):
    """单线程队列：ffmpeg 渲染串行，避免抢 CPU。"""
    global _queue, _thread
    if _queue is None:
        _queue = []
        def run():
            while True:
                job = _queue.pop(0) if _queue else None
                if job is None:
                    import time
                    time.sleep(0.3)
                    continue
                try:
                    job()
                except Exception as e:
                    print(f"[export] 失败 {type(e).__name__}: {e}", flush=True)
        _thread = threading.Thread(target=run, daemon=True)
        _thread.start()
    _queue.append(fn)
