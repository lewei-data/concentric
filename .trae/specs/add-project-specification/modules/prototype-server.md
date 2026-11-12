# 模块规格 · prototype（工作台后端 / 旧版控制台 / 交付物）

> 上级文档：[spec.md](../spec.md)。本文件只描述 `prototype/` 一个模块，接口与行为均以当前工作区代码为准。
> 取证方式：逐文件阅读 + `wc -l` / `grep` 统计；标注约定同 spec.md 第 0 节（已核实 / 推断 / 未验证）。

## 1. 模块定位

`prototype/` 是 OPC 工作台的**唯一常驻 HTTP 服务**：既是 REST + SSE 网关，也是 Refine 前端的静态宿主，同时内嵌「AI 员工编排内核」与「交付物落盘/渲染」能力。

- 进程模型：单进程多线程，`ThreadingHTTPServer`（`prototype/server.py:3798-3819` 的 `main()`）。
- 默认监听 `127.0.0.1:8848`（`server.py:3801-3802`）；绑定非回环地址且未配置 `OPC_BASIC_USER` 时**仅打印警告**，仍会启动（`server.py:3804-3805`）。
- 旁路依赖：Hermes 内核（子进程/gateway）、media-engine、ffmpeg、Pillow、Remotion；任一缺失只降级，不阻断启动（`server.py:45-55`、`243-266`）。

## 2. 目录与文件清单（已核实，`wc -l`）

| 文件 | 行数 | 职责 |
| --- | ---: | --- |
| `server.py` | 3,819 | HTTP 服务 + 编排内核 + 鉴权 + SSE + 组织配置 |
| `index.html` | 1,146 | 旧版控制台（首页 / 素材库 / 审批入口） |
| `login.html` | 167 | 登录页 |
| `office3d.html` | 2,139 | 3D 办公室视图 |
| `meeting3d.html` | 1,412 | 3D 会议室视图 |
| `deliverables.py` | 921 | 交付物文件树生成（选题/文案/分镜/封面/评估） |
| `om_render.py` | 738 | 真实样片渲染（TTS 旁白 + Remotion 出片 + 验收） |
| `works.py` | 352 | 作品档案：把交付物目录归一为「名称 + 时间」资产 |
| `illustration.py` | 317 | 配图生成与插图资源保障 |
| `verify.py` | 356 | 离线验收脚本（不依赖服务的断言集合） |
| `materials.json` | — | 首页素材库清单（`server.py:3542-3566`） |

## 3. 内部结构分层

| 层 | 关键符号 | 出处 | 说明 |
| --- | --- | --- | --- |
| HTTP 表现层 | `Handler` | `server.py:3129-3795` | 路由分发、Cookie 会话、SSE、静态文件 |
| 编排内核 | `Kernel` | `server.py:965-3126` | 员工状态、消息总线、流水线、审批、成本护栏、巡检 |
| 模型适配 | `HermesAdapter` | `server.py:759-953` | 统一 `prompt/ask`；gateway 优先，失败回落 CLI |
| 交付物 | `DL`（`deliverables`） | `server.py:243-250` | 导入失败则 `DL=None`，流水线仅内存运行 |
| 作品档案 | `WK`（`works`） | `server.py:251-257` | 导入失败则 `/api/works` 返回 500 |
| 渲染 | `om_render` / `illustration` | `server.py:258-266` | 封面与样片按需调用 |
| 数据源 | Vault 文件 | `server.py:95-103` | Obsidian 为唯一配置/状态真源 |

内核关键方法（行号即入口）：

