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
