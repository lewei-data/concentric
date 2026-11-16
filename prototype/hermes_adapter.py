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

    def available(self):
        return MODE == "hermes" and os.path.exists(CLI)

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

    def ask(self, agent, task, context="", extra="", schema=None):
        if not self.available():
            return None, {"engine": "rule", "latencyMs": 0, "profile": agent["id"], "usage": {}}
        # 大模型通道选择：按注册表优先级挑可用通道（priority 最小者为默认，停用/额度耗尽
        # 自动故障转移到下一个）；全部不可用时拦截本次调用（CTO 在运维页控制）
        st = LREG._llm_store()
        channel = None
        if st:
            channel, gate_reason = st.llm_pick()
            if channel is None:
                with self.lock:
                    self.failures += 1
                print(f"[hermes] {agent['id']} 调用被拦截：{gate_reason}", flush=True)
                return None, {"engine": "rule", "reason": gate_reason, "llmBlocked": True,
                              "profile": agent["id"], "latencyMs": 0, "usage": {}}
        model_name = (channel or {}).get("name") or MODEL
        alias = str((channel or {}).get("alias") or "")
        p = self.prompt(agent, task, context, extra, schema)
        t0 = time.time()
        reason = ""
        for attempt in (1, 2):
            q = p if attempt == 1 else p + "\n上一次输出不是合法 JSON，这次只输出 JSON 对象本身，不要任何多余文字。"
            usage_path = ""
            try:
                fd, usage_path = tempfile.mkstemp(prefix="opc-usage-", suffix=".json")
                os.close(fd)
                env = dict(os.environ)
                env["HERMES_PROFILE"] = agent["id"]   # 每人一个独立 profile（配置/技能/记忆隔离）
                cmd = [CLI, "-z", q]
                if alias and alias != MODEL:          # 通道带 hermes 别名时切换模型（如本地 qwen38）
                    cmd += ["--model", alias]
                cmd += ["--usage-file", usage_path]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, env=env)
                raw = (proc.stdout or "").strip()
                usage = self._read_usage(usage_path)
                if usage.get("totalTokens"):
                    LREG._llm_consume((channel or {}).get("id") or MODEL,
                                 usage.get("totalTokens"))   # 扣减实际所用通道的额度
                data = self._extract(raw)
                ms = int((time.time() - t0) * 1000)
                with self.lock:
                    self.calls += 1
                if data:
                    with self.lock:
                        self.latencies.append(ms)
                    return data, {"engine": "hermes", "model": model_name,
                                  "channel": (channel or {}).get("id") or "",
                                  "latencyMs": ms,
                                  "attempts": attempt, "profile": agent["id"], "usage": usage}
                reason = f"非 JSON 输出：{raw[:120]}"
            except Exception as exc:
                with self.lock:
                    self.calls += 1
                if isinstance(exc, subprocess.TimeoutExpired):
                    reason = f"调用超时（>{TIMEOUT}s）"
                    break
                reason = f"{type(exc).__name__}: {exc}"[:160]
            finally:
                if usage_path:
                    try:
                        os.unlink(usage_path)
                    except OSError:
                        pass
        with self.lock:
            self.failures += 1
        print(f"[hermes] {agent['id']} 两次尝试均失败：{reason}", flush=True)
        return None, {"engine": "degraded", "reason": reason, "profile": agent["id"],
                      "latencyMs": int((time.time() - t0) * 1000), "usage": {}}

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
                    "mode": MODE, "model": MODEL, "cli": CLI, "workers": WORKERS}

HERMES = HermesAdapter()
