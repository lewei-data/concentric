# OPC MCP Client 当前用法（权威口径，核实于 2026-11-19）

> 本文以线上 `opc_mcp/server.py::TOOLS` 为准，是当前唯一权威的 client 用法基线。
> 历史文档中的工具数（13/14/15/18/19/22）、token 说法、"图片只归档"等表述均已过时，以本文为准。
>
> **契约 v2（2026-11-19 起）**：每个工具带 `annotations`（readOnlyHint/destructiveHint/
> idempotentHint），客户端应按注解过滤只读白名单，不要硬编码工具名；错误返回带稳定 `code`；
> `ask` 支持 `idempotency_key` 幂等重放；`material_list` 支持 cursor 分页；上传带同名取代语义；
> `kb_status`/`opc_whoami` 默认脱敏；新增轻量健康检查 `kb_ping`。详见第 6 节。

## 1. 接入与鉴权（唯一一套凭据）

| 项 | 值 |
| --- | --- |
| 公网 Endpoint | `https://ai.boraydata.com/mcp`（Streamable HTTP） |
| 鉴权头 | `X-Mail-Agent: <个人 API key>`，由 MCP 连接器/网关自动注入为工具参数 `client_key` |
| 领取 key | `https://ai.boraydata.com/workbench/mcp-key`（登录后自助生成，可多把并存，新增不撤销旧 key） |
| 手机号 | **由 key 自带，不要传 `phone`，也不要向用户索要手机号或 key** |

- **全套 22 个工具共用这一把 key**：注册、检索、问答、上传、生成、取回都用它，没有第二套 token。
- 工具 schema 里**没有 `token` 字段**，也不需要手写 `client_key`（网关注入）。看到旧文档里 `token=<...>` 的写法一律忽略；传 `token` 只作兼容，等同 `client_key`。
- `opc_init(key=...)` 可用 key 换 `session_token`，但普通工具调用不需要它。
- 返回 `need_auth` / `need_secret` 时，引导用户去领取/更新 key，**不要解读成"没有数据"**。

## 2. 工具清单（23 个，按用途分组）

### 身份与路由（无需鉴权）
| 工具 | 关键参数 | 用途 |
| --- | --- | --- |
| `opc_init` | `key?`, `phone?`, `password?` | key 换 session_token，验证连通 |
| `opc_login` | `key?`, `phone?`, `password?` | 密码/key 登录 |
| `opc_whoami` | `detail?` | 确认身份与租户；默认不回服务端路径，`detail=true` 仅管理员 key 可见完整信息 |
| `opc_route` | `query` | 不确定用哪个工具时，传用户原话拿推荐工具链 |
| `kb_ping` | `refresh?` | 轻量健康检查（LLM/语音/图像三盏灯），结果缓存约 60 秒；**面板轮询用这个，不要轮询 kb_status** |

### 知识与问答
| 工具 | 关键参数 | 用途 |
| --- | --- | --- |
| `kb_status` | `detail?` | 素材数、片段数、LLM 探活；**默认已脱敏**（不含服务端路径/内网地址），`detail=true` 仅管理员 key 放开；面板轮询请用 `kb_ping` |
| `kb_search` | `query`, `k=6` | 跨源检索（OPC + vault + Web + MCP 素材 + 全局共享），命中带 `source`；最高分低于阈值时返回 `low_confidence: true` + `hint`（换自然语言再问） |
| `ask` | `text` 或 `question`（等价）, `idempotency_key?`, `wait_sec=60`, `with_brief=true` | 综合问答；**超时重试务必带同一 `idempotency_key`**（窗口 15 分钟，重放返回同一 `job_id` + `idempotent_replay: true`，不会双排任务）；`wait_sec` 上限 300，到点未完成返回 `status=queued/running` + `retry_after_sec`，用 `job_get` 续等 |

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
| `material_upload` | `path` 或 `content_base64`, `filename`, `supersedes?`, `keep_history?` | 小文件（<100MB）；**版本语义**：同名旧素材默认被本版本取代（旧版切块/卡片/内化条目不再参与召回，文件保留可审计）；`keep_history=true` 保留多版本并存；`supersedes=<material_id>` 显式指定被取代者。返回 `superseded` 列表 |
| `upload_begin` | `filename`, `size`, `chunk_size` | 大文件（≤2GB）分片续传，返回 `put_url` / `progress_url`；完成后同名旧版同样自动取代 |
| `material_list` | `limit=50`, `cursor?`, `include_superseded?` | 本人素材清单；默认不含被取代旧版；返回 `total`/`has_more`/`next_cursor`，用 `cursor` 翻页 |
| `material_analyze` | `material_id` 或 `all` | 重新分析（入队后自动 OCR/ASR + LLM 卡片 + 知识库 + 图谱 + 评分，结果经小秘书/飞书汇报）；被取代（superseded）的素材会被拒绝 |
| `material_download` | `material_id`, `inline=false`, `dest?` | 导出素材；默认返回服务端路径，`inline=true` 且 ≤4MB 才附 base64 |
| `material_delete` | `material_id`, `remove_file?` | 删本人素材（先确认），同步清切块/卡片；`annotations.destructiveHint=true` |
| `job_get` | `job_id`, `wait_sec` | 查异步作业；未完成时带 `retry_after_sec`，按它轮询；返回结构带 `job_contract: 2` |

