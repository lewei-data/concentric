# 模块规格 · web（Refine 新版控制台 `/app`）

> 上级文档：[spec.md](../spec.md)。本文件只描述 `web/`。
> 校对基准：2026-11-12 线上 `http://192.168.31.206:8848/app`（本机 `:8848`，`web/dist` 于本次功能改动后重建，当前产物 `index-C21ZRNYx.js` / `index-q43a5DfF.css` / `MarkdownBody-C9D22Bk_.js`），工作区 HEAD `71b82f5`；运行进程实测 `OPC_DB=1`、`OPC_INGEST=1`（`/etc/systemd/system/opc-workbench.service.d/db.conf`）。
> 规模：`web/src` 下 18 个 `.ts/.tsx` + 1 个 CSS，共 6,300 行（`wc -l`）：页面 3,417 行、组件/数据/鉴权/入口 502 行、样式 2,381 行。
> 本版补齐内容：新增「消息中心」「3D 场景」两页与三条路由（`/messages`、`/office`、`/meeting`）、统一消息的 **DB 数据访问契约**、老板权限与降级、深蓝渐变顶栏、3D iframe 的会话预检/postMessage 握手/真实数据侧栏，以及「创意灵感」（CCO）与「运维监控」（CTO）两页与配套后端聚合接口。

## 0. 校对结论（与上一版规格的差异）

| 项 | 上一版规格（HEAD `7e07433`） | 线上新版本（HEAD `4399383`） | 证据 |
| --- | --- | --- | --- |
| 源码规模 | 13 个 `.ts/.tsx` + 1 CSS，3,488 行 | 18 个 `.ts/.tsx` + 1 CSS，6,300 行（+2,812） | `wc -l web/src/**` |
| 新增页面 | 无 | `MessagesPage.tsx` 489 行（消息中心，读 SQLite）、`ScenePage.tsx` 374 行（3D 场景 + 数据侧栏）、`InspirationPage.tsx` 255 行、**`OpsPage.tsx` 443 行** | `web/src/pages/` |
| 显式路由 | 6 条 + `*` 兜底 | 11 条 + `*` 兜底（新增 `/messages`、`/office`、`/meeting`、`/inspiration`、`/ops`） | `App.tsx:30-51` |
| 导航 | 侧边导航壳 38 行 | 深蓝渐变顶栏 105 行：8 项主导航（含创意灵感 / 运维监控）、老板专属消息中心、3D 快捷入口、旧版回退、用户身份卡 | `Layout.tsx:1-105`、`styles.css` |
| 数据访问 | 全部 `/api/*` 走内存 / Obsidian 文件 | **统一消息与过程数据改读 `prototype/opc.db`**；新增 `/api/inspiration` / `/api/ops` 只读聚合；其余页面仍走内存 / 文件（影子双写） | `server.py:3513-3542`、`insights.py`、`ops_monitor.py`、`db.py:585-687` |
| 实时通道 | 仅首页 SSE | 消息中心订阅 `chat.message`；3D 场景、创意灵感、运维监控侧栏均订阅 `/api/events` 增量刷新 | `MessagesPage.tsx:195-232`、`ScenePage.tsx`、`InspirationPage.tsx`、`OpsPage.tsx` |
| 首页入口 | 根路径返回旧控制台 | `/` 302 → `/app`；`/legacy` 与 `/app/legacy` 回退旧版；`OPC_HOME_LEGACY=1` 可整站回退 | `server.py:3425-3435, 3456-3462`、`README.md` |
| 处置能力 | 仅首页指令/审批 | 运维监控页提供仅老板可用的 `POST /api/ops/recover`（巡检派单 / 热加载 / 审批），带二次确认 | `server.py` do_POST、`OpsPage.tsx` |
| 构建产物 | 未登记哈希 | 当前 `index-C21ZRNYx.js` / `index-q43a5DfF.css` / 懒加载 `MarkdownBody-C9D22Bk_.js`（以 `web/dist/index.html` 为准） | `web/dist/index.html`、`web/dist/assets/` |

> **口径校正（重要）**：「数据访问已经改成了 db」适用于**过程数据 / 统一消息**（`chat_threads`、`chat_messages`、`trace_events` 等），不是整个前端的所有列表。实测 `/api/state`、`/api/hotspots`、`/api/knowledge`、`/api/works`、`/api/org`、`/api/meeting`、`/api/reports` 仍读内存快照、Obsidian md 或交付物目录；`server.py:1155-1168` 是「DB 影子双写」，`_restore_history()` 仍以 `runtime.json` / md 恢复，读切换（P1）尚未完成。详见 §4.5 与 §8。

