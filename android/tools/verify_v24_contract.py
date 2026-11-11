"""v2.4 契约校验：自我介绍流式播放 + 实时视频通话（客户端侧）。

检查三层：
  1) Java 纯逻辑实测（javac + java）：同源解析、双重前缀、外链拒绝、HLS 判定、重试 URL
  2) 契约规则断言：call_start 多种返回形态都能取到同源 live 地址
  3) 静态一致性：字段/工具常量/Activity 注册/布局 id/版本/零依赖/无新增外链

服务端 2026-11-10 实测能力快照见 docs/v24_server_contract.json。
输出 docs/v24_contract_check.json。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "app", "src", "main", "java", "com", "boray", "voicemcp")
RES = os.path.join(ROOT, "app", "src", "main", "res")
DOCS = os.path.join(ROOT, "docs")
MCP = os.path.join(SRC, "McpConfig.java")
GRADLE = os.path.join(ROOT, "app", "build.gradle")

results = []


def check(name, ok, detail=""):
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" | " + detail) if detail and not ok else ""))
    return bool(ok)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def has(path, *tokens):
    if not os.path.isfile(path):
        return False
    t = read(path)
    return all(x in t for x in tokens)


def find_jdk():
    env = os.environ.get("JAVA_HOME", "")
    if env and os.path.isfile(os.path.join(env, "bin", "javac.exe")):
        return env
    if env and os.path.isfile(os.path.join(env, "bin", "javac")):
        return env
    local = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp", "voice-mcp-jdk17")
    for d in sorted(glob.glob(os.path.join(local, "jdk-17*")), reverse=True):
        if os.path.isfile(os.path.join(d, "bin", "javac.exe")):
            return d
    which = shutil.which("javac")
    return os.path.dirname(os.path.dirname(which)) if which else ""


def find_android_jar():
    candidates = []
    lp = os.path.join(ROOT, "local.properties")
    if os.path.isfile(lp):
        for line in read(lp).splitlines():
            if line.startswith("sdk.dir="):
                candidates.append(line.split("=", 1)[1].strip())
    if os.environ.get("ANDROID_HOME"):
        candidates.append(os.environ["ANDROID_HOME"])
    for sdk in candidates:
        for jar in sorted(glob.glob(os.path.join(sdk, "platforms", "android-*", "android.jar"))):
            if os.path.isfile(jar):
                return jar
    return ""


def run_java_checks():
    jdk = find_jdk()
    jar = find_android_jar()
    if not jdk or not jar:
        return check("Java 纯逻辑校验环境可用", False,
                     "jdk=%r android.jar=%r" % (jdk, jar))
    check("Java 纯逻辑校验环境可用", True)
    javac = os.path.join(jdk, "bin", "javac.exe")
    java = os.path.join(jdk, "bin", "java.exe")
    if not os.path.isfile(javac):
        javac = os.path.join(jdk, "bin", "javac")
        java = os.path.join(jdk, "bin", "java")
    with tempfile.TemporaryDirectory(prefix="v24verify-") as tmp:
        sources = [os.path.join(SRC, x) for x in
                   ("AssetUrls.java", "McpConfig.java", "StreamContract.java")]
        sources.append(os.path.join(ROOT, "tools", "VerifyStream.java"))
        cp = jar
        build = subprocess.run(
            [javac, "-encoding", "UTF-8", "-cp", cp, "-d", tmp] + sources,
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("VerifyStream 编译通过", build.returncode == 0,
              (build.stderr or "")[:400])
        if build.returncode != 0:
            return
        cp_run = tmp + os.pathsep + jar
        run = subprocess.run([java, "-cp", cp_run, "VerifyStream"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (run.stdout or "") + (run.stderr or "")
        check("VerifyStream 全部断言通过", run.returncode == 0, out[-500:])
        for line in out.splitlines():
            if line.strip().startswith("PASS") or line.strip().startswith("FAIL"):
                print("       " + line.strip())


# ---------------------------------------------------------------- 契约规则（等价断言）
URL_KEYS = ("stream_url", "hls_url", "playback_url", "play_url",
            "live_url", "m3u8", "stream")
ORIGIN = "https://ai.boraydata.com"


def pick_stream(obj):
    scopes = [obj] + [obj.get(k) for k in ("data", "result", "session", "stream", "call")]
    for o in scopes:
        if not isinstance(o, dict):
            continue
        for k in URL_KEYS:
            v = o.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict) and isinstance(v.get("url"), str):
                return v["url"].strip()
    return ""


def same_origin(spec):
    if spec.startswith("/"):
        return ORIGIN + spec
    if spec.startswith(ORIGIN + "/"):
        return spec
    return ""


def contract_rules():
    samples = [
        ("扁平标准返回", {"session_id": "s1", "stream_url": "/conring/asr/live/s1.m3u8",
                          "audio_muxed": True, "segment_ms": 1000}),
        ("data 嵌套", {"data": {"session_id": "s2", "hls_url": "/conring/asr/live/s2.m3u8"}}),
        ("stream 对象", {"session": {"id": "s3"},
                         "stream": {"url": "/conring/asr/live/s3.m3u8"}}),
        ("playback_url 别名", {"playback_url": ORIGIN + "/conring/asr/live/s4.m3u8"}),
    ]
    for name, obj in samples:
        spec = pick_stream(obj)
        url = same_origin(spec)
        check("call_start 形态：" + name,
              bool(url) and url.startswith(ORIGIN + "/") and url.endswith(".m3u8"),
              "spec=%r url=%r" % (spec, url))
    check("call_start 外链返回必须被拒绝",
          same_origin(pick_stream({"stream_url": "https://cdn.other.com/live.m3u8"})) == "")
    check("profile 字段别名覆盖驼峰与蛇形",
          has(os.path.join(SRC, "Employee.java"),
              "intro_video", "introVideo", "intro_stream", "call_stream", "callStream"))
    check("自我介绍视频与通话流都经同源校验",
          has(os.path.join(SRC, "IntroPlayerActivity.java"), "StreamContract.resolveStream")
          and has(os.path.join(SRC, "LiveCallActivity.java"), "StreamContract.resolveStream"))


# ---------------------------------------------------------------- 静态一致性
def static_checks():
    java_new = ["StreamContract.java", "StreamPlayback.java",
                "IntroPlayerActivity.java", "LiveCallActivity.java"]
    for f in java_new:
        check("源码存在 " + f, os.path.isfile(os.path.join(SRC, f)))
    for f in ("activity_intro_player.xml", "activity_live_call.xml"):
        check("布局存在 " + f, os.path.isfile(os.path.join(RES, "layout", f)))

    check("Employee 新增 v2.4 字段与方法",
          has(os.path.join(SRC, "Employee.java"), "introVideo", "introAudio", "introText",
              "introPoster", "callStream", "hasIntro", "hasIntroVideo",
              "introCaption", "streamLine"))
    check("StreamContract 工具常量齐全",
          has(os.path.join(SRC, "StreamContract.java"), "call_start", "live_call_start",
              "call_turn", "call_stop", "voice_chat", "parseCallStart", "replyText"))
    check("StreamPlayback 同时支持 VOD 与 LIVE 且带音轨",
          has(os.path.join(SRC, "StreamPlayback.java"), "Mode.VOD", "Mode.LIVE",
              "setVolume", "setVideoURI", "LIVE_MAX_ATTEMPTS"))
    check("自我介绍页：流式播放 + 服务端未就绪降级",
          has(os.path.join(SRC, "IntroPlayerActivity.java"),
              "StreamPlayback.Mode.VOD", "PreviewAudio.synthesize",
              "等待服务端生成", "ReportActivity.EXTRA_AUDIO"))
    check("通话页：call_start/turn/stop + live 播放 + 上行语音 + 降级",
          has(os.path.join(SRC, "LiveCallActivity.java"),
              "listTools", "StreamPlayback.Mode.LIVE", "WavRecorder",
              "ListenService.addJob", "enterDegraded", "RECORD_AUDIO"))
    check("档案页两个入口与状态提示",
          has(os.path.join(SRC, "EmployeeDetailActivity.java"),
              "btnIntro", "btnCall", "tvStreamState", "emp.streamLine()"))

    manifest = read(os.path.join(ROOT, "app", "src", "main", "AndroidManifest.xml"))
    check("Manifest 注册自我介绍页", ".IntroPlayerActivity" in manifest)
    check("Manifest 注册实时通话页", ".LiveCallActivity" in manifest)
    strings = read(os.path.join(RES, "values", "strings.xml"))
    check("新增页面标题文案", "intro_player_title" in strings and "live_call_title" in strings)

    for f, ids in (("activity_intro_player.xml",
                    ("videoStage", "ivPoster", "tvState", "tvCaption", "tvSource",
                     "tvTime", "tvNotice", "pbProgress", "btnReplay", "btnFallback", "btnClose")),
                   ("activity_live_call.xml",
                    ("videoStage", "ivPoster", "tvCallState", "tvNetState", "tvCaption",
                     "tvNotice", "tvProtocol", "btnStart", "btnTalk", "btnEnd"))):
        t = read(os.path.join(RES, "layout", f))
        missing = [i for i in ids if ("@+id/" + i) not in t]
        check("布局 id 齐全 " + f, not missing, "missing=" + repr(missing))

    gradle = read(GRADLE)
    mvn = re.search(r'def appVersionName\s*=\s*"([^"]+)"', gradle)
    code = re.search(r'def appVersionCode\s*=\s*(\d+)', gradle)
    cfgv = re.search(r'CLIENT_VERSION\s*=\s*"([^"]+)"', read(MCP))
    check("版本号三处一致 0.4.0 / 6",
          bool(mvn and code and cfgv) and mvn.group(1) == cfgv.group(1) == "0.4.0"
          and code.group(1) == "6",
          "gradle=%s/%s cfg=%s" % (mvn.group(1) if mvn else "?",
                                   code.group(1) if code else "?",
                                   cfgv.group(1) if cfgv else "?"))
    check("仍然零第三方依赖",
          bool(re.search(r"dependencies\s*\{\s*\}", gradle)))

    external = []
    for f in java_new:
        t = read(os.path.join(SRC, f))
        for u in re.findall(r'https?://[^\s"\']+', t):
            external.append((f, u))
    check("新增源码不引入任何第三方 URL", not external, repr(external[:3]))

    snap = os.path.join(DOCS, "v24_server_contract.json")
    if os.path.isfile(snap):
        data = json.loads(read(snap))
        asr = data.get("endpoints", {}).get("asr", {}).get("tools", [])
        names = [t.get("name") for t in asr]
        check("服务端能力快照已记录（当前无 call_* 工具）",
              "profile" in names and not any(
                  str(n).startswith("call_") for n in names),
              "tools=" + repr(names))
    else:
        check("服务端能力快照已记录", False, "缺少 docs/v24_server_contract.json")


def main():
    print("=== 1. Java 纯逻辑实测 ===")
    run_java_checks()
    print("=== 2. 契约规则断言 ===")
    contract_rules()
    print("=== 3. 静态一致性 ===")
    static_checks()
    ok = sum(1 for r in results if r["ok"])
    bad = len(results) - ok
    out = {"checked_at": "2026-11-10", "version": "0.4.0",
           "total": len(results), "pass": ok, "fail": bad, "checks": results}
    path = os.path.join(DOCS, "v24_contract_check.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("CONTRACT: %d PASS / %d FAIL -> docs/v24_contract_check.json" % (ok, bad))
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
