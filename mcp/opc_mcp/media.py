"""媒体能力客户端：TTS（media-engine :9311）与文生图（media-gen :9103）。"""

import json
import shutil
import time
import urllib.error
import urllib.request
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


def tts(text, out_dir, voice=None, engine="cosyvoice", speed=1.0, wait_sec=120, stem=None):
    text = (text or "").strip()
    if not text:
        raise MediaError("text 不能为空")
    stem = stem or ("tts_%d" % int(time.time()))
    body = {"text": text, "format": "json", "engine": engine, "mode": "async",
            "speed": speed, "stem": stem}
    if voice:
        body["voice"] = voice
        body["speaker"] = voice
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
    return {"path": path, "engine": resp.get("engine"), "duration": resp.get("duration"),
            "sample_rate": resp.get("sample_rate"), "segments": resp.get("segment_count"),
            "srt_url": resp.get("srt_url")}


def image(prompt, out_dir, width=640, height=832, steps=20, negative=None,
          seed=None, wait_sec=300, stem=None):
    prompt = (prompt or "").strip()
    if not prompt:
        raise MediaError("prompt 不能为空")
    stem = stem or ("img_%d" % int(time.time()))
    body = {"prompt": prompt, "width": int(width), "height": int(height),
            "steps": int(steps), "backend": "sd15", "wait": True,
            "wait_sec": float(min(max(wait_sec, 5), 900))}
    if negative:
        body["negative_prompt"] = negative
    if seed is not None:
        body["seed"] = int(seed)
    _, resp = _call(config.IMAGE_URL + "/v1/image/jobs", body, config.IMAGE_TOKEN,
                    timeout=float(body["wait_sec"]) + 60)
    if resp.get("status") != "succeeded":
        raise MediaError("图像作业 %s：%s" % (resp.get("status"), resp.get("error") or resp))
    path = _collect(resp, Path(out_dir), stem, config.IMAGE_TOKEN, config.IMAGE_URL)
    if not path:
        raise MediaError("图像作业无产物：%s" % str(resp)[:300])
    res = resp.get("result") or {}
    params = resp.get("params") or {}
    return {"path": path, "width": res.get("width") or params.get("width"),
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
