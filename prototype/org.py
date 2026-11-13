"""组织配置：岗位/制度/技能/记忆 的读写、同步与审计（写 Obsidian 与 hermes profile）。"""
import json
import os
import re
import threading

from opc_paths import *  # noqa: F401,F403
from utils import _to_int, iso_now
import company_config as CC

ORG_LOCK = threading.Lock()

POLICY_FILES = {
    "jobs": ("公司/岗位职责.md", "岗位职责（制度源）", True),
    "org": ("公司/组织编制.md", "组织编制", True),
    "roster": ("公司/员工名册.md", "员工名册（配置源）", True),
    "projects": ("公司/项目配置.md", "项目配置（配置源）", True),
    "kernel": ("公司/内核与编排.md", "内核与编排", True),
    "tone": ("公司/项目与调性.md", "项目与调性（自动生成视图）", False),
    "tech": ("公司/技术备忘-运行时状态重导.md", "技术备忘", False),
}
ORG_FILE_LIMIT = 2_000_000          # 单个文件读写上限（config.yaml 约 114KB）

def _org_agent_row(aid):
    for row in CC.AGENT_SPEC:
        if row[0] == aid:
            return row
    return None

def _org_project_label(pid):
    return "全公司" if pid == "all" else CC.PROJECTS.get(pid, {}).get("name", pid)

def _org_tone(pid):
    return "跨项目中台" if pid == "all" else CC.TONE.get(pid, "")

def _org_profile_dir(aid):
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", str(aid or "")):
        raise ValueError("非法员工 ID")
    path = os.path.join(ORG_PROFILES, aid)
    if not os.path.isdir(path):
        raise ValueError(f"{aid} 没有独立 Hermes profile（老板为人工审批角色）")
    return path

def _org_read(path, limit=ORG_FILE_LIMIT):
    try:
        if os.path.getsize(path) > limit:
            raise ValueError(f"文件超过 {limit} 字节，拒绝网页读写")
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""

def _org_write(path, text, expect_prefix=None):
    """原子替换写入；保留原文件权限；expect_prefix 用于锁定只允许写指定目录。"""
    real = os.path.realpath(path)
    if expect_prefix and not real.startswith(os.path.realpath(expect_prefix) + os.sep):
        raise ValueError("写入路径越界")
    if len(text.encode("utf-8")) > ORG_FILE_LIMIT:
        raise ValueError(f"内容超过 {ORG_FILE_LIMIT} 字节")
    os.makedirs(os.path.dirname(real), exist_ok=True)
    mode = 0o644
    try:
        mode = os.stat(real).st_mode & 0o777
    except FileNotFoundError:
        pass
    tmp = f"{real}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, real)

def _org_audit(user, action, target, detail=""):
    try:
        os.makedirs(os.path.dirname(ORG_AUDIT), exist_ok=True)
        with open(ORG_AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": iso_now(), "by": (user or {}).get("name", ""),
                                "role": (user or {}).get("role", ""), "action": action,
                                "target": target, "detail": str(detail)[:200]},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass

def _org_skill_paths(profile_dir):
    """返回 [(相对 skills 的路径, 分类, 名称)]；支持 skills/<name> 与 skills/<分类>/<name> 两种层级。"""
    root = os.path.join(profile_dir, "skills")
    out = []
    try:
        for top in sorted(os.listdir(root)):
            top_dir = os.path.join(root, top)
            if top.startswith(".") or not os.path.isdir(top_dir):
                continue
            if os.path.isfile(os.path.join(top_dir, "SKILL.md")):
                out.append((top, "", top))
                continue
            for name in sorted(os.listdir(top_dir)):
                sub = os.path.join(top_dir, name)
                if name.startswith(".") or not os.path.isdir(sub):
                    continue
                if os.path.isfile(os.path.join(sub, "SKILL.md")):
                    out.append((os.path.join(top, name), top, name))
    except FileNotFoundError:
        pass
    return out

