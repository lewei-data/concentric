import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { MediaViewer } from "../components/MediaViewer";
import { siteUrl } from "../lib/siteUrl";
import { appRoot } from "../appRoot";

type Kind = "image" | "audio" | "video";

interface ParamSpec {
  key: string;
  label: string;
  type: "int" | "float" | "text" | "select" | string;
  default: unknown;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
}

interface Channel {
  name: string;
  kind: string;
  label: string;
  notes: string;
  available: boolean;
  reason: string;
  params: ParamSpec[];
  maxSec?: number | null;
}

interface Job {
  id: string;
  kind: string;
  channel: string;
  prompt: string;
  status: string;
  progress: number;
  outputUrl: string;
  error: string;
  note: string;
  durationSec: number;
  createdAt: string;
}

const KINDS: { key: Kind; label: string; sub: string }[] = [
  { key: "image", label: "图片生成", sub: "提示词 → 单张成品" },
  { key: "audio", label: "口播语音", sub: "台词 + 音色 → wav" },
  { key: "video", label: "短视频", sub: "文生视频 → 工厂排队出片" },
];

const STATUS_TEXT: Record<string, string> = {
  queued: "排队中",
  running: "生成中",
  done: "已完成",
  failed: "失败",
  canceled: "已停止",
};

function defaultsOf(ch?: Channel | null): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  (ch?.params || []).forEach((p) => { out[p.key] = p.default; });
  return out;
}

