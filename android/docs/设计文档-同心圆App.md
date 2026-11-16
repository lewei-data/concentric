---
kb_id: kb_android_app_design
title: 同心圆 Android App 设计文档 — 架构、模块、链路与接口契约
project: 全公司
tags: 设计文档、Android、APP、架构、语音模式、实时通话、接口契约、MCP、构建发布
trace:
archived: 2026-11-16T12:00:00+08:00
---

同心圆 Android App（`com.boray.voicemcp`，桌面名「同心圆」）是老板在手机上使用数字员工的主入口：按住说话下发指令、实时语音通话、看数字员工汇报、审批待办、回看历史。本文是工程侧设计文档，对应代码在 `android/`（`/opt/sefs/opc-workbench/android`），版本基线 0.13.3 / b47。

## 1. 定位与设计取向

| 项 | 决策 | 理由 |
|---|---|---|
| 目标用户 | 老板本人（单账号、单机） | 不做多租户切换，登录即绑定手机号 |
| 核心动作 | **按住说话** 一个手势完成「说—识别—确认—发送」 | 比点击录音/再点发送少两次操作，参考飞书 |
| 技术栈 | Java + 纯 Android framework，**零第三方依赖**（无 AndroidX / OkHttp / Kotlin / 图片库） | APK < 500KB，无供应链风险，离线环境可直接编译 |
| 网络 | 公网 HTTPS 优先，失败切内网（dispatch） | 老板在外网也能用；内网时走直连端口延迟更低 |
| 语音 | 16kHz / 16bit / 单声道 WAV 上传，服务端 ASR + TTS | 不依赖手机编码器，兼容所有机型 |
| 状态呈现 | **默认状态零动画**，状态一律用一行文字说 | 环形转圈既耗电又容易被误读成卡死（0.13.1 起彻底移除） |

工程规模：54 个 Java 文件 / 约 1.8 万行 / 17 个布局；`minSdk 26`、`targetSdk 35`、`compileSdk 35`。

## 2. 分层架构

```
┌───────────────────────────── UI 层（Activity + XML） ─────────────────────────────┐
│ LoginActivity  MainActivity  EmployeeList/Detail  Progress  Todo  History/Detail   │
│ ReportActivity  IntroPlayer  LiveCall  VoiceCall  Settings  （SessionPanel 弹层）  │
└───────────────┬───────────────────────────────────────────────────────────────────┘
                │ 只调用业务对象，不直接拼 HTTP
┌───────────────▼────────────── 业务/状态层 ───────────────────────────────────────┐
│ 语音模式状态机（MainActivity.renderTalk）  通话状态（VoiceCallService 静态量）      │
│ EmployeeStore  Briefing  HistoryDb  SessionStore  ResultStore  VoiceOutbox         │
│ ResultParser  CallReply  LipSync  StreamContract                                  │
└───────────────┬───────────────────────────────────────────────────────────────────┘
                │
┌───────────────▼────────────── 通道层（自研最小客户端） ───────────────────────────┐
│ McpClient(Streamable HTTP)  SseListener  ChatApi(workbench REST)  DispatchApi      │
│ WsClient(RFC6455)  RealtimeChannel  WavStream  PcmPlayer  WavRecorder  VoicePlayer │
└───────────────┬───────────────────────────────────────────────────────────────────┘
                │
        [同心圆服务端] ai.boraydata.com/conring/*   +   /workbench
```

约束：通道层全部为阻塞式自研实现，调用方必须放子线程（`executor` / `ListenService` 线程池 / `VoiceCallService` 独立线程），主线程只做 `ui.post` 渲染。

## 3. 模块清单

