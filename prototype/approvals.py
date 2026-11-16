"""审批门与断点续跑（C1 第四刀）：审批巡检、决策、重启恢复、自动续跑、CEO 巡检。

从 kernel.Kernel 抽出，全部函数以 Kernel 实例为第一参数。不 import kernel，
kernel 单向依赖本模块（薄委托），因此不成环。

依赖的 kernel 属性：lock / approvals / approval_events / db / traces / active_trace /
agents / projects / scene / stats / last_hotspot / _sweep_stop，
以及实例方法 emit / send / run_pipeline / inject_hotspot / call_agent。
"""
import json
import threading
import time
from datetime import datetime

from obs import log_event
from opc_paths import *  # noqa: F401,F403 —— APPROVAL_TIMEOUT / TZ
from utils import iso_now, uid


AUTO_RESUME_MAX_TRIES = 3


def _approval_sweeper(kn):
    """每 15 秒巡检一次 pending 审批，超时自动挂起并唤醒等待方。"""
    while not kn._sweep_stop.wait(15):
        try:
            _sweep_approvals(kn)
        except Exception as e:
            print(f"[approval] 超时巡检异常：{type(e).__name__}: {e}", flush=True)


def _sweep_approvals(kn):
    now = datetime.now(TZ)
    expired = []
    with kn.lock:
        for a in kn.approvals:
            if a.get("status") != "pending":
                continue
            try:
                created = datetime.fromisoformat(a.get("createdAt") or "")
                if created.tzinfo is None:      # 历史单可能无时区：按本机时区补，避免整轮巡检 TypeError
                    created = created.replace(tzinfo=TZ)
            except (TypeError, ValueError):
                continue
            limit = a.get("timeoutSec") or APPROVAL_TIMEOUT
            if (now - created).total_seconds() < limit:
                continue
            a["status"] = "timeout"
            a["decidedAt"] = iso_now()
            a["timeoutReason"] = f"等待老板审批超过 {limit}s，已按挂起处理（巡检）"
            expired.append(a)
    for a in expired:
        kn.emit("approval", a.get("traceId", ""), a)
        ev = kn.approval_events.get(a["id"])
        if ev:
            ev.set()
        kn.send("escalation", "ceo", "owner", a.get("traceId", ""),
                  {"text": f"审批超时挂起：{a.get('title') or a.get('action')}"}, priority=2)


def _restore_pending_approvals(kn):
    """重启后把库里仍 pending 的审批单拉回内存，老板打开工作台就能看到待办。"""
    if not kn.db:
        return
    try:
        rows = kn.db.approvals_pending()
    except Exception as e:
        print(f"[boot] 读取待审批失败：{type(e).__name__}: {e}", flush=True)
        return
    known = {a.get("id") for a in kn.approvals}
    added = 0
    for row in rows:
        try:
            raw = json.loads(row.get("raw") or "{}")
        except ValueError:
            raw = {}
        raw.setdefault("id", row.get("id"))
        raw.setdefault("traceId", row.get("trace_id"))
        raw.setdefault("action", row.get("action"))
        raw.setdefault("status", "pending")
        raw.setdefault("createdAt", row.get("created_at"))
        if raw["id"] in known:
            continue
        if not raw.get("timeoutSec"):
            raw["timeoutSec"] = APPROVAL_TIMEOUT
        kn.approvals.append(raw)
        added += 1
    if added:
        print(f"[boot] 从过程库恢复 {added} 条待审批单", flush=True)


