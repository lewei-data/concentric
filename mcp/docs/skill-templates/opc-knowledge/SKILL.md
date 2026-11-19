---
name: opc-knowledge
description: 查询 OPC 内化知识库、素材与跨源资源；库内事实问答、素材删除与出处追溯时使用
whenToUse: 用户问公司内部资料/规范/盘点，或要求查素材、删自己上传的素材、追出处
---

# OPC 知识库与素材

## 身份

可选：先调用 `mcp__opc__opc_whoami` 确认身份是否识别成功。返回里的 `dataDir` / `workspace` 只用于连通性自检，不要转述给用户。

## 库内事实问答

库内事实、规范、盘点、配置类问题，直接用 `mcp__opc__ask` 并带 `with_brief=true`：

- 简报自带按五行三才分组的图谱信息，有字符硬预算，通常不必先调 `mcp__opc__kb_search`。
- 该模式同时按对外口径处理内网地址与服务端路径，无需额外过滤。
- 检索不到就明确说没有，不要用通用知识冒充公司内部资料。

## 素材

- 用户说"我上传的素材"：先 `mcp__opc__material_list` 拿 id。删除前跟用户确认，再调 `mcp__opc__material_delete`。
- 导出已上传素材用 `mcp__opc__material_download`；导出**生成的音频/图片**用生成结果里的 `download_url`（见下）。
- 问"公司共享的资料/公开下载区"：用 `mcp__opc__opc_resource_list(kind=shared_materials 或 downloads)`；`material_list` 只查本人上传的素材，不要混淆。

## 生成与取回（同一把 key，无单独 token）

- 调 `mcp__opc__media_capabilities` 查渠道/音色/限制，再调 `mcp__opc__tts(text, brief?, notes?, channel?, voice?)` 或 `mcp__opc__image(prompt, brief?, reference_image?, channel?)`。
- 生成结果含 `file_key` + `download_url`，**不含服务端 `path`**；取回用同一把个人 key：`curl -L -H "X-Mail-Agent: $OPC_KEY" "$download_url" -o 文件名`。
- 链接丢失时用 `mcp__opc__file_link(file_key=...)` 重取。不要用网页登录态、conring 独立 token 或内联 base64 搬运。
- 生成结果与 `media_capabilities` 已按对外口径净化：音色只回 `id`+说明，无参考音频/模型落盘路径；照抄返回不泄漏服务端路径，但仍不要转述任何服务端路径/IP。
- 员工照片/半身照用 `mcp__opc__staff_photos(kind=all\|halfbody\|portrait\|digital_human)` 列出（全局共享、所有 key 可见），拿返回的 `download_url` 用同一把 key 下载；不要试图找服务端路径。
- DSH 若把二进制变成占位，以 `download_url` 为准引导用户在自己机器执行 curl。

## 出处追溯

- 完整出处以 `knowledge_brief.items[].source` 或 `citations[].path` 为准；正文里的 `→ 路径` 是**截断显示，不可直接读取**。
- 取到完整 vault 相对路径后，用 `mcp__opc__opc_resource_read(ref=vault:完整路径)` 读原文；不要使用本地读文件命令。
- `citations` 命中 `opc.message` 时 `path` 为空，这类消息引用**不可追溯原文**，不要尝试构造路径。

## 八步执行法标签与员工对话

- `mcp__opc__kb_search` 的每条命中带 `reasoning`（1-8）与 `reasoning_name`：该材料服务八步执行法的主步骤（1立题 2证据 3结构 4决策 5执行 6反馈 7归档 8复盘）；按标签组织汇报时可用 `reasoning=N` 只取某一步的材料，返回体的 `reasoning_steps` 是命中分布。标签是推导值（一条材料可服务多步，这里只给主步骤），`null` 表示对不齐，不要硬编。
- 概览/面板数据用 `mcp__opc__kb_overview` 一次拿全（分组计数+健康灯+近期作业），带 `rev` 条件请求省流；`refresh=true` 立即重算，缓存后台自动预热，不需要自建轮询节流。
- 仅老板 key：`mcp__opc__staff_chat(list_agents=true)` 查 18 名员工清单，`staff_chat(agent=<id 或姓名>, message=<指令>)` 直接 @员工下指令并等真实回复；非老板 key 返回 forbidden，不要重试。

## 对外红线

- 只用公网入口连接。
- 回复里不转述服务端绝对路径、`dataDir` / `workspace`、内网 IP 或内部端口。
- 删除类操作执行前先确认。
