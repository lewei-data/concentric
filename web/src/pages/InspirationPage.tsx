import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type Idea = {
  id: string;
  category: string;
  subKind: string;
  title: string;
  summary: string;
  detail: string;
  tags: string[];
  source: string;
  link: string;
  at: string;
  score: number | null;
  origin: string;
};

type InspirationData = {
  ok: boolean;
  generatedAt?: string;
  cached?: boolean;
  counts?: Record<string, number>;
  sources?: Record<string, number>;
  items?: Idea[];
  notes?: string[];
  error?: string;
};

const CATEGORIES = ["全部", "热点分析", "视频分析", "蒸馏启发"] as const;
type Category = (typeof CATEGORIES)[number];

const CAT_CLASS: Record<string, string> = {
  热点分析: "cat-hot",
  视频分析: "cat-video",
  蒸馏启发: "cat-distill",
};

const SOURCE_LABELS: Record<string, string> = {
  hotspotBriefs: "热点简报",
  hotspotRows: "热点线索",
  videoDocs: "视频拆解文档",
  knowledgeEntries: "知识库条目",
  meetingConclusions: "评审会结论",
};

const REFRESH_KINDS = new Set([
  "hotspot", "knowledge", "collection", "meeting.start", "meeting.end",
  "config-reload", "report", "pipeline",
]);

async function getInspiration(refresh = false): Promise<InspirationData> {
  const res = await fetch(`/api/inspiration${refresh ? "?refresh=1" : ""}`, {
    credentials: "include",
    cache: "no-store",
  });
  if (res.status === 401) {
    window.location.assign("/app/login");
    throw new Error("登录已过期");
  }
  if (!res.ok) throw new Error(`灵感数据读取失败（HTTP ${res.status}）`);
  return (await res.json()) as InspirationData;
}

function shortTime(at?: string) {
  if (!at) return "—";
  const m = at.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : at.slice(0, 16);
}

export function InspirationPage() {
  const [data, setData] = useState<InspirationData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [category, setCategory] = useState<Category>("全部");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async (force = false) => {
    try {
      const next = await getInspiration(force);
      if (!next.ok) throw new Error(next.error || "灵感数据不可用");
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
    const poll = window.setInterval(() => void load(), 60000);
    const es = new EventSource("/api/events");
    es.onmessage = (ev) => {
      try {
        const envelope = JSON.parse(ev.data) as { kind?: string };
        if (!envelope.kind || !REFRESH_KINDS.has(envelope.kind)) return;
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => void load(true), 900);
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

  const items = data?.items || [];
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items.filter((it) => {
      if (category !== "全部" && it.category !== category) return false;
      if (!q) return true;
      const hay = `${it.title} ${it.summary} ${it.detail} ${(it.tags || []).join(" ")} ${it.source}`.toLowerCase();
      return hay.includes(q);
    });
  }, [items, category, query]);

  const counts = data?.counts || {};
  const stats = [
    { label: "灵感总数", value: counts.total ?? "-", tone: "total" },
    { label: "来自热点分析", value: counts["热点分析"] ?? "-", tone: "hot" },
    { label: "来自视频分析", value: counts["视频分析"] ?? "-", tone: "video" },
    { label: "蒸馏启发", value: counts["蒸馏启发"] ?? "-", tone: "distill" },
  ];

  return (
    <div className="ins-page">
      <div className="page-head">
        <div>
          <h1>创意灵感</h1>
          <p className="ins-sub">
            CCO 首席创意官视角 · 热点分析 → 视频拆解 → 选题蒸馏，三类线索与启发集中呈现
          </p>
        </div>
        <div className="ins-head-actions">
          <span className="ins-updated">
            {data?.generatedAt ? `更新于 ${shortTime(data.generatedAt)}` : loading ? "正在聚合灵感…" : ""}
          </span>
          <button type="button" className="btn btn-ghost" onClick={() => void load(true)} disabled={loading}>
            重新聚合
          </button>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <section className="ins-stats" aria-label="灵感概览">
        {stats.map((s) => (
          <div key={s.label} className={`ins-stat ins-${s.tone}`}>
            <b>{s.value}</b>
            <span>{s.label}</span>
          </div>
        ))}
      </section>

      <div className="ins-toolbar">
        <div className="chip-group" role="tablist" aria-label="灵感分类">
          {CATEGORIES.map((c) => (
            <button
              key={c}
              type="button"
              role="tab"
              aria-selected={category === c}
              className={`chip-btn ${category === c ? "is-active" : ""}`}
              onClick={() => setCategory(c)}
            >
              {c}
              <small>{c === "全部" ? counts.total ?? 0 : counts[c] ?? 0}</small>
            </button>
          ))}
        </div>
        <input
          className="input ins-search"
          placeholder="搜索灵感、标签、来源…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {loading && !data && <div className="ins-empty">正在读取热点简报、视频拆解与知识库…</div>}
      {!loading && filtered.length === 0 && (
        <div className="ins-empty">当前筛选下没有灵感条目。换个分类或清空搜索试试。</div>
      )}

      <section className="ins-grid" aria-label="灵感清单">
        {filtered.map((it) => {
          const expanded = !!open[it.id];
          return (
            <article key={it.id} className="ins-card">
              <div className="ins-card-top">
                <span className={`ins-cat ${CAT_CLASS[it.category] || "cat-hot"}`}>{it.category}</span>
                <span className="ins-kind">{it.subKind}</span>
                {typeof it.score === "number" && (
                  <span className="ins-score">热度 {Math.round(it.score * 100)}%</span>
                )}
              </div>
              <h3>{it.title}</h3>
              <p className="ins-summary">{it.summary || "暂无摘要，展开查看原文要点。"}</p>
              <div className="ins-tags">
                {(it.tags || []).slice(0, 5).map((t, i) => (
                  <span key={`${t}-${i}`} className="ins-tag">{t}</span>
                ))}
              </div>
              <div className="ins-card-foot">
                <span className="ins-source" title={it.source}>{it.source}</span>
                <span>{shortTime(it.at)}</span>
              </div>
              <div className="ins-card-actions">
                {it.detail && (
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => setOpen((prev) => ({ ...prev, [it.id]: !prev[it.id] }))}
                  >
                    {expanded ? "收起要点" : "展开要点"}
                  </button>
                )}
                {it.link ? (
                  <a href={it.link} target="_blank" rel="noreferrer">查看原文 →</a>
                ) : (
                  <span className="muted">仓库文档 · 要点已内联</span>
                )}
              </div>
              {expanded && <pre className="ins-detail">{it.detail}</pre>}
            </article>
          );
        })}
      </section>

      <section className="ins-sources">
        <h2>数据来源与口径</h2>
        <div className="ins-source-grid">
          {Object.entries(data?.sources || {}).map(([key, value]) => (
            <div key={key} className="ins-source-item">
              <b>{value}</b>
              <span>{SOURCE_LABELS[key] || key}</span>
            </div>
          ))}
        </div>
        <ul>
          {(data?.notes || []).map((n, i) => <li key={i}>{n}</li>)}
        </ul>
        <p className="muted">
          页面只读展示；热点/知识库读路径目前仍以内存快照与 Obsidian 文件为准，过程库为影子双写。
        </p>
      </section>
    </div>
  );
}
