#!/bin/sh
# media-engine 重启入口：已托管给 systemd（media-engine.service），优先走 systemctl
# 用法：sh /opt/sefs/opc-workbench/media-engine/restart.sh
set -e

if systemctl is-active --quiet media-engine.service 2>/dev/null; then
  systemctl restart media-engine.service 2>/dev/null || sudo -n systemctl restart media-engine.service
  echo "media-engine restarted (systemd)"
  exit 0
fi

# 回退：unit 不可用时按老方式裸启动（进程组会脱离终端，但可能留下 PPID=1 的 worker 池）
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
echo "media-engine restarted (standalone)"
