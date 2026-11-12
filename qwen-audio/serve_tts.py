#!/usr/bin/env python3
"""Qwen3-TTS 离线合成服务（CPU，CustomVoice 音色）。

启动: /opt/sefs/venvs/qwen-tts/bin/python serve_tts.py
接口:
  GET  /health
  POST /v1/tts {"text": "...", "speaker": "Vivian", "language": "Chinese", "instruct": "..."}
"""
import json
import os
import time
import uuid

import torch
import soundfile as sf
from flask import Flask, jsonify, request, send_from_directory
from qwen_tts import Qwen3TTSModel

MODEL_DIR = os.environ.get("QWEN_TTS_MODEL", "/opt/sefs/models/qwen-audio/Qwen3-TTS-12Hz-0.6B-CustomVoice")
WORK = os.environ.get("QWEN_TTS_WORK", "/opt/sefs/opc-workbench/qwen-audio/work")
PORT = int(os.environ.get("QWEN_TTS_PORT", "9311"))
THREADS = int(os.environ.get("QWEN_TTS_THREADS", "64"))
os.makedirs(WORK, exist_ok=True)

torch.set_num_threads(THREADS)
app = Flask(__name__)
model = None


def get_model():
    global model
    if model is None:
        model = Qwen3TTSModel.from_pretrained(MODEL_DIR, device_map="cpu", dtype=torch.float32)
    return model


@app.get("/health")
def health():
    return jsonify({"ok": True, "engine": "qwen3-tts-12hz-0.6b-customvoice", "loaded": model is not None,
                    "threads": THREADS, "model_dir": MODEL_DIR})


@app.get("/audio/<name>")
def audio(name):
    return send_from_directory(WORK, name)


@app.post("/v1/tts")
def tts():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text 不能为空"}), 400
    speaker = data.get("speaker") or "Vivian"
    language = data.get("language") or "Chinese"
    instruct = data.get("instruct") or None

    t0 = time.time()
    wavs, sr = get_model().generate_custom_voice(
        text=text, language=language, speaker=speaker, instruct=instruct
    )
    synth = time.time() - t0
    name = f"qwen_tts_{uuid.uuid4().hex[:10]}.wav"
    sf.write(os.path.join(WORK, name), wavs[0], sr)
    dur = len(wavs[0]) / sr
    return jsonify({
        "audio_path": os.path.join(WORK, name),
        "audio_url": f"http://{request.host}/audio/{name}",
        "sample_rate": sr,
        "duration": round(dur, 2),
        "synth_sec": round(synth, 2),
        "rtf": round(synth / dur, 2),
        "speaker": speaker,
        "engine": "qwen3-tts-12hz-0.6b-customvoice",
    })


if __name__ == "__main__":
    get_model()
    app.run(host="0.0.0.0", port=PORT, threaded=False)
