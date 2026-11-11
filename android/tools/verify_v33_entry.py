"""0.11.0 静态契约：电话指令入口统一到 dispatch，live_call 只当渲染层。

背景：服务端 `voice_chat` 是直接 `run_hermes()`，绕过调度中心 —— 没有 P0 任务、
没有 utterances/events 台账、没有 lane 分配、通话摘要不进 Obsidian。
这轮把「语音（HLS 兜底）」与「视频」两条链路都改成：
  dispatch（WS 优先 / passthrough 兜底）出文字 → live_call say 渲染出声画。
输出 docs/v33_entry_check.json。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "app", "src", "main", "java", "com", "boray", "voicemcp")
DOCS = os.path.join(ROOT, "docs")

checks = []


def read(name):
    p = os.path.join(SRC, name)
    return open(p, encoding="utf-8").read() if os.path.isfile(p) else ""


def check(name, ok, detail=""):
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" | " + detail) if detail and not ok else ""))


def main():
    svc = read("VoiceCallService.java")
    live = read("LiveCallActivity.java")
    api = read("DispatchApi.java")
    chan = read("RealtimeChannel.java")

    print("== 1. 客户端不再用 voice_chat 下指令 ==")
    check("语音服务不再调 voice_chat", "TOOL_VOICE_CHAT" not in svc)
    check("语音服务不再轮询 voice_job", "TOOL_VOICE_JOB" not in svc)
    check("视频页不再调 voice_chat", "TOOL_VOICE_CHAT" not in live)
    check("视频页不再轮询 voice_job", "TOOL_VOICE_JOB" not in live)

    print("== 2. dispatch 客户端能力 ==")
    check("submit 支持 modality/channel", "String modality, String channel" in api)
    check("channel=null 走 passthrough", 'if (channel != null && !channel.isEmpty()) body.put("channel", channel);' in api)
    check("submit 仍带 source=boss", 'body.put("source", "boss")' in api)
    check("一轮式 ask（提交+等结果）", "public static String ask(" in api and "/task/" in api)
    check("任务结果字段", 't.result = j.optString("result", "")' in api)

    print("== 3. 语音：WS 主链路 + HLS 兜底都进 dispatch ==")
    check("语音主链路是 dispatch WS", "startDispatchChannel" in svc and "DispatchApi.submit(" in svc)
    check("HLS 兜底用 dispatch.ask", "DispatchApi.ask(this, profile" in svc)
    check("拿到文字后用 live_call say 渲染", "private void say(String text)" in svc
          and 'args.put("action", StreamContract.ACTION_SAY)' in svc)
    check("兜底先本地 ASR 再提交", "private String asrText(byte[] wav)" in svc)
    check("开场白复用 say()", "say(hello)" in svc)

    print("== 4. 视频：dispatch 出文字，HLS 渲染口型 ==")
    check("视频接通后建实时通道", "startDialog()" in live and "RealtimeChannel ch = new RealtimeChannel(s, urls, dialogListener)" in live)
    check("视频通道 modality=video", 'DispatchApi.submit(app, profile, "", "视频通话接通", "video", "ws")' in live)
    check("视频回复用 say 渲染", "private void say(String text)" in live
          and "say(spoken)" in live)
    check("视频通道不可用时退回 dispatch.ask", "DispatchApi.ask(app, emp.id, \"video\"" in live)
    check("视频兜底先 ASR", "private String asrText(byte[] wav)" in live)
    check("通道断开后清 submit 标记", "dialogSub = null;" in live)

    print("== 5. 多轮并行与主动话术 ==")
    reply = read("CallReply.java")
    check("解析 turn / greeting", "public int turn = 0;" in reply and "greeting" in reply)
    check("按轮次排队再回调", "pendingReplies" in chan and "enqueueReply" in chan)
    check("缺号 300ms 兜底补发", "postDelayed(flushReplies, 300)" in chan)
    check("视频跳过服务端重复问候", "reply.turn == 0 && reply.greeting" in live)

    print("== 5.1 公网链路健壮性（防「假连接」） ==")
    check("握手后等 session_started 才算就绪", "READY_WAIT_MS" in chan and "readyLatch" in chan)
    check("假连接会被关掉并试下一个地址", "handshake ok but no session_started" in chan)
    check("上行独立线程（不被重连阻塞）", "senderIo" in chan and "sendUtteranceNow" in chan)
    check("dispatch 失败原因可见", "dispatchError" in svc)
    probe = open("tools/WsProbe.java", encoding="utf-8").read()
    check("探针忽略 turn=0 的 reply_end", "turn\\\": 0" in probe and "reply_end" in probe)
    check("上行整句单帧（不分片）", "AUDIO_MAX_FRAME" in chan and "sendBinary(data, 0, data.length)" in chan)
    check("推流期间不发 ping", "uplinkBusy" in chan and "else if (!uplinkBusy)" in chan)
    check("先停上行再发 close", "senderIo.shutdownNow()" in chan)
    check("4409 被接管不再重连", "code == 4409" in chan)
    check("挂断等 bye 再断", "byeSeen" in chan)
    check("单 socket（连新前关旧）", "WsClient prev = ws;" in chan)

    print("== 6. 台账/交付物链路存在 ==")
    check("WS 帧协议（hello/audio_end）", 'type\\":\\"hello' in chan and "audio_end" in chan)
    check("实时通道 client 有 transcript/reply", "transcript" in chan and '"reply"' in chan)

    print("== 6. 版本 ==")
    g = open(os.path.join(ROOT, "app", "build.gradle"), encoding="utf-8").read()
    ver = re.search(r'def appVersionName\s*=\s*"([^"]+)"', g).group(1)
    code = int(re.search(r'def appVersionCode\s*=\s*(\d+)', g).group(1))
    cfg = read("McpConfig.java")
    check("版本 ≥ 0.11.2", tuple(map(int, ver.split("."))) >= (0, 11, 0), ver)
    check("versionCode ≥ 39", code >= 37, str(code))
    check("CLIENT_VERSION 同步", ('CLIENT_VERSION = "%s"' % ver) in cfg)

    ok = sum(1 for c in checks if c["ok"])
    bad = len(checks) - ok
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "v33_entry_check.json"), "w", encoding="utf-8") as f:
        json.dump({"version": ver, "versionCode": code, "total": len(checks),
                   "pass": ok, "fail": bad, "checks": checks},
                  f, ensure_ascii=False, indent=2)
    print("ENTRY: %d PASS / %d FAIL" % (ok, bad))
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
