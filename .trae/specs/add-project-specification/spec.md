# OPC 一人公司工作台 · 全量工程规格说明书 Spec

## Why

本仓库已是可运行的多子系统工程（`prototype/server.py` 3,960 行、`prototype/index.html` 1,146 行、媒体引擎 `media-engine/server.py` 484 行，另有 Refine 前端、Android 客户端、Remotion 视频工程与 SQLite 过程库 `prototype/opc.db`），但 `.trae/specs/` 为空，唯一的总览文档 `README.md` 只覆盖 `prototype/` 与 `web/` 的一部分：`media-engine/`、`hermes-plugins/`、`android/`、`video-remotion/`、`ep2/ep3` 均未进入任何规格。接口契约、事件类型、审批规则、配置降级逻辑只存在于代码里，导致「文档说法」与「实现行为」已经出现可证实的偏差（见第 15 节）。

本规格以**代码为唯一事实源**，把现状逆向整理为可验收的需求条目 + 接口契约，供后续变更（Hermes 媒体网关 S0~S4、Refine 页面迁移、Android 发版）以此为基线做差异评审。

## What Changes

- 新增 `.trae/specs/add-project-specification/` 下的 `spec.md` / `tasks.md` / `checklist.md`。
- 规格覆盖 13 个能力域（C1~C13），每域给出：需求条目（SHALL）、场景（WHEN/THEN）、接口/数据结构契约、代码出处。
- **不修改任何运行代码、配置、构建产物**；已知不一致只登记、不修复（修复属后续独立变更）。
- 建立「规格条目 → 代码位置」追溯表，使每条需求可被验证脚本或人工复查。

## Impact

- Affected specs: 全部（首次建立）
- Affected code: 无运行时改动；新增 3 个核心文档 + `modules/` 下 5 个模块规格文档
- 影响读者：后续所有 spec 变更、`verify.py` 扩展、README 重写、媒体网关迁移

---

## 0. 事实源与取证方法

| 项 | 说明 |
| --- | --- |
| 事实源优先级 | 运行代码 > 代码内注释 > README / IMPROVEMENT-PLAN / `.trae/documents` |
| 取证方式 | 逐文件阅读 + `grep`/`wc` 统计 + 线上 `http://192.168.31.206:8848/app` 接口与 playwright-cli 浏览器实测；行号以当前**工作区** HEAD `6445703` 为准（C9 前端、C12 路由、C4 事件与 DB 相关条目已按新版复核，其余能力域仍以初版基线 `7e07433` 为准） |
| 标注约定 | 「已核实」＝本次直接读到代码；「推断」＝由代码逻辑推导但未实跑；「未验证」＝仓库外系统 |
| 数据源分工 | Obsidian Vault＝业务配置与人读产物真源；`prototype/opc.db`＝过程数据真源（统一消息、trace、事件、token 等）；`KERNEL` 内存＝热态。详见 [modules/web.md §4.5](./modules/web.md) |

---

## 1. 系统边界与运行拓扑

```
浏览器 ──► prototype/server.py :8848（标准库 HTTP + SSE，单进程多线程）
                │  ├─ /           旧版控制台（index.html + 3D 页面）
                │  ├─ /app        Refine 新版（web/dist 静态托管，含消息中心 / 3D iframe）
                │  ├─ /api/*      REST + SSE（业务列表读内存/文件；统一消息读 SQLite）
                │  ├─ prototype/opc.db（SQLite/WAL：过程数据、统一消息、事件、token）
                │  └─ Hermes 内核 /opt/sefs/hermes/run-hermes.sh（子进程）
                ▼
        Obsidian Vault /opt/sefs/obsidian（业务配置 / 人读产物真源）
                ▲
                │
media-engine :9200（FastAPI，TTS/ASR/数字人视频）◄── hermes-plugins/media-platform（模型工具层）
                ▲
                │ HTTPS /conring/*
Android App（com.boray.voicemcp，MCP Streamable HTTP + WS）
```

- **数据源不再单一**：Vault 管配置与 md，SQLite 管过程数据；`OPC_DB=1` 时 `emit()`/`send()` 影子双写 `opc.db`，但 `/api/state`、`/api/hotspots`、`/api/knowledge`、`/api/works` 等仍读内存/文件（`server.py:1155-1168, 3472-3512`；[modules/web.md §4.5](./modules/web.md)）。读切换属 P1，尚未完成。

- 端口约定（已核实）：工作台 `8848`（`opc.sh:24-25`、`prototype/server.py:3756`）；media-engine `9200`（`media-engine/server.py:39`）。
- 三者独立进程、独立重启；media-engine 故障不阻断工作台主流程（旁路设计，`media-engine/server.py:12` 注释）。

---

## 2. C1 进程与运维（opc.sh）

### Requirement: 后台生命周期管理
系统 SHALL 提供 `opc.sh` 管理服务端进程，子命令集合固定为 `start|stop|restart|status|logs|verify|reload|patrol|vault`（`opc.sh:150-160`）。

#### Scenario: 首次启动
- **WHEN** 执行 `./opc.sh start`
- **THEN** 以 `setsid nohup` 后台启动 `prototype/server.py`，PID 写入 `logs/server.pid`，最多轮询 40×0.25s 探测 `/api/health`（`opc.sh:73-97`）
- **AND** 日志超过 10MB 时启动前切分为 `server.log.1`（`opc.sh:31,81-83`）

#### Scenario: 已启用登录时的存活判定
- **WHEN** `/api/health` 返回 401
- **THEN** `probe()` 仍判定服务存活（`opc.sh:61-71`）

### Requirement: 参数与环境变量
- `OPC_HOST` 默认 `0.0.0.0`、`OPC_PORT` 默认 `8848`、`OPC_LOG_DIR`、`OPC_PYTHON`（`opc.sh:24-29`）。
- `OPC_BASIC_USER` / `OPC_BASIC_PASS` 默认空＝不启用（`opc.sh:35-36`）。
- 启动脚本 SHALL 先 `unset LD_LIBRARY_PATH`，避免污染系统命令（`opc.sh:18`）。
- 注意：`opc.sh:5` 头部注释仍写「默认 127.0.0.1:8848」，与实际 `OPC_HOST=0.0.0.0` 不符（见 D10）。

#### Scenario: 端口被占用
- **WHEN** PID 文件无存活进程但 8848 已可探测
- **THEN** 输出「端口已被其他进程占用」并返回 1，不重复启动（`opc.sh:78-80`）

---

## 3. C2 配置与数据源（Obsidian 配置真源 + SQLite 过程库）

### Requirement: 配置读取与降级
系统 SHALL 从 `OPC_VAULT`（默认 `/opt/sefs/obsidian`）读取配置，读取失败时回退代码内置默认（`prototype/server.py:84-89`）。

