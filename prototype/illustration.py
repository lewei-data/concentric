"""本地 SVG 插图资产库：分镜 → 透明底 SVG → Remotion 可直接引用。

不依赖绘图模型；用固定风格的 SVG 元件 + 版式合成，保证透明、可缓存、可降级。
真实接入时，分镜可带 illustration.objects / illustration.layout；没有就按画面文本推断。
"""
import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# --- ComfyUI 作图（qwen-image-2.1-turbo）配置 ---
# 开启 OPC_USE_COMFYUI_IMAGES=1 后，制图优先用 ComfyUI 文生图出 PNG，失败自动回退本地 SVG。
# 直连 ComfyUI 作图服务（不走 127.0.0.1 代理隧道）。
COMFY_IMAGE_URL = os.environ.get("OPC_COMFY_IMAGE_URL", "http://192.168.21.157:8188")
USE_COMFYUI_IMAGES = os.environ.get("OPC_USE_COMFYUI_IMAGES", "").lower() in ("1", "true", "yes")
COMFY_IMAGE_TIMEOUT = int(os.environ.get("OPC_COMFY_IMAGE_TIMEOUT", "300"))

# qwen-image-2.1-t2i 工作流（与 OpenMontage tools/_comfyui/workflows/qwen-image-2.1-t2i.json 一致）
_COMFY_QWEN_WF = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2.1_turbo_int8_convrot.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image"}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
    "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "prompt": "", "negative_prompt": "blurry, low quality, distorted, text, watermark, extra limbs, deformed", "resolution": 0}},
    "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
    "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 0], "latent_image": ["5", 0], "seed": 0, "steps": 8, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
    "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "om_qwen"}},
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SVG_DIR = os.path.join(BASE_DIR, "assets", "svg")
MANIFEST_PATH = os.path.join(SVG_DIR, "manifest.json")
DEFAULT_REMOTION_DIR = os.environ.get("OPC_REMOTION_DIR", "/opt/sefs/OpenMontage/remotion-composer")
CANVAS = 1200

# 与 om_render.THEME_BG / THEME_SURFACE 对齐的纸感配色
PROJECT_STYLE = {
    "wenren": {"paper": "#F5F1E8", "surface": "#FFFDF7", "ink": "#2F2A26",
               "primary": "#2563EB", "accent": "#B45309", "muted": "#D8D2C4"},
    "gou": {"paper": "#FFF7ED", "surface": "#FFFFFF", "ink": "#3B2416",
            "primary": "#EA580C", "accent": "#F59E0B", "muted": "#FDE3C4"},
    "tongxinyuan": {"paper": "#F5F3FF", "surface": "#FFFFFF", "ink": "#241E3A",
                    "primary": "#7C3AED", "accent": "#0EA5E9", "muted": "#DDD6FE"},
    "all": {"paper": "#F8FAFC", "surface": "#FFFFFF", "ink": "#1F2937",
            "primary": "#0D9488", "accent": "#0EA5E9", "muted": "#E2E8F0"},
}

# 画面文本 → 元件（按匹配到的标签数量排序，取前 2 个）
_DEFAULT_ASSETS = ["photo_paper", "magnifier"]


def _log(msg):
    print(f"[illustration] {msg}", flush=True)


def load_manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _asset_map():
    return {a["id"]: a for a in load_manifest().get("assets", [])}


def _pick_asset(name, amap):
    """支持 asset id、中文标签、英文标签。"""
    if not name:
        return None
    name = str(name).strip()
    if name in amap:
        return name
    low = name.lower()
    for aid, meta in amap.items():
        if low == str(meta.get("label", "")).lower():
            return aid
        for tag in meta.get("tags", []):
            if low == str(tag).lower():
                return aid
    for aid, meta in amap.items():
        if any(str(tag).lower() in low or low in str(tag).lower()
               for tag in meta.get("tags", []) if len(str(tag)) >= 2):
            return aid
    return None


def _infer_assets(text, amap, limit=2):
    scores = {}
    text = text or ""
    low = text.lower()
    for aid, meta in amap.items():
        score = 0
        for tag in meta.get("tags", []):
            t = str(tag)
            if not t:
                continue
            if t.isascii():
                # 英文标签按词边界匹配，避免 "api" 命中 rapid、"ai" 命中 detail
                hit = re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(t), text, re.I)
                if hit:
                    score += max(1, len(t))
            elif t in text:
                score += max(1, len(t))
        if score:
            scores[aid] = score
    ordered = sorted(scores, key=lambda a: (-scores[a], a))
    return ordered[:limit] or _DEFAULT_ASSETS[:]


