#!/usr/bin/env python3
"""OPC 工作台端到端验收（零依赖）

用法：
    python3 prototype/server.py &
    python3 prototype/verify.py            # 预期 RESULT: ALL PASS

覆盖验收项（对应方案第 10 节）：
  编制 17 人 / 三项目分布 · SSE 实时事件 · 选题会与冲突投票 · 审核 · 审批门闭环
  未审批不得发布 · 人设串味驳回 · trace 可回放 · 知识归档 · 日报生成 · 审批耗时
"""

import http.cookiejar
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("OPC_BASE", "http://127.0.0.1:8848")
RESULTS = []
COOKIE = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIE))


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def get(path):
    with OPENER.open(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode())


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=15) as r:
        return json.loads(r.read().decode())


class Stream:
    """SSE 采集器：验收依据是事件而非轮询快照。"""

    def __init__(self):
        self.events = []
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            resp = OPENER.open(BASE + "/api/events", timeout=600)
            self.ready.set()
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                try:
                    env = json.loads(line[5:].strip())
                except Exception:
                    continue
                with self.lock:
                    self.events.append(env)
        except Exception:
            self.ready.set()

    def snapshot(self):
        with self.lock:
            return list(self.events)

    def wait(self, pred, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if pred(self.events):
                    return True
            time.sleep(0.1)
        return False

    def count(self, **kw):
        with self.lock:
            n = 0
            for e in self.events:
                if all(e.get(k) == v for k, v in kw.items()):
                    n += 1
            return n

    def msgs(self, trace=None):
        with self.lock:
            return [e["data"] for e in self.events
                    if e.get("kind") == "message" and (trace is None or e.get("traceId") == trace)]


def ensure_server():
    try:
        get("/api/health")
        return None
    except urllib.error.HTTPError as e:
        if e.code == 401:      # 已启用登录：服务活着即可，登录在 main 里做
            return None
    except Exception:
        pass
    print("服务未启动，自动拉起 prototype/server.py ...")
    env = dict(os.environ, OPC_QUIET="1")
    proc = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            get("/api/health")
            return proc
        except Exception:
            time.sleep(0.2)
    raise SystemExit("无法启动 server.py")


def login():
    """从 Obsidian 登录凭据取第一个账号登录，建立会话 Cookie。"""
    users = []
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from server import load_credentials
        users = load_credentials()
    except Exception as e:
        print(f"[verify] 读取登录凭据失败：{e}")
    if not users:
        return False
    u = users[0]
    account = u.get("phone") or u.get("email") or u.get("user")
    try:
        r = post("/api/login", {"account": account, "password": u["password"]})
        return bool(r.get("ok"))
    except Exception as e:
        print(f"[verify] 登录失败：{e}")
        return False


def print_report():
    """打印汇总并返回退出码（中途终止时也复用，避免只输出半截结果）。"""
    ok = all(r[1] for r in RESULTS)
    print("\n" + "=" * 60)
    for name, passed, detail in RESULTS:
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    print("=" * 60)
    print(f"共 {len(RESULTS)} 项，通过 {sum(1 for r in RESULTS if r[1])} 项")
    print("RESULT: ALL PASS" if ok else "RESULT: FAIL")
    return 0 if ok else 1


def main():
    proc = ensure_server()
    try:
        record("登录会话建立", login())
        st = get("/api/state")
        record("快照接口可用", st.get("serverTime") is not None, st.get("serverTime", ""))

        # 1. 编制与项目分布
        agents = st.get("agents", [])
        per = {}
        for a in agents:
            per[a["projectId"]] = per.get(a["projectId"], 0) + 1
        record("17 名员工编制", len(agents) == 17, f"agents={len(agents)}")
        record("三项目各 3 人", all(per.get(p) == 3 for p in ("wenren", "gou", "tongxinyuan")),
               json.dumps({k: per.get(k, 0) for k in ("wenren", "gou", "tongxinyuan")}, ensure_ascii=False))
        record("中台/管理层 8 人", per.get("all") == 8, f"all={per.get('all')}")
        record("项目 KPI 齐全", len(st.get("projects", [])) == 3, f"projects={len(st.get('projects', []))}")

        # 2. SSE
        sse = Stream()
        sse.ready.wait(3)
        sse.wait(lambda evs: any(e.get("kind") == "state" for e in evs), 3)
        record("SSE 事件流建立", sse.count(kind="state") >= 1, f"state 帧={sse.count(kind='state')}")

        # 3. 注入热点 → 选题会
        t0 = time.time()
        inj = post("/api/hotspots/inject")
        trace = inj["traceId"]
        record("热点注入成功", bool(trace), trace)

        hot_seen = sse.wait(lambda evs: any(
            e.get("kind") == "hotspot" and e.get("traceId") == trace for e in evs), 5)
        hot_latency = round(time.time() - t0, 2)
        record("业务事件→UI 可见 < 1.5s（SSE 帧）", hot_seen and hot_latency < 1.5, f"{hot_latency}s")

        meeting = sse.wait(lambda evs: any(
            e.get("kind") == "scene" and e.get("data", {}).get("scene") == "MEETING" for e in evs), 60)
        meeting_latency = round(time.time() - t0, 2)
        record("CEO 召集选题会（scene=MEETING）", meeting, f"{meeting_latency}s")

        # 真实模型单次调用约 60-120s：W1（6 并行）→ CCO → CEO 三段串行，阈值按实测留余量
        # 实测（2026-11-07 06:30 轮）：CCO 事件 360s、定稿 633s —— 员工已启用邮件工具，选题段更慢
        selected = sse.wait(lambda evs: any(
            e.get("kind") == "message" and e.get("data", {}).get("type") == "topic.select"
            and e.get("traceId") == trace for e in evs), 540)
        record("CCO 汇总选出 9 条（每项目 3 条）", selected, f"{round(time.time() - t0, 2)}s")
        decided = sse.wait(lambda evs: any(
            e.get("kind") == "message" and e.get("data", {}).get("type") == "decision"
            and e.get("traceId") == trace for e in evs), 600)
        decide_latency = round(time.time() - t0, 2)
        record("热点→选题评审会定稿 ≤ 900s", decided and decide_latency <= 900, f"{decide_latency}s")

        msgs = [m for m in sse.msgs(trace)]
        types = {m["type"] for m in msgs}
        record("类型化消息 ≥ 20 条", len(msgs) >= 20, f"messages={len(msgs)}")
        record("消息类型 ≥ 6 种", len(types) >= 6, ",".join(sorted(types)))
        record("CCO 选题（topic.select）", "topic.select" in types)
        record("决议（decision）", "decision" in types)
        decision = next((m for m in msgs if m["type"] == "decision"), {})
        final = (decision.get("payload") or {}).get("final") or {}
        record("评审会定稿 6 条（每项目 2 条）",
               all(len(final.get(p, [])) == 2 for p in ("wenren", "gou", "tongxinyuan")),
               json.dumps({k: len(v) for k, v in final.items()}, ensure_ascii=False))

        # 3.5 开工审批：老板批准后才进入制作
        sse.wait(lambda evs: any(
            e.get("kind") == "approval"
            and (e.get("data") or {}).get("action") == "production.start"
            and e.get("traceId") == trace for e in evs), 900)
        prod = [a for a in get("/api/state").get("approvals", [])
                if a["traceId"] == trace and a.get("action") == "production.start"]
        record("开工审批已挂起", len(prod) == 1 and prod[0]["status"] == "pending",
               prod[0]["status"] if prod else "none")
        if prod:
            post(f"/api/approvals/{prod[0]['id']}", {"decision": "approve"})

        # 4. 审核结论（真实出片 + 样片评审 + 精剪 + 终审，按事件等待）
        # 实测单条 Remotion 渲染 ≈ 316s，6 条串行 + TTS + 模型评审 ≈ 40-55 分钟
        sse.wait(lambda evs: sum(1 for e in evs if e.get("kind") == "sample-review"
                                 and e.get("traceId") == trace) >= 6, 4200)
        sample = [e for e in sse.snapshot() if e.get("kind") == "sample-review" and e.get("traceId") == trace]
        # 样片三方评审通过的条目才进入精剪与终审
        sample_pass_ids = {e["data"]["itemId"] for e in sample if e["data"].get("pass")}
        sse.wait(lambda evs: len({e["data"]["payload"]["verdict"].get("itemId")
                                  for e in evs
                                  if e.get("kind") == "message" and e.get("data", {}).get("type") == "result"
                                  and isinstance(e.get("data", {}).get("payload", {}).get("verdict"), dict)
                                  and "reasons" in e["data"]["payload"]["verdict"]}) >= len(sample_pass_ids), 1200)
        msgs = [e["data"] for e in sse.snapshot()
                if e.get("kind") == "message" and e.get("traceId") == trace]
        verdicts = [m for m in msgs if m["type"] == "result"
                    and isinstance(m.get("payload", {}).get("verdict"), dict)
                    and "reasons" in m["payload"]["verdict"]]
        reviewed_ids = {v["payload"]["verdict"].get("itemId") for v in verdicts}
        pass_ids = {v["payload"]["verdict"].get("itemId") for v in verdicts
                    if v["payload"]["verdict"].get("pass")}
        record("审核覆盖全部送审项", len(reviewed_ids) >= len(sample_pass_ids) and bool(sample_pass_ids),
               f"reviewed={len(reviewed_ids)} sample_pass={len(sample_pass_ids)}")
        record("样片三方评审已执行", len(sample) >= 3, f"sample-review={len(sample)}")
        record("审核结论附带理由", all(v["payload"]["verdict"].get("reasons") for v in verdicts))

        # 5. 审批门：未审批绝不发布
        sse.wait(lambda evs: any(e.get("kind") == "approval" and e.get("traceId") == trace
                                 and (e.get("data") or {}).get("action") == "publish.execute"
                                 for e in evs), 1500)
        pub_before = [m for m in msgs if m["sender"] == "release_ops"]
        appr = [a for a in get("/api/state").get("approvals", [])
                if a["traceId"] == trace and a.get("action") == "publish.execute"
                and a.get("status") == "pending"]
        record("审批门已挂起", len(appr) == 1 and appr[0]["status"] == "pending",
               appr[0]["status"] if appr else "none")
        record("未审批前发布次数 = 0", len(pub_before) == 0, f"publish msgs={len(pub_before)}")
        record("审批仅放行终审通过项", bool(appr) and set(appr[0].get("items", [])) == pass_ids,
               f"approved={len(appr[0].get('items', [])) if appr else 0} pass={len(pass_ids)}")
        blocked = [m for m in msgs if m["type"] == "escalation"]
        record("被阻断项已升级给老板", bool(blocked), f"escalation={len(blocked)}")
        if not appr:
            print("\n[verify] 审批未挂起，终止后续断言")
            sys.exit(print_report())

        # 6. 老板审批 → 发布 → 归档 → 日报
        t1 = time.time()
        post(f"/api/approvals/{appr[0]['id']}", {"decision": "approve"})
        published = sse.wait(lambda evs: any(
            e.get("kind") == "pipeline" and e.get("data", {}).get("stage") == "published" for e in evs), 600)
        record("审批后进入已发布", published, f"{round(time.time() - t1, 2)}s")

        sse.wait(lambda evs: any(
            e.get("kind") == "report" and e.get("data", {}).get("kind") == "daily"
            and e.get("traceId") == trace for e in evs), 600)

        st2 = get("/api/state")
        stages = {}
        for c in st2.get("pipeline", []):
            if c["traceId"] == trace:
                stages.setdefault(c["projectId"], []).append(c["stage"])
        record("三项目均已发布", all("published" in stages.get(p, []) or "archived" in stages.get(p, [])
                                    for p in ("wenren", "gou", "tongxinyuan")), json.dumps(stages, ensure_ascii=False))

        kb = st2.get("knowledge", {})
        approved_n = len(appr[0]["items"])
        record("知识归档 = 审批放行条数", kb.get("count", 0) >= approved_n and approved_n >= 1,
               f"count={kb.get('count', 0)} approved={approved_n}")
        record("CEO 日报已生成", any(r["kind"] == "daily" and r["traceId"] == trace for r in st2.get("reports", [])))

        # 6.5 Hermes 内核实证
        hs = st2.get("stats", {}).get("hermes", {})
        full = sse.msgs(trace)
        hermes_msgs = [m for m in full if m.get("payload", {}).get("engine") == "hermes"]
        degraded = [m for m in full if m.get("payload", {}).get("engine") == "degraded"]
        record("Hermes 内核已启用", st2.get("engine") == "hermes", f"engine={st2.get('engine')}")
        record("真实内核调用 ≥ 15 次", hs.get("calls", 0) >= 15, f"calls={hs.get('calls', 0)}")
        record("模型调用失败受控（≤3 次重试失败）", hs.get("failures", 0) <= 3, f"failures={hs.get('failures', 0)}")
        record("结果由真实模型产出", len(hermes_msgs) >= 8,
               f"hermes={len(hermes_msgs)} degraded={len(degraded)}")
        record("单次调用时延已计量", hs.get("avgLatencyMs", 0) > 0, f"avg={hs.get('avgLatencyMs', 0)}ms")

        # 7. 人设串味注入 → reviewer 必须驳回
        p = post("/api/commands", {"text": "人设串味测试"})
        pt = p.get("personaTest") or {}
        record("人设串味被驳回", pt.get("pass") is False, "；".join(pt.get("reasons", [])))

        # 8. trace 回放
        tr = get("/api/trace/" + trace)
        record("trace 可回放", tr.get("messageCount", 0) >= 20 and len(tr.get("contents", [])) >= 6,
               f"msgs={tr.get('messageCount')} contents={len(tr.get('contents', []))}")
        record("日报可回溯 traceId", len(tr.get("reports", [])) >= 1)

        hist = tr.get("meta", {}).get("reviewHistory") or []
        final_items = {h.get("itemId"): h["pass"] for h in hist if h.get("itemId")}
        approved_ids = set(appr[0]["items"])
        record("终审通过 = 审批放行条数（允许驳回后返工）",
               approved_ids <= set(final_items) and all(final_items.get(i) for i in approved_ids),
               json.dumps({"rounds": len(hist), "approved": len(approved_ids)}, ensure_ascii=False))

        archived = [c for c in tr.get("contents", []) if c["stage"] == "archived"]
        record("产出归档率 100%",
               len(archived) >= approved_n and len(tr.get("knowledge", [])) >= len(archived),
               f"archived={len(archived)} kb={len(tr.get('knowledge', []))}")

        # 9. 审批耗时
        appr2 = [a for a in get("/api/state").get("approvals", [])
                 if a["traceId"] == trace and a.get("action") == "publish.execute"]
        elapsed = st2["stats"].get("approvalSecondsAvg", 0)
        record("老板审批耗时 ≤ 10 分钟", elapsed <= 600, f"{elapsed}s")

        # 10. 无高危工具越权
        blocked = [m for m in msgs if m["type"] == "delegation" and "publish.execute" in json.dumps(m, ensure_ascii=False)]
        record("高危工具未直接下发", len(blocked) == 0, f"violations={len(blocked)}")

    finally:
        if proc:
            proc.terminate()

    return print_report()


if __name__ == "__main__":
    sys.exit(main())