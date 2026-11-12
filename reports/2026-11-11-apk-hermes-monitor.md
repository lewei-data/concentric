# APK 构建 / 验证 / Hermes 运行监控分析报告

- 报告时间：2026-11-11 22:30（Asia/Shanghai）
- 采集主机：206（192.168.31.206 / 192.168.21.206）
- 数据来源：本机命令实测、`/opt/sefs/hermes/logs/*.log`、`journalctl -u hermes-gateway-8987483c`、gateway `/health/detailed`

---

## 1. APK 构建结果

| 项目 | 值 | 来源 |
|---|---|---|
| 构建命令 | `./gradlew collectApks --no-daemon` | 本次执行 |
| 结果 | BUILD SUCCESSFUL in 10s（77 tasks，3 executed / 74 up-to-date） | Gradle 输出 |
| 版本同步 | `VERSION SYNC OK: 0.11.3 (versionCode 40)` | Gradle `verifyVersionSync` |
| debug 产物 | `/home/boray/opc-workbench/android/dist/voice-mcp-v0.11.3-b40-debug.apk`，498,430 字节 | Gradle 输出 + stat（mtime 22:21:22） |
| release 产物 | `/home/boray/opc-workbench/android/dist/voice-mcp-v0.11.3-b40-release.apk`，449,467 字节 | 同上（mtime 22:21:22） |
| 签名 | Android Debug 自签（`CN=Android Debug`），仅自测可用 | `apksigner verify --print-certs` |
| versionName / versionCode | 0.11.3 / 40 | `aapt2 dump badging` |
| minSdk / targetSdk | 26 / 35 | `aapt2 dump badging` |
| 包名 | com.boray.voicemcp | `aapt2 dump badging` |

说明：本次为增量构建（源码自 20:25 那次构建后无改动），Gradle 判定 74 个 task 为 UP-TO-DATE，但 `collectApks` 重新产出了 dist 目录下的两个 APK（时间戳 22:21:22，与本次构建时间一致）。

---

## 2. APK 验证结果

**已完成（静态验证，全部通过）**

- 签名校验：`apksigner verify` 通过，v1/v2 签名有效。
- Manifest 校验：包名/版本/SDK 等级与版本号同步任务一致。
- 产物完整性：SHA256 已计算（见下表），可用于安装前比对。

| 文件 | SHA256 |
|---|---|
| voice-mcp-v0.11.3-b40-debug.apk | `a9f94c2a8731b41902cac1611560847ef32908db6ac2659f6e16c11c6dba085f` |
| voice-mcp-v0.11.3-b40-release.apk | `e360132611d9f158a1d63395b933b5ebc2bffcdf048bf87766cffe3fdaad9d67` |

**未完成（受环境限制）**

- 本机 `which adb` 无输出、`adb devices` 无设备列表 → **无法完成真机安装与「录音→ASR→执行→TTS」全链路验证**。
- release APK 使用 debug keystore 签名，属自测包，不能用于正式分发。

---

## 3. Gateway 端到端验证（本次现场实测）

| 用例 | 结果 | 证据 |
|---|---|---|
| 公网无 key 提交 | 401 | `POST https://ai.boraydata.com/conring/gw/v1/runs`，curl `%{http_code}` |
| 公网错误 key 查询 | 401 | `GET .../v1/runs/abc` + 伪 key |
| 公网 default profile 提交 | 202 → completed，端到端 **9s**，输出「在的」 | 提交后 3s 间隔轮询，第 3 次轮询 completed |
| 公网 profile 前缀提交 `/p/secretary/v1/runs` | 202 → completed，端到端 **12s**，usage `input 12,858 / output 4 / total 12,862` | 同上 |
| 公网健康检查 | `/conring/gw/health` = 200 | curl `-w '%{http_code} %{time_total}'` |
| 服务存活 | dispatchd、mcp-mail、mcp-asr、mcp-tts、hermes-gateway 均 active | `systemctl is-active` |

结论：统一接入链路（公网 nginx → gateway `/v1/runs` + `/p/<profile>/v1/runs` → 鉴权 → 执行 → 状态轮询）**当前可用**，profile 前缀路由与 default 路由都正常。

---

## 4. Hermes 运行监控

### 4.1 进程与资源

| 指标 | 值 | 来源 |
|---|---|---|
| 服务 | `hermes-gateway-8987483c.service` active，本次启动 22:15:23 | systemctl |
| PID / 运行时长 | 1151092 / 12m10s（采样时点） | ps |
| CPU / 内存 | 21.6% / RSS 543,784 KB（约 531 MB） | ps |
| 主机负载 | load average 1.53 | uptime |
| 主机内存 | 已用 270 GB / 总 318 GB（约 85%），可用约 48 GB | free -g |
| 磁盘 `/opt/sefs` | 使用率 4% | df -h |

### 4.2 今日请求画像

| 指标 | 值 |
|---|---|
| 提交（HTTP 202） | 36 次 |
| 状态轮询（HTTP 200） | 1,402 次 |
| 429 限流 | 0 次 |
| 401 未授权 | 13 次（时间集中在 21:43–22:23，均为配置/鉴权测试与无 key 请求） |
| 404 | 3 次（跨 profile 查询、重启前旧 run） |
| 405 | 1 次（`GET /v1/runs` 列表探测，接口不提供列表） |

profile 请求分布（含轮询，Top）：

