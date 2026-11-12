# 统一接入 Gateway `/v1/runs` 接口 —— 补齐方案

## 1. 背景与目标

当前有 **4 处**各自 `fork` `run-hermes.sh -z` 来执行 Hermes：

| 调用方 | 位置 | 现状 |
|---|---|---|
| mcp-asr（语音） | `/opt/sefs/mcp-asr/server.py:741` `run_hermes()`，4 个调用点（724 / 1135 / 2328 / 2345） | 每条语音指令 fork 一次，冷启动 10.47s / 未预热 16.10s |
| dispatchd（任务调度） | `/opt/sefs/hermes/dispatch/dispatchd.py:532` `run_hermes()` | `QUEUE_WORKERS=2`、`HERMES_PARALLEL=4`，注释写明「-z 是一次性进程，无法常驻复用」 |
| mcp-mail（邮件派单） | `/opt/sefs/mcp-mail/dispatch.py:164` | `HERMES_PROFILE=aid` + `-z` |
| prototype（工作台） | `/opt/sefs/opc-workbench/prototype/server.py:816` | `CLI -z <prompt> --usage-file`，`WORKERS=6` |

而 gateway 已内建 `api_server` 平台适配器，是官方一等公民的常驻执行入口：
`gateway/platforms/api_server.py:1-9` 明确写了 `/v1/runs` 路由；`api_server_runs.py` 实现了
准入（202 + `run_id`）、状态、事件、审批/转向/停止、`Idempotency-Key` 幂等。
另外 `gateway_state.json` 显示 `multiplex_profiles: true`、已服务 18 个 profile
（default/ceo/cco/cto/…/secretary），可直接用 `/p/<profile>/v1/runs` 复用同一常驻 agent。

**目标**：4 处调用全部统一走 gateway `/v1/runs`（保留子进程作为故障回退），
语音链路从「每句话冷启动」变为「常驻 agent 热执行」，并补齐暴露面、鉴权、并发、审批、会话、回滚等缺口。

**非目标**：不改 App 端协议（App 仍走 nginx → mcp-asr）；不做 ASR/TTS 上收到 gateway
（`/v1/capabilities` 里 `audio_api: False`、`realtime_voice: False`，语音仍由 mcp-asr + 9101 TTS 负责）。

---

## 2. 现状事实（全部为本机实测/源码确认）

1. **api_server 未启用**：`ss -ltnp` 无 8642；仅 `127.0.0.1:48643`（dashboard/消息平台层）。
   `/opt/sefs/hermes/.env` 中 **没有** `API_SERVER_KEY`。
2. **启用方式有两条**：
   - 环境变量：`.env` 写 `API_SERVER_KEY`（≥16 字符）→ `gateway/config.py:548-563 _has_usable_api_server_key`
     经 `config_env._api_server()` 自动启用，`host/port` 由 `API_SERVER_HOST` / `API_SERVER_PORT` 桥接
     （`api_server.py:155 DEFAULT_PORT = 8642`）。
   - YAML：`platforms.api_server.enabled + key`。**两处都不要重复存密钥**，本方案只走 `.env`。
3. **`/p/<profile>` 是 fail-closed 且读 profile 自己的 secret scope**：
   `_expected_api_key()` 对非 default profile 走 `agent/secret_scope.get_secret("API_SERVER_KEY")`；
   在 multiplex 路由 profile 下，scoped 未命中返回 `""` → 直接 401。
   **结论：17 个 `profiles/*/.env` 必须各自写入同一把 key**（`ls /opt/sefs/hermes/profiles` 共 17 个，无 `default`）。
4. **并发上限只在 YAML**：`gateway.api_server.max_concurrent_runs`（默认 10，超限 429，无 env 覆盖）。
   4 处合计峰值 ≈ mcp-asr 3 + dispatchd 8 + prototype 6 + mail 少量 → **需调到 32**。
5. **会话 ID 校验**：`api_server.py:2589-2592` 禁止 `\r\n\0`、`_is_path_unsafe`（含 `/ \ .. :`），有长度上限。
   我们生成的 id 必须匹配 `^[a-z0-9][a-z0-9._-]{2,80}$`。
