#!/usr/bin/env python
"""media-gen：本机文生图 / 文生视频推理服务（纯 CPU，端口 9103）。

为什么单独一个服务：diffusers 需要 0.42（WanPipeline），而 media-engine 跑在
CosyVoice 的 venv（diffusers 0.29），两边依赖不兼容；另外推理会吃满 CPU，
放在网关进程里会拖垮 TTS/ASR。

  GET  /health             健康 + 各渠道加载状态
  GET  /v1/backends        可选渠道清单（含不可用原因）
  POST /v1/image/jobs      文生图（默认 sd15 → Realistic Vision V5.1）
  POST /v1/video/jobs      文生视频（默认 wan21 → WAN2.1-T2V-1.3B）
  GET  /v1/jobs/{id}       查任务，可 ?wait_sec= 长轮询
  GET  /v1/jobs            最近任务列表
  GET  /files/{name}       取回产物（png / mp4）
  GET  /metrics            Prometheus 文本

设计：
  - 单 worker 线程串行推理（CPU 抢满了并行只会更慢），队列 + 落盘 jobs.json
  - 模型懒加载，空闲 MEDIA_GEN_IDLE_UNLOAD 秒后卸载，避免常驻吃 20GB+
  - 渠道可插拔：后端在 BACKENDS 里声明，探测失败只影响该渠道的可用性
"""

import json
import os
import queue
import random
import sys
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

SERVICE = "media-gen"
VERSION = "0.1.0"
STARTED_AT = time.time()

