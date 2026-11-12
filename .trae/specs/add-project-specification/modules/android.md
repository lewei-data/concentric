# 模块规格 · android（语音 MCP 客户端）

> 上级文档：[spec.md](../spec.md)。本文件只描述 `android/`。
> 规模：`app/src/main/java/com/boray/voicemcp/` 52 个 Java 文件、16,272 行；`res/layout/` 16 个布局（`find` + `wc -l`）。

## 1. 模块定位

Android App 是老板侧的**随身语音入口**：通过 MCP Streamable HTTP 调用员工能力，通过 dispatch 调度中心 + WebSocket 建立 P0 实时通道，通过同源 HLS 播放数字人自我介绍与实时通话（`DispatchApi.java:12-32`、`StreamContract.java:8-30`）。

- 包名 `com.boray.voicemcp`；版本 `0.11.3`（versionCode 40），版本号唯一来源为 `app/build.gradle:11-13`。
- **零第三方依赖**：仅 Android framework + `org.json`（`app/build.gradle:83-85`），WS、MCP、HLS 解析均自实现。
- 连接参数全部持久化在 `SharedPreferences("voice_mcp_config")`，设置页可改，无需重编译（`McpConfig.java:9-20, 33-84`）。

## 2. 构建与产物

| 项 | 值 | 出处 |
| --- | --- | --- |
| compileSdk / targetSdk | 35 / 35 | `app/build.gradle:29,34` |
| minSdk | 26（Android 8.0） | `app/build.gradle:33` |
| Java | 17（source/target） | `app/build.gradle:77-80` |
| applicationId | `com.boray.voicemcp` | `app/build.gradle:32` |
| debug/release | 共用同一 applicationId，可直接覆盖安装 | `app/build.gradle:58-67` |
| release 签名 | 有 `keystore.properties` 用正式证书，否则回退 debug keystore | `app/build.gradle:19-25, 42-56` |
| APK 命名 | `voice-mcp-v0.11.3-b40-{debug\|release}.apk` | `app/build.gradle:13, 69-75` |
| 版本一致性 | `verifyVersionSync` 校验 `McpConfig.CLIENT_VERSION == appVersionName` | `app/build.gradle:87-103` |
| 一键出包 | `collectApks`：先校验版本，再构建 debug+release，必须正好收集 2 个 APK | `app/build.gradle:105-130` |

## 3. 代码结构与职责

| 分组 | 文件 | 职责 |
| --- | --- | --- |
| 连接配置 | `McpConfig`、`AssetUrls` | 端点/超时/带宽/模式持久化；同源素材地址解析 |
| MCP 协议 | `McpClient`、`McpResult`、`ResultParser` | Streamable HTTP 初始化、`tools/call`、结果解析 |
| 实时通道 | `DispatchApi`、`WsClient`、`RealtimeChannel`、`SseListener` | dispatch 提交/查询/取消；RFC 6455 WS；SSE 兜底 |
| 实时通话 | `LiveCallActivity`、`VoiceCallService`、`LiveMic`、`StreamPlayback`、`CallTone`、`CallReply`、`LipSync`、`LiveLevelMeter` | 1s 分片 HLS 直播、上行音频、电平同步 |
| 自我介绍 | `IntroPlayerActivity`、`CoverVideoView`、`DigitalHumanView` | MP4/HLS VOD 边下边播 |
| 语音采集/播放 | `WavRecorder`、`WavStream`、`VoicePlayer`、`PcmPlayer`、`PreviewAudio`、`VoiceLevelView` | 录音、PCM 流式播放、TTS 播报 |
| 会话与任务 | `SessionStore`、`SessionPanel`、`VoiceOutbox`、`Briefing`、`ListenService`、`PushProcessor`、`SpokenTextSync` | 会话状态、待播队列、后台监听、通知处理 |
| 档案与历史 | `Employee`、`EmployeeStore`、`HistoryDb`、`ResultStore` | 18 人离线档案、缓存、历史记录 |
| UI | `MainActivity`、`SettingsActivity`、`EmployeeListActivity`、`EmployeeDetailActivity`、`ProgressActivity`、`TodoActivity`、`HistoryActivity`、`HistoryDetailActivity`、`ReportActivity`、`VoiceCallActivity`、`ScreenOn`、`WindowInsetsHelper` | 11 个 Activity 与通用视图 |
| 系统集成 | `BootReceiver`、`ResultFileProvider` | 开机自启、结果文件共享 |

