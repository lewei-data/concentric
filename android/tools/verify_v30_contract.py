"""0.9.6 静态契约校验（不联网）：调度中心双地址 + WS 协议 + 流式播放 + UI 分层。

检查的是「源码里有没有按服务端契约实现」，与 VerifyDispatch（解析行为）互补。
输出 docs/v30_contract_check.json，失败返回非 0。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "app", "src", "main", "java", "com", "boray", "voicemcp")
RES = os.path.join(ROOT, "app", "src", "main", "res", "layout")
DOCS = os.path.join(ROOT, "docs")

checks = []


def read(name, base=SRC):
    p = os.path.join(base, name)
    if not os.path.isfile(p):
        return ""
    return open(p, encoding="utf-8").read()


def check(name, ok, detail=""):
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" | " + detail) if detail and not ok else ""))


def has(text, *needles):
    return all(n in text for n in needles)


def main():
    dispatch = read("DispatchApi.java")
    ws = read("WsClient.java")
    channel = read("RealtimeChannel.java")
    player = read("PcmPlayer.java")
    wav = read("WavStream.java")
    svc = read("VoiceCallService.java")
    reply = read("CallReply.java")
    config = read("McpConfig.java")
    main_xml = read("activity_main.xml", RES)
    call_xml = read("activity_voice_call.xml", RES)

    print("== A. 网络层：公网优先 + 内网 fallback ==")
    check("公网 dispatch 地址", "https://ai.boraydata.com/conring/dispatch" in dispatch)
    check("内网 HTTP 9120", "http://192.168.31.206:9120" in dispatch)
    check("内网 WS 9121", "ws://192.168.31.206:9121" in dispatch)
    check("按上一次成功线路排序", "preferred == 2" in dispatch and "routes(" in dispatch)
    check("公网失败切内网", dispatch.count("for (String[] r : routes") >= 1 and "/submit" in dispatch)
    check("任务查询 /task/", '"/task/"' in dispatch)
    check("任务取消 /cancel", "/cancel" in dispatch)
    check("/health 探活", "/health" in dispatch)
    check("双地址可在设置里覆盖", has(config, "dispatchUrl", "dispatchUrlLan", "dispatchWsLan"))

    print("== B. submit 请求体 ==")
    check("显式 channel=ws", 'body.put("channel", channel)' in dispatch
          and '"ws"' in dispatch)
    check("source=boss", 'body.put("source", "boss")' in dispatch)
    check("modality 可传 voice/video", 'body.put("modality", modality' in dispatch)
    check("text 非空兜底", '"语音通话"' in dispatch)
    check("解析 channel_kind", "channel_kind" in dispatch)
    check("realtime_url 模板替换", 'replace("<channel_id>"' in dispatch)
    check("非同源/非 ws 协议拒绝", 'startsWith("ws://")' in dispatch and 'startsWith("wss://")' in dispatch)

    print("== C. WS 协议（RFC6455 + 业务帧）==")
    check("握手 key", "Sec-WebSocket-Key" in ws)
    check("版本 13", "Sec-WebSocket-Version: 13" in ws)
    check("101 校验", "101" in ws)
    check("Accept 计算含 GUID", "258EAFA5-E914-47DA-95CA-C5AB0DC85B11" in ws)
    check("客户端帧加掩码", "0x80 | len" in ws or "0x80|len" in ws.replace(" ", ""))
    check("二进制帧 0x2", "OP_BIN = 0x2" in ws)
    check("自动回 pong", "OP_PONG" in ws and "OP_PING" in ws)
    check("分片续帧 0x0", "0x0" in ws and "frag" in ws)
    check("wss 证书域名校验", 'setEndpointIdentificationAlgorithm("HTTPS")' in ws)
    check("SNI", "SNIHostName" in ws)
    check("hello 帧", '"{\\"type\\":\\"hello\\"}"' in channel)
    check("audio_end 帧", 'type\\":\\"audio_end' in channel)
    check("ping 帧", 'type\\":\\"ping' in channel)
    check("close 帧", 'type\\":\\"close' in channel)
    check("session_started/hello_ok", "session_started" in channel and "hello_ok" in channel)
    check("transcript", "transcript" in channel)
    check("reply / reply_end", '"reply"' in channel and "reply_end" in channel)
    check("barge_in", "barge_in" in channel)
    check("bye / timeout / error", has(channel, '"bye"', '"timeout"', '"error"'))
    check("4404 视为会话结束", "4404" in channel)
    # 0.12.0：服务端 REATTACH_GRACE 已从 30s 改到 300s，客户端重连窗口同步
    check("300s 内原 channel 重连", "GRACE_MS" in channel and "300_000" in channel)
    check("心跳 12s", "PING_MS = 12_000" in channel)
    check("上行分块 3200B/100ms", "AUDIO_CHUNK = 3200" in channel and "sleep(20)" in channel)

    print("== D. 下行音频：多段 wav + 流式播放 ==")
    check("按 chunk 找 data（不假设 44B 头）", "findDataOffset" in wav and "LIST/INFO" in wav)
    check("多段 wav 边界识别", "isRiff(data, 0)" in wav)
    check("首段也回调格式", "configured" in wav)
    check("采样率变化重配", "sameAs(format)" in wav)
    check("AudioTrack 流式", "MODE_STREAM" in player)
    check("打断 pause+flush", "track.pause()" in player and "track.flush()" in player)
    check("打断后静音闸门", "muted" in player and "if (muted) return;" in player)
    check("电平用 PCM 算", "WavStream.level" in player)

    print("== E. 文稿分层 / 附件 ==")
    check("voice_text 与 text 分离", has(reply, "voiceText", "detail"))
    check("text 相同则不重复展示", "hasExtraDetail" in reply)
    check("attachments 只收有 url 的", "attachments" in reply and "url.isEmpty()" in reply)
    panel = read("SessionPanel.java")
    check("只允许 http(s) 打开", 'startsWith("http://")' in panel)
    check("附件胶囊挂在会话行下", "addLinks" in panel and "bg_badge" in panel)
    check("通话页详情+附件", "tvDetail" in call_xml and "llAtts" in call_xml)
    check("详情行单独样式", "KIND_DETAIL" in svc and "KIND_DETAIL" in panel)

    print("== F. 通话链路选择与兜底 ==")
    check("WS 主链路", '"ws".equals(transport)' in svc and "startDispatchChannel" in svc)
    check("失败回退 HLS", "connectHls" in svc and 'transport = "hls"' in svc)
    check("连接中挂断不再回退", "if (!active) {" in svc)
    check("dispatch 重试上限", "dispatchTries >= 3" in svc)
    check("本地抢先打断", "wsPlayer.stopAndFlush()" in svc)
    check("挂断发 close", "ch.close()" in svc)
    check("回复解除静音", "setMuted(false)" in svc)
    check("挂断清详情", "lastReply = null" in svc)

    print("== G. 版本与包 ==")
    g = open(os.path.join(ROOT, "app", "build.gradle"), encoding="utf-8").read()
    ver = re.search(r'def appVersionName\s*=\s*"([^"]+)"', g).group(1)
    code = int(re.search(r'def appVersionCode\s*=\s*(\d+)', g).group(1))
    check("CLIENT_VERSION 与构建版本一致", ('CLIENT_VERSION = "%s"' % ver) in config, ver)
    check("versionCode ≥ 30", code >= 30, str(code))

    ok = sum(1 for c in checks if c["ok"])
    bad = len(checks) - ok
    out = {"version": ver, "versionCode": code, "total": len(checks),
           "pass": ok, "fail": bad, "checks": checks}
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "v30_contract_check.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("CONTRACT 0.9.6: %d PASS / %d FAIL" % (ok, bad))
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
