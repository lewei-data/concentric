#!/usr/bin/env python3
"""把 OPC 工作台的「公司配置」与「运行时动态」导出成 Obsidian 仓库（纯 Markdown）。

用法：
    python3 vault_export.py            # 默认写入 /opt/sefs/obsidian
    OPC_VAULT=/path/to/vault python3 vault_export.py

配置页直接读取 server.py 里的常量，避免两处手工维护；动态页读取运行中服务的 /api/state。
服务未启动时会写出「服务未运行」占位，不会报错。
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "prototype"))
import server as S  # noqa: E402  仅为读取配置常量（导入不会启动服务）

VAULT = os.environ.get("OPC_VAULT", "/opt/sefs/obsidian")
BASE = os.environ.get("OPC_BASE", "http://127.0.0.1:8848")
TZ = timezone(timedelta(hours=8))

MSG_TYPES = ["delegation", "result", "conflict", "vote", "decision", "biz.event",
             "escalation", "approval", "report.ready", "scene.change", "heartbeat"]


def now():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


COOKIE = ""   # 服务开启登录后，导出脚本用凭据表里的账号换取会话 Cookie


def _login_cookie():
    """用 Obsidian 凭据表里的第一个可用账号登录，返回 opc_sess token。"""
    for u in S.load_credentials():
        acc = u.get("phone") or u.get("email") or u.get("user")
        if not acc:
            continue
        data = json.dumps({"account": acc, "password": u["password"]}).encode()
        req = urllib.request.Request(BASE + "/api/login", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                sc = r.headers.get("Set-Cookie") or ""
        except Exception as exc:
            print(f"[warn] 自动登录失败：{exc}")
            return ""
        return sc.split("opc_sess=", 1)[1].split(";", 1)[0] if "opc_sess=" in sc else ""
    return ""


def _req(path):
    global COOKIE

    def call():
        req = urllib.request.Request(BASE + path)
        user = os.environ.get("OPC_BASIC_USER", "")
        if user:
            pwd = os.environ.get("OPC_BASIC_PASS", "")
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode())
        if COOKIE:
            req.add_header("Cookie", f"opc_sess={COOKIE}")
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r)

    try:
        return call()
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not COOKIE:
            COOKIE = _login_cookie()
            if COOKIE:
                return call()
        raise


def cell(v):
    """表格单元格转义：竖线与换行会破坏 Markdown 表格。"""
    return str(v).replace("|", "\\|").replace("\n", " ").strip()


def write(rel, title, tags, body):
    path = os.path.join(VAULT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    head = f"---\ntitle: {title}\ntags: [{', '.join(tags)}]\nupdated: {now()}\n---\n\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + body.rstrip() + "\n")
    return path


# ---------------------------------------------------------------- 公司配置页

ROLE_RECORDS = [("secretary", "小秘书"), ("ceo", "CEO"), ("cto", "CTO"), ("cco", "CCO")]


def _stamp(path):
    """文件最后修改时间（导出时采样）；文件不存在返回空串，由调用方显示「未创建」。"""
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), TZ).strftime("%m-%d %H:%M")
    except OSError:
        return "未创建"


def role_record_rows():
    """首页的岗位记录引用：每岗位给出 latest 与当日记录的直链 + 修改时间。

    链接按真实文件存在与否生成，缺文件写「未创建」而不是死链；时间只是导出时的采样值。
    """
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    rows = []
    for role, cn in ROLE_RECORDS:
        base = os.path.join(VAULT, "OPC/岗位记录", role)
        latest = os.path.join(base, "latest.md")
        daily = os.path.join(base, f"{today}.md")
        latest_cell = (f"[[OPC/岗位记录/{role}/latest|latest]]"
                       if os.path.exists(latest) else "latest（未创建）")
        daily_cell = (f"[[OPC/岗位记录/{role}/{today}|{today}]]"
                      if os.path.exists(daily) else f"{today}（未创建）")
        rows.append(f"| {cn} | {latest_cell} | {daily_cell} | "
                    f"{_stamp(latest)} | {_stamp(daily)} |")
    return "\n".join(rows)


def page_home():
    body = f"""# OPC 一人公司 · 工作台知识库

> [!info] 这是什么
> 一个人经营三个项目、雇 {len(S.AGENT_SPEC)} 个 AI 员工。这里存放**公司配置**（编制、项目、内核参数、红线）
> 与**运行时动态**（谁在干什么、消息流、审批、报告、知识资产）。

