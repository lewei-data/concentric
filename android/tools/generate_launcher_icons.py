"""生成「同心圆」启动图标的各密度 PNG 兜底 + 预览图。

与 res/drawable/ic_launcher_*.xml（adaptive icon）保持同一套几何参数：
108dp canvas，中心 (54,54)，三环半径 31/23.5/16，中心盘 9.5，
中心声波条、右上 Agent 节点。PNG 里把图案放大 1.15 倍以填满传统图标。

用法：python tools/generate_launcher_icons.py
依赖：Pillow（仅构建期使用，App 本身零第三方依赖）
"""
import os
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "app", "src", "main", "res")
DIST = os.path.join(ROOT, "dist")

TEAL_TOP = (20, 183, 163)
TEAL_BOTTOM = (1, 70, 63)
CORE_TEAL = (0, 108, 99)
MONO_BG = (231, 240, 237)
MONO_INK = (22, 58, 54)
K = 1.15  # legacy PNG 的图案放大系数


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rgba(rgb, alpha):
    return (rgb[0], rgb[1], rgb[2], alpha)


def render(size, shape="square", monochrome=False, ss=4):
    """shape: square(圆角方) / circle(圆) / raw(不带遮罩，用于预览)"""
    w = size * ss
    cx = cy = w / 2.0

    def x(v):
        return cx + (v - 54) / 108.0 * w * K

    def y(v):
        return cy + (v - 54) / 108.0 * w * K

    def r(v):
        return v / 108.0 * w * K

    def px(v):
        return max(1, int(round(r(v))))

    # --- 背景：teal 渐变 + 左上柔光 ---
    bg = Image.new("RGBA", (w, w))
    d = ImageDraw.Draw(bg)
    top, bottom = (MONO_BG, MONO_BG) if monochrome else (TEAL_TOP, TEAL_BOTTOM)
    for row in range(w):
        d.line([(0, row), (w, row)], fill=rgba(lerp(top, bottom, row / max(1, w - 1)), 255))
    if not monochrome:
        glow = Image.new("RGBA", (w, w), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse(
            [-0.45 * w, -0.55 * w, 0.95 * w, 0.55 * w], fill=(255, 255, 255, 46))
        bg = Image.alpha_composite(bg, glow.filter(ImageFilter.GaussianBlur(w / 14)))
    fg = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)

    ink = MONO_INK if monochrome else (255, 255, 255)

    # 底纹：两圈极淡同心圆
    for rad, alpha in ((47, 18), (35, 14)):
        d.ellipse([cx - r(rad), cy - r(rad), cx + r(rad), cy + r(rad)],
                  outline=rgba(ink, alpha), width=px(1))

    # 主同心圆：三层由外到内逐层加粗提亮
    for rad, width, alpha in ((31, 2.6, 61), (23.5, 4.5, 128), (16, 6, 230)):
        d.ellipse([cx - r(rad), cy - r(rad), cx + r(rad), cy + r(rad)],
                  outline=rgba(ink, alpha if not monochrome else min(255, alpha + 25)),
                  width=px(width))

    # 中心盘
    core = r(9.5)
    d.ellipse([cx - core, cy - core, cx + core, cy + core], fill=rgba(ink, 255))

    # 中心声波（非单色版才有，颜色与背景 teal 呼应）
    if not monochrome:
        bars = [(50, 52, 56), (53.2, 49.4, 58.6), (56.4, 51, 57), (59.6, 52.8, 55.2)]
        bw = px(1.8)
        for bx, y0, y1 in bars:
            x0, ya, yb = x(bx), y(y0), y(y1)
            d.line([(x0, ya), (x0, yb)], fill=rgba(CORE_TEAL, 255), width=bw)
            cap = bw / 2.0
            d.ellipse([x0 - cap, ya - cap, x0 + cap, ya + cap], fill=rgba(CORE_TEAL, 255))
            d.ellipse([x0 - cap, yb - cap, x0 + cap, yb + cap], fill=rgba(CORE_TEAL, 255))

    # 右上 Agent 在线节点
    sx, sy, sr = x(76.8), y(34), r(2.8)
    d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=rgba(ink, 204))

    img = Image.alpha_composite(bg, fg)
    if shape != "raw":
        mask = Image.new("L", (w, w), 0)
        md = ImageDraw.Draw(mask)
        if shape == "circle":
            md.ellipse([0, 0, w - 1, w - 1], fill=255)
        else:
            md.rounded_rectangle([0, 0, w - 1, w - 1], radius=int(w * 0.22), fill=255)
        img.putalpha(mask)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main():
    densities = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
    for folder, size in densities.items():
        out = os.path.join(RES, "mipmap-" + folder)
        os.makedirs(out, exist_ok=True)
        render(size, "square").save(os.path.join(out, "ic_launcher.png"))
        render(size, "circle").save(os.path.join(out, "ic_launcher_round.png"))
        print("PNG", folder, size, "->", out)

    os.makedirs(DIST, exist_ok=True)
    big = 360
    preview = Image.new("RGB", (1320, 570), (243, 247, 246))
    pd = ImageDraw.Draw(preview)
    # 卡片底
    for i, shape in enumerate(("square", "circle")):
        px0 = 70 + i * 430
        pd.rounded_rectangle([px0 - 22, 58, px0 + big + 22, 58 + big + 44],
                             radius=28, fill=(255, 255, 255))
        icon = render(big, shape, ss=3)
        preview.paste(icon, (px0, 80), icon)
    # 第三张：Android 13+ 主题图标示意
    px0 = 70 + 2 * 430
    pd.rounded_rectangle([px0 - 22, 58, px0 + big + 22, 58 + big + 44],
                         radius=28, fill=(255, 255, 255))
    mono = render(big, "circle", monochrome=True, ss=3)
    preview.paste(mono, (px0, 80), mono)

    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 24)
        small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 18)
    except Exception:
        font = small = ImageFont.load_default()

    labels = ["方形图标", "圆形图标", "主题图标 · Android 13+"]
    for i, label in enumerate(labels):
        px0 = 70 + i * 430
        pd.text((px0 + big / 2, 468), label, fill=(56, 84, 79), font=font, anchor="ma")
    pd.text((660, 532), "CONRING  ·  三层同心圆 = 语音波纹 / 同心圆 Agent",
            fill=(120, 145, 140), font=small, anchor="mm")
    preview_path = os.path.join(DIST, "ic_launcher_preview.png")
    preview.save(preview_path)
    print("PREVIEW ->", preview_path)


if __name__ == "__main__":
    main()
