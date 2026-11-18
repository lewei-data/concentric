# OPC MCP Client 当前用法（权威口径，核实于 2026-11-18）

> 本文以线上 `opc_mcp/server.py::TOOLS` 为准，是当前唯一权威的 client 用法基线。
> 历史文档中的工具数（13/14/15/18/19）、token 说法、"图片只归档"等表述均已过时，以本文为准。

## 1. 接入与鉴权（唯一一套凭据）

| 项 | 值 |
| --- | --- |
| 公网 Endpoint | `https://ai.boraydata.com/mcp`（Streamable HTTP） |
| 鉴权头 | `X-Mail-Agent: <个人 API key>`，由 MCP 连接器/网关自动注入为工具参数 `client_key` |
| 领取 key | `https://ai.boraydata.com/workbench/mcp-key`（登录后自助生成，可多把并存，新增不撤销旧 key） |
| 手机号 | **由 key 自带，不要传 `phone`，也不要向用户索要手机号或 key** |

- **全套 21 个工具共用这一把 key**：注册、检索、问答、上传、生成、取回都用它，没有第二套 token。
- 工具 schema 里**没有 `token` 字段**，也不需要手写 `client_key`（网关注入）。看到旧文档里 `token=<...>` 的写法一律忽略；传 `token` 只作兼容，等同 `client_key`。
- `opc_init(key=...)` 可用 key 换 `session_token`，但普通工具调用不需要它。
- 返回 `need_auth` / `need_secret` 时，引导用户去领取/更新 key，**不要解读成"没有数据"**。

## 2. 工具清单（21 个，按用途分组）

### 身份与路由（无需鉴权）
| 工具 | 关键参数 | 用途 |
| --- | --- | --- |
| `opc_init` | `key?`, `phone?`, `password?` | key 换 session_token，验证连通 |
| `opc_login` | `key?`, `phone?`, `password?` | 密码/key 登录 |
| `opc_whoami` | 无 | 确认身份与租户（服务端路径只用于自检，不要转述） |
| `opc_route` | `query` | 不确定用哪个工具时，传用户原话拿推荐工具链 |

### 知识与问答
| 工具 | 关键参数 | 用途 |
| --- | --- | --- |
| `kb_status` | 无 | 素材数、片段数、LLM 探活状态 |
| `kb_search` | `query`, `k=6` | 跨源检索（OPC + vault + Web + MCP 素材 + 全局共享），命中带 `source` |
| `ask` | `text` 或 `question`（等价，推荐 `text`）, `wait_sec=60`, `with_brief=true` | 综合问答；库内事实类带 `with_brief` 自带五行三才图谱简报与对外脱敏，通常不必先 `kb_search` |

### 资源（本人 + 全局共享）
| 工具 | 关键参数 | 用途 |
| --- | --- | --- |
| `opc_resources` | 无 | 本人 OPC 库/vault/MCP 素材/全局共享总览 |
| `opc_resource_list` | `kind`, `limit=20` | 本人：`knowledge/reports/contents/messages/approvals/vault`；**全局共享：`shared_knowledge/shared_materials/shared_hotspots/downloads`** |
| `opc_resource_read` | `ref` | `knowledge:kb_xxx` / `vault:相对路径` / `material:<id>` / `shared_knowledge:<id>` / `shared_material:<id>` / `shared_download:<文件名>` |

> **共享资料路由纠错**：问"公司共享的资料/公开下载区"用 `opc_resource_list(kind=shared_materials)` 或 `kind=downloads`；
> `material_list` 只查**自己上传的素材**，与共享区无关。

### 素材上传与管理
| 工具 | 关键参数 | 用途 |
| --- | --- | --- |
| `material_upload` | `path` 或 `content_base64`, `filename` | 小文件（<100MB）；`path` 是服务端路径，外部客户端请用 `content_base64` 或 `upload_begin` |
| `upload_begin` | `filename`, `size`, `chunk_size` | 大文件（≤2GB）分片续传，返回 `put_url` / `progress_url` |
| `material_list` | `limit=50` | 本人素材清单 |
| `material_analyze` | `material_id` 或 `all` | 重新分析（入队后自动 OCR/ASR + LLM 卡片 + 知识库 + 图谱 + 评分，结果经小秘书/飞书汇报） |
| `material_download` | `material_id`, `inline=false`, `dest?` | 导出素材；默认返回服务端路径，`inline=true` 且 ≤4MB 才附 base64 |
| `material_delete` | `material_id`, `remove_file?` | 删本人素材（先确认），同步清切块/卡片 |
| `job_get` | `job_id`, `wait_sec` | 查异步作业 |

