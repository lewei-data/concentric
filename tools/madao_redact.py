#!/usr/bin/env python3
"""对渲染用截图副本打码：oc_sk_ 前缀 + 后接字符 → oc_sk_████████。"""
import os
import re

from PIL import Image, ImageDraw, ImageFont

SRC = "/mnt/sefs/conring/users/18911789286/obsidian/OPC/交付物/魔道码农"
DST = "/opt/sefs/OpenMontage/remotion-composer/public/om/madao/screens"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

# 文件名 -> 需要打码的 (y起点比例, y终点比例, x起点比例, x终点比例)
REGIONS = {
    "6.claudecode.png": [(0.34, 0.42, 0.45, 0.95)],
}

for name, regions in REGIONS.items():
    im = Image.open(os.path.join(SRC, name)).convert("RGB")
    d = ImageDraw.Draw(im)
    for y0, y1, x0, x1 in regions:
        box = (int(im.width * x0), int(im.height * y0), int(im.width * x1), int(im.height * y1))
        d.rectangle(box, fill=(244, 233, 208))
        f = ImageFont.truetype(FONT, 14)
        d.text((box[0] + 4, box[1] + 2), "oc_sk_████████  [REDACTED]", font=f, fill=(110, 100, 85))
    out = os.path.join(DST, name)
    im.save(out)
    print(f"redacted {name} -> {out}")
