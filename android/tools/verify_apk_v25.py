"""校验 debug/release 双包内容：新 Activity、新类、契约字符串、资源、
ZIP 完整性与哈希。版本号从 app/build.gradle 读取，升版无需改脚本。

输出 docs/v30_apk_check.json（0.9.6 起：调度中心 + WS 实时通道）。
"""
import hashlib
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
DOCS = os.path.join(ROOT, "docs")
GRADLE = os.path.join(ROOT, "app", "build.gradle")

# 版本号以 build.gradle 为唯一真源，升级版本不用改校验脚本
_g = open(GRADLE, encoding="utf-8").read()
VERSION = re.search(r'def appVersionName\s*=\s*"([^"]+)"', _g).group(1)
CODE = int(re.search(r'def appVersionCode\s*=\s*(\d+)', _g).group(1))
TAG = "v%s-b%d" % (VERSION, CODE)

ACTIVITIES = ("IntroPlayerActivity", "LiveCallActivity", "ProgressActivity",
              "TodoActivity", "VoiceCallActivity")
CLASSES = ("StreamContract", "StreamPlayback", "IntroPlayerActivity",
           "LiveCallActivity", "CoverVideoView", "LiveMic", "Briefing",
           "ProgressActivity", "TodoActivity", "VoiceCallService",
           "VoiceCallActivity", "VoiceLevelView", "LiveLevelMeter", "SpokenTextSync", "CallTone", "ScreenOn",
           "LipSync", "Employee",
           # 0.9.6 新增：调度中心 + 零依赖 WS + 流式 WAV 播放
           "DispatchApi", "WsClient", "RealtimeChannel", "PcmPlayer",
           "WavStream", "CallReply",
           # 0.9.7：会话记录（SQLite）+ 可拖拽面板
           "SessionStore", "SessionPanel")
LAYOUTS = ("activity_intro_player.xml", "activity_live_call.xml",
           "activity_employee_detail.xml", "activity_progress.xml",
           "activity_todo.xml", "activity_voice_call.xml", "view_session_panel.xml")
# v2.5 服务端契约常量：必须真的进了 dex
CONTRACT_STRINGS = ("live_call", "intro_video",
                    "first_segment_sec", "segment_sec", "hls_url", "session_id",
                    "boss_briefing", "secretary", "audio",
                    # 0.9.6 dispatch / WS 契约
                    "/conring/dispatch", "realtime_url", "channel_kind",
                    "voice_text", "attachments", "barge_in", "audio_end",
                    "session_started", "reply_end", "ws://192.168.31.206:9121",
                    # 0.9.7 会话记录
                    "conring_sessions.db", "messages", "links_json", "day",
                    # 0.11.0：电话指令统一进 dispatch
                    "DispatchApi$Submit", "/channel/")
OLD_CONTRACT_STRINGS = ("call_start", "call_turn", "call_stop")

rows = []
checks = []


def check(name, ok, detail=""):
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  PASS  " if ok else "  FAIL  ") + name
          + ((" | " + detail) if detail and not ok else ""))


def present(data, word):
    return word.encode() in data or word.encode("utf-16-le") in data


def main():
    for kind in ("debug", "release"):
        name = "voice-mcp-%s-%s.apk" % (TAG, kind)
        path = os.path.join(DIST, name)
        if not os.path.isfile(path):
            check(kind + " APK 存在", False, path)
            continue
        raw = open(path, "rb").read()
        z = zipfile.ZipFile(path)
        names = z.namelist()
        manifest = z.read("AndroidManifest.xml")
        dex = b"".join(z.read(n) for n in names if n.endswith(".dex"))
        arsc = z.read("resources.arsc") if "resources.arsc" in names else b""

        check(kind + " 文件名带版本号 " + TAG, TAG in name)
        for a in ACTIVITIES:
            check(kind + " Manifest 注册 " + a, present(manifest, a))
        for c in CLASSES:
            sig = ("Lcom/boray/voicemcp/" + c + ";").encode()
            check(kind + " dex 含 " + c, sig in dex)
        for lay in LAYOUTS:
            stem = lay[:-4]
            # release 开启 optimizeReleaseResources 后文件名被混淆成 res/xx.xml，
            # 资源条目名保留在 resources.arsc 里，因此以 arsc 为准。
            hit = present(arsc, stem) or any(lay in n for n in names)
            check(kind + " 资源含 " + stem, hit)
        check(kind + " 应用名为同心圆", present(arsc, "同心圆"))
        check(kind + " 内置版本资源 " + VERSION, present(arsc, VERSION))

        # 详情页首屏铺满播放介绍视频：布局里必须有 CoverVideoView
        xmls = b"".join(z.read(n) for n in names
                        if n.endswith(".xml") and n != "AndroidManifest.xml")
        check(kind + " 布局使用 CoverVideoView 铺满", present(xmls, "CoverVideoView"))

        for s in CONTRACT_STRINGS:
            check(kind + " dex 含契约串 " + s, present(dex, s))
        stale = [s for s in OLD_CONTRACT_STRINGS if present(dex, s)]
        check(kind + " 不含旧 call_* 契约", not stale, repr(stale))

        check(kind + " ZIP 完好", z.testzip() is None)
        # AGP 8.x 默认只打 v2/v3（APK Signing Block），META-INF 里没有 .RSA/.SF。
        jar_signed = any(n.startswith("META-INF/") and
                         (n.endswith(".RSA") or n.endswith(".DSA") or n.endswith(".SF"))
                         for n in names)
        v2_signed = b"APK Sig Block 42" in raw
        check(kind + " 已签名", jar_signed or v2_signed)
        rows.append({"type": kind, "file": name, "bytes": len(raw),
                     "sha256": hashlib.sha256(raw).hexdigest()})
        z.close()

    ok = sum(1 for c in checks if c["ok"])
    bad = len(checks) - ok
    out = {"version": VERSION, "versionCode": CODE, "total": len(checks),
           "pass": ok, "fail": bad, "apks": rows, "checks": checks}
    with open(os.path.join(DOCS, "v30_apk_check.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("APK CHECK: %d PASS / %d FAIL" % (ok, bad))
    for r in rows:
        print("  %s  %d B  sha256=%s" % (r["file"], r["bytes"], r["sha256"]))
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
