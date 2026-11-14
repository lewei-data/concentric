import { useMemo, useState } from "react";
import { useList } from "@refinedev/core";
import { useNavigate } from "react-router-dom";
import type { Knowledge } from "../types";
import { WUXING } from "../wuxing";
import { WuxingLogo } from "../components/WuxingLogo";

type DecisionModel = {
  id: string;
  title: string;
  summary?: string;
  principle?: string;
  when?: string;
  then?: string;
  priority?: string;
  risk?: string;
  basis?: string[];
  tags?: string[];
  group?: string;
};

/** 演示数据：仅当真实知识库无 decision 条目时回退展示，明确标注，不写入真实库 */
const DEMO_DECISIONS: DecisionModel[] = [
  {
    id: "demo-principle-1", group: "principle", title: "内容开工审批原则",
    summary: "内容先于产出，合规为底线；任何出图/短视频任务须过三检。",
    principle: "内容先于产出，合规为底线。",
    when: "任何出图 / 短视频 / 蹭热点类任务启动前",
    then: "完成成本、素材、合规三检并全部通过后才允许开工",
    priority: "高", risk: "低",
    basis: ["MiniMax H3-Image 出图技能", "知识库审计报告"],
    tags: ["principle", "合规"],
  },
  {
    id: "demo-strategy-1", group: "strategy", title: "内容方向优先级",
    summary: "老板战略意图优先于流量投机；先吃透战略洞察再排期。",
    principle: "战略意图 > 流量投机。",
    when: "季度内容排期 / 新栏目立项",
    then: "优先承接老板战略洞察，蹭量内容控制在 20% 以内",
    priority: "高", risk: "中",
    basis: ["管理层五行学习计划", "CEO 五行学习技能"],
    tags: ["strategy", "排期"],
  },
  {
    id: "demo-risk-1", group: "risk", title: "热点蹭量风险评估",
    summary: "蹭热点须不违平台规则、不碰敏感议题，否则一票否决。",
    principle: "热点可蹭，红线不碰。",
    when: "选用热点选题 / 跟榜内容",
    then: "敏感词、争议议题、未经核实信源一律否决，走战略洞察复核",
    priority: "高", risk: "高",
    basis: ["内容开工审批原则"],
    tags: ["risk", "热点"],
  },
  {
    id: "demo-heuristic-1", group: "heuristic", title: "短视频完播优先",
    summary: "前 3 秒定生死；钩子先行、信息密度优先于修饰。",
    principle: "前 3 秒定生死。",
    when: "短视频脚本结构决策",
    then: "首帧给钩子，15 秒内抛出核心冲突，修饰后置",
    priority: "中", risk: "低",
    basis: ["MiniMax H3-Image 出图技能"],
    tags: ["heuristic", "脚本"],
  },
  {
    id: "demo-domain-1", group: "domain-model", title: "OPC 内容生产领域模型",
    summary: "创意→作品→工厂→知识→运维的五行闭环，决策落点需在火位。",
    principle: "决策落点在火（明断），其上游须有金（技能）支撑。",
    when: "任何涉及流程/职责分工的判断",
    then: "参照五行闭环定位责任域，缺上游支撑则先补再决",
    priority: "中", risk: "低",
    basis: ["知识库审计报告", "管理层五行学习计划"],
    tags: ["domain-model"],
  },
  {
    id: "demo-insight-1", group: "mgmt-insight", title: "知识库宁可少不可脏",
    summary: "管理层共识：知识库以可信为先，垃圾条目比空白更有害。",
    principle: "可信 > 完备。",
    when: "是否沉淀某条内容 / 是否自动补登",
    then: "流水线 topic 不自动进知识库；存疑条目先归档不入库",
    priority: "高", risk: "低",
    basis: ["知识库审计报告"],
    tags: ["mgmt-insight", "治理"],
  },
];

const GROUPS = [
  { key: "principle", label: "原则框架" },
  { key: "strategy", label: "战略决策" },
  { key: "risk", label: "风险评估" },
  { key: "heuristic", label: "经验法则" },
  { key: "domain-model", label: "领域模型" },
  { key: "mgmt-insight", label: "战略洞察" },
];

const DECISION_TAG_GROUP: Record<string, string> = {
  "领域模型": "domain-model", "战略决策": "strategy", "风险评估": "risk",
  "原则框架": "principle", "经验法则": "heuristic", "战略洞察": "mgmt-insight",
};

function enrich(real: Knowledge[]): DecisionModel[] {
  return real.map((k) => {
    const tags = k.tags ?? [];
    const group = tags.map((t) => DECISION_TAG_GROUP[t]).find(Boolean) ?? "principle";
    return {
      id: String(k.id), title: k.title, summary: k.summary,
      principle: k.title, when: "（见正文）", then: k.summary,
      priority: "未标注", risk: "未标注", basis: [], tags, group,
    };
  });
}

function priorClass(p?: string) {
  if (p === "高") return "dm-pill dm-pill--high";
  if (p === "中") return "dm-pill dm-pill--mid";
  if (p === "低") return "dm-pill dm-pill--low";
  return "dm-pill dm-pill--na";
}

