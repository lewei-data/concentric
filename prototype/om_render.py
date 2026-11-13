"""真实样片渲染：分镜 → MCP-TTS 旁白 → OpenMontage(Remotion) 出片 → 画面验收门。

与旧 animatic 的区别：旧版把分镜文字画到静态 PNG 再拼成无声 MP4；
这里每一镜都落到 OpenMontage 的图形场景组件（进度条/柱状图/数据卡/引述框/标题卡），
旁白由 MCP-TTS(CosyVoice2) 合成，音画按旁白实测时长对齐，最后过验收门。

产物仍写 OPC_VAULT（默认 /opt/sefs/obsidian），不落临时目录：
  <item>/05-preview.mp4              验收通过的样片
  <item>/_render/                    中间件（分段旁白、props、manifest、失败片）
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageChops, ImageStat

try:
    import illustration as IL
except Exception:  # SVG 资产库不可用时回退到原有矢量场景
    IL = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VAULT = os.environ.get("OPC_VAULT", "/opt/sefs/obsidian")
ROOT = os.path.join(VAULT, "OPC", "交付物")

REMOTION_DIR = os.environ.get("OPC_REMOTION_DIR", "/opt/sefs/OpenMontage/remotion-composer")
TTS_RUN = os.environ.get("OPC_TTS_RUN", "/opt/sefs/mcp-tts/run.sh")
TTS_VOICE = os.environ.get("OPC_TTS_VOICE", "female-zh-short")
TTS_SPEED = float(os.environ.get("OPC_TTS_SPEED", "0.7"))  # CosyVoice2 语速偏快，压到 0.7
NODE_BIN = os.environ.get("OPC_NODE_BIN", "/opt/sefs/tools/node22/bin")

# 交付规格：正常由 server 传入《项目配置》的交付合同（DELIVERY[pid]）；
# 这里只作为单独调用 om_render 时的兜底，渲染参数不再写死在业务代码里。
DEFAULT_SPEC = {"width": 1080, "height": 1920, "fps": "30/1", "video_codec": "h264",
                "sample_rate": 48000, "channels": 2, "duration_tolerance": 2.5,
                "min_mean_db": -45.0, "max_peak_db": -1.0}


def _spec(spec=None):
    """合并交付合同与兜底值；空值不覆盖兜底。"""
    s = dict(DEFAULT_SPEC)
    s.update({k: v for k, v in (spec or {}).items() if v not in (None, "", [])})
    return s


def _fps_int(value):
    """'30/1' / 30 / '29.97' → Remotion 需要的整数帧率。"""
    try:
        return max(1, int(float(str(value).split("/")[0])))
    except (TypeError, ValueError):
        return 30

RENDER_VERSION = "om-7"   # 场景规则/字幕规则变化时递增，避免命中旧缓存
RENDER_TIMEOUT = int(os.environ.get("OPC_RENDER_TIMEOUT", "900"))
TTS_TIMEOUT = int(os.environ.get("OPC_TTS_TIMEOUT", "300"))
# Remotion 并发渲染线程数：机器核数多时可上调；默认 8（原先硬编码 4）
RENDER_CONCURRENCY = int(os.environ.get("OPC_RENDER_CONCURRENCY", "8"))
TZ = timezone(timedelta(hours=8))

# 每个项目一套浅色纸感主题，避免看起来像深色科技模板
THEME_BG = {"wenren": "#F5F1E8", "gou": "#FFF7ED", "tongxinyuan": "#F5F3FF"}
THEME_SURFACE = {"wenren": "#FFFDF7", "gou": "#FFFFFF", "tongxinyuan": "#FFFFFF"}

# 渲染串行锁（多项目/多 trace 共用，避免 CPU 抢跑与 TTS 进程打架）
_RENDER_LOCK_PATH = os.path.join(ROOT, ".render.lock")


def _log(msg):
    print(f"[render] {msg}", flush=True)


def _now():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _clean_env():
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    env["PATH"] = NODE_BIN + ":" + env.get("PATH", "")
    env["NO_UPDATE_NOTIFIER"] = "1"
    env["NODE_NO_WARNINGS"] = "1"
    return env


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    for p in ("/opt/sefs/tools/ffmpeg/ffmpeg", "/opt/sefs/tools/ffmpeg"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise RuntimeError("找不到 ffmpeg")


FF = None


def _ff():
    global FF
    if FF is None:
        FF = _ffmpeg()
    return FF


def _probe(path):
    """解析 ffmpeg -i 的输出，拿到时长/分辨率/有无音轨。"""
    r = subprocess.run([_ff(), "-i", path], capture_output=True, text=True, timeout=60)
    out = r.stderr
    dur = 0.0
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    vw = vh = 0
    m = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", out)
    if m:
        vw, vh = int(m.group(1)), int(m.group(2))
    return {"duration": dur, "width": vw, "height": vh,
            "has_video": vh > 0, "has_audio": bool(re.search(r"Audio:", out))}


def _mean_volume(path):
    """整条音轨平均音量，用于判断旁白是否真的有声。"""
    r = subprocess.run([_ff(), "-i", path, "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=120)
    m = re.search(r"mean_volume: (-?\d+(?:\.\d+)?) dB", r.stderr)
    return float(m.group(1)) if m else -999.0


def _run(cmd, cwd=None, timeout=RENDER_TIMEOUT):
    r = subprocess.run(cmd, cwd=cwd, env=_clean_env(), capture_output=True,
                       text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ---------------------------------------------------------------- MCP-TTS 客户端
class TTSClient:
    """常驻 MCP-TTS 进程（stdio JSON-RPC），模型只加载一次，多条目复用。"""

    def __init__(self):
        self.proc = None
        self.rid = 0
        self.errlog = None
        self.lock = threading.Lock()      # 并行出片时多条目共享同一 MCP-TTS 进程

    def _start(self):
        os.makedirs(ROOT, exist_ok=True)
        self.errlog = open(os.path.join(ROOT, ".tts.stderr.log"), "a", encoding="utf-8")
        self.proc = subprocess.Popen([TTS_RUN], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.errlog,
                                     text=True, bufsize=1, env=_clean_env())
        self._call("initialize", {"protocolVersion": "2025-06-18"}, timeout=30)

    def _call(self, method, params, timeout=TTS_TIMEOUT):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            self.rid += 1
            rid = self.rid
            self.proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError("MCP-TTS 进程已退出")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") != rid:
                    continue
                if "error" in msg:
                    raise RuntimeError(f"MCP-TTS 错误：{msg['error'].get('message')}")
                return msg.get("result") or {}
            raise TimeoutError(f"MCP-TTS {method} 超时（{timeout}s）")

    def synthesize(self, text, out_dir, stem, speed=None):
        res = self._call("tools/call", {"name": "tts_synthesize",
                                        "arguments": {"text": text, "voice": TTS_VOICE,
                                                      "speed": TTS_SPEED if speed is None else speed,
                                                      "out_dir": out_dir, "stem": stem}})
        if res.get("isError"):
            raise RuntimeError(str(res.get("content") or "TTS 合成失败"))
        payload = json.loads(res["content"][0]["text"])
        if not os.path.isfile(payload["audio_path"]):
            raise RuntimeError("TTS 未产出音频")
        return payload


_TTS = None
_TTS_LOCK = threading.Lock()


def _tts():
    global _TTS
    with _TTS_LOCK:
        if _TTS is None:
            _TTS = TTSClient()
        return _TTS


# ---------------------------------------------------------------- 分镜 → 图形场景
_SILENT_MARKS = ("无旁白", "不配音", "无人声", "不开口", "仅字幕", "静默", "静音")
_META_MARKS = ("占位", "不进成片", "示意", "音效", "BGM", "烧录", "角标",
               "界面文字", "字幕卡", "群消息", "提示音", "拟音")
_LABEL = re.compile(r"(旁白|口播|台词|字幕|VO|画外音)\s*[①②③④⑤⑥⑦⑧⑨⑩0-9]*\s*[：:=＝]\s*")


def _narration_text(raw):
    """从分镜台词里抽出真正要念的一句，返回 (文本, 是否明确静音)。

    分镜里的「旁白①：「…」」「界面文字：…」以及「本镜旁白不开口」
    都是制作说明，不能直接交给 TTS，否则样片会念出生产备注。
    """
    s = str(raw or "").strip()
    if not s:
        return "", True
    labels = list(_LABEL.finditer(s))
    if labels:
        s = s[labels[-1].end():]
    s = s.strip().strip("「」“”\"'")
    if any(k in s for k in _SILENT_MARKS):
        return "", True
    segs = [x.strip() for x in re.split(r"[；;。]", s) if x.strip()]
    segs = [x for x in segs if not any(k in x for k in _META_MARKS)]
    s = "。".join(segs).strip().strip("「」“”\"'— ")
    if not s or s in ("—", "-", "无"):
        # 只剩制作说明（占位/示意/界面文字）时不算静音，交给文案兜底
        return "", not any(k in str(raw) for k in _META_MARKS)
    if any(k in s for k in _META_MARKS):
        return "", False
    return s, False


def _copy_fallback(item, idx, total):
    """分镜没给台词（或只有占位说明）时，从成稿文案里取一句兜底。"""
    copy = str(item.get("copy") or "")
    drafts = [d.strip() for d in re.split(r"／|/", copy) if d.strip()]
    draft = drafts[-1] if drafts else copy
    draft = re.sub(r"^(稿|方案|版本)\s*\d*\s*[：:]\s*", "", draft)
    sents = [x.strip() for x in re.split(r"[。！？；;!?]", draft) if x.strip()]
    if not sents:
        return ""
    if idx >= total - 1:
        return sents[-1]
    return sents[min(idx, len(sents) - 1)]


def _clause(text, n=22):
    s = re.split(r"[，,。；;！!？?\n]", str(text or "").strip())
    s = next((x.strip() for x in s if x.strip()), "")
    return s[:n]


def _short_title(text):
    for kw, title in (("token", "Token 消耗"), ("成功率", "协作成功率"),
                      ("进度", "执行进度"), ("任务", "实测数据")):
        if kw in text.lower() or kw in text:
            return title
    return _clause(text, 14)


def _nums(text):
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(text or ""))]


def _visual_title(visual, n=16):
    """画面描述取短标题：截到第一个冒号/逗号/分句，避免出现半句说明。"""
    s = re.split(r"[：:（(，,。；;]", str(visual or ""))[0].strip()
    if "「" in s and "」" not in s:
        s = s.split("「")[0]
    s = s.strip().strip("｜| 　“”\"'")
    return (s or _clause(visual, n))[:n]


def _scene_for(shot, idx, total, item, project, line):
    """把一镜的「画面 + 台词」映射成 OpenMontage 图形场景。

    规则是保守的启发式：识别到数字/百分比/倍数就上图表或数据卡，
    识别不到就落到引述框，最后一镜固定收在标题卡。
    """
    visual = str(shot.get("visual") or "")
    text = visual + " " + line
    nums = _nums(text)
    last = idx >= total - 1

    if last or "落版" in visual or "结尾" in visual:
        title = (item.get("cover") or _clause(line or visual, 14) or item["title"])[:16]
        sub = line if line else project["name"]
        return {"type": "hero_title", "text": title, "heroSubtitle": sub,
                "accentColor": project["color"]}

    if nums and any(k in text for k in ("成功率", "下降", "提升", "增长",
                                         "低约", "降低", "高出", "低于", "少三成")):
        n = next((x for x in nums if "%" in text and x <= 100), nums[0])
        unit = "%" if "%" in text else ("+" if n >= 100 else "")
        return {"type": "stat_card", "stat": f"{n:g}{unit}",
                "subtitle": (line or _clause(visual, 24))[:36]}

    if "%" in text and nums:
        pct = next((n for n in nums if 0 < n <= 100), 60.0)
        return {"type": "progress_bar", "progress": pct,
                "title": _short_title(text),
                "progressLabel": line or f"进度停在 {pct:g}%"}

    if "倍" in text and len(nums) >= 2 and max(nums) > min(nums):
        lo, hi = min(nums), max(nums)
        left = "串行" if "串行" in text else "基准"
        right = "组队" if "组队" in text else "实测"
        return {"type": "bar_chart", "title": _short_title(text),
                "chartData": [{"label": left, "value": lo}, {"label": right, "value": hi}],
                "chartAnimation": "grow-up"}

    kind = "quote" if ("“" in text or "”" in text or "你说" in text or "你先" in text) else \
        ("warning" if any(k in text for k in ("卡", "失败", "问题", "坑", "崩")) else "info")
    return {"type": "callout", "callout_type": kind, "title": _visual_title(visual),
            "text": line[:52]}


def _caption_words(line, start, end):
    """把整句台词切成小句，按字数比例分配时间，供 CaptionOverlay 逐句显示。"""
    parts = [p for p in re.split(r"([，,。；;！!？?])", str(line or "").strip()) if p]
    chunks = []
    buf = ""
    for p in parts:
        if p in "，,。；;！!？?":
            buf += p
            if buf.strip():
                chunks.append(buf.strip())
                buf = ""
        else:
            buf += p
    if buf.strip():
        chunks.append(buf.strip())
    if not chunks:
        return []
    total_chars = sum(len(c) for c in chunks) or 1
    out, cur = [], start
    for i, c in enumerate(chunks):
        share = (end - start) * len(c) / total_chars
        out.append({"word": c, "startMs": int(cur * 1000),
                    "endMs": int((cur + share) * 1000),
                    "pageBreakAfter": i == len(chunks) - 1 or c[-1:] in "。！？?"})
        cur += share
    return out


def _hash(item, project, spec=None):
    sp = _spec(spec)
    payload = json.dumps({
        "storyboard": item.get("storyboard"), "copy": item.get("copy"),
        "cover": item.get("cover"), "color": project.get("color"),
        "tone": item.get("toneProfile"), "voice": TTS_VOICE,
        "w": sp["width"], "h": sp["height"], "fps": sp["fps"], "version": RENDER_VERSION,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _shot_tts(line, seg_dir, idx, attempt=0, tag=""):
    """同一句台词只合成一次（返工只改封面/排版时不必重跑模型）。"""
    key = hashlib.sha1((TTS_VOICE + "\x00" + str(TTS_SPEED) + "\x00" + line).encode("utf-8")).hexdigest()[:10]
    stem = f"shot{idx+1:02d}-{key}-a{attempt}{tag}"
    meta_path = os.path.join(seg_dir, stem + ".meta.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            if os.path.isfile(meta["audio_path"]):
                return meta
        except Exception:
            pass
    payload = _tts().synthesize(line, seg_dir, stem)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return payload


def _synth_try(line, seg_dir, idx, tag="", retries=2):
    """按字均时长复核一版合成；返回合格结果，或最长的一版（供上层兜底）。

    mcp-tts 服务内部已经对整句重试 3 次；这里最多再复核 2 次，
    避免 3×3 的嵌套重试把一条视频拖到十几分钟。
    服务端遇到“首句严重超长”会立即返回，外层再按标点分句兜底。
    """
    cps = max(1, len(re.sub(r"\s", "", line)))
    # 极短镜（1-3 字）本身可能带较长停顿，放宽上限；正常口播按 0.12-0.42 秒/字
    lo = 0.12
    hi = 0.90 if cps <= 3 else 0.42
    best = None
    for attempt in range(retries):
        payload = _shot_tts(line, seg_dir, idx, attempt, tag)
        rate = float(payload.get("duration") or 0) / cps
        if lo <= rate <= hi:
            return payload
        if best is None or abs(rate - 0.2) < abs(best[0] - 0.2):
            best = (rate, payload)
    return None


def _synth_checked(line, seg_dir, idx):
    """CosyVoice2 时长不稳定（会在句号处提前收尾，或自行续写），逐层兜底。

    先整句合成三次；仍偏短就按标点拆成小句分别合成再拼接，
    避开「读半句就停」的模型行为；小句也异常才让渲染失败。
    """
    whole = _synth_try(line, seg_dir, idx)
    if whole:
        return whole
    clauses = [x.strip() for x in re.split(r"[。！？；;!?，,]", line) if x.strip()]
    if len(clauses) >= 2:
        files, total = [], 0.0
        for j, c in enumerate(clauses):
            # 分句后句子更短，偶尔第一次仍会失控；给 2 次复核机会（每次服务端首句超长会快速返回）
            part = _synth_try(c, seg_dir, idx, tag=f"c{j}", retries=2)
            if not part:
                files = []
                break
            files.append(part["audio_path"])
            total += float(part.get("duration") or 0)
        if files:
            out = os.path.join(seg_dir, f"shot{idx+1:02d}-clause-{hashlib.sha1(line.encode()).hexdigest()[:10]}.wav")
            ok, _ = _concat_audio(files, out, seg_dir)
            if ok:
                _log(f"第 {idx+1} 镜整句偏短，已按 {len(files)} 个小句合成并拼接（{total:.1f}s）")
                return {"audio_path": out, "duration": total, "segments": [],
                        "text": line, "clause_fallback": True}
    # 2) 无标点或分句仍失败：按 4-6 字均衡切块，避免单句失控拖垮整条视频
    plain = re.sub(r"[。！？；;!?，,、\s]", "", line)
    if len(plain) >= 6:
        n_chunks = max(2, (len(plain) + 5) // 6)
        size = (len(plain) + n_chunks - 1) // n_chunks
        chunks = [plain[i:i + size] for i in range(0, len(plain), size)]
        chunks = [c for c in chunks if len(c) >= 2]
        if len(chunks) >= 2 and chunks != clauses:
            files, total = [], 0.0
            for j, c in enumerate(chunks):
                part = _synth_try(c, seg_dir, idx, tag=f"w{j}", retries=2)
                if not part:
                    files = []
                    break
                files.append(part["audio_path"])
                total += float(part.get("duration") or 0)
            if files:
                out = os.path.join(seg_dir, f"shot{idx+1:02d}-chunk-{hashlib.sha1(line.encode()).hexdigest()[:10]}.wav")
                ok, _ = _concat_audio(files, out, seg_dir)
                if ok:
                    _log(f"第 {idx+1} 镜整句异常，已按 {len(files)} 个字块合成并拼接（{total:.1f}s）")
                    return {"audio_path": out, "duration": total, "segments": [],
                            "text": line, "chunk_fallback": True}
    cps = max(1, len(re.sub(r"\s", "", line)))
    raise RuntimeError(f"旁白合成时长异常（{cps} 字整句、分句与字块均未达标），已重试")


# ---------------------------------------------------------------- 音轨拼接
def _pad_audio(src, dst, target_sec):
    pad = max(0.0, target_sec - 0.05)
    _run([_ff(), "-y", "-v", "error", "-i", src, "-af", f"apad=pad_dur={pad:.2f}",
          "-t", f"{target_sec:.2f}", "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", dst],
         timeout=120)
    return os.path.isfile(dst)


def _silence(dst, sec):
    """分镜明确写「本镜不配音」时生成等长静音段。"""
    code, _ = _run([_ff(), "-y", "-v", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=mono", "-t", f"{sec:.2f}",
                    "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", dst], timeout=120)
    return code == 0 and os.path.isfile(dst)


def _concat_audio(parts, out_path, work):
    lst = os.path.join(work, "audio-list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % p.replace("'", "'\\''"))
    code, out = _run([_ff(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
                      "-i", lst, "-ar", "44100", "-ac", "1",
                      "-c:a", "pcm_s16le", out_path], timeout=180)
    return code == 0 and os.path.isfile(out_path), out


# ---------------------------------------------------------------- 画面验收门
def validate(path, expected_sec, sample_dir, spec=None):
    """验收门：分辨率、音轨、音量、时长、画面动态、色彩丰富度。

    精确门槛（分辨率/时长容差/静音下限）来自交付合同 spec，不再写死。
    纯文字分镜卡会同时死在「无音轨」和「画面无动态」两项上，
    这正是旧样片被拦下的原因。
    """
    sp = _spec(spec)
    w, h = sp["width"], sp["height"]
    _ff()
    checks, reasons = {}, []
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    checks["文件体积"] = size
    if size < 400_000:
        reasons.append(f"文件过小（{size/1024:.0f} KB），疑似空片")

    info = _probe(path) if size else {"duration": 0, "width": 0, "height": 0,
                                      "has_video": False, "has_audio": False}
    checks.update({k: info[k] for k in ("duration", "width", "height",
                                       "has_video", "has_audio")})
    if not info["has_video"]:
        reasons.append("没有视频流")
    else:
        if (info["width"], info["height"]) != (w, h):
            reasons.append(f"分辨率 {info['width']}x{info['height']}，要求 {w}x{h}")
    if not info["has_audio"]:
        reasons.append("没有音轨（旁白缺失）")
    else:
        vol = _mean_volume(path)
        checks["meanVolumeDb"] = round(vol, 1)
        if vol <= sp["min_mean_db"]:
            reasons.append(f"音轨近乎静音（mean_volume {vol:.1f} dB）")
    if expected_sec and abs(info["duration"] - expected_sec) > sp["duration_tolerance"]:
        reasons.append(f"时长 {info['duration']:.1f}s 与分镜 {expected_sec:.1f}s 偏差过大")

    # 动态与色彩：按 3 秒抽帧比较相邻帧
    os.makedirs(sample_dir, exist_ok=True)
    for f in os.listdir(sample_dir):
        if f.endswith(".png"):
            os.remove(os.path.join(sample_dir, f))
    _run([_ff(), "-v", "error", "-y", "-i", path, "-vf", "fps=1/3,scale=270:480",
          "-frames:v", "16", os.path.join(sample_dir, "s%02d.png")], timeout=180)
    frames = sorted(f for f in os.listdir(sample_dir) if f.endswith(".png"))
    imgs = []
    for f in frames:
        try:
            imgs.append(Image.open(os.path.join(sample_dir, f)).convert("RGB"))
        except Exception:
            pass
    diffs, colors = [], []
    for i, im in enumerate(imgs):
        if i:
            d = ImageChops.difference(imgs[i - 1], im).convert("L")
            diffs.append(ImageStat.Stat(d).mean[0])
        small = im.resize((135, 240))
        entries = small.getcolors(maxcolors=1 << 20) or []
        total = 135 * 240
        colors.append(sum(1 for cnt, _ in entries if cnt >= total * 0.003))
    checks["frames"] = len(imgs)
    checks["motionMax"] = round(max(diffs), 2) if diffs else 0.0
    checks["motionMean"] = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
    checks["motionMovingRatio"] = round(
        sum(1 for d in diffs if d > 0.3) / len(diffs), 2) if diffs else 0.0
    checks["colorCount"] = int(sum(colors) / len(colors)) if colors else 0
    if len(imgs) < 4 or not diffs:
        reasons.append("抽帧失败，无法判断画面")
    else:
        # 文字卡拼贴：整片几乎无变化（旧样片 movingRatio≈0.07，max<5）；
        # 正常分镜里有「整镜定版停留」，会拉低 movingRatio，故不能要求过半帧变化
        if checks["motionMax"] < 5.0 or checks["motionMean"] < 0.4 \
                or checks["motionMovingRatio"] < 0.25:
            reasons.append("画面基本静止（疑似文字卡拼贴）")
        if checks["colorCount"] < 8:
            reasons.append(f"色彩层次不足（平均 {checks['colorCount']} 种主色，疑似纯文字卡）")

    checks["pass"] = not reasons
    checks["reasons"] = reasons
    return checks


# ---------------------------------------------------------------- 主流程
def produce(item, project, tone="", spec=None):
    """为一条内容出真实样片；成功返回 ok=True，失败不覆盖已通过验收的旧片。

    spec 为交付合同（宽/高/帧率/时长容差/静音下限），由 server 从
    《项目配置》读取后传入；缺省时用 DEFAULT_SPEC。
    """
    import deliverables as DL
    sp = _spec(spec)
    files = DL.plan(item["traceId"], item["projectId"], item["id"])
    item_dir = DL._local(files["dir"])
    work = os.path.join(item_dir, "_render")
    os.makedirs(work, exist_ok=True)
    video_path = DL._local(files["video"])
    manifest_path = os.path.join(work, "manifest.json")
    digest = _hash(item, project, sp)

    def result(ok, reason="", check=None, video="", duration=0, scenes=None):
        return {"ok": ok, "reason": reason, "check": check or {},
                "video": video, "videoUrl": files["video"], "coverUrl": files["cover"],
                "duration": round(duration, 2), "scenes": scenes or [],
                "engine": "MCP-TTS(CosyVoice2) + OpenMontage/Remotion",
                "renderedAt": _now(),
                # 与 OPC 流水线对齐的溯源字段：OM 产物里同样能查到 trace_id / 阶段
                "traceId": item.get("traceId"), "itemId": item.get("id"), "omStage": "compose"}

    # 命中缓存：分镜/文案/音色都没变，且旧片仍能过验收门，直接复用
    try:
        with open(manifest_path, encoding="utf-8") as f:
            man = json.load(f)
        if man.get("hash") == digest and os.path.isfile(video_path):
            check = validate(video_path, man.get("duration") or 0,
                             os.path.join(work, "check"), sp)
            if check["pass"]:
                return result(True, check=check, video=video_path,
                              duration=man.get("duration") or 0,
                              scenes=man.get("scenes"))
    except Exception:
        pass

    shots = DL.normalize_storyboard(item.get("storyboard"), item.get("durationSec", 45))
    if not shots:
        return result(False, "没有可用的分镜，无法出片")

    illus_map = {}
    if IL:
        try:
            illus_map = IL.ensure_assets(item, item["projectId"], shots, files, REMOTION_DIR)
        except Exception as e:
            _log(f"{item['id']} SVG 插图生成失败，全部回退矢量场景：{type(e).__name__}: {e}")

    os.makedirs(ROOT, exist_ok=True)
    lock_fd = open(_RENDER_LOCK_PATH, "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    t_start = time.time()
    try:
        _log(f"{item['id']} 开始出片：{len(shots)} 镜，TTS 音色 {TTS_VOICE}")
        # 1) 逐镜旁白
        scenes, words, segments, duration = [], [], [], 0.0
        for i, s in enumerate(shots):
            line, silent = _narration_text(s.get("line"))
            if not line and not silent:
                line = _copy_fallback(item, i, len(shots))
            scene = _scene_for(s, i, len(shots), item, project, line)
            ill = illus_map.get(i)
            if ill:
                scene = {
                    "type": "illustration_scene",
                    "source": ill["source"],
                    "imageFit": "contain",
                    "imageLayout": ill.get("layout", "center"),
                    "imagePadding": 64,
                    "imageShadow": True,
                    "imageAnimation": ill.get("animation", "float-in"),
                    "imageKicker": str(item.get("title") or "")[:16],
                    "imageIndex": i + 1,
                    "imageTotal": len(shots),
                }
            seg_dir = os.path.join(work, "tts")
            os.makedirs(seg_dir, exist_ok=True)
            planned = float(s.get("dur") or 0) or item.get("durationSec", 45) / len(shots)
            payload = None
            if line:
                try:
                    payload = _synth_checked(line, seg_dir, i)
                except Exception as e:
                    _log(f"镜{i+1} 旁白合成失败，降级为字幕+静音：{type(e).__name__}: {e}")
                if payload:
                    audio_dur = float(payload.get("duration") or 0)
                    sec = max(planned, audio_dur + 0.5)
                    segments.append((payload["audio_path"], sec, audio_dur))
                else:
                    audio_dur = 0.0
                    sec = planned
                    segments.append(None)
                # 即使降级为静音，字幕仍保留，观众至少能读到这句话
                words += _caption_words(line, duration, duration + sec)
            else:
                audio_dur = 0.0
                sec = planned
                segments.append(None)
            scene.update({"in_seconds": duration, "out_seconds": duration + sec,
                          "line": line, "ttsFallback": bool(line and payload is None)})
            scenes.append(scene)
            duration += sec
            _log(f"{item['id']} 镜{i+1} 旁白 {audio_dur:.1f}s / 定长 {sec:.1f}s → {scene['type']}"
                 + ("" if audio_dur else "（静音）"))

        # 2) 音轨：每镜按定长补静音后拼接
        parts = []
        for idx, seg in enumerate(segments):
            dst = os.path.join(work, f"narr{idx+1:02d}.wav")
            if seg is None:
                sec = scenes[idx]["out_seconds"] - scenes[idx]["in_seconds"]
                if not _silence(dst, sec):
                    return result(False, f"第 {idx+1} 镜静音生成失败")
                parts.append(dst)
                continue
            src, sec, _ad = seg
            if not _pad_audio(src, dst, sec):
                return result(False, f"第 {idx+1} 镜音频补齐失败")
            parts.append(dst)
        narration = os.path.join(work, "narration.wav")
        ok, out = _concat_audio(parts, narration, work)
        if not ok:
            return result(False, f"旁白拼接失败：{out[-160:]}")
        _log(f"{item['id']} 旁白完成：{duration:.1f}s，TTS 段耗时 {time.time() - t_start:.1f}s")

        # Remotion 的 Audio 只接受可下载资源，把旁白放进 remotion public 目录
        pub_dir = os.path.join(REMOTION_DIR, "public", "om")
        os.makedirs(pub_dir, exist_ok=True)
        pub_name = f"{item['traceId'][-8:]}-{item['id']}-{digest[:8]}.wav".replace("/", "_")
        shutil.copyfile(narration, os.path.join(pub_dir, pub_name))
        narration_src = f"om/{pub_name}"

        # 3) OpenMontage(Remotion) 渲染
        props = {
            "meta": {"source": "opc", "traceId": item.get("traceId"),
                     "itemId": item.get("id"), "omStage": "compose"},
            "cuts": [{"id": f"cut-{i+1:02d}", "source": "", **sc} for i, sc in enumerate(scenes)],
            "overlays": [],
            "captions": words,
            "audio": {"narration": {"src": narration_src, "volume": 1.0}},
            "themeConfig": {
                "primaryColor": project.get("color") or "#2563EB",
                "accentColor": "#0EA5E9",
                "backgroundColor": THEME_BG.get(item["projectId"], "#F7F6F2"),
                "surfaceColor": THEME_SURFACE.get(item["projectId"], "#FFFFFF"),
                "textColor": "#1F2937", "mutedTextColor": "#6B7280",
                "headingFont": "Inter", "bodyFont": "Inter", "monoFont": "JetBrains Mono",
                "chartColors": [project.get("color") or "#2563EB", "#0EA5E9",
                                "#10B981", "#F59E0B", "#EC4899", "#8B5CF6"],
                "springConfig": {"damping": 20, "stiffness": 120, "mass": 1},
                "transitionDuration": 0.4,
                "captionHighlightColor": project.get("color") or "#2563EB",
                "captionBackgroundColor": "rgba(255,255,255,0.85)",
            },
        }
        props_path = os.path.join(work, "props.json")
        with open(props_path, "w", encoding="utf-8") as f:
            json.dump(props, f, ensure_ascii=False)
        tmp_video = os.path.join(work, "render.mp4")
        t_remotion = time.time()
        code, out = _run([os.path.join(NODE_BIN, "npx"), "remotion", "render",
                          "src/index.tsx", "Explainer", tmp_video,
                          "--props", props_path, "--width", str(sp["width"]),
                          "--height", str(sp["height"]),
                          "--fps", str(_fps_int(sp["fps"])), "--codec", "h264", "--crf", "23",
                          "--x264-preset", "veryfast",
                          "--concurrency", str(RENDER_CONCURRENCY), "--overwrite"],
                         cwd=REMOTION_DIR)
        if code != 0 or not os.path.isfile(tmp_video):
            return result(False, f"OpenMontage 渲染失败：{out[-220:]}")
        _log(f"{item['id']} Remotion 渲染完成，耗时 {time.time() - t_remotion:.1f}s"
             f"（并发 {RENDER_CONCURRENCY}）")

        check = validate(tmp_video, duration, os.path.join(work, "check"), sp)
        if not check["pass"]:
            bad = os.path.join(work, f"rejected-{_now().replace(':', '')}.mp4")
            shutil.move(tmp_video, bad)
            return result(False, "画面验收未通过：" + "；".join(check["reasons"]),
                          check=check, duration=duration, scenes=scenes)

        os.makedirs(os.path.dirname(video_path), exist_ok=True)
        shutil.move(tmp_video, video_path)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"hash": digest, "duration": duration, "scenes": scenes,
                       "check": check, "video": video_path, "renderedAt": _now(),
                       "traceId": item.get("traceId"), "itemId": item.get("id"),
                       "omStage": "compose"},
                      f, ensure_ascii=False, indent=2)
        _log(f"{item['id']} 出片完成：{duration:.1f}s，总耗时 {time.time() - t_start:.1f}s，验收通过")
        return result(True, check=check, video=video_path, duration=duration, scenes=scenes)
    except Exception as e:
        traceback.print_exc()
        return result(False, f"{type(e).__name__}: {e}")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
