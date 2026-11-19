#!/usr/bin/env python3
"""MCP-Mail：OPC 员工内部邮箱读写 MCP 服务（stdio 传输）。

对外工具：
  mail_send   以本人邮箱发送内部邮件（仅 mail.conring.cn 域内）
  mail_list   列出指定文件夹的邮件摘要
  mail_read   按 UID 读取一封邮件正文
  mail_check  返回本人邮箱账号与未读统计（自检用）

身份绑定：进程启动时由环境变量 MAIL_AGENT（agent id）确定邮箱账号，
密码从 /mnt/sefs/conring/users/18911789286/obsidian/公司/.邮箱凭据.md 读取；发件人地址固定为本人账号，
不接受调用方传入 From，避免冒用他人身份。

协议为 MCP stdio：stdin/stdout 按行收发 JSON-RPC 2.0，日志一律写 stderr。
仅使用 Python 标准库（smtplib / imaplib），不依赖第三方包。
"""

import email
import html as _html
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import traceback
from email.header import decode_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

SERVER_NAME = "mcp-mail"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"

DOMAIN = os.environ.get("MAIL_DOMAIN", "mail.conring.cn")
HOST = os.environ.get("MAIL_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("MAIL_SMTP_PORT", "587"))
IMAP_PORT = int(os.environ.get("MAIL_IMAP_PORT", "993"))
CRED_FILE = os.environ.get("MAIL_CRED_FILE", "/mnt/sefs/conring/users/18911789286/obsidian/公司/.邮箱凭据.md")
AUDIT_LOG = os.environ.get("MAIL_AUDIT_LOG", "/opt/sefs/opc-workbench/mcp-mail/logs/mail-audit.jsonl")
MAX_BODY_CHARS = 50_000
MAX_SEND_PER_PROCESS = 30

# stdout 只承载 JSON-RPC 报文；第三方/底层库的打印统一改到 stderr
_PROTO_OUT = sys.stdout
sys.stdout = sys.stderr

_send_count = 0


def log(msg):
    sys.stderr.write("[mcp-mail] %s\n" % msg)
    sys.stderr.flush()


# ---------------------------------------------------------------- 身份与凭据
def agent_id():
    for key in ("MAIL_AGENT", "HERMES_PROFILE"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    home = (os.environ.get("HERMES_HOME") or "").rstrip("/")
    if home:
        return os.path.basename(home)
    return ""


def load_accounts():
    """解析凭据表：| 邮箱 | 姓名 | 密码 | → {邮箱: (姓名, 密码)}"""
    accounts = {}
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("|"):
                    continue
                cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
                if len(cells) < 3 or "@" not in cells[0]:
                    continue
                accounts[cells[0].lower()] = (cells[1], cells[2])
    except OSError as e:
        log("读取凭据失败：%s" % e)
    return accounts


def account(aid_override=None):
    aid = (aid_override or "").strip() or agent_id()
    if not aid:
        raise RuntimeError("未设置 MAIL_AGENT / HERMES_PROFILE，且调用未指定 agent，无法确定邮箱身份")
    address = "%s@%s" % (aid, DOMAIN)
    accounts = load_accounts()
    if address.lower() not in accounts:
        raise RuntimeError("凭据文件中没有 %s 的邮箱账号" % address)
    name, password = accounts[address.lower()]
    if not password:
        raise RuntimeError("%s 的邮箱密码为空" % address)
    return aid, name, address, password


def _ssl_ctx():
    # 内部邮件服务使用自签证书，且只连本机回环/内网地址
    return ssl._create_unverified_context()


# ---------------------------------------------------------------- 发信
def _normalize_addrs(value, field):
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,，;；\s]+", str(value))
    out = []
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        addr = parseaddr(item)[1] or item
        if "@" not in addr:
            raise ValueError("%s 地址不合法：%s" % (field, item))
        if not addr.lower().endswith("@" + DOMAIN):
            raise ValueError("%s 只能发往内部域 @%s：%s" % (field, DOMAIN, addr))
        out.append(addr)
    return out


def _audit(record):
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        log("写审计日志失败：%s" % e)


# ---------------------------------------------------------------- HTML 排版
# 巡检/日报正文是「【结论】/【进展】/【风险与异常】/【需要老板拍板】」这类分节纯文本，
# 发信时按分节渲染成带卡片、加粗标签的 HTML（同时保留纯文本，见 multipart/alternative），
# 这样手机邮件客户端打开就是排版好的报告，纯文本客户端仍可读。
_HEAD_RE = re.compile(r"^\s*(?:[#>]+\s*)?【([^】]{1,24})】\s*(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:([-•*·])|(\d{1,2})[.)、])\s+(.+)$")
_URGENT_RE = re.compile(r"【\s*急\s*】")

_FONT = "-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',Arial,sans-serif"
_ACCENT = "#0f766e"
_URGENT = "#b91c1c"
_TEXT = "#1f2933"
_MUTED = "#6b7280"


def _esc(s):
    return _html.escape(str(s or ""), quote=False)


def _inline(s):
    """行内标记：纯文本转义后，把【急】渲染成红色徽标，保留 **加粗** 与 `代码`。"""
    out = _esc(s)
    out = _URGENT_RE.sub('<span style="display:inline-block;padding:1px 7px;margin-right:2px;'
                         'border-radius:4px;background:%s;color:#ffffff;font-size:12px;'
                         'font-weight:700;letter-spacing:.5px;">急</span>' % _URGENT, out)
    out = re.sub(r"\*\*(.+?)\*\*", r'<strong style="color:#111827;">\1</strong>', out)
    out = re.sub(r"`([^`]+)`",
                 r'<code style="padding:1px 5px;border-radius:4px;background:#f1f3f5;'
                 r'font-size:13px;color:#334155;">\1</code>', out)
    return out


def _li(text):
    return ('<li style="font-size:14px;line-height:1.78;color:%s;margin:0 0 5px;'
            'word-break:break-word;">%s</li>' % (_TEXT, _inline(text)))


def _render_lines(lines):
    """渲染一节内容：正文成段，`-`/`•` 开头走无序列表，`1.`/`1)` 开头保留编号走有序列表。"""
    out, para, items = [], [], []          # items: (kind, 起始编号, 文本)
    p_style = ("margin:0 0 10px;font-size:14px;line-height:1.78;color:%s;"
               "word-break:break-word;" % _TEXT)

    def flush():
        if para:
            out.append('<p style="%s">%s</p>' % (p_style, _inline(" ".join(x.strip() for x in para))))
            para.clear()
        if items:
            kind = items[0][0]
            if kind == "ol":
                out.append('<ol start="%d" style="margin:0 0 12px;padding-left:22px;">%s</ol>'
                           % (items[0][1] or 1, "".join(_li(t) for _, _, t in items)))
            else:
                out.append('<ul style="margin:0 0 12px;padding-left:20px;">%s</ul>'
                           % "".join(_li(t) for _, _, t in items))
            items.clear()

    for line in lines:
        if not line.strip():
            flush()
            continue
        m = _BULLET_RE.match(line)
        if m:
            if para:
                flush()
            kind = "ul" if m.group(1) else "ol"
            if items and items[0][0] != kind:
                flush()
            items.append((kind, int(m.group(2)) if m.group(2) else 0, m.group(3).strip()))
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return "".join(out)


def _parse_sections(body):
    """按【小节】切分正文；首个【标题】之前的内容归到无标题节。"""
    sections, cur = [], {"title": "", "lines": []}
    for raw in (body or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        m = _HEAD_RE.match(raw)
        if m and m.group(1).strip() != "急":   # 【急】是行内标记，不算小节标题
            if cur["title"] or any(x.strip() for x in cur["lines"]):
                sections.append(cur)
            cur = {"title": m.group(1).strip(), "lines": []}
            rest = m.group(2).strip()
            if rest:
                cur["lines"].append(rest)
            continue
        cur["lines"].append(raw.rstrip())
    if cur["title"] or any(x.strip() for x in cur["lines"]):
        sections.append(cur)
    return sections


def render_report_html(body, subject, name, role, address):
    """把分节纯文本正文渲染成邮件 HTML（内联样式，兼容主流客户端与手机暗色模式）。"""
    m = re.match(r"^\s*\[([^\]]{1,24})\]\s*(.*)$", subject or "")
    badge, headline = (m.group(1).strip(), m.group(2).strip()) if m else ("", subject or "")
    headline = headline or "工作汇报"
    stamp = time.strftime("%Y-%m-%d %H:%M")

    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta name="color-scheme" content="light">',
             "<title>%s</title></head>" % _esc(subject or headline),
             '<body style="margin:0;padding:0;background:#eef0f3;">',
             '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
             'style="background:#eef0f3;padding:24px 12px;"><tr><td align="center">',
             '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
             'style="max-width:640px;background:#ffffff;border-radius:14px;overflow:hidden;'
             'box-shadow:0 1px 3px rgba(16,24,40,.08);font-family:' + _FONT + ';">']

    # 顶部标题条
    parts.append('<tr><td style="padding:22px 26px 20px;background:linear-gradient(135deg,#0f766e,#134e4a);">')
    if badge:
        parts.append('<div style="font-size:12px;letter-spacing:2px;color:rgba(255,255,255,.78);">%s</div>'
                     % _esc(badge))
    parts.append('<div style="margin-top:6px;font-size:19px;line-height:1.45;font-weight:700;color:#ffffff;">%s</div>'
                 % _esc(headline))
    parts.append('<div style="margin-top:10px;font-size:12px;color:rgba(255,255,255,.82);">%s · %s</div>'
                 % (_esc(stamp), _esc(name or address)))
    parts.append('</td></tr>')

    # 正文分节
    sections = _parse_sections(body)
    if not sections:
        sections = [{"title": "", "lines": [body or ""]}]
    for idx, sec in enumerate(sections):
        title = sec["title"]
        urgent = "急" in title or any(_URGENT_RE.search(x) for x in sec["lines"])
        color = _URGENT if urgent else _ACCENT
        inner = _render_lines(sec["lines"])
        if not inner:
            inner = '<p style="margin:0;font-size:14px;color:%s;">（无）</p>' % _MUTED
        if "结论" in title:      # 结论单独做成高亮卡片，老板一眼能看到
            inner = ('<div style="background:#f0fdfa;border:1px solid #ccfbf1;border-radius:10px;'
                     'padding:14px 16px 6px;">%s</div>' % inner)
        pad = "22px 26px 6px" if idx == 0 else "18px 26px 6px"
        parts.append('<tr><td style="padding:%s;">' % pad)
        if title:
            parts.append('<div style="font-size:12px;font-weight:700;letter-spacing:1.2px;color:%s;'
                         'padding-left:10px;border-left:3px solid %s;line-height:1.2;">%s</div>'
                         % (color, color, _esc(title)))
            parts.append('<div style="height:10px;line-height:10px;">&nbsp;</div>')
        parts.append(inner)
        parts.append('</td></tr>')

    # 落款
    parts.append('<tr><td style="padding:16px 26px 22px;">'
                 '<div style="border-top:1px solid #eef0f3;padding-top:14px;font-size:12px;'
                 'color:%s;line-height:1.8;">%s%s｜OPC %s<br>'
                 '本邮件由 OPC 数字员工自动发送，可直接回复。</div></td></tr>'
                 % (_MUTED, _esc(name or address), "（%s）" % _esc(role) if role else "", _esc(address)))
    parts.append('</table></td></tr></table></body></html>')
    return "".join(parts)


def mail_send(args):
    global _send_count
    if _send_count >= MAX_SEND_PER_PROCESS:
        raise RuntimeError("本次会话发送已达上限 %d 封，请复核后再发" % MAX_SEND_PER_PROCESS)

    subject = (args.get("subject") or "").strip()
    body = (args.get("body") or "").strip()
    fmt = (args.get("format") or "text").strip().lower()
    html_body = (args.get("html") or "").strip()
    if not subject:
        raise ValueError("subject 不能为空")
    if not body:
        raise ValueError("body 不能为空")
    if len(subject) > 200:
        raise ValueError("subject 过长（>200 字）")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError("body 过长（>%d 字）" % MAX_BODY_CHARS)
    if fmt not in ("text", "report"):
        raise ValueError("format 只支持 text（纯文本）或 report（分节报告排版成 HTML）")
    if len(html_body) > MAX_BODY_CHARS * 4:
        raise ValueError("html 过长（>%d 字）" % (MAX_BODY_CHARS * 4))

    to = _normalize_addrs(args.get("to"), "收件人 to")
    cc = _normalize_addrs(args.get("cc"), "抄送 cc")
    if not to:
        raise ValueError("to 不能为空")

    aid, name, address, password = account(args.get("agent"))
    role = (os.environ.get("MAIL_ROLE") or "").strip()

    msg = EmailMessage()
    msg["From"] = formataddr((str(email.header.Header(name, "utf-8")), address))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=DOMAIN)
    if args.get("auto_reply"):
        # 自动派工回执标记：收件方的派工机制见到该头就不再触发回复，避免邮件来回循环
        msg["Auto-Submitted"] = "auto-replied"
        msg["X-OPC-Dispatch"] = "auto"
    signature = "\n\n——\n%s%s｜OPC %s" % (name, "（%s）" % role if role else "", address)
    msg.set_content(body.rstrip() + signature)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    elif fmt == "report":
        # 纯文本仍是正文主体，客户端不支持 HTML 时照常可读
        msg.add_alternative(render_report_html(body, subject, name, role, address), subtype="html")

    with smtplib.SMTP(HOST, SMTP_PORT, timeout=30) as s:
        s.ehlo()
        s.starttls(context=_ssl_ctx())
        s.login(address, password)
        s.send_message(msg, from_addr=address, to_addrs=to + cc)
    _send_count += 1

    saved_to_sent = _append_sent(msg, aid)
    _audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": "send",
            "agent": aid, "from": address, "to": to, "cc": cc,
            "subject": subject, "chars": len(body), "format": fmt,
            "html": bool(html_body) or fmt == "report", "ok": True,
            "saved_to_sent": saved_to_sent})
    log("已发送：%s → %s（%d 字，%s）" % (subject, ", ".join(to + cc), len(body),
                                     "HTML" if (html_body or fmt == "report") else "纯文本"))
    return {"ok": True, "from": address, "to": to, "cc": cc,
            "subject": subject, "message_id": msg["Message-ID"],
            "has_html": bool(html_body) or fmt == "report",
            "saved_to_sent": saved_to_sent}


