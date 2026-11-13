import { useCallback, useEffect, useMemo, useState } from "react";
import { useGetIdentity } from "@refinedev/core";
import type { OpcUser } from "../types";

// ---------- 类型 ----------
interface Workshop {
  no: string;
  id: string;
  name: string;
  product: string;
  desc: string;
}
/* 工艺步骤：每个工艺的工序序列与名称不同，CCO 定工艺时定格（快照存 product_crafts.steps） */
interface CraftStep {
  id: string;
  name: string;
  omStage: string;
  opcStage: string;
  status: string;
  startedAt: string;
  finishedAt: string;
}
interface StepView {
  trace_id?: string;
  stage_id: string;
  stage_name: string;
  om_stage: string;
  status: string;
  started_at: string;
  finished_at: string;
  detail: { items?: number; kind?: string; name?: string } | null;
}
interface Product {
  itemId: string;
  traceId: string;
  serialNo: string;
  projectId: string;
  title: string;
  workshopNo: string;
  craftId: string;
  craftName: string;
  product: string;
  renderMode: string;
  reason: string;
  decidedBy: string;
  decidedAt: string;
  shippedAt: string;
  craftSteps: CraftStep[];
  run: { status: string; stage: string; stageIndex: number; updatedAt: string; closedAt: string };
  steps: StepView[];
}
interface FactoryData {
  ok: boolean;
  workshops: Workshop[];
  products: Product[];
}
interface StepEvent {
  id: number;
  kind: string;
  at: string;
  data: Record<string, unknown> | null;
}
interface StepDetail {
  ok: boolean;
  step?: StepView;
  events?: StepEvent[];
  error?: string;
}

const STATUS_LABEL: Record<string, string> = {
  running: "生产中",
  completed: "已完工",
  interrupted: "已中断",
  done: "已过站",
  stopped: "暂停待放行",
  failed: "故障",
  pending: "待上料",
};
const RUN_LABEL: Record<string, string> = {
  running: "生产中",
  completed: "已完工",
  interrupted: "已中断",
};

function shortTime(value?: string) {
  return value ? value.replace("T", " ").slice(0, 19) : "-";
}

async function factoryGet<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include", cache: "no-store" });
  if (res.status === 401) {
    window.location.assign("/app/login");
    throw new Error("登录已过期");
  }
  return (await res.json()) as T;
}

