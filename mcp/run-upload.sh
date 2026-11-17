#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec env -u LD_LIBRARY_PATH OPC_MCP_ROLE=upload \
  PYTHONPATH="$(pwd)${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m opc_mcp.upload "$@"
