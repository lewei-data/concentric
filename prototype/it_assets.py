#!/usr/bin/env python3
"""IT 系统资产台账（P1-1，2026-11-16 CEO 指令提前交付）。

权威数据 = opc.db 表 it_assets（迁移见 prototype/db.py SCHEMA v4）。
人读版 = /mnt/sefs/conring/users/18911789286/obsidian/公司/IT系统资产台账.md，由本脚本 render 自动重写，
与 ops/nodes.md 同款机制：禁止手工维护两份，防止漂移。

子命令：
  seed     写入/刷新台账行（INSERT OR REPLACE，保留现有 status/updated_at）
  check    逐条跑 verify_cmd → 回写 status/updated_at；端口漂移检测；重写 md。
           输出稳定签名行（IT_STATUS=..、IT_DRIFT ..），供 patrol_tick.sh 拼接。
  render   仅重写 md。
  show     打印台账（TSV）。

漂移检测口径（CEO 验收第 4 条）：
  - 206 本机与 208（ssh 只读）：ports 字段中声明的 NNNN/tcp 必须仍在 ss -tln 监听；
  - 21.157：由 verify_cmd（curl /system_stats）覆盖，端口被改即 verify 失败。
状态语义：ok=verify 通过；down=verify 失败；drift=声明端口消失；pending=信息待补。
退出码：0 全部 ok/pending；2 有 down/drift（与 opsctl check 一致）。
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("OPC_DB_PATH", os.path.join(BASE_DIR, "opc.db"))
VAULT_MD = os.environ.get("IT_ASSETS_MD", "/mnt/sefs/conring/users/18911789286/obsidian/公司/IT系统资产台账.md")
RUNBOOK = "公司/运维手册/IT系统恢复手册.md"
SSH208 = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "boray@192.168.31.208"]
COLS = ("id system host role public_domain reverse_proxy ports depends_on cert_path "
        "cert_expire data_disk log_paths backup_policy runbook verify_cmd owner "
        "status updated_at evidence").split()

# 2026-11-16 07:05-07:15 实测采集；evidence 一律写命令/配置文件路径。
SEED = [
 dict(id="app-workbench", system="opc-workbench（APP 工作台 / Hermes 编排内核）",
  host="sup206 192.168.21.206（万兆）/192.168.31.206（办公网）",
  role="3D 办公 APP 入口、编排内核、/api/ops 监控页、/api/notify/boss 语音播报",
  public_domain="ai.boraydata.com（路径 /workbench/）",
  reverse_proxy="208:/etc/nginx/conf.d/proxy_8001.conf include snippets/conring-mcp.conf location /workbench/ → http://192.168.21.206:8848/",
  ports="8848/tcp python3(prototype/server.py)",
  depends_on="opc.db(prototype/opc.db)、dispatchd 9120、MCP 9100/9101/9102、media-engine 9200、llm-qwen38-local、aliyun 模型 API",
  cert_path="内网直连无独立证书（TLS 在 208 终结）", cert_expire="不适用（证书见 gw-nginx-208）",
  data_disk="/ LVM ubuntu-vg 877G 用 240G(29%)；代码与库在 /opt/sefs(nvme0n1 1.8T 6%)",
  log_paths="journalctl -u opc-workbench；/opt/sefs/opc-workbench/logs/server.log",
  backup_policy="git 自动同步（每小时「自动同步」提交，git log 65a64bc 等实测）",
  verify_cmd="curl -fsS -m8 -o /dev/null http://127.0.0.1:8848/login",
  evidence="systemctl cat opc-workbench.service(ExecStart --port 8848)；208 snippets/conring-mcp.conf grep；curl /login 实测；ss -tlnp pid573416"),
 dict(id="llm-hermes-gateway", system="LLM 网关（hermes gateway + 模型注册表）",
  host="sup206 192.168.21.206",
  role="全员会话/定时任务统一出口：默认通道 qwen3.7-plus（aliyun token-plan OpenAI 兼容）；本地通道 qwen38；/v1/runs AG-UI",
  public_domain="ai.boraydata.com（路径 /conring/gw/）",
  reverse_proxy="208:snippets/conring-mcp.conf location /conring/gw/ → http://192.168.21.206:8642/",
  ports="8642/tcp hermes(gateway run)",
  depends_on="上游 https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1（/opt/sefs/hermes/config.yaml:120）、llm-qwen38-local 208:8080、opc.db llm_models 注册表",
  cert_path="内网直连无独立证书（TLS 在 208 终结）", cert_expire="不适用（证书见 gw-nginx-208）",
  data_disk="/ LVM 877G（配置目录 /opt/sefs/hermes 在 nvme0n1）",
  log_paths="journalctl -u hermes-gateway-8987483c.service",
  backup_policy="未建立（config.yaml 含 api_key，无版本库；建议脱敏后备份——缺口已登记）",
  verify_cmd="ss -tln | grep -q ':8642 '",
  evidence="ss -tlnp 8642→hermes pid1015221；systemctl cat hermes-gateway-8987483c(ExecStart hermes gateway run)；config.yaml:88 default qwen3.7-plus/120 base_url"),
 dict(id="llm-qwen38-local", system="本地 LLM 通道 qwen38（llama.cpp）",
  host="sup208 192.168.21.208",
  role="无实时性要求任务的离线推理（OpenAI 兼容，qwen3.8-flash-next，65536 ctx，纯 CPU）",
  public_domain="不对公网（仅 21 网段可达）",
  reverse_proxy="无（206 直连 http://192.168.21.208:8080/v1）",
  ports="8080/tcp llama-server",
  depends_on="模型文件 /opt/mcp/qwen38/models/Qwen3.8-Flash-Next-UD-Q3_K_XL-00001-of-00004.gguf + mmproj-F16.gguf",
  cert_path="无（http 明文，内网）", cert_expire="不适用",
  data_disk="208:/ LVM 98G 用 72G(77%)——11-16 磁盘事故同分区，新增大模型文件先查水位",
  log_paths="未登记（进程手工拉起，无 systemd 单元——缺口，runbook 给重启命令）",
  backup_policy="模型 gguf 在 208 本地盘，无异地备份（缺口已登记）",
  verify_cmd="curl -fsS -m10 http://192.168.21.208:8080/health",
  evidence="208 sudo ss -tlnp 8080→llama-server pid2139066；ps args 全行含模型路径与 --alias qwen3.8-flash-next"),
 dict(id="tts-mcp", system="TTS 服务（MCP 桥 + Qwen3-TTS 后端）",
  host="sup206 192.168.21.206",
  role="语音合成：员工 profile 经 MCP 9101；播报语音经后端 9311",
  public_domain="ai.boraydata.com（路径 /conring/tts/）",
  reverse_proxy="208:snippets/conring-mcp.conf location /conring/tts/ → http://192.168.21.206:9101/",
  ports="9101/tcp python(bridge.py --name tts)、9311/tcp python(serve_tts.py)",
  depends_on="qwen-tts.service（QWEN_TTS_PORT=9311, /opt/sefs/opc-workbench/qwen-audio/serve_tts.py）",
  cert_path="内网直连无独立证书（TLS 在 208 终结）", cert_expire="不适用（证书见 gw-nginx-208）",
  data_disk="/ LVM 877G(29%)；桥目录 /opt/sefs/mcp-tts 在 nvme0n1",
  log_paths="journalctl -u mcp-tts-http -u qwen-tts",
  backup_policy="随 206 git 同步（qwen-audio 代码）；模型权重备份未建立（缺口）",
  verify_cmd="systemctl is-active --quiet mcp-tts-http qwen-tts && curl -fsS -m8 -o /dev/null http://127.0.0.1:9311/health",
  evidence="systemctl cat mcp-tts-http/qwen-tts；curl 9311/health 实测 200；ss 9101 pid1355139"),
 dict(id="asr-mcp", system="ASR 服务（MCP 桥 + Qwen3-ASR 后端）",
  host="sup206 192.168.21.206",
  role="语音识别 + 语音回路（Streamable HTTP），并静态发布素材 /conring/asr/assets/*",
  public_domain="ai.boraydata.com（路径 /conring/asr/）",
  reverse_proxy="208:snippets/conring-mcp.conf location /conring/asr/ → http://192.168.21.206:9102/",
  ports="9102/tcp python(bridge.py --name asr)、9312/tcp python(serve_asr.py)",
  depends_on="qwen-asr.service（QWEN_ASR_PORT=9312, serve_asr.py）；静态目录 /opt/sefs/opc-workbench/prototype/assets",
  cert_path="内网直连无独立证书（TLS 在 208 终结）", cert_expire="不适用（证书见 gw-nginx-208）",
  data_disk="/ LVM 877G(29%)；素材在 prototype/assets（nvme0n1）",
  log_paths="journalctl -u mcp-asr-http -u qwen-asr",
  backup_policy="素材主体在 /mnt/sefs(conring 用户区)与 prototype/assets；git 同步代码",
  verify_cmd="systemctl is-active --quiet mcp-asr-http qwen-asr && curl -fsS -m8 -o /dev/null http://127.0.0.1:9312/health",
  evidence="systemctl cat mcp-asr-http --static-dir 参数/qwen-asr；curl 9312/health 实测 200"),
 dict(id="mail-mcp", system="邮件 MCP 桥",
  host="sup206 192.168.21.206",
  role="员工收发内部邮件的唯一入口（--identity-param agent，发件人不可伪造）",
  public_domain="ai.boraydata.com（路径 /conring/mail/）",
  reverse_proxy="208:snippets/conring-mcp.conf location /conring/mail/ → http://192.168.21.206:9100/",
  ports="9100/tcp python(bridge.py --name mail)",
  depends_on="mail-server（postfix/dovecot）",
  cert_path="内网直连无独立证书（TLS 在 208 终结）", cert_expire="不适用（证书见 gw-nginx-208）",
  data_disk="/ LVM 877G(29%)；配置 /opt/sefs/mcp-mail",
  log_paths="journalctl -u mcp-mail-http",
  backup_policy="配置目录未纳版本库（缺口已登记）",
  verify_cmd="systemctl is-active --quiet mcp-mail-http postfix dovecot",
  evidence="systemctl cat mcp-mail-http.service；ss 9100 pid1122596"),
 dict(id="mail-server", system="邮件服务器（Postfix+Dovecot, mail.conring.cn）",
  host="sup206 192.168.31.206（/etc/hosts: mail.conring.cn）",
  role="内部邮箱域 @mail.conring.cn 收发与存储（全员权限制度第 5 节的底座）",
  public_domain="mail.conring.cn（办公网名址，非公网域名）",
  reverse_proxy="不经 208（SMTP/IMAP 直连 206）",
  ports="25/tcp postfix、587/tcp postfix、143/tcp dovecot、993/tcp dovecot（127.0.0.1 与 192.168.31.206 双监听）",
  depends_on="/ LVM 根分区（/var/mail）",
  cert_path="993 使用系统默认证书链（snakeoil 级），未申请独立公信证书（待核实项）", cert_expire="不适用/待核实",
  data_disk="/ LVM 877G 用 240G(29%)",
  log_paths="/var/log/mail.log；journalctl -u postfix@- -u dovecot",
  backup_policy="未建立（邮件库无备份任务——缺口已登记）",
  verify_cmd="systemctl is-active --quiet postfix dovecot && ss -tln | grep -q ':993 '",
  evidence="ss -tlnp 25/143/587/993 双址实测；/etc/hosts mail.conring.cn→192.168.31.206；systemd 单元 postfix@-/dovecot active"),
 dict(id="media-engine", system="媒体引擎（CosyVoice TTS / ASR / 视频合成）",
  host="sup206 192.168.21.206（仅绑万兆口）",
  role="自我介绍视频/音频合成，开机预载 TTS 进程池（6×~7GB）消除冷启动",
  public_domain="ai.boraydata.com（路径 /conring/media/）",
  reverse_proxy="208:snippets/conring-mcp.conf location /conring/media/ → http://192.168.21.206:9200/",
  ports="9200/tcp python（绑 192.168.21.206，127.0.0.1 不可达属预期）",
  depends_on="系统 ffmpeg 6.1.1（drop-in 固定，禁 ~/.local/bin 2018 旧版）、/mnt/sefs 输出",
  cert_path="内网直连无独立证书（TLS 在 208 终结）", cert_expire="不适用（证书见 gw-nginx-208）",
  data_disk="/ LVM 877G(29%)；工作目录 /opt/sefs/opc-workbench/media-engine",
  log_paths="journalctl -u media-engine",
  backup_policy="随 206 git 自动同步（代码）；产物走 /mnt/sefs",
  verify_cmd="curl -fsS -m8 -o /dev/null http://192.168.21.206:9200/health",
  evidence="systemctl cat media-engine(+ffmpeg.conf drop-in 注释)；curl 9200/health 实测 200；ss 绑 192.168.21.206:9200"),
 dict(id="media-gen", system="媒体生成（SD1.5 出图 / WAN2.1 文生视频）",
  host="sup206 192.168.21.206（仅绑万兆口）",
  role="本机异步出图（Realistic Vision V5.1）与 T2V（WAN2.1-T2V-1.3B）",
  public_domain="不对公网（21 网段内部调用）",
  reverse_proxy="无（http://192.168.21.206:9103 直连）",
  ports="9103/tcp python（media-gen/run.sh）",
  depends_on="/opt/sefs/opc-workbench/media-gen 权重目录、ffmpeg 6.1.1",
  cert_path="内网 http 明文，无证书", cert_expire="不适用",
  data_disk="/ LVM 877G(29%)；权重在 media-gen 目录（nvme0n1 6%）",
  log_paths="journalctl -u media-gen",
  backup_policy="代码随 git 同步；权重未异地备份（缺口）",
  verify_cmd="curl -fsS -m8 -o /dev/null http://192.168.21.206:9103/health",
  evidence="systemctl cat media-gen.service；curl 9103/health 实测 200；ss 192.168.21.206:9103 pid2577604"),
 dict(id="dispatchd", system="调度中心 dispatchd（P0 实时 + 任务队列）",
  host="sup206 192.168.21.206",
  role="APP 输入链路的实时通道：MCP 转发任务分类打标、优先级队列、老板 P0 专属 worker",
  public_domain="ai.boraydata.com（路径 /conring/dispatch/ 与 /conring/dispatch-ws/）",
  reverse_proxy="208:snippets/conring-mcp.conf location /conring/dispatch/ → :9120；/conring/dispatch-ws/ → :9121",
  ports="9120/tcp python(dispatchd.py HTTP)、9121/tcp python(实时 WS)",
  depends_on="dispatch.db、hermes worker（resume_profiles secretary 等）",
  cert_path="内网直连无独立证书（TLS 在 208 终结）", cert_expire="不适用（证书见 gw-nginx-208）",
  data_disk="/ LVM 877G(29%)；目录 /opt/sefs/hermes/dispatch",
  log_paths="/opt/sefs/hermes/dispatch/dispatch.log；journalctl -u dispatchd",
  backup_policy="db 无独立备份（见 dispatch-db 行）；代码在 /opt/sefs/hermes 未纳版本库（缺口）",
  verify_cmd="curl -fsS -m8 http://127.0.0.1:9120/health | grep -q '\"ok\": true'",
  evidence="systemctl cat dispatchd（0.0.0.0 绑定的原因见单元注释：公网边缘反代要连 9120）；curl /health 实测 ok:true queue_depth:0；ss 9120/9121 pid1523293"),
 dict(id="dispatch-db", system="dispatch.db（调度台账，APP 输入链路数据面）",
  host="sup206 192.168.21.206",
  role="SQLite(WAL)：任务/台账唯一写者是 dispatchd；工作台与编排侧读取",
  public_domain="不适用（数据文件）", reverse_proxy="不适用",
  ports="无监听（文件资产）",
  depends_on="dispatchd（唯一写者）",
  cert_path="不适用", cert_expire="不适用",
  data_disk="/opt/sefs/hermes/dispatch/dispatch.db+wal/shm+state.db（/ LVM 29%）",
  log_paths="sqlite 无日志；写侧看 dispatch.log",
  backup_policy="每日 03:30 sqlite3 .backup 至 /mnt/sefs/conring/backup/db/<日期>/dispatch.db（保留 14 天）",
  verify_cmd="sqlite3 /opt/sefs/hermes/dispatch/dispatch.db 'PRAGMA quick_check;' | grep -qx ok",
  evidence="ls /opt/sefs/hermes/dispatch/（dispatch.db-shm/-wal 在盘=WAL 活跃）；dispatchd.py docstring「记台账（SQLite WAL）」"),
 dict(id="sefs-cluster", system="SEFS（RapidSeFS）分布式存储集群",
  host="mgmtd/meta/storage 均在 sup206 192.168.21.206（客户端：206 本机、21.157 已证、208 待证）",
  role="全司用户数据/输出/素材底座（/mnt/sefs/conring/users/*，ComfyUI 输出、语音播报 wav 均落此）",
  public_domain="不适用（内网存储）", reverse_proxy="不适用",
  ports="13301/tcp+udp(mgmtd)、客户端 UDP 13304（connPortShift=0）；RDMA RoCE 网卡 ens2f1np1(rocep109s0f1 ACTIVE)，ens2f0np0 口 DOWN⚠️",
  depends_on="/data/sefs(nvme1n1)、/data/sedb_system(nvme2n1) 存储目标盘、RDMA 链路",
  cert_path="不适用", cert_expire="不适用",
  data_disk="存储盘 /data/sefs 1.4T/1.8T=77%⚠️高水位、/data/sedb_system 8%；客户端挂载 /mnt/sefs 3.5T 用 2.4T=67%",
  log_paths="journalctl -u sefs-manager -u sefs-meta -u sefs-storage；208 syslog 的 sefs programname 已被 49-sefs-rdma-drop.conf 过滤（11-16 P0-1）",
  backup_policy="meta_bak_202607211449 为最近一次元数据手工备份（/data/sefs/ 实测目录名）——无定期备份，缺口已登记",
  verify_cmd="mountpoint -q /mnt/sefs && systemctl is-active --quiet sefs-manager sefs-meta sefs-storage && ls /mnt/sefs/conring >/dev/null",
  evidence="/etc/sefs/sefs-client.conf(sysMgmtdHost=192.168.21.206 connMgmtdPort=13301 connUseRDMA=true)；mount|grep sefs；rdma link show；df -h /mnt/sefs /data/sefs；157 /proc/mounts sefs 行"),
 dict(id="gw-nginx-208", system="接入网关（208 nginx + certbot）",
  host="sup208 192.168.21.208/192.168.31.208",
  role="ai.boraydata.com 唯一公网入口：TLS 终结 + /conring/* 分流到 206 各服务",
  public_domain="ai.boraydata.com",
  reverse_proxy="主配置 /etc/nginx/conf.d/proxy_8001.conf（443）+ sefs-mcp.conf（80 default）+ snippets/conring-mcp.conf（路由清单）+ conring-gw-limits.conf/gzip.conf；proxy_pass 目标 127.0.0.1:8765(sefs-mcp) 与 192.168.21.206:9100/9101/9102/9200/9120/9121/8642/8848",
  ports="80/tcp nginx、443/tcp nginx（双栈）；备份文件 proxy_8001.conf.bak-20261102/-1111、sefs-mcp.conf.bak-llm-20261113 在 conf.d",
  depends_on="sefs-mcp-edge(127.0.0.1:8765)、206 各服务、certbot.timer 续期",
  cert_path="/etc/letsencrypt/live/ai.boraydata.com/fullchain.pem（key: privkey.pem 同目录）",
  cert_expire="2027-02-12 01:03:20+00:00（certbot certificates 实测 VALID 88 天；certbot.timer 每日双次自动续期 00:36/21:21）",
  data_disk="proxy_temp 未显式配置→nginx 编译默认 /var/lib/nginx/proxy（208 /var 与 / 同分区）；所在分区 / LVM 98G 用 72G=77%⚠️——11-16 磁盘写满事故根因分区，水位>80% 即 critical",
  log_paths="208:/var/log/nginx/access.log、error.log；/var/log/syslog（RDMA 噪声已过滤，logrotate maxsize 500M+rotate3+compress）",
  backup_policy="配置改动有 .bak 序列留痕；无系统级配置仓库（缺口已登记）；纪律：CTO 不动 nginx 配置与证书，故障只上报（infra-patrol 制度）",
  verify_cmd="curl -fsS -m10 -o /dev/null https://ai.boraydata.com/ && ssh -o BatchMode=yes -o ConnectTimeout=8 boray@192.168.31.208 'systemctl is-active --quiet nginx'",
  evidence="208 grep conf.d/snippets 输出、certbot certificates 原文、sudo ss 80/443、df 208 / 77%；11-16 复盘报告第 2 节 proxy_temp pwritev(28) 记录"),
 dict(id="sefs-mcp-edge", system="SEFS Data MCP（208 公网 MCP 边缘）",
  host="sup208 192.168.21.208（仅 127.0.0.1）",
  role="外部 MCP 客户端经 ai.boraydata.com 访问 SEFS 数据的落地服务（proxy_8001.conf 默认 location）",
  public_domain="ai.boraydata.com（路径 /）",
  reverse_proxy="208:proxy_8001.conf location / → http://127.0.0.1:8765",
  ports="8765/tcp python(/opt/mcp/sefs-mcp/server.py --transport http，仅回环)",
  depends_on="gw-nginx-208、SEFS 客户端挂载",
  cert_path="TLS 在 nginx 终结", cert_expire="不适用（证书见 gw-nginx-208）",
  data_disk="/opt/mcp/sefs-mcp 在 208 / LVM(77%)",
  log_paths="未登记（无 systemd 单元证据，进程 pid378037——缺口）",
  backup_policy="未建立（缺口已登记）",
  verify_cmd="ssh -o BatchMode=yes -o ConnectTimeout=8 boray@192.168.31.208 'ss -tln | grep -q \"127.0.0.1:8765 \"'",
  evidence="208 sudo ss 8765 + ps args=/opt/mcp/sefs-mcp/.venv/bin/python server.py；proxy_8001.conf 头部注释"),
 dict(id="comfyui-8188", system="ComfyUI 出图实例（21.157 :8188）",
  host="192.168.21.157（comfyui-21-157，用户 rpdsql，密钥 ~/.ssh/opc_comfy_ed25519）",
  role="图像渲染（--cpu 模式）；comfyctl.py 统一接单/取回",
  public_domain="不对公网（21 网段）", reverse_proxy="无（http://192.168.21.157:8188 直连）",
  ports="8188/tcp python(main.py --listen 0.0.0.0 --port 8188)",
  depends_on="模型权重 /conring/ComfyUI/models（extra_model_paths.yaml base_path，实测 102G）、输出目录 /mnt/sefs/conring/comfyui-output（SEFS）",
  cert_path="无（http）", cert_expire="不适用",
  data_disk="157:/ openeuler-root 69G 用 5.3G=9%；/data vgdisk3-lvdisk 1.4T 1%；/conring nvme0n1 ext2（权重盘）",
  log_paths="157:/conring/ComfyUI/comfyui.log",
  backup_policy="权重无异地备份（102G，缺口已登记）；任务登记 /opt/sefs/opc-workbench/comfyui-ops/jobs.jsonl（206 侧，随 git 同步）",
  verify_cmd="curl -fsS -m8 -o /dev/null http://192.168.21.157:8188/system_stats",
  evidence="comfyui-ops/config.json endpoints.image；157 ps cmdline(pid2125108 含 --output-directory /mnt/sefs/conring/comfyui-output)；du/df 实测"),
 dict(id="comfyui-8189", system="ComfyUI 视频实例（21.157 :8189）",
  host="192.168.21.157（同上 SSH 通道）",
  role="Wan2.2 T2V/I2V 视频渲染（串行队列，见 comfyui-render-ops 技能）",
  public_domain="不对公网（21 网段）", reverse_proxy="无（http://192.168.21.157:8189 直连）",
  ports="8189/tcp python(main.py --cpu --port 8189 --output-directory /data)",
  depends_on="同 8188 权重目录；temp /tmp/comfy_temp2、user /tmp/comfy_user2、input /conring/ComfyUI/input",
  cert_path="无（http）", cert_expire="不适用",
  data_disk="输出在 157:/data 1.4T(1%) 与 /mnt/sefs/conring/comfyui-output",
  log_paths="157:/conring/ComfyUI/comfyui_8189.log",
  backup_policy="同 comfyui-8188（任务登记随 git；权重无异地备份）",
  verify_cmd="curl -fsS -m8 -o /dev/null http://192.168.21.157:8189/system_stats",
  evidence="comfyui-ops/config.json endpoints.video.start 模板；157 ps cmdline(pid2125109)；ss 8189 实测"),
 dict(id="obsidian-vault", system="知识库/制度库存储（Obsidian vault）",
  host="sup206 192.168.21.206（路径 /mnt/sefs/conring/users/18911789286/obsidian）",
  role="公司制度、岗位记录、知识库条文的唯一文件底座；全员 file 工具读写域",
  public_domain="不适用", reverse_proxy="不适用",
  ports="无监听（文件资产；经 8848 工作台只读呈现）",
  depends_on="vault_export.py（公司配置/运行时动态导出写入方）、各员工 profile 写口",
  cert_path="不适用", cert_expire="不适用",
  data_disk="/mnt/sefs/conring/users/18911789286/obsidian 位于 sefs_nodev rapids_sefs /mnt/sefs 3.5T 用 2.4T(69%)（2026-11-16 由本地旧库迁入，旧库待删）",
  log_paths="无守护日志；导出方 cron（cto-daily-report 等）经 journalctl 间接可查",
  backup_policy="每日 03:30 rsync 镜像至 /mnt/sefs/conring/backup/vault（--delete 与源 1:1，排除 .trash）；成功标记 .files_success 由巡检判新鲜",
  verify_cmd="test -f /mnt/sefs/conring/users/18911789286/obsidian/00-首页.md && test -w /mnt/sefs/conring/users/18911789286/obsidian",
  evidence="df -h /mnt/sefs → sefs_nodev rapids_sefs 3.5T 用 2.4T(69%)；mount /mnt/sefs rapids_sefs；ls vault 根；test -w 实测"),
 dict(id="platform-services", system="平台组件（有 systemd 单元 / 常驻，非公司业务）",
  host="sup206（192.168.21.206 / 192.168.31.206，无公网 IP）",
  role="backlot-board 4750（OpenMontage 分镜看板，uvicorn `--host 0.0.0.0`，**首页 200 无鉴权**）；"
       "hermes-dashboard 9119（hermes 管理面板，`0.0.0.0`，有 /login；MainPID 2149775 起于 11-09）；"
       "**37603 孤儿**（pid 1015221，`Hermes Agent - Dashboard`，`HERMES_SUPERVISED_CHILD=1` 但 ppid=1、"
       "不在 dashboard 的 CGroup，11-13 20:07 出生，0 连接未清理，待 owner 确认）；"
       "rpdsql-ops 9999（rapids-manager，**root 常驻 96 天**，管理面）；"
       "SEFS 集群 13300/13301/13303/13305/13307；syncthing 8384/22000；smbd 445；adb 5037",
  public_domain="无（仅内网）", reverse_proxy="无（未进 nginx）",
  ports="4750/9119/9999/13300/13301/13303/13305/13307/8384/22000/445/5037（进程级认领见 ops/ports.md）",
  depends_on="systemd（backlot-board / hermes-dashboard / rpdsql-ops）；SEFS 集群自身；"
             "syncthing 曾承担 /opt/sefs/obsidian 跨机同步，2026-11-16 库迁至 /mnt/sefs/conring/users/18911789286/obsidian 后旧库仅存档",
  cert_path="不适用", cert_expire="不适用",
  data_disk="同 /mnt/sefs（备份未单独覆盖 ComfyUI 102G 权重与 OpenMontage 产物）",
  log_paths="journalctl -u backlot-board / hermes-dashboard / rpdsql-ops",
  backup_policy="未单独备份（配置随代码发版；产物目录未纳入 ops/backup.sh）",
  runbook="4750/9119 若被投诉\"外网能打开\"：`ip addr` 确认无公网 IP + iptables 无 DNAT 即内网；"
          "要收紧就改单元 `--host 127.0.0.1` 后 `sudo -n systemctl restart <unit>`（会影响内网访问，需 owner 同意）",
  verify_cmd="systemctl is-active backlot-board.service hermes-dashboard.service rpdsql-ops.service >/dev/null",
  owner="CTO",
  evidence="2026-11-16 全量 `ss -tlnp` 对账：iptables INPUT policy=ACCEPT 且仅 8642 有源限制规则，"
           "ufw 不活动，无 PREROUTING DNAT，本机仅 192.168.21.206/192.168.31.206/172.17.0.1 → 内网可达、非公网。"
           "4750 首页 200、9999 首页 200、9119 → 302 /login（实测）。"
           "第二轮对账 42 个监听端口：**本机无 443**（`ss -tln` 计 0，HTTPS 由 208 终结）已从 ports.md 撤下；"
           "37603 为 dashboard 脱管孤儿（0 连接、/login 不引用、MainPID 无连接）未杀。详见 ops/ports.md"),
dict(id="opc-db", system="工作台业务库（每员工一库）",
  host="sup206 /mnt/sefs/conring/users/<手机号>/opc.db（owner 18911789286 16M；测试号三库各 268K）",
  role="**业务数据真源**。25 张业务表 + 4 张 FTS 影子表："
       "messages/chat_messages/chat_threads（会话与消息）、pipeline_runs/traces/trace_events（流水线与追踪）、"
       "approvals（审批）、knowledge/knowledge_relations/knowledge_questions（知识库）、"
       "meetings/meeting_speeches（会议）、contents/reports/hotspots/collect_*（内容与采集）、"
       "token_usage/llm_models/llm_schedule（模型与计量）、workshop_steps/product_crafts/runs（工坊）、"
       "directives/ingest_cursor/stats_daily/release_ledger/asset_links。"
       "**敏感级：高**——含聊天正文、审批意见、员工记忆、令牌用量",
  public_domain="不适用", reverse_proxy="不适用",
  ports="无监听（由 8848 进程内经 prototype/db.py 直连 sqlite）",
  depends_on="/mnt/sefs/conring（SEFS 集群盘，非本地盘）、prototype/db.py（唯一读写入口）、ops/backup.sh",
  cert_path="不适用", cert_expire="不适用",
  data_disk="owner 库 16M（trace_events 3623 行最多、chat_messages 1231、messages 1037）；"
            "四库合计 16.8M，日增约 1–3M；/mnt/sefs 3.5T 用 2.4T(67%)",
  log_paths="无独立日志（审计走 trace_events/traces 表）；备份日志 ops/backups/backup.log",
  backup_policy="每日 03:30 `sqlite3 .backup` → backup/db/<日期>/opc-<手机号>.db，"
                "integrity_check=ok 才计入成功数，保留 14 天。**RPO ≤ 24h**",
  runbook="恢复：停 opc-workbench.service → cp 备份库覆盖 → **同删 -wal/-shm** → 启服务 → selfcheck。"
          "**RTO ≤ 5 分钟**。保留期：业务表永久（不可自动裁剪）；trace_events/token_usage 建议 180 天滚动，未实施。"
          "勿用 prototype/opc.db 代替（那是 CLI 遗留库，无业务数据）",
  verify_cmd="python3 -c \"import sqlite3;c=sqlite3.connect('file:/mnt/sefs/conring/users/18911789286/opc.db?mode=ro',uri=True);"
             "assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok';print('messages',c.execute('select count(*) from messages').fetchone()[0])\"",
  owner="CTO（数据）/ owner（内容）",
  evidence="2026-11-16 实测：integrity_check=ok；messages 1037、pipeline_runs 16、approvals 39；"
           "与当日备份副本三个数完全一致（恢复演练通过）；表清单见 `sqlite3 opc.db .tables`"),
dict(id="backup-store", system="数据备份（DB 快照 + 文件镜像）",
  host="sup206 192.168.21.206 → /mnt/sefs/conring/backup（SEFS 集群盘）",
  role="opc.db/media_jobs.db/nodes.db/dispatch.db/hermes state.db 每日快照；vault 与各用户 vault rsync 镜像",
  public_domain="不适用", reverse_proxy="不适用",
  ports="无监听（ops/backup.sh 由 cron 触发，不常驻）",
  depends_on="ops/backup.sh、crontab 30 3 * * *、/mnt/sefs/conring/backup、rsync、sqlite3",
  cert_path="不适用", cert_expire="不适用",
  data_disk="/mnt/sefs 3.5T 用 2.4T(67%)；备份目标 /mnt/sefs/conring/backup",
  log_paths="/opt/sefs/opc-workbench/ops/backups/backup.log；状态 ops/backup_status.json",
  backup_policy="每日 03:30 全量 DB 快照（保留 14 天）+ vault/用户 vault 镜像；"
                "成功标记 $DEST/db/.last_success 与 $DEST/.files_success，"
                "由巡检 fresh 协议判新鲜（>26h 即告警，防止备份静默失效）",
  verify_cmd="python3 -c \"import json;d=json.load(open('/opt/sefs/opc-workbench/ops/backup_status.json'));raise SystemExit(0 if d['fail']==0 else 1)\"",
  evidence="2026-11-16 首跑：7 库 .backup+integrity=ok 耗时 4.6s；"
           "vault 26G/97504 文件首次镜像；巡检 sup206/backup-db 与 backup-files 两项"),
dict(id="nas", system="NAS 网络存储",
  host="待补（本轮无设备信息）",
  role="CEO 必录清单项；当前全司共享存储职责实际由 SEFS 集群承担",
  public_domain="不适用", reverse_proxy="不适用", ports="待补", depends_on="待补",
  cert_path="待补", cert_expire="待补", data_disk="待补", log_paths="待补",
  backup_policy="待补",
  verify_cmd="",
  evidence="采集无果证据：206 mount/fstab/hosts 无 NAS 条目；ops/nodes.db 3 节点无 NAS；vault 全文 grep 'NAS' 仅 事故与复盘/2026-11-16-网关磁盘写满-复盘.md:119 与管理层绩效/CTO-2026-11.md:71 的入账要求本身。status=pending，不编造。",
  _status="pending"),
]

MD_COLS = [("system", "系统"), ("host", "主机"), ("role", "角色"),
           ("public_domain", "公网域名"), ("reverse_proxy", "反代链路(配置路径)"),
           ("ports", "端口/协议/进程"), ("depends_on", "依赖"), ("cert_path", "证书路径"),
           ("cert_expire", "证书到期"), ("data_disk", "数据盘(挂载/容量/水位)"),
           ("log_paths", "日志落点"), ("backup_policy", "备份策略"), ("runbook", "恢复手册"),
           ("verify_cmd", "验活命令"), ("owner", "负责人")]


def now():
    return datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S%z")


def connect():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    return db


def seed(db):
    n = 0
    for a in SEED:
        cur_status = db.execute("SELECT status FROM it_assets WHERE id=?", (a["id"],)).fetchone()
        status = cur_status["status"] if cur_status else a.get("_status", "unchecked")
        db.execute(
            "INSERT OR REPLACE INTO it_assets (" + ",".join(COLS) + ") VALUES (" +
            ",".join("?" * len(COLS)) + ")",
            tuple([a["id"], a["system"], a["host"], a["role"], a["public_domain"],
                   a["reverse_proxy"], a["ports"], a["depends_on"], a["cert_path"],
                   a["cert_expire"], a["data_disk"], a["log_paths"], a["backup_policy"],
                   RUNBOOK + "#" + a["id"], a["verify_cmd"], "CTO 扶频", status,
                   now(), a["evidence"]]))
        n += 1
    db.commit()
    return n


def run_verify(cmd, timeout=25):
    if not cmd:
        return None, "无验活命令"
    try:
        p = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True, text=True,
                           timeout=timeout)
        out = (p.stdout + p.stderr).strip().replace("\n", " ")[:200]
        return p.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT>%ss" % timeout


def live_ports_206():
    p = subprocess.run(["ss", "-tln"], capture_output=True, text=True)
    return set(re.findall(r":(\d+)\s", p.stdout))


def live_ports_208():
    try:
        p = subprocess.run(SSH208 + ["ss -tln"], capture_output=True, text=True, timeout=20)
        return set(re.findall(r":(\d+)\s", p.stdout))
    except subprocess.TimeoutExpired:
        return None


def check(db):
    drifts, downs = [], []
    ports206 = live_ports_206()
    ports208 = None  # 懒加载
    rows = db.execute("SELECT * FROM it_assets ORDER BY id").fetchall()
    for r in rows:
        a = dict(r)
        declared = set(re.findall(r"(\d{2,5})/tcp", a["ports"] or ""))
        if a["id"] == "nas" or not a["verify_cmd"]:
            a["status"] = "pending"
            a["detail"] = "待 CEO/老板补充设备信息"
            # 端口漂移不适用；继续
            db.execute("UPDATE it_assets SET status=?, updated_at=? WHERE id=?",
                       ("pending", now(), a["id"]))
            continue
        if a["host"].startswith("sup206") or "192.168.21.206" in a["host"] or "192.168.31.206" in a["host"]:
            gone = {p for p in declared if p not in ports206}
        elif "sup208" in a["host"] or "192.168.21.208" in a["host"]:
            if ports208 is None:
                ports208 = live_ports_208() or set()
            gone = {p for p in declared if p not in ports208}
        else:
            gone = set()  # 157/其他由 verify_cmd 覆盖
        ok, out = run_verify(a["verify_cmd"])
        if gone:
            a["status"] = "drift"
            drifts.append("%s:声明端口未监听 %s" % (a["id"], ",".join(sorted(gone))))
        elif ok:
            a["status"] = "ok"
        else:
            a["status"] = "down"
            downs.append("%s:verify失败(%s)" % (a["id"], out[:120]))
        a["detail"] = out if out else ""
        db.execute("UPDATE it_assets SET status=?, updated_at=? WHERE id=?",
                   (a["status"], now(), a["id"]))
    db.commit()
    return rows, drifts, downs


def render(db):
    rows = db.execute("SELECT * FROM it_assets ORDER BY id").fetchall()
    bad = {r["id"]: r for r in rows if r["status"] in ("down", "drift", "pending")}
    L = []
    L.append("---")
    L.append("role: IT台账")
    L.append("date: %s" % datetime.now(TZ).strftime("%Y-%m-%d"))
    L.append("kind: 运维台账")
    L.append("tags: [IT台账, 运维, 资产]")
    L.append("---")
    L.append("")
    L.append("# IT 系统资产台账")
    L.append("")
    L.append("> **由 `prototype/opc.db` 表 `it_assets` 自动生成，勿手改**（手改必被下轮巡检覆盖）。")
    L.append("> 修改数据请改库（`sqlite3 prototype/opc.db` 或 `prototype/it_assets.py seed`），")
    L.append("> 权威口径见 `公司/岗位职责.md` CTO 节与恢复手册 `公司/运维手册/IT系统恢复手册.md`。")
    L.append("> 生成时间：%s（ops-patrol-30m 每轮自动重写；⚠️=实测不通/漂移/待补）" % now())
    L.append("")
    L.append("| ID | %s | 状态 | 更新 |" % " | ".join(h for _, h in MD_COLS))
    L.append("|---|" + "---|" * (len(MD_COLS) + 3))
    for r in rows:
        mark = " ⚠️" if r["status"] in ("down", "drift") else (" ❔" if r["status"] == "pending" else "")
        cells = [str(r[k] or "—").replace("|", "\\|").replace("\n", " ") for k, _ in MD_COLS]
        st = r["status"] + mark
        L.append("| %s | %s | %s | %s |" % (r["id"], " | ".join(cells), st,
                                            (r["updated_at"] or "")[:16]))
        # 状态列即 owner 后一列；见 MD_COLS 顺序（owner,status）
    L.append("")
    L.append("## 验活与漂移")
    L.append("- 验活命令列在台账内一条命令可跑，退出码 0 即 alive；巡检每 30 分钟回写 status/updated_at。")
    if bad:
        L.append("- 本轮异常/待补：%s" % "；".join(sorted(bad)))
    else:
        L.append("- 本轮全部条目验活通过。")
    L.append("")
    L.append("## 排障入口（内化路径）")
    L.append("岗位职责 → 本台账定位资产与主机 → `[[IT系统恢复手册]]` 对应锚点执行恢复/上报。")
    L.append("分层诊断：源文件→本机端口(ss)→本机直连(curl)→网关 208→公网 ai.boraydata.com。")
    os.makedirs(os.path.dirname(VAULT_MD), exist_ok=True)
    with open(VAULT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["seed", "check", "render", "show"])
    args = ap.parse_args()
    db = connect()
    if args.cmd == "seed":
        print("seeded rows=%d" % seed(db))
    elif args.cmd == "check":
        rows, drifts, downs = check(db)
        render(db)
        bad = len(drifts) + len(downs)
        print("IT_STATUS=%s total=%d" % ("DEGRADED" if bad else "OK", len(rows)))
        for d in drifts:
            print("IT_DRIFT %s" % d)
        for d in downs:
            print("IT_DOWN %s" % d)
        sys.exit(2 if bad else 0)
    elif args.cmd == "render":
        print("rendered rows=%d -> %s" % (render(db), VAULT_MD))
    else:
        for r in db.execute("SELECT id,status,host,ports FROM it_assets ORDER BY id"):
            print("\t".join(str(x) for x in r))


if __name__ == "__main__":
    main()
