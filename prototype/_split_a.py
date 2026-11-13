#!/usr/bin/env python3
"""server.py 拆分脚本 A：抽取路径常量/工具/默认编制/邮件/公司配置/用户/组织/LLM 注册表/HermesAdapter。

策略：
1. 用 AST 定位顶层节点（含紧邻前导注释），按名字搬移到新模块；
2. 散落常量按名字单独搬移；
3. 对所有新模块与 server.py 残余部分统一应用模块前缀替换（CC./USERS./ORG./LREG./MAILM.）；
4. 新模块自带上划线工具函数与常量的显式导入；
5. 全程不改动任何逻辑，只做移动与名字限定。
"""
import ast
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "server.py")
lines = open(SRC, encoding="utf-8").read().splitlines(keepends=True)
src = "".join(lines)
tree = ast.parse(src)

# ---------- 收集顶层节点行区间（含前导注释） ----------
nodes = {}
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        start = node.lineno
        # 前导注释（紧邻的上方连续注释行）
        while start - 2 >= 0 and lines[start - 2].lstrip().startswith("#"):
            start -= 1
        nodes[node.name] = (start, node.end_lineno)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                nodes.setdefault(t.id, (node.lineno, node.end_lineno))

def cut(names):
    """按名字搬移代码段（合并相邻行区间），返回 (code, 被移除的行号集合)。"""
    spans = sorted(nodes[n][:2] for n in names if n in nodes)
    missing = [n for n in names if n not in nodes]
    if missing:
        raise SystemExit(f"未找到符号: {missing}")
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    out, removed = [], set()
    for s, e in merged:
        out.append("".join(lines[s - 1:e]))
        removed.update(range(s, e + 1))
    return "\n".join(out), removed

def unindent_single(name, code):
    """把被搬走的名字从 server 行集合中移除（由 rewrite 主流程处理删除）。"""
    return code

# ---------- 名字前缀替换 ----------
def prefix(code, mapping):
    for name, mod in mapping.items():
        code = re.sub(rf"\b{re.escape(name)}\b", f"{mod}.{name}", code)
    return code

CC_NAMES = ["PROJECTS", "TONE", "PROJECT_BY_NAME", "AGENT_SPEC", "DELIVERY",
            "CRAFT_STEPS", "CRAFT_BY_ID", "OPENMONTAGE_CRAFTS", "DEFAULT_PROJECTS",
            "DEFAULT_TONE", "DEFAULT_HIGH_RISK", "DEFAULT_DELIVERY",
            "FACTORY_ENTRY_STAGE", "FACTORY_EXIT_STAGE",
            "_load_project_cfg", "_load_delivery_cfg", "_parse_project_cfg",
            "_parse_delivery_cfg", "_load_craft_steps", "_load_agent_spec",
            "_parse_roster", "_load_dl"]
USER_NAMES = ["load_credentials", "find_user", "_users_conn", "_hash_password",
              "_user_get", "_user_create", "_migrate_owner_workspace", "_user_roster_md",
              "_captcha_sign", "_captcha_issue", "_captcha_verify"]
ORG_NAMES = ["_org_agent_row", "_org_project_label", "_org_tone", "_org_profile_dir",
             "_org_read", "_org_write", "_org_audit", "_org_skill_paths", "_org_skills",
             "_org_toolsets", "_org_agents", "_org_policies", "_org_agent_detail",
             "_org_skill_detail", "_org_update_roster", "_org_sync_soul",
             "_org_sync_org_table", "_org_validate_config"]
LREG_NAMES = ["LLM_DEFAULTS", "_llm_store", "_llm_consume", "_llm_view",
              "_parse_profile_model", "_llm_profiles_view", "_hermes_cfg_set",
              "_llm_profile_apply"]
MAIL_NAMES = ["load_mail_accounts", "send_internal_mail"]

