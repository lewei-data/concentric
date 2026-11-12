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
WEB_DIST = os.path.normpath(os.path.join(BASE_DIR, "..", "web", "dist"))
WEB_MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml",
            ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2", ".woff": "font/woff"}
TZ = timezone(timedelta(hours=8))
CLI = os.environ.get("OPC_HERMES_CLI", "/opt/sefs/hermes/run-hermes.sh")
MODE = os.environ.get("OPC_HERMES_MODE", "hermes")
TIMEOUT = int(os.environ.get("OPC_HERMES_TIMEOUT", "600"))
WORKERS = int(os.environ.get("OPC_HERMES_WORKERS", "6"))
APPROVAL_TIMEOUT = int(os.environ.get("OPC_APPROVAL_TIMEOUT", "600"))
MODEL = os.environ.get("OPC_HERMES_MODEL", "deepseek-v4.1-flash")
# CTO 成本护栏：单轮 trace 累计 / 单次调用 token 上限；金额阈值默认关闭（0=不启用）
# 标定依据（2026-11-07 实测，hermes --usage-file 的 total_including_auxiliary.total_tokens）：
#   单次员工调用（含工具轮次）约 7k–650k；选题阶段约 2.6M；一轮完整编排实测 12.4M（含返工）。
# 默认阈值取 20M / 2M：正常整轮不触发，只拦截真正的失控消耗；可用环境变量按项目收紧。
TOKEN_LIMIT = int(os.environ.get("OPC_TOKEN_LIMIT", "20000000"))
CALL_TOKEN_LIMIT = int(os.environ.get("OPC_CALL_TOKEN_LIMIT", "2000000"))
COST_LIMIT_USD = float(os.environ.get("OPC_COST_LIMIT_USD", "0"))
AUTH_USER = os.environ.get("OPC_BASIC_USER", "")
AUTH_PASS = os.environ.get("OPC_BASIC_PASS", "")
LOOPBACK = ("127.0.0.1", "::1")
# 首页默认进入新版 /app；设 OPC_HOME_LEGACY=1 可一键回退到旧版控制台（不改代码、不丢 /app）
HOME_LEGACY = os.environ.get("OPC_HOME_LEGACY", "0") == "1"


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _hotkey(title) -> str:
    """热点去重键：忽略空白与大小写，同一议题只留一条。"""
    return re.sub(r"\s+", "", str(title or "")).lower()


def _to_int(value, default):
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return default


# 配置以 Obsidian 为数据源：项目配置 + 员工名册；文件缺失或解析异常时回退内置默认
VAULT = os.environ.get("OPC_VAULT", "/opt/sefs/obsidian")
PROJECT_FILE = os.path.join(VAULT, "公司", "项目配置.md")
ROSTER_FILE = os.path.join(VAULT, "公司", "员工名册.md")

# 登录凭据放在 Obsidian 公司目录，每次登录实时读取（改文件即生效，不用重启服务）
CRED_FILE = os.path.join(VAULT, "公司", "登录凭据.md")
# 素材库：首页卡片的数据源 + 只读文件发布目录（materials.json 里写相对路径即可）
MATERIALS_FILE = os.environ.get("OPC_MATERIALS_FILE", os.path.join(BASE_DIR, "materials.json"))
MATERIALS_ROOT = os.environ.get("OPC_MATERIALS_ROOT", "/mnt/sefs/github-hotspots")
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

# 作品档案：把交付物目录归一成「名称 + 时间」可管理的资产记录（只读）
try:
    import works as WK
except Exception as _wk_err:
    WK = None
    print(f"[boot] 作品档案不可用：{type(_wk_err).__name__}: {_wk_err}", flush=True)

# 真实样片渲染（MCP-TTS 旁白 + OpenMontage/Remotion 出片 + 画面验收门）
try:
    import om_render as OMR
except Exception as _om_err:
    OMR = None
    print(f"[boot] 真实渲染不可用：{type(_om_err).__name__}: {_om_err}", flush=True)

# CCO 创意灵感 / CTO 运维监控：只读聚合，失败不影响主服务其它接口
try:
    import insights
    import ops_monitor
except Exception as _panel_err:
    insights = ops_monitor = None
    print(f"[boot] 灵感/运维聚合不可用：{type(_panel_err).__name__}: {_panel_err}", flush=True)

# 内容流水线定义（声明式）：阶段顺序/门禁/轮次以 config/pipeline.yaml 为准
import pipeline_defs as PIPE

# 过程数据落库（SQLite/WAL）+ 多渠道消息摄取；OPC_DB 未开启时整体降级为旧路径
try:
    import db as DBX
    import ingest as ING
except Exception as _db_err:
    DBX = None
    ING = None
    print(f"[boot] 过程数据入库不可用：{type(_db_err).__name__}: {_db_err}", flush=True)
DB_ON = bool(DBX and DBX.ENABLED)
INGEST_ON = DB_ON and os.environ.get("OPC_INGEST", "0") == "1"

DEFAULT_PROJECTS = {
    "wenren": {"id": "wenren", "name": "文人莫克", "kind": "自媒体账号", "color": "#2563EB",
               "kpi": {"views": 0, "completion": 0.0, "fans": 0, "engagement": 0.0}},
    "gou": {"id": "gou", "name": "狗大人", "kind": "自媒体账号", "color": "#EA580C",
            "kpi": {"views": 0, "completion": 0.0, "fans": 0, "engagement": 0.0}},
    "tongxinyuan": {"id": "tongxinyuan", "name": "同心圆", "kind": "数字分身服务平台", "color": "#7C3AED",
                    "kpi": {"uptime": 99.95, "tickets": 0, "delivery_hours": 48.0}},
}
DEFAULT_TONE = {"wenren": "克制·书卷气", "gou": "幽默·松弛", "tongxinyuan": "专业·可信"}
DEFAULT_HIGH_RISK = ["publish.execute", "deploy.prod", "customer.reply", "ad.spend"]


def _num(v):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            continue
    return 0


DEFAULT_DELIVERY = {
    "_default": {"width": 1080, "height": 1920, "fps": "30/1", "video_codec": "h264",
                 "pix_fmt": ["yuv420p", "yuvj420p"], "audio_codec": "aac",
                 "sample_rate": 48000, "channels": 2,
                 "duration_tolerance": 2.5, "min_mean_db": -45.0, "max_peak_db": -1.0},
}


