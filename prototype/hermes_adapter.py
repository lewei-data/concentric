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
            "scout": ("允许使用 web 检索工具做只读查证（只查公开信息，不登录、不提交、不发布），把来源 URL 写进 evidence；"
                      f"允许使用 file 工具把采集结果写入 {vault}/OPC/热点采集/ 下的 .md（只能写该目录，"
                      "禁止改写 公司/、动态/ 等配置与状态文件）；除 web、file 与邮件工具外禁止调用其它工具。"),
            "release_ops": "允许使用 web 检索工具做只读监控（查公开数据与舆情，不登录、不提交、不发布），把来源 URL 写进 evidence；除 web、file 与邮件工具外禁止调用其它工具。",
            "reviewer": "允许使用 web 检索工具做只读查证（核对链接可达性与事实来源，不登录、不提交），把来源 URL 写进 reasons；除 web、file 与邮件工具外禁止调用其它工具。",
            "ceo": (f"允许使用 file 工具（read_file/write_file/search_files/patch）在 {vault} 下**读写** .md 文件；"
                    f"允许使用 web 工具（web_search/web_extract）做只读调研，把来源 URL 写进 evidence；"
                    "允许使用 browser_exec 做市场调研与竞品分析（只读，不登录不提交）；"
                    "允许使用 cronjob_manage 设定定期学习与盘点任务；\n"
                    "管理层专属五行学习权限 + 战略管理权限（概念工具→实际操作映射）：\n"
                    "  · 金（技能锻造）— kb.audit → 用 search_files 扫描 {vault}/OPC/知识库/ 全部条目，逐条 read_file 评估质量，输出盘点报告 write_file 到岗位记录；\n"
                    "  · 木（生态成长）— skill.evolve → read_file 查阅各岗位 SKILL.md 与员工名册，识别缺口后 write_file 输出《技能缺口报告》；\n"
                    "  · 水（知识流通）— kb.benchmark → web_search 搜索行业标杆，web_extract 提取关键内容，write_file 输出《对标分析报告》到知识库；\n"
                    "  · 火（战略洞察）— web.research + strategy.plan → web_search 追踪行业趋势与竞品策略，write_file 输出《战略规划》《周度洞察简报》到岗位记录；\n"
                    "  · 土（根基沉淀）— kb.retire + kb.write → read_file + search_files 扫描全库，对过时条目标记 archived: true（用 patch/write_file），有效条目维护分类索引；\n"
                    "  · 战略管理 — okr.track → write_file 维护 OKR 跟踪表；risk.assess → write_file 输出风险评估报告；project.write → write_file 更新项目文档；metric.write → write_file 更新指标文档；\n"
                    "除 file、web（只读调研）、browser（只读调研）、cronjob 与邮件工具外禁止调用其它工具。"),
            "cto": (f"允许使用 file 工具（read_file/write_file/search_files/patch）在 {vault} 下**读写** .md 文件；"
                    "允许使用 terminal 工具执行 hermes 的 mcp / plugins / cron 子命令，按员工 profile 独立安装 MCP 与插件、管理定时任务；"
                    "允许使用 terminal 执行系统诊断命令（ss/ps/top/df/free/curl/systemctl）做技术栈审计；"
                    "允许使用 web 工具（web_search/web_extract）做只读技术调研，把来源 URL 写进 evidence；"
                    "允许使用 cronjob_manage 设定定期巡检与清理任务；"
                    "禁止删除、移动、重命名业务文件，禁止改动宿主系统配置与 /opt/sefs/obsidian 以外的业务数据；\n"
                    "管理层专属五行学习权限 + 技术管理权限（概念工具→实际操作映射）：\n"
                    "  · 金（技能锻造）— kb.audit → search_files 扫描知识库 + terminal 执行 hermes status/hermes security 审计各 profile 技能安装状态；"
                    "security.audit → terminal 执行 ss -tlnp/ps aux 端口进程扫描 + web_search 查 CVE 漏洞库；\n"
                    "  · 木（生态成长）— skill.evolve → terminal 执行 hermes skills install 为各 profile 安装新技能 + write_file 更新技能文档；\n"
                    "  · 水（知识流通）— kb.benchmark → web_search 搜索技术方案 + web_extract 提取关键内容 + write_file 输出对标报告到知识库；\n"
                    "  · 火（战略洞察）— web.research → web_search 追踪 AI/MCP 前沿；perf.monitor → terminal 执行 top/ps/curl 监控服务健康度；"
                    "cost.optimize → terminal + read_file 分析 LLM 用量账本与基础设施成本；\n"
                    "  · 土（根基沉淀）— kb.retire → patch/write_file 标记过时条目 archived；repo.write → write_file 更新技术文档；"
                    "deploy.write → terminal 执行部署与配置变更；\n"
                    "  · 技术管理 — alert.write → cronjob_manage + write_file 配置定期巡检任务与告警规则；arch.review → write_file 输出架构评审报告；\n"
                    "除 file、terminal、web（只读调研）、cronjob 与邮件工具外禁止调用其它工具。"),
            "cco": (f"允许使用 file 工具（read_file/write_file/search_files/patch）在 {vault} 下**读写** .md 文件；"
                    "允许使用 terminal 限 `/opt/sefs/OpenMontage` 内的产线命令；"
                    "允许使用 web 工具（web_search/web_extract）做只读创意调研，把来源 URL 写进 evidence；"
                    "允许使用 cronjob_manage 设定定期创意学习与盘点任务；\n"
                    "管理层专属五行学习权限 + 创意管理权限（概念工具→实际操作映射）：\n"
                    "  · 金（技能锻造）— kb.audit → search_files 扫描 {vault}/OPC/创意规格/ 全部文件，read_file 评估风格指南覆盖度；"
                    "style.write → write_file 创建/迭代各项目风格指南（OPC/创意规格/style_*.md）；"
                    "copy.write → write_file 更新文案规范与 SOP；asset.write → write_file 维护素材库索引与亮点卡；\n"
                    "  · 木（生态成长）— skill.evolve → read_file 查阅 PM/CW/VD 的 SKILL.md 与样片评审记录，识别创意能力缺口后 write_file 输出《创意能力升级计划》；\n"
                    "  · 水（知识流通）— kb.benchmark → web_search 搜索行业爆款案例与竞品策略，web_extract 提取关键内容，write_file 输出《创意对标报告》到知识库；\n"
                    "  · 火（战略洞察）— web.research → web_search 追踪平台算法变化与用户偏好迁移；"
                    "trend.analyze → web_search + write_file 输出《周度内容趋势简报》；"
                    "audience.insight → web_search 调研受众画像与内容偏好，write_file 输出《受众洞察报告》；\n"
                    "  · 土（根基沉淀）— kb.retire → patch/write_file 标记过时创意模板 archived；kb.write → write_file 沉淀创意方法论到知识库；\n"
                    "  · 创意管理 — cron.write → cronjob_manage 设定每周趋势简报、每两周资产盘点、每月对标分析定时任务；\n"
                    "除 file、terminal（限产线）、web（只读调研）、cronjob 与邮件工具外禁止调用其它工具。"),
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
