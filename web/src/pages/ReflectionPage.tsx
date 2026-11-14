import { useMemo, useState } from "react";
import { useList } from "@refinedev/core";
import { useNavigate } from "react-router-dom";
import type { Knowledge } from "../types";
import { WUXING } from "../wuxing";
import { WuxingLogo } from "../components/WuxingLogo";

type ReflectItem = {
  id: string;
  month: string;
  tag: string;
  title: string;
  summary?: string;
  before?: string;
  after?: string;
};

const TAG_LABEL: Record<string, string> = {
  retrospective: "复盘总结",
  optimization: "优化方案",
  lesson: "经验教训",
  feedback: "用户反馈",
  iteration: "迭代记录",
  "mgmt-evolve": "技能进化",
};

/** 演示数据：仅当真实知识库无 reflection 条目时回退展示，明确标注，不写入真实库 */
const DEMO_REFLECTIONS: ReflectItem[] = [
  {
    id: "demo-r1", month: "2026-11", tag: "retrospective", title: "知识库审计复盘",
    summary: "盘点知识库垃圾并溯源污染链路。",
    before: "知识库混入 13 条流水线 topic，Web 展示臃肿、来源不可溯。",
    after: "清理至 7 条真 KB；定位并禁用 _backfill 自动补登，根绝复发。",
  },
  {
    id: "demo-r2", month: "2026-11", tag: "optimization", title: "知识图谱改 ontology-lite",
    summary: "放弃自动本体归纳，改五行轻量图。",
    before: "想用 auto-ontology 自动学概念体系，担心产出不一致垃圾。",
    after: "钉死五行 schema，只自动填实例与边，可控、可审计、不脏。",
  },
  {
    id: "demo-r3", month: "2026-11", tag: "lesson", title: "流水线 topic 不得自动补登",
    summary: "沉淀边界共识：流水线选题不属于知识库。",
    before: "archive_knowledge / _backfill 把交付物选题自动写回知识库。",
    after: "真 KB 仅来自 OPC/知识库 目录 md；流水线产出走交付物清单。",
  },
  {
    id: "demo-r4", month: "2026-11", tag: "feedback", title: "老板：图谱要能显形垃圾",
    summary: "治理诉求：异常应一眼可见。",
    before: "垃圾混在 294 行列表里，需人工翻找。",
    after: "图谱叠加异常检测：孤儿 / 冗余簇 / 决策缺支撑 自动标红。",
  },
  {
    id: "demo-r5", month: "2026-10", tag: "mgmt-evolve", title: "CEO 技能 SOP v2",
    summary: "技能进化：增加溯源要求。",
    before: "SOP v1 只写步骤，无来源标注。",
    after: "v2 每个步骤标注依据节点，便于图谱连线与审计。",
  },
  {
    id: "demo-r6", month: "2026-10", tag: "iteration", title: "_backfill 已禁用",
    summary: "迭代记录：关闭自动补登开关。",
    before: "每次重启自动补登最新 trace 选题，污染循环。",
    after: "禁用后服务重启不再写回脏数据，知识库稳定收敛。",
  },
];

function fmtMonth(at?: string): string {
  const m = /^(\d{4}-\d{2})/.exec(at ?? "");
  return m ? m[1] : "未标注";
}

const REFLECT_TAG_GROUP: Record<string, string> = {
  "复盘总结": "retrospective", "优化方案": "optimization", "经验教训": "lesson",
  "用户反馈": "feedback", "迭代记录": "iteration", "技能进化": "mgmt-evolve",
};

function enrich(real: Knowledge[]): ReflectItem[] {
  return real.map((k) => {
    const tags = k.tags ?? [];
    const tag = tags.map((t) => REFLECT_TAG_GROUP[t]).find(Boolean) ?? "iteration";
    return {
      id: String(k.id), month: fmtMonth(k.at), tag,
      title: k.title, summary: k.summary,
    };
  });
}

