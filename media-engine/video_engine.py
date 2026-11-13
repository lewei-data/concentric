#!/usr/bin/env python
"""media-engine 的视频/数字人能力：员工档案、自我介绍视频、异步任务队列、老板汇报。

从 /opt/sefs/mcp-asr/server.py 迁移（代码复用，不重写）：
  - 员工档案 / 数字人素材解析（employees.json + assets/avatars|faces|videos|portraits）
  - intro 流水线：文案（Hermes 润色，失败回退档案模板）→ TTS → Wav2Lip 口型
    → ffmpeg 合成 720×960 MP4（+faststart），落 assets/intro/<id>.mp4
  - 语音汇报文本整理（Markdown 清洗 + 1-2 句摘要）
唯一行为差异：TTS 不再走 9101 MCP，直接复用本进程的 tts_engine 进程池。

环境变量沿用 ASR_*，同名 MEDIA_* 优先：
  MEDIA_ASSET_DIRS / ASR_ASSET_DIRS          素材根目录（os.pathsep 分隔）
  MEDIA_ASSET_WRITE_DIR / ASR_ASSET_WRITE_DIR 可写目录（默认第一个素材根）
  MEDIA_PROFILES / ASR_PROFILES              员工档案 employees.json
  MEDIA_HERMES_CLI / HERMES_CLI              Hermes CLI，用于生成文案与汇报
  MEDIA_FFMPEG / ASR_FFMPEG                  ffmpeg 路径
  MEDIA_LIPSYNC / ASR_LIPSYNC                是否启用 Wav2Lip 口型（默认 1）
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid

import tts_engine

SERVICE = "media-engine"
WORK_DIR = os.environ.get("MEDIA_WORK_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "work"))


def log(msg):
    sys.stderr.write("[media-engine/video] %s\n" % msg)
    sys.stderr.flush()


def _cfg(name, default=""):
    """读配置：MEDIA_<name> 优先，其次 ASR_<name>（兼容迁移前部署）。"""
    return os.environ.get("MEDIA_" + name) or os.environ.get("ASR_" + name) or default


# ---------------------------------------------------------------- 路径与素材
ASSET_DIRS = [p for p in _cfg("ASSET_DIRS", "/home/boray/opc-workbench/prototype/assets").split(os.pathsep) if p]
ASSET_WRITE_DIR = _cfg("ASSET_WRITE_DIR") or (ASSET_DIRS[0] if ASSET_DIRS else "")
INTRO_DIR = os.path.join(ASSET_WRITE_DIR, "intro") if ASSET_WRITE_DIR else ""
ASSET_URL_BASE = _cfg("ASSET_URL_BASE", "").rstrip("/")
PUBLIC_BASE = _cfg("PUBLIC_BASE", "").rstrip("/")
PROFILES_FILE = _cfg("PROFILES", "/opt/sefs/mcp-asr/employees.json")
HERMES_CLI = _cfg("HERMES_CLI", "/opt/sefs/hermes/run-hermes.sh")

FFMPEG_BIN = (os.environ.get("MEDIA_FFMPEG") or os.environ.get("ASR_FFMPEG")
              or shutil.which("ffmpeg")
              or next((p for p in ("/usr/bin/ffmpeg", "/home/boray/.local/bin/ffmpeg")
                       if os.path.exists(p)), ""))


def _probe_bin():
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


def asset_roots():
    roots = list(ASSET_DIRS)
    if ASSET_WRITE_DIR and ASSET_WRITE_DIR not in roots:
        roots.append(ASSET_WRITE_DIR)
    return roots


def _asset_path(subdir, filename):
    for root in asset_roots():
        path = os.path.join(root, subdir, filename)
        if os.path.exists(path):
            return path
    return ""


def _asset_url(subdir, filename):
    rel = "/%s/%s" % (subdir, filename)
    return (ASSET_URL_BASE + rel) if ASSET_URL_BASE else ("/v1/assets" + rel)


def _asset_url_full(subdir, filename):
    rel = _asset_url(subdir, filename)
    return (PUBLIC_BASE + rel) if (PUBLIC_BASE and rel.startswith("/")) else rel


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
            capture_output=True, text=True, timeout=30, env=FF_ENV)
        return round(float((out.stdout or "0").strip() or 0), 2)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


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
    return os.path.isdir(os.path.join(os.path.dirname(HERMES_CLI), "profiles", pid))


def avatar_profile(aid, name=None, role=None):
    """按员工 ID 找数字人素材：照片必选，仿真人视频素材可插拔（有就返回，没有就只给照片）。

    约定（丢文件即生效，无需改代码）：
      assets/avatars/<id>.png|jpg   头像 / 半身照
      assets/faces/<id>.png         面部特写（口型贴图用）
      assets/videos/<id>.talk.mp4   说话素材（可选）
      assets/videos/<id>.idle.mp4   待机素材（可选）
    """
    if not aid:
        return None
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
        full_body = ""
        for ext in ("png", "jpg"):
            if _asset_path("fullbody", "%s.%s" % (asset_aid, ext)):
                full_body = _asset_url("fullbody", "%s.%s" % (asset_aid, ext))
                break
        id_photo = ""
        for ext in ("png", "jpg"):
            if _asset_path("idphoto", "%s.%s" % (asset_aid, ext)):
                id_photo = _asset_url("idphoto", "%s.%s" % (asset_aid, ext))
                break
        prof["fullBody"] = full_body
        prof["idPhoto"] = id_photo
        prof["photoNote"] = ("全身形象照与证件照均由本人肖像生成：全身照先由 Qwen-Image 扩展，"
                             "再用 InsightFace 还原本人面部特征；证件照为抠图换白底。仅用于内部员工档案。")
        prof["assets"] = {"photo": photo, "face_url": face or None,
                          "video_talk": videos.get("talk"), "video_idle": videos.get("idle")}
        intro_file = os.path.join(INTRO_DIR, "%s.mp4" % aid) if INTRO_DIR else ""
        intro_ready = bool(intro_file and os.path.exists(intro_file))
        intro_meta = intro_meta_of(aid) if intro_ready else {}
        prof["introVideo"] = _asset_url("intro", "%s.mp4" % aid) if intro_ready else ""
        prof["introReady"] = intro_ready
        prof["introText"] = _read_intro_text(aid) if intro_ready else ""
        prof["introDuration"] = intro_meta.get("duration") or 0
    return prof


def list_profiles(aid=None, dept=None, project=None, include_assets=True):
    out = []
    for pid in load_profiles():
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


def list_avatars():
    items = []
    for aid in load_profiles():
        av = avatar_profile(aid)
        if av:
            items.append(av)
    return {"asset_dirs": ASSET_DIRS, "asset_url_base": ASSET_URL_BASE or None,
            "count": len(items), "avatars": items}


# ---------------------------------------------------------------- 情绪化韵律
# CosyVoice 只认参考音色、不接受情绪指令，情绪落到两个可听维度：语速（TTS 原生）+ 音高
# （ffmpeg 变调不变速）。情绪取自员工档案的 emotion 字段。
EMOTION_PRESETS = {
    "cheerful": {"speed": 1.08, "pitch": 1.05},
    "calm":     {"speed": 0.96, "pitch": 0.99},
    "serious":  {"speed": 0.93, "pitch": 0.97},
    "neutral":  {"speed": 1.00, "pitch": 1.00},
}
EMOTION_ALIASES = {"开朗": "cheerful", "活泼": "cheerful", "轻快": "cheerful", "热情": "cheerful",
                   "冷静": "calm", "沉稳": "calm", "温和": "calm", "稳重": "calm",
                   "严肃": "serious", "严谨": "serious", "克制": "serious",
                   "中性": "neutral", "平静": "calm"}
EMOTION_TTS = os.environ.get("MEDIA_EMOTION_TTS", "1") not in ("0", "false", "False", "no")
EMOTION_STRENGTH = float(os.environ.get("MEDIA_EMOTION_STRENGTH", "1.0"))


def norm_emotion(emotion):
    e = (emotion or "").strip().lower()
    e = EMOTION_ALIASES.get(e, e)
    return e if e in EMOTION_PRESETS else "neutral"


def emotion_of(aid=None, employee=None, override=None):
    if override and str(override).strip().lower() != "auto":
        return norm_emotion(override)
    prof = employee or load_profiles().get(aid or "") or {}
    return norm_emotion(prof.get("emotion"))


def _emotion_preset(emotion):
    if not EMOTION_TTS or EMOTION_STRENGTH <= 0:
        return None
    base = EMOTION_PRESETS[norm_emotion(emotion)]
    k = EMOTION_STRENGTH
    speed = round(1.0 + (base["speed"] - 1.0) * k, 3)
    pitch = round(1.0 + (base["pitch"] - 1.0) * k, 3)
    if abs(speed - 1.0) < 0.005 and abs(pitch - 1.0) < 0.005:
        return None
    return {"speed": speed, "pitch": pitch}


def _pitch_shift(wav_path, pitch, stem):
    """变调不变速：asetrate 抬/压音高，紧接 atempo 把时长还原。返回新路径（失败返回原路径）。"""
    if not wav_path or abs(pitch - 1.0) < 0.005 or not FFMPEG_BIN:
        return wav_path
    dst = os.path.join(os.path.dirname(wav_path), "%s.pitch.wav" % stem)
    try:
        rate = 24000                                  # CosyVoice 输出 24k，先统一再变调
        af = ("aresample=%d,asetrate=%d,aresample=%d,atempo=%.4f"
              % (rate, int(round(rate * pitch)), rate, 1.0 / pitch))
        subprocess.run([FFMPEG_BIN, "-y", "-loglevel", "error", "-i", wav_path,
                        "-filter:a", af, "-ac", "1", "-ar", str(rate),
                        "-c:a", "pcm_s16le", dst], check=True, timeout=120)
        return dst if os.path.exists(dst) else wav_path
    except Exception as e:                            # noqa: BLE001 - 变调失败不影响主流程
        log("情绪变调失败（pitch=%.3f）：%s" % (pitch, e))
        return wav_path


def speak(text, voice=None, emotion=None, stem=None, out_dir=None):
    """按情绪合成语音（语速走 TTS，音高走 ffmpeg），返回 tts_engine 的结果字典。"""
    preset = _emotion_preset(emotion) or {"speed": 1.0, "pitch": 1.0}
    stem = stem or "speech_%s" % uuid.uuid4().hex[:8]
    result = tts_engine.synthesize(text, voice=voice, speed=preset["speed"],
                                   out_dir=out_dir or WORK_DIR, stem=stem)
    shifted = _pitch_shift(result["audio_path"], preset["pitch"], stem)
    if shifted != result["audio_path"]:
        result = dict(result, audio_path=shifted, emotion=norm_emotion(emotion))
    return result


# ---------------------------------------------------------------- 自我介绍视频
# 每位员工一段「画面 + 语音」自我介绍：档案字段经 Hermes 润色成 2-3 句 → TTS → Wav2Lip
# 口型视频 → 720×960 MP4，落 assets/intro/<id>.mp4；首次请求生成并缓存（+faststart 可边下边播）。
INTRO_LLM_TIMEOUT = float(_cfg("INTRO_LLM_TIMEOUT", "180"))
INTRO_MAX_CHARS = int(_cfg("INTRO_MAX_CHARS", "140"))
INTRO_LOCK_WAIT = float(_cfg("INTRO_LOCK_WAIT", "600"))
INTRO_PIPELINE = "fullbody-hold-v1"
# 自我介绍视频：有全身照时整条只用全身照（慢推），配音从头开始；不再拼接半身口型段
INTRO_OPEN_SEC = max(0.0, float(_cfg("INTRO_OPEN_SEC", "2.4")))
LIPSYNC_ENABLE = _cfg("LIPSYNC", "1") not in ("0", "false", "False", "no")
LIPSYNC_RUNNER = _cfg("LIPSYNC_RUNNER", os.path.join("/opt/sefs/mcp-asr/lipsync", "run_lipsync.py"))
LIPSYNC_PYTHON = _cfg("LIPSYNC_PYTHON", "/opt/sefs/CosyVoice/venv/bin/python")
LIPSYNC_WEIGHTS = _cfg("LIPSYNC_WEIGHTS",
                       os.path.join("/opt/sefs/mcp-asr/lipsync", "weights", "wav2lip_gan.pth"))
LIPSYNC_THREADS = max(1, int(_cfg("LIPSYNC_THREADS", "32")))
LIPSYNC_TIMEOUT = float(_cfg("LIPSYNC_TIMEOUT", "1200"))

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
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _intro_lock(aid):
    with _INTRO_LOCKS_LOCK:
        lock = _INTRO_LOCKS.get(aid)
        if lock is None:
            lock = _INTRO_LOCKS[aid] = threading.Lock()
        return lock


def intro_meta_of(aid):
    path = os.path.join(INTRO_DIR, "%s.json" % aid) if INTRO_DIR else ""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _read_intro_text(aid):
    path = os.path.join(INTRO_DIR, "%s.txt" % aid) if INTRO_DIR else ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, TypeError):
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


def run_hermes(prompt, profile=None, timeout=240):
    """调 Hermes CLI 生成文本（文案 / 汇报）。无输出时抛错，由调用方决定回退。"""
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    if profile:
        env["HERMES_PROFILE"] = profile
    else:
        env.pop("HERMES_PROFILE", None)
    proc = subprocess.run([HERMES_CLI, "-z", prompt], capture_output=True, text=True,
                          timeout=timeout, env=env)
    out = _ANSI_RE.sub("", proc.stdout or "").strip()
    if not out:
        err = _ANSI_RE.sub("", proc.stderr or "").strip()[-500:]
        raise RuntimeError("Hermes 无输出（exit=%s）：%s" % (proc.returncode, err))
    return out


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
    except Exception as e:                            # noqa: BLE001 - 文案失败不阻断视频生成
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


def _intro_fullbody(aid):
    """开头全身照：assets/fullbody/<id>.png（已换脸，身份与肖像一致），没有则空。"""
    prof = load_profiles().get(aid) or {}
    asset_aid = prof.get("assetId") or aid
    for ext in ("png", "jpg"):
        path = _asset_path("fullbody", "%s.%s" % (asset_aid, ext))
        if path:
            return path
    return ""


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
                          timeout=LIPSYNC_TIMEOUT, env=env)
    if proc.returncode != 0 or not os.path.exists(out_path):
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise RuntimeError("口型合成失败：%s" % (tail or "无输出"))
    summary = (proc.stdout or "").strip().splitlines()
    log("口型视频已合成：%s（%s）" % (aid, summary[-1] if summary else "ok"))
    return out_path


def _intro_fullbody_filter(total):
    """整条视频只用全身照：按总时长均摊的一次极慢推近（无拼接）。"""
    step = 0.06 / max(1.0, total * 25)
    return ("scale=1080:1440:force_original_aspect_ratio=increase,crop=1080:1440,"
            "zoompan=z='min(zoom+%.6f,1.06)':d=1:x='iw/2-(iw/zoom/2)'"
            ":y='ih/2-(ih/zoom/2)':s=720x960:fps=25,setsar=1" % step)


def _intro_filter(kind):
    scale = ("scale=720:960:force_original_aspect_ratio=decrease,"
             "pad=720:960:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if kind == "lipsync":
        return ("scale=720:960:force_original_aspect_ratio=increase,"
                "crop=720:960,setsar=1")
    if kind == "photo":
        # 静止照片做极慢推近，避免「定住不动」的幻灯片感
        return ("scale=1080:1440:force_original_aspect_ratio=increase,crop=1080:1440,"
                "zoompan=z='min(zoom+0.0006,1.10)':d=1:x='iw/2-(iw/zoom/2)'"
                ":y='ih/2-(ih/zoom/2)':s=720x960:fps=25,setsar=1")
    return scale


def intro_build(aid, force=False, refresh_text=True, text=None, voice=None):
    """生成（或复用）该员工的自我介绍视频，返回档案里要用的字段。

    画面优先用「照片 + 配音」现场合成口型视频（Wav2Lip）；失败时回退 talk/idle 视频
    或照片运镜，保证任何情况下都有可播的档案视频。text 由调用方给出时直接配音。
    """
    if not INTRO_DIR:
        raise RuntimeError("没有可写的素材目录（ASSET_DIRS 为空）")
    if not FFMPEG_BIN:
        raise RuntimeError("本机没有可用的 ffmpeg，无法生成自我介绍视频")
    prof = load_profiles().get(aid) or {"id": aid}
    _ensure_dir(INTRO_DIR)
    out = os.path.join(INTRO_DIR, "%s.mp4" % aid)
    url = _asset_url("intro", "%s.mp4" % aid)

    def ready_item(cached, script=""):
        meta = intro_meta_of(aid)
        return {"id": aid, "name": prof.get("name") or aid, "ready": True, "cached": cached,
                "video": url, "video_full": _asset_url_full("intro", "%s.mp4" % aid),
                "text": script or _read_intro_text(aid),
                "duration": meta.get("duration") or _probe_duration(out),
                "size": os.path.getsize(out), "source": meta.get("source"),
                "pipeline": meta.get("pipeline"), "generated_at": meta.get("generated_at")}

    if os.path.exists(out) and not force:
        return ready_item(True)

    lock = _intro_lock(aid)
    if not lock.acquire(timeout=INTRO_LOCK_WAIT):
        raise RuntimeError("员工 %s 的自我介绍视频正在生成，请稍后查询" % aid)
    try:
        if os.path.exists(out) and not force:
            return ready_item(True)
        started = time.time()
        script = text or intro_script(aid, prof, force=(force and refresh_text))
        if text:                                      # 外部指定文案也要落盘，档案里能看到
            with open(os.path.join(INTRO_DIR, "%s.txt" % aid), "w", encoding="utf-8") as f:
                f.write(script + "\n")
        emotion = emotion_of(aid)
        tts = speak(script, voice=voice or prof.get("voice"), emotion=emotion,
                    stem="intro_%s_%d" % (aid, int(time.time())))
        wav_path = tts["audio_path"]
        open_img = _intro_fullbody(aid) if INTRO_OPEN_SEC > 0 else ""
        kind, source = _intro_video_source(aid)
        source_label = "%s:%s" % (kind, os.path.basename(source)) if source else ""
        lipsync_tmp = ""
        # 有全身照时整条片子只用它，不再做口型段（两段拼接会硬切换人/换背景）
        photo = "" if open_img else _intro_photo(aid)
        if photo and lipsync_ready():
            lipsync_tmp = os.path.join(INTRO_DIR, ".%s.lipsync.mp4" % aid)
            try:
                _intro_lipsync(aid, photo, wav_path, lipsync_tmp)
                kind, source = "lipsync", lipsync_tmp
                source_label = "lipsync:%s" % os.path.basename(photo)
            except Exception as e:                    # noqa: BLE001 - 口型失败回退静态画面
                log("口型视频不可用（%s），回退原有画面：%s" % (aid, e))
                if lipsync_tmp and os.path.exists(lipsync_tmp):
                    os.unlink(lipsync_tmp)
                lipsync_tmp = ""
        if not source and not open_img:
            raise RuntimeError("员工 %s 既没有全身照也没有 talk 视频/照片，无法生成画面" % aid)
        duration = _probe_duration(wav_path)
        tmp_out = os.path.join(INTRO_DIR, ".%s.intro.mp4" % aid)
        if open_img:
            # 整条视频只放全身照：配音从头开始，一次运镜到底，不做任何拼接
            total = max(1.0, duration)
            kind, source = "fullbody", open_img
            source_label = "fullbody:%s" % os.path.basename(open_img)
            cmd = [FFMPEG_BIN, "-y", "-loglevel", "error",
                   "-loop", "1", "-framerate", "25", "-i", open_img, "-i", wav_path,
                   "-map", "0:v:0", "-map", "1:a:0",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                   "-pix_fmt", "yuv420p", "-r", "25",
                   "-vf", _intro_fullbody_filter(total),
                   "-c:a", "aac", "-b:a", "96k", "-ar", "24000", "-ac", "1",
                   "-t", "%.3f" % total,
                   "-movflags", "+faststart", tmp_out]
        else:
            if kind in ("video", "lipsync"):
                inputs = [FFMPEG_BIN, "-y", "-loglevel", "error",
                          "-stream_loop", "-1", "-i", source, "-i", wav_path]
            else:
                inputs = [FFMPEG_BIN, "-y", "-loglevel", "error",
                          "-loop", "1", "-framerate", "25", "-i", source, "-i", wav_path]
            cmd = inputs + [
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-r", "25", "-vf", _intro_filter(kind),
                "-c:a", "aac", "-b:a", "96k", "-ar", "24000", "-ac", "1",
                "-t", "%.3f" % max(1.0, duration),
                "-movflags", "+faststart", tmp_out]
        try:
            subprocess.run(cmd, check=True, timeout=300, env=FF_ENV, capture_output=True)
            if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) < 4096:
                raise RuntimeError("ffmpeg 输出为空")
            os.replace(tmp_out, out)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode("utf-8", "replace")[-400:]
            raise RuntimeError("自我介绍视频合成失败：%s" % err)
        finally:
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)
            if lipsync_tmp and os.path.exists(lipsync_tmp):
                os.unlink(lipsync_tmp)
        duration = _probe_duration(out) or duration
        meta_out = {"id": aid, "text": script, "duration": duration,
                    "source": source_label or "%s:%s" % (kind, os.path.basename(source)),
                    "pipeline": INTRO_PIPELINE, "emotion": emotion,
                    "generated_at": time.time(),
                    "generate_ms": int((time.time() - started) * 1000),
                    "size": os.path.getsize(out)}
        with open(os.path.join(INTRO_DIR, "%s.json" % aid), "w", encoding="utf-8") as f:
            json.dump(meta_out, f, ensure_ascii=False)
        log("自我介绍视频已生成：%s（%.1fs，%d 字节）" % (aid, duration, meta_out["size"]))
        return {"id": aid, "name": prof.get("name") or aid, "ready": True, "cached": False,
                "video": url, "video_full": _asset_url_full("intro", "%s.mp4" % aid),
                "text": script, "duration": duration, "size": meta_out["size"],
                "source": meta_out["source"], "pipeline": INTRO_PIPELINE,
                "generate_ms": meta_out["generate_ms"]}
    finally:
        lock.release()


def intro_stale(aid):
    """成片缺失、流水线升级、或照片/文案比成片新，都需要重建。"""
    if not INTRO_DIR:
        return False
    out = os.path.join(INTRO_DIR, "%s.mp4" % aid)
    if not os.path.exists(out):
        return True
    if (intro_meta_of(aid).get("pipeline") or "") != INTRO_PIPELINE:
        return True
    out_m = os.path.getmtime(out)
    photo = _intro_photo(aid)
    script = os.path.join(INTRO_DIR, "%s.txt" % aid)
    for p in (photo, _intro_fullbody(aid), script):
        if p and os.path.exists(p) and os.path.getmtime(p) > out_m:
            return True
    return False


def resolve_ids(employee_id=None, all_employees=False):
    """解析要处理哪些员工：显式 id / 指定 all；两者都没有时报错，避免误触发全量生成。"""
    known = list(load_profiles())
    if employee_id:
        if known and employee_id not in known:
            raise ValueError("员工不存在：%s（可用：%s）" % (employee_id, ", ".join(known)))
        return [employee_id]
    if all_employees:
        if not known:
            raise ValueError("员工档案为空：%s" % PROFILES_FILE)
        return known
    raise ValueError("需要 employee_id，或显式传 all=true 处理全部员工")


def intro_job(ids, force=False, refresh_text=True, text=None, voice=None):
    """批量生成（任务队列里跑）：单人失败不影响其他人。"""
    items, failed = [], []
    for aid in ids:
        try:
            items.append(intro_build(aid, force=force, refresh_text=refresh_text,
                                     text=text, voice=voice))
        except Exception as e:                        # noqa: BLE001
            log("自我介绍生成失败（%s）：%s" % (aid, e))
            failed.append({"id": aid, "error": str(e)})
    return {"count": len(items), "dir": INTRO_DIR, "url_base": ASSET_URL_BASE or None,
            "items": items, "failed": failed}


# ---------------------------------------------------------------- 语音汇报文本
_MD_FENCE_RE = re.compile(r"```.*?```", re.S)
_SENT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
VOICE_SUMMARY_CHARS = int(_cfg("VOICE_SUMMARY_CHARS", "90"))


def _clean_for_speech(text):
    """去掉 Markdown 噪音，得到适合朗读的纯文本。"""
    s = _MD_FENCE_RE.sub(" ", text or "")
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = s.replace("**", "").replace("__", "").replace("`", "")
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"^\s{0,3}(?:[-*·]|\d+[.)、])\s*", "", s, flags=re.M)
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
        if picked and picked[-1] not in "，,。！？!?；;：:":
            picked += "，"
        picked += seg
        if len(picked) >= max(12, int(limit * 0.7)):
            break
    picked = picked or clean
    if len(picked) > limit:
        picked = picked[:limit].rstrip("，,、；; ") + "……"
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


# ---------------------------------------------------------------- 老板上线汇报
SECRETARY_ID = _cfg("SECRETARY", "secretary")
BOSS_BRIEFING_PROMPT = _cfg(
    "BOSS_BRIEFING_PROMPT",
    "老板刚上线。主动汇报近期发生的事：只读查阅 /opt/sefs/obsidian 下的 动态/运行时快照.md、"
    "动态/事件流.md、OPC/交付物/*/06-log.md 和收件箱未读邮件，梳理各员工任务状态与最近更新时间。"
    "按「一句话结论 → 进展 → 风险与异常 → 需要老板拍板」输出，第三、四部分每项给依据，"
    "待拍板事项给选项、你的建议和不决策的后果，必须老板拍板的标【急】并置顶。"
    "控制在老板 30 秒能读完的长度。没有记录就写没有记录，禁止编造进度、数据或来源。")
BOSS_BRIEFING_TIMEOUT = float(_cfg("BOSS_BRIEFING_TIMEOUT", "300"))


def boss_briefing(voice=None, emotion="calm"):
    """让 OPC-001 小秘书汇报近期事项：Hermes 生成详细文字 + TTS 成语音。"""
    aid = SECRETARY_ID if _profile_ready(SECRETARY_ID) else "ceo"
    prof = load_profiles().get(aid) or {}
    raw = run_hermes(BOSS_BRIEFING_PROMPT, profile=aid, timeout=BOSS_BRIEFING_TIMEOUT)
    detail, speech = normalize_answer(raw)
    tts = speak(speech, voice=voice or prof.get("voice"), emotion=emotion,
                stem="briefing_%s" % uuid.uuid4().hex[:8])
    return {"scene": "boss_online", "id": aid, "name": prof.get("name") or aid,
            "detail": detail, "speech": speech,
            "audio_url": "/files/" + os.path.basename(tts["audio_path"]),
            "audio_path": tts["audio_path"], "duration": tts["duration"],
            "segments": tts.get("segments") or [],
            "avatar": avatar_profile(aid, prof.get("name"), prof.get("role"))}


# ---------------------------------------------------------------- 异步任务队列
class JobRegistry:
    """N 个 worker 消费队列；任务状态留在内存，按 TTL 清理（重启即丢，重跑即可）。"""

    def __init__(self, workers=2, ttl=7200, max_jobs=200):
        self.workers = max(1, int(workers))
        self.ttl = ttl
        self.max_jobs = max_jobs
        self._q = queue.Queue()
        self._jobs = {}
        self._lock = threading.Lock()
        self._events = {}
        for i in range(self.workers):
            threading.Thread(target=self._worker, args=(i,), daemon=True).start()

    def _prune_locked(self):
        cutoff = time.time() - self.ttl
        stale = [jid for jid, j in self._jobs.items()
                 if j["status"] in ("done", "failed") and j["finished_at"] < cutoff]
        for jid in stale:
            self._jobs.pop(jid, None)
            self._events.pop(jid, None)
        if len(self._jobs) > self.max_jobs:
            done = sorted((j for j in self._jobs.values() if j["status"] in ("done", "failed")),
                          key=lambda j: j["finished_at"])
            for j in done[:len(self._jobs) - self.max_jobs]:
                self._jobs.pop(j["job_id"], None)
                self._events.pop(j["job_id"], None)

    def submit(self, kind, fn, meta=None):
        job_id = "job_%s" % uuid.uuid4().hex[:12]
        job = {"job_id": job_id, "kind": kind, "status": "queued", "meta": meta or {},
               "created_at": time.time(), "started_at": None, "finished_at": None,
               "result": None, "error": None, "queue_ahead": 0}
        ev = threading.Event()
        with self._lock:
            self._prune_locked()
            job["queue_ahead"] = sum(1 for j in self._jobs.values()
                                     if j["status"] in ("queued", "running"))
            self._jobs[job_id] = job
            self._events[job_id] = ev
        self._q.put((job_id, fn))
        log("任务入队 %s kind=%s 前面 %d 个" % (job_id, kind, job["queue_ahead"]))
        return job

    def _worker(self, idx):
        while True:
            job_id, fn = self._q.get()
            with self._lock:
                job = self._jobs.get(job_id)
            if not job:
                continue
            job.update(status="running", started_at=time.time())
            try:
                job["result"] = fn()
                job["status"] = "done"
            except Exception as e:                    # noqa: BLE001 - 任务失败要回给调用方
                job["error"] = str(e)
                job["status"] = "failed"
                log("任务失败 %s：%s" % (job_id, traceback.format_exc()))
            finally:
                job["finished_at"] = time.time()
                with self._lock:
                    ev = self._events.get(job_id)
                if ev:
                    ev.set()
                log("任务结束 %s -> %s" % (job_id, job["status"]))

    def get(self, job_id, wait_sec=0.0):
        with self._lock:
            job = self._jobs.get(job_id)
            ev = self._events.get(job_id)
        if not job:
            return None
        if wait_sec > 0 and job["status"] in ("queued", "running") and ev:
            ev.wait(min(float(wait_sec), 300))
        return self.view(job)

    def view(self, job):
        out = {k: v for k, v in job.items() if k != "result"}
        out["elapsed_ms"] = int(((job["finished_at"] or time.time()) - job["created_at"]) * 1000)
        out["result"] = job.get("result")
        return out

    def list(self, limit=20):
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
        return [self.view(j) for j in jobs[:limit]]

    def status(self):
        with self._lock:
            queued = sum(1 for j in self._jobs.values() if j["status"] == "queued")
            running = sum(1 for j in self._jobs.values() if j["status"] == "running")
            total = len(self._jobs)
        return {"workers": self.workers, "queued": queued, "running": running, "total": total}


JOBS = JobRegistry(workers=int(_cfg("VIDEO_WORKERS", "2")),
                   ttl=int(_cfg("JOB_TTL", "7200")),
                   max_jobs=int(_cfg("JOB_MAX", "200")))


def capabilities():
    """健康检查用：这条链路在当前机器上能不能跑。"""
    return {"ffmpeg": FFMPEG_BIN or None, "ffprobe": FFPROBE_BIN or None,
            "asset_dirs": ASSET_DIRS, "intro_dir": INTRO_DIR or None,
            "profiles_file": PROFILES_FILE,
            "profiles": len(load_profiles()) if os.path.exists(PROFILES_FILE) else 0,
            "hermes_cli": HERMES_CLI if os.path.exists(HERMES_CLI) else None,
            "lipsync": lipsync_ready(), "jobs": JOBS.status()}
