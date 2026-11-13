"""用户体系：登录凭据（Obsidian）/ 注册表（users.db）/ 校验码 / 独立工作区工厂。"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import shutil
import time

from opc_paths import *  # noqa: F401,F403
from utils import iso_now, uid
import db as DBX
import agent_defaults as AD
import company_config as CC

SESSIONS = {}                        # token -> {"user": {...}, "exp": epoch}
SESS_LOCK = threading.Lock()
LOGIN_FAILS = {}                     # ip -> [连续失败次数, 解锁时间]
LOGIN_LOCK = threading.Lock()

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

def _users_conn():
    """用户注册表连接（users.db，WAL）；首次调用自动建表（含老库补列）。"""
    import sqlite3
    con = sqlite3.connect(USERS_DB, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS users (
        phone         TEXT PRIMARY KEY,
        name          TEXT,
        password_hash TEXT,
        salt          TEXT,
        role          TEXT NOT NULL DEFAULT '老板',
        source        TEXT NOT NULL DEFAULT 'register',
        data_dir      TEXT NOT NULL,
        created_at    TEXT,
        last_login    TEXT
    )""")
    # 老库补列：个人资料（邮箱 / 头像文件名）
    cols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
    for col in ("email", "avatar"):
        if col not in cols:
            con.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    con.commit()
    return con

def _hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt), 120_000).hex()

def _user_get(phone):
    try:
        con = _users_conn()
        try:
            row = con.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()
    except Exception as e:
        print(f"[users] 读取注册表失败：{type(e).__name__}: {e}", flush=True)
        return None

