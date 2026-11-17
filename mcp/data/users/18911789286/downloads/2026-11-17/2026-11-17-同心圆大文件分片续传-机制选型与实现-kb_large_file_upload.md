---
kb_id: kb_large_file_upload
title: 同心圆大文件分片续传 — 机制选型、协议、实现与实测
project: 全公司
tags: 大文件、分片上传、断点续传、MCP、HTTP、zip、PDF、LibreOffice、进度查询
trace: mcp/opc_mcp/upload.py、mcp/opc_mcp/upload_client.py、mcp/opc_mcp/server.py
archived: 2026-11-17T16:50:00+08:00
---

结论：同心圆现在可以直接收 zip / pdf / doc / xls / ppt 等大文件，单文件上限 2GB，8MB 分片，**断电、断网、进程被 kill 都能从断点续传**；传完自动解析、入库、排队分析。走独立 HTTP 服务（9105），不经过 MCP 的 JSON-RPC 消息体，因此不受 base64 与单消息大小限制。

## 一、机制选型（为什么不用现成方案）

| 机制 | 上限 | 断点续传 | 结论 |
|---|---|---|---|
| base64 内联 JSON-RPC | 受单消息限制，本系统设 100MB | 不支持，失败全量重来 | 只保留给小文件 |
| multipart 一次性 POST | 受 nginx / 内存限制 | 不支持 | 不用 |
| TUS 协议 | 支持 | 支持 | 需引入额外依赖与中间件，本场景只有 1 个服务、1 种客户端，收益低 |
| S3 分片上传 | 支持 | 支持 | 无对象存储，且要引入签名体系 |
| **自定义 HTTP 分片续传** | 2GB（配置项） | 支持（offset 语义） | **采用**：纯标准库、行为可读、故障语义清晰 |
| rsync / scp | 不限 | 支持（块级） | 需 SSH 通道，外部 agent 用不了 |
| 网盘挂载 / SEFS 直读 | 不限 | 依赖底层 | 只解决内网，不解决公网与鉴权 |

## 二、协议

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/v1/uploads` | 建单 `{filename,size}` → 201 `{upload_id,offset,size,put_url,progress_url,complete_url}` |
| PUT | `/v1/uploads/{id}?offset=N` | 追加原始字节；`Content-Length` 必填，可带 `Content-Range: bytes s-e/total` |
| GET | `/v1/uploads/{id}` | 详情（含全部进度字段与端点） |
| GET | `/v1/uploads/{id}/progress` | 进度：percent / remaining / 速率 / ETA / status |
| HEAD | `/v1/uploads/{id}` | 只要进度头（`X-Upload-Offset/Size/Status/Percent`），开销最小 |
| POST | `/v1/uploads/{id}/complete` | 校验大小 → 流式 sha256（可比对 `X-Content-SHA256`）→ 去重 → 入库 → 排队分析 |
| DELETE | `/v1/uploads/{id}` | 取消并删临时文件 |
| GET | `/v1/uploads` | 本人上传单列表（含每单进度、`count`、`active`） |
| GET | `/healthz` | 探活：`{ok,service,max_mb,chunk_hint,uploads}` |

鉴权：`Authorization: Bearer <session_token>`（token 来自 `opc_init`），也支持 `?token=`；跨用户访问返回 403。

### 错误码语义（客户端必须按此分支）

| 码 | 含义 | 客户端动作 |
|---|---|---|
| 409 | offset 与当前偏移不符，body 附 `offset=当前值` | 按返回的 offset 跳到下一片继续（幂等） |
| 410 | 临时 `.part` 丢失（过期清理/换机） | 重新建单，从头传 |
| 400 | size 非法 / 偏移越界 / sha256 不符 | 不重试，修参数或重传 |
| 401 | token 无效或过期 | 重新 `opc_init` |
| 403 | 别人的上传单 | 换单 |
| 413 | 超出声明大小 | 不重试 |
| 404 | 上传单不存在 | 重建单 |

### 续传状态机

```
建单(offset=0) ──PUT 分片成功──▶ uploading(offset 前进) ──offset==size──▶ complete ──▶ done
      ▲                              │  断网/超时/kill
      │                              ▼
      └──── 410（临时文件丢）──── 重跑命令：GET progress 拿 offset，原地续传
