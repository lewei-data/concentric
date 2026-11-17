"""初始化与鉴权：手机号 → 凭据表用户；手机号+密码 / key 完成首次绑定。"""

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


def init(store, phone=None):
    """按手机号初始化。已在名册 → 直接给会话；否则要求外部 agent 补手机号密码或 key。"""
    phone = normalize_phone(phone)
    if not phone:
        return {
            "status": "need_input",
            "required": {"phone": "11 位手机号"},
            "message": "请提供手机号以初始化 OPC 会话。",
            "next": "带 phone 重新调用 opc_init(phone)；若提示未登记，再调用 opc_login(phone, password|key)。",
        }
    if len(phone) != 11:
        return {"status": "need_input", "required": {"phone": "11 位手机号"},
                "message": "手机号格式不对：%s" % phone,
                "next": "请提供 11 位中国大陆手机号后重调 opc_init(phone)。"}

    row = lookup(phone)
    if row:
        user = _upsert_user(store, phone, row.get("用户名") or "", row.get("角色") or "",
                            row.get("邮箱") or "", "phone")
        return _issue(store, phone)

    known = store.one("SELECT * FROM users WHERE phone=?", (phone,))
    if known:
        return _issue(store, phone)

    return {
        "status": "need_registration",
        "required": {"phone": phone, "password_or_key": "登录密码 或 API key"},
        "message": "手机号 %s 不在公司登录名册，需补充密码或 key 完成初始化。" % phone,
        "next": "向用户索取密码或 key 后调用 opc_login(phone, password) 或 opc_login(phone, key=...)。",
    }


def login(store, phone, password=None, key=None):
    phone = normalize_phone(phone)
    if len(phone) != 11:
        raise AuthError("手机号需为 11 位：%s" % phone)

    row = lookup(phone)
    if key:
        if not _key_ok(key):
            raise AuthError("key 无效")
        name = (row or {}).get("用户名") or "key-user"
        role = (row or {}).get("角色") or "key"
        _upsert_user(store, phone, name, role, (row or {}).get("邮箱") or "", "key")
        return _issue(store, phone)

    if not row:
        raise AuthError("手机号未登记，且未提供 key")
    if not password:
        raise AuthError("需要密码或 key")
    expect = row.get("密码") or ""
    if not expect or not hmac.compare_digest(str(password), expect):
        raise AuthError("密码错误")
    _upsert_user(store, phone, row.get("用户名") or "", row.get("角色") or "",
                 row.get("邮箱") or "", "password")
    return _issue(store, phone)


def authenticate(store, token):
    if not token:
        raise AuthError("缺少 session_token：先调用 opc_init(phone) 完成初始化")
    user = store.session_user(token)
    if not user:
        raise AuthError("会话无效或已过期：重新调用 opc_init(phone)")
    return user


def fingerprint(phone):
    return hashlib.sha256(("opc:" + normalize_phone(phone)).encode()).hexdigest()[:12]
