#!/usr/bin/env python3
"""把 OPC 全员（含老板）的岗位/性格/记忆/职责/历史记录内化为五行三才画像。

只写两处：vault 的 OPC/知识库/*.md（kb_graph 召回真源）与 owner 库 knowledge 表。
与 vault_digest.write_entry 同口径：同 id 幂等覆盖，旧 md 先清再写；selfcheck 的
「vault md 数 == db kb_ 数」一致性因此不被破坏。

用法：
  python3 prototype/staff_profile.py            # 预演，不落盘
  python3 prototype/staff_profile.py --apply    # 正式内化
  python3 prototype/staff_profile.py --json     # 输出画像摘要 JSON
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("OPC_VAULT", "/mnt/sefs/conring/users/18911789286/obsidian"))
KB_DIR = VAULT / "OPC" / "知识库"
OWNER_DB = Path(os.environ.get("OPC_OWNER_DB", "/mnt/sefs/conring/users/18911789286/opc.db"))
PROFILES = Path(os.environ.get("OPC_PROFILES", "/opt/sefs/hermes/profiles"))
ROSTER = VAULT / "公司" / "员工名册.md"
DUTIES = VAULT / "公司" / "岗位职责.md"
TZ = timezone(timedelta(hours=8))
DAY = datetime.now(TZ).strftime("%Y-%m-%d")
NOW = datetime.now(TZ).isoformat(timespec="seconds")

WUXING = {
    "fire": ("火", "决策模型", "decision"),
    "water": ("水", "知识图谱", "graph"),
    "earth": ("土", "分析调研", "analysis"),
    "metal": ("金", "技能能力", "skill"),
    "wood": ("木", "反馈迭代", "reflection"),
}
SANCAI = {"tian": "天·取势", "di": "地·载物", "ren": "人·成事"}

# 人工判读层：岗位事实取自名册/职责/profile，五行三才与性格判读在此单一口径维护。
META = {
    "boss": {
        "name": "蒿色莫", "element": "fire", "sancai": "tian", "secondary": "木·人 / 土·地",
        "personality": ["结论先行", "证据洁癖", "强边界授权", "敢砍任务敢叫停", "反形式主义", "催办有节奏"],
        "thinking": ["先三才后五行", "五行定性质三才定所处", "先止血后归因",
                     "最小可用再铺开", "取证式四档", "按区授权", "复盘→规则→用例→验证"],
        "skills_focus": ["战略拍板与门禁审批", "制度与流程设计", "五行三才知识治理",
                         "成本/素材/合规三检", "技术架构判断", "IT台账与自愈体系", "产能调度"],
        "advice": "材料齐套前置；保留 10 分钟超时+CEO 代批+秘书催办；派单用「结论+边界+验收口径」三段式。",
        "risk": "审批瓶颈是材料不齐而非无人批；一人一号使留痕主体不可辨；直接命令过多会打断产线节奏。",
    },
    "secretary": {
        "name": "小秘书", "element": "metal", "sancai": "ren", "secondary": "火·天",
        "personality": ["老板视角", "结论先行", "克制分寸", "盯人盯事盯账", "不越权", "双重人格"],
        "thinking": ["对内直给对外代言", "30 秒读完", "先结论后细节",
                     "拍板项给选项+建议+不决策后果", "异常有依据点到为止"],
        "skills_focus": ["browser_exec", "cron.write", "mail.read/send", "kb.read/search", "report.render", "语音桥"],
        "advice": "继续做老板影子与催办中枢；所有需要拍板的事项持续催到有明确结论。",
        "risk": "不得代拍板、不改配置、不对外承诺；双重人格边界与语音链路依赖需守住。",
    },
    "cco": {
        "name": "沈青禾", "element": "fire", "sancai": "tian", "secondary": "土·人",
        "personality": ["调性强势", "议题驱动", "一锤定音", "反对凑数", "敢自批研究提案", "用知识库说话"],
        "thinking": ["知识库→定稿→派单", "一议题一项目一产品", "近 30 天高复用优先",
                     "调性判唯一归属", "灵感直提例外严控"],
        "skills_focus": ["style.guide/write", "trend.analyze", "audience.insight",
                         "kb.audit/retire", "web.research", "init_project 与派单"],
        "advice": "把「串味」判据写成可引用条目；每周 3 条灵感与最多 1 条直提留痕。",
        "risk": "选题口径高度集中；直提通道若失控会绕过 knowledgeRefs 约束。",
    },
    "ceo": {
        "name": "陆清安", "element": "fire", "sancai": "tian", "secondary": "木·人",
        "personality": ["大盘裁决", "留痕", "产能硬指标", "不直接改稿", "超时才代批", "置信度门槛"],
        "thinking": ["OKR 与资源调度", "缺口从大到小", "不可测不达标", "P0/P1 裁决",
                     "技术听 CTO 创意听 CCO"],
        "skills_focus": ["strategy.plan", "okr.track", "risk.assess", "capacity.plan",
                         "approval.proxy", "report.render"],
        "advice": "派单说明必须带产能缺口；代批只在材料齐、置信度≥0.6 时执行。",
        "risk": "代批材料不齐易被驳回；日报若只堆数字会变成形式主义。",
    },
    "cto": {
        "name": "扶频", "element": "water", "sancai": "di", "secondary": "金·人 / 木·人",
        "personality": ["结构优先", "依赖向内", "取证", "反大重构", "按区授权", "成本安全敏感"],
        "thinking": ["五行洋葱架构", "先查证据再改", "AST 搬迁法", "六个必查", "验证四层", "发布回滚"],
        "skills_focus": ["arch.review", "security.audit", "perf.monitor", "cost.optimize",
                         "mcp/plugin.install", "opc-engineering"],
        "advice": "继续最小回归用例集与 knowledgeRefs 有效率月报；大重构边际收益低时明确停止。",
        "risk": "名册 21 个工具名是意图不是实现；ComfyUI 只有 CPU、/mnt/sefs 68% 是硬约束。",
    },
    "pm_wenren": {
        "name": "贺平生", "element": "earth", "sancai": "ren", "secondary": "火·人",
        "personality": ["书卷克制", "老年受众意识", "细节控", "按秒抠分镜", "小样节奏第一责任人"],
        "thinking": ["hook→setup→build→climax→landing", "逐镜画面/台词/秒齐全",
                     "先小样后成片", "素材缺口报苏寒"],
        "skills_focus": ["script.write", "scene_plan.write", "storyboard.write", "sample_reel", "backlog.write"],
        "advice": "继续把 50+ 受众的语速、字号、停留时间写进分镜硬指标。",
        "risk": "只管本项目；跨项目调人或套用素材会造成人设串味。",
    },
    "pm_gou": {
        "name": "米珂", "element": "earth", "sancai": "ren", "secondary": "火·人",
        "personality": ["节奏快", "梗感强", "狗大人调性", "善用复用资产", "敢砍不符"],
        "thinking": ["天狗大陆世界观优先", "复用既有 OpenMontage 工件", "157 ComfyUI 生成素材",
                     "小样先听看"],
        "skills_focus": ["script.write", "scene_plan.write", "storyboard.write", "sample_reel", "backlog.write"],
        "advice": "复用 tian-gou-power-ranking 的定稿/脚本/分镜时保留来源标记。",
        "risk": "素材库无犬类照片，动物题材必须走 157；画面禁嵌字。",
    },
    "pm_tongxinyuan": {
        "name": "姜月初", "element": "earth", "sancai": "ren", "secondary": "火·人",
        "personality": ["系统叙事", "技术隐喻", "克制表达", "隐私口径敏感"],
        "thinking": ["同心圆讲人机分工", "五行三才叙事化", "不落真实账号/LOGO", "样片先过"],
        "skills_focus": ["script.write", "scene_plan.write", "storyboard.write", "sample_reel", "backlog.write"],
        "advice": "把抽象机制翻成生活语言，但不牺牲准确性。",
        "risk": "隐私红线：一次性比对、结果不落库；禁把「可查」写成「查得准」。",
    },
    "cw_wenren": {
        "name": "陆川", "element": "metal", "sancai": "ren", "secondary": "土·地",
        "personality": ["温和慢速", "老年友好", "只用 A/B 级音色", "不拿能出声凑数"],
        "thinking": ["provider_text 不可改", "试音-实测-登记", "语速偏缓", "BGM 配合"],
        "skills_focus": ["media_tts", "media_asr", "subtitle.write", "音色/BGM 台账", "media_job_status"],
        "advice": "每周五前更新音色与 BGM 台账，重大变化当日上报。",
        "risk": "授权不明一律 D 级禁用；字数/读法偏差 >15% 必须回报 PM。",
    },
    "cw_gou": {
        "name": "叶秋", "element": "metal", "sancai": "ren", "secondary": "土·地",
        "personality": ["活泼", "节奏感", "狗大人声音辨识度", "零食袋音效敏感"],
        "thinking": ["按分镜秒数反推读法", "试音先于全量", "周五周报"],
        "skills_focus": ["media_tts", "media_asr", "subtitle.write", "音色/BGM 台账", "media_job_status"],
        "advice": "与米珂的小样节奏绑定，先定读法再全量合成。",
        "risk": "不得跨项目借音色；未实测不入台账。",
    },
    "cw_tongxinyuan": {
        "name": "江佑泽", "element": "metal", "sancai": "ren", "secondary": "土·地",
        "personality": ["冷静", "解释型", "技术概念口语化", "隐私克制"],
        "thinking": ["长句拆短", "数字/专名读法先定", "同心圆口播不露真实信息"],
        "skills_focus": ["media_tts", "media_asr", "subtitle.write", "音色/BGM 台账", "media_job_status"],
        "advice": "技术词先做朗读转换表，再进 TTS。",
        "risk": "读法错误会直接毁掉解释型内容，不能只靠默认发音。",
    },
    "vd_wenren": {
        "name": "陆离", "element": "metal", "sancai": "ren", "secondary": "土·地",
        "personality": ["画面克制", "字号偏大", "信息不过密", "写实优先", "拒绝糊图"],
        "thinking": ["按通道选图", "asset_manifest 唯一汇总", "先小样后定版", "同镜最多 2 次生成"],
        "skills_focus": ["image.gen", "video.render", "cover.gen", "sample_reel", "asset_manifest"],
        "advice": "精确文字走 text_card/SVG，写实走 sd15；不达标换路线或报 PM 改分镜。",
        "risk": "ComfyUI 只有 CPU，关键帧默认合成卡；单张扩散 >180s 不可接受。",
    },
    "vd_gou": {
        "name": "萧执", "element": "metal", "sancai": "ren", "secondary": "土·地",
        "personality": ["动感", "梗画面", "犬类生成经验", "快而不糊"],
        "thinking": ["157 ComfyUI 生成犬类图", "9:16 禁嵌字", "关键帧+对白+BGM 按时间码对齐"],
        "skills_focus": ["image.gen", "video.render", "cover.gen", "sample_reel", "asset_manifest"],
        "advice": "复用项目素材时用 sha256 判版本，不按文件名推断。",
        "risk": "素材缺口经 PM 报苏寒；成片多处落盘需核对版本。",
    },
    "vd_tongxinyuan": {
        "name": "崔聿棠", "element": "metal", "sancai": "ren", "secondary": "土·地",
        "personality": ["结构化视觉", "同心圆隐喻", "隐私干净", "稳定输出"],
        "thinking": ["程序化视觉与结构图优先", "不落 LOGO/域名", "小样帧声对齐"],
        "skills_focus": ["image.gen", "video.render", "cover.gen", "sample_reel", "asset_manifest"],
        "advice": "结构图用 mermaid/diagram 通道，避免扩散模型生成错误文字。",
        "risk": "版本命名易混，必须用 sha256 与 render_report 对齐。",
    },
    "release_ops": {
        "name": "顾景琛", "element": "metal", "sancai": "tian", "secondary": "火·天",
        "personality": ["门禁意识强", "只认审批回执", "发布克制", "异常即升级"],
        "thinking": ["未批不发布", "发布回执+监控口径", "异常升级老板"],
        "skills_focus": ["publish.execute", "metric.read", "alert.raise", "kb.read"],
        "advice": "代批不等于放行未过审核门的内容，发布前复核四档结论。",
        "risk": "publish.execute 是高危动作；审批状态与审核结论必须同时成立。",
    },
    "reviewer": {
        "name": "顾凛川", "element": "wood", "sancai": "ren", "secondary": "火·天",
        "personality": ["取证式", "四档判定", "一票驳回", "只认清单", "不凭印象"],
        "thinking": ["先判对象性质", "工具输出绑定结论", "未验证分级", "同缺口两轮同档", "修订另存"],
        "skills_focus": ["review.check", "compliance.check", "final-reviewer", "ffprobe/ffmpeg", "evidence 留痕"],
        "advice": "把驳回理由按事实/结构/合规/技术四类结构化，补上木行信号库。",
        "risk": "人工复核类记 PENDING_HUMAN 而非 REJECT；ADVISORY 不作驳回理由。",
    },
    "scout": {
        "name": "苏寒", "element": "earth", "sancai": "tian", "secondary": "火·天",
        "personality": ["小时级采集", "链接为准", "不冒充当日数据", "Qwen TTS 必采"],
        "thinking": ["抓不到写失败原因", "来源 URL+时间+字段口径", "周报给配音", "不下载权重不实测"],
        "skills_focus": ["hotspot.fetch", "asset.fetch/tag", "playwright-local-control", "音色与 BGM 情报"],
        "advice": "外部情报只做采集与初判，实测归配音岗，部署归 CTO。",
        "risk": "JS 渲染需兜底；授权不明不转存；star/许可/日期以页面为准。",
    },
    "kb_manager": {
        "name": "祁南", "element": "water", "sancai": "di", "secondary": "土·地",
        "personality": ["索引洁癖", "只读盘点", "孤儿索引修复", "过时标归档不删"],
        "thinking": ["数字必须来自查询", "写清库名/表名/路径/统计时间",
                     "vault==db 一致性口径", "重复合并与退役标记"],
        "skills_focus": ["kb.index/qa/audit/retire", "asset.audit", "report.render", "cron.write"],
        "advice": "digest 重建抹掉评分标签后继续自愈补回；报告文件名避开「台账」红线。",
        "risk": "vault 屏蔽规则：登录凭据、隐藏段、台账/凭据/拓扑结尾、运维手册路径一律不收录。",
    },
    "manual_scout": {
        "name": "灰太狼", "element": "earth", "sancai": "tian", "secondary": "金·人",
        "personality": ["人类登录后接管", "边界清楚", "不碰验证码风控", "样本量口径"],
        "thinking": ["两段式登录→采集", "保留 URL/时间/截图", "写明 confidence", "不足即标缺口"],
        "skills_focus": ["browser.login", "browser.collect", "web.fetch", "file.write"],
        "advice": "登录指令写明平台、入口、方式与完成回执。",
        "risk": "不转存凭据、不执行系统命令、不采集工单外隐私数据。",
    },
}


def clip(text, n=140):
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:n] + ("…" if len(s) > n else "")


def read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def parse_roster():
    out = {}
    for ln in read(ROSTER).splitlines():
        if not ln.strip().startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 6 or cells[0] not in META:
            continue
        out[cells[0]] = {
            "id": cells[0], "name": cells[1], "role": cells[2], "project": cells[3],
            "scope": cells[4], "tools": [t for t in cells[5].split("、") if t],
        }
    return out


def parse_duties(name):
    lines = read(DUTIES).splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("### ") and name in ln:
            start = i
            break
    if start is None:
        return ""
    out = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.startswith("## ") or ln.startswith("### "):
            break
        out.append(ln)
    return "\n".join(out).strip()


def cli_toolsets(aid):
    path = PROFILES / aid / "config.yaml"
    out, in_cli = [], False
    for ln in read(path).splitlines():
        if re.match(r"^platform_toolsets:\s*$", ln):
            continue
        if re.match(r"^\s{2}cli:\s*$", ln):
            in_cli = True
            continue
        if in_cli:
            m = re.match(r"^\s+-\s+(\S+)", ln)
            if m:
                out.append(m.group(1))
                continue
            # 同级键（两空格缩进的键名）出现即离开 cli 段；四空格是列表项缩进，不 break
            if ln.strip() and re.match(r"^\s{2}[A-Za-z_]", ln):
                break
    return out


def profile_facts(aid):
    pdir = PROFILES / aid
    soul = read(pdir / "SOUL.md")
    mem = read(pdir / "memories" / "MEMORY.md")
    user = read(pdir / "memories" / "USER.md")
    persona_lines = []
    lines = soul.splitlines()
    heads = [i for i, ln in enumerate(lines)
             if re.match(r"^##\s+", ln) and re.search(r"风格|性格|原则|纪律|行为|口吻|汇报|表达|判断", ln)]
    if heads:
        i = heads[0]
        persona_lines = [lines[i]]
        for ln in lines[i + 1:]:
            if ln.startswith("## "):
                break
            if ln.strip():
                persona_lines.append(ln)
    else:
        persona_lines = [ln for ln in lines[:28] if ln.strip()]
    bits = []
    for chunk in re.split(r"\n\s*§\s*\n|\n---\n", mem):
        for ln in chunk.splitlines():
            if ln.strip() and (ln.strip().startswith("-") or len(ln.strip()) > 12):
                bits.append(clip(ln.strip().lstrip("- "), 110))
                break
        if len(bits) >= 4:
            break
    if not bits:
        bits = [clip(x, 110) for x in user.splitlines() if x.strip()][:3]
    skills = [str(p.relative_to(pdir / "skills"))[:-len("/SKILL.md")]
              for p in sorted((pdir / "skills").rglob("SKILL.md"))] if (pdir / "skills").is_dir() else []
    return {
        "soul": clip("\n".join(persona_lines), 700),
        "memory": bits,
        "toolsets": cli_toolsets(aid),
        "skills": skills,
        "has_profile": bool(soul),
        "record": pdir.exists(),
    }


def db_stats(aid, name):
    empty = {"messages": 0, "messages_last": "", "messages_sample": [],
             "chat": 0, "chat_last": "", "chat_sample": [], "events": 0}
    try:
        con = sqlite3.connect(f"file:{OWNER_DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return empty
    try:
        sender = "owner" if aid == "boss" else aid
        row = con.execute("select count(*),max(at) from messages where sender=?", (sender,)).fetchone()
        empty["messages"], empty["messages_last"] = row[0] or 0, row[1] or ""
        for r in con.execute("select payload from messages where sender=? order by at desc limit 2", (sender,)):
            empty["messages_sample"].append(clip(r[0], 130))
        if aid == "boss":
            cond, args = "sender_name='老板' or sender_id in ('boss','owner','18911789286')", ()
        else:
            cond, args = "sender_name=? or sender_id=?", (name, aid)
        row = con.execute(f"select count(*),max(created_at) from chat_messages where {cond}", args).fetchone()
        empty["chat"], empty["chat_last"] = row[0] or 0, row[1] or ""
        for r in con.execute(f"select content from chat_messages where {cond} order by created_at desc limit 2", args):
            empty["chat_sample"].append(clip(r[0], 130))
        empty["events"] = con.execute(
            "select count(*) from trace_events where data like ? or data like ?",
            (f"%{aid}%", f"%{name}%")).fetchone()[0] or 0
        if aid == "boss":
            empty["directives"] = con.execute("select count(*) from directives").fetchone()[0] or 0
            rows = con.execute("select status,count(*) from approvals group by status").fetchall()
            empty["approvals"] = {k: v for k, v in rows}
            empty["approvals_total"] = sum(empty["approvals"].values())
            empty["directive_sample"] = [clip(r[0], 150) for r in con.execute(
                "select text from directives order by created_at desc limit 3")]
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return empty


def wuxing_of(key):
    label, meaning, category = WUXING[key]
    return f"{label}行·{meaning}", category


def build_entry(aid, meta, row, facts, hist):
    wlabel, category = wuxing_of(meta["element"])
    san = SANCAI[meta["sancai"]]
    display_role = row["role"]
    if row["name"] in display_role and "（" in display_role:
        display_role = display_role.replace(f"{row['name']}（", "").rstrip("）") or display_role
    tools = facts["toolsets"] or row["tools"]
    skills = facts["skills"]
    tags = ["员工画像", "五行三才", "性格画像", "思维模式",
            wlabel, san, row["project"], meta["name"], aid]
    if aid == "boss":
        tags.append("老板")
    summary = (f"{row['name']}（{display_role}·{row['project']}）：{wlabel}／{san}，"
               f"性格关键词 {'、'.join(meta['personality'][:3])}；"
               f"核心技能 {'、'.join(meta['skills_focus'][:3])}；"
               f"历史消息 {hist['messages'] + hist['chat']} 条。")
    lines = [
        "---",
        f"kb_id: kb_staff_{aid}",
        f"title: OPC员工画像·{row['name']}（{row['role']}）——{wlabel}／{san}",
        f"project: {row['project']}",
        f"category: {category}",
        f"sancai: {meta['sancai']}",
        f"wuxing: {meta['element']}",
        f"employee_id: {aid}",
        f"role: {row['role']}",
        f"tags: {'、'.join(tags)}",
        "trace:",
        f"archived: {NOW}",
        "---", "",
        f"# OPC 员工画像 · {row['name']}", "",
        f"> 岗位事实源：`公司/员工名册.md` + `公司/岗位职责.md`；"
        f"性格与记忆源：`/opt/sefs/hermes/profiles/{aid}/`"
        + ("（老板为真实档案 OPC-000，只记录不派活；日常由小秘书代行）。"
           if aid == "boss" else "。"),
        "",
        "## 一、基础身份", "",
        f"- 工号：`{aid}`｜姓名：{row['name']}｜岗位：{row['role']}｜项目：{row['project']}",
        f"- 负责范围：{row['scope']}",
        f"- 工具白名单（名册意图）：{'、'.join(row['tools']) or '—'}",
        "",
        "## 二、五行三才归属", "",
        f"- 主位：**{wlabel} ／ {san}**；辅位：{meta['secondary']}",
        f"- 判据：{wlabel.split('·')[1]}定「性质」，{san}定「所处」；"
        f"本岗以{'对外取势与拍板' if meta['sancai']=='tian' else '事实底座与沉淀' if meta['sancai']=='di' else '人的加工与成事'}为主。",
        "",
        "## 三、性格画像", "",
        "- " + "、".join(meta["personality"]),
        f"- 制度/SOUL 佐证：{facts['soul'] or '无独立 profile（老板为人工审批角色）'}",
        "",
        "## 四、思维模式", "",
        "- " + " → ".join(meta["thinking"]),
        "",
        "## 五、技能画像", "",
        f"- 核心能力：{'、'.join(meta['skills_focus'])}",
        f"- 运行时工具集（{len(tools)}）：{'、'.join(tools[:16]) or '—'}",
        f"- 已登记技能（{len(skills)}）：{'、'.join(skills[:10]) or '—'}",
        "",
        "## 六、记忆锚点", "",
    ]
    if facts["memory"]:
        lines += [f"- {b}" for b in facts["memory"]]
    else:
        lines += ["- 无独立长期记忆文件；以岗位职责与历史记录为准。"]
    lines += ["", "## 七、职责摘要（制度口径）", "", clip(parse_duties(row["name"]), 1600) or "—", "",
              "## 八、历史记录（owner 库实测）", "",
              f"- 站内消息：{hist['messages']} 条，最近 {hist['messages_last'] or '—'}",
              f"- 统一聊天：{hist['chat']} 条，最近 {hist['chat_last'] or '—'}",
              f"- 事件提及：{hist['events']} 条"]
    if aid == "boss":
        appr = hist.get("approvals", {})
        lines += [
            f"- 老板原话指令：{hist.get('directives', 0)} 条；审批单：{hist.get('approvals_total', 0)} 张"
            f"（{'、'.join(f'{k} {v}' for k,v in sorted(appr.items()))}）",
            "- 原话样本：" + ("；".join(hist.get("directive_sample", [])) or "—"),
        ]
    for s in hist["messages_sample"] + hist["chat_sample"]:
        lines.append(f"- 记录样本：{s}")
    lines += ["", "## 九、使用建议与风险边界", "",
              f"- 建议：{meta['advice']}", f"- 风险：{meta['risk']}", "",
              "## 十、证据路径", "",
              "- `公司/员工名册.md`、`公司/岗位职责.md`",
              (f"- `/opt/sefs/hermes/profiles/{aid}/SOUL.md`、`memories/USER.md`、`memories/MEMORY.md`、"
               "`config.yaml`、`skills/opc/`（真实档案 OPC-000，只记录不派活）。"
               if aid == "boss" else
               f"- `/opt/sefs/hermes/profiles/{aid}/SOUL.md`、`memories/MEMORY.md`、`config.yaml`"),
              "- `/mnt/sefs/conring/users/18911789286/opc.db`（messages / chat_messages / trace_events / approvals / directives）",
              "- `OPC/知识库/2026-11-16-同心圆五行三才内化-管理层运行手册-kb_wuxing_sancai_ops.md`",
              ""]
    return {
        "id": f"kb_staff_{aid}",
        "title": f"OPC员工画像·{row['name']}（{display_role}）——{wlabel}／{san}",
        "path": "",
        "category": category, "sancai": meta["sancai"], "tags": tags,
        "summary": summary, "body": "\n".join(lines), "project": row["project"],
    }


def build_boss_extra():
    return """## 老板专项更新（性格 / 技能 / 思维模式）

