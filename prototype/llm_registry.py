"""大模型注册表：可用通道 / 额度 / 状态，Hermes 调用前检查与扣减；通道写入 profile。"""
import json
import os
import re
import subprocess
import sqlite3
import time

from opc_paths import *  # noqa: F401,F403
from utils import iso_now, _to_int
import db as DBX
import company_config as CC
import org as ORG

LLM_DEFAULTS = [
    {
        # 百炼 Qwen3.7-Plus：日间默认推理通道（08:00–22:00，quality-first）
        "id": "qwen37plus-bailian",
        "name": "qwen3.7-plus",
        "url": os.environ.get("OPC_LLM_QWEN37PLUS_BASE_URL", "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
        "protocol": "openai-chat",
        "totalTokens": int(os.environ.get("OPC_LLM_QWEN37PLUS_TOTAL_TOKENS", "0")),   # 0=不限额
        "alias": "qwen37plus",
        "priority": 3,
        "provider": "custom",
        "apiKey": os.environ.get("OPC_LLM_QWEN37PLUS_API_KEY", "sk-sp-H.HIRMIY.MTtu.MEUCIBMFZgP1a-BuN2dHQoxIkHQulwA35bCgydldMJoNvlcjAiEAuDjzjPx0OlvGEcoD_JaoOn7t33KcYXq0pzce_1PB7Qo"),
        "note": "百炼 Qwen3.7-Plus（日间默认 08–22，131k 输入 / 16k 输出）",
    },
    {
        # 百炼 Qwen3.8-Flash：夜间默认推理通道（22:00–08:00，cost-first，4 折 + 缓存叠加）
        "id": "qwen38flash-bailian",
        "name": "qwen3.8-flash",
        "url": os.environ.get("OPC_LLM_QWEN38FLASH_BASE_URL", "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
        "protocol": "openai-chat",
        "totalTokens": int(os.environ.get("OPC_LLM_QWEN38FLASH_TOTAL_TOKENS", "0")),   # 0=不限额
        "alias": "qwen38flash",
        "priority": 4,
        "provider": "custom",
        "apiKey": os.environ.get("OPC_LLM_QWEN38FLASH_API_KEY", "sk-sp-H.HIRMIY.MTtu.MEUCIBMFZgP1a-BuN2dHQoxIkHQulwA35bCgydldMJoNvlcjAiEAuDjzjPx0OlvGEcoD_JaoOn7t33KcYXq0pzce_1PB7Qo"),
        "note": "百炼 Qwen3.8-Flash（夜间默认 22–08，多模态/4 折性价比第一）",
    },
    {
        # LiteLLM 网关（192.168.21.208:8000）· auto-free：免费通道，2026-11-14 起默认（priority 最小）
        "id": "litellm-autofree",
        "name": "auto-free",
        "url": os.environ.get("OPC_LLM_AUTOFREE_BASE_URL", "http://192.168.21.208:8000/v1"),
        "protocol": "openai-chat",
        "totalTokens": int(os.environ.get("OPC_LLM_AUTOFREE_TOTAL_TOKENS", "0")),   # 免费通道，0=不限额
        "alias": "autofree",
        "priority": 5,
        "provider": "custom",
        "apiKey": os.environ.get("OPC_LLM_AUTOFREE_KEY", "sk-litellm-15bbcf2ea58c156130a00afd1b43ae4a"),
        "note": "LiteLLM 网关免费通道（auto-free，默认）",
    },
    {
        "id": "hermes-default",
        "name": MODEL,
        "url": os.environ.get("OPC_LLM_BASE_URL", ""),
        "protocol": os.environ.get("OPC_LLM_PROTOCOL", "hermes-cli"),
        "totalTokens": int(os.environ.get("OPC_LLM_TOTAL_TOKENS", "20000000")),
        "alias": "",
        "priority": 10,
        "provider": os.environ.get("OPC_LLM_PROVIDER", "opencode-go"),
        "apiKey": "",
        "note": "默认模型（Hermes CLI 调用）",
    },
    {
        # OpenCode Go · GLM-5.3-Flash：多模态、1M 上下文；api_key 由管理员在运维页登记（不入代码）
        "id": "opencode-glm53",
        "name": "glm-5.3-flash",
        "url": os.environ.get("OPC_LLM_GLM_BASE_URL", "https://opencode.ai/zen/go/v1"),
        "protocol": "hermes-cli",
        "totalTokens": int(os.environ.get("OPC_LLM_GLM_TOTAL_TOKENS", "20000000")),
        "alias": "glm53",
        "priority": 20,
        "provider": "opencode-go",
        "apiKey": "",
        "note": "OpenCode Go · GLM-5.3-Flash（多模态/1M 上下文）",
    },
    {
        # 本地推理通道（LiteLLM 网关，OpenAI 兼容）：无时间要求任务的优先通道
        "id": "qwen38-local",
        "name": "local-qwen3.8-flash",
        "url": os.environ.get("OPC_LLM_LOCAL_BASE_URL", "http://192.168.31.208/llm/v1"),
        "protocol": "openai-chat",
        "totalTokens": int(os.environ.get("OPC_LLM_LOCAL_TOTAL_TOKENS", "0")),   # 本地推理，0=不限额
        "alias": "qwen38",
        "priority": 50,
        "provider": "custom",
        "apiKey": "sk-litellm-15bbcf2ea58c156130a00afd1b43ae4a",
        "note": "本地推理通道（LiteLLM @ 192.168.31.208/llm，2026-11-13 自 llama.cpp 直连迁移）",
    },
]

def _llm_store():
    """过程库单例（llm_models 所在）；未启用时返回 None。"""
    return DBX.store() if DBX else None

def _llm_consume(model, tokens):
    """调用成功后把本次 token 用量记到大模型注册表（扣额度/自动熔断）。"""
    st = _llm_store()
    if st:
        st.llm_consume(model, tokens)

def _llm_view():
    """大模型注册表视图（运维页 /api/ops）：camelCase + 汇总 + 员工通道设置 + 时段调度。"""
    st = _llm_store()
    if not st:
        return {"available": False, "error": "过程库未启用（OPC_DB=0），大模型注册表不可用",
                "models": [], "profiles": [], "schedule": []}
    models = []
    for r in st.llm_models():
        total, used = _to_int(r.get("total_tokens"), 0), _to_int(r.get("used_tokens"), 0)
        models.append({
            "id": r.get("id") or "", "name": r.get("name") or "",
            "url": r.get("url") or "", "protocol": r.get("protocol") or "",
            "alias": r.get("alias") or "",
            "priority": _to_int(r.get("priority"), 100),
            "status": r.get("status") or "enabled",
            "totalTokens": total, "usedTokens": used,
            "availableTokens": (total - used) if total > 0 else -1,   # -1 = 不限额
            "note": r.get("note") or "",
            "updatedAt": r.get("updated_at") or "", "updatedBy": r.get("updated_by") or "",
        })
    view = {
        "available": True,
        "models": models,
        "enabled": sum(1 for m in models if m["status"] == "enabled"),
        "exhausted": sum(1 for m in models if m["status"] == "exhausted"),
        "disabled": sum(1 for m in models if m["status"] == "disabled"),
        "profiles": _llm_profiles_view(models),
        "schedule": st.llm_schedule_list() if st else [],
    }
    return view

def _llm_manage_view():
    """模型管理页专用视图：含完整可编辑字段（含 API Key）、时段规则与当前生效通道。
    仅对「老板 + 存量公司空间」开放，故此处返回明文凭据。"""
    st = _llm_store()
    if not st:
        return {"available": False, "error": "过程库未启用", "models": [],
                "schedule": [], "effective": None}
    rows = st.llm_models()
    models = []
    for r in rows:
        total, used = _to_int(r.get("total_tokens"), 0), _to_int(r.get("used_tokens"), 0)
        models.append({
            "id": r.get("id") or "",
            "name": r.get("name") or "",
            "url": r.get("url") or "",
            "apiKey": r.get("api_key") or "",
            "protocol": r.get("protocol") or "",
            "provider": r.get("provider") or "",
            "alias": r.get("alias") or "",
            "priority": _to_int(r.get("priority"), 100),
            "status": r.get("status") or "enabled",
            "isDefault": bool(_to_int(r.get("is_default"), 0)),
            "contextLength": _to_int(r.get("context_length"), 0),
            "maxOutputTokens": _to_int(r.get("max_output_tokens"), 0),
            "totalTokens": total, "usedTokens": used,
            "note": r.get("note") or "",
            "updatedAt": r.get("updated_at") or "",
            "updatedBy": r.get("updated_by") or "",
        })
    rules = st.llm_schedule_list()
    picked, _ = st.llm_pick()
    effective = None
    if picked and picked.get("id"):
        active = next((x for x in rules if x["enabled"] and x["active"]
                       and x["modelId"] == picked.get("id")), None)
        source = "schedule" if active else ("default" if _to_int(picked.get("is_default"), 0) else "priority")
        effective = {"id": picked.get("id"), "name": picked.get("name"),
                     "source": source, "rule": active["label"] if active else ""}
    return {"available": True, "models": models, "schedule": rules,
            "effective": effective, "profiles": _llm_profiles_view(models)}

# ---------- Hermes 配置同步：模型管理页的增删改要能真正被 `--model <alias>` 使用 ----------
def _hermes_config_files():
    """主配置 + 全部员工 profile 的 config.yaml。"""
    root = os.path.dirname(ORG_PROFILES.rstrip("/"))
    files = [os.path.join(root, "config.yaml")]
    try:
        for pid in sorted(os.listdir(ORG_PROFILES)):
            p = os.path.join(ORG_PROFILES, pid, "config.yaml")
            if os.path.isfile(p):
                files.append(p)
    except OSError:
        pass
    return [p for p in files if os.path.isfile(p)]

def _yaml_section(text, key):
    """取出顶层 `key:` 段落并解析为 dict；返回 (start, end, dict, 行列表)。
    段落边界 = 下一个顶层的非注释、非空行；段落外的注释与内容原样保留。"""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(key + ":") and (not ln[0].isspace()):
            start = i
            break
    if start is None:
        return None, None, {}, lines
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and not ln[0].isspace() and not ln.startswith("#"):
            end = j
            break
    try:
        import yaml
        data = yaml.safe_load("\n".join(lines[start + 1:end])) or {}
    except Exception:
        data = {}
    return start, end, data if isinstance(data, dict) else {}, lines

def _yaml_section_write(text, key, data):
    """把 dict 写回顶层 `key:` 段落，其余内容（含注释）逐行保留。"""
    import yaml
    start, end, _, lines = _yaml_section(text, key)
    body = yaml.safe_dump(data, default_flow_style=False, allow_unicode=True,
                          sort_keys=False, width=1000)
    block = [key + ":"] + [("  " + l if l.strip() else "") for l in body.splitlines()]
    if start is None:
        merged = lines + ([""] if lines and lines[-1].strip() else []) + block
    else:
        merged = lines[:start] + block + lines[end:]
    return "\n".join(merged) + "\n"

def _llm_alias_sync(row, remove=False, old_alias=""):
    """把通道别名 / 端点 / 凭据 / 上下文长度同步到主配置与全部员工 profile。
    采用「只重写 providers 与 model_aliases 两段」的文本级合并，保留其它注释与配置。
    """
    alias = str(old_alias or row.get("alias") or "").strip()
    if not alias:
        return
    provider_key = str(row.get("id") or "").strip() or alias
    for path in _hermes_config_files():
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            _, _, providers, _ = _yaml_section(text, "providers")
            _, _, aliases, _ = _yaml_section(text, "model_aliases")
            if remove:
                aliases.pop(alias, None)
                providers.pop(provider_key, None)
                providers.pop(alias, None)
            else:
                aliases[alias] = {
                    "model": str(row.get("name") or ""),
                    "provider": str(row.get("provider") or "custom"),
                    "base_url": str(row.get("url") or ""),
                    "api_key": str(row.get("apiKey") or row.get("api_key") or ""),
                }
                model_cfg = {}
                if _to_int(row.get("contextLength"), 0) > 0:
                    model_cfg["context_length"] = _to_int(row.get("contextLength"), 0)
                if _to_int(row.get("maxOutputTokens"), 0) > 0:
                    model_cfg["max_output_tokens"] = _to_int(row.get("maxOutputTokens"), 0)
                providers[provider_key] = {
                    "api": str(row.get("url") or ""),
                    "api_key": str(row.get("apiKey") or row.get("api_key") or ""),
                    "transport": "openai_chat",
                    "models": {str(row.get("name") or ""): model_cfg},
                }
            out = _yaml_section_write(text, "providers", providers)
            out = _yaml_section_write(out, "model_aliases", aliases)
            with open(path, "w", encoding="utf-8") as f:
                f.write(out)
        except OSError as e:
            print(f"[llm] 同步 hermes 配置失败 {path}: {e}", flush=True)

def _parse_profile_model(path):
    """读取 profile config.yaml 顶层 model 段的 default/provider/base_url（只读文本解析）。"""
    out = {"default": "", "provider": "", "base_url": ""}
    try:
        top = None
        with open(path, encoding="utf-8") as f:
            for raw in f:
                ln = raw.rstrip("\n")
                if ln and not ln[0].isspace() and not ln.startswith("#"):
                    top = ln.split(":", 1)[0].strip()
                    continue
                if top != "model":
                    continue
                m = re.match(r'^\s{2,}(default|provider|base_url):\s*"?(.*?)"?\s*(?:#.*)?$', ln)
                if m:
                    out[m.group(1)] = m.group(2).strip()
    except OSError:
        pass
    return out

def _llm_profiles_view(models):
    """全部 hermes profile 的当前模型通道设置（按 CC.AGENT_SPEC 显示中文名）。"""
    names = {a[0]: a[1] for a in CC.AGENT_SPEC}
    roles = {a[0]: a[2] for a in CC.AGENT_SPEC}
    by_model = {}
    for m in models:
        by_model.setdefault(m["name"], m["id"])
    # provider 兜底匹配：custom+本地 URL → 本地通道；其他按模型名匹配
    out = []
    try:
        entries = sorted(os.listdir(ORG_PROFILES))
    except OSError:
        entries = []
    for pid in entries:
        cfg = os.path.join(ORG_PROFILES, pid, "config.yaml")
        if not os.path.isfile(cfg):
            continue
        cur = _parse_profile_model(cfg)
        channel_id = ""
        for m in models:
            if m["name"] != cur["default"]:
                continue
            if m["id"] == "qwen38-local" and cur["provider"] not in ("custom", ""):
                continue
            if m["id"] == "hermes-default" and cur["provider"] not in ("opencode-go", ""):
                continue
            channel_id = m["id"]
            break
        out.append({
            "id": pid, "name": names.get(pid, pid), "role": roles.get(pid, ""),
            "model": cur["default"], "provider": cur["provider"], "baseUrl": cur["base_url"],
            "channelId": channel_id,
            "channelName": next((m["name"] for m in models if m["id"] == channel_id), ""),
        })
    return out

def _hermes_cfg_set(profile, key, value):
    """hermes config set（HERMES_HOME 指向该 profile）；官方接口，自带校验。返回 (ok, 输出)。"""
    env = dict(os.environ)
    env["HERMES_HOME"] = os.path.join(ORG_PROFILES, profile)
    try:
        proc = subprocess.run([HERMES_BIN, "config", "set", key, str(value)],
                              capture_output=True, text=True, timeout=30, env=env)
        return proc.returncode == 0, (proc.stdout or proc.stderr or "")[-200:]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

def _llm_profile_apply(profile, channel_row):
    """把注册表通道写入 profile config.yaml 的 model 段。返回错误串（空=成功）。"""
    base_url = (channel_row.get("url") or "").strip()
    api_key = (channel_row.get("api_key") or "").strip()
    plan = [("model.default", channel_row.get("name") or ""),
            ("model.provider", channel_row.get("provider") or "custom")]
    if base_url:
        plan.append(("model.base_url", base_url))
    if api_key:
        plan.append(("model.api_key", api_key))
    for key, value in plan:
        ok, out = _hermes_cfg_set(profile, key, value)
        if not ok:
            return f"{key}: {out}"
    return ""
