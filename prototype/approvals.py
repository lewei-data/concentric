"""审批门与断点续跑（C1 第四刀）：审批巡检、决策、重启恢复、自动续跑、CEO 巡检。

从 kernel.Kernel 抽出，全部函数以 Kernel 实例为第一参数。不 import kernel，
kernel 单向依赖本模块（薄委托），因此不成环。

依赖的 kernel 属性：lock / approvals / approval_events / db / traces / active_trace /
agents / projects / scene / stats / last_hotspot / _sweep_stop，
以及实例方法 emit / send / run_pipeline / inject_hotspot / call_agent。
"""
import json
import os
import threading
import time
from datetime import datetime

import capacity
from obs import log_event
from opc_paths import *  # noqa: F401,F403 —— APPROVAL_TIMEOUT / TZ
from utils import iso_now, uid


AUTO_RESUME_MAX_TRIES = 3

# ---------- CEO 超时代审批（老板 2026-11-16 授权） ----------
# 老板超过审批时限未处理（status=timeout）的单据，由 CEO 酌情代批：
#   - 有依据才放行（成本/素材/合规检查无 fail、历史同类通过率、产能缺口）
#   - 置信度不足或解析失败一律不代批，升级老板
#   - 全程限速串行，避免一次性放行大批挂起单把流水线挤爆
CEO_PROXY_ON = os.environ.get("OPC_CEO_PROXY_APPROVE", "1") == "1"
CEO_PROXY_MIN_CONF = float(os.environ.get("OPC_CEO_PROXY_MIN_CONF", "0.6"))
CEO_PROXY_GAP_SEC = int(os.environ.get("OPC_CEO_PROXY_GAP_SEC", "300"))
CEO_PROXY_MAX_TRIES = 2
PROXY_ACTOR = "CEO 陆清安（超时代批）"
PROXY_CC = "secretary@mail.conring.cn"
PROXY_MAIL_GAP = int(os.environ.get("OPC_CEO_PROXY_MAIL_GAP", "1800"))

_proxy_lock = threading.Lock()
_proxy_state = {"lastAt": 0.0, "tries": {}, "approved": 0, "rejected": 0, "declined": 0}
_proxy_mail_at = {"lastAt": 0.0}


def _proxy_claim_slot():
    """全局节流：两次代批之间至少间隔 CEO_PROXY_GAP_SEC 秒（跨线程共享）。"""
    with _proxy_lock:
        now = time.time()
        if now - _proxy_state["lastAt"] < CEO_PROXY_GAP_SEC:
            return False
        _proxy_state["lastAt"] = now
        return True


def _disk_status(kn, approval_id):
    """以库为准的审批状态（内存快照可能滞后于已决策的单）。读不到返回空串。"""
    if not kn.db:
        return ""
    try:
        row = kn.db.fetch_one("SELECT raw FROM approvals WHERE id=?", (approval_id,))
        return (json.loads((row or {}).get("raw") or "{}") or {}).get("status") or ""
    except Exception:
        return ""


def _proxy_due(approval_id):
    """同一张单最多尝试 CEO_PROXY_MAX_TRIES 次，避免模型抖动无限重试烧钱。"""
    with _proxy_lock:
        return _proxy_state["tries"].get(approval_id, 0) < CEO_PROXY_MAX_TRIES


def _proxy_count(approval_id):
    with _proxy_lock:
        _proxy_state["tries"][approval_id] = _proxy_state["tries"].get(approval_id, 0) + 1


def ceo_proxy_status():
    with _proxy_lock:
        return {"enabled": CEO_PROXY_ON, "minConfidence": CEO_PROXY_MIN_CONF,
                "gapSec": CEO_PROXY_GAP_SEC, "maxTries": CEO_PROXY_MAX_TRIES,
                "approved": _proxy_state["approved"], "rejected": _proxy_state["rejected"],
                "declined": _proxy_state["declined"],
                "lastAt": (datetime.fromtimestamp(_proxy_state["lastAt"], TZ).isoformat()
                           if _proxy_state["lastAt"] else "")}


def _ceo_proxy_kick(kn, rows=None):
    """把已超时的审批单交给 CEO 代审（异步串行 + 限速）。返回排队数量。"""
    if not CEO_PROXY_ON:
        return {"ok": False, "queued": 0, "reason": "CEO 代审已关闭（OPC_CEO_PROXY_APPROVE=0）"}
    with _proxy_lock:
        if time.time() - _proxy_state["lastAt"] < CEO_PROXY_GAP_SEC:
            return {"ok": True, "queued": 0,
                    "reason": f"限速中（每 {CEO_PROXY_GAP_SEC}s 最多代批 1 单）"}
    with kn.lock:
        cand = [a for a in (rows if rows is not None else kn.approvals)
                if a.get("status") == "timeout"]
    ids = [a["id"] for a in cand
           if _proxy_due(a["id"]) and _disk_status(kn, a["id"]) in ("", "timeout")]
    if not ids:
        return {"ok": True, "queued": 0, "reason": "已超时的单均已代审过或配额用尽"}
    threading.Thread(target=_ceo_proxy_worker, args=(kn, ids), daemon=True).start()
    return {"ok": True, "queued": len(ids), "ids": ids[:5]}


