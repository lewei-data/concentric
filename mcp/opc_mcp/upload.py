"""大文件分片续传服务（stdlib HTTP，默认 9105）。

协议（纯 HTTP，可断点续传，客户端只需记住 upload_id 与本地已发字节数）：

  POST   /v1/uploads            建单  {filename,size}        -> 201 {upload_id,offset,size,...}
  HEAD   /v1/uploads/{id}       查进度（响应头 X-Upload-Offset / X-Upload-Size / X-Upload-Status）
  GET    /v1/uploads/{id}       查进度（JSON）
  PUT    /v1/uploads/{id}?offset=N   追加分片（body 为原始字节，支持 Content-Range）
  POST   /v1/uploads/{id}/complete   完成：校验大小 → sha256 → 去重 → 入库 → 排队分析
  DELETE /v1/uploads/{id}       取消并删除临时文件
  GET    /v1/uploads?token=...  列出本人上传单
  GET    /healthz               健康检查

鉴权：Authorization: Bearer <session_token>（也可用 ?token=）。

中断后客户端重新 HEAD/GET 拿 offset，从该偏移继续 PUT 即可，不需要重传已完成部分。
"""

import hashlib
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import auth, config, knowledge
from .jobs import JobRunner
from .store import Store

CHUNK = 1024 * 1024


def log(msg):
    sys.stderr.write("[opc-upload] %s\n" % msg)
    sys.stderr.flush()


def safe_name(name):
    name = Path(str(name or "upload.bin")).name
    return re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name)[:120] or "upload.bin"


def guess_mime(filename):
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def user_materials_dir(phone):
    d = config.DATA_DIR / "users" / phone / "materials" / time.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


