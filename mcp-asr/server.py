#!/usr/bin/env python3
"""MCP-ASR：语音指令入口 + 异步结果播报 + 数字人播报档案。

链路（异步两段式）：
  1) 客户端音频 --voice_chat--> ASR 文本 → 意图识别 → 立即返回「收到」语音 + job_id
  2) 客户端 --voice_job-------> 轮询/长轮询取结果：简要语音 + 详细文字 + 音频/时间轴/数字人档案
     也可在 voice_chat 里给 callback_url，任务完成后由服务端主动推送结果 JSON。

要点：
  - 语音汇报（speech_text）与详细文字（detail_text）分开：语音只说一两句，详细资料交给客户端展示。
  - 语音里点名员工（@id / 姓名 / 工号）时，指令直接下发给该员工的 Hermes profile，由员工处理并回复。
  - 服务端音频为唯一音源：返回 wav(base64) + 采样率 + 句级时间轴，客户端解析振幅包络实时驱动口型/表情，
    不依赖预录视频；数字人档案支持「照片 + 仿真人视频素材」可插拔（assets 目录下按 id 放文件即可）。

协议为 MCP stdio：stdin/stdout 按行收发 JSON-RPC 2.0，状态日志一律写 stderr。
ASR 用 openai-whisper（本机已装），TTS 复用 9101 端口的 MCP-TTS 桥接，避免重复加载语音模型。
"""

import base64
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

SERVER_NAME = "asr"
SERVER_VERSION = "2.2.0"
PROTOCOL_VERSION = "2024-11-05"

TTS_URL = os.environ.get("MCP_TTS_URL", "http://127.0.0.1:9101/mcp")
HERMES_CLI = os.environ.get("HERMES_CLI", "/opt/sefs/hermes/run-hermes.sh")
HERMES_HOME_DIR = os.path.dirname(HERMES_CLI)
# 统一执行入口：auto=优先 gateway /v1/runs、不可用时回落子进程；gateway=只走网关；subprocess=只走子进程
HERMES_EXEC_MODE = (os.environ.get("HERMES_EXEC_MODE") or "auto").strip().lower()
# 非通话指令的会话空闲轮换时长：超过此时长（或跨日）换新会话，避免上下文无限增长
ASR_SESSION_IDLE = float(os.environ.get("ASR_SESSION_IDLE", "1800"))
try:  # 与 gateway 客户端解耦：模块缺失时自动只走子进程
    sys.path.insert(0, "/opt/sefs/hermes")
    import gw_runs_client as _GW
except Exception:  # noqa: BLE001 - 客户端不可用不应阻断服务启动
    _GW = None
DEFAULT_MODEL = os.environ.get("ASR_MODEL", "small")
DEFAULT_LANG = os.environ.get("ASR_LANG", "zh")
# 回复语音用真人参考音色（CosyVoice3 下长 prompt 不再失控，音色比 Piper 参考自然得多）
DEFAULT_VOICE = os.environ.get("ASR_TTS_VOICE", "female-zh-real")
MAX_TTS_CHARS = 2000
# 语音汇报只说重点：默认最多 ~90 字，避免语音过长影响体验
VOICE_SUMMARY_CHARS = int(os.environ.get("ASR_VOICE_SUMMARY_CHARS", "90"))
# 数字人档案：assets 目录下 avatars/<id>.png、faces/<id>.png、videos/<id>[.idle|.talk].mp4
ASSET_DIRS = [p for p in os.environ.get(
    "ASR_ASSET_DIRS", "/home/boray/opc-workbench/prototype/assets").split(os.pathsep) if p]
ASSET_URL_BASE = os.environ.get("ASR_ASSET_URL_BASE", "").rstrip("/")
# 直播流是直接丢给播放器打开的资源（不是给 App 自己拼地址的图片），必须给绝对地址，
# 否则 Android MediaPlayer 拿到 /conring/asr/assets/live/xx/index.m3u8 会当本地路径打开失败，
# 表现就是「通话已接通、一直没声音」
PUBLIC_BASE = os.environ.get("ASR_PUBLIC_BASE", "").rstrip("/")
# 生成物（自我介绍视频 / 实时通话分片）落到第一个素材目录，复用 /conring/asr/assets/* 公网通路
ASSET_WRITE_DIR = os.environ.get("ASR_ASSET_WRITE_DIR") or (ASSET_DIRS[0] if ASSET_DIRS else "")
INTRO_DIR = os.path.join(ASSET_WRITE_DIR, "intro") if ASSET_WRITE_DIR else ""
LIVE_DIR = os.path.join(ASSET_WRITE_DIR, "live") if ASSET_WRITE_DIR else ""
LIVE_SEGMENT_SEC = float(os.environ.get("ASR_LIVE_SEGMENT_SEC", "1"))
# 响度归一化：TTS 各句原始音量不一致，进流前统一拉到 -16 LUFS / -1.5 dBTP（手机外放才听得清）
LIVE_LOUDNORM = os.environ.get("ASR_LIVE_LOUDNORM", "1") not in ("0", "false", "False", "no")
LOUDNESS_I = os.environ.get("ASR_LOUDNESS_I", "-16")
LOUDNESS_TP = os.environ.get("ASR_LOUDNESS_TP", "-1.5")
# 归一化后再补的整体增益（限幅到 -1 dBFS）：手机外放常觉得小声，默认 +3 dB
LIVE_GAIN_DB = float(os.environ.get("ASR_LIVE_GAIN_DB", "3"))
_LIMITER = "alimiter=limit=0.891:level=disabled:attack=5:release=80"
LIVE_SESSIONS_MAX = int(os.environ.get("ASR_LIVE_SESSIONS_MAX", "8"))
LIVE_TTL = int(os.environ.get("ASR_LIVE_TTL", "900"))       # 挂断后保留秒数内的回放
# 通话空闲自动收线：App 被杀/断网时不会发 live_call stop，会话和 ffmpeg 会一直占着，
# 累计 8 条后新通话直接被拒（"server 不能接通"）。空闲超过这个秒数就由服务端挂断。
LIVE_HANGUP_SEC = float(os.environ.get("ASR_LIVE_HANGUP_SEC", "600"))
# 会话满时优先回收「空闲超过这么多秒」的通话，而不是直接拒绝新通话
LIVE_EVICT_IDLE = float(os.environ.get("ASR_LIVE_EVICT_IDLE", "120"))
# 带宽够就上数字人；单位 kbps，<=0 表示客户端未提供、按可用素材决定
VIDEO_MIN_KBPS = int(os.environ.get("ASR_VIDEO_MIN_KBPS", "600"))
# 任务完成后回调推送（客户端可在 voice_chat 里覆盖 callback_url）
PUSH_URL = os.environ.get("ASR_PUSH_URL", "")
JOB_TTL = int(os.environ.get("ASR_JOB_TTL", "7200"))
JOB_MAX = int(os.environ.get("ASR_JOB_MAX", "100"))
# 后台任务并发上限：Hermes 是重型子进程，突发多条指令时不能无限开线程，
# 超出的先入队（status=accepted，带 queue_ahead），等 worker 空闲再跑。
JOB_WORKERS = max(1, int(os.environ.get("ASR_JOB_WORKERS", "3")))
# 单条任务执行超时：gateway 超时会主动 stop run（等于把活废掉），
# 历史 p90≈260s，240s 会误杀约一成任务，所以默认放宽到 600s。
JOB_TIMEOUT = int(os.environ.get("ASR_JOB_TIMEOUT", "600"))
# 客户端 client_id → job_id 的幂等表保留时长（App 重发同一段录音时不重复执行）
CLIENT_TTL = int(os.environ.get("ASR_CLIENT_TTL", "86400"))
# 员工名册（唯一数据源，与 OPC 工作台一致）；用于语音点名派单
ROSTER_FILE = os.environ.get("OPC_ROSTER", "/opt/sefs/obsidian/公司/员工名册.md")
# 员工完整档案（部门/工号/简介/技能/口头禅/形象参数），字段与 App 的 employees.json 对齐
PROFILES_FILE = os.environ.get("ASR_PROFILES", "/opt/sefs/opc-workbench/mcp-asr/employees.json")
# OPC-001 小秘书（老板影子分身）：未点名指令的默认处理人，也是默认播报人
SECRETARY_ID = os.environ.get("ASR_SECRETARY", "secretary")
# 老板上线时让影子分身主动汇报的提示词（boss_briefing 工具）
BOSS_BRIEFING_PROMPT = (
    "老板刚上线。主动汇报近期发生的事：只读查阅 /opt/sefs/obsidian 下的 动态/运行时快照.md、"
    "动态/事件流.md、OPC/交付物/*/06-log.md 和收件箱未读邮件，梳理各员工任务状态与最近更新时间。"
    "按「一句话结论 → 进展 → 风险与异常 → 需要老板拍板」输出，第三、四部分每项给依据，"
    "待拍板事项给选项、你的建议和不决策的后果，必须老板拍板的标【急】并置顶。"
    "控制在老板 30 秒能读完的长度。没有记录就写没有记录，禁止编造进度、数据或来源。"
)
# 「收到」语音最长等多久合成：超时就先回文字，客户端可自行 TTS
ACK_TTS_TIMEOUT = float(os.environ.get("ASR_ACK_TTS_TIMEOUT", "8"))
# 实时通话里「收到」超时后不丢弃：后台继续合成（本机 TTS RTF 1.7~6.4，短句常要 10~16s），
# 合成完再补混进直播流，用户晚几秒也能听到，否则整通电话一句语音都没有
ACK_TTS_LATE = float(os.environ.get("ASR_ACK_TTS_LATE", "90"))
# —— 无语音 / 幻觉过滤 ——
# whisper 在静音、低电平噪声、极短录音上会吐训练语料里的片头片尾口径，中文最典型的是
# 「字幕by索兰娅」「请不吝点赞 订阅 转发 打赏支持…」。这些句子与用户说的内容无关，
# 一旦当成指令下发给 agent 就会被真的执行（本机已复现：4 秒粉噪 → 字幕by索兰娅）。
# 判定用「模型自带的置信度 + 已知口径黑名单 + 复读检测」三重，宁可判无语音也不要误执行。
ASR_NO_SPEECH_PROB = float(os.environ.get("ASR_NO_SPEECH_PROB", "0.8"))
ASR_MIN_AVG_LOGPROB = float(os.environ.get("ASR_MIN_AVG_LOGPROB", "-0.6"))
ASR_MAX_COMPRESSION = float(os.environ.get("ASR_MAX_COMPRESSION", "3.0"))
# 整段最响 20ms 帧的 RMS 低于此值＝没说话（0.002 ≈ -54 dBFS），直接返回空文本
ASR_MIN_RMS = float(os.environ.get("ASR_MIN_RMS", "0.002"))
# —— 低置信兜底 ——
# whisper 的 avg_logprob 是段内 token 的平均对数概率，e^avg_logprob 近似平均 token 置信度。
# 置信度低于阈值（或压根没识别出字）时不执行指令，改让大模型用员工口吻请老板重说。
ASR_LOW_CONFIDENCE = float(os.environ.get("ASR_LOW_CONFIDENCE", "0.5"))
# 澄清语是否交给大模型生成（关掉则用按情绪写死的模板句，响应更快）
ASR_CLARIFY_LLM = os.environ.get("ASR_CLARIFY_LLM", "1") not in ("0", "false", "False", "no")
ASR_CLARIFY_TIMEOUT = float(os.environ.get("ASR_CLARIFY_TIMEOUT", "20"))
# 是否用被点名员工的 Hermes profile 生成澄清语。默认关闭：员工 profile 是干活的 agent，
# 容易答非所问（实测回了一句「老板早！」），人设与情绪靠 prompt + 音色/韵律表达即可。
ASR_CLARIFY_PROFILE = os.environ.get("ASR_CLARIFY_PROFILE", "0") in ("1", "true", "True", "yes")
# 澄清语音是本轮唯一的回复（没有后台任务兜底），合成可以多等一会，别只回文字
ASR_CLARIFY_TTS_TIMEOUT = float(os.environ.get("ASR_CLARIFY_TTS_TIMEOUT", "30"))
_HALLUCINATION_PATTERNS = [re.compile(p, re.I) for p in (
    r"字幕\s*(?:by|from|由|：|:)\s*\S{0,20}",          # 字幕by索兰娅 / 字幕由XX翻译
    r"(?:请)?不吝?\s*点赞", r"点赞.{0,6}订阅", r"订阅.{0,6}转发",
    r"转发.{0,6}打赏", r"打赏.{0,6}支持", r"明镜|点点栏目",
    r"优优独播剧场|YYeTs|人人影视|Amara\.org",
)]
# 软口径只在「整段就是这句」时才算幻觉：真话里出现「把字幕翻译成英文」不能误杀
_SOFT_HALLUCINATION_PATTERNS = [re.compile(p, re.I) for p in (
    r"(?:字幕\s*)?(?:组|制作|提供|翻译|校对|压制|时间轴|后期|片源)\s*[：:]\s*\S{0,24}",
    r"字幕\s*(?:制作|提供|翻译|校对)",
    r"(?:谢谢|感谢)(?:大家)?(?:观看|收看|收听)\s*[。.!！]?",
    r"字幕组(?:\s*\S{0,12})?",
)]

_PROTO_OUT = sys.stdout
_MODELS = {}
_MODEL_LOCK = threading.Lock()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


_SEND_LOCK = threading.Lock()
# 并发上限：请求改为多线程处理（避免 wait=true 长时间阻塞把别的工具调用一起卡住），
# 但 whisper 推理本身串行，且总并发有上限，防止手机端重试风暴把机器打满。
_REQ_SEM = threading.BoundedSemaphore(int(os.environ.get("ASR_MAX_INFLIGHT", "16")))
_ASR_LOCK = threading.Lock()


def log(msg):
    # 必须带时间戳：没有时间戳时，52 次进程重启的日志糊在一起，无法按时间对账
    # 「哪一句语音在哪一刻进来、有没有被执行」，这是排查丢指令的前提。
    now = time.time()
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + ".%03d" % int((now % 1) * 1000)
    sys.stderr.write("[mcp-asr] %s %s\n" % (ts, msg))
    sys.stderr.flush()


# ---------------------------------------------------------------- 协议
def _send(obj):
    # 请求改成多线程处理后会并发写 stdout，必须整行落锁，否则两行 JSON 交错
    # 会变成「收到非 JSON 行，已忽略」/ Parse error（历史上 75 + 2 次）。
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _SEND_LOCK:
        _PROTO_OUT.write(line)
        _PROTO_OUT.flush()


