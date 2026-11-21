#!/usr/bin/env python3
"""魔道码农短视频：图文卡片生成器（中文烘进图片，零字体依赖）。"""
import argparse
import json
import os
import re

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "/home/boray/.local/share/fonts/NotoSansSC-Regular.otf"
PUBLIC_ROOT = "/opt/sefs/OpenMontage/remotion-composer/public/om/madao"
PROPS_ROOT = "/opt/sefs/OpenMontage/remotion-composer/props/madao"
MD_PATH = "/mnt/sefs/conring/users/18911789286/obsidian/魔道码农-OpenCodeGo实战-短视频脚本.md"

BG_TOP = (11, 16, 32)
BG_BOTTOM = (23, 31, 48)
WHITE = (248, 250, 252)
MUTED = (148, 163, 184)
BLUE = (56, 189, 248)
GREEN = (34, 197, 94)
RED = (239, 68, 68)
GOLD = (250, 204, 21)
FOOTER = "魔道码农 · OpenCode Go 实战"


def font(size):
    return ImageFont.truetype(FONT_PATH, size)


def wrap(text, fnt, max_w, draw):
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if draw.textlength(cur + ch, font=fnt) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def background(w, h):
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        draw.line(
            [(0, y), (w, y)],
            fill=tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)),
        )
    return img, draw


