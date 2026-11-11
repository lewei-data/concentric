"""0.4.0 需求前置核查：服务端是否已提供「自我介绍视频/语音」与「实时视频通话」。

只访问 MCP 同源端点（/conring/{asr,tts,mail}/mcp），不探测任何其它 URL。
输出 docs/v24_server_contract.json，供客户端确定字段/工具契约。
"""
import json
import os

import probe_voice_flow as p

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
BASE = "https://ai.boraydata.com"
ENDPOINTS = {
    "asr": BASE + "/conring/asr/mcp",
    "tts": BASE + "/conring/tts/mcp",
    "mail": BASE + "/conring/mail/ceo/mcp",
}


def text_of(result):
    return "\n".join(b.get("text", "") for b in result.get("content", [])
                     if b.get("type") == "text")


def tool_list(url, sid):
    _, raw = p.post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                          "params": {}}, sid, 60)
    return p.message(raw).get("result", {}).get("tools", [])


def main():
    out = {"endpoints": {}}
    for tag, url in ENDPOINTS.items():
        entry = {"url": url}
        try:
            sid = p.session(url)
            tools = tool_list(url, sid)
            entry["tools"] = [
                {"name": t.get("name"),
                 "description": (t.get("description") or "")[:400],
                 "params": list((t.get("inputSchema") or {}).get("properties", {}).keys())}
                for t in tools
            ]
            print("[%s] tools:" % tag, ", ".join(t["name"] for t in entry["tools"]))
        except Exception as e:
            entry["error"] = type(e).__name__ + ": " + str(e)[:200]
            print("[%s] ERROR %s" % (tag, entry["error"]))
        out["endpoints"][tag] = entry

    url = ENDPOINTS["asr"]
    try:
        sid = p.session(url)
        prof = p.call(url, sid, "profile", {"include_assets": True}, timeout=240)
        data = json.loads(text_of(prof))
        out["profile_top_keys"] = sorted(data.keys())
        out["profile_count"] = data.get("count")
        out["asset_url_base"] = data.get("asset_url_base")
        emps = data.get("employees", [])
        if emps:
            out["employee_keys"] = sorted(emps[0].keys())
            ceo = next((e for e in emps if e.get("id") == "ceo"), emps[0])
            out["ceo"] = json.loads(json.dumps(p.brief(ceo), ensure_ascii=False))
            out["ceo_raw_keys"] = sorted(ceo.keys())
            # 汇总所有出现过的字段名，便于发现新字段
            keys = set()
            for e in emps:
                keys.update(e.keys())
                a = e.get("assets") or {}
                keys.update("assets." + k for k in a.keys())
            out["all_employee_field_names"] = sorted(keys)
        print("[profile] count =", out["profile_count"],
              "asset_url_base =", out["asset_url_base"])
        print("[profile] top keys =", out["profile_top_keys"])
        print("[profile] employee keys =", out.get("employee_keys"))
        print("[profile] all fields =", out.get("all_employee_field_names"))
        print("[profile] ceo =", json.dumps(out.get("ceo"), ensure_ascii=False)[:1200])
    except Exception as e:
        out["profile_error"] = type(e).__name__ + ": " + str(e)[:300]
        print("[profile] ERROR", out["profile_error"])

    path = os.path.join(DOCS, "v24_server_contract.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("SAVED -> docs/v24_server_contract.json")


if __name__ == "__main__":
    main()
