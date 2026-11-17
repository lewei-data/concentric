#!/bin/bash
# Trae 会话记录 / 项目资料 -> NAS 每日归档
# 由 crontab 在每天 23:59 调用：59 23 * * * /opt/sefs/opc-workbench/ops/trae_memory_sync.sh
set -u
export TZ=Asia/Shanghai

SRC=/home/boray/.trae-cn/memory/projects
ROOT=/mnt/sefs/conring/users/18911789286/trae
TODAY=$(date +%Y%m%d)
DAY=$(date +%F)
NOW=$(date '+%Y-%m-%d %H:%M:%S %Z')
LOG=$ROOT/sync.log
LATEST=$ROOT/latest.md

log_line() { printf '%s | %s\n' "$NOW" "$1" >> "$LOG" 2>/dev/null; }

# 目标不可写则只记录失败，不做任何替代路径
if ! mkdir -p "$ROOT" 2>/dev/null; then
  echo "[$NOW] 失败：无法创建或写入 $ROOT（NAS 未挂载或无权限）" >&2
  exit 1
fi
if ! touch "$ROOT/.write_test" 2>/dev/null; then
  echo "[$NOW] 失败：$ROOT 不可写（NAS 未挂载或无权限）" >&2
  exit 1
fi
rm -f "$ROOT/.write_test"

rows=""; status="成功"; errs=""; files=0; bytes=0; newf=0; skipf=0; projects=0

# 1) 当日会话记录
for pdir in "$SRC"/*; do
  [ -d "$pdir" ] || continue
  proj=$(basename "$pdir")
  d="$pdir/$TODAY"
  [ -d "$d" ] || continue
  n=$(find "$d" -maxdepth 1 -type f | wc -l)
  [ "$n" -gt 0 ] || continue
  b=$(du -sb "$d" 2>/dev/null | cut -f1)
  dst="$ROOT/$DAY/$proj"
  mkdir -p "$dst"
  before=$(find "$dst" -maxdepth 1 -type f 2>/dev/null | wc -l)
  if out=$(rsync -a "$d"/ "$dst"/ 2>&1); then rc=0; else rc=$?; status="部分失败"; errs="${errs}${proj}: ${out}; "; fi
  after=$(find "$dst" -maxdepth 1 -type f 2>/dev/null | wc -l)
  newf=$((newf + after - before)); skipf=$((skipf + n - (after - before)))
  projects=$((projects+1)); files=$((files+n)); bytes=$((bytes+b))
  rows="${rows}| ${proj} | ${n} | ${b} | rc=${rc} |\n"
done

# 2) 项目资料 project_memory.md
pm_rows=""
for pdir in "$SRC"/*; do
  [ -d "$pdir" ] || continue
  proj=$(basename "$pdir")
  f="$pdir/project_memory.md"
  [ -f "$f" ] || continue
  dst="$ROOT/_project_memory/$proj"
  mkdir -p "$dst"
  if cp -a "$f" "$dst"/ 2>/dev/null; then :; else status="部分失败"; errs="${errs}${proj}/project_memory.md: copy failed; "; fi
  sz=$(stat -c %s "$f" 2>/dev/null)
  pm_rows="${pm_rows}| ${proj} | ${sz} |\n"
done

# 3) 日志
if [ "$files" -eq 0 ] && [ -z "$pm_rows" ]; then
  log_line "归档日期 $DAY | 无当日记录 | 状态 $status"
else
  log_line "归档日期 $DAY | 项目 $projects 个 | 文件数 $files | 字节数 $bytes | 新增 $newf 跳过 $skipf | 状态 $status | ${errs}"
fi

# 4) latest.md（整体覆写）
{
  echo "# 最后操作记录"
  echo
  echo "- 最后执行时间：$NOW"
  echo "- 本次归档日期目录：$DAY"
  [ "$files" -gt 0 ] && echo "- 目标路径：$ROOT/$DAY"
  echo "- 当日会话文件：$files 个，$bytes 字节（新增 $newf，跳过 $skipf）"
  echo "- 总体状态：$status"
  [ -n "$errs" ] && echo "- 错误：$errs"
  echo "- 执行方式：系统 crontab（59 23 * * *），脚本 $0"
  if [ -n "$rows" ]; then
    echo
    echo "| 项目 | 单日文件数 | 字节数 | 结果 |"
    echo "|---|---|---|---|"
    printf "$rows"
  else
    echo
    echo "当日无 Trae 会话记录。"
  fi
  if [ -n "$pm_rows" ]; then
    echo
    echo "| 项目（project_memory.md） | 字节数 |"
    echo "|---|---|"
    printf "$pm_rows"
  fi
} > "$LATEST" 2>/dev/null || { echo "[$NOW] 失败：无法写入 $LATEST" >&2; exit 1; }

exit 0
