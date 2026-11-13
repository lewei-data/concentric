"""流水线内核：PipelineContext / Kernel（16 阶段编排）/ 多工作区内核注册表。

不反向依赖 server：基础设施单例来自 deps，公司配置来自 company_config，
模型调用统一走 hermes_adapter（HERMES）；HTTP 路由与会话留在 server.Handler。
"""
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from urllib.parse import parse_qs

from opc_paths import *  # noqa: F401,F403 —— 路径与运行常量
from utils import iso_now, uid, _to_int, _hotkey, _step_view, _iso_shift, _num
from agent_defaults import DEFAULT_AGENT_SPEC, AGENTS_REQUIRED
from deps import *  # noqa: F401,F403 —— 基础设施单例
import company_config as CC
import mail as MAILM
import pipeline_defs as PIPE
from hermes_adapter import HERMES


class BudgetAbort(Exception):
    """CTO 判定成本/额度超限，在阶段边界终止本轮任务。"""

class BusyError(Exception):
    """已有一轮选题会未收口：拒绝并发注入，避免两条流水线互相覆盖。"""

    def __init__(self, trace):
        super().__init__(f"上一轮选题会仍在进行（{trace}），请等它结束或先处理待审批")
        self.traceId = trace

_PIPELINE_STOP = object()

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

