import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { appRoot } from "../appRoot";
import type { Agent, Approval, OpcState, Work } from "../types";

// 公网挂在 /workbench/app（无尾斜杠）下：用 appRoot() 统一推导，避免算成 "/"

const STATUS_TEXT: Record<string, string> = {
  working: "执行中",
  meeting: "会议中",
  done: "已交付",
  idle: "待命",
};

const PROJECT_FALLBACK: Record<string, { name: string; color: string }> = {
  wenren: { name: "文人莫克", color: "#2563EB" },
  gou: { name: "狗大人", color: "#EA580C" },
  tongxinyuan: { name: "同心圆", color: "#7C3AED" },
};
const PROJECT_ORDER = ["wenren", "gou", "tongxinyuan"];
// 管理层：CEO / CCO / CTO + 小秘书
const MGMT_IDS = ["ceo", "cco", "cto", "secretary"];
// 老板不占工位
const FLOOR_HIDDEN = new Set(["boss"]);

type WorkFacets = {
  total: number;
  byStatus: Record<string, number>;
  byProject: Record<string, number>;
  withFinal: number;
  withReview: number;
};
type WorksResp = { ok: boolean; total: number; items: Work[]; facets?: WorkFacets };
type InspirationResp = {
  ok: boolean;
  counts: Record<string, number>;
  sources?: Record<string, number>;
};
type OpsService = { id: string; name: string; online: boolean; status?: string };
type OpsResp = {
  ok: boolean;
  generatedAt?: string;
  service?: { ok: boolean; uptimeSec: number; engine?: string; hermes?: Record<string, number | string> };
  pipeline?: {
    stageCounts?: Record<string, number>;
    blocked?: { id: string; title: string; stage: string; blockedBy: string }[];
  };
  video?: {
    services?: OpsService[];
    localJobs?: { id: string; status: string; kind?: string }[];
    pipelineRender?: { id: string; title: string; stage: string; state: string }[];
    pendingRender?: { id: string; title: string; stage: string; blockedBy: string }[];
  };
  alerts?: { id: string; level: string; title: string; detail: string; source?: string }[];
};