> **一致性核验（已核实）**：`web/src` 重新执行 `npm run build`（`tsc --noEmit && vite build`）得到与线上完全相同的资源文件名，改动前三个文件 MD5 逐一相等；本次功能改动后线上已切到新哈希 `index-C21ZRNYx.js` / `index-q43a5DfF.css`，本地 `web/dist` 与线上一致。因此 `/app` 始终由当前 `web/src` 构建产物托管，**不存在「线上有、仓库里缺」的前端代码**。

## 1. 模块定位

`web/` 是工作台的**新版单页控制台**：Refine v4 提供资源列表/详情，React 18 + Vite 5 构建，产物由 `prototype/server.py` 以 `/app` 路径托管（`server.py:3442-3454, 3605-3624`）。

- 根路径 `/` 默认 302 跳 `/app`；旧版 `prototype/index.html` 由 `/legacy`、`/index.html` 提供，`/app/legacy` 302 → `/legacy`；`OPC_HOME_LEGACY=1` 重启后根路径直接回退旧版（`server.py:3425-3435, 3456-3462`）。
- 页面共 10 个业务页 + 1 个登录页：总览、创意灵感、作品档案、热点、知识库、消息中心、组织配置、运维监控、Office 3D、Meeting 3D。
- **只读为主**：`dataProvider` 的 `create/update/deleteOne` 一律 reject；写操作只存在于组织配置页与首页指令/审批（`dataProvider.ts:86-90`）。
- **两套数据后端并存**：业务列表/快照走 `KERNEL` 内存与 Vault 文件；消息中心与 trace 回放补齐走 SQLite（`opc.db`）。

## 2. 目录与文件清单（`wc -l`）

| 文件 | 行数 | 职责 |
| --- | ---: | --- |
| `src/main.tsx` | 10 | React 挂载入口 |
| `src/App.tsx` | 57 | Refine 初始化、4 个 resource、12 条路由 |
| `src/authProvider.ts` | 77 | 登录/登出/会话检查/401 处理 |
| `src/dataProvider.ts` | 90 | 列表/详情数据适配，写操作拒绝 |
| `src/types.ts` | 151 | 领域类型（Agent / Work / Hotspot / Knowledge / OpcState / 事件等） |
| `src/vite-env.d.ts` | 1 | Vite 类型声明 |
| `src/components/Layout.tsx` | 105 | 深蓝渐变顶栏、8 项主导航、老板可见性、旧版回退、用户身份卡 |
| `src/components/MarkdownBody.tsx` | 11 | Markdown 渲染（`react-markdown` + `remark-gfm`），按需懒加载 |
| `src/pages/HomePage.tsx` | 656 | 驾驶舱：状态快照、SSE 事件流、指令、审批 |
| `src/pages/WorksPage.tsx` | 388 | 作品档案列表 + 详情 |
| `src/pages/OrgPage.tsx` | 428 | 组织配置：制度/岗位/人员/技能/记忆 |
| `src/pages/MessagesPage.tsx` | **489** | **消息中心：飞书 / APP / Web 三源统一消息（DB 只读）** |
| `src/pages/HotspotsPage.tsx` | 157 | 热点列表 + 详情 |
| `src/pages/KnowledgePage.tsx` | 165 | 知识库列表 + 详情 |
| `src/pages/ScenePage.tsx` | **374** | **3D 办公区 / 会议室 iframe 嵌入、会话预检、postMessage 握手、真实数据侧栏（`/api/state` / `/api/meeting` + SSE）、20s 超时与重试** |
| `src/pages/InspirationPage.tsx` | **255** | **创意灵感：热点分析 / 视频分析 / 蒸馏启发三类线索，筛选、搜索、展开要点、跳原文** |
| `src/pages/OpsPage.tsx` | **443** | **运维监控：系统状态、任务执行、视频生成、告警与恢复运行动作** |
| `src/pages/LoginPage.tsx` | 62 | 登录表单 |
| `src/styles.css` | 2,381 | 全站样式（含消息中心 / 深蓝顶栏 / 3D 场景 / 灵感 / 运维） |

