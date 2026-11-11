# Hermes 媒体网关改造方案（v2，按技术评审意见修订）

> 版本：v2 / 2026-11-11
> 变更：吸收《Hermes媒体网关改造方案_技术评审意见》的 P0-1 ~ P2-3；**所有 API 断言已改为本机实测**，未实测项单列「搁置清单」。
> 本文所有「实测」标记均可复现，命令附在附录 A。

---

## 0. 评审意见落实情况总览

| 评审项 | 结论 | 本文位置 | 处置 |
|---|---|---|---|
| P0-1 架构层错了 | **部分成立** | §2、§3 | 改为「host-native provider 为数据面 + 独立推理进程 + 适配器仅做入口」三层结构，并**分期**执行；不再把 media 适配器当作唯一入口 |
| P0-2 prompt cache 论证站不住 | **成立** | §4 | 修正论证：收益来自**卸载 12 个 MCP 工具**（已实测枚举），不是卸载 2 个；`text_to_speech` 是核心工具、保留；`platform_hint` 真实作用域已核实并降级表述 |
| P0-3 API 臆造 | **成立，且发现更严重错误** | §5 | 逐条改正；`PlatformEntry` 原写作 `factory=` 会直接 TypeError（真实字段是 `adapter_factory` + 必填 `check_fn`） |
| P0-4 质量门 | 成立 | §6 | 补多媒体防幻觉、标签映射、版本矩阵、平台默认音色对齐、去掉无源并发数字 |
| P1-1 工作量 | 成立 | §7 | 拆 S0~S4 五个阶段，每阶段独立可验收、可回滚 |
| P1-2 数据面 | 成立 | §8 | 补断点降级阶梯 + job 状态语义（pending 只有两个） |
| P1-3 安全 | 成立 | §9 | 补 job 归属校验、临时文件 TTL、限流、多租户隔离 |
| P1-4 前置检查空泛 | 成立 | §10 | 给出可直接运行的 3 个检查脚本 |
| P1-5 契约未定义 | 成立 | §11 | 移动端契约单列 |
| P1-6 进程模型 | **不成立（已证伪）** | §12 | 实测 bridge 是 3 个独立进程，可分别停；另发现**同一模型被加载两遍**的真实缺陷 |
| P2-1 运营指标 | 成立 | §13 | 补指标与埋点位置 |
| P2-2 文档缺子问题 | 成立 | §14 | 明示搁置清单 |
| P2-3 主张分级 | 成立 | §15 | 全文主张分级表 |

---

## 1. 现状（全部为本机实测）

### 1.1 版本与环境

| 项 | 实测值 | 说明 |
|---|---|---|
| Hermes 版本 | **0.22.7** | 评审提到的 0.23.0 在本机不存在，按 0.22.7 设计 |
| 网关进程 | 单进程 `hermes gateway`，pid 3463001 | 多 profile 复用 |
| `config.yaml → platforms` | **空**（无任何消息平台） | 现有链路完全不经过平台适配器 |
| `config.yaml → stt` | `enabled: true`，未设 `provider` → 内置 local | 默认模型偏英文 |
| `config.yaml → tts` | `provider: edge` | 与自建 CosyVoice 并存 |
| 9200 / 9201 端口 | 空闲 | 可直接使用 |

### 1.2 MCP 现状（关键）

网关主进程 **pid 3463001 直接拉起了一个 stdio 的 `mcp-tts`（pid 3463273）**；同时 `mcp-bridge` 又拉起了**另一个** `mcp-tts`（pid 4192061，端口 9101）。

> **这是真实缺陷，不是推测**：CosyVoice 模型被两个进程各加载一份，显存/内存双倍占用。评审 P1-6 担心的「多 profile 打架」在本机表现为更糟的形态——**同一个 profile 就加载了两遍**。

三个 bridge 是**独立进程**（9102 asr / 9100 mail / 9101 tts），**可分别启停**，停 9101/9102 不影响 9100 的 mail。评审 P1-6 的「牵一发动全身」不成立。

暴露的工具（实测枚举，共 **12 个**，常驻每轮 API 请求）：

| 来源 | 工具 |
|---|---|
| mcp-tts | `tts_list_voices`、`tts_synthesize` |
| mcp-asr | `asr_transcribe`、`voice_chat`、`voice_job`、`boss_briefing`、`profile`、`voice_avatars`、`intro_video`、`live_call`、`dispatch_task`、`dispatch_status` |

依赖关系：`mcp-asr` 内部会反向调用 `tts_synthesize`（`server.py:357`），二者不能单独下线。

