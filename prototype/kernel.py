"""流水线内核：PipelineContext / Kernel（阶段编排）/ 业务方法入口。

不反向依赖 server：基础设施单例来自 deps，公司配置来自 company_config，
模型调用统一走 hermes_adapter（HERMES）；HTTP 路由与会话留在 server.Handler。

C1 拆分后各模块边界：
- events.py    事件总线（SSE 广播 + 关键事件落库）
- pool.py      按工作区取/建 Kernel 的池与 LRU 逐出
- kb_views.py  知识库分类/分页/详情/图谱边/提示词召回
- factory.py   工厂/渲染：分镜工作台、素材作业、出片验收门、工具箱
- approvals.py 审批门与断点续跑：巡检/决策/重启恢复/自动续跑/CEO 巡检
- ops.py       运营支撑：会议记录、Obsidian 热同步、知识归档、交付导出
- pipeline_kit.py 流水线无状态辅助：归一化、评审映射、审批摘要、技术门、车间事件
- pipeline.py   流水线编排与阶段：PipelineContext、run_pipeline、15 个 _stage_*
本文件保留审批、工厂、工具箱与入口编排；上述能力以薄委托方式暴露，
调用方（server/agent/eval）无需感知实现位置。
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
import message_feeds as FEED
import pipeline_defs as PIPE
import sancai as SC


def _assign_task_name(trace, title="", db=None):
    """建单即分配中文任务号（第 012 号 · 《标题》）；失败只打一行，绝不挡开工。"""
    try:
        import task_names as TN
        TN.assign(trace, title, db)
    except Exception as e:            # noqa: BLE001
        print(f"[task] 编号分配失败：{type(e).__name__}: {e}", flush=True)
from hermes_adapter import HERMES
from obs import log_event       # C3：失败可见，替代静默 pass
from events import EventBus     # C1：事件总线独立成模块
import kb_views                # C1：知识库视图/图谱边独立成模块
import factory                  # C1：工厂/渲染独立成模块
import video_jobs               # 远程视频作业（提交/状态/审批/签名产物）
import approvals                # C1：审批门/断点续跑独立成模块
import capacity                 # CEO 产能均衡口径（各产品组成品数/缺口）
import ops                      # C1：会议/vault 同步/知识归档/交付导出独立成模块
import pipeline_kit             # C1：流水线无状态辅助（归一/评审映射/技术门/车间事件）
import pipeline                 # C1：流水线编排与 15 个阶段独立成模块
from pipeline import BudgetAbort, PipelineContext, _PIPELINE_STOP  # re-export：旧调用点保持不变
from kb_views import (KB_CATEGORIES, KB_CATEGORY_MAP, KB_INJECT_ON, KB_INJECT_LIMIT,
                      KB_INJECT_CHARS, _infer_category, _rel_key, _core_title, _kb_from_row)
try:
    import vault_mirror as DOCMIRROR   # 共享 vault 文档 → 老板个人空间（内部 300 秒节流）
except Exception as _e:
    DOCMIRROR = None
    print(f"[boot] vault_mirror 导入失败：{type(_e).__name__}: {_e}", flush=True)



class BusyError(Exception):
    """已有一轮选题会未收口：拒绝并发注入，避免两条流水线互相覆盖。"""

    def __init__(self, trace):
        super().__init__(f"上一轮选题会仍在进行（{trace}），请等它结束或先处理待审批")
        self.traceId = trace






# 注入块字符预算：知识图谱再有用也不能把提示词撑爆，超预算按相关度截断











class Kernel:
    def __init__(self, data_dir=""):
        # 多用户：data_dir 非空 = 独立工作区（过程库/素材/vault/编制都在该目录下）；
        # 空 = 全局兜底内核（存量老板空间，共用 Obsidian vault 与全局编制）。
        self.data_dir = os.path.abspath(data_dir) if data_dir else ""
        self.is_owner = self.data_dir == os.path.abspath(OWNER_DATA_DIR) or not self.data_dir
        # owner 用全局 Obsidian vault（与 DL/公司配置一致），注册用户用自己工作区下的 vault/。
        # 注意不能写成 data_dir/vault：OWNER_DATA_DIR 是 owner 的工作区（过程库/素材），
        # 下面并没有 vault 目录，员工拿到的知识库范围会指向不存在的路径。
        self.vault_dir = (os.path.join(self.data_dir, "vault")
                          if self.data_dir and not self.is_owner else VAULT)
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
        self.messages = []
        self.hotspots, self.contents, self.reports, self.approvals, self.knowledge = [], [], [], [], []
        self._hotspot_scan_at = 0.0        # 热点目录重扫节流（秒级时间戳）
        # 知识库目录指纹：初值必须是空，否则后台 sync 首轮就判"没变化"直接早退，
        # 导致 db 永远停在上次快照（实测 db 74 条 vs vault 90 条就是这么来的）
        self._kb_stamp = (0, 0.0)
        try:
            self._cfg_stamp = (os.path.getmtime(PROJECT_FILE), os.path.getmtime(ROSTER_FILE))
        except OSError:
            self._cfg_stamp = None         # 项目配置/名册 mtime 指纹
        self.collection = None             # 苏寒最近一次原始榜采集状态（只展示，不当业务热点）
        self.bus = EventBus()      # 事件总线（独立锁，订阅端慢不阻塞流水线）
        self.approval_events, self.traces = {}, {}
        self.directives = []       # C4：老板指令（原文 + 来源 + 生效范围），落库为真源
        self.stats = {"approvalSeconds": [], "published": 0, "blockedPublish": 0, "traces": 0}
        # 成本护栏：按 trace 聚合 token/花费，阈值/异常由 CTO 处置
        self.token_usage = {}
        self.token_alerts = {}
        self.aborts = {}
        self.active_trace = ""
        self.cancel_requests = {}   # trace -> 取消原因；pipeline 阶段边界检查后收口
        self.last_hotspot = None
        # 手工多次启动：素材制作/剪辑合成/终版合成的手工触发排队（不阻塞 HTTP 线程）
        self.stage_launches = []
        self._stage_queue = None
        self._stage_thread = None
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
                # DB 就绪后同步内存中的知识库到 SQLite（带完整字段）
                with self.lock:
                    kb_snapshot = list(self.knowledge)
                for k in kb_snapshot:
                    self.db.upsert_knowledge(k)
                if kb_snapshot:
                    print(f"[boot] 知识库同步到 SQLite：{len(kb_snapshot)} 条", flush=True)
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
        threading.Thread(target=approvals._approval_sweeper, args=(self,), daemon=True).start()
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
        return approvals._approval_sweeper(self)

    def _sweep_approvals(self):
        return approvals._sweep_approvals(self)

    def _restore_pending_approvals(self):
        return approvals._restore_pending_approvals(self)

    AUTO_RESUME_MAX_TRIES = approvals.AUTO_RESUME_MAX_TRIES   # 真源在 approvals.py

    def _mark_interrupted_runs(self):
        return approvals._mark_interrupted_runs(self)

    def _auto_resume_loop(self, traces):
        return approvals._auto_resume_loop(self, traces)

    def _wait_run_done(self, trace, poll=2.0):
        return approvals._wait_run_done(self, trace, poll)

    def resume_pipeline(self, trace, auto=False):
        return approvals.resume_pipeline(self, trace, auto)

    # ---------- CEO 超时代审批 + 产能均衡（老板 2026-11-16 授权） ----------
    def ceo_proxy_sweep(self):
        """手动触发一轮 CEO 代审（老板/巡检可调；仍需过限速与置信度门槛）。"""
        return approvals._ceo_proxy_kick(self)

    def ceo_proxy_status(self):
        return approvals.ceo_proxy_status()

    def capacity_snapshot(self):
        return capacity.snapshot(self)

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
                    # 启动时同步到 SQLite（带完整字段）
                    if self.db:
                        for k in self.knowledge:
                            self.db.upsert_knowledge(k)
            except Exception as e:
                print(f"[boot] 恢复历史知识失败：{type(e).__name__}: {e}", flush=True)
        self._backfill_knowledge()
        try:
            self.rebuild_relations()      # 关系边落库：前端只读，不再每次自算
        except Exception as e:
            print(f"[boot] 关系边重建失败：{type(e).__name__}: {e}", flush=True)
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
            # 固定 ".tmp" 名在并发保存时会互相截断（两次 json.dump 交织 → 半个 JSON 被 replace 上正式文件）：
            # 进程内串行 + 每次唯一临时名，写坏只影响临时文件，正式文件永远是一份完整快照
            with _SAVE_LOCK:
                tmp = f"{self.journal_file}.{os.getpid()}.{threading.get_ident()}.tmp"
                try:
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self.journal_file)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError as e:
                            log_event("save_runtime.cleanup_tmp", e, level="debug", quiet=True, path=tmp)
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

    # ---------- 消息中心三源（Hermes / 制片工厂 / 大模型） ----------
    def _trace_title(self, trace):
        with self.lock:
            tr = self.traces.get(trace) or {}
        return str((tr.get("hotspot") or {}).get("title") or trace or "实时任务")

    def _factory_products(self, trace, stage_id):
        """本工位推进的产品工序：按 CCO 定格的工艺步骤序列对齐 opcStage。"""
        if not self.db:
            return []
        out = []
        try:
            rows = [r for r in self.db.product_crafts(500)
                    if (r.get("trace_id") or "") == trace]
        except Exception as e:
            print(f"[msgfeed] 读取工艺决策失败：{type(e).__name__}: {e}", flush=True)
            return []
        for row in rows:
            try:
                seq = json.loads(row.get("steps") or "null")
            except ValueError:
                seq = None
            if not seq:
                seq = CC.CRAFT_STEPS.get(row.get("craft_id") or "", [])
            steps = [s.get("name") for s in seq if s.get("opcStage") == stage_id]
            out.append({"itemId": row.get("item_id"), "title": row.get("title"),
                        "workshopNo": row.get("workshop_no"),
                        "craftName": row.get("craft_name"), "steps": steps})
        return out

    # ---------- 总线 ----------
    def emit(self, kind, trace, data):
        return self.bus.emit(kind, trace, data, db=self.db, on_key=self.save_runtime)

    @property
    def subscribers(self):
        """只读视图：SSE 路由应从 bus.subscribe/unsubscribe 进出。"""
        return self.bus.subscribers

    @property
    def events(self):
        return self.bus.count

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
        return ops.meeting_begin(self, trace, title, participants, agenda)

    def meeting_say(self, aid, text, kind='speech', trace=''):
        return ops.meeting_say(self, aid, text, kind, trace)

    def meeting_end(self, trace, conclusion='', minutes_url=''):
        return ops.meeting_end(self, trace, conclusion, minutes_url)

    def meeting_payload(self):
        return ops.meeting_payload(self)

    def meeting_detail(self, mid):
        return ops.meeting_detail(self, mid)

    # ---------- 调用模型 ----------
    def call_agent(self, aid, task, context="", extra="", schema=None, kb=None, preferred=""):
        """调用员工。kb 控制知识库召回条数（0=不注入，None=默认上限）；preferred 指定首选通道 id/name。"""
        a = self.agents[aid]
        self.set_agent(aid, "working", task[:40])
        n = KB_INJECT_LIMIT if kb is None else _to_int(kb, 0)
        if n > 0 and KB_INJECT_ON:
            try:
                # 检索词以 task 为主：context 过长会掺入大量泛词把相关性打分拉平
                block = self.recall_kb(f"{task}\n{str(context or '')[:200]}", limit=n)
            except Exception as e:      # 召回失败不能拖垮这次调用
                block = ""
                print(f"[kb] 召回失败，跳过注入：{type(e).__name__}: {e}", flush=True)
            if block:
                context = f"{context}\n\n{block}" if context else block
        data, meta = HERMES.ask(a, task, context, extra, schema, preferred=preferred)
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
        # 大模型调用账本：真实调用（成功/降级/被拦截）都留一条，规则引擎降级不记
        meta = meta or {}
        if meta.get("engine") in ("hermes", "degraded") or meta.get("llmBlocked"):
            a = self.agents.get(agent_id) or {}
            FEED.llm_call(self.db, self.emit, trace, agent_id, a.get("name") or agent_id,
                          meta, task=str(a.get("task") or "")[:60])
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
        return approvals._patrol_note(self, result, facts)

    def _patrol_facts(self):
        return approvals._patrol_facts(self)

    # ---------- 业务入口 ----------
    def _sweep_stale(self):
        return approvals._sweep_stale(self)

    def patrol(self):
        return approvals.patrol(self)

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

    def create_directive(self, text, source="chat", scope="global", trace="",
                         project="", extra=None):
        """C4：老板每次下指令落一条不可变记录（原文不改写），流水线只引用 directive_id。

        md→db 的顺序由本方法保证：先落库再回指 trace；写库失败记 error 但不阻断业务流程
        （指令留痕是审计能力，不能因为库抖一下就让老板的指令丢失执行）。
        """
        text = str(text or "").strip()
        if not text:
            return ""
        d = {"id": uid("dir"), "traceId": trace or "", "text": text[:4000],
             "source": source, "scope": scope, "projectId": project or "",
             "confirmedBy": str(getattr(self, "owner_name", "") or "owner"),
             "createdAt": iso_now(),
             "raw": json.dumps(extra or {}, ensure_ascii=False)}
        with self.lock:
            self.directives.insert(0, d)
            del self.directives[200:]
        if self.db:
            try:
                self.db.upsert_directive(d)
            except Exception as e:
                log_event("create_directive.persist", e, level="error", id=d["id"])
        return d["id"]

    def directives_view(self, limit=100, trace=""):
        """指令列表：优先内存（最新 200 条），带 trace 过滤时回落到过程库。"""
        limit = max(1, min(500, int(limit or 100)))
        if trace:
            with self.lock:
                hit = [d for d in self.directives if d.get("traceId") == trace]
            if hit:
                return hit[:limit]
        if self.db:
            try:
                return self.db.directives(limit=limit, trace=trace)
            except Exception as e:
                log_event("directives_view.db", e, level="warn", trace=trace)
        with self.lock:
            return list(self.directives[:limit])

    def inject_hotspot(self, title=None, tags=None, score=None, force=False):
        """发起每日选题。

        显式传 title：老板指定方向（作为 CCO 选题的参考，不再按项目拆解热点）；
        未传 title：完全由知识库驱动——CCO 从内化认知中提炼最佳选题，不读热点榜。
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
        basis, source, kind = "", "手动注入", "manual"
        if title and title.strip():
            title = title.strip()
            if score is None:
                basis = "老板指定方向（未提供热度）"
        else:
            # 每日选题改为知识库内化：不再从热点榜分别匹配三个项目
            kb = self._kb_brief(limit=1)
            if not kb["count"]:
                raise ValueError("知识库暂无可用于选题的条目：请先在 OPC/知识库 归档内化认知")
            title, kind = "知识库内化选题", "knowledge"
            source = f"知识库 {kb['count']} 条"
            score = 0.0
            basis = "CCO 从知识库提炼最佳选题"
            if not tags:
                tags = list(kb["categories"].keys())[:4]
        if score is None:
            score = 0.8
        if isinstance(tags, str):
            tags = re.split(r"[/、,，]", tags)
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()] or ["知识内化"]
        trace = uid("tr")
        _assign_task_name(trace, title, self.db)   # 建单即得中文编号：第 012 号 · 《标题》
        hotspot = {"id": uid("hs"), "traceId": trace, "title": title, "source": source,
                   "tags": tags, "score": score, "fit": {}, "at": iso_now(), "kind": kind}
        if basis:
            hotspot["scoreBasis"] = basis
        key = _hotkey(title)
        with self.lock:
            if kind == "knowledge":
                # 知识库选题不是热点，不写进热点榜，只作为本轮的选题来源快照
                self.last_hotspot = hotspot
            else:
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
        _assign_task_name(trace, title, self.db)   # 建单即得中文编号
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
            # 同上：并发灵感开工时锁内再核一次，避免两条流水线同时起跑
            running = [t for t, d in self.traces.items()
                       if d.get("status") == "running" and t != trace]
            if running:
                raise BusyError(running[-1])
            self.hotspots.append(hotspot)
            self.last_hotspot = hotspot
            self.stats["traces"] += 1
            self.traces[trace] = {"traceId": trace, "hotspot": hotspot, "startedAt": iso_now(),
                                  "status": "running", "engine": MODE, "source": "idea"}
        # C4：灵感 + 老板要求完整留痕（原文拼接，不改写）
        did = self.create_directive(
            f"{title}\n{requirements}".strip() if requirements else title,
            source="idea", scope="trace", trace=trace, project=pid,
            extra={"ideaId": str(idea.get("id") or ""), "projectId": pid,
                   "requirements": requirements})
        if did:
            with self.lock:
                self.traces.setdefault(trace, {})["directiveId"] = did
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

    def _idea_project(self, idea, project=""):
        """灵感归属项目：显式指定 > 灵感标签/来源里认得出的项目 > 默认同心圆（正式宣讲调性）。"""
        pid = str(project or "").strip().lower()
        if pid in self.projects:
            return pid
        hay = " ".join([str(idea.get("title") or ""), str(idea.get("summary") or ""),
                        str(idea.get("origin") or ""), str(idea.get("source") or ""),
                        " ".join(str(t) for t in (idea.get("tags") or []))])
        for key, val in (("贾文人", "wenren"), ("文人莫克", "wenren"), ("狗大人", "gou"), ("同心圆", "tongxinyuan")):
            if key in hay:
                return val
        return "tongxinyuan"

    def run_pipeline(self, trace, hotspot, start=None, state=None):
        return pipeline.run_pipeline(self, trace, hotspot, start, state)

    # ---------- 选题流程护栏 ----------
    PIDS = ("wenren", "gou", "tongxinyuan")

    def _deg(self, aid, what):
        return pipeline_kit._deg(self, aid, what)

    def _kb_brief(self, limit=40):
        """选题上下文：知识库近期条目 + 分类/项目分布（CCO 内化提炼的唯一素材来源）。"""
        with self.lock:
            entries = [dict(k) for k in self.knowledge]
        entries = [k for k in entries if str(k.get("title") or "").strip()]
        entries.sort(key=lambda k: str(k.get("at") or ""), reverse=True)
        cats, projs = {}, {}
        for k in entries:
            cat = str(k.get("category") or "未分类")
            cats[cat] = cats.get(cat, 0) + 1
            pid = str(k.get("projectId") or "all")
            projs[pid] = projs.get(pid, 0) + 1
        picked = [{"title": str(k.get("title"))[:60],
                   "category": str(k.get("category") or ""),
                   "projectId": str(k.get("projectId") or ""),
                   "tags": [str(x)[:20] for x in (k.get("tags") or [])][:4],
                   "at": str(k.get("at") or "")[:10],
                   "summary": str(k.get("summary") or k.get("body") or "")[:160]}
                  for k in entries[:limit]]
        return {"count": len(entries), "categories": cats, "projects": projs, "entries": picked}

    def _coerce_cco_topics(self, data, kb):
        return pipeline_kit._coerce_cco_topics(self, data, kb)

    def _coerce_plan(self, data, topic, idx):
        return pipeline_kit._coerce_plan(self, data, topic, idx)

    def _review_map(self, data, ids):
        return pipeline_kit._review_map(self, data, ids)

    def _sample_wave(self, trace, ids, items):
        return pipeline_kit._sample_wave(self, trace, ids, items)

    def _production_checks(self, items):
        return pipeline_kit._production_checks(self, items)

    def _publish_checks(self, items, passed_items, review_history):
        return pipeline_kit._publish_checks(self, items, passed_items, review_history)

    def _stage_timeout(self, c):
        return pipeline._stage_timeout(self, c)

    def _pipeline(self, trace, hotspot, start=None, state=None):
        return pipeline._pipeline(self, trace, hotspot, start, state)

    # ---------- 制片工厂（车间监控/控制） ----------
    def _workshop_start(self, trace, st):
        return pipeline_kit._workshop_start(self, trace, st)

    def _workshop_end(self, trace, st, status, ctx=None):
        return pipeline_kit._workshop_end(self, trace, st, status, ctx)

    def factory_view(self):
        return factory.factory_view(self)

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
        return pipeline._run_checkpoint(self, trace, st, pos, c)

    def _write_stage_checkpoint(self, trace, st, pos, c):
        return pipeline._write_stage_checkpoint(self, trace, st, pos, c)

    def _stage_topic_meeting(self, c):
        return pipeline._stage_topic_meeting(self, c)


    def _stage_boss_report(self, c):
        return pipeline._stage_boss_report(self, c)


    def _stage_create_items(self, c):
        return pipeline._stage_create_items(self, c)

    @staticmethod
    @staticmethod
    def _content_id(trace, pid, idx, title):
        return pipeline_kit._content_id(trace, pid, idx, title)


    def _open_approval(self, c, draft):
        return approvals._open_approval(self, c, draft)

    def _stage_production_approval(self, c):
        return pipeline._stage_production_approval(self, c)


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
        return pipeline._stage_assessment(self, c)


    def _stage_copy(self, c):
        return pipeline._stage_copy(self, c)

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
        # 素材投料：进工厂（render/成片段）前把补素材产出入 OpenMontage 项目库（kb_materials_sky 规约）
        try:
            import asset_feed
            fed = asset_feed.feed(item, trace)
            item["materials"]["feed"] = {"slug": fed.get("slug"), "dest": fed.get("dest"),
                                         "copied": fed.get("copied", []),
                                         "reason": fed.get("reason", "")}
            self.items_emit(trace, item)
            print(f"[materials] {item['id']} 已投料 OpenMontage："
                  f"{fed.get('slug') or '_inbox'} ← {len(fed.get('copied', []))} 张", flush=True)
        except Exception as e:          # noqa: BLE001 投料失败不阻断补素材链路
            print(f"[materials] {item['id']} OpenMontage 投料失败：{e}", flush=True)
        self.send("result", f"cw_{pid}", f"pm_{pid}", trace,
                  {"itemId": item["id"], "补素材结论":
                   f"素材库命中 {len(item['materials']['library'])}，"
                   f"ComfyUI 补齐 {n_gen}/{len(missing[:3])}，TTS 音频 {n_voice} 段，"
                   f"已投料 OpenMontage({(item['materials'].get('feed') or {}).get('slug') or '_inbox'})"},
                  project=pid)


    def _stage_sample(self, c):
        return pipeline._stage_sample(self, c)


    def _stage_sample_reel(self, c):
        return pipeline._stage_sample_reel(self, c)


    def _stage_render(self, c):
        return factory._stage_render(self, c)

    def _render_one(self, c, idx, total, iid, item):
        return factory._render_one(self, c, idx, total, iid, item)

    # 类常量真源在 factory.py，这里只做别名，外部按 Kernel.STAGE_BUTTONS 取值不受影响
    STAGE_BUTTONS = factory.STAGE_BUTTONS
    TOOLBOX_SUB = factory.TOOLBOX_SUB
    _RESEND_SKIP = factory._RESEND_SKIP
    _RESEND_AUDIO = factory._RESEND_AUDIO

    def resend_render(self, trace_id, action='assets', shots=None, stage='render', prompts=None, project=''):
        return factory.resend_render(self, trace_id, action, shots, stage, prompts, project)

    def _resend_assets_worker(self, trace_id, reqs):
        return factory._resend_assets_worker(self, trace_id, reqs)

    def _resend_stage(self, trace_id, stage):
        return factory._resend_stage(self, trace_id, stage)


    def _media(self):
        return factory._media(self)

    def _shot_item(self, trace_id, item_id=''):
        return factory._shot_item(self, trace_id, item_id)

    def _shots_of(self, item):
        return factory._shots_of(self, item)

    def _shot_out_dir(self, trace_id, project_id, item_id, kind):
        return factory._shot_out_dir(self, trace_id, project_id, item_id, kind)

    def factory_channels(self):
        return factory.factory_channels(self)

    def voice_preview(self, channel, voice, text=""):
        return factory.voice_preview(self, channel, voice, text)

    def voice_sample(self, channel, voice):
        return factory.voice_sample(self, channel, voice)

    def workshop_tts_sync(self, channel, voice, text, params=None):
        return factory.workshop_tts_sync(self, channel, voice, text, params)

    def workshop_tts_confirm(self, wid):
        return factory.workshop_tts_confirm(self, wid)

    def workshop_bgm_list(self):
        return factory.workshop_bgm_list(self)

    def workshop_bgm_select(self, bid, volume=0.16):
        return factory.workshop_bgm_select(self, bid, volume)

    def workshop_mix_sync(self, voice_id, bgm_id, volume=None):
        return factory.workshop_mix_sync(self, voice_id, bgm_id, volume)

    def workshop_mix_confirm(self, wid):
        return factory.workshop_mix_confirm(self, wid)

    def workshop_state(self):
        return factory.workshop_state(self)

    def workshop_audio_path(self, wid):
        return factory.workshop_audio_path(self, wid)


    def toolbox_save_frame(self, raw):
        return factory.toolbox_save_frame(self, raw)

    def toolbox_voices(self):
        return factory.toolbox_voices(self)

    def toolbox_clone_voice(self, raw, name='', text='', owner=''):
        return factory.toolbox_clone_voice(self, raw, name, text, owner)

    def toolbox_tune_voice(self, vid, direction='', raw=b'', text=''):
        return factory.toolbox_tune_voice(self, vid, direction, raw, text)

    def toolbox_undo_voice(self, vid):
        return factory.toolbox_undo_voice(self, vid)

    def toolbox_voice_profile(self, vid):
        return factory.toolbox_voice_profile(self, vid)

    def toolbox_delete_voice(self, vid):
        return factory.toolbox_delete_voice(self, vid)

    def toolbox_voice_ref(self, vid):
        return factory.toolbox_voice_ref(self, vid)

    def toolbox_channels(self):
        return factory.toolbox_channels(self)

    def toolbox_run(self, kind, channels, prompt='', line='', sec=0.0, params=None, frame=''):
        return factory.toolbox_run(self, kind, channels, prompt, line, sec, params, frame)

    def toolbox_jobs(self, limit=40):
        return factory.toolbox_jobs(self, limit)

    # ---- 远程视频作业：MCP key / 会话均可调用，底层仍是同一条流水线
    def video_submit(self, actor, brief, requirements='', project='',
                     source_refs=None, idempotency_key=''):
        return video_jobs.submit(self, actor, brief, requirements, project,
                                 source_refs, idempotency_key)

    def video_recent(self, actor, limit=30):
        return video_jobs.recent(self, actor, limit)

    def video_status(self, job_id, actor=''):
        return video_jobs.status(self, job_id, actor)

    def video_approve(self, job_id, actor, decision='approve'):
        return video_jobs.approve(self, job_id, actor, decision)

    def video_cancel(self, job_id, actor, reason=''):
        return video_jobs.cancel(self, job_id, actor, reason)

    def video_result(self, job_id, actor=''):
        return video_jobs.result(self, job_id, actor)

    def video_media_file(self, job_id):
        return video_jobs.media_file(self, job_id)

    def forget_output(self, dest):
        return factory.forget_output(self, dest)

    def factory_shots(self, trace_id, item_id=''):
        return factory.factory_shots(self, trace_id, item_id)

    def factory_shot_save(self, trace_id, item_id, shot_no, fields):
        return factory.factory_shot_save(self, trace_id, item_id, shot_no, fields)

    def factory_run(self, trace_id, item_id, kind, channels, shots=None, params=None):
        return factory.factory_run(self, trace_id, item_id, kind, channels, shots, params)

    def factory_jobs(self, trace_id, item_id=''):
        return factory.factory_jobs(self, trace_id, item_id)

    def factory_cancel(self, job_ids):
        return factory.factory_cancel(self, job_ids)

    def factory_retry(self, job_ids):
        return factory.factory_retry(self, job_ids)

    # ---- 三环节手工多次启动：排队串行下发，不阻塞 HTTP
    def _stage_lane(self):
        return factory._stage_lane(self)

    def factory_stage_start(self, trace_id, stage):
        return factory.factory_stage_start(self, trace_id, stage)

    def _stage_queue_put(self, rec):
        return factory._stage_queue_put(self, rec)

    def _stage_lane_loop(self):
        return factory._stage_lane_loop(self)

    def _stage_lane_run(self, rec):
        return factory._stage_lane_run(self, rec)

    def factory_stage_runs(self, trace_id):
        return factory.factory_stage_runs(self, trace_id)

    def _stage_sample_review(self, c):
        return pipeline._stage_sample_review(self, c)


    def _stage_final(self, c):
        return pipeline._stage_final(self, c)


    def _stage_review(self, c):
        return pipeline._stage_review(self, c)


    def _stage_publish_approval(self, c):
        return pipeline._stage_publish_approval(self, c)


    def _stage_release(self, c):
        return pipeline._stage_release(self, c)


    def _stage_archive(self, c):
        return pipeline._stage_archive(self, c)


    def _stage_report(self, c):
        return pipeline._stage_report(self, c)


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
        return ops._asset_owned(self, item, url)

    def _tech_gate(self, item):
        return pipeline_kit._tech_gate(self, item)

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
        return ops._schedule_export(self, trace, item)

    def export_trace_docs(self, trace):
        return ops.export_trace_docs(self, trace)

    # ---------- 审批 ----------
    def decide_approval(self, approval_id, decision, decided_by=''):
        return approvals.decide_approval(self, approval_id, decision, decided_by)

    def _resume_if_interrupted(self, trace):
        return approvals._resume_if_interrupted(self, trace)

    def _approval_from_db(self, approval_id):
        return approvals._approval_from_db(self, approval_id)

    # ---------- 人设串味注入 ----------
    def persona_injection(self):
        trace = uid("tr")
        item = {"id": uid("c"), "projectId": "gou", "traceId": trace, "stage": "review",
                "title": "（测试）把贾文人的稿子塞给狗大人", "owner": "cw_gou",
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
                "conclusion": "检测到狗大人稿件使用贾文人设与素材，已驳回返工", "evidence": reasons,
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
            # 共享 vault 的调研报告/知识条目 → 老板个人空间（APP 与私有目录可见），
            # 不放进用户内核：个人空间是 owner 的对外窗口，员工目录不广播公司文档
            if DOCMIRROR is not None and self.is_owner:
                try:
                    DOCMIRROR.mirror()      # 内部按 OPC_DOC_MIRROR_INTERVAL 节流
                except Exception as e:
                    print(f"[mirror] 文档镜像失败：{type(e).__name__}: {e}", flush=True)

    @staticmethod
    @staticmethod
    @staticmethod
    def _kb_norm(item):
        return ops._kb_norm(item)

    def _sync_knowledge_from_vault(self):
        return ops._sync_knowledge_from_vault(self)

    def _sync_config_from_vault(self):
        return ops._sync_config_from_vault(self)

    def _sync_hotspots_from_vault(self, force=False):
        return ops._sync_hotspots_from_vault(self, force)

    def _hotspot_view(self, limit=5):
        return kb_views._hotspot_view(self, limit)

    def _page_params(self, query='', default_size=20, max_size=100):
        return kb_views._page_params(self, query, default_size, max_size)

    def _knowledge_all(self):
        return kb_views._knowledge_all(self)

    def knowledge_categories(self):
        return kb_views.knowledge_categories(self)

    def knowledge_page(self, query=''):
        return kb_views.knowledge_page(self, query)

    def hotspot_page(self, query=''):
        return kb_views.hotspot_page(self, query)

    def knowledge_detail(self, kid):
        return kb_views.knowledge_detail(self, kid)

    def hotspot_detail(self, hid):
        return kb_views.hotspot_detail(self, hid)

    # ---------- 知识归档 ----------
    def archive_knowledge(self, trace, entries, stage_label='选题定稿'):
        return ops.archive_knowledge(self, trace, entries, stage_label)

    def _backfill_knowledge(self):
        return kb_views._backfill_knowledge(self)

    # ---------- 知识图谱关系边 ----------
    def rebuild_relations(self):
        return kb_views.rebuild_relations(self)

    def knowledge_relations(self):
        return kb_views.knowledge_relations(self)

    def recall_kb(self, text, limit=None):
        return kb_views.recall_kb(self, text, limit)

    def kb_brief(self, text, limit=None):
        return kb_views.kb_brief(self, text, limit)

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

_SAVE_LOCK = threading.Lock()     # runtime 快照串行写（内核池已迁至 pool.py）
