#!/usr/bin/env python3
"""MCP-RENDER：独立视频渲染 MCP server（stdio 传输）。

把本机 Remotion / OpenMontage 出片能力开放给外部 agent：
  render_capabilities —— 规格、场景类型、后端状态
  render_submit       —— 提交异步渲染作业（图片 + 旁白 → MP4）
  render_status       —— 查作业状态 / 进度 / 失败原因
  render_file_link    —— 用 file_key 重取下载链接

底层复用 prototype/remotion_image_reel.produce()（scroll_image_scene + MCP-TTS 旁白 +
OmRender.validate 验收门），不重造渲染逻辑。产物写独立 vault，不污染生产交付物目录。
stdout 只承载 JSON-RPC；import 时 prototype 会打 boot 日志，故先重定向到 stderr。
"""

import base64
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
import urllib.request
from queue import Queue
from pathlib import Path

_PROTO_OUT = sys.stdout
sys.stdout = sys.stderr

SERVER_NAME = "mcp-render"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"

ROOT = "/opt/sefs/opc-workbench"
RENDER_ROOT = Path(os.environ.get("OPC_RENDER_ROOT") or os.path.join(ROOT, "mcp-render"))
# 必须在 import deliverables / om_render 之前设置，隔离产物路径
os.environ["OPC_VAULT"] = str(RENDER_ROOT / "vault")
OUT_DIR = RENDER_ROOT / "output"
INPUT_DIR = RENDER_ROOT / "inputs"
PUBLIC_BASE = os.environ.get("OPC_RENDER_PUBLIC_BASE",
                             "https://ai.boraydata.com/conring/render").rstrip("/")
INDEX = OUT_DIR / "index.json"
MAX_IMAGES = int(os.environ.get("OPC_RENDER_MAX_IMAGES", "30"))
MAX_IMAGE_MB = int(os.environ.get("OPC_RENDER_MAX_IMAGE_MB", "20"))
MAX_TOTAL_SEC = float(os.environ.get("OPC_RENDER_MAX_SEC", "300"))

sys.path.insert(0, os.path.join(ROOT, "mcp"))
sys.path.insert(0, os.path.join(ROOT, "prototype"))

from opc_mcp import auth, config  # noqa: E402
from opc_mcp.store import Store  # noqa: E402
import deliverables as DL  # noqa: E402
import om_render as OMR  # noqa: E402
import remotion_image_reel as REEL  # noqa: E402

_STORE = None
_STORE_LOCK = threading.Lock()
_INDEX_LOCK = threading.Lock()
_QUEUE = Queue()


def log(msg):
    sys.stderr.write("[mcp-render] %s\n" % msg)
    sys.stderr.flush()


def _store():
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = Store(config.DB_PATH)
    return _STORE


def _load_index():
    try:
        with open(INDEX, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(data):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, INDEX)


def _patch(job_id, **fields):
    with _INDEX_LOCK:
        index = _load_index()
        rec = index.get(job_id)
        if not rec:
            return
        rec.update(fields)
        rec["updated_at"] = int(time.time())
        _save_index(index)


def _auth(args):
    user = auth.resolve(_store(), args)
    return auth.fingerprint(user["phone"])