### 性格（综合判定）
结论先行、证据洁癖、强边界授权、敢砍任务敢叫停、反形式主义、催办有节奏。
对「无数据源却报达标」零容忍；对已经跑偏的流水线先叫停再复盘，不为沉没成本续命。
对外克制分寸，对内直给态度；小事不推回给自己，大事必须回到他手上拍板。

### 技能（综合判定）
1. 战略拍板与门禁审批：production.start / publish.execute / deploy.prod / ad.spend 唯一放行口。
2. 制度与流程设计：RACI、审批门、简化小样、产能硬指标、10 分钟超时与 CEO 代批授权。
3. 五行三才知识治理：五行定性质、三才定所处；把资产地图变成可查字段与台账。
4. 成本 / 素材 / 合规三检：材料不齐就不批，审批门瓶颈在材料而非无人批。
5. 技术架构判断：C1 拆分、依赖向内、按区授权、验证四层、发布回滚；能识别「名册工具名≠实现」。
6. IT 台账与自愈体系：巡检、ports 白名单、runbook、UI 基线、日志治理。
7. 内容调性与产能调度：一议题一项目一产品；每组成品产出不少于 2。

### 思维模式（综合判定）
- **双维归因**：先判输入 / 处理 / 输出（三才），再进五行圈层；不跳阶段全量排查。
- **取证式判断**：无数据源不上报，宁可交白卷不造假；结论必须绑定路径、命令输出或库表。
- **先止血后归因**：P0 先回滚，复盘后置；同一缺口两轮同档才变档。
- **最小可用再铺开**：反对一次性铺满，先跑最小闭环，再按月补缺口。
- **按区授权**：知识区 .md、研发区代码+验证命令+restart、hermes 子命令、既有运维授权；宿主配置与生产库禁改。
- **红队思维**：不可测即不达标；fail-closed 优于静默放行；外部案例无出处即禁用。
- **闭环复活**：复盘 → 改规则 → 补回归用例 → 再验证，禁止「修完靠人记得」。

