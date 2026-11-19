#!/usr/bin/env python3
"""把 stdio 的 MCP server 桥接成 Streamable HTTP 服务，监听 0.0.0.0。

用法：
  bridge.py --name mail --port 9100 --cwd /opt/sefs/opc-workbench/mcp-mail \
            --identity-param agent -- /opt/sefs/opc-workbench/mcp-mail/run.sh

身份：工具调用时读取请求头 X-Mail-Agent，并以 --identity-param 指定的参数名
注入到工具入参（mail 的邮箱名 = Agent ID）。请求头缺失时退回 --default-agent。
对不支持自定义 Header 的客户端，可用路径式路由 /{agent}/mcp 表达同一身份。
"""

import argparse
import hashlib
import hmac
import mimetypes
import os
import re
import sys
import asyncio
import json
import time
import urllib.request
from contextlib import asynccontextmanager

import anyio
import uvicorn
import websockets
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from mcp.client.session import ClientSession, NotificationBinding
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel import Server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCNotification

# 实时通话 / 自我介绍用到 HLS：系统 mime 表把 .ts 认成别的类型，Android 端会拒播
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")
mimetypes.add_type("video/mp4", ".mp4")


class MediaStaticFiles(StaticFiles):
    """静态素材：播放列表/分片禁止缓存，其余（图片、MP4）保持 Range + ETag 默认行为。"""

    async def get_response(self, path, scope):
        # 实时通话的员工侧电平文件每 200ms 整体替换一次。StaticFiles 先 stat 取长度、
        # 后按路径开文件，正好落在两次替换之间时会把新文件的长度配上旧文件的内容，
        # 客户端读到半截 JSON（IncompleteRead）。这里一次读全量再返回，保证快照一致。
        if path.endswith("level.json"):
            try:
                with open(os.path.join(self.directory, path), "rb") as f:
                    body = f.read()
            except OSError:
                raise HTTPException(status_code=404)
            return Response(body, media_type="application/json",
                            headers={"Cache-Control": "no-store"})
        resp = await super().get_response(path, scope)
        if path.endswith((".m3u8", ".ts")):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_HEADER = "x-mail-agent"
AGENT_PATH_RE = re.compile(r"^/([A-Za-z0-9_.\-]{1,64})/mcp$")


class JobEvent(BaseModel):
    """stdio 服务端发出的作业终态通知（notifications/opc/job）。

    phone_fp 只是路由用的账号指纹，绝不能出现在对外 SSE 帧里。
    """
    event_id: str = ""                      # 去重键：与事件日志 tail 共用，必须保留
    job_id: str = ""
    kind: str = ""
    status: str = ""
    phone_fp: str = ""                      # 内部路由键（sha256 指纹），不对外
    error: str | None = None
    at: float | None = None


class EventsServer(Server):
    """在 initialize.capabilities.experimental 里声明作业事件通道，
    让只订阅标准 GET <endpoint> 的规范客户端也能发现 /mcp/events 与通知方法。
    纯 SSE 客户端用 /mcp/events；标准 MCP 客户端直接在 GET /mcp 收 JSON-RPC 通知。
    """

    def create_initialization_options(self, *a, **kw):
        opts = super().create_initialization_options(*a, **kw)
        exp = dict(opts.capabilities.experimental or {})
        exp["events"] = {
            "path": "/mcp/events",
            "event": "job",
            "notification": "notifications/opc/job",
            "auth_header": IDENTITY_HEADER,
            "replay_query": "since",
            "fields": ["job_id", "kind", "status", "error", "at", "event_id"],
        }
        opts.capabilities.experimental = exp
        return opts


