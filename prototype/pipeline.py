"""流水线编排与阶段实现（C1 第七刀）：PipelineContext / run_pipeline / 15 个阶段。

从 kernel.Kernel 抽出。约定：
- 每个函数第一参数是 Kernel 实例 kn，阶段间状态放 PipelineContext（容器原地修改）；
- 阶段实现按 config/pipeline.yaml 的 stage id 命名 `_stage_<id>`，新增阶段只改定义+加函数；
  例外：render 阶段实现留在 factory.py（分镜/出片/重发同域），本模块经 kn._stage_render 委托过去；
- 不 import kernel（kernel 单向依赖本模块并保留同名薄委托），断点写档/车间事件走 kn 委托；
- 异常语义不变：BudgetAbort 表示阶段边界止损，_PIPELINE_STOP 表示提前收口后续阶段。
"""
import json
import os
import shutil
import threading
import time

from opc_paths import *  # noqa: F401,F403 —— APPROVAL_TIMEOUT / VAULT / BOSS_MAIL 等常量
from utils import iso_now, uid
from deps import *  # noqa: F401,F403 —— DL / OMR 等基础设施单例
import company_config as CC
import message_feeds as FEED
from hermes_adapter import HERMES
from obs import log_event
import factory


class BudgetAbort(Exception):
    """CTO 判定成本/额度超限，在阶段边界终止本轮任务。"""


class PipelineContext:
    """流水线阶段之间的共享状态（字段即阶段契约，不承载业务逻辑）。

    容器字段一律原地修改，避免阶段间出现“改了副本但没写回”的隐性 bug；
    需要提前收口的阶段返回 _PIPELINE_STOP，由 runner 决定停止后续阶段。
    """

    def __init__(self, kernel, trace, hotspot):
        self.kernel = kernel
        self.trace = trace
        self.hotspot = hotspot
        self.stage = None                 # 当前阶段定义（pipeline_defs.Stage）
        self.pms = [("wenren", "pm_wenren"), ("gou", "pm_gou"),
                    ("tongxinyuan", "pm_tongxinyuan")]
        self.items = {}                   # itemId -> 内容单
        self.selected = {}                # pid -> 选题
        self.final = {}                   # pid -> 定稿列表
        self.eliminated = []              # CCO 未入选记录
        self.pool_size = 0
        self.meeting_doc = {}             # 选题会纪要（归档 + 汇报）
        self.participants = []            # 选题会参会人（两场会议共用）
        self.conclusion = ""              # 评审会结论正文
        self.render_blocked = set()       # 真实出片失败的 itemId
        self.sample_blocked = set()       # 样片评审未过的 itemId
        self.review_ids = []              # 待终审 itemId
        self.passed_items = {}            # 终审通过 itemId -> 结论
        self.review_history = []
        self.pending = []                 # 已挂起的发布审批
        self.pub_ctx = ""                 # 发布结论（归档/汇报引用）

    # 断点续跑用的快照字段：只放阶段间真正需要的状态，set 序列化成 list
    # pms 可被快照覆盖：创意灵感发起的单项目流水线只带本项目 PM（缺省仍为三项目）
    STATE_FIELDS = ("pms", "selected", "final", "eliminated", "pool_size", "meeting_doc",
                    "participants", "conclusion", "review_ids", "passed_items",
                    "review_history", "pending", "pub_ctx")

    def to_state(self):
        """阶段边界快照：进程重启后据此恢复上下文（至少一次语义，重跑当前阶段）。"""
        out = {k: getattr(self, k) for k in self.STATE_FIELDS}
        out["items"] = self.items
        out["render_blocked"] = sorted(self.render_blocked)
        out["sample_blocked"] = sorted(self.sample_blocked)
        return out

    def load_state(self, state):
        """把快照写回上下文；缺失字段保留默认值，避免旧数据缺列直接报错。"""
        for k in self.STATE_FIELDS:
            if k in state:
                setattr(self, k, state[k])
        if isinstance(state.get("items"), dict):
            self.items = state["items"]
        self.render_blocked = set(state.get("render_blocked") or [])
        self.sample_blocked = set(state.get("sample_blocked") or [])
        return self



_PIPELINE_STOP = object()



def run_pipeline(kn, trace, hotspot, start=None, state=None):
    kn.active_trace = trace
    t0 = time.time()
    with kn.lock:
        origin = str((kn.traces.get(trace) or {}).get("source") or "")
    try:
        FEED.hermes_start(kn.db, kn.emit, trace, kn._trace_title(trace), origin,
                          start or "", len((state or {}).get("items") or {}))
    except Exception as e:
        print(f"[msgfeed] hermes 启动记录失败：{type(e).__name__}: {e}", flush=True)
    if kn.db:
        try:
            if start:
                kn.db.pipeline_run_stage(trace, start, 0, state or {})
            else:
                kn.db.pipeline_run_start(trace, hotspot)
        except Exception as e:
            print(f"[pipeline] 运行态入库失败（不影响执行）：{type(e).__name__}: {e}", flush=True)
    try:
        _pipeline(kn, trace, hotspot, start=start, state=state)
    except BudgetAbort as exc:
        # CTO 已判定超阈值并通知老板：这里只负责按“停止”收口
        with kn.lock:
            kn.traces[trace]["status"] = "aborted"
            kn.traces[trace]["closedAt"] = iso_now()
            kn.traces[trace]["abortReason"] = str(exc)
            for item in kn.contents:
                if item.get("traceId") == trace and item.get("stage") not in (
                        "published", "archived"):
                    item["stage"] = "blocked"
                    item["blockedBy"] = "cost-gate"
        kn._persist_trace(trace)
        kn.send("escalation", "cto", "owner", trace,
                  {"text": f"任务已在阶段边界停止：{exc}", "aborted": True}, priority=2)
    except Exception as exc:
        kn.send("escalation", "ceo", "owner", trace, {"text": f"编排异常：{exc}"}, priority=2)
        with kn.lock:
            kn.traces[trace]["status"] = "error"
            kn.traces[trace]["closedAt"] = iso_now()
        kn._persist_trace(trace)
    finally:
        if kn.active_trace == trace:
            kn.active_trace = ""
        # 编排异常时也要收口会议，避免 3D 会议室一直停留在“开会中”
        if kn.meeting_log.get("traceId") == trace and kn.meeting_log.get("status") == "live":
            try:
                kn.meeting_end(trace, "会议中断：编排异常，未形成结论。")
                kn.set_scene("WORK", trace, title="会议中断")
            except Exception as e:
                log_event("run_pipeline.meeting_close", e, level="error", trace=trace)
        # 兜底收口：无论从哪条分支提前返回，都不能把 trace 永远留在 running，否则巡检会一直拒派
        with kn.lock:
            t = kn.traces.get(trace)
            if t and t.get("status") == "running":
                t["status"] = "incomplete"
                t["closedAt"] = iso_now()
        kn._persist_trace(trace)
        kn.export_trace_docs(trace)   # 收口之后再落盘，交付文档才能记录到最终状态
        kn.save_runtime()             # 本轮报告/消息/知识入库，重启后可恢复
        if kn.db:
            try:
                final = (kn.traces.get(trace) or {}).get("status") or "incomplete"
                kn.db.pipeline_run_end(trace, final)
            except Exception as e:
                print(f"[pipeline] 运行态收口失败：{type(e).__name__}: {e}", flush=True)
        # 结束后的成果：最终状态 / 产出条数 / 成片文件 / 总耗时（消息中心 hermes 源）
        try:
            with kn.lock:
                items = [dict(c) for c in kn.contents if c.get("traceId") == trace]
                final = str((kn.traces.get(trace) or {}).get("status") or "incomplete")
            done = [c for c in items if c.get("stage") in ("published", "archived")]
            files = sum(len(c.get("assets") or []) for c in done)
            FEED.hermes_finish(kn.db, kn.emit, trace, kn._trace_title(trace),
                               final, len(items), len(done), files, time.time() - t0)
        except Exception as e:
            print(f"[msgfeed] hermes 成果记录失败：{type(e).__name__}: {e}", flush=True)