## 公司配置
- [[岗位职责]] —— 指挥链、每日 3→9→9→6 选题流程、RACI、交接与门禁
- [[组织编制]] —— {len(S.AGENT_SPEC)} 人 / 8 类岗位，含工具白名单与审批高危项
- [[项目与调性]] —— 文人莫克、狗大人、同心圆；风格隔离与 KPI
- [[内核与编排]] —— Hermes 内核参数、DAG 波次、消息协议、审批门

## 运行时动态
- [[运行时快照]] —— 当前场景、会议、员工状态、流水线、内核统计
- [[事件流]] —— 最近一条 trace 的类型化消息
- [[审批与报告]] —— 审批门记录与 CEO/CTO/项目报告
- [[知识与KPI]] —— 知识库条目与三项目指标
- [[OPC/创意规格/00-索引|创意规格]] —— CCO 给每个在制作品定的亮点卡（钩子/结构/运镜/音色/封面），下游施工唯一规格来源
- [[OPC/技术研究/00-索引|技术研究]] —— CTO 的技术验证课题与「可用/不可用/待定」结论

## 岗位记录（四岗位留痕，人人可读）
规则见 [[OPC/岗位记录/00-索引|岗位记录索引]]：各岗位只写自己目录，跨岗位交接以对方 `latest.md` 的「交接」为准。

| 岗位 | 固定入口 | 今日记录 | latest 更新 | 今日记录更新 |
| --- | --- | --- | --- | --- |
{role_record_rows()}

> 更新时间为导出时采样；「未创建」＝该岗位当天还没落记录。汇报成品（md/PDF/语音）在 `OPC/汇报/<role>/`，最新一份是 `latest.md` 与 `latest.pdf`。

## 更新方式
```bash
cd /home/boray/opc-workbench
./opc.sh vault        # 重新生成本仓库（配置页读代码常量，动态页读 /api/state）
```

> 导出时间：{now()}｜数据源：`{BASE}`｜配置源：`prototype/server.py`
"""
    return write("00-首页.md", "OPC 首页", ["opc", "首页"], body)


def profile_skills(aid):
    """读取该员工 profile 内实际安装的技能类目录。"""
    d = os.path.join("/opt/sefs/hermes/profiles", aid, "skills")
    if not os.path.isdir(d):
        return "—"
    cats = sorted(c for c in os.listdir(d) if not c.startswith(".") and os.path.isdir(os.path.join(d, c)))
    return "、".join(cats) or "—"


def page_org():
    rows = []
    for i, (aid, name, role, pid, tools, scope) in enumerate(S.AGENT_SPEC, 1):
        project = S.PROJECTS.get(pid, {}).get("name", "全公司")
        tone = S.TONE.get(pid, "跨项目中台")
        high = [t for t in tools if t in S.HIGH_RISK_TOOLS]
        rows.append(f"| {i} | {aid} | {name} | {role} | {project} | {tone} | "
                    f"`{aid}` | {profile_skills(aid)} | {'、'.join(tools)} | "
                    f"{'、'.join(high) or '—'} |")
    body = f"""# 组织编制

共 **{len(S.AGENT_SPEC)} 人** / 8 类岗位：老板 1 + 管理层 3 + 三项目小组 3×3 + 中台 4。
每人一个独立 Hermes profile（`/opt/sefs/hermes/profiles/<agent_id>`），配置、人设、技能与记忆互不共享。
数据源：[员工名册](员工名册.md)（服务启动时读取，改表即改编制）。

| # | Agent ID | 姓名 | 岗位 | 绑定项目 | 人设调性 | Hermes profile | profile 内技能类 | 工具白名单 | 高危工具（需审批） |
|---|---|---|---|---|---|---|---|---|---|
{chr(10).join(rows)}

## 结构说明
- **小前台**：文人莫克 / 狗大人 / 同心圆各配 `PM + 文案 + 视频`，每人只绑定一个 `project_id`。
- **视频制片技能**：`openmontage` 归 `vd_*` 执行（素材 / 剪辑 / 合成，terminal 限 `/opt/sefs/OpenMontage`）；`cw_*` 只保留前置四阶段（调研 / 提案 / 脚本 / 分镜）文字工件，无 terminal。
- **共享中台**：`scout`（热点采集与素材）、`reviewer`（最终审核，有驳回权）、`release_ops`（发布与监控）、`kb_manager`（知识库）。
- **管理层**：`ceo` 统筹与拍板，`cto` 负责技术与同心圆架构，`cco` 负责三项目创意取舍。

