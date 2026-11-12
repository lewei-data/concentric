---
name: sefs-nginx-gateway
description: Operate the ai.boraydata.com nginx gateway on 192.168.31.208 that proxies /conring routes to 192.168.21.206 (206 万兆 21 网段). Use when adding a route, debugging 401 404 502 000, or reloading nginx and TLS. Do not use for app code changes.
---

# SEFS Nginx 网关（ai.boraydata.com）

公网唯一入口是 **208**（`192.168.31.208`，nginx/1.24.0 Ubuntu，SSH 22 免密，`sudo` 免密）；业务服务跑在 **206**。206 本机也装了 nginx，但只有默认欢迎页，**不承载公网流量**，不要在那里改配置。

## 网络约定（硬规则）：21 网段 = 万兆，优先

两台机各有两条链路，**/conring 上游一律写 21 网段**：

| 主机 | 21 网段（万兆，优先） | 31 网段（千兆办公网） |
|---|---|---|
| 206 | `192.168.21.206/24` @ `ens2f1np1`（MTU 4200） | `192.168.31.206/23` @ `eno1np0`（默认路由） |
| 208 | `192.168.21.208/24` @ `ens2f1np1`（MTU 4200） | `192.168.31.208/23` @ `eno1np0` |

- **208 → 206 的反代必须走 `192.168.21.206`**（实测 4~12ms）；写 `192.168.31.206` 也能通，但绕千兆网，属于退化配置。
- 206 的服务（9100/9101/9102/9120/9121）仍绑 `0.0.0.0`，因此 21 与 31 两个地址都能连；**办公网手机直连 31 网段是保留能力，不要改成只绑 21**。
- `media-engine` 的 9200 是例外：只绑 `192.168.21.206`（`run.sh` 里 `MEDIA_HOST`），**不再监听回环**。任何本机调用方都必须写 `http://192.168.21.206:9200`，写 `127.0.0.1:9200` 会连接被拒。
- 21 网段是 206↔208 的直连链路，办公网设备**无法**路由到 `192.168.21.x`；对外仍走 `https://ai.boraydata.com`。

```
客户端 ──https──▶ 208 nginx:443
                   ├─ /conring/{mail,tts,asr}/   → 192.168.21.206:9100 / 9101 / 9102
                   ├─ /conring/media/            → 192.168.21.206:9200（/files/* 固定 404）
                   ├─ /conring/dispatch/         → 192.168.21.206:9120
                   ├─ /conring/dispatch-ws/      → 192.168.21.206:9121 (WebSocket)
                   └─ 其余（含 /conring/ 本身）   → 127.0.0.1:8765（208 本机 sefs-mcp，负责 /token 与 401）
```

## 文件职责

| 路径（208） | 作用 |
|---|---|
| `/etc/nginx/snippets/conring-mcp.conf` | **唯一的 /conring 路由表**，被下面两个 server 同时 include |
| `/etc/nginx/conf.d/proxy_8001.conf` | 443 server，certbot 证书，`/` → 127.0.0.1:8765 |
| `/etc/nginx/conf.d/sefs-mcp.conf` | 80 default_server，同样 include 上面的 snippet |
| `/etc/nginx/conf.d/gzip.conf` | http 级 gzip，只压 json 与 m3u8（不能压 SSE） |

改路由**只改 snippet**；两个 server 块里的 `location /`、证书、`client_max_body_size` 不要重复添加。

## 当前路由（2026-11-11 实测）

| 公网路径 | 上游（206） | 无 token 期望 |
|---|---|---|
| `/conring/asr/mcp` | 9102 | 400 `Missing session ID` |
| `/conring/tts/mcp` | 9101 | 400 |
| `/conring/mail/ceo/mcp` | 9100 | 400 |
| `/conring/asr/assets/*` | 9102 静态目录 | 200 |
| `/conring/dispatch/health` | 9120 | 200 |
| `/conring/media/health` | 9200 | 200 |
| `/conring/media/v1/voices` | 9200 | 200 |
| `/conring/media/v1/assets/intro/<id>.mp4` | 9200 | 200（支持 Range/206） |
| `/conring/media/files/*` | — | 404（nginx 直接拦，不转发） |
| `/conring/`、`/conring/nope/` | 208:8765 | 401 `realm="sefs-mcp"` |

## 改配置的固定流程

208 上 `sudo` 免密，可直接执行：

