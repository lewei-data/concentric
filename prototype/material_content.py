#!/usr/bin/env python3
"""素材内容化引擎：把 Web / MCP / 飞书 收到的图片、影音、文档主动变成知识。

流水线：入队（sha256 去重）→ 文本抽取（OCR / ASR / 文档解析）→ LLM 整理成卡片
      → vault_digest 双写（vault md + knowledge 表，kb_graph 自动可见）
      → asset_score 评分 → 小秘书 + 飞书 汇总汇报老板。

三个入口只做 enqueue()，重活在 Web 进程的后台线程 / CLI --once 里做；
队列落 ops/material_content_queue.jsonl，flock 抢锁，进程重启不丢单。
"""
import argparse
import base64
import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
OPS_DIR = ROOT / "ops"
QUEUE = OPS_DIR / "material_content_queue.jsonl"
STATE = OPS_DIR / "material_content_state.json"
LOCK = OPS_DIR / ".material_content.lock"

TZ = timezone(timedelta(hours=8))
OWNER_PHONE = os.environ.get("OPC_OWNER_PHONE", "18911789286")
MAX_BYTES = int(float(os.environ.get("OPC_MATERIAL_MAX_MB", "200")) * 1024 * 1024)
LLM_CHARS = 12000
ASR_SECONDS = int(os.environ.get("OPC_MATERIAL_ASR_SEC", "300"))
NOTIFY_BOSS = os.environ.get("OPC_MATERIAL_NOTIFY", "1") != "0"
NOTIFY_FEISHU = os.environ.get("OPC_MATERIAL_FEISHU", "1") != "0"
WORKBENCH_URL = os.environ.get("OPC_WORKBENCH_URL", "http://127.0.0.1:8848").rstrip("/")

# 本机 ~/.local/bin/ffmpeg 是 2018 静态版（adelay 无 all），必须优先用系统 6.1.1
FFMPEG = "/usr/bin/ffmpeg" if Path("/usr/bin/ffmpeg").is_file() else (shutil.which("ffmpeg") or "ffmpeg")
FFPROBE = "/usr/bin/ffprobe" if Path("/usr/bin/ffprobe").is_file() else (shutil.which("ffprobe") or "ffprobe")
TESSERACT = shutil.which("tesseract") or ""

TEXT_EXT = {".md", ".markdown", ".txt", ".json", ".csv", ".tsv", ".log", ".srt",
            ".vtt", ".yaml", ".yml", ".html", ".htm", ".xml", ".ini", ".conf"}
# 这些格式 MCP 侧已有成熟解析（xlsx/pptx/zip 走 zip+xml，旧版 Office 走 soffice），
# 内容化引擎直接复用，避免两套口径不一致。
MCP_DOC_EXT = {".xlsx", ".xls", ".pptx", ".ppt", ".zip", ".odt", ".ods", ".odp"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

_LLM_BASE = os.environ.get("OPC_MCP_LLM_BASE_URL", "http://192.168.21.208:8000/v1").rstrip("/")
_LLM_MODEL = os.environ.get("OPC_MCP_LLM_MODEL", "auto-free")
_LLM_KEY = (os.environ.get("OPC_MCP_LLM_API_KEY")
            or os.environ.get("OPC_LLM_AUTOFREE_KEY")
            or "sk-litellm-15bbcf2ea58c156130a00afd1b43ae4a")


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for buf in iter(lambda: f.read(4 << 20), b""):
            h.update(buf)
    return h.hexdigest()


@contextmanager
def _locked(blocking=True):
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    fp = open(LOCK, "a+")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield fp
    finally:
        fcntl.flock(fp, fcntl.LOCK_UN)
        fp.close()


def _read_queue():
    if not QUEUE.is_file():
        return []
    rows = []
    for line in QUEUE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _write_queue(rows):
    tmp = QUEUE.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                   + ("\n" if rows else ""), encoding="utf-8")
    os.replace(tmp, QUEUE)


