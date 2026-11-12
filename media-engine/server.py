#!/usr/bin/env python
"""media-engine：Hermes 媒体能力的单实例推理宿主（方案 S1）。

  GET  /health          健康与加载状态
  GET  /v1/voices       音色表
  POST /v1/tts          文本 → wav/mp3 音频流，或 json（含 srt/segments 链接）
  POST /v1/asr          multipart(file) 或 json(audio_base64/audio_path) → 文本 + 时间轴
  GET  /files/{name}    取回合成产物（srt / segments.json / mp3）
  GET  /metrics         Prometheus 文本格式

推理全部在本进程外/懒加载：TTS 走多进程池（每进程一份模型），ASR 走本进程 whisper。
S1 期间 mcp-tts / mcp-asr 保持在线，本服务只做旁路，出问题停掉即可回滚。
"""

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import asr_engine
import qwen_engine
import tts_engine
import video_engine

SERVICE = "media-engine"
VERSION = "0.1.0"
STARTED_AT = time.time()

HOST = os.environ.get("MEDIA_HOST", "192.168.21.206")
PORT = int(os.environ.get("MEDIA_PORT", "9200"))
WORK_DIR = os.environ.get("MEDIA_WORK_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "work"))
TOKEN = os.environ.get("MEDIA_ENGINE_TOKEN", "").strip()
TTL_HOURS = float(os.environ.get("MEDIA_TTL_HOURS", "24"))
MAX_BODY_MB = float(os.environ.get("MEDIA_MAX_BODY_MB", "25"))
FFMPEG = os.environ.get("FFMPEG", shutil.which("ffmpeg") or "/home/boray/.local/bin/ffmpeg")

os.makedirs(WORK_DIR, exist_ok=True)

app = FastAPI(title=SERVICE, version=VERSION, docs_url=None, redoc_url=None)

# ---------------------------------------------------------------- 指标
_METRICS = {
    "tts_requests": 0, "tts_fail": 0, "tts_ms": 0.0,
    "asr_requests": 0, "asr_fail": 0, "asr_ms": 0.0, "asr_empty": 0,
    "video_jobs": 0, "video_fail": 0,
}
_METRICS_LOCK = threading.Lock()


def _count(key, delta=1):
    with _METRICS_LOCK:
        _METRICS[key] = _METRICS.get(key, 0) + delta


class _Timer:
    def __init__(self, latency_key):
        self.key = latency_key

    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, exc_type, *_):
        _count(self.key, (time.time() - self.t0) * 1000)


def log(msg):
    sys.stderr.write("[media-engine] %s\n" % msg)
    sys.stderr.flush()


def _authorized(request):
    if not TOKEN:
        return True
    auth = request.headers.get("authorization", "")
    return auth == "Bearer " + TOKEN


def _forbidden():
    return JSONResponse({"error": "unauthorized"}, status_code=401)


# ---------------------------------------------------------------- TTL 清理
def _cleanup_loop():
    while True:
        cutoff = time.time() - TTL_HOURS * 3600
        removed = 0
        try:
            for name in os.listdir(WORK_DIR):
                p = os.path.join(WORK_DIR, name)
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.unlink(p)
                    removed += 1
        except Exception as e:  # 清理失败不能影响服务
            log("清理失败：%s" % e)
        if removed:
            log("清理过期产物 %d 个（TTL %.0fh）" % (removed, TTL_HOURS))
        time.sleep(600)


# ---------------------------------------------------------------- 基础
@app.get("/")
def root():
    return {"service": SERVICE, "version": VERSION, "uptime_sec": round(time.time() - STARTED_AT, 1)}


@app.get("/health")
def health():
    tts = tts_engine.pool_status()
    asr = asr_engine.model_status()
    status = "ok"
    if tts.get("failed"):
        status = "degraded"
    return {"status": status, "service": SERVICE, "version": VERSION,
            "uptime_sec": round(time.time() - STARTED_AT, 1),
            "tts": tts,
            "asr": {"loaded": bool(asr), "models": list(asr), "default_model": asr_engine.MODEL_NAME,
                    "default_language": asr_engine.DEFAULT_LANG},
            "qwen": qwen_engine.health(),
            "video": video_engine.capabilities(),
            "work_dir": WORK_DIR}


