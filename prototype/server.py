#!/usr/bin/env python3
"""OPC 一人公司 · AI 员工工作台 —— Hermes 内核版 (v3)

与 v2 的差异：15 名员工不再由规则拼装文本，而是逐个调用真实 Hermes Agent
（/opt/sefs/hermes/run-hermes.sh -z，模型 deepseek-v4.1-flash），把模型返回的
JSON 结果写入原有类型化总线。规则仅保留“结构性护栏”（人设/素材归属校验），
语义判断交给 reviewer 的模型推理。

运行：python3 prototype/server.py   →  http://127.0.0.1:8848
环境变量：
  OPC_HERMES_CLI     Hermes 启动包装器（默认 /opt/sefs/hermes/run-hermes.sh）
  OPC_HERMES_MODE    hermes | rule（默认 hermes；rule 仅用于离线演示）
  OPC_HERMES_TIMEOUT 单次调用超时秒数（默认 180）
  OPC_HERMES_WORKERS 并发模型调用数（默认 6）
"""

import base64
import concurrent.futures
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TZ = timezone(timedelta(hours=8))
CLI = os.environ.get("OPC_HERMES_CLI", "/opt/sefs/hermes/run-hermes.sh")
MODE = os.environ.get("OPC_HERMES_MODE", "hermes")
TIMEOUT = int(os.environ.get("OPC_HERMES_TIMEOUT", "300"))
WORKERS = int(os.environ.get("OPC_HERMES_WORKERS", "6"))
APPROVAL_TIMEOUT = int(os.environ.get("OPC_APPROVAL_TIMEOUT", "600"))
MODEL = os.environ.get("OPC_HERMES_MODEL", "deepseek-v4.1-flash")
# CTO 成本护栏：单轮 trace 累计 / 单次调用 token 上限；金额阈值默认关闭（0=不启用）
# 标定依据（hermes --usage-file 实测）：空载单次调用约 7.7k tokens；一轮完整编排约 1.0–1.6M。
TOKEN_LIMIT = int(os.environ.get("OPC_TOKEN_LIMIT", "3000000"))
CALL_TOKEN_LIMIT = int(os.environ.get("OPC_CALL_TOKEN_LIMIT", "150000"))
COST_LIMIT_USD = float(os.environ.get("OPC_COST_LIMIT_USD", "0"))
AUTH_USER = os.environ.get("OPC_BASIC_USER", "")
AUTH_PASS = os.environ.get("OPC_BASIC_PASS", "")
LOOPBACK = ("127.0.0.1", "::1")


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _hotkey(title) -> str:
    """热点去重键：忽略空白与大小写，同一议题只留一条。"""
    return re.sub(r"\s+", "", str(title or "")).lower()


def _to_int(value, default):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# 配置以 Obsidian 为数据源：项目配置 + 员工名册；文件缺失或解析异常时回退内置默认
VAULT = os.environ.get("OPC_VAULT", "/opt/sefs/obsidian")
PROJECT_FILE = os.path.join(VAULT, "公司", "项目配置.md")
ROSTER_FILE = os.path.join(VAULT, "公司", "员工名册.md")

# 登录凭据放在 Obsidian 公司目录，每次登录实时读取（改文件即生效，不用重启服务）
CRED_FILE = os.path.join(VAULT, "公司", "登录凭据.md")
SESSION_COOKIE = "opc_sess"
SESSION_TTL = 7 * 24 * 3600          # 7 天免登录
SESSIONS = {}                        # token -> {"user": {...}, "exp": epoch}
SESS_LOCK = threading.Lock()
LOGIN_FAILS = {}                     # ip -> [连续失败次数, 解锁时间]
LOGIN_LOCK = threading.Lock()
# 表头别名 → 内部字段名；列顺序任意，缺列就留空
CRED_ALIAS = {
    "用户名": "user", "账号": "user", "姓名": "user",
    "手机号": "phone", "手机": "phone", "电话": "phone",
    "邮箱": "email", "邮件": "email", "email": "email", "e-mail": "email",
    "角色": "role", "岗位": "role", "职位": "role",
    "头像": "avatar",
    "密码": "password",
}


def load_credentials():
    """解析 Obsidian 登录凭据表，返回用户列表（每个用户一个 dict）。

    支持列：用户名 / 手机号 / 邮箱 / 角色 / 头像 / 密码（顺序任意）；
    兼容旧的两列格式（用户名 | 密码）。缺文件时返回 []。
    """
    rows, cols = [], {}
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line.startswith("|"):
                    continue
                cells = [c.strip().strip("`") for c in line.strip("|").split("|")]
                if len(cells) < 2:
                    continue
                if set("".join(cells)) <= set("-: "):
                    continue                     # 表格的 |---| 分隔行
                keys = [CRED_ALIAS.get(c.lower()) for c in cells]
                if len([k for k in keys if k]) >= 2:
                    cols = {k: i for i, k in enumerate(keys) if k}
                    continue                     # 表头行
                pi = cols.get("password", 1)
                if pi >= len(cells) or not cells[pi]:
                    continue
                def cell(key, _cells=cells):
                    i = cols.get(key)
                    return _cells[i] if i is not None and i < len(_cells) else ""
                name = cell("user") or cells[0]
                rows.append({
                    "user": name,
                    "phone": cell("phone"),
                    "email": cell("email"),
                    "role": cell("role") or "成员",
                    "avatar": cell("avatar"),
                    "password": cells[pi],
                })
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[auth] 读取登录凭据失败：{type(e).__name__}: {e}", flush=True)
    return rows


def find_user(account):
    """按手机号 / 邮箱 / 用户名匹配用户；邮箱不区分大小写。"""
    acc = (account or "").strip().lower()
    if not acc:
        return None
    for u in load_credentials():
        for key in ("phone", "email", "user"):
            v = (u.get(key) or "").strip().lower()
            if v and v == acc:
                return u
    return None


# ---------------------------------------------------------------- 内部邮件
MAIL_DOMAIN = os.environ.get("OPC_MAIL_DOMAIN", "mail.conring.cn")
MAIL_HOST = os.environ.get("OPC_MAIL_HOST", "127.0.0.1")
MAIL_SMTP_PORT = int(os.environ.get("OPC_MAIL_SMTP_PORT", "587"))
MAIL_CRED_FILE = os.environ.get("OPC_MAIL_CRED", os.path.join(VAULT, "公司", ".邮箱凭据.md"))
MAIL_AUDIT = os.environ.get("OPC_MAIL_AUDIT", "/opt/sefs/mcp-mail/logs/mail-audit.jsonl")
BOSS_MAIL = os.environ.get("OPC_BOSS_MAIL", f"boss@{MAIL_DOMAIN}")


def load_mail_accounts():
    """解析 .邮箱凭据.md：| 邮箱 | 姓名 | 密码 | → {邮箱: (姓名, 密码)}"""
    accounts = {}
    try:
        with open(MAIL_CRED_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("|"):
                    continue
                cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
                if len(cells) < 3 or "@" not in cells[0]:
                    continue
                accounts[cells[0].lower()] = (cells[1], cells[2])
    except OSError as e:
        print(f"[mail] 读取邮箱凭据失败：{e}", flush=True)
    return accounts


def send_internal_mail(agent_id, to, subject, body, cc=None):
    """以员工本人邮箱发内部邮件（仅限 @mail.conring.cn）；失败直接抛异常由调用方兜底。"""
    def _addr(x):
        x = str(x).strip()
        return x if "@" in x else f"{x}@{MAIL_DOMAIN}"

    to = [_addr(x) for x in ([to] if isinstance(to, str) else list(to or []))]
    cc = [_addr(x) for x in (cc or [])]
    for addr in to + cc:
        if not addr.lower().endswith("@" + MAIL_DOMAIN):
            raise ValueError(f"仅允许内部域发信：{addr}")
    if not to:
        raise ValueError("收件人不能为空")
    accounts = load_mail_accounts()
    address = f"{agent_id}@{MAIL_DOMAIN}"
    if address.lower() not in accounts:
        raise RuntimeError(f"凭据文件中没有 {address}")
    name, password = accounts[address.lower()]
    msg = EmailMessage()
    msg["From"] = formataddr((name, address))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=MAIL_DOMAIN)
    msg.set_content(body.strip())
    with smtplib.SMTP(MAIL_HOST, MAIL_SMTP_PORT, timeout=30) as s:
        s.ehlo()
        s.starttls(context=ssl._create_unverified_context())   # 内部邮件服务自签证书
        s.login(address, password)
        s.send_message(msg, from_addr=address, to_addrs=to + cc)
    try:
        os.makedirs(os.path.dirname(MAIL_AUDIT), exist_ok=True)
        with open(MAIL_AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": iso_now(), "action": "send", "agent": agent_id,
                                "from": address, "to": to, "cc": cc, "subject": subject,
                                "chars": len(body), "ok": True, "source": "opc"},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass
    return {"ok": True, "from": address, "to": to, "cc": cc, "subject": subject}


# 交付物落盘模块（选题/文案/分镜/封面/样片全部写进 Obsidian，不落临时目录）
try:
    import deliverables as DL
except Exception as _dl_err:          # 缺 Pillow/ffmpeg 时降级为仅内存，不影响主流程
    DL = None
    print(f"[boot] 交付物落盘不可用：{type(_dl_err).__name__}: {_dl_err}", flush=True)

# 真实样片渲染（MCP-TTS 旁白 + OpenMontage/Remotion 出片 + 画面验收门）
try:
    import om_render as OMR
except Exception as _om_err:
    OMR = None
    print(f"[boot] 真实渲染不可用：{type(_om_err).__name__}: {_om_err}", flush=True)

DEFAULT_PROJECTS = {
    "wenren": {"id": "wenren", "name": "文人莫克", "kind": "自媒体账号", "color": "#2563EB",
               "kpi": {"views": 0, "completion": 0.0, "fans": 0, "engagement": 0.0}},
    "gou": {"id": "gou", "name": "狗大人", "kind": "自媒体账号", "color": "#EA580C",
            "kpi": {"views": 0, "completion": 0.0, "fans": 0, "engagement": 0.0}},
    "tongxinyuan": {"id": "tongxinyuan", "name": "同心圆", "kind": "数字分身服务平台", "color": "#7C3AED",
                    "kpi": {"uptime": 99.95, "tickets": 0, "delivery_hours": 48.0}},
}
DEFAULT_TONE = {"wenren": "克制·书卷气", "gou": "幽默·松弛", "tongxinyuan": "专业·可信"}
DEFAULT_STAGES = ["topic", "copy", "video", "review", "approved", "published", "archived"]
DEFAULT_HIGH_RISK = ["publish.execute", "deploy.prod", "customer.reply", "ad.spend"]


def _num(v):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            continue
    return 0