### 生成与取回（同一把 key，无单独 token）
| 工具 | 关键参数 | 用途 |
| --- | --- | --- |
| `media_capabilities` | 无 | 查询渠道、音色、支持模式、限制（尺寸/文本长度/耗时），**调 tts/image 前先查**；音色只回 `id`+说明，不回参考音频/模型权重的落盘路径。百炼渠道在 `audio.channels[name=bailian]`/`image.channels[name=bailian_image]`，**601 个百炼音色在 `audio.bailianVoices`**（每项 `id`+`name`+`label`+性别/年龄/特质/场景），可用性看渠道 `available` |
| `tts` | `text`（口播稿）, `brief`（需求描述）, `notes`（注意事项→Qwen3 instruct）, `channel=cosyvoice\|tts\|qwen3\|bailian`, `voice`, `speed`, `wait_sec`；**bailian 专用**：`format`(mp3/wav/pcm/opus), `sample_rate`(8000~48000) | 合成音频；`tts`/`qwen3`=Qwen3-TTS，`cosyvoice`=CosyVoice，`bailian`=阿里云百炼云服务（qwen-audio-3.0-tts-plus，按字符计费，同步返回后服务端立即下载转 `file_key`）。百炼音色从 `media_capabilities` 的 `audio.bailianVoices` 取 `id` 传给 `voice`，不传默认 `longanhuan_v3.6` |
| `image` | `prompt`, `brief`, `reference_image`（本地绝对路径或 data URI）, `channel=sd15\|qwen-image\|bailian_image`, `width/height/steps`, `wait_sec`；**bailian_image 专用**：`size`(「宽*高」星号，如 1024*1024), `n`(1~4), `prompt_extend`, `watermark` | 文生图；`sd15`=9103，`qwen-image`=8188/157 Qwen-Image 图生图；`bailian_image`=阿里云百炼 qwen-image-3.0-pro 云服务（按张计费，支持文生图/图生图，参考图内联 base64） |
| `file_link` | `file_key` | 换设备/链接丢失时凭生成结果里的 `file_key` 重取下载链接 |

### 员工照片（全局共享）
| 工具 | 关键参数 | 用途 |
| --- | --- | --- |
| `staff_photos` | `kind=all\|portrait\|halfbody\|digital_human`, `limit=200` | 列出员工本人形象 / 半身照 / 数字人口播形象，返回每人的 `name`/`kind`/`file_key`/`download_url`；**所有 key 可见**，不按手机号隔离 |

## 3. 生成文件怎么取回本地（关键）

`tts` / `image` 成功后，结果里**不再含服务端 `path`**（已在出口移除，避免远程 agent 照抄泄漏落盘位置），只给：

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
- 生成类工具与能力查询按 `ask(with_brief)` 同源对外口径净化：`media_capabilities` 的音色仅 `id`+`description`（无 `prompt_wav`）、模型后端无 `path`、`reachability` 内网地址换占位；`tts`/`image` 结果无 `path`。照抄返回也不会泄漏服务端路径。

## 3b. 员工照片/半身照怎么取回

`staff_photos` 列出公司共享的员工照片（本人形象 19 张、半身照 19 张、数字人口播形象 1 张），
**所有 key 可见**（与公开下载区同口径，不按手机号隔离）。结果只给 `name`/`kind`/`file_key`/`download_url`，不含服务端路径：

```json
{"scope": "global-shared", "count": 39, "items": [
  {"name": "01 蒿瑟莫 · 老板", "kind": "portrait", "file_key": "sp...",
   "download_url": "https://ai.boraydata.com/workbench/api/mcp/file/sp..."}
]}
```