@app.get("/metrics")
def metrics():
    with _METRICS_LOCK:
        m = dict(_METRICS)
    lines = ["# HELP media_engine_requests_total 请求总数",
             "# TYPE media_engine_requests_total counter",
             "media_engine_requests_total{op=\"tts\"} %d" % m["tts_requests"],
             "media_engine_requests_total{op=\"asr\"} %d" % m["asr_requests"],
             "# TYPE media_engine_failures_total counter",
             "media_engine_failures_total{op=\"tts\"} %d" % m["tts_fail"],
             "media_engine_failures_total{op=\"asr\"} %d" % m["asr_fail"],
             "# TYPE media_engine_asr_empty_total counter",
             "media_engine_asr_empty_total %d" % m["asr_empty"],
             "# TYPE media_engine_video_jobs_total counter",
             "media_engine_video_jobs_total %d" % m["video_jobs"],
             "media_engine_video_failures_total %d" % m["video_fail"],
             "# TYPE media_engine_latency_ms_total counter",
             "media_engine_latency_ms_total{op=\"tts\"} %.1f" % m["tts_ms"],
             "media_engine_latency_ms_total{op=\"asr\"} %.1f" % m["asr_ms"],
             "# TYPE media_engine_latency_ms_count counter",
             "media_engine_latency_ms_count{op=\"tts\"} %d" % m["tts_requests"],
             "media_engine_latency_ms_count{op=\"asr\"} %d" % m["asr_requests"]]
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/v1/voices")
def voices(request: Request):
    if not _authorized(request):
        return _forbidden()
    out = tts_engine.list_voices()
    out["engines"] = ["cosyvoice", "qwen3"]
    out["qwen3"] = {"speakers": list(qwen_engine.SPEAKERS),
                    "default": qwen_engine.DEFAULT_SPEAKER,
                    "note": "CPU 上 RTF 约 24~38，仅适合离线配音，非实时"}
    return out


# ---------------------------------------------------------------- TTS
class TTSRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: float = 1.0
    format: str = "wav"      # wav | mp3 | json
    stem: str | None = None
    engine: str = "cosyvoice"   # cosyvoice（默认，实时）| qwen3（离线，高质量）
    speaker: str | None = None  # engine=qwen3 时的音色（Vivian/Serena/...）
    language: str | None = None
    instruct: str | None = None  # engine=qwen3 时的情绪指令，如「兴奋地说」


def _to_mp3(wav_path, out_path):
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", wav_path,
           "-codec:a", "libmp3lame", "-q:a", "4", out_path]
    subprocess.run(cmd, check=True, timeout=120)
    return out_path


def _file_url(name):
    return "/files/" + name


