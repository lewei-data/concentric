# 远程 Agent 调用视频作业 API 使用说明

> 适用对象：hermes 员工 agent、Claude Code / Cursor 等编码助手、手机端 mail agent、自建脚本。
> 上线时间：2026-11-23。本文以 `/opt/sefs/opc-workbench/prototype/server.py` 与 `video_jobs.py` 为准。
>
> **定位**：这是工作台的**异步作业 HTTP API**，不是 MCP 工具。现有 MCP（`:9104`）明确不提供 video 工具，
> 远程 agent 要出片就调本文的端点；MCP 只负责身份（同一把个人 key）与事件通知。

---

## 1. 前置条件：先领一把个人 key

| 项 | 值 |
| --- | --- |
| 领取地址 | `https://ai.boraydata.com/workbench/mcp-key`（登录后自助生成，可多把并存） |
| 工作台公网前缀 | `https://ai.boraydata.com/workbench` |
| 本机直连 | `http://127.0.0.1:8848` |
| 鉴权头 | `X-Mail-Agent: <个人 API key>` |

**不要向用户索要手机号或 key 本身**——key 自带身份（手机号），服务端从 key 反查。
同一把 key 也是 MCP 的 `client_key`，不需要第二套凭据。

---

## 2. 30 秒快速开始（curl）

```bash
BASE=https://ai.boraydata.com/workbench
KEY=<你的个人 API key>

# 1) 提交（幂等键务必由 agent 自己生成并保留）
curl -sS -H "X-Mail-Agent: $KEY" -H 'Content-Type: application/json' \
  -d '{"brief":"做一条30秒的产品宣传片，突出狗大人品牌",
       "project":"gou", "idempotencyKey":"job-20261123-001"}' \
  "$BASE/api/video/jobs"
# → {"ok":true,"job_id":"vj_xxxxxxxxxxxx","status":"queued", ...}

# 2) 轮询状态
curl -sS -H "X-Mail-Agent: $KEY" \
  "$BASE/api/video/jobs/vj_xxxxxxxxxxxx"

# 3) 完成后取成片（返回公网签名 URL，10 分钟有效）
curl -sS -H "X-Mail-Agent: $KEY" \
  "$BASE/api/video/jobs/vj_xxxxxxxxxxxx/result"
# → {"ok":true,"videoUrl":"https://ai.boraydata.com/workbench/api/video/jobs/.../media?exp=...&sig=...", ...}
```

提交返回 HTTP 200，但语义是异步入队（真正的成片要等流水线跑完），**不是**同步出片。
**实测整条流水线 20–45 分钟**（2026-11-23 两条样本：21 分钟与 43.5 分钟，均停在内容审核未出片）。
请按此设置 Agent 的超时预算，不要按"几十秒/几分钟"设计。

---

## 3. 端点一览

| 方法 | 路径 | 作用 | 鉴权 |
| --- | --- | --- | --- |
| POST | `/api/video/jobs` | 提交视频作业 | key 或会话 |
| GET | `/api/video/jobs` | 列出自己最近 30 条作业（**仅供参考/取 job_id，进度请看详情端点**） | key 或会话 |
| GET | `/api/video/jobs/<job_id>` | 查状态 / 阶段 / 审批门 / 阻断原因 | key 或会话 |
| POST | `/api/video/jobs/<job_id>/approve` | 老板审批放行/驳回 | **仅老板 key** |
| POST | `/api/video/jobs/<job_id>/cancel` | 取消/关闭作业（活跃则中止，终态则标记关闭） | 本人或老板 key/会话 |
| GET | `/api/video/jobs/<job_id>/result` | 取成片签名 URL + 元信息（未出片时先返回 409；实测不带 key 返回 401，鉴权在前） | key 或会话 |
| GET | `/api/video/jobs/<job_id>/media?exp=&sig=` | 下载成片（支持 Range） | **URL 签名自鉴权**，无需 key |

### 3.1 提交 `POST /api/video/jobs`

```json
{
  "brief": "做一条30秒产品片，轻松幽默，突出狗大人品牌",
  "requirements": "用 f02 女声，字幕避开底部水印区",
  "project": "gou",
  "sourceRefs": ["OPC/素材/品牌KV.png"],
  "idempotencyKey": "agent-run-20261123-001"
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `brief` | 是 | 创意需求原文；空字符串返回 `400` |
| `requirements` | 否 | 老板特定要求，随单流转到 CCO/PM |
| `project` | 否 | `gou` / `wenren` / `tongxinyuan`；不填走默认项目路由 |
| `sourceRefs` | 否 | **仅作留痕备注**，不会自动读取文件；素材仍需在工作区/流水线里补齐。响应里以数组回显 |
| `idempotencyKey` | 否 | 强烈建议填写。同一调用方 + 同一 key 重复提交 → 返回同一 `job_id`，带 `idempotent_replay: true`，不会双排任务 |

成功返回（已剔除 `phone` / `idem` 等内部字段；**整包仍不要转发给第三方**）：

```json
{"ok": true, "job_id": "vj_a1b2c3d4e5f6", "status": "queued", "trace_id": "tr_...",
 "stage": "assessment", "gate": null, "source_refs": ["OPC/素材/品牌KV.png"],
 "item_id": "c_...", "brief": "...", "requirements": "...", "project": "gou",
 "output_path": "", "error": "", "created_at": "...", "updated_at": "...",
 "idempotent_replay": false}