### 综合性整体判断
老板是这套一人公司的**系统设计师 + 最终仲裁人**，不是日常执行者。
他的价值集中在三件事：定口径、卡门禁、砍跑偏；其余应通过 CEO（裁决与代批）、
小秘书（催办与汇总）、CTO/CCO（技术与创意口径）分流量。
当前真实瓶颈是**审批材料不齐**（成本/素材/合规三项与产能缺口未随单上送），
不是审批意愿不足；因此 10 分钟超时 + CEO 有条件代批 + 小秘书即时催办应保留，
同时把「材料齐套校验」前移到 PM/CCO 出单环节。
主要风险：一人一号导致审批留痕主体不可辨；直接命令过多会绕过编制节奏；
知识库与台账的双写一致性依赖纪律而非机制。建议继续用「结论 + 边界 + 验收口径」
三段式派单，并把所有拍板理由落到 knowledgeRefs 与审批单 raw 字段。

### Profile 补齐状态（2026-11-17）
老板已建立真实档案：`/opt/sefs/hermes/profiles/boss/`，含 `SOUL.md`、
`memories/USER.md`、`memories/MEMORY.md`、`config.yaml` 与 10 张技能卡
（原六张 `boss-governance` / `five-elements-sancai` / `evidence-gate` / `dispatch-protocol` /
`decision-model` / `feedback-loop`，新增 `risk-assessment` / `heuristics` / `domain-model` / `strategic-insight`）。
档案只用于人格、口径、记忆与技能召回，不进入自动派单；config 不持有模型凭据。
技能明细见 `kb_staff_boss_skills`；五行三才归位见 `kb_staff_boss_wuxing_sancai`；
自我迭代与决策四件见 `kb_staff_boss_iteration`。
"""


BOSS_SKILL_MAP = {
    "boss-governance": ("金行·技能能力／天·取势", "定口径、卡门禁、砍跑偏"),
    "five-elements-sancai": ("水行·知识图谱／地·载物", "双维归因与组织能力盘点"),
    "evidence-gate": ("火行·决策模型／天·取势", "材料齐套与取证式门禁"),
    "dispatch-protocol": ("木行·反馈迭代／人·成事", "三段式派单与按区授权"),
    "decision-model": ("火行·决策模型／天·取势", "三类范式与决策四要素"),
    "feedback-loop": ("木行·反馈迭代／人·成事", "信号→归因→改规则→补用例→再验证"),
    "risk-assessment": ("火行·决策模型／天·取势", "八类风险与可逆性定级"),
    "heuristics": ("土行·分析调研／地·载物", "判断/执行/治理/迭代四类快判"),
    "domain-model": ("水行·知识图谱／地·载物", "五层边界、十类实体与不变量"),
    "strategic-insight": ("火行·决策模型／天·取势", "往哪走/不做什么/下一刀砍哪里"),
}


def build_boss_skills_entry(facts):
    """把老板生产 profile 里的技能卡内化为一条可召回的技能资产。"""
    root = PROFILES / "boss" / "skills"
    rows = []
    for rel in facts["skills"]:
        name = rel.split("/")[-1]
        text = read(root / rel / "SKILL.md")
        m = re.search(r"^description:\s*(.+)$", text, re.M)
        desc = clip(m.group(1) if m else name, 120)
        seat, role = BOSS_SKILL_MAP.get(name, ("金行·技能能力／人·成事", "老板治理辅助"))
        rows.append(f"| {name} | `{rel}/SKILL.md` | {seat} | {role} | {desc} |")
    body = f"""---
