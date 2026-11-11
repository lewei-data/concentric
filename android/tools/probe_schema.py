"""导出 /conring/asr/mcp 的工具参数定义（含 voice_chat / voice_job 的 inputSchema）。"""
import json
import urllib.request

URL = "https://ai.boraydata.com/conring/asr/mcp"
PROTOCOL = "2025-03-26"
H = {"Content-Type": "application/json; charset=utf-8",
     "Accept": "application/json, text/event-stream",
     "MCP-Protocol-Version": PROTOCOL}


def post(payload, sid=None):
    h = dict(H)
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return dict(r.headers), r.read().decode("utf-8", "replace")


def msg(raw):
    s = raw.strip()
    if s.startswith("event:") or s.startswith("data:"):
        for line in s.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
    return json.loads(s)


def main():
    headers, raw = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "schema-probe", "version": "0.1"}}})
    sid = next((v for k, v in headers.items() if k.lower() == "mcp-session-id"), None)
    try:
        post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, sid)
    except Exception as e:
        print("initialized notification (ignored):", type(e).__name__)
    _, raw = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, sid)
    tools = msg(raw)["result"]["tools"]
    out = {t["name"]: {"description": t.get("description", ""),
                       "inputSchema": t.get("inputSchema", {})} for t in tools}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    with open("docs/mcp_asr_tools.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\nSAVED -> docs/mcp_asr_tools.json")


if __name__ == "__main__":
    main()