| 文件（相对 Vault） | 作用 | 缺失时行为 | 出处 |
| --- | --- | --- | --- |
| `公司/项目配置.md` | 项目清单、调性、阶段、高危工具、交付合同 | 回退 `DEFAULT_PROJECTS` / `DEFAULT_TONE` / `DEFAULT_STAGES` / `DEFAULT_HIGH_RISK` | server.py:259-269, 338-369 |
| `公司/员工名册.md` | 员工编制、负责范围、工具白名单 | 回退 `DEFAULT_AGENT_SPEC`（15 条） | server.py:391-407 |
| `公司/登录凭据.md` | 登录账号/密码 | 无凭据则无法登录（`/api/logininfo.ready=false`） | server.py:88-89, 3313-3315 |
| `公司/.邮箱凭据.md` | 内部邮件账号（`邮箱 \| 姓名 \| 密码`） | 缺失则发信抛异常，由调用方兜底 | server.py:171, 208-240 |
| `动态/runtime.json` | 运行时状态快照，原子写（`tmp` + `os.replace`）；`OPC_DB=1` 时普通事件不再重写，退为日/关机快照 | 缺失时按空状态启动；重启恢复仍只读该文件与 md，**不查过程库** | server.py:973, 996-1056, 1058-1080, 1155-1168 |
| `prototype/opc.db` | SQLite/WAL 过程库：traces / trace_events / messages / approvals / reports / hotspots / knowledge / contents / meetings / chat_threads / chat_messages / token_usage 等 | `OPC_DB=0`（默认代码值）时 `db.store()` 返回 None，全部降级旧路径；生产由 systemd drop-in 设 `OPC_DB=1`、`OPC_INGEST=1` | db.py:18-19, 24-247, 737-750；server.py:267-268, 3513-3542 |
| `OPC/交付物/<traceId>/` | 交付物落盘 | 目录自动创建 | README:73 |
| `OPC/知识库/` | 归档知识页 | 自动创建 | README:74 |

新增环境变量（已核实）：`OPC_DB`（开关）、`OPC_DB_PATH`（库路径，默认与 `db.py` 同目录）、`OPC_INGEST`（飞书/APP 摄取线程）、`OPC_INGEST_INTERVAL`（默认 30s）、`OPC_HERMES_PROFILES`、`OPC_DISPATCH_DB`（`db.py:18-19`、`server.py:267-268`、`ingest.py:20`）。摄入只读取上游库，水位线存 `ingest_cursor`。

#### Scenario: 热加载失败保护
- **WHEN** `POST /api/config/reload` 且新配置缺少必需项目（`wenren/gou/tongxinyuan`）或缺少内置岗位
- **THEN** 返回 400 与缺失清单，旧配置保持不变（`server.py:1056-1063`）

#### Scenario: 交付合同解析
- **WHEN** `项目配置.md` 含「交付合同」表且列数 ≥10
- **THEN** 按项目覆盖默认交付参数（1080×1920 / 30fps / h264 / yuv420p / aac / 48000Hz / 2 声道 / 时长容差 2.5s / 均响度 ≥-45dB / 峰值 ≤-1dB）（`server.py:281-323`）
- **AND** 缺表或解析异常则使用 `DEFAULT_DELIVERY["_default"]`（`server.py:326-335`）

---

## 4. C3 组织与员工模型

### Requirement: 员工对象结构
系统 SHALL 为每名员工维护 `id/name/role/projectId/scope/tools/status/task/avatar/tone/highRisk` 字段（`server.py:1073-1078`）。

- `status` 在热加载时保留旧值，初始为 `idle`。
- `avatar` 固定为 `/avatars/<id>.png`。
- `tone` 取所属项目调性，跨项目中台为「跨项目中台」。
- `highRisk` = 工具白名单 ∩ 高危工具表。

