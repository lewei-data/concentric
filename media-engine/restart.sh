#!/bin/sh
# media-engine 常规重启入口（S2 之后用）
# 用法：sh /opt/sefs/opc-workbench/media-engine/restart.sh
set -e

if pgrep -f "media-engine/server.py" >/dev/null 2>&1; then
  pkill -TERM -f "media-engine/server.py" || true
  i=0
  while pgrep -f "media-engine/server.py" >/dev/null 2>&1 && [ "$i" -lt 20 ]; do
    sleep 1
    i=$((i + 1))
  done
fi

cd /opt/sefs/opc-workbench/media-engine
setsid --fork sh run.sh > engine.log 2>&1 < /dev/null
echo "media-engine restarted"
