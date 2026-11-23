# 独立渲染出片 MCP（MCP-RENDER）客户端用法

> 权威口径，核实于 2026-11-24。服务名 `mcp-render`，stdio 上游 + mcp-bridge 包装成 Streamable HTTP。
> 用途：**外部 agent 提交图片与旁白，本机直接渲染出竖屏 MP4**。

## 1. 与现有视频入口的区别

| 入口 | 做什么 | 耗时 | 适合 |
|---|---|---|---|
| 本服务 `mcp-render`（9107） | 图片 + 旁白 → Remotion 直接渲染成片 | 单作业数分钟到十余分钟 | agent 已有素材，只要「渲染出片」 |
| `POST /workbench/api/video/jobs` | 选题 → 文案 → 分镜 → 审批 → 渲染的完整内容流水线 | 20–45 分钟 | agent 只给一句需求，要工作台全流程代做 |

两者产物与作业 ID **不互通**。完整流水线见 `REMOTE_VIDEO_JOB_API.md`；本服务的 `job_id` 同时是下载用的 `file_key`。

## 2. 接入与鉴权

```json
{
  "mcpServers": {
    "render": {
      "type": "streamable-http",
      "url": "https://ai.boraydata.com/conring/render/mcp",
      "headers": { "X-Mail-Agent": "<OPC 个人 API key>" }
    }
  }
}
```

- 公网：`https://ai.boraydata.com/conring/render/mcp`
- 内网：`http://192.168.21.206:9107/mcp`
- 鉴权：与 OPC / image MCP 共用同一把个人 API key，`X-Mail-Agent` 由网关注入 `client_key`。

## 3. 工具清单（4 个）

### 3.1 `render_capabilities`

只读能力查询，入参 `{}`。返回默认规格（720×1280@24）、场景类型（`scroll_image_scene` / `fit_image_scene`）、
上限（图片数、单图大小、总时长）、下载口径与「与完整流水线的区别」。提交前先查。

### 3.2 `render_submit`（异步）

| 参数 | 类型 | 说明 |
|---|---|---|
| `shots` | array | 必填，非空。每项 `{image, line, duration}` |
| `shots[].image` | string | 图片：`data:image/...;base64`、`http(s)://` URL、或服务端可见的绝对路径 |
| `shots[].line` | string | 该镜旁白文字；空则该镜静音（仍会出画面） |
| `shots[].duration` | number | 该镜秒数，默认 6 |
| `spec` | object | 可选 `{width, height, fps}`，默认 720×1280@**30**；`fps` 会被强制为 30（composer 的 `calculateMetadata` 按 30 计算帧数，传 24 会多出 2.75s 导致时长验收失败） |
| `theme` | object | 可选 `{color}`，主题主色 |
| `brief` | string | 可选需求描述 |

**返回**：`{"job_id":"rj...","status":"queued","shots":N,"poll_after_sec":10}`。

### 3.3 `render_status`

`{"job_id":"rj..."}` → 状态 `queued` / `running` / `succeeded` / `failed`。
成功时带 `download_url`、`duration`、`size`、`elapsed_sec`、`check`（验收门明细）；失败时带 `error`。

### 3.4 `render_file_link`

`{"file_key":"rj..."}`（即 `job_id`）→ 重取成片下载链接，仅同 key 可见。

## 4. 调用示例

```python
import json, time, urllib.request

KEY = "<OPC 个人 API key>"
URL = "https://ai.boraydata.com/conring/render/mcp"

def post(body, sid=None):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream", "X-Mail-Agent": KEY}
    if sid: h["mcp-session-id"] = sid
    r = urllib.request.urlopen(urllib.request.Request(
        URL, data=json.dumps(body).encode(), headers=h), timeout=120)
    raw = r.read().decode()
    if raw.startswith("data:"):
        raw = "\n".join(l[5:] for l in raw.splitlines() if l.startswith("data:"))
    return (json.loads(raw) if raw.strip() else None), (r.headers.get("mcp-session-id") or sid)

def call(sid, rid, name, args):
    r, sid = post({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                   "params": {"name": name, "arguments": args}}, sid)
    return json.loads(r["result"]["content"][0]["text"]), sid

init, sid = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "my-agent", "version": "1"}}})
post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)

sub, sid = call(sid, 2, "render_submit", {
    "spec": {"width": 720, "height": 1280, "fps": "24/1"},
    "shots": [
        {"image": "data:image/png;base64,...", "line": "第一句旁白", "duration": 6},
        {"image": "https://example.com/shot2.png", "line": "第二句旁白", "duration": 6},
    ]})
job = sub["job_id"]

while True:
    st, sid = call(sid, 3, "render_status", {"job_id": job})
    if st["status"] in ("succeeded", "failed"):
        break
    time.sleep(15)

if st["status"] == "succeeded":
    print(st["download_url"])
else:
    raise RuntimeError(st["error"])
```

## 5. 渲染管线与产物

