import { useCallback, useEffect, useRef, useState } from "react";

type OpsService = {
  id: string;
  name: string;
  url: string;
  online: boolean;
  status: string;
  health: Record<string, unknown>;
  jobs: Record<string, unknown>[];
  jobsNote: string;
};
type LocalJob = {
  id: string;
  kind: string;
  backend: string;
  status: string;
  prompt: string;
  progress: { step: number | null; total: number | null };
  createdAt: string;
  finishedAt: string;
  elapsedSec: number | null;
  error: string;
  outputPath: string;
};
type RenderItem = {
  id: string;
  title: string;
  stage: string;
  state: string;
  error: string;
  blockedBy: string;
  video: string;
};
type OpsAlert = {
  id: string;
  level: "critical" | "warning" | "info";
  title: string;
  detail: string;
  source: string;
  at: string;
  action: string;
};
type PendingApproval = {
  id: string;
  title: string;
  action: string;
  risk: string;
  status: string;
  createdAt: string;
};
type OpsData = {
  ok: boolean;
  generatedAt?: string;
  error?: string;
  service?: {
    ok: boolean; version: string; engine: string; scene: string;
    serverTime: string; serverStartedAt: string; uptimeSec: number;
    activeTrace: string; hermes: Record<string, number | string>;
  };
  stats?: Record<string, number>;
  pipeline?: {
    stageCounts: Record<string, number>;
    blockedBy: Record<string, number>;
    blocked: { id: string; title: string; stage: string; blockedBy: string }[];
    recent: { id: string; title: string; stage: string; blockedBy: string }[];
  };
  video?: {
    services: OpsService[];
    localJobsFile: string;
    localJobsAvailable: boolean;
    localJobs: LocalJob[];
    pipelineRender: RenderItem[];
    pendingRender: { id: string; title: string; stage: string; blockedBy: string }[];
  };
  alerts?: OpsAlert[];
  actions?: {
    pendingApprovals: PendingApproval[];
    supported: { action: string; label: string; detail: string }[];
    manualOnly: string;
  };
  notes?: string[];
};

const REFRESH_KINDS = new Set([
  "pipeline", "cost-alert", "render-gate", "tech-gate", "rework",
  "engine-meta", "sample-review", "approval", "report", "scene",
  "metrics", "config-reload", "agent",
]);

async function getOps(): Promise<OpsData> {
  const res = await fetch("/api/ops", { credentials: "include", cache: "no-store" });
  if (res.status === 401) {
    window.location.assign("/app/login");
    throw new Error("登录已过期");
  }
  if (!res.ok) throw new Error(`运维数据读取失败（HTTP ${res.status}）`);
  return (await res.json()) as OpsData;
}

function shortTime(at?: string) {
  if (!at) return "—";
  const m = at.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : at.slice(0, 16);
}