IMPORTS_COMMON = (
    "import json\nimport os\nimport re\nimport shutil\nimport subprocess\n"
    "import threading\nimport time\nimport zipfile\nimport io\nimport secrets\n"
    "import hashlib\nimport hmac\nimport sqlite3\n"
    "from datetime import datetime, timedelta, timezone\n\n"
    "from opc_paths import *  # noqa: F401,F403 —— 路径与运行常量\n"
    "from utils import iso_now, uid, _to_int, _hotkey, _step_view, _iso_shift, _num\n"
    "import company_config as CC\n"
)

# ---------- 1. opc_paths.py（常量） ----------
CONST_PATH = ["BASE_DIR", "WEB_DIST", "WEB_MIME", "TZ", "CLI", "HERMES_BIN", "MODE",
              "TIMEOUT", "WORKERS", "APPROVAL_TIMEOUT", "MODEL",
              "TOKEN_LIMIT", "CALL_TOKEN_LIMIT", "COST_LIMIT_USD", "AUTH_USER",
              "AUTH_PASS", "LOOPBACK", "HOME_LEGACY",
              "VAULT", "PROJECT_FILE", "ROSTER_FILE", "CRED_FILE",
              "MATERIALS_FILE", "MATERIALS_ROOT",
              "LEGACY_USERS_DIR", "USERS_DIR", "USERS_DB", "OWNER_PHONE",
              "OWNER_DATA_DIR", "CAPTCHA_TTL", "CAPTCHA_SECRET",
              "SESSION_COOKIE", "SESSION_TTL",
              "MAIL_DOMAIN", "MAIL_HOST", "MAIL_SMTP_PORT", "MAIL_CRED_FILE",
              "MAIL_AUDIT", "BOSS_MAIL",
              "OM_DEFS_DIR",
              "ORG_PROFILES", "ORG_AUDIT"]
code_paths, rm_paths = cut(CONST_PATH)
code_paths = code_paths.replace("BASE_DIR = os.path.dirname(os.path.abspath(__file__))",
                                "BASE_DIR = os.path.dirname(os.path.abspath(__file__))")
open(os.path.join(BASE, "opc_paths.py"), "w", encoding="utf-8").write(
    '"""路径与运行参数常量（全部模块共用；常量不可重绑，可安全 from-import）。"""\n'
    "import os\nimport secrets\nimport threading\n"
    "from datetime import timezone, timedelta\n\n" + code_paths)

# ---------- 2. utils.py（工具函数） ----------
code_utils, rm_utils = cut(["iso_now", "uid", "_hotkey", "_step_view", "_iso_shift", "_to_int", "_num"])
open(os.path.join(BASE, "utils.py"), "w", encoding="utf-8").write(
    '"""通用小工具（时间 / ID / 数值转换 / 热点键）。"""\n'
    "import time\nimport uuid\nimport json\n"
    "from datetime import datetime, timedelta, timezone\n\n"
    "TZ = timezone(timedelta(hours=8))\n\n" + code_utils)

# ---------- 3. agent_defaults.py（默认编制数据） ----------
code_ad, rm_ad = cut(["DEFAULT_AGENT_SPEC", "AGENTS_REQUIRED"])
open(os.path.join(BASE, "agent_defaults.py"), "w", encoding="utf-8").write(
    '"""内置默认编制（与 Obsidian 员工名册一致）：owner 回退与注册用户模板共用。"""\n\n' + code_ad)

# ---------- 4. mail.py（内部邮件） ----------
code_mail, rm_mail = cut(MAIL_NAMES)
code_mail = prefix(code_mail, {n: "OPC" for n in []})
open(os.path.join(BASE, "mail.py"), "w", encoding="utf-8").write(
    '"""内部邮件：以员工邮箱收发（SMTP），仅限内部域。"""\n'
    "import json\nimport os\nimport re\nimport smtplib\nimport ssl\nimport time\n"
    "from email.message import EmailMessage\nfrom email.utils import formataddr, formatdate, make_msgid\n\n"
    "from utils import iso_now, uid, _to_int\n" + code_mail)

