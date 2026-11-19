"""MCP stdio 服务：JSON-RPC 2.0，暴露初始化/素材/分片续传/知识库/跨源资源/路由/对话/
TTS/图像/能力查询/产物取回/轻量健康检查共 23 个工具（以 TOOLS 列表为准，
client 用法见 mcp/docs/OPC_MCP_CLIENT_USAGE.md）。

契约要点（2026-11-19 起）：
- 每个工具带 annotations（readOnlyHint/destructiveHint/idempotentHint），客户端按注解过滤；
- 错误返回带稳定 code（invalid_argument/not_found/unauthorized/forbidden/
  rate_limited/upstream_unavailable/internal_error）；
- ask 支持 idempotency_key 幂等重放；未完成作业统一带 retry_after_sec；
- material_list 支持 cursor 分页；material_upload 支持同名取代（supersedes/keep_history）；
- kb_status/opc_whoami 默认脱敏（detail=true 按白名单手机号放开）；
- kb_ping 提供缓存式轻量健康检查。
"""

import base64
import binascii
import collections
import hashlib
import json
import re
import secrets
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, auth, config, events as events_mod
from . import graph_brief, knowledge, llm, media, overview, resources, staff_chat, staff_photos
from . import supersede as supersede_mod
from .jobs import JobRunner
from .store import Store

_STDOUT_LOCK = threading.Lock()   # 响应与作业通知共用 stdout，单行写入必须原子

PROTOCOL = "2024-11-05"
SYSTEM_PROMPT = ("你是 OPC 公司的知识助手。优先依据给出的知识库资料回答，资料不足就明确说缺什么，"
                 "不要编造。回答用中文，先给结论，再给依据。")


_DEGRADED_RE = re.compile(r"[（(]?\s*LLM\s*通道(?:暂)?不可用[^）)\n]*[）)]?")


def _clean_snippet(text, limit=180):
    """清掉旧降级回答留下的嵌套标记，避免一次故障滚成多层拼接。"""
    s = str(text or "").replace("\n", " ").strip()
    if "LLM 通道" in s and ("先给知识库命中" in s or s.lstrip().startswith(("[降级]", "（LLM", "(LLM"))):
        s = _DEGRADED_RE.sub("", s)
        s = re.sub(r"^[\s\-–—•]*【[^】]{1,40}】", "", s)
        s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def log(msg):
    sys.stderr.write("[opc-mcp] %s\n" % msg)
    sys.stderr.flush()


def _json(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}],
            "isError": False}


def _err(msg, status=None, code=None, **extra):
    """统一错误体：code 是稳定机器码；status 保留旧字段向后兼容。

    code 取值：invalid_argument / not_found / unauthorized / forbidden /
    rate_limited / upstream_unavailable / internal_error。
    """
    body = {"ok": False, "error": str(msg)}
    if status:
        body["status"] = status
    body["code"] = code or _ERR_CODE.get(status or "", "internal_error")
    body.update(extra)
    return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
            "isError": True}


# 旧 status → 稳定 code 的映射（历史客户端只看 status，不受影响）
_ERR_CODE = {"need_auth": "unauthorized", "not_found": "not_found", "gone": "not_found",
             "blocked": "forbidden", "media_error": "upstream_unavailable",
             "bad_kind": "invalid_argument"}


def _safe_name(name):
    name = Path(str(name or "upload.bin")).name
    return re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name)[:120] or "upload.bin"