## 3. 技术栈与构建

| 项 | 值 | 出处 |
| --- | --- | --- |
| Refine | `@refinedev/core` / `@refinedev/react-router-v6` `^4.0.0` | `package.json:12-13` |
| React | `^18.3.1` | `package.json:14-15` |
| 路由 | `react-router-dom ^6.0.0` | `package.json:17` |
| Markdown | `react-markdown ^10.1.0` + `remark-gfm ^4.0.1` | `package.json:16,18` |
| 构建 | `tsc --noEmit && vite build`，输出 `dist/` | `package.json:8`、`vite.config.ts:7-9` |
| 基路径 | `base: "/app/"`；SPA `basename="/app"` | `vite.config.ts:5`、`App.tsx:18` |
| 开发代理 | `/api`、`/login.html`、`/avatars` → `http://127.0.0.1:8848` | `vite.config.ts:10-17` |
| 代码分割 | `MarkdownBody` 用 `React.lazy` 动态 import | `KnowledgePage.tsx:5-6` |

## 4. 路由与数据契约

### 4.1 路由表

| 路径（浏览器） | 组件 | 鉴权 | 数据来源 | 出处 |
| --- | --- | --- | --- | --- |
| `/app/login` | `LoginPage` | 公开 | `POST /api/login` | `App.tsx:31` |
| `/app/` | `HomePage` | `Authenticated` | `/api/state`（内存快照） | `App.tsx:41` |
| `/app/inspiration` | `InspirationPage` | 同上 | **`/api/inspiration`**（热点简报 / 视频拆解 / 知识库 / 评审会聚合） | `App.tsx:42` |
| `/app/ops` | `OpsPage` | 同上 | **`/api/ops`** + `POST /api/ops/recover`（恢复动作仅老板） | `App.tsx:43` |
| `/app/works` | `WorksPage` | 同上 | `/api/works`（交付物目录） | `App.tsx:44` |
| `/app/hotspots` | `HotspotsPage` | 同上 | `/api/hotspots`（内存 / md） | `App.tsx:45` |
| `/app/knowledge` | `KnowledgePage` | 同上 | `/api/knowledge`（内存 / md） | `App.tsx:46` |
| `/app/messages` | `MessagesPage` | 同上 + **仅老板可见入口** | **`/api/chat/*`（SQLite）** | `App.tsx:47` |
| `/app/org` | `OrgPage` | 同上 | `/api/org*`（profile / md） | `App.tsx:48` |
| `/app/office` | `ScenePage scene="office"` | 同上 | iframe `/office3d?embed=1` + `/api/state` 侧栏 | `App.tsx:49` |
| `/app/meeting` | `ScenePage scene="meeting"` | 同上 | iframe `/meeting3d?embed=1` + `/api/meeting` 侧栏 | `App.tsx:50` |
| `/app/*` 其他 | 重定向到 `/` | — | — | `App.tsx:52` |

未登录时 `Authenticated` 以 `<Navigate to="/login" replace />` 回登录页（`App.tsx:32-37`）。

**3D 嵌入契约（2026-11-12 修复）**：顶层先 `GET /api/session` 预检，通过后挂载 iframe；3D 页 `?embed=1` 隐藏自带 HUD/记录栏/发起会议按钮并铺满画面，`window.parent.postMessage({source:'opc-3d',type:'opc:scene-ready'|'opc:navigate'|'opc:unauthorized'})` 与父页握手；父页按 `event.origin` 校验同源，收到就绪才移除加载遮罩，收到 `opc:navigate` 用 React Router 切场景（避免 `top.location.href` 整页刷新），收到 `opc:unauthorized` 跳 `/app/login`。3D 页 `/api/state`、`/api/meeting` 的 401 不再被当成空数据。右侧数据栏直接读真实接口并订阅 SSE，10–15s 兜底轮询；20s 未就绪给出重试。独立窗口打开（不带 `embed=1`）仍保留完整 3D UI。

### 4.2 数据提供者（`dataProvider.ts`）

| 方法 | 映射 | 约定 |
| --- | --- | --- |
| `getList` | `GET /api/{resource}?page&size[&q&project&stage&status&sort]` | 响应 `{ok,total,page,size,items}`，取 `items/total`（`:50-70`） |
| `getOne` | `GET /api/{resource}/{id}` | 直接返回记录对象（`:72-80`） |
| `create/update/deleteOne` | — | 一律 `Promise.reject(new Error("not implemented"))`（`:86-90`） |
| 401 处理 | — | `window.location.assign("/app/login")`（`:20-27`） |
| 过滤字段白名单 | `q/project/stage/status/sort` | 其他字段忽略（`:34-48`） |

