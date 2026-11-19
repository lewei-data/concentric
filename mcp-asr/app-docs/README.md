# MCP 语音指令（Android App）

只做一件事：**按下按钮说话 → 通过 MCP 把语音发给同心圆 agent 服务的 `voice_chat` → 显示文字、自动语音播报、返回的文件可以打开查看。**

**已生成 APK**：`dist\voice-mcp-debug.apk`（120,167 字节；2026-11-10 **OPC 数字员工档案 + 员工汇报画面版**重新构建，默认端点为正式域名；云端 CI 配置见第 5 节，界面说明见第 11 节，数字员工说明见第 12 节）。

工程目录：`E:\src2026\voice-mcp-android`，标准 Android Gradle 工程，Java + 纯 Android framework，**零第三方依赖**（无 AndroidX / OkHttp / Kotlin）。

---

## 1. 正式服务端配置（已切换并实测）

**App 默认端点：`https://ai.boraydata.com/conring/asr/mcp`**

| 服务 | 内网应急端点（需 VPN） | 正式域名端点 | serverInfo |
| --- | --- | --- | --- |
| mail | `http://192.168.31.208/conring/mail/ceo/mcp` | `https://ai.boraydata.com/conring/mail/ceo/mcp` | mail 1.0.0 |
| tts | `http://192.168.31.208/conring/tts/mcp` | `https://ai.boraydata.com/conring/tts/mcp` | tts 1.0.0 |
| **asr（App 调用）** | `http://192.168.31.208/conring/asr/mcp` | **`https://ai.boraydata.com/conring/asr/mcp`** | asr 1.0.0 |

> ⚠️ **别把端点填成 `https://ai.boraydata.com/mcp`**：那是另一套服务「SEFS Data MCP v2.1.0」（远程文件系统 / SQL / 媒体分析 / 向量检索 / 代码图谱，59 个工具），**没有 `voice_chat`**，且需要 Bearer Token。同心圆 agent 服务必须带 `/conring/...` 前缀。

```
[手机] 录音 16kHz WAV → base64
   │  HTTPS  MCP Streamable HTTP  tools/call voice_chat
   ▼
[ai.boraydata.com/conring/asr/mcp] ASR(whisper) → Hermes agent → TTS(CosyVoice2)
   │  content: [ text块(JSON), audio块(base64 wav) ]
   ▼
[手机] 文字展示 + 保存 txt/wav 附件 + 自动播放语音 + 点击用系统应用打开
```

- 传输：MCP Streamable HTTP，`protocolVersion = 2025-03-26`，响应头 `mcp-session-id`，服务端 uvicorn / FastMCP，响应为 `application/json`。
- 鉴权：`/conring/*` 三条路径**均不需要 Token**，App 的 Bearer Token 保持留空。
- 网络：走公网 HTTPS，**不再需要 VPN**（原来的 `192.168.31.x` 内网直连才需要）。

## 2. 域名验证结果（2026-11-10）

| 验证项 | 结果 |
| --- | --- |
| DNS | `ai.boraydata.com` → `49.7.58.10`；HTTP 根路径 301 → HTTPS |
| TLS 证书 | Let's Encrypt，`CN=ai.boraydata.com`，有效期至 2026-12-13，Android 系统信任 |
| `/conring/{mail/ceo,tts,asr}/mcp` 握手 | 三条路径全部 `200` + `mcp-session-id` + serverInfo，不带 Token 也通过 |
| 其它路径 | `/agent/mcp`、`/hermes/mcp`、`/asr/mcp` 等均为 404（只有 `/conring/*` 有效） |
| `/mcp`（对照） | 未带 Token → 401（提示去 `/token` 申请）；带 Token → `sefs-mcp 2.1.0`，59 个工具，无 `voice_chat` |
| `tools/list`（asr） | `asr_transcribe`、`voice_chat`；后者 schema 与 App 一致：`audio_base64` / `language` / `model` / `voice` / `profile` / `timeout_sec` |
| **闭环调用** | `/conring/tts/mcp` 合成一句问候 → 回灌 `/conring/asr/mcp` 的 `voice_chat`：`isError: false`，text 块含 `question` / `answer` / `audio_path` / `duration` / `asr`，audio 块 `mimeType=audio/wav`、base64 139,624 字符；**耗时 23.4 秒** |
| **大请求体** | 60 秒静音 WAV（1.92MB → base64 2.56MB，请求体 2.44MB）调 `asr_transcribe` → `200`，15.2 秒，云端反代未返回 413 |
| 内网回退 | `http://192.168.31.208/conring/asr/mcp` 直连 `200`；`https://192.168.31.208/...` 证书 CN 不匹配（内网请用 http，或用域名） |

## 3. 请求与返回样例

请求（`tools/call`）：

```json
{"name":"voice_chat","arguments":{"audio_base64":"<16kHz WAV 的 base64>","language":"zh","model":"small","timeout_sec":300}}
```

成功返回的 `result.content`：

```json
[
  {"type":"text","text":"{\n  \"question\": \"识别出的指令\",\n  \"answer\": \"Hermes 的回复\",\n  \"audio_path\": \"/opt/sefs/opc-workbench/mcp-tts/output/voice_xxx.wav\",\n  \"duration\": 2.18,\n  \"asr\": {\"model\": \"small\", \"language\": \"zh\", \"audio_seconds\": 1.87}\n}"},
  {"type":"audio","data":"<base64 wav>","mimeType":"audio/wav"}
]
```

无语音时返回 `isError: true` + text 块「执行失败：没有识别到语音内容」，App 会按错误状态显示。

## 4. App 使用流程（v0.2 目标流程）