export function FactoryPage() {
  const { data: identity } = useGetIdentity<OpcUser>();
  const isBoss = String(identity?.role ?? "").includes("老板");

  const [data, setData] = useState<FactoryData | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [selectedNo, setSelectedNo] = useState<string>("");   // 空 = 全部车间
  const [stepOpen, setStepOpen] = useState<{ traceId: string; stageId: string; name: string } | null>(null);
  const [stepDetail, setStepDetail] = useState<StepDetail | null>(null);
  const [stepLoading, setStepLoading] = useState(false);
  const [resuming, setResuming] = useState<string>("");
  const [resumeMsg, setResumeMsg] = useState("");

  const load = useCallback(() => {
    factoryGet<FactoryData>("/api/factory")
      .then((d) => {
        setData(d);
        setLoadErr("");
      })
      .catch((e) => setLoadErr(String(e.message || e)));
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  const workshops = data?.workshops ?? [];
  const products = data?.products ?? [];
  const shownProducts = useMemo(
    () => (selectedNo ? products.filter((p) => p.workshopNo === selectedNo) : products),
    [products, selectedNo],
  );
  /* 工厂生命周期：CCO 审批进厂 → 加工 → 质检审核通过出厂（流水结束，转入历史） */
  const producingCount = products.filter((p) => !p.shippedAt).length;
  const shippedCount = products.filter((p) => !!p.shippedAt).length;
  const monitorProducts = shownProducts.filter((p) => !p.shippedAt);
  const historyProducts = shownProducts.filter((p) => !!p.shippedAt);
  const [showHistory, setShowHistory] = useState(true);
  const selectedWorkshop = workshops.find((w) => w.no === selectedNo);

  const openStep = (traceId: string, stageId: string, name: string) => {
    setStepOpen({ traceId, stageId, name });
    setStepDetail(null);
    setStepLoading(true);
    factoryGet<StepDetail>(`/api/factory/step/${encodeURIComponent(traceId)}/${encodeURIComponent(stageId)}`)
      .then(setStepDetail)
      .catch((e) => setStepDetail({ ok: false, error: String(e.message || e) }))
      .finally(() => setStepLoading(false));
  };

  const resume = async (traceId: string) => {
    setResuming(traceId);
    setResumeMsg("");
    try {
      const res = await fetch("/api/ops/recover", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "resume", traceId }),
      });
      const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
      if (!res.ok || body.ok === false) throw new Error(String(body.error || `HTTP ${res.status}`));
      setResumeMsg(`已下发续跑指令：从断点阶段恢复（trace ${traceId}）`);
      load();
    } catch (e) {
      setResumeMsg(`续跑失败：${(e as Error).message}`);
    } finally {
      setResuming("");
    }
  };

  // 横向工艺轴：按产品自己的工艺步骤序列（CCO 定工艺时定格）计算进度
  const axisProgress = (p: Product) => {
    const steps = p.craftSteps ?? [];
    let last = -1;
    steps.forEach((s, idx) => {
      if (s.status && s.status !== "pending" && idx > last) last = idx;
    });
    return steps.length ? Math.max(0, ((last + 1) / steps.length) * 100) : 0;
  };

  return (
    <section className="page fac-page">
      <div className="page-head">
        <div>
          <h1>制片工厂</h1>
          <p className="muted home-sub">
            全厂 13 个车间对应 OpenMontage 的 13 类工艺；每个工艺有各自的工序序列与名称
            （CCO 审批定工艺时即定格）。流水线生命周期：内容单经 CCO 审批通过发放流水号
            （WO-YYYYMMDD-NNN）进厂，按所属工艺的工序逐站加工，质检审核通过即出厂、流水结束——
            出厂后转入「出厂历史」，当前监控只显示在制流水线；进厂前的选题调度与出厂后的
            发布/归档由 hermes 编排，不属工厂工序。
          </p>
        </div>
        <div className="fac-meta">
          <span className="live-badge" title="每 3 秒自动刷新">
            <i aria-hidden="true" />实时监控
          </span>
          <span className="muted">
            在制 {producingCount} · 已出厂 {shippedCount}
          </span>
        </div>
      </div>

      {loadErr && <div className="fac-error">监控数据加载失败：{loadErr}</div>}
      {resumeMsg && <div className="fac-note">{resumeMsg}</div>}

      {/* 顶部：流水线监控（横向工艺轴） */}
      <div className="fac-monitors">
        <div className="fac-section-head">
          <h2>
            流水线监控
            <span className="fac-section-tag">
              {selectedWorkshop
                ? `${selectedWorkshop.no}号车间 · ${selectedWorkshop.name}`
                : "全部车间"}
            </span>
          </h2>
          <div className="fac-section-ops">
            {selectedNo && (
              <button type="button" className="btn btn-ghost fac-btn" onClick={() => setSelectedNo("")}>
                显示全部车间
              </button>
            )}
            <span className="muted">在制 {monitorProducts.length} 条流水线</span>
          </div>
        </div>

        {monitorProducts.length === 0 ? (
          <div className="fac-empty">
            {selectedNo
              ? "该车间暂无在制流水线。点击下方其他车间查看，或选择「显示全部车间」。"
              : "当前无在制流水线。内容单经 CCO 审批进厂后在这里实时监控，"
                + "质检通过出厂后转入下方「出厂历史」。"}
          </div>
        ) : (
          monitorProducts.map((p) => {
            const w = workshops.find((x) => x.no === p.workshopNo);
            return (
              <div key={p.itemId || p.traceId} className="fac-product">
                <div className="fac-product-head">
                  <div className="fac-product-title">
                    <b>{p.serialNo ? `${p.serialNo} · ${p.title}` : p.title}</b>
                    <span className={`fac-run ${p.shippedAt ? "s-shipped" : `s-${p.run.status}`}`}>
                      {p.shippedAt ? "已出厂" : (RUN_LABEL[p.run.status] || p.run.status)}
                    </span>
                  </div>
                  <div className="fac-product-meta muted">
                    {w ? `${w.no}号车间` : p.workshopNo} · 工艺：{p.craftName} · 出片模式：{p.renderMode || "-"}
                    · 决策：{p.decidedBy} · {shortTime(p.decidedAt)} · trace {p.traceId}
                    {p.shippedAt && ` · 出厂：${shortTime(p.shippedAt)}`}
                  </div>
                  <div className="fac-product-reason" title={p.reason}>CCO 决策依据：{p.reason}</div>
                  <div className="fac-product-ctl">
                    {isBoss && p.run.status !== "completed" && p.run.stage && (
                      <button
                        type="button"
                        className="btn btn-ghost fac-btn"
                        disabled={resuming === p.traceId}
                        onClick={() => resume(p.traceId)}
                      >
                        {resuming === p.traceId ? "下发中…" : "续跑此单"}
                      </button>
                    )}
                  </div>
                </div>

                {/* 横向工艺轴：图形 + 动画 */}
                <div className="fac-axis-wrap">
                  <div className="fac-axis-track">
                    <div
                      className="fac-axis-fill"
                      style={{ width: `${axisProgress(p)}%` }}
                      aria-hidden="true"
                    />
                  </div>
                  <div className="fac-axis">
                    {(p.craftSteps ?? []).map((cs, idx) => {
                      const status = cs.status || "pending";
                      return (
                        <button
                          key={`${cs.id}-${idx}`}
                          type="button"
                          className={`fac-node n-${status}${status === "running" ? " pulse" : ""}`}
                          title={`${cs.name} · ${STATUS_LABEL[status] || status}`}
                          onClick={() => openStep(p.traceId, cs.opcStage, cs.name)}
                        >
                          <span className="fac-node-icon" aria-hidden="true">
                            {status === "done" ? (
                              <svg viewBox="0 0 16 16" width="12" height="12">
                                <path d="M2.5 8.5 6 12l7.5-8" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                              </svg>
                            ) : status === "failed" ? (
                              <svg viewBox="0 0 16 16" width="12" height="12">
                                <path d="M3 3l10 10M13 3 3 13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                              </svg>
                            ) : status === "running" ? (
                              <svg viewBox="0 0 16 16" width="12" height="12" className="fac-spin">
                                <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray="9 5" />
                              </svg>
                            ) : (
                              <span className="fac-node-num">{idx + 1}</span>
                            )}
                          </span>
                          <span className="fac-node-name">{cs.name}</span>
                          {cs.omStage && <span className="fac-node-om">OM·{cs.omStage}</span>}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* 出厂历史：质检通过即出厂，流水结束，只在这里可查 */}
      {historyProducts.length > 0 && (
        <div className="fac-monitors">
          <div className="fac-section-head">
            <h2>
              出厂历史
              <span className="fac-section-tag">{historyProducts.length} 件</span>
            </h2>
            <div className="fac-section-ops">
              <button
                type="button"
                className="btn btn-ghost fac-btn"
                onClick={() => setShowHistory((v) => !v)}
              >
                {showHistory ? "收起" : "展开"}
              </button>
            </div>
          </div>
          {showHistory && (
            <div className="card">
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>流水号</th>
                      <th>名称</th>
                      <th>车间 / 工艺</th>
                      <th>进厂</th>
                      <th>出厂</th>
                      <th>trace</th>
                    </tr>
                  </thead>
                  <tbody>
                    {historyProducts.map((p) => (
                      <tr key={`his-${p.itemId || p.traceId}`}>
                        <td className="nowrap mono">{p.serialNo}</td>
                        <td>{p.title}</td>
                        <td className="nowrap">
                          {p.workshopNo ? `${p.workshopNo}号车间 · ${p.craftName}` : "-"}
                        </td>
                        <td className="nowrap">{shortTime(p.decidedAt)}</td>
                        <td className="nowrap">{shortTime(p.shippedAt)}</td>
                        <td className="nowrap mono">{p.traceId}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 底部：车间清单（横排列表） */}
      <div className="fac-list" role="list" aria-label="车间清单">
        <div className="fac-row fac-row-head" aria-hidden="true">
          <span>车间</span>
          <span>工艺</span>
          <span>产品</span>
          <span>工艺说明</span>
          <span>流水线</span>
          <span>状态</span>
        </div>
        {workshops.map((w) => {
          const items = products.filter((p) => p.workshopNo === w.no);
          const busy = items.some((p) => !p.shippedAt && p.run.status === "running");
          const done = items.filter((p) => !!p.shippedAt).length;
          const isActive = selectedNo === w.no;
          return (
            <button
              key={w.id}
              type="button"
              role="listitem"
              className={`fac-row${isActive ? " is-active" : ""}${busy ? " is-busy" : ""}`}
              onClick={() => setSelectedNo(isActive ? "" : w.no)}
              title={`点击${isActive ? "显示全部车间" : `只看 ${w.no} 号车间的流水线`}`}
            >
              <span className="fac-row-no">{w.no}号车间</span>
              <span className="fac-row-name">{w.name}</span>
              <span className="fac-row-product">{w.product}</span>
              <span className="fac-row-desc">{w.desc}</span>
              <span className="fac-row-count">
                <i className={`fac-dot${busy ? " blink" : ""}`} aria-hidden="true" />
                {items.length
                  ? `${items.filter((p) => !p.shippedAt).length} 在制 · ${done} 已出厂`
                  : "空闲"}
              </span>
              <span className="fac-row-state">
                <span className={`fac-gear${busy ? " spin" : ""}`} aria-hidden="true">
                  <svg viewBox="0 0 24 24" width="15" height="15">
                    <path
                      fill="currentColor"
                      d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm0 2.4a1.6 1.6 0 1 1 0 3.2 1.6 1.6 0 0 1 0-3.2ZM10.6 2h2.8l.4 2.4c.6.2 1.2.5 1.7.9l2.3-.9 1.4 2.4-1.8 1.6c.1.6.1 1.2 0 1.8l1.8 1.6-1.4 2.4-2.3-.9c-.5.4-1.1.7-1.7.9l-.4 2.4h-2.8l-.4-2.4a6 6 0 0 1-1.7-.9l-2.3.9-1.4-2.4 1.8-1.6a6 6 0 0 1 0-1.8L4.8 6.8l1.4-2.4 2.3.9c.5-.4 1.1-.7 1.7-.9L10.6 2Z"
                    />
                  </svg>
                </span>
                {busy ? "加工中" : items.length ? "已出厂" : "空闲"}
              </span>
            </button>
          );
        })}
      </div>

      {/* 工位详情弹窗 */}
      {stepOpen && (
        <div className="fac-modal" role="dialog" aria-modal="true" onClick={() => setStepOpen(null)}>
          <div className="fac-modal-body" onClick={(e) => e.stopPropagation()}>
            <div className="fac-modal-head">
              <h3>
                {stepOpen.name} · 处理过程
                <small className="muted"> trace {stepOpen.traceId}</small>
              </h3>
              <button type="button" className="btn btn-ghost" onClick={() => setStepOpen(null)}>
                关闭
              </button>
            </div>
            {stepLoading && <div className="fac-empty">加载中…</div>}
            {!stepLoading && stepDetail && !stepDetail.ok && (
              <div className="fac-error">{stepDetail.error || "详情加载失败"}</div>
            )}
            {!stepLoading && stepDetail?.ok && (
              <>
                <div className="fac-step-info">
                  <span className={`fac-run s-${stepDetail.step?.status}`}>
                    {STATUS_LABEL[stepDetail.step?.status || ""] || stepDetail.step?.status}
                  </span>
                  <span className="muted">
                    开工 {shortTime(stepDetail.step?.started_at)} · 完工{" "}
                    {shortTime(stepDetail.step?.finished_at) || "进行中"}
                    {stepDetail.step?.detail?.items != null && ` · 涉及内容单 ${stepDetail.step.detail.items} 条`}
                    {stepDetail.step?.om_stage && ` · OpenMontage 阶段 ${stepDetail.step.om_stage}`}
                  </span>
                </div>
                <div className="fac-events">
                  {(() => {
                    const all = stepDetail.events ?? [];
                    if (all.length === 0) return <div className="fac-empty">该工位暂无事件记录</div>;
                    const tail = all.slice(-80);
                    return (
                      <>
                        {all.length > tail.length && (
                          <div className="muted fac-event-more">
                            共 {all.length} 条事件，展示最近 {tail.length} 条
                          </div>
                        )}
                        {tail.map((ev) => (
                          <div key={ev.id} className="fac-event">
                            <span className="fac-event-kind">{ev.kind}</span>
                            <span className="fac-event-at">{shortTime(ev.at)}</span>
                            <code className="fac-event-data">
                              {JSON.stringify(ev.data).slice(0, 260)}
                            </code>
                          </div>
                        ))}
                      </>
                    );
                  })()}
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
