#!/usr/bin/env python3
"""MCP-IMAGE：独立图片生成 MCP server（stdio 传输）。

对外暴露三个工具：
  image_capabilities —— 渠道、模型、规格、后端可达性（只读，不烧额度）
  image_generate     —— 文生图 / 图生图，三渠道：sd15(本机 9103)、
                         qwen-image(157:8188 ComfyUI)、bailian_image(阿里云百炼)
  image_file_link    —— 用 file_key 重新取下载链接

身份沿用 OPC 个人 API key（网关注入 X-Mail-Agent → client_key），不引入第二套鉴权。
产物落在本服务自己的目录，不写 opc.db 的 generated_files 表；下载走 bridge 静态挂载。
stdout 只承载 JSON-RPC 报文，所有日志（含 prototype 模块 import 时的 boot 输出）走 stderr。
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
from pathlib import Path

_PROTO_OUT = sys.stdout
sys.stdout = sys.stderr

SERVER_NAME = "mcp-image"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"

ROOT = "/opt/sefs/opc-workbench"
sys.path.insert(0, os.path.join(ROOT, "mcp"))
sys.path.insert(0, os.path.join(ROOT, "prototype"))

from opc_mcp import auth, config, graph_brief, media  # noqa: E402
from opc_mcp.store import Store  # noqa: E402

OUT_DIR = Path(os.environ.get("OPC_IMAGE_OUT_DIR") or os.path.join(ROOT, "mcp-image", "output"))
PUBLIC_BASE = os.environ.get("OPC_IMAGE_PUBLIC_BASE",
                             "https://ai.boraydata.com/conring/image").rstrip("/")
INLINE_MAX = int(os.environ.get("OPC_IMAGE_INLINE_MAX", str(8 * 1024 * 1024)))
INDEX = OUT_DIR / "index.json"

_CHANNEL_ALIAS = {
    "local": "sd15", "sd": "sd15", "sd1.5": "sd15", "本机": "sd15",
    "comfy": "qwen-image", "comfyui": "qwen-image", "157": "qwen-image",
    "qwen": "qwen-image", "qwen_image": "qwen-image", "qwen-image-2.1": "qwen-image",
    "bailian": "bailian_image", "aliyun": "bailian_image", "云": "bailian_image",
    "qwen-image-3.0-pro": "bailian_image",
}
_CHOICES = ("sd15", "qwen-image", "bailian_image")

_STORE = None
_STORE_LOCK = threading.Lock()
_INDEX_LOCK = threading.Lock()


def log(msg):
    sys.stderr.write("[mcp-image] %s\n" % msg)
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


def _channel(raw, reference):
    name = str(raw or "").strip().lower()
    name = _CHANNEL_ALIAS.get(name, name)
    if not name:
        return "qwen-image" if reference else "sd15"
    if name not in _CHOICES:
        raise ValueError("未知渠道 %r，可用：%s" % (raw, ", ".join(_CHOICES)))
    return name


def _url(key, filename):
    return "%s/assets/%s/%s" % (PUBLIC_BASE, key, urllib_quote(filename))


def urllib_quote(text):
    from urllib.parse import quote
    return quote(str(text), safe=".-_")


def _auth(args):
    """与 OPC 共用同一套 key；返回不含手机号的身份指纹。"""
    user = auth.resolve(_store(), args)
    return auth.fingerprint(user["phone"])


def image_capabilities(args):
    fp = _auth(args)
    caps = media.capabilities()
    image_caps = caps.get("image") or {}
    reach = caps.get("reachability") or {}
    return {
        "service": SERVER_NAME,
        "identity": fp,
        "channels": image_caps.get("channels", []),
        "backends": image_caps.get("backends", []),
        "reachability": {k: v for k, v in reach.items()
                         if k in ("image", "comfy_image")},
        "accepts": {
            "channel": list(_CHOICES) + sorted(_CHANNEL_ALIAS),
            "reference_image": "本地绝对路径（服务端可见）或 data:image/...;base64，≤10MB",
            "note": "sd15 仅文生图；传 reference_image 时自动改走 qwen-image（157）图生图。"
                    "bailian_image 按张计费，务必先确认再调用。",
        },
        "download": {
            "url_pattern": PUBLIC_BASE + "/assets/<file_key>/<filename>",
            "auth": "静态下载无需 key；file_key 不可枚举。image_file_link 可用同一 key 重取链接。",
            "inline_limit_bytes": INLINE_MAX,
        },
        "bailianError": caps.get("bailianError"),
    }


def image_generate(args):
    fp = _auth(args)
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    reference = args.get("reference_image")
    if isinstance(reference, str):
        reference = reference.strip() or None
    channel = _channel(args.get("channel"), reference)
    wait_sec = float(args.get("wait_sec") or 300)
    if channel == "qwen-image" and reference:
        wait_sec = max(wait_sec, 600)
    if channel == "bailian_image":
        n = int(args.get("n") or 1)
        if n < 1 or n > 4:
            raise ValueError("bailian_image 的 n 必须在 1~4")

    key = secrets.token_urlsafe(18).replace("-", "").replace("_", "")
    out_dir = OUT_DIR / key
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        res = media.image(
            prompt, str(out_dir),
            width=int(args.get("width") or 640),
            height=int(args.get("height") or 832),
            steps=int(args.get("steps") or 20),
            negative=args.get("negative_prompt"),
            seed=args.get("seed"),
            wait_sec=wait_sec,
            stem="image",
            channel=channel,
            reference_image=reference,
            brief=args.get("brief"),
            size=str(args.get("size") or "1024*1024"),
            n=int(args.get("n") or 1),
            prompt_extend=bool(args.get("prompt_extend", True)),
            watermark=bool(args.get("watermark", False)),
        )
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise

    path = Path(str(res.pop("path", "") or ""))
    if not path.is_file():
        shutil.rmtree(out_dir, ignore_errors=True)
        raise RuntimeError("生成服务未返回图片文件：%s" % str(res)[:300])
    if path.parent != out_dir:
        moved = out_dir / path.name
        shutil.move(str(path), str(moved))
        path = moved
    stat = path.stat()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    res.update({
        "file_key": key, "filename": path.name, "mime": mime, "size": stat.st_size,
        "download_url": _url(key, path.name),
        "elapsed_sec": round(time.time() - t0, 2),
    })
    record = dict(res)
    record.update({"owner": fp, "created_at": int(time.time()),
                   "prompt": prompt[:500], "channel": channel})
    with _INDEX_LOCK:
        index = _load_index()
        index[key] = record
        _save_index(index)

    payload = dict(res)
    payload.pop("output_url", None)          # 内网直链不外泄
    if args.get("return_base64", False) and stat.st_size <= INLINE_MAX:
        with open(path, "rb") as f:
            payload["image_base64"] = base64.b64encode(f.read()).decode("ascii")
        payload["image_mime"] = mime
    elif args.get("return_base64", False):
        payload["inline_skipped"] = "文件超过 %d 字节，请用 download_url 下载" % INLINE_MAX
    log("生成完成 channel=%s key=%s size=%d %.1fs"
        % (channel, key, stat.st_size, payload["elapsed_sec"]))
    return payload


def image_file_link(args):
    fp = _auth(args)
    key = str(args.get("file_key") or "").strip()
    with _INDEX_LOCK:
        rec = _load_index().get(key)
    if not rec:
        raise ValueError("file_key 不存在")
    if rec.get("owner") != fp:
        raise PermissionError("file_key 不属于当前身份")
    path = OUT_DIR / key / str(rec.get("filename") or "")
    if not path.is_file():
        raise ValueError("产物文件已不存在（可能已清理）")
    return {"file_key": key, "filename": path.name, "size": rec.get("size"),
            "mime": rec.get("mime"), "channel": rec.get("channel"),
            "download_url": _url(key, path.name),
            "download_note": "静态链接无需 key；必须带 file_key，无法枚举目录。"}


TOOLS = [
    {
        "name": "image_capabilities",
        "description": "查询独立图片服务的能力：sd15(本机 9103)、qwen-image(157 ComfyUI，支持图生图)、"
                       "bailian_image(阿里云百炼，按张计费) 的规格、后端可达性与下载口径。"
                       "调用 image_generate 前先查；身份沿用 OPC 个人 API key，无额外 token。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "image_generate",
        "description": "生成图片：只给 prompt 即文生图；传 reference_image（本地路径或 data URI）即图生图。"
                       "默认 sd15(本机)；channel=qwen-image 走 157，channel=bailian_image 走阿里云百炼按张计费。"
                       "返回 file_key 与 download_url；需要内联图片可设 return_base64=true（≤8MB）。",
        "inputSchema": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "画面文字描述"},
            "brief": {"type": "string", "description": "整体需求描述（用途、风格等，可选）"},
            "channel": {"type": "string", "default": "sd15",
                        "enum": ["sd15", "qwen-image", "bailian_image",
                                 "local", "comfy", "comfyui", "157", "bailian", "aliyun"]},
            "reference_image": {"type": "string",
                                "description": "参照图：服务端可见的绝对路径或 data:image/...;base64；给出即图生图"},
            "negative_prompt": {"type": "string"},
            "width": {"type": "integer", "default": 640},
            "height": {"type": "integer", "default": 832},
            "steps": {"type": "integer", "default": 20},
            "seed": {"type": "integer"},
            "size": {"type": "string", "default": "1024*1024",
                     "description": "仅 bailian_image：输出尺寸「宽*高」，面积 512²~2048²、比例 1:8~8:1"},
            "n": {"type": "integer", "default": 1, "description": "仅 bailian_image：出图张数 1~4，按张计费"},
            "prompt_extend": {"type": "boolean", "default": True},
            "watermark": {"type": "boolean", "default": False},
            "wait_sec": {"type": "number", "default": 300},
            "return_base64": {"type": "boolean", "default": False,
                              "description": "是否内联返回 base64 图片（≤8MB，默认只回下载链接）"},
        }, "required": ["prompt"]},
    },
    {
        "name": "image_file_link",
        "description": "用 image_generate 返回的 file_key 重新取下载链接（同一把 key 才能取）。",
        "inputSchema": {"type": "object", "properties": {
            "file_key": {"type": "string", "description": "image_generate 结果里的 file_key"}},
            "required": ["file_key"]},
    },
]

_DISPATCH = {
    "image_capabilities": image_capabilities,
    "image_generate": image_generate,
    "image_file_link": image_file_link,
}

_OUT_LOCK = threading.Lock()


def _send(obj):
    with _OUT_LOCK:
        _PROTO_OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
        _PROTO_OUT.flush()


def _result(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def handle(req):
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        _result(rid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    elif method == "tools/list":
        _result(rid, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        fn = _DISPATCH.get(name)
        if not fn:
            _result(rid, {"content": [{"type": "text", "text": "未知工具 %s" % name}],
                          "isError": True})
            return
        try:
            payload = fn(params.get("arguments") or {})
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            content = [{"type": "text", "text": text}]
            if name == "image_generate" and payload.get("image_base64"):
                content.append({"type": "image", "data": payload["image_base64"],
                                "mimeType": payload.get("image_mime") or "image/png"})
            _result(rid, {"content": content, "isError": False})
        except Exception as e:
            log(traceback.format_exc())
            _result(rid, {"content": [{"type": "text", "text": "图片生成失败：%s" % e}],
                          "isError": True})
    elif method == "ping":
        _result(rid, {})
    elif rid is not None:
        _send({"jsonrpc": "2.0", "id": rid,
               "error": {"code": -32601, "message": "未实现的方法 %s" % method}})


def _safe(req):
    try:
        handle(req)
    except Exception:
        log(traceback.format_exc())


def main():
    log("启动 %s %s；产物目录 %s；公开基址 %s"
        % (SERVER_NAME, SERVER_VERSION, OUT_DIR, PUBLIC_BASE))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
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
            if isinstance(item, dict) and item.get("method") == "tools/call":
                threading.Thread(target=_safe, args=(item,), daemon=True).start()
            else:
                _safe(item)


if __name__ == "__main__":
    main()