6. **审批会挂起**：run 可能进入 `waiting_for_approval`（`api_server_runs.py:1004`），
   必须 `POST /v1/runs/{id}/approval {"choice": "deny"|"once"|"session"|"always"}`（`:1371-1377`）才继续。
   无人审批的语音场景必须显式裁决，否则会一直占用并发槽直到超时。
7. **终态**：`TERMINAL_STATUSES = {completed, failed, cancelled, interrupted}`
   （`api_server_run_idempotency.py:19`）。状态载荷含 `output / error / usage / pending_steer / session_id`。
8. **工具集差异**：`platform_toolsets` 无 `api_server` 行 → 默认 `hermes-api-server` 复合集，
   主动剔除 `clarify`、`send_message`、`text_to_speech`、`computer_use`、kanban
   （`hermes_cli/platforms.py:34`、`toolsets.py:193`、`website/docs/reference/toolsets-reference.md:98`）。
   与 CLI 的 `platform_toolsets.cli` 不完全一致，需明确取舍（见 §4.1）。
9. **nginx（208）无 gateway 路由**：`/etc/nginx/snippets/conring-mcp.conf` 现有 5 条
   （asr→9102、mail→9100、tts→9101、dispatch→9120、dispatch-ws→9121），需新增 gw。
10. **mcp-asr 零第三方依赖**：仅 stdlib + `urllib` + 线程池（`JOB_WORKERS=3`），
    客户端必须用 `urllib.request` 实现，不能用 requests/httpx。

---

## 3. 目标架构

```
Android App ──HTTPS──> ai.boraydata.com (208 nginx)
                        ├─ /conring/asr/      → 206:9102  mcp-asr ─┐
                        ├─ /conring/mail/     → 206:9100            │
                        ├─ /conring/tts/      → 206:9101            │  Bearer API_SERVER_KEY
                        ├─ /conring/dispatch/ → 206:9120 dispatchd ─┤  (loopback 192.168.31.206:8642)
                        └─ /conring/gw/       → 206:8642 ───────────┤
                                                                     ▼
                                          hermes-gateway (multiplex, 18 profiles)
                                          POST /v1/runs           → default profile
                                          POST /p/<id>/v1/runs    → 员工 profile
                                          GET  /v1/runs/{id}      → 状态轮询
                                          POST /v1/runs/{id}/stop|approval
```

执行语义：`submit(202)` → 轮询 `GET /v1/runs/{id}`（0.4s→1s 退避）→ 终态取 `output`；
超时/取消 → `POST /stop`；`waiting_for_approval` → 按 `GW_APPROVAL_POLICY` 自动裁决。

---

## 4. 变更清单

### 4.1 Gateway 启用 `api_server`

**文件 A：`/opt/sefs/hermes/.env`（追加，勿覆盖）**

```dotenv
# --- gateway api_server（统一接入 /v1/runs） ---
API_SERVER_KEY=<openssl rand -hex 24 生成，48 字符>
API_SERVER_HOST=192.168.31.206
API_SERVER_PORT=8642
```

> 绑 `192.168.31.206`（非 loopback）是 `SECURITY.md §2.6.5` 所说的 break-glass 决定，
> 必须配套 §4.6 的 iptables 白名单，并在文件里写一行注释说明原因。

**文件 B：`/opt/sefs/hermes/profiles/<id>/.env` × 17（追加同一把 key）**

```dotenv
API_SERVER_KEY=<同 A>
```

脚本化执行（幂等：已存在则替换该行，不存在则追加），涉及 profile：
`ceo cco cto cfo coo cmo chro ciso cdo cso cpo cto_ai … secretary`（以 `ls` 实际目录为准）。

**文件 C：`/opt/sefs/hermes/config.yaml`（只加并发与工具集，不加 key）**

```yaml
gateway:
  api_server:
    max_concurrent_runs: 32
```

`platform_toolsets` 决策：**保持默认 `hermes-api-server`**。
理由：语音/派单是程序化调用，`clarify`（交互澄清）、`text_to_speech`（mcp-asr 自己有 9101）、
`send_message` 都不应出现；`computer_use` 被剔除属于可接受妥协（当前无语音指令依赖点击屏幕）。
若后续确需与 CLI 完全对齐，再单独加 `platform_toolsets.api_server: [hermes-api-server, computer_use]`。

