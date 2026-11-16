# 端口与常驻进程总表（sup206）

> 生成方式：`sudo -n ss -tlnp` 全量对账 + `/proc/<pid>/cwd` 认领 + `ps -o lstart` 取启动时间。
> 最后核对：2026-11-16（第二轮对账 42 个监听端口，修正 443 误登记、补录 37603）。刷新命令见文末。
> 三套台账的分工：本表管**端口与进程认领**；`ops/nodes.db` 管**探活**；`prototype/opc.db#it_assets` 管**资产事实**（19 列）。

## 准入规则（2026-11-16 起）

1. **新服务上线前必须先在本表登记一行**（端口、进程、cwd、systemd 单元名、归属、用途），再改 `ops/nodes.db` 加探活项。没登记的服务不视为公司资产，出事不修。
2. **禁止裸进程常驻**。凡是要活过当天的，必须写成 systemd 单元；临时调试进程只许监听 `127.0.0.1`，且**当天必须杀掉**，杀完在本表「清理记录」留痕。
3. **冒烟测试必须用隔离库 + 随机端口**（`OPC_USERS_DB=/tmp/xxx`），不许连 `/mnt/sefs/conring/users/*/opc.db`。历史上正是这条没执行，留下 5 个跑了两天的僵尸实例。
4. 端口落在下表「已占用」区间内且本表无记录的，巡检视为**端口漂移**（待补检查项）。

## A. 公司业务服务（已在 it_assets + 巡检）

| 端口 | 进程 | 单元/来源 | 用途 | 对外 |
|---|---|---|---|---|
| 8848 | python3 prototype/server.py | `opc-workbench.service` | 工作台 Web/API | 0.0.0.0（反代 443） |
| 8642 | hermes | `hermes-gateway-*` | LLM 网关 | 待核 |
| 9100 | python | `mcp-mail` | 邮件 MCP | 127.0.0.1 |
| 9101 | python | TTS MCP | 语音合成 MCP | 127.0.0.1 |
| 9102 | python | ASR MCP | 语音识别 MCP | 127.0.0.1 |
| 9103 | python | 媒体生成 | 图/视频生成 | 127.0.0.1 |
| 9120 / 9121 | python3 | dispatchd | 调度（APP 指令） | 127.0.0.1 |
| 9200 | python | 媒体引擎 | 媒体任务引擎 | 127.0.0.1 |
| 9311 | python | qwen-tts | **TTS 后端**（health 返回 engine=tts） | 127.0.0.1 |
| 9312 | python | qwen-asr | **ASR 后端**（health 返回 engine=asr） | 127.0.0.1 |
| 143 / 993 | dovecot | `dovecot.service` | IMAP / IMAPS | 是（邮件客户端） |
| 110 / 995 | dovecot | `dovecot.service` | POP3 / POP3S（历史遗留，可关） | 是 |
| 25 / 587 | master | `postfix.service` | SMTP / 提交 | 25 对内，587 对外 |
| 22 | sshd | `ssh.service` | SSH | 是 |
| 80 | nginx | `nginx.service` | 206 自有站点/CDN 探测 | 0.0.0.0 |
| ~~443~~ | — | — | **本机无 443 监听**（2026-11-16 实测 `ss -tln` 计数为 0）。HTTPS 由 208 终端反代终结，**别在本机找 443** | 否 |

## B. 平台底座（非公司业务，但占端口）

| 端口 | 进程 | 说明 | 备注 |
|---|---|---|---|
| 445 | smbd | Samba 文件共享 | 给 Windows 侧挂 `/mnt/sefs` |
| 8384 / 22000 | syncthing | 同步服务 UI / 数据传输 | 6700 条 MD 靠它跨机同步 |
| 5037 | adb | Android 调试桥 | 出包/真机调试用，非业务 |
| 53 | systemd-resolve | 本地 DNS | 系统组件 |
| 53/13301 | — | 见 D 段 | — |

## C. 有 systemd 单元但未登 it_assets

> 2026-11-16 已聚合登记为 `it_assets#platform-services`（21 行），单服务细节看本节。

| 端口 | 进程 | 单元 | 用途 | 风险 |
|---|---|---|---|---|
| 4750 | uvicorn backlot.server:app | `backlot-board.service` | OpenMontage 分镜看板 | **`--host 0.0.0.0` 对外监听**，无鉴权 |
| 9119 | hermes（MainPID 2149775，11-09 起） | `hermes-dashboard.service` | hermes 管理面板 | **`--host 0.0.0.0`**，有 /login；**另有孤儿子进程占 37603**（见 G 段） |
| 9999 | rapids-manager（root，96 天） | `rpdsql-ops.service` | SEFS/RapidDB 集群管理 | root 常驻，管理面 |

## D. SEFS / RapidDB 集群组件（存储底座，台账只写了 13301）

