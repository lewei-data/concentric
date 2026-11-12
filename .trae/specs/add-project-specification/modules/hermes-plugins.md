# 模块规格 · hermes-plugins（Hermes 插件层）

> 上级文档：[spec.md](../spec.md)。本文件只描述 `hermes-plugins/`。
> 当前仓库仅一个插件：`media-platform`（`plugin.yaml` 21 行、`__init__.py` 323 行）。

## 1. 模块定位

`hermes-plugins/media-platform` 把 media-engine 的数字人/媒体能力包装成 Hermes Agent 可调用的**模型工具**（toolset = `media`）。插件进程只做 HTTP 客户端，不加载任何模型；推理与合成全部在 media-engine 内完成（`__init__.py:1-22`）。

- 插件形态：`kind: standalone`，`requires_hermes: ">=0.22.0"`，无 hook（`plugin.yaml:1-21`）。
- 设计动机：网关进程保持轻量，插件热更新不拖垮推理服务（`__init__.py:13-14`）。
- 消费方：Hermes 内核（工作台 `prototype/server.py` 的 `HermesAdapter` 间接使用，见 [prototype-server.md](./prototype-server.md)）。

## 2. 目录与文件清单

| 文件 | 行数 | 职责 |
| --- | ---: | --- |
| `plugin.yaml` | 21 | 插件元数据：名称、版本、依赖、8 个工具声明 |
| `__init__.py` | 323 | HTTP 客户端、8 个工具实现、schema、`register()` |

## 3. 生命周期与注册契约

| 环节 | 约定 | 出处 |
| --- | --- | --- |
| 入口 | 模块级 `register(ctx) -> None`，导出 `__all__ = ["register"]` | `__init__.py:309-323` |
| 注册参数 | `name`、`toolset="media"`、`schema={name,description,parameters}`、`handler`、`check_fn=engine_ready`、`description`、`emoji` | `__init__.py:310-320` |
| 可用性门 | `engine_ready()` 缓存 15s：`GET /health`（3s 超时）成功才挂出工具 | `__init__.py:78-93` |
| 返回值 | 统一 JSON 字符串；对象走 `json.dumps(ensure_ascii=False)` | `__init__.py:97-117` |
| 异常 | 任何异常转成 `{"error": "..."}`，不让模型断链 | `__init__.py:101-117` |
| 文档声明 | `plugin.yaml` 的 `provides_tools` 必须与 `_HANDLERS` 键一致（8 个） | `plugin.yaml:12-20`、`__init__.py:297-306` |

## 4. 工具 → 引擎接口映射

| 工具 | 引擎调用 | 超时 | 必填 | 出处 |
| --- | --- | --- | --- | --- |
| `media_intro_video` | `POST /v1/video/jobs` | `MEDIA_JOB_TIMEOUT` 900s | 无 | `__init__.py:121-134` |
| `media_intro_status` | `GET /v1/video/{employee_id}` | 60s | `employee_id` | `__init__.py:137-141` |
| `media_profile` | `GET /v1/profiles` | 60s | 无 | `__init__.py:144-150` |
| `media_avatars` | `GET /v1/avatars` | 60s | 无 | `__init__.py:153-154` |
| `media_tts` | `POST /v1/tts`（`format=json`） | `MEDIA_TTS_TIMEOUT` 900s | `text` | `__init__.py:157-164` |
| `media_asr` | `POST /v1/asr` | `MEDIA_ASR_TIMEOUT` 300s | `audio_path` 或 `audio_base64` | `__init__.py:167-175` |
| `media_boss_briefing` | `POST /v1/jobs`（`kind=briefing`） | `MEDIA_JOB_TIMEOUT` 900s | 无 | `__init__.py:178-184` |
| `media_job_status` | `GET /v1/jobs` 或 `GET /v1/jobs/{id}` | 60s / 900s | 无 | `__init__.py:187-193` |

### 4.1 入参默认值

| 工具 | 参数 | 默认 | 说明 |
| --- | --- | --- | --- |
| `media_intro_video` | `all` / `force` | `false` | 批量或强制重生成 |
| | `refresh_text` | `true` | `force` 时同时重写文案（`__init__.py:126`） |
| | `wait` | `true` | 同步等出片 |
| | `wait_sec` | `90` | **插件默认 90s，引擎接口默认 300s**（`__init__.py:128`、`media-engine/server.py:309`） |
| `media_profile` | `include_assets` | `true` | 显式 `false` 才关闭（`__init__.py:149`） |
| `media_tts` | `speed` | `1.0` | 语速倍率 |
| `media_boss_briefing` | `wait_sec` | `30` | 文案提示「生成通常更久，建议 false + 轮询」（`__init__.py:179, 280`） |
| `media_job_status` | `limit` | `20` | 仅无 `job_id` 时生效 |
| | `wait_sec` | `0` | 长轮询，引擎侧上限 300 |

