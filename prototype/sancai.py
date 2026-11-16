"""三才（天 / 地 / 人）分类维：与五行分类正交的第二维。

口径（与《知识库·五行三才双维》一致）：

- **8 个显性**：五行 5（木火土金水，已落为 knowledge.category）+ 三才 3（天地人，本模块）。
  显性 = 人工可标、界面可筛、可统计。
- **7 个隐性**：五行之间的关系（相生 / 相克 / 相乘 / 相侮 / 母病及子 / 子病犯母 / 制化），
  由系统从正文与标签推导，**不落成标签**，只在图谱边上体现。见 RELATIONS_7。

取值一律用英文 key（tian/di/ren），中文只做展示，避免引入大小写与全半角差异。
"""
from __future__ import annotations

SANCAI = [
    {
        "key": "tian", "label": "天", "verb": "取势", "color": "#2563eb",
        "slogan": "时也、势也",
        "desc": "外部输入与对外输出：热点、趋势、平台规则、时机判断、选题拍板与发布",
    },
    {
        "key": "di", "label": "地", "verb": "载物", "color": "#b45309",
        "slogan": "厚载、存真",
        "desc": "素材与事实底座：素材库、调研数据、图谱索引、归档沉淀",
    },
    {
        "key": "ren", "label": "人", "verb": "成事", "color": "#059669",
        "slogan": "裁断、成器",
        "desc": "人的加工与判断：选题决策、文案成稿、出片、评审、复盘",
    },
]
SANCAI_KEYS = ("tian", "di", "ren")
SANCAI_MAP = {c["key"]: c for c in SANCAI}
SANCAI_LABEL = {c["key"]: c["label"] for c in SANCAI}

# 中英文与单字都收，接口传「天」也能用
_ALIAS = {
    "天": "tian", "tian": "tian", "heaven": "tian",
    "地": "di", "di": "di", "earth": "di",
    "人": "ren", "ren": "ren", "human": "ren",
}

# 7 个隐性：五行之间的关系，不落标签，只进图谱边
RELATIONS_7 = [
    {"key": "sheng", "label": "相生", "desc": "木→火→土→金→水→木：一条链路把上一环的结果变成下一环的输入"},
    {"key": "ke", "label": "相克", "desc": "木克土、土克水、水克火、火克金、金克木：约束项，防止单一环节独大"},
    {"key": "cheng", "label": "相乘", "desc": "克得过头：约束变成压制，例：审核流程压死产出节奏"},
    {"key": "wu", "label": "相侮", "desc": "反被克：被约束的一方反噬约束方，例：素材质量拖垮决策"},
    {"key": "mother_child", "label": "母病及子", "desc": "上游带病下传：素材缺源 → 分析失真 → 决策跑偏"},
    {"key": "child_mother", "label": "子病犯母", "desc": "下游反推上游：发布数据差 → 反查选题与素材标准"},
    {"key": "zhihua", "label": "制化", "desc": "生克同用后的自我修复：加一道约束把系统拉回平衡"},
]

# 五行分类（已有）对应的默认才位，仅在无标签、无关键词命中时兜底
CATEGORY_SANCAI = {
    "analysis": "di",    # 分析调研 → 事实底座
    "graph": "di",       # 知识图谱 → 连通与溯源
    "skill": "ren",      # 技能 → 人打磨的器物
    "decision": "ren",   # 决策模型 → 人下的判断
    "reflection": "ren", # 反馈迭代 → 人复盘
}

# 关键词优先级：先看标签、再看标题摘要；天 → 地 → 人
KEYWORDS = (
    ("tian", ("热点", "趋势", "时机", "风口", "政策", "平台规则", "榜单", "节气", "当令",
              "信号", "外部", "行业", "市场", "竞品", "流量", "算法推荐")),
    ("di", ("素材", "数据", "事实", "调研", "案例", "图谱", "索引", "归档", "底稿",
            "文献", "样本", "存储", "血缘", "库表", "原始")),
    ("ren", ("文案", "脚本", "作品", "成稿", "出片", "剪辑", "拍摄", "审核", "评审",
             "复盘", "技能", "决策", "团队", "表达", "方案", "判断")),
)