**重启**：`systemctl restart hermes-gateway-8987483c.service`
**验收**：`ss -ltnp | grep 8642` 出现 `192.168.31.206:8642`；`curl -s localhost:8642/health` 返回
`{"status":"ok"}`（`/health` 免鉴权，`:2266-2268`）；`/health/detailed` 带 Bearer 返回平台表含 `api_server`。

### 4.2 新增统一客户端 `/opt/sefs/hermes/gw_runs_client.py`（唯一新增文件）

stdlib only（`urllib.request` + `json` + `threading`），约 180 行，被 4 个调用方共享导入。

**公开 API**

```python
class GatewayUnavailable(RuntimeError)   # 连接失败 / 5xx / 401 / 熔断中
class GatewayBusy(RuntimeError)          # 429（重试一次后仍 429）
class RunFailed(RuntimeError)            # status=failed，携带 error
class RunCancelled(RuntimeError)         # cancelled / interrupted
class RunTimeout(RuntimeError)           # 超时，已尽力 stop

@dataclass
class RunResult:
    text: str
    usage: dict
    run_id: str
    elapsed: float

def run(prompt, *, profile=None, timeout=240, idem_key=None, session_id=None,
        cancel=None, want_usage=False) -> RunResult
def health() -> bool
def served_profiles() -> set[str]      # 读 /opt/sefs/hermes/gateway_state.json，60s 缓存
def mode() -> str                      # HERMES_EXEC_MODE: auto|gateway|subprocess
```

**实现要点（决策已定，执行时不再选型）**

1. `BASE = os.environ.get("HERMES_GW_BASE", "http://192.168.31.206:8642")`；
   `KEY = os.environ.get("HERMES_GW_KEY") or _read_env("/opt/sefs/hermes/.env", "API_SERVER_KEY")`。
2. URL：`profile` 非空 → `{BASE}/p/{profile}/v1/runs`；否则 `{BASE}/v1/runs`。
   所有后续 `status/stop/approval` 用**同一个 base 前缀**（profile 前缀必须保持一致）。
3. 请求头：`Authorization: Bearer <KEY>`、`Content-Type: application/json`、
   `Idempotency-Key: <idem_key>`、`X-Hermes-Session-Id: <session_id>`（后两者有值才发）。
4. 请求体：`{"input": prompt}`（只发 `input`，不传 `model`，走 profile 默认模型）。
5. **幂等键推导由调用方给**，规则统一写成 helper `make_idem(prefix, client_id, prompt)`：
   `prefix + "-" + sha256(client_id + "\0" + prompt)[:40]` → 同一 `client_id` + 同一文本重试才重放，
   文本变化不会触发 409 conflict。
6. 状态机：
   - `queued` / `running` / `waiting_for_approval` → 继续轮询（间隔 0.4s×12 次后转 1.0s）
   - `waiting_for_approval` → `POST .../approval {"choice": GW_APPROVAL_POLICY}`（默认 `deny`），
     然后继续轮询；拒批后模型会收到拒绝结果继续作答，不会卡死
   - `completed` → 取 `output`（dict 时取 `output.text` 或 `json.dumps(..., ensure_ascii=False)`）
   - `failed` → `RunFailed(error)`
   - `cancelled` / `interrupted` → `RunCancelled`
   - 超过 `timeout` → `POST /stop` 后 `RunTimeout`
   - `cancel`（`threading.Event`）被 set → 同上 stop 路径
7. **熔断**：模块级 `fail_count / open_until`；60s 内连续 3 次 `GatewayUnavailable` → 打开 60s，
   期间 `run()` 直接抛 `GatewayUnavailable`（调用方回落子进程），任意一次成功即清零。
8. `health()`：`GET /health`，连接超时 0.5s / 读 2s，结果缓存 5s。
9. 日志：统一 `[gw] submit profile=%s run=%s idem=%s`、`[gw] done run=%s elapsed=%.2fs status=%s`。

### 4.3 mcp-asr（`/opt/sefs/mcp-asr/server.py`）

