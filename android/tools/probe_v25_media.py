"""v2.5 媒体面实测：intro mp4 的 Range 播放、live 起流/说话/挂断、HLS 分片可用性。

只用同源 https://ai.boraydata.com 下的 /conring/asr/assets/*，不访问其它 URL。
输出 docs/v25_media_check.json。
"""
import json
import os
import urllib.request

import probe_voice_flow as p

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
BASE = "https://ai.boraydata.com"
ASR = BASE + "/conring/asr/mcp"
UA = {"User-Agent": "conring-android-probe/0.1"}


def head(url, rng=None):
    h = dict(UA)
    if rng:
        h["Range"] = rng
    req = urllib.request.Request(url, headers=h, method="GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read(4096) if rng else b""
        return {"status": r.status, "ctype": r.headers.get("Content-Type"),
                "len": r.headers.get("Content-Length"),
                "cr": r.headers.get("Content-Range"),
                "accept_ranges": r.headers.get("Accept-Ranges"),
                "first": body[:400].decode("utf-8", "replace")}


def text_of(result):
    return "\n".join(b.get("text", "") for b in result.get("content", [])
                     if b.get("type") == "text")


def main():
    out = {}

    # 1) 自我介绍 MP4：整体头 + Range
    url = BASE + "/conring/asr/assets/intro/ceo.mp4"
    try:
        out["intro_full"] = head(url)
        out["intro_range"] = head(url, "bytes=0-1023")
        print("[intro] full:", out["intro_full"]["status"], out["intro_full"]["ctype"],
              out["intro_full"]["len"])
        print("[intro] range:", out["intro_range"]["status"], out["intro_range"]["cr"])
    except Exception as e:
        out["intro_error"] = type(e).__name__ + ": " + str(e)[:200]
        print("[intro] ERROR", out["intro_error"])

    sid = p.session(ASR)

    # 2) 起流
    start = json.loads(text_of(p.call(ASR, sid, "live_call",
                                      {"action": "start", "avatar_id": "ceo"}, 180)))
    out["start"] = start
    live = start.get("hls_url", "")
    print("[live] start:", start.get("session_id"), start.get("status"), live)

    try:
        out["m3u8"] = head(BASE + live)
        print("[live] m3u8:", out["m3u8"]["status"], out["m3u8"]["ctype"])
        print(out["m3u8"]["first"][:400])
    except Exception as e:
        out["m3u8_error"] = type(e).__name__ + ": " + str(e)[:200]
        print("[live] m3u8 ERROR", out["m3u8_error"])

    # 3) 说话
    try:
        say = text_of(p.call(ASR, sid, "live_call",
                             {"action": "say", "session_id": start["session_id"],
                              "text": "这是实时通话测试，请忽略。"}, 180))
        out["say"] = say[:2000]
        print("[live] say:", say[:600])
    except Exception as e:
        out["say_error"] = type(e).__name__ + ": " + str(e)[:300]
        print("[live] say ERROR", out["say_error"])

    # 4) 分片可达
    try:
        m3 = out.get("m3u8", {}).get("first", "")
        segs = [l.strip() for l in m3.splitlines() if l.strip() and not l.startswith("#")]
        out["segments"] = segs
        if segs:
            base_dir = live.rsplit("/", 1)[0]
            su = BASE + base_dir + "/" + segs[0]
            out["seg0"] = head(su, "bytes=0-2047")
            print("[live] seg0:", out["seg0"]["status"], out["seg0"]["ctype"],
                  out["seg0"]["len"])
    except Exception as e:
        out["seg_error"] = type(e).__name__ + ": " + str(e)[:200]
        print("[live] seg ERROR", out["seg_error"])

    # 5) 挂断
    try:
        stop = text_of(p.call(ASR, sid, "live_call",
                              {"action": "stop", "session_id": start["session_id"]}, 120))
        out["stop"] = stop[:1000]
        print("[live] stop:", stop[:300])
    except Exception as e:
        out["stop_error"] = type(e).__name__ + ": " + str(e)[:300]
        print("[live] stop ERROR", out["stop_error"])

    with open(os.path.join(DOCS, "v25_media_check.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("SAVED -> docs/v25_media_check.json")


if __name__ == "__main__":
    main()