def _save_image(src, work, idx):
    """data URI / http(s) URL / 服务端本地路径 → work 下的 png/jpg 文件。"""
    src = str(src or "").strip()
    if not src:
        raise ValueError("第 %d 张图片为空" % (idx + 1))
    suffix = ".png"
    if src.startswith("data:image/"):
        m = re.match(r"^data:image/([a-zA-Z0-9+.-]+);base64,(.*)$", src, re.S)
        if not m:
            raise ValueError("第 %d 张图片不是合法 data URI" % (idx + 1))
        ext = m.group(1).lower().replace("jpeg", "jpg")
        suffix = "." + ext if ext in ("png", "jpg", "webp") else ".png"
        raw = base64.b64decode(m.group(2))
        if len(raw) > MAX_IMAGE_MB * 1024 * 1024:
            raise ValueError("第 %d 张图片超过 %dMB" % (idx + 1, MAX_IMAGE_MB))
    elif src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=120) as r:
            raw = r.read(MAX_IMAGE_MB * 1024 * 1024 + 1)
        if len(raw) > MAX_IMAGE_MB * 1024 * 1024:
            raise ValueError("第 %d 张图片超过 %dMB" % (idx + 1, MAX_IMAGE_MB))
    else:
        p = Path(src)
        if not p.is_file():
            raise ValueError("第 %d 张图片不存在：%s" % (idx + 1, src))
        raw = p.read_bytes()
        suffix = p.suffix.lower() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") else ".png"
        if len(raw) > MAX_IMAGE_MB * 1024 * 1024:
            raise ValueError("第 %d 张图片超过 %dMB" % (idx + 1, MAX_IMAGE_MB))
    dst = work / ("src%02d%s" % (idx + 1, suffix))
    dst.write_bytes(raw)
    return str(dst)


def _worker():
    while True:
        job_id = _QUEUE.get()
        try:
            with _INDEX_LOCK:
                rec = _load_index().get(job_id) or {}
            if rec.get("status") != "queued":
                continue
            _patch(job_id, status="running", started_at=int(time.time()))
            t0 = time.time()
            work = INPUT_DIR / job_id
            work.mkdir(parents=True, exist_ok=True)
            shots = rec["shots"]
            images = [_save_image(s["image"], work, i) for i, s in enumerate(shots)]
            storyboard = [{"line": s.get("line") or "", "duration": float(s.get("duration") or 6)}
                          for s in shots]
            total = sum(x["duration"] for x in storyboard)
            if total > MAX_TOTAL_SEC:
                raise ValueError("总时长 %.1fs 超过上限 %.0fs" % (total, MAX_TOTAL_SEC))
            item = {"traceId": "mcp-" + job_id, "projectId": "mcp-render",
                    "id": job_id, "storyboard": storyboard, "durationSec": total}
            project = {"color": (rec.get("theme") or {}).get("color") or "#7C3AED"}
            res = REEL.produce(item, project, images, rec.get("spec") or {})
            if not res.get("ok"):
                raise RuntimeError(res.get("reason") or "渲染失败")
            src_video = Path(res["video"])
            if not src_video.is_file():
                raise RuntimeError("渲染器未产出视频文件")
            target_dir = OUT_DIR / job_id
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / "video.mp4"
            shutil.copyfile(src_video, target)
            _patch(job_id, status="succeeded", file_key=job_id,
                   filename="video.mp4", size=target.stat().st_size,
                   duration=res.get("duration"), check=res.get("check"),
                   elapsed_sec=round(time.time() - t0, 1),
                   download_url="%s/assets/%s/video.mp4" % (PUBLIC_BASE, job_id))
            log("作业 %s 完成 %.1fs %d 字节" % (job_id, time.time() - t0, target.stat().st_size))
        except Exception as e:
            log("作业 %s 失败：%s" % (job_id, traceback.format_exc()))
            _patch(job_id, status="failed", error="%s: %s" % (type(e).__name__, e),
                   finished_at=int(time.time()))
        finally:
            _QUEUE.task_done()


threading.Thread(target=_worker, name="render-worker", daemon=True).start()


