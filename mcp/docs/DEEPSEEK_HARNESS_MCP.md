# DeepSeek Harness 接入 OPC MCP 说明

适用对象：使用 DeepSeek Harness（DSH）的远程 agent。本文只描述客户端需要知道的内容；连接只用公网入口，不涉及任何内网地址或服务端路径。

## 1. 是否需要单独的 skill 文档

需要一份**单独的接入说明**（即本文），但**不需要**把豆包 / WorkBuddy 的 skill 文档照搬一份。

原因：

- DSH 的 skill 与 MCP 是两层解耦的。`SKILL.md` 只是按需加载的指令文本；MCP 工具由官方 `@deepseek-ai/dsh-mcp-client` 自动发现，工具名带 `mcp__<serverName>__` 前缀。
- 豆包 / WorkBuddy 的 JSON `mcpServers` 配置与工具名写法不能直接用；DSH 使用可组合插件 + YAML 配置。
- 工具本身的描述已经足够让模型发现能力，因此 skill 不是必须项。建议只配一份极简 `SKILL.md` 稳定触发流程（见第 5 节）。

## 2. 连接配置

字段语法以实际安装的 DSH 版本及插件文档为准；本环境未安装 `dsh` 命令，以下为**示意**，未做实机验证。

```yaml
# ~/.dsh/config.yaml（示意）
plugins:
  - !!js 'import("@deepseek-ai/dsh-mcp-client")'
mcpServers:
  opc:
    url: https://ai.boraydata.com/mcp
    headers:
      X-Mail-Agent: !!js '`${process.env.OPC_MCP_KEY}`'
```

要点：

- `serverName` 用 `opc`（纯字母数字）。DSH 对非法工具名会做规范化/hash，改名会导致工具名不稳定。
- 个人 key 只从环境变量读取，不写进文档、URL 或 skill 正文。
- `X-Mail-Agent` 是身份头；网关注入后工具自动按当前手机号隔离数据，调用时不要再传 `phone`。
- 只写公网入口。不要把服务端路径、内网 IP 或内部端口写进配置或对话。

## 3. 工具名映射

连接成功后，DSH 侧工具名带统一前缀 `mcp__opc__`，例如：

| 能力 | DSH 工具名 |
|---|---|
| 身份自检 | `mcp__opc__opc_whoami` |
| 库内事实问答（图谱简报） | `mcp__opc__ask` |
| 跨源检索 | `mcp__opc__kb_search` |
| 自然语言路由 | `mcp__opc__opc_route` |
| 列本人素材 | `mcp__opc__material_list` |
| 删本人素材 | `mcp__opc__material_delete` |
| 读出处原文 | `mcp__opc__opc_resource_read` |

| 生成能力查询 | `mcp__opc__media_capabilities` |
| 语音合成 | `mcp__opc__tts` |
| 图片生成（支持参照图） | `mcp__opc__image` |
| 生成文件重取链接 | `mcp__opc__file_link` |
| 轻量健康检查（缓存式三盏灯） | `mcp__opc__kb_ping` |
| 概览一次返回（分层缓存 + rev 条件请求） | `mcp__opc__kb_overview` |
| @员工对话（仅老板 key） | `mcp__opc__staff_chat` |

其余工具同理，共 **25 个**（以 `tools/list` 为准；详见随附 `OPC_MCP_CLIENT_USAGE.md`）。

**白名单建议改按注解过滤（2026-11-19 起）**：每个工具的 `tools/list` 条目带
`annotations.readOnlyHint / destructiveHint / idempotentHint`。DSH 侧 Host 桥不要再硬编码
8 个只读工具名——按 `readOnlyHint === true` 过滤即可，工具增删改名都不会静默失效；
`material_delete` 带 `destructiveHint: true`，面板/桥一律不放开。
`ask` 带非幂等作业语义，重试请传 `idempotency_key`（窗口内重放返回同一 `job_id`）；
`job_get` 未完成时带 `retry_after_sec`，按它轮询。错误返回统一带稳定 `code`
（`invalid_argument`/`not_found`/`unauthorized`/`forbidden`/`rate_limited`/`upstream_unavailable`）。
`kb_status` 默认已脱敏（不再回 `dataDir`/`vault`/内网地址），面板健康灯改轮询 `kb_ping`（有缓存）。

**概览与对话（2026-11-19 新增）**：面板/概览页用 `kb_overview` 一次拿全（分组计数 + 三盏灯 +
近期作业），分层缓存 groups/rev 30s、recent 10s、健康灯 60s/30s，后台线程按各 TTL 自动预热；
回传上次的 `rev` 未变时只回 `changed:false`（不计限流），`refresh=true` 立即重算并强制重探健康灯。
`kb_search` 命中带 `reasoning`（1-8，八步执行法主步骤：立题/证据/结构/决策/执行/反馈/归档/复盘）与
`reasoning_name`，返回体带 `reasoning_steps` 分布 + `reasoning_legend` 图例，可用 `reasoning=N`
只取第 N 步的材料。老板 key 可用 `staff_chat(agent=<员工id 或姓名>, message=<指令>)` 直接 @员工
下发指令，员工走独立 hermes profile 真实回复（`list_agents=true` 先查 18 人名单；非老板 key 返回
forbidden），指令与回复落消息中心线程 `mcp:<手机号>`。

**实时刷新（替代轮询）**：服务端作业完成会推送 `notifications/opc/job`，**默认直接走标准
`GET https://ai.boraydata.com/mcp`（即 `initialize` 的同一个 endpoint）的 SSE 流**——DSH 自带
MCP 连接只要注册 `method == "notifications/opc/job"` 的通知处理器即可收到，无需另开连接、无需
设自定义头。`initialize` 结果的 `capabilities.experimental.events` 会声明这条约定
（`notification`/`path`/`replay_query` 等）。通知 `params` 字段仅
`job_id/kind/status/error/at/event_id`，**无手机号/指纹**；`job_id` 对应 `ask`/`material_upload`
返回的作业号。事件按账号指纹定向分发，跨账号不会串。

