"""路径与运行参数常量（全部模块共用；常量不可重绑，可安全 from-import）。"""
import os
import secrets
import threading
from datetime import timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIST = os.path.normpath(os.path.join(BASE_DIR, "..", "web", "dist"))
# 对外发布目录：downloads（公开）与 private_obsidian（私有，仅鉴权路由）软链在里面，
# 均由 server.py 运行时下发（构建不复制，否则会跟随软链把几十 G 数据复制进 dist）
WEB_PUBLIC = os.path.normpath(os.path.join(BASE_DIR, "..", "web", "public"))
WEB_MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml",
            ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2", ".woff": "font/woff"}
# 素材/成品下发用：在页面资源表基础上补音视频与文档
FILE_MIME = {**WEB_MIME,
             ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
             ".srt": "text/plain; charset=utf-8", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp", ".gif": "image/gif", ".pdf": "application/pdf",
             ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
             ".mp4": "video/mp4", ".webm": "video/webm", ".ts": "text/plain; charset=utf-8",
             ".ttf": "font/ttf", ".otf": "font/otf",
             # 补全常见图片/音视频：缺表会被当 application/octet-stream 下发，
             # 浏览器不内嵌显示（新窗口打开变下载或空白），素材页就会"看不到实际图片"
             ".png": "image/png", ".bmp": "image/bmp", ".ico": "image/x-icon",
             ".avif": "image/avif", ".heic": "image/heic", ".heif": "image/heif",
             ".tif": "image/tiff", ".tiff": "image/tiff", ".svg": "image/svg+xml",
             ".apng": "image/apng", ".jfif": "image/jpeg", ".pjpeg": "image/jpeg",
             ".mov": "video/quicktime", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
             ".avi": "video/x-msvideo", ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv",
             ".m3u8": "application/vnd.apple.mpegurl",
             ".flac": "audio/flac", ".aac": "audio/aac", ".ogg": "audio/ogg",
             ".opus": "audio/opus", ".wma": "audio/x-ms-wma", ".amr": "audio/amr",
             ".vtt": "text/vtt; charset=utf-8", ".ass": "text/plain; charset=utf-8",
             ".lrc": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8"}
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

VAULT = os.environ.get("OPC_VAULT", "/mnt/sefs/conring/users/18911789286/obsidian")
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
