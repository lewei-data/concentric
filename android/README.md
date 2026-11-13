# 同心圆（Android App · OPC 数字员工汇报）

只做一件事：**按下按钮说话 → 通过 MCP 把语音发给同心圆 agent 服务的 `voice_chat` → 显示文字、自动语音播报、返回的文件可以打开查看。**

**已生成 APK**（2026-11-10，同心圆 **0.3.2 / versionCode 5**，一次构建同时产出两个带版本号的包，详见第 17 节）：

| 文件                                     | 用途                                   |
| -------------------------------------- | ------------------------------------ |
| `dist\voice-mcp-v0.3.2-b5-debug.apk`   | 调试版，含 `application-debuggable`，开发排查用 |
| `dist\voice-mcp-v0.3.2-b5-release.apk` | 发布版，已签名、可直接安装，日常使用 / 内部分发用           |

本轮修复：首页名称统一为「同心圆」、默认汇报员改为 CEO、档案页试听改为真实音频驱动口型、汇报画面大屏与小窗统一使用同一位员工的服务端真人素材（详见第 18 节）。

应用名为「同心圆」，默认端点为正式域名；云端 CI 见第 5 节，界面见第 11 节，数字员工见第 12 节，安全区与图标见第 13 节，异步任务 / 监听 / 历史见第 14 节，v2.1.0 适配见第 15 节，素材修复见第 16 节，**双版本出包规则见第 17 节**，**本轮核查见第 18 节**。

工程目录：`E:\src2026\voice-mcp-android`，标准 Android Gradle 工程，Java + 纯 Android framework，**零第三方依赖**（无 AndroidX / OkHttp / Kotlin）。

---

## 1. 正式服务端配置（已切换并实测）

**App 默认端点：`https://ai.boraydata.com/conring/asr/mcp`**

| 服务              | 内网应急端点（需 VPN）                                | 正式域名端点                                          | serverInfo |
| --------------- | -------------------------------------------- | ----------------------------------------------- | ---------- |
| mail            | `http://192.168.31.208/conring/mail/ceo/mcp` | `https://ai.boraydata.com/conring/mail/ceo/mcp` | mail 1.0.0 |
| tts             | `http://192.168.31.208/conring/tts/mcp`      | `https://ai.boraydata.com/conring/tts/mcp`      | tts 1.0.0  |
| **asr（App 调用）** | `http://192.168.31.208/conring/asr/mcp`      | **`https://ai.boraydata.com/conring/asr/mcp`**  | asr 1.0.0  |

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

| 验证项                                  | 结果                                                                                                                                                                                                               |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DNS                                  | `ai.boraydata.com` → `49.7.58.10`；HTTP 根路径 301 → HTTPS                                                                                                                                                           |
| TLS 证书                               | Let's Encrypt，`CN=ai.boraydata.com`，有效期至 2026-12-13，Android 系统信任                                                                                                                                                 |
| `/conring/{mail/ceo,tts,asr}/mcp` 握手 | 三条路径全部 `200` + `mcp-session-id` + serverInfo，不带 Token 也通过                                                                                                                                                        |
| 其它路径                                 | `/agent/mcp`、`/hermes/mcp`、`/asr/mcp` 等均为 404（只有 `/conring/*` 有效）                                                                                                                                                |
| `/mcp`（对照）                           | 未带 Token → 401（提示去 `/token` 申请）；带 Token → `sefs-mcp 2.1.0`，59 个工具，无 `voice_chat`                                                                                                                                 |
| `tools/list`（asr）                    | `asr_transcribe`、`voice_chat`；后者 schema 与 App 一致：`audio_base64` / `language` / `model` / `voice` / `profile` / `timeout_sec`                                                                                     |
| **闭环调用**                             | `/conring/tts/mcp` 合成一句问候 → 回灌 `/conring/asr/mcp` 的 `voice_chat`：`isError: false`，text 块含 `question` / `answer` / `audio_path` / `duration` / `asr`，audio 块 `mimeType=audio/wav`、base64 139,624 字符；**耗时 23.4 秒** |
| **大请求体**                             | 60 秒静音 WAV（1.92MB → base64 2.56MB，请求体 2.44MB）调 `asr_transcribe` → `200`，15.2 秒，云端反代未返回 413                                                                                                                       |
| 内网回退                                 | `http://192.168.31.208/conring/asr/mcp` 直连 `200`；`https://192.168.31.208/...` 证书 CN 不匹配（内网请用 http，或用域名）                                                                                                          |

## 3. 请求与返回样例

请求（`tools/call`）：

```json
{"name":"voice_chat","arguments":{"audio_base64":"<16kHz WAV 的 base64>","language":"zh","model":"small","timeout_sec":300}}
```

成功返回的 `result.content`：

```json
[
  {"type":"text","text":"{\n  \"question\": \"识别出的指令\",\n  \"answer\": \"Hermes 的回复\",\n  \"audio_path\": \"/opt/sefs/mcp-tts/output/voice_xxx.wav\",\n  \"duration\": 2.18,\n  \"asr\": {\"model\": \"small\", \"language\": \"zh\", \"audio_seconds\": 1.87}\n}"},
  {"type":"audio","data":"<base64 wav>","mimeType":"audio/wav"}
]
```

无语音时返回 `isError: true` + text 块「执行失败：没有识别到语音内容」，App 会按错误状态显示。

## 4. App 使用流程

1. 首次启动授予录音权限。
2. 点「开始说话」开始录音，再点「说完了，点击发送」结束（最长 60 秒）。
3. 等待服务端返回（视 agent 执行复杂度，实测 23 秒 ~ 3.5 分钟），顶部显示状态。
4. 完成后：结果区显示「识别到的指令 / 回复内容」；自动播放服务端返回的 WAV；下方列出所有返回文件，点任意一项用系统应用打开。
5. 「查看文字」把当前结果存成 txt 并打开；「重播语音」重新播报；结果区文字可长按复制、链接可直接点开。
6. 设置页可改端点、测试连接、查看服务端工具清单、开启演示模式（不联网，本地回显 + 生成示例附件，便于先验证 UI 和文件打开）。

> 每个 MCP 内容块都会落盘一份：`text` 块 → `.txt`，`audio` 块 → `.wav`，`resource` 块 → 按内容存 txt/二进制。文件通过自定义 `ResultFileProvider` 以 `content://` 暴露，不依赖 AndroidX。

## 5. 云端生成 APK（CI，免本地 SDK）

工程已内置 `.github/workflows/build-apk.yml`：推送到 GitHub 仓库（`main` / `master`）或在 Actions 页手动触发 `workflow_dispatch`，即自动构建 **debug + release 两个带版本号的 APK**，在 **Actions → Artifacts → `conring-apks`** 里下载。

流水线步骤：`actions/checkout` → `setup-java` (Temurin 17) → `android-actions/setup-android` → `sdkmanager platforms;android-35 / build-tools;35.0.0` → `keytool` 创建回退签名证书 → `./gradlew collectApks --no-daemon` → 上传 `dist/*.apk`。

操作：

1. 工程目录执行 `git init && git add . && git commit -m "init"`。
2. GitHub 建空仓库，`git remote add origin <url> && git push -u origin main`。
3. Actions 页等构建完成，下载 artifact 里的 `voice-mcp-v<版本>-b<构建号>-debug.apk` 与 `...-release.apk`。

其它可选云端方案（把上面几步照搬即可）：

| 平台                   | 配置文件                                            | 说明              |
| -------------------- | ----------------------------------------------- | --------------- |
| GitHub Actions       | `.github/workflows/build-apk.yml`（已提供）          | 公共仓库免费额度        |
| CNB / CODING / 腾讯云构建 | `.cnb.yml` / Jenkinsfile                        | 企业内网代码托管，适合信创环境 |
| GitLab CI            | `.gitlab-ci.yml`，镜像 `gradle:8.14-jdk17` + 装 SDK | 自建 Runner       |
| Codemagic / Bitrise  | 图形化配置                                           | 适合带分发与真机测试      |

> 云端 CI 都需要先把代码推到远端仓库（外部动作，需你授权），所以本次没有擅自创建仓库；实际 APK 改为直接从云端下载 SDK 后在本机构建（见第 6 节）。

## 6. 本机构建

需要：JDK 17（或 17/21，Android Studio 自带 JBR 即可）、Android SDK Platform 35 + Build Tools、网络（首次下载 AGP）。

- Android Studio：`File > Open` 选择 `E:\src2026\voice-mcp-android`，等待 Gradle Sync，然后 Run。
- **一键出双包（推荐）**：`gradlew.bat collectApks --no-daemon` → 构建 debug + release 并复制到 `dist\`，文件名自动带版本号。
- 只要某一个包：`gradlew.bat assembleDebug` / `gradlew.bat assembleRelease` → 产物同样带版本号，位于 `app\build\outputs\apk\<type>\`。
- Gradle Wrapper 8.14.3 与 `gradle-wrapper.jar` 已随工程提供；AGP 8.10.1，`compileSdk 35 / minSdk 26`。

**本次（同心圆 0.3.2 / 名称统一 + 默认 CEO + 试听对嘴）构建**：Temurin JDK 17 + 云端下载的 `platforms;android-35` / `build-tools;35.0.0`，`gradlew.bat collectApks --no-daemon` → **BUILD SUCCESSFUL in 1m31s**（一次编译出 debug + release；0.3.1 双包 1m44s、素材修复版单 debug 1m4s、0.3.0 版 36s、首次全量 2m13s）。

交付文件（两包均为 `com.boray.voicemcp`、versionCode 5 / v0.3.2、targetSdk 35、`apksigner verify` 通过 v2 签名）：

| 文件                                     | 字节      | SHA256                                                             |
| -------------------------------------- | ------- | ------------------------------------------------------------------ |
| `dist\voice-mcp-v0.3.2-b5-debug.apk`   | 353,549 | `681c08aabccbc34b9a791b5200395571c26d23cc972554a83da0d5cb9302015b` |
| `dist\voice-mcp-v0.3.2-b5-release.apk` | 328,083 | `b9a6b0dc510cfc46802555e7a37ac0ae03be70895e1aeac4a7773b6318b4339e` |

图标预览：`dist\ic_launcher_preview.png`。旧版 `v0.3.1-b4` 双包已从 `dist/` 移除。

安装：`adb install -r dist\voice-mcp-v0.3.2-b5-release.apk`（调试时换成 `-debug.apk`），或把文件传到手机点开安装（需允许未知来源）。两包共用同一 `applicationId` 且当前共用同一证书，可直接互相覆盖安装。

> 已从 APK 的 dex 中核实：默认 URL 为 `https://ai.boraydata.com/conring/asr/mcp`，旧的 `http://192.168.31.206:9102/mcp` 已不存在。本机 SDK 位于临时目录 `%TEMP%\voice-mcp-sdk`，`local.properties` 已指向它；系统清理临时目录后重新构建需再装一次 SDK，或直接走第 5 节云端 CI。

| 校验                                                             | 结果                                                                                                                                                                                                                                                                                                                                         |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 6 个 XML（Manifest / 2 布局 / values / network config）             | 全部 well-formed                                                                                                                                                                                                                                                                                                                             |
| `R.id` 引用（26 个）                                                | 与布局声明完全一致，无缺失                                                                                                                                                                                                                                                                                                                              |
| Java 语法（10 个源码文件）                                              | 解析器全部通过                                                                                                                                                                                                                                                                                                                                    |
| `javac` + Android 15 framework jar 类型编译                        | 0 错误（仅 1 条 deprecated API 提示）                                                                                                                                                                                                                                                                                                              |
| Gradle `assembleDebug`（JDK 17 + AGP 8.10.1）                    | BUILD SUCCESSFUL（首次 2m13s、域名版 28s、界面版 32s、数字员工版 49s）                                                                                                                                                                                                                                                                                       |
| 界面版静态校验                                                        | 44 个 XML well-formed；35 个 `R.id` 全部匹配；33 个 drawable / 30 个 color 引用无缺失                                                                                                                                                                                                                                                                     |
| 数字员工版静态校验                                                      | 58 个 XML well-formed；74 个 `R.id` 全部匹配；42 个 drawable / 30 个 color / 7 个 layout 引用无缺失；16 个 Java 文件语法通过                                                                                                                                                                                                                                       |
| `apksigner verify` / `aapt2 dump badging`                      | Verifies（v2 签名）；包名、权限、targetSdk 正确；4 份员工档案已打包进 `assets/employees.json`                                                                                                                                                                                                                                                                     |
| 域名 MCP 链路（握手 / tools/list / tools/call / voice_chat 闭环 / 大请求体） | 全部实测通过                                                                                                                                                                                                                                                                                                                                     |
| 安全区 + 图标版                                                      | `aapt dump badging` 显示 icon 指向 `mipmap-anydpi-v33/ic_launcher.xml`；`aapt2 dump xmltree` 确认 background/foreground/monochrome 三层；APK 内含 4 份矢量图标 + 10 张各密度 PNG；`WindowInsetsHelper` 已编入 `classes2.dex`，5 个 Activity 全部调用                                                                                                                      |
| 同心圆 0.2.0                                                      | `aapt dump badging`：`application-label='同心圆'`、versionCode 2 / 0.2.0、4 项新权限、`other-services`/`other-receivers` 已注册；`apksigner verify --verbose` Verifies (v2)；`tools/VerifyCore.java` 纯 JVM 校验核心文本过滤 `PASS=true`                                                                                                                            |
| 同心圆 0.3.0（v2.1.0 适配）                                           | `BUILD SUCCESSFUL in 36s`；`aapt dump badging` → versionCode 3 / 0.3.0 / label 同心圆；`aapt dump resources` 确认 11 个新增控件 ID；`apksigner verify --verbose` Verifies (v2)；`tools/verify_v21_contract.py` 离线契约校验 **24/24 PASS**（含「源码不含其它素材 URL」断言）；`AssetUrls` / `EmployeeStore` / `ReportActivity` / `HistoryDb` 已进 dex                            |
| 同心圆 0.3.1（素材路径修复）                                              | `BUILD SUCCESSFUL in 1m4s`；`aapt dump badging` → versionCode 4 / 0.3.1 / label 同心圆；`apksigner verify --verbose` Verifies (v2)；APK 内 `assets/employees.json` 为 18 人新路径；`tools/verify_v21_contract.py` **33/33 PASS**；`tools/VerifyAssets.java` 纯 JVM URL 解析 **16/16 PASS**；`docs/v22_assets_check.json` 18 人 × 4 类素材 + Range 共 **90/90 可达** |
| 同心圆 0.3.2（名称 / CEO / 对嘴）                                       | `collectApks` **BUILD SUCCESSFUL in 1m31s**；双包 versionCode 5 / 0.3.2、`application-label='同心圆'`、v2 Verifies；`tools/verify_v21_contract.py` **46/46 PASS**；`tools/VerifyPreview.java` 真实试听 WAV **8/8 PASS**（8.45s / 211 帧 / 86% 发声帧 / 5 种口型）；`tools/VerifyAssets.java` **16/16 PASS**                                                        |

## 7. 端点配置与切换

**正式端点已写进代码默认值**（`McpConfig.DEFAULT_URL`），也是设置页的初始值：`https://ai.boraydata.com/conring/asr/mcp`。

- 想临时切回内网：设置页把 URL 改成 `http://192.168.31.208/conring/asr/mcp`，保存即生效（需 VPN）。代码里也留了 `McpConfig.LEGACY_URL` 常量备用。
- Bearer Token **留空**：`/conring/*` 不需要鉴权。只有切到 `https://ai.boraydata.com/mcp`（SEFS FS/DB 服务）才需要去 `/token` 申请 Token，但那套服务没有 `voice_chat`。
- 若以后域名或路径变化，改设置页「MCP 端点 URL」一处即可，无需重新编译；改代码默认值则改 `McpConfig.DEFAULT_URL`。
- `res/xml/network_security_config.xml`：默认 `cleartextTrafficPermitted="false"` 强制 HTTPS，仅对 `192.168.31.206` / `192.168.31.208` 内网应急端点放行 HTTP 明文。
- 工具名、音频参数名、协议版本、音色、Hermes profile 也都在设置页，换到别的 MCP server 时无需改代码即可试配。

## 8. 设置项

| 设置              | 默认值                                        | 说明                                                |
| --------------- | ------------------------------------------ | ------------------------------------------------- |
| MCP 端点 URL      | `https://ai.boraydata.com/conring/asr/mcp` | 正式域名；内网回退 `http://192.168.31.208/conring/asr/mcp` |
| 工具名             | `voice_chat`                               | 可用「查看工具」从服务端发现后点选                                 |
| 音频参数名           | `audio_base64`                             | 上传 WAV base64 的参数键                                |
| 识别语言 / 模型       | `zh` / `small`                             | 传给 whisper                                        |
| voice / profile | 空                                          | 音色 id、Hermes profile，留空走服务端默认                     |
| Bearer Token    | 空                                          | 空则不发 Authorization 头；`/conring/*` 无需 Token        |
| 协议版本            | `2025-03-26`                               | 与服务端实测一致                                          |
| 请求超时            | 600 秒                                      | 握手/工具发现固定 15 秒，仅工具调用使用长超时                         |
| 自动播报            | 开                                          | 关掉后可用「重播语音」手动播                                    |
| 播报时显示数字员工汇报画面   | 开                                          | 关掉后退回原来的纯语音播报（见第 12 节）                            |
| 优先使用仿真人视频形象素材   | 开                                          | 档案里配置了 mp4 时用它做汇报画面底片                             |
| 演示模式            | 关                                          | 不联网的本地回显                                          |


## 9. 排障

| 现象                         | 原因 / 处理                                                                |
| -------------------------- | ---------------------------------------------------------------------- |
| `Unknown tool: voice_chat` | URL 少了 `/conring/asr/mcp` 前缀，或误填成 FS/DB 的 `/mcp`                       |
| `HTTP 401` + 「未携带 Token」   | 填到 `https://ai.boraydata.com/mcp`（SEFS）了；同心圆服务不需要 Token                |
| `HTTP 404`                 | 路径拼错，只允许 `/conring/mail/ceo/mcp`、`/conring/tts/mcp`、`/conring/asr/mcp` |
| 证书错误                       | 用域名 `https://ai.boraydata.com`；不要对 `192.168.31.208` 用 HTTPS（证书 CN 不匹配） |
| 连接超时                       | 域名走公网无需 VPN；若切回内网 `192.168.31.208` 则必须先连 VPN                           |
| 一直「已发送，等待服务端返回」            | `voice_chat` 正常要几十秒到几分钟；可在设置页调大超时                                      |
| 「执行失败：没有识别到语音内容」           | 录音太短或环境太安静，靠近麦克风重说一次                                                   |
| 没有声音                       | 优先播服务端音频；若音频缺失才用系统 TTS，部分国产 ROM 无中文 TTS 引擎                             |
| 打开附件提示无应用                  | 设备缺少能打开该 MIME 的应用；txt/wav 通常都有                                         |

## 10. 已知边界

- `voice_chat` 会让 Hermes 以完整 agent 权限执行语音指令，可能触发外部动作（例如 `mail_send`）；生产使用前建议加白名单或二次确认。
- 服务端会在 `/opt/sefs/mcp-tts/output/` 生成 wav/srt/segments.json，那些是**服务器本地路径**，App 不直接访问；App 打开的是从内容块落盘的副本。
- 单次响应音频、录音 base64 均可达数 MB，弱网下耗时明显；当前云端反代已验证 2.44MB 请求体可通，后续可改为返回 URL 而非 base64。
- 这是 **debug 签名** APK，仅用于自测，不能上架；要发布需配正式签名。

## 11. 界面设计（参考 LocalSend，2026-11-10 美化）

LocalSend 官方主题是 **Material 3 + `Colors.teal` 种子色**（`app/lib/config/theme.dart`，支持 system / localsend / oled / yaru 配色），界面语言为「大圆角卡片 + 图标化 BigButton + 状态徽章 + 三段导航」。本工程按这套语言重做，**仍是零第三方依赖的纯 framework 实现**：

| 元素   | 实现方式                                                                                                                                              |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| 双主题  | `values/colors.xml` + `values-night/colors.xml`（teal 主色，跟随系统深色模式自动切换）；`Theme.Material.Light.NoActionBar` / `Theme.Material.NoActionBar`，状态栏与导航栏同色 |
| 卡片   | `bg_card.xml`（24dp 圆角 + 细描边）、`bg_hero.xml`（渐变 hero 区）、`bg_card_soft.xml`（分组底）、`bg_input.xml`（输入框聚焦态描边）                                            |
| 核心动作 | 148dp 圆形麦克风 `bg_mic.xml`（selector：默认渐变 / 录音中红 / 禁用灰）+ 190dp 呼吸光环 + 176dp 进度环 + 按压缩放动画                                                             |
| 状态反馈 | 连接卡内 9dp 状态点（正常 teal / 进行中琥珀 / 错误红）+ 顶部「已就绪 / 演示模式」徽章 + 结果文字淡入                                                                                    |
| 快捷操作 | 三个 BigButton（重播语音 / 查看文字 / 连接设置），矢量图标 + ripple                                                                                                    |
| 设置页  | 分组卡片（连接 / 工具 / 识别与播报 / 行为）、胶囊主按钮、描边次按钮、`Switch` + ColorStateList 主题色、顶部返回按钮                                                                       |
| 附件列表 | `item_attachment.xml` 卡片行：类型图标 + 文件名 + 大小/类型 + 箭头，点击用系统应用打开                                                                                       |
| 图标   | 10 个 `<vector>` drawable，零 PNG、零图标库；33 个 drawable 全部为 XML                                                                                         |

新增资源：`res/values-night/`、`res/color/`、`res/drawable/*`、`res/layout/item_attachment.xml`。  
Java 侧适配：`MainActivity` 改为 View 型按钮 + 状态点/脉冲/进度环/淡入 + 附件卡片渲染；`SettingsActivity` 增加返回按钮并把 CheckBox 换成 Switch。

> 功能与配置完全不变：`voice_chat` 调用、结果解析、语音播报、文件打开、演示模式、域名端点与全部设置项保持原样。

## 12. OPC 数字员工档案与员工汇报画面（2026-11-10 新增）

### 12.1 功能组成

| 能力           | 落地位置                                                             | 说明                                                        |
| ------------ | ---------------------------------------------------------------- | --------------------------------------------------------- |
| 数字员工档案       | `assets/employees.json` + `Employee.java` / `EmployeeStore.java` | 内置 4 位员工，各自独立的工号、部门、职位、简介、技能、口头禅、音色、外观参数与人设 profile       |
| 档案界面         | `EmployeeListActivity` / `EmployeeDetailActivity`                | 列表卡片带「汇报员」徽章；详情展示形象照、仿真人视频素材状态、完整档案、设为汇报员、试听汇报            |
| 汇报画面         | `ReportActivity`                                                 | 结果返回后自动全屏弹出：员工名牌 + 形象舞台 + 分句字幕 + 进度条 + 播控（重播 / 切换员工 / 结束） |
| 口型 / 表情 / 动作 | `LipSync.java` + `DigitalHumanView.java`                         | 服务端 WAV 的 PCM 振幅包络实时驱动嘴型；文本情感驱动表情；呼吸、点头、摆头、眨眼、手势随播报节奏变化   |

主界面新增「当前汇报员」卡片，点击进档案切换。切换后 `voice_chat` 请求里的 `profile` 参数会自动带上该员工标识，让 Hermes 按对应人设作答（设置页手工填的 profile 优先）。

### 12.2 档案字段

`app/src/main/assets/employees.json` 是默认档案；若 `filesDir/employees.json` 存在则优先读它（可用 `adb push` 导入真实档案，不用重新编译）。

| 字段                                                                   | 含义                                                                                                              |
| -------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `id` / `no` / `name` / `dept` / `title` / `motto` / `bio` / `skills` | 组织信息，列表与详情页展示                                                                                                   |
| `photo`                                                              | 照片素材；**当前 18 人均为服务端同源路径** `/conring/asr/assets/avatars/xxx.png`（见第 16 节），早期 4 人示例为 `drawable://photo_lin` 矢量占位图 |
| `videoIdle` / `videoTalk`                                            | 仿真人视频形象（待机 / 汇报）；**当前 18 人均为** `/conring/asr/assets/videos/xxx.idle.mp4` / `.talk.mp4`                          |
| `faceUrl`                                                            | 面部特写图 `/conring/asr/assets/faces/xxx.png`（口型贴图预留）                                                               |
| `profile`                                                            | Hermes 人设标识；客户端默认留空，由服务端按档案 / 点名派单决定                                                                            |
| `voice`                                                              | 专属音色 id（`m01`–`m10` / `f01`–`f08`），服务端按被点名员工自动选用                                                                |
| `accent` / `skin` / `hair` / `suit` / `shirt`                        | 内置 Canvas 形象的配色，保证每人长相不同                                                                                        |
| `hairStyle` / `glasses` / `emotion`                                  | 发型（`short`/`long`/`bun`/`curly`）、眼镜、情绪基线（`calm`/`cheerful`/`serious`）                                           |

素材字段支持：服务端同源绝对路径（以 `/` 开头，如 `/conring/asr/assets/avatars/ceo.png`）、绝对 `https://...`、`drawable://资源名`、`assets://employees/xxx.jpg`、`file:///sdcard/...`。留空则由内置 Canvas 数字人按外观参数实时绘制。以 `/` 开头的路径由 `AssetUrls` 直接拼 MCP 端点 origin，**不叠加 `asset_url_base`**（服务端已把前缀写进字段）。

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

