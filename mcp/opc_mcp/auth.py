"""初始化与鉴权：手机号只是身份标识，凭据是管理员发放的个人 API key（或旧密码）。

外部部署默认禁止「仅凭手机号发会话」；个人 key 存哈希，手机号决定数据隔离的工作区。
"""

import hashlib
import hmac
import re
import secrets
import time

from . import config


class AuthError(RuntimeError):
    pass


def normalize_phone(raw):
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits


def hash_key(key):
    """个人 API key 只存哈希；与全局 OPC_MCP_API_KEYS 用不同前缀隔离。"""
    return hashlib.sha256(("opc-personal:" + str(key or "").strip()).encode()).hexdigest()


def _cred_rows():
    """解析 `公司/登录凭据.md` 的 Markdown 表格（表头：用户名/手机号/邮箱/角色/头像/密码）。"""
    path = config.CRED_TABLE
    if not path.exists():
        return []
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
             if ln.strip().startswith("|")]
    if len(lines) < 2:
        return []
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def lookup(phone):
    phone = normalize_phone(phone)
    for row in _cred_rows():
        if normalize_phone(row.get("手机号")) == phone and phone:
            return row
    return None


def _upsert_user(store, phone, name, role, email, auth_kind):
    now = time.time()
    store.exec("INSERT INTO users(phone, name, role, email, auth_kind, created_at, last_login)"
               " VALUES(?,?,?,?,?,?,?)"
               " ON CONFLICT(phone) DO UPDATE SET name=excluded.name, role=excluded.role,"
               " email=excluded.email, auth_kind=excluded.auth_kind, last_login=excluded.last_login",
               (phone, name, role, email, auth_kind, now, now))
    return store.one("SELECT * FROM users WHERE phone=?", (phone,))


def _issue(store, phone):
    token = secrets.token_urlsafe(24)
    store.create_session(token, phone, config.SESSION_TTL_SEC)
    user = store.one("SELECT * FROM users WHERE phone=?", (phone,))
    return {"status": "ready", "session_token": token,
            "expires_in": config.SESSION_TTL_SEC,
            "user": {"phone": phone, "name": user["name"], "role": user["role"],
                     "email": user["email"], "auth_kind": user["auth_kind"]},
            "workspace": str(config.DATA_DIR / "users" / phone)}


def _key_ok(key):
    key = (key or "").strip()
    if not key or not config.API_KEYS:
        return False
    return any(hmac.compare_digest(key, k) for k in config.API_KEYS)


def init(store, phone=None, password=None, key=None):
    """手机号只是身份；外部部署必须带个人 key（或密码）才发会话。"""
    phone = normalize_phone(phone)
    if not phone:
        return {
            "status": "need_input",
            "required": {"phone": "11 位手机号"},
            "message": "请提供手机号以初始化 OPC 会话。",
            "next": "带 phone 和 key 重新调用 opc_init(phone, key=...)；key 由管理员按手机号发放。",
        }
    if len(phone) != 11:
        return {"status": "need_input", "required": {"phone": "11 位手机号"},
                "message": "手机号格式不对：%s" % phone,
                "next": "请提供 11 位中国大陆手机号后重调 opc_init(phone, key=...)。"}

    if key or password:
        return login(store, phone, password=password, key=key)

    row = lookup(phone)
    known = store.one("SELECT * FROM users WHERE phone=?", (phone,))
    if not row and not known:
        return {
            "status": "need_registration",
            "required": {"phone": phone, "key": "管理员发放的个人 API key"},
            "message": "手机号 %s 未登记，请管理员先执行 opc_mcp.admin add 发放个人 key。" % phone,
            "next": "拿到 key 后调用 opc_init(phone, key=...)，或由网关在请求头中自动注入。",
        }

    if config.PHONE_ONLY:
        if row:
            _upsert_user(store, phone, row.get("用户名") or "", row.get("角色") or "",
                         row.get("邮箱") or "", "phone")
        return _issue(store, phone)

    return {
        "status": "need_secret",
        "required": {"key": "个人 API key（推荐）", "password": "或不填 key 改用密码"},
        "message": "手机号 %s 已登记，但仅凭手机号不能登录；请提供个人 API key。" % phone,
        "next": "调用 opc_init(phone, key=...)；外部客户端把 key 配在 MCP 请求头即可，无需在对话里出现。",
    }


def login(store, phone, password=None, key=None):
    phone = normalize_phone(phone)
    if len(phone) != 11:
        raise AuthError("手机号需为 11 位：%s" % phone)

    row = lookup(phone)
    known = store.one("SELECT * FROM users WHERE phone=?", (phone,))
    if key:
        user = store.api_key_user(hash_key(key))
        if user and user["phone"] == phone:
            return _issue(store, phone)
        if user and user["phone"] != phone:
            raise AuthError("该 API key 属于其他手机号，不能用于当前手机号")
        if _key_ok(key):  # 兼容旧版全局 OPC_MCP_API_KEYS；新部署应删掉该环境变量
            _upsert_user(store, phone, (row or {}).get("用户名") or "key-user",
                         (row or {}).get("角色") or "key", (row or {}).get("邮箱") or "", "legacy-key")
            return _issue(store, phone)
        raise AuthError("key 无效、已撤销或不属于该手机号")

    if not row:
        raise AuthError("手机号未登记，且未提供个人 API key")
    if not password:
        raise AuthError("需要个人 API key 或密码")
    expect = row.get("密码") or ""
    if not expect or not hmac.compare_digest(str(password), expect):
        raise AuthError("密码错误")
    _upsert_user(store, phone, row.get("用户名") or "", row.get("角色") or "",
                 row.get("邮箱") or "", "password")
    return _issue(store, phone)


def authenticate(store, token=None, api_key=None):
    """个人 key 优先：网关注入的 key 直接映射到手机号，不产生会话 token。"""
    if api_key:
        user = store.api_key_user(hash_key(api_key))
        if not user:
            raise AuthError("个人 API key 无效或已撤销，请管理员重新发放")
        return user
    if token:
        user = store.session_user(token)
        if not user:
            raise AuthError("会话无效或已过期：重新调用 opc_init(phone, key=...)")
        return user
    raise AuthError("缺少身份凭据：外网客户端请配置个人 API key（请求头 X-Mail-Agent）")


def resolve(store, args):
    """工具入参解析：网关注入的 client_key 优先于模型自带的 token。"""
    api_key = args.get("client_key") or args.get("api_key") or config.CLIENT_KEY
    return authenticate(store, token=args.get("token"), api_key=api_key)


def fingerprint(phone):
    return hashlib.sha256(("opc:" + normalize_phone(phone)).encode()).hexdigest()[:12]
