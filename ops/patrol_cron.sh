#!/bin/bash
# 每小时全量巡检：只在有 FAIL 时通知，避免每天 24 条「一切正常」把告警淹没。
# 与 runbook（自愈）、ui_baseline（前端）互补：这个只读，不改任何状态。
set -u
cd /opt/sefs/opc-workbench || exit 1

LOG=ops/opsctl.log
OUT=$(python3 /opt/sefs/hermes/shared-skills/devops/infra-patrol/scripts/opsctl.py check 2>&1)
RC=$?

# 日志超 2MB 就留最近 5000 行，防止自己变成新的日志问题
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 2097152 ]; then
    tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
{ echo "--- $(date '+%F %T') rc=$RC ---"; echo "$OUT"; } >> "$LOG"

if [ "$RC" -ne 0 ] || echo "$OUT" | grep -q "^FAIL"; then
    DETAIL=$(echo "$OUT" | grep -E "^FAIL|项不可用" | head -3 | sed 's/  */ /g' | tr '\n' ' ')
    python3 -c "import sys;sys.path.insert(0,'prototype');import notify as n;n.notify('warning','patrol',sys.argv[1][:400])" \
        "${DETAIL:-巡检返回非零}" 2>/dev/null || true
fi
exit 0
