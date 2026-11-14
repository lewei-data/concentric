import { lazy, Suspense, useState, useMemo, useCallback } from "react";
import { useList, useOne } from "@refinedev/core";
import type { Knowledge, KnowledgeCategory, CategoryDef } from "../types";

const MarkdownBody = lazy(() => import("../components/MarkdownBody"));

const PAGE_SIZE = 24;

/** 五行元素视觉配置：渐变背景、图标、动画类名 */
const ELEMENT_VISUALS: Record<string, {
  gradient: string;
  icon: string;
  animClass: string;
  sound: number; // Hz for click tone
}> = {
  "土": { gradient: "linear-gradient(135deg, #fef3c7 0%, #fde68a 50%, #d97706 100%)", icon: "⛰️", animClass: "kb-anim-earth", sound: 220 },
  "水": { gradient: "linear-gradient(135deg, #dbeafe 0%, #93c5fd 50%, #1d4ed8 100%)", icon: "🌊", animClass: "kb-anim-water", sound: 330 },
  "金": { gradient: "linear-gradient(135deg, #fef9c3 0%, #fde047 50%, #ca8a04 100%)", icon: "⚔️", animClass: "kb-anim-metal", sound: 440 },
  "火": { gradient: "linear-gradient(135deg, #fee2e2 0%, #fca5a5 50%, #dc2626 100%)", icon: "🔥", animClass: "kb-anim-fire", sound: 550 },
  "木": { gradient: "linear-gradient(135deg, #d1fae5 0%, #6ee7b7 50%, #059669 100%)", icon: "🌿", animClass: "kb-anim-wood", sound: 280 },
};

/** 五行点击音效（Web Audio API，无需外部文件） */
function playElementSound(hz: number) {
  try {
    const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = hz;
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.15);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.15);
  } catch { /* ignore */ }
}

/** 前端兜底分类定义（五行体系），后端 /api/knowledge/categories 优先 */
const FALLBACK_CATEGORIES: CategoryDef[] = [
  { key: "analysis", label: "分析调研", element: "土", icon: "⛰️",
    slogan: "厚基、存真、稳态",
    description: "做厚事实底座，保障素材可信度与完整性；作为全体系的「土壤」，保持相对稳定",
    color: "#b45309",
    tags: [
      { key: "copywriting", label: "文案分析" }, { key: "short-video", label: "短视频分析" },
      { key: "market", label: "市场调研" }, { key: "competitor", label: "竞品分析" },
      { key: "trend", label: "趋势洞察" },
    ]},
  { key: "graph", label: "知识图谱", element: "水", icon: "🌊",
    slogan: "通联、溯源、无滞",
    description: "核心价值在「连通」而非「堆节点」，建设实体间的关联链路与因果传导路径",
    color: "#1e40af",
    tags: [
      { key: "data-source", label: "数据源关联" }, { key: "concept", label: "概念图谱" },
      { key: "workflow", label: "流程图谱" }, { key: "domain", label: "领域模型" },
    ]},
  { key: "skill", label: "技能", element: "金", icon: "⚔️",
    slogan: "精专、规整、耐用",
    description: "像铸造器物一样打磨每个 Skill，边界清晰、输入输出明确、异常处理完备",
    color: "#a16207",
    tags: [
      { key: "content", label: "内容创作" }, { key: "data", label: "数据分析" },
      { key: "design", label: "设计" }, { key: "ops", label: "运维" },
      { key: "management", label: "管理" }, { key: "research", label: "调研" },
    ]},
  { key: "decision", label: "决策模型", element: "火", icon: "🔥",
    slogan: "明断、守序、不妄",
    description: "清晰的判断逻辑与优先级秩序，规则明确、边界清晰；顶层范式不轻易变动",
    color: "#dc2626",
    tags: [
      { key: "domain-model", label: "领域模型" }, { key: "strategy", label: "战略决策" },
      { key: "risk", label: "风险评估" }, { key: "principle", label: "原则框架" },
      { key: "heuristic", label: "经验法则" },
    ]},
  { key: "reflection", label: "反馈迭代", element: "木", icon: "🌿",
    slogan: "循序、日新、有度",
    description: "遵循生长规律，小步持续优化；控制迭代节奏与灰度范围，避免拔苗助长",
    color: "#059669",
    tags: [
      { key: "retrospective", label: "复盘总结" }, { key: "optimization", label: "优化方案" },
      { key: "lesson", label: "经验教训" }, { key: "feedback", label: "用户反馈" },
      { key: "iteration", label: "迭代记录" },
    ]},
];

const CATEGORY_COLORS: Record<KnowledgeCategory, string> = {
  analysis: "#b45309",   // 土・赭
  graph: "#1e40af",      // 水・靛
  skill: "#a16207",      // 金・赤金
  decision: "#dc2626",   // 火・朱
  reflection: "#059669", // 木・翠
};