```

### 3.2 状态 `GET /api/video/jobs/<job_id>`

```json
{"ok": true, "job_id": "vj_...", "status": "running", "runStatus": "blocked",
 "stage": "review", "gate": "blocked", "gateReason": "内容审核未全部通过（cost-gate）",
 "approvalId": null, "traceId": "tr_...", "brief": "..."}
```

| 字段 | 含义 |
| --- | --- |
| `status` | `queued` / `running` / **`blocked`** / `aborted` / `error` / `rejected` / `timeout` / `interrupted` / `completed` / `failed` / **`canceled`** |
| `runStatus` | 过程库 `pipeline_runs` 原始状态；目前与 `status` 始终同值，**以 `status` 为准即可** |
| `stage` | 当前流水线阶段（16 段，见 `config/pipeline.yaml`） |
| `gate` | `awaiting_human` = 等老板审批；`blocked` = 被材料/成本/审核门挡住；无门时为假值（`null` 或空串）——**用 `if (gate)` 判断，不要比较 `=== null`** |
| `gateReason` | 在**详情/列表**中为常驻字段：无阻断时为 `null`（不是缺省），阻断时给原因，如 `内容审核未全部通过（sample-gate）`；提交响应里不返回此字段。括号内是**具体门名变量**（已见 `render-gate` / `sample-gate` / `material-gate` / `cost-gate`），随阶段变化——**只用于展示，不要解析或据此分支** |
| `approvalId` | 待审批单号，审批时要用；`blocked` 时通常为 `null` |

**终态 vs 可续跑**（决定"上报"还是"继续等"）：

| 类别 | 取值 | Agent 动作 |
| --- | --- | --- |
| 成功终态 | `completed` | 调 `/result` 取链接 |
| 阻断终态 | `blocked` / `aborted` / `error` / `rejected` / `timeout` / `failed` | **停止轮询并上报**，带 `gateReason` / `error`；不要重提。需要清理时调 `/cancel` |
| 已取消 | `canceled` | 停止轮询，无需上报；`gateReason` 为取消原因 |
| 可续跑 | `interrupted` | 继续轮询；工作台会从断点恢复 |
| 中间态 | `queued` / `running` | 继续轮询 |

注意：跑到 `blocked` 是**正常终态**（内容审核未通过），不代表系统卡死；`blocked` 没有 `approvalId`，`/approve` 无法放行，只能在工作台界面驳回或重提。

**列表与详情已对齐**：两者都经过同一状态同步，`updated_at` 随真实状态变化更新。
但仍建议**只看详情端点**判断进度。

### 3.3 审批 `POST /api/video/jobs/<job_id>/approve`

```json
{"decision": "approve"}
```

- **只有老板手机号对应的 key / 会话可调用**，其他身份返回 `403`。
- 没有待审批项时返回 `400`（"该作业当前没有待审批项"）。
- agent **不得自行伪造审批**：看到 `gate=awaiting_human` 应通知老板，由老板本人调用，或在工作台审批界面处理。
- 审批放行后从断点续跑，不会从头重来。

### 3.4 取消 `POST /api/video/jobs/<job_id>/cancel`

```json
{"reason": "测试作业已完成核对，关闭"}
```

- 权限：作业创建者本人或老板；其他身份 `403`。
- 活跃作业（`queued` / `running`）：立即标记 `canceled` 释放流水线占用，正在执行的阶段不抢占，
  在**下一个阶段边界**收口，不会启动后续阶段。
- 已终态作业（`blocked` 等）：直接标记 `canceled` 关闭，便于清理卡住的作业；返回 `wasRunning: false`。
- 取消后详情的 `status=canceled`、`gate=null`、`gateReason` 为取消原因；列表同步。
- `/approve` 对 `blocked` 无单号可用时，用本端点关闭即可，不必去工作台界面。

返回：`{"ok":true,"jobId":"...","status":"canceled","traceId":"...","wasRunning":false,"reason":"..."}`

### 3.5 取回 `GET /api/video/jobs/<job_id>/result`

```json
{"ok": true, "jobId": "vj_...", "videoUrl": "https://.../media?exp=...&sig=...",
 "expiresIn": 600, "bytes": 19820331, "mime": "video/mp4"}