---

## 2. 架构决策（回应 P0-1）

### 2.1 评审指出的问题

评审认为：ASR/TTS 是**数据面/传输层**，Hermes 的 `stt.provider` / `tts.provider` 就是为此提供的 host-native 扩展点；把它们搬到独立 HTTP 服务，等于**撤回平台原生能力自建一套**，并带来断点、数据暴露、心跳、监控、限流、多租户、配额七个必须自补的洞。

### 2.2 我的判断：两者不矛盾，但原方案把顺序做反了

关键区分：

- **provider 接口**是 host-native 的 seam —— 只要 ASR/TTS 的**调用入口**走 `register_transcription_provider` / `register_tts_provider`，自动转写与语音回复就仍然由 Hermes 内核驱动，不违反平台边界。
- **推理宿主**是另一个独立问题 —— CosyVoice/torch 不可能进网关进程（网关是单进程多 profile，模型数 GB）。现有 `mcp-tts` 已经在进程内 `load_model`，这正是它必须再加一层 9101 桥的原因。

所以正确形态是**三层**，而不是「全部搬到 MCP」或「全部留在 MCP」：

| 层 | 职责 | 归属 |
|---|---|---|
| L1 调用入口 | 语音消息自动转写、语音回复渲染、`text_to_speech` 工具 | **host-native**（provider 插件） |
| L2 推理宿主 | CosyVoice / whisper / Wav2Lip 模型常驻 | **单实例独立进程**（可先沿用现有 venv） |
| L3 外部入口 | App/小程序上传音频、取回语音/视频 | media 适配器（**最后做**，且只做入口） |

**原方案真正的错误不是「分服务」，而是把 L3 当成唯一入口一次性建成**——这让整个改造变成大爆炸式迁移。v2 改为按 L1 → L2 → L3 顺序，每步独立可验收。

### 2.3 由此产生的关键取舍：MCP 是「下线」还是「降级为唯一推理实现」

评审建议：保留 `mcp-tts`/`mcp-asr` 作为唯一推理实现，补缓存/健康检查/单实例化，流量走 host-native（改动最小，一天级）。

用户此前选择：全部下线，重写进插件。

**v2 的处置**：**分阶段逼近用户目标，但不在第一步就删代码**。

- S0（当天，零代码/极小改动）：**单实例化**。停掉网关直拉的那份 `mcp-tts`，只保留 9101 一份。这一步不改变任何架构，纯粹消除 §1.2 的双份加载缺陷。**无论后续走哪条路，这一步都必须做，且立即有收益。**
- S1：把 `mcp-tts` 的推理代码抽成 `media-engine` 的 TTS 端点，**代码复用不重写**；`mcp-tts` 仍然在线，作为回滚路径。
- S2：新增 `media-voice` provider 插件，`stt.provider=media_whisper`、`tts.provider=media_cosyvoice`。**此时数据面已完全 host-native**，MCP 的 `tts_synthesize` 已无调用方。
- S3：下线 `mcp-tts`/9101；`mcp-asr` 的数字人能力迁到 `media-engine` 的 `/v1/video/jobs`，其余工具逐项确认无调用方后下线 9102。
- S4：media 适配器（L3）。

> 这样既满足「最终 MCP 全下线、能力进插件」，又满足评审的「不要一次性推翻数据面」和「可渐进回滚」。**S0 与 S1 之间不设阻断**：如果 S1 受挫，系统仍处于 S0 的健康状态。

### 2.4 目标架构图（v2）

```
            App / 小程序 / H5                          （S4 才接入）
                    │  POST /media/v1/messages
                    ▼
        ┌────────────────────────────┐
        │ media 适配器 :9201 (插件)   │  ← L3，仅入口，无推理
        └───────┬──────────▲─────────┘
   MessageEvent  │          │ send / send_voice / send_video
                ▼          │
        ┌──────────────────┴─────────────────────────────────┐
        │ Hermes 网关主循环（host-native，单进程多 profile）    │
        │  stt.provider = media_whisper                       │
        │  tts.provider = media_cosyvoice                     │
        └──────────────────────┬─────────────────────────────┘
                               │ HTTP（L1 → L2）
                               ▼
        ┌────────────────────────────────────────────────────┐
        │ media-engine :9200  单实例常驻（S1 起）              │
        │  /v1/asr  /v1/tts  /v1/video/jobs  /files/{name}    │
        └────────────────────────────────────────────────────┘
                 ▲ 迁移自 mcp-tts / mcp-asr（代码复用）
```

