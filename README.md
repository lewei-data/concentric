# 同心圆（Concentric）· OPC 一人公司 AI 员工工作台

17 名 AI 员工组成的「一人公司」，用真实大模型跑内容生产流水线：选题 → 文案 → 分镜/样片 → 审核 → 审批 → 发布。
业务配置（项目、员工、阶段、高危工具）与人读产物仍来自 Obsidian Markdown；过程数据（事件、trace、统一消息、token 用量）改落 SQLite `prototype/opc.db`（`OPC_DB=1`，`PROCESS-DATA-DB-PLAN.md`）。

## 功能概览

- **员工编制**：老板、首席创意官 + 15 名职能员工（CEO / CTO / 3×产品经理 / 3×文案 / 3×视频 / 审核 / 发布运维 / 热点采集 / 知识库管理），每人独立人设与工具白名单。
- **三条业务线**：文人莫克、狗大人（自媒体账号）与同心圆（数字分身服务平台）。
- **工作台前端**：会议纪要、CEO 日报、项目周报、风险单、热点与知识库五个页签，SSE 实时刷新。
- **3D 场景**：Three.js 办公区与会议室，员工工位与实时状态联动。
- **事件总线**：类型化消息（delegation / result / conflict / vote / decision / biz.event / escalation / approval / report.ready / scene.change / heartbeat），支持 trace 回放。
- **审批门**：高危工具（`publish.execute` / `deploy.prod` / `customer.reply` / `ad.spend`）必须经审批，未审批不得发布；审批超时自动升级。
- **协作语义**：选题会、冲突投票、人设串味驳回、CEO 巡检（`/api/patrol`）、知识归档。
- **交付物落盘**：每条内容的选题 / 文案 / 分镜 / 封面 / 样片写成实体文件，不落临时目录。
- **样片渲染**：MCP-TTS 合成旁白 → OpenMontage(Remotion) 出片 → 画面验收门，竖屏 1080×1920 @30fps。
- **内部邮件**：SMTP 发信 + 邮件审计（仅在配置了邮箱凭据文件时启用）。
- **知识库导出**：`vault_export.py` 把公司配置与运行时动态导出为 Markdown 页面。

## 目录结构

```
opc-workbench/
├── opc.sh                  # 后台管理脚本：start/stop/restart/status/logs/verify/reload/patrol/vault
├── vault_export.py         # 公司配置 + 运行时动态 → Obsidian Markdown
├── web/                    # 新版前端（React 18 + Refine v4 + Vite，构建产物 dist 托管在 /app）
└── prototype/                # 旧版前端与服务端（旧界面仍保留，回滚用）
    ├── server.py           # 服务端主程序（标准库 HTTP + SSE + Hermes 适配层）
    ├── verify.py           # 端到端验收（零依赖）
    ├── deliverables.py     # 交付物落盘（Markdown / 封面 / 分镜帧）
    ├── om_render.py        # 真实样片渲染（MCP-TTS + Remotion + 验收门）
    ├── illustration.py     # 分镜 → 透明底 SVG 插图资产
    ├── index.html          # 工作台控制台
    ├── login.html          # 登录页
    ├── office3d.html       # 3D 办公区
    ├── meeting3d.html      # 3D 会议室
    ├── assets/             # 员工头像、脸部贴图、字体、SVG 插图
    └── vendor/             # three.module.js（本地化，避免外网 CDN）
```

## 快速开始

依赖：Python 3.10+（服务端只用标准库）。以下为可选能力，缺失时对应功能降级或跳过：

| 能力 | 依赖 |
| --- | --- |
| 封面 / 分镜帧 | Pillow |
| 样片合成 | ffmpeg |
| 样片渲染 | Node 22 + OpenMontage(Remotion) |
| 旁白 | MCP-TTS（`run.sh`） |
| Agent 推理 | Hermes 内核（`run-hermes.sh`） |

```bash
chmod +x opc.sh
./opc.sh start        # 默认 0.0.0.0:8848
./opc.sh status       # 状态 + /api/health
./opc.sh logs         # 跟踪日志
./opc.sh verify       # 端到端验收，预期 RESULT: ALL PASS
./opc.sh vault        # 导出 Markdown 到 /opt/sefs/obsidian
```

无 Hermes 内核时可走离线规则模式演示：`OPC_HERMES_MODE=rule ./opc.sh start`。

## 配置来源（Obsidian 配置真源 + SQLite 过程库）

| 文件（相对 `OPC_VAULT`，或仓库内路径） | 作用 |
| --- | --- |
| `公司/项目配置.md` | 项目清单、调性、流水线阶段、高危工具 |
| `公司/员工名册.md` | 员工编制、负责范围、工具白名单（当前 17 人） |
| `公司/登录凭据.md` | 登录账号与密码，每次登录实时读取 |
| `公司/.邮箱凭据.md` | 内部邮件账号（缺失则不发信） |
| `动态/runtime.json` | 运行时快照；`OPC_DB=1` 时普通事件不再重写，仍是启动恢复来源之一 |
| `prototype/opc.db` | SQLite/WAL 过程库：traces / 事件 / 审批 / 报告 / 会议 / `chat_threads` / `chat_messages` 等 |
| `OPC/交付物/<traceId>/` | 交付物落盘目录 |
| `OPC/知识库/` | 归档知识页 |

