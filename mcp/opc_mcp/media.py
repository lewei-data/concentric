"""媒体能力客户端：TTS（media-engine :9200/:9311）、图像（media-gen :9103 / ComfyUI :8188）。

鉴权说明：这里的 token 全部来自服务端 config（MEDIA_ENGINE_TOKEN / MEDIA_GEN_TOKEN），
是服务间调用的固定密钥；外部 MCP client 只使用 opc_init 的 client_key，
不传也不需要知道任何媒体服务密钥。
"""

import base64
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import config


class MediaError(RuntimeError):
    pass


def _call(url, body, token, timeout):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise MediaError("%s -> HTTP %s：%s" % (url, e.code, detail)) from e
    except Exception as e:
        raise MediaError("%s 不可达：%s" % (url, e)) from e


def _fetch(url, token, timeout=120):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _collect(result, out_dir, stem, token, base_url):
    """把产物落到调用方目录：优先本地路径复制，其次按 URL 下载。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    src = result.get("audio_path") or result.get("output_path")
    if src and Path(src).exists():
        dst = out_dir / (stem + Path(src).suffix)
        shutil.copy2(src, dst)
        return str(dst)
    url = result.get("audio_url") or result.get("output_url")
    if url:
        ext = Path(url).suffix or ".bin"
        dst = out_dir / (stem + ext)
        dst.write_bytes(_fetch(base_url + url, token))
        return str(dst)
    return ""


def tts(text, out_dir, voice=None, engine="cosyvoice", speed=1.0, wait_sec=120, stem=None,
        notes=None, language=None, channel=None, brief=None):
    """合成音频。text=口播稿，notes=注意事项（Qwen3 走 instruct），brief=需求描述（仅记录）。"""
    text = (text or "").strip()
    if not text:
        raise MediaError("text 不能为空")
    if channel in ("tts", "qwen3"):
        engine = "qwen3"
    elif channel == "cosyvoice":
        engine = "cosyvoice"
    stem = stem or ("tts_%d" % int(time.time()))
    body = {"text": text, "format": "json", "engine": engine, "mode": "async",
            "speed": speed, "stem": stem}
    if voice:
        body["voice"] = voice
        body["speaker"] = voice
    if notes:
        body["instruct"] = str(notes)[:500]
    if language:
        body["language"] = language
    status, resp = _call(config.TTS_URL + "/v1/tts", body, config.TTS_TOKEN,
                         timeout=max(30, wait_sec))
    if status == 202 or resp.get("poll_url"):
        deadline = time.time() + max(5, wait_sec)
        poll = resp.get("poll_url") or "/v1/audio/jobs/" + str(resp.get("job_id"))
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                with urllib.request.urlopen(config.TTS_URL + poll, timeout=30) as r:
                    job = json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:
                raise MediaError("轮询 TTS 作业失败：%s" % e) from e
            if job.get("status") in ("succeeded", "failed", "interrupted"):
                if job.get("status") != "succeeded":
                    raise MediaError("TTS 作业 %s：%s" % (job.get("status"), job.get("error")))
                resp = job.get("result") or {}
                break
        else:
            raise MediaError("TTS 超时（%ss 未完成），job_id=%s" % (wait_sec, resp.get("job_id")))
    path = _collect(resp, Path(out_dir), stem, config.TTS_TOKEN, config.TTS_URL)
    if not path:
        raise MediaError("TTS 未返回音频：%s" % str(resp)[:300])
    return {"path": path, "engine": resp.get("engine"), "channel": channel or engine,
            "voice": voice or "", "brief": brief or "", "notes": notes or "",
            "duration": resp.get("duration"), "sample_rate": resp.get("sample_rate"),
            "segments": resp.get("segment_count"), "srt_url": resp.get("srt_url")}


COMFY_IMAGE_URL = os.environ.get("OPC_COMFY_IMAGE_URL", "http://192.168.21.157:8188").rstrip("/")


def _json_get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _json_post(url, body, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _comfy_upload_image(path):
    boundary = "----b" + uuid.uuid4().hex
    name = Path(path).name
    body = b""
    for k, v in (("overwrite", "true"), ("type", "input")):
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
             f"Content-Type: application/octet-stream\r\n\r\n").encode()
    with open(path, "rb") as f:
        body += f.read()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(COMFY_IMAGE_URL + "/upload/image", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8", "replace")).get("name", name)


def _comfy_prepare_reference(reference):
    """参照图 → ComfyUI input 文件名；支持本地绝对路径与 data:image/...;base64。"""
    if not reference:
        return None
    ref = str(reference).strip()
    if ref.startswith("data:image/"):
        head, _, b64 = ref.partition(",")
        if "base64" not in head:
            raise MediaError("参照图 data URI 必须是 base64")
        ext = ".png" if "png" in head else (".jpg" if "jpg" in head or "jpeg" in head else ".webp")
        tmp = Path(config.DATA_DIR) / ("ref_%s%s" % (uuid.uuid4().hex[:8], ext))
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(base64.b64decode(b64))
        return _comfy_upload_image(tmp)
    p = Path(ref)
    if not p.is_file():
        raise MediaError("参照图不存在：%s" % ref)
    if p.stat().st_size > 20 * 1024 * 1024:
        raise MediaError("参照图超过 20MB 限制")
    return _comfy_upload_image(p)


def _comfy_graph(prompt, prefix, seed, ref_name=None, resolution=768):
    """Qwen-Image 2.1 Turbo graph（来自生产验证的 tools/staff_photos.py）。"""
    text_node = {"clip": ["2", 0], "prompt": prompt, "negative_prompt": "",
                 "vae": ["3", 0], "resolution": int(resolution)}
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "qwen_image_2.1_turbo_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "cpu"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "4": {"class_type": "QwenImage21Cache", "inputs": {
            "model": ["1", 0], "device": "cpu", "dtype": "default"}},
    }
    if ref_name:
        graph["13"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
        text_node["images"] = {"image_1": ["13", 0]}
    graph["5"] = {"class_type": "TextEncodeQwenImage21", "inputs": text_node}
    graph.update({
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "7": {"class_type": "CFGGuider", "inputs": {"model": ["4", 0], "positive": ["5", 0],
                                                     "negative": ["5", 1], "cfg": 1.0}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "ManualSigmas", "inputs": {
            "sigmas": "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["6", 0], "guider": ["7", 0], "sampler": ["8", 0],
            "sigmas": ["9", 0], "latent_image": ["5", 2]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "SaveImage", "inputs": {"images": ["11", 0], "filename_prefix": prefix}},
    })
    return graph


def _comfy_generate(prompt, out_dir, stem, reference=None, wait_sec=900, seed=None):
    t0 = time.time()
    ref_name = _comfy_prepare_reference(reference)
    try:
        submitted = _json_post(COMFY_IMAGE_URL + "/prompt",
                               {"prompt": _comfy_graph(prompt, stem, seed or int(t0), ref_name),
                                "client_id": "opc-mcp"}, timeout=60)
        pid = submitted.get("prompt_id")
        if not pid:
            raise MediaError("ComfyUI 未返回 prompt_id：%s" % str(submitted)[:300])
    except urllib.error.HTTPError as e:
        raise MediaError("ComfyUI 提交失败：%s" % e.read().decode("utf-8", "replace")[:600]) from e
    limit = max(30, min(float(wait_sec), 1800))
    deadline = t0 + limit
    while time.time() < deadline:
        history = _json_get(COMFY_IMAGE_URL + "/history/" + pid, timeout=30)
        if pid in history:
            for outputs in (history[pid].get("outputs") or {}).values():
                for im in (outputs.get("images") or []):
                    q = urllib.parse.urlencode({"filename": im["filename"],
                                                "subfolder": im.get("subfolder", ""),
                                                "type": im.get("type", "output")})
                    dst = Path(out_dir) / (stem + Path(im["filename"]).suffix)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    with urllib.request.urlopen(COMFY_IMAGE_URL + "/view?" + q, timeout=120) as r:
                        dst.write_bytes(r.read())
                    return {"path": str(dst), "channel": "qwen-image", "job_id": pid,
                            "mode": "i2i" if ref_name else "t2i", "reference": bool(ref_name),
                            "elapsed_sec": int(time.time() - t0)}
            raise MediaError("ComfyUI 作业无图片产物：%s" % str(history[pid])[:300])
        time.sleep(5)
    raise MediaError("ComfyUI 出图超时（%ss），prompt_id=%s" % (int(limit), pid))


def image(prompt, out_dir, width=640, height=832, steps=20, negative=None,
          seed=None, wait_sec=300, stem=None, channel="sd15", reference_image=None, brief=None):
    prompt = (prompt or "").strip()
    if not prompt:
        raise MediaError("prompt 不能为空")
    stem = stem or ("img_%d" % int(time.time()))
    if channel == "qwen-image" or reference_image:
        return _comfy_generate(prompt, out_dir, stem, reference_image,
                               wait_sec=max(wait_sec, 600), seed=seed)
    body = {"prompt": prompt, "width": int(width), "height": int(height),
            "steps": int(steps), "backend": "sd15", "wait": True,
            "wait_sec": float(min(max(wait_sec, 5), 900))}
    if negative:
        body["negative_prompt"] = negative
    if seed is not None:
        body["seed"] = int(seed)
    _, resp = _call(config.IMAGE_URL + "/v1/image/jobs", body, config.IMAGE_TOKEN,
                    timeout=float(body["wait_sec"]) + 60)
    # 服务端 wait_sec 有自己的上限，慢任务会直接回 running；必须按 job_id 轮询到终态
    if resp.get("status") in ("pending", "queued", "running"):
        job_id = str(resp.get("id") or "")
        deadline = time.time() + max(5, wait_sec)
        while job_id and time.time() < deadline:
            time.sleep(2.0)
            try:
                req = urllib.request.Request(config.IMAGE_URL + "/v1/jobs/" + job_id)
                if config.IMAGE_TOKEN:
                    req.add_header("Authorization", "Bearer " + config.IMAGE_TOKEN)
                with urllib.request.urlopen(req, timeout=30) as r:
                    resp = json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:
                raise MediaError("轮询图像作业失败：%s" % e) from e
            if resp.get("status") not in ("pending", "queued", "running"):
                break
    if resp.get("status") != "succeeded":
        raise MediaError("图像作业 %s：%s" % (resp.get("status"), resp.get("error") or resp))
    path = _collect(resp, Path(out_dir), stem, config.IMAGE_TOKEN, config.IMAGE_URL)
    if not path:
        raise MediaError("图像作业无产物：%s" % str(resp)[:300])
    res = resp.get("result") or {}
    params = resp.get("params") or {}
    return {"path": path, "channel": "sd15", "mode": "t2i", "reference": False,
            "brief": brief or "",
            "width": res.get("width") or params.get("width"),
            "height": res.get("height") or params.get("height"),
            "seed": res.get("seed", params.get("seed")),
            "job_id": resp.get("id"), "elapsed_sec": resp.get("elapsed_sec"),
            "output_url": resp.get("output_url")}


def health():
    out = {}
    for name, base, token in (("tts", config.TTS_URL, config.TTS_TOKEN),
                              ("image", config.IMAGE_URL, config.IMAGE_TOKEN)):
        req = urllib.request.Request(base + "/health")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                out[name] = {"ok": True, "status": r.status}
        except Exception as e:
            out[name] = {"ok": False, "error": str(e)[:200]}
    return out


def capabilities():
    """聚合生成能力：渠道、音色、支持模式、限制；只读探测，不烧 token。"""
    caps = {
        "audio": {"channels": [
            {"name": "cosyvoice", "engine": "cosyvoice", "service": "media-engine:9200",
             "supports": ["text_to_speech"], "voice_param": "voice",
             "notes": "默认渠道；输出 24kHz wav/mp3；长文本自动分句；音色见 voices 字段",
             "limits": {"concurrency": "串行队列"}},
            {"name": "tts", "engine": "qwen3", "service": "qwen-tts:9311 (via media-engine)",
             "supports": ["text_to_speech", "instruct"], "voice_param": "speaker",
             "notes": "notes 作为 instruct 下发；language 默认 Chinese；CPU RTF 24~38，仅离线",
             "limits": {"max_text_chars": 200, "concurrency": 1}},
        ], "voices": {}},
        "image": {"channels": [
            {"name": "sd15", "service": "media-gen:9103", "model": "Realistic Vision V5.1",
             "supports": ["text_to_image"], "reference_image": False,
             "limits": {"size": "256~1024 且 8 的倍数", "steps": "1~50", "default": "640x832"}},
            {"name": "qwen-image", "service": "ComfyUI:8188", "model": "Qwen-Image 2.1 Turbo",
             "supports": ["text_to_image", "image_to_image"], "reference_image": True,
             "reference_formats": ["本地绝对路径", "data:image/...;base64"],
             "limits": {"resolution": "512~1536 且 64 对齐（i2i 跟随输入图）",
                        "reference_min_short_side": 384, "reference_max_mb": 20,
                        "steps": 8, "timeout_sec": 900,
                        "note": "无 GPU，CPU 单张约 1~3 分钟"}},
        ]},
        "video": {"mcp_tools": [], "channels": [
            {"name": "wan21", "service": "media-gen:9103", "supports": ["text_to_video"],
             "limits": {"frames": "1~81 且 4k+1", "size": "256~1280 且 16 倍数",
                        "note": "CPU 3s 480p 约 10~30 分钟；MCP 当前未暴露 video 工具"}},
            {"name": "wan22_ti2v", "service": "ComfyUI:8189",
             "supports": ["text_to_video", "image_to_video"],
             "note": "MCP 当前未暴露 video 工具"},
        ]},
        "auth": "生成类工具与 opc_init 共用 client_key；媒体服务密钥为服务端配置，客户端不传",
    }
    caps["reachability"] = {}
    for name, base, token, probe in (
            ("tts", config.TTS_URL, config.TTS_TOKEN, "/health"),
            ("image", config.IMAGE_URL, config.IMAGE_TOKEN, "/health"),
            ("comfy_image", COMFY_IMAGE_URL, "", "/system_stats")):
        try:
            req = urllib.request.Request(base + probe)
            if token:
                req.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(req, timeout=5) as r:
                caps["reachability"][name] = {"ok": True, "status": r.status}
        except Exception as e:
            caps["reachability"][name] = {"ok": False, "error": str(e)[:160]}
    for key, url, token in (("audio_voices", config.TTS_URL + "/v1/voices", config.TTS_TOKEN),
                            ("image_backends", config.IMAGE_URL + "/v1/backends", config.IMAGE_TOKEN)):
        try:
            req = urllib.request.Request(url)
            if token:
                req.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            data = {"error": str(e)[:160]}
        if key == "audio_voices":
            caps["audio"]["voices"] = data
        else:
            caps["image"]["backends"] = data
    return caps
