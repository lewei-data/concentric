"""素材二进制预览的签名 URL（服务端待办 S16）。

语义：URL 自鉴权——`sig = HMAC-SHA256(secret, mid:exp)`，不带个人 key（避免进访问日志与
Referer）；短时效（默认 600s）；不含服务端存储路径。

- 密钥：`DATA_DIR/media_secret.key`（0600，首次自动生成）。桥进程（mcp-bridge）读同一份
  文件做校验，两个进程不共享内存，靠这份密钥文件对齐。
- 校验端：mcp-bridge 的 `GET /mcp/media/raw?mid=&exp=&sig=`（nginx 把 `/mcp/media/` 反代到
  9104），支持 Range（206/Content-Range），供 `<video>/<img src>` 直接播放与拖动进度。
"""

import hashlib
import hmac
import os
import time

from . import config

TTL = int(os.environ.get("OPC_MCP_MEDIA_URL_TTL", "600"))
PUBLIC_BASE = os.environ.get("OPC_MCP_PUBLIC_BASE",
                             "https://ai.boraydata.com/mcp").rstrip("/")


def _secret_path():
    return config.DATA_DIR / "media_secret.key"


def secret() -> bytes:
    """读共享密钥；缺失则生成（0600）。桥进程只读不写。"""
    p = _secret_path()
    try:
        return p.read_bytes().strip()
    except OSError:
        raw = os.urandom(32).hex().encode()
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return raw


def sign(material_id: str, exp: int) -> str:
    msg = ("%s:%d" % (material_id, exp)).encode("utf-8")
    return hmac.new(secret(), msg, hashlib.sha256).hexdigest()


def media_url(material_id: str, ttl=None):
    """返回 (download_url, expires_in)。"""
    ttl = int(ttl or TTL)
    exp = int(time.time()) + ttl
    sig = sign(material_id, exp)
    url = "%s/media/raw?mid=%s&exp=%d&sig=%s" % (PUBLIC_BASE, material_id, exp, sig)
    return url, ttl
