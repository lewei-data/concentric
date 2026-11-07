import { useEffect, useState } from "react";

type Agent = {
  id: string;
  name: string;
  role: string;
  projectId: string;
  project: string;
  scope: string;
  tone: string;
  tools: string[];
  profile: boolean;
  hasSoul: boolean;
  skills: { name: string; bytes: number; updated: string }[];
  memoryChars: { memory: number; user: number };
};

type AgentDetail = Agent & {
  soul: string;
  memory: string;
  user: string;
  config: string;
  note?: string;
};

type Policy = { key: string; title: string; path: string; editable: boolean; exists: boolean; bytes: number };
type Project = { id: string; name: string };
type Audit = { ts: string; by: string; role: string; action: string; target: string; detail: string };

const TABS = [
  ["staff", "人员与岗位"],
  ["policy", "制度"],
  ["soul", "性格与技能"],
  ["memory", "记忆"],
  ["advanced", "高级与审计"],
] as const;

type Tab = (typeof TABS)[number][0];

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { credentials: "include", ...init });
  const json = (await res.json().catch(() => ({}))) as T & { ok?: boolean; error?: string };
  if (!res.ok || json.ok === false) {
    throw new Error(json.error || `HTTP ${res.status}`);
  }
  return json;
}

function post<T>(url: string, body: unknown) {
  return api<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function OrgPage() {
  const [tab, setTab] = useState<Tab>("staff");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [policyKey, setPolicyKey] = useState<string>("jobs");
  const [policy, setPolicy] = useState<{ title: string; content: string; editable: boolean; path: string } | null>(null);
  const [skillName, setSkillName] = useState("");
  const [skillBody, setSkillBody] = useState("");
  const [audits, setAudits] = useState<Audit[]>([]);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const loadOrg = async () => {
    try {
      const data = await api<{ agents: Agent[]; policies: Policy[]; projects: Project[] }>("/api/org");
      setAgents(data.agents);
      setPolicies(data.policies);
      setProjects(data.projects);
      setErr("");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  useEffect(() => {
    void loadOrg();
  }, []);

  const selectAgent = async (id: string) => {
    setSelected(id);
    setMsg("");
    try {
      const data = await api<{ agent: AgentDetail }>(`/api/org/agent/${id}`);
      setDetail(data.agent);
      setSkillName(data.agent.skills[0]?.name ?? "");
      setSkillBody("");
      setErr("");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  const loadPolicy = async (key: string) => {
    setPolicyKey(key);
    try {
      const data = await api<{ title: string; content: string; editable: boolean; path: string }>(
        `/api/org/policy?key=${encodeURIComponent(key)}`,
      );
      setPolicy(data);
      setErr("");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  useEffect(() => {
    if (tab === "policy" && !policy) void loadPolicy("jobs");
    if (tab === "advanced") {
      void api<{ items: Audit[] }>("/api/org/audit?limit=30").then((d) => setAudits(d.items)).catch(() => undefined);
    }
  }, [tab]);

  const saveStaff = async () => {
    if (!detail) return;
    try {
      const data = await post<{ agent: AgentDetail; soulSynced: boolean }>(`/api/org/agent/${detail.id}`, {
        name: detail.name,
        role: detail.role,
        projectId: detail.projectId,
        scope: detail.scope,
        tools: detail.tools,
      });
      setDetail(data.agent);
      await loadOrg();
      setMsg(`已保存：名册、组织编制${data.soulSynced ? "、SOUL 头部" : ""} 已同步，配置已热加载`);
      setErr("");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  const saveSoul = async () => {
    if (!detail) return;
    try {
      await post(`/api/org/agent/${detail.id}/soul`, { content: detail.soul });
      setMsg("SOUL.md 已写入，下一次员工调用立即生效");
      setErr("");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  const saveMemory = async () => {
    if (!detail) return;
    try {
      await post(`/api/org/agent/${detail.id}/memory`, { memory: detail.memory, user: detail.user });
      await loadOrg();
      setMsg("MEMORY.md / USER.md 已写入，下一次会话即注入");
      setErr("");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  const loadSkill = async (name: string) => {
    if (!detail || !name) return;
    try {
      const data = await api<{ skill: { name: string; content: string } }>(
        `/api/org/agent/${detail.id}/skill?name=${encodeURIComponent(name)}`,
      );
      setSkillBody(data.skill.content);
      setErr("");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  const saveSkill = async () => {
    if (!detail) return;
    try {
      await post(`/api/org/agent/${detail.id}/skill`, { name: skillName, content: skillBody });
      const fresh = await api<{ agent: AgentDetail }>(`/api/org/agent/${detail.id}`);
      setDetail(fresh.agent);
      setMsg(`技能 ${skillName} 已写入 profile，下一次调用即加载`);
      setErr("");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  const saveConfig = async () => {
    if (!detail) return;
    try {
      await post(`/api/org/agent/${detail.id}/config`, { content: detail.config });
      setMsg("config.yaml 已保存（YAML 校验通过），下次 -z 调用重新读取");
      setErr("");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  const savePolicy = async () => {
    if (!policy) return;
    try {
      await post("/api/org/policy", { key: policyKey, content: policy.content });
      setMsg(`${policy.path} 已保存`);
      setErr("");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  return (
    <section className="page">
      <div className="page-head">
        <h1>组织配置</h1>
        <div className="tabbar">
          {TABS.map(([key, label]) => (
            <button
              key={key}
              type="button"
              className={`btn ${tab === key ? "" : "btn-ghost"}`}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {err && <p className="error banner">{err}</p>}
      {msg && <p className="banner">{msg}</p>}

      {tab === "staff" && (
        <div className="panel-grid">
          <div className="card">
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Agent ID</th>
                    <th>姓名</th>
                    <th>岗位</th>
                    <th>项目</th>
                    <th>负责范围</th>
                    <th>profile</th>
                  </tr>
                </thead>
                <tbody>
                  {agents.map((a) => (
                    <tr key={a.id} className={selected === a.id ? "row-active" : ""} onClick={() => void selectAgent(a.id)}>
                      <td className="nowrap">{a.id}</td>
                      <td>{a.name}</td>
                      <td>{a.role}</td>
                      <td className="nowrap">{a.project}</td>
                      <td>{a.scope}</td>
                      <td className="nowrap">{a.profile ? `SOUL ${a.hasSoul ? "✓" : "✗"} · 技能 ${a.skills.length}` : "人工"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {detail && (
            <aside className="card detail">
              <div className="detail-head">
                <h2>{detail.id}</h2>
                <button type="button" className="btn btn-ghost" onClick={() => setDetail(null)}>关闭</button>
              </div>
              <div className="form">
                <label className="field"><span>姓名</span>
                  <input className="input" value={detail.name} onChange={(e) => setDetail({ ...detail, name: e.target.value })} />
                </label>
                <label className="field"><span>岗位</span>
                  <input className="input" value={detail.role} onChange={(e) => setDetail({ ...detail, role: e.target.value })} />
                </label>
                <label className="field"><span>绑定项目</span>
                  <select className="input" value={detail.projectId} onChange={(e) => setDetail({ ...detail, projectId: e.target.value })}>
                    {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </label>
                <label className="field"><span>负责范围</span>
                  <input className="input" value={detail.scope} onChange={(e) => setDetail({ ...detail, scope: e.target.value })} />
                </label>
                <label className="field"><span>工具白名单（顿号或逗号分隔）</span>
                  <input className="input" value={detail.tools.join("、")}
                    onChange={(e) => setDetail({ ...detail, tools: e.target.value.split(/[、,，]/).map((x) => x.trim()).filter(Boolean) })} />
                </label>
                <p className="muted">调性：{detail.tone}｜保存后写回《员工名册》并热加载；SOUL 头部（姓名/岗位/项目）同步。</p>
                <button type="button" className="btn btn-primary" onClick={() => void saveStaff()}>保存人员与岗位</button>
              </div>
            </aside>
          )}
        </div>
      )}

      {tab === "policy" && (
        <div className="panel-grid">
          <div className="card">
            <div className="toolbar">
              <select className="input" value={policyKey} onChange={(e) => void loadPolicy(e.target.value)}>
                {policies.map((p) => <option key={p.key} value={p.key}>{p.title}{p.editable ? "" : "（只读）"}</option>)}
              </select>
              {policy && <span className="muted">{policy.path} · {policy.editable ? "可编辑" : "只读视图"}</span>}
            </div>
            {policy && (
              <>
                <textarea className="editor" value={policy.content} readOnly={!policy.editable}
                  onChange={(e) => setPolicy({ ...policy, content: e.target.value })} />
                {policy.editable && <button type="button" className="btn" onClick={() => void savePolicy()}>保存制度</button>}
              </>
            )}
          </div>
        </div>
      )}

      {tab === "soul" && (
        <div className="panel-grid single">
          <div className="card">
            <div className="toolbar">
              <select className="input" value={selected ?? ""} onChange={(e) => void selectAgent(e.target.value)}>
                <option value="">选择员工</option>
                {agents.filter((a) => a.profile).map((a) => <option key={a.id} value={a.id}>{a.name}（{a.id}）</option>)}
              </select>
              {detail && <span className="muted">SOUL.md · 性格、纪律、权限边界</span>}
            </div>
            {detail && (
              <>
                <textarea className="editor" value={detail.soul} onChange={(e) => setDetail({ ...detail, soul: e.target.value })} />
                <button type="button" className="btn" onClick={() => void saveSoul()}>保存 SOUL</button>
                <h3 className="section-title">技能（skills/&lt;name&gt;/SKILL.md）</h3>
                <div className="toolbar">
                  <input className="input" value={skillName} placeholder="技能名，如 final-reviewer"
                    onChange={(e) => setSkillName(e.target.value)} />
                  <button type="button" className="btn btn-ghost" onClick={() => void loadSkill(skillName)}>读取</button>
                  <button type="button" className="btn" onClick={() => void saveSkill()}>保存为新/覆盖</button>
                </div>
                <textarea className="editor small" value={skillBody} placeholder="SKILL.md 内容"
                  onChange={(e) => setSkillBody(e.target.value)} />
              </>
            )}
          </div>
        </div>
      )}

      {tab === "memory" && (
        <div className="panel-grid single">
          <div className="card">
            <div className="toolbar">
              <select className="input" value={selected ?? ""} onChange={(e) => void selectAgent(e.target.value)}>
                <option value="">选择员工</option>
                {agents.filter((a) => a.profile).map((a) => <option key={a.id} value={a.id}>{a.name}（{a.id}）</option>)}
              </select>
              {detail && <span className="muted">memories/MEMORY.md · USER.md</span>}
            </div>
            {detail && (
              <>
                <h3 className="section-title">MEMORY.md（环境事实、约定、经验）</h3>
                <textarea className="editor small" value={detail.memory} onChange={(e) => setDetail({ ...detail, memory: e.target.value })} />
                <h3 className="section-title">USER.md（用户画像与偏好）</h3>
                <textarea className="editor small" value={detail.user} onChange={(e) => setDetail({ ...detail, user: e.target.value })} />
                <button type="button" className="btn" onClick={() => void saveMemory()}>保存记忆</button>
              </>
            )}
          </div>
        </div>
      )}

      {tab === "advanced" && (
        <div className="panel-grid single">
          <div className="card">
            <div className="toolbar">
              <select className="input" value={selected ?? ""} onChange={(e) => void selectAgent(e.target.value)}>
                <option value="">选择员工</option>
                {agents.filter((a) => a.profile).map((a) => <option key={a.id} value={a.id}>{a.name}（{a.id}）</option>)}
              </select>
              <button type="button" className="btn btn-ghost" onClick={() => void post("/api/org/reload", {})}>重载配置</button>
            </div>
            {detail && (
              <>
                <h3 className="section-title">config.yaml（保存前做 YAML 校验）</h3>
                <textarea className="editor" value={detail.config} onChange={(e) => setDetail({ ...detail, config: e.target.value })} />
                <button type="button" className="btn" onClick={() => void saveConfig()}>保存 config.yaml</button>
              </>
            )}
            <h3 className="section-title">最近变更（审计）</h3>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>说明</th></tr></thead>
                <tbody>
                  {audits.map((a, i) => (
                    <tr key={i}>
                      <td className="nowrap">{a.ts}</td><td className="nowrap">{a.by}</td>
                      <td className="nowrap">{a.action}</td><td>{a.target}</td><td>{a.detail}</td>
                    </tr>
                  ))}
                  {audits.length === 0 && <tr><td colSpan={5} className="muted">暂无变更记录</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
