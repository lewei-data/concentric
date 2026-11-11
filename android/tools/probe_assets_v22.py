"""实测修复后的服务端素材（server v2.1.0+ / 2026-11-10）。

严格约束：只访问 MCP 端点同源地址 https://ai.boraydata.com，
不探测其它主机、端口或第三方 URL，不带 Token、不需要 VPN。

产出：
  docs/v22_profile_all.json   profile 原始响应
  docs/v22_voice_avatars.json voice_avatars 原始响应
  docs/v22_assets_check.json  18 人 × 4 类素材的逐项 HTTP 校验结果
"""
import json
import os
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP = "https://ai.boraydata.com/conring/asr/mcp"
ORIGIN = "https://ai.boraydata.com"
UA = {"User-Agent": "conring-probe/0.3"}


def parse_body(body):
    b = body.strip()
    if not b:
        return {}
    if b.startswith("event:") or b.startswith("data:") or "\ndata:" in b:
        lines = [l[5:].strip() for l in b.splitlines() if l.startswith("data:")]
        if lines:
            b = lines[-1]
    return json.loads(b)


def call(payload, sid=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **UA,
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(
        MCP, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        lower = {k.lower(): v for k, v in r.headers.items()}
        new_sid = lower.get("mcp-session-id") or sid
        body = r.read().decode("utf-8", "replace")
    return new_sid, body


def session():
    sid, _ = call({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "conring-probe", "version": "0.3"},
        },
    })
    call({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, sid)
    return sid


def tool(sid, name, args):
    _, body = call({
        "jsonrpc": "2.0", "id": 77, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }, sid)
    return parse_body(body)


def content_text(doc):
    for block in doc.get("result", doc).get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    return ""


def check(path, with_range=False):
    """只把服务端返回的相对路径拼到 MCP 同源 origin"""
    if not path:
        return {"path": "", "status": "EMPTY"}
    url = ORIGIN + path if path.startswith("/") else path
    if not url.startswith(ORIGIN):
        return {"path": path, "status": "SKIPPED_NON_SAME_ORIGIN"}
    headers = dict(UA)
    if with_range:
        headers["Range"] = "bytes=0-1023"
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=15) as r:
            chunk = r.read(16)
            return {
                "path": path, "status": r.status,
                "type": r.headers.get("Content-Type"),
                "length": r.headers.get("Content-Length"),
                "magic": chunk[:8].hex(),
            }
    except urllib.error.HTTPError as e:
        return {"path": path, "status": e.code, "reason": e.reason}
    except Exception as e:
        return {"path": path, "status": type(e).__name__, "reason": str(e)[:100]}


def pick_asset(e, *keys):
    for k in keys:
        v = e.get(k, "")
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
    return ""


def main():
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    sid = session()

    profile_raw = tool(sid, "profile", {"include_assets": True})
    avatars_raw = tool(sid, "voice_avatars", {})

    with open(os.path.join(ROOT, "docs", "v22_profile_all.json"), "w",
              encoding="utf-8") as f:
        json.dump(profile_raw, f, ensure_ascii=False, indent=2)
    with open(os.path.join(ROOT, "docs", "v22_voice_avatars.json"), "w",
              encoding="utf-8") as f:
        json.dump(avatars_raw, f, ensure_ascii=False, indent=2)

    root = json.loads(content_text(profile_raw))
    emps = root.get("employees", [])
    print("=== profile ===")
    print("count =", len(emps), " asset_url_base =", repr(root.get("asset_url_base")))
    print("missing_photo =", root.get("missing_photo"),
          " missing_video =", root.get("missing_video"))
    if emps:
        ceo = next((e for e in emps if e.get("id") == "ceo"), emps[0])
        print("ceo keys =", sorted(ceo.keys()))
        for k in ("photo", "videoTalk", "videoIdle", "face_url", "faceUrl",
                  "has_photo", "has_video", "assets"):
            print("   ", k, "=", repr(ceo.get(k))[:220])

    print("\n=== voice_avatars 结构 ===")
    av = json.loads(content_text(avatars_raw))
    print("keys =", sorted(av.keys()))
    print("asset_url_base =", repr(av.get("asset_url_base")))
    if av.get("avatars"):
        print("avatar[0] =", json.dumps(av["avatars"][0], ensure_ascii=False)[:400])

    print("\n=== 18 人素材逐项校验（同源 GET + 视频 Range） ===")
    report = {"asset_url_base": root.get("asset_url_base"),
              "count": len(emps), "items": []}
    ok = bad = 0
    for e in emps:
        eid = e.get("id", "")
        assets = e.get("assets") or {}
        paths = {
            "photo": pick_asset(e, "photo"),
            "videoTalk": pick_asset(e, "videoTalk", "video_talk"),
            "videoIdle": pick_asset(e, "videoIdle", "video_idle"),
            "face_url": pick_asset(e, "face_url", "faceUrl")
                        or (assets.get("face_url") or ""),
        }
        row = {"id": eid, "checks": {}}
        for kind, path in paths.items():
            r = check(path)
            row["checks"][kind] = r
            if r.get("status") in (200, 206):
                ok += 1
            else:
                bad += 1
                print("  FAIL", eid, kind, r)
        talk = paths["videoTalk"]
        if talk:
            rr = check(talk, with_range=True)
            row["checks"]["videoTalk_range"] = rr
            if rr.get("status") == 206:
                ok += 1
            else:
                bad += 1
                print("  FAIL", eid, "Range", rr)
        report["items"].append(row)
    report["ok"] = ok
    report["bad"] = bad
    with open(os.path.join(ROOT, "docs", "v22_assets_check.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("TOTAL ok =", ok, " bad =", bad)
    print("SAVED -> docs/v22_*.json")


if __name__ == "__main__":
    main()