### 生成与取回（同一把 key，无单独 token）
| 工具 | 关键参数 | 用途 |
| --- | --- | --- |
| `media_capabilities` | 无 | 查询渠道、音色、支持模式、限制（尺寸/文本长度/耗时），**调 tts/image 前先查** |
| `tts` | `text`（口播稿）, `brief`（需求描述）, `notes`（注意事项→Qwen3 instruct）, `channel=cosyvoice\|tts\|qwen3`, `voice`, `speed`, `wait_sec` | 合成音频；`tts`/`qwen3` 走 Qwen3-TTS，`cosyvoice` 走 CosyVoice |
| `image` | `prompt`, `brief`, `reference_image`（本地绝对路径或 data URI）, `channel=sd15\|qwen-image`, `width/height/steps`, `wait_sec` | 文生图（sd15）；给参照图或指定 `qwen-image` 走 8188 Qwen-Image 图生图 |
| `file_link` | `file_key` | 换设备/链接丢失时凭生成结果里的 `file_key` 重取下载链接 |

## 3. 生成文件怎么取回本地（关键）

`tts` / `image` 成功后，结果里除 `path`（**服务端路径，跨网络不可用**）外，还会给：

```json
{
  "file_key": "不透明标识",
  "download_url": "https://ai.boraydata.com/workbench/api/mcp/file/<file_key>",
  "download_auth": "X-Mail-Agent: <生成时用的同一把个人 API key>",
  "download_example": "curl -L -H \"X-Mail-Agent: $OPC_KEY\" \"...\" -o 文件名"
}
```

**下载命令**（与生成用同一把 key，不需要网页登录、不需要 conring 独立 token、不要内联 base64）：

```bash
curl -L -H "X-Mail-Agent: $OPC_KEY" "$download_url" -o output.wav
# 或：curl -L -H "Authorization: Bearer <session_token>" "$download_url" -o output.wav
```

- 端点 `GET /api/mcp/file/<file_key>` 支持 Range，按 phone 隔离，产物 7 天过期。
- `file_link(file_key)` 用同一把 key 重取链接；别人的 key 取不到。
- 不要再把 `srt_url` / `output_url`（内网 `/files/...` 相对路径）当成公网可访问地址。

## 4. 探活与降级

- `kb_status` 的 `llm` 字段现在来自**真实 chat 探活**（`max_tokens=1`），不是只看 HTTP 通不通：
  - `llm.ok=true` 才代表模型当前有部署；
  - 冷却期返回 `llm.ok=false`，`llm.probe.status=429`、`llm.probe.retry_after_sec`（秒）、`cooldown_until`，`detail` 里有可读的"冷却约 N 小时"。
- auto-free 冷却可达 6.8 小时，期间 `ask` 会降级为纯检索摘要。**看到 `kb_status.ok=true` 但 `ask` 说降级时，先看 `llm.probe.retry_after_sec`，不要以为是检索失败。**

## 5. 对外红线

- 只用公网入口；不转述服务端绝对路径、`dataDir`/`workspace`、内网 IP/端口或内部实现文件名。
- 素材内容化会自动 OCR/ASR + 知识库入库 + 评分 + 汇报；**"图片只归档不做理解"的说法已过时**，但像素级语义（如"照片里的人是谁"）仍取决于识别结果，识别不到要明确说明。
- 敏感文件按 Web 同口径屏蔽（登录凭据、隐藏段、运维手册、台账/凭据/拓扑结尾、下划线开头），不要尝试绕过。
- 写操作（上传/分析/删除/TTS/生图）执行前确认用户确实需要。