- 早期示例档案的 `videoIdle` / `videoTalk` 留空、4 张形象照为矢量占位图；**2026-11-10 服务端已提供 18 人真实素材并公网放通**（第 16 节），离线快照与 `profile` 缓存均已换成真实地址，汇报画面现在直接切成「视频底片 + 角落驱动小窗」。矢量占位图仍保留在 APK 中作为加载失败时的兜底。
- 加载 / 循环静音播放 / Range 拖动 / 失败逐级回退（视频 → 照片 → 内置数字人）逻辑不变。
- 口型基于**音量包络 + 元音轮换**，不是音素/viseme 识别；要做到逐音素精确，需服务端返回 phoneme/viseme 时间轴或实时数字人视频流，客户端已预留 `setFrame()` 接口可直接接。
- 视频形象是静音循环底片，其自带口型不会与播报音频同步；真正同步的口型由角落里的内置驱动数字人呈现。
- 设置页可关闭「汇报画面」回到原来纯语音播报，或关闭「优先使用视频素材」只看内置驱动形象。

---

## 13. 系统栏安全区 + 同心圆启动图标（2026-11-10 新增）

### 13.1 顶部 / 底部不再被系统栏遮挡

**根因**：本工程 `targetSdk 35`，Android 15（API 35）对这类应用**强制 edge-to-edge** —— 状态栏与导航栏变透明、内容直接绘制到系统栏下方，于是顶部标题压到状态栏、底部「结束汇报」等按钮被手势导航条盖住。

| 做法          | 文件                                            | 说明                                                                                               |
| ----------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 安全区 padding | `WindowInsetsHelper.java`                     | 给内容根视图 `android.R.id.content` 加上「系统栏 + 刘海」insets，窗口背景色自然露出填满系统栏区域                                |
| 系统栏外观       | 同上                                            | 状态栏 / 导航栏设成窗口色；浅色主题用深色图标、深色主题用浅色图标；关闭系统自动加的导航栏灰度遮罩                                               |
| 主题声明        | `values/styles.xml`、`values-night/styles.xml` | 不透明系统栏 + `windowLightStatusBar` / `windowLightNavigationBar` + `windowDrawsSystemBarBackgrounds` |
| 全部页面生效      | 5 个 Activity 的 `onCreate`                     | `setContentView()` 之后统一调用 `WindowInsetsHelper.apply(this)`                                       |
| 可见留白        | 5 个布局                                         | 顶部 padding 16→20dp / 22→26dp，底部 28→36dp / 18→24dp，配合安全区形成呼吸空间                                    |

**版本兼容**（仍是纯 framework，零 AndroidX）：

| 系统                | 取值方式                                                                  | 说明                             |
| ----------------- | --------------------------------------------------------------------- | ------------------------------ |
| API 30+           | `getInsetsIgnoringVisibility(systemBars \| displayCutout)`            | 忽略可见性的稳定值，键盘弹出 / 系统栏临时隐藏时页面不跳动 |
| API 26~29         | `stableInsets` + 顶部 `systemWindowInsets` + `DisplayCutout.safeInset*` | 避开键盘高度，同时覆盖刘海                  |
| 低版本非 edge-to-edge | 系统已自行避让，分发到内容根的 insets 为 0                                            | 不会重复叠加 padding                 |

### 13.2 同心圆主题图标

设计语义：**三层同心圆 = 语音波纹 / 同心圆（Conring）Agent**，由外到内逐层加粗提亮；中心白色圆盘里的 teal 声波条 = 语音指令入口；右上一颗小圆点 = 在线 Agent 节点。所有图形半径 ≤ 33dp，落在 adaptive icon 安全区内，不会被各家启动器遮罩裁掉。

| 文件                                        | 作用                                                        |
| ----------------------------------------- | --------------------------------------------------------- |
| `drawable/ic_launcher_background.xml`     | teal 渐变 + 两圈极淡同心圆底纹                                       |
| `drawable/ic_launcher_foreground.xml`     | 三层同心环 + 中心声波 + Agent 节点（矢量）                               |
| `drawable/ic_launcher_monochrome.xml`     | Android 13+ 主题图标单色层，跟随壁纸着色                                |
| `mipmap-anydpi-v26/ic_launcher*.xml`      | Adaptive icon（API 26+，含 `roundIcon`）                      |
| `mipmap-anydpi-v33/ic_launcher*.xml`      | 同上，额外带 `<monochrome>` 单色层                                 |
| `mipmap-{mdpi..xxxhdpi}/ic_launcher*.png` | 48/72/96/144/192px 的 PNG 兜底（方形 + 圆形各 5 张），供旧启动器、设置页与第三方取用 |
| `drawable/ic_conring.xml`                 | 应用内顶部 logo，与启动图标同一视觉语言（替换原来的麦克风）                          |
| `AndroidManifest.xml`                     | `android:icon` / `android:roundIcon` 指向新图标                |

PNG 由 `tools/generate_launcher_icons.py`（构建期脚本，依赖 Pillow，App 运行期零依赖）生成，几何参数与矢量版一致，改设计时重新跑一次即可同步矢量以外的位图。`dist/ic_launcher_preview.png` 是方形 / 圆形 / 主题图标三种效果的预览。

### 13.3 本次校验

- `gradlew.bat assembleDebug --no-daemon` → **BUILD SUCCESSFUL in 1m17s**（仅一条 deprecated API 提示，来自 API 26~29 兼容分支）。
- `aapt dump badging`：包名 / 权限 / targetSdk 35 正确，`application icon='res/mipmap-anydpi-v33/ic_launcher.xml'`。
- `aapt2 dump xmltree`：确认 `adaptive-icon` 的 `background` / `foreground` / `monochrome` 三层完整。
- APK 内含 4 份矢量图标 XML + 10 张密度 PNG；`apksigner verify` → Verifies（v2，Android Debug 证书）。
- `WindowInsetsHelper` 已编入 `classes2.dex`，5 个 Activity 均调用 `apply(this)`。

> 真机效果需在 Android 15+ 设备上目测：状态栏图标不与标题重叠、底部按钮不被手势条压住；浅色 / 深色模式、三键导航 / 手势导航、横屏刘海均按 insets 自适应。



---

## 14. 同心圆 0.2.0：保持监听 / 精简展示 / 长期历史（2026-11-10 新增）

### 14.1 服务端协议实测（本次最重要的发现）

用 `tools/probe_push.py` / `probe_schema.py` / `probe_voice_flow.py` / `probe_mail.py` 实测了 `ai.boraydata.com` 的三个 `/conring/*` 端点（协议 `2025-03-26`，无需 Token）：

| 端点                      | 工具                                | 关键结论                                                                                                                                                                                                 |
| ----------------------- | --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/conring/asr/mcp`      | `voice_chat`                      | **已改为异步任务**：默认 `wait=false`，立刻回 `text(JSON: job_id / status=accepted / question / ack.text / mode / avatar)` + `audio(收到语音)`。可选 `wait=true` 兼容同步；`callback_url` 可让服务端完成后回调                           |
| 同上                      | `voice_job`                       | `job_id` + `wait_sec`（长轮询，≤30 秒）+ `include_audio`。`status=done` 时返回 `result.speech_text`（简要播报）、`result.detail_text`（详细正文）、`segments` 句级时间轴、`result.render.avatar.videos.talk.url`，并附 `audio/wav` 内容块 |
| 同上                      | `profile` / `voice_avatars`       | 员工档案与数字人播报素材清单                                                                                                                                                                                       |
| `/conring/tts/mcp`      | `tts_synthesize`                  | 合成播报 WAV（本次用它造语音输入做闭环实测）                                                                                                                                                                             |
| `/conring/mail/ceo/mcp` | `mail_check` / `mail_list`        | 未读数与最新邮件摘要（uid / from / subject / date / seen），`mail_list` 不改已读状态                                                                                                                                    |
| 三个端点                    | GET + `Accept: text/event-stream` | 均返回 `200 text/event-stream` 并长期挂起，是 MCP 的服务器推送通道                                                                                                                                                     |

**为什么客户端用长轮询 / SSE 而不是 `callback_url`**：Android 设备在 NAT / 移动网络后没有公网可回调地址，服务端无法 POST 进来。所以「保持监听」由客户端主动建立长连接 + 长轮询实现，收到结果即播报。工具定义见 `docs/mcp_asr_tools.json`，实测链路样本见 `docs/voice_flow_sample.json`。

### 14.2 四项需求如何落地

**① 应用名称**：`strings.xml` 的 `app_name` 改为「同心圆」，`aapt dump badging` 已确认 `application-label='同心圆'`；`McpClient` 的 clientInfo 同步改为 `conring-android / 0.2.0`。

**② 保持监听 + 新消息直接播报** —— `ListenService`（前台服务，常驻通知）并行跑三条链路：

| 链路      | 机制                                                                     | 结果                                   |
| ------- | ---------------------------------------------------------------------- | ------------------------------------ |
| 语音任务    | `voice_chat(wait=false)` 回 `job_id` → 服务用 `voice_job(wait_sec=20)` 长轮询 | done 后播放结果 WAV，App 在前台时自动弹数字员工汇报画面   |
| 服务端推送   | `GET /mcp` 的 SSE 长连接（`SseListener`，空闲 60 秒重连）                          | 收到通知直接播报；若通知带 `job_id`，转交任务链路取完整音频再播 |
| CEO 新邮件 | 定时 `mail_list`（不改已读状态），比对最新 uid                                        | 新邮件用 TTS 播「来自 X，主题 Y」                |

统一出口 `PushProcessor`：写历史 → 播报（优先服务端音频，否则系统 TTS）→ 系统通知 → 前台弹汇报画面。去重靠 `message_id`（job_id / 邮件 uid / 推送 hash），pending job 与邮件 uid 存 SharedPreferences，**服务重启不丢也不重复播报**。任务结果有 TTL，过期或连续 8 次失败会收尾并写一条失败历史，不会死循环。设置页可开关「保持监听 / 异步任务 / SSE / 新邮件」并配置轮询周期与邮件端点；`BootReceiver` 在开机或应用更新后自动恢复监听。

**③ 精简返回信息显示**：`ResultParser` 现在产出 `coreText`（只含 `speech_text` / `answer` / `detail_text` 的核心内容）与 `detailText`。`cleanCore()` 过滤系统提示行（`audio_path:`、`音频时长`、`识别到的指令：`…）、服务端路径（`/opt/...`、`/home/...`）和音频 / 字幕 / 视频文件名（`.wav/.srt/.json/.mp4`…）。主界面只显示 `coreText`；返回文件统一落盘成**语义标签图标链接**（播报语音 / 文字详情 / 汇报视频 / 图片），点击用系统应用打开，界面上不再出现真实文件名。`tools/VerifyCore.java` 纯 JVM 校验过滤结果 `PASS=true`。


**④ 本机长期历史（含音频和视频）**：`HistoryDb`（SQLite `conring_history.db`）每条消息一行，回执先落 `status=accepted`、结果到达按 `message_id` 原地更新。文件写入 `files/history/` **永久目录**（旧的 60 文件清理只作用于临时 `files/results/`），`ResultFileProvider` 已支持 `history/` 子路径。`HistoryActivity` 列表 + 时间/来源/媒体标记，`HistoryDetailActivity` 可重播音频、播放视频（本地优先、远程 URL 兜底）、查看附件图标链接、重新唤起员工汇报、单条删除或清空（同时删除关联文件）。服务端给的数字人视频 URL 会后台尝试缓存到本机（≤60MB），缓存成功后历史即可离线回看。

### 14.3 新增文件

| 类别   | 文件                                                                                                                                                                              |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 监听   | `ListenService.java`、`SseListener.java`、`PushProcessor.java`、`BootReceiver.java`                                                                                                |
| 数据   | `HistoryDb.java`、`ResultStore.java`（永久目录 / 视频 MIME / 语义标签）、`ResultFileProvider.java`（history 子路径）                                                                               |
| 解析   | `ResultParser.java`（重构：核心文本 / 异步元数据 / transient 解析）、`McpResult.java`（扩展字段）、`McpClient.java`（sessionId getter）                                                                   |
| 界面   | `HistoryActivity.java`、`HistoryDetailActivity.java`、`activity_history.xml`、`item_history.xml`、`activity_history_detail.xml`、`ic_history/ic_video/ic_play/ic_delete.xml`         |
| 配置   | `McpConfig.java`（asyncMode / keepListening / listenSse / listenMail / 周期 / 邮件端点）、`SettingsActivity.java` + 设置页新分组、`AndroidManifest.xml`（权限 / service / receiver / 2 个 Activity） |
| 探测工具 | `tools/probe_push.py`、`probe_schema.py`、`probe_voice_flow.py`、`probe_mail.py`、`probe_job_done.py <job_id>`、`tools/VerifyCore.java`                                              |

### 14.4 验证结果与边界

- `assembleDebug` **BUILD SUCCESSFUL in 35s**；`aapt dump badging` → `versionCode=2 / 0.2.0`、`application-label='同心圆'`、4 项新权限、service 与 receiver 已注册；`apksigner verify --verbose` → Verifies (v2)。
- 实测链路：`tts_synthesize` → `voice_chat` 拿到 `job_id=vj_8380af_50550` → `voice_job` 第 3 次长轮询（约 74 秒）返回 `status=done` + `audio/wav`（约 997KB base64）；`audio/wav` 与 `result.speech_text` / `detail_text` 已按新解析规则落盘。
- **已知边界**：① 服务端数字人素材 URL（`/videos/ceo.talk.mp4`、`/avatars/ceo.png`）当前公网返回 **401**，视频暂只能保存 URL / 尝试在线播放，服务端放行后客户端会自动缓存到本机；② SSE 通道实测可建立但空闲时无事件，真实推送要服务端主动发；③ 服务端任务结果有 TTL，长时间离线后旧 `job_id` 会过期；④ Android 15 对 `dataSync` 类型前台服务有时长限制，`START_STICKY` + 开机广播可恢复；⑤ 仍是 debug 签名，仅自测；⑥ 后台监听、通知权限、SSE/邮件轮询的耗电与真机行为需上机验证。

---

## 15. 同心圆 0.3.0：适配 server v2.1.0（2026-11-10 新增）

> **用户约束**：不使用其它 URL 的资源，等待 mcp server 完善。因此本版只消费服务端 `profile` 返回的素材地址（或 MCP 同源地址），不做多地址探测、端口猜测、第三方 CDN / 内网 IP 兜底；素材暂不可访问时安静回退内置 Canvas 数字人，服务端完善后无需改客户端即自动生效。

### 15.1 服务端实测（tools/list + 真实调用）

| 接口                              | 实测结果                                                                                                                                                                                                                                                            |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profile {include_assets:true}` | 一次返回 **18 人**，`asset_url_base=null`（相对路径），`missing_photo` / `missing_video` 均为空；每人含 `gender` / `age` / `voice` / `profile_ready` / `photo` / `videoTalk` / `videoIdle` / `assets.face_url`；`boss`、`manual_scout` 的 `profile_ready=false`                        |
| `voice_avatars`                 | 18 条素材清单，结构 `{photo{url}, videos{talk,idle}, has_video}`                                                                                                                                                                                                        |
| `voice_chat`                    | 新参数 `avatar_id` / `mode` / `bandwidth_kbps` / `delegate` / `callback_url` / `wait` / `wait_sec` / `include_audio` 全部在 schema 中；带 `avatar_id=pm_wenren, mode=auto, bandwidth_kbps=2000` 实测返回 `status=accepted` + `job_id` + `mode=video` + `avatar.id=pm_wenren` |
| `voice_job`                     | `wait_sec≤30` 长轮询；`running` 态即带 `ack` / `avatar`；`done` 态带 `result.speech_text` / `detail_text` / `segments` / `render` + `audio/wav`                                                                                                                           |
| 素材 HTTP                         | `https://ai.boraydata.com/avatars/*`、`/videos/*`、`/faces/*` 当前 **401**（`WWW-Authenticate: Bearer realm="sefs-mcp"`）；按用户约束，客户端不改用其它地址                                                                                                                            |

样本：`docs/v21_tools.json`、`v21_profile_all.json`、`v21_profile_ceo.json`、`v21_voice_avatars.json`、`v21_voice_chat.json`、`v21_voice_job_status.json`；探测脚本 `tools/probe_v21.py`，离线契约校验 `tools/verify_v21_contract.py`。

### 15.2 App 端改动

| 主题             | 实现                                                                                                                                                                            |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 员工档案服务端化       | `EmployeeStore.syncFromServer()` 调 `profile` 拉 18 人，缓存到 `filesDir/employees_server.json`（6 小时）；App 启动 / 档案页 / 监听服务启动自动刷新，离线用缓存或 `assets` 内置档案；设置页与档案页都有「立即同步」按钮               |
| 档案字段           | `Employee` 新增 `gender` / `age` / `role` / `project` / `profileReady` / `hasPhoto` / `hasVideo` / `faceUrl` / `voice`；列表页显示性别年龄、专属音色、素材与人设就绪状态，详情页显示项目 / 角色 / profile_ready 提示 |
| 素材地址           | 新增 `AssetUrls.java`：只把相对路径补成「服务端 `asset_url_base`」或「MCP 端点同源 origin」；**无候选列表、无探测、无其它主机 / 端口**；照片 / 视频加载失败逐级回退（视频 → 照片 → 内置数字人）                                                |
| voice_chat 新参数 | 请求带 `avatar_id`（默认当前汇报员）、`mode`、`bandwidth_kbps`（0 = 自动读链路带宽，测不到按 2000）、`delegate`、`include_audio`；**默认不再传 `voice` 与 `profile`**，让服务端按员工档案 / 语音点名自动决定（设置页可显式覆盖）               |
| 回执与派单          | 回执中的 `avatar` / `delegate` 决定实际汇报员工 → App 立即切换当前汇报员与顶部形象卡；`ack` 音频立即播「收到」，无音频块时用系统 TTS 读 `ack.text`（本次实测 `wait=false` 回执只有 text 块）                                            |
| done 结构        | `ResultParser` 解析 `delegate`、`error`、`voice`、`render.driver=wav_envelope`、`render.lip_sync=amplitude`、`segments`；`status=error` 用 `error` 字段提示重试，不把错误 JSON 当正文播报              |
| 汇报画面           | `ReportActivity` 支持 `video → avatar（照片 + 小窗数字人）→ 内置 Canvas` 三级形象；口型仍按 WAV 振幅包络驱动（与 `render` 指令一致），字幕改用服务端 `segments` 秒级时间轴精确定位，没有时才按字数估算                                      |
| 历史             | `HistoryDb` 升到 v2，**用 ALTER TABLE 增量加列（不 DROP、不丢长期历史）**，新增 `segments_json` / `avatar_id` / `delegate` / `voice` / `error_text`；历史详情显示音色 / 派单 / 错误，重新汇报时带时间轴与当次素材              |
| 设置页            | 新增 `avatar_id`、`mode`、`bandwidth_kbps`、`delegate`、`autoSyncProfile` 与「立即同步档案」；`voice` / `profile` 提示改为「留空 = 服务端自动选择（推荐）」                                                      |

### 15.3 新增 / 修改文件

- 新增：`AssetUrls.java`、`tools/probe_v21.py`、`tools/verify_v21_contract.py`、`tools/build_offline_profiles.py`、`docs/v21_*.json`；`assets/employees.json` 由 4 人示例换成服务端 18 人的离线快照。
- 重写：`Employee.java`、`EmployeeStore.java`、`McpConfig.java`、`McpResult.java`、`ReportActivity.java`、`HistoryDb.java`、`EmployeeListActivity.java`。
- 增量改：`MainActivity.java`、`SettingsActivity.java`、`ResultParser.java`、`PushProcessor.java`、`ListenService.java`、`HistoryDetailActivity.java`、`EmployeeDetailActivity.java`、`McpClient.java`、`activity_settings.xml`、`activity_employee_list.xml`、`item_employee.xml`、`activity_report.xml`、`app/build.gradle`（0.3.0 / versionCode 3）。权限未变（`ACCESS_NETWORK_STATE` 上一版已声明）。

### 15.4 验证与边界

| 项目                             | 结果                                                                                                                                                          |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `assembleDebug --no-daemon`    | **BUILD SUCCESSFUL in 36s**（最终打包 18 人离线档案的增量构建 21s；仅 deprecated API 提示）                                                                                     |
| 离线兜底档案                         | `tools/build_offline_profiles.py` 从 `profile` 样本生成 `assets/employees.json`（18 人、去掉服务器本地绝对路径），APK 内 `unzip -p ... assets/employees.json` 校验 `count=18`       |
| `aapt dump badging`            | `versionCode=3 / versionName=0.3.0`、`application-label='同心圆'`、targetSdk 35、权限齐全                                                                             |
| `aapt dump resources`          | `btnSync` / `btnSyncProfile` / `etAvatarId` / `etMode` / `etBandwidth` / `cbDelegate` / `cbAutoSync` / `tvMeta` / `tvSyncState` / `ivPhotoStage` 等新 ID 全部存在 |
| `apksigner verify --verbose`   | Verifies（v2，Android Debug 证书）                                                                                                                               |
| `tools/verify_v21_contract.py` | **24 / 24 PASS**：工具参数、18 人字段、18 个互异音色、`profile_ready` 标记、素材相对路径，以及「源码无 8848 / 无其它主机 / 无候选探测」「不再固定传 voice/profile」「segments 时间轴」「ALTER TABLE 不丢数据」等断言        |

**边界**：① ~~素材路径公网 401，照片 / 视频暂由内置数字人顶上~~ —— **服务端已于 2026-11-10 修复，见第 16 节**；② 若已有同一服务端的 Bearer Token，可在设置页填写，但素材静态路径现在不需要 Token（客户端也不再给素材请求附加 Bearer）；③ `boss`、`manual_scout` 无独立 Hermes profile，语音点名派单可能失败，档案页已标注；④ 本次实测 `wait=false` 回执没有 `content[1]` 音频块，App 用 `ack.text` + 系统 TTS 兜底；⑤ 仍是 debug 签名；⑥ 真机上需验证 18 人头像批量加载、segments 字幕与 WAV 对齐、后台同步耗电。

---

## 16. 同心圆 0.3.1：服务端素材路径修复适配（2026-11-10）

### 16.1 服务端变化与实测

服务端把静态素材挂到了 MCP 同源路径 `/conring/asr/assets/*`，并让 `profile` / `voice_avatars` 直接返回可用地址：

| 项                                 | 修复后                                                                                      |
| --------------------------------- | ---------------------------------------------------------------------------------------- |
| `profile.asset_url_base`          | `/conring/asr/assets`（相对路径前缀，不再是 `null`）                                                 |
| `photo`                           | `/conring/asr/assets/avatars/ceo.png`                                                    |
| `videoTalk` / `videoIdle`         | `/conring/asr/assets/videos/ceo.talk.mp4` / `.idle.mp4`                                  |
| `assets.face_url`                 | `/conring/asr/assets/faces/ceo.png`                                                      |
| `missing_photo` / `missing_video` | 均为空                                                                                      |
| 公网访问                              | **无需 VPN、无需 Token**，`https://ai.boraydata.com` + 上述路径即 200；视频 Range `bytes=0-1023` → 206 |

本机复测（`tools/probe_assets_v22.py`）：18 人 × `photo` / `videoTalk` / `videoIdle` / `face_url` = 72 项，加 18 个 talk 视频 Range，共 **90 项全部 200/206，0 失败**；`ceo` 照片 354,076 B、面部图 132,716 B、talk 视频 66,075 B，PNG / MP4 魔数与 Content-Type 均正确。明细保存在 `docs/v22_assets_check.json`，原始响应在 `docs/v22_profile_all.json`、`docs/v22_voice_avatars.json`。

> 同时服务端 ASR 回归通过（`text=你好用一句話介紹你自己`、`confidence=0.662`、`low_confidence=false`），客户端解析无需改动。

### 16.2 关键 bug：`asset_url_base` 会被叠加两次

0.3.0 的 `AssetUrls.resolve()` 把 `asset_url_base` 当前缀无条件拼接。服务端修复后 `photo` **本身已含该前缀**，旧逻辑会拼出：

```
/conring/asr/assets  +  /conring/asr/assets/avatars/ceo.png
= /conring/asr/assets/conring/asr/assets/avatars/ceo.png   ← 双重前缀，且仍是相对路径
```

结果不是合法 URL，`HttpURLConnection` 直接打不开，所有照片 / 视频都会静默回退内置数字人 —— 即服务端修好了，App 反而用不上。

**修复后的解析规则**（`AssetUrls.resolveSpec(mcpUrl, serverBase, spec)`，纯逻辑、可离线校验）：

| 服务端写法                                                   | 处理方式                                              |
| ------------------------------------------------------- | ------------------------------------------------- |
| `https://...` / `drawable://` / `assets://` / `file://` | 原样返回                                              |
| `/` 开头的同源绝对路径                                           | **MCP 端点 origin + 该路径**，不叠加 `asset_url_base`      |
| 裸相对文件名 `avatars/ceo.png`                                | 用 `asset_url_base`（相对则先按 origin 规范化；绝对 CDN 直接用）补齐 |
| 未配置 base                                                | origin + `/` + 文件名                                |

`origin` 仍由 `McpConfig.url`（MCP 端点本身）派生，**不新增主机、不新增端口、不加 8848 / 内网 IP / 第三方 CDN**，完全保持「只消费服务端自己返回的地址」这一约束。

### 16.3 本版改动