## 指挥链
1. **老板**（蒿瑟莫）直接命令为最高优先级，覆盖任何管理层与项目组指令。
2. **管理层**（ceo / cto / cco）可直接向其他员工下发指令，跨项目，须留痕到 trace。
3. **项目组**：产品经理只对本项目组下指令；文案 / 视频只接受本项目 PM 与管理层指令。
4. **中台**：在各自职能域内对全公司负责；审核员有一票驳回权。
5. 冲突裁决：老板 > 管理层 > 项目组；管理层内部 CEO 拍板，技术以 CTO 为准，创意与调性以 CCO 为准。

完整职责与每日流程见 [岗位职责](岗位职责.md)。

## 隔离原则
每个员工绑定唯一项目，风格库与素材库按项目隔离；`reviewer` 强制做跨项目人设比对，检测到串味直接驳回。

相关：[[岗位职责]]｜[[项目与调性]]｜[[内核与编排]]
"""
    return write("公司/组织编制.md", "组织编制", ["opc", "公司配置", "编制"], body)


def page_projects():
    rows = []
    for pid, p in S.PROJECTS.items():
        kpi = "、".join(f"{k}={v}" for k, v in p["kpi"].items())
        rows.append(f"| {p['name']} | {p['kind']} | {S.TONE[pid]} | {pid} | {kpi} |")
    body = f"""# 项目与调性

> 自动生成视图，请勿手改；数据源是 [项目配置](项目配置.md)（服务启动时读取）。

| 项目 | 类型 | 人设调性 | project_id | KPI 字段 |
|---|---|---|---|---|
{chr(10).join(rows)}

## 为什么调性要单独列
三个项目共用同一支 AI 文案/视频池时，最容易出的问题是**把 A 账号的语气串到 B 账号**。
所以调性不是文案偏好，而是一条硬约束：`reviewer` 会做跨项目比对，串味即驳回。

## 流水线阶段
`{' → '.join(S.STAGES)}`

- 自媒体线（文人莫克 / 狗大人）：热点 → 选题 → 文案 → 视频 → 审核 → 审批 → 发布 → 数据 → 归档
- 数字分身线（同心圆）：需求 → CTO 技术方案 → 产品文案 + 演示视频 → 合规审核 → 审批（最高级）→ 上线监控 → 归档

相关：[[组织编制]]｜[[审批与报告]]
"""
    return write("公司/项目与调性.md", "项目与调性", ["opc", "公司配置", "项目"], body)


def page_kernel():
    body = f"""# 内核与编排

## Hermes 内核参数（运行时生效值）
| 参数 | 值 | 说明 |
|---|---|---|
| 引擎模式 | `{S.MODE}` | `hermes`=真实模型调用；`rule`=纯规则降级 |
| 模型 | `{S.MODEL}` | provider `opencode-go` |
| CLI 入口 | `{S.CLI}` | 自带 DNS 命名空间的包装脚本（`-z` 一次性会话） |
| 并发上限 | {S.WORKERS} | 同一波次最多并行 {S.WORKERS} 个模型调用 |
| 单次超时 | {S.TIMEOUT}s | 超时记一次失败并降级 |
| 输出约束 | 只输出 JSON | 解析失败自动重试 1 次 |

## DAG 编排（制度口径：3 → 9 → 9 → 6 → 2）
1. 3 名 PM 各提交本项目 **3 条**选题 → 发 CCO、CTO（同时 `scout` 出热点简报）
2. CCO 汇总 **9 条**创意，叠加 CEO/CTO/老板指定选题与自选，选出 **9 条（每项目 3 条）** → 抄送 CEO、CTO
3. CEO 召集选题评审会（CEO / CTO / CCO + 涉及项目 PM）→ 定 **6 条（每项目 2 条）**，出会议纪要，邮件群发并抄送老板
4. PM 组织文案 + 分镜 + 时长 + 配音/TTS 方案 → 《制作评估报告》 → 向视频制作派单；素材缺失报 `scout`
5. 样片由 **CCO / CTO / PM 三方评审** → 通过后精剪
6. `reviewer` 最终审核；驳回返工再复审，两轮不过转 `blocked`
7. **ApprovalGate**（发布/上线/对外承诺）
8. `release_ops` 发布 → `kb_manager` 归档 → CEO 日报 / CTO 技术周报 / 项目周报并行

