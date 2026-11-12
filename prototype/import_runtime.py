#!/usr/bin/env python3
"""一次性导入：runtime.json + 会议纪要 + 飞书 + APP → opc.db。

用法：OPC_DB=1 python3 prototype/import_runtime.py
幂等：所有写入走 upsert / UNIQUE 约束，重复执行不会产生重复行。
无法还原的字段（例如历史 trace 的收口原因）留空，不做臆造。
"""
import json
import os
import sys

import db as DB

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VAULT = os.environ.get("OPC_VAULT", "/opt/sefs/obsidian")
RUNTIME = os.path.join(VAULT, "动态", "runtime.json")


def load_runtime():
    try:
        with open(RUNTIME, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        print(f"[import] 未找到 {RUNTIME}，跳过 runtime 部分")
    except Exception as e:
        print(f"[import] 读取 runtime.json 失败：{type(e).__name__}: {e}")
    return {}


def import_runtime(store, data):
    for r in data.get("reports") or []:
        store.upsert_report(r)
        store.append_event("report", r.get("traceId") or "", r, at=r.get("createdAt"))
    for a in data.get("approvals") or []:
        store.upsert_approval(a)
        store.append_event("approval", a.get("traceId") or "", a, at=a.get("createdAt"))
    for h in data.get("hotspots") or []:
        store.upsert_hotspot(h)
    for k in data.get("knowledge") or []:
        store.upsert_knowledge(k)
    for m in data.get("messages") or []:
        store.insert_message(m)
        store.append_event("message", m.get("traceId") or "", m, at=m.get("at"))

    # 历史 trace 只能从这些引用里还原骨架：状态标记 legacy，不编造收口结论
    seen = {}
    for coll in ("reports", "approvals", "messages"):
        for item in data.get(coll) or []:
            tid = item.get("traceId")
            at = item.get("createdAt") or item.get("at") or ""
            if tid and at and (tid not in seen or at < seen[tid]):
                seen[tid] = at
    for tid, at in seen.items():
        store.upsert_trace({"traceId": tid, "status": "legacy", "startedAt": at,
                            "meta": {"source": "runtime.json", "backfilled": True}})
    print(f"[import] runtime.json：报告 {len(data.get('reports') or [])}、"
          f"审批 {len(data.get('approvals') or [])}、热点 {len(data.get('hotspots') or [])}、"
          f"知识 {len(data.get('knowledge') or [])}、消息 {len(data.get('messages') or [])}、"
          f"trace 骨架 {len(seen)}")


def import_meetings(store):
    try:
        import deliverables as DL
    except Exception as e:
        print(f"[import] deliverables 不可用，跳过会议/交付：{type(e).__name__}: {e}")
        return 0, 0
    records = []
    try:
        records = DL.load_meeting_records(limit=1000)
    except Exception as e:
        print(f"[import] 读取会议记录失败：{type(e).__name__}: {e}")
    for rec in records:
        store.upsert_meeting(rec)
        for seq, sp in enumerate(rec.get("speeches") or [], 1):
            store.add_speech(rec.get("id"), seq, sp)
    try:
        items = DL.load_deliverable_items(limit=2000)
    except Exception as e:
        print(f"[import] 读取交付物失败：{type(e).__name__}: {e}")
        items = []
    for item in items:
        store.upsert_content(item)
    print(f"[import] 会议 {len(records)} 场 / 发言 "
          f"{sum(len(r.get('speeches') or []) for r in records)} 条 / 交付 {len(items)} 条")
    return len(records), len(items)


def import_chat(store):
    import ingest as ING
    store.flush()
    feishu = ING.pull_feishu(store, full=True)
    app = ING.pull_dispatch(store, full=True)
    print(f"[import] 多渠道消息：飞书新增 {feishu} 条、APP 新增 {app} 条")
    return feishu, app


def main():
    if not DB.ENABLED:
        print("请用 OPC_DB=1 运行：OPC_DB=1 python3 prototype/import_runtime.py")
        return 2
    store = DB.Store()
    print(f"[import] 目标库 {DB.DB_PATH}")
    data = load_runtime()
    import_runtime(store, data)
    import_meetings(store)
    import_chat(store)
    store.flush()
    counts = store.table_counts()
    print("[import] 表 → 行数")
    for table, n in counts.items():
        print(f"  {table:18s} {n}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
