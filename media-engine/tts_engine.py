#!/usr/bin/env python
"""media-engine 的 TTS 核心：自 /opt/sefs/mcp-tts/server.py 迁移（S1，代码复用）。

只剥离 MCP 协议层；切句、归一化、音色注册、超长重试、并发进程池全部保持原样，
保证 S3 下线 mcp-tts 时行为不跳变。相对原文件的差异只有：
  - 音色表默认仍读 /opt/sefs/mcp-tts/voices.json（唯一数据源），可用 MEDIA_VOICES_FILE 覆盖
  - 日志前缀改为 [media-engine/tts]
  - 合成入口 synthesize() 只返回路径与时间轴，不再拼 base64（由 HTTP 层决定响应形态）
"""

import json
import multiprocessing as mp
import os
import re
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np

ROOT = "/opt/sefs/CosyVoice"
for _p in (os.path.join(ROOT, "third_party", "Matcha-TTS"), ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_V3_DIR = os.path.join(ROOT, "pretrained_models", "Fun-CosyVoice3-0.5B")
_V2_DIR = os.path.join(ROOT, "pretrained_models", "CosyVoice2-0.5B")
MODEL_DIR = os.environ.get("TTS_MODEL_DIR") or (
    _V3_DIR if os.path.exists(os.path.join(_V3_DIR, "cosyvoice3.yaml")) else _V2_DIR)
IS_V3 = os.path.exists(os.path.join(MODEL_DIR, "cosyvoice3.yaml"))
V3_SYS_PROMPT = "You are a helpful assistant.<|endofprompt|>"
VOICES_FILE = os.environ.get("MEDIA_VOICES_FILE", "/opt/sefs/mcp-tts/voices.json")
SENTENCE_GAP = 0.12

_CPU = os.cpu_count() or 4
# 实测（2026-11-11，CosyVoice3-0.5B，本机）：每个 worker 进程 RSS 约 6.6~7.8GB。
# S1 期间 mcp-tts 的 6 进程池仍在线，media-engine 默认只开 2 个，避免叠加到 ~84GB；
# S3 下线 mcp-tts 后再用 TTS_WORKERS=6 恢复原并发度。
WORKERS = max(1, int(os.environ.get("TTS_WORKERS", "2")))
THREADS_PER_WORKER = int(os.environ.get("TTS_THREADS", "16"))
SYNTH_TIMEOUT = float(os.environ.get("TTS_SYNTH_TIMEOUT", "600"))


def log(msg):
    sys.stderr.write("[media-engine/tts] %s\n" % msg)
    sys.stderr.flush()


# ---------------------------------------------------------------- 文本切句
_SPLIT_RE = re.compile(r"[^。！？；!?\n]+[。！？；!?\n]*")


def split_sentences(text):
    """按中英文句末标点切句，保留标点；过长的句子再在逗号处断开。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for seg in _SPLIT_RE.findall(line):
            seg = seg.strip()
            if not seg:
                continue
            out.extend(_split_long(seg))
    return out


_CN_DIGITS = "零一二三四五六七八九"


def _cn_number(s):
    return "".join(_CN_DIGITS[int(c)] if c.isdigit() else c for c in s)


def pre_normalize(text):
    """定点归一化：版本号读成中文，拉丁术语里的点号保持不动。"""
    return re.sub(r"(?<![A-Za-z0-9.])(\d+)\.(\d+)(?![0-9])",
                  lambda m: _cn_number(m.group(1)) + "点" + _cn_number(m.group(2)), text)


def _split_long(seg, limit=34):
    if len(seg) <= limit:
        return [seg]
    parts = re.findall(r"[^，,：:、]+[，,：:、]*", seg)
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) > limit:
            out.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        out.append(cur)
    return [p for p in out if p.strip()]


def merge_short(sentences, min_chars=14):
    """把过短的句子并入下一句，规避 CosyVoice 对短句失控。"""
    out, buf = [], ""
    for s in sentences:
        buf = buf + s
        if len(buf) >= min_chars:
            out.append(buf)
            buf = ""
    if buf:
        if out:
            out[-1] += buf
        else:
            out.append(buf)
    return out


def expected_seconds(text):
    units = 0.0
    for ch in text:
        units += 0.6 if ch.isascii() and ch.isalnum() else 1.0
    return 0.14 * units


# ---------------------------------------------------------------- 合成引擎
class Engine:
    def __init__(self):
        self.cv = None
        self.voices = {}
        self.loaded = set()
        self.load_sec = None

    def config(self):
        with open(VOICES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    def ensure_model(self):
        if self.cv is not None:
            return
        import torch

        torch.set_num_threads(THREADS_PER_WORKER)
        from cosyvoice.cli.cosyvoice import CosyVoice2, CosyVoice3

        t0 = time.time()
        if os.path.exists(os.path.join(MODEL_DIR, "cosyvoice3.yaml")):
            self.cv = CosyVoice3(MODEL_DIR, load_trt=False, fp16=False)
        else:
            self.cv = CosyVoice2(MODEL_DIR, load_jit=False, load_trt=False,
                                 load_vllm=False, fp16=False)
        self.load_sec = round(time.time() - t0, 2)
        log("模型加载完成 %s %.2fs，采样率 %d" % (MODEL_DIR, self.load_sec, self.cv.sample_rate))

    def voice(self, name):
        cfg = self.config()
        self.voices = cfg["voices"]
        if name not in self.voices:
            raise ValueError("未知音色 %r，可用：%s" % (name, ", ".join(self.voices)))
        v = self.voices[name]
        self.ensure_model()
        if name not in self.loaded:
            if not os.path.exists(v["prompt_wav"]):
                raise ValueError("参考音频不存在：%s" % v["prompt_wav"])
            prompt_text = v["prompt_text"]
            if IS_V3:
                prompt_text = V3_SYS_PROMPT + prompt_text
            self.cv.add_zero_shot_spk(prompt_text, v["prompt_wav"], name)
            self.loaded.add(name)
        return v

    def synth(self, text, voice, speed):
        """合成一句，带静音裁剪与失控重试。"""
        self.voice(voice)
        expected = expected_seconds(text)
        best, best_dur = None, None
        for attempt in range(3):
            wav = self._synth_once(text, voice, speed)
            wav = self._trim(wav)
            dur = wav.shape[1] / self.cv.sample_rate
            if best_dur is None or dur < best_dur:
                best, best_dur = wav, dur
            if dur <= expected * 1.6 + 0.6:
                return wav
            if attempt == 0 and dur > expected * 4 + 1.0:
                log("首句严重超长（%.2fs / 预期 %.2fs），跳过整句重试，转分句兜底" % (dur, expected))
                return best
            log("时长异常：%r 预期 %.2fs 实际 %.2fs，第 %d 次重试" % (text, expected, dur, attempt + 1))
        log("重试后仍偏长，取最短的一条 %.2fs" % best_dur)
        return best

    def _synth_once(self, text, voice, speed):
        import torch

        v = self.voices[voice]
        chunks = []
        for out in self.cv.inference_zero_shot(
            text, v["prompt_text"], "", zero_shot_spk_id=voice, stream=False,
            speed=speed, text_frontend=IS_V3
        ):
            chunks.append(out["tts_speech"])
        if not chunks:
            raise RuntimeError("合成未返回音频：%r" % text)
        return torch.cat(chunks, dim=1)

    def _trim(self, wav, level=0.02):
        x = wav.squeeze(0).numpy()
        idx = np.where(np.abs(x) > level)[0]
        if idx.size == 0:
            return wav
        sr = self.cv.sample_rate
        pad = int(0.02 * sr)
        a, b = max(0, int(idx[0]) - pad), min(len(x), int(idx[-1]) + 1 + pad)
        return wav[:, a:b]


# ---------------------------------------------------------------- 并发合成池
def _worker_main(idx, in_q, out_q, threads):
    """合成子进程：独占一份模型，按句子粒度接活。"""
    os.environ["OMP_NUM_THREADS"] = str(threads)
    os.environ["MKL_NUM_THREADS"] = str(threads)
    os.environ["OPENBLAS_NUM_THREADS"] = str(threads)
    eng = Engine()
    try:
        eng.ensure_model()
    except Exception:
        out_q.put(("fatal", idx, None, traceback.format_exc(), None, None))
        return
    out_q.put(("ready", idx, eng.cv.sample_rate, None, None, None))
    while True:
        job = in_q.get()
        if job is None:
            return
        jid, text, voice, speed = job
        try:
            out_q.put(("ok", idx, None, None, jid,
                       eng.synth(text, voice, speed).squeeze(0).numpy()))
        except Exception as e:
            out_q.put(("err", idx, None, "%s" % e, jid, None))


class Pool:
    """N 个合成进程 + 最小负载派单；句子任务失败只影响该句。"""

    def __init__(self):
        ctx = mp.get_context("spawn")
        self.out_q = ctx.Queue()
        self.in_qs = []
        self.procs = []
        self.pending = {}
        self.lock = threading.Lock()
        self.inflight = [0] * WORKERS
        self.ready = threading.Event()
        self.ready_count = 0
        self.failed = None
        self.sample_rate = 24000
        for i in range(WORKERS):
            q = ctx.Queue()
            p = ctx.Process(target=_worker_main,
                            args=(i, q, self.out_q, THREADS_PER_WORKER), daemon=True)
            p.start()
            self.in_qs.append(q)
            self.procs.append(p)
        threading.Thread(target=self._collect, daemon=True).start()

    def _collect(self):
        while True:
            kind, idx, sr, err, jid, wav = self.out_q.get()
            if kind == "ready":
                with self.lock:
                    self.ready_count += 1
                    if sr:
                        self.sample_rate = int(sr)
                    if self.ready_count >= WORKERS:
                        self.ready.set()
                log("合成进程 %d 就绪（%d/%d，每进程 %d 线程）"
                    % (idx, self.ready_count, WORKERS, THREADS_PER_WORKER))
                continue
            if kind == "fatal":
                log("合成进程 %d 启动失败：%s" % (idx, err))
                self.failed = err
                self.ready.set()
                continue
            with self.lock:
                item = self.pending.pop(jid, None)
                if item:
                    self.inflight[item["w"]] -= 1
            if item:
                item["kind"], item["wav"], item["err"] = kind, wav, err
                item["ev"].set()

    def synth(self, text, voice, speed):
        jid = uuid.uuid4().hex
        with self.lock:
            w = min(range(WORKERS), key=lambda i: self.inflight[i])
            self.inflight[w] += 1
            item = {"ev": threading.Event(), "w": w, "kind": None, "wav": None, "err": None}
            self.pending[jid] = item
        self.in_qs[w].put((jid, text, voice, speed))
        if not item["ev"].wait(SYNTH_TIMEOUT):
            with self.lock:
                self.pending.pop(jid, None)
                self.inflight[w] -= 1
            raise RuntimeError("合成超时（%ds）" % SYNTH_TIMEOUT)
        if item["kind"] != "ok":
            raise RuntimeError(item["err"] or "合成失败")
        return item["wav"]


_POOL = None
_POOL_LOCK = threading.Lock()


def get_pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            t0 = time.time()
            pool = Pool()
            if not pool.ready.wait(timeout=600):
                raise RuntimeError("合成进程 600s 内未就绪")
            if pool.failed:
                raise RuntimeError("合成进程启动失败：%s" % pool.failed)
            log("并发合成池就绪：%d 进程 × %d 线程，用时 %.1fs"
                % (WORKERS, THREADS_PER_WORKER, time.time() - t0))
            _POOL = pool
        return _POOL


def pool_status():
    if _POOL is None:
        return {"loaded": False, "workers": WORKERS, "ready": False}
    return {"loaded": True, "workers": WORKERS, "ready": _POOL.ready.is_set(),
            "ready_workers": _POOL.ready_count, "sample_rate": _POOL.sample_rate,
            "failed": _POOL.failed}


def list_voices():
    cfg = Engine().config()
    items = [{"id": k, "description": v.get("description", ""),
              "prompt_wav": v["prompt_wav"], "prompt_text": v["prompt_text"]}
             for k, v in cfg["voices"].items()]
    return {"default": cfg.get("default"), "voices": items}


def _srt_ts(sec):
    ms = int(round(sec * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def synthesize(text, voice=None, speed=1.0, out_dir=None, stem=None):
    """文本 → wav + srt + segments.json，返回路径与时间轴。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    cfg = Engine().config()
    voice = voice or cfg.get("default")
    if voice not in cfg["voices"]:
        raise ValueError("未知音色：%s（可用：%s）" % (voice, ", ".join(cfg["voices"])))
    speed = float(speed or 1.0)
    out_dir = out_dir or os.environ.get("MEDIA_WORK_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "work"))
    stem = stem or "tts_%d_%s" % (int(time.time()), uuid.uuid4().hex[:6])
    os.makedirs(out_dir, exist_ok=True)

    sentences = merge_short(split_sentences(text))
    if not sentences:
        raise ValueError("没有可合成的句子")

    pool = get_pool()
    sr = pool.sample_rate
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(sentences))) as ex:
        arrays = list(ex.map(lambda s: pool.synth(pre_normalize(s), voice, speed), sentences))
    synth_sec = round(time.time() - t0, 2)

    pieces, segs, cursor = [], [], 0.0
    for i, (s, arr) in enumerate(zip(sentences, arrays)):
        dur = len(arr) / sr
        pieces.append(arr)
        segs.append({"index": i, "text": s, "start": round(cursor, 3),
                     "end": round(cursor + dur, 3)})
        cursor += dur + SENTENCE_GAP
        if i < len(sentences) - 1:
            pieces.append(np.zeros(int(sr * SENTENCE_GAP), dtype=np.float32))
    speech_np = np.concatenate(pieces)

    import torch
    import torchaudio

    speech = torch.from_numpy(speech_np).unsqueeze(0)
    wav_path = os.path.join(out_dir, stem + ".wav")
    torchaudio.save(wav_path, speech, sr)

    total = speech.shape[1] / sr
    srt_path = os.path.join(out_dir, stem + ".srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for n, s in enumerate(segs, 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (n, _srt_ts(s["start"]), _srt_ts(s["end"]), s["text"]))

    seg_path = os.path.join(out_dir, stem + ".segments.json")
    with open(seg_path, "w", encoding="utf-8") as f:
        json.dump({"voice": voice, "speed": speed, "sample_rate": sr,
                   "duration": round(total, 3), "segments": segs}, f, ensure_ascii=False, indent=2)

    return {"stem": stem, "audio_path": wav_path, "srt_path": srt_path,
            "segments_path": seg_path, "sample_rate": sr, "duration": round(total, 3),
            "segment_count": len(segs), "synth_sec": synth_sec,
            "rtf": round(synth_sec / total, 3) if total else None,
            "workers": WORKERS, "segments": segs}