```

- `videoUrl` **自鉴权、短时效（默认 600 秒）**，可直接给 `<video src>` 或下载，不带个人 key。
- 链接过期后重新调 `/result` 换新链接，不要缓存长期地址。
- 成片还没生成时返回 `409`（"成片尚未生成"）。

---

## 4. 状态机与 agents 应当遵循的轮询策略

```
提交 → queued → running → [awaiting_human] → running → completed
                      ├→ blocked（材料/成本/审核门阻断，无 approvalId）
                      ├→ aborted / error / rejected / timeout / failed
                      └→ canceled（本人/老板调用 /cancel；阶段边界收口）
```

**四个停止条件**（命中任一个就停轮询、上报，不要空转）：

1. `gate=awaiting_human` → 通知老板，等人工审批；
2. `gate=blocked` 或 `status ∈ {blocked, aborted, error, rejected, timeout, failed}` → 记录 `gateReason` / `error` 上报；
3. `status=completed` → 调 `/result` 取链接；
4. **总时长超过 45–60 分钟**仍未终态 → 报告人工（实测完整流水线 21–43.5 分钟；最长单段约 16 分钟，仅靠"单阶段 20 分钟"兜不住）。
   辅助条件：单阶段超过 20 分钟无 `stage` 变化也可上报，但它不是主要兜底。

**推荐轮询节奏**（避免把工作台打爆）：

1. 提交后先睡 `5s`；
2. 之后每 `10–15s` 查一次状态，**只看 `GET /api/video/jobs/<job_id>` 详情端点**；
3. 拿 `gate` 请用真假值判断（`if (gate)`），因为提交响应里是 `null`、历史记录里可能是空串；
4. 不要盲目重提：先读 `gateReason` / `error` 判断是材料问题还是成本问题。

**并发限制**：工作台同一时刻只跑一条流水线（`om_render` 有全局 `.render.lock`）。
若提交返回：

```json
{"ok": false, "error": "流水线忙",
 "detail": "上一轮选题会仍在进行（tr_xxx），请等它结束或先处理待审批",
 "retry_after_sec": 30}
```

- `detail` 是判断"该等还是该催人"的唯一线索（点名占用者的 trace，并提示是否需处理待审批），**务必原文带给老板**。
- 按 `retry_after_sec` 退避重试，**带上同一个 `idempotencyKey`**。
- **退避要有上限**：建议最多 10 次 / 10 分钟；仍忙则停止并上报，不要无限循环。实测曾连续退避 24 次、等 18 分钟才拿到提交机会，已超出手机端 agent 的超时预算。

---

## 5. hermes 接入示例

### 5.1 直接让 agent 用 shell / curl

在 hermes 的 prompt 或技能里写清楚端点、key 来源和轮询规则，例如：

```text
用户要一条产品宣传片时：
1. 读取环境变量 OPC_API_KEY（不要向用户索要）。
2. 用 curl POST https://ai.boraydata.com/workbench/api/video/jobs，
   带 X-Mail-Agent: $OPC_API_KEY，body 含 brief 与 idempotencyKey。
3. 记录返回的 job_id，每 15 秒 GET 详情端点状态。
4. 若 gate=awaiting_human 或 gate=blocked，停止并上报原因，不得自行 approve。
5. 完成后 GET /result，把 videoUrl 交给用户（10 分钟内有效）。
6. 若作业卡在 blocked 且无 approvalId，可 POST /cancel（带 reason）关闭它。
```

注意：hermes profile 需具备 shell/curl 权限（按区授权口径）。若该员工 profile 禁用了 shell，
改走 5.2 的 Python 脚本方式或由小秘书代为提交。

### 5.2 Python 脚本（任何能执行 Python 的 agent 都可用）

```python
import json, os, time, urllib.request

BASE = "https://ai.boraydata.com/workbench"
KEY = os.environ["OPC_API_KEY"]