1. 顶部 `sys.path.insert(0, "/opt/sefs/hermes")` 后 `import gw_runs_client as gw`。
2. 把现有 `run_hermes` 拆成两层，**原实现改名 `_run_hermes_subprocess` 原样保留**：

```python
def run_hermes(prompt, profile=None, timeout=240, *, idem_key=None, session_id=None):
    if gw.mode() != "subprocess":
        try:
            return gw.run(prompt, profile=profile, timeout=timeout,
                          idem_key=idem_key, session_id=session_id).text
        except (gw.GatewayUnavailable, gw.GatewayBusy):
            if gw.mode() == "gateway":
                raise
            log("gateway 不可用，回落子进程 profile=%s", profile)
    return _run_hermes_subprocess(prompt, profile, timeout)
```

3. 4 个调用点改造：

| 位置 | 传入 | 说明 |
|---|---|---|
| `:724` 澄清语 `_clarify_line` | `profile=ASR_CLARIFY_PROFILE or None`，**不带 session** | 一次性短文本，保持无上下文 |
| `:1135` 自我介绍文案 | `profile=None` | 同上 |
| `:2328 _delegate_to_employee` | `profile=employee`、`session_id=emp_session(employee)`、`idem_key=make_idem("asr", client_id, text)` | 员工派单走 `/p/<employee>` |
| `:2345 _run_job` 未点名 | `profile=SECRETARY_ID` | 逻辑不变 |

4. `_profile_ready(pid)` 增加 gateway 分支：gateway 模式下校验 `gw.served_profiles()` 包含 `pid`
   （数据源 `gateway_state.json` 的 `served_profiles`），不再只依赖 `os.path.isdir`；
   仍保留目录检查作为子进程回退路径的前置条件。
5. **会话策略**（用户已确认要连续对话）：
   - 实时通话：`session_id = "live-" + live_sid`（一通电话一个会话，挂断即失效）。
   - 非通话 job：`emp_session(profile)` 维护 `{profile: (sid, last_ts, seq)}`（带锁）：
     `sid = f"asr-{profile or 'default'}-{yyyymmdd}-{seq}"`，当 `now - last_ts > ASR_SESSION_IDLE`
     （默认 1800s）或跨日时 `seq += 1` 轮换，避免上下文无限增长。
   - 字符集只允许 `[a-z0-9._-]`，长度 ≤ 64，满足 `api_server.py:2589-2592` 校验。
6. 进度与看门狗：`gw.run()` 每次状态跃迁回调 `on_status`，`_run_job` 用它更新
   `job["stage"]`（`queued→running→tool/completed`）与 `job["progress_ts"]`，
   供既有的「空闲主动汇报」判断指令是否仍在推进（不引入 SSE，避免 worker 线程复杂度）。
7. 环境变量新增：`HERMES_EXEC_MODE`（默认 `auto`）、`GW_APPROVAL_POLICY`（默认 `deny`）、
   `ASR_SESSION_IDLE`（默认 1800）、`HERMES_GW_BASE`、`HERMES_GW_KEY`（可不设，读 `.env`）。

### 4.4 dispatchd（`/opt/sefs/hermes/dispatch/dispatchd.py:532`）

- 在 `_spawn_hermes` 之前插入 gateway 分支，`cancel` 直接透传给 `gw.run(..., cancel=cancel)`，
  取消时由客户端 `POST /stop`（比 `proc.kill()` 更干净，能回收 agent 任务）。
- `use_session=True` 且 profile 在 `RESUME_PROFILES` → `session_id = f"dsp-{profile}-{yyyymmdd}"`
  （等价于原 resume lane）；否则无 session（与现网一致）。
- 保留 `lane_hint`、`DEFAULT_TIMEOUT`、`_strip_ansi` 行为不变；`GatewayUnavailable/Busy` → 回落 `_spawn_hermes`。
- `QUEUE_WORKERS=2`、`HERMES_PARALLEL=4` 不改。

### 4.5 mcp-mail（`/opt/sefs/mcp-mail/dispatch.py:162-170`）与 prototype（`/opt/sefs/opc-workbench/prototype/server.py:45-51, 810-831`）

