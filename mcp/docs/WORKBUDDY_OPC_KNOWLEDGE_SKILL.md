---
name: opc-knowledge
description: 当用户提到「知识库、知识管理、OPC、同心圆、知识图谱、图数据库、共享资料、公开下载、热点」等词，或要检索公司内部资料、基于资料问答、查看/上传/导出素材、看本人手机号下资源或全局共享内容时，使用本技能通过已配置的 OPC MCP（连接器名 opc-kb）完成。触发词：知识库、知识管理、OPC、同心圆、知识图谱、图数据库、共享资料、公开下载、热点、kb、素材、内化、资料检索、资源互通。
---

# OPC 知识库（opc-knowledge）

## 何时使用
只要用户话语中出现上面 description 里的任一触发词，或意图是「查资料 / 问公司知识 / 看素材 / 传文件 / 导出材料」，就启用本技能，通过 `opc-kb` MCP 工具完成。

- 不要凭记忆猜公司内部信息，也不要回答「我没有知识库」——工具就在 `opc-kb` 连接器里。
- 不确定该调哪个工具时，先调 `opc_route(query=<用户原话>)` 拿推荐工具链。

## 前置条件
- 用户已在 WorkBuddy 配好 `opc-kb` MCP：`url = https://ai.boraydata.com/mcp`，请求头 `X-Mail-Agent` 由连接器注入。
- 身份（手机号）由 key 自带，**操作工具时不要传 phone，也不要在对话里向用户索要手机号或 key**。
- 工具返回 `need_auth` / `need_secret` 时，引导用户访问 `https://ai.boraydata.com/workbench/mcp-key` 领取或更新个人 key，不要把错误解读成「没有数据」。

## 标准流程（知识类问题）
1. `opc_whoami`（可选）：确认租户与身份；返回里的服务端路径字段只用于确认连通，不要转述给用户。
2. `kb_status`（可选）：看素材数、可检索片段数、LLM 状态，避免对空库发问。
3. `kb_search`：`query=<关键词>, k=6`，拿片段与出处；适合「有哪些 / 在哪里 / 原文是什么」。
4. `ask`：`text=<问题>, wait_sec=60`，拿 LLM 综合回答（结果含 citations）；适合「总结 / 解释 / 为什么 / 怎么做」。
   - 问题内容用 **`text` 或 `question`** 任一传入均可（两者等价，推荐 `text`）；`query` 是 `kb_search`/`opc_route` 的参数。
   - **库内事实类问题加 `with_brief=true`**：自动注入按五行三才分组的知识图谱简报（硬字符预算 900，不随知识库变大膨胀），结果多返回 `knowledge_brief` 元数据；此时通常**不必再先调 `kb_search`**。可用 `brief_level=0/1/2` 控制层级、`brief_chars` 控制预算（200~2000）。
   - 创作类、库内无答案类问题不加也行：零命中时简报为空串，不会增加开销。
   - `with_brief=true` 同时是对外安全口径：正文、引用标题与路径、回答中的内网地址/服务端路径会被替换成占位；素材引用不再给服务端绝对路径，而给 `ref=material:<id>`（配合 `opc_resource_read`）。
5. 不确定用哪个工具时，先调 `opc_route(query=<原话>)`。

## 工具速查
| 意图 | 工具与关键参数 |
| --- | --- |
| 我是谁 / 数据在哪 | `opc_whoami`（无参数） |
| key 能看到哪些资源 | `opc_resources`（OPC 库 / vault / MCP 素材 / 全局共享 `shared` 总览） |
| 知识库状态 | `kb_status` |
| 跨源检索（OPC+Obsidian+Web+MCP 素材+全局共享） | `kb_search(query, k=6)`，命中带 `source`：`opc.*` / `mcp.material` / `shared.*` |
| 列出 OPC 资源 | `opc_resource_list(kind=knowledge/reports/contents/messages/approvals/vault)` |
| 列出全局共享 | `opc_resource_list(kind=shared_knowledge/shared_materials/shared_hotspots/downloads)` |
| 读取单个资源 | `opc_resource_read(ref=knowledge:kb_xxx / vault:相对路径 / shared_knowledge:<id> / shared_material:<id> / shared_download:<文件名>)` |
| 综合问答 | `ask(text 或 question, wait_sec=60)` |
| 路由兜底 | `opc_route(query)` |
| 看素材列表 | `material_list(limit=50)` |
| 导出素材 | `material_download(material_id, dest?)` |
| 删除自己上传的素材 | `material_delete(material_id, remove_file?)`：只能删本 key 账号的素材，同步清掉切块/卡片；已内化进 OPC 库/vault 的条目不受影响 |
| 传小文件（<100MB） | `material_upload(path 或 content_base64, filename)` |
| 传大文件（≤2GB） | `upload_begin(filename, size, chunk_size)` → 返回 put_url/progress_url |
| 重新分析 | `material_analyze(material_id 或 all)` + `job_get(job_id, wait_sec)` |
| 语音 / 图片 | `tts(text, voice?, engine?)` / `image(prompt, width?, height?)` |

## 资源互通与边界
- 同一把 key 可访问本人手机号下的 OPC 库、Obsidian vault、MCP 素材——Web/Obsidian 生成的资料 MCP 也能检索读取，MCP 问答也会回流 Web 消息中心。
- **全局共享部分所有 key 可见**：全公司知识、共享素材清单、公开下载区、共享热点流（`shared.*`），不按手机号隔离；这与 Web 的 `/materials/`、`/downloads/`、共享热点流公开口径一致。
- 敏感文件按 Web 同口径屏蔽（登录凭据、隐藏段、运维手册、台账/凭据/拓扑结尾、下划线开头），`opc_resource_read` 会被拒绝，不要尝试绕过。共享知识只取 `project_id=全公司`，不泄漏其他手机号的私有条目。
- 私有资源只读自己手机号；vault 路径越界（`../`）会被拒绝。

## 回答用户时
- 先给结论再给依据；引用用 `kb_search` 的 `source`/`title`/`path` 或 `ask` 的 `citations` 标出。
- 检索不到就明确说「知识库没有命中」，不要用通用知识冒充公司内部资料。
- 图片类素材只归档、不做像素级理解；问「图片里是什么」要明确说明限制。
- 写操作（上传 / 分析 / 删除 / TTS / 生图）执行前确认用户确实需要，避免重复触发产生垃圾文件。
- **对外只说能力，不说部署**：不要转述服务端绝对路径、内网 IP/端口或内部实现文件名；工具返回的 `dataDir`/`workspace` 仅用于判断连通，不写进回复。

## 安装位置（陌生用户）
把本文件内容原样保存为：

| 系统 | 路径 |
| --- | --- |
| Windows | `%USERPROFILE%\.workbuddy\skills\opc-knowledge\SKILL.md` |
| macOS / Linux | `~/.workbuddy/skills/opc-knowledge/SKILL.md` |

保存后在 WorkBuddy 里刷新技能列表；首次使用的 key 领取与 MCP 配置见随附 `WORKBUDDY_EXTERNAL_USER_GUIDE.md`。
