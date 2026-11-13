"""公司配置：项目 / 员工编制 / 交付合同 / OpenMontage 车间工序（vault 加载与回退）。"""
import ast
import json
import os
import re

from opc_paths import *  # noqa: F401,F403
from utils import _to_int, _num
import pipeline_defs as PIPE
from agent_defaults import DEFAULT_AGENT_SPEC, AGENTS_REQUIRED

def _load_dl(vault_dir):
    """按 vault 根目录加载一份独立的 deliverables 模块实例。

    deliverables 的路径常量（知识库/热点采集/交付物/会议纪要…）在 import 时由
    OPC_VAULT 环境变量计算，因此给每个用户工作区 import 一份独立实例即可实现
    知识库/热点/交付物完全隔离，互不影响 owner 的共享 vault。
    """
    import importlib.util
    dl_path = os.path.join(BASE_DIR, "deliverables.py")
    saved = os.environ.get("OPC_VAULT")
    os.environ["OPC_VAULT"] = vault_dir
    try:
        spec = importlib.util.spec_from_file_location(
            f"deliverables_user_{abs(hash(vault_dir))}", dl_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"[users] 用户 vault 交付模块加载失败（{vault_dir}）：{type(e).__name__}: {e}", flush=True)
        return None
    finally:
        if saved is None:
            os.environ.pop("OPC_VAULT", None)
        else:
            os.environ["OPC_VAULT"] = saved

DEFAULT_PROJECTS = {
    "wenren": {"id": "wenren", "name": "文人莫克", "kind": "自媒体账号", "color": "#2563EB",
               "kpi": {"views": 0, "completion": 0.0, "fans": 0, "engagement": 0.0}},
    "gou": {"id": "gou", "name": "狗大人", "kind": "自媒体账号", "color": "#EA580C",
            "kpi": {"views": 0, "completion": 0.0, "fans": 0, "engagement": 0.0}},
    "tongxinyuan": {"id": "tongxinyuan", "name": "同心圆", "kind": "数字分身服务平台", "color": "#7C3AED",
                    "kpi": {"uptime": 99.95, "tickets": 0, "delivery_hours": 48.0}},
}
DEFAULT_TONE = {"wenren": "克制·书卷气", "gou": "幽默·松弛", "tongxinyuan": "专业·可信"}

OPENMONTAGE_CRAFTS = (
    {"no": "01", "id": "animated-explainer", "name": "动画讲解工艺", "product": "动画讲解视频",
     "desc": "选题研究先行，旁白+图形动画完整成片，适合知识科普类选题"},
    {"no": "02", "id": "animation", "name": "动效图形工艺", "product": "动效图形视频",
     "desc": "Motion Graphics/图表动画/动态字幕为主的图形化表达"},
    {"no": "03", "id": "avatar-spokesperson", "name": "数字人口播工艺", "product": "数字人口播视频",
     "desc": "数字分身出镜口播，适合品牌播报、栏目化更新与讲解"},
    {"no": "04", "id": "character-animation", "name": "角色动画工艺", "product": "角色动画短片",
     "desc": "可复用卡通角色驱动的剧情动画，适合 IP 化系列内容"},
    {"no": "05", "id": "cinematic", "name": "电影感大片工艺", "product": "电影感宣传片",
     "desc": "情绪先导的电影级镜头语言，适合预告片/品牌片/氛围混剪"},
    {"no": "06", "id": "clip-factory", "name": "切片流水线工艺", "product": "长内容切片集",
     "desc": "从长视频/直播/课程中批量切出高光短片段"},
    {"no": "07", "id": "documentary-montage", "name": "纪实蒙太奇工艺", "product": "纪实混剪视频",
     "desc": "检索真实素材构建语义语料，主题化纪实混剪"},
    {"no": "08", "id": "framework-smoke", "name": "框架冒烟工艺", "product": "框架验证样片",
     "desc": "最小流水线清单，用于验证框架契约与链路连通"},
    {"no": "09", "id": "hybrid", "name": "混合实拍工艺", "product": "实拍混合视频",
     "desc": "实拍素材叠加设计/生成画面，虚实结合的表达"},
    {"no": "10", "id": "localization-dub", "name": "本地化译制工艺", "product": "多语译制版本",
     "desc": "字幕翻译+配音改制，一条成片衍生多语种版本"},
    {"no": "11", "id": "podcast-repurpose", "name": "播客改制工艺", "product": "播客改制视频",
     "desc": "把播客音频/视频改制为可发布的短视频内容"},
    {"no": "12", "id": "screen-demo", "name": "屏幕演示工艺", "product": "屏幕录制教程",
     "desc": "屏幕录制+讲解的软件演示/操作教程类成片"},
    {"no": "13", "id": "talking-head", "name": "真人口播工艺", "product": "真人口播视频",
     "desc": "真人出镜口播素材的端到端精修成片"},
)
CRAFT_BY_ID = {w["id"]: w for w in OPENMONTAGE_CRAFTS}
DEFAULT_CRAFT_ID = "animated-explainer"   # CCO 未明确时的科普类默认车间

