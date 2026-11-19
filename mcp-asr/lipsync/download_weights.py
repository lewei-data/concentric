import os
import sys
import urllib.request

URL = "https://hf-mirror.com/camenduru/Wav2Lip/resolve/main/checkpoints/wav2lip_gan.pth"
DST = "/opt/sefs/opc-workbench/mcp-asr/lipsync/weights/wav2lip_gan.pth"


def main():
    os.makedirs(os.path.dirname(DST), exist_ok=True)
    tmp = DST + ".part"
    if os.path.exists(DST) and os.path.getsize(DST) > 100 * 1024 * 1024:
        print("already exists", os.path.getsize(DST))
        return
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if done % (20 * 1024 * 1024) < 1024 * 1024:
                print(f"{done/1048576:.0f}/{total/1048576:.0f} MB", flush=True)
    os.replace(tmp, DST)
    print("done", os.path.getsize(DST))


if __name__ == "__main__":
    main()
