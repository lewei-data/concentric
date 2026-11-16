#!/usr/bin/env python3
"""CTO P0-3 告警推送通道（2026-11-16 老板通道纠正版）。

通道分级（老板 2026-11-16 指令）：
  - 紧急档 level=critical → POST http://127.0.0.1:8848/api/notify/boss（小秘书语音播报，
    spoken=true+audio 非空才算「已播报」），并同步发内部邮件留痕；
    spoken=false → 降级纯邮件 + 在 ops/notify_banner.json 挂红色横幅（/api/ops 读取展示）。
  - 普通档 level=warning/info → 只走内部邮件（boss@mail.conring.cn 收件，cto 抄送）。
  - 禁止用 /api/delegate 做播报证据或反复触发秘书会话（kernel.py:3427-3435 mgmt_delegate
    只进事件总线，不写 chat_messages、不调 TTS、不 emit chat.message——不会响）。
    delegate 仅保留「给老板留文字话」用途，本模块不调用它。

去重与静默（两个通道统一管理）：
  - 指纹 = sha256(f"{source}|{level}|{归一化text}")[:16]；归一化 = 去掉时间戳样式的
    数字串与空白差异，同一告警反复检出视为同一条。
  - 静默窗 30 分钟：同指纹在窗口内任何通道都只发一次（--force 可越窗，仅供演练）。
  - 状态文件 ops/notify_state.json；发送失败不落指纹（下轮巡检可重试），
    失败记录追加 ops/notify_events.jsonl。

用法：
  python3 notify.py --level critical --source cto-disk --text "31.208 磁盘 92%…"
  或 from notify import notify; notify("critical", "cto-disk", "…")
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(BASE_DIR, ".."))
OPS_DIR = os.path.join(REPO_ROOT, "ops")
STATE_FILE = os.path.join(OPS_DIR, "notify_state.json")
EVENTS_FILE = os.path.join(OPS_DIR, "notify_events.jsonl")
BANNER_FILE = os.path.join(OPS_DIR, "notify_banner.json")

BOSS_NOTIFY_URL = "http://127.0.0.1:8848/api/notify/boss"
MAIL_MCP_URL = "http://127.0.0.1:9100/mcp"
MAIL_TO = "boss@mail.conring.cn"
MAIL_CC = "cto@mail.conring.cn"
MAIL_AGENT = "cto"
SILENCE_WINDOW_SEC = 30 * 60
HTTP_TIMEOUT = 60  # TTS 合成可能偏慢


def _now():
    return time.time()


def _iso(ts=None):
    return datetime.fromtimestamp(ts or _now(), TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _norm(text):
    t = re.sub(r"\d{4}-\d{2}-\d{2}[T ]?\d{0,2}:?\d{0,2}:?\d{0,2}", "#", str(text))
    t = re.sub(r"\b\d{1,3}:\d{2}(:\d{2})?\b", "#", t)   # 裸时间 hh:mm(:ss)
    t = re.sub(r"\d{6,}", "#", t)                        # 长数字串/时间戳样式
    return re.sub(r"\s+", " ", t).strip().lower()


def fingerprint(source, level, text):
    key = f"{source}|{level}|{_norm(text)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _dump(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _log_event(rec):
    os.makedirs(OPS_DIR, exist_ok=True)
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _http_json(url, payload, extra_headers=None, timeout=HTTP_TIMEOUT):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    headers.update(extra_headers or {})
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            sid = r.headers.get("mcp-session-id", "")
    except (URLError, HTTPError, OSError) as e:
        return None, str(e), ""
    # MCP streamable-http 可能回 SSE，取最后一个 data: 行
    for line in raw.splitlines():
        if line.startswith("data: "):
            raw = line[6:]
    try:
        return json.loads(raw), "", sid
    except Exception:
        return None, f"响应非 JSON：{raw[:200]}", sid


def send_voice(text, level, source):
    """紧急播报：/api/notify/boss。返回 (ok, spoken, audio, raw)。只有回环来源可调用。"""
    data, err, _ = _http_json(BOSS_NOTIFY_URL,
                              {"text": text[:300], "level": level, "source": source})
    if err or not isinstance(data, dict):
        return False, False, "", {"error": err or "empty"}
    ok = bool(data.get("ok"))
    spoken = ok and bool(data.get("spoken")) and bool(data.get("audio"))
    return ok, spoken, str(data.get("audio") or ""), data


def _mail_session():
    data, err, sid = _http_json(
        MAIL_MCP_URL,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "opc-notify", "version": "1.0"}}},
        timeout=10)
    if err or not sid:
        return None, err or "no session id"
    _http_json(MAIL_MCP_URL, {"jsonrpc": "2.0",
                              "method": "notifications/initialized"},
               {"mcp-session-id": sid}, timeout=10)
    return sid, ""


def send_mail(subject, body, level="info"):
    """内部邮件（MCP 9100，身份固定 cto）。返回 (ok, detail)。"""
    sid, err = _mail_session()
    if not sid:
        return False, f"mail MCP 会话失败：{err}"
    data, err, _ = _http_json(
        MAIL_MCP_URL,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "mail_send",
                    "arguments": {"to": MAIL_TO, "cc": MAIL_CC, "agent": MAIL_AGENT,
                                  "subject": subject[:200], "body": body[:50000]}}},
        {"mcp-session-id": sid}, timeout=30)
    if err:
        return False, f"mail MCP 调用失败：{err}"
    res = (data or {}).get("result") or {}
    if res.get("isError"):
        txt = "".join(c.get("text", "") for c in res.get("content") or [])
        return False, f"mail_send 报错：{txt[:200]}"
    return True, "sent"


def raise_banner(text, source, detail):
    _dump(BANNER_FILE, {"active": True, "at": _iso(), "source": source,
                        "text": text[:300], "detail": detail[:300]})


def clear_banner():
    st = _load(BANNER_FILE, {})
    if st.get("active"):
        _dump(BANNER_FILE, {"active": False, "clearedAt": _iso()})


def notify(level, source, text, force=False):
    """统一入口。紧急档=播报+邮件双通道；普通档=邮件。指纹+30min 静默窗跨通道生效。
    返回 dict：{"fp","suppressed"?,"voice"?,"mail"?,"banner"?,"at"}"""
    level = (level or "info").lower()
    fp = fingerprint(source, level, text)
    state: dict = _load(STATE_FILE, {})
    last = state.get(fp)
    if last and not force and _now() - float(last.get("at", 0)) < SILENCE_WINDOW_SEC:
        return {"fp": fp, "suppressed": True, "lastSent": last.get("iso")}

    body = f"级别：{level}\n来源：{source}\n时间：{_iso()}\n\n{text}"
    subj = f"【{'紧急' if level == 'critical' else '告警'}·{source}】{text[:80]}"
    out = {"fp": fp, "at": _iso()}
    mail_ok, mail_detail = False, ""
    voice = None

    if level == "critical":
        ok, spoken, audio, raw = send_voice(text, level, source)
        voice = {"ok": ok, "spoken": spoken, "audio": audio, "raw": raw}
        out["voice"] = voice
        if not spoken:
            raise_banner(text, source, f"notify/boss 失败：{raw}"[:300])
            out["banner"] = "raised(spoken=false→降级邮件+横幅)"
        else:
            clear_banner()
        mail_detail_prefix = ("（播报已成功，本邮件为留痕）" if spoken
                              else "（注意：语音播报失败，本邮件为降级主通道）")
        mail_ok, mail_detail = send_mail(subj, mail_detail_prefix + "\n\n" + body)
    else:
        mail_ok, mail_detail = send_mail(subj, body)
    out["mail"] = {"ok": mail_ok, "detail": mail_detail}

    # 至少一个通道成功才落指纹；全失败则保留下轮重试
    sent_ok = mail_ok or (voice or {}).get("spoken", False)
    if sent_ok:
        state[fp] = {"at": _now(), "iso": _iso(), "level": level, "source": source}
        state = {k: v for k, v in state.items()
                 if _now() - float(v.get("at", 0)) < 24 * 3600}
        _dump(STATE_FILE, state)
    _log_event({"at": _iso(), "fp": fp, "level": level, "source": source,
                "text": text[:200], "voice": voice, "mail": out.get("mail"),
                "force": force})
    return out


def main():
    ap = argparse.ArgumentParser(description="OPC CTO 告警推送（P0-3）")
    ap.add_argument("--level", default="info", choices=["info", "warning", "critical"])
    ap.add_argument("--source", required=True, help="如 cto-disk / cto-patrol / cto-bigfile")
    ap.add_argument("--text", required=True)
    ap.add_argument("--force", action="store_true", help="越出 30 分钟静默窗（演练用）")
    args = ap.parse_args()
    res: dict = notify(args.level, args.source, args.text, force=args.force)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if res.get("suppressed"):
        sys.exit(0)
    ok = res.get("mail", {}).get("ok") or res.get("voice", {}).get("spoken")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