@app.post("/v1/tts")
async def tts(payload: TTSRequest, request: Request):
    if not _authorized(request):
        return _forbidden()
    fmt = (payload.format or "wav").lower()
    if fmt not in ("wav", "mp3", "json"):
        return JSONResponse({"error": "format 只支持 wav/mp3/json"}, status_code=400)
    engine = (payload.engine or "cosyvoice").lower()
    if engine not in ("cosyvoice", "qwen3"):
        return JSONResponse({"error": "engine 只支持 cosyvoice / qwen3"}, status_code=400)
    _count("tts_requests")
    try:
        with _Timer("tts_ms"):
            if engine == "qwen3":
                result = await run_in_threadpool(
                    qwen_engine.synthesize, payload.text, payload.speaker or payload.voice,
                    payload.language, payload.instruct, WORK_DIR, payload.stem)
            else:
                result = await run_in_threadpool(
                    tts_engine.synthesize, payload.text, payload.voice, payload.speed,
                    WORK_DIR, payload.stem)
    except Exception as e:
        _count("tts_fail")
        log("TTS 失败（%s）：%s" % (engine, e))
        return JSONResponse({"error": str(e)}, status_code=500)

    wav_path = result["audio_path"]
    headers = {"X-Media-Duration": str(result["duration"]),
               "X-Media-Sample-Rate": str(result["sample_rate"]),
               "X-Media-Engine": result.get("engine", "cosyvoice"),
               "X-Media-Srt": _file_url(os.path.basename(result["srt_path"]))}
    if fmt == "wav":
        return FileResponse(wav_path, media_type="audio/wav", headers=headers)
    if fmt == "mp3":
        try:
            mp3_path = await run_in_threadpool(_to_mp3, wav_path, wav_path[:-4] + ".mp3")
        except Exception as e:
            _count("tts_fail")
            log("mp3 转码失败：%s" % e)
            return JSONResponse({"error": "mp3 转码失败：%s" % e}, status_code=500)
        return FileResponse(mp3_path, media_type="audio/mpeg", headers=headers)

    return {"job_id": result["stem"], "voice": payload.voice or "default",
            "engine": result.get("engine", "cosyvoice"),
            "speaker": result.get("speaker"),
            "audio_url": _file_url(os.path.basename(wav_path)),
            "audio_path": wav_path,
            "srt_url": _file_url(os.path.basename(result["srt_path"])),
            "segments_url": _file_url(os.path.basename(result["segments_path"])),
            "sample_rate": result["sample_rate"], "duration": result["duration"],
            "segment_count": result["segment_count"], "segments": result["segments"],
            "synth_sec": result["synth_sec"], "rtf": result["rtf"], "workers": result["workers"]}


# ---------------------------------------------------------------- ASR
async def _asr_input(request: Request):
    """multipart 或 json 两种入参，统一落到临时文件。返回 (path, tmp?, language, model, prompt, engine)。"""
    ctype = request.headers.get("content-type", "")
    lang = model = prompt = engine = None
    audio_path = tmp = None
    if ctype.startswith("application/json"):
        body = await request.json()
        lang, model = body.get("language"), body.get("model")
        prompt = body.get("initial_prompt")
        engine = body.get("engine")
        if body.get("audio_path"):
            audio_path = body["audio_path"]
            if not os.path.exists(audio_path):
                raise ValueError("音频文件不存在：%s" % audio_path)
        else:
            b64 = body.get("audio_base64") or body.get("audio")
            if not b64:
                raise ValueError("需要 file、audio_base64 或 audio_path")
            if b64.startswith("data:"):
                b64 = b64.split(",", 1)[-1]
            raw = base64.b64decode(b64)
            fd, tmp = tempfile.mkstemp(prefix="asr_", suffix=".bin", dir=WORK_DIR)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            audio_path = tmp
    else:
        form = await request.form()
        upload = form.get("file") or form.get("audio")
        if upload is None:
            raise ValueError("需要 file 字段")
        lang, model = form.get("language"), form.get("model")
        prompt = form.get("initial_prompt")
        engine = form.get("engine")
        fd, tmp = tempfile.mkstemp(prefix="asr_", suffix=".bin", dir=WORK_DIR)
        size = 0
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = await upload.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BODY_MB * 1024 * 1024:
                    f.close()
                    os.unlink(tmp)
                    raise ValueError("音频超过 %.0fMB 上限" % MAX_BODY_MB)
                f.write(chunk)
        audio_path = tmp
    return audio_path, tmp, lang, model, prompt, engine


