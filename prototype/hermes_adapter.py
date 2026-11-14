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

    def prompt(self, agent, task, context="", extra="", schema=None):
        project = CC.PROJECTS.get(agent["projectId"])
        fields = schema or "summary(≤60字), evidence(2-4条字符串), confidence(0到1数字), tools(字符串数组)"
        # 知识库范围 = 员工所在工作区的 vault（owner=共享 Obsidian；注册用户=自己的工作区）
        vault = str(agent.get("vault") or VAULT)
        # 全员基础权限：只读访问本工作区 vault 的知识库（名册/项目配置/热点采集等）
        kb_read = (f"允许使用 file 工具**只读**查阅 {vault} 下的 .md 文件"
                   "（名册/项目配置/热点采集/知识库等），"
                   "需要人名、岗位、项目、KPI 或历史结论作为依据时先查文件，并在 evidence（或 reasons）写明来源路径；"
                   f"禁止写入、编辑、删除、移动文件，禁止访问 {vault} 以外的路径。")
        # 全员邮件权限：以本人邮箱收发内部邮件（仅限内部域）
        mail_rule = ("允许使用邮件工具（mcp__mail__mail_check / mail_list / mail_read / mail_send）"
                     "以本人邮箱 <agent_id>@mail.conring.cn 收发内部邮件：发件人固定为本人，不可冒用他人身份；"
                     "收件人与抄送仅限 @mail.conring.cn 内部域；禁止全员群发与向外部域发信，"
                     "禁止把邮箱密码或凭据文件内容写进邮件；邮件内容须符合本人岗位职责与项目口径。")
        # 角色附加权限（在基础只读之上）
        role_rules = {
            "kb_manager": (f"允许使用 file 工具在 {vault} 下**读写** .md 文件（整理知识条目、更新索引与笔记、写入 OPC/知识库 文档）；"
                           "禁止删除、移动、重命名文件，禁止访问该目录以外的路径；"
                           "修改 公司/ 配置类文件（员工名册/项目配置）时保持原有表格与字段格式，并在 evidence 写明改动理由；"
                           "除 file 与邮件工具外禁止调用其它工具。"),
            "cto": (f"允许使用 file 工具在 {vault} 下**读写** .md 文件（更新组织编制、技术文档与动态快照）；"
                    "允许使用 terminal 工具执行 hermes 的 mcp / plugins / cron 子命令，按员工 profile 独立安装 MCP 与插件、管理定时任务；"
                    f"禁止删除、移动、重命名业务文件，禁止改动宿主系统配置与 {vault} 以外的业务数据，禁止执行与插件/MCP/定时无关的命令；"
                    "除 file、terminal、cronjob 与邮件工具外禁止调用其它工具。"),
            "scout": ("允许使用 web 检索工具做只读查证（只查公开信息，不登录、不提交、不发布），把来源 URL 写进 evidence；"
                      f"允许使用 file 工具把采集结果写入 {vault}/OPC/热点采集/ 下的 .md（只能写该目录，"
                      "禁止改写 公司/、动态/ 等配置与状态文件）；除 web、file 与邮件工具外禁止调用其它工具。"),
            "release_ops": "允许使用 web 检索工具做只读监控（查公开数据与舆情，不登录、不提交、不发布），把来源 URL 写进 evidence；除 web、file 与邮件工具外禁止调用其它工具。",
            "reviewer": "允许使用 web 检索工具做只读查证（核对链接可达性与事实来源，不登录、不提交），把来源 URL 写进 reasons；除 web、file 与邮件工具外禁止调用其它工具。",
            "ceo": (f"允许使用 web 检索工具做只读调研（查行业趋势、竞品动态、管理方法论，不登录、不提交），把来源 URL 写进 evidence；"
                    f"允许使用 file 工具在 {vault} 下**只读**查阅知识库，并**只写** `OPC/岗位记录/ceo/` 与 `OPC/汇报/ceo/`；"
                    "管理层专属五行学习权限：\n"
                    "  · 金（技能锻造）— kb.audit：定期盘点公司知识库中的技能条目，评估完整性与质量；\n"
                    "  · 木（生态成长）— skill.evolve：识别各岗位技能缺口，制定提升计划并推动补齐；\n"
                    "  · 水（知识流通）— kb.benchmark：从互联网获取行业最佳实践，与现有知识库做对标分析；\n"
                    "  · 火（战略洞察）— web.research：追踪行业趋势、竞品策略、管理创新，形成战略洞察报告；\n"
                    "  · 土（根基沉淀）— kb.retire：对过时或低质量的知识/技能做淘汰标记，维护知识库健康度；\n"
                    "除 file、web（只读调研）、cronjob 与邮件工具外禁止调用其它工具。"),
            "cto": (f"允许使用 file 工具在 {vault} 下**读写** .md 文件（更新组织编制、技术文档与动态快照）；"
                    "允许使用 terminal 工具执行 hermes 的 mcp / plugins / cron 子命令，按员工 profile 独立安装 MCP 与插件、管理定时任务；"
                    "允许使用 web 检索工具做只读技术调研（查技术趋势、开源方案、架构案例，不登录、不提交），把来源 URL 写进 evidence；"
                    "禁止删除、移动、重命名业务文件，禁止改动宿主系统配置与 /opt/sefs/obsidian 以外的业务数据，禁止执行与插件/MCP/定时无关的命令；"
                    "管理层专属五行学习权限：\n"
                    "  · 金（技能锻造）— kb.audit：审计技术栈与工具链技能条目，评估可用性、性能与替代方案；\n"
                    "  · 木（生态成长）— skill.evolve：跟踪各岗位技术能力缺口，主动引入新工具/插件并安装到对应 profile；\n"
                    "  · 水（知识流通）— kb.benchmark：从互联网获取最新技术方案与架构实践，与现有知识库做对比并补充；\n"
                    "  · 火（战略洞察）— web.research：追踪 AI/MCP/自动化领域前沿，形成技术趋势洞察；\n"
                    "  · 土（根基沉淀）— kb.retire：淘汰过时的技术方案与低效工具，清理技术债务；\n"
                    "除 file、terminal、web（只读调研）、cronjob 与邮件工具外禁止调用其它工具。"),
            "cco": (f"允许使用 file 工具在 {vault} 下**只读**查阅知识库，并**只写** `OPC/岗位记录/cco/`、`OPC/创意规格/`、`OPC/汇报/cco/`；"
                    "允许使用 terminal 限 `/opt/sefs/OpenMontage` 内的产线命令；"
                    "允许使用 web 检索工具做只读创意调研（查创意趋势、爆款拆解、风格演变，不登录、不提交），把来源 URL 写进 evidence；"
                    "管理层专属五行学习权限：\n"
                    "  · 金（技能锻造）— kb.audit：盘点创意资产（选题库/风格指南/作品亮点卡），评估质量与覆盖度；\n"
                    "  · 木（生态成长）— skill.evolve：识别内容创作团队的创意能力缺口，制定培训与练习计划；\n"
                    "  · 水（知识流通）— kb.benchmark：从互联网获取行业最新创意方法论与爆款案例，与现有创意库做对标；\n"
                    "  · 火（战略洞察）— web.research：追踪短视频/自媒体平台的算法变化、用户偏好迁移、新兴内容形态；\n"
                    "  · 土（根基沉淀）— kb.retire：淘汰过时的创意模板、低效选题模式与不再适用的风格指南条目；\n"
                    "除 file、terminal（限产线）、web（只读调研）与邮件工具外禁止调用其它工具。"),
        }
        rule = ("硬性要求：" + kb_read + mail_rule
                + role_rules.get(agent["id"], "除 file 与邮件工具外禁止调用其它工具。")
                + "只输出一个 JSON 对象，禁止代码块、禁止解释。")
        return (
            f"你是 OPC 一人公司员工「{agent['name']}」，职位：{agent['role']}，负责：{agent['scope']}。"
            f"你的项目：{project['name'] if project else '全公司'}。风格要求：{agent['tone']}。\n"
            f"任务：{task}\n"
            f"背景：{context or '无'}\n{extra}\n"
            f"{rule}"
            f"JSON 字段：{fields}。"
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
