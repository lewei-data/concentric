/**
 * 三才（天 / 地 / 人）：知识库的第二分类维，与五行分类正交。
 *
 * 口径（与 prototype/sancai.py 一致，改一处必须同步另一处）：
 *   - 8 个显性 = 五行 5（分类）+ 三才 3（本文件）；
 *   - 7 个隐性 = 五行之间的关系（生 / 克 / 乘 / 侮 / 母病及子 / 子病犯母 / 制化），
 *     由系统推导，不作为标签展示，只在知识图谱的边上呈现。
 *
 * 显性标签的用处：一个对象（素材 / 热点 / 知识 / 作品 / 处理阶段）永远同时有
 * 一个五行归属和一个才位归属；可只按一个维度筛，也可两维叠加。
 */

export type SanCai = "tian" | "di" | "ren";

export interface SanCaiDef {
  key: SanCai;
  label: string;
  verb: string;
  color: string;
  slogan: string;
  desc: string;
  /** 单字图形，用于徽标 */
  mark: string;
}

export const SANCAI: SanCaiDef[] = [
  {
    key: "tian", label: "天", verb: "取势", color: "#2563eb", slogan: "时也、势也", mark: "天",
    desc: "外部输入与对外输出：热点、趋势、平台规则、时机判断、选题拍板与发布",
  },
  {
    key: "di", label: "地", verb: "载物", color: "#b45309", slogan: "厚载、存真", mark: "地",
    desc: "素材与事实底座：素材库、调研数据、图谱索引、归档沉淀",
  },
  {
    key: "ren", label: "人", verb: "成事", color: "#059669", slogan: "裁断、成器", mark: "人",
    desc: "人的加工与判断：选题决策、文案成稿、出片、评审、复盘",
  },
];

export const SANCAI_KEYS: SanCai[] = ["tian", "di", "ren"];
export const SANCAI_MAP: Record<SanCai, SanCaiDef> = SANCAI.reduce(
  (acc, c) => { acc[c.key] = c; return acc; },
  {} as Record<SanCai, SanCaiDef>,
);

const ALIAS: Record<string, SanCai> = {
  天: "tian", tian: "tian", heaven: "tian",
  地: "di", di: "di", earth: "di",
  人: "ren", ren: "ren", human: "ren",
};

/** 把 tian / 天 / heaven 统一成 key；不认识返回空 */
export function sancaiKey(value?: string | null): SanCai | "" {
  return ALIAS[String(value ?? "").trim().toLowerCase()] ?? "";
}

/** 对象级默认：素材属地、热点属天、作品属人 */
export const SANCAI_DEFAULT: Record<string, SanCai> = {
  material: "di", hotspot: "tian", work: "ren", stage: "ren", knowledge: "di",
};

/** 与后端一致的关键词表，用于后端没给 sancai 时的兜底 */
const KEYWORDS: Array<[SanCai, string[]]> = [
  ["tian", ["热点", "趋势", "时机", "风口", "政策", "平台规则", "榜单", "节气", "当令",
    "信号", "外部", "行业", "市场", "竞品", "流量", "算法推荐"]],
  ["di", ["素材", "数据", "事实", "调研", "案例", "图谱", "索引", "归档", "底稿",
    "文献", "样本", "存储", "血缘", "库表", "原始"]],
  ["ren", ["文案", "脚本", "作品", "成稿", "出片", "剪辑", "拍摄", "审核", "评审",
    "复盘", "技能", "决策", "团队", "表达", "方案", "判断"]],
];

/** 流水线 15 阶段 → 才位（取势在天、沉淀在地、加工在人） */
export const STAGE_SANCAI: Record<string, SanCai> = {
  topic_meeting: "di", boss_report: "tian", create_items: "ren", production_approval: "tian",
  assessment: "ren", copy: "ren", sample: "ren", render: "ren", sample_review: "ren",
  final: "ren", review: "ren", publish_approval: "tian", release: "tian",
  archive: "di", report: "tian",
};

/** 作品档案自身阶段 → 才位 */
export const WORK_STAGE_SANCAI: Record<string, SanCai> = {
  topic: "tian", copy: "ren", video: "ren", factory: "ren", review: "ren", archive: "di",
};

export function stageSancai(stage?: string): SanCai {
  return STAGE_SANCAI[String(stage ?? "")] ?? "ren";
}

