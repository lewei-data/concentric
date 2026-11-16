"""简化版小样（sample reel）：关键帧 × 对白 × BGM 对齐，全部走本机通道。

定位
----
PM 出完详细分镜脚本后，先在轻量路径上产一个"能听能看"的小样（720×1280，几十秒），
供 CCO/PM/老板快速判断节奏、文案与画面重点；它不是成片——成片仍走 render 阶段
（OpenMontage/Remotion）。

本机通道
--------
- 对白：MCP-TTS / ComfyUI GPT-SoVITS（复用 om_render._tts()，与成片同一音色）。
- 关键帧：ComfyUI(8188/8189, qwen-image) 文生图；不可用时回退 PIL 渐变卡。
- 音乐：素材库 BGM（OPC_BGM_DIR，缺省 deliverables/小秘书宣传片）。
- 合成：本机 ffmpeg（逐镜静帧+对白 → concat → 与 BGM 混音）。

产出
----
`<item_dir>/05-sample-reel.mp4` + `08-sample-reel.json` 清单；阶段把结果写进
`item["sampleReel"]`，前端/评审直接播。
"""
import os
import json
import re
import shutil
import subprocess
import threading
import time

import deliverables as DL

try:
    import om_render as OMR          # TTS 客户端 / ffmpeg / 旁白抽取
except Exception:                     # noqa: BLE001 —— 渲染模块不可用时小样整体降级
    OMR = None

try:
    import illustration as IL         # ComfyUI 文生图 / 场景规格
except Exception:                     # noqa: BLE001
    IL = None

