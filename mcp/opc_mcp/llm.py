"""LLM 通道：OpenAI 兼容 chat/completions，纯标准库实现。"""

import hashlib
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

TZ = timezone(timedelta(hours=8))


class LLMError(RuntimeError):
    """LLM 调用失败；429 冷却信息结构化保留，供 kb_status / ask 共用。"""

    def __init__(self, message, status=None, retry_after_sec=None, detail=""):
        super().__init__(message)
        self.status = status
        self.retry_after_sec = retry_after_sec
        self.detail = detail
        self.cooldown_until = (
            (datetime.now(TZ) + timedelta(seconds=retry_after_sec)).isoformat(timespec="seconds")
            if retry_after_sec else None)


_RETRY_RE = re.compile(
    r"(?:try\s*again\s*in|retry[_-]?after['\":=\s]*|冷却)\s*(\d+(?:\.\d+)?)\s*"
    r"(seconds?|secs?|s\b|minutes?|mins?|m\b|hours?|hrs?|h\b)?", re.I)


def _cooldown_seconds(text, headers=None):
    """从 Retry-After 头或错误正文解析冷却秒数（兼容 JSON 字段与 Try again in N seconds）。"""
    if headers:
        try:
            raw = headers.get("Retry-After") or headers.get("retry-after")
            if raw is not None:
                return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    s = str(text or "")
    m = re.search(r"['\"]?retry[_-]?after['\"]?\s*:\s*(\d+(?:\.\d+)?)", s, re.I)
    if m:
        return max(0.0, float(m.group(1)))
    m = _RETRY_RE.search(s)
    if not m:
        return None
    n, unit = float(m.group(1)), (m.group(2) or "s").lower()
    if unit.startswith(("m", "min")):
        n *= 60
    elif unit.startswith(("h", "hr")):
        n *= 3600
    return max(0.0, n)


