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
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import io
import shutil
import zipfile
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
from urllib.request import urlopen as _urlopen, Request as _UrlRequest

# hermes 本体（config set 等本地子命令用；不经 run-hermes.sh 的 DNS 隔离包装）
# 大模型注册表（llm_models 表）的内置初始条目：仅在表为空时写入，之后以库里/CTO 修改为准
# priority 小者优先；alias 是 hermes 调用时附加的 --model 别名（空=内置默认模型）
# CTO 成本护栏：单轮 trace 累计 / 单次调用 token 上限；金额阈值默认关闭（0=不启用）
# 标定依据（2026-11-07 实测，hermes --usage-file 的 total_including_auxiliary.total_tokens）：
#   单次员工调用（含工具轮次）约 7k–650k；选题阶段约 2.6M；一轮完整编排实测 12.4M（含返工）。
# 默认阈值取 20M / 2M：正常整轮不触发，只拦截真正的失控消耗；可用环境变量按项目收紧。
# 首页默认进入新版 /app；设 OPC_HOME_LEGACY=1 可一键回退到旧版控制台（不改代码、不丢 /app）














# 配置以 Obsidian 为数据源：项目配置 + 员工名册；文件缺失或解析异常时回退内置默认

# 登录凭据放在 Obsidian 公司目录，每次登录实时读取（改文件即生效，不用重启服务）
# 素材库：首页卡片的数据源 + 只读文件发布目录（materials.json 里写相对路径即可）
# 多用户工作区：每个注册用户一个目录（opc.db / materials.json / workspace / 名册）；
# 默认放在 /mnt/sefs/conring/users/<手机号>/，存量数据归属 OWNER_PHONE（默认 18911789286）
# 表头别名 → 内部字段名；列顺序任意，缺列就留空






# =============================================================================
# 多用户：注册（手机号 + 密码 + 校验码）→ 独立工作区 → 按用户 Kernel/过程库
# =============================================================================











# ---------------- 简单校验码（无状态 HMAC 算术题，5 分钟有效） ----------------











# ---------------------------------------------------------------- 内部邮件






# 内容流水线定义（声明式）：阶段顺序/门禁/轮次以 config/pipeline.yaml 为准
import pipeline_defs as PIPE
# ---------------- 模块化拆分后的组合根：功能模块见下列各文件 ----------------
from opc_paths import *  # noqa: F401,F403 —— 路径与运行常量
from utils import iso_now, uid, _to_int, _hotkey, _step_view, _iso_shift, _num
from agent_defaults import DEFAULT_AGENT_SPEC, AGENTS_REQUIRED
import company_config as CC
import users as USERS
import org as ORG
import llm_registry as LREG
import mail as MAILM
from hermes_adapter import HERMES
from deps import *  # noqa: F401,F403 —— 基础设施单例（DBX/ING/DL/WK/OMR/asset_fill/insights/ops_monitor）
from kernel import BusyError, _kernel_for, _kernel_reset
import voice_bridge
from users import (SESSIONS, SESS_LOCK, SESSION_COOKIE, SESSION_TTL,
                   LOGIN_FAILS, LOGIN_LOCK)




# 制片工厂的 13 类工艺基准：与 OpenMontage pipeline_defs/*.yaml 一一对应。
# 每个工艺一个"车间"（编号 01-13），CCO 按产品选题参考此表做选择并落库 product_crafts。

# ---- 工艺步骤（每个工艺的工序序列与名称都不同，CCO 定工艺时即定格） ----
# 定义源：/opt/sefs/OpenMontage/pipeline_defs/<craft_id>.yaml 的 stages 序列。
# OM 步骤 → 中文工序名（未收录的回退原词）
# OM 步骤 → OPC 流水线阶段（工厂中段）：工位进度按 OPC 阶段的 workshop_steps 映射





# 制片工厂的进/出厂边界（流水线 16 段中只有中段是工厂工序）：
# 进厂 = CCO 审批（工艺决策，在 assessment 内发流水号）；
# 出厂 = 质检审核（review）通过。前段（选题/开工审批）与后段（发布/归档/汇报）在 hermes 调度。














if PIPE.LOAD_NOTE:
    print(f"[boot] 流程定义：{PIPE.LOAD_NOTE}", flush=True)
CC.STAGES = CC.PIPELINE.item_stages    # 对外暴露的内容单状态名，唯一来源是流程定义

# 内置默认编制：与 Obsidian 员工名册（19 人，boss 不占编制）保持一致，用于
# ① owner 名册文件缺失时的回退；② 注册用户的默认编制模板（USERS._user_roster_md）

# 员工编制以 Obsidian 名册为唯一数据源；文件缺失或解析异常时回退上面的内置编制








# =============================================================================
# 组织配置（Web 可编辑）：制度 md / 员工名册 / SOUL / 技能 / 记忆 / config.yaml
# 写入全部走白名单 + 路径校验 + 审计日志（logs/org-changes.jsonl），仅老板可写
# =============================================================================
# key → (相对 VAULT 的路径, 标题, 是否允许编辑)；登录凭据永不开放












































# ---------------- 员工（profile）通道设置：读 config.yaml + 应用通道 ----------------











KERNEL = None  # 延迟绑定，见下方 Kernel












# ---------------- 多用户内核注册表：data_dir → Kernel（惰性创建） ----------------
# 流水线关键员工：用户名册缺任何一个即整份回退默认编制（保证编排可跑）






# 存量数据一次性迁移到 owner 工作区（幂等），然后建 owner 内核 + 全局大模型注册表
USERS._migrate_owner_workspace()
KERNEL = _kernel_for(OWNER_DATA_DIR)
if DBX:
    _st = DBX.store()
    if _st:
        _st.llm_seed(LREG.LLM_DEFAULTS)   # 全局 llm_models 注册表（默认路径库），空表时初始化
# 共享热点消息流：系统定时采集（HN / GitHub / 抖音 / 快手），公用区域全用户可见
FEED_STOP = threading.Event()
try:
    import hotspot_feed
    hotspot_feed.start(interval_sec=1800, stop_event=FEED_STOP)
    print("[feed] 共享热点采集线程已启动（30 分钟一轮）", flush=True)
