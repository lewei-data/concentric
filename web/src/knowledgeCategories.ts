import type { KnowledgeCategory } from "./types";

export type KnowledgeTagDef = { key: string; label: string };

export type KnowledgeCategoryDef = {
  key: KnowledgeCategory;
  label: string;
  /** 五行汉字：土 / 水 / 金 / 火 / 木 */
  element: string;
  icon: string;
  slogan: string;
  description: string;
  color: string;
  tags: KnowledgeTagDef[];
};

/**
 * 知识库 5 大分类的单一事实源。
 * 前端兜底分类、知识图谱配色、图谱 tag 筛选都从这里取，避免多处定义漂移。
 * 后端 /api/knowledge/categories 仍优先（带真实 tag 统计与条数）。
 */
export const KB_CATEGORIES: KnowledgeCategoryDef[] = [
  {
    key: "analysis", label: "分析调研", element: "土", icon: "⛰️",
    slogan: "厚基、存真、稳态",
    description: "做厚事实底座，保障素材可信度与完整性；作为全体系的「土壤」，保持相对稳定",
    color: "#b45309",
    tags: [
      { key: "copywriting", label: "文案分析" }, { key: "short-video", label: "短视频分析" },
      { key: "market", label: "市场调研" }, { key: "competitor", label: "竞品分析" },
      { key: "trend", label: "趋势洞察" },
    ],
  },
  {
    key: "graph", label: "知识图谱", element: "水", icon: "🌊",
    slogan: "通联、溯源、无滞",
    description: "核心价值在「连通」而非「堆节点」，建设实体间的关联链路与因果传导路径",
    color: "#1e40af",
    tags: [
      { key: "data-source", label: "数据源关联" }, { key: "concept", label: "概念图谱" },
      { key: "workflow", label: "流程图谱" }, { key: "domain", label: "领域模型" },
      { key: "mgmt-benchmark", label: "管理层对标" },
    ],
  },
  {
    key: "skill", label: "技能", element: "金", icon: "⚔️",
    slogan: "精专、规整、耐用",
    description: "像铸造器物一样打磨每个 Skill，边界清晰、输入输出明确、异常处理完备",
    color: "#a16207",
    tags: [
      { key: "content", label: "内容创作" }, { key: "data", label: "数据分析" },
      { key: "design", label: "设计" }, { key: "ops", label: "运维" },
      { key: "management", label: "管理" }, { key: "research", label: "调研" },
    ],
  },
  {
    key: "decision", label: "决策模型", element: "火", icon: "🔥",
    slogan: "明断、守序、不妄",
    description: "清晰的判断逻辑与优先级秩序，规则明确、边界清晰；顶层范式不轻易变动",
    color: "#dc2626",
    tags: [
      { key: "domain-model", label: "领域模型" }, { key: "strategy", label: "战略决策" },
      { key: "risk", label: "风险评估" }, { key: "principle", label: "原则框架" },
      { key: "heuristic", label: "经验法则" },
    ],
  },
  {
    key: "reflection", label: "反馈迭代", element: "木", icon: "🌿",
    slogan: "循序、日新、有度",
    description: "遵循生长规律，小步持续优化；控制迭代节奏与灰度范围，避免拔苗助长",
    color: "#059669",
    tags: [
      { key: "retrospective", label: "复盘总结" }, { key: "optimization", label: "优化方案" },
      { key: "lesson", label: "经验教训" }, { key: "feedback", label: "用户反馈" },
      { key: "iteration", label: "迭代记录" },
    ],
  },
];

export const KB_CATEGORY_MAP: Record<KnowledgeCategory, KnowledgeCategoryDef> = KB_CATEGORIES.reduce(
  (acc, c) => { acc[c.key] = c; return acc; },
  {} as Record<KnowledgeCategory, KnowledgeCategoryDef>,
);

export const KB_CATEGORY_COLOR: Record<KnowledgeCategory, string> = KB_CATEGORIES.reduce(
  (acc, c) => { acc[c.key] = c.color; return acc; },
  {} as Record<KnowledgeCategory, string>,
);

/** 知识图谱分类的 tag（图谱页筛选按钮来源） */
export const KB_GRAPH_TAGS: KnowledgeTagDef[] = KB_CATEGORY_MAP.graph.tags;