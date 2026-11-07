import { useEffect, useMemo, useRef, useState } from "react";
import type { Agent, Approval, OpcEvent, OpcState } from "../types";

/* 工位坐标与旧版组织地图保持一致（viewBox 900×520） */
const SEATS: Record<string, [number, number]> = {
  ceo: [250, 80],
  cco: [450, 80],
  cto: [650, 80],
  pm_wenren: [82, 196],
  cw_wenren: [82, 262],
  vd_wenren: [82, 328],
  pm_gou: [352, 196],
  cw_gou: [352, 262],
  vd_gou: [352, 328],
  pm_tongxinyuan: [628, 196],
  cw_tongxinyuan: [628, 262],
  vd_tongxinyuan: [628, 328],
  scout: [132, 452],
  reviewer: [332, 452],
  release_ops: [532, 452],
  kb_manager: [732, 452],
};
const MGMT: Record<string, number> = { ceo: 250, cco: 450, cto: 650 };
const PROJECT_COLOR: Record<string, string> = {
  wenren: "#2563EB",
  gou: "#EA580C",
  tongxinyuan: "#7C3AED",
};
const STATUS_TEXT: Record<string, string> = {
  working: "执行中",
  meeting: "会议中",
  done: "已交付",
  idle: "待命",
};
const TYPE_LABEL: Record<string, string> = {
  delegation: "委派",
  result: "交付",
  conflict: "冲突",
  vote: "投票",
  decision: "决议",
  approval: "审批",
  escalation: "升级",
  "biz.event": "业务事件",
  "scene.change": "场景",
  "report.ready": "汇报",
  heartbeat: "心跳",
  "config-reload": "配置热加载",
};
const QUICK_COMMANDS = [
  "今天哪个项目卡住了？",
  "@苏寒 汇报今日热点采集情况",
  "注入热点",
];

type FeedItem = { key: string; at: string; kind: string; text: string };

type CommandAnswer = {
  summary?: string;
  reply?: string;
  evidence?: string[];
  confidence?: number;
  tools?: string[];
};

type CommandResult = {
  ok?: boolean;
  error?: string;
  agent?: string;
  answer?: CommandAnswer;
  traceId?: string;
  engine?: string;
  hotspot?: { title?: string; score?: number };
};