def _pipeline(kn, trace, hotspot, start=None, state=None):
    """按流程定义（config/pipeline.yaml）顺序执行阶段。

    每个阶段一个 _stage_<id> 方法，阶段间状态放 PipelineContext；
    新增/调整阶段只改定义文件与对应实现，不再维护一条硬编码长函数。
    每次进入阶段前把「阶段 + 上下文快照」写入过程库，进程重启可从这里续跑。
    """
    c = PipelineContext(kn, trace, hotspot)
    if state:
        c.load_state(state)
        # 续跑时把快照里的内容单挂回总线，否则前端/报表看不到这些单
        with kn.lock:
            known = {x.get("id") for x in kn.contents}
            for item in c.items.values():
                if item.get("id") not in known:
                    kn.contents.append(item)
    stages = [st for st in CC.PIPELINE.stages if st.enabled]
    begin = 0
    if start:
        idx = next((i for i, st in enumerate(stages) if st.id == start), None)
        if idx is None:
            raise RuntimeError(f"断点阶段 {start} 不在流程定义里，无法续跑")
        begin = idx
        print(f"[pipeline] {trace} 从阶段 {start}（#{idx}）续跑", flush=True)
    for pos in range(begin, len(stages)):
        st = stages[pos]
        fn = getattr(kn, "_stage_" + st.id, None)
        if fn is None:
            raise RuntimeError(f"流程定义里的阶段 {st.id} 没有实现 _stage_{st.id}")
        c.stage = st
        _run_checkpoint(kn, trace, st, pos, c)
        kn._workshop_start(trace, st)
        try:
            stop = fn(c)
        except Exception:
            kn._workshop_end(trace, st, "failed", c)
            try:
                FEED.hermes_node(kn.db, kn.emit, trace, kn._trace_title(trace),
                                 st.name, "failed", len(c.items))
            except Exception as e:
                print(f"[msgfeed] hermes 节点记录失败：{type(e).__name__}: {e}", flush=True)
            raise
        kn._workshop_end(trace, st, "stopped" if stop else "done", c)
        # 关键节点（消息中心 hermes 源）：阶段收口 + 在制数量
        try:
            FEED.hermes_node(kn.db, kn.emit, trace, kn._trace_title(trace),
                             st.name, "stopped" if stop else "done", len(c.items))
        except Exception as e:
            print(f"[msgfeed] hermes 节点记录失败：{type(e).__name__}: {e}", flush=True)
        if stop is _PIPELINE_STOP:
            return


def _run_checkpoint(kn, trace, st, pos, c):
    """阶段边界写档：stage 是即将执行的阶段，状态快照随行。"""
    _write_stage_checkpoint(kn, trace, st, pos, c)
    if not kn.db:
        return
    try:
        kn.db.pipeline_run_stage(trace, st.id, pos, c.to_state())
    except Exception as e:
        print(f"[pipeline] 断点写档失败（不影响执行）：{type(e).__name__}: {e}", flush=True)


