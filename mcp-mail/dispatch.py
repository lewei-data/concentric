#!/usr/bin/env python3
"""OPC 邮件自动派工与回执（方案 B）。

流程：扫描 16 个员工邮箱的未读邮件 → 跳过自动回执/退信/历史/自身/超时邮件 →
调用该员工 Hermes profile 处理 → 由本脚本统一回信（带 Auto-Submitted 头防环）→ 落状态库。

用法：
    python3 dispatch.py                    # 全量扫描
    python3 dispatch.py --account cw_gou   # 只处理指定邮箱（联调用）
    python3 dispatch.py --limit 3          # 单次最多处理 3 封
"""

import argparse
import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, "/opt/sefs/opc-workbench/mcp-mail")
import server as M  # noqa: E402

# 统一执行入口：优先常驻 gateway /v1/runs，不可用时回落 run-hermes.sh 子进程
sys.path.insert(0, "/opt/sefs/hermes")
try:
    import gw_runs_client as _GW
except Exception:  # noqa: BLE001 - 客户端不可用不应阻断邮件派单
    _GW = None
HERMES_EXEC_MODE = (os.environ.get("HERMES_EXEC_MODE") or "auto").strip().lower()

BASE = "/opt/sefs/opc-workbench/mcp-mail/dispatch"
STATE_DB = os.path.join(BASE, "state.db")
HERMES = "/opt/sefs/hermes/run-hermes.sh"
ROSTER = "/mnt/sefs/conring/users/18911789286/obsidian/公司/员工名册.md"

ACCOUNTS = ["cco", "ceo", "cto", "pm_wenren", "pm_gou", "pm_tongxinyuan",
            "cw_wenren", "cw_gou", "cw_tongxinyuan", "vd_wenren", "vd_gou",
            "vd_tongxinyuan", "release_ops", "reviewer", "scout", "kb_manager"]

MAX_PER_RUN = 6           # 单次整体最多处理的邮件数，避免一次拉起过多模型调用
MAX_AGE_HOURS = 24        # 超过 24 小时的邮件不自动回复
HOURLY_REPLY_LIMIT = 20   # 每个邮箱每小时最多自动回执数
AGENT_TIMEOUT = int(os.environ.get("HERMES_AGENT_TIMEOUT", "86400"))  # 单次模型处理超时（秒），默认 24h：渲染/生图这类长任务需要
OPC_DELEGATE_TIMEOUT = 60  # 派单接口等待上限；超时视为已提交，编排层继续处理，避免阻塞后续邮件
MAX_REPLY_CHARS = 1200    # 回信正文上限
SKIP_SUBJECTS = ("[provision]", "[自检]", "[联调]", "Undelivered Mail", "Mail delivery failed")
SKIP_SENDERS = ("mailer-daemon", "postmaster", "noreply", "no-reply", "do-not-reply")
# 【】前缀默认视为全员广播，但工单类主题必须放行，否则跨岗位派单会被吞掉
TASK_SUBJECT_PREFIXES = ("【派单】", "【交接】", "【任务】", "【工单】")