kb_id: kb_staff_boss_skills
title: 老板技能内化·OPC-000 十张治理技能卡
project: 全公司
category: skill
sancai: ren
tags: 老板技能、技能内化、五行三才、性格画像、思维模式、老板
trace:
archived: {NOW}
---

# 老板技能内化 · OPC-000

> 老板技能从「人格判读」升级为「可召回、可复用、可校验」的技能资产。
> 真源：`/opt/sefs/hermes/profiles/boss/skills/`；本条是检索摘要，不替代原文。

## 一、技能卡清单

| 技能 | 路径 | 五行三才主位 | 职能 | 说明 |
|---|---|---|---|---|
{chr(10).join(rows) if rows else "| — | — | — | — | 无技能卡 |"}

## 二、能力矩阵

- **火·天（决策）**：`decision-model` 管三类范式与四要素；`risk-assessment` 管八类风险与可逆性；`evidence-gate` 管材料齐套；`strategic-insight` 管方向排序；`boss-governance` 管最终裁决与止损。
- **水·地（图谱）**：`five-elements-sancai` 定位行×才节点；`domain-model` 给出五层边界、十类实体与不变量。
- **土·地（事实底座）**：`heuristics` 把反复验证的判断压成可调用快判，带反例。
- **木·人（反馈）**：`feedback-loop` 管信号→归因→改规则→补用例→再验证；`dispatch-protocol` 把结论转成边界与验收口径。
- **金（执行口径）**：十张卡共同约束老板只定口径不代执行，日常展开交 CEO / 小秘书 / CTO / CCO。

## 三、使用方式

1. 拍板前：`risk-assessment` 定级 → `decision-model` 选范式写四要素 → `evidence-gate` 查五项齐套。
2. 问题定位：`five-elements-sancai` 定阶段与圈层 → `domain-model` 查不变量与影响面。
3. 快判与带教：调用 `heuristics` 对应法则，先检查反例是否命中。
4. 事故/驳回收口：`feedback-loop` 六步闭环，必须补回归用例才算修复。
5. 派单/方向：`dispatch-protocol` 写三段式；`strategic-insight` 回答做什么、不做什么、下一刀砍哪里。
6. 冲突或跑偏：`boss-governance` 裁决顺序、叫停与复盘，驳回与超时分记。

## 四、红线

- 不进入自动派单；config.yaml 不持有模型凭据。
- 不越过审核四档与发布门禁；老板发起也要留 research / proposal 痕迹。
- 不直接改宿主配置、不写生产库业务数据、不读抄凭据文件。
- 无数据源不上报；不可测写不可测；外部案例无出处不引用。

## 五、证据路径

- `/opt/sefs/hermes/profiles/boss/SOUL.md`
- `/opt/sefs/hermes/profiles/boss/memories/USER.md`
- `/opt/sefs/hermes/profiles/boss/memories/MEMORY.md`
- `/opt/sefs/hermes/profiles/boss/config.yaml`
- `/opt/sefs/hermes/profiles/boss/skills/opc/*/SKILL.md`
"""
    return {
        "id": "kb_staff_boss_skills", "title": body.splitlines()[2][len("title: "):],
        "path": "", "category": "skill", "sancai": "ren",
        "tags": ["老板技能", "技能内化", "五行三才", "性格画像", "思维模式", "老板"],
        "summary": "老板 OPC-000 十张治理技能卡已内化：新增 risk-assessment / heuristics / domain-model / strategic-insight，与原六张共同覆盖自我迭代与决策全链路。",
        "body": body, "project": "全公司",
    }


def build_boss_projection_entry():
    """投影反推法：员工是老板某一面的外化，代偿处即缺陷。"""
    body = f"""---
kb_id: kb_staff_boss_projection_review
title: 老板投影反推与缺陷评审·{DAY}
project: 全公司
category: reflection
sancai: ren
tags: 老板、投影分析、缺陷评审、评分、性格、能力、五行三才
trace:
archived: {NOW}
---

# 老板投影反推与缺陷评审（{DAY}）

> 结论先行：综合 **76/100**（性格侧 71、能力侧 80）。
> 员工不是「另一些人」，而是老板在不同维度上的投影：**放大投影**说明他有能力但选择放大，
> **代偿投影**说明他把不耐久、不愿停留或没机制化的部分外给了人。缺陷主要不在战略与判断，
> 而在**耐心、授权一致性、材料齐套前置、反馈闭环机制化**。

## 一、方法：投影反推法

1. 把每名员工看成老板的一种功能外化：他不必亲自做，但那一面一定在他身上存在过。
2. 区分两类投影：
   - **能力放大**：老板具备该能力，交给员工是为了规模化（如 CEO 裁决、CTO 架构、CCO 调性）。
   - **短板代偿**：老板反复要求却没自己稳定做到，因而必须靠某个岗位补位（如小秘书催办、审核员取证、知识库记忆）。
3. 判断规则：**放大是杠杆，代偿是信号**。同一岗位若长期高频介入、老板又反复追问，说明这一面不是「交给别人更省」，而是「自己停不住」。

## 二、员工 → 老板 投影映射

