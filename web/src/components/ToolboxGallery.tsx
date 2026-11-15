import { useEffect, useMemo, useState } from "react";
import { MediaViewer, type ViewerItem } from "./MediaViewer";
import { siteUrl } from "../lib/siteUrl";

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
  // 缩略图 404 说明文件已被删掉：立刻按“成品已删除”显示，不必等后端对账
  const [broken, setBroken] = useState<Record<string, true>>({});
  // 和素材浏览共用同一个偏好键：在哪儿切过，回来还是那个看法
  const [mode, setMode] = useState<"list" | "grid">(() => {
    try {
      return window.localStorage.getItem("mat.view") === "list" ? "list" : "grid";
    } catch {
      return "grid";
    }
  });

  const setModePersist = (v: "list" | "grid") => {
    setMode(v);
    try {
      window.localStorage.setItem("mat.view", v);
    } catch {
      // 隐私模式下 localStorage 不可写，只影响记住偏好
    }
  };

  useEffect(() => {
    let alive = true;
    const load = () => {
      // 字面量 /api/... 交给网关改写，别在这里拼前缀（会变成 /workbench/workbench/api）
      fetch("/api/toolbox/jobs?limit=200", { credentials: "same-origin" })
        .then((r) => r.json())
        .then((d: { ok?: boolean; items?: ToolboxJob[]; error?: string }) => {
          if (!alive) return;
          if (d.ok) {
            setJobs(d.items ?? []);
            // 产物可能被重新生成，失效标记跟着新数据一起作废
            setBroken({});
            setErr("");
          } else setErr(d.error || "读取失败");
        })
        .catch((e: Error) => { if (alive) setErr(e.message); })
        .finally(() => { if (alive) setLoading(false); });
    };
    load();
    // 用户多半是去文件管理器删了成品再切回浏览器：一回来就重新对账，别等手动刷新
    const onWake = () => {
      if (document.visibilityState === "visible") load();
    };
    window.addEventListener("focus", onWake);
    document.addEventListener("visibilitychange", onWake);
    return () => {
      alive = false;
      window.removeEventListener("focus", onWake);
      document.removeEventListener("visibilitychange", onWake);
    };
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
    if (j.status !== "done" || !j.outputUrl || broken[j.id]) return;
    // 传裸路径：MediaViewer 自己补服务根前缀，这里再拼一次会变成 /workbench/workbench/...
    setView({ url: j.outputUrl, kind: j.kind, title: j.prompt || j.note || "" });
  };

  return (
    <>
      <div className="mat-counts">
        <span className="muted tg-hint">
          共 {jobs.length} 条手工任务 · 成品保存在个人空间 <code>conring/users/&lt;手机号&gt;/工具箱</code>，仅本人可见
        </span>
        <div className="mat-views" role="group" aria-label="查看方式">
          <button
            type="button"
            className={mode === "list" ? "active" : ""}
            onClick={() => setModePersist("list")}
            title="一行一条，文案看得全"
          >
            列表
          </button>
          <button
            type="button"
            className={mode === "grid" ? "active" : ""}
            onClick={() => setModePersist("grid")}
            title="方块缩略图，一屏看很多"
          >
            缩略图
          </button>
        </div>
      </div>
      {groups.map(([day, list]) => (
        <section key={day} className="tg-group">
          <h3 className="tg-day">
            {dayLabel(day)}
            <span className="muted">　{list.length} 条</span>
          </h3>
          {mode === "grid" ? (
            <div className="tg-grid">
            {list.map((j) => {
              const done = j.status === "done" && !!j.outputUrl && !broken[j.id];
              return (
                <div
                  key={j.id}
                  className={`tg-card ${done ? "" : "tg-card--dead"}`}
                  role={done ? "button" : undefined}
                  tabIndex={done ? 0 : undefined}
                  title={done ? "点开查看"
                    : broken[j.id] ? "成品文件已不在服务器上（可能已被删除）"
                      : j.error || (j.status === "done" ? "成品已删除" : j.status)}
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
                    {done && j.outputUrl && j.kind === "image" && (
                      <img
                        src={siteUrl(j.outputUrl)}
                        alt={j.prompt?.slice(0, 40) || ""}
                        loading="lazy"
                        onError={() => setBroken((b) => (b[j.id] ? b : { ...b, [j.id]: true }))}
                      />
                    )}
                    {done && j.outputUrl && j.kind === "video" && (
                      // #t=0.1 让浏览器取首帧当封面，否则缩略图是黑的
                      <video
                        src={`${siteUrl(j.outputUrl)}#t=0.1`}
                        preload="metadata"
                        muted
                        onError={() => setBroken((b) => (b[j.id] ? b : { ...b, [j.id]: true }))}
                      />
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
          ) : (
            <ul className="mat-list">
              {list.map((j) => {
                const done = j.status === "done" && !!j.outputUrl && !broken[j.id];
                return (
                  <li key={j.id}>
                    <button
                      type="button"
                      className="mat-entry"
                      disabled={!done}
                      onClick={() => open(j)}
                      title={done ? "点开查看"
                        : broken[j.id] ? "成品文件已不在服务器上（可能已被删除）"
                      : j.error || (j.status === "done" ? "成品已删除" : j.status)}
                    >
                      <span className="mat-icon" aria-hidden="true">{done ? "·" : "×"}</span>
                      <span className="mat-name">{j.prompt || j.note || "（无提示词）"}</span>
                      <span className="mat-file-meta">
                        {KIND_LABEL[j.kind] || j.kind}　{j.channel}　
                        {String(j.createdAt || "").replace("T", " ").slice(11, 16)}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      ))}
      <MediaViewer item={view} onClose={() => setView(null)} />
    </>
  );
}