```bash
# 与生成文件同一条取回通道，同一把个人 key
curl -L -H "X-Mail-Agent: $OPC_KEY" "$download_url" -o 员工照片.png
```

- `kind=portrait`（本人形象）/`halfbody`（半身照）/`digital_human`（数字人）/ `all`（默认）。
- `file_key` 为确定性哈希（`sp` 前缀），换设备、重启后仍稳定；链接支持 Range。
- 半身照按员工 id 命名，清单里已用名册补上中文姓名与职位。

## 3c. 阿里云百炼云服务渠道（配音 / 选音色 / 生图）

百炼是**公网云服务，按量计费**，不占本机 GPU；此前只在网页加工厂可见，现已开放到 MCP。
鉴权仍用同一把个人 `client_key`，百炼 API Key 是服务端配置，客户端不传也不应索要。

**① 发现渠道与音色**（只读，不烧额度）：`media_capabilities()` →
`audio.channels[name=bailian].available` / `image.channels[name=bailian_image].available` 为 `true` 表示 Key 已配置；
601 个音色在 `audio.bailianVoices`，按 `label`（含中文说明/性别/年龄/特质/场景）挑 `id`。

**② 配音（选音色）**：
```json
tts({"text":"欢迎来到 conring","channel":"bailian","voice":"longanhuan_v3.6","format":"mp3"})
```
- `voice` 取 `audio.bailianVoices[].id`；不传默认 `longanhuan_v3.6`（4 个官方精品音色之一，Key 实测可用）。
- 官方清单共 601 个（4 系统 + 597 基础），基础音色形如 `qwen-audio-3.0-tts-plus-longcanzhuyue`；
  清单**已剔除 flash 系列**（历史实测中 5 个 flash 音色在 plus 模型上报 411，现已不在 601 清单内；当前清单中无 id 含 flash 的样本）。
  从清单外自行拼 flash 音色 id 不会被校验拦下，但 plus 模型不兼容。情绪/语速细粒度控制按官方说明写在 `text` 标签里。
- `format` 支持 mp3/wav/pcm/opus，`sample_rate` 默认 22050。

**③ 生图 / 图生图**：
```json
image({"prompt":"一只戴墨镜的柴犬","channel":"bailian_image","size":"1024*1024","n":1})
```
- 图生图：再传 `reference_image`（本地绝对路径或 `data:image/...;base64`），服务端内联参考图，无需公网 URL。
- `size` 用「宽*高」**星号**（非 x），面积 512²~2048²、长宽比 1:8–8:1；推荐 1024*1024 / 768*1024 / 1024*768。
- `n` 多张按张数计费；`prompt_extend`（默认 true）对短提示词更稳；`watermark` 默认 false。

**④ 下载**：与本机渠道完全一致——② ③ 的返回里取 `file_key`/`download_url`，按第 3 节 curl 命令下载；
`file_link(file_key)` 可重取链接。百炼返回的时效音频/图片 URL 已在服务端立即下载，客户端拿不到也不会过期。

**计费提示**：百炼是唯一按量计费的渠道（TTS 按字符、图像按张）。客户没明确要求时，优先用本机免费渠道；
明确要"云服务/百炼/阿里云/更好音色/更快出图"才走 `bailian`/`bailian_image`。

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

## 6. 契约 v2（2026-11-19 起，DSH 面板整改 S 系列）

### 6.1 工具注解（按注解过滤，别再硬编码白名单）

`tools/list` 的每个工具带 `annotations`：

- `readOnlyHint: true` = 不改动用户数据，可安全用于只读面板（`opc_whoami`/`kb_ping`/`kb_status`/
  `kb_search`/`ask`/`job_get`/`material_list`/`opc_resources`/`opc_resource_list`/`opc_resource_read`/
  `opc_route`/`media_capabilities`/`file_link`/`staff_photos`）。
  `ask` 虽只读但会创建后台作业并消耗 LLM 额度，重试请带 `idempotency_key`。
- `destructiveHint: true` = 删除类（目前仅 `material_delete`），调用前必须与用户确认。
- 工具将来改名会保留别名至少一个发布周期，并在注解里标 `deprecated` + `replacement`。

### 6.2 稳定错误码

失败返回统一带 `code`（旧字段 `status` 保留兼容）：`invalid_argument` / `not_found` /
`unauthorized`（旧 need_auth）/ `forbidden`（旧 blocked）/ `rate_limited`（带 `retry_after_sec`）/
`upstream_unavailable`（媒体/上游故障）/ `internal_error`。客户端按 `code` 决定重试或提示。

### 6.3 幂等与等待语义

