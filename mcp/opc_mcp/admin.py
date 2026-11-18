"""个人 API key 管理：按手机号发 key、列出、改元数据、撤销。

  python3 -m opc_mcp.admin add --phone 18911789286 --name 蒿色莫 --label 豆包工作 --source doubao --days 90
  python3 -m opc_mcp.admin list [--phone 18911789286]
  python3 -m opc_mcp.admin update --id 3 --label 新名称 --enabled 0 --days 30
  python3 -m opc_mcp.admin revoke --id 3        # 或 --key opc_xxx / --phone 189...

新增 key 是追加，不撤销旧 key；每把 key 可独立开关/改名/设置有效期。
key 只在 add 时明文出现一次；库里只存 sha256。--out 会以 600 落盘，避免进聊天记录。
"""

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

from . import auth, config
from .store import Store


def _store():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Store(config.DB_PATH)


def _upsert(store, phone, name, role, email):
    row = auth.lookup(phone) or {}
    name = name or row.get("用户名") or "用户"
    role = role or row.get("角色") or "member"
    email = email or row.get("邮箱") or ""
    return auth._upsert_user(store, phone, name, role, email, "personal-key")


def cmd_add(args):
    phone = auth.normalize_phone(args.phone)
    if len(phone) != 11:
        raise SystemExit("手机号需为 11 位：%s" % args.phone)
    store = _store()
    _upsert(store, phone, args.name, args.role, args.email)
    key = "opc_" + secrets.token_urlsafe(32)
    expires_at = time.time() + int(args.days) * 86400 if getattr(args, "days", None) else None
    row = store.add_api_key(phone, auth.hash_key(key), args.label,
                            source=getattr(args, "source", None), expires_at=expires_at)
    out = {"phone": phone, "key_id": row["id"], "label": args.label,
           "source": row["source"],
           "expires_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at)) if expires_at else None,
           "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["created_at"]))}
    if args.out:
        path = Path(args.out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(key + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        out["key_file"] = str(path)
        out["note"] = "key 已写入 600 文件，请勿提交到代码库；泄漏后用 revoke 撤销"
    else:
        out["key"] = key
        out["note"] = "key 只显示这一次；建议改用 --out 写 600 文件"
    print(json.dumps(out, ensure_ascii=False))


def cmd_list(args):
    store = _store()
    rows = store.api_keys(auth.normalize_phone(args.phone) if args.phone else None)
    print(json.dumps({"count": len(rows), "items": rows}, ensure_ascii=False))


def cmd_update(args):
    if not args.id:
        raise SystemExit("update 需要 --id")
    store = _store()
    row = store.api_key(args.id)
    if not row:
        raise SystemExit("key id 不存在：%s" % args.id)
    expires_at = None
    if getattr(args, "days", None):
        expires_at = time.time() + int(args.days) * 86400
    updated = store.update_api_key(
        args.id, phone=auth.normalize_phone(args.phone) if args.phone else None,
        label=args.label, source=args.source,
        enabled=(None if args.enabled is None else int(args.enabled)),
        expires_at=expires_at, clear_expires=getattr(args, "forever", False))
    print(json.dumps({"updated": bool(updated), "item": updated}, ensure_ascii=False))


def cmd_revoke(args):
    if not (args.id or args.key or args.phone):
        raise SystemExit("revoke 需要 --id / --key / --phone 之一")
    store = _store()
    n = store.revoke_api_key(key_id=args.id,
                             key_hash=auth.hash_key(args.key) if args.key else None,
                             phone=auth.normalize_phone(args.phone) if args.phone else None)
    print(json.dumps({"revoked": n}, ensure_ascii=False))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="opc_mcp.admin", description="OPC MCP 个人 API key 管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="按手机号发放个人 key")
    p.add_argument("--phone", required=True)
    p.add_argument("--name")
    p.add_argument("--role")
    p.add_argument("--email")
    p.add_argument("--label")
    p.add_argument("--source", help="MCP 客户端来源，如 workbuddy / doubao / cli")
    p.add_argument("--days", type=int, help="有效期天数；不填表示长期有效")
    p.add_argument("--out", help="把 key 写到 600 权限文件，避免出现在终端/聊天记录")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("list", help="列出 key（不含哈希）")
    p.add_argument("--phone")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("update", help="改 key 名称/来源/开关/有效期")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--phone")
    p.add_argument("--label")
    p.add_argument("--source")
    p.add_argument("--enabled", type=int, choices=[0, 1], help="1=启用 0=停用")
    p.add_argument("--days", type=int, help="从现在起 N 天有效")
    p.add_argument("--forever", action="store_true", help="清除有效期（长期有效）")
    p.set_defaults(fn=cmd_update)

    p = sub.add_parser("revoke", help="撤销 key")
    p.add_argument("--id", type=int)
    p.add_argument("--key")
    p.add_argument("--phone")
    p.set_defaults(fn=cmd_revoke)

    args = ap.parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