function ParamField({
  spec,
  value,
  onChange,
}: {
  spec: ParamSpec;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  if (spec.type === "select") {
    return (
      <label className="tb-field">
        <span>{spec.label}</span>
        <select value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
          {(spec.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      </label>
    );
  }
  if (spec.type === "int" || spec.type === "float") {
    const num = Number(value);
    return (
      <label className="tb-field">
        <span>{spec.label}</span>
        <input
          type="number"
          value={Number.isFinite(num) ? num : ""}
          min={spec.min}
          max={spec.max}
          step={spec.step ?? (spec.type === "int" ? 1 : 0.1)}
          onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
        />
      </label>
    );
  }
  return (
    <label className="tb-field tb-field--wide">
      <span>{spec.label}</span>
      <input type="text" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

/** 从成品 URL 取出所在目录（相对浏览根）。
 *  工具箱成品现在落在个人空间：/private/<手机号>/工具箱/2026-11-15/图片/x.png
 *  历史成品可能还是老的 /vault/素材/... 形态，两种都认。
 */
function matDirOf(url: string): string {
  const p = /^\/private\/\d+\/(.*\/)[^/]*$/.exec(url || "");
  if (p) {
    try {
      return decodeURIComponent(p[1]).replace(/\/$/, "");
    } catch {
      return "";
    }
  }
  const v = /^\/vault\/素材\/(.*\/)[^/]*$/.exec(url || "");
  return v ? v[1].replace(/\/$/, "") : "";
}

function JobRow({ job, onOpen }: { job: Job; onOpen: (j: Job) => void }) {
  const live = job.status === "queued" || job.status === "running";
  const ready = job.status === "done" && !!job.outputUrl;
  const dir = ready ? matDirOf(job.outputUrl) : "";
  return (
    <li className={`tb-job tb-job--${job.status}`}>
      {ready && job.kind === "image" ? (
        <button
          type="button"
          className="tb-job-preview tb-job-preview--btn"
          onClick={() => onOpen(job)}
          title="点开看大图"
        >
          <img src={siteUrl(job.outputUrl)} alt={job.prompt.slice(0, 40)} loading="lazy" />
        </button>
      ) : (
        <div className="tb-job-preview">
          {ready && job.kind === "video" && (
            <video src={siteUrl(job.outputUrl)} preload="metadata" muted />
          )}
          {ready && job.kind === "audio" && (
            <button type="button" className="tb-job-play" onClick={() => onOpen(job)} title="播放">
              ♪
            </button>
          )}
          {!live && !ready && <span className="tb-job-x">×</span>}
          {live && <span className="tb-job-pct">{job.progress}%</span>}
        </div>
      )}
      <div className="tb-job-main">
        <p className="tb-job-prompt">{job.prompt || job.note || "（无提示词）"}</p>
        <div className="tb-job-meta">
          <span className={`tb-pill tb-pill--${job.status}`}>{STATUS_TEXT[job.status] || job.status}</span>
          <span>{job.channel}</span>
          {job.durationSec ? <span>{Number(job.durationSec).toFixed(1)}s</span> : null}
          <span>{String(job.createdAt).replace("T", " ").slice(5, 16)}</span>
        </div>
        {live && (
          <div className="tb-bar"><i style={{ width: `${Math.max(2, job.progress)}%` }} /></div>
        )}
        {job.error && <p className="tb-job-error">{job.error}</p>}
        {job.status === "done" && !job.outputUrl && (
          <p className="muted">成品已从素材页删除</p>
        )}
        {ready && (
          <div className="tb-job-links">
            <button type="button" className="tb-link" onClick={() => onOpen(job)}>
              打开查看
            </button>
            <a className="tb-link" href={siteUrl(job.outputUrl)} target="_blank" rel="noreferrer">
              新窗口
            </a>
            {/* 整页跳转：让素材页按深链初始化 path（SPA 内跳会被切 tab 的重置逻辑覆盖） */}
            {dir && (
              <a
                className="tb-link"
                href={`${appRoot()}app/materials?tab=personal&path=${encodeURIComponent(dir)}`}
              >
                所在目录
              </a>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

export function ToolboxPage() {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [cap, setCap] = useState(0);
  const [modes, setModes] = useState<{ i2v: boolean; i2vReason: string }>({ i2v: false, i2vReason: "" });
  const [loading, setLoading] = useState(true);
  const [fatal, setFatal] = useState("");

  const [kind, setKind] = useState<Kind>("image");
  const [chName, setChName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [line, setLine] = useState("");
  const [sec, setSec] = useState(3);
  const [mode, setMode] = useState<"t2v" | "i2v">("t2v");
  const [vals, setVals] = useState<Record<string, unknown>>({});

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [toast, setToast] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [zoom, setZoom] = useState<Job | null>(null);

  const kindChans = useMemo(() => channels.filter((c) => c.kind === kind), [channels, kind]);
  const active = useMemo(
    () => kindChans.find((c) => c.name === chName) ?? null,
    [kindChans, chName],
  );

  const loadChannels = useCallback(async () => {
    setLoading(true);
    setFatal("");
    try {
      // 字面量 /api/... 不用补前缀：公网 nginx 会把 JS 里的 "/api/ 改写成 "/workbench/api/
      const r = await fetch("/api/toolbox/channels", { credentials: "include" });
      const d = await r.json() as {
        ok?: boolean; error?: string; channels?: Channel[]; maxSec?: number;
        modes?: { i2v: boolean; i2vReason: string };
      };
      if (!r.ok || !d.ok) { setFatal(d.error || "渠道清单读取失败"); return; }
      setChannels(d.channels || []);
      setCap(Number(d.maxSec) || 6);
      if (d.modes) setModes(d.modes);
    } catch {
      setFatal("渠道清单读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadJobs = useCallback(async () => {
    try {
      const r = await fetch("/api/toolbox/jobs", { credentials: "include" });
      const d = await r.json() as { ok?: boolean; items?: Job[] };
      if (d.ok) setJobs(d.items || []);
    } catch {
      /* 轮询失败不打扰用户 */
    }
  }, []);

  useEffect(() => { void loadChannels(); }, [loadChannels]);

  useEffect(() => {
    void loadJobs();
    const t = window.setInterval(() => { void loadJobs(); }, 4000);
    return () => window.clearInterval(t);
  }, [loadJobs]);

  // 切类型时自动落到第一个可用渠道，并把参数重置为该渠道默认值
  useEffect(() => {
    if (loading) return;
    const usable = kindChans.filter((c) => c.available);
    const pick = usable[0] || kindChans[0];
    setChName(pick ? pick.name : "");
    setVals(defaultsOf(pick));
  }, [kindChans, loading]);

  useEffect(() => {
    if (active) setVals(defaultsOf(active));
  }, [active?.name]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(""), 4000);
    return () => window.clearTimeout(t);
  }, [toast]);

  

  const submit = async () => {
    setErr("");
    if (!active) { setErr("请选择渠道"); return; }
    if (!active.available) { setErr(`渠道不可用：${active.reason || "未就绪"}`); return; }
    if (mode === "i2v") { setErr(modes.i2vReason || "图生视频当前不可用"); return; }
    setBusy(true);
    try {
      const r = await fetch("/api/toolbox/jobs", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind,
          channels: [active.name],
          prompt: kind === "audio" ? "" : prompt,
          line: kind === "audio" ? line : "",
          sec: kind === "video" ? sec : 0,
          params: { [active.name]: vals },
        }),
      });
      const d = await r.json() as { ok?: boolean; error?: string; note?: string; warnings?: string[] };
      if (!r.ok || !d.ok) { setErr(d.error || "提交失败"); return; }
      setToast(d.note || "已发布任务到工厂");
      if (d.warnings?.length) setErr(d.warnings.join("；"));
      void loadJobs();
    } catch {
      setErr("提交请求失败");
    } finally {
      setBusy(false);
    }
  };

  const canSubmit = !busy && !!active && active.available && (kind === "audio" ? !!line.trim() : !!prompt.trim());

  return (
    <div className="page tb-page">
      <div className="page-head">
        <div>
          <h1>工具箱</h1>
          <p className="tb-sub">
            单技能手工验证 · 提交即进工厂队列，无需走完整流水线 · 成品统一落到「素材」
          </p>
        </div>
        <Link className="btn btn-ghost" to="/materials">去素材查看</Link>
      </div>

      <div className="tb-kinds" role="tablist" aria-label="技能类型">
        {KINDS.map((k) => (
          <button
            key={k.key}
            type="button"
            role="tab"
            aria-selected={kind === k.key}
            className={`tb-kind ${kind === k.key ? "active" : ""}`}
            onClick={() => setKind(k.key)}
          >
            <b>{k.label}</b>
            <small>{k.sub}</small>
          </button>
        ))}
      </div>

      {fatal && <div className="error-banner">{fatal}</div>}

      {loading ? (
        <p className="muted">正在探测可用渠道…</p>
      ) : (
        <div className="tb-grid">
          <section className="tb-card">
            <h2 className="tb-card-title">参数</h2>

            <div className="tb-chans">
              {kindChans.map((c) => (
                <button
                  key={c.name}
                  type="button"
                  className={`tb-chan ${chName === c.name ? "active" : ""} ${c.available ? "" : "off"}`}
                  onClick={() => setChName(c.name)}
                  title={c.notes}
                >
                  <b>{c.label}</b>
                  <small>{c.available ? c.notes : (c.reason || "不可用")}</small>
                </button>
              ))}
              {kindChans.length === 0 && <p className="muted">该类型暂无渠道</p>}
            </div>

            {kind === "video" && (
              <div className="tb-modes">
                <button
                  type="button"
                  className={`tb-mode ${mode === "t2v" ? "active" : ""}`}
                  onClick={() => setMode("t2v")}
                >
                  文生视频
                </button>
                <button
                  type="button"
                  className={`tb-mode ${mode === "i2v" ? "active" : ""}`}
                  disabled={!modes.i2v}
                  title={modes.i2vReason}
                  onClick={() => { if (modes.i2v) setMode("i2v"); }}
                >
                  图生视频
                </button>
                {!modes.i2v && <span className="tb-mode-note">{modes.i2vReason}</span>}
              </div>
            )}

            {kind === "audio" ? (
              <label className="tb-field tb-field--wide">
                <span>台词</span>
                <textarea
                  rows={5}
                  value={line}
                  placeholder="要念出来的内容，标点会影响停顿…"
                  onChange={(e) => setLine(e.target.value)}
                />
              </label>
            ) : (
              <label className="tb-field tb-field--wide">
                <span>提示词</span>
                <textarea
                  rows={5}
                  value={prompt}
                  placeholder={kind === "image" ? "画面内容、风格、构图、光线…" : "主体动作、镜头运动、场景氛围…"}
                  onChange={(e) => setPrompt(e.target.value)}
                />
              </label>
            )}

            {kind === "video" && (
              <label className="tb-field">
                <span>时长（秒，上限 {cap}s）</span>
                <input
                  type="number"
                  value={sec}
                  min={1}
                  max={cap}
                  step={1}
                  onChange={(e) => setSec(Math.min(cap, Math.max(1, Number(e.target.value) || 1)))}
                />
              </label>
            )}

            <div className="tb-params">
              {(active?.params || []).map((p) => (
                <ParamField
                  key={p.key}
                  spec={p}
                  value={vals[p.key]}
                  onChange={(v) => setVals((prev) => ({ ...prev, [p.key]: v }))}
                />
              ))}
            </div>

            {err && <div className="error-banner">{err}</div>}
            {toast && <div className="tb-toast">{toast}</div>}

            <button
              type="button"
              className="btn tb-submit"
              disabled={!canSubmit}
              onClick={() => void submit()}
            >
              {busy ? "提交中…" : "生成"}
            </button>
          </section>

          <section className="tb-card tb-card--jobs">
            <h2 className="tb-card-title">
              最近任务
              <small>每 4 秒刷新</small>
            </h2>
            {jobs.length === 0 ? (
              <p className="muted">还没有手工任务。</p>
            ) : (
              <ul className="tb-jobs">
                {jobs.map((j) => <JobRow key={j.id} job={j} onOpen={setZoom} />)}
              </ul>
            )}
          </section>
        </div>
      )}

      <MediaViewer
        item={zoom ? {
          url: zoom.outputUrl,
          kind: zoom.kind,
          title: `${zoom.channel} · ${zoom.prompt.slice(0, 50) || zoom.kind}`,
        } : null}
        onClose={() => setZoom(null)}
      />
    </div>
  );
}