resource 仍为 4 个：`works`、`hotspots`、`knowledge`、`org`（`App.tsx:23-28`）。注意 `org` 的 `/api/org` 返回 `{agents,policies,...}`，**不符合 `{items,total}` 契约**；当前 `OrgPage` 用自有 `api()` 直取，未触发通用 `getList`（§8）。

### 4.3 页面级直连接口（非 Refine）

| 页面 | 调用 | 用途 | 出处 |
| --- | --- | --- | --- |
| HomePage | `GET /api/state` | 首屏快照 | `HomePage.tsx:81-89` |
| HomePage | `EventSource /api/events` | 实时事件；忽略 `heartbeat`，`state` 帧覆盖快照，其余入 feed（最多 80 条） | `HomePage.tsx:176-202` |
| HomePage | `POST /api/commands` | 下发自然语言指令（同时写 `chat_messages`） | `HomePage.tsx:246-253`、`server.py:1129-1143` |
| HomePage | `POST /api/approvals/{id}` | 老板审批（approve/reject） | `HomePage.tsx:282-290` |
| OrgPage | `GET /api/org` | 员工/制度/项目 | `OrgPage.tsx:75` |
| OrgPage | `GET /api/org/agent/{id}` | 员工详情 | `OrgPage.tsx:93,182` |
| OrgPage | `GET /api/org/agent/{id}/skill?name=` | 技能正文 | `OrgPage.tsx:169` |
| OrgPage | `GET /api/org/policy?key=` | 制度正文 | `OrgPage.tsx:107` |
| OrgPage | `GET /api/org/audit?limit=30` | 变更审计 | `OrgPage.tsx:119` |
| OrgPage | `POST /api/org/agent/{id}` | 保存岗位/人员/性格 | `OrgPage.tsx:126` |
| OrgPage | `POST /api/org/agent/{id}/soul` | 保存人格 | `OrgPage.tsx:145` |
| OrgPage | `POST /api/org/agent/{id}/memory` | 保存记忆 | `OrgPage.tsx:156` |
| OrgPage | `POST /api/org/agent/{id}/skill` | 保存技能 | `OrgPage.tsx:181` |
| OrgPage | `POST /api/org/agent/{id}/config` | 保存个人配置 | `OrgPage.tsx:194` |
| OrgPage | `POST /api/org/policy` | 保存制度 | `OrgPage.tsx:205` |
| OrgPage | `POST /api/org/reload` | 重载配置 | `OrgPage.tsx:399` |

### 4.4 DB 数据访问（本版新增，消息中心）

**启用条件**：`OPC_DB=1`（`db.py:18-19`，路径 `OPC_DB_PATH` 默认 `prototype/opc.db`）、`OPC_INGEST=1`（`server.py:267-268, 988-992`）。SQLite + WAL、单写连接 + 只读连接；`db.store()` 未启用返回 `None`（`db.py:737-750`）。摄取线程只读轮询飞书 `state.db` 与 APP `dispatch.db`，间隔 30s，按 `ingest_cursor` 水位增量（`ingest.py:20, 73, 223, 242-250`）。

**表结构**：`chat_threads`（`db.py:188-197`）、`chat_messages`（`:199-221`，`UNIQUE(source,origin_table,origin_id)`）、FTS5 `trigram` 虚表 + 3 个同步触发器（`:223-238`）。列名全部 **snake_case**，`participants/asr/meta` 为 JSON 文本，DAO 读取时反序列化（`db.py:598-603, 644-650`）。