- `ask` 接受 `idempotency_key`（≤128 字符）：窗口内（默认 900 秒）同 key 重放返回同一 `job_id`
  （带 `idempotent_replay: true`），不会双排任务。**任何"失败了想重试一次"的客户端都必须带它。**
- `ask(wait_sec)` / `job_get(wait_sec)`：到点未完成统一返回 `status: queued|running` +
  `retry_after_sec`（默认 5 秒）+ `job_contract: 2`，按 `retry_after_sec` 轮询 `job_get`。

### 6.4 上传版本语义

- 相同内容（sha256）去重：返回既有 `material_id` + `duplicate: true`。
- 相同文件名的**新版**默认取代旧版：旧素材 `status=superseded` + `superseded_by`，其切块/卡片/
  已内化知识条目不再参与 `kb_search`/`ask` 召回；素材行与文件保留可审计，被回收的 vault md
  移到 `<用户根>/mcp/backup/superseded/<日期>/` 可人工恢复。`keep_history=true` 保留多版本并存。
- `material_list` 默认不列被取代旧版；`include_superseded=true` 可见全部。

### 6.5 分页

`material_list` 返回 `total` / `has_more` / `next_cursor`；下一页传 `cursor=<上一页的 next_cursor>`
（原样透传，非法值报 `invalid_argument`）。`limit` 默认 50、上限 200。

### 6.6 脱敏与健康检查

- `kb_status` / `opc_whoami` 默认响应不含服务端绝对路径与内网地址（正文再过一层占位替换）；
  `detail=true` 只对管理员手机号（`OPC_MCP_DETAIL_PHONES`）放开。
- `kb_ping` 为缓存式轻量健康检查（约 60 秒缓存），面板轮询用 `kb_ping`，不要轮询 `kb_status`
  （后者每次都真实探测上游模型）。

### 6.7 限流与低置信度

- 每 key 滑动窗口限流（默认 40 次 / 10 秒，`OPC_MCP_RATE_MAX`/`OPC_MCP_RATE_WINDOW` 可调），
  超限返回 `code: rate_limited` + `retry_after_sec`。
- `kb_search` 最高分低于阈值（默认 6.0）或零命中时返回 `low_confidence: true` + `hint`；
  符号化提问（标识符/路径/参数名堆叠）建议换自然语言描述。

### 6.8 观测

`ask` 每次完成会追加一行 JSON 到服务端 `data/metrics.jsonl`（时延、degraded、失败原因、引用数，
手机号只记指纹），供运维统计 p50/p95 与降级率。

### 6.9 并发模型与实时推送（2026-11-19 二次优化）

**并发**：stdio 服务端是「stdin 单读线程 + `OPC_MCP_REQUEST_WORKERS` 请求线程池（默认 8）」，
响应按 JSON-RPC id 回写，stdout 单行原子写入。实测两个 `wait_sec=5s` 的调用总墙钟 5.2s（原先 10.4s），
长等待调用不再阻塞 `ping`/检索。后台作业池仍是 `JOB_WORKERS`（默认 2），可按需调高。

**SSE 作业事件**：服务端在每个作业终态向 stdout 发 JSON-RPC 通知
`notifications/opc/job`，mcp-bridge 绑定后按手机号推送：

```
GET https://ai.boraydata.com/mcp/events
Header: X-Mail-Agent: <个人 API key>
Accept: text/event-stream
```

- 必须用 `fetch + ReadableStream` 读流并解析 `event: job` 帧；**EventSource 不能设置自定义头**，
  不要用它。首帧为 `: connected`，之后每 20 秒 `: ping` 保活。
- 事件体：`{"job_id","kind","status","phone","error","at"}`，只推送同账号事件；
  断线期间的事件不补发，重要场景仍用 `job_get` 兜底。
- 面板/插件拿到 `ask(wait_sec=0)` 的 `job_id` 后订阅此流，作业完成即刷新，无需轮询。

**Webhook**：bridge 进程设置 `OPC_MCP_JOB_WEBHOOK_URLS`（逗号分隔多个 URL）后，
每条作业事件会 POST JSON 到这些地址；设置 `OPC_MCP_WEBHOOK_SECRET` 时带
`X-OPC-Event-Signature: sha256=<HMAC-SHA256>` 供接收方校验。生产当前未配置 URL（按需启用）。

**覆盖范围**：经 MCP stdio 提交的作业（`ask`、`material_upload` 的 analyze）会推送；
大文件分片通道（9105 上传服务）提交的作业暂不推送，仍用 `job_get` 轮询。