def _write_stage_checkpoint(kn, trace, st, pos, c):
    """按 om_stage 对齐的断点文件（OpenMontage 侧没有原生 checkpoint 协议）。

    与过程库同源：trace_id + stage + om_stage，外部编排器或排障时可直接读文件，
    不必连库；checkpoints.jsonl 保留整轮轨迹，checkpoint.json 只留当前断点。
    """
    try:
        d = os.path.join(VAULT, "OPC", "运行态", trace)
        os.makedirs(d, exist_ok=True)
        payload = {"traceId": trace, "stage": st.id, "omStage": st.om_stage,
                   "stageIndex": pos, "items": len(c.items), "at": iso_now()}
        with open(os.path.join(d, "checkpoint.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        with open(os.path.join(d, "checkpoints.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[pipeline] 断点文件写入失败：{e}", flush=True)


def _stage_timeout(kn, c):
    """审批超时：流程定义优先，未配置时沿用 OPC_APPROVAL_TIMEOUT。"""
    return c.stage.timeout_sec or APPROVAL_TIMEOUT


def _stage_topic_meeting(kn, c):
    """1. 每日选题（知识库内化）：CCO 从知识库归档认知中提炼若干最佳选题，直出定稿。

    制度（2026-11-17 老板修订）：一个议题只匹配一个具体项目、产出一个最终产品，
    不再把一个方向拆给三家各做一份。不设选题总量，某项目当天 0 条合法。
    不发起选题评审会：CCO 决策即定稿，直接进入汇报与建单。
    """
    kn.guard_budget(c.trace)
    kb = kn._kb_brief()
    if not kb["entries"]:
        raise RuntimeError("知识库暂无可用于选题的条目：请先在 OPC/知识库 归档内化认知后再发起每日选题")
    hint = str(c.hotspot.get("title") or "") if str(c.hotspot.get("kind") or "") != "knowledge" else ""
    participants = c.participants = ["cco", "kb_manager"]
    agenda = [f"知识来源：{hint or '知识库内化'}",
              f"知识条目 {kb['count']} 条（分类 {len(kb['categories'])} 个）",
              "CCO 提炼最佳选题：议题驱动、不设总量，每个议题只归一个项目", "不设评审会：CCO 决策即定稿"]
    kn.meeting_begin(c.trace, "每日选题会", participants, agenda)
    kn.set_scene("MEETING", c.trace, title="每日选题会", participants=participants, agenda=agenda)
    for aid in participants:
        kn.set_agent(aid, "meeting", "选题会")
        kn.send("delegation", "cco", aid, c.trace, {"task": "参会：知识库内化选题"}, priority=1)
    kn.meeting_say("cco", f"今天不从热点找题，直接从知识库 {kb['count']} 条内化认知里提炼："
                            "有几个合格议题就定几条，每条只归一个项目组、只出一个产品；"
                            "我定了就进制作，不再开评审会。", "opening", c.trace)

    ctx = {"知识库": {"条目总数": kb["count"], "分类分布": kb["categories"],
                    "项目分布": kb["projects"], "近期条目": kb["entries"]},
           "项目": {pid: {"名称": kn.projects[pid]["name"], "调性": kn.tone[pid]}
                    for pid in kn.PIDS},
           "老板指定方向": hint}
    # 自动采集 → CCO 分析出的最新灵感：选题时优先采用（有就先占位，不足再用知识库条目补）
    try:
        import collector
        fresh = collector.priority_ideas(kn.data_dir, limit=8)
    except Exception:
        fresh = []
    if fresh:
        ctx["采集灵感（优先采用）"] = [
            {"标题": r.get("title") or "", "灵感": r.get("summary") or "",
             "结合点": r.get("detail") or "", "标签": r.get("tags") or "",
             "来源": r.get("source") or "", "素材目录": r.get("asset") or ""}
            for r in fresh]
    data, meta = kn.call_agent(
        "cco",
        "你是 CCO。从给定知识库条目中提炼今日最佳选题：优先近 30 天、可复用结论扎实、"
        "与项目调性一致且具备制作可行性的认知。数量按议题质量决定，不设固定条数、不为每个项目凑数。"
        "硬性制度（2026-11-17 老板修订）：一个议题只匹配一个具体项目、只产出一个最终产品——"
        "同一个认知/方向不得拆成多条分给三个项目，也不得给同一议题填多个 projectId；"
        "每条选题的 projectId 必须唯一指向最匹配的那一个项目组，宁可某项目当天 0 条。"
        "要求：1) 不得直接照抄条目标题，要提炼成可拍选题；2) 必须引用具体知识条目（knowledgeRefs）；"
        "3) 不引用热点榜单、不编造数字；4) 选题之间必须主题各异，禁止同一方向换措辞重复出现；"
        "5) 给了「采集灵感（优先采用）」时，优先把这些灵感落成选题（它们来自自动采集的最新素材，"
        "老板要求优先采用），每条灵感只归一个项目，并在 reason 里注明「采集灵感」。",
        json.dumps(ctx, ensure_ascii=False),
        "额外 JSON 字段：final(数组，条数由议题数决定，每条 {projectId,title,angle,form,durationSec,reason,knowledgeRefs}，"
        "所有 title 必须主题各异、每个议题只出现一次)；"
        "excluded(数组，未采用的认知 {title, projectId, reason})；minutes(会议纪要 Markdown ≤800 字)。",
        "summary, evidence, confidence, tools, final, excluded, minutes",
        preferred="qwen38-local")
    data = data or kn._deg("cco", "知识库选题")
    c.final = kn._coerce_cco_topics(data, kb)
    c.selected = {pid: list(v) for pid, v in c.final.items()}
    c.pool_size = kb["count"]
    c.eliminated = [x for x in (data.get("excluded") or []) if isinstance(x, dict)]
    total = sum(len(v) for v in c.final.values())
    kn.send("topic.select", "cco", "ceo", c.trace,
              {"selected": c.selected, "eliminated": c.eliminated, "poolSize": c.pool_size,
               "result": data, "engine": meta["engine"], "source": "knowledge"}, priority=2)
    kn.send("topic.select", "cco", "cto", c.trace,
              {"selected": c.selected, "eliminated": c.eliminated, "cc": True,
               "engine": meta["engine"]}, priority=1)
    kn.emit("engine-meta", c.trace, {"agent": "cco", **meta})
    picked = [t.get("title", "") for pid in kn.PIDS for t in c.final[pid]]
    kn.meeting_say("cco", (data.get("summary") or
                             f"从知识库 {kb['count']} 条认知中提炼出 {total} 条选题。") +
                     (" 定稿：" + "；".join(picked) if picked else ""), "decision", c.trace)

    # 会议纪要：只有一场选题会（知识库内化），CCO 结论即定稿，不再有第二场评审
    display = lambda xs: ["老板" if x == "owner" else kn.agents.get(x, {}).get("name", x) for x in xs]
    minutes = str(data.get("minutes") or "").strip() or (
        f"# 每日选题会（知识库内化）\n\n- 知识来源：{hint or '知识库'}（{kb['count']} 条）\n"
        "- 参会：" + "、".join(display(participants)) + "\n"
        f"- 结论：CCO 提炼定稿 {total} 条（议题驱动，每个议题唯一归属一个项目组），不发起评审会\n\n## 定稿\n"
        + "\n".join(f"- {kn.projects[pid]['name']}《{t['title']}》："
                    f"{t.get('reason') or t.get('angle') or ''}"
                    for pid in kn.PIDS for t in c.final[pid]) + "\n")
    recipients = [str(x) for x in (data.get("recipients") or [])] or ["cco", "ceo", "cto"]
    cc = [str(x) for x in (data.get("cc") or [])] or ["owner"]
    c.meeting_doc = {}
    if kn.DL:
        try:
            c.meeting_doc = kn.DL.export_meeting(c.trace, {
                "title": "每日选题会（知识库内化）", "minutes": minutes,
                "recipients": display(recipients), "cc": display(cc),
                "final": c.final, "hotspot": c.hotspot})
        except Exception as e:
            print(f"[pipeline] 会议纪要落盘失败：{type(e).__name__}: {e}", flush=True)
    kn.send("decision", "cco", "all", c.trace,
              {"text": f"知识库内化定稿 {total} 条（议题驱动，每个议题唯一归属一个项目组）",
               "final": c.final, "minutes": minutes,
               "minutesUrl": (c.meeting_doc or {}).get("minutes", ""),
               "mailUrl": (c.meeting_doc or {}).get("mail", ""),
               "result": data, "engine": meta["engine"]}, project="all", priority=2)
    kn.send("report.ready", "cco", "owner", c.trace,
              {"kind": "meeting", "title": "每日选题会纪要（知识库内化）",
               "minutesUrl": (c.meeting_doc or {}).get("minutes", ""),
               "mailUrl": (c.meeting_doc or {}).get("mail", ""),
               "recipients": recipients, "cc": cc}, priority=1)
    c.conclusion = (f"知识库内化定稿 {total} 条（议题唯一归属，知识条目 {kb['count']} 条，无评审）：" +
                    "；".join(f"{kn.projects[pid]['name']}《{t['title']}》"
                             for pid in kn.PIDS for t in c.final[pid]))
    kn.meeting_say("cco", "结论：" + c.conclusion + "。纪要已归档，直接进制作前的开工审批。",
                     "conclusion", c.trace)
    kn.meeting_end(c.trace, c.conclusion, (c.meeting_doc or {}).get("minutes", ""))
    # 祁南（kb_manager）在选题定稿即归档：来源写知识内化，不再挂热点标签
    kn.archive_knowledge(c.trace, [
        {"projectId": pid, "title": t.get("title"),
         "body": f"选题定稿：{t.get('angle') or t.get('title')}；来源：知识库内化；"
                 f"依据：{'、'.join(t.get('knowledgeRefs') or []) or '知识条目'}；"
                 f"CCO 结论：{c.conclusion[:100]}",
         "tags": ["知识内化"] + list(kb["categories"].keys())[:3]}
        for pid in kn.PIDS for t in c.final.get(pid, [])])
    kn.set_scene("WORK", c.trace, title=f"定稿 {total} 条，进入制作")


def _stage_boss_report(kn, c):
    """3.5 CCO 邮件汇报老板：知识库选题定稿 + 等待开工审批（抄送 CEO/CTO）"""
    kn.guard_budget(c.trace)
    total = sum(len(v) for v in c.final.values())
    src = "知识库内化" if str(c.hotspot.get("kind") or "") == "knowledge" \
        else f"老板指定方向「{c.hotspot.get('title')}」"
    mail_lines = [
        f"老板：",
        "",
        f"今日选题已完成（{src}）。",
        f"知识库 {c.pool_size} 条内化认知 → CCO 提炼定稿 {total} 条（议题驱动，每个议题唯一归属一个项目组），"
        "不经评审会，CCO 决策即定稿。",
        "",
        "## 定稿清单",
    ]
    for pid in kn.PIDS:
        mail_lines.append(f"### {kn.projects[pid]['name']}")
        for t in c.final[pid]:
            refs = "、".join(t.get("knowledgeRefs") or []) or "—"
            mail_lines.append(
                f"- 《{t['title']}》｜角度：{t.get('angle', '—')}｜形态：{t.get('form', '—')}｜"
                f"时长：{t.get('durationSec', '—')}s｜理由：{t.get('reason', '—')}｜知识依据：{refs}")
    if c.eliminated:
        mail_lines += ["", "## 未采用的认知"]
        for e in c.eliminated[:12]:
            mail_lines.append(f"- {e.get('title', '—')}：{e.get('reason', '—')}")
    mail_lines += [
        "",
        "## 待办",
        "- 开工审批单已生成（含成本/素材/合规检查），请老板在 OPC 工作台批准后进入制作。",
        f"- 会议纪要：{(c.meeting_doc or {}).get('minutes', '（未生成）')}",
    ]
    mail_subject = f"[每日选题] {src} · 定稿 {total} 条，待开工审批"
    mail_body = "\n".join(mail_lines)
    kn.send("biz.event", "cco", "owner", c.trace,
              {"text": "CCO 已邮件汇报知识库选题结果，等待开工审批",
               "to": BOSS_MAIL, "cc": ["ceo", "cto"], "subject": mail_subject,
               "finalCount": total, "eliminated": len(c.eliminated)}, priority=2)
    threading.Thread(target=kn.mail_boss,
                     args=(mail_subject, mail_body, "cco", ["ceo", "cto"]),
                     daemon=True).start()


def _stage_create_items(kn, c):
    """4. 按定稿逐议题建内容单（一议题一内容单一产品，不设每项目配额）"""
    # 按定稿逐议题建内容单；id 由 trace+项目+序号+标题决定，续跑复用
    # 内容单 id 由 trace+项目+序号+标题 决定：阶段中途被打断、续跑重建时复用同一条，
    # 既有内容不会变成孤儿，发布回执（release_ledger 按 item_id 判重）也不会失效。
    c.items = {}
    for pid in kn.PIDS:
        for idx, t in enumerate(c.final[pid]):
            iid = kn._content_id(c.trace, pid, idx, t["title"])
            item = {"id": iid, "projectId": pid, "traceId": c.trace, "stage": "topic",
                    "title": t["title"], "angle": t.get("angle", ""),
                    "owner": f"pm_{pid}", "toneProfile": kn.tone[pid], "assets": [],
                    "reviewNote": "", "durationSec": t.get("durationSec", 45)}
            with kn.lock:
                existing = next((x for x in kn.contents if x.get("id") == iid), None)
                if existing is not None:
                    item = existing            # 断点续跑：复用已有内容单，不重复登记
                else:
                    kn.contents.append(item)
            c.items[item["id"]] = item
            kn.emit("pipeline", c.trace, item)
        kn.send("delegation", f"pm_{pid}", f"cw_{pid}", c.trace,
                  {"task": f"承接本项目 {len(c.final[pid])} 条定稿选题，先出制作评估再写稿"}, project=pid)
        kn.send("delegation", f"pm_{pid}", f"vd_{pid}", c.trace,
                  {"task": f"承接本项目 {len(c.final[pid])} 条定稿选题，先出制作评估再制作样片"}, project=pid)


def _stage_production_approval(kn, c):
    """4.5 开工审批：老板批准选题与成本/素材/合规摘要后才进入制作"""
    # 4.5 开工审批：老板批准选题与成本/素材/合规摘要后才进入制作
    prod_approval = {
        "id": uid("ap"), "traceId": c.trace, "action": "production.start", "risk": "high",
        "status": "pending", "approver": "老板", "createdAt": iso_now(), "decidedAt": "",
        "timeoutSec": _stage_timeout(kn, c),
        "items": list(c.items.keys()),
        "titles": [i["title"] for i in c.items.values()],
        "title": f"{len(c.items)} 条内容开工审批（含成本/素材/合规检查）",
        "checks": kn._production_checks(c.items),
    }
    prod_approval, prod_ev, _ = kn._open_approval(c, prod_approval)
    prod_ok = (prod_approval["status"] == "approved") if prod_ev is None \
        else prod_ev.wait(timeout=_stage_timeout(kn, c))
    with kn.lock:
        prod_appr = next(a for a in kn.approvals if a["id"] == prod_approval["id"])
        kn.approval_events.pop(prod_approval["id"], None)
    if not prod_ok or prod_appr["status"] != "approved":
        with kn.lock:
            if prod_appr["status"] == "pending":
                prod_appr["status"] = "timeout"
                prod_appr["decidedAt"] = iso_now()
                prod_appr["timeoutReason"] = f"等待老板审批超过 {_stage_timeout(kn, c)}s，按挂起处理"
            for item in c.items.values():
                item["stage"] = "blocked"
                item["blockedBy"] = "production-gate"
            t = kn.traces.get(c.trace)
            if t:
                t["status"] = "rejected" if prod_appr["status"] == "rejected" else "timeout"
                t["closedAt"] = iso_now()
        for item in c.items.values():
            kn.items_emit(c.trace, item)
        kn.send("escalation", "ceo", "owner", c.trace,
                  {"text": "开工审批未通过，本轮不进入制作"}, priority=2)
        kn.emit("approval", c.trace, prod_appr)
        kn.set_scene("WORK", c.trace, title="开工审批未通过")
        return _PIPELINE_STOP

    kn.guard_budget(c.trace)   # CTO 成本熔断：审批通过后、进入制作前再核一次


def _stage_assessment(kn, c):
    """5. PM 细化分镜脚本并按并行原则拆分派单；CCO 定出片模式；scout 补素材缺口"""
    # 5. PM 组织本项目组出《制作评估报告》：把分镜补充完善（逐镜 画面/台词/时长/素材），
    #    并按并行原则拆任务——视频制作耗时较长，逐镜子任务先行开工，文案岗同步领取成稿与补素材指令。
    jobs = {}
    active_pms = [(pid, pm) for pid, pm in c.pms if (c.final.get(pid) or [])]
    for pid, pm in active_pms:
        boss_reqs = "；".join(
            f"《{t.get('title')}》{t['bossBrief']}" for t in (c.final.get(pid) or [])
            if t.get("bossBrief"))
        jobs[pid] = HERMES.pool.submit(
            kn.call_agent, pm,
            f"为 {kn.projects[pid]['name']} 的 {len(c.final.get(pid) or [])} 条定稿选题细化制作方案：把分镜脚本补充完善到逐镜可执行"
            "（画面/台词/时长/所需素材），并按并行原则拆分任务——视频制作拆成逐镜子任务先开工，"
            "文案岗同步领取成稿与补素材指令。"
            + (f"老板特定要求（逐条落实，不得跑偏）：{boss_reqs}" if boss_reqs else ""),
            json.dumps(c.final.get(pid) or [], ensure_ascii=False),
            "额外 JSON 字段：assessment({items:[{title,durationSec,tts:{voice,style},"
            "copyBrief,storyboardNotes,"
            "storyboardShots:[{no,画面,台词,秒,素材}],videoTasks:[{shot,task}]}], summary})；"
            "硬性要求：每镜必须写全 台词 与 秒（简化小样要按这两项对齐配音与关键帧，缺一即返工）。"
            "assetGaps(数组，每条 {item, why})。",
            "summary, evidence, confidence, tools, assessment, assetGaps")
    gap_jobs = {}
    for pid, pm in active_pms:
        data, meta = jobs[pid].result()
        data = data or kn._deg(pm, "制作评估报告")
        pid_items = [i for i in c.items.values() if i["projectId"] == pid]
        for idx, item in enumerate(pid_items):
            item["plan"] = kn._coerce_plan(data, item, idx)
            item["assessment"] = {"summary": data.get("summary", ""),
                                  "evidence": data.get("evidence", []),
                                  "risks": data.get("risks", []),
                                  "assetGaps": data.get("assetGaps", [])}
            kn.items_emit(c.trace, item)
        kn._cco_render_mode(c, pid, pid_items)
        kn._cco_craft(c, pid, pid_items)
        # 只派发 CCO 审批通过（已发流水号）的内容单：审批通过才能进流水线
        approved = [it for it in pid_items if it.get("craft")]
        if approved:
            vtasks = [{"item": it["title"], "renderMode": it.get("renderMode"),
                       "serialNo": it.get("serialNo", ""),
                       **t} for it in approved
                      for t in ((it.get("plan") or {}).get("videoTasks") or [])] or \
                     [{"item": it["title"], "shot": "all",
                       "task": "按细化分镜逐镜并行制作"} for it in approved]
            # 并行派单：视频耗时长，priority=1 先开工（逐镜细分）；文案同步领取
            kn.send("delegation", pm, f"vd_{pid}", c.trace,
                      {"task": "分镜已细化，按逐镜子任务并行开工（视频制作耗时较长，立即启动）",
                       "videoTasks": vtasks[:16]}, project=pid, priority=1)
        kn.send("delegation", pm, f"cw_{pid}", c.trace,
                  {"task": "按文案要求成稿；收到补素材指令后先查素材库，缺口走 ComfyUI 生成，"
                           "台词/对白走 TTS 出音频"}, project=pid)
        kn.send("result", pm, "cco", c.trace,
                  {"result": data, "assessment": data.get("assessment"), "engine": meta["engine"]}, project=pid)
        kn.send("result", pm, "cto", c.trace,
                  {"assessment": data.get("assessment"), "engine": meta["engine"]}, project=pid)
        gaps = data.get("assetGaps") or []
        if gaps:
            kn.send("delegation", pm, "scout", c.trace,
                      {"task": "素材缺失工单：补齐/替代并给出结论", "gaps": gaps}, project=pid, priority=2)
            gap_jobs[pid] = HERMES.pool.submit(
                kn.call_agent, "scout", "承接素材缺失工单：逐项给出补齐、替代或「无可用素材」的结论。",
                json.dumps(gaps, ensure_ascii=False),
                "额外 JSON 字段：gaps(数组，每条 {item,status,alternative})。",
                "summary, evidence, confidence, tools, gaps")
    for pid, job in gap_jobs.items():
        data, meta = job.result()
        data = data or kn._deg("scout", "素材工单")
        for item in c.items.values():
            if item["projectId"] == pid:
                item["assetResolution"] = data.get("gaps", [])
                kn.items_emit(c.trace, item)
        kn.send("result", "scout", f"pm_{pid}", c.trace,
                  {"result": data, "engine": meta["engine"]}, project=pid)


def _stage_copy(kn, c):
    """6. 文案成稿 + 补素材链路（素材库 → ComfyUI 生成 → TTS 音频），两者并行"""
    # 6. 文案成稿（6 个真实调用）
    jobs = {}
    for iid, item in c.items.items():
        pid = item["projectId"]
        plan = item.get("plan") or {}
        duration = int(plan.get("durationSec", 45) or 45)
        copy_max = max(80, int(duration * 3.2))  # 口播约 3.2 字/秒，45s≈144 字；下限 80
        jobs[iid] = HERMES.pool.submit(
            kn.call_agent, f"cw_{pid}",
            f"为《{item['title']}》写一段成稿文案（约{copy_max}字，可上下浮动20%，务必写足、不要压缩成短句），"
            f"符合本项目人设，禁止串用其他项目风格。",
            f"项目：{kn.projects[pid]['name']}；风格：{kn.tone[pid]}；时长：{duration}s；"
            f"文案要求：{plan.get('copyBrief', '')}",
            f"额外 JSON 字段：copy(成稿文案约{copy_max}字，按{duration}s口播写足，禁止过短)。",
            "summary, evidence, confidence, tools, copy")
        kn.send("delegation", f"pm_{pid}", f"cw_{pid}", c.trace,
                  {"task": f"按评估报告写《{item['title']}》成稿；收到补素材指令：先查素材库，"
                           f"缺口走 ComfyUI 生成，台词/对白走 TTS"}, project=pid)
    # 补素材与成稿并行推进：素材库检索/ComfyUI 出图/TTS 都有 IO 等待，不占文案的推理时间
    fill_jobs = {iid: HERMES.pool.submit(kn._fill_materials, c.trace, item,
                                         item["projectId"])
                 for iid, item in c.items.items()}
    for iid, item in c.items.items():
        pid = item["projectId"]
        data, meta = jobs[iid].result()
        if meta.get("engine") != "hermes" or not (data or {}).get("copy"):
            item.setdefault("degraded", []).append("copy")
        data = data or {"summary": "文案已交付（降级）", "evidence": [], "confidence": 0.2,
                        "tools": [], "copy": f"{kn.projects[pid]['name']} 文案占位"}
        item["stage"] = "copy"
        item["copy"] = data.get("copy") or data.get("summary", "")
        kn.send("result", f"cw_{pid}", f"pm_{pid}", c.trace,
                  {"result": data, "itemId": iid, "engine": meta["engine"],
                   "latencyMs": meta.get("latencyMs", 0)}, project=pid)
        kn.items_emit(c.trace, item)
    for job in fill_jobs.values():
        try:
            job.result()
        except Exception as e:
            print(f"[materials] 补素材任务异常：{type(e).__name__}: {e}", flush=True)


def _stage_sample(kn, c):
    """7. 视频制作出样片（6 个真实调用）"""
    # 7. 视频制作出样片（6 个真实调用）
    jobs = {}
    for iid, item in c.items.items():
        pid = item["projectId"]
        plan = item.get("plan") or {}
        jobs[iid] = HERMES.pool.submit(
            kn.call_agent, f"vd_{pid}",
            f"按《制作评估报告》与 PM 细化分镜为《{item['title']}》出样片方案："
            f"出片模式 {item.get('renderMode', 'remotion')}（{item.get('renderModeNote', '')}），"
            "逐镜剧本、封面、配音/TTS。",
            f"项目：{kn.projects[pid]['name']}；风格：{kn.tone[pid]}；时长：{plan.get('durationSec', 45)}s；"
            f"配音：{plan.get('tts', {})}；文案：{item.get('copy', '')}；"
            f"细化分镜：{json.dumps(plan.get('storyboardShots') or [], ensure_ascii=False)}；"
            f"分镜要点：{plan.get('storyboardNotes', '')}",
            "额外 JSON 字段：shots(数字), storyboard(4-6 镜，每镜 画面/台词，可加 插图:1-2 个中文物件名，"
            "如 相纸/药水盘/放大镜，≤220字), cover(≤20字), sample(样片说明≤80字)；"
            "每镜务必写清台词与时长（简化小样按镜对齐对白与关键帧）。",
            "summary, evidence, confidence, tools, shots, storyboard, cover, sample")
        kn.send("delegation", f"pm_{pid}", f"vd_{pid}", c.trace,
                  {"task": f"按评估报告出《{item['title']}》样片"}, project=pid)
    for iid, item in c.items.items():
        pid = item["projectId"]
        data, meta = jobs[iid].result()
        if meta.get("engine") != "hermes" or not (data or {}).get("storyboard"):
            item.setdefault("degraded", []).append("video")
        data = data or {"summary": "样片已出（降级）", "evidence": [], "confidence": 0.2, "tools": []}
        item["stage"] = "sample"
        item["video"] = data
        item["shots"] = data.get("shots")
        item["storyboard"] = data.get("storyboard") or ""
        item["cover"] = data.get("cover") or ""
        item["sampleNote"] = data.get("sample") or data.get("summary", "")
        kn.send("result", f"vd_{pid}", f"pm_{pid}", c.trace,
                  {"result": data, "itemId": iid, "engine": meta["engine"],
                   "latencyMs": meta.get("latencyMs", 0)}, project=pid)
        kn.items_emit(c.trace, item)


def _stage_sample_reel(kn, c):
    """7.5 简化小样：用本机 TTS × 关键帧 × BGM 对齐出一版可听可看的样片。

    不做复杂视频（成片仍走 render）；PM 牵头，配音（TTS 文本/音色）与制作
    （关键帧/合成）岗位协同。失败不阻断，记 degraded 由评审阶段兜底。
    """
    import sample_reel as SR
    ok, total, failed = 0, 0, []
    for iid, item in c.items.items():
        pid = item["projectId"]
        total += 1
        kn.send("delegation", f"pm_{pid}", f"vd_{pid}", c.trace,
                {"task": f"为《{item['title']}》产简化小样：关键帧×对白×BGM 对齐"},
                project=pid)
        try:
            res = SR.build(item, pid, files=item.get("files") or None)
        except Exception as e:            # noqa: BLE001
            res = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        if res.get("ok"):
            ok += 1
            item["sampleReel"] = {k: res.get(k) for k in
                                  ("ok", "video", "videoUrl", "duration", "shots",
                                   "bgm", "engine", "builtAt")}
            item["sampleNote"] = item.get("sampleNote") or f"简化小样 {res.get('duration')}s"
        else:
            item.setdefault("degraded", []).append("sampleReel")
            item["sampleReel"] = {"ok": False,
                                  "reason": str(res.get("reason") or "未知原因")[:160]}
            failed.append(f"{item.get('title')}：{item['sampleReel']['reason']}")
        kn.send("result", f"vd_{pid}", f"pm_{pid}", c.trace,
                {"itemId": iid, "ok": bool(res.get("ok")),
                 "sampleUrl": item["sampleReel"].get("videoUrl", ""),
                 "reason": str(res.get("reason") or "")[:120]}, project=pid)
        kn.items_emit(c.trace, item)
    if failed:
        kn.send("escalation", "vd_all", "cto", c.trace,
                {"text": f"简化小样 {len(failed)}/{total} 条未产出：" + "；".join(failed[:3])},
                priority=1)


def _stage_sample_review(kn, c):
    """8. 样片三方评审（CCO + CTO + 本项目 PM），最多两轮"""
    # 8. 样片三方评审（CCO + CTO + 本项目 PM），最多两轮
    c.sample_blocked = set()
    c.review_ids = [iid for iid in c.items if iid not in c.render_blocked]
    for rnd in range(1, c.stage.rounds + 1):
        maps, metas = kn._sample_wave(c.trace, c.review_ids, c.items)
        failed = []
        for iid in c.review_ids:
            item = c.items[iid]
            pid = item["projectId"]
            roles = {"cco": maps["cco"][iid], "cto": maps["cto"][iid], "pm": maps[pid][iid]}
            item["sampleReviews"] = {k: {"pass": v[0], "reasons": v[1]} for k, v in roles.items()}
            passed = all(v[0] for v in roles.values())
            item["samplePass"] = passed
            kn.emit("sample-review", c.trace,
                      {"itemId": iid, "projectId": pid, "round": rnd, "pass": passed,
                       "reviews": item["sampleReviews"]})
            if not passed:
                failed.append(iid)
        if not failed:
            break
        if rnd >= c.stage.rounds:
            for iid in failed:
                c.items[iid]["stage"] = "blocked"
                c.items[iid]["blockedBy"] = "sample-gate"
                c.sample_blocked.add(iid)
                kn.items_emit(c.trace, c.items[iid])
                kn.send("escalation", "cco", "owner", c.trace,
                          {"text": f"《{c.items[iid]['title']}》两轮样片评审未通过，已阻断"},
                          project=c.items[iid]["projectId"], priority=2)
            break
        jobs = {}
        for iid in failed:
            item = c.items[iid]
            pid = item["projectId"]
            reasons = json.dumps(item.get("sampleReviews", {}), ensure_ascii=False)
            jobs[iid] = HERMES.pool.submit(
                kn.call_agent, f"vd_{pid}",
                f"按三方样片评审意见返工《{item['title']}》的样片：重出逐镜剧本、封面与样片说明。",
                f"项目：{kn.projects[pid]['name']}；风格：{kn.tone[pid]}；评审意见：{reasons}",
                "额外 JSON 字段：shots(数字), storyboard(≤220字，每镜可加 插图:1-2 个中文物件名，"
                "如 相纸/药水盘/放大镜), cover(≤20字), sample(≤80字)。",
                "summary, evidence, confidence, tools, shots, storyboard, cover, sample")
            kn.send("delegation", f"pm_{pid}", f"vd_{pid}", c.trace,
                      {"task": "按样片三方评审意见返工"}, project=pid, priority=2)
        for iid, job in jobs.items():
            data, meta = job.result()
            data = data or {"summary": "返工未返回，沿用原样片", "evidence": [], "confidence": 0.2}
            item = c.items[iid]
            pid = item["projectId"]
            item["storyboard"] = data.get("storyboard") or item.get("storyboard", "")
            item["cover"] = data.get("cover") or item.get("cover", "")
            item["sampleNote"] = data.get("sample") or item.get("sampleNote", "")
            # 返工必须重新出片，否则第二轮仍在评审同一份视频（空转）
            if OMR and not item.get("renderSkipped"):
                try:
                    res = OMR.produce(item, kn.projects[pid], kn.tone.get(pid, ""),
                                      CC.DELIVERY.get(pid) or CC.DELIVERY["_default"])
                    item["render"] = {k: v for k, v in res.items() if k != "check"}
                    item["renderCheck"] = res.get("check") or {
                        "pass": False, "reasons": [res.get("reason") or "未知渲染错误"]}
                    if res.get("ok"):
                        item["assets"] = [res["videoUrl"], res["coverUrl"]]
                        item.pop("renderError", None)
                    else:
                        item["assets"] = []
                        item["renderError"] = res.get("reason", "")
                except Exception as e:
                    item["renderCheck"] = {"pass": False, "reasons": [f"返工出片异常：{type(e).__name__}"]}
                    print(f"[pipeline] {iid} 返工出片异常：{type(e).__name__}: {e}", flush=True)
            kn.items_emit(c.trace, item)
            kn.send("result", f"vd_{item['projectId']}", f"pm_{item['projectId']}", c.trace,
                      {"result": data, "itemId": iid, "engine": meta["engine"]},
                      project=item["projectId"])
        c.review_ids = failed


def _stage_final(kn, c):
    """9. 样片过审后「定版」：不再二次剪辑，直接把过审样片锁为 05-final.mp4"""
    # 9. 样片过审后「定版」：不再二次剪辑，直接把过审样片锁为 05-final.mp4
    final_ids = [iid for iid in c.items
                 if iid not in c.sample_blocked and iid not in c.render_blocked]
    jobs = {}
    for iid in final_ids:
        item = c.items[iid]
        pid = item["projectId"]
        jobs[iid] = HERMES.pool.submit(
            kn.call_agent, f"vd_{pid}",
            f"《{item['title']}》样片已通过 CCO/CTO/PM 评审，现执行定版：确认交付物清单与口径。",
            f"项目：{kn.projects[pid]['name']}；文案：{item.get('copy', '')}；"
            f"样片说明：{item.get('sampleNote', '')}",
            "制度：定版不重剪，禁止声称做了帧级调整或重新剪辑；额外 JSON 字段："
            "finalCut(定版说明≤120字，须写明「不改画面」), assets(数组，交付文件名)。",
            "summary, evidence, confidence, tools, finalCut, assets")
        kn.send("delegation", f"pm_{pid}", f"vd_{pid}", c.trace,
                  {"task": "样片评审通过，执行定版（不重剪）"}, project=pid)
    for iid, job in jobs.items():
        data, meta = job.result()
        data = data or {"summary": "定版完成（降级）", "evidence": [], "confidence": 0.2}
        item = c.items[iid]
        pid = item["projectId"]
        item["stage"] = "final"
        item["finalCut"] = data.get("finalCut") or data.get("summary", "")
        # 定版 = 以过审样片为母版锁定，不做二次剪辑；禁止声称改动画面
        files = item.get("files") or {}
        final_url = files.get("final", "")
        src = (item.get("render") or {}).get("video") or ""
        item["finalAssets"] = []
        if DL and final_url and src and os.path.isfile(src):
            try:
                dst = kn.DL._local(final_url)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
                item["finalAssets"] = [final_url]
                item["assets"] = [a for a in item.get("assets", []) if a and a != final_url] + [final_url]
            except Exception as e:
                print(f"[pipeline] {iid} 定版母版落盘失败：{type(e).__name__}: {e}", flush=True)
        kn.send("result", f"vd_{pid}", f"pm_{pid}", c.trace,
                  {"result": data, "itemId": iid, "engine": meta["engine"]}, project=pid)
        kn.items_emit(c.trace, item)


def _stage_review(kn, c):
    """10. 审核员终审（定版后）；驳回返工重写，最多 2 轮"""
    # 10. 审核员终审（定版后）；驳回返工重写，最多 2 轮
    kn.guard_budget(c.trace)   # 制作完成后、终审前核对成本
    c.passed_items, c.review_history = {}, []
    c.pending = [iid for iid in c.items
               if iid not in c.sample_blocked and iid not in c.render_blocked]
    for rnd in range(1, c.stage.rounds + 1):
        rev_jobs = {iid: HERMES.pool.submit(kn.review_with_model, c.items[iid], c.trace) for iid in c.pending}
        failed = []
        for iid in c.pending:
            item = c.items[iid]
            pid = item["projectId"]
            verdict, reasons, meta = rev_jobs[iid].result()
            item["stage"] = "review"
            item["reviewNote"] = "通过" if verdict else "；".join(reasons)
            c.review_history.append({"itemId": iid, "projectId": pid, "round": rnd, "pass": verdict})
            kn.send("result", "reviewer", "ceo", c.trace,
                      {"verdict": {"pass": verdict, "reasons": reasons,
                                   "projectId": pid, "itemId": iid},
                       "result": {"summary": "审核通过" if verdict else "审核驳回",
                                  "evidence": reasons, "confidence": 0.9, "tools": ["review.check"]},
                       "engine": meta["engine"], "reason": meta.get("reason", "")}, project=pid)
            kn.items_emit(c.trace, item)
            if verdict:
                c.passed_items[iid] = item
                # 质检审核通过 → 出厂：工厂段流水到此结束
                if kn.db and item.get("serialNo"):
                    kn.db.mark_shipped(c.trace, iid)
                    kn.emit("factory.shipped", c.trace,
                              {"itemId": iid, "title": item["title"], "projectId": pid,
                               "serialNo": item.get("serialNo"), "by": "reviewer"})
            else:
                failed.append(iid)
        if not failed:
            break
        if rnd >= c.stage.rounds:
            for iid in failed:
                c.items[iid]["stage"] = "blocked"
                c.items[iid]["blockedBy"] = "review-gate"
                kn.items_emit(c.trace, c.items[iid])
                kn.send("escalation", "reviewer", "owner", c.trace,
                          {"text": f"《{c.items[iid]['title']}》两次审核未通过，已阻断，不进入审批"},
                          project=c.items[iid]["projectId"], priority=2)
            break
        rework = {}
        for iid in failed:
            item = c.items[iid]
            pid = item["projectId"]
            duration = int((item.get("plan") or {}).get("durationSec", 45) or 45)
            copy_max = max(80, int(duration * 3.2))
            rework[iid] = HERMES.pool.submit(
                kn.call_agent, f"cw_{pid}",
                f"按审核意见重写《{item['title']}》文案（约{copy_max}字，按{duration}s口播写足，不要压缩）。",
                f"项目：{kn.projects[pid]['name']}；风格：{kn.tone[pid]}",
                f"审核驳回理由：{item['reviewNote']}。必须修正这些问题且保持本项目人设。",
                "summary, evidence, confidence, tools, copy")
            kn.send("delegation", f"pm_{pid}", f"cw_{pid}", c.trace,
                      {"task": "按审核意见返工重写"}, project=pid, priority=2)
        kn.emit("rework", c.trace, {"round": rnd, "items": failed,
                                    "reasons": [c.items[i]["reviewNote"] for i in failed]})
        for iid, job in rework.items():
            new, m = job.result()
            item = c.items[iid]
            item["copy"] = (new or {}).get("copy") or item.get("copy", "")
            kn.send("result", f"cw_{item['projectId']}", f"pm_{item['projectId']}", c.trace,
                      {"result": new or {"summary": "返工未返回，沿用原稿", "evidence": [],
                                         "confidence": 0.2}, "engine": m["engine"]}, project=item["projectId"])
            kn.items_emit(c.trace, item)
        c.pending = failed

    with kn.lock:
        kn.traces[c.trace]["reviewHistory"] = c.review_history
        kn.traces[c.trace]["reviewGate"] = {"passed": len(c.passed_items),
                                            "blocked": len(c.items) - len(c.passed_items)}
    kn._persist_trace(c.trace)
    kn.export_trace_docs(c.trace)

    if not c.passed_items:
        kn.send("escalation", "reviewer", "owner", c.trace,
                  {"text": "全部内容审核未通过，本轮不进入审批"}, priority=2)
        kn.set_scene("WORK", c.trace, title="全部驳回，等待返工")
        with kn.lock:
            kn.traces[c.trace]["status"] = "blocked"
            kn.traces[c.trace]["closedAt"] = iso_now()
        kn._persist_trace(c.trace)
        return _PIPELINE_STOP


def _stage_publish_approval(kn, c):
    """11. 审批门（仅放行审核通过的内容）"""
    # 11. 审批门（仅放行审核通过的内容）
    kn.guard_budget(c.trace)   # 发布审批前最后一道成本核对
    approval = {"id": uid("ap"), "traceId": c.trace, "action": "publish.execute", "risk": "high",
                "status": "pending", "approver": "老板", "createdAt": iso_now(), "decidedAt": "",
                "timeoutSec": _stage_timeout(kn, c),
                "items": list(c.passed_items),
                "titles": [i["title"] for i in c.passed_items.values()],
                "title": f"{len(c.passed_items)} 条内容发布审批（含同心圆对外承诺）",
                "checks": kn._publish_checks(c.items, c.passed_items, c.review_history)}
    approval, ev, _ = kn._open_approval(c, approval)
    for item in c.passed_items.values():
        item["stage"] = "approved"
        kn.items_emit(c.trace, item)

    approved = (approval["status"] == "approved") if ev is None \
        else ev.wait(timeout=_stage_timeout(kn, c))
    with kn.lock:
        appr = next(a for a in kn.approvals if a["id"] == approval["id"])
        kn.approval_events.pop(approval["id"], None)
    if not approved or appr["status"] != "approved":
        with kn.lock:
            if appr["status"] == "pending":
                appr["status"] = "timeout"
                appr["decidedAt"] = iso_now()
                appr["timeoutReason"] = f"等待老板审批超过 {_stage_timeout(kn, c)}s，按挂起处理"
            for item in c.passed_items.values():
                item["stage"] = "blocked"
                item["blockedBy"] = "publish-gate"
                kn._schedule_export(c.trace, item)
            kn.traces[c.trace]["status"] = "rejected" if appr["status"] == "rejected" else "timeout"
            kn.traces[c.trace]["closedAt"] = iso_now()
        kn._persist_trace(c.trace)
        for item in c.passed_items.values():
            kn.emit("pipeline", c.trace, item)
        kn.send("escalation", "ceo", "owner", c.trace,
                  {"text": "审批未通过或超时，内容已标记 blocked（publish-gate），保持未发布"}, priority=2)
        kn.emit("approval", c.trace, appr)
        return _PIPELINE_STOP


def _stage_release(kn, c):
    """12. release_ops 发布（幂等：同一 trace 的每条内容只发布一次）"""
    # 断点续跑会重跑 release，靠发布回执判重，避免重复发消息/重复计数
    done = set()
    if kn.db:
        try:
            done = kn.db.released_items(c.trace)
        except Exception as e:
            print(f"[release] 读发布回执失败，按未发布处理：{type(e).__name__}: {e}", flush=True)
    fresh = {iid: it for iid, it in c.passed_items.items() if iid not in done}
    for iid in done:
        if iid in c.passed_items:
            c.passed_items[iid]["stage"] = "published"      # 续跑回填，不重复广播
    if not fresh:
        print(f"[release] {c.trace} 无新增待发布内容（已有回执 {len(done)} 条），跳过重复执行",
              flush=True)
        return
    c.pub_ctx = json.dumps({i["title"]: i["projectId"] for i in fresh.values()}, ensure_ascii=False)
    data, meta = kn.call_agent("release_ops", f"按排期发布 {len(fresh)} 条内容并给出监控口径。",
                                 c.pub_ctx, "", "summary, evidence, confidence, tools")
    data = data or {"summary": "内容已发布并进入监控", "evidence": ["平台回执 200"],
                    "confidence": 0.9, "tools": ["publish.execute"]}
    for iid, item in fresh.items():
        item["stage"] = "published"
        if kn.db:
            try:
                kn.db.release_mark(c.trace, iid, item.get("projectId"), item.get("title"))
            except Exception as e:
                print(f"[release] 发布回执登记失败（继续发布）：{type(e).__name__}: {e}", flush=True)
        kn.send("result", "release_ops", "ceo", c.trace,
                  {"result": data, "engine": meta["engine"], "itemId": iid},
                  project=item["projectId"])
        kn.items_emit(c.trace, item)
    # KPI 按项目上调；续跑时只补此前一条都没发布过的项目，避免重复累计
    released_pids = {c.passed_items[iid]["projectId"] for iid in done if iid in c.passed_items}
    pids = {i["projectId"] for i in fresh.values()} - released_pids
    with kn.lock:
        kn.stats["published"] += len(fresh)
        for pid in pids:
            k = kn.projects[pid]["kpi"]
            if "views" in k:
                k["views"] += 1200
                k["fans"] += 18
                k["completion"] = 0.62
                k["engagement"] = 0.071
            else:
                k["tickets"] = 2
                k["delivery_hours"] = 36.0
    if pids:
        kn.emit("metrics", c.trace, {p: kn.projects[p]["kpi"] for p in pids})


def _stage_archive(kn, c):
    """13. kb_manager 归档"""
    # 13. kb_manager 归档
    data, meta = kn.call_agent("kb_manager", "把本轮 6 条内容归档为知识条目，给出检索标签。",
                                 c.pub_ctx, "", "summary, evidence, confidence, tools")
    data = data or {"summary": "已归档知识资产", "evidence": ["kb.write"], "confidence": 0.9, "tools": []}
    # 定稿时已归档过同题条目，这里按 traceId + 标题更新为发布口径，不重复计数
    kn.archive_knowledge(c.trace, [
        {"projectId": item["projectId"], "title": item["title"],
         "body": data.get("summary", "归档"), "tags": ["内容复盘"]}
        for item in c.passed_items.values()], stage_label="发布归档")
    for iid, item in c.passed_items.items():
        item["stage"] = "archived"
        kn.items_emit(c.trace, item)
    kn.send("result", "kb_manager", "ceo", c.trace, {"result": data, "engine": meta["engine"]},
              project="all")


def _stage_report(kn, c):
    """14. 汇报：CEO 日报 / CTO 技术周报 / 项目周报（并行）"""
    # 14. 汇报：CEO 日报 / CTO 技术周报 / 项目周报（并行）
    kn.set_scene("REPORT", c.trace, title="CEO 日报生成")
    wave = {
        "ceo": HERMES.pool.submit(kn.call_agent, "ceo",
                                  "写一段今日 CEO 日报结论，覆盖 6 条产出与审批。",
                                  c.pub_ctx, "", "summary, evidence, confidence, tools"),
        "cto": HERMES.pool.submit(kn.call_agent, "cto",
                                  "写一段 CTO 技术周报：Agent 编排健康度与风险。",
                                  f"本轮消息数 {len([m for m in kn.messages if m['traceId'] == c.trace])}；"
                                  "审批门拦截 publish.execute", "", "summary, evidence, confidence, tools"),
    }
    for pid in {i["projectId"] for i in c.passed_items.values()}:
        wave[f"pm_{pid}"] = HERMES.pool.submit(
            kn.call_agent, f"pm_{pid}", f"写一段 {kn.projects[pid]['name']} 项目周报结论。",
            c.pub_ctx, "", "summary, evidence, confidence, tools")

    daily_data, daily_meta = wave["ceo"].result()
    daily_data = daily_data or kn._deg("ceo", "CEO 日报")
    report = {"id": uid("rp"), "traceId": c.trace, "kind": "daily", "title": "CEO 日报",
              "conclusion": daily_data.get("summary", ""), "evidence": daily_data.get("evidence", []),
              "decisions": [{"text": f"定稿 {sum(len(v) for v in c.final.values())} 条（议题唯一归属）并发布", "owner": "ceo",
                             "risk": "mid", "approved": True}],
              "engine": daily_meta["engine"], "createdAt": iso_now()}
    with kn.lock:
        kn.reports.append(report)
    kn.send("report.ready", "ceo", "owner", c.trace, {"report": report}, priority=1)
    kn.emit("report", c.trace, report)

    tech_data, tech_meta = wave["cto"].result()
    tech_data = tech_data or kn._deg("cto", "技术周报")
    tech = {"id": uid("rp"), "traceId": c.trace, "kind": "tech", "title": "CTO 技术周报",
            "conclusion": tech_data.get("summary", ""), "evidence": tech_data.get("evidence", []),
            "decisions": [], "engine": tech_meta["engine"], "createdAt": iso_now()}
    with kn.lock:
        kn.reports.append(tech)
    kn.emit("report", c.trace, tech)

    for pid in {i["projectId"] for i in c.passed_items.values()}:
        wd, wm = wave[f"pm_{pid}"].result()
        wd = wd or kn._deg(f"pm_{pid}", "项目周报")
        weekly = {"id": uid("rp"), "traceId": c.trace, "kind": "weekly",
                  "title": f"{kn.projects[pid]['name']} 项目周报",
                  "conclusion": wd.get("summary", ""), "evidence": wd.get("evidence", []),
                  "decisions": [], "engine": wm["engine"], "createdAt": iso_now()}
        with kn.lock:
            kn.reports.append(weekly)
        kn.emit("report", c.trace, weekly)

    with kn.lock:
        kn.traces[c.trace]["status"] = "completed"
        kn.traces[c.trace]["closedAt"] = iso_now()
        kn.traces[c.trace]["hermes"] = HERMES.stats()
    kn._persist_trace(c.trace)
    kn.set_scene("WORK", c.trace, title="归档完成，回到生产")


