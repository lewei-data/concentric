# Hermes 媒体网关改造方案：用 platform 适配器替代 ASR/TTS MCP Server

## 摘要

**结论：ASR/TTS 属于「传输层」能力，不应该做成 MCP 工具。** 正确做法是把 CosyVoice、whisper、Wav2Lip 收敛为一个常驻的 `media-engine` HTTP 服务（9200），Hermes 侧只保留两层薄集成：

1. **provider 插件**（`media-voice`）：向 Hermes 注册 STT/TTS provider，接入网关原生的「语音消息自动转写」与「语音回复」通道；
2. **platform 适配器插件**（`media`）：仿 Telegram/IRC 适配器，提供 `POST /media/v1/messages` 异步任务接口，入站音频→STT→Agent，出站按 `reply_mode=text|voice|video` 渲染文本/语音/数字人播报视频。

MCP 注册与 9101/9102 MCP 桥全部下线，能力由上述「HTTP 引擎 + 两个插件」等价替代。

---

## 一、现状调研（代码级）

### 1.1 当前部署

| 组件 | 位置 | 现状 | 说明 |
|---|---|---|---|
| Hermes Agent | `/opt/sefs/hermes/hermes-agent` | v0.22.7，网关多路复用 18 个 profile | 单进程 `hermes gateway` |
| 平台配置 | `/opt/sefs/hermes/config.yaml` | **没有任何 `platforms:` 配置**，网关未接任何消息平台 | 无 Telegram/飞书凭据 |
| STT | `config.yaml → stt` | 仅 `enabled: true`，未设 `provider` → 走内置 `local`（faster-whisper base，默认英文） | 中文识别效果差 |
| TTS | `config.yaml → tts` | `provider: edge`（云端） | 与自建 CosyVoice 并存，双份实现 |
| mcp-tts | `/opt/sefs/mcp-tts/server.py` | CosyVoice2/3 模型**在 MCP 进程内加载**，6 worker 池；由 `mcp-bridge` 以 9101 暴露为 HTTP MCP | 模型常驻在 MCP 子进程里 |
| mcp-asr | `/opt/sefs/mcp-asr/server.py` | whisper small + 后处理 + Wav2Lip 数字人 + HLS 直播 + `run_hermes` 反向派发；9102 桥 | 职责过载，含反向调用 Hermes |
| MCP 注册 | `config.yaml → mcp_servers.tts` | 只有 tts 挂在全局；profile 级只有 mail（保留） | — |
| 端口占用 | 9100/9101/9102 已监听；9200/9201 空闲 | — | 已验证 |

### 1.2 为什么 MCP 方案不合理（5 条硬理由）

1. **能力错位**：语音消息的转写是网关收消息时的固定步骤，不是「模型可以决定要不要调用」的工具。Hermes 本身就在 `gateway/run_inbound_transcription.py` 里按 `MessageType.VOICE` 自动转写，在 `gateway/run_voice.py` 里做语音回复——MCP 工具是绕开原生链路另起一套。
2. **工具 schema 常驻上下文**：Hermes 每轮 API 调用都会带上全部工具定义。把 `tts_synthesize / asr_transcribe` 做成工具，等于每一轮对话都为「可能用不到的能力」付费（token + 延迟）。
3. **进程模型冲突**：`mcp-tts` 的 `Engine` 在进程内 `load_model` 加载 CosyVoice（数 GB）。MCP stdio 子进程是按会话/按 profile 拉起的，多 profile 场景会重复加载；这正是当初必须再加一层 9101 桥的原因。
4. **Base64 音频污染历史**：现有 `tts_synthesize` 返回 wav base64。工具结果会进入会话历史，并在后续每一轮被重发，上下文随音频线性膨胀。
5. **长任务不匹配**：数字人视频是分钟级任务，MCP 同步调用会占住一次 turn；需要 job 队列 + 轮询，而不是 tool call。

> 反过来说，「数字人视频生成」是真正的 Agent 能力，但本方案也不需要把它塞进工具表：由适配器按请求的 `reply_mode` 在传输层触发，模型无感知、schema 零成本。

### 1.3 Hermes 可复用的原生机制（已确认存在）