CRAFT_STEP_NAMES = {
    "research": "选题研究", "idea": "创意构思", "proposal": "提案定案",
    "script": "剧本脚本", "scene_plan": "分镜规划", "character_design": "角色设计",
    "rig_plan": "角色绑定", "assets": "素材制作", "edit": "剪辑合成",
    "compose": "终版合成", "publish": "成片发布",
}

CRAFT_STEP_OPC = {
    "research": "assessment", "idea": "assessment", "proposal": "assessment",
    "script": "copy", "scene_plan": "sample", "character_design": "sample",
    "rig_plan": "sample", "assets": "render", "edit": "sample_review",
    "compose": "final", "publish": "review",
}

def _load_craft_steps(craft_id):
    """读工艺定义的 stages 序列 → [{id,name,omStage,opcStage}]；失败回退通用 8 段。"""
    names = []
    try:
        import yaml
        with open(os.path.join(OM_DEFS_DIR, f"{craft_id}.yaml"), encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        names = [str(s.get("name") or "") for s in (data.get("stages") or [])
                 if isinstance(s, dict) and s.get("name")]
    except Exception as e:
        print(f"[boot] 工艺步骤读取失败 {craft_id}：{type(e).__name__}: {e}", flush=True)
    if not names:
        names = ["research", "proposal", "script", "scene_plan",
                 "assets", "edit", "compose", "publish"]
    return [{"id": n, "name": CRAFT_STEP_NAMES.get(n, n), "omStage": n,
             "opcStage": CRAFT_STEP_OPC.get(n, "final")} for n in names]

CRAFT_STEPS = {w["id"]: _load_craft_steps(w["id"]) for w in OPENMONTAGE_CRAFTS}

FACTORY_ENTRY_STAGE = "assessment"
FACTORY_EXIT_STAGE = "review"
DEFAULT_HIGH_RISK = ["publish.execute", "deploy.prod", "customer.reply", "ad.spend"]
PIPELINE = PIPE.PIPELINE          # 流程定义（热加载可重绑）
STAGES = PIPELINE.item_stages    # 内容单状态名，唯一来源是流程定义
HIGH_RISK_TOOLS = list(DEFAULT_HIGH_RISK)

DEFAULT_DELIVERY = {
    "_default": {"width": 1080, "height": 1920, "fps": "30/1", "video_codec": "h264",
                 "pix_fmt": ["yuv420p", "yuvj420p"], "audio_codec": "aac",
                 "sample_rate": 48000, "channels": 2,
                 "duration_tolerance": 2.5, "min_mean_db": -45.0, "max_peak_db": -1.0},
}

def _parse_delivery_cfg(path):
    """解析《项目配置》里的「交付合同」表；缺表则用内置默认（竖屏 1080×1920/30fps）。"""
    spec = {"_default": dict(DEFAULT_DELIVERY["_default"])}
    section, rows = "", 0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("## "):
                section = line[3:]
                continue
            if "交付合同" not in section or not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 10:
                continue
            pid = cells[0]
            if pid in ("项目", "项目 ID") or set(pid) <= set("-: "):
                continue
            try:
                one = dict(DEFAULT_DELIVERY["_default"])
                one.update(width=_to_int(cells[1], 1080), height=_to_int(cells[2], 1920),
                           fps=cells[3] or "30/1",
                           video_codec=cells[4] or "h264",
                           pix_fmt=[x.strip() for x in cells[5].replace("、", "|").split("|") if x.strip()]
                           or DEFAULT_DELIVERY["_default"]["pix_fmt"],
                           sample_rate=_to_int(cells[6], 48000),
                           channels=_to_int(cells[7], 2),
                           duration_tolerance=float(cells[8] or 2.5),
                           min_mean_db=float(cells[9] or -45.0),
                           max_peak_db=float(cells[10]) if len(cells) > 10 and cells[10] else -1.0)
            except (TypeError, ValueError):
                continue
            spec[pid] = one
            rows += 1
    return spec, rows

def _load_delivery_cfg():
    try:
        spec, rows = _parse_delivery_cfg(PROJECT_FILE)
        print(f"[boot] 交付合同 {PROJECT_FILE} → 专属 {rows} 项目（其余用默认 "
              f"{spec['_default']['width']}x{spec['_default']['height']}@{spec['_default']['fps']}）",
              flush=True)
        return spec
    except Exception as e:
        print(f"[boot] 交付合同解析失败({type(e).__name__})，使用内置默认", flush=True)
        return {"_default": dict(DEFAULT_DELIVERY["_default"])}

def _parse_project_cfg(path):
    projects, tone, stages, high = {}, {}, [], []
    section = ""
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("## "):
                section = line[3:]
                continue
            if line.startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                # 项目表固定 6 列；其余表格（如「交付合同」11 列）不按项目解析
                if len(cells) != 6:
                    continue
                pid = cells[0]
                if pid.startswith("项目") or set(pid) <= set("-: "):
                    continue
                kpi = {}
                for kv in cells[5].replace("，", "、").split("、"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        kpi[k.strip()] = _num(v.strip())
                projects[pid] = {"id": pid, "name": cells[1], "kind": cells[2],
                                 "color": cells[3], "kpi": kpi}
                tone[pid] = cells[4]
            elif line.startswith("- "):
                item = line[2:].strip().strip("`")
                if "阶段" in section:
                    stages.append(item)
                elif "高危" in section:
                    high.append(item)
    return projects, tone, stages, high

def _load_project_cfg():
    """项目/调性/高危工具来自 Obsidian；阶段名以流程定义 pipeline.yaml 为准。

    项目配置里的「内容流水线阶段」仅作历史视图保留，不再参与流程控制，
    避免代码实际使用的 sample/final/blocked 与配置里的 video 长期漂移。
    """
    try:
        p, t, s, h = _parse_project_cfg(PROJECT_FILE)
        if p and t and h:
            if s and list(s) != PIPE.PIPELINE.item_stages:
                print(f"[boot] 提示：项目配置的阶段列表与流程定义不一致，已以 pipeline.yaml 为准"
                      f"（配置 {s} → 实际 {PIPE.PIPELINE.item_stages}）", flush=True)
            print(f"[boot] 项目配置 {PROJECT_FILE} → {len(p)} 个项目 / {len(h)} 高危工具；"
                  f"流程阶段以 {PIPE.PIPELINE.source} 为准（{len(PIPE.PIPELINE.stages)} 段）", flush=True)
            return p, t, h
        print(f"[boot] 项目配置不完整(项目{len(p)} 调性{len(t)} 高危{len(h)})，回退内置默认",
              flush=True)
    except FileNotFoundError:
        print(f"[boot] 未找到 {PROJECT_FILE}，回退内置默认", flush=True)
    except Exception as e:
        print(f"[boot] 解析项目配置失败({e})，回退内置默认", flush=True)
    return dict(DEFAULT_PROJECTS), dict(DEFAULT_TONE), list(DEFAULT_HIGH_RISK)

DELIVERY = _load_delivery_cfg()

PROJECTS, TONE, HIGH_RISK_TOOLS = _load_project_cfg()
PROJECT_BY_NAME = {v["name"]: k for k, v in PROJECTS.items()}
PROJECT_BY_NAME["全公司"] = "all"     # 默认编制的 ceo/cco/cto/秘书等挂"全公司"

def _parse_roster(path, by_name=None):
    mapping = dict(by_name or PROJECT_BY_NAME)
    mapping.setdefault("全公司", "all")     # 默认编制的 ceo/cco/cto/秘书等挂"全公司"
    spec, skipped = [], 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 6:
                skipped += 1
                continue
            aid = cells[0]
            if aid.startswith("Agent") or set(aid) <= set("-: "):
                continue
            pid = mapping.get(cells[3])
            if not pid or not aid.replace("_", "").isalnum():
                skipped += 1
                continue
            tools = [t.strip() for t in cells[5].replace("，", "、").split("、") if t.strip()]
            spec.append((aid, cells[1], cells[2], pid, tools, cells[4]))
    return spec, skipped

def _load_agent_spec():
    try:
        spec, skipped = _parse_roster(ROSTER_FILE)
        if len(spec) >= 8:
            print(f"[boot] 员工名册 {ROSTER_FILE} → {len(spec)} 人（跳过 {skipped} 行）", flush=True)
            return spec
        print(f"[boot] 名册仅解析出 {len(spec)} 人，回退内置编制", flush=True)
    except FileNotFoundError:
        print(f"[boot] 未找到 {ROSTER_FILE}，回退内置编制", flush=True)
    except Exception as e:
        print(f"[boot] 解析名册失败({e})，回退内置编制", flush=True)
    return list(DEFAULT_AGENT_SPEC)

AGENT_SPEC = _load_agent_spec()
