#!/bin/sh
# MCP-ASR 启动脚本：清掉外部库路径干扰，交给 CosyVoice 的 venv（已装 whisper）运行。
unset LD_LIBRARY_PATH
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-8}
exec /opt/sefs/CosyVoice/venv/bin/python /opt/sefs/opc-workbench/mcp-asr/server.py "$@"
