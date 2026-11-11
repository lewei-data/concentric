"""读取一个已完成 voice_job 的完整结果结构 + 探测数字人视频 URL 是否可下载。"""
import json
import os
import sys
import urllib.request
import urllib.error
import probe_voice_flow as p

# 用法：python tools/probe_job_done.py <job_id>
JOB_ID = sys.argv[1] if len(sys.argv) > 1 else "vj_8380af_50550"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    sid = p.session(p.ASR)
    job = p.call(p.ASR, sid, "voice_job",
                 {"job_id": JOB_ID, "wait_sec": 0, "include_audio": True}, timeout=60)
    full_text = ""
    for block in job.get("content", []):
        if block.get("type") == "text":
            full_text = block.get("text", "")
    print("STATUS text len:", len(full_text))
    try:
        j = json.loads(full_text)
        print("TOP KEYS:", list(j.keys()))
        print(json.dumps(j, ensure_ascii=False, indent=2)[:6000])
        with open(os.path.join(ROOT, "docs", "voice_job_done.json"), "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False, indent=2)
        print("SAVED -> docs/voice_job_done.json")
    except Exception as e:
        print("not json:", e, full_text[:2000])

    print("\nVIDEO URL PROBE:")
    for url in ["https://ai.boraydata.com/videos/ceo.talk.mp4",
                "https://ai.boraydata.com/avatars/ceo.png"]:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=12) as r:
                print(" ", url, "->", r.status, r.headers.get("Content-Type"),
                      r.headers.get("Content-Length"))
        except urllib.error.HTTPError as e:
            print(" ", url, "-> HTTP", e.code)
        except Exception as e:
            print(" ", url, "->", type(e).__name__, str(e)[:100])


if __name__ == "__main__":
    main()
