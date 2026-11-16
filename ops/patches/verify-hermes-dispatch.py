#!/usr/bin/env python3
"""校验 hermes dispatch「APP 下行可靠投递」补丁是否在位、是否真在工作。

用途：
  - hermes 升级后跑一次，确认补丁没被覆盖（退出码 1 即丢失）；
  - 日常巡检可调用，检查 outbox 是否堆积（堆积说明 APP 一直没上线/没回执）。

退出码：0 全部通过；1 有项失败。
"""
import json
import os
import sqlite3
import subprocess
import urllib.request

DISPATCH = os.environ.get("DISPATCH_DIR", "/opt/sefs/hermes/dispatch")
DB = os.path.join(DISPATCH, "dispatch.db")
HTTP = os.environ.get("DISPATCH_HTTP", "http://127.0.0.1:9120")
results = []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:                                   # noqa: BLE001
        ok, detail = False, "%s: %s" % (type(e).__name__, e)
    results.append((name, ok, detail))


def _read(fn):
    with open(os.path.join(DISPATCH, fn), encoding="utf-8") as f:
        return f.read()


def c_outbox_module():
    src = _read("outbox.py")
    for token in ("class Outbox", "def append", "def pending", "def ack"):
        assert token in src, "缺 %s" % token
    return True, "outbox.py 完整"


def c_realtime():
    src = _read("realtime_ws.py")
    need = ("from outbox import", "def set_outbox", "def replay", "def ack(",
            "_token_ok", "last_seq", "server_seq")
    miss = [t for t in need if t not in src]
    return (not miss), ("缺: %s" % miss if miss else "send 落库/补发/鉴权特征齐全")


def c_dispatchd():
    src = _read("dispatchd.py")
    need = ("import outbox", 'if base == "/messages"', "channel_token",
            "OUTBOX = outbox.Outbox", "set_outbox")
    miss = [t for t in need if t not in src]
    return (not miss), ("缺: %s" % miss if miss else "表初始化/补拉接口/令牌特征齐全")


def c_schema():
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(outbox)")]
        assert "seq" in cols and "payload" in cols and "status" in cols, "outbox 表结构不符"
        tcols = [r[1] for r in con.execute("PRAGMA table_info(channels)")]
        assert "token" in tcols, "channels 缺 token 列"
        n = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        return True, "outbox 表就绪，累计 %d 条" % n
    finally:
        con.close()


def c_service():
    body = json.loads(urllib.request.urlopen(HTTP + "/health", timeout=5).read())
    assert body.get("ok"), "health 非 ok"
    assert "outbox" in body, "health 未上报 outbox 统计"
    return True, "dispatchd 存活，outbox=%s" % body.get("outbox")


def c_backlog():
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT channel_id, COUNT(*), MAX(created_at) FROM outbox"
            " WHERE status='pending' GROUP BY channel_id ORDER BY 3 DESC LIMIT 5"
        ).fetchall()
    finally:
        con.close()
    import time
    now = time.time()
    stale = [r for r in rows if now - (r[2] or 0) > 3600]
    if stale:
        return False, "有 %d 个通道积压超 1 小时（APP 可能一直没上线）: %s" % (
            len(stale), [(r[0], r[1]) for r in stale])
    return True, "无长时间积压" if not rows else "pending: %s" % [(r[0], r[1]) for r in rows]


check("outbox 模块", c_outbox_module)
check("realtime_ws 补发/鉴权", c_realtime)
check("dispatchd 表与接口", c_dispatchd)
check("数据库结构", c_schema)
check("服务存活", c_service)
check("投递积压", c_backlog)

print("=" * 62)
failed = 0
for name, ok, detail in results:
    print("%s %-22s %s" % ("PASS" if ok else "FAIL", name, detail))
    failed += 0 if ok else 1
print("=" * 62)
print("%s（%d/%d 通过）" % ("全部通过" if not failed else "有失败项",
                            len(results) - failed, len(results)))
raise SystemExit(0 if not failed else 1)