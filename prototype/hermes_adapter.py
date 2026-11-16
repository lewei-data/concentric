"""Hermes 调用适配：员工提示词组装、子进程调用、JSON 提取、用量账本、注册表门槛。"""
import concurrent.futures
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, urlparse
import queue

from opc_paths import *  # noqa: F401,F403
from utils import iso_now, _to_int
import company_config as CC
import llm_registry as LREG

class HermesAdapter:
    """把一名员工的活交给真实 Hermes 一次性会话，并强约束 JSON 输出。"""

    def __init__(self):
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS)
        self.calls = 0
        self.failures = 0
        self.latencies = []
        self.lock = threading.Lock()
        # 每通道并发闸门：channel_id -> (Semaphore, 当前并发数, 排队数, 上限)
        # 通道上限从 llm_models.max_concurrency 读，动态更新（不重启生效）；0=不限。
        self.channel_gates = {}       # id -> {"sem": Semaphore|None, "active": int, "waiting": int, "limit": int}
        self.gate_lock = threading.Lock()
        # 429/限流后推迟的重跑队列：(retry_at, agent_id, task, context, extra, schema, channels_tried)
        self.deferred = []
        self.deferred_lock = threading.Lock()
        self.rate_limit_count = 0
        self.queue_wait_count = 0
        self.sync_retry_count = 0   # 同步等待冷却后内联重跑的次数（原调用者拿到结果）
        # 后台定时排空推迟队列：通道冷却结束/排队下降后自动重跑（每 10 秒一轮）
        threading.Thread(target=self._deferred_loop, daemon=True,
                         name="llm-deferred-drainer").start()

    def _deferred_loop(self):
        while True:
            try:
                if self.pending_count():
                    self.drain_deferred(self._lookup_agent)
            except Exception as e:
                print(f"[hermes] 推迟队列处理异常：{type(e).__name__}: {e}", flush=True)
            time.sleep(10)

    @staticmethod
    def _lookup_agent(aid):
        """从公司名册重建最小 agent 字典（供推迟任务重跑；不依赖某个内核实例）。"""
        for row in CC.AGENT_SPEC:
            if row[0] == aid:
                a_id, name, role, pid, tools, scope = row
                return {"id": a_id, "name": name, "role": role, "projectId": pid,
                        "scope": scope, "tools": tools, "tone": ""}
        return None

    def available(self):
        return MODE == "hermes" and os.path.exists(CLI)

    # ---------- 通道并发闸门 ----------
    def _gate(self, channel):
        """取/建通道闸门；通道上限变更时重建 semaphore（老调用持有旧 sem 也会自然释放）。"""
        cid = str((channel or {}).get("id") or MODEL)
        limit = max(0, _to_int((channel or {}).get("max_concurrency"), 2) or 2)
        wait_ms = max(0, _to_int((channel or {}).get("queue_wait_ms"), 30000))
        with self.gate_lock:
            g = self.channel_gates.get(cid)
            if g is None or g["limit"] != limit:
                g = {"sem": threading.BoundedSemaphore(limit) if limit > 0 else None,
                     "active": 0, "waiting": 0, "limit": limit}
                self.channel_gates[cid] = g
            return g, wait_ms

    def gate_snapshot(self):
        """运维视图：每通道当前并发/排队/上限，供前端展示与告警。"""
        with self.gate_lock:
            return {cid: {"limit": g["limit"], "active": g["active"], "waiting": g["waiting"]}
                    for cid, g in self.channel_gates.items()}

    def _is_rate_limited(self, text):
        """从子进程输出里识别 429/限流（hermes 走子进程，拿不到 RateLimitError 类型）。"""
        if not text:
            return False
        low = str(text).lower()
        patterns = ("429", "rate limit", "rate_limit", "too many requests", "throttled",
                    "requests per minute", "tokens per minute", "too many concurrent",
                    "allocated quota exceeded", "quota exceeded", "go usage limit", "额度耗尽")
        return any(p in low for p in patterns)

    def _defer(self, retry_at, agent, task, context, extra, schema, tried):
        """把限流失败的调用放进重跑队列，等冷却结束/排队下降后由 drain_deferred 拉起。"""
        with self.deferred_lock:
            self.deferred.append((retry_at, agent["id"], task, context, extra, schema, list(tried)))

    def drain_deferred(self, agent_lookup):
        """后台定时调用：把到期的推迟任务重新提交到线程池（每次最多 8 条，避免瞬间冲击）。
        agent_lookup(id) -> agent dict；取不到就丢弃（员工已移除）。"""
        now = time.time()
        ready = []
        with self.deferred_lock:
            keep = []
            for item in self.deferred:
                (ready if item[0] <= now else keep).append(item)
            self.deferred = keep
        for retry_at, aid, task, context, extra, schema, tried in ready[:8]:
            agent = agent_lookup(aid)
            if not agent:
                continue
            self.pool.submit(self.ask, agent, task, context, extra, schema, tried)
        # 未取走的放回队首，下轮再试
        if len(ready) > 8:
            with self.deferred_lock:
                self.deferred = ready[8:] + self.deferred
        return len(ready[:8])

    def pending_count(self):
        with self.deferred_lock:
            return len(self.deferred)