# ---------- 5. company_config.py（项目/编制/交付/车间配置） ----------
seg_cc, rm_cc = cut(["DEFAULT_PROJECTS", "DEFAULT_TONE", "OPENMONTAGE_CRAFTS",
                     "CRAFT_BY_ID", "DEFAULT_CRAFT_ID", "CRAFT_STEP_NAMES",
                     "CRAFT_STEP_OPC", "_load_craft_steps", "CRAFT_STEPS",
                     "FACTORY_ENTRY_STAGE", "FACTORY_EXIT_STAGE", "DEFAULT_HIGH_RISK",
                     "DEFAULT_DELIVERY", "_parse_delivery_cfg", "_load_delivery_cfg",
                     "DELIVERY", "_parse_project_cfg", "_load_project_cfg",
                     "PROJECT_BY_NAME", "_parse_roster",
                     "_load_agent_spec", "AGENT_SPEC", "_load_dl"])
seg_cc = re.sub(r"\bDBX\b", "None  # noqa", seg_cc) if False else seg_cc
open(os.path.join(BASE, "company_config.py"), "w", encoding="utf-8").write(
    '"""公司配置：项目 / 员工编制 / 交付合同 / OpenMontage 车间工序（vault 加载与回退）。"""\n'
    "import ast\nimport json\nimport os\nimport re\n\n"
    "from opc_paths import *  # noqa: F401,F403\n"
    "from utils import _to_int, _num\n"
    "import pipeline_defs as PIPE\n\n" + seg_cc)

# ---------- 6. users.py（登录凭据 / 注册表 / 校验码 / 工作区） ----------
seg_users, rm_users = cut(["CRED_ALIAS", "load_credentials", "find_user", "_users_conn",
                           "_hash_password", "_user_get", "_user_create",
                           "_migrate_owner_workspace", "_user_roster_md", "_load_dl",
                           "_captcha_sign", "_captcha_issue", "_captcha_verify",
                           "SESSIONS", "SESS_LOCK", "LOGIN_FAILS", "LOGIN_LOCK"])
seg_users = prefix(seg_users, {"DEFAULT_AGENT_SPEC": "AD", "PROJECTS": "CC", "_parse_roster": "CC"})
open(os.path.join(BASE, "users.py"), "w", encoding="utf-8").write(
    '"""用户体系：登录凭据（Obsidian）/ 注册表（users.db）/ 校验码 / 独立工作区工厂。"""\n'
    "import hashlib\nimport hmac\nimport json\nimport os\nimport re\nimport secrets\n"
    "import sqlite3\nimport shutil\nimport time\n\n"
    "from opc_paths import *  # noqa: F401,F403\n"
    "from utils import iso_now, uid\n"
    "import db as DBX\n"
    "import agent_defaults as AD\n"
    "import company_config as CC\n\n" + seg_users)

# ---------- 7. org.py（组织配置读写与审计） ----------
seg_org, rm_org = cut(["ORG_LOCK", "POLICY_FILES", "ORG_FILE_LIMIT"] + ORG_NAMES)
seg_org = prefix(seg_org, {n: "CC" for n in CC_NAMES if n in ("AGENT_SPEC", "TONE", "PROJECTS", "PROJECT_BY_NAME", "_parse_roster")})
open(os.path.join(BASE, "org.py"), "w", encoding="utf-8").write(
    '"""组织配置：岗位/制度/技能/记忆 的读写、同步与审计（写 Obsidian 与 hermes profile）。"""\n'
    "import json\nimport os\nimport re\nimport threading\n\n"
    "from opc_paths import *  # noqa: F401,F403\n"
    "from utils import _to_int, iso_now\n"
    "import company_config as CC\n\n" + seg_org)