def enqueue(path, filename="", source="web", phone="", sha256="",
            preset_text="", note=""):
    """素材入队。同 sha256 不重复内容化；调用方只管调，失败不影响原流程。"""
    p = Path(str(path or ""))
    try:
        if not p.is_file():
            return {"ok": False, "error": "文件不存在"}
        size = p.stat().st_size
        if size <= 0 or size > MAX_BYTES:
            return {"ok": False, "error": f"文件大小不合法：{size}"}
        digest = sha256 or _sha(p)
        with _locked():
            rows = _read_queue()
            for row in rows:
                if row.get("sha256") == digest and row.get("status") != "failed":
                    return {"ok": True, "duplicate": True, "id": row["id"],
                            "status": row.get("status")}
            item = {"id": "mc_" + digest[:16], "path": str(p.resolve()),
                    "filename": str(filename or p.name)[:120],
                    "source": str(source or "web")[:24],
                    "phone": str(phone or OWNER_PHONE),
                    "size": size, "sha256": digest,
                    "preset_text": str(preset_text or "")[:LLM_CHARS],
                    "note": str(note or "")[:200],
                    "status": "pending", "attempts": 0, "created_at": _now()}
            rows.append(item)
            _write_queue(rows)
        return {"ok": True, "duplicate": False, "id": item["id"], "status": "pending"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 文本抽取
def _normalize(text):
    text = re.sub(r"[ \t]+", " ", str(text or ""))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _mcp_doc_text(p):
    """复用 MCP 的文档解析器（含 zip-slip / 解压上限防护与 soffice 兜底）。"""
    mcp_root = str(ROOT / "mcp")
    if mcp_root not in sys.path:
        sys.path.insert(0, mcp_root)
    from opc_mcp.knowledge import extract_text
    text, _reason = extract_text(str(p))
    return _normalize(text)


def _doc_text(p):
    ext = p.suffix.lower()
    if ext in MCP_DOC_EXT:
        return _mcp_doc_text(p)
    if ext in TEXT_EXT:
        raw = p.read_text(encoding="utf-8", errors="replace")
        if ext in (".html", ".htm", ".xml"):
            raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
            raw = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        return _normalize(raw)
    if ext == ".docx":
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return _normalize(html.unescape(re.sub(r"<[^>]+>", "", xml)))
    if ext == ".pdf":
        exe = shutil.which("pdftotext")
        if not exe:
            return ""
        r = subprocess.run([exe, "-q", "-enc", "UTF-8", str(p), "-"],
                           capture_output=True, text=True, timeout=180)
        return _normalize(r.stdout)
    return ""


def _ocr(p):
    if not TESSERACT:
        return "", "未安装 tesseract"
    try:
        r = subprocess.run([TESSERACT, str(p), "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                           capture_output=True, timeout=180)
        text = _normalize(r.stdout.decode("utf-8", "replace"))
        return text, "" if text else "图片未识别出文字"
    except Exception as e:
        return "", f"OCR 失败：{type(e).__name__}"


def _wav_bytes(p):
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-i", str(p),
           "-t", str(ASR_SECONDS), "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"]
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    if r.returncode or not r.stdout:
        return b"", r.stderr.decode("utf-8", "replace")[-200:]
    return r.stdout, ""


def _media_meta(p, kind):
    if kind != "video":
        return ""
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height:format=duration",
                            "-of", "default=nw=1", str(p)],
                           capture_output=True, text=True, timeout=30)
        return _normalize(r.stdout).replace("\n", "，")[:300]
    except Exception:
        return ""


def extract(item):
    """返回 (正文, meta)。meta.kind ∈ doc/image/audio/video/other。"""
    path = item["path"]
    p = Path(path)
    ext = p.suffix.lower()
    preset = str(item.get("preset_text") or "").strip()
    kind = ("image" if ext in IMAGE_EXT else "audio" if ext in AUDIO_EXT
            else "video" if ext in VIDEO_EXT else "doc")
    meta = {"kind": kind, "detail": "", "degraded": False}
    text = ""
    if preset and len(preset) >= 8:
        text = preset
        meta["detail"] = "上游已转写"
    elif kind == "image":
        text, err = _ocr(p)
        meta["detail"] = err or "OCR 识别"
        meta["degraded"] = not text
    elif kind in ("audio", "video"):
        meta["detail"] = _media_meta(p, kind)
        wav, err = _wav_bytes(p)
        if wav:
            try:
                sys.path.insert(0, str(BASE_DIR))
                import voice_bridge
                text = voice_bridge.asr_transcribe(
                    base64.b64encode(wav).decode("ascii")) or ""
            except Exception as e:
                meta["detail"] += f"｜ASR 失败：{type(e).__name__}"
        else:
            meta["detail"] += f"｜抽音轨失败：{err}"
        meta["degraded"] = not text
    else:
        text = _doc_text(p)
        meta["degraded"] = not text
    return text[:LLM_CHARS], meta


