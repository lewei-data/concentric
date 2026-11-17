"""MCP stdio 服务：JSON-RPC 2.0，暴露初始化/素材/分片续传/知识库/对话/TTS/图像共 13 个工具。"""

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

from . import __version__, auth, config, knowledge, llm, media
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
        "description": "用手机号初始化 OPC 会话。已登记手机号直接返回 session_token；"
                       "未登记或未传手机号时返回 need_input/need_registration，告知外部 agent 还需用户提供什么。",
        "inputSchema": {"type": "object", "properties": {
            "phone": {"type": "string", "description": "11 位手机号；不传则返回需要的输入项"}},
            "required": []},
    },
    {
        "name": "opc_login",
        "description": "手机号 + 密码（或 API key）完成初始化，返回 session_token。",
        "inputSchema": {"type": "object", "properties": {
            "phone": {"type": "string"},
            "password": {"type": "string", "description": "登录密码"},
            "key": {"type": "string", "description": "API key（password 的替代）"}},
            "required": ["phone"]},
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
        "description": "大文件分片续传建单（支持 zip/pdf/doc 等，单文件最大 2GB）。返回 upload_id 与 HTTP 端点："
                       "PUT {put_url}?offset=<已传字节> 追加分片（Content-Length 必填，可带 Content-Range: bytes s-e/total），"
                       "GET {progress_url} 查进度（percent/已传/剩余/速率/ETA），"
                       "POST .../complete 完成入库并自动分析。断电/断网后重新 GET progress_url 拿 offset 续传即可。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"},
            "filename": {"type": "string", "description": "含扩展名的文件名，如 资料.zip、合同.pdf"},
            "size": {"type": "integer", "description": "文件总字节数（必须精确）"}},
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
        "description": "知识库与依赖服务状态：素材数、片段数、卡片数、近期作业、LLM/TTS/图像探活。",
        "inputSchema": {"type": "object", "properties": {"token": {"type": "string"}},
                        "required": ["token"]},
    },
    {
        "name": "kb_search",
        "description": "在本人知识库做关键词检索，返回最相关片段与出处。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"}, "query": {"type": "string"},
            "k": {"type": "integer", "default": 6}},
            "required": ["token", "query"]},
    },
    {
        "name": "ask",
        "description": "文本问答：后台结合知识库内化内容调用 LLM 生成结果；wait_sec>0 可直接拿答案。",
        "inputSchema": {"type": "object", "properties": {
            "token": {"type": "string"}, "text": {"type": "string"},
            "wait_sec": {"type": "number", "default": 0, "description": "0=只返回作业号"}},
            "required": ["token", "text"]},
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
        context = knowledge.build_context(store, phone, question)
        citations = [{"material_id": h["material_id"], "seq": h["seq"], "score": h["score"],
                      "filename": (store.material(h["material_id"]) or {}).get("filename")}
                     for h in knowledge.search(store, phone, question, k=5)]
        history = store.messages(phone, limit=8)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if context:
            messages.append({"role": "system", "content": "知识库检索结果：\n" + context})
        messages += history
        messages.append({"role": "user", "content": question})

        degraded, error = False, None
        try:
            answer = llm.chat(messages, max_tokens=1400)
        except llm.LLMError as e:
            degraded, error = True, str(e)[:300]
            if citations:
                answer = "（LLM 通道不可用，先给知识库命中）\n" + "\n".join(
                    "- %s" % h["snippet"] for h in knowledge.search(store, phone, question, k=5))
            else:
                answer = "LLM 通道不可用，且知识库没有命中「%s」的内容。" % question

        store.add_message(phone, "user", question)
        store.add_message(phone, "assistant", answer)
        return {"answer": answer, "degraded": degraded, "llm_error": error,
                "citations": citations, "context_chars": len(context)}

    # ---------------------------------------------------------------- 工具实现
    def _dir(self, phone, sub):
        d = config.DATA_DIR / "users" / phone / sub / time.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def tool_opc_init(self, args):
        return _json(auth.init(self.store, args.get("phone")))

    def tool_opc_login(self, args):
        return _json(auth.login(self.store, args.get("phone"), args.get("password"), args.get("key")))

    def tool_material_upload(self, args):
        user = auth.authenticate(self.store, args.get("token"))
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
        user = auth.authenticate(self.store, args.get("token"))
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
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        upload_id = "up_" + secrets.token_hex(8)
        path = config.UPLOAD_DIR / (upload_id + ".part")
        path.write_bytes(b"")
        self.store.add_upload(upload_id, phone, filename, size, path)
        base = config.PUBLIC_UPLOAD_BASE
        return _json({
            "upload_id": upload_id, "filename": filename, "size": size, "offset": 0,
            "chunk_size_hint": config.UPLOAD_CHUNK_HINT,
            "auth": "Authorization: Bearer <token>（复用本工具传入的 session_token）",
            "put_url": "%s/v1/uploads/%s" % (base, upload_id),
            "head_url": "%s/v1/uploads/%s" % (base, upload_id),
            "progress_url": "%s/v1/uploads/%s/progress" % (base, upload_id),
            "complete_url": "%s/v1/uploads/%s/complete" % (base, upload_id),
            "delete_url": "%s/v1/uploads/%s" % (base, upload_id),
            "local_base": config.LOCAL_UPLOAD_BASE,
            "next": "PUT 每次带 ?offset=<已传字节>，服务端返回新 offset；中断后 GET 该 URL 取 offset 续传；"
                    "传满后 POST complete_url（可带 X-Content-SHA256），返回 material_id 与 job_id。"})

    def tool_material_list(self, args):
        user = auth.authenticate(self.store, args.get("token"))
        rows = self.store.materials(user["phone"], int(args.get("limit") or 50))
        return _json({"count": len(rows), "items": [
            {"material_id": r["id"], "filename": r["filename"], "size": r["size"],
             "status": r["status"], "reason": r["reason"],
             "created_at": _ts(r["created_at"])} for r in rows]})

    def tool_material_download(self, args):
        user = auth.authenticate(self.store, args.get("token"))
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
        user = auth.authenticate(self.store, args.get("token"))
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
        user = auth.authenticate(self.store, args.get("token"))
        job = self.runner.wait(str(args.get("job_id") or ""), user["phone"],
                               float(args.get("wait_sec") or 0))
        return _json(job) if job else _err("作业不存在")

    def tool_kb_status(self, args):
        user = auth.authenticate(self.store, args.get("token"))
        phone = user["phone"]
        by_status = {r["status"]: r["n"] for r in self.store.all(
            "SELECT status, COUNT(*) n FROM materials WHERE phone=? GROUP BY status", (phone,))}
        chunks = self.store.one("SELECT COUNT(*) n FROM chunks WHERE phone=?", (phone,))
        cards = self.store.one("SELECT COUNT(*) n FROM cards WHERE phone=?", (phone,))
        ok, detail = llm.probe()
        return _json({
            "user": {"phone": phone, "name": user["name"], "role": user["role"]},
            "workspace": str(config.DATA_DIR / "users" / phone),
            "materials": {"total": sum(by_status.values()), "by_status": by_status},
            "chunks": chunks["n"], "cards": cards["n"],
            "recent_jobs": [{"job_id": j["id"], "kind": j["kind"], "status": j["status"],
                             "error": j["error"]} for j in self.store.jobs(phone, limit=5)],
            "llm": {"ok": ok, "base_url": config.LLM_BASE_URL, "model": config.LLM_MODEL,
                    "detail": None if ok else detail},
            "media": media.health(),
        })

    def tool_kb_search(self, args):
        user = auth.authenticate(self.store, args.get("token"))
        query = str(args.get("query") or "")
        hits = knowledge.search(self.store, user["phone"], query, int(args.get("k") or 6))
        for h in hits:
            h["filename"] = (self.store.material(h["material_id"]) or {}).get("filename")
        return _json({"query": query, "count": len(hits), "hits": hits})

    def tool_ask(self, args):
        user = auth.authenticate(self.store, args.get("token"))
        text = str(args.get("text") or "").strip()
        if not text:
            return _err("text 不能为空")
        job = self.runner.submit(user["phone"], "ask", {"text": text})
        wait = float(args.get("wait_sec") or 0)
        if wait > 0:
            return _json(self.runner.wait(job["job_id"], user["phone"], wait))
        return _json(job)

    def tool_tts(self, args):
        user = auth.authenticate(self.store, args.get("token"))
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
        user = auth.authenticate(self.store, args.get("token"))
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
            return _err(str(e), status="need_init",
                        next="先调用 opc_init(phone)；未登记则 opc_login(phone, password|key)")
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
