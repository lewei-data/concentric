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
    """大模型注册表视图（运维页 /api/ops）：camelCase + 汇总 + 员工通道设置。"""
    st = _llm_store()
    if not st:
        return {"available": False, "error": "过程库未启用（OPC_DB=0），大模型注册表不可用",
                "models": [], "profiles": []}
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
    }
    return view

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