def infer_spec(shot, item=None, project_id="all"):
    """从分镜生成插图规格；优先用 shot['illustration']，否则按画面/台词推断。"""
    item = item or {}
    amap = _asset_map()
    ill = shot.get("illustration") if isinstance(shot, dict) else None
    if not isinstance(ill, dict):
        ill = {}

    raw_objects = ill.get("objects") or ill.get("assets") or ill.get("物件")
    assets = []
    if isinstance(raw_objects, str):
        raw_objects = [x for x in re.split(r"[/、,，|]", raw_objects) if x.strip()]
    if isinstance(raw_objects, list):
        for obj in raw_objects:
            aid = _pick_asset(obj, amap)
            if aid and aid not in assets:
                assets.append(aid)
    if not assets:
        text = " ".join(str(x or "") for x in (
            shot.get("visual"), shot.get("line"), item.get("title"), item.get("summary")))
        assets = _infer_assets(text, amap, 2)

    layout = str(ill.get("layout") or "").strip().lower()
    if layout not in ("hero", "duo", "trio", "left", "right", "center"):
        layout = "duo" if len(assets) >= 2 else "hero"
    if layout == "center":
        layout = "hero"

    style = dict(PROJECT_STYLE.get(project_id) or PROJECT_STYLE["all"])
    custom = ill.get("palette") if isinstance(ill.get("palette"), dict) else {}
    for key in ("paper", "surface", "ink", "primary", "accent", "muted"):
        if custom.get(key):
            style[key] = str(custom[key])

    return {
        "assets": assets,
        "layout": layout,
        "palette": style,
        "captionSpace": ill.get("captionSpace") or "bottom",
        "prompt": ill.get("prompt") or "",
        "caption": ill.get("caption") or "",
    }


def _inner_svg(text):
    text = re.sub(r"^.*?<svg[^>]*>", "", text, count=1, flags=re.S)
    return re.sub(r"</svg>\s*$", "", text).strip()


def _colorize(svg_text, palette):
    for key in ("PAPER", "SURFACE", "INK", "PRIMARY", "ACCENT", "MUTED"):
        svg_text = svg_text.replace("{{%s}}" % key, str(palette.get(key.lower(), "")))
    return svg_text


def _place(layout, n):
    """返回每个元件的 (center_x, center_y, scale)。"""
    if layout == "left":
        return [(360, 560, 2.0)]
    if layout == "right":
        return [(840, 560, 2.0)]
    if layout == "duo" or n >= 2:
        if n == 1:
            return [(600, 540, 2.4)]
        return [(600, 420, 1.9), (600, 840, 1.15)]
    if layout == "trio" or n >= 3:
        return [(600, 470, 1.9), (300, 840, 0.9), (900, 840, 0.9)]
    return [(600, 540, 2.4)]


def compose(spec, out_path, title=""):
    """把 1-3 个 SVG 元件合成一张 1200×1200 透明底 SVG。"""
    amap = _asset_map()
    palette = spec["palette"]
    assets = [a for a in (spec.get("assets") or []) if a in amap][:3]
    if not assets:
        assets = _DEFAULT_ASSETS[:]
    places = _place(spec.get("layout") or "hero", len(assets))
    groups = []
    for idx, aid in enumerate(assets):
        path = os.path.join(SVG_DIR, amap[aid]["file"])
        with open(path, encoding="utf-8") as f:
            body = _colorize(_inner_svg(f.read()), palette)
        cx, cy, scale = places[min(idx, len(places) - 1)]
        tx, ty = cx - 200 * scale, cy - 200 * scale
        groups.append(f'<g transform="translate({tx:.1f},{ty:.1f}) scale({scale:.3f})">{body}</g>')

    paper = palette["paper"]
    primary = palette["primary"]
    accent = palette["accent"]
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS} {CANVAS}" width="{CANVAS}" height="{CANVAS}">
<defs>
  <filter id="softShadow" x="-30%" y="-30%" width="160%" height="160%">
    <feDropShadow dx="0" dy="26" stdDeviation="30" flood-color="#1F2937" flood-opacity="0.18"/>
  </filter>