def _result(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


# ---------------------------------------------------------------- ASR
def _model(name):
    import whisper
    with _MODEL_LOCK:
        if name not in _MODELS:
            log("加载 whisper 模型 %s（首次会下载权重）" % name)
            _MODELS[name] = whisper.load_model(name)
            log("whisper 模型 %s 就绪" % name)
        return _MODELS[name]


def _sniff_ext(raw):
    if raw[:4] == b"RIFF":
        return ".wav"
    if raw[:3] == b"ID3" or raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if raw[:4] == b"OggS":
        return ".ogg"
    if raw[:4] == b"fLaC":
        return ".flac"
    if raw[:4] == b"\x1aE\xdf\xa3":
        return ".webm"
    return ".bin"


def _audio_path(args):
    path = args.get("audio_path")
    if path:
        if not os.path.exists(path):
            raise ValueError("音频文件不存在：%s" % path)
        return path, None
    b64 = args.get("audio_base64") or args.get("audio")
    if not b64:
        raise ValueError("需要 audio_base64 或 audio_path")
    if isinstance(b64, str) and b64.startswith("data:"):
        b64 = b64.split(",", 1)[-1]
    raw = base64.b64decode(b64)
    if not raw:
        raise ValueError("audio_base64 解码后为空")
    fd, path = tempfile.mkstemp(prefix="asr_in_", suffix=_sniff_ext(raw))
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return path, path


def _max_frame_rms(audio, frame=320):
    """整段音频里最响的 20ms 帧的 RMS，用来在送模型前判静音（-54 dBFS 以下当没说话）。"""
    import numpy as np
    if audio is None or len(audio) == 0:
        return 0.0
    n = len(audio) // frame
    if n <= 0:
        return float(np.sqrt(np.mean(np.square(audio))))
    frames = audio[:n * frame].reshape(n, frame)
    return float(np.sqrt(np.mean(np.square(frames), axis=1)).max())


def _norm_asr_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def _is_hallucination(seg):
    """单段判定：已知口径黑名单、低置信 + 高 no_speech 概率、复读压缩比过高。"""
    t = _norm_asr_text(seg.get("text"))
    if not t:
        return True
    if any(p.search(t) for p in _HALLUCINATION_PATTERNS):
        return True
    if any(p.fullmatch(t) for p in _SOFT_HALLUCINATION_PATTERNS):
        return True
    no_speech = float(seg.get("no_speech_prob") or 0.0)
    logprob = float(seg.get("avg_logprob") or 0.0)
    if no_speech >= ASR_NO_SPEECH_PROB and logprob <= ASR_MIN_AVG_LOGPROB:
        return True
    if float(seg.get("compression_ratio") or 0.0) >= ASR_MAX_COMPRESSION:
        return True
    return False


def _filter_asr_segments(raw_segments):
    """丢掉幻觉/无语音段并折叠连续复读，返回 (保留段, 丢弃数)。"""
    kept, dropped, last = [], 0, None
    for s in raw_segments:
        text = _norm_asr_text(s.get("text"))
        if _is_hallucination(s) or text == last:   # 连续同一句＝复读，只留头一遍
            dropped += 1
            continue
        last = text
        kept.append(dict(s, text=text))
    return kept, dropped


def _seg_dur(seg):
    return max(0.01, float(seg.get("end") or 0) - float(seg.get("start") or 0))


def asr_confidence(kept):
    """把段级 avg_logprob / no_speech_prob 按时长加权，聚合成 0-1 的整段置信度。

    e^avg_logprob 近似「平均每个 token 的概率」；再乘 (1 - no_speech_prob) 惩罚像噪声的段。
    这只是启发式指标，不做校准，用于「要不要执行这条指令」的门槛判断。
    """
    if not kept:
        return {"confidence": 0.0, "avg_logprob": None, "no_speech_prob": None,
                "max_compression_ratio": None, "segments": 0}
    total = sum(_seg_dur(s) for s in kept)
    lp = sum(float(s.get("avg_logprob") or 0.0) * _seg_dur(s) for s in kept) / total
    ns = sum(float(s.get("no_speech_prob") or 0.0) * _seg_dur(s) for s in kept) / total
    comp = max(float(s.get("compression_ratio") or 0.0) for s in kept)
    score = math.exp(min(0.0, lp)) * max(0.0, 1.0 - ns)
    if comp >= ASR_MAX_COMPRESSION:            # 复读倾向，压低置信度
        score *= 0.5
    return {"confidence": round(max(0.0, min(1.0, score)), 3),
            "avg_logprob": round(lp, 3), "no_speech_prob": round(ns, 3),
            "max_compression_ratio": round(comp, 2), "segments": len(kept)}


def _asr_trace(tag, client_id, q, t0):
    """每条转写都留一行结构化日志：这是「每条语音指令都可对账」的唯一证据。
    以前成功路径完全不打点，导致 3 次转写只有 1 次有反应时无法判断另 2 次去哪了。
    """
    log("ASR[%s] client_id=%s %.2fs rms=%.5f dur=%.2fs silent=%s filtered=%d "
        "conf=%.2f low=%s text=%r" % (
            tag, client_id or "-", time.time() - t0, float(q.get("rms") or 0.0),
            float(q.get("duration") or 0.0), bool(q.get("silent")),
            int(q.get("filtered") or 0), float(q.get("confidence") or 0.0),
            bool(q.get("low_confidence")), (q.get("text") or "")[:80]))


def asr_transcribe(args, tag="asr_transcribe"):
    t0 = time.time()
    client_id = str(args.get("client_id") or "").strip()[:120]
    q = _asr_transcribe_impl(args, client_id, t0, tag)
    _asr_trace(tag, client_id, q, t0)
    return q


def _asr_transcribe_impl(args, client_id, t0, tag):
    model_name = args.get("model") or DEFAULT_MODEL
    lang = args.get("language") or DEFAULT_LANG
    path, tmp = _audio_path(args)
    try:
        import whisper
        audio = whisper.load_audio(path)  # 内部走 ffmpeg，支持 wav/mp3/ogg 等
        duration = round(len(audio) / 16000.0, 2)
        rms = _max_frame_rms(audio)
        if rms < ASR_MIN_RMS:              # 空录音不必跑模型，也绝不会返回幻觉
            log("ASR 判为静音（peak-frame rms=%.5f < %.5f），不送模型" % (rms, ASR_MIN_RMS))
            stats = asr_confidence([])
            return {"text": "", "language": lang, "duration": duration,
                    "model": model_name, "segments": [], "filtered": 0, "silent": True,
                    "rms": round(rms, 5), "low_confidence": True, **stats}
        kwargs = {"language": lang}
        if args.get("initial_prompt"):
            kwargs["initial_prompt"] = args["initial_prompt"]
        with _ASR_LOCK:                            # whisper 非线程安全，串行推理
            r = _model(model_name).transcribe(audio, **kwargs)
        kept, dropped = _filter_asr_segments(r.get("segments", []))
        if dropped:
            log("ASR 过滤幻觉/无语音段 %d 段（原始文本长度 %d）" % (
                dropped, len(r.get("text") or "")))
        segments = [{"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"]}
                    for s in kept]
        joiner = "" if str(lang).lower().startswith(("zh", "ja", "yue", "ko")) else " "
        text = joiner.join(s["text"] for s in segments).strip() or (
            "" if dropped else (r.get("text") or "").strip())
        stats = asr_confidence(kept)
        return {
            "text": text,
            "language": r.get("language") or lang,
            "duration": duration,
            "model": model_name,
            "segments": segments,
            "filtered": dropped,
            "rms": round(rms, 5),
            "low_confidence": (not text) or stats["confidence"] < ASR_LOW_CONFIDENCE,
            **stats,
        }
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------- TTS（HTTP 桥接）
def _post_json(url, payload, sid=None, timeout=600):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        sid_out = resp.headers.get("Mcp-Session-Id")
        body = resp.read()
    if not body:
        return {}, sid_out
    text = body.decode("utf-8", "replace").strip()
    if text.startswith("event:") or text.startswith("data:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    return json.loads(text), sid_out


def tts_synthesize_http(text, voice=None, stem=None, speed=None):
    init, sid = _post_json(TTS_URL, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "mcp-asr", "version": SERVER_VERSION}},
    })
    if "result" not in init:
        raise RuntimeError("TTS 初始化失败：%s" % json.dumps(init, ensure_ascii=False)[:200])
    _post_json(TTS_URL, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    arguments = {"text": text, "return_audio": True,
                 "stem": stem or "voice_%d" % int(time.time())}
    if voice:
        arguments["voice"] = voice
    if speed and abs(float(speed) - 1.0) > 0.005:
        arguments["speed"] = round(float(speed), 3)
    resp, _ = _post_json(TTS_URL, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "tts_synthesize", "arguments": arguments}}, sid)
    if "error" in resp:
        raise RuntimeError("TTS 调用失败：%s" % resp["error"].get("message"))
    content = (resp.get("result") or {}).get("content") or []
    audio = next((c for c in content if c.get("type") == "audio"), None)
    meta = next((json.loads(c["text"]) for c in content
                 if c.get("type") == "text" and c.get("text", "").lstrip().startswith("{")), {})
    if not audio:
        raise RuntimeError("TTS 未返回音频内容块")
    return audio.get("data"), audio.get("mimeType", "audio/wav"), meta


def _tts_timed(text, voice, timeout, emotion=None):
    """带超时的 TTS：合成慢时先让调用方拿到文字，不让「收到」被卡住。"""
    box = {}

    def run():
        try:
            box["v"] = speak(text, voice=voice, emotion=emotion,
                             stem="ack_%s" % os.urandom(4).hex())
        except Exception as e:                       # noqa: BLE001 - 原样回传给调用方
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("TTS 超过 %.0fs 未返回" % timeout)
    if "e" in box:
        raise box["e"]
    return box["v"]


# ---------------------------------------------------------------- 情绪化韵律
# CosyVoice2 只认参考音色、不接受情绪指令，所以情绪落到两个可听维度：语速 + 音高。
# 语速交给 TTS 的 speed（原生、音质好），音高用 ffmpeg 变调不变速后处理。
# 情绪取自员工档案的 emotion 字段；强度可整体缩放，设 0 即关闭情绪化。
EMOTION_PRESETS = {
    "cheerful": {"speed": 1.08, "pitch": 1.05},   # 轻快、上扬
    "calm":     {"speed": 0.96, "pitch": 0.99},   # 平稳、略沉
    "serious":  {"speed": 0.93, "pitch": 0.97},   # 放缓、压低
    "neutral":  {"speed": 1.00, "pitch": 1.00},
}
# 档案里若写了中文情绪词，折算到上面四档
EMOTION_ALIASES = {"开朗": "cheerful", "活泼": "cheerful", "轻快": "cheerful", "热情": "cheerful",
                   "冷静": "calm", "沉稳": "calm", "温和": "calm", "稳重": "calm",
                   "严肃": "serious", "严谨": "serious", "克制": "serious",
                   "中性": "neutral", "平静": "calm"}
ASR_EMOTION_TTS = os.environ.get("ASR_EMOTION_TTS", "1") not in ("0", "false", "False", "no")
ASR_EMOTION_STRENGTH = float(os.environ.get("ASR_EMOTION_STRENGTH", "1.0"))
EMOTION_OUTPUT_DIR = os.environ.get("ASR_EMOTION_OUTPUT", "/opt/sefs/opc-workbench/mcp-asr/output")
FFMPEG_BIN = (os.environ.get("ASR_FFMPEG") or shutil.which("ffmpeg")
              or next((p for p in ("/usr/bin/ffmpeg", "/home/boray/.local/bin/ffmpeg")
                       if os.path.exists(p)), ""))


def _probe_bin():
    """优先用与 ffmpeg 同目录的 ffprobe，其次系统 /usr/bin/ffprobe。"""
    cands = []
    if FFMPEG_BIN:
        cands.append(os.path.join(os.path.dirname(FFMPEG_BIN), "ffprobe"))
    cands += ["/usr/bin/ffprobe", shutil.which("ffprobe") or ""]
    for p in cands:
        if p and os.path.exists(p):
            return p
    return ""


FFPROBE_BIN = _probe_bin()
# 本机 shell 的 LD_LIBRARY_PATH 会污染系统 ffmpeg/ffprobe（报 GLIBCXX 缺失），子进程里剔除
FF_ENV = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}


def norm_emotion(emotion):
    """把档案里的情绪词归一到预设键，未知值按 neutral 处理。"""
    e = (emotion or "").strip().lower()
    e = EMOTION_ALIASES.get(e, e)
    return e if e in EMOTION_PRESETS else "neutral"


def emotion_of(aid=None, employee=None, override=None):
    """谁是这次播报的「人」，就用谁的档案情绪；override 优先（auto 表示仍按档案）。"""
    if override and str(override).strip().lower() != "auto":
        return norm_emotion(override)
    prof = employee or load_profiles().get(aid or "") or {}
    return norm_emotion(prof.get("emotion"))


def _emotion_preset(emotion):
    """情绪 → {speed, pitch}；关闭或强度为 0 时返回 None（不做任何处理）。"""
    if not ASR_EMOTION_TTS or ASR_EMOTION_STRENGTH <= 0:
        return None
    base = EMOTION_PRESETS[norm_emotion(emotion)]
    k = ASR_EMOTION_STRENGTH
    speed = round(1.0 + (base["speed"] - 1.0) * k, 3)
    pitch = round(1.0 + (base["pitch"] - 1.0) * k, 3)
    if abs(speed - 1.0) < 0.005 and abs(pitch - 1.0) < 0.005:
        return None
    return {"speed": speed, "pitch": pitch}