def _user_create(phone, password):
    """注册新用户：users.db 记录 + 独立工作区（过程库/素材/工作目录/默认编制）。"""
    import sqlite3
    salt = secrets.token_hex(16)
    row = {
        "phone": phone, "name": phone, "password_hash": _hash_password(password, salt),
        "salt": salt, "role": "老板", "source": "register",
        "data_dir": os.path.join(USERS_DIR, phone), "created_at": iso_now(),
    }
    data_dir = row["data_dir"]
    ws = os.path.join(data_dir, "workspace")
    os.makedirs(ws, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)
    # 过程库：完整 schema 的独立 opc.db（即使 OPC_DB 关闭也建库，注册即初始化）
    try:
        if DBX:
            DBX.Store(os.path.join(data_dir, "opc.db"))
    except Exception as e:
        print(f"[users] {phone} 过程库初始化失败：{type(e).__name__}: {e}", flush=True)
    # 素材清单（结构对齐全局 materials.json；root 指向用户素材目录）
    mat = os.path.join(data_dir, "materials.json")
    if not os.path.exists(mat):
        with open(mat, "w", encoding="utf-8") as f:
            json.dump({"updated": iso_now()[:10], "root": os.path.join(data_dir, "素材"),
                       "items": []}, f, ensure_ascii=False, indent=2)
    os.makedirs(os.path.join(data_dir, "素材"), exist_ok=True)
    # 独立 vault 目录树：知识库/热点采集/交付物/会议纪要/邮件 全部用户私有
    for sub in ("热点采集", "知识库", "交付物", "会议纪要", "邮件", "_归档"):
        os.makedirs(os.path.join(data_dir, "vault", "OPC", sub), exist_ok=True)
    # 默认编制名册（内置 19 人编制 + 短剧风格随机姓名）+ 运行时日志
    roster = os.path.join(data_dir, "员工名册.md")
    if not os.path.exists(roster):
        with open(roster, "w", encoding="utf-8") as f:
            f.write(_user_roster_md(randomize=True))
    journal = os.path.join(ws, "runtime.json")
    if not os.path.exists(journal):
        with open(journal, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False)
    con = _users_conn()
    try:
        with con:
            con.execute("""INSERT INTO users(phone,name,password_hash,salt,role,source,
                           data_dir,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (row["phone"], row["name"], row["password_hash"], row["salt"],
                         row["role"], row["source"], row["data_dir"], row["created_at"]))
    except sqlite3.IntegrityError:
        return None
    finally:
        con.close()
    print(f"[users] 新用户注册 {phone}，工作区 {data_dir}", flush=True)
    return row

def _migrate_owner_workspace():
    """存量数据一次性迁移：prototype/opc.db + materials.json → users/<OWNER_PHONE>/。

    幂等：目标 opc.db 已存在则跳过；materials.json 缺失才拷贝。
    另：旧位置的 users/<手机号>/ 目录整体搬到 USERS_DIR（默认位置变更时自动搬家）。
    """
    # 旧默认位置（prototype/users）→ 新位置（/mnt/sefs/conring/users）
    if os.path.isdir(LEGACY_USERS_DIR) and LEGACY_USERS_DIR != USERS_DIR:
        try:
            os.makedirs(USERS_DIR, exist_ok=True)
            for name in os.listdir(LEGACY_USERS_DIR):
                src = os.path.join(LEGACY_USERS_DIR, name)
                dst = os.path.join(USERS_DIR, name)
                if not os.path.isdir(src) or os.path.exists(dst):
                    continue
                shutil.move(src, dst)
                print(f"[migrate] 用户工作区已搬至新位置 → {dst}", flush=True)
        except OSError as e:
            print(f"[migrate] 用户工作区搬迁失败（不阻断启动）：{type(e).__name__}: {e}", flush=True)
    db_src = os.path.join(BASE_DIR, "opc.db")
    db_dst = os.path.join(OWNER_DATA_DIR, "opc.db")
    if os.path.exists(db_src) and not os.path.exists(db_dst):
        os.makedirs(OWNER_DATA_DIR, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            src = db_src + suffix
            if os.path.exists(src):
                shutil.copy2(src, db_dst + suffix)
        print(f"[migrate] 存量过程库已迁移 → {db_dst}", flush=True)
    mat_dst = os.path.join(OWNER_DATA_DIR, "materials.json")
    if os.path.exists(MATERIALS_FILE) and not os.path.exists(mat_dst):
        os.makedirs(OWNER_DATA_DIR, exist_ok=True)
        shutil.copy2(MATERIALS_FILE, mat_dst)
        print(f"[migrate] 素材清单已迁移 → {mat_dst}", flush=True)
    # 注册用户的默认名册升级：旧版本模板（缺管理层/配音或员工不全）刷新为最新完整编制；
    # 仅当名册仍是自动生成（标题含"注册时自动生成"）时才覆盖，用户手工编辑过的不动
    try:
        con = _users_conn()
        try:
            for row in con.execute("SELECT phone,data_dir FROM users WHERE source='register'"):
                roster = os.path.join(row["data_dir"], "员工名册.md")
                try:
                    with open(roster, encoding="utf-8") as f:
                        head = f.read(400)
                    spec_ids = {r[0] for r in CC._parse_roster(roster)[0]}
                except OSError:
                    continue
                if "注册时自动生成" not in head:
                    continue
                missing = {"cco", "secretary", "ceo", "cto"} - spec_ids
                if not missing and len(spec_ids) >= len(AD.DEFAULT_AGENT_SPEC):
                    continue
                with open(roster, "w", encoding="utf-8") as f:
                    f.write(_user_roster_md())
                print(f"[migrate] {row['phone']} 默认名册已升级 → {len(AD.DEFAULT_AGENT_SPEC)} 人",
                      flush=True)
        finally:
            con.close()
    except Exception as e:
        print(f"[migrate] 用户名册升级失败（不阻断启动）：{type(e).__name__}: {e}", flush=True)
    # 注册表登记存量用户（密码仍以 Obsidian 凭据为准，不落明文/哈希）；
    # 已登记的旧路径记录同步刷新到当前 OWNER_DATA_DIR
    try:
        con = _users_conn()
        try:
            con.execute("""INSERT INTO users(phone,name,password_hash,salt,role,source,
                           data_dir,created_at) VALUES(?,?,?,?,?,?,?,?)
                           ON CONFLICT(phone) DO UPDATE SET data_dir=excluded.data_dir""",
                        (OWNER_PHONE, "老板（存量）", "", "", "老板", "legacy",
                         OWNER_DATA_DIR, iso_now()))
            con.commit()
        finally:
            con.close()
    except Exception as e:
        print(f"[migrate] 注册表登记存量用户失败：{type(e).__name__}: {e}", flush=True)

def _captcha_sign(answer, exp):
    msg = f"{answer}|{exp}".encode()
    return hmac.new(CAPTCHA_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:24]

def _captcha_issue():
    a, b = secrets.randbelow(9) + 1, secrets.randbelow(9) + 1
    answer = a + b
    exp = int(time.time()) + CAPTCHA_TTL
    token = f"{exp}.{_captcha_sign(answer, exp)}"
    return {"id": uid("cap"), "question": f"{a} + {b} = ?", "token": token}

def _captcha_verify(token, answer):
    try:
        exp_str, sig = str(token).split(".", 1)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sig, _captcha_sign(str(answer).strip(), exp))

def _user_roster_md(randomize=False):
    """默认编制名册（从内置 19 人编制生成，项目列用项目名以便名册解析回读）。

    randomize=True（注册新用户）时姓名用短剧虚拟人物风格随机生成；
    False（存量名册升级）保持内置固定名，避免升级悄悄给老用户改名。
    """
    names = AD.random_agent_names() if randomize else [r[1] for r in AD.DEFAULT_AGENT_SPEC]
    lines = ["# 员工名册（默认编制 · 注册时自动生成）", "",
             "> 本工作空间的 AI 员工编制；可在此调整，保存后重启服务生效。",
             ""]
    lines.append("| ID | 姓名 | 岗位 | 项目 | 负责范围 | 工具白名单 |")
    lines.append("|----|------|------|------|----------|------------|")
    for (aid, _name, role, pid, tools, scope), name in zip(AD.DEFAULT_AGENT_SPEC, names):
        # 项目列固定用内置默认项目名（产品一组/二组/三组），不随全局 Obsidian 的 owner 配置漂移
        pname = (CC.DEFAULT_PROJECTS.get(pid) or {}).get("name") or (
            "全公司" if pid == "all" else pid)
        lines.append(f"| {aid} | {name} | {role} | {pname} | {scope} | {'、'.join(tools)} |")
    return "\n".join(lines) + "\n"