def render_capabilities(args):
    fp = _auth(args)
    return {
        "service": SERVER_NAME, "identity": fp,
        "engine": "OpenMontage/Remotion Explainer (scroll_image_scene) + MCP-TTS(CosyVoice2)",
        "composition": "1920x1080 默认；按 spec 覆盖为竖屏",
        "default_spec": {"width": 720, "height": 1280, "fps": "30/1"},
        "scene_types": ["scroll_image_scene", "fit_image_scene"],
        "limits": {"max_images": MAX_IMAGES, "max_image_mb": MAX_IMAGE_MB,
                   "max_total_sec": MAX_TOTAL_SEC,
                   "fps": "固定 30（composer 的 calculateMetadata 按 30 计算帧数，传其它帧率会导致时长偏差）",
                   "min_total_sec": 12,
                   "note": "总时长建议 ≥12s：验收门每 3 秒抽一帧，短片不足 4 帧会被判「抽帧失败」。"
                           "无 GPU，Remotion CPU 渲染，单作业数分钟到十余分钟"},
        "auth": "沿用 OPC 个人 API key；产物 file_key 即 job_id，静态下载无需 key",
        "download": "https://ai.boraydata.com/conring/render/assets/<job_id>/video.mp4",
        "related_api": "完整内容流水线（选题→审批→成片，20–45 分钟）见 /workbench/api/video/jobs；"
                       "本服务是直接渲染入口，不做内容与审批。",
    }


def render_submit(args):
    fp = _auth(args)
    shots = args.get("shots") or []
    if not isinstance(shots, list) or not shots:
        raise ValueError("shots 必须是非空数组，每项含 image / line / duration")
    if len(shots) > MAX_IMAGES:
        raise ValueError("图片数超过上限 %d" % MAX_IMAGES)
    norm = []
    for i, s in enumerate(shots):
        if not isinstance(s, dict):
            raise ValueError("第 %d 个 shot 必须是对象" % (i + 1))
        norm.append({"image": str(s.get("image") or ""),
                     "line": str(s.get("line") or ""),
                     "duration": float(s.get("duration") or 6)})
    spec = args.get("spec") or {}
    if not isinstance(spec, dict):
        raise ValueError("spec 必须是对象")
    # composer 的 calculateMetadata 按 30fps 算帧数，其它帧率会导致时长验收失败
    forced_fps = str(spec.get("fps") or "30/1") != "30/1"
    spec = dict(spec)
    spec["fps"] = "30/1"
    job_id = "rj" + secrets.token_hex(8)
    now = int(time.time())
    rec = {"job_id": job_id, "owner": fp, "status": "queued", "shots": norm,
           "spec": spec, "theme": args.get("theme") or {}, "brief": str(args.get("brief") or "")[:500],
           "created_at": now, "updated_at": now}
    with _INDEX_LOCK:
        index = _load_index()
        index[job_id] = rec
        _save_index(index)
    _QUEUE.put(job_id)
    log("作业 %s 入队：%d 镜 / %.1fs" % (job_id, len(norm), sum(s["duration"] for s in norm)))
    note = "异步作业；用 render_status 轮询，完成后取 download_url"
    if forced_fps:
        note += "；fps 已强制为 30（composer 按 30 计算帧数）"
    total = sum(s["duration"] for s in norm)
    if total < 12:
        note += "；总时长 %.1fs < 12s，验收门可能因抽帧不足判失败，建议加长镜头" % total
    return {"job_id": job_id, "status": "queued", "shots": len(norm),
            "poll_after_sec": 10, "note": note}


def render_status(args):
    fp = _auth(args)
    job_id = str(args.get("job_id") or "").strip()
    with _INDEX_LOCK:
        rec = _load_index().get(job_id)
    if not rec:
        raise ValueError("job_id 不存在")
    if rec.get("owner") != fp:
        raise PermissionError("作业不属于当前身份")
    keys = ("job_id", "status", "shots", "duration", "elapsed_sec", "error",
            "download_url", "filename", "size", "created_at", "updated_at", "brief")
    out = {k: rec.get(k) for k in keys if rec.get(k) is not None}
    out["spec"] = rec.get("spec")
    if rec.get("status") == "failed":
        out["hint"] = "可用相同 shots 重新 render_submit；失败原因见 error"
    return out


