"""实测异步语音链路：tts_synthesize -> voice_chat(wait=false) -> voice_job(长轮询)。
把返回结构（截断 base64）保存到 docs/voice_flow_sample.json，供 Android 端解析实现参考。
注意：会真实触发一次服务端 Hermes 任务，语音内容已限制为「不执行任何操作」。
"""
import base64
import json
import time
import urllib.request

BASE = "https://ai.boraydata.com"
ASR = BASE + "/conring/asr/mcp"
TTS = BASE + "/conring/tts/mcp"
PROTOCOL = "2025-03-26"
H = {"Content-Type": "application/json; charset=utf-8",
     "Accept": "application/json, text/event-stream",
     "MCP-Protocol-Version": PROTOCOL,
     "User-Agent": "conring-android-probe/0.1"}
TEXT = "测试连接，请只回复一句话：连接正常。不要执行任何操作，不要发送邮件。"


def post(url, payload, sid=None, timeout=120):
    h = dict(H)
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return dict(r.headers), r.read().decode("utf-8", "replace")


def message(raw):
    s = raw.strip()
    if s.startswith("event:") or s.startswith("data:"):
        for line in s.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except Exception:
                    pass
    return json.loads(s)


def session(url):
    headers, raw = post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                         "clientInfo": {"name": "flow-probe", "version": "0.1"}}})
    sid = next((v for k, v in headers.items() if k.lower() == "mcp-session-id"), None)
    try:
        post(url, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, sid, 15)
    except Exception:
        pass
    return sid


def call(url, sid, name, args, timeout=150):
    _, raw = post(url, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                        "params": {"name": name, "arguments": args}}, sid, timeout)
    m = message(raw)
    if "error" in m:
        raise RuntimeError(str(m["error"])[:300])
    return m.get("result", {})


def brief(obj, depth=0):
    """递归把 >200 字符串缩成 <base64 len=N>"""
    if isinstance(obj, str):
        return obj if len(obj) <= 200 else "<string len=%d> %s..." % (len(obj), obj[:80])
    if isinstance(obj, dict):
        return {k: brief(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [brief(v, depth + 1) for v in obj[:6]] + (["..."] if len(obj) > 6 else [])
    return obj


def main():
    out = {"text": TEXT}
    print("STEP 1 tts_synthesize ...")
    tts_sid = session(TTS)
    tts = call(TTS, tts_sid, "tts_synthesize", {"text": TEXT}, timeout=180)
    out["tts_result_keys"] = list(tts.keys())
    audio_b64 = ""
    for block in tts.get("content", []):
        if block.get("type") == "audio":
            audio_b64 = block.get("data", "")
            print("  tts audio bytes:", len(base64.b64decode(audio_b64)))
        elif block.get("type") == "text":
            print("  tts text:", block.get("text", "")[:300])
            out["tts_text"] = block.get("text", "")[:1000]
    if not audio_b64:
        print("  NO AUDIO, tts brief:", json.dumps(brief(tts), ensure_ascii=False)[:600])
        return
    out["tts_audio_bytes"] = len(base64.b64decode(audio_b64))

    print("STEP 2 voice_chat(wait=false) ...")
    asr_sid = session(ASR)
    chat = call(ASR, asr_sid, "voice_chat",
                {"audio_base64": audio_b64, "language": "zh", "model": "small",
                 "wait": False, "timeout_sec": 120}, timeout=180)
    out["voice_chat"] = brief(chat)
    job_id = ""
    for block in chat.get("content", []):
        if block.get("type") == "text":
            t = block.get("text", "")
            print("  chat text:", t[:600])
            try:
                job_id = json.loads(t).get("job_id", "")
            except Exception:
                pass
        elif block.get("type") == "audio":
            print("  chat ack audio bytes:", len(base64.b64decode(block.get("data", ""))))
    print("  job_id =", job_id)
    out["job_id"] = job_id
    if not job_id:
        with open("docs/voice_flow_sample.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return

    print("STEP 3 voice_job long polling ...")
    polls = []
    for i in range(4):
        started = time.time()
        try:
            job = call(ASR, asr_sid, "voice_job",
                       {"job_id": job_id, "wait_sec": 25, "include_audio": True}, timeout=60)
        except Exception as e:
            print("  poll", i + 1, "error:", type(e).__name__, str(e)[:200])
            break
        brief_job = brief(job)
        polls.append({"elapsed_sec": round(time.time() - started, 1), "result": brief_job})
        print("  poll", i + 1, "elapsed", round(time.time() - started, 1), "s")
        for block in job.get("content", []):
            if block.get("type") == "text":
                print("    job text:", block.get("text", "")[:700])
            elif block.get("type") == "audio":
                print("    job audio bytes:", len(base64.b64decode(block.get("data", ""))))
        txt = " ".join(b.get("text", "") for b in job.get("content", []) if b.get("type") == "text")
        if '"done":true' in txt.replace(" ", "") or '"status":"done"' in txt.replace(" ", ""):
            break
    out["voice_job_polls"] = polls
    with open("docs/voice_flow_sample.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("SAVED -> docs/voice_flow_sample.json")


if __name__ == "__main__":
    main()
