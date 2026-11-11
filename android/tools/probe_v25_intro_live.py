"""服务端 v2.5 契约实测：intro_video / live_call 工具 + profile 新增字段。

只访问 MCP 同源端点（/conring/{asr,tts,mail}/mcp），不探测任何其它 URL。
输出 docs/v25_server_contract.json。
"""
import json
import os
import sys
import time

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


def load(url, sid, name):
    return p.call(url, sid, name, {}, timeout=240)


def main():
    out = {"base": BASE, "endpoints": {}}
    for tag, url in ENDPOINTS.items():
        entry = {"url": url}
        try:
            sid = p.session(url)
            _, raw = p.post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                  "params": {}}, sid, 60)
            tools = p.message(raw).get("result", {}).get("tools", [])
            entry["tools"] = [
                {"name": t.get("name"),
                 "description": (t.get("description") or "")[:600],
                 "params": list((t.get("inputSchema") or {}).get("properties", {}).keys())}
                for t in tools
            ]
            print("[%s] tools:" % tag, ", ".join(t["name"] for t in entry["tools"]))
        except Exception as e:
            entry["error"] = type(e).__name__ + ": " + str(e)[:300]
            print("[%s] ERROR %s" % (tag, entry["error"]))
        out["endpoints"][tag] = entry

    url = ENDPOINTS["asr"]
    sid = None
    try:
        sid = p.session(url)
        prof = p.call(url, sid, "profile", {"include_assets": True}, timeout=240)
        data = json.loads(text_of(prof))
        out["profile_top_keys"] = sorted(data.keys())
        out["profile_count"] = data.get("count")
        out["asset_url_base"] = data.get("asset_url_base")
        out["profile_meta"] = {k: data.get(k) for k in
                               ("missing_photo", "missing_video", "missing_intro",
                                "intro_ready", "counts", "stats")
                               if k in data}
        emps = data.get("employees", [])
        print("[profile] count =", out["profile_count"],
              "asset_url_base =", out["asset_url_base"])
        print("[profile] top keys =", out["profile_top_keys"])
        print("[profile] meta =", json.dumps(out["profile_meta"], ensure_ascii=False)[:600])

        if emps:
            keys = set()
            for e in emps:
                keys.update(e.keys())
                a = e.get("assets") or {}
                keys.update("assets." + k for k in a.keys())
                lc = e.get("liveCall") or e.get("live_call")
                if isinstance(lc, dict):
                    keys.update("liveCall." + k for k in lc.keys())
            out["all_employee_field_names"] = sorted(keys)
            print("[profile] all fields =", out["all_employee_field_names"])

            ceo = next((e for e in emps if e.get("id") == "ceo"), emps[0])
            out["ceo"] = p.brief(ceo)
            print("[profile] ceo =", json.dumps(out["ceo"], ensure_ascii=False)[:1600])

            ready = [e.get("id") for e in emps
                     if e.get("introReady") or e.get("intro_ready")
                     or e.get("introVideo") or e.get("intro_video")]
            out["intro_ready_ids"] = ready
            print("[profile] intro ready =", len(ready), ready[:20])
            lc_ok = [e.get("id") for e in emps
                     if (isinstance(e.get("liveCall"), dict) and e["liveCall"].get("supported"))
                     or (isinstance(e.get("live_call"), dict) and e["live_call"].get("supported"))]
            out["live_call_ids"] = lc_ok
            print("[profile] liveCall supported =", len(lc_ok))
    except Exception as e:
        out["profile_error"] = type(e).__name__ + ": " + str(e)[:400]
        print("[profile] ERROR", out["profile_error"])

    # 尝试按需生成 CEO 自我介绍（若工具存在）
    names = [t["name"] for t in out["endpoints"]["asr"].get("tools", [])]
    if sid and "intro_video" in names:
        try:
            t0 = time.time()
            r = p.call(url, sid, "intro_video",
                       {"employee_id": "ceo", "force": False}, timeout=600)
            out["intro_video_call"] = {"elapsed": round(time.time() - t0, 1),
                                       "text": text_of(r)[:3000]}
            print("[intro_video] done in", out["intro_video_call"]["elapsed"], "s")
            print("[intro_video]", text_of(r)[:1200])
        except Exception as e:
            out["intro_video_error"] = type(e).__name__ + ": " + str(e)[:400]
            print("[intro_video] ERROR", out["intro_video_error"])

    if sid and "live_call" in names:
        try:
            r = p.call(url, sid, "live_call", {"employee_id": "ceo", "action": "start"},
                       timeout=180)
            out["live_call_start"] = {"text": text_of(r)[:3000]}
            print("[live_call:start]", text_of(r)[:1200])
        except Exception as e:
            out["live_call_error"] = type(e).__name__ + ": " + str(e)[:400]
            print("[live_call:start] ERROR", out["live_call_error"])

    path = os.path.join(DOCS, "v25_server_contract.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("SAVED -> docs/v25_server_contract.json")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
