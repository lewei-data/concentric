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

export type Knowledge = {
  id: string;
  title: string;
  summary: string;
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
  copy?: string;
  storyboard?: string;
};
