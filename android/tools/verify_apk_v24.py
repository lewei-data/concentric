"""校验 0.4.0 debug/release 双包内容：新 Activity、新类、新资源、ZIP 完整性与哈希。

输出 docs/v24_apk_check.json。版本/包名/label 另由 aapt2 dump badging 复核。
"""
import hashlib
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
DOCS = os.path.join(ROOT, "docs")

ACTIVITIES = ("IntroPlayerActivity", "LiveCallActivity")
CLASSES = ("StreamContract", "StreamPlayback", "IntroPlayerActivity",
           "LiveCallActivity", "LipSync", "Employee")
LAYOUTS = ("activity_intro_player.xml", "activity_live_call.xml")

rows = []
checks = []


def check(name, ok, detail=""):
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" | " + detail) if detail and not ok else ""))


def present(data, word):
    return word.encode() in data or word.encode("utf-16-le") in data


def main():
    for kind in ("debug", "release"):
        name = "voice-mcp-v0.4.0-b6-%s.apk" % kind
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

        check(kind + " 文件名带版本号", "v0.4.0-b6" in name)
        for a in ACTIVITIES:
            check(kind + " Manifest 注册 " + a, present(manifest, a))
        for c in CLASSES:
            sig = ("Lcom/boray/voicemcp/" + c + ";").encode()
            check(kind + " dex 含 " + c, sig in dex)
        for lay in LAYOUTS:
            stem = lay[:-4]
            # release 开启 optimizeReleaseResources 后文件名会被混淆成 res/xx.xml，
            # 但 resources.arsc 里的资源条目名保留，因此以 arsc 为准。
            hit = present(arsc, stem) or any(lay in n for n in names)
            check(kind + " 资源含 " + stem, hit)
        check(kind + " 应用名为同心圆", present(arsc, "同心圆"))
        check(kind + " 内置版本资源 0.4.0", present(arsc, "0.4.0"))
        check(kind + " ZIP 完好", z.testzip() is None)
        # AGP 8.x 默认只打 v2/v3（APK Signing Block），META-INF 里没有 .RSA/.SF；
        # v1(JAR) 签名才会在 META-INF 留文件，因此以 signing block magic 为准。
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
    out = {"version": "0.4.0", "versionCode": 6, "total": len(checks),
           "pass": ok, "fail": bad, "apks": rows, "checks": checks}
    with open(os.path.join(DOCS, "v24_apk_check.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("APK CHECK: %d PASS / %d FAIL" % (ok, bad))
    for r in rows:
        print("  %s  %d B  sha256=%s" % (r["file"], r["bytes"], r["sha256"]))
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
