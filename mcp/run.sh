#!/usr/bin/env bash
# OPC MCP 服务（stdio 传输）启动入口。
# 用法：./run.sh            # 由 MCP 客户端以子进程方式拉起
#      ./run.sh --help     # 无需参数，服务从 stdin 读 JSON-RPC
set -euo pipefail
cd "$(dirname "$0")"
# 本机 shell 带有 LD_LIBRARY_PATH 时 python3 的 sqlite3 会加载错库，先清掉
exec env -u LD_LIBRARY_PATH PYTHONPATH="$(pwd)${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m opc_mcp.server "$@"