def _parse_project_cfg(path):
    projects, tone, stages, high = {}, {}, [], []
    section = ""
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("## "):
                section = line[3:]
                continue
            if line.startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) < 6:
                    continue
                pid = cells[0]
                if pid.startswith("项目") or set(pid) <= set("-: "):
                    continue
                kpi = {}
                for kv in cells[5].replace("，", "、").split("、"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        kpi[k.strip()] = _num(v.strip())
                projects[pid] = {"id": pid, "name": cells[1], "kind": cells[2],
                                 "color": cells[3], "kpi": kpi}
                tone[pid] = cells[4]
            elif line.startswith("- "):
                item = line[2:].strip().strip("`")
                if "阶段" in section:
                    stages.append(item)
                elif "高危" in section:
                    high.append(item)
    return projects, tone, stages, high


def _load_project_cfg():
    try:
        p, t, s, h = _parse_project_cfg(PROJECT_FILE)
        if p and t and s and h:
            print(f"[boot] 项目配置 {PROJECT_FILE} → {len(p)} 个项目 / {len(s)} 阶段 / {len(h)} 高危工具",
                  flush=True)
            return p, t, s, h
        print(f"[boot] 项目配置不完整(项目{len(p)} 调性{len(t)} 阶段{len(s)} 高危{len(h)})，回退内置默认",
              flush=True)
    except FileNotFoundError:
        print(f"[boot] 未找到 {PROJECT_FILE}，回退内置默认", flush=True)
    except Exception as e:
        print(f"[boot] 解析项目配置失败({e})，回退内置默认", flush=True)
    return dict(DEFAULT_PROJECTS), dict(DEFAULT_TONE), list(DEFAULT_STAGES), list(DEFAULT_HIGH_RISK)


PROJECTS, TONE, STAGES, HIGH_RISK_TOOLS = _load_project_cfg()

DEFAULT_AGENT_SPEC = [
    ("ceo", "马斯克", "CEO", "all", ["project.read", "metric.read", "report.render", "kb.search"], "全公司"),
    ("cto", "陈默", "CTO", "all", ["repo.read", "deploy.read", "arch.review", "kb.write"], "全公司 + 同心圆"),
    ("pm_wenren", "苏晚", "产品经理", "wenren", ["hotspot.read", "metric.read", "backlog.write"], "文人莫克"),
    ("pm_gou", "老白", "产品经理", "gou", ["hotspot.read", "metric.read", "backlog.write"], "狗大人"),
    ("pm_tongxinyuan", "何清", "产品经理", "tongxinyuan", ["hotspot.read", "metric.read", "backlog.write"], "同心圆"),
    ("cw_wenren", "江离", "文案编写", "wenren", ["kb.search", "style.guide", "copy.draft"], "文人莫克"),
    ("cw_gou", "桃子", "文案编写", "gou", ["kb.search", "style.guide", "copy.draft"], "狗大人"),
    ("cw_tongxinyuan", "林深", "文案编写", "tongxinyuan", ["kb.search", "style.guide", "copy.draft"], "同心圆"),
    ("vd_wenren", "阿丘", "视频制作", "wenren", ["asset.read", "video.render", "cover.gen"], "文人莫克"),
    ("vd_gou", "大脚", "视频制作", "gou", ["asset.read", "video.render", "cover.gen"], "狗大人"),
    ("vd_tongxinyuan", "木木", "视频制作", "tongxinyuan", ["asset.read", "video.render", "cover.gen"], "同心圆"),
    ("release_ops", "小满", "发布与监控", "all", ["publish.execute", "metric.read", "alert.raise"], "全公司"),
    ("reviewer", "严苛", "审核", "all", ["review.check", "compliance.check", "copy.read"], "全公司"),
    ("scout", "阿寻", "热点采集", "all", ["hotspot.fetch", "asset.fetch", "asset.tag"], "全公司"),
    ("kb_manager", "书禾", "知识库管理", "all", ["kb.write", "kb.index", "kb.qa"], "全公司"),
]

# 员工编制以 Obsidian 名册为唯一数据源；文件缺失或解析异常时回退上面的内置编制
PROJECT_BY_NAME = {v["name"]: k for k, v in PROJECTS.items()}
PROJECT_BY_NAME["全公司"] = "all"


def _parse_roster(path):
    spec, skipped = [], 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 6:
                skipped += 1
                continue
            aid = cells[0]
            if aid.startswith("Agent") or set(aid) <= set("-: "):
                continue
            pid = PROJECT_BY_NAME.get(cells[3])
            if not pid or not aid.replace("_", "").isalnum():
                skipped += 1
                continue
            tools = [t.strip() for t in cells[5].replace("，", "、").split("、") if t.strip()]
            spec.append((aid, cells[1], cells[2], pid, tools, cells[4]))
    return spec, skipped


def _load_agent_spec():
    try:
        spec, skipped = _parse_roster(ROSTER_FILE)
        if len(spec) >= 8:
            print(f"[boot] 员工名册 {ROSTER_FILE} → {len(spec)} 人（跳过 {skipped} 行）", flush=True)
            return spec
        print(f"[boot] 名册仅解析出 {len(spec)} 人，回退内置编制", flush=True)
    except FileNotFoundError:
        print(f"[boot] 未找到 {ROSTER_FILE}，回退内置编制", flush=True)
    except Exception as e:
        print(f"[boot] 解析名册失败({e})，回退内置编制", flush=True)
    return list(DEFAULT_AGENT_SPEC)


AGENT_SPEC = _load_agent_spec()


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
        project = PROJECTS.get(agent["projectId"])
        fields = schema or "summary(≤60字), evidence(2-4条字符串), confidence(0到1数字), tools(字符串数组)"
        # 全员基础权限：只读访问 Obsidian 知识库（公司配置/名册/动态/热点采集等）
        kb_read = ("允许使用 file 工具**只读**查阅 /opt/sefs/obsidian 下的 .md 文件"
                   "（公司名册/项目配置/组织编制/动态快照/热点采集等），"
                   "需要人名、岗位、项目、KPI 或历史结论作为依据时先查文件，并在 evidence（或 reasons）写明来源路径；"
                   "禁止写入、编辑、删除、移动文件，禁止访问 /opt/sefs/obsidian 以外的路径。")
        # 全员邮件权限：以本人邮箱收发内部邮件（仅限内部域）
        mail_rule = ("允许使用邮件工具（mcp__mail__mail_check / mail_list / mail_read / mail_send）"
                     "以本人邮箱 <agent_id>@mail.conring.cn 收发内部邮件：发件人固定为本人，不可冒用他人身份；"
                     "收件人与抄送仅限 @mail.conring.cn 内部域；禁止全员群发与向外部域发信，"
                     "禁止把邮箱密码或凭据文件内容写进邮件；邮件内容须符合本人岗位职责与项目口径。")
        # 角色附加权限（在基础只读之上）
        role_rules = {
            "kb_manager": ("允许使用 file 工具在 /opt/sefs/obsidian 下**读写** .md 文件（整理知识条目、更新索引与笔记、写入 OPC/动态 文档）；"
                           "禁止删除、移动、重命名文件，禁止访问该目录以外的路径；"
                           "修改 公司/ 配置类文件（员工名册/项目配置）时保持原有表格与字段格式，并在 evidence 写明改动理由；"
                           "除 file 与邮件工具外禁止调用其它工具。"),
            "cto": ("允许使用 file 工具在 /opt/sefs/obsidian 下**读写** .md 文件（更新组织编制、技术文档与动态快照）；"
                    "允许使用 terminal 工具执行 hermes 的 mcp / plugins / cron 子命令，按员工 profile 独立安装 MCP 与插件、管理定时任务；"
                    "禁止删除、移动、重命名业务文件，禁止改动宿主系统配置与 /opt/sefs/obsidian 以外的业务数据，禁止执行与插件/MCP/定时无关的命令；"
                    "除 file、terminal、cronjob 与邮件工具外禁止调用其它工具。"),
            "scout": ("允许使用 web 检索工具做只读查证（只查公开信息，不登录、不提交、不发布），把来源 URL 写进 evidence；"
                      "允许使用 file 工具把采集结果写入 /opt/sefs/obsidian/OPC/热点采集/ 下的 .md（只能写该目录，"
                      "禁止改写 公司/、动态/ 等配置与状态文件）；除 web、file 与邮件工具外禁止调用其它工具。"),
            "release_ops": "允许使用 web 检索工具做只读监控（查公开数据与舆情，不登录、不提交、不发布），把来源 URL 写进 evidence；除 web、file 与邮件工具外禁止调用其它工具。",
            "reviewer": "允许使用 web 检索工具做只读查证（核对链接可达性与事实来源，不登录、不提交），把来源 URL 写进 reasons；除 web、file 与邮件工具外禁止调用其它工具。",
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
                proc = subprocess.run([CLI, "-z", q, "--usage-file", usage_path],
                                      capture_output=True, text=True, timeout=TIMEOUT, env=env)
                raw = (proc.stdout or "").strip()
                usage = self._read_usage(usage_path)
                data = self._extract(raw)
                ms = int((time.time() - t0) * 1000)
                with self.lock:
                    self.calls += 1
                if data:
                    with self.lock:
                        self.latencies.append(ms)
                    return data, {"engine": "hermes", "model": MODEL, "latencyMs": ms,
                                  "attempts": attempt, "profile": agent["id"], "usage": usage}
                reason = f"非 JSON 输出：{raw[:120]}"
            except Exception as exc:
                with self.lock:
                    self.calls += 1
                reason = str(exc)[:160]
                if isinstance(exc, subprocess.TimeoutExpired):
                    break
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
KERNEL = None  # 延迟绑定，见下方 Kernel


class BudgetAbort(Exception):
    """CTO 判定成本/额度超限，在阶段边界终止本轮任务。"""


class Kernel:
    def __init__(self):
        self.lock = threading.RLock()
        self.agents = {}
        for aid, name, role, project, tools, scope in AGENT_SPEC:
            self.agents[aid] = {"id": aid, "name": name, "role": role, "projectId": project,
                                "scope": scope, "tools": tools, "status": "idle", "task": "",
                                "avatar": f"/avatars/{aid}.png",
                                "tone": TONE.get(project, "跨项目中台"),
                                "highRisk": [t for t in tools if t in HIGH_RISK_TOOLS]}
        self.projects = json.loads(json.dumps(PROJECTS, ensure_ascii=False))
        self.scene = "WORK"
        self.meeting = {"active": False, "title": "", "participants": [], "agenda": [], "startedAt": ""}
        self.meeting_log = {"id": "", "traceId": "", "status": "idle", "title": "",
                            "participants": [], "agenda": [], "speeches": [],
                            "startedAt": "", "endedAt": "", "conclusion": "",
                            "transcriptUrl": "", "minutesUrl": ""}
        self.meeting_records = []
        if DL:
            try:
                self.meeting_records = DL.load_meeting_records()
            except Exception as e:
                print(f"[boot] 读取会议历史失败：{type(e).__name__}: {e}", flush=True)
        self.messages, self.events = [], 0
        self.hotspots, self.contents, self.reports, self.approvals, self.knowledge = [], [], [], [], []
        self._hotspot_scan_at = 0.0        # 热点目录重扫节流（秒级时间戳）
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
        self.journal_file = os.path.join(VAULT, "动态", "runtime.json")
        self._restore_history()
        self._sync_stop = threading.Event()
        threading.Thread(target=self._background_sync, daemon=True).start()

    def _restore_history(self):
        """重启后恢复历史：交付物读磁盘，报告/审批/热点/知识/消息读运行时日志。"""
        if DL:
            try:
                items = DL.load_deliverable_items()
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
                secs = stats.get("approvalSeconds")
                if isinstance(secs, list):
                    self.stats["approvalSeconds"] = secs[-100:]
        if not self.messages and DL:
            try:
                self.messages = DL.load_recent_messages(120)
            except Exception as e:
                print(f"[boot] 恢复历史消息失败：{type(e).__name__}: {e}", flush=True)
        if DL:
            if not self.hotspots:
                try:
                    self.hotspots = DL.load_hotspots()
                except Exception as e:
                    print(f"[boot] 恢复历史热点失败：{type(e).__name__}: {e}", flush=True)
            if not self.knowledge:
                try:
                    self.knowledge = DL.load_knowledge()
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
        global PROJECTS, TONE, STAGES, HIGH_RISK_TOOLS, AGENT_SPEC, PROJECT_BY_NAME
        p, t, s, h = _load_project_cfg()
        missing_proj = {"wenren", "gou", "tongxinyuan"} - set(p)
        if missing_proj:
            raise ValueError(f"项目配置缺少必需项目: {sorted(missing_proj)}")
        spec = _load_agent_spec()
        known = {a[0] for a in spec}
        missing_ids = [a[0] for a in DEFAULT_AGENT_SPEC if a[0] not in known]
        if missing_ids:
            raise ValueError(f"员工名册缺少必需岗位: {missing_ids}")
        PROJECTS, TONE, STAGES, HIGH_RISK_TOOLS = p, t, s, h
        PROJECT_BY_NAME = {v["name"]: k for k, v in PROJECTS.items()}
        PROJECT_BY_NAME["全公司"] = "all"
        AGENT_SPEC = spec
        with self.lock:
            old = self.agents
            self.agents = {}
            for aid, name, role, project, tools, scope in AGENT_SPEC:
                prev = old.get(aid, {})
                self.agents[aid] = {"id": aid, "name": name, "role": role, "projectId": project,
                                    "scope": scope, "tools": tools,
                                    "status": prev.get("status", "idle"), "task": prev.get("task", ""),
                                    "avatar": f"/avatars/{aid}.png",
                                    "tone": TONE.get(project, "跨项目中台"),
                                    "highRisk": [x for x in tools if x in HIGH_RISK_TOOLS]}
            self.projects = json.loads(json.dumps(PROJECTS, ensure_ascii=False))
        info = {"agents": len(self.agents), "projects": len(self.projects), "stages": len(STAGES),
                "highRiskTools": len(HIGH_RISK_TOOLS),
                "sources": {"projects": PROJECT_FILE, "roster": ROSTER_FILE}}
        self.emit("config-reload", "", info)
        return info

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
        if kind in ("hotspot", "report", "approval", "knowledge"):
            self.save_runtime()     # 低频关键事件落盘，重启后可恢复
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
        if DL:
            try:
                urls = DL.save_meeting_record(out)
                out.update({k: v for k, v in urls.items() if v})
                with self.lock:
                    for r in self.meeting_records:
                        if r.get("id") == out["id"]:
                            r.update({k: v for k, v in urls.items() if v})
            except Exception as e:
                print(f"[meeting] 会议记录落盘失败：{type(e).__name__}: {e}", flush=True)
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
            res = send_internal_mail(agent_id, [BOSS_MAIL], subject, body, cc=cc or [])
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
        project = PROJECTS.get(a["projectId"], {}).get("name", "全公司")
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
            kpi = "；".join(f"{PROJECTS[k]['name']}={PROJECTS[k]['kpi']}" for k in PROJECTS)
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
            title, tags, score = "AI 编程工具周报：多智能体协作成主流", ["科技", "AI", "生产力"], 0.88
        trace, hotspot = self.inject_hotspot(title, tags, score)
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

    def inject_hotspot(self, title=None, tags=None, score=None):
        if not title:
            title = "AI 复活老照片引发版权争议"
            tags = ["科技", "版权", "文化"]
            score = 0.86
        trace = uid("tr")
        fit = {"wenren": round(min(0.97, (score or 0.8) + 0.04), 3),
               "gou": round(max(0.3, (score or 0.8) - 0.14), 3),
               "tongxinyuan": round(max(0.2, (score or 0.8) - 0.45), 3)}
        hotspot = {"id": uid("hs"), "traceId": trace, "title": title, "source": "热点源/RSS",
                   "tags": tags or ["综合"], "score": score or 0.8, "fit": fit, "at": iso_now()}
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
        self.emit("hotspot", trace, hotspot)
        threading.Thread(target=self.run_pipeline, args=(trace, hotspot), daemon=True).start()
        return trace, hotspot

    def run_pipeline(self, trace, hotspot):
        self.active_trace = trace
        try:
            self._pipeline(trace, hotspot)
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
            self.send("escalation", "cto", "owner", trace,
                      {"text": f"任务已在阶段边界停止：{exc}", "aborted": True}, priority=2)
        except Exception as exc:
            self.send("escalation", "ceo", "owner", trace, {"text": f"编排异常：{exc}"}, priority=2)
            with self.lock:
                self.traces[trace]["status"] = "error"
                self.traces[trace]["closedAt"] = iso_now()
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
            self.export_trace_docs(trace)   # 收口之后再落盘，交付文档才能记录到最终状态
            self.save_runtime()             # 本轮报告/消息/知识入库，重启后可恢复

    # ---------- 选题流程护栏 ----------
    PIDS = ("wenren", "gou", "tongxinyuan")

    def _deg(self, aid, what):
        return self.fallback(aid, what)

    def _coerce_topics(self, data, pid, hotspot):
        """PM 选题卡：强制恰好 3 条，模型漏项时用规则兜底。"""
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
        while len(out) < 3:
            i = len(out)
            base = PROJECTS[pid]["name"]
            angle = (hotspot.get("title") or "今日热点").strip()
            forms = ("热点解读", "延伸视角", "互动话题")
            out.append({"projectId": pid, "title": f"{angle}｜{base}{forms[i]}",
                        "angle": f"从{base}定位切入{forms[i]}", "form": forms[i],
                        "durationSec": 45, "reason": "规则兜底选题"})
        return out[:3]

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
        """CCO 选 9 条：每个项目强制 3 条，缺项从 PM 池补齐。"""
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
        return {"durationSec": _to_int(src.get("durationSec"), topic.get("durationSec", 45)),
                "tts": {"voice": str(tts.get("voice") or "zh-CN-XiaoxiaoNeural")[:40],
                        "style": str(tts.get("style") or "纪录片口播")[:40]},
                "materials": [str(x)[:60] for x in (src.get("materials") or [])][:8],
                "copyBrief": str(src.get("copyBrief") or "按定稿选题写口播稿")[:200],
                "storyboardNotes": str(src.get("storyboardNotes") or "4-6 镜，首镜 3 秒抓人")[:300]}

    def _review_map(self, data, ids):
        """样片评审：三票制；解析失败按基础设施降级放行并写明原因。"""
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
            out.setdefault(iid, (True, ["评审结果未返回，按基础设施降级放行"]))
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
                    f"以 {PROJECTS[pid]['name']} 产品经理身份评审本项目样片，判断能否进入精剪。",
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
                "reviewerGate": "精剪后终审",
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

    def _pipeline(self, trace, hotspot):
        pms = [("wenren", "pm_wenren"), ("gou", "pm_gou"), ("tongxinyuan", "pm_tongxinyuan")]
        # 1. 每日选题会：PM 各交 3 条 + CEO/CTO 指定选题 + scout 简报，同波 6 个调用
        participants = ["ceo", "cto", "cco", "pm_wenren", "pm_gou", "pm_tongxinyuan", "scout", "kb_manager"]
        agenda1 = [f"热点：{hotspot['title']}", "三项目各 3 条候选", "CCO 汇总选 9 条"]
        self.meeting_begin(trace, "每日选题会", participants, agenda1)
        self.set_scene("MEETING", trace, title="每日选题会", participants=participants, agenda=agenda1)
        for aid in participants:
            self.set_agent(aid, "meeting", "选题会")
            self.send("delegation", "ceo", aid, trace, {"task": "参会：按岗位职责提交/评估选题"},
                      project=None if self.agents[aid]["projectId"] == "all" else self.agents[aid]["projectId"])
        self.meeting_say("ceo", f"开会。今天的议题是热点「{hotspot['title']}」（热度 {hotspot['score']}）。"
                                "请热点采集先报简报，三位产品经理各提 3 条候选，CCO 汇总。", "opening", trace)

        ctx = (f"热点：{hotspot['title']}；热度 {hotspot['score']}；三项目匹配度："
               f"文人莫克 {hotspot['fit']['wenren']}，狗大人 {hotspot['fit']['gou']}，同心圆 {hotspot['fit']['tongxinyuan']}")
        futures = {"scout": HERMES.pool.submit(
            self.call_agent, "scout", "针对给定热点产出一条业务简报，并判断素材可用性。",
            f"热点：{hotspot['title']}；热度 {hotspot['score']}；标签 {hotspot['tags']}")}
        for pid, pm in pms:
            futures[pm] = HERMES.pool.submit(
                self.call_agent, pm,
                f"提交 {PROJECTS[pid]['name']} 今日 3 条候选选题，供 CCO 与 CTO 汇总。",
                ctx,
                f"本项目匹配度 {hotspot['fit'][pid]}。额外 JSON 字段：topics(数组，恰好 3 条，"
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
            self.send("biz.event", "scout", rcpt, trace,
                      {"hotspot": hotspot, "result": scout_result,
                       "text": f"热点上报：{hotspot['title']}（热度 {hotspot['score']}）"}, priority=1)
        self.emit("engine-meta", trace, {"agent": "scout", **scout_meta})
        self.meeting_say("scout", scout_result.get("summary") or
                         f"热点「{hotspot['title']}」热度 {hotspot['score']}，素材可采集，建议三项目按匹配度跟进。",
                         "report", trace)

        pm_topics = {}
        for pid, pm in pms:
            data, meta = futures[pm].result()
            data = data or self._deg(pm, "选题卡")
            pm_topics[pid] = self._coerce_topics(data, pid, hotspot)
            self.send("result", pm, "cco", trace,
                      {"result": data, "topics": pm_topics[pid], "engine": meta["engine"],
                       "latencyMs": meta.get("latencyMs", 0), "projectId": pid}, project=pid)
            self.send("result", pm, "cto", trace,
                      {"topics": pm_topics[pid], "engine": meta["engine"]}, project=pid)
            self.meeting_say(pm, "提交 3 条候选：" + "；".join(t.get("title", "") for t in pm_topics[pid]),
                             "speech", trace)
        ceo_topics = self._coerce_mgmt_topics(futures["ceo"].result()[0])
        cto_topics = self._coerce_mgmt_topics(futures["cto"].result()[0])
        boss_topic = {"projectId": "all", "title": str(hotspot["title"])[:60],
                      "angle": "老板注入选题", "form": "老板指定", "durationSec": 45,
                      "reason": "老板直接指定"}

        # 2. CCO 汇总创意池 → 选 9 条（每项目 3 条），抄送 CEO、CTO
        pool = {pid: pm_topics[pid] for pid in self.PIDS}
        pool["_management"] = {"ceo": ceo_topics, "cto": cto_topics, "boss": [boss_topic]}
        pool_size = sum(len(pm_topics[p]) for p in self.PIDS) + len(ceo_topics) + len(cto_topics) + 1
        data, meta = self.call_agent(
            "cco",
            "汇总 3 名产品经理的 9 条创意，叠加 CEO/CTO/老板指定选题与自选创意，"
            "按项目调性、匹配度、差异化去重，选出 9 条（每项目恰好 3 条），并给出淘汰理由。",
            json.dumps(pool, ensure_ascii=False),
            "额外 JSON 字段：selected(数组，恰好 9 条，每条 {projectId, title, angle, form, durationSec, reason})；"
            "eliminated(数组，每条 {title, reason})。",
            "summary, evidence, confidence, tools, selected, eliminated")
        data = data or self._deg("cco", "选题汇总")
        selected = self._coerce_selection(data, pm_topics)
        eliminated = data.get("eliminated") or []
        self.send("topic.select", "cco", "ceo", trace,
                  {"selected": selected, "eliminated": data.get("eliminated", []),
                   "poolSize": pool_size, "result": data, "engine": meta["engine"]}, priority=2)
        self.send("topic.select", "cco", "cto", trace,
                  {"selected": selected, "cc": True, "engine": meta["engine"]}, priority=1)
        self.emit("engine-meta", trace, {"agent": "cco", **meta})
        picked = [t.get("title", "") for pid in self.PIDS for t in selected.get(pid, [])]
        self.meeting_say("cco", (data.get("summary") or
                                 f"候选池共 {pool_size} 条，按调性与差异化去重后选出 9 条。") +
                         (" 入选：" + "；".join(picked) if picked else ""), "decision", trace)
        self.meeting_end(trace, f"每日选题会结束：候选池 {pool_size} 条，CCO 筛出 9 条（每项目 3 条），"
                                "转入选题评审会定稿 6 条。")

        # 3. CEO 召集选题评审会：CEO/CTO/CCO + 涉及项目 PM，最终定 6 条（每项目 2 条）
        agenda2 = ["复核 CCO 9 条", "逐项目定 2 条", "形成会议纪要"]
        self.meeting_begin(trace, "选题评审会", participants, agenda2)
        self.set_scene("MEETING", trace, title="选题评审会", participants=participants, agenda=agenda2)
        for aid in ("cto", "cco", "pm_wenren", "pm_gou", "pm_tongxinyuan"):
            self.set_agent(aid, "meeting", "选题评审会")
            self.send("delegation", "ceo", aid, trace,
                      {"task": "参会：对 CCO 9 条选题复核并定稿 6 条"}, priority=2)
        self.meeting_say("ceo", "现在复核 CCO 的 9 条，逐项目定 2 条。今天必须形成 6 条定稿和会议纪要。",
                         "opening", trace)
        data, meta = self.call_agent(
            "ceo",
            "主持选题评审会：从 CCO 的 9 条中逐项目定稿 2 条，共 6 条；"
            "输出会议纪要（议题、参会人、结论、每项目 2 条及理由、后续分工），并给出收件人与抄送人。",
            json.dumps({pid: selected[pid] for pid in self.PIDS}, ensure_ascii=False),
            "额外 JSON 字段：final(数组，恰好 6 条，每条 {projectId,title,angle,durationSec})；"
            "minutes(会议纪要 Markdown，≤1500字)；recipients(数组)；cc(数组)。",
            "summary, evidence, confidence, tools, final, minutes, recipients, cc")
        data = data or self._deg("ceo", "选题评审会")
        final = self._coerce_final(data, selected)
        minutes = str(data.get("minutes") or "").strip() or (
            f"# 选题评审会\n\n- 议题：{hotspot['title']}\n- 参会：CEO 陆清安、CTO 扶频、CCO 沈青禾、"
            f"产品经理 贺平生/米珂/姜月初\n- 结论：每项目定稿 2 条，共 6 条\n")
        recipients = [str(x) for x in (data.get("recipients") or [])] or \
            ["ceo", "cto", "cco", "pm_wenren", "pm_gou", "pm_tongxinyuan"]
        cc = [str(x) for x in (data.get("cc") or [])] or ["owner"]
        display = lambda xs: ["老板" if x == "owner" else self.agents.get(x, {}).get("name", x) for x in xs]
        meeting_doc = {}
        if DL:
            try:
                meeting_doc = DL.export_meeting(trace, {
                    "title": "选题评审会", "minutes": minutes,
                    "recipients": display(recipients), "cc": display(cc),
                    "final": final, "hotspot": hotspot})
            except Exception as e:
                print(f"[pipeline] 会议纪要落盘失败：{type(e).__name__}: {e}", flush=True)
        self.send("decision", "ceo", "all", trace,
                  {"text": f"定稿 6 条（每项目 2 条）：{hotspot['title']}",
                   "final": final, "minutes": minutes,
                   "minutesUrl": (meeting_doc or {}).get("minutes", ""),
                   "mailUrl": (meeting_doc or {}).get("mail", ""),
                   "result": data, "engine": meta["engine"]}, project="all", priority=2)
        self.send("report.ready", "ceo", "owner", trace,
                  {"kind": "meeting", "title": "选题评审会纪要",
                   "minutesUrl": (meeting_doc or {}).get("minutes", ""),
                   "mailUrl": (meeting_doc or {}).get("mail", ""),
                   "recipients": recipients, "cc": cc}, priority=1)
        conclusion = "定稿 6 条（每项目 2 条）：" + "；".join(
            f"{PROJECTS[pid]['name']}《{t['title']}》" for pid in self.PIDS for t in final[pid])
        self.meeting_say("ceo", "结论：" + conclusion + "。后续按项目进入制作，会议纪要已归档到 Obsidian。",
                         "conclusion", trace)
        self.meeting_end(trace, conclusion, (meeting_doc or {}).get("minutes", ""))
        # 祁南（kb_manager）在选题定稿即归档，不再等发布审批；发布后只更新同一条
        self.archive_knowledge(trace, [
            {"projectId": pid, "title": t.get("title"),
             "body": f"选题定稿：{t.get('angle') or t.get('title')}；来源热点：{hotspot.get('title')}；"
                     f"评审会结论：{conclusion[:120]}",
             "tags": ["热点/" + str(hotspot.get("title") or "")[:24]] + list(hotspot.get("tags") or [])[:3]}
            for pid in self.PIDS for t in final.get(pid, [])])
        self.set_scene("WORK", trace, title="定稿 6 条，进入制作")

        # 3.5 CCO 邮件汇报老板：选题评审结果 + 等待开工审批（抄送 CEO/CTO）
        self.guard_budget(trace)
        mail_lines = [
            f"老板：",
            "",
            f"本轮选题评审已完成（热点：{hotspot['title']}）。",
            f"候选池 {pool_size} 条 → CCO 筛出 9 条 → 评审会定稿 {sum(len(v) for v in final.values())} 条（每项目 2 条）。",
            "",
            "## 定稿清单",
        ]
        for pid in self.PIDS:
            mail_lines.append(f"### {PROJECTS[pid]['name']}")
            for t in final[pid]:
                mail_lines.append(
                    f"- 《{t['title']}》｜角度：{t.get('angle', '—')}｜形态：{t.get('form', '—')}｜"
                    f"时长：{t.get('durationSec', '—')}s｜理由：{t.get('reason', '—')}")
        if eliminated:
            mail_lines += ["", "## 淘汰与原因"]
            for e in eliminated[:12]:
                mail_lines.append(f"- {e.get('title', '—')}：{e.get('reason', '—')}")
        mail_lines += [
            "",
            "## 待办",
            "- 开工审批单已生成（含成本/素材/合规检查），请老板在 OPC 工作台批准后进入制作。",
            f"- 会议纪要：{(meeting_doc or {}).get('minutes', '（未生成）')}",
        ]
        mail_subject = f"[选题评审] {str(hotspot['title'])[:30]} · 定稿 6 条，待开工审批"
        mail_body = "\n".join(mail_lines)
        self.send("biz.event", "cco", "owner", trace,
                  {"text": "CCO 已邮件汇报选题评审结果，等待开工审批",
                   "to": BOSS_MAIL, "cc": ["ceo", "cto"], "subject": mail_subject,
                   "finalCount": sum(len(v) for v in final.values()),
                   "eliminated": len(eliminated)}, priority=2)
        threading.Thread(target=self.mail_boss,
                         args=(mail_subject, mail_body, "cco", ["ceo", "cto"]),
                         daemon=True).start()

        # 4. 建 6 条内容（每项目 2 条）
        items = {}
        for pid in self.PIDS:
            for t in final[pid]:
                item = {"id": uid("c"), "projectId": pid, "traceId": trace, "stage": "topic",
                        "title": t["title"], "angle": t.get("angle", ""),
                        "owner": f"pm_{pid}", "toneProfile": TONE[pid], "assets": [],
                        "reviewNote": "", "durationSec": t.get("durationSec", 45)}
                with self.lock:
                    self.contents.append(item)
                items[item["id"]] = item
                self.emit("pipeline", trace, item)
            self.send("delegation", f"pm_{pid}", f"cw_{pid}", trace,
                      {"task": f"承接本项目 {len(final[pid])} 条定稿选题，先出制作评估再写稿"}, project=pid)
            self.send("delegation", f"pm_{pid}", f"vd_{pid}", trace,
                      {"task": f"承接本项目 {len(final[pid])} 条定稿选题，先出制作评估再制作样片"}, project=pid)

        # 4.5 开工审批：老板批准选题与成本/素材/合规摘要后才进入制作
        prod_approval = {
            "id": uid("ap"), "traceId": trace, "action": "production.start", "risk": "high",
            "status": "pending", "approver": "老板", "createdAt": iso_now(), "decidedAt": "",
            "items": list(items.keys()),
            "titles": [i["title"] for i in items.values()],
            "title": f"{len(items)} 条内容开工审批（含成本/素材/合规检查）",
            "checks": self._production_checks(items),
        }
        with self.lock:
            self.approvals.append(prod_approval)
            prod_ev = threading.Event()
            self.approval_events[prod_approval["id"]] = prod_ev
        self.send("approval", "ceo", "owner", trace, {"approval": prod_approval}, priority=2)
        self.emit("approval", trace, prod_approval)

        prod_ok = prod_ev.wait(timeout=APPROVAL_TIMEOUT)
        with self.lock:
            prod_appr = next(a for a in self.approvals if a["id"] == prod_approval["id"])
            self.approval_events.pop(prod_approval["id"], None)
        if not prod_ok or prod_appr["status"] != "approved":
            with self.lock:
                if prod_appr["status"] == "pending":
                    prod_appr["status"] = "timeout"
                    prod_appr["decidedAt"] = iso_now()
                for item in items.values():
                    item["stage"] = "blocked"
                    item["blockedBy"] = "production-gate"
                t = self.traces.get(trace)
                if t:
                    t["status"] = "rejected" if prod_appr["status"] == "rejected" else "timeout"
                    t["closedAt"] = iso_now()
            for item in items.values():
                self.items_emit(trace, item)
            self.send("escalation", "ceo", "owner", trace,
                      {"text": "开工审批未通过，本轮不进入制作"}, priority=2)
            self.emit("approval", trace, prod_appr)
            self.set_scene("WORK", trace, title="开工审批未通过")
            return

        self.guard_budget(trace)   # CTO 成本熔断：审批通过后、进入制作前再核一次

        # 5. PM 组织本项目组出《制作评估报告》：文案要求、分镜、时长、配音/TTS、素材
        jobs = {}
        for pid, pm in pms:
            jobs[pid] = HERMES.pool.submit(
                self.call_agent, pm,
                f"为 {PROJECTS[pid]['name']} 的 2 条定稿选题组织本项目组：给出文案要求、分镜剧本要点、"
                "时长评估、配音/TTS 方案与素材清单，形成《制作评估报告》，并列出素材缺口。",
                json.dumps(final[pid], ensure_ascii=False),
                "额外 JSON 字段：assessment({items:[{title,durationSec,tts:{voice,style},"
                "materials:[],copyBrief,storyboardNotes}], summary})；assetGaps(数组，每条 {item, why})。",
                "summary, evidence, confidence, tools, assessment, assetGaps")
        gap_jobs = {}
        for pid, pm in pms:
            data, meta = jobs[pid].result()
            data = data or self._deg(pm, "制作评估报告")
            pid_items = [i for i in items.values() if i["projectId"] == pid]
            for idx, item in enumerate(pid_items):
                item["plan"] = self._coerce_plan(data, item, idx)
                item["assessment"] = {"summary": data.get("summary", ""),
                                      "evidence": data.get("evidence", []),
                                      "risks": data.get("risks", []),
                                      "assetGaps": data.get("assetGaps", [])}
                self.items_emit(trace, item)
            self.send("result", pm, "cco", trace,
                      {"result": data, "assessment": data.get("assessment"), "engine": meta["engine"]}, project=pid)
            self.send("result", pm, "cto", trace,
                      {"assessment": data.get("assessment"), "engine": meta["engine"]}, project=pid)
            gaps = data.get("assetGaps") or []
            if gaps:
                self.send("delegation", pm, "scout", trace,
                          {"task": "素材缺失工单：补齐/替代并给出结论", "gaps": gaps}, project=pid, priority=2)
                gap_jobs[pid] = HERMES.pool.submit(
                    self.call_agent, "scout", "承接素材缺失工单：逐项给出补齐、替代或「无可用素材」的结论。",
                    json.dumps(gaps, ensure_ascii=False),
                    "额外 JSON 字段：gaps(数组，每条 {item,status,alternative})。",
                    "summary, evidence, confidence, tools, gaps")
        for pid, job in gap_jobs.items():
            data, meta = job.result()
            data = data or self._deg("scout", "素材工单")
            for item in items.values():
                if item["projectId"] == pid:
                    item["assetResolution"] = data.get("gaps", [])
                    self.items_emit(trace, item)
            self.send("result", "scout", f"pm_{pid}", trace,
                      {"result": data, "engine": meta["engine"]}, project=pid)

        # 6. 文案成稿（6 个真实调用）
        jobs = {}
        for iid, item in items.items():
            pid = item["projectId"]
            plan = item.get("plan") or {}
            jobs[iid] = HERMES.pool.submit(
                self.call_agent, f"cw_{pid}",
                f"为《{item['title']}》写一段成稿文案（≤80字），符合本项目人设，禁止串用其他项目风格。",
                f"项目：{PROJECTS[pid]['name']}；风格：{TONE[pid]}；时长：{plan.get('durationSec', 45)}s；"
                f"文案要求：{plan.get('copyBrief', '')}",
                "额外 JSON 字段：copy(成稿文案≤80字)。",
                "summary, evidence, confidence, tools, copy")
            self.send("delegation", f"pm_{pid}", f"cw_{pid}", trace,
                      {"task": f"按评估报告写《{item['title']}》成稿"}, project=pid)
        for iid, item in items.items():
            pid = item["projectId"]
            data, meta = jobs[iid].result()
            data = data or {"summary": "文案已交付（降级）", "evidence": [], "confidence": 0.2,
                            "tools": [], "copy": f"{PROJECTS[pid]['name']} 文案占位"}
            item["stage"] = "copy"
            item["copy"] = data.get("copy") or data.get("summary", "")
            self.send("result", f"cw_{pid}", f"pm_{pid}", trace,
                      {"result": data, "itemId": iid, "engine": meta["engine"],
                       "latencyMs": meta.get("latencyMs", 0)}, project=pid)
            self.items_emit(trace, item)

        # 7. 视频制作出样片（6 个真实调用）
        jobs = {}
        for iid, item in items.items():
            pid = item["projectId"]
            plan = item.get("plan") or {}
            jobs[iid] = HERMES.pool.submit(
                self.call_agent, f"vd_{pid}",
                f"按《制作评估报告》为《{item['title']}》制作样片方案：逐镜剧本、封面、配音/TTS。",
                f"项目：{PROJECTS[pid]['name']}；风格：{TONE[pid]}；时长：{plan.get('durationSec', 45)}s；"
                f"配音：{plan.get('tts', {})}；文案：{item.get('copy', '')}；"
                f"分镜要点：{plan.get('storyboardNotes', '')}",
                "额外 JSON 字段：shots(数字), storyboard(4-6 镜，每镜 画面/台词，可加 插图:1-2 个中文物件名，"
                "如 相纸/药水盘/放大镜，≤220字), cover(≤20字), sample(样片说明≤80字)。",
                "summary, evidence, confidence, tools, shots, storyboard, cover, sample")
            self.send("delegation", f"pm_{pid}", f"vd_{pid}", trace,
                      {"task": f"按评估报告出《{item['title']}》样片"}, project=pid)
        for iid, item in items.items():
            pid = item["projectId"]
            data, meta = jobs[iid].result()
            data = data or {"summary": "样片已出（降级）", "evidence": [], "confidence": 0.2, "tools": []}
            item["stage"] = "sample"
            item["video"] = data
            item["shots"] = data.get("shots")
            item["storyboard"] = data.get("storyboard") or ""
            item["cover"] = data.get("cover") or ""
            item["sampleNote"] = data.get("sample") or data.get("summary", "")
            self.send("result", f"vd_{pid}", f"pm_{pid}", trace,
                      {"result": data, "itemId": iid, "engine": meta["engine"],
                       "latencyMs": meta.get("latencyMs", 0)}, project=pid)
            self.items_emit(trace, item)

        # 7.5 真实出片：MCP-TTS 旁白 + OpenMontage 渲染 + 画面验收门
        render_blocked = set()
        for idx, (iid, item) in enumerate(items.items(), 1):
            pid = item["projectId"]
            self.send("biz.event", f"vd_{pid}", "ceo", trace,
                      {"text": f"真实出片 {idx}/{len(items)}：《{item['title']}》", "step": "render"},
                      project=pid, priority=2)
            if os.environ.get("OPC_SKIP_RENDER") == "1":
                files = item.get("files") or {}
                item["renderCheck"] = {"pass": True, "reasons": ["测试模式：跳过真实渲染"]}
                item["assets"] = [files.get("video", ""), files.get("cover", "")]
                item["renderSkipped"] = True
            elif not OMR:
                item["renderCheck"] = {"pass": False, "reasons": ["渲染模块不可用"]}
            else:
                res = OMR.produce(item, PROJECTS[pid], TONE.get(pid, ""))
                item["render"] = {k: v for k, v in res.items() if k != "check"}
                item["renderCheck"] = res.get("check") or {
                    "pass": False, "reasons": [res.get("reason") or "未知渲染错误"]}
                if res.get("ok"):
                    item["assets"] = [res["videoUrl"], res["coverUrl"]]
                else:
                    item["assets"] = []
                    item["renderError"] = res.get("reason", "")
            if not item["renderCheck"].get("pass"):
                render_blocked.add(iid)
                item["blockedBy"] = "render-gate"
                reason = item.get("renderError") or "；".join(
                    (item["renderCheck"].get("reasons") or [])[:2])
                self.send("escalation", f"vd_{pid}", "owner", trace,
                          {"text": f"《{item['title']}》样片未过画面验收：{reason}"},
                          project=pid, priority=2)
            self.emit("render-gate", trace, {"itemId": iid,
                                            "pass": item["renderCheck"].get("pass"),
                                            "check": item["renderCheck"]})
            self.items_emit(trace, item)

        # 8. 样片三方评审（CCO + CTO + 本项目 PM），最多两轮
        sample_blocked = set()
        review_ids = [iid for iid in items if iid not in render_blocked]
        for rnd in (1, 2):
            maps, metas = self._sample_wave(trace, review_ids, items)
            failed = []
            for iid in review_ids:
                item = items[iid]
                pid = item["projectId"]
                roles = {"cco": maps["cco"][iid], "cto": maps["cto"][iid], "pm": maps[pid][iid]}
                item["sampleReviews"] = {k: {"pass": v[0], "reasons": v[1]} for k, v in roles.items()}
                passed = all(v[0] for v in roles.values())
                item["samplePass"] = passed
                self.emit("sample-review", trace,
                          {"itemId": iid, "projectId": pid, "round": rnd, "pass": passed,
                           "reviews": item["sampleReviews"]})
                if not passed:
                    failed.append(iid)
            if not failed:
                break
            if rnd == 2:
                for iid in failed:
                    items[iid]["stage"] = "blocked"
                    sample_blocked.add(iid)
                    self.items_emit(trace, items[iid])
                    self.send("escalation", "cco", "owner", trace,
                              {"text": f"《{items[iid]['title']}》两轮样片评审未通过，已阻断"},
                              project=items[iid]["projectId"], priority=2)
                break
            jobs = {}
            for iid in failed:
                item = items[iid]
                pid = item["projectId"]
                reasons = json.dumps(item.get("sampleReviews", {}), ensure_ascii=False)
                jobs[iid] = HERMES.pool.submit(
                    self.call_agent, f"vd_{pid}",
                    f"按三方样片评审意见返工《{item['title']}》的样片：重出逐镜剧本、封面与样片说明。",
                    f"项目：{PROJECTS[pid]['name']}；风格：{TONE[pid]}；评审意见：{reasons}",
                    "额外 JSON 字段：shots(数字), storyboard(≤220字，每镜可加 插图:1-2 个中文物件名，"
                    "如 相纸/药水盘/放大镜), cover(≤20字), sample(≤80字)。",
                    "summary, evidence, confidence, tools, shots, storyboard, cover, sample")
                self.send("delegation", f"pm_{pid}", f"vd_{pid}", trace,
                          {"task": "按样片三方评审意见返工"}, project=pid, priority=2)
            for iid, job in jobs.items():
                data, meta = job.result()
                data = data or {"summary": "返工未返回，沿用原样片", "evidence": [], "confidence": 0.2}
                item = items[iid]
                item["storyboard"] = data.get("storyboard") or item.get("storyboard", "")
                item["cover"] = data.get("cover") or item.get("cover", "")
                item["sampleNote"] = data.get("sample") or item.get("sampleNote", "")
                self.items_emit(trace, item)
                self.send("result", f"vd_{item['projectId']}", f"pm_{item['projectId']}", trace,
                          {"result": data, "itemId": iid, "engine": meta["engine"]},
                          project=item["projectId"])
            review_ids = failed

        # 9. 通过样片评审后进入最终精剪
        final_ids = [iid for iid in items
                     if iid not in sample_blocked and iid not in render_blocked]
        jobs = {}
        for iid in final_ids:
            item = items[iid]
            pid = item["projectId"]
            jobs[iid] = HERMES.pool.submit(
                self.call_agent, f"vd_{pid}",
                f"《{item['title']}》样片已通过 CCO/CTO/PM 评审，按意见完成最终精剪并说明交付物。",
                f"项目：{PROJECTS[pid]['name']}；文案：{item.get('copy', '')}；"
                f"样片说明：{item.get('sampleNote', '')}",
                "额外 JSON 字段：finalCut(精剪说明≤120字), assets(数组，交付文件名)。",
                "summary, evidence, confidence, tools, finalCut, assets")
            self.send("delegation", f"pm_{pid}", f"vd_{pid}", trace,
                      {"task": "样片评审通过，执行最终精剪"}, project=pid)
        for iid, job in jobs.items():
            data, meta = job.result()
            data = data or {"summary": "精剪完成（降级）", "evidence": [], "confidence": 0.2}
            item = items[iid]
            pid = item["projectId"]
            item["stage"] = "final"
            item["finalCut"] = data.get("finalCut") or data.get("summary", "")
            # 精剪必须产出可直接审的真实母版：以已过画面验收的样片为母版落 05-final.mp4
            files = item.get("files") or {}
            final_url = files.get("final", "")
            src = (item.get("render") or {}).get("video") or ""
            item["finalAssets"] = []
            if DL and final_url and src and os.path.isfile(src):
                try:
                    dst = DL._local(final_url)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(src, dst)
                    item["finalAssets"] = [final_url]
                    item["assets"] = [a for a in item.get("assets", []) if a and a != final_url] + [final_url]
                except Exception as e:
                    print(f"[pipeline] {iid} 精剪母版落盘失败：{type(e).__name__}: {e}", flush=True)
            self.send("result", f"vd_{pid}", f"pm_{pid}", trace,
                      {"result": data, "itemId": iid, "engine": meta["engine"]}, project=pid)
            self.items_emit(trace, item)

        # 10. 审核员最终审核（精剪后）；驳回返工重写，最多 2 轮
        self.guard_budget(trace)   # 制作完成后、终审前核对成本
        passed_items, review_history = {}, []
        pending = [iid for iid in items
                   if iid not in sample_blocked and iid not in render_blocked]
        for rnd in (1, 2):
            rev_jobs = {iid: HERMES.pool.submit(self.review_with_model, items[iid], trace) for iid in pending}
            failed = []
            for iid in pending:
                item = items[iid]
                pid = item["projectId"]
                verdict, reasons, meta = rev_jobs[iid].result()
                item["stage"] = "review"
                item["reviewNote"] = "通过" if verdict else "；".join(reasons)
                review_history.append({"itemId": iid, "projectId": pid, "round": rnd, "pass": verdict})
                self.send("result", "reviewer", "ceo", trace,
                          {"verdict": {"pass": verdict, "reasons": reasons,
                                       "projectId": pid, "itemId": iid},
                           "result": {"summary": "审核通过" if verdict else "审核驳回",
                                      "evidence": reasons, "confidence": 0.9, "tools": ["review.check"]},
                           "engine": meta["engine"], "reason": meta.get("reason", "")}, project=pid)
                self.items_emit(trace, item)
                if verdict:
                    passed_items[iid] = item
                else:
                    failed.append(iid)
            if not failed:
                break
            if rnd == 2:
                for iid in failed:
                    items[iid]["stage"] = "blocked"
                    self.items_emit(trace, items[iid])
                    self.send("escalation", "reviewer", "owner", trace,
                              {"text": f"《{items[iid]['title']}》两次审核未通过，已阻断，不进入审批"},
                              project=items[iid]["projectId"], priority=2)
                break
            rework = {}
            for iid in failed:
                item = items[iid]
                pid = item["projectId"]
                rework[iid] = HERMES.pool.submit(
                    self.call_agent, f"cw_{pid}", f"按审核意见重写《{item['title']}》文案（≤80字）。",
                    f"项目：{PROJECTS[pid]['name']}；风格：{TONE[pid]}",
                    f"审核驳回理由：{item['reviewNote']}。必须修正这些问题且保持本项目人设。",
                    "summary, evidence, confidence, tools, copy")
                self.send("delegation", f"pm_{pid}", f"cw_{pid}", trace,
                          {"task": "按审核意见返工重写"}, project=pid, priority=2)
            self.emit("rework", trace, {"round": rnd, "items": failed,
                                        "reasons": [items[i]["reviewNote"] for i in failed]})
            for iid, job in rework.items():
                new, m = job.result()
                item = items[iid]
                item["copy"] = (new or {}).get("copy") or item.get("copy", "")
                self.send("result", f"cw_{item['projectId']}", f"pm_{item['projectId']}", trace,
                          {"result": new or {"summary": "返工未返回，沿用原稿", "evidence": [],
                                             "confidence": 0.2}, "engine": m["engine"]}, project=item["projectId"])
                self.items_emit(trace, item)
            pending = failed

        with self.lock:
            self.traces[trace]["reviewHistory"] = review_history
            self.traces[trace]["reviewGate"] = {"passed": len(passed_items),
                                                "blocked": len(items) - len(passed_items)}
        self.export_trace_docs(trace)

        if not passed_items:
            self.send("escalation", "reviewer", "owner", trace,
                      {"text": "全部内容审核未通过，本轮不进入审批"}, priority=2)
            self.set_scene("WORK", trace, title="全部驳回，等待返工")
            with self.lock:
                self.traces[trace]["status"] = "blocked"
                self.traces[trace]["closedAt"] = iso_now()
            return

        # 11. 审批门（仅放行审核通过的内容）
        self.guard_budget(trace)   # 发布审批前最后一道成本核对
        approval = {"id": uid("ap"), "traceId": trace, "action": "publish.execute", "risk": "high",
                    "status": "pending", "approver": "老板", "createdAt": iso_now(), "decidedAt": "",
                    "items": list(passed_items),
                    "titles": [i["title"] for i in passed_items.values()],
                    "title": f"{len(passed_items)} 条内容发布审批（含同心圆对外承诺）",
                    "checks": self._publish_checks(items, passed_items, review_history)}
        with self.lock:
            self.approvals.append(approval)
            ev = threading.Event()
            self.approval_events[approval["id"]] = ev
        self.send("approval", "ceo", "owner", trace, {"approval": approval}, priority=2)
        self.emit("approval", trace, approval)
        for item in passed_items.values():
            item["stage"] = "approved"
            self.items_emit(trace, item)

        approved = ev.wait(timeout=APPROVAL_TIMEOUT)
        with self.lock:
            appr = next(a for a in self.approvals if a["id"] == approval["id"])
            self.approval_events.pop(approval["id"], None)
        if not approved or appr["status"] != "approved":
            with self.lock:
                if appr["status"] == "pending":
                    appr["status"] = "timeout"
                    appr["decidedAt"] = iso_now()
                self.traces[trace]["status"] = "rejected" if appr["status"] == "rejected" else "timeout"
                self.traces[trace]["closedAt"] = iso_now()
            self.send("escalation", "ceo", "owner", trace,
                      {"text": "审批未通过，内容保持未发布"}, priority=2)
            self.emit("approval", trace, appr)
            return

        # 12. release_ops 发布
        pub_ctx = json.dumps({i["title"]: i["projectId"] for i in passed_items.values()}, ensure_ascii=False)
        data, meta = self.call_agent("release_ops", f"按排期发布 {len(passed_items)} 条内容并给出监控口径。",
                                     pub_ctx, "", "summary, evidence, confidence, tools")
        data = data or {"summary": "内容已发布并进入监控", "evidence": ["平台回执 200"],
                        "confidence": 0.9, "tools": ["publish.execute"]}
        for iid, item in passed_items.items():
            item["stage"] = "published"
            self.send("result", "release_ops", "ceo", trace,
                      {"result": data, "engine": meta["engine"], "itemId": iid},
                      project=item["projectId"])
            self.items_emit(trace, item)
        with self.lock:
            self.stats["published"] += len(passed_items)
            for pid in {i["projectId"] for i in passed_items.values()}:
                k = self.projects[pid]["kpi"]
                if "views" in k:
                    k["views"] += 1200
                    k["fans"] += 18
                    k["completion"] = 0.62
                    k["engagement"] = 0.071
                else:
                    k["tickets"] = 2
                    k["delivery_hours"] = 36.0
        self.emit("metrics", trace, {p: self.projects[p]["kpi"]
                                     for p in {i["projectId"] for i in passed_items.values()}})

        # 13. kb_manager 归档
        data, meta = self.call_agent("kb_manager", "把本轮 6 条内容归档为知识条目，给出检索标签。",
                                     pub_ctx, "", "summary, evidence, confidence, tools")
        data = data or {"summary": "已归档知识资产", "evidence": ["kb.write"], "confidence": 0.9, "tools": []}
        # 定稿时已归档过同题条目，这里按 traceId + 标题更新为发布口径，不重复计数
        self.archive_knowledge(trace, [
            {"projectId": item["projectId"], "title": item["title"],
             "body": data.get("summary", "归档"), "tags": ["内容复盘"]}
            for item in passed_items.values()], stage_label="发布归档")
        for iid, item in passed_items.items():
            item["stage"] = "archived"
            self.items_emit(trace, item)
        self.send("result", "kb_manager", "ceo", trace, {"result": data, "engine": meta["engine"]},
                  project="all")

        # 14. 汇报：CEO 日报 / CTO 技术周报 / 项目周报（并行）
        self.set_scene("REPORT", trace, title="CEO 日报生成")
        wave = {
            "ceo": HERMES.pool.submit(self.call_agent, "ceo",
                                      "写一段今日 CEO 日报结论，覆盖 6 条产出与审批。",
                                      pub_ctx, "", "summary, evidence, confidence, tools"),
            "cto": HERMES.pool.submit(self.call_agent, "cto",
                                      "写一段 CTO 技术周报：Agent 编排健康度与风险。",
                                      f"本轮消息数 {len([m for m in self.messages if m['traceId'] == trace])}；"
                                      "审批门拦截 publish.execute", "", "summary, evidence, confidence, tools"),
        }
        for pid in {i["projectId"] for i in passed_items.values()}:
            wave[f"pm_{pid}"] = HERMES.pool.submit(
                self.call_agent, f"pm_{pid}", f"写一段 {PROJECTS[pid]['name']} 项目周报结论。",
                pub_ctx, "", "summary, evidence, confidence, tools")

        daily_data, daily_meta = wave["ceo"].result()
        daily_data = daily_data or self._deg("ceo", "CEO 日报")
        report = {"id": uid("rp"), "traceId": trace, "kind": "daily", "title": "CEO 日报",
                  "conclusion": daily_data.get("summary", ""), "evidence": daily_data.get("evidence", []),
                  "decisions": [{"text": "定稿 6 条（每项目 2 条）并发布", "owner": "ceo",
                                 "risk": "mid", "approved": True}],
                  "engine": daily_meta["engine"], "createdAt": iso_now()}
        with self.lock:
            self.reports.append(report)
        self.send("report.ready", "ceo", "owner", trace, {"report": report}, priority=1)
        self.emit("report", trace, report)

        tech_data, tech_meta = wave["cto"].result()
        tech_data = tech_data or self._deg("cto", "技术周报")
        tech = {"id": uid("rp"), "traceId": trace, "kind": "tech", "title": "CTO 技术周报",
                "conclusion": tech_data.get("summary", ""), "evidence": tech_data.get("evidence", []),
                "decisions": [], "engine": tech_meta["engine"], "createdAt": iso_now()}
        with self.lock:
            self.reports.append(tech)
        self.emit("report", trace, tech)

        for pid in {i["projectId"] for i in passed_items.values()}:
            wd, wm = wave[f"pm_{pid}"].result()
            wd = wd or self._deg(f"pm_{pid}", "项目周报")
            weekly = {"id": uid("rp"), "traceId": trace, "kind": "weekly",
                      "title": f"{PROJECTS[pid]['name']} 项目周报",
                      "conclusion": wd.get("summary", ""), "evidence": wd.get("evidence", []),
                      "decisions": [], "engine": wm["engine"], "createdAt": iso_now()}
            with self.lock:
                self.reports.append(weekly)
            self.emit("report", trace, weekly)

        with self.lock:
            self.traces[trace]["status"] = "completed"
            self.traces[trace]["closedAt"] = iso_now()
            self.traces[trace]["hermes"] = HERMES.stats()
        self.set_scene("WORK", trace, title="归档完成，回到生产")

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
            prefix = DL.plan(item["traceId"], item["projectId"], item["id"])["dir"] + "/"
        except Exception:
            return False
        return str(url).startswith(prefix)

    def review_with_model(self, item, trace):
        structural = []
        expected = TONE.get(item["projectId"])
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
        if structural:
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
            f"审核精剪成片：{item['title']}；文案：{item.get('copy', '')[:120]}；"
            f"精剪说明：{(item.get('finalCut') or '—')[:120]}",
            f"项目：{PROJECTS[item['projectId']]['name']}；要求风格：{expected}；样片三方评审：{sample_note}",
            extra, "summary, evidence, confidence, tools, pass, reasons")
        self.record_usage("reviewer", meta, trace)
        if not data:
            return (True, ["模型审核不可用，结构护栏通过"], meta)
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
            files = DL.plan(trace, item["projectId"], item["id"])
            item["files"] = files
            project = dict(PROJECTS.get(item["projectId"], {"name": item["projectId"], "color": "#64748B"}))
            tone = item.get("toneProfile") or TONE.get(item["projectId"], "")
            snap = json.loads(json.dumps(item, ensure_ascii=False))

            def job():
                res = DL.export_item(snap, project, tone)
                if res.get("video_written") and (snap.get("renderCheck") or {}).get("pass"):
                    with self.lock:
                        cur = next((c for c in self.contents if c["id"] == item["id"]), None)
                    if cur:
                        cur["assets"] = [files["video"], files["cover"]]
                        self.emit("pipeline", trace, cur)
                elif res.get("error"):
                    print(f"[export] {item['id']} {res['error'][:120]}", flush=True)

            DL.submit(job)
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
            names = {pid: p["name"] for pid, p in PROJECTS.items()}
            DL.submit(lambda: DL.export_trace(trace, contents, msgs, apprs, names, tr))
        except Exception as e:
            print(f"[export] trace 文档失败 {type(e).__name__}: {e}", flush=True)

    # ---------- 审批 ----------
    def decide_approval(self, approval_id, decision, decided_by=""):
        with self.lock:
            appr = next((a for a in self.approvals if a["id"] == approval_id), None)
        if not appr:
            return None
        if appr["status"] != "pending":
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
        self.emit("approval", appr["traceId"], appr)
        ev = self.approval_events.get(approval_id)
        if ev:
            ev.set()
        return appr

    # ---------- 人设串味注入 ----------
    def persona_injection(self):
        trace = uid("tr")
        item = {"id": uid("c"), "projectId": "gou", "traceId": trace, "stage": "review",
                "title": "（测试）把文人莫克的稿子塞给狗大人", "owner": "cw_gou",
                "toneProfile": TONE["wenren"], "assets": ["asset://wenren/master.mp4"],
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

    def _sync_hotspots_from_vault(self, force=False):
        """运行中重扫 OPC/热点采集：苏寒新落盘的业务简报无需重启即可进面板。"""
        if not DL:
            return
        now = time.time()
        if not force and now - self._hotspot_scan_at < 60:
            return
        self._hotspot_scan_at = now
        try:
            self.collection = DL.load_collection_status()
        except Exception as e:
            print(f"[hotspot] 读取采集状态失败：{type(e).__name__}: {e}", flush=True)
        try:
            disk = DL.load_hotspots()
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
                         "sourceTrace": trace, "title": title[:80],
                         "body": body[:300] or "（归档占位）",
                         "tags": list(dict.fromkeys(tags))[:12], "at": iso_now()}
                self.knowledge.append(entry)
                index[(trace, title)] = entry
                made.append(entry)
        if made:
            self.emit("knowledge", trace, {
                "stage": stage_label, "count": len(made),
                "latest": [{"title": k["title"], "projectId": k["projectId"]} for k in made[-3:]]})
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


KERNEL = Kernel()


class Handler(BaseHTTPRequestHandler):
    server_version = "OPCWorkbench/3.0-hermes"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("OPC_QUIET") != "1":
            super().log_message(fmt, *args)

    def _json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    def _require_auth(self):
        """非回环来源必须携带 Basic 凭据；本机访问免登录（验收脚本、调试不受影响）。"""
        if not AUTH_USER or self.client_address[0] in LOOPBACK:
            return True
        header = self.headers.get("Authorization") or ""
        if header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:].strip()).decode("utf-8")
            except Exception:
                raw = ""
            user, _, pwd = raw.partition(":")
            if hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(pwd, AUTH_PASS):
                return True
        body = b'{"error":"unauthorized"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="OPC Workbench", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        return False

    # ---------- 登录会话 ----------
    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v
        return ""

    def _session_user(self):
        tok = self._cookie_token()
        if not tok:
            return None
        now = time.time()
        with SESS_LOCK:
            rec = SESSIONS.get(tok)
            if not rec:
                return None
            if rec["exp"] < now:
                SESSIONS.pop(tok, None)
                return None
            return dict(rec["user"])

    def _need_login(self, path):
        """未登录：API 回 401，页面 302 到登录页（带 next 回跳地址）。"""
        if self._session_user():
            return True
        if path.startswith("/api/"):
            self._json({"error": "unauthorized", "login": "/login"}, 401)
            return False
        nxt = quote(self.path if self.path.startswith("/") else "/")
        self.send_response(302)
        self.send_header("Location", f"/login?next={nxt}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _login(self, body):
        ip = self.client_address[0]
        now = time.time()
        with LOGIN_LOCK:
            fails, until = LOGIN_FAILS.get(ip, [0, 0])
        if until > now:
            return self._json({"ok": False, "error": f"失败次数过多，请 {int(until - now)} 秒后再试"}, 429)

        users = load_credentials()
        if not users:
            print(f"[auth] 登录失败：凭据文件缺失或没有密码字段 {CRED_FILE}", flush=True)
            return self._json({"ok": False, "error": "服务端未配置登录账号（见 Obsidian 公司/登录凭据.md）"}, 503)

        account = str(body.get("account") or body.get("user") or "").strip()
        pwd = str(body.get("password") or "")
        u = find_user(account)
        if not u or not hmac.compare_digest(pwd, u["password"]):
            with LOGIN_LOCK:
                f2 = fails + 1
                LOGIN_FAILS[ip] = [f2, now + 60 if f2 >= 5 else 0]
            print(f"[auth] 登录失败 {ip} 第 {f2} 次（账号 {account or '空'}）", flush=True)
            return self._json({"ok": False, "error": "账号或密码不正确"}, 401)

        with LOGIN_LOCK:
            LOGIN_FAILS.pop(ip, None)
        role = u["role"]
        avatar = u["avatar"]
        if avatar and not avatar.startswith("/"):
            avatar = ""
        if not avatar and role == "老板":
            avatar = "/avatars/boss.png"
        user = {"id": "boss" if role == "老板" else "staff",
                "role": role, "avatar": avatar, "name": u["user"]}
        tok = secrets.token_urlsafe(32)
        with SESS_LOCK:
            expired = [t for t, r in SESSIONS.items() if r["exp"] < now]
            for t in expired:
                SESSIONS.pop(t, None)
            SESSIONS[tok] = {"user": user, "exp": now + SESSION_TTL}
        cookie = f"{SESSION_COOKIE}={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"
        print(f"[auth] 登录成功 {ip} → {user['name']}", flush=True)
        return self._json({"ok": True, "user": user}, cookie=cookie)

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path != "/":
            path = path.rstrip("/")   # 允许 /office3d/ 或 /office3d%2F 这类结尾带斜杠的访问
        if not self._require_auth():
            return
        # 登录页、登录信息、会话查询：未登录也可访问
        if path in ("/login", "/login.html"):
            return self._static("login.html", "text/html; charset=utf-8")
        if path == "/api/logininfo":
            users = load_credentials()
            return self._json({"ok": True, "ready": bool(users), "file": "公司/登录凭据.md"})
        if path == "/api/session":
            user = self._session_user()
            if not user:
                return self._json({"ok": False, "login": "/login"}, 401)
            return self._json({"ok": True, "user": user})
        # 静态资源与交付物不拦登录（交付链接要能在外部浏览器直接打开）
        if path == "/vendor/three.module.js":
            return self._static("vendor/three.module.js", "application/javascript; charset=utf-8")
        if path.startswith("/avatars/"):
            name = path[len("/avatars/"):]
            if not re.fullmatch(r"[a-z_]+\.png", name):
                return self._json({"error": "not found"}, 404)
            return self._static(os.path.join("assets", "avatars", name), "image/png")
        if path.startswith("/faces/"):
            name = path[len("/faces/"):]
            if not re.fullmatch(r"[a-z_]+\.png", name):
                return self._json({"error": "not found"}, 404)
            return self._static(os.path.join("assets", "faces", name), "image/png")
        if path.startswith("/vault/"):
            return self._vault(path[len("/vault/"):])
        # 其余页面与接口一律要求登录
        if not self._need_login(path):
            return
        if path == "/api/health":
            return self._json({"ok": True, "version": "3.0", "engine": MODE,
                               "hermes": HERMES.stats(), "time": iso_now()})
        if path in ("/", "/index.html"):
            return self._index()
        if path in ("/office3d", "/office3d.html"):
            return self._static("office3d.html", "text/html; charset=utf-8")
        if path in ("/meeting3d", "/meeting3d.html"):
            return self._static("meeting3d.html", "text/html; charset=utf-8")
        if path == "/api/meeting":
            return self._json(KERNEL.meeting_payload())
        if path.startswith("/api/meeting/"):
            rec = KERNEL.meeting_detail(path.rsplit("/", 1)[-1])
            return self._json(rec) if rec else self._json({"error": "meeting not found"}, 404)
        if path == "/api/state":
            return self._json(KERNEL.snapshot())
        if path == "/api/events":
            return self._sse()
        if path.startswith("/api/trace/"):
            data = self._trace(path.rsplit("/", 1)[-1])
            if data is None:
                return self._json({"error": "trace not found"}, 404)
            return self._json(data)
        if path == "/api/reports":
            kind = (parse_qs(urlparse(self.path).query).get("kind") or [""])[0]
            return self._json({"reports": [r for r in KERNEL.reports if (not kind or r["kind"] == kind)]})
        return self._json({"error": "not found"}, 404)

    def _index(self):
        return self._static("index.html", "text/html; charset=utf-8")

    def _static(self, rel, ctype):
        """白名单静态文件：仅 index.html / office3d.html / meeting3d.html / vendor 下的 three.js"""
        file = os.path.join(BASE_DIR, rel)
        try:
            body = open(file, "rb").read()
        except FileNotFoundError:
            return self._json({"error": f"{rel} missing"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _vault(self, rel):
        """Obsidian 交付物只读发布：/vault/... → VAULT/...，支持 Range（视频拖动进度条要用）。"""
        root = os.path.realpath(VAULT)
        full = os.path.realpath(os.path.join(VAULT, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        # 登录凭据与隐藏目录不对外发布
        rel_parts = os.path.relpath(full, root).split(os.sep)
        if full == os.path.realpath(CRED_FILE) or any(p.startswith(".") for p in rel_parts):
            return self._json({"error": "not found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = {".md": "text/plain; charset=utf-8", ".png": "image/png",
                 ".mp4": "video/mp4", ".json": "application/json; charset=utf-8",
                 ".html": "text/html; charset=utf-8"}.get(ext, "application/octet-stream")
        size = os.path.getsize(full)
        m = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if m:
            start = int(m.group(1) or 0)
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            if start >= size or start > end:
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            with open(full, "rb") as f:
                f.seek(start)
                body = f.read(end - start + 1)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            with open(full, "rb") as f:
                body = f.read()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _trace(self, trace):
        with KERNEL.lock:
            msgs = [m for m in KERNEL.messages if m.get("traceId") == trace]
            contents = [c for c in KERNEL.contents if c.get("traceId") == trace]
            if trace not in KERNEL.traces and not msgs and not contents:
                return None
            return {"traceId": trace, "meta": KERNEL.traces.get(trace, {}),
                    "messages": msgs, "contents": contents,
                    "approvals": [a for a in KERNEL.approvals if a.get("traceId") == trace],
                    "reports": [r for r in KERNEL.reports if r.get("traceId") == trace],
                    "knowledge": [k for k in KERNEL.knowledge if k.get("traceId") == trace],
                    "messageCount": len(msgs)}

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = queue.Queue(maxsize=500)
        with KERNEL.lock:
            KERNEL.subscribers.append(q)

        def chunk(payload):
            raw = payload.encode()
            self.wfile.write(f"{len(raw):X}\r\n".encode() + raw + b"\r\n")
            self.wfile.flush()

        try:
            chunk(f"data: {json.dumps({'kind': 'state', 'traceId': '', 'at': iso_now(), 'data': KERNEL.snapshot()}, ensure_ascii=False)}\n\n")
            last = time.time()
            while True:
                try:
                    env = q.get(timeout=1.0)
                    chunk(f"data: {json.dumps(env, ensure_ascii=False)}\n\n")
                except queue.Empty:
                    if time.time() - last > 10:
                        chunk(f"data: {json.dumps({'kind': 'heartbeat', 'at': iso_now(), 'traceId': '', 'data': {}}, ensure_ascii=False)}\n\n")
                        last = time.time()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass
        finally:
            with KERNEL.lock:
                if q in KERNEL.subscribers:
                    KERNEL.subscribers.remove(q)
            self.wfile.write(b"0\r\n\r\n")

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._require_auth():
            return
        body = self._body()
        if path == "/api/login":
            return self._login(body)
        if path == "/api/logout":
            tok = self._cookie_token()
            with SESS_LOCK:
                SESSIONS.pop(tok, None)
            return self._json({"ok": True},
                              cookie=f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        user = self._session_user()
        if not user:
            return self._json({"error": "unauthorized", "login": "/login"}, 401)
        if path == "/api/patrol":
            return self._json({"ok": True, **KERNEL.patrol()})
        if path == "/api/hotspots/inject":
            trace, hotspot = KERNEL.inject_hotspot(body.get("title"), body.get("tags"), body.get("score"))
            return self._json({"ok": True, "traceId": trace, "hotspot": hotspot})
        if path == "/api/meetings":
            trace, hs = KERNEL.inject_hotspot(body.get("title"), body.get("tags"), body.get("score"))
            return self._json({"ok": True, "traceId": trace, "meeting": "daily_topic"})
        if path.startswith("/api/approvals/"):
            if user.get("role") != "老板":
                return self._json({"ok": False, "error": "forbidden",
                                   "reason": "仅老板可审批（含开工与发布）"}, 403)
            decision = str(body.get("decision") or "")
            if decision not in ("approve", "reject"):
                return self._json({"ok": False, "error": "invalid decision",
                                   "expected": "approve|reject"}, 400)
            appr = KERNEL.decide_approval(path.rsplit("/", 1)[-1], decision, user.get("name", ""))
            if not appr:
                return self._json({"error": "approval not found"}, 404)
            return self._json({"ok": True, "approval": appr})
        if path == "/api/config/reload":
            try:
                return self._json({"ok": True, **KERNEL.reload_config()})
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path == "/api/delegate":
            try:
                return self._json({"ok": True, **KERNEL.mgmt_delegate(
                    body.get("sender", ""), body.get("recipient", ""), body.get("text", ""))})
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path == "/api/commands":
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"ok": False, "error": "空指令"}, 400)
            m = re.match(r"@(\S+)\s*([\s\S]*)", text)
            if m:
                key, rest = m.group(1), m.group(2).strip()
                cand = [a for a in KERNEL.agents.values() if a["id"] == key or a["name"] == key]
                if not cand:
                    cand = [a for a in KERNEL.agents.values() if key in a["name"] or key in a["id"]]
                if not cand:
                    return self._json({"ok": False, "error": f"没有找到员工 {key}"}, 404)
                try:
                    return self._json({"ok": True, **KERNEL.ask_agent(cand[0]["id"],
                                       rest or "请汇报当前进展与卡点。")})
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
            if "串味" in text or "persona" in text.lower():
                return self._json({"ok": True, "personaTest": KERNEL.persona_injection()})
            if "注入" in text or "热点" in text:
                trace, hs = KERNEL.inject_hotspot()
                return self._json({"ok": True, "traceId": trace, "hotspot": hs})
            with KERNEL.lock:
                blockers = [c for c in KERNEL.contents if c["stage"] in ("review", "approved")]
                pending = [a for a in KERNEL.approvals if a["status"] == "pending"]
            data, meta = KERNEL.call_agent("ceo", "回答老板：今天哪个项目卡住了，卡在哪。",
                                          f"待审/待发布 {len(blockers)} 条；待审批 {len(pending)} 个",
                                          "", "summary, evidence, confidence, tools")
            answer = data or {"summary": f"当前 {len(blockers)} 条待审、{len(pending)} 个审批待拍板",
                              "evidence": [], "confidence": 0.4, "tools": []}
            KERNEL.send("result", "ceo", "owner", "", {"result": answer, "engine": meta["engine"]}, priority=1)
            return self._json({"ok": True, "answer": answer, "engine": meta["engine"]})
        return self._json({"error": "not found"}, 404)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("OPC_PORT", 8848)))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    if args.host not in LOOPBACK and args.host != "localhost" and not AUTH_USER:
        print("警告：绑定到非回环地址且未设置 OPC_BASIC_USER / OPC_BASIC_PASS，局域网内任何人可访问与审批")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    auth = f"auth=Basic({AUTH_USER})" if AUTH_USER else "auth=off"
    print(f"OPC Workbench (Hermes kernel) on http://{args.host}:{args.port}  mode={MODE} model={MODEL}  {auth}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
