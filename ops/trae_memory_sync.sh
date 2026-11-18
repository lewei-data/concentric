#!/bin/bash
# Trae 会话记录 / 项目资料 -> NAS 全量增量归档
# 由 crontab 每天 00:30 调用：30 0 * * * /opt/sefs/opc-workbench/ops/trae_memory_sync.sh
# 逻辑：把 memory/projects 下所有项目、所有日期目录整体增量镜像到 NAS（rsync 幂等，只补新增/变化文件）
# 用法：bash trae_memory_sync.sh            # 正常全量增量同步
#       bash trae_memory_sync.sh --dry-run  # 只统计不复制
set -u
export TZ=Asia/Shanghai

SRC=/home/boray/.trae-cn/memory/projects
ROOT=/mnt/sefs/conring/users/18911789286/trae
PMDIR=$ROOT/_project_memory
LOG=$ROOT/sync.log
LATEST=$ROOT/latest.md
NOW=$(date '+%Y-%m-%d %H:%M:%S %Z')
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
RSYNC_OPT=(-a)
[ "$DRY" -eq 1 ] && RSYNC_OPT+=(--dry-run)

# 目标可写性检查：不可写则记录失败并退出，不做替代路径
if ! mkdir -p "$ROOT" 2>/dev/null || ! touch "$ROOT/.write_test" 2>/dev/null; then
  echo "[$NOW] 失败：$ROOT 不可写（NAS 未挂载或无权限）" >&2
  echo "[$NOW] 失败：$ROOT 不可写（NAS 未挂载或无权限）" >> "$ROOT/sync.log" 2>/dev/null
  exit 1
fi
rm -f "$ROOT/.write_test"

status="成功"; errs=""; dates=0; files=0; bytes=0; newf=0; skipf=0; rows=""

for pdir in "$SRC"/*; do
  [ -d "$pdir" ] || continue
  proj=$(basename "$pdir")
  for d in "$pdir"/*/; do
    [ -d "$d" ] || continue
    day=$(basename "$d")
    case "$day" in [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;; *) continue ;; esac
    n=$(find "$d" -maxdepth 1 -type f | wc -l)
    [ "$n" -gt 0 ] || continue
    b=$(du -sb "$d" 2>/dev/null | cut -f1)
    dd="${day:0:4}-${day:4:2}-${day:6:2}"
    dst="$ROOT/$dd/$proj"
    before=0; [ -d "$dst" ] && before=$(find "$dst" -maxdepth 1 -type f | wc -l)
    mkdir -p "$dst" 2>/dev/null
    if out=$(rsync "${RSYNC_OPT[@]}" "$d" "$dst"/ 2>&1); then rc=0; else rc=$?; status="部分失败"; errs="${errs}${dd}/${proj}: ${out}; "; fi
    after=0; [ -d "$dst" ] && after=$(find "$dst" -maxdepth 1 -type f | wc -l)
    added=$((after - before)); [ "$added" -lt 0 ] && added=0
    newf=$((newf + added)); skipf=$((skipf + n - added))
    dates=$((dates+1)); files=$((files+n)); bytes=$((bytes+b))
    [ "$added" -gt 0 ] && rows="${rows}| ${dd} | ${proj} | ${n} | ${added} | ${b} |\n"
  done
done

# project_memory.md 同步
pm_rows=""; pm_new=0
for pdir in "$SRC"/*; do
  [ -d "$pdir" ] || continue
  f="$pdir/project_memory.md"
  [ -f "$f" ] || continue
  proj=$(basename "$pdir")
  dst="$PMDIR/$proj"
  mkdir -p "$dst" 2>/dev/null
  if [ -f "$dst/project_memory.md" ] && cmp -s "$f" "$dst/project_memory.md"; then
    pm_rows="${pm_rows}| ${proj} | $(stat -c %s "$f") | 无变化 |\n"
  else
    if [ "$DRY" -eq 0 ]; then
      cp -a "$f" "$dst"/ 2>/dev/null || { status="部分失败"; errs="${errs}${proj}/project_memory.md: copy failed; "; }
    fi
    pm_new=$((pm_new+1))
    pm_rows="${pm_rows}| ${proj} | $(stat -c %s "$f") | 已更新 |\n"
  fi
done

# 日志
log_line() { printf '%s | %s\n' "$NOW" "$1" >> "$LOG" 2>/dev/null; }
tag=""; [ "$DRY" -eq 1 ] && tag="[dry-run] "
if [ "$dates" -eq 0 ] && [ "$pm_new" -eq 0 ]; then
  log_line "${tag}全量增量同步 | 无新增（会话日期目录 $dates 个 / ${files} 文件，均未变化） | 状态 $status"
else
  log_line "${tag}全量增量同步 | 会话日期目录 $dates 个 | 文件数 $files | 字节数 $bytes | 新增 ${newf} 跳过 ${skipf} | project_memory 更新 ${pm_new} | 状态 $status | ${errs}"
fi

# latest.md（整体覆写）
{
  echo "# 最后操作记录"
  echo
  echo "- 最后执行时间：$NOW"
  echo "- 操作类型：全量增量同步（memory/projects → NAS）$([ "$DRY" -eq 1 ] && echo "（dry-run，未实际复制）")"
  echo "- 覆盖会话日期目录：$dates 个，文件 $files 个，$bytes 字节"
  echo "- 本次新增 $newf 个，幂等跳过 $skipf 个；project_memory.md 更新 $pm_new 个"
  echo "- 总体状态：$status"
  [ -n "$errs" ] && echo "- 错误：$errs"
  echo "- 执行方式：系统 crontab（每天 00:30），脚本 $0"
  if [ -n "$rows" ]; then
    echo
    echo "本次有新增的日期："
    echo
    echo "| 日期 | 项目 | 文件数 | 新增 | 字节数 |"
    echo "|---|---|---|---|---|"
    printf "$rows"
  else
    echo
    echo "本次无新增文件（全部已归档）。"
  fi
  if [ -n "$pm_rows" ]; then
    echo
    echo "| 项目（project_memory.md） | 字节数 | 本次 |"
    echo "|---|---|---|"
    printf "$pm_rows"
  fi
} > "$LATEST" 2>/dev/null || { echo "[$NOW] 失败：无法写入 $LATEST" >&2; exit 1; }

echo "[$NOW] dates=$dates files=$files bytes=$bytes new=$newf skip=$skipf pm_new=$pm_new status=$status"
