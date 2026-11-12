#!/usr/bin/env python3
"""OPC 过程数据层：SQLite(WAL) 落库 + 缓冲批写。

设计要点（见 PROCESS-DATA-DB-PLAN.md §3/§4/§12.3）：
- 单写连接（check_same_thread=False）+ 进程内锁；读用独立只读连接。
- 普通写入进缓冲，200ms 或 200 条触发一次事务，避免每事件一次 fsync。
- 所有异常只记日志，绝不向上抛：写库失败时主流程照旧走内存/JSON。
- 开关：OPC_DB=1 启用；路径 OPC_DB_PATH（默认与本文件同目录 opc.db）。
"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("OPC_DB_PATH", os.path.join(BASE_DIR, "opc.db"))
ENABLED = os.environ.get("OPC_DB", "0") == "1"
TZ = timezone(timedelta(hours=8))
FLUSH_MS = 200
FLUSH_ROWS = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS traces (
  trace_id       TEXT PRIMARY KEY,
  hotspot_id     TEXT,
  hotspot_title  TEXT,
  status         TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  closed_at      TEXT,
  abort_reason   TEXT,
  review_gate    TEXT,
  review_history TEXT,
  hermes         TEXT,
  engine         TEXT,
  meta           TEXT
);
CREATE INDEX IF NOT EXISTS idx_traces_status_time ON traces(status, started_at DESC);

CREATE TABLE IF NOT EXISTS trace_events (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id TEXT,
  kind     TEXT NOT NULL,
  at       TEXT NOT NULL,
  data     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_trace ON trace_events(trace_id, id);
CREATE INDEX IF NOT EXISTS idx_events_time  ON trace_events(at);

CREATE TABLE IF NOT EXISTS messages (
  id         TEXT PRIMARY KEY,
  trace_id   TEXT,
  type       TEXT,
  sender     TEXT,
  recipient  TEXT,
  project_id TEXT,
  priority   INTEGER,
  at         TEXT NOT NULL,
  payload    TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_time ON messages(at DESC);

CREATE TABLE IF NOT EXISTS approvals (
  id          TEXT PRIMARY KEY,
  trace_id    TEXT,
  action      TEXT,
  risk        TEXT,
  status      TEXT NOT NULL,
  approver    TEXT,
  created_at  TEXT NOT NULL,
  decided_at  TEXT,
  decided_by  TEXT,
  timeout_sec INTEGER,
  items       TEXT,
  raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_appr_status ON approvals(status, created_at DESC);

CREATE TABLE IF NOT EXISTS reports (
  id         TEXT PRIMARY KEY,
  trace_id   TEXT,
  kind       TEXT,
  title      TEXT,
  conclusion TEXT,
  evidence   TEXT,
  decisions  TEXT,
  engine     TEXT,
  created_at TEXT,
  raw        TEXT
);

CREATE TABLE IF NOT EXISTS meetings (
  id             TEXT PRIMARY KEY,
  trace_id       TEXT,
  title          TEXT,
  status         TEXT,
  started_at     TEXT,
  ended_at       TEXT,
  conclusion     TEXT,
  participants   TEXT,
  transcript_url TEXT,
  minutes_url    TEXT,
  raw            TEXT
);

CREATE TABLE IF NOT EXISTS meeting_speeches (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  speaker_id TEXT,
  speaker    TEXT,
  role       TEXT,
  kind       TEXT,
  text       TEXT,
  at         TEXT,
  UNIQUE(meeting_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_speech_meeting ON meeting_speeches(meeting_id, seq);

CREATE TABLE IF NOT EXISTS hotspots (
  id           TEXT PRIMARY KEY,
  trace_id     TEXT,
  title        TEXT,
  source       TEXT,
  url          TEXT,
  score        REAL,
  tags         TEXT,
  collected_at TEXT,
  raw          TEXT
);

CREATE TABLE IF NOT EXISTS knowledge (
  id         TEXT PRIMARY KEY,
  title      TEXT,
  path       TEXT,
  tags       TEXT,
  updated_at TEXT,
  raw        TEXT
);

CREATE TABLE IF NOT EXISTS contents (
  id          TEXT PRIMARY KEY,
  trace_id    TEXT,
  project_id  TEXT,
  stage       TEXT,
  title       TEXT,
  owner       TEXT,
  tone_profile TEXT,
  created_at  TEXT,
  raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_trace ON contents(trace_id);

CREATE TABLE IF NOT EXISTS token_usage (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id          TEXT,
  agent             TEXT,
  model             TEXT,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  total_tokens      INTEGER,
  cost              REAL,
  at                TEXT
);
CREATE INDEX IF NOT EXISTS idx_token_trace ON token_usage(trace_id);

CREATE TABLE IF NOT EXISTS runs (
  run_id     TEXT PRIMARY KEY,
  pid        INTEGER,
  started_at TEXT,
  ended_at   TEXT,
  version    TEXT
);

CREATE TABLE IF NOT EXISTS stats_daily (
  day                  TEXT PRIMARY KEY,
  traces               INTEGER,
  published            INTEGER,
  blocked              INTEGER,
  approval_seconds_avg REAL,
  events               INTEGER
);

-- ---------- §12.3 多渠道统一消息（飞书 / APP / Web） ----------
CREATE TABLE IF NOT EXISTS chat_threads (
  id           TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  title        TEXT,
  participants TEXT,
  created_at   TEXT NOT NULL,
  last_at      TEXT,
  msg_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ct_source ON chat_threads(source, last_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id    TEXT NOT NULL,
  source       TEXT NOT NULL,
  modality     TEXT NOT NULL,
  direction    TEXT NOT NULL,
  sender_id    TEXT,
  sender_name  TEXT,
  content      TEXT,
  content_kind TEXT NOT NULL DEFAULT 'text',
  origin_table TEXT,
  origin_id    TEXT,
  trace_id     TEXT,
  reply_to     INTEGER,
  asr          TEXT,
  meta         TEXT,
  created_at   TEXT NOT NULL,
  ingested_at  TEXT NOT NULL,
  UNIQUE(source, origin_table, origin_id)
);
CREATE INDEX IF NOT EXISTS idx_cm_thread ON chat_messages(thread_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_cm_time   ON chat_messages(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cm_src    ON chat_messages(source, modality, id DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts USING fts5(
  content, content='chat_messages', content_rowid='id', tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS chat_messages_ai AFTER INSERT ON chat_messages BEGIN
  INSERT INTO chat_messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS chat_messages_ad AFTER DELETE ON chat_messages BEGIN
  INSERT INTO chat_messages_fts(chat_messages_fts, rowid, content)
  VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS chat_messages_au AFTER UPDATE ON chat_messages BEGIN
  INSERT INTO chat_messages_fts(chat_messages_fts, rowid, content)
  VALUES ('delete', old.id, old.content);
  INSERT INTO chat_messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS ingest_cursor (
  source     TEXT NOT NULL,
  table_name TEXT NOT NULL,
  watermark  INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY(source, table_name)
);
"""


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def ts_to_iso(value):
    """epoch 秒（float）→ ISO+08；非法值原样返回空串。"""
    try:
        return datetime.fromtimestamp(float(value), TZ).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def jdump(value):
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _log(msg):
    print(f"[db] {msg}", flush=True)