def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method="POST" if body is not None else "GET",
        headers={"X-Mail-Agent": KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

r = call("/api/video/jobs", {"brief": "做一条30秒产品片", "idempotencyKey": "hermes-001"})
job = r["job_id"]
BLOCKING = {"blocked", "aborted", "error", "rejected", "timeout", "failed"}
deadline = time.time() + 60 * 60          # 总时长上限 60 分钟
while time.time() < deadline:
    st = call("/api/video/jobs/" + job)
    gate = st.get("gate")
    if gate == "awaiting_human":
        print("需要老板审批：", st.get("approvalId")); break
    if gate == "blocked" or st.get("status") in BLOCKING:
        print("已阻断：", st.get("gateReason") or st.get("error")); break
    if st.get("status") == "completed":
        break
    time.sleep(15)
else:
    print("超过 60 分钟未终态，请人工介入")
if st.get("status") == "completed":
    print(call("/api/video/jobs/" + job + "/result")["videoUrl"])
if st.get("status") == "completed":
    print(call("/api/video/jobs/" + job + "/result")["videoUrl"])
```

### 5.3 与 MCP 的关系

- **身份同源**：本文的 `X-Mail-Agent` 就是 MCP 的 `client_key`，一把 key 走天下。
- **能力互补**：MCP 负责问答、检索、TTS/图片生成、员工对话；**视频成片走本文 HTTP API**。
- **事件通知**：MCP 桥的 `GET /mcp/events`（SSE）会推送 `notifications/opc/job` 终态事件；
  但你自己的作业状态以本文 `GET /api/video/jobs/<id>` 为准。
- 若客户端只认 MCP 工具，目前需要一层薄转发（尚未实现），不要误以为 `tts`/`image` 能出视频。

---

## 6. 错误码

| HTTP | 返回体 `error` | 含义 / 处理 |
| --- | --- | --- |
| 400 | `brief 不能为空` / `请求体不是合法 JSON` / `该作业当前没有待审批项` | 修正参数，不要重试同一请求 |
| 401 | `需要登录或 MCP 个人 key` | key 缺失/无效/已撤销；引导用户重新领取 |
| 401 | `{"error":"unauthorized","login":"..."}`（无 `ok` 字段） | **未知路径**也会返回 401；别误判成 key 失效，先核对 URL 拼写 |
| 403 | `仅老板可审批远程视频作业` / `作业不属于当前用户` | 权限边界，停止 |
| 403 | `链接无效或已过期` | 签名 URL 过期，重调 `/result` |
| 404 | `作业不存在` / `成片不存在或尚未生成` | 核对 `job_id` |
| 409 | `流水线忙` + `retry_after_sec` + `detail` | 带 `detail` 退避重试（上限 10 次/10 分钟），同一幂等键；仍忙则上报原文 |
| 409 | `成片尚未生成` | 继续轮询状态 |
| 403 | `作业不属于当前用户` | `/cancel` 仅本人或老板可调用 |
| 404 | `not found`（英文小写） | 方法/路径不匹配，核对 HTTP 方法 |
| 500 | `TypeError: ...` 等 | 服务端异常，记录原文并报告，不要盲目重试 |
| 501 | Python `http.server` 默认 HTML 页 | 方法不支持（如 `PUT`）；**不是 JSON**，客户端需对所有非 2xx 做兜底 |

---

## 7. 红线与排障

**红线**

1. 不要把 `videoUrl` 当作长期地址转发给无关方——它是 10 分钟有效的 bearer 链接。
2. 不要把个人 key 写进日志、prompt 回显或前端代码；用环境变量传入。
3. 不要绕过审批门：远程 agent 没有权力替老板 `approve`。
4. 不要指望 `sourceRefs` 自动上传素材——它只是备注；素材缺失会导致流水线停在材料门。

**排障**

- 公网 401 但本机 200：确认请求带的是 `X-Mail-Agent` 而不是 `Authorization: Bearer` 以外的错误头；
  两者都支持，`?key=` 也可以。
- 根路径 `/api/*` **亦可达**（实测与 `/workbench/api/*` 返回一致），但请统一用 `/workbench` 前缀，便于将来收口；不要把它当作 404 的排障依据。
- `gate` 是假值时既可能是 `null` 也可能是空串，请用 `if (gate)` 判断。
- 状态一直 `queued`：先看是否已有别的流水线在跑（工作台唯一流水线），按 409 的退避规则处理；详情端点会把真实状态同步回列表。
- 想确认服务端健康：`curl -sS https://ai.boraydata.com/workbench/api/health`。

---

## 8. 变更记录

- 2026-11-23：初版。提交/状态/审批/结果/签名下载五个端点上线；`selfcheck` 10 PASS/0 WARN。
- 2026-11-23（核对后修订）：补 `blocked` 等阻断态与 `gateReason`；列表与详情状态同步；`gate` 统一假值；`source_refs` 回数组；响应剔除 `phone`/`idem`；修正根路径排障口径；非法 JSON 单独报错；错误码补 401（未知路径）/404/501。
- 2026-11-23（v2 复核后）：补 `409.detail` 与退避上限；实测时长改为 20–45 分钟并加 45–60 分钟总上限；补终态/可续跑表；`gateReason` 注明常驻且门名不可解析；`runStatus` 与 `status` 同值口径。
- 2026-11-23（取消端点）：新增 `POST /api/video/jobs/<job_id>/cancel`（本人/老板），协作式阶段边界收口；`status=canceled` 入枚举与终态表。
