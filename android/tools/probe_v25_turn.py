"""v2.5 上行链路实测：接通话后「用户说话 -> 员工在直播流里回应」走哪条最稳。

对比两条路径：
  A) live_call{action:say, audio_base64}      —— 工具注释写明支持音频
  B) voice_chat{audio_base64, live_session}   —— voice_chat 新增 live_session 参数
只访问同源端点，音频来自 tts_synthesize。
输出 docs/v25_turn_check.json。
"""
import base64
import json
import os
import time

import probe_voice_flow as p

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
BASE = "https://ai.boraydata.com"
ASR = BASE + "/conring/asr/mcp"
TTS = BASE + "/conring/tts/mcp"
ASK = "请用一句话介绍你自己，不要执行任何操作。"


def text_of(result):
    return "\n".join(b.get("text", "") for b in result.get("content", [])
                     if b.get("type") == "text")


def audio_blocks(result):
    out = []
    for b in result.get("content", []):
        if b.get("type") == "audio":
            out.append({"mime": b.get("mimeType"), "bytes": len(b.get("data", ""))})
    return out


def main():
    out = {"ask": ASK}

    tts_sid = p.session(TTS)
    tts = p.call(TTS, tts_sid, "tts_synthesize",
                 {"text": ASK, "return_audio": True}, 180)
    out["tts_blocks"] = audio_blocks(tts)
    txt = text_of(tts)
    print("[tts]", out["tts_blocks"], txt[:200])
    b64 = ""
    for b in tts.get("content", []):
        if b.get("type") == "audio" and b.get("data"):
            b64 = b["data"]
            break
    if not b64:
        try:
            j = json.loads(txt)
            b64 = j.get("audio_base64") or j.get("audio") or ""
        except Exception:
            pass
    out["audio_b64_len"] = len(b64)
    print("[tts] audio base64 len =", len(b64))
    if not b64:
        print("NO AUDIO, abort")
        return

    asr = p.session(ASR)

    # A) live_call say(audio)
    try:
        st = json.loads(text_of(p.call(ASR, asr, "live_call",
                                       {"action": "start", "avatar_id": "ceo"}, 180)))
        out["session_a"] = st["session_id"]
        t0 = time.time()
        r = p.call(ASR, asr, "live_call",
                   {"action": "say", "session_id": st["session_id"],
                    "audio_base64": b64}, 300)
        out["A_live_call_audio"] = {"elapsed": round(time.time() - t0, 1),
                                    "text": text_of(r)[:2500],
                                    "audio_blocks": audio_blocks(r)}
        print("[A] live_call say(audio) %.1fs" % out["A_live_call_audio"]["elapsed"])
        print("[A]", out["A_live_call_audio"]["text"][:800].replace("\n", " "))
        p.call(ASR, asr, "live_call",
               {"action": "stop", "session_id": st["session_id"]}, 120)
    except Exception as e:
        out["A_error"] = type(e).__name__ + ": " + str(e)[:400]
        print("[A] ERR", out["A_error"])

    # B) voice_chat(live_session)
    try:
        st2 = json.loads(text_of(p.call(ASR, asr, "live_call",
                                        {"action": "start", "avatar_id": "ceo"}, 180)))
        out["session_b"] = st2["session_id"]
        t0 = time.time()
        r = p.call(ASR, asr, "voice_chat",
                   {"audio_base64": b64, "avatar_id": "ceo",
                    "live_session": st2["session_id"], "wait": True,
                    "include_audio": False, "timeout_sec": 180}, 300)
        out["B_voice_chat_live"] = {"elapsed": round(time.time() - t0, 1),
                                    "text": text_of(r)[:2500],
                                    "audio_blocks": audio_blocks(r)}
        print("[B] voice_chat(live_session) %.1fs" % out["B_voice_chat_live"]["elapsed"])
        print("[B]", out["B_voice_chat_live"]["text"][:1200].replace("\n", " "))
        time.sleep(2)
        hl = st2["hls_url"]
        import urllib.request
        req = urllib.request.Request(BASE + hl, headers={"User-Agent": "probe/0.1"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            pl = resp.read().decode("utf-8", "replace")
        segs = [l for l in pl.splitlines() if l and not l.startswith("#")]
        out["B_segments"] = len(segs)
        print("[B] playlist segments =", len(segs))
        p.call(ASR, asr, "live_call",
               {"action": "stop", "session_id": st2["session_id"]}, 120)
    except Exception as e:
        out["B_error"] = type(e).__name__ + ": " + str(e)[:400]
        print("[B] ERR", out["B_error"])

    with open(os.path.join(DOCS, "v25_turn_check.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("SAVED -> docs/v25_turn_check.json")


if __name__ == "__main__":
    main()