- 图片按 `shots` 顺序成镜；宽高比 >1.4 走 `scroll_image_scene`（长图滚动），否则 `fit_image_scene`（contain + 呼吸缩放）。
- 旁白走 MCP-TTS（CosyVoice2，默认 f02 女声 / 语速 1.15），逐镜合成后拼成整条音轨。
- 字幕按旁白字数比生成，底部留水印安全区（`CaptionOverlay` 的 `paddingBottom` 为画布高度 22%）。
- Remotion 渲染 H.264 MP4，随后过 `OmRender.validate` 验收门（分辨率、音轨、音量、时长、画面动态、色彩丰富度）；不通过则 `status=failed` 并给出原因。
- 产物落独立目录 `mcp-render/output/<job_id>/video.mp4`（隔离的 vault，不污染生产交付物）。

下载：

```bash
curl -L "https://ai.boraydata.com/conring/render/assets/<job_id>/video.mp4" -o out.mp4
```

静态链接无需 key，`file_key` 为 20 位随机串不可枚举；支持 Range 拖动播放。

## 6. 限制与红线

- **单 worker 串行**：本机无 GPU，Remotion 与 TTS 都吃 CPU；作业按提交顺序串行渲染，请勿一次提交几十个。
- 单图 ≤20MB、最多 30 张、总时长 ≤300s（可用环境变量覆盖）。
- **总时长建议 ≥12s**：验收门每 3 秒抽一帧比对画面动态，短片不足 4 帧会被判「抽帧失败」。
  实测 1 镜 6s 失败、3 镜 18s 可过。
- **fps 固定 30**，见上表；composer 的 `calculateMetadata` 硬编码 30，其它帧率会产生时长偏差。
- 旁白为空的镜头是静音画面；不会自动配 BGM。
- 不做选题、文案润色、审批；这些内容流水线能力在 `/workbench/api/video/jobs`。
- 与 `image` MCP 一样，产物 `file_key` 各自独立，不可跨服务重取。

## 7. 运维信息

| 项 | 值 |
|---|---|
| stdio server | `/opt/sefs/opc-workbench/mcp-render/server.py`（`run.sh` 用 `/usr/bin/python3`） |
| 作业与产物 | `mcp-render/output/index.json`、`mcp-render/output/<job_id>/video.mp4` |
| 输入暂存 | `mcp-render/inputs/<job_id>/` |
| 隔离 vault | `mcp-render/vault/`（启动前设 `OPC_VAULT`，不写生产交付物） |
| systemd | `mcp-render-http.service`（`sudo -n systemctl restart mcp-render-http.service`） |
| 日志 | `journalctl -u mcp-render-http`；上游 `mcp-bridge/render.stderr.log` |
| 208 nginx | `/etc/nginx/snippets/conring-mcp.conf` 的 `location /conring/render/`（备份 `.bak-render-20261124`） |

**关键实现约束**：`server.py` 必须在 import `deliverables` / `om_render` 之前设置 `OPC_VAULT` 并把
`sys.stdout` 改指向 `sys.stderr`；前者隔离产物路径，后者防止 boot 日志污染 JSON-RPC 行协议。

## 8. 验证记录

- 2026-11-24：stdio 握手 / tools/list / 能力查询 / 异步提交 / 状态轮询 / 产物下载 —— 见下方补录。

- 2026-11-24：端到端实测（真实渲染，非桩测）：
  - 握手 / tools/list / `render_capabilities` / 异步提交 / 状态轮询 / `render_file_link` 全 PASS；
    公网 HTTPS 入口握手 200（`serverInfo.name=render`）。
  - 3 镜 × 6s（共 18s），720×1280@30，三张不同来源图片 + 中文旁白 → `status=succeeded`，
    耗时 **147.6s**（TTS 25.7s + Remotion 146.9s 串行段），产物 **2,451,278 B**，
    `job_id=rjc5e290dd10f874ad`。MP4 实测 h264/aac、720×1280、19.05s（含 1s 尾部淡出 padding）。
  - 本机与公网静态下载均 HTTP 200、`video/mp4`、大小一致；`render_file_link` 重取链接正确。
  - `selfcheck` 10 PASS / 0 WARN。
- 踩坑记录（已修，勿重复踩）：
  1. 传 `fps=24` 时成片 8.75s（分镜 6s），因 composer 的 `calculateMetadata` 硬编码
     `Math.ceil((lastEnd+1)*30)` 计算帧数 → 服务端已强制 fps=30。
  2. 6s 短片被验收门判「抽帧失败」（每 3 秒抽一帧，不足 4 帧）→ 总时长建议 ≥12s。
  3. 真实照片被判「色彩层次不足」（未量化的 `getcolors` 每项计数恒为 1，实测 colorCount=0）
     → 已修 `om_render.validate()`：先量化到 32 色再统计（照片 31~32、SVG 场景 17~25、纯文字卡仍 2~3，
     阈值 8 保持有效）；备份 `om_render.py.bak-colorcount-20261124`。