def log(msg):
    sys.stderr.write("[dispatch %s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def db():
    os.makedirs(BASE, exist_ok=True)
    conn = sqlite3.connect(STATE_DB, timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS processed(
        mid TEXT PRIMARY KEY, account TEXT, uid TEXT, sender TEXT, subject TEXT,
        status TEXT, attempts INTEGER DEFAULT 0, ts TEXT, reply_mid TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS replies(
        id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, ts TEXT, to_addr TEXT, mid TEXT)""")
    conn.commit()
    return conn


def roster_roles():
    roles = {}
    try:
        with open(ROSTER, encoding="utf-8") as f:
            for line in f:
                if not line.startswith("|"):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) >= 6 and not cells[0].startswith("Agent"):
                    roles[cells[0]] = cells[2]
    except OSError:
        pass
    return roles


def decode(value):
    if not value:
        return ""
    return str(email.header.make_header(email.header.decode_header(value))).strip()


def header_msg(conn, uid):
    typ, d = conn.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID "
                                  "AUTO-SUBMITTED PRECEDENCE X-OPC-DISPATCH IN-REPLY-TO REFERENCES TO CC)])")
    raw = b""
    for p in d or []:
        if isinstance(p, tuple):
            raw = p[1] or b""
    return email.message_from_bytes(raw)


def in_auto_thread(conn_db, head):
    """来信是否在“本机制已自动回执过”的线程里（依据 In-Reply-To / References）。"""
    refs = re.findall(r"<[^>]+>", (head.get("In-Reply-To") or "") + " " + (head.get("References") or ""))
    for mid in refs:
        if conn_db.execute("SELECT 1 FROM replies WHERE mid=?", (mid,)).fetchone():
            return True
    return False


def imap_login(addr, password):
    ctx = ssl._create_unverified_context()
    c = imaplib.IMAP4_SSL(M.HOST, M.IMAP_PORT, ssl_context=ctx, timeout=30)
    c.login(addr, password)
    return c


def baseline(conn_db):
    """首次运行：把现有邮件全部登记为基线，不回复历史邮件。"""
    n = 0
    for aid in ACCOUNTS:
        addr = aid + "@" + M.DOMAIN
        try:
            c = imap_login(addr, M.load_accounts()[addr][1])
            c.select("INBOX", readonly=True)
            typ, d = c.uid("search", None, "ALL")
            for uid in (d[0] or b"").split()[-500:]:
                head = header_msg(c, uid)
                mid = head.get("Message-ID") or "uid:%s:%s" % (aid, uid.decode())
                conn_db.execute("INSERT OR IGNORE INTO processed VALUES (?,?,?,?,?,?,?,?,?)",
                                (mid, aid, uid.decode(), decode(head.get("From")),
                                 decode(head.get("Subject")), "baseline", 0,
                                 time.strftime("%Y-%m-%dT%H:%M:%S"), ""))
                n += 1
            c.logout()
        except Exception as e:
            log("基线登记失败 %s: %s" % (aid, e))
    conn_db.commit()
    log("首次运行：基线登记 %d 封，历史邮件不自动回复" % n)


def recent_replies(conn_db, aid):
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3600))
    return conn_db.execute("SELECT COUNT(*) FROM replies WHERE account=? AND ts>=?",
                           (aid, since)).fetchone()[0]


def run_agent(aid, role, uid, sender, subject):
    prompt = (
        "你是 OPC 一人公司员工「%s」，岗位：%s。\n"
        "你的收件箱有一封新邮件：UID=%s，发件人=%s，主题=%s。\n"
        "请按以下步骤处理：\n"
        "1) 调用 mcp__mail__mail_read 读取这封邮件（folder 用 INBOX，uid 用 %s）；\n"
        "2) 按你的岗位职责与公司制度处理：能在权限内直接办的先办；确有跨岗位任务时，"
        "写进 delegations 由编排层正式下发；\n"
        "3) 只输出一个 JSON 对象（不要代码块、不要多余文字）：\n"
        '{"reply": "给发件人的回信正文（纯文本，≤400 字）", '
        '"delegations": [{"recipient": "员工ID", "text": "下发给该同事的任务，≤120 字"}]}\n'
        "delegations 最多 2 条，没有就填空数组。recipient 只能是：%s。\n"
        "硬性约束：不得调用 mcp__mail__mail_send；不得对发布、上线、对外承诺、花钱作任何承诺；"
        "涉及审批的事项只回复“已收到，等待老板审批”；不得声称已安排或已通知其他同事"
        "（正式下发由系统执行，你只在 delegations 里提出）；不得在回信中泄露密码或凭据。"
        % (M.load_accounts().get("%s@%s" % (aid, M.DOMAIN), ("", ""))[0], role, uid, sender, subject, uid,
           "、".join(ACCOUNTS))
    )
    out = ""
    if _GW is not None and HERMES_EXEC_MODE != "subprocess":
        try:
            # 每封邮件独立处理，不带会话上下文
            out = _GW.run(prompt, profile=aid, timeout=AGENT_TIMEOUT).text
        except (_GW.GatewayUnavailable, _GW.GatewayBusy) as e:
            if HERMES_EXEC_MODE == "gateway":
                raise
            log("gateway 不可用（%s），回落子进程 %s" % (e, aid))
    if not out:
        env = dict(os.environ)
        env["HERMES_PROFILE"] = aid
        proc = subprocess.run([HERMES, "-z", prompt], capture_output=True, text=True,
                              timeout=AGENT_TIMEOUT, env=env)
        out = (proc.stdout or "").strip()
        if not out:
            raise RuntimeError("模型无输出：%s" % (proc.stderr or "")[-200:])
    out = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", out).strip()
    return parse_agent_output(out)


def parse_agent_output(out):
    """解析 {reply, delegations}；失败时把原文当回信、不下发。"""
    obj = None
    try:
        obj = json.loads(out)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", out)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None
    if not isinstance(obj, dict):
        return out[:MAX_REPLY_CHARS], []
    reply = str(obj.get("reply") or "").strip() or out[:MAX_REPLY_CHARS]
    delegations = []
    for d in (obj.get("delegations") or [])[:2]:
        if not isinstance(d, dict):
            continue
        rid = str(d.get("recipient") or "").strip()
        text = str(d.get("text") or "").strip()
        if rid in ACCOUNTS and text:
            delegations.append({"recipient": rid, "text": text[:200]})
    return reply[:MAX_REPLY_CHARS], delegations


# ---------------------------------------------------------------- 编排层派单
OPC_BASE = os.environ.get("OPC_BASE", "http://127.0.0.1:8848")
LOGIN_CRED = "/mnt/sefs/conring/users/18911789286/obsidian/公司/登录凭据.md"
MGMT = ("ceo", "cto", "cco")
_cookie = [""]
_AGENT_NAMES = {}


def agent_names():
    if _AGENT_NAMES:
        return _AGENT_NAMES
    try:
        with open(ROSTER, encoding="utf-8") as f:
            for line in f:
                if not line.startswith("|"):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) >= 2 and not cells[0].startswith("Agent"):
                    _AGENT_NAMES[cells[0]] = cells[1]
    except OSError:
        pass
    return _AGENT_NAMES


def boss_login():
    """用老板账号换取编排层会话（派单接口需要登录态）。"""
    if _cookie[0]:
        return _cookie[0]
    user = pwd = ""
    try:
        with open(LOGIN_CRED, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip().startswith("|")]
        header = None
        for ln in lines:
            cells = [c.strip().strip("`") for c in ln.strip("|").split("|")]
            if "用户名" in cells and "密码" in cells:
                header = cells
                continue
            if not header or set("".join(cells)) <= set("-: "):
                continue
            row = dict(zip(header, cells))
            if row.get("密码"):
                user, pwd = row.get("用户名", ""), row["密码"]
                if row.get("角色") == "老板":
                    break
    except OSError as e:
        log("读取登录凭据失败：%s" % e)
        return ""
    if not user or not pwd:
        return ""
    req = urllib.request.Request(OPC_BASE + "/api/login",
                                 data=json.dumps({"account": user, "password": pwd}).encode(),
                                 headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=15)
    _cookie[0] = resp.headers.get("Set-Cookie", "").split(";")[0]
    return _cookie[0]


def opc_delegate(sender, recipient, text):
    """调用编排层 /api/delegate，正式下发工单并等待回执。"""
    cookie = boss_login()
    if not cookie:
        raise RuntimeError("无法登录编排层")
    body = json.dumps({"sender": sender, "recipient": recipient, "text": text}).encode()
    req = urllib.request.Request(OPC_BASE + "/api/delegate", data=body,
                                 headers={"Content-Type": "application/json", "Cookie": cookie})
    data = json.load(urllib.request.urlopen(req, timeout=OPC_DELEGATE_TIMEOUT))
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "派单失败")
    return data


def process_account(conn_db, aid, roles, budget):
    addr = aid + "@" + M.DOMAIN
    password = M.load_accounts()[addr][1]
    c = imap_login(addr, password)
    done = 0
    try:
        c.select("INBOX", readonly=False)
        typ, d = c.uid("search", None, "UNSEEN")
        uids = (d[0] or b"").split()
        # 失败/中断的历史邮件即使已被读，也要按状态库取回重试
        retry = [r[0].encode() for r in conn_db.execute(
            "SELECT uid FROM processed WHERE account=? AND status IN ('pending','failed','timeout') "
            "AND attempts<2 AND uid<>''", (aid,)).fetchall()]
        if retry:
            uids = sorted(set(uids) | set(retry), key=lambda x: int(x))
        for uid in uids:
            if budget[0] <= 0:
                break
            head = header_msg(c, uid)
            mid = head.get("Message-ID") or "uid:%s:%s" % (aid, uid.decode())
            row = conn_db.execute("SELECT status, attempts FROM processed WHERE mid=?", (mid,)).fetchone()
            if row:
                status, attempts = row[0], row[1] or 0
                retryable = status in ("pending", "failed", "timeout") and attempts < 2
                if not retryable:
                    continue
                log("%s 重试 uid=%s（上次 %s）" % (aid, uid.decode(), status))

            sender_raw = decode(head.get("From"))
            sender = email.utils.parseaddr(sender_raw)[1].lower()
            subject = decode(head.get("Subject"))
            auto = (head.get("Auto-Submitted") or "").strip().lower()
            skip = None
            if head.get("X-OPC-Dispatch"):
                skip = "skip:auto-dispatch"
            elif auto and auto != "no":
                skip = "skip:auto-submitted"
            elif in_auto_thread(conn_db, head):
                skip = "skip:auto-thread"
            elif not sender.endswith("@" + M.DOMAIN) or sender == addr:
                skip = "skip:sender"
            elif any(s in subject for s in SKIP_SUBJECTS):
                skip = "skip:subject"
            elif (subject.startswith("【") and sender != "boss@" + M.DOMAIN
                  and not subject.startswith(TASK_SUBJECT_PREFIXES)):
                skip = "skip:broadcast"
            elif any(s in sender for s in SKIP_SENDERS):
                skip = "skip:system-sender"
            else:
                try:
                    ts = email.utils.parsedate_to_datetime(head.get("Date", "")).timestamp()
                    if time.time() - ts > MAX_AGE_HOURS * 3600:
                        skip = "skip:too-old"
                except Exception:
                    pass
                if not skip and recent_replies(conn_db, aid) >= HOURLY_REPLY_LIMIT:
                    skip = "skip:hourly-limit"

            if skip:
                conn_db.execute("INSERT OR IGNORE INTO processed VALUES (?,?,?,?,?,?,?,?,?)",
                                (mid, aid, uid.decode(), sender, subject, skip, 0,
                                 time.strftime("%Y-%m-%dT%H:%M:%S"), ""))
                conn_db.commit()
                log("%s 跳过 uid=%s（%s）" % (aid, uid.decode(), skip))
                continue

            conn_db.execute("INSERT OR IGNORE INTO processed VALUES (?,?,?,?,?,?,?,?,?)",
                            (mid, aid, uid.decode(), sender, subject, "pending", 0,
                             time.strftime("%Y-%m-%dT%H:%M:%S"), ""))
            conn_db.execute("UPDATE processed SET status='pending', uid=?, sender=?, subject=? WHERE mid=?",
                            (uid.decode(), sender, subject, mid))
            conn_db.commit()
            log("%s 处理 uid=%s 来自 %s：%s" % (aid, uid.decode(), sender, subject[:40]))
            try:
                reply, delegations = run_agent(aid, roles.get(aid, aid), uid.decode(), sender, subject)
                if delegations and aid in MGMT:
                    extra = "\n\n（跨岗位事项将由编排层下发，处理结果见系统事件流）"
                elif delegations:
                    extra = "\n\n（跨岗位事项仅作建议：%s）" % "；".join(
                        "%s：%s" % (agent_names().get(d["recipient"], d["recipient"]), d["text"])
                        for d in delegations)
                else:
                    extra = ""
                body = reply + extra
                subj = subject if re.match(r"(?i)^(re|aw|sv)\s*[:：]", subject) or subject.startswith("回复：") \
                    else "Re: " + subject
                prev = os.environ.get("MAIL_AGENT"), os.environ.get("MAIL_ROLE")
                os.environ["MAIL_AGENT"] = aid
                os.environ["MAIL_ROLE"] = roles.get(aid, aid)
                try:
                    res = M.mail_send({"to": sender, "subject": subj,
                                       "body": body + "\n\n（本邮件由 OPC 邮件派工机制自动回执）",
                                       "auto_reply": True})
                finally:
                    for key, val in zip(("MAIL_AGENT", "MAIL_ROLE"), prev):
                        if val is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = val
                conn_db.execute("UPDATE processed SET status='done', reply_mid=?, ts=? WHERE mid=?",
                                (res.get("message_id", ""), time.strftime("%Y-%m-%dT%H:%M:%S"), mid))
                conn_db.execute("INSERT INTO replies(account, ts, to_addr, mid) VALUES (?,?,?,?)",
                                (aid, time.strftime("%Y-%m-%dT%H:%M:%S"), sender,
                                 res.get("message_id", "")))
                conn_db.commit()
                done += 1
                budget[0] -= 1
                log("%s 已回执 → %s" % (aid, sender))
                # 回信先发（保证及时），随后正式派单；派单失败只记日志，不回滚已发回执
                if delegations and aid in MGMT:
                    for d in delegations:
                        rid = d["recipient"]
                        try:
                            res_d = opc_delegate(aid, rid, d["text"])
                            log("%s 派单 → %s：%s｜trace %s"
                                % (aid, rid, d["text"][:40], res_d.get("traceId") or "-"))
                        except Exception as e:
                            log("%s 派单未即时回执 → %s：%s（编排层可能仍在处理）"
                                % (aid, rid, str(e)[:140]))
            except subprocess.TimeoutExpired:
                conn_db.execute("UPDATE processed SET status='timeout', attempts=attempts+1, ts=? WHERE mid=?",
                                (time.strftime("%Y-%m-%dT%H:%M:%S"), mid))
                conn_db.commit()
                log("%s uid=%s 处理超时" % (aid, uid.decode()))
            except Exception as e:
                conn_db.execute("UPDATE processed SET status='failed', attempts=attempts+1, ts=? WHERE mid=?",
                                (time.strftime("%Y-%m-%dT%H:%M:%S"), mid))
                conn_db.commit()
                log("%s uid=%s 处理失败：%s" % (aid, uid.decode(), str(e)[:160]))
    finally:
        try:
            c.logout()
        except Exception:
            pass
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", help="只处理指定 agent id")
    ap.add_argument("--limit", type=int, default=MAX_PER_RUN)
    args = ap.parse_args()

    conn_db = db()
    empty = conn_db.execute("SELECT COUNT(*) FROM processed").fetchone()[0] == 0
    if empty:
        baseline(conn_db)
        log("基线建立完成，本次不处理邮件")
        return

    roles = roster_roles()
    ids = [args.account] if args.account else ACCOUNTS
    budget = [max(1, args.limit)]
    total = 0
    for aid in ids:
        if budget[0] <= 0:
            break
        if aid not in ACCOUNTS:
            log("未知账号 %s" % aid)
            continue
        try:
            total += process_account(conn_db, aid, roles, budget)
        except Exception as e:
            log("%s 扫描失败：%s" % (aid, e))
    log("本轮完成，回执 %d 封，预算剩余 %d" % (total, budget[0]))


if __name__ == "__main__":
    main()
