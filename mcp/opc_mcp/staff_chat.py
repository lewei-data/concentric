"""员工对话通道（staff_chat）：老板经 MCP @某员工下指令，工作台真实员工回复。

链路：MCP job(staff_chat) → 工作台 POST /api/commands（@员工 全链路：delegation 事件 +
hermes 独立 profile 真员工回复）→ 指令与回复写 mcp 库 messages（role=user/assistant）→
工作台 ingest.pull_mcp 摄取进消息中心线程 mcp:<phone>，小秘书面板可见。

鉴权：仅 config.DETAIL_PHONES（默认老板手机号）可用；工作台会话用登录凭据自动登录，
token 只在进程内存缓存 25 分钟，不落盘、不打日志。
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import auth, config

WB_URL = config.WORKBENCH_URL
SESSION_TTL = 25 * 60
COMMAND_TIMEOUT = float(os.environ.get("OPC_STAFF_CHAT_TIMEOUT", "150"))

_lock = threading.Lock()
_sess = {"cookie": None, "exp": 0.0}


class StaffChatError(RuntimeError):
    """可归因错误：status/code 沿用 MCP 错误码口径。"""

    def __init__(self, message, status="upstream_error", code="upstream_unavailable"):
        super().__init__(message)
        self.status = status
        self.code = code


def _owner_phone():
    return sorted(config.DETAIL_PHONES)[0] if config.DETAIL_PHONES else ""


def _login():
    """用登录凭据里的老板账号向工作台登录，返回会话 cookie（内存缓存）。"""
    phone = _owner_phone()
    row = None
    for r in auth._cred_rows(phone):
        if auth.normalize_phone(r.get("手机号")) == phone:
            row = r
            break
    pwd = (row or {}).get("密码") or ""
    if not phone or not pwd:
        raise StaffChatError("登录凭据里没有老板账号，无法代连工作台", code="forbidden")
    body = json.dumps({"account": phone, "password": pwd}).encode("utf-8")
    req = urllib.request.Request(WB_URL + "/api/login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        raise StaffChatError("工作台登录失败：%s" % e)
    if resp.status != 200:
        raise StaffChatError("工作台登录失败：HTTP %s" % resp.status)
    cookie = (resp.headers.get("Set-Cookie") or "").split(";")[0].strip()
    if not cookie:
        raise StaffChatError("工作台登录成功但未返回会话")
    return cookie


def _session():
    with _lock:
        if _sess["cookie"] and time.time() < _sess["exp"]:
            return _sess["cookie"]
        cookie = _login()
        _sess["cookie"] = cookie
        _sess["exp"] = time.time() + SESSION_TTL
        return cookie


def _drop_session():
    with _lock:
        _sess["cookie"] = None
        _sess["exp"] = 0.0


def _request(path, payload=None, retried=False):
    """带会话调工作台；401 自动重登一次。返回 (data, status)。"""
    op = urllib.request.Request(WB_URL + path, method="POST" if payload is not None else "GET",
                                headers={"Content-Type": "application/json",
                                         "Cookie": _session()},
                                data=json.dumps(payload).encode("utf-8") if payload is not None else None)
    try:
        resp = urllib.request.urlopen(op, timeout=COMMAND_TIMEOUT)
        return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as e:
        if e.code == 401 and not retried:
            _drop_session()
            return _request(path, payload, retried=True)
        raw = e.read().decode("utf-8", "replace")[:300]
        try:
            data = json.loads(raw)
        except Exception:
            data = {"error": raw or ("HTTP %s" % e.code)}
        return data, e.code
    except Exception as e:
        raise StaffChatError("工作台不可达：%s" % e)


def agents():
    """员工清单（id/name/role），供客户端选人；来自工作台组织配置。"""
    data, status = _request("/api/org")
    if status != 200 or not isinstance(data, dict):
        raise StaffChatError("取员工清单失败：HTTP %s" % status)
    out = []
    for a in data.get("agents") or []:
        if isinstance(a, dict) and a.get("id") and a.get("id") != "boss":
            out.append({"id": a["id"], "name": a.get("name") or "",
                        "role": a.get("role") or ""})
    return out


def command(agent, message):
    """@员工 下指令，返回工作台 ask_agent 结果（真实员工回复）。"""
    agent = str(agent or "").strip()
    message = str(message or "").strip()
    if not agent:
        raise StaffChatError("请指定员工：agent 填员工 id 或姓名（list_agents=true 可查清单）",
                             status="invalid_argument", code="invalid_argument")
    if not message:
        message = "请汇报当前进展与卡点。"
    data, status = _request("/api/commands", {"text": "@%s %s" % (agent, message)})
    if status == 404:
        raise StaffChatError(str((data or {}).get("error") or "没有找到员工 %s" % agent),
                             status="not_found", code="not_found")
    if status != 200 or not isinstance(data, dict) or not data.get("ok"):
        raise StaffChatError(str((data or {}).get("error") or ("工作台拒绝：HTTP %s" % status)),
                             status="invalid_argument", code="invalid_argument")
    return data
