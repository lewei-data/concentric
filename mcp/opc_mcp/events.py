"""作业事件日志（跨进程，append-only JSONL）。

- MCP stdio 服务端与 9105 上传服务都把作业终态写到这里，格式统一；
- mcp-bridge tail 这份文件推 SSE/Webhook，并支持 `since` 补发；
- 每个事件带 event_id 与 phone_fp（手机号指纹，仅用于桥侧按账号路由），
  对外帧由 bridge 按白名单字段构造，不带手机号/指纹等 PII。

路径：`OPC_MCP_EVENTS_FILE` 可覆盖，默认 <DATA_DIR>/job_events.jsonl。
"""

import json
import os
import threading
import time
import uuid
from pathlib import Path

from . import config

_LOCK = threading.Lock()


def events_path():
    return Path(os.environ.get("OPC_MCP_EVENTS_FILE",
                               str(config.DATA_DIR / "job_events.jsonl")))


def append_event(event):
    """原子追加一条事件（单行 < 4KB，O_APPEND 写跨进程安全），返回带 event_id 的完整记录。"""
    rec = dict(event or {})
    rec.setdefault("event_id", uuid.uuid4().hex)
    rec.setdefault("at", time.time())
    try:
        path = events_path()
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
    except OSError:
        pass                                          # 事件日志失败不影响业务
    return rec


def read_new(offset=0):
    """从字节 offset 增量读取，返回 (records, new_offset)；文件被截短自动从头读。"""
    path = events_path()
    if not path.is_file():
        return [], offset
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(offset if offset <= size else 0)
            data = f.read()
            new_offset = f.tell()
    except OSError:
        return [], offset
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except json.JSONDecodeError:
            continue
    return out, new_offset


def read_since(since=0, phone_fp=None, cap=500):
    """读取 at >= since 且属于某账号指纹的事件（最新 cap 条），供 SSE 断线补发。"""
    recs, _ = read_new(0)
    out = [r for r in recs if float(r.get("at") or 0) >= float(since or 0)
           and (not phone_fp or r.get("phone_fp") == phone_fp)]
    return out[-cap:]
