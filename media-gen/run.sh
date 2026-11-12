#!/bin/bash
# media-gen 启动脚本：diffusers 0.42 + torch CPU，独占 sd-venv
set -e
cd "$(dirname "$0")"
export PYTHONUNBUFFERED=1
export LD_LIBRARY_PATH=
export OMP_NUM_THREADS="${MEDIA_GEN_THREADS:-0}"
exec /opt/sefs/sd-venv/bin/python server.py
