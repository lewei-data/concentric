#!/usr/bin/env bash
# 应用 / 校验 hermes dispatch 的「APP 下行可靠投递」补丁。
#
# 用法：
#   ./apply-hermes-dispatch.sh              # 幂等应用（已应用则跳过）
#   ./apply-hermes-dispatch.sh --check      # 只检查是否已应用，不改动
#   ./apply-hermes-dispatch.sh --rollback   # 反向应用（回滚）
#   ./apply-hermes-dispatch.sh --restart    # 应用后重启 dispatchd
#
# 背景：dispatch/ 目录不在 git 管理内，hermes 升级可能整目录覆盖。
# 升级后跑一次 --check，退出码 1 就说明补丁丢了，重新 apply 即可。
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PATCH="$HERE/hermes-dispatch-app-realtime.patch"
TARGET="${DISPATCH_DIR:-/opt/sefs/hermes/dispatch}"
STAMP="$(date +%m%d-%H%M)"

log() { echo "[$(date +%H:%M:%S)] $*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

[ -f "$PATCH" ] || die "找不到 patch：$PATCH"
[ -d "$TARGET" ] || die "找不到目标目录：$TARGET"
cd "$TARGET" || die "无法进入 $TARGET"

applied() {
    grep -q "from outbox import" realtime_ws.py 2>/dev/null \
        && grep -q 'if base == "/messages"' dispatchd.py 2>/dev/null \
        && [ -f outbox.py ]
}

case "${1:-}" in
    --check)
        if applied; then log "补丁已应用（outbox.py + realtime_ws + dispatchd 特征齐全）"; exit 0; fi
        log "补丁未应用或已被升级覆盖"
        exit 1
        ;;
    --rollback)
        for f in realtime_ws.py dispatchd.py outbox.py; do
            [ -f "$f.bak-rollback-$STAMP" ] && continue
            [ -f "$f" ] && cp -a "$f" "$f.bak-rollback-$STAMP"
        done
        patch -p1 -R --batch < "$PATCH" || die "回滚失败"
        log "已回滚；如需重启：sudo systemctl restart dispatchd"
        exit 0
        ;;
esac

if applied; then
    log "已是目标状态，跳过应用"
    exit 0
fi

# 1) 备份（保留最近一份可回滚原件）
for f in realtime_ws.py dispatchd.py; do
    [ -f "$f" ] && cp -a "$f" "$f.bak-$STAMP"
done

# 2) 预检：先 dry-run，避免打了一半
if ! patch -p1 --dry-run --batch < "$PATCH" >/dev/null 2>&1; then
    die "patch 预检失败（源码与基线不一致，可能已被改动）。请先人工比对：diff -u $TARGET/realtime_ws.py 与 patch"
fi

# 3) 应用
patch -p1 --batch < "$PATCH" || die "patch 应用失败，已保留 .bak-$STAMP 可回滚"

# 4) 语法自检，失败立即回滚
if ! python3 -m py_compile outbox.py realtime_ws.py dispatchd.py 2>/dev/null; then
    log "语法检查失败，自动回滚"
    patch -p1 -R --batch < "$PATCH" >/dev/null 2>&1
    die "已回滚"
fi

log "补丁应用完成（备份：*.bak-$STAMP）"

if [ "${1:-}" = "--restart" ]; then
    log "重启 dispatchd（会中断进行中的实时通话）"
    sudo -n systemctl restart dispatchd.service && sleep 4
    systemctl is-active dispatchd.service
fi
log "下一步：python3 $HERE/verify-hermes-dispatch.py"