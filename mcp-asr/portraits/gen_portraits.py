#!/usr/bin/env python
"""员工写实肖像生成（本地 SD1.5 / Realistic Vision V5.1，纯 CPU）。

从 employees.json 读取性别/年龄/发型/眼镜/西装色/表情，拼成统一的商务半身像提示词，
输出 640x832 PNG 到 prototype/assets/portraits/<id>.png，供后续数字人驱动使用。

用法：
  gen_portraits.py --only boss          # 单人生成，先看效果
  gen_portraits.py --all                # 全量 19 人
"""
import argparse
import hashlib
import json
import os
import time

import torch
from diffusers import AutoPipelineForText2Image, DPMSolverMultistepScheduler

MODEL_DIR = os.environ.get("SD_MODEL_DIR", "/opt/sefs/models/Realistic_Vision_V5.1")
EMPLOYEES = os.environ.get("ASR_PROFILES", "/opt/sefs/opc-workbench/mcp-asr/employees.json")
OUT_DIR = os.environ.get(
    "PORTRAIT_DIR", "/home/boray/opc-workbench/prototype/assets/portraits")

NEGATIVE = (
    "(deformed iris, deformed pupils, semi-realistic, cgi, 3d, render, sketch, cartoon, "
    "drawing, anime:1.4), text, close up, cropped, out of frame, worst quality, low quality, "
    "jpeg artifacts, ugly, duplicate, morbid, mutilated, extra fingers, mutated hands, "
    "poorly drawn hands, poorly drawn face, mutation, deformed, blurry, dehydrated, "
    "bad anatomy, bad proportions, extra limbs, cloned face, disfigured, gross proportions, "
    "malformed limbs, missing arms, missing legs, extra arms, extra legs, fused fingers, "
    "too many fingers, long neck, logo, watermark, signature, "
    "midriff, crop top, navel, bare shoulders, undershirt"
)

# 个别形象需要额外约束（默认提示词跑偏时用）
EXTRA = {
    "manual_scout": "modest conservative business attire, blazer buttoned up, no exposed midriff",
    "release_ops": "head facing forward, neutral posture, arms relaxed at sides",
    "reviewer": "waist up shot, whole head visible with headroom above the hair, zoomed out",
}

HAIR = {
    "short": "short neat hair",
    "long": "long straight hair",
    "bun": "hair tied in a neat bun",
    "curly": "shoulder-length curly hair",
}

EXPR = {
    "serious": "calm serious expression, mouth closed, looking straight at the camera",
    "calm": "gentle calm expression with a faint smile, looking straight at the camera",
    "cheerful": "warm friendly smile, looking straight at the camera",
}

# 西装主色 → 人类可读颜色（避免模型把十六进制当噪声）
SUIT = {
    "#1F2937": "dark charcoal", "#4A1D33": "deep plum", "#1E3A5F": "navy blue",
    "#7C2D5A": "deep magenta", "#134E4A": "dark teal", "#243B55": "navy",
    "#7C3A1D": "warm brown", "#3B2E6B": "deep purple", "#6B2E12": "dark brown",
    "#312E81": "indigo", "#0F172A": "charcoal black", "#1C1917": "black",
    "#4C1D95": "purple", "#111827": "near black", "#0C4A6E": "dark blue",
    "#365314": "olive green", "#292524": "dark brown",
}


def prompt_for(e):
    sex = "man" if e.get("gender") == "male" else "woman"
    parts = [
        "RAW photo, %d year old East Asian %s" % (int(e.get("age", 30)), sex),
        HAIR.get(e.get("hairStyle"), "short neat hair"),
        "(%s suit jacket:1.3), (%s dress shirt:1.1)" % (
            SUIT.get((e.get("suit") or "").upper(), "dark charcoal"),
            "white" if _is_light(e.get("shirt", "#FFFFFF")) else "black"),
    ]
    if e.get("glasses"):
        parts.append("thin metal frame glasses")
    if e["id"] in EXTRA:
        # 必须放在前面：CLIP 只取前 77 token，放末尾会被截断
        parts.append(EXTRA[e["id"]])
    parts += [
        EXPR.get(e.get("emotion"), EXPR["calm"]),
        "front view, shoulders squared to camera, upper body, chest up",
        "plain light gray background",
        "soft studio lighting, 85mm lens, sharp focus, detailed skin",
    ]
    return ", ".join(parts)


def _is_light(hex_color):
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        return True
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0.299 * r + 0.587 * g + 0.114 * b) > 140


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--steps", type=int, default=28)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=832)
    ap.add_argument("--cfg", type=float, default=6.0)
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--seed-offset", type=int, default=0)
    args = ap.parse_args()

    if args.threads > 0:
        torch.set_num_threads(args.threads)
    os.makedirs(OUT_DIR, exist_ok=True)

    emps = json.load(open(EMPLOYEES, encoding="utf-8"))["employees"]
    if args.only:
        emps = [e for e in emps if e["id"] in args.only]
    elif not args.all:
        raise SystemExit("需要 --only <id…> 或 --all")

    t0 = time.time()
    pipe = AutoPipelineForText2Image.from_pretrained(
        MODEL_DIR, torch_dtype=torch.float32, safety_checker=None, requires_safety_checker=False)
    pipe.scheduler = DPMSolverMultistepScheduler.from_config(
        pipe.scheduler.config, use_karras_sigmas=True,
        algorithm_type="dpmsolver++", final_sigmas_type="sigma_min")
    pipe.set_progress_bar_config(disable=False)
    print("[load] %.1fs" % (time.time() - t0), flush=True)

    for e in emps:
        out = os.path.join(OUT_DIR, e["id"] + ".png")
        if os.path.exists(out) and not args.overwrite:
            print("[skip] %s 已存在" % e["id"], flush=True)
            continue
        seed = (int(hashlib.md5(e["id"].encode()).hexdigest()[:8], 16)
                + args.seed_offset) % 2**31
        g = torch.Generator("cpu").manual_seed(seed)
        t = time.time()
        img = pipe(
            prompt=prompt_for(e), negative_prompt=NEGATIVE,
            num_inference_steps=args.steps, guidance_scale=args.cfg,
            width=args.width, height=args.height, generator=g,
        ).images[0]
        img.save(out)
        print("[ok] %s  %.1fs  seed=%d\n      %s" % (
            e["id"], time.time() - t, seed, prompt_for(e)), flush=True)


if __name__ == "__main__":
    main()