def _parse_delivery_cfg(path):
    """解析《项目配置》里的「交付合同」表；缺表则用内置默认（竖屏 1080×1920/30fps）。"""
    spec = {"_default": dict(DEFAULT_DELIVERY["_default"])}
    section, rows = "", 0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("## "):
                section = line[3:]
                continue
            if "交付合同" not in section or not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 10:
                continue
            pid = cells[0]
            if pid in ("项目", "项目 ID") or set(pid) <= set("-: "):
                continue
            try:
                one = dict(DEFAULT_DELIVERY["_default"])
                one.update(width=_to_int(cells[1], 1080), height=_to_int(cells[2], 1920),
                           fps=cells[3] or "30/1",
                           video_codec=cells[4] or "h264",
                           pix_fmt=[x.strip() for x in cells[5].replace("、", "|").split("|") if x.strip()]
                           or DEFAULT_DELIVERY["_default"]["pix_fmt"],
                           sample_rate=_to_int(cells[6], 48000),
                           channels=_to_int(cells[7], 2),
                           duration_tolerance=float(cells[8] or 2.5),
                           min_mean_db=float(cells[9] or -45.0),
                           max_peak_db=float(cells[10]) if len(cells) > 10 and cells[10] else -1.0)
            except (TypeError, ValueError):
                continue
            spec[pid] = one
            rows += 1
    return spec, rows


def _load_delivery_cfg():
    try:
        spec, rows = _parse_delivery_cfg(PROJECT_FILE)
        print(f"[boot] 交付合同 {PROJECT_FILE} → 专属 {rows} 项目（其余用默认 "
              f"{spec['_default']['width']}x{spec['_default']['height']}@{spec['_default']['fps']}）",
              flush=True)
        return spec
    except Exception as e:
        print(f"[boot] 交付合同解析失败({type(e).__name__})，使用内置默认", flush=True)
        return {"_default": dict(DEFAULT_DELIVERY["_default"])}


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
                # 项目表固定 6 列；其余表格（如「交付合同」11 列）不按项目解析
                if len(cells) != 6:
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
    """项目/调性/高危工具来自 Obsidian；阶段名以流程定义 pipeline.yaml 为准。

    项目配置里的「内容流水线阶段」仅作历史视图保留，不再参与流程控制，
    避免代码实际使用的 sample/final/blocked 与配置里的 video 长期漂移。
    """
    try:
        p, t, s, h = _parse_project_cfg(PROJECT_FILE)
        if p and t and h:
            if s and list(s) != PIPE.PIPELINE.item_stages:
                print(f"[boot] 提示：项目配置的阶段列表与流程定义不一致，已以 pipeline.yaml 为准"
                      f"（配置 {s} → 实际 {PIPE.PIPELINE.item_stages}）", flush=True)
            print(f"[boot] 项目配置 {PROJECT_FILE} → {len(p)} 个项目 / {len(h)} 高危工具；"
                  f"流程阶段以 {PIPE.PIPELINE.source} 为准（{len(PIPE.PIPELINE.stages)} 段）", flush=True)
            return p, t, h
        print(f"[boot] 项目配置不完整(项目{len(p)} 调性{len(t)} 高危{len(h)})，回退内置默认",
              flush=True)
    except FileNotFoundError:
        print(f"[boot] 未找到 {PROJECT_FILE}，回退内置默认", flush=True)
    except Exception as e:
        print(f"[boot] 解析项目配置失败({e})，回退内置默认", flush=True)
    return dict(DEFAULT_PROJECTS), dict(DEFAULT_TONE), list(DEFAULT_HIGH_RISK)


PIPELINE = PIPE.PIPELINE
if PIPE.LOAD_NOTE:
    print(f"[boot] 流程定义：{PIPE.LOAD_NOTE}", flush=True)
PROJECTS, TONE, HIGH_RISK_TOOLS = _load_project_cfg()
STAGES = PIPELINE.item_stages          # 对外暴露的内容单状态名，唯一来源是流程定义
DELIVERY = _load_delivery_cfg()

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


# =============================================================================
# 组织配置（Web 可编辑）：制度 md / 员工名册 / SOUL / 技能 / 记忆 / config.yaml
# 写入全部走白名单 + 路径校验 + 审计日志（logs/org-changes.jsonl），仅老板可写
# =============================================================================
ORG_PROFILES = os.environ.get("OPC_PROFILES", "/opt/sefs/hermes/profiles")
ORG_AUDIT = os.environ.get("OPC_ORG_AUDIT",
                           os.path.normpath(os.path.join(BASE_DIR, "..", "logs", "org-changes.jsonl")))
ORG_LOCK = threading.Lock()
# key → (相对 VAULT 的路径, 标题, 是否允许编辑)；登录凭据永不开放
POLICY_FILES = {
    "jobs": ("公司/岗位职责.md", "岗位职责（制度源）", True),
    "org": ("公司/组织编制.md", "组织编制", True),
    "roster": ("公司/员工名册.md", "员工名册（配置源）", True),
    "projects": ("公司/项目配置.md", "项目配置（配置源）", True),
    "kernel": ("公司/内核与编排.md", "内核与编排", True),
    "tone": ("公司/项目与调性.md", "项目与调性（自动生成视图）", False),
    "tech": ("公司/技术备忘-运行时状态重导.md", "技术备忘", False),
}
ORG_FILE_LIMIT = 2_000_000          # 单个文件读写上限（config.yaml 约 114KB）


def _org_agent_row(aid):
    for row in AGENT_SPEC:
        if row[0] == aid:
            return row
    return None


def _org_project_label(pid):
    return "全公司" if pid == "all" else PROJECTS.get(pid, {}).get("name", pid)


def _org_tone(pid):
    return "跨项目中台" if pid == "all" else TONE.get(pid, "")


def _org_profile_dir(aid):
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", str(aid or "")):
        raise ValueError("非法员工 ID")
    path = os.path.join(ORG_PROFILES, aid)
    if not os.path.isdir(path):
        raise ValueError(f"{aid} 没有独立 Hermes profile（老板为人工审批角色）")
    return path


def _org_read(path, limit=ORG_FILE_LIMIT):
    try:
        if os.path.getsize(path) > limit:
            raise ValueError(f"文件超过 {limit} 字节，拒绝网页读写")
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _org_write(path, text, expect_prefix=None):
    """原子替换写入；保留原文件权限；expect_prefix 用于锁定只允许写指定目录。"""
    real = os.path.realpath(path)
    if expect_prefix and not real.startswith(os.path.realpath(expect_prefix) + os.sep):
        raise ValueError("写入路径越界")
    if len(text.encode("utf-8")) > ORG_FILE_LIMIT:
        raise ValueError(f"内容超过 {ORG_FILE_LIMIT} 字节")
    os.makedirs(os.path.dirname(real), exist_ok=True)
    mode = 0o644
    try:
        mode = os.stat(real).st_mode & 0o777
    except FileNotFoundError:
        pass
    tmp = f"{real}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, real)


def _org_audit(user, action, target, detail=""):
    try:
        os.makedirs(os.path.dirname(ORG_AUDIT), exist_ok=True)
        with open(ORG_AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": iso_now(), "by": (user or {}).get("name", ""),
                                "role": (user or {}).get("role", ""), "action": action,
                                "target": target, "detail": str(detail)[:200]},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


