"""内部邮件：以员工邮箱收发（SMTP），仅限内部域。"""
import json
import os
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from opc_paths import MAIL_AUDIT, MAIL_CRED_FILE, MAIL_DOMAIN, MAIL_HOST, MAIL_SMTP_PORT
from utils import iso_now, uid, _to_int
def load_mail_accounts():
    """解析 .邮箱凭据.md：| 邮箱 | 姓名 | 密码 | → {邮箱: (姓名, 密码)}"""
    accounts = {}
    try:
        with open(MAIL_CRED_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("|"):
                    continue
                cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
                if len(cells) < 3 or "@" not in cells[0]:
                    continue
                accounts[cells[0].lower()] = (cells[1], cells[2])
    except OSError as e:
        print(f"[mail] 读取邮箱凭据失败：{e}", flush=True)
    return accounts

def send_internal_mail(agent_id, to, subject, body, cc=None):
    """以员工本人邮箱发内部邮件（仅限 @mail.conring.cn）；失败直接抛异常由调用方兜底。"""
    def _addr(x):
        x = str(x).strip()
        return x if "@" in x else f"{x}@{MAIL_DOMAIN}"

    to = [_addr(x) for x in ([to] if isinstance(to, str) else list(to or []))]
    cc = [_addr(x) for x in (cc or [])]
    for addr in to + cc:
        if not addr.lower().endswith("@" + MAIL_DOMAIN):
            raise ValueError(f"仅允许内部域发信：{addr}")
    if not to:
        raise ValueError("收件人不能为空")
    accounts = load_mail_accounts()
    address = f"{agent_id}@{MAIL_DOMAIN}"
    if address.lower() not in accounts:
        raise RuntimeError(f"凭据文件中没有 {address}")
    name, password = accounts[address.lower()]
    msg = EmailMessage()
    msg["From"] = formataddr((name, address))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=MAIL_DOMAIN)
    msg.set_content(body.strip())
    with smtplib.SMTP(MAIL_HOST, MAIL_SMTP_PORT, timeout=30) as s:
        s.ehlo()
        s.starttls(context=ssl._create_unverified_context())   # 内部邮件服务自签证书
        s.login(address, password)
        s.send_message(msg, from_addr=address, to_addrs=to + cc)
    try:
        os.makedirs(os.path.dirname(MAIL_AUDIT), exist_ok=True)
        with open(MAIL_AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": iso_now(), "action": "send", "agent": agent_id,
                                "from": address, "to": to, "cc": cc, "subject": subject,
                                "chars": len(body), "ok": True, "source": "opc"},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass
    return {"ok": True, "from": address, "to": to, "cc": cc, "subject": subject}