TOOLS = [
    {
        "name": "opc_init",
        "description": "用个人 API key 初始化 OPC 会话（推荐外网直接配请求头 X-Mail-Agent，通常不用本工具）。"
                       "只给 key 即可，key 自带手机号；手机号只在网页申请 key 或密码登录时使用。"
                       "只传手机号返回 need_secret，什么都不传也返回 need_secret（带申请地址）。",
        "inputSchema": {"type": "object", "properties": {
            "key": {"type": "string", "description": "个人 API key；外网接入只需这一项"},
            "phone": {"type": "string", "description": "可选/已弃用：仅密码登录或旧版全局 key 时才需要"},
            "password": {"type": "string", "description": "可选/不推荐：密码登录需同时带 phone"}},
            "required": []},
    },
    {
        "name": "opc_login",
        "description": "个人 API key 单独即可换 session_token（手机号从 key 解析）；"
                       "只有密码登录或旧版全局 key 才需要 phone。",
        "inputSchema": {"type": "object", "properties": {
            "key": {"type": "string", "description": "个人 API key（推荐，单独使用即可）"},
            "phone": {"type": "string", "description": "仅密码登录/旧版全局 key 需要"},
            "password": {"type": "string", "description": "登录密码；需同时带 phone"}},
            "required": []},
    },
    {
        "name": "opc_whoami",
        "description": "确认当前网关注入的个人 API key 对应的手机号与工作区；不需要传 token。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "material_upload",
        "description": "上传素材文件并自动排队分析、内化进知识库。path 方式上限 2GB（服务端本地文件，流式拷贝）；"
                       "content_base64 上限 100MB。zip/pdf/doc/docx/xls/ppt/pptx 均可解析；"
                       "需要跨网络断点续传时改用 upload_begin。"
                       "版本语义：同名旧素材默认被本版本取代（旧版切块/卡片与内化条目不再参与召回，"
                       "文件保留可审计）；keep_history=true 保留历史并存；也可用 supersedes 显式指定被取代的素材。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"},
            "path": {"type": "string", "description": "服务端可读的本地文件路径"},
            "content_base64": {"type": "string", "description": "文件内容 base64（与 path 二选一）"},
            "filename": {"type": "string", "description": "content_base64 时的文件名，含扩展名"},
            "supersedes": {"type": "string",
                           "description": "显式指定被本版本取代的素材 id（可选，默认按同名自动取代）"},
            "keep_history": {"type": "boolean", "default": False,
                             "description": "true=同名旧版不取代，多版本并存（默认 false 只召回最新版）"}},
            "required": ["token"]},
    },
    {
        "name": "upload_begin",
        "description": "大文件分片续传建单（zip/pdf/doc 等，单文件最大 2GB）。兼容四种协议："
                       "①PUT ?offset=N；②Content-Range: bytes s-e/total（bytes */* 查状态）；"
                       "③片号 ?part=N（建单带 chunk_size）；④Tus 1.0：POST /files + HEAD/PATCH /files/{id}。"
                       "返回 put_url/part_put_url/tus_url/tus_create_url/progress_url/complete_url。"
                       "进度接口含 percent/已传/剩余/速率/ETA；断电后 GET progress_url 取 offset 续传。"
                       "版本语义与 material_upload 一致：同名旧版默认取代；supersedes/keep_history 可在建单时指定。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"},
            "filename": {"type": "string", "description": "含扩展名的文件名，如 资料.zip、合同.pdf"},
            "size": {"type": "integer", "description": "文件总字节数（必须精确）"},
            "chunk_size": {"type": "integer", "description": "可选：片号模式的分片字节数，需固定"},
            "supersedes": {"type": "string",
                           "description": "显式指定被本版本取代的素材 id（可选，默认按同名自动取代）"},
            "keep_history": {"type": "boolean", "default": False,
                             "description": "true=同名旧版不取代，多版本并存"}},
            "required": ["token", "filename", "size"]},
    },
    {
        "name": "material_list",
        "description": "列出本人已上传素材及其分析状态（默认不含已被取代的旧版；"
                       "include_superseded=true 可见全部）。超出一页时用 next_cursor 翻页。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"}, "limit": {"type": "integer", "default": 50},
            "cursor": {"type": "string", "description": "上一页返回的 next_cursor（可选）"},
            "include_superseded": {"type": "boolean", "default": False,
                                   "description": "true=连已被取代的旧版一起列出"}},
            "required": ["token"]},
    },
    {
        "name": "material_download",
        "description": "下载素材：返回落盘路径，内联时可附 base64。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"}, "material_id": {"type": "string"},
            "inline": {"type": "boolean", "default": False},
            "dest": {"type": "string", "description": "另存到指定路径（可选）"}},
            "required": ["token", "material_id"]},
    },
    {
        "name": "material_delete",
        "description": "删除本人上传的素材，并同步清掉它的切块与知识卡片，删除后 kb_search/ask 不再命中。"
                       "只能删当前 key 所属账号的素材（先用 material_list 拿 material_id）；"
                       "默认同时删磁盘原文件，仅当没有其他素材引用同一路径时才删。"
                       "已内化进 OPC 知识库/vault 的条目不在 MCP 素材范围内，不会随之删除。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"},
            "material_id": {"type": "string", "description": "要删除的素材 id"},
            "remove_file": {"type": "boolean", "default": True,
                            "description": "是否同时删除磁盘原文件，默认 true"}},
            "required": ["token", "material_id"]},
    },
    {
        "name": "material_analyze",
        "description": "对指定素材（或 all=全部待处理）重新做后台分析，返回作业号。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"},
            "material_id": {"type": "string", "description": "素材 id；填 all 表示全部待处理"}},
            "required": ["token", "material_id"]},
    },
    {
        "name": "job_get",
        "description": "查询后台作业（分析/问答）状态与结果；wait_sec>0 时最多等待若干秒。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"}, "job_id": {"type": "string"},
            "wait_sec": {"type": "number", "default": 0}},
            "required": ["token", "job_id"]},
    },
    {
        "name": "kb_status",
        "description": "知识库与依赖服务状态：素材数、可检索片段数、卡片数、近期作业、LLM/TTS/图像探活。"
                       "当用户提到知识库/知识管理/OPC/同心圆/知识图谱/图数据库，想先看数据规模或是否可用时优先调用。"
                       "默认已脱敏（不含服务端路径/内网地址）；面板轮询请改用 kb_ping（有缓存，不重复探测上游）。",
        "inputSchema": {"type": "object", "properties": {"token": {"type": "string"},
                        "detail": {"type": "boolean", "default": False,
                                   "description": "true=返回 dataDir/vault/llm.base_url 等完整信息（仅管理员 key）"}},
                        "required": ["token"]},
    },
    {
        "name": "kb_ping",
        "description": "轻量健康检查：LLM/语音/图像三盏灯 + degraded 标记。结果缓存约 60 秒，"
                       "面板/轮询场景用本工具，不要轮询 kb_status（那会真实探测上游模型、消耗额度）。"
                       "refresh=true 忽略缓存立即重探。",
        "inputSchema": {"type": "object", "properties": {
            "refresh": {"type": "boolean", "default": False,
                        "description": "true=忽略缓存立即探测（仍受每 key 限流约束）"}},
            "required": []},
    },
    {
        "name": "kb_overview",
        "description": "知识库概览一次返回：分组计数（knowledge/materials/content）+ 三盏健康灯 + "
                       "账号信息 + 近期作业，带版本号 rev 支持条件请求（rev 相同只回约 60 字节的 "
                       "changed:false，不计入限流）。概览页/面板用本工具，替代 kb_status+kb_ping 两次调用。"
                       "缓存分层 TTL：groups/rev 30s、recent 10s、健康灯 60s/30s，后台线程按各 TTL 自动预热。",
        "inputSchema": {"type": "object", "properties": {
            "rev": {"type": "string",
                    "description": "上次拿到的 rev；传相同 rev 且数据未变时返回 changed:false（几乎零成本）"},
            "fields": {"type": "string",
                       "description": "逗号分隔裁剪：groups,health,owner,recent；默认全给"},
            "refresh": {"type": "boolean", "default": False,
                        "description": "true=忽略缓存立即重算 groups/rev 与 recent，健康灯强制重探（计入限流）"},
            "detail": {"type": "boolean", "default": False,
                       "description": "true=附带服务端路径（仅管理员 key）"}},
            "required": []},
    },
    {
        "name": "kb_search",
        "description": "在本人知识库做关键词检索，返回最相关片段与出处。当用户提到知识库/知识管理/OPC/同心圆/"
                       "知识图谱/图数据库，或要查原文、找出处、确认『有没有/在哪里』时优先调用（配合 ask 做综合问答）。"
                       "命中带 reasoning 字段（1-8，八步执行法主步骤：立题/证据/结构/决策/执行/反馈/归档/复盘），"
                       "可用 reasoning 参数只取某一步的材料。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"}, "query": {"type": "string"},
            "k": {"type": "integer", "default": 6},
            "reasoning": {"type": "integer", "minimum": 1, "maximum": 8,
                          "description": "只取八步执行法第 N 步的材料（1立题 2证据 3结构 4决策 5执行 6反馈 7归档 8复盘）"}},
            "required": ["token", "query"]},
    },
    {
        "name": "ask",
        "description": "文本问答：后台结合知识库内化内容调用 LLM 生成结果；wait_sec>0 可直接拿答案。"
                       "当用户提到知识库/知识管理/OPC/同心圆/知识图谱/图数据库，或要求总结/解释/对比/给结论并带引用时"
                       "优先调用。问题内容用 text 或 question 任一传入即可（两者等价）。"
                       "库内事实类问题建议 with_brief=true：自动注入按五行三才分组的知识图谱简报，"
                       "有硬字符预算且不随知识库变大膨胀，可省掉先调 kb_search 的一轮；返回 knowledge_brief 元数据。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"},
            "text": {"type": "string", "description": "问题内容；与 question 二选一（等价，question 为推荐名）"},
            "question": {"type": "string", "description": "同 text，问题内容；二选一即可"},
            "idempotency_key": {"type": "string", "maxLength": 128,
                                "description": "幂等键：同一 key 在窗口内（默认 15 分钟）重放返回同一 job_id，"
                                               "客户端超时重试时务必携带，避免双排任务"},
            "wait_sec": {"type": "number", "default": 0,
                         "description": "同步等待秒数，0=只返回作业号；上限 300；"
                                        "到点未完成返回 status=queued/running + retry_after_sec，用 job_get 续等"},
            "with_brief": {"type": "boolean", "default": False,
                           "description": "true=注入图谱简报并在结果里返回 knowledge_brief（含 text 全文）；默认 false 完全走原链路"},
            "brief_level": {"type": "integer", "enum": [0, 1, 2], "default": 1,
                            "description": "简报层级：0 只要种子；1 种子+一跳邻居；2 再加图扩展节点"},
            "brief_chars": {"type": "integer", "default": 900,
                            "description": "简报硬字符预算 200~2000，默认 900"}},
            "required": ["token"]},
    },
    {
        "name": "opc_route",
        "description": "路由兜底：当用户提到知识库/知识管理/OPC/同心圆/知识图谱/图数据库等词但不确定该调哪个 OPC 工具时，"
                       "先调本工具，传用户原话 query，返回推荐工具链和参数模板。纯提示工具，不需要鉴权或手机号。",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "用户原话或意图描述"}},
            "required": ["query"]},
    },
    {
        "name": "opc_resources",
        "description": "资源总览：一次看清本人手机号下所有可访问资源——OPC 库（知识/报告/内容/消息/流水线/审批）、"
                       "Obsidian vault 顶层目录、MCP 自有素材/片段/卡片，以及全局共享部分 shared"
                       "（全公司知识、共享素材清单、公开下载区、共享热点流；所有 key 可见）。"
                       "想确认『key 能看到哪些东西』或跨源检索前先调本工具。"
                       "默认已脱敏（不含服务端绝对路径/内网地址）；detail=true 仅管理员 key 可见完整信息。",
        "inputSchema": {"type": "object", "properties": {
            "detail": {"type": "boolean", "default": False,
                       "description": "true=返回 dataDir/vault/ownerDb 等完整信息（仅管理员 key）"}},
            "required": []},
    },
    {
        "name": "opc_resource_list",
        "description": "列出某类资源。本人手机号：knowledge/reports/contents/messages/approvals/vault"
                       "（vault 只列未屏蔽 md）。全局共享（所有 key 可见）：shared_knowledge/"
                       "shared_materials/shared_hotspots/downloads。",
        "inputSchema": {"type": "object", "properties": {
            "kind": {"type": "string", "default": "knowledge",
                     "description": "knowledge/reports/contents/messages/approvals/vault/"
                                    "shared_knowledge/shared_materials/shared_hotspots/downloads"},
            "limit": {"type": "integer", "default": 20},
            "detail": {"type": "boolean", "default": False,
                       "description": "true=保留 path/fileRoot 等字段（仅管理员 key；默认已净化）"}},
            "required": []},
    },
    {
        "name": "opc_resource_read",
        "description": "读取单个资源：本人 knowledge:kb_xxx / report:<id> / content:<id> / "
                       "vault:相对路径 / material:<mat_id>；全局共享 shared_knowledge:<id> / "
                       "shared_material:<id> / shared_hotspot:<id> / shared_download:<文件名>。"
                       "敏感文件按安全策略屏蔽；超过 2MB 请用 kb_search 检索片段。",
        "inputSchema": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "<类型>:<标识>"},
            "detail": {"type": "boolean", "default": False,
                       "description": "true=保留 item.path 等字段（仅管理员 key；默认已净化）"}},
            "required": ["ref"]},
    },
    {
        "name": "tts",
        "description": "按需求描述合成音频：text=口播文字稿、brief=整体需求描述、notes=注意事项。"
                       "渠道 tts(Qwen3/9311)、cosyvoice(9200) 或 bailian(阿里云百炼 qwen-audio-3.0-tts-plus，云服务按字符计费)，"
                       "可指定音色，产出 wav/mp3 文件路径并用 file_key 交付下载。"
                       "身份沿用 opc_init 的 client_key，不需单独 token；渠道/音色（百炼 601 个在 media_capabilities 的 audio.bailianVoices）/限制先查 media_capabilities。",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "要读出来的口播原文"},
            "brief": {"type": "string", "description": "整体需求描述（用途、风格等）"},
            "notes": {"type": "string", "description": "注意事项（语速、停顿、情绪）；Qwen3 渠道作为 instruct 下发"},
            "channel": {"type": "string", "enum": ["tts", "qwen3", "cosyvoice", "bailian"], "default": "cosyvoice",
                        "description": "tts/qwen3=Qwen3-TTS，cosyvoice=CosyVoice，bailian=阿里云百炼云服务"},
            "voice": {"type": "string", "description": "音色名或 speaker；百炼必填音色 id，可用值查 media_capabilities 的 audio.bailianVoices"},
            "engine": {"type": "string", "enum": ["cosyvoice", "qwen3"], "description": "兼容旧调用，优先用 channel"},
            "speed": {"type": "number", "default": 1.0},
            "format": {"type": "string", "enum": ["mp3", "wav", "pcm", "opus"], "default": "mp3",
                       "description": "仅 bailian 渠道：输出音频格式"},
            "sample_rate": {"type": "integer", "default": 22050, "description": "仅 bailian 渠道：采样率 8000~48000"},
            "wait_sec": {"type": "number", "default": 180}},
            "required": ["text"]},
    },
    {
        "name": "image",
        "description": "生成新图片：只给文字描述即可文生图；也可附参照照片做图生图。渠道 sd15(9103 文生图)、"
                       "qwen-image(8188 本地/157)、bailian_image(阿里云百炼 qwen-image-3.0-pro 云服务，按张计费，"
                       "支持文生图/图生图)。身份沿用 opc_init 的 client_key，不需单独 token；"
                       "渠道/尺寸/耗时限制先查 media_capabilities。",
        "inputSchema": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "画面文字描述"},
            "brief": {"type": "string", "description": "整体需求描述（用途、风格等，可选）"},
            "reference_image": {"type": "string", "description": "参照照片：本地绝对路径或 data:image/...;base64；给定时自动走图生图（bailian_image 也支持）"},
            "channel": {"type": "string", "enum": ["sd15", "qwen-image", "bailian_image"], "default": "sd15"},
            "negative_prompt": {"type": "string"},
            "width": {"type": "integer", "default": 640},
            "height": {"type": "integer", "default": 832},
            "steps": {"type": "integer", "default": 20},
            "seed": {"type": "integer"},
            "size": {"type": "string", "default": "1024*1024",
                     "description": "仅 bailian_image：输出尺寸「宽*高」星号，面积 512²~2048²、比例 1:8~8:1"},
            "n": {"type": "integer", "default": 1, "description": "仅 bailian_image：出图张数 1~4，多张按张计费"},
            "prompt_extend": {"type": "boolean", "default": True, "description": "仅 bailian_image：提示词扩写"},
            "watermark": {"type": "boolean", "default": False, "description": "仅 bailian_image：是否加百炼水印"},
            "wait_sec": {"type": "number", "default": 300}},
            "required": ["prompt"]},
    },
    {
        "name": "media_capabilities",
        "description": "查询生成能力：音频/图像/视频的渠道、音色、支持模式与限制（文本长度、尺寸、耗时）。"
                       "调 tts/image 前建议先查；与注册共用 client_key，无额外 token。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "file_link",
        "description": "已生成的 tts/image 产物重新取下载链接（换设备/丢失链接时用）。"
                       "输入 tts/image 返回的 file_key；下载用同一把个人 key 作 X-Mail-Agent 头。",
        "inputSchema": {"type": "object", "properties": {
            "file_key": {"type": "string", "description": "tts/image 结果里的 file_key"}},
            "required": ["file_key"]},
    },
    {
        "name": "staff_photos",
        "description": "列出公司员工的照片与半身照（全局共享，所有 key 可见，与公开下载区同口径）。"
                       "kind=portrait(本人形象)/halfbody(半身照)/digital_human(数字人口播形象)/all；"
                       "返回每张的 name/kind/file_key/download_url，用同一把个人 key 作 X-Mail-Agent 头即可 curl 到本地。"
                       "结果与 media_capabilities 同样已净化，不含服务端路径。",
        "inputSchema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["all", "portrait", "halfbody", "digital_human"],
                     "default": "all", "description": "照片类别，默认全部"},
            "limit": {"type": "integer", "default": 200, "description": "最多返回张数"}},
            "required": []},
    },
    {
        "name": "staff_chat",
        "description": "员工对话通道（仅老板 key）：像在工作台输入 @员工 一样给具体 AI 员工下指令，"
                       "员工走独立 hermes profile 真实处理后回复（非通用问答）。"
                       "先 list_agents=true 拿员工 id/姓名/岗位，再 agent=<id 或姓名> message=<指令> 下发；"
                       "wait_sec>0 等回复（员工处理约几十秒），指令与回复会落入消息中心线程 mcp:<手机号>。",
        "inputSchema": {"type": "object", "properties": {
            "agent": {"type": "string", "description": "员工 id 或姓名，如 cto / 牛盒码；先 list_agents 查清单"},
            "message": {"type": "string", "description": "下发的指令内容"},
            "list_agents": {"type": "boolean", "default": False,
                            "description": "true=只返回员工清单，不下发指令"},
            "wait_sec": {"type": "number", "default": 120, "maximum": 300,
                         "description": "等员工回复的秒数；到点未完成返回 job_id 用 job_get 续等"},
            "idempotency_key": {"type": "string", "description": "重试时带同一 key 防重复下发"}},
            "required": []},
    },
]