def _session_id(model, messages):
    """OpenCode 网关要求 x-opencode-session；按模型+首条用户消息稳定派生，便于上游缓存。"""
    first = ""
    for m in messages:
        if m.get("role") == "user":
            first = str(m.get("content") or "")[:512]
            break
    seed = "%s|%s" % (model, first)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _post(url, model, api_key, messages, temperature, max_tokens, timeout):
    payload = {"model": model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    if "opencode.ai" in url:
        req.add_header("x-opencode-session", _session_id(model, messages))
        req.add_header("User-Agent", "opencode/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        retry = _cooldown_seconds(detail, e.headers)
        raise LLMError("LLM HTTP %s：%s" % (e.code, detail[:300]),
                       status=e.code, retry_after_sec=retry, detail=detail) from e
    except Exception as e:
        raise LLMError("LLM 不可达（%s）：%s" % (url, e)) from e
    try:
        content = body["choices"][0]["message"].get("content")
        return (content or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise LLMError("LLM 响应缺 choices/content：%s" % str(body)[:300]) from e


_FALLBACK_CACHE = {"at": 0.0, "cfg": None}


def fallback_config(force=False):
    """兜底通道配置；key 优先环境变量，其次模型管理页的 opencode 通道（60 秒缓存）。"""
    if not config.LLM_FALLBACK_ENABLED:
        return None
    if not force and time.monotonic() - _FALLBACK_CACHE["at"] < 60:
        return _FALLBACK_CACHE["cfg"]
    url = str(config.LLM_FALLBACK_URL or "").strip()
    model = str(config.LLM_FALLBACK_MODEL or "").strip()
    key = str(config.LLM_FALLBACK_API_KEY or "").strip()
    cid = str(config.LLM_FALLBACK_CHANNEL_ID or "").strip()
    db = Path(config.LLM_FALLBACK_DB)
    if cid and db.is_file():
        try:
            c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3)
            row = c.execute("SELECT name,url,api_key,status FROM llm_models WHERE id=?",
                            (cid,)).fetchone()
            c.close()
            if row and str(row[0] or "") != "disabled":
                model = model or str(row[0] or "")
                url = url or str(row[1] or "")
                key = key or str(row[2] or "")
        except sqlite3.Error:
            pass
    cfg = {"id": cid or "fallback", "url": url, "model": model, "key": key} \
        if (url and model and key) else None
    _FALLBACK_CACHE.update({"at": time.monotonic(), "cfg": cfg})
    return cfg


def _should_fallback(err):
    """429/5xx/超时/网络错误才切换；400/401/403 这类请求或凭据问题不兜底。"""
    return err.status is None or err.status in (408, 409, 425, 429) or (err.status or 0) >= 500


def chat(messages, temperature=0.3, max_tokens=1200, timeout=None, allow_fallback=True):
    primary = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    try:
        return _post(primary, config.LLM_MODEL, config.LLM_API_KEY, messages,
                     temperature, max_tokens, timeout or config.LLM_TIMEOUT)
    except LLMError as e:
        if not allow_fallback or not _should_fallback(e):
            raise
        cfg = fallback_config()
        if not cfg:
            raise
        try:
            answer = _post(cfg["url"].rstrip("/") + "/chat/completions", cfg["model"],
                           cfg["key"], messages, temperature, max_tokens,
                           timeout or config.LLM_FALLBACK_TIMEOUT)
        except LLMError as e2:
            raise LLMError("%s；兜底通道失败：%s" % (e, e2), status=e2.status,
                           retry_after_sec=e2.retry_after_sec, detail=e2.detail) from e2
        print("[llm] 主通道失败(status=%s)，已切兜底 %s" % (e.status, cfg["id"]), flush=True)
        return answer


def chat_json(messages, **kwargs):
    """要求模型只回 JSON：剥掉可能的 ``` 围栏后再解析。"""
    text = chat(messages, **kwargs)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("模型未返回 JSON：%s" % text[:200])
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        # 统一包成 LLMError，让调用方（如素材卡片生成）走降级路径而不是整单失败
        raise LLMError("模型返回的 JSON 无法解析：%s（%s）" % (e, text[:120]))


def _probe_one(url, model, key, timeout):
    started = time.monotonic()

    def elapsed():
        return int((time.monotonic() - started) * 1000)

    try:
        _post(url.rstrip("/") + "/chat/completions", model, key,
              [{"role": "user", "content": "ping"}], 0.0, 1, timeout)
        return True, None, {"method": "chat", "status": 200, "model": model,
                            "latency_ms": elapsed()}
    except LLMError as e:
        info = {"method": "chat", "status": e.status, "model": model,
                "retry_after_sec": e.retry_after_sec,
                "cooldown_until": e.cooldown_until, "latency_ms": elapsed()}
        detail = str(e)[:300]
        if e.retry_after_sec:
            detail += "；冷却约 %d 秒（%.1f 小时）" % (
                int(e.retry_after_sec), e.retry_after_sec / 3600)
        return False, detail, info
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e), {
            "method": "chat", "status": None, "model": model, "latency_ms": elapsed()}


def probe(timeout=8):
    """真实探活：主通道 + 兜底通道各打一次 max_tokens=1。

    返回 (ok, detail, info)；ok=任一通道可用。info 保留主通道字段以兼容旧调用，
    并额外给出 primary / fallback 明细，便于 kb_status 判断当前实际走哪条。
    """
    p_ok, p_detail, p_info = _probe_one(config.LLM_BASE_URL, config.LLM_MODEL,
                                        config.LLM_API_KEY, timeout)
    cfg = fallback_config()
    f_info = {"configured": bool(cfg), "channel": (cfg or {}).get("id")}
    f_ok, f_detail = None, None
    if not p_ok and cfg:
        f_ok, f_detail, f_info = _probe_one(cfg["url"], cfg["model"], cfg["key"], timeout)
        f_info["channel"] = cfg["id"]
    ok = bool(p_ok or f_ok)
    info = dict(p_info)
    info["primary"] = p_info
    info["fallback"] = f_info
    if f_ok and not p_ok:
        info.update({"status": 200, "retry_after_sec": None, "cooldown_until": None,
                     "channel": cfg["id"], "model": cfg["model"]})
    else:
        info["channel"] = "primary" if p_ok else ""
    if ok:
        detail = None
    else:
        detail = p_detail + ("；兜底：" + (f_detail or "不可用") if cfg else "")
    return ok, detail, info


_PROBE_CACHE = {"at": 0.0, "ok": None, "detail": None, "info": None}
_PROBE_LOCK = threading.Lock()


def probe_cached(ttl=60, timeout=8, force=False):
    """带缓存的探活：kb_ping/面板轮询用，避免每次都打上游模型。

    ttl 秒内的重复调用直接回缓存；force=true 忽略缓存立即重探。
    """
    with _PROBE_LOCK:
        if (not force and _PROBE_CACHE["ok"] is not None
                and time.monotonic() - _PROBE_CACHE["at"] < max(5, ttl)):
            return _PROBE_CACHE["ok"], _PROBE_CACHE["detail"], _PROBE_CACHE["info"]
    ok, detail, info = probe(timeout)
    with _PROBE_LOCK:
        _PROBE_CACHE.update({"at": time.monotonic(), "ok": ok,
                             "detail": detail, "info": info})
    return ok, detail, info