## 4. 对外契约

### 4.1 MCP 端点

| 常量 | 值 | 出处 |
| --- | --- | --- |
| `DEFAULT_URL` | `https://ai.boraydata.com/conring/asr/mcp` | `McpConfig.java:25` |
| `TTS_URL` | `https://ai.boraydata.com/conring/tts/mcp` | `McpConfig.java:26` |
| `MAIL_URL` | `https://ai.boraydata.com/conring/mail/ceo/mcp` | `McpConfig.java:27` |
| `LEGACY_URL` | `http://192.168.31.208/conring/asr/mcp` | `McpConfig.java:28` |
| 默认工具 | `voice_chat` | `McpConfig.java:29` |
| 音频字段 | `audio_base64` | `McpConfig.java:30` |
| 协议版本 | `2025-03-26` | `McpConfig.java:31` |
| 客户端版本 | `0.11.3` | `McpConfig.java:23` |

### 4.2 工具清单（`StreamContract.java:34-40`）

| 工具 | 用途 |
| --- | --- |
| `live_call` | 实时通话 `start/say/stop/status`（`StreamContract.java:42-45`） |
| `intro_video` | 按需生成/缓存自我介绍视频 |
| `asr_transcribe` | 语音转写 |
| `voice_chat` | 一轮语音对话（可带 `live_session` 混流） |
| `profile` | 员工档案与音色 |
| `voice_job` | 异步语音任务状态 |

### 4.3 调度中心（dispatch）

| 项 | 约定 | 出处 |
| --- | --- | --- |
| 公网 | `https://ai.boraydata.com/conring/dispatch` | `DispatchApi.java:38` |
| 内网 HTTP | `http://192.168.31.206:9120` | `DispatchApi.java:40` |
| 内网 WS | `ws://192.168.31.206:9121` | `DispatchApi.java:41` |
| 线路策略 | 公网优先，失败切内网，并记住上次成功线路 | `DispatchApi.java:12-24, 41-44` |
| 实时通道 | 必须显式 `channel:"ws"`，否则只回 passthrough | `DispatchApi.java:22-27` |
| P0 lane | `source:"boss"` + `modality:"voice"` 才进立即响应 lane | `DispatchApi.java:28-29` |
| 响应 | `task_id/channel_id/channel_kind/realtime_url/lane/priority` | `DispatchApi.java:30-34, 66-80` |

### 4.4 WebSocket（自实现）

- RFC 6455 子集：文本帧、二进制帧、ping/pong、close；客户端帧强制掩码，服务端分片自动重组（`WsClient.java:22-31`）。
- `wss` 使用系统信任库并启用 HTTPS 主机名校验（`WsClient.java:26-27`）。
- 线程模型：连接阻塞在建连线程，读线程回调 `Listener`；发送端任意线程、内部串行化（`WsClient.java:29-31, 57`）。

### 4.5 流地址安全（硬约束）

- 流地址只能来自服务端 `profile` / `live_call` 返回，且必须与 MCP 端点**同源**（scheme + host + 有效端口），否则返回空串并降级（`StreamContract.java:28-30, 92-118`）。
- 非同源时 `parseCall` 会把原因写入 `message`：「服务端返回的流地址不是 MCP 同源地址，已拒绝」（`StreamContract.java:316`）。
- HLS 起播从 live edge 回退至少 500ms，避免从第 0 片追赶（`StreamContract.java:187-192`）。

## 5. 配置项（`McpConfig.java` 全量字段）

