# 模块规格 · media-engine（媒体推理宿主）

> 上级文档：[spec.md](../spec.md)。本文件只描述 `media-engine/`。
> 事实源：`server.py` 484 行、`tts_engine.py` 406 行、`asr_engine.py` 221 行、`video_engine.py` 894 行（`wc -l`）。

## 1. 模块定位

media-engine 是 Hermes 媒体能力的**单实例推理宿主**：把 TTS、ASR、数字人/汇报视频三类重推理收拢到一个 FastAPI 进程，向上以 `/v1/*` HTTP 接口供 `hermes-plugins/media-platform` 与 Android 客户端调用（`media-engine/server.py:2-12`）。

- 设计定位是**旁路**：S1 期间 `mcp-tts` / `mcp-asr` 仍在线，本服务出问题可直接停掉回滚（`server.py:10-11`）。
- 推理全部懒加载：TTS 走多进程池（每进程一份模型），ASR 在本进程加载 whisper（`server.py:10-11`、`tts_engine.py:40-46`）。
- 与工作台无直接调用关系；工作台通过 Hermes 插件间接使用（见 [hermes-plugins.md](./hermes-plugins.md)）。

## 2. 目录与文件清单

| 文件 | 行数 | 职责 |
| --- | ---: | --- |
| `server.py` | 484 | FastAPI 路由、鉴权、TTL 清理、Prometheus 指标 |
| `tts_engine.py` | 406 | CosyVoice 切句/归一化/音色/进程池合成 |
| `asr_engine.py` | 221 | faster-whisper 转写 + 七道防幻觉门 |
| `video_engine.py` | 894 | 数字人档案、口型合成、自我介绍/老板汇报视频、任务队列 |
| `run.sh` | — | 生产启动入口（venv、线程数、worker 数、监听地址） |
| `restart.sh` | — | 按进程名 `pkill -TERM` 后 `setsid` 重启 |
| `work/` | — | 合成产物目录（音频、srt、segments），按 TTL 清理 |

## 3. 对外接口契约

### 3.1 鉴权模型

- 仅当 `MEDIA_ENGINE_TOKEN` 非空时启用 Bearer 校验：`Authorization: Bearer <TOKEN>`（`server.py:39, 72-78`）。
- 未通过返回 `401 {"error":"unauthorized"}`（`server.py:78-79`）。
- **默认 `TOKEN` 为空＝全部放行**（`server.py:39`），生产必须显式设置。

### 3.2 路由全集（已核实）

| 方法 | 路径 | 鉴权 | 入参 | 出参 / 行为 | 出处 |
| --- | --- | --- | --- | --- | --- |
| GET | `/` | 无 | — | `{service,version,uptime_sec}` | `server.py:111-113` |
| GET | `/health` | 无 | — | TTS 池、ASR 模型、视频能力、`work_dir` | `server.py:116-129` |
| GET | `/metrics` | 无 | — | Prometheus 文本 | `server.py:132-154` |
| GET | `/v1/voices` | Bearer | — | 音色表 | `server.py:157-162` |
| POST | `/v1/tts` | Bearer | `{text,voice?,speed=1.0,format=wav\|mp3\|json,stem?}` | 音频流或 JSON（含 srt/segments 链接） | `server.py:184-226` |
| POST | `/v1/asr` | Bearer | multipart `file/audio` 或 JSON `audio_base64/audio_path`、`language/model/initial_prompt` | 文本 + 时间轴 + 质量门结果 | `server.py:276-299`（入参解析 228-273） |
| POST | `/v1/video/jobs` | Bearer | `{employee_id?,all=false,force=false,refresh_text=true,wait=true,wait_sec=300,text?,voice?}` | 任务视图（可同步等待） | `server.py:319-338` |
| GET | `/v1/video/{employee_id}` | Bearer | — | 单员工成片状态，不触发生成 | `server.py:341-355` |
| POST | `/v1/jobs` | Bearer | `{kind:intro\|briefing, wait, wait_sec, ...}` | 通用异步任务 | `server.py:358-394` |
| GET | `/v1/jobs/{job_id}` | Bearer | `wait_sec`（0..300 长轮询） | 任务视图，过期 404 | `server.py:397-405` |
| GET | `/v1/jobs` | Bearer | `limit=20` | 最近任务 + 队列状态 | `server.py:408-413` |
| GET | `/v1/avatars` | Bearer | — | 可插拔数字人档案 | `server.py:416-421` |
| GET | `/v1/profiles` | Bearer | `id/dept/project/include_assets` | 员工档案 | `server.py:424-430` |
| GET | `/v1/assets/{subdir}/{name}` | **无** | `subdir ∈ {avatars,faces,videos,portraits,intro,live}` | 素材/成片下载，`FileResponse` 支持 Range | `server.py:439-453` |
| GET | `/files/{name}` | **无** | — | `work/` 产物下载（wav/mp3） | `server.py:457-465` |

### 3.3 TTS 细节

