"""media-platform: 把 media-engine（192.168.21.206:9200）的数字人/媒体能力暴露成模型工具。

工具（toolset=media）：
  media_intro_video     员工自我介绍视频（intro_video）：生成/复用 assets/intro/<id>.mp4
  media_intro_status    查单个员工成片状态（不触发生成）
  media_profile         员工档案（含照片/视频/自我介绍是否就绪）
  media_avatars         可插拔数字人素材清单（照片 + 仿真人视频）
  media_tts             文本转语音（返回 wav 路径 + 句级时间轴）
  media_asr             语音转文字（本地文件或 base64）
  media_boss_briefing   触发小秘书「老板上线汇报」（异步，出详细文字 + 语音）
  media_job_status      查询/列出异步任务

视频合成、TTS/ASR 全部在 media-engine 进程内完成，本插件不加载模型。
之所以走 HTTP：网关进程要保持轻量，插件热更新也不能把推理服务一起拖下水。

环境变量（均可不填）：
  MEDIA_ENGINE_URL        默认 http://192.168.21.206:9200（21 网段万兆；引擎已不绑回环）
  MEDIA_ENGINE_TOKEN      与引擎 MEDIA_ENGINE_TOKEN 一致时带 Bearer
  MEDIA_PLATFORM_TIMEOUT  普通请求超时秒数，默认 60
  MEDIA_JOB_TIMEOUT       带等待的任务请求超时秒数，默认 900
  MEDIA_TTS_TIMEOUT       单次合成超时秒数，默认 900（首次要等模型加载）
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

ENGINE_URL = os.environ.get("MEDIA_ENGINE_URL", "http://192.168.21.206:9200").rstrip("/")
ENGINE_TOKEN = os.environ.get("MEDIA_ENGINE_TOKEN", "").strip()
TIMEOUT = float(os.environ.get("MEDIA_PLATFORM_TIMEOUT", "60"))
JOB_TIMEOUT = float(os.environ.get("MEDIA_JOB_TIMEOUT", "900"))
TTS_TIMEOUT = float(os.environ.get("MEDIA_TTS_TIMEOUT", "900"))
ASR_TIMEOUT = float(os.environ.get("MEDIA_ASR_TIMEOUT", "300"))
TOOLSET = "media"


class EngineError(RuntimeError):
    """media-engine 返回错误或不可达。"""


# ---------------------------------------------------------------- HTTP 客户端
def _request(method, path, params=None, body=None, timeout=None):
    url = ENGINE_URL + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if ENGINE_TOKEN:
        req.add_header("Authorization", "Bearer " + ENGINE_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - 读错误体失败不影响报错
            pass
        raise EngineError("media-engine HTTP %s：%s" % (e.code, detail or e.reason))
    except urllib.error.URLError as e:
        raise EngineError("连不上 media-engine（%s）：%s" % (ENGINE_URL, e.reason))
    except TimeoutError:
        raise EngineError("media-engine 请求超时（%.0fs）：%s" % (timeout or TIMEOUT, path))
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except ValueError as e:
        raise EngineError("media-engine 返回非 JSON：%s" % e)


_ALIVE = {"at": 0.0, "ok": False}


def engine_ready():
    """check_fn：引擎不在线就把工具收起，避免模型反复调用拿到连接错误。"""
    import time
    now = time.time()
    if now - _ALIVE["at"] < 15:
        return _ALIVE["ok"]
    try:
        _request("GET", "/health", timeout=3)
        ok = True
    except Exception:  # noqa: BLE001 - 探活失败按不可用处理
        ok = False
    _ALIVE.update({"at": now, "ok": ok})
    return ok


# ---------------------------------------------------------------- 结果封装
def _ok(data):
    return json.dumps(data, ensure_ascii=False)


def _err(message):
    return json.dumps({"error": str(message)}, ensure_ascii=False)


def _guard(fn):
    """工具异常统一转成 {"error": ...}，模型能看到原因而不是断链；返回值统一 JSON 字符串。"""
    def wrapper(args):
        try:
            out = fn(args or {})
        except EngineError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err("%s: %s" % (type(e).__name__, e))
        return out if isinstance(out, str) else _ok(out)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------- 工具实现
def intro_video(args):
    body = {
        "employee_id": args.get("employee_id"),
        "all": bool(args.get("all")),
        "force": bool(args.get("force")),
        "refresh_text": bool(args.get("refresh_text", True)),
        "wait": bool(args.get("wait", True)),
        "wait_sec": float(args.get("wait_sec") or 90),
        "text": args.get("text"),
        "voice": args.get("voice"),
    }
    view = _request("POST", "/v1/video/jobs", body=body,
                    timeout=JOB_TIMEOUT)
    return view


def intro_status(args):
    aid = str(args.get("employee_id") or "").strip()
    if not aid:
        raise ValueError("需要 employee_id")
    return _request("GET", "/v1/video/%s" % urllib.parse.quote(aid, safe=""))


def profile(args):
    return _request("GET", "/v1/profiles", params={
        "id": args.get("employee_id") or args.get("id"),
        "dept": args.get("dept"),
        "project": args.get("project"),
        "include_assets": "false" if args.get("include_assets") is False else "true",
    })


def avatars(args):
    return _request("GET", "/v1/avatars")


def tts(args):
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("需要 text")
    engine = str(args.get("engine") or "cosyvoice").lower()
    # qwen3 在大模型侧是离线高质量引擎，默认走异步作业；cosyvoice 保持同步直出
    mode = str(args.get("mode") or ("async" if engine == "qwen3" else "sync")).lower()
    if engine not in ("cosyvoice", "qwen3"):
        raise ValueError("engine 只支持 cosyvoice / qwen3")
    if mode not in ("async", "sync"):
        raise ValueError("mode 只支持 async / sync")
    body = {"text": text, "voice": args.get("voice"), "speed": float(args.get("speed") or 1.0),
            "format": "json", "stem": args.get("stem"), "engine": engine, "mode": mode,
            "speaker": args.get("speaker"), "language": args.get("language"),
            "instruct": args.get("instruct")}
    return _request("POST", "/v1/tts", body=body, timeout=TTS_TIMEOUT)


def asr(args):
    engine = str(args.get("engine") or "whisper").lower()
    mode = str(args.get("mode") or ("async" if engine == "qwen3" else "sync")).lower()
    if engine not in ("whisper", "qwen3"):
        raise ValueError("engine 只支持 whisper / qwen3")
    if mode not in ("async", "sync"):
        raise ValueError("mode 只支持 async / sync")
    body = {"language": args.get("language"), "model": args.get("model"),
            "engine": engine, "mode": mode}
    if args.get("audio_path"):
        body["audio_path"] = args["audio_path"]
    elif args.get("audio_base64"):
        body["audio_base64"] = args["audio_base64"]
    else:
        raise ValueError("需要 audio_path 或 audio_base64")
    return _request("POST", "/v1/asr", body=body, timeout=ASR_TIMEOUT)


def boss_briefing(args):
    wait_sec = float(args.get("wait_sec") or 30)
    return _request("POST", "/v1/jobs", body={
        "kind": "briefing", "wait": bool(args.get("wait", True)),
        "wait_sec": wait_sec, "voice": args.get("voice"),
        "emotion": args.get("emotion"),
    }, timeout=JOB_TIMEOUT)


# 语音作业（qwen3 异步 TTS/ASR）的 job_id 前缀，命中就走 /v1/audio/jobs
_AUDIO_JOB_PREFIXES = ("asr-", "tts-")


def job_status(args):
    job_id = str(args.get("job_id") or "").strip()
    kind = str(args.get("kind") or "").strip().lower()
    if not job_id:
        if kind in ("asr", "tts"):
            return _request("GET", "/v1/audio/jobs",
                            params={"limit": int(args.get("limit") or 20), "kind": kind})
        return _request("GET", "/v1/jobs", params={"limit": int(args.get("limit") or 20)})
    if job_id.startswith(_AUDIO_JOB_PREFIXES):
        return _request("GET", "/v1/audio/jobs/%s" % urllib.parse.quote(job_id, safe=""),
                        params={"wait_sec": float(args.get("wait_sec") or 0)},
                        timeout=JOB_TIMEOUT)
    return _request("GET", "/v1/jobs/%s" % urllib.parse.quote(job_id, safe=""),
                    params={"wait_sec": float(args.get("wait_sec") or 0)},
                    timeout=JOB_TIMEOUT)


# ---------------------------------------------------------------- 工具清单
_EMPLOYEE_ID = {"type": "string", "description": "员工 ID，如 secretary / cto / pm_wenren"}

TOOLS = [
    {
        "name": "media_intro_video",
        "description": (
            "生成或复用员工的自我介绍视频（数字人档案 intro_video）：文案润色 → TTS → 口型/画面合成，"
            "成片落 assets/intro/<id>.mp4 并写入员工档案。已在成片且未 force 时秒回缓存。"
            "长任务可 wait=false 拿 job_id，再用 media_job_status 查询。"
        ),
        "emoji": "🎬",
        "schema": {"type": "object", "properties": {
            "employee_id": _EMPLOYEE_ID,
            "all": {"type": "boolean", "description": "true 表示处理全部员工（与 employee_id 互斥）"},
            "force": {"type": "boolean", "description": "true 忽略缓存重新生成"},
            "refresh_text": {"type": "boolean", "description": "force 时是否同时重写自我介绍文案，默认 true"},
            "wait": {"type": "boolean", "description": "true 等出片再返回，默认 true；false 立即返回 job_id"},
            "wait_sec": {"type": "number", "description": "最长等待秒数，默认 90"},
            "text": {"type": "string", "description": "指定配音文案（不给则由档案自动生成）"},
            "voice": {"type": "string", "description": "TTS 音色 ID（默认取员工档案 voice）"},
        }, "required": []},
    },
    {
        "name": "media_intro_status",
        "description": "查某个员工自我介绍视频是否已生成（只读，不触发生成），返回地址、时长与文案。",
        "emoji": "🔎",
        "schema": {"type": "object", "properties": {"employee_id": _EMPLOYEE_ID},
                   "required": ["employee_id"]},
    },
    {
        "name": "media_profile",
        "description": (
            "查员工档案：部门/岗位/职责/口头禅/技能，以及照片、仿真人视频、自我介绍视频是否就绪。"
            "可按 employee_id / dept / project 过滤，不传则返回全部。"
        ),
        "emoji": "🧑‍💼",
        "schema": {"type": "object", "properties": {
            "employee_id": _EMPLOYEE_ID,
            "dept": {"type": "string", "description": "按部门名过滤（包含匹配）"},
            "project": {"type": "string", "description": "按项目过滤（包含匹配）"},
            "include_assets": {"type": "boolean", "description": "是否带素材地址，默认 true"},
        }, "required": []},
    },
    {
        "name": "media_avatars",
        "description": "列出可插拔的数字人素材（头像/面部特写/仿真人 talk·idle 视频），用于判断谁能出镜。",
        "emoji": "🧿",
        "schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "media_tts",
        "description": (
            "文本转语音，按任务形态选引擎（调度规则）："
            "① 异步/离线配音——视频旁白、长文本、成品音轨、批量配音——用 engine=qwen3，"
            "提交后立即返回 job_id，再用 media_job_status 轮询拿 audio_url；"
            "② 实时短回执/即时播报——用默认 engine=cosyvoice，同步直出 wav。"
            "返回 wav 路径、下载地址、时长和句级时间轴。"
        ),
        "emoji": "🔊",
        "schema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "要朗读的文本"},
            "engine": {"type": "string", "enum": ["cosyvoice", "qwen3"],
                       "description": "cosyvoice=实时同步（默认）；qwen3=离线高质量，默认异步作业"},
            "mode": {"type": "string", "enum": ["async", "sync"],
                     "description": "async=立即返回 job_id 轮询（qwen3 默认）；sync=阻塞等结果"},
            "voice": {"type": "string", "description": "音色 ID，如 m06 / female-zh-real（cosyvoice）"},
            "speaker": {"type": "string",
                        "description": "qwen3 音色：Vivian/Serena/Uncle_Fu/Dylan/Eric/Ryan/Aiden/Ono_Anna/Sohee"},
            "instruct": {"type": "string", "description": "qwen3 情绪指令，如「兴奋地说」（可选）"},
            "language": {"type": "string", "description": "语言，默认中文"},
            "speed": {"type": "number", "description": "语速倍率，默认 1.0（仅 cosyvoice）"},
            "stem": {"type": "string", "description": "产物文件名前缀（可选）"},
        }, "required": ["text"]},
    },
    {
        "name": "media_asr",
        "description": (
            "语音转文字，按任务形态选引擎（调度规则）："
            "① 异步/离线转写——成片审听、逐字稿、长音频、素材归档——用 engine=qwen3，"
            "提交后立即返回 job_id，再用 media_job_status 轮询拿文本与时间轴；"
            "② 实时短语音——即时回执、聊天语音消息——用默认 engine=whisper，同步返回。"
        ),
        "emoji": "🎧",
        "schema": {"type": "object", "properties": {
            "audio_path": {"type": "string", "description": "本机音频文件绝对路径"},
            "audio_base64": {"type": "string", "description": "音频 base64（与 audio_path 二选一）"},
            "engine": {"type": "string", "enum": ["whisper", "qwen3"],
                       "description": "whisper=实时同步（默认）；qwen3=离线高质量，默认异步作业"},
            "mode": {"type": "string", "enum": ["async", "sync"],
                     "description": "async=立即返回 job_id 轮询（qwen3 默认）；sync=阻塞等结果"},
            "language": {"type": "string", "description": "语言，默认 zh"},
            "model": {"type": "string", "description": "whisper 模型，默认引擎配置"},
        }, "required": []},
    },
    {
        "name": "media_boss_briefing",
        "description": (
            "让 OPC-001 小秘书做一次「老板上线汇报」：读公司动态/事件流/交付日志，输出"
            "「结论 → 进展 → 风险 → 待拍板」，并合成语音。异步任务，可 wait=false 后查询。"
        ),
        "emoji": "📋",
        "schema": {"type": "object", "properties": {
            "wait": {"type": "boolean", "description": "true 等结果，默认 true"},
            "wait_sec": {"type": "number", "description": "最长等待秒数，默认 30（生成通常要更久，建议 false+轮询）"},
            "voice": {"type": "string", "description": "播报音色"},
            "emotion": {"type": "string", "description": "播报情绪：calm/serious/cheerful/neutral"},
        }, "required": []},
    },
    {
        "name": "media_job_status",
        "description": (
            "查询异步任务：job_xxx（自我介绍视频 / 老板汇报）走视频作业；asr-xxx / tts-xxx"
            "（qwen3 异步语音）走语音作业，按前缀自动路由。给 job_id 取结果，不给则列出最近任务。"
            "传 wait_sec 可长轮询，作业结束立即返回，不必自己 sleep 等待。"
        ),
        "emoji": "⏳",
        "schema": {"type": "object", "properties": {
            "job_id": {"type": "string", "description": "任务 ID（job_xxx / asr-xxx / tts-xxx）"},
            "kind": {"type": "string", "enum": ["asr", "tts"],
                     "description": "不带 job_id 时按类型列语音作业"},
            "wait_sec": {"type": "number",
                         "description": "长轮询秒数，最多 300（视频作业与语音作业均支持）"},
            "limit": {"type": "integer", "description": "列出任务条数，默认 20"},
        }, "required": []},
    },
]

_HANDLERS = {
    "media_intro_video": intro_video,
    "media_intro_status": intro_status,
    "media_profile": profile,
    "media_avatars": avatars,
    "media_tts": tts,
    "media_asr": asr,
    "media_boss_briefing": boss_briefing,
    "media_job_status": job_status,
}


def register(ctx) -> None:
    for tool in TOOLS:
        ctx.register_tool(
            name=tool["name"],
            toolset=TOOLSET,
            schema={"name": tool["name"], "description": tool["description"],
                    "parameters": tool["schema"]},
            handler=_guard(_HANDLERS[tool["name"]]),
            check_fn=engine_ready,
            description=tool["description"],
            emoji=tool["emoji"],
        )


__all__ = ["register"]
