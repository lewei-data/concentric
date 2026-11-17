"""MCP stdio 服务：JSON-RPC 2.0，暴露初始化/素材/分片续传/知识库/跨源资源/路由/对话/TTS/图像共 18 个工具。"""

import base64
import binascii
import hashlib
import json
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

from . import __version__, auth, config, knowledge, llm, media, resources
from .jobs import JobRunner
from .store import Store

PROTOCOL = "2024-11-05"
SYSTEM_PROMPT = ("你是 OPC 公司的知识助手。优先依据给出的知识库资料回答，资料不足就明确说缺什么，"
                 "不要编造。回答用中文，先给结论，再给依据。")


def log(msg):
    sys.stderr.write("[opc-mcp] %s\n" % msg)
    sys.stderr.flush()


def _json(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}],
            "isError": False}


def _err(msg, **extra):
    body = {"ok": False, "error": str(msg)}
    body.update(extra)
    return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
            "isError": True}


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
                       "需要跨网络断点续传时改用 upload_begin。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"},
            "path": {"type": "string", "description": "服务端可读的本地文件路径"},
            "content_base64": {"type": "string", "description": "文件内容 base64（与 path 二选一）"},
            "filename": {"type": "string", "description": "content_base64 时的文件名，含扩展名"}},
            "required": ["token"]},
    },
    {
        "name": "upload_begin",
        "description": "大文件分片续传建单（zip/pdf/doc 等，单文件最大 2GB）。兼容四种协议："
                       "①PUT ?offset=N；②Content-Range: bytes s-e/total（bytes */* 查状态）；"
                       "③片号 ?part=N（建单带 chunk_size）；④Tus 1.0：POST /files + HEAD/PATCH /files/{id}。"
                       "返回 put_url/part_put_url/tus_url/tus_create_url/progress_url/complete_url。"
                       "进度接口含 percent/已传/剩余/速率/ETA；断电后 GET progress_url 取 offset 续传。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"},
            "filename": {"type": "string", "description": "含扩展名的文件名，如 资料.zip、合同.pdf"},
            "size": {"type": "integer", "description": "文件总字节数（必须精确）"},
            "chunk_size": {"type": "integer", "description": "可选：片号模式的分片字节数，需固定"}},
            "required": ["token", "filename", "size"]},
    },
    {
        "name": "material_list",
        "description": "列出本人已上传素材及其分析状态。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"}, "limit": {"type": "integer", "default": 50}},
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
                       "当用户提到知识库/知识管理/OPC/同心圆/知识图谱/图数据库，想先看数据规模或是否可用时优先调用。",
        "inputSchema": {"type": "object", "properties": {"token": {"type": "string"}},
                        "required": ["token"]},
    },
    {
        "name": "kb_search",
        "description": "在本人知识库做关键词检索，返回最相关片段与出处。当用户提到知识库/知识管理/OPC/同心圆/"
                       "知识图谱/图数据库，或要查原文、找出处、确认『有没有/在哪里』时优先调用（配合 ask 做综合问答）。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"}, "query": {"type": "string"},
            "k": {"type": "integer", "default": 6}},
            "required": ["token", "query"]},
    },
    {
        "name": "ask",
        "description": "文本问答：后台结合知识库内化内容调用 LLM 生成结果；wait_sec>0 可直接拿答案。"
                       "当用户提到知识库/知识管理/OPC/同心圆/知识图谱/图数据库，或要求总结/解释/对比/给结论并带引用时"
                       "优先调用。问题内容用 text 或 question 任一传入即可（两者等价）。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"},
            "text": {"type": "string", "description": "问题内容；与 question 二选一"},
            "question": {"type": "string", "description": "同 text，问题内容；二选一即可"},
            "wait_sec": {"type": "number", "default": 0, "description": "0=只返回作业号"}},
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
                       "想确认『key 能看到哪些东西』或跨源检索前先调本工具。",
        "inputSchema": {"type": "object", "properties": {}},
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
            "limit": {"type": "integer", "default": 20}},
            "required": []},
    },
    {
        "name": "opc_resource_read",
        "description": "读取单个资源：本人 knowledge:kb_xxx / report:<id> / content:<id> / "
                       "vault:相对路径 / material:<mat_id>；全局共享 shared_knowledge:<id> / "
                       "shared_material:<id> / shared_hotspot:<id> / shared_download:<文件名>。"
                       "敏感文件按安全策略屏蔽；超过 2MB 请用 kb_search 检索片段。",
        "inputSchema": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "<类型>:<标识>"}},
            "required": ["ref"]},
    },
    {
        "name": "tts",
        "description": "文本转语音，产出 wav 文件路径（media-engine CosyVoice/Qwen3）。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"}, "text": {"type": "string"},
            "voice": {"type": "string", "description": "音色名或 speaker"},
            "engine": {"type": "string", "enum": ["cosyvoice", "qwen3"], "default": "cosyvoice"},
            "speed": {"type": "number", "default": 1.0},
            "wait_sec": {"type": "number", "default": 180}},
            "required": ["token", "text"]},
    },
    {
        "name": "image",
        "description": "文生图（media-gen sd15），产出图片文件路径。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"}, "prompt": {"type": "string"},
            "negative_prompt": {"type": "string"},
            "width": {"type": "integer", "default": 640},
            "height": {"type": "integer", "default": 832},
            "steps": {"type": "integer", "default": 20},
            "seed": {"type": "integer"},
            "wait_sec": {"type": "number", "default": 300}},
            "required": ["token", "prompt"]},
    },
]