def render_file_link(args):
    fp = _auth(args)
    job_id = str(args.get("file_key") or str(args.get("job_id") or "")).strip()
    with _INDEX_LOCK:
        rec = _load_index().get(job_id)
    if not rec:
        raise ValueError("file_key 不存在")
    if rec.get("owner") != fp:
        raise PermissionError("产物不属于当前身份")
    if rec.get("status") != "succeeded":
        raise ValueError("作业尚未成功，当前状态：%s" % rec.get("status"))
    path = OUT_DIR / job_id / "video.mp4"
    if not path.is_file():
        raise ValueError("产物文件已不存在")
    return {"file_key": job_id, "filename": "video.mp4", "size": rec.get("size"),
            "duration": rec.get("duration"), "mime": "video/mp4",
            "download_url": "%s/assets/%s/video.mp4" % (PUBLIC_BASE, job_id),
            "download_note": "静态链接无需 key；支持 Range 拖动播放"}


TOOLS = [
    {"name": "render_capabilities",
     "description": "查询本机 Remotion/OpenMontage 出片能力：竖屏规格、场景类型、图片数量/大小/总时长上限、"
                    "鉴权与下载口径。提交前先查。身份沿用 OPC 个人 API key。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "render_submit",
     "description": "提交异步渲染作业：每张图片一句旁白，按数组顺序成镜，输出竖屏 MP4。"
                    "image 支持 data:image/...;base64、http(s) URL 或服务端可见本地路径；"
                    "line 为空则该镜静音。返回 job_id，用 render_status 轮询。不做选题/审批，与 /workbench/api/video/jobs 不同。",
     "inputSchema": {"type": "object", "properties": {
         "shots": {"type": "array", "description": "镜头数组，每项 {image, line, duration(秒)}",
                   "items": {"type": "object", "properties": {
                       "image": {"type": "string"}, "line": {"type": "string"},
                       "duration": {"type": "number", "default": 6}},
                       "required": ["image"]}},
         "spec": {"type": "object", "description": "可选 {width,height,fps}，默认 720x1280@24"},
         "theme": {"type": "object", "description": "可选 {color} 主题主色"},
         "brief": {"type": "string", "description": "可选需求描述"},
     }, "required": ["shots"]}},
    {"name": "render_status",
     "description": "查渲染作业状态：queued/running/succeeded/failed，成功时带 download_url，失败时带 error。",
     "inputSchema": {"type": "object", "properties": {
         "job_id": {"type": "string"}}, "required": ["job_id"]}},
    {"name": "render_file_link",
     "description": "用 job_id（同时是 file_key）重取成片下载链接，仅同 key 可见。",
     "inputSchema": {"type": "object", "properties": {
         "file_key": {"type": "string"}}, "required": ["file_key"]}},
]

_DISPATCH = {
    "render_capabilities": render_capabilities,
    "render_submit": render_submit,
    "render_status": render_status,
    "render_file_link": render_file_link,
}
_OUT_LOCK = threading.Lock()


def _send(obj):
    with _OUT_LOCK:
        _PROTO_OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
        _PROTO_OUT.flush()


def handle(req):
    rid, method = req.get("id"), req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        _send({"jsonrpc": "2.0", "id": rid,
               "result": {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}})
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = params.get("name")
        fn = _DISPATCH.get(name)
        if not fn:
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "未知工具 %s" % name}], "isError": True}})
            return
        try:
            payload = fn(params.get("arguments") or {})
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                "isError": False}})
        except Exception as e:
            log(traceback.format_exc())
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "渲染服务失败：%s" % e}], "isError": True}})
    elif method == "ping":
        _send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif rid is not None:
        _send({"jsonrpc": "2.0", "id": rid,
               "error": {"code": -32601, "message": "未实现的方法 %s" % method}})


def _safe(req):
    try:
        handle(req)
    except Exception:
        log(traceback.format_exc())


def main():
    for d in (OUT_DIR, INPUT_DIR, RENDER_ROOT / "vault"):
        d.mkdir(parents=True, exist_ok=True)
    log("启动 %s %s；产物 %s；公开基址 %s" % (SERVER_NAME, SERVER_VERSION, OUT_DIR, PUBLIC_BASE))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log("收到非 JSON 行，已忽略")
            continue
        for item in (req if isinstance(req, list) else [req]):
            _safe(item)


if __name__ == "__main__":
    main()