def _mark_interrupted_runs(kn):
    """启动时把上次进程留下的 running 标成 interrupted；按需串行自动续跑。

    续跑候选同时包含 running（上次进程直接挂掉）与 interrupted（已标记但当时
    没续成，比如多条断点抢不到执行位）：只要还有断点阶段、且续跑次数未超上限，
    这次重启就再试一次。续跑必须串行——内核同一时刻只允许一条 trace 运行。
    """
    if not kn.db:
        return
    try:
        rows = kn.db.pipeline_runs_resumable(AUTO_RESUME_MAX_TRIES)
    except Exception as e:
        print(f"[boot] 读取运行态失败：{type(e).__name__}: {e}", flush=True)
        return
    pending = []
    for row in rows:
        trace = row.get("trace_id")
        if not trace:
            continue
        if row.get("status") == "running":
            try:
                kn.db.pipeline_run_end(trace, "interrupted")
            except Exception as e:
                log_event("_mark_interrupted_runs.pipeline_run_end", e, level="error", trace=trace)
            with kn.lock:
                if kn.traces.get(trace, {}).get("status") == "running":
                    kn.traces[trace]["status"] = "interrupted"
                    kn.traces[trace]["closedAt"] = iso_now()
            print(f"[boot] 上次未跑完：{trace} 停在阶段 {row.get('stage')}（已标记 interrupted）",
                  flush=True)
        pending.append(trace)
    if pending and os.environ.get("OPC_RESUME_AUTO", "0") == "1":
        threading.Thread(target=_auto_resume_loop, args=(kn, pending), daemon=True).start()


def _auto_resume_loop(kn, traces):
    """按顺序逐条续跑：一条收口后再拉下一条，避免 active_trace 互斥导致静默失败。"""
    for trace in traces:
        try:
            kn.db.pipeline_resume_try(trace)
            info = resume_pipeline(kn, trace, auto=True)
            print(f"[boot] 自动续跑 {trace}：从阶段 {info.get('from')} 恢复", flush=True)
        except Exception as e:
            print(f"[boot] 自动续跑失败 {trace}：{type(e).__name__}: {e}", flush=True)
            continue
        _wait_run_done(kn, trace)
    print(f"[boot] 自动续跑队列处理完毕（{len(traces)} 条）", flush=True)


def _wait_run_done(kn, trace, poll=2.0):
    """等这条 trace 收口再处理下一条（先等线程起来，再等它退出）。"""
    deadline = time.time() + 30
    while time.time() < deadline and kn.active_trace != trace:
        time.sleep(poll)
    while kn.active_trace == trace:
        time.sleep(poll)


def resume_pipeline(kn, trace, auto=False):
    """按过程库里的断点阶段续跑（至少一次：重启时该阶段会重跑一次）。"""
    if not kn.db:
        raise RuntimeError("过程库未启用（OPC_DB=0），无法续跑")
    row = kn.db.pipeline_run(trace)          # 先查库（锁外），再做锁内抢占
    if not row:
        raise RuntimeError("过程库里没有这条 trace 的运行记录")
    if row.get("status") == "completed":
        raise RuntimeError("该 trace 已完成，无需续跑")
    if not row.get("stage"):
        raise RuntimeError("该 trace 还没有阶段断点，无法续跑")
    hotspot = json.loads(row.get("hotspot") or "null") or kn.last_hotspot \
        or {"title": "断点续跑", "tags": []}
    state = json.loads(row.get("state") or "{}")
    # 占位与检查必须原子：锁外"先查 active_trace 再起线程"时，两个并发续跑都能通过检查，
    # 同一 trace 被两条流水线同时推进（TOCTOU），表现为阶段错乱/重复交付
    with kn.lock:
        if kn.active_trace:
            if kn.active_trace == trace:
                raise RuntimeError(f"该任务正在运行（{trace}），无需重复续跑")
            raise RuntimeError(f"已有任务在跑（{kn.active_trace}），稍后再续跑")
        kn.active_trace = trace
        tr = kn.traces.setdefault(trace, {"traceId": trace, "createdAt": iso_now(),
                                           "hotspot": hotspot})
        tr["status"] = "running"
        tr.pop("closedAt", None)
        tr["resumedAt"] = iso_now()
    kn.send("status", "ceo", "owner", trace,
              {"text": f"按断点续跑：从阶段 {row.get('stage')} 恢复"}, priority=2)
    try:
        threading.Thread(target=kn.run_pipeline, args=(trace, hotspot),
                         kwargs={"start": row.get("stage"), "state": state}, daemon=True).start()
    except Exception:
        with kn.lock:            # 线程没起来必须归还占位，否则这条 trace 永远续不了
            if kn.active_trace == trace:
                kn.active_trace = ""
        raise
    return {"traceId": trace, "from": row.get("stage"), "auto": auto}