- mcp-mail：`plt = gw.run(prompt, profile=aid, timeout=AGENT_TIMEOUT)`，**不带 session**
  （每封邮件独立）；`GatewayUnavailable/Busy` → 保留原 `subprocess.run([HERMES,"-z",...])`。
  输出仍是严格 JSON，`parse_agent_output` 不变。
- prototype：`OPC_HERMES_BACKEND=gateway|cli`（默认 `gateway`）；
  `ask()` 里 gateway 分支用 `gw.run(q, profile=agent["id"], timeout=TIMEOUT, want_usage=True)`，
  `RunResult.usage` 直接替代 `--usage-file` 的 `_read_usage()`；`_extract`、两次重试、
  `latencyMs` 统计逻辑不变。
- 4 处共享同一个 `gw_runs_client`，不再各自维护一份 HTTP 逻辑。

### 4.6 nginx（208）新增 `/conring/gw/`

在 `/etc/nginx/snippets/conring-mcp.conf` 追加（与现有 5 条同风格）：

```nginx
# 统一 agent 执行入口（gateway /v1/runs）；Bearer 鉴权，勿加 CORS
location /conring/gw/ {
    proxy_pass http://192.168.31.206:8642/;
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
    client_max_body_size 2m;
    limit_req zone=conring_gw burst=20 nodelay;
}
```

配套：`http {}` 内加 `limit_req_zone $binary_remote_addr zone=conring_gw:10m rate=10r/s;`
（若已有同名 zone 则复用）。执行方式：按 `sefs-nginx-gateway` 技能操作 208，
`nginx -t && systemctl reload nginx`。

**路径映射**：`/conring/gw/v1/runs` → `/v1/runs`；`/conring/gw/p/secretary/v1/runs` → `/p/secretary/v1/runs`。

**网络白名单（必做，配合 §4.1 的非 loopback 绑定）**：

```bash
# 206 上：只允许本机与 208 访问 8642，其余 DROP
iptables -I INPUT -p tcp --dport 8642 -s 127.0.0.1 -j ACCEPT
iptables -I INPUT -p tcp --dport 8642 -s 192.168.31.208 -j ACCEPT
iptables -I INPUT -p tcp --dport 8642 -j DROP
# 持久化（按本机既有机制：iptables-persistent 或 netfilter-persistent save）
```

引用依据：`SECURITY.md §2.6` 第 1/2/5 条 —— 跨信任边界必须有调用方 allowlist；
无 allowlist 必须 fail closed；把本地 surface 绑到非 loopback 是 break-glass 决定。
`§4`（292-316 行）明确禁止「无认证/VPN/防火墙就暴露公网」。

---

## 5. 执行顺序（每步可独立回滚）

1. **网关侧启用**：写 default `.env` → 17 个 profile `.env` → `config.yaml` 加 `max_concurrent_runs` → 重启 → 验 `/health` + `/v1/toolsets`（带 Bearer）。
2. **新增客户端** `gw_runs_client.py`，先只被 mcp-asr 引用，`HERMES_EXEC_MODE=auto` 灰度。
3. **mcp-asr 改造**：`systemctl restart` 对应服务（9102）→ 语音回归（见 §6 V2）。
4. **dispatchd → mcp-mail → prototype** 依次切换，每处单独重启、单独回归。
5. **nginx + iptables** 最后做（先内网验证客户端 OK，再开公网入口），减少同时变更面。
6. 观察 24h 后把 `HERMES_EXEC_MODE` 从 `auto` 固化（`auto` 本身就带回落，可保持）。

---

## 6. 验证清单

**V1 gateway 自身**
```bash
KEY=$(grep -m1 '^API_SERVER_KEY=' /opt/sefs/hermes/.env | cut -d= -f2-)
curl -s localhost:8642/health                              # {"status":"ok"}
curl -s -H "Authorization: Bearer $KEY" localhost:8642/v1/capabilities | head -c 400
curl -s -H "Authorization: Bearer $KEY" localhost:8642/v1/toolsets | head -c 300
# default profile
curl -s -X POST localhost:8642/v1/runs -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: smoke-1' \
  -d '{"input":"只回复两个字：在的"}'                     # 202 + run_id
# 员工 profile（验证 17 个 .env 生效）
curl -s -X POST localhost:8642/p/secretary/v1/runs -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"input":"只回复两个字：在的"}'
```
预期：均 202；错 key → 401；无 key → 401（fail closed）。