class PathIdentityMiddleware:
    """把 /{agent}/mcp 重写成 /mcp，并补上 X-Mail-Agent，兼容不能设 Header 的客户端。"""

    def __init__(self, app, enabled: bool):
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if self.enabled and scope.get("type") == "http":
            match = AGENT_PATH_RE.match(scope.get("path", ""))
            if match:
                headers = [(k, v) for k, v in scope.get("headers", [])
                           if k.lower() != IDENTITY_HEADER.encode()]
                headers.append((IDENTITY_HEADER.encode(), match.group(1).encode()))
                scope = dict(scope)
                scope["headers"] = headers
                scope["path"] = "/mcp"
                scope["raw_path"] = b"/mcp"
        await self.app(scope, receive, send)


class SessionIdentityMiddleware:
    """记录 mcp-session-id → client_key，供标准 GET /mcp SSE 流按账号路由作业事件。

    必须放在 PathIdentityMiddleware 内层：后者先把 /{agent}/mcp 的头像写进
    X-Mail-Agent，这里才读得到。只保留有 session id 的请求（initialize 的响应头
    才会有 session id；之后的 POST/GET 都带它）。
    """

    def __init__(self, app, mapping: dict):
        self.app = app
        self.mapping = mapping

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            hdrs = {k.lower(): v for k, v in scope.get("headers", [])}
            key = hdrs.get(IDENTITY_HEADER.encode(), b"").decode("latin-1").strip()
            sid = hdrs.get(b"mcp-session-id", b"").decode("latin-1").strip()
            if key and sid:
                self.mapping[sid] = (key, time.time())
                if len(self.mapping) > 2000:                  # 惰性清理过期条目
                    now = time.time()
                    for k, v in list(self.mapping.items()):
                        if now - v[1] > 3600:
                            self.mapping.pop(k, None)
        await self.app(scope, receive, send)


REALTIME_WS_UPSTREAM = os.environ.get("REALTIME_WS_UPSTREAM", "ws://127.0.0.1:9121/channel/")
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


async def realtime_proxy(websocket):
    """把 dispatch 的实时通道（9121）透明代理到 MCP 域名下：/ws/<channel_id>。

    App 把 dispatch_task 返回的 ws://<内网IP>:9121/channel/<id> 换成本服务的
    /ws/<id>，就能复用现有 HTTPS 域名和反代，不必额外对外开 9121。
    """
    channel_id = websocket.path_params.get("channel_id", "")
    await websocket.accept()
    if not _CHANNEL_RE.match(channel_id):
        await websocket.send_text(json.dumps({"type": "error", "message": "channel_id 非法"}))
        await websocket.close(code=4400)
        return
    try:
        upstream = await websockets.connect(
            REALTIME_WS_UPSTREAM + channel_id, open_timeout=10,
            ping_interval=20, ping_timeout=10, max_size=4 * 1024 * 1024)
    except Exception as e:                                    # noqa: BLE001
        await websocket.send_text(json.dumps({
            "type": "error", "message": "实时通道不可用：%s" % str(e)[:200]}))
        await websocket.close(code=1011)
        return

    async def down():
        try:
            async for msg in upstream:
                if isinstance(msg, bytes):
                    await websocket.send_bytes(msg)
                else:
                    await websocket.send_text(msg)
        except Exception:                                     # noqa: BLE001
            pass

    async def up():
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except Exception:                                     # noqa: BLE001
            pass

    tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await upstream.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass


def build_app(name: str, command: str, args: list[str], cwd: str | None, host: str,
              default_agent: str | None = None, identity_param: str | None = None,
              static_dirs: list[str] | None = None, static_path: str = "/assets",
              identity_override: bool = False):
    errlog = open(os.path.join(HERE, f"{name}.stderr.log"), "ab", buffering=0)
    # 作业事件枢纽：stdio 通知 + 跨进程事件日志 → 本账号 SSE 订阅者 + 标准 GET /mcp 流 + Webhook
    subscribers: dict[str, set] = {}
    subs_lock = anyio.Lock()
    session_ref = {"session": None}
    session_keys: dict[str, tuple[str, float]] = {}  # mcp-session-id → (client_key, 最近活跃)
    events_enabled = (identity_param == "client_key")  # 仅 OPC 桥有作业事件/上游 opc_whoami
    fp_cache: dict[str, tuple[str, float]] = {}   # key 哈希 → 账号指纹（不存手机号）
    seen_events: dict[str, float] = {}
    events_offset = [0]
    events_file = os.environ.get(
        "OPC_MCP_EVENTS_FILE", "/opt/sefs/opc-workbench/mcp/data/job_events.jsonl")
    sse_max = max(1, int(os.environ.get("OPC_MCP_SSE_MAX", "100")))
    webhook_urls = [u.strip() for u in os.environ.get("OPC_MCP_JOB_WEBHOOK_URLS", "").split(",")
                    if u.strip()]
    webhook_secret = os.environ.get("OPC_MCP_WEBHOOK_SECRET", "").encode()
    webhook_queue = asyncio.Queue()

    def post_webhook(url, body, signature):
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        if signature:
            req.add_header("X-OPC-Event-Signature", signature)
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    async def webhook_worker():
        """失败按 1s/5s/30s 退避重试三次；耗尽后只落 stderr，不丢后续事件。"""
        while True:
            payload = await webhook_queue.get()
            body = json.dumps(payload, ensure_ascii=False).encode()
            signature = ("sha256=" + hmac.new(webhook_secret, body, hashlib.sha256).hexdigest()
                         if webhook_secret else "")
            for url in webhook_urls:
                for attempt, delay in enumerate((0, 1, 5, 30)):
                    if delay:
                        await asyncio.sleep(delay)
                    try:
                        await asyncio.to_thread(post_webhook, url, body, signature)
                        break
                    except Exception as e:                     # noqa: BLE001
                        if attempt == 3:
                            print("[mcp-bridge] webhook 失败 %s: %s | %s"
                                  % (url, str(e)[:160], payload.get("job_id", "")),
                                  file=sys.stderr, flush=True)
            webhook_queue.task_done()

    def mark_seen(payload):
        """event_id 去重；stdio 通知与事件日志 tail 不会双推。返回 True=已见过。"""
        eid = payload.get("event_id")
        if not eid:
            return False
        now = time.time()
        if eid in seen_events:
            return True
        seen_events[eid] = now
        if len(seen_events) > 2000:
            for k, v in list(seen_events.items()):
                if now - v > 300:
                    seen_events.pop(k, None)
        return False

    _EVENT_FIELDS = ("job_id", "kind", "status", "error", "at", "event_id")

    def public_event(payload, replay=False):
        """出站白名单：绝不带手机号/指纹；补发帧显式标 replay=true。"""
        out = {k: payload[k] for k in _EVENT_FIELDS if k in payload}
        if replay:
            out["replay"] = True
        return out

    def route_fp(payload):
        """内部路由键；兼容老事件日志里带 phone 的记录（只在内存算指纹，不外发）。"""
        fp = payload.get("phone_fp")
        if fp:
            return fp
        phone = payload.get("phone")
        if not phone:
            return ""
        digits = re.sub(r"\D", "", str(phone))
        if len(digits) == 13 and digits.startswith("86"):
            digits = digits[2:]
        return hashlib.sha256(("opc:" + digits).encode()).hexdigest()[:12]

    def frame(payload):
        return "event: job\ndata: %s\n\n" % json.dumps(payload, ensure_ascii=False)

    async def publish(payload):
        if mark_seen(payload):
            return
        fp = route_fp(payload)
        if not fp:
            return
        public = public_event(payload)                        # 一次净化，SSE 与 Webhook 共用
        async with subs_lock:
            targets = list(subscribers.get(fp, ()))
        for send in targets:
            try:
                send.send_nowait(public)
            except Exception:                                 # noqa: BLE001
                pass
        if webhook_urls:
            webhook_queue.put_nowait(public)
        if events_enabled:
            asyncio.create_task(broadcast_get_stream(public, fp))  # 标准 GET /mcp 流

    async def on_job_event(params: JobEvent):
        await publish(params.model_dump())

    def read_new_events():
        try:
            with open(events_file, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(events_offset[0] if events_offset[0] <= size else 0)
                data = f.read()
                events_offset[0] = f.tell()
        except OSError:
            return []
        out = []
        for line in data.decode("utf-8", "replace").splitlines():
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    out.append(rec)
            except json.JSONDecodeError:
                continue
        return out

    def read_events_since(cutoff, fp):
        try:
            with open(events_file, "r", encoding="utf-8") as f:
                lines = f.readlines()[-1000:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                rec = json.loads(line)
                if (isinstance(rec, dict) and float(rec.get("at") or 0) >= cutoff
                        and route_fp(rec) == fp):
                    out.append(public_event(rec, replay=True))  # 补发帧显式标 replay
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return out

    def init_events_offset():
        """桥启动时从文件末尾开始 tail；历史事件只通过 ?since 补发，不当实时帧重推。"""
        try:
            with open(events_file, "rb") as f:
                events_offset[0] = f.seek(0, os.SEEK_END)
        except OSError:
            events_offset[0] = 0

    async def events_poller():
        """每 1 秒 tail 事件日志，覆盖 9105 上传服务等不走 stdio 通知的作业。"""
        while True:
            await asyncio.sleep(1)
            for rec in read_new_events():
                await publish(rec)

    # 把父进程（systemd 单元里的 Environment=…）整份传给 stdio 子进程；
    # 只传 fallback_env 会把 ASR_* 这类业务配置全部丢掉。
    child_env = dict(os.environ)

    @asynccontextmanager
    async def lifespan(_server):
        env = dict(child_env)
        if default_agent:
            env.setdefault("MAIL_AGENT", default_agent)
        params = StdioServerParameters(command=command, args=args, cwd=cwd, env=env)
        async with stdio_client(params, errlog=errlog) as (read, write):
            bindings = [NotificationBinding(method="notifications/opc/job",
                                            params_type=JobEvent,
                                            handler=on_job_event)]
            async with ClientSession(read, write, notification_bindings=bindings) as session:
                await session.initialize()
                session_ref["session"] = session
                init_events_offset()
                poll_task = asyncio.create_task(events_poller())
                hook_task = asyncio.create_task(webhook_worker()) if webhook_urls else None
                try:
                    yield {"session": session}
                finally:
                    session_ref["session"] = None
                    poll_task.cancel()
                    if hook_task:
                        hook_task.cancel()

    def identity_of(ctx) -> str:
        request = getattr(ctx, "request", None)
        if request is not None:
            value = request.headers.get(IDENTITY_HEADER, "").strip()
            if value:
                return value
        return default_agent or ""

    async def on_list_tools(ctx, params):
        return await ctx.lifespan_context["session"].list_tools(params=params)

    async def on_call_tool(ctx, params):
        arguments = dict(params.arguments or {})
        identity = identity_of(ctx)
        if events_enabled and identity and not fp_of_key(identity):
            # 预热账号指纹：标准 GET /mcp 流没有 X-Mail-Agent 的 SSE 订阅流程，
            # 用工具调用顺带把 key→指纹写进缓存，事件广播才能按账号路由。
            try:
                await resolve_fp(identity)
            except Exception:                                 # noqa: BLE001
                pass
        if identity_param and identity:
            # 默认兼容旧行为；external 服务开 override，杜绝模型自带参数覆盖网关注入的身份
            if identity_override:
                arguments[identity_param] = identity
            else:
                arguments.setdefault(identity_param, identity)
        return await ctx.lifespan_context["session"].call_tool(params.name, arguments)

    server = (EventsServer if events_enabled else Server)(
        name,
        version="1.0.0",
        lifespan=lifespan,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )

    async def healthz(_request):
        return JSONResponse({
            "ok": True,
            "bridge": name,
            "upstream": command,
            "identity_header": IDENTITY_HEADER,
            "identity_param": identity_param,
            "identity_override": identity_override,
            "default_agent": default_agent,
            "path_template": "/{agent}/mcp" if identity_param else None,
            "events_path": "/mcp/events" if events_enabled else None,
            "events_in_get_mcp": events_enabled,   # 标准 GET /mcp 是否广播通知
            "job_notification": "notifications/opc/job" if events_enabled else None,
            "events_file": os.path.basename(events_file) if events_enabled else None,
            "sse_limit": sse_max,
            "job_webhook": bool(webhook_urls),
        })

    async def resolve_fp(key):
        """SSE 用个人 key 换账号指纹（上游 opc_whoami），结果缓存 5 分钟；bridge 不存手机号。"""
        ck = hashlib.sha256(key.encode()).hexdigest()
        cached = fp_cache.get(ck)
        if cached and time.time() - cached[1] < 300:
            return cached[0]
        if cached:
            fp_cache.pop(ck, None)                            # 过期项及时清理
        session = session_ref.get("session")
        if session is None:
            raise RuntimeError("upstream unavailable")
        res = await session.call_tool("opc_whoami", {"client_key": key})
        text = res.content[0].text if res.content else ""
        who = json.loads(text)
        fp = who.get("phone_fp") or ""
        if not fp:                                            # 老服务端兜底：本地算指纹，不留手机号
            digits = re.sub(r"\D", "", str(who.get("phone") or ""))
            if len(digits) == 13 and digits.startswith("86"):
                digits = digits[2:]
            fp = hashlib.sha256(("opc:" + digits).encode()).hexdigest()[:12]
        fp_cache[ck] = (fp, time.time())
        return fp

    def fp_of_key(key):
        """只查缓存（不触发上游调用，避免在通知处理器里重入 ClientSession）。"""
        rec = fp_cache.get(hashlib.sha256(key.encode()).hexdigest())
        if rec and time.time() - rec[1] < 300:
            return rec[0]
        return None

    async def broadcast_get_stream(public, fp):
        """把净化后的事件以 notifications/opc/job 写进各会话的写流。

        Streamable HTTP 的 message_router 会把无 request id 的服务端通知投递到
        该会话的 GET /mcp SSE 流；只投给账号指纹匹配的会话，避免跨账号泄露。
        """
        mgr = getattr(server, "_session_manager", None)
        instances = getattr(mgr, "_server_instances", {}) if mgr else {}
        if not instances:
            return
        now = time.time()
        for sid, transport in list(instances.items()):
            rec = session_keys.get(sid)
            if not rec or now - rec[1] > 3600:                 # 会话身份超过 1 小时视为过期
                continue
            if fp_of_key(rec[0]) != fp:                         # 未预热/不匹配 → 跳过
                continue
            ws = getattr(transport, "_write_stream", None)
            if ws is None:
                continue
            try:
                msg = SessionMessage(JSONRPCNotification(
                    jsonrpc="2.0", method="notifications/opc/job", params=dict(public)))
                await asyncio.wait_for(ws.send(msg), timeout=1)
            except Exception:                                   # noqa: BLE001
                pass

    async def job_events(request):
        """GET /mcp/events：按 X-Mail-Agent 过滤的作业完成事件流（fetch SSE，非 EventSource）。

        EventSource 不能设自定义头；请用 fetch + ReadableStream 解析 `event: job` 帧。
        事件帧只有 {"job_id","kind","status","error","at","event_id"}，不含手机号/指纹；
        账号路由在服务端用 sha256 指纹完成。断线补发的帧额外带 `"replay": true`。
        重连传 ?since=<上次收到的最大 at>（Unix 秒）即补发；客户端按 event_id/job_id
        去重（同 job 的补发帧与实时帧业务上等同）。超出 1000 条上限时用 job_get 兜底。
        """
        key = request.headers.get(IDENTITY_HEADER, "").strip()
        if not key:
            return JSONResponse({"error": "missing " + IDENTITY_HEADER}, status_code=401)
        try:
            fp = await resolve_fp(key)
        except Exception:                                     # noqa: BLE001
            return JSONResponse({"error": "invalid identity"}, status_code=401)
        if not fp:
            return JSONResponse({"error": "invalid identity"}, status_code=401)
        async with subs_lock:
            total = sum(len(v) for v in subscribers.values())
            if total >= sse_max:
                return JSONResponse({"error": "too many SSE subscribers",
                                     "limit": sse_max}, status_code=429)
            send, recv = anyio.create_memory_object_stream(64)
            subscribers.setdefault(fp, set()).add(send)
        try:
            since = float(request.query_params.get("since") or 0)
        except (TypeError, ValueError):
            since = 0

        async def stream():
            try:
                yield ": connected\n\n"
                if since > 0:                                  # 断线补发：只发本账号的终态事件
                    for rec in read_events_since(since, fp):
                        if not mark_seen(rec):
                            yield frame(rec)
                while True:
                    with anyio.move_on_after(20):
                        try:
                            event = await recv.receive()
                        except anyio.EndOfStream:
                            break
                    if event is None:                          # 20 秒心跳，防代理断链
                        yield ": ping\n\n"
                        continue
                    yield frame(event)
            finally:
                async with subs_lock:
                    subs = subscribers.get(fp)
                    if subs is not None:
                        subs.discard(send)
                        if not subs:
                            subscribers.pop(fp, None)
                await send.aclose()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    routes = [Route("/healthz", healthz),
              Route("/mcp/events", job_events, methods=["GET"]),
              WebSocketRoute("/ws/{channel_id}", realtime_proxy)]
    # 可选静态素材：把本地目录挂到 HTTP 路径下，供 nginx 反代后的 /conring/<svc>/assets/* 直接取图/视频
    for d in static_dirs or []:
        if os.path.isdir(d):
            routes.append(Mount(static_path, MediaStaticFiles(directory=d), name="assets"))
            print(f"[mcp-bridge:{name}] static {static_path} -> {d}", file=sys.stderr, flush=True)
        else:
            print(f"[mcp-bridge:{name}] static dir missing: {d}", file=sys.stderr, flush=True)

    app = server.streamable_http_app(
        host=host,
        json_response=True,
        custom_starlette_routes=routes,
    )
    if events_enabled:
        # PathIdentityMiddleware 在 main() 里最后 add → 最外层先运行并注入 X-Mail-Agent，
        # 这里的中间件作为内层才能读到重写后的身份；只为 OPC 桥记录 session 身份。
        app.add_middleware(SessionIdentityMiddleware, mapping=session_keys)
    return app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--default-agent", default=None, help="请求头缺失时的兜底身份")
    ap.add_argument("--identity-param", default=None, help="把请求头身份注入到该工具参数名")
    ap.add_argument("--identity-override", action="store_true",
                    help="强制覆盖同名工具参数（外网身份注入必须开启）")
    ap.add_argument("--static-dir", action="append", default=[],
                    help="可选：把该目录挂成静态资源（可多次，取第一个存在的）")
    ap.add_argument("--static-path", default="/assets", help="静态资源 URL 前缀，默认 /assets")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    ns = ap.parse_args()

    cmd = ns.command[1:] if ns.command[:1] == ["--"] else ns.command
    if not cmd:
        ap.error("缺少上游 stdio 命令")

    print(f"[mcp-bridge:{ns.name}] {ns.host}:{ns.port} -> {' '.join(cmd)}",
          file=sys.stderr, flush=True)
    app = build_app(ns.name, cmd[0], cmd[1:], ns.cwd, ns.host, ns.default_agent, ns.identity_param,
                    ns.static_dir, ns.static_path, ns.identity_override)
    app.add_middleware(PathIdentityMiddleware, enabled=bool(ns.identity_param))
    uvicorn.run(app, host=ns.host, port=ns.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