# 外部网关会把个人 API key 注入 client_key；模型不需要也不应该手写 token/key。
for _tool in TOOLS:
    _schema = _tool["inputSchema"]
    _props = _schema.setdefault("properties", {})
    # 所有工具共用 opc_init 的 client_key：schema 里彻底移除独立的 token 字段，
    # 避免客户端误以为生成类工具要另配密钥（网关注入 client_key，个人 key 不会透传给媒体后端）。
    _props.pop("token", None)
    if _tool["name"] not in ("opc_init", "opc_login", "opc_whoami", "opc_route"):
        _props.setdefault("client_key", {
            "type": "string",
            "description": "个人 API key；外网客户端由 MCP 网关按请求头自动注入，无需手写"})
    _schema["required"] = [r for r in _schema.get("required", [])
                           if r not in ("token", "client_key")]

# 工具注解：客户端应按 readOnlyHint 过滤只读白名单，而不是硬编码工具名；
# destructiveHint 标删除类；ask 会创建后台作业（不改动用户数据，故归只读），
# 传 idempotency_key 后可安全重试。
_TOOL_ANN = {
    "opc_init": {"readOnlyHint": False, "idempotentHint": True},
    "opc_login": {"readOnlyHint": False, "idempotentHint": False},
    "opc_whoami": {"readOnlyHint": True, "idempotentHint": True},
    "kb_ping": {"readOnlyHint": True, "idempotentHint": True},
    "kb_status": {"readOnlyHint": True, "idempotentHint": True},
    "kb_search": {"readOnlyHint": True, "idempotentHint": True},
    "ask": {"readOnlyHint": True, "idempotentHint": False,
            "note": "创建后台作业并消耗 LLM 额度；带 idempotency_key 可安全重试"},
    "job_get": {"readOnlyHint": True, "idempotentHint": True},
    "material_list": {"readOnlyHint": True, "idempotentHint": True},
    "material_download": {"readOnlyHint": False, "idempotentHint": True},
    "material_analyze": {"readOnlyHint": False, "idempotentHint": False},
    "opc_resources": {"readOnlyHint": True, "idempotentHint": True},
    "opc_resource_list": {"readOnlyHint": True, "idempotentHint": True},
    "opc_resource_read": {"readOnlyHint": True, "idempotentHint": True},
    "opc_route": {"readOnlyHint": True, "idempotentHint": True},
    "media_capabilities": {"readOnlyHint": True, "idempotentHint": True},
    "file_link": {"readOnlyHint": True, "idempotentHint": True},
    "staff_photos": {"readOnlyHint": True, "idempotentHint": True},
    "tts": {"readOnlyHint": False, "idempotentHint": False},
    "image": {"readOnlyHint": False, "idempotentHint": False},
    "material_upload": {"readOnlyHint": False, "idempotentHint": False},
    "upload_begin": {"readOnlyHint": False, "idempotentHint": False},
    "material_delete": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    "kb_overview": {"readOnlyHint": True, "idempotentHint": True},
}
for _tool in TOOLS:
    _ann = dict(_TOOL_ANN.get(_tool["name"],
                              {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}))
    _ann.setdefault("destructiveHint", False)
    _ann.setdefault("idempotentHint", False)
    _tool["annotations"] = _ann


