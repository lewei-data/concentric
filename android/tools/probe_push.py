"""探测同心圆 MCP 端点的「服务端推送」能力：tools/list + SSE GET 长连接。

用法：python tools/probe_push.py
只读探测，不写服务端数据。
"""
import json
import time
import urllib.request
import urllib.error

BASE = "https://ai.boraydata.com"
PATHS = ["/conring/asr/mcp", "/conring/tts/mcp", "/conring/mail/ceo/mcp"]
PROTOCOL = "2025-03-26"
HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": PROTOCOL,
    "User-Agent": "conring-android-probe/0.1",
}


def post(url, payload, session=None, timeout=15):
    headers = dict(HEADERS)
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read().decode("utf-8", "replace")


def parse_message(raw):
    s = raw.strip()
    if s.startswith("event:") or s.startswith("data:"):
        for line in s.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except Exception:
                    pass
    try:
        return json.loads(s)
    except Exception:
        return {"_raw": s[:300]}


def probe(path):
    url = BASE + path
    print("\n==== " + url + " ====")
    try:
        status, headers, raw = post(url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "probe", "version": "0.1"}},
        })
        sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        info = parse_message(raw)
        print("initialize:", status, "session=", sid,
              "server=", (info.get("result") or {}).get("serverInfo"),
              "protocol=", (info.get("result") or {}).get("protocolVersion"))
        try:
            post(url, {"jsonrpc": "2.0", "method": "notifications/initialized",
                       "params": {}}, sid, timeout=8)
        except Exception as e:
            print("initialized notification:", type(e).__name__, str(e)[:100])

        _, _, tools_raw = post(url, {"jsonrpc": "2.0", "id": 2,
                                     "method": "tools/list", "params": {}}, sid)
        tools = ((parse_message(tools_raw).get("result") or {}).get("tools")) or []
        print("tools (%d):" % len(tools))
        for t in tools:
            print("  - %s : %s" % (t.get("name"), (t.get("description") or "")[:160].replace("\n", " ")))

        # MCP Streamable HTTP 的服务器推送通道：GET + Accept: text/event-stream
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "text/event-stream",
                "MCP-Protocol-Version": PROTOCOL,
                "Mcp-Session-Id": sid or "",
                "User-Agent": "conring-android-probe/0.1",
            }, method="GET")
            start = time.time()
            with urllib.request.urlopen(req, timeout=8) as r:
                print("GET SSE: status", r.status, "content-type", r.headers.get("Content-Type"))
                lines = []
                while time.time() - start < 5:
                    line = r.readline()
                    if not line:
                        break
                    lines.append(line.decode("utf-8", "replace").rstrip())
                    if len(lines) >= 12:
                        break
                print("GET SSE first lines:", lines[:12] if lines else "(no data within 5s)")
        except urllib.error.HTTPError as e:
            print("GET SSE: HTTP", e.code, e.read().decode("utf-8", "replace")[:200])
        except Exception as e:
            print("GET SSE:", type(e).__name__, str(e)[:160])
    except Exception as e:
        print("initialize failed:", type(e).__name__, str(e)[:200])


if __name__ == "__main__":
    for p in PATHS:
        probe(p)