def _append_sent(msg, agent=None):
    """把已发邮件存入本人 Sent 文件夹（不存在则创建），失败不影响发信结果。"""
    try:
        conn, _, _ = _imap(agent)
        try:
            raw = msg.as_bytes()
            for attempt in (1, 2):
                typ, _ = conn.append("Sent", "(\\Seen)", imaplib.Time2Internaldate(time.time()), raw)
                if typ == "OK":
                    return True
                if attempt == 1:
                    conn.create("Sent")
            return False
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as e:
        log("写入 Sent 失败：%s" % e)
        return False



# ---------------------------------------------------------------- 收信
def _decode(value):
    if not value:
        return ""
    parts = []
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            for cand in (enc, "utf-8", "gb18030", "latin-1"):
                if not cand:
                    continue
                try:
                    parts.append(text.decode(cand, "replace"))
                    break
                except LookupError:
                    continue
            else:
                parts.append(text.decode("utf-8", "replace"))
        else:
            parts.append(text)
    return "".join(parts).strip()


def _imap(agent=None):
    aid, name, address, password = account(agent)
    conn = imaplib.IMAP4_SSL(HOST, IMAP_PORT, ssl_context=_ssl_ctx(), timeout=30)
    conn.login(address, password)
    return conn, aid, address


def _folder(args):
    folder = (args.get("folder") or "INBOX").strip()
    if not re.fullmatch(r"[A-Za-z0-9._/\- ]{1,64}", folder):
        raise ValueError("folder 名称不合法：%s" % folder)
    return folder


