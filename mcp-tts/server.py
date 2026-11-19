#!/usr/bin/env python
"""MCP-TTS：基于 CosyVoice2 的独立语音合成 MCP 服务（stdio 传输）。

对外只暴露两个工具：
  tts_list_voices  —— 列出可用音色
  tts_synthesize   —— 输入纯文本，输出 wav + srt + segments.json，
                       并在返回内容里直接附带 base64 音频（audio 内容块）

协议为 MCP stdio：stdin/stdout 上按行收发 JSON-RPC 2.0，日志一律写 stderr。
CosyVoice2 不提供词级时间戳，因此字幕按句计时（切句点为中英文标点）。
"""

import base64
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

import multiprocessing as mp
import numpy as np

ROOT = "/opt/sefs/CosyVoice"
sys.path.insert(0, os.path.join(ROOT, "third_party", "Matcha-TTS"))
sys.path.insert(0, ROOT)

_V3_DIR = os.path.join(ROOT, "pretrained_models", "Fun-CosyVoice3-0.5B")
_V2_DIR = os.path.join(ROOT, "pretrained_models", "CosyVoice2-0.5B")
# 默认优先用 CosyVoice3（韵律/音色相似度明显更高），目录不在时退回 CosyVoice2
MODEL_DIR = os.environ.get("TTS_MODEL_DIR") or (
    _V3_DIR if os.path.exists(os.path.join(_V3_DIR, "cosyvoice3.yaml")) else _V2_DIR)
IS_V3 = os.path.exists(os.path.join(MODEL_DIR, "cosyvoice3.yaml"))
# CosyVoice3 的 LLM 是 Qwen 底座：参考文本前必须带这句系统提示，否则一开口就吐结束符
# （实测缺前缀时 flow 拿到的 speech token 只有 1~2 个，报 Kernel size can't be greater than actual input size）
V3_SYS_PROMPT = "You are a helpful assistant.<|endofprompt|>"
VOICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices.json")
DEFAULT_OUT_DIR = "/opt/sefs/opc-workbench/mcp-tts/output"
SENTENCE_GAP = 0.12  # 句间停顿（秒），写进时间轴，保证音频与字幕对齐
SERVER_NAME = "mcp-tts"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2025-06-18"

