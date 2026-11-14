import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import type { Knowledge, KnowledgeCategory } from "../types";
import { WUXING, WUXING_ORDER, type WuXing } from "../wuxing";
import { KB_CATEGORY_COLOR, KB_CATEGORY_MAP, KB_GRAPH_TAGS } from "../knowledgeCategories";
import { WuxingLogo } from "../components/WuxingLogo";

const MarkdownBody = lazy(() => import("../components/MarkdownBody"));

const CAT_ELEMENT: Record<KnowledgeCategory, WuXing> = {
  analysis: "earth",
  skill: "metal",
  graph: "water",
  decision: "fire",
  reflection: "wood",
};

/** 五行 → 默认分类（反向映射） */
const ELEMENT_CAT: Record<WuXing, KnowledgeCategory> = {
  earth: "analysis", metal: "skill", water: "graph", fire: "decision", wood: "reflection",
};

const ELEMENT_ZH: Record<WuXing, string> = { wood: "木", fire: "火", earth: "土", metal: "金", water: "水" };

/** 五行方位：木东（左）、火南（上）、土中、金西（右）、水北（下） */
const HUB_POS: Record<WuXing, { x: number; y: number }> = {
  wood: { x: -270, y: 0 },
  fire: { x: 0, y: -200 },
  earth: { x: 0, y: 0 },
  metal: { x: 270, y: 0 },
  water: { x: 0, y: 200 },
};

const GEN: [WuXing, WuXing][] = [
  ["wood", "fire"], ["fire", "earth"], ["earth", "metal"], ["metal", "water"], ["water", "wood"],
];
const KE: [WuXing, WuXing][] = [
  ["wood", "earth"], ["earth", "water"], ["water", "fire"], ["fire", "metal"], ["metal", "wood"],
];

const EDGE_COLOR: Record<string, string> = {
  tag: "#2563eb",
  ref: "#7c3aed",
  member: "#cbd5e1",
};

type GKind = "hub" | "entry";
type EKind = "member" | "tag" | "ref";

interface GNode {
  id: string;
  kind: GKind;
  /** 画布上的短标签 */
  label: string;
  title: string;
  element: WuXing;
  cat?: KnowledgeCategory;
  color: string;
  /** 被引用度数（入度，只统计正文引用） */
  inDeg: number;
  tags: string[];
  summary?: string;
  orphan: boolean;
  x?: number;
  y?: number;
  fx?: number;
  fy?: number;
}

interface GLink {
  id: string;
  source: string;
  target: string;
  kind: EKind;
  label?: string;
  weight: number;
}

interface GAnomaly {
  key: string;
  text: string;
  tone: "warn" | "ok";
}

interface Built {
  nodes: GNode[];
  edges: GLink[];
  byId: Map<string, GNode>;
  stats: { entries: number; relations: number; orphans: number; categories: number };
  anomalies: GAnomaly[];
}

function shortTitle(title: string): string {
  const t = String(title ?? "").replace(/\s+/g, " ").trim();
  return t.length > 10 ? `${t.slice(0, 10)}…` : t;
}

