"""实测 server v2.1.0 新增接口：profile / voice_avatars / voice_chat 新参数 / 素材地址。
样本保存到 docs/v21_*.json，供 Android 端适配参考。
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

import probe_voice_flow as p

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
BASE = "https://ai.boraydata.com"
ASR = BASE + "/conring/asr/mcp"
TTS = BASE + "/conring/tts/mcp"
TEXT = "请只回复一句话：连接正常。不要执行任何操作，不要发送邮件。"


def save(name, obj):
    path = os.path.join(DOCS, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print("SAVED ->", name, os.path.getsize(path), "bytes")


def text_of(result):
    return "\n".join(b.get("text", "") for b in result.get("content", [])
                     if b.get("type") == "text")


def audio_sizes(result):
    return [len(base64.b64decode(b.get("data", "")))
            for b in result.get("content", []) if b.get("type") == "audio"]


def probe_assets(urls):
    out = []
    for url in urls:
        item = {"url": url}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "conring-probe/0.2",
                                                       "Range": "bytes=0-1023"}, method="GET")
            with urllib.request.urlopen(req, timeout=12) as r:
                item["status"] = r.status
                item["type"] = r.headers.get("Content-Type")
                item["len"] = r.headers.get("Content-Length")
                item["accept_ranges"] = r.headers.get("Accept-Ranges")
                item["preview"] = r.read(64)[:16].hex()
        except urllib.error.HTTPError as e:
            item["status"] = e.code
            item["error"] = e.reason
        except Exception as e:
            item["status"] = -1
            item["error"] = type(e).__name__ + ": " + str(e)[:120]
        out.append(item)
        print("  ASSET", json.dumps(item, ensure_ascii=False))
    return out


def main():
    print("=== 1. tools/list 参数定义 ===")
    sid = p.session(ASR)
    _, raw = p.post(ASR, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, sid, 60)
    tools = p.message(raw).get("result", {}).get("tools", [])
    save("v21_tools.json", tools)
    for t in tools:
        print("  TOOL", t.get("name"), "params:", ",".join((t.get("inputSchema") or {}).get("properties", {}).keys()))

    print("=== 2. profile 全量 ===")
    try:
        full = p.call(ASR, sid, "profile", {"include_assets": True}, timeout=240)
        save("v21_profile_all.json", full)
        j = json.loads(text_of(full))
        print("  count =", j.get("count"), "asset_url_base =", j.get("asset_url_base"))
        print("  missing_photo =", j.get("missing_photo"), "missing_video =", j.get("missing_video"))
        emps = j.get("employees", [])
        for e in emps:
            print("   ", e.get("no"), e.get("id"), e.get("name"), e.get("gender"),
                  e.get("age"), e.get("voice"), "ready=", e.get("profile_ready"),
                  "photo=", e.get("photo"), "talk=", e.get("videoTalk"))
    except Exception as e:
        print("  profile ERROR", type(e).__name__, str(e)[:300])

    print("=== 3. profile 单个 ceo ===")
    try:
        one = p.call(ASR, sid, "profile", {"id": "ceo", "include_assets": True}, timeout=120)
        save("v21_profile_ceo.json", one)
    except Exception as e:
        print("  profile(ceo) ERROR", type(e).__name__, str(e)[:300])

    print("=== 4. voice_avatars ===")
    try:
        av = p.call(ASR, sid, "voice_avatars", {}, timeout=120)
        save("v21_voice_avatars.json", av)
    except Exception as e:
        print("  voice_avatars ERROR", type(e).__name__, str(e)[:300])

    print("=== 5. 素材地址可达性（只测 MCP 同源返回的地址，不探测其它 URL） ===")
    assets = probe_assets([
        BASE + "/avatars/ceo.png",
        BASE + "/videos/ceo.talk.mp4",
        BASE + "/faces/ceo.png",
    ])
    save("v21_assets_probe.json", assets)

    print("=== 6. voice_chat 新参数（wait=false） ===")
    try:
        tts_sid = p.session(TTS)
        tts = p.call(TTS, tts_sid, "tts_synthesize", {"text": TEXT}, timeout=180)
        audio_b64 = next((b.get("data", "") for b in tts.get("content", [])
                          if b.get("type") == "audio"), "")
        print("  tts audio bytes:", len(base64.b64decode(audio_b64)) if audio_b64 else 0)
        if audio_b64:
            chat = p.call(ASR, sid, "voice_chat", {
                "audio_base64": audio_b64, "language": "zh", "model": "small",
                "avatar_id": "pm_wenren", "mode": "auto", "bandwidth_kbps": 2000,
                "wait": False, "timeout_sec": 120,
            }, timeout=180)
            save("v21_voice_chat.json", p.brief(chat))
            print("  ack audio blocks bytes:", audio_sizes(chat))
            cj = json.loads(text_of(chat))
            print("  job_id:", cj.get("job_id"), "mode:", cj.get("mode"),
                  "avatar:", (cj.get("avatar") or {}).get("id"),
                  "delegate:", (cj.get("delegate") or {}).get("id"))
            job_id = cj.get("job_id", "")
            if job_id:
                st = p.call(ASR, sid, "voice_job", {"job_id": job_id, "wait_sec": 0,
                                                    "include_audio": False}, timeout=60)
                save("v21_voice_job_status.json", p.brief(st))
                print("  job status text:", text_of(st)[:400])
    except Exception as e:
        print("  voice_chat ERROR", type(e).__name__, str(e)[:300])

    print("DONE", time.strftime("%H:%M:%S"))


if __name__ == "__main__":
    main()