> `prototype/server.py` 已实现 3→9→6、CCO 节点、制作评估、素材工单、样片三方评审与精剪后终审；制度以 [岗位职责](岗位职责.md) 为准。

## 消息协议
类型化消息：{', '.join(f'`{t}`' for t in MSG_TYPES)}；每条带 `traceId / sender / recipient / priority`。

## 三条安全红线
| 红线 | 机制 |
|---|---|
| 不自动发布 / 上线 / 对外承诺 / 花钱 | 高危工具默认禁用：{'、'.join(f'`{t}`' for t in S.HIGH_RISK_TOOLS)}，只能由审批门放行并留痕 |
| 人设不串味 | `project_id` 绑定 + 风格库隔离 + reviewer 跨项目比对 |
| 知识资产不随会话消失 | 每条产出由 `kb_manager` 归档并经 trace 回溯 |

相关：[[运行时快照]]｜[[事件流]]
"""
    return write("公司/内核与编排.md", "内核与编排", ["opc", "公司配置", "内核"], body)


# ---------------------------------------------------------------- 动态页

def msg_brief(m):
    p = m.get("payload") or {}
    if p.get("text"):
        return p["text"]
    if p.get("task"):
        return p["task"]
    if isinstance(p.get("result"), dict) and p["result"].get("summary"):
        return p["result"]["summary"]
    if isinstance(p.get("verdict"), dict):
        v = p["verdict"]
        return ("通过" if v.get("pass") else "驳回") + "：" + "；".join(v.get("reasons") or [])[:80]
    if isinstance(p.get("vote"), dict):
        return f"投票 {p['vote'].get('stance')}（置信 {p['vote'].get('confidence')}）"
    if isinstance(p.get("report"), dict):
        return p["report"].get("title", "")
    if isinstance(p.get("hotspot"), dict):
        return p["hotspot"].get("title", "")
    if isinstance(p.get("approval"), dict):
        return p["approval"].get("title", "")
    return ""


def page_snapshot(st, trace_ids):
    if not st:
        body = "# 运行时快照\n\n> [!warning] 服务未运行\n> 启动服务后重新执行 `./opc.sh vault`。\n"
        return write("动态/运行时快照.md", "运行时快照", ["opc", "动态"], body)

    stats = st.get("stats", {})
    h = stats.get("hermes", {})
    meeting = st.get("meeting", {})
    agent_rows = [f"| {a['id']} | {a['name']} | {a['role']} | {a['status']} | {cell(a.get('task') or '—')} |"
                  for a in st.get("agents", [])]
    pipe = st.get("pipeline", [])[-12:]
    pipe_rows = [f"| {cell(c.get('title', ''))} | {S.PROJECTS.get(c['projectId'], {}).get('name', c['projectId'])} | {c['stage']} |"
                 for c in pipe]
    traces = "、".join(f"`{t}`" for t in trace_ids) or "—"
    started = st.get("serverStartedAt") or "—"

    # 磁盘交付不随重启清零：列出 OPC/交付物 下的 trace 目录，和内存态计数并列展示
    disk_rows = []
    base = os.path.join(VAULT, "OPC", "交付物")
    try:
        names = [n for n in os.listdir(base) if n.startswith("tr_")]
        names.sort(key=lambda n: os.path.getmtime(os.path.join(base, n)), reverse=True)
    except OSError:
        names = []
    for n in names:
        p = os.path.join(base, n)
        files = sum(len(fs) for _, _, fs in os.walk(p))
        mt = datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")
        disk_rows.append(f"| `{n}` | {files} | {mt} |")

    # 磁盘侧对账：知识库文件与各轮 00-index 里的审批记录（都不随重启清零）
    kb_dir = os.path.join(VAULT, "OPC", "知识库")
    try:
        kb_files = [n for n in os.listdir(kb_dir) if n.endswith(".md") and not n.startswith("00-")]
    except OSError:
        kb_files = []
    ok_n = rej_n = 0
    for n in names:
        try:
            txt = open(os.path.join(base, n, "00-index.md"), encoding="utf-8").read()
        except OSError:
            continue
        for ln in txt.splitlines():
            if "审批" in ln and "· approved ·" in ln:
                ok_n += 1
            if "审批" in ln and "· rejected ·" in ln:
                rej_n += 1

    body = f"""# 运行时快照

