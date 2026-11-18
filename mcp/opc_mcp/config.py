"""运行配置：全部可用环境变量覆盖，默认值与产线现有组件对齐。"""

import os
import re
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT = PKG_DIR.parent                                   # /home/boray/opc-workbench/mcp
DATA_DIR = Path(os.environ.get("OPC_MCP_DATA_DIR") or (ROOT / "data"))
DB_PATH = DATA_DIR / "opc.db"

VAULT = Path(os.environ.get("OPC_VAULT") or "/mnt/sefs/conring/users/18911789286/obsidian")
# 用户根目录与工作台 dataDir 同源；MCP 产出的文件落在 <root>/<phone>/mcp/ 下。
USER_ROOT = Path(os.environ.get("OPC_USER_ROOT", "/mnt/sefs/conring/users"))


def phone_root(phone):
    """手机号 → 该用户 OPC 根目录（工作台 dataDir），只接受 11 位数字。"""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) > 11 and digits.startswith("86"):
        digits = digits[2:]
    safe = digits if len(digits) == 11 else "unknown"
    return USER_ROOT / safe


def user_dir(phone, sub=""):
    """手机号 → 该用户 OPC 根目录下的 MCP 工作区。"""
    path = phone_root(phone) / "mcp"
    if sub:
        path = path / str(sub).lstrip("/")
    return path


def user_vault(phone):
    return phone_root(phone) / "obsidian"


CRED_TABLE = Path(os.environ.get("OPC_CRED_TABLE") or (VAULT / "公司" / "登录凭据.md"))


def cred_table(phone=None):
    """每个手机号用自己的 OPC vault 里的凭据表；没有才回退全局默认。"""
    if phone:
        own = user_vault(phone) / "公司" / "登录凭据.md"
        if own.is_file():
            return own
    return CRED_TABLE

SESSION_TTL_SEC = int(os.environ.get("OPC_MCP_SESSION_TTL", 7 * 86400))
# 外部部署默认关掉「仅凭手机号发会话」；本机历史客户端可用 OPC_MCP_PHONE_ONLY=1 兼容
PHONE_ONLY = os.environ.get("OPC_MCP_PHONE_ONLY", "0") == "1"
API_KEYS = [k.strip() for k in (os.environ.get("OPC_MCP_API_KEYS") or "").split(",") if k.strip()]
# 本机 stdio 客户端可用环境变量绑定个人身份，等价于 HTTP 网关注入的 client_key
CLIENT_KEY = os.environ.get("OPC_MCP_CLIENT_KEY", "")
# 无 key 时回给外部 agent 的自助申请地址（登录后按手机号生成个人 key）
KEY_APPLY_URL = os.environ.get("OPC_MCP_KEY_APPLY_URL",
                               "https://ai.boraydata.com/workbench/mcp-key")

# LLM：默认走本机 LiteLLM 免费通道（与 prototype/llm_registry.py 的 auto-free 一致）
LLM_BASE_URL = os.environ.get("OPC_MCP_LLM_BASE_URL", "http://192.168.21.208:8000/v1")
LLM_MODEL = os.environ.get("OPC_MCP_LLM_MODEL", "auto-free")
LLM_API_KEY = os.environ.get("OPC_MCP_LLM_API_KEY") or os.environ.get("OPC_LLM_AUTOFREE_KEY") \
    or "sk-litellm-15bbcf2ea58c156130a00afd1b43ae4a"
LLM_TIMEOUT = float(os.environ.get("OPC_MCP_LLM_TIMEOUT", "120"))

# 兜底通道：主通道 429/5xx/网络错误时自动切到 OpenCode 通道。
# key 默认从模型管理页的 opencode-glm53 读取，避免在环境变量里再存一份明文。
LLM_FALLBACK_ENABLED = os.environ.get("OPC_MCP_LLM_FALLBACK", "1") != "0"
LLM_FALLBACK_URL = os.environ.get("OPC_MCP_LLM_FALLBACK_URL", "https://opencode.ai/zen/go/v1")
LLM_FALLBACK_MODEL = os.environ.get("OPC_MCP_LLM_FALLBACK_MODEL", "glm-5.3-flash")
LLM_FALLBACK_API_KEY = os.environ.get("OPC_MCP_LLM_FALLBACK_API_KEY", "")
LLM_FALLBACK_CHANNEL_ID = os.environ.get("OPC_MCP_LLM_FALLBACK_CHANNEL_ID", "opencode-glm53")
LLM_FALLBACK_DB = Path(os.environ.get("OPC_MCP_LLM_FALLBACK_DB")
                       or (USER_ROOT / "18911789286" / "opc.db"))
