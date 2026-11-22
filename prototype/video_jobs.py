#!/usr/bin/env python3
"""远程视频作业：提交 / 状态 / 审批 / 产物签名链接。

远程智能体用 MCP 个人key（X-Mail-Agent / Bearer / ?key=）或网页会话调用；
底层仍由 kernel.inject_idea 起流水线，om_render 在 render 阶段出片，
本模块不碰 OpenMontage 本体，只提供远程友好的作业视图。

存储与 media_jobs 共用 <data_dir>/media_jobs.db（表独立）；
签名复用 opc_mcp.media_sign 的共享密钥，URL 自鉴权、不带个人 key。
"""
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from urllib.parse import quote

from utils import iso_now

OWNER_PHONE = os.environ.get("OPC_OWNER_PHONE", "18911789286")
URL_TTL = int(os.environ.get("OPC_VIDEO_URL_TTL", "600"))
# 统一用 /workbench 前缀（根路径亦可达，前缀便于将来收口）
PUBLIC_BASE = os.environ.get("OPC_VIDEO_PUBLIC_BASE",
                             "https://ai.boraydata.com/workbench").rstrip("/")

SCHEMA = """
CREATE TABLE IF NOT EXISTS video_jobs (
  job_id       TEXT PRIMARY KEY,
  trace_id     TEXT,
  phone        TEXT,
  brief        TEXT,
  requirements TEXT,
  project      TEXT,
  source_refs  TEXT,
  item_id      TEXT,
  status       TEXT,
  stage        TEXT,
  gate         TEXT,
  output_path  TEXT,
  error        TEXT,
  idem         TEXT UNIQUE,
  created_at   TEXT,
  updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_vj_phone ON video_jobs(phone, created_at DESC);
"""


