#!/usr/bin/env python3
"""魔道码农封面·宋体版：Noto Serif SC Bold（印刷体带笔锋），横竖屏两版。"""
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SERIF = "/home/boray/.local/share/fonts/NotoSerifSC-Bold-Cover.ttf"
SANS = "/home/boray/.local/share/fonts/NotoSansSC-Regular.otf"
OUT = "/mnt/sefs/conring/users/18911789286/obsidian/OPC/交付物/魔道码农"

INK = (8, 12, 24)
WHITE = (245, 248, 252)
GOLD = (232, 193, 96)
MUTED = (145, 155, 175)


def f_serif(size):
    return ImageFont.truetype(SERIF, size)


def f_sans(size):
    return ImageFont.truetype(SANS, size)


def base(w, h, seed):
    random.seed(seed)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=(int(8 + 12 * t), int(12 + 18 * t), int(24 + 26 * t), 255))
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dl = ImageDraw.Draw(layer)
    for _ in range(6):
        cx, cy = random.randint(0, w), random.randint(0, h)
        rx, ry = random.randint(w // 12, w // 5), random.randint(h // 20, h // 8)
        pts = []
        for k in range(24):
            a = 2 * math.pi * k / 24
            r = random.uniform(0.6, 1.2)
            pts.append((cx + rx * r * math.cos(a), cy + ry * r * math.sin(a)))
        dl.polygon(pts, fill=(22, 30, 50, random.randint(25, 55)))
    img = Image.alpha_composite(img, layer.filter(ImageFilter.GaussianBlur(24)))
    return img, ImageDraw.Draw(img)


def big(d, text, x, y, size):
    f = f_serif(size)
    d.text((x, y), text, font=f, fill=WHITE)


def make_horizontal(path):
    w, h = 1920, 1080
    img, d = base(w, h, 31)
    big(d, "万亿市值", 130, 200, 190)
    big(d, "却设限", 130, 430, 190)
    d.rectangle([1230, 200, 1236, 840], fill=GOLD)
    d.text((1290, 240), "你用过吗？", font=f_sans(40), fill=GOLD)
    d.text((1290, 320), "Claude Code  ×  Codex", font=f_sans(48), fill=WHITE)
    d.text((1290, 400), "一套订阅 · 两个工具", font=f_sans(48), fill=WHITE)
    d.text((1290, 500), "本地代理 · 标准协议 · 实战接入", font=f_sans(32), fill=MUTED)
    d.text((130, 960), "魔道码农  ·  OpenCode Go 实战", font=f_sans(30), fill=MUTED)
    img.convert("RGB").save(path, quality=95)


def make_vertical(path):
    w, h = 1080, 1920
    img, d = base(w, h, 41)
    big(d, "万亿市值", 90, 330, 145)
    big(d, "却设限", 90, 540, 145)
    d.rectangle([92, 880, 988, 886], fill=GOLD)
    d.text((96, 960), "你用过吗？", font=f_sans(52), fill=GOLD)
    d.text((96, 1050), "Claude Code  ×  Codex", font=f_sans(56), fill=WHITE)
    d.text((96, 1140), "一套订阅 · 两个工具", font=f_sans(56), fill=WHITE)
    d.text((96, 1250), "本地代理 · 标准协议 · 实战接入", font=f_sans(36), fill=MUTED)
    d.text((96, 1800), "魔道码农  ·  OpenCode Go 实战", font=f_sans(30), fill=MUTED)
    img.convert("RGB").save(path, quality=95)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    hp = os.path.join(OUT, "封面-宋体-横屏-1920x1080.png")
    vp = os.path.join(OUT, "封面-宋体-竖屏-1080x1920.png")
    make_horizontal(hp)
    make_vertical(vp)
    print(hp)
    print(vp)