@app.post("/v1/asr")
async def asr(request: Request):
    if not _authorized(request):
        return _forbidden()
    _count("asr_requests")
    try:
        audio_path, tmp, lang, model, prompt, engine = await _asr_input(request)
    except Exception as e:
        _count("asr_fail")
        return JSONResponse({"error": str(e)}, status_code=400)
    engine = (engine or "whisper").lower()
    if engine not in ("whisper", "qwen3"):
        _count("asr_fail")
        return JSONResponse({"error": "engine 只支持 whisper / qwen3"}, status_code=400)
    try:
        with _Timer("asr_ms"):
            if engine == "qwen3":
                # 先过音频级门（毫秒级），避免把静音送给 Qwen 让它幻觉出文本
                _, duration, _, reason = await run_in_threadpool(asr_engine.probe, audio_path)
                if reason:
                    result = qwen_engine.empty_result(lang or asr_engine.DEFAULT_LANG,
                                                      duration, reason)
                else:
                    result = await run_in_threadpool(qwen_engine.transcribe, audio_path, lang)
                    kept, reasons = asr_engine.filter_segments(
                        [{"text": result["text"], "start": 0.0, "end": result["duration"],
                          "avg_logprob": 0.0, "no_speech_prob": 0.0, "compression_ratio": 1.0}],
                        lang or asr_engine.DEFAULT_LANG)
                    if reasons:
                        result.update({"text": "", "segments": [], "silent": True,
                                       "silent_reason": "blacklist",
                                       "filtered": sum(reasons.values()),
                                       "filtered_reasons": reasons})
            else:
                result = await run_in_threadpool(
                    asr_engine.transcribe, audio_path, lang, model, prompt)
        if result.get("silent"):
            _count("asr_empty")
        return result
    except Exception as e:
        _count("asr_fail")
        log("ASR 失败：%s" % e)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------- 数字人与视频
class IntroRequest(BaseModel):
    employee_id: str | None = None
    all: bool = False
    force: bool = False
    refresh_text: bool = True
    wait: bool = True
    wait_sec: float = 300.0
    text: str | None = None
    voice: str | None = None


def _job_view(job, wait_sec=0.0):
    """等任务（可选）并返回统一视图；result 里是各 kind 的业务结果。"""
    return video_engine.JOBS.get(job["job_id"], wait_sec if wait_sec > 0 else 0.0)


@app.post("/v1/video/jobs")
async def video_jobs(payload: IntroRequest, request: Request):
    """提交自我介绍视频任务；wait=true 时同步等到出片（长任务建议 wait=false）。"""
    if not _authorized(request):
        return _forbidden()
    try:
        ids = video_engine.resolve_ids(payload.employee_id, payload.all)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    _count("video_jobs")
    job = video_engine.JOBS.submit(
        "intro",
        lambda: video_engine.intro_job(ids, force=payload.force,
                                       refresh_text=payload.refresh_text,
                                       text=payload.text, voice=payload.voice),
        meta={"ids": ids, "force": payload.force})
    view = await run_in_threadpool(_job_view, job, payload.wait_sec if payload.wait else 0.0)
    if view.get("status") == "failed":
        _count("video_fail")
    return view


@app.get("/v1/video/{employee_id}")
def video_status(employee_id: str, request: Request):
    """查单个员工的成片状态（不触发生成）。"""
    if not _authorized(request):
        return _forbidden()
    aid = os.path.basename(employee_id)
    path = os.path.join(video_engine.INTRO_DIR, "%s.mp4" % aid) if video_engine.INTRO_DIR else ""
    ready = bool(path and os.path.exists(path))
    meta = video_engine.intro_meta_of(aid) if ready else {}
    url = video_engine._asset_url("intro", "%s.mp4" % aid) if ready else ""
    return {"id": aid, "ready": ready, "video": url,
            "video_full": video_engine._asset_url_full("intro", "%s.mp4" % aid) if ready else "",
            "text": video_engine._read_intro_text(aid) if ready else "",
            "duration": meta.get("duration") or 0, "size": meta.get("size") or 0,
            "source": meta.get("source"), "generated_at": meta.get("generated_at")}