HOST = os.environ.get("MEDIA_GEN_HOST", "192.168.21.206")
PORT = int(os.environ.get("MEDIA_GEN_PORT", "9103"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("MEDIA_GEN_OUT_DIR", os.path.join(BASE_DIR, "output"))
JOBS_FILE = os.path.join(OUT_DIR, "jobs.json")
TOKEN = os.environ.get("MEDIA_GEN_TOKEN", "").strip()
THREADS = int(os.environ.get("MEDIA_GEN_THREADS", "0"))  # 0 = 用全部核心
IDLE_UNLOAD = float(os.environ.get("MEDIA_GEN_IDLE_UNLOAD", "900"))
SD_MODEL = os.environ.get("MEDIA_GEN_SD_MODEL", "/opt/sefs/models/Realistic_Vision_V5.1")
MOTION_MODEL = os.environ.get(
    "MEDIA_GEN_MOTION_MODEL", "/opt/sefs/models/animatediff-motion-adapter-v1-5-2")
WAN_MODEL = os.environ.get(
    "MEDIA_GEN_WAN_MODEL", "/opt/sefs/models/Wan2.1-T2V-1.3B-Diffusers")
H3_MODEL = os.environ.get("MEDIA_GEN_H3_MODEL", "/opt/sefs/models/MiniMax-H3")
WAN_DTYPE = os.environ.get("MEDIA_GEN_WAN_DTYPE", "bfloat16")
FFMPEG = os.environ.get("MEDIA_GEN_FFMPEG", "/usr/bin/ffmpeg")

os.makedirs(os.path.join(OUT_DIR, "images"), exist_ok=True)
os.makedirs(os.path.join(OUT_DIR, "videos"), exist_ok=True)

app = FastAPI(title=SERVICE, version=VERSION, docs_url=None, redoc_url=None)


def log(msg):
    sys.stderr.write("[media-gen] %s\n" % msg)
    sys.stderr.flush()


def _authorized(request):
    if not TOKEN:
        return True
    return request.headers.get("authorization", "") == "Bearer " + TOKEN


def _forbidden():
    return JSONResponse({"error": "unauthorized"}, status_code=401)

# ---------------------------------------------------------------- 渠道定义
# available 为 None 表示"待探测"；探测结果在 /v1/backends 里实时算
BACKENDS = {
    "sd15": {
        "kind": "image", "label": "SD1.5 · Realistic Vision V5.1",
        "path": SD_MODEL, "notes": "写实人像/产品图，CPU 512x512 约 9.5s/步",
    },
    "animatediff": {
        "kind": "video", "label": "AnimateDiff v1.5.2 + SD1.5（肖像微动）",
        "path": MOTION_MODEL, "notes": "图生视频微动，需输入首帧；适合数字人呼吸/点头",
    },
    "wan21": {
        "kind": "video", "label": "WAN2.1-T2V-1.3B",
        "path": WAN_MODEL, "notes": "文生视频，480P 原生；CPU 上按帧数与步数线性增长",
    },
    "minimax_h3": {
        "kind": "video", "label": "MiniMax H3（本机开源权重尝试）",
        "path": H3_MODEL, "notes": "H3-Base 33B 音视频联合模型；单 checkpoint 144GB，"
                                 "量化 GGUF Q4 约 45GB；无 x86 纯 CPU 跑通案例，暂列不可用",
    },
}


def _path_ready(path):
    """模型目录可用 = 目录内至少有一个 safetensors/bin 权重。"""
    if not os.path.isdir(path):
        return False
    for root, _dirs, files in os.walk(path):
        for f in files:
            if f.endswith((".safetensors", ".bin", ".pth", ".gguf")):
                return True
    return False


def _wan_ready():
    """WAN 需要分片齐全且没有 aria2 未完成标记（断点续传期间文件大小是满的）。"""
    need = ["model_index.json", "scheduler/scheduler_config.json",
            "vae/diffusion_pytorch_model.safetensors",
            "transformer/diffusion_pytorch_model.safetensors.index.json",
            "text_encoder/model.safetensors.index.json"]
    for n in need:
        if not os.path.exists(os.path.join(WAN_MODEL, n)):
            return False, "缺少 %s" % n
    for root, _dirs, files in os.walk(WAN_MODEL):
        for f in files:
            if f.endswith(".aria2"):
                return False, "下载未完成（%s 仍在续传）" % f[:-6]
    for idx in ("transformer/diffusion_pytorch_model.safetensors.index.json",
                "text_encoder/model.safetensors.index.json"):
        try:
            with open(os.path.join(WAN_MODEL, idx), encoding="utf-8") as f:
                shards = set(json.load(f)["weight_map"].values())
        except Exception as e:
            return False, "索引不可读 %s：%s" % (idx, e)
        base = os.path.dirname(os.path.join(WAN_MODEL, idx))
        for s in shards:
            if not os.path.exists(os.path.join(base, s)):
                return False, "分片缺失 %s" % s
    return True, ""


def backends_view():
    out = []
    for name, b in BACKENDS.items():
        item = {"name": name, "kind": b["kind"], "label": b["label"],
                "path": b["path"], "notes": b["notes"],
                "weights_present": _path_ready(b["path"])}
        eng = _ENGINES.get(name)
        item["loaded"] = bool(eng and eng.loaded)
        item["available"] = item["weights_present"] and (eng.available if eng else True)
        if name == "minimax_h3" and not item["weights_present"]:
            item["reason"] = (
                "未接入：H3-Base 单 checkpoint 144GB（DiT 66.28 + Qwen3-VL-32B 文本编码器 66.73 "
                "+ 视频 VAE 10.42 + 音频 VAE 0.6 GB），量化 GGUF Q4 约 45GB；"
                "社区无 x86 纯 CPU 跑通案例，本机无 GPU，故不作为可用渠道")
        elif not item["weights_present"]:
            item["reason"] = "权重目录为空：%s" % b["path"]
        if name == "wan21":
            ok, why = _wan_ready()
            if not ok:
                item["available"] = False
                item["reason"] = why
        out.append(item)
    return out

# ---------------------------------------------------------------- 任务注册表
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_Q = queue.Queue()
_TTY = {"image": 0, "video": 0}
_FAILS = {"image": 0, "video": 0}


def _persist():
    try:
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_JOBS, f, ensure_ascii=False)
        os.replace(tmp, JOBS_FILE)
    except Exception as e:  # 落盘失败不影响推理
        log("jobs.json 落盘失败：%s" % e)


def _load_jobs():
    if not os.path.exists(JOBS_FILE):
        return
    try:
        with open(JOBS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log("jobs.json 读取失败：%s" % e)
        return
    with _JOBS_LOCK:
        _JOBS.update(data)
    # 重启后遗留的 running 任务标记为中断
    for j in _JOBS.values():
        if j["status"] in ("queued", "running"):
            j["status"] = "interrupted"
            j["error"] = "服务重启，任务中断"


def _job_view(j):
    v = dict(j)
    if v.get("output_path"):
        v["output_url"] = "/files/" + os.path.basename(v["output_path"])
    return v


class _Registry:
    pass


# ---------------------------------------------------------------- 引擎（懒加载）
class BaseEngine:
    name = ""
    kind = ""

    def __init__(self):
        self.pipe = None
        self.loaded = False
        self.available = True
        self.reason = ""
        self.last_used = 0.0
        self.load_sec = 0.0
        self._lock = threading.Lock()

    def unload(self):
        with self._lock:
            if self.pipe is not None:
                del self.pipe
                self.pipe = None
                self.loaded = False
                import gc

                gc.collect()
                log("%s 已卸载（空闲 %.0fs）" % (self.name, time.time() - self.last_used))


class SD15Engine(BaseEngine):
    name = "sd15"
    kind = "image"

    def _load(self):
        if self.pipe is not None:
            return self.pipe
        import torch
        from diffusers import AutoPipelineForText2Image, DPMSolverMultistepScheduler

        if THREADS > 0:
            torch.set_num_threads(THREADS)
        t0 = time.time()
        pipe = AutoPipelineForText2Image.from_pretrained(
            SD_MODEL, torch_dtype=torch.float32,
            safety_checker=None, requires_safety_checker=False)
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(
            pipe.scheduler.config, use_karras_sigmas=True,
            algorithm_type="dpmsolver++", final_sigmas_type="sigma_min")
        pipe.set_progress_bar_config(disable=True)
        self.load_sec = time.time() - t0
        self.pipe = pipe
        self.loaded = True
        log("sd15 加载完成 %.1fs" % self.load_sec)
        return pipe

    def run(self, params, on_step):
        import torch

        pipe = self._load()
        self.last_used = time.time()
        seed = int(params.get("seed") or random.randint(0, 2**31 - 1))
        g = torch.Generator("cpu").manual_seed(seed)

        def cb(_pipe, step, _ts, kw):
            on_step(step + 1, params["steps"])
            return {}  # diffusers 会对返回值做 .pop()，必须给 dict

        img = pipe(
            prompt=params["prompt"], negative_prompt=params.get("negative_prompt") or None,
            num_inference_steps=params["steps"], guidance_scale=params["cfg"],
            width=params["width"], height=params["height"], generator=g,
            callback_on_step_end=cb, callback_on_step_end_tensor_inputs=[],
        ).images[0]
        path = os.path.join(OUT_DIR, "images", "%s.png" % params["job_id"])
        img.save(path)
        self.last_used = time.time()
        return {"output_path": path, "seed": seed, "width": img.width, "height": img.height}


class Wan21Engine(BaseEngine):
    name = "wan21"
    kind = "video"

    def _load(self):
        if self.pipe is not None:
            return self.pipe
        import torch
        from diffusers import WanPipeline

        if THREADS > 0:
            torch.set_num_threads(THREADS)
        dtype = {"bfloat16": torch.bfloat16, "float16": torch.float16,
                 "float32": torch.float32}[WAN_DTYPE]
        t0 = time.time()
        pipe = WanPipeline.from_pretrained(WAN_MODEL, torch_dtype=dtype)
        pipe.set_progress_bar_config(disable=True)
        self.load_sec = time.time() - t0
        self.pipe = pipe
        self.loaded = True
        log("wan21 加载完成 %.1fs dtype=%s" % (self.load_sec, WAN_DTYPE))
        return pipe

    def run(self, params, on_step):
        import torch
        from diffusers.utils import export_to_video

        pipe = self._load()
        self.last_used = time.time()
        seed = int(params.get("seed") or random.randint(0, 2**31 - 1))
        g = torch.Generator("cpu").manual_seed(seed)

        def cb(_pipe, step, _ts, kw):
            on_step(step + 1, params["steps"])
            return {}  # diffusers 会对返回值做 .pop()，必须给 dict

        frames = pipe(
            prompt=params["prompt"], negative_prompt=params.get("negative_prompt") or None,
            height=params["height"], width=params["width"],
            num_frames=params["num_frames"], num_inference_steps=params["steps"],
            guidance_scale=params["cfg"], generator=g,
            callback_on_step_end=cb, callback_on_step_end_tensor_inputs=[],
        ).frames[0]
        path = os.path.join(OUT_DIR, "videos", "%s.mp4" % params["job_id"])
        export_to_video(frames, path, fps=int(params.get("fps") or 16))
        self.last_used = time.time()
        return {"output_path": path, "seed": seed, "num_frames": len(frames),
                "fps": int(params.get("fps") or 16),
                "width": params["width"], "height": params["height"]}


_ENGINES = {"sd15": SD15Engine(), "wan21": Wan21Engine()}


def _engine_for(backend):
    if backend == "animatediff":
        raise ValueError("animatediff 渠道暂未加入 media-gen（请用 media-engine 的肖像微动接口）")
    if backend == "minimax_h3":
        raise ValueError("minimax_h3 渠道不可用：本机无可用权重（详见 /v1/backends）")
    eng = _ENGINES.get(backend)
    if eng is None:
        raise ValueError("未知 backend：%s（可选 %s）" % (backend, list(_ENGINES)))
    if backend == "wan21":
        ok, why = _wan_ready()
        if not ok:
            raise ValueError("wan21 渠道暂不可用：%s" % why)
    return eng

# ---------------------------------------------------------------- worker
def _worker():
    while True:
        job_id = _Q.get()
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None or job["status"] != "queued":
                _Q.task_done()
                continue
            job["status"] = "running"
            job["started_at"] = time.time()
            params = dict(job["params"], job_id=job_id)
            job["progress"] = {"step": 0, "total": params.get("steps") or 0}
            _persist()
        _TTY[job["kind"]] += 1
        try:
            eng = _engine_for(job["backend"])

            def on_step(step, total):
                with _JOBS_LOCK:
                    _JOBS[job_id]["progress"] = {"step": step, "total": total}

            result = eng.run(params, on_step)
            with _JOBS_LOCK:
                job["status"] = "succeeded"
                job["finished_at"] = time.time()
                job["elapsed_sec"] = round(job["finished_at"] - job["started_at"], 1)
                job["output_path"] = result.pop("output_path")
                job["result"] = result
                _persist()
        except Exception as e:  # noqa: BLE001 - 任务失败要落到任务体里给调用方看
            import traceback

            traceback.print_exc()
            _FAILS[job["kind"]] += 1
            with _JOBS_LOCK:
                job["status"] = "failed"
                job["finished_at"] = time.time()
                job["elapsed_sec"] = round(job["finished_at"] - job["started_at"], 1)
                job["error"] = "%s: %s" % (type(e).__name__, e)
                _persist()
        finally:
            _Q.task_done()


def _idle_loop():
    while True:
        time.sleep(60)
        if IDLE_UNLOAD <= 0:
            continue
        for eng in _ENGINES.values():
            if eng.loaded and time.time() - eng.last_used > IDLE_UNLOAD and _Q.empty():
                eng.unload()

# ---------------------------------------------------------------- 接口
class ImageJob(BaseModel):
    prompt: str
    negative_prompt: str | None = None
    width: int = 640
    height: int = 832
    steps: int = 20
    cfg: float = 6.0
    seed: int | None = None
    backend: str = "sd15"
    wait: bool = False
    wait_sec: float = 30.0


class VideoJob(BaseModel):
    prompt: str
    negative_prompt: str | None = None
    width: int = 832
    height: int = 480
    num_frames: int = 17
    steps: int = 20
    cfg: float = 5.0
    fps: int = 16
    seed: int | None = None
    backend: str = "wan21"
    wait: bool = False
    wait_sec: float = 30.0


def _submit(kind, params):
    _engine_for(params["backend"])  # 渠道不可用/参数外的硬错误直接同步拒绝，不进队列
    if kind == "image":
        w, h = int(params["width"]), int(params["height"])
        if not (256 <= w <= 1024 and 256 <= h <= 1024 and w % 8 == 0 and h % 8 == 0):
            raise ValueError("宽高需 256-1024 且为 8 的倍数")
        params["steps"] = max(1, min(int(params["steps"]), 50))
    else:
        w, h = int(params["width"]), int(params["height"])
        if not (256 <= w <= 1280 and 256 <= h <= 1280 and w % 16 == 0 and h % 16 == 0):
            raise ValueError("宽高需 256-1280 且为 16 的倍数")
        nf = int(params["num_frames"])
        if not (1 <= nf <= 81):
            raise ValueError("num_frames 需 1-81（Wan 原生 81 帧 @16fps）")
        if (nf - 1) % 4 != 0:
            raise ValueError("num_frames 必须是 4k+1（VAE 时间压缩 4 倍）：5/9/17/33/49/81")
        params["num_frames"] = nf
        params["steps"] = max(1, min(int(params["steps"]), 50))
    jid = ("img_" if kind == "image" else "vid_") + uuid.uuid4().hex[:10]
    job = {"id": jid, "kind": kind, "backend": params["backend"], "status": "queued",
           "created_at": time.time(), "params": params, "progress": {"step": 0, "total": params["steps"]}}
    with _JOBS_LOCK:
        _JOBS[jid] = job
        _persist()
    _Q.put(jid)
    return jid


def _wait(job_id, wait_sec):
    end = time.time() + max(0.0, min(float(wait_sec or 0), 3600.0))
    while True:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return None
            view = _job_view(job)
        if view["status"] in ("succeeded", "failed", "interrupted") or time.time() >= end:
            return view
        time.sleep(2)


def _submit_and_maybe_wait(kind, body):
    params = body.model_dump()
    wait, wait_sec = params.pop("wait", False), params.pop("wait_sec", 30.0)
    params.pop("backend_wait", None)
    jid = _submit(kind, params)
    if wait:
        return _wait(jid, wait_sec)
    with _JOBS_LOCK:
        return _job_view(_JOBS[jid])


@app.get("/")
def root():
    return {"service": SERVICE, "version": VERSION,
            "uptime_sec": round(time.time() - STARTED_AT, 1)}


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE, "version": VERSION,
            "uptime_sec": round(time.time() - STARTED_AT, 1),
            "threads": THREADS or os.cpu_count(),
            "queue_depth": _Q.qsize(),
            "engines": {k: {"loaded": e.loaded, "load_sec": round(e.load_sec, 1)}
                        for k, e in _ENGINES.items()},
            "backends": backends_view()}


@app.get("/v1/backends")
def backends(request: Request):
    if not _authorized(request):
        return _forbidden()
    return {"backends": backends_view()}


@app.post("/v1/image/jobs")
def image_job(body: ImageJob, request: Request):
    if not _authorized(request):
        return _forbidden()
    try:
        return _submit_and_maybe_wait("image", body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/v1/video/jobs")
def video_job(body: VideoJob, request: Request):
    if not _authorized(request):
        return _forbidden()
    try:
        return _submit_and_maybe_wait("video", body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/v1/jobs")
def list_jobs(request: Request, limit: int = 20, kind: str | None = None):
    if not _authorized(request):
        return _forbidden()
    with _JOBS_LOCK:
        items = sorted(_JOBS.values(), key=lambda j: j["created_at"], reverse=True)
    if kind:
        items = [j for j in items if j["kind"] == kind]
    return {"jobs": [_job_view(j) for j in items[:max(1, min(limit, 200))]]}


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, request: Request, wait_sec: float = 0, wait: bool = False):
    if not _authorized(request):
        return _forbidden()
    view = _wait(job_id, wait_sec if wait or wait_sec else 0)
    if view is None:
        return JSONResponse({"error": "任务不存在：%s" % job_id}, status_code=404)
    return view


@app.get("/files/{name}")
def files(name: str):
    # 只允许取 output 目录下的直接子文件，防目录穿越
    safe = os.path.basename(name)
    for sub in ("images", "videos"):
        p = os.path.join(OUT_DIR, sub, safe)
        if os.path.isfile(p):
            media = "image/png" if p.endswith(".png") else "video/mp4"
            return FileResponse(p, media_type=media)
    return JSONResponse({"error": "文件不存在：%s" % safe}, status_code=404)


@app.get("/metrics")
def metrics():
    with _JOBS_LOCK:
        total = {"image": 0, "video": 0}
        for j in _JOBS.values():
            total[j["kind"]] += 1
    lines = ["# TYPE media_gen_jobs_total counter",
             'media_gen_jobs_total{kind="image"} %d' % total["image"],
             'media_gen_jobs_total{kind="video"} %d' % total["video"],
             "# TYPE media_gen_failures_total counter",
             'media_gen_failures_total{kind="image"} %d' % _FAILS["image"],
             'media_gen_failures_total{kind="video"} %d' % _FAILS["video"],
             "# TYPE media_gen_queue_depth gauge",
             "media_gen_queue_depth %d" % _Q.qsize()]
    return PlainTextResponse("\n".join(lines) + "\n")


def main():
    if THREADS > 0:
        os.environ.setdefault("OMP_NUM_THREADS", str(THREADS))
    _load_jobs()
    threading.Thread(target=_worker, name="gen-worker", daemon=True).start()
    threading.Thread(target=_idle_loop, name="idle-unloader", daemon=True).start()
    log("监听 %s:%d，输出 %s" % (HOST, PORT, OUT_DIR))
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