```sh
ssh boray@192.168.31.208
sudo cp /etc/nginx/snippets/conring-mcp.conf \
        /etc/nginx/snippets/conring-mcp.conf.bak-$(date +%Y%m%d-%H%M%S)
sudo vi /etc/nginx/snippets/conring-mcp.conf
sudo nginx -t
sudo systemctl reload nginx      # 不用 restart，reload 不断开 SSE
```

回到 206 验收：

```sh
sh /home/boray/opc-workbench/.trae/skills/sefs-nginx-gateway/scripts/verify-routes.sh
```

回滚（同目录已有多份 `.bak-*` 是历史操作留下的）：

```sh
sudo cp /etc/nginx/snippets/conring-mcp.conf.bak-<时间戳> \
        /etc/nginx/snippets/conring-mcp.conf
sudo nginx -t && sudo systemctl reload nginx
```

## 新增一条 /conring 路由

在 `conring-mcp.conf` 末尾追加，模板见 `assets/conring-mcp.conf`。最小形态：

```nginx
location /conring/<svc>/ {
    proxy_pass http://192.168.21.206:<port>/;   # 尾斜杠必须保留；上游永远写 21 网段
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

WebSocket 路由把 `Connection` 换成 `"upgrade"`，并加 `proxy_set_header Upgrade $http_upgrade;`（照抄 `dispatch-ws`）。

## 坑与判定规则

- **`proxy_pass` 尾斜杠** 决定是否剥离前缀。带尾斜杠：`/conring/asr/mcp` → `http://206:9102/mcp`。去掉尾斜杠，206 会收到完整的 `/conring/asr/mcp` 并 404。
- **401 不是 nginx 产生的**。nginx 里没有任何 `auth_request`/`auth_basic`；401 的 JSON 与 `WWW-Authenticate: Bearer realm="sefs-mcp"` 都来自 208:8765。看到 401 说明该路径**没有匹配到专属 location**。
- **502** = 206 上服务没监听、只绑了回环，或者 208 到 `192.168.21.206` 这条万兆链路断了。dispatch 的 `DISPATCH_HOST` 默认就是回环，必须显式 `0.0.0.0`（见 `/opt/sefs/hermes/dispatch/start.sh` 顶部注释）。先 `ssh boray@192.168.31.208 curl -s -o /dev/null -w '%{http_code}' http://192.168.21.206:<port>/health` 判断是链路还是进程。
- **curl 返回 000** = 连接建立后被上游断开，常见于对目录路径发 GET（如 `/conring/tts/`、`/conring/asr/assets/`），用具体文件或 `/mcp` 验证。
- **MCP 端点没有 nginx 层 token 校验**：带假 token 访问 `/conring/asr/mcp` 仍返回 400，说明鉴权在 206 服务内部，安全边界不在 208。
- **SSE 长连接**：`proxy_buffering off` + `proxy_read_timeout 3600s` + `Connection ""` 三者缺一不可，否则 MCP 流式响应会被缓冲或 60s 断开。
- **证书由 certbot 管理**：`ssl_certificate` 行由 certbot 写入，续期靠 `certbot.timer`，不要手改这两行；证书在 `/etc/letsencrypt/live/ai.boraydata.com/`。

## media-engine（9200）暴露现状（2026-11-11 已上线）

- 9200 只绑 `192.168.21.206`（由 `/opt/sefs/opc-workbench/media-engine/run.sh` 的 `MEDIA_HOST` 决定），**回环不可用**。
- 208 只代理 `/health` 与 `/v1/*`；`/conring/media/files/` 在 nginx 层 `return 404`，因为引擎的 `/files/{name}` 无鉴权、产物 TTL 24h。
- 所以 `/v1/tts` 返回的 `audio_url`（`/files/xxx`）**不能**直接拼公网地址；要音频走 9101 的 MCP 门面，或后续加签名 URL。
- 数字人成片走 `/conring/media/v1/assets/intro/<id>.mp4`（Range 可用）；App 现有 `/conring/asr/assets/intro/<id>.mp4` 通路仍有效（9102 静态目录）。
- 本机调用方统一 `MEDIA_ENGINE_URL=http://192.168.21.206:9200`：hermes gateway unit、`/opt/sefs/mcp-tts/run.sh`、`media-voice` 与 `media-platform` 插件默认值。
- 改 9200 绑定前先想清楚：绑 21 之后**所有** `127.0.0.1:9200` 调用方都会连接被拒，必须同步改。