async function apiGet<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include" });
  if (res.status === 401) {
    window.location.assign(`${appRoot()}app/login`);
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

function fmtUptime(sec?: number): string {
  if (!sec || sec < 0) return "-";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d} 天 ${h} 小时`;
  if (h) return `${h} 小时 ${m} 分`;
  return `${m} 分钟`;
}

function shortTime(value?: string): string {
  return value ? value.replace("T", " ").slice(5, 16) : "-";
}

function daysSince(value?: string): number {
  const t = Date.parse(value || "");
  if (!t) return 0;
  return Math.floor((Date.now() - t) / 86400000);
}

// 头像等静态资源在「服务根」，公网需要带 /workbench 前缀。
// 注意：公网 nginx 的 sub_filter 会把接口 JSON 里的 "/avatars/..." 也改写成
// "/workbench/avatars/..."，这里必须先判重，否则会变成 /workbench/workbench/...
function assetUrl(path?: string): string {
  if (!path) return "";
  if (!path.startsWith("/")) return path;
  const prefix = appRoot().replace(/\/$/, "");
  if (!prefix) return path;
  if (path === prefix || path.startsWith(`${prefix}/`)) return path;
  return `${prefix}${path}`;
}

// 岗位职责：短句，控制在 7-8 个汉字，避免把工位撑开
function dutyFor(role: string, scope?: string): string {
  const r = role || "";
  if (/秘书/.test(r)) return "日程与会议协调";
  if (/首席创意|创意官|CCO/i.test(r)) return "创意与风格把控";
  if (/CEO|首席执行官/i.test(r)) return "统筹经营与决策";
  if (/CTO|技术总监/i.test(r)) return "技术与系统稳定";
  if (/产品经理/.test(r)) return "选题与产线推进";
  if (/文案/.test(r)) return "文案撰写与把关";
  if (/视频/.test(r)) return "分镜剪辑与成片";
  if (/发布|运营/.test(r)) return "发布排期与监控";
  if (/审核/.test(r)) return "内容审核与合规";
  if (/热点|采集/.test(r)) return "热点追踪与采集";
  if (/知识库/.test(r)) return "知识沉淀与检索";
  if (scope) return scope.length > 8 ? `${scope.slice(0, 8)}…` : scope;
  return "协同与支持";
}

// 按岗位给工位做差异化装饰
type DeskRole = { key: string; label: string };

function deskRole(role: string, isMgmt: boolean): DeskRole {
  const r = role || "";
  if (isMgmt) return { key: "lead", label: "管理" };
  if (/视频|剪辑|分镜|渲染|摄影|摄像|拍摄/.test(r)) return { key: "video", label: "视频" };
  if (/设计|美术|视觉|ui|ux/i.test(r)) return { key: "design", label: "设计" };
  if (/技术|工程|开发|运维|数据|算法|后端|前端/.test(r)) return { key: "tech", label: "技术" };
  if (/运营|发布|增长|市场|渠道|投放/.test(r)) return { key: "ops", label: "运营" };
  if (/创意|文案|选题|策划|灵感|编辑|编剧/.test(r)) return { key: "creative", label: "创意" };
  return { key: "support", label: "支撑" };
}

// 工作状态图标：执行中 / 会议中 / 已交付 / 待命
function StatusIcon({ status }: { status: string }) {
  const base = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.9,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };
  if (status === "working") {
    return <svg {...base}><path d="M3 12h4l2.5-7 4 14L16 12h5" /></svg>;
  }
  if (status === "meeting") {
    return (
      <svg {...base}>
        <path d="M20 12a7 7 0 0 1-7 7H8l-4 3v-4.6A7 7 0 1 1 20 12Z" />
        <path d="M9 11h.01M12.5 11h.01M16 11h.01" />
      </svg>
    );
  }
  if (status === "done") {
    return <svg {...base}><circle cx="12" cy="12" r="8.5" /><path d="m8.5 12 2.5 2.5 4.5-5" /></svg>;
  }
  return (
    <svg {...base}>
      <path d="M5 9h11v6a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4V9Z" />
      <path d="M16 10h1.5a2.5 2.5 0 0 1 0 5H16" />
      <path d="M8 5.5c0 1-1 1.5-1 2.8M11.5 5.5c0 1-1 1.5-1 2.8" />
    </svg>
  );
}

// 短视频架构原则（依据 ep2/ep3 制作记录里的口播时间轴与固定镜头结构）
const VIDEO_PRINCIPLES = [
  { t: "口播先行", d: "先定语音时间轴，画面跟着语音走，避免字幕与口播错位。" },
  { t: "3 秒钩子", d: "开头必须先抓住注意力，再进入正题。" },
  { t: "固定镜头结构", d: "按秒切分镜头（如 0–4 / 4–14 …），先定结构再填内容。" },
  { t: "单点强节奏", d: "一条视频只讲清一个点，节奏紧凑、信息不堆叠。" },
];

export function HomePage() {
  const [state, setState] = useState<OpcState | null>(null);
  const [works, setWorks] = useState<WorksResp | null>(null);
  const [ins, setIns] = useState<InspirationResp | null>(null);
  const [ops, setOps] = useState<OpsResp | null>(null);
  const [error, setError] = useState("");
  const [approvalMsg, setApprovalMsg] = useState("");
  const [soundOn, setSoundOn] = useState(true);
  const audioRef = useRef<AudioContext | null>(null);
  const lastHoverRef = useRef(0);

  // 浏览器要求先有用户手势才能播音频：首次点击/触摸时解锁
  useEffect(() => {
    const unlock = () => {
      try {
        if (!audioRef.current) {
          const Ctor = window.AudioContext
            || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
          if (Ctor) audioRef.current = new Ctor();
        }
        void audioRef.current?.resume();
      } catch {
        /* 不支持音频时静默降级 */
      }
      window.removeEventListener("pointerdown", unlock);
    };
    window.addEventListener("pointerdown", unlock);
    return () => window.removeEventListener("pointerdown", unlock);
  }, []);

  // 悬停工位时的轻提示音（很短、音量很低，可一键关闭）
  const playHover = useCallback(() => {
    if (!soundOn) return;
    const now = performance.now();
    if (now - lastHoverRef.current < 140) return;
    lastHoverRef.current = now;
    try {
      if (!audioRef.current) {
        const Ctor = window.AudioContext
          || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
        if (!Ctor) return;
        audioRef.current = new Ctor();
      }
      const ctx = audioRef.current;
      if (ctx.state === "suspended") void ctx.resume();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      const t = ctx.currentTime;
      osc.type = "sine";
      osc.frequency.setValueAtTime(660, t);
      osc.frequency.exponentialRampToValueAtTime(880, t + 0.12);
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.035, t + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.17);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(t);
      osc.stop(t + 0.19);
    } catch {
      /* 忽略音频异常 */
    }
  }, [soundOn]);

  const loadState = useCallback(() => {
    apiGet<OpcState>("/api/state").then(setState).catch((e) => setError(String(e?.message || e)));
  }, []);
  const loadWorks = useCallback(() => {
    apiGet<WorksResp>("/api/works?size=100").then(setWorks).catch(() => undefined);
  }, []);
  const loadIns = useCallback(() => {
    apiGet<InspirationResp>("/api/inspiration").then(setIns).catch(() => undefined);
  }, []);
  const loadOps = useCallback(() => {
    apiGet<OpsResp>("/api/ops").then(setOps).catch(() => undefined);
  }, []);

  useEffect(() => {
    loadState();
    loadWorks();
    loadIns();
    loadOps();
    const t1 = window.setInterval(loadState, 10000);
    const t2 = window.setInterval(loadWorks, 30000);
    const t3 = window.setInterval(loadIns, 60000);
    const t4 = window.setInterval(loadOps, 15000);
    return () => {
      window.clearInterval(t1);
      window.clearInterval(t2);
      window.clearInterval(t3);
      window.clearInterval(t4);
    };
  }, [loadState, loadWorks, loadIns, loadOps]);

  const agents = state?.agents ?? [];
  // 老板不占工位，工位图与在岗统计都排除
  const floorAgents = useMemo(() => agents.filter((a) => !FLOOR_HIDDEN.has(a.id)), [agents]);
  const items = works?.items ?? [];
  const facets = works?.facets;
  const byStatus = facets?.byStatus ?? {};

  const projName = (id?: string) =>
    state?.projects?.find((p) => p.id === id)?.name || PROJECT_FALLBACK[id || ""]?.name || "中台";
  const projColor = (id?: string) =>
    state?.projects?.find((p) => p.id === id)?.color || PROJECT_FALLBACK[id || ""]?.color || "#6B7280";

  const today = new Date().toISOString().slice(0, 10);

  const metrics = useMemo(() => {
    const totalWorks = facets?.total ?? items.length;
    const doing = byStatus["制作中"] ?? 0;
    const review = byStatus["待评审"] ?? 0;
    const approved = byStatus["待发布"] ?? 0;
    const published = (byStatus["已发布"] ?? 0) + (byStatus["已归档"] ?? 0);
    const doneToday = items.filter(
      (w) => (w.statusKey === "published" || w.statusKey === "archived")
        && (w.updatedAt || "").slice(0, 10) === today,
    ).length;
    const failing = items.filter(
      (w) => w.reviewPass === false
        || w.statusKey === "review"
        || (w.statusKey === "doing" && w.completeness < 50 && daysSince(w.updatedAt) >= 7),
    ).length;
    return { totalWorks, doing, review, approved, published, doneToday, failing };
  }, [facets, byStatus, items, today]);

  // 热门/上线表现：已发布/已归档优先，按评审通过 → 完整度 → 更新时间
  const hotWorks = useMemo(() => {
    const pool = items.filter((w) => w.statusKey === "published" || w.statusKey === "archived");
    const base = pool.length ? pool : items;
    return [...base]
      .sort((a, b) =>
        (Number(b.reviewPass === true) - Number(a.reviewPass === true))
        || (b.completeness - a.completeness)
        || String(b.updatedAt).localeCompare(String(a.updatedAt)))
      .slice(0, 5);
  }, [items]);

  // 滞销/受阻：未过审、待评审积压、长期停滞
  const blockedWorks = useMemo(() => {
    const rows = items
      .map((w) => {
        let reason = "";
        if (w.reviewPass === false) reason = "评审未通过";
        else if (w.statusKey === "review") reason = "待评审积压";
        else if (w.statusKey === "doing" && w.completeness < 50 && daysSince(w.updatedAt) >= 7) {
          reason = `停滞 ${daysSince(w.updatedAt)} 天`;
        }
        return reason ? { w, reason } : null;
      })
      .filter((x): x is { w: Work; reason: string } => x !== null);
    return rows.slice(0, 5);
  }, [items]);

  const funnel = useMemo(() => {
    const stageCount = (s: string) => items.filter((w) => w.stage === s).length;
    return [
      { key: "idea", label: "创意池", value: ins?.counts?.total ?? 0, hint: "热点 / 视频 / 蒸馏" },
      { key: "topic", label: "选题", value: stageCount("topic") },
      { key: "copy", label: "文案定稿", value: stageCount("copy") },
      { key: "video", label: "分镜 / 制作", value: stageCount("video") },
      { key: "review", label: "待评审", value: stageCount("review") },
      { key: "approved", label: "待发布", value: stageCount("approved") },
      { key: "published", label: "已发布", value: (stageCount("published") + stageCount("archived")) || metrics.published },
    ];
  }, [items, ins, metrics.published]);

  const people = useMemo(() => {
    // 管理层固定顺序：CEO / CCO / CTO，小秘书单独放最右侧
    const mgmtOrder = ["ceo", "cco", "cto"];
    const mgmt = floorAgents
      .filter((a) => MGMT_IDS.includes(a.id))
      .sort((a, b) => {
        const ia = mgmtOrder.indexOf(a.id);
        const ib = mgmtOrder.indexOf(b.id);
        return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
      });
    const mid: Agent[] = [];
    const byProj = new Map<string, Agent[]>();
    for (const a of floorAgents) {
      if (MGMT_IDS.includes(a.id)) continue;
      if (a.projectId && PROJECT_ORDER.includes(a.projectId)) {
        const arr = byProj.get(a.projectId) ?? [];
        arr.push(a);
        byProj.set(a.projectId, arr);
      } else {
        mid.push(a);
      }
    }
    const groups: { key: string; name: string; color: string; members: Agent[] }[] = [
      { key: "mgmt", name: "管理层", color: "#4F46E5", members: mgmt },
      ...PROJECT_ORDER.filter((p) => (byProj.get(p) ?? []).length)
        .map((p) => ({ key: p, name: projName(p), color: projColor(p), members: byProj.get(p) ?? [] })),
    ];
    if (mid.length) groups.push({ key: "mid", name: "中台支撑", color: "#6B7280", members: mid });
    return groups;
  }, [floorAgents, state]);

  const statusSummary = useMemo(() => {
    const c: Record<string, number> = { working: 0, meeting: 0, idle: 0, done: 0 };
    for (const a of floorAgents) c[a.status || "idle"] = (c[a.status || "idle"] ?? 0) + 1;
    return c;
  }, [floorAgents]);

  const pending = (state?.approvals ?? []).filter((a) => a.status === "pending");
  const opsService = ops?.service;
  const opsVideo = ops?.video;
  const onlineServices = (opsVideo?.services ?? []).filter((s) => s.online).length;
  const totalServices = (opsVideo?.services ?? []).length;
  const runningJobs = (opsVideo?.localJobs ?? []).filter(
    (j) => /run|process|queue|pending/i.test(j.status || ""),
  ).length;
  const pendingRender = opsVideo?.pendingRender?.length ?? 0;
  const alerts = (ops?.alerts ?? []).slice(0, 3);

  const decide = async (approval: Approval, decision: "approve" | "reject") => {
    setApprovalMsg("");
    try {
      const res = await fetch(`/api/approvals/${approval.id}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      if (res.status === 401) {
        window.location.assign(`${appRoot()}app/login`);
        return;
      }
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        setApprovalMsg(String(json?.reason || json?.error || `HTTP ${res.status}`));
        return;
      }
      setApprovalMsg(decision === "approve" ? "已通过" : "已驳回");
      loadState();
    } catch (e: any) {
      setApprovalMsg(String(e?.message || e));
    }
  };

  const kpis = [
    { key: "total", label: "作品总数", value: metrics.totalWorks, hint: `${facets?.withFinal ?? 0} 件已成片`, tone: "" },
    { key: "done", label: "当天完成", value: metrics.doneToday, hint: "今天发布/归档", tone: "ok" },
    { key: "doing", label: "正在制作", value: metrics.doing, hint: "制作中", tone: "" },
    { key: "review", label: "待评审", value: metrics.review, hint: "待我审", tone: "warn" },
    { key: "published", label: "已发布", value: metrics.published, hint: "含归档", tone: "ok" },
    { key: "failing", label: "受阻 / 滞销", value: metrics.failing, hint: "未过审/积压/停滞", tone: "danger" },
  ];

  return (
    <section className="page ceo">
      <div className="page-head">
        <div>
          <h1>总览</h1>
          <p className="muted home-sub">CEO 视角 · 从作品运营到人员分工与系统运行的一屏概览</p>
        </div>
        <span className="muted nowrap">
          {state?.serverTime ? `服务时间 ${state.serverTime.replace("T", " ").slice(0, 19)}` : "连接中…"}
        </span>
      </div>

      {error && <div className="banner error-banner">状态加载失败：{error}</div>}

      {/* 1. 运营指标 */}
      <div className="ceo-kpis">
        {kpis.map((k) => (
          <div key={k.key} className={`ceo-kpi${k.tone ? ` ceo-kpi-${k.tone}` : ""}`}>
            <span className="ceo-kpi-label">{k.label}</span>
            <b className="ceo-kpi-value">{k.value}</b>
            <span className="ceo-kpi-hint">{k.hint}</span>
          </div>
        ))}
      </div>

      {/* 2. 作品表现 */}
      <div className="ceo-grid-2">
        <section className="card ceo-block">
          <div className="block-head">
            <div>
              <h2>热门作品 · 上线表现</h2>
              <p className="muted">已发布/归档作品按评审通过、完整度与更新时间排序</p>
            </div>
            <Link to="/works" className="ceo-more">作品档案 →</Link>
          </div>
          {hotWorks.length === 0 ? (
            <p className="muted">暂无已上线作品，先去作品档案把定稿推上线。</p>
          ) : (
            <div className="ceo-list">
              {hotWorks.map((w, i) => (
                <div key={w.id} className="ceo-row">
                  <span className="ceo-rank">{i + 1}</span>
                  <div className="ceo-row-main">
                    <div className="ceo-row-title">{w.title}</div>
                    <div className="ceo-row-sub">
                      <span className="ceo-dot" style={{ background: projColor(w.projectId) }} />
                      {w.projectName} · {w.status} · 完整度 {w.completeness}%
                    </div>
                  </div>
                  <span className={`ceo-pill${w.reviewPass === true ? " is-ok" : ""}`}>
                    {w.reviewPass === true ? "评审通过" : w.reviewPass === false ? "未通过" : "未评审"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="card ceo-block">
          <div className="block-head">
            <div>
              <h2>滞销 / 受阻作品</h2>
              <p className="muted">未通过评审、待评审积压、或制作长期停滞</p>
            </div>
            <Link to="/works" className="ceo-more">全部 →</Link>
          </div>
          {blockedWorks.length === 0 ? (
            <p className="muted">当前没有受阻作品，产线通畅。</p>
          ) : (
            <div className="ceo-list">
              {blockedWorks.map(({ w, reason }) => (
                <div key={w.id} className="ceo-row">
                  <span className="ceo-rank ceo-rank-warn">!</span>
                  <div className="ceo-row-main">
                    <div className="ceo-row-title">{w.title}</div>
                    <div className="ceo-row-sub">
                      <span className="ceo-dot" style={{ background: projColor(w.projectId) }} />
                      {w.projectName} · {w.stageName} · 更新 {shortTime(w.updatedAt)}
                    </div>
                  </div>
                  <span className="ceo-pill is-danger">{reason}</span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      {/* 3. 创作流水线 */}
      <section className="card ceo-block">
        <div className="block-head">
          <div>
            <h2>创作流水线</h2>
            <p className="muted">创意 → 选题 → 定稿 → 分镜 → 评审 → 发布 的当前分布</p>
          </div>
          <span className="pill-count">共 {metrics.totalWorks} 件在管</span>
        </div>
        <div className="ceo-funnel">
          {funnel.map((s, i) => (
            <div key={s.key} className="ceo-stage">
              <span className="ceo-stage-label">{s.label}</span>
              <b className="ceo-stage-value">{s.value}</b>
              {s.hint && <span className="ceo-stage-hint">{s.hint}</span>}
              {i < funnel.length - 1 && <span className="ceo-stage-arrow" aria-hidden="true">›</span>}
            </div>
          ))}
        </div>

        <div className="ceo-principle">
          <div className="ceo-principle-head">
            <h3>短视频架构确定原则</h3>
            <Link to="/knowledge" className="ceo-more">知识库 →</Link>
          </div>
          <div className="ceo-principle-grid">
            {VIDEO_PRINCIPLES.map((p) => (
              <div key={p.t} className="ceo-principle-item">
                <b>{p.t}</b>
                <span>{p.d}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* 4. 工位图：俯视 3D 工位区 */}
      <section className="card ceo-block floor-block">
        <div className="block-head">
          <div>
            <h2>工位图 · 任务分配</h2>
            <p className="muted">
              在岗 {floorAgents.length} 人 · 执行中 {statusSummary.working} · 会议中 {statusSummary.meeting} · 待命 {statusSummary.idle} · 已交付 {statusSummary.done}
            </p>
          </div>
          <div className="floor-legend">
            {Object.entries(STATUS_TEXT).map(([k, label]) => (
              <span key={k} className={`floor-legend-item is-${k}`}>
                <i />{label}
              </span>
            ))}
            <button
              type="button"
              className={`floor-sound${soundOn ? " is-on" : ""}`}
              onClick={() => setSoundOn((v) => !v)}
              aria-pressed={soundOn}
              title="悬停工位时播放轻提示音"
            >
              {soundOn ? "音效开" : "音效关"}
            </button>
            <Link to="/org" className="ceo-more">组织配置 →</Link>
          </div>
        </div>

        <div className="floor-stage">
          <div className="floor-room">
            {people.map((group) => {
              const zoneTier = group.key === "mgmt" ? "is-mgmt" : group.key === "mid" ? "is-mid" : "is-project";
              return (
                <div
                  key={group.key}
                  className={`floor-zone ${zoneTier}`}
                  style={{ "--zone": group.color } as React.CSSProperties}
                >
                  <div className="floor-zone-label">
                    <span className="ceo-dot" style={{ background: group.color }} />
                    {group.name}
                    <em>{group.members.length} 工位</em>
                  </div>
                  <div className="floor-desks">
                    {group.members.map((a, i) => {
                      const dr = deskRole(a.role, group.key === "mgmt");
                      const status = a.status || "idle";
                      const statusText = STATUS_TEXT[status] || status;
                      const photo = assetUrl(a.avatar || `/avatars/${a.id}.png`);
                      return (
                        <article
                          key={a.id}
                          className={`desk is-${status} role-${dr.key}${a.id === "secretary" ? " desk-secretary" : ""}`}
                          style={{ "--d": `${(i % 6) * 0.05}s` } as React.CSSProperties}
                          onPointerEnter={playHover}
                        >
                          <span className="desk-edge" aria-hidden="true" />

                          <span className="desk-avatar-wrap">
                            <span className="desk-avatar desk-avatar-fallback">{(a.name || "?").slice(0, 1)}</span>
                            <img
                              className="desk-avatar desk-avatar-img"
                              src={photo}
                              alt={`${a.name} 头像`}
                              loading="lazy"
                              decoding="async"
                              onError={(e) => {
                                const img = e.currentTarget;
                                if (img.dataset.fb !== "1") {
                                  img.dataset.fb = "1";
                                  img.src = assetUrl("/avatars/scout.png");
                                } else {
                                  img.style.display = "none";
                                }
                              }}
                            />
                          </span>

                          <div className="desk-info">
                            <span className="desk-role">{a.role}</span>
                            <b className="desk-name">{a.name}</b>
                            <span className="desk-duty" title={dutyFor(a.role, a.scope)}>
                              {dutyFor(a.role, a.scope)}
                            </span>
                          </div>

                          <span className={`desk-status is-${status}`} title={statusText} aria-label={statusText}>
                            <StatusIcon status={status} />
                          </span>

                          {/* 悬停：照片铺满桌面，保持比例裁剪 */}
                          <span className="desk-photo" aria-hidden="true">
                            <img
                              className="desk-photo-img"
                              src={photo}
                              alt=""
                              loading="lazy"
                              decoding="async"
                              onError={(e) => {
                                e.currentTarget.closest(".desk-photo")?.classList.add("is-broken");
                              }}
                            />
                            <span className="desk-photo-cap">{a.name} · {a.role}</span>
                          </span>
                        </article>
                      );
                    })}
                  </div>
                </div>
              );
            })}
            {!people.length && <p className="muted floor-empty">员工名册加载中…</p>}
          </div>
        </div>
      </section>

      {/* 5. IT 系统运行 + 审批 */}
      <div className="ceo-grid-2">
        <section className="card ceo-block">
          <div className="block-head">
            <div>
              <h2>IT 系统运行</h2>
              <p className="muted">服务健康、视频产线与告警</p>
            </div>
            <Link to="/ops" className="ceo-more">运维监控 →</Link>
          </div>
          <div className="ceo-sys">
            <div className="ceo-sys-row">
              <span>主服务</span>
              <span className={`ceo-pill${opsService?.ok ? " is-ok" : " is-danger"}`}>
                {opsService?.ok ? "运行中" : ops ? "异常" : "加载中"}
              </span>
            </div>
            <div className="ceo-sys-row">
              <span>已运行</span>
              <b>{fmtUptime(opsService?.uptimeSec)}</b>
            </div>
            <div className="ceo-sys-row">
              <span>视频服务</span>
              <b>{totalServices ? `${onlineServices}/${totalServices} 在线` : "-"}</b>
            </div>
            <div className="ceo-sys-row">
              <span>渲染队列</span>
              <b>
                执行 {runningJobs} · 阻塞 {pendingRender}
              </b>
            </div>
          </div>
          {alerts.length > 0 ? (
            <div className="ceo-alerts">
              {alerts.map((a) => (
                <div key={a.id} className={`ceo-alert is-${a.level}`}>
                  <b>{a.title}</b>
                  <span>{a.detail}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted ceo-ok">暂无告警，系统平稳。</p>
          )}
        </section>

        <section className="card ceo-block">
          <div className="block-head">
            <div>
              <h2>待我拍板</h2>
              <p className="muted">开工 / 发布 / 对外动作必须人工拍板</p>
            </div>
            <span className="pill-count">{pending.length} 待审批</span>
          </div>
          {approvalMsg && <p className="muted">{approvalMsg}</p>}
          {pending.length === 0 ? (
            <p className="muted">暂无待审批。AI 不得自行开工、发布或对外承诺。</p>
          ) : (
            <div className="approval-list">
              {pending.slice(-3).reverse().map((approval) => (
                <div key={approval.id} className="approval-card">
                  <h3>{approval.title || approval.action || approval.id}</h3>
                  <div className="approval-line">
                    风险：{approval.risk || "-"} · 动作：{approval.action || "-"} · 内容 {(approval.items || []).length} 条
                  </div>
                  <div className="approval-actions">
                    <button type="button" className="btn btn-ok" onClick={() => decide(approval, "approve")}>
                      {approval.action === "publish.execute" ? "通过并发布" : "通过并开工"}
                    </button>
                    <button type="button" className="btn btn-ghost" onClick={() => decide(approval, "reject")}>
                      驳回
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      <p className="ceo-note muted">
        说明：作品档案暂未接入平台播放量，热门/滞销以「上线状态 + 评审结论 + 完整度 + 停滞天数」等真实信号排序，未做播放数据臆造。
      </p>
    </section>
  );
}