</defs>
<circle cx="600" cy="560" r="460" fill="{paper}" opacity="0.55"/>
<circle cx="600" cy="560" r="460" fill="none" stroke="{accent}" stroke-width="3" opacity="0.18"/>
<g filter="url(#softShadow)">
{''.join(groups)}
</g>
<circle cx="250" cy="250" r="12" fill="{primary}" opacity="0.55"/>
<circle cx="940" cy="300" r="8" fill="{accent}" opacity="0.55"/>
<path d="M880 900 q40 -30 80 0" fill="none" stroke="{accent}" stroke-width="8" opacity="0.5"/>
<path d="M240 920 h70" stroke="{primary}" stroke-width="8" opacity="0.35"/>
</svg>
'''
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)
    qa = _qa_svg(out_path)
    return {"path": out_path, "qa": qa, "spec": spec, "title": title}


def _qa_svg(path):
    try:
        text = open(path, encoding="utf-8").read()
        root = ET.fromstring(text)
        tag = root.tag.split("}")[-1]
        if tag != "svg":
            return {"pass": False, "reason": "not svg"}
        if "viewBox" not in root.attrib:
            return {"pass": False, "reason": "missing viewBox"}
        # 不允许整张画布的背景矩形，否则透明底会变成白底/黑底
        for rect in root.iter():
            if rect.tag.split("}")[-1] != "rect":
                continue
            try:
                w = float(rect.attrib.get("width", "0").replace("px", ""))
                h = float(rect.attrib.get("height", "0").replace("px", ""))
            except Exception:
                continue
            if w >= CANVAS and h >= CANVAS and rect.attrib.get("fill", "") not in ("none", "transparent"):
                return {"pass": False, "reason": "full-canvas background rect"}
        if os.path.getsize(path) < 500:
            return {"pass": False, "reason": "file too small"}
        return {"pass": True, "bytes": os.path.getsize(path)}
    except Exception as e:
        return {"pass": False, "reason": f"{type(e).__name__}: {e}"}


def _comfy_post(url, data=None, timeout=10):
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _default_prompt(shot, item, project_id):
    bits = [str(x or "") for x in (shot.get("visual"), shot.get("line"), item.get("title")) if x]
    base = " ".join(bits) or "温暖质感的插画"
    return f"{base}，电影感，精致构图，柔光，暖色调，高质量插画风格，无文字"


def _comfyui_generate(prompt, out_path, width=1024, height=1024, timeout=COMFY_IMAGE_TIMEOUT):
    """调用 ComfyUI（qwen-image-2.1-turbo）文生图，落盘 PNG。成功返回 True。"""
    try:
        _comfy_post(COMFY_IMAGE_URL + "/system_stats", timeout=8)
    except Exception as e:
        _log(f"ComfyUI 不可达({COMFY_IMAGE_URL})，跳过作图：{e}")
        return False
    wf = json.loads(json.dumps(_COMFY_QWEN_WF))
    wf["4"]["inputs"]["prompt"] = prompt
    wf["4"]["inputs"]["negative_prompt"] = (
        "blurry, low quality, distorted, text, watermark, extra limbs, deformed, "
        "no text, no words, 无文字 无字母 无标题"
    )
    wf["5"]["inputs"]["width"] = width
    wf["5"]["inputs"]["height"] = height
    wf["6"]["inputs"]["seed"] = int(time.time() * 1000) % (1 << 31)
    wf["8"]["inputs"]["filename_prefix"] = f"om_{int(time.time()) % 100000}"
    body = json.dumps({"prompt": wf, "client_id": "opc-illustration"},
                      ensure_ascii=False).encode("utf-8")
    try:
        resp = json.loads(_comfy_post(COMFY_IMAGE_URL + "/prompt", data=body, timeout=20).decode("utf-8"))
    except Exception as e:
        _log(f"ComfyUI 提交失败：{e}")
        return False
    pid = resp.get("prompt_id")
    if not pid:
        _log(f"ComfyUI 拒绝作业：{resp}")
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            hist = json.loads(_comfy_post(f"{COMFY_IMAGE_URL}/history/{pid}", timeout=20).decode("utf-8"))
        except Exception:
            time.sleep(2)
            continue
        entry = hist.get(pid) or {}
        if entry.get("status", {}).get("status_str") == "error":
            _log("ComfyUI 作业执行失败")
            return False
        outs = (entry.get("outputs") or {}).get("8") or {}
        imgs = outs.get("images") or []
        if imgs:
            q = urllib.parse.urlencode({
                "filename": imgs[0]["filename"],
                "subfolder": imgs[0].get("subfolder", ""),
                "type": imgs[0].get("type", "output"),
            })
            try:
                blob = _comfy_post(f"{COMFY_IMAGE_URL}/view?{q}", timeout=60)
            except Exception as e:
                _log(f"ComfyUI 取图失败：{e}")
                return False
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(blob)
            return True
        time.sleep(2)
    _log("ComfyUI 出图超时")
    return False


def ensure_assets(item, project_id, shots, files, remotion_dir=None):
    """为每条内容生成插图（ComfyUI 作图优先，失败回退本地 SVG），并复制到 Remotion public 目录。

    返回 {shot_index: {"source": "om/<dir>/s01.png|svg", "layout": "center", "animation": "float-in"}}
    """
    import deliverables as DL
    item_dir = DL._local(files["dir"])
    assets_dir = os.path.join(item_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    remotion_dir = remotion_dir or DEFAULT_REMOTION_DIR
    safe_item = re.sub(r"[^A-Za-z0-9_.-]", "_", str(item.get("id") or "item"))
    safe_trace = re.sub(r"[^A-Za-z0-9_.-]", "_", str(item.get("traceId") or "trace"))[-8:]
    pub_rel = f"om/{safe_trace}-{safe_item}"
    pub_dir = os.path.join(remotion_dir, "public", pub_rel)
    os.makedirs(pub_dir, exist_ok=True)

    out_map, manifest_shots = {}, []
    for i, shot in enumerate(shots):
        spec = infer_spec(shot, item, project_id)
        engine = "local-svg"
        public_name = None
        out_file = None

        # 1) 优先 ComfyUI 作图（qwen-image-2.1-turbo）
        if USE_COMFYUI_IMAGES:
            prompt = ((shot.get("illustration") or {}).get("prompt")
                      or shot.get("image_prompt")
                      or spec.get("prompt")
                      or _default_prompt(shot, item, project_id))
            png = os.path.join(assets_dir, f"illustration-s{i + 1:02d}.png")
            if _comfyui_generate(prompt, png):
                engine = "comfyui-qwen"
                out_file = png
                public_name = f"s{i + 1:02d}.png"
            else:
                _log(f"镜{i + 1} ComfyUI 作图失败，回退本地 SVG")

        # 2) 回退：本地 SVG 矢量图
        if public_name is None:
            out = os.path.join(assets_dir, f"illustration-s{i + 1:02d}.svg")
            try:
                info = compose(spec, out, title=str(shot.get("visual") or ""))
            except Exception as e:
                _log(f"镜{i + 1} 插图生成失败，回退矢量场景：{type(e).__name__}: {e}")
                continue
            if not info["qa"].get("pass"):
                _log(f"镜{i + 1} 插图 QA 失败，回退矢量场景：{info['qa'].get('reason')}")
                continue
            out_file = out
            public_name = f"s{i + 1:02d}.svg"

        pub_path = os.path.join(pub_dir, public_name)
        shutil.copyfile(out_file, pub_path)
        out_map[i] = {
            "source": f"{pub_rel}/{public_name}",
            "layout": "center" if spec["layout"] in ("duo", "trio", "hero") else spec["layout"],
            "animation": "float-in",
        }
        manifest_shots.append({
            "shot": i + 1,
            "assets": spec["assets"],
            "layout": spec["layout"],
            "palette": spec["palette"],
            "prompt": ((shot.get("illustration") or {}).get("prompt") or spec.get("prompt") or ""),
            "engine": engine,
            "file": os.path.relpath(out_file, item_dir),
            "public": f"{pub_rel}/{public_name}",
            "qa": {"pass": True, "bytes": os.path.getsize(out_file)},
        })

    manifest = {
        "version": 1,
        "engine": "comfyui-qwen" if USE_COMFYUI_IMAGES else "local-svg",
        "itemId": item.get("id"),
        "traceId": item.get("traceId"),
        "projectId": project_id,
        "count": len(out_map),
        "shots": manifest_shots,
    }
    with open(os.path.join(item_dir, "10-illustrations.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    _log(f"{item.get('id')} 插图 {len(out_map)}/{len(shots)} 张（engine={manifest['engine']}），清单 10-illustrations.json")
    return out_map


def _demo(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    samples = [
        ("photo_paper", ["photo_paper", "developing_tray"], "hero", "相纸在药水里浮出影像"),
        ("chat_bubble", ["chat_bubble", "red_envelope"], "duo", "群聊里有人发红包"),
        ("document", ["document", "stamp"], "duo", "合同盖章通过"),
    ]
    for i, (name, assets, layout, title) in enumerate(samples):
        spec = infer_spec({"visual": title, "illustration": {"objects": assets, "layout": layout}},
                          {"title": title}, "wenren")
        info = compose(spec, os.path.join(out_dir, f"{i + 1:02d}-{name}.svg"), title)
        print(json.dumps({"file": info["path"], "qa": info["qa"], "assets": spec["assets"]},
                         ensure_ascii=False))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", metavar="OUT_DIR", help="生成 3 张示例 SVG")
    args = ap.parse_args()
    if args.demo:
        _demo(args.demo)
    else:
        ap.print_help()