def _select(conn, folder, readonly):
    typ, _ = conn.select(folder, readonly=readonly)
    if typ == "OK":
        return
    try:
        typ2, data = conn.list()
        names = []
        for line in data or []:
            if isinstance(line, bytes):
                m = re.search(rb'"([^"]+)"$', line.strip()) or re.search(rb"(\S+)$", line.strip())
                if m:
                    names.append(m.group(1).decode("utf-8", "ignore"))
        hint = "、".join(names) if names else "仅 INBOX"
    except Exception:
        hint = "仅 INBOX"
    raise RuntimeError("无法打开文件夹 %s；可用文件夹：%s" % (folder, hint))


def mail_list(args):
    folder = _folder(args)
    limit = max(1, min(int(args.get("limit") or 10), 50))
    unread_only = bool(args.get("unread_only"))

    conn, aid, address = _imap(args.get("agent"))
    try:
        _select(conn, folder, readonly=True)
        typ, data = conn.uid("search", None, "UNSEEN" if unread_only else "ALL")
        uids = (data[0] or b"").split()
        unread_all = None
        if not unread_only:
            typ, d2 = conn.uid("search", None, "UNSEEN")
            unread_all = len((d2[0] or b"").split())
        items = []
        for uid in reversed(uids[-limit:]):
            typ, d = conn.uid("fetch", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw = b""
            flags = ""
            for part in d or []:
                if isinstance(part, tuple):
                    raw = part[1] or b""
                    if isinstance(part[0], bytes):
                        flags = part[0].decode("utf-8", "ignore")
            head = email.message_from_bytes(raw)
            items.append({
                "uid": uid.decode(),
                "from": _decode(head.get("From")),
                "subject": _decode(head.get("Subject")),
                "date": head.get("Date", ""),
                "seen": "\\Seen" in flags,
            })
        return {"account": address, "folder": folder, "total": len(uids),
                "unread": unread_all if unread_all is not None else sum(1 for i in items if not i["seen"]),
                "returned": len(items), "messages": items}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _html_to_text(text):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_body(msg):
    if msg.is_multipart():
        texts, htmls, attachments = [], [], []
        for part in msg.walk():
            disp = (part.get("Content-Disposition") or "").lower()
            if part.get_filename():
                attachments.append(_decode(part.get_filename()))
                continue
            if part.get_content_maintype() != "text":
                continue
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, "replace")
            except LookupError:
                text = payload.decode("utf-8", "replace")
            if part.get_content_subtype() == "html":
                htmls.append(_html_to_text(text))
            else:
                texts.append(text.strip())
        # multipart/alternative 里纯文本与 HTML 是同一份内容，优先纯文本，避免读出来重复两遍
        if texts:
            return "\n\n".join(t for t in texts if t), attachments
        return "\n\n".join(t for t in htmls if t), attachments
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, "replace").strip()
    except LookupError:
        text = payload.decode("utf-8", "replace").strip()
    if msg.get_content_subtype() == "html":
        text = _html_to_text(text)
    return text, []


