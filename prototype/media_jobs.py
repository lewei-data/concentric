#!/usr/bin/env python3
"""分镜级多渠道媒体生成作业（图片 / 视频 / 音频）：并行、进度、启停、重试。

设计约定（对应制片工厂「素材制作 / 剪辑合成 / 终版合成」的人工多次启动）：
  - 一个作业 = (trace, 内容单, 分镜号, 类型, 渠道)；同一提示词可同时投多个渠道，
    每个渠道一行作业，互不影响，可分别看进度、停、重来。
  - 渠道注册表 CHANNELS 是唯一事实源：类型（image/video/audio）、端点、参数规格、
    并发额度、时长上限都在这里；前端直接读注册表渲染表单，不在前端写死。
  - 视频时长上限：全局 OPC_SHOT_MAX_SEC（默认 6s）与渠道 maxSec 取小；WAN2.1 受
    num_frames(4k+1, ≤81)/fps 反推（81 帧 @16fps = 5.06s）。超限一律夹到上限并把
    原因写进作业 note，不静默改写。
  - 启停：cancel 置标记并尽力中断（ComfyUI 走 /interrupt；media-gen / TTS 无取消
    接口，只停本地轮询，远端作业自然跑完，结果弃用）。
  - 失败重来：retry 用原参数新建一行作业（run_no+1），原作业保留为历史。
  - 存储：独立 SQLite（默认 <工作区>/media_jobs.db，WAL），不与过程库 opc.db 抢写锁。
  - 只做「生成 + 取回」，不写业务状态；状态落库、事件由调用方（kernel）负责。
"""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TZ = timezone(timedelta(hours=8))
# 个人空间根：落在 <USERS_DIR>/<手机号>/ 下的成品一律走 /private 私有前缀
USERS_DIR = os.environ.get("OPC_USERS_DIR", "/mnt/sefs/conring/users")

MEDIAGEN = os.environ.get("OPC_MEDIAGEN_URL", "http://192.168.21.206:9103").rstrip("/")
MEDIAENGINE = os.environ.get("OPC_MEDIAENGINE_URL", "http://192.168.21.206:9200").rstrip("/")
SHOT_MAX_SEC = float(os.environ.get("OPC_SHOT_MAX_SEC", "6"))
MAX_WORKERS = int(os.environ.get("OPC_MEDIA_WORKERS", "6"))
PROBE_TTL = 30.0

# 音色事实源：media-engine（192.168.21.206:9200）与 hermes 的 tts 共用这份表，
# 且 Engine.voice() 每次合成前重读文件 —— 往这里追加一条就等于「注册新音色」，
# 不必重启 media-engine（重启会打断网页配音与精灵语音）。
TTS_VOICES_JSON = os.environ.get("OPC_TTS_VOICES_JSON", "/opt/sefs/mcp-tts/voices.json")
TTS_REF_DIR = os.environ.get("OPC_TTS_REF_DIR", "/opt/sefs/mcp-tts/voices/ref")
CLONE_PREFIX = "clone-"
CLONE_MAX_SEC = 20.0
CLONE_MIN_SEC = 1.5

# 云服务：阿里云百炼 Token Plan 语音合成（qwen-audio-3.0-tts-plus）。
# 与 llm_registry.py 里的百炼 LLM 共用同一把 sk-sp-* Token Plan Key，不另存凭据；
# 需要单独一把 Key 时用 OPC_BAILIAN_TTS_API_KEY 覆盖。
BAILIAN_TTS_BASE = os.environ.get(
    "OPC_BAILIAN_TTS_BASE_URL", "https://token-plan.cn-beijing.maas.aliyuncs.com").rstrip("/")
BAILIAN_TTS_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"
BAILIAN_TTS_MODEL = os.environ.get("OPC_BAILIAN_TTS_MODEL", "qwen-audio-3.0-tts-plus")
BAILIAN_TTS_KEY = (os.environ.get("OPC_BAILIAN_TTS_API_KEY")
                   or os.environ.get("OPC_LLM_QWEN37PLUS_API_KEY")
                   or os.environ.get("OPC_LLM_QWEN38FLASH_API_KEY") or "")
# 云服务：阿里云百炼 Token Plan 图像生成（qwen-image-3.0-pro，文生图 + 图生图）
BAILIAN_IMAGE_BASE = os.environ.get(
    "OPC_BAILIAN_IMAGE_BASE_URL", "https://token-plan.cn-beijing.maas.aliyuncs.com").rstrip("/")
BAILIAN_IMAGE_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
BAILIAN_IMAGE_MODEL = os.environ.get("OPC_BAILIAN_IMAGE_MODEL", "qwen-image-3.0-pro")

KINDS = ("image", "video", "audio")
LANE_LIMITS = {"comfyui": 1, "comfyui_video": 1, "sd15": 1, "wan21": 1,
               "minimax_h3": 1, "qwen": 2, "cosyvoice": 2, "bailian": 2,
               "comfyui_157": 1, "bailian_image": 2}


# ---------------------------------------------------------------- 基础工具
def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def _log(msg):
    print(f"[media-jobs] {msg}", flush=True)


def _p(key, label, typ, default, **kw):
    spec = {"key": key, "label": label, "type": typ, "default": default}
    spec.update(kw)
    return spec