except Exception as e:
    print(f"[feed] 采集线程启动失败：{type(e).__name__}: {e}", flush=True)


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

    def _kernel(self):
        """当前会话用户工作区的内核（未登录/兜底 → owner 存量空间）。"""
        user = self._session_user() or {}
        return _kernel_for(str(user.get("dataDir") or OWNER_DATA_DIR))

    def _owner_tenant(self):
        """存量公司空间（老板/员工）才能动共享资源：组织配置、全局模型注册表等。"""
        user = self._session_user() or {}
        return user.get("tenant") == "owner"

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

    def _register(self, body):
        """注册：手机号 + 密码 + 简单校验码 → 独立工作区 → 自动登录。"""
        ip = self.client_address[0]
        now = time.time()
        with LOGIN_LOCK:
            fails, until = LOGIN_FAILS.get(f"reg:{ip}", [0, 0])
        if until > now:
            return self._json({"ok": False, "error": f"尝试过于频繁，请 {int(until - now)} 秒后再试"}, 429)

        phone = re.sub(r"\D", "", str(body.get("phone") or ""))
        password = str(body.get("password") or "")
        answer = str(body.get("captcha") or body.get("captchaAnswer") or "")
        token = str(body.get("captchaToken") or "")
        if not re.fullmatch(r"1[3-9]\d{9}", phone):
            return self._json({"ok": False, "error": "手机号格式不正确（11 位，1 开头）"}, 400)
        if len(password) < 6:
            return self._json({"ok": False, "error": "密码至少 6 位"}, 400)
        if not USERS._captcha_verify(token, answer):
            with LOGIN_LOCK:
                f2 = fails + 1
                LOGIN_FAILS[f"reg:{ip}"] = [f2, now + 300 if f2 >= 5 else 0]
            return self._json({"ok": False, "error": "校验码不正确或已过期，请刷新后重试"}, 400)

        if USERS._user_get(phone):
            return self._json({"ok": False, "error": "该手机号已注册，请直接登录"}, 409)
        if phone == OWNER_PHONE:
            return self._json({"ok": False, "error": "该手机号不可注册"}, 400)
        row = USERS._user_create(phone, password)
        if not row:
            return self._json({"ok": False, "error": "注册失败，请稍后重试"}, 500)
        with LOGIN_LOCK:
            LOGIN_FAILS.pop(f"reg:{ip}", None)
        # 注册即登录，直接进入自己的工作空间
        user = {"id": phone, "role": "老板", "avatar": "", "email": "", "name": phone,
                "phone": phone, "dataDir": row["data_dir"], "tenant": "self"}
        tok = secrets.token_urlsafe(32)
        with SESS_LOCK:
            SESSIONS[tok] = {"user": user, "exp": now + SESSION_TTL}
        return self._json({"ok": True, "user": user},
                          cookie=f"{SESSION_COOKIE}={tok}; Path=/; HttpOnly; SameSite=Lax; "
                                 f"Max-Age={SESSION_TTL}")

    def _login(self, body):
        ip = self.client_address[0]
        now = time.time()
        with LOGIN_LOCK:
            fails, until = LOGIN_FAILS.get(ip, [0, 0])
        if until > now:
            return self._json({"ok": False, "error": f"失败次数过多，请 {int(until - now)} 秒后再试"}, 429)

        users = USERS.load_credentials()
        if not users:
            print(f"[auth] 登录失败：凭据文件缺失或没有密码字段 {CRED_FILE}", flush=True)
            return self._json({"ok": False, "error": "服务端未配置登录账号（见 Obsidian 公司/登录凭据.md）"}, 503)

        account = str(body.get("account") or body.get("user") or "").strip()
        pwd = str(body.get("password") or "")
        # 1) 注册用户（users.db，pbkdf2）优先；2) Obsidian 凭据（存量员工）兜底
        reg = USERS._user_get(account) if re.fullmatch(r"1[3-9]\d{9}", account) else None
        if reg and reg.get("password_hash"):
            if not hmac.compare_digest(USERS._hash_password(pwd, reg["salt"]),
                                       reg["password_hash"]):
                with LOGIN_LOCK:
                    f2 = fails + 1
                    LOGIN_FAILS[ip] = [f2, now + 60 if f2 >= 5 else 0]
                return self._json({"ok": False, "error": "账号或密码不正确"}, 401)
            with LOGIN_LOCK:
                LOGIN_FAILS.pop(ip, None)
            user = {"id": reg["phone"], "role": "老板",
                    "name": (reg["name"] or reg["phone"]),
                    "email": reg["email"] or "",
                    "avatar": "/api/account/avatar" if reg["avatar"] else "",
                    "phone": reg["phone"], "dataDir": reg["data_dir"], "tenant": "self"}
            tok = secrets.token_urlsafe(32)
            with SESS_LOCK:
                SESSIONS[tok] = {"user": user, "exp": now + SESSION_TTL}
            print(f"[auth] 注册用户登录成功 {ip} → {reg['phone']}", flush=True)
            return self._json({"ok": True, "user": user},
                              cookie=f"{SESSION_COOKIE}={tok}; Path=/; HttpOnly; SameSite=Lax; "
                                     f"Max-Age={SESSION_TTL}")
        u = USERS.find_user(account)
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
                "role": role, "avatar": avatar, "name": u["user"],
                "phone": (u.get("phone") or "").strip(),
                "dataDir": OWNER_DATA_DIR, "tenant": "owner"}
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
                info = self._kernel().reload_config()
                ORG._org_audit(user, "config.reload", "公司/员工名册.md", str(info))
                return self._json({"ok": True, "applied": "immediate", **info})
            if path == "/api/org/policy":
                key = str(body.get("key") or "")
                if key not in ORG.POLICY_FILES:
                    return self._json({"ok": False, "error": "未知制度文件"}, 404)
                rel, title, editable = ORG.POLICY_FILES[key]
                if not editable:
                    return self._json({"ok": False, "error": f"{title} 为只读视图"}, 403)
                content = str(body.get("content") or "")
                if not content.strip():
                    return self._json({"ok": False, "error": "内容为空，拒绝写入"}, 400)
                ORG._org_write(os.path.join(VAULT, rel), content, expect_prefix=VAULT)
                ORG._org_audit(user, "policy.save", rel, f"{len(content)} chars")
                return self._json({"ok": True, "applied": "immediate", "key": key,
                                   "path": rel, "bytes": len(content.encode('utf-8'))})
            parts = [p for p in path[len("/api/org/agent/"):].split("/") if p]
            if not parts:
                return self._json({"ok": False, "error": "缺少员工 ID"}, 400)
            aid = parts[0]
            if len(parts) == 1:
                name, role, pid, tools, scope, project_label = ORG._org_update_roster(aid, body)
                tone = ORG._org_tone(pid)
                soul = ORG._org_sync_soul(aid, name, role, project_label, tone)
                ORG._org_sync_org_table(aid, name, role, project_label, tone, tools)
                info = self._kernel().reload_config()
                ORG._org_audit(user, "agent.update", aid,
                           f"{name}/{role}/{project_label}/{scope}/{','.join(tools)} soul={soul}")
                return self._json({"ok": True, "applied": "immediate", "soulSynced": soul,
                                   "agent": ORG._org_agent_detail(aid), "reload": info})
            action = parts[1]
            pdir = ORG._org_profile_dir(aid)
            if action == "soul":
                content = str(body.get("content") or "")
                if not content.strip():
                    return self._json({"ok": False, "error": "SOUL 内容为空"}, 400)
                ORG._org_write(os.path.join(pdir, "SOUL.md"), content, expect_prefix=pdir)
                ORG._org_audit(user, "soul.save", aid, f"{len(content)} chars")
                return self._json({"ok": True, "applied": "immediate", "path": "SOUL.md"})
            if action == "memory":
                mem_dir = os.path.join(pdir, "memories")
                changed = []
                for key, fname in (("memory", "MEMORY.md"), ("user", "USER.md")):
                    if key in body:
                        content = str(body.get(key) or "")
                        ORG._org_write(os.path.join(mem_dir, fname), content, expect_prefix=pdir)
                        changed.append(f"{fname}:{len(content)}")
                        ORG._org_audit(user, "memory.save", f"{aid}/{fname}", f"{len(content)} chars")
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
                for rel, _cat, nm in ORG._org_skill_paths(pdir):
                    if nm == name:
                        target = os.path.join(pdir, "skills", rel, "SKILL.md")
                        break
                ORG._org_write(target, content, expect_prefix=pdir)
                ORG._org_audit(user, "skill.save", f"{aid}/{name}", f"{len(content)} chars")
                return self._json({"ok": True, "applied": "immediate", "name": name})
            if action == "config":
                content = str(body.get("content") or "")
                ORG._org_validate_config(content)
                ORG._org_write(os.path.join(pdir, "config.yaml"), content, expect_prefix=pdir)
                ORG._org_audit(user, "config.save", f"{aid}/config.yaml", f"{len(content)} chars")
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
            users = USERS.load_credentials()
            return self._json({"ok": True, "ready": bool(users), "file": "公司/登录凭据.md"})
        if path == "/api/captcha":
            return self._json({"ok": True, **USERS._captcha_issue()})
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
            return self._json(self._kernel().meeting_payload())
        if path.startswith("/api/meeting/"):
            rec = self._kernel().meeting_detail(path.rsplit("/", 1)[-1])
            return self._json(rec) if rec else self._json({"error": "meeting not found"}, 404)
        if path == "/api/knowledge":
            return self._json(self._kernel().knowledge_page(urlparse(self.path).query))
        if path == "/api/knowledge/categories":
            return self._json(self._kernel().knowledge_categories())
        if path.startswith("/api/knowledge/"):
            rec = self._kernel().knowledge_detail(path.rsplit("/", 1)[-1])
            return self._json(rec) if rec else self._json({"error": "knowledge not found"}, 404)
        # ---------- 作品档案（交付物统一管理：名称 + 时间 + 处理流程） ----------
        if path == "/api/works":
            if WK is None:
                return self._json({"ok": False, "error": "作品档案模块未加载"}, 500)
            with self._kernel().lock:
                approvals = list(self._kernel().approvals)
            idx = self._kernel()._factory_works_index()
            factory, step_status = idx if idx else (None, None)
            return self._json(WK.page(parse_qs(urlparse(self.path).query), approvals,
                                      factory=factory, step_status=step_status))
        if path.startswith("/api/works/"):
            if WK is None:
                return self._json({"ok": False, "error": "作品档案模块未加载"}, 500)
            with self._kernel().lock:
                approvals = list(self._kernel().approvals)
            idx = self._kernel()._factory_works_index()
            factory, step_status = idx if idx else (None, None)
            rec = WK.detail(path[len("/api/works/"):], approvals,
                            factory=factory, step_status=step_status)
            return self._json(rec) if rec else self._json({"error": "work not found"}, 404)
        if path == "/api/hotspots":
            return self._json(self._kernel().hotspot_page(urlparse(self.path).query))
        if path.startswith("/api/hotspots/"):
            rec = self._kernel().hotspot_detail(path.rsplit("/", 1)[-1])
            return self._json(rec) if rec else self._json({"error": "hotspot not found"}, 404)
        # ---------- 共享热点消息流（系统定时采集，公用区域，全用户可见） ----------
        if path == "/api/hotspot/feed":
            return self._json(self._hotspot_feed_view())
        if path == "/api/hotspot/categories":
            return self._json(self._hotspot_categories_view())
        if path == "/api/state":
            return self._json(self._kernel().snapshot())
        # ---------- CCO 创意灵感 / CTO 运维监控（只读聚合） ----------
        if path == "/api/inspiration":
            if insights is None:
                return self._json({"ok": False, "error": "灵感聚合模块未加载"}, 500)
            try:
                refresh = (parse_qs(urlparse(self.path).query).get("refresh") or [""])[0] == "1"
                return self._json(insights.build(self._kernel(), refresh=refresh,
                                                 vault=self._kernel().vault_dir))
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        if path == "/api/ops":
            if ops_monitor is None:
                return self._json({"ok": False, "error": "运维聚合模块未加载"}, 500)
            try:
                health = {"ok": True, "version": "3.0", "engine": MODE,
                          "hermes": HERMES.stats(), "time": iso_now()}
                return self._json(ops_monitor.build(self._kernel().snapshot(), health,
                                                    runs=self._kernel().pipeline_runs_view(),
                                                    llm=LREG._llm_view()))
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        if path == "/api/events":
            return self._sse()
        # ---------- 制片工厂（车间监控：工艺决策 + 工位过程） ----------
        if path == "/api/factory":
            return self._json(self._kernel().factory_view())
        # ---------- 用户中心 ----------
        if path == "/api/account":
            user = self._session_user() or {}
            data_dir = os.path.abspath(str(user.get("dataDir") or OWNER_DATA_DIR))
            created, k_agents = "", 0
            if user.get("tenant") == "self":
                row = USERS._user_get(str(user.get("phone") or ""))
                created = (row or {}).get("created_at") or ""
            k_agents = len(self._kernel().agents)
            assets, total, files = self._account_assets(data_dir)
            return self._json({
                "ok": True,
                "user": {"phone": user.get("phone") or "", "name": user.get("name") or "",
                         "email": user.get("email") or "",
                         "role": user.get("role") or "", "tenant": user.get("tenant") or "",
                         "createdAt": created},
                "stats": {"agents": k_agents, "assetFiles": files, "assetBytes": total},
                "assets": assets, "settings": self._account_settings(data_dir),
            })
        if path == "/api/account/export":
            try:
                return self._account_export()
            except Exception as e:
                return self._json({"ok": False, "error": f"导出失败：{type(e).__name__}: {e}"}, 500)
        if path == "/api/account/avatar":
            return self._account_avatar_get()
        if path.startswith("/api/factory/step/"):
            return self._json(self._factory_step(path[len("/api/factory/step/"):]))
        # OpenMontage Backlot 看板 JSON 代理（Backlot 无 CORS 头，浏览器不能直连；
        # 海报/媒体走绝对 URL 由 <img>/<video> 直接加载，不受 CORS 限制）
        # OpenMontage 媒体代理：外网（域名）访问 31.206 不可达，经 workbench 同源转发；
        # 内网访问时前端仍直连 Backlot（更快），代理仅供域名场景与兜底
        if path.startswith("/api/om/media/"):
            parts = path[len("/api/om/media/"):].split("/", 1)
            if len(parts) != 2 or not all(parts):
                return self._json({"ok": False, "error": "路径格式：/api/om/media/<project_id>/<file_path>"}, 400)
            target = f"http://127.0.0.1:4750/media/{quote(parts[0])}/{quote(parts[1], safe='/')}"
            return self._proxy_media(target)
        if path.startswith("/api/om/thumb/"):
            parts = path[len("/api/om/thumb/"):].strip("/").split("/", 1)
            if len(parts) != 2 or not all(parts):
                return self._json({"ok": False, "error": "路径格式：/api/om/thumb/<project_id>/<file_path>"}, 400)
            w = (parse_qs(urlparse(self.path).query).get("w") or ["320"])[0]
            return self._proxy_media(f"http://127.0.0.1:4750/thumb/{quote(parts[0])}"
                                     f"/{quote(parts[1], safe='/')}?w={quote(w)}")
        # ComfyUI 实时预览帧（阶段性产物）：ops_monitor 的 WS 监听线程缓存最新 JPEG
        if path.startswith("/api/comfy/preview/"):
            return self._comfy_preview(path[len("/api/comfy/preview/"):].strip("/"))
        # ComfyUI 产出物代理：21.157 在服务网段，浏览器不可直达，经 workbench 转发 /view
        if path.startswith("/api/comfy/view/"):
            port = path[len("/api/comfy/view/"):].strip("/")
            if port not in ("8188", "8189"):
                return self._json({"ok": False, "error": "非法节点端口"}, 400)
            qs = parse_qs(urlparse(self.path).query)
            fn = (qs.get("filename") or [""])[0]
            if not fn:
                return self._json({"ok": False, "error": "缺少 filename"}, 400)
            sub = quote((qs.get("subfolder") or [""])[0])
            ftype = quote((qs.get("type") or ["output"])[0])
            return self._proxy_media(
                f"http://192.168.21.157:{port}/view?filename={quote(fn)}&subfolder={sub}&type={ftype}")
        if path.startswith("/api/om/project/"):
            parts = path[len("/api/om/project/"):].strip("/").split("/")
            if len(parts) != 2 or parts[1] != "state":
                return self._json({"ok": False, "error": "路径格式：/api/om/project/<project_id>/state"}, 400)
            return self._proxy_json(f"http://127.0.0.1:4750/api/project/{quote(parts[0])}/state")
        if path.startswith("/api/trace/"):
            data = self._trace(path.rsplit("/", 1)[-1])
            if data is None:
                return self._json({"error": "trace not found"}, 404)
            return self._json(data)
        if path == "/api/reports":
            kind = (parse_qs(urlparse(self.path).query).get("kind") or [""])[0]
            return self._json({"reports": [r for r in self._kernel().reports if (not kind or r["kind"] == kind)]})
        # ---------- 统一消息（飞书 / APP / Web，只读） ----------
        if path.startswith("/api/chat/"):
            if not self._owner_only():
                return
            qs = parse_qs(urlparse(self.path).query)
            arg = lambda k, d="": (qs.get(k) or [d])[0]
            if not self._kernel().db:
                return self._json({"ok": False, "enabled": False, "error": "过程库未启用"}, 503)
            try:
                if path == "/api/chat/threads":
                    rows = self._kernel().db.chat_threads(arg("source"), _to_int(arg("limit", "50"), 50),
                                                  arg("before"))
                    return self._json({"ok": True, "enabled": True, "threads": rows})
                if path == "/api/chat/messages":
                    rows = self._kernel().db.chat_messages(
                        thread=arg("thread"), source=arg("source"), modality=arg("modality"),
                        direction=arg("direction"), q=arg("q"),
                        before_id=_to_int(arg("before", "0"), 0),
                        limit=_to_int(arg("limit", "50"), 50))
                    return self._json({"ok": True, "enabled": True, "messages": rows})
                if path == "/api/chat/stats":
                    return self._json({"ok": True, "enabled": True,
                                       "stats": self._kernel().db.chat_stats(_to_int(arg("days", "7"), 7))})
                if path == "/api/chat/export":
                    rows = self._kernel().db.chat_export(arg("start"), arg("end"),
                                                 _to_int(arg("limit", "10000"), 10000))
                    return self._json({"ok": True, "enabled": True, "count": len(rows), "messages": rows})
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._json({"ok": False, "error": "not found"}, 404)
        # ---------- 消息中心 · 聊天附件（仅老板） ----------
        if path.startswith("/chat-files/"):
            if not self._owner_only():
                return
            got = self._chat_file(path[len("/chat-files/"):])
            if not got:
                return self._json({"error": "not found"}, 404)
            mime, blob = got
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "private, max-age=60")
            self.end_headers()
            self.wfile.write(blob)
            return
        # ---------- 桌面精灵 · 出场播报（仅老板） ----------
        if path == "/api/secretary/brief":
            if not self._owner_only():
                return
            return self._json(self._secretary_brief())
        # ---------- 组织配置（制度 / 岗位 / 人员 / 性格 / 技能 / 记忆） ----------
        if path == "/api/org":
            try:
                llm = LREG._llm_view()
                return self._json({
                    "ok": True, "agents": ORG._org_agents(), "policies": ORG._org_policies(),
                    "profilesRoot": ORG_PROFILES,
                    "projects": [{"id": k, "name": v["name"]} for k, v in CC.PROJECTS.items()]
                               + [{"id": "all", "name": "全公司"}],
                    "llmChannels": [{"id": m["id"], "name": m["name"], "status": m["status"],
                                     "protocol": m["protocol"], "url": m["url"]}
                                    for m in llm.get("models") or []],
                    "llmProfiles": llm.get("profiles") or [],
                })
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        if path == "/api/org/policy":
            key = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
            if key not in ORG.POLICY_FILES:
                return self._json({"ok": False, "error": "未知制度文件"}, 404)
            rel, title, editable = ORG.POLICY_FILES[key]
            try:
                return self._json({"ok": True, "key": key, "title": title, "path": rel,
                                   "editable": editable,
                                   "content": ORG._org_read(os.path.join(VAULT, rel))})
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
                    return self._json({"ok": True, "agent": ORG._org_agent_detail(aid)})
                if len(parts) == 2 and parts[1] == "skill":
                    name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
                    return self._json({"ok": True, "skill": ORG._org_skill_detail(aid, name)})
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
        """首页「素材库」清单：读当前用户工作区的 materials.json，改文件下次刷新即生效。"""
        try:
            with open(self._kernel().materials_file, encoding="utf-8") as f:
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
        """交付物只读发布：/vault/... → 当前用户 vault/...，支持 Range（视频拖动进度条要用）。"""
        vault_root = self._kernel().vault_dir
        root = os.path.realpath(vault_root)
        full = os.path.realpath(os.path.join(vault_root, rel))
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

    def _comfy_preview(self, spec):
        """返回节点当前执行任务的最新预览帧（JPEG）；无帧时 204。"""
        port = spec.split("/", 1)[0].strip("/")
        body = b""
        if port in ("8188", "8189"):
            try:
                import ops_monitor as _OM
                st = _OM._COMFY_WS.get(port) or {}
                if st.get("preview") and st.get("curPid"):
                    body = st["preview"]
            except Exception:
                body = b""
        self.send_response(200 if body else 204)
        self.send_header("Content-Type", "image/jpeg" if body else "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _proxy_media(self, url):
        """只读媒体流代理：透传 Range（视频拖动播放）与长度/类型头，流式写回。"""
        headers = {}
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        try:
            req = _UrlRequest(url, headers=headers)
            with _urlopen(req, timeout=15) as resp:
                self.send_response(resp.status)
                for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    v = resp.headers.get(h)
                    if v:
                        self.send_header(h, v)
                self.send_header("Cache-Control", "private, max-age=600")
                self.end_headers()
                shutil.copyfileobj(resp, self.wfile, 64 * 1024)
        except Exception as e:
            try:
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 502)
            except Exception:
                pass

    def _proxy_json(self, url):
        """只读 JSON 代理（Backlot 看板等无 CORS 头的内网服务）；失败返回 502 结构。"""
        try:
            with _urlopen(url, timeout=4) as resp:
                return self._json(json.loads(resp.read(240000).decode("utf-8", "replace")))
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 502)

    # ---------- 用户中心：基本信息 / 参数设置 / 数据资产导出与导入 ----------

    _ASSET_LABELS = {
        "opc.db": ("过程数据库", "trace / 审批 / 成本 / 消息记录"),
        "materials.json": ("素材清单", "首页素材库索引"),
        "素材": ("素材文件", "视频 / 音频 / 图片等个人素材"),
        "vault": ("知识库与工作文档", "知识库 / 热点采集 / 交付物 / 会议纪要 / 邮件"),
        "workspace": ("运行时与设置", "运行态日志与用户中心参数"),
        "logs": ("日志", "运行日志"),
        "员工名册.md": ("AI 员工编制", "名册快照"),
    }

    def _sessions_touch(self, phone, **fields):
        """更新某手机号全部在线会话的用户信息（改名/邮箱/头像后立即生效）。"""
        with SESS_LOCK:
            for rec in SESSIONS.values():
                if rec["user"].get("phone") == phone:
                    rec["user"].update({k: v for k, v in fields.items() if v is not None})

    def _account_profile_save(self, body):
        """修改个人基本资料（姓名 / 邮箱）：仅注册用户（存量账号由公司凭据文件管理）。"""
        user = self._session_user() or {}
        if user.get("tenant") != "self":
            return self._json({"ok": False, "error":
                "存量账号的基本信息由 公司/登录凭据.md 管理，用户中心暂不开放修改"}, 403)
        phone = str(user.get("phone") or "")
        name = str(body.get("name") or "").strip()[:24]
        email = str(body.get("email") or "").strip()[:64]
        if not name:
            return self._json({"ok": False, "error": "姓名不能为空"}, 400)
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return self._json({"ok": False, "error": "邮箱格式不正确"}, 400)
        con = USERS._users_conn()
        try:
            with con:
                con.execute("UPDATE users SET name=?, email=? WHERE phone=?",
                            (name, email, phone))
        finally:
            con.close()
        self._sessions_touch(phone, name=name, email=email)
        ORG._org_audit(user, "account-profile", phone, f"name={name} email={email or '-'}")
        return self._json({"ok": True, "user": {**user, "name": name, "email": email}})

    _AVATAR_TYPES = {b"\x89PNG": ("png", "image/png"),
                     b"\xff\xd8\xff": ("jpg", "image/jpeg"),
                     b"RIFF": ("webp", "image/webp")}

    def _account_avatar_upload(self):
        """上传头像照片（png/jpg/webp，≤5MB）：存到个人工作区，回写注册表与会话。"""
        user = self._session_user() or {}
        if user.get("tenant") != "self":
            return self._json({"ok": False, "error": "存量账号暂不支持修改头像"}, 403)
        phone = str(user.get("phone") or "")
        raw = self._raw_body()
        if not raw:
            return self._json({"ok": False, "error": "请求体为空（应上传图片文件）"}, 400)
        if len(raw) > 5 * 1024 * 1024:
            return self._json({"ok": False, "error": "图片超过 5MB 上限"}, 413)
        ext = ctype = None
        for magic, (e, ct) in self._AVATAR_TYPES.items():
            if raw.startswith(magic):
                ext, ctype = e, ct
                break
        if not ext:
            return self._json({"ok": False, "error": "仅支持 png / jpg / webp 图片"}, 400)
        fname = f"avatar.{ext}"
        ws = os.path.join(data_dir := os.path.abspath(
            str(user.get("dataDir") or OWNER_DATA_DIR)), "workspace")
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, fname), "wb") as f:
            f.write(raw)
        # 清掉其他格式的旧头像，避免展示旧图
        for e, _ in self._AVATAR_TYPES.values():
            if e != ext:
                try:
                    os.remove(os.path.join(ws, f"avatar.{e}"))
                except OSError:
                    pass
        con = USERS._users_conn()
        try:
            with con:
                con.execute("UPDATE users SET avatar=? WHERE phone=?", (fname, phone))
        finally:
            con.close()
        self._sessions_touch(phone, avatar="/api/account/avatar")
        ORG._org_audit(user, "account-avatar", phone, fname)
        return self._json({"ok": True, "avatar": "/api/account/avatar"})

    def _account_avatar_get(self):
        """读取当前用户头像（无头像 404）。"""
        user = self._session_user() or {}
        if user.get("tenant") != "self":
            return self._json({"ok": False, "error": "not found"}, 404)
        data_dir = os.path.abspath(str(user.get("dataDir") or OWNER_DATA_DIR))
        ws = os.path.join(data_dir, "workspace")
        for e, ct in self._AVATAR_TYPES.values():
            p = os.path.join(ws, f"avatar.{e}")
            if os.path.exists(p):
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(os.path.getsize(p)))
                self.send_header("Cache-Control", "private, max-age=60")
                self.end_headers()
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 64 * 1024)
                return
        return self._json({"ok": False, "error": "not found"}, 404)

    # ---------- 共享热点消息流：定时采集（公用区域）+ 用户订阅 ----------
    _SUBSCRIBE_KEYS = {"hn", "github", "douyin", "kuaishou"}

    def _subscriptions(self, data_dir):
        try:
            with open(os.path.join(data_dir, "workspace", "subscriptions.json"),
                      encoding="utf-8") as f:
                subs = json.load(f)
            if isinstance(subs, list):
                return [s for s in subs if s in self._SUBSCRIBE_KEYS]
        except Exception:
            pass
        return []   # 未订阅 = 查看全部

    def _hotspot_feed_view(self):
        subs = self._subscriptions(os.path.abspath(
            str((self._session_user() or {}).get("dataDir") or OWNER_DATA_DIR)))
        view = hotspot_feed.feed_view(limit=120)
        mine = [s for s in view["sources"] if s["id"] in subs] if subs else []
        return {"ok": True, "sources": view["sources"], "items": view["items"],
                "subscribed": subs, "subscribedItems": [s["id"] for s in mine]}

    def _hotspot_categories_view(self):
        """热点分类表（订阅页直接展示的清单）：分类说明 + 各源的采集说明与运行状态。"""
        subs = self._subscriptions(os.path.abspath(
            str((self._session_user() or {}).get("dataDir") or OWNER_DATA_DIR)))
        view = hotspot_feed.categories_view()
        view["subscribed"] = subs
        return view

    # ---------- 消息中心 · 桌面精灵（小秘书对话：文字 / 语音 / 文件） ----------
    _CHAT_AUDIO_EXT = {"wav", "mp3", "ogg", "m4a", "amr", "webm", "aac", "flac"}
    _CHAT_FILE_MAX = 25 * 1024 * 1024
    # 小秘书可以指挥 Web 端跳转的页面白名单（整页跳转 window.location.assign）
    _CHAT_NAV = {"/", "/inspiration", "/ops", "/works", "/factory", "/hotspots",
                 "/knowledge", "/messages", "/org", "/account", "/account/assets",
                 "/account/settings", "/account/help", "/office", "/meeting"}
    _CHAT_FILES_RE = re.compile(
        r"[\w\u4e00-\u9fa5\-]+\.(wav|webm|mp3|m4a|ogg|flac|aac|png|jpg|jpeg|gif|pdf|txt|md|docx|xlsx|pptx|zip)", re.I)
    _CHAT_MIME = {"wav": "audio/wav", "webm": "audio/webm", "mp3": "audio/mpeg",
                  "m4a": "audio/mp4", "ogg": "audio/ogg", "flac": "audio/flac",
                  "aac": "audio/aac", "png": "image/png", "jpg": "image/jpeg",
                  "jpeg": "image/jpeg", "gif": "image/gif", "pdf": "application/pdf",
                  "txt": "text/plain; charset=utf-8", "md": "text/plain; charset=utf-8",
                  "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                  "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                  "zip": "application/zip"}

    def _chat_dir(self):
        d = os.path.join(OWNER_DATA_DIR, "workspace", "chat")
        os.makedirs(d, exist_ok=True)
        return d

    def _chat_put_inbound(self, db, *, modality, content, content_kind="text",
                          meta=None, asr=None):
        """老板从 Web 发来的一条消息 → web:secretary 线程；返回线程 ID。

        参考 APP 语音链路的处理：入库后小秘书立即给一条「收到」回执（随时回复能力），
        正式回复由后台任务完成后再异步到达。
        """
        tid = "web:secretary"
        now = iso_now()
        db.chat_upsert_thread(tid, "web", "Web · 小秘书",
                              [{"id": "boss", "name": "老板"},
                               {"id": "secretary", "name": "小秘书"}], now, now)
        db.chat_insert({"thread_id": tid, "source": "web", "modality": modality,
                        "direction": "inbound", "sender_id": "boss", "sender_name": "老板",
                        "content": str(content)[:4000], "content_kind": content_kind,
                        "origin_table": "chat_web", "origin_id": uid("cw"),
                        "asr": asr, "meta": meta, "created_at": now})
        # 即时回执
        if modality == "voice":
            ack = f"收到，老板。已听到「{str(content)[:24]}」，马上处理。"
        elif modality == "file":
            ack = f"收到，老板。《{(meta or {}).get('name', '文件')}》已收到，马上查看。"
        else:
            ack = "收到，老板。马上处理，稍后向你汇报。"
        k = self._kernel()
        db.chat_insert({"thread_id": tid, "source": "web", "modality": "text",
                        "direction": "outbound", "sender_id": "secretary",
                        "sender_name": k.agents.get("secretary", {}).get("name", "小秘书"),
                        "content": ack, "content_kind": "text",
                        "origin_table": "chat_web", "origin_id": uid("cw"),
                        "meta": {"ack": 1}, "created_at": iso_now()})
        k.emit("chat.message", "", {"threadId": tid, "source": "web",
                                    "direction": "outbound", "content": ack, "ack": 1})
        return tid

    def _chat_reply_async(self, k, tid, user_text, note=""):
        """后台调小秘书（hermes）回复；回复落库 + TTS 语音 + SSE 推送。"""
        chat_dir = self._chat_dir()
        secret_name = k.agents.get("secretary", {}).get("name", "小秘书")

        def work():
            reply, audio, err, nav = "", "", "", ""
            try:
                task = user_text if not note else f"{user_text}\n（附带：{note}）"
                data, meta = k.call_agent(
                    "secretary",
                    f"老板通过 Web 消息中心给你发来一条消息，请直接回应或执行：\n{task}",
                    context="这是老板的私人指令通道（Web 桌面精灵）。回复简短口语化、先给结论；"
                            "老板在 Web 端能看到你引用的资料链接。需要查资料/操作网页可用 browser_exec。"
                            "如果老板想看某个页面（报表/工厂/作品等），用 navigate 给出页面路径，"
                            "老板的浏览器会自动整页跳转过去展示。",
                    schema={"summary": "给老板的回复文本（≤200字，先结论）", "done": "是否已完成，布尔",
                            "detail": "可选：执行结果细节或资料出处",
                            "navigate": "可选：老板要看页面路径，仅限 / /inspiration /works /factory "
                                        "/hotspots /knowledge /ops /messages 之一"})
                if isinstance(data, dict):
                    reply = str(data.get("summary") or data.get("reply")
                                or data.get("detail") or "").strip()
                    nav_cand = str(data.get("navigate") or "").strip()
                    if nav_cand in self._CHAT_NAV and nav_cand != "/":
                        nav = nav_cand
                if not reply:
                    reply = "（我没有得出有效结论，请换种说法再试一次）"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                print(f"[chat] 小秘书处理失败：{err}", flush=True)
                reply = f"我这边处理出错了（{type(e).__name__}），请稍后再试或换个说法。"
            try:
                wav = voice_bridge.tts_synthesize(reply, os.path.join(
                    chat_dir, f"{datetime.now(TZ).strftime('%Y%m%d-%H%M%S')}-reply.wav"))
                if wav:
                    audio = f"/chat-files/{os.path.basename(wav)}"
            except Exception as e:
                print(f"[chat] TTS 失败：{type(e).__name__}: {e}", flush=True)
            try:
                now = iso_now()
                k.db.chat_insert({
                    "thread_id": tid, "source": "web", "modality": "voice" if audio else "text",
                    "direction": "outbound", "sender_id": "secretary", "sender_name": secret_name,
                    "content": reply, "content_kind": "text",
                    "origin_table": "chat_web", "origin_id": uid("cw"), "reply_to": None,
                    "meta": {"audio": audio, "error": err, "navigate": nav}, "created_at": now})
                k.emit("chat.message", "", {"threadId": tid, "source": "web",
                                            "direction": "outbound", "content": reply[:160],
                                            "audio": audio, "navigate": nav})
            except Exception as e:
                print(f"[chat] 回复落库失败：{type(e).__name__}: {e}", flush=True)

        threading.Thread(target=work, daemon=True).start()

    def _chat_file(self, name):
        """读取 workspace/chat 下老板自己的聊天附件（realpath 防目录穿越）。"""
        if not self._CHAT_FILES_RE.fullmatch(name):
            return None
        base = os.path.realpath(self._chat_dir())
        full = os.path.realpath(os.path.join(base, name))
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            return None
        ext = name.rsplit(".", 1)[-1].lower()
        mime = self._CHAT_MIME.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                return mime, f.read()
        except OSError:
            return None

    def _secretary_brief(self):
        """出场播报：小秘书登录问候 + 最新消息摘要（热点/留言/报告，逐项降级）。"""
        k = self._kernel()
        items = []
        try:
            feed = hotspot_feed.feed_view(limit=6)
            for it in (feed.get("items") or [])[:2]:
                title = str(it.get("title") or "").strip()
                if title:
                    items.append({"kind": "hotspot",
                                  "text": f"{it.get('sourceName') or it.get('source') or '热点'}：{title[:56]}"})
        except Exception:
            pass
        try:
            if k.db:
                rows = k.db.chat_messages(thread="web:secretary", limit=5)
                last = [r for r in rows if r.get("direction") == "outbound"]
                if last:
                    items.append({"kind": "chat",
                                  "text": f"我上次留言：{str(last[-1].get('content'))[:56]}"})
        except Exception:
            pass
        try:
            rep = next((r for r in reversed(k.reports or [])
                        if isinstance(r, dict) and r.get("title")), None)
            if rep:
                items.append({"kind": "report", "text": f"最新报告：{str(rep.get('title'))[:56]}"})
        except Exception:
            pass
        if not items:
            items = [{"kind": "idle", "text": "各系统运行正常，随时吩咐。"}]
        return {"ok": True, "greeting": "老板好，小秘书上线了，今日要点：", "items": items[:4]}

    def _chat_binary_post(self, path):
        """桌面精灵的二进制直传路由：/api/chat/voice（音频）、/api/chat/upload（文件）。"""
        if not self._owner_only():
            return
        k = self._kernel()
        if not k.db:
            return self._json({"ok": False, "enabled": False, "error": "过程库未启用"}, 503)
        try:
            blob = self._raw_body()
            if not blob:
                return self._json({"ok": False, "error": "请求体为空"}, 400)
            if len(blob) > self._CHAT_FILE_MAX:
                return self._json({"ok": False, "error": "超过 25MB 上限"}, 413)
            qs = parse_qs(urlparse(self.path).query)
            stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
            if path == "/api/chat/voice":
                ext = (qs.get("ext") or ["webm"])[0]
                if ext not in self._CHAT_AUDIO_EXT:
                    ext = "webm"
                fname = f"{stamp}-voice.{ext}"
                with open(os.path.join(self._chat_dir(), fname), "wb") as f:
                    f.write(blob)
                try:
                    transcript = voice_bridge.asr_transcribe(base64.b64encode(blob).decode())
                except Exception as e:
                    return self._json({"ok": False,
                                       "error": f"语音识别失败：{type(e).__name__}"}, 502)
                if not transcript.strip():
                    return self._json({"ok": False, "error": "没有识别到语音内容"}, 400)
                tid = self._chat_put_inbound(k.db, modality="voice", content=transcript,
                                             asr=1, meta={"audio": f"/chat-files/{fname}"})
                self._chat_reply_async(k, tid, transcript, note="这是一条语音消息的转写")
                return self._json({"ok": True, "threadId": tid, "transcript": transcript})
            # /api/chat/upload
            name = (qs.get("name") or ["附件"])[0]
            name = os.path.basename(name.replace("\\", "/"))[:80] or "附件"
            fname = f"{stamp}-{re.sub(r'[\\/:*?\"<>|]', '_', name)}"
            if not self._CHAT_FILES_RE.fullmatch(fname):
                fname = f"{stamp}-file.bin"
            with open(os.path.join(self._chat_dir(), fname), "wb") as f:
                f.write(blob)
            tid = self._chat_put_inbound(k.db, modality="file", content=f"发来文件：{name}",
                                         content_kind="file",
                                         meta={"file": f"/chat-files/{fname}", "name": name,
                                               "size": len(blob)})
            self._chat_reply_async(k, tid, f"老板发来了一个文件：{name}（{len(blob)} 字节）",
                                   note="文件已存到你的工作区 chat 目录，可先确认能否读取并给老板反馈")
            return self._json({"ok": True, "threadId": tid, "file": fname})
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _hotspot_subscribe(self, body):
        subs = body.get("sources")
        if not isinstance(subs, list):
            return self._json({"ok": False, "error": "sources 应为源 ID 数组"}, 400)
        subs = [s for s in subs if s in self._SUBSCRIBE_KEYS]
        data_dir = os.path.abspath(str((self._session_user() or {}).get("dataDir") or OWNER_DATA_DIR))
        os.makedirs(os.path.join(data_dir, "workspace"), exist_ok=True)
        with open(os.path.join(data_dir, "workspace", "subscriptions.json"),
                  "w", encoding="utf-8") as f:
            json.dump(subs, f, ensure_ascii=False, indent=2)
        user = self._session_user() or {}
        ORG._org_audit(user, "hotspot-subscribe", str(user.get("phone") or ""), ",".join(subs) or "全部")
        return self._json({"ok": True, "subscribed": subs})

    def _raw_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _send_file(self, path, filename):
        """附件下载：流式写回 + Content-Disposition（UTF-8 文件名）。"""
        size = os.path.getsize(path)
        quoted = quote(filename)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quoted}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 256 * 1024)

    def _account_assets(self, data_dir):
        """数据资产清单：按顶层条目聚合文件数与体积（全部打包进导出 zip）。"""
        items = []
        for name, (label, desc) in self._ASSET_LABELS.items():
            full = os.path.join(data_dir, name)
            if not os.path.exists(full):
                continue
            files, total = 0, 0
            if os.path.isdir(full):
                for dp, dn, fn in os.walk(full):
                    dn[:] = [d for d in dn if not d.startswith(".")]
                    files += len(fn)
                    for f in fn:
                        try:
                            total += os.path.getsize(os.path.join(dp, f))
                        except OSError:
                            pass
            else:
                files, total = 1, os.path.getsize(full)
            items.append({"key": name, "label": label, "desc": desc,
                          "files": files, "bytes": total, "included": True})
        return items, sum(i["bytes"] for i in items), sum(i["files"] for i in items)

    _SETTING_DEFAULTS = {"sound": True, "autoRefreshSec": 15, "signature": ""}

    def _account_settings(self, data_dir):
        path = os.path.join(data_dir, "workspace", "settings.json")
        try:
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
        except Exception:
            saved = {}
        return {**self._SETTING_DEFAULTS, **saved}

    def _account_settings_save(self, data_dir, patch):
        merged = self._account_settings(data_dir)
        for key in self._SETTING_DEFAULTS:
            if key in patch:
                merged[key] = patch[key]
        merged["autoRefreshSec"] = max(5, min(120, int(merged.get("autoRefreshSec") or 15)))
        merged["signature"] = str(merged.get("signature") or "")[:50]
        merged["sound"] = bool(merged.get("sound"))
        os.makedirs(os.path.join(data_dir, "workspace"), exist_ok=True)
        with open(os.path.join(data_dir, "workspace", "settings.json"),
                  "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        return merged

    def _account_export(self):
        """一键导出：过程库（sqlite backup 保证一致性）+ 素材 + vault + 名册 → zip 下载。"""
        user = self._session_user() or {}
        data_dir = os.path.abspath(str(user.get("dataDir") or OWNER_DATA_DIR))
        phone = str(user.get("phone") or user.get("name") or "user")
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="opc-export-", suffix=".zip")
        os.close(tmp_fd)
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                manifest = {"version": 1, "phone": phone, "exportedAt": iso_now(),
                            "dataDir": os.path.basename(data_dir), "items": []}
                for name in sorted(os.listdir(data_dir)):
                    full = os.path.join(data_dir, name)
                    if name == "opc.db":
                        # 过程库一致性快照（含 WAL 数据）
                        try:
                            import sqlite3
                            src = sqlite3.connect(full, timeout=5)
                            snap = tmp_path + ".db"
                            with src:
                                src.backup(snap)
                            src.close()
                            zf.write(snap, "opc.db")
                            os.remove(snap)
                        except Exception as e:
                            zf.write(full, "opc.db")
                            print(f"[export] {phone} 过程库快照失败（直拷）：{e}", flush=True)
                    elif name.endswith(("-wal", "-shm")):
                        continue
                    elif os.path.isdir(full):
                        for dp, dn, fn in os.walk(full):
                            dn[:] = [d for d in dn if not d.startswith(".")]
                            for f in fn:
                                p = os.path.join(dp, f)
                                zf.write(p, os.path.relpath(p, data_dir))
                    else:
                        zf.write(full, name)
                    manifest["items"].append(name)
                zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            ORG._org_audit(user, "account-export", phone, f"items={len(manifest['items'])}")
            fname = f"opc-{phone}-{iso_now()[:10]}.zip"
            return self._send_file(tmp_path, fname)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    _IMPORT_MAX = 512 * 1024 * 1024   # 上传 zip 上限 512MB

    def _account_import(self):
        """上传 zip → 校验 → 替换个人数据 → 重置内核（下次访问按新数据重建）。"""
        user = self._session_user() or {}
        data_dir = os.path.abspath(str(user.get("dataDir") or OWNER_DATA_DIR))
        raw = self._raw_body()
        if not raw:
            return self._json({"ok": False, "error": "请求体为空（应上传 zip 文件）"}, 400)
        if len(raw) > self._IMPORT_MAX:
            return self._json({"ok": False, "error": "压缩包超过 512MB 上限"}, 413)
        import zipfile as _zip
        try:
            zf = _zip.ZipFile(io.BytesIO(raw))
        except Exception as e:
            return self._json({"ok": False, "error": f"不是有效的 zip 文件：{type(e).__name__}"}, 400)
        names = zf.namelist()
        # zip-slip 防护：拒绝绝对路径 / 路径穿越 / 符号链接
        for n in names:
            if n.startswith("/") or ".." in n.replace("\\", "/").split("/") or ":" in n:
                return self._json({"ok": False, "error": f"压缩包含不安全路径：{n}"}, 400)
        if not ("manifest.json" in names or "opc.db" in names):
            return self._json({"ok": False, "error": "压缩包缺少 manifest.json / opc.db，"
                               "请使用本系统导出的资产包"}, 400)
        # 解压到暂存目录，校验通过后逐项替换
        staging = tempfile.mkdtemp(prefix="opc-import-")
        try:
            zf.extractall(staging)
            if "manifest.json" in names:
                try:
                    json.loads(open(os.path.join(staging, "manifest.json"),
                                    encoding="utf-8").read())
                except ValueError:
                    return self._json({"ok": False, "error": "manifest.json 损坏"}, 400)
            imported = []
            for name in sorted(set(names)):
                top = name.split("/")[0]
                if top in imported or top in ("manifest.json",) or top.endswith(("-wal", "-shm")):
                    continue
                src = os.path.join(staging, top)
                if not os.path.exists(src):
                    continue
                dst = os.path.join(data_dir, top)
                if os.path.isdir(src):
                    shutil.rmtree(dst, ignore_errors=True)
                elif top == "opc.db":
                    for suffix in ("-wal", "-shm"):
                        try:
                            os.remove(dst + suffix)
                        except OSError:
                            pass
                try:
                    shutil.move(src, dst)
                    imported.append(top)
                except OSError as e:
                    return self._json({"ok": False, "error": f"写入 {top} 失败：{e}"}, 500)
            _kernel_reset(data_dir)
            ORG._org_audit(user, "account-import", str(user.get("phone") or ""),
                       f"items={','.join(imported)}")
            print(f"[users] {user.get('phone')} 导入个人数据 {len(imported)} 项", flush=True)
            return self._json({"ok": True, "imported": imported,
                               "note": "个人数据已替换，工作台数据在下次刷新时按新数据重建"})
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _factory_step(self, spec):
        """车间工位详情：/api/factory/step/<trace_id>/<stage_id> → 工位记录 + 时间段内事件。"""
        parts = spec.split("/", 1)
        if len(parts) != 2:
            return {"ok": False, "error": "路径格式：/api/factory/step/<trace_id>/<stage_id>"}
        trace_id, stage_id = parts[0], parts[1]
        if not self._kernel().db:
            return {"ok": False, "error": "过程库未启用（OPC_DB=0）"}
        step = self._kernel().db.workshop_step(trace_id, stage_id)
        if not step:
            return {"ok": False, "error": "step not found"}
        started, finished = step.get("started_at") or "", step.get("finished_at") or ""
        # 工位边界各放宽 2 秒：缓冲写库/同一秒内的事件不至于被时间窗切掉
        lo = _iso_shift(started, -2) if started else ""
        hi = _iso_shift(finished, +2) if finished else iso_now()   # 进行中：取到当前时刻
        events = self._kernel().db.trace_events_between(trace_id, lo, hi)
        artifacts = []
        if WK is not None:
            try:
                idx = self._kernel()._factory_works_index()
                artifacts = WK.step_artifacts(trace_id, stage_id, (idx or (None,))[0])
            except Exception as e:
                print(f"[factory] 阶段成果扫描失败：{type(e).__name__}: {e}", flush=True)
        return {"ok": True, "step": _step_view(step),
                "events": events, "artifacts": artifacts,
                "traceId": trace_id, "stageId": stage_id}

    def _trace(self, trace):
        with self._kernel().lock:
            msgs = [m for m in self._kernel().messages if m.get("traceId") == trace]
            contents = [c for c in self._kernel().contents if c.get("traceId") == trace]
            meta = dict(self._kernel().traces.get(trace) or {})
            if trace not in self._kernel().traces and not msgs and not contents:
                if self._kernel().db:
                    return self._trace_from_db(trace)
                return None
            out = {"traceId": trace, "meta": meta,
                   "messages": msgs, "contents": contents,
                   "approvals": [a for a in self._kernel().approvals if a.get("traceId") == trace],
                   "reports": [r for r in self._kernel().reports if r.get("traceId") == trace],
                   "knowledge": [k for k in self._kernel().knowledge if k.get("traceId") == trace],
                   "messageCount": len(msgs)}
        # 内存里没有这条 trace 的元数据（重启后）或事件：从过程库补齐
        if self._kernel().db:
            db_meta, events = self._db_trace_parts(trace)
            if not out["meta"] and db_meta:
                out["meta"] = db_meta
            if events:
                out["events"] = events
                out["source"] = "db+mem" if msgs else "db"
        return out

    def _db_trace_parts(self, trace):
        row = self._kernel().db.trace_row(trace)
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
        return meta, self._kernel().db.trace_events(trace, 500)

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
        with self._kernel().lock:
            self._kernel().subscribers.append(q)

        def chunk(payload):
            raw = payload.encode()
            self.wfile.write(f"{len(raw):X}\r\n".encode() + raw + b"\r\n")
            self.wfile.flush()

        try:
            chunk(f"data: {json.dumps({'kind': 'state', 'traceId': '', 'at': iso_now(), 'data': self._kernel().snapshot()}, ensure_ascii=False)}\n\n")
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
            with self._kernel().lock:
                if q in self._kernel().subscribers:
                    self._kernel().subscribers.remove(q)
            self.wfile.write(b"0\r\n\r\n")

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._require_auth():
            return
        # 二进制直传路由（不走 JSON 解析，body 是原始音频/文件）
        if path in ("/api/chat/voice", "/api/chat/upload"):
            return self._chat_binary_post(path)
        body = self._body()
        if path == "/api/register":
            return self._register(body)
        # ---------- 用户中心：参数设置 / 数据导入 ----------
        if path == "/api/account/settings":
            user = self._session_user() or {}
            data_dir = os.path.abspath(str(user.get("dataDir") or OWNER_DATA_DIR))
            if not isinstance(body, dict):
                return self._json({"ok": False, "error": "参数应为对象"}, 400)
            merged = self._account_settings_save(data_dir, body)
            ORG._org_audit(user, "account-settings", str(user.get("phone") or ""),
                       json.dumps({k: merged[k] for k in merged}, ensure_ascii=False)[:160])
            return self._json({"ok": True, "settings": merged})
        if path == "/api/account/import":
            try:
                return self._account_import()
            except Exception as e:
                return self._json({"ok": False, "error": f"导入失败：{type(e).__name__}: {e}"}, 500)
        if path == "/api/account/profile":
            return self._account_profile_save(body)
        if path == "/api/account/avatar":
            return self._account_avatar_upload()
        if path == "/api/hotspot/subscribe":
            return self._hotspot_subscribe(body)
        # ---------- 消息中心 · 桌面精灵（小秘书对话） ----------
        if path in ("/api/chat/send", "/api/chat/voice", "/api/chat/upload"):
            if not self._owner_only():
                return
            if not self._kernel().db:
                return self._json({"ok": False, "enabled": False, "error": "过程库未启用"}, 503)
            try:
                if path == "/api/chat/send":
                    text = str(body.get("text") or "").strip()
                    if not text:
                        return self._json({"ok": False, "error": "消息内容不能为空"}, 400)
                    tid = self._chat_put_inbound(
                        self._kernel().db, modality="text", content=text)
                    self._chat_reply_async(self._kernel(), tid, text)
                    return self._json({"ok": True, "threadId": tid})
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
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
                return self._json({"ok": True, **self._kernel().patrol()})
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path == "/api/hotspots/inject":
            try:
                trace, hotspot = self._kernel().inject_hotspot(body.get("title"), body.get("tags"), body.get("score"))
            except BusyError as e:
                return self._json({"ok": False, "error": str(e), "busy": True,
                                   "traceId": e.traceId}, 409)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json({"ok": True, "traceId": trace, "hotspot": hotspot})
        if path == "/api/inspiration/produce":
            if user.get("role") != "老板":
                return self._json({"ok": False, "error": "forbidden",
                                   "reason": "仅老板可从创意灵感发起制作"}, 403)
            try:
                trace, hotspot = self._kernel().inject_idea(body.get("idea") or {},
                                                    body.get("requirements") or "")
            except BusyError as e:
                return self._json({"ok": False, "error": str(e), "busy": True,
                                   "traceId": e.traceId}, 409)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json({"ok": True, "traceId": trace, "hotspot": hotspot,
                               "note": "已进入 hermes 流水线（制作评估起跑），指令已同步 CCO"})
        if path == "/api/meetings":
            try:
                trace, hs = self._kernel().inject_hotspot(body.get("title"), body.get("tags"), body.get("score"))
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
            appr = self._kernel().decide_approval(path.rsplit("/", 1)[-1], decision, user.get("name", ""))
            if not appr:
                return self._json({"error": "approval not found"}, 404)
            return self._json({"ok": True, "approval": appr})
        if path == "/api/config/reload":
            if not self._owner_tenant():
                return self._json({"ok": False, "error": "forbidden",
                                   "reason": "热加载只对存量公司空间开放"}, 403)
            try:
                return self._json({"ok": True, **self._kernel().reload_config()})
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        # ---------- 大模型开关：CTO 运维控制（仅老板） ----------
        if path == "/api/ops/llm/profile":
            if user.get("role") != "老板" or not self._owner_tenant():
                return self._json({"ok": False, "error": "forbidden",
                                   "reason": "仅存量公司空间可修改员工模型通道（全局注册表）"}, 403)
            st = LREG._llm_store()
            if not st:
                return self._json({"ok": False, "error": "过程库未启用"}, 503)
            who = user.get("name") or user.get("user") or ""
            channel_id = str(body.get("channelId") or "")
            targets = []
            if body.get("all"):
                targets = [p["id"] for p in LREG._llm_view().get("profiles") or []]
            else:
                pid = str(body.get("profile") or "")
                if not pid or not os.path.isfile(os.path.join(ORG_PROFILES, pid, "config.yaml")):
                    return self._json({"ok": False, "error": f"未知 profile：{pid or '空'}"}, 400)
                targets = [pid]
            chan = None
            if channel_id:
                chan = next((r for r in st.llm_models() if r.get("id") == channel_id), None)
                if chan is None:
                    return self._json({"ok": False, "error": f"未知通道：{channel_id}"}, 400)
                if chan.get("status") != "enabled":
                    return self._json({"ok": False, "error": f"通道 {chan.get('name')} 未启用"}, 400)
            done, failed = [], []
            for pid in targets:
                if chan is None:
                    # 清空覆盖：恢复 profile 原生默认（云端主模型）
                    chan = next((r for r in st.llm_models() if r.get("id") == "hermes-default"), None)
                err = LREG._llm_profile_apply(pid, chan) if chan else "注册表无 hermes-default 通道"
                (failed if err else done).append(f"{pid}: {err}" if err else pid)
                ORG._org_audit(user, "llm-profile-channel", pid,
                           f"channel={channel_id or 'hermes-default'} {'OK' if not err else 'FAIL'}")
            if failed:
                return self._json({"ok": False, "error": "部分失败",
                                   "done": done, "failed": failed}, 500)
            return self._json({"ok": True, "applied": done,
                               "channel": channel_id or "hermes-default", "by": who,
                               "at": iso_now()})
        if path == "/api/ops/llm":
            if user.get("role") != "老板" or not self._owner_tenant():
                return self._json({"ok": False, "error": "forbidden",
                                   "reason": "仅存量公司空间可修改大模型开关与额度（全局注册表）"}, 403)
            st = LREG._llm_store()
            if not st:
                return self._json({"ok": False, "error": "过程库未启用，无法修改大模型配置"}, 503)
            mid = str(body.get("id") or "")
            action = str(body.get("action") or "set_status")
            who = user.get("name") or user.get("user") or ""
            if action == "set_status":
                status = str(body.get("status") or "")
                if status not in ("enabled", "disabled"):
                    return self._json({"ok": False, "error": "invalid status",
                                       "expected": "enabled|disabled"}, 400)
                if not st.llm_set_status(mid, status, who):
                    return self._json({"ok": False, "error": "模型不存在或更新失败"}, 404)
            elif action == "update":
                fields = {k: body[k] for k in ("name", "url", "protocol", "note", "totalTokens")
                          if k in body}
                if not st.llm_update(mid, fields, who):
                    return self._json({"ok": False, "error": "模型不存在或无可更新字段"}, 404)
            else:
                return self._json({"ok": False, "error": "invalid action",
                                   "expected": "set_status|update"}, 400)
            return self._json({"ok": True, "action": action, "llm": LREG._llm_view(),
                               "by": who, "at": iso_now()})
        # ---------- 模型管理（老板）：增删改 / 启停 / 设默认 / 时段 / url / key / 上下文 ----------
        if path == "/api/ops/llm-manage":
            if user.get("role") != "老板" or not self._owner_tenant():
                return self._json({"ok": False, "error": "forbidden",
                                   "reason": "仅存量公司空间的老板可管理大模型"}, 403)
            st = LREG._llm_store()
            if not st:
                return self._json({"ok": False, "error": "过程库未启用"}, 503)
            action = str(body.get("action") or "list")
            who = user.get("name") or user.get("user") or ""
            view = lambda: LREG._llm_manage_view()
            if action == "list":
                return self._json({"ok": True, **view()})
            model = body.get("model") if isinstance(body.get("model"), dict) else {}
            mid = str(body.get("id") or model.get("id") or "").strip()
            if action == "create":
                ok, err = st.llm_add(model, who)
                if not ok:
                    return self._json({"ok": False, "error": err}, 400)
                LREG._llm_alias_sync(model)
                ORG._org_audit(user, "llm-model-create", mid, "新增大模型通道")
                return self._json({"ok": True, **view(), "by": who, "at": iso_now()})
            if action == "update":
                if not mid:
                    return self._json({"ok": False, "error": "缺少通道 id"}, 400)
                before = next((r for r in st.llm_models() if r.get("id") == mid), None)
                if before is None:
                    return self._json({"ok": False, "error": "通道不存在"}, 404)
                fields = {k: model[k] for k in (
                    "name", "url", "protocol", "alias", "provider", "apiKey", "note",
                    "totalTokens", "priority", "contextLength", "maxOutputTokens")
                    if k in model}
                status = str(model.get("status") or "")
                if status not in ("", "enabled", "disabled", "exhausted"):
                    return self._json({"ok": False, "error": "invalid status"}, 400)
                if fields:
                    if not st.llm_update(mid, fields, who):
                        return self._json({"ok": False, "error": "更新失败"}, 400)
                if status in ("enabled", "disabled"):
                    st.llm_set_status(mid, status, who)
                if model.get("isDefault"):
                    st.llm_set_default(mid)
                after = next((r for r in st.llm_models() if r.get("id") == mid), None) or {}
                old_alias = str(before.get("alias") or "")
                new_alias = str(after.get("alias") or "")
                if old_alias and old_alias != new_alias:
                    LREG._llm_alias_sync(after, remove=True, old_alias=old_alias)
                if new_alias:
                    LREG._llm_alias_sync({**after, "apiKey": after.get("api_key") or "",
                                         "contextLength": after.get("context_length") or 0,
                                         "maxOutputTokens": after.get("max_output_tokens") or 0})
                ORG._org_audit(user, "llm-model-update", mid, "修改大模型参数")
                return self._json({"ok": True, **view(), "by": who, "at": iso_now()})
            if action == "delete":
                if not mid:
                    return self._json({"ok": False, "error": "缺少通道 id"}, 400)
                row = next((r for r in st.llm_models() if r.get("id") == mid), None)
                ok, err = st.llm_delete(mid)
                if not ok:
                    return self._json({"ok": False, "error": err}, 400)
                if row:
                    LREG._llm_alias_sync(row, remove=True)
                ORG._org_audit(user, "llm-model-delete", mid, "删除大模型通道")
                return self._json({"ok": True, **view(), "by": who, "at": iso_now()})
            if action == "set_status":
                status = str(body.get("status") or "")
                if status not in ("enabled", "disabled"):
                    return self._json({"ok": False, "error": "invalid status"}, 400)
                if not st.llm_set_status(mid, status, who):
                    return self._json({"ok": False, "error": "通道不存在"}, 404)
                return self._json({"ok": True, **view(), "by": who, "at": iso_now()})
            if action == "set_default":
                ok, err = st.llm_set_default(mid)
                if not ok:
                    return self._json({"ok": False, "error": err}, 404)
                ORG._org_audit(user, "llm-model-default", mid, "设为全局默认模型")
                return self._json({"ok": True, **view(), "by": who, "at": iso_now()})
            if action in ("schedule_set", "schedule_delete", "schedule_toggle_all"):
                if action == "schedule_toggle_all":
                    enable = bool(body.get("enabled"))
                    for r in st.llm_schedule_list():
                        st.llm_schedule_set(r["id"], {"enabled": enable}, who)
                elif action == "schedule_set":
                    rid = str(body.get("ruleId") or "").strip()
                    fields = {k: body[k] for k in ("modelId", "startHour", "endHour",
                                                   "label", "enabled", "priority", "note")
                              if k in body}
                    existing = next((r for r in st.llm_schedule_list()
                                     if r["id"] == rid), None) if rid else None
                    smid = str(fields.get("modelId") or mid
                               or (existing or {}).get("modelId") or "")
                    if not smid or not any(r.get("id") == smid for r in st.llm_models()):
                        return self._json({"ok": False, "error": "时段规则指向的通道不存在"}, 400)
                    sh, eh = fields.get("startHour"), fields.get("endHour")
                    if sh is not None and not 0 <= _to_int(sh, -1) <= 23:
                        return self._json({"ok": False, "error": "起始小时需为 0–23"}, 400)
                    if eh is not None and not 0 <= _to_int(eh, -1) <= 23:
                        return self._json({"ok": False, "error": "结束小时需为 0–23"}, 400)
                    if not existing and (sh is None or eh is None):
                        return self._json({"ok": False, "error": "新增规则需提供起止小时"}, 400)
                    if not rid:
                        rid = f"{smid}-{_to_int(sh, 0)}-{_to_int(eh, 0)}"
                    fields["modelId"] = smid
                    ok, err = st.llm_schedule_set(rid, fields, who)
                    if not ok:
                        return self._json({"ok": False, "error": err}, 400)
                else:
                    rid = str(body.get("ruleId") or "").strip()
                    if not rid:
                        return self._json({"ok": False, "error": "缺少规则 id"}, 400)
                    if not st.llm_schedule_delete(rid):
                        return self._json({"ok": False, "error": "删除失败"}, 500)
                return self._json({"ok": True, **view(), "by": who, "at": iso_now()})
            return self._json({"ok": False, "error": "invalid action",
                               "expected": "list|create|update|delete|set_status|set_default|schedule_set|schedule_delete|schedule_toggle_all"}, 400)
        # ---------- 大模型时段调度（按小时自动切换默认通道，如夜间切 qwen3.8-flash 降成本） ----------
        if path == "/api/ops/llm-schedule":
            if user.get("role") != "老板" or not self._owner_tenant():
                return self._json({"ok": False, "error": "forbidden",
                                   "reason": "仅存量公司空间可修改大模型时段调度"}, 403)
            st = LREG._llm_store()
            if not st:
                return self._json({"ok": False, "error": "过程库未启用"}, 503)
            action = str(body.get("action") or "list")
            who = user.get("name") or user.get("user") or ""
            if action == "list":
                return self._json({"ok": True, "schedule": st.llm_schedule_list()})
            elif action in ("set", "upsert"):
                rule_id = str(body.get("id") or "").strip()
                if not rule_id:
                    return self._json({"ok": False, "error": "缺少规则 id"}, 400)
                fields = {k: body[k] for k in ("modelId", "startHour", "endHour",
                                               "label", "enabled", "priority", "note")
                          if k in body}
                ok, err = st.llm_schedule_set(rule_id, fields, who)
                if not ok:
                    return self._json({"ok": False, "error": err or "更新失败"}, 500)
                return self._json({"ok": True, "schedule": st.llm_schedule_list(),
                                   "by": who, "at": iso_now()})
            elif action == "delete":
                rule_id = str(body.get("id") or "").strip()
                if not rule_id:
                    return self._json({"ok": False, "error": "缺少规则 id"}, 400)
                if not st.llm_schedule_delete(rule_id):
                    return self._json({"ok": False, "error": "删除失败"}, 500)
                return self._json({"ok": True, "schedule": st.llm_schedule_list(),
                                   "by": who, "at": iso_now()})
            else:
                return self._json({"ok": False, "error": "invalid action",
                                   "expected": "list|set|delete"}, 400)
        # ---------- CTO 运维处置：把巡检派单 / 热加载 / 审批收敛到一处（仅老板） ----------
        if path == "/api/ops/recover":
            if user.get("role") != "老板":
                return self._json({"ok": False, "error": "forbidden",
                                   "reason": "仅老板可执行运维恢复动作"}, 403)
            action = str(body.get("action") or "patrol")
            who = user.get("name") or user.get("user") or ""
            try:
                if action == "patrol":
                    result = self._kernel().patrol()
                elif action == "reload":
                    result = self._kernel().reload_config()
                elif action in ("approve", "reject"):
                    aid = str(body.get("id") or "")
                    if not aid:
                        return self._json({"ok": False, "error": "缺少审批 id"}, 400)
                    appr = self._kernel().decide_approval(aid, action, who)
                    if not appr:
                        return self._json({"ok": False, "error": "approval not found"}, 404)
                    result = {"approval": appr}
                elif action == "resume":
                    tid = str(body.get("traceId") or body.get("id") or "")
                    if not tid:
                        return self._json({"ok": False, "error": "缺少续跑的 traceId"}, 400)
                    result = self._kernel().resume_pipeline(tid)
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
                return self._json({"ok": True, **self._kernel().mgmt_delegate(
                    body.get("sender", ""), body.get("recipient", ""), body.get("text", ""))})
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path == "/api/commands":
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"ok": False, "error": "空指令"}, 400)
            self._kernel()._web_chat("inbound", text, "command", "owner", "老板", uid("web"))
            m = re.match(r"@(\S+)\s*([\s\S]*)", text)
            if m:
                key, rest = m.group(1), m.group(2).strip()
                cand = [a for a in self._kernel().agents.values() if a["id"] == key or a["name"] == key]
                if not cand:
                    cand = [a for a in self._kernel().agents.values() if key in a["name"] or key in a["id"]]
                if not cand:
                    return self._json({"ok": False, "error": f"没有找到员工 {key}"}, 404)
                try:
                    return self._json({"ok": True, **self._kernel().ask_agent(cand[0]["id"],
                                       rest or "请汇报当前进展与卡点。")})
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
            if "串味" in text or "persona" in text.lower():
                return self._json({"ok": True, "personaTest": self._kernel().persona_injection()})
            if "注入" in text or "热点" in text:
                try:
                    trace, hs = self._kernel().inject_hotspot()
                except BusyError as e:
                    return self._json({"ok": False, "error": str(e), "busy": True,
                                       "traceId": e.traceId}, 409)
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                return self._json({"ok": True, "traceId": trace, "hotspot": hs})
            with self._kernel().lock:
                blockers = [c for c in self._kernel().contents if c["stage"] in ("review", "approved")]
                pending = [a for a in self._kernel().approvals if a["status"] == "pending"]
            data, meta = self._kernel().call_agent("ceo", "回答老板：今天哪个项目卡住了，卡在哪。",
                                          f"待审/待发布 {len(blockers)} 条；待审批 {len(pending)} 个",
                                          "", "summary, evidence, confidence, tools")
            answer = data or {"summary": f"当前 {len(blockers)} 条待审、{len(pending)} 个审批待拍板",
                              "evidence": [], "confidence": 0.4, "tools": []}
            self._kernel().send("result", "ceo", "owner", "", {"result": answer, "engine": meta["engine"]}, priority=1)
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
        if self._kernel().db:
            self._kernel().db.run_end(self._kernel()._run_id)
            self._kernel().db.close()


if __name__ == "__main__":
    main()
