#!/bin/bash
# MCP-Mail 启动包装：清掉宿主被污染的 LD_LIBRARY_PATH，再以无缓冲模式拉起 stdio 服务
unset LD_LIBRARY_PATH
export PYTHONUNBUFFERED=1
exec python3 /opt/sefs/opc-workbench/mcp-mail/server.py "$@"