| 投影面 | 对应员工 | 老板被投射的面向 | 类型 | 反推信号 |
|---|---|---|---|---|
| 日常展开与催办 | 小秘书 | 盯人盯事盯账、对外代言 | 代偿 | 老板多次「检查检查检查」「为什么回复这么慢」「通了没」 |
| 裁决与代批 | CEO 陆清安 | 大盘裁决、冲突拍板 | 放大 + 代偿 | 超时授权已立，但材料不齐导致代批也被驳回 |
| 结构与工程 | CTO 扶频 | 架构判断、依赖向内、验证四层 | 放大 | 老板能判方向，但具体实现与核验交给 CTO |
| 创意与调性 | CCO 沈青禾 | 选题定稿、一议题一项目一产品 | 放大 | 口径由老板定，日常定稿交 CCO |
| 拆解与节奏 | PM×3 | 把想法拆成分镜与秒表 | 放大 | 老板给方向，逐镜细化靠 PM |
| 声音表达 | 配音×3 | 语感、音色、停顿 | 代偿 | TTS 被定位为核心资产，说明老板对读法敏感但不亲自试音 |
| 画面实现 | 视频×3 | 镜头、构图、成片审美 | 放大 | 审美归老板，执行归视频岗 |
| 怀疑与取证 | 审核 顾凛川 | 一票驳回、只认证据 | **强代偿** | 老板自己审批常无理由字段，怀疑被外置给审核员 |
| 外部好奇 | 苏寒 / 灰太狼 | 热点、素材、外部触角 | 放大 | 采集交给别人，老板只看结论 |
| 记忆与秩序 | 祁南 | 归档、索引、一致性 | **代偿** | vault==db 靠纪律维持，说明老板自身记忆外置 |
| 门禁与回执 | 发布 顾景琛 | 未批不发布、异常升级 | 放大 + 代偿 | 一人一号使留痕主体不可辨，机制未补 |

## 三、性格缺陷

1. **耐心不足、即时反馈依赖（最强信号）**
   历史聊天里反复出现「检查检查检查」「为什么回复的这么慢」「是在处理哪些事情」「通了沒通了沒」。
   老板要的是即时可视的结果，对异步过程容忍低；这会让员工优先「先回一句」而不是「把事做完整」。

2. **直接干预强，容易绕过编制节奏**
   直接命令为最高优先级本身合理，但 2026-11-16 夜间连续下发多条自检/补录任务（23:04–23:14），
   把本应由小秘书汇总的巡检拆成逐条人工指令。放权写在制度里，收权发生在行为里。

3. **证据标准双低**
   要求员工「无数据源不上报、数字必须带路径」；但历史驳回单 `decided_by=蒿色莫` 多次无理由字段。
   对别人的判断要求可追溯，对自己的拍板留白——这是最容易被组织学会的双标。

4. **怀疑外置**
   取证、四档判定、一票驳回全压给审核员。老板自己更像「先看结果再问」，
   缺少在派单前就把风险问出来的习惯，导致问题后置到审核门。

5. **对内直给 / 对外克制的两极**
   对内直给效率高，但压缩了不同意见的出场空间；对外又过度克制，
   两者切换全靠本人临场判断，没有形成可复制的表达机制。

## 四、能力缺陷

1. **材料齐套校验放错了位置**
   审批门严格，但成本 / 素材 / 合规 / 产能缺口是在审批时才看，不是出单时强制。
   于是批量审批被代批引擎以「素材缺口 + 成本未回传」驳回。制度设计者把校验点后移了一环。

2. **留痕主体机制性缺陷长期未补**
   一人一号 + 反代同源，使 `decided_by=蒿色莫` 只能证明「有人持有老板登录态」。
   这个问题已被识别多日，但没转化为代理签名 / 复核字段，属于「看见但未机制化」。

3. **反馈闭环停在文化层**
   复盘 → 改规则 → 补回归用例 → 再验证的闭环写进了 SOUL 与技能卡，
   但木行信号库、驳回原因四类枚举、回归用例集仍未落地。老板有复盘习惯，缺复盘基础设施。

4. **环境认知依赖事故修正**
   Wan2.2 / Wan2.1 文档错写、旧版 ffmpeg 路径、ComfyUI 只有 CPU 等，都是踩坑后才内化。
   说明主动核验节奏不足，知识更新靠故障驱动。

5. **底座治理偏薄**
   `/mnt/sefs` 已用 68%、备份同盘、多份 `materials.json` 事实源混淆。
   台账建得很全，但物理层单点与同盘风险没被同等重视。

6. **自我复制过度**
   老板是唯一账号、唯一 sudo、唯一放行口；秘书、CEO 都只是代行。
   组织看似 19 人，关键路径仍收敛到一个人，这是规模化上限。

## 五、评分

| 维度 | 满分 | 得分 | 判据 |
|---|---:|---:|---|
| 战略判断与拍板 | 18 | 17 | 方向清晰、敢砍跑偏；批量终止需更证据化 |
| 制度与流程设计 | 15 | 11 | RACI/审批门/小样到位；材料齐套前置缺失 |
| 证据纪律 | 15 | 12 | 无数据源不上报；但自身批复无理由字段 |
| 授权与边界 | 12 | 8 | 按区授权已立；直接命令过多、一人一号未解 |
| 情绪与耐心 | 10 | 6 | 反复催问、即时反馈依赖、深夜连续自检 |
| 闭环与复盘机制 | 12 | 8 | 复盘文化强；信号库与回归用例未机制化 |
| 技术与环境核验 | 10 | 8 | 架构判断强；文档滞后靠事故修正 |
| 对外表达与角色一致 | 8 | 6 | 对外克制；客户话术体系未成 |
| **合计** | **100** | **76** | 性格侧 71 / 能力侧 80 |

**解读**：不是能力型短板，而是**机制型 + 情绪型短板**。
战略、制度框架、证据意识都在高位；把「知道」变成「不依赖人盯的机制」、把「直给」变成「可等待的节奏」，是提分最快的两处。

## 六、改进建议

| 优先级 | 动作 | 验收口径 |
|---|---|---|
| P0 | 审批单强制附成本/素材/合规/产能缺口，不齐不进老板队列 | 审批单齐套字段可查 |
| P0 | 老板批准/驳回必须写理由字段，向审核四档看齐 | `decidedBy` 不再孤零零出现 |
| P1 | 非 P0 直接命令延迟 15 分钟发出，先经小秘书汇总 | 夜间逐条自检任务减少 |
| P1 | 老板只看 30 秒看板，过程追问由小秘书代答 | 「为什么这么慢」类追问下降 |
| P1 | 建木行信号库 + 驳回四类枚举 + 回归用例集 | 同类缺口两轮同档可追 |
| P2 | 每月亲验一次环境事实（模型版本/ffmpeg/磁盘/端口） | 不再靠事故更新文档 |

## 七、证据与口径

- 员工投影来源：`kb_staff_*` 19 条画像、`kb_staff_boss_skills`。
- 行为证据：owner 库 `chat_messages`（老板 433 条）、`directives`（12 条）、`approvals`（44 张）。
- 制度证据：`公司/岗位职责.md`、`SOUL.md`、`USER.md`、`MEMORY.md`、五行三才运行手册。
- 评分口径：本条是管理改进用的相对分，不是绩效考核；同一口径复评应可逐项对照。
"""
    return {
        "id": "kb_staff_boss_projection_review",
        "title": f"老板投影反推与缺陷评审·{DAY}",
        "path": "", "category": "reflection", "sancai": "ren",
        "tags": ["老板", "投影分析", "缺陷评审", "评分", "性格", "能力", "五行三才"],
        "summary": "以 19 名员工作为老板的功能投影反推：综合 76/100，性格侧 71、能力侧 80；短板在耐心、授权一致性、材料齐套前置与反馈闭环机制化。",
        "body": body, "project": "全公司",
    }


def build_boss_wuxing_sancai_entry():
    """老板完整五行×三才归位：主位火天，补齐木人反馈与火天决策模型。"""
    body = f"""---
kb_id: kb_staff_boss_wuxing_sancai
title: 老板五行三才完整归位·决策模型与反馈迭代补齐·{DAY}
project: 全公司
category: decision
sancai: tian
tags: 老板、五行、三才、决策模型、反馈迭代、行动建议、知识库补齐
trace:
archived: {NOW}
---

# 老板五行三才完整归位 — 决策模型与反馈迭代补齐（{DAY}）

> 结论先行：老板主位 **火行·决策模型／天·取势**，辅位依次是
> **木·人（反馈迭代）→ 水·地（图谱归因）→ 土·地（事实底座）→ 金·人（执行口径）**。
> 本轮不再只标「火·天」，而是把五行全部落到三才格子里；其中**火行决策模型**与
> **木行反馈迭代**是最需要机制化的两处短板，已各补一张技能卡并给出行动建议。

## 一、五行×三才完整矩阵

| 五行 | 三才落点 | 老板身上的表现 | 投影员工 | 当前状态 | 缺口 |
|---|---|---|---|---|---|
| 火·决策模型 | 天·取势 | 拍板、门禁、砍跑偏、对外承诺 | CEO / 审核 / 发布 | **强** | 理由链、置信度、失效条件未固化到审批字段 |
| 木·反馈迭代 | 人·成事 | 复盘→改规则→补用例→再验证 | 审核 / CTO / CEO | **中偏弱** | 信号库、驳回四类枚举、回归用例集未落地 |
| 水·知识图谱 | 地·载物 | 双维归因、kb_graph 召回、组织画像 | CTO / 祁南 | 中 | 引用有效率、实体消歧仍是人工纪律 |
| 土·分析调研 | 地·载物 | 素材血缘、事实底座、一议题一产品 | 苏寒 / 灰太狼 / PM | 中偏弱 | `source_tier` / `confidence_1_5` 存量未回填 |
| 金·技能能力 | 人·成事 | 按区授权、工具边界、验证四层 | 全体执行岗 | 中 | 名册工具名≠实现；执行成功率无统一台账 |