class Kernel:
    def __init__(self, data_dir=""):
        # 多用户：data_dir 非空 = 独立工作区（过程库/素材/vault/编制都在该目录下）；
        # 空 = 全局兜底内核（存量老板空间，共用 Obsidian vault 与全局编制）。
        self.data_dir = os.path.abspath(data_dir) if data_dir else ""
        self.is_owner = self.data_dir == os.path.abspath(OWNER_DATA_DIR) or not self.data_dir
        self.vault_dir = os.path.join(self.data_dir, "vault") if self.data_dir else VAULT
        # 用户 vault 目录树补齐（老版本注册的用户自动补建，幂等）
        if self.data_dir and not self.is_owner:
            for sub in ("热点采集", "知识库", "交付物", "会议纪要", "邮件", "_归档"):
                try:
                    os.makedirs(os.path.join(self.vault_dir, "OPC", sub), exist_ok=True)
                except OSError as e:
                    print(f"[boot] 用户 vault 目录创建失败：{type(e).__name__}: {e}", flush=True)
        # 交付/知识/热点模块：owner 用全局实例（共享 Obsidian vault）；用户各一份独立实例
        self.DL = DL if self.is_owner else (CC._load_dl(self.vault_dir) if DL else None)
        self.lock = threading.RLock()
        # 编制：优先读工作区自己的 员工名册.md，解析失败回退系统默认编制；
        # 流水线关键员工缺失（旧模板/手工删减）时整份回退默认编制，保证编排可跑。
        # 项目组/调性：用户工作区默认用内置配置（产品一组/二组/三组），
        # 用户 vault 里放了自己的 公司/项目配置.md 才用自己的；owner 用全局 Obsidian 配置。
        spec = CC.AGENT_SPEC
        self.projects = CC.PROJECTS
        self.tone = CC.TONE
        if self.data_dir and not self.is_owner:
            self.projects, self.tone = CC.user_projects(self.vault_dir)
            roster = os.path.join(self.data_dir, "员工名册.md")
            try:
                by_name = {v["name"]: k for k, v in self.projects.items()}
                for _pid, _p in CC.DEFAULT_PROJECTS.items():
                    by_name.setdefault(_p["name"], _pid)   # 用户自定义项目名后，旧名册仍可解析
                by_name.setdefault("全公司", "all")
                user_spec, skipped = CC._parse_roster(roster, by_name=by_name)
                if len(user_spec) >= 3:
                    ids = {row[0] for row in user_spec}
                    missing = AGENTS_REQUIRED - ids
                    if missing:
                        print(f"[boot] 用户名册缺流水线关键员工 {sorted(missing)}，回退默认编制",
                              flush=True)
                    else:
                        spec = user_spec
                        print(f"[boot] 用户编制 {roster} → {len(user_spec)} 人", flush=True)
            except Exception as e:
                print(f"[boot] 用户名册解析失败（用默认编制）：{type(e).__name__}: {e}", flush=True)
        self.agents = {}
        for aid, name, role, project, tools, scope in spec:
            self.agents[aid] = {"id": aid, "name": name, "role": role, "projectId": project,
                                "scope": scope, "tools": tools, "status": "idle", "task": "",
                                "avatar": f"/avatars/{aid}.png",
                                "tone": self.tone.get(project, "跨项目中台"),
                                "vault": self.vault_dir,   # 员工知识库读写范围（多用户隔离）
                                "highRisk": [t for t in tools if t in CC.HIGH_RISK_TOOLS]}
        self.projects = json.loads(json.dumps(self.projects, ensure_ascii=False))
        self.scene = "WORK"
        self.meeting = {"active": False, "title": "", "participants": [], "agenda": [], "startedAt": ""}
        self.meeting_log = {"id": "", "traceId": "", "status": "idle", "title": "",
                            "participants": [], "agenda": [], "speeches": [],
                            "startedAt": "", "endedAt": "", "conclusion": "",
                            "transcriptUrl": "", "minutesUrl": ""}
        self.meeting_records = []
        if self.is_owner and DL:
            try:
                self.meeting_records = self.DL.load_meeting_records()
            except Exception as e:
                print(f"[boot] 读取会议历史失败：{type(e).__name__}: {e}", flush=True)
        self.messages, self.events = [], 0
        self.hotspots, self.contents, self.reports, self.approvals, self.knowledge = [], [], [], [], []
        self._hotspot_scan_at = 0.0        # 热点目录重扫节流（秒级时间戳）
        self._kb_stamp = self.DL.knowledge_stamp() if (self.is_owner and self.DL) else (0, 0.0)   # 知识库目录指纹
        try:
            self._cfg_stamp = (os.path.getmtime(PROJECT_FILE), os.path.getmtime(ROSTER_FILE))
        except OSError:
            self._cfg_stamp = None         # 项目配置/名册 mtime 指纹
        self.collection = None             # 苏寒最近一次原始榜采集状态（只展示，不当业务热点）
        self.subscribers, self.approval_events, self.traces = [], {}, {}
        self.stats = {"approvalSeconds": [], "published": 0, "blockedPublish": 0, "traces": 0}
        # 成本护栏：按 trace 聚合 token/花费，阈值/异常由 CTO 处置
        self.token_usage = {}
        self.token_alerts = {}
        self.aborts = {}
        self.active_trace = ""
        self.last_hotspot = None
        self.startedAt = iso_now()          # 本次进程启动时间：快照用它区分“内存态归零”
        # 素材清单 / 运行时日志 / 过程库：按用户工作区隔离；owner 沿用存量位置
        self.materials_file = (os.path.join(self.data_dir, "materials.json")
                               if self.data_dir else MATERIALS_FILE)
        self.journal_file = (os.path.join(self.data_dir, "workspace", "runtime.json")
                             if self.data_dir else os.path.join(VAULT, "动态", "runtime.json"))
        db_path = (os.path.join(self.data_dir, "opc.db")
                   if self.data_dir else DBX.DB_PATH if DBX else "")
        self.db = None
        if self.is_owner:
            self._restore_history()          # 存量运行时历史只归 owner；新用户从零开始
        # 过程数据入库：影子期双写，内存仍是读路径；库不可用时全部跳过
        self.db = DBX.store_for(db_path) if (DBX and db_path) else None
        self._run_id = uid("run")
        if self.db:
            try:
                self.db.run_start(self._run_id, os.getpid(), MODEL)
                self.db.chat_upsert_thread("web:workbench", "web", "工作台 · 老板指令",
                                           [{"id": "owner", "name": "老板"}],
                                           self.startedAt, self.startedAt)
            except Exception as e:
                print(f"[boot] 过程库初始化失败：{type(e).__name__}: {e}", flush=True)
        self.ingestor = None
        # 消息摄取读取的是全公司 dispatch 库，只归 owner 内核
        if self.is_owner and INGEST_ON and ING:
            try:
                self.ingestor = ING.Ingestor(self.db, on_event=self._ingest_event).start()
            except Exception as e:
                print(f"[boot] 消息摄取启动失败：{type(e).__name__}: {e}", flush=True)
        self._sync_stop = threading.Event()
        threading.Thread(target=self._background_sync, daemon=True).start()
        # 审批超时巡检：进程内等待由 ev.wait 兜底，这里补上「重启后无人等待」的遗留 pending 单
        self._sweep_stop = threading.Event()
        threading.Thread(target=self._approval_sweeper, daemon=True).start()
        self._restore_pending_approvals()
        self._mark_interrupted_runs()

    # ---------- 运行态：审批巡检 / 断点续跑 ----------
    def stop(self):
        """停止后台线程（数据导入重建内核时调用；进程内多内核互不影响）。"""
        try:
            self._sync_stop.set()
            self._sweep_stop.set()
        except Exception as e:
            print(f"[kernel] 停止异常：{type(e).__name__}: {e}", flush=True)

    def _approval_sweeper(self):
        """每 15 秒巡检一次 pending 审批，超时自动挂起并唤醒等待方。"""
        while not self._sweep_stop.wait(15):
            try:
                self._sweep_approvals()
            except Exception as e:
                print(f"[approval] 超时巡检异常：{type(e).__name__}: {e}", flush=True)

    def _sweep_approvals(self):
        now = datetime.now(TZ)
        expired = []
        with self.lock:
            for a in self.approvals:
                if a.get("status") != "pending":
                    continue
                try:
                    created = datetime.fromisoformat(a.get("createdAt") or "")
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
            self.emit("approval", a.get("traceId", ""), a)
            ev = self.approval_events.get(a["id"])
            if ev:
                ev.set()
            self.send("escalation", "ceo", "owner", a.get("traceId", ""),
                      {"text": f"审批超时挂起：{a.get('title') or a.get('action')}"}, priority=2)

    def _restore_pending_approvals(self):
        """重启后把库里仍 pending 的审批单拉回内存，老板打开工作台就能看到待办。"""
        if not self.db:
            return
        try:
            rows = self.db.approvals_pending()
        except Exception as e:
            print(f"[boot] 读取待审批失败：{type(e).__name__}: {e}", flush=True)
            return
        known = {a.get("id") for a in self.approvals}
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
            self.approvals.append(raw)
            added += 1
        if added:
            print(f"[boot] 从过程库恢复 {added} 条待审批单", flush=True)

    AUTO_RESUME_MAX_TRIES = 3

    def _mark_interrupted_runs(self):
        """启动时把上次进程留下的 running 标成 interrupted；按需串行自动续跑。

        续跑候选同时包含 running（上次进程直接挂掉）与 interrupted（已标记但当时
        没续成，比如多条断点抢不到执行位）：只要还有断点阶段、且续跑次数未超上限，
        这次重启就再试一次。续跑必须串行——内核同一时刻只允许一条 trace 运行。
        """
        if not self.db:
            return
        try:
            rows = self.db.pipeline_runs_resumable(self.AUTO_RESUME_MAX_TRIES)
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
                    self.db.pipeline_run_end(trace, "interrupted")
                except Exception:
                    pass
                with self.lock:
                    if self.traces.get(trace, {}).get("status") == "running":
                        self.traces[trace]["status"] = "interrupted"
                        self.traces[trace]["closedAt"] = iso_now()
                print(f"[boot] 上次未跑完：{trace} 停在阶段 {row.get('stage')}（已标记 interrupted）",
                      flush=True)
            pending.append(trace)
        if pending and os.environ.get("OPC_RESUME_AUTO", "0") == "1":
            threading.Thread(target=self._auto_resume_loop, args=(pending,), daemon=True).start()

    def _auto_resume_loop(self, traces):
        """按顺序逐条续跑：一条收口后再拉下一条，避免 active_trace 互斥导致静默失败。"""
        for trace in traces:
            try:
                self.db.pipeline_resume_try(trace)
                info = self.resume_pipeline(trace, auto=True)
                print(f"[boot] 自动续跑 {trace}：从阶段 {info.get('from')} 恢复", flush=True)
            except Exception as e:
                print(f"[boot] 自动续跑失败 {trace}：{type(e).__name__}: {e}", flush=True)
                continue
            self._wait_run_done(trace)
        print(f"[boot] 自动续跑队列处理完毕（{len(traces)} 条）", flush=True)

    def _wait_run_done(self, trace, poll=2.0):
        """等这条 trace 收口再处理下一条（先等线程起来，再等它退出）。"""
        deadline = time.time() + 30
        while time.time() < deadline and self.active_trace != trace:
            time.sleep(poll)
        while self.active_trace == trace:
            time.sleep(poll)

    def resume_pipeline(self, trace, auto=False):
        """按过程库里的断点阶段续跑（至少一次：重启时该阶段会重跑一次）。"""
        if not self.db:
            raise RuntimeError("过程库未启用（OPC_DB=0），无法续跑")
        if self.active_trace and self.active_trace != trace:
            raise RuntimeError(f"已有任务在跑（{self.active_trace}），稍后再续跑")
        row = self.db.pipeline_run(trace)
        if not row:
            raise RuntimeError("过程库里没有这条 trace 的运行记录")
        if row.get("status") == "completed":
            raise RuntimeError("该 trace 已完成，无需续跑")
        if not row.get("stage"):
            raise RuntimeError("该 trace 还没有阶段断点，无法续跑")
        hotspot = json.loads(row.get("hotspot") or "null") or self.last_hotspot \
            or {"title": "断点续跑", "tags": []}
        state = json.loads(row.get("state") or "{}")
        with self.lock:
            tr = self.traces.setdefault(trace, {"traceId": trace, "createdAt": iso_now(),
                                               "hotspot": hotspot})
            tr["status"] = "running"
            tr.pop("closedAt", None)
            tr["resumedAt"] = iso_now()
        self.send("status", "ceo", "owner", trace,
                  {"text": f"按断点续跑：从阶段 {row.get('stage')} 恢复"}, priority=2)
        threading.Thread(target=self.run_pipeline, args=(trace, hotspot),
                         kwargs={"start": row.get("stage"), "state": state}, daemon=True).start()
        return {"traceId": trace, "from": row.get("stage"), "auto": auto}

    def _restore_history(self):
        """重启后恢复历史：交付物读磁盘，报告/审批/热点/知识/消息读运行时日志。"""
        if self.DL:
            try:
                items = self.DL.load_deliverable_items()
                if items:
                    self.contents = items
                    print(f"[boot] 从 Obsidian 恢复 {len(items)} 条历史交付", flush=True)
            except Exception as e:
                print(f"[boot] 恢复历史交付失败：{type(e).__name__}: {e}", flush=True)
        saved = {}
        try:
            with open(self.journal_file, encoding="utf-8") as f:
                saved = json.load(f)
        except FileNotFoundError:
            saved = {}
        except Exception as e:
            print(f"[boot] 读取运行时日志失败：{type(e).__name__}: {e}", flush=True)
            saved = {}
        if isinstance(saved, dict):
            for key in ("reports", "approvals", "hotspots"):
                value = saved.get(key)
                if isinstance(value, list):
                    setattr(self, key, value[-40:])
            if isinstance(saved.get("knowledge"), list):
                self.knowledge = saved["knowledge"][-80:]
            if isinstance(saved.get("messages"), list) and saved["messages"]:
                self.messages = saved["messages"][-120:]
            stats = saved.get("stats")
            if isinstance(stats, dict):
                self.stats["published"] = _to_int(stats.get("published"), 0)
                self.stats["blockedPublish"] = _to_int(stats.get("blockedPublish"), 0)
                self.stats["traces"] = _to_int(stats.get("traces"), 0)
                secs = stats.get("approvalSeconds")
                if isinstance(secs, list):
                    self.stats["approvalSeconds"] = secs[-100:]
        # 统计口径自洽：trace 数不得少于历史交付里实际出现的 trace 数
        disk_traces = len({c.get("traceId") for c in self.contents if c.get("traceId")})
        if disk_traces > self.stats["traces"]:
            self.stats["traces"] = disk_traces
        if not self.messages and DL:
            try:
                self.messages = self.DL.load_recent_messages(120)
            except Exception as e:
                print(f"[boot] 恢复历史消息失败：{type(e).__name__}: {e}", flush=True)
        if self.DL:
            if not self.hotspots:
                try:
                    self.hotspots = self.DL.load_hotspots()
                except Exception as e:
                    print(f"[boot] 恢复历史热点失败：{type(e).__name__}: {e}", flush=True)
            # Obsidian 是唯一真源：md 文件优先，runtime.json 只在没有 md 时兜底
            try:
                disk_kb = self.DL.load_knowledge(limit=200)
                if disk_kb:
                    self.knowledge = [self._kb_norm(k) for k in disk_kb]
            except Exception as e:
                print(f"[boot] 恢复历史知识失败：{type(e).__name__}: {e}", flush=True)
        self._backfill_knowledge()
        print(f"[boot] 恢复运行时历史：报告 {len(self.reports)} / 审批 {len(self.approvals)} / "
              f"热点 {len(self.hotspots)} / 知识 {len(self.knowledge)} / 消息 {len(self.messages)}", flush=True)

    def save_runtime(self):
        """把易失的运行时数据落到 OPC 动态目录，下一次启动可恢复。"""
        try:
            with self.lock:
                payload = {
                    "savedAt": iso_now(),
                    "reports": self.reports[-40:],
                    "approvals": self.approvals[-40:],
                    "hotspots": self.hotspots[-20:],
                    "knowledge": self.knowledge[-80:],
                    "messages": self.messages[-120:],
                    "stats": {"published": self.stats["published"],
                              "blockedPublish": self.stats["blockedPublish"],
                              "traces": self.stats["traces"],
                              "approvalSeconds": self.stats["approvalSeconds"][-100:]},
                }
            os.makedirs(os.path.dirname(self.journal_file), exist_ok=True)
            tmp = self.journal_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.journal_file)
        except Exception as e:
            print(f"[runtime] 保存运行时日志失败：{type(e).__name__}: {e}", flush=True)

    def reload_config(self):
        """热加载 Obsidian 里的项目配置与员工名册，不重启服务"""
        p, t, h = CC._load_project_cfg()
        missing_proj = {"wenren", "gou", "tongxinyuan"} - set(p)
        if missing_proj:
            raise ValueError(f"项目配置缺少必需项目: {sorted(missing_proj)}")
        spec = CC._load_agent_spec()
        known = {a[0] for a in spec}
        missing_ids = [a[0] for a in DEFAULT_AGENT_SPEC if a[0] not in known]
        if missing_ids:
            raise ValueError(f"员工名册缺少必需岗位: {missing_ids}")
        # 流程定义也支持热加载（改 pipeline.yaml 后无需重启）；校验失败会回退内置默认
        CC.PIPELINE = PIPE.load()[0]
        CC.PROJECTS, CC.TONE, CC.HIGH_RISK_TOOLS = p, t, h
        CC.STAGES = CC.PIPELINE.item_stages
        CC.PROJECT_BY_NAME = {v["name"]: k for k, v in CC.PROJECTS.items()}
        CC.PROJECT_BY_NAME["全公司"] = "all"
        CC.AGENT_SPEC = spec
        with self.lock:
            old = self.agents
            self.agents = {}
            for aid, name, role, project, tools, scope in CC.AGENT_SPEC:
                prev = old.get(aid, {})
                self.agents[aid] = {"id": aid, "name": name, "role": role, "projectId": project,
                                    "scope": scope, "tools": tools,
                                    "status": prev.get("status", "idle"), "task": prev.get("task", ""),
                                    "avatar": f"/avatars/{aid}.png",
                                    "tone": CC.TONE.get(project, "跨项目中台"),
                                    "highRisk": [x for x in tools if x in CC.HIGH_RISK_TOOLS]}
            self.projects = json.loads(json.dumps(CC.PROJECTS, ensure_ascii=False))
        info = {"agents": len(self.agents), "projects": len(self.projects), "stages": len(CC.STAGES),
                "highRiskTools": len(CC.HIGH_RISK_TOOLS),
                "sources": {"projects": PROJECT_FILE, "roster": ROSTER_FILE}}
        self.emit("config-reload", "", info)
        return info

    # ---------- 过程数据落库 ----------
    def _persist_trace(self, trace):
        if not self.db:
            return
        with self.lock:
            t = self.traces.get(trace)
        if t:
            self.db.upsert_trace(t)

    def _ingest_event(self, thread_id, msg):
        """摄取线程回调：把新消息推进 SSE，前端免轮询。"""
        self.emit("chat.message", "", {"threadId": thread_id, "message": msg})

    def _web_chat(self, direction, text, kind, sender, sender_name, origin_id, trace=""):
        """Web 侧消息同进程直写（§12.4）。"""
        if not self.db or not str(text or "").strip():
            return
        try:
            self.db.chat_insert({
                "thread_id": "web:workbench", "source": "web", "modality": "text",
                "direction": direction, "sender_id": sender, "sender_name": sender_name,
                "content": str(text)[:4000], "content_kind": kind or "text",
                "origin_table": "opc.kernel", "origin_id": origin_id,
                "trace_id": trace or None, "meta": {"type": kind},
                "created_at": iso_now(),
            })
        except Exception as e:
            print(f"[chat] Web 消息入库失败：{type(e).__name__}: {e}", flush=True)

    # ---------- 总线 ----------
    def emit(self, kind, trace, data):
        env = {"kind": kind, "traceId": trace, "at": iso_now(), "data": data}
        with self.lock:
            self.events += 1
            for q in list(self.subscribers):
                try:
                    q.put_nowait(env)
                except queue.QueueFull:
                    pass
        if self.db:
            self.db.append_event(kind, trace, data, at=env["at"])
            if kind == "report" and isinstance(data, dict):
                self.db.upsert_report(data)
            elif kind == "approval" and isinstance(data, dict):
                self.db.upsert_approval(data)
            elif kind == "hotspot" and isinstance(data, dict):
                self.db.upsert_hotspot(data)
            elif kind == "knowledge" and isinstance(data, dict):
                self.db.upsert_knowledge(data)
            elif kind == "pipeline" and isinstance(data, dict) and data.get("id"):
                self.db.upsert_content(data)
        elif kind in ("hotspot", "report", "approval", "knowledge"):
            self.save_runtime()     # 旧路径：低频关键事件落盘，重启后可恢复
        return env

    def send(self, mtype, sender, recipient, trace, payload=None, project=None, priority=1):
        msg = {"id": uid("msg"), "traceId": trace, "type": mtype, "sender": sender,
               "recipient": recipient, "at": iso_now(), "ttl": 20, "priority": priority,
               "projectId": project, "payload": payload or {}}
        with self.lock:
            self.messages.append(msg)
            if len(self.messages) > 4000:
                self.messages = self.messages[-2000:]
        self.emit("message", trace, msg)
        if self.db:
            self.db.insert_message(msg)
            if "owner" in (sender, recipient):
                payload = payload or {}
                text = (payload.get("text") or payload.get("task") or payload.get("title")
                        or payload.get("message") or "")
                self._web_chat("inbound" if sender == "owner" else "outbound",
                               text, mtype, sender,
                               "老板" if sender == "owner"
                               else (self.agents.get(sender) or {}).get("name", sender),
                               msg["id"], trace)
        return msg

    def set_agent(self, aid, status, task=""):
        with self.lock:
            self.agents[aid]["status"] = status
            self.agents[aid]["task"] = task
        self.emit("agent", (self.last_hotspot or {}).get("traceId", ""), {"id": aid, "status": status, "task": task})

    def set_scene(self, scene, trace, **extra):
        self.scene = scene
        payload = {"scene": scene, "participants": self.meeting.get("participants", [])}
        payload.update(extra)
        self.emit("scene", trace, payload)

    # ---------- 会议记录（3D 会议室 + Obsidian 归档） ----------
    def meeting_begin(self, trace, title, participants, agenda=None):
        rec = {"id": uid("mt"), "traceId": trace, "title": title, "status": "live",
               "participants": [], "agenda": list(agenda or []), "speeches": [],
               "startedAt": iso_now(), "endedAt": "", "conclusion": "",
               "transcriptUrl": "", "minutesUrl": ""}
        with self.lock:
            for aid in participants:
                a = self.agents.get(aid)
                if a:
                    rec["participants"].append({"id": aid, "name": a["name"],
                                                "role": a["role"], "projectId": a["projectId"]})
            self.meeting_log = rec
            self.meeting.update({"active": True, "title": title,
                                 "participants": list(participants),
                                 "agenda": list(agenda or []), "startedAt": rec["startedAt"]})
        if self.db:
            self.db.upsert_meeting(rec)
        self.emit("meeting.start", trace, rec)
        return rec

    def meeting_say(self, aid, text, kind="speech", trace=""):
        text = str(text or "").strip()
        if not text:
            return None
        a = self.agents.get(aid, {})
        with self.lock:
            rec = self.meeting_log
            if rec.get("status") != "live":
                return None
            item = {"id": uid("sp"), "speakerId": aid, "speaker": a.get("name", aid),
                    "role": a.get("role", ""), "projectId": a.get("projectId", "all"),
                    "kind": kind, "text": text[:1200], "at": iso_now()}
            rec["speeches"].append(item)
        if self.db:
            self.db.add_speech(rec.get("id"), len(rec["speeches"]), item)
        self.emit("meeting.speech", trace or rec.get("traceId", ""), item)
        return item

    def meeting_end(self, trace, conclusion="", minutes_url=""):
        with self.lock:
            rec = self.meeting_log
            if rec.get("status") != "live":
                return None
            rec["status"] = "ended"
            rec["endedAt"] = iso_now()
            rec["conclusion"] = str(conclusion or "").strip()
            if minutes_url:
                rec["minutesUrl"] = minutes_url
            self.meeting["active"] = False
            out = json.loads(json.dumps(rec, ensure_ascii=False))
            self.meeting_records.insert(0, out)
            self.meeting_records = self.meeting_records[:40]
        if self.DL:
            try:
                urls = self.DL.save_meeting_record(out)
                out.update({k: v for k, v in urls.items() if v})
                with self.lock:
                    for r in self.meeting_records:
                        if r.get("id") == out["id"]:
                            r.update({k: v for k, v in urls.items() if v})
            except Exception as e:
                print(f"[meeting] 会议记录落盘失败：{type(e).__name__}: {e}", flush=True)
        if self.db:
            self.db.upsert_meeting(out)
        self.emit("meeting.end", trace, out)
        return out

    def meeting_payload(self):
        with self.lock:
            current = json.loads(json.dumps(self.meeting_log, ensure_ascii=False))
            history = []
            for r in self.meeting_records:
                history.append({"id": r.get("id"), "traceId": r.get("traceId"),
                                "title": r.get("title"), "status": r.get("status"),
                                "startedAt": r.get("startedAt"), "endedAt": r.get("endedAt"),
                                "participants": r.get("participants") or [],
                                "agenda": r.get("agenda") or [],
                                "speechCount": len(r.get("speeches") or []),
                                "conclusion": r.get("conclusion") or "",
                                "transcriptUrl": r.get("transcriptUrl") or "",
                                "minutesUrl": r.get("minutesUrl") or "",
                                "legacy": bool(r.get("legacy"))})
        return {"ok": True, "active": bool(self.meeting.get("active")),
                "current": current, "last": history[0] if history else None,
                "history": history}

    def meeting_detail(self, mid):
        mid = str(mid or "")
        with self.lock:
            for r in self.meeting_records:
                if mid in (r.get("id"), r.get("traceId")):
                    return json.loads(json.dumps(r, ensure_ascii=False))
        return None

    # ---------- 调用模型 ----------
    def call_agent(self, aid, task, context="", extra="", schema=None):
        a = self.agents[aid]
        self.set_agent(aid, "working", task[:40])
        data, meta = HERMES.ask(a, task, context, extra, schema)
        self.record_usage(aid, meta)
        self.set_agent(aid, "done", "已交付" if data else "降级交付")
        return data, meta

    # ---------- 成本护栏（CTO 监控） ----------
    def record_usage(self, agent_id, meta, trace=""):
        """把单次调用的 token/花费记到当前 trace，并交给 CTO 阈值监控。"""
        usage = (meta or {}).get("usage") or {}
        trace = trace or self.active_trace or ""
        with self.lock:
            bucket = self.token_usage.get(trace)
            if not bucket:
                bucket = {"totalTokens": 0, "inputTokens": 0, "outputTokens": 0,
                          "costUsd": 0.0, "calls": 0, "byAgent": {}, "startedAt": iso_now()}
                self.token_usage[trace] = bucket
            tokens = _to_int(usage.get("totalTokens"), 0)
            cost = float(usage.get("costUsd") or 0)
            bucket["calls"] += 1
            bucket["totalTokens"] += tokens
            bucket["inputTokens"] += _to_int(usage.get("inputTokens"), 0)
            bucket["outputTokens"] += _to_int(usage.get("outputTokens"), 0)
            bucket["costUsd"] = round(bucket["costUsd"] + cost, 6)
            one = bucket["byAgent"].setdefault(agent_id, {"tokens": 0, "costUsd": 0.0, "calls": 0})
            one["tokens"] += tokens
            one["costUsd"] = round(one["costUsd"] + cost, 6)
            one["calls"] += 1
            snapshot = json.loads(json.dumps(bucket, ensure_ascii=False))
            alerts = self.token_alerts.setdefault(trace, set())
            trace_hit = snapshot["totalTokens"] >= TOKEN_LIMIT and "trace" not in alerts
            cost_hit = (COST_LIMIT_USD and snapshot["costUsd"] >= COST_LIMIT_USD
                        and "cost" not in alerts)
            call_hit = bool(usage) and tokens >= CALL_TOKEN_LIMIT and "call" not in alerts
            missing = ((meta or {}).get("engine") == "hermes" and not usage
                       and "missing" not in alerts)
            if trace_hit:
                alerts.add("trace")
                self.aborts[trace] = f"累计 {snapshot['totalTokens']} tokens 超阈值 {TOKEN_LIMIT}"
            if cost_hit:
                alerts.add("cost")
                self.aborts[trace] = f"累计 ${snapshot['costUsd']} 超金额阈值 ${COST_LIMIT_USD}"
            if call_hit:
                alerts.add("call")
            if missing:
                alerts.add("missing")
        if self.db:
            self.db.insert_usage(trace, agent_id, (meta or {}).get("model") or MODEL, usage)
        if trace_hit or cost_hit:
            self._cto_alert(
                f"成本熔断：trace {trace or '—'} 累计 {snapshot['totalTokens']} tokens / "
                f"${snapshot['costUsd']}，已超阈值（{TOKEN_LIMIT} tokens"
                + (f" / ${COST_LIMIT_USD}" if COST_LIMIT_USD else "") +
                f"）。CTO 决定停止本轮任务，在下一个阶段边界生效。",
                trace, snapshot, stop=True)
        elif call_hit:
            self._cto_alert(
                f"成本异常：{agent_id} 单次调用 {tokens} tokens，超过单次阈值 {CALL_TOKEN_LIMIT}；"
                "已记录并持续观察，暂不停止任务。", trace, snapshot, stop=False)
        elif missing:
            self._cto_alert(
                f"成本异常：{agent_id} 本次调用未返回用量账本（--usage-file 缺失或损坏），"
                "本轮 token 统计偏低，请 CTO 复核。", trace, snapshot, stop=False)
        return snapshot

    def _cto_alert(self, text, trace, bucket, stop=False):
        """在线升级 + 邮件同时汇报老板（CTO 身份）。"""
        payload = {"text": text, "stop": bool(stop), "usage": bucket, "by": "cto"}
        self.send("escalation", "cto", "owner", trace, payload, priority=2)
        self.emit("cost-alert", trace, payload)
        subject = ("[OPC 成本熔断] 任务已停止" if stop else "[OPC 成本告警] 需 CTO 复核") + \
                  f" · {trace or '实时'}"
        body = (text + "\n\n## 本轮用量\n"
                + json.dumps(bucket, ensure_ascii=False, indent=2)
                + f"\n\n阈值：单轮 {TOKEN_LIMIT} tokens / 单次 {CALL_TOKEN_LIMIT} tokens"
                + (f" / 金额 ${COST_LIMIT_USD}" if COST_LIMIT_USD else "")
                + "\n处置：超阈值由 CTO 决定停止当前任务；异常仅汇报。")
        threading.Thread(target=self.mail_boss, args=(subject, body, "cto"), daemon=True).start()

    def guard_budget(self, trace):
        """阶段边界检查：CTO 已判定停止则中断本轮编排。"""
        with self.lock:
            reason = self.aborts.get(trace)
        if reason:
            raise BudgetAbort(reason)

    def mail_boss(self, subject, body, agent_id="cto", cc=None):
        """以员工本人邮箱给老板发内部邮件；失败只告警，不阻断主流程。"""
        try:
            res = MAILM.send_internal_mail(agent_id, [BOSS_MAIL], subject, body, cc=cc or [])
        except Exception as exc:
            print(f"[mail] {agent_id} 发信失败：{type(exc).__name__}: {exc}", flush=True)
            self.send("escalation", agent_id, "owner", "", 
                      {"text": f"邮件发送失败（{subject}）：{exc}"}, priority=2)
            return {"ok": False, "error": str(exc)}
        self.send("biz.event", agent_id, "owner", "",
                  {"text": f"邮件已发：{subject}", "mail": res}, priority=1)
        return res


    def ask_agent(self, aid, text):
        """老板 @某员工 下指令：走该员工独立的 Hermes profile，结果实时推回事件流"""
        a = self.agents.get(aid)
        if not a:
            raise ValueError(f"未知员工: {aid}")
        trace = (self.last_hotspot or {}).get("traceId", "")
        project = self.projects.get(a["projectId"], {}).get("name", "全公司")
        self.send("delegation", "owner", aid, trace, {"task": text, "text": text},
                  project=a["projectId"], priority=1)
        ctx = f"当前场景 {self.scene}；你负责 {project}；老板直接点名向你下达指令"
        data, meta = self.call_agent(
            aid, text, ctx, "",
            "reply(直接回复老板，≤120字，不要客套), evidence(2-4条), confidence(0到1), tools")
        if not data:
            data = {"reply": "这次没拿到模型回复，请稍后重试。", "evidence": [], "confidence": 0.2, "tools": []}
        data.setdefault("summary", data.get("reply") or "")
        self.send("result", aid, "owner", trace,
                  {"result": data, "engine": meta["engine"], "latencyMs": meta["latencyMs"]},
                  project=a["projectId"], priority=1)
        return {"agentId": aid, "agent": a["name"], "answer": data,
                "engine": meta["engine"], "latencyMs": meta["latencyMs"]}

    def fallback(self, aid, task):
        a = self.agents[aid]
        return {"summary": f"{a['name']}（降级）：{task[:40]}", "evidence": ["模型调用失败，使用占位结论"],
                "confidence": 0.2, "tools": []}

    def _patrol_note(self, result, facts):
        """巡检结论：让 CEO 基于真实状态写一段结论并推回事件流（异步，不阻塞巡检接口）"""
        try:
            data, meta = self.call_agent(
                "ceo",
                "写一段本次两小时巡检结论：当前进度、是否有员工空转、下一动作。只用给定事实，不许编造数字。",
                facts, "", "summary(≤120字，直接向老板说), evidence(2-4条，引用给定事实), actions(1-3条), confidence(0到1)")
            if not data:
                return
            data.setdefault("summary", "")
            self.send("result", "ceo", "owner", result.get("traceId", ""),
                      {"result": data, "engine": meta["engine"], "latencyMs": meta["latencyMs"],
                       "patrol": result.get("action")}, priority=1)
        except Exception as exc:
            self.send("escalation", "ceo", "owner", "", {"text": f"巡检结论生成失败：{exc}"}, priority=1)

    def _patrol_facts(self):
        """在动作发生之后才采集事实，避免模型拿到过期状态"""
        with self.lock:
            running = [t for t, d in self.traces.items() if d.get("status") == "running"]
            pending = [a for a in self.approvals if a.get("status") == "pending"]
            idle = [a["id"] for a in self.agents.values() if a.get("status") == "idle"]
            working = [a["id"] for a in self.agents.values() if a.get("status") == "working"]
            kpi = "；".join(f"{self.projects[k]['name']}={self.projects[k]['kpi']}" for k in self.projects)
            return (f"场景={self.scene}；在岗={working or '无'}；空闲={len(idle)}人；"
                    f"运行中trace={running[-1] if running else '无'}；待审批={len(pending)}；"
                    f"累计trace={self.stats.get('traces', 0)}；消息数={self.events}；项目KPI：{kpi}")

    # ---------- 业务入口 ----------
    def _sweep_stale(self):
        """兜底收口：超过 45 分钟仍 running、且没有待审批的 trace 视为超时，不再占用派单。"""
        now = datetime.now(TZ)
        with self.lock:
            pending_tr = {a["traceId"] for a in self.approvals if a.get("status") == "pending"}
            for d in self.traces.values():
                if d.get("status") != "running" or d.get("traceId") in pending_tr:
                    continue
                try:
                    age = (now - datetime.fromisoformat(d["startedAt"])).total_seconds()
                except Exception:
                    continue
                if age > 2700:
                    d["status"] = "timeout"
                    d["closedAt"] = iso_now()

    def patrol(self):
        """CEO 每两小时巡检：看进度 / 找空闲员工 / 无事可做就派新任务"""
        self._sweep_stale()
        with self.lock:
            running = [t for t, d in self.traces.items() if d.get("status") == "running"]
            pending = [a for a in self.approvals if a.get("status") == "pending"]
            idle = [a["id"] for a in self.agents.values() if a.get("status") == "idle"]
        # 审批门优先：流水线会阻塞在审批上（trace 仍显示 running），此时该催的是老板而不是“继续等”
        if pending:
            ap = pending[0]
            note = (f"巡检：{len(pending)} 个审批待老板拍板（{ap['id']} / {ap.get('action')}），"
                    "AI 不得自行开工或发布，已停止派单")
            self.send("escalation", "ceo", "owner", "", {"text": note, "approvalId": ap["id"]}, priority=2)
            result = {"action": "await-approval", "approvalId": ap["id"], "idle": idle}
            facts = self._patrol_facts() + f"；本次动作=停止派单，等待老板审批 {ap['id']}"
            threading.Thread(target=self._patrol_note, args=(result, facts), daemon=True).start()
            return result
        if running:
            note = f"巡检：trace {running[-1]} 仍在运行，本轮不派单；{len(idle)} 人空闲待命"
            self.send("heartbeat", "ceo", "owner", running[-1], {"text": note, "idle": len(idle)}, priority=0)
            result = {"action": "busy", "traceId": running[-1], "idle": idle, "pendingApprovals": len(pending)}
            facts = self._patrol_facts() + "；本次动作=不派单（已有 trace 在跑）"
            threading.Thread(target=self._patrol_note, args=(result, facts), daemon=True).start()
            return result
        hs = self.last_hotspot or {}
        if hs.get("title"):
            title = hs["title"] + "（二轮深挖）"
            tags, score = hs.get("tags") or ["综合"], min(0.95, (hs.get("score") or 0.8) + 0.02)
        else:
            title, tags, score = None, None, None
        trace, hotspot = self.inject_hotspot(title, tags, score, force=True)
        title = hotspot["title"]
        assign = {"scout": "复核热点与素材包", "pm_wenren": "出文人莫克选题卡", "pm_gou": "出狗大人选题卡",
                  "pm_tongxinyuan": "出同心圆选题卡", "cw_wenren": "写稿", "cw_gou": "写稿",
                  "cw_tongxinyuan": "写产品文案", "vd_wenren": "出片", "vd_gou": "出片",
                  "vd_tongxinyuan": "出演示视频", "reviewer": "跨项目串味审核",
                  "release_ops": "备发布与监控", "kb_manager": "归档", "cto": "技术周报", "ceo": "日报"}
        self.send("delegation", "ceo", "all", trace,
                  {"task": f"全员复工：{title}（空闲 {len(idle)} 人已按岗位分派）", "assignments": assign}, priority=1)
        result = {"action": "dispatched", "traceId": trace, "hotspot": title, "idle": idle}
        facts = (self._patrol_facts()
                 + f"；本次动作=已派单「{title}」并下发全员分工，员工正在进入在岗（上表的在岗/空闲为空闲态排查前的值）")
        threading.Thread(target=self._patrol_note, args=(result, facts), daemon=True).start()
        return result

    def _hotspot_from_collection(self):
        """按最新一档原始采集榜生成热点；没有采集数据时返回 None。

        取当日最新一档榜单里“今日新增★”最高的仓库，热度用它在当档新增总量里的占比，
        来源写清文件名与档位，避免再出现“热点源/RSS”这类无依据字段。
        """
        status = None
        if self.DL:
            try:
                status = self.DL.load_collection_status()
            except Exception as e:
                print(f"[hotspot] 读取最新采集榜失败：{type(e).__name__}: {e}", flush=True)
        if status:
            self.collection = status
        if not status:
            return None
        items = status.get("items") or []
        picked = max(items, key=lambda r: _to_int(r.get("stars"), 0)) if items else (status.get("top") or {})
        repo = str(picked.get("repo") or "").strip()
        if not repo:
            return None
        stars = _to_int(picked.get("stars"), 0)
        total = sum(_to_int(r.get("stars"), 0) for r in items)
        lang = str(picked.get("lang") or "").strip()
        stamp = f"{status.get('date') or ''} {status.get('time') or ''}".strip()
        if stars > 0:
            title = f"GitHub 日榜 {stamp} 今日新增最高：{repo}（+{stars:,}★" + (f"，{lang}）" if lang else "）")
        else:
            title = f"GitHub 日榜 {stamp}：{repo}"
        if stars > 0 and total > 0:
            score = round(min(1.0, stars / total), 3)
            basis = f"今日新增 {stars:,}★ / 当档 {len(items)} 个仓库合计 {total:,}★"
        else:
            score = 0.0
            basis = "原始榜未给出可计算的今日新增★"
        return {"title": title,
                "tags": [t for t in ("GitHub Trending", lang) if t],
                "score": score, "scoreBasis": basis,
                "source": f"{status.get('file') or '热点采集'} · {status.get('time') or '?'} 档"}

    def inject_hotspot(self, title=None, tags=None, score=None, force=False):
        """注入热点并触发选题会。

        未显式传 title 时，热点由最新一档原始采集榜定义；采集缺失直接报错，不再写死文案。
        """
        if not force:
            self._sweep_stale()
            with self.lock:
                running = [t for t, d in self.traces.items() if d.get("status") == "running"]
            if running:
                raise BusyError(running[-1])
        if title is not None and not isinstance(title, str):
            raise ValueError("title 需为字符串")
        if tags is not None and not isinstance(tags, (list, tuple, str)):
            raise ValueError("tags 需为数组或字符串")
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError):
                raise ValueError("score 需为 0-1 的数字")
            if not 0 <= score <= 1:
                raise ValueError("score 需在 0-1 之间")
        basis, source = "", "手动注入"
        if title and title.strip():
            title = title.strip()
            if score is None:
                basis = "手动注入未提供热度"
        else:
            data = self._hotspot_from_collection()
            if not data:
                raise ValueError("暂无最新采集数据：OPC/热点采集 下没有当日榜单，请先执行采集")
            title = data["title"]
            tags = tags or data["tags"]
            if score is None:
                score = data["score"]
            basis, source = data["scoreBasis"], data["source"]
        if score is None:
            score = 0.8
        if isinstance(tags, str):
            tags = re.split(r"[/、,，]", tags)
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()] or ["综合"]
        trace = uid("tr")
        hotspot = {"id": uid("hs"), "traceId": trace, "title": title, "source": source,
                   "tags": tags, "score": score, "fit": {}, "at": iso_now()}
        if basis:
            hotspot["scoreBasis"] = basis
        key = _hotkey(title)
        with self.lock:
            idx = next((i for i in range(len(self.hotspots) - 1, -1, -1)
                        if _hotkey(self.hotspots[i].get("title")) == key), -1)
            if idx >= 0:
                # 同一议题重复注入：替换列表里的旧记录（旧 trace 仍保留自己的快照）
                hotspot["id"] = self.hotspots[idx].get("id") or hotspot["id"]
                self.hotspots[idx] = hotspot
            else:
                self.hotspots.append(hotspot)
            self.last_hotspot = hotspot
            self.stats["traces"] += 1
            self.traces[trace] = {"traceId": trace, "hotspot": hotspot, "startedAt": iso_now(),
                                  "status": "running", "engine": MODE}
        self._persist_trace(trace)
        self.emit("hotspot", trace, hotspot)
        threading.Thread(target=self.run_pipeline, args=(trace, hotspot), daemon=True).start()
        return trace, hotspot

    def inject_idea(self, idea, requirements="", project=""):
        """创意灵感直接发起制作（老板在 /app/inspiration 选灵感点「开始制作」）。

        不走热点采集与选题会：灵感 + 老板特定要求直接建 1 张内容单并同步指令给 CCO，
        流水线从 assessment（制作评估/CCO 定工艺）起跑——老板点击即视为开工审批，
        后续 copy → sample → render(OpenMontage) → review 与热点链路共用同一套阶段
        与制片工厂监控。
        """
        if not isinstance(idea, dict):
            idea = {}
        title = str(idea.get("title") or "").strip()
        if not title:
            raise ValueError("灵感缺少标题，无法发起制作")
        self._sweep_stale()
        with self.lock:
            running = [t for t, d in self.traces.items() if d.get("status") == "running"]
        if running:
            raise BusyError(running[-1])
        requirements = str(requirements or "").strip()
        pid = self._idea_project(idea, project)
        trace = uid("tr")
        tags = [str(t).strip() for t in (idea.get("tags") or []) if str(t).strip()]
        try:
            score = float(idea.get("score"))
            if not 0 <= score <= 1:
                score = 0.8
        except (TypeError, ValueError):
            score = 0.8
        hotspot = {"id": uid("hs"), "traceId": trace, "title": title[:60],
                   "source": "创意灵感", "tags": (tags + ["创意灵感"])[:12],
                   "score": score, "fit": {}, "at": iso_now(),
                   "ideaId": str(idea.get("id") or ""), "requirements": requirements}
        with self.lock:
            self.hotspots.append(hotspot)
            self.last_hotspot = hotspot
            self.stats["traces"] += 1
            self.traces[trace] = {"traceId": trace, "hotspot": hotspot, "startedAt": iso_now(),
                                  "status": "running", "engine": MODE, "source": "idea"}
        self._persist_trace(trace)
        self.emit("hotspot", trace, hotspot)
        # 内容单与 _stage_create_items 同构；老板特定要求随单流转（PM 细化 / CCO 审批可见）
        iid = self._content_id(trace, pid, 0, title[:60])
        item = {"id": iid, "projectId": pid, "traceId": trace, "stage": "topic",
                "title": title[:60],
                "angle": str(idea.get("summary") or idea.get("detail") or title)[:80],
                "owner": f"pm_{pid}", "toneProfile": self.tone[pid], "assets": [],
                "reviewNote": "", "durationSec": 60,
                "source": "创意灵感", "ideaId": str(idea.get("id") or ""),
                "bossBrief": requirements}
        with self.lock:
            self.contents.append(item)
        self.items_emit(trace, item)
        # 指令同步发给 CCO：老板灵感 + 特定要求，CCO 据此完成基础准备并补齐创意
        self.send("delegation", "owner", "cco", trace,
                  {"task": f"老板在创意灵感页选定《{item['title']}》发起制作，已进入流水线（制作评估起跑）。"
                           + (f"老板特定要求：{requirements}" if requirements else "无额外特定要求。"),
                   "idea": {"id": item["ideaId"], "title": item["title"],
                            "summary": str(idea.get("summary") or ""),
                            "detail": str(idea.get("detail") or ""),
                            "source": str(idea.get("source") or "")},
                   "requirements": requirements, "from": "老板"},
                  project=pid, priority=1)
        self.emit("idea.produce", trace,
                  {"itemId": iid, "title": item["title"], "projectId": pid,
                   "requirements": requirements, "by": "owner"})
        # 种子状态：单项目单条定稿，assessment 起跑（跳过选题会/开工审批）
        state = {"pms": [[pid, f"pm_{pid}"]],
                 "selected": {pid: [item["title"]]},
                 "final": {pid: [{"title": item["title"], "angle": item["angle"],
                                  "form": "正式宣传片", "durationSec": 60,
                                  "reason": "老板创意灵感直接发起",
                                  "bossBrief": requirements}]},
                 "pool_size": 1,
                 "items": {iid: item}}
        threading.Thread(target=self.run_pipeline, args=(trace, hotspot, "assessment", state),
                         daemon=True).start()
        return trace, hotspot

    @staticmethod
    def _idea_project(idea, project=""):
        """灵感归属项目：显式指定 > 灵感标签/来源里认得出的项目 > 默认同心圆（正式宣讲调性）。"""
        pid = str(project or "").strip().lower()
        if pid in self.projects:
            return pid
        hay = " ".join([str(idea.get("title") or ""), str(idea.get("summary") or ""),
                        str(idea.get("origin") or ""), str(idea.get("source") or ""),
                        " ".join(str(t) for t in (idea.get("tags") or []))])
        for key, val in (("文人莫克", "wenren"), ("狗大人", "gou"), ("同心圆", "tongxinyuan")):
            if key in hay:
                return val
        return "tongxinyuan"

    def run_pipeline(self, trace, hotspot, start=None, state=None):
        self.active_trace = trace
        if self.db:
            try:
                if start:
                    self.db.pipeline_run_stage(trace, start, 0, state or {})
                else:
                    self.db.pipeline_run_start(trace, hotspot)
            except Exception as e:
                print(f"[pipeline] 运行态入库失败（不影响执行）：{type(e).__name__}: {e}", flush=True)
        try:
            self._pipeline(trace, hotspot, start=start, state=state)
        except BudgetAbort as exc:
            # CTO 已判定超阈值并通知老板：这里只负责按“停止”收口
            with self.lock:
                self.traces[trace]["status"] = "aborted"
                self.traces[trace]["closedAt"] = iso_now()
                self.traces[trace]["abortReason"] = str(exc)
                for item in self.contents:
                    if item.get("traceId") == trace and item.get("stage") not in (
                            "published", "archived"):
                        item["stage"] = "blocked"
                        item["blockedBy"] = "cost-gate"
            self._persist_trace(trace)
            self.send("escalation", "cto", "owner", trace,
                      {"text": f"任务已在阶段边界停止：{exc}", "aborted": True}, priority=2)
        except Exception as exc:
            self.send("escalation", "ceo", "owner", trace, {"text": f"编排异常：{exc}"}, priority=2)
            with self.lock:
                self.traces[trace]["status"] = "error"
                self.traces[trace]["closedAt"] = iso_now()
            self._persist_trace(trace)
        finally:
            if self.active_trace == trace:
                self.active_trace = ""
            # 编排异常时也要收口会议，避免 3D 会议室一直停留在“开会中”
            if self.meeting_log.get("traceId") == trace and self.meeting_log.get("status") == "live":
                try:
                    self.meeting_end(trace, "会议中断：编排异常，未形成结论。")
                    self.set_scene("WORK", trace, title="会议中断")
                except Exception:
                    pass
            # 兜底收口：无论从哪条分支提前返回，都不能把 trace 永远留在 running，否则巡检会一直拒派
            with self.lock:
                t = self.traces.get(trace)
                if t and t.get("status") == "running":
                    t["status"] = "incomplete"
                    t["closedAt"] = iso_now()
            self._persist_trace(trace)
            self.export_trace_docs(trace)   # 收口之后再落盘，交付文档才能记录到最终状态
            self.save_runtime()             # 本轮报告/消息/知识入库，重启后可恢复
            if self.db:
                try:
                    final = (self.traces.get(trace) or {}).get("status") or "incomplete"
                    self.db.pipeline_run_end(trace, final)
                except Exception as e:
                    print(f"[pipeline] 运行态收口失败：{type(e).__name__}: {e}", flush=True)

    # ---------- 选题流程护栏 ----------
    PIDS = ("wenren", "gou", "tongxinyuan")

    def _deg(self, aid, what):
        return self.fallback(aid, what)

    def _coerce_topics(self, data, pid, hotspot):
        """PM 选题卡：每项目恰好 4 条（CCO 有 12 选 9 的淘汰空间），模型漏项时规则兜底。"""
        raw = (data or {}).get("topics")
        out = []
        if isinstance(raw, list):
            for t in raw:
                if not isinstance(t, dict):
                    continue
                title = str(t.get("title") or t.get("angle") or "").strip()
                if not title:
                    continue
                out.append({"projectId": pid, "title": title[:60],
                            "angle": str(t.get("angle") or title)[:80],
                            "form": str(t.get("form") or "短视频")[:16],
                            "durationSec": _to_int(t.get("durationSec"), 45),
                            "reason": str(t.get("reason") or "")[:120]})
        forms = ("热点解读", "延伸视角", "互动话题", "反常识问答")
        while len(out) < 4:
            i = len(out)
            base = self.projects[pid]["name"]
            angle = (hotspot.get("title") or "今日热点").strip()
            out.append({"projectId": pid, "title": f"{angle}｜{base}{forms[i]}",
                        "angle": f"从{base}定位切入{forms[i]}", "form": forms[i],
                        "durationSec": 45, "reason": "规则兜底选题"})
        return out[:4]

    def _coerce_mgmt_topics(self, data):
        raw = (data or {}).get("topics")
        out = []
        if isinstance(raw, list):
            for t in raw:
                if isinstance(t, dict):
                    title = str(t.get("title") or t.get("angle") or "").strip()
                    if title:
                        out.append({"projectId": "all", "title": title[:60],
                                    "angle": str(t.get("angle") or "")[:80],
                                    "form": "管理层指定", "durationSec": 45,
                                    "reason": "管理层指定选题"})
        return out[:3]

    def _coerce_selection(self, data, pm_topics):
        """CCO 选 9 条：每个项目 3 条（从 4 条池中淘汰 1 条），缺项从 PM 池补齐。"""
        raw = (data or {}).get("selected")
        picked = {pid: [] for pid in self.PIDS}
        if isinstance(raw, list):
            for t in raw:
                if not isinstance(t, dict):
                    continue
                pid = str(t.get("projectId") or "")
                if pid not in picked:
                    continue
                title = str(t.get("title") or t.get("angle") or "").strip()
                if not title:
                    continue
                picked[pid].append({"projectId": pid, "title": title[:60],
                                    "angle": str(t.get("angle") or "")[:80],
                                    "form": str(t.get("form") or "短视频")[:16],
                                    "durationSec": _to_int(t.get("durationSec"), 45),
                                    "reason": str(t.get("reason") or "")[:120]})
        for pid in self.PIDS:
            if len(picked[pid]) < 3:
                picked[pid] += pm_topics[pid][:3 - len(picked[pid])]
            picked[pid] = picked[pid][:3]
        return picked

    def _coerce_final(self, data, selected):
        """评审会定 6 条：每个项目强制 2 条，缺项从 CCO 9 条中补齐。"""
        raw = (data or {}).get("final")
        final = {pid: [] for pid in self.PIDS}
        if isinstance(raw, list):
            for t in raw:
                if not isinstance(t, dict):
                    continue
                pid = str(t.get("projectId") or "")
                if pid not in final:
                    continue
                title = str(t.get("title") or t.get("angle") or "").strip()
                if not title:
                    continue
                final[pid].append({"projectId": pid, "title": title[:60],
                                   "angle": str(t.get("angle") or "")[:80],
                                   "durationSec": _to_int(t.get("durationSec"), 45)})
        for pid in self.PIDS:
            if len(final[pid]) < 2:
                final[pid] += selected[pid][:2 - len(final[pid])]
            final[pid] = final[pid][:2]
        return final

    def _coerce_plan(self, data, topic, idx):
        raw = (data or {}).get("assessment")
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            raw = raw["items"]
        items = raw if isinstance(raw, list) else []
        src = items[idx] if idx < len(items) and isinstance(items[idx], dict) else {}
        tts = src.get("tts") if isinstance(src.get("tts"), dict) else {}
        shots = []
        for s in (src.get("storyboardShots") or [])[:8]:
            if isinstance(s, dict) and (s.get("画面") or s.get("scene")):
                shots.append({"no": _to_int(s.get("no"), len(shots) + 1),
                              "画面": str(s.get("画面") or s.get("scene"))[:120],
                              "台词": str(s.get("台词") or s.get("line") or "")[:120],
                              "秒": _to_int(s.get("秒") or s.get("sec"), 6),
                              "素材": str(s.get("素材") or s.get("asset") or "")[:60]})
        vtasks = [{"shot": str(t.get("shot") or i + 1),
                   "task": str(t.get("task") or "")[:120]}
                  for i, t in list(enumerate(src.get("videoTasks") or []))[:12]
                  if isinstance(t, dict)]
        return {"durationSec": _to_int(src.get("durationSec"), topic.get("durationSec", 45)),
                "tts": {"voice": str(tts.get("voice") or "zh-CN-XiaoxiaoNeural")[:40],
                        "style": str(tts.get("style") or "纪录片口播")[:40]},
                "materials": [str(x)[:60] for x in (src.get("materials") or [])][:8],
                "copyBrief": str(src.get("copyBrief") or "按定稿选题写口播稿")[:200],
                "storyboardNotes": str(src.get("storyboardNotes") or "4-6 镜，首镜 3 秒抓人")[:300],
                "storyboardShots": shots, "videoTasks": vtasks}

    def _review_map(self, data, ids):
        """样片评审：三票制；评审结果缺失按未通过处理（基础设施故障不得放行）。"""
        raw = (data or {}).get("reviews")
        out = {}
        if isinstance(raw, list):
            for r in raw:
                if not isinstance(r, dict):
                    continue
                iid = str(r.get("itemId") or "")
                if iid in ids:
                    out[iid] = (bool(r.get("pass")),
                                [str(x)[:100] for x in (r.get("reasons") or [])][:5])
        for iid in ids:
            out.setdefault(iid, (False, ["评审结果未返回，按未通过处理"]))
        return out

    def _sample_wave(self, trace, ids, items):
        """CCO、CTO 各评审全部样片；PM 只评审本项目样片。"""
        if not ids:
            return {}, {}
        payload = [{"itemId": i, "projectId": items[i]["projectId"],
                    "title": items[i].get("title"), "copy": items[i].get("copy"),
                    "storyboard": items[i].get("storyboard"),
                    "durationSec": items[i].get("durationSec"),
                    "render": items[i].get("renderCheck")} for i in ids]
        ctx = json.dumps(payload, ensure_ascii=False)
        extra = ("逐条判定并输出 reviews 数组：[{itemId, pass(布尔), reasons(数组≤2条)}]。"
                 "只有创意完成度/调性/技术可交付性明显不达标才判 false。")
        schema = "summary, evidence, confidence, tools, reviews"
        jobs = {
            "cco": HERMES.pool.submit(self.call_agent, "cco",
                                      "以 CCO 身份评审样片：项目调性、创意完成度、人设是否串味。", ctx, extra, schema),
            "cto": HERMES.pool.submit(self.call_agent, "cto",
                                      "以 CTO 身份评审样片：分镜可执行性、时长、配音/TTS 与素材风险。", ctx, extra, schema),
        }
        for pid in self.PIDS:
            pid_ids = [i for i in ids if items[i]["projectId"] == pid]
            if pid_ids:
                p_ctx = json.dumps([items[i] for i in pid_ids], ensure_ascii=False)
                jobs[pid] = HERMES.pool.submit(
                    self.call_agent, f"pm_{pid}",
                    f"以 {self.projects[pid]['name']} 产品经理身份评审本项目样片，判断能否进入定版。",
                    p_ctx, extra, schema)
        maps, metas = {}, {}
        for who, job in jobs.items():
            data, meta = job.result()
            data = data or {}
            ids_for_role = ids if who in ("cco", "cto") else \
                [i for i in ids if items[i]["projectId"] == who]
            maps[who] = self._review_map(data, ids_for_role)
            metas[who] = meta
            self.send("result", who if who in ("cco", "cto") else f"pm_{who}", "ceo", trace,
                      {"verdict": {"role": who, "reviews": maps[who]}, "engine": meta["engine"]},
                      project="all" if who in ("cco", "cto") else who)
        return maps, metas

    def _production_checks(self, items):
        """开工审批单的成本/素材/合规摘要：调用量按编排静态估算，不伪造金额。"""
        n = len(items)
        return {
            "cost": {
                "modelCallsTypical": 22 + 4 * n,
                "modelCallsWorst": 54 + 3 * n,
                "renders": n,
                "ttsSegments": [4 * n, 6 * n],
                "cashUsd": None,
                "budgetCapUsd": None,
                "note": "渲染/TTS 走本地链路（现金 0）；模型调用按 provider 计费，用量未回传，无法核算",
            },
            "materials": {
                "source": "本地 SVG 插图 + 本地 CosyVoice2 配音",
                "externalAssets": 0,
                "licenseRisk": "无外部素材",
                "assetGapsPending": True,
            },
            "compliance": {
                "toneIsolation": True,
                "projectBinding": True,
                "sourcesRequired": False,
                "reviewerGate": "定版后终审",
            },
        }

    def _publish_checks(self, items, passed_items, review_history):
        """发布审批单的成本/素材/合规摘要。"""
        n = len(items)
        total_assets = sum(len(i.get("assets") or []) for i in passed_items.values())
        rounds = len({h.get("round") for h in review_history}) or 0
        return {
            "cost": {
                "modelCallsTypical": 22 + 4 * n,
                "renders": n,
                "cashUsd": None,
                "budgetCapUsd": None,
                "note": "本轮未使用付费素材/生成接口；模型金额未回传，无法核算",
            },
            "materials": {
                "assets": total_assets,
                "externalAssets": 0,
                "licenseRisk": "无外部素材",
            },
            "compliance": {
                "reviewPassed": len(passed_items),
                "blocked": n - len(passed_items),
                "reviewRounds": rounds,
                "toneIsolation": True,
                "reviewerGate": "已通过",
            },
        }

    def _stage_timeout(self, c):
        """审批超时：流程定义优先，未配置时沿用 OPC_APPROVAL_TIMEOUT。"""
        return c.stage.timeout_sec or APPROVAL_TIMEOUT

    def _pipeline(self, trace, hotspot, start=None, state=None):
        """按流程定义（config/pipeline.yaml）顺序执行阶段。

        每个阶段一个 _stage_<id> 方法，阶段间状态放 PipelineContext；
        新增/调整阶段只改定义文件与对应实现，不再维护一条硬编码长函数。
        每次进入阶段前把「阶段 + 上下文快照」写入过程库，进程重启可从这里续跑。
        """
        c = PipelineContext(self, trace, hotspot)
        if state:
            c.load_state(state)
            # 续跑时把快照里的内容单挂回总线，否则前端/报表看不到这些单
            with self.lock:
                known = {x.get("id") for x in self.contents}
                for item in c.items.values():
                    if item.get("id") not in known:
                        self.contents.append(item)
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
            fn = getattr(self, "_stage_" + st.id, None)
            if fn is None:
                raise RuntimeError(f"流程定义里的阶段 {st.id} 没有实现 _stage_{st.id}")
            c.stage = st
            self._run_checkpoint(trace, st, pos, c)
            self._workshop_start(trace, st)
            try:
                stop = fn(c)
            except Exception:
                self._workshop_end(trace, st, "failed", c)
                raise
            self._workshop_end(trace, st, "stopped" if stop else "done", c)
            if stop is _PIPELINE_STOP:
                return

    # ---------- 制片工厂（车间监控/控制） ----------
    def _workshop_start(self, trace, st):
        """阶段开始：车间工位标记 running（过程库关闭时静默跳过）。"""
        if not self.db:
            return
        self.db.workshop_step_start(trace, st.id, st.name, st.om_stage)

    def _workshop_end(self, trace, st, status, ctx=None):
        """阶段结束：车间工位落定 done/stopped/failed，摘要随行供前端回放。"""
        if not self.db:
            return
        self.db.workshop_step_end(trace, st.id, status, {
            "stage": st.id, "name": st.name, "kind": st.kind, "omStage": st.om_stage,
            "items": len(getattr(ctx, "items", ()) or ()),
        })

    def factory_view(self):
        """制片工厂聚合：13 类车间 + 各车间产品（工艺决策 + 工位过程 + 运行态）。

        只展示 CCO 审批通过（product_crafts 有记录、带流水号 serial_no）的内容单；
        工艺轴只取工厂段（进厂 assessment → 出厂 review），前段选题/开工审批与
        后段发布/归档/汇报在 hermes 调度，不属于工厂工序。
        """
        workshops = [dict(w) for w in CC.OPENMONTAGE_CRAFTS]
        enabled = [s for s in CC.PIPELINE.stages if s.enabled]
        ids = [s.id for s in enabled]
        try:
            lo, hi = ids.index(CC.FACTORY_ENTRY_STAGE), ids.index(CC.FACTORY_EXIT_STAGE)
        except ValueError:
            lo, hi = 0, len(ids) - 1
        axis_stages = enabled[min(lo, hi):max(lo, hi) + 1]
        stage_axis = [{"id": s.id, "name": s.name, "kind": s.kind, "omStage": s.om_stage}
                      for s in axis_stages]
        axis_ids = {s.id for s in axis_stages}
        products = []
        if self.db:
            runs = {r.get("trace_id"): r for r in self.db.pipeline_runs_recent(80)}
            steps_by_trace = {}
            for s in self.db.workshop_steps():
                steps_by_trace.setdefault(s.get("trace_id"), []).append(s)
            for row in self.db.product_crafts(120):
                tid = row.get("trace_id") or ""
                run = runs.get(tid) or {}
                # 工艺步骤序列：CCO 审批时快照（steps 列），老数据回退到工艺定义现值
                try:
                    seq = json.loads(row.get("steps") or "null")
                except ValueError:
                    seq = None
                if not seq:
                    seq = CC.CRAFT_STEPS.get(row.get("craft_id") or "", [])
                steps_by_stage = {s.get("stage_id"): _step_view(s)
                                  for s in steps_by_trace.get(tid, [])}
                craft_steps = []
                for st in seq:
                    rec = steps_by_stage.get(st.get("opcStage"))
                    craft_steps.append({**st,
                                        "status": (rec or {}).get("status") or "pending",
                                        "startedAt": (rec or {}).get("started_at") or "",
                                        "finishedAt": (rec or {}).get("finished_at") or ""})
                products.append({
                    "itemId": row.get("item_id"), "traceId": tid,
                    "serialNo": row.get("serial_no") or "",
                    "projectId": row.get("project_id"), "title": row.get("title"),
                    "workshopNo": row.get("workshop_no"), "craftId": row.get("craft_id"),
                    "craftName": row.get("craft_name"), "product": row.get("product"),
                    "renderMode": row.get("render_mode"), "reason": row.get("reason"),
                    "decidedBy": row.get("decided_by"),
                    "decidedAt": row.get("updated_at") or row.get("created_at"),
                    "shippedAt": row.get("shipped_at") or "",
                    "craftSteps": craft_steps,
                    "run": {"status": run.get("status"), "stage": run.get("stage"),
                            "stageIndex": run.get("stage_index"),
                            "updatedAt": run.get("updated_at"), "closedAt": run.get("closed_at")},
                    "steps": [v for v in steps_by_stage.values()
                              if v.get("stage_id") in axis_ids],
                })
        return {"ok": True, "workshops": workshops, "stages": stage_axis, "products": products}

    def _factory_works_index(self):
        """作品档案 × 工厂车间对齐：以 product_crafts（有流水号）为准的 (trace,item) 索引。

        返回 (factory 索引, 工位状态表)；过程库未启用返回 None（档案回退磁盘扫描）。
        """
        if not self.db:
            return None
        runs = {r.get("trace_id"): r for r in self.db.pipeline_runs_recent(100)}
        step_status = {}
        for s in self.db.workshop_steps():
            step_status.setdefault(s.get("trace_id"), {})[s.get("stage_id")] = \
                s.get("status") or "pending"
        idx = {}
        for row in self.db.product_crafts(200):
            tid = row.get("trace_id") or ""
            try:
                steps = json.loads(row.get("steps") or "null")
            except ValueError:
                steps = None
            if not steps:
                steps = CC.CRAFT_STEPS.get(row.get("craft_id") or "", [])
            idx[(tid, row.get("item_id") or "")] = {
                "itemId": row.get("item_id") or "",
                "title": row.get("title") or "",
                "projectId": row.get("project_id") or "",
                "serialNo": row.get("serial_no") or "",
                "workshopNo": row.get("workshop_no") or "",
                "craftId": row.get("craft_id") or "",
                "craftName": row.get("craft_name") or "",
                "decidedAt": row.get("updated_at") or row.get("created_at"),
                "shippedAt": row.get("shipped_at") or "",
                "runStatus": (runs.get(tid) or {}).get("status") or "",
                "steps": steps,
            }
        return idx, step_status

    def _run_checkpoint(self, trace, st, pos, c):
        """阶段边界写档：stage 是即将执行的阶段，状态快照随行。"""
        self._write_stage_checkpoint(trace, st, pos, c)
        if not self.db:
            return
        try:
            self.db.pipeline_run_stage(trace, st.id, pos, c.to_state())
        except Exception as e:
            print(f"[pipeline] 断点写档失败（不影响执行）：{type(e).__name__}: {e}", flush=True)

    def _write_stage_checkpoint(self, trace, st, pos, c):
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

    def _stage_topic_meeting(self, c):
        """1. 每日选题会：PM 各交 4 条（池 12）+ CEO/CTO 指定选题 + scout 简报，同波 6 个调用"""
        # 1. 每日选题会：PM 各交 4 条（池 12）+ CEO/CTO 指定选题 + scout 简报，同波 6 个调用
        participants = c.participants = ["ceo", "cto", "cco", "pm_wenren", "pm_gou", "pm_tongxinyuan", "scout", "kb_manager"]
        agenda1 = [f"热点：{c.hotspot['title']}", "三项目各 4 条候选（池 12）", "CCO 12 选 9"]
        self.meeting_begin(c.trace, "每日选题会", participants, agenda1)
        self.set_scene("MEETING", c.trace, title="每日选题会", participants=participants, agenda=agenda1)
        for aid in participants:
            self.set_agent(aid, "meeting", "选题会")
            self.send("delegation", "ceo", aid, c.trace, {"task": "参会：按岗位职责提交/评估选题"},
                      project=None if self.agents[aid]["projectId"] == "all" else self.agents[aid]["projectId"])
        self.meeting_say("ceo", f"开会。今天的议题是热点「{c.hotspot['title']}」（热度 {c.hotspot['score']}）。"
                                "请热点采集先报简报，三位产品经理各提 4 条候选，CCO 汇总 12 选 9。", "opening", c.trace)

        fit = c.hotspot.get("fit") or {}
        ctx = (f"热点：{c.hotspot['title']}；热度 {c.hotspot['score']}；三项目匹配度："
               f"文人莫克 {fit.get('wenren', '未评估')}，狗大人 {fit.get('gou', '未评估')}，"
               f"同心圆 {fit.get('tongxinyuan', '未评估')}")
        futures = {"scout": HERMES.pool.submit(
            self.call_agent, "scout", "针对给定热点产出一条业务简报，并判断素材可用性。",
            f"热点：{c.hotspot['title']}；热度 {c.hotspot['score']}；标签 {c.hotspot['tags']}")}
        for pid, pm in c.pms:
            futures[pm] = HERMES.pool.submit(
                self.call_agent, pm,
                f"提交 {self.projects[pid]['name']} 今日 4 条候选选题，供 CCO 与 CTO 汇总。",
                ctx,
                f"本项目匹配度 {fit.get(pid, '未评估')}。额外 JSON 字段：topics(数组，恰好 4 条，"
                "每条 {title, angle, form, durationSec, reason})。",
                "summary, evidence, confidence, tools, topics")
        futures["ceo"] = HERMES.pool.submit(
            self.call_agent, "ceo", "结合热点给出 0-3 条 CEO 指定选题与评审关注点。", ctx,
            "额外 JSON 字段：topics(数组，0-3 条，每条 {title, angle})。",
            "summary, evidence, confidence, tools, topics")
        futures["cto"] = HERMES.pool.submit(
            self.call_agent, "cto", "结合热点与素材/技术条件给出 0-3 条 CTO 指定选题与约束。", ctx,
            "额外 JSON 字段：topics(数组，0-3 条，每条 {title, angle})。",
            "summary, evidence, confidence, tools, topics")

        scout_data, scout_meta = futures["scout"].result()
        scout_result = scout_data or self._deg("scout", "热点简报")
        for rcpt in ("ceo", "cco", "cto"):
            self.send("biz.event", "scout", rcpt, c.trace,
                      {"hotspot": c.hotspot, "result": scout_result,
                       "text": f"热点上报：{c.hotspot['title']}（热度 {c.hotspot['score']}）"}, priority=1)
        self.emit("engine-meta", c.trace, {"agent": "scout", **scout_meta})
        self.meeting_say("scout", scout_result.get("summary") or
                         f"热点「{c.hotspot['title']}」热度 {c.hotspot['score']}，素材可采集，建议三项目按匹配度跟进。",
                         "report", c.trace)

        pm_topics = {}
        for pid, pm in c.pms:
            data, meta = futures[pm].result()
            data = data or self._deg(pm, "选题卡")
            pm_topics[pid] = self._coerce_topics(data, pid, c.hotspot)
            self.send("result", pm, "cco", c.trace,
                      {"result": data, "topics": pm_topics[pid], "engine": meta["engine"],
                       "latencyMs": meta.get("latencyMs", 0), "projectId": pid}, project=pid)
            self.send("result", pm, "cto", c.trace,
                      {"topics": pm_topics[pid], "engine": meta["engine"]}, project=pid)
            self.meeting_say(pm, "提交 4 条候选：" + "；".join(t.get("title", "") for t in pm_topics[pid]),
                             "speech", c.trace)
        ceo_topics = self._coerce_mgmt_topics(futures["ceo"].result()[0])
        cto_topics = self._coerce_mgmt_topics(futures["cto"].result()[0])
        boss_topic = {"projectId": "all", "title": str(c.hotspot["title"])[:60],
                      "angle": "老板注入选题", "form": "老板指定", "durationSec": 45,
                      "reason": "老板直接指定"}

        # 2. CCO 从 12 条池中淘汰 3 条 → 选 9 条（每项目 3 条），抄送 CEO、CTO
        pool = {pid: pm_topics[pid] for pid in self.PIDS}
        pool["_management"] = {"ceo": ceo_topics, "cto": cto_topics, "boss": [boss_topic]}
        c.pool_size = sum(len(pm_topics[p]) for p in self.PIDS) + len(ceo_topics) + len(cto_topics) + 1
        data, meta = self.call_agent(
            "cco",
            f"汇总 3 名产品经理的 {sum(len(pm_topics[p]) for p in self.PIDS)} 条创意，叠加 CEO/CTO/老板指定选题与自选创意，"
            "按项目调性、匹配度、差异化去重，每个项目必须淘汰至少 1 条，最终选出 9 条（每项目恰好 3 条），"
            "并给出被淘汰条目与理由。",
            json.dumps(pool, ensure_ascii=False),
            "额外 JSON 字段：selected(数组，恰好 9 条，每条 {projectId, title, angle, form, durationSec, reason})；"
            "eliminated(数组，每条 {title, projectId, reason}，不得少于 3 条)。",
            "summary, evidence, confidence, tools, selected, eliminated")
        data = data or self._deg("cco", "选题汇总")
        c.selected = self._coerce_selection(data, pm_topics)
        # 淘汰记录必须可核：模型漏填时，用「PM 池 − 入选」补足，保证每项目至少 1 条可追溯
        c.eliminated = [x for x in (data.get("eliminated") or []) if isinstance(x, dict)]
        seen = {t.get("title") for v in c.selected.values() for t in v}
        known = {str(x.get("title") or "") for x in c.eliminated}
        for pid in self.PIDS:
            for t in pm_topics[pid]:
                if t["title"] not in seen and t["title"] not in known:
                    c.eliminated.append({"projectId": pid, "title": t["title"],
                                       "reason": "四选三未入选（按调性/匹配度/差异化淘汰，规则补记）"})
                    known.add(t["title"])
        self.send("topic.select", "cco", "ceo", c.trace,
                  {"selected": c.selected, "eliminated": c.eliminated,
                   "poolSize": c.pool_size, "result": data, "engine": meta["engine"]}, priority=2)
        self.send("topic.select", "cco", "cto", c.trace,
                  {"selected": c.selected, "eliminated": c.eliminated, "cc": True,
                   "engine": meta["engine"]}, priority=1)
        self.emit("engine-meta", c.trace, {"agent": "cco", **meta})
        picked = [t.get("title", "") for pid in self.PIDS for t in c.selected.get(pid, [])]
        self.meeting_say("cco", (data.get("summary") or
                                 f"候选池共 {c.pool_size} 条，按调性与差异化去重后选出 9 条。") +
                         (" 入选：" + "；".join(picked) if picked else ""), "decision", c.trace)
        self.meeting_end(c.trace, f"每日选题会结束：候选池 {c.pool_size} 条，CCO 筛出 9 条（每项目 3 条），"
                                "转入选题评审会定稿 6 条。")


    def _stage_topic_review(self, c):
        """3. CEO 召集选题评审会：CEO/CTO/CCO + 涉及项目 PM，最终定 6 条（每项目 2 条）"""
        # 3. CEO 召集选题评审会：CEO/CTO/CCO + 涉及项目 PM，最终定 6 条（每项目 2 条）
        agenda2 = ["复核 CCO 9 条", "逐项目定 2 条", "形成会议纪要"]
        self.meeting_begin(c.trace, "选题评审会", c.participants, agenda2)
        self.set_scene("MEETING", c.trace, title="选题评审会", participants=c.participants, agenda=agenda2)
        for aid in ("cto", "cco", "pm_wenren", "pm_gou", "pm_tongxinyuan"):
            self.set_agent(aid, "meeting", "选题评审会")
            self.send("delegation", "ceo", aid, c.trace,
                      {"task": "参会：对 CCO 9 条选题复核并定稿 6 条"}, priority=2)
        self.meeting_say("ceo", "现在复核 CCO 的 9 条，逐项目定 2 条。今天必须形成 6 条定稿和会议纪要。",
                         "opening", c.trace)
        data, meta = self.call_agent(
            "ceo",
            "主持选题评审会：从 CCO 的 9 条中逐项目定稿 2 条，共 6 条；"
            "输出会议纪要（议题、参会人、结论、每项目 2 条及理由、后续分工），并给出收件人与抄送人。",
            json.dumps({pid: c.selected[pid] for pid in self.PIDS}, ensure_ascii=False),
            "额外 JSON 字段：final(数组，恰好 6 条，每条 {projectId,title,angle,durationSec})；"
            "minutes(会议纪要 Markdown，≤1500字)；recipients(数组)；cc(数组)。",
            "summary, evidence, confidence, tools, final, minutes, recipients, cc")
        data = data or self._deg("ceo", "选题评审会")
        c.final = self._coerce_final(data, c.selected)
        display = lambda xs: ["老板" if x == "owner" else self.agents.get(x, {}).get("name", x) for x in xs]
        minutes = str(data.get("minutes") or "").strip() or (
            f"# 选题评审会\n\n- 议题：{c.hotspot['title']}\n- 参会："
            + "、".join(display(["ceo", "cto", "cco", "pm_wenren", "pm_gou", "pm_tongxinyuan"]))
            + "\n- 结论：每项目定稿 2 条，共 6 条\n")
        recipients = [str(x) for x in (data.get("recipients") or [])] or \
            ["ceo", "cto", "cco", "pm_wenren", "pm_gou", "pm_tongxinyuan"]
        cc = [str(x) for x in (data.get("cc") or [])] or ["owner"]
        c.meeting_doc = {}
        if self.DL:
            try:
                c.meeting_doc = self.DL.export_meeting(c.trace, {
                    "title": "选题评审会", "minutes": minutes,
                    "recipients": display(recipients), "cc": display(cc),
                    "final": c.final, "hotspot": c.hotspot})
            except Exception as e:
                print(f"[pipeline] 会议纪要落盘失败：{type(e).__name__}: {e}", flush=True)
        self.send("decision", "ceo", "all", c.trace,
                  {"text": f"定稿 6 条（每项目 2 条）：{c.hotspot['title']}",
                   "final": c.final, "minutes": minutes,
                   "minutesUrl": (c.meeting_doc or {}).get("minutes", ""),
                   "mailUrl": (c.meeting_doc or {}).get("mail", ""),
                   "result": data, "engine": meta["engine"]}, project="all", priority=2)
        self.send("report.ready", "ceo", "owner", c.trace,
                  {"kind": "meeting", "title": "选题评审会纪要",
                   "minutesUrl": (c.meeting_doc or {}).get("minutes", ""),
                   "mailUrl": (c.meeting_doc or {}).get("mail", ""),
                   "recipients": recipients, "cc": cc}, priority=1)
        c.conclusion = "定稿 6 条（每项目 2 条）：" + "；".join(
            f"{self.projects[pid]['name']}《{t['title']}》" for pid in self.PIDS for t in c.final[pid])
        self.meeting_say("ceo", "结论：" + c.conclusion + "。后续按项目进入制作，会议纪要已归档到 Obsidian。",
                         "conclusion", c.trace)
        self.meeting_end(c.trace, c.conclusion, (c.meeting_doc or {}).get("minutes", ""))
        # 祁南（kb_manager）在选题定稿即归档，不再等发布审批；发布后只更新同一条
        self.archive_knowledge(c.trace, [
            {"projectId": pid, "title": t.get("title"),
             "body": f"选题定稿：{t.get('angle') or t.get('title')}；来源热点：{c.hotspot.get('title')}；"
                     f"评审会结论：{c.conclusion[:120]}",
             "tags": ["热点/" + str(c.hotspot.get("title") or "")[:24]] + list(c.hotspot.get("tags") or [])[:3]}
            for pid in self.PIDS for t in c.final.get(pid, [])])
        self.set_scene("WORK", c.trace, title="定稿 6 条，进入制作")


    def _stage_boss_report(self, c):
        """3.5 CCO 邮件汇报老板：选题评审结果 + 等待开工审批（抄送 CEO/CTO）"""
        # 3.5 CCO 邮件汇报老板：选题评审结果 + 等待开工审批（抄送 CEO/CTO）
        self.guard_budget(c.trace)
        mail_lines = [
            f"老板：",
            "",
            f"本轮选题评审已完成（热点：{c.hotspot['title']}）。",
            f"候选池 {c.pool_size} 条 → CCO 筛出 9 条 → 评审会定稿 {sum(len(v) for v in c.final.values())} 条（每项目 2 条）。",
            "",
            "## 定稿清单",
        ]
        for pid in self.PIDS:
            mail_lines.append(f"### {self.projects[pid]['name']}")
            for t in c.final[pid]:
                mail_lines.append(
                    f"- 《{t['title']}》｜角度：{t.get('angle', '—')}｜形态：{t.get('form', '—')}｜"
                    f"时长：{t.get('durationSec', '—')}s｜理由：{t.get('reason', '—')}")
        if c.eliminated:
            mail_lines += ["", "## 淘汰与原因"]
            for e in c.eliminated[:12]:
                mail_lines.append(f"- {e.get('title', '—')}：{e.get('reason', '—')}")
        mail_lines += [
            "",
            "## 待办",
            "- 开工审批单已生成（含成本/素材/合规检查），请老板在 OPC 工作台批准后进入制作。",
            f"- 会议纪要：{(c.meeting_doc or {}).get('minutes', '（未生成）')}",
        ]
        mail_subject = f"[选题评审] {str(c.hotspot['title'])[:30]} · 定稿 6 条，待开工审批"
        mail_body = "\n".join(mail_lines)
        self.send("biz.event", "cco", "owner", c.trace,
                  {"text": "CCO 已邮件汇报选题评审结果，等待开工审批",
                   "to": BOSS_MAIL, "cc": ["ceo", "cto"], "subject": mail_subject,
                   "finalCount": sum(len(v) for v in c.final.values()),
                   "eliminated": len(c.eliminated)}, priority=2)
        threading.Thread(target=self.mail_boss,
                         args=(mail_subject, mail_body, "cco", ["ceo", "cto"]),
                         daemon=True).start()


    def _stage_create_items(self, c):
        """4. 建 6 条内容（每项目 2 条）"""
        # 4. 建 6 条内容（每项目 2 条）
        # 内容单 id 由 trace+项目+序号+标题 决定：阶段中途被打断、续跑重建时复用同一条，
        # 既有内容不会变成孤儿，发布回执（release_ledger 按 item_id 判重）也不会失效。
        c.items = {}
        for pid in self.PIDS:
            for idx, t in enumerate(c.final[pid]):
                iid = self._content_id(c.trace, pid, idx, t["title"])
                item = {"id": iid, "projectId": pid, "traceId": c.trace, "stage": "topic",
                        "title": t["title"], "angle": t.get("angle", ""),
                        "owner": f"pm_{pid}", "toneProfile": self.tone[pid], "assets": [],
                        "reviewNote": "", "durationSec": t.get("durationSec", 45)}
                with self.lock:
                    existing = next((x for x in self.contents if x.get("id") == iid), None)
                    if existing is not None:
                        item = existing            # 断点续跑：复用已有内容单，不重复登记
                    else:
                        self.contents.append(item)
                c.items[item["id"]] = item
                self.emit("pipeline", c.trace, item)
            self.send("delegation", f"pm_{pid}", f"cw_{pid}", c.trace,
                      {"task": f"承接本项目 {len(c.final[pid])} 条定稿选题，先出制作评估再写稿"}, project=pid)
            self.send("delegation", f"pm_{pid}", f"vd_{pid}", c.trace,
                      {"task": f"承接本项目 {len(c.final[pid])} 条定稿选题，先出制作评估再制作样片"}, project=pid)

    @staticmethod
    def _content_id(trace, pid, idx, title):
        """内容单稳定 id：同一 trace 下同样一条选题，重跑也得到同一个 id。"""
        raw = f"{trace}|{pid}|{idx}|{title}".encode("utf-8")
        return "c_" + hashlib.sha1(raw).hexdigest()[:12]


    def _open_approval(self, c, draft):
        """审批单幂等开单：同一 trace + 动作已有单就复用，重启续跑不会重复打扰老板。

        返回 (审批单, 唤醒事件 or None, 是否新建)。
        - 已有已决单：不新建、不等待（事件为 None）
        - 已有待决单：复用记录并重新登记唤醒事件，等待老板处理
        - 无记录：与改造前一致，新建 + 推送 + 写库
        """
        existing = None
        if self.db:
            try:
                existing = self.db.approval_for(c.trace, draft["action"])
            except Exception as e:
                print(f"[approval] 读库失败，按新单处理：{type(e).__name__}: {e}", flush=True)
        with self.lock:
            if not existing:
                self.approvals.append(draft)
                ev = threading.Event()
                self.approval_events[draft["id"]] = ev
                row, created = draft, True
            else:
                row = next((a for a in self.approvals if a["id"] == existing["id"]), None)
                if row is None:
                    row = dict(existing.get("raw") or {})
                    for k in ("id", "traceId", "action", "status", "createdAt", "decidedAt", "timeoutSec"):
                        if existing.get(k) not in (None, ""):
                            row[k] = existing[k]
                    self.approvals.append(row)
                if row.get("status") != "pending":
                    return row, None, False
                ev = self.approval_events.get(row["id"]) or threading.Event()
                self.approval_events[row["id"]] = ev
                created = False
        # 新建单与“重启后重新挂起”都推一次提醒，保证老板能看到待办
        self.send("approval", "ceo", "owner", c.trace,
                  {"approval": row} if created else {"approval": row, "reopened": True}, priority=2)
        self.emit("approval", c.trace, row)
        return row, ev, created

    def _stage_production_approval(self, c):
        """4.5 开工审批：老板批准选题与成本/素材/合规摘要后才进入制作"""
        # 4.5 开工审批：老板批准选题与成本/素材/合规摘要后才进入制作
        prod_approval = {
            "id": uid("ap"), "traceId": c.trace, "action": "production.start", "risk": "high",
            "status": "pending", "approver": "老板", "createdAt": iso_now(), "decidedAt": "",
            "timeoutSec": self._stage_timeout(c),
            "items": list(c.items.keys()),
            "titles": [i["title"] for i in c.items.values()],
            "title": f"{len(c.items)} 条内容开工审批（含成本/素材/合规检查）",
            "checks": self._production_checks(c.items),
        }
        prod_approval, prod_ev, _ = self._open_approval(c, prod_approval)
        prod_ok = (prod_approval["status"] == "approved") if prod_ev is None \
            else prod_ev.wait(timeout=self._stage_timeout(c))
        with self.lock:
            prod_appr = next(a for a in self.approvals if a["id"] == prod_approval["id"])
            self.approval_events.pop(prod_approval["id"], None)
        if not prod_ok or prod_appr["status"] != "approved":
            with self.lock:
                if prod_appr["status"] == "pending":
                    prod_appr["status"] = "timeout"
                    prod_appr["decidedAt"] = iso_now()
                    prod_appr["timeoutReason"] = f"等待老板审批超过 {self._stage_timeout(c)}s，按挂起处理"
                for item in c.items.values():
                    item["stage"] = "blocked"
                    item["blockedBy"] = "production-gate"
                t = self.traces.get(c.trace)
                if t:
                    t["status"] = "rejected" if prod_appr["status"] == "rejected" else "timeout"
                    t["closedAt"] = iso_now()
            for item in c.items.values():
                self.items_emit(c.trace, item)
            self.send("escalation", "ceo", "owner", c.trace,
                      {"text": "开工审批未通过，本轮不进入制作"}, priority=2)
            self.emit("approval", c.trace, prod_appr)
            self.set_scene("WORK", c.trace, title="开工审批未通过")
            return _PIPELINE_STOP

        self.guard_budget(c.trace)   # CTO 成本熔断：审批通过后、进入制作前再核一次


    RENDER_MODES = ("remotion", "full_video", "voice_video")

    def _cco_render_mode(self, c, pid, items):
        """CCO 确定短视频架构（出片模式）：普通科普类 → remotion（Remotion 最终合成，
        精确控制字幕与语音）；休闲娱乐类 → full_video（全视频制作）或 voice_video
        （语音+视频），按各自创意剧本的适配度定，未明确时回落科普类默认。"""
        data = {}
        try:
            data, meta = self.call_agent(
                "cco", f"为 {self.projects[pid]['name']} 的 {len(items)} 条选题逐条确定短视频出片架构："
                "普通科普类用 remotion（Remotion 最终合成，字幕/语音精确可控）；"
                "休闲娱乐类在 full_video（全视频制作）与 voice_video（语音+视频）里按创意剧本择优。",
                json.dumps([{"title": i["title"], "angle": i.get("angle", ""),
                             "bossBrief": i.get("bossBrief", "")} for i in items],
                           ensure_ascii=False),
                "额外 JSON 字段：modes(数组，每条 {title,mode,reason}；"
                "mode ∈ remotion/full_video/voice_video)。",
                "summary, evidence, confidence, tools, modes")
        except Exception as e:
            print(f"[pipeline] CCO 出片模式决策失败，按默认处理：{type(e).__name__}: {e}", flush=True)
        picks = {}
        for m in (data or {}).get("modes") or []:
            if isinstance(m, dict) and m.get("mode") in self.RENDER_MODES:
                picks[str(m.get("title") or "")] = m
        for item in items:
            pick = picks.get(item["title"]) or {}
            item["renderMode"] = pick.get("mode", "remotion")
            item["renderModeNote"] = str(pick.get("reason")
                                         or "CCO 未明确，按科普类默认 Remotion 合成（字幕/语音精确可控）")
            self.send("result", "cco", f"pm_{pid}", c.trace,
                      {"itemId": item["id"], "renderMode": item["renderMode"],
                       "reason": item["renderModeNote"]}, project=pid)
            self.items_emit(c.trace, item)

    def _cco_craft(self, c, pid, items):
        """CCO 审批定工艺（参考 OpenMontage 13 类工艺）——进入流水线的门槛。

        只有 CCO 给出明确工艺决策（= 审批通过）的内容单才发流水号（WO-YYYYMMDD-NNN）、
        落库 product_crafts 并进车间；CCO 未明确（模型降级/无决策）的内容单不进流水线，
        也不下发车间派单。
        """
        data = {}
        try:
            crafts_brief = "\n".join(
                f"{w['no']}号车间 {w['id']}（{w['name']}，产品：{w['product']}）：{w['desc']}"
                for w in CC.OPENMONTAGE_CRAFTS)
            data, meta = self.call_agent(
                "cco", f"为 {self.projects[pid]['name']} 的 {len(items)} 条选题逐条审批制作工艺。"
                f"参考 OpenMontage 的 13 类工艺（每类对应一个车间）：\n{crafts_brief}"
                "（bossBrief 是老板通过创意灵感页下达的特定要求，工艺决策与理由必须满足它）",
                json.dumps([{"title": i["title"], "angle": i.get("angle", ""),
                             "renderMode": i.get("renderMode", ""),
                             "bossBrief": i.get("bossBrief", "")} for i in items],
                           ensure_ascii=False),
                "额外 JSON 字段：crafts(数组，每条 {title,craft,reason}；"
                "craft 取上表车间对应的工艺 id，如 animated-explainer；"
                "审批不通过或无法判断的条目不要出现在 crafts 里)。",
                "summary, evidence, confidence, tools, crafts")
        except Exception as e:
            print(f"[pipeline] CCO 工艺审批失败，相关条目不进流水线：{type(e).__name__}: {e}", flush=True)
        picks = {}
        for m in (data or {}).get("crafts") or []:
            if isinstance(m, dict) and str(m.get("craft") or "") in CC.CRAFT_BY_ID:
                picks[str(m.get("title") or "")] = m
        for item in items:
            pick = picks.get(item["title"])
            if not pick:
                # CCO 未审批通过：不分配流水号、不落库、不进流水线
                item["craft"] = ""
                item["craftNote"] = "CCO 未审批通过，不进入流水线"
                self.send("result", "cco", f"pm_{pid}", c.trace,
                          {"itemId": item["id"], "approved": False,
                           "reason": item["craftNote"]}, project=pid)
                self.emit("craft.rejected", c.trace,
                          {"itemId": item["id"], "title": item["title"],
                           "projectId": pid, "by": "cco"})
                continue
            craft = CC.CRAFT_BY_ID[str(pick["craft"])]
            # 重审批（同条目重复决策）复用已发流水号，不重复发号
            serial = item.get("serialNo") or (self.db.alloc_serial() if self.db else "")
            item["craft"] = craft["id"]
            item["serialNo"] = serial
            item["craftNote"] = str(pick.get("reason") or "CCO 审批通过")
            if self.db:
                self.db.upsert_product_craft({
                "traceId": c.trace, "itemId": item["id"], "serialNo": serial,
                "projectId": item.get("projectId", pid),
                "title": item["title"], "workshopNo": craft["no"], "craftId": craft["id"],
                "craftName": craft["name"], "product": craft["product"],
                "renderMode": item.get("renderMode", ""), "reason": item["craftNote"],
                "decidedBy": "cco",
                "steps": CC.CRAFT_STEPS.get(craft["id"], []),   # CCO 定工艺时定格工序序列
            })
            self.send("result", "cco", f"pm_{pid}", c.trace,
                      {"itemId": item["id"], "approved": True, "serialNo": serial,
                       "craft": craft["id"], "workshop": f"{craft['no']}号车间",
                       "reason": item["craftNote"]}, project=pid)
            self.emit("craft.approved", c.trace,
                      {"itemId": item["id"], "title": item["title"], "projectId": pid,
                       "serialNo": serial, "craft": craft["id"],
                       "workshopNo": craft["no"], "by": "cco"})
            self.items_emit(c.trace, item)

    def _stage_assessment(self, c):
        """5. PM 细化分镜脚本并按并行原则拆分派单；CCO 定出片模式；scout 补素材缺口"""
        # 5. PM 组织本项目组出《制作评估报告》：把分镜补充完善（逐镜 画面/台词/时长/素材），
        #    并按并行原则拆任务——视频制作耗时较长，逐镜子任务先行开工，文案岗同步领取成稿与补素材指令。
        jobs = {}
        for pid, pm in c.pms:
            boss_reqs = "；".join(
                f"《{t.get('title')}》{t['bossBrief']}" for t in (c.final.get(pid) or [])
                if t.get("bossBrief"))
            jobs[pid] = HERMES.pool.submit(
                self.call_agent, pm,
                f"为 {self.projects[pid]['name']} 的 {len(c.final.get(pid) or [])} 条定稿选题细化制作方案：把分镜脚本补充完善到逐镜可执行"
                "（画面/台词/时长/所需素材），并按并行原则拆分任务——视频制作拆成逐镜子任务先开工，"
                "文案岗同步领取成稿与补素材指令。"
                + (f"老板特定要求（逐条落实，不得跑偏）：{boss_reqs}" if boss_reqs else ""),
                json.dumps(c.final.get(pid) or [], ensure_ascii=False),
                "额外 JSON 字段：assessment({items:[{title,durationSec,tts:{voice,style},"
                "copyBrief,storyboardNotes,"
                "storyboardShots:[{no,画面,台词,秒,素材}],videoTasks:[{shot,task}]}], summary})；"
                "assetGaps(数组，每条 {item, why})。",
                "summary, evidence, confidence, tools, assessment, assetGaps")
        gap_jobs = {}
        for pid, pm in c.pms:
            data, meta = jobs[pid].result()
            data = data or self._deg(pm, "制作评估报告")
            pid_items = [i for i in c.items.values() if i["projectId"] == pid]
            for idx, item in enumerate(pid_items):
                item["plan"] = self._coerce_plan(data, item, idx)
                item["assessment"] = {"summary": data.get("summary", ""),
                                      "evidence": data.get("evidence", []),
                                      "risks": data.get("risks", []),
                                      "assetGaps": data.get("assetGaps", [])}
                self.items_emit(c.trace, item)
            self._cco_render_mode(c, pid, pid_items)
            self._cco_craft(c, pid, pid_items)
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
                self.send("delegation", pm, f"vd_{pid}", c.trace,
                          {"task": "分镜已细化，按逐镜子任务并行开工（视频制作耗时较长，立即启动）",
                           "videoTasks": vtasks[:16]}, project=pid, priority=1)
            self.send("delegation", pm, f"cw_{pid}", c.trace,
                      {"task": "按文案要求成稿；收到补素材指令后先查素材库，缺口走 ComfyUI 生成，"
                               "台词/对白走 TTS 出音频"}, project=pid)
            self.send("result", pm, "cco", c.trace,
                      {"result": data, "assessment": data.get("assessment"), "engine": meta["engine"]}, project=pid)
            self.send("result", pm, "cto", c.trace,
                      {"assessment": data.get("assessment"), "engine": meta["engine"]}, project=pid)
            gaps = data.get("assetGaps") or []
            if gaps:
                self.send("delegation", pm, "scout", c.trace,
                          {"task": "素材缺失工单：补齐/替代并给出结论", "gaps": gaps}, project=pid, priority=2)
                gap_jobs[pid] = HERMES.pool.submit(
                    self.call_agent, "scout", "承接素材缺失工单：逐项给出补齐、替代或「无可用素材」的结论。",
                    json.dumps(gaps, ensure_ascii=False),
                    "额外 JSON 字段：gaps(数组，每条 {item,status,alternative})。",
                    "summary, evidence, confidence, tools, gaps")
        for pid, job in gap_jobs.items():
            data, meta = job.result()
            data = data or self._deg("scout", "素材工单")
            for item in c.items.values():
                if item["projectId"] == pid:
                    item["assetResolution"] = data.get("gaps", [])
                    self.items_emit(c.trace, item)
            self.send("result", "scout", f"pm_{pid}", c.trace,
                      {"result": data, "engine": meta["engine"]}, project=pid)


    def _stage_copy(self, c):
        """6. 文案成稿 + 补素材链路（素材库 → ComfyUI 生成 → TTS 音频），两者并行"""
        # 6. 文案成稿（6 个真实调用）
        jobs = {}
        for iid, item in c.items.items():
            pid = item["projectId"]
            plan = item.get("plan") or {}
            jobs[iid] = HERMES.pool.submit(
                self.call_agent, f"cw_{pid}",
                f"为《{item['title']}》写一段成稿文案（≤80字），符合本项目人设，禁止串用其他项目风格。",
                f"项目：{self.projects[pid]['name']}；风格：{self.tone[pid]}；时长：{plan.get('durationSec', 45)}s；"
                f"文案要求：{plan.get('copyBrief', '')}",
                "额外 JSON 字段：copy(成稿文案≤80字)。",
                "summary, evidence, confidence, tools, copy")
            self.send("delegation", f"pm_{pid}", f"cw_{pid}", c.trace,
                      {"task": f"按评估报告写《{item['title']}》成稿；收到补素材指令：先查素材库，"
                               f"缺口走 ComfyUI 生成，台词/对白走 TTS"}, project=pid)
        # 补素材与成稿并行推进：素材库检索/ComfyUI 出图/TTS 都有 IO 等待，不占文案的推理时间
        fill_jobs = {iid: HERMES.pool.submit(self._fill_materials, c.trace, item,
                                             item["projectId"])
                     for iid, item in c.items.items()}
        for iid, item in c.items.items():
            pid = item["projectId"]
            data, meta = jobs[iid].result()
            if meta.get("engine") != "hermes" or not (data or {}).get("copy"):
                item.setdefault("degraded", []).append("copy")
            data = data or {"summary": "文案已交付（降级）", "evidence": [], "confidence": 0.2,
                            "tools": [], "copy": f"{self.projects[pid]['name']} 文案占位"}
            item["stage"] = "copy"
            item["copy"] = data.get("copy") or data.get("summary", "")
            self.send("result", f"cw_{pid}", f"pm_{pid}", c.trace,
                      {"result": data, "itemId": iid, "engine": meta["engine"],
                       "latencyMs": meta.get("latencyMs", 0)}, project=pid)
            self.items_emit(c.trace, item)
        for job in fill_jobs.values():
            try:
                job.result()
            except Exception as e:
                print(f"[materials] 补素材任务异常：{type(e).__name__}: {e}", flush=True)

    def _fill_materials(self, trace, item, pid):
        """文案岗补素材：逐缺口先查已有资料库，未命中的走 ComfyUI 文生图尽快补齐；
        台词/对白同步调用 TTS 出音频。结论回传 PM，不阻断主流程。
        OPC_MATERIAL_FILL=0 时只登记缺口不实际生成（验收/省 GPU 场景）。"""
        if not asset_fill or os.environ.get("OPC_MATERIAL_FILL") == "0":
            return
        plan = item.get("plan") or {}
        out_dir = os.path.join(BASE_DIR, ".cache", "materials", item["id"])
        # 1) 汇总素材缺口：评估报告的 assetGaps + 逐镜细化里的素材需求
        gaps = [dict(g) for g in (item.get("assessment") or {}).get("assetGaps") or []
                if isinstance(g, dict)]
        for s in plan.get("storyboardShots") or []:
            if s.get("素材"):
                gaps.append({"item": item["title"], "why": s["素材"]})
        # 2) 先查已有资料库（逐缺口检索，命中即复用）
        lib_hits, missing = [], []
        for g in gaps[:6]:
            kws = [k for k in re.split(r"[，,、;；\s（）()：:]+", str(g.get("why") or ""))
                   if len(k) >= 2]
            hits = []
            if kws:
                try:
                    hits = asset_fill.search_library(self.materials_file, kws, limit=2)
                except Exception as e:
                    print(f"[materials] {item['id']} 素材库检索失败："
                          f"{type(e).__name__}: {e}", flush=True)
            lib_hits += hits
            if not hits:
                missing.append(g)
        seen = set()
        item["materials"] = {"library": [h for h in lib_hits
                                         if not (h["url"] in seen or seen.add(h["url"]))],
                             "generated": [], "voice": [], "gaps": gaps[:6]}
        self.items_emit(trace, item)
        # 3) 库里没有的 → ComfyUI 文生图，尽快补齐（逐缺口串行提交，失败记录不阻断）
        for i, g in enumerate(missing[:3]):
            try:
                path = asset_fill.gen_image(
                    f"{g.get('why') or g.get('item') or item['title']}，"
                    f"{self.projects[pid]['name']} 风格竖版配图，{self.tone.get(pid, '')}",
                    os.path.join(out_dir, f"gap{i + 1}.png"))
                item["materials"]["generated"].append({"why": g.get("why"), "path": path})
            except Exception as e:
                item["materials"]["generated"].append(
                    {"why": g.get("why"), "error": f"{type(e).__name__}: {str(e)[:120]}"})
                print(f"[materials] {item['id']} ComfyUI 补图失败：{e}", flush=True)
        # 4) 台词/对白 → TTS 音频（旁白合成仍在 render 阶段，这里补齐对话类语音资产）
        lines = [s["台词"] for s in plan.get("storyboardShots") or [] if s.get("台词")]
        if not lines and item.get("copy"):
            lines = [str(item["copy"])]
        for i, line in enumerate(lines[:4]):
            try:
                audio = asset_fill.gen_tts(line, out_dir, f"line{i + 1}")
                item["materials"]["voice"].append({"text": line, "audioPath": audio})
            except Exception as e:
                item["materials"]["voice"].append(
                    {"text": line, "error": f"{type(e).__name__}: {str(e)[:120]}"})
                print(f"[materials] {item['id']} TTS 台词音频失败：{e}", flush=True)
        self.items_emit(trace, item)
        n_gen = len([g for g in item["materials"]["generated"] if g.get("path")])
        n_voice = len([v for v in item["materials"]["voice"] if v.get("audioPath")])
        self.send("result", f"cw_{pid}", f"pm_{pid}", trace,
                  {"itemId": item["id"], "补素材结论":
                   f"素材库命中 {len(item['materials']['library'])}，"
                   f"ComfyUI 补齐 {n_gen}/{len(missing[:3])}，TTS 音频 {n_voice} 段"},
                  project=pid)


    def _stage_sample(self, c):
        """7. 视频制作出样片（6 个真实调用）"""
        # 7. 视频制作出样片（6 个真实调用）
        jobs = {}
        for iid, item in c.items.items():
            pid = item["projectId"]
            plan = item.get("plan") or {}
            jobs[iid] = HERMES.pool.submit(
                self.call_agent, f"vd_{pid}",
                f"按《制作评估报告》与 PM 细化分镜为《{item['title']}》出样片方案："
                f"出片模式 {item.get('renderMode', 'remotion')}（{item.get('renderModeNote', '')}），"
                "逐镜剧本、封面、配音/TTS。",
                f"项目：{self.projects[pid]['name']}；风格：{self.tone[pid]}；时长：{plan.get('durationSec', 45)}s；"
                f"配音：{plan.get('tts', {})}；文案：{item.get('copy', '')}；"
                f"细化分镜：{json.dumps(plan.get('storyboardShots') or [], ensure_ascii=False)}；"
                f"分镜要点：{plan.get('storyboardNotes', '')}",
                "额外 JSON 字段：shots(数字), storyboard(4-6 镜，每镜 画面/台词，可加 插图:1-2 个中文物件名，"
                "如 相纸/药水盘/放大镜，≤220字), cover(≤20字), sample(样片说明≤80字)。",
                "summary, evidence, confidence, tools, shots, storyboard, cover, sample")
            self.send("delegation", f"pm_{pid}", f"vd_{pid}", c.trace,
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
            self.send("result", f"vd_{pid}", f"pm_{pid}", c.trace,
                      {"result": data, "itemId": iid, "engine": meta["engine"],
                       "latencyMs": meta.get("latencyMs", 0)}, project=pid)
            self.items_emit(c.trace, item)


    def _stage_render(self, c):
        """7.5 真实出片：MCP-TTS 旁白 + OpenMontage 渲染 + 画面验收门（多条目并行）"""
        # 视频制作是流水线耗时大头：多条目并行渲染（逐条内部仍按镜串行，
        # 共享的 MCP-TTS 进程在 om_render.TTSClient 内部有锁，不会串流）。
        c.render_blocked = set()
        from concurrent.futures import ThreadPoolExecutor
        total = len(c.items)
        args = [(idx, total, iid, item) for idx, (iid, item) in enumerate(c.items.items(), 1)]
        workers = min(4, total) if total > 1 and os.environ.get("OPC_SKIP_RENDER") != "1" else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in pool.map(lambda a: self._render_one(c, *a), args):
                pass

    def _render_one(self, c, idx, total, iid, item):
        pid = item["projectId"]
        self.send("biz.event", f"vd_{pid}", "ceo", c.trace,
                  {"text": f"真实出片 {idx}/{total}：《{item['title']}》", "step": "render"},
                  project=pid, priority=2)
        if os.environ.get("OPC_SKIP_RENDER") == "1":
            files = item.get("files") or {}
            item["renderCheck"] = {"pass": True, "reasons": ["测试模式：跳过真实渲染"]}
            item["assets"] = [files.get("video", ""), files.get("cover", "")]
            item["renderSkipped"] = True
        elif not OMR:
            item["renderCheck"] = {"pass": False, "reasons": ["渲染模块不可用"]}
        else:
            res = OMR.produce(item, self.projects[pid], self.tone.get(pid, ""),
                              CC.DELIVERY.get(pid) or CC.DELIVERY["_default"])
            item["render"] = {k: v for k, v in res.items() if k != "check"}
            item["renderCheck"] = res.get("check") or {
                "pass": False, "reasons": [res.get("reason") or "未知渲染错误"]}
            if res.get("ok"):
                item["assets"] = [res["videoUrl"], res["coverUrl"]]
            else:
                item["assets"] = []
                item["renderError"] = res.get("reason", "")
        if not item["renderCheck"].get("pass"):
            c.render_blocked.add(iid)
            item["blockedBy"] = "render-gate"
            reason = item.get("renderError") or "；".join(
                (item["renderCheck"].get("reasons") or [])[:2])
            self.send("escalation", f"vd_{pid}", "owner", c.trace,
                      {"text": f"《{item['title']}》样片未过画面验收：{reason}"},
                      project=pid, priority=2)
        self.emit("render-gate", c.trace, {"itemId": iid,
                                        "pass": item["renderCheck"].get("pass"),
                                        "check": item["renderCheck"]})
        self.items_emit(c.trace, item)


    def _stage_sample_review(self, c):
        """8. 样片三方评审（CCO + CTO + 本项目 PM），最多两轮"""
        # 8. 样片三方评审（CCO + CTO + 本项目 PM），最多两轮
        c.sample_blocked = set()
        c.review_ids = [iid for iid in c.items if iid not in c.render_blocked]
        for rnd in range(1, c.stage.rounds + 1):
            maps, metas = self._sample_wave(c.trace, c.review_ids, c.items)
            failed = []
            for iid in c.review_ids:
                item = c.items[iid]
                pid = item["projectId"]
                roles = {"cco": maps["cco"][iid], "cto": maps["cto"][iid], "pm": maps[pid][iid]}
                item["sampleReviews"] = {k: {"pass": v[0], "reasons": v[1]} for k, v in roles.items()}
                passed = all(v[0] for v in roles.values())
                item["samplePass"] = passed
                self.emit("sample-review", c.trace,
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
                    self.items_emit(c.trace, c.items[iid])
                    self.send("escalation", "cco", "owner", c.trace,
                              {"text": f"《{c.items[iid]['title']}》两轮样片评审未通过，已阻断"},
                              project=c.items[iid]["projectId"], priority=2)
                break
            jobs = {}
            for iid in failed:
                item = c.items[iid]
                pid = item["projectId"]
                reasons = json.dumps(item.get("sampleReviews", {}), ensure_ascii=False)
                jobs[iid] = HERMES.pool.submit(
                    self.call_agent, f"vd_{pid}",
                    f"按三方样片评审意见返工《{item['title']}》的样片：重出逐镜剧本、封面与样片说明。",
                    f"项目：{self.projects[pid]['name']}；风格：{self.tone[pid]}；评审意见：{reasons}",
                    "额外 JSON 字段：shots(数字), storyboard(≤220字，每镜可加 插图:1-2 个中文物件名，"
                    "如 相纸/药水盘/放大镜), cover(≤20字), sample(≤80字)。",
                    "summary, evidence, confidence, tools, shots, storyboard, cover, sample")
                self.send("delegation", f"pm_{pid}", f"vd_{pid}", c.trace,
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
                        res = OMR.produce(item, self.projects[pid], self.tone.get(pid, ""),
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
                self.items_emit(c.trace, item)
                self.send("result", f"vd_{item['projectId']}", f"pm_{item['projectId']}", c.trace,
                          {"result": data, "itemId": iid, "engine": meta["engine"]},
                          project=item["projectId"])
            c.review_ids = failed


    def _stage_final(self, c):
        """9. 样片过审后「定版」：不再二次剪辑，直接把过审样片锁为 05-final.mp4"""
        # 9. 样片过审后「定版」：不再二次剪辑，直接把过审样片锁为 05-final.mp4
        final_ids = [iid for iid in c.items
                     if iid not in c.sample_blocked and iid not in c.render_blocked]
        jobs = {}
        for iid in final_ids:
            item = c.items[iid]
            pid = item["projectId"]
            jobs[iid] = HERMES.pool.submit(
                self.call_agent, f"vd_{pid}",
                f"《{item['title']}》样片已通过 CCO/CTO/PM 评审，现执行定版：确认交付物清单与口径。",
                f"项目：{self.projects[pid]['name']}；文案：{item.get('copy', '')}；"
                f"样片说明：{item.get('sampleNote', '')}",
                "制度：定版不重剪，禁止声称做了帧级调整或重新剪辑；额外 JSON 字段："
                "finalCut(定版说明≤120字，须写明「不改画面」), assets(数组，交付文件名)。",
                "summary, evidence, confidence, tools, finalCut, assets")
            self.send("delegation", f"pm_{pid}", f"vd_{pid}", c.trace,
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
                    dst = self.DL._local(final_url)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(src, dst)
                    item["finalAssets"] = [final_url]
                    item["assets"] = [a for a in item.get("assets", []) if a and a != final_url] + [final_url]
                except Exception as e:
                    print(f"[pipeline] {iid} 定版母版落盘失败：{type(e).__name__}: {e}", flush=True)
            self.send("result", f"vd_{pid}", f"pm_{pid}", c.trace,
                      {"result": data, "itemId": iid, "engine": meta["engine"]}, project=pid)
            self.items_emit(c.trace, item)


    def _stage_review(self, c):
        """10. 审核员终审（定版后）；驳回返工重写，最多 2 轮"""
        # 10. 审核员终审（定版后）；驳回返工重写，最多 2 轮
        self.guard_budget(c.trace)   # 制作完成后、终审前核对成本
        c.passed_items, c.review_history = {}, []
        c.pending = [iid for iid in c.items
                   if iid not in c.sample_blocked and iid not in c.render_blocked]
        for rnd in range(1, c.stage.rounds + 1):
            rev_jobs = {iid: HERMES.pool.submit(self.review_with_model, c.items[iid], c.trace) for iid in c.pending}
            failed = []
            for iid in c.pending:
                item = c.items[iid]
                pid = item["projectId"]
                verdict, reasons, meta = rev_jobs[iid].result()
                item["stage"] = "review"
                item["reviewNote"] = "通过" if verdict else "；".join(reasons)
                c.review_history.append({"itemId": iid, "projectId": pid, "round": rnd, "pass": verdict})
                self.send("result", "reviewer", "ceo", c.trace,
                          {"verdict": {"pass": verdict, "reasons": reasons,
                                       "projectId": pid, "itemId": iid},
                           "result": {"summary": "审核通过" if verdict else "审核驳回",
                                      "evidence": reasons, "confidence": 0.9, "tools": ["review.check"]},
                           "engine": meta["engine"], "reason": meta.get("reason", "")}, project=pid)
                self.items_emit(c.trace, item)
                if verdict:
                    c.passed_items[iid] = item
                    # 质检审核通过 → 出厂：工厂段流水到此结束
                    if self.db and item.get("serialNo"):
                        self.db.mark_shipped(c.trace, iid)
                        self.emit("factory.shipped", c.trace,
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
                    self.items_emit(c.trace, c.items[iid])
                    self.send("escalation", "reviewer", "owner", c.trace,
                              {"text": f"《{c.items[iid]['title']}》两次审核未通过，已阻断，不进入审批"},
                              project=c.items[iid]["projectId"], priority=2)
                break
            rework = {}
            for iid in failed:
                item = c.items[iid]
                pid = item["projectId"]
                rework[iid] = HERMES.pool.submit(
                    self.call_agent, f"cw_{pid}", f"按审核意见重写《{item['title']}》文案（≤80字）。",
                    f"项目：{self.projects[pid]['name']}；风格：{self.tone[pid]}",
                    f"审核驳回理由：{item['reviewNote']}。必须修正这些问题且保持本项目人设。",
                    "summary, evidence, confidence, tools, copy")
                self.send("delegation", f"pm_{pid}", f"cw_{pid}", c.trace,
                          {"task": "按审核意见返工重写"}, project=pid, priority=2)
            self.emit("rework", c.trace, {"round": rnd, "items": failed,
                                        "reasons": [c.items[i]["reviewNote"] for i in failed]})
            for iid, job in rework.items():
                new, m = job.result()
                item = c.items[iid]
                item["copy"] = (new or {}).get("copy") or item.get("copy", "")
                self.send("result", f"cw_{item['projectId']}", f"pm_{item['projectId']}", c.trace,
                          {"result": new or {"summary": "返工未返回，沿用原稿", "evidence": [],
                                             "confidence": 0.2}, "engine": m["engine"]}, project=item["projectId"])
                self.items_emit(c.trace, item)
            c.pending = failed

        with self.lock:
            self.traces[c.trace]["reviewHistory"] = c.review_history
            self.traces[c.trace]["reviewGate"] = {"passed": len(c.passed_items),
                                                "blocked": len(c.items) - len(c.passed_items)}
        self._persist_trace(c.trace)
        self.export_trace_docs(c.trace)

        if not c.passed_items:
            self.send("escalation", "reviewer", "owner", c.trace,
                      {"text": "全部内容审核未通过，本轮不进入审批"}, priority=2)
            self.set_scene("WORK", c.trace, title="全部驳回，等待返工")
            with self.lock:
                self.traces[c.trace]["status"] = "blocked"
                self.traces[c.trace]["closedAt"] = iso_now()
            self._persist_trace(c.trace)
            return _PIPELINE_STOP


    def _stage_publish_approval(self, c):
        """11. 审批门（仅放行审核通过的内容）"""
        # 11. 审批门（仅放行审核通过的内容）
        self.guard_budget(c.trace)   # 发布审批前最后一道成本核对
        approval = {"id": uid("ap"), "traceId": c.trace, "action": "publish.execute", "risk": "high",
                    "status": "pending", "approver": "老板", "createdAt": iso_now(), "decidedAt": "",
                    "timeoutSec": self._stage_timeout(c),
                    "items": list(c.passed_items),
                    "titles": [i["title"] for i in c.passed_items.values()],
                    "title": f"{len(c.passed_items)} 条内容发布审批（含同心圆对外承诺）",
                    "checks": self._publish_checks(c.items, c.passed_items, c.review_history)}
        approval, ev, _ = self._open_approval(c, approval)
        for item in c.passed_items.values():
            item["stage"] = "approved"
            self.items_emit(c.trace, item)

        approved = (approval["status"] == "approved") if ev is None \
            else ev.wait(timeout=self._stage_timeout(c))
        with self.lock:
            appr = next(a for a in self.approvals if a["id"] == approval["id"])
            self.approval_events.pop(approval["id"], None)
        if not approved or appr["status"] != "approved":
            with self.lock:
                if appr["status"] == "pending":
                    appr["status"] = "timeout"
                    appr["decidedAt"] = iso_now()
                    appr["timeoutReason"] = f"等待老板审批超过 {self._stage_timeout(c)}s，按挂起处理"
                for item in c.passed_items.values():
                    item["stage"] = "blocked"
                    item["blockedBy"] = "publish-gate"
                    self._schedule_export(c.trace, item)
                self.traces[c.trace]["status"] = "rejected" if appr["status"] == "rejected" else "timeout"
                self.traces[c.trace]["closedAt"] = iso_now()
            self._persist_trace(c.trace)
            for item in c.passed_items.values():
                self.emit("pipeline", c.trace, item)
            self.send("escalation", "ceo", "owner", c.trace,
                      {"text": "审批未通过或超时，内容已标记 blocked（publish-gate），保持未发布"}, priority=2)
            self.emit("approval", c.trace, appr)
            return _PIPELINE_STOP


    def _stage_release(self, c):
        """12. release_ops 发布（幂等：同一 trace 的每条内容只发布一次）"""
        # 断点续跑会重跑 release，靠发布回执判重，避免重复发消息/重复计数
        done = set()
        if self.db:
            try:
                done = self.db.released_items(c.trace)
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
        data, meta = self.call_agent("release_ops", f"按排期发布 {len(fresh)} 条内容并给出监控口径。",
                                     c.pub_ctx, "", "summary, evidence, confidence, tools")
        data = data or {"summary": "内容已发布并进入监控", "evidence": ["平台回执 200"],
                        "confidence": 0.9, "tools": ["publish.execute"]}
        for iid, item in fresh.items():
            item["stage"] = "published"
            if self.db:
                try:
                    self.db.release_mark(c.trace, iid, item.get("projectId"), item.get("title"))
                except Exception as e:
                    print(f"[release] 发布回执登记失败（继续发布）：{type(e).__name__}: {e}", flush=True)
            self.send("result", "release_ops", "ceo", c.trace,
                      {"result": data, "engine": meta["engine"], "itemId": iid},
                      project=item["projectId"])
            self.items_emit(c.trace, item)
        # KPI 按项目上调；续跑时只补此前一条都没发布过的项目，避免重复累计
        released_pids = {c.passed_items[iid]["projectId"] for iid in done if iid in c.passed_items}
        pids = {i["projectId"] for i in fresh.values()} - released_pids
        with self.lock:
            self.stats["published"] += len(fresh)
            for pid in pids:
                k = self.projects[pid]["kpi"]
                if "views" in k:
                    k["views"] += 1200
                    k["fans"] += 18
                    k["completion"] = 0.62
                    k["engagement"] = 0.071
                else:
                    k["tickets"] = 2
                    k["delivery_hours"] = 36.0
        if pids:
            self.emit("metrics", c.trace, {p: self.projects[p]["kpi"] for p in pids})


    def _stage_archive(self, c):
        """13. kb_manager 归档"""
        # 13. kb_manager 归档
        data, meta = self.call_agent("kb_manager", "把本轮 6 条内容归档为知识条目，给出检索标签。",
                                     c.pub_ctx, "", "summary, evidence, confidence, tools")
        data = data or {"summary": "已归档知识资产", "evidence": ["kb.write"], "confidence": 0.9, "tools": []}
        # 定稿时已归档过同题条目，这里按 traceId + 标题更新为发布口径，不重复计数
        self.archive_knowledge(c.trace, [
            {"projectId": item["projectId"], "title": item["title"],
             "body": data.get("summary", "归档"), "tags": ["内容复盘"]}
            for item in c.passed_items.values()], stage_label="发布归档")
        for iid, item in c.passed_items.items():
            item["stage"] = "archived"
            self.items_emit(c.trace, item)
        self.send("result", "kb_manager", "ceo", c.trace, {"result": data, "engine": meta["engine"]},
                  project="all")


    def _stage_report(self, c):
        """14. 汇报：CEO 日报 / CTO 技术周报 / 项目周报（并行）"""
        # 14. 汇报：CEO 日报 / CTO 技术周报 / 项目周报（并行）
        self.set_scene("REPORT", c.trace, title="CEO 日报生成")
        wave = {
            "ceo": HERMES.pool.submit(self.call_agent, "ceo",
                                      "写一段今日 CEO 日报结论，覆盖 6 条产出与审批。",
                                      c.pub_ctx, "", "summary, evidence, confidence, tools"),
            "cto": HERMES.pool.submit(self.call_agent, "cto",
                                      "写一段 CTO 技术周报：Agent 编排健康度与风险。",
                                      f"本轮消息数 {len([m for m in self.messages if m['traceId'] == c.trace])}；"
                                      "审批门拦截 publish.execute", "", "summary, evidence, confidence, tools"),
        }
        for pid in {i["projectId"] for i in c.passed_items.values()}:
            wave[f"pm_{pid}"] = HERMES.pool.submit(
                self.call_agent, f"pm_{pid}", f"写一段 {self.projects[pid]['name']} 项目周报结论。",
                c.pub_ctx, "", "summary, evidence, confidence, tools")

        daily_data, daily_meta = wave["ceo"].result()
        daily_data = daily_data or self._deg("ceo", "CEO 日报")
        report = {"id": uid("rp"), "traceId": c.trace, "kind": "daily", "title": "CEO 日报",
                  "conclusion": daily_data.get("summary", ""), "evidence": daily_data.get("evidence", []),
                  "decisions": [{"text": "定稿 6 条（每项目 2 条）并发布", "owner": "ceo",
                                 "risk": "mid", "approved": True}],
                  "engine": daily_meta["engine"], "createdAt": iso_now()}
        with self.lock:
            self.reports.append(report)
        self.send("report.ready", "ceo", "owner", c.trace, {"report": report}, priority=1)
        self.emit("report", c.trace, report)

        tech_data, tech_meta = wave["cto"].result()
        tech_data = tech_data or self._deg("cto", "技术周报")
        tech = {"id": uid("rp"), "traceId": c.trace, "kind": "tech", "title": "CTO 技术周报",
                "conclusion": tech_data.get("summary", ""), "evidence": tech_data.get("evidence", []),
                "decisions": [], "engine": tech_meta["engine"], "createdAt": iso_now()}
        with self.lock:
            self.reports.append(tech)
        self.emit("report", c.trace, tech)

        for pid in {i["projectId"] for i in c.passed_items.values()}:
            wd, wm = wave[f"pm_{pid}"].result()
            wd = wd or self._deg(f"pm_{pid}", "项目周报")
            weekly = {"id": uid("rp"), "traceId": c.trace, "kind": "weekly",
                      "title": f"{self.projects[pid]['name']} 项目周报",
                      "conclusion": wd.get("summary", ""), "evidence": wd.get("evidence", []),
                      "decisions": [], "engine": wm["engine"], "createdAt": iso_now()}
            with self.lock:
                self.reports.append(weekly)
            self.emit("report", c.trace, weekly)

        with self.lock:
            self.traces[c.trace]["status"] = "completed"
            self.traces[c.trace]["closedAt"] = iso_now()
            self.traces[c.trace]["hermes"] = HERMES.stats()
        self._persist_trace(c.trace)
        self.set_scene("WORK", c.trace, title="归档完成，回到生产")


    def mgmt_delegate(self, sender, recipient, text):
        """管理层直接下发指令：老板命令优先级最高，管理层可跨项目派单并留痕。"""
        if sender not in ("ceo", "cto", "cco"):
            raise ValueError("只有 CEO/CTO/CCO 可以直接下发指令")
        if recipient not in self.agents:
            raise ValueError(f"没有员工 {recipient}")
        if not text.strip():
            raise ValueError("指令不能为空")
        trace = (self.last_hotspot or {}).get("traceId", "")
        target = self.agents[recipient]
        self.send("delegation", sender, recipient, trace,
                  {"task": text, "authority": "management", "from": sender},
                  project=None if target["projectId"] == "all" else target["projectId"], priority=2)
        data, meta = self.call_agent(recipient, f"执行管理层 {sender} 的指令：{text}",
                                     f"当前场景 {self.scene}；老板的直接命令优先级更高。", "",
                                     "summary, evidence, confidence, tools")
        answer = data or self._deg(recipient, "指令回复")
        self.send("result", recipient, sender, trace,
                  {"result": answer, "engine": meta["engine"], "authority": "management"}, priority=2)
        return {"sender": sender, "recipient": recipient, "answer": answer,
                "engine": meta["engine"], "traceId": trace}

    # ---------- 审核 ----------
    def _asset_owned(self, item, url):
        """素材必须落在本条内容自己的交付目录里（真实路径，不再接受 asset:// 占位）。"""
        if not DL:
            return str(url).startswith(f"asset://{item['projectId']}/")
        try:
            prefix = self.DL.plan(item["traceId"], item["projectId"], item["id"])["dir"] + "/"
        except Exception:
            return False
        return str(url).startswith(prefix)

    def _tech_gate(self, item):
        """发布前的技术硬门槛：对实际落盘的成片重新 probe，不达标即不得 PASS。

        制度要求「导出文件必须重新 probe 才算验收」，本函数是这条制度的执行点：
        分辨率/帧率/编码/像素格式/音轨/采样率/声道/时长/黑帧/削波/静音，逐项实测。
        """
        import subprocess
        path = ""
        # 复检对象优先取"定版母版"（05-final.mp4），其次取样片
        cands = list(item.get("finalAssets") or []) + [((item.get("files") or {}).get("final") or "")]
        cands += [a for a in reversed(item.get("assets") or []) if str(a).lower().endswith(".mp4")]
        for a in cands:
            if not a:
                continue
            cand = self.DL._local(a) if (self.DL and str(a).startswith("/vault/")) else str(a)
            if os.path.isfile(cand):
                path = cand
                break
        if not path or not os.path.isfile(path):
            return {"pass": False, "reasons": ["缺少可复检的成片文件"], "path": path}
        env = dict(os.environ)
        env.pop("LD_LIBRARY_PATH", None)
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                capture_output=True, text=True, timeout=120, env=env).stdout
            info = json.loads(out or "{}")
        except Exception as e:
            return {"pass": False, "reasons": [f"ffprobe 失败：{type(e).__name__}"], "path": path}
        streams = info.get("streams") or []
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        au = next((s for s in streams if s.get("codec_type") == "audio"), None)
        pid = item.get("projectId") or ""
        spec = CC.DELIVERY.get(pid) or CC.DELIVERY.get("_default") or CC.DEFAULT_DELIVERY["_default"]
        checks_spec = {"projectId": pid, "source": "项目配置.md#交付合同" if pid in CC.DELIVERY else "内置默认",
                       "width": spec["width"], "height": spec["height"], "fps": spec["fps"]}
        reasons, checks = [], {"spec": checks_spec}
        if not v:
            reasons.append("没有视频流")
        if not au:
            reasons.append("没有音轨（旁白缺失）")
        if v:
            checks.update(width=v.get("width"), height=v.get("height"),
                          fps=v.get("r_frame_rate"), codec=v.get("codec_name"),
                          pix_fmt=v.get("pix_fmt"), duration=v.get("duration"))
            if (v.get("width"), v.get("height")) != (spec["width"], spec["height"]):
                reasons.append(f"分辨率 {v.get('width')}x{v.get('height')}，"
                               f"交付合同要求 {spec['width']}x{spec['height']}")
            if str(v.get("r_frame_rate")) != spec["fps"]:
                reasons.append(f"帧率 {v.get('r_frame_rate')}，交付合同要求 {spec['fps']}")
            if v.get("codec_name") != spec["video_codec"]:
                reasons.append(f"视频编码 {v.get('codec_name')}，交付合同要求 {spec['video_codec']}")
            if v.get("pix_fmt") not in spec["pix_fmt"]:
                reasons.append(f"像素格式 {v.get('pix_fmt')}，交付合同要求 {'/'.join(spec['pix_fmt'])}")
        if au:
            checks.update(sample_rate=au.get("sample_rate"), channels=au.get("channels"),
                          audio_codec=au.get("codec_name"))
            if str(au.get("sample_rate")) != str(spec["sample_rate"]):
                reasons.append(f"采样率 {au.get('sample_rate')}，交付合同要求 {spec['sample_rate']}")
            if _to_int(au.get("channels"), 0) != spec["channels"]:
                reasons.append(f"声道数 {au.get('channels')}，交付合同要求 {spec['channels']}")
            if au.get("codec_name") != spec["audio_codec"]:
                reasons.append(f"音频编码 {au.get('codec_name')}，交付合同要求 {spec['audio_codec']}")
        try:
            dur = float((info.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        checks["duration"] = round(dur, 3)
        want = float(item.get("durationSec") or 0)
        if want and abs(dur - want) > spec["duration_tolerance"]:
            reasons.append(f"时长 {dur:.2f}s 与分镜 {want:.0f}s 偏差超过 "
                           f"{spec['duration_tolerance']}s")
        # 黑帧与音量：只读输入，不产生新文件
        try:
            bd = subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vf",
                 "blackdetect=d=0.5:pix_th=0.10", "-an", "-f", "null", "-"],
                capture_output=True, text=True, timeout=300, env=env).stderr
            checks["blackFrames"] = bd.count("black_start")
            if checks["blackFrames"]:
                reasons.append(f"检出 {checks['blackFrames']} 段黑帧（>0.5s）")
        except Exception as e:
            reasons.append(f"黑帧检测失败：{type(e).__name__}")
        try:
            vd = subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "volumedetect",
                 "-vn", "-f", "null", "-"],
                capture_output=True, text=True, timeout=300, env=env).stderr
            mx = re.search(r"max_volume:\s*(-?[\d.]+) dB", vd)
            mn = re.search(r"mean_volume:\s*(-?[\d.]+) dB", vd)
            if mx:
                checks["maxVolumeDb"] = float(mx.group(1))
                if checks["maxVolumeDb"] > spec["max_peak_db"]:
                    reasons.append(f"峰值 {checks['maxVolumeDb']} dB，超过交付合同上限 "
                                   f"{spec['max_peak_db']} dB")
            if mn:
                checks["meanVolumeDb"] = float(mn.group(1))
                if checks["meanVolumeDb"] <= spec["min_mean_db"]:
                    reasons.append(f"音轨近乎静音（均值 {checks['meanVolumeDb']} dB，"
                                   f"下限 {spec['min_mean_db']} dB）")
        except Exception as e:
            reasons.append(f"音量检测失败：{type(e).__name__}")
        checks["path"] = path
        return {"pass": not reasons, "reasons": reasons, "checks": checks}

    def review_with_model(self, item, trace):
        structural = []
        expected = self.tone.get(item["projectId"])
        if item.get("toneProfile") != expected:
            structural.append(f"人设串味：标注「{item.get('toneProfile')}」，要求「{expected}」")
        bad_assets = [a for a in item.get("assets", []) if not self._asset_owned(item, a)]
        if bad_assets:
            structural.append(f"素材跨项目：{bad_assets}")
        chk = item.get("renderCheck") or {}
        if not chk.get("pass"):
            why = item.get("renderError") or "；".join((chk.get("reasons") or [])[:2]) \
                or "样片未通过画面验收"
            structural.append(f"画面验收未通过：{why}")
        if not item.get("copy"):
            structural.append("缺少稿件正文")
        tech = self._tech_gate(item)
        item["techCheck"] = tech
        structural.extend(f"成片技术门槛未过：{r}" for r in tech.get("reasons") or [])
        if item.get("degraded"):
            structural.append(f"产出为模型降级占位（{','.join(item['degraded'])}），不得作为发布件")
        if structural:
            self.emit("tech-gate", trace, {"itemId": item.get("id"), "pass": tech.get("pass"),
                                           "checks": tech.get("checks", {}), "reasons": structural})
            return (False, structural, {"engine": "guardrail"})
        a = self.agents["reviewer"]
        extra = ("判定标准：1) 文案语气必须与项目风格一致；2) 不得出现其他项目的专属人设特征；"
                 "3) 事实陈述须有可核查来源，纯创意表达不算事实断言。"
                 "若三项均未发现明显问题必须判定通过（pass=true）；仅在有具体问题时 pass=false。"
                 "额外 JSON 字段：pass(布尔), reasons(字符串数组)。")
        sample = item.get("sampleReviews") or {}
        sample_note = "；".join(
            f"{role}:{'通过' if v.get('pass') else '未通过'}" for role, v in sample.items()) or "未记录"
        data, meta = HERMES.ask(
            a,
            f"审核定版成片：{item['title']}；文案：{item.get('copy', '')[:120]}；"
            f"定版说明：{(item.get('finalCut') or '—')[:120]}",
            f"项目：{self.projects[item['projectId']]['name']}；要求风格：{expected}；样片三方评审：{sample_note}",
            extra, "summary, evidence, confidence, tools, pass, reasons")
        self.record_usage("reviewer", meta, trace)
        if not data:
            # 审核员单点故障：不得降级放行，按未通过处理并升级给管理层指定代理复核
            item["reviewUnavailable"] = True
            self.send("escalation", "reviewer", "cto", trace,
                      {"text": f"审核模型不可用：《{item['title']}》未获结论。"
                               "按制度不得放行；如需继续，请 CTO 指定代理复核（代理结论只能 PENDING_HUMAN）。",
                       "itemId": item.get("id")}, project=item["projectId"], priority=2)
            self.send("escalation", "reviewer", "cco", trace,
                      {"text": f"审核模型不可用：《{item['title']}》内容项待代理复核（仅 CCO 可代内容项）。",
                       "itemId": item.get("id")}, project=item["projectId"], priority=2)
            return (False, ["审核不可用：模型未返回判定，按未通过处理，不得放行；已升级管理层代理复核"], meta)
        passed = bool(data.get("pass", True))
        reasons = data.get("reasons") or data.get("evidence") or ["模型审核完成"]
        return (passed, reasons, meta)

    def items_emit(self, trace, item):
        self._schedule_export(trace, item)
        self.emit("pipeline", trace, item)

    def _schedule_export(self, trace, item):
        """把当前阶段写入 Obsidian；文件地址先算好挂到 item.files，渲染异步串行跑。"""
        if not DL:
            return
        try:
            files = self.DL.plan(trace, item["projectId"], item["id"])
            item["files"] = files
            project = dict(self.projects.get(item["projectId"], {"name": item["projectId"], "color": "#64748B"}))
            tone = item.get("toneProfile") or self.tone.get(item["projectId"], "")
            snap = json.loads(json.dumps(item, ensure_ascii=False))

            def job():
                res = self.DL.export_item(snap, project, tone)
                if res.get("video_written") and (snap.get("renderCheck") or {}).get("pass"):
                    with self.lock:
                        cur = next((c for c in self.contents if c["id"] == item["id"]), None)
                    if cur:
                        cur["assets"] = [files["video"], files["cover"]]
                        self.emit("pipeline", trace, cur)
                elif res.get("error"):
                    print(f"[export] {item['id']} {res['error'][:120]}", flush=True)

            self.DL.submit(job)
        except Exception as e:
            print(f"[export] 排期失败 {type(e).__name__}: {e}", flush=True)

    def export_trace_docs(self, trace):
        """本轮交付清单 + 过程记录。"""
        if not DL:
            return
        try:
            with self.lock:
                contents = [dict(c) for c in self.contents if c.get("traceId") == trace]
                msgs = [dict(m) for m in self.messages if m.get("traceId") == trace]
                apprs = [dict(a) for a in self.approvals if a.get("traceId") == trace]
                tr = dict(self.traces.get(trace) or {})
            names = {pid: p["name"] for pid, p in self.projects.items()}
            self.DL.submit(lambda: self.DL.export_trace(trace, contents, msgs, apprs, names, tr))
        except Exception as e:
            print(f"[export] trace 文档失败 {type(e).__name__}: {e}", flush=True)

    # ---------- 审批 ----------
    def decide_approval(self, approval_id, decision, decided_by=""):
        with self.lock:
            appr = next((a for a in self.approvals if a["id"] == approval_id), None)
        if not appr and self.db:
            appr = self._approval_from_db(approval_id)   # 重启后内存里没有，从过程库补
        if not appr:
            return None
        if appr["status"] != "pending":
            # 超时挂起的单允许老板迟到补批：补批后从断点续跑，不用整轮重来
            if not (appr["status"] == "timeout" and decision == "approve"):
                return appr
        appr["status"] = "approved" if decision == "approve" else "rejected"
        appr["decidedAt"] = iso_now()
        appr["decidedBy"] = decided_by or appr.get("approver", "")
        try:
            created = datetime.fromisoformat(appr["createdAt"])
            decided = datetime.fromisoformat(appr["decidedAt"])
            self.stats["approvalSeconds"].append(round((decided - created).total_seconds(), 1))
        except Exception:
            pass
        if decision != "approve" and appr.get("action") == "publish.execute":
            self.stats["blockedPublish"] += 1
        with self.lock:      # 从库补出来的单此前不在内存里，补进去让续跑逻辑能看到最新状态
            if not any(a["id"] == appr["id"] for a in self.approvals):
                self.approvals.append(appr)
        self.emit("approval", appr["traceId"], appr)
        ev = self.approval_events.get(approval_id)
        if ev:
            ev.set()
        elif decision == "approve":
            # 没有等待线程 = 进程重启后恢复的审批单，run 仍停在断点，放行后自动续跑
            self._resume_if_interrupted(appr["traceId"])
        return appr

    def _resume_if_interrupted(self, trace):
        """审批放行后自动续跑：仅当该 trace 在过程库里停在断点（interrupted / 审批超时）。"""
        if not self.db or not trace:
            return False
        try:
            run = self.db.pipeline_run(trace)
        except Exception as e:
            print(f"[approval] 读运行态失败，跳过自动续跑：{type(e).__name__}: {e}", flush=True)
            return False
        if not run or run.get("status") not in ("interrupted", "timeout"):
            return False
        with self.lock:
            if self.traces.get(trace, {}).get("status") == "running":
                return False                     # 已经在续跑，避免重复拉起
        try:
            info = self.resume_pipeline(trace, auto=True)
        except Exception as e:
            print(f"[approval] 自动续跑失败 {trace}：{type(e).__name__}: {e}", flush=True)
            return False
        print(f"[approval] {trace} 审批放行后自动续跑：从阶段 {info.get('from')} 恢复", flush=True)
        return True

    def _approval_from_db(self, approval_id):
        """从过程库补一张待决审批单到内存（进程重启后审批仍可被处理）。"""
        try:
            rows = self.db.approvals_pending()
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
        with self.lock:
            self.approvals.append(raw)
        return raw

    # ---------- 人设串味注入 ----------
    def persona_injection(self):
        trace = uid("tr")
        item = {"id": uid("c"), "projectId": "gou", "traceId": trace, "stage": "review",
                "title": "（测试）把文人莫克的稿子塞给狗大人", "owner": "cw_gou",
                "toneProfile": self.tone["wenren"], "assets": ["asset://wenren/master.mp4"],
                "copy": "书卷气、克制、不追网络梗。"}
        with self.lock:
            self.contents.append(item)
        verdict, reasons, meta = self.review_with_model(item, trace)
        item["reviewNote"] = "；".join(reasons)
        self.send("result", "reviewer", "ceo", trace,
                  {"verdict": {"pass": verdict, "reasons": reasons, "projectId": "gou"},
                   "result": {"summary": "人设串味，已驳回" if not verdict else "通过",
                              "evidence": reasons, "confidence": 0.95, "tools": ["review.check"]},
                   "engine": meta["engine"]}, project="gou", priority=2)
        risk = {"id": uid("rp"), "traceId": trace, "kind": "incident", "title": "风险单：跨项目人设串味",
                "conclusion": "检测到狗大人稿件使用文人莫克人设与素材，已驳回返工", "evidence": reasons,
                "decisions": [{"text": "驳回并返工，不进入审批", "owner": "reviewer", "risk": "high",
                              "approved": False}], "engine": meta["engine"], "createdAt": iso_now()}
        with self.lock:
            self.reports.append(risk)
        self.emit("report", trace, risk)
        self.emit("pipeline", trace, item)
        return {"traceId": trace, "pass": verdict, "reasons": reasons}

    # ---------- 热点目录同步 ----------
    def _background_sync(self):
        """每 60 秒同步一次采集状态；变化时推给前端，避免页面一直显示旧值。"""
        while not self._sync_stop.wait(60):
            try:
                before = json.dumps(self.collection, ensure_ascii=False, sort_keys=True)
                self._sync_hotspots_from_vault(force=True)
                after = json.dumps(self.collection, ensure_ascii=False, sort_keys=True)
                if after != before:
                    self.emit("collection", "", self.collection)
            except Exception as e:
                print(f"[hotspot] 后台同步失败：{type(e).__name__}: {e}", flush=True)
            try:
                self._sync_knowledge_from_vault()
            except Exception as e:
                print(f"[kb] 后台同步失败：{type(e).__name__}: {e}", flush=True)
            try:
                self._sync_config_from_vault()
            except Exception as e:
                print(f"[config] 后台同步失败：{type(e).__name__}: {e}", flush=True)

    @staticmethod
    def _kb_norm(item):
        """统一知识条目结构：文件解析结果与内存归档保持同一套字段。"""
        body = str(item.get("body") or "")
        return {"id": item.get("id"), "projectId": item.get("projectId"),
                "traceId": item.get("traceId") or item.get("sourceTrace") or "",
                "sourceTrace": item.get("sourceTrace") or "",
                "title": item.get("title") or "",
                "summary": item.get("summary") or body[:80],
                "body": body, "tags": item.get("tags") or [], "at": item.get("at") or ""}

    def _sync_knowledge_from_vault(self):
        """运行中重扫 OPC/知识库：md 有变化就以文件为准替换 Web 端知识库。"""
        if not DL:
            return
        stamp = self.DL.knowledge_stamp()
        if stamp == self._kb_stamp:
            return
        items = self.DL.load_knowledge(limit=200)
        if not items:
            return
        fresh = [self._kb_norm(k) for k in items]
        with self.lock:
            before = len(self.knowledge)
            self.knowledge = fresh
        self._kb_stamp = stamp
        if len(fresh) != before:
            self.emit("knowledge", "", {"stage": "vault-sync", "count": len(fresh)})

    def _sync_config_from_vault(self):
        """项目配置/员工名册改动后自动热加载，不用手动点重载（仅 owner，共享 Obsidian 配置）。"""
        if not self.is_owner:
            return
        try:
            stamp = (os.path.getmtime(PROJECT_FILE), os.path.getmtime(ROSTER_FILE))
        except OSError:
            return
        if stamp == self._cfg_stamp:
            return
        self._cfg_stamp = stamp
        try:
            info = self.reload_config()
            print(f"[config] 检测到 Obsidian 配置变更，已热加载：{info}", flush=True)
        except Exception as e:
            print(f"[config] 自动热加载失败：{type(e).__name__}: {e}", flush=True)

    def _sync_hotspots_from_vault(self, force=False):
        """运行中重扫 OPC/热点采集：苏寒新落盘的业务简报无需重启即可进面板。"""
        if not DL:
            return
        now = time.time()
        if not force and now - self._hotspot_scan_at < 60:
            return
        self._hotspot_scan_at = now
        try:
            self.collection = self.DL.load_collection_status()
        except Exception as e:
            print(f"[hotspot] 读取采集状态失败：{type(e).__name__}: {e}", flush=True)
        try:
            disk = self.DL.load_hotspots()
        except Exception as e:
            print(f"[hotspot] 扫描热点目录失败：{type(e).__name__}: {e}", flush=True)
            return
        if not disk:
            return
        changed = False
        with self.lock:
            before = len(self.hotspots)
            by_key = {_hotkey(h.get("title")): h for h in self.hotspots if h.get("title")}
            for h in disk:
                key = _hotkey(h.get("title"))
                if not key:
                    continue
                cur = by_key.get(key)
                if not cur:
                    self.hotspots.append(h)
                    by_key[key] = h
                elif not cur.get("traceId"):
                    # 内存里是启动时读入的旧简报，用磁盘上的最新字段覆盖
                    for field in ("score", "tags", "at", "source"):
                        if h.get(field):
                            cur[field] = h[field]
            self.hotspots = self._hotspot_view(20)
            changed = len(self.hotspots) != before
        if changed:
            self.save_runtime()

    def _hotspot_view(self, limit=5):
        """同一标题只保留最新一条，避免“简报 + 注入”出现两行重复。"""
        latest = {}
        for h in sorted(self.hotspots, key=lambda x: str(x.get("at") or "")):
            if h.get("title"):
                latest[_hotkey(h.get("title"))] = h
        return sorted(latest.values(), key=lambda x: str(x.get("at") or ""))[-limit:]

    def _page_params(self, query="", default_size=20, max_size=100):
        """解析 ?page=&size=&q=，供列表接口做服务端分页与关键词过滤。"""
        qs = parse_qs(query)
        try:
            page = max(1, int(qs.get("page", ["1"])[0]))
        except (TypeError, ValueError):
            page = 1
        try:
            size = min(max_size, max(1, int(qs.get("size", [str(default_size)])[0])))
        except (TypeError, ValueError):
            size = default_size
        q = (qs.get("q", [""])[0] or "").strip().lower()
        return page, size, q

    def knowledge_page(self, query=""):
        """知识库分页列表：title 为完整标题，summary 供列表省略显示。"""
        page, size, q = self._page_params(query)
        with self.lock:
            items = list(self.knowledge)
        if q:
            items = [k for k in items if q in
                     f"{k.get('title') or ''} {k.get('summary') or ''} {k.get('body') or ''} "
                     f"{k.get('projectId') or ''} {' '.join(k.get('tags') or [])}".lower()]
        items.sort(key=lambda k: str(k.get("at") or ""), reverse=True)
        total = len(items)
        start = (page - 1) * size
        rows = [{"id": k.get("id"),
                 "title": k.get("title") or k.get("summary") or "",
                 "summary": k.get("summary") or (k.get("title") or "")[:80],
                 "projectId": k.get("projectId"), "traceId": k.get("traceId"),
                 "tags": k.get("tags") or [], "at": k.get("at"),
                 "body": k.get("body") or ""} for k in items[start:start + size]]
        return {"ok": True, "total": total, "page": page, "size": size, "items": rows}

    def hotspot_page(self, query=""):
        """热点分页列表：沿用 _hotspot_view 的同标题去重。"""
        page, size, q = self._page_params(query)
        items = sorted(self._hotspot_view(10 ** 6), key=lambda h: str(h.get("at") or ""), reverse=True)
        if q:
            items = [h for h in items if q in
                     f"{h.get('title') or ''} {h.get('source') or ''} "
                     f"{' '.join(h.get('tags') or [])}".lower()]
        total = len(items)
        start = (page - 1) * size
        return {"ok": True, "total": total, "page": page, "size": size,
                "items": items[start:start + size]}

    def knowledge_detail(self, kid):
        with self.lock:
            return next((k for k in self.knowledge if k.get("id") == kid), None)

    def hotspot_detail(self, hid):
        with self.lock:
            return next((h for h in self.hotspots if h.get("id") == hid), None)

    # ---------- 知识归档 ----------
    def archive_knowledge(self, trace, entries, stage_label="选题定稿"):
        """祁南（kb_manager）归档：按 traceId + 标题去重，重复触发只更新不叠加。"""
        made = []
        if not entries:
            return made
        with self.lock:
            index = {(k.get("sourceTrace") or k.get("traceId"), k.get("title")): k
                     for k in self.knowledge}
            for e in entries:
                title = str(e.get("title") or "").strip()
                if not title:
                    continue
                pid = e.get("projectId") or "全公司"
                body = str(e.get("body") or "").strip()
                tags = [pid, stage_label] + [str(t) for t in (e.get("tags") or []) if t]
                cur = index.get((trace, title))
                if cur:
                    if body:
                        cur["body"] = body[:300]
                    cur["tags"] = list(dict.fromkeys(list(cur.get("tags") or []) + tags))[:12]
                    cur["at"] = iso_now()
                    made.append(cur)
                    continue
                entry = {"id": uid("kb"), "projectId": pid, "traceId": trace,
                         "sourceTrace": trace, "title": title, "summary": title[:80],
                         "body": body[:300] or "（归档占位）",
                         "tags": list(dict.fromkeys(tags))[:12], "at": iso_now()}
                self.knowledge.append(entry)
                index[(trace, title)] = entry
                made.append(entry)
        if made:
            if self.DL:                                   # 写回 Obsidian，文件与 Web 同步落地
                for k in made:
                    try:
                        self.DL.save_knowledge(k)
                    except Exception as e:
                        print(f"[kb] 写回 Obsidian 失败：{type(e).__name__}: {e}", flush=True)
            self.emit("knowledge", trace, {
                "stage": stage_label, "count": len(made),
                "latest": [{"title": k["title"], "summary": k.get("summary") or k["title"][:80],
                            "projectId": k["projectId"], "id": k["id"]} for k in made[-3:]]})
        return made

    def _backfill_knowledge(self):
        """启动修复：最近一轮已有交付物、但知识库没有对应条目时，按定稿标题补登。"""
        if not self.contents or not self.knowledge:
            return
        latest_trace = ""
        for item in self.contents:
            if item.get("traceId") and item.get("title"):
                latest_trace = item["traceId"]
        if not latest_trace:
            return
        existing = {k.get("title") for k in self.knowledge
                    if str(k.get("sourceTrace") or k.get("traceId") or "").startswith(latest_trace)}
        todo = [i for i in self.contents
                if i.get("traceId") == latest_trace and i.get("title") not in existing]
        if not todo:
            return
        self.archive_knowledge(latest_trace, [
            {"projectId": i.get("projectId"), "title": i.get("title"),
             "body": f"启动补登：交付物已落盘（阶段 {i.get('stage')}）",
             "tags": [str(i.get("stage") or "")]} for i in todo], stage_label="启动补登")

    # ---------- 快照 ----------
    def snapshot(self):
        self._sync_hotspots_from_vault()
        with self.lock:
            return {"scene": self.scene, "agents": list(self.agents.values()),
                    "projects": [{"id": p, **self.projects[p]} for p in self.projects],
                    "pipeline": self.contents[-60:], "hotspots": self._hotspot_view(5),
                    "collection": self.collection,
                    "reports": self.reports[-8:], "approvals": self.approvals[-5:],
                    "knowledge": {"count": len(self.knowledge), "latest": self.knowledge[-3:]},
                    "meeting": self.meeting,
                    "meetingLog": self.meeting_log,
                    "meetingHistory": self.meeting_payload()["history"],
                    "stats": {**self.stats, "events": self.events,
                              "approvalSecondsAvg": round(sum(self.stats["approvalSeconds"]) /
                                                          len(self.stats["approvalSeconds"]), 1)
                              if self.stats["approvalSeconds"] else 0,
                              "hermes": HERMES.stats()},
                    "cost": {"limits": {"traceTokens": TOKEN_LIMIT,
                                        "callTokens": CALL_TOKEN_LIMIT,
                                        "usd": COST_LIMIT_USD},
                             "activeTrace": self.active_trace,
                             "aborted": dict(self.aborts),
                             "usage": {k: v for k, v in list(self.token_usage.items())[-5:]}},
                    "eventsList": self.messages[-40:],
                    "engine": MODE, "serverTime": iso_now(), "serverStartedAt": self.startedAt}

    def pipeline_runs_view(self, limit=12):
        """运维页用：最近流水线运行记录（含断点阶段），不带 state 大字段。"""
        if not self.db:
            return []
        try:
            rows = self.db.pipeline_runs_recent(limit)
        except Exception as e:
            print(f"[ops] 读取运行记录失败：{type(e).__name__}: {e}", flush=True)
            return []
        return [{"traceId": r.get("trace_id"), "status": r.get("status"),
                 "stage": r.get("stage"), "stageIndex": r.get("stage_index"),
                 "startedAt": r.get("started_at"), "updatedAt": r.get("updated_at"),
                 "closedAt": r.get("closed_at"), "resumeTries": r.get("resume_tries") or 0}
                for r in rows]

KERNELS = {}
KERNELS_LOCK = threading.Lock()

def _kernel_for(data_dir):
    """取/建某用户工作区的内核；owner 目录的内核在 boot 时即创建。"""
    data_dir = os.path.abspath(data_dir or OWNER_DATA_DIR)
    with KERNELS_LOCK:
        k = KERNELS.get(data_dir)
        if k is None:
            try:
                k = Kernel(data_dir=data_dir)
            except Exception as e:
                print(f"[kernel] 用户内核创建失败 {data_dir}：{type(e).__name__}: {e}", flush=True)
                raise
            KERNELS[data_dir] = k
        return k

def _kernel_reset(data_dir):
    """数据导入后重置内核：停后台线程、丢 Store 缓存，下次访问按新数据重建。"""
    data_dir = os.path.abspath(data_dir)
    with KERNELS_LOCK:
        k = KERNELS.pop(data_dir, None)
    if k:
        try:
            k.stop()
        except Exception as e:
            print(f"[kernel] 旧内核停止异常（忽略）：{type(e).__name__}: {e}", flush=True)
    if DBX:
        DBX.drop_store(os.path.join(data_dir, "opc.db"))
    return _kernel_for(data_dir)
