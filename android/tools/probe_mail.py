"""探测 mail/ceo 服务的 mail_check / mail_list 返回结构（只读，不改已读状态）。"""
import json
import probe_voice_flow as p

MAIL = p.BASE + "/conring/mail/ceo/mcp"


def main():
    sid = p.session(MAIL)
    for name, args in (("mail_check", {}), ("mail_list", {"folder": "INBOX", "limit": 3})):
        try:
            r = p.call(MAIL, sid, name, args, timeout=30)
            print("\n====", name, "====")
            for b in r.get("content", []):
                if b.get("type") == "text":
                    t = b.get("text", "")
                    print(t[:1500])
                    try:
                        j = json.loads(t)
                        print("KEYS:", list(j.keys()) if isinstance(j, dict) else type(j).__name__)
                    except Exception:
                        pass
        except Exception as e:
            print(name, "error:", type(e).__name__, str(e)[:200])


if __name__ == "__main__":
    main()