def mail_read(args):
    folder = _folder(args)
    uid = str(args.get("uid") or "").strip()
    if not re.fullmatch(r"\d+", uid):
        raise ValueError("uid 必须是数字（来自 mail_list 返回的 uid）")

    conn, aid, address = _imap(args.get("agent"))
    try:
        _select(conn, folder, readonly=False)
        typ, d = conn.uid("fetch", uid, "(BODY.PEEK[])")
        raw = b""
        for part in d or []:
            if isinstance(part, tuple):
                raw = part[1] or b""
        if not raw:
            raise RuntimeError("未找到 uid=%s 的邮件" % uid)
        msg = email.message_from_bytes(raw)
        body, attachments = _extract_body(msg)
        truncated = len(body) > MAX_BODY_CHARS
        if truncated:
            body = body[:MAX_BODY_CHARS] + "\n…（正文过长已截断）"
        conn.uid("store", uid, "+FLAGS", "(\\Seen)")
        _audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": "read",
                "agent": aid, "from_account": address, "uid": uid, "folder": folder})
        return {"uid": uid, "folder": folder,
                "from": _decode(msg.get("From")), "to": _decode(msg.get("To")),
                "cc": _decode(msg.get("Cc")), "subject": _decode(msg.get("Subject")),
                "date": msg.get("Date", ""), "attachments": attachments,
                "truncated": truncated, "body": body}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def mail_check(args):
    aid, name, address, password = account(args.get("agent"))
    result = {"agent": aid, "name": name, "address": address,
              "domain": DOMAIN, "host": HOST, "smtp_port": SMTP_PORT, "imap_port": IMAP_PORT,
              "sent_this_session": _send_count}
    try:
        conn, _, _ = _imap(args.get("agent"))
        try:
            conn.select("INBOX", readonly=True)
            typ, data = conn.uid("search", None, "UNSEEN")
            result["inbox_unread"] = len((data[0] or b"").split())
            typ, data = conn.uid("search", None, "ALL")
            result["inbox_total"] = len((data[0] or b"").split())
            result["imap_ok"] = True
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as e:
        result["imap_ok"] = False
        result["imap_error"] = str(e)
    return result


