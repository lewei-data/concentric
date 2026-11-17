"""断点续传客户端：把本地大文件分片上传到 opc-upload 服务（默认 9105）。

分片与续传逻辑：
  1. 建单：POST /v1/uploads {filename,size}，服务端建空 .part 文件，offset=0
  2. 分片：每次 PUT /v1/uploads/{id}?offset=N，body 为本片原始字节，带 Content-Range
     服务端校验 offset == 当前偏移后才追加，追加完 fsync 并推进 offset
  3. 断点：进程被 kill / 网络断，重跑同一命令即可。offset 以服务端为准
     （GET /progress），不依赖本地记录
  4. 重试：5xx / 超时后先查进度，服务端已写入的字节直接跳过，避免重复传输
  5. 重复提交：分片已入库但响应丢失时，服务端回 409 + 当前 offset，客户端按 409
     携带的 offset 跳到下一片（幂等）
  6. 收尾：本地算 sha256 → POST /complete 带 X-Content-SHA256，服务端流式复核、
     去重、入库并排队分析

用法：
  OPC_UPLOAD_TOKEN=<session_token> python3 -m opc_mcp.upload_client 资料.zip
  python3 -m opc_mcp.upload_client 资料.zip --token <t> --base http://127.0.0.1:9105 \
      --chunk-mb 4 --retries 5
  python3 -m opc_mcp.upload_client 资料.zip --upload-id up_xxxxxxxxxxxxxxxx
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib import error, request as urlreq

CHUNK_MB = 8
STATE_DIR = Path(os.environ.get("OPC_UPLOAD_STATE_DIR") or (Path.home() / ".cache" / "opc-upload"))


class Fatal(RuntimeError):
    """不可重试的错误。"""


class Interrupted(RuntimeError):
    """本次中断，但可续传（状态已保存在服务端）。"""


def _load(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"raw": raw[:400].decode("utf-8", "replace")}


def api(method, url, token, data=None, headers=None, timeout=600):
    """返回 (状态码, 响应头, JSON)；连接失败时状态码为 None。"""
    req = urlreq.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        req.add_header(k, str(v))
    try:
        with urlreq.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), _load(r.read())
    except error.HTTPError as e:
        return e.code, dict(e.headers), _load(e.read())
    except (error.URLError, OSError) as e:                 # 超时 / 连接被重置 / DNS 失败
        return None, {}, {"error": "%s: %s" % (type(e).__name__, e)}


def human_bytes(n):
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= scale:
            return "%.1f %s" % (n / scale, unit)
    return "%d B" % n


def human_rate(bps):
    return human_bytes(bps) + "/s" if bps and bps > 0 else "?"


# ---------------------------------------------------------------- 本地状态
def state_key(path, size, mtime_ns, base):
    raw = "%s|%d|%d|%s" % (path.resolve(), size, mtime_ns, base)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def state_load(spath):
    try:
        return json.loads(spath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def state_save(spath, payload):
    if not spath:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        spath.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print("警告：状态文件写入失败（不影响续传，offset 以服务端为准）：%s" % e, file=sys.stderr)


def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(4 << 20)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


# ---------------------------------------------------------------- 服务端动作
def create_upload(base, token, filename, size, timeout):
    body = json.dumps({"filename": filename, "size": size}).encode()
    code, _, out = api("POST", base + "/v1/uploads", token, body,
                       {"Content-Type": "application/json"}, timeout)
    if code != 201:
        raise Fatal("建单失败：HTTP %s %s" % (code, out.get("error") or out))
    return out


def fetch_progress(base, token, uid, timeout):
    return api("GET", "%s/v1/uploads/%s/progress" % (base, uid), token, timeout=timeout)


def put_chunk(base, token, uid, offset, blob, size, timeout):
    headers = {"Content-Type": "application/octet-stream",
               "Content-Range": "bytes %d-%d/%d" % (offset, offset + len(blob) - 1, size)}
    return api("PUT", "%s/v1/uploads/%s?offset=%d" % (base, uid, offset), token, blob,
               headers, timeout)


def put_with_retry(base, token, uid, offset, blob, size, args, say):
    """上传一个分片，返回服务端新 offset。已提交但响应丢失时按 409 的 offset 跳过。"""
    delay = 1.0
    for attempt in range(1, args.retries + 1):
        code, _, body = put_chunk(base, token, uid, offset, blob, size, args.timeout)

        if code == 200:
            return int(body.get("offset", offset + len(blob)))

        if code == 409:
            cur = body.get("offset")
            if isinstance(cur, int) and cur > offset:
                say("  服务端已收到该分片（409），跳到 %s" % human_bytes(cur))
                return cur
            # 服务端状态异常（如已 done/canceled），看进度决定
            pcode, _, prog = fetch_progress(base, token, uid, args.timeout)
            if pcode == 200 and prog.get("status") in ("done", "canceled", "expired", "corrupt"):
                raise Fatal("上传单状态为 %s：%s" % (prog.get("status"), body.get("error") or ""))
            say("  偏移被拒（409）：%s" % (body.get("error") or ""))
        elif code == 401:
            raise Fatal("鉴权失败：token 无效或已过期")
        elif code == 410:
            raise Interrupted("临时文件已丢失（410），请去掉 --upload-id 重新建单上传")
        elif code in (400, 411, 413):
            raise Fatal("请求被拒绝（HTTP %s）：%s" % (code, body.get("error") or body))
        else:
            say("  分片重试 %d/%d：%s" % (attempt, args.retries,
                                        body.get("error") or "HTTP %s" % code))

        time.sleep(delay)
        delay = min(delay * 2, 15)
        # 重试前先问服务端真实 offset：超时但实际已写入的分片不再重传
        pcode, _, prog = fetch_progress(base, token, uid, args.timeout)
        if pcode == 200 and int(prog.get("offset") or 0) > offset:
            say("  服务端已推进到 %s，继续续传" % human_bytes(prog["offset"]))
            return int(prog["offset"])

    raise Interrupted("连续 %d 次重试失败，已停在 offset=%d，重跑同一命令可续传"
                      % (args.retries, offset))


# ---------------------------------------------------------------- 主流程
def upload(args):
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise Fatal("文件不存在：%s" % path)
    st = path.stat()
    size = st.st_size
    if size <= 0:
        raise Fatal("空文件不需要分片上传")
    base = args.base.rstrip("/")
    token = args.token or os.environ.get("OPC_UPLOAD_TOKEN") or ""
    if not token:
        raise Fatal("缺少 token：用 --token 或环境变量 OPC_UPLOAD_TOKEN（来自 opc_init）")

    spath = None
    if not args.no_state:
        spath = STATE_DIR / (state_key(path, size, st.st_mtime_ns, base) + ".json")
    meta = state_load(spath) if spath else None

    uid = args.upload_id or (meta or {}).get("upload_id")
    view = None
    if uid:
        code, _, body = fetch_progress(base, token, uid, args.timeout)
        if code == 200:
            if int(body.get("size") or 0) != size:
                raise Fatal("upload_id %s 的总大小 %s 与本文件 %d 不一致"
                            % (uid, body.get("size"), size))
            view = body
        elif code in (403, 404):
            print("上传单 %s 不可用（HTTP %s），重新建单" % (uid, code))
            uid = None
        else:
            raise Fatal("查询进度失败：HTTP %s %s" % (code, body.get("error") or body))

    if view and view.get("status") == "done":
        print("该文件已完成上传：material_id=%s sha256=%s" % (view.get("material_id"), view.get("sha256")))
        return 0
    if view and view.get("status") not in ("uploading",):
        print("上传单状态为 %s，重新建单" % view.get("status"))
        uid, view = None, None

    if not uid:
        created = create_upload(base, token, path.name, size, args.timeout)
        uid = created["upload_id"]
        offset = 0
        state_save(spath, {"upload_id": uid, "size": size, "base": base,
                           "filename": path.name, "created_at": time.time()})
        print("建单：%s（%s）" % (uid, human_bytes(size)))
    else:
        offset = int(view.get("offset") or 0)
        state_save(spath, {"upload_id": uid, "size": size, "base": base,
                           "filename": path.name, "created_at": (meta or {}).get("created_at")})
        print("续传：%s，已有 %s / %s（%.2f%%）" % (uid, human_bytes(offset),
                                                human_bytes(size), offset * 100.0 / size))

    chunk = max(64 * 1024, args.chunk_mb * 1024 * 1024)
    t0 = time.time()
    start = offset
    say = (lambda *a: None) if args.quiet else (lambda m: print(m, flush=True))

    with path.open("rb") as f:
        while offset < size:
            n = min(chunk, size - offset)
            f.seek(offset)
            blob = f.read(n)
            if len(blob) != n:
                raise Fatal("本地文件读取不完整：期望 %d 实得 %d" % (n, len(blob)))

            offset = put_with_retry(base, token, uid, offset, blob, size, args, say)

            elapsed = max(time.time() - t0, 1e-6)
            rate = (offset - start) / elapsed
            eta = int((size - offset) / rate) if rate > 0 else None
            say("[%5.2f%%] %s / %s  %s  ETA %s" % (
                offset * 100.0 / size, human_bytes(offset), human_bytes(size),
                human_rate(rate), ("%ds" % eta) if eta is not None else "?"))

    digest = sha256_file(path)
    code, _, body = api("POST", "%s/v1/uploads/%s/complete" % (base, uid), token, b"",
                        {"Content-Length": "0", "X-Content-SHA256": digest}, args.timeout)
    if code == 200:
        print("完成：material_id=%s job_id=%s duplicate=%s sha256=%s"
              % (body.get("material_id"), body.get("job_id"),
                 body.get("duplicate"), body.get("sha256") or digest))
        state_save(spath, {"upload_id": uid, "size": size, "base": base,
                           "filename": path.name, "done": True,
                           "material_id": body.get("material_id")})
        return 0
    if code == 409:
        raise Interrupted("服务端认为尚未传完：%s" % body.get("error"))
    if code == 400:
        raise Fatal("sha256 校验失败：%s" % (body.get("error") or body))
    raise Fatal("完成失败：HTTP %s %s" % (code, body.get("error") or body))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="opc_mcp.upload_client",
        description="OPC 大文件分片断点续传客户端（zip/pdf/doc 等，单文件 ≤ 服务端上限）")
    ap.add_argument("file", help="本地文件路径")
    ap.add_argument("--token", help="session_token；默认读 OPC_UPLOAD_TOKEN")
    ap.add_argument("--base", default=os.environ.get("OPC_UPLOAD_BASE")
                    or "https://ai.boraydata.com/conring/upload",
                    help="上传服务地址（内网可用 http://192.168.21.206:9105）")
    ap.add_argument("--chunk-mb", type=int, default=CHUNK_MB, help="分片大小 MB（默认 8）")
    ap.add_argument("--retries", type=int, default=5, help="单片重试次数（默认 5）")
    ap.add_argument("--timeout", type=float, default=600, help="单次请求超时秒（默认 600）")
    ap.add_argument("--upload-id", help="指定已有上传单续传")
    ap.add_argument("--no-state", action="store_true", help="不读写本地状态文件")
    ap.add_argument("--quiet", "-q", action="store_true", help="只输出结果")
    args = ap.parse_args(argv)
    try:
        return upload(args)
    except KeyboardInterrupt:
        print("\n已中断。offset 已保存在服务端，重跑同一命令即可续传。", file=sys.stderr)
        return 3
    except Interrupted as e:
        print("中断：%s" % e, file=sys.stderr)
        return 3
    except Fatal as e:
        print("失败：%s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
