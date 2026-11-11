"""v2.5 深度实测：live playlist 生命周期、分片容器是否含音频、intro mp4 音轨。

只访问同源 https://ai.boraydata.com/conring/asr/assets/*。
输出 docs/v25_live_deep.json。
"""
import json
import os
import re
import time
import urllib.request

import probe_voice_flow as p

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
BASE = "https://ai.boraydata.com"
ASR = BASE + "/conring/asr/mcp"
UA = {"User-Agent": "conring-android-probe/0.1"}


def get(url, rng=None, raw=False):
    h = dict(UA)
    if rng:
        h["Range"] = rng
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read() if raw else r.read()
        return r.status, dict(r.headers), body


def text_of(result):
    return "\n".join(b.get("text", "") for b in result.get("content", [])
                     if b.get("type") == "text")


def ts_streams(data):
    """在 TS 分片里找 PAT/PMT，列出 stream_type（0x0f=AAC, 0x1b=H.264）"""
    types = set()
    i = 0
    n = len(data)
    while i + 188 <= n:
        if data[i] != 0x47:
            i += 1
            continue
        pkt = data[i:i + 188]
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        pusi = (pkt[1] & 0x40) != 0
        afc = (pkt[3] >> 4) & 0x03
        off = 4
        if afc & 0x02:
            off += 1 + pkt[4]
        if pusi and off < 188 and pkt[off] == 0x00:
            # pointer_field
            ptr = pkt[off]
            sec = pkt[off + 1 + ptr: 188]
            if len(sec) > 8 and sec[0] == 0x02:  # PMT
                slen = ((sec[1] & 0x0F) << 8) | sec[2]
                pil = ((sec[10] & 0x0F) << 8) | sec[11]
                k = 12 + pil
                end = min(3 + slen - 4, len(sec))
                while k + 4 < end:
                    st = sec[k]
                    elen = ((sec[k + 3] & 0x0F) << 8) | sec[k + 4]
                    if st:
                        types.add(hex(st))
                    k += 5 + elen
        i += 188
    return sorted(types)


def mp4_tracks(data):
    """粗略扫描 mp4 box 类型，判断是否有视频/音频轨"""
    kinds = set()
    for tag in (b"avc1", b"hvc1", b"mp4a", b"ac-3", b"vp09", b"av01"):
        if tag in data:
            kinds.add(tag.decode())
    return sorted(kinds)


def main():
    out = {}
    sid = p.session(ASR)
    start = json.loads(text_of(p.call(ASR, sid, "live_call",
                                      {"action": "start", "avatar_id": "ceo"}, 180)))
    live = start["hls_url"]
    out["start"] = start
    print("[start]", json.dumps(start, ensure_ascii=False))

    snapshots = []
    for i in range(10):
        try:
            st, hdr, body = get(BASE + live, raw=True)
            txt = body.decode("utf-8", "replace")
            segs = [l.strip() for l in txt.splitlines()
                    if l.strip() and not l.startswith("#")]
            snapshots.append({"t": round(time.time() - start["started_at"], 1),
                              "status": st, "len": len(body),
                              "segments": len(segs), "endlist": "#EXT-X-ENDLIST" in txt,
                              "head": txt[:600], "segs": segs[-2:]})
            print("[m3u8 t=%5.1f] status=%s bytes=%d segs=%d endlist=%s"
                  % (snapshots[-1]["t"], st, len(body), len(segs),
                     snapshots[-1]["endlist"]))
        except Exception as e:
            snapshots.append({"t": round(time.time() - start["started_at"], 1),
                              "error": type(e).__name__ + ": " + str(e)[:160]})
            print("[m3u8] ERR", snapshots[-1]["error"])
        time.sleep(1.2)
    out["playlist_snapshots"] = snapshots

    # 分片容器是否有音轨
    m3 = next((s for s in snapshots if s.get("segs")), None)
    if m3:
        base_dir = live.rsplit("/", 1)[0]
        seg = m3["segs"][-1]
        try:
            st, hdr, data = get(BASE + base_dir + "/" + seg, raw=True)
            out["segment"] = {"name": seg, "status": st,
                              "ctype": hdr.get("Content-Type"), "bytes": len(data),
                              "ts_stream_types": ts_streams(data),
                              "mp4_tracks": mp4_tracks(data),
                              "first_bytes": data[:8].hex()}
            print("[seg]", out["segment"]["name"], out["segment"]["bytes"],
                  "TS streams:", out["segment"]["ts_stream_types"],
                  "mp4:", out["segment"]["mp4_tracks"])
        except Exception as e:
            out["segment_error"] = type(e).__name__ + ": " + str(e)[:200]
            print("[seg] ERR", out["segment_error"])

    # 让员工说话（文本入流），观察 segments 增长
    try:
        t0 = time.time()
        say = text_of(p.call(ASR, sid, "live_call",
                             {"action": "say", "session_id": start["session_id"],
                              "text": "我是陆清安，这是实时通话的音画同步测试。"}, 240))
        out["say"] = {"elapsed": round(time.time() - t0, 1), "text": say[:2000]}
        print("[say] %.1fs" % out["say"]["elapsed"], say[:300].replace("\n", " "))
    except Exception as e:
        out["say_error"] = type(e).__name__ + ": " + str(e)[:300]
        print("[say] ERR", out["say_error"])

    time.sleep(3)
    try:
        st, hdr, body = get(BASE + live, raw=True)
        txt = body.decode("utf-8", "replace")
        segs = [l.strip() for l in txt.splitlines()
                if l.strip() and not l.startswith("#")]
        out["after_say"] = {"segments": len(segs), "endlist": "#EXT-X-ENDLIST" in txt,
                            "head": txt[:500]}
        print("[after say] segs=%d endlist=%s" % (len(segs),
                                                  out["after_say"]["endlist"]))
        if segs:
            base_dir = live.rsplit("/", 1)[0]
            st, hdr, data = get(BASE + base_dir + "/" + segs[-1], raw=True)
            out["after_say"]["seg_streams"] = ts_streams(data)
            out["after_say"]["seg_bytes"] = len(data)
            print("[after say] seg", len(data), "streams", out["after_say"]["seg_streams"])
    except Exception as e:
        out["after_say_error"] = type(e).__name__ + ": " + str(e)[:200]
        print("[after say] ERR", out["after_say_error"])

    # 自我介绍 mp4 音轨
    try:
        st, hdr, data = get(BASE + "/conring/asr/assets/intro/ceo.mp4", raw=True)
        out["intro_mp4"] = {"status": st, "ctype": hdr.get("Content-Type"),
                            "bytes": len(data), "tracks": mp4_tracks(data)}
        print("[intro mp4]", out["intro_mp4"]["bytes"], out["intro_mp4"]["tracks"])
    except Exception as e:
        out["intro_mp4_error"] = type(e).__name__ + ": " + str(e)[:200]
        print("[intro mp4] ERR", out["intro_mp4_error"])

    # 挂断
    try:
        out["stop"] = text_of(p.call(ASR, sid, "live_call",
                                     {"action": "stop", "session_id": start["session_id"]}, 120))
        print("[stop]", out["stop"][:200].replace("\n", " "))
    except Exception as e:
        out["stop_error"] = type(e).__name__ + ": " + str(e)[:200]
        print("[stop] ERR", out["stop_error"])

    with open(os.path.join(DOCS, "v25_live_deep.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("SAVED -> docs/v25_live_deep.json")


if __name__ == "__main__":
    main()