async function fetchState(): Promise<OpcState | null> {
  const res = await fetch("/api/state", { credentials: "include" });
  if (res.status === 401) {
    window.location.assign("/app/login");
    return null;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as OpcState;
}

function nameOf(agents: Agent[], id?: string) {
  return agents.find((a) => a.id === id)?.name || id || "系统";
}

function describeMessage(agents: Agent[], ev: OpcEvent) {
  const who = `${nameOf(agents, ev.sender)} → ${nameOf(agents, ev.recipient)}`;
  const label = TYPE_LABEL[ev.type || ""] || ev.type || "消息";
  const text = ev.payload?.result?.summary || ev.payload?.text
    || (ev.payload?.verdict ? `审核${ev.payload.verdict.pass ? "通过" : "驳回"}` : "");
  return `${who} · ${label}${text ? `：${text}` : ""}`;
}

function describeEnvelope(agents: Agent[], env: any) {
  const d = env?.data || {};
  switch (env?.kind) {
    case "message":
      return describeMessage(agents, d as OpcEvent);
    case "hotspot":
      return `热点：${d.title || "-"}（热度 ${d.score ?? "-"}）`;
    case "scene":
      return `场景切换 → ${d.scene || "-"}${d.title ? ` · ${d.title}` : ""}`;
    case "pipeline":
      return `流水线：《${d.title || "-"}》 → ${d.stage || "-"}`;
    case "approval":
      return `审批门：${d.title || d.action || "-"} · ${d.status || "-"}`;
    case "report":
      return `${d.title || "汇报"} · ${d.conclusion || ""}`;
    case "agent":
      return `${nameOf(agents, d.id)} ${STATUS_TEXT[d.status] || d.status || ""} ${d.task || ""}`;
    default:
      return env?.kind || "事件";
  }
}

function toFeed(agents: Agent[], ev: OpcEvent, index: number): FeedItem {
  return {
    key: ev.id || `init-${index}`,
    at: ev.at || "",
    kind: "message",
    text: describeMessage(agents, ev),
  };
}

export function HomePage() {
  const [state, setState] = useState<OpcState | null>(null);
  const [error, setError] = useState("");
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [command, setCommand] = useState("");
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<CommandResult | null>(null);
  const agentsRef = useRef<Agent[]>([]);

  const agents = state?.agents ?? [];
  agentsRef.current = agents;

  const loadState = () => {
    fetchState()
      .then((data) => {
        if (!data) return;
        setState(data);
        setError("");
        const initial = (data.eventsList ?? []).map((ev, i) => toFeed(data.agents ?? [], ev, i));
        setFeed((current) => (current.length ? current : initial.reverse()));
      })
      .catch((err) => setError(String(err?.message || err)));
  };

  useEffect(() => {
    loadState();
    const timer = window.setInterval(loadState, 10000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const source = new EventSource("/api/events");
    source.onmessage = (event) => {
      let env: any;
      try {
        env = JSON.parse(event.data);
      } catch {
        return;
      }
      if (env?.kind === "state" && env.data) {
        setState(env.data as OpcState);
        return;
      }
      if (env?.kind === "heartbeat") return;
      const item: FeedItem = {
        key: `${env?.at || Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
        at: env?.at || "",
        kind: env?.kind || "event",
        text: describeEnvelope(agentsRef.current, env),
      };
      setFeed((current) => [item, ...current].slice(0, 80));
    };
    return () => source.close();
  }, []);

  const selected = useMemo(
    () => agents.find((agent) => agent.id === selectedId) ?? null,
    [agents, selectedId],
  );

  const projectName = (pid?: string) =>
    state?.projects?.find((p) => p.id === pid)?.name
    || ({ wenren: "文人莫克", gou: "狗大人", tongxinyuan: "同心圆", all: "全公司" } as Record<string, string>)[pid || ""]
    || "全公司";

  const projectColor = (pid?: string) =>
    state?.projects?.find((p) => p.id === pid)?.color || PROJECT_COLOR[pid || ""] || "#6B7280";

  const statusOf = (agent: Agent) => {
    if (state?.meeting?.active && (state.meeting.participants || []).includes(agent.id)) {
      return "meeting";
    }
    return agent.status || "idle";
  };

  const colorOf = (agent: Agent) => {
    const status = statusOf(agent);
    if (status === "meeting") return "#4F46E5";
    if (status === "working") return "#0EA5E9";
    if (status === "done") return "#059669";
    return projectColor(agent.projectId);
  };

  const pickAgent = (agent: Agent) => {
    setSelectedId(agent.id);
    setCommand(`@${agent.name} `);
  };

  const sendCommand = async (text: string) => {
    const value = text.trim();
    if (!value || sending) return;
    setSending(true);
    setResult(null);
    try {
      const res = await fetch("/api/commands", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: value }),
      });
      const json = (await res.json()) as CommandResult;
      setResult(json);
      if (res.status === 401) window.location.assign("/app/login");
      setCommand("");
      loadState();
    } catch (err: any) {
      setResult({ ok: false, error: String(err?.message || err) });
    } finally {
      setSending(false);
    }
  };

  const decide = async (approval: Approval, decision: "approve" | "reject") => {
    try {
      const res = await fetch(`/api/approvals/${approval.id}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        setResult({ ok: false, error: String(json?.reason || json?.error || `HTTP ${res.status}`) });
        return;
      }
      loadState();
    } catch (err: any) {
      setResult({ ok: false, error: String(err?.message || err) });
    }
  };

  const pending = (state?.approvals ?? []).filter((a) => a.status === "pending");
  const latestApproval = (state?.approvals ?? []).slice(-1)[0];
  const lastEvidence = selected
    ? feed.find((item) => item.text.includes(selected.name))
    : null;

  return (
    <section className="home">
      <div className="page-head">
        <div>
          <h1>首页</h1>
          <p className="muted home-sub">Conring 工作流 · 工位图 · 指令台 · 员工详情 · 老板审批门</p>
        </div>
        <span className="muted nowrap">
          {state?.serverTime ? `服务时间 ${state.serverTime.replace("T", " ")}` : "连接中…"}
        </span>
      </div>

      {error && <div className="banner error-banner">状态加载失败：{error}</div>}

      {/* 1. Conring 工作流 */}
      <section className="card home-block">
        <div className="block-head">
          <div>
            <h2>Conring 工作流</h2>
            <p className="muted">员工之间的委派、交付、审批与汇报实时流</p>
          </div>
          <span className="pill-count">{feed.length} 条</span>
        </div>
        <div className="event-feed">
          {feed.length === 0 ? (
            <p className="muted">等待事件…</p>
          ) : (
            feed.slice(0, 60).map((item) => (
              <div key={item.key} className={`event-item event-${item.kind}`}>
                <span className="event-time">{item.at ? item.at.replace("T", " ").slice(11, 19) : "--:--:--"}</span>
                <span className="event-text">{item.text}</span>
              </div>
            ))
          )}
        </div>
      </section>

      {/* 2. 工位图 */}
      <section className="card home-block">
        <div className="block-head">
          <div>
            <h2>工位图</h2>
            <p className="muted">点击工位即可选中员工，并把指令自动 @ 给他</p>
          </div>
          <span className="muted">{agents.length} 名员工</span>
        </div>
        <div className="map-wrap">
          <svg className="office-map" viewBox="0 0 900 520" role="img" aria-label="组织工位图">
            <rect className="map-zone" x="16" y="28" width="868" height="84" rx="12" />
            <text className="map-zone-label" x="26" y="44">管理层</text>
            <rect className="map-zone" x="20" y="138" width="290" height="224" rx="12" />
            <text className="map-zone-label" x="30" y="154" fill={projectColor("wenren")}>
              {projectName("wenren")} · 项目区
            </text>
            <rect className="map-zone" x="320" y="138" width="290" height="224" rx="12" />
            <text className="map-zone-label" x="330" y="154" fill={projectColor("gou")}>
              {projectName("gou")} · 项目区
            </text>
            <rect className="map-zone" x="620" y="138" width="264" height="224" rx="12" />
            <text className="map-zone-label" x="630" y="154" fill={projectColor("tongxinyuan")}>
              {projectName("tongxinyuan")} · 项目区
            </text>
            <rect className="map-zone" x="20" y="394" width="856" height="110" rx="12" />
            <text className="map-zone-label" x="30" y="410">中台支撑区</text>

            {agents.map((agent) => {
              if (agent.id === "boss") return null;
              const status = statusOf(agent);
              const color = colorOf(agent);
              const badge = STATUS_TEXT[status] || status;
              const seat = SEATS[agent.id] || [450, 450];
              const active = status === "working" || status === "meeting";
              const isSelected = selectedId === agent.id;
              const avatar = agent.avatar || `/avatars/${agent.id}.png`;
              if (MGMT[agent.id]) {
                const cx = MGMT[agent.id];
                return (
                  <g
                    key={agent.id}
                    className={`map-seat map-mgmt${active ? " active" : ""}${isSelected ? " selected" : ""}`}
                    onClick={() => pickAgent(agent)}
                  >
                    <rect className="map-mgmt-card" x={cx - 95} y="56" width="190" height="48" rx="12" />
                    <clipPath id={`clip-${agent.id}`}>
                      <circle cx={cx - 70} cy="80" r="16" />
                    </clipPath>
                    <circle cx={cx - 70} cy="80" r="16" fill={color} />
                    <image
                      href={avatar}
                      x={cx - 86}
                      y="64"
                      width="32"
                      height="32"
                      clipPath={`url(#clip-${agent.id})`}
                      preserveAspectRatio="xMidYMid slice"
                    />
                    <text className="map-name" x={cx - 46} y="76">{agent.name}</text>
                    <text className="map-role" x={cx - 46} y="91">{agent.role}</text>
                    <circle cx={cx + 72} cy="74" r="3.5" fill={color} />
                    <text className="map-badge" x={cx + 72} y="91" textAnchor="middle">{badge}</text>
                  </g>
                );
              }
              return (
                <g
                  key={agent.id}
                  className={`map-seat${active ? " active" : ""}${isSelected ? " selected" : ""}`}
                  onClick={() => pickAgent(agent)}
                >
                  <clipPath id={`clip-${agent.id}`}>
                    <circle cx={seat[0]} cy={seat[1]} r="17.5" />
                  </clipPath>
                  <circle className="map-core" cx={seat[0]} cy={seat[1]} r="17" fill={color} />
                  <image
                    href={avatar}
                    x={seat[0] - 17}
                    y={seat[1] - 17}
                    width="34"
                    height="34"
                    clipPath={`url(#clip-${agent.id})`}
                    preserveAspectRatio="xMidYMid slice"
                  />
                  <text className="map-name" x={seat[0] + 25} y={seat[1] - 2}>{agent.name}</text>
                  <text className="map-role" x={seat[0] + 25} y={seat[1] + 11}>
                    {agent.role} · {badge}
                  </text>
                </g>
              );
            })}
          </svg>
        </div>
      </section>

      {/* 3. 命令窗口 */}
      <section className="card home-block cmd-card">
        <div className="cmd-head">
          <div className="cmd-icon">›_</div>
          <div>
            <h2>指令台</h2>
            <p className="muted">
              支持 <code>@员工 指令</code>、<code>注入热点</code>，其余问题由 CEO 汇总当前进度
            </p>
          </div>
          {selected && (
            <span className="cmd-target">
              已选中 <b>{selected.name}</b> · {selected.role}
            </span>
          )}
        </div>
        <div className="cmd-row">
          <span className="cmd-prefix">$</span>
          <input
            className="cmd-input"
            value={command}
            onChange={(event) => setCommand(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendCommand(command);
              }
            }}
            placeholder="例如：@陆清安 今天哪个项目卡住了 / 注入热点"
          />
          <button
            type="button"
            className="btn cmd-send"
            disabled={sending || !command.trim()}
            onClick={() => sendCommand(command)}
          >
            {sending ? "执行中…" : "发送"}
          </button>
        </div>
        <div className="cmd-quick">
          {QUICK_COMMANDS.map((text) => (
            <button
              key={text}
              type="button"
              className="chip-btn"
              onClick={() => {
                setCommand(text);
                sendCommand(text);
              }}
            >
              {text}
            </button>
          ))}
        </div>
        {result && (
          <div className={`cmd-result${result.ok === false ? " is-error" : ""}`}>
            {result.ok === false || result.error ? (
              <p className="error">{result.error || "指令执行失败"}</p>
            ) : (
              <>
                <div className="cmd-result-head">
                  <span>{result.agent ? `${result.agent} 回复` : result.hotspot ? "热点注入" : "CEO 回复"}</span>
                  {typeof result.answer?.confidence === "number" && (
                    <span className="muted">置信度 {Math.round(result.answer.confidence * 100)}%</span>
                  )}
                  {result.engine && <span className="muted">{result.engine}</span>}
                </div>
                <p className="cmd-result-text">
                  {result.answer?.summary || result.answer?.reply
                    || (result.hotspot ? `已注入《${result.hotspot.title || "-"}》，trace ${result.traceId || "-"}` : "已完成")}
                </p>
                {!!result.answer?.evidence?.length && (
                  <div className="tags">
                    {result.answer.evidence.map((item) => (
                      <span key={item} className="tag">{item}</span>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </section>

      <div className="home-bottom">
        {/* 4. 员工详情 */}
        <section className="card home-block">
          <div className="block-head">
            <div>
              <h2>员工详情</h2>
              <p className="muted">任务、工具与最近一次协作证据</p>
            </div>
            {selected && (
              <button type="button" className="btn btn-ghost" onClick={() => setSelectedId(null)}>
                清除选择
              </button>
            )}
          </div>
          {!selected ? (
            <p className="muted">点击上方工位查看员工详情。</p>
          ) : (
            <div className="agent-detail">
              <div className="agent-head">
                <span className="agent-avatar" style={{ background: colorOf(selected) }}>
                  <img
                    src={selected.avatar || `/avatars/${selected.id}.png`}
                    alt={selected.name}
                    onError={(event) => {
                      (event.currentTarget as HTMLImageElement).style.display = "none";
                    }}
                  />
                </span>
                <div>
                  <div className="agent-name">{selected.name} · {selected.role}</div>
                  <div className="muted">
                    {projectName(selected.projectId)} · {selected.tone || "跨项目中台"}
                  </div>
                </div>
              </div>
              <dl className="kv">
                <dt>状态</dt>
                <dd>{STATUS_TEXT[statusOf(selected)] || statusOf(selected)}</dd>
                <dt>当前任务</dt>
                <dd>{selected.task || "暂无在执行的任务"}</dd>
                <dt>权限范围</dt>
                <dd>{selected.scope || "-"}</dd>
                <dt>最近证据</dt>
                <dd>{lastEvidence?.text || "暂无（等待下一轮事件）"}</dd>
              </dl>
              <div className="tags">
                {(selected.tools?.length ? selected.tools : ["暂无工具"]).map((tool) => (
                  <span key={tool} className="tag">{tool}</span>
                ))}
                {(selected.highRisk || []).map((item) => (
                  <span key={item} className="tag tag-danger">高风险：{item}</span>
                ))}
              </div>
            </div>
          )}
        </section>

        {/* 5. 审批 */}
        <section className="card home-block">
          <div className="block-head">
            <div>
              <h2>老板审批门</h2>
              <p className="muted">开工 / 发布 / 对外动作必须人工拍板</p>
            </div>
            <span className="pill-count">{pending.length} 待审批</span>
          </div>
          {pending.length === 0 ? (
            <p className="muted">
              {latestApproval
                ? `最近审批：${latestApproval.title || latestApproval.action} · ${latestApproval.status === "approved" ? "已通过" : latestApproval.status}`
                : "暂无待审批。AI 不得自行开工、发布或对外承诺。"}
            </p>
          ) : (
            <div className="approval-list">
              {pending.slice(-3).reverse().map((approval) => {
                const checks = approval.checks || {};
                const cost = checks.cost;
                return (
                  <div key={approval.id} className="approval-card">
                    <h3>{approval.title || approval.action || approval.id}</h3>
                    <div className="approval-line">
                      风险：{approval.risk || "-"} · 动作：{approval.action || "-"} · 内容 {(approval.items || []).length} 条
                    </div>
                    {cost && (
                      <div className="approval-line">
                        模型调用约 {cost.modelCallsTypical ?? "-"} 次 · 渲染 {cost.renders ?? "-"} 条
                      </div>
                    )}
                    <div className="approval-line">trace：{approval.traceId || "-"}</div>
                    <div className="approval-actions">
                      <button type="button" className="btn btn-ok" onClick={() => decide(approval, "approve")}>
                        {approval.action === "publish.execute" ? "通过并发布" : "通过并开工"}
                      </button>
                      <button type="button" className="btn btn-ghost" onClick={() => decide(approval, "reject")}>
                        驳回
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>
      </div>
    </section>
  );
}
