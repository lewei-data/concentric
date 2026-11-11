"""0.3.2 核查：服务端 profile 与 APK 内置 18 人档案的素材地址是否一致。

只访问 MCP 同源端点（/conring/asr/mcp），不探测任何其它地址。
输出 docs/v23_assets_compare.json，供离线复核「照片/视频形象一致性」。
"""
import json
import os
import urllib.request

BASE = "https://ai.boraydata.com/conring/asr/mcp"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def post(obj, sid=None, parse=True):
    h = dict(HEADERS)
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(
        BASE, data=json.dumps(obj).encode("utf-8"), headers=h, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", "replace")
        sid = resp.headers.get("Mcp-Session-Id") or sid
    if not parse:
        return None, sid
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
    return json.loads(raw), sid


def main():
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "conring-asset-check", "version": "0.3.2"},
        },
    }
    _, sid = post(init)
    post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
         sid, False)
    res, _ = post({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "profile", "arguments": {"include_assets": True}},
    }, sid)
    text = next(b.get("text", "") for b in res["result"]["content"]
                if b.get("type") == "text")
    data = json.loads(text)

    with open(os.path.join(ROOT, "app", "src", "main", "assets", "employees.json"),
              encoding="utf-8") as f:
        offline = {e["id"]: e for e in json.load(f)["employees"]}

    fields = ("photo", "videoTalk", "videoIdle", "faceUrl")
    mismatches = []
    rows = []
    for e in data["employees"]:
        a = e.get("assets") or {}
        want = {
            "photo": e.get("photo", ""),
            "videoTalk": e.get("videoTalk", ""),
            "videoIdle": e.get("videoIdle", ""),
            "faceUrl": a.get("face_url", ""),
        }
        got = offline.get(e["id"], {})
        row = {"id": e["id"], "name": e.get("name"), "voice": e.get("voice"),
               "server": want, "offline": {k: got.get(k, "") for k in fields}}
        rows.append(row)
        for k in fields:
            if (want[k] or "") != (got.get(k) or ""):
                mismatches.append({"id": e["id"], "field": k,
                                   "server": want[k], "offline": got.get(k, "")})

    out = {
        "checked_at": "2026-11-10",
        "server_count": len(data["employees"]),
        "offline_count": len(offline),
        "asset_url_base": data.get("asset_url_base"),
        "fields": list(fields),
        "compared": len(rows) * len(fields),
        "mismatches": mismatches,
        "ceo": next((r for r in rows if r["id"] == "ceo"), None),
        "employees": rows,
    }
    path = os.path.join(ROOT, "docs", "v23_assets_compare.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("server=%d offline=%d compared=%d mismatches=%d"
          % (out["server_count"], out["offline_count"],
             out["compared"], len(mismatches)))
    print("asset_url_base =", out["asset_url_base"])
    print("ceo =", json.dumps(out["ceo"], ensure_ascii=False))
    print("SAVED -> docs/v23_assets_compare.json")


if __name__ == "__main__":
    main()