class Store:
    """单写连接 + 缓冲批写。所有方法对异常静默（只记日志）。"""

    def __init__(self, path=DB_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._batch = []
        self._cv = threading.Condition()
        self._stop = False
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()

    # ---------- 基础设施 ----------
    def _init_schema(self):
        with self._lock:
            for pragma in ("journal_mode=WAL", "synchronous=NORMAL",
                           "busy_timeout=5000", "foreign_keys=ON"):
                try:
                    self._conn.execute(f"PRAGMA {pragma}")
                except sqlite3.Error as e:
                    _log(f"PRAGMA {pragma} 失败：{e}")
            self._conn.executescript(SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_version(version) VALUES (1)")
            self._conn.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _flush_loop(self):
        while True:
            with self._cv:
                if not self._batch and not self._stop:
                    self._cv.wait(timeout=FLUSH_MS / 1000)
                batch = self._batch
                self._batch = []
                stop = self._stop
            if batch:
                self._write(batch)
            if stop and not batch:
                return

    def _write(self, batch):
        with self._lock:
            try:
                with self._conn:
                    for sql, params in batch:
                        try:
                            self._conn.execute(sql, params)
                        except sqlite3.Error as e:
                            _log(f"写入失败 {sql[:60]}：{e}")
            except sqlite3.Error as e:
                _log(f"批量事务失败（{len(batch)} 条）：{e}")

    def put(self, sql, params=()):
        try:
            with self._cv:
                self._batch.append((sql, tuple(params)))
                if len(self._batch) >= FLUSH_ROWS:
                    self._cv.notify()
        except Exception as e:
            _log(f"入队失败：{type(e).__name__}: {e}")

    def flush(self):
        """同步刷盘，供摄取水位线/验收使用。"""
        with self._cv:
            batch = self._batch
            self._batch = []
        if batch:
            self._write(batch)

    def close(self):
        with self._cv:
            self._stop = True
            self._cv.notify()
        try:
            self._thread.join(timeout=3)
        except RuntimeError:
            pass
        self.flush()
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
            except sqlite3.Error as e:
                _log(f"关闭连接失败：{e}")

    def read_conn(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def scalar(self, sql, params=()):
        try:
            conn = self.read_conn()
            try:
                row = conn.execute(sql, params).fetchone()
                return (row[0] if row else 0)
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"查询失败 {sql[:60]}：{e}")
            return 0

    # ---------- 过程主轴 ----------
    def upsert_trace(self, t):
        if not t:
            return
        try:
            self.put("""INSERT INTO traces(trace_id,hotspot_id,hotspot_title,status,started_at,
                        closed_at,abort_reason,review_gate,review_history,hermes,engine,meta)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(trace_id) DO UPDATE SET
                          status=excluded.status, closed_at=excluded.closed_at,
                          abort_reason=excluded.abort_reason, review_gate=excluded.review_gate,
                          review_history=excluded.review_history, hermes=excluded.hermes,
                          engine=excluded.engine, meta=excluded.meta""",
                     (t.get("traceId"), (t.get("hotspot") or {}).get("id"),
                      (t.get("hotspot") or {}).get("title"), t.get("status") or "running",
                      t.get("startedAt") or now_iso(), t.get("closedAt") or None,
                      t.get("abortReason") or None,
                      jdump(t.get("reviewGate")) if t.get("reviewGate") is not None else None,
                      jdump(t.get("reviewHistory")) if t.get("reviewHistory") is not None else None,
                      jdump(t.get("hermes")) if t.get("hermes") is not None else None,
                      t.get("engine"), jdump(t.get("meta")) if t.get("meta") is not None else None))
        except Exception as e:
            _log(f"upsert_trace 失败：{type(e).__name__}: {e}")

    def append_event(self, kind, trace_id, data, at=None):
        self.put("INSERT INTO trace_events(trace_id,kind,at,data) VALUES(?,?,?,?)",
                 (trace_id or None, kind, at or now_iso(), jdump(data)))

    def insert_message(self, m):
        try:
            self.put("""INSERT OR IGNORE INTO messages(id,trace_id,type,sender,recipient,
                        project_id,priority,at,payload) VALUES(?,?,?,?,?,?,?,?,?)""",
                     (m.get("id"), m.get("traceId") or None, m.get("type"), m.get("sender"),
                      m.get("recipient"), m.get("projectId"), m.get("priority"),
                      m.get("at") or now_iso(), jdump(m.get("payload"))))
        except Exception as e:
            _log(f"insert_message 失败：{type(e).__name__}: {e}")

    def upsert_approval(self, a):
        try:
            self.put("""INSERT INTO approvals(id,trace_id,action,risk,status,approver,created_at,
                        decided_at,decided_by,timeout_sec,items,raw)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                          status=excluded.status, decided_at=excluded.decided_at,
                          decided_by=excluded.decided_by, raw=excluded.raw""",
                     (a.get("id"), a.get("traceId") or None, a.get("action"), a.get("risk"),
                      a.get("status") or "pending", a.get("approver"),
                      a.get("createdAt") or now_iso(), a.get("decidedAt"), a.get("decidedBy"),
                      _int(a.get("timeoutSec")), jdump(a.get("items")), jdump(a)))
        except Exception as e:
            _log(f"upsert_approval 失败：{type(e).__name__}: {e}")

    def upsert_report(self, r):
        try:
            self.put("""INSERT INTO reports(id,trace_id,kind,title,conclusion,evidence,decisions,
                        engine,created_at,raw) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET raw=excluded.raw""",
                     (r.get("id"), r.get("traceId") or None, r.get("kind"), r.get("title"),
                      r.get("conclusion"), jdump(r.get("evidence")), jdump(r.get("decisions")),
                      r.get("engine"), r.get("createdAt") or now_iso(), jdump(r)))
        except Exception as e:
            _log(f"upsert_report 失败：{type(e).__name__}: {e}")

    def upsert_hotspot(self, h):
        try:
            self.put("""INSERT INTO hotspots(id,trace_id,title,source,url,score,tags,collected_at,raw)
                        VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET raw=excluded.raw""",
                     (h.get("id"), h.get("traceId") or None, h.get("title"), h.get("source"),
                      h.get("url"), _float(h.get("score")), jdump(h.get("tags")),
                      h.get("at") or h.get("collectedAt") or now_iso(), jdump(h)))
        except Exception as e:
            _log(f"upsert_hotspot 失败：{type(e).__name__}: {e}")

    def upsert_knowledge(self, k):
        try:
            self.put("""INSERT INTO knowledge(id,title,path,tags,updated_at,raw)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET title=excluded.title,path=excluded.path,
                          tags=excluded.tags,updated_at=excluded.updated_at,raw=excluded.raw""",
                     (k.get("id"), k.get("title"), k.get("path"), jdump(k.get("tags")),
                      k.get("updatedAt") or k.get("at") or now_iso(), jdump(k)))
        except Exception as e:
            _log(f"upsert_knowledge 失败：{type(e).__name__}: {e}")

    def upsert_content(self, c):
        try:
            self.put("""INSERT INTO contents(id,trace_id,project_id,stage,title,owner,
                        tone_profile,created_at,raw) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET stage=excluded.stage, raw=excluded.raw""",
                     (c.get("id"), c.get("traceId"), c.get("projectId"), c.get("stage"),
                      c.get("title"), c.get("owner"), c.get("toneProfile"),
                      c.get("createdAt") or now_iso(), jdump(c)))
        except Exception as e:
            _log(f"upsert_content 失败：{type(e).__name__}: {e}")

    def upsert_meeting(self, rec, seq_base=0):
        try:
            self.put("""INSERT INTO meetings(id,trace_id,title,status,started_at,ended_at,conclusion,
                        participants,transcript_url,minutes_url,raw)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                          ended_at=excluded.ended_at, conclusion=excluded.conclusion,
                          transcript_url=excluded.transcript_url,
                          minutes_url=excluded.minutes_url, raw=excluded.raw""",
                     (rec.get("id"), rec.get("traceId") or None, rec.get("title"),
                      rec.get("status"), rec.get("startedAt"), rec.get("endedAt"),
                      rec.get("conclusion"), jdump(rec.get("participants")),
                      rec.get("transcriptUrl"), rec.get("minutesUrl"), jdump(rec)))
        except Exception as e:
            _log(f"upsert_meeting 失败：{type(e).__name__}: {e}")

    def add_speech(self, meeting_id, seq, sp):
        try:
            self.put("""INSERT OR IGNORE INTO meeting_speeches(meeting_id,seq,speaker_id,speaker,
                        role,kind,text,at) VALUES(?,?,?,?,?,?,?,?)""",
                     (meeting_id, seq, sp.get("speakerId"), sp.get("speaker"), sp.get("role"),
                      sp.get("kind"), sp.get("text"), sp.get("at")))
        except Exception as e:
            _log(f"add_speech 失败：{type(e).__name__}: {e}")

    def insert_usage(self, trace_id, agent, model, usage):
        usage = usage or {}
        try:
            self.put("""INSERT INTO token_usage(trace_id,agent,model,prompt_tokens,
                        completion_tokens,total_tokens,cost,at) VALUES(?,?,?,?,?,?,?,?)""",
                     (trace_id or None, agent, model, _int(usage.get("inputTokens")),
                      _int(usage.get("outputTokens")), _int(usage.get("totalTokens")),
                      _float(usage.get("costUsd")), now_iso()))
        except Exception as e:
            _log(f"insert_usage 失败：{type(e).__name__}: {e}")

    def run_start(self, run_id, pid, version):
        self.put("""INSERT OR REPLACE INTO runs(run_id,pid,started_at,ended_at,version)
                    VALUES(?,?,?,NULL,?)""", (run_id, pid, now_iso(), version))

    def run_end(self, run_id):
        self.put("UPDATE runs SET ended_at=? WHERE run_id=?", (now_iso(), run_id))

    # ---------- 统一消息 ----------
    def chat_upsert_thread(self, tid, source, title, participants, created_at, last_at):
        """立即写入：chat_insert 是同步事务，线程行必须先落地（msg_count 才能累加）。"""
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        """INSERT INTO chat_threads(id,source,title,participants,created_at,last_at)
                           VALUES(?,?,?,?,?,?)
                           ON CONFLICT(id) DO UPDATE SET title=COALESCE(excluded.title,title),
                             participants=COALESCE(excluded.participants,participants),
                             last_at=CASE WHEN excluded.last_at > COALESCE(last_at,'')
                                          THEN excluded.last_at ELSE last_at END""",
                        (tid, source, title, jdump(participants), created_at, last_at))
            except sqlite3.Error as e:
                _log(f"chat_upsert_thread 失败：{e}")

    def chat_insert(self, msg):
        """立即写入（摄取用），返回是否新插入。"""
        with self._lock:
            try:
                with self._conn:
                    cur = self._conn.execute(
                        """INSERT OR IGNORE INTO chat_messages(thread_id,source,modality,
                           direction,sender_id,sender_name,content,content_kind,origin_table,
                           origin_id,trace_id,reply_to,asr,meta,created_at,ingested_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (msg["thread_id"], msg["source"], msg.get("modality") or "text",
                         msg.get("direction") or "inbound", msg.get("sender_id"),
                         msg.get("sender_name"), msg.get("content"),
                         msg.get("content_kind") or "text", msg.get("origin_table"),
                         str(msg.get("origin_id")), msg.get("trace_id"), msg.get("reply_to"),
                         msg.get("asr"), jdump(msg.get("meta")), msg["created_at"], now_iso()))
                    if cur.rowcount:
                        self._conn.execute(
                            """UPDATE chat_threads SET msg_count=msg_count+1,
                               last_at=CASE WHEN ? > COALESCE(last_at,'') THEN ? ELSE last_at END
                               WHERE id=?""",
                            (msg["created_at"], msg["created_at"], msg["thread_id"]))
                    return bool(cur.rowcount)
            except sqlite3.Error as e:
                _log(f"chat_insert 失败：{e}")
                return False

    def get_cursor(self, source, table_name):
        return _int(self.scalar(
            "SELECT watermark FROM ingest_cursor WHERE source=? AND table_name=?",
            (source, table_name)))

    def set_cursor(self, source, table_name, watermark):
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        """INSERT INTO ingest_cursor(source,table_name,watermark,updated_at)
                           VALUES(?,?,?,?)
                           ON CONFLICT(source,table_name) DO UPDATE SET
                             watermark=excluded.watermark, updated_at=excluded.updated_at""",
                        (source, table_name, _int(watermark), now_iso()))
            except sqlite3.Error as e:
                _log(f"set_cursor 失败：{e}")

    # ---------- 读取（API 用） ----------
    def chat_threads(self, source="", limit=50, before=""):
        sql = "SELECT * FROM chat_threads WHERE 1=1"
        params = []
        if source:
            sql += " AND source=?"
            params.append(source)
        if before:
            sql += " AND last_at < ?"
            params.append(before)
        sql += " ORDER BY COALESCE(last_at,created_at) DESC LIMIT ?"
        params.append(max(1, min(int(limit or 50), 500)))
        conn = self.read_conn()
        try:
            rows = [dict(r) for r in conn.execute(sql, params)]
            for r in rows:
                try:
                    r["participants"] = json.loads(r.get("participants") or "[]")
                except ValueError:
                    pass
            return rows
        finally:
            conn.close()

    def chat_messages(self, thread="", source="", modality="", direction="", q="",
                      before_id=0, limit=50):
        limit = max(1, min(int(limit or 50), 500))
        where, params = ["1=1"], []
        if q:
            if len(q) >= 3:
                sql = """SELECT m.* FROM chat_messages_fts f
                         JOIN chat_messages m ON m.id=f.rowid
                         WHERE chat_messages_fts MATCH ?"""
                params.append('"' + q.replace('"', '') + '"')
            else:
                # trigram 索引最短 3 字：更短的查询退回 LIKE，避免“两字查不到”
                sql = "SELECT m.* FROM chat_messages m WHERE m.content LIKE ?"
                params.append(f"%{q}%")
        else:
            sql = "SELECT m.* FROM chat_messages m WHERE 1=1"
        if thread:
            where.append("m.thread_id=?")
            params.append(thread)
        if source:
            where.append("m.source=?")
            params.append(source)
        if modality:
            where.append("m.modality=?")
            params.append(modality)
        if direction:
            where.append("m.direction=?")
            params.append(direction)
        if before_id:
            where.append("m.id<?")
            params.append(int(before_id))
        sql += " AND " + " AND ".join(where) + " ORDER BY m.id DESC LIMIT ?"
        params.append(limit)
        conn = self.read_conn()
        try:
            rows = [dict(r) for r in conn.execute(sql, params)]
            for r in rows:
                for key in ("asr", "meta"):
                    if r.get(key):
                        try:
                            r[key] = json.loads(r[key])
                        except ValueError:
                            pass
            return rows
        finally:
            conn.close()

    def chat_stats(self, days=7):
        days = max(1, min(int(days or 7), 90))
        cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat(timespec="seconds")
        conn = self.read_conn()
        try:
            by_source = [dict(r) for r in conn.execute(
                "SELECT source, COUNT(*) AS n FROM chat_messages GROUP BY source ORDER BY n DESC")]
            by_modality = [dict(r) for r in conn.execute(
                "SELECT source, modality, COUNT(*) AS n FROM chat_messages GROUP BY 1,2")]
            by_day = [dict(r) for r in conn.execute(
                """SELECT substr(created_at,1,10) AS day, source, COUNT(*) AS n
                   FROM chat_messages WHERE created_at >= ?
                   GROUP BY day, source ORDER BY day DESC""", (cutoff,))]
            return {"bySource": by_source, "byModality": by_modality, "byDay": by_day}
        finally:
            conn.close()

    def chat_export(self, start="", end="", limit=10000):
        sql = "SELECT * FROM chat_messages WHERE 1=1"
        params = []
        if start:
            sql += " AND created_at>=?"
            params.append(start)
        if end:
            sql += " AND created_at<=?"
            params.append(end)
        sql += " ORDER BY id LIMIT ?"
        params.append(max(1, min(int(limit or 10000), 20000)))
        conn = self.read_conn()
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def trace_row(self, trace_id):
        conn = self.read_conn()
        try:
            row = conn.execute("SELECT * FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def trace_events(self, trace_id, limit=500):
        conn = self.read_conn()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id,kind,at,data FROM trace_events WHERE trace_id=? ORDER BY id LIMIT ?",
                (trace_id, max(1, min(int(limit), 2000))))]
            for r in rows:
                try:
                    r["data"] = json.loads(r.get("data") or "null")
                except ValueError:
                    pass
            return rows
        finally:
            conn.close()

    def table_counts(self):
        tables = ["traces", "trace_events", "messages", "approvals", "reports", "hotspots",
                  "knowledge", "contents", "meetings", "meeting_speeches", "token_usage",
                  "chat_threads", "chat_messages"]
        return {t: self.scalar(f"SELECT COUNT(*) FROM {t}") for t in tables}


def _int(value):
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


_STORE = None
_STORE_LOCK = threading.Lock()


def store():
    """进程级单例；OPC_DB 未开启时返回 None。"""
    global _STORE
    if not ENABLED:
        return None
    with _STORE_LOCK:
        if _STORE is None:
            try:
                _STORE = Store()
                _log(f"已启用，库文件 {DB_PATH}")
            except Exception as e:
                _log(f"初始化失败，降级为旧路径：{type(e).__name__}: {e}")
                _STORE = None
    return _STORE
