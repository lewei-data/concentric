#!/usr/bin/env python3
"""多渠道消息摄取：飞书 / APP（dispatch）→ opc.db 的 chat_threads / chat_messages。

原则（PROCESS-DATA-DB-PLAN.md §12.4）：
- 源库一律只读（mode=ro），不给 Hermes 网关打补丁。
- 轮询 30s，水位线存 ingest_cursor，只前进不回退；UNIQUE 约束兜底去重。
- 单次异常只记日志，不影响工作台主流程。
"""
import glob
import json
import os
import sqlite3
import threading
import time

import db as DB

PROFILES_DIR = os.environ.get("OPC_HERMES_PROFILES", "/opt/sefs/hermes/profiles")
DISPATCH_DB = os.environ.get("OPC_DISPATCH_DB", "/opt/sefs/hermes/dispatch/dispatch.db")
INTERVAL = int(os.environ.get("OPC_INGEST_INTERVAL", "8"))
BATCH = 500
VOICE_MARK = "voice message could not be transcribed"


def _ro(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def _log(msg):
    print(f"[ingest] {msg}", flush=True)


def _row(r, key, default=None):
    try:
        return r[key]
    except (IndexError, KeyError, TypeError):
        return default


def _modality_of(text):
    return "voice" if VOICE_MARK in str(text or "").lower() else "text"


def _emit(on_event, thread, msg, inserted):
    if inserted and on_event:
        try:
            on_event(thread, msg)
        except Exception as e:
            _log(f"事件推送失败：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 飞书
def feishu_dbs():
    found = []
    for path in sorted(glob.glob(os.path.join(PROFILES_DIR, "*", "state.db"))):
        try:
            conn = _ro(path)
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id=m.session_id "
                    "WHERE s.source='feishu'").fetchone()[0]
            finally:
                conn.close()
            if n:
                found.append((os.path.basename(os.path.dirname(path)), path))
        except sqlite3.Error:
            continue
    return found


def pull_feishu(store, on_event=None, full=False):
    total = 0
    for profile, path in feishu_dbs():
        table = f"messages:{profile}"
        watermark = 0 if full else store.get_cursor("feishu", table)
        try:
            conn = _ro(path)
        except sqlite3.Error as e:
            _log(f"打开飞书库失败 {path}: {e}")
            continue
        try:
            last_id = watermark
            while True:
                rows = conn.execute(
                    """SELECT m.id, m.session_id, m.role, m.content, m.timestamp,
                              m.platform_message_id, s.title AS session_title, s.user_id,
                              s.started_at
                       FROM messages m JOIN sessions s ON s.id=m.session_id
                       WHERE s.source='feishu' AND m.role IN ('user','assistant')
                         AND m.id > ? ORDER BY m.id LIMIT ?""",
                    (last_id, BATCH)).fetchall()
                if not rows:
                    break
                for r in rows:
                    mid = r["id"]
                    session_id = r["session_id"]
                    thread_id = f"feishu:{session_id}"
                    created = DB.ts_to_iso(r["timestamp"]) or DB.now_iso()
                    is_user = r["role"] == "user"
                    content = r["content"] or ""
                    title = r["session_title"] or "飞书会话"
                    store.chat_upsert_thread(
                        thread_id, "feishu", title,
                        [{"id": r["user_id"] or "", "name": title}],
                        DB.ts_to_iso(r["started_at"]) or created, created)
                    msg = {
                        "thread_id": thread_id, "source": "feishu",
                        "modality": _modality_of(content) if is_user else "text",
                        "direction": "inbound" if is_user else "outbound",
                        "sender_id": (r["user_id"] or "") if is_user else "assistant",
                        "sender_name": title if is_user else "小秘书",
                        "content": content,
                        "content_kind": "transcript" if _modality_of(content) == "voice" else "text",
                        "origin_table": f"state.messages[{profile}]",
                        "origin_id": f"{mid}",
                        "meta": {"platformMessageId": r["platform_message_id"],
                                 "role": r["role"], "profile": profile},
                        "created_at": created,
                    }
                    inserted = store.chat_insert(msg)
                    total += 1 if inserted else 0
                    _emit(on_event, thread_id, msg, inserted)
                    last_id = mid
                store.set_cursor("feishu", table, last_id)
                if len(rows) < BATCH:
                    break
        except sqlite3.Error as e:
            _log(f"飞书摄取失败 {path}：{e}")
        finally:
            conn.close()
    return total


# ---------------------------------------------------------------- APP / dispatch
def _dispatch_tasks(store, conn, on_event, full):
    table = "tasks"
    watermark = 0 if full else store.get_cursor("app", table)
    total, last = 0, watermark
    while True:
        rows = conn.execute(
            """SELECT rowid, task_id, priority, lane, source, modality, profile, text,
                      channel_id, status, created_at
               FROM tasks WHERE rowid > ? ORDER BY rowid LIMIT ?""",
            (last, BATCH)).fetchall()
        if not rows:
            break
        for r in rows:
            last = r["rowid"]
            profile = r["profile"] or "default"
            thread_id = f"app:{profile}"
            created = DB.ts_to_iso(r["created_at"]) or DB.now_iso()
            store.chat_upsert_thread(thread_id, "app", "APP · 小秘书", [{"id": "boss", "name": "老板"}],
                                     created, created)
            msg = {
                "thread_id": thread_id, "source": "app",
                "modality": r["modality"] or "text", "direction": "inbound",
                "sender_id": "boss", "sender_name": "老板",
                "content": r["text"] or "", "content_kind": "task",
                "origin_table": "dispatch.tasks", "origin_id": r["task_id"],
                "meta": {"taskId": r["task_id"], "priority": r["priority"],
                         "lane": r["lane"], "status": r["status"],
                         "channelId": r["channel_id"], "profile": profile},
                "created_at": created,
            }
            inserted = store.chat_insert(msg)
            total += 1 if inserted else 0
            _emit(on_event, thread_id, msg, inserted)
        store.set_cursor("app", table, last)
        if len(rows) < BATCH:
            break
    return total


def _dispatch_utterances(store, conn, on_event, full):
    table = "utterances"
    watermark = 0 if full else store.get_cursor("app", table)
    total, last = 0, watermark
    while True:
        rows = conn.execute(
            """SELECT u.id, u.task_id, u.channel_id, u.role, u.asr_text, u.reply_text,
                      u.voice_text, u.mode, u.ts, u.duration_ms, u.turn_no,
                      t.profile, t.modality AS task_modality
               FROM utterances u LEFT JOIN tasks t ON t.task_id = u.task_id
               WHERE u.id > ? ORDER BY u.id LIMIT ?""",
            (last, BATCH)).fetchall()
        if not rows:
            break
        for r in rows:
            last = r["id"]
            profile = r["profile"] or "default"
            thread_id = f"app:{profile}"
            created = DB.ts_to_iso(r["ts"]) or DB.now_iso()
            modality = r["task_modality"] or ("voice" if r["voice_text"] else "text")
            store.chat_upsert_thread(thread_id, "app", "APP · 小秘书", [{"id": "boss", "name": "老板"}],
                                     created, created)
            base_meta = {"utteranceId": r["id"], "taskId": r["task_id"], "mode": r["mode"],
                         "durationMs": r["duration_ms"], "turnNo": r["turn_no"],
                         "profile": profile, "voiceText": r["voice_text"]}
            pairs = [("in", "inbound", "boss", "老板", r["asr_text"], "transcript"),
                     ("out", "outbound", "assistant", "小秘书", r["reply_text"], "text")]
            for suffix, direction, sid, sname, content, ckind in pairs:
                if not content:
                    continue
                msg = {
                    "thread_id": thread_id, "source": "app", "modality": modality,
                    "direction": direction, "sender_id": sid, "sender_name": sname,
                    "content": content, "content_kind": ckind,
                    "origin_table": "dispatch.utterances",
                    "origin_id": f"{r['id']}:{suffix}", "meta": base_meta,
                    "created_at": created,
                }
                inserted = store.chat_insert(msg)
                total += 1 if inserted else 0
                _emit(on_event, thread_id, msg, inserted)
        store.set_cursor("app", table, last)
        if len(rows) < BATCH:
            break
    return total


def pull_dispatch(store, on_event=None, full=False):
    if not os.path.isfile(DISPATCH_DB):
        return 0
    try:
        conn = _ro(DISPATCH_DB)
    except sqlite3.Error as e:
        _log(f"打开 dispatch 库失败：{e}")
        return 0
    try:
        return (_dispatch_tasks(store, conn, on_event, full)
                + _dispatch_utterances(store, conn, on_event, full))
    except sqlite3.Error as e:
        _log(f"dispatch 摄取失败：{e}")
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------- 调度
class Ingestor:
    def __init__(self, store, on_event=None, interval=INTERVAL):
        self.store = store
        self.on_event = on_event
        self.interval = max(5, interval)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="opc-ingest")
        self._thread.start()
        return self

    def _loop(self):
        _log(f"已启动，间隔 {self.interval}s")
        while not self._stop.is_set():
            for name, fn in (("feishu", pull_feishu), ("dispatch", pull_dispatch)):
                try:
                    n = fn(self.store, self.on_event)
                    if n:
                        _log(f"{name} 新增 {n} 条")
                except Exception as e:
                    _log(f"{name} 轮询异常：{type(e).__name__}: {e}")
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


def jsonl(rows):
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