# ---------- 提示词 ----------
    # 原则：身份/纪律/记录义务由系统提示词（profiles/<id>/SOUL.md）承载，这里只补
    # 「本次任务的增量边界」。同一句话不写两遍——重复既稀释注意力，又直接烧 token。
    # 曾经每人 700~1600 字的权限手册里，一半是 kb/邮件通用规则，一半是「概念工具→实际
    # 操作」两层命名（kb.write → write_file），模型要在两套词汇间对齐，纯负担。
    def _boundary(self, agent, vault):
        """按岗位给最小边界：可写路径 + 可用工具 + 红线，一人一段（约 200-400 字）。"""
        aid = str(agent.get("id") or "")
        head = (f"边界：{vault} 默认只读，只有下面点名的路径可写；一律不删、不改名、不出 vault；"
                f"邮件仅限 @mail.conring.cn 内部域，以本人 {aid}@mail.conring.cn 收发，"
                f"禁冒用身份、禁群发外发、禁写凭据；")
        # 与系统提示词共享的收尾：告诉模型图谱从哪来、不够时怎么补，避免全库瞎搜
        tail = ("背景里的【知识图谱命中】是公司已有结论；不够时按出处路径直接 read_file 原文，"
                "不要全库遍历。\n")
        body = {
            "kb_manager": (
                f"可读写 {vault}/OPC/知识库 与 OPC/汇报（write_file/patch），新增条目同步 00-索引.md；"
                "可用 terminal 只读盘点（sqlite3 -readonly、ls/du/find/wc）与 asset_audit.py 产出的 "
                "OPC/汇报/资产盘点/最新.md；数字必须可追溯到表名/文件/口径，禁写业务库、禁改流水线状态；"
                "深挖单条：python3 /opt/sefs/opc-workbench/prototype/kb_graph.py show <id>；"
                "除 file/terminal(只读)/cronjob/邮件外不要调用其它工具。"),
            "scout": (
                f"可只读查证公开信息（不登录、不提交、不发布），URL 写入 evidence；"
                f"可写 {vault}/OPC/热点采集/*.md，禁改 公司/ 与 动态/；"
                "除 web/file/邮件外不要调用其它工具。"),
            "release_ops": (
                "可只读监控公开数据与舆情（不登录、不提交），URL 写入 evidence；"
                "除 web/file/邮件外不要调用其它工具。"),
            "reviewer": (
                f"可读写 {vault} 下 .md，审核报告写项目目录；terminal 限 /opt/sefs/OpenMontage 产线命令"
                "（ffprobe/ffmpeg/python）取证；可用 media_asr/media_tts/media_job_status；web 只读核实；"
                "红线：字幕竖屏 y>1500 不合格、横屏 y>980 不合格；图片单边 <2048px 不合格；"
                "字幕与旁白误差 >100ms 不合格；交付片必须 yuv420p+bt709；"
                "不改叙事与选镜头、不删素材、不覆盖作者原工程；"
                "除 file/terminal/media_*/web/邮件外不要调用其它工具。"),
            "ceo": (
                f"可读写 {vault} 下 .md；web/browser 只读调研（不登录不提交），URL 写入 evidence；"
                "cronjob 设定期学习盘点；五行学习落到文件：金=知识库审计报告、木=技能缺口报告、"
                "水=对标分析、火=趋势/风险/OKR、土=归档过时条目+维护索引；"
                "除 file/web/browser/cronjob/邮件外不要调用其它工具。"),
            "cto": (
                f"可读写 {vault} 下 .md；terminal 可跑 hermes mcp/plugins/cron 与诊断"
                "（ss/ps/top/df/free/curl/systemctl）；web 只读技术调研；cronjob 做巡检；"
                f"禁改宿主配置与 {vault} 以外的业务数据，禁删业务文件；"
                "除 file/terminal/web/cronjob/邮件外不要调用其它工具。"),
            "secretary": (
                f"可读写 {vault} 下 .md（汇报与记录写到 OPC/汇报/secretary、OPC/岗位记录/secretary）；"
                "可用 browser_exec 做只读网页操控（查资料、截图、填表；不提交支付、不发布、不登录敏感账号）；"
                "可用 mail 收发与 cronjob 设提醒；"
                "除 file/browser/cronjob/邮件外不要调用其它工具。"),
            "cco": (
                f"可读写 {vault} 下 .md；terminal 限 /opt/sefs/OpenMontage 产线命令；"
                "web 只读创意调研（URL 写入 evidence）；cronjob 做定期学习；"
                "五行：金=创意规格评估+风格指南、木=能力升级计划、水=行业对标、"
                "火=趋势与受众洞察、土=归档+方法论沉淀；"
                "除 file/terminal/web/cronjob/邮件外不要调用其它工具。"),
        }
        if aid in body:
            return head + body[aid] + tail
        # 三个产线岗位族：同一套流程，只差交付物与红线
        family = {
            "pm": (
                f"可读写 {vault} 下 .md；terminal 限产线命令（make/npm/npx/node/ffmpeg/python/piper）；"
                "交付 projects/<slug>/artifacts/script.json（total_duration_seconds、voice_performance、"
                "sections[]）、artifacts/scene_plan.json（style_playbook、scenes[] 五段式）、"
                "STORYBOARD.md（每帧画面/音效/旁白/转场/特效/节奏锚点）；"
                "禁改 render_runtime、禁在 provider_text 写 SSML、不替配音合成音轨、不替剪辑渲染；"
                "除 file/terminal/邮件外不要调用其它工具。"),
            "cw": (
                f"可读写 {vault} 下 .md；terminal 限产线命令；"
                "交付 projects/<slug>/assets/subtitles/captions.json（style、position、words、sentences），"
                "时间戳只能来自 media_asr 词级转写；字幕竖屏安全区 y=1200–1500px，禁 y>1500；"
                "禁改旁白文字、禁编造时间戳、不替剪辑渲染；"
                "除 file/terminal/media_asr/邮件外不要调用其它工具。"),
            "vd": (
                f"可读写 {vault} 下 .md；terminal 限产线命令；"
                "ComfyUI 文生图（192.168.21.157:8188/prompt）写 projects/<slug>/assets/images|videos；"
                "收到分镜后 5 分钟内给素材清单/依赖/估时；图片 ≥2048px、音画同步 <100ms；"
                "禁改旁白与音轨、禁用低清素材、不跳过细节自检；"
                "除 file/terminal/邮件外不要调用其它工具。"),
        }
        return head + family.get(aid.split("_")[0],
                                "除 file 与邮件工具外不要调用其它工具。") + tail

    def prompt(self, agent, task, context="", extra="", schema=None):
        project = CC.PROJECTS.get(agent["projectId"])
        fields = schema or "summary(≤60字), evidence(2-4条字符串), confidence(0到1数字), tools(字符串数组)"
        vault = str(agent.get("vault") or VAULT)
        return (
            f"你是 OPC 员工「{agent['name']}」，职位：{agent['role']}，负责：{agent['scope']}；"
            f"项目：{project['name'] if project else '全公司'}；风格：{agent['tone']}。\n"
            f"任务：{task}\n"
            f"背景：{context or '无'}\n{extra}\n"
            f"{self._boundary(agent, vault)}"
            f"只输出一个 JSON 对象（不要代码块、不要解释），字段：{fields}。"
        )

    def ask(self, agent, task, context="", extra="", schema=None, _tried=None):
        """调用一次大模型：按通道并发闸门排队，429 熔断换通道；全部限流时有界同步等待后内联重试。

        - 并发满：按通道上限排队等待 queue_wait_ms，超时换其它通道；
        - 429/额度：通道熔断冷却并自动切换；在同步预算内等待冷却结束后内联重试，
          原调用者能直接拿到成功结果（这是「排队下降后自动重跑」的主路径）；
        - 超过同步预算（OPC_LLM_SYNC_WAIT_SEC，默认 60s）才转后台 deferred 兜底，
          后台在冷却结束/并发下降后自动重跑（极端情况，结果不再回传原调用者）。
        _tried 为重跑时已试过的通道 id（内部参数，外部不要传）。
        """
        if not self.available():
            return None, {"engine": "rule", "latencyMs": 0, "profile": agent["id"], "usage": {}}
        st = LREG._llm_store()
        tried = set(_tried or [])
        t0 = time.time()
        try:
            max_wait = max(0.0, float(os.environ.get("OPC_LLM_SYNC_WAIT_SEC", "60")))
        except (TypeError, ValueError):
            max_wait = 60.0
        waited = 0.0
        reason = ""
        p = self.prompt(agent, task, context, extra, schema)

        while True:
            retryable = False       # 本轮是否遇到可恢复失败（429 / 并发排队超时）
            # 最多轮 3 个不同通道：拿到可用通道 → 过并发闸门 → 调用；429 则熔断并换下一个
            for round_no in range(3):
                channel, gate_reason = (st.llm_pick() if st else (None, ""))
                if channel is None:
                    reason = gate_reason or "通道全部限流"
                    break
                cid = str(channel.get("id") or MODEL)
                if cid in tried:
                    # llm_pick 又挑到试过的通道：临时冷却它，强制下一轮换人
                    if st:
                        st.llm_cooldown(cid, 30, "本轮已试过，换通道")
                    continue
                tried.add(cid)
                gate, wait_ms = self._gate(channel)
                # 并发闸门：按通道上限排队等待；等不到（队列满）则冷却一小会儿并换其它通道
                if gate["sem"] is not None:
                    with self.gate_lock:
                        gate["waiting"] += 1
                    try:
                        got = gate["sem"].acquire(timeout=wait_ms / 1000.0)
                    finally:
                        with self.gate_lock:
                            gate["waiting"] -= 1
                    if not got:
                        retryable = True
                        with self.lock:
                            self.queue_wait_count += 1
                        reason = f"通道 {cid} 并发已满，排队 {wait_ms}ms 超时"
                        if st:
                            st.llm_cooldown(cid, 20, reason)
                        continue
                with self.gate_lock:
                    gate["active"] += 1
                try:
                    result = self._invoke(channel, agent, p, round_no, t0)
                    if result["data"] is not None:
                        return result["data"], result["meta"]
                    reason = result["reason"]
                    if result.get("rate_limited"):
                        retryable = True
                        with self.lock:
                            self.rate_limit_count += 1
                        backoff = self._backoff(round_no, result.get("retry_after"))
                        if st:
                            st.llm_cooldown(cid, backoff, reason)
                        continue
                    if result.get("timeout"):
                        break
                finally:
                    with self.gate_lock:
                        gate["active"] -= 1
                    if gate["sem"] is not None:
                        gate["sem"].release()

            # 本轮未拿到结果：判断能否在同步预算内等待冷却结束后内联重试
            retry_at = self._earliest_cooldown(st)
            wait_needed = max(0.0, retry_at - time.time())
            remaining = max_wait - waited
            if not retryable or remaining <= 0 or wait_needed > remaining:
                self._defer(retry_at, agent, task, context, extra, schema, tried)
                with self.lock:
                    self.failures += 1
                print(f"[hermes] {agent['id']} 全通道失败（{reason}），已推迟到 {retry_at:.0f} 重跑",
                      flush=True)
                return None, {"engine": "degraded", "reason": reason, "deferred": True,
                              "retryAt": retry_at, "profile": agent["id"],
                              "latencyMs": int((time.time() - t0) * 1000), "usage": {}}
            # 可恢复失败且预算够：同步等到最早冷却结束，清空已试列表内联重试，结果回传原调用者
            sleep_s = min(max(wait_needed, 1.0), remaining)
            time.sleep(sleep_s)
            waited += sleep_s
            with self.lock:
                self.sync_retry_count += 1
            tried = set()

    def _earliest_cooldown(self, st):
        """返回最早通道冷却结束时间；无熔断则 15 秒后再试（避免立即打爆）。"""
        now = time.time()
        earliest = None
        if st:
            try:
                for r in st._rows("SELECT cooldown_until FROM llm_models WHERE cooldown_until>?"):
                    u = float(r.get("cooldown_until") or 0)
                    if u > now and (earliest is None or u < earliest):
                        earliest = u
            except Exception:
                pass
        return max(earliest or (now + 15), now + 5)

    @staticmethod
    def _backoff(round_no, retry_after=None):
        """退避：优先用服务给的 Retry-After，否则 5s/10s/20s 抖动，上限 120s。"""
        try:
            ra = float(retry_after or 0)
        except (TypeError, ValueError):
            ra = 0
        if 0 < ra <= 600:
            return min(ra, 600)
        return min(5 * (2 ** min(round_no, 4)), 120)

    def _invoke(self, channel, agent, p, round_no, t0):
        """单次子进程调用：返回 {"data","meta","reason","rate_limited","timeout"}。"""
        model_name = channel.get("name") or MODEL
        alias = str(channel.get("alias") or "")
        cid = str(channel.get("id") or MODEL)
        q = p if round_no == 0 else p + "\n只输出 JSON 对象本身，不要多余文字。"
        usage_path = ""
        try:
            fd, usage_path = tempfile.mkstemp(prefix="opc-usage-", suffix=".json")
            os.close(fd)
            env = dict(os.environ)
            env["HERMES_PROFILE"] = agent["id"]
            cmd = [CLI, "-z", q]
            if alias and alias != MODEL:
                cmd += ["--model", alias]
            cmd += ["--usage-file", usage_path]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, env=env)
            raw = (proc.stdout or "").strip()
            combined = f"{raw}\n{proc.stderr or ''}"
            usage = self._read_usage(usage_path)
            if usage.get("totalTokens"):
                LREG._llm_consume(cid, usage.get("totalTokens"))
            limited = self._is_rate_limited(combined) or (
                bool(usage.get("failed")) and "quota" in combined.lower())
            if limited:
                return {"data": None, "meta": {}, "reason": f"限流/429：{combined[-160:]}",
                        "rate_limited": True, "timeout": False,
                        "retry_after": self._retry_after(combined)}
            data = self._extract(raw)
            with self.lock:
                self.calls += 1
            ms = int((time.time() - t0) * 1000)
            if data is not None:
                with self.lock:
                    self.latencies.append(ms)
                return {"data": data,
                        "meta": {"engine": "hermes", "model": model_name, "channel": cid,
                                 "latencyMs": ms,
                                 "attempts": round_no + 1, "profile": agent["id"], "usage": usage},
                        "reason": "", "rate_limited": False, "timeout": False}
            return {"data": None, "meta": {}, "reason": f"非 JSON 输出：{raw[:120]}",
                    "rate_limited": False, "timeout": False}
        except subprocess.TimeoutExpired:
            return {"data": None, "meta": {}, "reason": f"调用超时（>{TIMEOUT}s）",
                    "rate_limited": False, "timeout": True}
        except Exception as exc:
            return {"data": None, "meta": {}, "reason": f"{type(exc).__name__}: {exc}"[:160],
                    "rate_limited": False, "timeout": False}
        finally:
            if usage_path:
                try:
                    os.unlink(usage_path)
                except OSError:
                    pass

    @staticmethod
    def _retry_after(text):
        m = re.search(r"retry[_-]?after[\"':=\s]+([\d.]+)", str(text), re.I)
        return float(m.group(1)) if m else None

    @staticmethod
    def _read_usage(path):
        """解析 hermes --usage-file 的用量账本；缺失/损坏时返回 {}（不阻断业务）。"""
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        merged = raw.get("total_including_auxiliary")
        if isinstance(merged, dict):
            total = _to_int(merged.get("total_tokens"), 0)
            cost = merged.get("estimated_cost_usd")
            calls = _to_int(merged.get("api_calls"), 0)
        else:
            total = _to_int(raw.get("total_tokens"), 0)
            cost = raw.get("estimated_cost_usd")
            calls = _to_int(raw.get("api_calls"), 0)
        return {"totalTokens": total,
                "inputTokens": _to_int(raw.get("input_tokens"), 0),
                "outputTokens": _to_int(raw.get("output_tokens"), 0),
                "costUsd": round(float(cost or 0), 6),
                "apiCalls": calls,
                "model": raw.get("model") or MODEL,
                "provider": raw.get("provider") or "",
                "completed": raw.get("completed"),
                "costStatus": raw.get("cost_status") or "",
                "failed": bool(raw.get("failed"))}

    @staticmethod
    def _extract(raw):
        if not raw:
            return None
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        try:
            return json.loads(raw)
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except Exception:
                return None
        return None

    def stats(self):
        with self.lock:
            avg = int(sum(self.latencies) / len(self.latencies)) if self.latencies else 0
            return {"calls": self.calls, "failures": self.failures, "avgLatencyMs": avg,
                    "mode": MODE, "model": MODEL, "cli": CLI, "workers": WORKERS,
                    "rateLimited": self.rate_limit_count, "queueTimeouts": self.queue_wait_count,
                    "syncRetries": self.sync_retry_count,
                    "deferred": self.pending_count(), "gates": self.gate_snapshot()}

HERMES = HermesAdapter()