| 文件                                                      | 改动                                                                                              |
| ------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `AssetUrls.java`                                        | 重写解析；新增纯逻辑 `resolveSpec()` 供离线测试；注释更新为修复后的服务端形态                                                 |
| `Employee.java`                                         | 素材字段兼容驼峰 / 蛇形（`videoTalk`/`video_talk`、`faceUrl`/`face_url`、`assets.*`），支持 `{"url":"..."}` 对象写法 |
| `EmployeeStore.java`                                    | `profile` 缓存注释更新；照片加载**不再附加 `/mcp` 的 Bearer**（同源静态资源无需 Token）；视频 `resolveVideo()` 走同一套解析        |
| `PushProcessor.java`                                    | 历史视频缓存同样去掉 Bearer；注释更新为 200/206 现状                                                              |
| `assets/employees.json`                                 | 用 `docs/v22_profile_all.json` 重建 18 人离线快照，全部为新同源路径（`version 3`）                                 |
| `HistoryDetailActivity.java`                            | 视频来源文案由「需可访问」改为「可直接播放」                                                                          |
| `tools/build_offline_profiles.py`                       | 改读 v22 样本，兼容 `{result:{content:[]}}` 结构，写入真实 `asset_url_base`                                   |
| `tools/probe_assets_v22.py` / `tools/VerifyAssets.java` | 新增：只访问 MCP 同源地址的 18 人素材实测；`resolveSpec` 纯 JVM 回归（含「无双重前缀」断言）                                    |
| `tools/verify_v21_contract.py`                          | 新增 9 条 v22 断言（asset_url_base、72 条路径、90 项可达、离线快照、不带 Bearer、无其它 URL）                              |
| 版本号                                                     | `versionCode 4 / 0.3.1`，`McpConfig.CLIENT_VERSION` 同步（MCP `clientInfo.version` 自动带出）            |

### 16.4 验证

| 项目                             | 结果                                                                                       |
| ------------------------------ | ---------------------------------------------------------------------------------------- |
| `assembleDebug --no-daemon`    | **BUILD SUCCESSFUL in 1m4s**（仅 deprecated API 提示）                                        |
| `aapt dump badging`            | `versionCode=4 / 0.3.1`、`application-label='同心圆'`、targetSdk 35                           |
| `apksigner verify --verbose`   | Verifies（v2，Android Debug 证书）                                                            |
| APK 内 `assets/employees.json`  | `count=18`、`asset_url_base=/conring/asr/assets`、18 人 `photo` 全为新路径                       |
| `tools/verify_v21_contract.py` | **33 / 33 PASS**                                                                         |
| `tools/VerifyAssets.java`      | **16 / 16 PASS**，`PASS_ALL=true`（旧式 `/avatars/`、裸文件名 + 相对/绝对 base、绝对 URL、内置资源、空值、双重前缀回归） |
| `docs/v22_assets_check.json`   | **ok=90 / bad=0**                                                                        |

**当时的交付**：单包 `dist\voice-mcp-debug.apk`（346,057 字节，SHA256 `86dcdbdd…0299a`）；该无版本号命名已在第 17 节改为 debug + release 双包。


**仍待真机验证**：18 人头像批量加载的首屏耗时、`VideoView` 播 720×960 H.264 静音底片与 WAV 口型小窗的叠加效果、Range 拖动、弱网下三级回退、后台同步耗电。

---

## 17. 出包规则：debug + release 双版本，文件名带版本号

### 17.1 命名与唯一版本来源

`app\build.gradle` 顶部的两行常量是版本号的**唯一来源**，文件名、`defaultConfig`、资源注入全部引用它：

```groovy
def appVersionName = "0.3.2"
def appVersionCode = 5
def apkPrefix = "voice-mcp-v${appVersionName}-b${appVersionCode}"
```

`applicationVariants` 重命名输出文件，`collectApks` 任务再收集到 `dist/`。当前：

```
voice-mcp-v0.3.2-b5-debug.apk     353,549 B   dev / 可调试
voice-mcp-v0.3.2-b5-release.apk   328,083 B   日常安装 / 内部分发
```

`v<versionName>-b<versionCode>` 同时含对外版本名与内部构建号，避免同名覆盖；升级版本只需改那两行，Gradle 任务、CI 工件名、dist 文件名都会跟着变。两个 buildType 共用 `applicationId`，不加后缀。

### 17.2 一键出包