class App:
    def __init__(self):
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = Store(config.DB_PATH)
        self.runner = JobRunner(self.store, owner="upload")
        self.runner.register("analyze", self._job_analyze)
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._cleanup_stale()

    # ------------------------------------------------------------ 作业
    @staticmethod
    def _job_analyze(store, phone, payload):
        mid = payload.get("material_id")
        return knowledge.analyze_material(store, phone, mid)

    # ------------------------------------------------------------ 上传单
    def lock_for(self, upload_id):
        with self._locks_guard:
            lk = self._locks.get(upload_id)
            if lk is None:
                lk = self._locks[upload_id] = threading.Lock()
            return lk

    def part_path(self, upload_id):
        return config.UPLOAD_DIR / (upload_id + ".part")

    def create(self, user, filename, size):
        if size <= 0:
            raise ApiError(400, "size 必须为正整数")
        if size > config.UPLOAD_MAX_MB * 1024 * 1024:
            raise ApiError(413, "文件超过上限 %sMB" % config.UPLOAD_MAX_MB)
        upload_id = "up_" + secrets.token_hex(8)
        path = self.part_path(upload_id)
        path.write_bytes(b"")
        self.store.add_upload(upload_id, user["phone"], safe_name(filename), int(size), path)
        row = self.store.upload(upload_id)
        return self.view(row)

    def view(self, row, extra=None):
        base = config.PUBLIC_UPLOAD_BASE
        out = {
            "upload_id": row["id"],
            "filename": row["filename"],
            "size": row["size"],
            "offset": row["offset"],
            "remaining": max(0, (row["size"] or 0) - (row["offset"] or 0)),
            "status": row["status"],
            "sha256": row["sha256"],
            "material_id": row["material_id"],
            "chunk_size_hint": config.UPLOAD_CHUNK_HINT,
            "public_base": config.PUBLIC_UPLOAD_BASE,
            "local_base": config.LOCAL_UPLOAD_BASE,
            "head_url": "%s/v1/uploads/%s" % (base, row["id"]),
            "put_url": "%s/v1/uploads/%s?offset=%d" % (base, row["id"], row["offset"] or 0),
            "complete_url": "%s/v1/uploads/%s/complete" % (base, row["id"]),
        }
        if extra:
            out.update(extra)
        return out

    def append(self, row, offset, rfile, length):
        """把 length 字节追加到 .part；offset 必须等于当前偏移。返回新偏移。"""
        with self.lock_for(row["id"]):
            row = self.store.upload(row["id"])
            if not row:
                raise ApiError(404, "上传单不存在")
            if row["status"] != "uploading":
                raise ApiError(409, "上传单状态为 %s，不能再写" % row["status"])
            cur = int(row["offset"] or 0)
            if offset != cur:
                raise ApiError(409, "偏移不匹配，请从 %d 继续" % cur, offset=cur)
            if cur + length > row["size"]:
                raise ApiError(413, "写入超出声明大小 %d" % row["size"], offset=cur)
            path = Path(row["path"])
            with open(path, "ab") as f:
                left = length
                while left > 0:
                    buf = rfile.read(min(CHUNK, left))
                    if not buf:
                        break
                    f.write(buf)
                    left -= len(buf)
                f.flush()
                os.fsync(f.fileno())
                written = length - left
            new_offset = cur + written
            self.store.update_upload(row["id"], offset=new_offset)
            return new_offset, int(row["size"]), written

    def complete(self, user, row_id, expected_sha=None):
        row = self.store.upload(row_id)
        if not row:
            raise ApiError(404, "上传单不存在")
        if row["phone"] != user["phone"]:
            raise ApiError(403, "无权操作该上传单")
        if row["status"] == "done":
            return self.view(row, {"duplicate": True})
        if row["status"] != "uploading":
            raise ApiError(409, "上传单状态为 %s" % row["status"])
        if int(row["offset"] or 0) != int(row["size"] or 0):
            raise ApiError(409, "尚未传完：%d/%d" % (row["offset"], row["size"]),
                           offset=int(row["offset"] or 0))
        path = Path(row["path"])
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                buf = f.read(CHUNK * 4)
                if not buf:
                    break
                sha.update(buf)
        digest = sha.hexdigest()
        if expected_sha and expected_sha.lower() != digest:
            self.store.update_upload(row_id, status="corrupt", sha256=digest)
            raise ApiError(400, "sha256 校验失败", expected=expected_sha, actual=digest)

        dup = self.store.one("SELECT * FROM materials WHERE phone=? AND sha256=?",
                             (user["phone"], digest))
        if dup:
            path.unlink(missing_ok=True)
            self.store.update_upload(row_id, status="done", sha256=digest, material_id=dup["id"])
            return self.view(self.store.upload(row_id), {
                "duplicate": True, "material_id": dup["id"],
                "note": "相同内容已入库，未重复分析"})

        dst = user_materials_dir(user["phone"]) / row["filename"]
        if dst.exists():
            dst = dst.with_name("%s_%s%s" % (dst.stem, digest[:6], dst.suffix))
        os.replace(path, dst)
        mid = self.store.add_material(user["phone"], row["filename"], dst, int(row["size"]),
                                      digest, guess_mime(row["filename"]))
        self.store.update_upload(row_id, status="done", sha256=digest, material_id=mid)
        job = self.runner.submit(user["phone"], "analyze", {"material_id": mid})
        return self.view(self.store.upload(row_id), {
            "duplicate": False, "material_id": mid, "job_id": job["job_id"],
            "job_status": job["status"]})

    def cancel(self, user, row_id):
        row = self.store.upload(row_id)
        if not row:
            raise ApiError(404, "上传单不存在")
        if row["phone"] != user["phone"]:
            raise ApiError(403, "无权操作该上传单")
        if row["status"] == "done":
            raise ApiError(409, "已完成的素材不能取消")
        Path(row["path"]).unlink(missing_ok=True)
        self.store.update_upload(row_id, status="canceled")
        return self.view(self.store.upload(row_id))

    def cleanup(self):
        """清理过期的未完成上传单（默认 7 天）。"""
        deadline = time.time() - config.UPLOAD_TTL_SEC
        n = 0
        for row in self.store.stale_uploads(deadline):
            Path(row["path"]).unlink(missing_ok=True)
            self.store.update_upload(row["id"], status="expired")
            n += 1
        return n

    def _cleanup_stale(self):
        try:
            n = self.cleanup()
            if n:
                log("清理过期上传单 %d 个" % n)
        except Exception as e:                                # 清理失败不影响服务
            log("清理过期上传单失败：%s" % e)