def _patrol_note(kn, result, facts):
    """巡检结论：让 CEO 基于真实状态写一段结论并推回事件流（异步，不阻塞巡检接口）"""
    try:
        data, meta = kn.call_agent(
            "ceo",
            "写一段本次两小时巡检结论：当前进度、是否有员工空转、下一动作。只用给定事实，不许编造数字。",
            facts, "", "summary(≤120字，直接向老板说), evidence(2-4条，引用给定事实), actions(1-3条), confidence(0到1)")
        if not data:
            return
        data.setdefault("summary", "")
        kn.send("result", "ceo", "owner", result.get("traceId", ""),
                  {"result": data, "engine": meta["engine"], "latencyMs": meta["latencyMs"],
                   "patrol": result.get("action")}, priority=1)
    except Exception as exc:
        kn.send("escalation", "ceo", "owner", "", {"text": f"巡检结论生成失败：{exc}"}, priority=1)


def _patrol_facts(kn):
    """在动作发生之后才采集事实，避免模型拿到过期状态"""
    with kn.lock:
        running = [t for t, d in kn.traces.items() if d.get("status") == "running"]
        pending = [a for a in kn.approvals if a.get("status") == "pending"]
        idle = [a["id"] for a in kn.agents.values() if a.get("status") == "idle"]
        working = [a["id"] for a in kn.agents.values() if a.get("status") == "working"]
        kpi = "；".join(f"{kn.projects[k]['name']}={kn.projects[k]['kpi']}" for k in kn.projects)
        return (f"场景={kn.scene}；在岗={working or '无'}；空闲={len(idle)}人；"
                f"运行中trace={running[-1] if running else '无'}；待审批={len(pending)}；"
                f"累计trace={kn.stats.get('traces', 0)}；消息数={kn.events}；项目KPI：{kpi}")


# ---------- 业务入口 ----------
def _sweep_stale(kn):
    """兜底收口：超过 45 分钟仍 running、且没有待审批的 trace 视为超时，不再占用派单。"""
    now = datetime.now(TZ)
    with kn.lock:
        pending_tr = {a["traceId"] for a in kn.approvals if a.get("status") == "pending"}
        for d in kn.traces.values():
            if d.get("status") != "running" or d.get("traceId") in pending_tr:
                continue
            try:
                began = datetime.fromisoformat(d["startedAt"])
                if began.tzinfo is None:
                    began = began.replace(tzinfo=TZ)
                age = (now - began).total_seconds()
            except Exception:
                continue
            if age > 2700:
                d["status"] = "timeout"
                d["closedAt"] = iso_now()


