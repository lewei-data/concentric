#!/usr/bin/env python3
"""内容流水线定义加载器（声明式，唯一事实来源）。

设计边界：
- 本模块只负责“读取 + 校验 + 查询”流程定义，不执行业务逻辑；执行器在
  server.py 的 Kernel._pipeline（按 stages 顺序调用 _stage_<id>）。
- 定义文件默认 config/pipeline.yaml，可用 OPC_PIPELINE_FILE 覆盖。
- 文件缺失 / YAML 不可用 / 校验失败时回退内置 DEFAULT_STAGES，并打印告警，
  保证服务仍能启动；回退内容与改造前 _pipeline 的行为完全一致。
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.normpath(os.path.join(BASE_DIR, "..", "config", "pipeline.yaml"))
FILE = os.environ.get("OPC_PIPELINE_FILE", DEFAULT_FILE)

# 执行器已实现的阶段 id；定义里出现未知 id 视为配置错误（防止拼写错误静默跳过）
KNOWN_STAGES = (
    "topic_meeting", "topic_review", "boss_report", "create_items",
    "production_approval", "assessment", "copy", "sample", "render",
    "sample_review", "final", "review", "publish_approval", "release",
    "archive", "report",
)
KINDS = ("produce", "gate", "approval", "terminal")


def _stage(pid, name, kind, item_stage="", om_stage="", rounds=2,
           approval_action="", timeout_sec=None, on_reject="stop", description=""):
    return {"id": pid, "name": name, "kind": kind, "item_stage": item_stage,
            "om_stage": om_stage, "rounds": rounds, "enabled": True,
            "approval_action": approval_action, "timeout_sec": timeout_sec,
            "on_reject": on_reject, "description": description}


# 与改造前 _pipeline 的阶段切分逐段对应，作为文件不可用时的兜底
DEFAULT_STAGES = [
    _stage("topic_meeting", "每日选题会", "produce", "topic", "research"),
    _stage("topic_review", "选题评审会", "gate", "topic", "proposal"),
    _stage("boss_report", "选题结果汇报", "produce"),
    _stage("create_items", "建内容单", "produce", "topic"),
    _stage("production_approval", "开工审批", "approval",
           approval_action="production.start"),
    _stage("assessment", "制作评估", "produce", "topic", "proposal"),
    _stage("copy", "文案成稿", "produce", "copy", "script"),
    _stage("sample", "样片方案", "produce", "sample", "scene_plan"),
    _stage("render", "真实出片", "gate", "sample", "assets"),
    _stage("sample_review", "样片三方评审", "gate", "sample", "edit"),
    _stage("final", "定版", "produce", "final", "compose"),
    _stage("review", "审核员终审", "gate", "review"),
    _stage("publish_approval", "发布审批", "approval", "approved",
           approval_action="publish.execute"),
    _stage("release", "发布", "produce", "published", "publish"),
    _stage("archive", "知识归档", "produce", "archived"),
    _stage("report", "汇报生成", "produce"),
]


class Stage:
    """一个阶段的只读视图。"""

    __slots__ = ("id", "name", "kind", "item_stage", "om_stage", "rounds", "enabled",
                 "approval_action", "timeout_sec", "on_reject", "description")

    def __init__(self, raw):
        self.id = str(raw.get("id") or "").strip()
        self.name = str(raw.get("name") or self.id)
        self.kind = str(raw.get("kind") or "produce").strip()
        self.item_stage = str(raw.get("item_stage") or "").strip()
        self.om_stage = str(raw.get("om_stage") or "").strip()
        self.rounds = max(1, int(raw.get("rounds") or 2))
        self.enabled = bool(raw.get("enabled", True))
        self.approval_action = str(raw.get("approval_action") or "").strip()
        self.timeout_sec = raw.get("timeout_sec")
        self.on_reject = str(raw.get("on_reject") or "stop").strip()
        self.description = str(raw.get("description") or "")

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


class Pipeline:
    def __init__(self, name, stages, version=1, description="", source="builtin"):
        self.name = name
        self.version = version
        self.description = description
        self.source = source
        self.stages = [s if isinstance(s, Stage) else Stage(s) for s in stages]
        self.by_id = {s.id: s for s in self.stages}

    @property
    def item_stages(self):
        """内容单对外暴露的状态名（按流程顺序去重），供 API/前端使用。"""
        seen, out = set(), []
        for s in self.stages:
            if s.item_stage and s.item_stage not in seen:
                seen.add(s.item_stage)
                out.append(s.item_stage)
        return out

    def stage(self, sid):
        return self.by_id.get(sid)

    def item_stage_of(self, sid):
        st = self.by_id.get(sid)
        return st.item_stage if st else ""


def _validate(stages):
    ids = [s.id for s in stages]
    if not ids:
        raise ValueError("流程定义没有任何阶段")
    dup = {x for x in ids if ids.count(x) > 1}
    if dup:
        raise ValueError(f"阶段 id 重复：{sorted(dup)}")
    for s in stages:
        if s.id not in KNOWN_STAGES:
            raise ValueError(f"未知阶段 id：{s.id}（执行器未实现）")
        if s.kind not in KINDS:
            raise ValueError(f"阶段 {s.id} 的 kind 非法：{s.kind}")
        if s.kind == "approval" and not s.approval_action:
            raise ValueError(f"审批阶段 {s.id} 缺少 approval_action")
    missing = [x for x in KNOWN_STAGES if x not in ids]
    if missing:
        raise ValueError(f"流程定义缺少阶段：{missing}")


def load(path=None):
    """读取流程定义；任何异常都回退内置默认并返回 (Pipeline, 错误信息)。"""
    path = path or FILE
    try:
        import yaml
    except Exception as e:                      # 无 PyYAML 时用内置换算，不阻断启动
        return Pipeline("opc-content-pipeline", DEFAULT_STAGES, source="builtin"), \
            f"PyYAML 不可用({type(e).__name__})，使用内置流程定义"

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        stages = [Stage(x) for x in (raw.get("stages") or [])]
        _validate(stages)
        pipe = Pipeline(raw.get("name") or "opc-content-pipeline", stages,
                        version=raw.get("version") or 1,
                        description=str(raw.get("description") or ""),
                        source=path)
        return pipe, ""
    except FileNotFoundError:
        return Pipeline("opc-content-pipeline", DEFAULT_STAGES, source="builtin"), \
            f"未找到 {path}，使用内置流程定义"
    except Exception as e:
        return Pipeline("opc-content-pipeline", DEFAULT_STAGES, source="builtin"), \
            f"解析 {path} 失败({type(e).__name__}: {e})，使用内置流程定义"


PIPELINE, LOAD_NOTE = load()