/** 去掉副标题与括注，得到用于「正文引用」匹配的标题核心 */
function coreTitle(title: string): string {
  let s = String(title ?? "");
  s = s.replace(/[（(].*$/, "");
  s = s.replace(/\s*[—–]\s*.*$/, "");
  return s.replace(/\s+/g, "").trim();
}

function nodeRadius(n: GNode): number {
  if (n.kind === "hub") return 16;
  return 5 + Math.min(9, n.inDeg * 2.2);
}

function buildGraph(raw: Knowledge[]): Built {
  const seen = new Set<string>();
  const entries = raw.filter((it) => {
    const id = String(it?.id ?? "").trim();
    const title = String(it?.title ?? "").trim();
    if (!id || id === "null" || id === "undefined" || !title || seen.has(id)) return false;
    seen.add(id);
    return true;
  });

  const nodes: GNode[] = [];
  const byId = new Map<string, GNode>();

  for (const el of WUXING_ORDER) {
    const p = HUB_POS[el];
    const hub: GNode = {
      id: `hub:${el}`, kind: "hub", label: ELEMENT_ZH[el], title: WUXING[el].fullName,
      element: el, color: WUXING[el].color, inDeg: 0, tags: [], orphan: false,
      fx: p.x, fy: p.y, x: p.x, y: p.y,
    };
    nodes.push(hub);
    byId.set(hub.id, hub);
  }

  const groups = new Map<WuXing, GNode[]>();
  const entryNodes = entries.map((it) => {
    const rawCat = it.category as KnowledgeCategory | undefined;
    // 未知/缺失分类兜底到「知识图谱（水）」，避免 KB_CATEGORY_MAP 取空
    const cat: KnowledgeCategory = rawCat && KB_CATEGORY_COLOR[rawCat] ? rawCat : "graph";
    const el = CAT_ELEMENT[cat] ?? "water";
    const node: GNode = {
      id: String(it.id),
      kind: "entry",
      label: shortTitle(it.title),
      title: it.title,
      element: el,
      cat,
      // 节点颜色取分类定义色（KB_CATEGORIES.color），而非五行主题色
      color: KB_CATEGORY_COLOR[cat] ?? WUXING[el].color,
      inDeg: 0,
      tags: (it.tags ?? []).map(String),
      summary: it.summary,
      orphan: false,
    };
    byId.set(node.id, node);
    const arr = groups.get(el) ?? [];
    arr.push(node);
    groups.set(el, arr);
    return node;
  });
  nodes.push(...entryNodes);

  // ---- 共享标签建边：只取「信息量适中」的标签（出现 2~4 次）----
  const tagGroups = new Map<string, GNode[]>();
  for (const n of entryNodes) {
    for (const t of new Set(n.tags)) {
      if (!t) continue;
      const arr = tagGroups.get(t) ?? [];
      arr.push(n);
      tagGroups.set(t, arr);
    }
  }

  interface Pair { a: GNode; b: GNode; tags: string[]; ref: boolean; from?: string; to?: string }
  const pairs = new Map<string, Pair>();
  const keyOf = (x: string, y: string) => (x < y ? `${x}||${y}` : `${y}||${x}`);

  for (const [tag, arr] of tagGroups) {
    const d = arr.length;
    if (d < 2 || d > 4) continue;
    for (let i = 0; i < arr.length; i++) {
      for (let j = i + 1; j < arr.length; j++) {
        const k = keyOf(arr[i].id, arr[j].id);
        const cur = pairs.get(k);
        if (cur) cur.tags.push(tag);
        else pairs.set(k, { a: arr[i], b: arr[j], tags: [tag], ref: false });
      }
    }
  }

  // ---- 正文引用建边：A 的正文出现 B 的标题核心即视为「A 引用 B」----
  const meta = entries.map((it) => ({
    node: byId.get(String(it.id)) as GNode,
    body: String(it.body ?? "").replace(/\s+/g, "").toLowerCase(),
  }));
  for (const A of meta) {
    for (const B of meta) {
      if (A.node.id === B.node.id) continue;
      const core = coreTitle(B.node.title).toLowerCase();
      if (core.length < 4) continue;
      if (!A.body.includes(core)) continue;
      B.node.inDeg += 1;
      const k = keyOf(A.node.id, B.node.id);
      const cur = pairs.get(k);
      if (cur) {
        cur.ref = true;
        cur.from = cur.from ?? A.node.id;
        cur.to = cur.to ?? B.node.id;
      } else {
        pairs.set(k, { a: A.node, b: B.node, tags: [], ref: true, from: A.node.id, to: B.node.id });
      }
    }
  }

  const edges: GLink[] = [];
  for (const p of pairs.values()) {
    const kind: EKind = p.tags.length ? "tag" : "ref";
    const directed = p.tags.length === 0 && p.from && p.to;
    edges.push({
      id: `r:${keyOf(p.a.id, p.b.id)}`,
      source: directed ? (p.from as string) : p.a.id,
      target: directed ? (p.to as string) : p.b.id,
      kind,
      label: [p.tags.map((t) => `#${t}`).join(" "), p.ref ? "引用" : ""].filter(Boolean).join(" · "),
      weight: p.tags.length + (p.ref ? 1 : 0),
    });
  }

  for (const n of entryNodes) {
    edges.push({ id: `m:${n.id}`, source: n.id, target: `hub:${n.element}`, kind: "member", weight: 1 });
    n.orphan = n.inDeg === 0 && !edges.some(
      (e) => e.kind !== "member" && (e.source === n.id || e.target === n.id),
    );
  }

  // ---- 拓扑体检 ----
  const orphanList = entryNodes.filter((n) => n.orphan);
  const decisionNoMetal: GNode[] = [];
  for (const n of entryNodes) {
    if (n.cat !== "decision") continue;
    const nb = edges
      .filter((e) => e.kind !== "member" && (e.source === n.id || e.target === n.id))
      .map((e) => byId.get(e.source === n.id ? e.target : e.source))
      .filter((x): x is GNode => !!x);
    if (!nb.some((m) => m.element === "metal")) decisionNoMetal.push(n);
  }
  const emptyCats = WUXING_ORDER.filter((el) => !(groups.get(el)?.length ?? 0));

  const anomalies: GAnomaly[] = [];
  anomalies.push(orphanList.length
    ? { key: "orphan", tone: "warn", text: `${orphanList.length} 个孤儿节点：${orphanList.map((n) => n.label).join("、")}` }
    : { key: "orphan", tone: "ok", text: "无孤儿节点（每条知识至少有一条关系边）" });
  anomalies.push(decisionNoMetal.length
    ? { key: "dmetal", tone: "warn", text: `${decisionNoMetal.length} 条决策缺「金」技能支撑：${decisionNoMetal.map((n) => n.label).join("、")}` }
    : { key: "dmetal", tone: "ok", text: "决策均有「金」技能支撑" });
  if (emptyCats.length) {
    anomalies.push({
      key: "empty",
      tone: "warn",
      text: `空分类：${emptyCats.map((el) => `${ELEMENT_ZH[el]}·${KB_CATEGORY_MAP[ELEMENT_CAT[el]]?.label ?? ""}`).join("、")}`,
    });
  }

  return {
    nodes,
    edges,
    byId,
    stats: {
      entries: entryNodes.length,
      relations: pairs.size,
      orphans: orphanList.length,
      categories: groups.size,
    },
    anomalies,
  };
}

function withAlpha(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const r = parseInt(full.slice(0, 2), 16);
  const g = parseInt(full.slice(2, 4), 16);
  const b = parseInt(full.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export function GraphPage({ embedded = false }: { embedded?: boolean }) {
  const [rows, setRows] = useState<Knowledge[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [showWuxing, setShowWuxing] = useState(true);

  const [detail, setDetail] = useState<Knowledge | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const wrapRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<ForceGraphMethods>();
  const fittedRef = useRef(false);
  const posRef = useRef(new Map<string, { x: number; y: number }>());
  const [size, setSize] = useState({ w: 880, h: embedded ? 500 : 620 });

  // ---- 取数：带正文，一次拿全，用于推导引用边 ----
  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const all: Knowledge[] = [];
        let page = 1;
        for (;;) {
          const res = await fetch(`/api/knowledge?page=${page}&size=100&with_body=1`, {
            credentials: "include",
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const json = (await res.json()) as { total?: number; items?: Knowledge[] };
          const items = json.items ?? [];
          all.push(...items);
          const total = Number(json.total ?? all.length);
          if (items.length === 0 || all.length >= total || page >= 20) break;
          page += 1;
        }
        if (!cancelled) {
          // 数据没变就不换引用，避免 60s 轮询把力导向布局重新抖动一次
          setRows((prev) => {
            const same = prev.length === all.length && all.every((a, i) => {
              const b = prev[i];
              return String(a.id) === String(b.id)
                && a.category === b.category
                && (a.tags ?? []).join("|") === (b.tags ?? []).join("|")
                && (a.body ?? "").length === (b.body ?? "").length;
            });
            return same ? prev : all;
          });
          setLoadError(null);
        }
      } catch (err) {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    const timer = window.setInterval(load, 60000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  // ---- 容器尺寸 ----
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const height = embedded ? 500 : 620;
    const apply = (w: number) => {
      if (w > 0) setSize({ w: Math.round(w), h: height });
    };
    apply(el.clientWidth);
    const ro = new ResizeObserver((entries) => apply(entries[0].contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, [embedded]);

  const built = useMemo<Built>(() => buildGraph(rows), [rows]);
  const { nodes, edges, byId, stats, anomalies } = built;

  // ---- 按 graph tag 过滤 ----
  const graphData = useMemo(() => {
    const kept = nodes.filter((n) => n.kind === "hub" || !activeTag || n.tags.includes(activeTag));
    const keepIds = new Set(kept.map((n) => n.id));
    const links = edges.filter((l) => keepIds.has(l.source) && keepIds.has(l.target));
    return {
      // 拷贝一份：force-graph 会往节点/边上写坐标与对象引用
      nodes: kept.map((n) => {
        const p = posRef.current.get(n.id);
        return p ? { ...n, x: p.x, y: p.y } : { ...n };
      }),
      links: links.map((l) => ({ ...l })),
    };
  }, [nodes, edges, activeTag]);

  const hits = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return new Set<string>();
    return new Set(
      nodes
        .filter((n) => n.kind === "entry" && (n.title.toLowerCase().includes(q) || n.tags.some((t) => t.toLowerCase().includes(q))))
        .map((n) => n.id),
    );
  }, [query, nodes]);

  const focusId = hoverId ?? selectedId;
  const neighbors = useMemo(() => {
    const s = new Set<string>();
    if (!focusId) return s;
    for (const e of edges) {
      if (e.source === focusId) s.add(e.target);
      if (e.target === focusId) s.add(e.source);
    }
    return s;
  }, [focusId, edges]);

  const selected = selectedId ? byId.get(selectedId) ?? null : null;
  const relList = useMemo(() => {
    if (!selected) return [] as { id: string; kind: EKind; other: GNode }[];
    return edges
      .filter((e) => e.source === selected.id || e.target === selected.id)
      .map((e) => {
        const other = byId.get(e.source === selected.id ? e.target : e.source);
        return other ? { id: e.id, kind: e.kind, other } : null;
      })
      .filter((x): x is { id: string; kind: EKind; other: GNode } => !!x);
  }, [selected, edges, byId]);

  // ---- 侧栏详情：走 /api/knowledge/:id（复用 knowledge_detail）----
  useEffect(() => {
    if (!selected || selected.kind !== "entry") {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    fetch(`/api/knowledge/${encodeURIComponent(selected.id)}`, { credentials: "include" })
      .then((res) => (res.ok ? res.json() : null))
      .then((json) => {
        if (!cancelled) setDetail((json as Knowledge) ?? null);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  // ---- 力参数 ----
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    fg.d3Force("charge")?.strength(-210).distanceMax(520);
    fg.d3Force("center")?.strength(0.04);
    const link = fg.d3Force("link");
    link?.distance((l: GLink) => (l.kind === "member" ? 92 : 60));
    link?.strength((l: GLink) => (l.kind === "member" ? 0.55 : 0.28));
    fittedRef.current = false;
  }, [graphData]);

  useEffect(() => {
    fittedRef.current = false;
  }, [activeTag]);

  const centerOn = useCallback((id: string) => {
    // 坐标由 force-graph 写在渲染副本上，必须从 graphData 取实时值
    const n = graphData.nodes.find((x) => x.id === id);
    if (!n || typeof n.x !== "number" || typeof n.y !== "number") return;
    fgRef.current?.centerAt(n.x, n.y, 600);
    fgRef.current?.zoom(3.2, 600);
  }, [graphData]);

  const onSearchSubmit = useCallback(() => {
    const first = Array.from(hits)[0];
    if (first) {
      setSelectedId(first);
      centerOn(first);
    }
  }, [hits, centerOn]);

  // ---- 绘制 ----
  const drawNode = useCallback((raw: unknown, ctx: CanvasRenderingContext2D, scale: number) => {
    const n = raw as GNode;
    if (typeof n.x !== "number" || typeof n.y !== "number") return;
    const isHub = n.kind === "hub";
    const isSel = n.id === selectedId;
    const isHover = n.id === hoverId;
    const isHit = hits.has(n.id);
    const focus = focusId;
    const dim = !!focus && n.id !== focus && !neighbors.has(n.id) && !isHit;

    const r = nodeRadius(n) * (isHover ? 1.18 : 1);
    ctx.save();
    ctx.globalAlpha = dim ? 0.16 : 1;

    ctx.beginPath();
    ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
    ctx.fillStyle = n.color;
    ctx.fill();

    if (isSel || isHit) {
      ctx.lineWidth = 3 / scale;
      ctx.strokeStyle = isHit && !isSel ? "#f59e0b" : "#0f172a";
      ctx.stroke();
    } else if (n.orphan) {
      ctx.setLineDash([3 / scale, 2 / scale]);
      ctx.lineWidth = 1.2 / scale;
      ctx.strokeStyle = "#94a3b8";
      ctx.stroke();
      ctx.setLineDash([]);
    } else {
      ctx.lineWidth = 1.4 / scale;
      ctx.strokeStyle = "#ffffff";
      ctx.stroke();
    }

    if (isHub) {
      const fs = 14 / scale;
      ctx.font = `700 ${fs}px "PingFang SC","Microsoft YaHei",system-ui,sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "#ffffff";
      ctx.fillText(n.label, n.x, n.y);
    } else if (scale > 0.55) {
      const fs = 10 / scale;
      ctx.font = `${fs}px "PingFang SC","Microsoft YaHei",system-ui,sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = dim ? "rgba(71,85,105,.35)" : "#334155";
      ctx.fillText(n.label, n.x, n.y + r + 2 / scale);
    }
    ctx.restore();
  }, [selectedId, hoverId, focusId, neighbors, hits]);

  const paintPointer = useCallback((raw: unknown, color: string, ctx: CanvasRenderingContext2D) => {
    const n = raw as GNode;
    if (typeof n.x !== "number" || typeof n.y !== "number") return;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(n.x, n.y, nodeRadius(n) + 3, 0, Math.PI * 2);
    ctx.fill();
  }, []);

  const linkColor = useCallback((raw: unknown) => {
    const l = raw as GLink;
    const base = EDGE_COLOR[l.kind] ?? "#cbd5e1";
    if (l.kind === "member") return withAlpha(base, 0.55);
    if (!focusId) return withAlpha(base, 0.75);
    // force-graph 初始化后会把 link.source/target 从 id 替换成节点对象，两种都要兼容
    const idOf = (v: unknown) => (typeof v === "string" ? v : String((v as GNode | null)?.id ?? ""));
    const hit = idOf(l.source) === focusId || idOf(l.target) === focusId;
    return withAlpha(base, hit ? 1 : 0.12);
  }, [focusId]);

  const linkWidth = useCallback((raw: unknown) => {
    const l = raw as GLink;
    if (l.kind === "tag") return 1 + Math.min(1.8, l.weight * 0.45);
    if (l.kind === "ref") return 1.4;
    return 0.8;
  }, []);

  // 五行生克叠加层（不参与力学，逐帧手绘）
  const drawWuxingLayer = useCallback((ctx: CanvasRenderingContext2D, scale: number) => {
    if (!showWuxing) return;
    const draw = (a: WuXing, b: WuXing, kind: "sheng" | "ke") => {
      const p = HUB_POS[a];
      const q = HUB_POS[b];
      const dx = q.x - p.x;
      const dy = q.y - p.y;
      const len = Math.hypot(dx, dy) || 1;
      const ux = dx / len;
      const uy = dy / len;
      const inset = 19;
      const a2 = { x: p.x + ux * inset, y: p.y + uy * inset };
      const b2 = { x: q.x - ux * inset, y: q.y - uy * inset };
      const mx = (a2.x + b2.x) / 2;
      const my = (a2.y + b2.y) / 2;
      const bend = kind === "sheng" ? 24 : -24;
      const cx = mx - uy * bend;
      const cy = my + ux * bend;

      ctx.save();
      ctx.beginPath();
      ctx.moveTo(a2.x, a2.y);
      ctx.quadraticCurveTo(cx, cy, b2.x, b2.y);
      ctx.lineWidth = 1.5 / scale;
      ctx.strokeStyle = kind === "sheng" ? "rgba(62,168,116,.62)" : "rgba(217,80,63,.58)";
      if (kind === "ke") ctx.setLineDash([4 / scale, 3 / scale]);
      ctx.stroke();
      ctx.setLineDash([]);

      const bx = 0.25 * a2.x + 0.5 * cx + 0.25 * b2.x;
      const by = 0.25 * a2.y + 0.5 * cy + 0.25 * b2.y;
      const rr = 9 / scale;

      ctx.beginPath();
      ctx.arc(bx, by, rr, 0, Math.PI * 2);
      ctx.fillStyle = "#ffffff";
      ctx.fill();
      ctx.lineWidth = 1.2 / scale;
      ctx.strokeStyle = kind === "sheng" ? "#3ea874" : "#d9503f";
      ctx.stroke();

      ctx.font = `600 ${10 / scale}px "PingFang SC","Microsoft YaHei",system-ui,sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = kind === "sheng" ? "#2e7d5b" : "#b3382a";
      ctx.fillText(kind === "sheng" ? "生" : "克", bx, by);
      ctx.restore();
    };
    GEN.forEach(([a, b]) => draw(a, b, "sheng"));
    KE.forEach(([a, b]) => draw(a, b, "ke"));
  }, [showWuxing]);

  const onRenderFramePost = useCallback((ctx: CanvasRenderingContext2D, scale: number) => {
    // 记录坐标，避免刷新/切换筛选后重新抖动
    for (const n of graphData.nodes) {
      if (typeof n.x === "number" && typeof n.y === "number") {
        posRef.current.set(String(n.id), { x: n.x, y: n.y });
      }
    }
    drawWuxingLayer(ctx, scale);
  }, [graphData, drawWuxingLayer]);

  return (
    <section
      className={"page gx-page" + (embedded ? " gx-embed" : "")}
      style={{ ...(WUXING.water.vars as Record<string, string>) } as React.CSSProperties}
    >
      {!embedded && (
        <div className="gx-head">
          <WuxingLogo element="water" size={42} />
          <div>
            <div className="gx-title">知识图谱 <span className="gx-el">水 · 通联、溯源、无滞</span></div>
            <div className="gx-slogan">节点为知识条目、大小取决于被引用度数；五行为锚，标签与引用为边</div>
          </div>
        </div>
      )}

      {loading ? (
        <div className="gx-banner">正在汇总知识库关系…</div>
      ) : loadError ? (
        <div className="gx-banner">关系图加载失败：{loadError}</div>
      ) : stats.entries === 0 ? (
        <div className="gx-banner">知识库暂无条目，图谱为空。先去「全部」或对应分类归档条目。</div>
      ) : (
        <>
          <div className="gx-toolbar">
            <div className="gx-search">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") onSearchSubmit(); }}
                placeholder="搜索标题或标签，回车定位"
                aria-label="搜索知识条目"
              />
              {query && (
                <button type="button" className="gx-search-clear" onClick={() => setQuery("")} aria-label="清空">×</button>
              )}
              {query && (
                <span className="gx-search-hit">{hits.size} 条命中</span>
              )}
            </div>

            <div className="gx-chips gx-chips--filter">
              <button
                type="button"
                className={"gx-chip gx-chip--btn" + (activeTag === null ? " is-on" : "")}
                onClick={() => setActiveTag(null)}
              >全部</button>
              {KB_GRAPH_TAGS.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  className={"gx-chip gx-chip--btn" + (activeTag === t.key ? " is-on" : "")}
                  onClick={() => setActiveTag(activeTag === t.key ? null : t.key)}
                  title={`按 #${t.label} 过滤`}
                >{t.label}</button>
              ))}
            </div>

            <label className="gx-switch">
              <input type="checkbox" checked={showWuxing} onChange={(e) => setShowWuxing(e.target.checked)} />
              <span>五行生克</span>
            </label>
          </div>

          <div className="gx-layout">
            <div className="gx-canvas" ref={wrapRef}>
              <ForceGraph2D
                ref={fgRef}
                graphData={graphData}
                width={size.w}
                height={size.h}
                backgroundColor="transparent"
                cooldownTicks={120}
                warmupTicks={40}
                nodeRelSize={4}
                nodeCanvasObject={drawNode}
                nodePointerAreaPaint={paintPointer}
                nodeLabel={(n) => (n as unknown as GNode).title}
                linkColor={linkColor}
                linkWidth={linkWidth}
                linkLabel={(l) => (l as unknown as GLink).label ?? ""}
                linkDirectionalArrowLength={(l) => ((l as unknown as GLink).kind === "ref" ? 3.2 : 0)}
                linkDirectionalArrowRelPos={1}
                linkCurvature={(l) => ((l as unknown as GLink).kind === "member" ? 0 : 0.12)}
                onNodeClick={(n) => { setSelectedId(String((n as unknown as GNode).id)); }}
                onNodeHover={(n) => setHoverId(n ? String((n as unknown as GNode).id) : null)}
                onBackgroundClick={() => setSelectedId(null)}
                onRenderFramePost={onRenderFramePost}
                onEngineStop={() => {
                  if (!fittedRef.current) {
                    fgRef.current?.zoomToFit(500, 64);
                    fittedRef.current = true;
                  }
                }}
              />
            </div>

            <div className="gx-side">
              {selected ? (
                <div className="gx-detail">
                  <div className="gx-detail-top">
                    <span className="gx-detail-el" style={{ color: selected.color }}>
                      {ELEMENT_ZH[selected.element]} · {selected.kind === "hub" ? "五行枢纽" : KB_CATEGORY_MAP[selected.cat ?? "graph"].label}
                    </span>
                    <button type="button" className="gx-close" onClick={() => setSelectedId(null)} aria-label="关闭">×</button>
                  </div>
                  <h3 className="gx-detail-title">{selected.title}</h3>

                  {selected.kind === "entry" ? (
                    <>
                      <div className="gx-detail-meta">
                        <span>被引用 {selected.inDeg}</span>
                        <span>{relList.filter((r) => r.kind !== "member").length} 条关系</span>
                        <span>{selected.tags.length} 个标签</span>
                      </div>
                      {selected.tags.length > 0 && (
                        <div className="gx-chips">
                          {selected.tags.map((t) => <span key={t} className="gx-chip">#{t}</span>)}
                        </div>
                      )}
                      <div className="gx-detail-body">
                        {detailLoading ? (
                          <div className="gx-muted">正在加载正文…</div>
                        ) : detail?.body ? (
                          <Suspense fallback={<div className="gx-muted">正文渲染中…</div>}>
                            <MarkdownBody text={detail.body} />
                          </Suspense>
                        ) : (
                          <div className="gx-muted">{selected.summary || "该条目暂无正文。"}</div>
                        )}
                      </div>
                      <div className="gx-rel-head">邻里关系（{relList.length}）</div>
                      <ul className="gx-rel-list">
                        {relList.map((r) => (
                          <li key={r.id} onClick={() => { setSelectedId(r.other.id); centerOn(r.other.id); }}>
                            <span className={`gx-rel-kind gx-rel-kind--${r.kind}`}>
                              {r.kind === "tag" ? "共享标签" : r.kind === "ref" ? "正文引用" : "五行归属"}
                            </span>
                            <span className="gx-rel-title">
                              {r.other.kind === "hub"
                                ? `${ELEMENT_ZH[r.other.element]}·${KB_CATEGORY_MAP[ELEMENT_CAT[r.other.element]].label}`
                                : r.other.title}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </>
                  ) : (
                    <>
                      <div className="gx-detail-meta">
                        <span>{(nodes.filter((n) => n.element === selected.element && n.kind === "entry").length)} 条成员</span>
                      </div>
                      <p className="gx-detail-sum">
                        {KB_CATEGORY_MAP[ELEMENT_CAT[selected.element]].description}
                      </p>
                      <ul className="gx-rel-list">
                        {nodes
                          .filter((n) => n.element === selected.element && n.kind === "entry")
                          .map((n) => (
                            <li key={n.id} onClick={() => { setSelectedId(n.id); centerOn(n.id); }}>
                              <span className="gx-rel-kind gx-rel-kind--member">五行归属</span>
                              <span className="gx-rel-title">{n.title}</span>
                            </li>
                          ))}
                      </ul>
                    </>
                  )}
                </div>
              ) : (
                <div className="gx-report">
                  <div className="gx-report-title">拓扑体检</div>
                  <div className="gx-statgrid">
                    <div><b>{stats.entries}</b><span>条目</span></div>
                    <div><b>{stats.relations}</b><span>关系</span></div>
                    <div><b>{stats.orphans}</b><span>孤儿</span></div>
                    <div><b>{stats.categories}</b><span>覆盖分类</span></div>
                  </div>
                  <ul className="gx-anomaly">
                    {anomalies.map((a) => (
                      <li key={a.key} className={`gx-anomaly--${a.tone}`}>{a.text}</li>
                    ))}
                  </ul>
                  <div className="gx-legend">
                    <div className="gx-legend-row"><i className="gx-dot" style={{ background: EDGE_COLOR.tag }} />共享标签</div>
                    <div className="gx-legend-row"><i className="gx-dot" style={{ background: EDGE_COLOR.ref }} />正文引用</div>
                    <div className="gx-legend-row"><i className="gx-dot" style={{ background: EDGE_COLOR.member }} />五行归属</div>
                    <div className="gx-legend-row"><i className="gx-line gx-line--sheng" />相生（生 →）</div>
                    <div className="gx-legend-row"><i className="gx-line gx-line--ke" />相克（克 ×）</div>
                  </div>
                  <div className="gx-hint">
                    点节点看详情；节点大小 = 被引用度数；「完成」后自动适应视图。
                  </div>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </section>
  );
}