| 场景 | 命令 | 结果 |
| --- | --- | --- |
| 同时出双包（推荐） | `gradlew.bat collectApks --no-daemon` | 编译 debug + release → 复制到 `dist/`，并断言恰好 2 个 APK |
| 只出其中一个 | `gradlew.bat assembleDebug` / `assembleRelease` | `app\build\outputs\apk\<type>\` 里同样是带版本号的同名文件 |
| 云端 CI | `.github/workflows/build-apk.yml` → `./gradlew collectApks` | Artifact `conring-apks` 内含两个 APK |
| 版本一致性检查 | `gradlew.bat verifyVersionSync` | 断言 `McpConfig.CLIENT_VERSION == appVersionName`；`collectApks` 已自动依赖，不一致直接构建失败 |

`defaultConfig` 额外注入 `string/build_version_name` 与 `string/build_version_code` 资源，App 端后续可直接读取版本号，不必再手工同步常量。

### 17.3 release 签名策略

release 必须是**已签名**才能安装，所以 `build.gradle` 里配了两级策略：

| 条件 | 使用的证书 | 适用 |
| --- | --- | --- |
| 工程根存在 `keystore.properties` | 其中指定的正式 JKS | 正式分发 |
| 不存在（默认） | `~/.android/debug.keystore`（Android 默认 debug 证书） | 内部自测 |

`keystore.properties` 模板见 `keystore.properties.example`，文件本身与 `signing/*.jks` 均已加入 `.gitignore`，不会把私钥带进仓库。CI 上因首次运行没有 debug keystore，流水线加了一步 `keytool -genkeypair` 幂等创建它，再执行构建。

> 当前交付的两个包都用 debug 证书（`SHA-256 e7ffa31d…ba01`），**仅供自测，不可正式分发**；好处是两包同证书、同 applicationId，可以互相 `adb install -r` 覆盖安装来对比调试版与发布版。换成正式证书后，需先卸载旧包再安装。

### 17.4 上一轮（0.3.1）APK 正确性检查（2026-11-10）

> 以下为建立双版本出包规则时针对 0.3.1 交付的实测记录，保留作历史对照；**当前 0.3.2 的核查结论见第 18 节**。

对旧交付包 `dist\voice-mcp-debug.apk` 与新双包逐项校验：

| 检查项 | 结果 |
| --- | --- |
| 交付包 vs 构建产物 | SHA256 完全一致 `86dcdbdd…0299a`，无手工篡改 |
| `aapt dump badging` | `com.boray.voicemcp`、versionCode 4 / 0.3.1、minSdk 26 / targetSdk 35、`application-label='同心圆'`、launchable `MainActivity` |
| 权限与组件 | 7 项权限；Manifest 内 8 个 Activity + 1 Service + 1 Receiver + 1 Provider 全部注册 |
| `apksigner verify --verbose` | 两包均 Verifies、v2 签名、1 个签名者、证书摘要一致 |
| ZIP 完整性 | `unzip -t` 两包均 No errors detected |
| dex | debug 为 `classes.dex` + `classes2.dex`；release 合并为单个 `classes.dex`，13 个关键类（`AssetUrls` / `EmployeeStore` / `ListenService` / `HistoryDb` / `ReportActivity` / `WindowInsetsHelper` 等）全部存在 |
| release 差异 | 无 `application-debuggable`；开启资源路径缩短（`res/0K.xml` 等）与 `version-control-info.textproto`，资源 ID / 布局名 / adaptive icon 均保留；体积比 debug 小约 23 KB |
| 版本资源 | release 内 `string/build_version_name="0.3.1"`、`string/build_version_code="4"` |
| 业务数据 | 两包 `assets/employees.json` 均 `count=18`、`asset_url_base=/conring/asr/assets`、18 个互异音色、素材路径全为新前缀 |
| 离线回归 | `verify_v21_contract.py` **33/33 PASS**、`VerifyAssets.java` **16/16 PASS**；源码无 8848 / 内网 IP / 其它素材 URL |
| 构建 | `collectApks --no-daemon` **BUILD SUCCESSFUL in 1m44s**，仅 deprecated API / Gradle 9 兼容性提示 |

结论：**APK 结构与内容正确，可直接安装自测**；真机行为（录音权限、后台保活、通知、视频底片叠加）仍需在设备上验证。

---

## 18. 同心圆 0.3.2：名称一致、默认 CEO、试听对嘴与形象一致性核查

### 18.1 逐问结论

| 问题 | 核查结论 | 处理 |
| --- | --- | --- |
| 首页名称 | 首页大标题硬编码 `MCP 语音指令`，而启动器 / 设置里的 `app_name` 是「同心圆」，确实不一致 | 首页标题改为引用 `@string/app_name`，副标题统一为 `OPC 数字员工 · 语音直达`；README 标题同步 |
| 默认汇报员 | 旧逻辑 `current()` 在没有选择时回退列表第 1 位 —— 那是 `boss` 蒿色莫（OPC-001），**不是 CEO** | 默认改为 `ceo`（OPC-002 陆清安 / `m08`）；旧版本升级时做一次性迁移，之后用户手动切换的选择不会被覆盖 |
| 实时视频是否有效 | 服务端给的是 **6 秒预录 talk 视频**（循环、无音轨），`VideoView` 只做静音形象底片，并不是逐帧实时生成的口型视频。素材本身公网 90/90 可达 | 标签如实改为「仿真人视频底片 · 口型实时驱动」；`onPrepared` / `onError` 状态可见，失败明确提示「已回退照片」 |
| 口型是否跟随语音 | **正式汇报：是**（结果 WAV 的 PCM 包络 + `MediaPlayer.getCurrentPosition()` 逐帧同步）。**档案页「试听」：否** —— 旧实现跳进汇报页时不带音频，用「文本音节估算包络」配系统 TTS，听到的声音和驱动口型的包络不是同一份，所以不对嘴 | 试听改为先向同一服务端 `/conring/tts/mcp` 用该员工专属音色合成真实 WAV，再按真实音频驱动；离线时由汇报页用系统 TTS `synthesizeToFile` 生成 WAV 兜底 |
| 照片是否与 MCP server 一致 | **数据完全一致**：`profile` 的 18 人 × `photo` / `videoTalk` / `videoIdle` / `assets.face_url` = **72 项逐字段比对，0 差异**，`asset_url_base=/conring/asr/assets` | 不一致出在渲染层：角落「口型小窗」原来画的是内置 Canvas 卡通人，与大屏真人素材是「两个人」；现改为同一位员工的面部特写 / 照片做底片，只叠加动态嘴形 |

一致性样本：`tools/probe_assets_v23.py` → `docs/v23_assets_compare.json`（只访问 MCP 同源端点）。CEO 服务端实测：`id=ceo`、`name=陆清安`、`voice=m08`、`photo=/conring/asr/assets/avatars/ceo.png`、`face_url=/conring/asr/assets/faces/ceo.png`。

### 18.2 改动要点

| 文件 | 改动 |
| --- | --- |
| `res/layout/activity_main.xml` / `values/strings.xml` | 首页标题引用 `app_name`；新增 `home_subtitle` |
| `EmployeeStore.java` | `DEFAULT_EMPLOYEE_ID="ceo"`、`findById()` 严格查找、`current()` 默认 + 一次性迁移；新增异步 `loadPhotoBitmap()` 供小窗取同人素材 |
| `PreviewAudio.java`（新增） | 调 `tts_synthesize(text, voice=员工音色, return_audio=true)`，把服务端返回的 audio 内容块落成 WAV |
| `EmployeeDetailActivity.java` | 试听按钮先生成真实语音再进汇报页，同时传 `employee_name` / `face_url` / `photo` / `video`，失败提示并由汇报页本地兜底 |
| `ReportActivity.java` | 新增 `EXTRA_FACE` / `EXTRA_EMPLOYEE_NAME`；avatar 不在本地缓存时用服务端返回字段构造形象，不再错配列表首位；无音频时 `synthesizeToFile` 生成 WAV；`syncLabel()` 如实区分「真实 WAV 包络逐帧同步」与「文本音节估算（非音频同步）」；视频 prepared/error 状态可见 |
| `DigitalHumanView.java` | 新增 `setPhotoBitmap()` / `setMouthAnchor()`：有小窗照片时用真人底片 + 音频驱动的嘴形 + 音量条 + 情绪描边，不再画另一个卡通人；无素材时仍回退内置 Canvas |
| `MainActivity.java` / `PushProcessor.java` / `HistoryDetailActivity.java` | 汇报启动参数补齐 avatar 名字与面部图，保证大屏 / 小窗 / 标题都是同一位员工 |
| 版本 | `0.3.2 / versionCode 5`，`McpConfig.CLIENT_VERSION` 同步（`verifyVersionSync` 自动校验） |

### 18.3 验证

| 项目 | 结果 |
| --- | --- |
| 服务端素材一致性 | `docs/v23_assets_compare.json`：`server=18 offline=18 compared=72 mismatches=0` |
| CEO 专属音色试听 | `tts_synthesize(voice=m08)` → `405,778 B` WAV，24 kHz / 单声道 / 16 bit / 8.45 s；样本 `docs/preview_ceo_m08.wav` |
| 口型包络回归 | `tools/VerifyPreview.java` **8/8 PASS**：211 帧、86% 有效发声帧、5 种口型、存在闭嘴帧、来自真实音频而非文本估算 |
| 离线契约 | `tools/verify_v21_contract.py` **46/46 PASS**（新增 13 条本轮断言） |
| URL 解析回归 | `tools/VerifyAssets.java` **16/16 PASS** |
| 构建 | `collectApks --no-daemon` **BUILD SUCCESSFUL in 1m31s**，`VERSION SYNC OK: 0.3.2 (versionCode 5)` |
| 双包校验 | `versionCode 5 / 0.3.2`、`application-label='同心圆'`、v2 Verifies、ZIP 完好；release 内 `build_version_name="0.3.2"` / `home_subtitle` 正确；`PreviewAudio` 已进 dex |
| 交付哈希 | debug `681c08aa…015b`（353,549 B）、release `b9a6b0dc…339e`（328,083 B） |

### 18.4 仍需注意

- **视频不是实时渲染**：它是服务端 6 s 循环底片；实时的是按 WAV 逐帧驱动的口型层（对应 `render.driver=wav_envelope` / `lip_sync=amplitude`）。若要「视频级实时口型」，需要服务端提供 viseme 时间轴或实时渲染流。
- 面部特写嘴部锚点默认 `(0.50, 0.72)`；若某张 face 图构图不同，可用 `setMouthAnchor()` 微调。
- 本地 `synthesizeToFile` 依赖设备 TTS 引擎支持；不支持时会明确标注「非音频同步」，不会假装对嘴。
- 仍是 debug 证书自测包；真机需目测 VideoView 解码 / Range 拖动、照片首屏加载、试听语音与口型对齐、后台保活耗电。

## 19. 同心圆 0.4.0：server 端生成的自我介绍视频 + 实时视频通话（2026-11-10 新增）

### 19.1 实时推流技术选型结论

**推荐：CMAF/fMP4（或 TS）分片 + HLS，全程复用 `https://ai.boraydata.com` 同源 443。**

- 自我介绍是 **VOD**：服务端预生成音画合一的 MP4 / HLS VOD，客户端 `VideoView` 用 Range 或 m3u8 分片边下边播，起播 0.5–2 s。
- 实时通话是 **短分片 HLS Live**：1–2 s 分片，端到端 2–4 s；本场景是「用户说一句 → 员工答一句」的半双工回合，该延迟足够。
- **音视频必须 mux 在同一路流 / 同一分片**，共用 PTS，由 `MediaPlayer` 自动完成 A/V 同步。切勿「视频走 HLS、语音单独用 `voice_chat` 返回 WAV」——两路独立时钟需要客户端自研 PTS 对齐，复杂度高且必然漂移。

| 方案 | 延迟 | 零依赖原生可播 | 同源 443 | A/V 同步 | 服务端改造 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| MP4 Range / HLS VOD | 起播 0.5–2 s | ✅ VideoView | ✅ | 容器内自动 | 极低 | ✅ 自我介绍 |
| 1–2 s 短分片 HLS Live | 2–4 s | ✅ MediaPlayer | ✅ | 自动 | 中（切片器） | ✅ 实时通话 |
| LL-HLS `EXT-X-PART` | 1–2 s | ⚠️ 原生 MediaPlayer 不认 PART | ✅ | 自动 | 中高 | ⚠️ 服务端先出 CMAF 短分片即可 |
| WebRTC | 0.1–0.5 s | ❌ 需 libwebrtc AAR | ❌ 需 UDP/STUN/TURN | ✅ 内建 | 高（信令 + SFU + TURN） | ⚠️ 仅「全双工可打断」才值得，列为 v2 |
| MPEG-DASH | 2–10 s | ❌ 需 ExoPlayer | ✅ | 自动 | 中 | ❌ 违反零依赖 |
| RTMP / RTSP / SRT | 0.5–2 s | ❌ 无原生解码 | ❌ 非 443 | 部分 | 高 | ❌ |
| SSE 推 JPEG 帧 + 单独 WAV | 0.3–1 s | ✅ 需自研 | ✅ | ❌ 需自研对齐 | 中 | ❌ 仅作降级 |

实操建议：分片容器优先 **TS**（MediaPlayer 兼容最稳，fMP4/CMAF 需真机验）；VOD playlist 带 `#EXT-X-ENDLIST`，live 不带；上行语音继续走现有 MCP `voice_chat` / `call_turn`，无需另建通道。

### 19.2 服务端需要补的契约（2026-11-10 实测当前尚缺）

`tools/probe_v24_stream.py` → `docs/v24_server_contract.json`：asr 只有 `asr_transcribe / voice_chat / voice_job / profile / voice_avatars`，tts 2 个、mail 4 个，**无 `call_*` 工具**；`profile` 18 人字段里只有 `photo / videoTalk / videoIdle / assets.face_url` 等，**无 `intro_*`**。所以本次是「客户端契约先行 + 自动降级」。

| 位置 | 需补内容 | 建议值 |
| --- | --- | --- |
| `profile.employees[].intro_video` | 自我介绍视频（含语音） | 同源绝对路径，如 `/conring/asr/assets/videos/<id>.intro.m3u8`（HLS VOD）或 `.mp4` |
| `intro_text` / `introText` | 字幕与降级朗读文案 | 可缺省，客户端用档案字段拼接 |
| `intro_poster` / `introPoster` | 首帧海报 | 可选，缺省用同人 `photo` |
| `intro_audio` | 独立语音 | **不建议**；只在视频未 mux 音轨时给 |
| `call_stream` | 预配置 live playlist | 可选，优先用 `call_start` 返回的 |
| `has_intro` | 是否已生成 | bool |
| 工具 `call_start` | 发起通话 | 入参 `employee_id / avatar_id / protocol=hls / audio_muxed=true / segment_ms=1000`；返回 `session_id` + `stream_url`（同源 `/conring/asr/live/...m3u8`）+ `audio_muxed` + `segment_ms` |
| 工具 `call_turn` | 上行一轮语音 | 入参 `session_id / audio_base64 / stream=true`；返回 `reply_text` 或 ack |
| 工具 `call_stop` | 结束通话 | 入参 `session_id` |

字段兼容：客户端同时接受驼峰、蛇形、`intro` / `assets` 子对象，`stream_url` 兼容 `hls_url / playback_url / live_url / stream.url` 等别名。**所有地址必须 MCP 同源**，外链一律拒绝。

### 19.3 客户端改动

| 文件 | 作用 |
| --- | --- |
| `Employee.java` | 新增 `introVideo / introAudio / introText / introPoster / callStream / hasIntro`，多命名兼容；`introCaption()` / `streamLine()` |
| `StreamContract.java`（新增） | 同源校验、HLS 判定、`call_start` 多形态解析、重试 URL、工具常量、回复文本提取 |
| `StreamPlayback.java`（新增） | 封装 `VideoView`/`MediaPlayer`：VOD 边下边播、LIVE 断流自动重试 4 次（同源 `_retry` 参数）、带音轨、进度/缓冲回调 |
| `IntroPlayerActivity.java` + `activity_intro_player.xml`（新增） | 档案页「播放自我介绍」：同源 HLS/MP4 流式播放，含语音、字幕、进度、首帧照片；未生成/失败时降级到真实 WAV + 照片口型 |
| `LiveCallActivity.java` + `activity_live_call.xml`（新增） | `call_start` → 同源 live m3u8 → MediaPlayer 实时播放；按住说话走 `call_turn`（缺工具退回 `voice_chat`）；结束 `call_stop`；断流重试失败切「回合模式」 |
| `EmployeeDetailActivity.java` / 布局 | 增加两个入口与 `tvStreamState`，显示 `emp.streamLine()` 的就绪状态 |
| `AndroidManifest.xml` / `strings.xml` | 注册两个 Activity、新增标题 |
| 版本 | `0.4.0 / versionCode 6`，`McpConfig.CLIENT_VERSION` 同步 |

降级原则：服务端未生成视频或未实现 `call_start` 时，页面明确写出「等待服务端生成 / 需要补的契约」，仍可用 `tts_synthesize` 真实 WAV + 照片口型试听，或走 `voice_chat` + `ListenService` 长轮询的回合式语音对话；**不白屏、不假装实时、不连任何其它 URL**。

### 19.4 验证结果

| 项目 | 结果 |
| --- | --- |
| 服务端能力快照 | `tools/probe_v24_stream.py` → `docs/v24_server_contract.json`：18 人、asr 5 工具、无 `call_*` / `intro_*` |
| 离线契约 | `tools/verify_v24_contract.py` **31/31 PASS**（含 14 条 Java 纯逻辑实测 + 7 条规则断言 + 静态一致性） |
| 流地址回归 | `tools/VerifyStream.java` **14/14 PASS**：同源绝对路径不叠加前缀、外链/异端口拒绝、HLS 判定、重试 URL 同源 |
| 编译 | `assembleDebug` BUILD SUCCESSFUL 1m4s；`collectApks` BUILD SUCCESSFUL 55s，`VERSION SYNC OK: 0.4.0 (versionCode 6)` |
| 双包内容 | `tools/verify_apk_v24.py` **30/30 PASS**：两包 Manifest 均注册新 Activity、dex 含 4 个新类、资源齐全、ZIP 完好、v2 签名块存在 |
| 包信息 | `aapt2 dump badging`：`versionCode 6 / 0.4.0`、`application-label='同心圆'`、minSdk 26 / targetSdk 35；`apksigner verify` release **v2 Verifies / 1 signer** |
| 交付 | `dist/voice-mcp-v0.4.0-b6-debug.apk` 376,398 B · SHA256 `4f9a15b3…003f`；`dist/voice-mcp-v0.4.0-b6-release.apk` 347,763 B · SHA256 `5e38bf87…6fc0`；旧 `0.3.2` 双包已移除 |

### 19.5 边界与后续

- 客户端不做两路 A/V 对齐：同步质量完全取决于服务端是否把音频 mux 进视频分片；`audio_muxed=false` 时页面会标注提示。
- 纯 framework 零第三方依赖保持不变（未引入 ExoPlayer / WebRTC / OkHttp）；HLS 能力由系统 `MediaPlayer` 提供，部分国产 ROM 对 fMP4/CMAF 分片需真机验证，建议服务端优先 TS。
- 「实时」为短分片 HLS Live 的 2–4 s 半双工回合；若后续要求 **<500 ms、可随时打断、双向同时说话**，再评估 WebRTC（需引入 libwebrtc AAR + 同源信令 + TURN over TLS 443），那会打破零依赖约束。
- release 仍使用 Android debug 证书，仅内部自测；正式发布需在 `keystore.properties` 配真实 JKS。
- 真机待验：HLS VOD/Live 解码与 playlist 刷新、麦克风运行时权限、后台切回后的播放器恢复、弱网断流重试体验。



## 20. 同心圆 0.5.0：对接 server v2.5 真实契约（introVideo + live_call）

服务端已上线第 19 节提出的两项能力，客户端从「契约先行」改为「按真实返回实现」。

### 20.1 服务端实测（`tools/probe_v25_*.py` → `docs/v25_*.json`）

`profile` 18 人全部就绪，`missing_photo / missing_video / missing_intro` 均为空：

| 字段 | 实测值（CEO 陆清安） |
| --- | --- |
| `introVideo` | `/conring/asr/assets/intro/ceo.mp4`（660,258 B，`avc1` + `mp4a`，Range 206） |
| `introReady` / `introText` / `introDuration` | `true` / 自我介绍文案 / `21.16`（18 人 18.0–21.2 s） |
| `liveCall` | `{supported:true, protocol:"hls", segment_sec:1.0}` |

工具契约（asr 端点 7 个工具）：

| 工具 | 参数 | 实测返回 |
| --- | --- | --- |
| `intro_video` | `id / all / force / wait` | `{count, url_base, items:[{id, ready, cached, video, text, duration, size, source}]}`，已缓存时 0.1 s 返回 |
| `live_call` | `action:start\|say\|stop\|status`、`session_id`、`avatar_id`、`text`、`audio_base64`、`first_segment_sec` | `{session_id, status:"live", hls_url:"/conring/asr/assets/live/<sid>/index.m3u8", segment_sec:1.0, sync:"音视频同流复用（AAC+H.264）…"}` |
| `voice_chat` | 新增 `live_session` | 给了 `live_session` 就把「收到」与正式回复**混进同一路 HLS 流**，客户端只播一个 m3u8 |

直播流实测形态：`#EXT-X-PLAYLIST-TYPE:EVENT`、`#EXT-X-TARGETDURATION:1`、分片 `seg_0000N.ts` 持续追加（1 秒 1 片，`start` 返回时已有 1 片），分片内 PMT 同时含 `0x1b`(H.264) 与 `0x0f`(AAC)；`live_call stop` 后写 `#EXT-X-ENDLIST`。起流约 3.3 s，`say` 返回即时并把语音排队进流（`queued_seconds`）。

### 20.2 客户端对齐改动

| 文件 | 改动 |
| --- | --- |
| `Employee.java` | 新增 `introReady / introDuration / liveSupported / liveProtocol / liveSegmentSec`；解析 `liveCall.{supported,protocol,segment_sec}`；`hasIntroVideo()` 以 `introReady` 为准；`streamLine()` 显示「已生成 21.2s · hls 1s 分片」 |
| `StreamContract.java` | 工具常量改为 `live_call` / `intro_video` / `voice_chat` / `voice_job`；`parseCall` 解析扁平 `hls_url`+`segment_sec`(秒→毫秒)+`sync`；新增 `parseIntro`（按 id 选人、外链拒绝）、`hasSegment` / `isEnded`；修 `replyText` 取 `ack.text` 内层文本 |
| `StreamPlayback.java` | LIVE 模式先用同源 `HttpURLConnection` 轮询 m3u8 到「有 `#EXTINF` 且至少一个分片」再交给 `MediaPlayer`（空 playlist 会立刻报错）；新增 `onWaiting` 回调；断流只对同一同源 URL 加 `_retry` |
| `LiveCallActivity.java` | `live_call{action:start, avatar_id, first_segment_sec:8}` 起流；接通后 `say` 一句开场问候；按住说话 → `voice_chat{audio_base64, live_session}`（`wait=false` 拿 `job_id`）→ `voice_job` 轮询出字幕；结束 `live_call{action:stop}`；顶栏显示通话时长 |
| `IntroPlayerActivity.java` | 直接播 `profile.introVideo`（含音轨 MP4，Range 边下边播），显示 `introDuration`；未生成时按钮变「生成自我介绍」→ `intro_video{id, wait:true}` 现生成再播；仍保留真实 WAV + 照片口型降级 |
| 版本 | `0.5.0 / versionCode 7`，`McpConfig.CLIENT_VERSION` 同步 |

关键取舍：**上行不再走 `live_call say`**（那是「让员工说这段音频」），而是 `voice_chat + live_session`——只有它才会做完 ASR + 大模型 + TTS 并把「收到」和正式回复一起混进直播流，客户端始终只播一个 m3u8，音画天然同步。

### 20.3 验证结果

| 项目 | 结果 |
| --- | --- |
| 契约校验 | `tools/verify_v25_contract.py` **53/53 PASS** |
| Java 纯逻辑 | `tools/VerifyStream.java` **40/40 PASS**：用服务端原样 JSON 跑 `parseCall(start/say/stop)`、`parseIntro`（含未知 id 回退、外链拒绝）、playlist 分片/ENDLIST 判定、`ack.text` 提取 |
| 在线档案 | 18/18 `introVideo` 同源、`introReady=true`、`introDuration>0`、`liveCall.supported=true`；18/18 介绍视频 Range 均可取流；CEO 视频含 `avc1`+`mp4a` |
| 在线通话 | `start`→同源 `hls_url`→playlist 有分片→分片含 H.264+AAC→`stop`→`status=ended` 且写 ENDLIST |
| 静态一致性 | 字段/工具/常量/布局 id/Manifest/版本三处一致/零第三方依赖/新代码无第三方 URL 全通过 |
| 双包校验 | `tools/verify_apk_v25.py` **48/48 PASS**：两包均含新 Activity、dex 含 `live_call/intro_video/live_session/first_segment_sec/segment_sec` 等契约串、**不含旧 `call_*`**、ZIP 完好、已签名 |
| 包信息 | `aapt2 dump badging`：`versionCode 7 / 0.5.0`、`application-label='同心圆'`、targetSdk 35；`apksigner verify` release **v2 Verifies / 1 signer** |
| 交付 | `dist/voice-mcp-v0.5.0-b7-debug.apk` 510,032 B · SHA256 `53a05853…9bb0`；`dist/voice-mcp-v0.5.0-b7-release.apk` 353,171 B · SHA256 `55c571ff…4a33` |

工具链补充：`android.jar` 里的 `org.json` 是 `Stub!`，纯 JVM 校验改挂 `tools/lib/json-20240303.jar`（仅测试期使用，APK 仍零第三方依赖）。

### 20.4 边界与真机待验

- 直播是 **EVENT 型累积 playlist**：客户端从第 0 片开始播，因此比实时落后「接入耗时 ≈ 2–4 s」且不会自动追帧；同一会话越晚接入落后越多。若需贴边播放，需服务端提供 `#EXT-X-SLIDING` 窗口或客户端做分片跳转。
- 一次 `voice_chat` 回合实测 10–60 s（取决于大模型/任务），期间画面继续播 idle/talk 循环；「收到」语音约 1 s 内先入流。字幕由 `voice_job` 轮询补上。
- 真机待验：MP4 Range 首帧、live playlist 刷新与断流重连、麦克风权限、`voice_chat` 回合期间长时间直播的内存与耗电、切后台回前台后的播放器恢复。
- release 仍是 Android debug 证书自测包；正式发布需 `keystore.properties` 配真实 JKS。

## 21. 同心圆 0.5.1：档案页首屏自动播服务端介绍视频（铺满）+ 播放页加固

### 21.1 问题与原因

| 现象 | 原因 |
| --- | --- |
| 详情页「小照片、背景更大」 | 270dp 的舞台里画的是**内置 Canvas 卡通数字人**，真照片只是下面一张 76×94dp 的小卡片；仿真人视频默认走静音底片，观感像「大空背景 + 小照片」 |
| 点「播放自我介绍」后自动返回 | 全屏页 `onCreate` 里只有 `emp == null → finish()` 一条关闭路径，实测档案不会为空，说明更可能是**未捕获异常**导致页面被销毁；原实现没有任何兜底与日志，看不出原因 |
| 个人视频与 server 不同步 | 只读本地缓存 `employees_server.json`，进页面不刷新；服务端重新生成 `intro/&lt;id&gt;.mp4` 后客户端仍播旧缓存 |

顺带修掉一个真实隐患：`syncFromServer` 之前是「truncate 后直接写」`employees_server.json`，别的页面正好在这时读就会 parse 到半截 JSON，退化成内置档案（表现为「人和名字对不上」或「点进去就返回」）。现在改为**先写 .tmp 再原子 rename**。

### 21.2 改动

| 文件 | 改动 |
| --- | --- |
| `CoverVideoView.java`（新增） | 铺满裁切的 VideoView：`onMeasure` 按「视频宽高比且不小于容器」量尺寸，由父 `FrameLayout`（`clipChildren=true` + `layout_gravity=center`）裁掉溢出；视频尺寸由 `MediaPlayer.getVideoWidth/Height` 在 prepare 后回填（`VideoView` 的 `mVideoWidth` 是隐藏字段，取不到） |
| `activity_employee_detail.xml` | 舞台扩到 3:4（代码里按屏宽算高，最高 58% 屏高），依次叠 `ivIntroPoster`（同人照片，首帧前顶住）→ `CoverVideoView` → `DigitalHumanView`（降级）＋ 状态徽标 `tvIntroState` ＋ 右下 `btnStageReplay` 重播 |
| `EmployeeDetailActivity.java` | **进入即 `playHero(false)` 自动播放服务端介绍视频（带声音）**；降级链 介绍视频 → idle/talk 仿真人视频（静音循环）→ 内置数字人；`syncLatest()` 后台拉 `profile`，若 `introVideo/introDuration/introText` 变化立即换地址重播；`onPause/onResume` 暂停恢复 |
| `StreamContract.versioned()` | 给同源视频加 `?v=<时长+文案哈希>` 内容版本参数，服务端重新生成同名文件后不会命中旧缓存（nginx 实测带 query 仍 206） |
| `IntroPlayerActivity.java` | 全屏页同样用 `CoverVideoView` + 自动播放 + `syncLatest()`；**`onCreate` 整体 try/catch(Throwable) 并 `Log.e("IntroPlayer")`，不再静默关页**；播放失败在页面上写明 what/extra |
| `StreamPlayback.java` | prepare 后把视频尺寸交给 `CoverVideoView`，避免首帧按容器比例拉伸 |
| `EmployeeStore.java` | 档案缓存原子写入（.tmp + rename） |
| 版本 | `0.5.1 / versionCode 8`，`McpConfig.CLIENT_VERSION` 同步 |

### 21.3 验证

| 项目 | 结果 |
| --- | --- |
| 契约校验 | `tools/verify_v25_contract.py` **59/59 PASS**（新增 6 条：详情节自动播放/对齐最新/不静音/布局 CoverVideoView/全屏页不静默关页/缓存原子写） |
| Java 纯逻辑 | `VerifyStream.java` 40/40（版本号断言改为从 build.gradle 读取，升版不用改脚本） |
| 双包校验 | `tools/verify_apk_v25.py` **54/54 PASS**（新增 CoverVideoView 类与布局检查） |
| 包信息 | `versionCode 8 / 0.5.1 / 同心圆`；release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.5.1-b8-release.apk` 358,183 B · SHA256 `0ba89e47…f4eba`；`dist/voice-mcp-v0.5.1-b8-debug.apk` 521,379 B · SHA256 `3d60ab33…1bd07` |

### 21.4 仍需真机确认

- 若「点进去就返回」在 0.5.1 上仍复现，现在页面不会直接消失，而是显示具体原因；抓 `adb logcat -s IntroPlayer` 能看到 `init failed` 的堆栈（这就是当初缺的那条线索）。
- 竖版 720×960 视频在超宽屏上会被裁掉上下少许；若希望完整显示整幅画面，把该页 `CoverVideoView` 换回原生 `VideoView` 即可（会留黑边）。
- 自动播放带语音，开会/静音场景需注意；如需默认静音，可给 `playIntro` 里 `mp.setVolume(0f,0f)` 加一个开关。

## 22. 同心圆 0.5.2：首次才自动播、语音控制开关、去掉底部介绍按钮

### 22.1 三条需求与实现

| 需求 | 实现 |
| --- | --- |
| 打开档案自动播自我介绍，但**只有首次**，切回来不再播 | 进程级集合 `AUTO_PLAYED`（`Collections.synchronizedSet`）记录本次运行已自动播过的员工 id；`playHero(firstTime)` 只有首次 `autoStart=true`。非首次只把视频准备到首帧、暂停在 0 点，中央显示播放按钮；`onResume` 刻意**不**起播，从别的页面/App 切回来保持暂停（重启 App 后重新算首次） |
| 「语音控制」图标可开关并记住，关掉后首次也不出声，视频照旧 | 顶栏新增喇叭图标 `btnVoice`（`ic_volume_up` / `ic_volume_off`），写 `McpConfig.voiceEnabled`（默认 true，随其他设置存 SharedPreferences）。关掉后：视频照常加载最新版并**静音播放**（`setVolume(0,0)`），点按播放也不出声；空档期的首帧起播也是静音起播，绝不会漏音 |
| 去掉下方「播放自我介绍」按钮 | 布局与代码里删除 `btnIntro`；舞台上点按=暂停/继续，右下角=重播，**长按舞台**仍可进全屏播放页（原按钮能力保留，不占底部位置） |

首帧落地技巧：`VideoView` 不起播就不出画面。非自动播放时先 `setVolume(0,0)` 起播 320ms 再 `pause()+seekTo(0)`，然后恢复音量等待用户点按——既有首帧又不会突然出声。

### 22.2 顺带修掉的一个真实泄漏

`LiveCallActivity` 原来把 `live_call stop` 丢给 `io` 线程池，紧接着 `finish()` → `onDestroy()` 里 `io.shutdownNow()`，任务经常还没执行就被杀掉，**服务端会话一直挂着**。服务端同时最多 8 路，攒满后就再也起不来新通话（会回「实时通话会话已达上限 8，请先挂断」）。现在改为独立守护线程 + `AtomicBoolean` 只发一次，`onDestroy` 也兜底调一次；会话满时页面会明确提示而不是笼统报错。

### 22.3 验证

| 项目 | 结果 |
| --- | --- |
| 契约校验 | `tools/verify_v25_contract.py` **53/53 PASS**（新增：已删 `btnIntro`、新增 `btnVoice`/`btnStagePlay`、两套音量图标、`AUTO_PLAYED` 进程级记录、`voiceEnabled` 持久化、关闭后静音、`onResume` 不起播） |
| Java 纯逻辑 | `VerifyStream.java` 40/40 |
| 双包校验 | `tools/verify_apk_v25.py` **54/54 PASS** |
| 包信息 | `versionCode 9 / 0.5.2 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.5.2-b9-release.apk` 361,523 B · SHA256 `4ea92984…17f5`；`dist/voice-mcp-v0.5.2-b9-debug.apk` 528,244 B · SHA256 `8b4830c7…a587` |
| 在线通话 | 本次 **SKIP**：服务端 8 路会话已被之前探测占满（`live_call` 无 list/cleanup 动作，需服务端侧清理） |

### 22.4 服务端待办

- 清理遗留的 `assets/live/lv_*` 会话（或给 `live_call` 加 `action=list` / 空闲超时回收），否则真机上点通话会一直提示「会话已达上限 8」。

## 23. 同心圆 0.5.3：实时通话页改成「开始/结束 + 持续聆听 + 追直播边缘」

### 23.1 改了什么

| 需求 | 实现 |
| --- | --- |
| 去掉「重新连接」按钮，自动重连 | `LiveCallActivity` 不再有重连入口：`StreamPlayback` 先做 4 次同源重试，仍失败则由 `autoRebuild()` 静默重建会话（1.5s 后 `live_call start` 换新流），**麦克风全程不中断**。用户只需「开始 / 结束」两个按钮 |
| 只保留开始、结束 | 布局只留 `btnStart`（实心）与 `btnEnd`（描边）；删掉 `btnTalk`（按住说话）与旧的「重新连接」 |
| 开始后一直监听、实时发送 | 新增 `LiveMic`：`AudioRecord(VOICE_COMMUNICATION)` 常开，20ms 一帧算 RMS，动态估底噪做 VAD；说话前留 300ms 预录，停顿 700ms 且说话 ≥400ms 判定一句说完，单句最长 12s；每句封成 16k/16bit/单声道 WAV 抛给上层 |
| 不丢话 | 句子进 `LinkedBlockingQueue`（容量 6），单独 `live-sender` 线程串行 `voice_chat{audio_base64, live_session}`，上一句再慢也按顺序发 |
| 通话无时长上限 | 没有任何自动挂断/计时限制，只有用户点「结束」才 `live_call stop` |
| 视频要是实时的 | 服务端 playlist 是 `#EXT-X-PLAYLIST-TYPE:EVENT`、从 `seg_0` 累加。若从 0 播就会越拖越远。现在 `onReady` 先读一次 playlist 算出总时长（= 直播边缘），`seekTo(total-1.2s)` 起播；之后每 1.5s 复查，落后 >3s 再追一次 |
| 防自问自答 | 扬声器在放员工声音，麦克风会听到。`LiveMic` 用 `VOICE_COMMUNICATION` + `AcousticEchoCanceler` + `NoiseSuppressor`；通话期间 `AudioManager` 切 `MODE_IN_COMMUNICATION` + 强制外放，结束恢复 |

界面：卡片里新增麦克风电平条 `pbMic` 与状态 `tvMicState`（聆听中 / 正在听你说 / 已拾音 1.2s 排队发送 / 已发送·继续聆听）。

### 23.2 验证

| 项目 | 结果 |
| --- | --- |
| 契约校验 | `tools/verify_v25_contract.py` **74/74 PASS**（新增：只有两个按钮、无「重新连接」、`autoRebuild`、`LiveMic`、AEC、串行队列、追边缘、无时长上限） |
| Java 纯逻辑 | `VerifyStream.java` **45/45 PASS**（新增 `playlistTotalMs / segmentCount / liveStartMs`：7 段 playlist = 7000ms，起播位 5800ms） |
| 在线实测 | 起流 6s 后 playlist 7 段 / 总长 7.0s，验证「总时长 = 直播边缘」这一前提成立 |
| 双包校验 | `tools/verify_apk_v25.py` **56/56 PASS**（含 `LiveMic` 类） |
| 包信息 | `versionCode 10 / 0.5.3 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.5.3-b10-release.apk` 365,803 B · `79ec478d…9d50`；`dist/voice-mcp-v0.5.3-b10-debug.apk` 399,285 B · `972ff640…2b53` |

顺带发现：服务端 profile 已从 18 人变成 **19 人**，校验脚本改为「≥18 人且字段齐全」，不再写死数量。

### 23.3 已知限制（真机需验证）

- 「实时」的**上行**受服务端大模型回合耗时限制：一句话发出去到员工开口仍要几秒到几十秒（`voice_chat` 是推理任务）；下行画面/语音是同一条 HLS 流，追边缘后延迟约 2–4s。
- `seekTo` 在直播 HLS 上的行为依 ROM 而异：能 seek 就贴边播；个别 ROM 会忽略 seek，此时退回从 `seg_0` 顺序播（`chase` 会持续尝试）。
- 回声消除依赖设备 AEC 能力；若外放音量过大仍有回灌，建议用耳机或降低音量。
- 需要麦克风 + 前台运行；长期通话建议接充电器（持续录音 + 解码耗电）。

## 24. 同心圆 0.6.0：小秘书 + 实时语音通话 + 当前进度 / 待办事项

### 24.1 服务端这次的变更（实测）

| 变化 | 实测 |
| --- | --- |
| 新增员工 | `profile` 19 人，新增 **`secretary`（OPC-001 小秘书，老板影子分身，音色 m10）** |
| 新增工具 | asr 端点多出 **`boss_briefing`**：老板上线钩子，异步返回 `job_id`，再用 `voice_job` 取「小秘书巡检简报」（含 `speech_text` / `detail_text` / 语音 / 分段） |
| 简报结构 | `detail_text` 固定四段：`一句话结论` / `进展` / `风险与异常` / `需要你拍板（急件置顶）`，待办以 `【急】/【缓】` 开头，末尾带「建议 X」 |

### 24.2 三项改动

| 需求 | 实现 |
| --- | --- |
| 默认汇报员改成小秘书 | `EmployeeStore.DEFAULT_EMPLOYEE_ID = "secretary"`，并换迁移 key 让老版本升级后一次性切到小秘书（之后用户手选不再被覆盖） |
| 通话改成实时语音、不先录音 | 新增 **`VoiceCallService`**（前台服务）：`live_call start` 拿同源 HLS，用 `MediaPlayer` **只播音轨、不建 Surface**（所以没有视频）；用户侧 `LiveMic` 常开 + VAD 自动断句，每句 `voice_chat{live_session}` 上行；员工的「收到 + 回复」由服务端混进同一路流，**解码即播**，不用等整轮推理 |
| 通话图标连通后变红、可随时关 | 首页头部新增 `btnVoiceCall`（电话图标）：空闲=主色；接通中/已连接=**红色**（`bg_call_active` + `call_red` 着色），再点即挂断。首页 `tvBadge` 同步显示「通话中 · 点击挂断」 |
| 重播语音 → 当前进度 | `ProgressActivity`：调 `boss_briefing` → `voice_job`，解析成「一句话结论 / 进展与交付 / 风险与异常」三段展示，带刷新与缓存（私有目录 `boss_briefing.json`，断网上次可看） |
| 历史消息 → 待办事项 | `TodoActivity`：把简报「需要你拍板」拆成条目卡片（急/缓徽章、建议、后果说明），每条 2 个按钮「通过 / 不通过」；审批**不是本地改状态**，而是把决定合成语音走 `voice_chat` 交给小秘书执行，本机只记「已提交」 |
| 连接设置 → 系统设置 | `settings_title` 改为「系统设置」 |
| 去掉查看文字 | 首页删除 `btnOpenText`，4 个入口变 3 个（当前进度 / 待办事项 / 系统设置） |

架构说明：通话放在 Service 里，**切到别的页面或退到后台都不中断**，`VoiceCallActivity` 只是控制面板（头像、电平、字幕、红色挂断键）；只有点红色图标或挂断键才会 `live_call stop`。

### 24.3 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **87/87 PASS**（新增：首页三入口、通话图标、默认小秘书、Service 常驻、纯音频、变红可挂断、简报页、审批页、四段解析、Manifest 注册） |
| 简报解析实测 | 新增 `tools/VerifyBriefing.java`，用 `boss_briefing` **真实样本**跑 `Briefing.parse`：**14/14 PASS**（结论/进展 3 条/风险 2 条/待办 3 条、急件徽章、建议 A、编号剥离、批准与不批准指令） |
| Java 纯逻辑 | `VerifyStream.java` 45/45 |
| 双包校验 | `tools/verify_apk_v25.py` **82/82 PASS**（5 个 Activity、13 个类含 `Briefing`/`VoiceCallService`、6 个布局、dex 含 `boss_briefing`/`secretary`） |
| 包信息 | `versionCode 11 / 0.6.0 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.6.0-b11-release.apk` 391,875 B · `6a02f117…2ea5`；`dist/voice-mcp-v0.6.0-b11-debug.apk` 592,256 B · `626d47fe…f8b5` |

### 24.4 已知边界

- 简报生成是**真实巡检任务**（读 `/opt/sefs/obsidian`，30–120s），所以「当前进度」首次打开要等；已做缓存 + 10 分钟新鲜度判断。
- 审批走「合成语音 → `voice_chat`」，服务端执行结果要看下一轮简报；本机只标记已提交，不会假装已生效。
- 实时语音的下行延迟仍是 HLS 的 2–4s（已做直播边缘对齐）；上行受 `voice_chat` 推理耗时影响。
- 通话常驻前台服务需要通知权限；Android 14+ 已声明 `FOREGROUND_SERVICE_MICROPHONE`。

## 25. 同心圆 0.6.1：首页通话按 v2.5「纯语音模式」重做

服务端 `live_call` 已支持 `mode=audio`（纯 AAC 一路流、不说话也推静音保持连接），按第 12 章规格把首页主入口改成通话。

### 25.1 实测服务端

```
live_call{action:start, mode:audio, avatar_id:secretary}
→ session_id=lv_..., status=live, mode=audio, elapsed=2.21s, segments=2,
  sync="纯音频 HLS 直播流（AAC），不说话时推静音保持连接"
分片：1,504 B（视频模式同款分片是 108 KB），PMT 只有 0x0f(AAC)，没有 0x1b(H.264)
say{text:"您好，我在听。"} → speech_seconds=1.25, segments=7, mode=audio  ✅
```

起流 **2.2 秒**（不带 `text` 问候），比视频模式更快、带宽低两个数量级。

### 25.2 首页改造

| 项 | 现在 |
| --- | --- |
| 当前汇报人 | 默认 `secretary`（OPC-001 小秘书），副标题「点击通话，和小秘书实时对话」 |
| 主按钮 | 148dp 圆形 `btnCall`：`通话` → `连接中…` → **红色 `挂断`**（`bg_call_end`，`@color/call_red`），再点即挂断 |
| 状态行 | `未连接` / `正在接通…` / `通话中 00:12 · 小秘书` / `通话已结束` |
| 实时转写 | 新增卡片 `tvTranscribe`，逐行「我： …」「小秘书： …」（`VoiceCallService.lines()`，保留 40 行） |
| 语音通道 | 一路 `mode=audio` HLS 不断开；`MediaPlayer` 只解 AAC，不建 Surface |
| 用户说话 | `LiveMic` 连续采集 + 能量断句（静音 0.7s 收尾），每句 `voice_chat{live_session}` |
| 回复 | 服务端把「收到」和正式回复混进同一路流，解码即播 |
| 结束 | 随时点红色按钮；先 `player.stop()` 再 `live_call stop`（修掉「挂断后还在响」） |
| 旧文案 | 首页不再有「按下开始录音」「说完了，点击发送」「最长 60 秒」 |

取舍说明：文档 12.6 写「保留 查看文字 / 重播语音」，但上一轮你已把这两个入口换成「当前进度 / 待办事项」，因此本版以你的最新要求为准，只做通话改造。旧的「录音→发送」链路代码保留在 `MainActivity` 里，仅把按钮设为 `gone`（随时可恢复），首页主入口只有通话。

### 25.3 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **92/92 PASS**（新增：主按钮/状态/转写、`bg_call_end`、旧文案缺席、`mode=audio`、挂断顺序） |
| 简报解析 | `VerifyBriefing.java` 14/14 |
| Java 纯逻辑 | `VerifyStream.java` 45/45 |
| 双包校验 | `tools/verify_apk_v25.py` **84/84 PASS** |
| 包信息 | `versionCode 12 / 0.6.1 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.6.1-b12-release.apk` 393,699 B · `129393bd…c25c`；`dist/voice-mcp-v0.6.1-b12-debug.apk` 431,842 B · `9fd5da87…7412` |

### 25.4 边界

- 上行仍是 `voice_chat`：说一句 → ASR → agent → 回复混进流，agent 耗时几十秒时只能先听到「收到」。
- 一次只能说一句，第二句要等上一句处理完（单线程队列，v1 不支持打断）。
- 外放时靠 AEC 压回声，音量过大仍可能回灌。

## 26. 同心圆 0.6.2：按服务端排查清单修播放链路

服务端给了 4 条排查结论 + 一个 10 秒判定法，逐条落到代码：

| # | 服务端结论 | 客户端改法 |
| --- | --- | --- |
| 1 | 播放地址直接用 `hls_url_full`，不要再 `absolute()` 拼一遍（会拼成 `.../conring/asr/assets/conring/asr/...`） | `parseCall` 优先取 `hls_url_full`（兼容 `stream_url_full`）；只有相对路径时才补一次 origin。实测返回 `hls_url=/conring/asr/assets/live/lv_xxx/index.m3u8`、`hls_url_full=https://ai.boraydata.com/...`，两种都不再重复拼接 |
| 2 | `live.ended=true` 说明通话已挂断、回复没进流，必须播 `voice_job` 的音频；`ended=false` 只播流，别再播 audio 块（会重复） | 新增 `StreamContract.liveEnded()` + `audioOf()`；`sendUtterance` 先判 ended，`pollJob` 每轮也会更新 ended；结束时若 ended → `playLocally()`：先要 `voice_job{include_audio:true}` 的音频块写成本地 WAV 用 `VoicePlayer` 播，取不到才退化到系统 TTS。**ended=false 时不本地播任何音频** |
| 3 | `-hls_list_size 5` 滑动窗口，`MediaPlayer` 续播不可靠，播到列表末尾就 `onCompletion` | 实测确认：固定 5 片、`EXT-X-MEDIA-SEQUENCE` 0→2→4 递增、无 ENDLIST。因此**删除原来的「seek 到直播边缘」逻辑**（滑窗下总时长恒 ~5s，seek 只会疯狂跳），改成 `onCompletion`/停滞 6s → `reopenPlayer()` 用新时间戳重开**同一路流**，不重建 session（重建会把正在说话的那路丢掉）。视频通话页同样改为 `playback.replay()` |
| 4 | 服务端没设 `Cache-Control`，请求要带时间戳 | 新增 `StreamContract.cacheBust()`：`setDataSource`、playlist 探测、视频页 LIVE 打开全部带 `?t=<ms>` |
| — | 挂断后回复会丢 | `pollJob` 改跑在独立 `postIo` 线程池，挂断不关闭；用 `pendingJobs` 计数，全部播完才 `stopSelf()`（原来挂断即 `stopSelf()`，回复必丢） |

### 26.1 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **96/96 PASS**（新增：`hls_url_full` 优先、无二次拼接、只有相对路径也只拼一次、`cacheBust`、`live.ended` 识别与缺省、滑窗重开、ended 本地播放、挂断等待） |
| Java 纯逻辑 | `VerifyStream.java` **51/51 PASS** |
| 简报解析 | `VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **84/84 PASS** |
| 包信息 | `versionCode 13 / 0.6.2 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.6.2-b13-release.apk` 393,907 B · `85c20052…8658`；`dist/voice-mcp-v0.6.2-b13-debug.apk` 432,226 B · `5aa0ffa0…5c42` |

### 26.2 真机自检（对应「10 秒判定法」）

1. 手机浏览器/VLC 直接打开 `hls_url_full`：**有声音** → 服务端无责，问题在 App 播放器（看 `adb logcat -s VoiceCall`，`reopen stream:` 会打印重开原因）。
2. **没声音** → 服务端/公网反代问题，把这条 URL 发回来。
3. 挂断后如果还听到员工说完这句，说明 `live.ended=true` 的本地兜底生效了（正常行为）。

## 27. 同心圆 0.6.3：员工档案列表拆成「在册员工 + 老板专栏」

| 需求 | 实现 |
| --- | --- |
| 在册员工只列除老板外所有人 | `isBoss()` 判定（`id=boss` / 工号 `OPC-000` / 岗位是「老板」），命中的人不进主列表；`tvCount` 只统计在册员工（当前 18 位） |
| 页面最后增加「老板」专栏，只有老板本人 | 主列表后加分隔线 + 「老板」标题 + `OPC-000 · 董事长办公室` 小字 + `llBoss` 容器，只放一条老板卡片，样式与员工卡一致、可点进详情；没有老板档案时整块隐藏 |
| 去掉页面头部介绍性文字 | 删掉「每位数字员工都有独立形象…」那段说明卡 |
| 「从服务端同步档案」→「立即同步」 | 按钮文案改为「立即同步」，同步逻辑不变（`EmployeeStore.syncFromServer`） |

抽出了 `row(Employee, current, parent)` 供两处复用，避免两份渲染代码走偏。

### 27.1 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **101/101 PASS**（新增 5 条：介绍文字已删、按钮文案、老板分块、主列表排除老板、老板判定规则） |
| Java 纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **84/84 PASS** |
| 包信息 | `versionCode 14 / 0.6.3 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.6.3-b14-release.apk` 394,279 B · `0049798c…73f6`；`dist/voice-mcp-v0.6.3-b14-debug.apk` 432,738 B · `091a1da6…32ac` |

## 28. 同心圆 0.6.4：员工详情页重排

新的信息顺序（从上到下）：

| 顺序 | 区块 | 说明 |
| --- | --- | --- |
| 1 | **照片 + 姓名 + 岗位简介** | 86×106dp 的照片（`bg_avatar` 圆角）、姓名 20sp 加粗、`职位 · 部门`、口头禅，合成一张顶部卡片 |
| 2 | **动态视频区域** | 原舞台保持不变：`CoverVideoView` 铺满裁切 + 首帧海报 + 播放/暂停提示 + 右下重播，仍按 3:4 自适应高度 |
| 3 | **发起实时通话** | 主行动按钮（实心 `bg_pill`），文案「发起实时通话」 |
| 4 | **员工资料** | 卡片：简介（`tvBio`）+ 分隔线 + 擅长/性别年龄音色（`tvSkills`）+ 形象素材状态（`tvVideoState`） |
| 5 | **设为当前汇报人** | 描边按钮，放在最末 |

同时删除：
- 「试听汇报（真实语音驱动口型 / 表情 / 动作）」按钮及其整段 `PreviewAudio` 逻辑；
- 该按钮下方的说明文字 `tvStreamState`（`refreshStreamState()` 已置空，调用点不受影响）。

这样详情页只剩「看人 → 看视频 → 打电话 → 看资料 → 设为汇报人」一条主线，没有需要读的说明段。

### 28.1 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **104/104 PASS**（新增：详情页区块顺序按索引校验、`btnPreview`/`tvStreamState` 已移除、通话按钮文案） |
| Java 纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **84/84 PASS** |
| 包信息 | `versionCode 15 / 0.6.4 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.6.4-b15-release.apk` 393,199 B · `846ca828…c2ce`；`dist/voice-mcp-v0.6.4-b15-debug.apk` 431,522 B · `4363b24f…78bb` |

## 29. 同心圆 0.6.5：老板档案不显示功能按钮

老板本人就是这个 App 的用户，所以他自己的档案页不该出现「发起实时通话」「设为当前汇报人」。

| 项 | 改动 |
| --- | --- |
| 老板判定 | 收进 `Employee.isBoss()`（`id=boss` / 工号 `OPC-000` / 岗位或角色是「老板」），员工列表与详情页共用同一套口径，避免两处漂移 |
| 详情页按钮 | `emp.isBoss()` 时 `btnCall` 与 `btnSetCurrent` 直接 `View.GONE`，且**不绑点击事件**（不是设了禁用还留着） |
| 详情页标题 | 老板显示「老板档案」，其他员工仍是「员工档案」 |
| 兜底 | `refreshCurrentButton()` 对老板提前返回，不会出现「✓ 当前汇报员」字样 |

其他区块（照片姓名、动态视频、员工资料）对老板照常展示，只是没有可操作按钮。

### 29.1 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **107/107 PASS**（新增 3 条：老板隐藏两个按钮、标题「老板档案」、refreshCurrentButton 提前返回；老板判定断言改为查 `Employee.java`） |
| Java 纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **84/84 PASS** |
| 包信息 | `versionCode 16 / 0.6.5 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.6.5-b16-release.apk` 393,327 B · `a303bd40…1b2a`；`dist/voice-mcp-v0.6.5-b16-debug.apk` 431,626 B · `62880107…7b01` |

## 30. 同心圆 0.6.6：档案页视频舞台「正好铺满」，不再露背景

### 30.1 根因

服务端介绍视频是 **720×960（3:4 竖版）**，而舞台原来是「屏宽 × 3:4 宽的比例」（即 4:3 横版）。`VideoView` 的 Surface 默认 **等比缩放并留边（scale to fit）**，容器比例和视频比例不等时，画面只能在中间放一份，左右（或上下）留出空白，露出舞台的渐变背景——就是看到的那圈「视频边上的空白」。

`CoverVideoView` 本来想用「把 View 量得比容器大、再由父容器裁掉溢出」来兜住，但两个前提并不成立：`getVideoWidth()` 在部分机型 `onPrepared` 时仍是 0（拿不到尺寸就一直按容器量），而且 SurfaceView 的 Surface 不保证被父容器裁切。

### 30.2 改法：让容器比例 == 视频比例

| 改动 | 说明 |
| --- | --- |
| 舞台全宽出血 | `layout_marginLeft/Right="-18dp"` 抵消内容区的 18dp 内缩，视频直接顶到屏幕两侧；背景换成 `bg_stage_bleed`（无圆角，避免四角被削） |
| 高度按视频比例算 | `fitStageTo(vw, vh)`：`h = 舞台实际宽度 × vh / vw`。比例完全一致时 Surface 就是 1:1 铺满——**不留白、也不裁切、不变形** |
| 尺寸拿不到就轮询 | `fitStageFromPlayer()`：`getVideoWidth()` 为 0 时每 80ms 重试，最多 12 次 |
| 布局完成再校正 | 首帧前 `stage.getWidth()` 还是 0，先按屏宽算，`stage.post()` 里用真实宽度重算一次，避免负边距在个别 ROM 上表现不同导致差几像素 |
| 去掉高度百分比上限 | 原来 `min(w*3/4, 58%屏高)` 会破坏比例；现在严格按比例，页面本来就可滚动 |

实测服务端两个视频：`ceo.mp4` 与 `secretary.mp4` 都是 `tkhd 720×960`、时长 21.16s / 19.2s。

### 30.3 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **111/111 PASS**（新增 4 条：全宽出血负边距、按视频比例算高且无百分比上限、尺寸 0 时轮询、出血背景无圆角） |
| Java 纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **84/84 PASS** |
| 包信息 | `versionCode 17 / 0.6.6 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.6.6-b17-release.apk` 394,347 B · `acf601f7…e807`；`dist/voice-mcp-v0.6.6-b17-debug.apk` 432,879 B · `ffc8b9ed…b5b6`（`clean` 后重建，debug 曾有 classes2.dex 是增量构建残留） |

### 30.4 取舍

- 视频块现在是**满屏宽 × 3:4**（20:9 机型约 60% 屏高），所以「发起实时通话」在部分手机上会落到首屏下方，需要滑一点。想更矮就得裁切画面（上下各切一条），要的话我改成裁切版。
- 满宽出血后舞台没有圆角（视频是方的，圆角会被视频盖掉）；如果更想保持卡片式圆角，可以改成「保留 18dp 内缩 + 视频精确填满卡片」，代价是四角会有轻微方角。

## 31. 同心圆 0.7.0：通话界面按汇报人联动 + 接通后收成一条 + 转写加大

### 31.1 汇报人是谁，就显示谁

| 位置 | 之前 | 现在 |
| --- | --- | --- |
| 首页提示 | 写死「点击通话，和小秘书实时对话」 | `tvCallHint` 由代码填：`点击通话，和 {当前汇报人} 实时对话`，`refreshEmployeeCard()` / `applyReporterLabels()` 里同步 |
| 接通中状态 | `正在接通小秘书…` | `正在接通 {汇报人}…` |
| 通话中状态 | `通话中 00:12 · 小秘书` | 时长独立显示（见下），不再拼死名字 |
| 转写称呼 | 服务端按「小秘书」写死 | 服务端只存空串，界面按当前汇报人渲染，切人后旧记录称呼也跟着改 |
| **实际拨给谁** | `VoiceCallService.start()` 不带 `employee_id` → 服务端永远给小秘书 | 带上 `EmployeeStore.current()` 的 id，切了汇报人就拨给他 |

最后一条是这次顺手发现的真 bug：之前切换汇报人只影响「汇报画面」，通话始终打到小秘书。

### 31.2 接通后收成一条：时长 · 音量 · 挂断

未接通时保持原来的大圆钮 + 提示文案；一旦 `connected`：

- hero 从 **264dp 收到 104dp**（`heroCompactHeight`），大圆钮、呼吸光环、`点击通话…` 提示、底部状态全部隐藏；
- 显示 `llCallBar` 一行：**通话时长（20sp 等宽数字） · 音量动态条 · 红色挂断键**；
- 挂断键与其他入口共用 `toggleCall()`，点一下立刻断开。
- 刷新频率：未接通 800ms；接通后提到 **100ms**，专门驱动音量动画。

新增 `VoiceLevelView`（纯 Canvas，零依赖）：23 根竖条，高度 = 实时音量 × 固定「频谱形状」（两个不同周期正弦叠加，避免整排一样高），上升快回落慢；没人说话时给 5%~9% 的轻微呼吸，不会显得整排死平。颜色跟随主色。

### 31.3 实时转写加大

| 项 | 之前 | 现在 |
| --- | --- | --- |
| 字号 | 12sp | **15sp**，行距 7dp |
| 可见长度 | `maxLines=6` 截断 | 固定 168dp 的可滚动区（`svTranscribe`），不再截断 |
| 内容量 | 40 行 × 160 字 | **60 行 × 300 字** |
| 跟随 | 不动 | 每次更新自动 `fullScroll(FOCUS_DOWN)`，始终看到最新一句 |
| 样式 | 纯文本 `我： …` / `小秘书： …` | `Spannable`：说话人加粗着色（我=次要色、对方=主色），内容正常色 |

为了避免每 100ms 重建文本，`renderTranscribe()` 用纯文本做「内容指纹」，没变化就直接 return。

### 31.4 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **118/118 PASS**（新增 7 条：提示随汇报人、通话带 employee_id、转写称呼按人渲染、紧凑条三件套、接通后隐藏提示与大圆钮、100ms 电平刷新、转写更大可滚动） |
| Java 纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **86/86 PASS**（新增 `VoiceLevelView` 类检查） |
| 包信息 | `versionCode 18 / 0.7.0 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.7.0-b18-release.apk` 396,767 B · `9046501f…ea39`；`dist/voice-mcp-v0.7.0-b18-debug.apk` 436,031 B · `2bb22566…d2ac` |

### 31.5 真机待确认

- 音量条读的是**麦克风电平**（服务端不提供员工侧音量），所以对方说话时音频条基本只做轻微呼吸；如果要真的「跟着对方的声音跳」，需要服务端在流里给电平和字幕（`voice_job` 已能出字幕，电平还没有）。
- 转写区固定 168dp，屏幕很矮时可再压到 140dp。

## 32. 同心圆 0.7.1：通话块更紧凑 + 转写真正实时

### 32.1 间距整体收紧

| 位置 | 之前 | 现在 |
| --- | --- | --- |
| 接通后通话块高度 | 104dp | **84dp** |
| hero 上边距 | 16dp | 10dp |
| 通话条左右内边距 | 22dp | **14dp** |
| 计时字号 / 最小宽 | 20sp / 62dp | 17sp / 50dp |
| 音量条高度 / 左右间距 | 38dp / 14·18dp | **28dp / 10·12dp** |
| 挂断键 / 图标 | 56dp / 26dp | **46dp / 21dp** |
| 转写卡上边距 / 内边距 | 14 / 14·12dp | **10 / 11·9dp** |
| 转写可视高度 | 168dp | 150dp |

### 32.2 转写为什么之前"不同步"

原因是**只在拿到整轮结果时才加行**：一句话说完 → 等 `voice_chat` 返回（实测 **4.0s**，里面含 ASR + ack TTS）→ 才出现「我」；正式回复要等 job 跑完（十几秒~几十秒）→ 才出现第二行。中间界面什么都不动。

现在改成**占位行原地替换**：

| 时机 | 界面 |
| --- | --- |
| 说完一句（VAD 收尾，~0.1s） | 立刻出现 `我：（识别中·）`，点随时间跳动 |
| `voice_chat` 返回（~4s） | 同一行原地变成你的原话；同时出现 `对方：<ack>` |
| 同时 | 再挂一行 `对方：（处理中··）` |
| job 完成 | 该占位行原地换成正式回复 |

配套改动：转写数据从 `String[]` 换成 `Line{fromUser, text, pending}`；`VoiceCallService` 新增 `LineListener`，**每次变化主动回调**（`UI.post`），不再依赖 800ms/100ms 定时器；同一句话不会再"占位一条 + 正式一条"重复。

### 32.3 员工侧电平（等服务端）

服务端还在完善员工侧电平。客户端先埋好兼容：`readRemoteLevel()` 会从 `voice_chat` ack 与 `voice_job` 返回里读 `level / speech_level / audio_level / energy / volume`（兼容 0–1 与 0–100 两种量纲，`live/result/ack/speech` 子对象也认），读不到就是 0，3 秒没更新自动归零。音量条取 `max(自己, 员工侧)`，所以服务端一旦下发，界面自动跟着跳，不用再改客户端。

顺带实测确认：单独的 `asr_transcribe` 首调 **6.3s**，比 `voice_chat` 自己的 4.0s ack 还慢，所以没有走"并行 ASR 抢先出字"，占位方案更合适。

### 32.4 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **124/124 PASS**（新增 6 条：说完即出「我」、原地替换、变更回调、占位跳动、员工侧电平兼容、通话块收紧） |
| Java 纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **86/86 PASS** |
| 包信息 | `versionCode 19 / 0.7.1 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.7.1-b19-release.apk` 398,343 B · `069b681e…8042`；`dist/voice-mcp-v0.7.1-b19-debug.apk` 437,651 B · `aae229d7…6c67` |

### 32.5 顺带发现

服务端 `profile` 又新增了 3 人（`vd_wenren`、`vd_tongxinyuan`、`scout`），**介绍视频还没生成**（`introVideo` 为空、`introReady=false`）。所以在线校验从"每人都有"改成"覆盖率 + 已生成者逐条 Range"，现在 16/19 就绪。这三人进档案页会走「仿真人形象视频 → 内置数字人」的降级，属预期行为。

## 33. 同心圆 0.7.2：通话期间不让屏幕自动熄灭

### 33.1 做法

新增 `ScreenOn`（20 行）：用窗口标志 `FLAG_KEEP_SCREEN_ON`，**不用 WakeLock**。

- 不申请任何权限（WakeLock 要 `WAKE_LOCK`，而且必须手动释放，忘了就耗电）；
- 只在页面可见时生效，用户切走/退出后系统自动恢复正常息屏策略；
- 通话结束立刻 `clearFlags`。

接入三处：

| 页面 | 行为 |
| --- | --- |
| 首页 `MainActivity` | `applyCallState()` 里跟随 `VoiceCallService.isActive()`（含「接通中」），`onDestroy` 清除 |
| 通话面板 `VoiceCallActivity` | `onResume` 与 400ms tick 里跟随通话状态，销毁清除 |
| 视频通话页 `LiveCallActivity` | `enterLive()` 置位，`endCall()` / `onDestroy()` 清除 |

所以「首页点通话 → 页面停留多久都不会黑屏」，通话跑到后台时（首页不可见）屏幕照常按系统策略息屏，不会白白亮着。

### 33.2 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **115 PASS / 0 FAIL**（新增 4 条：FLAG 常亮且不用 PowerManager/WakeLock、首页跟随通话状态、面板与视频页同样常亮、Manifest 无 `WAKE_LOCK`） |
| Java 纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **86/86 PASS** |
| 包信息 | `versionCode 20 / 0.7.2 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.7.2-b20-release.apk` 398,531 B · `8ee4dff3…8281`；`dist/voice-mcp-v0.7.2-b20-debug.apk` 437,979 B · `3da81c50…fce7` |

注：本轮在线通话链路校验被 **SKIP**（服务端提示「实时通话会话已达上限 8，请先挂断」——之前探测留下的会话还没过期），与客户端无关；静态与双包校验全部通过。

## 34. 同心圆 0.8.0：拨号音 · 已接通提示 · 接通中动画 · 去掉「未连接」

### 34.1 首页状态文字全部撤掉

| 位置 | 之前 | 现在 |
| --- | --- | --- |
| 主按钮下方 | `未连接` / `正在接通 xxx…` / `通话中` | **整条删除**（`tvCallStatus` 已移除） |
| 转写区空态（接通中） | `正在接通 xxx…` | **留空** |
| 顶部提示（接通中） | `点击通话，和 xxx 实时对话` | **隐藏**，只在空闲时显示 |
| 顶部提示（接通后） | — | 短暂显示 `已接通` 1.6s 后自动消失 |

状态现在完全靠视觉表达：空闲=大圆钮、接通中=呼吸+转圈、通话中=紧凑通话条。

### 34.2 拨号音与接通提示音

新增 `CallTone`（约 140 行），**本地合成正弦波**，不带任何音频文件、不增 APK 体积：

| 场景 | 声音 |
| --- | --- |
| 点「通话」后 | 440Hz 0.55s（嘟）+ 静音 1.25s，循环直到接通 |
| 接通瞬间 | 660Hz 110ms + 880Hz 160ms 两声上行短音 |
| 挂断 / 失败 / 切到后台 | 立刻停止（不在后台响） |

合成时加了 20ms 淡入淡出，避免起停爆音；`AudioTrack` 每次用完即 `stop/release`，不留句柄。

### 34.3 接通中动画

接通中（已请求、未接通）：

- `connectRing` 进度环绕着 148dp 大圆钮转；
- 圆钮 alpha 在 0.45↔1.0 之间呼吸（620ms 一个来回）；
- 接通的瞬间动画停止、圆钮淡出，整块收成 84dp 的「时长 · 音量 · 挂断」条。

### 34.4 顺带修掉一个真实隐患

原来 `startPlayer()` 一开始就 `setMicRunning(true)`，也就是**起流等待期麦克风是开着的**。加入拨号音后，这 2–4 秒的「嘟——」会被 VAD 当成人声录下来发给服务端。现在改成**真正 `onPrepared` 之后再开麦**，拨号音不会再被当成一句话。

### 34.5 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **133/133 PASS**（新增 5 条：无「未连接」、拨号音/接通音、已接通提示自动消失、接通中只留动画、无「正在接通 xxx」） |
| Java 纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **90/90 PASS**（新增 `CallTone`、`ScreenOn` 两个类检查） |
| 包信息 | `versionCode 21 / 0.8.0 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.8.0-b21-release.apk` 400,247 B · `92486793…40cb`；`dist/voice-mcp-v0.8.0-b21-debug.apk` 440,483 B · `96799638…b0a4` |

另外把校验脚本对**在线**部分做了容错：服务端会话占满、连接被关、请求超时都记为 SKIP 而不是 FAIL（这几轮实测都撞到过），静态与双包校验仍然必须全绿。

## 35. 同心圆 0.8.1：员工侧电平（level.json）接上，音量条真正跟着对方的声音跳

按 12.9 的契约实现，服务端已上线（实测 `live_call start` 返回 `level_url` / `level_url_full` / `level_fps` / `level_window_sec`）。

### 35.1 服务端实测

```
live_call{action:start, mode:audio, avatar_id:secretary}
→ level_url      = /conring/asr/assets/live/lv_4f206b09/level.json
  level_url_full = https://ai.boraydata.com/conring/asr/assets/live/lv_4f206b09/level.json
  level_fps      = 10      segment_sec = 1.0

GET level.json →
{session_id, seq:30, fps:10, t0:0.0, edge:6.0, wall:..., elapsed:6.22, status:"live", lv:[0..100]}
lv 长度 60（= edge × fps），本次峰值 88
```

### 35.2 客户端实现（`LiveLevelMeter`）

| 环节 | 做法 |
| --- | --- |
| 地址 | 优先用 `level_url_full`；`voice_chat` 的 `live` 里没有该字段时按 `index.m3u8 → level.json` 推导 |
| 锚点 | `播放位置 0 对应媒体时间 = EXT-X-MEDIA-SEQUENCE × 单片时长`，单片时长取窗口内**最大** `EXTINF`（末片常被截断成 0.883，用均值会偏 1 秒以上） |
| 采样 | `mediaT = anchor + getCurrentPosition()/1000`，查 `lv[(mediaT-t0)*fps]`，格间线性插值，超过 `edge` 不查未来 |
| 存活 | 3 秒没有新 `seq` 或 `status=ended` → 归零 |
| 重载 | 播放位置回跳 >0.5s 视为播放器重载，重新读播放列表锚定 |
| 安全 | 只访问与播放列表**同源**的地址（异域名/异端口一律拒绝），轮询线程异常不退出 |

接入 `VoiceCallService`：`onPrepared → mp.start() → startLevelMeter(mp)`；`reopenPlayer` / 结束时 `stopLevelMeter()`。电平写入 `remoteLevel`，首页音量条取 `max(自己麦克风, 员工侧)`——**双方谁说话都跳**。

### 35.3 离线验算（服务端实测会话 `lv_742062b5`）

新增 `tools/VerifyLevel.java`，用文档 12.9.2 的数据跑纯逻辑：

| 断言 | 结果 |
| --- | --- |
| 首片起始 `46 × 0.998456 = 45.929s` | ✅（不是 46.0，也不是均值 44.87） |
| 末片截断不影响锚点（取最大 EXTINF） | ✅ |
| `mediaT 45.929 → 53.2%` / `46.927 → 82.9%` / `47.929 → 0` | ✅ |
| 超过 `edge` 取尾格、早于 `t0` 取首格、两格中点插值、空/null 安全 | ✅ |
| 同源放行 / 异域名拒绝 / 异端口拒绝 / 空地址拒绝 | ✅ |

### 35.4 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **127 PASS / 0 FAIL**（新增 4 条静态 + `VerifyLevel` **16/16**） |
| Java 纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **92/92 PASS**（新增 `LiveLevelMeter` 类检查） |
| 包信息 | `versionCode 22 / 0.8.1 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.8.1-b22-release.apk` 403,799 B · `5ed3ca1e…9fbc`；`dist/voice-mcp-v0.8.1-b22-debug.apk` 444,443 B · `127b8a47…71de` |

### 35.5 真机需要校准的一点

Android `MediaPlayer` 对 HLS 的 `getCurrentPosition()` 是相对**本次播放起点**的，`LiveLevelMeter` 假设「0 = 起播时播放列表首片」。联调时看这行日志即可判断：

```
adb logcat -s LiveLevel
# meter start, anchor=45.929 url=https://...
```

若发现 `mediaT` 明显落后/超前 `edge`，只需把 `anchorMedia` 改成「首次 `pos > 0` 时的 `seq × 单片时长`」，采样逻辑不用动。

## 36. 同心圆 0.8.2：按 12.9 参考实现校对并修正 5 处

### 36.1 逐条校对结果

| 契约/参考实现 | 本工程 | 结论 |
| --- | --- | --- |
| `level_url_full` 优先，缺省用 `hls_url_full` 把 `index.m3u8→level.json` | 一致（`StreamContract.CallSession.levelUrl`，服务没给时按同规则推导） | ✅ |
| 锚点 = `MEDIA-SEQUENCE × 单片时长`，单片时长取窗口**最大** `EXTINF` | 一致 | ✅ |
| `mediaT = anchor + pos/1000`，超 `edge` 不查未来 | 一致（`edge=0` 时不裁剪，比参考实现更稳） | ✅ |
| 格间线性插值、越界取首/尾格 | 一致 | ✅ |
| 3 秒无新 `seq` / `status=ended` → 0 | 一致 | ✅ |
| 位置回跳 >0.5s 视为重载，重新锚定 | 一致 | ✅ |
| `lv` 0~100 → 0~1 | 一致 | ✅ |
| 轮询 150ms（服务端 200ms 重写） | **原先写成 200ms**（与重写同频、容易撞拍读到同一帧） | 已改为 150ms |
| 播放列表读不到时 | 参考实现 `return anchorMedia`（保留旧值），**我第一版返回 0** → 整条电平会掉到开头的静音格 | 已改为保留旧锚点 |
| 单次 `GET level.json` 失败 | 参考实现直接 `post(0f)`（弱网会闪 0） | 改为 **保持上一帧**，超过 3 秒才归零 |
| 联调日志 | 参考实现给了 `pos/seq/t0/edge` 一行 | 已加，2 秒一条：`LiveLevel` 里 `pos=…ms mediaT=… edge=… seq=… anchor=…` |
| 同源校验 | 参考实现未做 | 保留本工程的 `sameOrigin()`（异域名/异端口一律拒绝） |

另外修了一个会「打架」的小问题：`VoiceCallService.readRemoteLevel()`（早期对 ack/job 里 `level` 字段的启发式兜底）之前用 `optDouble`，遇到 `"level":"high"` 这类非数字会得到 `0` 并覆盖实时电平。现在只认 `instanceof Number`。

### 36.2 用真实会话再验一次锚点（不是只用文档里的样例）

```
start + say 后等待 9s：
seq=23  maxEXTINF=0.998467  anchor=22.965
fps=10  t0=0.0  edge=28.4  len=284   ← len == edge*fps（完全吻合）
非零格区间 idx 1..228 → 0.1s ~ 22.8s
pos=0s → mediaT=22.965 → 0.0
```

结论：`len == edge × fps` 成立，说明索引换算自洽；`anchor(22.965)` 已经落在语音段（≤22.8s）之后，所以此刻取到的是静音——这是**正确**的（窗口已经滚过那句话），不是错位。

### 36.3 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **142 PASS / 0 FAIL**（新增：150ms、失败不归零、锚点保留、联调日志） |
| 电平纯逻辑 | `VerifyLevel.java` **16/16**（锚点 45.929、三处取样、插值/边界、同源/异域名/异端口） |
| Java 纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **92/92 PASS** |
| 包信息 | `versionCode 23 / 0.8.2 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.8.2-b23-release.apk` 404,131 B · `a7d0ea10…7a9b`；`dist/voice-mcp-v0.8.2-b23-debug.apk` 444,731 B · `615c3dff…6c40` |

## 37. 同心圆 0.9.0：实时转写与语音同步（并行 ASR + 按声音到达显示）

### 37.1 为什么之前不同步

实测了三条链路的真实耗时：

| 环节 | 实测 | 后果 |
| --- | --- | --- |
| `voice_chat` 回执（含 ASR + 「收到」TTS） | **4.0s**；遇到低置信澄清（`clarify`）**25.9s** | 自己说的话要等这么久才出字 |
| 单独 `asr_transcribe` | 热态 **1.7s**（冷启 6.3s） | 明显更快，但当初没并行调 |
| 正式回复 | 等 agent 执行完，十几秒~几十秒 | 文字要么早于声音、要么晚于声音 |

另外发现一个真问题：`voice_chat` 带 `live_session` 时，**`live` 字段可能是 `null`**（会话没混上流）。旧逻辑只在 `live.ended=true` 时本地补播，于是会出现「有文字、没声音」。

### 37.2 三条改法

| 改法 | 说明 |
| --- | --- |
| **自己说的话：并行 ASR** | 一句话发出去的同时，用独立线程池（`newFixedThreadPool(2)`，不占 job 轮询的单线程）调 `asr_transcribe`，**约 1.7s** 就把「我：…」填上；`voice_chat` 的 `question` 只作兜底，不再覆盖已有文本 |
| **对方的文字：等声音真的响起来** | `LiveLevelMeter` 新增 `lagMs() = (edge − (anchor + pos)) × 1000`，即「客户端还落后直播边缘多少」。ack / 正式回复 / 开场白都用 `showSpokenText()` 按这个滞后延后显示（上限 8s），拿不到电平快照时退回立即显示 |
| **没混进流就本地补播** | 记录 `mixed = ack 或任一 job 轮询里出现过 live 对象`；完成时 `ended || !mixed` → 走 `playLocally()`（先取 `voice_job` 音频，取不到退系统 TTS），避免「有字无声」 |

起播瞬间电平计还没拿到第一帧，`showSpokenText` 会最多等 1.2s（4×300ms）再决定延后多少，不会因为「暂无数据」把文字提前甩出去。

### 37.3 关于「服务端同步发文本」

你提的方案（server 在 TTS / 推流时把对应文本一起发过来）是最干净的：客户端拿到「文本 + 音频入流时刻」就能零猜测地对齐。现在服务端**没有**这个字段（`live` 里只有 `session_id / hls_url / hls_url_full / mode`，`ack` 只带 `segments[{text,start,end}]` 相对时间）。

所以本版用服务端已有的 `level.json` 反推「声音还要多久才播到」，等价于拿 `edge` 当“音频入流时刻”的代理。等 server 真的下发 `speech_at_seconds`（或每句文本带媒体时间），把 `showSpokenText` 里的 `lagMs()` 换成 `anchor + speech_at − mediaT` 即可，其余不动。

### 37.4 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **146 PASS / 0 FAIL**（新增 4 条：并行 ASR、按声音到达显示、live 缺失本地补播、双线程池互不阻塞） |
| 电平纯逻辑 | `VerifyLevel.java` 16/16 |
| Java 纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **92/92 PASS** |
| 包信息 | `versionCode 24 / 0.9.0 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.9.0-b24-release.apk` 405,699 B · `62376840…bf0e`；`dist/voice-mcp-v0.9.0-b24-debug.apk` 446,411 B · `c1845c06…652d` |

### 37.5 真机预期

- 说完一句 → **约 1.7s**「我：…」出字（不再等 4s 或澄清时的 26s）。
- 员工开口时文字才出现（对齐 lag），不会再出现「字先说、声音后到」。
- 抓 `adb logcat -s VoiceCall LiveLevel`：能看到 `early asr: …`、`reply text delayed 2400ms to match audio`、`local playback, ended=false mixed=false`。

## 38. 同心圆 0.9.1：修「点通话没有拨号音」

### 38.1 根因（真 bug，不是音量问题）

`toggleCall()` 里是这么写的：

```java
VoiceCallService.start(this);     // startForegroundService：异步，active 还没置位
applyCallState();
renderCallUi();                   // 同步执行 → isActive() 仍是 false
```

而 `renderCallUi()` 末尾有：

```java
if (!active) { ringing = false; tone.stop(); }
```

于是**点下通话后约 10ms，拨号音就被这行代码停掉了**——`CallTone.startDialing()` 刚把线程拉起来就被 `stop()`，听起来就是「完全没有拨号音」。服务随后（几十~几百 ms）才把 `active=true`，但音已经停了，`ringing` 也被清掉，不会自己回来。

### 38.2 修法

| 改动 | 说明 |
| --- | --- |
| 记录 `callRequestedAt` | 点通话那一刻打时间戳 |
| `!active` 分支加 3 秒宽限 | `boolean starting = ringing && now - callRequestedAt < 3000;` 宽限内**不停**拨号音；超过 3 秒还没 active 才判定启动失败、停止发声 |
| 顺序调整 | 先 `VoiceCallService.start()` 再 `tone.startDialing()`，避免服务侧的音频模式切换与建轨抢先后 |
| `ToneGenerator` 兜底 | `AudioTrack` 建不出来（个别 ROM 在通话音频模式下不让建）时退回系统 `ToneGenerator.TONE_SUP_DIAL`，不再静音摆烂 |
| 日志 | `CallTone: startDialing`、`AudioTrack 失败，退回 ToneGenerator: …`，真机一条 `adb logcat -s CallTone` 就能定位 |

### 38.3 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **148 PASS / 0 FAIL**（新增 2 条：3s 宽限、ToneGenerator 兜底） |
| 电平 / 简报 / 流契约 | `VerifyLevel` 16/16、`VerifyBriefing` 14/14、`VerifyStream` 51/51 |
| 双包校验 | `tools/verify_apk_v25.py` **92/92 PASS** |
| 包信息 | `versionCode 25 / 0.9.1 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.9.1-b25-release.apk` 406,251 B · `98486ede…1a9e`；`dist/voice-mcp-v0.9.1-b25-debug.apk` 446,815 B · `6fca7716…3d5c` |

装上后点通话应当规律地听到「嘟——（0.55s）…停 1.25s…嘟——」，接通瞬间换成两声上行短音。

## 39. 同心圆 0.9.2：拨号音「听不到」——没有音频文件，是音量和声道的问题

### 39.1 先回答：不存在声音文件

拨号音是**本地实时合成**的正弦波（`CallTone`），APK 里没有也不需要 wav/mp3。所以「没有对应的声音文件」这条排除。

真正的问题是两点叠加：

| 问题 | 原来 | 现在 |
| --- | --- | --- |
| **太轻** | 幅度 `0.22`（约 −13 dBFS），单音 440Hz | `0.60`（约 −4.4 dBFS），**双音 440+480Hz**（经典回铃音色），`setVolume(1f)` |
| **走错声道** | `USAGE_MEDIA`。通话期间系统已切到 `MODE_IN_COMMUNICATION` + 外放，媒体轨会被压到耳筒或被策略压低 | `USAGE_VOICE_COMMUNICATION`，跟着通话路由走（本 App 通话时强制外放） |
| 通话音量为 0 | 什么都听不到，也不提示 | 只在**确实为 0** 时临时抬到 40%，页面离开还原（`MODIFY_AUDIO_SETTINGS`，普通权限安装即授） |
| 切换时机 | 声音可能在模式切换前就出来，落在听筒上 | 起播前先等 **350ms** 让服务把音频模式/外放落定 |

另外每个 beep 现在都会打日志，能直接看出是"没建成功"还是"写进去了但没听见"：

```
adb logcat -s CallTone
# startDialing vol voice=5/15 music=9/15 mode=3 speaker=true
# beep 600ms freqs=[440, 480] written=26460 state=0
# （state=0 表示 STATE_INITIALIZED；失败会打 "AudioTrack 失败，退回 ToneGenerator: …"）
```

### 39.2 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **137 PASS / 0 FAIL**（新增 2 条：通话声道/幅度/双音、音量为 0 时抬升与还原；本轮在线段因服务端不可用 Skip） |
| 电平 / 简报 / 流契约 | `VerifyLevel` 16/16、`VerifyBriefing` 14/14、`VerifyStream` 51/51 |
| 双包校验 | `tools/verify_apk_v25.py` **92/92 PASS** |
| 包信息 | `versionCode 26 / 0.9.2 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.9.2-b26-release.apk` 407,383 B · `bafa5e01…6f3a`；`dist/voice-mcp-v0.9.2-b26-debug.apk` 447,987 B · `3a703417…d09f` |

### 39.3 如果还是听不到

按日志分三种情况：

1. 没有 `startDialing` → 点击没进到通话分支（状态机问题，把日志发我）。
2. 有 `startDialing`、没有 `beep ... written=...` → 音频轨建不起来（会同时出现 `退回 ToneGenerator`）。
3. 有 `beep ... written>0` 但听不见 → 声道/音量问题：看那行 `vol voice=… mode=… speaker=…`，
   - `voice=0` → 抬升没生效（个别 ROM 限制），需要用户手动加一档；
   - `speaker=false` → 外放没打开；
   - 都正常 → 那就是机型把通话声道也压了，只能改回 `USAGE_MEDIA` 或改用系统 `Ringtone` 播放。

## 40. 同心圆 0.9.3：拨号音换成「铃声感」的旋律（类似微信语音通话）

### 40.1 从「电信单音」改成「叮咚旋律」

原来是一个平直的双音（440+480Hz）长嘟，音色偏电信回铃、也比较容易被当成“没声音”。现在改成带包络的旋律：

| 时机 | 声音 |
| --- | --- |
| 拨号等待 | 一小节三音下行「叮 - 叮 - 咚」：**988Hz → 784Hz → 659Hz**（150/150/240ms，音间 90ms），每 **1.9s** 重复 |
| 接通瞬间 | 两音上行「叮 - 咚」：**784Hz → 1047Hz**（130/220ms） |

音色处理（这是“像微信那种”的关键，不只是换频率）：

- **基频 + 25% 二次谐波**，比纯正弦更“亮”更像铃；
- **8ms 起音 + 指数衰减**（`exp(-3.2t)`，120ms 掉到约 35%）+ 30ms 收尾，听起来是“敲一下铃”而不是“按住哨子”；
- 幅度 0.45（带衰减后峰值仍清晰，但不刺耳）。

路由与音量沿用 0.9.2 的修正：`USAGE_VOICE_COMMUNICATION`（跟随通话外放）、`setVolume(1f)`、起播前 350ms 让音频模式落定、通话音量为 0 时临时抬到 40%。

### 40.2 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **150 PASS / 0 FAIL** |
| 电平 / 简报 / 流契约 | `VerifyLevel` 16/16、`VerifyBriefing` 14/14、`VerifyStream` 51/51 |
| 双包校验 | `tools/verify_apk_v25.py` **92/92 PASS** |
| 包信息 | `versionCode 27 / 0.9.3 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.9.3-b27-release.apk` 407,463 B · `221ea9c8…5304`；`dist/voice-mcp-v0.9.3-b27-debug.apk` 447,955 B · `1ad595d1…844f` |

日志也跟着改了：每个音一条 `note 988Hz 150ms written=… state=0`，方便确认到底播没播。

## 41. 同心圆 0.9.4：拨号音「咔咔」声——每个音都重建音轨，尾巴被截断

### 41.1 根因：`write()` 不等于「播完」

0.9.3 的实现是「一个音一条 AudioTrack」：

```java
track = new AudioTrack(...);   // 每个音都建一条
track.play();
track.write(buf);              // ← 只是把数据塞进缓冲，返回时还没播完
track.stop(); track.release(); // ← 立刻关，剩下的缓冲被丢弃 → 咔
```

于是每个音结尾都被硬切一刀，听起来就是「咔、咔、咔」，而且建轨/关轨本身在通话声道上还会有一次 pop。

### 41.2 改法：一条轨 + 连续 PCM + 淡出收尾

| 改动 | 说明 |
| --- | --- |
| 整段旋律预渲染 | `renderDialPhrase()` 把「988/784/659 + 音间静音 + 尾部静音」渲染成 **1.9s 连续 PCM**（83,790 帧），循环写 |
| 一条轨贯穿整个会话 | `createTrack()` 每次会话只建一次（全类只有一处 `new AudioTrack.Builder`），消除建/关轨的 pop |
| 10ms 分块写入 | `CHUNK_FRAMES = 441`，`stop()` 后最多 10ms 就停，不会拖到下一轮 |
| 先淡出再收轨 | `fadeAndClose()` 先写 30ms 余弦淡出（终点严格为 0），等 60ms 让淡出播出去，再 `stop/release`；即使被截断也落在静音处 |
| 接通音自带尾巴 | 「叮-咚」后面补 200ms 静音，等它自然播完再收轨 |
| **每次会话独立持有音轨** | 之前用类字段共享 track，旧会话淡出时会把**新会话**的轨关掉（同样造成"断"），现在各自持有 |

### 41.3 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **138 PASS / 0 FAIL**（新增 1 条：全类仅 1 处 `new AudioTrack.Builder`、连续写、淡出收尾） |
| 电平 / 简报 / 流契约 | `VerifyLevel` 16/16、`VerifyBriefing` 14/14、`VerifyStream` 51/51 |
| 双包校验 | `tools/verify_apk_v25.py` **92/92 PASS** |
| 包信息 | `versionCode 28 / 0.9.4 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.9.4-b28-release.apk` 408,019 B · `58723600…df80`；`dist/voice-mcp-v0.9.4-b28-debug.apk` 448,479 B · `bfb28e7c…aa0a` |

日志：`AudioTrack ready buf=17640 min=…`、`dial loop start, phrase=83790 frames`、`dial loop end`。

## 42. 同心圆 0.9.5：按评审建议重做「字幕跟声音」的同步

0.9.4 之前是「一次性 lag 延迟」：ack 到达时算一个滞后值，把文字晚这么久再显示。评审指出的问题是——单次采样会被 HLS 分段跳变带偏，而且整段文字一起砸出来，长回复必然越来越不同步。按建议改成**句子级调度**。

### 42.1 五层改动

| 层 | 建议 | 实现 |
| --- | --- | --- |
| 1 采样 | 300–500ms 拉一次，记录 `edge` 与当时 `mediaT`，`seq` 必须单调，`ended` 后停轮询 | `POLL_MS = 300`；每次采样算 `lag = edge − mediaT`；`if (seq > lastSeq)` 丢弃过期/回退样本；`status=ended` → `ended=true` 并停止轮询 |
| 2 lag 估计 | 最近 8 个样本的**中位数**，seek/重连清空重估 | `LAG_WINDOW = 8` + `lagMedianSec()`（排序取中位）；位置回跳 >0.5s 与 `start()` 时 `resetLags()` |
| 3 文本调度 | 句子队列，`estimatedPlayed ≥ seg.start − 120ms` 出，`≥ seg.end + 200ms` 滚出；**不要按 ack 时刻** | 新增 `SpokenTextSync`：`GUARD_SEC = 0.12`、`TAIL_SEC = 0.20`，`push()` 把 `segments[{text,start,end}]` 换算成绝对媒体时刻（`pushSec + start`），`tick()` 每 100ms 调度 |
| 4 降级 | 连续 3 次拉取失败 → 退回「播放头 + 300ms」，恢复后自动切回 | `DEGRADE_AFTER = 3`；`frontierSec()` 在降级/无数据时返回 `mediaSec() + 0.3`；成功一次即退出降级并清空样本 |
| 5 升级开关 | 锚点来源收敛成一个函数 | `LiveLevelMeter.frontierSec()` 是唯一入口，且已预留 `setSpeechAtSec()`；`SpokenTextSync.frontierSec()` 只转调它 |

### 42.2 数据流

```
live_call start ──► level.json（seq/edge/fps/lv）
                        │  每 300ms 采样：lag = edge − mediaT
                        ▼
              lagMedian(最近 8 个) ──► frontierSec() = edge − lag
                        │                    ▲
                        │        （未来：setSpeechAtSec 一行切换）
                        ▼
voice_chat/job 的 segments[{text,start,end}]
        │  pushSec = edge（这段音频入流时刻）
        ▼
   SpokenTextSync 队列 ── 每 100ms tick ──► frontier ≥ start−120ms 出字
                                      └──► frontier ≥ end+200ms 滚出
```

占位行（「处理中…」）在第一句真文本出现时被`removeLine()`原地替换，不会出现「占位一条 + 正式一条」。

### 42.3 验证

| 项目 | 结果 |
| --- | --- |
| 契约 + 静态校验 | `tools/verify_v25_contract.py` **145 PASS / 0 FAIL**（新增 7 条：中位数窗口、seq 单调、降级/恢复、ended 冻结、锚点单一入口、120/200ms 阈值、300ms 采样） |
| 电平换算 | `VerifyLevel.java` 16/16 |
| 其它纯逻辑 | `VerifyStream.java` 51/51、`VerifyBriefing.java` 14/14 |
| 双包校验 | `tools/verify_apk_v25.py` **94/94 PASS**（新增 `SpokenTextSync` 类检查） |
| 包信息 | `versionCode 29 / 0.9.5 / 同心圆`，release v2 签名通过 |
| 交付 | `dist/voice-mcp-v0.9.5-b29-release.apk` 410,303 B · `0185f4a9…63c5`；`dist/voice-mcp-v0.9.5-b29-debug.apk` 451,179 B · `5723d1af…7a25` |

日志：`reply queued: 3 句, pushSec=45.9`、`level.json 连续 3 次失败，降级为播放头锚点`、`level.json 恢复，退出降级`、`level.json status=ended，停止轮询并放出剩余字幕`。

---

## 43. 同心圆 0.9.6：实时通话换到 dispatch 双向 WS（公网优先、内网兜底）

这一版把「实时通话」的传输层从 `live_call + HLS` 换成调度中心的 **P0 双向 WebSocket**：
上行推 WAV、下行收 ack/回复语音与结构化文字，语音往返更短，而且能拿到
`text / voice_text / mode / attachments` 这层新数据。旧的 HLS 链路保留为兜底。

### 43.1 网络层：公网优先，失败切内网

| 用途 | 公网（默认） | 内网直连（备选） |
| --- | --- | --- |
| 提交/查询/取消 | `https://ai.boraydata.com/conring/dispatch` | `http://192.168.31.206:9120` |
| 实时通话 WS | 由 submit 响应的 `realtime_url` 模板替换 `<channel_id>` 得到 | `ws://192.168.31.206:9121/channel/<channel_id>` |

- `DispatchApi` 里两条线路按「上次成功的那条优先」排序；任一条 `IOException/超时` 立刻换另一条。
- 记住成功线路（进程内 `preferred`），下次直接命中，不用先等一次公网超时。
- WS 连接同样先公网、失败再内网；断线重连也按这个顺序扫一遍。
- 三个地址都在 `McpConfig` 里持久化（`dispatchUrl / dispatchUrlLan / dispatchWsLan`），
  现场可改，不必重新出包。

### 43.2 submit 请求体（两个必踩的坑）

```json
{"text":"语音通话接通","source":"boss","modality":"voice",
 "channel":"ws","profile":"secretary","timeout_sec":600}
```

1. **必须显式带 `channel:"ws"`**。不带只会拿到 `channel_kind:"passthrough"`，响应里没有
   `realtime_url`，客户端只能退回 HLS。实测对照：
   - 不带：`{"channel_kind":"passthrough","channel_id":"ch_e522a1_8"}`（无 realtime_url）
   - 带：`{"channel_kind":"ws","realtime_url":"wss://ai.boraydata.com/conring/dispatch-ws/channel/<channel_id>"}`
2. **`source:"boss"` + `modality:"voice"`** 才进 P0 立即响应 lane；文字任务会进 P1 队列。
3. `text` 不能为空（服务端直接 400）。实时会话的内容完全由 WS 上行语音驱动，
   这里的 text 只进台账，所以给一句占位「语音通话接通」即可。
4. 本版只把**语音通话**切到 WS；视频通话（`LiveCallActivity`）仍走 HLS 混流——
   服务端 WS 通道本身只承载音频，没有视频轨。

### 43.3 WS 协议（服务端 `realtime_ws.py`，2026-11-11 实测）

| 方向 | 帧 | 说明 |
| --- | --- | --- |
| ↑ | `{"type":"hello"}` | 可选；重连时复用同一 `channel_id` 即恢复会话 |
| ↑ | 二进制 | 上行音频。**整段 16k 单声道 WAV**（含 RIFF 头）按 3200B/帧、20ms 间隔推 |
| ↑ | `{"type":"audio_end"}` | 显式断句（不发也行，服务端上行静音 1.2s 自动断句） |
| ↑ | `{"type":"ping"}` | 应用层心跳 → `{"type":"pong"}`（客户端 12s 一次） |
| ↑ | `{"type":"close"}` | 挂断 → 服务端回 `bye` 并结单 |
| ↓ | `session_started` / `hello_ok` | 通道就绪，带 `task_id / channel_id / profile` |
| ↓ | `{"type":"transcript","text":...}` | ASR 回显（自己的话） |
| ↓ | 二进制 | 即时应答 ack（1~2s，预热缓存）+ 正式回复语音 |
| ↓ | `{"type":"reply","text":...,"voice_text":...,"mode":...,"attachments":[...]}` | 结构化回复 |
| ↓ | `{"type":"reply_end"}` | 本条回复的音频推完 |
| ↓ | `{"type":"barge_in"}` | 播报中检测到上行语音，服务端已停播 |
| ↓ | `pong` / `timeout` / `error{message}` / `bye` | 心跳、空闲超时、错误、挂断确认 |

断线后服务端保留会话 **30s**（`REATTACH_GRACE`），客户端用同一 `channel_id` 重连即可续上；
客户端给 28s 窗口、指数退避重连，超时才判定会话结束。

### 43.4 客户端实现（仍然零第三方依赖）

| 新增类 | 职责 |
| --- | --- |
| `DispatchApi` | 双地址 HTTP：submit / task / cancel / health；解析 `task_id/channel_id/realtime_url` |
| `WsClient` | RFC6455 客户端：TLS+SNI+HTTPS 主机名校验、握手、掩码帧、分片重组、ping→pong、close |
| `RealtimeChannel` | 业务帧：hello/audio/audio_end/ping/close；解析 reply 系列；28s 重连窗口 |
| `WavStream` | 下行 WAV 流解析：按 chunk 扫到 `data` 再切，多段连续 wav、采样率切换、裸 PCM 兜底 |
| `PcmPlayer` | 流式 AudioTrack：收到即播、barge_in 立刻 pause+flush、打断后静音闸门、PCM 电平 |
| `CallReply` | `text`（文字详情）/ `voice_text`（语音稿）/ `mode` / `attachments` |

**下行 WAV 的真实形态（本轮最大的坑）**：服务端宣称「每段一个 44 字节头」，但它只是把
文件**前 44 字节**单独发一帧；CosyVoice 的 wav 在 `fmt` 之后还有 `LIST/INFO` 块，
`data` 实际在 ~92 字节处，头的后半段混在第一个「载荷」帧里。所以不能按固定 44 字节切，
必须顺序扫 chunk 找到 `data` 再切，否则 INFO 头会被当 PCM 播成杂音。抓到的原始头：

```
BIN #1 44B   524946466aae0100 ...            ← RIFF，但只是前 44 字节
BIN #2 4800B 494e464f49534654 ...            ← "INFOISFT"，头还在继续
BIN #3 4800B d2fe1900c201ae02 ...            ← 已经是 24k/16bit 的 PCM（100ms）
```

**打断（barge_in）是两层**：
1. 本地抢先：麦克风 VAD 一判到人声就 `pause+flush`，用户不用等服务端回 `barge_in`；
2. 静音闸门：打断后 `muted=true`，服务端还在路上的残余分块直接丢掉，直到下一次
   `transcript`（新一轮）或 `reply` 才解除——否则会出现「打断完又接着说」。

### 43.5 文稿分层：念的是一份、看的是一份

| 字段 | 用途 | 客户端处理 |
| --- | --- | --- |
| `voice_text` | 念出来的口语稿（服务端已限长） | 字幕行 + 音频对应文本，**唯一**可读的内容 |
| `text` | 文字详情，可能包含语音里没有的信息 | 转写区「详情：」一行 + 通话页详情卡，**绝不喂给 TTS** |
| `mode` | `task` / `chat` | 仅展示（决定回复风格） |
| `attachments` | `[{name,url}]` 共享链接 | 首页/通话页胶囊，点开用系统浏览器（只放行 http/https） |

`text` 与 `voice_text` 相同则不重复展示（服务端有时会 `text=voice_text` 兜底）。

### 43.6 兜底与降级

```
点通话 → dispatch submit(channel=ws)
           ├─ 成功 → 连 WS（公网 → 内网）→ 双向通话
           │           └─ 断线：28s 内同 channel_id 重连，仍失败才结束
           └─ 失败 → live_call + HLS（0.9.5 的老链路，行为不变）
```

- 每次通话最多尝试 3 次 dispatch，避免服务端异常时每 2.5s 建一个 P0 任务。
- WS 判定成功的标准：`channel_kind=="ws"` 且 `realtime_url` 非空且能完成 101 握手。
- 通道彻底不可用（重连超时 / 4404）→ 通话结束并提示原因，不会静默卡住。

### 43.7 验证

| 项目 | 结果 |
| --- | --- |
| 静态契约 | `tools/verify_v30_contract.py` **65 PASS / 0 FAIL**（双地址、channel=ws、WS 帧、WAV 切分、静音闸门、UI 分层、版本同步） |
| 解析/播放纯逻辑 | `tools/VerifyDispatch.java` **46/46 PASS**（submit 三种响应、模板替换、reply 分层、多段 wav、INFO 头、采样率切换、裸 PCM、电平） |
| 真实链路 | `tools/WsProbe.java` **PASS**：用 App 内同一份 `WsClient` 连生产 wss，推 8.45s 语音 → transcript → ack → reply(text/voice_text 分层) → reply_end → bye；共 133 个二进制帧、623,334 字节、3 个 RIFF 头（= ack + 2 段回复语音） |
| 回归 | `VerifyStream.java` 51/51、`VerifyLevel.java` 16/16、`VerifyBriefing.java` 14/14 |
| 双包 | `tools/verify_apk_v25.py` **126 PASS / 0 FAIL**（新增 6 个类 + 10 条协议串） |
| 包信息 | `versionCode 30 / 0.9.6 / 同心圆`，release v2 签名通过 |

交付：

| 文件 | 大小 | sha256（前/后 12 位） |
| --- | --- | --- |
| `dist/voice-mcp-v0.9.6-b30-release.apk` | 428,843 B | `a767c47f9fd8…40faaefb` |
| `dist/voice-mcp-v0.9.6-b30-debug.apk` | 474,823 B | `1d1dbfd7b46f…340f1534` |

原始联测日志：`docs/v30_ws_probe.log`；契约结果：`docs/v30_contract_check.json`、`docs/v30_apk_check.json`。

### 43.8 真机待确认

1. 24k 下行音频在 `MODE_IN_COMMUNICATION` 下是否被系统重采样（听感是否发闷）。
2. 打断时 `pause+flush` 的静音时延（目标 < 100ms）。
3. 弱网（切 4G/电梯）时 28s 重连窗口是否够用；必要时把 `GRACE_MS` 调到 26s 留余量。
4. 字幕与声音的对齐：WS 链路直接用下行音频当基准（不再依赖 `level.json`），
   真机上量一下「行出现」与「首字出声」的偏差；若需要字级同步，服务端补
   `speech_at_seconds` 后只需改 `SpokenTextSync.frontierSec()` 一处。

---

## 44. 同心圆 0.9.7：实时转写升级成「会话记录」——SQLite 长期保存 + 可拖拽面板

「实时转写」这块不再是通话时的一过性文本，而是与某个数字员工的 **session 会话记录**：
按人、按天长期存在 SQLite 里，App 启动就加载当天，往上翻能一天天回看。

### 44.1 为什么单开一张库

`HistoryDb`（0.9.x 已有）存的是「一次指令的结果」——问题、回答、音频/视频文件路径，
一条指令一行，用于结果回看。这轮要的是「对话流水」：谁在什么时候说了什么、是语音稿
还是文字详情、带不带附件链接。两者生命周期和查询方式都不同，硬塞进一张表会让
`history` 的语义变糊，所以新开 `conring_sessions.db`，互不影响。

### 44.2 表结构

```sql
sessions(id TEXT PRIMARY KEY, emp_id, emp_name, day, started_at, ended_at, transport)
messages(id INTEGER PK AUTOINCREMENT, session_id, emp_id, emp_name, day,
         ts, role, kind, text, links_json)
CREATE INDEX idx_msg_emp_day ON messages(emp_id, day, ts);
CREATE INDEX idx_msg_sid     ON messages(session_id, ts);
CREATE INDEX idx_sess_emp_day ON sessions(emp_id, day);
```

- `day` 是手机本地时区的 `yyyy-MM-dd`，用户看到的「今天」就是查询的「今天」。
- `role`：0 我 / 1 数字员工 / 2 系统；`kind`：0 语音稿 / 1 文字详情 / 2 系统提示。
- `links_json`：`[{"name":"合同.pdf","url":"https://…"}]`，只有「详情」类会带。
- 升级策略与 `HistoryDb` 一致：只增列、不重建，长期数据不会因为版本升级被清掉。

### 44.3 面板行为（首页 / 视频通话页共用）

| 能力 | 实现 |
| --- | --- |
| 启动即加载 | `bind(empId, empName)` → 载入**当天**；标题是员工名，副标题「今天 · N 条」 |
| 调整大小 | 顶部把手按住上下拖，跟手改高度；松手吸附到 **折叠 52dp / 半展 300dp / 全展（屏高−96dp）** 三档 |
| 直达底部 | 面板底部常驻屏幕底（`layout_gravity=bottom`），往上拉就是「拉长到底」 |
| 最新在下面 | 查询 `ORDER BY ts ASC, id ASC`，新消息追加在底部并自动贴底 |
| 上翻历史 | 滚到顶 `scrollY==0` 时查 `MAX(day) < 当前最早一天`，前插上一日并补回滚动位置 |
| 不打扰阅读 | 用户不在底部时，新消息只更新计数，不抢滚动 |
| 链接 | 正文里的 `http(s)://` 下划线高亮 + `ClickableSpan`，点开系统浏览器；附件另做胶囊 |
| 通话中 | 自动展开到半展；底部一行状态「正在听 / 思考…」（状态不入库） |

折叠态只刷新标题计数（`countDay`），不会为了几条新消息重建几百行视图。

### 44.4 文本信息单独处理（不只是语音转写）

这轮明确区分了三类内容，全部进会话记录：

1. **我说的**：ASR 出来的原文（role=我）；
2. **员工念的**：`voice_text` 语音稿（role=员工，kind=语音稿）——和声音一一对应；
3. **员工给的文字**：`reply.text` 文字详情（kind=详情，可能带附件链接）+ 服务端单独下发的
   `{"type":"message"/"text"}` 文本帧——这些**不朗读**，只在这里展示。

`（没听清）/（已发送）/（排队已满）` 这类瞬时状态被 `statusMarker()` 挡在库外，
不会污染历史；同一条消息后续更新走 `UPDATE messages SET text=?`，不会重复插入。

### 44.5 两条通话链路写同一张表

| 链路 | 写入点 |
| --- | --- |
| 语音（dispatch WS / HLS 兜底） | 服务里 `setLineSink(this::persistLine)`，每句定稿写库；`ensureLogSession("ws"/"hls")` 开会话，挂断 `finish()` |
| 视频（LiveCallActivity） | `ensureLogSession()` + `logMsg()`：ASR、ack、语音稿、文字详情分别落库 |

所以同一个员工今天先用语音、再用视频聊，会话面板里是**同一条时间线**。

### 44.6 验证

| 项目 | 结果 |
| --- | --- |
| 会话层静态契约 | `tools/verify_v31_sessions.py` **57 PASS / 0 FAIL**（建表/索引、按天查询、上一日、拖动三档、上翻分页、链接、文本帧、两条链路、版本） |
| SQL 语义 | 用同结构内存库跑了 `day / previousDay / countDay / latest / UPDATE` 五类语句，结果正确 |
| 0.9.6 契约回归 | `verify_v30_contract.py` 64/64 |
| 协议/播放回归 | `VerifyStream.java` 51、`VerifyLevel.java` 16、`VerifyBriefing.java` 14、`VerifyDispatch.java` 46 |
| 双包 | `tools/verify_apk_v25.py` **140 PASS / 0 FAIL**（新增 `SessionStore`、`SessionPanel`、`view_session_panel.xml`） |

交付：

| 文件 | 大小 | sha256（前/后 12 位） |
| --- | --- | --- |
| `dist/voice-mcp-v0.9.7-b31-release.apk` | 439,819 B | `0b2a3784c20b…3888d0c8` |
| `dist/voice-mcp-v0.9.7-b31-debug.apk` | 486,365 B | `4b2097e13a32…2ff28448` |

### 44.7 真机待验

1. 拖动把手的跟手感与三档吸附阈值（52/300/屏高−96）是否顺手。
2. 长历史（>400 条）展开时的首帧耗时；必要时把单日上限调小或改成分页加载。
3. 链接 `ClickableSpan` 在 `ScrollView` 里的点击/滚动冲突（点链接是否会误触发滚动）。
4. 跨天（凌晨通话）时 `day` 归属是否与用户预期一致。

---

## 45. 同心圆 0.9.8：会话框收起不再「消失」——首页常驻最新一条 + 右上角打开

0.9.7 的弹出层收起后只剩一条「会话记录 · 今天 N 条」的标题栏，看起来就像整块没了。
这版把它拆成两种形态，来回切换：

| 形态 | 位置 | 显示内容 | 操作 |
| --- | --- | --- | --- |
| **内嵌卡**（常驻） | 首页内容流里，通话主按钮下面 | 标题（员工名）+「今天 · N 条」+ **最新一条消息**（一行，超出省略） | 右上角「打开」小图标（`ic_open_panel`）或点标题 → 弹出完整面板 |
| **弹出面板**（按需） | 底部悬浮，可拖拽 | 当日全部消息 + 历史分页 | 拖动把手改高度；「收起」回到内嵌卡 |

- 收起 = 弹出层 `GONE`，内嵌卡本来就在，**不会再出现「点一下就没了」**；
- 通话接通/点通话时自动打开弹出面板，收起后通话继续，内嵌卡实时更新最新一句；
- 内嵌卡只查**当天**最新一条（`latest(empId, today)`），避免「今天 0 条却显示昨天的内容」；
- 两个面板同时存在，所以监听从单槽 `setListener` 改成多监听 `addListener/removeListener`，
  内嵌卡、弹出层、视频页面板各自刷新、互不覆盖。

### 45.1 验证

| 项目 | 结果 |
| --- | --- |
| 会话层契约 | `tools/verify_v31_sessions.py` **66 PASS / 0 FAIL**（新增：折叠显示最新一条、收起回调、内嵌模式、打开图标、当天最新） |
| 双包 | `tools/verify_apk_v25.py` **140 PASS / 0 FAIL** |
| 回归 | `verify_v30_contract.py` 64/64 |

交付：

| 文件 | 大小 | sha256（前/后 8 位） |
| --- | --- | --- |
| `dist/voice-mcp-v0.9.8-b32-release.apk` | 441,543 B | `729137d357f9…e7bceab7` |
| `dist/voice-mcp-v0.9.8-b32-debug.apk` | 488,365 B | `20e2653fe69a…fc12575b` |

### 45.2 真机待验

1. 内嵌卡 76dp 高度下「标题 + 条数 + 一行预览」是否拥挤（必要时压到 68dp / 去掉副标题）。
2. 收起后点「打开」重新展开时，滚动位置是否停在最新一条。
3. 视频页收起态同样显示最新一条预览，确认与视频画面不冲突。

---

## 46. 同心圆 0.9.9：通话即整页会话；首页头部收窄成「数字员工 18 名」

这轮把 0.9.8 的「可拖拽三档」彻底拿掉——不再有「展开 / 半展」，只有两种状态：
**一条**（最新一句）和**整页**（占满屏幕）。打开就是整页，右上角只有「收起」。

### 46.1 通话页编排

| 场景 | 行为 |
| --- | --- |
| 点通话 / 接通 | 会话直接以**整页**铺满（`openFull()`，`MATCH_PARENT`），盖住首页内容 |
| 右上角「收起」 | 整页隐藏（首页）/ 收回成一条（视频页），回到主界面，通话继续 |
| 未通话 | 首页只留内嵌一条：员工名 +「今天 · N 条」+ 最新一句 + 右上角打开图标 |

- 删掉了拖动把手、三档吸附、`expandPeek()/applyHeight()` 这套逻辑，`SessionPanel` 只保留
  `openFull()` / `showBar()` / `applyMode()`；
- 整页背景改成纯卡片色（不再用圆角 sheet），标题栏右侧固定一个「收起」；
- 视频通话页同样：接通即整页会话，收起后回到视频画面（底部留一条会话条）。

### 46.2 首页头部收窄

| 项目 | 之前 | 现在 |
| --- | --- | --- |
| 标题「同心圆」 | 21sp | **17sp**，`includeFontPadding=false` |
| 副标题 | 「OPC 数字员工 · 语音直达」 | **「数字员工 18 名」**（按档案实时统计，老板不计入） |
| LOGO | 48dp 方块、内图 26dp | 40dp 方块、内图 22dp |
| 间距 | 顶部 26dp、图标与文字 12dp | 顶部 18dp、图标与文字 10dp，文字块与 LOGO 垂直居中对齐 |

人数在 `refreshEmployeeCard()` 里刷新（`EmployeeStore.load()` 遍历、`isBoss()` 排除老板），
换汇报人 / 档案同步后都会跟着变。

### 46.3 验证

| 项目 | 结果 |
| --- | --- |
| 会话层契约 | `tools/verify_v31_sessions.py` **67 PASS / 0 FAIL**（整页 MATCH_PARENT、无拖动/展开档位、右上角收起、一条形态、人数副标题） |
| 双包 | `tools/verify_apk_v25.py` **140 PASS / 0 FAIL** |
| 回归 | `verify_v30_contract.py` 64/64 |

交付：

| 文件 | 大小 | sha256（前/后 8 位） |
| --- | --- | --- |
| `dist/voice-mcp-v0.9.9-b33-release.apk` | 440,943 B | `829bc50165ca…cc488d4e` |
| `dist/voice-mcp-v0.9.9-b33-debug.apk` | 487,469 B | `04875d3b24d1…5a0b02ee` |

### 46.4 真机待验

1. 整页会话里能否顺手挂断（目前需先「收起」回首页点红色通话图标；需要的话可在整页标题栏加挂断）。
2. 首页头部收窄后，标题 / 人数与右侧徽章是否仍在一行、不挤压。
3. 视频页接通即整页会话，收起后回到视频的衔接是否自然。

---

## 47. 同心圆 0.10.0：整页会话头部补齐「时长 · 电平 · 挂断 · 条数 · 收起」

整页会话的标题栏原来只有「名字 + 条数 + 收起」，通话中要挂断还得先收起回首页。
这版把通话控件直接搬进整页头部，从左到右：

```
小秘书   00:12   ▮▮▯▯    ⬤挂断    12 条    [ 收起 ]
  名字    时长    电平     小红钮    条数     加大的胶囊按钮
```

| 控件 | 行为 |
| --- | --- |
| 时长 | `mm:ss`，来自 `VoiceCallService.connectedAt()`；视频页用 `connectedAt` 同一口径 |
| 电平 | 34×16 的 `VoiceLevelView`，100ms 刷新一次，取麦克风与员工侧电平的较大值 |
| 挂断 | 30dp 红色圆钮（`bg_hangup_mini` + `ic_call`），点击直接结束通话并回到主界面 |
| 条数 | 整页显示「N 条」，一条形态显示「今天 · N 条」 |
| 收起 | 胶囊按钮（`bg_badge`，14dp 内边距、12sp 加粗），比之前明显好点 |

- 只有「整页 + 通话中」才显示时长/电平/挂断；不在通话或收成一条时自动隐藏，
  一条形态仍然是「名字 + 今天 · N 条 + 最新一句 + 打开图标」；
- 挂断动作由宿主注入（`setOnHangup`）：首页 → `VoiceCallService.stop()` 并隐藏整页；
  视频页 → `LiveCallActivity.endCall()`，两边共用同一个按钮；
- 电平/时长由宿主每帧喂（`setCallStatus(dur, level)`），面板只负责显示，不自己起循环。

### 47.1 验证

| 项目 | 结果 |
| --- | --- |
| 会话层契约 | `tools/verify_v31_sessions.py` **70 PASS / 0 FAIL**（新增：头部四件套、挂断注入、时长/电平喂值、收起胶囊） |
| 双包 | `tools/verify_apk_v25.py` **140 PASS / 0 FAIL** |
| 回归 | `verify_v30_contract.py` 64/64 |

交付：

| 文件 | 大小 | sha256（前/后 8 位） |
| --- | --- | --- |
| `dist/voice-mcp-v0.10.0-b34-release.apk` | 442,431 B | `d751d821eca8…fd23aa60` |
| `dist/voice-mcp-v0.10.0-b34-debug.apk` | 489,362 B | `8d672d4ef346…f9d85d1e` |

### 47.2 真机待验

1. 头部一行在 360dp 宽屏上是否挤（名字 3 字 + 时长 + 电平 + 挂断 + 条数 + 收起）；
   窄屏可把电平压到 26dp 或条数改为「12」。
2. 挂断按钮 30dp 是否好点（需要更大可加到 34dp，仍能放进标题栏）。
3. 视频页接通即整页、点挂断后是否正确回到视频页并结束会话。

---

## 48. 同心圆 0.10.1：返回键先回首页；通话中退出必须二次确认；未接通不弹整页

三个行为修正：

### 48.1 返回键

| 当前状态 | 按返回 |
| --- | --- |
| 整页会话开着 | **只收起整页，回到首页**（不结束通话、不退出 App） |
| 首页 + 通话中 | 弹确认框：「退出会先结束当前通话。确定要退出吗？」→ 取消 / 结束并退出 |
| 确认退出 | 先 `VoiceCallService.stop()` 中断会话，再 `finish()` |
| 首页 + 无通话 | 正常返回（退出） |

视频页同理：整页会话 → 只收起回视频页；通话中再按返回 → 确认后 `endCall()`；
空闲时返回直接结束页面。实现上用 `android.app.AlertDialog`（纯 framework，不引 AndroidX）。

### 48.2 未接通不弹整页

原来一点通话就 `openSessionSheet()`，会出现「还没接通就占满屏」。
现在只有 `connected == true`（WS 握手成功 / HLS 播放器 prepared，即真正进入会话模式）
才在 `renderCallUi()` 里自动打开整页；拨号与连接阶段只显示首页，内嵌那条照常。
视频页同口径：`enterLive()`（拿到直播流）才 `openFull()`。

手动入口（首页内嵌卡右上角小图标）不受限制，用户想提前翻历史随时可点。

### 48.3 验证

| 项目 | 结果 |
| --- | --- |
| 会话层契约 | `tools/verify_v31_sessions.py` **74 PASS / 0 FAIL**（新增：返回键只收起、退出二次确认、未接通不弹、视频页返回逻辑） |
| 双包 | `tools/verify_apk_v25.py` **140 PASS / 0 FAIL** |
| 回归 | `verify_v30_contract.py` 64/64 |

交付：

| 文件 | 大小 | sha256（前/后 8 位） |
| --- | --- | --- |
| `dist/voice-mcp-v0.10.1-b35-release.apk` | 442,927 B | `20ab07e921c3…27ff90a3` |
| `dist/voice-mcp-v0.10.1-b35-debug.apk` | 489,814 B | `3506ed127c9f…07d3f899` |

### 48.4 真机待验

1. Android 13+ 的预测性返回手势是否会绕过 `onBackPressed`（当前未开启
   `enableOnBackInvokedCallback`，走的是兼容路径）。
2. 通话中确认退出后，前台服务是否在 1~2 秒内真正停止（通知消失、麦克风释放）。
3. 未接通阶段的拨号音与返回键交互是否顺手（此时返回应直接退出）。

---

## 49. 同心圆 0.10.2：核查整页头部三件套 + 修一处「误触即收起」

### 49.1 核查结论：三件套在，且只在「整页 + 已接通」出现

从最终 APK 里直接 dump 布局树（`aapt2 dump xmltree`），整页头部结构是：

```
LinearLayout(llHead)
├─ TextView(tvTitle)                    小秘书
├─ TextView(tvTimer)   visibility=GONE  00:12
├─ VoiceLevelView(levelMini) visibility=GONE
├─ FrameLayout(btnHangup) visibility=GONE  └─ ImageView(ic_call)
├─ TextView(tvSub)                     12 条
└─ TextView(tvToggle)                  收起
```

- 三个控件默认 `GONE`，由 `applyCallChrome()` 在 `fullScreen && callLive` 时改成 `VISIBLE`；
- 编译产物里确认有 `openFull / setCallLive / setCallStatus / applyCallChrome`；
- 时序也核对过：`renderCallUi()` 里先 `setCallLive(connected)`、后 `openSessionSheet()`，
  所以整页打开那一刻 `callLive` 已经是 true。

### 49.2 修掉的真实问题：点头部会误触收起

`head.setOnClickListener(... toggle())` 原本是为了「一条形态点一下展开」，但整页里
电平条、时长这些**不可点**的子控件会把触摸冒泡到头部，触发 `toggle()` → 整页立刻收起，
观感就是「刚弹出来就没了 / 没看到时长和挂断」。

现在：整页里头部不再响应点击，只有右上角「收起」能收；一条形态仍可点标题栏打开。

### 49.3 兜底与自查

- `setCallStatus()` 每帧（100ms）除了喂时长/电平，还会在「整页 + 通话中」强制三件套
  `VISIBLE`，避免打开顺序或回调时序把它们漏掉；
- `openFull()` 打一条日志，真机可直接核对：

```
I/SessionPanel: openFull live=true timer=true level=true hangup=true
```

- 头部间距收紧（名字 ≤60dp、各控件 4dp 间距、挂断 28dp、收起 13dp 内边距），
  窄屏 / 大字体下不会被挤掉。

### 49.4 验证

| 项目 | 结果 |
| --- | --- |
| 会话层契约 | `tools/verify_v31_sessions.py` **76 PASS / 0 FAIL**（新增兜底可见、头部不误触） |
| 包内布局 | `aapt2 dump xmltree` 确认 `tvTimer / levelMini / btnHangup` 三个节点在 `view_session_panel.xml` 里 |
| 双包 | `tools/verify_apk_v25.py` **140 PASS / 0 FAIL** |

交付：

| 文件 | 大小 | sha256（前/后 8 位） |
| --- | --- | --- |
| `dist/voice-mcp-v0.10.2-b36-release.apk` | 443,119 B | `c271f31e4343…1a5ec112` |
| `dist/voice-mcp-v0.10.2-b36-debug.apk` | 490,122 B | `7c90e1f0a5da…bf2ead73` |

### 49.5 如果真机上仍然看不到

按顺序排查：

1. `adb logcat | grep SessionPanel` 看 `openFull live=?` —— 若 `live=false`，
   说明打开时还没进入 `connected`，把当时的 `VoiceCallService.status()` 发我；
2. 若 `live=true` 但肉眼无控件，看一眼是否处于**一条形态**（高 76dp）：那种形态本来就只显示
   「名字 + 今天 N 条 + 最新一句 + 打开图标」，时长/电平/挂断只在整页出现；
3. 若整页里只缺电平条，多半是 `VoiceLevelView` 高度被压到 0（父容器空间不足），
   把设备宽度/字体缩放告诉我，我再调布局。

---

## 50. 同心圆 0.11.0：电话指令统一落 dispatch，`live_call` 降为渲染层

### 50.1 问题确认

服务端 `voice_chat` 是直接 `run_hermes()`，**绕过调度中心**：没有 P0 任务、没有
`utterances/events` 台账、不走 lane、通话摘要不进 Obsidian。核查客户端后发现两处仍在用它：

| 链路 | 改造前 | 改造后 |
| --- | --- | --- |
| 纯语音（dispatch WS） | 0.9.6 起已走 WS ✅ | 不变，仍是主链路 |
| 纯语音（HLS 兜底） | `voice_chat` + `voice_job` ❌ | **本地 ASR → dispatch passthrough → live_call say** |
| 视频通话 | `voice_chat(live_session)` + `voice_job` ❌ | **dispatch WS(modality=video) → live_call say 渲染口型** |

现在 `VoiceCallService` / `LiveCallActivity` 里**已无任何 `voice_chat` / `voice_job` 调用**，
`live_call` 只剩 `start/stop/say`（say 只负责把文字念出来、驱动口型）。

### 50.2 新链路

```
纯语音：麦克风 → 3200B/帧 WS 上行 → 服务端 ASR+hermes → transcript/reply → 客户端流式播放
         （P0 任务、utterances 台账、lane、通话摘要交付物全在 dispatch 里）

视频：  麦克风 → dispatch WS（modality=video）→ reply.voice_text
         → live_call say(text) → HLS 直播流（数字人口型 + 声音）

兜底：  WS 不可用 → 本地 asr_transcribe → dispatch /submit（passthrough，不建 WS）
         → 轮询 /task/<id> 取 result → live_call say 渲染
```

`DispatchApi` 新增：

- `submit(profile, voice, text, modality, channel)` —— `channel=null` 即 passthrough；
- `ask(profile, modality, text, timeout)` —— 一步提交并等结果（兜底链路用）；
- `publicWsUrl(submit)` —— 由 `https://host/conring/dispatch` 推导
  `wss://host/conring/dispatch-ws/channel/<id>`。

### 50.3 顺带修的两个「服务端配置回退」导致的连不上

1. **`realtime_url` 回成了内网地址**：服务端没配 `DISPATCH_WS_PUBLIC_URL` 时返回
   `ws://192.168.31.206:9121/...`，外网手机连不上。客户端现在按同源规则自己推导公网
   `wss://ai.boraydata.com/conring/dispatch-ws/channel/<id>`，尝试顺序：
   **公网推导 → 服务端原样 → 内网直连**。
2. **公网 HTTP 502**：`dispatchd` 现在只绑 `127.0.0.1:9120`（`DISPATCH_HOST` 默认值），
   边缘反代连不进来。服务端加一行即可恢复（我没有替你重启服务）：

   ```bash
   DISPATCH_HOST=0.0.0.0 DISPATCH_WS_PUBLIC_URL=wss://ai.boraydata.com/conring/dispatch-ws \
     nohup python3 /opt/sefs/hermes/dispatch/dispatchd.py >> logs/dispatch.log 2>&1 &
   ```

   实测证据：`ss -lntp` 里 `9120` 只监听 `127.0.0.1`，`9121` 是 `0.0.0.0`；
   从服务器本机 `curl 127.0.0.1:9120/health` 正常，公网域名返回 502。

### 50.4 跟进服务端新协议（这轮新增）

服务端加了「多轮并行 + 主动话术 + 首段 18 字」，客户端同步适配：

| 变化 | 客户端处理 |
| --- | --- |
| `reply` / `transcript` / `reply_end` 带 `turn` | 按轮次排序后再交给界面（缺号 300ms 兜底补发），避免「文字和语音错位」 |
| 主动问候/催问 `turn=0` + `greeting` | 语音链路正常展示；视频链路跳过 greeting（本地已 greet，避免问两次） |
| 多轮回复可能连发 | 新一轮不再覆盖上一轮的行，已有文字就另起一行 |

### 50.5 验证

| 项目 | 结果 |
| --- | --- |
| 入口契约 | `tools/verify_v33_entry.py` **29 PASS / 0 FAIL**（无 voice_chat/voice_job、dispatch.ask、say 渲染、turn 排序、greeting 去重） |
| 真链路（passthrough） | `tools/AskProbe.java` **PASS**：提交 → `channel_kind=passthrough` → 14s 后 `done`，result 为「现在是 2026 年 11 月 11 日…」 |
| 解析/播放 | `VerifyDispatch.java` **48/48**（新增公网 WS 推导）；`VerifyStream` 51、`VerifyLevel` 16、`VerifyBriefing` 14 |
| 会话层 | `verify_v31_sessions.py` **76/76** |
| 旧契约 | `verify_v30_contract.py` **64/64** |
| 双包 | `verify_apk_v25.py` **140/140** |

交付：

| 文件 | 大小 | sha256（前/后 8 位） |
| --- | --- | --- |
| `dist/voice-mcp-v0.11.0-b37-release.apk` | 444,423 B | `27104d42ed43…1ae1c029` |
| `dist/voice-mcp-v0.11.0-b37-debug.apk` | 491,562 B | `0f6df19cc791…51ea163d` |

### 50.6 待办 / 注意

1. 服务端两处 env 未配（见 50.3），配好之前公网语音/视频通话都无法真正接通；
   WS 9121 是 `0.0.0.0`，局域网内可用。
2. 首页那个已隐藏的「按住说话」老入口仍走 `McpConfig.tool`（默认 `voice_chat`），
   不影响电话链路；要彻底清掉可以再删一轮。
3. `live_call say` 渲染与 WS 回复之间仍有 2~5s 的 TTS 延迟（视频链路），
   这是「先出文字再出声」的固有代价，真机上可再评估是否直接用 WS 音频配 HLS 画面。

---

## 51. 同心圆 0.11.1：公网语音指令进不了后台 —— 根因是 dispatchd 只绑 127.0.0.1

### 51.1 症状与定位

现象：走 `https://ai.boraydata.com` 打通话，**服务端主动问候的声音能听到**，
但老板说「老板叫什么」这类指令没有任何回复，后台也查不到记录。

三步定位（不需要看 App 日志）：

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| `curl https://ai.boraydata.com/conring/dispatch/health` | **502** | 公网 HTTP 入口挂了 |
| `ss -lntp \| grep 9120` | `127.0.0.1:9120` | dispatchd 只监听回环，边缘反代连不进来 |
| `ss -lntp \| grep 9121` | `0.0.0.0:9121` | WS 端口正常 → 所以「能听到服务端说话」 |

服务端日志同时给出铁证：

```
P0 实时 worker 启动 rt_a0334d_9 ... （等待 WS 连接）
实时会话结束 rt_a0334d_9 → failed：客户端 120s 内未建立实时通道
```

也就是说：`submit` 根本没成功 → 客户端拿不到 `channel_id` → 退回 HLS 兜底；
而 0.11.0 起 HLS 的上行也要走 `dispatch.ask()`，同样 502 → **指令一条都到不了后台**。

### 51.2 根因（两个服务端配置，一条写死在代码里的默认值）

```python
# dispatchd.py:45
HOST = os.environ.get("DISPATCH_HOST", "127.0.0.1")     # ← 默认值害的
```

1. **`DISPATCH_HOST` 没设** → 只绑 `127.0.0.1`，49.7.58.10 上的边缘 nginx 连不进来 → 502。
2. **`DISPATCH_WS_PUBLIC_URL` 没设** → `realtime_url` 回成
   `ws://192.168.31.206:9121/channel/<id>`（局域网地址），外网手机连不上。
3. 只把 `DISPATCH_WS_PUBLIC_URL` 设成 base（`.../dispatch-ws`）也不行：
   `dispatchd.py:964` 是 `WS_URL_PUBLIC.replace("<channel_id>", cid)`，
   **模板必须带 `<channel_id>` 占位**，否则返回的 `realtime_url` 缺 `/channel/<id>` 段。

### 51.3 修复（已落地，且重启不丢）

装成 systemd 服务，两个环境变量写进 unit：

```ini
# /etc/systemd/system/dispatchd.service
[Service]
WorkingDirectory=/opt/sefs/hermes/dispatch
Environment=DISPATCH_HOST=0.0.0.0
Environment=DISPATCH_WS_PUBLIC_URL=wss://ai.boraydata.com/conring/dispatch-ws/channel/<channel_id>
ExecStart=/usr/bin/python3 dispatchd.py
Restart=always
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now dispatchd
```

同时留了一份手动启动脚本 `/opt/sefs/hermes/dispatch/start.sh`（含同样两个变量），
`systemctl` 不可用时直接 `./start.sh` 也能起到正确状态。

修复后：

| 检查 | 修复前 | 修复后 |
| --- | --- | --- |
| `https://…/conring/dispatch/health` | 502 | **200** |
| `realtime_url` | `ws://192.168.31.206:9121/...` | **`wss://ai.boraydata.com/conring/dispatch-ws/channel/ch_xxx`** |
| `ss -lntp \| grep 9120` | `127.0.0.1:9120` | **`0.0.0.0:9120`** |
| 服务端台账 | 一直 `failed：120s 内未建立实时通道` | `第 1 轮入队（12.7s 音频）` → `第 1 轮指令：…` |

### 51.4 端到端证据（公网 wss，非隧道）

```
SUBMIT  {"channel_kind":"ws","realtime_url":"wss://ai.boraydata.com/conring/dispatch-ws/channel/ch_e65b01_10"}
OPEN    wss://…
TEXT    {"type":"session_started",…}
TEXT    {"type":"reply","turn":0,"greeting":true,"text":"老板早，睡好了吧，事儿您直接讲。"}
AUDIO_END +2784ms
TEXT    {"type":"transcript","turn":1,"confidence":0.769,
         "text":"您好,我是陸清安。公司CEO,接下來由我為您匯報本次語音指令的執行結果。"}
TEXT    {"type":"reply","turn":1,"voice_text":"陆总您好，我是墨老板这边的小秘书…",
         "text":"提示：本次通话系统登记人是墨老板（OPC-000）…"}
PASS    transcript=true reply=true voice_text=true（48 帧 / 214,524 B / 3 个 RIFF 头）
```

服务端日志同步出现「第 1 轮入队（12.7s 音频）」「第 1 轮指令：…」「通话摘要已归档」。

### 51.5 客户端加固（避免下次「静默无响应」）

| 加固 | 原因 |
| --- | --- |
| 握手后必须 4s 内收到 `session_started`/`hello_ok`，否则关掉换下一个地址 | 边缘代理可能「假接受」升级（101 回来了但后端没转发），旧逻辑会误判成功、上行全丢 |
| 上行改独立线程 `senderIo` | 原来与重连循环共用 `io`，断线重连时这句会被排队丢掉 |
| `dispatchError` 写进界面 | 原来失败只 `Log.w`，用户看到的是「没反应」；现在显示「调度中心不可达（HTTP 502），指令进不去后台」 |
| 内网探测超时 6s → 3s | 公网现场根本没有内网路由，白等 6 秒 |
| `WsProbe` 只认 `turn>=1` 的 `reply_end` | 服务端新增主动问候（`turn=0`），旧探针把它当成本轮结束，提前关连接导致误判 FAIL |

### 51.6 以后再遇到「说了没反应」，按这个顺序查

```bash
# 1) 公网 HTTP 是否通（502 就是绑定/反代问题）
curl -s -o /dev/null -w '%{http_code}\n' https://ai.boraydata.com/conring/dispatch/health
# 2) submit 是否给完整通道
curl -s -X POST https://ai.boraydata.com/conring/dispatch/submit -H 'Content-Type: application/json' \
  -d '{"text":"测试","source":"boss","modality":"voice","channel":"ws","profile":"secretary"}'
# 3) 服务端有没有收到这一轮
tail -20 /opt/sefs/hermes/dispatch/logs/dispatch.log   # 找「第 N 轮入队」/「第 N 轮指令」
```

- 有「第 N 轮入队」= 音频到了服务端，问题在 ASR/模型；
- 没有 = 上行没到，看 `ss -lntp | grep 9121` 与 App 的 `adb logcat | grep RealtimeChannel`
  （`handshake ok but no session_started` 就是假连接）。

### 51.7 验证

| 项目 | 结果 |
| --- | --- |
| 公网健康/提交 | health **200**；submit 返回完整 `wss://…/channel/ch_xxx` |
| 公网端到端 `WsProbe` | **PASS**（transcript + reply 分层 + voice_text + 3 个 RIFF 头） |
| 入口契约 | `verify_v33_entry.py` **34/34**（新增假连接防护、独立上行线程、失败可见） |
| 会话层 / 旧契约 / 双包 | 76/76、64/64、**140/140** |
| 纯逻辑回归 | VerifyDispatch 48、VerifyStream 51、VerifyLevel 16、VerifyBriefing 14 |
| systemd | `systemctl is-active dispatchd` = **active**，`enable --now` 已开机自启 |

交付：

| 文件 | 大小 | sha256（前/后 8 位） |
| --- | --- | --- |
| `dist/voice-mcp-v0.11.1-b38-release.apk` | 444,883 B | `eae5ce305a78…f1f3baa` |
| `dist/voice-mcp-v0.11.1-b38-debug.apk` | 492,522 B | `b032039f5e48…3980531` |

### 51.8 备注

- 服务端这次重启会中断当时在跑的实时会话（重启前确认过 `running/queued = 0`）。
- `dispatchd.py` 的默认 `HOST=127.0.0.1` 建议上游改掉，或至少在 README 里标注——
  它和「本地能 curl、公网 502」这个现象是强耦合的，很容易反复踩。

---

## 52. 同心圆 0.11.2：服务端新一轮改动验证 + 1011 的真正根因（`tts_text` 返回 tuple）

### 52.1 这轮服务端改了什么（拉最新代码 diff 得出）

| 改动 | 影响 |
| --- | --- |
| `REATTACH_GRACE` 30s → **300s** | 断线 5 分钟内凭原 channel_id 或新 channel_id 都能接回同一会话 |
| `adopt_orphan()` + `WS_ADOPT_ORPHAN` | 未知 channel_id 连进来，若只有一个刚断开的会话就接管 |
| `submit` 支持 `session_key` / 自动 resume | 换号重新提交不再新建会话（`resumed: true`） |
| 下行改**单写队列** `_outq` + `_writer` | 多轮并发播报不再交错帧 |
| ASR 全局串行 + `ASR_QUEUE_MAX` + 慢提示 | whisper 单路跑 CPU，排队超时快速失败；不出字先出声安抚 |
| TTS 磁盘缓存 + 多句 ack 轮换 + 首段 18 字 | 首声更快、应答不重复 |
| `BARGE_MIN_MS=240` | 单帧底噪不再把自己的播报掐掉 |

### 52.2 公网验证结果（全部走 `ai.boraydata.com`）

| 项 | 结果 |
| --- | --- |
| `health` | 200 |
| `submit` | `resumed` 正确、`realtime_url` 为公网 wss 且带 `/channel/<id>` |
| 上行 → ASR | transcript **+2999ms**（11.5s 音频） |
| 即时安抚 | `reply turn=0` **+12.5s** |
| 正式回复 | `reply turn=1` **+22.3s**，`text` 136 字 / `voice_text` 51 字（分层正常） |
| 流式 TTS | 日志「第 1 轮流式 TTS：2 段」 |
| 挂断 | `close 1000`，**无 1011** |
| 台账 | `实时会话结束 → done` + `通话摘要已归档` |
| 换号重连 | 新建 `ch_verify_qe0ozs` 直连 → `hello_ok`，**task_id 与原会话一致** |

### 52.3 1011 的真正根因：不是客户端分片

先做帧级自检 `tools/FrameCheck.java`（零依赖极简 WS 服务端逐帧解析）：

```
fin=1 op=2 len=3200 masked=1
fin=1 op=2 len=3200 masked=1
fin=1 op=2 len=3200 masked=1
fin=1 op=1 len=20   masked=1
PASS  frames=4 分片帧=0
```

即便改成「整句一个 binary 消息一次发完」，1011 依旧出现；而**推完流什么都不做时不会出现**。
所以它由 close 触发，与音频分片无关。

真正的根因在服务端 `realtime_ws.py`：

```python
futs = [ex.submit(tts_text, c, self.task.get("voice"), 150) for c in chunks]
w = fut.result()                     # ← tts_text 返回 (wav_bytes, mime)，这里是 tuple
self._send_wav_paced(w)              # → ws.send(tuple) → TypeError
```

websockets 收到非 str/bytes 会按 iterable 处理，tuple 里是 `bytes` + `str`（mime），
于是抛 `TypeError('iterable must contain uniform types')`；而 `_writer` 当时是：

```python
except Exception as e:
    log("下行发送失败：%r" % (e,))
    return          # ← 协程退出，这条连接之后再也发不出任何帧
```

**表现就是：第一段 TTS 后突然没声音，之后所有回复只有文字、没有语音。**

已打三处最小补丁（原文件备份为 `realtime_ws.py.bak-0616`）：

| 位置 | 补丁 |
| --- | --- |
| 流式 TTS | `w = fut.result()` → `w, _mime = fut.result()` |
| `_send_wav_paced` | 开头把 tuple/list/bytearray 归一成 `bytes` |
| `_writer` | 单帧异常 `return` → `continue`（只丢这一帧，不废掉整条连接） |

补丁后日志中不再出现 `下行发送失败`，`close 1000`，无 1011，任务 `done`。

### 52.4 客户端 0.11.2 的配合改动

| 改动 | 原因 |
| --- | --- |
| 上行一句话**整段一个 binary 帧**（≤1MB），超长才退回 3200B 分块 | 按你的建议“一次发完”，避免多帧 + 文本帧交错 |
| 推流期间不发应用层 `ping`（`uplinkBusy`） | 音频流中间不插 JSON 帧 |
| 挂断**先停上行**（`senderIo.shutdownNow()`）再发 `{"type":"close"}` | 杜绝音频帧与 close 帧并发写 |
| 挂断后等 `bye` 最多 900ms 再断 socket | 给服务端留收尾时间 |
| 重连前先关旧 socket（`ws = null` + `prev.close`） | **一条会话只挂一个 socket**，避免自己踢自己 |
| `4409` 表示被新连接替换 → 停止重连并提示 | 不再进入重连风暴 |
| 每条连接独立 listener（`listenerFor(owner)`） | 旧连接的 `onClosed` 能被识别并忽略 |

### 52.5 验证

| 项目 | 结果 |
| --- | --- |
| 帧级自检 `FrameCheck` | **PASS**（4 帧全 FIN=1，0 分片） |
| 公网端到端 | transcript + reply 分层 + 2 段 TTS + `close 1000` + `done` + 摘要归档 |
| 换号重连 | **接管成功，同一 task_id** |
| 入口契约 | `verify_v33_entry.py` **40/40**（新增单帧上行、推流不 ping、先停上行、4409、等 bye、单 socket） |
| 会话层 / 旧契约 / 双包 | 76/76、64/64、**140/140** |
| 纯逻辑回归 | VerifyDispatch 48、VerifyStream 51、VerifyLevel 16、VerifyBriefing 14 |

交付：

| 文件 | 大小 | sha256（前/后 8 位） |
| --- | --- | --- |
| `dist/voice-mcp-v0.11.2-b39-release.apk` | 445,191 B | `f6a7642d1334…1bbb4dc57a` |
| `dist/voice-mcp-v0.11.2-b39-debug.apk` | 493,110 B | `6058610e93ad…4ef76199b220f` |

### 52.6 给服务端的两条后续建议

1. `_writer` 里再补一层类型兜底（`if not isinstance(data, (str, bytes)): data = bytes(data)`），
   避免任何一处忘记解包又把整条下行打死。
2. `tts_text` 的返回值改成命名结构（如 `dataclass Wav(data, mime)`）或在类型注释里写死，
   这类「返回 tuple 但按 bytes 用」的错很难在 review 里看出来。

---

## 53. 同心圆 0.12.0：启动登录（本机号码 / 手机号+密码）+ 后台处理模式校对

### 53.1 登录链路（新增）

| 项 | 实现 |
| --- | --- |
| 入口 | `LoginActivity` 改为唯一 LAUNCHER；`MainActivity` 不再导出，未登录一律回登录页 |
| 本机号码 | API 33+ `READ_PHONE_NUMBERS`、≤32 `READ_PHONE_STATE`；`SubscriptionManager.getActiveSubscriptionInfoList()` 逐卡取号（API 33+ `getPhoneNumber(subId)`，旧版 `SubscriptionInfo.getNumber()`），`getLine1Number()` 兜底，号码归一化后按 SIM 去重 |
| 交互 | 一个号码 → 弹「是否使用本机号码登录」确认；双卡多个 → 列表选一个（另有「手动输入其他号码」）；读不到 / 拒绝权限 → 直接手动输入。本机号码**只自动填账号**，服务端没有免密登录，密码必须输入 |
| 后端 | `POST https://ai.boraydata.com/workbench/api/login {"account": 手机号, "password": 密码}`，与 Web 端同一套账号（users.db 手机号 / Obsidian 登录凭据.md）；成功返回 `{"ok":true,"user":{...}}` + `Set-Cookie: opc_sess=...; Path=/; Max-Age=604800` |
| 会话 | 私有 SP `conring_auth` 存 Cookie/手机号/姓名/角色；`GET /api/session` 启动校验（401 回登录页、网络不通按离线继续）；`POST /api/logout` 退出。登录地址在登录页 / 设置页可改（如内网 `http://192.168.31.206:8848`） |
| 生效范围 | Cookie 同步进 `McpConfig.sessionCookie`，由 `McpClient` / `SseListener` / `DispatchApi` 回传；首页、设置页、`ListenService`、开机自启都做登录门禁；设置页显示账号并提供「退出登录 / 切换账号」 |

顺带修掉一个旧问题：`SettingsActivity.read()` 原先 `new McpConfig()`，每次点「保存设置」
都会把 `dispatchUrl / dispatchUrlLan / dispatchWsLan` 重置回默认值；现在从 `McpConfig.load()` 起读。

### 53.2 后台处理模式校对（服务端 2026-11-14 实况）

| 层 | 服务端现状 | 0.12.0 客户端对齐 |
| --- | --- | --- |
| `dispatchd.py` | `/submit` **没有** `mode`/`priority`/`lane` 入参，由 `source`+`modality` 分类 P0/P1/P2；P1/P2 进优先级队列（2 worker），P0 专属线程；支持 `session_key` / 自动 resume（`resumed:true`），`REATTACH_GRACE` 已由 30s 提到 **300s** | `GRACE_MS` 28s → 300s；submit 带 `session_key=登录手机号`，解析 `resumed`；`verify_v30_contract.py` 的 30s 断言同步 |
| `realtime_ws.py` | 改成**妙记式听写**：每句语音立刻「收到」ack、ASR 只落草稿（transcript，不跑 hermes）；听到「没了 / 结束 / 就这些」或静音 12s 收尾 → 先发 `reply mode="readback"` 复述 → 一次性 `run_hermes` → 正式 `reply` + 流式 TTS（`BATCH_TIMEOUT=600`） | 旧逻辑按 `turn` 从 1 严格递增排队，而 transcript 也消耗轮次、收尾两帧共用同一 turn，**回复会被永久卡在 `pendingReplies`**；改为 `TreeMap<Integer, List<CallReply>>` + 300ms 窗口全量放行（允许跳号 / 同轮多帧），`CallReply.isReadback()` 单独成行，字幕改「已记下…／已复述，正在整段执行…」 |
| `mcp-asr/server.py` v2.2.0 | `voice_chat` **仍是**异步两段式（`_JOB_Q` 固定 3 worker）+ `voice_job` 长轮询，`wait` 仍支持；0.11.0 的「统一走 dispatch」只发生在**客户端电话链路** | `VoiceOutbox` / `ListenService` 的离线兜底与保持监听保留，注释和设置页文案改成「双轨」说明，避免误当废弃代码删除 |
| 客户端开关 | 服务端不读取 `asyncMode` / `keepListening`，它们只是本机「同步等待 / 后台常驻」开关 | 保持原语义，文案标注清楚 |

### 53.3 验证

| 项目 | 结果 |
| --- | --- |
| `./gradlew collectApks --offline` | debug + release 双包 → `dist/voice-mcp-v0.12.0-b43-*.apk` |
| `tools/verify_v34_login.py`（本轮新增） | **38 / 38** |
| `verify_v33_entry.py` / `verify_v31_sessions.py` / `verify_v30_contract.py` | 40 / 40、76 / 76、64 / 64 |
| 公网接口实测 | `/workbench/api/logininfo` 200；`/workbench/api/login` 错误口令返回 401 `账号或密码不正确`；`/workbench/api/session` 未带 Cookie 返回 401；`/conring/dispatch/health` 200 |
| 隔离冒烟（`OPC_USERS_DB` 指向临时库、`OPC_DB=0`、独立端口） | 注册 → `POST /api/login {account:手机号,password}` 200 + `Set-Cookie: opc_sess=…; Max-Age=604800` → 带 Cookie 的 `GET /api/session` 200 → 错误口令 401 → `POST /api/logout` 200 + `Max-Age=0`，**SMOKE OK**（未触碰生产注册表） |

> 历史脚本 `verify_v21_contract.py` / `verify_v24_contract.py` / `verify_v25_contract.py` 仍有
> 硬编码 0.3.2/0.4.0 版本、旧 UI（`btnTalk`）与旧纯 JVM 源清单（缺 `DispatchApi.java`）造成的
> 既有失败项，与本次改动无关；本轮未新增失败。