> [!info] 采样时间 {now()}
> 场景 `{st.get('scene')}`｜引擎 `Conring`｜服务启动 `{started}`
> 下方 trace / 事件 / 流水线是**内存态**，服务重启会归零；磁盘交付见文末「磁盘交付」。

> [!warning] 本页由 `./opc.sh vault` 自动整页重写，手工批注会被覆盖；需要长期留存的说明请另建备忘文件。

## 内核统计
| 指标 | 值 |
|---|---|
| 模型调用次数 | {h.get('calls', 0)} |
| 失败 / 降级 | {h.get('failures', 0)} |
| 平均单次时延 | {h.get('avgLatencyMs', 0)} ms |
| 模型 | `{h.get('model', S.MODEL)}` |

## 会议
- 活动：{meeting.get('active')}
- 标题：{meeting.get('title') or '—'}
- 参会：{'、'.join(meeting.get('participants') or []) or '—'}

## 员工状态（{len(st.get('agents', []))} 人）
| Agent | 姓名 | 岗位 | 状态 | 当前任务 |
|---|---|---|---|---|
{chr(10).join(agent_rows)}

## 流水线（最近 {len(pipe_rows)} 条）
| 内容 | 项目 | 阶段 |
|---|---|---|
{chr(10).join(pipe_rows) if pipe_rows else '| — | — | — |'}

## 计数
- 已发布内容：{stats.get('published', 0)}
- 被审批门拦截：{stats.get('blockedPublish', 0)}
- 累计 trace（自 {started} 起）：{stats.get('traces', 0)}｜事件（内存态）：{stats.get('events', 0)}
- 相关 trace：{traces}

## 磁盘交付（重启不清零）
| Trace | 文件数 | 最后写入 |
|---|---|---|
{chr(10).join(disk_rows) if disk_rows else '| — | — | — |'}

- 磁盘知识条目：{len(kb_files)}（`OPC/知识库`）
- 磁盘审批记录：approved {ok_n} 条 / rejected {rej_n} 条（来自各轮 00-index）
- 说明：上面的内存计数与这里的磁盘计数口径不同，不能相加比较；重启后内存归零属正常。

相关：[[事件流]]｜[[审批与报告]]
"""
    return write("动态/运行时快照.md", "运行时快照", ["opc", "动态", "快照"], body)


def page_events(traces, events=None):
    events = events or []
    if not traces and not events:
        body = ("# 事件流\n\n> [!info] 暂无事件（内存态）\n"
                "> 服务刚重启时事件流为空，属正常；历史交付请看 [[运行时快照]] 的「磁盘交付」与 OPC/交付物 目录。\n"
                "> 在 UI 里点「注入热点」或调用 `POST /api/hotspots/inject` 后重新导出即可看到新事件。\n")
        return write("动态/事件流.md", "事件流", ["opc", "动态", "事件"], body)

    blocks = []
    for tid, tr in traces:
        msgs = tr.get("messages", [])
        rows = [f"| {m.get('at', '')[11:19]} | `{m.get('type')}` | {m.get('sender')} → {m.get('recipient')} | "
                f"{cell(msg_brief(m))[:90]} |" for m in msgs]
        blocks.append(f"""## trace `{tid}`（{tr.get('meta', {}).get('status', '?')}，{len(msgs)} 条）

| 时间 | 类型 | 收发 | 摘要 |
|---|---|---|---|
{chr(10).join(rows)}
""")
    if not traces and events:
        rows = [f"| {m.get('at', '')[11:19]} | `{m.get('type')}` | {m.get('sender')} → {m.get('recipient')} | "
                f"{cell(msg_brief(m))[:90]} |" for m in events]
        blocks.append(f"""## 会话事件（无 trace，共 {len(events)} 条）