## 5. 配置项

| 变量 | 默认 | 说明 | 出处 |
| --- | --- | --- | --- |
| `MEDIA_ENGINE_URL` | `http://192.168.21.206:9200` | 引擎地址，末尾 `/` 自动去除 | `__init__.py:32` |
| `MEDIA_ENGINE_TOKEN` | 空 | 非空时每个请求带 `Authorization: Bearer` | `__init__.py:33, 56-57` |
| `MEDIA_PLATFORM_TIMEOUT` | `60` | 普通请求超时 | `__init__.py:34` |
| `MEDIA_JOB_TIMEOUT` | `900` | 带等待的任务请求超时 | `__init__.py:35` |
| `MEDIA_TTS_TIMEOUT` | `900` | 单次 TTS 合成超时（首次含模型加载） | `__init__.py:36` |
| `MEDIA_ASR_TIMEOUT` | `300` | 单次 ASR 超时 | `__init__.py:37` |

> 地址口径已统一为 `192.168.21.206:9200`：`plugin.yaml:8` 文案、模块 docstring（`__init__.py:1`）与代码默认值（`__init__.py:32`）一致；部署时仍以 `MEDIA_ENGINE_URL` 环境变量为准。

## 6. 错误语义

| 场景 | 返回 | 出处 |
| --- | --- | --- |
| 引擎 4xx/5xx | `{"error":"media-engine HTTP <code>：<body 前 300 字符>"}` | `__init__.py:61-67` |
| 连不上 | `{"error":"连不上 media-engine（<URL>）：<reason>"}` | `__init__.py:68-69` |
| 请求超时 | `{"error":"media-engine 请求超时（<n>s）：<path>"}` | `__init__.py:70-71` |
| 非 JSON 响应 | `{"error":"media-engine 返回非 JSON：<原因>"}` | `__init__.py:72-75` |
| 参数缺失 | `{"error":"需要 text"}` / `{"error":"需要 audio_path 或 audio_base64"}` | `__init__.py:159-160, 173-174` |
| 引擎离线 | `check_fn` 返回 false，工具从模型视图隐藏（15s 缓存） | `__init__.py:81-93` |

## 7. 失败与降级

- **探活即降级**：`/health` 3s 内不通，8 个工具全部收起，模型不会反复拿到连接错误（`__init__.py:81-93`）。
- **无本地兜底**：插件不缓存任何媒体结果，引擎重启后需要重新生成。
- **超时分层**：TTS/ASR/任务各自独立超时，避免长合成拖死普通查询（§5）。
- **代理无状态**：每次调用独立 HTTP，无重试、无熔断、无并发限制。

## 8. 已知限制

1. 无重试与退避：瞬时 502/超时直接以 `error` 返回给模型（`__init__.py:58-75`）。
2. 无鉴权强制：`MEDIA_ENGINE_TOKEN` 为空时不带任何凭据；与引擎默认空 Token 叠加即完全开放（见 [media-engine.md](./media-engine.md) §8）。
3. `media_asr` 支持 `audio_path` 本机路径，工具描述明确写「本机音频文件绝对路径」，属于内网可信前提（`__init__.py:265-266`）。
4. `media_boss_briefing` 默认只等 30s，但视频/语音合成常超过该值；文档已提示用 `wait=false` + 轮询（`__init__.py:280`）。
5. 仅有 `plugin.yaml` 声明，无插件级单元测试；`engine_ready` 的 15s 缓存不可配置。

## 9. 验收手段

- 结构：`python3 -c "import ast;ast.parse(open('hermes-plugins/media-platform/__init__.py').read())"`。
- 声明一致：`plugin.yaml` 的 `provides_tools` 应为 8 项，且与 `_HANDLERS` 键集合相等。
- 探活：引擎在线时 `engine_ready()` 返回 True；停掉引擎后 ≤15s 内工具消失。
- 端到端：调用 `media_profile` 应返回员工档案；调用 `media_tts` 应返回 `audio_url/srt_url/segments`。
