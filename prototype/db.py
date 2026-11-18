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
import re
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
RETRY_MAX = 5000        # 批量写累计失败超过这个条数才丢弃，防止坏数据无限重试

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

-- v4（2026-11-16，P1-1 IT 系统资产台账）：权威数据在库，人读版由
-- prototype/it_assets.py render 自动重写 公司/IT系统资产台账.md，禁止手工维护两份。
-- 字段口径见 CEO 指令：system/host/role/public_domain/reverse_proxy/ports/
-- depends_on/cert_path/cert_expire/data_disk/log_paths/backup_policy/runbook/
-- verify_cmd/owner/status/updated_at；evidence 存采集来源（命令/配置文件路径）。
CREATE TABLE IF NOT EXISTS it_assets (
  id             TEXT PRIMARY KEY,
  system         TEXT NOT NULL,
  host           TEXT,
  role           TEXT,
  public_domain  TEXT,
  reverse_proxy  TEXT,
  ports          TEXT,
  depends_on     TEXT,
  cert_path      TEXT,
  cert_expire    TEXT,
  data_disk      TEXT,
  log_paths      TEXT,
  backup_policy  TEXT,
  runbook        TEXT,
  verify_cmd     TEXT,
  owner          TEXT,
  status         TEXT,
  updated_at     TEXT,
  evidence       TEXT
);

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
  raw        TEXT,
  category   TEXT,
  sancai     TEXT,                          -- 三才：tian / di / ren（与 category 正交）
  summary    TEXT,
  project_id TEXT,
  trace_id   TEXT,
  body       TEXT,
  -- 认知维：只记「结论」的库不会长大，这四列负责「结论怎么来的、会不会错」
  created_at    TEXT,                       -- 形成时间（updated_at 是最后修改，不能替代）
  confidence    TEXT DEFAULT 'medium',      -- high / medium / low
  invalid_when  TEXT,                       -- 失效条件（自然语言，一条即可）
  superseded_by TEXT                        -- 被哪条修订/推翻（knowledge.id）
);

