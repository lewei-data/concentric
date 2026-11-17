"""大文件分片续传服务（stdlib HTTP，默认 9105），兼容多种客户端协议。

同一套上传单/落盘/内化逻辑，对外提供四种入口：

1) 原生 offset（推荐给 MCP agent）
   POST /v1/uploads {filename,size[,chunk_size]} -> {upload_id, ...urls}
   PUT  /v1/uploads/{id}?offset=N   原始字节 + Content-Length
   HEAD/GET /v1/uploads/{id}[/progress]
   POST /v1/uploads/{id}/complete

2) Content-Range：PUT 支持 `Content-Range: bytes s-e/total`；
   `bytes */*` 查状态，未传完回 308 + `Range: bytes=0-(offset-1)`（Google resumable 习惯）。

3) 片号模式：建单带 chunk_size 后，PUT /v1/uploads/{id}?part=N
   （也支持 X-Chunk-Size / X-Part-Number 头）。

4) Tus 1.0.0：POST /files（Upload-Length / Upload-Name / Upload-Metadata）-> 201 + Location
   HEAD /files/{id} -> Upload-Offset；PATCH /files/{id}（Upload-Offset + application/offset+octet-stream）
   最后一片自动 complete；DELETE /files/{id} -> 204。

偏移也可放 Header：Upload-Offset / X-Upload-Offset。
鉴权：Authorization: Bearer <session_token>、?token=、X-OPC-Token、X-Upload-Token。

服务端语义不变：offset 以服务端为准，重复分片 409 幂等，逐片 fsync，中断后从当前 offset 续传。
"""

import base64
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


def tus_filename(header_value):
    """兼容 Tus 的 Upload-Name 与 Upload-Metadata（filename <base64>）。"""
    raw = str(header_value or "").strip()
    if not raw:
        return "upload.bin"
    for item in raw.split(","):
        bits = item.strip().split(" ", 1)
        key = bits[0].strip().lower()
        if key in ("filename", "name", "upload-name") and len(bits) == 2:
            try:
                return safe_name(base64.b64decode(bits[1]).decode("utf-8"))
            except Exception:
                return safe_name(bits[1])
    return safe_name(raw)


def guess_mime(filename):
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def iso_ts(ts):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return None


