#!/usr/bin/env bash
# GitHub 自动同步：把 /home/boray/opc-workbench 的本地变更提交并推送到 origin/main
#
# 由 crontab 每小时触发，也可手动执行：./sync-github.sh
# 认证：SSH 部署密钥 ~/.ssh/opc_github_ed25519（仓库 Deploy keys，需勾选 write）
# 日志：logs/github-sync.log（已被 .gitignore 忽略，不入库）

unset LD_LIBRARY_PATH   # 本机 env 里的 LD_LIBRARY_PATH 会污染 ls/stat 等系统命令
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANCH="main"
LOG="$ROOT/logs/github-sync.log"
LOCK="/tmp/opc-github-sync.lock"
SSH_KEY="/home/boray/.ssh/opc_github_ed25519"

export HOME="${HOME:-/home/boray}"
export PATH="/usr/local/bin:/usr/bin:/bin"
# 只认这把部署密钥，避免用错身份；BatchMode 保证 cron 下不等待交互输入
export GIT_SSH_COMMAND="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

mkdir -p "$ROOT/logs"

# 日志超过 5MB 切分一次，避免无限增长
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  mv "$LOG" "$LOG.1"
fi

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# 防并发：上一轮未结束时直接退出
exec 9>"$LOCK"
flock -n 9 || { log "上一轮同步仍在进行，跳过"; exit 0; }

cd "$ROOT" || { log "目录不存在：$ROOT"; exit 1; }
[ -d .git ] || { log "不是 git 仓库：$ROOT"; exit 1; }

# 1. 提交本地变更
git add -A
if git diff --cached --quiet; then
  log "无本地变更"
else
  if git commit -q -m "自动同步：$(date '+%F %T')"; then
    log "已提交 $(git log -1 --pretty=%h)"
  else
    log "提交失败"
    exit 1
  fi
fi

# 2. 拉取远端（网络抖动时重试）
fetched=0
for i in 1 2 3; do
  if git fetch -q origin "$BRANCH"; then fetched=1; break; fi
  sleep $((i * 15))
done
if [ "$fetched" -ne 1 ]; then
  log "拉取远端失败（已重试 3 次），本轮不推送"
  exit 1
fi

# 3. 变基到远端最新，保留本地提交
if ! git rebase -q "origin/$BRANCH" >> "$LOG" 2>&1; then
  git rebase --abort 2>/dev/null
  log "rebase 冲突已中止，需人工处理"
  exit 1
fi

# 4. 推送（网络抖动时重试）
if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ]; then
  log "本地与远端一致，无需推送"
  exit 0
fi
for i in 1 2 3; do
  if git push -q origin "$BRANCH" >> "$LOG" 2>&1; then
    log "推送成功 $(git log -1 --pretty=%h)"
    exit 0
  fi
  sleep $((i * 20))
done
log "推送失败（已重试 3 次），下轮再试"
exit 1
