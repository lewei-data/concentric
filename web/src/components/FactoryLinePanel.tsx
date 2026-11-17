import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { FactoryLine } from "../factoryLines";
import { siteUrl } from "../lib/siteUrl";

/** 主页面的产品摘要（与 FactoryPage 的 Product 兼容） */
export interface ProductLite {
  itemId: string;
  traceId: string;
  serialNo: string;
  title: string;
  workshopNo: string;
  craftName: string;
  shippedAt: string;
  run: { status: string; stage: string };
}

interface Channel {
  name: string;
  label: string;
  kind: string;
  group?: string;
  endpoint?: string;
  notes: string;
  available: boolean;
  reason: string;
  modes?: string[];
  queue?: number;
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

const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "加工中",
  done: "已完成",
  failed: "失败",
  canceled: "已停止",
};

/** 流水线阶段 → 所属要素车间（用于「关联在制流水线」） */
const STAGE_LINE: Record<string, string> = {
  assessment: "text",
  copy: "text",
  sample: "text",
  sample_reel: "video",
  render: "video",
  sample_review: "video",
  final: "video",
  review: "video",
};

function shortTime(value?: string) {
  return value ? value.replace("T", " ").slice(5, 19) : "-";
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include", cache: "no-store" });
  if (res.status === 401) {
    window.location.assign("/app/login");
    throw new Error("登录已过期");
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

export function FactoryLinePanel({ line, products }: { line: FactoryLine; products: ProductLite[] }) {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    const tasks: Promise<unknown>[] = [];
    if (line.channelKind) {
      tasks.push(
        getJson<{ ok?: boolean; channels?: Channel[] }>("/api/factory/channels")
          .then((d) => setChannels(d.channels ?? [])),
      );
    }
    if (line.jobKind) {
      tasks.push(
        getJson<{ ok?: boolean; items?: Job[] }>("/api/toolbox/jobs?limit=60")
          .then((d) => setJobs(d.items ?? [])),
      );
    }
    if (!tasks.length) {
      setLoading(false);
      return;
    }
    try {
      await Promise.all(tasks);
      setErr("");
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setLoading(false);
    }
  }, [line.channelKind, line.jobKind]);

  useEffect(() => {
    setLoading(true);
    setChannels([]);
    setJobs([]);
    load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, [load]);

  const lineChannels = useMemo(
    () => (line.channelKind ? channels.filter((c) => c.kind === line.channelKind) : []),
    [channels, line.channelKind],
  );
  const lineJobs = useMemo(
    () => (line.jobKind ? jobs.filter((j) => j.kind === line.jobKind) : []),
    [jobs, line.jobKind],
  );
  const activeJobs = lineJobs.filter((j) => j.status === "queued" || j.status === "running").length;
  const doneJobs = lineJobs.filter((j) => j.status === "done").length;
  const availChannels = lineChannels.filter((c) => c.available).length;
  const relatedProducts = useMemo(
    () => products.filter((p) => !p.shippedAt && STAGE_LINE[p.run?.stage || ""] === line.key),
    [products, line.key],
  );

  return (
    <div className="fac-line">
      <div className="fac-line-intro">
        <div className="fac-line-mark">{line.short}</div>
        <div className="fac-line-intro-main">
          <h2>
            {line.label}
            <span className="fac-line-sub">{line.subtitle}</span>
          </h2>
          <p className="fac-line-desc">{line.desc}</p>
        </div>
        <div className="fac-line-stats">
          {line.channelKind && (
            <div>
              <b>
                {availChannels}/{lineChannels.length}
              </b>
              <span>可用渠道</span>
            </div>
          )}
          {line.jobKind && (
            <>
              <div>
                <b>{activeJobs}</b>
                <span>在制任务</span>
              </div>
              <div>
                <b>{doneJobs}</b>
                <span>已完工</span>
              </div>
            </>
          )}
          {relatedProducts.length > 0 && (
            <div>
              <b>{relatedProducts.length}</b>
              <span>在制流水</span>
            </div>
          )}
        </div>
      </div>

      {err && <div className="fac-error">车间数据加载失败：{err}</div>}

      <div className="fac-line-card">
        <div className="fac-line-card-head">
          <h3>标准化流程</h3>
          <span className="muted">{line.steps.length} 道工序 · 进厂 → 出厂</span>
        </div>
        <div className="fac-flow">
          {line.steps.map((s, i) => (
            <div className="fac-flow-item" key={s.name}>
              <div className="fac-flow-num">{i + 1}</div>
              <div className="fac-flow-body">
                <b>{s.name}</b>
                <p>{s.desc}</p>
              </div>
              {i < line.steps.length - 1 && <span className="fac-flow-arrow">→</span>}
            </div>
          ))}
        </div>
      </div>

      {line.channelKind && (
        <div className="fac-line-card">
          <div className="fac-line-card-head">
            <h3>加工渠道</h3>
            <span className="muted">来自渠道注册表 · 可用性实时探测</span>
          </div>
          {loading ? (
            <div className="fac-empty">渠道加载中…</div>
          ) : lineChannels.length === 0 ? (
            <div className="fac-empty">未登记该要素类型的加工渠道</div>
          ) : (
            <div className="fac-ch-grid">
              {lineChannels.map((c) => (
                <div className={`fac-ch ${c.available ? "is-ok" : "is-off"}`} key={c.name}>
                  <div className="fac-ch-head">
                    <b>{c.label}</b>
                    <span className="fac-ch-state">{c.available ? "可用" : "不可用"}</span>
                  </div>
                  <div className="fac-ch-meta">
                    <code>{c.endpoint || "-"}</code>
                    <span>{c.group || "-"}</span>
                    {c.queue != null && <span>队列 {c.queue}</span>}
                  </div>
                  <p className="fac-ch-notes">{c.notes}</p>
                  {!c.available && <p className="fac-ch-reason">{c.reason}</p>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {line.jobKind && (
        <div className="fac-line-card">
          <div className="fac-line-card-head">
            <h3>最近加工任务</h3>
            <span className="muted">全厂「{line.label}」作业 · 每 8 秒刷新</span>
          </div>
          {lineJobs.length === 0 ? (
            <div className="fac-empty">暂无该要素的加工任务，可在工具箱手工提交</div>
          ) : (
            <ul className="fac-job-list">
              {lineJobs.slice(0, 20).map((j) => (
                <li className={`fac-job s-${j.status}`} key={j.id}>
                  <div className="fac-job-preview">
                    {j.status === "done" && j.outputUrl && j.kind === "image" && (
                      <img src={siteUrl(j.outputUrl)} alt="" loading="lazy" />
                    )}
                    {j.status === "done" && j.outputUrl && j.kind === "video" && (
                      <video src={siteUrl(j.outputUrl)} preload="metadata" muted />
                    )}
                    {j.status === "done" && j.outputUrl && j.kind === "audio" && (
                      <span className="fac-job-audio">♪</span>
                    )}
                    {(j.status === "queued" || j.status === "running") && (
                      <span className="fac-job-pct">{j.progress}%</span>
                    )}
                    {!["queued", "running", "done"].includes(j.status) && <span className="fac-job-x">×</span>}
                  </div>
                  <div className="fac-job-main">
                    <p className="fac-job-prompt">{j.prompt || j.note || "（无提示词）"}</p>
                    <div className="fac-job-meta">
                      <span className={`fac-job-pill s-${j.status}`}>{STATUS_LABEL[j.status] || j.status}</span>
                      <span>{j.channel}</span>
                      {j.durationSec ? <span>{Number(j.durationSec).toFixed(1)}s</span> : null}
                      <span>{shortTime(j.createdAt)}</span>
                    </div>
                    {(j.status === "queued" || j.status === "running") && (
                      <div className="fac-job-bar">
                        <i style={{ width: `${Math.max(2, j.progress)}%` }} />
                      </div>
                    )}
                    {j.error && <p className="fac-job-error">{j.error}</p>}
                  </div>
                  {j.status === "done" && j.outputUrl && (
                    <a
                      className="fac-job-open"
                      href={siteUrl(j.outputUrl)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      成品 ↗
                    </a>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {relatedProducts.length > 0 && (
        <div className="fac-line-card">
          <div className="fac-line-card-head">
            <h3>关联在制流水线</h3>
            <span className="muted">当前处在本车间工序的流水线（点全局查看工艺轴）</span>
          </div>
          <div className="fac-rel-list">
            {relatedProducts.map((p) => (
              <div className="fac-rel" key={p.traceId}>
                <b>{p.serialNo ? `${p.serialNo} · ${p.title}` : p.title}</b>
                <span className="muted">
                  {p.craftName} · 当前工位 {p.run?.stage || "-"} · trace {p.traceId}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="fac-line-card">
        <div className="fac-line-card-head">
          <h3>标准出厂物</h3>
          <span className="muted">各车间统一交付形态</span>
        </div>
        <div className="fac-out-list">
          {line.outputs.map((o) => (
            <span className="fac-out" key={o}>
              {o}
            </span>
          ))}
        </div>
        {line.note && <p className="fac-line-note">{line.note}</p>}
      </div>

      {line.entry && (
        <div className="fac-line-actions">
          <Link className="btn btn-ghost fac-btn" to={line.entry.href}>
            {line.entry.label} →
          </Link>
        </div>
      )}
    </div>
  );
}