| 能力 | 方法 | 出处 |
| --- | --- | --- |
| 事件广播 | `Kernel.emit` | `server.py:1132-1144` |
| 消息投递 | `Kernel.send` | `server.py:1145-1155` |
| 运行时落盘 | `Kernel.save_runtime` | `server.py:1073-1096` |
| 配置热加载 | `Kernel.reload_config` | `server.py:1097-1131` |
| 员工调用 | `Kernel.call_agent` | `server.py:1258-1266` |
| 用量记账 | `Kernel.record_usage` | `server.py:1267-1322` |
| 成本护栏 | `Kernel.guard_budget` | `server.py:1337-1343` |
| 热点注入 | `Kernel.inject_hotspot` | `server.py:1515-1578` |
| 主流水线 | `Kernel.run_pipeline` / `_pipeline` | `server.py:1579-1622` / `1838-2575` |
| 管理派单 | `Kernel.mgmt_delegate` | `server.py:2576-2598` |
| 技术门 | `Kernel._tech_gate` | `server.py:2609-2713` |
| 模型复核 | `Kernel.review_with_model` | `server.py:2714-2767` |
| 审批决策 | `Kernel.decide_approval` | `server.py:2814-2838` |
| 快照 | `Kernel.snapshot` | `server.py:3099-3125` |

## 4. 对外接口契约

### 4.1 无需登录（公开）

| 方法 | 路径 | 行为 | 出处 |
| --- | --- | --- | --- |
| GET | `/login`、`/login.html` | 登录页 | `server.py:3356-3357` |
| GET | `/api/logininfo` | `{ok, ready, file}`，`ready` 取决于凭据文件是否可解析 | `server.py:3358-3360` |
| GET | `/api/session` | 有会话返回 `{ok,user}`，否则 401 `{login:"/login"}` | `server.py:3361-3365` |
| GET | `/vendor/three.module.js` | three.js 模块 | `server.py:3367-3368` |
| GET | `/avatars/<id>.png` | 头像，文件名正则 `[a-z_]+\.png` | `server.py:3369-3373` |
| GET | `/faces/<id>.png` | 人脸素材，同上正则 | `server.py:3374-3378` |
| GET | `/videos/<id>[.talk|.idle].mp4` | 数字人视频素材 | `server.py:3380-3384` |
| GET | `/vault/*` | Vault 只读文件（交付链接口径） | `server.py:3385-3386` |
| GET | `/app`、`/app/*` | Refine 构建产物，未命中回退 `index.html` | `server.py:3389-3399, 3520-3540` |
| GET | `/app/legacy` | 302 → `/` | `server.py:3391-3395` |
| GET | `/materials/*`、`/api/materials` | 素材库只读文件与清单 | `server.py:3401-3404` |
| POST | `/api/login` | 登录，成功写 `opc_sess` Cookie | `server.py:3694-3695, 3219-3261` |
| POST | `/api/logout` | 清除会话 Cookie | `server.py:3696-3701` |

### 4.2 需要登录（GET）

| 路径 | 返回 | 出处 |
| --- | --- | --- |
| `/api/health` | `{ok, version, engine, hermes, time}` | `server.py:3408-3410` |
| `/`、`/index.html` | 旧版控制台 | `server.py:3411-3412` |
| `/office3d`、`/meeting3d` | 3D 页面 | `server.py:3413-3416` |
| `/api/meeting`、`/api/meeting/<id>` | 会议列表 / 详情，未命中 404 | `server.py:3417-3421` |
| `/api/knowledge`、`/api/knowledge/<id>` | 知识库分页 / 详情 | `server.py:3422-3426` |
| `/api/works`、`/api/works/<id>` | 作品档案分页 / 详情；`WK` 未加载返回 500 | `server.py:3428-3440` |
| `/api/hotspots`、`/api/hotspots/<id>` | 热点分页 / 详情 | `server.py:3441-3445` |
| `/api/state` | 全量快照（员工、消息、审批、内容） | `server.py:3446-3447` |
| `/api/events` | SSE 流 | `server.py:3448-3449, 3654-3688` |
| `/api/trace/<id>` | trace 明细，未命中 404 | `server.py:3450-3454` |
| `/api/reports?kind=` | 报告列表（按 `kind` 过滤） | `server.py:3455-3457` |
| `/api/org` | `agents/policies/profilesRoot/projects`（含 `all`） | `server.py:3459-3468` |
| `/api/org/policy?key=` | 制度文件；未知 key 404 | `server.py:3469-3479` |
| `/api/org/audit?limit=` | 组织变更审计，`limit` 截断到 1..500 | `server.py:3480-3488` |
| `/api/org/agent/<aid>` | 员工详情 | `server.py:3489-3494` |
| `/api/org/agent/<aid>/skill?name=` | 技能详情 | `server.py:3495-3497` |