def _ceo_proxy_worker(kn, ids):
    for idx, aid in enumerate(ids):
        try:
            r = _ceo_proxy_decide(kn, aid)
        except Exception as e:           # 单张失败不影响后续
            log_event("ceo_proxy_decide", e, level="error", approvalId=aid)
            print(f"[approval] CEO 代审异常 {aid}：{type(e).__name__}: {e}", flush=True)
            continue
        if r == "throttled":             # 限速：本轮配额用完，留给下一轮巡检
            print(f"[approval] CEO 代审限速，剩 {len(ids) - idx} 单留待下一轮", flush=True)
            break


def _proxy_facts(kn, appr):
    """代审批的事实清单：单据 + 历史同类 + 产能缺口（CEO 只许引用这些）。"""
    checks = appr.get("checks") or {}
    try:
        checks_txt = json.dumps(checks, ensure_ascii=False)[:1200]
    except (TypeError, ValueError):
        checks_txt = str(checks)[:1200]
    history = ""
    if kn.db:
        try:
            rows = kn.db.fetch(
                "SELECT status AS s, COUNT(*) AS n FROM approvals WHERE action=? GROUP BY status",
                (appr.get("action"),))
            history = "、".join(f"{r['s']}={r['n']}" for r in (rows or []))
        except Exception:
            history = "读取失败"
    project = ""
    with kn.lock:
        for c in kn.contents:
            if c.get("traceId") == appr.get("traceId"):
                project = c.get("projectId") or ""
                break
    return (f"审批单={appr.get('id')} 动作={appr.get('action')} 风险={appr.get('risk')}；"
            f"标题={appr.get('title')}；条目数={len(appr.get('items') or [])}；"
            f"创建于 {appr.get('createdAt')}，老板超过 {appr.get('timeoutSec')}s 未处理（已超时挂起）；"
            f"所属项目={project or '未标注'}；"
            f"自动检查={checks_txt}；历史同类审批（{appr.get('action')}）：{history or '无'}；"
            f"{capacity.facts(kn)}；"
            "老板授权口径：老板超时未审批的由 CEO 酌情代批，须保证每个产品组成品产出不少于 2。")


def _ceo_proxy_decide(kn, approval_id):
    """CEO 对一张超时单酌情裁决：approve / reject / 放弃代批（升级老板）。"""
    with kn.lock:
        appr = next((a for a in kn.approvals if a["id"] == approval_id), None)
    if not appr or appr.get("status") != "timeout":
        return None
    # 幂等硬保护：以库为准（内存快照可能滞后，重启后尤其容易重复代批同一张单）
    disk = _disk_status(kn, approval_id)
    if disk and disk != "timeout":
        print(f"[approval] 单 {approval_id} 库中已为 {disk}，跳过代审", flush=True)
        return None
    if not _proxy_claim_slot():          # 额度只在真正要调模型时占用
        return "throttled"
    _proxy_count(approval_id)
    data, meta = kn.call_agent(
        "ceo",
        "老板超过审批时限仍未处理这张审批单，按授权由你酌情代批："
        "判断能否开工/发布、要补哪个产品组的产能缺口。依据不足或风险不可控就 reject。",
        _proxy_facts(kn, appr), "",
        "decision(approve|reject), confidence(0到1), reason(≤80字，直接向老板说), "
        "risks(2-4条，代批风险), capacityGroup(本次产出主补的产品组id)")
    if not data:
        with _proxy_lock:
            _proxy_state["declined"] += 1
        kn.send("escalation", "ceo", "owner", appr.get("traceId", ""),
                {"text": f"代审失败（模型无返回）：{approval_id} 仍挂起，请老板拍板",
                 "approvalId": approval_id}, priority=2)
        return None
    decision = str(data.get("decision") or "").strip().lower()
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    reason = str(data.get("reason") or "").strip()[:120]
    if decision not in ("approve", "reject") or conf < CEO_PROXY_MIN_CONF:
        with _proxy_lock:
            _proxy_state["declined"] += 1
        kn.send("escalation", "ceo", "owner", appr.get("traceId", ""),
                {"text": f"CEO 放弃代批（{decision or '无结论'} / 置信度 {conf:.2f}）："
                         f"{reason or '依据不足'}，请老板拍板", "approvalId": approval_id},
                priority=2)
        return None
    decided = decide_approval(kn, approval_id, decision, PROXY_ACTOR)
    if not decided:
        return None
    with kn.lock:
        decided["ceoProxy"] = True
        decided["ceoReason"] = reason
        decided["ceoConfidence"] = round(conf, 2)
        decided["ceoAt"] = iso_now()
        decided["ceoGroup"] = str(data.get("capacityGroup") or "")[:24]
        decided["ceoRisks"] = data.get("risks") or []
        snapshot = json.loads(json.dumps(decided, ensure_ascii=False))
    with _proxy_lock:
        _proxy_state["approved" if decision == "approve" else "rejected"] += 1
    kn.emit("approval", decided.get("traceId", ""), snapshot)   # 补字段后二次落库（events 负责 upsert）
    _notify_proxy(kn, snapshot, decision, reason, conf, meta)
    return snapshot