# ---------------------------------------------------------------- LLM 卡片
def _chat_json(messages, max_tokens=700):
    body = json.dumps({"model": _LLM_MODEL, "messages": messages,
                       "temperature": 0.2, "max_tokens": max_tokens,
                       "stream": False}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(_LLM_BASE + "/chat/completions", data=body,
                                 method="POST", headers={"Content-Type": "application/json"})
    if _LLM_KEY:
        req.add_header("Authorization", "Bearer " + _LLM_KEY)
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.loads(resp.read().decode("utf-8", "replace"))
    content = out["choices"][0]["message"].get("content") or ""
    a, b = content.find("{"), content.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("模型未返回 JSON")
    raw = content[a:b + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 模型常在字符串值里塞裸换行：只转义字符串内部的控制字符，不动结构换行
        fixed, inside, escaped = [], False, False
        for ch in raw:
            if inside and not escaped and ch in "\r\n\t":
                fixed.append({"\r": "\\r", "\n": "\\n", "\t": "\\t"}[ch])
                continue
            fixed.append(ch)
            if ch == '"' and not escaped:
                inside = not inside
            escaped = ch == "\\" and not escaped
        return json.loads("".join(fixed))


def _fallback_card(item, text, meta):
    lines = [ln.strip(" -*\t|>") for ln in text.splitlines() if ln.strip()]
    lines = [re.sub(r"^#+\s*", "", ln) for ln in lines]
    ext = Path(item["path"]).suffix.lower().lstrip(".")
    return {"title": Path(item["filename"]).stem[:18],
            "summary": (" ".join(lines)[:200] or f"{ext.upper()} 素材，{meta['detail']}，待人工补充摘要"),
            "bullets": lines[:4] or [f"来源：{item['source']}", f"类型：{meta['kind']}"],
            "tags": [ext, item["source"], meta["kind"], "素材内化"], "degraded": True}


def make_card(item, text, meta):
    if not text:
        return _fallback_card(item, text, meta)
    try:
        card = _chat_json([
            {"role": "system", "content": "你是 OPC 公司的资料整理员。只输出 JSON，不要解释。"},
            {"role": "user", "content":
                "阅读素材识别结果，输出 JSON：{\"title\":\"≤18字标题\",\"summary\":\"120字内摘要\","
                "\"bullets\":[\"3-5条关键信息\"],\"tags\":[\"4-8个中文标签\"]}。\n\n"
                f"素材名：{item['filename']}\n来源：{item['source']}\n"
                f"类型：{meta['kind']}（{meta['detail']}）\n\n{text[:LLM_CHARS]}"},
        ])
        card.setdefault("degraded", False)
        return card
    except Exception as e:
        card = _fallback_card(item, text, meta)
        card["llm_error"] = f"{type(e).__name__}: {e}"[:200]
        return card


# ---------------------------------------------------------------- 内化 + 评分
def contentize(item, dry=False):
    text, meta = extract(item)
    card = make_card(item, text, meta) if not dry else _fallback_card(item, text, meta)
    title = f"内化·素材·{str(card.get('title') or Path(item['filename']).stem)[:40]}"
    # 文件名不能以「台账/凭据/拓扑」结尾，否则撞 server._vault 屏蔽规则
    title = re.sub(r"(台账|凭据|拓扑)$", "资料", title)
    tags = ["素材内化", item["source"], meta["kind"]] + [
        str(t) for t in (card.get("tags") or []) if str(t).strip()][:8]
    summary = str(card.get("summary") or "")[:400]
    if meta["detail"]:
        summary += f"（{meta['detail'][:120]}）"
    k = {"id": "kb_vault_mat_" + hashlib.sha1(item["path"].encode("utf-8")).hexdigest()[:10],
         "title": title, "category": "analysis", "sancai": "di",
         "tags": tags, "path": item["path"], "summary": summary,
         "sections": [str(b) for b in (card.get("bullets") or [])[:5]],
         "members": [], "mtime": _mtime(item["path"])}
    if dry:
        return {"kb_id": k["id"], "kind": meta["kind"], "chars": len(text),
                "title": title, "summary": summary, "dry_run": True}
    sys.path.insert(0, str(BASE_DIR))
    import vault_digest
    md = vault_digest.write_entry(k)
    return {"kb_id": k["id"], "kind": meta["kind"], "chars": len(text),
            "title": title, "summary": summary, "md": str(md),
            "degraded": bool(card.get("degraded") or meta["degraded"])}


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return time.time()


def apply_scores():
    """复用现有五维评分；kb_vault_mat_* + 「内化·素材」标题会被 asset_score 收进评分集。"""
    try:
        sys.path.insert(0, str(BASE_DIR))
        import asset_score
        scored = asset_score.run(apply=True, quiet=True, return_scored=True)
        return {k: {"grade": v.get("grade"), "score": v.get("score")}
                for k, v in (scored or {}).items()}
    except Exception as e:
        print(f"[content] 评分失败：{type(e).__name__}: {e}", flush=True)
        return {}


# ---------------------------------------------------------------- 汇报老板
def _feishu_target():
    """返回飞书 chat_id / open_id，优先显式配置，其次 secretary .env 的报表会话。"""
    env_target = os.environ.get("OPC_MATERIAL_FEISHU_TARGET", "").strip()
    if env_target:
        return env_target.split(":", 1)[-1] if env_target.startswith("feishu:") else env_target
    env_file = Path(os.environ.get("OPC_SECRETARY_HOME", "/opt/sefs/hermes/profiles/secretary")) / ".env"
    values = {}
    try:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    for key in ("FEISHU_REPORT_CHAT", "FEISHU_REPORT_USER"):
        if values.get(key):
            return values[key]
    allowed = values.get("FEISHU_ALLOWED_USERS", "").split(",")
    return next((u.strip() for u in allowed if u.strip()), "")


def _notify_secretary(text, quiet=False):
    try:
        req = urllib.request.Request(
            WORKBENCH_URL + "/api/notify/boss",
            data=json.dumps({"text": text, "level": "info", "voice": False,
                             "source": "material_content"}).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            ok = resp.status == 200
        if not quiet:
            print(f"[content] 小秘书汇报 {'OK' if ok else resp.status}", flush=True)
        return ok
    except Exception as e:
        print(f"[content] 小秘书汇报失败：{type(e).__name__}: {e}", flush=True)
        return False


def _notify_feishu(text, quiet=False):
    target = _feishu_target()
    if not target:
        if not quiet:
            print("[content] 未找到飞书投递目标，跳过", flush=True)
        return False
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    lark = shutil.which("lark-cli") or str(Path.home() / ".local/bin/lark-cli")
    if Path(lark).is_file():
        key = "--user-id" if target.startswith(("ou_", "on_")) else "--chat-id"
        cmd = [lark, "im", "+messages-send", "--as", "bot", "--markdown", text,
               key, target, "--idempotency-key",
               "material-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=45, env=env)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            pos = out.find("{")
            data = json.JSONDecoder().raw_decode(out[pos:])[0] if pos >= 0 else {}
            ok = bool(data.get("ok")) if isinstance(data, dict) else r.returncode == 0
            if not quiet and not ok:
                print(f"[content] 飞书汇报失败：{out[-300:]}", flush=True)
            return ok
        except Exception as e:
            print(f"[content] 飞书汇报失败：{type(e).__name__}: {e}", flush=True)
            return False
    hermes = shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")
    env["HERMES_HOME"] = os.environ.get("OPC_SECRETARY_HOME", "/opt/sefs/hermes/profiles/secretary")
    try:
        r = subprocess.run([hermes, "send", "--to", "feishu:" + target, "--quiet", text],
                           capture_output=True, text=True, timeout=60, env=env)
        ok = r.returncode == 0
        if not quiet and not ok:
            print(f"[content] 飞书汇报失败 rc={r.returncode}：{r.stderr[-200:]}", flush=True)
        return ok
    except Exception as e:
        print(f"[content] 飞书汇报失败：{type(e).__name__}: {e}", flush=True)
        return False


def report(done_rows, scores, quiet=False):
    """一批素材只发一条汇总，避免刷屏。"""
    if not done_rows:
        return
    lines = [f"老板，{len(done_rows)} 份新素材已内容化并入库："]
    for row in done_rows:
        res = row.get("result") or {}
        kb_id = res.get("kb_id", "")
        sc = scores.get(kb_id, {})
        grade = sc.get("grade") or ("降级登记" if res.get("degraded") else "待评分")
        score = sc.get("score")
        lines.append(f"· {row['filename']}｜{res.get('kind', '?')}｜{grade}"
                     + (f"（{score:.0f}分）" if isinstance(score, (int, float)) else "")
                     + f"｜{Path(res.get('md') or kb_id).name}")
        if res.get("summary"):
            lines.append(f"  摘要：{res['summary'][:90]}")
    lines.append("已写入知识库并更新知识图谱，可随时检索备用。")
    text = "\n".join(lines)[:1800]
    if NOTIFY_BOSS:
        _notify_secretary(text, quiet)
    if NOTIFY_FEISHU:
        _notify_feishu(text, quiet)


# ---------------------------------------------------------------- worker
def run_once(limit=10, dry=False, quiet=False):
    try:
        with _locked(blocking=False):
            rows = _read_queue()
            todo = [r for r in rows
                    if r.get("status") in ("pending", "failed") and int(r.get("attempts") or 0) < 3]
            todo = todo[:max(1, limit)]
            if not todo:
                return {"processed": 0, "pending": sum(
                    1 for r in rows if r.get("status") == "pending")}
            done = []
            for row in todo:
                row["attempts"] = int(row.get("attempts") or 0) + 1
                try:
                    res = contentize(row, dry=dry)
                    row["status"] = "done"
                    row["result"] = res
                    row["done_at"] = _now()
                    done.append(row)
                    if not quiet:
                        print(f"[content] + {row['filename']} → {res['kb_id']} "
                              f"[{res['kind']}/{res['chars']}字]", flush=True)
                except Exception as e:
                    row["status"] = "failed" if row["attempts"] >= 3 else "pending"
                    row["error"] = f"{type(e).__name__}: {e}"[:400]
                    if not quiet:
                        print(f"[content] ! {row['filename']} {row['error']}", flush=True)
            # 已完成条目留 7 天备查，之后从队列清理（知识库和 state 里仍有记录）
            cutoff = time.time() - 7 * 86400
            kept = []
            for row in rows:
                if row.get("status") == "done":
                    try:
                        if datetime.fromisoformat(row.get("done_at") or "").timestamp() < cutoff:
                            continue
                    except ValueError:
                        pass
                kept.append(row)
            _write_queue(kept)
    except BlockingIOError:
        return {"processed": 0, "busy": True}
    scores = {} if dry else apply_scores()
    if done and not dry:
        # 通知走独立线程：飞书网关偶发挂起不能拖住队列 worker 和状态落盘
        threading.Thread(target=report, args=(done, scores, quiet), daemon=True,
                         name="opc-material-notify").start()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    st = {}
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    st.update({"updated": _now(), "last_processed": len(done),
               "total_done": int(st.get("total_done") or 0) + len(done)})
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"processed": len(done), "failed": len(todo) - len(done)}


_worker = {"thread": None, "stop": threading.Event()}


def kick(interval=20):
    """Web 进程内常驻后台 worker：有新单 20 秒内完成内容化。"""
    t = _worker["thread"]
    if t is not None and t.is_alive():
        return
    _worker["stop"].clear()

    def loop():
        while not _worker["stop"].wait(max(5, interval)):
            try:
                run_once(limit=5, quiet=True)
            except Exception as e:
                print(f"[content] worker 异常：{type(e).__name__}: {e}", flush=True)

    t = threading.Thread(target=loop, daemon=True, name="opc-material-content")
    _worker["thread"] = t
    t.start()


def stats():
    rows = _read_queue()
    by = {}
    for r in rows:
        by[r.get("status", "?")] = by.get(r.get("status", "?"), 0) + 1
    return {"total": len(rows), "by_status": by, "state": json.loads(
        STATE.read_text(encoding="utf-8")) if STATE.is_file() else {}}


def main():
    ap = argparse.ArgumentParser(description="素材内容化：入队 / 后台内化 / 统计")
    ap.add_argument("--once", action="store_true", help="处理一轮队列（默认行为）")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true", help="只抽取和出卡片，不写库不通知")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--enqueue", metavar="PATH", help="把本地文件入队")
    ap.add_argument("--source", default="cli")
    ap.add_argument("--name", default="")
    a = ap.parse_args()
    if a.stats:
        print(json.dumps(stats(), ensure_ascii=False, indent=1))
        return
    if a.enqueue:
        print(json.dumps(enqueue(a.enqueue, filename=a.name, source=a.source), ensure_ascii=False))
        return
    out = run_once(limit=a.limit, dry=a.dry_run, quiet=a.quiet)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
