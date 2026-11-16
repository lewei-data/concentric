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

/** 7 个隐性关系：只解释、不筛选 */
export const RELATIONS_7: Array<{ key: string; label: string; desc: string }> = [
  { key: "sheng", label: "相生", desc: "木→火→土→金→水：上一环的结果成为下一环的输入" },
  { key: "ke", label: "相克", desc: "约束项，防止单一环节独大" },
  { key: "cheng", label: "相乘", desc: "克得过头：约束变成压制" },
  { key: "wu", label: "相侮", desc: "反被克：被约束的一方反噬约束方" },
  { key: "mother_child", label: "母病及子", desc: "上游带病下传" },
  { key: "child_mother", label: "子病犯母", desc: "下游反推上游标准" },
  { key: "zhihua", label: "制化", desc: "生克同用后的自我修复" },
];