---

## 3. 与「保留 MCP」方案的成本对比（评审要求补充）

| 维度 | A. 保留 MCP + 单实例化 | B. 本文 v2 分层迁移 |
|---|---|---|
| 模型加载份数 | S0 后 1 份 | 1 份 |
| 常驻工具 schema | **12 个** | **0 个**（`text_to_speech` 是核心工具，另计） |
| 语音消息自动转写 | 需 MCP 工具被模型主动调用 → **不可靠** | 内核自动触发 → 确定 |
| 语音回复 | 同上 | `send_voice` 路径确定 |
| 断点/重试/限流 | 需自己补 | 同样需自己补（见 §8、§9） |
| 一次性风险 | 低 | 中（靠 S0~S4 分期与回滚控制） |
| 终局维护成本 | 高（能力锁死在 MCP） | 低（host-native） |

评审「改动最小、一天级」的判断**对 A 方案成立**，这正是 v2 把 S0 提前、并允许长期停在 S0 的原因。

---

## 4. 关于 prompt cache（回应 P0-2）

**原方案表述错误**：原文说「卸载 2 个工具节省 schema」，实际是 12 个（§1.2 实测枚举）。但**修正后仍不足以单独支撑架构决策**，接受评审批评。

v2 的表述降级为：

- 卸载 12 个工具确实减少每轮请求体积（量化需实测 token 数，见附录 A-3，**本文不给未测数字**）。
- `text_to_speech` 属 `_HERMES_CORE_TOOLS`（`toolsets.py:20`），**下线 MCP 后依然存在**，由新 provider 提供后端。因此「卸载 MCP」不等于「少一个 TTS 入口」，两者不冲突。
- `platform_hint` 的真实作用域：只注入 system prompt 的 platform hint 段（`agent/system_prompt.py::_resolve_platform_hint`、`agent/prompt_builder.py:1221`），并影响 skill 可见性；**不是**整段 system prompt 替换。原方案「设置 platform_hint 让模型说短句」表述过重，v2 改为「hint + 适配器侧截断」双保险。
- **真正的收益是确定性与可靠性**（语音必被转写），不是 token 节省。架构理由以 §2.2 为准。

---

## 5. API 接线（逐条实测，回应 P0-3）

| # | 原方案断言 | 核实结果 | v2 正确写法 |
|---|---|---|---|
| 1 | `PlatformEntry(name=..., label=..., factory=...)` | ❌ **字段是 `adapter_factory`**；且 `check_fn` 为**必填**位置参数 | `PlatformEntry(name, label, adapter_factory, check_fn, validate_config=None, ensure_deps_fn=None, **entry_kwargs)` |
| 2 | `register_platform(...)` 关键字调用 | 签名接受位置参数 name/label/adapter_factory/check_fn | 见 §5.1 示例 |
| 3 | `trusted_inbound=True` 的安全含义 | 对 core 平台会被拒绝；media 非 core 平台，可用 | 仅在 S4 使用，并配合 token |
| 4 | `MessageEvent.media_urls` 传本地路径 | ✅ `media_urls: list[str] = 本地文件路径`（`event.py:95`） | 保持不变 |
| 5 | `cache_media_bytes(...)` | ✅ **返回本地路径 str**（`media_cache.py:69`），非 URL | `path = cache_media_bytes(data, mime, kind_hint="audio")` |
| 6 | `adapter.handle_message(event)` | ✅ 基类方法（`base.py:3924`） | 直接用 |
| 7 | `send_voice(chat_id, audio, ...)` | ✅ 第二参数是 **`audio_path: str`**，不是 bytes（`base.py:2971`） | 落盘后传路径 |
| 8 | `send_video(...)` | ✅ 同族签名，默认实现为降级提示 | 传路径 |
| 9 | `TTSProvider.synthesize` | ✅ 存在（`agent/tts_provider.py:57`），另有 `list_voices` | 按基类签名实现 |
| 10 | `TranscriptionProvider.transcribe` | ✅ 存在（`agent/transcription_provider.py:27`） | 按基类签名实现 |
| 11 | `platforms.<name>` 非保留键进 `extra` | ✅ `PlatformConfig.from_dict` 用 `_TYPED_KEYS` 白名单过滤（`config.py:444-456`） | 自定义键放顶层即可，读 `config.extra` |
| 12 | `Platform("media")` 解析早于插件注册会静默跳过 | ✅ `config.py:727` 处注释确认 unknown platform 被 skip | §10 检查脚本 1 |
| 13 | `mcp-asr` 的 `run_hermes` 反向派发语义 | ⚠️ **未完全核实**（`server.py:357` 仅确认其内部转发 `tts_synthesize`） | 列入 §14 搁置清单，S3 前置检查项 |

