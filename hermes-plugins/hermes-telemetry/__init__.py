"""hermes-telemetry: 把 Hermes 运行时事件追加写入 spool/，由 telemetryd.py 汇总进 DuckDB。

设计约束（为什么这么写）：
  * 网关进程里不能做重活：hook 回调只做 dict 组装 + 入队，耗时微秒级。
  * pre_tool_call 是 fail-closed 钩子：本插件任何异常都必须被吞掉，否则会阻断工具调用。
  * 不回传任何值（返回 None），不改变 agent 行为。
  * 不在此处连 DuckDB：DuckDB 单写者、会与查询抢锁，落库交给独立进程 telemetryd.py。
  * 不记录正文：只记录长度/数量/耗时/状态等元数据，避免把消息内容写进遥测库。

spool 格式：每行一个 JSON：
  {"ts": "<iso8601>", "epoch": 1.23, "kind": "post_tool_call", "profile": "default",
   "pid": 123, "data": {...}}
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SPOOL_DIR = Path(os.environ.get("HERMES_TELEMETRY_SPOOL", "/opt/sefs/hermes/telemetry/spool"))
MAX_QUEUE = 20000
FLUSH_INTERVAL = 1.0
FLUSH_BATCH = 500

HOOKS = (
    "pre_tool_call", "post_tool_call",
    "pre_llm_call", "post_llm_call",
    "pre_api_request", "post_api_request", "api_request_error",
    "on_session_start", "on_session_end", "on_session_finalize",
    "subagent_start", "subagent_stop",
    "on_kanban_dispatch_tick",
    "kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked",
    "on_kanban_worker_spawned", "on_kanban_worker_exited",
)

# 这些字段是正文/大对象：只记长度或数量，不落内容。
_BIG_TEXT = {
    "user_message", "assistant_message", "assistant_response", "assistant_content",
    "result", "tool_result", "response", "llm_response", "error_body", "output", "text",
    "system_prompt",
}
_BIG_COLLECTION = {
    "args", "conversation_history", "api_messages", "request_messages", "messages",
    "tool_calls", "tools", "skills", "attachments", "result_obj", "payload",
    "request", "middleware_trace",
}
_MAX_STR = 240
# usage 是扁平的数值桶（prompt_tokens/completion_tokens/...），单独展开。
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
               "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")

_queue: "queue.Queue[dict]" = queue.Queue(maxsize=MAX_QUEUE)
_writer_lock = threading.Lock()
_writer_started = False
_dropped = 0
_written = 0


# --------------------------------------------------------------- 序列化
def _scalar(value):
    """把任意值压成可 JSON 序列化的标量；大对象折算成大小。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + f"…({len(value)})"
    if isinstance(value, (list, tuple, set, dict)):
        try:
            return len(value)
        except Exception:
            return None
    return str(value)[:_MAX_STR]


def _size(value) -> int:
    """只算长度/条目数；不对未知对象做序列化，避免在 hook 里付出高代价。"""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return 0


def _profile() -> str:
    # 网关是多 profile 复用的；current_profile_name 会读 HERMES_HOME 覆盖（contextvar），
    # 能正确区分「为哪个 profile 服务的这一轮」。
    try:
        from hermes_cli.profiles import current_profile_name
        return current_profile_name(default="default") or "default"
    except Exception:
        return os.environ.get("HERMES_PROFILE") or "default"


def _shape(kwargs: dict) -> dict:
    data: dict = {}
    for key, value in kwargs.items():
        if key == "usage" and isinstance(value, dict):
            for name in _USAGE_KEYS:
                if name in value:
                    data[f"usage_{name}"] = _scalar(value[name])
        elif key == "error" and isinstance(value, dict):
            data["error_type"] = _scalar(value.get("type"))
            data["error_message"] = _scalar(value.get("message"))
        elif key in _BIG_TEXT:
            data[f"{key}_chars"] = _size(value)
        elif key in _BIG_COLLECTION:
            data[f"{key}_count"] = _size(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            data[key] = _scalar(value)
        else:
            # DispatchResult 之类的 dataclass：展开标量字段（spawned/skipped_locked…）
            try:
                import dataclasses
                if dataclasses.is_dataclass(value):
                    for name, sub in dataclasses.asdict(value).items():
                        if isinstance(sub, (str, int, float, bool)) or sub is None:
                            data[f"{key}_{name}"] = _scalar(sub)
                        elif isinstance(sub, (list, tuple, dict, set)):
                            data[f"{key}_{name}_count"] = len(sub)
                    continue
            except Exception:
                pass
            data[key] = f"<{type(value).__name__}>"
    return data


# --------------------------------------------------------------- 写入线程
def _current_spool() -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H")
    return SPOOL_DIR / f"events-{stamp}-{os.getpid()}.jsonl"


def _writer_loop() -> None:
    buf: list[str] = []
    last = time.monotonic()
    global _written
    while True:
        timeout = max(0.05, FLUSH_INTERVAL - (time.monotonic() - last))
        try:
            item = _queue.get(timeout=timeout)
            buf.append(item["line"])
        except queue.Empty:
            pass
        if not buf:
            last = time.monotonic()
            continue
        if len(buf) < FLUSH_BATCH and (time.monotonic() - last) < FLUSH_INTERVAL:
            continue
        try:
            SPOOL_DIR.mkdir(parents=True, exist_ok=True)
            with open(_current_spool(), "a", encoding="utf-8") as fh:
                fh.write("".join(buf))
            _written += len(buf)
        except Exception:
            pass  # 遥测失败绝不影响主流程
        buf.clear()
        last = time.monotonic()


def _ensure_writer() -> None:
    global _writer_started
    if _writer_started:
        return
    with _writer_lock:
        if _writer_started:
            return
        threading.Thread(target=_writer_loop, name="hermes-telemetry-writer", daemon=True).start()
        atexit.register(_flush_now)
        _writer_started = True


def _flush_now() -> None:
    """atexit：尽力把队列里的行写完（daemon 线程此时可能已被回收）。"""
    lines = []
    while True:
        try:
            lines.append(_queue.get_nowait()["line"])
        except queue.Empty:
            break
    if not lines:
        return
    try:
        SPOOL_DIR.mkdir(parents=True, exist_ok=True)
        with open(_current_spool(), "a", encoding="utf-8") as fh:
            fh.write("".join(lines))
    except Exception:
        pass


# --------------------------------------------------------------- hook 入口
def _emit(kind: str, kwargs: dict) -> None:
    global _dropped
    try:
        record = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "epoch": round(time.time(), 3),
            "kind": kind,
            "profile": _profile(),
            "pid": os.getpid(),
            "data": _shape(kwargs),
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    except Exception:
        return
    _ensure_writer()
    try:
        _queue.put_nowait({"line": line})
    except queue.Full:
        _dropped += 1  # 宁可丢遥测，也不阻塞对话


def _make(kind: str):
    def _hook(**kwargs):
        # 绝不抛异常：pre_tool_call 抛异常会被 fail-closed 当成「阻断工具」。
        try:
            _emit(kind, kwargs)
        except Exception:
            pass
        return None
    _hook.__name__ = f"telemetry_{kind}"
    return _hook


_hooks = {name: _make(name) for name in HOOKS}


def register(ctx) -> None:
    for name, callback in _hooks.items():
        ctx.register_hook(name, callback)


def stats() -> dict:
    return {"queued": _queue.qsize(), "written": _written, "dropped": _dropped}


__all__ = ["register", "stats"]