```

## 三、服务端实现（`mcp/opc_mcp/upload.py`，约 400 行）

- **纯标准库**：`ThreadingHTTPServer` + `BaseHTTPRequestHandler`（HTTP/1.1、显式 Content-Length、`Connection` 处理），`sqlite3`（WAL）、`hashlib`、`secrets`、`shutil`。
- **建单**：`up_<16hex>`，在 `data/uploads/` 建空 `.part`，写 `uploads` 表（phone/filename/size/offset/sha256/path/status/material_id）。
- **追加**：按 upload 粒度加锁 → 校验 `offset == 当前偏移`（不符 409）→ 检查 `.part` 存在（不存在 410）→ 循环读 body 写入并逐片 `fsync` → 更新 offset。**body 没读满（客户端断开）时按实际写入字节推进 offset**，因此断开也丢不了数据。
- **完成**：校验 `offset == size` → 流式算 sha256（与 `X-Content-SHA256` 比对）→ 按 sha 去重（命中则 `duplicate:true`，不重复入库）→ `os.replace` 到 `users/<phone>/materials/<date>/` → `add_material` → `runner.submit(analyze)`。
- **隔离**：`jobs.owner` 列区分 MCP 进程（`mcp`）与上传进程（`upload`），`recover_stale_jobs(owner)` 只恢复自己的作业，两个进程共写同一个 `opc.db` 不会互相误杀。
- **清理**：`uploading` 且 `updated_at` 超过 7 天（`OPC_MCP_UPLOAD_TTL_SEC`）的单标记 `expired` 并删文件。

### 部署

| 组件 | 位置 |
|---|---|
| 服务进程 | `systemd: mcp-opc-upload.service`，`/opt/sefs/opc-workbench/mcp/run-upload.sh --host 0.0.0.0 --port 9105`，`Restart=always` |
| 公网入口 | 208 nginx `/conring/upload/` → `192.168.21.206:9105`，`client_max_body_size 0`、`proxy_request_buffering off`、300s+ 超时 |
| 内网直连 | `http://192.168.21.206:9105` |
| 数据 | `mcp/data/uploads/*.part`、`mcp/data/opc.db` |

## 四、进度查询（本次新增）

`GET /v1/uploads/{id}/progress` 返回：`percent`（2 位小数）、`uploaded`/`remaining`、`speed_bps`/`speed_human`、`speed_source`（`instant`|`average`）、`eta_sec`、`elapsed_sec`、`status`、`done`、`started_at`/`updated_at`、`sha256`、`material_id`、三个端点 URL。

- 瞬时速率：进程内记录每次写入的起点 `(ts, offset)`，`(新offset-旧offset)/Δt`；服务重启后退化为「总字节 ÷ 已耗时」并标 `average`。
- `done` 与 `status` 区分：传满但未 complete 时 `status=uploading`、`done=true`。
- 同时 `PUT` 响应带 `percent`、`HEAD` 带 `X-Upload-Percent`、列表每项带进度。

## 五、客户端实现（`mcp/opc_mcp/upload_client.py`）

```
python3 -m opc_mcp.upload_client 资料.zip            # 同一命令重跑 = 续传
  --base（默认公网 /conring/upload，内网可指 9105）
  --chunk-mb 8  --retries 5  --timeout 600  --upload-id up_xxx  --no-state  --quiet
```

算法：本地 stat 取大小 → 读状态文件拿 `upload_id`（key = 路径+大小+mtime+服务地址）→ `GET /progress` 拿**服务端真实 offset**（不信任本地记录）→ 循环 `seek(offset)` 读分片、`PUT ?offset=N` → 完成后本地算 sha256、`POST /complete`。

重试矩阵：超时/5xx 退避重试，**重试前先查进度**，已写入的字节直接跳过；409 按返回 offset 跳过（响应丢失但服务端已收到）；401/400/413 立即失败；410 提示重建单。返回码：0 成功 / 2 不可重试 / 3 已中断可续传。任何异常（含 `SIGKILL`）都不丢 offset。

## 六、格式支持矩阵（传完自动解析入库）

