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
import tts_engine

SERVICE = "media-engine"
VERSION = "0.1.0"
STARTED_AT = time.time()

HOST = os.environ.get("MEDIA_HOST", "127.0.0.1")
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
    return tts_engine.list_voices()


# ---------------------------------------------------------------- TTS
class TTSRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: float = 1.0
    format: str = "wav"      # wav | mp3 | json
    stem: str | None = None


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
    _count("tts_requests")
    try:
        with _Timer("tts_ms"):
            result = await run_in_threadpool(
                tts_engine.synthesize, payload.text, payload.voice, payload.speed,
                WORK_DIR, payload.stem)
    except Exception as e:
        _count("tts_fail")
        log("TTS 失败：%s" % e)
        return JSONResponse({"error": str(e)}, status_code=500)

    wav_path = result["audio_path"]
    headers = {"X-Media-Duration": str(result["duration"]),
               "X-Media-Sample-Rate": str(result["sample_rate"]),
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
            "audio_url": _file_url(os.path.basename(wav_path)),
            "srt_url": _file_url(os.path.basename(result["srt_path"])),
            "segments_url": _file_url(os.path.basename(result["segments_path"])),
            "sample_rate": result["sample_rate"], "duration": result["duration"],
            "segment_count": result["segment_count"], "segments": result["segments"],
            "synth_sec": result["synth_sec"], "rtf": result["rtf"], "workers": result["workers"]}


# ---------------------------------------------------------------- ASR
async def _asr_input(request: Request):
    """multipart 或 json 两种入参，统一落到临时文件。返回 (path, tmp?, language, model, prompt)。"""
    ctype = request.headers.get("content-type", "")
    lang = model = prompt = None
    audio_path = tmp = None
    if ctype.startswith("application/json"):
        body = await request.json()
        lang, model = body.get("language"), body.get("model")
        prompt = body.get("initial_prompt")
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
    return audio_path, tmp, lang, model, prompt


@app.post("/v1/asr")
async def asr(request: Request):
    if not _authorized(request):
        return _forbidden()
    _count("asr_requests")
    try:
        audio_path, tmp, lang, model, prompt = await _asr_input(request)
    except Exception as e:
        _count("asr_fail")
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        with _Timer("asr_ms"):
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