def _notify_proxy(kn, appr, decision, reason, conf, meta):
    """代批结果必须让老板与小秘书同时知道（系统消息 + ceo 邮箱抄送小秘书）。"""
    verdict = "放行" if decision == "approve" else "驳回"
    text = (f"CEO 代批（老板超时未处理）：{verdict} {appr.get('title') or appr.get('action')}"
            f"｜理由：{reason or '—'}｜置信度 {conf:.2f}｜审批号 {appr.get('id')}")
    trace = appr.get("traceId", "")
    kn.send("escalation", "ceo", "owner", trace,
            {"text": text, "approvalId": appr.get("id"), "decision": decision,
             "ceoProxy": True, "decidedBy": PROXY_ACTOR}, priority=2)
    kn.send("note", "ceo", "secretary", trace,
            {"text": text + "（请记入待办并转达相关项目组）", "approvalId": appr.get("id"),
             "decision": decision}, priority=1)
    subject = f"[OPC CEO 代批] {verdict} · {appr.get('action')} · {appr.get('id')}"
    body = (f"{text}\n\n## 单据\n- 审批号：{appr.get('id')}\n- trace：{trace}\n"
            f"- 动作：{appr.get('action')}（风险 {appr.get('risk')}）\n"
            f"- 条目：{len(appr.get('items') or [])} 条\n- 创建：{appr.get('createdAt')}\n"
            f"- 代批：{PROXY_ACTOR} · {appr.get('ceoAt')}\n\n"
            f"## CEO 判断\n- 理由：{reason or '—'}\n- 置信度：{conf:.2f}\n"
            f"- 风险：{json.dumps(appr.get('ceoRisks') or [], ensure_ascii=False)}\n"
            f"- 补产能组：{appr.get('ceoGroup') or '未指定'}\n\n"
            "## 口径\n老板超过审批时限未处理（status=timeout）的单据由 CEO 酌情代批，"
            "全程留痕（审批号 + 理由 + 置信度），老板可随时复核或否决。")
    # 邮件节流：放行单必发；驳回批次按 PROXY_MAIL_GAP（默认 30 分钟）合并，避免信箱被刷爆
    with _proxy_lock:
        due = decision == "approve" or time.time() - _proxy_mail_at["lastAt"] >= PROXY_MAIL_GAP
        if due:
            _proxy_mail_at["lastAt"] = time.time()
    if due:
        threading.Thread(target=kn.mail_boss, args=(subject, body, "ceo", [PROXY_CC]),
                         daemon=True).start()


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


    try:
        # 老板超时未批 → 交 CEO 酌情代批（限速串行；含重启前遗留的 timeout 单）
        if _ceo_proxy_kick(kn).get("queued"):
            print("[approval] 已把超时单交 CEO 代审", flush=True)
    except Exception as e:
        log_event("_sweep_approvals.ceo_proxy", e, level="error")


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
            facts, "", "summary(≤120字，直接向老板说), evidence(2-4条，引用给定事实), "
            "actions(1-3条), capacityPlan(按产品组补齐到目标产能的具体安排), confidence(0到1)")
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
    # 审批门优先：流水线会阻塞在审批上（trace 仍显示 running），此时该催的是老板而不是"继续等"
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
              {"task": f"全员复工：{title}（空闲 {len(idle)} 人已按岗位分派；"
                       f"产能缺口 {capacity.snapshot(kn)['summary']}）",
               "assignments": assign, "capacity": capacity.snapshot(kn)}, priority=1)
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
    # 新建单与"重启后重新挂起"都推一次提醒，保证老板能看到待办
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
    # 查找→判状态→写状态必须一次锁内完成：锁外"先判后写"时，两个并发请求会同时看到
    # pending，同一张单被批两次、续跑被触发两次（TOCTOU）
    with kn.lock:
        cur = next((a for a in kn.approvals if a["id"] == appr["id"]), None)
        if cur is None:      # 从库补出来的单此前不在内存里，补进去让续跑逻辑能看到最新状态
            kn.approvals.append(appr)
            cur = appr
        if cur.get("status") not in ("pending", "timeout"):
            # 超时挂起的单允许迟到补批/补拒：补批后从断点续跑，不用整轮重来
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


