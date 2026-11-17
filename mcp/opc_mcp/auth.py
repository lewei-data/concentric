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
    """外部接入只要 key；手机号只在网页申请 key 或密码登录时需要。"""
    if key:
        return login(store, phone, password=password, key=key)

    phone = normalize_phone(phone)
    if password and len(phone) != 11:
        return {"status": "need_input", "required": {"phone": "11 位手机号"},
                "message": "密码登录需要手机号；个人 key 登录不需要。",
                "next": "带 phone + password 调用 opc_init，或只用 key 调用 opc_init(key=...)。"}

    if not phone:
        return {
            "status": "need_secret",
            "required": {"key": "个人 API key"},
            "message": "外部接入只需个人 API key；手机号只在申请 key 时使用。",
            "next": "打开 %s 登录后生成 key，然后配置请求头 X-Mail-Agent=<个人 key>；"
                    % config.KEY_APPLY_URL
                    + "手写客户端也可调用 opc_init(key=...)，不需要 phone。",
        }
    if len(phone) != 11:
        return {"status": "need_input", "required": {"phone": "11 位手机号"},
                "message": "手机号格式不对：%s" % phone,
                "next": "请提供 11 位手机号，或直接只用 key 调用 opc_init(key=...)。"}

    row = lookup(phone)
    known = store.one("SELECT * FROM users WHERE phone=?", (phone,))
    if not row and not known:
        return {
            "status": "need_registration",
            "required": {"phone": phone, "key": "个人 API key"},
            "message": "手机号 %s 未登记/未领取 key，请登录工作台自助申请。" % phone,
            "next": "打开 %s 登录后按手机号生成 key；" % config.KEY_APPLY_URL
                    + "拿到后调用 opc_init(key=...) 即可，不必再传 phone。",
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
        "next": "未领取请登录 %s 自助生成；" % config.KEY_APPLY_URL
                + "已有 key 则调用 opc_init(key=...)，或由网关在请求头注入。",
    }


def login(store, phone=None, password=None, key=None):
    """个人 key 自包含手机号；只有密码登录/旧版全局 key 才需要 phone。"""
    if key:
        user = store.api_key_user(hash_key(key))
        if user:
            return _issue(store, user["phone"])

    phone = normalize_phone(phone)
    if key and _key_ok(key):  # 旧版全局 key 没有绑定手机号，仍需显式给 phone
        if len(phone) != 11:
            raise AuthError("旧版全局 key 需要同时提供 11 位手机号；个人 key 不需要")
        row = lookup(phone)
        _upsert_user(store, phone, (row or {}).get("用户名") or "key-user",
                     (row or {}).get("角色") or "key", (row or {}).get("邮箱") or "", "legacy-key")
        return _issue(store, phone)
    if key:
        raise AuthError("key 无效、已撤销；请到 %s 登录后重新生成" % config.KEY_APPLY_URL)

    if len(phone) != 11:
        raise AuthError("密码登录需要 11 位手机号；个人 key 登录不需要")
    row = lookup(phone)
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
            raise AuthError("个人 API key 无效或已撤销；请到 %s 登录后重新生成" % config.KEY_APPLY_URL)
        return user
    if token:
        user = store.session_user(token)
        if not user:
            raise AuthError("会话无效或已过期：重新调用 opc_init(key=...)，或直接使用请求头 X-Mail-Agent")
        return user
    raise AuthError("缺少身份凭据：请到 %s 登录后领取个人 API key，" % config.KEY_APPLY_URL
                    + "再配置到 MCP 请求头 X-Mail-Agent")


def resolve(store, args):
    """工具入参解析：网关注入的 client_key 优先于模型自带的 token。"""
    api_key = args.get("client_key") or args.get("api_key") or config.CLIENT_KEY
    return authenticate(store, token=args.get("token"), api_key=api_key)


def fingerprint(phone):
    return hashlib.sha256(("opc:" + normalize_phone(phone)).encode()).hexdigest()[:12]
