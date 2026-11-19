"""后台作业：线程池执行 + SQLite 持久化，支持提交后轮询或短等待。"""

import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from . import config


class JobRunner:
    def __init__(self, store, owner=None, on_finish=None):
        self.store = store
        self.owner = owner or config.PROCESS_ROLE
        self.pool = ThreadPoolExecutor(max_workers=config.JOB_WORKERS, thread_name_prefix="opc-job")
        self.handlers = {}
        # 作业终态回调：服务端用它发 JSON-RPC 通知，bridge 转 SSE/Webhook
        self.on_finish = on_finish
        # 只清理本进程上次遗留的作业；另一个进程的在跑作业不受影响
        store.recover_stale_jobs(self.owner)

    def register(self, kind, fn):
        self.handlers[kind] = fn

    def submit(self, phone, kind, payload):
        if kind not in self.handlers:
            raise ValueError("未知作业类型：%s" % kind)
        job_id = "job_" + secrets.token_hex(6)
        self.store.add_job(job_id, phone, kind, json.dumps(payload, ensure_ascii=False), owner=self.owner)
        self.pool.submit(self._run, job_id, phone, kind, payload)
        return self.view(job_id)


    def _run(self, job_id, phone, kind, payload):
        self.store.update_job(job_id, "running")
        status, error = "succeeded", None
        try:
            result = self.handlers[kind](self.store, phone, payload)
            self.store.update_job(job_id, status, json.dumps(result, ensure_ascii=False))
        except Exception as e:
            status, error = "failed", "%s: %s" % (type(e).__name__, e)
            self.store.update_job(job_id, status, error=error)
        if self.on_finish:
            try:
                self.on_finish(job_id, kind, status, phone, error)
            except Exception:
                pass                                  # 通知失败不影响作业落库

    def view(self, job_id, phone=None):
        row = self.store.job(job_id)
        if not row or (phone and row["phone"] != phone):
            return None
        out = {"job_id": row["id"], "kind": row["kind"], "status": row["status"],
               "created_at": row["created_at"], "updated_at": row["updated_at"]}
        if row["payload"]:
            out["payload"] = json.loads(row["payload"])
        if row["result"]:
            out["result"] = json.loads(row["result"])
        if row["error"]:
            out["error"] = row["error"]
        return out

    def wait(self, job_id, phone=None, timeout=60):
        end = time.time() + max(0.0, min(float(timeout or 0), 300.0))
        while True:
            view = self.view(job_id, phone)
            if not view or view["status"] in ("succeeded", "failed", "interrupted") or time.time() >= end:
                return view
            time.sleep(0.4)