export function workStageSancai(stage?: string): SanCai {
  return WORK_STAGE_SANCAI[String(stage ?? "")] ?? "ren";
}

/** 兜底推断：显式字段 → 标签 → 标题摘要 → 对象默认 */
export function sancaiOf(
  item: { sancai?: string; tags?: string[]; title?: string; summary?: string; category?: string } | null | undefined,
  kind: keyof typeof SANCAI_DEFAULT | string = "knowledge",
): SanCai {
  if (!item) return SANCAI_DEFAULT[kind] ?? "ren";
  const hit = sancaiKey(item.sancai);
  if (hit) return hit;
  const tags = (item.tags ?? []).join(" ");
  for (const [key, words] of KEYWORDS) {
    if (words.some((w) => tags.includes(w))) return key;
  }
  const text = `${item.title ?? ""} ${item.summary ?? ""}`;
  for (const [key, words] of KEYWORDS) {
    if (words.some((w) => text.includes(w))) return key;
  }
  return SANCAI_DEFAULT[kind] ?? "ren";
}

/** 五行之间的关系（传统生克乘侮）：知识图谱边上的解释，不做筛选标签 */
export const SHENGKE_RELATIONS: Array<{ key: string; label: string; desc: string }> = [
  { key: "sheng", label: "相生", desc: "木→火→土→金→水：上一环的结果成为下一环的输入" },
  { key: "ke", label: "相克", desc: "约束项，防止单一环节独大" },
  { key: "cheng", label: "相乘", desc: "克得过头：约束变成压制" },
  { key: "wu", label: "相侮", desc: "反被克：被约束的一方反噬约束方" },
  { key: "mother_child", label: "母病及子", desc: "上游带病下传" },
  { key: "child_mother", label: "子病犯母", desc: "下游反推上游标准" },
  { key: "zhihua", label: "制化", desc: "生克同用后的自我修复" },
];

/* ────────────────────────────── 8 显性 / 7 隐性 ──────────────────────────────
 * 8 显性 = 5 行（每行 1~2 个锚点节点）+ 3 才（天地人，作为固定维度）。
 * 7 隐性 = 5 行 × 3 才 的 15 格中，落在「输入端 / 处理端 / 输出端」的 7 个格子；
 *           它们不做一级节点，封装成内部流转型原子 Skill，托管在「金行·技能资产」。
 * 这份模型是知识库分类面板与 /api/sancai 的共同口径，改这里要同步 prototype/sancai.py。
 */

export type Element = "wood" | "fire" | "earth" | "metal" | "water";

export const ELEMENT_LABEL: Record<Element, string> = {
  wood: "木", fire: "火", earth: "土", metal: "金", water: "水",
};

/** 后端 categories[].element 给的是中文单字，这里做一次映射 */
export function elementOf(cn?: string | null): Element | "" {
  const map: Record<string, Element> = {
    木: "wood", 火: "fire", 土: "earth", 金: "metal", 水: "water",
  };
  return map[String(cn ?? "").trim()] ?? "";
}

export interface XianNode {
  key: string;
  /** 单字卦名：坤艮坎离乾兑震巽 */
  symbol: string;
  name: string;
  element: Element;
  polarity: "yin" | "yang" | "all";
  position: string;
  /** 人机分工：人做什么，Skill 做什么 */
  role: string;
  skills: string[];
}