class ApiError(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "opc-upload/1.0"
    app = None                                            # main() 注入

    # ------------------------------------------------------------ 基础
    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    def _send(self, code, obj, headers=None):
        body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "请求体不是合法 JSON")

    def _query(self):
        return parse_qs(urlsplit(self.path).query)

    def _token(self, q=None):
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        q = q if q is not None else self._query()
        return (q.get("token") or [""])[0]

    def _user(self, q=None):
        token = self._token(q)
        if not token:
            raise ApiError(401, "缺少 Authorization: Bearer <session_token>")
        try:
            return auth.authenticate(self.app.store, token)
        except auth.AuthError as e:
            raise ApiError(401, str(e))

    def _path(self):
        return urlsplit(self.path).path.rstrip("/") or "/"

    def _wrap(self, fn):
        try:
            fn()
        except ApiError as e:
            self._send(e.code, dict({"ok": False, "error": e.message}, **e.extra))
        except BrokenPipeError:
            pass
        except Exception as e:
            log("未处理异常：%s: %s" % (type(e).__name__, e))
            self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        self._wrap(self._get)

    def do_HEAD(self):
        self._wrap(self._head)

    def do_POST(self):
        self._wrap(self._post)

    def do_PUT(self):
        self._wrap(self._put)

    def do_DELETE(self):
        self._wrap(self._delete)

    def _get(self):
        path = self._path()
        if path in ("/healthz", "/health"):
            return self._send(200, {"ok": True, "service": "opc-upload",
                                    "max_mb": config.UPLOAD_MAX_MB,
                                    "chunk_hint": config.UPLOAD_CHUNK_HINT,
                                    "uploads": len(self.app.store.all(
                                        "SELECT id FROM uploads WHERE status='uploading'"))})
        if path == "/v1/uploads":
            user = self._user()
            return self._send(200, {"ok": True, "items": [
                self.app.view(r) for r in self.app.store.uploads(user["phone"])]})
        m = re.match(r"^/v1/uploads/(up_[0-9a-f]{16})$", path)
        if m:
            user = self._user()
            row = self.app.store.upload(m.group(1))
            if not row:
                raise ApiError(404, "上传单不存在")
            if row["phone"] != user["phone"]:
                raise ApiError(403, "无权查看该上传单")
            return self._send(200, self.app.view(row))
        raise ApiError(404, "未知路径 %s" % path)

    def _head(self):
        path = self._path()
        m = re.match(r"^/v1/uploads/(up_[0-9a-f]{16})$", path)
        if not m:
            raise ApiError(404, "未知路径 %s" % path)
        user = self._user()
        row = self.app.store.upload(m.group(1))
        if not row or row["phone"] != user["phone"]:
            raise ApiError(404, "上传单不存在")
        self.send_response(204)
        self.send_header("X-Upload-Offset", str(row["offset"] or 0))
        self.send_header("X-Upload-Size", str(row["size"] or 0))
        self.send_header("X-Upload-Status", str(row["status"]))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _post(self):
        path = self._path()
        if path == "/v1/uploads":
            body = self._json_body()
            user = self._user()
            try:
                size = int(body.get("size") or 0)
            except (TypeError, ValueError):
                raise ApiError(400, "size 必须是整数")
            view = self.app.create(user, body.get("filename"), size)
            return self._send(201, view)
        m = re.match(r"^/v1/uploads/(up_[0-9a-f]{16})/complete$", path)
        if m:
            self._json_body()                              # 允许空体，但吃掉 body
            user = self._user()
            expected = self.headers.get("X-Content-SHA256")
            return self._send(200, self.app.complete(user, m.group(1), expected))
        raise ApiError(404, "未知路径 %s" % path)

    def _put(self):
        path = self._path()
        m = re.match(r"^/v1/uploads/(up_[0-9a-f]{16})$", path)
        if not m:
            raise ApiError(404, "未知路径 %s" % path)
        user = self._user()
        row = self.app.store.upload(m.group(1))
        if not row:
            raise ApiError(404, "上传单不存在")
        if row["phone"] != user["phone"]:
            raise ApiError(403, "无权写入该上传单")

        length = int(self.headers.get("Content-Length") or -1)
        if length < 0:
            raise ApiError(411, "PUT 需要 Content-Length")
        offset = None
        q = self._query()
        if q.get("offset"):
            try:
                offset = int(q["offset"][0])
            except ValueError:
                raise ApiError(400, "offset 必须是整数")
        cr = self.headers.get("Content-Range")
        if cr:
            cm = re.match(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", cr.strip())
            if not cm:
                raise ApiError(400, "Content-Range 格式应为 bytes start-end/total")
            start, end = int(cm.group(1)), int(cm.group(2))
            if end - start + 1 != length:
                raise ApiError(400, "Content-Range 与 Content-Length 不一致")
            if offset is not None and offset != start:
                raise ApiError(400, "offset 与 Content-Range 起点不一致")
            offset = start
        if offset is None:
            offset = int(row["offset"] or 0)

        new_offset, size, written = self.app.append(row, offset, self.rfile, length)
        self._send(200, {"ok": True, "upload_id": row["id"], "offset": new_offset,
                         "size": size, "written": written,
                         "remaining": max(0, size - new_offset),
                         "done": new_offset >= size},
                   headers={"X-Upload-Offset": new_offset,
                            "X-Upload-Size": size})

    def _delete(self):
        path = self._path()
        m = re.match(r"^/v1/uploads/(up_[0-9a-f]{16})$", path)
        if not m:
            raise ApiError(404, "未知路径 %s" % path)
        user = self._user()
        return self._send(200, {"ok": True, "upload": self.app.cancel(user, m.group(1))})


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="OPC 大文件分片续传服务")
    ap.add_argument("--host", default=config.UPLOAD_HOST)
    ap.add_argument("--port", type=int, default=config.UPLOAD_PORT)
    args = ap.parse_args(argv)

    Handler.app = App()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    log("listening on %s:%d, upload_dir=%s, max=%sMB, chunk_hint=%d"
        % (args.host, args.port, config.UPLOAD_DIR, config.UPLOAD_MAX_MB, config.UPLOAD_CHUNK_HINT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
