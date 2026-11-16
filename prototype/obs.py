"""失败可见性（C3）：全局失败事件环形缓冲 + 按位置聚合。

背景：内核里多处 `except Exception: pass`，出问题时日志一片安静，
运维只能看到"功能没反应"。统一走 log_event 后，/api/ops 暴露
"近 1 小时失败点 Top10"，一眼看到哪条路径在持续报错。

只做内存环形缓冲（默认 1000 条），不落库：失败点用于即时定位，
不需要长期存档，也避免异常路径再写库造成二次故障。
"""
import collections
import threading
import time

FAILS = collections.deque(maxlen=1000)
_LOCK = threading.Lock()
_LEVEL_LOG = {"error": 1, "warn": 1, "info": 0, "debug": 0}


def log_event(where, err=None, level="warn", quiet=False, **ctx):
    """记录一次失败/异常。where 用"方法名.动作"，便于聚合时分得开。

    quiet=True 时只进缓冲不打印（用于清理临时文件这类预期内失败）。
    """
    item = {
        "t": time.time(),
        "level": level,
        "where": where,
        "err": f"{type(err).__name__}: {err}" if err is not None else "",
        "ctx": {k: str(v)[:100] for k, v in ctx.items()},
    }
    with _LOCK:
        FAILS.append(item)
    if not quiet and _LEVEL_LOG.get(level, 0):
        print(f"[{level}] {where} {item['err']} {item['ctx'] or ''}", flush=True)
    return item


def recent_top(minutes=60, limit=10):
    """近 N 分钟失败点聚合：按 (位置, 异常类型) 计数降序。"""
    cut = time.time() - max(1, minutes) * 60
    with _LOCK:
        items = [i for i in FAILS if i["t"] >= cut]
    agg = {}
    for i in items:
        kind = i["err"].split(":")[0]
        key = (i["where"], kind)
        a = agg.get(key)
        if a is None:
            a = agg[key] = {"where": i["where"], "kind": kind, "err": i["err"],
                            "level": i["level"], "count": 0, "lastAt": 0.0}
        a["count"] += 1
        if i["t"] > a["lastAt"]:
            a["lastAt"] = i["t"]
            a["err"] = i["err"] or a["err"]
            a["level"] = i["level"]
    top = sorted(agg.values(), key=lambda x: (-x["count"], -x["lastAt"]))[:max(1, limit)]
    for t in top:
        t["lastAt"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t["lastAt"]))
    return {"windowMinutes": minutes, "total": len(items), "top": top}


def reset():
    """清空缓冲（测试用）。"""
    with _LOCK:
        FAILS.clear()