/** 判断是否为 PDF（根据 body 内容或 tags） */
function isPdfContent(item: Knowledge | undefined): boolean {
  if (!item) return false;
  const body = item.body ?? "";
  if (body.startsWith("%PDF") || body.startsWith("data:application/pdf")) return true;
  if (item.tags?.some(t => t.toLowerCase().includes("pdf"))) return true;
  return false;
}

export function KnowledgePage() {
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [activeCat, setActiveCat] = useState<KnowledgeCategory | "all">("all");
  const [activeTag, setActiveTag] = useState<string>("");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // 拉取分类定义（含各分类条目数）
  const catsRes = useList<{ key: string; label: string; icon: string; description: string;
    tags: { key: string; label: string }[]; count: number }>({
    resource: "knowledge/categories",
    pagination: { mode: "off" },
    queryOptions: { refetchInterval: 30000 },
  });

  const categories: CategoryDef[] = useMemo(() => {
    const items = catsRes.data?.data as unknown as CategoryDef[] | undefined;
    return items?.length ? items : FALLBACK_CATEGORIES;
  }, [catsRes.data]);

  const catCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const c of categories) m[c.key] = (c as any).count ?? 0;
    return m;
  }, [categories]);

  // 当前分类下的可选标签
  const currentTags = useMemo(() => {
    if (activeCat === "all") {
      const seen = new Map<string, string>();
      for (const c of categories) for (const t of c.tags) seen.set(t.key, t.label);
      return Array.from(seen, ([key, label]) => ({ key, label }));
    }
    return categories.find((c) => c.key === activeCat)?.tags ?? [];
  }, [activeCat, categories]);

  const filters: { field: string; operator: "eq"; value: unknown }[] = [];
  if (q) filters.push({ field: "q", operator: "eq" as const, value: q });
  if (activeCat !== "all") filters.push({ field: "category", operator: "eq" as const, value: activeCat });
  if (activeTag) filters.push({ field: "tag", operator: "eq" as const, value: activeTag });

  const list = useList<Knowledge>({
    resource: "knowledge",
    pagination: { current: page, pageSize: PAGE_SIZE },
    filters,
    queryOptions: { refetchInterval: 15000 },
  });

  const detail = useOne<Knowledge>({
    resource: "knowledge",
    id: selectedId ?? "",
    queryOptions: { enabled: selectedId !== null },
  });

  const items = list.data?.data ?? [];
  const total = list.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const handleSearch = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPage(1);
    setQ(input.trim());
  };

  const switchCategory = useCallback((cat: KnowledgeCategory | "all", element?: string) => {
    setActiveCat(cat);
    setActiveTag("");
    setPage(1);
    // 五行音效
    if (element && ELEMENT_VISUALS[element]) {
      playElementSound(ELEMENT_VISUALS[element].sound);
    }
  }, []);

  const switchTag = (tag: string) => {
    setActiveTag((prev) => (prev === tag ? "" : tag));
    setPage(1);
  };

  const openDetail = useCallback((id: string) => {
    setSelectedId(id);
  }, []);

  const closeDetail = useCallback(() => {
    setSelectedId(null);
  }, []);

  const selected = detail.data?.data;
  const isPdf = isPdfContent(selected);

  return (
    <section className="page">
      <div className="page-head">
        <h1>知识库</h1>
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

      {/* ── 分类 Tab（五行体系・美化版） ── */}
      <div className="kb-tabs">
        <button
          className={`kb-tab ${activeCat === "all" ? "active" : ""}`}
          onClick={() => switchCategory("all")}
        >
          <span className="kb-tab-icon">☯</span>
          <span className="kb-tab-label">全部</span>
          <span className="kb-tab-count">{total}</span>
        </button>
        {categories.map((c) => {
          const visual = ELEMENT_VISUALS[c.element];
          const isActive = activeCat === c.key;
          return (
            <button
              key={c.key}
              className={`kb-tab kb-tab--${c.key} ${isActive ? "active" : ""}`}
              style={isActive && visual
                ? { background: visual.gradient, borderColor: "transparent", color: "#fff" }
                : {}}
              onClick={() => switchCategory(c.key as KnowledgeCategory, c.element)}
            >
              <span className={`kb-tab-icon ${isActive ? visual?.animClass : ""}`}>
                {c.icon || c.element}
              </span>
              <span className="kb-tab-text">
                <span className="kb-tab-label">{c.label}</span>
                <span className="kb-tab-slogan">{c.slogan}</span>
              </span>
              <span className="kb-tab-count">{catCounts[c.key] ?? 0}</span>
            </button>
          );
        })}
      </div>

      {/* ── 分类描述 + 标签筛选 ── */}
      {activeCat !== "all" && (
        <div className="kb-sub-bar">
          <div className="kb-cat-header">
            <span className="kb-cat-element" style={{
              color: categories.find((c) => c.key === activeCat)?.color
            }}>
              {categories.find((c) => c.key === activeCat)?.element}
            </span>
            <div className="kb-cat-info">
              <p className="kb-cat-desc">
                {categories.find((c) => c.key === activeCat)?.description}
              </p>
              <p className="kb-cat-slogan">
                {categories.find((c) => c.key === activeCat)?.slogan}
              </p>
            </div>
          </div>
          <div className="kb-tag-row">
            {currentTags.map((t) => (
              <button
                key={t.key}
                className={`kb-tag ${activeTag === t.key ? "active" : ""}`}
                style={activeTag === t.key
                  ? { background: CATEGORY_COLORS[activeCat as KnowledgeCategory], color: "#fff", borderColor: "transparent" }
                  : {}}
                onClick={() => switchTag(t.key)}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ── 内联详情视图（替代弹窗） ── */}
      {selectedId !== null ? (
        <div className="kb-detail-view">
          <div className="kb-detail-toolbar">
            <button className="btn btn-ghost kb-detail-back" onClick={closeDetail}>
              ← 返回列表
            </button>
            <div className="kb-detail-meta">
              {selected?.category && (() => {
                const cat = categories.find((c) => c.key === selected.category);
                const visual = cat ? ELEMENT_VISUALS[cat.element] : null;
                return (
                  <>
                    <span className="kb-detail-element" style={{ color: cat?.color }}>
                      {visual?.icon ?? cat?.element ?? "⊕"}
                    </span>
                    <span className="kb-detail-cat-label">{cat?.label}</span>
                  </>
                );
              })()}
              {selected?.projectId && <span className="kb-detail-project">{selected.projectId}</span>}
              {selected?.at && <span className="kb-detail-time">{selected.at.slice(0, 10)}</span>}
            </div>
          </div>
          <div className="kb-detail-content">
            <h1 className="kb-detail-title">{selected?.title ?? "加载中…"}</h1>
            {selected?.tags?.length ? (
              <div className="kb-detail-tags">
                {selected.tags.map((t) => <span key={t} className="kb-card-tag">{t}</span>)}
              </div>
            ) : null}
            <div className="kb-detail-body">
              {detail.isLoading ? (
                <p className="muted">加载中…</p>
              ) : detail.isError ? (
                <p className="error">详情加载失败</p>
              ) : isPdf ? (
                <iframe
                  className="kb-pdf-viewer"
                  src={selected?.body?.startsWith("data:application/pdf")
                    ? selected.body
                    : `data:application/pdf;base64,${btoa(selected?.body ?? "")}`}
                  title={selected?.title ?? "PDF"}
                />
              ) : selected?.body ? (
                <Suspense fallback={<span className="muted">正文渲染中…</span>}>
                  <MarkdownBody text={selected.body} />
                </Suspense>
              ) : (
                <p className="muted">（无正文）</p>
              )}
            </div>
          </div>
        </div>
      ) : (
        /* ── 卡片网格 ── */
        <div className="kb-grid-wrap">
          {list.isLoading ? (
            <p className="muted">加载中…</p>
          ) : list.isError ? (
            <p className="error">加载失败</p>
          ) : items.length === 0 ? (
            <p className="muted">暂无知识条目</p>
          ) : (
            <div className="kb-grid">
              {items.map((item) => {
                const cat = categories.find((c) => c.key === item.category);
                const catColor = cat?.color ?? CATEGORY_COLORS[(item.category as KnowledgeCategory) ?? "graph"] ?? "#8b5cf6";
                const visual = cat ? ELEMENT_VISUALS[cat.element] : null;
                return (
                  <article
                    key={item.id}
                    className="kb-card"
                    style={{ "--kb-accent": catColor } as React.CSSProperties}
                    onClick={() => openDetail(String(item.id))}
                  >
                    <div className="kb-card-head">
                      <span className="kb-card-element" style={{ color: catColor, borderColor: catColor }}>
                        {visual?.icon ?? cat?.element ?? "⊕"}
                      </span>
                      <span className="kb-card-time">{item.at?.slice(0, 10) ?? ""}</span>
                    </div>
                    <h3 className="kb-card-title">{item.title}</h3>
                    <p className="kb-card-summary">{item.summary}</p>
                    {item.tags?.length ? (
                      <div className="kb-card-tags">
                        {item.tags.slice(0, 3).map((t: string) => (
                          <span key={t} className="kb-card-tag">{t}</span>
                        ))}
                      </div>
                    ) : null}
                  </article>
                );
              })}
            </div>
          )}

          {totalPages > 1 && (
            <div className="pager">
              <button className="btn btn-ghost" disabled={page <= 1}
                onClick={() => setPage((v) => Math.max(1, v - 1))}>上一页</button>
              <span className="muted">第 {page} / {totalPages} 页 · 共 {total} 条</span>
              <button className="btn btn-ghost" disabled={page >= totalPages}
                onClick={() => setPage((v) => Math.min(totalPages, v + 1))}>下一页</button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