def patrol(kn):
    """CEO 每两小时巡检：看进度 / 找空闲员工 / 无事可做就派新任务"""
    _sweep_stale(kn)
    with kn.lock:
        running = [t for t, d in kn.traces.items() if d.get("status") == "running"]
        pending = [a for a in kn.approvals if a.get("status") == "pending"]
        idle = [a["id"] for a in kn.agents.values() if a.get("status") == "idle"]
    # 审批门优先：流水线会阻塞在审批上（trace 仍显示 running），此时该催的是老板而不是“继续等”
    if pending:
        ap = pending[0]
        note = (f"巡检：{len(pending)} 个审批待老板拍板（{ap['id']} / {ap.get('action')}），"
                "AI 不得自行开工或发布，已停止派单")
        kn.send("escalation", "ceo", "owner", "", {"text": note, "approvalId": ap["id"]}, priority=2)
        result = {"action": "await-approval", "approvalId": ap["id"], "idle": idle}
        facts = _patrol_facts(kn) + f"；本次动作=停止派单，等待老板审批 {ap['id']}"
        threading.Thread(target=_patrol_note, args=(kn, result, facts), daemon=True).start()
        return result
    if running:
        note = f"巡检：trace {running[-1]} 仍在运行，本轮不派单；{len(idle)} 人空闲待命"
        kn.send("heartbeat", "ceo", "owner", running[-1], {"text": note, "idle": len(idle)}, priority=0)
        result = {"action": "busy", "traceId": running[-1], "idle": idle, "pendingApprovals": len(pending)}
        facts = _patrol_facts(kn) + "；本次动作=不派单（已有 trace 在跑）"
        threading.Thread(target=_patrol_note, args=(kn, result, facts), daemon=True).start()
        return result
    hs = kn.last_hotspot or {}
    if hs.get("title"):
        title = hs["title"] + "（二轮深挖）"
        tags, score = hs.get("tags") or ["综合"], min(0.95, (hs.get("score") or 0.8) + 0.02)
    else:
        title, tags, score = None, None, None
    trace, hotspot = kn.inject_hotspot(title, tags, score, force=True)
    title = hotspot["title"]
    assign = {"scout": "复核热点与素材包", "pm_wenren": "出文人莫克选题卡", "pm_gou": "出狗大人选题卡",
              "pm_tongxinyuan": "出同心圆选题卡", "cw_wenren": "写稿", "cw_gou": "写稿",
              "cw_tongxinyuan": "写产品文案", "vd_wenren": "出片", "vd_gou": "出片",
              "vd_tongxinyuan": "出演示视频", "reviewer": "跨项目串味审核",
              "release_ops": "备发布与监控", "kb_manager": "归档", "cto": "技术周报", "ceo": "日报"}
    kn.send("delegation", "ceo", "all", trace,
              {"task": f"全员复工：{title}（空闲 {len(idle)} 人已按岗位分派）", "assignments": assign}, priority=1)
    result = {"action": "dispatched", "traceId": trace, "hotspot": title, "idle": idle}
    facts = (_patrol_facts(kn)
             + f"；本次动作=已派单「{title}」并下发全员分工，员工正在进入在岗（上表的在岗/空闲为空闲态排查前的值）")
    threading.Thread(target=_patrol_note, args=(kn, result, facts), daemon=True).start()
    return result


def _open_approval(kn, c, draft):
    """审批单幂等开单：同一 trace + 动作已有单就复用，重启续跑不会重复打扰老板。

    返回 (审批单, 唤醒事件 or None, 是否新建)。
    - 已有已决单：不新建、不等待（事件为 None）
    - 已有待决单：复用记录并重新登记唤醒事件，等待老板处理
    - 无记录：与改造前一致，新建 + 推送 + 写库
    """
    existing = None
    if kn.db:
        try:
            existing = kn.db.approval_for(c.trace, draft["action"])
        except Exception as e:
            print(f"[approval] 读库失败，按新单处理：{type(e).__name__}: {e}", flush=True)
    with kn.lock:
        if not existing:
            kn.approvals.append(draft)
            ev = threading.Event()
            kn.approval_events[draft["id"]] = ev
            row, created = draft, True
        else:
            row = next((a for a in kn.approvals if a["id"] == existing["id"]), None)
            if row is None:
                row = dict(existing.get("raw") or {})
                for k in ("id", "traceId", "action", "status", "createdAt", "decidedAt", "timeoutSec"):
                    if existing.get(k) not in (None, ""):
                        row[k] = existing[k]
                kn.approvals.append(row)
            if row.get("status") != "pending":
                return row, None, False
            ev = kn.approval_events.get(row["id"]) or threading.Event()
            kn.approval_events[row["id"]] = ev
            created = False
    # 新建单与“重启后重新挂起”都推一次提醒，保证老板能看到待办
    kn.send("approval", "ceo", "owner", c.trace,
              {"approval": row} if created else {"approval": row, "reopened": True}, priority=2)
    kn.emit("approval", c.trace, row)
    return row, ev, created