def _org_skills(profile_dir):
    out = []
    for rel, cat, name in _org_skill_paths(profile_dir):
        f = os.path.join(profile_dir, "skills", rel, "SKILL.md")
        out.append({"name": name, "category": cat, "bytes": os.path.getsize(f),
                    "updated": datetime.fromtimestamp(os.path.getmtime(f), TZ)
                    .isoformat(timespec="minutes")})
    return out

def _org_toolsets(profile_dir):
    """运行时真实生效的工具集（config.yaml → platform_toolsets.cli）；名册里的白名单只是意图。"""
    try:
        import yaml
        with open(os.path.join(profile_dir, "config.yaml"), encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        pt = data.get("platform_toolsets") or {}
        cli = pt.get("cli") or []
        return [str(x) for x in cli]
    except Exception:
        return []

def _org_agents():
    out = []
    for aid, name, role, pid, tools, scope in CC.AGENT_SPEC:
        pdir = os.path.join(ORG_PROFILES, aid)
        has_profile = os.path.isdir(pdir)
        mem = os.path.join(pdir, "memories")
        out.append({
            "id": aid, "name": name, "role": role, "projectId": pid,
            "project": _org_project_label(pid), "scope": scope, "tone": _org_tone(pid),
            "tools": tools, "profile": has_profile,
            "toolsets": _org_toolsets(pdir) if has_profile else [],
            "hasSoul": os.path.isfile(os.path.join(pdir, "SOUL.md")),
            "skills": _org_skills(pdir) if has_profile else [],
            "memoryChars": {
                "memory": len(_org_read(os.path.join(mem, "MEMORY.md"))),
                "user": len(_org_read(os.path.join(mem, "USER.md"))),
            },
        })
    return out

def _org_policies():
    out = []
    for key, (rel, title, editable) in POLICY_FILES.items():
        full = os.path.join(VAULT, rel)
        out.append({"key": key, "title": title, "path": rel, "editable": editable,
                    "exists": os.path.isfile(full),
                    "bytes": os.path.getsize(full) if os.path.isfile(full) else 0})
    return out

def _org_agent_detail(aid):
    row = _org_agent_row(aid)
    if not row:
        raise ValueError(f"没有员工 {aid}")
    pdir = os.path.join(ORG_PROFILES, aid)
    detail = {
        "id": aid, "name": row[1], "role": row[2], "projectId": row[3],
        "project": _org_project_label(row[3]), "scope": row[5],
        "tone": _org_tone(row[3]), "tools": row[4],
        "profile": os.path.isdir(pdir), "toolsets": [],
        "soul": "", "memory": "", "user": "", "config": "", "skills": [],
    }
    if os.path.isdir(pdir):
        detail["toolsets"] = _org_toolsets(pdir)
        detail["soul"] = _org_read(os.path.join(pdir, "SOUL.md"))
        detail["memory"] = _org_read(os.path.join(pdir, "memories", "MEMORY.md"))
        detail["user"] = _org_read(os.path.join(pdir, "memories", "USER.md"))
        detail["config"] = _org_read(os.path.join(pdir, "config.yaml"))
        detail["skills"] = _org_skills(pdir)
    elif aid == "boss":
        detail["note"] = "老板为人工审批角色，无独立 profile"
    return detail

def _org_skill_detail(aid, name):
    pdir = _org_profile_dir(aid)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,40}", str(name or "")):
        raise ValueError("非法技能名")
    for rel, cat, nm in _org_skill_paths(pdir):
        if nm == name:
            return {"name": name, "category": cat,
                    "content": _org_read(os.path.join(pdir, "skills", rel, "SKILL.md"))}
    raise ValueError(f"{aid} 没有技能 {name}")