LLM_FALLBACK_TIMEOUT = float(os.environ.get("OPC_MCP_LLM_FALLBACK_TIMEOUT", "90"))

# 媒体能力：TTS 走 media-engine（本机 192.168.21.206:9200），图像走 media-gen（:9103）
# 注：9311/9312 只是 media-engine 背后的 qwen 引擎，不对 MCP 直接暴露
TTS_URL = os.environ.get("OPC_MCP_TTS_URL", "http://192.168.21.206:9200").rstrip("/")
TTS_TOKEN = os.environ.get("MEDIA_ENGINE_TOKEN", "")
IMAGE_URL = os.environ.get("OPC_MCP_IMAGE_URL", "http://192.168.21.206:9103").rstrip("/")
IMAGE_TOKEN = os.environ.get("MEDIA_GEN_TOKEN", "")

MAX_UPLOAD_MB = float(os.environ.get("OPC_MCP_MAX_UPLOAD_MB", "100"))
CHUNK_CHARS = int(os.environ.get("OPC_MCP_CHUNK_CHARS", "800"))
CONTEXT_BUDGET = int(os.environ.get("OPC_MCP_CONTEXT_BUDGET", "4000"))
JOB_WORKERS = max(1, int(os.environ.get("OPC_MCP_JOB_WORKERS", "2")))

# ---------------- ask(with_brief=true) 的知识图谱简报 ----------------
# 硬预算：无论库长到多大，简报正文都不超过这个字符数。
BRIEF_BUDGET = max(200, int(os.environ.get("OPC_MCP_BRIEF_BUDGET", "900")))
BRIEF_HARD_MAX = max(BRIEF_BUDGET, int(os.environ.get("OPC_MCP_BRIEF_HARD_MAX", "2000")))
# 层级：0=只给种子；1=种子+一跳邻居短标题；2=再加图扩展节点。
BRIEF_LEVEL = max(0, min(2, int(os.environ.get("OPC_MCP_BRIEF_LEVEL", "1"))))
# 每个才位最多占多少种子，防止单行/单一分类把预算吃光。
BRIEF_MAX_GROUP = max(1, int(os.environ.get("OPC_MCP_BRIEF_MAX_GROUP", "2")))

# ---------------- 大文件分片续传（HTTP 旁路，端口 9105） ----------------
UPLOAD_HOST = os.environ.get("OPC_MCP_UPLOAD_HOST", "0.0.0.0")
UPLOAD_PORT = int(os.environ.get("OPC_MCP_UPLOAD_PORT", "9105"))
UPLOAD_MAX_MB = float(os.environ.get("OPC_MCP_UPLOAD_MAX_MB", "2048"))
UPLOAD_DIR = Path(os.environ.get("OPC_MCP_UPLOAD_DIR") or (DATA_DIR / "uploads"))
# 分片大小建议值（客户端可任意，但服务端按 offset 校验）
UPLOAD_CHUNK_HINT = int(os.environ.get("OPC_MCP_UPLOAD_CHUNK_HINT", str(8 * 1024 * 1024)))
# 对外展示给外部 agent 的两个 base（前者公网 HTTPS，后者内网直连）
PUBLIC_UPLOAD_BASE = os.environ.get("OPC_MCP_PUBLIC_UPLOAD_BASE",
                                    "https://ai.boraydata.com/conring/upload").rstrip("/")
LOCAL_UPLOAD_BASE = os.environ.get("OPC_MCP_LOCAL_UPLOAD_BASE",
                                   "http://192.168.21.206:9105").rstrip("/")
# 未完成的上传单保留时长（秒），过期由 cleanup 清理
UPLOAD_TTL_SEC = int(os.environ.get("OPC_MCP_UPLOAD_TTL_SEC", str(7 * 86400)))
# 本进程角色，用于隔离两个进程共享 SQLite 的作业恢复
PROCESS_ROLE = os.environ.get("OPC_MCP_ROLE", "mcp")

# 文档抽取：LibreOffice 转 txt（pdf/doc/xls/ppt/pptx）
SOFFICE = os.environ.get("OPC_MCP_SOFFICE") or "soffice"
SOFFICE_TIMEOUT = float(os.environ.get("OPC_MCP_SOFFICE_TIMEOUT", "180"))
ZIP_MAX_ENTRIES = int(os.environ.get("OPC_MCP_ZIP_MAX_ENTRIES", "200"))
ZIP_MAX_UNPACK_MB = float(os.environ.get("OPC_MCP_ZIP_MAX_UNPACK_MB", "300"))