# 外部网关会把个人 API key 注入 client_key；模型不需要也不应该手写 token/key。
for _tool in TOOLS:
    _schema = _tool["inputSchema"]
    _props = _schema.setdefault("properties", {})
    if _tool["name"] not in ("opc_init", "opc_login", "opc_whoami", "opc_route"):
        _props.setdefault("client_key", {
            "type": "string",
            "description": "个人 API key；外网客户端由 MCP 网关按请求头自动注入，无需手写"})
        _schema["required"] = [r for r in _schema.get("required", []) if r != "token"]


class OpcServer:
    def __init__(self):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = Store(config.DB_PATH)
        self.runner = JobRunner(self.store)
        self.runner.register("analyze", self._job_analyze)
        self.runner.register("ask", self._job_ask)

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
        context = resources.context(store, phone, question)
        hits = resources.search(store, phone, question, k=5)
        citations = [{"source": h["source"], "id": h["id"], "title": h["title"],
                      "score": h["score"], "path": h["path"]} for h in hits]
        history = store.messages(phone, limit=8)
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
                answer = "（LLM 通道不可用，先给知识库命中）\n" + "\n".join(
                    "- 【%s】%s" % (h["source"], h["snippet"]) for h in hits)
            else:
                answer = "LLM 通道不可用，且知识库没有命中「%s」的内容。" % question

        store.add_message(phone, "user", question)
        store.add_message(phone, "assistant", answer)
        return {"answer": answer, "degraded": degraded, "llm_error": error,
                "citations": citations, "context_chars": len(context)}

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
        return _json({"phone": phone, "name": user["name"], "role": user["role"],
                      "tenant": config.phone_root(phone).name,
                      "dataDir": str(config.phone_root(phone)),
                      "vault": str(config.user_vault(phone)),
                      "workspace": str(config.user_dir(phone))})

    def tool_material_upload(self, args):
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        if args.get("content_base64"):
            try:
                raw = base64.b64decode(str(args["content_base64"]).split(",")[-1])
            except (binascii.Error, ValueError) as e:
                return _err("content_base64 解码失败：%s" % e)
            filename = _safe_name(args.get("filename") or "upload.bin")
            if len(raw) > config.MAX_UPLOAD_MB * 1024 * 1024:
                return _err("base64 上传超过上限 %sMB，请改用 upload_begin 分片续传"
                            % config.MAX_UPLOAD_MB)
            return self._ingest_bytes(phone, filename, raw)
        if args.get("path"):
            src = Path(str(args["path"])).expanduser()
            if not src.is_file():
                return _err("文件不存在或不是普通文件：%s" % src)
            filename = _safe_name(args.get("filename") or src.name)
            size = src.stat().st_size
            if size > config.UPLOAD_MAX_MB * 1024 * 1024:
                return _err("文件超过上限 %sMB" % config.UPLOAD_MAX_MB)
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
            return self._queue(phone, filename, dst, size, digest)
        return _err("需要 path 或 content_base64")

    def _ingest_bytes(self, phone, filename, raw):
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
        return self._queue(phone, filename, dst, len(raw), sha)

    def _queue(self, phone, filename, dst, size, digest):
        mid = self.store.add_material(phone, filename, dst, size, digest, _guess_mime(filename))
        job = self.runner.submit(phone, "analyze", {"material_id": mid})
        return _json({"material_id": mid, "filename": filename, "size": size,
                      "path": str(dst), "job_id": job["job_id"], "status": "queued",
                      "note": "已在后台分析并内化，可轮询 job_get"})

    def tool_upload_begin(self, args):
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        filename = _safe_name(args.get("filename") or "upload.bin")
        try:
            size = int(args.get("size") or 0)
        except (TypeError, ValueError):
            return _err("size 必须是整数")
        if size <= 0:
            return _err("size 必须为正整数")
        if size > config.UPLOAD_MAX_MB * 1024 * 1024:
            return _err("文件超过上限 %sMB" % config.UPLOAD_MAX_MB)
        try:
            chunk_size = int(args.get("chunk_size") or 0)
        except (TypeError, ValueError):
            return _err("chunk_size 必须是整数")
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        upload_id = "up_" + secrets.token_hex(8)
        path = config.UPLOAD_DIR / (upload_id + ".part")
        path.write_bytes(b"")
        self.store.add_upload(upload_id, phone, filename, size, path, chunk_size)
        base = config.PUBLIC_UPLOAD_BASE
        return _json({
            "upload_id": upload_id, "filename": filename, "size": size, "offset": 0,
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
        rows = self.store.materials(user["phone"], int(args.get("limit") or 50))
        return _json({"count": len(rows), "items": [
            {"material_id": r["id"], "filename": r["filename"], "size": r["size"],
             "status": r["status"], "reason": r["reason"],
             "created_at": _ts(r["created_at"])} for r in rows]})

    def tool_material_download(self, args):
        user = auth.resolve(self.store, args)
        mat = self.store.material(str(args.get("material_id") or ""))
        if not mat or mat["phone"] != user["phone"]:
            return _err("素材不存在或不属于当前账号")
        src = Path(mat["path"])
        if not src.is_file():
            return _err("素材文件已丢失：%s" % src)
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
                return _err("素材不存在或不属于当前账号")
            ids = [target]
        job = self.runner.submit(user["phone"], "analyze", {"material_ids": ids})
        return _json({"job_id": job["job_id"], "status": "queued", "count": len(ids)})

    def tool_job_get(self, args):
        user = auth.resolve(self.store, args)
        job = self.runner.wait(str(args.get("job_id") or ""), user["phone"],
                               float(args.get("wait_sec") or 0))
        return _json(job) if job else _err("作业不存在")

    def tool_kb_status(self, args):
        user = auth.resolve(self.store, args)
        phone = user["phone"]
        by_status = {r["status"]: r["n"] for r in self.store.all(
            "SELECT status, COUNT(*) n FROM materials WHERE phone=? GROUP BY status", (phone,))}
        total = sum(by_status.values())
        content_ids = {r["material_id"] for r in self.store.all(
            "SELECT DISTINCT material_id FROM chunks WHERE phone=? AND material_id IS NOT NULL", (phone,))}
        chunks = self.store.one("SELECT COUNT(*) n FROM chunks WHERE phone=?", (phone,))
        cards = self.store.one("SELECT COUNT(*) n FROM cards WHERE phone=?", (phone,))
        ok, detail, probe_info = llm.probe()
        rc = resources.counts(phone, self.store)
        return _json({
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
                    "detail": None if ok else detail, "probe": probe_info},
            "media": media.health(),
        })

    def tool_kb_search(self, args):
        user = auth.resolve(self.store, args)
        query = str(args.get("query") or "")
        hits = resources.search(self.store, user["phone"], query, int(args.get("k") or 6))
        return _json({"query": query, "count": len(hits), "hits": hits,
                      "sources": ["opc.knowledge", "opc.report", "opc.content",
                                  "opc.message", "mcp.material",
                                  "shared.knowledge", "shared.material", "shared.hotspot"]})

    def tool_opc_resources(self, args):
        user = auth.resolve(self.store, args)
        return _json(resources.counts(user["phone"], self.store))

    def tool_opc_resource_list(self, args):
        user = auth.resolve(self.store, args)
        return _json(resources.list_kind(user["phone"], str(args.get("kind") or "knowledge"),
                                         int(args.get("limit") or 20)))

    def tool_opc_resource_read(self, args):
        user = auth.resolve(self.store, args)
        out, error = resources.read(user["phone"], args.get("ref"), self.store)
        return _err(error, status="blocked") if error else _json(out)

    def tool_ask(self, args):
        user = auth.resolve(self.store, args)
        text = str(args.get("text") or args.get("question") or "").strip()
        if not text:
            return _err("请提供问题内容：text 或 question 任填一项")
        job = self.runner.submit(user["phone"], "ask", {"text": text})
        wait = float(args.get("wait_sec") or 0)
        if wait > 0:
            return _json(self.runner.wait(job["job_id"], user["phone"], wait))
        return _json(job)

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
            (("上传", "导入", "传文件", "upload"), "upload_begin",
             "大文件走 upload_begin 分片；<100MB 可用 material_upload"),
            (("共享", "公开", "公共", "下载区", "全局", "shared", "downloads"),
             "opc_resource_list",
             "全局共享走 opc_resource_list(kind=shared_materials 或 downloads)"),
            (("列出", "我的素材", "我的下载", "导出", "list", "download"), "material_list",
             "本人素材用 material_list：先 list 拿 material_id 再 download"),
            (("重新分析", "再分析", "analyze"), "material_analyze",
             "提交分析后用 job_get 等结果"),
            (("语音", "朗读", "配音", "tts"), "tts", "文本转语音"),
            (("图片", "画图", "生图", "图像", "image"), "image", "文生图"),
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
        if not matched:
            matched = [("kb_search", "关键词检索拿片段与出处"),
                       ("ask", "带引用的综合问答（参数名 text）")]

        ordered = list(dict.fromkeys(t for t, _ in matched))
        non_kb = ("material_upload", "upload_begin", "material_list", "material_download",
                  "tts", "image")
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
            "kb_search": 'kb_search(query="<关键词>", k=6)',
            "ask": 'ask(text="<你的问题>", wait_sec=60)',
            "material_list": "material_list(limit=50)",
            "opc_resource_list": 'opc_resource_list(kind="shared_materials", limit=20)｜'
                                 '公开下载区用 opc_resource_list(kind="downloads", limit=20)',
            "material_download": 'material_download(material_id="<mat_id>")',
            "material_upload": 'material_upload(path="<本地文件路径>")',
            "upload_begin": 'upload_begin(filename="<名称>", size=<字节数>, chunk_size=8388608)',
            "material_analyze": 'material_analyze(material_id="<mat_id> 或 all>")',
            "job_get": 'job_get(job_id="<job_id>", wait_sec=60)',
            "tts": 'tts(text="<要朗读的文字>")',
            "image": 'image(prompt="<画面描述>")',
        }
        return _json({
            "query": query,
            "knowledge_related": knowledge_related,
            "recommended": [t for t, _ in matched],
            "steps": steps,
            "templates": {t: templates[t] for t in steps if t in templates},
            "notes": "身份由 key 自带，不要传 phone；ask 的问题内容用 text 或 question 均可；"
                     "检索不到要明确说明，不要用通用知识冒充公司内部资料。",
        })

    def tool_tts(self, args):
        user = auth.resolve(self.store, args)
        out_dir = self._dir(user["phone"], "audio")
        try:
            res = media.tts(str(args.get("text") or ""), out_dir,
                            voice=args.get("voice"), engine=args.get("engine") or "cosyvoice",
                            speed=float(args.get("speed") or 1.0),
                            wait_sec=float(args.get("wait_sec") or 180))
        except media.MediaError as e:
            return _err(str(e), status="media_error")
        return _json(res)

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
                              wait_sec=float(args.get("wait_sec") or 300))
        except media.MediaError as e:
            return _err(str(e), status="media_error")
        return _json(res)

    # ---------------------------------------------------------------- 协议
    def dispatch(self, name, args):
        fn = getattr(self, "tool_" + name, None)
        if not fn:
            return _err("未知工具：%s" % name)
        try:
            return fn(args or {})
        except auth.AuthError as e:
            return _err(str(e), status="need_auth",
                        next="未领取 key：打开 %s 登录后自助生成；" % config.KEY_APPLY_URL
                             + "已领取：配置请求头 X-Mail-Agent=<个人 API key>，"
                               "或只调用 opc_init(key=...) 获取 session_token（不需要手机号）")
        except Exception as e:
            log("工具 %s 异常：%r" % (name, e))
            return _err("%s: %s" % (type(e).__name__, e))

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

    def serve(self):
        log("ready db=%s vault=%s" % (config.DB_PATH, config.VAULT))
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                _write({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error: %s" % e}})
                continue
            resp = self.handle(msg)
            if resp is not None:
                _write(resp)


def _write(obj):
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
