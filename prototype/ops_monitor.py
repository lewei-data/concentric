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
import sqlite3
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

# CTO 节点台账：由 ops-patrol-30m 每 30 分钟写入（opsctl.py check）
NODES_DB = os.path.join(REPO_ROOT, "ops", "nodes.db")
PATROL_JOBS = os.environ.get(
    "OPC_PATROL_JOBS", "/opt/sefs/hermes/profiles/cto/cron/jobs.json")
NODE_TTL = 15
_NODES = {"at": 0.0, "data": None}
_NODES_LOCK = threading.Lock()

SERVICES = [
    {"id": "media-engine", "name": "媒体引擎 TTS / ASR / 视频", "url": "http://127.0.0.1:9200",
     "health": "/health", "jobs": "/v1/jobs?limit=20"},
    {"id": "media-gen", "name": "图 / 视频生成任务服务", "url": "http://127.0.0.1:9103",
     "health": "/health", "jobs": "/v1/jobs?limit=20"},
]

# ---------- OpenMontage 渲染渠道 ----------
# 调研结论（2026-11-13）：OpenMontage 渲染走 CLI 子进程（om_render.py 在
# remotion-composer 目录执行 `npx remotion render`），无 HTTP 渲染 API；
# 唯一服务化组件是 Backlot 看板（FastAPI，127.0.0.1:4750，/api/health、/api/projects），
# 属可选常驻服务（未运行时渲染不受影响），故离线按 standby 处理不告警。
OM_ROOT = os.environ.get("OPC_OPENMONTAGE_DIR", "/opt/sefs/OpenMontage")
OM_COMPOSER = os.path.join(OM_ROOT, "remotion-composer")
OM_ENTRY = os.path.join(OM_COMPOSER, "src", "index.tsx")
OM_BACKLOT_URL = os.environ.get("OPC_BACKLOT_URL", "http://127.0.0.1:4750")
# 浏览器打开看板 UI 用的外部可达地址（内网办公段），与探活地址分离；
# 前端跳转时按当前访问方式判断：域名（外网）访问时 21.206/31.206 均不可直达，给提示不跳转
OM_BOARD_URL = os.environ.get("OPC_BACKLOT_BOARD_URL", "http://192.168.31.206:4750")

# ---------- ComfyUI 节点（素材补齐链路依赖 8188 文生图 / 8189 视频） ----------
COMFY_NODES = [
    {"id": "8188", "name": "ComfyUI 出图", "url": "http://192.168.21.157:8188"},
    {"id": "8189", "name": "ComfyUI 视频", "url": "http://192.168.21.157:8189"},
]
COMFY_TTL = 15
_COMFY = {"at": 0.0, "data": None, "runSince": {}}
_COMFY_LOCK = threading.Lock()

# ---------- ComfyUI /ws 实时监听：阶段性产物（预览帧）+ 节点进度 ----------
# ComfyUI 执行期间经 WebSocket 推送 progress/executing 事件与 binary 预览帧
# （KSampler 等节点的中间画面），这里是唯一能“看见执行中阶段性产物”的通道。
try:
    import asyncio as _asyncio

    import websockets as _websockets
except ImportError:
    _websockets = None

_COMFY_WS = {}  # port -> {connected, live: {pid: {value,max,pct,node,done,previewAt}}, curPid, preview, previewAt}
_COMFY_WS_STARTED = False
_COMFY_WS_LOCK = threading.Lock()


