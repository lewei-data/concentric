import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useList, useOne } from "@refinedev/core";
import type { Knowledge, KnowledgeCategory } from "../types";
import { WUXING, useWuxing, type WuXing } from "../wuxing";
import { KB_CATEGORIES } from "../knowledgeCategories";
import { WuxingLogo } from "../components/WuxingLogo";
import { DecisionPage } from "./DecisionPage";
import { ReflectionPage } from "./ReflectionPage";
import { GraphPage } from "./GraphPage";
import { AssetLinksPanel } from "../components/AssetLinksPanel";
import { QuestionsPanel } from "../components/QuestionsPanel";

const MarkdownBody = lazy(() => import("../components/MarkdownBody"));

const PAGE_SIZE = 24;

const ELEMENT_TO_WUXING: Record<string, WuXing> = {
  木: "wood", 火: "fire", 土: "earth", 金: "metal", 水: "water",
};
const ELEMENT_TONE: Record<WuXing, number> = {
  wood: 280, fire: 550, earth: 220, metal: 440, water: 330,
};

function wxOf(element?: string): WuXing {
  return ELEMENT_TO_WUXING[element ?? ""] ?? "water";
}

/** 五行点击音：复用同一个 AudioContext，避免每次点击泄漏一个音频上下文 */
let soundCtx: AudioContext | null = null;
function playElementSound(element?: string) {
  try {
    const Ctor = window.AudioContext
      || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return;
    if (!soundCtx) soundCtx = new Ctor();
    const ctx = soundCtx;
    if (ctx.state === "suspended") void ctx.resume();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = ELEMENT_TONE[wxOf(element)];
    gain.gain.setValueAtTime(0.06, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.16);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.16);
  } catch { /* 音频不可用时忽略 */ }
}

type TagStat = { key: string; label: string; count?: number };

type CategoryWithStats = {
  key: string;
  label: string;
  element: string;
  icon: string;
  slogan: string;
  description: string;
  color: string;
  tags: TagStat[];
  /** 后端按真实条目聚合出的标签（优先于静态定义，否则筛选永远命中不了） */
  tagStats?: TagStat[];
  count?: number;
};

/** 前端兜底分类定义（五行体系），后端 /api/knowledge/categories 优先 */
const FALLBACK_CATEGORIES: CategoryWithStats[] = KB_CATEGORIES.map((c) => ({
  ...c,
  tags: c.tags.map((t) => ({ ...t })),
}));

function isPdfContent(item?: Knowledge): boolean {
  if (!item) return false;
  const body = item.body ?? "";
  return body.startsWith("%PDF") || body.startsWith("data:application/pdf")
    || !!item.tags?.some((t) => t.toLowerCase().includes("pdf"));
}

/** PDF 内嵌地址：只有合法 base64 / data URI 才生成，中文正文不再触发 btoa 崩溃 */
function pdfSrc(body?: string): string {
  if (!body) return "";
  if (body.startsWith("data:application/pdf")) return body;
  if (!/^[A-Za-z0-9+/=\s]+$/.test(body)) return "";
  try {
    return `data:application/pdf;base64,${btoa(body)}`;
  } catch {
    return "";
  }
}

/** 早期归档把「落笔说明」写进了时间字段，非 ISO 的不展示，避免出现半截怪文本 */
function fmtDate(value?: string): string {
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(value ?? "");
  return m ? m[1] : "";
}

function KbState({
  kind, text, onRetry,
}: {
  kind: "loading" | "error" | "empty";
  text: string;
  onRetry?: () => void;
}) {
  const { element } = useWuxing();
  return (
    <div className={`kb-state kb-state--${kind}`}>
      <span className="kb-state-logo"><WuxingLogo element={element} size={46} /></span>
      <b>{kind === "loading" ? "正在取数" : kind === "error" ? "数据未能载入" : "这里还没有条目"}</b>
      <span className="kb-state-text">{text}</span>
      {kind === "error" && onRetry && (
        <button type="button" className="btn btn-ghost" onClick={onRetry}>重新加载</button>
      )}
    </div>
  );
}