def _org_skill_paths(profile_dir):
    """返回 [(相对 skills 的路径, 分类, 名称)]；支持 skills/<name> 与 skills/<分类>/<name> 两种层级。"""
    root = os.path.join(profile_dir, "skills")
    out = []
    try:
        for top in sorted(os.listdir(root)):
            top_dir = os.path.join(root, top)
            if top.startswith(".") or not os.path.isdir(top_dir):
                continue
            if os.path.isfile(os.path.join(top_dir, "SKILL.md")):
                out.append((top, "", top))
                continue
            for name in sorted(os.listdir(top_dir)):
                sub = os.path.join(top_dir, name)
                if name.startswith(".") or not os.path.isdir(sub):
                    continue
                if os.path.isfile(os.path.join(sub, "SKILL.md")):
                    out.append((os.path.join(top, name), top, name))
    except FileNotFoundError:
        pass
    return out


def _org_skills(profile_dir):
    out = []
    for rel, cat, name in _org_skill_paths(profile_dir):
        f = os.path.join(profile_dir, "skills", rel, "SKILL.md")
        out.append({"name": name, "category": cat, "bytes": os.path.getsize(f),
                    "updated": datetime.fromtimestamp(os.path.getmtime(f), TZ)
                    .isoformat(timespec="minutes")})
    return out


def _org_toolsets(profile_dir):
    """运行时真实生效的工具集（config.yaml → platform_toolsets.cli）；名册里的白名单只是意图。"""
    try:
        import yaml
        with open(os.path.join(profile_dir, "config.yaml"), encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        pt = data.get("platform_toolsets") or {}
        cli = pt.get("cli") or []
        return [str(x) for x in cli]
    except Exception:
        return []


def _org_agents():
    out = []
    for aid, name, role, pid, tools, scope in AGENT_SPEC:
        pdir = os.path.join(ORG_PROFILES, aid)
        has_profile = os.path.isdir(pdir)
        mem = os.path.join(pdir, "memories")
        out.append({
            "id": aid, "name": name, "role": role, "projectId": pid,
            "project": _org_project_label(pid), "scope": scope, "tone": _org_tone(pid),
            "tools": tools, "profile": has_profile,
            "toolsets": _org_toolsets(pdir) if has_profile else [],
            "hasSoul": os.path.isfile(os.path.join(pdir, "SOUL.md")),
            "skills": _org_skills(pdir) if has_profile else [],
            "memoryChars": {
                "memory": len(_org_read(os.path.join(mem, "MEMORY.md"))),
                "user": len(_org_read(os.path.join(mem, "USER.md"))),
            },
        })
    return out


def _org_policies():
    out = []
    for key, (rel, title, editable) in POLICY_FILES.items():
        full = os.path.join(VAULT, rel)
        out.append({"key": key, "title": title, "path": rel, "editable": editable,
                    "exists": os.path.isfile(full),
                    "bytes": os.path.getsize(full) if os.path.isfile(full) else 0})
    return out


def _org_agent_detail(aid):
    row = _org_agent_row(aid)
    if not row:
        raise ValueError(f"没有员工 {aid}")
    pdir = os.path.join(ORG_PROFILES, aid)
    detail = {
        "id": aid, "name": row[1], "role": row[2], "projectId": row[3],
        "project": _org_project_label(row[3]), "scope": row[5],
        "tone": _org_tone(row[3]), "tools": row[4],
        "profile": os.path.isdir(pdir), "toolsets": [],
        "soul": "", "memory": "", "user": "", "config": "", "skills": [],
    }
    if os.path.isdir(pdir):
        detail["toolsets"] = _org_toolsets(pdir)
        detail["soul"] = _org_read(os.path.join(pdir, "SOUL.md"))
        detail["memory"] = _org_read(os.path.join(pdir, "memories", "MEMORY.md"))
        detail["user"] = _org_read(os.path.join(pdir, "memories", "USER.md"))
        detail["config"] = _org_read(os.path.join(pdir, "config.yaml"))
        detail["skills"] = _org_skills(pdir)
    elif aid == "boss":
        detail["note"] = "老板为人工审批角色，无独立 profile"
    return detail


def _org_skill_detail(aid, name):
    pdir = _org_profile_dir(aid)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,40}", str(name or "")):
        raise ValueError("非法技能名")
    for rel, cat, nm in _org_skill_paths(pdir):
        if nm == name:
            return {"name": name, "category": cat,
                    "content": _org_read(os.path.join(pdir, "skills", rel, "SKILL.md"))}
    raise ValueError(f"{aid} 没有技能 {name}")


def _org_update_roster(aid, body):
    """改写员工名册对应行（唯一数据源），返回 (name, role, projectId, tools, scope)。"""
    row = _org_agent_row(aid)
    if not row:
        raise ValueError(f"没有员工 {aid}")
    name = str(body.get("name", row[1])).strip() or row[1]
    role = str(body.get("role", row[2])).strip() or row[2]
    pid = str(body.get("projectId", row[3])).strip() or row[3]
    scope = str(body.get("scope", row[5])).strip() or row[5]
    tools = body.get("tools", row[4])
    if not isinstance(tools, list):
        tools = [t.strip() for t in re.split(r"[、,，]", str(tools)) if t.strip()]
    tools = [str(t).strip() for t in tools if str(t).strip()]
    if pid != "all" and pid not in PROJECTS:
        raise ValueError(f"项目不存在：{pid}")
    for label, value, cap in (("姓名", name, 24), ("岗位", role, 24), ("负责范围", scope, 60)):
        if len(value) > cap or "\n" in value or "|" in value:
            raise ValueError(f"{label}不合法（≤{cap} 字、不含换行与竖线）")
    for t in tools:
        if not re.fullmatch(r"[a-z][a-z0-9._-]{1,40}", t):
            raise ValueError(f"工具名不合法：{t}")
    project_name = _org_project_label(pid)
    line = f"| {aid} | {name} | {role} | {project_name} | {scope} | {'、'.join(tools)} |\n"
    with ORG_LOCK:
        raw = _org_read(ROSTER_FILE)
        hit = False
        lines = raw.splitlines(keepends=True)
        for i, ln in enumerate(lines):
            if not ln.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) >= 6 and cells[0] == aid:
                lines[i] = line
                hit = True
                break
        if not hit:
            raise ValueError(f"名册里找不到 {aid} 的数据行")
        _org_write(ROSTER_FILE, "".join(lines), expect_prefix=VAULT)
    return name, role, pid, tools, scope, project_name


def _org_sync_soul(aid, name, role, project_label, tone):
    pdir = os.path.join(ORG_PROFILES, aid)
    path = os.path.join(pdir, "SOUL.md")
    if not os.path.isfile(path):
        return False
    txt = _org_read(path)
    new = re.sub(r"^你是 OPC 一人公司的 AI 员工「[^」]*」，岗位：[^。\n]*。",
                 f"你是 OPC 一人公司的 AI 员工「{name}」，岗位：{role}。",
                 txt, count=1, flags=re.M)
    new = re.sub(r"^负责范围：[^\n]*", f"负责范围：{project_label}", new, count=1, flags=re.M)
    new = re.sub(r"^所属项目：[^\n]*", f"所属项目：{project_label}｜风格基调：{tone}",
                 new, count=1, flags=re.M)
    if new != txt:
        _org_write(path, new, expect_prefix=pdir)
        return True
    return False