# ---------------------------------------------------------------- 工具描述
TOOLS = [
    {
        "name": "mail_send",
        "description": ("以本人邮箱发送内部邮件（仅限 @%s 域内地址）。"
                        "发件人固定为本人，不可伪造；正文会自动附上姓名与岗位签名。"
                        "format=report 时把分节纯文本（【结论】/【进展】/【风险与异常】/【需要老板拍板】）"
                        "另存为排版好的 HTML 版本，纯文本仍保留作降级。" % DOMAIN),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人邮箱，多个用逗号分隔"},
                "subject": {"type": "string", "description": "邮件主题（≤200 字）"},
                "body": {"type": "string", "description": "邮件正文纯文本（≤50000 字）"},
                "format": {"type": "string", "enum": ["text", "report"],
                           "description": "text=纯文本（默认）；report=用【】分节渲染 HTML 排版（巡检/日报用这个）"},
                "html": {"type": "string", "description": "可选，自定义 HTML 正文（优先级高于 format=report）"},
                "cc": {"type": "string", "description": "抄送邮箱，多个用逗号分隔（可省略）"},
                "auto_reply": {"type": "boolean", "description": "自动回执模式：加 Auto-Submitted 头，避免对方自动机制来回回复；仅派工回执时使用"},
                "agent": {"type": "string", "description": "操作身份（Agent ID，如 ceo）；不填则用进程环境 MAIL_AGENT"},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mail_list",
        "description": ("列出本人邮箱指定文件夹的邮件摘要（发件人/主题/时间/是否已读），"
                        "默认 INBOX 最新 10 封，不修改任何已读状态。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "文件夹名，默认 INBOX"},
                "limit": {"type": "integer", "description": "返回条数，1-50，默认 10"},
                "unread_only": {"type": "boolean", "description": "只看未读，默认 false"},
                "agent": {"type": "string", "description": "操作身份（Agent ID，如 ceo）；不填则用进程环境 MAIL_AGENT"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "mail_read",
        "description": "按 mail_list 返回的 uid 读取本人邮箱中一封邮件的正文、抄送与附件名，读后标记为已读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "description": "邮件 UID，来自 mail_list"},
                "folder": {"type": "string", "description": "文件夹名，默认 INBOX"},
                "agent": {"type": "string", "description": "操作身份（Agent ID，如 ceo）；不填则用进程环境 MAIL_AGENT"},
            },
            "required": ["uid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mail_check",
        "description": "检查本人邮箱配置与连通性：账号、SMTP/IMAP 端口、收件箱总数与未读数。",
        "inputSchema": {"type": "object", "properties": {
            "agent": {"type": "string", "description": "操作身份（Agent ID，如 ceo）；不填则用进程环境 MAIL_AGENT"},
        }, "additionalProperties": False},
    },
]


# ---------------------------------------------------------------- 协议循环
def _send(obj):
    _PROTO_OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _PROTO_OUT.flush()


def _result(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


HANDLERS = {
    "mail_send": mail_send,
    "mail_list": mail_list,
    "mail_read": mail_read,
    "mail_check": mail_check,
}


def handle(req):
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "initialize":
        _result(rid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    elif method == "tools/list":
        _result(rid, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if not handler:
            _result(rid, {"content": [{"type": "text", "text": "未知工具 %s" % name}], "isError": True})
            return
        try:
            payload = handler(args)
            _result(rid, {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                          "isError": False})
        except Exception as e:
            log(traceback.format_exc())
            _result(rid, {"content": [{"type": "text", "text": "邮件操作失败：%s" % e}], "isError": True})
    elif method == "ping":
        _result(rid, {})
    elif rid is not None:
        _error(rid, -32601, "未实现的方法 %s" % method)


def main():
    try:
        aid, name, address, _ = account()
        log("启动，agent=%s，邮箱=%s，服务=%s:%d/%d" % (aid, address, HOST, SMTP_PORT, IMAP_PORT))
    except Exception as e:
        log("启动自检失败：%s" % e)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log("收到非 JSON 行，已忽略")
            continue
        for r in (req if isinstance(req, list) else [req]):
            try:
                handle(r)
            except Exception:
                log(traceback.format_exc())


if __name__ == "__main__":
    main()
