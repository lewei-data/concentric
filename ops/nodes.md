# CTO 节点台账

> 由 `opsctl` 自动生成于 2026-11-13 16:46:55；权威数据在 `nodes.db`，本文件每次巡检后重写。

## 节点

| 节点 | 主机 | 角色 | SSH 别名 | 备注 |
|---|---|---|---|---|
| comfy-21-157 | 192.168.21.157 | ComfyUI 出图与视频 | comfyui-21-157 | SSH 用户 rpdsql，密钥 opc_comfy_ed25519 |
| gw-21-208 | 192.168.21.208 | nginx 反向代理 / TLS | - | ai.boraydata.com 入口 |
| sup206 | 192.168.21.206 | 应用/媒体/邮件/TTS/ASR | - | 亦为 192.168.31.206（办公网直连） |

## 服务与最近状态

| 节点 | 服务 | 端口 | 协议 | 级别 | 最近状态 | 耗时 | 检查时间 | 说明 |
|---|---|---|---|---|---|---|---|---|
| comfy-21-157 | SSH | 22 | ssh | high | 正常 | 6429ms | 11-13 16:46 | localhost.localdomain /  16:46:46 up 131 days |
| comfy-21-157 | 模型资产 | - | files | high | 正常 | 8023ms | 11-13 16:46 | 权重 7/7 正常 |
| comfy-21-157 | ComfyUI 出图 | 8188 | http | high | 正常 | 20ms | 11-13 16:46 | HTTP 200 |
| comfy-21-157 | ComfyUI 视频 | 8189 | http | high | 正常 | 6ms | 11-13 16:46 | HTTP 200 |
| comfy-21-157 | 出图能力 | - | nodes | high | 正常 | 592ms | 11-13 16:46 | 节点 8/8 就绪 |
| comfy-21-157 | 视频音频能力 | - | nodes | high | 正常 | 346ms | 11-13 16:46 | 节点 8/8 就绪 |
| gw-21-208 | HTTP | 80 | tcp | mid | 正常 | 0ms | 11-13 16:46 | TCP 80 可连 |
| gw-21-208 | HTTPS | 443 | tcp | high | 正常 | 0ms | 11-13 16:46 | TCP 443 可连 |
| gw-21-208 | ai.boraydata.com | 443 | http | high | 正常 | 56ms | 11-13 16:46 | HTTP 200 |
| sup206 | 邮件 MCP | 9100 | mcp | high | 正常 | 6ms | 11-13 16:46 | MCP initialize HTTP 200 |
| sup206 | TTS MCP | 9101 | mcp | high | 正常 | 6ms | 11-13 16:46 | MCP initialize HTTP 200 |
| sup206 | ASR MCP | 9102 | mcp | mid | 正常 | 9ms | 11-13 16:46 | MCP initialize HTTP 200 |
| sup206 | 媒体引擎 | 9200 | http | high | 正常 | 15ms | 11-13 16:46 | HTTP 200 |
| sup206 | 调度服务 | 9120 | http | mid | 正常 | 3ms | 11-13 16:46 | HTTP 200 |
| sup206 | 调度 WS | 9121 | http | low | 正常 | 3ms | 11-13 16:46 | HTTP 426 |
| sup206 | 媒体生成 MCP | 9103 | http | mid | 正常 | 5ms | 11-13 16:46 | HTTP 200 |

## 最近一次巡检

- 时间：2026-11-13 16:15:39
- 结果：2026-11-13 16:15 巡检正常：16/16 项服务在线
- 报告：（无异常，未生成详细报告）

## 维护约定

- 新增机器：`opsctl add-node`；新增端口/服务：`opsctl add-svc`。
- 每次增删都会同时更新 `nodes.db` 与本文件，保证 MD 与 SQLite 一致。
- 巡检每 30 分钟由 cron 触发；异常时另出详细报告并通知老板。