# ---------- 8. llm_registry.py（大模型注册表） ----------
seg_llm, rm_llm = cut(LREG_NAMES + ["_parse_profile_model"])
seg_llm = prefix(seg_llm, {n: "CC" for n in ("AGENT_SPEC", "_parse_roster")})
seg_llm = re.sub(r"\b_org_audit\b", "ORG._org_audit", seg_llm)
open(os.path.join(BASE, "llm_registry.py"), "w", encoding="utf-8").write(
    '"""大模型注册表：可用通道 / 额度 / 状态，Hermes 调用前检查与扣减；通道写入 profile。"""\n'
    "import json\nimport os\nimport re\nimport subprocess\nimport sqlite3\n"
    "import time\n\n"
    "from opc_paths import *  # noqa: F401,F403\n"
    "from utils import iso_now, _to_int\n"
    "import db as DBX\n"
    "import company_config as CC\n"
    "import org as ORG\n\n" + seg_llm)

# ---------- 9. hermes_adapter.py（Hermes 调用适配） ----------
seg_ha, rm_ha = cut(["HermesAdapter", "HERMES"])
seg_ha = prefix(seg_ha, {n: "CC" for n in ("PROJECTS",)})
seg_ha = prefix(seg_ha, {n: "LREG" for n in ("_llm_store", "_llm_consume")})
open(os.path.join(BASE, "hermes_adapter.py"), "w", encoding="utf-8").write(
    '"""Hermes 调用适配：员工提示词组装、子进程调用、JSON 提取、用量账本、注册表门槛。"""\n'
    "import json\nimport os\nimport re\nimport subprocess\nimport tempfile\nimport threading\nimport time\n"
    "from urllib.parse import quote, urlparse\n"
    "import queue\n\n"
    "from opc_paths import *  # noqa: F401,F403\n"
    "from utils import iso_now, _to_int\n"
    "import company_config as CC\n"
    "import llm_registry as LREG\n\n" + seg_ha)

# ---------- 10. 重写 server.py：删除已搬移段，插入导入头，应用前缀替换 ----------
keep = [ln for i, ln in enumerate(lines, 1) if i not in
        (rm_paths | rm_utils | rm_ad | rm_mail | rm_cc | rm_users | rm_org | rm_llm | rm_ha)]
server = "".join(keep)

# 移除旧 import（mail 相关的 smtplib/ssl/email 不再需要；zipfile/io 若残余使用保留）
server = server.replace("import smtplib\n", "").replace("import ssl\n", "")

# 前缀替换（对 server 剩余部分）
server = prefix(server, {n: "CC" for n in CC_NAMES})
server = prefix(server, {n: "USERS" for n in USER_NAMES})
server = prefix(server, {n: "ORG" for n in ORG_NAMES})
server = prefix(server, {n: "LREG" for n in LREG_NAMES})
server = prefix(server, {n: "MAILM" for n in MAIL_NAMES})

# 插入导入头（放在原 import 块末尾之后：找最后一个顶层 from/import 行）
m = None
for mm in re.finditer(r"^(?:import|from)\s.*$", server, re.M):
    m = mm
insert_at = m.end() if m else 0
header = ("\n# ---------------- 模块化拆分后的组合根：功能模块见下列各文件 ----------------\n"
          "from opc_paths import *  # noqa: F401,F403 —— 路径与运行常量\n"
          "from utils import iso_now, uid, _to_int, _hotkey, _step_view, _iso_shift, _num\n"
          "from agent_defaults import DEFAULT_AGENT_SPEC, AGENTS_REQUIRED\n"
          "import company_config as CC\n"
          "import users as USERS\n"
          "import org as ORG\n"
          "import llm_registry as LREG\n"
          "import mail as MAILM\n"
          "from hermes_adapter import HERMES\n"
          "from users import (SESSIONS, SESS_LOCK, SESSION_COOKIE, SESSION_TTL,\n"
          "                   LOGIN_FAILS, LOGIN_LOCK)\n")
server = server[:insert_at] + header + server[insert_at:]

open(SRC, "w", encoding="utf-8").write(server)
print("拆分完成：", [f for f in os.listdir(BASE) if f.endswith(".py")])
