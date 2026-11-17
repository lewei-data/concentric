import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnchoredPanel } from "../components/AnchoredPanel";
import { siteUrl } from "../lib/siteUrl";

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
  /** 当前读取范围：personal=只看自己的库，owner=公司共享库 */
  scope?: { kind: string; label: string };
  error?: string;
};

const CATEGORIES = ["全部", "采集洞察", "热点分析", "视频分析", "蒸馏启发"] as const;
type Category = (typeof CATEGORIES)[number];

const CAT_CLASS: Record<string, string> = {
  采集洞察: "cat-collect",
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
  collectIdeas: "自动采集灵感",
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

type ProduceResult = {
  ok: boolean;
  traceId?: string;
  note?: string;
  error?: string;
  busy?: boolean;
};

async function startProduction(idea: Idea, requirements: string): Promise<ProduceResult> {
  const res = await fetch("/api/inspiration/produce", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ idea, requirements }),
  });
  if (res.status === 401) {
    window.location.assign("/app/login");
    throw new Error("登录已过期");
  }
  return (await res.json()) as ProduceResult;
}

function shortTime(at?: string) {
  if (!at) return "—";
  const m = at.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : at.slice(0, 16);
}

/** embedded=true 时不渲染页头，供「素材」页内嵌为 tab 使用 */
export function InspirationPage({ embedded = false }: { embedded?: boolean } = {}) {
  const [data, setData] = useState<InspirationData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [category, setCategory] = useState<Category>("全部");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [produce, setProduce] = useState<Idea | null>(null);
  const [reqs, setReqs] = useState("");
  const [producing, setProducing] = useState(false);
  const [produceErr, setProduceErr] = useState("");
  const [produceOk, setProduceOk] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 弹窗贴着「开始制作」按钮弹，而不是浮在屏幕中间
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);

  const openProduce = (it: Idea, el?: HTMLElement) => {
    setAnchorEl(el ?? null);
    setProduce(it);
    setReqs("");
    setProduceOk("");
    setProduceErr("");
  };
  const closeProduce = () => {
    if (producing) return;
    setAnchorEl(null);
    setProduce(null);
    setProduceOk("");
    setProduceErr("");
  };
  const confirmProduce = async () => {
    if (!produce || producing) return;
    setProducing(true);
    setProduceErr("");
    try {
      const data = await startProduction(produce, reqs.trim());
      if (!data.ok) {
        setProduceErr(data.error || "发起失败，请稍后再试");
        return;
      }
      setProduceOk(data.traceId || "");
    } catch (e) {
      setProduceErr(e instanceof Error ? e.message : String(e));
    } finally {
      setProducing(false);
    }
  };

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
    { label: "采集洞察", value: counts["采集洞察"] ?? "-", tone: "collect" },
    { label: "来自热点分析", value: counts["热点分析"] ?? "-", tone: "hot" },
    { label: "来自视频分析", value: counts["视频分析"] ?? "-", tone: "video" },
    { label: "蒸馏启发", value: counts["蒸馏启发"] ?? "-", tone: "distill" },
  ];

  return (
    <div className="ins-page">
      {!embedded && (
      <div className="page-head">
        <div>
          <h1>创意灵感</h1>
          <p className="ins-sub">
            CCO 首席创意官视角 · 自动采集 → 热点分析 → 视频拆解 → 选题蒸馏，线索与启发集中呈现（采集洞察优先）
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

      )}

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
        {data?.scope && (
          <span className={`ins-scope ${data.scope.kind === "personal" ? "is-personal" : ""}`}
                title={data.scope.kind === "personal" ? "只读取你自己的库，不含公司共享内容" : "读取公司共享 Obsidian 库"}>
            {data.scope.label}
          </span>
        )}
      </div>

      {loading && !data && <div className="ins-empty">正在读取热点简报、视频拆解与知识库…</div>}
      {!loading && filtered.length === 0 && (
        <div className="ins-empty">
          {data?.scope?.kind === "personal"
            ? "你的个人空间里还没有可聚合的文档。把拆解、笔记类 .md 放进个人 Obsidian 或知识库，重新聚合即可看到。"
            : "当前筛选下没有灵感条目。换个分类或清空搜索试试。"}
        </div>
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
                <button
                  type="button"
                  className="btn btn-produce"
                  onClick={(e) => openProduce(it, e.currentTarget)}
                >
                  开始制作
                </button>
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
                  <a href={siteUrl(it.link)} target="_blank" rel="noreferrer">查看原文 →</a>
                ) : (
                  <span className="muted">要点已内联（该文件不在下载路由内）</span>
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

      {produce && (
        <AnchoredPanel
          anchor={anchorEl}
          onClose={closeProduce}
          className="idea-dialog"
          ariaLabel="开始制作"
          anchorLabel="开始制作"
        >
            {produceOk ? (
              <>
                <h3>已进入制作流水线</h3>
                <p className="idea-dialog-sub">
                  指令已同步 CCO，正在从「制作评估」起跑：CCO 定工艺（发流水号进厂）→ 文案 → 样片 →
                  OpenMontage 出片 → 终审。
                </p>
                <p className="idea-trace">
                  已派单，任务在加工厂排产中（任务名见「作品」列表）
                </p>
                <div className="idea-dialog-actions">
                  <a className="btn btn-ghost" href="/app/factory">查看加工厂 →</a>
                  <button type="button" className="btn btn-ghost" onClick={closeProduce}>
                    关闭
                  </button>
                </div>
              </>
            ) : (
              <>
                <h3>开始制作</h3>
                <p className="idea-dialog-sub">灵感：《{produce.title}》</p>
                <textarea
                  className="input idea-reqs"
                  rows={4}
                  placeholder="输入特定要求（可选）——画面风格、时长、调性、必须出现的元素…&#10;示例：正式宣传片，画面精致唯美，配轻音乐。"
                  value={reqs}
                  onChange={(e) => setReqs(e.target.value)}
                  disabled={producing}
                />
                {produceErr && <div className="idea-dialog-error">{produceErr}</div>}
                <div className="idea-dialog-actions">
                  <button type="button" className="btn btn-ghost" onClick={closeProduce} disabled={producing}>
                    取消
                  </button>
                  <button type="button" className="btn btn-produce" disabled={producing} onClick={() => void confirmProduce()}>
                    {producing ? "发起中…" : "确认开始制作"}
                  </button>
                </div>
              </>
            )}
        </AnchoredPanel>
      )}
    </div>
  );
}
