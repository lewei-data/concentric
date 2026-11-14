export type Hotspot = {
  id: string;
  traceId?: string;
  title: string;
  source?: string;
  score?: number;
  tags?: string[];
  at?: string;
  fit?: Record<string, unknown>;
};

/** 知识库 5 大分类 */
export type KnowledgeCategory = "skill" | "analysis" | "graph" | "decision" | "reflection";

export type KnowledgeTagDef = {
  key: string;
  label: string;
};

export type CategoryDef = {
  key: KnowledgeCategory;
  label: string;
  element: string;
  icon: string;
  slogan: string;
  description: string;
  color: string;
  tags: KnowledgeTagDef[];
};

export type Knowledge = {
  id: string;
  title: string;
  summary: string;
  category?: KnowledgeCategory;
  projectId?: string;
  traceId?: string;
  tags?: string[];
  at?: string;
  body?: string;
};

export type OpcUser = {
  id?: string;
  user?: string;
  name?: string;
  role?: string;
  email?: string;
  avatar?: string;
};

export type Agent = {
  id: string;
  name: string;
  role: string;
  projectId?: string;
  scope?: string;
  tools?: string[];
  status?: "idle" | "working" | "meeting" | "done" | string;
  task?: string;
  avatar?: string;
  tone?: string;
  highRisk?: string[];
};

export type Project = {
  id: string;
  name: string;
  color?: string;
  kind?: string;
  kpi?: Record<string, number>;
};

export type OpcEvent = {
  id?: string;
  traceId?: string;
  type?: string;
  sender?: string;
  recipient?: string;
  at?: string;
  ttl?: number;
  priority?: number;
  projectId?: string;
  payload?: {
    text?: string;
    step?: string;
    result?: { summary?: string };
    verdict?: { pass?: boolean };
  };
};

export type Approval = {
  id: string;
  traceId?: string;
  action?: string;
  risk?: string;
  status?: "pending" | "approved" | "rejected" | string;
  approver?: string;
  createdAt?: string;
  decidedAt?: string;
  title?: string;
  items?: unknown[];
  checks?: Record<string, any>;
};

export type OpcState = {
  agents: Agent[];
  projects: Project[];
  approvals: Approval[];
  eventsList: OpcEvent[];
  meeting?: { active?: boolean; participants?: string[]; title?: string };
  stats?: Record<string, any>;
  serverTime?: string;
};

export type WorkFile = {
  key: string;
  label: string;
  exists: boolean;
  size: number;
  at: string;
  url: string;
};

export type WorkStep = {
  key: string;
  label: string;
  at: string;
  exists: boolean;
};

export type Work = {
  id: string;
  code: string;
  title: string;
  projectId: string;
  projectName: string;
  traceId: string;
  contentId: string;
  stage: string;
  stageName: string;
  status: string;
  statusKey: string;
  owner: string;
  toneProfile?: string;
  createdAt: string;
  updatedAt: string;
  folder: string;
  files: WorkFile[];
  assetCount: number;
  completeness: number;
  reviewNote: string;
  reviewPass: boolean | null;
  assessment: string;
  /* 工厂车间对齐字段（product_crafts 为准；无工厂记录的历史档案没有这些字段） */
  serialNo?: string;
  workshopNo?: string;
  craftId?: string;
  craftName?: string;
  decidedAt?: string;
  shippedAt?: string;
  craftSteps?: Array<{
    id: string;
    name: string;
    omStage: string;
    opcStage: string;
    status: string;
  }>;
  craftProgress?: string;
  render?: { ok: boolean; video: string; reason: string };
  renderCheck?: { pass: boolean; reasons: string[] };
  hasFinal: boolean;
  hasPreview: boolean;
  approvals?: Array<{
    id?: string;
    action?: string;
    risk?: string;
    status?: string;
    approver?: string;
    createdAt?: string;
    decidedAt?: string;
    title?: string;
  }>;
  timeline: WorkStep[];
  docs?: Record<string, { label: string; text: string }>;
};