def _comfy_ws_loop(port):
    """常驻 WS 客户端：断线 5s 重连，缓存进度事件与最新预览帧。"""

    async def run():
        while True:
            st = _COMFY_WS[port]
            try:
                async with _websockets.connect(
                        f"ws://192.168.21.157:{port}/ws?clientId=opc-ops-{port}",
                        max_size=8 * 1024 * 1024) as ws:
                    st["connected"] = True
                    async for msg in ws:
                        now = time.time()
                        if isinstance(msg, (bytes, bytearray)):
                            # binary 头 8 字节：event_type(1=预览帧)+payload type(1=PNG,2=JPEG)，其后为图像
                            if len(msg) > 8 and msg[0] == 1:
                                st["preview"] = bytes(msg[8:])
                                st["previewAt"] = now
                                pid = st.get("curPid")
                                if pid:
                                    lv = st["live"].setdefault(pid, {})
                                    lv["previewAt"] = now
                            continue
                        try:
                            data = json.loads(msg)
                        except ValueError:
                            continue
                        kind, d = data.get("type"), data.get("data") or {}
                        if kind == "executing" and d.get("prompt_id"):
                            pid = str(d["prompt_id"])
                            st["curPid"] = pid
                            lv = st["live"].setdefault(pid, {"value": 0, "max": 0, "pct": 0})
                            if d.get("node") is None:      # 该 prompt 执行完毕
                                lv["done"] = True
                                lv["doneAt"] = now
                        elif kind == "progress":
                            pid = st.get("curPid")
                            if pid:
                                lv = st["live"].setdefault(pid, {"value": 0, "max": 0, "pct": 0})
                                lv["value"], lv["max"] = d.get("value"), d.get("max")
                                lv["pct"] = round(100 * d["value"] / d["max"]) if d.get("max") else 0
                                lv["node"] = str(d.get("node") or "")
                                lv["updatedAt"] = now
            except Exception:
                st["connected"] = False
            await _asyncio.sleep(5)

    _asyncio.run(run())


def _comfy_ws_start():
    """惰性启动每节点一个 WS 监听线程（daemon，随进程退出）。"""
    global _COMFY_WS_STARTED
    if _websockets is None or _COMFY_WS_STARTED:
        return
    with _COMFY_WS_LOCK:
        if _COMFY_WS_STARTED:
            return
        _COMFY_WS_STARTED = True
        for n in COMFY_NODES:
            port = n["id"]
            _COMFY_WS[port] = {"connected": False, "live": {}, "curPid": None,
                               "preview": None, "previewAt": 0.0}
            threading.Thread(target=_comfy_ws_loop, args=(port,),
                             daemon=True, name=f"comfy-ws-{port}").start()


def _comfy_task(pid, graph):
    """压缩 ComfyUI 任务 graph → 展示摘要（模型/步数/尺寸/提示词/节点类型）。"""
    nodes, model, steps, size, text = [], "", None, "", ""
    for _nid, nd in (graph or {}).items():
        if not isinstance(nd, dict):
            continue
        ct = str(nd.get("class_type") or "")
        nodes.append(ct)
        inputs = nd.get("inputs") or {}
        if not model:
            model = str(inputs.get("ckpt_name") or inputs.get("unet_name")
                        or inputs.get("lora_name") or inputs.get("model_name") or "")
        if steps is None and isinstance(inputs.get("steps"), (int, float)):
            steps = int(inputs["steps"])
        if not size and isinstance(inputs.get("width"), (int, float)) \
                and isinstance(inputs.get("height"), (int, float)):
            size = f"{int(inputs['width'])}x{int(inputs['height'])}"
        if not text and isinstance(inputs.get("text"), str):
            text = inputs["text"]
    return {
        "id": str(pid), "nodes": len(nodes), "types": nodes[:10],
        "model": model, "steps": steps, "size": size,
        "prompt": (text or "").replace("\n", " ").strip()[:160],
    }
OM_TTL = 30
_OM = {"at": 0.0, "data": None}
_OM_LOCK = threading.Lock()


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


def _patrol_next_run():
    """从 CTO 的 30 分钟巡检任务里读下次执行时间；读不到就按 lastCheck + 30m 估算。"""
    try:
        with open(PATROL_JOBS, encoding="utf-8") as f:
            jobs = (json.load(f) or {}).get("jobs") or []
        for job in jobs:
            name = str(job.get("name") or "")
            if "patrol" not in name.lower():
                continue
            minutes = int(((job.get("schedule") or {}).get("minutes")) or 30)
            return str(job.get("next_run_at") or ""), minutes
    except (OSError, ValueError, TypeError):
        pass
    return "", 30


