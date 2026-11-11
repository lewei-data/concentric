#!/bin/sh
# media-engine 启动脚本（方案 S1）。
# 复用 CosyVoice 的 venv（已装 torch / whisper / fastapi），务必清掉外部库路径。
unset LD_LIBRARY_PATH
export PYTHONPATH=/opt/sefs/CosyVoice/third_party/Matcha-TTS${PYTHONPATH:+:$PYTHONPATH}
export PATH="/home/boray/.local/bin:$PATH"
# 线程数与 server.py 的 TTS_THREADS 保持一致：16 线程之后延迟不再下降，只徒增 CPU 时间
export OMP_NUM_THREADS=${TTS_THREADS:-16}
export MKL_NUM_THREADS=${TTS_THREADS:-16}
export OPENBLAS_NUM_THREADS=${TTS_THREADS:-16}
# MEDIA_PRELOAD=1 时启动即加载模型（默认懒加载，首个请求再加载）
# 并发：9101 已改为代理模式、不再各自加载模型，这里恢复 6 个 worker（app 通话并发回到改造前水平）
export TTS_WORKERS=${TTS_WORKERS:-6}
exec /opt/sefs/CosyVoice/venv/bin/python /opt/sefs/opc-workbench/media-engine/server.py "$@"