**判据**：五行定性质（老板是「火」性决策者），三才定所处（主战场在「天」——取势与拍板）。
五行不是部门，一个判断可以同时落火与木；本表写的是老板的主/辅位，不是把人切成五份。

## 二、火行补齐：决策模型

### 三类范式路由
| 范式 | 适用 | 做法 | 输出 |
|---|---|---|---|
| 规则类 | 门禁、合规、审批时限、安全红线 | 阈值写死、fail-closed、无例外不放行 | PASS / REJECT + 命中规则 |
| 打分类 | 素材可信度、内容质量、产能缺口 | 维度与权重先写清，逐项打分再汇总 | 分数 + 权重 + 分档 |
| 溯因类 | 事故、驳回、超时、修完又复现 | 先三才阶段，再五行圈层，最后节点证据 | 根因 + 证据路径 + 修复动作 |

### 决策输出四要素
1. **结论**：批准 / 驳回 / 挂起 / 超时，只能选一个明确状态。
2. **理由链**：依据哪条规则、哪个分数、哪条证据；驳回必须写可执行理由。
3. **置信度**：high / medium / low；低于 0.6 不代批、不拍板，升级补材料。
4. **失效条件**：什么变化让结论作废，写入 `invalid_when`。

### 留痕口径
- 老板本人批复同样写 `reason` / `confidence` / `evidence` / `knowledgeRefs`，不再只留 `decidedBy`。
- CEO 代批仅在材料齐、检查无 fail、置信度 ≥0.6 时生效；老板可复核否决并写明理由。
- 超时 ≠ 驳回；驳回 ≠ 可重提；代批 ≠ 放行未过审核门的内容。
- 技能真源：`/opt/sefs/hermes/profiles/boss/skills/opc/decision-model/SKILL.md`。

## 三、木行补齐：反馈迭代

### 六步闭环
1. **采信号**：驳回、超时、事故、老板纠正、用户抱怨 → `ops/feedback_events.jsonl`。
2. **结构化**：四类枚举（事实 / 结构 / 合规 / 技术）+ 严重等级 + trace + 责任岗。
3. **双维归因**：先三才（输入/处理/输出/回流），再五行（火水土金木），落到节点与证据路径。
4. **改规则**：更新 SOUL.md / 岗位职责 / config / 技能卡 / 提示词；只改产物不算修复。
5. **补用例**：每条 P0/P1 至少 1 条可复跑用例，纳入 `selfcheck.py` 或专项脚本。
6. **再验证归档**：同缺口两轮同档才变档；结论写进 `公司/事故与复盘/` 与知识库条目。

### 归属分工
- CEO 归口信号库与 P1 台账；审核员给四档判定与驳回理由；CTO 负责用例可执行化。
- 祁南归档并维护双写一致性；老板只做分级裁决与规则拍板，不代替日常登记。
- 验收：每条信号有类型/等级/trace/根因/规则变更/用例路径/复验结果；同类问题第二次必须命中已有用例。
- 技能真源：`/opt/sefs/hermes/profiles/boss/skills/opc/feedback-loop/SKILL.md`。

## 四、行动建议

| 优先级 | 动作 | 主责 | 验收口径 |
|---|---|---|---|
| P0 | 审批单新增 `reason` / `confidence` / `evidence` / `knowledgeRefs`，本人批复也必填 | 老板 + CEO | 不再出现无理由 `decidedBy` |
| P0 | 材料齐套校验前置到 CCO/PM 出单口，审批门只收齐套单 | CCO + PM | 审批单齐套字段可查 |
| P1 | 建 `ops/feedback_events.jsonl` 与驳回四类枚举，木行不再只有审核员一人 | CEO + 顾凛川 | 每条信号六字段齐全 |
| P1 | 每条 P0/P1 沉淀 1 条回归用例并接入自检 | 扶频 + 祁南 | 同缺口两轮同档可追 |
| P1 | 每月一次「五行三才体检」：kb_graph 召回 + sancai 分布 + 磁盘/端口/模型版本亲验 | 祁南 + 扶频 | 不再靠事故更新文档 |
| P2 | 非 P0 直接命令延迟 15 分钟由小秘书汇总，老板只看 30 秒看板 | 老板 + 小秘书 | 逐条催问下降 |

## 五、与其他条目的关系

- 主画像：`kb_staff_boss`（火·天）。
- 六张技能卡：`kb_staff_boss_skills`。
- 缺陷评审：`kb_staff_boss_projection_review`（76/100，性格 71 / 能力 80）。
- 总报告：`kb_staff_report_{DAY.replace('-','')}`。
- 五行三才口径：`prototype/sancai.py`、`kb_wuxing_sancai_ops`。

## 六、证据与口径
- 制度源：`公司/岗位职责.md`、`公司/员工名册.md`。
- profile 源：`/opt/sefs/hermes/profiles/boss/SOUL.md`、`memories/USER.md`、`memories/MEMORY.md`。
- 行为源：owner 库 `approvals`（44 张）、`directives`（12 条）、`chat_messages`（433 条）。
- 本条为机制化补齐条目，与 `kb_staff_boss` 同时生效；改五行归位时两处必须同步。
"""
    return {
        "id": "kb_staff_boss_wuxing_sancai",
        "title": f"老板五行三才完整归位·决策模型与反馈迭代补齐·{DAY}",
        "path": "", "category": "decision", "sancai": "tian",
        "tags": ["老板", "五行", "三才", "决策模型", "反馈迭代", "行动建议", "知识库补齐"],
        "summary": "老板主位火·天，补齐木·人反馈迭代与火·天决策模型；新增 decision-model / feedback-loop 两张技能卡及 P0-P2 行动建议。",
        "body": body, "project": "全公司",
    }


def build_boss_iteration_entry():
    """自我迭代与决策四件：风险评估 / 经验法则 / 领域模型 / 战略洞察。"""
    body = f"""---
kb_id: kb_staff_boss_iteration
title: 老板自我迭代与决策四件补齐·风险评估/经验法则/领域模型/战略洞察·{DAY}
project: 全公司
category: decision
sancai: tian
tags: 老板、自我迭代、决策、风险评估、经验法则、领域模型、战略洞察
trace:
archived: {NOW}
---

# 老板自我迭代与决策四件补齐（{DAY}）

> 结论先行：老板的迭代回路此前有 `decision-model`（火）与 `feedback-loop`（木），
> 但缺「风险怎么定级、经验怎么复用、系统改哪里不坏、方向往哪走」四件。
> 本轮补齐 `risk-assessment` / `heuristics` / `domain-model` / `strategic-insight` 四张技能卡，
> 使自我迭代从「靠复盘文化」升级为「有模型、有法则、有边界、有方向」的闭环。

## 一、四件总览

| 能力 | 技能卡 | 五行三才 | 回答的问题 | 输出物 |
|---|---|---|---|---|
| 风险评估 | `risk-assessment` | 火·天 | 这事能不能做、要不要本人拍板 | 风险卡（等级/可逆性/回滚） |
| 经验法则 | `heuristics` | 土·地 | 类似情况过去怎么判、反例是什么 | 可调用快判规则 + 反例 |
| 领域模型 | `domain-model` | 水·地 | 改这里会不会破坏系统 | 五层边界 + 十类实体 + 不变量 |
| 战略洞察 | `strategic-insight` | 火·天 | 往哪走、不做什么、下一刀砍哪里 | 洞察卡（信号→解读→决策→验证） |

与前序能力的关系：`decision-model` 是决策格式，`feedback-loop` 是迭代节奏，
本四件是**判断输入**——没有它们，四要素会写成空话，六步闭环会停在「修好了」。

## 二、风险评估

### 八类风险
合规 / 成本 / 事实素材 / 技术运维 / 产能人力 / 对外承诺 / 数据凭据 / 单点依赖。

### 定级四问
- **等级**：P0 核心不可用或合规风险；P1 核心异常；P2 局部瑕疵；P3 体验优化。
- **影响面**：跨项目即 upgrade 一级。
- **可逆性**：不可逆 + 高影响必须本人拍板；可逆 + 低影响按区授权。
- **证据充分度**：无证据写「不可测」；证据不足补材料，不拍板。

### 风险卡字段
`id | 描述 | 类别 | 等级 | 证据路径 | 触发条件 | 缓解动作 | 回滚方案 | 责任岗 | 失效条件`

### 公司级已知风险（2026-11-17）
`/mnt/sefs` 68% 且备份同盘；一人一号使审批留痕主体不可辨；4750/9999 未收紧；
多份 `materials.json` 事实源混淆；ComfyUI 只有 CPU；木行反馈闭环未机制化。
以上风险不是「待优化」，而是下一次决策的硬约束。

## 三、经验法则