# —— 多路并发合成 ——
# 本机无 GPU，单进程只能串行合成（上一版一句 10~30s，多句排队到分钟级）。
# 现在按核数起 N 个合成进程，每个进程各持一份模型、自带 核数/N 个线程；
# 句子级任务在进程间分发，多句/多请求同时合成，墙钟时间按并发度下降。
_CPU = os.cpu_count() or 4
# 环境变量 <=0 表示按机器配置自动算：每 24 核开一个进程，最多 8 个，线程数 = 核数/进程数
WORKERS = int(os.environ.get("TTS_WORKERS", "0"))
if WORKERS <= 0:
    WORKERS = max(2, min(8, _CPU // 24))
# 实测（CosyVoice3-0.5B，同一句，单进程）：8 线程 14.1s、16 线程 10.8s、
# 24 线程 10.95s、48 线程 11.5s、72 线程 11.0s——16 线程之后延迟不再下降，
# 但 CPU 时间随线程线性上涨（112s → 790s），多出来的线程全耗在同步等锁上，
# 还会挤占其它并发请求。所以默认 16，而不是核数/进程数。
THREADS_PER_WORKER = int(os.environ.get("TTS_THREADS", "16"))
# 单个句子的合成上限：CPU 上长句可能要 1~2 分钟，给足余量
SYNTH_TIMEOUT = float(os.environ.get("TTS_SYNTH_TIMEOUT", "600"))

# —— 代理模式（2026-11-11 起）——
# 设了 MEDIA_ENGINE_URL 就不在本进程加载模型，合成/音色列表全部转调 media-engine（9200）。
# 原因：网关侧已改用 media_cosyvoice（media-engine 常驻 2 worker），9101 再各持一份模型，
# 同一模型被加载两遍（实测 6×6.5GB + 2×6.7GB ≈ 53GB）。代理后 9101 只保留 MCP 门面。
# 未设置该变量时行为与改造前完全一致，可随时回滚。
MEDIA_ENGINE_URL = os.environ.get("MEDIA_ENGINE_URL", "").rstrip("/")
MEDIA_ENGINE_TOKEN = os.environ.get("MEDIA_ENGINE_TOKEN", "")
MEDIA_TTS_TIMEOUT = float(os.environ.get("MEDIA_TTS_TIMEOUT", "900"))
PROXY_MODE = bool(MEDIA_ENGINE_URL)

# stdout 只承载 JSON-RPC 报文；模型加载等第三方库会往 stdout 打印，
# 所以先把真实 stdout 留一份，再把 sys.stdout 指向 stderr。
_PROTO_OUT = sys.stdout
sys.stdout = sys.stderr


def log(msg):
    sys.stderr.write("[mcp-tts] %s\n" % msg)
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
    """把阿拉伯数字串逐位读成中文，保留十/百等进位不处理（用于版本号这类读法）。"""
    return "".join(_CN_DIGITS[int(c)] if c.isdigit() else c for c in s)


def pre_normalize(text):
    """定点归一化：版本号读成中文，拉丁术语里的点号保持不动。

    CosyVoice 的中文前端会把所有 "." 换成 "。"，导致 CXL.cache 被切成两句，
    因此合成时关闭 text_frontend，改由这里只处理真正需要改写的部分。
    """
    text = re.sub(r"(?<![A-Za-z0-9.])(\d+)\.(\d+)(?![0-9])",
                  lambda m: _cn_number(m.group(1)) + "点" + _cn_number(m.group(2)), text)
    return text


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
    """把过短的句子并入下一句。

    CosyVoice2 对明显短于参考文本的句子容易失控（多生成数秒语音），
    官方代码里也有 len(text) < 0.5 * len(prompt_text) 的告警，这里提前规避。
    """
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


# ---------------------------------------------------------------- 合成引擎
def expected_seconds(text):
    """粗略的时长预期，仅用于识别明显失控的输出（0.14s/字，拉丁字母按 0.6 折算）。"""
    units = 0.0
    for ch in text:
        units += 0.6 if ch.isascii() and ch.isalnum() else 1.0
    return 0.14 * units


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
        # 同一套推理代码，按模型目录里的 yaml 判定版本：CosyVoice3 更接近真人（韵律/音色相似度更高）
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
        # 把参考音频的说话人信息缓存一次，后续逐句合成不再重复提取
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
        """合成一句，带静音裁剪与失控重试。

        CosyVoice2 偶尔会在句末自行续写，产出数倍于正常长度的语音（静音检测
        可排除"只是尾部静音"）。因此按时长预期判定异常，重试取最短的一条。
        """
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
            # 首句就严重失控（>4 倍预期）时，整句重试几乎不会变好；
            # 直接返回最短结果，交给上层按标点分句兜底，避免 3 次空跑。
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
        """按幅度裁掉首尾静音，留 20ms 余量。"""
        import numpy as np

        x = wav.squeeze(0).numpy()
        idx = np.where(np.abs(x) > level)[0]
        if idx.size == 0:
            return wav
        sr = self.cv.sample_rate
        pad = int(0.02 * sr)
        a, b = max(0, int(idx[0]) - pad), min(len(x), int(idx[-1]) + 1 + pad)
        return wav[:, a:b]


# 进程内单例：CosyVoice2 模型只加载一次，音色参考也只注册一次。
# 之前每次 tools/call 都新建 Engine()，导致每合成一句都重新加载 5-8s 模型，
# 高负载时单次加载甚至要 36-62s。
_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def get_engine():
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = Engine()
        return _ENGINE


# ---------------------------------------------------------------- 多路并发合成池
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


# ---------------------------------------------------------------- 代理（media-engine）
def _media_headers():
    h = {"Content-Type": "application/json"}
    if MEDIA_ENGINE_TOKEN:
        h["Authorization"] = "Bearer " + MEDIA_ENGINE_TOKEN
    return h


def _media_json(path, payload=None):
    """GET（payload 为 None）或 POST 到 media-engine，返回解析后的 JSON。"""
    url = MEDIA_ENGINE_URL + path
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_media_headers(),
                                 method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=MEDIA_TTS_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError("media-engine %s 返回 HTTP %s：%s" % (path, e.code, body))
    except urllib.error.URLError as e:
        raise RuntimeError("media-engine 不可达（%s）：%s" % (MEDIA_ENGINE_URL, e.reason))


def _media_download(url_path, dest):
    req = urllib.request.Request(MEDIA_ENGINE_URL + url_path, headers=_media_headers())
    try:
        with urllib.request.urlopen(req, timeout=MEDIA_TTS_TIMEOUT) as r, open(dest, "wb") as f:
            f.write(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("下载 media-engine 产物失败 HTTP %s：%s" % (e.code, url_path))
    except urllib.error.URLError as e:
        raise RuntimeError("下载 media-engine 产物失败：%s" % e.reason)
    return dest


def _proxy_synthesize(args, text):
    """把一次 tts_synthesize 转发给 media-engine，落盘后返回与本地模式同构的结果。"""
    voice = (args.get("voice") or "").strip() or None
    speed = float(args.get("speed") or 1.0)
    out_dir = args.get("out_dir") or DEFAULT_OUT_DIR
    stem = args.get("stem") or "tts_%d_%s" % (int(time.time()), uuid.uuid4().hex[:6])
    os.makedirs(out_dir, exist_ok=True)

    meta = _media_json("/v1/tts", {"text": text, "voice": voice, "speed": speed,
                                   "format": "json", "stem": stem})
    wav_path = _media_download(meta["audio_url"], os.path.join(out_dir, stem + ".wav"))

    segs = meta.get("segments") or []
    srt_path = os.path.join(out_dir, stem + ".srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for n, s in enumerate(segs, 1):
            f.write("%d\n%s --> %s\n%s\n\n"
                    % (n, _srt_ts(s["start"]), _srt_ts(s["end"]), s["text"]))

    seg_path = os.path.join(out_dir, stem + ".segments.json")
    with open(seg_path, "w", encoding="utf-8") as f:
        json.dump({"voice": meta.get("voice") or voice, "speed": speed,
                   "sample_rate": meta.get("sample_rate"),
                   "duration": meta.get("duration"), "segments": segs},
                  f, ensure_ascii=False, indent=2)

    log("代理合成完成：%d 字 / %d 句 / %.2fs"
        % (len(text), len(segs), float(meta.get("synth_sec") or 0)))
    return {"audio_path": wav_path, "srt_path": srt_path, "segments_path": seg_path,
            "sample_rate": meta.get("sample_rate"),
            "duration": round(float(meta.get("duration") or 0), 3),
            "segment_count": len(segs), "synth_sec": meta.get("synth_sec"),
            "rtf": meta.get("rtf"), "model_load_sec": None,
            "workers": meta.get("workers"), "segments": segs,
            "backend": "media-engine"}


# ---------------------------------------------------------------- 工具实现
def tts_list_voices(_args):
    if PROXY_MODE:
        return _media_json("/v1/voices")
    cfg = get_engine().config()
    items = [{"id": k, "description": v.get("description", ""),
              "prompt_wav": v["prompt_wav"], "prompt_text": v["prompt_text"]}
             for k, v in cfg["voices"].items()]
    return {"default": cfg.get("default"), "voices": items}


def tts_synthesize(args):
    text = (args.get("text") or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    if PROXY_MODE:
        return _proxy_synthesize(args, text)
    voice = args.get("voice") or Engine().config().get("default")
    speed = float(args.get("speed") or 1.0)
    out_dir = args.get("out_dir") or DEFAULT_OUT_DIR
    stem = args.get("stem") or "tts_%d_%s" % (int(time.time()), uuid.uuid4().hex[:6])

    os.makedirs(out_dir, exist_ok=True)
    sentences = merge_short(split_sentences(text))
    if not sentences:
        raise ValueError("没有可合成的句子")

    cfg = Engine().config()
    if voice not in cfg["voices"]:
        raise ValueError("未知音色：%s（可用：%s）" % (voice, ", ".join(cfg["voices"])))

    if WORKERS > 1:
        # 并发路径：每句一个任务丢进进程池，多句/多请求同时合成
        pool = get_pool()
        sr = pool.sample_rate
        eng = None
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
        model_load_sec = None
    else:
        eng = get_engine()   # 复用已加载模型；只有本进程首次调用才会加载
        eng.voice(voice)     # 先加载模型并缓存说话人，耗时不计入 RTF
        import torch
        import torchaudio

        pieces, segs = [], []
        sr = None
        cursor = 0.0
        t0 = time.time()
        for i, s in enumerate(sentences):
            wav = eng.synth(pre_normalize(s), voice, speed)
            sr = sr or eng.cv.sample_rate
            dur = wav.shape[1] / sr
            pieces.append(wav)
            segs.append({"index": i, "text": s, "start": round(cursor, 3),
                         "end": round(cursor + dur, 3)})
            cursor += dur + SENTENCE_GAP
            if i < len(sentences) - 1:
                pieces.append(torch.zeros(1, int(sr * SENTENCE_GAP)))
            log("句 %d/%d 完成 %.2fs" % (i + 1, len(sentences), dur))

        speech = torch.cat(pieces, dim=1)
        synth_sec = round(time.time() - t0, 2)
        model_load_sec = eng.load_sec

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

    elapsed = synth_sec
    return {"audio_path": wav_path, "srt_path": srt_path, "segments_path": seg_path,
            "sample_rate": sr, "duration": round(total, 3), "segment_count": len(segs),
            "synth_sec": synth_sec, "rtf": round(elapsed / total, 3),
            "model_load_sec": model_load_sec, "workers": WORKERS, "segments": segs}


def _srt_ts(sec):
    ms = int(round(sec * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


TOOLS = [
    {
        "name": "tts_list_voices",
        "description": "列出 MCP-TTS 可用音色（CosyVoice2 zero-shot 克隆，需参考音频）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "tts_synthesize",
        "description": "把一段中英文混排文本合成为音频，输出句级 SRT 与 segments.json 时间轴；默认同时返回 base64 wav 音频内容块。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要朗读的纯文本，可含换行"},
                "voice": {"type": "string", "description": "音色 id，省略则用默认音色"},
                "speed": {"type": "number", "description": "语速倍率，默认 1.0"},
                "out_dir": {"type": "string", "description": "输出目录"},
                "stem": {"type": "string", "description": "输出文件名前缀，不含扩展名"},
                "return_audio": {"type": "boolean",
                                 "description": "是否在返回内容里直接附带 base64 音频（默认 true）"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
]


# ---------------------------------------------------------------- 协议循环
_OUT_LOCK = threading.Lock()


def _send(obj):
    with _OUT_LOCK:
        _PROTO_OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
        _PROTO_OUT.flush()


def _result(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


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
            if name == "tts_list_voices":
                payload = tts_list_voices(args)
            elif name == "tts_synthesize":
                payload = tts_synthesize(args)
            else:
                _result(rid, {"content": [{"type": "text", "text": "未知工具 %s" % name}], "isError": True})
                return
            content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]
            if name == "tts_synthesize" and args.get("return_audio", True):
                audio_path = payload.get("audio_path")
                if audio_path and os.path.exists(audio_path):
                    with open(audio_path, "rb") as f:
                        content.append({"type": "audio",
                                        "data": base64.b64encode(f.read()).decode("ascii"),
                                        "mimeType": "audio/wav"})
            _result(rid, {"content": content, "isError": False})
        except Exception as e:
            log(traceback.format_exc())
            _result(rid, {"content": [{"type": "text", "text": "合成失败：%s" % e}], "isError": True})
    elif method == "ping":
        _result(rid, {})
    elif rid is not None:
        _error(rid, -32601, "未实现的方法 %s" % method)
    # 通知类消息（notifications/initialized 等）不需要回复


def _handle_safe(req):
    try:
        handle(req)
    except Exception:
        log(traceback.format_exc())


def main():
    if PROXY_MODE:
        log("代理模式：合成转发到 %s（本进程不加载模型）" % MEDIA_ENGINE_URL)
    else:
        log("启动，模型目录 %s，并发 %d 进程 × %d 线程"
            % (MODEL_DIR, WORKERS, THREADS_PER_WORKER))
        if WORKERS > 1:
            # 预热进程池：首通电话不再等 1 分钟模型加载
            threading.Thread(target=lambda: _poolload(), daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log("收到非 JSON 行，已忽略")
            continue
        for r in (req if isinstance(req, list) else [req]):
            # 合成请求并行处理：多路同时在跑时不再互相排队（stdin 读取仍然顺序，但会话已经能并发）
            if isinstance(r, dict) and r.get("method") == "tools/call":
                threading.Thread(target=_handle_safe, args=(r,), daemon=True).start()
            else:
                _handle_safe(r)


def _poolload():
    try:
        get_pool()
    except Exception:
        log("预热合成池失败：%s" % traceback.format_exc())


if __name__ == "__main__":
    main()
