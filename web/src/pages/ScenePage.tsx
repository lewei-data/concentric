import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

type SceneKey = "office" | "meeting";

interface SceneConfig {
  title: string;
  desc: string;
  src: string;
  other: string;
  otherLabel: string;
  endpoint: string;
}

const SCENES: Record<SceneKey, SceneConfig> = {
  office: {
    title: "3D 办公区",
    desc: "员工工位、实时状态与会议指示牌；点击人物查看任务，拖拽旋转视角。",
    src: "/office3d?embed=1",
    other: "/meeting",
    otherLabel: "前往 3D 会议室",
    endpoint: "/api/state",
  },
  meeting: {
    title: "3D 会议室",
    desc: "选题会实时投影、逐条发言与会议纪要索引，数据来自 /api/meeting 与 SSE。",
    src: "/meeting3d?embed=1",
    other: "/office",
    otherLabel: "返回 3D 办公区",
    endpoint: "/api/meeting",
  },
};

type AgentBrief = {
  id: string;
  name?: string;
  role?: string;
  status?: string;
  task?: string;
  projectId?: string;
};

type Speech = {
  id?: string;
  speaker?: string;
  role?: string;
  text?: string;
  at?: string;
};

type MeetingRecord = {
  id?: string;
  title?: string;
  status?: string;
  startedAt?: string;
  endedAt?: string;
  speechCount?: number;
  conclusion?: string;
  participants?: unknown[];
  speeches?: Speech[];
};

type MeetingView = {
  active: boolean;
  current: MeetingRecord | null;
  last: MeetingRecord | null;
  focus: MeetingRecord | null;
  history: MeetingRecord[];
  participants: number;
  speechCount: number;
  lastSpeech: Speech | null;
  conclusion: string;
};

type StateView = {
  agents: AgentBrief[];
  scene: string;
  serverTime: string;
  counts: { total: number; working: number; meeting: number; idle: number; done: number };
};

const STATUS_LABEL: Record<string, string> = {
  working: "执行中",
  meeting: "会议中",
  done: "已交付",
  idle: "待命",
};

const SCENE_LABEL: Record<string, string> = { WORK: "日常办公", MEETING: "选题会中" };

function fmtTime(value?: string): string {
  if (!value) return "-";
  const text = value.replace("T", " ");
  return text.length >= 16 ? text.slice(5, 16) : text;
}

