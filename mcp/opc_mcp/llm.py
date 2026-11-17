"""LLM 通道：OpenAI 兼容 chat/completions，纯标准库实现。"""

import json
import urllib.error
import urllib.request

from . import config


class LLMError(RuntimeError):
    pass


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
        detail = e.read().decode("utf-8", "replace")[:300]
        raise LLMError("LLM HTTP %s：%s" % (e.code, detail)) from e
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
    """轻量探活：列模型，避免真的烧 token。"""
    url = config.LLM_BASE_URL.rstrip("/") + "/models"
    req = urllib.request.Request(url)
    if config.LLM_API_KEY:
        req.add_header("Authorization", "Bearer " + config.LLM_API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.status
    except Exception as e:
        return False, str(e)[:200]