### Requirement: 组织配置读写
| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/org` | 登录 | 返回 `agents`、`policies`、`profilesRoot`、`projects`（含 `all`） |
| GET | `/api/org/policy?key=` | 登录 | 读取制度文件，未知 key 返回 404 |
| GET | `/api/org/audit?limit=` | 登录 | 读取 `org-changes.jsonl` 最近 N 条（上限 500） |
| GET | `/api/org/agent/<aid>` | 登录 | 员工详情 |
| GET | `/api/org/agent/<aid>/skill?name=` | 登录 | 技能详情 |
| POST | `/api/org/*` | 老板 | 写入制度/岗位/人员/性格/技能/记忆，非老板 403 |

出处：`server.py:3414-3455`（读）、`3661-3665`（写）。

#### Scenario: 非老板改配置
- **WHEN** 会话用户 `role != "老板"` 调用 `POST /api/org/*`
- **THEN** 返回 403，`reason` 说明「仅老板可修改制度、岗位、人员、性格、技能与记忆」

### Requirement: 编制数量
- README SHALL 声明当前编制为 17 人（老板 + 首席创意官 + 15 名职能员工）（README:8）。
- 实际名册为 19 个 `id`（含 `boss`、`secretary`、`manual_scout`），Android 离线档案 `count=18`，内置兜底 15 条且不含 `cco`——三者不一致，登记于第 15 节。

---

## 5. C4 事件总线与 SSE

### Requirement: 事件信封
系统 SHALL 以统一信封广播事件：`{"kind","traceId","at","data"}`（`server.py:1146-1147`）。

- 向所有订阅者队列 `put_nowait`，队列满则丢弃该订阅者本次事件（`server.py:1148-1154`）。
- **过程数据已改落库**：`OPC_DB=1` 时所有事件写 `trace_events`，并对 `report/approval/hotspot/knowledge/pipeline` 做表级 upsert；`OPC_DB=0` 时仅 `hotspot/report/approval/knowledge` 四类走 `save_runtime()` 重写 `runtime.json`（`server.py:1155-1168`）。这是影子双写，不改变读路径。

### Requirement: 事件类型枚举（已核实，2026-11-12 重新静态提取）
`agent, approval, chat.message, collection, config-reload, cost-alert, engine-meta, hotspot, knowledge, meeting.end, meeting.speech, meeting.start, message, metrics, pipeline, render-gate, report, rework, sample-review, scene, tech-gate`（`self.emit("…")` 静态提取，共 21 种；新增 `chat.message` 来自摄取线程回调 `server.py:1125-1127`）。
SSE 通道另有首帧 `state` 与 10s `heartbeat`（`server.py:3791-3824`）；新版 `/app` 的「消息中心」消费 `chat.message` 做增量刷新（`web/src/pages/MessagesPage.tsx:195-232`）。

### Requirement: 消息对象与容量
- 消息字段：`id/traceId/type/sender/recipient/at/ttl=20/priority/projectId/payload`（`server.py:1100-1103`）。
- 消息列表超过 4000 条时截断保留最近 2000 条（`server.py:1105-1107`）。
- **不截断 payload**：`send()` 在 `server.py:1109` 直接返回消息对象，全文件无 payload 长度截断逻辑（已核验）。

### Requirement: SSE 端点
#### Scenario: 客户端订阅
- **WHEN** 已登录客户端 `GET /api/events`
- **THEN** 响应 `Transfer-Encoding: chunked`，先推 `state` 快照帧，之后实时事件 + 10s 心跳
- **AND** 断开时从 `subscribers` 移除队列（`server.py:3636-3642`）
- **AND** 每个订阅者队列 `maxsize=500`，满时 `put_nowait` 抛 `QueueFull` 并被静默丢弃（丢帧不阻塞业务线程，`server.py:3616, 1091-1095`）

---

## 6. C5 内容流水线与协作语义

### Requirement: 阶段机
系统 SHALL 使用默认为 `topic → copy → video → review → approved → published → archived` 的阶段序列，另含 `blocked` 状态（`server.py:268`）。
`blocked` 共 5 处写入：预算中止 `server.py:1547`、开工审批超时 `2042`、复核驳回 `2224`、发布审批超时 `2423`、复核驳回 `2348`。

### Requirement: 选题会与冲突投票
#### Scenario: 注入热点触发选题会
- **WHEN** `POST /api/hotspots/inject` 携带 `title/tags/score`
- **THEN** 立即创建 trace 与热点，SSE 广播 `collection`、`meeting.start`、`meeting.speech`、`meeting.end`，产出定稿选题
- **AND** 已有 running trace 时返回 409 `{busy:true, traceId}`（`server.py:3671-3679`）

#### Scenario: CEO 巡检
- **WHEN** `POST /api/patrol`
- **THEN** 巡检 pending 审批与卡点；存在 pending 审批时停止派单（`server.py:1385-1401`）

#### Scenario: 人设串味拦截
- **WHEN** 指令文本包含「串味」或 `persona`
- **THEN** 调用 `persona_injection()` 返回驳回结果（`server.py:3729-3730`）

### Requirement: 人工派单与指令
| 方法 | 路径 | 参数 | 行为 |
| --- | --- | --- | --- |
| POST | `/api/delegate` | `sender/recipient/text` | 管理派单 |
| POST | `/api/commands` | `text` | 分支优先级：`@员工 指令` → 定向调用（`3716-3728`，优先于其他关键词）> 含「串味/persona」→ 人设测试（`3729`）> 含「注入/热点」→ 注入热点（`3731`）> 否则 CEO 汇总回答（`3740-3749`） |

出处：`server.py:3706-3749`。

### Requirement: 协作产物
- 会议纪要：`GET /api/meeting`、`GET /api/meeting/<id>`、`POST /api/meetings`（`server.py:3372-3376, 3680-3688`）。
- 日报/周报/风险单：`GET /api/reports?kind=`（`server.py:3410-3412`）。

---

## 7. C6 审批门与成本护栏

### Requirement: 双审批门
系统 SHALL 在 `production.start`（开工）与 `publish.execute`（发布）两处阻塞，等待老板决策（`server.py:2014-2054, 2393-2433`）。

| 属性 | 规则 | 出处 |
| --- | --- | --- |
| 审批人 | 仅 `role="老板"`，其他人 403 | server.py:3689-3692 |
| 决策值 | `approve` / `reject`，其他 400 | server.py:3693-3696 |
| 超时 | `OPC_APPROVAL_TIMEOUT` 默认 600s | server.py:49, 2031, 2412 |
| 超时后果 | `status=timeout`，条目 `blockedBy=production-gate/publish-gate`，trace 置 `timeout`，升级通知 owner | server.py:2036-2046, 2417-2431 |
| 拒绝发布 | 计入 `blockedPublish`，内容保持未发布 | server.py:2785-2786 |

#### Scenario: 未审批不得发布
- **WHEN** 发布审批未通过或超时
- **THEN** 内容停留 `blocked`，不产生 `published` 交付物

### Requirement: 高危工具
高危工具默认 `publish.execute, deploy.prod, customer.reply, ad.spend`，可被项目配置覆盖（`server.py:269`）。

#### Scenario: 员工工具越权
- **WHEN** 员工调用不在其白名单或属高危的工具
- **THEN** 不直接执行，须走审批门（`verify.py` 断言「高危工具未下发」，verify.py:9 区域说明）

### Requirement: 成本护栏
| 变量 | 默认 | 语义 | 出处 |
| --- | --- | --- | --- |
| `OPC_TOKEN_LIMIT` | 20,000,000 | 单轮 trace 累计 token 上限 | server.py:55 |
| `OPC_CALL_TOKEN_LIMIT` | 2,000,000 | 单次调用 token 上限 | server.py:56 |
| `OPC_COST_LIMIT_USD` | 0（关闭） | 金额阈值 | server.py:57 |

- 标定依据（代码注释，2026-11-07 实测）：单次员工调用 7k–650k token；选题阶段约 2.6M；一轮完整编排 12.4M（`server.py:52-54`）。
- `guard_budget` 调用点：`server.py:1961`、`2056`（开工审批之后）、`2319`（发布审批**之前**）、`2394`；发布审批通过后无二次校验。
- 该函数只检查中止标志（`server.py:1292-1297`），token 累计比对在 `server.py:1240-1260`。

### Requirement: 僵尸 trace 清理
#### Scenario: 超时未收敛
- **WHEN** trace 运行超过 2700s 且无 pending 审批
- **THEN** `_sweep_stale` 将其置为 `timeout`（`server.py:1369-1383`）

---

## 8. C7 交付物、作品档案与样片渲染

### Requirement: 交付物落盘
系统 SHALL 按 trace 落盘以下文件（实现 `prototype/deliverables.py`，与 README:107-108 列出的 8 类不完全一致，见 D9）：

| 位置 | 文件 | 写入条件 | 出处 |
| --- | --- | --- | --- |
| trace 根目录 | `00-index.md`、`06-log.md` | 始终 | deliverables.py:882, 900 |
| 条目目录 | `01-topic.md`、`02-copy.md`、`03-storyboard.md`、`07-data.json`、`08-assessment.md`、`09-review.md` | 始终 | deliverables.py:842-848（路径映射 55-61） |
| 条目目录 | `04-cover.png` | 仅 Pillow 可用 | deliverables.py:850-854 |
| 条目目录 | `05-preview.mp4` | om_render 渲染成功且验收通过 | deliverables.py:57、om_render.py:8 |
| 条目目录 | `05-final.mp4` | 样片过审后定版 | server.py:2276 |

- 三个可选依赖模块（Pillow/ffmpeg 封装、作品档案、om_render）各自独立 `try/except` 导入，任一失败则该模块置 `None` 并打印启动日志，服务继续运行（`server.py:239-257`）。
- 缺 Pillow 时 deliverables 内部降级为仅写 Markdown（`deliverables.py:33-34, 850-852`）。

### Requirement: 作品档案
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/works?page&size&q` | 交付物归一为「名称 + 时间」资产记录，附审批状态 |
| GET | `/api/works/<id>` | 单条详情，未找到 404 |

出处：`server.py:3382-3395`（模块缺失时 500「作品档案模块未加载」）。

### Requirement: 样片渲染与验收门
- 渲染链路：逐镜 props → TTS 分段旁白按实测时长对齐 → Remotion 渲染 → 验收门校验分辨率/时长/音轨。
- TTS 走 stdio JSON-RPC：`OPC_TTS_RUN` 默认 `/opt/sefs/mcp-tts/run.sh`，调用 `tts_synthesize`（`prototype/om_render.py:35, 129-176`）。
- Remotion 以 `NODE_BIN/npx remotion render src/index.tsx Explainer` 执行，工作目录固定 `/opt/sefs/OpenMontage/remotion-composer`（`om_render.py:34, 706-712`）。
- 验收门事件：`render-gate`、`sample-review`、`tech-gate`、`rework`（见 C4 枚举）。
- 分辨率基线 1080×1920、30fps（`server.py:282`；om_render 内另有 W/H/FPS 常量，`om_render.py:40`）。
- 缺 `om_render` 模块时该能力整体降级（`server.py:253-257`）。

#### Scenario: 画面验收不通过
- **WHEN** 成片分辨率/时长/音轨任一不符交付合同
- **THEN** 不登记为正式样片，发 `render-gate` 失败事件并进入返工（`rework`）

### Requirement: 离线内容生产目录（非运行时）
以下目录是人工/半自动的视频制作产物，**不参与服务启动，也无 HTTP 接口**；登记它们是为了让规格覆盖仓库全貌（README 目录树漏列，见 D6）。

| 目录 | 内容 | 是否入库 | 证据 |
| --- | --- | --- | --- |
| `video-remotion/` | 独立的 Remotion 4.0.223 工程（`package.json:7-8`），3 个 composition：`Tongxinyuan`、`TongxinyuanEp3`、`PortraitReel`（`src/Root.tsx:19-44`）；渲染产物在 `out/`，`public/` 不入库 | `node_modules/`、`public/`、`out/` 均排除 | `.gitignore:49` |
| `ep2/` | 第二支口播版素材：`ep2_build_audio.js`、`ep2-timeline.json`、脚本 md、`*.srt`、`backup/` | 入库 | 目录清单 |
| `ep3/` | 第三支制作记录 `同心圆-第三支-口播版-制作记录.md` | 入库 | 目录清单 |
| `storyboard/` | 分镜图 `s01`~`s08`、字幕 `*.srt`/`*.ass`、`v3/`、`voice/`、`voice3/` | 图片与音频按 `.gitignore:52-56` 规则大多不入库 | 目录清单 |
| `preview/watermark/` | 水印方案对比样片（B/C/D 三个 mp4 + `compare.png`） | 排除 | `.gitignore:51` |
| 仓库根 | `同心圆-*.mp4`、`*.srt`、`*.md` 成片与脚本 | mp4 排除、md 入库 | `.gitignore:52-55` |

- 与 C7 的运行期渲染无关：运行期样片由 `om_render.py` 调 `/opt/sefs/OpenMontage/remotion-composer`（`om_render.py:34, 706-712`），**不是**本仓库的 `video-remotion/`。

---

## 9. C8 媒体引擎（media-engine，FastAPI）

### Requirement: 服务定位
media-engine SHALL 作为媒体能力的单实例推理宿主，默认监听 `192.168.21.206:9200`（`server.py:38` 默认值与 `run.sh:20` 一致；本机 `ens2f1np1` 即该地址），工作目录 `media-engine/work`，产物 TTL 默认 24h、单请求体上限 25MB（`media-engine/server.py:38-44`）。
- 可选 Bearer 鉴权：设置 `MEDIA_ENGINE_TOKEN` 后，仅 10 个业务端点校验 `Authorization: Bearer <token>`；`/`、`/health`、`/metrics`、`/files/*`、`/v1/assets/*` 均**免鉴权**（`media-engine/server.py:81-89, 439-453`；`_authorized` 调用点：159/186/278/322/344/361/400/410/419/428）。
- 启动时后台线程每 600s 清理过期产物；`MEDIA_PRELOAD=1` 时预热 TTS/ASR（`media-engine/server.py:93-107, 468-480`）。

### Requirement: 接口契约
| 方法 | 路径 | 鉴权 | 入参 | 出参 | 出处 |
| --- | --- | --- | --- | --- | --- |
| GET | `/` | 免 | — | `{service, version, uptime_sec}` | :111-113 |
| GET | `/health` | 免 | — | `{status, service, version, uptime_sec, tts{...}, asr{loaded,models,default_model,default_language}, video, work_dir}`；TTS 池失败时 `status=degraded` | :116-129 |
| GET | `/metrics` | 免 | — | Prometheus 文本（请求数/失败数/延迟累计） | :132-154 |
| GET | `/v1/voices` | Bearer | — | 音色表 | :157-161 |
| POST | `/v1/tts` | Bearer | `{text, voice?, speed=1.0, format=wav\|mp3\|json, stem?}` | wav/mp3 流（Header：`X-Media-Duration`、`X-Media-Sample-Rate`、`X-Media-Srt`）或 JSON（`job_id/voice/audio_url/audio_path/srt_url/segments_url/segments/duration/sample_rate/segment_count/synth_sec/rtf/workers`） | :184-224 |
| POST | `/v1/asr` | Bearer | multipart `file`/`audio`，或 JSON `audio_base64`/`audio_path`/`audio`（可带 `data:` 前缀），可选 `language/model/initial_prompt` | 文本 + 时间轴；静音时 `silent` | :276-299 |
| POST | `/v1/video/jobs` | Bearer | `{employee_id?/all?/force?/refresh_text?/wait=true/wait_sec=300/text?/voice?}` | 任务视图 | :319-338 |
| GET | `/v1/video/{employee_id}` | Bearer | — | `{id, ready, video, video_full, text, duration, size, source, generated_at}`（不触发生成） | :341-355 |
| POST | `/v1/jobs` | Bearer | `{kind=intro\|briefing, ...}` | 任务视图；未知 kind 400 | :358-394 |
| GET | `/v1/jobs/{job_id}?wait_sec=` | Bearer | 0–300s 长轮询（超出截断） | 任务视图；不存在 404 | :397-405 |
| GET | `/v1/jobs?limit=` | Bearer | 默认 20 | `{count, jobs, queue}` | :408-413 |
| GET | `/v1/avatars` | Bearer | — | 数字人档案 | :416-421 |
| GET | `/v1/profiles?id&dept&project&include_assets` | Bearer | — | 员工档案过滤 | :424-430 |
| GET | `/v1/assets/{subdir}/{name}` | 免 | subdir ∈ `avatars/faces/videos/portraits/intro/live` | 媒体文件（Range 由 FileResponse 处理）；越界/未命中 404 | :439-453 |
| GET | `/files/{name}` | 免 | — | 产物下载（`basename` + `realpath` 前缀校验，越界 404） | :457-465 |

- 「Bearer」= 仅当设置 `MEDIA_ENGINE_TOKEN` 时校验；未设置时全部放行（`media-engine/server.py:81-89`），故上表鉴权列应理解为「可启用鉴权」。
- 10 个 Bearer 端点即 `_authorized` 的 10 个调用点：159/186/278/322/344/361/400/410/419/428。

### Requirement: 推理实现
- TTS：CosyVoice 进程池。模型目录判定顺序：`TTS_MODEL_DIR` 显式指定 > 存在 `Fun-CosyVoice3-0.5B/cosyvoice3.yaml` 时用 CosyVoice3 > 否则 `CosyVoice2-0.5B`（`media-engine/tts_engine.py:29-33`）；`TTS_WORKERS` 代码默认 2（`tts_engine.py:42`），`run.sh` 覆盖为 6（`media-engine/run.sh:13`）。
- 单 worker 实测 RSS 6.6–7.8GB（`media-engine/tts_engine.py:39` 注释，2026-11-11 本机）。
- ASR：faster-whisper，`ASR_MODEL` 默认 `small`，`ASR_LANG` 默认 `zh`（`media-engine/asr_engine.py:15-16`）。
- 启动脚本复用 CosyVoice venv，`OMP/MKL/OPENBLAS_NUM_THREADS` 默认 16，并注入 `ASR_ASSET_URL_BASE=/conring/asr/assets`、`ASR_PUBLIC_BASE=https://ai.boraydata.com`（`media-engine/run.sh:4-18`）。

### Requirement: 与 Hermes 的接线
`hermes-plugins/media-platform` SHALL 以 HTTP 方式把媒体能力暴露为模型工具，自身不加载模型；`MEDIA_ENGINE_URL` 默认 `http://192.168.21.206:9200`，超时 60s（普通）/900s（任务）/900s（TTS）/300s（ASR）（`hermes-plugins/media-platform/__init__.py:17, 32-37`）。

#### Scenario: 引擎不可达
- **WHEN** 插件调用 media-engine 连接失败或超时
- **THEN** 抛 `EngineError`，由调用方决定降级，不影响网关主循环（`hermes-plugins/media-platform/__init__.py:62-75`）

---

## 10. C9 前端（旧版控制台 / Refine 新版 / 3D）

### Requirement: 旧版控制台
`prototype/index.html`（1,146 行）SHALL 提供：组织地图、事件流、员工详情、审批门、流水线看板、交付清单、素材库，以及会议/CEO 日报/项目周报/风险单/热点与知识库五个页签（README:10；实现事实见 `prototype/index.html`）。

- 技术栈：单文件原生 HTML/CSS/JS，**无构建步骤**、无框架依赖。
- 部署：文件随仓库，由 `server.py` 直接托管 `/` 与 `/index.html`（需登录，`server.py:3366-3367`）；改完刷新即生效。
- 数据契约：首屏 `GET /api/state`（`index.html:919,1122`）+ `EventSource('/api/events')` 增量（`index.html:1128`）；审批 `POST /api/approvals/<id>`（`:750`）、指令 `POST /api/commands`（`:1026`）、素材 `GET /api/materials`（`:1105`）。

### Requirement: Refine 新版（2026-11-12 线上 `/app` 新版本）
完整契约见 [modules/web.md](./modules/web.md)。本版共 16 个 `.ts/.tsx` + 1 CSS、5,173 行（新增「消息中心」489 行、「3D 场景」374 行）。本地 `npm run build` 重建的资源与线上 `/app/assets/*` 文件名一致，源码与线上版本一致（已核实）；2026-11-12 功能改动后线上哈希为 `index-BDPN3rhf.js` / `index-yvsrD8mC.css`。

- 技术栈：React 18 + Refine v4 + Vite，`basename="/app"`（`web/src/App.tsx:18`）。
- 资源：`works / hotspots / knowledge / org`（`web/src/App.tsx:23-28`）。
- 路由：9 条显式路径 `/login / /works /hotspots /knowledge /messages /org /office /meeting` + 1 条 `*` 兜底，另有一条无 `path` 的布局包裹路由；未登录重定向 `/login`（`web/src/App.tsx:30-49`）。`/office`、`/meeting` 是 `ScenePage` 用 iframe 嵌入 3D 页面：顶层 `/api/session` 预检、`postMessage` 就绪/跳转/会话过期握手、`?embed=1` 隐藏 3D 页重复 UI，右侧真实数据侧栏分别读 `/api/state`、`/api/meeting` 并订阅 SSE（`web/src/pages/ScenePage.tsx`、`prototype/office3d.html`、`prototype/meeting3d.html`）。
- 首页入口：根路径 `/` 302 → `/app`；`/legacy` 与 `/app/legacy` 回退旧控制台，`OPC_HOME_LEGACY=1` 可整站回退（`prototype/server.py:3425-3435, 3456-3462`）。顶栏为深蓝渐变分组导航，含 3D 快捷入口与「旧版控制台」链接（`web/src/components/Layout.tsx`）。
- 数据契约：`GET /api/{resource}?page&size[&q&project&stage&status&sort]`、`GET /api/{resource}/{id}`；写操作在前端一律 reject（`web/src/dataProvider.ts:34-48, 50-80, 86-90`）。
- **DB 数据访问**：新增「消息中心」直连 `GET /api/chat/threads|messages|stats|export`，全部读 `prototype/opc.db`（snake_case 行、FTS trigram 搜索、`before` 游标分页）；仅老板可见/可访问，后端 403 / 503（`server.py:3257-3263, 3513-3542`、`db.py:585-687`、`Layout.tsx:27,51`、`MessagesPage.tsx:3-34, 289-306`）。`/api/trace/<id>` 在内存缺失时由库补齐（`server.py:3756-3789`），但前端暂无调用方。
- 实时：`/api/state` 10s 轮询（`HomePage.tsx:163`）+ `EventSource('/api/events')`；消息中心另消费 `chat.message` 增量刷新（`MessagesPage.tsx:195-232`）。
- 部署：构建产物 `web/dist` 由 `prototype/server.py` 托管；`/app/assets/*` 长缓存 immutable，其余 `no-cache`，未命中回退 SPA `index.html`（`server.py:3442-3454, 3605-3624`）。线上当前哈希 `index-BDPN3rhf.js` / `index-yvsrD8mC.css` / 懒加载 `MarkdownBody-DdXjBN7k.js`。
- **读路径边界**：`/api/state`、`/api/hotspots`、`/api/knowledge`、`/api/works`、`/api/org`、`/api/meeting`、`/api/reports` 仍读内存/文件（`server.py:3472-3512`），DB 只是影子双写；重启后页面数据可能少于库内行数（见 D12）。

#### Scenario: 未构建新版前端
- **WHEN** `web/dist` 不存在且访问 `/app`
- **THEN** 返回 404 并提示「web/dist 未构建，先在 web/ 执行 npm run build」（`server.py:3613-3614`）

#### Scenario: 统一消息不可用
- **WHEN** 非老板访问 `/api/chat/*`，或 `OPC_DB=0` 使 `KERNEL.db is None`
- **THEN** 分别返回 403 / 503，消息中心显示整页空态；其余页面不受影响（`server.py:3514-3520`、`MessagesPage.tsx:289-306`）

### Requirement: 3D 场景
- 技术栈：Three.js（`importmap` 指向 `/vendor/three.module.js`，无 CDN 依赖），单文件原生 JS（`office3d.html:70`、`meeting3d.html:132`）。
- 路由：`/office3d`、`/meeting3d`（含 `.html` 别名），需登录（`server.py:3368-3371`）。
- 数据契约：`GET /api/state` 或 `GET /api/meeting` 首屏 + `EventSource('/api/events')`（`office3d.html:1932`、`meeting3d.html:1337`）；`office3d` 每 30s 全量刷新（`:1967`），`meeting3d` 每 15s 刷新（`:1402`），SSE 断线由浏览器自动重连（`meeting3d.html:1347`）。
- 部署：与旧版控制台同一静态文件路径，无构建步骤。

---

## 11. C10 Android 客户端

### Requirement: 工程与版本
- 包名 `com.boray.voicemcp`，`compileSdk 35`、`minSdk 26`、`targetSdk 35`，版本 `0.11.3 (code 40)`（`android/app/build.gradle:28-40`）。
- Java 17、纯 framework、`dependencies {}` 为空、零第三方依赖（`android/app/build.gradle:77-85`）。
- 版本号单一来源为 `build.gradle:11-12`；`verifyVersionSync` 断言 `McpConfig.CLIENT_VERSION` 与之一致，不一致直接构建失败（`build.gradle:87-103`）。
- 构建产物：`collectApks` 依赖 `verifyVersionSync + assembleDebug + assembleRelease`，把 debug/release 两个 APK 收进 `android/dist/`，数量不为 2 即失败（`build.gradle:106-130`）；APK 不入 git（`.gitignore:25,31`）。

### Requirement: 通信协议
- MCP Streamable HTTP：`initialize → notifications/initialized → tools/call`，`protocolVersion 2025-03-26`（`android/.../McpClient.java`）。
- 默认入口 `https://ai.boraydata.com/conring/asr/mcp`（`McpConfig.java`）；素材路径 `/conring/asr/assets/*`（`AssetUrls.java`、`PushProcessor.java:92`）。
- 调度：`dispatch` + WS `dispatch-ws/channel/<id>`（`DispatchApi.java`）。

### Requirement: 功能面
录音/指令台、数字员工档案与详情、汇报页、自我介绍播放、实时语音/视频通话、进度简报、待办审批、历史、前台监听服务、开机恢复（`AndroidManifest.xml` 与 `strings.xml`）。

> 注：功能面与协议条目基于代码静态阅读，**未验证**（未在真机回归）；版本/构建条目已按 `build.gradle` 核实。

---

## 12. C11 知识归档与导出

### Requirement: 归档
- 知识条目字段 `id/projectId/traceId/title/summary/body/tags/at`（`server.py:2840-2849`）。
- 列表/详情：`GET /api/knowledge?page&size&q`、`GET /api/knowledge/<id>`（`server.py:3377-3381`）。
- 热点：`id/traceId/title/source/tags/score/fit/at`，`GET /api/hotspots?page&size&q`、`GET /api/hotspots/<id>`（`server.py:3396-3400`）。

### Requirement: Vault 导出
`vault_export.py` SHALL 输出 8 个 Markdown 页面：`00-首页.md`、`公司/组织编制.md`、`公司/项目与调性.md`、`公司/内核与编排.md`、`动态/运行时快照.md`、`动态/事件流.md`、`动态/审批与报告.md`、`动态/知识与KPI.md`，数据取自 `/api/state` 与最近 2 个 trace（`vault_export.py:124,175,203,243,273,375,403,435,485-490`）。

#### Scenario: 服务未启动
- **WHEN** 直接运行 `./opc.sh vault` 但 `/api/state` 不可达
- **THEN** 不中断：捕获异常并打印 `[warn]`，`state=None`，8 个页面照常写出，其中动态类页面落「服务未运行」提示块，进程退出码为 0（`vault_export.py:466-493`、降级写入 `:272-273, 403-404, 435`）

---

## 13. C12 安全与鉴权

### Requirement: 双层鉴权
| 层 | 触发条件 | 失败表现 | 出处 |
| --- | --- | --- | --- |
| Basic 鉴权 | 绑定非回环且设置 `OPC_BASIC_USER/PASS` | 401 | server.py:3113-3134, 3759-3760 |
| 会话 Cookie | `opc_sess`，TTL 7 天 | API 401 / 页面 302 `/login` | server.py:93-95, 3159-3172 |

- 登录失败同 IP 连续 5 次锁定 60s（`server.py:3190-3195`）。
- `POST /api/login`、`/api/logout` 免会话校验（`server.py:3649-3656`）。

### Requirement: 公开资源白名单
`/login`、`/api/logininfo`、`/api/session`、`/vendor/three.module.js`、`/avatars/*.png`、`/faces/*.png`、`/vault/*`、`/app*`、`/materials/*`、`/api/materials` 绕过会话拦截（`server.py:3417-3459`）。`/app` 页面公开但数据接口全部经 `/api` 鉴权；消息中心接口另加「仅老板」判定（`server.py:3257-3263`）。
- 注意：`/api/session` 只是不做会话跳转，未登录时仍返回 401（`server.py:3318-3319`）。

#### Scenario: 路径穿越
- **WHEN** 请求 `/files/../../etc/passwd` 或 `/v1/assets/../`
- **THEN** `realpath` 前缀校验失败返回 404（`media-engine/server.py:439-465`、`server.py:3475-3480`）

### Requirement: 工作台 HTTP 路由全集（GET）
仅实现 `do_GET` / `do_POST` 两个方法（`grep 'def do_' server.py` 只有 `server.py:3304`、`server.py:3644`），其余方法由标准库返回 501（推断，依据为未重写 `do_*`）。
GET 先过 Basic 层（`_require_auth`，`server.py:3113-3134`），再做尾斜杠归一（除 `/` 外均 `rstrip("/")`，`server.py:3306-3307`），然后才按「公开 / 需登录」分流。

| 路径 | 鉴权 | 行为 | 出处 |
| --- | --- | --- | --- |
| `/login`、`/login.html` | 公开 | 返回 `login.html` | server.py:3311-3312 |
| `/api/logininfo` | 公开 | `{ok, ready, file}`，`ready` 表示凭据表是否可读 | server.py:3313-3315 |
| `/api/session` | 公开路径、仍校验会话 | 已登录 `200 {ok,user}`；未登录 `401 {ok:false, login:"/login"}` | server.py:3316-3320 |
| `/` | 公开 | **默认 `302 → /app`**（`Content-Length: 0`）；`OPC_HOME_LEGACY=1` 时不在此分支，落到登录后的旧版分支 | server.py:3430-3437 |
| `/vendor/three.module.js` | 公开 | 3D 依赖 | server.py:3322-3323 |
| `/avatars/<name>.png` | 公开 | 仅 `[a-z_]+\.png`，否则 404 | server.py:3324-3328 |
| `/faces/<name>.png` | 公开 | 同上 | server.py:3329-3333 |
| `/videos/<id>[.talk\|.idle].mp4` | 公开 | 数字人素材 | server.py:3335-3339 |
| `/vault/*` | 公开 | 交付物/纪要/邮件静态文件；`realpath` 越界 404，隐藏目录与 `公司/登录凭据.md` 不发布；支持 `Range`（206） | server.py:3340-3341, 3556-3572 |
| `/app/assets/*` | 公开 | `web/dist` 带哈希资源，长缓存（`immutable`） | server.py:3444-3445, 3605-3624 |
| `/app/legacy` | 公开 | `302 → /legacy`（避免与根路径 `/app` 重定向成环） | server.py:3456-3462 |
| `/app`、`/app/*` | 公开 | Refine 外壳与 SPA 回退（页面无数据，数据仍走 `/api` 鉴权） | server.py:3451-3454, 3606-3614 |
| `/materials/*` | 公开 | 素材库只读文件 | server.py:3356-3357 |
| `/api/materials` | 公开 | 素材库索引 JSON | server.py:3358-3359 |
| `/api/health` | 登录 | `{ok, version:"3.0", engine, hermes, time}` | server.py:3363-3365 |
| `/`、`/legacy`、`/index.html` | 登录 | 旧版控制台；默认只有 `/legacy`、`/index.html` 可达（`/` 已 302 到 `/app`），`OPC_HOME_LEGACY=1` 时 `/` 也返回旧版 | server.py:3479-3480 |
| `/office3d`、`/office3d.html` | 登录 | 3D 办公场景；`?embed=1` 隐藏重复 UI 并与父页 `postMessage` 握手 | server.py:3481-3482 |
| `/meeting3d`、`/meeting3d.html` | 登录 | 3D 会议室；同上，内嵌时 3D 画面铺满 iframe | server.py:3483-3484 |
| `/api/meeting` | 登录 | 会议室实时快照 | server.py:3372-3373 |
| `/api/meeting/<id>` | 登录 | 单场会议详情；缺失 404 | server.py:3374-3376 |
| `/api/knowledge?<query>` | 登录 | 知识归档分页 | server.py:3377-3378 |
| `/api/knowledge/<id>` | 登录 | 单条详情；缺失 404 | server.py:3379-3381 |
| `/api/works?<query>` | 登录 | 作品档案分页；`works` 模块未加载 500 | server.py:3383-3388 |
| `/api/works/<id>` | 登录 | 作品详情；缺失 404 | server.py:3389-3395 |
| `/api/hotspots?<query>` | 登录 | 热点分页 | server.py:3396-3397 |
| `/api/hotspots/<id>` | 登录 | 热点详情；缺失 404 | server.py:3398-3400 |
| `/api/state` | 登录 | 全量快照（`KERNEL.snapshot()`） | server.py:3401-3402 |
| `/api/events` | 登录 | SSE 事件流（见 C4） | server.py:3403-3404 |
| `/api/trace/<id>` | 登录 | trace 回放；缺失 404 | server.py:3405-3409 |
| `/api/reports?kind=` | 登录 | 报告列表，`kind` 为空返回全部 | server.py:3510-3512 |
| `/api/chat/threads?source&limit&before` | 登录 + **仅老板** | 统一会话列表（SQLite `chat_threads`）：`{ok,enabled,threads}` | server.py:3522-3525；db.py:585-606 |
| `/api/chat/messages?thread&source&modality&direction&q&before&limit` | 登录 + **仅老板** | 统一消息流（SQLite `chat_messages` + FTS5 trigram）：`{ok,enabled,messages}`，`id DESC`，`before` 为 id 游标 | server.py:3526-3532；db.py:608-653 |
| `/api/chat/stats?days=` | 登录 + **仅老板** | 7 天聚合：`{ok,enabled,stats:{bySource,byModality,byDay}}` | server.py:3533-3535；db.py:655-670 |
| `/api/chat/export?start&end&limit` | 登录 + **仅老板** | 导出 JSON 对象 `{ok,enabled,count,messages}`（上限 20000） | server.py:3536-3539；db.py:672-687 |
| `/api/chat/*` 其他 | 登录 + **仅老板** | `404 {"ok":false,"error":"not found"}` | server.py:3542 |
| `/api/org` | 登录 | 员工 + 制度 + 画像根 + 项目清单（含虚拟项 `all/全公司`） | server.py:3544-3553 |
| `/api/org/policy?key=` | 登录 | 制度文件正文；未知 key 404 | server.py:3424-3434 |
| `/api/org/audit?limit=` | 登录 | 审计日志，默认 50、上限 500 | server.py:3435-3443 |
| `/api/org/agent/<aid>[/skill?name=]` | 登录 | 员工详情 / 单技能详情；未知形态 404、参数错误 400 | server.py:3444-3455 |
| 其余 | — | `404 {"error":"not found"}` | server.py:3586 |

> 统一消息前置条件：`/api/chat/*` 先过 `_owner_only()`（非老板 403），再判 `KERNEL.db`（未启用 503 `{"ok":false,"enabled":false,"error":"过程库未启用"}`）；DAO 异常统一 500（`server.py:3514-3542`）。`POST /api/commands` 与 `Kernel.send()` 是 Web 侧写入 `chat_messages` 的唯一在线入口（`server.py:1129-1143, 1171-1190, 3894-3932`）。

### Requirement: 工作台 HTTP 路由全集（POST）
`do_POST` 不做 `unquote`/尾斜杠归一（与 GET 不同，`server.py:3645`）。先过 Basic 层，再分「免会话 / 需登录」；未登录统一 `401 {"error":"unauthorized","login":"/login"}`（`server.py:3657-3659`）。

| 路径 | 鉴权 | 入参要点 | 行为 / 错误码 | 出处 |
| --- | --- | --- | --- | --- |
| `/api/login` | 免会话 | `account`/`password`（`user` 兼容） | 成功下发 `opc_sess`（7 天）；密码错 401、连续失败 5 次锁定 60s → 429；凭据表缺失或无密码列 → 503 | server.py:3649-3650, 3174-3214 |
| `/api/logout` | 免会话 | — | 清会话并回 `Max-Age=0` Cookie | server.py:3651-3656 |
| `/api/org/*` | 登录 + 仅老板 | 见 C3 | 非老板 403 | server.py:3661-3665 |
| `/api/patrol` | 登录 | — | CEO 巡检；参数错误 400 | server.py:3666-3670 |
| `/api/hotspots/inject` | 登录 | `title`/`tags`/`score` | 成功返回 `traceId`；有在跑 trace 时 409（带 `busy:true` 与原 `traceId`）；参数错误 400 | server.py:3671-3679 |
| `/api/meetings` | 登录 | 同上，语义为「开选题会」 | 返回 `traceId` + `meeting:"daily_topic"`；409/400 同上 | server.py:3680-3688 |
| `/api/approvals/<id>` | 登录 + 仅老板 | `decision` 必取 `approve`\|`reject` | 非老板 403；非法 decision 400；审批不存在 404 | server.py:3689-3700 |
| `/api/config/reload` | 登录 | — | 热加载 Obsidian 配置；校验失败 400 并保留旧值 | server.py:3701-3705 |
| `/api/delegate` | 登录 | `sender`/`recipient`/`text` | 管理派单；参数错误 400 | server.py:3706-3711 |
| `/api/commands` | 登录 | `text` 非空 | 分支优先级见 C5；空指令 400 | server.py:3712-3749 |
| 其余 | — | — | `404 {"error":"not found"}` | server.py:3750 |

### Requirement: 启动告警
- **WHEN** 绑定非回环且未设 `OPC_BASIC_USER`
- **THEN** 启动日志打印「局域网内任何人可访问与审批」警告（`server.py:3759-3760`）

---

## 14. C13 构建、发布与同步

### Requirement: 自动化同步
`sync-github.sh` SHALL 由 crontab 每小时执行 `git add -A → commit → fetch → rebase → push`，日志写 `logs/github-sync.log`（`sync-github.sh:13, 39-75`）。
- 认证方式为 SSH 部署密钥 `~/.ssh/opc_github_ed25519`（`GIT_SSH_COMMAND`，`sync-github.sh:5, 20`），**不使用 GitHub 代理**（已核验全仓 `*.sh` 无代理配置）。

### Requirement: 产物边界
`.gitignore`（共 58 行）SHALL 排除：运行时日志/缓存、`media-engine/work/`（:34）、`video-remotion/public/`（:49）、`preview/`（:51）、`*.mp4/*.wav/*.jpg`（:52-55）、`*.png`（:56）、APK（:25,31）；仅保留 Android 启动图标等例外（:57-58）。

### Requirement: 验收基线
`prototype/verify.py` SHALL 覆盖：登录会话、快照、编制与三项目分布、SSE、热点注入时延、选题会、审核、双审批门、未审不发布、人设串味、trace 回放、归档率、审批耗时、高危工具未下发（`verify.py:156-347`）。
- 其中「17 名员工编制」为硬断言（`verify.py:168`）。

---

## 15. 已知不一致与待确认项（只登记，不在本变更修复）

| # | 现象 | 证据 | 影响 | 状态 |
| --- | --- | --- | --- | --- |
| D1 | 文档头写 `OPC_HERMES_TIMEOUT` 默认 180s，实际 600s | `server.py:14` vs `server.py:58` | 运维预期偏差 | 已核实 |
| D2 | 内置兜底名册 15 条且无 `cco`，而流水线强依赖 `cco`；名册缺失时可能 KeyError | `server.py:391-407`、`verify.py:168` | 无 Vault 环境下启动/验收失败 | 推断（未实跑） |
| D3 | 编制数量三处不一致：README 17 人 / 名册 19 个 id / Android 档案 18 | `README.md:8`、`公司/员工名册.md`、`android/.../employees.json` | 文档与前端展示口径不一 | 已核实 |
| D4 | `TTS_WORKERS` 代码默认 2、`run.sh` 默认 6 | `tts_engine.py:42` vs `run.sh:13` | 并发与内存差异（每 worker 6.6–7.8GB） | 已核实 |
| D5 | ~~README 称新版前端「热点/知识库两页」~~ **已修复**：2026-11-12 校对后 README 改为 8 个业务页 + 登录，并补齐 `/app/assets`、DB 接口与 SPA 回退说明；实际为 9 条显式路由（新增消息中心、Office 3D、Meeting 3D） | `README.md:86-88` vs `web/src/App.tsx:30-49` | 文档滞后已消除 | 已修复（2026-11-12） |
| D6 | README 目录树未列 `media-engine/`、`hermes-plugins/`、`android/`、`video-remotion/`、`ep2/ep3/`、`storyboard/` | `README.md:22-39` vs 仓库根目录 | 新人不了解全貌 | 已核实 |
| D7 | `/conring` 反代配置不在仓库内 | 仅见 Android 端路径与 media-engine 环境变量 | 外部依赖不可追溯 | 未验证（仓库外） |
| D8 | Android 版本以 `build.gradle` 0.11.3 为准，`dist/` 内历史 APK 可能为 0.11.2 | `android/app/build.gradle:11-12` | 发版核对点 | 已核实（dist 内容未逐一核对） |
| D9 | README 称交付物为 8 类，实现实际写 11 个文件（多 `08-assessment.md`、`09-review.md`、`05-final.mp4`，且 `04-cover.png` / `05-preview.mp4` 为条件写入） | `README.md:107-108` vs `deliverables.py:5-9, 55-61, 842-854`、`server.py:2276` | 交付物验收口径不一致 | 已核实 |
| D10 | `opc.sh` 头部注释默认 `127.0.0.1:8848`，实际 `OPC_HOST` 默认 `0.0.0.0` | `opc.sh:5` vs `opc.sh:24` | 暴露面认知偏差（默认监听所有网卡） | 已核实 |
| D11 | ~~媒体引擎地址三处口径不一致~~ **已修复**：统一为 `192.168.21.206:9200` —— `plugin.yaml` 文案、插件 docstring、引擎 `MEDIA_HOST` 默认值均改为该地址，与 `run.sh` 一致 | `plugin.yaml:8`、`__init__.py:1, 32`、`media-engine/server.py:38`、`media-engine/run.sh:20` | 已消除「无环境变量时连错回环」的部署风险 | 已修复（2026-11-11） |
| D12 | 「数据访问已改成 db」只覆盖过程数据：`/api/chat/*` 与 `/api/trace/<id>` 读 `opc.db`，但 `/api/state`、`/api/hotspots`、`/api/knowledge`、`/api/works`、`/api/org`、`/api/meeting`、`/api/reports` 仍读内存/文件；`emit()` 是影子双写，`_restore_history()` 不查库，重启后页面数据可能少于库内行数 | `server.py:1155-1168, 996-1056, 3472-3512, 3513-3542, 3756-3789`；线上实测 `/api/knowledge` total 178 vs `knowledge` 表 82 行、`/api/works` 171 vs `contents` 172 行 | 读切换（P1）未完成；按「全站已读库」做验收会得到错误结论 | 已核实（2026-11-12 线上 + 只读查库） |
| D13 | ~~`PROCESS-DATA-DB-PLAN.md` 记「P1 前端消息中心页未做」~~ **已修复**：本次校对把该条改为「已完成并上线」，并注明 P1 后端读切换仍待排期；`MessagesPage.tsx` 489 行，11:44 构建 | `PROCESS-DATA-DB-PLAN.md` §13 遗留清单 vs `web/src/pages/MessagesPage.tsx:1-489` | 计划文档状态滞后已消除 | 已修复（2026-11-12） |

---

## 16. 追溯表（规格条目 → 代码）

| 能力域 | 主要代码位置 | 验收手段 |
| --- | --- | --- |
| C1 进程运维 | `opc.sh:24-160` | `./opc.sh status` / `logs` |
| C2 配置 | `prototype/server.py:83-372, 973-1080, 1155-1168`、`db.py:18-19, 24-247` | `./opc.sh reload`；只读查 `opc.db` 表行数 |
| C3 组织 | `prototype/server.py:1073-1078, 3544-3585, 3843-3847` | `/api/org` 手工回归 |
| C4 事件总线 | `prototype/server.py:1125-1191, 3791-3824` | `curl -N /api/events`；观察 `chat.message` |
| C5 流水线 | `prototype/server.py:1793-2005, 3853-3932` | `./opc.sh verify` |
| C6 审批门 | `prototype/server.py:2014-2054, 2393-2433, 3871-3882` | `verify.py` 审批用例 |
| C7 交付物/渲染 | `prototype/deliverables.py`、`om_render.py`、`works.py` | 交付目录人工核对 |
| C8 媒体引擎 | `media-engine/*.py`、`hermes-plugins/media-platform/` | `/health`、`/metrics` |
| C9 前端 | `web/src/**`（4,495 行）、`prototype/*.html`；DB 契约 `db.py:585-687`、`server.py:3513-3542` | 页面手工回归 + 线上 `/app` 接口冒烟（见 [modules/web.md §9](./modules/web.md)） |
| C10 Android | `android/**` | Gradle 构建 + 真机 |
| C11 归档导出 | `vault_export.py`、`server.py:2840-2849, 3477-3481, 3496-3500` | `./opc.sh vault` |
| C12 安全 | `server.py:3211-3321, 3513-3542, 3943` | 未登录 / 非老板 / `OPC_DB=0` 三类用例 |
| C13 构建同步 | `.gitignore`、`sync-github.sh`、`verify.py` | `./opc.sh verify` |

---

## ADDED Requirements（本变更自身）

### Requirement: 规格可追溯
每一节 SHALL 至少给出一个可点击的代码位置（文件:行号），且数字类断言必须能在对应代码中复现。

#### Scenario: 复核规格
- **WHEN** 读者按追溯表打开代码
- **THEN** 能找到与该条目一致的常量、路由或函数，无「文档说有、代码没有」的条目

### Requirement: 不改运行代码
本变更 SHALL 不修改 `prototype/`、`media-engine/`、`web/src/`、`android/`、`video-remotion/` 下任何文件。

#### Scenario: 变更范围审计
- **WHEN** Task 1~12（规格撰写与 web 校对）完成后执行 `git status`
- **THEN** 仅新增/修改规格文档与 `README.md` / `PROCESS-DATA-DB-PLAN.md` 的 web、状态条目，不触碰运行代码

#### Scenario: 功能改动阶段（Task 13）
- **WHEN** 2026-11-12 执行「首页跳 /app、顶栏改版、3D 修复与数据对接」
- **THEN** 允许改运行代码（`prototype/server.py`、`prototype/login.html`、两个 3D HTML、`web/src/**`），但必须 `npm run build` + 重启服务 + playwright-cli 实测，并同步回规格文档

---

## 17. 模块规格索引

总规格按能力域（C1~C13）描述「系统做什么」；模块规格按目录描述「一个模块内部怎么组织、对外契约是什么、怎么验收」。两者交叉引用，不重复维护同一事实。

| 模块 | 文档 | 覆盖范围 | 规模 |
| --- | --- | --- | --- |
| `prototype/` | [modules/prototype-server.md](./modules/prototype-server.md) | HTTP 路由、会话鉴权、SSE、编排内核、交付物、配置项 | `server.py` 3,960 行 + 子模块（过程库 `db.py` / `ingest.py` / `import_runtime.py` 见 `PROCESS-DATA-DB-PLAN.md`） |
| `media-engine/` | [modules/media-engine.md](./modules/media-engine.md) | TTS/ASR/视频任务接口、鉴权、TTL、指标、环境变量 | 4 个 Python 文件 2,005 行 |
| `web/` | [modules/web.md](./modules/web.md) | Refine 路由、dataProvider/authProvider、页面直连接口、**消息中心 DB 契约**、深蓝顶栏、3D iframe 握手与真实数据侧栏、构建托管 | 5,173 行 |
| `android/` | [modules/android.md](./modules/android.md) | 构建产物、MCP/dispatch/WS 契约、配置项、权限、离线降级 | 52 个 Java 文件 16,272 行 |
| `hermes-plugins/` | [modules/hermes-plugins.md](./modules/hermes-plugins.md) | 插件注册契约、8 个工具到引擎接口的映射、错误语义 | `__init__.py` 323 行 |

维护约定：

1. 模块内行为变更时，先改对应 `modules/*.md`，再改 `spec.md` 的能力域条目（若影响系统级契约）。
2. 新增模块目录时，必须同时新增 `modules/<module>.md` 并在本表登记。
3. 模块文档中的行号以改动后的工作区为准，引用前用 `grep -n` 复核。