**V2 mcp-asr 语音链路**
- 单条指令端到端延迟：改造前基线（首个请求 10.47s / 未预热 16.10s）→ 目标首字响应 < 3s。
- 连续 5 条同一会话指令：日志中 `run_id` 不同、`X-Hermes-Session-Id` 相同，模型能引用上一句。
- 空闲 > `ASR_SESSION_IDLE` 后再发：`session_id` 轮换（seq+1）。
- 同一 `client_id` 重发：网关返回 `replayed: true`，不重复执行（看 `[gw] submit` 日志）。

**V3 故障与边界**
- `systemctl stop hermes-gateway-8987483c` → 发指令：走子进程兜底成功（日志出现「回落子进程」）。
- 网关侧把 `max_concurrent_runs` 临时调到 1 并并发 3 条：验证 429 → 重试 → 兜底，不丢指令。
- 构造需要审批的指令（如写文件/执行命令）：确认按 `deny` 自动裁决，run 正常收敛为 `completed`，不卡 `waiting_for_approval`。
- `/v1/runs/{id}/stop`：取消后 mcp-asr job 标记为 cancelled 且 App 收到「已取消」。
- dispatchd 取消按钮 / mcp-mail 1 封测试邮件 / prototype 一次 6 并发（无 429）。

**V4 回归**
- App 录音→ASR→执行→TTS 全链路（真机）；`verifyVersionSync` 与 APK 无需改动（App 端零变更）。
- 208 公网：`curl https://ai.boraydata.com/conring/gw/health` 200；无 Bearer 调 `/v1/runs` → 401。

---

## 7. 回滚

| 层级 | 操作 |
|---|---|
| 单服务 | `HERMES_EXEC_MODE=subprocess` + 重启该服务，立即回到 `-z` |
| 全量 | 4 处全部 `HERMES_EXEC_MODE=subprocess`；gateway 侧 `.env` 注释掉 `API_SERVER_*` 重启 |
| nginx | `rm` 该 location 片段 → `nginx -t && systemctl reload nginx` |
| iptables | 删除 3 条 8642 规则 |
| config.yaml | 删除 `gateway.api_server` 块（并发上限回默认 10） |

---

## 8. 假设与决策

1. **只走 `.env` 存 key**，YAML 不重复存密钥（避免明文双份）。
2. **17 个 profile 复用同一把 key**：`SECURITY.md §2.6.4` 明确「同一授权集合内调用方同等信任」，
   需要能力隔离应拆实例而不是拆 key。
3. **默认工具集保持 `hermes-api-server`**，不强行对齐 `cli`（见 §4.1）。
4. **不接 SSE 事件流**，用状态轮询（0.4s/1s）+ 状态跃迁回调；理由见 §4.3-6。
5. **审批默认 `deny`**；老板口头指令无法完成交互式授权，`once` 仅作为显式环境变量开关。
6. **`ASR_SESSION_IDLE=1800`、`max_concurrent_runs=32`** 为初始值，按 §6 压测再调。
7. 假设 `192.168.31.206` 为本机 LAN 地址且网关进程 DNS 正常（gateway 已在跑，模型调用正常）。

---

## 9. 待确认风险（执行时如出现即暂停并回报）

- gateway 的 `api_server` 工具集/系统提示与 `-z` 存在语义差异，若某类指令通过 `/v1/runs` 结果明显劣化，
  需为该员工单独对齐 `platform_toolsets.api_server` 或 `platforms.api_server.extra.toolset`。
- 同一 `session_id` 的并发 run 会被 turn lease 串行化（`api_server_runs.py` 会话租约），
  语音连发场景可能排队；若实测延迟不可接受，改为「默认 stateless + 仅实时通话带 session」。
- 208 若不允许新增 location，退化方案：只在 206 内网使用，公网继续走 `/conring/asr/`（不影响 4 处改造收益）。

---

## 10. 执行记录（2026-11-11 22:00–22:30 实测）

### 10.1 已完成