| 分组 | 类 | 职责要点 |
|---|---|---|
| 入口 | `LoginActivity`、`AuthSession`、`PhoneNumbers` | 手机号+密码登录，`POST /api/login`，Cookie 存 `McpConfig.sessionCookie`；本机号码只用于自动填账号 |
| 首页 | `MainActivity` | 按住说话状态机、通话图标、员工卡、会话摘要、结果与附件 |
| 首页卡片 | `SessionPanel`、`SessionTeaser`(内嵌) | 按「人 + 天」的会话流水，可拖高度、翻到顶加载前一天 |
| 员工 | `EmployeeListActivity`、`EmployeeDetailActivity`、`Employee`、`EmployeeStore` | 档案三级数据源：用户自定义 > 服务端缓存 > assets 内置；`AssetUrls` 只解析服务端返回的同源地址 |
| 汇报 | `ReportActivity`、`ProgressActivity`、`TodoActivity`、`Briefing` | 汇报动画渲染；小秘书简报；待办审批（决定回传服务端执行） |
| 介绍片 | `IntroPlayerActivity`、`LiveCallActivity`、`StreamPlayback`、`CoverVideoView` | 自我介绍 MP4（音画合一）、实时 HLS Live，center-crop 铺满 |
| 实时通话 | `VoiceCallService`、`VoiceCallActivity`、`RealtimeChannel`、`WsClient`、`LiveMic`、`PcmPlayer`、`CallTone`、`CallReply`、`WavStream`、`SpokenTextSync`、`LiveLevelMeter` | 后台前台服务承载通话；WS 双向；VAD 自动断句；边收边播；本地合成拨号音 |
| 语音模式 | `MainActivity`(TALK_*)、`ChatApi`、`WavRecorder`、`VoiceLevelView` | 按住录音 → `/api/asr` → 确认 → `/api/chat/*` → 轮询回复 → TTS 播报 |
| 后台 | `ListenService`、`BootReceiver`、`PushProcessor` | 三条消息来源 + 统一出口（写历史→播报→通知→唤起汇报） |
| 存储 | `HistoryDb`、`SessionStore`、`ResultStore`、`VoiceOutbox` | SQLite 三库 + 目录约定；待发指令持久化保证不丢 |
| 音频/渲染 | `WavRecorder`、`VoicePlayer`、`PreviewAudio`、`LipSync`、`DigitalHumanView`、`VoiceLevelView` | 录音/播报/口型包络/Canvas 仿真人；无素材时也能有表情口型 |
| 基础设施 | `McpClient`、`SseListener`、`DispatchApi`、`McpConfig`、`McpResult`、`ResultParser`、`ResultFileProvider`、`ScreenOn`、`WindowInsetsHelper` | 协议、配置、文件外发、安全区适配 |

## 4. 关键链路

### 4.1 语音模式（首页大圆钮 · 按住说话）

```
ACTION_DOWN  按钮 scale→0.86 / 光晕呼吸 / 实时电平 / 计时
   按住录音 16k WAV（≤60s，<400ms 或 <2KB 提示「多说一会」）
   上滑 >72dp → 松开取消
ACTION_UP    停录 → POST /api/asr?ext=wav&source=app（只识别，不建线程）
   ↓ 识别成功
PANEL        按钮上方展示识别文本 + 三个按钮：重录 / 发语音 / 发送文字
   发送文字 → POST /api/chat/send        {source:app, text}
   发语音   → POST /api/chat/voice?text=  （音频留档，不再二次 ASR）
   ↓
SENDING → WAITING（1.5s 轮询 GET /api/chat/messages?thread=app:secretary，120s 超时）
   ↓ 取到 outbound 且非 ack 的回复
下载 meta.audio 播放；下载失败回落系统 TTS。等待期间可继续按住说下一句（旧轮询作废）。
```

状态机：`TALK_IDLE → TALK_RECORDING → TALK_ASR → TALK_PANEL → TALK_SENDING → TALK_WAITING → TALK_IDLE`。`onPause` 时若在录音则丢弃（不占麦克风），`onDestroy` 使轮询 `replyGen++` 失效。

与 Web 桌面精灵**同一条服务端流水线**：消息落 `chat_messages`，线程 `app:secretary` 与 `web:secretary` 同结构，同一个小秘书 agent、同一套 TTS、同一个 SSE `chat.message`，因此手机和网页看到的是同一段对话。