| 前端调用 | 方法/路径 | 参数 | 响应 | 后端 / DAO |
| --- | --- | --- | --- | --- |
| 会话列表 | `GET /api/chat/threads` | `source,limit=50,before`（`last_at` 游标） | `{ok,enabled,threads:[...]}` | `server.py:3522-3525` → `db.py:585-606` |
| 消息流 | `GET /api/chat/messages` | `thread,source,modality,direction,q,before,limit=50` | `{ok,enabled,messages:[...]}`，按 `id DESC` | `server.py:3526-3532` → `db.py:608-653` |
| 统计 | `GET /api/chat/stats?days=7` | `days`（1–90） | `{ok,enabled,stats:{bySource,byModality,byDay}}` | `server.py:3533-3535` → `db.py:655-670` |
| 导出 | `GET /api/chat/export?limit=20000` | `start,end,limit` | `{ok,enabled,count,messages}`（JSON 对象，前端另存为文件） | `server.py:3536-3539` → `db.py:672-687` |
| 实时 | `EventSource /api/events`（`chat.message`） | — | 信封 `{kind,traceId,at,data:{threadId,message}}` | `server.py:1125-1127, 3791-3824`；`MessagesPage.tsx:195-232` 去抖 600ms 后增量合并 |
| 写库（间接） | `POST /api/commands`、`Kernel.send()` | `text` | Web 指令同进程写 `chat_messages`（`thread_id='web:workbench'`） | `server.py:1129-1143, 1171-1190, 3894-3932` |
| trace 回放补齐 | `GET /api/trace/<id>` | — | 内存缺 `meta` 或事件时由 `traces` / `trace_events` 补齐，附 `source:"db"\|"db+mem"` | `server.py:3756-3789` → `db.py:689-707`；**前端目前无调用方** |

**前端适配要点**（`MessagesPage.tsx`）：

- 类型 `ChatThread` / `ChatMessage` 故意使用 DB 原味 snake_case（`thread_id,created_at,last_at,msg_count,sender_name,content_kind,ingested_at`），`ChatStats` 用 camelCase（`:3-34`）；与其余页面的 camelCase 领域类型并存，不能互换。
- 筛选：`source ∈ {feishu,app,web}`、`direction ∈ {inbound,outbound}`、`modality ∈ {text,voice,video}`、`q` ≥3 字走 FTS，`<3` 退 LIKE（`db.py:613-621`）；分页用 `before=<最后一条 id>`（`:234-250`）。
- 权限与空态：非老板 403、`OPC_DB` 关闭 503 时整页空态（`:116-125, 289-306`）；入口同步隐藏（`Layout.tsx:27,51`）。
- 首页/消息中心共用 `/api/events`；`chat.message` 只在消息中心被消费，首页归入通用 feed。

**2026-11-12 线上实测（登录后 GET）**：`/api/chat/threads|messages|stats` 均 `{ok:true, enabled:true}`；`/api/state` 18 个键；`/api/hotspots` total 20、`/api/knowledge` 178、`/api/works` 171。`prototype/opc.db` 只读统计：`chat_messages` 640、`chat_threads` 20、`hotspots` 21、`knowledge` 82、`contents` 172、`traces` 25、`trace_events` 596。列表总数与库内行数不相等，正是 §4.5 所述「读路径未切换」的旁证。

### 4.5 数据源分工（校对「已改成 db」的实际边界）

| 层 | 真源 | 覆盖接口 | 行为 |
| --- | --- | --- | --- |
| 业务配置 / 人读产物 | Obsidian Vault（md / json / 交付物目录） | `/api/org*`、`/api/works*`、`/api/hotspots*`（部分）、`/api/knowledge*`、`/api/meeting*` | 文件扫描 + 内存列表 |
| 热态 | `KERNEL` 内存字典 | `/api/state`、`/api/reports`、`/api/events` 首帧 | 进程重启归零 |
| 过程数据 | `prototype/opc.db`（SQLite/WAL） | `/api/chat/*`、`/api/trace/<id>` 补齐 | **新版 web 真正读库的部分** |
| 影子双写 | `emit()` / `send()` | 无读接口 | `server.py:1155-1168, 1180-1190` 写 traces / trace_events / messages / reports / approvals / hotspots / knowledge / contents；`OPC_DB=1` 时普通事件不再重写 `runtime.json` |

风险（已核实）：`_restore_history()` 仍只从 `runtime.json` + Obsidian 恢复，不查 `opc.db`；因此重启后 `/api/state`、`/api/hotspots`、`/api/knowledge`、`/api/reports` 可能少于库内行数，DB 数据只能经 `/api/chat/*` 与 `/api/trace/<id>` 看到。这是 P0 影子期设计，读切换属 `PROCESS-DATA-DB-PLAN.md` 的 P1，登记在 §8，不在本模块内修复。

### 4.6 创意灵感 / 运维监控数据契约（2026-11-12 新增）