class OpcServer:
    def __init__(self):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = Store(config.DB_PATH)
        self.runner = JobRunner(self.store, on_finish=self._emit_job_event)
        self.runner.register("analyze", self._job_analyze)
        self.runner.register("ask", self._job_ask)
        self.runner.register("staff_chat", self._job_staff_chat)
        # 每 key 滑动窗口限流（S11）：phone -> 最近请求时间戳
        self._rate_lock = threading.Lock()
        self._rate_hits = {}
        # kb_overview 分层缓存：groups/rev 每 tenant 30s，recent.jobs 每 tenant 10s
        self._ov_cache = {}      # phone -> {at, rev, groups, generated_at}
        self._ov_recent = {}     # phone -> {at, jobs}
        # 幂等键「查→提交→登记」必须原子，否则并发同 key 会双排作业
        self._idem_lock = threading.Lock()

    def _emit_job_event(self, job_id, kind, status, phone, error=None):
        event = events_mod.append_event({
            "job_id": job_id, "kind": kind, "status": status,
            "phone_fp": auth.fingerprint(phone),   # 手机号只留指纹，事件帧对外不带 PII
            "error": graph_brief.public_text(error)[:300] if error else None})
        _write({"jsonrpc": "2.0", "method": "notifications/opc/job", "params": event})
        if kind == "analyze":
            # 分析完成会改变 chunks/cards/knowledge 计数 → 推 overview 变更信号
            self._overview_changed(phone)

    def _overview_changed(self, phone):
        """写操作后刷新 groups/rev 并推 overview 变更信号（事件日志 + stdio 通知）。"""
        try:
            groups, rev = overview.groups_and_rev(phone, self.store)
            fp = auth.fingerprint(phone)
            now = time.time()
            self._ov_cache[phone] = {"at": now, "rev": rev, "groups": groups,
                                     "generated_at": now}
            event = events_mod.append_event({"kind": "overview", "rev": rev, "phone_fp": fp})
            _write({"jsonrpc": "2.0", "method": "notifications/opc/overview", "params": event})
        except Exception as e:                          # 信号失败不影响业务
            log("overview 信号失败：%r" % e)

    # ---------------------------------------------------------------- 限流
    def _rate_allow(self, phone):
        """返回 (是否放行, retry_after_sec)。窗口/上限取 config，便于运维调整。"""
        now = time.time()
        window = max(1.0, float(config.RATE_WINDOW_SEC))
        with self._rate_lock:
            q = self._rate_hits.setdefault(phone, collections.deque())
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= max(1, int(config.RATE_MAX)):
                return False, max(1, int(window - (now - q[0])))
            q.append(now)
            return True, 0

    def _metric(self, kind, **fields):
        """轻量观测指标（S13）：追加 JSONL，手机号只记指纹。失败不影响业务。"""
        try:
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind}
            rec.update(fields)
            with open(config.DATA_DIR / "metrics.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---------------------------------------------------------------- 作业
    def _job_analyze(self, store, phone, payload):
        ids = payload.get("material_ids") or [payload.get("material_id")]
        out = []
        for mid in [i for i in ids if i]:
            try:
                out.append(knowledge.analyze_material(store, phone, mid))
            except Exception as e:
                out.append({"material_id": mid, "status": "failed", "error": "%s: %s" % (type(e).__name__, e)})
        return {"items": out}

    def _job_ask(self, store, phone, payload):
        question = payload["text"]
        _t0 = time.time()
        public = bool(payload.get("with_brief"))       # 对外口径：结果里不留内网地址/服务端路径
        brief = None
        if public:
            try:
                brief = graph_brief.build(store, phone, question,
                                          level=payload.get("brief_level"),
                                          budget=payload.get("brief_chars"))
            except Exception as e:                        # 简报只是增益，失败回退原链路
                log("图谱简报异常（忽略）：%s: %s" % (type(e).__name__, e))
                brief = None
        if brief and brief.get("hit"):
            context = graph_brief.merged_context(store, phone, brief)
        else:
            context = resources.context(store, phone, question)
            if public:
                context = graph_brief.public_text(context)
        hits = resources.search(store, phone, question, k=5)
        citations = [{"source": h["source"], "id": h["id"],
                      "title": graph_brief.public_text(h["title"]) if public else h["title"],
                      "score": h["score"],
                      "path": graph_brief.public_text(h["path"]) if public else h["path"]}
                     for h in hits]
        if public:                                          # 素材绝对路径换成可调用的 ref
            for c in citations:
                if str(c["path"]).startswith("/"):
                    c["path"] = ""
                    c["ref"] = "%s:%s" % ("material" if c["source"] == "mcp.material" else "knowledge", c["id"])
        history = store.messages(phone, limit=8)
        # 历史里的降级回答没有信息量，还会把故障文本滚进下一轮
        history = [m for m in history
                   if not (m.get("role") == "assistant" and "LLM 通道" in str(m.get("content") or ""))]
        if public:
            history = [{"role": m["role"], "content": graph_brief.public_text(m["content"])}
                       for m in history]
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if context:
            messages.append({"role": "system", "content": "知识库检索结果：\n" + context})
        messages += history
        messages.append({"role": "user", "content": question})

        degraded, error = False, None
        try:
            answer = llm.chat(messages, max_tokens=1400)
            if not answer:
                error = "LLM 首次返回空内容，已重试一次"
                answer = llm.chat(messages, max_tokens=1400, temperature=0.1)
            if not answer:
                raise llm.LLMError("模型未返回内容（重试后仍为空）")
        except Exception as e:
            degraded, error = True, str(e)[:300]
            if hits:
                lines = []
                for h in hits[:5]:
                    snip = _clean_snippet(h.get("snippet"))
                    lines.append("- 【%s】%s%s" % (h["source"], h.get("title") or h["id"],
                                                 ("：" + snip) if snip else ""))
                answer = "（LLM 通道暂不可用，先给知识库命中）\n" + "\n".join(lines)
            else:
                answer = "LLM 通道暂不可用，且知识库没有命中「%s」的内容。" % str(question)[:80]

        if public:
            answer = graph_brief.public_text(answer)
            error = graph_brief.public_text(error) if error else error
        store.add_message(phone, "user", question)
        store.add_message(phone, "assistant", ("[降级] %s" % (error or "LLM 通道暂不可用")[:160])
                          if degraded else answer)
        out = {"answer": answer, "degraded": degraded, "llm_error": error,
               "citations": citations, "context_chars": len(context)}
        if public:
            meta = {k: brief[k] for k in ("hit", "text", "chars", "level", "nodes", "edges",
                                          "groups", "items", "budget", "mode", "reason")} if brief else {}
            meta.setdefault("hit", False)
            meta.setdefault("text", "")
            meta.setdefault("reason", "简报不可用，已回退原链路")
            out["knowledge_brief"] = meta
        self._metric("ask", phone_fp=auth.fingerprint(phone), with_brief=public,
                     degraded=degraded, error=(error or "")[:120] or None,
                     citations=len(citations), duration_sec=round(time.time() - _t0, 2))
        return out

    def _job_staff_chat(self, store, phone, payload):
        """@员工 下指令：经工作台 /api/commands 驱动真实员工，回复落 mcp 消息线程。"""
        agent = str(payload.get("agent") or "").strip()
        message = str(payload.get("message") or "").strip()
        out = staff_chat.command(agent, message)
        answer = out.get("answer") or {}
        reply = str(answer.get("reply") or answer.get("summary") or "")
        # 指令与回复写 mcp 库 messages → 工作台 ingest.pull_mcp 摄取进消息中心 mcp:<phone>
        store.add_message(phone, "user", "@%s %s" % (agent, message))
        store.add_message(phone, "assistant", "【%s】%s" % (out.get("agent") or agent, reply))
        return {"agentId": out.get("agentId"), "agent": out.get("agent"),
                "reply": reply, "evidence": answer.get("evidence") or [],
                "confidence": answer.get("confidence"),
                "engine": out.get("engine"), "latencyMs": out.get("latencyMs"),
                "thread": "mcp:%s" % phone,
                "note": "指令与回复已写入消息中心线程 mcp:<手机号>，小秘书面板可见"}

    # ---------------------------------------------------------------- 工具实现
    def _dir(self, phone, sub):
        d = config.user_dir(phone, sub) / time.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def tool_opc_init(self, args):
        return _json(auth.init(self.store, args.get("phone"),
                               args.get("password"), args.get("key")))

    def tool_opc_login(self, args):
        return _json(auth.login(self.store, args.get("phone"), args.get("password"), args.get("key")))

    def tool_opc_whoami(self, args):
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        out = {"phone": phone, "name": user["name"], "role": user["role"],
               "phone_fp": auth.fingerprint(phone),
               "tenant": config.phone_root(phone).name}
        # 服务端路径默认不下发（S4）；detail=true 仅对白名单手机号放开
        if args.get("detail"):
            if phone not in config.DETAIL_PHONES:
                return _err("detail 模式仅对管理员开放", status="blocked", code="forbidden")
            out.update({"dataDir": str(config.phone_root(phone)),
                        "vault": str(config.user_vault(phone)),
                        "workspace": str(config.user_dir(phone))})
        return _json(out)

    def tool_material_upload(self, args):
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        keep_history = bool(args.get("keep_history"))
        supersedes = str(args.get("supersedes") or "").strip() or None
        if supersedes:
            old = self.store.material(supersedes)
            if not old or old["phone"] != phone:
                return _err("supersedes 指向的素材不存在或不属于当前账号：%s" % supersedes,
                            status="not_found", code="not_found")
        if args.get("content_base64"):
            try:
                raw = base64.b64decode(str(args["content_base64"]).split(",")[-1])
            except (binascii.Error, ValueError) as e:
                return _err("content_base64 解码失败：%s" % e,
                            status="invalid_argument", code="invalid_argument")
            filename = _safe_name(args.get("filename") or "upload.bin")
            if len(raw) > config.MAX_UPLOAD_MB * 1024 * 1024:
                return _err("base64 上传超过上限 %sMB，请改用 upload_begin 分片续传"
                            % config.MAX_UPLOAD_MB,
                            status="invalid_argument", code="invalid_argument")
            return self._ingest_bytes(phone, filename, raw,
                                      supersedes=supersedes, keep_history=keep_history)
        if args.get("path"):
            src = Path(str(args["path"])).expanduser()
            if not src.is_file():
                return _err("文件不存在或不是普通文件：%s" % src,
                            status="invalid_argument", code="invalid_argument")
            filename = _safe_name(args.get("filename") or src.name)
            size = src.stat().st_size
            if size > config.UPLOAD_MAX_MB * 1024 * 1024:
                return _err("文件超过上限 %sMB" % config.UPLOAD_MAX_MB,
                            status="invalid_argument", code="invalid_argument")
            sha = hashlib.sha256()
            with open(src, "rb") as f:
                for buf in iter(lambda: f.read(4 << 20), b""):
                    sha.update(buf)
            digest = sha.hexdigest()
            dup = self.store.one("SELECT * FROM materials WHERE phone=? AND sha256=?",
                                 (phone, digest))
            if dup:
                return _json({"material_id": dup["id"], "filename": dup["filename"],
                              "duplicate": True, "status": dup["status"],
                              "note": "相同内容已入库，未重复分析"})
            dst = self._dir(phone, "materials") / filename
            if dst.exists():
                dst = dst.with_name("%s_%s%s" % (dst.stem, digest[:6], dst.suffix))
            shutil.copyfile(src, dst)
            return self._queue(phone, filename, dst, size, digest,
                               supersedes=supersedes, keep_history=keep_history)
        return _err("需要 path 或 content_base64",
                    status="invalid_argument", code="invalid_argument")

    def _ingest_bytes(self, phone, filename, raw, supersedes=None, keep_history=False):
        sha = hashlib.sha256(raw).hexdigest()
        dup = self.store.one("SELECT * FROM materials WHERE phone=? AND sha256=?", (phone, sha))
        if dup:
            return _json({"material_id": dup["id"], "filename": dup["filename"],
                          "duplicate": True, "status": dup["status"],
                          "note": "相同内容已入库，未重复分析"})
        dst = self._dir(phone, "materials") / filename
        if dst.exists():
            dst = dst.with_name("%s_%s%s" % (dst.stem, sha[:6], dst.suffix))
        dst.write_bytes(raw)
        return self._queue(phone, filename, dst, len(raw), sha,
                           supersedes=supersedes, keep_history=keep_history)

    def _queue(self, phone, filename, dst, size, digest, supersedes=None, keep_history=False):
        mid = self.store.add_material(phone, filename, dst, size, digest, _guess_mime(filename))
        job = self.runner.submit(phone, "analyze", {"material_id": mid})
        out = {"material_id": mid, "filename": filename, "size": size,
               "path": str(dst), "job_id": job["job_id"], "status": "queued",
               "note": "已在后台分析并内化，可轮询 job_get"}
        # 版本语义（S3）：显式 supersedes 优先；同名旧版默认取代，keep_history 可保留
        sup = []
        if supersedes:
            sup.append(supersede_mod.supersede(self.store, phone, supersedes, mid))
        sup.extend(supersede_mod.supersede_older_same_name(
            self.store, phone, filename, mid, keep_history=keep_history))
        sup = [s for s in sup if s.get("ok")]
        if sup:
            out["superseded"] = [{"material_id": s["material_id"],
                                  "deleted_chunks": s["deleted_chunks"],
                                  "deleted_cards": s["deleted_cards"],
                                  "retracted_kb": s.get("retracted_kb", [])}
                                 for s in sup]
            out["note"] = ("旧版已被本版本取代（切块/卡片/内化条目不再参与召回，文件保留）；"
                           + out["note"])
        self._overview_changed(phone)               # 上传改变计数 → 推 overview 信号
        return _json(out)

    def tool_upload_begin(self, args):
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        filename = _safe_name(args.get("filename") or "upload.bin")
        try:
            size = int(args.get("size") or 0)
        except (TypeError, ValueError):
            return _err("size 必须是整数", status="invalid_argument", code="invalid_argument")
        if size <= 0:
            return _err("size 必须为正整数", status="invalid_argument", code="invalid_argument")
        if size > config.UPLOAD_MAX_MB * 1024 * 1024:
            return _err("文件超过上限 %sMB" % config.UPLOAD_MAX_MB,
                        status="invalid_argument", code="invalid_argument")
        try:
            chunk_size = int(args.get("chunk_size") or 0)
        except (TypeError, ValueError):
            return _err("chunk_size 必须是整数",
                        status="invalid_argument", code="invalid_argument")
        supersedes = str(args.get("supersedes") or "").strip() or None
        if supersedes:
            old = self.store.material(supersedes)
            if not old or old["phone"] != phone:
                return _err("supersedes 指向的素材不存在或不属于当前账号：%s" % supersedes,
                            status="not_found", code="not_found")
        keep_history = bool(args.get("keep_history"))
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        upload_id = "up_" + secrets.token_hex(8)
        path = config.UPLOAD_DIR / (upload_id + ".part")
        path.write_bytes(b"")
        self.store.add_upload(upload_id, phone, filename, size, path, chunk_size,
                              supersedes=supersedes, keep_history=keep_history)
        base = config.PUBLIC_UPLOAD_BASE
        return _json({
            "upload_id": upload_id, "filename": filename, "size": size, "offset": 0,
            "supersedes": supersedes, "keep_history": keep_history,
            "chunk_size_hint": config.UPLOAD_CHUNK_HINT, "chunk_size": chunk_size,
            "protocols": ["native-offset", "content-range", "chunk-index", "tus-1.0"],
            "auth": "Authorization: Bearer <个人 API key 换来的 session_token>，或用 ?token=/X-OPC-Token",
            "put_url": "%s/v1/uploads/%s?offset=0" % (base, upload_id),
            "part_put_url": "%s/v1/uploads/%s?part={part}" % (base, upload_id),
            "head_url": "%s/v1/uploads/%s" % (base, upload_id),
            "progress_url": "%s/v1/uploads/%s/progress" % (base, upload_id),
            "complete_url": "%s/v1/uploads/%s/complete" % (base, upload_id),
            "delete_url": "%s/v1/uploads/%s" % (base, upload_id),
            "tus_create_url": "%s/files" % base,
            "tus_url": "%s/files/%s" % (base, upload_id),
            "offset_headers": ["Upload-Offset", "X-Upload-Offset"],
            "local_base": config.LOCAL_UPLOAD_BASE,
            "next": "四种任选：①PUT ?offset=N；②PUT 带 Content-Range: bytes s-e/total；"
                    "③建单带 chunk_size 后 PUT ?part=N；④Tus: POST /files -> HEAD/PATCH /files/{id}。"
                    "中断后 GET progress_url 取 offset 续传；传满后 POST complete_url 入库。"
                    "Tus 最后一片会自动 complete。"})

    def tool_material_list(self, args):
        user = auth.resolve(self.store, args)
        try:
            limit = max(1, min(int(args.get("limit") or 50), 200))
        except (TypeError, ValueError):
            return _err("limit 必须是整数", status="invalid_argument", code="invalid_argument")
        include_superseded = bool(args.get("include_superseded"))
        offset = 0
        cursor = args.get("cursor")
        if cursor:
            try:
                offset = max(0, int(base64.urlsafe_b64decode(str(cursor) + "===")
                                    .decode("ascii", "strict")))
            except (ValueError, UnicodeError, binascii.Error):
                return _err("cursor 非法：请原样透传上一页返回的 next_cursor",
                            status="invalid_argument", code="invalid_argument")
        phone = user["phone"]
        rows = self.store.materials(phone, limit + 1, offset, include_superseded)
        has_more = len(rows) > limit
        rows = rows[:limit]
        out = {"count": len(rows),
               "total": self.store.materials_total(phone, include_superseded),
               "has_more": has_more,
               "items": [{"material_id": r["id"], "filename": r["filename"], "size": r["size"],
                          "status": r["status"], "reason": r["reason"],
                          "superseded_by": r.get("superseded_by"),
                          "created_at": _ts(r["created_at"])} for r in rows]}
        if has_more:
            out["next_cursor"] = base64.urlsafe_b64encode(
                str(offset + limit).encode("ascii")).decode("ascii")
        return _json(out)

    def tool_material_download(self, args):
        user = auth.resolve(self.store, args)
        mat = self.store.material(str(args.get("material_id") or ""))
        if not mat or mat["phone"] != user["phone"]:
            return _err("素材不存在或不属于当前账号", status="not_found", code="not_found")
        src = Path(mat["path"])
        if not src.is_file():
            return _err("素材文件已丢失：%s" % src, status="not_found", code="not_found")
        dest = args.get("dest")
        dst = Path(str(dest)).expanduser() if dest else (self._dir(user["phone"], "downloads") / mat["filename"])
        if dst.is_dir():
            dst = dst / mat["filename"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.resolve() != src.resolve():
            dst.write_bytes(src.read_bytes())
        out = {"material_id": mat["id"], "filename": mat["filename"],
               "path": str(dst), "size": mat["size"], "mime": mat["mime"]}
        if args.get("inline"):
            if mat["size"] > 4 * 1024 * 1024:
                out["note"] = "文件超过 4MB，未内联"
            else:
                out["content_base64"] = base64.b64encode(src.read_bytes()).decode()
        return _json(out)

    def tool_material_delete(self, args):
        user = auth.resolve(self.store, args)
        mid = str(args.get("material_id") or "").strip()
        if not mid:
            return _err("请提供 material_id（先用 material_list 查看）",
                        status="invalid_argument", code="invalid_argument")
        deleted = self.store.delete_material(mid, user["phone"])
        if not deleted:
            return _err("素材不存在或不属于当前账号：%s" % mid,
                        status="not_found", code="not_found")
        src = Path(str(deleted.get("path") or ""))
        file_removed, note = False, ""
        if args.get("remove_file", True) and src.is_file():
            refs = self.store.all("SELECT id FROM materials WHERE path=?", (str(src),))
            if refs:
                note = "磁盘文件仍被其他素材引用，未删除"
            else:
                try:
                    src.unlink()
                    file_removed = True
                except OSError as e:
                    note = "素材记录已删，但磁盘文件删除失败：%s" % e
        self._overview_changed(user["phone"])       # 删除改变计数 → 推 overview 信号
        return _json({
            "material_id": mid, "filename": deleted["filename"], "deleted": True,
            "deleted_chunks": deleted["deleted_chunks"], "deleted_cards": deleted["deleted_cards"],
            "path": str(src), "file_removed": file_removed, "note": note,
            "hint": "已内化进 OPC 知识库/vault 的条目不在 MCP 素材范围内，不会随之删除",
        })

    def tool_material_analyze(self, args):
        user = auth.resolve(self.store, args)
        target = str(args.get("material_id") or "")
        if target == "all":
            ids = [m["id"] for m in self.store.pending_materials(user["phone"])]
            if not ids:
                return _json({"status": "noop", "note": "没有待处理的素材"})
        else:
            mat = self.store.material(target)
            if not mat or mat["phone"] != user["phone"]:
                return _err("素材不存在或不属于当前账号", status="not_found", code="not_found")
            if mat.get("status") == "superseded":
                return _err("素材已被新版本取代（superseded_by=%s），请对最新版本操作或先确认需要恢复"
                            % mat.get("superseded_by"),
                            status="invalid_argument", code="invalid_argument")
            ids = [target]
        job = self.runner.submit(user["phone"], "analyze", {"material_ids": ids})
        return _json({"job_id": job["job_id"], "status": "queued", "count": len(ids)})

    def _job_view(self, view):
        """作业视图契约 v2：未完成带 retry_after_sec，方便所有客户端统一轮询节奏。"""
        if not view:
            return view
        view = dict(view)
        view["job_contract"] = 2
        if view.get("status") in ("queued", "running"):
            view["retry_after_sec"] = 5
        return view

    def tool_job_get(self, args):
        user = auth.resolve(self.store, args)
        job = self.runner.wait(str(args.get("job_id") or ""), user["phone"],
                               float(args.get("wait_sec") or 0))
        return _json(self._job_view(job)) if job else _err(
            "作业不存在", status="not_found", code="not_found")

    def tool_kb_status(self, args):
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        detail = bool(args.get("detail"))
        if detail and phone not in config.DETAIL_PHONES:
            return _err("detail 模式仅对管理员开放", status="blocked", code="forbidden")
        by_status = {r["status"]: r["n"] for r in self.store.all(
            "SELECT status, COUNT(*) n FROM materials WHERE phone=? GROUP BY status", (phone,))}
        total = sum(by_status.values())
        content_ids = {r["material_id"] for r in self.store.all(
            "SELECT DISTINCT material_id FROM chunks WHERE phone=? AND material_id IS NOT NULL", (phone,))}
        chunks = self.store.one("SELECT COUNT(*) n FROM chunks WHERE phone=?", (phone,))
        cards = self.store.one("SELECT COUNT(*) n FROM cards WHERE phone=?", (phone,))
        ok, detail_text, probe_info = llm.probe()
        fcfg = llm.fallback_config() or {}
        rc = resources.counts(phone, self.store)
        body = {
            "user": {"phone": phone, "name": user["name"], "role": user["role"]},
            "tenant": config.phone_root(phone).name,
            "dataDir": rc["dataDir"], "vault": rc["vault"],
            "workspace": str(config.user_dir(phone)),
            "materials": {"total": total, "by_status": by_status,
                          "content_bearing": len(content_ids),
                          "archived_only": max(0, total - len(content_ids))},
            "chunks": chunks["n"], "cards": cards["n"],
            "opc": {k: rc[k] for k in ("ownerDbExists", "knowledge", "reports", "contents",
                                       "chatMessages", "pipelineRuns", "approvals", "vaultDirs")},
            "recent_jobs": [{"job_id": j["id"], "kind": j["kind"], "status": j["status"],
                             "error": j["error"]} for j in self.store.jobs(phone, limit=5)],
            "llm": {"ok": ok, "base_url": config.LLM_BASE_URL, "model": config.LLM_MODEL,
                    "detail": None if ok else detail_text, "probe": probe_info,
                    "fallback": {"enabled": config.LLM_FALLBACK_ENABLED,
                                 "configured": bool(fcfg),
                                 "channel": fcfg.get("id") or config.LLM_FALLBACK_CHANNEL_ID,
                                 "model": fcfg.get("model") or config.LLM_FALLBACK_MODEL}},
            "media": media.health(),
        }
        if not detail:
            # 对外脱敏（S4）：不回服务端路径与内网地址，剩余字符串再过一遍净化
            body.pop("dataDir"), body.pop("vault"), body.pop("workspace")
            body["llm"].pop("base_url", None)
            return _json(graph_brief.public_obj(body))
        return _json(body)

    def tool_kb_ping(self, args):
        """轻量健康检查（S9）：缓存式三盏灯，面板轮询不再触发上游模型探测。"""
        auth.resolve(self.store, args)
        ttl = max(10, int(config.PING_TTL_SEC))
        ok, _detail, info = llm.probe_cached(ttl, force=bool(args.get("refresh")))
        mhealth = media.health_cached(max(10, int(config.MEDIA_PING_TTL_SEC)))
        checks = {
            "llm": {"ok": bool(ok)},
            "tts": {"ok": bool((mhealth.get("tts") or {}).get("ok"))},
            "image": {"ok": bool((mhealth.get("image") or {}).get("ok"))},
        }
        if info.get("retry_after_sec"):
            checks["llm"]["retry_after_sec"] = info["retry_after_sec"]
            checks["llm"]["cooldown_until"] = info.get("cooldown_until")
        all_ok = all(c["ok"] for c in checks.values())
        return _json({"ok": all_ok, "degraded": not all_ok, "checks": checks,
                      "cached_for_sec": ttl,
                      "note": "结果按缓存返回；refresh=true 立即重探。面板轮询请用本工具，"
                              "kb_status 会真实探测上游模型。"})

    # ---------------------------------------------------------------- kb_overview
    def _overview_health(self, force=False):
        """三盏健康灯：与 kb_ping 共用同一份缓存，不重复探测上游。失败隔离（不拖垮整页）。

        LLM 冷缓存时走后台探测（stale-while-revalidate，probe_background），
        本次回 status=probing，首屏绝不被上游模型探测拖住；下一次调用即可见真值。
        refresh=true（force）才同步重探，等一个确定答案。
        """
        try:
            if force:
                ok, _detail, info = llm.probe_cached(config.PING_TTL_SEC, force=True)
            else:
                ok, info = llm.probe_background(config.PING_TTL_SEC)
            if ok is None:                              # 冷缓存/探测中：不阻塞
                llm_item = {"ok": None, "status": "probing",
                            "checked_at": info.get("checked_at")}
            else:
                llm_item = {"ok": bool(ok), "degraded": not ok,
                            "checked_at": info.get("checked_at")}
                if info.get("retry_after_sec"):
                    llm_item["retry_after_sec"] = info["retry_after_sec"]
                    llm_item["cooldown_until"] = info.get("cooldown_until")
        except Exception as e:                          # 探测失败不拖垮概览
            log("overview LLM 探测失败：%r" % e)
            llm_item = {"ok": False, "degraded": True, "status": "unknown"}
        try:
            mh = media.health_cached(config.MEDIA_PING_TTL_SEC, force=force)
            tts_item = {"ok": bool((mh.get("tts") or {}).get("ok")),
                        "checked_at": mh.get("checked_at")}
            img_item = {"ok": bool((mh.get("image") or {}).get("ok")),
                        "checked_at": mh.get("checked_at")}
        except Exception as e:
            log("overview 媒体探测失败：%r" % e)
            tts_item = {"ok": False, "status": "unknown"}
            img_item = {"ok": False, "status": "unknown"}
        health = {"llm": llm_item, "tts": tts_item, "image": img_item}
        # degraded 只看明确不可用（False）；probing(None) 是"未知"，不算降级
        return health, any(v.get("ok") is False for v in health.values())

    def _overview_recent(self, phone):
        """近期作业：单独 10s 短 TTL 缓存。"""
        now = time.time()
        cache = self._ov_recent.get(phone)
        if cache and now - cache["at"] < config.OVERVIEW_RECENT_TTL_SEC:
            return cache["jobs"]
        jobs = [{"job_id": j["id"], "kind": j["kind"], "status": j["status"],
                 "error": graph_brief.public_text(j["error"])[:200] if j.get("error") else None,
                 "updated_at": j.get("updated_at")}
                for j in self.store.jobs(phone, limit=5)]
        self._ov_recent[phone] = {"at": now, "jobs": jobs}
        return jobs

    def tool_kb_overview(self, args):
        """概览一次返回（条件请求 + 分层缓存）。

        条件命中（rev 相同且缓存新鲜）≈ 60 字节且不计入限流；其余路径正常计数。
        """
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        detail, error = self._resource_detail(args, phone)
        if error:
            return error
        want = [f.strip() for f in str(args.get("fields") or "").split(",") if f.strip()]
        fields = set(want) if want else None
        client_rev = str(args.get("rev") or "").strip()
        refresh = bool(args.get("refresh"))
        if refresh:
            # 指定刷新：丢弃本 tenant 的 groups/rev 与 recent 缓存，健康灯强制重探
            self._ov_cache.pop(phone, None)
            self._ov_recent.pop(phone, None)
        ttl = config.OVERVIEW_TTL_SEC
        now = time.time()
        cache = self._ov_cache.get(phone)
        if cache and now - cache["at"] < ttl and client_rev and client_rev == cache["rev"]:
            return _json({"rev": cache["rev"], "generated_at": cache["generated_at"],
                          "ttl_sec": ttl, "changed": False})
        # 变化/首屏路径才计入限流（dispatch 层对 kb_overview 豁免）
        allowed, retry = self._rate_allow(phone)
        if not allowed:
            return _err("请求过于频繁（每 %g 秒最多 %d 次），请 %d 秒后重试"
                        % (config.RATE_WINDOW_SEC, config.RATE_MAX, retry),
                        status="rate_limited", code="rate_limited", retry_after_sec=retry)
        if cache and now - cache["at"] < ttl:
            groups, rev = cache["groups"], cache["rev"]
            generated_at = cache["generated_at"]
        else:
            try:
                groups, rev = overview.groups_and_rev(phone, self.store)
            except Exception as e:
                log("overview 计数失败：%r" % e)
                return _err("概览计数失败：%s" % type(e).__name__,
                            code="internal_error")
            generated_at = now
            self._ov_cache[phone] = {"at": now, "rev": rev, "groups": groups,
                                     "generated_at": generated_at}
        out = {"rev": rev, "generated_at": generated_at, "ttl_sec": ttl,
               "changed": not (client_rev and client_rev == rev)}
        if fields is None or "groups" in fields:
            out["groups"] = groups
        if fields is None or "health" in fields:
            health, degraded = self._overview_health(force=refresh)
            out["health"] = health
            out["degraded"] = degraded
        if fields is None or "owner" in fields:
            out["owner"] = {"name": user["name"], "role": user["role"],
                            "phone_fp": auth.fingerprint(phone)}
        if fields is None or "recent" in fields:
            out["recent"] = {"jobs": self._overview_recent(phone)}
        if detail:
            rc = resources.counts(phone, self.store)
            out["paths"] = {"dataDir": rc["dataDir"], "vault": rc["vault"]}
        return _json(out)

    def tool_kb_search(self, args):
        user = auth.resolve(self.store, args)
        query = str(args.get("query") or "")
        hits = resources.search(self.store, user["phone"], query, int(args.get("k") or 6))
        # 八步执行法过滤：reasoning=N 只保留主步骤为 N 的命中
        try:
            want = int(args.get("reasoning") or 0)
        except (TypeError, ValueError):
            want = 0
        if 1 <= want <= 8:
            hits = [h for h in hits if h.get("reasoning") == want]
        dist = {}
        for h in hits:
            step = h.get("reasoning")
            if step:
                dist[str(step)] = dist.get(str(step), 0) + 1
        # 低置信度（S8）：最高分低于阈值，或查询词覆盖率不足一半（常用词误命中）
        qterms = knowledge.terms(query)
        top = hits[0] if hits else None
        top_score = float(top["score"]) if top else 0.0
        coverage = ((top.get("matched", 0) / len(qterms))
                    if top and qterms else 0.0)
        low = (not hits) or top_score < config.LOW_CONFIDENCE_SCORE or coverage < 0.5
        out = {"query": query, "count": len(hits), "hits": hits,
               "low_confidence": low,
               "reasoning_legend": [{"step": s, "name": n} for s, n in resources.REASONING_STEPS],
               "reasoning_steps": dist,
               "sources": ["opc.knowledge", "opc.report", "opc.content",
                           "opc.message", "mcp.material",
                           "shared.knowledge", "shared.material", "shared.hotspot"]}
        if low:
            out["hint"] = ("最高分 %.1f（阈值 %.1f）、查询词覆盖率 %.0f%%，命中可能与问题无关；"
                           "建议换用自然语言描述（少用标识符/路径/参数名堆叠）后再试"
                           % (top_score, config.LOW_CONFIDENCE_SCORE, coverage * 100))
        return _json(out)

    def _resource_detail(self, args, phone):
        """detail=true 白名单校验；返回 (detail, 错误响应)。"""
        detail = bool(args.get("detail"))
        if detail and phone not in config.DETAIL_PHONES:
            return None, _err("detail 模式仅对管理员开放", status="blocked", code="forbidden")
        return detail, None

    def tool_opc_resources(self, args):
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        detail, error = self._resource_detail(args, phone)
        if error:
            return error
        out = resources.counts(phone, self.store)
        if not detail:
            for key in ("dataDir", "vault", "ownerDb"):
                out.pop(key, None)
            return _json(graph_brief.public_obj(out))
        return _json(out)

    def tool_opc_resource_list(self, args):
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        detail, error = self._resource_detail(args, phone)
        if error:
            return error
        out = resources.list_kind(phone, str(args.get("kind") or "knowledge"),
                                  int(args.get("limit") or 20))
        return _json(out if detail else graph_brief.public_obj(out))

    def tool_opc_resource_read(self, args):
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        detail, error = self._resource_detail(args, phone)
        if error:
            return error
        out, error = resources.read(phone, args.get("ref"), self.store)
        return _err(error, status="blocked") if error else _json(
            out if detail else graph_brief.public_obj(out))

    def tool_ask(self, args):
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        text = str(args.get("text") or "").strip()
        alt = str(args.get("question") or "").strip()
        if text and alt and text != alt:
            return _err("text 与 question 是同义字段，只能提供一个（内容需一致）",
                        status="invalid_argument", code="invalid_argument")
        question = text or alt
        if not question:
            return _err("请提供问题内容：text 或 question 任填一项",
                        status="invalid_argument", code="invalid_argument")
        try:
            level = int(args["brief_level"]) if args.get("brief_level") is not None else None
            chars = int(args["brief_chars"]) if args.get("brief_chars") is not None else None
        except (TypeError, ValueError):
            return _err("brief_level / brief_chars 必须是整数",
                        status="invalid_argument", code="invalid_argument")
        if level is not None:
            level = max(0, min(2, level))
        if chars is not None:
            chars = max(200, min(config.BRIEF_HARD_MAX, chars))
        try:
            wait = max(0.0, min(float(args.get("wait_sec") or 0), 300.0))
        except (TypeError, ValueError):
            return _err("wait_sec 必须是数字", status="invalid_argument", code="invalid_argument")

        # 幂等（S1）：「查重→提交→登记」在同一把锁内完成，并发同 key 不双排作业
        idem = str(args.get("idempotency_key") or "").strip()[:128]
        map_key = None
        if idem:
            map_key = hashlib.sha256(("%s|%s" % (phone, idem)).encode("utf-8")).hexdigest()
        with self._idem_lock:
            if map_key:
                prev = self.store.get_idempotent(map_key, config.IDEMPOTENCY_TTL)
                if prev:
                    view = self.runner.wait(prev, phone, wait) if wait > 0 \
                        else self.runner.view(prev, phone)
                    if view:
                        view = self._job_view(view)
                        view["idempotent_replay"] = True
                        return _json(view)
            job = self.runner.submit(phone, "ask", {
                "text": question, "with_brief": bool(args.get("with_brief")),
                "brief_level": level, "brief_chars": chars})
            if map_key:
                self.store.put_idempotent(map_key, job["job_id"], phone)
        if wait > 0:
            return _json(self._job_view(self.runner.wait(job["job_id"], phone, wait)))
        return _json(self._job_view(job))

    def tool_opc_route(self, args):
        query = str(args.get("query") or "").strip()
        low = query.lower()
        kb_words = ("知识库", "知识管理", "opc", "同心圆", "知识图谱", "图数据库")
        knowledge_related = any(w.lower() in low for w in kb_words)

        rules = (
            (("身份", "我是谁", "手机号", "根目录", "租户", "whoami"), "opc_whoami",
             "确认当前 key 对应的手机号与 OPC 根目录"),
            (("状态", "多少", "统计", "健康", "可用", "探活", "status"), "kb_status",
             "看素材/片段/卡片数与 LLM 状态"),
            (("没分析", "未分析", "待分析", "没处理", "待处理", "pending", "unanalyzed"),
             "material_list",
             "素材是否已分析看 material_list 的 status；要补分析再 material_analyze(all=true)"),
            (("上传", "导入", "传文件", "upload"), "upload_begin",
             "大文件走 upload_begin 分片；<100MB 可用 material_upload"),
            (("共享", "公开", "公共", "下载区", "全局", "shared", "downloads"),
             "opc_resource_list",
             "全局共享走 opc_resource_list(kind=shared_materials 或 downloads)"),
            (("列出", "我的素材", "我的下载", "导出", "list", "download"), "material_list",
             "本人素材用 material_list：先 list 拿 material_id 再 download"),
            (("删除", "删掉", "删素材", "移除", "delete"), "material_delete",
             "先用 material_list 拿 material_id，再 material_delete 删除本人素材"),
            (("重新分析", "再分析", "analyze"), "material_analyze",
             "提交分析后用 job_get 等结果"),
            (("语音", "朗读", "配音", "tts"), "tts", "文本转语音；阿里云百炼云服务用 channel=bailian，601 个音色先查 media_capabilities"),
            (("百炼", "阿里云", "dashscope", "云服务音色", "qwen-audio"), "tts",
             "阿里云百炼 TTS：channel=bailian，音色查 media_capabilities.audio.bailianVoices"),
            # @员工/下指令是对话通道；「照片」类词留给 staff_photos，注意顺序
            (("@员工", "员工对话", "点名员工", "找员工", "给员工", "叫员工", "对员工",
              "通知员工", "提醒员工", "下达指令", "派单给"),
             "staff_chat", "给具体员工下指令用 staff_chat（仅老板 key），先 list_agents 查名单"),
            # 员工照片/半身照是查现成图，不是文生图；必须排在通用「图片/图像」之前
            (("员工照片", "员工头像", "员工形象", "半身照", "半身像", "本人形象",
              "数字人形象", "口播形象", "员工照", "staff photo", "portrait"),
             "staff_photos", "现成员工照片用 staff_photos 列出并下载，不是 image 生图"),
            (("图片", "画图", "生图", "图像", "image"), "image",
             "文生图；阿里云百炼出图用 channel=bailian_image，尺寸参数 size=1024*1024"),
            (("检索", "查找", "原文", "出处", "有没有", "在哪里", "搜索", "search"), "kb_search",
             "关键词检索拿片段与出处"),
            (("总结", "解释", "为什么", "怎么办", "如何", "对比", "问答", "回答", "ask"), "ask",
             "带引用的综合问答（参数名 text）"),
        )
        matched = []
        for words, tool, why in rules:
            if any(w.lower() in low for w in words):
                matched.append((tool, why))
        # 「共享/公开下载区」与「本人素材」是两条互斥路径：前者命中后不再推 material_list，
        # 除非用户同时明确说了「我的素材」。
        if any(t == "opc_resource_list" for t, _ in matched) and "我的素材" not in low:
            matched = [x for x in matched if x[0] not in ("material_list", "material_download")]
        # "我上传的素材里有哪些没分析"：问的是状态，不是再传一遍，上传规则要让位给 material_list。
        if any(w in low for w in ("没分析", "未分析", "待分析", "没处理", "待处理", "pending", "unanalyzed")):
            matched = [x for x in matched if x[0] not in ("material_upload", "upload_begin")]
            if not any(t == "material_list" for t, _ in matched):
                matched.insert(0, ("material_list", "素材状态看 material_list 的 status"))
        # "删除我上传的素材"会同时命中上传规则；删除优先，并补一步 list 拿 id。
        if any(t == "material_delete" for t, _ in matched):
            matched = [x for x in matched if x[0] not in ("material_upload", "upload_begin")]
            if not any(t == "material_list" for t, _ in matched):
                matched.insert(0, ("material_list", "先用 material_list 拿 material_id"))
        if not matched:
            matched = [("kb_search", "关键词检索拿片段与出处"),
                       ("ask", "带引用的综合问答（参数名 text）")]

        ordered = list(dict.fromkeys(t for t, _ in matched))
        non_kb = ("material_upload", "upload_begin", "material_list", "material_download",
                  "material_delete", "tts", "image")
        steps = []
        if not knowledge_related and ordered and ordered[0] in non_kb:
            steps.append("opc_whoami")
        steps += ordered
        if knowledge_related and "kb_search" in steps and "ask" not in steps:
            steps.append("ask")
        steps = list(dict.fromkeys(steps))

        templates = {
            "opc_whoami": "opc_whoami()",
            "kb_status": "kb_status()",
            "kb_overview": "kb_overview()｜概览页一次返回，之后传 rev 做条件轮询",
            "kb_search": 'kb_search(query="<关键词>", k=6)',
            "ask": 'ask(text="<你的问题>", wait_sec=60)｜库内事实问题加 with_brief=true 可省掉 kb_search',
            "material_list": "material_list(limit=50)",
            "opc_resource_list": 'opc_resource_list(kind="shared_materials", limit=20)｜'
                                 '公开下载区用 opc_resource_list(kind="downloads", limit=20)',
            "material_download": 'material_download(material_id="<mat_id>")',
            "material_delete": 'material_delete(material_id="<mat_id>")',
            "material_upload": 'material_upload(path="<本地文件路径>")',
            "upload_begin": 'upload_begin(filename="<名称>", size=<字节数>, chunk_size=8388608)',
            "material_analyze": 'material_analyze(material_id="<mat_id> 或 all>")',
            "job_get": 'job_get(job_id="<job_id>", wait_sec=60)',
            "tts": 'tts(text="<要朗读的文字>")',
            "image": 'image(prompt="<画面描述>")',
            "staff_photos": 'staff_photos(kind="all")｜半身照用 kind="halfbody"；返回 download_url 直接 curl',
        }
        return _json({
            "query": query,
            "knowledge_related": knowledge_related,
            "recommended": [t for t, _ in matched],
            "steps": steps,
            "templates": {t: templates[t] for t in steps if t in templates},
            "notes": "身份由 key 自带，不要传 phone；ask 的问题内容用 text 或 question 均可；"
                     "库内事实类问题直接 ask(with_brief=true)：自带五行三才图谱简报，有硬预算，不必先 kb_search；"
                     "检索不到要明确说明，不要用通用知识冒充公司内部资料。",
        })

    def _publish_generated(self, res, phone, kind, mime):
        """给生成产物发不透明 file_key，并登记到 Store；返回可跨网下载的 URL。

        落盘路径只用于登记与推断文件名，不外泄给客户端；取回统一走 file_key。
        """
        path = str(res.pop("path", "") or "")
        res.pop("prompt_wav", None)
        res.pop("prompt_text", None)
        if not path or not Path(path).is_file():
            return res
        file_key = secrets.token_urlsafe(18).replace("-", "").replace("_", "")
        self.store.add_generated_file(file_key, phone, path, kind,
                                      filename=Path(path).name, mime=mime,
                                      size=Path(path).stat().st_size)
        res["file_key"] = file_key
        res["download_url"] = "%s/api/mcp/file/%s" % (config.PUBLIC_WORKBENCH_BASE, file_key)
        res["download_auth"] = "X-Mail-Agent: <生成时用的同一把个人 API key>（或用 Bearer session_token）"
        res["download_example"] = ('curl -L -H "X-Mail-Agent: $OPC_KEY" '
                                   '"%s/api/mcp/file/%s" -o "%s"'
                                   % (config.PUBLIC_WORKBENCH_BASE, file_key, Path(path).name))
        return res

    def tool_tts(self, args):
        user = auth.resolve(self.store, args)
        out_dir = self._dir(user["phone"], "audio")
        channel = args.get("channel")
        voice = args.get("voice")
        if channel == "bailian" and not voice:
            voice = "longanhuan_v3.6"       # 601 个里唯一 Key 实测稳定可用的默认音色
        try:
            res = media.tts(str(args.get("text") or ""), out_dir,
                            voice=voice, engine=args.get("engine") or "cosyvoice",
                            channel=channel, notes=args.get("notes"),
                            brief=args.get("brief"),
                            speed=float(args.get("speed") or 1.0),
                            wait_sec=float(args.get("wait_sec") or 180),
                            fmt=args.get("format") or "mp3",
                            sample_rate=int(args.get("sample_rate") or 22050))
        except media.MediaError as e:
            return _err(str(e), status="media_error")
        suffix = str(res.get("path", "")).lower()
        mime = "audio/wav" if suffix.endswith(".wav") else (
            "audio/mpeg" if suffix.endswith(".mp3") else "audio/ogg")
        return _json(self._publish_generated(res, user["phone"], "audio", mime))

    def tool_image(self, args):
        user = auth.resolve(self.store, args)
        out_dir = self._dir(user["phone"], "images")
        try:
            res = media.image(str(args.get("prompt") or ""), out_dir,
                              width=int(args.get("width") or 640),
                              height=int(args.get("height") or 832),
                              steps=int(args.get("steps") or 20),
                              negative=args.get("negative_prompt"),
                              seed=args.get("seed"),
                              channel=args.get("channel") or "sd15",
                              reference_image=args.get("reference_image"),
                              brief=args.get("brief"),
                              wait_sec=float(args.get("wait_sec") or 300),
                              size=args.get("size") or "1024*1024",
                              n=int(args.get("n") or 1),
                              prompt_extend=bool(args.get("prompt_extend", True)),
                              watermark=bool(args.get("watermark", False)))
        except media.MediaError as e:
            return _err(str(e), status="media_error")
        return _json(self._publish_generated(res, user["phone"], "image",
                                             "image/png" if str(res.get("path", "")).endswith(".png")
                                             else "image/jpeg"))

    def tool_media_capabilities(self, args):
        """生成能力查询：与注册共用 client_key，不引入第二套鉴权。

        对外口径与 ask(with_brief) 一致：剥离参考音频/模型权重的落盘路径，
        内网地址换占位，避免远程 agent 照抄返回时泄漏服务端路径。
        """
        auth.resolve(self.store, args)
        return _json(graph_brief.public_obj(media.capabilities()))

    def tool_file_link(self, args):
        """已生成产物重新取下载链接（同一把 key；只能取自己的）。"""
        user = auth.resolve(self.store, args)
        key = str(args.get("file_key") or "").strip()
        row = self.store.generated_file(key, user["phone"]) if key else None
        if not row:
            return _err("file_key 不存在或不属于当前用户", status="not_found")
        path = Path(row["path"] or "")
        if not path.is_file():
            return _err("产物文件已不存在（可能已过期清理）", status="gone")
        return _json({"file_key": key, "filename": row["filename"] or path.name,
                      "size": row["size"], "kind": row["kind"],
                      "download_url": "%s/api/mcp/file/%s" % (config.PUBLIC_WORKBENCH_BASE, key),
                      "download_auth": "X-Mail-Agent: <生成时用的同一把个人 API key>",
                      "download_example": ('curl -L -H "X-Mail-Agent: $OPC_KEY" '
                                           '"%s/api/mcp/file/%s" -o "%s"'
                                           % (config.PUBLIC_WORKBENCH_BASE, key,
                                              row["filename"] or path.name))})

    def tool_staff_photos(self, args):
        """员工照片/半身照清单：全局共享，任意有效 key 可见；只回 download_url，不回服务端路径。"""
        auth.resolve(self.store, args)       # 只需有效 key；照片属公司共享资料，不按 phone 隔离
        items, error = staff_photos.listing(args.get("kind") or "all",
                                            limit=args.get("limit") or 200)
        if error:
            return _err(error, status="bad_kind")
        return _json({"scope": "global-shared", "count": len(items),
                      "items": graph_brief.public_obj(items),
                      "download_auth": "X-Mail-Agent: <任意已注册的同一把个人 API key>",
                      "note": "file_key 跨重启稳定；下载链接支持 Range。照片为公司共享资料，任何 key 可下载。"})

    def tool_staff_chat(self, args):
        """员工对话通道（仅老板 key）：@员工 下指令，工作台真实员工回复。"""
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        if phone not in config.DETAIL_PHONES:
            return _err("员工对话通道仅对老板开放",
                        status="blocked", code="forbidden")
        if args.get("list_agents"):
            try:
                return _json({"ok": True, "agents": staff_chat.agents(),
                              "note": "agent 参数填 id 或姓名均可"})
            except staff_chat.StaffChatError as e:
                return _err(str(e), status=e.status, code=e.code)
        agent = str(args.get("agent") or "").strip()
        message = str(args.get("message") or "").strip()
        if not agent:
            return _err("请指定员工：agent 填员工 id 或姓名（list_agents=true 可查清单）",
                        status="invalid_argument", code="invalid_argument")
        if not message:
            return _err("请提供要下发的指令内容：message",
                        status="invalid_argument", code="invalid_argument")
        try:
            wait = max(0.0, min(float(args.get("wait_sec") or 0), 300.0))
        except (TypeError, ValueError):
            return _err("wait_sec 必须是数字", status="invalid_argument", code="invalid_argument")
        # 幂等：与 ask 同一套「查重→提交→登记」锁内流程，重试同 key 不重复下发指令
        idem = str(args.get("idempotency_key") or "").strip()[:128]
        map_key = None
        if idem:
            map_key = hashlib.sha256(("%s|%s" % (phone, idem)).encode("utf-8")).hexdigest()
        with self._idem_lock:
            if map_key:
                prev = self.store.get_idempotent(map_key, config.IDEMPOTENCY_TTL)
                if prev:
                    view = self.runner.wait(prev, phone, wait) if wait > 0 \
                        else self.runner.view(prev, phone)
                    if view:
                        view = self._job_view(view)
                        view["idempotent_replay"] = True
                        return _json(view)
            job = self.runner.submit(phone, "staff_chat",
                                     {"agent": agent, "message": message})
            if map_key:
                self.store.put_idempotent(map_key, job["job_id"], phone)
        if wait > 0:
            return _json(self._job_view(self.runner.wait(job["job_id"], phone, wait)))
        return _json(self._job_view(job))

    # ---------------------------------------------------------------- 协议
    def dispatch(self, name, args):
        fn = getattr(self, "tool_" + name, None)
        if not fn:
            return _err("未知工具：%s" % name, status="not_found", code="not_found")
        args = args or {}
        # 每 key 限流（S11）：面板/脚本反复点击也不会变成压测；鉴权类工具不计数。
        # kb_overview 在工具内部按路径计数（条件命中/缓存命中不计），这里也豁免。
        if name not in ("opc_init", "opc_login", "opc_route", "kb_overview"):
            try:
                _user = auth.resolve(self.store, args)
            except auth.AuthError:
                _user = None
            if _user:
                allowed, retry = self._rate_allow(_user["phone"])
                if not allowed:
                    return _err("请求过于频繁（每 %g 秒最多 %d 次），请 %d 秒后重试"
                                % (config.RATE_WINDOW_SEC, config.RATE_MAX, retry),
                                status="rate_limited", code="rate_limited",
                                retry_after_sec=retry)
        try:
            return fn(args)
        except auth.AuthError as e:
            return _err(str(e), status="need_auth",
                        next="未领取 key：打开 %s 登录后自助生成；" % config.KEY_APPLY_URL
                             + "已领取：配置请求头 X-Mail-Agent=<个人 API key>，"
                               "或只调用 opc_init(key=...) 获取 session_token（不需要手机号）")
        except Exception as e:
            log("工具 %s 异常：%r" % (name, e))
            return _err("%s: %s" % (type(e).__name__, e), code="internal_error")

    def handle(self, msg):
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}, "resources": {}},
                "serverInfo": {"name": "opc-mcp", "version": __version__}}}
        if mid is None:
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            result = self.dispatch(params.get("name"), params.get("arguments"))
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        if method in ("resources/list", "resources/templates/list"):
            return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "Method not found: %s" % method}}

    def _serve_line(self, line):
        """单条 JSON-RPC 的处理入口：在请求线程池里执行，响应按 id 回写。"""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error: %s" % e}})
            return
        try:
            resp = self.handle(msg)
        except Exception as e:                      # 单请求异常不能带垮整个服务
            log("请求处理异常：%r" % e)
            mid = msg.get("id") if isinstance(msg, dict) else None
            resp = {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": "%s: %s" % (type(e).__name__, e)}} \
                if mid is not None else None
        if resp is not None:
            _write(resp)

    def _start_overview_refresher(self):
        """后台定时刷新：让 kb_overview 缓存永不冷启动。

        每 3s 醒一次，只对「已有缓存条目的 tenant」做到期检查（自调压，无面板就不干活）：
        - groups/rev：到期（30s）才重算一次；
        - recent.jobs：到期（10s）才重查一次；
        - 健康灯：probe/health_cached 内部按各自 TTL（60s/30s）自行节流。
        """
        def _loop():
            log("overview refresher started (groups=%ds recent=%ds)"
                % (config.OVERVIEW_TTL_SEC, config.OVERVIEW_RECENT_TTL_SEC))
            while True:
                time.sleep(3)
                now = time.time()
                try:
                    for phone, cache in list(self._ov_cache.items()):
                        if now - cache["at"] >= config.OVERVIEW_TTL_SEC:
                            groups, rev = overview.groups_and_rev(phone, self.store)
                            self._ov_cache[phone] = {"at": time.time(), "rev": rev,
                                                     "groups": groups, "generated_at": time.time()}
                except Exception as e:
                    log("overview 后台刷新异常（忽略）：%r" % e)
                try:
                    for phone in list(self._ov_recent):
                        self._overview_recent(phone)     # 内部按 10s TTL 节流
                except Exception as e:
                    log("overview recent 后台刷新异常（忽略）：%r" % e)
                try:
                    llm.probe_cached(config.PING_TTL_SEC)          # 到期才真探
                    media.health_cached(config.MEDIA_PING_TTL_SEC)
                except Exception:
                    pass
        t = threading.Thread(target=_loop, name="opc-ov-refresh", daemon=True)
        t.start()
        return t

    def serve(self):
        """stdin 单读线程 + 请求线程池：长等待调用不再阻塞其它请求。

        JSON-RPC 靠 id 匹配响应，允许乱序；stdout 写入由 _STDOUT_LOCK 串行化，
        作业完成通知也走同一个锁，保证每行都是完整 JSON。
        """
        n = config.REQUEST_WORKERS
        log("ready db=%s vault=%s request_workers=%d" % (config.DB_PATH, config.VAULT, n))
        self._start_overview_refresher()
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="opc-req") as pool:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    pool.submit(self._serve_line, line)
        # EOF 时 with 退出，等待在途请求处理完毕再结束进程


def _write(obj):
    """原子写一行 JSON-RPC（响应或通知）到 stdout。"""
    with _STDOUT_LOCK:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

def _ts(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)) if epoch else None


def _guess_mime(name):
    ext = Path(name).suffix.lower()
    return {".md": "text/markdown", ".txt": "text/plain", ".json": "application/json",
            ".csv": "text/csv", ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".wav": "audio/wav", ".mp3": "audio/mpeg",
            ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".srt": "text/plain", ".mp4": "video/mp4"}.get(ext, "application/octet-stream")


def main():
    OpcServer().serve()


if __name__ == "__main__":
    main()