def _org_sync_org_table(aid, name, role, project_label, tone, tools):
    """组织编制是视图页，能同步就同步，失败不影响名册写入。"""
    path = os.path.join(VAULT, "公司", "组织编制.md")
    try:
        txt = _org_read(path)
        lines = txt.splitlines(keepends=True)
        skills = "、".join(dict.fromkeys(s["category"] or s["name"] for s in _org_skills(
            os.path.join(ORG_PROFILES, aid))))
        for i, ln in enumerate(lines):
            if not ln.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) >= 10 and cells[1] == aid:
                cells[2], cells[3], cells[4], cells[5] = name, role, project_label, tone
                if skills:
                    cells[7] = skills
                cells[8] = "、".join(tools)
                lines[i] = "| " + " | ".join(cells) + " |\n"
                _org_write(path, "".join(lines), expect_prefix=VAULT)
                return True
    except (OSError, ValueError):
        pass
    return False


def _org_validate_config(text):
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception as e:
        raise ValueError(f"config.yaml 解析失败：{type(e).__name__}: {e}")
    if not isinstance(data, dict):
        raise ValueError("config.yaml 顶层必须是映射")
    if not isinstance(data.get("model", {}).get("default") if isinstance(data.get("model"), dict) else None, str):
        raise ValueError("config.yaml 缺少 model.default")
    return True


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
KERNEL = None  # 延迟绑定，见下方 Kernel


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
    STATE_FIELDS = ("selected", "final", "eliminated", "pool_size", "meeting_doc",
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
        self._kb_stamp = DL.knowledge_stamp() if DL else (0, 0.0)   # 知识库目录指纹
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
        self.journal_file = os.path.join(VAULT, "动态", "runtime.json")
        self.db = None
        self._restore_history()
        # 过程数据入库：影子期双写，内存仍是读路径；库不可用时全部跳过
        self.db = DBX.store() if DBX else None
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
        if INGEST_ON and ING:
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

    def _mark_interrupted_runs(self):
        """启动时把上次进程留下的 running 运行态标成 interrupted；按需自动续跑。"""
        if not self.db:
            return
        try:
            rows = self.db.pipeline_runs("running")
        except Exception as e:
            print(f"[boot] 读取运行态失败：{type(e).__name__}: {e}", flush=True)
            return
        for row in rows:
            trace = row.get("trace_id")
            try:
                self.db.pipeline_run_end(trace, "interrupted")
            except Exception:
                pass
            with self.lock:
                if self.traces.get(trace, {}).get("status") == "running":
                    self.traces[trace]["status"] = "interrupted"
                    self.traces[trace]["closedAt"] = iso_now()
            print(f"[boot] 上次未跑完：{trace} 停在阶段 {row.get('stage')}（已标记 interrupted）", flush=True)
        if rows and os.environ.get("OPC_RESUME_AUTO", "0") == "1":
            for row in rows:
                try:
                    self.resume_pipeline(row.get("trace_id"), auto=True)
                except Exception as e:
                    print(f"[boot] 自动续跑失败 {row.get('trace_id')}：{type(e).__name__}: {e}", flush=True)

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
                self.messages = DL.load_recent_messages(120)
            except Exception as e:
                print(f"[boot] 恢复历史消息失败：{type(e).__name__}: {e}", flush=True)
        if DL:
            if not self.hotspots:
                try:
                    self.hotspots = DL.load_hotspots()
                except Exception as e:
                    print(f"[boot] 恢复历史热点失败：{type(e).__name__}: {e}", flush=True)
            # Obsidian 是唯一真源：md 文件优先，runtime.json 只在没有 md 时兜底
            try:
                disk_kb = DL.load_knowledge(limit=200)
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
        global PROJECTS, TONE, STAGES, HIGH_RISK_TOOLS, AGENT_SPEC, PROJECT_BY_NAME, PIPELINE
        p, t, h = _load_project_cfg()
        missing_proj = {"wenren", "gou", "tongxinyuan"} - set(p)
        if missing_proj:
            raise ValueError(f"项目配置缺少必需项目: {sorted(missing_proj)}")
        spec = _load_agent_spec()
        known = {a[0] for a in spec}
        missing_ids = [a[0] for a in DEFAULT_AGENT_SPEC if a[0] not in known]
        if missing_ids:
            raise ValueError(f"员工名册缺少必需岗位: {missing_ids}")
        # 流程定义也支持热加载（改 pipeline.yaml 后无需重启）；校验失败会回退内置默认
        PIPELINE = PIPE.load()[0]
        PROJECTS, TONE, HIGH_RISK_TOOLS = p, t, h
        STAGES = PIPELINE.item_stages
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
        if DL:
            try:
                status = DL.load_collection_status()
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
            base = PROJECTS[pid]["name"]
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
        return {"durationSec": _to_int(src.get("durationSec"), topic.get("durationSec", 45)),
                "tts": {"voice": str(tts.get("voice") or "zh-CN-XiaoxiaoNeural")[:40],
                        "style": str(tts.get("style") or "纪录片口播")[:40]},
                "materials": [str(x)[:60] for x in (src.get("materials") or [])][:8],
                "copyBrief": str(src.get("copyBrief") or "按定稿选题写口播稿")[:200],
                "storyboardNotes": str(src.get("storyboardNotes") or "4-6 镜，首镜 3 秒抓人")[:300]}

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
                    f"以 {PROJECTS[pid]['name']} 产品经理身份评审本项目样片，判断能否进入定版。",
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
        stages = [st for st in PIPELINE.stages if st.enabled]
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
            if fn(c) is _PIPELINE_STOP:
                return

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
                f"提交 {PROJECTS[pid]['name']} 今日 4 条候选选题，供 CCO 与 CTO 汇总。",
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
        minutes = str(data.get("minutes") or "").strip() or (
            f"# 选题评审会\n\n- 议题：{c.hotspot['title']}\n- 参会：CEO 陆清安、CTO 扶频、CCO 沈青禾、"
            f"产品经理 贺平生/米珂/姜月初\n- 结论：每项目定稿 2 条，共 6 条\n")
        recipients = [str(x) for x in (data.get("recipients") or [])] or \
            ["ceo", "cto", "cco", "pm_wenren", "pm_gou", "pm_tongxinyuan"]
        cc = [str(x) for x in (data.get("cc") or [])] or ["owner"]
        display = lambda xs: ["老板" if x == "owner" else self.agents.get(x, {}).get("name", x) for x in xs]
        c.meeting_doc = {}
        if DL:
            try:
                c.meeting_doc = DL.export_meeting(c.trace, {
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
            f"{PROJECTS[pid]['name']}《{t['title']}》" for pid in self.PIDS for t in c.final[pid])
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
            mail_lines.append(f"### {PROJECTS[pid]['name']}")
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
        c.items = {}
        for pid in self.PIDS:
            for t in c.final[pid]:
                item = {"id": uid("c"), "projectId": pid, "traceId": c.trace, "stage": "topic",
                        "title": t["title"], "angle": t.get("angle", ""),
                        "owner": f"pm_{pid}", "toneProfile": TONE[pid], "assets": [],
                        "reviewNote": "", "durationSec": t.get("durationSec", 45)}
                with self.lock:
                    self.contents.append(item)
                c.items[item["id"]] = item
                self.emit("pipeline", c.trace, item)
            self.send("delegation", f"pm_{pid}", f"cw_{pid}", c.trace,
                      {"task": f"承接本项目 {len(c.final[pid])} 条定稿选题，先出制作评估再写稿"}, project=pid)
            self.send("delegation", f"pm_{pid}", f"vd_{pid}", c.trace,
                      {"task": f"承接本项目 {len(c.final[pid])} 条定稿选题，先出制作评估再制作样片"}, project=pid)


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


    def _stage_assessment(self, c):
        """5. PM 组织本项目组出《制作评估报告》：文案要求、分镜、时长、配音/TTS、素材"""
        # 5. PM 组织本项目组出《制作评估报告》：文案要求、分镜、时长、配音/TTS、素材
        jobs = {}
        for pid, pm in c.pms:
            jobs[pid] = HERMES.pool.submit(
                self.call_agent, pm,
                f"为 {PROJECTS[pid]['name']} 的 2 条定稿选题组织本项目组：给出文案要求、分镜剧本要点、"
                "时长评估、配音/TTS 方案与素材清单，形成《制作评估报告》，并列出素材缺口。",
                json.dumps(c.final[pid], ensure_ascii=False),
                "额外 JSON 字段：assessment({items:[{title,durationSec,tts:{voice,style},"
                "materials:[],copyBrief,storyboardNotes}], summary})；assetGaps(数组，每条 {item, why})。",
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
        """6. 文案成稿（6 个真实调用）"""
        # 6. 文案成稿（6 个真实调用）
        jobs = {}
        for iid, item in c.items.items():
            pid = item["projectId"]
            plan = item.get("plan") or {}
            jobs[iid] = HERMES.pool.submit(
                self.call_agent, f"cw_{pid}",
                f"为《{item['title']}》写一段成稿文案（≤80字），符合本项目人设，禁止串用其他项目风格。",
                f"项目：{PROJECTS[pid]['name']}；风格：{TONE[pid]}；时长：{plan.get('durationSec', 45)}s；"
                f"文案要求：{plan.get('copyBrief', '')}",
                "额外 JSON 字段：copy(成稿文案≤80字)。",
                "summary, evidence, confidence, tools, copy")
            self.send("delegation", f"pm_{pid}", f"cw_{pid}", c.trace,
                      {"task": f"按评估报告写《{item['title']}》成稿"}, project=pid)
        for iid, item in c.items.items():
            pid = item["projectId"]
            data, meta = jobs[iid].result()
            if meta.get("engine") != "hermes" or not (data or {}).get("copy"):
                item.setdefault("degraded", []).append("copy")
            data = data or {"summary": "文案已交付（降级）", "evidence": [], "confidence": 0.2,
                            "tools": [], "copy": f"{PROJECTS[pid]['name']} 文案占位"}
            item["stage"] = "copy"
            item["copy"] = data.get("copy") or data.get("summary", "")
            self.send("result", f"cw_{pid}", f"pm_{pid}", c.trace,
                      {"result": data, "itemId": iid, "engine": meta["engine"],
                       "latencyMs": meta.get("latencyMs", 0)}, project=pid)
            self.items_emit(c.trace, item)


    def _stage_sample(self, c):
        """7. 视频制作出样片（6 个真实调用）"""
        # 7. 视频制作出样片（6 个真实调用）
        jobs = {}
        for iid, item in c.items.items():
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
        """7.5 真实出片：MCP-TTS 旁白 + OpenMontage 渲染 + 画面验收门"""
        # 7.5 真实出片：MCP-TTS 旁白 + OpenMontage 渲染 + 画面验收门
        c.render_blocked = set()
        for idx, (iid, item) in enumerate(c.items.items(), 1):
            pid = item["projectId"]
            self.send("biz.event", f"vd_{pid}", "ceo", c.trace,
                      {"text": f"真实出片 {idx}/{len(c.items)}：《{item['title']}》", "step": "render"},
                      project=pid, priority=2)
            if os.environ.get("OPC_SKIP_RENDER") == "1":
                files = item.get("files") or {}
                item["renderCheck"] = {"pass": True, "reasons": ["测试模式：跳过真实渲染"]}
                item["assets"] = [files.get("video", ""), files.get("cover", "")]
                item["renderSkipped"] = True
            elif not OMR:
                item["renderCheck"] = {"pass": False, "reasons": ["渲染模块不可用"]}
            else:
                res = OMR.produce(item, PROJECTS[pid], TONE.get(pid, ""),
                                  DELIVERY.get(pid) or DELIVERY["_default"])
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
                    f"项目：{PROJECTS[pid]['name']}；风格：{TONE[pid]}；评审意见：{reasons}",
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
                        res = OMR.produce(item, PROJECTS[pid], TONE.get(pid, ""),
                                          DELIVERY.get(pid) or DELIVERY["_default"])
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
                f"项目：{PROJECTS[pid]['name']}；文案：{item.get('copy', '')}；"
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
                    dst = DL._local(final_url)
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
                    f"项目：{PROJECTS[pid]['name']}；风格：{TONE[pid]}",
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
                self.call_agent, f"pm_{pid}", f"写一段 {PROJECTS[pid]['name']} 项目周报结论。",
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
                      "title": f"{PROJECTS[pid]['name']} 项目周报",
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
            prefix = DL.plan(item["traceId"], item["projectId"], item["id"])["dir"] + "/"
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
            cand = DL._local(a) if (DL and str(a).startswith("/vault/")) else str(a)
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
        spec = DELIVERY.get(pid) or DELIVERY.get("_default") or DEFAULT_DELIVERY["_default"]
        checks_spec = {"projectId": pid, "source": "项目配置.md#交付合同" if pid in DELIVERY else "内置默认",
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
            f"项目：{PROJECTS[item['projectId']]['name']}；要求风格：{expected}；样片三方评审：{sample_note}",
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
        if not appr and self.db:
            appr = self._approval_from_db(approval_id)   # 重启后内存里没有，从过程库补
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
        """审批放行后自动续跑：仅当该 trace 在过程库里是 interrupted（重启留下的断点）。"""
        if not self.db or not trace:
            return False
        try:
            run = self.db.pipeline_run(trace)
        except Exception as e:
            print(f"[approval] 读运行态失败，跳过自动续跑：{type(e).__name__}: {e}", flush=True)
            return False
        if not run or run.get("status") != "interrupted":
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
        stamp = DL.knowledge_stamp()
        if stamp == self._kb_stamp:
            return
        items = DL.load_knowledge(limit=200)
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
        """项目配置/员工名册改动后自动热加载，不用手动点重载。"""
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
            if DL:                                   # 写回 Obsidian，文件与 Web 同步落地
                for k in made:
                    try:
                        DL.save_knowledge(k)
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

    def _owner_only(self):
        """统一消息只对老板开放：其他角色 403。"""
        user = self._session_user() or {}
        if "老板" in str(user.get("role") or ""):
            return True
        self._json({"ok": False, "error": "forbidden"}, 403)
        return False

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

    def _org_save(self, path, body, user):
        """组织配置写入：白名单 + 路径校验 + 审计；返回 applied=immediate|restart。"""
        try:
            if path == "/api/org/reload":
                info = KERNEL.reload_config()
                _org_audit(user, "config.reload", "公司/员工名册.md", str(info))
                return self._json({"ok": True, "applied": "immediate", **info})
            if path == "/api/org/policy":
                key = str(body.get("key") or "")
                if key not in POLICY_FILES:
                    return self._json({"ok": False, "error": "未知制度文件"}, 404)
                rel, title, editable = POLICY_FILES[key]
                if not editable:
                    return self._json({"ok": False, "error": f"{title} 为只读视图"}, 403)
                content = str(body.get("content") or "")
                if not content.strip():
                    return self._json({"ok": False, "error": "内容为空，拒绝写入"}, 400)
                _org_write(os.path.join(VAULT, rel), content, expect_prefix=VAULT)
                _org_audit(user, "policy.save", rel, f"{len(content)} chars")
                return self._json({"ok": True, "applied": "immediate", "key": key,
                                   "path": rel, "bytes": len(content.encode('utf-8'))})
            parts = [p for p in path[len("/api/org/agent/"):].split("/") if p]
            if not parts:
                return self._json({"ok": False, "error": "缺少员工 ID"}, 400)
            aid = parts[0]
            if len(parts) == 1:
                name, role, pid, tools, scope, project_label = _org_update_roster(aid, body)
                tone = _org_tone(pid)
                soul = _org_sync_soul(aid, name, role, project_label, tone)
                _org_sync_org_table(aid, name, role, project_label, tone, tools)
                info = KERNEL.reload_config()
                _org_audit(user, "agent.update", aid,
                           f"{name}/{role}/{project_label}/{scope}/{','.join(tools)} soul={soul}")
                return self._json({"ok": True, "applied": "immediate", "soulSynced": soul,
                                   "agent": _org_agent_detail(aid), "reload": info})
            action = parts[1]
            pdir = _org_profile_dir(aid)
            if action == "soul":
                content = str(body.get("content") or "")
                if not content.strip():
                    return self._json({"ok": False, "error": "SOUL 内容为空"}, 400)
                _org_write(os.path.join(pdir, "SOUL.md"), content, expect_prefix=pdir)
                _org_audit(user, "soul.save", aid, f"{len(content)} chars")
                return self._json({"ok": True, "applied": "immediate", "path": "SOUL.md"})
            if action == "memory":
                mem_dir = os.path.join(pdir, "memories")
                changed = []
                for key, fname in (("memory", "MEMORY.md"), ("user", "USER.md")):
                    if key in body:
                        content = str(body.get(key) or "")
                        _org_write(os.path.join(mem_dir, fname), content, expect_prefix=pdir)
                        changed.append(f"{fname}:{len(content)}")
                        _org_audit(user, "memory.save", f"{aid}/{fname}", f"{len(content)} chars")
                if not changed:
                    return self._json({"ok": False, "error": "没有提供 memory 或 user"}, 400)
                return self._json({"ok": True, "applied": "immediate", "changed": changed})
            if action == "skill":
                name = str(body.get("name") or "")
                if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,40}", name):
                    return self._json({"ok": False, "error": "技能名只能用小写字母/数字/-/_（2-41 位）"}, 400)
                content = str(body.get("content") or "")
                if not content.strip():
                    return self._json({"ok": False, "error": "技能内容为空"}, 400)
                target = os.path.join(pdir, "skills", name, "SKILL.md")
                for rel, _cat, nm in _org_skill_paths(pdir):
                    if nm == name:
                        target = os.path.join(pdir, "skills", rel, "SKILL.md")
                        break
                _org_write(target, content, expect_prefix=pdir)
                _org_audit(user, "skill.save", f"{aid}/{name}", f"{len(content)} chars")
                return self._json({"ok": True, "applied": "immediate", "name": name})
            if action == "config":
                content = str(body.get("content") or "")
                _org_validate_config(content)
                _org_write(os.path.join(pdir, "config.yaml"), content, expect_prefix=pdir)
                _org_audit(user, "config.save", f"{aid}/config.yaml", f"{len(content)} chars")
                return self._json({"ok": True, "applied": "next-call",
                                   "note": "config.yaml 已保存；Hermes 每次 -z 调用都会重读，无需重启服务"})
            return self._json({"ok": False, "error": "not found"}, 404)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

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
        # 根路径默认进入新版工作台；OPC_HOME_LEGACY=1 时放行走后面的旧版分支
        if path == "/" and not HOME_LEGACY:
            self.send_response(302)
            self.send_header("Location", "/app")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # 静态资源与交付物不拦登录（交付链接要能在外部浏览器直接打开）
        # 旧版控制台 / 登录页 / 3D 页未显式声明 icon，浏览器会请求 /favicon.ico；
        # 以前返回 404，控制台出现「Failed to load resource」被误判为 3D 加载失败。
        if path == "/favicon.ico":
            icon = (
                b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">'
                b'<circle cx="20" cy="20" r="19" fill="#2f6fed"/>'
                b'<circle cx="20" cy="20" r="13.5" fill="none" stroke="#fff" stroke-width="1.6" opacity=".7"/>'
                b'<circle cx="20" cy="20" r="8.5" fill="none" stroke="#fff" stroke-width="2"/>'
                b'<circle cx="20" cy="20" r="3.2" fill="#fff"/></svg>'
            )
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(icon)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(icon)
            return
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
        # 新版前端（Refine）外壳与构建资源公开：页面本身不含数据，数据接口仍在 /api 层鉴权
        # 带内容哈希的 JS/CSS 用长缓存，其余（含 index.html）每次校验，避免发版后拿到旧壳
        if path.startswith("/app/assets/"):
            return self._web_dist(path[len("/app/"):], immutable=True)
        if path == "/app/legacy":
            self.send_response(302)
            self.send_header("Location", "/legacy")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/app":
            return self._web_dist("index.html")
        if path.startswith("/app/"):
            return self._web_dist(path[len("/app/"):])
        # 素材库文件只读发布（可外部浏览器直接打开，与交付链接同口径不拦登录）
        if path.startswith("/materials/"):
            return self._material(path[len("/materials/"):])
        if path == "/api/materials":
            return self._json(self._materials_index())
        # 其余页面与接口一律要求登录
        if not self._need_login(path):
            return
        if path == "/api/health":
            return self._json({"ok": True, "version": "3.0", "engine": MODE,
                               "hermes": HERMES.stats(), "time": iso_now()})
        # /legacy 始终可用：新版顶栏与 /app/legacy 用它回退旧控制台
        if path in ("/", "/legacy", "/index.html"):
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
        if path == "/api/knowledge":
            return self._json(KERNEL.knowledge_page(urlparse(self.path).query))
        if path.startswith("/api/knowledge/"):
            rec = KERNEL.knowledge_detail(path.rsplit("/", 1)[-1])
            return self._json(rec) if rec else self._json({"error": "knowledge not found"}, 404)
        # ---------- 作品档案（交付物统一管理：名称 + 时间 + 处理流程） ----------
        if path == "/api/works":
            if WK is None:
                return self._json({"ok": False, "error": "作品档案模块未加载"}, 500)
            with KERNEL.lock:
                approvals = list(KERNEL.approvals)
            return self._json(WK.page(parse_qs(urlparse(self.path).query), approvals))
        if path.startswith("/api/works/"):
            if WK is None:
                return self._json({"ok": False, "error": "作品档案模块未加载"}, 500)
            with KERNEL.lock:
                approvals = list(KERNEL.approvals)
            rec = WK.detail(path[len("/api/works/"):], approvals)
            return self._json(rec) if rec else self._json({"error": "work not found"}, 404)
        if path == "/api/hotspots":
            return self._json(KERNEL.hotspot_page(urlparse(self.path).query))
        if path.startswith("/api/hotspots/"):
            rec = KERNEL.hotspot_detail(path.rsplit("/", 1)[-1])
            return self._json(rec) if rec else self._json({"error": "hotspot not found"}, 404)
        if path == "/api/state":
            return self._json(KERNEL.snapshot())
        # ---------- CCO 创意灵感 / CTO 运维监控（只读聚合） ----------
        if path == "/api/inspiration":
            if insights is None:
                return self._json({"ok": False, "error": "灵感聚合模块未加载"}, 500)
            try:
                refresh = (parse_qs(urlparse(self.path).query).get("refresh") or [""])[0] == "1"
                return self._json(insights.build(KERNEL, refresh=refresh))
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        if path == "/api/ops":
            if ops_monitor is None:
                return self._json({"ok": False, "error": "运维聚合模块未加载"}, 500)
            try:
                health = {"ok": True, "version": "3.0", "engine": MODE,
                          "hermes": HERMES.stats(), "time": iso_now()}
                return self._json(ops_monitor.build(KERNEL.snapshot(), health))
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
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
        # ---------- 统一消息（飞书 / APP / Web，只读） ----------
        if path.startswith("/api/chat/"):
            if not self._owner_only():
                return
            qs = parse_qs(urlparse(self.path).query)
            arg = lambda k, d="": (qs.get(k) or [d])[0]
            if not KERNEL.db:
                return self._json({"ok": False, "enabled": False, "error": "过程库未启用"}, 503)
            try:
                if path == "/api/chat/threads":
                    rows = KERNEL.db.chat_threads(arg("source"), _to_int(arg("limit", "50"), 50),
                                                  arg("before"))
                    return self._json({"ok": True, "enabled": True, "threads": rows})
                if path == "/api/chat/messages":
                    rows = KERNEL.db.chat_messages(
                        thread=arg("thread"), source=arg("source"), modality=arg("modality"),
                        direction=arg("direction"), q=arg("q"),
                        before_id=_to_int(arg("before", "0"), 0),
                        limit=_to_int(arg("limit", "50"), 50))
                    return self._json({"ok": True, "enabled": True, "messages": rows})
                if path == "/api/chat/stats":
                    return self._json({"ok": True, "enabled": True,
                                       "stats": KERNEL.db.chat_stats(_to_int(arg("days", "7"), 7))})
                if path == "/api/chat/export":
                    rows = KERNEL.db.chat_export(arg("start"), arg("end"),
                                                 _to_int(arg("limit", "10000"), 10000))
                    return self._json({"ok": True, "enabled": True, "count": len(rows), "messages": rows})
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._json({"ok": False, "error": "not found"}, 404)
        # ---------- 组织配置（制度 / 岗位 / 人员 / 性格 / 技能 / 记忆） ----------
        if path == "/api/org":
            try:
                return self._json({
                    "ok": True, "agents": _org_agents(), "policies": _org_policies(),
                    "profilesRoot": ORG_PROFILES,
                    "projects": [{"id": k, "name": v["name"]} for k, v in PROJECTS.items()]
                               + [{"id": "all", "name": "全公司"}],
                })
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        if path == "/api/org/policy":
            key = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
            if key not in POLICY_FILES:
                return self._json({"ok": False, "error": "未知制度文件"}, 404)
            rel, title, editable = POLICY_FILES[key]
            try:
                return self._json({"ok": True, "key": key, "title": title, "path": rel,
                                   "editable": editable,
                                   "content": _org_read(os.path.join(VAULT, rel))})
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        if path == "/api/org/audit":
            limit = _to_int((parse_qs(urlparse(self.path).query).get("limit") or ["50"])[0], 50)
            lines = []
            try:
                with open(ORG_AUDIT, encoding="utf-8") as f:
                    lines = f.readlines()[-max(1, min(limit, 500)):]
            except FileNotFoundError:
                pass
            return self._json({"ok": True, "items": [json.loads(x) for x in lines if x.strip()]})
        if path.startswith("/api/org/agent/"):
            parts = [p for p in path[len("/api/org/agent/"):].split("/") if p]
            aid = parts[0] if parts else ""
            try:
                if len(parts) == 1:
                    return self._json({"ok": True, "agent": _org_agent_detail(aid)})
                if len(parts) == 2 and parts[1] == "skill":
                    name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
                    return self._json({"ok": True, "skill": _org_skill_detail(aid, name)})
                return self._json({"ok": False, "error": "not found"}, 404)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
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

    def _web_dist(self, rel, immutable=False):
        """提供 Refine 前端构建产物（web/dist）；未命中的路径回退 index.html 以支持 SPA 路由。"""
        root = WEB_DIST
        target = os.path.normpath(os.path.join(root, rel or "index.html"))
        if not (target == root or target.startswith(root + os.sep)):
            return self._json({"error": "bad path"}, 404)
        if not os.path.isfile(target):
            target = os.path.join(root, "index.html")
        if not os.path.isfile(target):
            return self._json({"error": "web/dist 未构建，先在 web/ 执行 npm run build"}, 404)
        body = open(target, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", WEB_MIME.get(os.path.splitext(target)[1].lower(),
                                                     "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        if immutable:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _materials_index(self):
        """首页「素材库」清单：读 prototype/materials.json，文件改了下次刷新即生效。"""
        try:
            with open(MATERIALS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {"ok": False, "error": "materials.json 缺失", "items": []}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "items": []}
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = []
        out = []
        for it in items:
            if not isinstance(it, dict) or not it.get("url"):
                continue
            out.append({
                "id": str(it.get("id") or ""),
                "title": str(it.get("title") or "未命名素材"),
                "category": str(it.get("category") or "其他"),
                "desc": str(it.get("desc") or ""),
                "url": str(it["url"]),
                "meta": str(it.get("meta") or ""),
            })
        return {"ok": True, "updated": str(data.get("updated") or ""), "items": out}

    def _material(self, rel):
        """素材库只读发布：/materials/... → MATERIALS_ROOT/...（限定目录、无隐藏目录）。"""
        root = os.path.realpath(MATERIALS_ROOT)
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        if any(p.startswith(".") for p in os.path.relpath(full, root).split(os.sep)):
            return self._json({"error": "not found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".md": "text/plain; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".srt": "text/plain; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif",
            ".ttf": "font/ttf", ".otf": "font/otf", ".woff2": "font/woff2",
            ".mp4": "video/mp4",
            ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
            ".pdf": "application/pdf",
        }.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
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
                 ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                 ".gif": "image/gif", ".svg": "image/svg+xml",
                 ".pdf": "application/pdf",
                 ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
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
            meta = dict(KERNEL.traces.get(trace) or {})
            if trace not in KERNEL.traces and not msgs and not contents:
                if KERNEL.db:
                    return self._trace_from_db(trace)
                return None
            out = {"traceId": trace, "meta": meta,
                   "messages": msgs, "contents": contents,
                   "approvals": [a for a in KERNEL.approvals if a.get("traceId") == trace],
                   "reports": [r for r in KERNEL.reports if r.get("traceId") == trace],
                   "knowledge": [k for k in KERNEL.knowledge if k.get("traceId") == trace],
                   "messageCount": len(msgs)}
        # 内存里没有这条 trace 的元数据（重启后）或事件：从过程库补齐
        if KERNEL.db:
            db_meta, events = self._db_trace_parts(trace)
            if not out["meta"] and db_meta:
                out["meta"] = db_meta
            if events:
                out["events"] = events
                out["source"] = "db+mem" if msgs else "db"
        return out

    def _db_trace_parts(self, trace):
        row = KERNEL.db.trace_row(trace)
        if not row:
            return None, []

        def dec(value):
            try:
                return json.loads(value) if value else None
            except (TypeError, ValueError):
                return value

        meta = {"traceId": row.get("trace_id"), "status": row.get("status"),
                "startedAt": row.get("started_at"), "closedAt": row.get("closed_at"),
                "engine": row.get("engine"),
                "hotspot": {"id": row.get("hotspot_id"), "title": row.get("hotspot_title")}}
        for src, dst in (("review_gate", "reviewGate"), ("review_history", "reviewHistory"),
                         ("hermes", "hermes"), ("meta", "meta")):
            if row.get(src):
                meta[dst] = dec(row[src])
        if row.get("abort_reason"):
            meta["abortReason"] = row["abort_reason"]
        return meta, KERNEL.db.trace_events(trace, 500)

    def _trace_from_db(self, trace):
        """内存已随重启清空的历史 trace：从过程库还原骨架 + 事件序列。"""
        meta, events = self._db_trace_parts(trace)
        if meta is None:
            return None
        return {"traceId": trace, "meta": meta, "events": events,
                "messages": [e["data"] for e in events
                             if e.get("kind") == "message" and isinstance(e.get("data"), dict)],
                "contents": [], "approvals": [], "reports": [], "knowledge": [],
                "messageCount": sum(1 for e in events if e.get("kind") == "message"),
                "source": "db"}

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
        # ---------- 组织配置写入（仅老板） ----------
        if path.startswith("/api/org/"):
            if user.get("role") != "老板":
                return self._json({"ok": False, "error": "forbidden",
                                   "reason": "仅老板可修改制度、岗位、人员、性格、技能与记忆"}, 403)
            return self._org_save(path, body, user)
        if path == "/api/patrol":
            try:
                return self._json({"ok": True, **KERNEL.patrol()})
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path == "/api/hotspots/inject":
            try:
                trace, hotspot = KERNEL.inject_hotspot(body.get("title"), body.get("tags"), body.get("score"))
            except BusyError as e:
                return self._json({"ok": False, "error": str(e), "busy": True,
                                   "traceId": e.traceId}, 409)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json({"ok": True, "traceId": trace, "hotspot": hotspot})
        if path == "/api/meetings":
            try:
                trace, hs = KERNEL.inject_hotspot(body.get("title"), body.get("tags"), body.get("score"))
            except BusyError as e:
                return self._json({"ok": False, "error": str(e), "busy": True,
                                   "traceId": e.traceId}, 409)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json({"ok": True, "traceId": trace, "meeting": "daily_topic", "hotspot": hs})
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
        # ---------- CTO 运维处置：把巡检派单 / 热加载 / 审批收敛到一处（仅老板） ----------
        if path == "/api/ops/recover":
            if user.get("role") != "老板":
                return self._json({"ok": False, "error": "forbidden",
                                   "reason": "仅老板可执行运维恢复动作"}, 403)
            action = str(body.get("action") or "patrol")
            who = user.get("name") or user.get("user") or ""
            try:
                if action == "patrol":
                    result = KERNEL.patrol()
                elif action == "reload":
                    result = KERNEL.reload_config()
                elif action in ("approve", "reject"):
                    aid = str(body.get("id") or "")
                    if not aid:
                        return self._json({"ok": False, "error": "缺少审批 id"}, 400)
                    appr = KERNEL.decide_approval(aid, action, who)
                    if not appr:
                        return self._json({"ok": False, "error": "approval not found"}, 404)
                    result = {"approval": appr}
                elif action == "resume":
                    tid = str(body.get("traceId") or body.get("id") or "")
                    if not tid:
                        return self._json({"ok": False, "error": "缺少续跑的 traceId"}, 400)
                    result = KERNEL.resume_pipeline(tid)
                else:
                    return self._json({"ok": False, "error": "invalid action",
                                       "expected": "patrol|reload|approve|reject|resume"}, 400)
            except (ValueError, RuntimeError) as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._json({"ok": True, "action": action, "result": result,
                               "by": who, "at": iso_now()})
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
            KERNEL._web_chat("inbound", text, "command", "owner", "老板", uid("web"))
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
                try:
                    trace, hs = KERNEL.inject_hotspot()
                except BusyError as e:
                    return self._json({"ok": False, "error": str(e), "busy": True,
                                       "traceId": e.traceId}, 409)
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
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
        if KERNEL.db:
            KERNEL.db.run_end(KERNEL._run_id)
            KERNEL.db.close()


if __name__ == "__main__":
    main()