def human_speed(bps):
    """字节/秒 → 人类可读，如 2.5 MB/s。"""
    if not bps or bps <= 0:
        return None
    for unit, scale in (("GB/s", 1 << 30), ("MB/s", 1 << 20), ("KB/s", 1 << 10)):
        if bps >= scale:
            return "%.1f %s" % (bps / scale, unit)
    return "%d B/s" % bps


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
        self._samples = {}                                # upload_id -> (ts, offset)，用于瞬时速率
        self._samples_guard = threading.Lock()
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

    def create(self, user, filename, size, chunk_size=0):
        if size <= 0:
            raise ApiError(400, "size 必须为正整数")
        if size > config.UPLOAD_MAX_MB * 1024 * 1024:
            raise ApiError(413, "文件超过上限 %sMB" % config.UPLOAD_MAX_MB)
        upload_id = "up_" + secrets.token_hex(8)
        path = self.part_path(upload_id)
        path.write_bytes(b"")
        self.store.add_upload(upload_id, user["phone"], safe_name(filename), int(size), path,
                              chunk_size=int(chunk_size or 0))
        row = self.store.upload(upload_id)
        return self.view(row)

    def _forget(self, upload_id):
        with self._samples_guard:
            self._samples.pop(upload_id, None)

    def _urls(self, row):
        base = config.PUBLIC_UPLOAD_BASE
        off = int(row["offset"] or 0)
        uid = row["id"]
        return {
            "public_base": config.PUBLIC_UPLOAD_BASE,
            "local_base": config.LOCAL_UPLOAD_BASE,
            "protocols": ["native-offset", "content-range", "chunk-index", "tus-1.0"],
            "progress_url": "%s/v1/uploads/%s/progress" % (base, uid),
            "head_url": "%s/v1/uploads/%s" % (base, uid),
            "put_url": "%s/v1/uploads/%s?offset=%d" % (base, uid, off),
            "part_put_url": "%s/v1/uploads/%s?part={part}" % (base, uid),
            "complete_url": "%s/v1/uploads/%s/complete" % (base, uid),
            "tus_create_url": "%s/files" % base,
            "tus_url": "%s/files/%s" % (base, uid),
            "offset_headers": ["Upload-Offset", "X-Upload-Offset"],
            "chunk_size": int(row["chunk_size"] or 0),
        }

    def _rate(self, row, offset, now):
        """瞬时速率（服务进程内的最近一次写入样本）；无样本返回 None。"""
        with self._samples_guard:
            prev = self._samples.get(row["id"])
        if not prev:
            return None
        ts0, off0 = prev
        dt = now - ts0
        if dt <= 0 or offset <= off0:
            return None
        return (offset - off0) / dt

    def progress(self, row):
        """上传进度：已传/总量/百分比/速率/预计剩余时间。"""
        size = int(row["size"] or 0)
        off = int(row["offset"] or 0)
        now = time.time()
        created = float(row["created_at"] or now)
        elapsed = max(0.0, now - created)
        avg = (off / elapsed) if elapsed > 0.5 and off > 0 else None
        inst = self._rate(row, off, now) if row["status"] == "uploading" else None
        speed = inst or avg
        remaining = max(0, size - off)
        eta = None
        if remaining == 0:
            eta = 0
        elif speed and speed > 0:
            eta = int(round(remaining / speed))
        out = {
            "upload_id": row["id"],
            "filename": row["filename"],
            "status": row["status"],
            "size": size,
            "offset": off,
            "uploaded": off,
            "remaining": remaining,
            "percent": round(off * 100.0 / size, 2) if size else 0.0,
            "done": row["status"] == "done" or (size > 0 and off >= size),
            "elapsed_sec": round(elapsed, 1),
            "speed_bps": int(speed) if speed else None,
            "speed_human": human_speed(speed),
            "speed_source": "instant" if inst else ("average" if avg else None),
            "eta_sec": eta,
            "started_at": iso_ts(created),
            "updated_at": iso_ts(row["updated_at"]),
            "sha256": row["sha256"],
            "material_id": row["material_id"],
            "chunk_size_hint": config.UPLOAD_CHUNK_HINT,
        }
        out.update(self._urls(row))
        return out

    def view(self, row, extra=None):
        out = self.progress(row)
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
            if not path.exists():
                self.store.update_upload(row["id"], status="missing")
                raise ApiError(410, "临时文件已丢失，请重新建单上传")
            with self._samples_guard:                     # 记录写入起点，供进度接口算瞬时速率
                self._samples[row["id"]] = (time.time(), cur)
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
            self._forget(row_id)
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
        self._forget(row_id)
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
        self._forget(row_id)
        return self.view(self.store.upload(row_id))

    def cleanup(self):
        """清理过期的未完成上传单（默认 7 天）。"""
        deadline = time.time() - config.UPLOAD_TTL_SEC
        n = 0
        for row in self.store.stale_uploads(deadline):
            Path(row["path"]).unlink(missing_ok=True)
            self.store.update_upload(row["id"], status="expired")
            self._forget(row["id"])
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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, Content-Range, Tus-Resumable, "
                         "Upload-Offset, Upload-Length, Upload-Name, Upload-Metadata, "
                         "X-Upload-Offset, X-OPC-Token, X-Upload-Token, X-Content-SHA256, "
                         "X-Chunk-Size, X-Part-Number, X-Auto-Complete")
        self.send_header("Access-Control-Expose-Headers",
                         "Location, Upload-Offset, Upload-Length, X-Upload-Offset, "
                         "X-Upload-Size, X-Upload-Percent, Range, Tus-Resumable")
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
        for name in ("X-OPC-Token", "X-Upload-Token", "token"):
            value = (self.headers.get(name) or "").strip()
            if value:
                return value
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

    def _upload_id(self, path):
        m = re.match(r"^(?:/v1/uploads|/files)/(up_[0-9a-f]{16})$", path)
        return m.group(1) if m else None

    def _guard(self, row):
        user = self._user()
        if not row:
            raise ApiError(404, "上传单不存在")
        if row["phone"] != user["phone"]:
            raise ApiError(403, "无权操作该上传单")
        return user

    def _tus_headers(self, row):
        return {"Tus-Resumable": "1.0.0",
                "Upload-Offset": str(row["offset"] or 0),
                "Upload-Length": str(row["size"] or 0),
                "Cache-Control": "no-store"}

    def _int_header(self, name):
        raw = self.headers.get(name)
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return int(raw)
        except ValueError:
            raise ApiError(400, "%s 必须是整数" % name)

    def _status_range(self, row):
        """Google resumable 习惯：bytes */* 未传完回 308 + Range。"""
        off, size = int(row["offset"] or 0), int(row["size"] or 0)
        headers = dict(self._tus_headers(row))
        headers["X-Upload-Offset"] = str(off)
        if off >= size:
            return self._send(200, self.app.view(row), headers=headers)
        headers["Range"] = "bytes=0-%d" % max(0, off - 1)
        return self._send(308, None, headers=headers)

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

    def do_PATCH(self):
        self._wrap(self._patch)

    def do_OPTIONS(self):
        self._send(204, None)

    def _get(self):
        path = self._path()
        if path in ("/healthz", "/health"):
            return self._send(200, {"ok": True, "service": "opc-upload",
                                    "max_mb": config.UPLOAD_MAX_MB,
                                    "chunk_hint": config.UPLOAD_CHUNK_HINT,
                                    "protocols": ["native-offset", "content-range",
                                                  "chunk-index", "tus-1.0"],
                                    "uploads": len(self.app.store.all(
                                        "SELECT id FROM uploads WHERE status='uploading'"))})
        if path == "/v1/uploads":
            user = self._user()
            rows = self.app.store.uploads(user["phone"])
            return self._send(200, {"ok": True, "count": len(rows),
                                    "active": sum(1 for r in rows if r["status"] == "uploading"),
                                    "items": [self.app.view(r) for r in rows]})
        m = re.match(r"^(?:/v1/uploads|/files)/(up_[0-9a-f]{16})(/progress)?$", path)
        if m:
            row = self.app.store.upload(m.group(1))
            self._guard(row)
            cr = (self.headers.get("Content-Range") or "").strip()
            if cr == "bytes */*" or self.headers.get("X-Status-Only") == "1":
                return self._status_range(row)
            if m.group(2):
                return self._send(200, dict({"ok": True}, **self.app.progress(row)),
                                  headers=self._tus_headers(row))
            return self._send(200, self.app.view(row), headers=self._tus_headers(row))
        raise ApiError(404, "未知路径 %s" % path)

    def _head(self):
        path = self._path()
        uid = self._upload_id(path)
        if not uid:
            raise ApiError(404, "未知路径 %s" % path)
        row = self.app.store.upload(uid)
        self._guard(row)
        self.send_response(204)
        for k, v in self._tus_headers(row).items():
            self.send_header(k, str(v))
        self.send_header("X-Upload-Status", str(row["status"]))
        self.send_header("X-Upload-Size", str(row["size"] or 0))
        self.send_header("X-Upload-Percent", "%.2f" % self.app.progress(row)["percent"])
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers",
                         "Upload-Offset, Upload-Length, X-Upload-Offset, X-Upload-Size, "
                         "X-Upload-Percent, Tus-Resumable")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _post(self):
        path = self._path()
        if path in ("/v1/uploads", "/files"):
            tus = path == "/files"
            body = {} if tus else self._json_body()
            user = self._user()
            if tus:
                size = self._int_header("Upload-Length")
                if size is None:
                    raise ApiError(400, "Tus 建单需要 Upload-Length 请求头")
                filename = tus_filename(self.headers.get("Upload-Name")
                                        or self.headers.get("Upload-Metadata"))
                chunk_size = self._int_header("X-Chunk-Size") or 0
            else:
                try:
                    size = int(body.get("size") or 0)
                    chunk_size = int(body.get("chunk_size") or 0)
                except (TypeError, ValueError):
                    raise ApiError(400, "size/chunk_size 必须是整数")
                filename = body.get("filename")
            view = self.app.create(user, filename, size, chunk_size)
            if tus:
                headers = self._tus_headers(self.app.store.upload(view["upload_id"]))
                headers["Location"] = "%s/files/%s" % (config.PUBLIC_UPLOAD_BASE, view["upload_id"])
                return self._send(201, None, headers=headers)
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
        row = self.app.store.upload(m.group(1))
        user = self._guard(row)

        q = self._query()
        cr = (self.headers.get("Content-Range") or "").strip()
        if cr == "bytes */*":
            return self._status_range(row)

        length = int(self.headers.get("Content-Length") or -1)
        if length < 0:
            raise ApiError(411, "PUT 需要 Content-Length")

        offset = None
        if q.get("offset"):
            try:
                offset = int(q["offset"][0])
            except ValueError:
                raise ApiError(400, "offset 必须是整数")
        for name in ("Upload-Offset", "X-Upload-Offset"):
            value = self._int_header(name)
            if value is None:
                continue
            if offset is not None and offset != value:
                raise ApiError(400, "%s 与 offset 不一致（%s != %s）" % (name, value, offset))
            offset = value

        part = q.get("part") or ([self.headers.get("X-Part-Number")] if self.headers.get("X-Part-Number") else None)
        if part:
            try:
                part_no = int(part[0])
            except ValueError:
                raise ApiError(400, "part 必须是整数")
            if part_no < 0:
                raise ApiError(400, "part 不能为负")
            raw_chunk = (q.get("chunk_size") or [None])[0] or self._int_header("X-Chunk-Size") \
                or row["chunk_size"] or 0
            chunk = int(raw_chunk)
            if chunk <= 0:
                raise ApiError(400, "片号模式需要 chunk_size（建单时带或 X-Chunk-Size）")
            calc = part_no * chunk
            if offset is not None and offset != calc:
                raise ApiError(400, "part=%d 对应 offset=%d，与传入 offset=%d 不一致"
                               % (part_no, calc, offset))
            offset = calc
            if length > chunk:
                raise ApiError(413, "分片长度 %d 超过 chunk_size %d" % (length, chunk))

        if cr:
            cm = re.match(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", cr)
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
        if (q.get("autocomplete") == ["1"] or self.headers.get("X-Auto-Complete") == "1") \
                and new_offset >= size:
            self.app.complete(user, row["id"])
        percent = round(new_offset * 100.0 / size, 2) if size else 0.0
        headers = {"X-Upload-Offset": new_offset, "Upload-Offset": str(new_offset),
                   "X-Upload-Size": size, "X-Upload-Percent": "%.2f" % percent,
                   "Tus-Resumable": "1.0.0"}
        self._send(200, {"ok": True, "upload_id": row["id"], "offset": new_offset,
                         "size": size, "written": written, "percent": percent,
                         "remaining": max(0, size - new_offset),
                         "done": new_offset >= size}, headers=headers)

    def _patch(self):
        """Tus 1.0.0：PATCH /files/{id}，最后一片自动 complete。"""
        path = self._path()
        uid = self._upload_id(path)
        if not uid or not path.startswith("/files/"):
            raise ApiError(404, "未知路径 %s" % path)
        row = self.app.store.upload(uid)
        user = self._guard(row)
        if row["status"] != "uploading":
            raise ApiError(409, "上传单状态为 %s，不能再写" % row["status"])
        offset = self._int_header("Upload-Offset")
        if offset is None:
            raise ApiError(400, "Tus PATCH 需要 Upload-Offset 请求头")
        length = int(self.headers.get("Content-Length") or -1)
        if length < 0:
            raise ApiError(411, "PATCH 需要 Content-Length")
        new_offset, size, written = self.app.append(row, offset, self.rfile, length)
        if new_offset >= size:
            self.app.complete(user, uid)
            row = self.app.store.upload(uid)
        self._send(204, None, headers=self._tus_headers(row))

    def _delete(self):
        path = self._path()
        uid = self._upload_id(path)
        if not uid:
            raise ApiError(404, "未知路径 %s" % path)
        user = self._guard(self.app.store.upload(uid))
        view = self.app.cancel(user, uid)
        if path.startswith("/files/"):
            return self._send(204, None, headers={"Tus-Resumable": "1.0.0"})
        return self._send(200, {"ok": True, "upload": view})


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