两页都不造数据，后端新增两个只读聚合模块与一个处置接口（均需登录）。

| 接口 | 模块 | 响应 | 数据源 |
| --- | --- | --- | --- |
| `GET /api/inspiration[?refresh=1]` | `prototype/insights.py` | `{ok,generatedAt,counts{total,热点分析,视频分析,蒸馏启发},sources,items[],notes}`；`items[]` 含 `id/category/subKind/title/summary/detail/tags/source/link/at/score/origin`，60s 缓存 | `KERNEL.hotspot_page()`；Obsidian `OPC/热点采集/*热点简报*.md`「分项目可用角度」表；`素材/**` 视频拆解 + 仓库根目录「同心圆-*」创意说明（按章节切分、过滤可复用结论）；知识库归档「一句话结论 / 可复用点」；选题评审会定稿清单 |
| `GET /api/ops` | `prototype/ops_monitor.py` | `{ok,generatedAt,service,stats,pipeline,video{services,localJobs,pipelineRender,pendingRender},alerts[],actions,notes}` | `KERNEL.snapshot()` / `/api/health`；`media-gen/output/jobs.json`；短超时探测 `127.0.0.1:9200` 与 `:9103` 的 `/health` `/v1/jobs`（15s 缓存，离线仍返回结构） |
| `POST /api/ops/recover` | `server.py` do_POST | `{ok,action,result,by,at}`；仅老板；`action ∈ patrol|reload|approve|reject`，审批类需 `id` | `KERNEL.patrol()`（清陈旧 trace → 待审批停派单 → 有任务在跑等待 → 否则派单全员复工）、`reload_config()`、`decide_approval()` |

**视频生成环节口径**：主流水线 `om_render` 的 `render/renderCheck/renderError` 结果 + 卡在门禁的待出片内容 + `media-gen/output/jobs.json` 落盘任务（状态/进度/耗时/错误）。`media-engine:9200`、`media-gen:9103` 为独立服务；离线时页面仍展示历史任务并给出 `critical` 告警。**前端不直接重启进程**：重启主服务/引擎仍走 `opc.sh restart` / `systemctl restart`。

**2026-11-12 线上实测**：`/api/inspiration` 返回 106 条（热点分析 23 / 视频分析 34 / 蒸馏启发 49，来源：1 份简报、7 份视频文档、37 条知识归档、3 场评审会、23 条热点线索）；`/api/ops` 返回 12 条告警（媒体引擎与生成服务离线、4 条失败/中断任务、2 条滞留任务、60 条 `production-gate` 阻塞、5 条超时审批、3 份事故报告）、16 条落盘视频任务、12 条待出片内容。两页 playwright-cli 实测 0 控制台报错。

## 5. 鉴权契约

| 能力 | 实现 | 出处 |
| --- | --- | --- |
| 会话检查 | `fetch("/api/state")`：200=true，401=false 并 `redirectTo:"/login"`，其他错误带 `AuthError` | `authProvider.ts:7-15` |
| 登录 | `POST /api/login` JSON `{account,password}`，成功后写 `localStorage["opc.user"]` 并跳 `/` | `authProvider.ts:18-39` |
| 身份 | 优先 `GET /api/session`，网络异常回退 localStorage | `authProvider.ts:41-60` |
| 登出 | `POST /api/logout`，再清本地状态并跳 `/login` | `authProvider.ts:61-69` |
| 全局 401 | `onError` 跳 `/app/login` 并 `{logout:true}` | `authProvider.ts:70-76` |
| 统一消息 | 后端 `_owner_only()` 仅老板放行，否则 403；前端按 `identity.role` 隐藏入口 | `server.py:3257-3263, 3514-3516`、`Layout.tsx:27,51` |
| 运维恢复动作 | `POST /api/ops/recover` 校验 `role == "老板"`，否则 403；前端对巡检派单与审批操作加二次确认 | `server.py` do_POST、`OpsPage.tsx` |

`/app` 页面本身公开，数据接口全部经过 `opc_sess` 会话拦截（`server.py:3442-3461`）。

## 6. 部署与缓存行为

