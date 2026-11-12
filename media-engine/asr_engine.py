#!/usr/bin/env python
"""media-engine 的 ASR 核心：自 /opt/sefs/mcp-asr/server.py 迁移（S1，代码复用）。

除了原有的「能量门 + 黑名单 + 复读 + 置信度」之外，按方案 §6.2 补齐多媒体防幻觉
过滤链：时长门 → 能量门 → VAD 门 → 文本置信门 → 复读门 → 黑名单门 → 字符集门。
任一命中即判「无可转写语音」，绝不把噪声/音乐的幻觉当成用户指令。
"""

import math
import os
import re
import sys
import threading

MODEL_NAME = os.environ.get("ASR_MODEL", "small")
DEFAULT_LANG = os.environ.get("ASR_LANG", "zh")

# —— 门 1/2/3：时长、能量、VAD ——
ASR_MIN_SEC = float(os.environ.get("ASR_MIN_SEC", "0.3"))
# 整段最响 20ms 帧的 RMS 低于此值＝没说话（0.002 ≈ -54 dBFS）
ASR_MIN_RMS = float(os.environ.get("ASR_MIN_RMS", "0.002"))
# 有声帧（高于峰值 5% 且高于底噪门）占比低于此值＝没有连续语音
ASR_MIN_SPEECH_RATIO = float(os.environ.get("ASR_MIN_SPEECH_RATIO", "0.05"))

# —— 门 4：段级文本置信 ——
ASR_NO_SPEECH_PROB = float(os.environ.get("ASR_NO_SPEECH_PROB", "0.6"))
ASR_MIN_AVG_LOGPROB = float(os.environ.get("ASR_MIN_AVG_LOGPROB", "-1.0"))
ASR_MAX_COMPRESSION = float(os.environ.get("ASR_MAX_COMPRESSION", "3.0"))

# —— 门 7：字符集（language=zh 时非中日韩字符占比上限）——
ASR_MAX_NON_CJK_RATIO = float(os.environ.get("ASR_MAX_NON_CJK_RATIO", "0.6"))

# 整条指令要不要执行的置信度门槛
ASR_LOW_CONFIDENCE = float(os.environ.get("ASR_LOW_CONFIDENCE", "0.5"))

_HALLUCINATION_PATTERNS = [re.compile(p, re.I) for p in (
    r"字幕\s*(?:by|from|由|：|:)\s*\S{0,20}",
    r"(?:请)?不吝?\s*点赞", r"点赞.{0,6}订阅", r"订阅.{0,6}转发",
    r"转发.{0,6}打赏", r"打赏.{0,6}支持", r"明镜|点点栏目",
    r"优优独播剧场|YYeTs|人人影视|Amara\.org",
)]
_SOFT_HALLUCINATION_PATTERNS = [re.compile(p, re.I) for p in (
    r"(?:字幕\s*)?(?:组|制作|提供|翻译|校对|压制|时间轴|后期|片源)\s*[：:]\s*\S{0,24}",
    r"字幕\s*(?:制作|提供|翻译|校对)",
    r"(?:谢谢|感谢)(?:大家)?(?:观看|收看|收听)\s*[。.!！]?",
    r"字幕组(?:\s*\S{0,12})?",
)]

_MODELS = {}
_MODEL_LOCK = threading.Lock()
_TRANSCRIBE_LOCK = threading.Lock()


def log(msg):
    sys.stderr.write("[media-engine/asr] %s\n" % msg)
    sys.stderr.flush()


def _model(name):
    import whisper
    with _MODEL_LOCK:
        if name not in _MODELS:
            log("加载 whisper 模型 %s（首次会下载权重）" % name)
            _MODELS[name] = whisper.load_model(name)
            log("whisper 模型 %s 就绪" % name)
        return _MODELS[name]


def model_status():
    return {k: True for k in _MODELS}


def _norm_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