| 时间 | 类型 | 收发 | 摘要 |
|---|---|---|---|
{chr(10).join(rows)}
""")
    body = "# 事件流\n\n> 类型化消息按 trace 聚合，摘要取自 payload（summary / verdict / vote / text）。\n\n" + "\n".join(blocks)
    return write("动态/事件流.md", "事件流", ["opc", "动态", "事件"], body)


def page_approvals(st):
    if not st:
        return write("动态/审批与报告.md", "审批与报告", ["opc", "动态"],
                     "# 审批与报告\n\n> [!warning] 服务未运行\n")

    ap = st.get("approvals", [])
    ap_rows = [f"| `{a['id']}` | {cell(a.get('title', ''))} | {a.get('status')} | {a.get('risk')} | "
               f"{a.get('createdAt', '')[11:19]} | {a.get('decidedAt', '')[11:19] or '—'} | {len(a.get('items') or [])} |"
               for a in ap]
    rp = st.get("reports", [])
    rp_rows = [f"| {r.get('kind')} | {cell(r.get('title', ''))} | {cell(r.get('conclusion', ''))[:70]} | "
               f"`{r.get('traceId', '')}` | {r.get('engine', '')} |" for r in rp]

    body = f"""# 审批与报告

## 审批门记录（{len(ap)} 条）
| 审批 ID | 标题 | 状态 | 风险 | 创建 | 决策 | 内容数 |
|---|---|---|---|---|---|---|
{chr(10).join(ap_rows) if ap_rows else '| — | 暂无审批 | — | — | — | — | — |'}

> 审批通过后才会执行 `publish.execute`；未审批的发布次数应恒为 0（当前拦截计数：{st.get('stats', {}).get('blockedPublish', 0)}）。

## 报告（{len(rp)} 篇）
| 类型 | 标题 | 结论 | trace | 引擎 |
|---|---|---|---|---|
{chr(10).join(rp_rows) if rp_rows else '| — | 暂无报告（审批通过后生成） | — | — | — |'}

相关：[[运行时快照]]
"""
    return write("动态/审批与报告.md", "审批与报告", ["opc", "动态", "审批"], body)


def page_knowledge(st):
    if not st:
        return write("动态/知识与KPI.md", "知识与KPI", ["opc", "动态"],
                     "# 知识与 KPI\n\n> [!warning] 服务未运行\n")

    kb = st.get("knowledge", {})
    latest = kb.get("latest", [])
    kb_rows = [f"| {cell(k.get('title', ''))} | {cell(k.get('projectId', ''))} | {cell(k.get('body', ''))[:50]} | "
               f"`{k.get('sourceTrace', '')}` |" for k in latest]
    kpi_rows = []
    for p in st.get("projects", []):
        kpi = "、".join(f"{k}={v}" for k, v in p.get("kpi", {}).items())
        kpi_rows.append(f"| {p['name']} | {p['kind']} | {kpi} |")

    body = f"""# 知识与 KPI

## 知识库（共 {kb.get('count', 0)} 条，展示最近 {len(latest)} 条）
| 条目 | 项目 | 摘要 | 来源 trace |
|---|---|---|---|
{chr(10).join(kb_rows) if kb_rows else '| — | 暂无条目 | — | — |'}

## 三项目指标
| 项目 | 类型 | 当前 KPI |
|---|---|---|
{chr(10).join(kpi_rows)}

> 知识资产归属公司：每条选题/稿件/成片/复盘由 `kb_manager` 归档，日报须引用历史条目。
"""
    return write("动态/知识与KPI.md", "知识与KPI", ["opc", "动态", "知识库"], body)


def main():
    os.makedirs(VAULT, exist_ok=True)
    try:
        state = _req("/api/state")
    except Exception as exc:
        print(f"[warn] 读取 {BASE}/api/state 失败：{exc}")
        state = None

    trace_ids = []
    if state:
        for c in state.get("pipeline", []):
            if c.get("traceId") and c["traceId"] not in trace_ids:
                trace_ids.append(c["traceId"])
    trace_ids = trace_ids[-2:]
    traces = []
    for tid in trace_ids:
        try:
            traces.append((tid, _req("/api/trace/" + tid)))
        except Exception as exc:
            print(f"[warn] 读取 trace {tid} 失败：{exc}")

    paths = [
        page_home(), page_org(), page_projects(), page_kernel(),
        page_snapshot(state, trace_ids),
        page_events(traces, (state or {}).get("eventsList") or []),
        page_approvals(state), page_knowledge(state),
    ]
    print(f"已写入 {len(paths)} 个 Markdown 到 {VAULT}")
    for p in paths:
        print("  ", os.path.relpath(p, VAULT))


if __name__ == "__main__":
    main()
