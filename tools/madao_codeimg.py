#!/usr/bin/env python3
"""把 opencode-proxy.mjs 烘成 1080 宽的长图，供 Remotion 纵向滚动。"""
import os

from PIL import Image, ImageDraw, ImageFont

SRC = "/mnt/sefs/conring/users/18911789286/obsidian/OPC/交付物/魔道码农/opencode-proxy.mjs"
OUT_DIR = "/opt/sefs/OpenMontage/remotion-composer/public/om/madao"
W = 1080
PAD = 46
TOP = 96
LINE_H = 27
FS = 21
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
SANS = "/home/boray/.local/share/fonts/NotoSansSC-Regular.otf"

BG = (13, 18, 30)
HEADER = (10, 14, 24)
GUTTER = (60, 72, 92)
CODE = (198, 214, 232)
COMMENT = (106, 156, 128)
STRING = (226, 176, 116)
KEYWORD = (126, 176, 236)
LINE_HL = (18, 26, 42)
GOLD = (232, 193, 96)

KEYWORDS = {
    "const", "let", "var", "function", "return", "if", "else", "for", "while",
    "await", "async", "new", "class", "import", "export", "from", "try", "catch",
    "throw", "typeof", "of", "in", "do", "switch", "case", "break", "continue",
    "true", "false", "null", "undefined", "this", "void", "yield", "static",
}


def colorize(line):
    s = line.strip()
    if s.startswith("//") or s.startswith("/*") or s.startswith("*"):
        return COMMENT
    for kw in KEYWORDS:
        if s.startswith(kw + " ") or s == kw or (" " + kw + " ") in s:
            return KEYWORD
    if '"' in line or "'" in line or "`" in line:
        return STRING
    return CODE


def main():
    lines = open(SRC, encoding="utf-8", errors="replace").read().splitlines()
    h = TOP + len(lines) * LINE_H + 80
    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, TOP], fill=HEADER)
    f_head = ImageFont.truetype(SANS, 34)
    f_mono = ImageFont.truetype(MONO, FS)
    d.text((PAD, 28), "opencode-proxy.mjs", font=f_head, fill=GOLD)
    d.text((W - PAD - 210, 36), f"// {len(lines)} lines", font=f_mono, fill=GUTTER)
    d.line([(0, TOP - 2), (W, TOP - 2)], fill=(40, 52, 74), width=2)

    gutter_w = 74
    for i, line in enumerate(lines):
        y = TOP + i * LINE_H
        if i % 2 == 1:
            d.rectangle([0, y, W, y + LINE_H], fill=(16, 22, 36))
        d.text((PAD, y + 2), f"{i + 1:03d}", font=f_mono, fill=GUTTER)
        d.line([(PAD + gutter_w, y), (PAD + gutter_w, y + LINE_H)], fill=(32, 42, 60), width=1)
        d.text((PAD + gutter_w + 16, y + 2), line[:110], font=f_mono, fill=colorize(line))

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "opencode-proxy-code.png")
    img.save(path, quality=95)
    print(f"{path}  {W}x{h}  {len(lines)} lines")


if __name__ == "__main__":
    main()