# ---------------------------------------------------------------- 音频级门
def frame_stats(audio, frame=320):
    """20ms 帧的 RMS 统计：峰值帧、有声帧占比（VAD 近似）。"""
    import numpy as np
    if audio is None or len(audio) == 0:
        return {"peak_rms": 0.0, "speech_ratio": 0.0, "frames": 0}
    n = len(audio) // frame
    if n <= 0:
        return {"peak_rms": float(np.sqrt(np.mean(np.square(audio)))),
                "speech_ratio": 1.0, "frames": 0}
    frames = audio[:n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    peak = float(rms.max())
    if peak <= 0:
        return {"peak_rms": 0.0, "speech_ratio": 0.0, "frames": n}
    thr = max(ASR_MIN_RMS, peak * 0.05)
    return {"peak_rms": peak, "speech_ratio": float((rms > thr).mean()), "frames": n}


# ---------------------------------------------------------------- 段级门
def _non_cjk_ratio(text):
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 1.0
    cjk = sum(1 for c in chars if "\u4e00" <= c <= "\u9fff" or "\u3040" <= c <= "\u30ff"
              or "\uac00" <= c <= "\ud7af")
    return 1.0 - cjk / len(chars)


def segment_reason(seg, lang="zh"):
    """返回该段被丢弃的单一原因；None 表示保留。"""
    t = _norm_text(seg.get("text"))
    if not t:
        return "empty"
    if any(p.search(t) for p in _HALLUCINATION_PATTERNS):
        return "blacklist"
    if any(p.fullmatch(t) for p in _SOFT_HALLUCINATION_PATTERNS):
        return "blacklist_soft"
    no_speech = float(seg.get("no_speech_prob") or 0.0)
    logprob = float(seg.get("avg_logprob") or 0.0)
    if no_speech > ASR_NO_SPEECH_PROB or logprob < ASR_MIN_AVG_LOGPROB:
        return "low_confidence_segment"
    if float(seg.get("compression_ratio") or 0.0) >= ASR_MAX_COMPRESSION:
        return "repeat_compression"
    if str(lang).lower().startswith(("zh", "ja", "yue", "ko")) and _non_cjk_ratio(t) > ASR_MAX_NON_CJK_RATIO:
        return "charset"
    return None


def filter_segments(raw_segments, lang="zh"):
    """丢弃命中门限的段，并折叠连续复读；返回 (保留段, 丢弃原因计数)。"""
    kept, reasons, last = [], {}, None
    for s in raw_segments:
        text = _norm_text(s.get("text"))
        reason = segment_reason(s, lang)
        if reason is None and text == last:
            reason = "repeat"
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        last = text
        kept.append(dict(s, text=text))
    return kept, reasons


def _seg_dur(seg):
    return max(0.01, float(seg.get("end") or 0) - float(seg.get("start") or 0))


def confidence(kept):
    """段级 avg_logprob / no_speech_prob 按时长加权，聚合成 0-1 的整段置信度（启发式）。"""
    if not kept:
        return {"confidence": 0.0, "avg_logprob": None, "no_speech_prob": None,
                "max_compression_ratio": None, "segments": 0}
    total = sum(_seg_dur(s) for s in kept)
    lp = sum(float(s.get("avg_logprob") or 0.0) * _seg_dur(s) for s in kept) / total
    ns = sum(float(s.get("no_speech_prob") or 0.0) * _seg_dur(s) for s in kept) / total
    comp = max(float(s.get("compression_ratio") or 0.0) for s in kept)
    score = math.exp(min(0.0, lp)) * max(0.0, 1.0 - ns)
    if comp >= ASR_MAX_COMPRESSION:
        score *= 0.5
    return {"confidence": round(max(0.0, min(1.0, score)), 3),
            "avg_logprob": round(lp, 3), "no_speech_prob": round(ns, 3),
            "max_compression_ratio": round(comp, 2), "segment_count": len(kept)}


def _empty(lang, model, duration, stats, reason):
    return {"text": "", "language": lang, "duration": duration, "model": model,
            "segments": [], "filtered": 0, "silent": True, "silent_reason": reason,
            "filtered_reasons": {}, "low_confidence": True, **stats}


def probe(path):
    """解码 + 音频级门（时长/能量/VAD）。返回 (audio, duration, 帧统计, 命中原因 or None)。"""
    import whisper

    audio = whisper.load_audio(path)  # 内部走 ffmpeg，支持 wav/mp3/ogg 等
    duration = round(len(audio) / 16000.0, 2)
    if duration < ASR_MIN_SEC:
        log("ASR 时长门：%.2fs < %.2fs，不送模型" % (duration, ASR_MIN_SEC))
        return audio, duration, {}, "too_short"
    fs = frame_stats(audio)
    if fs["peak_rms"] < ASR_MIN_RMS:
        log("ASR 能量门：peak rms=%.5f < %.5f，不送模型" % (fs["peak_rms"], ASR_MIN_RMS))
        return audio, duration, fs, "silent"
    if fs["speech_ratio"] < ASR_MIN_SPEECH_RATIO:
        log("ASR VAD 门：有声帧占比 %.3f < %.3f，不送模型" % (fs["speech_ratio"], ASR_MIN_SPEECH_RATIO))
        return audio, duration, fs, "no_speech"
    return audio, duration, fs, None


def transcribe(path, language=None, model=None, initial_prompt=None):
    """音频文件 → 文本 + 句级时间轴 + 置信度；命中音频级门时不跑模型。"""
    lang = language or DEFAULT_LANG
    model_name = model or MODEL_NAME
    audio, duration, fs, reason = probe(path)
    if reason:
        return _empty(lang, model_name, duration, confidence([]), reason)

    kwargs = {"language": lang}
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt
    with _TRANSCRIBE_LOCK:
        r = _model(model_name).transcribe(audio, **kwargs)

    kept, reasons = filter_segments(r.get("segments", []), lang)
    if reasons:
        log("ASR 过滤：%s（原始文本长度 %d）" % (reasons, len(r.get("text") or "")))
    segments = [{"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"]}
                for s in kept]
    joiner = "" if str(lang).lower().startswith(("zh", "ja", "yue", "ko")) else " "
    text = joiner.join(s["text"] for s in segments).strip()
    if not text and not reasons:
        text = (r.get("text") or "").strip()
    stats = confidence(kept)
    return {
        "text": text,
        "language": r.get("language") or lang,
        "duration": duration,
        "model": model_name,
        "segments": segments,
        "filtered": sum(reasons.values()),
        "filtered_reasons": reasons,
        "silent": False,
        "silent_reason": None,
        "peak_frame_rms": round(fs["peak_rms"], 5),
        "speech_ratio": round(fs["speech_ratio"], 3),
        "low_confidence": (not text) or stats["confidence"] < ASR_LOW_CONFIDENCE,
        **stats,
    }