def make_card_v(path, tag, title, body, accent, color, index, total):
    w, h = 1080, 1920
    img, draw = background(w, h)
    pad = int(w * 0.085)
    bar = int(w * 0.009)
    draw.rounded_rectangle([pad, int(h * 0.10), pad + bar, int(h * 0.20)], radius=bar // 2, fill=color)
    f_tag = font(int(w * 0.026))
    f_title = font(int(w * 0.070))
    f_body = font(int(w * 0.037))
    f_accent = font(int(w * 0.041))
    f_footer = font(int(w * 0.020))
    f_num = font(int(w * 0.022))
    y = int(h * 0.115)
    if tag:
        draw.text((pad + int(w * 0.035), y), tag, font=f_tag, fill=color)
        y += int(h * 0.065)
    for line in wrap(title, f_title, w - 2 * pad, draw)[:4]:
        draw.text((pad, y), line, font=f_title, fill=WHITE)
        y += int(h * 0.098)
    y += int(h * 0.035)
    for line in wrap(body, f_body, w - 2 * pad, draw)[:5]:
        draw.text((pad, y), line, font=f_body, fill=MUTED)
        y += int(h * 0.062)
    if accent:
        ay = int(h * 0.74)
        draw.rectangle([pad, ay - 14, w - pad, ay + int(h * 0.09)], fill=(17, 24, 39))
        draw.rectangle([pad, ay - 14, pad + 6, ay + int(h * 0.09)], fill=color)
        for line in wrap(accent, f_accent, w - 2 * pad - 40, draw)[:3]:
            draw.text((pad + 30, ay), line, font=f_accent, fill=color)
            ay += int(h * 0.050)
    draw.line([(pad, int(h * 0.915)), (w - pad, int(h * 0.915))], fill=(51, 65, 85), width=2)
    draw.text((pad, int(h * 0.935)), FOOTER, font=f_footer, fill=MUTED)
    num = f"{index:02d} / {total:02d}"
    draw.text((w - pad - draw.textlength(num, font=f_num), int(h * 0.935)), num, font=f_num, fill=MUTED)
    img.save(path)


def make_card_h(path, tag, title, body, accent, color, index, total):
    w, h = 1920, 1080
    img, draw = background(w, h)
    pad = int(w * 0.055)
    bar = int(w * 0.006)
    draw.rounded_rectangle([pad, int(h * 0.12), pad + bar, int(h * 0.25)], radius=bar // 2, fill=color)
    f_tag = font(34)
    f_title = font(92)
    f_body = font(46)
    f_accent = font(46)
    f_footer = font(28)
    f_num = font(30)
    y = int(h * 0.135)
    if tag:
        draw.text((pad + 40, y), tag[:24], font=f_tag, fill=color)
    y = int(h * 0.21)
    for line in wrap(title, f_title, w - 2 * pad, draw)[:2]:
        draw.text((pad, y), line, font=f_title, fill=WHITE)
        y += 112
    y = int(h * 0.41)
    for line in wrap(body, f_body, w - 2 * pad, draw)[:5]:
        draw.text((pad, y), line, font=f_body, fill=MUTED)
        y += 68
    ay = int(h * 0.79)
    draw.rectangle([pad, ay - 16, w - pad, ay + 116], fill=(17, 24, 39))
    draw.rectangle([pad, ay - 16, pad + 7, ay + 116], fill=color)
    for line in wrap(accent[:72], f_accent, w - 2 * pad - 50, draw)[:2]:
        draw.text((pad + 36, ay), line, font=f_accent, fill=color)
        ay += 58
    draw.line([(pad, int(h * 0.935)), (w - pad, int(h * 0.935))], fill=(51, 65, 85), width=2)
    draw.text((pad, int(h * 0.952)), FOOTER, font=f_footer, fill=MUTED)
    num = f"{index:02d} / {total:02d}"
    draw.text((w - pad - draw.textlength(num, font=f_num), int(h * 0.952)), num, font=f_num, fill=MUTED)
    img.save(path)


E03 = [
    ("", "配置三行\n翻车三次", "魔道码农 · OpenCode Go 实战", "你一定会踩的坑", BLUE),
    ("翻车 01", "profile 没生效", "写好的 profile 放进 profiles/ 子目录，它根本不读", "provider: openai  ← 连错了", RED),
    ("修复 01", "改到正确位置", "这个版本要求 profile 放在用户根目录下", "~/.codex/<名字>.config.toml  ✅", GREEN),
    ("翻车 02", "stdin 卡死", "codex exec \"说句话\"  →  终端卡住不动", "Reading additional input from stdin", RED),
    ("修复 02", "正确姿势", "用管道把提示词喂给 stdin，末尾加一个减号", "printf '...' | codex exec ... -  ✅", GREEN),
    ("翻车 03", "偷偷连了 OpenAI", "WebSocket 重连、502，其实是 profile 没生效走了默认配置", "provider 不是 opencode-go 就是没生效", RED),
    ("判断标准", "只看这一行", "别看别的输出，启动时的这一行就是答案", "provider: opencode-go", GOLD),
    ("", "代理连通", "model: kimi-k3 · 九千多 token · 第一次真实请求成功", "一套模型，两个工具 · 配置见评论区", GREEN),
]

EP_MINUTES = {"E01": 4, "E02": 5, "E03": 6, "E04": 5, "E05": 4, "E06": 5}
SEC_PER_CARD = 20


def parse_episodes(md_path):
    text = open(md_path, encoding="utf-8").read()
    parts = re.split(r"^## (E0\d) 《(.+?)》", text, flags=re.M)
    result = {}
    for i in range(1, len(parts), 3):
        code, title, body = parts[i], parts[i + 1], parts[i + 2]
        m = re.search(r"### 口播逐字稿\s*\n(.*?)(?=\n### 分镜脚本)", body, re.S)
        if not m:
            continue
        chunks = re.split(r"\*\*【(.+?)】\*\*", m.group(1))
        blocks = []
        for j in range(1, len(chunks), 2):
            name = re.sub(r"\s*\d+:\d+(–\d+:\d+)?\s*$", "", chunks[j]).strip()
            raw = chunks[j + 1].strip()
            raw = raw[1:] if raw.startswith('"') else raw
            raw = raw.rsplit('"', 1)[0] if raw.endswith('"') else raw
            raw = re.sub(r"\*\*", "", raw).strip()
            if raw:
                blocks.append((name, raw))
        result[code] = {"title": title, "blocks": blocks}
    return result


def split_sentences(text):
    parts = re.split(r"(?<=[。！？；])", text.replace("\n", ""))
    return [p.strip() for p in parts if p.strip()]


def pick_color(name):
    if any(k in name for k in ("翻车", "坑", "报错", "残缺", "卡死", "现象")):
        return RED
    if any(k in name for k in ("修复", "成功", "结论", "验证", "收尾", "关键")):
        return GREEN
    return BLUE


def build_vertical():
    w, h = 1080, 1920
    img_dir = os.path.join(PUBLIC_ROOT, "e03-promo-v")
    os.makedirs(img_dir, exist_ok=True)
    cuts = []
    for i, (tag, title, body, accent, color) in enumerate(E03, 1):
        src = f"om/madao/e03-promo-v/{i:02d}.png"
        make_card_v(os.path.join(img_dir, f"{i:02d}.png"), tag, title, body, accent, color, i, len(E03))
        cuts.append({
            "id": f"c{i}", "source": src, "type": "scroll_image_scene",
            "naturalSize": {"width": w, "height": h}, "imageMode": "fit", "fitPadding": 0,
            "in_seconds": (i - 1) * 3, "out_seconds": i * 3,
        })
    props = {"cuts": cuts, "captions": [], "overlays": []}
    os.makedirs(PROPS_ROOT, exist_ok=True)
    with open(os.path.join(PROPS_ROOT, "e03-promo-v.json"), "w", encoding="utf-8") as f:
        json.dump(props, f, ensure_ascii=False, indent=2)
    print(f"vertical: {len(cuts)} cards / {len(cuts) * 3}s")


def build_horizontal():
    eps = parse_episodes(MD_PATH)
    out_root = "/opt/sefs/OpenMontage/remotion-composer/out/madao-h"
    os.makedirs(out_root, exist_ok=True)
    for code in sorted(eps):
        ep = eps[code]
        total_cards = EP_MINUTES[code] * 60 // SEC_PER_CARD
        weights = [len(b[1]) for b in ep["blocks"]]
        total_w = sum(weights) or 1
        alloc = [max(1, round(total_cards * x / total_w)) for x in weights]
        while sum(alloc) > total_cards:
            idx = max(range(len(alloc)), key=lambda i: alloc[i])
            alloc[idx] -= 1
        while sum(alloc) < total_cards:
            idx = min(range(len(alloc)), key=lambda i: alloc[i])
            alloc[idx] += 1

        img_dir = os.path.join(out_root, f"{code}-cards")
        os.makedirs(img_dir, exist_ok=True)
        cards, n = [], 1
        for (name, text), count in zip(ep["blocks"], alloc):
            sents = split_sentences(text)
            color = pick_color(name)
            for k in range(count):
                start = k * len(sents) // count
                end = (k + 1) * len(sents) // count
                chunk = sents[start:end] or sents[:1]
                title = name if count == 1 else f"{name} · {k + 1}/{count}"
                body = "".join(chunk)
                if len(body) > 120:
                    body = body[:120] + "…"
                accent = chunk[-1][:70] if chunk else ""
                path = os.path.join(img_dir, f"{n:03d}.png")
                make_card_h(path, f"{code} · {ep['title'][:18]}", title, body, accent, color, n, total_cards)
                cards.append(path)
                n += 1

        concat = os.path.join(out_root, f"{code}.txt")
        with open(concat, "w", encoding="utf-8") as f:
            f.write("ffconcat version 1.0\n")
            for p in cards:
                f.write(f"file '{p}'\nduration {SEC_PER_CARD}\n")
            f.write(f"file '{cards[-1]}'\nduration 0.042\n")
        print(f"{code}: {len(cards)} cards / {len(cards) * SEC_PER_CARD}s -> {concat}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="vertical", choices=["vertical", "horizontal"])
    args = ap.parse_args()
    if args.only == "vertical":
        build_vertical()
    else:
        build_horizontal()