| 层 | 结果 |
|---|---|
| gateway | `API_SERVER_KEY/HOST/PORT` 写入 `.env`（含 17 个 profile `.env` 同一把 key）；`config.yaml` 加 `max_concurrent_runs: 32`；重启后 `API server listening on http://0.0.0.0:8642` |
| 客户端 | `/opt/sefs/hermes/gw_runs_client.py` 上线并被 4 处共用 |
| mcp-asr | gateway 优先 + 子进程回落；会话 `asr-<profile>-<yyyymmdd>-<seq>`、实时通话 `live-<sid>`；`_profile_ready` 走 `served_profiles` |
| dispatchd | gateway 优先 + `cancel` 透传；`RESUME_PROFILES` 岗位带 `dsp-<profile>-<yyyymmdd>` 会话 |
| mcp-mail | `run_agent` gateway 优先、无会话；输出解析不变 |
| prototype | `OPC_HERMES_BACKEND`（默认 gateway）；`_gw_usage()` 把网关 usage 映射成原 `--usage-file` 结构 |
| nginx(208) | `/conring/gw/` → `192.168.21.206:8642`；`POST /conring/gw/v1/runs` 单独限速 |
| iptables(206) | `/usr/local/sbin/hermes-gw-firewall.sh` + `hermes-gw-firewall.service`（enabled）：lo / 31.208 / 21.208 放行，其余 DROP |

### 10.2 与计划的偏差（均已验证）

1. **上游改走 21 网段**：并行任务在此期间把 208 的 `/conring/*` 上游从 `192.168.31.206` 全量迁到
   `192.168.21.206`（万兆直连 ens2f1np1），因此 gateway 改为 `API_SERVER_HOST=0.0.0.0`，
   nginx 上游写 `192.168.21.206:8642`；iptables 白名单同时放行 `192.168.21.208`。
   期间该任务重写了 snippet（22:06:38），本任务的 location 已按新约定重新追加（现存 2 处 gw 段）。
2. **限速只挂在提交入口**：`limit_req` 放在 `location = /conring/gw/v1/runs`（精确匹配），
   而非整个 `/conring/gw/`——状态查询是 0.4s/次的轮询，按前缀限速会把正常轮询打成 429。
3. **持久化方式**：本机无 `iptables-persistent`，改用 oneshot systemd 单元，规则脚本幂等。
4. **V3 的 429/审批/stop 用假网关单测**（`/tmp/gw_fake_test.py`）验证，不重启生产网关；
   生产侧只做了「网关不可达 → 回落子进程」真实验证。

### 10.3 验证结果（本机 206 实测）

| 项 | 结果 | 来源 |
|---|---|---|
| `/health`、`/v1/capabilities`、`/v1/toolsets` | 200 / 带 key 正常 | curl 输出 |
| 无 key / 错 key 提交 | 401（fail closed） | curl 输出 |
| default、`/p/secretary`、`/p/ceo`、`/p/cw_gou` 真跑 | 全部 completed | 各 run 轮询输出 |
| 会话连续（同 session 追问「青龙」） | 第二次回答正确 | `gw.run` 实测 |
| 同 `client_id`+同文本重发 | `replayed`，<2s 返回 | 实测 |
| 429→重试→GatewayBusy；审批 deny 收敛；cancel→POST stop；连接拒绝→GatewayUnavailable | 4/4 PASS | `/tmp/gw_fake_test.py` |
| dispatchd `/submit` | 6.1s 完成，结果「在的」 | `/task/<id>` 输出 |
| prototype `ask()` | JSON 正确、`usage.totalTokens` 有值 | 实测 |
| prototype 3 并发 | 全成功、无 429、wall 14.4s | 实测 |
| mcp-asr 死网关回落 | 「回落子进程 profile=secretary」→ 11.2s 返回 | 实测日志 |
| `HERMES_EXEC_MODE=gateway` 严格模式 | 直接抛 `GatewayUnavailable` | 实测 |
| 公网 `https://ai.boraydata.com/conring/gw/health` | 200 | curl |
| 公网提交→轮询 | completed，输出「在的」 | 实测 |

性能结论（同前）：gateway 的价值是去进程冷启动 + run_id/stop/审批/幂等，不是数量级提速；
实测冷启动 run 约 35s、热态 8–14s，与 `run-hermes.sh -z` 同数量级，耗时主要由模型侧决定。