async function apiGet<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include", cache: "no-store" });
  if (res.status === 401) {
    window.location.assign("/app/login");
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

function toStateView(raw: Record<string, any>): StateView {
  const agents: AgentBrief[] = Array.isArray(raw.agents) ? raw.agents : [];
  const counts = { total: agents.length, working: 0, meeting: 0, idle: 0, done: 0 };
  for (const a of agents) {
    if (a.status === "working") counts.working += 1;
    else if (a.status === "meeting") counts.meeting += 1;
    else if (a.status === "done") counts.done += 1;
    else counts.idle += 1;
  }
  return {
    agents,
    scene: String(raw.scene || "WORK"),
    serverTime: String(raw.serverTime || ""),
    counts,
  };
}

function toMeetingView(raw: Record<string, any>): MeetingView {
  const current: MeetingRecord | null = raw.current || null;
  const last: MeetingRecord | null = raw.last || null;
  const history: MeetingRecord[] = Array.isArray(raw.history) ? raw.history : [];
  const currentSpeeches = current?.speeches || [];
  // /api/meeting 在空闲时仍返回 status=idle 的空 current，真实数据在 last 里
  const hasLive = !!raw.active && !!current && current.status !== "idle" &&
    (currentSpeeches.length > 0 || (current.participants || []).length > 0);
  const focus = hasLive ? current : (last || current);
  const focusSpeeches = focus?.speeches || [];
  return {
    active: !!raw.active,
    current,
    last,
    focus,
    history,
    participants: (focus?.participants || []).length,
    speechCount: focus?.speechCount ?? focusSpeeches.length ?? 0,
    lastSpeech: hasLive && currentSpeeches.length ? currentSpeeches[currentSpeeches.length - 1] : null,
    conclusion: String(focus?.conclusion || ""),
  };
}

export function ScenePage({ scene }: { scene: SceneKey }) {
  const cfg = SCENES[scene];
  const navigate = useNavigate();
  const [nonce, setNonce] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [timedOut, setTimedOut] = useState(false);
  const [sessionReady, setSessionReady] = useState(false);
  const [stateView, setStateView] = useState<StateView | null>(null);
  const [meetingView, setMeetingView] = useState<MeetingView | null>(null);
  const [dataError, setDataError] = useState("");
  const timer = useRef<number | undefined>(undefined);
  const refreshTimer = useRef<number | undefined>(undefined);

  const loadData = useCallback(async () => {
    try {
      if (scene === "office") setStateView(toStateView(await apiGet<Record<string, any>>("/api/state")));
      else setMeetingView(toMeetingView(await apiGet<Record<string, any>>("/api/meeting")));
      setDataError("");
    } catch (err) {
      if ((err as Error).message !== "unauthorized") setDataError(`数据刷新失败：${(err as Error).message}`);
    }
  }, [scene]);

  // 顶层先确认会话，避免 iframe 内被 302 到旧登录页却显示「加载成功」
  useEffect(() => {
    let cancelled = false;
    fetch("/api/session", { credentials: "include" })
      .then((res) => {
        if (res.status === 401) {
          window.location.assign("/app/login");
          return;
        }
        if (!cancelled) setSessionReady(true);
      })
      .catch(() => { if (!cancelled) setSessionReady(true); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!sessionReady) return;
    loadData();
    const interval = window.setInterval(loadData, scene === "meeting" ? 10000 : 15000);
    return () => window.clearInterval(interval);
  }, [sessionReady, scene, loadData]);

  // 与 iframe 的握手：就绪 / 场景内指示牌切换 / 会话过期
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data as Record<string, any> | null;
      if (!data || data.source !== "opc-3d") return;
      if (data.type === "opc:scene-ready") {
        setLoaded(true);
        setTimedOut(false);
        window.clearTimeout(timer.current);
      } else if (data.type === "opc:navigate" && typeof data.path === "string" && data.path.startsWith("/")) {
        navigate(data.path);
      } else if (data.type === "opc:unauthorized") {
        window.location.assign("/app/login");
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [navigate]);

  // 外层侧栏消费同一份 SSE，iframe 里 3D 画面与外层数据保持一致
  useEffect(() => {
    if (!sessionReady) return;
    let alive = true;
    const es = new EventSource("/api/events");
    es.onmessage = (event) => {
      let env: any;
      try { env = JSON.parse(event.data); } catch { return; }
      const watched = scene === "office"
        ? ["agent", "scene", "config-reload", "pipeline", "state"]
        : ["meeting.start", "meeting.speech", "meeting.end", "scene", "state"];
      if (!watched.includes(env?.kind)) return;
      window.clearTimeout(refreshTimer.current);
      refreshTimer.current = window.setTimeout(() => { if (alive) loadData(); }, 350);
    };
    return () => {
      alive = false;
      window.clearTimeout(refreshTimer.current);
      es.close();
    };
  }, [sessionReady, scene, loadData]);

  useEffect(() => {
    setLoaded(false);
    setTimedOut(false);
    timer.current = window.setTimeout(() => setTimedOut(true), 20000);
    return () => window.clearTimeout(timer.current);
  }, [scene, nonce]);

  const reload = () => setNonce((n) => n + 1);

  const handleFrameLoad = (event: React.SyntheticEvent<HTMLIFrameElement>) => {
    try {
      const doc = event.currentTarget.contentDocument;
      if (doc && doc.title.includes("登录")) window.location.assign("/app/login");
    } catch { /* 跨源时忽略，仍以 postMessage 握手为准 */ }
  };

  const agentRows = useMemo(
    () => (stateView?.agents || []).filter((a) => a.id !== "boss").slice(0, 8),
    [stateView],
  );

  return (
    <section className="scene-page">
      <div className="scene-bar">
        <div>
          <h2>{cfg.title}</h2>
          <p>{cfg.desc}</p>
        </div>
        <div className="scene-actions">
          <Link className="btn" to={cfg.other}>{cfg.otherLabel}</Link>
          <button type="button" className="btn btn-ghost" onClick={reload}>
            重新加载
          </button>
          <a
            className="btn btn-ghost"
            href={cfg.src.replace("?embed=1", "")}
            target="_blank"
            rel="noreferrer"
          >
            独立窗口打开
          </a>
        </div>
      </div>

      {dataError && <div className="banner error-banner">{dataError}</div>}

      <div className="scene-layout">
        <div className="scene-frame">
          {sessionReady && (
            <iframe
              key={`${scene}-${nonce}`}
              src={`${cfg.src}&t=${nonce}`}
              title={cfg.title}
              onLoad={handleFrameLoad}
              allow="fullscreen"
            />
          )}
          {!sessionReady && <div className="scene-loading">正在校验登录状态…</div>}
          {sessionReady && !loaded && !timedOut && (
            <div className="scene-loading">正在加载 3D 场景…</div>
          )}
          {timedOut && !loaded && (
            <div className="scene-error">
              <div>
                <p>场景加载超时，请确认已登录且服务正常。</p>
                <button type="button" className="btn" onClick={reload}>重试</button>
              </div>
            </div>
          )}
        </div>

        <aside className="card scene-panel">
          <div className="panel-title">
            <h3>{scene === "office" ? "实时工位" : "会议实况"}</h3>
            <span className="scene-source">{cfg.endpoint} · SSE</span>
          </div>

          {scene === "office" ? (
            <>
              <div className="scene-stat-grid">
                <div className="scene-stat"><b>{stateView?.counts.total ?? "-"}</b><span>员工</span></div>
                <div className="scene-stat accent"><b>{stateView?.counts.working ?? "-"}</b><span>执行中</span></div>
                <div className="scene-stat violet"><b>{stateView?.counts.meeting ?? "-"}</b><span>会议中</span></div>
                <div className="scene-stat"><b>{stateView?.counts.idle ?? "-"}</b><span>待命</span></div>
              </div>
              <dl className="scene-kv">
                <dt>当前场景</dt>
                <dd>{SCENE_LABEL[stateView?.scene || ""] || stateView?.scene || "-"}</dd>
                <dt>快照时间</dt>
                <dd>{fmtTime(stateView?.serverTime)}</dd>
              </dl>
              <h4 className="panel-sub">员工状态</h4>
              <ul className="agent-list">
                {agentRows.length === 0 && <li className="muted">正在读取工位数据…</li>}
                {agentRows.map((agent) => (
                  <li key={agent.id}>
                    <span className={`status-dot status-${agent.status || "idle"}`} />
                    <span className="agent-name" title={agent.name}>{agent.name || agent.id}</span>
                    <span className="agent-status">{STATUS_LABEL[agent.status || ""] || agent.status}</span>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <>
              <div className={`scene-status ${meetingView?.active ? "live" : "idle"}`}>
                {meetingView?.active ? "进行中" : "当前无会议"}
              </div>
              <h3 className="meeting-title">
                {meetingView?.focus?.title || "暂无会议"}
              </h3>
              <div className="scene-stat-grid three">
                <div className="scene-stat"><b>{meetingView?.participants ?? "-"}</b><span>参会人</span></div>
                <div className="scene-stat accent"><b>{meetingView?.speechCount ?? "-"}</b><span>发言</span></div>
                <div className="scene-stat violet"><b>{meetingView?.history.length ?? "-"}</b><span>历史会议</span></div>
              </div>
              <div className="speech-box">
                <span>{meetingView?.lastSpeech ? "最新发言" : "最近会议结论"}</span>
                <p>
                  {meetingView?.lastSpeech
                    ? `${meetingView.lastSpeech.speaker || "员工"}：${meetingView.lastSpeech.text || ""}`
                    : meetingView?.conclusion || "暂无会议结论"}
                </p>
              </div>
              <h4 className="panel-sub">最近会议</h4>
              <ul className="meeting-list">
                {(meetingView?.history || []).slice(0, 4).map((item, index) => (
                  <li key={item.id || index}>
                    <span className="meeting-name" title={item.title}>{item.title || "未命名会议"}</span>
                    <span className="muted">{fmtTime(item.startedAt)} · {item.speechCount ?? 0} 条发言</span>
                  </li>
                ))}
                {!meetingView?.history.length && <li className="muted">正在读取会议数据…</li>}
              </ul>
            </>
          )}
        </aside>
      </div>
    </section>
  );
}
