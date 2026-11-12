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
# 素材/成片对外地址：沿用 App 与 nginx 现有通路（/conring/asr/assets → assets 目录），
# 与 mcp-asr 保持一致，客户端不用改；视频接口本身另走 /v1/assets。
export ASR_ASSET_URL_BASE=${ASR_ASSET_URL_BASE:-/conring/asr/assets}
export ASR_PUBLIC_BASE=${ASR_PUBLIC_BASE:-https://ai.boraydata.com}
# 监听地址：206 的 21 网段是万兆直连（ens2f1np1 192.168.21.206/24），208 反代固定走这条链路。
# 只绑 21 网段，不再绑 31 网段/回环；本机调用方（插件、mcp-tts）必须用 192.168.21.206:9200。
export MEDIA_HOST=${MEDIA_HOST:-192.168.21.206}
exec /opt/sefs/CosyVoice/venv/bin/python /opt/sefs/opc-workbench/media-engine/server.py "$@"