def _add_minutes(ts, minutes):
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return ""
    return (dt + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _nodes_view(force=False):
    """读取 ops/nodes.db（只读）：节点 / 服务最近状态 + 24 小时巡检趋势。"""
    with _NODES_LOCK:
        cached = _NODES["data"]
        fresh = cached is not None and (time.time() - _NODES["at"]) < NODE_TTL
    if fresh and not force:
        return cached

    next_run, cadence = _patrol_next_run()
    data = {
        "available": False,
        "dbPath": os.path.relpath(NODES_DB, REPO_ROOT),
        "cadenceMinutes": cadence,
        "nextCheck": next_run,
        "lastCheck": "",
        "ageMinutes": None,
        "stale": False,
        "summary": {"nodes": 0, "services": 0, "ok": 0, "failed": 0, "criticalFailed": 0},
        "nodes": [],
        "history": [],
    }
    if not os.path.exists(NODES_DB):
        data["error"] = "节点台账尚未生成（ops/nodes.db 不存在），等待 CTO 首轮 30 分钟巡检"
        with _NODES_LOCK:
            _NODES["at"], _NODES["data"] = time.time(), data
        return data

    try:
        con = sqlite3.connect(f"file:{NODES_DB}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            nodes = [dict(r) for r in con.execute("SELECT * FROM nodes ORDER BY id")]
            services = [dict(r) for r in con.execute("SELECT * FROM services ORDER BY node_id, id")]
            patrols = [dict(r) for r in con.execute(
                "SELECT ts,total,ok,failed,summary FROM patrols ORDER BY id DESC LIMIT 48")]
            latest = {r["service_id"]: dict(r) for r in con.execute(
                "SELECT c.service_id, c.status, c.latency_ms, c.detail, c.ts FROM checks c "
                "JOIN (SELECT service_id, MAX(id) AS mid FROM checks GROUP BY service_id) m "
                "ON c.id = m.mid")}
            cutoff = (datetime.now(TZ) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S%z")
            agg = {r["node_id"]: dict(r) for r in con.execute(
                "SELECT s.node_id, COUNT(*) AS total, "
                "SUM(CASE WHEN c.status = 'ok' THEN 1 ELSE 0 END) AS ok "
                "FROM checks c JOIN services s ON s.id = c.service_id "
                "WHERE c.ts >= ? GROUP BY s.node_id", (cutoff,))}
            trend_rows = [dict(r) for r in con.execute(
                "SELECT c.ts, s.node_id, COUNT(*) AS total, "
                "SUM(CASE WHEN c.status = 'ok' THEN 1 ELSE 0 END) AS ok "
                "FROM checks c JOIN services s ON s.id = c.service_id "
                "GROUP BY c.ts, s.node_id ORDER BY c.ts DESC LIMIT 400")]
        finally:
            con.close()
    except (sqlite3.Error, OSError) as e:
        data["error"] = f"读取 nodes.db 失败：{type(e).__name__}: {e}"
        with _NODES_LOCK:
            _NODES["at"], _NODES["data"] = time.time(), data
        return data

    # 趋势按节点归集，按时间升序保留最近 24 轮
    trend = {}
    for row in reversed(trend_rows):  # DESC → 升序
        bucket = trend.setdefault(row["node_id"], [])
        if len(bucket) >= 24:
            bucket.pop(0)
        bucket.append({
            "ts": row["ts"], "total": int(row["total"] or 0), "ok": int(row["ok"] or 0),
            "failed": int((row["total"] or 0) - (row["ok"] or 0)),
        })

    by_node = {}
    for svc in services:
        by_node.setdefault(svc["node_id"], []).append(svc)

    out_nodes = []
    all_latest = []
    for node in nodes:
        svcs = []
        ok = failed = critical_failed = 0
        latencies = []
        last_check = ""
        for svc in by_node.get(node["id"], []):
            chk = latest.get(svc["id"]) or {}
            status = str(chk.get("status") or "unknown")
            if status == "ok":
                ok += 1
            elif status == "fail":
                failed += 1
                if svc.get("criticality") == "high":
                    critical_failed += 1
            latency = chk.get("latency_ms")
            if latency is not None:
                try:
                    latencies.append(float(latency))
                except (TypeError, ValueError):
                    pass
            checked = str(chk.get("ts") or "")
            last_check = max(last_check, checked)
            item = {
                "id": svc["id"], "name": svc["name"], "port": svc.get("port"),
                "proto": svc.get("proto") or "", "criticality": svc.get("criticality") or "mid",
                "status": status, "latencyMs": latency,
                "detail": str(chk.get("detail") or "")[:160], "checkedAt": checked,
            }
            svcs.append(item)
            all_latest.append(item)

        agg_row = agg.get(node["id"]) or {}
        total24 = int(agg_row.get("total") or 0)
        ok24 = int(agg_row.get("ok") or 0)
        out_nodes.append({
            "id": node["id"], "name": node["name"], "host": node.get("host") or "",
            "role": node.get("role") or "", "note": node.get("note") or "",
            "sshAlias": node.get("ssh_alias") or "",
            "status": "fail" if failed else ("unknown" if not svcs else "ok"),
            "total": len(svcs), "ok": ok, "failed": failed,
            "lastCheck": last_check,
            "avgLatencyMs": round(sum(latencies) / len(latencies)) if latencies else None,
            "uptime24h": round(ok24 * 100.0 / total24, 1) if total24 else None,
            "services": svcs,
            "trend": trend.get(node["id"], []),
        })

    last_check = max((n["lastCheck"] for n in out_nodes), default="") or (
        str(patrols[0]["ts"]) if patrols else "")
    total = int(patrols[0]["total"] or 0) if patrols else len(all_latest)
    ok_total = int(patrols[0]["ok"] or 0) if patrols else sum(
        1 for s in all_latest if s["status"] == "ok")

    age_minutes = None
    if last_check:
        try:
            dt = datetime.strptime(last_check, "%Y-%m-%dT%H:%M:%S%z")
            age_minutes = max(0, int((datetime.now(TZ) - dt).total_seconds() // 60))
        except (TypeError, ValueError):
            age_minutes = None

    data.update({
        "available": True,
        "lastCheck": last_check,
        "ageMinutes": age_minutes,
        # 超过 1.5 个周期没写入，说明 30 分钟巡检本身可能停摆
        "stale": age_minutes is not None and age_minutes > cadence * 1.5,
        "nextCheck": next_run or _add_minutes(last_check, cadence),
        "summary": {
            "nodes": len(out_nodes),
            "services": len(services),
            "ok": ok_total,
            "failed": max(0, total - ok_total),
            "criticalFailed": sum(1 for s in all_latest
                                  if s["status"] == "fail" and s["criticality"] == "high"),
        },
        "nodes": out_nodes,
        "history": [
            {"ts": p["ts"], "total": int(p["total"] or 0), "ok": int(p["ok"] or 0),
             "failed": int(p["failed"] or 0), "summary": p.get("summary") or ""}
            for p in reversed(patrols[:24])  # 升序，最近 24 轮（约 12 小时）
        ],
    })
    with _NODES_LOCK:
        _NODES["at"], _NODES["data"] = time.time(), data
    return data


def _openmontage(force=False):
    """OpenMontage 渲染渠道监控：CLI 出片通道健康 + Backlot 看板服务探针。

    - CLI 通道：npx 可执行 + remotion-composer/node_modules + src/index.tsx 入口，
      与 om_render.py / video_compose.py 的可用性判定同口径；任一缺失则出片必失败。
    - Backlot 看板：唯一服务化 API 通道（127.0.0.1:4750），探活并拉取看板项目摘要；
      非常驻必需，离线展示 standby，不产生告警。
    """
    with _OM_LOCK:
        cached = _OM["data"]
        fresh = cached is not None and (time.time() - _OM["at"]) < OM_TTL
    if fresh and not force:
        return cached

    import shutil
    npx = shutil.which("npx") or ""
    checks = {
        "root": os.path.isdir(OM_ROOT),
        "npx": bool(npx),
        "nodeModules": os.path.isdir(os.path.join(OM_COMPOSER, "node_modules")),
        "entry": os.path.isfile(OM_ENTRY),
    }
    missing = [name for name, ok in checks.items() if not ok]
    cli = {
        "available": not missing,
        "npxPath": npx,
        "composerDir": OM_COMPOSER,
        "entry": OM_ENTRY,
        "checks": checks,
        "reason": "" if not missing else "缺失：" + "、".join(
            {"root": "OpenMontage 根目录", "npx": "npx 可执行文件",
             "nodeModules": "remotion-composer/node_modules（未安装依赖）",
             "entry": "渲染入口 src/index.tsx"}[name] for name in missing),
    }
    ok, health = _probe_json(OM_BACKLOT_URL + "/api/health", timeout=1.2)
    projects, projects_note = [], ""
    if ok:
        ok2, payload = _probe_json(OM_BACKLOT_URL + "/api/projects", timeout=1.5)
        if ok2 and isinstance(payload, list):
            projects = [p for p in payload if isinstance(p, dict)]
            projects_note = f"看板项目 {len(projects)} 个"
        else:
            projects_note = f"项目列表不可读（{payload.get('error') or payload.get('http')}）"
    else:
        projects_note = "服务未运行（待机）——渲染走 CLI 通道，不受影响"
    backlot = {
        "url": OM_BACKLOT_URL, "online": ok,
        "boardUrl": OM_BOARD_URL,
        "app": (health or {}).get("app") or "",
        "projectsNote": projects_note,
        "projects": [{
            "id": p.get("project_id") or p.get("id") or "",
            "name": p.get("title") or p.get("name") or p.get("project_id") or "",
            "pipelineType": p.get("pipeline_type") or "",
            "live": bool(p.get("live")),
            "awaitingHuman": bool(p.get("awaiting_human")),
            "activeStage": p.get("active_stage") or "",
            "poster": p.get("poster") or "",
            "lastActivity": _ts(p.get("last_activity")),
            "completedCount": p.get("completed_count"),
            "renderCount": p.get("render_count"),
            "sceneCount": p.get("scene_count"),
            "stageStates": [{"name": s.get("name") or "", "status": s.get("status") or ""}
                            for s in p.get("stage_states") or []],
        } for p in projects[:8]],
    }
    view = {
        "root": OM_ROOT, "channel": "cli", "cli": cli, "backlot": backlot,
        "checkedAt": _now(),
    }
    with _OM_LOCK:
        _OM["at"], _OM["data"] = time.time(), view
    return view


def _comfy(force=False):
    """ComfyUI 节点监控：/system_stats + /queue + /history + /ws 实时进度与预览帧。

    素材补齐链路（asset_fill）依赖 8188 文生图，视频环节依赖 8189；任一离线都应告警。
    """
    _comfy_ws_start()
    with _COMFY_LOCK:
        cached = _COMFY["data"]
        fresh = cached is not None and (time.time() - _COMFY["at"]) < COMFY_TTL
    if fresh and not force:
        return cached

    nodes = []
    for n in COMFY_NODES:
        ok, stats = _probe_json(n["url"] + "/system_stats", timeout=2.0)
        node = {"id": n["id"], "name": n["name"], "url": n["url"], "online": ok}
        if ok and isinstance(stats, dict):
            sysinfo = stats.get("system") or {}
            dev = (stats.get("devices") or [{}])[0] if isinstance(stats.get("devices"), list) else {}
            node.update({
                "version": str(sysinfo.get("comfyui_version") or ""),
                "ramTotal": sysinfo.get("ram_total"),
                "ramFree": sysinfo.get("ram_free"),
                "device": {
                    "name": str(dev.get("name") or ""),
                    "type": str(dev.get("type") or ""),
                    "vramTotal": dev.get("vram_total"),
                    "vramFree": dev.get("vram_free"),
                    "torchVersion": str(dev.get("torch_version") or ""),
                },
            })
            ok2, q = _probe_json(n["url"] + "/queue", timeout=2.0)
            q = q if ok2 and isinstance(q, dict) else {}
            node["queue"] = {
                "running": len(q.get("queue_running") or []),
                "pending": len(q.get("queue_pending") or []),
            }
            # 正在执行的任务：摘要 + 首次见到该 prompt_id 的时间 → 执行耗时
            run_ids = set()
            running = []
            for entry in (q.get("queue_running") or [])[:4]:
                try:
                    pid, graph = entry[1], entry[2] or {}
                except (IndexError, TypeError):
                    continue
                run_ids.add(str(pid))
                task = _comfy_task(str(pid), graph)
                task["elapsed"] = int(time.time() - _COMFY["runSince"].setdefault(
                    f"{n['id']}:{pid}", time.time()))
                # /ws 实时通道：节点级进度 + 最新预览帧可用标记（阶段性产物）
                st = _COMFY_WS.get(n["id"]) or {}
                lv = (st.get("live") or {}).get(str(pid)) or {}
                task["progress"] = {
                    "pct": lv.get("pct") or 0,
                    "value": lv.get("value") or 0,
                    "max": lv.get("max") or 0,
                    "node": lv.get("node") or "",
                    "preview": bool(st.get("preview")) and st.get("curPid") == str(pid),
                }
                running.append(task)
            for key in list(_COMFY["runSince"]):
                if not key.startswith(f"{n['id']}:") or key[len(n['id']) + 1:] not in run_ids:
                    _COMFY["runSince"].pop(key, None)
            node["running"] = running
            node["pending"] = [
                _comfy_task(str(e[1]), e[2] or {})
                for e in (q.get("queue_pending") or [])[:3] if len(e) > 2
            ]
            ok3, h = _probe_json(n["url"] + "/history?max_items=6", timeout=2.0)
            recent = []
            if ok3 and isinstance(h, dict):
                for hid, item in list(h.items())[-6:]:
                    st = (item or {}).get("status") or {}
                    files = []
                    for _nid, out in ((item or {}).get("outputs") or {}).items():
                        if not isinstance(out, dict):
                            continue
                        for key in ("images", "videos", "audio", "gifs", "3d"):
                            for f in out.get(key) or []:
                                if isinstance(f, dict) and f.get("filename"):
                                    # ComfyUI 常把 mp4/webm 也塞进 images 键，按扩展名定真实类型
                                    fn = str(f.get("filename")).lower()
                                    if fn.endswith((".mp4", ".webm", ".mkv", ".mov")):
                                        kind = "video"
                                    elif fn.endswith((".mp3", ".wav", ".flac", ".m4a")):
                                        kind = "audio"
                                    elif fn.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                                        kind = "image"
                                    else:
                                        kind = key.rstrip("s")
                                    files.append({
                                        "filename": str(f.get("filename")),
                                        "subfolder": str(f.get("subfolder") or ""),
                                        "type": str(f.get("type") or "output"),
                                        "kind": kind,
                                    })
                    recent.append({
                        "id": str(hid),
                        "status": str(st.get("status_str") or ""),
                        "outputs": len(files),
                        "files": files[:10],
                    })
            node["recent"] = recent
            ws_st = _COMFY_WS.get(n["id"]) or {}
            node["ws"] = {"connected": bool(ws_st.get("connected")),
                          "liveCount": len(ws_st.get("live") or {})}
        else:
            node["error"] = str((stats or {}).get("error") or "连接失败")
        nodes.append(node)
    view = {"available": any(n.get("online") for n in nodes),
            "nodes": nodes, "checkedAt": _now()}
    with _COMFY_LOCK:
        _COMFY["at"], _COMFY["data"] = time.time(), view
    return view


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


def build(state, health, force_probe=False, runs=None, llm=None):
    """返回 /api/ops 的响应体；state 为 Kernel.snapshot()，runs 为流水线运行记录，
    llm 为大模型注册表视图（server._llm_view()）。"""
    state = state or {}
    runs = runs or []
    break_runs = [r for r in runs if r.get("status") in ("interrupted", "timeout")]
    pipeline = state.get("pipeline") or []
    approvals = state.get("approvals") or []
    reports = state.get("reports") or []
    hermes = (state.get("stats") or {}).get("hermes") or {}
    cost = state.get("cost") or {}
    usage = cost.get("usage") or {}
    aborted = cost.get("aborted") or {}

    services = _services(force_probe)
    node_view = _nodes_view(force_probe)
    local_jobs, jobs_file_ok = _local_jobs()
    render_items = _render_view(pipeline)
    om_view = _openmontage(force_probe)
    comfy_view = _comfy(force_probe)
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
    # 节点台账里的 30 分钟巡检异常（以 SQLite 最近一次 check 为准）
    if node_view.get("available"):
        if node_view.get("stale"):
            alerts.append({
                "id": "node-patrol-stale", "level": "warning",
                "title": f"节点巡检 {node_view.get('ageMinutes')} 分钟未更新",
                "detail": f"最近一次巡检 {node_view.get('lastCheck') or '—'}，"
                          f"已超过 {node_view.get('cadenceMinutes')} 分钟周期；"
                          "页面显示的是历史快照，不代表节点当前状态。",
                "source": "ops/nodes.db", "at": node_view.get("lastCheck") or "",
                "action": "检查 CTO 定时任务 ops-patrol-30m 与 scripts/patrol_tick.sh 是否正常执行",
            })
        for node in node_view.get("nodes") or []:
            for svc in node.get("services") or []:
                if svc.get("status") != "fail":
                    continue
                critical = svc.get("criticality") == "high"
                alerts.append({
                    "id": f"node-{node['id']}-{svc['id']}", "level": "critical" if critical else "warning",
                    "title": f"{node['name']} · {svc['name']} 巡检异常",
                    "detail": f"{node['host']}:{svc.get('port')}（{svc.get('proto')}）"
                              f"最近一次检查 {svc.get('checkedAt') or '—'}：{svc.get('detail') or '探测失败'}",
                    "source": f"ops/nodes.db · {node['id']}/{svc['id']}", "at": svc.get("checkedAt") or "",
                    "action": "SSH 到节点确认服务进程 / 端口，修复后等下一轮 30 分钟巡检自动恢复",
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
    # OpenMontage 渲染渠道：CLI 通道不可用意味着所有样片/成片渲染必然失败
    if om_view["cli"].get("available") is False:
        alerts.append({
            "id": "om-cli-unavailable", "level": "critical",
            "title": "OpenMontage 出片通道不可用",
            "detail": f"{om_view['cli'].get('reason')}（{om_view['cli'].get('composerDir')}）",
            "source": "OpenMontage CLI 探测", "at": om_view.get("checkedAt") or "",
            "action": "安装 npx 与 remotion-composer 依赖（npm install），确认渲染入口存在",
        })
    # ComfyUI 节点：素材补齐（8188 文生图）与视频生成（8189）的执行引擎
    for n in comfy_view.get("nodes") or []:
        if not n.get("online"):
            alerts.append({
                "id": f"comfy-{n.get('id')}", "level": "warning",
                "title": f"{n.get('name')} 节点离线",
                "detail": f"{n.get('url')} 探测失败：{n.get('error') or '连接被拒绝'}。"
                          "依赖该节点的素材补齐/视频生成任务将无法执行。",
                "source": "ComfyUI 探测", "at": comfy_view.get("checkedAt") or "",
                "action": "SSH 到 192.168.21.157 确认 ComfyUI 服务进程与显存状态",
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
    for r in break_runs[:3]:
        alerts.append({
            "id": f"run-{r.get('traceId')}", "level": "warning",
            "title": f"运行断点：{r.get('traceId')} 停在 {r.get('stage') or '—'}",
            "detail": f"状态 {r.get('status')} · 更新于 {r.get('updatedAt') or ''}"
                      f" · 已自动重试 {r.get('resumeTries') or 0} 次",
            "source": "/api/ops.runs", "at": r.get("updatedAt") or "",
            "action": "可在下方「运行断点」点「续跑」从该阶段恢复",
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
    # 大模型注册表：停用 / 额度耗尽的模型会直接拦截 Hermes 调用，必须醒目提示
    llm = llm or {}
    for m in llm.get("models") or []:
        status = str(m.get("status") or "enabled")
        if status == "enabled":
            continue
        avail = m.get("availableTokens")
        avail_txt = "不限" if avail == -1 else f"{avail or 0}"
        alerts.append({
            "id": f"llm-{m.get('id')}", "level": "critical" if status == "exhausted" else "warning",
            "title": (f"大模型额度耗尽：{m.get('name')}" if status == "exhausted"
                      else f"大模型已停用：{m.get('name')}"),
            "detail": (f"可用 token {avail_txt}（已用 {m.get('usedTokens') or 0}"
                       f" / 总额 {m.get('totalTokens') or '不限'}）"
                       f" · 最后变更 {m.get('updatedAt') or '—'}（{m.get('updatedBy') or '—'}）"),
            "source": "过程库 llm_models", "at": m.get("updatedAt") or "",
            "action": ("在「大模型开关」追加总 token 额度即可自动恢复调用"
                       if status == "exhausted"
                       else "在「大模型开关」重新启用，否则 Hermes 调用将全部降级为规则模式"),
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
            "omChannelOk": 1 if om_view["cli"].get("available") else 0,
            "omBacklotOnline": 1 if om_view["backlot"].get("online") else 0,
            "omBacklotProjects": len(om_view["backlot"].get("projects") or []),
            "comfyOnline": sum(1 for n in comfy_view.get("nodes") or [] if n.get("online")),
            "nodesTotal": (node_view.get("summary") or {}).get("nodes", 0),
            "nodesFailed": (node_view.get("summary") or {}).get("failed", 0),
            "runsTotal": len(runs),
            "runsInterrupted": len(break_runs),
            "llmTotal": len(llm.get("models") or []),
            "llmEnabled": llm.get("enabled") or 0,
            "llmExhausted": llm.get("exhausted") or 0,
            "llmDisabled": llm.get("disabled") or 0,
        },
        "runs": runs,
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
            # 尚无出片结果、卡在门禁的内容：这些就是「视频生成环节」当前的实际待办
            "pendingRender": [
                {"id": it.get("id") or "", "title": it.get("title") or "",
                 "stage": it.get("stage") or "", "blockedBy": it.get("blockedBy") or ""}
                for it in pipeline if it.get("blockedBy")
            ][-12:],
        },
        "openmontage": om_view,
        "comfy": comfy_view,
        "nodes": node_view,
        "llm": llm if llm else {"available": False, "models": []},
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
                {"action": "resume", "label": "断点续跑", "detail": "按过程库断点阶段恢复指定 trace（仅老板）；中断/超时的运行记录见「运行断点」"},
            ],
            "manualOnly": "重启主服务 / 媒体引擎需运维执行 opc.sh restart 或 systemctl restart，前端不直接触发进程重启",
        },
        "notes": [
            "主服务状态来自 /api/state 与 /api/health；Hermes 统计、成本与流水线均为内存态，重启后归零。",
            "大模型注册表来自过程库 llm_models 表：Hermes 每次调用前检查开关与额度，成功后按实际用量扣减；额度耗尽自动停用，CTO 追加总额度后自动恢复。",
            "视频任务视图 = 主流水线 render 结果 + media-gen/output/jobs.json 落盘任务；媒体服务在线时额外探测其任务队列。",
            "媒体引擎 :9200 与 media-gen :9103 为独立服务，离线时仍展示历史任务并给出告警。",
            "OpenMontage 渲染走 CLI 子进程（npx remotion render，工作目录 remotion-composer），无 HTTP 渲染 API；"
            "页面展示 CLI 通道健康（npx/依赖/入口）与唯一服务化组件 Backlot 看板（127.0.0.1:4750）的探活结果，"
            "看板属可选服务，离线按待机处理不告警。",
            "节点监控来自 CTO 节点台账 ops/nodes.db（SQLite）：ops-patrol-30m 每 30 分钟执行 opsctl.py check，写入 nodes / services / checks / patrols；页面只读展示，不触发巡检。",
        ],
    }
    return data
