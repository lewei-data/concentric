#!/usr/bin/env python3
"""OPC 过程数据层：SQLite(WAL) 落库 + 缓冲批写。

设计要点（见 PROCESS-DATA-DB-PLAN.md §3/§4/§12.3）：
- 单写连接（check_same_thread=False）+ 进程内锁；读用独立只读连接。
- 普通写入进缓冲，200ms 或 200 条触发一次事务，避免每事件一次 fsync。
- 所有异常只记日志，绝不向上抛：写库失败时主流程照旧走内存/JSON。
- 开关：OPC_DB=1 启用（默认开启）；路径 OPC_DB_PATH（默认与本文件同目录 opc.db）。
  流水线运行态/审批/内容已按“写库为准、内存为缓存”使用，设 OPC_DB=0 可整体退回内存模式。
"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("OPC_DB_PATH", os.path.join(BASE_DIR, "opc.db"))
ENABLED = os.environ.get("OPC_DB", "1") == "1"
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

-- 流水线运行态：一条 trace 一行，stage/state 用于进程重启后从断点续跑
CREATE TABLE IF NOT EXISTS pipeline_runs (
  trace_id    TEXT PRIMARY KEY,
  status      TEXT NOT NULL,
  stage       TEXT,
  stage_index INTEGER,
  hotspot     TEXT,
  state       TEXT,
  started_at  TEXT,
  updated_at  TEXT,
  closed_at   TEXT,
  resume_tries INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status, updated_at DESC);

-- 发布回执：断点续跑重跑 release 阶段时，按 (trace, 内容单) 判重，避免重复发布/重复计数
CREATE TABLE IF NOT EXISTS release_ledger (
  trace_id     TEXT NOT NULL,
  item_id      TEXT NOT NULL,
  project_id   TEXT,
  title        TEXT,
  published_at TEXT,
  PRIMARY KEY (trace_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_release_ledger_trace ON release_ledger(trace_id);


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

-- ---------- 大模型注册表：系统可用模型配置与 token 额度（CTO 运维开关） ----------
-- status: enabled（启用）/ disabled（停用）/ exhausted（额度耗尽，自动标记）
-- total_tokens=0 表示不限额；可用 = total - used
-- alias: Hermes 调用该通道时附加的 --model 别名（空=走内置默认模型）
-- priority: 通道选择优先级，数字越小越优先（默认通道优先于慢速本地通道）
-- provider/api_key: 把通道写入 profile config.yaml 时使用（api_key 不对前端展示）
-- ---------- 制片工厂：CCO 工艺决策 + 车间工位过程（以 OpenMontage 13 类工艺为基准） ----------
-- 每条内容单（产品）由 CCO 参考.OpenMontage 13 类工艺确定制作工艺，工艺决定产品进哪个车间；
-- 同一 trace+内容单只保留最新决策（重跑制作评估时覆盖）。
CREATE TABLE IF NOT EXISTS product_crafts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id    TEXT,
  item_id     TEXT,
  serial_no   TEXT,
  project_id  TEXT,
  title       TEXT,
  workshop_no TEXT NOT NULL,
  craft_id    TEXT NOT NULL,
  craft_name  TEXT,
  product     TEXT,
  render_mode TEXT,
  reason      TEXT,
  decided_by  TEXT NOT NULL DEFAULT 'cco',
  created_at  TEXT NOT NULL,
  updated_at  TEXT,
  shipped_at  TEXT,
  steps       TEXT,
  UNIQUE(trace_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_craft_trace    ON product_crafts(trace_id);
CREATE INDEX IF NOT EXISTS idx_craft_workshop ON product_crafts(workshop_no);

-- 车间工位过程：每条产品（trace）在每个流水线环节上的实时加工记录（阶段边界写档）
CREATE TABLE IF NOT EXISTS workshop_steps (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id    TEXT NOT NULL,
  stage_id    TEXT NOT NULL,
  stage_name  TEXT,
  om_stage    TEXT,
  status      TEXT NOT NULL,
  detail      TEXT,
  started_at  TEXT,
  finished_at TEXT,
  UNIQUE(trace_id, stage_id)
);
CREATE INDEX IF NOT EXISTS idx_ws_trace ON workshop_steps(trace_id, id);

CREATE TABLE IF NOT EXISTS llm_models (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  url          TEXT,
  protocol     TEXT,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  used_tokens  INTEGER NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'enabled',
  alias        TEXT NOT NULL DEFAULT '',
  priority     INTEGER NOT NULL DEFAULT 100,
  provider     TEXT NOT NULL DEFAULT '',
  api_key      TEXT NOT NULL DEFAULT '',
  note         TEXT,
  updated_at   TEXT,
  updated_by   TEXT
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
            # 老库补列：续跑尝试次数，用于给反复失败的断点设上限（避免每次重启无限重试）
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(pipeline_runs)")}
            if "resume_tries" not in cols:
                self._conn.execute("ALTER TABLE pipeline_runs "
                                   "ADD COLUMN resume_tries INTEGER NOT NULL DEFAULT 0")
            # 老库补列：流水线工单流水号（CCO 审批通过进流水线时分配）
            # 无流水号的历史记录按要求清除（没有车间工单的老任务不再保留）
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(product_crafts)")}
            if cols and "serial_no" not in cols:
                self._conn.execute("ALTER TABLE product_crafts ADD COLUMN serial_no TEXT")
                self._conn.execute("DELETE FROM product_crafts "
                                   "WHERE serial_no IS NULL OR serial_no=''")
            # 老库补列：出厂时间（质检审核通过 → 出厂，工厂段流水结束）
            if cols and "shipped_at" not in cols:
                self._conn.execute("ALTER TABLE product_crafts ADD COLUMN shipped_at TEXT")
            # 老库补列：工艺步骤序列快照（CCO 定工艺时定格，每个工艺的工序不同）
            if cols and "steps" not in cols:
                self._conn.execute("ALTER TABLE product_crafts ADD COLUMN steps TEXT")
            # 老库补列：大模型注册表的 hermes 别名与优先级（通道选择/故障转移用）
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(llm_models)")}
            if cols:
                for col, ddl in (("alias", "TEXT NOT NULL DEFAULT ''"),
                                 ("priority", "INTEGER NOT NULL DEFAULT 100"),
                                 ("provider", "TEXT NOT NULL DEFAULT ''"),
                                 ("api_key", "TEXT NOT NULL DEFAULT ''")):
                    if col not in cols:
                        self._conn.execute(f"ALTER TABLE llm_models ADD COLUMN {col} {ddl}")
                # 既有登记行补默认值（只补未设置过的，不覆盖 CTO 手工调整）
                self._conn.execute("UPDATE llm_models SET alias='qwen38' "
                                   "WHERE id='qwen38-local' AND alias=''")
                self._conn.execute("UPDATE llm_models SET priority=10 "
                                   "WHERE id='hermes-default' AND priority=100")
                self._conn.execute("UPDATE llm_models SET priority=50 "
                                   "WHERE id='qwen38-local' AND priority=100")
                self._conn.execute("UPDATE llm_models SET provider='opencode-go' "
                                   "WHERE id='hermes-default' AND (provider IS NULL OR provider='')")
                self._conn.execute(
                    "UPDATE llm_models SET provider='custom', "
                    "api_key='sk-litellm-15bbcf2ea58c156130a00afd1b43ae4a' "
                    "WHERE id='qwen38-local' AND (provider IS NULL OR provider='')")
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

    # ---------- 流水线运行态（断点续跑） ----------
    def pipeline_run_start(self, trace_id, hotspot):
        """开一条流水线运行记录；同一 trace 重复调用只重置状态，不覆盖热点。"""
        self.put("""INSERT INTO pipeline_runs(trace_id,status,stage,stage_index,hotspot,state,
                    started_at,updated_at,closed_at) VALUES(?,?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(trace_id) DO UPDATE SET status=excluded.status,
                      closed_at=NULL, updated_at=excluded.updated_at""",
                 (trace_id, "running", None, 0, jdump(hotspot), None, now_iso(), now_iso()))
        self.flush()      # 断点必须落盘：进程被 kill 时不能丢

    def pipeline_run_stage(self, trace_id, stage, index, state):
        """阶段边界写档：stage 是"即将执行"的阶段，重启后从这里重跑（至少一次语义）。"""
        self.put("""UPDATE pipeline_runs SET status='running', stage=?, stage_index=?, state=?,
                    updated_at=? WHERE trace_id=?""",
                 (stage, _int(index), jdump(state), now_iso(), trace_id))
        self.flush()      # 每轮至多 16 次，代价可忽略

    def pipeline_run_end(self, trace_id, status="completed"):
        self.put("""UPDATE pipeline_runs SET status=?, closed_at=?, updated_at=? WHERE trace_id=?""",
                 (status, now_iso(), now_iso(), trace_id))
        self.flush()

    def pipeline_runs(self, status=""):
        """按状态列出流水线运行记录（最近在前）。"""
        sql = "SELECT * FROM pipeline_runs"
        params = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY updated_at DESC LIMIT 50"
        return self._rows(sql, params)

    def pipeline_runs_recent(self, limit=12):
        """最近的流水线运行记录（含断点阶段），供运维页展示与续跑入口。"""
        return self._rows("SELECT * FROM pipeline_runs ORDER BY updated_at DESC LIMIT ?",
                          (_int(limit),))

    def pipeline_run(self, trace_id):
        rows = self._rows("SELECT * FROM pipeline_runs WHERE trace_id=?", (trace_id,))
        return rows[0] if rows else None

    def pipeline_runs_resumable(self, max_tries=3):
        """可自动续跑的运行记录：仍有断点阶段，且续跑次数未超上限。

        running（上次进程直接挂掉）与 interrupted（已标记但当时没续成）都算，
        这样多断点、续跑被抢占/失败的情况下次重启还能再试。
        """
        return self._rows("""SELECT * FROM pipeline_runs
                             WHERE status IN ('running','interrupted')
                               AND stage IS NOT NULL AND stage != ''
                               AND COALESCE(resume_tries,0) < ?
                             ORDER BY updated_at ASC""", (_int(max_tries),))

    def pipeline_resume_try(self, trace_id):
        """记一次续跑尝试：给反复失败的断点设上限，避免每次重启都无意义重试。"""
        self.put("""UPDATE pipeline_runs SET resume_tries=COALESCE(resume_tries,0)+1, updated_at=?
                    WHERE trace_id=?""", (now_iso(), trace_id))
        self.flush()

    def release_mark(self, trace_id, item_id, project_id="", title=""):
        """登记发布回执，返回 True 表示首次登记（应真正发布）。

        写失败时返回 True：宁可重复一次（旧行为），也不能把该发的内容吞掉。
        """
        if not trace_id or not item_id:
            return False
        try:
            with self._lock:
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO release_ledger
                       (trace_id,item_id,project_id,title,published_at) VALUES(?,?,?,?,?)""",
                    (trace_id, item_id, project_id or "", title or "", now_iso()))
                self._conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as e:
            _log(f"发布回执写入失败（按未发布处理）：{e}")
            return True

    def released_items(self, trace_id):
        """某 trace 已登记发布的内容单 id 集合（幂等判重）。"""
        rows = self._rows("SELECT item_id FROM release_ledger WHERE trace_id=?", (trace_id,))
        return {r.get("item_id") for r in rows if r.get("item_id")}

    # ---------- 制片工厂（CCO 工艺决策 + 车间工位过程） ----------
    def alloc_serial(self):
        """分配流水线工单流水号：WO-YYYYMMDD-NNN（当日递增）。

        只在 CCO 审批通过（确定工艺/车间）后领取。写库为批量异步刷盘，
        读库可能看不到刚发出的号，因此用进程内计数器兜底，两者取大后递增。
        """
        day = datetime.now(TZ).strftime("%Y%m%d")
        seq = getattr(self, "_serial_seq", 0) if getattr(self, "_serial_day", "") == day else 0
        try:
            conn = self.read_conn()
            try:
                for (s,) in conn.execute(
                        "SELECT serial_no FROM product_crafts WHERE serial_no LIKE ?",
                        (f"WO-{day}-%",)):
                    try:
                        seq = max(seq, int(str(s).rsplit("-", 1)[-1]))
                    except ValueError:
                        pass
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"alloc_serial 查询失败：{e}")
        seq += 1
        self._serial_day, self._serial_seq = day, seq
        return f"WO-{day}-{seq:03d}"

    def upsert_product_craft(self, d):
        """CCO 工艺决策落库：同一 trace+内容单覆盖更新，只留最新决策。"""
        try:
            self.put("""INSERT INTO product_crafts(trace_id,item_id,serial_no,project_id,title,
                        workshop_no,craft_id,craft_name,product,render_mode,reason,decided_by,
                        created_at,updated_at,steps)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(trace_id,item_id) DO UPDATE SET
                          serial_no=excluded.serial_no, workshop_no=excluded.workshop_no,
                          craft_id=excluded.craft_id, craft_name=excluded.craft_name,
                          product=excluded.product, render_mode=excluded.render_mode,
                          reason=excluded.reason, decided_by=excluded.decided_by,
                          updated_at=excluded.updated_at,
                          steps=COALESCE(excluded.steps, steps)""",
                     (d.get("traceId") or None, d.get("itemId") or None, d.get("serialNo") or "",
                      d.get("projectId") or "", d.get("title") or "", d.get("workshopNo") or "",
                      d.get("craftId") or "", d.get("craftName") or "", d.get("product") or "",
                      d.get("renderMode") or "", d.get("reason") or "",
                      d.get("decidedBy") or "cco",
                      d.get("createdAt") or now_iso(), now_iso(),
                      jdump(d["steps"]) if d.get("steps") is not None else None))
        except Exception as e:
            _log(f"upsert_product_craft 失败：{type(e).__name__}: {e}")

    def mark_shipped(self, trace_id, item_id):
        """质检审核通过 → 出厂：工厂段流水到此结束（记录出厂时间）。"""
        try:
            self.put("""UPDATE product_crafts SET shipped_at=?
                        WHERE trace_id=? AND item_id=? AND (shipped_at IS NULL OR shipped_at='')""",
                     (now_iso(), trace_id, item_id))
        except Exception as e:
            _log(f"mark_shipped 失败：{type(e).__name__}: {e}")

    def workshop_step_start(self, trace_id, stage_id, stage_name, om_stage):
        """进入车间工位：阶段开始（重跑同阶段时重置为 running）。"""
        try:
            self.put("""INSERT INTO workshop_steps(trace_id,stage_id,stage_name,om_stage,status,
                        detail,started_at,finished_at) VALUES(?,?,?,?,?,NULL,?,NULL)
                        ON CONFLICT(trace_id,stage_id) DO UPDATE SET status='running',
                          stage_name=excluded.stage_name, om_stage=excluded.om_stage,
                          detail=NULL, started_at=excluded.started_at, finished_at=NULL""",
                     (trace_id, stage_id, stage_name, om_stage, "running", now_iso()))
        except Exception as e:
            _log(f"workshop_step_start 失败：{type(e).__name__}: {e}")

    def workshop_step_end(self, trace_id, stage_id, status, detail):
        """离开车间工位：阶段结束（done/stopped/failed），记录处理摘要。"""
        try:
            self.put("""UPDATE workshop_steps SET status=?, detail=?, finished_at=?
                        WHERE trace_id=? AND stage_id=?""",
                     (status, jdump(detail), now_iso(), trace_id, stage_id))
        except Exception as e:
            _log(f"workshop_step_end 失败：{type(e).__name__}: {e}")

    def product_crafts(self, limit=120):
        """工艺决策记录（最新在前），供制片工厂聚合。"""
        return self._rows("SELECT * FROM product_crafts ORDER BY updated_at DESC LIMIT ?",
                          (max(1, min(int(limit), 500)),))

    def workshop_steps(self, trace_id=""):
        """工位过程记录（可选按 trace 过滤），按写入顺序返回。"""
        sql, params = "SELECT * FROM workshop_steps", ()
        if trace_id:
            sql += " WHERE trace_id=?"
            params = (trace_id,)
        sql += " ORDER BY id"
        return self._rows(sql, params)

    def workshop_step(self, trace_id, stage_id):
        rows = self._rows("SELECT * FROM workshop_steps WHERE trace_id=? AND stage_id=?",
                          (trace_id, stage_id))
        return rows[0] if rows else None

    def trace_events_between(self, trace_id, start, end, limit=300):
        """某时间段内的事件（车间工位详情回放用），按事件序返回。"""
        conn = self.read_conn()
        try:
            rows = [dict(r) for r in conn.execute(
                """SELECT id,kind,at,data FROM trace_events
                   WHERE trace_id=? AND at>=? AND at<=? ORDER BY id LIMIT ?""",
                (trace_id, start, end, max(1, min(int(limit), 500))))]
            for r in rows:
                try:
                    r["data"] = json.loads(r.get("data") or "null")
                except ValueError:
                    pass
            return rows
        finally:
            conn.close()

    # ---------- 大模型注册表（CTO 运维开关） ----------
    def llm_seed(self, defaults):
        """llm_models 表为空时写入内置模型条目；已登记过则不动（保留 CTO 的修改）。

        额度从部署时起算（used_tokens=0），不回填 token_usage 历史——历史是"所有模型"
        的累计消耗，回填会让新注册表一上线就被判额度耗尽。
        """
        with self._lock:
            try:
                row = self._conn.execute("SELECT COUNT(*) FROM llm_models").fetchone()
                if row and row[0]:
                    return
                for m in defaults:
                    self._conn.execute(
                        """INSERT INTO llm_models(id,name,url,protocol,total_tokens,used_tokens,
                           status,note,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (m["id"], m["name"], m.get("url") or "", m.get("protocol") or "",
                         _int(m.get("totalTokens")), 0, m.get("status") or "enabled",
                         m.get("note") or "", now_iso()))
                self._conn.commit()
                _log(f"llm_models 初始化完成（{len(defaults)} 条，额度从部署时起算）")
            except sqlite3.Error as e:
                _log(f"llm_seed 失败：{e}")

    def llm_models(self):
        """全部模型配置行（含可用额度换算）。"""
        rows = self._rows("SELECT * FROM llm_models ORDER BY id")
        for r in rows:
            total, used = _int(r.get("total_tokens")), _int(r.get("used_tokens"))
            if total > 0 and used >= total and r.get("status") == "enabled":
                r["status"] = "exhausted"   # 展示层兜底：额度耗尽但状态未标
        return rows

    @staticmethod
    def _llm_unavailable(m):
        status = str(m.get("status") or "enabled")
        total, used = _int(m.get("total_tokens")), _int(m.get("used_tokens"))
        return status in ("disabled", "exhausted") or (total > 0 and used >= total)

    def llm_pick(self, preferred=""):
        """挑选可用大模型通道：优先 preferred（名称/id 匹配且可用），否则按 priority
        升序取第一个可用通道。返回 (通道行|None, 拦截原因)。

        - 注册表为空 / 查询故障 → 返回默认兜底通道（沿用内置 MODEL 直调，不拦截业务）；
        - 全部通道停用或额度耗尽 → 返回 (None, 原因)，调用方拦截本次请求。
        """
        fallback = {"id": "", "name": preferred, "alias": "", "status": "enabled"}
        try:
            rows = self._rows("SELECT * FROM llm_models ORDER BY priority ASC, id ASC")
        except Exception as e:
            _log(f"llm_pick 查询失败（走默认通道）：{type(e).__name__}: {e}")
            return fallback, ""
        if not rows:
            return fallback, ""     # 未登记任何模型：向后兼容，直调默认模型
        for m in rows:
            if preferred and preferred in (m.get("name"), m.get("id")) \
                    and not self._llm_unavailable(m):
                return m, ""
        for m in rows:              # 首选通道不可用 → 按优先级故障转移
            if not self._llm_unavailable(m):
                return m, ""
        # 全部不可用：首选通道若为额度耗尽，补标 exhausted 便于运维页对齐
        for m in rows:
            if m.get("name") == preferred or m.get("id") == preferred:
                total, used = _int(m.get("total_tokens")), _int(m.get("used_tokens"))
                if str(m.get("status") or "") == "enabled" and total > 0 and used >= total:
                    self.llm_set_status(m["id"], "exhausted", "system")
        detail = "；".join(f"{m.get('name')}（{m.get('status')}）" for m in rows)
        return None, f"无可用大模型通道：{detail}。需 CTO 在运维页启用通道或追加额度。"

    def llm_consume(self, model_name, tokens):
        """调用成功后扣减额度（同步写，不进缓冲）；达到上限自动标记 exhausted。"""
        tokens = _int(tokens)
        if tokens <= 0:
            return
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT id,total_tokens,used_tokens FROM llm_models WHERE name=? OR id=? LIMIT 1",
                    (model_name, model_name)).fetchone()
                if row is None:
                    return
                total, used = _int(row["total_tokens"]), _int(row["used_tokens"])
                with self._conn:
                    self._conn.execute(
                        "UPDATE llm_models SET used_tokens=used_tokens+?, updated_at=? WHERE id=?",
                        (tokens, now_iso(), row["id"]))
                    if total > 0 and used + tokens >= total:
                        self._conn.execute(
                            "UPDATE llm_models SET status='exhausted', updated_at=?, updated_by='system' "
                            "WHERE id=?", (now_iso(), row["id"]))
                        _log(f"llm_models：{row['id']} 额度耗尽（{used + tokens}/{total}），已自动停止调用")
            except sqlite3.Error as e:
                _log(f"llm_consume 失败：{e}")

    def llm_set_status(self, model_id, status, by=""):
        """手动改开关状态：enabled / disabled / exhausted（CTO 或系统）。"""
        if status not in ("enabled", "disabled", "exhausted"):
            return False
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        "UPDATE llm_models SET status=?, updated_at=?, updated_by=? WHERE id=?",
                        (status, now_iso(), by or "system", model_id))
                return True
            except sqlite3.Error as e:
                _log(f"llm_set_status 失败：{e}")
                return False

    def llm_update(self, model_id, fields, by=""):
        """CTO 编辑模型配置（name/url/protocol/note/totalTokens）；额度变化联动状态：
        改后已用 ≥ 总额 → exhausted；原 exhausted 且额度恢复足够 → enabled。"""
        with self._lock:
            try:
                sets, params = [], []
                for key, col in (("name", "name"), ("url", "url"),
                                 ("protocol", "protocol"), ("note", "note")):
                    if key in fields:
                        sets.append(f"{col}=?")
                        params.append(str(fields.get(key) or ""))
                if fields.get("totalTokens") is not None:
                    sets.append("total_tokens=?")
                    params.append(_int(fields.get("totalTokens")))
                if not sets:
                    return False
                sets += ["updated_at=?", "updated_by=?"]
                params += [now_iso(), by or "system", model_id]
                with self._conn:
                    self._conn.execute(f"UPDATE llm_models SET {', '.join(sets)} WHERE id=?", params)
                    row = self._conn.execute(
                        "SELECT total_tokens,used_tokens,status FROM llm_models WHERE id=?",
                        (model_id,)).fetchone()
                    if row is not None:
                        total, used, status = (_int(row["total_tokens"]), _int(row["used_tokens"]),
                                               str(row["status"] or "enabled"))
                        if total > 0 and used >= total:
                            new_status = "exhausted"
                        elif status == "exhausted":
                            new_status = "enabled"   # 追加额度后自动恢复
                        else:
                            new_status = None
                        if new_status and new_status != status:
                            self._conn.execute("UPDATE llm_models SET status=?, updated_by='system' "
                                               "WHERE id=?", (new_status, model_id))
                return True
            except sqlite3.Error as e:
                _log(f"llm_update 失败：{e}")
                return False


    def approval_for(self, trace_id, action):
        """取某 trace 指定动作的最新审批单（含已决状态），用于重启后幂等复用。"""
        rows = self._rows("""SELECT * FROM approvals WHERE trace_id=? AND action=?
                             ORDER BY created_at DESC LIMIT 1""", (trace_id, action))
        if not rows:
            return None
        row = rows[0]
        try:
            row["raw"] = json.loads(row.get("raw") or "{}")
        except (TypeError, ValueError):
            row["raw"] = {}
        return row

    def approvals_pending(self):
        """所有仍为 pending 的审批单（超时清理与重启恢复用）。"""
        return self._rows("SELECT * FROM approvals WHERE status='pending' ORDER BY created_at")

    def _rows(self, sql, params=()):
        """只读查询返回 dict 列表；库不可用时返回空列表（调用方按无数据处理）。"""
        try:
            conn = self.read_conn()
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"查询失败 {sql[:60]}：{e}")
            return []

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
