#!/usr/bin/env python3
"""魔道码农封面：挥墨体（Zhi Mang Xing, SIL OFL）大字 + 墨迹飞白，横竖屏两版。"""
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BRUSH = "/home/boray/.local/share/fonts/ZhiMangXing-Regular.ttf"
SANS = "/home/boray/.local/share/fonts/NotoSansSC-Regular.otf"
OUT = "/mnt/sefs/conring/users/18911789286/obsidian/OPC/交付物/魔道码农"

INK = (8, 12, 24)
PAPER = (245, 240, 228)
GOLD = (232, 193, 96)
MUTED = (150, 158, 174)
BRUSH_WHITE = (238, 242, 250)


def f_brush(size):
    return ImageFont.truetype(BRUSH, size)


def f_sans(size):
    return ImageFont.truetype(SANS, size)


def ink_layer(w, h, seed=7):
    """生成墨迹飞白层：几团不规则墨渍 + 笔触，模糊后合成。"""
    random.seed(seed)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(7):
        cx = random.randint(int(w * 0.05), int(w * 0.95))
        cy = random.randint(int(h * 0.05), int(h * 0.95))
        rx = random.randint(int(w * 0.05), int(w * 0.22))
        ry = random.randint(int(h * 0.03), int(h * 0.12))
        pts = []
        for k in range(28):
            a = 2 * math.pi * k / 28
            r = random.uniform(0.62, 1.18)
            pts.append((cx + rx * r * math.cos(a), cy + ry * r * math.sin(a)))
        d.polygon(pts, fill=(18, 26, 44, random.randint(30, 70)))
    # 飞白：斜向细笔触
    for _ in range(40):
        x = random.randint(0, w)
        y = random.randint(0, h)
        length = random.randint(int(w * 0.02), int(w * 0.12))
        d.line([(x, y), (x + length, y + random.randint(-8, 8))],
               fill=(90, 110, 150, random.randint(8, 26)), width=random.randint(1, 3))
    return layer.filter(ImageFilter.GaussianBlur(radius=18))


def base(w, h, seed):
    img = Image.new("RGB", (w, h), INK)
    d = ImageDraw.Draw(img)
    # 深色渐变
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)],
               fill=(int(8 + 12 * t), int(12 + 18 * t), int(24 + 26 * t)))
    img = Image.alpha_composite(img.convert("RGBA"), ink_layer(w, h, seed))
    return img, ImageDraw.Draw(img)


def draw_brush_lines(d, lines, x, y, size, gap, color=BRUSH_WHITE):
    f = f_brush(size)
    for line in lines:
        d.text((x, y), line, font=f, fill=color)
        y += gap


def draw_meta(d, x, y, w, scale, lines, question, promise):
    fq = f_sans(int(38 * scale))
    fp = f_sans(int(34 * scale))
    fs = f_sans(int(24 * scale))
    d.text((x, y), question, font=fq, fill=GOLD)
    y += int(62 * scale)
    for line in lines:
        d.text((x, y), line, font=fp, fill=BRUSH_WHITE)
        y += int(52 * scale)
    y += int(20 * scale)
    d.text((x, y), promise, font=fs, fill=MUTED)


def make_horizontal(path):
    w, h = 1920, 1080
    img, d = base(w, h, seed=11)
    # 左侧大字
    draw_brush_lines(d, ["万亿市值", "却设限"], 130, 210, 250, 290)
    # 金色竖线
    d.rectangle([1230, 200, 1236, 880], fill=GOLD)
    draw_meta(d, 1290, 230, w, 1.0,
              ["Claude Code  ×  Codex", "一套订阅 · 两个工具"],
              "你用过吗？", "本地代理 · 标准协议 · 实战接入")
    d.text((130, 960), "魔道码农  ·  OpenCode Go 实战", font=f_sans(30), fill=MUTED)
    img.convert("RGB").save(path, quality=95)


def make_vertical(path):
    w, h = 1080, 1920
    img, d = base(w, h, seed=23)
    draw_brush_lines(d, ["万亿市值", "却设限"], 90, 300, 200, 240)
    # 装饰横线
    d.rectangle([92, 930, 988, 936], fill=GOLD)
    draw_meta(d, 96, 1000, w, 1.3,
              ["Claude Code  ×  Codex", "一套订阅 · 两个工具"],
              "你用过吗？", "本地代理 · 标准协议 · 实战接入")
    d.text((96, 1800), "魔道码农  ·  OpenCode Go 实战", font=f_sans(30), fill=MUTED)
    img.convert("RGB").save(path, quality=95)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    hp = os.path.join(OUT, "封面-横屏-1920x1080.png")
    vp = os.path.join(OUT, "封面-竖屏-1080x1920.png")
    make_horizontal(hp)
    make_vertical(vp)
    print(hp)
    print(vp)
