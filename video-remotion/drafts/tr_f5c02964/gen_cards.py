#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tr_f5c02964 备料：静帧底版 + 字卡排版（仅排版，不改文案一字）。
不渲染视频、不出 mp4。输出：drafts/tr_f5c02964/stills/*
"""
import json, os, hashlib
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
MAIN = (0x7C, 0x3A, 0xED)
FONT = "public/fonts/NotoSansSC-Regular.otf"
OUT = "drafts/tr_f5c02964/stills"
os.makedirs(OUT, exist_ok=True)

CARDS = [
    ("shot1", 1, "它只写方案"),
    ("shot1", 2, "动手那一步留给你"),
    ("shot2", 3, "方案单可出"),
    ("shot2", 4, "改动必须回客户签"),
    ("shot3", 5, "建议，确认，执行，留痕"),
    ("shot3", 6, "四段卡，一步一张"),
    ("shot4", 7, "留痕按时间戳复算"),
    ("shot4", 8, "同种子只复现参数"),
]
SHOTS = {"shot1": 330, "shot2": 345, "shot3": 345, "shot4": 330}
SHOT_TINT = {"shot1": (0.11, 0.44), "shot2": (0.14, 0.48), "shot3": (0.17, 0.50), "shot4": (0.12, 0.46)}


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def make_plate(shot):
    """竖屏静帧底版：深色纵向渐变 + 同心圆环（品牌母题）+ 径向暗角。纯 PIL 实现。"""
    from PIL import ImageChops
    alpha, cy = SHOT_TINT[shot]
    top, bot = (0x0D, 0x09, 0x16), (0x19, 0x11, 0x29)
    base = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(base)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=lerp(top, bot, y / (H - 1)))
    # 同心圆环（母题，不上字）
    rings = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rings)
    cx, cyy = W // 2, int(H * cy)
    for i, r in enumerate((230, 355, 500, 660, 830)):
        a = int(255 * max(0.03, alpha - i * 0.018))
        rd.ellipse([cx - r, cyy - r, cx + r, cyy + r], outline=MAIN + (a,), width=3)
    base = Image.alpha_composite(base.convert("RGBA"), rings).convert("RGB")
    # 径向暗角：中心 0%，四角约 42%（低分辨率算掩膜后放大，避免逐像素过慢）
    sw, sh = 216, 384
    mask = Image.new("L", (sw, sh))
    px = []
    import math
    for j in range(sh):
        gy = (j + 0.5 - sh * 0.5) / (sh * 0.5)
        for i in range(sw):
            gx = (i + 0.5 - sw * 0.5) / (sw * 0.5)
            rad = min(1.0, math.hypot(gx, gy) / math.sqrt(2.0))
            px.append(int(255 * (1.0 - (rad ** 1.7) * 0.42)))
    mask.putdata(px)
    mask = mask.resize((W, H), Image.BILINEAR)
    base = ImageChops.multiply(base, Image.merge("RGB", (mask, mask, mask)))
    return base


def wrap(text, font_size, max_w):
    """仅在逗号处折行（text 内容、字序不变）；折不开再整体缩号。"""
    f = ImageFont.truetype(FONT, font_size)
    if f.getlength(text) <= max_w:
        return [text], font_size
    if "，" in text:
        parts = text.split("，")
        parts = [p + "，" for p in parts[:-1]] + [parts[-1]]
        mid = (len(parts) + 1) // 2
        l1, l2 = "".join(parts[:mid]), "".join(parts[mid:])
        f2 = ImageFont.truetype(FONT, font_size)
        if max(f2.getlength(l1), f2.getlength(l2)) <= max_w:
            return [l1, l2], font_size
    size = font_size
    while size > 48 and f.size and ImageFont.truetype(FONT, size).getlength(text) > max_w:
        size -= 4
    return [text], size


def make_card(shot, n, text):
    SIDE, FONT_SIZE, MAXW = 108, 96, W - 2 * 108  # 安全边距 108px
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lines, size = wrap(text, FONT_SIZE, MAXW)
    font = ImageFont.truetype(FONT, size)
    lh = int(size * 1.38)
    block = lh * len(lines)
    y0 = (H - block) // 2
    for i, ln in enumerate(lines):
        lw = font.getlength(ln)
        d.text(((W - lw) / 2, y0 + i * lh), ln, font=font, fill=(255, 255, 255, 255))
    # 主色装饰短线（无字）
    ry = y0 + block + 46
    d.rectangle([W // 2 - 60, ry, W // 2 + 60, ry + 6], fill=MAIN + (255,))
    widths = [round(font.getlength(l), 1) for l in lines]
    return img, {"lines": lines, "font_px": size, "line_px_width": widths,
                 "text_onscreen": "".join(lines)}


plates = {s: make_plate(s) for s in SHOTS}
for s, p in plates.items():
    p.save(f"{OUT}/plate-{s}.png")

cards = []
for shot, n, text in CARDS:
    ov, meta = make_card(shot, n, text)
    ov.save(f"{OUT}/card-{n:02d}-overlay.png")
    comp = Image.alpha_composite(plates[shot].convert("RGBA"), ov).convert("RGB")
    comp.save(f"{OUT}/preview-{n:02d}.png")
    assert meta["text_onscreen"] == text, (meta["text_onscreen"], text)
    cards.append({"no": n, "shot": shot, "text": text, **meta,
                  "net_chars": len([c for c in text if c not in "，。、；：！？"]),
                  "bytes_found": os.path.getsize(f"{OUT}/card-{n:02d}-overlay.png")})

# 帧位分配（30fps，总 1350 帧＝45.0s），镜内二卡按帧整数切分
f0, timeline = 0, []
for shot, frames in SHOTS.items():
    half = frames // 2
    spans = [half, frames - half]
    for i in range(2):
        n = len(timeline) + 1
        timeline.append({"no": n, "shot": shot, "from": f0, "to": f0 + spans[i] - 1,
                         "frames": spans[i], "sec": round(spans[i] / 30, 3)})
        f0 += spans[i]

for c in cards:
    t = timeline[c["no"] - 1]
    c.update({"from_frame": t["from"], "to_frame": t["to"], "frames": t["frames"], "sec": t["sec"]})

doc = {
    "trace": "tr_f5c02964", "candidate": "tongxinyuan-c_e936e121",
    "title": "它只写方案，动手那一步留给你",
    "spec": {"width": W, "height": H, "fps": 30, "total_frames": f0, "duration_sec": round(f0 / 30, 3),
             "codec": "h264", "pix_fmt_final": "yuv420p", "profile": "high", "level": "4.1",
             "audio": "48kHz 立体声 双声道 (aac 192k)", "main_color": "#7C3AED",
             "safe_margin_px": 108, "form": "字卡＋静帧·无旁白", "narration": False},
    "shots": [{"shot": s, "frames": f, "sec": round(f / 30, 3)} for s, f in SHOTS.items()],
    "cards": cards,
    "shots_total_sec": round(sum(SHOTS.values()) / 30, 3),
}
with open("drafts/tr_f5c02964/cards.json", "w", encoding="utf-8") as fp:
    json.dump(doc, fp, ensure_ascii=False, indent=2)

print("total_frames", f0, "dur", round(f0 / 30, 3))
for c in cards:
    print(c["no"], c["shot"], c["from_frame"], c["to_frame"], c["frames"], c["sec"], c["font_px"], c["lines"], c["bytes_found"])
print("card5 join ok:", "".join([str(x) for x in next(c["lines"] for c in cards if c["no"] == 5)]) == CARDS[4][2])
