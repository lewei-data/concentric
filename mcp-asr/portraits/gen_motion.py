#!/usr/bin/env python
"""员工肖像微动片段生成（AnimateDiff v1.5.2 运动模块 + SD1.5 / Realistic Vision V5.1，纯 CPU）。

思路：拿 gen_portraits.py 已经生成好的写实肖像当"首帧锚定"，走图生视频（video2video），
让运动模块只负责注入呼吸/点头/转头这类微动，避免文生视频重新生成导致"换人"。

输出：prototype/assets/motion/<id>__<action>/frame_%03d.png
      prototype/assets/motion/raw/<id>__<action>.mp4（8fps 原始帧率）
      prototype/assets/motion/smooth/<id>__<action>.mp4（插帧 + 降速，供 Remotion 使用）

用法：
  gen_motion.py --ids boss --actions idle --benchmark   # 单片段计时
  gen_motion.py --ids boss ceo --actions idle nod       # 指定人物 × 指定动作
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

import torch
from diffusers import (
    AnimateDiffVideoToVideoPipeline,
    EulerDiscreteScheduler,
    MotionAdapter,
)
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from gen_portraits import EMPLOYEES, NEGATIVE, prompt_for  # noqa: E402

MODEL_DIR = os.environ.get("SD_MODEL_DIR", "/opt/sefs/models/Realistic_Vision_V5.1")
MOTION_DIR = os.environ.get(
    "MOTION_ADAPTER_DIR", "/opt/sefs/models/animatediff-motion-adapter-v1-5-2")
PORTRAIT_DIR = os.environ.get(
    "PORTRAIT_DIR", "/home/boray/opc-workbench/prototype/assets/portraits")
OUT_DIR = os.environ.get(
    "MOTION_OUT_DIR", "/home/boray/opc-workbench/prototype/assets/motion")

# 动作预设：提示词（放在最前面，避免被 CLIP 77 token 截断）+ 去噪强度
ACTIONS = {
    "idle": ("subtle breathing, shoulders rising and falling slightly", 0.35),
    "nod": ("slow gentle nod, head dipping slightly and returning", 0.45),
    "turn": ("slowly turning head a few degrees to the left and back", 0.45),
    "smile": ("warm smile slowly appearing, natural eye blink", 0.45),
    "lean": ("leaning slightly forward toward the camera", 0.45),
}


def build_prompt(e, action):
    # 动作词放在最前，CLIP 只能吃 77 token，否则动作描述会被裁掉
    return "%s, %s" % (ACTIONS[action][0], prompt_for(e))


def load_pipe():
    t0 = time.time()
    adapter = MotionAdapter.from_pretrained(MOTION_DIR, torch_dtype=torch.float32)
    pipe = AnimateDiffVideoToVideoPipeline.from_pretrained(
        MODEL_DIR, motion_adapter=adapter, torch_dtype=torch.float32,
        safety_checker=None, requires_safety_checker=False)
    pipe.scheduler = EulerDiscreteScheduler.from_config(
        pipe.scheduler.config, beta_schedule="linear",
        clip_sample=False, timestep_spacing="linspace", steps_offset=1)
    pipe.set_progress_bar_config(disable=False)
    print("[load] %.1fs" % (time.time() - t0), flush=True)
    return pipe


def export(frames, stem, raw_fps, smooth_fps, slow):
    raw_dir = os.path.join(OUT_DIR, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    frame_dir = os.path.join(OUT_DIR, stem)
    os.makedirs(frame_dir, exist_ok=True)
    for i, img in enumerate(frames):
        img.save(os.path.join(frame_dir, "frame_%03d.png" % i))

    w, h = frames[0].size
    raw = os.path.join(raw_dir, stem + ".mp4")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-framerate", str(raw_fps), "-i", os.path.join(frame_dir, "frame_%03d.png"),
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "15", raw]
    subprocess.run(cmd, check=True)

    smooth_dir = os.path.join(OUT_DIR, "smooth")
    os.makedirs(smooth_dir, exist_ok=True)
    smooth = os.path.join(smooth_dir, stem + ".mp4")
    # minterpolate 补帧到目标帧率，再用 setpts 放慢，输出更顺滑的慢动作
    vf = ("setpts=%s*PTS,minterpolate=fps=%d:mi_mode=mci:mc_mode=aobmc:vsbmc=1,"
          "scale=trunc(iw/2)*2:trunc(ih/2)*2" % (slow, smooth_fps))
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", raw, "-vf", vf, "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-crf", "16", "-movflags", "+faststart",
                    smooth], check=True)
    return raw, smooth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="+", required=True)
    ap.add_argument("--actions", nargs="+", default=["idle"])
    ap.add_argument("--frames", type=int, default=12)
    ap.add_argument("--steps", type=int, default=12)
    ap.add_argument("--size", type=int, default=448, help="正方形边长，AnimateDiff v1.5.2 原生 512")
    ap.add_argument("--cfg", type=float, default=7.0)
    ap.add_argument("--raw-fps", type=int, default=8)
    ap.add_argument("--smooth-fps", type=int, default=30)
    ap.add_argument("--slow", type=float, default=2.0, help="慢放倍数，2.0 → 12 帧约 3 秒")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--benchmark", action="store_true")
    args = ap.parse_args()

    if args.threads > 0:
        torch.set_num_threads(args.threads)
    os.makedirs(OUT_DIR, exist_ok=True)

    emps = json.load(open(EMPLOYEES, encoding="utf-8"))["employees"]
    by_id = {e["id"]: e for e in emps}
    missing = [i for i in args.ids if i not in by_id]
    if missing:
        raise SystemExit("employees.json 中没有：%s" % missing)
    bad = [a for a in args.actions if a not in ACTIONS]
    if bad:
        raise SystemExit("未知动作：%s（可选 %s）" % (bad, list(ACTIONS)))

    pipe = load_pipe()
    for eid in args.ids:
        src = os.path.join(PORTRAIT_DIR, eid + ".png")
        if not os.path.exists(src):
            print("[skip] %s 缺少肖像 %s" % (eid, src), flush=True)
            continue
        base = Image.open(src).convert("RGB")
        # 肖像 640x832，等比裁成正方形再做缩放，避免拉伸变形
        s = min(base.size)
        left = (base.width - s) // 2
        top = int((base.height - s) * 0.35)  # 略微偏上，保留头部
        base = base.crop((left, top, left + s, top + s)).resize(
            (args.size, args.size), Image.LANCZOS)
        for action in args.actions:
            stem = "%s__%s" % (eid, action)
            smooth = os.path.join(OUT_DIR, "smooth", stem + ".mp4")
            if os.path.exists(smooth) and not args.overwrite:
                print("[skip] %s 已存在" % stem, flush=True)
                continue
            seed = int(hashlib.md5(stem.encode()).hexdigest()[:8], 16) % 2**31
            g = torch.Generator("cpu").manual_seed(seed)
            strength = ACTIONS[action][1] if not args.benchmark else 0.45
            t = time.time()
            res = pipe(
                video=[base] * args.frames,
                prompt=build_prompt(by_id[eid], action),
                negative_prompt=NEGATIVE,
                num_inference_steps=args.steps,
                guidance_scale=args.cfg,
                strength=strength,
                generator=g,
            )
            frames = getattr(res, "frames", None) or getattr(res, "images")
            frames = frames[0] if isinstance(frames[0], list) else frames
            frames = [f if isinstance(f, Image.Image) else Image.fromarray(f)
                      for f in frames]
            dt = time.time() - t
            raw, sm = export(frames, stem, args.raw_fps,
                             args.smooth_fps, args.slow)
            print("[ok] %s  %.1fs  seed=%d  strength=%.2f\n     %s" % (
                stem, dt, seed, strength, build_prompt(by_id[eid], action)), flush=True)
            if args.benchmark:
                print("[bench] 单片段 %.1fs，据此估算 %d 个片段约 %.0f 分钟" % (
                    dt, len(args.ids) * len(args.actions),
                    dt * len(args.ids) * len(args.actions) / 60), flush=True)
                return


if __name__ == "__main__":
    main()