- 构建产物由后端 `_web_dist` 提供：`/app/assets/*` 走 `public, max-age=31536000, immutable`；`index.html` 与其他路径 `no-cache`（`server.py:3444-3445, 3605-3624`）。
- 未命中文件回退 `web/dist/index.html`，支持 SPA 深链（`server.py:3606-3614`）。
- `web/dist` 未构建时返回 404「web/dist 未构建，先在 web/ 执行 npm run build」（`server.py:3613-3614`）。
- 当前线上 `index.html` 引用 `/app/assets/index-C21ZRNYx.js` 与 `index-q43a5DfF.css`；`MarkdownBody-C9D22Bk_.js` 为知识详情懒加载 chunk（`web/dist/index.html`、`KnowledgePage.tsx:5-6`）。`web/dist` 不入 git（`.gitignore`），由服务端目录直接托管，每次 `npm run build` 后线上立即换新哈希。

## 7. 失败与降级

| 场景 | 行为 | 出处 |
| --- | --- | --- |
| 会话过期（任意 API 401） | 强制跳 `/app/login` | `dataProvider.ts:20-27`、`authProvider.ts:70-76`、`MessagesPage.tsx:76-87` |
| 非老板访问 `/api/chat/*` | 403 → 消息中心整页「仅「老板」角色可查看统一消息」；导航入口也不显示 | `server.py:3257-3263`、`MessagesPage.tsx:289-306`、`Layout.tsx:27,51` |
| `OPC_DB=0` / 库不可用 | `/api/chat/*` 统一 503 `{ok:false,enabled:false}` → 整页「过程数据库未启用」；其余页面照常 | `server.py:3519-3520`、`db.py:737-750` |
| DB 写失败 | 记日志并降级，不中断主流程 | `server.py:1142-1143, 1155-1168` |
| SSE 断开 | 仅关闭连接，无自定义 `onerror` 重连（浏览器 `EventSource` 默认行为除外）；3D 侧栏另有 10–15s 轮询兜底 | `HomePage.tsx:176-202`、`MessagesPage.tsx:195-232`、`ScenePage.tsx` |
| 3D iframe 会话失效 | 顶层 `/api/session` 401 或 iframe `opc:unauthorized` → 跳 `/app/login`；`onLoad` 检测到旧登录页同样跳登录 | `ScenePage.tsx`、`office3d.html` / `meeting3d.html` |
| 3D iframe 20s 未就绪 | 显示超时提示 + 重试 / 独立窗口（以 `opc:scene-ready` 握手为准，不再把 302 登录页当成加载成功） | `ScenePage.tsx` |
| 后端不可达 | 登录 `catch` 提示失败；登出仍清本地 | `authProvider.ts:27-36, 61-67` |
| 作品档案模块未加载 | `/api/works` 500，页面展示空列表 | `server.py:3483-3485` |
| 无 `dist` | `/app` 404 提示构建 | `server.py:3613-3614` |
| 聚合模块加载失败 | `/api/inspiration` / `/api/ops` 返回 500 `{ok:false,error:"…模块未加载"}`，两页显示错误横幅，不影响其它页面 | `server.py` do_GET |
| 媒体引擎 / 生成服务离线 | `/api/ops` 仍返回历史 `localJobs` 并给出 `critical` 告警；探测结果 15s 缓存，服务恢复后下次刷新才变在线 | `ops_monitor.py` |
| 恢复动作非老板 / 参数错误 | 403 `{ok:false,error:"forbidden"}`；非法 action 400；缺审批 id 400 | `server.py` do_POST |

## 8. 已知限制