| 字段 | 默认 | 说明 | 行 |
| --- | --- | --- | ---: |
| `url` | 公网 ASR MCP | MCP 端点 | 25, 35, 89 |
| `tool` | `voice_chat` | 默认工具 | 29, 36, 90 |
| `audioKey` | `audio_base64` | 上行音频字段名 | 30, 37, 91 |
| `language` / `model` | `zh` / `small` | ASR 参数 | 38-39, 92-93 |
| `voice` / `profile` | 空 | 留空＝服务端按员工档案自动选 | 40-43, 94-95 |
| `token` | 空 | Bearer Token | 44, 96 |
| `timeoutSec` | 600 | 实际钳制到 10–900s | 46, 175-177 |
| `autoPlay` / `voiceEnabled` | true / true | 自动播报、语音总开关 | 48, 53 |
| `reportEnabled` / `reportVideo` | true / true | 汇报播报、视频汇报 | 54-55 |
| `asyncMode` / `keepListening` | true / true | 异步任务、后台监听 | 59-60 |
| `listenSse` / `listenMail` | true / true | SSE / 邮件监听 | 61-62 |
| `listenIntervalSec` | 20 | 钳制到 5–60s | 63, 171-173 |
| `avatarId` | 空 | 留空＝当前汇报人 | 68 |
| `mode` | `auto` | 仅 `auto/voice/avatar/video`，非法回退 `auto` | 70, 180-184 |
| `bandwidthKbps` | 0 | 0＝自动测链路，测不到按 2000kbps | 72, 186-203 |
| `delegate` | true | 允许语音点名派单 | 74 |
| `autoSyncProfile` | true | 启动拉取 18 人档案，缓存 6 小时 | 75-76 |
| `dispatchUrl` / `dispatchUrlLan` / `dispatchWsLan` | 见 §4.3 | 三地址可覆盖 | 80-84 |

## 6. 离线与降级

| 场景 | 行为 | 出处 |
| --- | --- | --- |
| 无网络/服务不可达 | 使用 `assets/employees.json` 离线档案（`count=18`、`version=3`、素材基址 `/conring/asr/assets`） | `EmployeeStore`、`assets/employees.json` |
| 公网 dispatch 失败 | 切内网 9120，并记住线路 | `DispatchApi.java:12-16` |
| WS 不可用 | 回退 SSE / 轮询（`listenSse`、`listenIntervalSec`） | `McpConfig.java:61-63` |
| 带宽不足 | `mode=auto` 且实测 <600kbps 时降级纯语音 | `McpConfig.java:71` |
| 流地址非同源 | 拒绝播放并给出提示，不回退到外链 | `StreamContract.java:92-99, 316` |
| 服务端 live 已结束 | `live.ended/closed/status=ended` → 客户端自行播回复 | `StreamContract.java:217-229` |
| 服务端未返回首个分片 | `hasSegment()` 判空后再起播 | `StreamContract.java:137-146` |

## 7. 权限与系统组件（`AndroidManifest.xml`）

| 项 | 值 |
| --- | --- |
| 权限 | `INTERNET`、`ACCESS_NETWORK_STATE`、`RECORD_AUDIO`、`MODIFY_AUDIO_SETTINGS`、`FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_DATA_SYNC`、`FOREGROUND_SERVICE_MICROPHONE`、`POST_NOTIFICATIONS`、`RECEIVE_BOOT_COMPLETED` |
| `usesCleartextTraffic` | `true`（内网 HTTP/WS 需要） |
| `VoiceCallService` | `foregroundServiceType="microphone|dataSync"`，`exported=false` |
| `ListenService` | `foregroundServiceType="dataSync"`，`exported=false` |
| `BootReceiver` | 开机恢复监听，`exported=false` |
| `ResultFileProvider` | 结果文件共享，`exported=false` |
| 唯一 exported Activity | `MainActivity`（LAUNCHER） |

## 8. 已知限制

1. **未真机验证**：本次仅做静态核对，未在设备上跑通通话、HLS 与后台监听全链路。
2. release 在缺少 `keystore.properties` 时使用 Android debug 证书，只能内部自测，不能上架（`app/build.gradle:15-18, 49-54`）。
3. debug 与 release 共用 applicationId，两个包不能并存，覆盖安装会替换数据。
4. 明文流量全局放开（`usesCleartextTraffic=true`），生产环境依赖 `network_security_config` 收紧，未在本模块核对具体域名白名单。
5. WS、MCP、HLS 解析均为自实现，协议边界（分片、重连、异常帧）缺乏自动化测试，仅有 `tools/` 下的探针脚本。
6. `dispatchUrlLan`/`dispatchWsLan` 默认指向 `192.168.31.206`，离开该局域网时会先等一次超时才回落公网（`DispatchApi.java:12-16`）。

## 9. 验收手段

- 版本：`cd android && ./gradlew verifyVersionSync`，应输出 `VERSION SYNC OK: 0.11.3`。
- 出包：`./gradlew collectApks`，`dist/` 必须恰好 2 个 APK。
- 静态契约：`java -cp tools/lib/json-20240303.jar:... tools/AskProbe`（探针脚本，需网络）。
- 安装后：设置页确认 `url` 指向 `/conring/asr/mcp`，首页应展示 18 人档案与头像；断网后应回退离线档案。
