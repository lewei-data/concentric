"""事件总线（C1 拆分第一刀）：SSE 订阅广播 + 关键事件落库。

从 Kernel.emit 抽出。两个实质性变化：

1. 总线用独立锁。原实现在 kernel.lock 内遍历订阅者，某个 SSE 连接卡住
   （浏览器挂起、移动端断网未释放）会把整条流水线一起堵住。
2. 落库/落盘职责通过 db、on_key 回调注入，总线本身不持有 Kernel，
   便于以后单测与替换（比如换成 Redis 广播）。
"""
import queue
import threading

from obs import log_event
from utils import iso_now

KEY_KINDS = ("hotspot", "report", "approval", "knowledge")


class EventBus:
    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers = []
        self.count = 0

    def subscribe(self, q):
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def emit(self, kind, trace, data, db=None, on_key=None):
        env = {"kind": kind, "traceId": trace, "at": iso_now(), "data": data}
        with self.lock:
            self.count += 1
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(env)
            except queue.QueueFull:
                # 订阅端消费太慢会丢事件；静默丢弃会让前端"少一条消息"且无从排查
                log_event("emit.subscriber_queue_full", None, level="warn", kind=kind, trace=trace)
        if db:
            db.append_event(kind, trace, data, at=env["at"])
            if kind == "report" and isinstance(data, dict):
                db.upsert_report(data)
            elif kind == "approval" and isinstance(data, dict):
                db.upsert_approval(data)
            elif kind == "hotspot" and isinstance(data, dict):
                db.upsert_hotspot(data)
            elif kind == "knowledge" and isinstance(data, dict):
                db.upsert_knowledge(data)
            elif kind == "pipeline" and isinstance(data, dict) and data.get("id"):
                db.upsert_content(data)
        elif kind in KEY_KINDS and on_key:
            on_key()          # 旧路径：低频关键事件落盘，重启后可恢复
        return env