1. **DB 读切换未完成**：只有消息中心与 trace 回放补齐读库；`/api/state`、`/api/hotspots`、`/api/knowledge`、`/api/works`、`/api/org`、`/api/meeting`、`/api/reports` 仍是内存/文件读路径（`server.py:3472-3512`），重启后可能与库内行数不一致（§4.5）。
2. **`/api/trace/<id>` 的 DB 补齐无前端入口**：新版 web 未使用该接口；若将来接入，需处理 `source:"db"` 时 `contents/approvals/reports/knowledge` 恒为空数组、`events[*].data` 为原始 camelCase 对象（`server.py:3779-3789`）。
3. **命名双轨**：消息中心用 snake_case DB 行，其余页面用 camelCase 业务对象；`types.ts` 未收录 `ChatThread/ChatMessage/ChatStats`（定义在 `MessagesPage.tsx:3-34`），跨页复用需先统一。
4. `org` 已注册为 Refine resource，但 `/api/org` 不返回 `{items,total}`；若未来换成通用列表组件会静默得到空列表（`App.tsx:27`、`dataProvider.ts:63-69`）。
5. `dataProvider` 只实现读，热点/知识无写接口；页面按钮若依赖写操作会直接 reject（`dataProvider.ts:86-90`）。
6. 首页、消息中心与 3D 侧栏的 SSE 均未处理 `onerror`，网络抖动后不主动重建连接（浏览器 `EventSource` 默认重连与 3D 侧栏轮询兜底除外）。
7. 组织页为直连 `fetch`，错误处理各自实现，与 Refine 通知体系不统一；写操作由后端 403 兜底。
8. `types.ts` 的 151 行领域类型未做运行时校验，`OpcState` 只声明 7 个键而 `/api/state` 实测返回 18 个键，字段漂移只能靠 TS 编译期发现。
9. 消息中心导出为一次性 JSON 下载（`limit=20000` 上限），无服务端流式导出与导出审计记录（`MessagesPage.tsx:257-280`）。
10. **创意灵感是只读聚合，不是新的数据模型**：没有灵感写接口 / 收藏 / 标注，视频拆解结论仍散落在 Obsidian 与仓库 md 中；`/vault` 无目录列表接口，仓库文档只能内联要点（`insights.py`）。
11. **运维恢复不含进程重启**：`patrol` 可能启动新一轮编排（非幂等），`reload_config` 校验失败保留旧值；媒体引擎 / 主服务重启、失败任务重提仍需运维脚本，前端只展示告警与建议（`ops_monitor.py`）。

## 9. 验收手段

```bash
# 构建与本地托管
cd web && npm ci && npm run build       # 生成 web/dist/index.html + assets/*
curl -s -D - -o /dev/null http://127.0.0.1:8848/ | head   # 302 Location: /app，且 Content-Length: 0（不挂起）
curl -s http://127.0.0.1:8848/app | head                  # 应返回 index.html（带哈希资源）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8848/app/messages   # 深链 200（SPA 回退）
curl -s -b /tmp/opc.cookies -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8848/legacy  # 200 旧控制台

# 登录态与数据接口（线上新版本）
curl -s -c /tmp/opc.cookies -H 'Content-Type: application/json' \
  -d '{"account":"<账号>","password":"<密码>"}' http://192.168.31.206:8848/api/login
curl -s -b /tmp/opc.cookies 'http://192.168.31.206:8848/api/chat/messages?limit=2'   # {ok:true,enabled:true,messages:[...]}
curl -s -b /tmp/opc.cookies 'http://192.168.31.206:8848/api/chat/stats?days=7'       # stats.bySource/byModality/byDay
curl -s -b /tmp/opc.cookies 'http://192.168.31.206:8848/api/state'                   # 18 个键
curl -s -b /tmp/opc.cookies 'http://192.168.31.206:8848/api/inspiration'             # counts/sources/items
curl -s -b /tmp/opc.cookies 'http://192.168.31.206:8848/api/ops'                     # service/stats/video/alerts/actions
curl -s -b /tmp/opc.cookies -H 'Content-Type: application/json' \
  -d '{"action":"reload"}' http://192.168.31.206:8848/api/ops/recover                 # 仅老板；热加载配置

# 过程库行数与开关
python3 -c 'import sqlite3; c=sqlite3.connect("file:prototype/opc.db?mode=ro",uri=True); \
print(c.execute("select count(*) from chat_messages").fetchone())'
tr '\0' '\n' < /proc/$(pgrep -f 'prototype/server.py' | head -1)/environ | grep '^OPC_'
```

人工回归：清 Cookie 访问 `/app/works` 应跳 `/app/login`；非老板登录后不应出现「消息中心」入口；`OPC_DB=0` 重启后消息中心应显示「过程数据库未启用」；根路径应跳 `/app`，顶栏「旧版控制台」应进入 `/legacy` 且旧版「新版界面 →」可切回；`/app/office`、`/app/meeting` 应显示 3D 画面 + 真实数据侧栏，控制台 0 报错，点击 3D 会议室指示牌应在 SPA 内切换而非整页刷新；`OPC_HOME_LEGACY=1` 重启后根路径应直接返回旧版控制台；顶栏「创意灵感」应显示三类灵感与来源统计、筛选/展开/原文链接可用，「运维监控」应显示系统状态、告警、视频任务与恢复按钮（热加载可实际执行）；两页控制台 0 报错。