### 4.3 需要登录（POST）

| 路径 | 准入 | 关键入参 | 失败语义 | 出处 |
| --- | --- | --- | --- | --- |
| `/api/org/*` | 仅 `role=老板` | 按子路径 | 非老板 403 | `server.py:3706-3710` |
| `/api/patrol` | 登录 | — | `ValueError` → 400 | `server.py:3711-3715` |
| `/api/hotspots/inject` | 登录 | `title/tags/score` | 已有运行中 trace → 409 `{busy,traceId}` | `server.py:3716-3724` |
| `/api/meetings` | 登录 | 同上 | 同上 | `server.py:3725-3733` |
| `/api/approvals/<id>` | 仅老板 | `decision=approve\|reject` | 非老板 403；非法值 400；不存在 404 | `server.py:3734-3745` |
| `/api/config/reload` | 登录 | — | 新配置缺项 → 400，旧配置保留 | `server.py:3746-3750` |
| `/api/delegate` | 登录 | `sender/recipient/text` | `ValueError` → 400 | `server.py:3751-3756` |
| `/api/commands` | 登录 | `text` | 空 400；`@员工` 未命中 404 | `server.py:3757-3794` |

`/api/commands` 的分支优先级（`server.py:3761-3794`）：`@员工 指令` → 人设串味测试 → 注入热点 → CEO 汇总回答。

### 4.4 SSE 契约

- 响应先推 `state` 快照帧，随后事件信封 `{"kind","traceId","at","data"}`，每 10s 一帧 `heartbeat`（`server.py:3654-3688`、`1132-1144`）。
- 每个订阅者独立队列，`maxsize=500`；满时 `put_nowait` 抛 `QueueFull` 并静默丢弃，不阻塞业务线程（`server.py:1091-1095, 3616`）。
- 客户端断开即移除订阅（`server.py:3684-3688`）。

## 5. 鉴权与路径安全

| 机制 | 规则 | 出处 |
| --- | --- | --- |
| 会话 | `opc_sess` Cookie，TTL 7 天，服务端 `SESSIONS` 内存表 | `server.py:104-107, 3158-3203` |
| 登录失败锁定 | 按 IP 计连续失败次数，锁定窗口存 `LOGIN_FAILS` | `server.py:108-109, 3219-3261` |
| 公开白名单 | `_need_login` 判定；静态与交付物不拦登录 | `server.py:3204-3218` |
| 角色门 | 组织写入、审批决策仅 `role="老板"` | `server.py:3707, 3735` |
| 文件名 | 头像/人脸/视频用正则；`/v1/assets` 类用 `basename` | `server.py:3371, 3376, 3382` |
| 静态函数 | `_static` 仅拼接 `BASE_DIR`，**自身无 realpath 越界校验**（安全依赖调用点正则） | `server.py:3506-3518`（已知限制） |
| SPA 托管 | `_web_dist` 基于 `_safe_path` 的 `realpath` 前缀校验，越界 404 | `server.py:520-521, 3520-3540` |
| Vault 读取 | `_vault` 独立实现路径校验 | `server.py:3601-3640` |

## 6. 配置项

启动参数与环境变量（已核实于文件头与 `main()`）：