- `format` 仅接受 `wav|mp3|json`，其他 400（`server.py:188-190`）。
- wav/mp3 响应头携带 `X-Media-Duration`、`X-Media-Sample-Rate`、`X-Media-Srt`（`server.py:203-205`）。
- mp3 由 ffmpeg `libmp3lame -q:a 4` 转码（`_to_mp3` 定义 `server.py:173-178`，调用与失败处理 `208-215`），超时 120s。
- json 响应包含 `job_id/audio_url/audio_path/srt_url/segments_url/sample_rate/duration/segment_count/segments/synth_sec/rtf/workers`（`server.py:217-224`）。
- 合成失败 500，并累加 `tts_fail`（`server.py:197-200`）。

### 3.4 ASR 细节

- multipart 逐块读取，超过 `MEDIA_MAX_BODY_MB`（默认 25MB）即中止并删除临时文件（`server.py:259-271`）。
- JSON 模式支持 `audio_base64`、`audio`（可带 `data:` 前缀）与 `audio_path`（要求文件存在）（`server.py:233-251`）。
- 入参解析失败 400，推理失败 500；临时文件在 `finally` 删除（`server.py:282-299`）。
- 结果为「无声」时累加 `asr_empty`（`server.py:290-292`）。
- 防幻觉七道门（时长 → 能量 → VAD → 文本置信 → 复读 → 黑名单 → 字符集）在 `asr_engine.py:14-45` 定义阈值，任一命中即判「无可转写语音」。

### 3.5 视频任务

- 两种入口：专用 `/v1/video/jobs`（仅 intro）与通用 `/v1/jobs`（intro / briefing）（`server.py:319-394`）。
- `wait=true` 时同步等待，`wait_sec` 最长 300s；建议长任务用 `wait=false` 后轮询（`server.py:309, 335, 399-402`）。
- 任务状态机：`queued → running → done|failed`，含 `queue_ahead`、`elapsed_ms`、`error`（`video_engine.py:786-847`）。
- `JobRegistry` 默认 2 worker、TTL 7200s、最多保留 200 条；**状态仅在内存，重启即丢**（`video_engine.py:786-790, 882-884`）。

## 4. 配置项

### 4.1 服务层（`server.py:38-44`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MEDIA_HOST` | `192.168.21.206` | 监听地址；与 `run.sh:20` 一致，只绑 21 网段（本机 `ens2f1np1`），回环不可用 |
| `MEDIA_PORT` | `9200` | 监听端口 |
| `MEDIA_WORK_DIR` | `<模块>/work` | 产物目录，启动即创建 |
| `MEDIA_ENGINE_TOKEN` | 空 | Bearer Token；空＝不鉴权 |
| `MEDIA_TTL_HOURS` | `24` | 产物保留小时数 |
| `MEDIA_MAX_BODY_MB` | `25` | multipart 音频体积上限 |
| `FFMPEG` | `shutil.which("ffmpeg")` 或 `~/.local/bin/ffmpeg` | 转码二进制 |
| `MEDIA_PRELOAD` | `0` | `1` 时启动即加载 TTS/ASR 模型（`server.py:473-480`） |

### 4.2 TTS（`tts_engine.py:29-46`）

| 变量 | 默认 | 说明 | 出处 |
| --- | --- | --- | --- |
| `TTS_MODEL_DIR` | 自动探测 Fun-CosyVoice3-0.5B，缺失回退 CosyVoice2-0.5B | 模型目录 | `tts_engine.py:31-33` |
| `MEDIA_VOICES_FILE` | `/opt/sefs/mcp-tts/voices.json` | 音色表唯一数据源 | `tts_engine.py:35` |
| `TTS_WORKERS` | `2`（代码）/ `6`（`run.sh`） | 进程池大小 | `tts_engine.py:42`、`run.sh:13` |
| `TTS_THREADS` | `16` | 每进程线程数；`run.sh` 同步设置 OMP/MKL/OPENBLAS | `tts_engine.py:43` |
| `TTS_SYNTH_TIMEOUT` | `600` | 单次合成超时（秒） | `tts_engine.py:44` |

> 内存约束：代码注释实测每 worker RSS 约 6.6–7.8GB，S1 期间默认 2 个以避免与 mcp-tts 叠加到 ~84GB（`tts_engine.py:38-41`）。

### 4.3 ASR（`asr_engine.py:15-34`）

| 变量 | 默认 | 语义 |
| --- | --- | --- |
| `ASR_MODEL` | `small` | faster-whisper 模型 |
| `ASR_LANG` | `zh` | 默认语言 |
| `ASR_MIN_SEC` | `0.3` | 最短音频时长 |
| `ASR_MIN_RMS` | `0.002` | 峰值 RMS 下限（约 -54 dBFS） |
| `ASR_MIN_SPEECH_RATIO` | `0.05` | 有声帧最低占比 |
| `ASR_NO_SPEECH_PROB` | `0.6` | 无语音概率上限 |
| `ASR_MIN_AVG_LOGPROB` | `-1.0` | 平均对数概率下限 |
| `ASR_MAX_COMPRESSION` | `3.0` | 压缩比上限（防复读） |
| `ASR_MAX_NON_CJK_RATIO` | `0.6` | 中文场景非中日韩字符占比上限 |
| `ASR_LOW_CONFIDENCE` | `0.5` | 指令执行置信度门槛 |