# ---------- 审批 ----------
def decide_approval(kn, approval_id, decision, decided_by=""):
    with kn.lock:
        appr = next((a for a in kn.approvals if a["id"] == approval_id), None)
    if not appr and kn.db:
        appr = _approval_from_db(kn, approval_id)   # 重启后内存里没有，从过程库补（锁外查库）
    if not appr:
        return None
    # 查找→判状态→写状态必须一次锁内完成：锁外“先判后写”时，两个并发请求会同时看到
    # pending，同一张单被批两次、续跑被触发两次（TOCTOU）
    with kn.lock:
        cur = next((a for a in kn.approvals if a["id"] == appr["id"]), None)
        if cur is None:      # 从库补出来的单此前不在内存里，补进去让续跑逻辑能看到最新状态
            kn.approvals.append(appr)
            cur = appr
        if cur.get("status") != "pending":
            # 超时挂起的单允许老板迟到补批：补批后从断点续跑，不用整轮重来
            if not (cur.get("status") == "timeout" and decision == "approve"):
                return cur
        cur["status"] = "approved" if decision == "approve" else "rejected"
        cur["decidedAt"] = iso_now()
        cur["decidedBy"] = decided_by or cur.get("approver", "")
        try:
            created = datetime.fromisoformat(cur["createdAt"])
            decided = datetime.fromisoformat(cur["decidedAt"])
            kn.stats["approvalSeconds"].append(round((decided - created).total_seconds(), 1))
        except Exception as e:
            log_event("decide_approval.duration", e, level="debug", quiet=True, approvalId=approval_id)
        if decision != "approve" and cur.get("action") == "publish.execute":
            kn.stats["blockedPublish"] += 1
        appr = cur
    kn.emit("approval", appr["traceId"], appr)
    ev = kn.approval_events.get(approval_id)
    if ev:
        ev.set()
    elif decision == "approve":
        # 没有等待线程 = 进程重启后恢复的审批单，run 仍停在断点，放行后自动续跑
        _resume_if_interrupted(kn, appr["traceId"])
    return appr


def _resume_if_interrupted(kn, trace):
    """审批放行后自动续跑：仅当该 trace 在过程库里停在断点（interrupted / 审批超时）。"""
    if not kn.db or not trace:
        return False
    try:
        run = kn.db.pipeline_run(trace)
    except Exception as e:
        print(f"[approval] 读运行态失败，跳过自动续跑：{type(e).__name__}: {e}", flush=True)
        return False
    if not run or run.get("status") not in ("interrupted", "timeout"):
        return False
    with kn.lock:
        if kn.traces.get(trace, {}).get("status") == "running":
            return False                     # 已经在续跑，避免重复拉起
    try:
        info = resume_pipeline(kn, trace, auto=True)
    except Exception as e:
        print(f"[approval] 自动续跑失败 {trace}：{type(e).__name__}: {e}", flush=True)
        return False
    print(f"[approval] {trace} 审批放行后自动续跑：从阶段 {info.get('from')} 恢复", flush=True)
    return True


def _approval_from_db(kn, approval_id):
    """从过程库补一张待决审批单到内存（进程重启后审批仍可被处理）。"""
    try:
        rows = kn.db.approvals_pending()
    except Exception:
        return None
    row = next((r for r in rows if r.get("id") == approval_id), None)
    if not row:
        return None
    try:
        raw = json.loads(row.get("raw") or "{}")
    except ValueError:
        raw = {}
    raw.setdefault("id", approval_id)
    raw.setdefault("traceId", row.get("trace_id"))
    raw.setdefault("action", row.get("action"))
    raw.setdefault("createdAt", row.get("created_at"))
    raw["status"] = "pending"
    raw.setdefault("timeoutSec", APPROVAL_TIMEOUT)
    with kn.lock:
        kn.approvals.append(raw)
    return raw