| 类别 | 核心法则 | 反例 |
|---|---|---|
| 判断 | 无数据源不上报；不可测即不达标；可查不等于查得准；名册工具名≠实现；先三才后五行 | 用记忆填「大概达标」 |
| 执行 | 先止血后归因；最小可用再铺开；一议题一项目一产品；材料齐套才批；三段式派单 | 边查边跑扩大损失 |
| 治理 | 按区授权；能出声不等于能交付；能跑不等于可验；不删文件只标归档 | 直接改宿主配置 |
| 迭代 | 同类缺口两轮同档才变档；修完不复现才算修复；复盘→规则→用例→验证；老板只定口径不代执行 | 口头「修好了」收尾 |

**升级规则**：两次以上独立事件的共同教训才可提炼为法则；一次事故只进 `feedback-loop`。
法则变更同步技能卡、`MEMORY.md` 与知识库条目。

## 四、领域模型

### 五层边界上下文
制度层（公司/*.md）→ 运行层（opc.db）→ 知识层（vault kb + db）→ 执行层（OpenMontage / skills）→ 身份层（profiles）。

### 十类实体
老板、员工、项目、内容单、trace、阶段、知识条目、素材/热点、审批单、信号/技能卡。

### 关键不变量
- 一议题一项目一产品；某项目当天 0 条合法。
- 每阶段有输入工件 / 处理 / 输出工件与 checkpoint，缺环即卡 gate。
- 审核四档封闭；未批不发布；超时 ≠ 驳回；驳回不得借重提复活。
- vault md 数 == db `kb_` 条目数；流水线阶段 id 只在 `config/pipeline.yaml` 定义。
- 技能真源在 `profiles/<id>/skills`；老板 profile 只记录不派活、不持凭据。

### 改动影响面速查
改制度 → 同步 SOUL + 知识库 + 名册；改阶段 → 同步 pipeline.yaml + pipeline_defs.py + 前端；
改权限 → 同步 config.yaml + SOUL 边界 + 审批字段；改知识分类 → 同步 sancai.py + web/src/sancai.ts。

## 五、战略洞察

| # | 洞察 | 决策含义 |
|---|---|---|
| 1 | 规模上限是机制化率，不是人数 | 把「知道」变成不依赖人盯的机制 |
| 2 | 审批瓶颈在材料，不在人 | 齐套校验前移到 CCO/PM 出单口 |
| 3 | 天强人足地薄 | 下一阶段补底座，不补人 |
| 4 | 木行最弱即复利最弱 | 回流侧是下一主战场 |
| 5 | 一人一号是隐性天花板 | 代理签名与复核字段优先于扩编 |
| 6 | 文档不是事实 | 每月亲验模型版本/ffmpeg/磁盘/端口 |
| 7 | 同盘备份不是备份 | 底座风险优先于功能需求 |
| 8 | 外部案例只引结构不引数字 | 无出处即禁用 |

**洞察卡格式**：信号（可查证据）→ 结构解读（为什么是系统问题）→ 决策（做/不做/延后）→ 验证口径。
**不做什么**：不扩编、不重建体系、不一次性铺满回归用例、不为凑数拆议题。

## 六、自我迭代总回路

风险评估选优先级 → 经验法则快判 → 领域模型查影响面 → 战略洞察定方向 →
`decision-model` 写四要素 → `evidence-gate` 查齐套 → `feedback-loop` 补用例再验证 →
更新技能卡 / SOUL / 知识库 → 回到风险评估。

**完成标准**：一次迭代必须同时留下三样东西——决策阈值变更、回归用例、知识库条目。
只改产物不算迭代，只写感想不算洞察。

## 七、行动建议

| 优先级 | 动作 | 主责 | 验收口径 |
|---|---|---|---|
| P0 | 审批单接入风险卡：`risk_level` / `reversible` / `rollback` | 老板 + CEO | 不可逆单无回滚不进队列 |
| P0 | 把八类已知风险写进 `production.start` 摘要模板 | CCO + PM | 风险与成本/素材/合规同单呈现 |
| P1 | 经验法则库接入 `selfcheck`，每次复盘核对是否违反已验证法则 | 扶频 + 祁南 | 违反法则的复现可自动标记 |
| P1 | 领域模型不变量清单纳入改动评审（改阶段/权限/分类必查） | 扶频 | 每次变更附不变量核对结果 |
| P1 | 每月出一张战略洞察卡，信号→解读→决策→验证四格齐全 | 老板 + CEO | 洞察落到阈值/用例/KB 三处 |
| P2 | 战略洞察与主报告同步刷新，避免重复判断 | 祁南 | 同一结论只有一个真源 |

## 八、关系与证据

- 主画像：`kb_staff_boss`；六卡版技能：`kb_staff_boss_skills`（现十张）。
- 五行归位：`kb_staff_boss_wuxing_sancai`；缺陷评审：`kb_staff_boss_projection_review`。
- 真源：`/opt/sefs/hermes/profiles/boss/skills/opc/` 下 `risk-assessment`、`heuristics`、`domain-model`、`strategic-insight` 四张卡。
- 行为证据：owner 库 `approvals` 44 张、`directives` 12 条、`chat_messages` 433 条。
"""
    return {
        "id": "kb_staff_boss_iteration",
        "title": f"老板自我迭代与决策四件补齐·{DAY}",
        "path": "", "category": "decision", "sancai": "tian",
        "tags": ["老板", "自我迭代", "决策", "风险评估", "经验法则", "领域模型", "战略洞察"],
        "summary": "补齐老板自我迭代与决策四件：风险评估、经验法则、领域模型、战略洞察；与 decision-model/feedback-loop 组成完整回路，附 P0-P2 行动建议。",
        "body": body, "project": "全公司",
    }


def build_report(entries, roster):
    elems = Counter(META[a]["element"] for a in META)
    sans = Counter(META[a]["sancai"] for a in META)
    projs = Counter(r["project"] for r in roster.values())
    rows = []
    for aid, m in META.items():
        r = roster[aid]
        wl, _ = wuxing_of(m["element"])
        rows.append(f"| {r['name']} | {r['role']} | {r['project']} | {wl}／{SANCAI[m['sancai']]} | {m['secondary']} | {'、'.join(m['personality'][:3])} |")
    elem_line = "｜".join(f"{WUXING[k][0]} {elems[k]}" for k in ("fire", "water", "earth", "metal", "wood"))
    san_line = "｜".join(f"{SANCAI[k]} {sans[k]}" for k in ("tian", "di", "ren"))
    body = f"""---
kb_id: kb_staff_report_{DAY.replace('-','')}
title: OPC全员画像与五行三才内化最终报告·{DAY}
project: 全公司
category: decision
sancai: tian
tags: 员工画像、五行三才、性格画像、思维模式、老板、全员盘点、综合判断、最终报告
trace:
archived: {NOW}
---

# OPC 全员画像与五行三才内化最终报告（{DAY}）

> 结论先行：全员 19 人（老板 1 + AI 员工 18）已逐人建档并内化到知识库；
> 组织能力呈「天强、人足、地薄」与「金重、火集中、水木偏少」的结构性特征。
> 真正瓶颈不在人不够，而在**审批材料齐套率**与**木行反馈闭环**。

## 一、内化口径与数据源

- 岗位/职责：`公司/员工名册.md`（配置源）、`公司/岗位职责.md`（制度源）。
- 性格/记忆：`/opt/sefs/hermes/profiles/<id>/SOUL.md`、`memories/USER.md`、`memories/MEMORY.md`、`config.yaml`；老板已补齐真实档案 OPC-000（只记录不派活），四张治理技能卡另见 `kb_staff_boss_skills`。
- 历史记录：owner 库 `messages`、`chat_messages`、`trace_events`、`approvals`、`directives` 实测计数与样本。
- 五行三才：`prototype/sancai.py` 与 `2026-11-16-同心圆五行三才内化-管理层运行手册-kb_wuxing_sancai_ops.md`；五行定性质，三才定所处，两维正交。
- 双写落点：`OPC/知识库/*.md` + owner 库 `knowledge` 表，同 id 幂等覆盖。

## 二、全员五行三才归属

| 姓名 | 岗位 | 项目 | 主位（五行／三才） | 辅位 | 性格关键词 |
|---|---|---|---|---|---|
{chr(10).join(rows)}

分布：{elem_line}；{san_line}；项目：{'｜'.join(f'{k} {v}' for k,v in projs.items())}。

## 三、老板专项更新

### 性格
结论先行、证据洁癖、强边界授权、敢砍任务敢叫停、反形式主义、催办有节奏。
无数据源不上报；先止血后归因；对外克制、对内直给。

### 技能
战略拍板与门禁审批；制度与流程设计；五行三才知识治理；成本/素材/合规三检；
技术架构判断（依赖向内、按区授权、验证四层、发布回滚）；IT 台账与自愈体系；
内容调性与产能调度。已补齐真实档案 `profiles/boss/`（SOUL / USER / MEMORY / config）
与十张技能卡：原六张 `boss-governance`、`five-elements-sancai`、`evidence-gate`、`dispatch-protocol`、
`decision-model`、`feedback-loop`，新增 `risk-assessment`、`heuristics`、`domain-model`、`strategic-insight`；
完整五行三才归位见 `kb_staff_boss_wuxing_sancai`，自我迭代四件见 `kb_staff_boss_iteration`。

### 思维模式
双维归因（先三才后五行）→ 取证式判断（不可测即不达标）→ 先止血后归因 →
最小可用再铺开 → 按区授权 → 红队思维（fail-closed）→ 复盘改规则补用例再验证。

### 综合判断
老板是系统设计师 + 最终仲裁人，不是执行者。应把日常流量交给 CEO（裁决/代批）、
小秘书（催办/汇总）、CTO 与 CCO（技术/创意口径）。当前审批门瓶颈是**材料不齐**
而非无人批；10 分钟超时 + CEO 有条件代批 + 小秘书即时催办应保留，
同时把材料齐套校验前移到 PM/CCO 出单口。
风险：一人一号使审批留痕主体不可辨；直接命令过多会绕过编制节奏；
知识库与台账双写一致性依赖纪律。派单坚持「结论 + 边界 + 验收口径」三段式。

## 四、组织整体判断

1. **天位强**：老板、CEO、CCO、苏寒、灰太狼、发布岗六人覆盖取势、拍板、外部采集与对外发布，方向感不是短板。
2. **人位足**：秘书、三名 PM、三名配音、三名视频、审核共十一人承担加工与成事，产线人力结构完整。
3. **地位薄**：只有 CTO 与祁南两人真正做底座与沉淀；素材血缘、回归用例、引用有效率仍靠人工纪律。
4. **金行偏重**：执行岗占 8 人，工具面广，但执行成功率/耗时缺统一台账，「能跑」不等于「可验」。
5. **火行集中**：拍板口径集中在老板/CEO/CCO，PM 只作制作判断；口径一致性好，但单点负荷高。
6. **木行最弱**：只有审核员顾凛川一人承担反馈迭代；驳回理由未结构化，复盘→规则→用例的闭环尚未机制化。
7. **审批门是真瓶颈**：历史 44 张审批单中大量 timeout/rejected，最近批量被代批引擎驳回的原因是素材缺口与成本未回传。

## 五、行动建议（按优先级）

| 优先级 | 动作 | 主责 | 验收口径 |
|---|---|---|---|
| P0 | 审批单强制附成本/素材/合规三项与产能缺口，不齐不进老板队列 | CCO + PM + CEO | 审批单 raw 字段齐套率可查 |
| P0 | 保留 10 分钟超时 + CEO 置信度≥0.6 代批 + 小秘书即时催办 | 老板授权 / CEO / 小秘书 | 超时单 100% 有协调待办 |
| P1 | 建木行信号库：驳回理由按事实/结构/合规/技术四类结构化 | 顾凛川 + CEO | 同类缺口两轮同档可追 |
| P1 | 水行补回归用例集与 knowledgeRefs 有效率月报 | 扶频 + 祁南 | 每条 P0/P1 至少 1 条可复跑用例 |
| P1 | 土行补 source_tier / confidence_1_5 与素材血缘 | 苏寒 + 灰太狼 + 三名 PM | 新素材必填，存量按批回填 |
| P2 | 老板派单统一「结论 + 边界 + 验收口径」，日常展开交秘书 | 老板 / 小秘书 | 直接命令不再绕过编制节奏 |

## 六、最终结论

这套组织的强项是**方向清晰、产线完整、门禁严格**；短板是**底座薄、反馈弱、审批材料不齐**。
不需要扩编，也不建议重建体系。下一步只做三件事：把材料齐套前置到出单口、
把驳回理由结构化、把回归用例与引用有效率做成台账。
老板继续守「定口径、卡门禁、砍跑偏」三件事，其余通过 CEO/小秘书/CTO/CCO 分流。

## 七、证据与召回

- 逐人画像条目：`kb_staff_boss`、`kb_staff_secretary` … `kb_staff_manual_scout`（共 19 条）。
- 老板技能条目：`kb_staff_boss_skills`（4 张技能卡）。
- 老板投影缺陷评审：`kb_staff_boss_projection_review`（综合 76/100）。
- 老板五行三才补齐：`kb_staff_boss_wuxing_sancai`（决策模型 + 反馈迭代 + 行动建议）。
- 老板自我迭代补齐：`kb_staff_boss_iteration`（风险评估 + 经验法则 + 领域模型 + 战略洞察）。
- 召回验证：`python3 prototype/kb_graph.py brief "员工画像 五行三才"`、`brief "老板 性格 思维模式"`、`brief "老板技能 治理 门禁"`。
- 一致性：`python3 prototype/selfcheck.py` 检查 vault md 数与 db kb_ 条目数。
"""
    return {
        "id": f"kb_staff_report_{DAY.replace('-','')}",
        "title": f"OPC全员画像与五行三才内化最终报告·{DAY}",
        "path": "OPC/知识库/",
        "category": "decision", "sancai": "tian",
        "tags": ["员工画像", "五行三才", "性格画像", "思维模式", "老板",
                 "全员盘点", "综合判断", "最终报告"],
        "summary": "19 名员工逐人画像完成，老板 profile 与 4 张技能卡补齐：金行人足、天位强、地位薄、木行最弱；瓶颈在审批材料齐套与反馈闭环。",
        "body": body, "project": "全公司",
    }


def safe_name(title):
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title)[:60].strip("-")


def write_entry(k, con, boss_extra=""):
    KB_DIR.mkdir(parents=True, exist_ok=True)
    md = KB_DIR / f"{DAY}-{safe_name(k['title'])}-{k['id']}.md"
    body = k["body"]
    if boss_extra:
        body = body.replace("## 十、证据路径", boss_extra + "\n## 十、证据路径")
    text = body if body.startswith("---") else "\n".join([
        "---", f"kb_id: {k['id']}", f"title: {k['title']}", f"project: {k['project']}",
        f"category: {k['category']}", f"sancai: {k['sancai']}",
        f"tags: {'、'.join(k['tags'])}", "trace:", f"archived: {NOW}", "---", "", body])
    for old in KB_DIR.glob(f"*{k['id']}.md"):
        if old != md:
            old.unlink()
    md.write_text(text, encoding="utf-8")
    k["path"] = str(md.relative_to(VAULT))
    con.execute("""INSERT INTO knowledge(id,title,path,tags,updated_at,raw,category,
                   sancai,summary,project_id,trace_id,body,created_at,confidence,invalid_when,superseded_by)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title,path=excluded.path,
                     tags=excluded.tags,updated_at=excluded.updated_at,raw=excluded.raw,
                     category=excluded.category,sancai=excluded.sancai,summary=excluded.summary,
                     project_id=excluded.project_id,trace_id=excluded.trace_id,body=excluded.body,
                     created_at=excluded.created_at,confidence=excluded.confidence,
                     invalid_when=excluded.invalid_when,superseded_by=excluded.superseded_by""",
                (k["id"], k["title"], k["path"], json.dumps(k["tags"], ensure_ascii=False), NOW,
                 json.dumps({kk: vv for kk, vv in k.items() if kk != "body"}, ensure_ascii=False),
                 k["category"], k["sancai"], k["summary"], k["project"], "", text[:6000],
                 NOW, "high", "岗位职责或生产 profile 发生结构性变更（岗位/职责/授权）", None))
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    roster = parse_roster()
    missing = [a for a in META if a not in roster]
    if missing:
        print(f"[staff-profile] 名册缺少 {missing}", file=sys.stderr)
        return 1
    entries = []
    boss_facts = None
    for aid, meta in META.items():
        facts = profile_facts(aid)
        if aid == "boss":
            boss_facts = facts
        entries.append(build_entry(aid, meta, roster[aid], facts, db_stats(aid, meta["name"])))
    if boss_facts is None:
        boss_facts = profile_facts("boss")
    entries.append(build_boss_skills_entry(boss_facts))
    entries.append(build_boss_projection_entry())
    entries.append(build_boss_wuxing_sancai_entry())
    entries.append(build_boss_iteration_entry())
    entries.append(build_report(entries, roster))
    if args.json:
        print(json.dumps([{"id": e["id"], "title": e["title"], "category": e["category"],
                           "sancai": e["sancai"], "summary": e["summary"]} for e in entries],
                         ensure_ascii=False, indent=2))
        return 0
    if not args.apply:
        print(f"[staff-profile] 预演 {len(entries)} 条（{len(META)} 人 + 1 老板技能 + 1 投影评审 + 1 五行三才补齐 + 1 自我迭代补齐 + 1 总报告），未落盘")
        for e in entries:
            print(f"  {e['id']:28s} {e['category']}/{e['sancai']}  {e['title']}")
        return 0
    con = sqlite3.connect(str(OWNER_DB), timeout=15)
    written = []
    for e in entries:
        extra = build_boss_extra() if e["id"] == "kb_staff_boss" else ""
        written.append(write_entry(e, con, extra))
    con.commit()
    kb_n = con.execute("select count(*) from knowledge where id like 'kb_%'").fetchone()[0]
    con.close()
    md_n = len(list(KB_DIR.rglob("*.md")))
    print(f"[staff-profile] 已内化 {len(written)} 条；vault md={md_n} / db kb_={kb_n}")
    print(f"[staff-profile] 报告：{written[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