开关：`OPC_DB=1`、`OPC_INGEST=1`（生产由 systemd drop-in 设置）；`OPC_DB_PATH` 可改库路径。统一消息由 `prototype/ingest.py` 每 30s 只读摄取飞书 / APP 源库，`/api/chat/*` 提供只读视图（仅老板）。业务列表与 `/api/state` 目前仍读内存 / 文件，DB 为影子双写，读切换属 P1。

文件缺失或解析异常时回退代码内置默认（`DEFAULT_PROJECTS` / `DEFAULT_AGENT_SPEC` 等）。
配置改完可热加载：`./opc.sh reload`（等价 `POST /api/config/reload`），无需重启。

## 页面与接口

| 路径 | 说明 |
| --- | --- |
| `/`、`/index.html` | 旧版工作台控制台（需登录，首页顶部有「新版界面」入口） |
| `/app` | 新版界面（React + Refine，页面公开、数据接口需登录）：首页 / 作品档案 / 热点 / 知识库 / 消息中心 / 组织配置 / Office 3D / Meeting 3D；SPA 深链回退 `web/dist/index.html` |
| `/app/assets/...` | 新版前端静态资源（带内容哈希，长缓存）；`web/` 下 `npm run build` 产物由服务端托管 |
| `/login` | 登录页 |
| `/office3d` | 3D 办公区（需登录） |
| `/meeting3d` | 3D 会议室（需登录） |
| `/vault/...` | Obsidian 交付物文件（公开访问） |
| `/api/health` | 健康检查 |
| `/api/state` | 全量状态快照 |
| `/api/events` | SSE 实时事件流（含 `chat.message`，消息中心增量刷新） |
| `/api/trace/<id>` | 单条 trace 回放；内存缺失时由过程库补齐 `meta` / `events` |
| `/api/reports` | 日报 / 周报 / 风险单 |
| `/api/meeting`、`/api/meetings` | 会议纪要查询 / 发起选题会（POST） |
| `/api/approvals/<id>` | 审批动作 |
| `/api/patrol` | 触发 CEO 巡检 |
| `/api/hotspots`、`/api/hotspots/<id>` | 热点分页列表（`page`/`size`/`q`）与单条详情 |
| `/api/knowledge`、`/api/knowledge/<id>` | 知识库分页列表（`page`/`size`/`q`）与单条详情（含完整正文） |
| `/api/works`、`/api/works/<id>` | 作品档案分页列表与详情（交付物目录） |
| `/api/org*`、`/api/org/audit` | 组织配置读写（写操作仅老板）与变更审计 |
| `/api/chat/threads`、`/api/chat/messages` | 统一会话 / 消息流（读 `opc.db`，`source`/`modality`/`direction`/`q`/`before`/`limit`；仅老板） |
| `/api/chat/stats`、`/api/chat/export` | 按来源/模态/天聚合统计；消息导出（仅老板） |
| `/api/hotspots/inject` | 注入热点 |
| `/api/delegate`、`/api/commands` | 人工派单与指令 |
| `/api/login`、`/api/logout`、`/api/session` | 会话管理 |
| `/api/config/reload` | 热加载 Obsidian 配置 |

## 样片渲染

`deliverables.py` 按 trace 落盘：`00-index.md`、`01-topic.md`、`02-copy.md`、`03-storyboard.md`、
`04-cover.png`、`05-preview.mp4`、`06-log.md`、`07-data.json`。
`om_render.py` 负责真实出片：逐镜生成 props → MCP-TTS 分段旁白并按实测时长对齐 → Remotion 渲染 →
验收门校验分辨率（1080×1920）、时长与音轨，通过后才登记为正式样片。

## 安全说明

- 未登录访问首页或 3D 区会跳转登录页；`/vault/` 交付物链接保持公开，便于外部浏览。
- 账号密码只存放在 Obsidian 凭据文件，仓库内不含任何明文凭据。
- 非回环地址对外提供服务时，建议启用 Basic 鉴权：`OPC_BASIC_USER=... OPC_BASIC_PASS=... ./opc.sh start`。
- `logs/`、`__pycache__/`、`*.log`、`*.pid` 已列入 `.gitignore`，运行日志不入库。

## 验收基线

`prototype/verify.py` 覆盖：17 人编制与三项目分布、SSE 实时事件、选题会与冲突投票、审核、
审批门闭环、未审批不得发布、人设串味驳回、trace 可回放、知识归档、日报生成、审批耗时统计。