| profile | 请求数 |
|---|---|
| vd_wenren | 318 |
| cto | 173 |
| cco | 168 |
| secretary | 144 |
| pm_wenren | 133 |
| pm_gou | 102 |
| release_ops | 99 |
| reviewer | 78 |
| ceo | 4 |
| cw_gou | 2 |

来源：`/opt/sefs/hermes/logs/agent.log` 的 aiohttp access 记录（今日）。

### 4.3 时延

| 指标 | 值 | 口径 |
|---|---|---|
| 今日完成轮次 | 28 轮 | gateway.log `response ready` |
| p50 / p90 / max | **7.5s / 27.1s / 183.5s** | 同上（今日） |
| gateway 自报 p95 | 162.8s（重启后样本仅 8 次，p95 近似等于最大值，参考价值有限） | `/health/detailed` metrics |
| 近 6 天全量 p50 / p90 | 9.5s / 50.5s（n=229） | gateway.log（11-06 至 11-11） |

### 4.4 错误与异常

| 类型 | 数量 | 时段 | 判定 |
|---|---|---|---|
| 上游 429 `RateLimitError`（opencode.ai `deepseek-v4.1-flash`，报 "inference service is overloaded"） | 277 行日志 / 66 个不同分钟 / 约 70 次调用失败 | 今日 06:35–10:20 | 上游容量问题，非本机配置错误；当前时段已无新增 |
| `APITimeoutError` | 57 行（24h 内），今日 19 次调用级 | 全天零散 | 与长任务/上游慢有关 |
| 连接类错误 | 2 次 | 24h | 偶发 |
| cron 任务失败 | 1 次（22:15 重启窗口内 worker 退出码 1） | 22:15 | 与重启相关，非业务逻辑 |
| 工具层报错 | `execute_code` 在 api_server 无人值守平台被 BLOCKED；`terminal` heredoc 被拦截；`mail_read` uid 类型不匹配 | 今日 | 平台工具集限制，见 §5.5 |

### 4.5 在线状态观测

- `active_runs` 在 22:25–22:26 采样中为 3 → 3 → 3 → 2 → 2 → 2，同期 `stored_runs` 10、`requests` 7→8。
- `agent.log` 同时段显示 `/p/pm_gou/...`、`/p/vd_wenren/...` 的状态轮询来自 `Python-urllib/3.12`，即 prototype（工作台）正在跑员工任务。
- **结论：活跃 run 波动来自真实并发调用（prototype 员工任务 + 其他会话），不是明显泄漏；但本轮未做长窗口回归，不能完全排除计数残留。**

---

## 5. 风险与建议

1. **上游 429 是当前最大可用性风险**（今晨 06:35–10:20 爆发，约 70 次调用失败）。建议给 `deepseek-v4.1-flash` 配 provider 级 fallback 或提高重试退避，并在 `dispatchd`/`mcp-asr` 侧对 `GatewayBusy`/`RunFailed` 做降级提示。
2. **尾延迟偏高**（今日 max 183.5s，p90 27.1s）。语音链路必须保留「先回收到、再异步执行」的现有设计；App 端超时不要短于 60s。
3. **release APK 仍用 debug 签名**，只能自测；正式分发需生成独立 keystore。
4. **公网 `/conring/gw/` 仅靠 Bearer key**，nginx 层只有 POST 提交做了 `limit_req`（5r/s，burst 20）。若 key 泄漏，攻击者可直接消耗模型额度；建议后续加源 IP 白名单或按 key 的配额。
5. **api_server 平台的工具限制**：`execute_code`、`terminal` heredoc 在无人值守平台会被拦截。若某类语音指令依赖这些工具，会直接失败——需按业务清单核对，必要时改走 CLI 子进程回退。
6. **主机内存 85%**（可用 48 GB）。gateway 常驻约 531 MB，当前不构成压力，但再叠加多 profile 并发跑长任务时需关注 swap/OOM。
7. **并行会话覆盖风险**：208 的 `conring-mcp.conf` 与 gateway systemd unit 今晚都被其他会话修改过（22:05、22:15 两次重启）。`/conring/gw/` 路由若再被整体覆盖会 401/404，建议每日巡检 `grep -c '/conring/gw/'` 与 `systemctl show -p NRestarts`。
8. **真机未验证**：本机无 adb 设备，App 端「录音→ASR→gateway→TTS」闭环尚未实测。

---

## 6. 结论

- APK：**0.11.3 (b40) 构建成功**，debug/release 双产物齐全，manifest 与签名校验通过，可直接安装自测（release 为 debug 签名）。
- Gateway：**统一接入链路端到端可用**，profile 前缀与鉴权均正确，今日 36 次提交、0 次 429。
- 运行面：服务稳定（重启后 NRestarts=0），今日 p50 7.5s；主要风险是上游 5 小时窗口内的 429 爆发与尾延迟。
- 未完成项：真机安装与语音全链路验证（缺设备）。

---

## 信息指数评分

**整体：8 / 10**

- 构建、签名、版本、公网 E2E、鉴权、请求画像、时延、错误分类均有命令输出或日志原文支撑。
- 扣分项：无设备导致真机验证缺失；`active_runs` 波动归因只做了 1 分钟采样。

**低于 7 分的单项（需标注）**

- 「`active_runs` 1–3 波动的原因」——**6 / 10**：只能确认与 prototype 员工任务并发相关，未做 30 分钟以上窗口验证，不能排除计数残留。
- 「24h 内 429 的准确调用失败次数」——**6 / 10**：277 是日志行数，按每失败约 4 行折算约 70 次，属估算而非精确计数。
