#!/usr/bin/env python
"""qwen_engine：把本机常驻的 Qwen3 ASR/TTS 服务适配成 media-engine 的调用形态。

Qwen 服务是旁路（qwen-asr:9312 / qwen-tts:9311，各自独立 venv），本模块只做 HTTP
转发与响应翻译，不加载任何模型。默认引擎仍是 CosyVoice / whisper small；调用方显式
传 engine=qwen3 才走这里。

CPU 实测（2026-11-12，本机 144 核，fp32）：ASR RTF 1.7~2.3、TTS RTF 24~38，
只适合离线批量转写 / 配音朗读，不能进实时通话链路。
"""

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid

ASR_URL = os.environ.get("QWEN_ASR_URL", "http://127.0.0.1:9312").rstrip("/")
TTS_URL = os.environ.get("QWEN_TTS_URL", "http://127.0.0.1:9311").rstrip("/")
TIMEOUT = float(os.environ.get("QWEN_TIMEOUT", "1800"))

# Qwen3-TTS-12Hz-0.6B-CustomVoice 的 9 个预置音色
SPEAKERS = ("Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
            "Ryan", "Aiden", "Ono_Anna", "Sohee")
DEFAULT_SPEAKER = os.environ.get("QWEN_TTS_SPEAKER", "Vivian")

# media-engine 用 ISO 639-1，Qwen 只认规范语言名；未收录的交由模型自动判别
_LANG_MAP = {"zh": "Chinese", "en": "English", "yue": "Cantonese", "ja": "Japanese",
             "ko": "Korean", "de": "German", "fr": "French", "es": "Spanish",
             "pt": "Portuguese", "it": "Italian", "ru": "Russian", "ar": "Arabic",
             "id": "Indonesian", "th": "Thai", "vi": "Vietnamese", "tr": "Turkish",
             "hi": "Hindi", "ms": "Malay", "nl": "Dutch", "sv": "Swedish"}


def _lang(language):
    if not language:
        return None
    language = str(language).strip()
    return _LANG_MAP.get(language.lower(), language if language[:1].isupper() else None)


def _post(url, payload, timeout=TIMEOUT):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _ping(url, timeout=1.0):
    try:
        with urllib.request.urlopen(url + "/health", timeout=timeout) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def health():
    return {"asr": {"url": ASR_URL, "up": _ping(ASR_URL)},
            "tts": {"url": TTS_URL, "up": _ping(TTS_URL)},
            "speakers": list(SPEAKERS), "default_speaker": DEFAULT_SPEAKER}


def _ffprobe_duration(path):
    exe = os.environ.get("FFPROBE") or shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        return round(float(out.stdout.strip()), 2)
    except Exception:
        return None


def _ts(sec):
    ms = int(round(sec * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


# ---------------------------------------------------------------- ASR
def transcribe(path, language=None):
    """音频文件 → media-engine 统一结构（Qwen 不返回句级时间轴与置信度）。"""
    t0 = time.time()
    r = _post(ASR_URL + "/v1/asr", {"audio_path": path, "language": _lang(language)})
    duration = _ffprobe_duration(path) or 0.0
    text = (r.get("text") or "").strip()
    return {
        "text": text,
        "language": (r.get("language") or _lang(language) or "unknown"),
        "duration": duration,
        "model": r.get("engine") or "qwen3-asr-0.6b",
        "engine": "qwen3",
        "segments": [{"start": 0.0, "end": duration, "text": text}] if text else [],
        "filtered": 0,
        "filtered_reasons": {},
        "silent": not text,
        "silent_reason": None if text else "empty_text",
        # Qwen 不输出 no_speech_prob / avg_logprob，置信度字段留空而不是编造数值
        "confidence": None,
        "avg_logprob": None,
        "no_speech_prob": None,
        "max_compression_ratio": None,
        "segment_count": 1 if text else 0,
        "low_confidence": False,
        "asr_sec": round(time.time() - t0, 2),
        "rtf": round((time.time() - t0) / duration, 2) if duration else None,
    }


def empty_result(language, duration, reason):
    """音频级门命中时的统一空结果（与 whisper 分支字段对齐）。"""
    return {
        "text": "", "language": language or "unknown", "duration": duration,
        "model": "qwen3-asr-0.6b", "engine": "qwen3", "segments": [],
        "filtered": 0, "filtered_reasons": {}, "silent": True,
        "silent_reason": reason, "confidence": None, "avg_logprob": None,
        "no_speech_prob": None, "max_compression_ratio": None,
        "segment_count": 0, "low_confidence": True,
    }


# ---------------------------------------------------------------- TTS
def synthesize(text, speaker=None, language=None, instruct=None, out_dir=None, stem=None):
    """文本 → wav + 单条 srt/segments（Qwen 不输出句级对齐），产物复制到 out_dir。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    speaker = speaker or DEFAULT_SPEAKER
    if speaker not in SPEAKERS:
        raise ValueError("未知 Qwen 音色：%s（可用：%s）" % (speaker, ", ".join(SPEAKERS)))
    out_dir = out_dir or os.environ.get("MEDIA_WORK_DIR", os.path.dirname(os.path.abspath(__file__)))
    stem = stem or "qwen_tts_%d_%s" % (int(time.time()), uuid.uuid4().hex[:6])
    os.makedirs(out_dir, exist_ok=True)

    r = _post(TTS_URL + "/v1/tts", {"text": text, "speaker": speaker,
                                    "language": _lang(language) or "Chinese",
                                    "instruct": instruct or None})
    wav_src = r.get("audio_path")
    if not wav_src or not os.path.isfile(wav_src):
        raise RuntimeError("Qwen TTS 未返回音频文件：%r" % r)

    wav_path = os.path.join(out_dir, stem + ".wav")
    shutil.copyfile(wav_src, wav_path)
    duration = float(r.get("duration") or 0.0)
    segs = [{"index": 0, "text": text, "start": 0.0, "end": round(duration, 3)}]

    srt_path = os.path.join(out_dir, stem + ".srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("1\n%s --> %s\n%s\n" % (_ts(0.0), _ts(duration), text))

    seg_path = os.path.join(out_dir, stem + ".segments.json")
    with open(seg_path, "w", encoding="utf-8") as f:
        json.dump({"engine": "qwen3", "speaker": speaker,
                   "language": _lang(language) or "Chinese", "instruct": instruct,
                   "sample_rate": r.get("sample_rate"), "duration": round(duration, 3),
                   "segments": segs}, f, ensure_ascii=False, indent=2)

    return {"stem": stem, "audio_path": wav_path, "srt_path": srt_path,
            "segments_path": seg_path, "sample_rate": int(r.get("sample_rate") or 24000),
            "duration": round(duration, 3), "segment_count": 1,
            "synth_sec": r.get("synth_sec"), "rtf": r.get("rtf"),
            "workers": 1, "engine": "qwen3", "speaker": speaker, "segments": segs}
