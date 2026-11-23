# 独立图片生成 MCP（MCP-IMAGE）客户端用法

> 权威口径，核实于 2026-11-24。服务名 `mcp-image`，stdio 上游 + mcp-bridge 包装成 Streamable HTTP。
> 与 `OPC_MCP_CLIENT_USAGE.md` 里的 OPC MCP 是**两个独立端点**，但**共用同一套个人 API key**。

## 1. 一句话接入

```json
{
  "mcpServers": {
    "image": {
      "type": "streamable-http",
      "url": "https://ai.boraydata.com/conring/image/mcp",
      "headers": { "X-Mail-Agent": "<OPC 个人 API key>" }
    }
  }
}
```

- 公网：`https://ai.boraydata.com/conring/image/mcp`
- 内网：`http://192.168.21.206:9106/mcp`
- 鉴权：请求头 `X-Mail-Agent` 带 OPC 个人 API key（与 `opc-kb` 同一把，无需单独申请）。
  bridge 会把 header 强制注入工具的 `client_key` 参数；模型自己手写 `client_key` 无效。
- 无 key 时 `tools/call` 返回 `isError: true`，正文提示缺少凭据。

## 2. 三渠道选型

| channel | 后端 | 能力 | 耗时 / 计费 | 关键限制 |
|---|---|---|---|---|
| `sd15`（默认） | 本机 `media-gen` @ `192.168.21.206:9103` | 仅文生图 | CPU 512² 约 9.5s/步；steps=2 小图实测 32s | 宽高 256~1024 且 8 的倍数；steps 1~50；模型 Realistic Vision V5.1 |
| `qwen-image` | 157 ComfyUI @ `192.168.21.157:8188`（Qwen-Image-2.1-Turbo） | 文生图 + 图生图 | 无 GPU，CPU 768² 实测 230s（约 4 分钟）；不计费 | 分辨率 512~1536 且 64 对齐；固定 8 步；参考图短边 ≥384、≤20MB；`wait_sec` 会自动抬到 ≥600 |
| `bailian_image` | 阿里云百炼 `qwen-image-3.0-pro` | 文生图 + 图生图 | **按张计费**，出图后服务端立即下载转 file_key | `size` 为「宽*高」星号格式，面积 512²~2048²、比例 1:8–8:1；`n` 1~4；图片 URL 仅 24h 有效 |

别名（会自动归一化）：`local`/`sd`/`本机` → sd15；`comfy`/`comfyui`/`157`/`qwen` → qwen-image；`bailian`/`aliyun`/`云` → bailian_image。

**选型建议**：先调 `image_capabilities` 看实时可达性；日常快速出图用 `sd15`；需要图生图或更好的画面质量用 `qwen-image`（耐心等）；`bailian_image` 只在明确接受计费时使用。

**注意**：传了 `reference_image` 时，无论 `channel` 写什么，实际都会走 `qwen-image`（sd15 不支持图生图）。显式写 `bailian_image` + 参考图则走百炼 i2i。

## 3. 工具清单（3 个）

### 3.1 `image_capabilities`

只读能力探测，不烧额度。入参空对象 `{}`。

返回要点：
- `channels`：三渠道的模型、支持模式、限制
- `reachability`：`image`（本机 9103）与 `comfy_image`（157）的实时可达性
- `identity`：调用方的账号指纹（`phone_fp`，不含明文手机号）
- `download`：下载 URL 模式与内联大小上限

### 3.2 `image_generate`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `prompt` | string | 必填 | 画面文字描述 |
| `brief` | string | — | 整体需求（用途、风格），可选 |
| `channel` | string | `sd15` | 见上表，支持别名 |
| `reference_image` | string | — | 参照图：服务端可见的绝对路径，或 `data:image/...;base64`；给出即图生图 |
| `negative_prompt` | string | — | 反向提示词（sd15 有效） |
| `width` / `height` | int | 640 / 832 | sd15 / qwen-image 用 |
| `steps` | int | 20 | sd15 用；qwen-image 固定 8 步 |
| `seed` | int | — | 随机种子，固定可复现 |
| `size` | string | `1024*1024` | 仅百炼，星号格式 |
| `n` | int | 1 | 仅百炼，1~4，**按张计费** |
| `prompt_extend` / `watermark` | bool | true / false | 仅百炼 |
| `wait_sec` | number | 300 | 等待终态秒数；qwen-image 自动抬到 ≥600 |
| `return_base64` | bool | false | 是否内联返回 base64 图片（≤8MB；超过只回 `inline_skipped`） |

**返回**（成功）：
```json
{
  "channel": "sd15",
  "mode": "t2i",
  "file_key": "N4NT1u8Bz3DEgbhQuUC49No",
  "filename": "image.png",
  "mime": "image/png",
  "size": 134793,
  "download_url": "https://ai.boraydata.com/conring/image/assets/N4NT1u8Bz3DEgbhQuUC49No/image.png",
  "elapsed_sec": 32.04,
  "image_base64": "...",
  "image_mime": "image/png"
}
```
`image_base64` / `image_mime` 仅在 `return_base64=true` 且文件 ≤8MB 时出现；此时 MCP 响应还会带一个 `type: "image"` 内容块，多数支持图片的 agent 可直接看到图。

**返回**（失败）：`isError: true`，`content[0].text` 写人类可读原因（如未知渠道、参考图超限、后端不可达、生成超时）。

### 3.3 `image_file_link`

`{"file_key": "..."}` → 重新签发 `download_url`。只能取同一把 key 生成的产物，跨身份返回「不属于当前身份」。

## 4. 调用示例

### 4.1 Python（streamable HTTP，带会话）

