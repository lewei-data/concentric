# 过程数据入库方案（调研 + 说明）

> 目标：把 OPC 工作台里「过程类数据」（trace、事件流、审批、报告、会议发言、消息、token 消耗等）从内存 + 单个 JSON 全量重写，改为落 SQLite。
> 状态：**P0 已获批准并实施**（2026-11-12），实施与验收记录见 §13。**P1 前端「消息中心」已完成并上线**（2026-11-12 11:44，`/app/messages` 读 `opc.db`）；**P1 后端读切换（`/api/state` / `/api/trace` / `/api/reports` 等改读库）仍待排期**。

---

## 1. 结论先行

1. 现在不是"完全没持久化"，而是**只用了一个 515 KB 的全量 JSON 做兜底**：`/opt/sefs/obsidian/动态/runtime.json`，每次热点/报告/审批/知识事件都会整文件重写一次（[server.py L1097-L1098](file:///opt/sefs/opc-workbench/prototype/server.py#L1097-L1098)、[save_runtime L1029-L1050](file:///opt/sefs/opc-workbench/prototype/server.py#L1029-L1050)）。
2. 真正"过程级"的数据反而**根本没落盘**：`self.traces`（每轮流水线的状态、评审门、hermes 统计）、`token_usage`、`token_alerts`、`aborts`、`active_trace`、`scene`、`meeting_log` 全是内存字典（[Kernel.__init__ L944-L960](file:///opt/sefs/opc-workbench/prototype/server.py#L944-L960)），进程一重启就归零。
3. 建议：**新建一个 SQLite 库 `prototype/opc.db`（WAL）承载过程数据**，内存只保留"热态"（当前会议、agent 状态），Obsidian 的 md 继续作为人读导出物。`/opt/sefs` 已确认为本地 XFS（`df -T` 实测 `/dev/nvme0n1 xfs`），WAL 可用，不存在 NFS/SMB 上的 WAL 崩溃风险。
4. 风险可控：沿用本机已验证的做法——`dispatchd` 与 Hermes 的 `state.db` 都是 SQLite + WAL（实测表结构见 §4 附注），不是引入新依赖。

---

## 2. 现状盘点（全部为实测）

| 数据类 | 现在放哪 | 具体行为 | 问题 |
|---|---|---|---|
| trace（每轮选题会流水线） | **仅内存** `self.traces` | 状态机：running/completed/blocked/rejected/timeout/error/aborted | 重启即丢；`/api/trace/<id>` 查不到历史；无法回答"上周哪几轮被驳回" |
| 事件流（emit） | 内存广播 + 少量触发落盘 | `emit()` 推给 SSE 订阅者，只有 hotspot/report/approval/knowledge 四类才调 `save_runtime()` | 事件本身没有表，回放不了 |
| 报告 reports | runtime.json | 只留最后 **40** 条 | 超出静默丢弃 |
| 审批 approvals | runtime.json | 只留最后 **40** 条（文件里现存 20 条） | 超期审批被覆盖；无法做审批时长统计的长周期分析 |
| 消息 messages（agent 互发） | runtime.json | 只留最后 **120** 条 | 单条 payload 很长，是 515 KB 的主因；掐头去尾 |
| 热点 hotspots | runtime.json | 只留 **20** 条 | — |
| 知识 knowledge | runtime.json + Obsidian md | 留 80 条；md 优先 | 两处真源，靠 `_backfill_knowledge` 对齐 |
| 会议纪要 | Obsidian `OPC/会议纪要/<slug>/meeting-transcript.md` + `index.json` | 实测 **47 场、211 KB** | 全文在 md 里，程序侧只能整份加载 index.json；按"发言逐条"查询/统计做不了 |
| token 消耗 / 成本护栏 | **仅内存** `token_usage`、`token_alerts` | 按 trace 聚合，阈值告警 | 重启清零，成本对账不可追溯 |
| 交付物 contents | Obsidian 目录 | 启动时全量扫描 | 与过程数据脱钩，无法按 trace 反查产物 |
| 计数器 events / latencies | 内存 | `self.events`、`self.latencies` | 重启归零 |

写入放大：`save_runtime()` 每次都是**整文件重写**（`json.dump` 到临时文件再替换），文件已 515 KB，而触发频率是"每个报告/审批/热点事件"。这是当前最该先解决的性能点。

---

## 3. 目标架构

```
              ┌─────────────────────────── opc-workbench (单进程 :8848) ───────────────────────────┐
  写入：      │  emit()/send()/状态机 ──► BufferWriter(线程, 200ms 或 200 条 flush) ──► SQLite(WAL)  │
              │  热态：agents / scene / meeting(当前) / subscribers  ── 仍在内存（可重建）           │
  读取：      │  /api/state、/api/trace/<id>、/api/reports ──► DAO 查库 + 热态合并                   │
  人读：      │  Obsidian md（会议纪要、审批报告、动态快照）── 由 DB 数据异步渲染，变成"导出物"        │
              └──────────────────────────────────────────────────────────────────────────────────┘
```

- 库文件：`/opt/sefs/opc-workbench/prototype/opc.db`（+ `-wal`/`-shm`），与代码同目录，备份随目录走。
- 连接：单写连接 + `check_same_thread=False` + `Kernel.lock` 复用（与 `dispatchd.py` L221 同款做法）；读用独立只读连接。
- 参数：`journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`foreign_keys=ON`。
- 不引入 ORM，用 `sqlite3` 原生 + 一层薄 DAO（`prototype/db.py`），降低对现有单文件服务结构的冲击。

---

## 4. 表设计（DDL 草案）

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE schema_version (version INTEGER NOT NULL);

-- 过程主轴：一轮流水线
CREATE TABLE traces (
  trace_id      TEXT PRIMARY KEY,
  hotspot_id    TEXT,
  status        TEXT NOT NULL,            -- running/completed/blocked/rejected/timeout/error/aborted
  started_at    TEXT NOT NULL,
  closed_at     TEXT,
  abort_reason  TEXT,
  review_gate   TEXT,                     -- JSON
  review_history TEXT,                    -- JSON（各阶段评审记录）
  hermes        TEXT,                     -- JSON（token/耗时统计）
  meta          TEXT                      -- JSON（其余扩展字段）
);
CREATE INDEX idx_traces_status_time ON traces(status, started_at DESC);

-- 事件流（emit 的持久化形态，只增不改）
CREATE TABLE trace_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id  TEXT,
  kind      TEXT NOT NULL,                -- hotspot/report/approval/knowledge/task/...
  at        TEXT NOT NULL,
  data      TEXT                          -- JSON
);
CREATE INDEX idx_events_trace ON trace_events(trace_id, id);
CREATE INDEX idx_events_time  ON trace_events(at);

-- agent 消息
CREATE TABLE messages (
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
CREATE INDEX idx_msg_time ON messages(at DESC);

-- 审批
CREATE TABLE approvals (
  id          TEXT PRIMARY KEY,
  trace_id    TEXT,
  action      TEXT,
  risk        TEXT,
  status      TEXT NOT NULL,              -- pending/approved/rejected/timeout
  approver    TEXT,
  created_at  TEXT NOT NULL,
  decided_at  TEXT,
  decided_by  TEXT,
  timeout_sec INTEGER,
  items       TEXT                        -- JSON
);
CREATE INDEX idx_appr_status ON approvals(status, created_at DESC);

-- 报告 / 风险单
CREATE TABLE reports (
  id         TEXT PRIMARY KEY,
  trace_id   TEXT,
  kind       TEXT,
  title      TEXT,
  conclusion TEXT,
  evidence   TEXT,                        -- JSON
  decisions  TEXT,                        -- JSON
  created_at TEXT
);

-- 会议
CREATE TABLE meetings (
  id            TEXT PRIMARY KEY,
  trace_id      TEXT,
  title         TEXT,
  status        TEXT,
  started_at    TEXT,
  ended_at      TEXT,
  conclusion    TEXT,
  participants  TEXT,                     -- JSON
  transcript_url TEXT
);
CREATE TABLE meeting_speeches (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  speaker    TEXT,
  role       TEXT,
  text       TEXT,
  at         TEXT
);
CREATE INDEX idx_speech_meeting ON meeting_speeches(meeting_id, seq);

-- 热点 / 知识 / 交付物
CREATE TABLE hotspots (
  id TEXT PRIMARY KEY, title TEXT, source TEXT, url TEXT,
  score REAL, collected_at TEXT, raw TEXT
);
CREATE TABLE knowledge (
  id TEXT PRIMARY KEY, title TEXT, path TEXT, tags TEXT,
  updated_at TEXT
);
CREATE TABLE contents (
  id TEXT PRIMARY KEY, trace_id TEXT, project_id TEXT, stage TEXT,
  title TEXT, owner TEXT, tone_profile TEXT, created_at TEXT, raw TEXT
);
CREATE INDEX idx_content_trace ON contents(trace_id);

-- 成本护栏
CREATE TABLE token_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id TEXT, agent TEXT, model TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER,
  cost REAL, at TEXT
);
CREATE INDEX idx_token_trace ON token_usage(trace_id);

-- 进程生命周期（区分"内存态归零"与"真的没跑过"）
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, pid INTEGER, started_at TEXT, ended_at TEXT, version TEXT
);

-- 过程统计（按天聚合，供 KPI 面板）
CREATE TABLE stats_daily (
  day TEXT PRIMARY KEY,
  traces INTEGER, published INTEGER, blocked INTEGER,
  approval_seconds_avg REAL, events INTEGER
);
```

设计取舍：
- **不把 JSON 拆成宽表**：`payload/items/evidence` 这类结构在各 agent 之间差异很大，拆表会让每次字段调整都变成迁移；只把**要查询/排序/统计**的列提出来，其余留在 JSON 列里（SQLite 支持 `json_extract`，需要时再加函数索引）。
- **会议发言单独成表**（`meeting_speeches`）：这是唯一明确"逐条追加、按序读"的高频过程数据，md 不再承担结构化查询。
- `runs` / `stats_daily` 解决当前最尴尬的体验问题：重启后 `startedAt` 重置，前端分不清"今天没跑"还是"刚重启"。

---

## 5. 改造点（按文件）

### 5.1 新增 `prototype/db.py`
- `connect()/write_conn()/read_conn()`、`init_schema()`、`buffer` 写入器（`INSERT` 打包成事务，200 ms 或 200 条触发 flush）。
- 关闭时 `flush()` + `wal_checkpoint(TRUNCATE)`。

### 5.2 `prototype/server.py`
| 位置 | 现状 | 改法 |
|---|---|---|
| `emit()` [L1088-L1099](file:///opt/sefs/opc-workbench/prototype/server.py#L1088-L1099) | 四类事件触发全量 `save_runtime()` | 一律 `db.append_event()`；删掉这里的 `save_runtime()` |
| `save_runtime()` [L1029](file:///opt/sefs/opc-workbench/prototype/server.py#L1029) | 全量重写 515 KB | 降级为"每日 03:00 快照"与"关机快照"，仅供人工查看，不再是恢复来源 |
| `_restore_history()` [L967-L1027](file:///opt/sefs/opc-workbench/prototype/server.py#L967-L1027) | 从 runtime.json/磁盘回填 | 改为"DB 为准，md 兜底"；`runtime.json` 只在 DB 为空时做一次性导入 |
| `self.traces[...]` 状态机（L1529/2380/2427/2527 等） | 只改内存 | 每次状态变更 `db.upsert_trace()`（同一事务，不额外开写） |
| `send()` [L1101](file:///opt/sefs/opc-workbench/prototype/server.py#L1101) | 只追加内存 | `db.insert_message()` |
| `token_usage` / `token_alerts` | 内存 | `db.insert_token_usage()` |
| `snapshot()` / `/api/state` [L3396](file:///opt/sefs/opc-workbench/prototype/server.py#L3396) | 全内存拼装 | 热态走内存，历史列表走 `SELECT ... ORDER BY at DESC LIMIT n`；**返回 JSON 结构保持不变**（前端零改动） |
| `/api/trace/<id>` [L3400](file:///opt/sefs/opc-workbench/prototype/server.py#L3400) | 查内存 | 查 DB，顺带返回该 trace 的 `trace_events` |
| `save_meeting_record()`（deliverables.py） | 写 md + index.json | 写 `meetings` + `meeting_speeches`，md 由 DB 渲染导出 |

### 5.3 一次性导入器 `prototype/import_runtime.py`
- 读 `动态/runtime.json` + `OPC/会议纪要/index.json` + 各会议 md，写入上面的表；`trace_events` 由 `messages` 与报告时间线合成最小事件序列（无法还原的字段留空，不做臆造）。
- 导入后打印"表 → 行数"对照，作为验收证据。

### 5.4 开关与回滚
- 环境变量 `OPC_DB=1`（默认关）启用新路径；`OPC_DB=0` 完全走旧逻辑。
- P0-P2 期间**双写**：DB 写成功 + `runtime.json` 照常写。任何异常只记日志、不影响主流程（写库失败降级为旧行为）。
- 回滚：改写 `opc-workbench.service` 的 `Environment=` 重启即可，无需改代码。

---

## 6. 数据保留与清理

| 表 | 保留策略 | 理由 |
|---|---|---|
| `trace_events` | 90 天，之后按天删 | 体积增长最快（每条 emit 一行） |
| `messages` | 30 天 | 中间过程，追溯窗口够用 |
| `traces` / `approvals` / `reports` | 永久 | 决策留痕，体积小 |
| `meetings` / `meeting_speeches` | 永久 | 会议是资产，且 md 已归档 |
| `token_usage` | 13 个月 | 成本对账 |
| `stats_daily` | 永久 | KPI 趋势 |

清理作业并入 Hermes 已有的定时体系（CEO profile cron 之外，建议放在系统 crontab 的维护类，符合"系统定时只做运行状态维护"的约定）。

预估体积：当前 515 KB JSON 对应约 40 条报告 + 20 审批 + 120 消息 + 47 场会议；按此密度，`trace_events` 90 天量级在**百 MB 以内**，本地 XFS 磁盘 1.8 TB 完全无压力。

---

## 7. 验收标准

1. `sqlite3 opc.db "select count(*) from approvals"` 与导入前 `runtime.json` 的审批数一致（当前 20）。
2. 重启 `opc-workbench.service` 后：`/api/trace/<旧 traceId>` 仍能返回完整记录与事件序列（改造前必 404）。
3. 连续跑一轮选题会：`traces` 有 1 行、`trace_events` 按序增长、`meetings`/`meeting_speeches` 有对应行，且 Obsidian md 内容与改造前一致。
4. `runtime.json` 不再因普通事件被重写（用 `stat` 观察 mtime 一天）。
5. 前端 `/api/state` 返回结构逐字段对比改造前后无缺失（写一个 diff 脚本，比对键名集合）。
6. 压测：连续 emit 1000 条，P95 写入耗时 < 20 ms（缓冲批写），进程 RSS 不因缓冲显著增长。
7. 故障注入：库文件设为只读启动一次，服务仍能正常服务（降级为旧路径 + 错误日志）。

---

## 8. 风险与明确不做的事

| 风险 | 处置 |
|---|---|
| 单进程并发写 | 单写连接 + 既有 `Kernel.lock`；读连接只读，WAL 允许读写并行 |
| schema 变更 | `schema_version` + 启动时幂等 `ALTER`（只增列），不做破坏性迁移 |
| 写库失败拖垮主流程 | 所有 DB 调用包 try/except，失败记 `errors.log` 并降级 |
| 前端字段缺失 | 保留 `/api/state` 结构，P1 加键名 diff 断言 |
| 会议 md 由程序生成，格式漂移 | md 渲染保持现有模板字符串，验收第 3 条做逐字节对比 |

**不做**：
- 不把 Hermes 的会话库（`profiles/*/state.db`）并入本库——两者生命周期与迁移节奏不同。
- 不引入 Postgres/Redis——单进程 + 单机，SQLite 足够。
- 不把 Obsidian 目录改成"只有 DB"——md 仍是人读真源，DB 是过程真源，二者职责分离。
- 不动 `dispatch.db`（实时语音链路），它已经有自己的表。

---

## 9. 分阶段执行清单

| 阶段 | 内容 | 可回滚 | 影响面 |
|---|---|---|---|
| **P0 影子期** | 建库 + `db.py` + 导入器 + 双写（`emit`/`send`/trace 状态/审批）；读数仍走内存 | 是（`OPC_DB=0`） | server.py 约 10 处调用点 |
| **P1 读切换** | `/api/state`、`/api/trace`、`/api/reports` 改读 DB；加键名 diff 断言 | 是 | 3 个接口 |
| **P2 会议与交付物** | `meetings`/`meeting_speeches`/`contents` 入库；md 改为 DB 渲染 | 是 | deliverables.py |
| **P3 运维收尾** | 保留策略清理作业、`stats_daily` 聚合、`runtime.json` 降级为日快照 | 是 | cron 维护项 |

建议批准范围：**先做 P0**，影子期观察一天确认行数与线上一致，再决定是否继续 P1。

> 多渠道消息统一（飞书 / APP 语音 / Web）见 §12，其工作量并入 P0（建表+回填+只读 API）与 P1（前端消息中心）。

---

## 10. 附：为什么不"直接把 runtime.json 换成 SQLite 单表"

可以，但只解决写入放大，解决不了核心问题：trace 根本不在文件里。单表 KV 也无法支撑"按状态/时间查 trace""某场会议逐条发言""按天统计审批时长"这三类最常问的问题，最后仍要再做一次拆表。上面的设计是一次到位且不复杂的方案。

## 11. 参考：本机已有 SQLite 实践（实测）

- `dispatchd`：`dispatch.db` 内 `tasks / channels / utterances / events / lane_sessions`，均为 SQLite 表（[dispatchd.py L221](file:///opt/sefs/hermes/dispatch/dispatchd.py#L221) 单连接 `check_same_thread=False`）。
- Hermes：`profiles/*/state.db` 用 `sessions / messages / messages_fts / worker_executions` 等，含 FTS5 全文索引。
- 结论：SQLite 在本机是被验证过的存储层，本方案不引入新组件。

---

## 12. 补充调研：多渠道消息统一（飞书 / APP 语音 / Web）

目标：飞书（含语音）、APP（语音/视频/文字）、Web 三个入口的消息**全部落库**，用同一套字段管理，并让 Web 能读。

### 12.1 现状实测（2026-11-12 02:00 左右）

| 通道 | 实际落点 | 实测规模（本次查询） | 缺口 |
|---|---|---|---|
| 飞书（文字+语音） | `profiles/secretary/state.db`：`sessions.source='feishu'` + `messages` | 1 个会话 / 481 条；其中 user 22、assistant 218、tool 241；user 里语音占位 3 条 | 语音只在转写失败时留 `"voice message could not be transcribed…"` 占位；`display_name` 存的是 chat_id 不是人名；无统一会话视图 |
| APP（语音/视频/文字） | `dispatch/dispatch.db`：`tasks` + `utterances` | `tasks` 173 条（boss voice 122 / video 9 / text 8 / 其他 34）；`utterances` 115 条（chat 48 / task 39 / 未标 28） | 一问一答挤在同一行（`asr_text`/`reply_text`/`voice_text`）；无对外读接口 |
| Web（工作台指令/派活） | `server.py` 的 `Kernel.messages`（内存，>4000 截断到 2000）+ `runtime.json` 仅留尾 120 条 | `runtime.json` 现存 120 条，文件 557,381 字节 | 重启即丢；跨源不可查；与飞书/APP 完全没有关联键 |

三个来源的 id 体系互不相通，无法回答"老板这周在哪些渠道提过什么"。

### 12.2 五条关键发现

1. **飞书语音在库里的形态**：转写失败 → 一段英文占位文本（含音频路径）；转写成功 → 纯文本，与打字**不可区分**。`messages` 表没有 `modality` 列，飞书原始消息类型（audio/text）在网关侧就丢了。
2. **中文检索有现成先例**：Hermes 的 `messages_fts_trigram` 用 `tokenize='trigram'`（本机 SQLite 3.45.1），中文两字词可命中；直接沿用，不自创分词。
3. **dispatch 一条 utterance = 一问一答**：`asr_text`（老板原话）、`reply_text`（文字回复）、`voice_text`（实际念出的稿），必须拆成 inbound + outbound 两条记录。
4. **telemetry 不能当消息库**：`telemetry/` 的 DuckDB 只存指标（字符数、token、时长），不含消息正文（依据 `telemetry/schema.sql`）。
5. **Hermes 属于上游代码**：摄取一律走 state.db **只读轮询**（`mode=ro`），不给网关打补丁，升级不会冲突。

### 12.3 表设计（新增，与 §4 的 agent 总线 `messages` 分开）

```sql
-- 统一会话：一个来源的一个对话线程
CREATE TABLE chat_threads (
  id           TEXT PRIMARY KEY,       -- feishu:<session_id> / app:<profile> / web:<room>
  source       TEXT NOT NULL,          -- feishu | app | web
  title        TEXT,
  participants TEXT,                   -- JSON
  created_at   TEXT NOT NULL,
  last_at      TEXT,
  msg_count    INTEGER NOT NULL DEFAULT 0
);

-- 统一消息：一行是一条人说的话或一条回复
CREATE TABLE chat_messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id   TEXT NOT NULL,
  source      TEXT NOT NULL,           -- feishu | app | web
  modality    TEXT NOT NULL,           -- text | voice | video | image | file | mixed
  direction   TEXT NOT NULL,           -- inbound | outbound
  sender_id   TEXT,
  sender_name TEXT,
  content     TEXT,
  content_kind TEXT NOT NULL DEFAULT 'text',
  origin_table TEXT,                   -- 来源表名，便于回溯
  origin_id   TEXT,                    -- 来源主键（state.db messages.id / task_id+utterance.id / msg_xxx）
  trace_id    TEXT,                    -- 关联 §4 的 traces（有就填）
  reply_to    INTEGER,                 -- 自引用 chat_messages.id
  asr         TEXT,                    -- JSON：confidence/avg_logprob/filtered_reasons/audio_path（语音专用）
  meta        TEXT,                    -- JSON：其余原始字段
  created_at  TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  UNIQUE(source, origin_table, origin_id)
);
CREATE INDEX idx_cm_thread ON chat_messages(thread_id, id DESC);
CREATE INDEX idx_cm_time   ON chat_messages(created_at DESC);
CREATE INDEX idx_cm_src    ON chat_messages(source, modality, id DESC);

-- 中文全文检索（沿用 Hermes 的 trigram 方案）
CREATE VIRTUAL TABLE chat_messages_fts USING fts5(
  content, content='chat_messages', content_rowid='id', tokenize='trigram'
);

-- 增量摄取水位线
CREATE TABLE ingest_cursor (
  source     TEXT NOT NULL,
  table_name TEXT NOT NULL,
  watermark  INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY(source, table_name)
);
```

FTS 用 `AFTER INSERT/DELETE/UPDATE` 触发器同步（照抄 Hermes `state.db` 的写法，只保留 `chat_messages` 一份，不重复存正文）。

### 12.4 三条摄取路径

| 源 | 方式 | 频率 | 映射 |
|---|---|---|---|
| 飞书 | `ingest_feishu.py` 只读连 `profiles/*/state.db`，取 `sessions.source='feishu'` 下的 `role in ('user','assistant')` 且 `id > watermark` | 30s | `sessions.id → thread_id`；`role → direction`；`platform_message_id → origin_id`；`content` 命中 `voice message could not be transcribed` → `modality='voice'`，否则 `text` |
| APP/语音 | `ingest_dispatch.py` 读 `dispatch.db`：`tasks` 按 `rowid` 水位、`utterances` 按 `id` 水位 | 30s | `tasks.modality` 决定 modality（voice/video/text）；一条 utterance 拆 inbound(`asr_text`) + outbound(`reply_text`)，`voice_text`/`mode` 进 `meta` |
| Web | `Kernel.send()` 与 `/api/commands` 直接同进程写入（无轮询） | 实时 | `msg.id → origin_id`；`type → content_kind`；`payload.text → content` |

- 统一调度：新增 `prototype/ingest.py` 后台线程，`OPC_INGEST=1` 开关；单次异常只记日志，不影响主流程。
- 去重：`UNIQUE(source, origin_table, origin_id)`；水位线只前进不回退，重启不重复插入。
- 只读打开：`sqlite3.connect('file:...?mode=ro', uri=True)`，不触碰 WAL 写锁，不影响 Hermes 网关。

### 12.5 Web 读取接口（挂在 8848 现有服务上）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/chat/threads?source=&limit=&before=` | 会话列表（含 last_at、msg_count） |
| GET | `/api/chat/messages?thread=&source=&modality=&direction=&q=&before_id=&limit=` | 消息流，游标分页；`q` 走 FTS trigram |
| GET | `/api/chat/stats?days=7` | 按 source / modality / 天 聚合 |
| GET | `/api/chat/export?from=&to=&format=jsonl` | 仅 `boss` 角色，写审计日志 |
| GET | `/api/events`（已有 SSE） | 新增 `chat.message` 事件，摄取成功即推送，前端免轮询 |

- 鉴权：复用 `opc_sess` 会话 Cookie（[server.py L3160](file:///opt/sefs/opc-workbench/prototype/server.py#L3160-L3173)）；P0 只对 `role=老板` 开放全量，`staff` 暂不开放，避免私聊扩散。
- 音频/视频文件不进 API：只返回 `asr.audio_path` 逻辑名，P0 不做播放。

### 12.6 字段映射速查

- 飞书：`thread_id='feishu:'+sessions.id`，`sender_id=sessions.user_id`（`ou_...`），`sender_name` 暂用 `title`（实测 `display_name` 是 chat_id），`created_at` 由 `messages.timestamp`（epoch）转 ISO+08；`tool` 角色不入库（实测 241 条）。
- APP：`thread_id='app:'+profile`，`sender_name='老板'`，`sender_id='boss'`，inbound `created_at` 取 `utterances.ts`，outbound 取 `ts` 同刻（同轮）。
- Web：`thread_id='web:workbench'`，`sender_name` 取登录用户 `name`，`direction` 由 `type` 判定（`result`→outbound，其余入站）。

### 12.7 风险与明确不做

1. **飞书成功转写的语音无法标记为 voice**：要么接受"文本级记录"，要么在网关加 hook（改上游代码）。建议 P0 接受，P2 再议；`asr` 字段先留空。
2. 轮询延迟最多 30s；Web 自身写入是实时的。可接受则不做网关推送。
3. 私聊正文入库 → 只读接口限老板 + 导出审计；库文件权限 `0600`。
4. 不搬音频/视频二进制，只存路径与元数据（体积与备份成本）。
5. 不做反向写入：Web 只能读镜像，不能改飞书/APP 原始记录。
6. 不复用 §4 的 `messages` 表（那是 agent 内部总线，字段语义不同），避免两类数据混表。

### 12.8 验收标准（在 §7 之外追加）

1. 回填后 `select source,count(*) from chat_messages group by 1` 与源库逐一对照：飞书 user 22（其中 voice 3）、APP tasks 173 / utterances 115。
2. 新发一条飞书文字 + 一条飞书语音，30s 内出现在 `/api/chat/messages`，语音那条 `modality='voice'`。
3. 新发一条 APP 语音，出现成对 inbound/outbound，均为 `source='app'`、`modality='voice'`。
4. Web 发一条指令，1s 内可查，且与 `/api/state` 的 messages 尾部一致。
5. 中文两字词 `q=` 能命中（trigram 生效）。
6. 断开/锁住源库时摄取线程只记日志，工作台主流程不受影响。
7. 重启后水位线不回退，行数不重复。

### 12.9 阶段归属

- **P0 增补**：`chat_threads` / `chat_messages` / FTS / `ingest_cursor` + `ingest.py` + 历史回填 + 只读 API（仅老板）。
- **P1**：前端「消息中心」页（列表 + 筛选 + 搜索 + SSE 实时）。
- **P2**：飞书 modality 精确化（网关 hook，需改上游）；受控音频播放。

---

## 13. 实施记录（P0 影子期 + §12 统一消息）

实施完成时间：2026-11-12；开关：systemd drop-in `/etc/systemd/system/opc-workbench.service.d/db.conf` 写入 `OPC_DB=1`、`OPC_INGEST=1`。

新增文件：
- `prototype/db.py`：§4/§12.3 DDL、WAL、200ms/200 条缓冲批写、单写连接、只读查询连接、FTS(trigram) 与触发器。
- `prototype/ingest.py`：飞书/APP 双源只读轮询（30s）、`ingest_cursor` 水位线、`UNIQUE(source,origin_table,origin_id)` 去重。
- `prototype/import_runtime.py`：runtime.json + 会议/交付 + 飞书/APP 一次性回填（幂等）。

`server.py` 改造：`emit()` 事件落库并按类型 upsert（report/approval/hotspot/knowledge/pipeline）；`send()` 落 `messages` 且 owner 相关消息进 `chat_messages`；trace 创建/评审/收口/token 用量/会议开始-发言-结束均落库；新增 `/api/chat/threads|messages|stats|export`（仅老板）；`/api/trace/<id>` 内存缺失时用库补齐（`source='db'|'db+mem'`）；`OPC_DB=0` 时完全走旧逻辑。

回填结果（2026-11-12 11:11 实测）：

| 表 | 行数 |
|---|---|
| traces | 24（全部 `legacy`，仅骨架） |
| trace_events | 435 |
| messages | 120 |
| approvals | 22 |
| reports | 3 |
| hotspots / knowledge | 20 / 80 |
| contents | 165 |
| meetings / meeting_speeches | 80 / 199 |
| chat_messages | 635（feishu 240 / app 395） |
| chat_threads | 19 |

验收对照（§7 + §12.8）：

1. 审批行数 22 = 当时 `runtime.json` 审批数 22 ✅
2. 重启后 `/api/trace/tr_ad892190` 返回 200，`meta.status=legacy`，事件 165 条 ✅（改造前重启即 404）
3. 完整跑一轮选题会：**未执行**（单轮实测 12.4M token，需专门安排）
4. `runtime.json` mtime 仍为 08:37:30（重启与 Web 指令后未重写）✅
5. `/api/state` 键集合保持 19 项不变 ✅
6. 1000 条事件缓冲写入：put P95 = 0.012 ms，max 0.149 ms ✅
7. 库路径不可写时 `db.store()` 返回 None、仅打日志、服务照常 ✅
8. 飞书回填 240 条（user 22 / assistant 218），其中 `modality='voice'` 2 条 —— 与源库当前 `content LIKE '%voice message could not be transcribed%'` 的 2 条一致（§12.8 原写 3 条，为 11-12 复核后的修正值）✅
9. APP 回填 395 条 = tasks 176 + utterances 派生 219；utterances 唯一 `origin_id` 115 条（与源表一致）✅
10. Web 指令落库耗时 0.02s，立即可查；`chat.message` 经 SSE 推送 ✅
11. 中文三字 `q=口型合成` 命中 FTS；两字 `q=选题` 走 LIKE 兜底命中 3 条 ✅
12. 两次重启后 `chat_messages` 恒为 636、线程 20，无重复 ✅

补充验证（2026-11-12 11:30–11:32，隔离环境，不写生产源库）：

13. 新消息 30s 可见性 ✅：复制 `profiles/secretary/state.db` 到临时目录并在副本插入探针消息，跑真实 `Ingestor`（interval=30s）→ **DB 可见延迟 29.9s**，`on_event` 回调同步触发，水位线推进到探针 id，落库行 `modality=text / direction=inbound / ingested_at` 正确。生产库同时核对：`ingest_cursor` 8715 = 源库 feishu user/assistant 最大 id，无积压。
14. `token_usage` 字段 ✅：真实最小调用（secretary / deepseek-v4.1-flash，10,663 tokens）经 `Kernel.record_usage` 落库一行，字段映射正确（`prompt_tokens`=inputTokens 9932、`completion_tokens`=outputTokens 151、`total_tokens`=totalTokens 10663、`cost`=costUsd 0.0、`model` 落库）。
   两个口径注意：① `total_tokens` 含辅助调用，故 prompt+completion ≠ total（10083 vs 10663）为正常；② provider 返回 `cost_status=unknown`，`cost` 目前恒为 0，成本只能按 token 计，不能按金额。
15. APP（dispatch）消息 ✅：复制 `dispatch.db` 到临时目录，插入 1 条 `tasks` + 1 条 `utterances`（interval=30s）→ **DB 可见延迟 30.02s**，派生 3 行 `rt_verify_30s` / `116:in` / `116:out`，`on_event` 命中 3/3，线程 `app:secretary` 的 `msg_count` 自增到 261，水位线推进到 tasks 177 / utterances 116。

### 13.1 飞书 / APP 消息验证步骤（可重复执行）

原则：**只读复制源库到临时目录，在副本里造消息，跑真实 `Ingestor`**；全程不写 `/opt/sefs/hermes` 生产库。所有命令前置 `env -u LD_LIBRARY_PATH`（本机 Trae 注入的 `LD_LIBRARY_PATH` 会导致 python 加载失败）。

公共准备：

```python
os.environ.update(OPC_DB="1", OPC_DB_PATH="<tmp>/opc.db",
                  OPC_HERMES_PROFILES="<tmp>/profiles",        # 飞书：放 state.db 副本；APP 测则指向空目录
                  OPC_DISPATCH_DB="<tmp>/dispatch.db")          # 飞书：指向不存在路径；APP 测则放 dispatch.db 副本
sys.path.insert(0, "/opt/sefs/opc-workbench/prototype")
import db, ingest
store = db.Store()
ing = ingest.Ingestor(store, on_event=lambda t, m: events.append((time.time(), m["origin_id"]))).start()
```

复制源库用 SQLite backup API（不要 `cp`，避免读到写入中的中间态）：
`src = sqlite3.connect("file:<源库>?mode=ro", uri=True); src.backup(sqlite3.connect("<副本>"))`

**飞书（`profiles/*/state.db`，当前仅 secretary 有数据）**

1. 等首轮同步完成：`store.get_cursor("feishu", "messages:secretary")` 追上源库
   `SELECT MAX(m.id) FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.source='feishu' AND m.role IN ('user','assistant')`。
2. 往副本插一条探针：`INSERT INTO messages(session_id, role, content, timestamp, platform_message_id) VALUES(<现有 feishu session_id>, 'user', '【验收探针】…', <epoch>, 'verify-probe-…')`，记下 `lastrowid = mid`。
3. 每 50ms 轮询 `SELECT COUNT(*) FROM chat_messages WHERE origin_id='<mid>'`，首个命中即延迟。
4. 断言（全部满足才算通过）：
   - 延迟 ≈ 30s + 处理开销（>30s 说明有积压或轮询异常）；
   - `thread_id='feishu:<session_id>'`、`direction='inbound'`（assistant 行是 `outbound`、`sender_name='小秘书'`）；
   - `modality='voice'` 仅当内容含 `voice message could not be transcribed`，同时 `content_kind='transcript'`；
   - `on_event` 收到该条（对应生产里 `_ingest_event` → SSE `chat.message`）；
   - 水位线 `ingest_cursor` 前进到 `mid`。

**APP（`dispatch.db` 的 `tasks` + `utterances`）**

1. 等 `app/tasks`、`app/utterances` 两条水位线追上源库 `MAX(rowid)` / `MAX(id)`。
2. 插 1 条 `tasks`（`task_id=rt_verify_30s, priority, lane, modality='voice', profile='secretary', text, status='done', created_at`）+ 1 条 `utterances`（`task_id` 同上，`asr_text` / `reply_text` / `voice_text` / `ts`），记 `utterances` 的 `lastrowid = uid`。
3. 同时轮询 3 个 `origin_id`：`rt_verify_30s`、`<uid>:in`、`<uid>:out`。
4. 断言：
   - tasks 行 → `origin_table='dispatch.tasks'`、`content_kind='task'`、`direction='inbound'`、`thread_id='app:<profile>'`；
   - utterances 拆两行 → `:in` 为 `inbound/transcript`（老板原话）、`:out` 为 `outbound/text`（小秘书回复），`modality` 继承 `tasks.modality`；
   - 线程 `chat_threads.msg_count` 按新插入行数自增，`last_at` 前移；
   - `app/tasks` 与 `app/utterances` 水位线分别前进到 `rowid` / `uid`。

**生产侧旁证（不造数据，只核对）**

```sql
-- 有没有积压：三个水位线应等于源库对应最大 id
SELECT source, table_name, watermark, updated_at FROM ingest_cursor;
-- 落库总量与最后一条
SELECT source, COUNT(*), MAX(ingested_at) FROM chat_messages GROUP BY source;
```

水位线的 `updated_at` 只在**有新数据**时前进（无新行时循环直接 break，不写水位线），所以它停在旧时间是正常现象，不能据此判定轮询失效；判据以「`watermark` = 源库最大 id」为准。

尚未验证/遗留：
- ~~真实新消息 30s 可见性~~ → 见验证 13（已通过，隔离库；生产新消息实测需等真实飞书/APP 消息）。
- ~~`token_usage` 0 行~~ → 见验证 14（已通过；下一轮完整选题会仍需确认多员工多轮累加）。
- ~~APP（dispatch tasks/utterances）~~ → 见验证 15（已通过，隔离库；生产水位线 176/115 = 源库最大值，无积压）。
- 会议实时写入（meeting_begin/say/end）随下一场会议验证。
- ~~P1 前端「消息中心」页未做~~ → **已完成（2026-11-12 11:44 构建上线）**：`web/src/pages/MessagesPage.tsx`（489 行）+ `/app/messages` 路由 + 顶栏老板可见入口；会话/消息/统计/导出全部走 `/api/chat/*` 读 `opc.db`，订阅 SSE `chat.message` 增量刷新，非老板 403、`OPC_DB=0` 503 时显示整页空态。前端契约与验收命令见 `.trae/specs/add-project-specification/modules/web.md`。
- **P1 读切换仍未完成**：`/api/state`、`/api/hotspots`、`/api/knowledge`、`/api/works`、`/api/org`、`/api/meeting`、`/api/reports` 仍读内存/文件；`emit()` 只是影子双写，`_restore_history()` 不查库。2026-11-12 线上实测 `/api/knowledge` total 178 而 `knowledge` 表 82 行、`/api/works` 171 而 `contents` 172 行，重启后页面可能少于库内行数。按 §9 的 P1 计划继续时需补 DAO 读方法与 snake→camel 映射。

