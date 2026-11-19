"""kb_overview 数据源：分组计数 + 数据指纹 rev（跨进程一致）。

设计（依据《同心圆知识库-概览数据一次返回与缓存方案》）：
- groups 分组（knowledge/materials/content）而不是平铺 6 个标量，面板按组做主次；
- rev 是「数据指纹」：由各分组计数 + 素材最近更新时间派生（sha1[:12]），
  任何写操作（上传/删除/取代/分析完成）都会改变它——不需要在每个写点埋计数器，
  stdio 服务与 9105 上传服务两个进程读到同一批库，天然一致；
- health/owner/recent 的缓存与限流豁免由 server 层处理，本模块只管算数。

缓存层级（server 层实现）：
- groups/rev：每 tenant 进程内缓存 30s（OPC_MCP_OVERVIEW_TTL）；
- health：与 kb_ping 共用 probe_cached / health_cached（60s/30s），不重复探测上游；
- recent.jobs：每 tenant 10s。
"""

import hashlib
import time
from datetime import datetime

from . import resources


def _iso(ts):
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    if not ts:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def groups_and_rev(phone, store):
    """返回 (groups, rev)。一条批量查询 + owner 库计数，成本可控（表都是小表）。

    口径与 kb_status 对齐（第 4 轮验证 D1/D2 整改）：
    - knowledge.cards/chunks 直接查 MCP 库（此前误用 owner_counts 的占位 0）；
    - materials.analyzed 是严格口径（by_status.analyzed，与 kb_status.by_status 同值），
      skipped 单列；pending 含 failed。
    """
    by_status = {r["status"]: r["n"] for r in store.all(
        "SELECT status, COUNT(*) n FROM materials WHERE phone=? GROUP BY status", (phone,))}
    latest = store.one(
        "SELECT MAX(created_at) m FROM materials WHERE phone=?", (phone,))
    total = sum(by_status.values())
    superseded = int(by_status.get("superseded", 0))
    pending = int(by_status.get("pending", 0)) + int(by_status.get("failed", 0))
    analyzed = int(by_status.get("analyzed", 0))
    skipped = int(by_status.get("skipped", 0))

    def _cnt(sql):
        try:
            return int(store.one(sql, (phone,))["n"] or 0)
        except Exception:                                 # noqa: BLE001 表缺失等按 0 计
            return 0

    cards = _cnt("SELECT COUNT(*) n FROM cards WHERE phone=?")
    chunks = _cnt("SELECT COUNT(*) n FROM chunks WHERE phone=?")
    rc = resources.owner_counts(phone)   # 轻量：不算共享 vault / 目录枚举
    groups = {
        "knowledge": {"entries": int(rc.get("knowledge", 0)),
                      "cards": cards,
                      "chunks": chunks},
        "materials": {"total": total, "analyzed": analyzed, "skipped": skipped,
                      "pending": pending, "superseded": superseded,
                      "latest_at": _iso(latest["m"]) if latest else None},
        "content": {"contents": int(rc.get("contents", 0)),
                    "reports": int(rc.get("reports", 0))},
    }
    fingerprint = "|".join(str(x) for x in (
        groups["knowledge"]["entries"], groups["knowledge"]["cards"],
        groups["knowledge"]["chunks"], groups["materials"]["total"],
        groups["materials"]["analyzed"], groups["materials"]["skipped"],
        groups["materials"]["superseded"],
        groups["materials"]["latest_at"], groups["content"]["contents"],
        groups["content"]["reports"]))
    rev = hashlib.sha1(fingerprint.encode()).hexdigest()[:12]
    return groups, rev
