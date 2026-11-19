"""员工照片/半身照的全局共享清单（所有 MCP key 可见，与 shared_materials 同口径）。

设计：
- 不复制文件、不建新表：file_key 由 (类别, 相对名) 确定性派生，重启/换设备都稳定；
- 目录白名单固定为三类真源，Web 下载端点与 MCP 工具共用本模块，避免口径漂移；
- 对外只给 id/姓名/职位/类别/download_url，绝不带服务端绝对路径（与 ask with_brief 同红线）。

真源（2026-11-18 核实）：
  portrait      <vault>/同心圆员工照片/员工本人形象-20261118/   19 张 1024x1024
  halfbody      <repo>/prototype/assets/portraits/<id>.png     19 张半身照 576x768
  digital_human <vault>/数字人口播形象/*.png                    1 张 1280x1920
"""

import hashlib
import json
import os
from pathlib import Path

from . import config

# 对外 file_key 只用字母数字，兼容 Web 下载端点的 [A-Za-z0-9]{16,64} 校验。
_KEY_PREFIX = "sp"          # staff photo 前缀，便于与生成产物的随机 key 区分
_KEY_LEN = 24

# 类别 → (目录解析, 中文标签, MIME)
_IMG_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_EMPLOYEES = Path(os.environ.get("OPC_EMPLOYEES_JSON",
                                 "/opt/sefs/opc-workbench/mcp-asr/employees.json"))


def _roots():
    """返回 [(kind, label, root_path)]，路径只用环境变量/代码常量，不接收客户端输入。"""
    repo = config.ROOT.parent          # mcp/ 的上一级 = opc-workbench
    return [
        ("portrait", "员工本人形象", config.VAULT / "同心圆员工照片" / "员工本人形象-20261118"),
        ("halfbody", "员工半身照", repo / "prototype" / "assets" / "portraits"),
        ("digital_human", "数字人口播形象", config.VAULT / "数字人口播形象"),
    ]


def _roster():
    """id → (姓名, 职位)，用于给半身照的文件名补可读标签；读不到就回退 id。"""
    try:
        data = json.loads(_EMPLOYEES.read_text(encoding="utf-8"))
        emps = data.get("employees", [])
        seq = emps.values() if isinstance(emps, dict) else emps
        return {str(e.get("id", "")): (str(e.get("name") or ""), str(e.get("role") or e.get("title") or ""))
                for e in seq if isinstance(e, dict)}
    except (OSError, ValueError, TypeError):
        return {}


def file_key(kind, rel_name):
    """确定性 file_key：同一张照片在任何进程/重启后都得到同一个 key。"""
    raw = "%s|%s|%s" % (_KEY_PREFIX, kind, rel_name)
    return _KEY_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_KEY_LEN]


def _label(kind, rel_name):
    stem = Path(rel_name).stem
    if kind == "portrait":
        # OPC员工照片-05-牛盒码-CTO-1024x1024-2026-11-18.png → 05 牛盒码 · CTO
        parts = stem.split("-")
        if len(parts) >= 4 and parts[0].startswith("OPC"):
            return "%s %s · %s" % (parts[1], parts[2], parts[3])
    elif kind == "halfbody":
        name, role = _roster().get(stem, ("", ""))
        return ("%s · %s" % (name, role)).strip(" ·") or stem
    return stem


def listing(kind="all", limit=200):
    """全局共享照片清单。只返回对外字段，不含服务端路径。"""
    wanted = str(kind or "all").strip().lower()
    valid = {k for k, _, _ in _roots()} | {"all"}
    if wanted not in valid:
        return None, "未知类别：%s（可选 %s）" % (wanted, "/".join(sorted(valid)))
    items, seen = [], set()
    for k, label, root in _roots():
        if wanted != "all" and k != wanted:
            continue
        if not root.is_dir():
            continue
        for p in sorted(root.iterdir()):
            # _contact 是拼版联络图（多员工同框），不是某个人的照片；隐藏文件一律跳过
            if (p.name.startswith((".", "_")) or not p.is_file()
                    or p.suffix.lower() not in _IMG_EXT):
                continue
            fk = file_key(k, p.name)
            if fk in seen:
                continue
            seen.add(fk)
            items.append({
                "id": fk,
                "file_key": fk,
                "name": _label(k, p.name),
                "filename": p.name,
                "kind": k,
                "category": label,
                "size": p.stat().st_size,
                "mime": _IMG_EXT[p.suffix.lower()],
                "download_url": "%s/api/mcp/file/%s" % (config.PUBLIC_WORKBENCH_BASE, fk),
                "download_auth": "X-Mail-Agent: <同一把个人 API key>（任意已注册 key 均可下载）",
            })
            if len(items) >= int(limit):
                return items, None
    return items, None


def resolve(file_key_value):
    """file_key → (绝对路径, MIME, 对外元数据)；仅供服务端下载端点使用，不对外返回路径。"""
    fk = str(file_key_value or "").strip()
    if not fk.startswith(_KEY_PREFIX) or not fk.isalnum():
        return None
    for k, _, root in _roots():
        if not root.is_dir():
            continue
        for p in root.iterdir():
            if p.is_file() and file_key(k, p.name) == fk:
                return p, _IMG_EXT.get(p.suffix.lower(), "application/octet-stream"), k
    return None