> 当前 APK（120,167 字节）仍是「录音→发送」旧流程；下面是首页改成实时语音通话后的目标体验，逐文件改造清单与可粘贴代码见 [13.11](#1311-首页实时语音通话纯语音2026-11-10)。

1. 首次启动授予录音、麦克风权限。
2. 首页顶部「当前汇报人」默认 **OPC-001 小秘书**（`secretary`），可切换员工。
3. 点首页的「通话」按钮 → 建立实时语音连接（`live_call start mode=audio`），服务端一路 HLS 音频流不中断，静音时也保持连接。
4. 连接成功后按钮变**红色**、文案变「挂断」，随时点击立即结束通话并 `live_call stop`。
5. 通话中一直用麦克风连续采集，本地断句（说完停 0.7 秒）即把这一句发给 `voice_chat(live_session=…)`；小秘书的「收到」和正式回复都直接混进同一路音频流里**立即播放**，App 不再录音后等待整段返回。
6. 同屏显示实时转写的用户话术与小秘书回复文字（可滚动），不再弹出「已发送，等待服务端返回」。
7. 结果仍会落盘（txt/wav），「查看文字」「重播语音」保留；设置页能力不变。

> 每个 MCP 内容块都会落盘一份：`text` 块 → `.txt`，`audio` 块 → `.wav`，`resource` 块 → 按内容存 txt/二进制。文件通过自定义 `ResultFileProvider` 以 `content://` 暴露，不依赖 AndroidX。

## 5. 云端生成 APK（CI，免本地 SDK）

工程已内置 `.github/workflows/build-apk.yml`：推送到 GitHub 仓库（`main` / `master`）或在 Actions 页手动触发 `workflow_dispatch`，即自动构建并在 **Actions → Artifacts → `voice-mcp-debug-apk`** 里下载 APK。

流水线步骤：`actions/checkout` → `setup-java` (Temurin 17) → `android-actions/setup-android` → `sdkmanager platforms;android-35 / build-tools;35.0.0` → `./gradlew assembleDebug` → 上传 APK。

操作：

1. 工程目录执行 `git init && git add . && git commit -m "init"`。
2. GitHub 建空仓库，`git remote add origin <url> && git push -u origin main`。
3. Actions 页等构建完成，下载 artifact 里的 `app-debug.apk`。

其它可选云端方案（把上面几步照搬即可）：

| 平台 | 配置文件 | 说明 |
| --- | --- | --- |
| GitHub Actions | `.github/workflows/build-apk.yml`（已提供） | 公共仓库免费额度 |
| CNB / CODING / 腾讯云构建 | `.cnb.yml` / Jenkinsfile | 企业内网代码托管，适合信创环境 |
| GitLab CI | `.gitlab-ci.yml`，镜像 `gradle:8.14-jdk17` + 装 SDK | 自建 Runner |
| Codemagic / Bitrise | 图形化配置 | 适合带分发与真机测试 |

> 云端 CI 都需要先把代码推到远端仓库（外部动作，需你授权），所以本次没有擅自创建仓库；实际 APK 改为直接从云端下载 SDK 后在本机构建（见第 6 节）。

## 6. 本机构建

需要：JDK 17（或 17/21，Android Studio 自带 JBR 即可）、Android SDK Platform 35 + Build Tools、网络（首次下载 AGP）。

- Android Studio：`File > Open` 选择 `E:\src2026\voice-mcp-android`，等待 Gradle Sync，然后 Run。
- 命令行：`gradlew.bat assembleDebug` → 产物 `app\build\outputs\apk\debug\app-debug.apk`。
- Gradle Wrapper 8.14.3 与 `gradle-wrapper.jar` 已随工程提供；AGP 8.10.1，`compileSdk 35 / minSdk 26`。

**本次（数字员工版）构建**：Temurin JDK 17 + 云端下载的 `platforms;android-35` / `build-tools;35.0.0`，`gradlew.bat assembleDebug --no-daemon` → **BUILD SUCCESSFUL in 49s**（界面版 32s、域名版 28s、首次全量 2m13s）。

交付文件：`dist\voice-mcp-debug.apk`（120,167 字节；`com.boray.voicemcp` v0.1.0，targetSdk 35；SHA256 `c8913c7e8f268414ad03fb99d51132f89989480b7796efdce94e27263999d18b`；`apksigner verify` 通过，v2 签名 / Android Debug 证书）。

安装：`adb install -r dist\voice-mcp-debug.apk`，或把文件传到手机点开安装（需允许未知来源）。

> 已从 APK 的 dex 中核实：默认 URL 为 `https://ai.boraydata.com/conring/asr/mcp`，旧的 `http://192.168.31.206:9102/mcp` 已不存在。本机 SDK 位于临时目录 `%TEMP%\voice-mcp-sdk`，`local.properties` 已指向它；系统清理临时目录后重新构建需再装一次 SDK，或直接走第 5 节云端 CI。

| 校验 | 结果 |
| --- | --- |
| 6 个 XML（Manifest / 2 布局 / values / network config） | 全部 well-formed |
| `R.id` 引用（26 个） | 与布局声明完全一致，无缺失 |
| Java 语法（10 个源码文件） | 解析器全部通过 |
| `javac` + Android 15 framework jar 类型编译 | 0 错误（仅 1 条 deprecated API 提示） |
| Gradle `assembleDebug`（JDK 17 + AGP 8.10.1） | BUILD SUCCESSFUL（首次 2m13s、域名版 28s、界面版 32s、数字员工版 49s） |
| 界面版静态校验 | 44 个 XML well-formed；35 个 `R.id` 全部匹配；33 个 drawable / 30 个 color 引用无缺失 |
| 数字员工版静态校验 | 58 个 XML well-formed；74 个 `R.id` 全部匹配；42 个 drawable / 30 个 color / 7 个 layout 引用无缺失；16 个 Java 文件语法通过 |
| `apksigner verify` / `aapt2 dump badging` | Verifies（v2 签名）；包名、权限、targetSdk 正确；4 份员工档案已打包进 `assets/employees.json` |
| 域名 MCP 链路（握手 / tools/list / tools/call / voice_chat 闭环 / 大请求体） | 全部实测通过 |

## 7. 端点配置与切换

**正式端点已写进代码默认值**（`McpConfig.DEFAULT_URL`），也是设置页的初始值：`https://ai.boraydata.com/conring/asr/mcp`。

- 想临时切回内网：设置页把 URL 改成 `http://192.168.31.208/conring/asr/mcp`，保存即生效（需 VPN）。代码里也留了 `McpConfig.LEGACY_URL` 常量备用。
- Bearer Token **留空**：`/conring/*` 不需要鉴权。只有切到 `https://ai.boraydata.com/mcp`（SEFS FS/DB 服务）才需要去 `/token` 申请 Token，但那套服务没有 `voice_chat`。
- 若以后域名或路径变化，改设置页「MCP 端点 URL」一处即可，无需重新编译；改代码默认值则改 `McpConfig.DEFAULT_URL`。
- `res/xml/network_security_config.xml`：默认 `cleartextTrafficPermitted="false"` 强制 HTTPS，仅对 `192.168.31.206` / `192.168.31.208` 内网应急端点放行 HTTP 明文。
- 工具名、音频参数名、协议版本、音色、Hermes profile 也都在设置页，换到别的 MCP server 时无需改代码即可试配。

## 8. 设置项

| 设置 | 默认值 | 说明 |
| --- | --- | --- |
| MCP 端点 URL | `https://ai.boraydata.com/conring/asr/mcp` | 正式域名；内网回退 `http://192.168.31.208/conring/asr/mcp` |
| 工具名 | `voice_chat` | 可用「查看工具」从服务端发现后点选 |
| 音频参数名 | `audio_base64` | 上传 WAV base64 的参数键 |
| 识别语言 / 模型 | `zh` / `small` | 传给 whisper |
| voice / profile | 空 | 音色 id、Hermes profile，留空走服务端默认 |
| Bearer Token | 空 | 空则不发 Authorization 头；`/conring/*` 无需 Token |
| 协议版本 | `2025-03-26` | 与服务端实测一致 |
| 请求超时 | 600 秒 | 握手/工具发现固定 15 秒，仅工具调用使用长超时 |
| 自动播报 | 开 | 关掉后可用「重播语音」手动播 |
| 播报时显示数字员工汇报画面 | 开 | 关掉后退回原来的纯语音播报（见第 12 节） |
| 优先使用仿真人视频形象素材 | 开 | 档案里配置了 mp4 时用它做汇报画面底片 |
| 演示模式 | 关 | 不联网的本地回显 |

## 9. 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| `Unknown tool: voice_chat` | URL 少了 `/conring/asr/mcp` 前缀，或误填成 FS/DB 的 `/mcp` |
| `HTTP 401` + 「未携带 Token」 | 填到 `https://ai.boraydata.com/mcp`（SEFS）了；同心圆服务不需要 Token |
| `HTTP 404` | 路径拼错，只允许 `/conring/mail/ceo/mcp`、`/conring/tts/mcp`、`/conring/asr/mcp` |
| 证书错误 | 用域名 `https://ai.boraydata.com`；不要对 `192.168.31.208` 用 HTTPS（证书 CN 不匹配） |
| 连接超时 | 域名走公网无需 VPN；若切回内网 `192.168.31.208` 则必须先连 VPN |
| 一直「已发送，等待服务端返回」 | `voice_chat` 正常要几十秒到几分钟；可在设置页调大超时 |
| 「执行失败：没有识别到语音内容」 | 录音太短或环境太安静，靠近麦克风重说一次 |
| 没有声音 | 优先播服务端音频；若音频缺失才用系统 TTS，部分国产 ROM 无中文 TTS 引擎 |
| 打开附件提示无应用 | 设备缺少能打开该 MIME 的应用；txt/wav 通常都有 |

## 10. 已知边界

- `voice_chat` 会让 Hermes 以完整 agent 权限执行语音指令，可能触发外部动作（例如 `mail_send`）；生产使用前建议加白名单或二次确认。
- 服务端会在 `/opt/sefs/opc-workbench/mcp-tts/output/` 生成 wav/srt/segments.json，那些是**服务器本地路径**，App 不直接访问；App 打开的是从内容块落盘的副本。
- 单次响应音频、录音 base64 均可达数 MB，弱网下耗时明显；当前云端反代已验证 2.44MB 请求体可通，后续可改为返回 URL 而非 base64。
- 这是 **debug 签名** APK，仅用于自测，不能上架；要发布需配正式签名。

## 11. 界面设计（参考 LocalSend，2026-11-10 美化）

LocalSend 官方主题是 **Material 3 + `Colors.teal` 种子色**（`app/lib/config/theme.dart`，支持 system / localsend / oled / yaru 配色），界面语言为「大圆角卡片 + 图标化 BigButton + 状态徽章 + 三段导航」。本工程按这套语言重做，**仍是零第三方依赖的纯 framework 实现**：

| 元素 | 实现方式 |
| --- | --- |
| 双主题 | `values/colors.xml` + `values-night/colors.xml`（teal 主色，跟随系统深色模式自动切换）；`Theme.Material.Light.NoActionBar` / `Theme.Material.NoActionBar`，状态栏与导航栏同色 |
| 卡片 | `bg_card.xml`（24dp 圆角 + 细描边）、`bg_hero.xml`（渐变 hero 区）、`bg_card_soft.xml`（分组底）、`bg_input.xml`（输入框聚焦态描边） |
| 核心动作 | 148dp 圆形麦克风 `bg_mic.xml`（selector：默认渐变 / 录音中红 / 禁用灰）+ 190dp 呼吸光环 + 176dp 进度环 + 按压缩放动画 |
| 状态反馈 | 连接卡内 9dp 状态点（正常 teal / 进行中琥珀 / 错误红）+ 顶部「已就绪 / 演示模式」徽章 + 结果文字淡入 |
| 快捷操作 | 三个 BigButton（重播语音 / 查看文字 / 连接设置），矢量图标 + ripple |
| 设置页 | 分组卡片（连接 / 工具 / 识别与播报 / 行为）、胶囊主按钮、描边次按钮、`Switch` + ColorStateList 主题色、顶部返回按钮 |
| 附件列表 | `item_attachment.xml` 卡片行：类型图标 + 文件名 + 大小/类型 + 箭头，点击用系统应用打开 |
| 图标 | 10 个 `<vector>` drawable，零 PNG、零图标库；33 个 drawable 全部为 XML |

新增资源：`res/values-night/`、`res/color/`、`res/drawable/*`、`res/layout/item_attachment.xml`。
Java 侧适配：`MainActivity` 改为 View 型按钮 + 状态点/脉冲/进度环/淡入 + 附件卡片渲染；`SettingsActivity` 增加返回按钮并把 CheckBox 换成 Switch。

> 功能与配置完全不变：`voice_chat` 调用、结果解析、语音播报、文件打开、演示模式、域名端点与全部设置项保持原样。

## 12. OPC 数字员工档案与员工汇报画面（2026-11-10 新增）

### 12.1 功能组成

| 能力 | 落地位置 | 说明 |
| --- | --- | --- |
| 数字员工档案 | `assets/employees.json` + `Employee.java` / `EmployeeStore.java` | 内置 4 位员工，各自独立的工号、部门、职位、简介、技能、口头禅、音色、外观参数与人设 profile |
| 档案界面 | `EmployeeListActivity` / `EmployeeDetailActivity` | 列表卡片带「汇报员」徽章；详情展示形象照、仿真人视频素材状态、完整档案、设为汇报员、试听汇报 |
| 汇报画面 | `ReportActivity` | 结果返回后自动全屏弹出：员工名牌 + 形象舞台 + 分句字幕 + 进度条 + 播控（重播 / 切换员工 / 结束） |
| 口型 / 表情 / 动作 | `LipSync.java` + `DigitalHumanView.java` | 服务端 WAV 的 PCM 振幅包络实时驱动嘴型；文本情感驱动表情；呼吸、点头、摆头、眨眼、手势随播报节奏变化 |

主界面新增「当前汇报员」卡片，点击进档案切换。切换后 `voice_chat` 请求里的 `profile` 参数会自动带上该员工标识，让 Hermes 按对应人设作答（设置页手工填的 profile 优先）。

### 12.2 档案字段

`app/src/main/assets/employees.json` 是默认档案；若 `filesDir/employees.json` 存在则优先读它（可用 `adb push` 导入真实档案，不用重新编译）。

| 字段 | 含义 |
| --- | --- |
| `id` / `no` / `name` / `dept` / `title` / `motto` / `bio` / `skills` | 组织信息，列表与详情页展示 |
| `photo` | 照片素材，示例为 `drawable://photo_lin` 等 4 张矢量形象照 |
| `videoIdle` / `videoTalk` | 仿真人视频形象（待机 / 汇报），示例留空，放入素材后自动生效 |
| `profile` | 传给 `voice_chat` 的人设标识，例如 `opc-001-lin-zhiyuan` |
| `voice` | 音色 id；服务端已使用——`voice_chat` 未显式传 `voice` 时按该员工档案的音色播报（18 人各不相同，见第 13.8 节） |
| `accent` / `skin` / `hair` / `suit` / `shirt` | 内置 Canvas 形象的配色，保证每人长相不同 |
| `hairStyle` / `glasses` / `emotion` | 发型（`short`/`long`/`bun`/`curly`）、眼镜、情绪基线（`calm`/`cheerful`/`serious`） |

素材字段支持四种写法：`drawable://资源名`、`assets://employees/xxx.jpg`、`file:///sdcard/...`、`https://...`。留空则由内置 Canvas 数字人按外观参数实时绘制。

**换成真实素材**：把 `xxx.jpg` / `xxx.mp4` 放到 `app/src/main/assets/employees/`，把档案里的 `photo` 改成 `assets://employees/xxx.jpg`、`videoTalk`（或 `videoIdle`）改成 `assets://employees/xxx.mp4`，重新出包即可；远程 URL 方式甚至不用重编。assets 下的 mp4 会被 `EmployeeStore` 自动复制到缓存供 `VideoView` 播放。

### 12.3 口型、表情、动作怎么被驱动

1. `voice_chat` 返回的 `audio` 内容块已由 `ResultParser` 落盘为 WAV。
2. `LipSync.fromWav()` 解析 RIFF / `fmt ` / `data`，按 **40ms 一帧**计算 16-bit PCM 的 RMS，用 90 分位归一化 + 三点平滑，得到 `level(t)`；再按能量与确定性伪随机映射成 5 种口型（闭 / A / E / O / U）。
3. `ReportActivity` 以 ~30fps 读 `MediaPlayer.getCurrentPosition()`，把 `level` 与口型喂给 `DigitalHumanView` —— 所以**口型和声音是同一份音频驱动的**，不是预录动画。
4. 表情按「汇报阶段 + 文本情感」切换：开场自信、结束微笑；正文里出现「完成/成功」→ 微笑，「失败/错误/异常」→ 关切，「请确认/是否」→ 思考，其余用员工情绪基线。
5. 动作由 View 内部时钟生成：呼吸起伏、说话点头、摆头、周期性眨眼、手势随音量抬起、两侧声波弧线；播报结束收势致意。
6. 没有音频（演示模式 / 系统 TTS）时用 `LipSync.synthetic()` 按文本音节估算包络，汇报画面照样有口型。

**实测**（域名 `tts_synthesize` 真实音频）：124,632 字节 WAV → 2,594ms / 64 帧，79% 帧有口型动作，5 种口型分布为闭 13、A 19、E 20、O 7、U 5，静音段正确收嘴；合成包络 6,880ms / 172 帧。

### 12.4 边界与后续

- 示例档案的 `videoIdle` / `videoTalk` **留空**：代码、字段、加载、循环静音播放与失败回退都已实现，但仓库里没有提供真实的仿真人 mp4；放入素材后汇报画面会自动切成「视频底片 + 角落驱动小窗」。
- 4 张形象照是矢量占位图，每人不同；可换成真人证件照。
- 口型基于**音量包络 + 元音轮换**，不是音素/viseme 识别；要做到逐音素精确，需服务端返回 phoneme/viseme 时间轴或实时数字人视频流，客户端已预留 `setFrame()` 接口可直接接。
- 视频形象是静音循环底片，其自带口型不会与播报音频同步；真正同步的口型由角落里的内置驱动数字人呈现。
- 设置页可关闭「汇报画面」回到原来纯语音播报，或关闭「优先使用视频素材」只看内置驱动形象。

---

## 13. 新增接口使用说明（server v2.2.0，2026-11-10）

本轮新增/变更 4 个能力，全部走同一条 MCP 通道（`tools/call`），不影响已有调用：

| 接口 | 状态 | 用途 | 客户端何时调用 |
| --- | --- | --- | --- |
| `profile` | **新增** | 一次取回全部员工档案（含形象素材地址、音色、性别年龄、Hermes profile 是否就绪） | App 启动 / 下拉刷新，替代本地写死的 employees.json |
| `voice_avatars` | 增强 | 只取数字人素材清单（照片 + 待机/汇报视频） | 需要素材地址但不需要组织信息时 |
| `voice_chat` | **新增参数** | 立即回「收到」语音 + job_id；新增 `callback_url` / `avatar_id` / `mode` / `bandwidth_kbps` | 按下说话按钮 |
| `voice_job` | **新增** | 按 `job_id` 取异步结果（可长轮询 30 秒） | `voice_chat` 返回后 |
| `intro_video` | **新增** | 按需生成员工自我介绍视频（大模型润色 + TTS + talk 画面），缓存复用 | 运营预热；客户端只读 `profile.introVideo` |
| `live_call` | **新增** | 实时通话：起 HLS 直播流 / 让员工说话 / 挂断 | 档案页点「通话」，见 13.9 |

### 13.1 `profile`：获取员工档案

请求：

```json
{"name":"profile","arguments":{"id":"ceo","include_assets":true}}
```

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 空=全部 | 员工 Agent ID（`boss` / `ceo` / `pm_wenren` …），空则返回 18 人 |
| `dept` | string | 空 | 按部门**模糊**过滤，如 `产品中心` |
| `project` | string | 空 | 按项目**模糊**过滤，如 `文人莫克` |
| `include_assets` | bool | `true` | 是否探测照片/视频文件并回填地址；只要名单时传 `false` 更快 |

响应（`content[0].text`，JSON；下面是 `id=ceo` 的**线上实测**返回，省略了 `skills` 数组内容）：

```json
{
  "count": 1,
  "asset_url_base": null,
  "employees": [{
    "id": "ceo", "no": "OPC-002", "name": "陆清安",
    "dept": "公司管理层", "title": "CEO", "project": "全公司", "role": "CEO",
    "motto": "所有人对齐同一张作战地图。", "bio": "统管三个自媒体项目……",
    "skills": ["经营分析", "目标拆解", "…"],
    "profile": "ceo", "voice": "m08", "gender": "male", "age": 42,
    "accent": "#2563EB", "skin": "#EBC9A8", "hair": "#1F2937",
    "suit": "#1E3A5F", "shirt": "#FFFFFF",
    "hairStyle": "short", "glasses": false, "emotion": "calm",
    "profile_ready": true,
    "photo": "/avatars/ceo.png",
    "videoTalk": "/videos/ceo.talk.mp4",
    "videoIdle": "/videos/ceo.idle.mp4",
    "has_photo": true, "has_video": true,
    "assets": {
      "photo": {"path": "/home/boray/opc-workbench/prototype/assets/avatars/ceo.png", "url": "/avatars/ceo.png"},
      "face_url": "/faces/ceo.png",
      "video_talk": {"path": "…/videos/ceo.talk.mp4", "url": "/videos/ceo.talk.mp4"},
      "video_idle": {"path": "…/videos/ceo.idle.mp4", "url": "/videos/ceo.idle.mp4"}
    }
  }],
  "missing_photo": [],
  "missing_video": []
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `gender` / `age` | **本轮新增**，`male`/`female` 与整数年龄，用于形象差异化与筛选 |
| `voice` | 该员工的专属音色 id（`m01`–`m10` 男声、`f01`–`f08` 女声）；`voice_chat` 不传 `voice` 时自动用它 |
| `photo` / `videoTalk` / `videoIdle` | 素材**URL**（通常为相对路径，见 13.7）；空字符串表示没有该素材 |
| `has_photo` / `has_video` | 布尔快捷判断，客户端可直接据此决定用真人素材还是内置 Canvas 数字人 |
| `assets.*.path` | 服务器本地绝对路径，仅用于排查，不要下发给客户端 |
| `assets.face_url` | 面部特写图（口型贴图用），无则为 `null` |
| `profile_ready` | 是否有独立 Hermes profile；`false` 时语音点名派单会失败（如 `boss`、`secretary`、`manual_scout`） |
| `recordOnly` / `duties` | **本轮新增**：`recordOnly=true` 表示这份档案只做记录、不接任务不派单（`OPC-000` 老板本人）；`duties` 是职责清单（`OPC-001` 小秘书：代老板处理日常、定时巡检汇报、重要事项催办、偶尔打小报告） |
| `introVideo` / `introReady` / `introText` / `introDuration` | **本轮新增**：服务端生成的自我介绍视频（画面+语音）、是否已生成、文案、实测时长（秒）；未生成时 `introVideo=""`、`introReady=false`，见 [静态素材接入.md](静态素材接入.md) 第 10 章 |
| `liveCall` | **本轮新增**：`{supported, protocol:"hls", segment_sec}`，`supported=false` 时客户端隐藏通话按钮，见第 11 章 |
| `missing_photo` / `missing_video` / `missing_intro` | 全量查询时的缺件清单（照片 / 视频 / 自我介绍视频），便于素材补齐 |
| `asset_url_base` | 服务端配置的素材前缀；`null` 表示返回相对路径 |

### 13.2 `voice_avatars`：只要素材

```json
{"name":"voice_avatars","arguments":{}}
```

返回 `{asset_dirs, asset_url_base, count, avatars:[{id,name,role,photo{path,url},videos{talk{path,url},idle{path,url}},has_video}]}`。
适合「员工列表页只显示头像」的场景，体积比 `profile` 小。

### 13.3 `voice_chat`：新增参数

原有参数不变（`audio_base64` / `audio_path` / `language` / `model` / `initial_prompt` / `voice` / `profile` / `timeout_sec`），新增：

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `avatar_id` | string | 自动 | 播报用哪个员工的形象；不传时=被点名员工，没点名时按 `profile`，再退到 `ceo` |
| `mode` | enum | `auto` | `voice` 纯语音；`avatar` 照片/内置数字人按 WAV 包络驱动；`video` 叠加仿真人视频素材；`auto` 按带宽与素材自动选 |
| `bandwidth_kbps` | number | 0 | 客户端下行带宽；`auto` 模式下 < `ASR_VIDEO_MIN_KBPS`（默认 600）降级为纯语音 |
| `callback_url` | string | 空 | 任务完成后服务端 POST 结果 JSON 到此地址（不保证送达，必须保留轮询兜底） |
| `wait` | bool | `false` | `true` 时同步等待结果（阻塞，最长 `wait_sec`，默认 240，上限 600） |
| `wait_sec` | number | 240 | `wait=true` 的最长等待 |
| `include_audio` | bool | `true` | `wait=true` 完成时是否连结果语音一起返回 |
| `delegate` | bool | `true` | 是否允许按语音点名自动派单给员工 |
| `emotion` | enum | `auto` | 播报情绪：`auto`/`neutral`/`cheerful`/`calm`/`serious`；`auto` 取播报对象档案里的 `emotion`（见 13.9） |
| `clarify` | bool | `true` | 识别置信度低时是否改用「没听清，请再说一遍」的情绪化语音兜底（见 13.9）；`false` 则照旧执行识别文本 |
| `clarify_timeout_sec` | number | 20 | 低置信时等大模型生成澄清语的秒数，超时用模板句 |

**音色选择优先级**：显式 `voice` > 被点名/播报员工的档案 `voice` > 服务端默认 `ASR_TTS_VOICE`。
所以客户端**不需要**再传 `voice`，除非要覆盖。

响应（`content[0]` 文本块 + `content[1]` 音频块）：

```json
{
  "job_id": "vj_3f9a1c_58213",
  "status": "accepted",
  "question": "让产品经理看一下这周的选题",
  "ack": {"text": "好的老板，马上安排给产品经理。", "audio_path": "…/ack_x.wav",
          "sample_rate": 24000, "duration": 1.9, "segments": [{"index":0,"text":"…","start":0.0,"end":1.9}]},
  "mode": "video",
  "avatar": {"id": "pm_wenren", "name": "贺平生", "role": "产品经理",
             "photo": {"path": "…", "url": "/avatars/pm_wenren.png"},
             "videos": {"talk": {"path": "…", "url": "/videos/pm_wenren.talk.mp4"},
                        "idle": {"path": "…", "url": "/videos/pm_wenren.idle.mp4"}},
             "has_video": true},
  "delegate": {"id": "pm_wenren", "name": "贺平生"},
  "emotion": "calm",
  "asr": {"model": "small", "language": "zh", "audio_seconds": 3.4,
          "confidence": 0.78, "avg_logprob": -0.24, "no_speech_prob": 0.02,
          "low_confidence": false},
  "next": "调用 voice_job(job_id) 取结果；任务完成也会推送到 callback_url"
}
```

> 结构为线上接口真实字段；`job_id` / 时长等取值随调用变化。

### 13.4 `voice_job`：取异步结果

```json
{"name":"voice_job","arguments":{"job_id":"vj_3f9a1c_58213","wait_sec":30}}
```

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `job_id` | string | 必填 | `voice_chat` 返回的任务 id |
| `wait_sec` | number | 0 | 长轮询秒数，**上限 30**；0=立即返回当前状态 |
| `include_audio` | bool | `true` | 完成时是否在 `content[1]` 附带结果语音 |

`status` 依次为 `accepted` → `running` → `done` / `error`；另有 `need_repeat`（识别置信度低，未执行指令，见第 15 章）。

`status=done` 时：

```json
{
  "job_id": "vj_3f9a1c_58213", "status": "done",
  "question": "让产品经理看一下这周的选题",
  "delegate": {"id": "pm_wenren", "name": "贺平生"},
  "mode": "video", "avatar": { "…": "同 voice_chat" },
  "result": {
    "speech_text": "本周选题已排好，共 6 条，周三前交初稿。",
    "detail_text": "1. 周一…\n2. 周三…",
    "audio_path": "/opt/sefs/opc-workbench/mcp-tts/output/report_x.wav",
    "audio_mime": "audio/wav",
    "sample_rate": 24000, "duration": 4.8,
    "segments": [{"index":0,"text":"本周选题已排好，","start":0.0,"end":1.6}],
    "render": {"driver": "wav_envelope", "lip_sync": "amplitude",
               "sample_rate": 24000, "timeline": "segments", "avatar": { "…": "同上" }}
  },
  "voice": "m04",
  "emotion": "calm",
  "created_at": 1762770000.1, "started_at": 1762770000.4, "finished_at": 1762770042.7
}
```

约定：

- `speech_text` 是**播报用摘要**（≤90 字），`detail_text` 是完整答复（正文/详情页展示）。
- `segments` 为句级时间轴（秒），与 WAV 严格对齐，可直接做字幕与口型分段。
- `render.driver="wav_envelope"`、`lip_sync="amplitude"` 是给客户端的渲染指令：按 WAV 振幅包络驱动口型。
- `error` 状态带 `error` 字段（字符串），客户端应提示并允许重试。
- `status=need_repeat`（v2.2.0 起）：识别置信度低，**没有执行指令**；澄清语音已在 `voice_chat` 的 `content[1]` 给过，`voice_job` 查询不会再带音频（详见第 15 章）。
- `audio_path` 是服务器本地文件：情绪化开启且需要变调时指向 `/opt/sefs/opc-workbench/mcp-asr/output/<stem>.wav`，否则为 TTS 原始输出；客户端应始终播 `content[1]` 的 base64，不要依赖该路径。
- 任务保留 `ASR_JOB_TTL`（默认 7200 秒）、最多 `ASR_JOB_MAX`（默认 100）个；过期后返回「任务不存在或已过期」。

`callback_url` 收到的 POST 体与上面的 done JSON 一致，**但不含音频 base64**（只给 `audio_path`，音频仍走 `voice_job` 的 audio 内容块或素材 HTTP 取）。

### 13.5 推荐调用时序

```
① profile（缓存到本地，App 启动/下拉刷新一次）
② 录音 → voice_chat(audio_base64, avatar_id, bandwidth_kbps)
      ├─ 置信度够 → 立刻拿到 ack 音频 → 立即播放「收到」，界面切到该员工的汇报画面
      │              └─ 拿到 job_id
      └─ 置信度低 → status=need_repeat + 澄清语音，本轮结束（不调 voice_job，请老板重说）
③ voice_job(job_id, wait_sec=30) 循环，直到 done / error
      └─ done → 播放 content[1] 音频 + 显示 detail_text + 按 segments 驱动口型/字幕
④ （可选）传 callback_url，收到推送时提前结束轮询
```

Java 伪代码（与现有 App 同一套 `HttpURLConnection` + 手写 JSON）：

```java
// mcpCall 返回整个 tools/call 结果，content[0]=text(JSON)，content[1]=audio（base64 在 data）
JSONObject res = mcpCall("profile", "{\"include_assets\":true}");
EmployeeStore.merge(contentText(res));                // 本地缓存，离线可用

JSONObject res2 = mcpCall("voice_chat", "{" +
    "\"audio_base64\":\"" + b64 + "\"," +
    "\"avatar_id\":\"" + employeeId + "\"," +
    "\"mode\":\"auto\",\"bandwidth_kbps\":" + kbps + "}");
playWav(base64ToBytes(audioData(res2)));              // content[1].data → 立即播「收到」

JSONObject chat = new JSONObject(contentText(res2));
String jobId = chat.getString("job_id");
while (true) {
    JSONObject res3 = mcpCall("voice_job",
        "{\"job_id\":\"" + jobId + "\",\"wait_sec\":30}");
    JSONObject job = new JSONObject(contentText(res3));
    if ("done".equals(job.getString("status")))  { renderReport(job, res3); break; }
    if ("error".equals(job.getString("status"))) { showRetry(job.getString("error")); break; }
}
```

> 注意：`ack` 对象里**没有**音频 base64，音频始终在内容块 `content[1]`（`{"type":"audio","data":"…","mimeType":"audio/wav"}`），解析方式与第 3 节一致。

### 13.6 19 名员工的音色与形象

共 19 条档案：`OPC-000` 老板本人（真实档案，只记录不派活）、`OPC-001` 小秘书（老板影子分身，代老板处理事务、巡检汇报与催办）、`OPC-002`~`OPC-018` 各岗位员工。小秘书 2026-11-10 起独立形象与音色（年轻女声 `female-zh-real`），不再复用老板素材。

每个人 `gender` / `age` / `voice` 都不同，`voice_chat` 会自动使用员工自己的音色与形象。

**形象素材（2026-11-10 更新）**：OPC-000 与 OPC-002~017 共 17 位员工的档案形象，全部改用 `/mnt/sefs/obsidian-huawei/素材/同心圆员工照片` 里的定妆照（`同心圆员工照片-01..17`，2048×2048，首席创意官为 1536×1536；统一为浅色背景半身正面、胸前工牌，右下角带生成水印）。派生规则：

| 产物 | 规格 | 裁切方式 |
| --- | --- | --- |
| `assets/avatars/<id>.png` | 512×512 | 用背景色差估人物顶边，从头顶起取 58% 居中方块（头肩特写，避开底部水印） |
| `assets/faces/<id>.png` | 256×256 | 头顶再上留 2%，取 46% 方块（完整头部+颈部） |
| `assets/videos/<id>.talk.mp4` | 720×960 / 6 s / 25 fps | 头顶对齐的 3:4 竖版底片，缓慢推近（zoom 1.00→1.07） |
| `assets/videos/<id>.idle.mp4` | 720×960 / 8 s / 25 fps | 同一底片，缓慢拉远（1.07→1.00） |
| `assets/intro/<id>.mp4` | 720×960 + AAC | 以新 talk 视频循环为画面、原自我介绍文案重新合成（文案缓存保留，只换画面与音色） |

OPC-001 小秘书与 OPC-018 灰太狼在该目录没有对应定妆照，沿用既有素材。`employees.json` 里每个员工的 `photoSource` 记录了所用定妆照文件名，顶层 `assetSource` 记录了素材来源目录。

| 工号 | id | 姓名 | 性别 | 年龄 | 音色 | 实测基频 |
| --- | --- | --- | --- | --- | --- | --- |
| OPC-000 | `boss` | 蒿色莫 | 男 | 52 | `m10` | 131 Hz |
| OPC-001 | `secretary` | 小秘书 | 女 | 27 | `female-zh-real` | 真人参考音色 |
| OPC-002 | `ceo` | 陆清安 | 男 | 42 | `m08` | 160 Hz |
| OPC-003 | `cco` | 沈青禾 | 女 | 36 | `f07` | 203 Hz |
| OPC-004 | `cto` | 扶频 | 男 | 33 | `m05` | 150 Hz |
| OPC-005 | `pm_wenren` | 贺平生 | 男 | 30 | `m04` | 158 Hz |
| OPC-006 | `pm_gou` | 米珂 | 女 | 26 | `f03` | 264 Hz |
| OPC-007 | `pm_tongxinyuan` | 姜月初 | 女 | 31 | `f05` | 207 Hz |
| OPC-008 | `cw_wenren` | 陆川 | 男 | 28 | `m03` | 155 Hz |
| OPC-009 | `cw_gou` | 叶秋 | 女 | 22 | `f01` | 267 Hz |
| OPC-010 | `cw_tongxinyuan` | 江佑泽 | 男 | 25 | `m01` | 147 Hz |
| OPC-011 | `vd_wenren` | 陆离 | 男 | 35 | `m06` | 159 Hz |
| OPC-012 | `vd_gou` | 萧执 | 男 | 27 | `m02` | 163 Hz |
| OPC-013 | `vd_tongxinyuan` | 崔聿棠 | 女 | 29 | `f04` | 253 Hz |
| OPC-014 | `release_ops` | 顾景琛 | 男 | 38 | `m07` | 145 Hz |
| OPC-015 | `reviewer` | 顾凛川 | 男 | 46 | `m09` | 137 Hz |
| OPC-016 | `scout` | 苏寒 | 女 | 40 | `f08` | 200 Hz |
| OPC-017 | `kb_manager` | 祁南 | 女 | 34 | `f06` | 205 Hz |
| OPC-018 | `manual_scout` | 灰太狼 | 女 | 24 | `f02` | 242 Hz |

“实测基频”为同一句中文（“各位好，我是本次汇报的员工，今天先同步一下进度和数据。”）合成后按自相关法测得的中位数，24 kHz 输出、随机取 1024 点帧，误差约 ±5 Hz。

音色的构造方式：从 3 个中文参考说话人（1 男 2 女）里切出 6 段参考音频，再按 0.92–1.04 变调派生 18 个音色 id，写入 `/opt/sefs/opc-workbench/mcp-tts/voices.json`（热加载，新增音色首次使用即注册，无需重启）。
**客观效果**：男女声分离明显（男 131–163 Hz，女 200–267 Hz），同性别内音高与语速也有可听差别；但变调幅度只有 ±4%，年龄感属于“略年轻/略年长”，并非真人级别的年龄差异。若要更明显的年龄差，需要更多真人参考音频（当前只有 3 位中文说话人）。
`tts_list_voices` 可列出全部 21 个音色（含 `female-zh-real` 真人参考、`female-zh` / `female-zh-short` 两个旧音色）。

2026-11-10 引擎升级：TTS 默认模型从 `CosyVoice2-0.5B` 换成 `Fun-CosyVoice3-0.5B-2512`，小秘书与默认播报改用真人参考音色 `female-zh-real`（`/opt/sefs/CosyVoice/asset/zero_shot_prompt.wav`）。同一文本、同一参考音的对照实测（本机 CPU，n=2 句）：

| 组合 | 声纹相似度（campplus 余弦） | 转写字错率（whisper small） | RTF |
| --- | --- | --- | --- |
| CosyVoice2 + Piper 参考（旧默认） | 0.24–0.28 | 0.39–0.61 | 1.9–2.1 |
| CosyVoice2 + 真人参考 | 0.80–0.83 | 0.36–0.39 | 2.4–2.5 |
| CosyVoice3 + 真人参考（当前默认） | 0.85 | 0.13–0.14 | 2.4 |

合成改为多进程池：默认按核数自动算进程数（`TTS_WORKERS`，144 核 → 6 个进程），每进程 `TTS_THREADS`（→ 24 线程），句子级派单到最空闲进程，5 句 / 20.2 s 音频的实测墙钟从 44.6 s（基线 2 进程句级并行）降到 23.5 s（RTF 1.08）。

形象侧：19 张肖像与 38 段视频（`talk` 6s / `idle` 8s，720×960，H.264，无音轨，单文件 ≈50–65 KB）本轮已全部重做，统一商务棚拍风格，性别、发型、眼镜、服装各自不同。
需要说明的是，图像模型对“中年”的呈现偏年轻：档案年龄 22–52 岁，实际观感大约 24–48 岁（`boss`、`reviewer` 明显年长，`release_ops`、`scout` 偏年轻）。若要严格贴合年龄，需要人工挑图或换用可控年龄的图像模型。

### 13.7 素材地址怎么拼

- `profile` 返回的 `photo` / `videoTalk` / `videoIdle` 是**站点相对路径**，前缀由服务端 `ASR_ASSET_URL_BASE` 决定，当前固定为 `/conring/asr/assets`：
  `/conring/asr/assets/avatars/ceo.png`、`/conring/asr/assets/videos/ceo.talk.mp4`、`/conring/asr/assets/faces/ceo.png`。
- 客户端用 MCP 端点同源域名拼即可：`https://ai.boraydata.com/conring/asr/assets/avatars/ceo.png` → 实测 `200`（`image/png`，354,076 B）；`videos/ceo.talk.mp4` 支持 Range（`Range: bytes=0-1023` → 实测 `206`）。
- 通路：nginx（192.168.31.208）的 `location /conring/asr/` 整段反代到 `192.168.31.206:9102`，由 mcp-bridge 的参数 `--static-dir /home/boray/opc-workbench/prototype/assets` 把素材目录挂到 `/assets`，所以**不需要再单独开静态站点**（工作台 8848 仅供局域网预览，公网不暴露）。
- 18 名员工的 `photo` / `videoTalk` / `videoIdle` / `face_url` 已逐个用公网 URL 校验：`18 人 × 4 类，全部 200`，`missing_photo=[]`、`missing_video=0`（2026-11-10）。
- 空 `photo`：客户端回退到内置 Canvas 数字人；空 `videoTalk`：`mode=video` 会自动降级为 `avatar`。
- 完整客户端接入文档（字段映射、Android 示例、缓存与 304、排查表）：[静态素材接入.md](静态素材接入.md)。

### 13.8 排障速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `profile` 里 `profile_ready=false` | 该员工没有独立 Hermes profile（当前 `boss`、`manual_scout`） | 语音点名派单会报错，改用 `delegate=false` 或先建 profile |
| `voice_chat` 返回 `need_repeat`（v2.2.0 起不再报错） | 音频为空/静音/太吵：置信度 < 0.5 或没识别出字 | 正常：播放 `content[1]` 的澄清语音请老板重说；确需照执行就传 `clarify=false` |
| `voice_job` 报「任务不存在或已过期」 | `job_id` 错 / 超过 7200 秒 / 服务重启 | 重新发起 `voice_chat` |
| 播报还是同一个声音 | 显式传了固定 `voice`，或档案 `voice` 未生效（档案为 mtime 热加载，改完即生效） | 去掉 `voice` 参数，或 `profile` 确认 `voice` 字段 |
| 音频块为空 | TTS 超时/失败 | 文本里有 `audio_path` 但无 audio 块时可读服务器文件；重试一次 |
| `mode=video` 但没视频 | 该员工缺 `videoTalk` | 已自动降级 `avatar`，客户端按 `mode` 字段分支即可 |

### 13.9 自我介绍视频与实时通话（server 2.2.0，2026-11-10）

**自我介绍视频（`intro_video`）**：服务端为每位员工生成「画面 + 语音」的自我介绍，缓存在 `assets/intro/<id>.mp4`。链路是 档案字段 → Hermes 润色 2~3 句 → TTS（员工音色）→ ffmpeg 与 `videos/<id>.talk.mp4`（无则照片推镜）合成为 720×960 / H.264+AAC / `+faststart` 的 MP4。首次生成约 95–110 秒，之后复用；客户端只读 `profile` 的 `introVideo` / `introReady` / `introText` / `introDuration`，直连 URL 即可边下边播（支持 Range），无需转码或 HLS。

```json
{"name":"intro_video","arguments":{"id":"ceo"}}          // 单人生成（阻塞到完成）
{"name":"intro_video","arguments":{"wait":false}}        // 全员后台排队，立即返回
{"name":"intro_video","arguments":{"id":"ceo","force":true}}   // 档案改了要重做
```

**实时通话（`live_call`）**：服务端用 ffmpeg 把员工画面（talk 视频循环 / 照片）与实时写入命名管道的语音 PCM 混成 HLS 直播（1 秒分片），音视频同一条流复用 → 天然同步。客户端 `MediaPlayer` 播 `hls_url` 即可，**不需要 WebRTC / ExoPlayer**，延迟量级 2–4 秒。

| action | 关键参数 | 用途 |
| --- | --- | --- |
| `start` | `mode`（`video` 默认 / `audio` 纯语音）、`avatar_id`（默认 `secretary`＝OPC-001 小秘书） | 起流，返回 `session_id` / `hls_url` / `ready` / `mode` |
| `say` | `session_id` + `text` 或 `audio_base64` | 让员工说话（`text` 走 TTS 约十几秒才出声，传音频可立即入流） |
| `stop` | `session_id` | 挂断，播放列表补 `#EXT-X-ENDLIST` |
| `status` | `session_id` | 查看 `status` / `elapsed` / `speech_seconds` / `segments` |

- `mode=audio`：**纯语音通话**，ffmpeg 只推 AAC 音频轨（不出画面、不需要照片/视频素材），客户端只用 `MediaPlayer` 播同一个 `m3u8`；静音时持续推静音保持连接。首页「通话」按钮用这个模式。
- 默认播报人已从 `ceo` 改为 **`secretary`（OPC-001 小秘书）**；`start` 不传 `avatar_id` 时即小秘书。

- `voice_chat` 新增 `live_session` 参数：把「收到」和正式回复的语音直接混进正在进行的通话流；此时**客户端只播 `hls_url`，不要再播返回的 audio 块**（否则重复）。
- `m3u8` / `.ts` 响应头为 `Cache-Control: no-cache, no-store, must-revalidate`，客户端不要缓存。
- 并发上限 8（`ASR_LIVE_SESSIONS_MAX`），挂断后目录保留 900 秒（`ASR_LIVE_TTL`）供回看，随后自动清理。
- 完整客户端契约（时序、Android 代码、延迟与排查表）：[静态素材接入.md](静态素材接入.md) 第 10、11 章。

### 13.10 OPC-000 / OPC-001 的落地方式（2026-11-10）

| 工号 | id | 定位 | 在系统里怎么运作 |
| --- | --- | --- | --- |
| `OPC-000` | `boss` | 老板蒿色莫本人（真实档案） | `recordOnly=true`：档案只做记录，**不接任务、不派单、不主动执行**。`find_employee` 会跳过它，语音里点名 `@boss` / 「蒿色莫」不会被任何 agent 接管；只保留形象与音色供档案展示 |
| `OPC-001` | `secretary` | 小秘书（老板影子分身） | 独立 Hermes profile `secretary`，是**未点名语音指令的默认处理人与默认播报人**；定时巡检任务 `secretary-patrol`（每 30 分钟） |

- **默认路由**：`voice_chat` / `voice_job` 在语音里没有点名员工时，优先交给 `secretary`；只有该 profile 未就绪（`/opt/sefs/hermes/profiles/secretary` 不存在）时才回落到 `ceo` / root。返回里 `avatar_id` 同为 `secretary`。
- **形象复用**：`secretary` 档案带 `assetId: "boss"`，照片、`talk/idle` 视频、自我介绍画面都复用老板本人素材，所以同脸同声（`m10`）。
- **双重人格**：人格提示词在 `/opt/sefs/hermes/profiles/secretary/SOUL.md`——对内用老板视角判断、第一人称直给结论，对外以「老板代言人」身份沟通且不说明自己是分身。
- **主动行为**：巡检（每 30 分钟汇总进展、找停滞/卡门事项）、老板上线时汇报近期事项、重要任务提醒、需老板拍板的事项持续催办，都写在 SOUL.md 与巡检提示词里；巡检结果用「结论 → 进展 → 风险 → 待拍板」四段式，控制在 30 秒可读完。
- **老板上线钩子**：客户端在 App 启动 / 切回前台时调一次 `boss_briefing`，返回 `job_id` 与 `scene="boss_online"`，再用 `voice_job(job_id)` 取小秘书的汇报（`speech_text` 播报 + `detail_text` 详情 + 语音）。prompt 见 `server.py` 的 `BOSS_BRIEFING_PROMPT`，改汇报口径只改这一处。
- **邮件身份**：`config.yaml` 的 `mcp_servers.mail.env.MAIL_AGENT = secretary`，小秘书用独立信箱 `secretary@mail.conring.cn` 发信（账号在 `/etc/dovecot/users` + `/opt/sefs/obsidian/公司/.邮箱凭据.md`，收件目录 `/opt/sefs/mail/vmail/mail.conring.cn/secretary`；新建账号后 `systemctl reload dovecot` 即可，postfix 走 LMTP 不需要额外 map）。改 `MAIL_AGENT` 后必须等 gateway 新建会话重新拉起 MCP 进程（旧进程仍持老身份），可用 `grep "agent=" logs/mcp-stderr.log` 确认。
- **巡检邮件推送**（收件人固定 `boss@mail.conring.cn`，三条 cron 都在 profile `secretary` 下）：
  - `secretary-patrol`（每 30 分钟）：只有「出现新【急】待拍板 / 与上轮基线相比有实质变化 / 距上次发信超 12 小时仍有卡点」才发，主题 `[小秘书巡检] M-D H:MM <一句话结论>`；不发时只本地记「无实质变化」。同一事项不重复提醒。
  - `secretary-daily-0900` / `secretary-daily-1800`（`0 9 * * *` / `0 18 * * *`，Asia/Shanghai）：无条件发完整日报，主题 `[小秘书日报] YYYY-MM-DD 早报|晚报 <一句话结论>`。
  - 变化判定基线在 `/opt/sefs/hermes/profiles/secretary/runtime/patrol-last.md`（每轮覆盖写；删掉即视为「首次巡检，有变化」）。cron 本身没有邮件通道，发信是 prompt 里让 agent 调 `mail_send` 完成的，所以 `platform_toolsets.cli` 必须保留 `mcp-mail`。
- **邮件正文排版（HTML）**：`mail_send` 带 `format="report"` 时，服务端用 `render_report_html()` 把【】分节的纯文本渲染成卡片式 HTML——640px 白卡 + teal 渐变抬头（`[徽标] 标题` + 时间 + 发件人）、每节 3px 竖线标题、`【一句话结论】` 单独做成浅色高亮卡、`1.` / `-` 分别渲染成有序 / 无序列表、`【急】` 变红色徽标、`**粗体**` 与 `` `代码` `` 保留；同时自动附 `text/plain` 降级（`multipart/alternative`），手机、网页、纯文本客户端都能读。三条 cron 的发信参数都已带这个开关，版式要调只改 `render_report_html()` 一处；`html` 参数可传自定义正文覆盖（一般不用）。
- **工号来源**：工号只存在于 `employees.json` 的 `no` 字段，名册（`员工名册.md`）只有 Agent ID，改动时两处要一起改。

### 13.11 首页实时语音通话（纯语音，2026-11-10）

首页把「录音 → 发送 → 等整段返回」改成**持续实时语音通话**，形态与实时视频通话一致，只是不显示视频：

| 项 | 目标行为 | 服务端接口 |
| --- | --- | --- |
| 当前汇报人 | 默认 **OPC-001 小秘书**（`secretary`），可切换 | `live_call start` 不传 `avatar_id` 即小秘书；`voice_chat` 未点名也默认 `secretary` |
| 主按钮 | 「通话」→ 连接中 → **红色「挂断」**，随时可断 | `live_call start mode=audio` / `live_call stop` |
| 音频通道 | 一路 HLS 音频流不断开，静音时也在推流 | `mode=audio`，1 秒分片，延迟 2–4 秒 |
| 说话 | 麦克风连续采集，本地断句后逐句发送，不再整段录音 | `voice_chat(audio_base64, live_session=…)` |
| 回复 | 「收到」和正式回复直接混进同一路流，**立即播放** | `voice_chat` 的 `live_session`；客户端只播 `hls_url` |
| 文案 | 去掉「开始说话」「说完了，点击发送」「已发送，等待服务端返回」 | 见下 |

首页文案（直接替换）：

- 顶部：「当前汇报人」+「OPC-001 小秘书」，副标题「点击通话，和小秘书实时对话」
- 按钮：未连接 `通话`；连接中 `连接中…`；已连接 **红色** `挂断`
- 状态：`未连接` / `正在接通…` / `通话中 00:12 · 小秘书` / `通话已结束`
- 结果区：`实时转写`（用户话术）/ `小秘书`（回复文字），保留「查看文字」「重播语音」
- 权限提示：`通话需要麦克风权限，请允许后重试`

App 端逐文件改造清单、可直接粘贴的 `RealtimeVoiceCall.java`（AudioRecord 连续采集 + 能量断句 + HLS 播放 + 红色挂断态）见 [静态素材接入.md](静态素材接入.md) 第 12 章。服务端 `live_call` 的 `mode=audio` 已上线，其余接口未变。

实测（2026-11-10，本机 MCP 9102）：`live_call{action:start, mode=audio}` 不带问候语 **2.2 秒**返回 `ready=true`（带 `text` 问候会先等 TTS，约 10 秒，不建议）；运行 16.7 秒产出 17 个 1 秒分片（说话 2.33 秒，其余自动补静音），`hls_url=/conring/asr/assets/live/lv_cb8b654d/index.m3u8`，`avatar_id=secretary`，`stop` 后 `status=ended`。`mcp-asr-http.service` 已重启加载新代码。

**2026-11-10「显示接通但没声音」修复**（详见 [静态素材接入.md](静态素材接入.md) 12.7 / 12.8）：

| 问题 | 原因（实测） | 处理 |
| --- | --- | --- |
| 整通电话一句语音都没有 | 「收到」TTS 超过 8 秒同步超时后被丢弃，`speech_seconds=0`（本机 TTS RTF 1.7~6.4，2.26 秒语音要 6.1~16.2 秒） | 新增 `_live_ack_later`：超时后台继续合成，好了补混进流；`prewarm_acks` 按小秘书/ceo 的**音色+情绪**预热，缓存 key 与 `voice_chat` 一致 |
| 播放越来越滞后 | 纯语音列表是 `EVENT + hls_list_size 0`，播放器从第 0 片起播 | 改成 5 片滑动直播窗口（无 PLAYLIST-TYPE），新接入约 2~3 秒延迟 |
| 播放器拿到空列表 | `start` 只等 `index.m3u8` 出现 | 改为等到 2 个 `#EXTINF` 再返回 |
| 正式回复永远听不到 | agent 任务 154.3 秒 > 通话 90.8 秒 | `live.ended=true`，客户端改用 `voice_job` 取回结果自己播（agent 侧提速不在本次范围） |

改后实测：`start` 2.2 秒（2 个分片）、`voice_chat` 8.0 秒返回（ASR 约 5 秒 + ack 命中预热）、ack 2.25 秒混入 `seg_00016~00018`、8 秒后列表稳定 5 片。

---

## 14. 无语音 / 幻觉过滤（server 2.2.0，2026-11-10）

whisper 在静音、低电平噪声、极短录音上会吐训练语料里的片头片尾口径，中文最典型的是 **`字幕by索兰娅`**、`请不吝点赞 订阅 转发 打赏支持明镜与点点栏目`。这类句子与用户说的内容无关，但 `voice_chat` 会把它当成真实指令下发 agent（可发邮件、派单），属于**静默的错误执行**。

**本机复现（`small` / `zh`）**：

| 输入 | 输出 | 模型置信度 |
| --- | --- | --- |
| 4 s 低电平粉噪（a=0.0006） | `字幕by索兰娅` | `no_speech=0.934` `logprob=-0.687` `comp=0.65` |
| 4 s 粉噪（a=0.01） | `字幕by索兰娅` | `no_speech=0.868` `logprob=-0.657` `comp=0.65` |
| 1–10 s 纯静音 | `我看你不太好` × 19 段 | `no_speech=0.938` `logprob=-0.571` `comp=10.36` |

**三道闸门**（`server.py`，顺序执行）：

1. **静音闸**：整段最响 20 ms 帧 RMS < `ASR_MIN_RMS`（默认 0.002 ≈ -54 dBFS）直接返回空文本，不送模型，连推理时间都省了。
2. **段落闸**：逐段判 `no_speech_prob ≥ 0.8 且 avg_logprob ≤ -0.6`、或 `compression_ratio ≥ 3.0`（复读）→ 丢；已知识别口径黑名单（`字幕by…`、`不吝点赞`、`YYeTs`、`Amara.org` 等）→ 丢；软口径（`字幕翻译：…`、`字幕组出品`、`感谢观看`）只在**整段全匹配**时丢，避免误杀「把字幕翻译成英文」这类真指令。
3. **复读折叠**：连续相同文本只留头一遍。

命中后 `asr_transcribe` 返回 `text=""`（并带 `filtered` / `silent` 计数），`voice_chat` **不会创建 agent 任务**，而是回一句情绪化的「没听清，请再说一遍」（`status=need_repeat`，见第 15 章）。

**可调环境变量**：`ASR_MIN_RMS`、`ASR_NO_SPEECH_PROB`、`ASR_MIN_AVG_LOGPROB`、`ASR_MAX_COMPRESSION`。

**验收**（线上 9102 实测，改后重启 `mcp-asr-http`）：

| 用例 | 结果 |
| --- | --- |
| 15 条规则单测 + 2 组复读测试 | 全通过（含 5 条防误杀用例） |
| 低电平粉噪 → `asr_transcribe` | `text=""` `silent=true` |
| 4 s 粉噪 → `asr_transcribe` | `text=""` `filtered=1`（原为 `字幕by索兰娅`） |
| 4 s 粉噪 → `voice_chat` | `status=need_repeat` + 澄清语音，无 agent 任务（v2.2.0 起；旧版为 `isError:true`） |
| 真实语音（15.95 s TTS） | 4 段全部保留，`filtered=0`，未误杀 |

---

## 15. 识别不准 → 情绪化澄清（server 2.2.0，2026-11-10）

行为：**识别置信度低（或没识别出字）时不执行识别文本**，改让大模型以「当前播报人」的身份和情绪回一句「没听清，请再说一遍」，并把这句话合成语音返回；不再报 `执行失败：没有识别到语音内容`。

### 15.1 置信度怎么算

`asr_transcribe` 现在额外返回 `confidence`（0–1）、`avg_logprob`、`no_speech_prob`、`max_compression_ratio`、`low_confidence`：

```
confidence = e^(按时长加权的 avg_logprob) × (1 − 加权 no_speech_prob)   # 复读段再 ×0.5
low_confidence = confidence < ASR_LOW_CONFIDENCE(默认 0.5)  或者 text 为空
```

`avg_logprob` 是 whisper 段内 token 的平均对数概率，`e^x` 近似「平均每 token 的概率」，因此该值只是**启发式门槛**，不做概率校准。

本机标定（`small` / `zh`，同一句 5.55 s 中文 TTS 逐步加噪，9102 实测）：

| 音频 | 识别结果 | `confidence` | 是否澄清 |
| --- | --- | --- | --- |
| 原始 TTS | 明天上午十点开项目评审会…（正确） | **0.812** | 否 |
| 0.4 音量 + 粉噪 a=0.06 | 基本正确 | **0.701** | 否 |
| 0.25 音量 + 粉噪 a=0.30 | 明天上午10點開項的提手會向船艇經理圓圓打光燃發出來 | **0.428** | **是** |
| 0.10 音量 + 粉噪 a=0.45 + lowpass 1.2k | 谢谢观众朋友…订阅我的频道（完全错） | **0.018** | **是** |

### 15.2 情绪从哪来、怎么落到声音上

情绪取**播报对象**（被点名员工 > `avatar_id` > `profile` > `ceo`）档案里的 `emotion` 字段，客户端可用 `emotion` 参数覆盖；`auto` 表示按档案。

CosyVoice2 只认参考音色、不接受情绪指令，所以情绪落到两个可听维度：**语速**（TTS 原生 `speed`）+ **音高**（ffmpeg `asetrate` 变调后 `atempo` 还原时长，文件写 `/opt/sefs/opc-workbench/mcp-asr/output/`）。

| 档案 `emotion` | 语速 | 音高 | 听感 |
| --- | --- | --- | --- |
| `cheerful`（开朗/活泼/热情） | ×1.08 | ×1.05 | 轻快上扬 |
| `calm`（冷静/沉稳/温和） | ×0.96 | ×0.99 | 平稳略沉 |
| `serious`（严肃/严谨） | ×0.93 | ×0.97 | 放缓压低 |
| `neutral` / 未知值 | ×1.00 | ×1.00 | 不做处理 |

实测同一句话：`cheerful` 中位基频 **145.5 Hz**、`serious` **135.6 Hz**（比值 1.073，接近预设 1.05/0.97=1.082）；时长 3.46 s vs 3.28 s，说明变调后时长基本守恒、语速差异生效。

强度可用 `ASR_EMOTION_STRENGTH`（默认 1.0，0=关闭情绪化）整体缩放；`ASR_EMOTION_TTS=0` 完全关闭。最终汇报语音（`voice_job` 的 `result.audio`）同样按该员工情绪合成，`result.emotion` 会回传。

### 15.3 澄清语怎么生成

1. prompt 里给：员工姓名/职位/情绪基调、置信度、误识文本，并要求**只说一句「没听清，请再说一遍」，禁止打招呼/执行/解释**。
2. 调 `run_hermes`（默认 **root** profile；`ASR_CLARIFY_PROFILE=1` 才用员工自己的 profile）。实测员工 profile 是干活 agent，容易答非所问（回过一句「老板早！」），因此默认关闭。
3. 输出做两道校验：取第一行、去 Markdown/引号，且必须命中 `听清|没听|再说|重复|不确定|…` 关键词，否则视为无效。
4. 大模型超时（`ASR_CLARIFY_TIMEOUT`，默认 20 s）、报错或输出无效 → 用按情绪写死的模板句兜底，保证一定能回话：

| 情绪 | 模板兜底 |
| --- | --- |
| `cheerful` | 诶？老板，刚才有点吵，我没太听清，你再说一遍呗？ |
| `calm` / `neutral` | 老板，刚才没听清楚，麻烦再说一遍。 |
| `serious` | 老板，刚才没听清，请再说一遍。 |

关闭大模型（`ASR_CLARIFY_LLM=0`）则直接走模板，响应最快。

### 15.4 返回结构（`status=need_repeat`）

```json
{
  "job_id": "vj_455eb6_64078",
  "status": "need_repeat",
  "clarify": true,
  "question": "明天上午10點開項的提手會向船艇經理圓圓打光燃發出來",
  "emotion": "cheerful",
  "ack": {"text": "老板，这句我没听清，您再说一遍呗～",
          "audio_path": "/opt/sefs/opc-workbench/mcp-asr/output/ack_b031cb1b.wav",
          "sample_rate": 24000, "duration": 3.625,
          "segments": [{"index":0,"text":"老板，这句我没听清，您再说一遍呗～","start":0.0,"end":3.625}],
          "emotion": "cheerful"},
  "mode": "voice",
  "avatar": {"id": "cw_gou", "name": "叶秋", "role": "文案编写", "photo": {"…": "…"}, "videos": {"…": "…"}, "has_video": true},
  "delegate": null,
  "asr": {"model": "small", "language": "zh", "audio_seconds": 5.55,
          "confidence": 0.428, "avg_logprob": -0.63, "no_speech_prob": 0.196,
          "low_confidence": true, "silent": false,
          "hear": "明天上午10點開項的提手會向船艇經理圓圓打光燃發出來"},
  "next": "识别置信度 0.43 低于阈值 0.50，未执行指令；请老板重说一遍"
}
```

- `content[1]` 是这条**澄清语音**（`audio/wav`），客户端照常播放即可，不用等 `voice_job`。
- `status=need_repeat` 的 job 不会进入 `running/done`，`voice_job` 查询也返回 `need_repeat` 且不带音频（音频已在 `voice_chat` 里给过）。
- 正常路径（置信度够）仍然是 `status=accepted`，`asr` 里同样带 `confidence`，并新增顶层 `emotion`。

### 15.5 客户端处理建议

```java
if ("need_repeat".equals(status) || Boolean.TRUE.equals(json.optBoolean("clarify"))) {
    // 已收到澄清语音：播放 content[1]，提示「没听清，请再说一次」，不要展示「执行失败」
    // 可选：把 asr.confidence 打到日志，便于调阈值
} else { /* 走原 voice_job 轮询 */ }
```

### 15.6 参数与环境变量

| 名称 | 默认 | 说明 |
| --- | --- | --- |
| `ASR_LOW_CONFIDENCE` | 0.5 | 置信度阈值，低于即澄清 |
| `ASR_CLARIFY_LLM` | 1 | 是否用大模型生成澄清语；0=只用模板 |
| `ASR_CLARIFY_TIMEOUT` | 20 | 等大模型秒数 |
| `ASR_CLARIFY_TTS_TIMEOUT` | 30 | 等澄清语音合成秒数（这是本轮唯一回复，给得比「收到」宽） |
| `ASR_CLARIFY_PROFILE` | 0 | 是否用员工 Hermes profile 生成澄清语 |
| `ASR_EMOTION_TTS` / `ASR_EMOTION_STRENGTH` | 1 / 1.0 | 情绪化开关与强度 |
| `ASR_EMOTION_OUTPUT` / `ASR_FFMPEG` | `/opt/sefs/opc-workbench/mcp-asr/output` / 自动探测 | 变调输出目录与 ffmpeg 路径 |

`voice_chat` 侧另有 `emotion`、`clarify`、`clarify_timeout_sec` 三个参数可逐次覆盖（见 13.3）。

### 15.7 已知边界

- 低置信澄清会多花一次大模型调用：实测端到端约 **20 s**（ASR 3–5 s + 大模型约 8–11 s + TTS 约 5 s）。要更快就 `ASR_CLARIFY_LLM=0` 走模板。
- 阈值 0.5 只在**本机 small 模型 + 中文 TTS 加噪**样本上标定，换 `medium`/其它语言或真实手机录音后建议重新取样调参。
- 情绪只改语速与音高，不会改措辞与音色；同音色同情绪的模板句会被缓存复用。
