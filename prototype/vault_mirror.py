"""共享 vault 文档 → 老板个人空间（conring/users/<手机号>/obsidian）增量镜像。

背景：调研报告、知识条目、热点简报只写在共享 vault（/mnt/sefs/conring/users/18911789286/obsidian），
而个人空间 users/<手机号>/ 下只有素材（背景音乐/数字人/员工照片），
导致 APP 与私有空间（/private_obsidian/<手机号>/...）看不到公司文档。

约定：
- 单向：只读共享 vault，只写个人空间，绝不反向覆盖（vault 是唯一真源）
- 白名单：只镜像 OPC_DOC_MIRROR_PHONES 里的手机号（默认只有老板），
  不往员工目录广播公司文档
- 增量：按 (size, mtime) 判断，未变不动；源端删除不传播（个人空间留历史副本）
- 节流：默认 300 秒一次，由 kernel 的 60 秒后台同步循环调用
"""
import os
import shutil
import threading
import time

VAULT = os.environ.get("OPC_VAULT", "/mnt/sefs/conring/users/18911789286/obsidian")
USERS_DIR = os.environ.get("OPC_USERS_DIR", "/mnt/sefs/conring/users")
# 只镜像给老板：技术研究/知识库含管理层与事故细节，不适合广播到员工私有目录
MIRROR_PHONES = tuple(p.strip() for p in
                      (os.environ.get("OPC_DOC_MIRROR_PHONES", "18911789286") or "").split(",")
                      if p.strip())
MIRROR_DIRS = tuple(d.strip() for d in
                    (os.environ.get("OPC_DOC_MIRROR_DIRS",
                                    "OPC/技术研究,OPC/知识库,OPC/热点采集") or "").split(",")
                    if d.strip())
INTERVAL = float(os.environ.get("OPC_DOC_MIRROR_INTERVAL", "300") or 300)

_lock = threading.Lock()
_last = 0.0
_last_stat = {"copied": 0, "skipped": 0, "errors": 0, "at": ""}


def _same(src, dst):
    try:
        a, b = os.stat(src), os.stat(dst)
    except OSError:
        return False
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def _mirror_dir(src_root, dst_root, stat):
    for root, _dirs, files in os.walk(src_root):
        rel = os.path.relpath(root, src_root)
        dst_dir = dst_root if rel == "." else os.path.join(dst_root, rel)
        for name in files:
            if not name.endswith(".md") or name.startswith("."):
                continue
            src = os.path.join(root, name)
            dst = os.path.join(dst_dir, name)
            if _same(src, dst):
                stat["skipped"] += 1
                continue
            try:
                os.makedirs(dst_dir, exist_ok=True)
                shutil.copy2(src, dst)
                stat["copied"] += 1
            except OSError:
                stat["errors"] += 1


def mirror(force=False, phones=None, dirs=None):
    """执行一次镜像；返回统计。未到间隔且非 force 时返回上次统计（不做事）。"""
    global _last, _last_stat
    now = time.time()
    with _lock:
        if not force and now - _last < INTERVAL:
            return dict(_last_stat)
        _last = now
        stat = {"copied": 0, "skipped": 0, "errors": 0, "targets": []}
        for phone in (phones or MIRROR_PHONES):
            home = os.path.join(USERS_DIR, phone, "obsidian")
            if not os.path.isdir(os.path.dirname(home)):
                # 该手机号没有个人空间目录，不擅自创建（注册流程负责建）
                continue
            for rel in (dirs or MIRROR_DIRS):
                src_root = os.path.join(VAULT, rel)
                if not os.path.isdir(src_root):
                    continue
                _mirror_dir(src_root, os.path.join(home, rel), stat)
            stat["targets"].append(phone)
        stat["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _last_stat = stat
        return dict(stat)


def stats():
    return dict(_last_stat)