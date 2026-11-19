#!/usr/bin/env python
"""照片 + 配音 → 口型对齐的说话视频（Wav2Lip GAN，CPU 推理）。

用法：
  python run_lipsync.py --face photo.png --audio speech.wav --out talk.mp4 \
      --fps 25 --batch 48 [--box x,y,w,h]

输出为无声 mp4（H.264/yuv420p），音轨由调用方合成。
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "code"))

import cv2
import numpy as np
import torch

from wav2lip import Wav2Lip
import audio_mel

IMG_SIZE = 96
MEL_STEP = 16
WEIGHTS = os.path.join(BASE, "weights", "wav2lip_gan.pth")
FFMPEG = os.environ.get("ASR_FFMPEG") or shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


def _load_model():
    model = Wav2Lip()
    ckpt = torch.load(WEIGHTS, map_location="cpu")
    state = ckpt.get("state_dict", ckpt)
    state = {k.replace("module.", "", 1): v for k, v in state.items()}
    model.load_state_dict(state)
    model.eval()
    return model


def _detect_box(img):
    """返回照片里的正方形人脸区域 (x, y, side)。检测失败时用人像构图兜底。"""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=5,
                                     minSize=(int(min(h, w) * 0.10),) * 2)
    if len(faces):
        # 取最靠中心的一张脸
        x, y, fw, fh = max(
            faces, key=lambda r: r[2] * r[3] - 0.5 * abs((r[0] + r[2] / 2) - w / 2))
    else:
        fw = int(w * 0.42)
        fh = int(fw * 1.05)
        x = int(w / 2 - fw / 2)
        y = int(h * 0.24)
    side = int(max(fw, fh) * 1.34)
    cx = x + fw / 2.0
    cy = y + fh * 0.56
    x0 = int(round(cx - side / 2.0))
    y0 = int(round(cy - side / 2.0))
    # 越界时向内平移，保持 side 不变
    x0 = max(0, min(x0, w - side)) if side <= w else 0
    y0 = max(0, min(y0, h - side)) if side <= h else 0
    side = min(side, w, h)
    return x0, y0, side


def _mel_chunks(wav, fps):
    mel = audio_mel.melspectrogram(wav)
    if np.isnan(mel.reshape(-1)).sum() > 0:
        wav = wav + np.random.normal(0, 1e-6, len(wav))
        mel = audio_mel.melspectrogram(wav)
    chunks = []
    step = 80.0 / fps
    i = 0
    while True:
        start = int(i * step)
        if start + MEL_STEP > mel.shape[1]:
            chunks.append(mel[:, mel.shape[1] - MEL_STEP:])
            break
        chunks.append(mel[:, start:start + MEL_STEP])
        i += 1
    return chunks


def _mouth_mask(side):
    """下半张脸的椭圆羽化遮罩：只覆盖口鼻下巴，保留原图眼睛/头发清晰度。"""
    mask = np.zeros((side, side), dtype=np.float32)
    cv2.ellipse(mask, (side // 2, int(side * 0.68)),
                (int(side * 0.30), int(side * 0.24)), 0, 0, 360, 1.0, -1)
    k = max(3, side // 12) | 1
    mask = cv2.GaussianBlur(mask, (k, k), 0)
    return mask[:, :, None]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--face", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--batch", type=int, default=48)
    ap.add_argument("--threads", type=int, default=0)
    args = ap.parse_args()

    if args.threads > 0:
        torch.set_num_threads(args.threads)
    if not os.path.exists(WEIGHTS):
        raise SystemExit("缺少模型权重：%s" % WEIGHTS)

    img = cv2.imread(args.face)
    if img is None:
        raise SystemExit("读不到照片：%s" % args.face)
    h, w = img.shape[:2]
    x0, y0, side = _detect_box(img)
    face = img[y0:y0 + side, x0:x0 + side]
    face_small = cv2.resize(face, (IMG_SIZE, IMG_SIZE), interpolation=cv2.INTER_AREA)

    wav = audio_mel.load_wav(args.audio, 16000)
    if wav.size == 0:
        raise SystemExit("音频为空：%s" % args.audio)
    chunks = _mel_chunks(wav, args.fps)
    n_frames = len(chunks)
    duration = wav.shape[0] / 16000.0

    model = _load_model()
    mask = _mouth_mask(side)

    cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", "%dx%d" % (w, h), "-r", "%.3f" % args.fps, "-i", "-",
           "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", args.out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    started = time.time()
    try:
        for b in range(0, n_frames, args.batch):
            batch = chunks[b:b + args.batch]
            n = len(batch)
            faces = np.repeat(face_small[None], n, axis=0).astype(np.float32) / 255.0
            masked = faces.copy()
            masked[:, IMG_SIZE // 2:] = 0
            inp = np.concatenate((masked, faces), axis=3)
            mel = np.stack(batch)[..., None].astype(np.float32)
            with torch.no_grad():
                pred = model(torch.from_numpy(mel.transpose(0, 3, 1, 2)),
                             torch.from_numpy(inp.transpose(0, 3, 1, 2)))
            pred = (pred.clamp(0, 1).numpy().transpose(0, 2, 3, 1) * 255).astype(np.uint8)
            for p in pred:
                gen = cv2.resize(p, (side, side), interpolation=cv2.INTER_CUBIC)
                mouth = cv2.detailEnhance(gen, sigma_s=10, sigma_r=0.15)
                frame = img.copy()
                roi = frame[y0:y0 + side, x0:x0 + side].astype(np.float32)
                roi = roi * (1 - mask) + mouth.astype(np.float32) * mask
                frame[y0:y0 + side, x0:x0 + side] = np.clip(roi, 0, 255).astype(np.uint8)
                proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
        proc.wait(timeout=120)

    elapsed = time.time() - started
    print("frames=%d duration=%.2fs elapsed=%.1fs speed=%.1ffps box=%d,%d,%d" % (
        n_frames, duration, elapsed, n_frames / max(elapsed, 1e-6), x0, y0, side))
    if proc.returncode != 0:
        raise SystemExit("ffmpeg 写入失败")


if __name__ == "__main__":
    main()