class Busy(Exception):
    """流水线已被占用：HTTP 侧映射为 409 + retry_after_sec。"""


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = self._conn()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _conn(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def put(self, row):
        keys = list(row)
        with self.lock:
            con = self._conn()
            try:
                con.execute("INSERT OR REPLACE INTO video_jobs(%s) VALUES(%s)"
                            % (",".join(keys), ",".join("?" for _ in keys)),
                            tuple(row[k] for k in keys))
                con.commit()
            finally:
                con.close()

    def update(self, job_id, **fields):
        if not fields:
            return
        sql = "UPDATE video_jobs SET " + ",".join("%s=?" % k for k in fields) + " WHERE job_id=?"
        with self.lock:
            con = self._conn()
            try:
                con.execute(sql, tuple(fields.values()) + (job_id,))
                con.commit()
            finally:
                con.close()

    def get(self, job_id):
        con = self._conn()
        try:
            row = con.execute("SELECT * FROM video_jobs WHERE job_id=?", (job_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def by_idem(self, idem):
        con = self._conn()
        try:
            row = con.execute("SELECT * FROM video_jobs WHERE idem=?", (idem,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def recent(self, phone, limit=30):
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT * FROM video_jobs WHERE phone=? ORDER BY created_at DESC LIMIT ?",
                (phone, max(1, min(int(limit), 200)))).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


_STORES = {}
_STORE_LOCK = threading.Lock()


def _store(kn):
    root = getattr(kn, "data_dir", "") or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(root, "media_jobs.db")
    with _STORE_LOCK:
        st = _STORES.get(path)
        if st is None:
            st = _STORES[path] = Store(path)
        return st


def _sign(material_id, exp):
    """与 mcp-bridge 校验端同一把 HMAC（media_sign 负责密钥文件生成）。"""
    try:
        root = os.environ.get("OPC_MCP_ROOT", "/opt/sefs/opc-workbench/mcp")
        if root not in sys.path:
            sys.path.insert(0, root)
        from opc_mcp import media_sign
        return media_sign.sign(material_id, int(exp))
    except Exception:
        raw = os.environ.get("OPC_VIDEO_SIGN_SECRET", "opc-video-fallback").encode()
        return hmac.new(raw, ("%s:%d" % (material_id, int(exp))).encode(), hashlib.sha256).hexdigest()


def signed_url(job_id, ttl=None, public_base=None):
    ttl = int(ttl or URL_TTL)
    exp = int(time.time()) + ttl
    sig = _sign(job_id, exp)
    base = (PUBLIC_BASE if public_base is None else str(public_base).rstrip("/"))
    path = "/api/video/jobs/%s/media?exp=%d&sig=%s" % (quote(str(job_id)), exp, sig)
    return (base + path) if base else path, ttl


def verify_url(job_id, exp, sig):
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    if exp < int(time.time()):
        return False
    return bool(sig) and hmac.compare_digest(str(sig), _sign(job_id, exp))


def _pending_approval(kn, trace):
    if not trace:
        return None
    with kn.lock:
        for a in getattr(kn, "approvals", []) or []:
            if a.get("traceId") == trace and a.get("status") in ("pending", "timeout"):
                return dict(a)
    if not getattr(kn, "db", None):
        return None
    try:
        rows = kn.db._rows(
            "SELECT * FROM approvals WHERE trace_id=? AND status IN ('pending','timeout') "
            "ORDER BY created_at DESC LIMIT 1", (trace,))
        return rows[0] if rows else None
    except Exception:
        return None


def _public_view(row):
    """对外字段：去掉手机号与内部幂等哈希，source_refs 还原为数组。"""
    data = dict(row)
    data.pop("phone", None)
    data.pop("idem", None)
    raw = data.get("source_refs")
    if isinstance(raw, str) and raw:
        try:
            data["source_refs"] = json.loads(raw)
        except ValueError:
            pass
    return data


BLOCKING = ("blocked", "aborted", "error", "rejected", "timeout")


def _sync_row(kn, row):
    """把流水线真实状态同步回作业表，返回 (row, appr, status, stage, gate, reason)。

    blocked 等终态只写在 kn.traces 内存里，pipeline_runs 未必跟着变；
    列表与详情都走这里，避免"列表停在 queued、详情已 blocked"的快照偏差。
    """
    trace = row.get("trace_id") or ""
    run = None
    if getattr(kn, "db", None) and trace:
        run = kn.db.pipeline_run(trace)
    with kn.lock:
        t = dict(kn.traces.get(trace) or {})
    appr = _pending_approval(kn, trace)
    status = (run or {}).get("status") or row.get("status") or "queued"
    t_status = str(t.get("status") or "")
    if t_status in BLOCKING:
        status = t_status
    stage = (run or {}).get("stage") or row.get("stage") or ""
    gate = "awaiting_human" if appr else ("blocked" if status in BLOCKING else None)
    reason = str(t.get("abortReason") or "")
    if not reason and status in BLOCKING:
        with kn.lock:
            items = [dict(c) for c in (getattr(kn, "contents", []) or [])
                     if c.get("traceId") == trace]
        blockers = [str(i.get("blockedBy")) for i in items if i.get("blockedBy")]
        reason = ("内容审核未全部通过（%s）" % ", ".join(sorted(set(blockers)))
                  if blockers else "流水线在 %s 阶段阻断（%s）" % (stage or "未知", status))
    changed = {}
    for key, val in (("status", status), ("stage", stage),
                     ("gate", gate or ""), ("error", reason)):
        if val != row.get(key):
            changed[key] = val
    if changed:
        changed["updated_at"] = iso_now()
        _store(kn).update(row["job_id"], **changed)
        row = _store(kn).get(row["job_id"]) or row
    return row, appr, status, stage, gate, reason


def submit(kn, actor, brief, requirements="", project="", source_refs=None, idempotency_key=""):
    actor = str(actor or "").strip()
    if not actor:
        raise PermissionError("需要 MCP 个人 key 或登录会话")
    brief = str(brief or "").strip()
    if not brief:
        raise ValueError("brief 不能为空")
    idem = hashlib.sha256(("%s|%s" % (actor, str(idempotency_key or brief)[:200]))
                          .encode("utf-8")).hexdigest()[:32]
    st = _store(kn)
    old = st.by_idem(idem)
    if old:
        return {**_public_view(old), "idempotent_replay": True}
    idea = {"title": brief[:60], "summary": brief[:200], "detail": brief[:1000],
            "source": "远程视频作业", "score": 0.8, "tags": ["远程作业"]}
    try:
        trace, _hotspot = kn.inject_idea(idea, str(requirements or "")[:2000],
                                         str(project or ""))
    except Exception as e:
        if type(e).__name__ == "BusyError":
            raise Busy(str(e)) from e
        raise
    item_id = ""
    with kn.lock:
        for c in getattr(kn, "contents", []) or []:
            if c.get("traceId") == trace:
                item_id = str(c.get("id") or "")
                break
    refs = source_refs if isinstance(source_refs, list) else []
    job_id = "vj_" + uuid.uuid4().hex[:12]
    now = iso_now()
    row = {"job_id": job_id, "trace_id": trace, "phone": actor,
           "brief": brief[:1000], "requirements": str(requirements or "")[:2000],
           "project": str(project or "")[:60],
           "source_refs": json.dumps([str(x)[:300] for x in refs[:20]], ensure_ascii=False),
           "item_id": item_id, "status": "queued", "stage": "assessment", "gate": None,
           "output_path": "", "error": "", "idem": idem,
           "created_at": now, "updated_at": now}
    st.put(row)
    return {**_public_view(row), "idempotent_replay": False}


def status(kn, job_id, actor=""):
    row = _store(kn).get(job_id)
    if not row:
        raise KeyError(job_id)
    if actor and row["phone"] != actor and actor != OWNER_PHONE:
        raise PermissionError("作业不属于当前用户")
    row, appr, status, stage, gate, reason = _sync_row(kn, row)
    data = _public_view(row)
    data.update({
        "traceId": row.get("trace_id"),
        "runStatus": (kn.db.pipeline_run(row["trace_id"]) or {}).get("status")
        if getattr(kn, "db", None) and row.get("trace_id") else status,
        "stage": stage, "status": status, "gate": gate,
        "gateReason": reason or None,
        "approvalId": (appr or {}).get("id"),
    })
    return data


def approve(kn, job_id, actor, decision="approve"):
    row = _store(kn).get(job_id)
    if not row:
        raise KeyError(job_id)
    if str(actor or "") != OWNER_PHONE:
        raise PermissionError("仅老板可审批远程视频作业")
    appr = _pending_approval(kn, row.get("trace_id"))
    if not appr:
        raise ValueError("该作业当前没有待审批项")
    result = kn.decide_approval(appr["id"], "approve" if decision != "reject" else "reject",
                                OWNER_PHONE)
    _store(kn).update(job_id, gate=None, updated_at=iso_now())
    return {"jobId": job_id, "approvalId": appr["id"],
            "decision": "approve" if decision != "reject" else "reject",
            "status": (result or {}).get("status") or ""}


def _artifact_path(kn, row):
    """产物真源在磁盘；优先已登记路径，回退 DL.plan 的确定性目录。"""
    fp = str(row.get("output_path") or "")
    if fp and os.path.isfile(fp):
        return fp
    dl = getattr(kn, "DL", None)
    if dl is None or not row.get("trace_id") or not row.get("item_id"):
        return fp or ""
    try:
        plan = dl.plan(row["trace_id"], row.get("project") or "tongxinyuan", row["item_id"])
        for key in ("final", "video"):
            rel = str(plan.get(key) or "")
            if rel.startswith("/vault/"):
                rel = rel[len("/vault/"):]
            full = os.path.join(kn.vault_dir, rel)
            if os.path.isfile(full):
                return full
    except Exception:
        pass
    return fp or ""


def result(kn, job_id, actor=""):
    row = _store(kn).get(job_id)
    if not row:
        raise KeyError(job_id)
    if actor and row["phone"] != actor and actor != OWNER_PHONE:
        raise PermissionError("作业不属于当前用户")
    fp = _artifact_path(kn, row)
    if not fp:
        raise FileNotFoundError("成片尚未生成")
    if fp != row.get("output_path"):
        _store(kn).update(job_id, output_path=fp, updated_at=iso_now())
    url, ttl = signed_url(job_id)
    size = os.path.getsize(fp)
    ext = os.path.splitext(fp)[1].lower()
    ctype = "video/mp4" if ext == ".mp4" else ("application/json" if ext == ".json" else "application/octet-stream")
    return {"jobId": job_id, "videoUrl": url, "expiresIn": ttl,
            "bytes": size, "mime": ctype, "path": fp}


def media_file(kn, job_id):
    row = _store(kn).get(job_id)
    if not row:
        return None
    fp = _artifact_path(kn, row)
    if not fp or not os.path.isfile(fp):
        return None
    ext = os.path.splitext(fp)[1].lower()
    return fp, ("video/mp4" if ext == ".mp4" else "application/octet-stream")


def recent(kn, actor, limit=30):
    out = []
    for row in _store(kn).recent(str(actor or ""), limit):
        synced, appr, status, stage, gate, reason = _sync_row(kn, row)
        item = _public_view(synced)
        item.update({"traceId": synced.get("trace_id"), "status": status,
                     "stage": stage, "gate": gate, "gateReason": reason or None,
                     "approvalId": (appr or {}).get("id")})
        out.append(item)
    return out