### 4.4 视频（`video_engine.py:45-60, 882-884`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MEDIA_ASSET_DIRS` | `/home/boray/opc-workbench/prototype/assets` | 素材根，`:` 分隔多根 |
| `MEDIA_ASSET_WRITE_DIR` | 首个素材根 | 写入根 |
| `MEDIA_ASSET_URL_BASE` | 空 | 对外相对前缀 |
| `MEDIA_PUBLIC_BASE` | 空 | 对外绝对前缀 |
| `MEDIA_PROFILES` | `/opt/sefs/mcp-asr/employees.json` | 员工档案 |
| `MEDIA_HERMES_CLI` | `/opt/sefs/hermes/run-hermes.sh` | 生成口播文案 |
| `MEDIA_VIDEO_WORKERS` | `2` | 视频任务 worker |
| `MEDIA_JOB_TTL` | `7200` | 完成任务保留秒数 |
| `MEDIA_JOB_MAX` | `200` | 任务表上限 |
| `ASR_ASSET_URL_BASE` / `ASR_PUBLIC_BASE` | `run.sh:16-17` 设 `/conring/asr/assets` / `https://ai.boraydata.com` | 兼容旧客户端素材路径 |

`_cfg(name)` 的读取顺序为 `MEDIA_<name>` → `ASR_<name>` → 默认值（`video_engine.py:42-44`）。

## 5. 指标

`/metrics` 暴露（`server.py:132-154`）：

- `media_engine_requests_total{op="tts"|"asr"}`
- `media_engine_failures_total{op="tts"|"asr"}`
- `media_engine_asr_empty_total`
- `media_engine_video_jobs_total`、`media_engine_video_failures_total`
- `media_engine_latency_ms_total{op}` 与 `media_engine_latency_ms_count{op}`

## 6. 生命周期与清理

- 启动时创建 `WORK_DIR`（`server.py:45`），可选预热模型（`server.py:473-480`）。
- 后台线程每 600s 扫描一次，删除 `mtime` 早于 `now-TTL` 的**文件**（不递归子目录）（`server.py:85-100`）。
- `restart.sh` 先 `pkill -TERM -f media-engine/server.py`，最多等 20s，再 `setsid --fork sh run.sh`。

## 7. 失败与降级

| 场景 | 行为 | 出处 |
| --- | --- | --- |
| TTS 进程池有失败进程 | `/health` 返回 `status=degraded` | `server.py:109-116` |
| 模型未加载 | 懒加载，首个请求承担加载耗时 | `server.py:10-11` |
| mp3 转码失败 | 500 `mp3 转码失败`，累加 `tts_fail` | `server.py:209-214` |
| 音频超限 | 400，临时文件删除 | `server.py:266-270` |
| 任务失败 | `status=failed` + `error`，任务表保留至 TTL | `video_engine.py:831-840` |
| 任务不存在/过期 | 404 | `server.py:403-404` |
| 清理异常 | 只记日志，不影响服务 | `server.py:95-97` |
| 服务崩溃 | 旁路设计，工作台主流程不受影响 | `server.py:10-11` |

## 8. 已知限制

1. **`/v1/assets/*` 与 `/files/*` 不校验 Bearer**，`TOKEN` 为空时整个服务对可达网络完全开放（`server.py:439-465, 39`）。
2. 只监听 `192.168.21.206`（`server.py:38` 默认值与 `run.sh:20` 已统一），`127.0.0.1:9200` 连接会被拒；本机调用方必须写 `http://192.168.21.206:9200`。
3. `TTS_WORKERS` 代码默认 2、`run.sh:13` 默认 6，用 `run.sh` 启动时实际以 6 运行（`tts_engine.py:42`）。
4. 视频任务表在内存，重启后进行中的任务全部丢失，需客户端重提（`video_engine.py:787`）。
5. `/v1/asr` 的 JSON `audio_path` 模式接受服务端任意可读路径，属于「本地文件读取面」，生产应只允许 `audio_base64`（`server.py:237-240`）。
6. 无并发限流；TTS 请求直接排队到进程池，突发流量会拉长延迟。

## 9. 验收手段

- 健康：`curl -s http://<host>:9200/health | python3 -m json.tool`，检查 `tts.failed`、`asr.loaded`、`video.ffmpeg`。
- 鉴权：设置 `MEDIA_ENGINE_TOKEN=t` 后，`curl -i http://<host>:9200/v1/voices` 应 401；带 `-H "Authorization: Bearer t"` 应 200。
- 任务：`POST /v1/video/jobs` 后轮询 `GET /v1/jobs/{job_id}?wait_sec=10`，观察 `queued→running→done`。
- 清理：在 `work/` 放一个旧 mtime 文件，等待清理线程（≤600s）后文件消失。
