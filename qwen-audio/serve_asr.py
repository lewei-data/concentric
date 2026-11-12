#!/usr/bin/env python3
"""Qwen3-ASR 离线识别服务（CPU）。

启动: /opt/sefs/venvs/qwen-asr/bin/python serve_asr.py
接口:
  GET  /health
  POST /v1/asr   {"audio_path": "/abs/path.wav", "language": null}
"""
import json
import os
import time

import torch
from flask import Flask, jsonify, request
from qwen_asr import Qwen3ASRModel

MODEL_DIR = os.environ.get("QWEN_ASR_MODEL", "/opt/sefs/models/qwen-audio/Qwen3-ASR-0.6B")
PORT = int(os.environ.get("QWEN_ASR_PORT", "9312"))
THREADS = int(os.environ.get("QWEN_ASR_THREADS", "64"))

torch.set_num_threads(THREADS)
app = Flask(__name__)
model = None


def get_model():
    global model
    if model is None:
        model = Qwen3ASRModel.from_pretrained(
            MODEL_DIR,
            dtype=torch.float32,
            device_map="cpu",
            max_inference_batch_size=4,
            max_new_tokens=512,
        )
    return model


@app.get("/health")
def health():
    return jsonify({"ok": True, "engine": "qwen3-asr-0.6b", "loaded": model is not None,
                    "threads": THREADS, "model_dir": MODEL_DIR})


@app.post("/v1/asr")
def asr():
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("audio_path")
    if not path or not os.path.isfile(path):
        return jsonify({"error": "audio_path 不存在"}), 400
    language = data.get("language") or None
    t0 = time.time()
    res = get_model().transcribe(audio=path, language=language)[0]
    return jsonify({
        "text": res.text,
        "language": res.language,
        "latency_s": round(time.time() - t0, 2),
        "engine": "qwen3-asr-0.6b",
        "audio_path": path,
    })


if __name__ == "__main__":
    get_model()
    app.run(host="0.0.0.0", port=PORT, threaded=False)
