#!/usr/bin/env python3
"""语音桥接客户端：把本机 mcp-asr（9102）/ mcp-tts（9101）的 MCP streamable-HTTP
服务包成两个简单函数，供消息中心（小秘书对话）做 语音转文字 / 文字转语音。

  asr_transcribe(audio_b64)   —— whisper 转写（wav/mp3/ogg/flac/webm 均可），纯转写无副作用
  tts_synthesize(text, out)   —— CosyVoice 合成，wav 落盘；失败返回 ""（前端回退浏览器朗读）

桥接协议（/opt/sefs/mcp-bridge/bridge.py）：streamable-HTTP，先 initialize 拿
Mcp-Session-Id，再 notifications/initialized，之后 tools/call；响应可能是 SSE
（data: 行）或纯 JSON，这里统一解析。
"""

import json
import os
import urllib.error
import urllib.request

ASR_URL = os.environ.get("MCP_ASR_URL", "http://127.0.0.1:9102/mcp")
TTS_URL = os.environ.get("MCP_TTS_URL", "http://127.0.0.1:9101/mcp")
_TIMEOUT = 120


class McpHttpError(Exception):
    pass


class McpHttpClient:
    """最小 MCP streamable-HTTP 客户端：每个实例持有一个会话。"""

    def __init__(self, url):
        self.url = url
        self.session = ""

    def _post(self, payload, timeout=_TIMEOUT):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
            if sid:
                self.session = sid
            return resp.read().decode("utf-8", "replace")

    @staticmethod
    def _extract(raw):
        """响应可能是 SSE（data: 行）或纯 JSON，取第一条 data 载荷。"""
        if not raw:
            return None
        if "data:" in raw:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    break
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def _ensure_session(self):
        if self.session:
            return
        init = self._extract(self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "opc-workbench", "version": "3.0"}}}, timeout=20))
        if init is None or "result" not in init:
            raise McpHttpError("MCP initialize 失败")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)

    def call_tool(self, tool, args, timeout=_TIMEOUT, request_id=2):
        self._ensure_session()
        raw = self._post({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}}, timeout=timeout)
        d = self._extract(raw)
        if not d or "result" not in d:
            err = (d or {}).get("error", {})
            raise McpHttpError(f"MCP tools/call 失败: {err.get('message') or '无结果'}")
        if d["result"].get("isError"):
            texts = [c.get("text", "") for c in d["result"].get("content", [])
                     if c.get("type") == "text"]
            raise McpHttpError(f"MCP 工具返回错误: {'; '.join(texts)[:200]}")
        return d["result"].get("content", [])

    @staticmethod
    def _content_texts(content):
        """content 块 → (文本列表, 音频 base64 列表)。"""
        texts, audios = [], []
        for c in content or []:
            if c.get("type") == "text":
                texts.append(c.get("text", ""))
            elif c.get("type") in ("audio", "resource"):
                data = c.get("data") or ""
                if not data and isinstance(c.get("resource"), dict):
                    data = c["resource"].get("blob") or ""
                if data:
                    audios.append(data)
        return texts, audios


def _client(url):
    return McpHttpClient(url)


def asr_transcribe(audio_b64, language="zh", timeout=_TIMEOUT):
    """音频 base64 → 转写文本；失败抛 McpHttpError。"""
    c = _client(ASR_URL)
    content = c.call_tool("asr_transcribe",
                          {"audio_base64": audio_b64, "language": language},
                          timeout=timeout, request_id=10)
    texts, _ = McpHttpClient._content_texts(content)
    text = "\n".join(t for t in texts if t.strip()).strip()
    # 桥接返回的是 JSON 字符串（text/language/segments…），解出转写文本
    if text.startswith("{"):
        try:
            j = json.loads(text)
            text = str(j.get("text") or "")
        except ValueError:
            pass
    return text


def tts_synthesize(text, out_wav, voice="", timeout=_TIMEOUT):
    """文本 → wav 落盘；返回 wav 路径或 ""（失败不抛，前端回退浏览器朗读）。"""
    try:
        c = _client(TTS_URL)
        args = {"text": text, "return_audio": True}
        if voice:
            args["voice"] = voice
        content = c.call_tool("tts_synthesize", args, timeout=timeout, request_id=11)
        texts, audios = McpHttpClient._content_texts(content)
        blob = audios[0] if audios else ""
        if not blob:
            joined = "\n".join(texts)
            if joined.startswith("{"):
                try:
                    j = json.loads(joined)
                    blob = j.get("audio_base64") or j.get("audio") or ""
                except ValueError:
                    blob = ""
        if not blob:
            return ""
        if blob.startswith("data:"):
            blob = blob.split(",", 1)[-1]
        import base64
        os.makedirs(os.path.dirname(out_wav) or ".", exist_ok=True)
        with open(out_wav, "wb") as f:
            f.write(base64.b64decode(blob))
        return out_wav
    except (McpHttpError, OSError, ValueError, urllib.error.URLError) as e:
        print(f"[voice] TTS 失败：{type(e).__name__}: {e}", flush=True)
        return ""