### 4.2 实时语音通话（顶部通话图标）

```
toggleCall → VoiceCallService.start()（前台服务 microphone|dataSync）
  → 状态「正在接通…」（静态文字，20s 未接通自动停止）
  → WS 握手成功 → connected=true → 首页 hero 收紧为「时长 · 电平 · 挂断」条
  上行：LiveMic 常开 + 能量 VAD（预录 300ms、停顿 700ms 断句、单句 ≤12s）
  下行：二进制帧当作连续 WAV 流，PcmPlayer 边收边播；CallReply 拆「语音稿 / 文字详情」
  打断：检测到用户开口即丢队列（barge_in）
挂断：自定义 ACTION_STOP → endCall → 等未播完回复（兜底 5min）→ stopSelf
```

与语音模式互斥：通话中按住大圆钮只会提示「先挂断」（未接通则等同取消呼叫）。

### 4.3 后台消息（保持监听）

`ListenService` 前台服务（通知「同心圆 · 保持监听」）三路并行：

1. `voice_chat(wait=false)` 返回 job_id → `voice_job` 长轮询取结果并播报；
2. SSE `GET /conring/asr/mcp`（`text/event-stream`）长连接，收到推送再拉完整结果；
3. 定时 `mail_list` 比对最新 uid，有新邮件就播报摘要（不改已读）。

结果统一进 `PushProcessor`：写历史 → 播报 → 系统通知 → App 在前台则唤起数字员工汇报画面。`BootReceiver` 在开机/覆盖安装后按开关自动恢复。`VoiceOutbox` 的待发指令由该服务线程池重试。

## 5. 数据与存储

| 位置 | 内容 | 生命周期 |
|---|---|---|
| `filesDir/history.db` | 历史消息一张表：time/kind/message_id/job_id/status/路径 | 永久，`ALTER TABLE` 增量升级 |
| `filesDir/session.db` | `sessions`（谁、哪天、起止、链路）+ `messages`（角色/类型/文本/附件） | 永久，按人+天组织 |
| `filesDir/voice_outbox/` | 未发出的录音 + client_id（服务端幂等去重） | 发出并完成后删除 |
| `filesDir/history/` | 历史音频/视频/文字 | 永久 |
| `filesDir/results/` | 临时结果文件（上限 60 个轮转） | 可清理 |
| `filesDir/employees_server.json` | 员工档案缓存 | 覆盖写，离线可用 |
| `SharedPreferences`（`McpConfig`） | 端点、工具名、超时、演示模式、监听开关、sessionCookie 等 | 全部可在设置页覆盖，无需重新编译 |

## 6. 接口契约（App 依赖的部分）

| 端点 | 用途 | 关键参数 |
|---|---|---|
| `POST /api/login` | 工作台登录 | `{account, password}` → `{ok, cookie…}` |
| `POST /api/asr?ext=wav&source=app` | 只识别 | body=音频二进制，回 `{ok, text}` |
| `POST /api/chat/send` | 发文字 | `{text, source:"app"}` → `{ok, threadId}` |
| `POST /api/chat/voice?text=` | 发语音（带已识别文本） | body=wav，`text` 跳过二次 ASR |
| `GET /api/chat/messages?thread=app:secretary` | 拉回复 | 增量 id、`meta.audio` 语音地址 |
| `voice_chat` / `voice_job`（MCP） | 老同步/异步任务、通话会话 | `audio_base64`、`wait`、`profile`、`avatar` |
| `boss_briefing` | 小秘书简报 | 异步 job → `voice_job` |
| `tts_synthesize` | 试听/待办决定语音化 | 文本 → base64 WAV |
| dispatch WS | 实时通道 | 上行 WAV、下行 ack/回复/结构化文字 |