- 平台适配器注册：`PluginContext.register_platform(PlatformEntry(...))`，`PlatformEntry` 支持 `trusted_inbound`、`platform_hint`、`allowed_users_env`、`allow_all_env`。
- 适配器基类：`gateway/platforms/base.py` 的 `BasePlatformAdapter`，需实现 `connect/disconnect/send`，可选覆盖 `send_voice/send_video/send_typing/get_chat_info`。
- 入站事件：`gateway/platforms/event.py` 的 `MessageEvent` + `MessageType.VOICE` + `SessionSource(profile=...)`；`MessageType.VOICE` 会命中自动转写。
- 媒体缓存：`gateway/platforms/media_cache.py` 的 `cache_media_bytes(...)`。
- Provider 注册：`ctx.register_transcription_provider(...)` / `ctx.register_tts_provider(...)`，基类 `CatalogProviderBase`、`TTSProvider`、`TranscriptionProvider`。
- 本地服务端：网关已依赖 `aiohttp`，参考 `gateway/platforms/webhook.py` 的 server 生命周期写法。
- 配置：`platforms.<name>` 块中非保留键会被自动提升进 `config.extra`（`PlatformConfig.from_dict`），适配器用 `config.extra.get(...)` 读取。

外部资料佐证 Hermes 网关的语音链路定位（语音备忘录自动转录、语音回复由网关投递）：
- [Telegram 设置（中文文档）](https://hermes-agent.nousresearch.com/docs/zh-Hans/user-guide/messaging/telegram)
- [消息网关总览](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)

---

## 二、目标架构

```
                 ┌──────────────────────────── 客户端 App / 小程序 / H5
                 │  POST /media/v1/messages (text | audio, reply_mode)
                 ▼
        ┌──────────────────────────┐        ┌───────────────────────────┐
        │ media 平台适配器 :9201    │        │  Hermes 网关主循环         │
        │ (插件 media, aiohttp)     │◄──────►│  会话 / Agent / 工具       │
        │  job 状态机 + 媒体文件服务 │        │  (18 profiles multiplex)   │
        └───────┬──────────▲───────┘        └───────────┬───────────────┘
                │          │                            │
   MessageEvent │          │ send()/send_voice/send_video│ stt.provider / tts.provider
   (VOICE)      │          │                            │
                ▼          │                            ▼
        ┌──────────────────┴───────────────────────────────────────────┐
        │ media-engine :9200  (CosyVoice venv, 单进程常驻)              │
        │  /v1/asr        whisper 转写                                  │
        │  /v1/tts        CosyVoice 合成（复用现有 Engine/Pool）         │
        │  /v1/video/jobs TTS → Wav2Lip → mp4（异步队列）                │
        │  /files/{name}  产物静态服务                                  │
        └───────────────────────────────────────────────────────────────┘
```

分层原则：
- **模型推理集中在 media-engine**，只加载一次，任何进程都不再 import torch/CosyVoice。
- **Hermes 进程只做协议转换与编排**，插件都是几十行的薄客户端。
- **MCP 完全退出链路**。

---

## 三、详细实施步骤

### Step 0：基线与回滚准备

1. 备份：`cp /opt/sefs/hermes/config.yaml /opt/sefs/hermes/config.yaml.bak-$(date +%F)`
2. 记录当前进程与端口：`ps -eo pid,cmd | grep -E "mcp-tts|mcp-asr|bridge.py"`、`ss -ltnp | grep -E "9101|9102"`。
3. 保留旧目录 `/opt/sefs/mcp-tts`、`/opt/sefs/mcp-asr` 不删除，仅停用（回滚窗口 7 天）。

### Step 1：新建 `media-engine` 常驻服务

目录：`/opt/sefs/media-engine/`

```
media-engine/
├── server.py            # aiohttp 路由 + 启动参数
├── tts_backend.py       # 从 mcp-tts/server.py 抽取 Engine / Pool / 文本规范化 / 分句 / srt
├── asr_backend.py       # 从 mcp-asr/server.py 抽取 whisper 转写 + 幻听过滤
├── video_backend.py     # 从 mcp-asr/server.py 抽取 intro_build 的 lip-sync 部分，
│                        #   改为调用 /opt/sefs/mcp-asr/lipsync/run_lipsync.py（Wav2Lip）
├── engine_client.py     # 供两个插件复制的薄客户端（≤60 行，故意不共享）
├── avatars/             # 数字人形象图（默认 boss.png，沿用现有 portraits）
├── work/                # 临时产物，按 job_id 分目录
└── run.sh               # exec /opt/sefs/CosyVoice/venv/bin/python server.py
```

**接口契约（v1）**

| 方法 | 路径 | 入参 | 出参 |
|---|---|---|---|
| GET | `/health` | — | `{ok, tts:{loaded,workers}, asr:{loaded,model}, video:{queued,running}}` |
| POST | `/v1/asr` | multipart `file`；form `language=zh`、`prompt`(可选) | `{text, language, duration_ms, segments:[{start_ms,end_ms,text}]}` |
| POST | `/v1/tts` | JSON `{text, voice, speed, format:"mp3"\|"wav"}` | 音频二进制；响应头 `X-Duration-Ms`、`X-Sample-Rate`；错误返回 JSON `{error}` |
| GET | `/v1/voices` | — | `{default, voices:[{id, label}]}` |
| POST | `/v1/video/jobs` | multipart `audio` 或 JSON `{audio_path}` + `avatar_id` + `subtitles:bool` | `202 {job_id}`；队列满 `429` |
| GET | `/v1/video/jobs/{id}` | — | `{status: queued\|running\|done\|failed, video_path, duration_ms, error}` |
| GET | `/files/{name}` | — | 静态文件（带路径穿越校验） |

**实现要点**

- `tts_backend` 直接复用现有 `mcp-tts/server.py` 的 `Engine`、`Pool`、`pre_normalize`、分句与 SRT 生成逻辑，避免重写推理代码；差异只在于入口从 MCP tool 换成 HTTP handler。
- `asr_backend` 复用 `mcp-asr/server.py` 中 whisper 调用与幻听过滤（空音频/静音/重复句判定），模型路径沿用现有配置。
- `video_backend` 只保留「音频 + 形象图 → 口型视频」：调用 `run_lipsync.py`，再用 ffmpeg 叠加底部字幕（可选）与统一编码（H.264 + AAC，yuv420p，faststart）。
- 并发与背压（工程默认值，非实测指标，上线前需按机器实测标定）：
  - TTS worker 数沿用现有池（当前 6）；
  - ASR 并发 1（模型懒加载，`MEDIA_ASR_LAZY=1`）；
  - 视频串行 1，队列上限 3，超出返回 429；
  - 单条视频音频上限 `MEDIA_VIDEO_MAX_SECONDS`（默认 60s）。
- 产物 TTL：`MEDIA_FILE_TTL_HOURS=24`，后台任务每 10 分钟清理 `work/`。
- 环境变量：`MEDIA_ENGINE_HOST=127.0.0.1`、`MEDIA_ENGINE_PORT=9200`、`MEDIA_TTS_VOICE=female-zh-real`、`MEDIA_AVATAR_DIR`、`MEDIA_WORK_DIR`、`MEDIA_ENGINE_TOKEN`（可选内网鉴权）。

**systemd（用户级）**：`~/.config/systemd/user/media-engine.service`，`ExecStart=/opt/sefs/media-engine/run.sh`，`Restart=always`，`After=network.target`，随后 `systemctl --user daemon-reload && systemctl --user enable --now media-engine`。

### Step 2：插件 `media-voice`（STT/TTS provider）

目录：`/opt/sefs/hermes/plugins/media-voice/`

```
media-voice/
├── plugin.yaml
├── __init__.py          # from .provider import register
├── provider.py          # CosyVoiceTTS(TTSProvider) + MediaWhisperSTT(TranscriptionProvider)
└── engine_client.py     # POST /v1/tts、/v1/asr，超时与重试（2 次，指数退避）
```

`plugin.yaml` 关键字段：`name: media-voice`、`label`、`description`、`requires_env: []`（不强制，缺失时 `is_available()` 返回 False 并降级）。

`provider.py` 需要实现：

- `CosyVoiceTTS(name="media_cosyvoice", ...)`
  - `is_available()`：探测 `/health`
  - `synthesize(text, voice, output_path, **kw)`：调 `/v1/tts` 落盘，返回时长/路径
  - `list_voices()`：GET `/v1/voices`
  - 语音/音色默认取 `MEDIA_TTS_VOICE`，请求可覆盖
- `MediaWhisperSTT(name="media_whisper", ...)`
  - `is_available()`：同上
  - `transcribe(audio_path, language="zh", prompt=None)`：调 `/v1/asr`，返回 Hermes 期望的转写结果结构（含 segments 时一并填充）

注册：`ctx.register_tts_provider(...)`、`ctx.register_transcription_provider(...)`。

### Step 3：插件 `media`（platform 适配器）

目录：`/opt/sefs/hermes/plugins/media/`

```
media/
├── plugin.yaml          # kind: platform
├── __init__.py          # from .adapter import register
├── adapter.py           # MediaAdapter(BasePlatformAdapter) + aiohttp app + job 状态机
└── engine_client.py     # 仅用于视频：POST /v1/video/jobs、GET /v1/video/jobs/{id}
```

**对外 API（明确契约）**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/media/v1/messages` | multipart：`session_id`(必填)、`text`、`audio`(文件，可选)、`reply_mode=text\|voice\|video`(默认 voice)、`avatar_id`、`voice`；JSON 也可（无音频时）。返回 `202 {job_id, session_id}` |
| GET | `/media/v1/jobs/{job_id}` | `{status: accepted\|running\|rendering\|done\|failed, text, messages:[{text, audio_url, video_url, duration_ms}], error}` |
| GET | `/media/v1/files/{name}` | 产物下载（音频 mp3 / 视频 mp4） |
| GET | `/health` | `{ok, upstream, jobs:{queued,running}}` |

鉴权：`Authorization: Bearer ${MEDIA_API_TOKEN}`（常量时间比较，缺失/错误 401）；默认只监听 `127.0.0.1`，跨机访问必须走反代 + TLS。入站请求带 `X-Agent-Profile: <profile>` 时映射到 `SessionSource.profile`，复用网关多 profile 路由（不存在则 400）。

**适配器实现要点**

```python
class MediaAdapter(BasePlatformAdapter):
    name = "media"
    platform = Platform("media")

    async def connect(self): ...      # 启动 aiohttp，_mark_connected()
    async def disconnect(self): ...   # runner.cleanup()，取消后台渲染任务
    async def send(self, chat_id, content, ...):   # 一轮回复的落点
    async def send_voice(self, chat_id, audio, ...)
    async def send_video(self, chat_id, video, ...)
    async def send_typing(self, chat_id): return True
    async def get_chat_info(self, chat_id): ...
```

- `connect()`：注册上表路由；`app["jobs"]` 内存字典（`job_id → Job`），并启动 TTL 清理协程。
- 入站（`POST /messages`）：
  1. 校验 token/session/reply_mode/size（音频 ≤ 25MB）；
  2. 有 `audio` → `cache_media_bytes(...)` 落盘，构造 `MessageEvent(text="", message_type=MessageType.VOICE, media_urls=[path], media_types=["audio/ogg"], source=SessionSource(platform=..., chat_id=session_id, user_id=session_id, profile=...), metadata={"job_id": ...})`；
  3. 纯文本 → `MessageType.TEXT`；
  4. `asyncio.create_task(self.handle_message(event))`，HTTP 立即返回 202（不阻塞请求）。
- 出站 `send()`：
  1. 按 `job.reply_mode` 分流：`text` 直接写入 job；`voice` 用 `get_tts_provider()` 合成 mp3，登记 `audio_url`；`video` 先 TTS，再把音频提交 `media-engine /v1/video/jobs`，**立即返回**并把 job 置 `rendering`，后台协程轮询至 `done` 后登记 `video_url`（避免占住会话锁）；
  2. 若本轮带 `MEDIA:/path` 附件，网关会调用 `send_voice/send_video`，实现里把文件复制进 job 目录并登记 URL；
  3. 视频模式对文本做长度约束（默认 80 字，超出截断并在 job 里标注），并设置 `platform_hint`：要求口语化短句、无 Markdown/URL/列表。
- `register()` 使用 `PlatformEntry(name="media", label="Media API", factory=..., trusted_inbound=True, platform_hint=..., allowed_users_env="MEDIA_ALLOWED_USERS", allow_all_env="MEDIA_ALLOW_ALL_USERS")`。
- 单 session 并发 2、全局限 8，超出 429；视频串行由引擎队列保证。

### Step 4：配置变更（`/opt/sefs/hermes/config.yaml`）

```yaml
stt:
  enabled: true
  provider: media_whisper          # 原为内置 local(base, 英文)
  language: zh
  echo_transcripts: true

tts:
  provider: media_cosyvoice        # 原为 edge
  voice: female-zh-real

plugins:
  enabled:                         # 用户插件需显式启用
    - media-voice
    - media

platforms:
  media:
    enabled: true
    host: 127.0.0.1
    port: 9201
    default_avatar: boss
    video_max_chars: 80
    file_ttl_hours: 24
    max_body_mb: 25
```

删除 `mcp_servers.tts`（Step 6）。密钥只放 `/opt/sefs/hermes/.env`：`MEDIA_API_TOKEN=...`、`MEDIA_ENGINE_URL=http://127.0.0.1:9200`。

### Step 5：启用与重启

```bash
systemctl --user enable --now media-engine
curl -s localhost:9200/health
hermes plugins list | grep -E "media-voice|media"
hermes gateway restart
hermes gateway status
```

**启动前置校验（关键）**：`platforms.media` 解析依赖插件先完成注册，否则 `Platform("media")` 抛 `ValueError` 被 `config.py:734` 静默跳过，表现为「配置了但适配器不启动」。校验命令：

```bash
/opt/sefs/hermes/hermes-agent/.venv/bin/python -c \
  "from gateway.config import Platform; print(repr(Platform('media')))"
```

若返回 `None`，兜底方案：把插件目录软链为内置平台
`ln -s /opt/sefs/hermes/plugins/media /opt/sefs/hermes/hermes-agent/plugins/platforms/media`
（`_scan_bundled_plugin_platforms` 支持该形态；升级 hermes-agent 后需重建软链）。

### Step 6：MCP 下线

1. 从 `config.yaml` 删除 `mcp_servers.tts`；
2. 停掉 9101（tts 桥）与 9102（asr 桥）对应的进程/服务，`mcp-bridge` 仅保留 9100（mail）不动；
3. 保留 `/opt/sefs/mcp-tts`、`/opt/sefs/mcp-asr` 源码目录与 venv（回滚用），但不再有 MCP 注册、不再被网关拉起；
4. 检查无残留：`grep -rn "mcp-tts\|9101" /opt/sefs/hermes/config.yaml /opt/sefs/hermes/profiles/*/config.yaml` 应无输出。

### Step 7：验证

| # | 验证项 | 命令/判定 |
|---|---|---|
| 1 | 引擎健康 | `curl -s localhost:9200/health` → `ok:true`，tts/asr/video 均非 error |
| 2 | TTS | `curl -s -X POST localhost:9200/v1/tts -H 'Content-Type: application/json' -d '{"text":"你好，这是语音合成测试","voice":"female-zh-real","format":"mp3"}' -o /tmp/t.mp3` → 文件可播放、`ffprobe` 有时长 |
| 3 | ASR | 用上一步 mp3：`curl -s -F file=@/tmp/t.mp3 -F language=zh localhost:9200/v1/asr` → 文本与原文一致（≥90% 字符匹配） |
| 4 | provider 生效 | 网关日志出现 `stt provider=media_whisper`、`tts provider=media_cosyvoice`；Agent 内 `text_to_speech` 可产出 mp3 |
| 5 | 文本链路 | `curl -s -X POST localhost:9201/media/v1/messages -H "Authorization: Bearer $MEDIA_API_TOKEN" -F session_id=t1 -F "text=帮我用一句话介绍你自己" -F reply_mode=text` → 轮询 job 到 `done`，`messages[0].text` 非空 |
| 6 | 语音链路 | 同 5，`reply_mode=voice` → job `done` 后 `audio_url` 可下载且内容对应回复 |
| 7 | 数字人视频 | 同 5，`reply_mode=video` → 状态经历 `rendering` → `done`，`video_url` 为可播放 mp4，口型与音频对齐 |
| 8 | 多 profile | 请求头 `X-Agent-Profile: <现有 profile>` → 回复风格/配置对应该 profile |
| 9 | 隔离性 | 未带 token → 401；超大文件 → 413；视频并发超限 → 429；`/files/../../etc/passwd` → 400 |
| 10 | 回归 | 其他 profile 的 mail MCP 仍可用；`hermes gateway status` 无 adapter 崩溃；内存占用不高于改造前（改造前两套进程已在跑） |
| 11 | MCP 已摘除 | 网关启动日志无 `mcp-tts`；`/opt/sefs/hermes/config.yaml` 无 `mcp_servers.tts` |

---

## 四、关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| ASR/TTS 接入方式 | Hermes provider 插件，非 MCP | 走原生自动转写/语音回复链路，零 schema 成本、零模型决策 |
| 模型宿主 | 独立常驻 HTTP 服务 | CosyVoice/torch 不能进网关进程；避免每会话重复加载 |
| 媒体入口 | 自建 `media` platform 适配器 | 用户选择；HTTP job 模型适配移动端/小程序，也便于加 Telegram/飞书而不改内核 |
| 视频能力 | 适配器按 `reply_mode` 触发，不做成工具 | 生成是传输层结果渲染，避免每轮工具表膨胀 |
| 视频执行 | 异步 job + 轮询 | 分钟级任务不能阻塞 turn/会话锁 |
| 语音渲染 | 适配器调用 TTS provider registry | 复用同一份 CosyVoice 配置，避免两套音色逻辑 |
| 数字人范围 | Wav2Lip 口型 + 可选字幕 | 复用现有 `run_lipsync.py`；直播 HLS 不在本期 |
| MCP 处置 | 全部摘除，源码保留 7 天 | 用户选择「全部下线」；保留回滚能力 |

## 五、风险与回滚

| 风险 | 影响 | 处置 |
|---|---|---|
| `Platform("media")` 解析早于插件注册 | `platforms.media` 被静默忽略，适配器不启动 | Step 5 前置校验；失败则软链为内置平台 |
| 用户插件未启用 | provider 不存在，STT 回退/失败 | `plugins.enabled` 显式列出，启动后 `hermes plugins list` 核对 |
| Wav2Lip CPU 耗时随文本线性增长 | 视频 job 超时 | 文本 80 字上限、音频 60s 上限、队列 3、超时 900s 后降级为语音+文本 |
| 单进程内存（TTS+ASR+视频同机） | OOM | ASR 懒加载、视频串行 1；上线前实测 RSS，超限则把 video 拆成独立进程 |
| 网关升级覆盖内置目录 | 软链兜底失效 | 升级后重跑 Step 5 校验 |
| 旧桥仍有调用方 | 9101/9102 停用后报错 | 上线前 `ss -tnp` + 日志确认无活跃调用方，再停 |

**回滚（≤5 分钟）**：停 `media` 插件与 `media-engine` → 恢复 `config.yaml.bak` → 重启 `mcp-tts`/`mcp-asr` 及 9101/9102 桥 → `hermes gateway restart`。

## 六、假设

1. 客户端可通过内网或反代访问 9201；本机默认仅监听 127.0.0.1。
2. 网关为单进程 multiplex（`gateway.multiplex_profiles: true`，已确认），不会出现多进程抢 9201 端口。
3. `/opt/sefs/CosyVoice/venv` 可同时承载 CosyVoice + whisper + Wav2Lip（现状即为同一 venv）。
4. Wav2Lip 模型与 `run_lipsync.py`、`avatars/` 形象图可直接复用。
5. 9200/9201 在本机空闲（已验证）。
6. 所有并发/时长/上限为工程默认值，不是实测数据，需按目标机器标定后再固化。

## 七、非目标（本期不做）

- HLS 直播（`live_call`）、Remotion 模板化口播视频（现有独立工程，后续可作为 `renderer=remotion` 接入）。
- 视频理解（入站视频抽帧/`video_analyze`）——Hermes 已有该工具，不在本方案范围。
- Telegram / 飞书接入：适配器抽象完成后可零改动内核直接加平台。
- 多租户计费、配额、审计。

---

## 附：参考资料

- Hermes 官方文档 · Telegram（语音自动转录说明）：https://hermes-agent.nousresearch.com/docs/zh-Hans/user-guide/messaging/telegram
- Hermes 官方文档 · 消息网关：https://hermes-agent.nousresearch.com/docs/user-guide/messaging/
- 本地源码：`gateway/platforms/base.py`、`gateway/platforms/event.py`、`gateway/platform_registry.py`、`gateway/run_inbound_transcription.py`、`gateway/run_voice.py`、`plugins/platforms/irc/adapter.py`、`agent/tts_provider.py`、`agent/transcription_provider.py`
