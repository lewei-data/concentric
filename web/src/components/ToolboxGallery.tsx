import { useEffect, useMemo, useState } from "react";
import { MediaViewer, type ViewerItem } from "./MediaViewer";

type ToolboxJob = {
  id: string;
  kind: string;
  channel: string;
  status: string;
  outputUrl?: string;
  prompt?: string;
  note?: string;
  error?: string;
  durationSec?: number;
  createdAt?: string;
};

const KIND_LABEL: Record<string, string> = { image: "图片", video: "视频", audio: "语音" };

function dayLabel(day: string): string {
  const now = new Date();
  const p = (x: number) => String(x).padStart(2, "0");
  const fmt = (d: Date) => `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  if (day === fmt(now)) return `今天 · ${day}`;
  if (day === fmt(new Date(now.getTime() - 86400000))) return `昨天 · ${day}`;
  return day;
}

/** 工具箱成品画廊：按日期分组，缩略图 + 摘要，点开看详情。 */
export function ToolboxGallery() {
  const [jobs, setJobs] = useState<ToolboxJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [view, setView] = useState<ViewerItem | null>(null);

  useEffect(() => {
    let alive = true;
    fetch("/api/toolbox/jobs?limit=200", { credentials: "same-origin" })
      .then((r) => r.json())
      .then((d: { ok?: boolean; items?: ToolboxJob[]; error?: string }) => {
        if (!alive) return;
        if (d.ok) setJobs(d.items ?? []);
        else setErr(d.error || "读取失败");
      })
      .catch((e: Error) => { if (alive) setErr(e.message); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);

  const groups = useMemo(() => {
    const m = new Map<string, ToolboxJob[]>();
    for (const j of jobs) {
      const day = (j.createdAt || "").slice(0, 10) || "未知日期";
      const list = m.get(day);
      if (list) list.push(j);
      else m.set(day, [j]);
    }
    return [...m.entries()];
  }, [jobs]);

  if (loading) return <p className="muted">读取中…</p>;
  if (err) return <div className="error-banner">{err}</div>;
  if (!jobs.length) {
    return (
      <div className="tg-empty">
        <b>还没有手工生成的成品</b>
        <p>到「工具箱」提交一条任务，出图出片后会出现在这里，同时也会落进素材目录。</p>
      </div>
    );
  }

  const open = (j: ToolboxJob) => {
    if (j.status !== "done" || !j.outputUrl) return;
    setView({ url: j.outputUrl, kind: j.kind, title: j.prompt || j.note || "" });
  };

  return (
    <>
      <p className="muted tg-hint">
        共 {jobs.length} 条手工任务 · 成品保存在个人空间 <code>conring/users/&lt;手机号&gt;/工具箱</code>，仅本人可见
      </p>
      {groups.map(([day, list]) => (
        <section key={day} className="tg-group">
          <h3 className="tg-day">
            {dayLabel(day)}
            <span className="muted">　{list.length} 条</span>
          </h3>
          <div className="tg-grid">
            {list.map((j) => {
              const done = j.status === "done" && !!j.outputUrl;
              return (
                <div
                  key={j.id}
                  className={`tg-card ${done ? "" : "tg-card--dead"}`}
                  role={done ? "button" : undefined}
                  tabIndex={done ? 0 : undefined}
                  title={done ? "点开查看" : j.error || j.status}
                  onClick={() => open(j)}
                  onKeyDown={(e) => {
                    if (!done) return;
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      open(j);
                    }
                  }}
                >
                  <div className="tg-thumb">
                    {done && j.kind === "image" && (
                      <img src={j.outputUrl} alt={j.prompt?.slice(0, 40) || ""} loading="lazy" />
                    )}
                    {done && j.kind === "video" && (
                      // #t=0.1 让浏览器取首帧当封面，否则缩略图是黑的
                      <video src={`${j.outputUrl}#t=0.1`} preload="metadata" muted />
                    )}
                    {done && j.kind === "audio" && <span className="tg-note">♪</span>}
                    {!done && <span className="tg-x">×</span>}
                    <span className="tg-kind">{KIND_LABEL[j.kind] || j.kind}</span>
                  </div>
                  <div className="tg-body">
                    <p className="tg-prompt">{j.prompt || j.note || "（无提示词）"}</p>
                    <div className="tg-meta">
                      <span>{j.channel}</span>
                      {j.durationSec ? <span>{Number(j.durationSec).toFixed(1)}s</span> : null}
                      <span>{String(j.createdAt || "").replace("T", " ").slice(11, 16)}</span>
                    </div>
                    {j.error && <p className="tg-err">{j.error.slice(0, 90)}</p>}
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      ))}
      <MediaViewer item={view} onClose={() => setView(null)} />
    </>
  );
}