| 格式 | 解析方式 |
|---|---|
| `.md` `.txt` `.csv` | 直接读 |
| `.docx` `.pptx` `.xlsx` | 原生 zip+XML 解析（快，无外部依赖） |
| `.pdf` | `pdftotext`，失败回退 LibreOffice |
| `.doc` `.xls` `.ppt` `.rtf` `.odt` `.ods` `.odp` | LibreOffice headless（独立 UserInstallation 防 profile 锁；xls/ods 走 csv 过滤器，ppt/odp 先转 pdf 再抽取） |
| `.zip` | 安全解压后逐条解析：过滤绝对路径与 `..`（zip-slip）、跳过 symlink、≤200 条目、总解压 ≤300MB、嵌套 ≤1 层 |
| 图片/音视频等 | 归档但跳过解析，给出原因 |

## 七、实测（2026-11-17）

| 项 | 结果 |
|---|---|
| 小文件 E2E（1.43MB zip，内含 doc/pdf/xlsx/md） | 13 个 MCP 工具；分片→HEAD→续传→409→complete→job `succeeded`→kb_search 命中→重复 complete 幂等，PASS |
| 128MB / 4MB 分片 / 3 次模拟断点 | 全部续传成功，sha256 一致，103.7s（公网） |
| 24MB 中途接管（先传 8MB 再 `--upload-id`） | 从 33.33% 续传，4 片补齐，job `succeeded`，sha256 一致 |
| 12MB 公网**真实 kill -9**（停在 58.33%） | 重跑同一命令续传至完成，sha256 一致；再跑提示「已完成」不上传 |
| 吞吐 | 本机 `127.0.0.1:9105` **518 MB/s**；公网 **2.54 MB/s**（206 上行带宽限制，非服务瓶颈） |
| 抽取 | doc 69 字 / pdf 69 字 / xlsx 53 字（含 sheet 名与 TSV）/ zip 349 字（4 段） |
| 冒烟 | `mcp/tests/test_smoke.py` 新增 4 项 upload_begin 断言，28/28 通过 |

## 八、运维与排查

- 探活：`curl https://ai.boraydata.com/conring/upload/healthz` → `{"ok":true,"max_mb":2048.0,...}`
- 日志：`journalctl -u mcp-opc-upload -f`
- 409 反复出现：客户端 offset 用的是旧值 → 改为每轮以 `GET /progress` 为准
- 410：临时文件被 TTL 清理或跨机 → 重新建单
- 502/504：查 208 nginx 与 9105 进程；上传路由必须 `proxy_request_buffering off`，否则大 body 会被 nginx 缓冲放大内存
- 磁盘：`.part` 在 `mcp/data/uploads`，异常中断的单会占用空间，7 天后自动清

## 九、已知边界（未验证 / 限制）

1. **2GB 单文件上限**（`OPC_MCP_UPLOAD_MAX_MB`），更大文件需调大配置并确认磁盘与抽取耗时。
2. 多个客户端并发抢同一个 `upload_id` 未实测：按 offset 串行推进，后到者会持续收到 409 并跟随，理论上幂等，但未做验证。
3. 公网续传时速率曾从 21MB/s 掉到 250KB/s（第 8 片起），未抓包定位，判断为链路侧限速/波动。
4. 断点粒度是**分片级字节偏移**（非固定块），客户端若读到不同文件内容但 size 相同，服务端只校验最终 sha256。
5. `.zip` 内单文件超大或嵌套 zip 超过 1 层会被跳过，属有意限制（防压缩炸弹）。

## 关联

- 服务端：`mcp/opc_mcp/upload.py`；客户端：`mcp/opc_mcp/upload_client.py`；MCP 工具：`mcp/opc_mcp/server.py`（`upload_begin`，工具总数 13）
- 抽取：`mcp/opc_mcp/knowledge.py`（`extract_text` 分支）
- 网关路由：`.trae/skills/sefs-nginx-gateway/SKILL.md`（`/conring/upload/` → 206:9105）
- 相关条目：`kb_tongxinyuan_arch_review`（MCP 收口路线 W3-4 提到的 9104/9105 已被本方案与 MCP HTTP 桥占用）
