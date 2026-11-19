#!/usr/bin/env python
"""把写实肖像裁成 App 用的头像（512 方形）和面部特写（256）。

肖像 576x768 是上半身构图，直接缩放当头像会显得人很小；这里按检测到的人脸
取一个以脸为中心的正方形，再缩放到目标尺寸。检测不到脸时退回顶部居中裁切。

用法：make_avatars.py [--backup]
"""
import argparse
import json
import os
import shutil

import cv2

ASSETS = "/home/boray/opc-workbench/prototype/assets"
PORTRAITS = os.path.join(ASSETS, "portraits")
EMPLOYEES = "/opt/sefs/opc-workbench/mcp-asr/employees.json"


def face_box(img):
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cas = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cas.detectMultiScale(gray, 1.08, 5, minSize=(int(w * 0.12),) * 2)
    if not len(faces):
        return None
    return max(faces, key=lambda r: r[2] * r[3])


def square_crop(img, box, ratio=2.15):
    h, w = img.shape[:2]
    if box is None:
        side = w
        x0, y0 = 0, 0
    else:
        x, y, fw, fh = box
        side = int(max(fw, fh) * ratio)
        cx = x + fw / 2.0
        cy = y + fh * 0.5 - side * 0.06      # 略微上移，给肩部留白
        x0, y0 = int(cx - side / 2), int(cy - side / 2)
    side = min(side, w, h)
    x0 = max(0, min(x0, w - side))
    y0 = max(0, min(y0, h - side))
    return img[y0:y0 + side, x0:x0 + side]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", action="store_true")
    args = ap.parse_args()
    ids = [e["id"] for e in json.load(open(EMPLOYEES, encoding="utf-8"))["employees"]]
    bdir = os.path.join(ASSETS, "avatars", "_cartoon")
    for d in ("avatars", "faces"):
        os.makedirs(os.path.join(ASSETS, d), exist_ok=True)
    if args.backup:
        os.makedirs(bdir, exist_ok=True)

    for aid in ids:
        src = os.path.join(PORTRAITS, aid + ".png")
        if not os.path.exists(src):
            print("[miss]", aid)
            continue
        img = cv2.imread(src)
        crop = square_crop(img, face_box(img))
        for sub, size in (("avatars", 512), ("faces", 256)):
            out = os.path.join(ASSETS, sub, aid + ".png")
            if args.backup and os.path.exists(out):
                shutil.copy2(out, os.path.join(bdir, aid + ".png"))
            cv2.imwrite(out, cv2.resize(crop, (size, size), interpolation=cv2.INTER_LANCZOS4))
        print("[ok]", aid)


if __name__ == "__main__":
    main()