错误映射：`forbidden` → 「当前账号不是老板，语音模式只对老板开放」；`unauthorized` → 「登录已过期，请重新登录」。二进制路由（`/api/asr`、`/api/chat/voice`）服务端必须在读取 JSON body 之前拦截，否则 body 被预读为空——这是 0.13.0 联调踩过的坑。

## 7. 音频与视觉

- 录音：`WavRecorder` 直接写 PCM 并封装 RIFF 头，16k/16bit/mono，上限 60s。
- 播报：`VoicePlayer` 优先播服务端 WAV（`meta.audio`），没有才用系统 `TextToSpeech`。
- 流式：`WavStream` 解析「44 字节头 + 100ms 载荷」序列，一条连接可连续多个 WAV；`PcmPlayer` 用一条 `AudioTrack` 连续写，避免每句重建轨导致的「咔咔响」。
- 口型：`LipSync` 从落盘 WAV 预算包络（不用 `Visualizer`，避免权限与定制 ROM 兼容问题），播放时按 `MediaPlayer.getCurrentPosition()` 查表。
- 仿真人：`DigitalHumanView` 纯 Canvas 分层绘制（光晕/身体/手势/头/声波/工牌），外部只需喂 `setFrame(音量, 口型, 表情, 是否在说话)`，自维护 ~30fps 呼吸、点头、眨眼。
- 电平：`VoiceLevelView` 由外部 tick（80–100ms）驱动重绘，不自建动画，省电。

视觉规范：主色 `#006C63`、卡片白 `#FFFFFF`、hero 渐变 `#E8F5F2→#E4F4F0`、圆角 24–30dp；大圆钮 148dp 居中（hero 264dp，通话接通后收紧到 84dp）。

**交互硬规则（0.13.x 定的）**：① 大圆钮默认状态不允许任何循环动画（连接环/识别环已在 0.13.1 从布局删除，避免任何代码路径再点亮）；② 状态文字、计时、电平一律放按钮**上方**，因为按住时手指必然盖住按钮下沿；③ 双胶囊按钮必须「浅底深字」或「深底白字」，不能深底深字——0.13.3 修的正是 `bg_pill`（实心主色）配 `primary` 文字导致「只有背景没有文字」。

## 8. 构建与发布

```bash
cd android && ./gradlew collectApks        # 唯一正确出包入口
```

- 产物：`dist/voice-mcp-v<版本>-b<号>-{debug,release}.apk`，同时**镜像**到 `/mnt/sefs/conring/tmp`（`-PapkMirrorDir=` 可覆盖；目录不可用只告警）。
- 版本号三处必须一致：`appVersionName` / `appVersionCode` / `McpConfig.CLIENT_VERSION`，`verifyVersionSync` 任务不一致直接失败。
- 签名**固化**：只有一张证书 `signing/conring-release.jks`（`keystore.properties` 指过去，两者都在 `.gitignore` 里）。debug 与 release 共用这张证书，任意包可覆盖安装另一个；配置期会把证书 SHA-256 与 `EXPECTED_CERT_SHA256`（`c8db44ed…85bae`）比对，不一致直接阻断——**不再回退 `~/.android/debug.keystore`**（那文件会自动重建，签名一变就得卸载重装）。
- 发布验证：`apksigner verify --print-certs` 两个包指纹相同且等于固化值；`aapt dump badging` 确认 `application-label:'同心圆'`。

## 9. 已知边界与后续

- 单进程单账号，不做多老板切换；会话面板按人+天，跨设备不同步（服务端 `chat_messages` 才是事实源）。
- 实时通话依赖服务端 WS 可用；不可用时 20s 超时退回语音模式。
- 无素材时用 Canvas 仿真人兜底，形象精度不如服务端真人视频。
- 明文 HTTP 仅用于内网应急端点，`network_security_config` 已限定；正式环境一律 HTTPS。
- 待完善：通话与语音模式共用一套「说话中」检测（避免通话时大圆钮仍可录）、无障碍（TalkBack）文案、平板横屏布局。