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

其余工具同理，共 19 个。

## 4. 能力边界与限制

- DSH 的 MCP 集成只暴露 **tools**；resources / prompts 没有消费者。
- 二进制内容（图片、音频）在 DSH 侧会变成占位丢失；TTS / 生图类工具返回的是可访问路径字符串，所以可以正常使用。
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