def _pitch_shift(audio_b64, pitch, stem):
    """变调不变速：asetrate 抬/压音高，紧接 atempo 把时长还原。返回 (base64, 输出路径)。"""
    if not audio_b64 or abs(pitch - 1.0) < 0.005 or not FFMPEG_BIN:
        return audio_b64, None
    try:
        os.makedirs(EMOTION_OUTPUT_DIR, exist_ok=True)
        src = os.path.join(EMOTION_OUTPUT_DIR, "%s.src.wav" % stem)
        dst = os.path.join(EMOTION_OUTPUT_DIR, "%s.wav" % stem)
        with open(src, "wb") as f:
            f.write(base64.b64decode(audio_b64))
        rate = 24000                                    # CosyVoice2 输出 24k，先统一再变调
        af = ("aresample=%d,asetrate=%d,aresample=%d,atempo=%.4f"
              % (rate, int(round(rate * pitch)), rate, 1.0 / pitch))
        subprocess.run([FFMPEG_BIN, "-nostdin", "-y", "-loglevel", "error", "-i", src,
                        "-filter:a", af, "-ac", "1", "-ar", str(rate),
                        "-c:a", "pcm_s16le", dst], check=True, timeout=60,
                       stdin=subprocess.DEVNULL)
        with open(dst, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii"), dst
    except Exception as e:                              # noqa: BLE001
        log("情绪变调失败（pitch=%.3f）：%s" % (pitch, e))
        return audio_b64, None
    finally:
        tmp_src = os.path.join(EMOTION_OUTPUT_DIR, "%s.src.wav" % stem)
        if os.path.exists(tmp_src):
            os.unlink(tmp_src)


def speak(text, voice=None, emotion=None, stem=None):
    """按情绪合成一句语音：语速走 TTS，音高走 ffmpeg。返回 (base64, mime, meta)。"""
    stem = stem or "emo_%s" % os.urandom(4).hex()
    preset = _emotion_preset(emotion) or {"speed": 1.0, "pitch": 1.0}
    b64, mime, meta = tts_synthesize_http(text, voice=voice, stem=stem, speed=preset["speed"])
    shifted, path = _pitch_shift(b64, preset["pitch"], stem)
    if path:
        meta = dict(meta or {}, audio_path=path, emotion=norm_emotion(emotion))
        return shifted, "audio/wav", meta
    return b64, mime, meta


# 「收到」语音按 (文本, 音色, 情绪) 缓存，常见回复一次合成、长期复用，保证即时响应
_ACK_CACHE = {}
_ACK_LOCK = threading.Lock()
_ACK_STATIC = ["好的，老板，马上安排。", "老板，稍等。"]
# 接通即打招呼：不能让老板对着静音等，第一句也是「链路通了」的确认；启动时预合成，接通瞬间就出声
GREETING_TEXTS = [t.strip() for t in os.environ.get(
    "ASR_GREETING_TEXTS", "老板你好，我在。|老板，我在呢。").split("|") if t.strip()]
# 通话中连续这么久没有一句语音，就主动搭一句，免得老板以为掉线了
# 老板 10 秒没新指令就主动搭一句（原来 45s 太钝）；之后按 REPEAT 间隔重提，避免每 10s 刷屏
IDLE_NUDGE_SEC = float(os.environ.get("ASR_IDLE_NUDGE_SEC", "10"))
IDLE_NUDGE_REPEAT_SEC = float(os.environ.get("ASR_IDLE_NUDGE_REPEAT_SEC", "60"))
# 再久还没有新指令，就不再只说「我在」，而是把当前任务/状态压成一条语音简报念给老板
BRIEFING_ENABLED = os.environ.get("ASR_BRIEFING", "1") not in ("0", "false", "False", "no")
BRIEFING_SEC = float(os.environ.get("ASR_BRIEFING_SEC", "90"))
BRIEFING_MAX_CHARS = int(os.environ.get("ASR_BRIEFING_MAX_CHARS", "120"))
BRIEFING_DIR = os.environ.get("ASR_BRIEFING_DIR", "/opt/sefs/obsidian")
# 简报是起流时后台预合成的，不在通话关键路径上：TTS 是串行 FIFO，服务刚重启时前面还排着
# 十几条「收到/问候」预热，按 90s 等就会像 2026-11-10 18:47 那样明明合成好了却被判超时丢弃
BRIEFING_TTS_TIMEOUT = float(os.environ.get("ASR_BRIEFING_TTS_TIMEOUT", "240"))
ACK_VOICE = os.environ.get("ASR_ACK_VOICE", DEFAULT_VOICE)


def ack_audio(text, voice=None, timeout=None, emotion=None):
    """取一句「收到」语音：命中缓存直接返回，未命中才合成；失败返回 None。"""
    voice = voice or ACK_VOICE
    key = (text, voice, norm_emotion(emotion) if emotion else "")
    with _ACK_LOCK:
        hit = _ACK_CACHE.get(key)
    if hit:
        return hit
    try:
        audio_b64, mime, meta = _tts_timed(text, voice, timeout or ACK_TTS_TIMEOUT,
                                           emotion=emotion)
    except Exception as e:                           # noqa: BLE001
        log("「收到」语音合成失败（%s），本次只回文字：%s" % (text, e))
        return None
    item = {"audio_base64": audio_b64, "mime": mime,
            "audio_path": meta.get("audio_path"), "sample_rate": meta.get("sample_rate"),
            "duration": meta.get("duration"), "segments": meta.get("segments") or [],
            "emotion": norm_emotion(emotion) if emotion else None}
    with _ACK_LOCK:
        _ACK_CACHE[key] = item
    return item


def _ack_cached(text, voice=None, emotion=None):
    """只查缓存，不触发合成：用于「有就立刻播，没有就走后台补」的场景。"""
    voice = voice or ACK_VOICE
    key = (text, voice, norm_emotion(emotion) if emotion else "")
    with _ACK_LOCK:
        return _ACK_CACHE.get(key)


def prewarm_acks():
    """后台预合成常用「收到」语音，客户端首次调用就能秒回。

    TTS 服务常比 ASR 晚就绪（模型加载要几十秒），所以这里带重试，直到缓存命中。
    缓存 key 带音色和情绪，必须按「默认播报人」的档案音色/情绪各预热一遍，
    否则 voice_chat 传了员工 voice/emotion 就永远命不中缓存，每次都要现场合成 10 多秒。
    """
    profiles = load_profiles()
    targets = [(DEFAULT_VOICE, None)]
    for aid in (SECRETARY_ID, "ceo"):
        prof = profiles.get(aid) or {}
        targets.append((prof.get("voice") or DEFAULT_VOICE,
                        norm_emotion(prof.get("emotion")) or None))
    done = set()
    for voice, emotion in targets:
        for phrase in _ACK_STATIC + GREETING_TEXTS:
            key = (phrase, voice, norm_emotion(emotion) if emotion else "")
            if key in done:
                continue
            done.add(key)
            for _ in range(6):
                if ack_audio(phrase, voice=voice, timeout=120, emotion=emotion):
                    log("预合成「收到」语音：%r（voice=%s，emotion=%s）"
                        % (phrase, voice, emotion))
                    break
                time.sleep(10)


# ---------------------------------------------------------------- 员工名册（语音派单）
_ROSTER = {"mtime": 0.0, "by_id": {}, "by_name": {}}
_ROSTER_LOCK = threading.Lock()


def load_roster():
    """解析 Obsidian 员工名册表格；文件变更时热加载，缺失则返回空表。"""
    try:
        mtime = os.path.getmtime(ROSTER_FILE)
    except OSError:
        return {}
    with _ROSTER_LOCK:
        if _ROSTER["mtime"] == mtime:
            return _ROSTER["by_id"]
        by_id, by_name = {}, {}
        try:
            with open(ROSTER_FILE, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line.startswith("|"):
                        continue
                    cells = [c.strip() for c in line.strip("|").split("|")]
                    if len(cells) < 3 or set("".join(cells)) <= set("-: "):
                        continue
                    aid, name = cells[0], cells[1]
                    if aid in ("Agent ID", "") or not re.match(r"^[A-Za-z0-9_]+$", aid):
                        continue
                    item = {"id": aid, "name": name,
                            "role": cells[2] if len(cells) > 2 else "",
                            "project": cells[3] if len(cells) > 3 else ""}
                    by_id[aid] = item
                    if name:
                        by_name[name] = aid
        except OSError as e:
            log("读取员工名册失败：%s" % e)
        _ROSTER.update({"mtime": mtime, "by_id": by_id, "by_name": by_name})
        log("员工名册 %s → %d 人" % (ROSTER_FILE, len(by_id)))
        return by_id


_DELEGATE_RE = re.compile(r"@|让|叫|安排|交给|转给|去找|通知|找|派")
_QUERY_RE = re.compile(r"怎么样|如何|是谁|在哪|干得|情况|进展")


def find_employee(text):
    """从语音文本里找出被点名的员工：@id 优先，其次姓名，最后 Agent ID。

    只在像「派活」时才认领：有派单动词，或以员工名开头但不是在打听他（怎么样/如何/进展…）。
    """
    roster = load_roster()
    profiles = load_profiles()
    text = text or ""
    m = re.search(r"@([A-Za-z0-9_]+)", text)
    if m and m.group(1) in roster and not is_record_only(m.group(1), profiles):
        return roster[m.group(1)]
    hit = None
    for name, aid in _ROSTER["by_name"].items():
        if not is_record_only(aid, profiles) and name and name in text:
            hit = roster.get(aid)
            break
    if not hit:
        for aid, item in roster.items():
            if not is_record_only(aid, profiles) and re.search(
                    r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(aid), text):
                hit = item
                break
    if not hit:
        return None
    if _DELEGATE_RE.search(text):
        return hit
    if text.strip().startswith((hit["name"], hit["id"])) and not _QUERY_RE.search(text):
        return hit
    return None


_HEAVY_RE = re.compile(
    r"写|做|生成|渲染|剪辑|视频|调研|搜索|查|分析|整理|部署|修复|跑|执行|发邮件|制作|方案|报告|排期|发布|评审|优化")


def ack_for(text, employee):
    """按指令类型挑一句最短的「收到」：派单报名字，重活说稍等，轻活说马上安排。"""
    if employee:
        return "好的老板，我这就让%s去办。" % employee["name"]
    if _HEAVY_RE.search(text or "") or len(text or "") > 24:
        return "老板，稍等。"
    return "好的，老板，马上安排。"


# ---------------------------------------------------------------- 低置信澄清
# 识别不准时不做任何执行，改用「当前播报人」的身份与情绪请老板重说。
# 话术优先让大模型按该员工人设生成；模型慢或失败就退回按情绪写死的模板句。
_CLARIFY_TEMPLATES = {
    "cheerful": "诶？老板，刚才有点吵，我没太听清，你再说一遍呗？",
    "calm": "老板，刚才没听清楚，麻烦再说一遍。",
    "serious": "老板，刚才没听清，请再说一遍。",
    "neutral": "老板，刚才没听清楚，麻烦再说一遍。",
}
# 大模型偶尔会答非所问（实测员工 profile 回过「老板早！」），必须命中「没听清/再说」才算数
_CLARIFY_OK = re.compile(r"听清|没听|听不|再说|重复|没听懂|不确定|确认一下|听错|没明白")


def _first_line(raw, limit=48):
    """取大模型输出里第一句像人话的回复：去 Markdown、去「回复：」前缀、去引号。"""
    s = _clean_for_speech(raw)
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    line = lines[0] if lines else ""
    line = line.strip(" \t*`#>-\"'“”‘’")
    line = re.sub(r"^(?:回复|回答|答复|答|助手|我)\s*[:：]\s*", "", line).strip(" \t*`")
    m = _SENT_RE.match(line)
    if m and m.group(0).strip():
        line = m.group(0).strip()
    if len(line) > limit:
        line = line[:limit].rstrip("，,、；; ") + "…"
    return line


def clarify_line(employee, aid, heard, confidence, timeout=None):
    """生成一句情绪化的「没听清，请重说」，返回 (句子, 情绪)。"""
    pid = (employee or {}).get("id") or aid or ""
    prof = dict(load_profiles().get(pid) or {})
    if employee:
        prof.setdefault("name", employee.get("name"))
        prof.setdefault("role", employee.get("role"))
    emotion = emotion_of(pid, employee=employee)
    if not ASR_CLARIFY_LLM:
        return _CLARIFY_TEMPLATES[emotion], emotion
    name = prof.get("name") or "值班员工"
    title = prof.get("title") or prof.get("role") or ""
    prompt = (
        "你是%s%s，语气基调是 %s。\n"
        "老板刚发来一条语音，但语音识别置信度只有 %.2f，识别成「%s」，很可能不准确。\n"
        "请只回一句话，内容必须是「没听清，请老板再说一遍」这类意思。\n"
        "禁止打招呼、禁止执行指令、禁止解释、禁止 Markdown。不超过 30 字。"
        % (name, ("（%s）" % title) if title else "", emotion, confidence,
           heard or "（没识别出内容）")
    )
    profile_arg = None
    if ASR_CLARIFY_PROFILE and pid and _profile_ready(pid):
        profile_arg = prof.get("profile") or prof.get("id")
    t0 = time.time()
    try:
        raw = run_hermes(prompt, profile=profile_arg,
                         timeout=timeout or ASR_CLARIFY_TIMEOUT)
        line = _first_line(raw)
        if line and _CLARIFY_OK.search(line):
            log("澄清语（%.1fs，%s）：%s" % (time.time() - t0, emotion, line))
            return line, emotion
        log("澄清语不合要求（%.1fs）：%r，改用模板兜底" % (time.time() - t0, line))
    except Exception as e:                           # noqa: BLE001
        log("澄清语生成失败（%.1fs），改用模板兜底：%s" % (time.time() - t0, e))
    return _CLARIFY_TEMPLATES[emotion], emotion


# ---------------------------------------------------------------- Hermes 执行
def _strip_ansi(s):
    return _ANSI_RE.sub("", s or "")


def _run_hermes_subprocess(prompt, profile=None, timeout=240):
    """一次性子进程执行（gateway 不可用时的兜底路径，行为与改造前完全一致）。"""
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    if profile:
        env["HERMES_PROFILE"] = profile
    else:
        env.pop("HERMES_PROFILE", None)
    proc = subprocess.run([HERMES_CLI, "-z", prompt], capture_output=True, text=True,
                          timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    out = _strip_ansi(proc.stdout).strip()
    if not out:
        err = _strip_ansi(proc.stderr).strip()[-500:]
        raise RuntimeError("Hermes 无输出（exit=%s）：%s" % (proc.returncode, err))
    return out


def run_hermes(prompt, profile=None, timeout=240, *, idem_key=None, session_id=None,
               on_status=None):
    """统一执行入口：优先常驻 gateway /v1/runs，不可用（或显式 subprocess）时回落子进程。"""
    if _GW is not None and HERMES_EXEC_MODE != "subprocess":
        log("执行走 gateway：profile=%s session=%s timeout=%ss prompt=%r" % (
            profile or "-", session_id or "-", timeout, (prompt or "")[:60]))
        try:
            return _GW.run(prompt, profile=profile, timeout=timeout, idem_key=idem_key,
                           session_id=session_id, on_status=on_status).text
        except (_GW.GatewayUnavailable, _GW.GatewayBusy) as e:
            if HERMES_EXEC_MODE == "gateway":
                raise
            log("gateway 不可用（%s），回落子进程 profile=%s" % (e, profile))
    else:
        log("执行走子进程（mode=%s gw=%s）：profile=%s prompt=%r" % (
            HERMES_EXEC_MODE, "ok" if _GW is not None else "missing",
            profile or "-", (prompt or "")[:60]))
    return _run_hermes_subprocess(prompt, profile, timeout)


# ---------------------------------------------------------------- 会话（连续对话）
_SESSION_LOCK = threading.Lock()
_SESSION_STATE = {}                                  # key → {sid, ts, seq, day}


def _sess_slug(key):
    s = re.sub(r"[^a-z0-9]+", "-", str(key or "").lower()).strip("-")
    return (s or "default")[:32]


def session_for(key):
    """同一 key（员工/默认账号）连续对话；空闲超过 ASR_SESSION_IDLE 或跨日则轮换新会话。"""
    now = time.time()
    day = time.strftime("%Y%m%d", time.localtime(now))
    with _SESSION_LOCK:
        st = _SESSION_STATE.get(key)
        if st and st["day"] == day and now - st["ts"] <= ASR_SESSION_IDLE:
            st["ts"] = now
            return st["sid"]
        seq = (st["seq"] + 1) if st else 1
        sid = "asr-%s-%s-%d" % (_sess_slug(key), day, seq)
        _SESSION_STATE[key] = {"sid": sid, "ts": now, "seq": seq, "day": day}
        return sid


def live_session_id(live_sid):
    """实时通话：一通电话一个会话，挂断即失效。"""
    return "live-" + _sess_slug(live_sid)


# ---------------------------------------------------------------- 语音汇报文本
_MD_FENCE_RE = re.compile(r"```.*?```", re.S)
_SENT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


def _clean_for_speech(text):
    """去掉 Markdown 噪音，得到适合朗读的纯文本。"""
    s = _MD_FENCE_RE.sub(" ", text or "")
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"^\s{0,3}(?:[-*·]|\d+[.)、])\s*", "", s, flags=re.M)
    s = s.replace("**", "").replace("__", "").replace("`", "")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def summarize_for_voice(text, limit=None):
    """把详细结论压成 1-2 句语音汇报（默认 ≤90 字），避免语音又长又碎。"""
    limit = limit or VOICE_SUMMARY_CHARS
    clean = _clean_for_speech(text)
    if not clean:
        return "老板，这次没拿到结果，请稍后重试。"
    picked = ""
    for m in _SENT_RE.finditer(clean):
        seg = m.group(0).strip()
        if not seg:
            continue
        if picked and not re.match(r"[，,。！？!?；;：:]", seg):
            picked += "，"                       # 多句拼接时补停顿，避免朗读粘连
        picked += seg
        if len(picked) >= max(12, int(limit * 0.7)):
            break
    picked = picked or clean
    if len(picked) > limit:
        picked = picked[:limit].rstrip("，,、；; ")
        picked += "……"
    if picked[-1] not in "。！？!?":
        picked += "。"
    return picked


def _extract_json(raw):
    """员工输出常被要求为 JSON；宽松解析，失败返回 None。"""
    s = (raw or "").strip()
    candidates = [re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I).strip(), s]
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        candidates.append(s[i:j + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            continue
    return None


def normalize_answer(raw):
    """把助手/员工输出整理成 (detail_text, speech_text)：详细文字给客户端，短句给语音。"""
    detail = (raw or "").strip()
    data = _extract_json(detail)
    speech = ""
    if data:
        speech = str(data.get("reply") or data.get("summary") or data.get("answer")
                     or data.get("text") or "").strip()
        lines = []
        if speech:
            lines.append(speech)
        for key, title in (("actions", "下一步"), ("next", "下一步"),
                           ("evidence", "依据"), ("reasons", "理由")):
            items = data.get(key)
            if isinstance(items, list) and items:
                lines.append(title + "：")
                lines.extend("· " + str(x) for x in items if str(x).strip())
        conf = data.get("confidence")
        if isinstance(conf, (int, float)):
            lines.append("置信度：%.2f" % conf)
        if lines:
            detail = "\n".join(lines)
    if not speech:
        speech = summarize_for_voice(detail)
    else:
        speech = summarize_for_voice(speech)
    return detail, speech


# ---------------------------------------------------------------- 数字人播报档案
def _asset_path(subdir, filename):
    for root in ASSET_DIRS:
        path = os.path.join(root, subdir, filename)
        if os.path.exists(path):
            return path
    return ""


def _asset_url(subdir, filename):
    rel = "/%s/%s" % (subdir, filename)
    return (ASSET_URL_BASE + rel) if ASSET_URL_BASE else rel


def _asset_url_full(subdir, filename):
    """绝对可播放地址：站点相对路径 + PUBLIC_BASE（未配置时退回相对路径）。"""
    rel = _asset_url(subdir, filename)
    return (PUBLIC_BASE + rel) if (PUBLIC_BASE and rel.startswith("/")) else rel


# ---------------------------------------------------------------- 员工档案
_PROFILES = {"mtime": 0.0, "by_id": {}}
_PROFILES_LOCK = threading.Lock()


def load_profiles():
    """读取 employees.json；文件变更时热加载，缺失返回空表。"""
    try:
        mtime = os.path.getmtime(PROFILES_FILE)
    except OSError:
        return {}
    with _PROFILES_LOCK:
        if _PROFILES["mtime"] == mtime:
            return _PROFILES["by_id"]
        by_id = {}
        try:
            with open(PROFILES_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("employees") or []:
                pid = (item.get("id") or "").strip()
                if pid:
                    by_id[pid] = item
        except (OSError, ValueError) as e:
            log("读取员工档案失败：%s" % e)
        _PROFILES.update({"mtime": mtime, "by_id": by_id})
        log("员工档案 %s → %d 人" % (PROFILES_FILE, len(by_id)))
        return by_id


def _profile_ready(pid):
    """该员工是否有独立 Hermes profile（没有的话派单会回落到 root，等于没派）。"""
    if not pid:
        return False
    if _GW is not None and HERMES_EXEC_MODE != "subprocess":
        served = _GW.served_profiles()
        if served:                                   # 网关在线：以 /p/<profile> 实际可用集合为准
            return pid in served
    return os.path.isdir(os.path.join(HERMES_HOME_DIR, "profiles", pid))


def is_record_only(pid, profiles=None):
    """OPC-000 老板本人：档案只做记录，不接任务、不派单（由 OPC-001 影子分身代行）。"""
    profiles = load_profiles() if profiles is None else profiles
    return bool((profiles.get(pid) or {}).get("recordOnly"))


def employee_profile(aid, include_assets=True):
    """拼一份完整档案：静态档案 + 素材实际文件 + Hermes profile 就绪状态。"""
    prof = dict(load_profiles().get(aid) or {"id": aid})
    prof.setdefault("id", aid)
    prof.setdefault("name", aid)
    prof.setdefault("role", prof.get("title") or "")
    prof.setdefault("recordOnly", False)
    prof["profile"] = prof.get("profile") or aid
    prof["profile_ready"] = _profile_ready(prof["profile"])
    if include_assets:
        # 影子分身（OPC-001）复用老板本人的形象素材，档案里配 assetId 指向原主
        asset_aid = prof.get("assetId") or aid
        av = avatar_profile(asset_aid, prof.get("name"), prof.get("role")) or {}
        photo = av.get("photo") or {}
        videos = av.get("videos") or {}
        face = ""
        for ext in ("png", "jpg"):
            if _asset_path("faces", "%s.%s" % (asset_aid, ext)):
                face = _asset_url("faces", "%s.%s" % (asset_aid, ext))
                break
        prof["photo"] = photo.get("url") or ""
        prof["videoTalk"] = (videos.get("talk") or {}).get("url") or ""
        prof["videoIdle"] = (videos.get("idle") or {}).get("url") or ""
        prof["has_photo"] = bool(prof["photo"])
        prof["has_video"] = bool(prof["videoTalk"] or prof["videoIdle"])
        prof["assets"] = {"photo": photo, "face_url": face or None,
                          "video_talk": videos.get("talk"), "video_idle": videos.get("idle")}
        # 自我介绍视频：服务端生成后放这里，客户端拿到 URL 直接边下边播（mp4 faststart）
        intro_file = os.path.join(INTRO_DIR, "%s.mp4" % aid) if INTRO_DIR else ""
        intro_ready = bool(intro_file and os.path.exists(intro_file))
        intro_meta = _intro_meta(aid) if intro_ready else {}
        prof["introVideo"] = _asset_url("intro", "%s.mp4" % aid) if intro_ready else ""
        prof["introReady"] = intro_ready
        prof["introText"] = _read_intro_text(aid) if intro_ready else ""
        prof["introDuration"] = intro_meta.get("duration") or 0
        prof["liveCall"] = {"supported": bool(FFMPEG_BIN and LIVE_DIR),
                            "protocol": "hls", "segment_sec": LIVE_SEGMENT_SEC}
    return prof


def profile(args):
    """获取员工档案：默认全部；可按 id / dept / project 过滤。"""
    aid = (args.get("id") or "").strip()
    dept = (args.get("dept") or "").strip()
    project = (args.get("project") or "").strip()
    include_assets = args.get("include_assets", True)
    ids = list(load_profiles()) or list(load_roster())
    out = []
    for pid in ids:
        item = load_profiles().get(pid) or {}
        if aid and pid != aid:
            continue
        if dept and dept not in (item.get("dept") or ""):
            continue
        if project and project not in (item.get("project") or ""):
            continue
        out.append(employee_profile(pid, include_assets))
    return {"count": len(out), "asset_url_base": ASSET_URL_BASE or None,
            "employees": out,
            "missing_photo": [p["id"] for p in out if not p.get("has_photo")],
            "missing_video": [p["id"] for p in out if not p.get("has_video")],
            "missing_intro": [p["id"] for p in out if not p.get("introReady")]}


def avatar_profile(aid, name=None, role=None):
    """按员工 ID 找数字人素材：照片必选，仿真人视频素材可插拔（有就返回，没有就只给照片）。

    约定（丢文件即生效，无需改代码）：
      assets/avatars/<id>.png|jpg   头像 / 半身照
      assets/faces/<id>.png         面部特写（口型贴图用）
      assets/videos/<id>.talk.mp4   说话素材（可选，按音频包络做口型叠加）
      assets/videos/<id>.idle.mp4   待机素材（可选）
    """
    if not aid:
        return None
    # 影子分身等档案用 assetId 指向原主的素材（OPC-001 小秘书复用老板本人的脸与视频）
    asset_aid = (load_profiles().get(aid) or {}).get("assetId") or aid
    photo, photo_url = "", None
    for subdir, ext in (("avatars", "png"), ("avatars", "jpg"), ("faces", "png")):
        photo = _asset_path(subdir, "%s.%s" % (asset_aid, ext))
        if photo:
            photo_url = _asset_url(subdir, "%s.%s" % (asset_aid, ext))
            break
    videos = {}
    for key, names in (("talk", ("%s.talk.mp4" % asset_aid, "%s.mp4" % asset_aid)),
                       ("idle", ("%s.idle.mp4" % asset_aid,))):
        for fn in names:
            path = _asset_path("videos", fn)
            if path:
                videos[key] = {"path": path, "url": _asset_url("videos", fn)}
                break
    if not photo and not videos:
        return None
    item = load_profiles().get(aid) or {}
    return {"id": aid, "name": name or item.get("name") or aid,
            "role": role or item.get("role") or item.get("title") or "",
            "photo": {"path": photo or None, "url": photo_url},
            "videos": videos, "has_video": bool(videos.get("talk"))}


def decide_mode(requested, bandwidth_kbps, avatar):
    """voice=纯语音；avatar=照片/3D 形象按 WAV 包络实时驱动；video=叠加仿真人视频素材。"""
    avatar = avatar or {}
    if requested in ("voice", "avatar"):
        return requested
    if requested == "video":
        return "video" if avatar.get("has_video") else "avatar"
    if bandwidth_kbps and bandwidth_kbps < VIDEO_MIN_KBPS:
        return "voice"
    if avatar.get("has_video"):
        return "video"
    return "avatar" if avatar.get("photo", {}).get("path") else "voice"


# ---------------------------------------------------------------- 自我介绍视频（服务端生成）
# 每位员工一段「画面 + 语音」自我介绍：档案字段经大模型润色成 2-3 句 → TTS → 与 talk 视频
# （没有则用照片做轻微运镜）合成 720×960 MP4，落到 assets/intro/<id>.mp4。
# 首次请求生成并缓存；MP4 带 faststart，客户端可边下边播（HTTP Range）。
INTRO_LLM_TIMEOUT = float(os.environ.get("ASR_INTRO_LLM_TIMEOUT", "180"))
INTRO_MAX_CHARS = int(os.environ.get("ASR_INTRO_MAX_CHARS", "140"))
_INTRO_LOCKS = {}
_INTRO_LOCKS_LOCK = threading.Lock()
_INTRO_PROMPT = (
    "你是 OPC 数字员工「{name}」（{dept}／{title}）。请写一段第一人称自我介绍，"
    "用于员工档案里的自我介绍视频配音。要求：\n"
    "1. 2~3 句，总长不超过 {limit} 个汉字，口语化、可直接朗读；\n"
    "2. 第一句报到（姓名＋部门/岗位），第二句说清主要负责什么，最后一句用档案里的口头禅或一句话收尾；\n"
    "3. 只输出介绍正文，不要 Markdown、不要引号、不要解释、不要换行。\n\n"
    "档案：\n- 姓名：{name}\n- 部门：{dept}\n- 岗位：{title}\n- 项目：{project}\n"
    "- 口头禅：{motto}\n- 简介：{bio}\n- 技能：{skills}\n"
)
# 自我介绍 = 照片 + 文案 TTS → Wav2Lip 口型视频 → 720×960 MP4。
# 生成的 MP4 直接落到员工档案库 assets/intro/，App 通过 profile.introVideo 立即可见，
# 没有审批/发布环节；素材或文案更新后由后台巡检自动重建（ASR_AUTO_INTRO）。
INTRO_PIPELINE = "wav2lip-gan-v1"
LIPSYNC_ENABLE = os.environ.get("ASR_LIPSYNC", "1") not in ("0", "false", "False", "no")
LIPSYNC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lipsync")
LIPSYNC_RUNNER = os.environ.get("ASR_LIPSYNC_RUNNER",
                                os.path.join(LIPSYNC_DIR, "run_lipsync.py"))
LIPSYNC_PYTHON = os.environ.get("ASR_LIPSYNC_PYTHON", "/opt/sefs/CosyVoice/venv/bin/python")
LIPSYNC_WEIGHTS = os.environ.get("ASR_LIPSYNC_WEIGHTS",
                                 os.path.join(LIPSYNC_DIR, "weights", "wav2lip_gan.pth"))
LIPSYNC_THREADS = max(1, int(os.environ.get("ASR_LIPSYNC_THREADS", "32")))
LIPSYNC_TIMEOUT = float(os.environ.get("ASR_LIPSYNC_TIMEOUT", "1200"))
AUTO_INTRO = os.environ.get("ASR_AUTO_INTRO", "1") not in ("0", "false", "False", "no")
AUTO_INTRO_INTERVAL = max(120, int(os.environ.get("ASR_AUTO_INTRO_INTERVAL", "600")))


def _ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path


def _probe_duration(path):
    if not (path and FFPROBE_BIN and os.path.exists(path)):
        return 0.0
    try:
        out = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30, env=FF_ENV,
            stdin=subprocess.DEVNULL)
        return round(float((out.stdout or "0").strip() or 0), 2)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _intro_lock(aid):
    with _INTRO_LOCKS_LOCK:
        lock = _INTRO_LOCKS.get(aid)
        if lock is None:
            lock = _INTRO_LOCKS[aid] = threading.Lock()
        return lock


def _intro_meta(aid):
    path = os.path.join(INTRO_DIR, "%s.json" % aid)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_intro_text(aid):
    path = os.path.join(INTRO_DIR, "%s.txt" % aid)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def intro_fallback_text(prof):
    """大模型不可用时的兜底文案（纯档案字段拼装）。"""
    name = prof.get("name") or prof.get("id") or "我"
    dept = (prof.get("dept") or "").strip()
    title = (prof.get("title") or prof.get("role") or "").strip()
    motto = (prof.get("motto") or "").strip().rstrip("。")
    skills = [str(s).strip() for s in (prof.get("skills") or []) if str(s).strip()]
    out = ["大家好，我是%s%s%s" % (dept, "的" if dept else "", name)]
    if title:
        out.append("，担任%s" % title)
    out.append("。")
    if motto:
        out.append("我常说的一句话是：%s。" % motto)
    if skills:
        out.append("我主要负责%s。" % "、".join(skills[:3]))
    return "".join(out)


def intro_script(aid, prof=None, force=False):
    """自我介绍文案：档案字段经 Hermes 润色，结果缓存到 assets/intro/<id>.txt。"""
    prof = prof or load_profiles().get(aid) or {"id": aid}
    _ensure_dir(INTRO_DIR)
    cached = _read_intro_text(aid)
    if cached and not force:
        return cached
    text = ""
    try:
        prompt = _INTRO_PROMPT.format(
            name=prof.get("name") or aid,
            dept=prof.get("dept") or "数字员工",
            title=prof.get("title") or prof.get("role") or "成员",
            project=prof.get("project") or "OPC",
            motto=prof.get("motto") or "把事做成",
            bio=prof.get("bio") or "",
            skills="、".join(prof.get("skills") or []) or "跨项目协同",
            limit=INTRO_MAX_CHARS)
        raw = run_hermes(prompt, timeout=INTRO_LLM_TIMEOUT)
        text = _clean_for_speech(raw).replace("\n", " ").strip()
        text = re.sub(r"^[\s\"'“”‘’「」]+|[\s\"'“”‘’「」]+$", "", text)
    except Exception as e:                          # noqa: BLE001 - 文案失败不阻断视频生成
        log("自我介绍文案生成失败（%s），改用档案模板：%s" % (aid, e))
    if not text:
        text = intro_fallback_text(prof)
    if len(text) > INTRO_MAX_CHARS:
        text = text[:INTRO_MAX_CHARS].rstrip("，,、；; ") + "。"
    with open(os.path.join(INTRO_DIR, "%s.txt" % aid), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return text


def _intro_video_source(aid):
    """画面来源：优先 talk 视频循环，其次 idle，最后用照片做运镜。"""
    prof = load_profiles().get(aid) or {}
    av = avatar_profile(prof.get("assetId") or aid) or {}
    videos = av.get("videos") or {}
    for key in ("talk", "idle"):
        path = (videos.get(key) or {}).get("path")
        if path and os.path.exists(path):
            return "video", path
    photo = (av.get("photo") or {}).get("path")
    if photo and os.path.exists(photo):
        return "photo", photo
    return "", ""


def lipsync_ready():
    """口型合成链路是否齐备：解释器、脚本、Wav2Lip 权重都在。"""
    return bool(LIPSYNC_ENABLE and os.path.exists(LIPSYNC_PYTHON)
                and os.path.exists(LIPSYNC_RUNNER) and os.path.exists(LIPSYNC_WEIGHTS))


def _intro_photo(aid):
    """口型视频的底图：优先 portraits/ 的 3:4 写实半身像，其次 App 头像。"""
    prof = load_profiles().get(aid) or {}
    asset_aid = prof.get("assetId") or aid
    for ext in ("png", "jpg"):
        portrait = _asset_path("portraits", "%s.%s" % (asset_aid, ext))
        if portrait:
            return portrait
    av = avatar_profile(asset_aid) or {}
    photo = (av.get("photo") or {}).get("path")
    return photo if photo and os.path.exists(photo) else ""


def _intro_lipsync(aid, photo, wav_path, out_path):
    """照片 + 配音 → 口型对齐的无声视频（Wav2Lip GAN，CPU 推理）。"""
    env = dict(FF_ENV)
    env["OMP_NUM_THREADS"] = str(LIPSYNC_THREADS)
    env["MKL_NUM_THREADS"] = str(LIPSYNC_THREADS)
    cmd = [LIPSYNC_PYTHON, LIPSYNC_RUNNER, "--face", photo, "--audio", wav_path,
           "--out", out_path, "--fps", "25", "--batch", "48",
           "--threads", str(LIPSYNC_THREADS)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=LIPSYNC_TIMEOUT, env=env,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0 or not os.path.exists(out_path):
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise RuntimeError("口型合成失败：%s" % (tail or "无输出"))
    summary = (proc.stdout or "").strip().splitlines()
    log("口型视频已合成：%s（%s）" % (aid, summary[-1] if summary else "ok"))
    return out_path



def _intro_filter(kind):
    scale = ("scale=720:960:force_original_aspect_ratio=decrease,"
             "pad=720:960:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if kind == "lipsync":
        # 口型视频与照片同尺寸，按 3:4 居中裁切铺满，不留黑边
        return ("scale=720:960:force_original_aspect_ratio=increase,"
                "crop=720:960,setsar=1")
    if kind == "photo":
        # 静止照片做极慢推近，避免「定住不动」的幻灯片感
        return ("scale=1080:1440:force_original_aspect_ratio=increase,crop=1080:1440,"
                "zoompan=z='min(zoom+0.0006,1.10)':d=1:x='iw/2-(iw/zoom/2)'"
                ":y='ih/2-(ih/zoom/2)':s=720x960:fps=25,setsar=1")
    return scale


def intro_build(aid, force=False, refresh_text=True):
    """生成（或复用）该员工的自我介绍视频，返回档案里要用的字段。

    画面优先用「照片 + 配音」现场合成口型视频（Wav2Lip）；失败时回退到原有
    talk/idle/照片运镜，保证任何情况下都有可播的档案视频。
    """
    if not INTRO_DIR:
        raise RuntimeError("没有可写的素材目录（ASR_ASSET_DIRS 为空）")
    if not FFMPEG_BIN:
        raise RuntimeError("本机没有可用的 ffmpeg，无法生成自我介绍视频")
    prof = load_profiles().get(aid) or {"id": aid}
    _ensure_dir(INTRO_DIR)
    out = os.path.join(INTRO_DIR, "%s.mp4" % aid)
    url = _asset_url("intro", "%s.mp4" % aid)

    def ready_item(cached):
        meta = _intro_meta(aid)
        return {"id": aid, "name": prof.get("name") or aid, "ready": True, "cached": cached,
                "video": url, "text": _read_intro_text(aid),
                "duration": meta.get("duration") or _probe_duration(out),
                "size": os.path.getsize(out), "source": meta.get("source")}

    if os.path.exists(out) and not force:
        return ready_item(True)

    with _intro_lock(aid):
        if os.path.exists(out) and not force:
            return ready_item(True)
        started = time.time()
        text = intro_script(aid, prof, force=(force and refresh_text))
        voice = prof.get("voice") or DEFAULT_VOICE
        emotion = emotion_of(aid)
        audio_b64, _mime, meta = speak(text, voice=voice, emotion=emotion,
                                       stem="intro_%s" % aid)
        wav_path = meta.get("audio_path")
        tmp_wav = ""
        if not (wav_path and os.path.exists(wav_path)):
            tmp_wav = os.path.join(INTRO_DIR, ".%s.intro.wav" % aid)
            with open(tmp_wav, "wb") as f:
                f.write(base64.b64decode(audio_b64))
            wav_path = tmp_wav
        kind, source = _intro_video_source(aid)
        source_label = "%s:%s" % (kind, os.path.basename(source)) if source else ""
        lipsync_tmp = ""
        photo = _intro_photo(aid)
        if photo and lipsync_ready():
            lipsync_tmp = os.path.join(INTRO_DIR, ".%s.lipsync.mp4" % aid)
            try:
                _intro_lipsync(aid, photo, wav_path, lipsync_tmp)
                kind, source = "lipsync", lipsync_tmp
                source_label = "lipsync:%s" % os.path.basename(photo)
            except Exception as e:                  # noqa: BLE001 - 口型失败回退静态画面
                log("口型视频不可用（%s），回退原有画面：%s" % (aid, e))
                if lipsync_tmp and os.path.exists(lipsync_tmp):
                    os.unlink(lipsync_tmp)
                lipsync_tmp = ""
        if not source:
            raise RuntimeError("员工 %s 既没有 talk 视频也没有照片，无法生成画面" % aid)
        duration = _probe_duration(wav_path)
        tmp_out = os.path.join(INTRO_DIR, ".%s.intro.mp4" % aid)
        if kind in ("video", "lipsync"):
            inputs = [FFMPEG_BIN, "-nostdin", "-y", "-loglevel", "error",
                      "-stream_loop", "-1", "-i", source, "-i", wav_path]
        else:
            inputs = [FFMPEG_BIN, "-nostdin", "-y", "-loglevel", "error",
                      "-loop", "1", "-framerate", "25", "-i", source, "-i", wav_path]
        cmd = inputs + [
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", "25", "-vf", _intro_filter(kind),
            "-c:a", "aac", "-b:a", "96k", "-ar", "24000", "-ac", "1",
            "-t", "%.3f" % max(1.0, duration),
            "-movflags", "+faststart", tmp_out]
        try:
            subprocess.run(cmd, check=True, timeout=300, env=FF_ENV,
                           capture_output=True, stdin=subprocess.DEVNULL)
            if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) < 4096:
                raise RuntimeError("ffmpeg 输出为空")
            os.replace(tmp_out, out)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode("utf-8", "replace")[-400:]
            raise RuntimeError("自我介绍视频合成失败：%s" % err)
        finally:
            for p in (tmp_wav, tmp_out, lipsync_tmp):
                if p and os.path.exists(p):
                    os.unlink(p)
        duration = _probe_duration(out) or duration
        meta_out = {"id": aid, "text": text, "duration": duration,
                    "source": source_label or "%s:%s" % (kind, os.path.basename(source)),
                    "pipeline": INTRO_PIPELINE,
                    "generated_at": time.time(),
                    "generate_ms": int((time.time() - started) * 1000),
                    "size": os.path.getsize(out)}
        with open(os.path.join(INTRO_DIR, "%s.json" % aid), "w", encoding="utf-8") as f:
            json.dump(meta_out, f, ensure_ascii=False)
        log("自我介绍视频已生成：%s（%.1fs，%d 字节）" % (
            aid, duration, meta_out["size"]))
        return {"id": aid, "name": prof.get("name") or aid, "ready": True, "cached": False,
                "video": url, "text": text, "duration": duration,
                "size": meta_out["size"], "source": meta_out["source"],
                "generate_ms": meta_out["generate_ms"]}


def intro_video(args):
    """按需生成自我介绍视频：id 单个 / all 全部；wait=false 时后台生成。"""
    ids = [args["id"]] if (args.get("id") or "").strip() else list(load_profiles())
    if args.get("id") and args["id"] not in load_profiles():
        raise ValueError("员工不存在：%s" % args["id"])
    force = bool(args.get("force"))
    wait = args.get("wait", True)
    if not wait:
        def bg():
            for aid in ids:
                try:
                    intro_build(aid, force)
                except Exception as e:              # noqa: BLE001
                    log("自我介绍后台生成失败（%s）：%s" % (aid, e))
        threading.Thread(target=bg, daemon=True).start()
        items = [{"id": aid, "queued": True,
                  "ready": os.path.exists(os.path.join(INTRO_DIR, "%s.mp4" % aid))}
                 for aid in ids]
        return {"count": len(ids), "queued": True, "dir": INTRO_DIR,
                "url_base": ASSET_URL_BASE or None, "items": items}
    items, failed = [], []
    for aid in ids:
        try:
            items.append(intro_build(aid, force))
        except Exception as e:                      # noqa: BLE001 - 单人失败不影响其他人
            log(traceback.format_exc())
            failed.append({"id": aid, "error": str(e)})
    return {"count": len(items), "dir": INTRO_DIR, "url_base": ASSET_URL_BASE or None,
            "items": items, "failed": failed}


def _intro_stale(aid):
    """成片缺失、流水线升级、或照片/文案比成片新，都需要重建。"""
    out = os.path.join(INTRO_DIR, "%s.mp4" % aid)
    if not os.path.exists(out):
        return True
    if (_intro_meta(aid).get("pipeline") or "") != INTRO_PIPELINE:
        return True
    out_m = os.path.getmtime(out)
    # 底图必须是成片真正用的那张（portraits 优先），否则换了半身像也不会重建
    photo = _intro_photo(aid)
    script = os.path.join(INTRO_DIR, "%s.txt" % aid)
    for p in (photo, script):
        if p and os.path.exists(p) and os.path.getmtime(p) > out_m:
            return True
    return False


def auto_intro_sweep():
    """全自动巡检：缺失/过期即重建，直接写进员工档案库，无审批、无发布环节。"""
    ids = [aid for aid in load_profiles() if _intro_stale(aid)]
    if not ids:
        return []
    log("自动重建自我介绍视频：%d 人 %s" % (len(ids), ",".join(ids)))
    for aid in ids:
        try:
            intro_build(aid, force=True, refresh_text=False)
        except Exception as e:                      # noqa: BLE001 - 单人失败不影响其他人
            log("自我介绍自动生成失败（%s）：%s" % (aid, e))
    return ids


def _auto_intro_loop():
    time.sleep(20)                                  # 等档案/TTS 就绪
    while True:
        try:
            auto_intro_sweep()
        except Exception:                           # noqa: BLE001
            log(traceback.format_exc())
        time.sleep(AUTO_INTRO_INTERVAL)



# ---------------------------------------------------------------- 实时通话（HLS 直播）
# 一通通话 = 一个 ffmpeg 进程：员工画面（talk 视频循环 / 照片）作为视频源，命名管道作为
# 音频源。不说话时写静音，说话时按实时速度把 TTS 的 PCM 写进管道，ffmpeg 立刻切成 1 秒
# 分片并滚动更新 index.m3u8。音视频在同一条流里复用，天生同步；客户端 MediaPlayer 播
# m3u8 即可，无需 WebRTC 或第三方 SDK。
LIVE_RATE = 24000
LIVE_CHUNK_SEC = 0.02
# 员工侧电平时间轴：与 HLS 用同一条媒体时间轴（写进管道的累计秒数），每 100 ms 一格 0~100。
# 客户端按「起播分片序号 × segment_sec + 播放进度」换算成媒体时间后查表，就能和听到的声音同步。
LIVE_LEVEL_ENABLE = os.environ.get("ASR_LIVE_LEVEL", "1") not in ("0", "false", "False", "no")
LIVE_LEVEL_FPS = max(1, int(os.environ.get("ASR_LIVE_LEVEL_FPS", "10")))
LIVE_LEVEL_WINDOW_SEC = max(10, int(os.environ.get("ASR_LIVE_LEVEL_WINDOW", "120")))
LIVE_LEVEL_FLUSH_SEC = float(os.environ.get("ASR_LIVE_LEVEL_FLUSH_SEC", "0.2"))
_LIVE = {}
_LIVE_LOCK = threading.Lock()


def _pcm_level(block):
    """s16le 单声道一小段 → 0~100 电平（RMS 映射到 -50~-6 dBFS），静音返回 0。"""
    n = len(block) // 2
    if n <= 0:
        return 0
    total = 0
    for v in memoryview(block).cast("h"):
        total += v * v
    rms = math.sqrt(total / n) / 32768.0
    if rms <= 0.003:
        return 0
    db = 20 * math.log10(min(1.0, rms))
    return int(max(0, min(100, round((db + 50) / 44 * 100))))


class LiveSession:
    def __init__(self, sid, aid, kind, source):
        self.sid = sid
        self.aid = aid
        self.kind = kind
        self.source = source
        self.out_dir = os.path.join(LIVE_DIR, sid)
        self.playlist = os.path.join(self.out_dir, "index.m3u8")
        self.fifo = os.path.join(self.out_dir, "audio.pcm")
        self.started_at = time.time()
        self.ended_at = 0.0
        self.last_speech_at = time.time()             # 最近一次出声时间，空闲提醒用
        self.last_input_at = time.time()              # 老板最近一次下指令的时间，简报从这时算起
        # 有新指令就 set：空闲看门狗被立刻唤醒重算，不做无脑定时轮询
        self._activity = threading.Event()
        self.briefing = {}                            # 起流时后台预生成的工作简报（文字 + 语音）
        self.speech_seconds = 0.0
        self.chunks = 0
        self.proc = None
        self._q = queue.Queue(maxsize=600)           # 最多缓冲 12 秒语音，防止失控堆积
        self._stop = threading.Event()
        self._log = None
        # 员工侧电平：level.json 里是「媒体时间轴 + 100ms 一格 0~100」的滚动窗口
        self.level_path = os.path.join(self.out_dir, "level.json")
        self._lv = []                                # [(media_sec, level)]，窗口内按时间递增
        self._lv_seq = 0
        self._lv_media = 0.0                         # 已写进管道的累计媒体秒数（与 HLS 同轴）
        self._lv_carry = b""                         # 不足 100 ms 的残留 PCM
        self._lv_dirty = False
        self._lv_last_flush = 0.0
        self._lv_lock = threading.Lock()

    # ---- 启动 ----
    def start(self):
        _ensure_dir(LIVE_DIR)
        _ensure_dir(self.out_dir)
        if os.path.exists(self.fifo):
            os.unlink(self.fifo)
        os.mkfifo(self.fifo, 0o666)
        self._log = open(os.path.join(self.out_dir, "ffmpeg.log"), "wb", buffering=0)
        if self.kind == "audio":
            # 纯语音通话：滑动窗口直播列表（只保留最近几个分片），播放器从「直播边缘」开始播，
            # 端到端延迟约 2~3 秒。不能用 event + hls_list_size 0：那会让播放器从第 0 片播起，
            # 通话久了就落后一大截，听不到后面混进来的回复（2026-11-10 通话「接通无声」的根因之一）。
            # 不给 -hls_playlist_type（缺省就是直播列表，不写 EVENT/VOD 标记）
            hls_out = [
                "-f", "hls", "-hls_time", str(LIVE_SEGMENT_SEC), "-hls_list_size", "5",
                "-hls_flags", "independent_segments",
                "-hls_segment_filename", os.path.join(self.out_dir, "seg_%05d.ts"),
                "-flush_packets", "1", self.playlist]
            # 不出视频轨，只把麦克风/回复语音按实时速度推成 HLS 音频流
            cmd = [FFMPEG_BIN, "-nostdin", "-y", "-loglevel", "error",
                   "-f", "s16le", "-ar", str(LIVE_RATE), "-ac", "1", "-i", self.fifo,
                   "-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "2"] + hls_out
        else:
            hls_out = [
                "-f", "hls", "-hls_time", str(LIVE_SEGMENT_SEC), "-hls_list_size", "0",
                "-hls_playlist_type", "event", "-hls_flags", "independent_segments",
                "-hls_segment_filename", os.path.join(self.out_dir, "seg_%05d.ts"),
                "-flush_packets", "1", self.playlist]
            if self.kind == "video":
                vin = ["-re", "-stream_loop", "-1", "-i", self.source]
            else:
                vin = ["-re", "-loop", "1", "-framerate", "25", "-i", self.source]
            cmd = [FFMPEG_BIN, "-nostdin", "-y", "-loglevel", "error"] + vin + [
                "-f", "s16le", "-ar", str(LIVE_RATE), "-ac", "1", "-i", self.fifo,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-r", "25",
                "-g", "25", "-keyint_min", "25", "-sc_threshold", "0",
                "-vf", _intro_filter(self.kind),
                "-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "2"] + hls_out
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=self._log, stderr=self._log, env=FF_ENV)
        threading.Thread(target=self._writer_loop, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        self._level_flush()          # 先落一个空时间轴，客户端起播就能拉到 200，不用等第一个分片
        log("实时通话开始：%s（员工 %s，形态 %s%s）" % (
            self.sid, self.aid, self.kind,
            "" if self.kind == "audio" else "，画面 %s" % os.path.basename(self.source)))
        return self

    def _watch(self):
        code = self.proc.wait()
        if not self._stop.is_set():
            log("实时通话 %s 的 ffmpeg 提前退出（code=%s）" % (self.sid, code))
            # 编码进程已经没了，这条会话再也发不出声音：就地收尾，否则槽位永远不释放
            self._stop.set()
            self.ended_at = time.time()
            self._finalize()

    def _level_add(self, pcm):
        """把刚写进管道的 PCM 折算成 100 ms 一档的电平，媒体时间轴与 HLS 完全同轴。"""
        if not LIVE_LEVEL_ENABLE or not pcm:
            return
        frame_bytes = max(2, int(LIVE_RATE / LIVE_LEVEL_FPS) * 2)
        data = self._lv_carry + pcm
        n = len(data) // frame_bytes
        if n:
            with self._lv_lock:
                base = self._lv_media
                for i in range(n):
                    blk = data[i * frame_bytes:(i + 1) * frame_bytes]
                    self._lv.append((round(base + i / LIVE_LEVEL_FPS, 3), _pcm_level(blk)))
                self._lv_media = base + n / LIVE_LEVEL_FPS
                keep = LIVE_LEVEL_WINDOW_SEC * LIVE_LEVEL_FPS
                if len(self._lv) > keep:
                    self._lv = self._lv[-keep:]
            self._lv_carry = data[n * frame_bytes:]
            self._lv_dirty = True
        else:
            self._lv_carry = data
        if self._lv_dirty and time.time() - self._lv_last_flush >= LIVE_LEVEL_FLUSH_SEC:
            self._level_flush()

    def _level_flush(self, final=False):
        """原子重写 level.json（先写 .tmp 再 replace），播放器轮询永远读到完整 JSON。"""
        if not LIVE_LEVEL_ENABLE:
            return
        try:
            with self._lv_lock:
                lv = self._lv
                payload = {
                    "session_id": self.sid,
                    "seq": self._lv_seq,
                    "fps": LIVE_LEVEL_FPS,
                    "t0": lv[0][0] if lv else 0.0,
                    "lv": [p[1] for p in lv],
                    "edge": round(self._lv_media, 3),
                    "wall": round(time.time(), 3),
                    "elapsed": round((self.ended_at or time.time()) - self.started_at, 2),
                    "status": "ended" if (final or self._stop.is_set()) else "live",
                }
            self._lv_seq += 1
            tmp = self.level_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.level_path)
            self._lv_dirty = False
            self._lv_last_flush = time.time()
        except OSError as e:
            log("实时通话 %s 电平文件写入失败：%s" % (self.sid, e))

    # ---- 音频写入：静音兜底 + 按实时速度推送 ----
    def _writer_loop(self):
        deadline = time.time() + 15
        fd = None
        while not self._stop.is_set() and time.time() < deadline and fd is None:
            try:
                fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                time.sleep(0.05)
        if fd is None:
            log("实时通话 %s：ffmpeg 未打开音频管道，停止推音频" % self.sid)
            return
        os.set_blocking(fd, True)
        silence = b"\x00\x00" * int(LIVE_RATE * LIVE_CHUNK_SEC)
        next_t = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    data = self._q.get(timeout=LIVE_CHUNK_SEC)
                except queue.Empty:
                    data = silence
                for i in range(0, max(1, len(data)), len(silence)):
                    if self._stop.is_set():
                        break
                    piece = data[i:i + len(silence)]
                    os.write(fd, piece)
                    self._level_add(piece)
                    next_t += len(piece) / 2.0 / LIVE_RATE
                    delay = next_t - time.monotonic()
                    if delay > 0:
                        time.sleep(min(delay, 0.5))
                    elif delay < -1.0:
                        next_t = time.monotonic()   # 落后太多就重新对齐
        except (BrokenPipeError, OSError) as e:
            log("实时通话 %s 音频写入结束：%s" % (self.sid, e))
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _pcm_from_wav(self, wav_path, measured=None):
        af = []
        if measured:
            # 已测得本句响度：用线性模式把整句整体拉到目标响度，不做逐段动态压限，
            # 短句不会有忽大忽小的「抽气」感
            chain = ("loudnorm=I=%s:TP=%s:LRA=11:measured_I=%s:measured_TP=%s:"
                     "measured_LRA=%s:measured_thresh=%s:offset=%s:linear=true"
                     % (LOUDNESS_I, LOUDNESS_TP, measured.get("input_i"),
                        measured.get("input_tp"), measured.get("input_lra"),
                        measured.get("input_thresh"), measured.get("target_offset")))
            if LIVE_GAIN_DB:
                chain += ",volume=%sdB,%s" % (LIVE_GAIN_DB, _LIMITER)
            af = ["-af", chain]
        elif LIVE_GAIN_DB:
            # 量不出响度（极短句/静音）也保证有增益与限幅，别一句响一句小
            af = ["-af", "volume=%sdB,%s" % (LIVE_GAIN_DB, _LIMITER)]
        out = subprocess.run(
            [FFMPEG_BIN, "-nostdin", "-y", "-loglevel", "error", "-i", wav_path] + af + [
             "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(LIVE_RATE), "-"],
            capture_output=True, timeout=120, env=FF_ENV, stdin=subprocess.DEVNULL)
        if out.returncode != 0:
            raise RuntimeError("语音转 PCM 失败：%s" % (out.stderr or b"")[-300:])
        return out.stdout

    def _measure_loudness(self, wav_path):
        """先量一遍本句的响度，给第二遍线性归一化用（TTS 音量不稳定，直接播会忽大忽小）。"""
        out = subprocess.run(
            [FFMPEG_BIN, "-nostdin", "-hide_banner", "-nostats", "-i", wav_path,
             "-af", "loudnorm=I=%s:TP=%s:LRA=11:print_format=json" % (LOUDNESS_I, LOUDNESS_TP),
             "-f", "null", "-"],
            capture_output=True, timeout=120, env=FF_ENV, stdin=subprocess.DEVNULL)
        txt = out.stderr.decode("utf-8", "ignore")
        i, j = txt.rfind("{"), txt.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            m = json.loads(txt[i:j + 1])
        except ValueError:
            return None
        if not m.get("input_i") or m.get("input_i") in ("-inf", "inf"):
            return None
        return m

    def say_pcm(self, pcm):
        if self._stop.is_set() or not pcm:
            return 0.0
        seconds = len(pcm) / 2.0 / LIVE_RATE
        self._q.put(pcm)
        self.speech_seconds = round(self.speech_seconds + seconds, 2)
        self.last_speech_at = time.time()
        self.chunks += 1
        return round(seconds, 2)

    def say_wav_b64(self, audio_b64):
        if not audio_b64:
            return 0.0
        tmp = os.path.join(self.out_dir, ".say_%s.wav" % os.urandom(3).hex())
        try:
            with open(tmp, "wb") as f:
                f.write(base64.b64decode(audio_b64))
            if not LIVE_LOUDNORM:
                return self.say_pcm(self._pcm_from_wav(tmp))
            try:
                pcm = self._pcm_from_wav(tmp, self._measure_loudness(tmp))
            except Exception as e:                  # noqa: BLE001
                # 极短句/静音句 loudnorm 可能量不出响度：退回原始音量，别把这一句丢掉
                log("响度归一化跳过（%s）：%s" % (self.sid, e))
                pcm = self._pcm_from_wav(tmp)
            return self.say_pcm(pcm)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---- 停止与展示 ----
    def stop(self):
        if self._stop.is_set():
            return self.view()
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.ended_at = time.time()
        self._finalize()
        self._level_flush(final=True)
        log("实时通话结束：%s（%.1fs，语音 %.1fs）" % (
            self.sid, self.ended_at - self.started_at, self.speech_seconds))
        return self.view()

    def _finalize(self):
        """确保播放列表以 ENDLIST 收尾，客户端能正常结束而不是一直转圈。"""
        try:
            with open(self.playlist, encoding="utf-8") as f:
                body = f.read()
            if "#EXT-X-ENDLIST" not in body:
                with open(self.playlist, "a", encoding="utf-8") as f:
                    f.write("#EXT-X-ENDLIST\n")
        except OSError:
            pass

    def segments(self):
        try:
            return sum(1 for n in os.listdir(self.out_dir) if n.endswith(".ts"))
        except OSError:
            return 0

    def view(self):
        live = not self._stop.is_set()
        return {"session_id": self.sid, "status": "live" if live else "ended",
                "avatar_id": self.aid,
                "mode": "audio" if self.kind == "audio" else "video",
                "hls_url": _asset_url("live/%s" % self.sid, "index.m3u8"),
                "hls_url_full": _asset_url_full("live/%s" % self.sid, "index.m3u8"),
                "level_url": _asset_url("live/%s" % self.sid, "level.json"),
                "level_url_full": _asset_url_full("live/%s" % self.sid, "level.json"),
                "level_fps": LIVE_LEVEL_FPS,
                "level_window_sec": LIVE_LEVEL_WINDOW_SEC,
                "segment_sec": LIVE_SEGMENT_SEC,
                "started_at": self.started_at,
                "elapsed": round((self.ended_at or time.time()) - self.started_at, 2),
                "speech_seconds": self.speech_seconds,
                "briefing_ready": bool((self.briefing or {}).get("audio_base64")),
                "segments": self.segments(),
                "sync": ("纯音频 HLS 直播流（AAC），不说话时推静音保持连接"
                         if self.kind == "audio" else
                         "音视频同流复用（AAC+H.264），由 ffmpeg 统一打时间戳")}

    def destroy(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
        except OSError:
            pass
        try:
            if self._log:
                self._log.close()
        except OSError:
            pass
        shutil.rmtree(self.out_dir, ignore_errors=True)


def _live_get(sid):
    if not sid:
        return None
    with _LIVE_LOCK:
        return _LIVE.get(sid)


def _live_prune(force_all=False):
    """清掉挂断超时的会话目录，避免 assets/live 无限增长。"""
    now = time.time()
    dead = []
    with _LIVE_LOCK:
        for sid, s in list(_LIVE.items()):
            if s._stop.is_set() and (force_all or now - s.ended_at > LIVE_TTL):
                dead.append(s)
                _LIVE.pop(sid, None)
    for s in dead:
        s.destroy()


def _live_cleanup_stale_dirs():
    """启动时清掉上次进程遗留的通话分片目录（没有会话认领，只会越积越多）。"""
    if not LIVE_DIR or not os.path.isdir(LIVE_DIR):
        return
    cutoff = time.time() - (LIVE_TTL + LIVE_HANGUP_SEC)
    removed = 0
    for name in os.listdir(LIVE_DIR):
        path = os.path.join(LIVE_DIR, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    if removed:
        log("清理遗留通话分片目录 %d 个（超过 %.0fs 未更新）" % (removed, LIVE_TTL + LIVE_HANGUP_SEC))


def _live_greet(session):
    """接通先打招呼：命中预合成缓存立刻混进第一片，没命中就后台补合成再补进流。

    老板反馈「接通后没声音、怀疑没连上」，所以这里不做「有语音才出声」，
    宁可多一句问候，也不能让通话头几秒是静音。
    """
    if not GREETING_TEXTS:
        return
    prof = load_profiles().get(session.aid) or {}
    voice = prof.get("voice") or DEFAULT_VOICE
    emotion = norm_emotion(prof.get("emotion")) or None
    phrase = GREETING_TEXTS[0]
    item = _ack_cached(phrase, voice=voice, emotion=emotion)
    if item and item.get("audio_base64"):
        try:
            secs = session.say_wav_b64(item["audio_base64"])
            log("接通问候 %s：%r（%.1fs，voice=%s）" % (session.sid, phrase, secs or 0.0, voice))
            return
        except Exception as e:                      # noqa: BLE001
            log("接通问候混流失败（%s）：%s" % (session.sid, e))
    # 未预热或 TTS 还没就绪：后台合成，好了立刻补进流里（同「收到」超时兜底）
    _live_ack_later(session.sid, phrase, voice, emotion)


def _briefing_lines(path, n=120):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def _clip(text, limit):
    s = re.sub(r"\s+", " ", (text or "")).strip()
    return s if len(s) <= limit else s[:limit].rstrip("，,、；; ") + "……"


def _clip_sentence(text, limit):
    """按标点收口截断：断在半句中间（「已驳回返……」）念出来很难听。"""
    s = re.sub(r"\s+", " ", (text or "")).strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for sep in ("，", ",", "；", ";", "。", "、"):
        k = cut.rfind(sep)
        if k >= limit // 2:
            return cut[:k]
    return cut


def _snapshot_age_hint(saved_at):
    """快照过旧就明说，别让老板把几小时前的数据当成实时进度。"""
    try:
        t = time.mktime(time.strptime((saved_at or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return ""
    age = time.time() - t
    if age < 7200:
        return ""
    if age < 86400:
        return "（快照是 %d 小时前的）" % int(age // 3600)
    return "（快照是 %d 天前的）" % int(age // 86400)


def build_work_briefing():
    """把当前任务和状态压成一条可朗读的短简报。

    只读本地快照（runtime.json / 事件流.md / 交付物日志），毫秒级出结果，不调大模型：
    老板等着听的时候再去跑 agent，等上几分钟就谈不上「快速简报」了。
    数据读不到就返回空串，调用方退回「老板，我在呢」。
    """
    root = BRIEFING_DIR
    try:
        with open(os.path.join(root, "动态", "runtime.json"), "r", encoding="utf-8") as f:
            runtime = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(runtime, dict):
        return ""
    stats = runtime.get("stats") or {}
    approvals = runtime.get("approvals") or []
    reports = runtime.get("reports") or []
    pending = [a for a in approvals if (a.get("status") or "") == "pending"]
    high = [a for a in pending if (a.get("risk") or "").lower() in ("high", "critical")]
    parts = ["跑了 %d 条任务链路，已发布 %d、阻断 %d"
             % (int(stats.get("traces") or 0), int(stats.get("published") or 0),
                int(stats.get("blockedPublish") or 0))]
    if pending:
        parts.append("%d 项待您拍板%s"
                     % (len(pending), "，其中 %d 项高风险" % len(high) if high else ""))
    if reports:
        last = reports[-1]
        title = re.sub(r"^风险单\s*[：:]\s*", "", str(last.get("title") or ""))
        conclusion = _clip_sentence(last.get("conclusion"), 24)
        parts.append("最新问题是%s%s"
                     % (_clip(title, 20), "，" + conclusion if conclusion else ""))
    # 事件流里升级给老板的事项：同一句会重复记多次，按内容去重
    esc = set()
    for line in _briefing_lines(os.path.join(root, "动态", "事件流.md")):
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 5 and "escalation" in cells[2]:
            esc.add(_clip(cells[4], 30))
    if esc:
        parts.append("另有 %d 条升级等您定夺" % len(esc))
    # 交付物：最近一天内有更新的目录数
    try:
        names = os.listdir(os.path.join(root, "OPC", "交付物"))
    except OSError:
        names = []
    recent = 0
    for name in names:
        try:
            if time.time() - os.path.getmtime(
                    os.path.join(root, "OPC", "交付物", name, "06-log.md")) < 86400:
                recent += 1
        except OSError:
            continue
    if recent:
        parts.append("最近一天有 %d 个交付物更新" % recent)
    # 语音简报要短：TTS 只有实时的 1/2~1/6，说上两分钟就得合成好几分钟，等不起。
    # 超长时整段丢掉末尾的事项（交付物 → 升级 → 风险），不要拦腰截断成「4 个交付物更……」这种半句
    head = "老板，没新指令，我报下进度："
    tail = "。" + _snapshot_age_hint(runtime.get("savedAt")) + "要细说哪块，您吩咐。"
    while len(parts) > 1 and len(head + "；".join(parts) + tail) > BRIEFING_MAX_CHARS:
        parts.pop()
    return head + "；".join(parts) + tail


def _briefing_prewarm(session):
    """起流时后台备好简报文字和语音：真到空闲触发那一刻，直接入流，不用现场等 TTS。"""
    def run():
        t0 = time.time()
        try:
            text = build_work_briefing()
        except Exception as e:                      # noqa: BLE001
            log("工作简报生成失败（%s）：%s" % (session.sid, e))
            return
        if not text:
            log("工作简报跳过（%s）：没读到当前任务数据" % session.sid)
            return
        prof = load_profiles().get(session.aid) or {}
        voice = prof.get("voice") or DEFAULT_VOICE
        emotion = norm_emotion(prof.get("emotion")) or None
        try:
            b64, _mime, _meta = _tts_timed(text, voice, BRIEFING_TTS_TIMEOUT, emotion=emotion)
        except Exception as e:                      # noqa: BLE001
            log("工作简报合成失败（%s）：%s" % (session.sid, e))
            return
        session.briefing = {"text": text, "audio_base64": b64}
        log("工作简报就绪（%s，%.1fs，%d 字）：%s"
            % (session.sid, time.time() - t0, len(text), text))
    threading.Thread(target=run, daemon=True).start()


def _session_pending_jobs(sid):
    """这个通话会话里还没跑完的指令（排队 + 正在跑）。"""
    with _JOBS_LOCK:
        jobs = list(_JOBS.values())
    pending = []
    for job in jobs:
        live = job.get("live") or {}
        if live.get("session_id") != sid:
            continue
        if job.get("status") in ("accepted", "running"):
            pending.append(job)
    return pending


def _pending_report_text(jobs):
    """有指令在处理时，空闲汇报先说指令进度，而不是泛泛报全局任务。"""
    parts = []
    for job in jobs[:3]:
        q = _clip((job.get("question") or "").strip(), 18) or "一条指令"
        parts.append("%s（%s）" % (q, "排队中" if job.get("status") == "accepted" else "处理中"))
    more = "，另有 %d 条" % (len(jobs) - 3) if len(jobs) > 3 else ""
    return ("老板，我还在处理 %d 条指令：%s%s。处理完马上报给您。"
            % (len(jobs), "；".join(parts), more))


def _say_idle_briefing(session, voice, emotion):
    """空闲够久时主动汇报：有待处理指令→报指令进度；没有指令→报任务情况。

    返回 True 表示这一轮已经说过（或正在说），False 表示素材还没准备好、下一轮再试。
    """
    pending = _session_pending_jobs(session.sid)
    if pending:
        text = _pending_report_text(pending)
        try:
            item = _tts_timed(text, voice, BRIEFING_TTS_TIMEOUT, emotion=emotion)
        except Exception as e:                      # noqa: BLE001
            log("指令进度汇报合成失败（%s）：%s" % (session.sid, e))
            return False
        b64 = (item or {}).get("audio_base64")
        if not b64:
            return False
        session.say_wav_b64(b64)
        session.last_speech_at = time.time()
        log("指令进度汇报 %s：%r" % (session.sid, text))
        return True
    item = session.briefing or {}
    if not item.get("audio_base64"):
        return False                                 # 简报还没合成好，下一轮再试
    session.say_wav_b64(item["audio_base64"])
    session.last_speech_at = time.time()
    log("工作简报 %s：%r" % (session.sid, item.get("text")))
    return True


def _live_idle_watch(session):
    """长时间没有新指令：先搭一句「我在」，再久就把当前指令/任务状态念成语音简报，
    超过 LIVE_HANGUP_SEC 还没动静就自动收线（App 被杀/断网时不会发 stop）。

    计时以「老板最后一次下指令」为起点，且由事件驱动：每来一条新指令就唤醒重算，
    不再固定每 5 秒轮询一遍。
    """
    if IDLE_NUDGE_SEC <= 0 and not BRIEFING_ENABLED and LIVE_HANGUP_SEC <= 0:
        return
    prof = load_profiles().get(session.aid) or {}
    voice = prof.get("voice") or DEFAULT_VOICE
    emotion = norm_emotion(prof.get("emotion")) or None
    phrase = GREETING_TEXTS[-1] if len(GREETING_TEXTS) >= 2 else ""
    briefed = False
    last_nudge = 0.0
    while not session._stop.is_set():
        # 算出「下一个真正要做事的时刻」，只睡到那里；中途老板一说话就 event 唤醒重算
        points = []
        if IDLE_NUDGE_SEC > 0:
            gap = max(IDLE_NUDGE_SEC, IDLE_NUDGE_REPEAT_SEC)
            points.append((last_nudge or session.last_input_at) + (gap if last_nudge else IDLE_NUDGE_SEC))
        if BRIEFING_ENABLED and not briefed and BRIEFING_SEC > 0:
            points.append(session.last_input_at + BRIEFING_SEC)
        if LIVE_HANGUP_SEC > 0:
            points.append(session.last_input_at + LIVE_HANGUP_SEC)
        wait = (min(points) - time.time()) if points else 30.0
        session._activity.wait(max(0.5, min(wait, 30.0)))
        session._activity.clear()
        if session._stop.is_set():
            return
        # 空闲时长按「老板最后一次下指令」算：自己说的话不算，否则每句提醒都会把计时清零
        idle = time.time() - session.last_input_at
        # 太久没动静（App 被杀 / 断网 / 忘了挂断）：服务端收线，释放会话槽位和 ffmpeg。
        # 刚说过话（简报/提醒还在播）不算闲置，避免念到一半被自己挂断。
        still_talking = time.time() - session.last_speech_at < 30
        if LIVE_HANGUP_SEC > 0 and idle >= LIVE_HANGUP_SEC and not still_talking:
            log("实时通话 %s 空闲 %.0fs，超过 %.0fs 自动收线"
                % (session.sid, idle, LIVE_HANGUP_SEC))
            session.stop()
            return
        if idle < IDLE_NUDGE_SEC:
            continue
        if not session._q.empty():                  # 还有语音在排队，别插话
            continue
        # 久到该汇报：有指令在处理就先报进度，没有才报任务情况（语音现场合成后入流）
        if BRIEFING_ENABLED and not briefed and BRIEFING_SEC > 0 and idle >= BRIEFING_SEC:
            briefed = True                          # 只报一次；失败也不再重试，避免 TTS 风暴
            try:
                if not _say_idle_briefing(session, voice, emotion):
                    log("空闲汇报跳过（%s）：音频未就绪" % session.sid)
            except Exception as e:                  # noqa: BLE001
                log("空闲汇报混流失败（%s）：%s" % (session.sid, e))
            last_nudge = time.time()                # 汇报即当一次出声，别再紧跟一句「我在」
            continue
        if not phrase or time.time() - last_nudge < max(IDLE_NUDGE_SEC, IDLE_NUDGE_REPEAT_SEC):
            continue
        item = _ack_cached(phrase, voice=voice, emotion=emotion)
        last_nudge = time.time()
        if not item or not item.get("audio_base64"):
            continue
        try:
            secs = session.say_wav_b64(item["audio_base64"])
            log("空闲提醒 %s：%r（%.1fs）" % (session.sid, phrase, secs or 0.0))
        except Exception as e:                      # noqa: BLE001
            log("空闲提醒混流失败（%s）：%s" % (session.sid, e))


def _live_start(args):
    if not (FFMPEG_BIN and LIVE_DIR):
        raise ValueError("服务端未配置素材目录或缺少 ffmpeg，无法发起实时通话")
    _live_prune()
    # 上限不再直接拒绝：App 被杀/断网不会发 stop，残留会话会把 8 个名额占死
    # （历史日志 192 次「已达上限」）。改成先回收最久没说话的那通电话。
    evict = None
    with _LIVE_LOCK:
        active = [s for s in _LIVE.values() if not s._stop.is_set()]
        if len(active) >= LIVE_SESSIONS_MAX:
            oldest = min(active, key=lambda s: s.last_input_at)
            idle = time.time() - oldest.last_input_at
            if idle < LIVE_EVICT_IDLE:
                raise ValueError("实时通话会话已达上限 %d，请先挂断" % LIVE_SESSIONS_MAX)
            evict = (oldest, idle)
    if evict:
        session_old, idle = evict
        log("实时通话会话已达上限 %d，回收最久空闲的 %s（空闲 %.0fs）" % (
            LIVE_SESSIONS_MAX, session_old.sid, idle))
        try:
            session_old.stop()
        except Exception as e:                       # noqa: BLE001
            log("回收空闲通话 %s 失败：%s" % (session_old.sid, e))
    # 默认播报人＝OPC-001 小秘书（老板影子分身）；未命中时退回 ceo
    aid = (args.get("avatar_id") or args.get("id")
           or (SECRETARY_ID if _profile_ready(SECRETARY_ID) else "ceo")).strip()
    # mode=audio 或明确不要画面：纯语音通话，不依赖照片/视频素材
    audio_only = (str(args.get("mode") or "").lower() == "audio"
                  or args.get("audio_only") is True or args.get("video") is False)
    if audio_only:
        kind, source = "audio", ""
    else:
        kind, source = _intro_video_source(aid)
        if not source:
            raise ValueError("员工 %s 没有可用的画面素材（talk/idle 视频或照片）" % aid)
    sid = "lv_%s" % os.urandom(4).hex()
    session = LiveSession(sid, aid, kind, source).start()
    with _LIVE_LOCK:
        _LIVE[sid] = session
    # 接通先打招呼（趁等分片的空档混进最前面的分片），长时间不说话再主动搭一句
    _live_greet(session)
    # 同时后台把「当前任务/状态」简报文字和语音备好，空闲触发时直接念，不用现场等 TTS
    if BRIEFING_ENABLED:
        _briefing_prewarm(session)
    threading.Thread(target=_live_idle_watch, args=(session,), daemon=True).start()
    # 首次出片要等 1~2 个分片，稍等一下再返回，客户端播放器少一次 404/空列表
    first = float(args.get("first_segment_sec") or 8)
    deadline = time.time() + max(1.0, first)
    while time.time() < deadline:
        try:
            with open(session.playlist, "rb") as f:
                if f.read().count(b"#EXTINF") >= 2:
                    break
        except OSError:
            pass
        time.sleep(0.1)
    view = session.view()
    view["ready"] = os.path.exists(session.playlist)
    if args.get("text"):
        view["speech_seconds"] = _live_say_text(session, args["text"], args)
    return view


def _live_say_text(session, text, args=None):
    args = args or {}
    aid = session.aid
    prof = load_profiles().get(aid) or {}
    voice = args.get("voice") or prof.get("voice") or DEFAULT_VOICE
    emotion = emotion_of(aid, override=args.get("emotion"))
    audio_b64, _mime, _meta = speak(text[:MAX_TTS_CHARS], voice=voice, emotion=emotion,
                                    stem="live_%s" % os.urandom(4).hex())
    return session.say_wav_b64(audio_b64)


def _live_touch(sid):
    """记下老板最近一次下指令的时间：空闲提醒与工作简报都从这里算起。

    同时唤醒空闲看门狗，让它按新起点重算等待时长（事件驱动，不用固定轮询）。
    """
    session = _live_get(sid) if sid else None
    if session:
        session.last_input_at = time.time()
        session._activity.set()


def _live_append_b64(sid, audio_b64):
    """把一段语音混进正在进行的通话（语音与画面在同一路流里，自动同步）。"""
    if not sid or not audio_b64:
        return None
    session = _live_get(sid)
    if not session:
        log("实时通话会话 %s 不存在，跳过混流" % sid)
        return None
    if session._stop.is_set():
        # 已挂断：回复无法再推进流里，客户端应从 voice_job 拿到结果自己播
        log("实时通话 %s 已挂断（%.1fs），本次语音未混入，请用 voice_job 播放"
            % (sid, session.ended_at - session.started_at))
        return None
    try:
        return session.say_wav_b64(audio_b64)
    except Exception as e:                          # noqa: BLE001 - 混流失败不影响主流程
        log("实时通话混流失败（%s）：%s" % (sid, e))
        return None


def _live_ack_later(sid, text, voice=None, emotion=None):
    """「收到」语音同步超时时的兜底：后台慢慢合成，好了直接补混进直播流。

    本机 TTS 合成速度只有实时的 1/2~1/6（2.3s 语音要 6~16s），8s 同步超时经常来不及，
    以前超时就把语音丢了，通话里只剩文字、一句声音都没有。
    """
    def run():
        t0 = time.time()
        try:
            item = ack_audio(text, voice=voice, timeout=ACK_TTS_LATE, emotion=emotion)
        except Exception as e:                      # noqa: BLE001
            log("「收到」语音补合成失败（%r）：%s" % (text, e))
            return
        secs = _live_append_b64(sid, (item or {}).get("audio_base64"))
        log("「收到」语音补混入 %s：%.1fs（合成 %.1fs，%r）"
            % (sid, secs or 0.0, time.time() - t0, text))
    threading.Thread(target=run, daemon=True).start()


def live_call(args):
    """实时通话：start 起流 / say 说话 / stop 挂断 / status 查看。"""
    action = (args.get("action") or "start").strip().lower()
    if action == "start":
        return _live_start(args)
    sid = (args.get("session_id") or "").strip()
    session = _live_get(sid)
    if not session:
        _live_prune()
        raise ValueError("实时通话会话不存在或已过期：%s" % sid)
    if action == "say":
        if args.get("audio_base64") or args.get("audio_path"):
            path = args.get("audio_path")
            if path and not os.path.exists(path):
                raise ValueError("音频文件不存在：%s" % path)
            if not path:
                path = os.path.join(session.out_dir, ".say_%s.wav" % os.urandom(3).hex())
                with open(path, "wb") as f:
                    f.write(base64.b64decode(args.get("audio_base64") or ""))
            try:
                seconds = session.say_pcm(session._pcm_from_wav(path))
            finally:
                if path.startswith(session.out_dir) and os.path.exists(path):
                    os.unlink(path)
            return dict(session.view(), queued_seconds=seconds)
        if args.get("text"):
            seconds = _live_say_text(session, args["text"], args)
            return dict(session.view(), queued_seconds=seconds)
        raise ValueError("say 需要 text 或 audio_base64 / audio_path")
    if action == "stop":
        return session.stop()
    if action == "status":
        return session.view()
    raise ValueError("未知 action：%s（可用 start/say/stop/status）" % action)


# ---------------------------------------------------------------- 异步任务
_JOBS = {}
_JOBS_LOCK = threading.Lock()
# client_id → (job_id, 绑定时间)：App 断网重发同一段录音时直接复用原任务，不重复执行
_CLIENT_JOBS = {}
_CLIENT_LOCK = threading.Lock()
# 有界任务队列：worker 数固定为 JOB_WORKERS，超出的任务排队等
_JOB_Q = queue.Queue()


def _job_view(job):
    view = {k: v for k, v in job.items() if not k.startswith("_")}
    if job.get("status") == "accepted" and job.get("_queued_at"):
        view["queue_ahead"] = _jobs_ahead(job["job_id"])
    return view


def _jobs_ahead(job_id):
    """当前排队中、排在这个任务前面的数量（App 用来显示「前面还有 N 条」）。"""
    try:
        items = list(_JOB_Q.queue)
    except AttributeError:
        return 0
    for i, item in enumerate(items):
        if item[0] == job_id:
            return i
    return 0


def _client_bind(client_id, job_id):
    if not client_id:
        return
    now = time.time()
    with _CLIENT_LOCK:
        _CLIENT_JOBS[client_id] = (job_id, now)
        for cid, (_jid, ts) in list(_CLIENT_JOBS.items()):
            if now - ts > CLIENT_TTL:
                _CLIENT_JOBS.pop(cid, None)


def _client_job(client_id):
    """按 client_id 找回原任务；任务已过期/清理则返回 None（允许重跑）。"""
    if not client_id:
        return None
    with _CLIENT_LOCK:
        item = _CLIENT_JOBS.get(client_id)
    if not item:
        return None
    jid, ts = item
    if time.time() - ts > CLIENT_TTL:
        return None
    return _job_get(jid)


def _enqueue_job(job_id, args, text, employee):
    """收到指令先回执，真正干活丢进有界队列，由固定数量 worker 消费。"""
    slim = {k: v for k, v in (args or {}).items() if k not in ("audio_base64", "audio_path")}
    job = _job_get(job_id)
    if job:
        job["_queued_at"] = time.time()
    _JOB_Q.put((job_id, slim, text, employee))
    log("任务 %s 入队：queue=%d ahead=%d workers=%d" % (
        job_id, _JOB_Q.qsize(), _jobs_ahead(job_id), JOB_WORKERS))


def _job_worker(idx):
    while True:
        job_id, args, text, employee = _JOB_Q.get()
        try:
            _run_job(job_id, args, text, employee)
        except Exception:                            # noqa: BLE001 - worker 不能死
            log("worker-%d 执行任务 %s 异常：\n%s" % (idx, job_id, traceback.format_exc()))
        finally:
            _JOB_Q.task_done()


def _start_job_workers():
    for i in range(JOB_WORKERS):
        threading.Thread(target=_job_worker, args=(i,), daemon=True,
                         name="job-worker-%d" % i).start()
    log("任务队列已启动：%d 个 worker" % JOB_WORKERS)


def _job_new(question, ack_text, mode, avatar, employee, args):
    jid = "vj_%s_%d" % (os.urandom(3).hex(), int(time.time() * 1000) % 100000)
    job = {"job_id": jid, "status": "accepted", "question": question,
           "ack": {"text": ack_text}, "mode": mode, "avatar": avatar,
           "delegate": ({"id": employee["id"], "name": employee["name"]} if employee else None),
           "created_at": time.time(),
           "callback_url": args.get("callback_url") or PUSH_URL or "",
           "voice": args.get("voice") or DEFAULT_VOICE,
           "_audio": None}
    with _JOBS_LOCK:
        _JOBS[jid] = job
        _prune_jobs_locked()
    log("任务 %s 新建：delegate=%s mode=%s client_id=%s text=%r" % (
        jid, (employee or {}).get("id") or "-", mode,
        str(args.get("client_id") or "-")[:40], (question or "")[:80]))
    return job


def _prune_jobs_locked():
    """只清理已结束（done/error）的任务。

    以前按创建时间无条件删，会把「还在排队/正在跑」的任务一起清掉，
    _run_job 拿不到 job 就静默 return，App 永远停在 accepted —— 这就是一类硬丢单。
    """
    now = time.time()

    def _terminal(job):
        return job.get("status") in ("done", "error", "cancelled")

    for jid, job in list(_JOBS.items()):
        if _terminal(job) and now - job.get("created_at", now) > JOB_TTL:
            _JOBS.pop(jid, None)
    while len(_JOBS) > JOB_MAX:
        cands = [(jid, j.get("created_at", 0)) for jid, j in _JOBS.items() if _terminal(j)]
        if not cands:
            break                                  # 全是未完成任务：宁可超量也不丢
        _JOBS.pop(min(cands, key=lambda kv: kv[1])[0], None)


def _job_get(job_id):
    with _JOBS_LOCK:
        return _JOBS.get(job_id or "")


def _push_result(url, payload, timeout=6):
    """任务完成后把结果 JSON 主动推给客户端；失败只记日志，不影响主流程。"""
    if not url:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            log("结果已推送 %s → HTTP %s" % (url, resp.status))
    except Exception as e:                           # noqa: BLE001
        log("结果推送失败 %s：%s" % (url, e))


def _delegate_to_employee(employee, text, timeout, *, idem_key=None, session_id=None,
                          on_status=None):
    """把老板的点名指令交给该员工的 Hermes profile，由员工本人处理并回复。"""
    if not _profile_ready(employee["id"]):
        # 没有独立 profile 会被 run-hermes.sh 静默回落到 root，等于没派单，必须显式报错
        raise RuntimeError("员工 %s（%s）还没有独立 Hermes profile，无法派单"
                           % (employee["name"], employee["id"]))
    prompt = ("老板通过语音向你下达指令，请按你的岗位职责直接处理，并回复老板。"
              "先给结论，不超过 120 字；有依据附 2-4 条。指令：%s" % text)
    return run_hermes(prompt, profile=employee["id"], timeout=timeout,
                      idem_key=idem_key, session_id=session_id, on_status=on_status)


def _run_job(job_id, args, text, employee):
    job = _job_get(job_id)
    if not job:
        log("任务 %s 开始执行时已不存在（被清理），跳过并丢弃" % job_id)
        return
    job["status"] = "running"
    job["started_at"] = time.time()
    wait = job["started_at"] - (job.get("_queued_at") or job["started_at"])
    timeout = int(args.get("timeout_sec") or JOB_TIMEOUT)
    client_id = str(args.get("client_id") or "").strip()
    idem_key = _GW.make_idem("asr", client_id, text) if (_GW is not None and client_id) else None
    live = job.get("live") or {}
    if live.get("session_id"):
        sess_id = live_session_id(live["session_id"])     # 实时通话：一通电话一个会话
    else:
        sess_id = session_for(employee["id"] if employee else
                              (args.get("profile") or SECRETARY_ID))

    def _stage(stage):
        job["stage"] = stage
        job["progress_ts"] = time.time()

    log("任务 %s 开始执行：排队 %.1fs，delegate=%s，session=%s，text=%r" % (
        job_id, wait, (employee or {}).get("id") or "-", sess_id or "-", (text or "")[:80]))

    try:
        _stage("running")
        if employee:
            raw = _delegate_to_employee(employee, text, timeout, idem_key=idem_key,
                                        session_id=sess_id, on_status=_stage)
        else:
            # 未点名：默认由 OPC-001 小秘书（老板影子分身）代老板处理
            prof_name = args.get("profile") or (
                SECRETARY_ID if _profile_ready(SECRETARY_ID) else None)
            raw = run_hermes(text, profile=prof_name, timeout=timeout, idem_key=idem_key,
                             session_id=sess_id, on_status=_stage)
        detail, speech = normalize_answer(raw)
        voice = job.get("voice") or DEFAULT_VOICE
        emotion = job.get("emotion")
        audio_b64, mime, meta = speak(
            speech if len(speech) <= MAX_TTS_CHARS else speech[:MAX_TTS_CHARS] + "……",
            voice=voice, emotion=emotion, stem="job_%s" % job_id)
        job["result"] = {
            "speech_text": speech,
            "detail_text": detail,
            "emotion": norm_emotion(emotion) if emotion else None,
            "voice": voice,
            "audio_path": meta.get("audio_path"),
            "audio_mime": mime,
            "sample_rate": meta.get("sample_rate"),
            "duration": meta.get("duration"),
            "segments": meta.get("segments") or [],
            "render": {"driver": "wav_envelope", "lip_sync": "amplitude",
                       "sample_rate": meta.get("sample_rate"),
                       "timeline": "segments", "avatar": job.get("avatar")},
        }
        job["_audio"] = (audio_b64, mime)
        # 实时通话：把正式回复也混进同一路直播流，客户端继续播同一个 m3u8 即可
        live = job.get("live")
        if live:
            live["speech_seconds"] = _live_append_b64(live.get("session_id"), audio_b64)
            sess = _live_get(live.get("session_id"))
            # ended=true：通话已挂断，这段回复没进直播流，客户端需自己播（voice_job 返回的音频）
            live["ended"] = bool(sess and sess._stop.is_set())
            live["hls_url"] = sess.view()["hls_url"] if sess else live.get("hls_url")
            live["hls_url_full"] = sess.view()["hls_url_full"] if sess else live.get("hls_url_full")
        job["status"] = "done"
        job["stage"] = "completed"
        job["finished_at"] = time.time()
        log("任务 %s 完成（%.1fs，语音 %.1fs）" % (
            job_id, job["finished_at"] - job["started_at"], meta.get("duration") or 0))
    except Exception as e:                           # noqa: BLE001
        log(traceback.format_exc())
        job["status"] = "error"
        job["stage"] = "failed"
        job["error"] = str(e)
        job["finished_at"] = time.time()
    finally:
        job["_done"].set()
        if job.get("callback_url"):
            payload = _job_view(job)
            payload.pop("_audio", None)
            threading.Thread(target=_push_result, args=(job["callback_url"], payload),
                             daemon=True).start()


def _ack_only(args, text):
    """只回一句语音、不建任务：识别链路异常时的兜底，保证老板一定听到回应。"""
    aid = args.get("avatar_id") or args.get("profile") or (
        SECRETARY_ID if _profile_ready(SECRETARY_ID) else "ceo")
    prof = load_profiles().get(aid) or {}
    voice = args.get("voice") or prof.get("voice")
    emotion = emotion_of(aid, override=args.get("emotion"))
    item = ack_audio(text, voice=voice, emotion=emotion) or {"text": text,
                                                             "audio_base64": None}
    avatar = avatar_profile(aid, name=prof.get("name"), role=prof.get("role"))
    payload = {"job_id": "", "status": "need_repeat", "clarify": True, "question": "",
               "ack": {"text": text, "audio_path": item.get("audio_path"),
                       "sample_rate": item.get("sample_rate"),
                       "duration": item.get("duration"),
                       "segments": item.get("segments") or [], "emotion": emotion},
               "mode": "voice", "avatar": avatar, "emotion": emotion,
               "next": "这次语音没能解析，未执行指令；请老板重说一遍"}
    return payload, item.get("audio_base64"), item.get("mime") or "audio/wav"


def _clarify_reply(args, q, text):
    """识别不准/没听清：不执行指令，用员工身份与情绪回一句「请再说一遍」。"""
    employee = find_employee(text) if (text and args.get("delegate", True)) else None
    default_avatar = SECRETARY_ID if _profile_ready(SECRETARY_ID) else "ceo"
    avatar_id = args.get("avatar_id") or (employee["id"] if employee else
                                          (args.get("profile") or default_avatar))
    prof = load_profiles().get(avatar_id) or {}
    if not args.get("voice"):
        voice = (employee or {}).get("voice") or prof.get("voice")
        if voice:
            args["voice"] = voice
    line, emotion = clarify_line(
        employee, avatar_id, text, float(q.get("confidence") or 0.0),
        float(args.get("clarify_timeout_sec") or ASR_CLARIFY_TIMEOUT))
    ack = ack_audio(line, voice=args.get("voice"), emotion=emotion,
                    timeout=ASR_CLARIFY_TTS_TIMEOUT) or {
        "text": line, "audio_base64": None}
    args["emotion"] = emotion
    avatar = avatar_profile(avatar_id, name=(employee or {}).get("name"),
                            role=(employee or {}).get("role"))
    job = _job_new(text, line, "voice", avatar, employee, args)
    job["status"] = "need_repeat"
    job["clarify"] = True
    job["emotion"] = emotion
    job["asr"] = {"model": q.get("model"), "language": q.get("language"),
                  "audio_seconds": q.get("duration"), "confidence": q.get("confidence"),
                  "avg_logprob": q.get("avg_logprob"),
                  "no_speech_prob": q.get("no_speech_prob"),
                  "low_confidence": True, "silent": bool(q.get("silent")), "hear": text}
    job["ack"].update({"audio_path": ack.get("audio_path"),
                       "sample_rate": ack.get("sample_rate"),
                       "duration": ack.get("duration"),
                       "segments": ack.get("segments") or [], "emotion": emotion})
    job["_done"] = threading.Event()
    job["_done"].set()
    if args.get("live_session"):
        sid = (args["live_session"] or "").strip()
        _live_touch(sid)
        if ack.get("audio_base64"):
            _live_append_b64(sid, ack.get("audio_base64"))
        else:
            _live_ack_later(sid, line, args.get("voice"), emotion)
        live = _live_get(sid)
        if live:
            job["live"] = {"session_id": sid, "hls_url": live.view()["hls_url"], "mode": "hls"}
    payload = {"job_id": job["job_id"], "status": "need_repeat", "clarify": True,
               "question": text, "emotion": emotion, "ack": job["ack"], "mode": "voice",
               "avatar": avatar, "delegate": job["delegate"], "asr": job["asr"],
               "next": "识别置信度 %.2f 低于阈值 %.2f，未执行指令；请老板重说一遍"
                       % (float(q.get("confidence") or 0.0), ASR_LOW_CONFIDENCE)}
    if job.get("callback_url"):
        threading.Thread(target=_push_result, args=(job["callback_url"], _job_view(job)),
                         daemon=True).start()
    return payload, ack.get("audio_base64"), ack.get("mime") or "audio/wav"


def voice_chat(args):
    """第一段：ASR → 意图 → 立即回「收到」语音 + job_id，重活丢后台排队跑。

    client_id：客户端为每段录音生成的稳定 id。断网重发时服务端按它找回原任务，
    只回执不再执行一遍（幂等），避免「重试导致同一条指令跑两次」。
    """
    client_id = str(args.get("client_id") or "").strip()[:120]
    audio_n = len(args.get("audio_base64") or args.get("audio") or "") or (
        1 if args.get("audio_path") else 0)
    log("voice_chat 收到：client_id=%s live=%s bytes≈%d clarify=%s text=%r" % (
        client_id or "-", args.get("live_session") or "-", audio_n,
        bool(args.get("clarify", True)), (args.get("text") or "")[:40]))
    prev = _client_job(client_id)
    if prev:
        payload = _job_view(prev)
        payload["duplicate"] = True
        ack = prev.get("_ack_audio") or (None, None)
        log("重复指令 client_id=%s 命中任务 %s（%s），不再执行"
            % (client_id, prev.get("job_id"), prev.get("status")))
        return payload, ack[0], ack[1] or "audio/wav"
    try:
        q = asr_transcribe(args, tag="voice_chat")
    except Exception as e:                           # noqa: BLE001 - 识别失败也要回一句话，不能静默丢
        log("ASR 失败（client_id=%s）：%s" % (client_id, e))
        return _ack_only(args, "老板，收到，但这次语音没解析出来，请再说一遍。")
    text = (q.get("text") or "").strip()
    # 识别不准（或完全没有效语音）时不执行误识文本，改回一句情绪化的「请再说一遍」
    if not text or (q.get("low_confidence") and args.get("clarify", True)):
        return _clarify_reply(args, q, text)
    employee = find_employee(text) if args.get("delegate", True) else None
    ack_text = ack_for(text, employee)
    # 未点名时默认播报人＝OPC-001 小秘书（老板影子分身），其 profile 没就绪才退回 ceo
    default_avatar = SECRETARY_ID if _profile_ready(SECRETARY_ID) else "ceo"
    avatar_id = args.get("avatar_id") or (employee["id"] if employee else
                                          (args.get("profile") or default_avatar))
    # 员工档案里配了音色就用员工自己的音色，客户端显式指定则优先
    if not args.get("voice"):
        voice = (load_profiles().get(avatar_id) or {}).get("voice")
        if voice:
            args["voice"] = voice
    # 情绪取播报对象的档案（点名员工优先），客户端可用 emotion 参数覆盖
    emotion = emotion_of(avatar_id, employee=employee, override=args.get("emotion"))
    ack = ack_audio(ack_text, voice=args.get("voice"), emotion=emotion) or {
        "text": ack_text, "audio_base64": None}
    avatar = avatar_profile(avatar_id, name=(employee or {}).get("name"),
                            role=(employee or {}).get("role"))
    mode = decide_mode(args.get("mode") or "auto",
                       float(args.get("bandwidth_kbps") or 0), avatar)
    job = _job_new(text, ack_text, mode, avatar, employee, args)
    job["emotion"] = emotion
    job["ack"]["audio_path"] = ack.get("audio_path")
    job["ack"]["sample_rate"] = ack.get("sample_rate")
    job["ack"]["duration"] = ack.get("duration")
    job["ack"]["segments"] = ack.get("segments") or []
    job["ack"]["emotion"] = emotion
    job["_done"] = threading.Event()
    job["asr"] = {"model": q.get("model"), "language": q.get("language"),
                  "audio_seconds": q.get("duration"), "confidence": q.get("confidence"),
                  "avg_logprob": q.get("avg_logprob"),
                  "no_speech_prob": q.get("no_speech_prob"),
                  "low_confidence": bool(q.get("low_confidence"))}
    # 实时通话：客户端已起流（live_call start）时，把「收到」语音直接混进直播流
    live_sid = (args.get("live_session") or "").strip()
    if live_sid:
        _live_touch(live_sid)
        if ack.get("audio_base64"):
            _live_append_b64(live_sid, ack.get("audio_base64"))
        else:
            # 同步合成超时：先回文字，语音后台补上再混入，别让整通电话一句声音都没有
            _live_ack_later(live_sid, ack_text, args.get("voice"), emotion)
        live = _live_get(live_sid)
        if live:
            job["live"] = {"session_id": live_sid, "hls_url": live.view()["hls_url"],
                           "hls_url_full": live.view()["hls_url_full"],
                           "mode": "hls", "segment_sec": LIVE_SEGMENT_SEC}

    payload = {"job_id": job["job_id"], "status": "accepted",
               "question": text, "ack": job["ack"], "mode": mode, "avatar": avatar,
               "delegate": job["delegate"], "emotion": emotion, "asr": job["asr"],
               "live": job.get("live"),
               "next": "调用 voice_job(job_id) 取结果；任务完成也会推送到 callback_url"}

    job["_ack_audio"] = (ack.get("audio_base64"), ack.get("mime") or "audio/wav")
    _client_bind(client_id, job["job_id"])
    _enqueue_job(job["job_id"], args, text, employee)

    if args.get("wait"):                             # 兼容同步调用：等结果一起返回
        deadline = time.time() + min(float(args.get("wait_sec") or 240), 600)
        while time.time() < deadline and job["status"] in ("accepted", "running"):
            job["_done"].wait(min(0.5, max(0.01, deadline - time.time())))
        payload = _job_view(job)
        payload.pop("_audio", None)
        audio = job.get("_audio") if args.get("include_audio", True) else None
        if job["status"] == "done" and audio:
            return payload, audio[0], audio[1]
        return payload, ack.get("audio_base64"), ack.get("mime") or "audio/wav"

    return payload, ack.get("audio_base64"), ack.get("mime") or "audio/wav"


def boss_briefing(args):
    """老板上线钩子：让 OPC-001 小秘书主动汇报近期发生的事。

    异步任务，调用方拿 job_id 后用 voice_job 取结果（含语音与详细文字）。
    客户端可以在 App 启动 / 从后台切回前台时调一次，实现「检测到老板在线就汇报」。
    """
    aid = SECRETARY_ID if _profile_ready(SECRETARY_ID) else "ceo"
    prof = load_profiles().get(aid) or {}
    if not args.get("voice") and prof.get("voice"):
        args["voice"] = prof["voice"]
    avatar = avatar_profile(aid, name=prof.get("name"), role=prof.get("role"))
    mode = decide_mode(args.get("mode") or "auto",
                       float(args.get("bandwidth_kbps") or 0), avatar)
    job = _job_new("老板上线，汇报近期事项", "老板，我先把最近的事过一遍。",
                   mode, avatar, None, args)
    job["delegate"] = {"id": aid, "name": prof.get("name") or aid}
    job["emotion"] = "calm"
    job["scene"] = "boss_online"
    job["_done"] = threading.Event()
    _enqueue_job(job["job_id"], args, BOSS_BRIEFING_PROMPT, None)
    return {"job_id": job["job_id"], "status": "accepted", "scene": "boss_online",
            "avatar": avatar, "mode": mode, "emotion": "calm",
            "next": "调用 voice_job(job_id) 取小秘书的汇报"}


def voice_job(args):
    """第二段：按 job_id 取结果；wait_sec 可长轮询，避免客户端高频轮询。"""
    job = _job_get(args.get("job_id"))
    if not job:
        raise ValueError("任务不存在或已过期：%s" % (args.get("job_id") or ""))
    wait = min(float(args.get("wait_sec") or 0), 30)
    deadline = time.time() + max(0, wait)
    while (wait > 0 and job["status"] in ("accepted", "running")
           and time.time() < deadline):
        job["_done"].wait(min(0.5, max(0.01, deadline - time.time())))
    payload = _job_view(job)
    payload.pop("_audio", None)
    audio = job.get("_audio") if args.get("include_audio", True) else None
    if job["status"] == "done" and audio:
        return payload, audio[0], audio[1]
    return payload, None, None


def voice_avatars(_args):
    """列出可插拔的数字人档案（照片 + 仿真人视频素材）。"""
    items = []
    for aid in (list(load_profiles()) or list(load_roster())):
        av = avatar_profile(aid)
        if av:
            items.append(av)
    return {"asset_dirs": ASSET_DIRS, "asset_url_base": ASSET_URL_BASE or None,
            "count": len(items), "avatars": items}


# ---------------------------------------------------------------- 调度中心转发
DISPATCH_URL = os.environ.get("DISPATCH_URL", "http://127.0.0.1:9120")


def _dispatch_req(method, path, body=None, timeout=10):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(DISPATCH_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def dispatch_task(args):
    """把任务转发给 hermes 调度中心统一记账与分流：
    老板语音/视频 → P0 立即拉起专属 worker；老板文字 → P1；其余 → P2，排队消费。"""
    text = (args.get("text") or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    body = {"source": args.get("source") or "boss",
            "modality": args.get("modality") or "text",
            "text": text}
    for k in ("profile", "callback_url", "live_session", "channel", "voice"):
        if args.get(k):
            body[k] = args[k]
    if args.get("timeout_sec"):
        body["timeout_sec"] = int(args["timeout_sec"])
    if args.get("dry_run"):
        body["dry_run"] = True
    return _dispatch_req("POST", "/submit", body)


def dispatch_status(args):
    """按 task_id 查任务，缺省返回最近任务列表。"""
    tid = (args.get("task_id") or "").strip()
    if tid:
        return _dispatch_req("GET", "/task/%s" % tid)
    q = "/tasks?limit=%d" % min(int(args.get("limit") or 20), 200)
    if args.get("status"):
        q += "&status=" + str(args["status"])
    return _dispatch_req("GET", q)


# ---------------------------------------------------------------- 工具定义
TOOLS = [
    {
        "name": "asr_transcribe",
        "description": "把一段语音转成文字。支持 base64 音频或服务器本地文件路径，返回文本、句级时间轴与置信度（confidence/low_confidence）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "audio_base64": {"type": "string", "description": "音频内容 base64（wav/mp3/ogg/flac/webm 均可），也可用 data: URL"},
                "audio_path": {"type": "string", "description": "服务器本地音频文件路径（与 audio_base64 二选一）"},
                "language": {"type": "string", "description": "语言代码，默认 zh"},
                "model": {"type": "string", "description": "whisper 模型：tiny/base/small/medium/large-v3，默认 small"},
                "initial_prompt": {"type": "string", "description": "可选提示词，帮助识别专有名词"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "voice_chat",
        "description": ("接收语音指令，解析完立刻回一句「收到」（如：好的老板，马上安排 / 老板稍等），"
                        "同时返回 job_id；结果稍后用 voice_job 取，或推送到 callback_url。"
                        "语音里点名员工（@id/姓名/工号）时，指令直接下发给该员工，由员工处理并回复。"
                        "识别置信度低（或没识别出字）时不执行指令，改由大模型按该员工的情绪回一句"
                        "「没听清，请再说一遍」，状态为 need_repeat。"
                        "返回 text(JSON) + audio(收到/澄清语音) 两个内容块。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "audio_base64": {"type": "string", "description": "语音指令的 base64 音频（wav/mp3 等）"},
                "audio_path": {"type": "string", "description": "服务器本地音频路径（与 audio_base64 二选一）"},
                "client_id": {"type": "string", "description": "客户端为每段录音生成的稳定 id；断网重发时按它幂等去重，不会重复执行"},
                "language": {"type": "string", "description": "识别语言，默认 zh"},
                "model": {"type": "string", "description": "whisper 模型，默认 small"},
                "initial_prompt": {"type": "string", "description": "可选提示词，帮助识别员工姓名等专有名词"},
                "voice": {"type": "string", "description": "播报音色 id，省略用默认音色"},
                "profile": {"type": "string", "description": "可选 HERMES_PROFILE（如 ceo）；省略走默认 root 配置"},
                "mode": {"type": "string", "enum": ["auto", "voice", "avatar", "video"],
                         "description": "播报形态：voice 纯语音；avatar 数字人按 WAV 包络实时驱动；video 叠加仿真人视频素材；auto 按带宽与素材自动选"},
                "bandwidth_kbps": {"type": "number", "description": "客户端下行带宽 kbps；足够时 auto 会升级到视频/数字人播报"},
                "avatar_id": {"type": "string", "description": "指定播报形象 id；派单时默认用被点名的员工"},
                "delegate": {"type": "boolean", "description": "是否允许按语音点名自动派单，默认 true"},
                "callback_url": {"type": "string", "description": "任务完成后接收结果 JSON 的 HTTP 回调地址（可选）"},
                "wait": {"type": "boolean", "description": "兼容同步调用：true 时等任务结束再返回（默认 false，立即回收到）"},
                "wait_sec": {"type": "number", "description": "wait=true 时最长等待秒数，默认 240"},
                "include_audio": {"type": "boolean", "description": "wait=true 时是否连结果语音一起返回，默认 true"},
                "timeout_sec": {"type": "integer", "description": "后台任务执行超时秒数，默认 600"},
                "emotion": {"type": "string",
                            "enum": ["auto", "neutral", "cheerful", "calm", "serious"],
                            "description": "播报情绪（影响语速与音高）：auto 按播报对象档案的 emotion 自动取，默认 auto"},
                "clarify": {"type": "boolean",
                            "description": "识别置信度低时是否改用「没听清，请再说一遍」的情绪化语音兜底，默认 true；false 则照旧执行识别文本"},
                "clarify_timeout_sec": {"type": "number",
                                        "description": "低置信时等待大模型生成澄清语的秒数，超时用模板句，默认 20"},
                "live_session": {"type": "string",
                                 "description": ("实时通话会话 id（live_call start 返回）。给了就把「收到」和正式回复"
                                                 "直接混进该路 HLS 直播流，客户端只播一个 m3u8，语音与画面同步；"
                                                 "纯语音通话（mode=audio）时回复只有音频，客户端继续播同一个 m3u8 即可")},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "voice_job",
        "description": ("按 job_id 取语音指令的执行结果。wait_sec>0 可长轮询（最多 30 秒）。"
                        "done 时返回简要语音（speech_text + audio）、详细文字（detail_text）、"
                        "句级时间轴与数字人档案（照片/仿真人视频素材），供客户端解析 WAV 包络驱动口型。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "voice_chat 返回的任务 id"},
                "wait_sec": {"type": "number", "description": "长轮询等待秒数，0 表示立即返回（默认 0）"},
                "include_audio": {"type": "boolean", "description": "完成时是否返回语音音频，默认 true"},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "boss_briefing",
        "description": ("老板上线钩子：让 OPC-001 小秘书（老板影子分身）主动汇报近期发生的事。"
                        "异步返回 job_id，再用 voice_job 取详细汇报与语音。"
                        "建议客户端在 App 启动 / 从后台切回前台时调用一次。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["auto", "voice", "avatar", "video"],
                         "description": "播报形态，默认 auto（有 talk 素材就用视频）"},
                "bandwidth_kbps": {"type": "number", "description": "客户端下行带宽，低于 600 会降级为纯语音"},
                "voice": {"type": "string", "description": "音色 id，省略用档案里的音色"},
                "timeout_sec": {"type": "number", "description": "后台任务超时秒数，默认 600"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "profile",
        "description": ("获取员工档案：默认返回全部员工（工号/部门/岗位/口头禅/简介/技能/形象参数/"
                        "照片与仿真人视频地址/音色/Hermes profile 是否就绪）。"
                        "传 id 取单人，可用 dept/project 过滤。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "员工 Agent ID（如 ceo、pm_wenren）；省略返回全部"},
                "dept": {"type": "string", "description": "按部门模糊过滤，如 产品中心"},
                "project": {"type": "string", "description": "按项目模糊过滤，如 文人莫克"},
                "include_assets": {"type": "boolean",
                                   "description": "是否探测照片/视频素材并回填地址，默认 true"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "voice_avatars",
        "description": "列出可插拔的数字人播报档案（照片 + 仿真人视频素材），按 assets 目录约定自动发现。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "intro_video",
        "description": ("按需生成员工的自我介绍视频（画面 + 语音）：档案字段经大模型润色成 2~3 句，"
                        "TTS 合成语音，再与 talk 视频（没有则用照片运镜）合成 720×960 MP4，"
                        "缓存在 assets/intro/<id>.mp4。首次调用生成，之后直接复用；"
                        "生成后 profile 返回 introVideo / introReady / introText / introDuration。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "员工 Agent ID（如 ceo）；与 all 二选一，省略表示全部"},
                "all": {"type": "boolean", "description": "为所有员工生成，默认 false"},
                "force": {"type": "boolean", "description": "忽略缓存重新生成（文案与视频都重做），默认 false"},
                "wait": {"type": "boolean", "description": "true 等生成完再返回（默认）；false 后台排队立即返回"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "live_call",
        "description": ("发起实时通话：服务端用 ffmpeg 把员工画面与实时写入的语音混成 HLS 直播流"
                        "（1 秒分片），音视频同流复用天然同步。mode=audio 时为纯语音通话（不出画面），"
                        "不说话时推静音保持连接，客户端一直播同一个 m3u8 即可，无需 WebRTC。"
                        "start 起流返回 hls_url（默认播报人 OPC-001 小秘书）；say 让员工说话；"
                        "stop 挂断并收尾；客户端用 MediaPlayer/ExoPlayer 直接播 m3u8。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "say", "stop", "status"],
                           "description": "start 起流（默认）/ say 说话 / stop 挂断 / status 查看"},
                "session_id": {"type": "string", "description": "start 返回的会话 id（say/stop/status 必填）"},
                "mode": {"type": "string", "enum": ["video", "audio"],
                         "description": "通话形态：audio 纯语音通话（首页通话按钮用这个）；video 实时视频通话；默认 video"},
                "avatar_id": {"type": "string", "description": "start 时的员工 id，默认 OPC-001 小秘书（secretary）"},
                "text": {"type": "string", "description": "say 时的说话内容（服务端 TTS，音色/情绪取员工档案）"},
                "audio_base64": {"type": "string", "description": "say 时直接给一段 wav/mp3 的 base64（走 ASR 结果时用）"},
                "audio_path": {"type": "string", "description": "say 时的服务器本地音频路径（与 audio_base64 二选一）"},
                "voice": {"type": "string", "description": "say 时覆盖音色 id"},
                "emotion": {"type": "string", "description": "say 时覆盖情绪：neutral/cheerful/calm/serious"},
                "first_segment_sec": {"type": "number", "description": "start 时等待首个分片的最长秒数，默认 8"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "dispatch_task",
        "description": ("把任务转发给 hermes 调度中心统一记账与分流（调度中心常驻 127.0.0.1:9120）。"
                        "老板的语音/视频指令（source=boss 且 modality=voice/video）为 P0："
                        "立即拉起专属 worker 并登记实时通道（返回 channel_id），不排队；"
                        "老板文字为 P1，员工/系统任务为 P2：进优先级队列由 worker 池消费。"
                        "返回 task_id/priority/lane，用 dispatch_status 查进度与结果。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "任务指令文本"},
                "source": {"type": "string", "enum": ["boss", "employee", "system"],
                           "description": "发起者，默认 boss；boss 语音/视频走 P0 实时 lane"},
                "modality": {"type": "string", "enum": ["voice", "video", "text"],
                             "description": "任务形态，默认 text"},
                "profile": {"type": "string", "description": "hermes 员工 profile id（如 secretary、vd_gou）；省略走默认配置"},
                "callback_url": {"type": "string", "description": "任务完成后接收结果 JSON 的 HTTP 回调地址（可选）"},
                "live_session": {"type": "string", "description": "复用 mcp-asr 的 live_call 会话 id，通道 kind 记为 hls"},
                "channel": {"type": "string", "enum": ["ws", "passthrough"],
                            "description": "P0 实时通道形态：ws 为双向 WebSocket（实时语音对话，worker 常驻保活）；passthrough 仅登记不建流"},
                "voice": {"type": "string", "description": "下行 TTS 音色 id（P0 实时会话回复用）"},
                "timeout_sec": {"type": "integer", "description": "worker 执行超时秒数，默认 600"},
                "dry_run": {"type": "boolean", "description": "测试用：不入 hermes，模拟执行"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dispatch_status",
        "description": ("查询调度中心任务：给 task_id 返回单条任务（状态/结果/通道），"
                        "不给则返回最近任务列表。状态含 queued/spawning/running/done/failed/cancelled。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "dispatch_task 返回的任务 id；省略则查列表"},
                "status": {"type": "string", "description": "列表过滤：queued/running/done/failed 等"},
                "limit": {"type": "integer", "description": "列表条数上限，默认 20"},
            },
            "additionalProperties": False,
        },
    },
]


# ---------------------------------------------------------------- 协议循环
def handle(req):
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "initialize":
        _result(rid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    elif method == "tools/list":
        _result(rid, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "asr_transcribe":
                payload = asr_transcribe(args)
                _result(rid, {"content": [{"type": "text",
                                           "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                              "isError": False})
            elif name == "boss_briefing":
                payload = boss_briefing(args)
                _result(rid, {"content": [{"type": "text",
                                           "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                              "isError": False})
            elif name in ("voice_chat", "voice_job"):
                payload, audio_b64, mime = (voice_chat(args) if name == "voice_chat"
                                            else voice_job(args))
                content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]
                if audio_b64:
                    content.append({"type": "audio", "data": audio_b64, "mimeType": mime})
                _result(rid, {"content": content, "isError": False})
            elif name in ("profile", "voice_avatars", "intro_video", "live_call"):
                payload = {"profile": profile, "voice_avatars": voice_avatars,
                           "intro_video": intro_video, "live_call": live_call}[name](args)
                _result(rid, {"content": [{"type": "text",
                                           "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                              "isError": False})
            elif name in ("dispatch_task", "dispatch_status"):
                payload = dispatch_task(args) if name == "dispatch_task" else dispatch_status(args)
                _result(rid, {"content": [{"type": "text",
                                           "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                              "isError": False})
            else:
                _result(rid, {"content": [{"type": "text", "text": "未知工具 %s" % name}], "isError": True})
        except Exception as e:
            log(traceback.format_exc())
            _result(rid, {"content": [{"type": "text", "text": "执行失败：%s" % e}], "isError": True})
    elif method == "ping":
        _result(rid, {})
    elif rid is not None:
        _error(rid, -32601, "未实现的方法 %s" % method)


def _warm_asr():
    """启动即加载 whisper，避免重启后第一条语音指令多等十几秒（首次加载权重）。"""
    try:
        t0 = time.time()
        _model(DEFAULT_MODEL)
        log("ASR 预热完成：%s（%.1fs）" % (DEFAULT_MODEL, time.time() - t0))
    except Exception as e:                           # noqa: BLE001 - 预热失败不影响正常按需加载
        log("ASR 预热失败（不影响使用）：%s" % e)


def main():
    log("启动，默认模型 %s，TTS 桥接 %s，数字人素材 %s" % (DEFAULT_MODEL, TTS_URL, ASSET_DIRS))
    _start_job_workers()
    _live_cleanup_stale_dirs()
    threading.Thread(target=_warm_asr, daemon=True).start()
    threading.Thread(target=prewarm_acks, daemon=True).start()
    threading.Thread(target=load_roster, daemon=True).start()
    if AUTO_INTRO and INTRO_DIR:
        log("自我介绍全自动巡检已开启（每 %ds），口型合成 %s" % (
            AUTO_INTRO_INTERVAL, "就绪" if lipsync_ready() else "未就绪，将回退静态画面"))
        threading.Thread(target=_auto_intro_loop, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log("收到非 JSON 行，已忽略")
            continue
        if isinstance(req, list):                  # JSON-RPC 批量：按序处理，避免乱序
            for r in req:
                try:
                    handle(r)
                except Exception:
                    log(traceback.format_exc())
            continue

        def _run_req(r):
            try:
                with _REQ_SEM:
                    handle(r)
            except Exception:
                log(traceback.format_exc())

        # 单条请求丢给线程：voice_chat(wait=true) 最长阻塞 600s，串行处理会让
        # voice_job / live_call say 全部排队，表现就是「说完没反应、通话没声音」。
        threading.Thread(target=_run_req, args=(req,), daemon=True).start()


if __name__ == "__main__":
    main()