W, H = 720, 1280
FPS = 24
MAX_SEC = int(os.environ.get("OPC_SAMPLE_MAX_SEC", "90"))
BGM_DIR = os.environ.get("OPC_BGM_DIR", "/opt/sefs/opc-workbench/deliverables/小秘书宣传片")
BGM_GAIN = float(os.environ.get("OPC_SAMPLE_BGM_GAIN", "0.16"))
TTS_MAX_CHARS = int(os.environ.get("OPC_SAMPLE_TTS_CHARS", "120"))
IMG_TIMEOUT = int(os.environ.get("OPC_SAMPLE_IMG_TIMEOUT", "90"))
# auto=复用已有插图/合成卡（默认，秒级）；comfyui=强制文生图（CPU 扩散很慢，慎用）
IMAGES_MODE = os.environ.get("OPC_SAMPLE_IMAGES", "auto").strip().lower()
_BUILD_LOCK = threading.Lock()   # TTS 客户端是常驻单例，小样合成串行执行
FONT_CANDIDATES = (
    "/home/boray/.local/share/fonts/NotoSansSC-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _log(msg):
    print(f"[sample-reel] {msg}", flush=True)


def _ff():
    return OMR._ff() if OMR else (shutil.which("ffmpeg") or "ffmpeg")


def _run(cmd, timeout=600):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode == 0, (p.stderr or b"").decode("utf-8", "replace")[-800:]
    except Exception as e:            # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ------------------------------------------------------------------ BGM / 关键帧
def pick_bgm(explicit=""):
    """选一条 BGM：显式路径 > OPC_BGM_DIR 里第一个音频 > 空（无音乐）。"""
    if explicit and os.path.isfile(explicit):
        return explicit
    if os.path.isdir(BGM_DIR):
        for name in sorted(os.listdir(BGM_DIR)):
            if name.lower().endswith((".mp3", ".wav", ".m4a", ".aac")):
                p = os.path.join(BGM_DIR, name)
                if os.path.getsize(p) > 200_000:
                    return p
    return ""


def _font(size):
    from PIL import ImageFont
    for p in FONT_CANDIDATES:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:         # noqa: BLE001
                continue
    return ImageFont.load_default()


def _wrap(text, per_line, max_lines):
    lines, cur = [], ""
    for ch in str(text or ""):
        cur += ch
        if len(cur) >= per_line:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def _card(title, line, no, out_path, palette=((16, 24, 42), (46, 82, 138))):
    """关键帧卡片：竖向渐变 + 画面描述 + 台词 + 镜号（无图也保证有一帧可看）。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), palette[0])
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / max(1, H - 1)
        d.line([(0, y), (W, y)],
               fill=tuple(int(a + (b - a) * t) for a, b in zip(palette[0], palette[1])))
    big, mid, small = _font(46), _font(30), _font(22)
    d.text((48, 64), f"镜 {no}", font=small, fill=(150, 180, 220))
    d.line([(48, 104), (W - 48, 104)], fill=(90, 130, 190), width=2)

    y = 190
    for ln in _wrap(title or "样片关键帧", 9, 5):
        d.text((48, y), ln, font=big, fill=(240, 245, 255))
        y += 60
    if line:
        y += 36
        d.line([(48, y - 18), (120, y - 18)], fill=(120, 170, 220), width=3)
        for ln in _wrap(f"「{line}」", 13, 4):
            d.text((48, y), ln, font=mid, fill=(190, 214, 244))
            y += 42
    d.text((48, H - 84), "OPC 简化小样 · 关键帧示意", font=small, fill=(150, 176, 210))
    img.save(out_path, "PNG")
    return out_path


def _reuse_asset(item_dir, idx):
    """复用 render/插图阶段已生成的关键帧（assets/illustration-sNN.*）。"""
    if not item_dir:
        return ""
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = os.path.join(item_dir, "assets", f"illustration-s{idx:02d}{ext}")
        if os.path.isfile(p):
            return p
    return ""


def _keyframe(shot, item, pid, idx, out_dir, item_dir="", visual=""):
    """一镜一张 720×1280 关键帧。

    优先级：已有插图 > ComfyUI 文生图（OPC_SAMPLE_IMAGES=comfyui）> 合成卡。
    默认 auto 不碰 CPU 扩散（一张要几分钟），保证小样秒级产出。
    """
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, f"frame-{idx:02d}.png")
    raw = os.path.join(out_dir, f"frame-{idx:02d}-raw.png")
    title = visual or shot.get("visual") or shot.get("line") or ""
    prompt, src = "", "card"

    reuse = _reuse_asset(item_dir, idx)
    if reuse and IMAGES_MODE in ("auto", "reuse"):
        src, raw = "reuse", reuse
    elif IMAGES_MODE == "comfyui" and IL:
        try:
            spec = IL.infer_spec(shot, item, pid)
            prompt = ((shot.get("illustration") or {}).get("prompt")
                      or shot.get("image_prompt")
                      or spec.get("prompt")
                      or IL._default_prompt(shot, item, pid))
            urls = getattr(IL, "COMFY_IMAGE_URLS", []) or []
            url = urls[idx % len(urls)] if urls else None
            if IL._comfyui_generate(prompt, raw, url=url, width=1024, height=1024,
                                    timeout=IMG_TIMEOUT):
                src = "comfyui"
        except Exception as e:         # noqa: BLE001
            _log(f"镜{idx} 文生图异常：{type(e).__name__}: {e}")

    if src in ("reuse", "comfyui") and os.path.isfile(raw):
        try:
            from PIL import Image, ImageOps
            with Image.open(raw) as im:
                ImageOps.fit(im.convert("RGB"), (W, H)).save(png, "PNG")
        except Exception as e:         # noqa: BLE001
            _log(f"镜{idx} 关键帧裁剪失败：{e}")
            src = "card"
    if src == "card":
        _card(title, _split_shot(shot)[1], shot.get("no") or idx, png)
    return {"path": png, "engine": src, "prompt": prompt}


# ------------------------------------------------------------------ 对白
_TIME_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[–\-—~至]\s*(\d+(?:\.\d+)?)\s*(?:s|秒)?")
_TIME_SINGLE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:s|秒)")
_ILL_MARK = re.compile(r"插图\s*[:：]\s*([^,，。;；、/|]+)")
_SHOT_NO_HEAD = re.compile(r"^\s*(?:镜|第)?\s*\d+\s*镜?\s*[｜|]?\s*")


def shot_sec(shot):
    """该镜时长：优先分镜文本里的时间码（0-8s / 8s），否则用解析出的 dur。"""
    v = str(shot.get("visual") or "")
    m = _TIME_RANGE.search(v)
    if m:
        return max(1.0, float(m.group(2)) - float(m.group(1)))
    m = _TIME_SINGLE.search(v)
    if m:
        return max(1.0, float(m.group(1)))
    return float(shot.get("dur") or 0)


def _clean_visual(visual):
    """去掉镜号/时间码/插图标注，只留画面描述（关键帧卡片与兜底旁白都用它）。"""
    s = _ILL_MARK.sub("", str(visual or ""))
    s = _TIME_RANGE.sub("", s)
    s = _SHOT_NO_HEAD.sub("", s)
    return s.strip(" ｜|，,。;；:：")


def _split_shot(shot):
    """→ (画面描述, 台词, 台词来源)。台词缺省时从「画面：台词」里切后半段。"""
    visual = _clean_visual(shot.get("visual"))
    line = str(shot.get("line") or "").strip()
    if line:
        return visual, line, "line"
    raw = str(shot.get("visual") or "")
    body = _ILL_MARK.sub("", raw)
    body = _TIME_RANGE.sub("", body)
    body = _SHOT_NO_HEAD.sub("", body).strip(" ｜|")
    parts = [p.strip() for p in re.split(r"[：:]", body) if p.strip()]
    if len(parts) >= 2:
        return visual, parts[-1], "split"
    return visual, "", ""


def _narration(shot):
    """要念的文本：分镜台词 > 「画面：台词」后半段 > 空（该镜静音）。"""
    _visual, line, _src = _split_shot(shot)
    if not line:
        return ""
    if OMR:
        try:
            text, _silent = OMR._narration_text(line)
            return (text or "").strip()[:TTS_MAX_CHARS]
        except Exception:              # noqa: BLE001
            pass
    return line[:TTS_MAX_CHARS]


def _speak(text, out_dir, idx):
    """文本 → wav；失败返回 ("", 0.0)，该镜退化为静帧停留。"""
    if not text or not OMR:
        return "", 0.0
    try:
        res = OMR._tts().synthesize(text, out_dir, f"vo-{idx:02d}")
        return res.get("audio_path") or "", float(res.get("duration") or 0)
    except Exception as e:             # noqa: BLE001
        _log(f"镜{idx} 配音失败：{type(e).__name__}: {e}")
        return "", 0.0


# ------------------------------------------------------------------ 合成
def _clip(frame, wav, sec, out_mp4):
    """一镜一段：静帧停留 sec 秒，音轨为对白（无对白则静音）。"""
    cmd = [_ff(), "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", frame]
    if wav and os.path.isfile(wav):
        cmd += ["-i", wav, "-map", "0:v", "-map", "1:a"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-map", "0:v", "-map", "1:a"]
    # apad 把对白音轨补到整镜时长，-t 收口；不能用 -shortest（会把画面截到音频长度）
    cmd += ["-t", f"{sec:.2f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "28", "-pix_fmt", "yuv420p", "-af", "apad",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", out_mp4]
    ok, err = _run(cmd, timeout=300)
    if not ok:
        _log(f"片段合成失败 {os.path.basename(out_mp4)}：{err[-200:]}")
    return ok


def _concat(parts, out_mp4):
    if not parts:
        return False
    lst = os.path.join(os.path.dirname(parts[0]), "clips.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % p.replace("'", "'\\''"))
    ok, err = _run([_ff(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", out_mp4], timeout=300)
    if not ok:
        _log(f"拼接失败：{err[-200:]}")
    return ok


def _mix_bgm(video_in, bgm, out_mp4):
    """对白视频 + BGM 混音（BGM 低音量循环，时长以人声为准）。"""
    if not bgm:
        return False
    flt = (f"[1:a]volume={BGM_GAIN},afade=t=in:st=0:d=1.2[bg];"
           "[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0[a]")
    ok, err = _run([_ff(), "-y", "-v", "error", "-i", video_in,
                    "-stream_loop", "-1", "-i", bgm,
                    "-filter_complex", flt, "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", out_mp4],
                   timeout=300)
    if not ok:
        _log(f"混音失败：{err[-200:]}")
    return ok


def build(item, project="", files=None, bgm="", workdir=None):
    """为一条内容产出简化小样。

    返回 {"ok", "video", "videoUrl", "duration", "shots":[{no,sec,voice,frame}],
          "bgm", "engine", "builtAt", "reason"?}
    """
    if not OMR:
        return {"ok": False, "reason": "渲染模块不可用（om_render 导入失败）"}
    pid = project or item.get("projectId") or ""
    files = files or DL.plan(item["traceId"], pid, item["id"])
    item_dir = DL._local(files["dir"])
    work = workdir or os.path.join(item_dir, "_sample")
    os.makedirs(work, exist_ok=True)
    os.makedirs(item_dir, exist_ok=True)
    out_local = os.path.join(item_dir, "05-sample-reel.mp4")
    out_url = f"{files['dir']}/05-sample-reel.mp4"

    raw_sb = (item.get("storyboard")
              or (item.get("video") or {}).get("storyboard")
              or (item.get("plan") or {}).get("storyboardShots")
              or "")
    shots = DL.normalize_storyboard(raw_sb, item.get("durationSec") or 45)
    if not shots:
        return {"ok": False, "reason": "没有可用分镜，无法出小样"}

    bgm_path = pick_bgm(bgm)
    audio_dir = os.path.join(work, "vo")
    frame_dir = os.path.join(work, "frames")
    os.makedirs(audio_dir, exist_ok=True)
    parts, rows, total = [], [], 0.0
    for i, shot in enumerate(shots, 1):
        if total >= MAX_SEC:
            break
        visual, line, src = _split_shot(shot)
        text = _narration(shot)
        wav, audio_sec = _speak(text, audio_dir, i)
        kf = _keyframe(shot, item, pid, i, frame_dir, item_dir, visual)
        sb_sec = shot_sec(shot)
        sec = max(audio_sec + 0.4, sb_sec, 2.0)
        sec = min(sec, max(1.5, MAX_SEC - total))
        clip = os.path.join(work, f"clip-{i:02d}.mp4")
        if not _clip(kf["path"], wav, sec, clip):
            continue
        parts.append(clip)
        rows.append({"no": shot.get("no") or str(i), "sec": round(sec, 2),
                     "sbSec": round(sb_sec, 2), "voice": bool(wav),
                     "voiceSec": round(audio_sec, 2), "voiceSrc": src,
                     "frame": kf["engine"], "visual": visual[:60], "line": text[:80]})
        total += sec

    if not parts:
        return {"ok": False, "reason": "没有可用片段（合成全部失败）"}
    voice_mp4 = os.path.join(work, "reel-voice.mp4")
    if not _concat(parts, voice_mp4):
        return {"ok": False, "reason": "片段拼接失败"}
    final = voice_mp4
    if bgm_path:
        mixed = os.path.join(work, "reel-mix.mp4")
        if _mix_bgm(voice_mp4, bgm_path, mixed):
            final = mixed
    shutil.copyfile(final, out_local)

    info = OMR._probe(out_local)
    res = {"ok": bool(info.get("has_video") and info.get("has_audio")),
           "video": out_local, "videoUrl": out_url,
           "duration": round(info.get("duration") or total, 2),
           "shots": rows, "bgm": os.path.basename(bgm_path) if bgm_path else "",
           "engine": ("comfyui-qwen" if any(r["frame"] == "comfyui" for r in rows) else "card")
                     + "+tts+ffmpeg",
           "builtAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "maxSec": MAX_SEC}
    if not res["ok"]:
        res["reason"] = "产物缺视频流或音轨"
    try:
        with open(os.path.join(item_dir, "08-sample-reel.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
    except Exception as e:             # noqa: BLE001
        _log(f"清单写入失败：{e}")
    _log(f"{item.get('id')} 小样 {res['duration']}s / {len(rows)} 镜 / {res['engine']}"
         f" → {out_local}")
    return res


def _demo():
    """本地冒烟：用假分镜跑一遍，验证 TTS / 出图 / ffmpeg 三通道。"""
    item = {"id": "demo-item", "traceId": "demo-trace", "projectId": "tongxinyuan",
            "title": "重复的事交给分身", "durationSec": 12,
            "storyboard": [
                {"镜": "1", "画面": "深夜桌面，台灯下的手写清单", "台词": "每天重复的事，先列出来", "秒": 4},
                {"镜": "2", "画面": "分身接手清单，人起身离开", "台词": "能交出去的就别自己扛", "秒": 4},
            ]}
    res = build(item, "tongxinyuan", workdir="/tmp/sample-reel-demo")
    print(json.dumps({k: v for k, v in res.items() if k != "shots"}, ensure_ascii=False)[:800])


if __name__ == "__main__":
    _demo()