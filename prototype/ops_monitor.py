#!/usr/bin/env python3
"""CTO「运维监控」聚合：系统运行、任务执行、视频生成环节与异常处置。

只读聚合，数据源：
  - 主服务 `Kernel.snapshot()`（engine / stats.hermes / pipeline / cost / approvals / reports）
  - 主服务 `/api/health` 等价信息
  - media-engine :9200、media-gen :9103 的 /health 与 /v1/jobs（短超时探测，离线也返回结构）
  - media-gen/output/jobs.json 的落盘任务记录（服务离线时仍能看到历史任务）

`/api/ops/recover` 负责动作，本模块只产出「有哪些异常、可做什么」。
"""

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import urlopen

TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(BASE_DIR, ".."))
JOBS_FILE = os.path.join(REPO_ROOT, "media-gen", "output", "jobs.json")
PROBE_TTL = 15
_PROBE = {"at": 0.0, "data": None}
_PROBE_LOCK = threading.Lock()

SERVICES = [
    {"id": "media-engine", "name": "媒体引擎 TTS / ASR / 视频", "url": "http://127.0.0.1:9200",
     "health": "/health", "jobs": "/v1/jobs?limit=20"},
    {"id": "media-gen", "name": "图 / 视频生成任务服务", "url": "http://127.0.0.1:9103",
     "health": "/health", "jobs": "/v1/jobs?limit=20"},
]


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _ts(epoch):
    try:
        return datetime.fromtimestamp(float(epoch), TZ).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _uptime(started_at):
    try:
        dt = datetime.fromisoformat(str(started_at))
        return max(0, int((datetime.now(TZ) - dt).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _probe_json(url, timeout=1.2):
    try:
        with urlopen(url, timeout=timeout) as resp:
            raw = resp.read(240000).decode("utf-8", "replace")
        try:
            return True, json.loads(raw)
        except ValueError:
            return True, {"text": raw[:2000]}
    except HTTPError as e:
        return False, {"http": e.code, "error": f"HTTP {e.code}"}
    except Exception as e:
        return False, {"error": type(e).__name__}


def _services(force=False):
    with _PROBE_LOCK:
        cached = _PROBE["data"]
        fresh = cached is not None and (time.time() - _PROBE["at"]) < PROBE_TTL
    if fresh and not force:
        return cached

    out = []
    for svc in SERVICES:
        ok, health = _probe_json(svc["url"] + svc["health"])
        jobs = []
        jobs_note = "服务离线，未查询任务队列"
        if ok:
            ok2, payload = _probe_json(svc["url"] + svc["jobs"])
            if ok2:
                jobs = payload.get("jobs") if isinstance(payload, dict) else []
                jobs = jobs if isinstance(jobs, list) else []
                jobs_note = f"在线任务 {len(jobs)} 条"
            else:
                jobs_note = f"任务队列需鉴权或不可达（{payload.get('error') or payload.get('http')}）"
        status = "degraded" if ok and isinstance(health, dict) and health.get("status") == "degraded" else (
            "online" if ok else "offline")
        out.append({
            "id": svc["id"], "name": svc["name"], "url": svc["url"],
            "online": ok, "status": status,
            "health": health if isinstance(health, dict) else {},
            "jobs": jobs[:20], "jobsNote": jobs_note,
        })
    with _PROBE_LOCK:
        _PROBE["at"], _PROBE["data"] = time.time(), out
    return out


def _local_jobs(limit=24):
    """media-gen 落盘任务：服务离线时这是唯一的视频任务视图。"""
    try:
        with open(JOBS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return [], False
    rows = list(raw.values()) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    rows.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
    out = []
    for j in rows[:limit]:
        params = j.get("params") or {}
        progress = j.get("progress") or {}
        out.append({
            "id": j.get("id") or "",
            "kind": j.get("kind") or "",
            "backend": j.get("backend") or "",
            "status": j.get("status") or "unknown",
            "prompt": str(params.get("prompt") or "")[:160],
            "progress": {"step": progress.get("step"), "total": progress.get("total")},
            "createdAt": _ts(j.get("created_at")),
            "finishedAt": _ts(j.get("finished_at")),
            "elapsedSec": j.get("elapsed_sec"),
            "error": str(j.get("error") or "")[:300],
            "outputPath": j.get("output_path") or "",
        })
    return out, True


def _render_view(pipeline):
    out = []
    for it in pipeline:
        render = it.get("render")
        check = it.get("renderCheck")
        err = it.get("renderError")
        if not (render or check or err or it.get("blockedBy") == "render-gate"):
            continue
        passed = None
        if isinstance(check, dict):
            passed = check.get("pass")
        state = "ok" if passed is True else ("failed" if passed is False or err else "pending")
        out.append({
            "id": it.get("id") or "", "title": it.get("title") or "",
            "stage": it.get("stage") or "", "state": state,
            "error": str(err or (check or {}).get("reasons") or "")[:240]
            if isinstance(check, dict) else str(err or "")[:240],
            "blockedBy": it.get("blockedBy") or "",
            "video": (render or {}).get("video") or (render or {}).get("mp4")
            if isinstance(render, dict) else str(render or "")[:180],
        })
    return out[-20:]


def build(state, health, force_probe=False):
    """返回 /api/ops 的响应体；state 为 Kernel.snapshot()。"""
    state = state or {}
    pipeline = state.get("pipeline") or []
    approvals = state.get("approvals") or []
    reports = state.get("reports") or []
    hermes = (state.get("stats") or {}).get("hermes") or {}
    cost = state.get("cost") or {}
    usage = cost.get("usage") or {}
    aborted = cost.get("aborted") or {}

    services = _services(force_probe)
    local_jobs, jobs_file_ok = _local_jobs()
    render_items = _render_view(pipeline)
    stage_counts = Counter(str(it.get("stage") or "unknown") for it in pipeline)
    blocked = [it for it in pipeline if it.get("blockedBy")]
    blocked_by = Counter(str(it.get("blockedBy") or "unknown") for it in blocked)
    pending_approvals = [a for a in approvals if a.get("status") == "pending"]
    failed_jobs = [j for j in local_jobs if j["status"] in ("failed", "interrupted")]
    stuck_jobs = [j for j in local_jobs if j["status"] in ("running", "queued")]
    render_failed = [r for r in render_items if r["state"] == "failed"]

    alerts = []
    for svc in services:
        if not svc["online"]:
            alerts.append({
                "id": f"svc-{svc['id']}", "level": "critical",
                "title": f"{svc['name']} 未运行",
                "detail": f"{svc['url']} 探测失败：{(svc['health'] or {}).get('error') or '连接被拒绝'}。"
                          "新的 TTS / ASR / 视频生成任务无法提交。",
                "source": svc["url"], "at": _now(),
                "action": "需运维启动服务（opc.sh / systemd），前端不直接重启进程",
            })
        elif svc["status"] == "degraded":
            alerts.append({
                "id": f"degraded-{svc['id']}", "level": "warning",
                "title": f"{svc['name']} 降级运行",
                "detail": "健康检查返回 degraded，部分 TTS 工作进程不可用。",
                "source": svc["url"], "at": _now(), "action": "检查引擎日志后恢复",
            })
    if failed_jobs:
        alerts.append({
            "id": "media-failed-jobs", "level": "warning",
            "title": f"生成任务失败 / 中断 {len(failed_jobs)} 条",
            "detail": "；".join(f"{j['id']} {(j['error'] or j['status'])[:60]}" for j in failed_jobs[:3]),
            "source": "media-gen/output/jobs.json", "at": _now(),
            "action": "查看错误详情，修正参数后由运维重提任务",
        })
    if stuck_jobs:
        alerts.append({
            "id": "media-stuck-jobs", "level": "warning",
            "title": f"生成任务滞留 {len(stuck_jobs)} 条（running / queued）",
            "detail": "；".join(f"{j['id']} {j['status']} {j['createdAt']}" for j in stuck_jobs[:3]),
            "source": "media-gen/output/jobs.json", "at": _now(),
            "action": "服务离线期间不会被自动重跑，需人工确认后恢复",
        })
    if render_failed:
        alerts.append({
            "id": "render-failed", "level": "critical",
            "title": f"主流水线出片失败 {len(render_failed)} 条",
            "detail": "；".join(f"{r['title']}（{r['error'] or r['state']}）"[:70] for r in render_failed[:3]),
            "source": "/api/state.pipeline", "at": _now(),
            "action": "排查 om_render / TTS / Remotion，返工后重新出片",
        })
    for gate, count in blocked_by.most_common(4):
        alerts.append({
            "id": f"blocked-{gate}", "level": "warning",
            "title": f"流水线阻塞于 {gate}：{count} 条",
            "detail": "；".join(str(it.get("title") or "")[:40]
                              for it in blocked if it.get("blockedBy") == gate)[:220],
            "source": "/api/state.pipeline", "at": _now(),
            "action": "审批门请到下方「待处理审批」拍板；其余门禁需按流程返工",
        })
    if aborted:
        alerts.append({
            "id": "cost-abort", "level": "critical",
            "title": f"成本熔断 {len(aborted)} 轮 trace",
            "detail": "；".join(f"{k}: {v}" for k, v in list(aborted.items())[:2])[:220],
            "source": "/api/state.cost", "at": _now(),
            "action": "成本护栏为内存态，需运维确认后重启清零",
        })
    for a in approvals:
        if a.get("status") in ("pending", "timeout"):
            alerts.append({
                "id": f"approval-{a.get('id')}", "level": "warning",
                "title": f"审批{a.get('status') == 'timeout' and '超时' or '待拍板'}：{a.get('title') or a.get('action')}",
                "detail": f"id={a.get('id')} · {a.get('action') or ''} · {a.get('createdAt') or ''}",
                "source": "/api/state.approvals", "at": a.get("createdAt") or "",
                "action": "可在下方「待处理审批」直接处理" if a.get("status") == "pending" else "超时单据需重新发起审批",
            })
    if hermes.get("failures"):
        alerts.append({
            "id": "hermes-failures", "level": "warning",
            "title": f"Hermes 调用失败 {hermes.get('failures')} 次",
            "detail": f"累计 {hermes.get('calls')} 次调用，平均时延 {hermes.get('avgLatencyMs')} ms。",
            "source": "/api/state.stats.hermes", "at": _now(),
            "action": "检查上游模型可用性与超时配置",
        })
    for r in [x for x in reports if x.get("kind") == "incident"][-3:]:
        alerts.append({
            "id": f"incident-{r.get('id')}", "level": "info",
            "title": f"CTO 事故报告：{r.get('title') or ''}",
            "detail": str(r.get("conclusion") or "")[:220],
            "source": "/api/reports?kind=incident", "at": r.get("createdAt") or "",
            "action": "阅读结论后决定是否需要巡检派单",
        })
    order = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda x: order.get(x.get("level"), 9))

    token_total = sum(float((v or {}).get("totalTokens") or 0) for v in usage.values())
    data = {
        "ok": True,
        "generatedAt": _now(),
        "service": {
            "ok": bool((health or {}).get("ok", True)),
            "version": (health or {}).get("version") or "3.0",
            "engine": state.get("engine") or (health or {}).get("engine") or "",
            "scene": state.get("scene") or "",
            "serverTime": state.get("serverTime") or "",
            "serverStartedAt": state.get("serverStartedAt") or "",
            "uptimeSec": _uptime(state.get("serverStartedAt")),
            "activeTrace": cost.get("activeTrace") or "",
            "hermes": hermes,
        },
        "stats": {
            "agents": len(state.get("agents") or []),
            "pipelineTotal": len(pipeline),
            "stageCounts": dict(stage_counts),
            "blockedTotal": len(blocked),
            "renderTotal": len(render_items),
            "renderFailed": len(render_failed),
            "approvalPending": len(pending_approvals),
            "hermesCalls": hermes.get("calls") or 0,
            "hermesFailures": hermes.get("failures") or 0,
            "hermesAvgLatencyMs": hermes.get("avgLatencyMs") or 0,
            "tokensRecent": int(token_total),
            "mediaJobs": len(local_jobs),
            "mediaJobsFailed": len(failed_jobs),
            "mediaJobsStuck": len(stuck_jobs),
        },
        "pipeline": {
            "stageCounts": dict(stage_counts),
            "blockedBy": dict(blocked_by),
            "blocked": [{"id": it.get("id"), "title": it.get("title"),
                         "stage": it.get("stage"), "blockedBy": it.get("blockedBy")}
                        for it in blocked[-12:]],
            "recent": [{"id": it.get("id"), "title": it.get("title"),
                        "stage": it.get("stage"), "blockedBy": it.get("blockedBy")}
                       for it in pipeline[-12:]],
        },
        "video": {
            "services": services,
            "localJobsFile": os.path.relpath(JOBS_FILE, REPO_ROOT),
            "localJobsAvailable": jobs_file_ok,
            "localJobs": local_jobs,
            "pipelineRender": render_items,
        },
        "alerts": alerts[:24],
        "actions": {
            "pendingApprovals": [{"id": a.get("id"), "title": a.get("title"),
                                  "action": a.get("action"), "risk": a.get("risk"),
                                  "status": a.get("status"), "createdAt": a.get("createdAt")}
                                 for a in pending_approvals[-8:]],
            "supported": [
                {"action": "patrol", "label": "一键恢复运行", "detail": "触发 CEO 巡检：清理陈旧 trace，按审批门/在跑任务判断，必要时派单全员复工"},
                {"action": "reload", "label": "热加载配置", "detail": "重读 Obsidian 项目配置与员工名册，校验失败保留旧值，不重启服务"},
                {"action": "approve", "label": "批准待审", "detail": "解除 approval 阻塞（仅老板）"},
                {"action": "reject", "label": "驳回待审", "detail": "驳回审批单（仅老板）"},
            ],
            "manualOnly": "重启主服务 / 媒体引擎需运维执行 opc.sh restart 或 systemctl restart，前端不直接触发进程重启",
        },
        "notes": [
            "主服务状态来自 /api/state 与 /api/health；Hermes 统计、成本与流水线均为内存态，重启后归零。",
            "视频任务视图 = 主流水线 render 结果 + media-gen/output/jobs.json 落盘任务；媒体服务在线时额外探测其任务队列。",
            "媒体引擎 :9200 与 media-gen :9103 为独立服务，离线时仍展示历史任务并给出告警。",
        ],
    }
    return data