export function ReflectionPage({ embedded = false }: { embedded?: boolean }) {
  const navigate = useNavigate();
  const [openId, setOpenId] = useState<string | null>(null);
  const list = useList<Knowledge>({
    resource: "knowledge",
    pagination: { current: 1, pageSize: 200 },
    filters: [{ field: "category", operator: "eq", value: "reflection" }],
    queryOptions: { refetchInterval: 60000, retry: 1 },
  });

  const real = useMemo(() => list.data?.data ?? [], [list.data]);
  const usingDemo = real.length === 0;
  const items = useMemo<ReflectItem[]>(
    () => (usingDemo ? DEMO_REFLECTIONS : enrich(real)), [usingDemo, real]);

  const byMonth = useMemo(() => {
    const m: Record<string, ReflectItem[]> = {};
    for (const it of items) (m[it.month] ??= []).push(it);
    return m;
  }, [items]);

  const stats = useMemo(() => ({
    total: items.length,
    groups: new Set(items.map((i) => i.tag)).size,
    months: Object.keys(byMonth).length,
  }), [items, byMonth]);

  return (
    <section
      className={"page rf-page" + (embedded ? " rf-embed" : "")}
      style={{ ...(WUXING.wood.vars as Record<string, string>) } as React.CSSProperties}
    >
      {!embedded && (
      <div className="rf-head">
        <WuxingLogo element="wood" size={42} />
        <div>
          <div className="rf-title">反馈迭代 <span className="rf-el">木 · 循序、日新、有度</span></div>
          <div className="rf-slogan">小步持续优化；控制迭代节奏与灰度范围，避免拔苗助长</div>
        </div>
      </div>
      )}

      {usingDemo && (
        <div className="rf-banner">
          当前真实知识库暂无「反馈迭代」条目，下列为<strong>演示数据</strong>，仅用于展示形式，不写入真实库。
        </div>
      )}
      {list.isError && !usingDemo && (
        <div className="rf-banner">迭代数据未能载入，已回退演示数据。</div>
      )}

      <div className="rf-stats">
        <div className="rf-stat"><b>{stats.total}</b><span>迭代条目</span></div>
        <div className="rf-stat"><b>{stats.groups}</b><span>涉及分组</span></div>
        <div className="rf-stat"><b>{stats.months}</b><span>跨越月份</span></div>
        <div className="rf-stat"><b>周更 · 小步</b><span>迭代节奏 / 灰度</span></div>
      </div>

      <div className="rf-timeline">
        {Object.keys(byMonth).sort().reverse().map((month) => (
          <div className="rf-month" key={month}>
            <div className="rf-month-head">{month}</div>
            {byMonth[month].map((it) => {
              const open = openId === it.id;
              return (
                <div className="rf-card" key={it.id}>
                  <div className="rf-card-head" onClick={() => setOpenId(open ? null : it.id)}>
                    <span className="rf-tag">{TAG_LABEL[it.tag] ?? it.tag}</span>
                    <span className="rf-card-title">{it.title}</span>
                    <span className="rf-caret">{open ? "▾" : "▸"}</span>
                  </div>
                  {it.summary && <p className="rf-summary">{it.summary}</p>}
                  {open && (
                    <div className="rf-diff">
                      {it.before && (
                        <div className="rf-diff-col">
                          <label>改前</label>
                          <p>{it.before}</p>
                        </div>
                      )}
                      {it.after && (
                        <div className="rf-diff-col rf-diff-col--after">
                          <label>改后</label>
                          <p>{it.after}</p>
                        </div>
                      )}
                      {!it.before && !it.after && (
                        <p className="muted">真实条目暂无结构化前后对比，可在知识库补全。</p>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>

      <div className="rf-foot">
        <button type="button" className="btn btn-ghost" onClick={() => navigate("/knowledge")}>
          在知识库查看全文 →
        </button>
      </div>
    </section>
  );
}
