import { useCallback, useEffect, useMemo, useState } from "react";

/* 分镜工作台：分镜清单可改提示词/台词/时长，素材（图片/视频/音频）按渠道并发投递，
   同一个提示词可同时发多个渠道；每路作业独立进度、独立启停、失败重来。
   视频有时长上限（渠道注册表 maxSec 与全局限额取小），超限由后端夹取并在作业上标注。 */

type Kind = "image" | "video" | "audio";

interface ChannelParam {
  key: string;
  label: string;
  type: "int" | "float" | "text" | "select";
  default: string | number;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
}
interface Channel {
  name: string;
  kind: Kind;
  label: string;
  notes: string;
  maxSec: number | null;
  concurrency: number;
  available: boolean;
  reason: string;
  params: ChannelParam[];
}
interface Job {
  id: string;
  shotNo: number;
  kind: Kind;
  channel: string;
  status: "queued" | "running" | "done" | "failed" | "canceled";
  progress: number;
  outputUrl: string;
  durationSec: number;
  error: string;
  note: string;
  runNo: number;
  createdAt: string;
}
interface Shot {
  no: number;
  prompt: string;
  line: string;
  sec: number;
  asset: string;
  channels: string[];
  params: Record<string, Record<string, string | number>>;
  jobs: Job[];
  summary: { total: number; active: number; done: number; failed: number; progress: number };
}
interface ShotsPayload {
  ok: boolean;
  error?: string;
  traceId: string;
  itemId: string;
  projectId: string;
  title: string;
  stage: string;
  seconds: number;
  maxSec: number;
  channels: Channel[];
  shots: Shot[];
  activeTrace: string;
  stages: { stage: string; label: string }[];
}
interface StageRun {
  id: string;
  stage: string;
  label: string;
  status: string;
  queuedAt: string;
  startedAt: string;
  finishedAt: string;
  note: string;
}

const KIND_LABEL: Record<Kind, string> = { image: "图片", video: "视频", audio: "音频" };
const JOB_LABEL: Record<string, string> = {
  queued: "排队", running: "生成中", done: "已完成", failed: "失败", canceled: "已停止",
};
const STAGE_LABEL: Record<string, string> = {
  render: "素材制作", sample_review: "剪辑合成", final: "终版合成",
};

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = (await res.json().catch(() => ({}))) as T & { ok?: boolean; error?: string };
  if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include", cache: "no-store" });
  if (res.status === 401) {
    window.location.assign("/app/login");
    throw new Error("登录已过期");
  }
  return (await res.json()) as T;
}

function shortTime(value?: string) {
  return value ? value.replace("T", " ").slice(5, 19) : "";
}