| 变量 | 默认 | 作用 | 出处 |
| --- | --- | --- | --- |
| `--host` | `127.0.0.1` | 监听地址；非回环且未设 `OPC_BASIC_USER` 时打印警告（不阻断启动） | `server.py:3801-3805` |
| `--port` / `OPC_PORT` | `8848` | 监听端口 | `server.py:3801` |
| `OPC_HERMES_CLI` | `/opt/sefs/hermes/run-hermes.sh` | Hermes CLI 包装器 | `server.py:47` |
| `OPC_HERMES_BACKEND` | `gateway` | `gateway`/`cli`，决定 gateway 不可用时是否回落 | `server.py:50-51` |
| `OPC_HERMES_MODE` | `hermes` | `hermes`/`rule`（离线演示） | `server.py:57` |
| `OPC_HERMES_TIMEOUT` | `600` | 单次调用超时（秒） | `server.py:58` |
| `OPC_HERMES_WORKERS` | `6` | 并发模型调用数 | `server.py:59` |
| `OPC_APPROVAL_TIMEOUT` | `600` | 审批等待超时（秒） | `server.py:60` |
| `OPC_HERMES_MODEL` | `deepseek-v4.1-flash` | 默认模型 | `server.py:61` |
| `OPC_TOKEN_LIMIT` | `20000000` | 单轮 trace 累计 token 上限 | `server.py:66` |
| `OPC_CALL_TOKEN_LIMIT` | `2000000` | 单次调用 token 上限 | `server.py:67` |
| `OPC_COST_LIMIT_USD` | `0` | 金额阈值，0=关闭 | `server.py:68` |
| `OPC_BASIC_USER` / `OPC_BASIC_PASS` | 空 | 非回环监听的强制认证 | `server.py:69-70` |
| `OPC_VAULT` | `/opt/sefs/obsidian` | 配置与状态真源 | `server.py:95` |
| `OPC_MATERIALS_ROOT` | `/mnt/sefs/github-hotspots` | 素材库文件根 | `server.py:103` |
| `OPC_MAIL_DOMAIN/HOST/SMTP_PORT` | `mail.conring.cn` / `127.0.0.1` / `587` | 内部邮件 | `server.py:179-181` |

## 7. 失败与降级

| 场景 | 行为 | 出处 |
| --- | --- | --- |
| `gw_runs_client` 缺失 | `_GW=None`，退化为纯 CLI 调用，不阻止启动 | `server.py:45-55` |
| `deliverables` 导入失败 | `DL=None`，交付物不落盘，主流程继续 | `server.py:243-250` |
| `works` 导入失败 | `WK=None`，`/api/works*` 返回 500 | `server.py:251-257, 3429-3436` |
| 配置热加载缺项 | 返回 400 + 缺失清单，内存配置保持旧值 | `server.py:1097-1131, 3746-3750` |
| 审批超时 | 审批 `timeout`，trace 置 `timeout`，内容 `blocked` | `server.py:2036-2046, 2417-2431` |
| trace 僵尸 | 运行 >2700s 且无 pending 审批 → 置 `timeout` | `server.py:1414-1429` |
| SSE 队列满 | 丢帧，不断连、不阻塞 | `server.py:1091-1095` |
| 模型不可用 | `call_agent` 返回兜底文本，`fallback()` 提供结构化结果 | `server.py:1258-1266, 1380-1384` |

## 8. 已知限制

1. `_static` 未做 `realpath` 越界校验，安全完全依赖各调用点的文件名正则（`server.py:3506-3518`）。
2. `server.py` 单文件 3,819 行，HTTP 层与编排内核同文件，无法按模块独立测试。
3. 会话与登录失败计数均为进程内存，重启即丢失（`server.py:104-109`）。
4. `/api/events` 无鉴权之外的租户隔离，所有登录用户看到同一事件流。
5. 交付物文件数量与 README「8 类」不一致，见 spec.md 第 15 节 D9。

## 9. 验收手段

- 路由自检：`grep -n 'path == "/api/\|path.startswith("/api/' prototype/server.py`。
- 事件枚举：`grep -o 'emit("[a-z.-]*"' prototype/server.py | sort -u`，应为 20 种（见 spec.md C4）。
- 公开路由：未登录 `curl -i http://127.0.0.1:8848/api/state` 应 401，`/api/health` 应 200。
- 静态托管：`curl -I http://127.0.0.1:8848/app/assets/<hash>.js` 应含 `max-age=31536000, immutable`（`server.py:3535-3536`）。