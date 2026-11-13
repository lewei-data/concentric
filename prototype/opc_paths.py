"""路径与运行参数常量（全部模块共用；常量不可重绑，可安全 from-import）。"""
import os
import secrets
import threading
from datetime import timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIST = os.path.normpath(os.path.join(BASE_DIR, "..", "web", "dist"))
WEB_MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml",
            ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2", ".woff": "font/woff"}
TZ = timezone(timedelta(hours=8))
CLI = os.environ.get("OPC_HERMES_CLI", "/opt/sefs/hermes/run-hermes.sh")

HERMES_BIN = os.environ.get("OPC_HERMES_BIN", "/home/boray/.local/bin/hermes")
MODE = os.environ.get("OPC_HERMES_MODE", "hermes")
TIMEOUT = int(os.environ.get("OPC_HERMES_TIMEOUT", "600"))
WORKERS = int(os.environ.get("OPC_HERMES_WORKERS", "6"))
APPROVAL_TIMEOUT = int(os.environ.get("OPC_APPROVAL_TIMEOUT", "600"))
MODEL = os.environ.get("OPC_HERMES_MODEL", "deepseek-v4.1-flash")

TOKEN_LIMIT = int(os.environ.get("OPC_TOKEN_LIMIT", "20000000"))
CALL_TOKEN_LIMIT = int(os.environ.get("OPC_CALL_TOKEN_LIMIT", "2000000"))
COST_LIMIT_USD = float(os.environ.get("OPC_COST_LIMIT_USD", "0"))
AUTH_USER = os.environ.get("OPC_BASIC_USER", "")
AUTH_PASS = os.environ.get("OPC_BASIC_PASS", "")
LOOPBACK = ("127.0.0.1", "::1")

HOME_LEGACY = os.environ.get("OPC_HOME_LEGACY", "0") == "1"

VAULT = os.environ.get("OPC_VAULT", "/opt/sefs/obsidian")
PROJECT_FILE = os.path.join(VAULT, "公司", "项目配置.md")
ROSTER_FILE = os.path.join(VAULT, "公司", "员工名册.md")

CRED_FILE = os.path.join(VAULT, "公司", "登录凭据.md")

MATERIALS_FILE = os.environ.get("OPC_MATERIALS_FILE", os.path.join(BASE_DIR, "materials.json"))
MATERIALS_ROOT = os.environ.get("OPC_MATERIALS_ROOT", "/mnt/sefs/github-hotspots")

LEGACY_USERS_DIR = os.path.join(BASE_DIR, "users")   # 旧位置：启动时自动搬到 USERS_DIR
USERS_DIR = os.environ.get("OPC_USERS_DIR", "/mnt/sefs/conring/users")
USERS_DB = os.environ.get("OPC_USERS_DB", os.path.join(BASE_DIR, "users.db"))
OWNER_PHONE = os.environ.get("OPC_OWNER_PHONE", "18911789286")
OWNER_DATA_DIR = os.path.join(USERS_DIR, OWNER_PHONE)
CAPTCHA_TTL = 300                # 校验码有效期（秒）
CAPTCHA_SECRET = secrets.token_hex(32)   # 进程级密钥：重启后旧校验码失效
SESSION_COOKIE = "opc_sess"
SESSION_TTL = 7 * 24 * 3600          # 7 天免登录

MAIL_DOMAIN = os.environ.get("OPC_MAIL_DOMAIN", "mail.conring.cn")
MAIL_HOST = os.environ.get("OPC_MAIL_HOST", "127.0.0.1")
MAIL_SMTP_PORT = int(os.environ.get("OPC_MAIL_SMTP_PORT", "587"))
MAIL_CRED_FILE = os.environ.get("OPC_MAIL_CRED", os.path.join(VAULT, "公司", ".邮箱凭据.md"))
MAIL_AUDIT = os.environ.get("OPC_MAIL_AUDIT", "/opt/sefs/mcp-mail/logs/mail-audit.jsonl")
BOSS_MAIL = os.environ.get("OPC_BOSS_MAIL", f"boss@{MAIL_DOMAIN}")

OM_DEFS_DIR = os.environ.get("OPC_OM_DEFS", "/opt/sefs/OpenMontage/pipeline_defs")

ORG_PROFILES = os.environ.get("OPC_PROFILES", "/opt/sefs/hermes/profiles")
ORG_AUDIT = os.environ.get("OPC_ORG_AUDIT",
                           os.path.normpath(os.path.join(BASE_DIR, "..", "logs", "org-changes.jsonl")))
