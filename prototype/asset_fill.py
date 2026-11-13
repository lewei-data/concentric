#!/usr/bin/env python3
"""文案岗补素材链路的底层能力：素材库检索 → ComfyUI 生成 → MCP-TTS 音频。

设计约定：
  - 素材库（materials.json）优先：命中即复用，不重复生成。
  - 未命中走 ComfyUI 文生图（通道与端点读 comfyui-ops/config.json，默认 21.157:8188）。
  - 台词 / 对白 / 旁白音频复用 om_render 的 MCP-TTS 常驻客户端，不再起第二个进程。
  - 所有函数只返回结果或抛异常，不写业务状态；调用方（server.py）负责落库与消息。
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMFY_CFG = os.environ.get(
    "OPC_COMFY_CFG",
    os.path.join(os.path.dirname(BASE_DIR), "comfyui-ops", "config.json"))
GEN_TIMEOUT = int(os.environ.get("OPC_COMFY_TIMEOUT", "240"))
POLL_INTERVAL = 2.0


def _http(url, timeout=8, data=None):
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _comfy_image_url():
    """从 comfyui-ops/config.json 取图片通道端点，探测可用性。"""
    with open(COMFY_CFG, encoding="utf-8") as f:
        cfg = json.load(f)
    url = str(((cfg.get("endpoints") or {}).get("image") or {}).get("url")
              or "").rstrip("/")
    if not url:
        raise RuntimeError("comfyui-ops/config.json 缺少 image 端点")
    _http(url + "/system_stats", timeout=5)          # 探测：不可用直接抛
    return url


def _pick_checkpoint(url):
    """从 /object_info 找一个可用底模；找不到就明确报错，不猜文件名。"""
    info = json.loads(_http(url + "/object_info", timeout=15).decode("utf-8"))
    ck = info.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {})
    names = next((v for k, v in ck.items() if isinstance(v, list) and v
                  and isinstance(v[0], list)), [])
    if not names:
        raise RuntimeError("ComfyUI 未发现可用 checkpoint")
    for n in names:
        if re.search(r"sd15|sd_xl|sdxl|realistic|qwen", n, re.I):
            return n
    return names[0]


def gen_image(prompt, out_path, width=768, height=768, timeout=GEN_TIMEOUT):
    """ComfyUI 文生图：提交 /prompt → 轮询 /history → /view 取回成品落盘。

    成功返回 out_path；失败抛 RuntimeError（调用方记入素材缺口结论）。
    """
    url = _comfy_image_url()
    ckpt = _pick_checkpoint(url)
    # 统一使用运维监控 WS 监听的 clientId：ComfyUI 的 progress/executing 事件与
    # 执行中预览帧（阶段性产物）定向推送给提交任务的 clientId，这样才能被监控看到
    port = urllib.parse.urlsplit(url).port or 8188
    client_id = f"opc-ops-{port}"
    graph = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "低质量, 模糊, 水印, 变形", "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": int(time.time() * 1000) % (1 << 31),
                         "steps": 20, "cfg": 6.5, "sampler_name": "euler",
                         "scheduler": "normal", "denoise": 1.0,
                         "model": ["1", 0], "positive": ["2", 0],
                         "negative": ["3", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"images": ["6", 0],
                         "filename_prefix": "opc-material"}},
    }
    body = json.dumps({"prompt": graph, "client_id": client_id},
                      ensure_ascii=False).encode("utf-8")
    resp = json.loads(_http(url + "/prompt", timeout=15,
                            data=body).decode("utf-8"))
    pid = resp.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI 拒绝作业：{resp}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        hist = json.loads(_http(f"{url}/history/{pid}",
                                timeout=15).decode("utf-8"))
        entry = hist.get(pid) or {}
        if entry.get("status", {}).get("status_str") == "error":
            raise RuntimeError("ComfyUI 作业执行失败")
        outs = (entry.get("outputs") or {}).get("7") or {}
        imgs = outs.get("images") or []
        if imgs:
            q = urllib.parse.urlencode({
                "filename": imgs[0]["filename"],
                "subfolder": imgs[0].get("subfolder", ""),
                "type": imgs[0].get("type", "output")})
            blob = _http(f"{url}/view?{q}", timeout=60)
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(blob)
            return out_path
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"ComfyUI 出图超时（{timeout}s）")


def search_library(materials_file, keywords, limit=5):
    """素材库检索：keywords 与 title/desc/category/meta 做包含匹配，按命中数排序。"""
    with open(materials_file, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    kws = [str(k).strip().lower() for k in keywords
           if len(str(k).strip()) >= 2]
    scored = []
    for it in items:
        if not isinstance(it, dict) or not it.get("url"):
            continue
        hay = " ".join(str(it.get(k) or "") for k in
                       ("title", "desc", "category", "meta")).lower()
        hits = sorted({k for k in kws if k in hay}, key=len, reverse=True)
        if hits:
            scored.append({"id": str(it.get("id") or ""),
                           "title": str(it.get("title") or "未命名素材"),
                           "category": str(it.get("category") or "其他"),
                           "url": str(it["url"]),
                           "meta": str(it.get("meta") or ""),
                           "matched": hits})
    scored.sort(key=lambda x: len(x["matched"]), reverse=True)
    return scored[:max(1, limit)]


def gen_tts(text, out_dir, stem, speed=None):
    """台词/对白/旁白 → MCP-TTS 音频（复用 om_render 常驻客户端，线程安全）。"""
    import om_render as _OMR          # 与 server.py 同目录，按需加载
    payload = _OMR._tts().synthesize(text, out_dir, stem, speed=speed)
    audio = payload.get("audio_path")
    if not audio or not os.path.isfile(audio):
        raise RuntimeError("TTS 未产出音频")
    return audio
