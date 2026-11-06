#!/usr/bin/env bash
# OPC 一人公司 · AI 员工工作台 —— 后台运行管理脚本（Hermes 内核版 v3）
#
# 用法：
#   ./opc.sh start      # 后台启动（默认 127.0.0.1:8848）
#   ./opc.sh stop       # 停止
#   ./opc.sh restart    # 重启
#   ./opc.sh status     # 状态 + /api/health
#   ./opc.sh logs       # 跟踪日志
#   ./opc.sh verify     # 端到端验收（需服务已启动）
#   ./opc.sh reload     # 热加载 Obsidian 配置（项目配置 + 员工名册），不重启
#   ./opc.sh vault      # 导出公司配置 + 运行时动态到 Obsidian（默认 /opt/sefs/obsidian）
#
# 可用环境变量覆盖：
#   OPC_HOST=127.0.0.1  OPC_PORT=8848  OPC_LOG_DIR=/path  OPC_PYTHON=python3
#   OPC_BASIC_USER / OPC_BASIC_PASS  非回环访问的登录凭据（默认关闭，传值即启用）

unset LD_LIBRARY_PATH   # 本机 env 里的 LD_LIBRARY_PATH 会污染 ls/stat 等系统命令

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$ROOT/prototype/server.py"
HOST="${OPC_HOST:-0.0.0.0}"
PORT="${OPC_PORT:-8848}"
LOG_DIR="${OPC_LOG_DIR:-$ROOT/logs}"
LOG="$LOG_DIR/server.log"
PID_FILE="$LOG_DIR/server.pid"
PY="${OPC_PYTHON:-python3}"
URL="http://$HOST:$PORT"
MAX_LOG=10485760   # 10MB 后启动时切分到 server.log.1

# 默认不启用登录（auth=off）；如需 Basic 鉴权，启动时传入：
#   OPC_BASIC_USER=admin OPC_BASIC_PASS=****** ./opc.sh restart
export OPC_BASIC_USER="${OPC_BASIC_USER:-}"
export OPC_BASIC_PASS="${OPC_BASIC_PASS:-}"

# 0.0.0.0 不能直接用于探测/点击，改用回环地址做健康检查
PROBE_HOST="$HOST"; [ "$HOST" = "0.0.0.0" ] && PROBE_HOST="127.0.0.1"
PROBE_URL="http://$PROBE_HOST:$PORT"

lan_ip() {
  "$PY" -c "import re,subprocess;o=subprocess.run(['ip','-4','-o','addr','show','scope','global'],capture_output=True,text=True).stdout;m=re.search(r'inet (\d+\.\d+\.\d+\.\d+)',o);print(m.group(1) if m else '127.0.0.1')"
}

access_hint() {
  local where
  if [ "$HOST" = "0.0.0.0" ]; then where="http://$(lan_ip):$PORT"; else where="$URL"; fi
  if [ -n "$OPC_BASIC_USER" ]; then
    echo "访问: $where   登录: $OPC_BASIC_USER / $OPC_BASIC_PASS"
  else
    echo "访问: $where   （auth=off，无登录）"
  fi
}

mkdir -p "$LOG_DIR"

alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
pid_of() { [ -f "$PID_FILE" ] && cat "$PID_FILE" 2>/dev/null || true; }

probe() {  # 服务可用则返回 0（启用登录后 /api/health 会返回 401，仍视为存活）
  "$PY" -c "
import sys, urllib.request, urllib.error
try:
    urllib.request.urlopen('$PROBE_URL/api/health', timeout=2)
except urllib.error.HTTPError as e:
    sys.exit(0 if e.code == 401 else 1)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1
}

start() {
  local pid; pid="$(pid_of)"
  if alive "$pid"; then
    echo "已在运行 (pid=$pid)"; access_hint; return 0
  fi
  if probe; then
    echo "端口 $PORT 已被其他进程占用，请先 stop 或改用 OPC_PORT=... 启动"; return 1
  fi
  if [ -f "$LOG" ] && [ "$("$PY" -c "import os;print(os.path.getsize('$LOG'))" 2>/dev/null || echo 0)" -gt "$MAX_LOG" ]; then
    mv "$LOG" "$LOG.1"
  fi
  echo "==== $(date '+%F %T') start host=$HOST port=$PORT ====" >> "$LOG"
  setsid nohup env OPC_QUIET=1 "$PY" "$APP" --host "$HOST" --port "$PORT" >> "$LOG" 2>&1 < /dev/null &
  echo $! > "$PID_FILE"
  for _ in $(seq 1 40); do
    if probe; then
      echo "已启动 (pid=$(pid_of))"
      access_hint
      echo "日志: $LOG"
      return 0
    fi
    sleep 0.25
  done
  echo "启动超时，最近日志："; tail -n 20 "$LOG"; return 1
}

stop() {
  local pid; pid="$(pid_of)"
  if ! alive "$pid"; then
    echo "未在运行"; rm -f "$PID_FILE"; return 0
  fi
  kill "$pid" 2>/dev/null
  local i
  for i in $(seq 1 20); do alive "$pid" || break; sleep 0.25; done
  if alive "$pid"; then kill -9 "$pid" 2>/dev/null; fi
  rm -f "$PID_FILE"
  echo "已停止 (pid=$pid)"
}

status() {
  local pid; pid="$(pid_of)"
  if ! alive "$pid"; then
    echo "未运行"; return 1
  fi
  echo "运行中 pid=$pid"; access_hint
  "$PY" -c "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('$PROBE_URL/api/health')),ensure_ascii=False))" 2>/dev/null
}

reload() {
  "$PY" - "$PROBE_URL" <<'EOF'
import json, sys, urllib.request
url = sys.argv[1] + "/api/config/reload"
req = urllib.request.Request(url, data=b"{}", method="POST",
                             headers={"Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req, timeout=15)
    print(json.dumps(json.load(r), ensure_ascii=False, indent=2))
except urllib.error.HTTPError as e:
    print("reload 失败:", e.code, e.read().decode("utf-8", "ignore"))
    sys.exit(1)
EOF
}

patrol() {
  "$PY" - "$PROBE_URL" <<'EOF'
import json, sys, urllib.request
url = sys.argv[1] + "/api/patrol"
req = urllib.request.Request(url, data=b"{}", method="POST",
                             headers={"Content-Type": "application/json"})
try:
    print(json.dumps(json.load(urllib.request.urlopen(req, timeout=30)), ensure_ascii=False, indent=2))
except Exception as e:
    print("巡检失败:", e)
    sys.exit(1)
EOF
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)  status ;;
  logs)    tail -n 40 -f "$LOG" ;;
  verify)  "$PY" "$ROOT/prototype/verify.py" ;;
  reload)  reload ;;
  patrol)  patrol ;;
  vault)   "$PY" "$ROOT/vault_export.py" ;;
  *)       echo "用法: $0 {start|stop|restart|status|logs|verify|reload|patrol|vault}"; exit 2 ;;
esac