export function FactoryShotsPanel({
  traceId,
  itemId,
  isBoss,
  onClose,
}: {
  traceId: string;
  itemId: string;
  isBoss: boolean;
  onClose: () => void;
}) {
  const [data, setData] = useState<ShotsPayload | null>(null);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");
  const [kind, setKind] = useState<Kind>("image");
  const [channels, setChannels] = useState<string[]>([]);
  const [params, setParams] = useState<Record<string, Record<string, string | number>>>({});
  const [showParams, setShowParams] = useState(false);
  const [selected, setSelected] = useState<number[]>([]);
  const [drafts, setDrafts] = useState<Record<number, { prompt: string; line: string; sec: number }>>({});
  const [runs, setRuns] = useState<StageRun[]>([]);

  const qs = `traceId=${encodeURIComponent(traceId)}&itemId=${encodeURIComponent(itemId)}`;

  const applyPayload = useCallback((d: ShotsPayload) => {
    setData(d);
    setDrafts((prev) => {
      const next: Record<number, { prompt: string; line: string; sec: number }> = {};
      for (const s of d.shots) {
        const cur = prev[s.no];
        // 已有本地编辑不覆盖，避免轮询把正在改的提示词刷掉
        next[s.no] = cur ?? { prompt: s.prompt, line: s.line, sec: s.sec };
      }
      return next;
    });
    setSelected((prev) => (prev.length ? prev.filter((n) => d.shots.some((s) => s.no === n)) : d.shots.map((s) => s.no)));
  }, []);

  const load = useCallback(async () => {
    try {
      const d = await getJson<ShotsPayload>(`/api/factory/shots?${qs}`);
      if (d.ok === false) throw new Error(d.error || "加载失败");
      applyPayload(d);
      setError("");
      // 渠道与参数默认值只在首次载入时初始化
      setChannels((prev) => {
        if (prev.length) return prev;
        const first = d.channels.find((c) => c.kind === kind && c.available) ?? d.channels.find((c) => c.kind === kind);
        return first ? [first.name] : [];
      });
      setParams((prev) => (Object.keys(prev).length ? prev : {}));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [qs, applyPayload, kind]);

  const loadRuns = useCallback(async () => {
    try {
      const d = await getJson<{ ok: boolean; runs: StageRun[] }>(`/api/factory/stage/runs?${qs}`);
      setRuns(d.runs ?? []);
    } catch {
      /* 环节记录是辅助信息，失败不影响工作台 */
    }
  }, [qs]);

  useEffect(() => {
    void load();
    void loadRuns();
  }, [load, loadRuns]);

  // 作业进度：2s 轮询（只刷作业，不动正在编辑的草稿）
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const d = await getJson<{ ok: boolean; jobs: Job[] }>(`/api/factory/jobs?${qs}`);
        if (!d.ok) return;
        setData((prev) => {
          if (!prev) return prev;
          const byShot = new Map<number, Job[]>();
          for (const j of d.jobs) {
            const arr = byShot.get(j.shotNo) ?? [];
            arr.push(j);
            byShot.set(j.shotNo, arr);
          }
          const shots = prev.shots.map((s) => {
            const jobs = byShot.get(s.no) ?? [];
            return {
              ...s,
              jobs,
              summary: {
                total: jobs.length,
                active: jobs.filter((j) => j.status === "queued" || j.status === "running").length,
                done: jobs.filter((j) => j.status === "done").length,
                failed: jobs.filter((j) => j.status === "failed").length,
                progress: Math.max(0, ...jobs.map((j) => j.progress || 0)),
              },
            };
          });
          return { ...prev, shots };
        });
      } catch {
        /* 轮询失败静默，页面顶部有主加载错误提示 */
      }
    }, 2000);
    return () => clearInterval(t);
  }, [qs]);

  // 有在跑作业时，环节记录也跟着刷（手工启动的排队/下发状态）
  const hasActive = useMemo(
    () => (data?.shots ?? []).some((s) => s.jobs.some((j) => j.status === "queued" || j.status === "running")),
    [data],
  );
  useEffect(() => {
    if (!hasActive) return;
    const t = setInterval(() => void loadRuns(), 3000);
    return () => clearInterval(t);
  }, [hasActive, loadRuns]);

  const kindChannels = useMemo(
    () => (data?.channels ?? []).filter((c) => c.kind === kind),
    [data, kind],
  );
  const picked = useMemo(
    () => kindChannels.filter((c) => channels.includes(c.name)),
    [kindChannels, channels],
  );
  // 视频上限：所选渠道里最严的那个
  const videoCap = useMemo(() => {
    if (kind !== "video") return 0;
    const caps = picked.map((c) => c.maxSec || data?.maxSec || 0).filter(Boolean);
    return caps.length ? Math.min(...caps) : (data?.maxSec ?? 0);
  }, [kind, picked, data]);

  const toggleChannel = (name: string) => {
    setChannels((prev) => (prev.includes(name) ? prev.filter((x) => x !== name) : [...prev, name]));
  };
  const toggleShot = (no: number) => {
    setSelected((prev) => (prev.includes(no) ? prev.filter((x) => x !== no) : [...prev, no]));
  };
  const setDraft = (no: number, patch: Partial<{ prompt: string; line: string; sec: number }>) => {
    setDrafts((prev) => ({ ...prev, [no]: { ...(prev[no] ?? { prompt: "", line: "", sec: 0 }), ...patch } }));
  };
  const paramValue = (name: string, spec: ChannelParam) => {
    const v = params[name]?.[spec.key];
    return v === undefined ? spec.default : v;
  };
  const setParam = (name: string, key: string, value: string | number) => {
    setParams((prev) => ({ ...prev, [name]: { ...(prev[name] ?? {}), [key]: value } }));
  };

  const saveShot = async (no: number, extra?: Partial<{ prompt: string; line: string; sec: number }>, silent = false) => {
    const d = drafts[no];
    const fields = {
      prompt: extra?.prompt ?? d?.prompt ?? "",
      line: extra?.line ?? d?.line ?? "",
      sec: extra?.sec ?? d?.sec ?? 0,
      channels,
      params,
    };
    try {
      await post(`/api/factory/shots/save`, {
        traceId, itemId, shotNo: no, fields,
      });
      if (!silent) setMsg(`分镜 ${no} 已保存（提示词/台词/时长/渠道参数）`);
      setData((prev) => prev ? {
        ...prev,
        shots: prev.shots.map((s) => s.no === no
          ? { ...s, prompt: String(fields.prompt), line: String(fields.line), sec: Number(fields.sec), channels }
          : s),
      } : prev);
    } catch (e) {
      setMsg(`分镜 ${no} 保存失败：${(e as Error).message}`);
      throw e;
    }
  };

  const startShots = async (shotNos: number[]) => {
    if (!shotNos.length) {
      setMsg("请先勾选要制作的分镜");
      return;
    }
    if (!channels.length) {
      setMsg("请先选择制作渠道（可多选，同一提示词并行投递）");
      return;
    }
    setBusy("start");
    setMsg("");
    try {
      // 先把草稿落库，保证跑的就是看到的提示词
      for (const no of shotNos) await saveShot(no, undefined, true);
      const res = await post<{ ok: boolean; jobs: Job[]; warnings: string[] }>(
        `/api/factory/jobs/start`,
        { traceId, itemId, kind, channels, shots: shotNos, params },
      );
      const warn = res.warnings?.length ? `；提示：${res.warnings.slice(0, 3).join("；")}` : "";
      setMsg(`已提交 ${res.jobs.length} 路作业（${shotNos.length} 个分镜 × ${channels.length} 个渠道）${warn}`);
      await load();
    } catch (e) {
      setMsg(`启动失败：${(e as Error).message}`);
    } finally {
      setBusy("");
    }
  };

  const stopJobs = async (jobIds: string[]) => {
    if (!jobIds.length) return;
    setBusy("stop");
    try {
      await post(`/api/factory/jobs/cancel`, { jobIds });
      setMsg(`已停止 ${jobIds.length} 路作业`);
      await load();
    } catch (e) {
      setMsg(`停止失败：${(e as Error).message}`);
    } finally {
      setBusy("");
    }
  };

  const retryJobs = async (jobIds: string[]) => {
    if (!jobIds.length) return;
    setBusy("retry");
    try {
      const res = await post<{ results: { ok: boolean }[] }>(`/api/factory/jobs/retry`, { jobIds });
      setMsg(`已重投 ${res.results.filter((r) => r.ok).length} 路作业`);
      await load();
    } catch (e) {
      setMsg(`重来失败：${(e as Error).message}`);
    } finally {
      setBusy("");
    }
  };

  const startStage = async (stage: string) => {
    setBusy(`stage-${stage}`);
    setMsg("");
    try {
      await post(`/api/factory/stage/start`, { traceId, stage });
      setMsg(`已排入「${STAGE_LABEL[stage] || stage}」手工启动队列（可多次启动，按队列依次下发）`);
      await loadRuns();
    } catch (e) {
      setMsg(`${STAGE_LABEL[stage] || stage}启动失败：${(e as Error).message}`);
    } finally {
      setBusy("");
    }
  };

  const shots = data?.shots ?? [];
  const activeShots = shots.filter((s) => s.summary.active > 0);

  return (
    <div className="fac-ws">
      <div className="fac-ws-head">
        <h4>分镜工作台{data?.title ? ` · ${data.title}` : ""}</h4>
        <span className="muted">
          共 {shots.length} 镜 · 视频单镜上限 {data?.maxSec ?? "-"}s · 选中 {selected.length} 镜
          {hasActive ? ` · 在跑 ${activeShots.length} 镜` : ""}
        </span>
        <button type="button" className="btn btn-ghost fac-btn" onClick={onClose}>收起</button>
      </div>

      {error && <div className="fac-error">分镜加载失败：{error}</div>}
      {msg && <div className="fac-note">{msg}</div>}

      {/* 三环节手工多次启动 */}
      <div className="fac-ws-stages">
        <div className="fac-ws-stages-title">
          <b>环节手工启动</b>
          <span className="muted">每次点击排一条，按队列依次下发；同一环节可反复启动</span>
        </div>
        <div className="fac-ws-stage-btns">
          {(data?.stages ?? Object.keys(STAGE_LABEL).map((s) => ({ stage: s, label: STAGE_LABEL[s] }))).map((s) => {
            const running = runs.find((r) => r.stage === s.stage && (r.status === "running" || r.status === "queued"));
            return (
              <button
                key={s.stage}
                type="button"
                className={`btn fac-btn ${running ? "btn-ghost" : ""}`}
                disabled={!isBoss || busy === `stage-${s.stage}`}
                title={running ? `队列中：${running.status}` : `从「${s.label}」环节重新下发`}
                onClick={() => void startStage(s.stage)}
              >
                {busy === `stage-${s.stage}` ? "下发中…" : `启动${s.label}`}
                {running && <span className="fac-ws-stage-dot" aria-hidden="true" />}
              </button>
            );
          })}
        </div>
        {runs.length > 0 && (
          <div className="fac-ws-runs">
            {runs.slice(0, 5).map((r) => (
              <span key={r.id} className={`fac-ws-run s-${r.status}`}>
                {r.label} · {r.status === "queued" ? "排队中" : r.status === "running" ? "下发中" : r.status === "failed" ? "失败" : "已下发"}
                <em>{shortTime(r.startedAt || r.queuedAt)}</em>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* 制作方案：素材类型 + 渠道（可多选并行）+ 参数 */}
      <div className="fac-ws-plan">
        <div className="fac-ws-row">
          <div className="fac-ws-kinds" role="tablist" aria-label="素材类型">
            {(["image", "video", "audio"] as Kind[]).map((k) => (
              <button
                key={k}
                type="button"
                role="tab"
                aria-selected={kind === k}
                className={`fac-ws-kind${kind === k ? " on" : ""}`}
                onClick={() => {
                  setKind(k);
                  const first = (data?.channels ?? []).find((c) => c.kind === k && c.available)
                    ?? (data?.channels ?? []).find((c) => c.kind === k);
                  setChannels(first ? [first.name] : []);
                }}
              >
                {KIND_LABEL[k]}
              </button>
            ))}
          </div>
          <button type="button" className="btn btn-ghost fac-btn" onClick={() => setShowParams((v) => !v)}>
            {showParams ? "隐藏渠道参数" : "渠道参数"}
          </button>
          <span className="muted">
            {kind === "video"
              ? `单镜时长上限 ${videoCap || data?.maxSec || "-"}s（超限自动夹到上限）`
              : kind === "audio" ? "音频按该镜「台词」合成" : "图片按该镜「提示词」生成"}
          </span>
        </div>

        <div className="fac-ws-channels">
          {kindChannels.map((c) => {
            const on = channels.includes(c.name);
            return (
              <button
                key={c.name}
                type="button"
                className={`fac-ws-chan${on ? " on" : ""}${c.available ? "" : " off"}`}
                title={c.available ? c.notes : `不可用：${c.reason}`}
                onClick={() => toggleChannel(c.name)}
              >
                <b>{c.label}</b>
                <em>
                  {c.available ? `并发 ${c.concurrency}` : "不可用"}
                  {c.maxSec ? ` · ≤${c.maxSec}s` : ""}
                </em>
              </button>
            );
          })}
        </div>

        {showParams && (
          <div className="fac-ws-params">
            {picked.length === 0 && <div className="fac-empty">先选渠道，再设参数</div>}
            {picked.map((c) => (
              <div key={c.name} className="fac-ws-param-card">
                <div className="fac-ws-param-head">
                  <b>{c.label}</b>
                  <span className="muted">{c.notes}</span>
                </div>
                <div className="fac-ws-param-grid">
                  {c.params.map((p) => (
                    <label key={p.key} className="fac-ws-field">
                      <span>{p.label}</span>
                      {p.type === "select" ? (
                        <select
                          value={String(paramValue(c.name, p))}
                          onChange={(e) => setParam(c.name, p.key, e.target.value)}
                        >
                          {(p.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
                        </select>
                      ) : p.type === "text" ? (
                        <input
                          value={String(paramValue(c.name, p))}
                          onChange={(e) => setParam(c.name, p.key, e.target.value)}
                        />
                      ) : (
                        <input
                          type="number"
                          min={p.min}
                          max={p.max}
                          step={p.step ?? (p.type === "int" ? 1 : 0.1)}
                          value={String(paramValue(c.name, p))}
                          onChange={(e) => setParam(c.name, p.key, Number(e.target.value))}
                        />
                      )}
                    </label>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 批量操作条 */}
      <div className="fac-ws-toolbar">
        <button
          type="button"
          className="btn btn-ghost fac-btn"
          onClick={() => setSelected(selected.length === shots.length ? [] : shots.map((s) => s.no))}
        >
          {selected.length === shots.length ? "取消全选" : "全部分镜"}
        </button>
        <button
          type="button"
          className="btn fac-btn"
          disabled={!isBoss || busy === "start" || !selected.length}
          onClick={() => void startShots(selected)}
        >
          {busy === "start" ? "提交中…" : `启动 ${selected.length} 镜 × ${channels.length} 渠道`}
        </button>
        <button
          type="button"
          className="btn btn-ghost fac-btn"
          disabled={!isBoss || !activeShots.length}
          onClick={() => void stopJobs(activeShots.flatMap((s) => s.jobs.filter((j) => j.status === "queued" || j.status === "running").map((j) => j.id)))}
        >
          停止全部在跑
        </button>
        <button
          type="button"
          className="btn btn-ghost fac-btn"
          disabled={!isBoss}
          onClick={() => void retryJobs(shots.flatMap((s) => s.jobs.filter((j) => j.status === "failed").map((j) => j.id)))}
        >
          重试全部失败
        </button>
      </div>

      {/* 分镜清单 */}
      <div className="fac-ws-shots">
        {shots.length === 0 && (
          <div className="fac-empty">该内容单还没有分镜清单：先在方案环节生成分镜（含画面/台词/时长）。</div>
        )}
        {shots.map((s) => {
          const d = drafts[s.no] ?? { prompt: s.prompt, line: s.line, sec: s.sec };
          const cap = kind === "video" ? (videoCap || data?.maxSec || 0) : 0;
          const over = cap > 0 && Number(d.sec) > cap + 0.01;
          const active = s.jobs.filter((j) => j.status === "queued" || j.status === "running");
          const failed = s.jobs.filter((j) => j.status === "failed");
          const latest = s.jobs.slice(0, 6);
          return (
            <div key={s.no} className={`fac-ws-shot${selected.includes(s.no) ? " on" : ""}`}>
              <div className="fac-ws-shot-head">
                <label className="fac-ws-pick">
                  <input type="checkbox" checked={selected.includes(s.no)} onChange={() => toggleShot(s.no)} />
                  <b>镜 {String(s.no).padStart(2, "0")}</b>
                </label>
                {s.asset && <span className="fac-ws-asset">{s.asset}</span>}
                <span className="muted">
                  {s.jobs.length ? `作业 ${s.jobs.length} 路 · 完成 ${s.summary.done} · 失败 ${s.summary.failed}` : "未制作"}
                </span>
                {s.summary.active > 0 && (
                  <span className="fac-ws-mini-progress" title={`${s.summary.progress}%`}>
                    <i style={{ width: `${s.summary.progress}%` }} />
                  </span>
                )}
                <div className="fac-ws-shot-ops">
                  <button type="button" className="btn btn-ghost fac-btn" disabled={!isBoss} onClick={() => void saveShot(s.no)}>
                    保存
                  </button>
                  <button
                    type="button"
                    className="btn fac-btn"
                    disabled={!isBoss || busy === "start" || !channels.length}
                    onClick={() => void startShots([s.no])}
                  >
                    启动
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost fac-btn"
                    disabled={!isBoss || !active.length}
                    onClick={() => void stopJobs(active.map((j) => j.id))}
                  >
                    停止
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost fac-btn"
                    disabled={!isBoss || !failed.length}
                    onClick={() => void retryJobs(failed.map((j) => j.id))}
                  >
                    重来
                  </button>
                </div>
              </div>

              <div className="fac-ws-fields">
                <label className="fac-ws-field wide">
                  <span>提示词（画面）</span>
                  <textarea
                    rows={2}
                    value={d.prompt}
                    onChange={(e) => setDraft(s.no, { prompt: e.target.value })}
                    placeholder="这一镜要生成的画面描述"
                  />
                </label>
                <label className="fac-ws-field">
                  <span>台词 / 旁白{kind === "audio" ? "（音频用）" : ""}</span>
                  <input
                    value={d.line}
                    onChange={(e) => setDraft(s.no, { line: e.target.value })}
                    placeholder="这一镜的口播文案"
                  />
                </label>
                <label className={`fac-ws-field${over ? " warn" : ""}`}>
                  <span>时长（秒）{cap ? ` ≤${cap}` : ""}</span>
                  <input
                    type="number"
                    min={0}
                    step={0.5}
                    max={cap || undefined}
                    value={String(d.sec)}
                    onChange={(e) => setDraft(s.no, { sec: Number(e.target.value) })}
                  />
                  {over && <em className="fac-ws-warn">超过上限，将夹到 {cap}s</em>}
                </label>
              </div>

              {latest.length > 0 && (
                <div className="fac-ws-jobs">
                  {latest.map((j) => (
                    <div key={j.id} className={`fac-ws-job s-${j.status}`}>
                      <span className={`fac-ws-job-chan k-${j.kind}`}>{j.channel}</span>
                      <span className={`fac-ws-job-st s-${j.status}`}>{JOB_LABEL[j.status] || j.status}</span>
                      <span className="fac-ws-job-bar"><i style={{ width: `${j.progress}%` }} /></span>
                      <span className="fac-ws-job-pct">{j.progress}%</span>
                      {j.durationSec > 0 && <span className="muted">{j.durationSec}s</span>}
                      {j.outputUrl && (
                        j.kind === "image"
                          ? <a className="fac-ws-out" href={j.outputUrl} target="_blank" rel="noopener noreferrer">
                              <img src={j.outputUrl} alt={`镜${j.shotNo} ${j.channel}`} loading="lazy" />
                            </a>
                          : <a className="fac-ws-out-link" href={j.outputUrl} target="_blank" rel="noopener noreferrer">
                              {j.kind === "video" ? "▶ 播放" : "♪ 试听"}
                            </a>
                      )}
                      {(j.status === "queued" || j.status === "running") && (
                        <button type="button" className="fac-ws-link" disabled={!isBoss} onClick={() => void stopJobs([j.id])}>
                          停
                        </button>
                      )}
                      {j.status === "failed" && (
                        <button type="button" className="fac-ws-link" disabled={!isBoss} onClick={() => void retryJobs([j.id])}>
                          重来
                        </button>
                      )}
                      {j.error && <span className="fac-ws-job-err" title={j.error}>{j.error}</span>}
                      {!j.error && j.note && <span className="fac-ws-job-note" title={j.note}>{j.note}</span>}
                      <span className="fac-ws-job-at muted">{shortTime(j.createdAt)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}