def _org_update_roster(aid, body):
    """改写员工名册对应行（唯一数据源），返回 (name, role, projectId, tools, scope)。"""
    row = _org_agent_row(aid)
    if not row:
        raise ValueError(f"没有员工 {aid}")
    name = str(body.get("name", row[1])).strip() or row[1]
    role = str(body.get("role", row[2])).strip() or row[2]
    pid = str(body.get("projectId", row[3])).strip() or row[3]
    scope = str(body.get("scope", row[5])).strip() or row[5]
    tools = body.get("tools", row[4])
    if not isinstance(tools, list):
        tools = [t.strip() for t in re.split(r"[、,，]", str(tools)) if t.strip()]
    tools = [str(t).strip() for t in tools if str(t).strip()]
    if pid != "all" and pid not in CC.PROJECTS:
        raise ValueError(f"项目不存在：{pid}")
    for label, value, cap in (("姓名", name, 24), ("岗位", role, 24), ("负责范围", scope, 60)):
        if len(value) > cap or "\n" in value or "|" in value:
            raise ValueError(f"{label}不合法（≤{cap} 字、不含换行与竖线）")
    for t in tools:
        if not re.fullmatch(r"[a-z][a-z0-9._-]{1,40}", t):
            raise ValueError(f"工具名不合法：{t}")
    project_name = _org_project_label(pid)
    line = f"| {aid} | {name} | {role} | {project_name} | {scope} | {'、'.join(tools)} |\n"
    with ORG_LOCK:
        raw = _org_read(ROSTER_FILE)
        hit = False
        lines = raw.splitlines(keepends=True)
        for i, ln in enumerate(lines):
            if not ln.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) >= 6 and cells[0] == aid:
                lines[i] = line
                hit = True
                break
        if not hit:
            raise ValueError(f"名册里找不到 {aid} 的数据行")
        _org_write(ROSTER_FILE, "".join(lines), expect_prefix=VAULT)
    return name, role, pid, tools, scope, project_name

def _org_sync_soul(aid, name, role, project_label, tone):
    pdir = os.path.join(ORG_PROFILES, aid)
    path = os.path.join(pdir, "SOUL.md")
    if not os.path.isfile(path):
        return False
    txt = _org_read(path)
    new = re.sub(r"^你是 OPC 一人公司的 AI 员工「[^」]*」，岗位：[^。\n]*。",
                 f"你是 OPC 一人公司的 AI 员工「{name}」，岗位：{role}。",
                 txt, count=1, flags=re.M)
    new = re.sub(r"^负责范围：[^\n]*", f"负责范围：{project_label}", new, count=1, flags=re.M)
    new = re.sub(r"^所属项目：[^\n]*", f"所属项目：{project_label}｜风格基调：{tone}",
                 new, count=1, flags=re.M)
    if new != txt:
        _org_write(path, new, expect_prefix=pdir)
        return True
    return False

def _org_sync_org_table(aid, name, role, project_label, tone, tools):
    """组织编制是视图页，能同步就同步，失败不影响名册写入。"""
    path = os.path.join(VAULT, "公司", "组织编制.md")
    try:
        txt = _org_read(path)
        lines = txt.splitlines(keepends=True)
        skills = "、".join(dict.fromkeys(s["category"] or s["name"] for s in _org_skills(
            os.path.join(ORG_PROFILES, aid))))
        for i, ln in enumerate(lines):
            if not ln.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) >= 10 and cells[1] == aid:
                cells[2], cells[3], cells[4], cells[5] = name, role, project_label, tone
                if skills:
                    cells[7] = skills
                cells[8] = "、".join(tools)
                lines[i] = "| " + " | ".join(cells) + " |\n"
                _org_write(path, "".join(lines), expect_prefix=VAULT)
                return True
    except (OSError, ValueError):
        pass
    return False

def _org_validate_config(text):
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception as e:
        raise ValueError(f"config.yaml 解析失败：{type(e).__name__}: {e}")
    if not isinstance(data, dict):
        raise ValueError("config.yaml 顶层必须是映射")
    if not isinstance(data.get("model", {}).get("default") if isinstance(data.get("model"), dict) else None, str):
        raise ValueError("config.yaml 缺少 model.default")
    return True
