#!/usr/bin/env python3
"""生成员工全身照（ComfyUI Qwen-Image 2.1 参考图扩展）。

用法:
  python staff_photos.py full  <emp_id|all>   生成全身照到 assets/fullbody_raw/
  python staff_photos.py ids   <emp_id|all>   由现有肖像生成白底证件照到 assets/idphoto/
  python staff_photos.py swap  <emp_id|all>   用现有肖像做人脸替换，输出 assets/fullbody/
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = "/opt/sefs/opc-workbench/prototype"
ASSETS = os.path.join(ROOT, "assets")
EMP_JSON = "/opt/sefs/mcp-asr/employees.json"
# ComfyUI 通道：只走 21.157（30.157 已确认没有 ComfyUI，放弃）
HOST = ""
HOST_CANDIDATES = [h for h in (
    os.environ.get("COMFY_HOST", ""),
    "http://192.168.21.157:8188",
) if h]
LOG = "/opt/sefs/opc-workbench/.cache/staff_media/gen.log"

os.makedirs(os.path.dirname(LOG), exist_ok=True)
os.makedirs(os.path.join(ASSETS, "fullbody_raw"), exist_ok=True)
os.makedirs(os.path.join(ASSETS, "fullbody"), exist_ok=True)
os.makedirs(os.path.join(ASSETS, "idphoto"), exist_ok=True)


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def pick_host():
    """按优先级探测可用的 ComfyUI；30.157 优先，不可用再回落 21.157。"""
    for h in HOST_CANDIDATES:
        h = h.rstrip("/")
        try:
            with urllib.request.urlopen(h + "/system_stats", timeout=4) as r:
                st = json.load(r)
            devs = [d.get("name") for d in (st.get("devices") or [])]
            ram = round((st.get("system") or {}).get("ram_total", 0) / 1e9, 1)
            log(f"ComfyUI 通道 = {h} (devices={devs}, ram={ram}GB)")
            return h
        except Exception as ex:
            log(f"通道不可用 {h}: {type(ex).__name__} {ex}")
    raise SystemExit("没有可用的 ComfyUI 通道：" + ", ".join(HOST_CANDIDATES))


def load_emps():
    return json.load(open(EMP_JSON))["employees"]


def portrait(emp_id):
    return os.path.join(ASSETS, "portraits", f"{emp_id}.png")


def upload(path, name):
    boundary = "----b" + uuid.uuid4().hex
    body = b""
    for k, v in (("overwrite", "true"), ("type", "input")):
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
             f"Content-Type: image/png\r\n\r\n").encode()
    with open(path, "rb") as f:
        body += f.read()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(HOST + "/upload/image", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.load(urllib.request.urlopen(req, timeout=120)).get("name", name)


def graph(ref_name, prompt, prefix, seed=20261112):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2.1_turbo_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "cpu"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "4": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "cpu", "dtype": "default"}},
        "13": {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "5": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["2", 0], "prompt": prompt, "negative_prompt": "", "vae": ["3", 0],
            "resolution": 768, "images": {"image_1": ["13", 0]}}},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "7": {"class_type": "CFGGuider", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["5", 1], "cfg": 1.0}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "ManualSigmas", "inputs": {"sigmas": "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["6", 0], "guider": ["7", 0], "sampler": ["8", 0], "sigmas": ["9", 0], "latent_image": ["5", 2]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "SaveImage", "inputs": {"images": ["11", 0], "filename_prefix": prefix}},
    }


def submit(g):
    req = urllib.request.Request(HOST + "/prompt", data=json.dumps({"prompt": g, "client_id": "staff_photos"}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))["prompt_id"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode()[:1500])


def wait(pid, timeout=2400):
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = json.load(urllib.request.urlopen(f"{HOST}/history/{pid}", timeout=30))
        if pid in h:
            return h[pid]
        time.sleep(8)
    raise TimeoutError(pid)


def download(v, dst):
    for o in v.get("outputs", {}).values():
        for im in o.get("images", []):
            q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": im.get("type", "output")})
            with urllib.request.urlopen(f"{HOST}/view?{q}", timeout=120) as r, open(dst, "wb") as f:
                f.write(r.read())
            return dst
    raise RuntimeError("no image in outputs")


_COLORS = [
    ("黑色", (26, 26, 26)), ("深藏青", (27, 42, 74)), ("藏青色", (35, 57, 91)),
    ("深灰色", (58, 63, 69)), ("中灰色", (110, 115, 120)), ("浅灰色", (201, 204, 209)),
    ("米白色", (239, 233, 220)), ("白色", (250, 250, 250)), ("金色", (200, 162, 74)),
    ("香槟金", (217, 190, 126)), ("酒红色", (123, 34, 49)), ("深绿色", (32, 81, 75)),
    ("墨绿色", (18, 59, 50)), ("咖色", (107, 74, 50)), ("驼色", (176, 138, 94)),
    ("蓝色", (47, 92, 138)), ("浅蓝色", (150, 185, 215)), ("粉色", (217, 160, 168)),
    ("紫色", (91, 75, 138)), ("鹅黄色", (235, 205, 120)),
]

_HAIR = {"short": "利落短发", "long": "长直发", "bun": "盘发", "curly": "卷发"}
_EMO = {"cheerful": "面带自然微笑", "calm": "神情平和沉稳", "serious": "表情严肃专注", "neutral": "表情自然"}


def color_name(hex_str, fallback="深色"):
    try:
        h = hex_str.lstrip("#")
        rgb = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except (ValueError, IndexError, AttributeError, TypeError):
        return fallback
    return min(_COLORS, key=lambda c: sum((a - b) ** 2 for a, b in zip(c[1], rgb)))[0]


def full_prompt(e):
    male = e.get("gender") == "male"
    he = "男士" if male else "女士"
    ta = "他" if male else "她"
    suit = color_name(e.get("suit"), "深色")
    shirt = color_name(e.get("shirt"), "白色")
    hair = _HAIR.get(e.get("hairStyle") or "short", "")
    emo = _EMO.get(e.get("emotion") or "calm", "表情自然")
    glasses = "，戴细框眼镜" if e.get("glasses") else ""
    bottoms = f"{suit}西裤" if male else f"{suit}西裤或及膝一步裙"
    return (
        f"以图 1 这位{e.get('age')}岁中国{he}的半身照为唯一身份依据，把它扩展成一张全身正面站姿照片，不要另画一个人："
        f"脸型、五官、肤色、年龄感、体型与图 1 完全一致，发型为{ta}图 1 中的{hair}；"
        f"服装沿用图 1 风格并补全整套，{suit}西装外套配{shirt}衬衫{glasses}，下身穿{bottoms}和深色皮鞋；"
        f"{ta}{emo}，双臂自然垂在身体两侧，双脚并拢站立；"
        f"浅灰白色极简摄影棚背景，柔和均匀的正面光，{ta}从头到脚完整入镜并居中，头顶和鞋子都不出画；"
        f"真实商业人物摄影，高清写实，画面中只有{ta}一个人，不要文字、水印、logo、其他人或多余的手。"
    )


# ---------------- 证件照：抠背景 + 白底 + 3:4 构图 ----------------

def make_id_photo(emp_id):
    from PIL import Image
    from rembg import remove, new_session

    src = Image.open(portrait(emp_id)).convert("RGBA")
    # 本机已有 isnet-general-use.onnx，避免 rembg 去 GitHub 下载 1GB 的 bria 模型
    session = new_session("isnet-general-use")
    cut = remove(src, session=session)
    bbox = cut.getbbox()
    if bbox:
        cut = cut.crop(bbox)

    W, H = 768, 1024
    # 证件照构图：人像宽度占 86%，顶部留 8% 空白，水平居中，白底
    scale = (W * 0.86) / cut.width
    head = cut.resize((int(cut.width * scale), max(1, int(cut.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    top = int(H * 0.08)
    left = (W - head.width) // 2
    if head.height + top > H:
        head = head.crop((0, 0, head.width, H - top))
    canvas.alpha_composite(head, (max(0, left), top))
    out = canvas.convert("RGB")
    dst = os.path.join(ASSETS, "idphoto", f"{emp_id}.png")
    out.save(dst, quality=95)
    return dst


# ---------------- 人脸替换 ----------------

_APP = None
_SWAPPER = None


def _models():
    global _APP, _SWAPPER
    if _SWAPPER is None:
        import insightface
        from insightface.app import FaceAnalysis
        _APP = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"], allowed_modules=["detection", "recognition"])
        _APP.prepare(ctx_id=-1, det_size=(640, 640))
        _SWAPPER = insightface.model_zoo.get_model("/opt/sefs/models/inswapper_128.onnx", providers=["CPUExecutionProvider"])
    return _APP, _SWAPPER


def swap_face(emp_id, src_path, dst_path):
    import cv2
    app, swapper = _models()
    src_img = cv2.imread(portrait(emp_id))
    dst_img = cv2.imread(src_path)
    src_faces = app.get(src_img)
    dst_faces = app.get(dst_img)
    if not src_faces or not dst_faces:
        raise RuntimeError(f"{emp_id}: face not found (src={len(src_faces)}, dst={len(dst_faces)})")
    src_face = max(src_faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    dst_face = max(dst_faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    out = swapper.get(dst_img, dst_face, src_face, paste_back=True)
    cv2.imwrite(dst_path, out)
    return dst_path


def main():
    global HOST
    mode = sys.argv[1]
    which = sys.argv[2] if len(sys.argv) > 2 else "all"
    if mode in ("full",):
        HOST = pick_host()
    emps = load_emps()
    todo = [e for e in emps if which == "all" or e["id"] == which]
    for e in todo:
        eid = e["id"]
        if mode == "full":
            dst = os.path.join(ASSETS, "fullbody_raw", f"{eid}.png")
            if os.path.exists(dst) and os.path.getsize(dst) > 20000:
                log(f"skip {eid} (exists)")
                continue
            name = upload(portrait(eid), f"staff_ref_{eid}.png")
            t0 = time.time()
            seed = 20261112 + int(hashlib.md5(eid.encode()).hexdigest()[:6], 16) % 100000
            pid = submit(graph(name, full_prompt(e), f"staff_full_{eid}", seed))
            v = wait(pid)
            if v["status"]["status_str"] != "success":
                log(f"FAIL {eid}: {json.dumps(v['status'], ensure_ascii=False)[:400]}")
                continue
            download(v, dst)
            log(f"done {eid} {round(time.time()-t0,1)}s -> {dst}")
        elif mode == "ids":
            dst = os.path.join(ASSETS, "idphoto", f"{eid}.png")
            if os.path.exists(dst):
                continue
            log(f"idphoto {eid} -> {make_id_photo(eid)}")
        elif mode == "swap":
            raw = os.path.join(ASSETS, "fullbody_raw", f"{eid}.png")
            dst = os.path.join(ASSETS, "fullbody", f"{eid}.png")
            if not os.path.exists(raw):
                log(f"skip {eid} (no raw)")
                continue
            try:
                log(f"swap {eid} -> {swap_face(eid, raw, dst)}")
            except Exception as ex:
                log(f"swap FAIL {eid}: {ex}")
                # 兜底：保留生成图
                import shutil
                shutil.copy(raw, dst)


if __name__ == "__main__":
    main()