@app.post("/v1/jobs")
async def create_job(request: Request):
    """通用异步任务入口：kind=intro（同 /v1/video/jobs）或 briefing（老板上线汇报）。"""
    if not _authorized(request):
        return _forbidden()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
    kind = (body.get("kind") or "").strip()
    wait_sec = float(body.get("wait_sec") or 0)
    if kind == "intro":
        try:
            ids = video_engine.resolve_ids(body.get("employee_id"), bool(body.get("all")))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _count("video_jobs")
        job = video_engine.JOBS.submit(
            "intro",
            lambda: video_engine.intro_job(ids, force=bool(body.get("force")),
                                           refresh_text=body.get("refresh_text", True),
                                           text=body.get("text"), voice=body.get("voice")),
            meta={"ids": ids, "force": bool(body.get("force"))})
    elif kind == "briefing":
        _count("video_jobs")
        job = video_engine.JOBS.submit(
            "briefing",
            lambda: video_engine.boss_briefing(voice=body.get("voice"),
                                               emotion=body.get("emotion") or "calm"),
            meta={"scene": "boss_online"})
    else:
        return JSONResponse({"error": "kind 只支持 intro / briefing"}, status_code=400)
    view = await run_in_threadpool(_job_view, job,
                                   wait_sec if body.get("wait", True) and wait_sec else 0.0)
    if view.get("status") == "failed":
        _count("video_fail")
    return view


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, request: Request, wait_sec: float = 0):
    """按 job_id 取结果；wait_sec 可长轮询（最多 300s），避免高频轮询。"""
    if not _authorized(request):
        return _forbidden()
    view = video_engine.JOBS.get(job_id, min(max(wait_sec, 0.0), 300.0))
    if not view:
        return JSONResponse({"error": "任务不存在或已过期：%s" % job_id}, status_code=404)
    return view


@app.get("/v1/jobs")
def list_jobs(request: Request, limit: int = 20):
    if not _authorized(request):
        return _forbidden()
    jobs = video_engine.JOBS.list(limit)
    return {"count": len(jobs), "jobs": jobs, "queue": video_engine.JOBS.status()}


@app.get("/v1/avatars")
def avatars(request: Request):
    """可插拔的数字人档案（照片 + 可选仿真人视频素材）。"""
    if not _authorized(request):
        return _forbidden()
    return video_engine.list_avatars()


@app.get("/v1/profiles")
def profiles(request: Request, id: str | None = None, dept: str | None = None,
             project: str | None = None, include_assets: bool = True):
    """员工档案：可按 id / dept / project 过滤。"""
    if not _authorized(request):
        return _forbidden()
    return video_engine.list_profiles(id, dept, project, include_assets)


_MEDIA_TYPES = {".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".mp3": "audio/mpeg", ".wav": "audio/wav",
                ".m3u8": "application/vnd.apple.mpegurl", ".txt": "text/plain; charset=utf-8",
                ".json": "application/json", ".srt": "text/plain; charset=utf-8"}


@app.get("/v1/assets/{subdir}/{name}")
def assets(subdir: str, name: str):
    """素材与成片下载（支持 Range，客户端可边下边播 mp4）。"""
    safe_sub = os.path.basename(subdir)
    safe = os.path.basename(name)
    if safe_sub not in ("avatars", "faces", "videos", "portraits", "intro", "live"):
        return JSONResponse({"error": "not found"}, status_code=404)
    for root in video_engine.asset_roots():
        root_real = os.path.realpath(root)
        path = os.path.realpath(os.path.join(root_real, safe_sub, safe))
        if path.startswith(root_real + os.sep) and os.path.isfile(path):
            media = _MEDIA_TYPES.get(os.path.splitext(safe)[1].lower(),
                                     "application/octet-stream")
            return FileResponse(path, media_type=media)
    return JSONResponse({"error": "not found"}, status_code=404)


# ---------------------------------------------------------------- 产物下载
@app.get("/files/{name}")
def files(name: str):
    safe = os.path.basename(name)
    path = os.path.realpath(os.path.join(WORK_DIR, safe))
    if not path.startswith(os.path.realpath(WORK_DIR) + os.sep) or not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    media = "audio/wav" if safe.endswith(".wav") else (
        "audio/mpeg" if safe.endswith(".mp3") else "application/octet-stream")
    return FileResponse(path, media_type=media)


@app.on_event("startup")
def _startup():
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    log("启动：http://%s:%d work_dir=%s workers=%d asr_model=%s"
        % (HOST, PORT, WORK_DIR, tts_engine.WORKERS, asr_engine.MODEL_NAME))
    if os.environ.get("MEDIA_PRELOAD", "0") in ("1", "true", "True", "yes"):
        def _preload():
            try:
                tts_engine.get_pool()
                asr_engine._model(asr_engine.MODEL_NAME)
            except Exception as e:
                log("预热失败：%s" % e)
        threading.Thread(target=_preload, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
