#!/bin/sh
# MCP-TTS 启动脚本：清掉外部库路径干扰，再交给 CosyVoice 的 venv 运行。
unset LD_LIBRARY_PATH
export PYTHONPATH=/opt/sefs/CosyVoice/third_party/Matcha-TTS${PYTHONPATH:+:$PYTHONPATH}
# 线程数必须和 server.py 的 TTS_THREADS 一致：之前这里写 64，子进程模型只跑 16~24 线程，
# 多出来的 OpenMP 池全是空转（实测每个 worker 280 个线程、6 个 worker 共 1600+ 线程），
# 既抢不到延迟收益，又挤占并发请求的核。
export OMP_NUM_THREADS=${TTS_THREADS:-16}
export MKL_NUM_THREADS=${TTS_THREADS:-16}
export OPENBLAS_NUM_THREADS=${TTS_THREADS:-16}
# 代理模式：设了就转发到 media-engine，本进程不再加载模型（避免同一模型加载两遍）。
# 想回滚到"9101 自己推理"只需注释掉下面这一行并重启 mcp-tts-http。
export MEDIA_ENGINE_URL=${MEDIA_ENGINE_URL:-http://192.168.21.206:9200}
exec /opt/sefs/CosyVoice/venv/bin/python /opt/sefs/opc-workbench/mcp-tts/server.py "$@"