export function KnowledgePage() {
  const wx = useWuxing();
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  // "questions" = 待解决（问题池），不再是分类但占用第 6 个 tab 位
  const [activeCat, setActiveCat] = useState<KnowledgeCategory | "all" | "questions">("questions");
  const [openQuestions, setOpenQuestions] = useState(0);
  const [activeTag, setActiveTag] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const detailRef = useRef<HTMLDivElement>(null);

  const catsRes = useList<CategoryWithStats>({
    resource: "knowledge/categories",
    pagination: { mode: "off" },
    queryOptions: { refetchInterval: 60000, retry: 1 },
  });

  const categories = useMemo<CategoryWithStats[]>(() => {
    const items = catsRes.data?.data;
    return items?.length ? items : FALLBACK_CATEGORIES;
  }, [catsRes.data]);

  const catCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const c of categories) m[c.key] = Number(c.count ?? 0);
    return m;
  }, [categories]);

  const activeCategory = activeCat === "all" || activeCat === "questions"
    ? null
    : categories.find((c) => c.key === activeCat) ?? null;
  const activeElement = wxOf(activeCategory?.element);
  const barElement = activeCategory ? activeElement : wx.element;

  const currentTags = useMemo<TagStat[]>(() => {
    const pick = (c: CategoryWithStats): TagStat[] =>
      c.tagStats?.length ? c.tagStats : c.tags ?? [];
    if (activeCat === "all") {
      const merged = new Map<string, TagStat>();
      for (const c of categories) {
        for (const t of pick(c)) {
          const prev = merged.get(t.key);
          merged.set(t.key, {
            key: t.key, label: t.label,
            count: (prev?.count ?? 0) + (t.count ?? 0),
          });
        }
      }
      return Array.from(merged.values())
        .sort((a, b) => (b.count ?? 0) - (a.count ?? 0)).slice(0, 16);
    }
    return activeCategory ? pick(activeCategory).slice(0, 18) : [];
  }, [activeCat, activeCategory, categories]);

  const filters = useMemo(() => {
    const f: { field: string; operator: "eq"; value: unknown }[] = [];
    if (q) f.push({ field: "q", operator: "eq", value: q });
    if (activeCat !== "all" && activeCat !== "questions") f.push({ field: "category", operator: "eq", value: activeCat });
    if (activeTag) f.push({ field: "tag", operator: "eq", value: activeTag });
    return f;
  }, [q, activeCat, activeTag]);

  const list = useList<Knowledge>({
    resource: "knowledge",
    pagination: { current: page, pageSize: PAGE_SIZE },
    filters,
    queryOptions: { refetchInterval: 20000, retry: 1 },
  });

  const detail = useOne<Knowledge>({
    resource: "knowledge",
    id: selectedId ?? "",
    queryOptions: { enabled: selectedId !== null, retry: 1 },
  });

  // 后端历史数据曾混入无 id / 无标题的阶段事件行：前端再做一次兜底去重，
  // 避免 React 重复 key 与「点击空白卡片 → /api/knowledge/null → 404」。
  const items = useMemo(() => {
    const seen = new Set<string>();
    return (list.data?.data ?? []).filter((it) => {
      const id = String(it?.id ?? "").trim();
      const title = String(it?.title ?? "").trim();
      if (!id || id === "null" || id === "undefined" || !title || seen.has(id)) return false;
      seen.add(id);
      return true;
    });
  }, [list.data]);

  const total = list.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  useEffect(() => {
    if (selectedId) detailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [selectedId]);

  const handleSearch = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPage(1);
    setQ(input.trim());
    setSelectedId(null);
  };

  const switchCategory = useCallback((cat: KnowledgeCategory | "all" | "questions", element?: string) => {
    setActiveCat(cat);
    setActiveTag("");
    setPage(1);
    setSelectedId(null);
    if (element) playElementSound(element);
  }, []);

  const switchTag = (tag: string) => {
    setActiveTag((prev) => (prev === tag ? "" : tag));
    setPage(1);
    setSelectedId(null);
  };

  const openDetail = useCallback((id: string) => setSelectedId(id), []);
  const closeDetail = useCallback(() => setSelectedId(null), []);

  const selected = detail.data?.data;
  const detailElement = wxOf(categories.find((c) => c.key === selected?.category)?.element);
  const detailTheme = WUXING[detailElement];
  const selectedCat = categories.find((c) => c.key === selected?.category);
  const pdfUrl = isPdfContent(selected) ? pdfSrc(selected?.body) : "";
  const detailText = selected?.body || selected?.summary || "";

  return (
    <section className="page kb-page">
      <div className="page-head kb-page-head">
        <div className="kb-heading">
          <h1>知识库</h1>
          <span className="kb-head-qi" title={wx.reading.reason}>
            <WuxingLogo element={wx.reading.element} size={20} />
            今日{wx.reading.ganzhi}日 · {WUXING[wx.reading.element].fullName}宜用 · {WUXING[wx.reading.element].motto}
          </span>
        </div>
        <form className="search" onSubmit={handleSearch}>
          <input
            className="input"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="按标题 / 摘要 / 正文搜索"
          />
          <button type="submit" className="btn">搜索</button>
        </form>
      </div>

      {catsRes.isError && (
        <p className="kb-hint">分类统计暂不可用，已回退到内置五行分类；列表数据不受影响。</p>
      )}

      {/* ── 五行分类 ── */}
      <div className="kb-tabs" role="tablist" aria-label="知识库五行分类">
        {categories.map((c) => {
          const el = wxOf(c.element);
          const theme = WUXING[el];
          const isActive = activeCat === c.key;
          return (
            <button
              key={c.key}
              type="button" role="tab" aria-selected={isActive}
              className={`kb-tab ${isActive ? "active" : ""} ${el === wx.reading.element ? "is-today" : ""}`}
              style={{ "--kb-el-1": theme.color, "--kb-el-2": theme.color2 } as React.CSSProperties}
              title={`${c.label} · ${WUXING[el].classic}`}
              onClick={() => switchCategory(c.key as KnowledgeCategory, c.element)}
            >
              <span className="kb-tab-logo"><WuxingLogo element={el} size={28} /></span>
              <span className="kb-tab-text">
                <span className="kb-tab-label">{c.label}</span>
                <span className="kb-tab-slogan">{c.slogan}</span>
              </span>
              <span className="kb-tab-count">{catCounts[c.key] ?? 0}</span>
              {el === wx.reading.element && <span className="kb-tab-today">当令</span>}
            </button>
          );
        })}
        {/* 待解决：提出但还没有知识条目认领的问题 */}
        <button
          type="button" role="tab" aria-selected={activeCat === "questions"}
          className={`kb-tab kb-tab--q ${activeCat === "questions" ? "active" : ""}`}
          title="待解决 · 已提出但还没有知识来认领的问题"
          onClick={() => switchCategory("questions")}
        >
          <span className="kb-tab-logo kb-tab-logo--q" aria-hidden="true">?</span>
          <span className="kb-tab-text">
            <span className="kb-tab-label">待解决</span>
            <span className="kb-tab-slogan">已知之不足</span>
          </span>
          <span className="kb-tab-count">{openQuestions}</span>
        </button>
      </div>

      {/* ── 分类说明 + 真实标签筛选 ── */}
      {(activeCategory || currentTags.length > 0) && (
        <div
          className="kb-sub-bar"
          style={{
            "--kb-el-1": WUXING[barElement].color,
            "--kb-el-2": WUXING[barElement].color2,
          } as React.CSSProperties}
        >
          <div className="kb-cat-header">
            <span className="kb-cat-logo"><WuxingLogo element={barElement} size={48} /></span>
            <div className="kb-cat-info">
              {activeCategory ? (
                <>
                  <div className="kb-cat-title">
                    <b>{activeCategory.label}</b>
                    <span>{WUXING[activeElement].classic} · {WUXING[activeElement].motto}</span>
                  </div>
                  <p className="kb-cat-desc">{activeCategory.description}</p>
                  <p className="kb-cat-slogan">{activeCategory.slogan}</p>
                </>
              ) : (
                <>
                  <div className="kb-cat-title">
                    <b>五行周流 · 全部归档</b>
                    <span>{WUXING[wx.element].classic} · 共 {total} 条</span>
                  </div>
                  <p className="kb-cat-desc">
                    今日以{WUXING[wx.reading.element].fullName}为用；下列标签取自真实归档条目，可与分类、关键词叠加筛选。
                  </p>
                </>
              )}
            </div>
          </div>
          {currentTags.length > 0 && (
            <div className="kb-tag-row">
              <span className="kb-tag-lead">标签</span>
              {currentTags.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  className={`kb-tag ${activeTag === t.key ? "active" : ""}`}
                  onClick={() => switchTag(t.key)}
                >
                  {t.label}{t.count ? <i>{t.count}</i> : null}
                </button>
              ))}
              {activeTag && (
                <button type="button" className="kb-tag-clear" onClick={() => switchTag(activeTag)}>
                  清除筛选 ×
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── 内联详情 ── */}
      {selectedId !== null ? (
        <div
          className="kb-detail-view"
          ref={detailRef}
          style={{ "--kb-el-1": detailTheme.color, "--kb-el-2": detailTheme.color2 } as React.CSSProperties}
        >
          <div className="kb-detail-toolbar">
            <button type="button" className="btn btn-ghost kb-detail-back" onClick={closeDetail}>
              ← 返回列表
            </button>
            <div className="kb-detail-meta">
              <span className="kb-detail-logo"><WuxingLogo element={detailElement} size={24} /></span>
              <span className="kb-detail-cat-label">{selectedCat?.label ?? "未分类"}</span>
              {selected?.projectId && <span className="kb-chip">{selected.projectId}</span>}
              {fmtDate(selected?.at) && <span className="kb-chip">{fmtDate(selected?.at)}</span>}
            </div>
          </div>
          <div className="kb-detail-content">
            {detail.isLoading ? (
              <KbState kind="loading" text="正在读取这条知识的正文…" />
            ) : detail.isError ? (
              <KbState kind="error" text="详情接口未返回，可重试或返回列表" onRetry={() => void detail.refetch()} />
            ) : !selected ? (
              <KbState kind="empty" text="这条记录可能已被清理" />
            ) : (
              <>
                <h1 className="kb-detail-title">{selected.title}</h1>
                {selected.summary && <p className="kb-detail-summary">{selected.summary}</p>}
                {selected.tags?.length ? (
                  <div className="kb-detail-tags">
                    {selected.tags.map((t) => <span key={t} className="kb-card-tag">{t}</span>)}
                  </div>
                ) : null}
                <div className="kb-detail-body">
                  {pdfUrl ? (
                    <iframe className="kb-pdf-viewer" src={pdfUrl} title={selected.title} />
                  ) : isPdfContent(selected) ? (
                    <KbState kind="empty" text="PDF 正文不完整，无法内嵌预览" />
                  ) : detailText ? (
                    <Suspense fallback={<span className="muted">正文渲染中…</span>}>
                      <MarkdownBody text={detailText} />
                    </Suspense>
                  ) : (
                    <KbState kind="empty" text="这条归档没有正文，仅保留标题与标签" />
                  )}
                </div>
                <AssetLinksPanel kbId={String(selected.id)} />
              </>
            )}
          </div>
        </div>
      ) : activeCat === "questions" ? (
        <QuestionsPanel onCountChange={setOpenQuestions} />
      ) : activeCat === "graph" ? (
        <GraphPage embedded />
      ) : activeCat === "decision" ? (
        <DecisionPage embedded />
      ) : activeCat === "reflection" ? (
        <ReflectionPage embedded />
      ) : (
        /* ── 卡片网格 ── */
        <div className="kb-grid-wrap">
          {list.isLoading ? (
            <KbState kind="loading" text="正在从工作区汇总知识条目…" />
          ) : list.isError ? (
            <KbState kind="error" text="列表接口未返回，可能是会话过期或后端暂不可用"
                     onRetry={() => void list.refetch()} />
          ) : items.length === 0 ? (
            <KbState kind="empty" text={q || activeTag
              ? "当前筛选下没有条目，试试清空搜索或标签"
              : "当前分类还没有归档知识"} />
          ) : (
            <>
              <div className="kb-grid">
                {items.map((item) => {
                  const cat = categories.find((c) => c.key === item.category);
                  const el = wxOf(cat?.element);
                  const theme = WUXING[el];
                  return (
                    <article
                      key={String(item.id)}
                      className="kb-card"
                      role="button"
                      tabIndex={0}
                      style={{ "--kb-el-1": theme.color, "--kb-el-2": theme.color2 } as React.CSSProperties}
                      onClick={() => openDetail(String(item.id))}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          openDetail(String(item.id));
                        }
                      }}
                    >
                      <div className="kb-card-head">
                        <span className="kb-card-logo"><WuxingLogo element={el} size={24} /></span>
                        <span className="kb-card-cat">{cat?.label ?? "未分类"}</span>
                        {fmtDate(item.at) && <time className="kb-card-time">{fmtDate(item.at)}</time>}
                      </div>
                      <h3 className="kb-card-title">{item.title}</h3>
                      <p className="kb-card-summary">{item.summary || "（暂无摘要）"}</p>
                      <div className="kb-card-foot">
                        {item.tags?.length ? (
                          <div className="kb-card-tags">
                            {item.tags.slice(0, 3).map((t: string) => (
                              <span key={t} className="kb-card-tag">{t}</span>
                            ))}
                          </div>
                        ) : <span />}
                        <span className="kb-card-more">查看正文 →</span>
                      </div>
                    </article>
                  );
                })}
              </div>
              {totalPages > 1 && (
                <div className="pager">
                  <button type="button" className="btn btn-ghost" disabled={page <= 1}
                    onClick={() => setPage((v) => Math.max(1, v - 1))}>上一页</button>
                  <span className="muted">第 {page} / {totalPages} 页 · 共 {total} 条</span>
                  <button type="button" className="btn btn-ghost" disabled={page >= totalPages}
                    onClick={() => setPage((v) => Math.min(totalPages, v + 1))}>下一页</button>
                </div>
              )}
            </>
          )}
          {list.isFetching && !list.isLoading && <span className="kb-sync">同步中…</span>}
        </div>
      )}
    </section>
  );
}