def _http(url, timeout=15, data=None, method=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _http_json(url, timeout=15, data=None, method=None, headers=None):
    body = None
    headers = dict(headers or {})
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    raw = _http(url, timeout=timeout, data=body, method=method, headers=headers)
    return json.loads(raw.decode("utf-8")) if raw else {}


def _bailian_api_key():
    """百炼 Token Plan Key：显式环境变量优先，否则复用百炼 LLM 通道那把 sk-sp-*。"""
    if BAILIAN_TTS_KEY:
        return BAILIAN_TTS_KEY
    try:
        from llm_registry import LLM_DEFAULTS
        for m in LLM_DEFAULTS:
            if str(m.get("id") or "").endswith("-bailian") and m.get("apiKey"):
                return m["apiKey"]
    except Exception:  # noqa: BLE001 - 取不到就走「未配置」，不吞掉真实合成时的报错
        pass
    return ""


# ---------------------------------------------------------------- 渠道注册表
# 分组：ComfyUI 跑在 192.168.21.157 那台远程工作站上，media-gen / media-engine
# 跑在 192.168.21.206，两者都不是本机进程，前端必须分组展示，别再混成「本地出图」。
GROUPS = [
    {"key": "comfy", "label": "ComfyUI 工作站（远程 192.168.21.157）",
     "desc": "与 hermes 的 comfyui 技能同一套 ComfyUI（8188 出图 / 8189 视频），"
             "纯 CPU 推理，单队列串行，排队看得见"},
    {"key": "lan", "label": "局域网媒体服务（192.168.21.206）",
     "desc": "media-gen 出图出片 + media-engine 语音，服务端自己排队，可并发"},
    {"key": "bailian", "label": "阿里云百炼（云服务）",
     "desc": "DashScope Token Plan 云端语音合成 / 图像生成，按量计费、不占本机算力；"
             "Key 复用百炼 LLM 通道的 sk-sp-*（可用 OPC_BAILIAN_TTS_API_KEY 单独覆盖）"},
]

CHANNELS = {
    "comfyui": {
        "kind": "image", "group": "comfy",
        "label": "ComfyUI · SD2.1 文生图",
        "endpoint": "comfy:8188",
        "notes": "ComfyUI 原生 KSampler 工作流（SD2.1 768），API 格式与 hermes comfyui 技能一致；"
                 "单队列，CPU 单张约 1–3 分钟",
        "modes": ["t2i", "i2i"],
        "spec": {
            "title": "SD2.1 出图规格红线",
            "limits": [
                "底模 v2-1_768-ema-pruned 按 768 训练：长边 ≤1024，超了容易重复人脸/肢体",
                "宽高按 64 对齐（8 的倍数由系统强制）；推荐 768×1024 竖屏 / 1024×768 横屏 / 768×768 方图",
                "steps 20–30：<12 细节没长出来，>40 只变慢不提质",
                "CFG 7–8：<5 跑题，>12 色彩过饱和、画面发硬",
                "图生图输出尺寸跟随输入图；本页面宽高只对文生图生效",
            ],
            "presets": [
                {"name": "竖屏封面 768×1024", "values": {"width": 768, "height": 1024, "steps": 25, "cfg": 7.5}},
                {"name": "方图 768×768", "values": {"width": 768, "height": 768, "steps": 20, "cfg": 7.5}},
                {"name": "横屏 1024×768", "values": {"width": 1024, "height": 768, "steps": 25, "cfg": 7.5}},
            ],
        },
        "params": [
            _p("denoise", "图生图重绘幅度", "float", 0.55, min=0.1, max=1.0, step=0.05,
               hint="仅图生图生效：0.4–0.7 保留原图构图，接近 1.0 等于重新画；过高会把参考图改得面目全非"),
            _p("width", "宽", "int", 768, min=256, max=1024, step=64, multiple=8,
               hint="SD2.1 按 768 训练：长边别超 1024，否则容易出现重复人脸/肢体。"
                    "竖屏封面用 768×1024，方图 768×768，横屏 1024×768"),
            _p("height", "高", "int", 1024, min=256, max=1024, step=64, multiple=8,
               hint="与宽一起决定构图比例：9:16 → 768×1024，16:9 → 1024×768"),
            _p("steps", "采样步数", "int", 20, min=4, max=50,
               hint="建议 20–30。低于 12 细节没长出来，高于 40 对 SD2.1 基本无提升，只变慢"),
            _p("cfg", "提示词权重 CFG", "float", 7.5, min=1, max=20, step=0.5,
               hint="建议 7–8。太低（<5）跑题，太高（>12）画面发硬发脆、色彩过饱和"),
            _p("sampler", "采样器", "select", "euler",
               options=["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde",
                        "heun", "dpm_2", "ddim"],
               hint="拿不准用 euler；要更细的皮肤/布料纹理用 dpmpp_2m；"
                    "euler_ancestral 更有创意但同种子不可复现"),
            _p("scheduler", "噪声调度", "select", "normal",
               options=["normal", "karras", "exponential", "sgm_uniform", "simple"],
               hint="normal 最稳；karras 出图更锐、对比更高；改调度器要同步调回 steps≈25"),
            _p("seed", "随机种子", "int", -1, min=-1, max=2147483647,
               hint="-1 表示每次随机；填固定数字可复现同一张图（想微调提示词时保留种子）"),
            _p("negative", "反向提示词", "text", "低质量, 模糊, 水印, 变形",
               hint="写要避免的东西：低质量、多余手指、文字/水印、畸形肢体；别重复正向词"),
        ],
    },
    "comfyui_video": {
        "kind": "video", "group": "comfy",
        "label": "ComfyUI · Wan2.2 TI2V 5B 视频",
        "endpoint": "comfy:8189",
        "notes": "Wan2.2 TI2V-5B（文生视频 / 首帧图生视频），ComfyUI 原生工作流，"
                 "输出 webm 后转 mp4；CPU 推理，一段 3s 480p 约 10–30 分钟",
        "max_sec": 5.0,
        "modes": ["t2v", "i2v"],
        "params": [
            _p("width", "宽", "int", 704, min=256, max=1280, step=16, multiple=16,
               hint="官方原生 1280×704。CPU 上推荐 704×480：再高耗时翻倍，画质提升有限"),
            _p("height", "高", "int", 480, min=256, max=1280, step=16, multiple=16,
               hint="竖屏短剧用 480×704，横屏 704×480；宽高都会自动对齐到 16 的倍数"),
            _p("steps", "采样步数", "int", 20, min=4, max=50,
               hint="建议 20–30（没配加速 LoRA 不要低于 15，画面会抖成糊块）"),
            _p("cfg", "提示词权重 CFG", "float", 5.0, min=1, max=20, step=0.5,
               hint="Wan 系列建议 5 左右；超过 8 运动幅度会失控、物体开始变形"),
            _p("fps", "帧率", "int", 16, min=8, max=24,
               hint="16 帧最省算力且有电影感；24 更顺滑，但耗时和体积同比上升"),
            _p("seed", "随机种子", "int", -1, min=-1, max=2147483647,
               hint="-1 每次随机；固定种子便于对比不同提示词对同一构图的影响"),
            _p("negative", "反向提示词", "text", "低质量, 模糊, 水印, 变形, 静止画面",
               hint="视频要额外压制「静止、闪烁、画面撕裂、多余肢体」，这些是 Wan 的常见问题"),
        ],
    },
    "sd15": {
        "kind": "image", "group": "lan",
        "label": "media-gen · SD1.5 写实",
        "endpoint": "media-gen:9103",
        "notes": "media-gen SD15（Realistic Vision V5.1），CPU 串行队列，人像写实稳；服务端只做文生图，不支持图生图",
        "spec": {
            "title": "SD1.5 出图规格红线",
            "limits": [
                "Realistic Vision V5.1 在 512–768 最稳；长边 >832 容易出两个头/双脸",
                "宽高必须是 8 的倍数；推荐 640×832 竖屏 / 832×512 横屏 / 640×640 方图",
                "steps 20–30；>35 收益很小，只增加排队时间",
                "CFG 6–7 写实人像最自然；>9 皮肤塑料感、明暗过硬",
                "服务端是文生图管道，上传参考图不会生效，请改用 ComfyUI 或百炼渠道做图生图",
            ],
            "presets": [
                {"name": "竖屏 640×832", "values": {"width": 640, "height": 832, "steps": 25, "cfg": 6.0}},
                {"name": "方图 640×640", "values": {"width": 640, "height": 640, "steps": 20, "cfg": 6.0}},
                {"name": "横屏 832×512", "values": {"width": 832, "height": 512, "steps": 25, "cfg": 6.0}},
            ],
        },
        "modes": ["t2i"],
        "params": [
            _p("width", "宽", "int", 640, min=256, max=1024, step=8, multiple=8,
               hint="SD1.5 在 512–768 最稳；超过 832 易出现两个人头/双脸，长边建议 ≤768"),
            _p("height", "高", "int", 832, min=256, max=1024, step=8, multiple=8,
               hint="竖屏 640×832 / 512×768，横屏 832×512"),
            _p("steps", "采样步数", "int", 20, min=4, max=50,
               hint="建议 20–30；SD1.5 超过 35 步收益很小"),
            _p("cfg", "提示词权重 CFG", "float", 6.0, min=1, max=20, step=0.5,
               hint="写实人像 6–7 最自然；高于 9 皮肤会塑料感、明暗对比过硬"),
            _p("seed", "随机种子", "int", -1, min=-1, max=2147483647,
               hint="-1 随机；固定种子可复现，便于同一人物微调"),
            _p("negative_prompt", "反向提示词", "text", "低质量, 模糊, 水印",
               hint="写实向可加：卡通, 插画, 3D渲染, 变形手, 多余手指"),
        ],
    },
    "comfyui_157": {
        "kind": "image", "group": "comfy",
        "label": "ComfyUI · 192.168.21.157 · Qwen-Image",
        "endpoint": "192.168.21.157:8188",
        "notes": "157 工作站 Qwen-Image-2.1-Turbo（UNET + qwen3vl-8b CLIP + 专用 VAE），"
                 "8 步出图；支持文生图与图生图（参考图走 VAEEncode）",
        "modes": ["t2i", "i2i"],
        "spec": {
            "title": "Qwen-Image-2.1-Turbo 规格红线",
            "limits": [
                "turbo 底模 8 步就够：steps >20 耗时翻倍但几乎不提质；CFG 保持 1.0 左右，>3 结构会崩",
                "文生图宽高按 64 对齐，范围 512–1536；推荐 1024×1024 方图 / 768×1024 竖屏",
                "图生图输出尺寸跟随输入图，页面上的宽/高只对文生图生效；参考图最短边建议 ≥384px",
                "图生图 denoise 0.4–0.7 保留原构图；接近 1.0 等于重画，容易丢掉参考图特征",
                "与 SD2.1 共用 157 的 8188 单队列，CPU 单张约 1–3 分钟，排队数见渠道卡片",
            ],
            "presets": [
                {"name": "方图 1024×1024 · 8步", "values": {"width": 1024, "height": 1024, "steps": 8, "cfg": 1.0}},
                {"name": "竖屏 768×1024 · 8步", "values": {"width": 768, "height": 1024, "steps": 8, "cfg": 1.0}},
                {"name": "横屏 1024×768 · 8步", "values": {"width": 1024, "height": 768, "steps": 8, "cfg": 1.0}},
            ],
        },
        "params": [
            _p("width", "宽", "int", 1024, min=512, max=1536, step=64, multiple=64,
               hint="文生图用；turbo 原生 1024 档，别超过 1536。图生图时该值不生效"),
            _p("height", "高", "int", 1024, min=512, max=1536, step=64, multiple=64,
               hint="文生图用；竖屏 768×1024，横屏 1024×768。图生图时该值不生效"),
            _p("steps", "采样步数", "int", 8, min=4, max=20,
               hint="turbo 模型 8 步即可，20 步耗时翻倍、画质几乎不变"),
            _p("cfg", "提示词权重 CFG", "float", 1.0, min=0.5, max=5.0, step=0.1,
               hint="qwen-image turbo 用 1.0；调到 3 以上会出现过饱和和结构崩坏"),
            _p("denoise", "图生图重绘幅度", "float", 0.55, min=0.1, max=1.0, step=0.05,
               hint="仅图生图生效：0.4–0.7 保留原图构图与主体；1.0 等于重新画"),
            _p("seed", "随机种子", "int", -1, min=-1, max=2147483647,
               hint="-1 每次随机；固定值便于同图对比提示词"),
            _p("negative", "反向提示词", "text", "blurry, low quality, watermark, extra limbs",
               hint="写要避免的英文标签；qwen-image 对中文反向词理解弱于英文"),
        ],
    },
    "bailian_image": {
        "kind": "image", "group": "bailian",
        "label": "阿里云百炼 · Qwen-Image-3.0-Pro",
        "endpoint": "token-plan.cn-beijing.maas.aliyuncs.com",
        "notes": "云服务 qwen-image-3.0-pro：multimodal-generation/generation 同步返回 PNG URL 后立即下载；"
                 "支持文生图与图生图（参考图内联 base64），按量计费",
        "modes": ["t2i", "i2i"],
        "spec": {
            "title": "qwen-image-3.0-pro 规格红线",
            "limits": [
                "输出 PNG；面积必须在 512×512 ~ 2048×2048 之间，长宽比 1:8–8:1；size 用「宽*高」星号格式",
                "推荐 1024*1024 方图 / 768*1024 竖屏 / 1024*768 横屏；2048*2048 耗时与费用都高",
                "图生图参考图 1–3 张（当前渠道传 1 张）：JPG/PNG/WEBP 等，单图 ≤10MB，1–8000px，长短边 ≤10:1",
                "图生图输出尺寸由 size 决定，不会自动跟随参考图；提示词写清「保留人物/换背景」更稳",
                "图片 URL 有效 24 小时，生成后立即下载；prompt_extend 开启对复杂描述更稳",
            ],
            "presets": [
                {"name": "方图 1024*1024", "values": {"size": "1024*1024", "n": 1}},
                {"name": "竖屏 768*1024", "values": {"size": "768*1024", "n": 1}},
                {"name": "横屏 1024*768", "values": {"size": "1024*768", "n": 1}},
            ],
        },
        "params": [
            _p("size", "输出尺寸", "select", "1024*1024",
               options=["512*512", "768*1024", "1024*768", "1024*1024", "1536*1024", "1024*1536", "2048*2048"],
               optionLabels={
                   "512*512": "512×512 · 最快，适合草稿/头像",
                   "768*1024": "768×1024 · 竖屏封面/短剧关键帧",
                   "1024*768": "1024×768 · 横屏插图",
                   "1024*1024": "1024×1024 · 方图默认（推荐）",
                   "1536*1024": "1536×1024 · 高清横图，耗时较高",
                   "1024*1536": "1024×1536 · 高清长图，耗时较高",
                   "2048*2048": "2048×2048 · 最大档，谨慎使用",
               },
               hint="面积需在 512²–2048²、长宽比 1:8–8:1；格式为「宽*高」（星号，不是 x）"),
            _p("n", "出图张数", "int", 1, min=1, max=4,
               hint="一次返回几张候选；多张会按张数计费，先用 1 张试提示词"),
            _p("prompt_extend", "提示词扩写", "select", "true", options=["true", "false"],
               optionLabels={"true": "开启（推荐）", "false": "关闭（严格按原文）"},
               hint="短提示词建议开启；已经写得很细的提示词可关闭"),
            _p("watermark", "图片水印", "select", "false", options=["false", "true"],
               optionLabels={"false": "不加水印（推荐）", "true": "加百炼水印"},
               hint="交付物默认不加水印"),
        ],
    },
    "wan21": {
        "kind": "video", "group": "lan",
        "label": "media-gen · WAN2.1 文生视频",
        "endpoint": "media-gen:9103",
        "notes": "WAN2.1-T2V-1.3B（480P），时长由 num_frames/fps 决定，最长 81 帧",
        "max_sec": 5.0,
        "modes": ["t2v"],
        "params": [
            _p("width", "宽", "int", 832, min=256, max=1280, step=16, multiple=16,
               hint="480P 档建议长边 ≤832；16:9 用 832×480，9:16 用 480×832"),
            _p("height", "高", "int", 480, min=256, max=1280, step=16, multiple=16,
               hint="与宽成对比；宽高自动对齐 16 的倍数"),
            _p("fps", "帧率", "int", 16, min=8, max=24,
               hint="16 帧是 1.3B 模型的训练档位；调高帧率等于同一时长要更多帧，耗时上升"),
            _p("steps", "采样步数", "int", 20, min=4, max=50,
               hint="建议 20；低于 12 动作会僵硬"),
            _p("cfg", "提示词权重 CFG", "float", 5.0, min=1, max=20, step=0.5,
               hint="Wan 系列 5 左右；越高越贴提示词，但运动越僵"),
            _p("seed", "随机种子", "int", -1, min=-1, max=2147483647,
               hint="-1 随机；固定种子可对比提示词改动"),
            _p("negative_prompt", "反向提示词", "text", "低质量, 模糊, 水印, 变形",
               hint="视频向建议加：静止, 闪烁, 画面撕裂, 畸形肢体"),
        ],
    },
    "minimax_h3": {
        "kind": "video", "group": "lan",
        "label": "media-gen · MiniMax H3 文生视频",
        "endpoint": "media-gen:9103",
        "notes": "MiniMax H3 开源权重（media-gen 未装载，选择后会明确失败，不静默降级）",
        "max_sec": 6.0,
        "modes": ["t2v"],
        "params": [
            _p("width", "宽", "int", 832, min=256, max=1280, step=16, multiple=16),
            _p("height", "高", "int", 480, min=256, max=1280, step=16, multiple=16),
            _p("fps", "帧率", "int", 16, min=8, max=24),
            _p("steps", "采样步数", "int", 20, min=1, max=50),
            _p("cfg", "提示词权重", "float", 5.0, min=1, max=20, step=0.5),
            _p("seed", "随机种子", "int", -1, min=-1, max=2147483647),
            _p("negative_prompt", "反向提示词", "text", "低质量, 模糊, 水印"),
        ],
    },
    "qwen": {
        "kind": "audio", "group": "lan",
        "label": "Qwen3-TTS · 千问",
        "endpoint": "media-engine:9200",
        "notes": "media-engine /v1/tts engine=qwen3（离线高质量，同步返回）",
        "modes": ["tts"],
        "params": [
            _p("speaker", "音色", "select", "Vivian",
               options=["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan",
                        "Aiden", "Ono_Anna", "Sohee"],
               optionLabels={
                   "Vivian": "Vivian · 明亮年轻女声（中文，默认）",
                   "Serena": "Serena · 温暖柔和年轻女声（中文）",
                   "Uncle_Fu": "Uncle_Fu · 低沉醇厚成熟男声（中文）",
                   "Dylan": "Dylan · 清亮自然北京青年男声（北京话）",
                   "Eric": "Eric · 活泼带沙哑的成都男声（四川话）",
                   "Ryan": "Ryan · 节奏感强的动感男声（英语）",
                   "Aiden": "Aiden · 阳光清晰的中频美音男声（英语）",
                   "Ono_Anna": "Ono_Anna · 俏皮灵动日语女声（日语）",
                   "Sohee": "Sohee · 情感丰富的温暖韩语女声（韩语）",
               },
               hint="按官方 Qwen3-TTS 音色描述标注；建议用该音色的母语，音质最好。"
                    "音色名对不上会回退到第一个"),
            _p("language", "语言", "select", "zh",
               options=["zh", "en", "ja", "ko"],
               hint="中文台词填 zh；中英混读也用 zh，模型会自动切换"),
            _p("instruct", "情绪指令", "text", "",
               hint="可留空，也可写「用兴奋的语气」「压低声音、急促」，直接影响语调"),
            _p("speed", "语速", "float", 1.0, min=0.5, max=2.0, step=0.1,
               hint="1.0 正常；口播偏慢用 0.9，急促旁白 1.1–1.2；超过 1.5 会明显失真"),
        ],
    },
    "cosyvoice": {
        "kind": "audio", "group": "lan",
        "label": "CosyVoice · 实时",
        "endpoint": "media-engine:9200",
        "notes": "media-engine /v1/tts engine=cosyvoice（实时、多 worker，hermes 的 tts.provider 也走它）；"
                 "音色下拉里的 clone-* 是自己克隆的音色",
        "modes": ["tts"],
        "params": [
            _p("voice", "音色", "select", "female-zh-real",
               options=["female-zh-real", "female-zh", "female-zh-short",
                        "m01", "m02", "m03", "f01", "f02", "f03",
                        "backup-zh-cosy2"],
               hint="female-zh-real 最自然（真女主播音色）；m0x 男声、f0x 女声；"
                    "clone-* 是下方「克隆音色」里用你自己录音建出来的音色，"
                    "列表会跟随 media-engine 实际音色表刷新"),
            _p("speed", "语速", "float", 1.0, min=0.5, max=2.0, step=0.1,
               hint="1.0 正常；0.9 更稳更适合口播；1.3 以上会有机械感"),
        ],
    },
    "bailian": {
        "kind": "audio", "group": "bailian",
        "label": "阿里云百炼 · Qwen-Audio-3.0-TTS-Plus",
        "endpoint": "token-plan.cn-beijing.maas.aliyuncs.com",
        "notes": "云服务模式：POST /api/v1/services/audio/tts/SpeechSynthesizer，"
                 "同步返回音频 URL 后立即下载（URL 有时效）。模型固定 "
                 "qwen-audio-3.0-tts-plus（可用 OPC_BAILIAN_TTS_MODEL 改），"
                 "按字符计费；情绪/语速等细粒度控制按官方说明写在文本标签里",
        "modes": ["tts"],
        "params": [
            _p("voice", "音色", "select", "longanhuan_v3.6",
               options=["longanhuan_v3.6", "longanyang_v3.6", "longjielidou_v3.6",
                        "longpaopao_v3.6", "longhuohuo_v3.6", "longchuanshu_v3.6"],
               optionLabels={
                   "longanhuan_v3.6": "龙安欢 · 活力欢快女声（默认）",
                   "longanyang_v3.6": "龙安阳 · 阳光友好年轻男声",
                   "longjielidou_v3.6": "龙杰力豆 · 天真男童声",
                   "longpaopao_v3.6": "龙泡泡 · 软糯可爱女童声",
                   "longhuohuo_v3.6": "龙火火 · 顽皮少年音",
                   "longchuanshu_v3.6": "龙川叔 · 川普大叔音",
               },
               hint="按阿里云「Qwen-Audio-TTS 音色列表」标注；完整列表以官方为准"),
            _p("format", "音频格式", "select", "mp3",
               options=["mp3", "wav", "pcm", "opus"],
               hint="mp3 体积小、网页可直接播；wav 无损但大；pcm/opus 供二次处理"),
            _p("sample_rate", "采样率", "int", 22050, min=8000, max=48000, step=50,
               hint="默认 22050；要更高清可填 44100/48000（以模型实际支持为准）"),
        ],
    },
}

_PROBE = {"at": 0.0, "data": {}}
_PROBE_LOCK = threading.Lock()


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SHOT_HEAD = re.compile(
    r"镜(?:头)?\s*([一二三四五六七八九十\d]{1,3})\s*[（(]?\s*"
    r"(?:(\d+(?:\.\d+)?)\s*[-–—~至]\s*(\d+(?:\.\d+)?)\s*s|(\d+(?:\.\d+)?)\s*s)?\s*[）)]?\s*"
    r"(?:[（(]\s*(?:\d+(?:\.\d+)?\s*s)?\s*[）)])?\s*[：:，,]?")
# 无「镜」字、直接用序号起行：「1｜0-6s …」「2) 手机怼脸…」「3·8s｜…」
_NUM_HEAD = re.compile(
    r"(?:^|(?<=[\n。；;]))\s*(\d{1,2})\s*[)）.、·:：|｜]\s*"
    r"(?:(\d+(?:\.\d+)?)\s*[-–—~至]\s*(\d+(?:\.\d+)?)\s*s|(\d+(?:\.\d+)?)\s*s)?")
# 连序号都没有，只有时间码：「0–4s …；4–14s …」
_TIME_HEAD = re.compile(
    r"(?:^|(?<=[\n。；;]))\s*(\d+(?:\.\d+)?)\s*[-–—~]\s*(\d+(?:\.\d+)?)\s*s")
_LINE_LABEL = re.compile(r"(?:台词|口播|旁白|配音)\s*[:：]")
_VIS_LABEL = re.compile(r"(?:画面|视觉|镜内)\s*[:：]")
_ASSET_LABEL = re.compile(r"插图\s*[:：]?\s*")
_TRIM = " \t\r\n／/｜|；;。、,，"


def _cn_num(tok):
    if tok.isdigit():
        return int(tok)
    if tok in _CN_NUM:
        return _CN_NUM[tok]
    if len(tok) == 2 and tok[0] == "十":
        return 10 + _CN_NUM.get(tok[1], 0)
    if len(tok) == 2 and tok[1] == "十":
        return _CN_NUM.get(tok[0], 0) * 10
    return None


def split_storyboard(text, total_sec=0):
    """把模型写的整段分镜按「镜一：／镜2 0–8s／镜3（8-17s）」切成清单。

    真实分镜文本一行到底、镜号写法五花八门（中文数字、时间码、括号），
    DL.normalize_storyboard 只按行切，所以这里单独做一个宽松切分器：
    依次尝试「镜N …」「N｜…」「N-Ms …」三种写法，取切出镜数最多的一种，
    切出每镜 画面（提示词）/ 台词（音频用）/ 时长 / 插图标注。
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    heads = []
    is_time = False
    for pat in (_SHOT_HEAD, _NUM_HEAD, _TIME_HEAD):
        found = list(pat.finditer(raw))
        if len(found) >= 2:
            heads, is_time = found, pat is _TIME_HEAD
            break
    if len(heads) < 2:
        return []
    out = []
    for idx, m in enumerate(heads):
        end = heads[idx + 1].start() if idx + 1 < len(heads) else len(raw)
        seg = raw[m.end():end]
        if is_time:
            no = idx + 1
            sec = round(float(m.group(2)) - float(m.group(1)), 2)
        elif m.group(2) and m.group(3):
            no = _cn_num(m.group(1)) or (idx + 1)
            sec = round(float(m.group(3)) - float(m.group(2)), 2)
        else:
            no = _cn_num(m.group(1)) or (idx + 1)
            sec = round(float(m.group(4)), 2) if m.group(4) else 0.0
        line = ""
        lm = _LINE_LABEL.search(seg)
        if lm:
            line = seg[lm.end():]
            seg = seg[:lm.start()]
            am = _ASSET_LABEL.search(line)
            if am:
                line = line[:am.start()]
        asset = ""
        am = _ASSET_LABEL.search(seg)
        if am:
            asset = seg[am.end():]
            seg = seg[:am.start()]
        vm = _VIS_LABEL.search(seg)
        if vm:
            seg = seg[vm.end():]
        if not line:
            # 没写「台词：」的，把引号里那句完整的话当口播（标签式短词如「只读」不算）
            qm = re.search(r"[「“]([^」”]{6,})[」”]", seg)
            if qm and (len(qm.group(1)) >= 8 or qm.group(1)[-1:] in "。！？…"):
                line = qm.group(1)
                seg = (seg[:qm.start()] + seg[qm.end():])
        if not line:
            # 「画面｜台词」用竖线分段的，后半段当口播
            parts = [p.strip(_TRIM) for p in re.split(r"[｜|]", seg)]
            if len(parts) >= 2 and len(parts[-1]) >= 6 and "插图" not in parts[-1]:
                line = parts[-1]
                seg = "｜".join(parts[:-1])
        for cut in ("）", ")", "，淡入", "。"):
            asset = asset.split(cut)[0]
        out.append({"no": no,
                    "prompt": seg.strip(_TRIM + "（("), "line": line.strip(_TRIM),
                    "sec": sec, "asset": asset.strip(_TRIM + "（(")})
    # 没有逐镜时间码的，按总时长均分
    if total_sec and all(not s["sec"] for s in out):
        each = round(float(total_sec) / len(out), 2)
        for s in out:
            s["sec"] = each
    return out


def _probe_media_gen():
    try:
        d = _http_json(MEDIAGEN + "/v1/backends", timeout=4)
        out = {}
        for b in d.get("backends") or []:
            out[b.get("name")] = {"available": bool(b.get("available")),
                                  "reason": b.get("reason") or "",
                                  "loaded": bool(b.get("loaded"))}
        return out
    except Exception as e:
        why = f"media-gen 不可达（{MEDIAGEN}）：{type(e).__name__}: {e}"
        return {k: {"available": False, "reason": why}
                for k in ("sd15", "wan21", "minimax_h3")}


def _probe_comfy(name="comfyui"):
    """按渠道探测对应 ComfyUI 端点（出图 8188 / 视频 8189 是两个独立实例）。"""
    try:
        import asset_fill
        url = (asset_fill._comfy_video_url() if name == "comfyui_video"
               else asset_fill._comfy_image_url())
        return {"available": True, "reason": "", "endpoint": url}
    except Exception as e:
        return {"available": False,
                "reason": f"ComfyUI 端点不可用：{type(e).__name__}: {e}"}


def _probe_comfyui():
    return _probe_comfy("comfyui")


def _probe_media_engine():
    try:
        d = _http_json(MEDIAENGINE + "/health", timeout=4)
        ok = str(d.get("status") or "").lower() in ("ok", "success", "healthy")
        reason = "" if ok else "media-engine 健康检查未通过"
        return {"available": ok, "reason": reason}
    except Exception as e:
        return {"available": False,
                "reason": f"media-engine 不可达（{MEDIAENGINE}）：{type(e).__name__}: {e}"}


def _probe_bailian():
    """百炼是云服务：有 Key 即视为可用，不额外打真实合成（避免烧额度）。"""
    if not _bailian_api_key():
        return {"available": False,
                "reason": "未配置百炼 API Key：设置 OPC_BAILIAN_TTS_API_KEY（或复用百炼 LLM Key）"}
    return {"available": True, "reason": "云服务 · 按量计费", "endpoint": BAILIAN_TTS_BASE}


def channels(force=False):
    """渠道视图：参数规格 + 实时可用性（探测结果缓存 30s）。"""
    with _PROBE_LOCK:
        if force or time.time() - _PROBE["at"] > PROBE_TTL or not _PROBE["data"]:
            mg = _probe_media_gen()
            me = _probe_media_engine()
            bailian = _probe_bailian()
            _PROBE["data"] = {
                "comfyui": _probe_comfy("comfyui"),
                "comfyui_157": _probe_comfy("comfyui_157"),
                "comfyui_video": _probe_comfy("comfyui_video"),
                "sd15": mg.get("sd15") or {"available": False, "reason": "media-gen 未登记 sd15"},
                "wan21": mg.get("wan21") or {"available": False, "reason": "media-gen 未登记 wan21"},
                "minimax_h3": mg.get("minimax_h3") or {"available": False,
                                                       "reason": "media-gen 未登记 minimax_h3"},
                "qwen": me, "cosyvoice": dict(me), "bailian": bailian,
                "bailian_image": dict(bailian, endpoint=BAILIAN_IMAGE_BASE),
            }
            _PROBE["at"] = time.time()
        probe = dict(_PROBE["data"])
    # 克隆音色是运行期新增的，音色下拉必须按 voices.json 实际内容渲染
    voice_items = (voices_list() or {}).get("voices") or []
    opts = [v["id"] for v in voice_items]
    vlabels = {v["id"]: (f"{v['id']} · {v['description']}" if v.get("description") else v["id"])
               for v in voice_items}
    out = []
    for name, ch in CHANNELS.items():
        st = probe.get(name) or {}
        cap = ch.get("max_sec")
        params = ch["params"]
        if name == "cosyvoice" and opts:
            params = [dict(p, options=opts, optionLabels=vlabels)
                      if p["key"] == "voice" else p for p in params]
        out.append({
            "name": name, "kind": ch["kind"], "label": ch["label"], "notes": ch["notes"],
            "group": ch.get("group") or "lan",
            "endpoint": st.get("endpoint") or ch.get("endpoint") or "",
            "modes": ch.get("modes") or [],
            "maxSec": min([x for x in (cap, SHOT_MAX_SEC) if x]) if ch["kind"] == "video" else None,
            "concurrency": LANE_LIMITS.get(name, 1),
            "queue": int(st.get("queue") or 0),
            "available": bool(st.get("available")), "reason": st.get("reason") or "",
            "params": params, "spec": ch.get("spec"),
        })
    return out, SHOT_MAX_SEC


def groups():
    """渠道分组元数据（顺序即前端展示顺序）。"""
    return [dict(g) for g in GROUPS]


def channel(name):
    return CHANNELS.get(str(name or ""))


# ---------------------------------------------------------------- 参数与时长
def _coerce_params(ch, raw):
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for spec in ch["params"]:
        key, typ = spec["key"], spec["type"]
        val = raw.get(key, spec.get("default"))
        try:
            if typ == "int":
                val = int(float(val))
                if spec.get("min") is not None:
                    val = max(spec["min"], val)
                if spec.get("max") is not None:
                    val = min(spec["max"], val)
                mult = int(spec.get("multiple") or 0)
                if mult > 1:  # 宽高需对齐（SD 8 的倍数、WAN 16 的倍数），就近取整
                    val = max(mult, int(round(val / mult)) * mult)
                    if spec.get("max") is not None:
                        val = min(int(spec["max"]) // mult * mult, val)
            elif typ == "float":
                val = float(val)
                if spec.get("min") is not None:
                    val = max(spec["min"], val)
                if spec.get("max") is not None:
                    val = min(spec["max"], val)
                val = round(val, 2)
            elif typ == "select":
                opts = spec.get("options") or []
                val = str(val)
                if opts and val not in opts:
                    val = spec.get("default") or opts[0]
            else:
                val = str(val if val is not None else "")
        except (TypeError, ValueError):
            val = spec.get("default")
        out[key] = val
    return out


def _frames_for(sec, fps, cap):
    """把秒数换算成 WAN 可接受的 4k+1 帧数（≤81），并受 cap 约束。"""
    fps = max(8, min(int(fps or 16), 24))
    max_frames = 81
    cap_frames = int(round(float(cap) * fps))
    if cap_frames > 0:
        max_frames = min(81, ((max(5, cap_frames) - 1) // 4) * 4 + 1)
    want = int(round(float(sec or 0) * fps))
    want = max(5, min(max_frames, want))
    return ((want - 1) // 4) * 4 + 1, fps


def plan_duration(ch_name, ch, params, sec):
    """返回 (实际时长秒, params 补丁, 夹取说明)。仅视频渠道有时长概念。"""
    if ch["kind"] != "video":
        return 0.0, {}, ""
    cap = min([x for x in (ch.get("max_sec"), SHOT_MAX_SEC) if x])
    want = float(sec or 0) or cap
    over = want > cap + 1e-6
    capped = min(want, cap)
    if ch_name in ("wan21", "minimax_h3", "comfyui_video"):
        frames, fps = _frames_for(capped, params.get("fps") or 16, cap)
        actual = round(frames / fps, 2)
        if over:
            note = f"分镜时长 {want:.1f}s 超过 {ch_name} 上限 {cap:.1f}s，已夹到 {actual:.2f}s"
        elif want - actual >= 0.5:  # 明显被帧数约束拉低才提示，避免噪音
            note = f"按帧数约束（{frames} 帧 @{fps}fps）实际 {actual:.2f}s"
        else:
            note = ""
        return actual, {"num_frames": frames, "fps": fps}, note
    if over:
        return round(capped, 2), {}, f"分镜时长 {want:.1f}s 超过 {ch_name} 上限 {cap:.1f}s，已夹到 {capped:.2f}s"
    return round(want, 2), {}, ""


# ---------------------------------------------------------------- 存储
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, trace_id TEXT, item_id TEXT, project_id TEXT, shot_no INTEGER,
  kind TEXT, channel TEXT, prompt TEXT, params TEXT, status TEXT, progress INTEGER,
  output_path TEXT, output_url TEXT, duration_sec REAL, error TEXT, note TEXT,
  remote_id TEXT, out_dir TEXT, vault_dir TEXT, run_no INTEGER DEFAULT 1,
  created_at TEXT, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mj_trace ON jobs(trace_id, item_id, shot_no, created_at DESC);
CREATE TABLE IF NOT EXISTS shots (
  trace_id TEXT NOT NULL, item_id TEXT NOT NULL, shot_no INTEGER NOT NULL,
  prompt TEXT, line TEXT, sec REAL, channels TEXT, params TEXT, updated_at TEXT,
  PRIMARY KEY (trace_id, item_id, shot_no)
);
"""

JOB_FIELDS = ("id", "trace_id", "item_id", "project_id", "shot_no", "kind", "channel",
              "prompt", "params", "status", "progress", "output_path", "output_url",
              "duration_sec", "error", "note", "remote_id", "out_dir", "vault_dir",
              "run_no", "created_at", "started_at", "finished_at")


class Manager:
    """一个工作区一份作业库：提交/查询/停止/重试；并发与进度在进程内统一调度。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.notify = None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = self._conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- 读写（连接用完即关，避免长跑进程句柄泄漏）
    def _write(self, sql, params=()):
        try:
            conn = self._conn()
            try:
                conn.execute(sql, params)
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"写库失败 {sql[:50]}：{e}")

    def get(self, job_id):
        try:
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"读作业失败：{e}")
            return None

    def jobs(self, trace_id, item_id="", shot_no=None, limit=400):
        sql = "SELECT * FROM jobs WHERE trace_id=?"
        args = [trace_id]
        if item_id:
            sql += " AND item_id=?"
            args.append(item_id)
        if shot_no is not None:
            sql += " AND shot_no=?"
            args.append(int(shot_no))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        try:
            conn = self._conn()
            try:
                return [_view(dict(r)) for r in conn.execute(sql, args).fetchall()]
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"列表失败：{e}")
            return []

    def toolbox_jobs(self, limit=40):
        """工具箱手工任务：trace_id 以 tb- 开头，独立于流水线，按提交时间倒序。

        列表顺带对账：产物是磁盘上的文件，用户在资源管理器里删掉后库里还留着地址，
        界面上就会一直挂着一个点开必然 404 的卡片。这里发现文件没了就把地址清掉
        （任务历史保留），下次列表就只会显示“成品已删除”。
        """
        try:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE trace_id LIKE 'tb-%' "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (max(1, min(int(limit), 200)),)).fetchall()
                out, dead = [], []
                for r in rows:
                    row = dict(r)
                    path = str(row.get("output_path") or "").strip()
                    url = str(row.get("output_url") or "").strip()
                    if url and path and not os.path.isfile(path):
                        # 只有上级目录还在才判定“文件被删”，网络盘掉线时不误删记录
                        if os.path.isdir(os.path.dirname(path)):
                            dead.append(path)
                            row["output_path"] = ""
                            row["output_url"] = ""
                            row["error"] = row.get("error") or "成品文件已删除"
                        else:
                            row["output_url"] = ""
                            row["error"] = row.get("error") or "成品所在目录不可访问"
                    out.append(_view(row))
                if dead:
                    conn.executemany(
                        "UPDATE jobs SET output_path=NULL, output_url=NULL, "
                        "error=CASE WHEN error IS NULL OR error='' THEN ? ELSE error END "
                        "WHERE output_path=?",
                        [("成品文件已删除", p) for p in dead])
                    conn.commit()
                    _log(f"工具箱对账：{len(dead)} 个成品已被删除，记录已清空")
                return out
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"工具箱列表失败：{e}")
            return []

    def forget_output(self, dest):
        """成品被删除后清掉指向它的产物字段。

        任务历史保留（谁在什么时候跑过什么还能看到），只是不再给出一个必然 404 的地址；
        dest 是目录时按前缀清空整棵子树。
        """
        dest = os.path.abspath(str(dest or "").strip())
        if not dest:
            return 0
        try:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE jobs SET output_path=NULL, output_url=NULL "
                    "WHERE output_path=? OR output_path LIKE ?",
                    (dest, dest + "/%"))
                conn.commit()
                return cur.rowcount or 0
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"清空产物记录失败：{e}")
            return 0

    def shots(self, trace_id, item_id):
        try:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM shots WHERE trace_id=? AND item_id=?",
                    (trace_id, item_id)).fetchall()
                return {int(r["shot_no"]): dict(r) for r in rows}
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"读分镜覆盖失败：{e}")
            return {}

    def shot_save(self, trace_id, item_id, shot_no, fields):
        cur = self.shots(trace_id, item_id).get(int(shot_no)) or {}
        prompt = fields.get("prompt", cur.get("prompt")) or ""
        line = fields.get("line", cur.get("line")) or ""
        sec = fields.get("sec", cur.get("sec"))
        # 没传渠道/参数（或传 None）时保留上次选择，避免「只改提示词」把方案清空
        chans = fields.get("channels")
        if chans is None:
            chans = json.loads(cur.get("channels") or "[]")
        params = fields.get("params")
        if params is None:
            params = json.loads(cur.get("params") or "{}")
        rec = {"trace_id": trace_id, "item_id": item_id, "shot_no": int(shot_no),
               "prompt": prompt, "line": line, "sec": sec,
               "channels": json.dumps(chans or [], ensure_ascii=False),
               "params": json.dumps(params or {}, ensure_ascii=False),
               "updated_at": now_iso()}
        self._write("""INSERT INTO shots(trace_id,item_id,shot_no,prompt,line,sec,channels,params,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(trace_id,item_id,shot_no) DO UPDATE SET
                         prompt=excluded.prompt, line=excluded.line, sec=excluded.sec,
                         channels=excluded.channels, params=excluded.params,
                         updated_at=excluded.updated_at""",
                    tuple(rec[k] for k in ("trace_id", "item_id", "shot_no", "prompt", "line",
                                           "sec", "channels", "params", "updated_at")))
        return self.shots(trace_id, item_id).get(int(shot_no)) or rec

    # ---- 作业状态
    def _set(self, job_id, **fields):
        if not fields:
            return
        keys = [k for k in fields if k in JOB_FIELDS]
        sql = "UPDATE jobs SET " + ", ".join(f"{k}=?" for k in keys) + " WHERE id=?"
        self._write(sql, tuple(fields[k] for k in keys) + (job_id,))

    def _emit(self, job_id, force=False):
        row = self.get(job_id)
        if row and self.notify:
            try:
                self.notify("media-job", row.get("trace_id"), _view(row))
            except Exception as e:
                _log(f"事件推送失败：{type(e).__name__}: {e}")
        return row

    # ---- 提交
    def submit(self, *, trace_id, item_id, project_id, shot_no, kind, channels,
               prompt, line="", sec=0.0, params=None, out_dir="", vault_dir=""):
        kind = str(kind or "").strip()
        if kind not in KINDS:
            raise ValueError(f"未知素材类型：{kind}（可选 {'/'.join(KINDS)}）")
        chans = [str(c) for c in (channels or []) if str(c).strip()]
        if not chans:
            raise ValueError("至少选择一个制作渠道")
        text = str(prompt if kind != "audio" else (line or prompt) or "").strip()
        if not text:
            raise ValueError("音频渠道需要「台词」，图片/视频渠道需要「提示词」")
        params_all = params if isinstance(params, dict) else {}
        jobs, warnings = [], []
        for name in chans:
            ch = CHANNELS.get(name)
            if not ch:
                warnings.append(f"未知渠道 {name}，已跳过")
                continue
            if ch["kind"] != kind:
                warnings.append(f"渠道 {name} 属于 {ch['kind']}，与所选类型 {kind} 不符，已跳过")
                continue
            raw_p = params_all.get(name) or {}
            p = _coerce_params(ch, raw_p)
            # 首帧不是「参数规格」里的项（是文件），单独带过来，且只认支持 i2v 的渠道
            frame = os.path.basename(str(raw_p.get("frame") or "").strip())
            if frame and "i2v" in (ch.get("modes") or []):
                p["frame"] = frame
            dur, patch, note = plan_duration(name, ch, p, sec)
            p.update(patch)
            jid = "mj_" + uuid.uuid4().hex[:12]
            row = {"id": jid, "trace_id": trace_id, "item_id": item_id,
                   "project_id": project_id or "", "shot_no": int(shot_no or 0),
                   "kind": kind, "channel": name, "prompt": text,
                   "params": json.dumps(p, ensure_ascii=False), "status": "queued",
                   "progress": 0, "output_path": "", "output_url": "", "duration_sec": dur,
                   "error": "", "note": note, "remote_id": "", "out_dir": out_dir or "",
                   "vault_dir": vault_dir or "", "run_no": 1,
                   "created_at": now_iso(), "started_at": "", "finished_at": ""}
            self._write(f"INSERT INTO jobs({','.join(JOB_FIELDS)}) "
                        f"VALUES({','.join('?' for _ in JOB_FIELDS)})",
                        tuple(row[k] for k in JOB_FIELDS))
            if note:
                warnings.append(f"{name}：{note}")
            _POOL.submit(_run_job, self, jid)
            jobs.append(_view(row))
        if not jobs and not warnings:
            raise ValueError("没有可提交的渠道")
        return {"jobs": jobs, "warnings": warnings}

    def cancel(self, job_ids):
        out = []
        for jid in job_ids or []:
            row = self.get(str(jid))
            if not row:
                out.append({"id": jid, "ok": False, "error": "作业不存在"})
                continue
            if row["status"] in ("done", "failed", "canceled"):
                out.append({"id": jid, "ok": False, "error": f"作业已{row['status']}，无需停止"})
                continue
            _CANCEL.add(str(jid))
            self._set(str(jid), status="canceled", progress=int(row.get("progress") or 0),
                      finished_at=now_iso(), error="老板手动停止")
            self._emit(str(jid))
            out.append({"id": jid, "ok": True, "status": "canceled"})
        return out

    def retry(self, job_ids):
        out = []
        for jid in job_ids or []:
            row = self.get(str(jid))
            if not row:
                out.append({"id": jid, "ok": False, "error": "作业不存在"})
                continue
            new_id = "mj_" + uuid.uuid4().hex[:12]
            clone = dict(row)
            clone.update(id=new_id, status="queued", progress=0, error="", note="",
                         remote_id="", output_path="", output_url="",
                         run_no=int(row.get("run_no") or 1) + 1,
                         created_at=now_iso(), started_at="", finished_at="")
            self._write(f"INSERT INTO jobs({','.join(JOB_FIELDS)}) "
                        f"VALUES({','.join('?' for _ in JOB_FIELDS)})",
                        tuple(clone.get(k) for k in JOB_FIELDS))
            _CANCEL.discard(new_id)
            _POOL.submit(_run_job, self, new_id)
            out.append({"id": new_id, "retryOf": str(jid), "ok": True, "status": "queued"})
        return out


def _view(row):
    """作业视图（camelCase，前端直接用；不暴露服务器绝对路径）。"""
    if not row:
        return {}
    try:
        params = json.loads(row.get("params") or "{}")
    except ValueError:
        params = {}
    return {
        "id": row.get("id"), "traceId": row.get("trace_id"),
        "itemId": row.get("item_id"), "projectId": row.get("project_id"),
        "shotNo": int(row.get("shot_no") or 0), "kind": row.get("kind"),
        "channel": row.get("channel"), "prompt": row.get("prompt") or "",
        "params": params, "status": row.get("status") or "queued",
        "progress": int(row.get("progress") or 0),
        "outputUrl": row.get("output_url") or "",
        "durationSec": row.get("duration_sec") or 0,
        "error": row.get("error") or "", "note": row.get("note") or "",
        "runNo": row.get("run_no") or 1,
        "createdAt": row.get("created_at") or "",
        "startedAt": row.get("started_at") or "",
        "finishedAt": row.get("finished_at") or "",
    }


# ---------------------------------------------------------------- 调度与执行
_POOL = ThreadPoolExecutor(max_workers=max(1, MAX_WORKERS), thread_name_prefix="media-job")
_LANES = {name: threading.BoundedSemaphore(max(1, n)) for name, n in LANE_LIMITS.items()}
_CANCEL = set()
_MANAGERS = {}
_MANAGERS_LOCK = threading.Lock()
# 本进程正在被线程跟踪的作业：看门狗只对「不在集合里的 running」动手，避免重复挂轮询
_ACTIVE_JOBS = set()
_ACTIVE_LOCK = threading.Lock()
WATCHDOG_SEC = float(os.environ.get("OPC_MEDIA_WATCHDOG_SEC", "120"))


def manager(path):
    path = os.path.abspath(path)
    with _MANAGERS_LOCK:
        fresh = path not in _MANAGERS
        mgr = _MANAGERS.get(path)
        if mgr is None:
            mgr = Manager(path)
            _MANAGERS[path] = mgr
    if fresh:
        # 进程重启后线程池里没有旧作业的轮询线程了：立刻对账一次
        _POOL.submit(_recover_orphans, mgr)
    return mgr


class Canceled(Exception):
    pass


def _canceling(job_id):
    if job_id in _CANCEL:
        raise Canceled()


def _progress(mgr, job_id, pct, last):
    pct = max(0, min(99, int(pct)))
    if pct - last >= 4:
        mgr._set(job_id, progress=pct)
        mgr._emit(job_id)
        return pct
    return last


def _run_job(mgr, job_id):
    with _ACTIVE_LOCK:
        _ACTIVE_JOBS.add(job_id)
    try:
        _run_job_inner(mgr, job_id)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_JOBS.discard(job_id)


def _run_job_inner(mgr, job_id):
    row = mgr.get(job_id)
    if not row or row.get("status") != "queued":
        return
    if job_id in _CANCEL:
        mgr._set(job_id, status="canceled", finished_at=now_iso(), error="老板手动停止")
        mgr._emit(job_id)
        return
    ch = CHANNELS.get(row.get("channel") or "")
    if not ch:
        mgr._set(job_id, status="failed", finished_at=now_iso(), error="未知渠道")
        mgr._emit(job_id)
        return
    try:
        params = json.loads(row.get("params") or "{}")
    except ValueError:
        params = {}
    mgr._set(job_id, status="running", started_at=now_iso(), progress=2)
    mgr._emit(job_id)
    lane = _LANES.get(row["channel"]) or threading.BoundedSemaphore(1)
    last = [2]

    def on_progress(pct):
        last[0] = _progress(mgr, job_id, pct, last[0])

    try:
        with lane:
            _canceling(job_id)
            if row["kind"] == "image":
                image_worker = {
                    "comfyui": _comfy_image, "comfyui_157": _comfy_image,
                    "bailian_image": _bailian_image,
                }.get(row["channel"]) or _mediagen_image
                result = image_worker(mgr, row, params, on_progress)
            elif row["kind"] == "video":
                result = (_comfy_video if row["channel"] == "comfyui_video"
                          else _mediagen_video)(mgr, row, params, on_progress)
            else:
                result = _tts(mgr, row, params, on_progress)
        path, url = _deliver(mgr, row, result)
        mgr._set(job_id, status="done", progress=100, output_path=path, output_url=url,
                 duration_sec=result.get("duration") or row.get("duration_sec") or 0,
                 remote_id=result.get("remote_id") or "", note=result.get("note") or row.get("note") or "",
                 finished_at=now_iso(), error="")
        _CANCEL.discard(job_id)
        mgr._emit(job_id)
    except Canceled:
        mgr._set(job_id, status="canceled", finished_at=now_iso(), error="老板手动停止")
        _CANCEL.discard(job_id)
        mgr._emit(job_id)
    except Exception as e:  # noqa: BLE001 - 失败要落到作业体里给前端看
        mgr._set(job_id, status="failed", finished_at=now_iso(),
                 error=f"{type(e).__name__}: {e}")
        _CANCEL.discard(job_id)
        mgr._emit(job_id)


# ---- 重启恢复 / 看门狗：进程重启后线程池蒸发，但远端（media-gen / ComfyUI）仍在跑 ----
_MEDIAGEN_CHANNELS = ("sd15", "wan21", "minimax_h3")
_COMFY_CHANNELS = ("comfyui", "comfyui_video")


def _remote_kind(channel):
    return "mediagen" if channel in _MEDIAGEN_CHANNELS else (
        "comfy" if channel in _COMFY_CHANNELS else "")


def _recover_one(mgr, row):
    """对一条孤儿作业：回查远端真实状态，成功则取回，失败则落错；仍在跑则重挂轮询。

    不重新提交远端作业（会重复生成）：只拿 row 里已保存的 remote_id 回查。
    """
    jid = row["id"]
    rid = str(row.get("remote_id") or "").strip()
    kind = _remote_kind(row.get("channel") or "")
    if row.get("status") == "queued":
        # 还没开始提交（重启时排在队列里）：重新走正常提交流程，幂等安全
        _log(f"恢复重投 queued 作业 {jid}")
        _POOL.submit(_run_job, mgr, jid)
        return
    if not rid or not kind:
        # running 但无作业号：提交阶段就重启了，无法回查，明确失败让老板重试，
        # 绝不重新提交（远端可能已经排了队，重提会变成两次生成）
        mgr._set(jid, status="failed", finished_at=now_iso(), error="服务重启，远端作业号丢失，请重新提交")
        mgr._emit(jid)
        return
    with _ACTIVE_LOCK:
        if jid in _ACTIVE_JOBS:
            return
        _ACTIVE_JOBS.add(jid)
    try:
        if kind == "mediagen":
            _recover_mediagen(mgr, row, rid)
        else:
            _recover_comfy(mgr, row, rid)
    except Exception as e:  # noqa: BLE001 - 对账失败只记日志，作业保持 running 等下轮
        _log(f"恢复对账 {jid} 异常：{type(e).__name__}: {e}")
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_JOBS.discard(jid)


def _recover_mediagen(mgr, row, rid):
    jid = row["id"]
    v = _http_json(f"{MEDIAGEN}/v1/jobs/{rid}", timeout=15)
    st = str(v.get("status") or "")
    if st == "succeeded":
        dur = round(params_sec(v), 2) if row.get("kind") == "video" else 0
        path = v.get("output_path") or ""
        if not (path and os.path.isfile(path)):
            url = v.get("output_url") or ""
            if not url:
                raise RuntimeError("media-gen 作业成功但没有产物路径")
            path = os.path.join(BASE_DIR, ".cache", "media",
                                f"{jid}{os.path.splitext(url)[1] or '.bin'}")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(_http(MEDIAGEN + url, timeout=120))
        dest, url = _deliver(mgr, row, {"path": path})
        mgr._set(jid, status="done", progress=100, output_path=dest, output_url=url,
                 duration_sec=dur or row.get("duration_sec") or 0,
                 finished_at=now_iso(), error="", note=(row.get("note") or "") + " · 重启后自动取回")
        mgr._emit(jid)
        _log(f"恢复取回成功 {jid} → {dest}")
    elif st in ("failed", "interrupted"):
        mgr._set(jid, status="failed", finished_at=now_iso(),
                 error=v.get("error") or f"远端作业{st}")
        mgr._emit(jid)
    elif st in ("running", "queued"):
        _reattach(mgr, jid)


def _recover_comfy(mgr, row, rid):
    jid = row["id"]
    import asset_fill
    is_video = row.get("channel") == "comfyui_video"
    url = asset_fill._comfy_video_url() if is_video else asset_fill._comfy_image_url()
    hist = _http_json(f"{url}/history/{rid}", timeout=20)
    entry = hist.get(rid) or {}
    if entry.get("status", {}).get("status_str") == "error":
        mgr._set(jid, status="failed", finished_at=now_iso(), error="ComfyUI 作业执行失败（重启对账）")
        mgr._emit(jid)
        return
    node = "9" if is_video else "7"
    outs = (entry.get("outputs") or {}).get(node) or {}
    files = outs.get("images") or outs.get("gifs") or []
    if not files:
        _reattach(mgr, jid)  # 远端还在跑，重新挂轮询
        return
    f = files[0]
    q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                                "type": f.get("type", "output")})
    blob = _http(f"{url}/view?{q}", timeout=300)
    src = os.path.join(BASE_DIR, ".cache", "media", f"{jid}.webm" if is_video else f"{jid}.png")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, "wb") as fp:
        fp.write(blob)
    result = {"path": _to_mp4(src) if is_video else src, "remote_id": rid}
    if is_video:
        result["duration"] = row.get("duration_sec") or 0
    dest, out_url = _deliver(mgr, row, result)
    mgr._set(jid, status="done", progress=100, output_path=dest, output_url=out_url,
             duration_sec=result.get("duration") or 0, finished_at=now_iso(), error="",
             note=(row.get("note") or "") + " · 重启后自动取回")
    mgr._emit(jid)
    _log(f"恢复取回成功 {jid} → {dest}")


def _reattach(mgr, jid):
    """远端仍在跑：重挂一条「只轮询、不重新提交」的作业线程。"""
    jid = str(jid)
    with _ACTIVE_LOCK:
        if jid in _ACTIVE_JOBS:
            return
        _ACTIVE_JOBS.add(jid)

    def done():
        with _ACTIVE_LOCK:
            _ACTIVE_JOBS.discard(jid)

    _POOL.submit(_reattach_inner, mgr, jid, done)


def _reattach_inner(mgr, jid, done):
    try:
        row = mgr.get(jid)
        if not row or row.get("status") not in ("running", "queued"):
            return
        params = json.loads(row.get("params") or "{}")
        last = [int(row.get("progress") or 0)]

        def on_progress(pct):
            last[0] = _progress(mgr, jid, pct, last[0])

        if row.get("channel") == "wan21":
            result = _poll_mediagen(mgr, row, {"id": row["remote_id"]}, on_progress)
        elif row.get("channel") in _COMFY_CHANNELS:
            # ComfyUI 重挂：用历史记录等待，不重新 POST /prompt
            result = _wait_comfy(mgr, row, on_progress)
        else:
            raise RuntimeError(f"渠道 {row.get('channel')} 不支持重挂轮询")
        dest, url = _deliver(mgr, row, result)
        mgr._set(jid, status="done", progress=100, output_path=dest, output_url=url,
                 duration_sec=result.get("duration") or row.get("duration_sec") or 0,
                 finished_at=now_iso(), error="", note=(row.get("note") or "") + " · 重挂后取回")
        _CANCEL.discard(jid)
        mgr._emit(jid)
        _log(f"重挂取回成功 {jid} → {dest}")
    except Exception as e:  # noqa: BLE001
        mgr._set(jid, status="failed", finished_at=now_iso(),
                 error=f"重挂轮询失败：{type(e).__name__}: {e}")
        _CANCEL.discard(jid)
        mgr._emit(jid)
    finally:
        done()


def _wait_comfy(mgr, row, on_progress):
    """只轮询 /history/{pid} 等产物，不重新提交；供重启重挂使用。"""
    import asset_fill
    jid = row["id"]
    is_video = row.get("channel") == "comfyui_video"
    url = asset_fill._comfy_video_url() if is_video else asset_fill._comfy_image_url()
    node = "9" if is_video else "7"
    t0 = time.time()
    deadline = t0 + float(os.environ.get("OPC_COMFY_VIDEO_TIMEOUT" if is_video
                                         else "OPC_COMFY_TIMEOUT", "5400"))
    while time.time() < deadline:
        _canceling(jid)
        entry = (_http_json(f"{url}/history/{row['remote_id']}", timeout=20)
                 .get(row["remote_id"]) or {})
        if entry.get("status", {}).get("status_str") == "error":
            raise RuntimeError("ComfyUI 作业执行失败（重挂轮询）")
        outs = (entry.get("outputs") or {}).get(node) or {}
        files = outs.get("images") or outs.get("gifs") or []
        if files:
            f = files[0]
            q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                                        "type": f.get("type", "output")})
            blob = _http(f"{url}/view?{q}", timeout=300)
            src = os.path.join(BASE_DIR, ".cache", "media",
                               f"{jid}.webm" if is_video else f"{jid}.png")
            os.makedirs(os.path.dirname(src), exist_ok=True)
            with open(src, "wb") as fp:
                fp.write(blob)
            out = {"path": _to_mp4(src) if is_video else src, "remote_id": row["remote_id"]}
            if is_video:
                out["duration"] = row.get("duration_sec") or 0
            return out
        on_progress(10 + int(min(80, (time.time() - t0) / 180.0 * 80)))
        time.sleep(3)
    raise TimeoutError("重挂轮询超时（ComfyUI）")


def _recover_orphans(mgr):
    """扫描所有 running/queued 且本进程无人跟踪的作业，逐个回查远端。"""
    try:
        conn = mgr._conn()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM jobs WHERE status IN ('running','queued') ORDER BY created_at ASC")]
        finally:
            conn.close()
    except sqlite3.Error as e:
        _log(f"恢复扫描失败：{e}")
        return
    stale = [r for r in rows if not str(r.get("started_at") or "").strip()]
    for row in rows:
        _recover_one(mgr, row)
    if stale:
        _log(f"恢复扫描：{len(rows)} 条在跑，其中 {len(stale)} 条无启动时间")


def _watchdog_loop():
    while True:
        time.sleep(max(30.0, WATCHDOG_SEC))
        try:
            with _MANAGERS_LOCK:
                managers = list(_MANAGERS.values())
            for mgr in managers:
                _recover_orphans(mgr)
        except Exception as e:  # noqa: BLE001 - 看门狗自己不能崩
            _log(f"看门狗异常：{type(e).__name__}: {e}")


# ---- 渠道适配器：返回 {"path": 本地成品路径, "duration": 秒, "remote_id": ...}
FRAME_DIR = os.path.join(BASE_DIR, ".cache", "frames")


def _seed_of(params):
    """seed=-1（或空）走随机，其它按原值复现。"""
    try:
        seed = int(params.get("seed"))
    except (TypeError, ValueError):
        seed = -1
    return seed if seed >= 0 else int(time.time() * 1000) % (1 << 31)


def _opt_names(info, node, field):
    """取节点某字段的可选值（兼容 ComfyUI 新旧两种 object_info 格式）。"""
    spec = (info.get(node) or {}).get("input", {}).get("required", {}).get(field) or []
    if not isinstance(spec, list) or not spec:
        return []
    cand = spec[0] if isinstance(spec[0], list) else spec
    return [str(x) for x in cand if isinstance(x, str)]


def _pick_opt(info, node, field, pattern, fallback=""):
    """按正则挑模型文件；挑不到就退回第一个候选（再没有返回 fallback）。"""
    names = _opt_names(info, node, field)
    for n in names:
        if re.search(pattern, n, re.I):
            return n
    return names[0] if names else fallback


def frame_dir():
    os.makedirs(FRAME_DIR, exist_ok=True)
    return FRAME_DIR


def _safe_frame_path(name):
    """首帧只允许读 .cache/frames 下的文件，挡住 ../ 之类的路径穿越。"""
    p = os.path.abspath(os.path.join(FRAME_DIR, os.path.basename(str(name or ""))))
    if not p.startswith(os.path.abspath(FRAME_DIR) + os.sep) or not os.path.isfile(p):
        raise RuntimeError("首帧图片不存在或已被清理，请重新上传")
    return p


def _comfy_upload_image(url, path):
    """把首帧图传到 ComfyUI 的 input 目录，返回 LoadImage 可用的文件名。"""
    boundary = "----opc" + uuid.uuid4().hex
    name = os.path.basename(path)
    ctype = "image/png" if name.lower().endswith(".png") else "image/jpeg"
    with open(path, "rb") as f:
        blob = f.read()
    parts = [
        b"--" + boundary.encode() + b"\r\n",
        b'Content-Disposition: form-data; name="image"; filename="' + name.encode() + b'"\r\n',
        b"Content-Type: " + ctype.encode() + b"\r\n\r\n", blob, b"\r\n",
        b"--" + boundary.encode() + b"\r\n",
        b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
        b"--" + boundary.encode() + b"--\r\n",
    ]
    raw = _http(url + "/upload/image", timeout=120, data=b"".join(parts),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    d = json.loads(raw.decode("utf-8") or "{}")
    saved = str(d.get("name") or name)
    sub = str(d.get("subfolder") or "")
    return f"{sub}/{saved}" if sub else saved


def _to_mp4(src):
    """ComfyUI 只给 webm（vp9），转成 mp4 才能进剪辑；转失败就保留 webm。"""
    ff = shutil.which("ffmpeg") or os.path.expanduser("~/.local/bin/ffmpeg")
    if not ff or not os.path.isfile(ff):
        return src
    dst = os.path.splitext(src)[0] + ".mp4"
    try:
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", src, "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst],
                       check=True, timeout=600)
        if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
            try:
                os.remove(src)
            except OSError:
                pass
            return dst
    except Exception as e:  # noqa: BLE001 - 转码失败不影响交付，保留 webm
        _log(f"webm→mp4 转码失败：{type(e).__name__}: {e}")
    return src


def _comfy_image(mgr, row, params, on_progress):
    """SD2.1（comfyui）与 157 的 Qwen-Image（comfyui_157）共用提交/轮询。

    i2i：params.frame 指定的图片先上传到 ComfyUI input，再走
    LoadImage → VAEEncode → KSampler(denoise<1)；t2i 走 EmptyLatentImage。
    """
    import asset_fill
    qwen = row["channel"] == "comfyui_157"
    url = asset_fill._comfy_image_url()
    frame = str(params.get("frame") or "").strip()
    steps = int(params.get("steps") or (8 if qwen else 20))
    if qwen:
        # 依赖模型名以 157 的 /object_info 为准，缺哪个就明确报错，不猜文件名
        info = _http_json(url + "/object_info", timeout=15)

        def _pick_model(node, field, keyword):
            v = (((info.get(node) or {}).get("input") or {}).get("required") or {}).get(field)
            cand = []
            if isinstance(v, list) and v:
                cand = v[0] if isinstance(v[0], list) else v
            names = [str(x) for x in cand if isinstance(x, str)]
            hit = next((n for n in names if keyword in n.lower()), "")
            if not hit:
                raise RuntimeError(f"ComfyUI 缺 {node}.{field}（需含 {keyword}），现有：{names[:5]}")
            return hit

        unet = _pick_model("UNETLoader", "unet_name", "qwen_image")
        clip = _pick_model("CLIPLoader", "clip_name", "qwen3vl_8b")
        vae = _pick_model("VAELoader", "vae_name", "qwen_image")
        vae_ref = ["3", 0]
        ks_id, save_id = "6", "8"
        graph = {
            "1": {"class_type": "UNETLoader",
                  "inputs": {"unet_name": unet, "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": clip, "type": "qwen_image"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
            "4": {"class_type": "TextEncodeQwenImage21",
                  "inputs": {"clip": ["2", 0], "prompt": row["prompt"],
                             "negative_prompt": params.get("negative")
                             or "blurry, low quality, watermark, extra limbs",
                             "resolution": 0}},
            ks_id: {"class_type": "KSampler",
                    "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 0],
                               "seed": _seed_of(params), "steps": steps,
                               "cfg": float(params.get("cfg") or 1.0),
                               "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                               "latent_image": ["5", 0]}},
            "7": {"class_type": "VAEDecode", "inputs": {"samples": [ks_id, 0], "vae": vae_ref}},
            save_id: {"class_type": "SaveImage",
                      "inputs": {"images": ["7", 0], "filename_prefix": "om_qwen"}},
        }
        note_name = f"Qwen-Image {unet}"
    else:
        ckpt = asset_fill._pick_checkpoint(url)
        vae_ref = ["1", 2]
        ks_id, save_id = "5", "7"
        graph = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
            "2": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": row["prompt"], "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": params.get("negative") or "低质量, 模糊, 水印, 变形",
                             "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": int(params.get("width") or 768),
                             "height": int(params.get("height") or 1024), "batch_size": 1}},
            ks_id: {"class_type": "KSampler",
                    "inputs": {"seed": _seed_of(params), "steps": steps,
                               "cfg": float(params.get("cfg") or 7.5),
                               "sampler_name": params.get("sampler") or "euler",
                               "scheduler": params.get("scheduler") or "normal", "denoise": 1.0,
                               "model": ["1", 0], "positive": ["2", 0],
                               "negative": ["3", 0], "latent_image": ["4", 0]}},
            "6": {"class_type": "VAEDecode", "inputs": {"samples": [ks_id, 0], "vae": vae_ref}},
            save_id: {"class_type": "SaveImage",
                      "inputs": {"images": ["6", 0], "filename_prefix": "opc-material"}},
        }
        note_name = f"ComfyUI {ckpt}"
    i2i = ""
    if frame:
        name = _comfy_upload_image(url, _safe_frame_path(frame))
        denoise = min(1.0, max(0.1, float(params.get("denoise") or 0.55)))
        graph["9"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        graph["10"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": vae_ref}}
        graph[ks_id]["inputs"]["latent_image"] = ["10", 0]
        graph[ks_id]["inputs"]["denoise"] = denoise
        graph.pop("4" if not qwen else "5", None)   # 空 latent 节点不再需要
        i2i = f" · i2i {name} denoise={denoise}"
    elif qwen:
        graph["5"] = {"class_type": "EmptySD3LatentImage",
                      "inputs": {"width": int(params.get("width") or 1024),
                                 "height": int(params.get("height") or 1024),
                                 "batch_size": 1}}
    port = urllib.parse.urlsplit(url).port or 8188
    resp = _http_json(url + "/prompt", timeout=20,
                      data={"prompt": graph, "client_id": f"opc-ops-{port}"})
    pid = resp.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI 拒绝作业：{resp.get('node_errors') or resp}")
    mgr._set(row["id"], remote_id=pid)
    on_progress(15)
    t0 = time.time()
    deadline = t0 + float(os.environ.get("OPC_COMFY_TIMEOUT", "900"))
    # ComfyUI 只有队列没有 HTTP 进度，按「模型预估耗时」给粗粒度进度（估算值写在 note）
    expect = max(45.0, steps * 6.0)
    while time.time() < deadline:
        _canceling(row["id"])
        hist = _http_json(f"{url}/history/{pid}", timeout=15)
        entry = hist.get(pid) or {}
        if entry.get("status", {}).get("status_str") == "error":
            raise RuntimeError("ComfyUI 作业执行失败（详见 ComfyUI 日志）")
        outs = (entry.get("outputs") or {}).get(save_id) or {}
        imgs = outs.get("images") or []
        if imgs:
            q = urllib.parse.urlencode({"filename": imgs[0]["filename"],
                                        "subfolder": imgs[0].get("subfolder", ""),
                                        "type": imgs[0].get("type", "output")})
            blob = _http(f"{url}/view?{q}", timeout=60)
            tmp = os.path.join(BASE_DIR, ".cache", "media", f"{row['id']}.png")
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(blob)
            on_progress(95)
            return {"path": tmp, "remote_id": pid,
                    "note": f"{note_name} · {round(time.time() - t0, 1)}s{i2i}"}
        on_progress(15 + int(min(75, max(0.0, (time.time() - t0) / expect) * 75)))
        time.sleep(2)
    # 超时：尽量中断远端执行，避免占着 ComfyUI 队列
    try:
        _http(url + "/interrupt", timeout=5, data=b"", method="POST",
              headers={"Content-Type": "application/json"})
    except Exception:
        pass
    raise TimeoutError("ComfyUI 出图超时")


def _comfy_video(mgr, row, params, on_progress):
    """Wan2.2 TI2V 5B：t2v 只吃提示词；i2v 额外把 params.frame 指定的首帧传进 ComfyUI。"""
    import asset_fill
    url = asset_fill._comfy_video_url()
    info = json.loads(_http(url + "/object_info", timeout=25).decode("utf-8"))
    unet = _pick_opt(info, "UNETLoader", "unet_name", r"ti2v")
    clip = _pick_opt(info, "CLIPLoader", "clip_name", r"umt5")
    vae = _pick_opt(info, "VAELoader", "vae_name", r"wan2\.2_vae")
    if not (unet and clip and vae):
        raise RuntimeError("ComfyUI 视频端点缺 Wan2.2 依赖模型"
                           f"（unet={unet!r} clip={clip!r} vae={vae!r}）")
    width = int(params.get("width") or 704)
    height = int(params.get("height") or 480)
    fps = int(params.get("fps") or 16)
    length = max(5, int(params.get("num_frames") or 49))
    length = ((length - 1) // 4) * 4 + 1          # Wan 要求 4k+1 帧
    steps = int(params.get("steps") or 20)
    graph = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "wan"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"text": row["prompt"], "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"text": params.get("negative") or "低质量, 模糊, 水印, 变形, 静止画面",
                         "clip": ["2", 0]}},
        "6": {"class_type": "Wan22ImageToVideoLatent",
              "inputs": {"vae": ["3", 0], "width": width, "height": height,
                         "length": length, "batch_size": 1}},
        "7": {"class_type": "KSampler",
              "inputs": {"seed": _seed_of(params), "steps": steps,
                         "cfg": float(params.get("cfg") or 5.0),
                         "sampler_name": params.get("sampler") or "euler",
                         "scheduler": params.get("scheduler") or "simple", "denoise": 1.0,
                         "model": ["1", 0], "positive": ["4", 0],
                         "negative": ["5", 0], "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveWEBM",
              "inputs": {"images": ["8", 0], "filename_prefix": "opc-shot",
                         "codec": "vp9", "fps": float(fps), "crf": 28.0}},
    }
    i2v = ""
    frame = str(params.get("frame") or "").strip()
    if frame:
        name = _comfy_upload_image(url, _safe_frame_path(frame))
        graph["10"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        graph["6"]["inputs"]["start_image"] = ["10", 0]
        i2v = f"首帧 {name}"
    on_progress(5)
    resp = _http_json(url + "/prompt", timeout=25,
                      data={"prompt": graph, "client_id": "opc-ops-video"})
    pid = resp.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI 拒绝作业：{resp.get('node_errors') or resp}")
    mgr._set(row["id"], remote_id=pid)
    t0 = time.time()
    deadline = t0 + float(os.environ.get("OPC_COMFY_VIDEO_TIMEOUT", "5400"))
    # ComfyUI 不给 HTTP 进度，按「帧数×步数」估个耗时给出粗粒度进度
    expect = max(180.0, length * steps * 0.9)
    while time.time() < deadline:
        _canceling(row["id"])
        hist = _http_json(f"{url}/history/{pid}", timeout=20)
        entry = hist.get(pid) or {}
        if entry.get("status", {}).get("status_str") == "error":
            raise RuntimeError("ComfyUI 视频作业失败（详见 8189 日志）")
        outs = (entry.get("outputs") or {}).get("9") or {}
        vids = outs.get("images") or outs.get("gifs") or []
        if vids:
            q = urllib.parse.urlencode({"filename": vids[0]["filename"],
                                        "subfolder": vids[0].get("subfolder", ""),
                                        "type": vids[0].get("type", "output")})
            blob = _http(f"{url}/view?{q}", timeout=300)
            src = os.path.join(BASE_DIR, ".cache", "media", f"{row['id']}.webm")
            os.makedirs(os.path.dirname(src), exist_ok=True)
            with open(src, "wb") as f:
                f.write(blob)
            on_progress(96)
            note = (f"Wan2.2 TI2V · {unet} · {width}×{height} · "
                    f"{length}帧@{fps}fps · {round(time.time() - t0, 1)}s")
            if i2v:
                note += f" · {i2v}"
            return {"path": _to_mp4(src), "remote_id": pid,
                    "duration": round(length / max(1, fps), 2), "note": note}
        on_progress(5 + int(min(85, max(0.0, (time.time() - t0) / expect) * 85)))
        time.sleep(3)
    try:  # 超时别白占 8189 的队列
        _http(url + "/interrupt", timeout=5, data=b"", method="POST",
              headers={"Content-Type": "application/json"})
    except Exception:
        pass
    raise TimeoutError("ComfyUI 视频超时（CPU 推理较慢，可在 8189 日志看进度）")


def _bailian_image(mgr, row, params, on_progress):
    """百炼 qwen-image-3.0-pro：DashScope multimodal-generation，同步返回 PNG URL 后下载。

    图生图把 params.frame 的图片读成 data URI 内联进 messages（服务端是内网，
    给公网 URL 反而不可达）；最多 3 张参考图，这里传 1 张。
    """
    key = _bailian_api_key()
    if not key:
        raise RuntimeError("百炼 API Key 未配置（OPC_BAILIAN_TTS_API_KEY）")
    content = []
    frame = str(params.get("frame") or "").strip()
    if frame:
        path = _safe_frame_path(frame)
        ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
        if ext == "jpg":
            ext = "jpeg"
        if ext not in ("png", "jpeg", "webp", "bmp", "tiff", "gif"):
            raise ValueError(f"百炼不支持的图片格式：{ext}")
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        content.append({"image": f"data:image/{ext};base64,{b64}"})
    content.append({"text": row["prompt"]})
    body = {
        "model": BAILIAN_IMAGE_MODEL,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": {
            "size": str(params.get("size") or "1024*1024"),
            "n": int(params.get("n") or 1),
            "prompt_extend": str(params.get("prompt_extend") or "true").lower() == "true",
            "watermark": str(params.get("watermark") or "false").lower() == "true",
        },
    }
    on_progress(20)
    res = _http_json(BAILIAN_IMAGE_BASE + BAILIAN_IMAGE_PATH, timeout=300, data=body,
                     headers={"Authorization": "Bearer " + key})
    if not isinstance(res, dict):
        raise RuntimeError("百炼图像返回格式异常")
    choices = ((res.get("output") or {}).get("choices") or [])
    parts = ((choices[0].get("message") or {}).get("content") or []) if choices else []
    url = ""
    for part in parts:
        if isinstance(part, dict) and part.get("image"):
            url = str(part["image"])
            break
    if not url:
        raise RuntimeError(f"百炼图像未返回图片 URL：{str(res)[:200]}")
    on_progress(70)
    tmp = os.path.join(BASE_DIR, ".cache", "media", f"{row['id']}.png")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(_http(url, timeout=180))
    on_progress(95)
    return {"path": tmp, "duration": 0.0,
            "remote_id": str(res.get("request_id") or ""),
            "note": f"阿里云百炼 · {BAILIAN_IMAGE_MODEL} · {body['parameters']['size']}"
                    + (" · i2i" if frame else "")}


def _mediagen_image(mgr, row, params, on_progress):
    body = {"prompt": row["prompt"], "negative_prompt": params.get("negative_prompt"),
            "width": int(params.get("width") or 640), "height": int(params.get("height") or 832),
            "steps": int(params.get("steps") or 20), "cfg": float(params.get("cfg") or 6.0),
            "seed": _seed_of(params), "backend": "sd15", "wait": False}
    ack = _http_json(MEDIAGEN + "/v1/image/jobs", timeout=20, data=body)
    return _poll_mediagen(mgr, row, ack, on_progress)


def _mediagen_video(mgr, row, params, on_progress):
    body = {"prompt": row["prompt"], "negative_prompt": params.get("negative_prompt"),
            "width": int(params.get("width") or 832), "height": int(params.get("height") or 480),
            "num_frames": int(params.get("num_frames") or 33),
            "steps": int(params.get("steps") or 20), "cfg": float(params.get("cfg") or 5.0),
            "fps": int(params.get("fps") or 16), "seed": _seed_of(params),
            "backend": row["channel"], "wait": False}
    ack = _http_json(MEDIAGEN + "/v1/video/jobs", timeout=20, data=body)
    return _poll_mediagen(mgr, row, ack, on_progress)


def _poll_mediagen(mgr, row, ack, on_progress):
    jid = ack.get("id") or ack.get("job_id")
    if not jid:
        raise RuntimeError(f"media-gen 拒绝作业：{ack}")
    # 立刻落远端作业号：进程重启后靠它回查 media-gen 取回成品，不能再等成功时才写
    if mgr is not None:
        mgr._set(row["id"], remote_id=jid)
    on_progress(10)
    deadline = time.time() + float(os.environ.get("OPC_MEDIAGEN_TIMEOUT", "14400"))
    while time.time() < deadline:
        _canceling(row["id"])
        v = _http_json(f"{MEDIAGEN}/v1/jobs/{jid}", timeout=15)
        st = str(v.get("status") or "")
        prog = v.get("progress") or {}
        step, total = int(prog.get("step") or 0), int(prog.get("total") or 0)
        if total > 0:
            on_progress(10 + int(85 * min(1.0, step / total)))
        if st == "succeeded":
            dur = round(params_sec(v), 2) if row["kind"] == "video" else 0
            path = v.get("output_path") or ""
            if path and os.path.isfile(path):
                return {"path": path, "remote_id": jid, "duration": dur}
            url = v.get("output_url") or ""
            if url:
                tmp = os.path.join(BASE_DIR, ".cache", "media",
                                   f"{row['id']}{os.path.splitext(url)[1] or '.bin'}")
                os.makedirs(os.path.dirname(tmp), exist_ok=True)
                with open(tmp, "wb") as f:
                    f.write(_http(MEDIAGEN + url, timeout=120))
                return {"path": tmp, "remote_id": jid, "duration": dur}
            raise RuntimeError("media-gen 作业成功但没有产物路径")
        if st in ("failed", "interrupted"):
            raise RuntimeError(v.get("error") or f"media-gen 作业{st}")
        time.sleep(2)
    raise TimeoutError("media-gen 作业超时（可在运维页看队列）")


def params_sec(view):
    """media-gen 视频结果里 num_frames/fps 换算真实时长；无结果返回 0。"""
    try:
        p = view.get("params") or {}
        r = view.get("result") or {}
        nf = int(r.get("num_frames") or p.get("num_frames") or 0)
        fps = int(r.get("fps") or p.get("fps") or 16)
        return nf / fps if nf and fps else 0
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 音色克隆
# CosyVoice 的「音色」本质就是一条 zero-shot 参考（prompt_wav + prompt_text），
# 内置 25 个音色就是这么登记的。所以克隆 = 把用户音频转成 16k 单声道 wav 落盘，
# 再往共享的 voices.json 追加一条记录，media-engine 下次合成即认。
_VOICES_CACHE = {"at": 0.0, "data": None}


def _ffbin(name):
    p = shutil.which(name) or os.path.expanduser(f"~/.local/bin/{name}")
    return p if p and os.path.isfile(p) else ""


def _audio_sec(path):
    """读音频时长（秒），读不到返回 0，用于校验参考音频是否够长。"""
    ff = _ffbin("ffprobe")
    if not ff or not path or not os.path.isfile(path):
        return 0.0
    try:
        r = subprocess.run([ff, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, timeout=60)
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _slug(name):
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "", str(name or "").replace(" ", "-"))
    return s.strip("-_")[:24] or "myvoice"


@contextlib.contextmanager
def _voices_guard():
    """voices.json 是 media-engine、hermes、本工具共用的表，改动必须加锁 + 原子替换。"""
    os.makedirs(os.path.dirname(TTS_VOICES_JSON) or ".", exist_ok=True)
    with open(TTS_VOICES_JSON + ".lock", "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_voices_cfg():
    with open(TTS_VOICES_JSON, encoding="utf-8") as f:
        return json.load(f)


def _write_voices_cfg(cfg):
    """临时文件 + os.replace：media-engine 可能正在读，绝不能让它读到半截 JSON。"""
    tmp = TTS_VOICES_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o664)
    os.replace(tmp, TTS_VOICES_JSON)


def voices_list(force=False):
    """音色清单（含克隆标记）。内置音色不测时长，只有克隆的才算，避免每次探测都跑 ffprobe。"""
    now = time.time()
    if not force and _VOICES_CACHE["data"] and now - _VOICES_CACHE["at"] < 20:
        return _VOICES_CACHE["data"]
    try:
        cfg = _read_voices_cfg()
    except Exception as e:  # noqa: BLE001 - 表坏了不该让整个工具箱挂掉
        _log(f"读音色表失败：{type(e).__name__}: {e}")
        return {"default": "", "voices": []}
    items = []
    for vid, v in (cfg.get("voices") or {}).items():
        cloned = vid.startswith(CLONE_PREFIX)
        items.append({
            "id": vid,
            "description": v.get("description") or "",
            "prompt_text": v.get("prompt_text") or "",
            "cloned": cloned,
            "sec": round(_audio_sec(v.get("prompt_wav") or ""), 2) if cloned else 0,
            "rounds": len((_read_profile(vid) or {}).get("rounds") or []) if cloned else 0,
        })
    data = {"default": cfg.get("default") or "", "voices": items}
    _VOICES_CACHE.update({"at": now, "data": data})
    return data


def voice_options():
    """给渠道下拉用的音色 id 列表（克隆音色排在内置之后）。"""
    vs = (voices_list() or {}).get("voices") or []
    return [v["id"] for v in vs]


def _clone_sample_path(vid):
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "", str(vid or ""))[:40] or "clone"
    return os.path.join(BASE_DIR, ".cache", "media", f"clone-sample-{safe}.wav")


def _probe_clone(vid):
    """克隆完立刻试合成一句：既验证音色可用，也把这段样本留给用户当场试听。
    返回 (状态, 说明, 样本路径)：ok / silent（文本不匹配，出空音）/ error（引擎问题）。
    """
    try:
        res = _http_json(MEDIAENGINE + "/v1/tts", timeout=420,
                         data={"text": "这是一段用来确认音色可用的试听，你听到的就是克隆后的声音。",
                               "engine": "cosyvoice", "voice": vid, "speed": 1.0,
                               "mode": "sync", "format": "json"})
    except Exception as e:  # noqa: BLE001 - 引擎抖动不该否定用户刚建的音色
        return "error", f"试合成未验证（{type(e).__name__}），音色已保存，可直接选它配音", ""
    apath = str(res.get("audio_path") or "")
    try:
        dur = float(res.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur < 1.0:
        return "silent", f"试合成仅 {dur:.2f}s", apath
    return "ok", "", apath


# ---------------------------------------------------------------- 音色微调
# zero-shot 没有「训练」这一步，参考音频本身就决定了音色。所以微调 = 在上一版参考上
# 按选定方向做一次确定性的声学处理（音高 / 语速 / 明亮度 / 轻重），重新登记成新参考，
# 就等于往那个方向挪了一小步。每轮都做真合成自检 + 可试听 + 可撤销，于是能一轮轮逼近。
TUNE_DIRECTIONS = {
    "deeper":   {"label": "更低沉",   "pitch": 0.950, "tempo": 1.00, "volume": 1.00, "extra": ""},
    "brighter": {"label": "更明亮",   "pitch": 1.050, "tempo": 1.00, "volume": 1.00, "extra": ""},
    "younger":  {"label": "更年轻",   "pitch": 1.030, "tempo": 1.02, "volume": 1.00, "extra": ""},
    "older":    {"label": "更成熟",   "pitch": 0.975, "tempo": 0.99, "volume": 1.00, "extra": ""},
    "warm":     {"label": "更温柔",   "pitch": 0.990, "tempo": 0.97, "volume": 0.88,
                 "extra": "lowpass=f=7200"},
    "strong":   {"label": "更有力",   "pitch": 1.010, "tempo": 1.03, "volume": 1.20,
                 "extra": "acompressor=threshold=0.12:ratio=3:attack=8:release=180"},
    "faster":   {"label": "语速更快", "pitch": 1.000, "tempo": 1.08, "volume": 1.00, "extra": ""},
    "slower":   {"label": "语速更慢", "pitch": 1.000, "tempo": 0.93, "volume": 1.00, "extra": ""},
}
TUNE_MAX_ROUNDS = 6
_TUNE_PITCH_RANGE = (0.86, 1.16)
_TUNE_TEMPO_RANGE = (0.86, 1.18)


def _profile_path(vid):
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "", str(vid or ""))[:40] or "clone"
    return os.path.join(BASE_DIR, ".cache", "media", f"voice-profile-{safe}.json")


def _read_profile(vid):
    """音色档案：初始参考 + 每轮微调记录（不放 voices.json，免得污染共享表）。"""
    try:
        with open(_profile_path(vid), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_profile(vid, prof):
    p = _profile_path(vid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prof, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _profile_current(prof):
    """当前生效的参考：最后一轮的产物，没调过就是初始参考。"""
    rounds = (prof or {}).get("rounds") or []
    if rounds:
        return rounds[-1]
    return (prof or {}).get("base") or {}


def _transcode_ref(raw):
    """任意音频字节 → 16k 单声道 wav（截到 CLONE_MAX_SEC），返回落盘路径。"""
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("参考音频超过 20MB，请截取 5–15 秒的片段再上传")
    ff = _ffbin("ffmpeg")
    if not ff:
        raise RuntimeError("本机缺少 ffmpeg，无法把参考音频统一转成 16k 单声道 wav")
    os.makedirs(TTS_REF_DIR, exist_ok=True)
    fd, src = tempfile.mkstemp(suffix=".raw", dir=TTS_REF_DIR)
    os.close(fd)
    wav = os.path.join(TTS_REF_DIR, f"{CLONE_PREFIX}{uuid.uuid4().hex[:10]}.wav")
    try:
        with open(src, "wb") as f:
            f.write(raw)
        r = subprocess.run([ff, "-y", "-loglevel", "error", "-i", src, "-vn",
                            "-ac", "1", "-ar", "16000", "-t", str(int(CLONE_MAX_SEC)), wav],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not os.path.isfile(wav) or os.path.getsize(wav) < 1024:
            msg = (r.stderr or "").strip().splitlines()
            with contextlib.suppress(OSError):
                os.remove(wav)
            raise ValueError("音频无法解析（请上传 wav / mp3 / m4a / ogg / webm）"
                             + (f"：{msg[-1][:120]}" if msg else ""))
    finally:
        if os.path.exists(src):
            os.remove(src)
    return wav


def _check_ref(wav, text):
    """参考文本与录音时长必须对得上，否则 zero-shot 只会产出几乎无声的音频。"""
    sec = _audio_sec(wav)
    if sec < CLONE_MIN_SEC:
        raise ValueError(f"参考音频只有 {sec:.1f}s，太短：请提供 3–15 秒连续说话的人声")
    ratio = len(text) / max(0.8, sec)
    if ratio < 1.0 or ratio > 12.0:
        raise ValueError(
            f"参考文本和录音时长对不上：{sec:.1f}s 的录音通常 {max(3, int(sec * 2.5))}–{int(sec * 6)} 个字，"
            f"你填了 {len(text)} 个字。请只填这段录音里逐字说的话（不要写提示词或概括）")
    return sec


def _apply_direction(src_wav, d, out_wav):
    """按方向处理参考音频：音高用 asetrate + atempo 反向抵消，时长不变，语速用 atempo。"""
    ff = _ffbin("ffmpeg")
    if not ff:
        raise RuntimeError("本机缺少 ffmpeg，无法做音色微调")
    p = float(d.get("pitch") or 1.0)
    t = float(d.get("tempo") or 1.0)
    v = float(d.get("volume") or 1.0)
    chain = []
    if abs(p - 1.0) > 1e-4:
        chain.append(f"asetrate={int(round(16000 * p))},aresample=16000")
        chain.append(f"atempo={1.0 / p:.6f}")
    if abs(t - 1.0) > 1e-4:
        chain.append(f"atempo={t:.6f}")
    if abs(v - 1.0) > 1e-4:
        chain.append(f"volume={v:.3f}")
    if d.get("extra"):
        chain.append(str(d["extra"]))
    chain += ["aresample=16000", "aformat=channel_layouts=mono"]
    r = subprocess.run([ff, "-y", "-loglevel", "error", "-i", src_wav, "-vn",
                        "-af", ",".join(chain), "-ac", "1", "-ar", "16000", out_wav],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.isfile(out_wav) or os.path.getsize(out_wav) < 1024:
        msg = (r.stderr or "").strip().splitlines()
        with contextlib.suppress(OSError):
            os.remove(out_wav)
        raise ValueError("音色微调失败（ffmpeg 处理参考音频出错）"
                         + (f"：{msg[-1][:120]}" if msg else ""))
    return out_wav


def _cleanup_wav(path):
    """只删参考目录里的文件，别的一概不动。"""
    ref = os.path.abspath(TTS_REF_DIR)
    p = str(path or "")
    if p and os.path.abspath(p).startswith(ref + os.sep) and os.path.isfile(p):
        with contextlib.suppress(OSError):
            os.remove(p)


def _save_sample(vid, apath):
    """把试合成样本留作试听（比参考录音更接近实际配音效果）。"""
    if not (apath and os.path.isfile(apath)):
        return False
    try:
        os.makedirs(os.path.dirname(_clone_sample_path(vid)), exist_ok=True)
        shutil.copyfile(apath, _clone_sample_path(vid))
        return True
    except OSError:
        return False


def clone_voice(raw, prompt_text, name="", owner=""):
    """上传参考音频 → 16k 单声道 wav → 注册进 voices.json。返回新音色信息。"""
    if not raw:
        raise ValueError("没有收到音频数据（请上传一段 5–15 秒的干净人声）")
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("参考音频超过 20MB，请截取 5–15 秒的片段再上传")
    text = str(prompt_text or "").strip()
    if not text:
        raise ValueError("必须填写「参考文本」：这段录音里说的原话，CosyVoice 靠它对齐音色")
    if len(text) > 200:
        raise ValueError("参考文本只填这段录音里的原话（200 字以内），不要写提示词")
    wav = _transcode_ref(raw)
    # zero-shot 靠 prompt_text 与录音对齐，文本写错会生成几乎无声的音频，
    # 所以先用「字数/时长」做一次粗筛，别等合成出来才发现（实测 3.5s 却填 2 个字 → 0.04s 空音）。
    try:
        sec = _check_ref(wav, text)
    except ValueError:
        _cleanup_wav(wav)
        raise

    with _voices_guard():
        cfg = _read_voices_cfg()
        voices = cfg.setdefault("voices", {})
        base = CLONE_PREFIX + _slug(name)
        vid, i = base, 1
        while vid in voices:
            i += 1
            vid = f"{base}-{i}"
        desc = f"克隆音色 · {name.strip()[:20] or vid}｜参考 {sec:.1f}s" + (f"｜{owner}" if owner else "")
        voices[vid] = {"description": desc, "prompt_wav": wav, "prompt_text": text}
        _write_voices_cfg(cfg)
    _VOICES_CACHE.update({"at": 0.0, "data": None})
    # 最后一步自检：真合成一句。参考文本写错时 zero-shot 会「成功」返回一段不足 0.5s
    # 的空音，用户拿到手只会以为坏了，所以当场拦下并回滚；正常就把这段样本留作试听。
    state, why, apath = _probe_clone(vid)
    if state == "silent":
        with contextlib.suppress(Exception):
            delete_clone_voice(vid)
        raise ValueError("克隆失败：用新音色试合成只得到几乎没有声音的音频。多半是「参考文本」"
                         "和录音内容不一致 —— 请逐字照抄录音里说的话再试")
    sample = _save_sample(vid, apath)
    # 初始参考存档：后面每一轮微调都在它之上叠加，撤销也靠它回到原声
    with contextlib.suppress(OSError):
        _write_profile(vid, {"id": vid, "created": time.time(),
                             "base": {"wav": wav, "text": text, "sec": round(sec, 2)},
                             "rounds": []})
    return {"id": vid, "sec": round(sec, 2), "description": desc, "prompt_text": text,
            "sample": sample, "warning": why if state == "error" else "", "rounds": 0}


def tune_clone_voice(vid, direction="", raw=b"", prompt_text=""):
    """按方向微调一轮：改参考音频 → 重新登记 → 真合成自检 → 留样本；失败自动还原。"""
    vid = str(vid or "").strip()
    if not vid.startswith(CLONE_PREFIX):
        raise ValueError("只能微调自己克隆的音色（clone- 开头）")
    key = str(direction or "").strip().lower()
    d = TUNE_DIRECTIONS.get(key)
    if not d and not raw:
        raise ValueError("请选择一个微调方向（或重新录一段参考音频再微调）")
    with _voices_guard():
        cfg = _read_voices_cfg()
        v = (cfg.get("voices") or {}).get(vid)
    if not v:
        raise ValueError(f"音色不存在：{vid}")
    prof = _read_profile(vid)
    if not prof:
        # 老音色没有档案：用当前登记值当第 0 轮，保证还能继续微调
        prof = {"id": vid, "created": time.time(),
                "base": {"wav": str(v.get("prompt_wav") or ""),
                         "text": str(v.get("prompt_text") or "")},
                "rounds": []}
    rounds = prof.get("rounds") or []
    if len(rounds) >= TUNE_MAX_ROUNDS:
        raise ValueError(f"这个音色已经微调 {len(rounds)} 轮，再叠加只会越来越糊："
                         f"建议拿一段更理想的录音重建音色（或先撤销几轮）")
    cur = _profile_current(prof)
    old_wav = str(cur.get("wav") or "")
    old_text = str(cur.get("text") or "")
    if not old_wav or not os.path.isfile(old_wav):
        raise ValueError("当前参考音频已不存在，无法继续微调")
    cum_p = 1.0
    cum_t = 1.0
    for r in rounds:
        cum_p *= float(r.get("pitch") or 1.0)
        cum_t *= float(r.get("tempo") or 1.0)
    if d:
        np_, nt = cum_p * float(d["pitch"]), cum_t * float(d["tempo"])
        if not (_TUNE_PITCH_RANGE[0] <= np_ <= _TUNE_PITCH_RANGE[1]):
            raise ValueError(f"音高已经到极限（累计 {np_:.2f}×）：再往「{d['label']}」调只会失真，"
                             f"建议换个方向或先撤销一轮")
        if not (_TUNE_TEMPO_RANGE[0] <= nt <= _TUNE_TEMPO_RANGE[1]):
            raise ValueError(f"语速已经到极限（累计 {nt:.2f}×）：再往「{d['label']}」调只会失真，"
                             f"建议换个方向或先撤销一轮")

    new_wav = ""
    new_text = old_text
    try:
        if raw:
            new_wav = _transcode_ref(raw)
            new_text = str(prompt_text or "").strip()
            if not new_text:
                raise ValueError("换新录音就要同时填这段录音的「参考文本」")
            _check_ref(new_wav, new_text)
            if d:
                tuned = os.path.join(TTS_REF_DIR, f"{CLONE_PREFIX}tune-{uuid.uuid4().hex[:10]}.wav")
                _apply_direction(new_wav, d, tuned)
                _cleanup_wav(new_wav)          # 中间产物不留
                new_wav = tuned
        else:
            new_wav = os.path.join(TTS_REF_DIR, f"{CLONE_PREFIX}tune-{uuid.uuid4().hex[:10]}.wav")
            _apply_direction(old_wav, d, new_wav)
    except Exception:
        _cleanup_wav(new_wav)
        raise

    with _voices_guard():
        cfg = _read_voices_cfg()
        voices = cfg.get("voices") or {}
        if vid not in voices:
            _cleanup_wav(new_wav)
            raise ValueError(f"音色不存在：{vid}")
        desc = voices[vid].get("description") or vid
        voices[vid] = {"description": desc, "prompt_wav": new_wav, "prompt_text": new_text}
        _write_voices_cfg(cfg)
    _VOICES_CACHE.update({"at": 0.0, "data": None})

    state, why, apath = _probe_clone(vid)
    if state == "silent":
        # 回滚到微调前：坏音色不能留在下拉里
        with _voices_guard():
            cfg = _read_voices_cfg()
            if vid in (cfg.get("voices") or {}):
                cfg["voices"][vid] = {"description": (cfg["voices"][vid].get("description") or vid),
                                      "prompt_wav": old_wav, "prompt_text": old_text}
                _write_voices_cfg(cfg)
        _VOICES_CACHE.update({"at": 0.0, "data": None})
        _cleanup_wav(new_wav)
        raise ValueError("这次微调没通过自检（试合成几乎没声音），已还原到上一版："
                         "换个方向、或换一段更清晰的参考录音再试")

    label = d["label"] if d else "换参考录音"
    prof["rounds"] = rounds + [{
        "n": len(rounds) + 1, "at": round(time.time(), 1),
        "direction": key or "re-record", "label": label,
        "pitch": float(d["pitch"]) if d else 1.0,
        "tempo": float(d["tempo"]) if d else 1.0,
        "wav": new_wav, "text": new_text,
        "sec": round(_audio_sec(new_wav), 2),
    }]
    with contextlib.suppress(OSError):
        _write_profile(vid, prof)
    sample = _save_sample(vid, apath)
    return {"id": vid, "rounds": len(prof["rounds"]), "label": label,
            "sec": round(_audio_sec(new_wav), 2), "sample": sample,
            "warning": why if state == "error" else ""}


def undo_tune(vid):
    """撤销最后一轮微调，回到上一版参考。"""
    vid = str(vid or "").strip()
    if not vid.startswith(CLONE_PREFIX):
        raise ValueError("只能撤销克隆音色的微调")
    prof = _read_profile(vid)
    rounds = (prof or {}).get("rounds") or []
    if not rounds:
        raise ValueError("这个音色还没有微调记录")
    last = rounds[-1]
    keep = rounds[:-1]
    cur = keep[-1] if keep else (prof.get("base") or {})
    wav, text = str(cur.get("wav") or ""), str(cur.get("text") or "")
    if not wav or not os.path.isfile(wav):
        raise ValueError("上一版参考音频已不存在，无法撤销")
    with _voices_guard():
        cfg = _read_voices_cfg()
        voices = cfg.get("voices") or {}
        if vid not in voices:
            raise ValueError(f"音色不存在：{vid}")
        voices[vid] = {"description": voices[vid].get("description") or vid,
                       "prompt_wav": wav, "prompt_text": text}
        _write_voices_cfg(cfg)
    _VOICES_CACHE.update({"at": 0.0, "data": None})
    prof["rounds"] = keep
    with contextlib.suppress(OSError):
        _write_profile(vid, prof)
    _cleanup_wav(last.get("wav"))
    state, why, apath = _probe_clone(vid)
    if state != "silent":
        _save_sample(vid, apath)
    return {"id": vid, "rounds": len(keep), "undone": last.get("label") or "",
            "warning": why if state == "error" else ""}


def voice_profile(vid):
    """微调档案：可选方向 + 已调轮次，前端据此渲染「微调」面板。"""
    vid = str(vid or "").strip()
    if not vid.startswith(CLONE_PREFIX):
        raise ValueError("只能查看克隆音色的微调档案")
    prof = _read_profile(vid) or {}
    rounds = prof.get("rounds") or []
    cur = _profile_current(prof)
    return {
        "id": vid,
        "count": len(rounds),
        "maxRounds": TUNE_MAX_ROUNDS,
        "rounds": [{"n": r.get("n"), "label": r.get("label") or "",
                    "direction": r.get("direction") or "",
                    "sec": r.get("sec") or 0} for r in rounds],
        "directions": [{"key": k, "label": v["label"]} for k, v in TUNE_DIRECTIONS.items()],
        "current": {"text": cur.get("text") or "",
                    "sec": round(_audio_sec(cur.get("wav") or ""), 2)},
    }


def delete_clone_voice(vid):
    """只允许删 clone- 前缀的音色，内置音色一个都不能动。"""
    vid = str(vid or "").strip()
    if not vid.startswith(CLONE_PREFIX):
        raise ValueError("只能删除自己克隆的音色（clone- 开头）")
    with _voices_guard():
        cfg = _read_voices_cfg()
        voices = cfg.get("voices") or {}
        if vid not in voices:
            raise ValueError(f"音色不存在：{vid}")
        v = voices.pop(vid)
        _write_voices_cfg(cfg)
    _VOICES_CACHE.update({"at": 0.0, "data": None})
    wav = str(v.get("prompt_wav") or "")
    ref = os.path.abspath(TTS_REF_DIR)
    if wav and os.path.abspath(wav).startswith(ref + os.sep) and os.path.isfile(wav):
        try:
            os.remove(wav)
        except OSError:
            pass
    # 微调轮次的中间 wav 与档案一起清掉，别在共享参考目录里留垃圾
    prof = _read_profile(vid) or {}
    for r in prof.get("rounds") or []:
        _cleanup_wav(r.get("wav"))
    _cleanup_wav((prof.get("base") or {}).get("wav"))
    with contextlib.suppress(OSError):
        os.remove(_profile_path(vid))
    with contextlib.suppress(OSError):
        os.remove(_clone_sample_path(vid))
    return {"id": vid}


def clone_ref_path(vid):
    """试听用：优先返回克隆后试合成的样本（更像实际配音效果），没有则退回参考录音。"""
    vid = str(vid or "").strip()
    if not vid.startswith(CLONE_PREFIX):
        raise ValueError("只能试听克隆音色")
    sample = _clone_sample_path(vid)
    if os.path.isfile(sample):
        return sample
    cfg = _read_voices_cfg()
    v = (cfg.get("voices") or {}).get(vid) or {}
    wav = str(v.get("prompt_wav") or "")
    ref = os.path.abspath(TTS_REF_DIR)
    if not wav or not os.path.abspath(wav).startswith(ref + os.sep) or not os.path.isfile(wav):
        raise ValueError("该音色的参考音频已不存在")
    return wav


# 试听：固定短句、按 (渠道, 音色, 文本) 缓存，避免反复烧本机算力 / 百炼额度
PREVIEW_TEXT = "大家好，这是音色试听。愿每一句话，都恰到好处。"
VOICE_PREVIEW_DIR = os.path.join(BASE_DIR, ".cache", "voice-preview")


def voice_preview(channel, voice, text=""):
    """按渠道+音色合成一小段 wav 试听音频，返回可 inline 播放的本地路径。"""
    name = str(channel or "")
    ch = CHANNELS.get(name)
    if not ch or ch.get("kind") != "audio":
        raise ValueError("不是可用的语音加工渠道")
    vid = str(voice or "").strip()
    if not vid:
        raise ValueError("请选择音色")
    specs = {p["key"]: p for p in (ch.get("params") or [])}
    vkey = "speaker" if "speaker" in specs else ("voice" if "voice" in specs else "")
    if not vkey:
        raise ValueError("该渠道没有音色参数")
    if name != "cosyvoice":
        opts = specs[vkey].get("options") or []
        if opts and vid not in opts:
            raise ValueError(f"音色 {vid} 不属于渠道 {name}")
    text = (text or PREVIEW_TEXT)[:100]
    digest = hashlib.md5(f"{name}|{vid}|{text}".encode("utf-8")).hexdigest()[:16]
    path = os.path.join(VOICE_PREVIEW_DIR, f"{name}-{digest}.wav")
    if os.path.isfile(path) and os.path.getsize(path) > 1024:
        return path
    params = {p["key"]: p.get("default") for p in (ch.get("params") or [])}
    params[vkey] = vid
    if name == "bailian":
        params["format"] = "wav"  # 试听接口固定 inline wav，别让 mp3 走 audio/wav 头
    row = {"id": f"preview-{digest}", "prompt": text, "channel": name, "kind": "audio"}
    res = _tts(None, row, params, lambda _p: None)
    os.makedirs(VOICE_PREVIEW_DIR, exist_ok=True)
    shutil.copyfile(res["path"], path)
    return path


def _tts_bailian(mgr, row, params, on_progress):
    """阿里云百炼 Token Plan 语音合成（云服务，同步返回音频 URL 后立即下载）。

    POST <base>/api/v1/services/audio/tts/SpeechSynthesizer
      {"model": ..., "input": {"text", "voice", "format", "sample_rate"}}
    -> {"output": {"audio": {"url": ..., "expires_at": ...}}, "usage": {...}}
    音频 URL 有时效，拿到立刻下载；Key 只走请求头，不落库、不写日志。
    """
    key = _bailian_api_key()
    if not key:
        raise RuntimeError("百炼 API Key 未配置（OPC_BAILIAN_TTS_API_KEY）")
    fmt = str(params.get("format") or "mp3").lower()
    if fmt not in ("mp3", "wav", "pcm", "opus"):
        fmt = "mp3"
    voice = str(params.get("voice") or "longanhuan_v3.6")
    body = {"model": BAILIAN_TTS_MODEL,
            "input": {"text": row["prompt"], "voice": voice, "format": fmt,
                      "sample_rate": int(float(params.get("sample_rate") or 22050))}}
    on_progress(20)
    res = _http_json(BAILIAN_TTS_BASE + BAILIAN_TTS_PATH, timeout=180, data=body,
                     headers={"Authorization": "Bearer " + key})
    if not isinstance(res, dict):
        raise RuntimeError("百炼 TTS 返回格式异常")
    audio = ((res.get("output") or {}).get("audio") or {}) if isinstance(res.get("output"), dict) else {}
    url = str(audio.get("url") or "")
    if not url:
        raise RuntimeError(f"百炼 TTS 未返回音频 URL：{str(res)[:200]}")
    on_progress(70)
    tmp = os.path.join(BASE_DIR, ".cache", "media", f"{row['id']}.{fmt}")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(_http(url, timeout=180))
    on_progress(95)
    return {"path": tmp, "duration": float(audio.get("duration") or 0),
            "remote_id": str(res.get("request_id") or ""),
            "note": f"阿里云百炼 · {BAILIAN_TTS_MODEL} · {voice}"}


def _tts(mgr, row, params, on_progress):
    if row["channel"] == "bailian":
        return _tts_bailian(mgr, row, params, on_progress)
    engine = "qwen3" if row["channel"] == "qwen" else "cosyvoice"
    body = {"text": row["prompt"], "engine": engine, "mode": "sync", "format": "json",
            "speed": float(params.get("speed") or 1.0)}
    if engine == "qwen3":
        body.update({"speaker": params.get("speaker") or "Vivian",
                     "language": params.get("language") or "zh",
                     "instruct": params.get("instruct") or None})
    else:
        body.update({"voice": params.get("voice") or "female-zh-real"})
    on_progress(20)
    res = _http_json(MEDIAENGINE + "/v1/tts", timeout=600, data=body)
    if not isinstance(res, dict):
        raise RuntimeError("TTS 返回格式异常")
    if res.get("audio_path") and os.path.isfile(res["audio_path"]):
        on_progress(95)
        return {"path": res["audio_path"], "duration": float(res.get("duration") or 0),
                "remote_id": str(res.get("job_id") or ""),
                "note": f"{engine} · {res.get('voice') or params.get('speaker') or ''}"}
    if res.get("audio_url"):
        tmp = os.path.join(BASE_DIR, ".cache", "media", f"{row['id']}.wav")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(_http(MEDIAENGINE + res["audio_url"], timeout=120))
        on_progress(95)
        return {"path": tmp, "duration": float(res.get("duration") or 0),
                "remote_id": str(res.get("job_id") or ""),
                "note": f"{engine} · {res.get('voice') or params.get('speaker') or ''}"}
    raise RuntimeError(f"TTS 未产出音频：{str(res)[:200]}")


def _private_url(dest):
    """成品落在 conring/users/<手机号>/ 下时给 /private/<手机号>/...，否则返回空串。

    工具箱成品是本人私有产物，不能落 /vault 那条免登录通道。
    """
    try:
        rel = os.path.relpath(dest, USERS_DIR)
    except ValueError:
        return ""
    if os.path.isabs(rel) or rel == ".." or rel.startswith(".." + os.sep):
        return ""
    parts = rel.split(os.sep)
    # 与 server.PRIVATE_SUBS 保持一致：库里文件（opc.db 等）绝不能拼出可访问的链接
    if len(parts) < 2 or not parts[0].isdigit() or parts[1] not in ("工具箱", "obsidian", "downloads"):
        return ""
    return "/private/" + "/".join([parts[0]] + [urllib.parse.quote(p) for p in parts[1:]])


def _deliver(mgr, row, result):
    src = (result or {}).get("path") or ""
    if not src or not os.path.isfile(src):
        raise RuntimeError("成品文件不存在")
    sub = {"image": "images", "video": "videos", "audio": "audio"}[row["kind"]]
    out_dir = row.get("out_dir") or os.path.join(BASE_DIR, ".cache", "media")
    vault = row.get("vault_dir") or ""
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(src)[1].lower() or {"image": ".png", "video": ".mp4", "audio": ".wav"}[row["kind"]]
    stem = f"shot{int(row.get('shot_no') or 0):02d}-{row['channel']}-r{int(row.get('run_no') or 1)}"
    # 工具箱手工任务没有分镜号，用 trace 后缀命名，避免一堆 shot00 分不清
    if str(row.get("trace_id") or "").startswith("tb-"):
        stem = f"tool-{row['channel']}-{str(row['trace_id'])[3:]}"
    dest = os.path.join(out_dir, stem + ext)
    # 手工任务同渠道重复提交会撞名（流水线靠 shot_no 区分，工具箱没有分镜号），
    # 撞名时追加序号，绝不覆盖已有成品
    if os.path.exists(dest):
        n = 2
        while os.path.exists(os.path.join(out_dir, f"{stem}-{n}{ext}")):
            n += 1
        dest = os.path.join(out_dir, f"{stem}-{n}{ext}")
    shutil.copyfile(src, dest)
    # 临时下载中转文件（.cache/media 下）落盘后即删，成品只在交付目录留一份
    cache_dir = os.path.join(BASE_DIR, ".cache", "media")
    if os.path.abspath(src).startswith(os.path.abspath(cache_dir)):
        try:
            os.remove(src)
        except OSError:
            pass
    url = _private_url(dest)
    if not url and vault:
        try:
            url = "/vault/" + os.path.relpath(dest, vault).replace(os.sep, "/")
        except ValueError:
            url = ""
    return dest, url


def summary(jobs):
    """分镜作业概览：数量与最坏状态，前端表格直接用。"""
    out = {}
    for j in jobs:
        no = int(j.get("shotNo") or j.get("shot_no") or 0)
        rec = out.setdefault(no, {"total": 0, "running": 0, "queued": 0, "done": 0,
                                  "failed": 0, "canceled": 0, "progress": 0})
        st = str(j.get("status") or "")
        rec["total"] += 1
        if st in rec:
            rec[st] += 1
        rec["progress"] = max(rec["progress"], int(j.get("progress") or 0))
    return out


# 模块加载即启动看门狗（daemon，进程退出自动回收）：
# 服务一启动就兜底扫描，不必等第一次访问媒体接口才懒加载。
threading.Thread(target=_watchdog_loop, name="media-watchdog", daemon=True).start()