```python
import json, urllib.request

KEY = "<OPC 个人 API key>"
URL = "https://ai.boraydata.com/conring/image/mcp"

def post(body, sid=None):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "X-Mail-Agent": KEY}
    if sid: h["mcp-session-id"] = sid
    r = urllib.request.urlopen(
        urllib.request.Request(URL, data=json.dumps(body).encode(), headers=h), timeout=1200)
    raw = r.read().decode()
    if raw.startswith("data:"):          # SSE 帧兼容
        raw = "\n".join(l[5:] for l in raw.splitlines() if l.startswith("data:"))
    return (json.loads(raw) if raw.strip() else None), (r.headers.get("mcp-session-id") or sid)

init, sid = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "my-agent", "version": "1"}}})
post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
r, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "image_generate",
                        "arguments": {"prompt": "一只橘猫坐在窗台上，阳光明媚",
                                      "channel": "sd15"}}}, sid)
result = json.loads(r["result"]["content"][0]["text"])
print(result["download_url"])
```

要点：initialize 后必须发 `notifications/initialized`（返回 202 空体，属正常）；后续请求带 `mcp-session-id`。

### 4.2 图生图（157）

```json
{"name": "image_generate", "arguments": {
  "prompt": "把这张图改成梵高油画风格，保留主体构图",
  "channel": "qwen-image",
  "reference_image": "data:image/png;base64,iVBORw0KGgo...",
  "width": 768, "height": 768, "wait_sec": 900
}}
```

参考图也可传服务端可见路径，例如另一张已生成图的本地路径。短边必须 ≥384。

### 4.3 下载产物

```bash
curl -L "https://ai.boraydata.com/conring/image/assets/<file_key>/<filename>" -o image.png
```

静态链接**不需要 key**；安全性依赖 `file_key` 不可枚举（`secrets.token_urlsafe(18)`）。丢链接时用 `image_file_link` 重取。

## 5. 红线与限制

- **百炼按张计费**：`bailian_image` 的 `n>1` 会成倍扣费；调用前务必确认。
- **CPU 推理慢**：本机与 157 都无 GPU，qwen-image 768² 实测约 230s；务必把 HTTP 超时设到 1200s，别在 agent 侧设 60s 就放弃。
- **图生图参考图**：短边 ≥384、≤20MB；过小会被后端拒绝。
- **不是 OPC MCP**：本服务**没有** `opc_init` / `ask` / `kb_*` 等知识库工具；那些走 `https://ai.boraydata.com/mcp`。
- **产物目录按 file_key 隔离**，索引 `output/index.json` 只记账号指纹，不记明文手机号。

## 6. 运维信息

| 项 | 值 |
|---|---|
| stdio server | `/opt/sefs/opc-workbench/mcp-image/server.py`（`run.sh` 用 `/usr/bin/python3`） |
| 产物目录 | `/opt/sefs/opc-workbench/mcp-image/output/<file_key>/` |
| systemd | `mcp-image-http.service`（`sudo -n systemctl restart mcp-image-http.service`） |
| 日志 | `journalctl -u mcp-image-http`；stdio 上游日志 `/opt/sefs/opc-workbench/mcp-bridge/image.stderr.log` |
| 208 nginx | `/etc/nginx/snippets/conring-mcp.conf` 中 `location /conring/image/`（备份 `.bak-image-20261124`） |
| 静态资源 | bridge `--static-dir .../output --static-path /assets` → `/conring/image/assets/*` |

**关键实现约束**：`server.py` 在 import `opc_mcp` / `prototype` 之前必须把 `sys.stdout` 改指向 `sys.stderr`——这些模块 import 时会打 `[boot] ...` 日志，污染 stdout 会直接破坏 MCP 的 JSON-RPC 行协议（OPC MCP 9104 曾出现过 4 条这类解析报错）。

## 7. 验证记录

- 2026-11-24：stdio 握手 / tools/list / 能力查询 / `sd15` 真实出图（256²，steps=2，32s，134KB PNG）/ 本机与公网静态下载 / `image_file_link` / 公网 HTTPS MCP 调用，全部 PASS；`selfcheck` 10 PASS / 0 WARN。
- 2026-11-24：157 `qwen-image` 端到端实测（真实调用，非桩测）：
  - **文生图**：`prompt="一只橘猫坐在窗台上，阳光明媚，电影感，高清写实"`，768×768，
    `wait_sec=900` → `isError=false`、`mode=t2i`、耗时 **230.2s**、PNG 768×768 / 987,103 B；
    `job_id=af60415b-26c8-403f-8f9a-633f69a149b6`，
    `file_key=hJDy6AKcSsljD4L7eNDeKpAJ`。
  - **图生图**：`reference_image` 为服务端本地 768×768 PNG，`prompt="把这张图改成梵高油画风格，保留主体构图"`，
    768×768 → `isError=false`、`mode=i2i`、耗时 **230.4s**、PNG 768×768 / 1,566,984 B；
    `job_id=a68e062f-84c9-44f3-a8f9-07bc7cacf65b`，
    `file_key=7OQenjeqVZquiXTWCpbDGK9f`。
  - 两张产物均通过本机与公网 HTTPS 静态下载校验（HTTP 200，文件大小一致）。
  - 结论：157 的 Qwen-Image-2.1-Turbo 文生图/图生图链路完整可用；无 GPU 下 768² 稳定约 230s，
    客户端超时务必 ≥600s（本服务已自动把 qwen-image 的 `wait_sec` 抬到 ≥600）。
  - 未测百炼真实出图（避免消耗计费额度），仅确认后端可达与参数口径。