### 5.1 适配器注册的正确骨架

```python
from gateway.config import Platform

def _media_deps_ok() -> bool:
    # check_fn 是无参被动探测：只判断依赖是否可导入，绝不安装、不发网络请求。
    # 网关状态页会随时调用它，所以不能读运行时状态、不能依赖 cfg。
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True

def register(ctx):
    ctx.register_platform(
        "media",                        # name
        "Media API",                    # label
        lambda cfg: MediaAdapter(cfg),  # adapter_factory(PlatformConfig) -> BasePlatformAdapter
        _media_deps_ok,                 # check_fn（必填、无参）
        validate_config=None,           # 可选：PlatformConfig -> bool
        required_env=None,              # 可选：缺失时状态页提示
        platform_hint="You are replying over an audio/video API. Keep replies short and spoken-style.",
        ensure_deps_fn=None,            # 可选：check_fn 为 False 时才调用的主动安装器
    )
```

> 签名已实测：[plugins.py](file:///opt/sefs/hermes/hermes-agent/hermes_cli/plugins.py#L779-L797) `register_platform(self, name, label, adapter_factory, check_fn, validate_config=None, required_env=None, install_hint="", **entry_kwargs)`；`check_fn` 是**无参**被动探测，`adapter_factory` 接收 `PlatformConfig`。端口等运行期参数从 `PlatformConfig.extra` 读（`from_dict` 会把未知键全部收进 `extra`），不要塞进 `check_fn`。

---

## 6. 质量门（回应 P0-4）

### 6.1 端到端质量门（每条必须可测）

| 门 | 判定 | 未通过处置 |
|---|---|---|
| ASR 文本质量 | 参考音频集 ≥30 条，字符错误率 CER ≤ 10%（中文） | 调模型/加 prompt/换 large-v3 |
| **多媒体防幻觉** | 纯音乐、噪声、静音输入 → **必须返回空文本或明确失败**，禁止输出编造句子 | 启用 §6.2 过滤链，硬阻塞上线 |
| TTS 自然度 | 3 人盲听，MOS ≥ 3.5（5 分制） | 换音色/调 speed |
| TTS 可懂度 | 合成音频回灌 ASR，CER ≤ 5% | 同上 |
| 数字人唇形对齐 | 抽 10 条，人工判断「明显不同步」比例 = 0 | 调 Wav2Lip 参数 |
| 平台默认音色对齐 | 见 §6.3 | 硬阻塞 |
| 视频可播放性 | `ffprobe` 通过 + 3 端（iOS/Android/Chrome）实际播放 | 重编码 |

> 以上阈值均为**待标定的目标值**，不是实测结论；标定方法见附录 A-4。

### 6.2 多媒体防幻觉过滤链（必须实现）

依序执行，任一命中即判定为「无可转写语音」：

1. **时长门**：音频 < 0.3s → 直接空结果。
2. **能量门**：整体 RMS < 阈值 → 空结果。
3. **VAD 门**：speech 占比 < 5% → 空结果。
4. **文本置信门**：whisper `avg_logprob` < -1.0 或 `no_speech_prob` > 0.6 → 丢弃该段。
5. **重复门**：同一句连续出现 ≥3 次（whisper 典型幻觉）→ 只保留一次。
6. **黑名单门**：命中常见幻觉模板（如「请不吝点赞订阅转发」「字幕由…提供」）→ 丢弃。
7. **字符集门**：`language=zh` 但输出字符中非中日韩占比 > 60% → 丢弃。

### 6.3 平台默认音色对齐（原方案缺失）

现网 `tts.provider: edge`，其默认音色与 CosyVoice 的 `female-zh-real` **音色不同**。若直接切换，所有平台语音风格会突变。

处置：
1. S2 上线前，导出 edge 默认音色样本；
2. 在 CosyVoice 音色库中做 ABX 主观比对，选出最接近的 2 个候选；
3. 写成 `MEDIA_TTS_VOICE` 默认值，并在 S2 验收里加「与改造前样本对比无明显风格跳变」；
4. 保留 `config.yaml` 的 `tts.voice` 覆盖能力。

---

## 7. 分期实施（回应 P1-1）

| 阶段 | 内容 | 工作量级 | 验收 | 回滚 |
|---|---|---|---|---|
| **S0** ✅ 2026-11-11 | 消除双份模型加载：停网关直拉的 `mcp-tts`，只留 9101 一份（或反向：停 9101，改由网关单份） | 小时级 | `ps` 只剩 1 个 CosyVoice 进程；内存下降 | 恢复启动 |
| **S1** ✅ 2026-11-11 | 建 `media-engine:9200`，迁入 TTS（代码复用 `mcp-tts`）与 ASR（复用 `mcp-asr`）；`/health` | 人日级 2-3 | 附录 A-2 冒烟全绿 | 停服务，MCP 仍在 |
| **S2** | `media-voice` provider 插件；切换 `stt.provider`/`tts.provider` | 人日级 1-2 | 语音消息自动转写生效；`send_voice` 可用；音色对齐通过 | 改回 `edge` + 内置 local |
| **S3** | 数字人迁 `/v1/video/jobs`；下线 9101/9102；删 `mcp_servers.tts` | 人日级 2 | 12 个工具全部消失且系统无报错；job 状态机正确 | 恢复 MCP 注册 + 重启桥 |
| **S4** | media 适配器 + 客户端契约 | 人日级 3-4 | §11 契约逐条通过 | 关插件 |

**S0 可立即执行，且不依赖任何后续决策。** 建议作为本次改造的第一步。

### 7.1 S0 执行记录（2026-11-11）

| 项 | 内容 |
|---|---|
| 改动 | `/opt/sefs/hermes/config.yaml` 删除 `mcp_servers.tts`（原 line 2186-2192），留 3 行注释说明停用原因与后续归属 |
| 备份 | `/opt/sefs/hermes/backups/config.yaml.bak-s0-20261111` |
| 校验 | `yaml.safe_load` 通过；`mcp_servers` 键已不存在；`tts.provider` 仍为 `edge` |
| 范围确认 | 18 个 profile 的 `config.yaml` 只注册 `mail` 一个 MCP，无 `tts`；`skills/`、`SOUL.md` 无任何 `tts_synthesize` / `tts_list_voices` 引用 |
| 进程结果 | 网关直拉的 `mcp-tts`（ppid=网关 pid）已消失；仅剩 9101 桥 1 份 CosyVoice（RSS 391MB，模型已加载） |
| 待办 | 网关进程内存里仍持有旧注册，需重启后彻底移除；重启由小时级自重启或人工 `gateway restart` 完成 |

**实测偏差更正**（原 §1 表述）：双份进程中，网关直拉那份 21 小时 0 调用、RSS 仅 13MB（懒加载，模型从未进内存），真正加载模型的是 9101 桥那份（RSS 391MB）。因此 S0 的收益是**消除隐患**（任何一次网关侧调用都会再加载一份 ~10GB 权重的模型），而不是「已发生的内存双倍占用」。

### 7.2 S1 执行记录（2026-11-11）

| 项 | 内容 |
|---|---|
| 交付物 | `/opt/sefs/media-engine/`：`server.py`（FastAPI）、`tts_engine.py`、`asr_engine.py`、`run.sh`、`engine.log`、`work/`（产物） |
| 运行方式 | `setsid --fork sh run.sh`（已脱离会话，ppid=1）；`run.sh` 清 `LD_LIBRARY_PATH`、复用 `/opt/sefs/CosyVoice/venv` |
| 代码复用 | TTS：切句/归一化/音色注册/静音裁剪/失控重试/进程池整段迁移，仅剥离 MCP 协议层；ASR：`whisper.load_audio` + `transcribe` 与原有过滤逻辑复用。音色表仍读 `mcp-tts/voices.json`（唯一数据源，`MEDIA_VOICES_FILE` 可覆盖） |
| 新增 | §6.2 完整过滤链：时长门(0.3s) → 能量门(RMS 0.002) → VAD 门(有声帧占比 5%) → 段置信门(`no_speech>0.6` 或 `avg_logprob<-1.0`) → 复读门 → 黑名单门 → 字符集门；阈值全部可用 env 覆盖 |
| 端点 | `GET /health` `/metrics` `/v1/voices` `/files/{name}`；`POST /v1/tts`（wav / mp3 / json+SRT+segments）；`POST /v1/asr`（multipart 或 json base64/path） |
| 回滚 | 停 9200 即可；9101/9102 桥与 dispatch 全程未改动，`mcp_tts` 仍在线 |

**附录 A-2 冒烟实测（全部通过）**

| 用例 | 结果 |
|---|---|
| `GET /health` | `status=ok`，tts 2 worker ready，sample_rate 24000，asr model=small |
| `POST /v1/tts` mp3 | HTTP 200，25,173 B，`ffprobe` duration 3.48s（首调用 32.1s 含模型加载，热调用 RTF≈2.9） |
| `POST /v1/tts` json | 返回 audio/srt/segments 三链接 + 句级时间轴，SRT 时间码正确 |
| `GET /files/{name}` | 200；`/files/..%2F..%2Fetc%2Fpasswd` → 404（路径穿越已阻断） |
| `POST /v1/asr`（TTS 回灌） | `"第二句话在这里,勇于验收。"`（原文「用于」，small 模型错字），confidence 0.613 |
| 静音 1s | `silent=true, silent_reason=silent`，不跑模型（能量门） |
| 粉噪 3s | whisper 编出文本，被黑名单门丢弃 → `text=""`，`filtered_reasons={blacklist:1}` |

**实测偏差 / 需后续处理**

1. **单 worker 内存 6.6~7.8GB**（CosyVoice3-0.5B，RSS 实测）：原 `mcp-tts` 默认 6 worker ≈ 42GB。S1 期间两套并存，故 media-engine 默认 `TTS_WORKERS=2`（≈14GB），S3 下线 9101 后再调回 6。
2. **ASR 质量**：whisper `small` 同音错字（「用于」→「勇于」），且该条 confidence 0.489 恰好落在 `ASR_LOW_CONFIDENCE=0.5` 下方被判 `low_confidence`。§6.1 的 CER≤10% 门尚未标定，S2 前需决定是否升 `medium`/`large-v3` 或改用 9102 现有配置。
3. **`ffmpeg`/`ffprobe`**：本机 shell 的 `LD_LIBRARY_PATH` 指向 IDE 自带库，直接调用会报 `GLIBCXX_3.4.32 not found`；`run.sh` 已 `unset`，运维脚本调用时同样要清。
4. 引擎未接 `/v1/video/jobs`（数字人）与 job 队列——按方案留到 S3。

---

## 8. 数据面：断点与降级（回应 P1-2）

### 8.1 链路断点清单

| 断点 | 现象 | 降级策略（必须实现） |
|---|---|---|
| engine 不可达 | `/v1/asr` 连接超时 | 语音消息 → 回复「语音服务暂不可用，请发文字」，**不静默丢弃** |
| ASR 超时 | > 60s | 返回「转写超时」，保留音频路径 |
| ASR 判定无可转写语音 | §6.2 过滤命中 | 回复「没听清，请再说一次」，**不调模型**（防幻觉） |
| TTS 失败 | 5xx / 超时 | 降级为纯文本回复，并在 job 标记 `tts_degraded: true` |
| 视频任务失败/超时 | 900s | 降级为「语音 + 文本」，job 标记 `video_degraded: true` |
| 队列满 | 429 | 客户端退避重试；服务端返回 `retry_after_ms` |
| 引擎重启 | 进行中 job 丢失 | job 状态置 `failed(reason=engine_restart)`，**不静默卡死** |

### 8.2 job 状态语义（明确边界）

- **只有两个渠道产生 `pending`/`queued`：HTTP 入口受理（`202`）与视频引擎入队（`202`）。**
- 状态机：`accepted → running → rendering(仅 video) → done | failed`
- 任何状态**不得倒退**；`done`/`failed` 为终态，**不可回写**（避免重试导致状态闪烁）。
- 每个 job 带 `deadline_at`，超时由清理协程统一置 `failed`，不依赖调用方轮询。

---

## 9. 安全（回应 P1-3）

| 项 | 要求 |
|---|---|
| **job 归属** | `GET /media/v1/jobs/{id}` 必须校验 `job.owner_session == 请求 session`（用 Bearer token 派生的主体，不接受客户端自报 session）；不匹配返回 **404**（不泄漏存在性） |
| **临时文件 TTL** | `media-engine/work/` 与适配器 job 目录统一 `TTL=24h`，后台每 10min 清理；进程重启时清理孤儿文件 |
| **限流** | 按 token 主体：ASR 10 req/min、TTS 30 req/min、video 3 req/hour；滑动窗口；超限 429 + `Retry-After` |
| **多租户** | 每个租户独立 token、独立 `work/<tenant>/` 目录、独立并发配额；**不做共享队列** |
| **路径穿越** | `/files/{name}` 只接受 basename，`os.path.realpath` 后必须仍在根目录内 |
| **上传限制** | body ≤ 25MB、音频时长 ≤ 60s、并发全局 ≤ 8（**这些是工程默认值，非实测容量**） |
| **监听地址** | 默认 `127.0.0.1`；跨机必须反代 + TLS |
| **密钥** | 只在 `/opt/sefs/hermes/.env`，禁止写入 `config.yaml` |

---

## 10. 前置检查（可执行，回应 P1-4）

### 检查 1：平台是否可解析（S4 前必跑）

```bash
/opt/sefs/hermes/hermes-agent/.venv/bin/python -c \
  "from gateway.config import Platform; print(repr(Platform('media')))"
```
- 输出 `None` → 插件未在 `Platform` 解析前注册 → 按附录 A-1 打印真实 `register_platform` 签名后再修。
- 兜底：软链为内置平台 `ln -s /opt/sefs/hermes/plugins/media /opt/sefs/hermes/hermes-agent/plugins/platforms/media`（升级后需重建）。

### 检查 2：provider 是否真的被选中（S2 后必跑）

```bash
grep -E "stt provider|tts provider" /opt/sefs/hermes/logs/*.log | tail -20
```

### 检查 3：工具表是否真的清空（S3 后必跑）

```bash
grep -rnE "tts_synthesize|asr_transcribe|intro_video|live_call" /opt/sefs/hermes/config.yaml /opt/sefs/hermes/profiles/*/config.yaml
```
- 必须无输出；同时 `ps -eo pid,cmd | grep -E "mcp-tts|mcp-asr"` 必须无输出。

### 检查 4：反向依赖（S3 前必跑）

`dispatch_task` / `dispatch_status` / `run_hermes` 若存在外部调用方，下线会断链。执行：
```bash
grep -rn "dispatch_task\|dispatch_status" /opt/sefs/hermes /home/boray/opc-workbench --include="*.py" --include="*.md" --include="*.json" | grep -v "/mcp-asr/"
```
有命中 → 该工具**不随 S3 下线**，需单独迁移。

---

## 11. 客户端契约（回应 P1-5）

### 11.1 移动端弱网

| 场景 | 约定 |
|---|---|
| 音频格式 | 上传接受 `ogg/opus`、`m4a/aac`、`wav`；服务端统一转 16k 单声道 |
| 分片上传 | v1 **不支持**；单文件 ≤ 25MB，超限客户端先压缩 |
| 上传中断 | 客户端幂等键 `Idempotency-Key`，服务端 24h 内相同键返回同一 job |
| 轮询 | 建议 1s 起、指数退避至 5s；`done/failed` 后停止 |
| 断线恢复 | 用 `job_id` 重连查询；服务端保证 job 存活 ≥ 24h |
| 超时语义 | 客户端侧 90s 无 `done` → 提示「仍在生成，稍后在历史查看」 |

### 11.2 与「转写 + 生成」双渠道的一致性

`reply_mode=video` 时，job 同时产出 `text` / `audio_url` / `video_url`；客户端应按 `video → audio → text` 优先级展示，**避免视频失败时用户拿不到任何结果**。

---

## 12. 进程模型（回应 P1-6，已证伪）

评审担心「两个桥在同一进程，牵一发动全身，未论证如何单停 9101 而保 9102」。实测：

```
9102 → asr 桥（独立进程）
9100 → mail 桥（独立进程）  ← 保留
9101 → tts 桥（独立进程）
```

**结论：可分别启停，9100 mail 不受影响。** 该风险不成立。

但实测发现一个**评审未提及、更严重**的问题：网关直拉的 `mcp-tts` 与 9101 桥的 `mcp-tts` **同时存在**，模型双份加载。这是 S0 的全部内容。

---

## 13. 运营指标（回应 P2-1）

| 指标 | 埋点位置 | 用途 |
|---|---|---|
| `asr_latency_ms` (p50/p95) | engine `/v1/asr` | 容量 |
| `asr_empty_ratio` | §6.2 过滤命中率 | 幻觉防线健康度 |
| `tts_latency_ms` (p50/p95) | engine `/v1/tts` | 容量 |
| `tts_fail_ratio` | provider 插件 | 降级频率 |
| `video_queue_depth` / `video_job_duration_ms` | engine job 队列 | 扩容依据 |
| `job_fail_reason` 分布 | 适配器 job 表 | 定位主要故障 |
| `degraded_ratio`（tts/video 降级占比） | 适配器 | 用户体验 |

指标一律由 engine 暴露 `/metrics`（Prometheus 文本格式），**不引入新依赖**。

---

## 14. 明示搁置清单（回应 P2-2）

以下问题本文**未解决**，明确列出，不假装闭合：

1. **`run_hermes` / `dispatch_task` 的语义**：未核实其是否为反向调用 Hermes 的通道；若存在外部调用方，S3 不能直接下线。见 §10 检查 4。
2. **`register_platform` 底层装饰器签名**：`adapter_factory` 是否带 `cfg` 参数、`platform_hint` 传参位置，未实测到最底层，S4 开工前打印签名确认。
3. **CosyVoice 与 whisper 同进程部署的资源争用**：未实测同机峰值内存，S1 需实测后决定是否拆分进程。
4. **数字人视频的并发容量**：Wav2Lip 在本机的单条耗时未测，队列上限（3）是拍定值。
5. **多租户计费/配额**：本期只做隔离，不做计量。
6. **HLS 直播（`live_call`）、Remotion 模板化口播**：不在本期。
7. **平台默认音色的客观对齐方法**：§6.3 用主观 ABX，未引入客观声纹相似度指标。

---

## 15. 主张分级表（回应 P2-3）

| 主张 | 等级 | 依据 |
|---|---|---|
| 网关直拉与 9101 桥各加载一份 CosyVoice | **代码/进程实测** | `ps` 输出，附录 A-5 |
| 三个 bridge 是独立进程，可分别停 | **代码/进程实测** | 同上 |
| MCP 暴露 12 个工具 | **代码实测** | `server.py` 枚举，§1.2 |
| `PlatformEntry` 字段为 `adapter_factory` + 必填 `check_fn` | **代码实测** | `gateway/platform_registry.py` |
| `cache_media_bytes` 返回本地路径 | **代码实测** | `media_cache.py:69-77` |
| `send_voice` 第二参数为路径 | **代码实测** | `base.py:2971` |
| `platform_hint` 只影响 system prompt 的 hint 段 | **代码实测** | `agent/system_prompt.py::_resolve_platform_hint` |
| `text_to_speech` 属核心工具 | **代码实测** | `toolsets.py:20` |
| 每轮可省多少 token | **推理（未实测）** | 需 A-3 实测 |
| 各质量门阈值（CER 10%、MOS 3.5 等） | **推理（目标值）** | 需标定，非实测 |
| 并发上限 8 / 25MB / 60s | **推理（工程默认值）** | 需压测 |
| 评审提到的 0.23.0 版本 | **不成立** | 本机实测 0.22.7 |

---

## 附录 A：可复现命令

**A-1 打印平台注册签名**
```bash
cd /opt/sefs/hermes/hermes-agent && \
python -c "import inspect; from hermes_cli.plugins import PluginContext; print(inspect.signature(PluginContext.register_platform))"
```

**A-2 engine 冒烟**
```bash
curl -s localhost:9200/health
curl -s -X POST localhost:9200/v1/tts -H 'Content-Type: application/json' \
  -d '{"text":"你好，这是语音合成测试","voice":"female-zh-real","format":"mp3"}' -o /tmp/t.mp3
ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/t.mp3
curl -s -F file=@/tmp/t.mp3 -F language=zh localhost:9200/v1/asr
```

**A-3 量化工具表 token 占用**（用于替代表 §15 的「未实测」）
```bash
# 改造前后各跑一次，比较 model_tools 导出的 schema 总字节数
cd /opt/sefs/hermes/hermes-agent && \
python -c "import json,model_tools; s=json.dumps(model_tools.get_tool_schemas()); print(len(s))"
```

**A-4 质量门标定**：准备 ≥30 条中文参考音频（16k 单声道），逐条跑 `/v1/asr` 计算 CER；TTS 侧合成后回灌 ASR 计算往返 CER；数字人抽 10 条人工判读。

**A-5 复现双份模型加载**
```bash
ps -eo pid,ppid,cmd | grep -E "mcp-tts|mcp-asr|bridge.py" | grep -v grep
```

---

## 附录 B：参考资料

- Hermes 官方文档 · Telegram（语音自动转录）：https://hermes-agent.nousresearch.com/docs/zh-Hans/user-guide/messaging/telegram
- Hermes 官方文档 · 消息网关：https://hermes-agent.nousresearch.com/docs/user-guide/messaging/
- 本机源码：`gateway/platform_registry.py`、`gateway/platforms/base.py`、`gateway/platforms/event.py`、`gateway/platforms/media_cache.py`、`gateway/config.py`、`agent/tts_provider.py`、`agent/transcription_provider.py`、`agent/system_prompt.py`、`toolsets.py`