export function DecisionPage({ embedded = false }: { embedded?: boolean }) {
  const navigate = useNavigate();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const list = useList<Knowledge>({
    resource: "knowledge",
    pagination: { current: 1, pageSize: 200 },
    filters: [{ field: "category", operator: "eq", value: "decision" }],
    queryOptions: { refetchInterval: 60000, retry: 1 },
  });

  const real = useMemo(() => list.data?.data ?? [], [list.data]);
  const usingDemo = real.length === 0;
  const decisions = useMemo<DecisionModel[]>(
    () => (usingDemo ? DEMO_DECISIONS : enrich(real)), [usingDemo, real]);

  const grouped = useMemo(() => {
    const m: Record<string, DecisionModel[]> = {};
    for (const g of GROUPS) m[g.key] = [];
    for (const d of decisions) (m[d.group ?? "principle"] ??= []).push(d);
    return m;
  }, [decisions]);

  const stats = useMemo(() => {
    const withBasis = decisions.filter((d) => (d.basis?.length ?? 0) > 0).length;
    const highRisk = decisions.filter((d) => d.risk === "高").length;
    return {
      total: decisions.length,
      coverage: decisions.length ? Math.round((withBasis / decisions.length) * 100) : 0,
      highRisk,
    };
  }, [decisions]);

  const selected = decisions.find((d) => d.id === selectedId) ?? decisions[0] ?? null;

  return (
    <section
      className={"page dm-page" + (embedded ? " dm-embed" : "")}
      style={{ ...(WUXING.fire.vars as Record<string, string>) } as React.CSSProperties}
    >
      {!embedded && (
      <div className="dm-head">
        <WuxingLogo element="fire" size={42} />
        <div>
          <div className="dm-title">决策模型 <span className="dm-el">火 · 明断、守序、不妄</span></div>
          <div className="dm-slogan">规则明确、边界清晰；顶层范式不轻易变动</div>
        </div>
      </div>
      )}

      {usingDemo && (
        <div className="dm-banner">
          当前真实知识库暂无「决策模型」条目，下列为<strong>演示数据</strong>，仅用于展示形式，不写入真实库。
        </div>
      )}
      {list.isError && !usingDemo && (
        <div className="dm-banner">决策数据未能载入，已回退演示数据。</div>
      )}

      <div className="dm-stats">
        <div className="dm-stat"><b>{stats.total}</b><span>决策条目</span></div>
        <div className="dm-stat"><b>{stats.coverage}%</b><span>依据覆盖度</span></div>
        <div className="dm-stat"><b>{stats.highRisk}</b><span>高风险决策</span></div>
        <div className="dm-stat"><b>{GROUPS.length}</b><span>原则分组</span></div>
      </div>

      <div className="dm-main">
        <div className="dm-tree">
          <h3>原则框架树</h3>
          {GROUPS.map((g) => (
            <div className="dm-group" key={g.key}>
              <div className="dm-group-title">{g.label}（{grouped[g.key]?.length ?? 0}）</div>
              {(grouped[g.key] ?? []).map((d) => (
                <div
                  key={d.id}
                  className={`dm-node ${selected?.id === d.id ? "active" : ""}`}
                  onClick={() => setSelectedId(d.id)}
                >
                  {d.title}
                </div>
              ))}
            </div>
          ))}
        </div>

        <div className="dm-detail">
          {selected ? (
            <>
              <div className="dm-detail-head">
                <h2>{selected.title}</h2>
                {!embedded && (
                <button type="button" className="btn btn-ghost dm-link"
                        onClick={() => navigate("/knowledge")}>在知识库查看全文 →</button>
                )}
              </div>
              {selected.summary && <p className="dm-summary">{selected.summary}</p>}
              <div className="dm-field">
                <label>原则（principle）</label>
                <div>{selected.principle || "—"}</div>
              </div>
              <div className="dm-grid2">
                <div className="dm-field">
                  <label>触发条件（when）</label>
                  <div>{selected.when || "—"}</div>
                </div>
                <div className="dm-field">
                  <label>动作（then）</label>
                  <div>{selected.then || "—"}</div>
                </div>
              </div>
              <div className="dm-grid2">
                <div className="dm-field">
                  <label>优先级</label>
                  <span className={priorClass(selected.priority)}>{selected.priority || "未标注"}</span>
                </div>
                <div className="dm-field">
                  <label>风险</label>
                  <span className={priorClass(selected.risk)}>{selected.risk || "未标注"}</span>
                </div>
              </div>
              <div className="dm-field">
                <label>依据溯源（basis）</label>
                <div className="dm-basis">
                  {(selected.basis ?? []).length
                    ? selected.basis!.map((b) => <span key={b} className="dm-chip">{b}</span>)
                    : <span className="muted">无（真实条目待补结构化依据）</span>}
                </div>
              </div>
            </>
          ) : (
            <p className="muted">暂无决策条目。</p>
          )}
        </div>
      </div>

      <div className="dm-matrix">
        <h3>决策矩阵</h3>
        <table>
          <thead>
            <tr><th>决策</th><th>优先级</th><th>风险</th><th>触发条件</th><th>动作</th></tr>
          </thead>
          <tbody>
            {decisions.map((d) => (
              <tr key={d.id}>
                <td>{d.title}</td>
                <td><span className={priorClass(d.priority)}>{d.priority || "未标注"}</span></td>
                <td><span className={priorClass(d.risk)}>{d.risk || "未标注"}</span></td>
                <td>{d.when || "—"}</td>
                <td>{d.then || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
