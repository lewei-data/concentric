"""LLM 通道：OpenAI 兼容 chat/completions，纯标准库实现。"""

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

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


def chat(messages, temperature=0.3, max_tokens=1200, timeout=None):
    url = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {"model": config.LLM_MODEL, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if config.LLM_API_KEY:
        req.add_header("Authorization", "Bearer " + config.LLM_API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.LLM_TIMEOUT) as resp:
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


def chat_json(messages, **kwargs):
    """要求模型只回 JSON：剥掉可能的 ``` 围栏后再解析。"""
    text = chat(messages, **kwargs)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("模型未返回 JSON：%s" % text[:200])
    return json.loads(text[start:end + 1])


def probe(timeout=8):
    """真实探活：max_tokens=1 的极小 chat，确认模型有部署而非仅 HTTP 通。

    返回 (ok, detail, info)；info 含 status / retry_after_sec / cooldown_until。
    /models 在 auto-free 冷却期仍返回 200，所以不再用它判活。
    """
    started = time.monotonic()

    def elapsed():
        return int((time.monotonic() - started) * 1000)

    try:
        chat([{"role": "user", "content": "ping"}], max_tokens=1, timeout=timeout)
        return True, None, {"method": "chat", "status": 200, "latency_ms": elapsed()}
    except LLMError as e:
        info = {"method": "chat", "status": e.status, "retry_after_sec": e.retry_after_sec,
                "cooldown_until": e.cooldown_until, "latency_ms": elapsed()}
        detail = str(e)[:300]
        if e.retry_after_sec:
            detail += "；冷却约 %d 秒（%.1f 小时）" % (
                int(e.retry_after_sec), e.retry_after_sec / 3600)
        return False, detail, info
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e), {
            "method": "chat", "status": None, "latency_ms": elapsed()}
