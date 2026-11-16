#!/bin/bash
# OPC 数据备份（2026-11-16，P0-1）
# DB：全部 opc.db（用户库）+ media_jobs.db + prototype/opc.db + nodes.db + dispatch.db
#     + hermes 各 profile 的 state.db（会话与记忆，丢了等于员工失忆）
# 文件：vault 镜像 + 各用户 vault（排除 downloads 缓存）
# 用法：ops/backup.sh [--db-only|--files-only] [--quiet]
# 产物：$DEST/db/<YYYYMMDD>/*.db、$DEST/vault/、$DEST/users/<id>/obsidian/
# 标记：$DEST/db/.last_success、$DEST/.files_success（供巡检 fresh 协议判新鲜）
# 保留：db 快照 14 天；文件为镜像（--delete 与源 1:1）
set -uo pipefail

ROOT=/opt/sefs/opc-workbench
DEST=${OPC_BACKUP_DIR:-/mnt/sefs/conring/backup}
KEEP_DAYS=${OPC_BACKUP_KEEP_DAYS:-14}
DAY=$(date +%Y%m%d)
STAMP=$(date -Iseconds)
LOGDIR=$ROOT/ops/backups
LOG=$LOGDIR/backup.log
STATUS=$ROOT/ops/backup_status.json
MODE=all
QUIET=0
for a in "$@"; do
  case "$a" in
    --db-only) MODE=db ;;
    --files-only|--vault-only) MODE=files ;;
    --quiet|-q) QUIET=1 ;;
  esac
done

mkdir -p "$LOGDIR" "$DEST/db/$DAY"

say() { [ "$QUIET" = 1 ] || echo "$@"; echo "$(date '+%F %T') $*" >> "$LOG"; }

FAIL=0
DB_OK=0
DB_ERR=""
FILES_OK=null
FILES_ERR=""

backup_sqlite() {
  local src=$1 name=$2
  [ -f "$src" ] || { say "SKIP $name（不存在）"; return 0; }
  local out="$DEST/db/$DAY/$name"
  if sqlite3 "$src" ".timeout 8000" ".backup '$out'" 2>>"$LOG"; then
    local ic
    ic=$(sqlite3 "$out" "PRAGMA integrity_check" 2>/dev/null | head -1)
    if [ "$ic" = "ok" ]; then
      DB_OK=$((DB_OK + 1))
      say "OK   $name  $(du -h "$out" | cut -f1)  integrity=ok"
    else
      FAIL=1; DB_ERR="$DB_ERR $name:integrity=$ic"
      say "FAIL $name integrity=$ic"
    fi
  else
    FAIL=1; DB_ERR="$DB_ERR $name:backup-failed"
    say "FAIL $name 备份失败"
  fi
}

if [ "$MODE" = all ] || [ "$MODE" = db ]; then
  say "=== DB 备份开始 → $DEST/db/$DAY ==="
  for f in /mnt/sefs/conring/users/*/opc.db; do
    [ -f "$f" ] || continue
    uid=$(basename "$(dirname "$f")")
    backup_sqlite "$f" "opc-$uid.db"
  done
  for f in /mnt/sefs/conring/users/*/media_jobs.db; do
    [ -f "$f" ] || continue
    uid=$(basename "$(dirname "$f")")
    backup_sqlite "$f" "media-jobs-$uid.db"
  done
  backup_sqlite "$ROOT/prototype/opc.db" "opc-cli.db"
  backup_sqlite "$ROOT/ops/nodes.db" "nodes.db"
  backup_sqlite "/opt/sefs/hermes/dispatch/dispatch.db" "dispatch.db"
  for f in /opt/sefs/hermes/profiles/*/state.db; do
    [ -f "$f" ] || continue
    pid=$(basename "$(dirname "$f")")
    backup_sqlite "$f" "hermes-$pid-state.db"
  done
  [ "$FAIL" -eq 0 ] && date -Iseconds > "$DEST/db/.last_success"
fi

if [ "$MODE" = all ] || [ "$MODE" = files ]; then
  if ! command -v rsync >/dev/null 2>&1; then
    FAIL=1; FILES_OK=false; FILES_ERR="rsync missing"
    say "FAIL 未安装 rsync"
  else
    say "=== vault 镜像同步开始 ==="
    mkdir -p "$DEST/vault"
    if rsync -a --delete --exclude '.trash/' --exclude '.DS_Store' \
        /opt/sefs/obsidian/ "$DEST/vault/" 2>>"$LOG"; then
      say "OK   vault 镜像 $(du -sh "$DEST/vault" 2>/dev/null | cut -f1)"
    else
      FAIL=1; FILES_ERR="$FILES_ERR vault"
      say "FAIL vault 镜像失败"
    fi
    for d in /mnt/sefs/conring/users/*/; do
      uid=$(basename "$d")
      [ -d "$d/obsidian" ] || continue
      mkdir -p "$DEST/users/$uid"
      if rsync -a --delete --exclude '.trash/' "$d/obsidian/" "$DEST/users/$uid/obsidian/" 2>>"$LOG"; then
        say "OK   用户 vault $uid $(du -sh "$DEST/users/$uid/obsidian" 2>/dev/null | cut -f1)"
      else
        FAIL=1; FILES_ERR="$FILES_ERR user-$uid"
        say "FAIL 用户 vault $uid"
      fi
    done
    if [ -f /opt/sefs/hermes/config.yaml ]; then
      mkdir -p "$DEST/config"
      if install -m 600 /opt/sefs/hermes/config.yaml "$DEST/config/hermes-config.yaml" 2>>"$LOG"; then
        say "OK   网关配置 hermes-config.yaml（600）"
      else
        FAIL=1; FILES_ERR="$FILES_ERR hermes-config"
        say "FAIL 网关配置备份失败"
      fi
    fi
    if [ "$FAIL" -eq 0 ]; then
      FILES_OK=true
      date -Iseconds > "$DEST/.files_success"
    else
      FILES_OK=false
    fi
  fi
fi

if [ "$MODE" = all ] || [ "$MODE" = db ]; then
  find "$DEST/db" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} + 2>/dev/null
fi

cat > "$STATUS" <<JSON
{
  "at": "$STAMP",
  "mode": "$MODE",
  "dest": "$DEST",
  "today": "$DAY",
  "db_ok": $DB_OK,
  "files_ok": $FILES_OK,
  "fail": $FAIL,
  "db_err": "$(echo "$DB_ERR" | sed 's/^ *//')",
  "files_err": "$(echo "$FILES_ERR" | sed 's/^ *//')",
  "log": "$LOG"
}
JSON

say "=== 备份结束 fail=$FAIL db_ok=$DB_OK files=$FILES_OK ==="
exit "$FAIL"