`GET /mcp/events`（Header `X-Mail-Agent: <个人 key>`）是给纯 SSE/`fetch` 客户端用的**额外通道**，
输出同样的字段，只是包成 `event: job` 帧；EventSource 不能带自定义头，须用 `fetch + ReadableStream`。
`/mcp/events` 每 20 秒发 `: ping` 注释帧保活（不是重复投递）；重连传 `?since=<上次最大 at>` 补发，
补发帧带 `replay: true`，客户端按 `event_id` 去重。标准 GET /mcp 通道无此心跳需求（SDK 自己维护）。
断线补发上限最近 1000 条，超出用 `job_get` 兜底。
**概览数据（DSH 同心圆面板）**：概览页用 `kb_overview` 一次调用替代 `kb_status + kb_ping`
（后者每次都真实探测上游模型）。首屏 `kb_overview {}` 拿 `groups/health/owner/recent + rev`；
之后把 `rev` 原样传回（`kb_overview {"rev": <上次 rev>}`）做条件请求——数据没变只回约 91 字节的
`changed:false` 且不计入限流，可放心轮询。写操作完成会推 `event: overview`（GET /mcp 流上是
`notifications/opc/overview` 通知，`params` 只有 `kind/rev/event_id`），收到后条件请求一次即可，
不必整页刷新。健康灯冷缓存回 `status:"probing"`（后台探测，下一轮见真值），探测失败只影响该灯、
`groups` 照常返回。详见 `OPC_MCP_CLIENT_USAGE.md` §6.10。POST /mcp 只收 JSON-RPC，需
`Accept: application/json, text/event-stream`（纯 `text/event-stream` 返回 406）；长任务走
`ask(wait_sec=0)` + 事件通知的异步模式。服务端请求层是线程池并发
（`OPC_MCP_REQUEST_WORKERS` 默认 8），长等待调用不会堵住其他请求。

## 4. 能力边界与限制

- DSH 的 MCP 集成只暴露 **tools**；resources / prompts 没有消费者。
- 二进制内容（图片、音频）在 DSH 侧会变成占位丢失：**TTS / 生图类工具不返回服务端 `path`，只给可跨网下载的 `download_url` + `file_key`**；把 `download_url` 交给用户在本地 curl 下载（同一把个人 key 作 `X-Mail-Agent`），链接丢了用 `file_link(file_key=...)` 重取。`media_capabilities` 同样已净化：音色只有 `id`+说明，无参考音频/模型权重的落盘路径。
- 员工照片/半身照用 `staff_photos(kind=all\|halfbody\|portrait\|digital_human)` 列出（全局共享，所有 key 可见），返回 `download_url` 走同一条 `X-Mail-Agent` 下载通道；`file_key` 为确定性 `sp` 前缀，不含服务端路径。
- 不要尝试在 DSH 里"读取 MCP resource"，统一用 `opc_resource_read` 工具。
- 读出处时，完整路径取 `knowledge_brief.items[].source` 或 `citations[].path`；简报正文里的 `→ 路径` 是截断显示，直接读会报文件不存在。
- `citations` 命中 `opc.message` 时 `path` 为空，这类消息引用不可追溯原文，只作线索。
- `ask` 带 `with_brief=true` 时会自动按对外口径处理：正文、元数据、引用与回答里的内网地址 / 服务端路径会被替换为占位；素材引用不再给服务端绝对路径，而给 `ref=material:<id>`。
- 零命中的简报为空串并回退原链路，不会增加开销。

## 5. 极简 skill 模板

模板文件：`skill-templates/opc-knowledge/SKILL.md`，可整目录复制到客户端技能根目录。

DSH 的 skill 发现**只深一层**：`<root>/<name>/SKILL.md` 或 `<root>/<name>.md`。不要使用 `skills/<分类>/<name>/SKILL.md` 这样的两层结构，否则扫描不到。

frontmatter 规则：

- `name` 必填，kebab-case；`description` 必填。
- `whenToUse`、`metadata` 选填。
- `disable-model-invocation: true` 会让模型完全看不到该 skill，只能通过 `/opc-knowledge` 由用户显式触发，不要误设。
- 字段拼写错误或布尔值非法会导致**整条技能被丢弃**。

## 6. 对外红线（必须遵守）

- 只通过公网入口连接，回复里只描述能力、结果和工具用法。
- 不转述 `dataDir` / `workspace` 等服务端字段、内网 IP、内部端口或服务端绝对路径。
- 检索不到要明确说明，不要用通用知识冒充公司内部资料。
- 删除类操作执行前先跟用户确认。

## 7. 与现有文档的分工

| 文档 | 面向 | 复用关系 |
|---|---|---|
| `DOUBAO_MCP_CLIENT_SKILL.md` | 豆包 JSON 配置 + 工具清单 | 不直接复用 |
| `WORKBUDDY_OPC_KNOWLEDGE_SKILL.md` | WorkBuddy 触发词 | 不直接复用 |
| `WORKBUDDY_EXTERNAL_USER_GUIDE.md` | WorkBuddy 用户指南 | 不直接复用 |
| 服务端工具描述 | 所有客户端共用 | 无需为 DSH 重写 |
| 对外红线 | 所有客户端共用 | 必须保留 |

## 8. 验证状态

- 公网入口、工具清单、工具名前缀规则来自 DSH 公开文档。
- 本说明未在本地安装 DSH 实机验证；首次接入时请用 `opc_whoami` 确认连通，再逐步试 `kb_search` / `ask(with_brief=true)`。