-- 资料层 ↔ 认知层：四种带方向的引用，asset_kind 只区分原料与成品，
-- 加工过程靠 trace_id 贯穿（见 pipeline_runs / 交付物目录），不单列
CREATE TABLE IF NOT EXISTS asset_links (
  id         TEXT PRIMARY KEY,              -- kb_id|kind|ref|role 组合键
  kb_id      TEXT NOT NULL,                 -- knowledge.id
  asset_kind TEXT NOT NULL,                 -- raw(原料) | work(成品)
  asset_ref  TEXT NOT NULL,                 -- 原料相对路径 / 成品编号 W-xxx
  role       TEXT NOT NULL,                 -- evidence(证据) | applied(应用) | produced(产出) | verified(验证) | refutes(推翻)
  note       TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_alink_kb ON asset_links(kb_id);
CREATE INDEX IF NOT EXISTS idx_alink_ref ON asset_links(asset_kind, asset_ref);

-- 问题池：库里如果只有答案没有疑问，认知就失去生长动力
CREATE TABLE IF NOT EXISTS knowledge_questions (
  id           TEXT PRIMARY KEY,
  question     TEXT NOT NULL,
  status       TEXT DEFAULT 'open',         -- open(待解) | answered(已有答案) | dropped(放弃)
  asked_by     TEXT,
  origin_ref   TEXT,                        -- 问题从哪来：素材路径 / traceId / 人工
  answer_kb_id TEXT,                        -- 回答了它的知识条目
  created_at   TEXT,
  updated_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_kq_status ON knowledge_questions(status);

-- 知识图谱关系边：src/dst 均为 knowledge.id，id 用无序键保证同一对只有一条
CREATE TABLE IF NOT EXISTS knowledge_relations (
  id         TEXT PRIMARY KEY,
  src        TEXT NOT NULL,
  dst        TEXT NOT NULL,
  kind       TEXT,
  label      TEXT,
  weight     REAL DEFAULT 1,
  origin     TEXT DEFAULT 'heuristic',
  confirmed  INTEGER DEFAULT 1,
  detail     TEXT,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_krel_src ON knowledge_relations(src);
CREATE INDEX IF NOT EXISTS idx_krel_dst ON knowledge_relations(dst);

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


CREATE TABLE IF NOT EXISTS directives (
  id           TEXT PRIMARY KEY,
  trace_id     TEXT,
  text         TEXT,          -- 老板原话（不改写）
  source       TEXT,          -- chat / hotspot / idea / stage
  scope        TEXT,          -- global / project / trace
  project_id   TEXT,
  confirmed_by TEXT,          -- 确认人
  created_at   TEXT,
  raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_directives_trace ON directives(trace_id, created_at);


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
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  url           TEXT,
  protocol      TEXT,
  total_tokens  INTEGER NOT NULL DEFAULT 0,
  used_tokens   INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'enabled',
  alias         TEXT NOT NULL DEFAULT '',
  priority      INTEGER NOT NULL DEFAULT 100,
  provider      TEXT NOT NULL DEFAULT '',
  api_key       TEXT NOT NULL DEFAULT '',
  is_default    INTEGER NOT NULL DEFAULT 0,
  context_length     INTEGER NOT NULL DEFAULT 0,
  max_output_tokens   INTEGER NOT NULL DEFAULT 0,
  -- 并发闸门：每通道最大并发数（0=不限）；排队等待毫秒；429 熔断截止时间（Unix 秒）
  max_concurrency    INTEGER NOT NULL DEFAULT 2,
  queue_wait_ms      INTEGER NOT NULL DEFAULT 30000,
  cooldown_until     REAL NOT NULL DEFAULT 0,
  note          TEXT,
  updated_at    TEXT,
  updated_by    TEXT
);

-- 大模型时段调度：按时段自动切换默认通道（如夜间切 qwen3.8-flash 降成本）
CREATE TABLE IF NOT EXISTS llm_schedule (
  id          TEXT PRIMARY KEY,
  model_id    TEXT NOT NULL,
  start_hour  INTEGER NOT NULL DEFAULT 0,
  end_hour    INTEGER NOT NULL DEFAULT 0,
  label       TEXT NOT NULL DEFAULT '',
  enabled     INTEGER NOT NULL DEFAULT 1,
  priority    INTEGER NOT NULL DEFAULT 10,
  note        TEXT,
  updated_at  TEXT,
  updated_by  TEXT
);

-- 自动采集（素材「自动采集」标签）：每个用户在自己的 opc.db 里维护自己的采集任务
CREATE TABLE IF NOT EXISTS collect_tasks (
  id           TEXT PRIMARY KEY,
  phone        TEXT NOT NULL DEFAULT '',
  title        TEXT NOT NULL DEFAULT '',
  sources      TEXT NOT NULL DEFAULT '',      -- 逗号分隔：douyin,xiaohongshu,bilibili,article,own_account
  schedule     TEXT NOT NULL DEFAULT '',      -- daily@HH:MM / every@N（小时）
  target_count INTEGER NOT NULL DEFAULT 20,
  brief        TEXT NOT NULL DEFAULT '',      -- 采集任务描述（交给大模型理解）
  enabled      INTEGER NOT NULL DEFAULT 1,
  status       TEXT NOT NULL DEFAULT 'idle',  -- idle/running/ok/error
  last_run     TEXT NOT NULL DEFAULT '',
  last_note    TEXT NOT NULL DEFAULT '',
  next_run     TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT '',
  updated_at   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS collect_runs (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL DEFAULT '',
  phone       TEXT NOT NULL DEFAULT '',
  started_at  TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT '',
  ok          INTEGER NOT NULL DEFAULT 0,
  file_count  INTEGER NOT NULL DEFAULT 0,
  idea_count  INTEGER NOT NULL DEFAULT 0,
  out_dir     TEXT NOT NULL DEFAULT '',
  note        TEXT NOT NULL DEFAULT ''
);

-- 采集后的 CCO 分析结论：素材「创意灵感 → 采集洞察」读这张表，选题会也优先引用
CREATE TABLE IF NOT EXISTS collect_ideas (
  id         TEXT PRIMARY KEY,
  phone      TEXT NOT NULL DEFAULT '',
  task_id    TEXT NOT NULL DEFAULT '',
  title      TEXT NOT NULL DEFAULT '',
  summary    TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '',
  tags       TEXT NOT NULL DEFAULT '',
  source     TEXT NOT NULL DEFAULT '',
  link       TEXT NOT NULL DEFAULT '',
  asset      TEXT NOT NULL DEFAULT '',
  score      REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT ''
);

-- ---------- 任务中文名（老板 2026-11-16：给人看的文本不要裸 trace 号） ----------
-- trace_id → 稳定序号（第 007 号）+ 作品标题摘要；序号一经分配永不改变
CREATE TABLE IF NOT EXISTS task_names (
  trace_id   TEXT PRIMARY KEY,
  seq        INTEGER NOT NULL,
  title      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_names_seq ON task_names(seq);
"""


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def ts_to_iso(value):
    """epoch 秒（float）→ ISO+08；非法值原样返回空串。"""
    try:
        return datetime.fromtimestamp(float(value), TZ).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _directive_from_row(row):
    """directives 行 → 接口用的驼峰结构。"""
    return {
        "id": row.get("id"),
        "traceId": row.get("trace_id") or "",
        "text": row.get("text") or "",
        "source": row.get("source") or "",
        "scope": row.get("scope") or "",
        "projectId": row.get("project_id") or "",
        "confirmedBy": row.get("confirmed_by") or "",
        "createdAt": row.get("created_at") or "",
        "raw": row.get("raw") or "",
    }


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
        self._retry = 0          # 批量写失败累计条数，超限才丢弃，防无限重试
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
                                 ("api_key", "TEXT NOT NULL DEFAULT ''"),
                                 ("is_default", "INTEGER NOT NULL DEFAULT 0"),
                                 ("context_length", "INTEGER NOT NULL DEFAULT 0"),
                                 ("max_output_tokens", "INTEGER NOT NULL DEFAULT 0"),
                                 ("max_concurrency", "INTEGER NOT NULL DEFAULT 2"),
                                 ("queue_wait_ms", "INTEGER NOT NULL DEFAULT 30000"),
                                 ("cooldown_until", "REAL NOT NULL DEFAULT 0")):
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
                # 一次性种子数据（version<2）：只在升级到「模型管理」时写入一次，
                # 之后老板在页面里删除通道/时段规则，重启不会被重新灌回来。
                ver_row = self._conn.execute(
                    "SELECT version FROM schema_version LIMIT 1").fetchone()
                if ver_row is None or _int(ver_row["version"]) < 2:
                    seed_sql = (
                        "INSERT OR IGNORE INTO llm_models(id,name,url,protocol,total_tokens,"
                        "used_tokens,status,note,updated_at,alias,priority,provider,api_key,"
                        "is_default,context_length,max_output_tokens) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
                    bailian = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
                    bailian_key = ("sk-sp-H.HIRMIY.MTtu.MEUCIBMFZgP1a-BuN2dHQoxIkHQulwA35bCgydldMJo"
                                   "NvlcjAiEAuDjzjPx0OlvGEcoD_JaoOn7t33KcYXq0pzce_1PB7Qo")
                    for row_vals in (
                            ("qwen37plus-bailian", "qwen3.7-plus", bailian,
                             "openai-chat", 0, "enabled",
                             "百炼 Qwen3.7-Plus（日间默认 08–22，131k 输入 / 16k 输出）",
                             "qwen37plus", 3, "custom", bailian_key, 1, 131072, 16384),
                            ("qwen38flash-bailian", "qwen3.8-flash", bailian,
                             "openai-chat", 0, "enabled",
                             "百炼 Qwen3.8-Flash（夜间默认 22–08，多模态/4 折性价比第一）",
                             "qwen38flash", 4, "custom", bailian_key, 0, 262144, 65536),
                            ("litellm-autofree", "auto-free", "http://192.168.21.208:8000/v1",
                             "openai-chat", 0, "enabled",
                             "LiteLLM 网关免费通道（auto-free）",
                             "autofree", 5, "custom",
                             "sk-litellm-15bbcf2ea58c156130a00afd1b43ae4a", 0, 128000, 16384)):
                        mid, name, url, proto, total, status, note, alias, pri, prov, key, dflt, ctx, maxout = row_vals
                        self._conn.execute(seed_sql,
                                           (mid, name, url, proto, total, 0, status, note,
                                            now_iso(), alias, pri, prov, key, dflt, ctx, maxout))
                    # 时段调度默认规则：日间 08–22 → qwen3.7-plus；夜间 22–08 → qwen3.8-flash
                    sched_sql = (
                        "INSERT OR IGNORE INTO llm_schedule(id,model_id,start_hour,end_hour,"
                        "label,enabled,priority,note,updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?)")
                    self._conn.execute(sched_sql,
                                       ("daytime", "qwen37plus-bailian", 8, 22, "日间", 1, 10,
                                        "08:00–22:00 默认 Qwen3.7-Plus（quality-first）", now_iso(), "system"))
                    self._conn.execute(sched_sql,
                                       ("nighttime", "qwen38flash-bailian", 22, 8, "夜间", 1, 10,
                                        "22:00–08:00 默认 Qwen3.8-Flash（cost-first）", now_iso(), "system"))
                    # 老库已存在的登记行补上下文参数（只补未设置过的）
                    for sid, ctx, maxout in (("qwen37plus-bailian", 131072, 16384),
                                             ("qwen38flash-bailian", 262144, 65536),
                                             ("litellm-autofree", 128000, 16384),
                                             ("qwen38-local", 65536, 8192),
                                             ("opencode-glm53", 1000000, 131072)):
                        self._conn.execute(
                            "UPDATE llm_models SET context_length=?, max_output_tokens=? "
                            "WHERE id=? AND context_length=0", (ctx, maxout, sid))
                    if ver_row is None:
                        self._conn.execute("INSERT INTO schema_version(version) VALUES (2)")
                    else:
                        self._conn.execute("UPDATE schema_version SET version=2")
                # 老库补列：knowledge 表新增分类/摘要/项目/追踪/正文等字段
                cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(knowledge)")}
                if cols and "category" not in cols:
                    self._conn.execute("ALTER TABLE knowledge ADD COLUMN category TEXT")
                if cols and "sancai" not in cols:
                    self._conn.execute("ALTER TABLE knowledge ADD COLUMN sancai TEXT")
                if cols and "summary" not in cols:
                    self._conn.execute("ALTER TABLE knowledge ADD COLUMN summary TEXT")
                if cols and "project_id" not in cols:
                    self._conn.execute("ALTER TABLE knowledge ADD COLUMN project_id TEXT")
                if cols and "trace_id" not in cols:
                    self._conn.execute("ALTER TABLE knowledge ADD COLUMN trace_id TEXT")
                if cols and "body" not in cols:
                    self._conn.execute("ALTER TABLE knowledge ADD COLUMN body TEXT")
                # 认知维补列（老库）：形成时间 / 置信度 / 失效条件 / 被谁替代
                if cols and "created_at" not in cols:
                    self._conn.execute("ALTER TABLE knowledge ADD COLUMN created_at TEXT")
                    # 老数据没有形成时间，用最后修改时间兜底，避免前端显示空
                    self._conn.execute("UPDATE knowledge SET created_at=updated_at "
                                       "WHERE created_at IS NULL")
                if cols and "confidence" not in cols:
                    self._conn.execute("ALTER TABLE knowledge ADD COLUMN confidence TEXT DEFAULT 'medium'")
                if cols and "invalid_when" not in cols:
                    self._conn.execute("ALTER TABLE knowledge ADD COLUMN invalid_when TEXT")
                if cols and "superseded_by" not in cols:
                    self._conn.execute("ALTER TABLE knowledge ADD COLUMN superseded_by TEXT")
                # knowledge 表索引（补列后创建，确保列存在）
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_sancai ON knowledge(sancai)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_project ON knowledge(project_id)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_trace ON knowledge(trace_id)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_updated ON knowledge(updated_at)")
                # 保证始终有且只有一个全局默认（老板删掉默认通道后自动补一个）
                if not self._conn.execute(
                        "SELECT 1 FROM llm_models WHERE is_default=1 LIMIT 1").fetchone():
                    pick = self._conn.execute(
                        "SELECT id FROM llm_models WHERE status='enabled' "
                        "ORDER BY priority ASC, id ASC LIMIT 1").fetchone()
                    if pick:
                        self._conn.execute("UPDATE llm_models SET is_default=1 WHERE id=?",
                                           (pick["id"],))
            # v3：知识认知维（created_at/confidence/invalid_when/superseded_by）
            #     + asset_links（资料↔认知）+ knowledge_questions（问题池）
            row = self._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_version(version) VALUES (3)")
            elif _int(row["version"]) < 3:
                self._conn.execute("UPDATE schema_version SET version=3")
            # v4（2026-11-16 CEO P1-1）：it_assets IT 系统资产台账。建表已由 SCHEMA
            # 幂等完成，这里只推进版本戳；人读版 公司/IT系统资产台账.md 由
            # prototype/it_assets.py 随巡检自动重写，禁止手工维护两份。
            self._conn.execute("UPDATE schema_version SET version=4 WHERE version<4")
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
                try:
                    self._write(batch)
                except Exception as e:
                    # 写线程绝不能死：一旦退出，此后所有 put() 只入队不落库，进程内静默丢数据
                    _log(f"批量写线程异常（{len(batch)} 条重新入队）：{type(e).__name__}: {e}")
                    self._requeue(batch)
            if stop and not batch:
                return

    def _requeue(self, batch):
        """失败批放回队首（保持写入顺序）；累计超限才丢弃，避免一条坏数据卡死全库。"""
        with self._cv:
            self._retry += len(batch)
            if self._retry > RETRY_MAX:
                _log(f"批量写累计失败超过 {RETRY_MAX} 条，丢弃最旧一批（{len(batch)} 条）：请检查磁盘/权限")
                self._retry = 0
                return
            self._batch = batch + self._batch

    def _write(self, batch):
        """整批一个事务：单条坏语句跳过，事务级失败退避重试一次后抛出。

        旧实现把事务失败也吞成一行日志，整批就此丢失（`database is locked` 时很常见），
        调用方完全不知道有数据没落库。
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    with self._conn:
                        for sql, params in batch:
                            try:
                                self._conn.execute(sql, params)
                            except sqlite3.Error as e:
                                _log(f"写入失败（跳过该条）{sql[:60]}：{e}")
                    self._retry = 0
                    return
                except sqlite3.Error as e:
                    if attempt == 2:
                        raise
                    _log(f"批量事务失败（{len(batch)} 条），0.2s 后重试：{e}")
                    time.sleep(0.2)

    def put(self, sql, params=()):
        try:
            with self._cv:
                self._batch.append((sql, tuple(params)))
                if len(self._batch) >= FLUSH_ROWS:
                    self._cv.notify()
        except Exception as e:
            _log(f"入队失败：{type(e).__name__}: {e}")

    def flush(self):
        """同步刷盘，供摄取水位线/验收使用。失败不丢批，放回队列等下一轮。"""
        with self._cv:
            batch = self._batch
            self._batch = []
        if batch:
            try:
                self._write(batch)
            except Exception as e:
                _log(f"同步刷盘失败（{len(batch)} 条重新入队）：{type(e).__name__}: {e}")
                self._requeue(batch)

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

    def fetch(self, sql, params=()):
        """查询多行，返回 dict 列表。"""
        try:
            conn = self.read_conn()
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"查询失败 {sql[:60]}：{e}")
            return []

    def fetch_one(self, sql, params=()):
        """查询单行，返回 dict 或 None。"""
        try:
            conn = self.read_conn()
            try:
                row = conn.execute(sql, params).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"查询失败 {sql[:60]}：{e}")
            return None

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
        # id 为空绝不能入库：knowledge 的 TEXT PRIMARY KEY 在 SQLite 里允许 NULL，
        # 于是历史脏行变成 id=NULL 的幽灵条目，既删不掉（=? 匹配不到 NULL）又会回灌内存
        if not str(k.get("id") or "").strip():
            _log(f"跳过无 id 的知识条目：{str(k.get('title') or '')[:40]}")
            return
        try:
            self.put("""INSERT INTO knowledge(id,title,path,tags,updated_at,raw,
                        category,sancai,summary,project_id,trace_id,body)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET title=excluded.title,path=excluded.path,
                          tags=excluded.tags,updated_at=excluded.updated_at,raw=excluded.raw,
                          category=excluded.category,sancai=excluded.sancai,
                          summary=excluded.summary,
                          project_id=excluded.project_id,trace_id=excluded.trace_id,
                          body=excluded.body""",
                     (k.get("id"), k.get("title"),
                      k.get("path") or k.get("source"),   # 出处：召回直接给路径
                      jdump(k.get("tags")),
                      k.get("updatedAt") or k.get("at") or now_iso(), jdump(k),
                      k.get("category"), k.get("sancai"), k.get("summary"),
                      k.get("projectId") or k.get("project_id"),
                      k.get("traceId") or k.get("trace_id"),
                      k.get("body")))
        except Exception as e:
            _log(f"upsert_knowledge 失败：{type(e).__name__}: {e}")

    # ---------- 任务中文名（第 N 号） ----------
    @staticmethod
    def _clean_task_title(title):
        t = re.sub(r"\s+", " ", str(title or "")).strip().strip("《》")
        return t[:24]

    def task_name_assign(self, trace, title=""):
        """给 trace 分配稳定中文序号，返回 {"seq": int, "title": str}。

        序号取 MAX(seq)+1、永不回收，所以同一 trace 永远是同一个号；title 可后补
        （选题定稿晚于建单时补写）。失败返回 {}，调用方自行兜底。
        """
        trace = str(trace or "").strip()
        if not trace:
            return {}
        title = self._clean_task_title(title)
        now = now_iso()
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT seq,title FROM task_names WHERE trace_id=?", (trace,)).fetchone()
                if row:
                    seq = int(row["seq"])
                    old_title = row["title"] or ""
                    if title and title != old_title:
                        self._conn.execute(
                            "UPDATE task_names SET title=?,updated_at=? WHERE trace_id=?",
                            (title, now, trace))
                        self._conn.commit()
                        return {"seq": seq, "title": title}
                    return {"seq": seq, "title": old_title or title}
                seq = int(self._conn.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM task_names").fetchone()[0])
                self._conn.execute(
                    "INSERT INTO task_names(trace_id,seq,title,created_at,updated_at)"
                    " VALUES(?,?,?,?,?)", (trace, seq, title, now, now))
                self._conn.commit()
                return {"seq": seq, "title": title}
        except Exception as e:
            _log(f"task_name_assign 失败：{type(e).__name__}: {e}")
            return {}

    def task_names_all(self):
        """全部 trace → {trace: {"seq","title"}}（列表页与语音清洗共用）。"""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT trace_id,seq,title FROM task_names ORDER BY seq").fetchall()
            return {r["trace_id"]: {"seq": int(r["seq"]), "title": r["title"] or ""}
                    for r in rows}
        except Exception as e:
            _log(f"task_names_all 失败：{type(e).__name__}: {e}")
            return {}

    # ---------- C4 老板指令版本化 ----------
    def upsert_directive(self, d):
        """指令只增不改：同 id 重复写入不改原文，保证事后能对账。"""
        try:
            self.put("""INSERT INTO directives(id,trace_id,text,source,scope,project_id,
                        confirmed_by,created_at,raw) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET trace_id=excluded.trace_id,
                          source=excluded.source,scope=excluded.scope,
                          project_id=excluded.project_id,
                          confirmed_by=excluded.confirmed_by,raw=excluded.raw""",
                     (d.get("id"), d.get("traceId"), d.get("text"), d.get("source"),
                      d.get("scope"), d.get("projectId"), d.get("confirmedBy"),
                      d.get("createdAt"), d.get("raw")))
        except Exception as e:
            _log(f"指令记录写入失败：{type(e).__name__}: {e}")

    def directive(self, did):
        row = self.fetch_one("SELECT * FROM directives WHERE id=?", (did,))
        return _directive_from_row(row) if row else None

    def directives(self, limit=100, trace=""):
        sql = "SELECT * FROM directives"
        params = []
        if trace:
            sql += " WHERE trace_id=?"
            params.append(trace)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(500, int(limit or 100))))
        return [_directive_from_row(r) for r in (self.fetch(sql, tuple(params)) or [])]

    def prune_knowledge(self, keep_ids):
        """以 vault 为准清掉 db 里已不存在的知识条目（连边一起删）。

        只 upsert 不删会让被移走/改名的条目变成"死节点"，随后被 _knowledge_all()
        回灌内存并参与图谱召回。这里只入队，跟随同一批写顺序落盘。
        """
        keep = {str(i) for i in keep_ids if i}
        if not keep:
            return 0                      # 没读到任何 id 时绝不清库，避免误删全表
        try:
            conn = self.read_conn()
            try:
                dead, dirty = [], False
                for (rid,) in conn.execute("SELECT id FROM knowledge"):
                    if rid is None or str(rid).strip() == "":
                        dirty = True          # 历史 NULL 主键脏行，不能用 ? 匹配，单独按 IS NULL 删
                    elif str(rid) not in keep:
                        dead.append(str(rid))
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"prune_knowledge 读取失败：{type(e).__name__}: {e}")
            return 0
        if dirty:
            self.put("DELETE FROM knowledge_relations WHERE src IS NULL OR dst IS NULL")
            self.put("DELETE FROM knowledge WHERE id IS NULL")
        for i in dead:
            self.put("DELETE FROM knowledge_relations WHERE src=? OR dst=?", (i, i))
            self.put("DELETE FROM knowledge WHERE id=?", (i,))
        if dead or dirty:
            _log(f"知识库差集清理 {len(dead)} 条 + NULL 脏行"
                 f"{'已清' if dirty else '无'}（vault 为准）：{dead[:3]}")
        return len(dead)

    def replace_relations(self, rows, origin="heuristic"):
        """全量替换某一个来源的关系边（启发式重算用），不触碰 ai/manual 边。"""
        try:
            self.put("DELETE FROM knowledge_relations WHERE origin=?", (origin,))
            for r in rows or []:
                self.put("""INSERT INTO knowledge_relations
                            (id,src,dst,kind,label,weight,origin,confirmed,detail,updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET src=excluded.src,dst=excluded.dst,
                              kind=excluded.kind,label=excluded.label,weight=excluded.weight,
                              origin=excluded.origin,confirmed=excluded.confirmed,
                              detail=excluded.detail,updated_at=excluded.updated_at""",
                         (r.get("id"), r.get("src"), r.get("dst"), r.get("kind"),
                          r.get("label"), _float(r.get("weight")) or 1.0,
                          r.get("origin") or origin, 1 if r.get("confirmed", True) else 0,
                          r.get("detail"), now_iso()))
        except Exception as e:
            _log(f"replace_relations 失败：{type(e).__name__}: {e}")

    def upsert_relation(self, r):
        try:
            self.put("""INSERT INTO knowledge_relations
                        (id,src,dst,kind,label,weight,origin,confirmed,detail,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,label=excluded.label,
                          weight=excluded.weight,origin=excluded.origin,
                          confirmed=excluded.confirmed,updated_at=excluded.updated_at""",
                     (r.get("id"), r.get("src"), r.get("dst"), r.get("kind"),
                      r.get("label"), _float(r.get("weight")) or 1.0,
                      r.get("origin") or "manual", 1 if r.get("confirmed", True) else 0,
                      r.get("detail"), now_iso()))
        except Exception as e:
            _log(f"upsert_relation 失败：{type(e).__name__}: {e}")

    def delete_relation(self, rid):
        try:
            self.put("DELETE FROM knowledge_relations WHERE id=?", (rid,))
        except Exception as e:
            _log(f"delete_relation 失败：{type(e).__name__}: {e}")

    def list_relations(self):
        return self.fetch("SELECT * FROM knowledge_relations")

    # ---------- 资料 ↔ 认知 关联 ----------
    def upsert_asset_link(self, link):
        """挂一条引用：同一 (知识, 资产, 角色) 只存一条，重复挂只更新备注。"""
        try:
            kid = str(link.get("kb_id") or "").strip()
            ref = str(link.get("asset_ref") or "").strip()
            kind = str(link.get("asset_kind") or "raw").strip()
            role = str(link.get("role") or "evidence").strip()
            if not kid or not ref:
                _log(f"资产关联缺参数，已忽略：kb_id={kid!r} asset_ref={ref!r}")
                return None
            lid = "|".join((kid, kind, ref, role))
            self.put("""INSERT INTO asset_links
                        (id,kb_id,asset_kind,asset_ref,role,note,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET note=excluded.note,
                          updated_at=excluded.updated_at""",
                     (lid, kid, kind, ref, role, link.get("note"), now_iso(), now_iso()))
            return lid
        except Exception as e:
            _log(f"upsert_asset_link 失败：{type(e).__name__}: {e}")
            return None

    def delete_asset_link(self, lid):
        try:
            self.put("DELETE FROM asset_links WHERE id=?", (lid,))
        except Exception as e:
            _log(f"delete_asset_link 失败：{type(e).__name__}: {e}")

    def list_asset_links(self, kb_id=None, asset_kind=None, asset_ref=None):
        try:
            if kb_id:
                return self.fetch("SELECT * FROM asset_links WHERE kb_id=?", (kb_id,))
            if asset_ref:
                return self.fetch("SELECT * FROM asset_links "
                                  "WHERE asset_kind=? AND asset_ref=?",
                                  (asset_kind or "raw", asset_ref))
            return self.fetch("SELECT * FROM asset_links")
        except Exception as e:
            _log(f"list_asset_links 失败：{type(e).__name__}: {e}")
            return []

    # ---------- 问题池：只有答案没有疑问，库就不会长大 ----------
    def upsert_question(self, q):
        try:
            qid = str(q.get("id") or "").strip()
            text = str(q.get("question") or "").strip()
            if not text:
                return None
            if not qid:
                qid = "kq" + os.urandom(4).hex()
            self.put("""INSERT INTO knowledge_questions
                        (id,question,status,asked_by,origin_ref,answer_kb_id,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET question=excluded.question,
                          status=excluded.status, answer_kb_id=excluded.answer_kb_id,
                          updated_at=excluded.updated_at""",
                     (qid, text, q.get("status") or "open", q.get("asked_by"),
                      q.get("origin_ref"), q.get("answer_kb_id"), now_iso(), now_iso()))
            return qid
        except Exception as e:
            _log(f"upsert_question 失败：{type(e).__name__}: {e}")
            return None

    def list_questions(self, status=None):
        try:
            if status:
                return self.fetch("SELECT * FROM knowledge_questions "
                                  "WHERE status=? ORDER BY created_at DESC", (status,))
            return self.fetch("SELECT * FROM knowledge_questions ORDER BY created_at DESC")
        except Exception as e:
            _log(f"list_questions 失败：{type(e).__name__}: {e}")
            return []

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
        """阶段边界写档：stage 是"即将执行"的阶段，重启后从这里重跑（至少一次语义）。

        创意灵感通道（inject_idea）从 assessment 起跑，只调本方法、不调 pipeline_run_start，
        因此这里必须能自建行，否则断点续跑查不到记录（重启即丢任务）。
        """
        self.put("""INSERT INTO pipeline_runs(trace_id,status,stage,stage_index,hotspot,state,
                    started_at,updated_at,closed_at) VALUES(?,'running',?,?,NULL,?,?,?,NULL)
                    ON CONFLICT(trace_id) DO UPDATE SET status='running', stage=excluded.stage,
                      stage_index=excluded.stage_index, state=excluded.state,
                      updated_at=excluded.updated_at""",
                 (trace_id, stage, _int(index), jdump(state), now_iso(), now_iso()))
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
                           status,alias,priority,provider,api_key,is_default,context_length,
                           max_output_tokens,note,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (m["id"], m["name"], m.get("url") or "", m.get("protocol") or "openai-chat",
                         _int(m.get("totalTokens")), 0, m.get("status") or "enabled",
                         str(m.get("alias") or ""), _int(m.get("priority")) or 100,
                         str(m.get("provider") or "custom"), str(m.get("apiKey") or ""),
                         1 if m.get("isDefault") else 0, _int(m.get("contextLength")),
                         _int(m.get("maxOutputTokens")), m.get("note") or "", now_iso()))
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
        if status in ("disabled", "exhausted") or (total > 0 and used >= total):
            return True
        # media-api 是加工厂云服务的 Key 条目，不是对话端点，绝不参与 llm_pick
        if str(m.get("protocol") or "openai-chat") not in ("openai-chat", "hermes-cli"):
            return True
        # 429/限流熔断：冷却期内的通道在 llm_pick 里自动跳过（到期自动恢复）
        try:
            if float(m.get("cooldown_until") or 0) > time.time():
                return True
        except (TypeError, ValueError):
            pass
        return False

    def _llm_schedule_model_id(self):
        """根据当前时段查 llm_schedule，返回匹配的 model_id（无匹配/表空/异常返回空串）。
        时段规则：start_hour < end_hour → [start, end)；跨零点如 [22, 8) → hour>=22 或 hour<8。
        """
        try:
            rules = self._rows("SELECT * FROM llm_schedule WHERE enabled=1 ORDER BY priority ASC")
        except Exception:
            return ""
        if not rules:
            return ""
        hour = datetime.now(TZ).hour
        for r in rules:
            s, e = _int(r.get("start_hour")), _int(r.get("end_hour"))
            if s == e:          # 起止相同 = 全天
                match = True
            elif s < e:
                match = s <= hour < e
            else:               # 跨零点：如 22–08 → hour>=22 或 hour<8
                match = hour >= s or hour < e
            if match:
                return str(r.get("model_id") or "")
        return ""

    def llm_pick(self, preferred=""):
        """挑选可用大模型通道：显式 preferred → 时段调度 → 默认模型 → priority 升序故障转移。
        返回 (通道行|None, 拦截原因)。

        - preferred：仅由非实时/定时分析整理类任务传入，命中可用通道时压过时段调度；
        - 时段调度：查 llm_schedule 表，当前时段有匹配规则且对应通道可用时优先选用；
        - 默认模型：llm_models.is_default=1 的通道（模型管理页可设置，全局唯一）；
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
        # 显式首选（非实时/定时分析类任务）优先于时段调度；不可用时回落原有时段/默认/优先级
        for m in rows:
            if preferred and preferred in (m.get("name"), m.get("id")) \
                    and not self._llm_unavailable(m):
                return m, ""
        # 时段调度：当前时段有匹配规则且对应通道可用时，直接选用
        sched_id = self._llm_schedule_model_id()
        if sched_id:
            for m in rows:
                if m.get("id") == sched_id and not self._llm_unavailable(m):
                    return m, ""
        # 全局默认模型（模型管理页「设为默认」），无时段规则命中时生效
        for m in rows:
            if _int(m.get("is_default")) and not self._llm_unavailable(m):
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

    def llm_cooldown(self, model_id, seconds, reason=""):
        """429/限流熔断：把通道冷却到 now+seconds；llm_pick 会自动跳过，到期自动恢复。
        seconds<=0 表示立即解除冷却。"""
        try:
            until = max(0.0, float(seconds or 0))
        except (TypeError, ValueError):
            return False
        until = (time.time() + until) if until > 0 else 0.0
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        "UPDATE llm_models SET cooldown_until=?, updated_at=?, updated_by='system' WHERE id=?",
                        (until, now_iso(), model_id))
                if until:
                    _log(f"llm_models：{model_id} 限流熔断 {int(until - time.time())}s（{reason[:60]}）")
                return True
            except sqlite3.Error as e:
                _log(f"llm_cooldown 失败：{e}")
                return False

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

    # 模型管理页可编辑字段 → 数据库列（驼峰入参、下划线存储）
    LLM_EDIT_FIELDS = (
        ("name", "name"), ("url", "url"), ("protocol", "protocol"),
        ("alias", "alias"), ("provider", "provider"), ("apiKey", "api_key"),
        ("note", "note"),
    )
    LLM_EDIT_INT_FIELDS = (
        ("totalTokens", "total_tokens"), ("priority", "priority"),
        ("contextLength", "context_length"),
        ("maxOutputTokens", "max_output_tokens"),
        ("maxConcurrency", "max_concurrency"), ("queueWaitMs", "queue_wait_ms"),
    )

    def llm_update(self, model_id, fields, by=""):
        """模型管理页编辑模型配置（含 url/apiKey/上下文大小/别名等）；额度变化联动状态：
        改后已用 ≥ 总额 → exhausted；原 exhausted 且额度恢复足够 → enabled。"""
        with self._lock:
            try:
                sets, params = [], []
                for key, col in self.LLM_EDIT_FIELDS:
                    if key in fields:
                        sets.append(f"{col}=?")
                        params.append(str(fields.get(key) or ""))
                for key, col in self.LLM_EDIT_INT_FIELDS:
                    if fields.get(key) is not None:
                        sets.append(f"{col}=?")
                        params.append(_int(fields.get(key)))
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

    def llm_add(self, row, by=""):
        """新增一个大模型通道。row 为驼峰字段字典。返回 (ok, error)。"""
        mid = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not mid or not name:
            return False, "通道 ID 与模型名不能为空"
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", mid):
            return False, "通道 ID 只能用字母/数字/._-（≤64 字符）"
        with self._lock:
            try:
                if self._conn.execute("SELECT 1 FROM llm_models WHERE id=?", (mid,)).fetchone():
                    return False, f"通道 ID {mid} 已存在"
                if self._conn.execute("SELECT 1 FROM llm_models WHERE name=?", (name,)).fetchone():
                    return False, f"模型名 {name} 已存在"
                alias = str(row.get("alias") or "").strip()
                if alias and self._conn.execute(
                        "SELECT 1 FROM llm_models WHERE alias=?", (alias,)).fetchone():
                    return False, f"hermes 别名 {alias} 已被其它通道占用"
                with self._conn:
                    self._conn.execute(
                        """INSERT INTO llm_models(id,name,url,protocol,total_tokens,used_tokens,
                           status,alias,priority,provider,api_key,is_default,context_length,
                           max_output_tokens,note,updated_at,updated_by)
                           VALUES(?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?)""",
                        (mid, name, str(row.get("url") or ""),
                         str(row.get("protocol") or "openai-chat"),
                         _int(row.get("totalTokens")),
                         str(row.get("status") or "enabled"), alias,
                         _int(row.get("priority")) or 100,
                         str(row.get("provider") or "custom"),
                         str(row.get("apiKey") or ""),
                         1 if row.get("isDefault") else 0,
                         _int(row.get("contextLength")),
                         _int(row.get("maxOutputTokens")),
                         str(row.get("note") or ""), now_iso(), by or "system"))
                    if row.get("isDefault"):
                        self._conn.execute("UPDATE llm_models SET is_default=0 WHERE id<>?", (mid,))
                return True, ""
            except sqlite3.Error as e:
                _log(f"llm_add 失败：{e}")
                return False, str(e)

    def llm_delete(self, model_id):
        """删除通道（连带删除其时段规则）；禁止删除最后一个启用通道。返回 (ok, error)。"""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT status FROM llm_models WHERE id=?", (model_id,)).fetchone()
                if row is None:
                    return False, "通道不存在"
                left = self._conn.execute(
                    "SELECT COUNT(*) FROM llm_models WHERE status='enabled' AND id<>?",
                    (model_id,)).fetchone()[0]
                if str(row["status"]) == "enabled" and left == 0:
                    return False, "不能删除最后一个启用通道（否则所有调用会被拦截）"
                with self._conn:
                    self._conn.execute("DELETE FROM llm_schedule WHERE model_id=?", (model_id,))
                    self._conn.execute("DELETE FROM llm_models WHERE id=?", (model_id,))
                return True, ""
            except sqlite3.Error as e:
                _log(f"llm_delete 失败：{e}")
                return False, str(e)

    def llm_set_default(self, model_id):
        """设为全局默认模型（唯一）；同时清空其它模型已标记的默认位。返回 (ok, error)。"""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT id FROM llm_models WHERE id=?", (model_id,)).fetchone()
                if row is None:
                    return False, "通道不存在"
                with self._conn:
                    self._conn.execute("UPDATE llm_models SET is_default=0 WHERE is_default<>0")
                    self._conn.execute("UPDATE llm_models SET is_default=1, updated_at=?, updated_by=? "
                                       "WHERE id=?", (now_iso(), "boss", model_id))
                return True, ""
            except sqlite3.Error as e:
                _log(f"llm_set_default 失败：{e}")
                return False, str(e)

    # ---------- 大模型时段调度 ----------
    def llm_schedule_list(self):
        """返回全部时段调度规则（含当前是否匹配）。"""
        try:
            rows = self._rows("SELECT * FROM llm_schedule ORDER BY priority ASC, start_hour ASC")
        except Exception as e:
            _log(f"llm_schedule_list 查询失败：{e}")
            return []
        hour = datetime.now(TZ).hour
        out = []
        for r in rows:
            s, e = _int(r.get("start_hour")), _int(r.get("end_hour"))
            if s == e:
                active = True            # 全天
            elif s < e:
                active = s <= hour < e
            else:
                active = hour >= s or hour < e
            out.append({
                "id": r.get("id") or "",
                "modelId": r.get("model_id") or "",
                "startHour": s, "endHour": e,
                "label": r.get("label") or "",
                "enabled": bool(_int(r.get("enabled"))),
                "priority": _int(r.get("priority")),
                "active": active,
                "note": r.get("note") or "",
                "updatedAt": r.get("updated_at") or "",
                "updatedBy": r.get("updated_by") or "",
            })
        return out

    def llm_schedule_set(self, rule_id, fields, by=""):
        """新增或更新一条时段调度规则。返回 (ok, error)。"""
        with self._lock:
            try:
                existing = self._conn.execute(
                    "SELECT id FROM llm_schedule WHERE id=?", (rule_id,)).fetchone()
                sets, params = [], []
                for key, col in (("modelId", "model_id"), ("label", "label"), ("note", "note")):
                    if key in fields:
                        sets.append(f"{col}=?")
                        params.append(str(fields.get(key) or ""))
                for key, col in (("startHour", "start_hour"), ("endHour", "end_hour"),
                                 ("priority", "priority")):
                    if key in fields:
                        sets.append(f"{col}=?")
                        params.append(_int(fields.get(key)))
                if "enabled" in fields:
                    sets.append("enabled=?")
                    params.append(1 if fields.get("enabled") else 0)
                if not sets:
                    return False, "无可更新字段"
                sets += ["updated_at=?", "updated_by=?"]
                params += [now_iso(), by or "system"]
                with self._conn:
                    if existing:
                        self._conn.execute(
                            f"UPDATE llm_schedule SET {', '.join(sets)} WHERE id=?",
                            params + [rule_id])
                    else:
                        self._conn.execute(
                            f"INSERT INTO llm_schedule(id, {', '.join(s.split('=')[0] for s in sets)}) "
                            f"VALUES(?{', ?' * len(sets)})",
                            [rule_id] + params)
                return True, ""
            except sqlite3.Error as e:
                _log(f"llm_schedule_set 失败：{e}")
                return False, str(e)

    def llm_schedule_delete(self, rule_id):
        """删除一条时段调度规则。"""
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute("DELETE FROM llm_schedule WHERE id=?", (rule_id,))
                return True
            except sqlite3.Error as e:
                _log(f"llm_schedule_delete 失败：{e}")
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

    # ---------- 自动采集（素材页「自动采集」标签） ----------
    def collect_tasks(self, phone=""):
        """采集任务列表；phone 为空则返回全部（调度线程用）。"""
        sql = "SELECT * FROM collect_tasks"
        params = ()
        if phone:
            sql += " WHERE phone=?"
            params = (phone,)
        sql += " ORDER BY created_at DESC"
        conn = self.read_conn()
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def collect_task(self, task_id):
        conn = self.read_conn()
        try:
            row = conn.execute("SELECT * FROM collect_tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def collect_task_save(self, row):
        """新增或整行更新采集任务（按 id 覆盖）。"""
        row = dict(row or {})
        fields = ["id", "phone", "title", "sources", "schedule", "target_count", "brief",
                  "enabled", "status", "last_run", "last_note", "next_run",
                  "created_at", "updated_at"]
        vals = [row.get(f) if row.get(f) is not None else "" for f in fields]
        vals[5] = _int(vals[5])        # target_count（fields 下标：5=target_count，6=brief 必须保持文本）
        vals[7] = 1 if str(vals[7]).lower() not in ("0", "false", "no") else 0
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        f"INSERT INTO collect_tasks({','.join(fields)}) "
                        f"VALUES({','.join('?' * len(fields))}) "
                        f"ON CONFLICT(id) DO UPDATE SET "
                        + ",".join(f"{f}=excluded.{f}" for f in fields if f != "id"),
                        vals)
            except sqlite3.Error as e:
                _log(f"collect_task_save 失败：{e}")
                return False
        return True

    def collect_task_patch(self, task_id, fields):
        """局部更新（状态/下次运行时间/上次结果等）。"""
        fields = {k: v for k, v in (fields or {}).items() if v is not None}
        if not fields:
            return False
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(f"UPDATE collect_tasks SET {sets} WHERE id=?",
                                       [*fields.values(), task_id])
            except sqlite3.Error as e:
                _log(f"collect_task_patch 失败：{e}")
                return False
        return True

    def collect_task_delete(self, task_id, phone=""):
        sql = "DELETE FROM collect_tasks WHERE id=?"
        params = [task_id]
        if phone:
            sql += " AND phone=?"
            params.append(phone)
        with self._lock:
            try:
                with self._conn:
                    cur = self._conn.execute(sql, params)
                return cur.rowcount > 0
            except sqlite3.Error as e:
                _log(f"collect_task_delete 失败：{e}")
                return False

    def collect_run_insert(self, row):
        row = dict(row or {})
        fields = ["id", "task_id", "phone", "started_at", "finished_at", "ok",
                  "file_count", "idea_count", "out_dir", "note"]
        vals = [row.get(f) if row.get(f) is not None else "" for f in fields]
        vals[5] = 1 if vals[5] else 0
        vals[6], vals[7] = _int(vals[6]), _int(vals[7])
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        f"INSERT INTO collect_runs({','.join(fields)}) "
                        f"VALUES({','.join('?' * len(fields))})", vals)
            except sqlite3.Error as e:
                _log(f"collect_run_insert 失败：{e}")

    def collect_runs(self, task_id=None, phone="", limit=20):
        sql = "SELECT * FROM collect_runs WHERE 1=1"
        params = []
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        if phone:
            sql += " AND phone=?"
            params.append(phone)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(max(1, min(int(limit or 20), 200)))
        conn = self.read_conn()
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def collect_ideas(self, phone="", limit=60):
        sql = "SELECT * FROM collect_ideas"
        params = []
        if phone:
            sql += " WHERE phone=?"
            params.append(phone)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit or 60), 400)))
        conn = self.read_conn()
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def collect_ideas_insert(self, rows):
        rows = [dict(r) for r in (rows or [])]
        if not rows:
            return 0
        with self._lock:
            try:
                with self._conn:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO collect_ideas(id,phone,task_id,title,summary,"
                        "detail,tags,source,link,asset,score,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(r.get("id"), r.get("phone", ""), r.get("task_id", ""), r.get("title", ""),
                          r.get("summary", ""), r.get("detail", ""), r.get("tags", ""),
                          r.get("source", ""), r.get("link", ""), r.get("asset", ""),
                          _float(r.get("score")), r.get("created_at", "")) for r in rows])
                return len(rows)
            except sqlite3.Error as e:
                _log(f"collect_ideas_insert 失败：{e}")
                return 0


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


_STORES = {}                     # 路径 → Store（多用户工作区各一个库）
_STORES_LOCK = threading.Lock()


def drop_store(path):
    """把某路径的 Store 实例移出缓存（数据导入后重建用）；不强制关闭连接，避免影响在用线程。"""
    path = os.path.abspath(path)
    with _STORES_LOCK:
        return _STORES.pop(path, None)


def store_for(path):
    """按路径取/建 Store 实例（每个用户工作区一个独立 opc.db）。

    默认路径复用 store() 单例；实例化失败缓存 None（下次调用重试）。
    """
    if not ENABLED:
        return None
    path = os.path.abspath(path)
    if path == os.path.abspath(DB_PATH):
        return store()
    with _STORES_LOCK:
        st = _STORES.get(path)
        if st is None:
            try:
                st = Store(path)
                _log(f"已启用，库文件 {path}")
            except Exception as e:
                _log(f"初始化失败({path})：{type(e).__name__}: {e}")
                st = None
            _STORES[path] = st
    return st