| 端口 | 进程 | 角色 |
|---|---|---|
| 13300 | sefs-manager | 集群管理 |
| 13301 | sefs-manager | 元数据（台账已登记） |
| 13303 | sefs-meta/Main | 元数据服务 |
| 13305 | sefs-storage/Ma.. | 存储节点 |
| 13307 | rpdsqld | RapidDB 查询守护 |

## E. 开发工具（IDE 侧，非公司资产，不纳入巡检）

| 端口 | 进程 | 说明 |
|---|---|---|
| 51001 / 51002 | ckg_server | Trae 代码索引（32 天） |
| 36391 / 38327 | code-* | VS Code Server |
| 32971 / 33049 | node | IDE 临时监听，**随编辑器启停**（2026-11-16 实测仍常驻，勿当业务） |

## F. 代理（个人工具链，待定性）

| 端口 | 进程 | cwd | 说明 |
|---|---|---|---|
| 18787 | node proxy.mjs | `~/.opencode-go-proxy` | 模型代理（出口） |
| 18788 | node forward.mjs | `~/.opencode-go-proxy` | 转发 |

## G. 遗留僵尸（2026-11-16 清理，见清理记录）

| 端口 | pid | 启动 | cwd | 连的库 | 处置 |
|---|---|---|---|---|---|
| 8852 | 1617166 | 11-14 04:58 | `prototype/` | `/tmp/opc-split-smoke*` | 已杀 |
| 8853 | 1626677 | 11-14 05:08 | `prototype/` | 同上 | 已杀 |
| 8854 | 1636058 | 11-14 05:18 | `prototype/` | 同上 | 已杀 |
| 8855 | 1736164 | 11-14 06:54 | `prototype/` | `/tmp/opc-sec*` | 已杀 |
| 8899 | 151796 | 11-13 08:10 | `prototype/` | — | 已杀 |
| 37603 | 1015221 | 11-13 20:07 | `/opt/sefs/hermes` | — | **未清理，待 owner 确认** |

判定依据（三条同时成立才敢动手）：① 监听 `127.0.0.1` 非 `0.0.0.0`；② `/proc/<pid>/fd` 指向 `/tmp/opc-*` 隔离库，**不连生产库**；③ `ss -tn` 活动连接 0 条。

### 37603 详情（2026-11-16 发现，未处置）

- 进程：hermes 启动器 + `dashboard`，页面 `<title>Hermes Agent - Dashboard</title>`，返回 200。
- `HERMES_SUPERVISED_CHILD=1`，但 `ppid=1`、**不在 `hermes-dashboard.service` 的 CGroup 内**（该单元 CGroup 只有 MainPID 2149775）。
- 出生 11-13 20:07:17，比 MainPID（11-09 05:01:14）晚 4 天 → 运行期被拉起后脱管的子进程，至今 2 天 22 小时。
- 三条孤儿证据：0 活动连接、`/login` 页面不含 `37603`、MainPID 无连接指向它。
- **未杀的理由**：它是 hermes 自有组件而非冒烟残留，杀它等于动 hermes 运行时；且它占着 37603，若 dashboard 下次要起内层 UI 会绑定失败——两种情况都得 owner 定。
- 处置建议：`sudo -n kill 1015221` 后观察 9119 与 hermes 各单元是否正常；若 dashboard 仍在，说明确为孤儿。

## 端口段规划（建议，避免再撞）

| 段 | 用途 |
|---|---|
| 8000–8099 | 未来第二 Web 应用 |
| 8100–8999 | 临时调试/冒烟（**用完即杀**，不许常驻） |
| 9000–9299 | MCP 与业务后端 |
| 9300–9399 | 模型推理后端（9311 TTS / 9312 ASR） |
| 9400–9999 | 管理面（9999 已占） |
| 13000–13400 | SEFS 集群 |
| 18000–18999 | 代理/个人工具 |

## 清理记录

| 时间 | 对象 | 动作 | 证据 |
|---|---|---|---|
| 2026-11-16 19:00 | pid 1617166/1626677/1636058/1736164/151796（8852-8855/8899） | `kill`，端口回收 | 清理前 5 端口 200 响应、0 连接、fd 指向 /tmp 隔离库；清理后 `ss` 无 8852/8853/8854/8855/8899 |

## 刷新本表

```bash
# 1. 全量监听端口
sudo -n ss -tlnp | awk 'NR>1{print $4}' | sed 's/.*://' | sort -n -u
# 2. 未认领端口 → pid → cwd（这是关键一步，只杀确认过的）
sudo -n ss -tlnp | grep ":<端口> "
for p in <pid...>; do sudo -n readlink /proc/$p/cwd; done
# 3. 是否连生产库（把 pid 与 /mnt/sefs/conring/users 对上，就是事故）
for p in <pid...>; do sudo -n ls -l /proc/$p/fd | grep -o "/[^ ]*\.db[^ ]*"; done
```