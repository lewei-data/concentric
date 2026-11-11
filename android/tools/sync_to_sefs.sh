#!/usr/bin/env bash
# 把本地 voice-mcp-android 工程同步到服务器。
#   bash tools/sync_to_sefs.sh
#
# 排除项说明：
#   .gradle / build / app/build —— Gradle 构建缓存，跨平台无效，服务器上会重建
#   local.properties            —— 里面是本机 Android SDK 路径（Windows），传过去反而误导
#   .idea / captures            —— IDE 与录屏缓存
#
# 可用环境变量覆盖目标：
#   REMOTE=user@host  DEST=/opt/sefs/opc-workbench/android
set -euo pipefail

REMOTE="${REMOTE:-boray@192.168.31.206}"
DEST="${DEST:-/opt/sefs/opc-workbench/android}"

cd "$(dirname "$0")/.."

echo "== 本地 -> $REMOTE:$DEST"
tar czf - \
  --exclude=./.gradle \
  --exclude=./.idea \
  --exclude=./build \
  --exclude=./app/build \
  --exclude=./captures \
  --exclude=./local.properties \
  --exclude=./.git \
  . | ssh -o StrictHostKeyChecking=no "$REMOTE" \
        "mkdir -p '$DEST' && tar xzf - -C '$DEST'"

echo "== 远端复核"
ssh -o StrictHostKeyChecking=no "$REMOTE" \
  "cd '$DEST' && echo \"files=\$(find . -type f | wc -l) size=\$(du -sh . | cut -f1) src=\$(find app/src -type f | wc -l) apk=\$(ls dist/*.apk 2>/dev/null | wc -l)\""