# 对象级默认才位（没有任何线索时用）
OBJECT_DEFAULT = {
    "material": "di",   # 素材 = 地
    "hotspot": "tian",  # 热点 = 天
    "work": "ren",      # 作品 = 人
    "stage": "ren",     # 处理过程兜底
    "knowledge": "di",
}

# 15 个流水线阶段 → 才位：取势在天、沉淀在地、加工在人
STAGE_SANCAI = {
    "topic_meeting": "di",       # 每日选题（知识库内化）：从存量里翻
    "boss_report": "tian",       # 选题结果汇报：对上拍板
    "create_items": "ren",       # 建内容单：人开工
    "production_approval": "tian",  # 开工审批：拍板
    "assessment": "ren",         # 制作评估：人判断
    "copy": "ren",               # 文案成稿
    "sample": "ren",             # 样片方案
    "render": "ren",             # 真实出片
    "sample_review": "ren",      # 样片评审
    "final": "ren",              # 定版
    "review": "ren",             # 审核员终审
    "publish_approval": "tian",  # 发布审批
    "release": "tian",           # 发布：对外
    "archive": "di",             # 知识归档：沉淀
    "report": "tian",            # 汇报生成：对外
}


def norm(value) -> str:
    """把 tian/天/heaven 之类统一成 key；不认识返回空串。"""
    return _ALIAS.get(str(value or "").strip().lower(), "")


# 作品档案自身的阶段键（works.STEP/STAGE_LABELS 口径），与流水线 15 阶段不同名
WORK_STAGE_SANCAI = {
    "topic": "tian",     # 选题：取势
    "copy": "ren",       # 文案：人写
    "video": "ren",      # 视频：人做
    "factory": "ren",    # 工厂制作
    "review": "ren",     # 评审
    "archive": "di",     # 归档
}


def work_stage_of(stage) -> str:
    """作品阶段 → 才位；未知一律人（作品是人的产物）。"""
    return WORK_STAGE_SANCAI.get(str(stage or "").strip()) or OBJECT_DEFAULT["work"]


def stage_of(stage_key) -> str:
    """阶段 key → 才位；不认识默认人（加工）。"""
    return STAGE_SANCAI.get(str(stage_key or "").strip()) or OBJECT_DEFAULT["stage"]


def infer(item, kind="knowledge", default="") -> str:
    """按「显式字段 → 标签 → 标题摘要 → 五行分类 → 对象默认」推断才位。

    显式字段优先，保证人工标注不被算法覆盖。
    """
    if not isinstance(item, dict):
        return default or OBJECT_DEFAULT.get(kind, "ren")
    hit = norm(item.get("sancai"))
    if hit:
        return hit
    tags = " ".join(str(t) for t in (item.get("tags") or []))
    for key, words in KEYWORDS:
        if any(w in tags for w in words):
            return key
    text = "{} {}".format(item.get("title") or "", item.get("summary") or "")
    for key, words in KEYWORDS:
        if any(w in text for w in words):
            return key
    cat = str(item.get("category") or "").strip()
    if cat in CATEGORY_SANCAI:
        return CATEGORY_SANCAI[cat]
    # 素材/热点的 category 是中文自由分类，先按词命中再落对象默认
    for key, words in KEYWORDS:
        if any(w in cat for w in words):
            return key
    return default or OBJECT_DEFAULT.get(kind, "ren")


def counts(items, kind="knowledge") -> dict:
    """统计一组对象的三才分布，返回 {tian: n, di: n, ren: n, total: n}。"""
    out = {k: 0 for k in SANCAI_KEYS}
    total = 0
    for it in items or []:
        out[infer(it, kind)] += 1
        total += 1
    out["total"] = total
    return out