function duration(sec?: number) {
  const s = Math.max(0, Math.floor(sec || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d} 天 ${h} 小时`;
  if (h) return `${h} 小时 ${m} 分`;
  return `${m} 分钟`;
}

const STATUS_CN: Record<string, string> = {
  succeeded: "成功", failed: "失败", running: "执行中", queued: "排队中",
  interrupted: "已中断", unknown: "未知", ok: "通过", pending: "待出片",
};

const LEVEL_CN: Record<string, string> = { critical: "严重", warning: "告警", info: "提示" };

export function OpsPage() {
  const [data, setData] = useState<OpsData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await getOps();
      if (!next.ok) throw new Error(next.error || "运维数据不可用");
      setData(next);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const poll = window.setInterval(() => void load(), 15000);
    const es = new EventSource("/api/events");
    es.onmessage = (ev) => {
      try {
        const envelope = JSON.parse(ev.data) as { kind?: string };
        if (!envelope.kind || !REFRESH_KINDS.has(envelope.kind)) return;
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => void load(), 700);
      } catch {
        /* 心跳与非 JSON 帧忽略 */
      }
    };
    return () => {
      window.clearInterval(poll);
      es.close();
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load]);

  const recover = async (action: string, id?: string, confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(`${action}:${id || ""}`);
    setFeedback(null);
    try {
      const res = await fetch("/api/ops/recover", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, id }),
      });
      const body = (await res.json()) as { ok?: boolean; error?: string; result?: Record<string, unknown> };
      if (!res.ok || !body.ok) throw new Error(body.error || `HTTP ${res.status}`);
      const result = body.result || {};
      let label = "操作完成";
      if (action === "patrol") {
        label = `巡检结果：${String(result.action || "完成")}` +
          (result.traceId ? `（trace ${String(result.traceId)}）` : "");
      } else if (action === "reload") {
        label = `配置热加载：${result.agents ?? "?"} 人 / ${result.projects ?? "?"} 项目`;
      } else if (action === "approve" || action === "reject") {
        label = `审批 ${id} 已${action === "approve" ? "批准" : "驳回"}`;
      }
      setFeedback({ ok: true, text: `已执行 · ${label}` });
      await load();
    } catch (e) {
      setFeedback({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy("");
    }
  };

  const service = data?.service;
  const stats = data?.stats || {};
  const alerts = data?.alerts || [];
  const services = data?.video?.services || [];
  const jobs = data?.video?.localJobs || [];
  const renders = data?.video?.pipelineRender || [];
  const pendingRender = data?.video?.pendingRender || [];
  const approvals = data?.actions?.pendingApprovals || [];
  const stageCounts = Object.entries(data?.pipeline?.stageCounts || {}).sort((a, b) => b[1] - a[1]);
  const maxStage = Math.max(1, ...stageCounts.map(([, n]) => n));

  return (
    <div className="ops-page">
      <div className="page-head">
        <div>
          <h1>运维监控</h1>
          <p className="ins-sub">
            CTO 视角 · 系统运行、任务执行、视频生成链路与异常处置；发现问题可直接在右侧恢复
          </p>
        </div>
        <div className="ins-head-actions">
          <span className="ins-updated">
            {data?.generatedAt ? `快照 ${shortTime(data.generatedAt)}` : loading ? "正在读取运行状态…" : ""}
          </span>
          <button type="button" className="btn btn-ghost" onClick={() => void load()} disabled={loading}>
            立即刷新
          </button>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}
      {feedback && (
        <div className={feedback.ok ? "ops-feedback ok" : "ops-feedback bad"}>{feedback.text}</div>
      )}

      <section className="ops-status" aria-label="系统运行状态">
        <div className="ops-status-item">
          <span>主服务</span>
          <b className={service?.ok ? "ok-text" : "ops-bad"}>{service?.ok ? "运行中" : "异常"}</b>
          <small>启动于 {shortTime(service?.serverStartedAt)}</small>
        </div>
        <div className="ops-status-item">
          <span>连续运行</span>
          <b>{duration(service?.uptimeSec)}</b>
          <small>场景 {service?.scene || "—"}</small>
        </div>
        <div className="ops-status-item">
          <span>引擎 / 模型</span>
          <b className="ops-model">{service?.engine || "—"}</b>
          <small>{String(service?.hermes?.model || "—")}</small>
        </div>
        <div className="ops-status-item">
          <span>Hermes 调用</span>
          <b>{stats.hermesCalls ?? "-"}</b>
          <small>失败 {stats.hermesFailures ?? "-"} · 均时延 {stats.hermesAvgLatencyMs ?? "-"} ms</small>
        </div>
        <div className="ops-status-item">
          <span>流水线 / 阻塞</span>
          <b>{stats.pipelineTotal ?? "-"} / <em className="ops-bad">{stats.blockedTotal ?? "-"}</em></b>
          <small>活跃 trace {service?.activeTrace || "无"}</small>
        </div>
        <div className="ops-status-item">
          <span>视频任务</span>
          <b>{stats.mediaJobs ?? "-"}</b>
          <small>失败 {stats.mediaJobsFailed ?? "-"} · 滞留 {stats.mediaJobsStuck ?? "-"}</small>
        </div>
      </section>

      <div className="ops-layout">
        <section className="ops-card">
          <h2>任务执行情况</h2>
          <div className="ops-stage-list">
            {stageCounts.length === 0 && <p className="muted">暂无流水线内容条目。</p>}
            {stageCounts.map(([stage, count]) => (
              <div key={stage} className="ops-stage">
                <span>{stage}</span>
                <div className="ops-bar"><i style={{ width: `${(count / maxStage) * 100}%` }} /></div>
                <b>{count}</b>
              </div>
            ))}
          </div>
          <h3>阻塞条目 <small className="muted">（{data?.pipeline?.blocked?.length || 0}）</small></h3>
          <ul className="ops-list">
            {(data?.pipeline?.blocked || []).slice(-8).reverse().map((it) => (
              <li key={it.id}>
                <span className="ops-pill warn">{it.blockedBy}</span>
                <span className="ops-list-title" title={it.title}>{it.title || it.id}</span>
              </li>
            ))}
            {!(data?.pipeline?.blocked || []).length && <li className="muted">当前无阻塞条目。</li>}
          </ul>
          <h3>最近流水线</h3>
          <ul className="ops-list">
            {(data?.pipeline?.recent || []).slice(-6).reverse().map((it) => (
              <li key={it.id}>
                <span className="ops-pill">{it.stage}</span>
                <span className="ops-list-title" title={it.title}>{it.title || it.id}</span>
              </li>
            ))}
          </ul>
        </section>

        <section className="ops-card">
          <h2>异常与处置 <small className="muted">（{alerts.length} 条）</small></h2>
          <ul className="ops-alerts">
            {alerts.map((a) => (
              <li key={a.id} className={`ops-alert lv-${a.level}`}>
                <div className="ops-alert-head">
                  <span className="ops-level">{LEVEL_CN[a.level] || a.level}</span>
                  <b>{a.title}</b>
                </div>
                <p>{a.detail}</p>
                <small>{a.source} · {shortTime(a.at)}</small>
                <div className="ops-alert-action">建议：{a.action}</div>
              </li>
            ))}
            {!alerts.length && <li className="muted">当前未发现异常。</li>}
          </ul>

          <h3>恢复运行</h3>
          <div className="ops-actions">
            <button
              type="button"
              className="btn btn-primary"
              disabled={!!busy}
              onClick={() => void recover(
                "patrol",
                undefined,
                "将触发 CEO 巡检：清理陈旧 trace，若无人审批且无在跑任务会派单「全员复工」，可能启动新一轮编排。确认继续？",
              )}
            >
              {busy.startsWith("patrol") ? "执行中…" : "一键恢复运行"}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={!!busy}
              onClick={() => void recover("reload")}
            >
              {busy.startsWith("reload") ? "执行中…" : "热加载配置"}
            </button>
          </div>
          <p className="muted ops-hint">
            巡检会按「待审批 → 有任务在跑 → 派单复工」顺序判断；配置热加载校验失败会保留旧值。
          </p>

          <h3>待处理审批 <small className="muted">（{approvals.length}）</small></h3>
          {approvals.length === 0 && <p className="muted">当前没有 pending 审批单。</p>}
          {approvals.map((a) => (
            <div key={a.id} className="ops-approval">
              <div>
                <b title={a.title}>{a.title || a.action}</b>
                <small>{a.id} · {a.action} · 风险 {a.risk || "—"} · {shortTime(a.createdAt)}</small>
              </div>
              <div className="ops-approval-btns">
                <button className="btn btn-ghost" disabled={!!busy}
                  onClick={() => void recover("approve", a.id, `确认批准「${a.title || a.id}」？`)}>批准</button>
                <button className="btn btn-ghost" disabled={!!busy}
                  onClick={() => void recover("reject", a.id, `确认驳回「${a.title || a.id}」？`)}>驳回</button>
              </div>
            </div>
          ))}
          <p className="muted ops-hint">{data?.actions?.manualOnly}</p>
        </section>
      </div>

      <section className="ops-card ops-video">
        <h2>视频生成环节</h2>
        <div className="ops-services">
          <div className="ops-service">
            <span className="ops-pill ok">主流水线</span>
            <b>om_render / Remotion 出片</b>
            <small>渲染条目 {renders.length} · 失败 {stats.renderFailed ?? "-"}</small>
          </div>
          {services.map((s) => (
            <div key={s.id} className="ops-service">
              <span className={`ops-pill ${s.online ? (s.status === "degraded" ? "warn" : "ok") : "bad"}`}>
                {s.online ? (s.status === "degraded" ? "降级" : "在线") : "离线"}
              </span>
              <b>{s.name}</b>
              <small>{s.url} · {s.jobsNote}</small>
            </div>
          ))}
        </div>

        <h3>生成任务记录 <small className="muted">（{data?.video?.localJobsFile || "media-gen/output/jobs.json"}）</small></h3>
        <div className="table-wrap">
          <table className="table ops-table">
            <thead>
              <tr><th>任务</th><th>类型</th><th>状态</th><th>进度</th><th>创建</th><th>耗时</th><th>说明</th></tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id}>
                  <td className="mono">{j.id}</td>
                  <td>{j.kind === "video" ? "视频" : j.kind === "image" ? "图片" : j.kind}</td>
                  <td><span className={`ops-pill ${j.status === "succeeded" ? "ok" : j.status === "failed" || j.status === "interrupted" ? "bad" : "warn"}`}>
                    {STATUS_CN[j.status] || j.status}</span></td>
                  <td>{j.progress?.step ?? "-"}/{j.progress?.total ?? "-"}</td>
                  <td className="nowrap">{shortTime(j.createdAt)}</td>
                  <td className="nowrap">{j.elapsedSec != null ? `${Math.round(j.elapsedSec)}s` : "—"}</td>
                  <td className="ops-error" title={j.error || j.prompt}>
                    {j.error || j.prompt || j.outputPath || "—"}
                  </td>
                </tr>
              ))}
              {!jobs.length && (
                <tr><td colSpan={7} className="muted">暂无落盘任务记录。</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <h3>待出片内容 <small className="muted">（卡在门禁，共 {stats.blockedTotal ?? 0} 条，展示最近 {pendingRender.length} 条）</small></h3>
        <ul className="ops-list">
          {pendingRender.slice().reverse().map((r) => (
            <li key={r.id}>
              <span className="ops-pill warn">{r.blockedBy || r.stage}</span>
              <span className="ops-list-title" title={r.title}>{r.title || r.id}</span>
            </li>
          ))}
          {!pendingRender.length && <li className="muted">没有卡在门禁的内容。</li>}
        </ul>

        <h3>主流水线出片结果</h3>
        {renders.length === 0 && <p className="muted">当前流水线条目未带出片结果（尚未进入渲染阶段）。</p>}
        <ul className="ops-list">
          {renders.slice(-10).reverse().map((r) => (
            <li key={r.id}>
              <span className={`ops-pill ${r.state === "ok" ? "ok" : r.state === "failed" ? "bad" : "warn"}`}>
                {STATUS_CN[r.state] || r.state}
              </span>
              <span className="ops-list-title" title={r.title}>{r.title || r.id}</span>
              <small className="ops-error">{r.blockedBy || r.error || ""}</small>
            </li>
          ))}
        </ul>
      </section>

      <section className="ins-sources">
        <h2>口径说明</h2>
        <ul>{(data?.notes || []).map((n, i) => <li key={i}>{n}</li>)}</ul>
      </section>
    </div>
  );
}