export const XIAN_8: XianNode[] = [
  {
    key: "kun", symbol: "坤", name: "坤元仓廪", element: "earth", polarity: "yin", position: "输入",
    role: "人筛素材源、定归档分类；Skill 批量采集、自动去重、元信息补全、初步分拣",
    skills: ["素材批量采集", "去重分拣", "元信息自动补全"],
  },
  {
    key: "gen", symbol: "艮", name: "艮山质检关", element: "earth", polarity: "yang", position: "输出",
    role: "人审结构化结果、校准可信度、确认归档版本；Skill 自动校验、评可信度、打版本、提醒过期",
    skills: ["素材质量校验", "可信度评级", "版本管理"],
  },
  {
    key: "kan", symbol: "坎", name: "坎水渠网", element: "water", polarity: "all", position: "中枢",
    role: "人定关系规则、审核核心实体、校准推理逻辑；Skill 建图谱、对齐实体、查链路、推演传导",
    skills: ["图谱构建", "实体对齐", "链路溯源", "情景推演"],
  },
  {
    key: "li", symbol: "离", name: "离火中枢塔", element: "fire", polarity: "all", position: "决策中枢",
    role: "人定决策范式、调权重边界、审高风险结论；Skill 范式匹配、自动判定、生成理由链、预警风险",
    skills: ["范式路由", "规则判定", "加权打分", "风险预警"],
  },
  {
    key: "qian", symbol: "乾", name: "乾金机作坊", element: "metal", polarity: "yang", position: "执行",
    role: "人处理异常、确认复杂操作、验收结果；Skill 执行原子能力、编排流程、异常重试、回填结果",
    skills: ["原子能力执行", "流程编排", "异常重试", "结果回填"],
  },
  {
    key: "dui", symbol: "兑", name: "兑泽交付台", element: "metal", polarity: "yin", position: "交付",
    role: "人调交付口径、优化呈现、确认最终输出；Skill 封装格式、对齐口径、润色内容、多形态导出",
    skills: ["格式封装", "口径对齐", "内容润色", "多格式导出"],
  },
  {
    key: "zhen", symbol: "震", name: "震雷讯号台", element: "wood", polarity: "yang", position: "触发",
    role: "人补主观反馈、定问题等级、标特殊场景；Skill 埋点采集、信号分级、异常告警、问题打标",
    skills: ["埋点采集", "信号分级", "异常告警", "问题打标"],
  },
  {
    key: "xun", symbol: "巽", name: "巽风润化坊", element: "wood", polarity: "yin", position: "落地",
    role: "人审优化方案、确认变更范围、验证效果；Skill 自动更新资产、灰度发布、回归测试、变更留痕",
    skills: ["资产自动更新", "灰度发布", "回归测试", "变更日志"],
  },
];

export interface YinStep {
  key: string;
  element: Element;
  sancai: SanCai;
  name: string;
  stage: "输入端" | "处理端" | "输出端";
  /** 衔接哪两个显性节点 */
  bridge: string;
  skills: string[];
}

export const YIN_7: YinStep[] = [
  {
    key: "earth_ren", element: "earth", sancai: "ren", name: "素材结构化加工", stage: "处理端",
    bridge: "坤 → 艮",
    skills: ["文档结构化抽取", "自动标签打标", "素材可信度初评", "过期信息识别"],
  },
  {
    key: "water_di", element: "water", sancai: "di", name: "实体关系抽取", stage: "输入端",
    bridge: "艮 → 坎",
    skills: ["实体关系三元组抽取", "实体消歧与对齐", "同义词映射"],
  },
  {
    key: "water_tian", element: "water", sancai: "tian", name: "图谱关联推理服务", stage: "输出端",
    bridge: "坎 → 决策/执行",
    skills: ["图谱链路溯源", "实体关联推荐", "多跳推理", "情景传导测算"],
  },
  {
    key: "fire_di", element: "fire", sancai: "di", name: "决策要素归集", stage: "输入端",
    bridge: "召回 → 离",
    skills: ["决策上下文组装", "相似案例匹配", "约束条件校验", "特征标准化"],
  },
  {
    key: "fire_tian", element: "fire", sancai: "tian", name: "决策指令封装", stage: "输出端",
    bridge: "离 → 乾",
    skills: ["决策理由链组装", "执行指令生成", "结论边界标注"],
  },
  {
    key: "metal_di", element: "metal", sancai: "di", name: "指令解析与参数校验", stage: "输入端",
    bridge: "决策 → 乾",
    skills: ["指令解析与参数提取", "参数合法性校验", "依赖环境检查", "Skill 路由匹配"],
  },
  {
    key: "wood_ren", element: "wood", sancai: "ren", name: "自动归因与方案生成", stage: "处理端",
    bridge: "震 → 巽",
    skills: ["双维自动归因", "根因定位", "优化方案生成", "问题等级评定"],
  },
];

export const xianNodesOf = (el: Element | "") =>
  el ? XIAN_8.filter((n) => n.element === el) : [];

export const yinStepsOf = (el: Element | "") =>
  el ? YIN_7.filter((s) => s.element === el) : [];

export const yinStepOf = (el: Element | "", sc: SanCai | "") =>
  el && sc ? YIN_7.find((s) => s.element === el && s.sancai === sc) ?? null : null;