# 显性 8 节点：5 行 × 1~2 个锚点（卦名 + 节点名 + 人机分工）
XIAN_8 = [
    {"key": "kun", "symbol": "坤", "name": "坤元仓廪", "element": "earth", "polarity": "yin",
     "position": "输入", "skills": ["素材批量采集", "去重分拣", "元信息自动补全"]},
    {"key": "gen", "symbol": "艮", "name": "艮山质检关", "element": "earth", "polarity": "yang",
     "position": "输出", "skills": ["素材质量校验", "可信度评级", "版本管理"]},
    {"key": "kan", "symbol": "坎", "name": "坎水渠网", "element": "water", "polarity": "all",
     "position": "中枢", "skills": ["图谱构建", "实体对齐", "链路溯源", "情景推演"]},
    {"key": "li", "symbol": "离", "name": "离火中枢塔", "element": "fire", "polarity": "all",
     "position": "决策中枢", "skills": ["范式路由", "规则判定", "加权打分", "风险预警"]},
    {"key": "qian", "symbol": "乾", "name": "乾金机作坊", "element": "metal", "polarity": "yang",
     "position": "执行", "skills": ["原子能力执行", "流程编排", "异常重试", "结果回填"]},
    {"key": "dui", "symbol": "兑", "name": "兑泽交付台", "element": "metal", "polarity": "yin",
     "position": "交付", "skills": ["格式封装", "口径对齐", "内容润色", "多格式导出"]},
    {"key": "zhen", "symbol": "震", "name": "震雷讯号台", "element": "wood", "polarity": "yang",
     "position": "触发", "skills": ["埋点采集", "信号分级", "异常告警", "问题打标"]},
    {"key": "xun", "symbol": "巽", "name": "巽风润化坊", "element": "wood", "polarity": "yin",
     "position": "落地", "skills": ["资产自动更新", "灰度发布", "回归测试", "变更日志"]},
]

# 隐性 7 环节：5 行 × 3 才 的 15 格中落在输入端 / 处理端 / 输出端的 7 格
YIN_7 = [
    {"key": "earth_ren", "element": "earth", "sancai": "ren", "name": "素材结构化加工",
     "stage": "处理端", "bridge": "坤 → 艮",
     "skills": ["文档结构化抽取", "自动标签打标", "素材可信度初评", "过期信息识别"]},
    {"key": "water_di", "element": "water", "sancai": "di", "name": "实体关系抽取",
     "stage": "输入端", "bridge": "艮 → 坎",
     "skills": ["实体关系三元组抽取", "实体消歧与对齐", "同义词映射"]},
    {"key": "water_tian", "element": "water", "sancai": "tian", "name": "图谱关联推理服务",
     "stage": "输出端", "bridge": "坎 → 决策/执行",
     "skills": ["图谱链路溯源", "实体关联推荐", "多跳推理", "情景传导测算"]},
    {"key": "fire_di", "element": "fire", "sancai": "di", "name": "决策要素归集",
     "stage": "输入端", "bridge": "召回 → 离",
     "skills": ["决策上下文组装", "相似案例匹配", "约束条件校验", "特征标准化"]},
    {"key": "fire_tian", "element": "fire", "sancai": "tian", "name": "决策指令封装",
     "stage": "输出端", "bridge": "离 → 乾",
     "skills": ["决策理由链组装", "执行指令生成", "结论边界标注"]},
    {"key": "metal_di", "element": "metal", "sancai": "di", "name": "指令解析与参数校验",
     "stage": "输入端", "bridge": "决策 → 乾",
     "skills": ["指令解析与参数提取", "参数合法性校验", "依赖环境检查", "Skill 路由匹配"]},
    {"key": "wood_ren", "element": "wood", "sancai": "ren", "name": "自动归因与方案生成",
     "stage": "处理端", "bridge": "震 → 巽",
     "skills": ["双维自动归因", "根因定位", "优化方案生成", "问题等级评定"]},
]


def describe() -> dict:
    """对外口径：8 个显性（5 行 + 3 才节点）与 7 个隐性（行×才 内部工序）。"""
    return {
        "explicit": {
            "count": 8,
            "wuxing": [{"key": "wood", "label": "木"}, {"key": "fire", "label": "火"},
                       {"key": "earth", "label": "土"}, {"key": "metal", "label": "金"},
                       {"key": "water", "label": "水"}],
            "sancai": SANCAI,
            "nodes": XIAN_8,
            "note": "五行定「性质」，三才定「所处」；两者正交，可单独筛，也可叠加筛。"
                    "显性节点是人机协同锚点：人做验收与边界，Skill 做执行与标准化。",
        },
        "implicit": {
            "count": 7,
            "steps": YIN_7,
            "relations": RELATIONS_7,
            "note": "7 个隐性环节 = 行×才 格子里的输入端 / 处理端 / 输出端工序，"
                    "封装为内部流转型原子 Skill（托管于金行·技能资产），可观测、可干预、可版本化；"
                    "relations 是五行生克乘侮，只做图谱边上的解释。",
        },
    }