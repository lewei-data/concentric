"""真实截图 → Remotion 竖屏成片（普通图 contain 适配，长图纵向滚动）。

与 om_render.produce() 的区别：后者走 SVG 插图场景；这里直接把用户给的
截图放进 composer 的 public 目录，用 scroll_image_scene 渲染。

复用 om_render 的 TTS / 音频拼接 / 验收门，不重复实现。
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from PIL import Image

import deliverables as DL
import om_render as OMR

REMOTION_DIR = OMR.REMOTION_DIR
NODE_BIN = OMR.NODE_BIN


def _log(msg):
    print(f"[image-reel] {msg}", flush=True)


def _size(path):
    with Image.open(path) as im:
        return {"width": im.width, "height": im.height}


def produce(item, project, images, spec=None):
    """images: 图片路径列表，顺序即镜头顺序。返回与 om_render.produce 同构的结果。"""
    sp = OMR._spec(spec)
    files = DL.plan(item["traceId"], item["projectId"], item["id"])
    item_dir = Path(DL._local(files["dir"]))
    work = os.path.join(str(item_dir), "_image_render")
    os.makedirs(work, exist_ok=True)
    video_path = DL._local(files["video"])
    shots = DL.normalize_storyboard(item.get("storyboard"), item.get("durationSec", 45))
    if not shots:
        return {"ok": False, "reason": "没有可用分镜"}
    n = min(len(shots), len(images))
    if n < len(images):
        _log(f"分镜 {len(shots)} 条 / 图片 {len(images)} 张，按 {n} 镜处理")

    def result(ok, reason="", check=None, duration=0):
        return {"ok": ok, "reason": reason, "check": check or {},
                "video": video_path if ok else "", "videoUrl": files["video"],
                "duration": round(duration, 2), "shots": n,
                "engine": "MCP-TTS(CosyVoice2) + OpenMontage/Remotion(scroll_image_scene)",
                "renderedAt": OMR._now(), "traceId": item.get("traceId"), "itemId": item.get("id")}

    t0 = time.time()
    job = f"{item['traceId'][-6:]}-{item['id']}".replace("/", "_")
    pub_dir = os.path.join(REMOTION_DIR, "public", "om", job)
    os.makedirs(pub_dir, exist_ok=True)

    cuts, words, segments, duration = [], [], [], 0.0
    seg_dir = os.path.join(work, "tts")
    os.makedirs(seg_dir, exist_ok=True)
    for i in range(n):
        src = Path(images[i])
        if not src.is_file():
            return result(False, f"第 {i+1} 张图片不存在：{src}")
        im_name = f"im{i+1:02d}.png"
        shutil.copyfile(src, os.path.join(pub_dir, im_name))
        natural = _size(src)

        s = shots[i]
        line, silent = OMR._narration_text(s.get("line"))
        planned = float(s.get("dur") or 0) or item.get("durationSec", 45) / n
        payload = None
        if line:
            try:
                payload = OMR._synth_checked(line, seg_dir, i)
            except Exception as e:
                _log(f"镜{i+1} 旁白失败，降级静音：{type(e).__name__}: {e}")
            audio_dur = float(payload.get("duration") or 0) if payload else 0.0
            sec = max(planned, audio_dur + 0.5)
            segments.append((payload["audio_path"], sec) if payload else None)
            words += OMR._caption_words(line, duration, duration + sec)
        else:
            sec = planned
            segments.append(None)
        mode = "scroll" if natural["height"] / natural["width"] > 1.4 else "fit"
        cut = {"id": f"cut-{i+1:02d}", "source": f"om/{job}/{im_name}",
               "type": "scroll_image_scene", "naturalSize": natural,
               "imageMode": mode, "in_seconds": duration, "out_seconds": duration + sec,
               "line": line}
        if mode == "fit":
            cut["fitPadding"] = 0.04
        else:
            cut["holdStart"] = 0.8
            cut["holdEnd"] = 0.8
        cuts.append(cut)
        duration += sec
        _log(f"镜{i+1} {src.name} {natural['width']}x{natural['height']} → {mode} / {sec:.1f}s")

    parts = []
    for idx, seg in enumerate(segments):
        dst = os.path.join(work, f"narr{idx+1:02d}.wav")
        sec = cuts[idx]["out_seconds"] - cuts[idx]["in_seconds"]
        if seg is None:
            if not OMR._silence(dst, sec):
                return result(False, f"第 {idx+1} 镜静音生成失败")
        elif not OMR._pad_audio(seg[0], dst, sec):
            return result(False, f"第 {idx+1} 镜音频补齐失败")
        parts.append(dst)
    narration = os.path.join(work, "narration.wav")
    ok, out = OMR._concat_audio(parts, narration, work)
    if not ok:
        return result(False, f"旁白拼接失败：{out[-160:]}")
    narration_src = f"om/{job}/narration.wav"
    shutil.copyfile(narration, os.path.join(pub_dir, "narration.wav"))
    _log(f"旁白完成 {duration:.1f}s，TTS 耗时 {time.time()-t0:.1f}s")

    props = {
        "meta": {"source": "opc-image-reel", "traceId": item.get("traceId"),
                 "itemId": item.get("id"), "omStage": "compose"},
        "cuts": cuts, "overlays": [], "captions": words,
        "audio": {"narration": {"src": narration_src, "volume": 1.0}},
        "themeConfig": {
            "primaryColor": project.get("color") or "#7C3AED",
            "accentColor": "#0EA5E9", "backgroundColor": "#0B1020",
            "surfaceColor": "#111827", "textColor": "#F8FAFC",
            "mutedTextColor": "#A5B4C8", "headingFont": "Inter",
            "bodyFont": "Inter", "monoFont": "JetBrains Mono",
            "chartColors": ["#7C3AED", "#0EA5E9", "#10B981", "#F59E0B"],
            "springConfig": {"damping": 20, "stiffness": 120, "mass": 1},
            "transitionDuration": 0.4, "captionHighlightColor": "#FBBF24",
            "captionBackgroundColor": "rgba(15,23,42,0.82)",
        },
    }
    props_path = os.path.join(work, "props.json")
    with open(props_path, "w", encoding="utf-8") as f:
        json.dump(props, f, ensure_ascii=False, indent=2)

    tmp_video = os.path.join(work, "render.mp4")
    cmd = [os.path.join(NODE_BIN, "npx"), "remotion", "render",
           "src/index.tsx", "Explainer", tmp_video,
           "--props", props_path, "--width", str(sp["width"]),
           "--height", str(sp["height"]), "--fps", str(OMR._fps_int(sp["fps"])),
           "--codec", "h264", "--crf", "23", "--x264-preset", "veryfast",
           "--concurrency", str(OMR.RENDER_CONCURRENCY), "--overwrite"]
    code, out = subprocess_run(cmd)
    if code != 0 or not os.path.isfile(tmp_video):
        return result(False, f"Remotion 渲染失败：{out[-300:]}")
    _log(f"Remotion 渲染完成，耗时 {time.time()-t0:.1f}s")

    check = OMR.validate(tmp_video, duration, os.path.join(work, "check"), sp)
    if not check["pass"]:
        return result(False, "验收未通过：" + "、".join(check.get("reasons") or []),
                      check=check, duration=duration)
    os.makedirs(os.path.dirname(video_path), exist_ok=True)
    shutil.copyfile(tmp_video, video_path)
    with open(os.path.join(work, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"props": props, "check": check, "duration": duration,
                   "images": [str(x) for x in images[:n]]}, f, ensure_ascii=False, indent=2)
    _log(f"出片完成 {duration:.1f}s，总耗时 {time.time()-t0:.1f}s，验收通过")
    return result(True, check=check, duration=duration)


def subprocess_run(cmd):
    env = OMR._clean_env()
    try:
        p = subprocess.run(cmd, cwd=REMOTION_DIR, env=env,
                           capture_output=True, timeout=OMR.RENDER_TIMEOUT)
        return p.returncode, (p.stderr or b"").decode("utf-8", "replace")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"
