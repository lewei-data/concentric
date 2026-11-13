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
interface StageAxis {
  id: string;
  name: string;
  kind: string;
  omStage: string;
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
  run: { status: string; stage: string; stageIndex: number; updatedAt: string; closedAt: string };
  steps: StepView[];
}
interface FactoryData {
  ok: boolean;
  workshops: Workshop[];
  stages: StageAxis[];
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

const KIND_LABEL: Record<string, string> = {
  produce: "加工",
  gate: "质检",
  approval: "审批",
  terminal: "收口",
};
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
  const [selectedNo, setSelectedNo] = useState<string>("");
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
        setSelectedNo((cur) => {
          if (cur) return cur;
          const filled = d.products.find((p) => p.run.status === "running") || d.products[0];
          return filled?.workshopNo ?? "01";
        });
      })
      .catch((e) => setLoadErr(String(e.message || e)));
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  const workshops = data?.workshops ?? [];
  const stages = useMemo(() => data?.stages ?? [], [data]);
  const products = data?.products ?? [];
  const workshopProducts = useMemo(
    () => products.filter((p) => p.workshopNo === selectedNo),
    [products, selectedNo],
  );

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

  // 横向轴进度：最后一个非 pending 工位占比
  const axisProgress = (p: Product) => {
    let last = -1;
    p.steps.forEach((s) => {
      const idx = stages.findIndex((st) => st.id === s.stage_id);
      if (idx > last) last = idx;
    });
    return stages.length ? Math.max(0, ((last + 1) / stages.length) * 100) : 0;
  };

  const nodeStatus = (p: Product, stageId: string): string => {
    const step = p.steps.find((s) => s.stage_id === stageId);
    if (step) return step.status;
    return "pending";
  };

  return (
    <section className="page fac-page">
      <div className="page-head">
        <div>
          <h1>制片工厂</h1>
          <p className="muted home-sub">
            以 OpenMontage 的 13 类工艺划分车间；每个产品的制作工艺由 CCO 确定并记录到过程库，
            横向工艺轴实时反映产品的加工过程，点击任一环节可查看详细处理记录。
          </p>
        </div>
        <div className="fac-meta">
          <span className="live-badge" title="每 3 秒自动刷新">
            <i aria-hidden="true" />实时监控
          </span>
          <span className="muted">
            在制 {products.filter((p) => p.run.status === "running").length} · 已完工{" "}
            {products.filter((p) => p.run.status === "completed").length}
          </span>
        </div>
      </div>

      {loadErr && <div className="fac-error">监控数据加载失败：{loadErr}</div>}
      {resumeMsg && <div className="fac-note">{resumeMsg}</div>}

      {/* 车间总览：13 类工艺车间 */}
      <div className="fac-board" role="list" aria-label="车间总览">
        {workshops.map((w) => {
          const items = products.filter((p) => p.workshopNo === w.no);
          const busy = items.some((p) => p.run.status === "running");
          const done = items.length > 0 && items.every((p) => p.run.status === "completed");
          return (
            <button
              key={w.id}
              type="button"
              role="listitem"
              className={`fac-card${selectedNo === w.no ? " is-active" : ""}${busy ? " is-busy" : ""}${done ? " is-done" : ""}`}
              onClick={() => setSelectedNo(w.no)}
            >
              <div className="fac-card-top">
                <span className="fac-no">{w.no}号车间</span>
                <span className={`fac-gear${busy ? " spin" : ""}`} aria-hidden="true">
                  <svg viewBox="0 0 24 24" width="16" height="16">
                    <path
                      fill="currentColor"
                      d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm0 2.4a1.6 1.6 0 1 1 0 3.2 1.6 1.6 0 0 1 0-3.2ZM10.6 2h2.8l.4 2.4c.6.2 1.2.5 1.7.9l2.3-.9 1.4 2.4-1.8 1.6c.1.6.1 1.2 0 1.8l1.8 1.6-1.4 2.4-2.3-.9c-.5.4-1.1.7-1.7.9l-.4 2.4h-2.8l-.4-2.4a6 6 0 0 1-1.7-.9l-2.3.9-1.4-2.4 1.8-1.6a6 6 0 0 1 0-1.8L4.8 6.8l1.4-2.4 2.3.9c.5-.4 1.1-.7 1.7-.9L10.6 2Z"
                    />
                  </svg>
                </span>
              </div>
              <div className="fac-card-name">{w.name}</div>
              <div className="fac-card-product">{w.product}</div>
              <div className="fac-card-desc" title={w.desc}>{w.desc}</div>
              <div className="fac-card-items">
                {items.length === 0 ? (
                  <span className="muted">暂无在制产品</span>
                ) : (
                  items.slice(0, 3).map((p) => (
                    <span key={p.itemId} className={`fac-chip s-${p.run.status}`} title={p.title}>
                      <i className="fac-dot" aria-hidden="true" />
                      {p.title.length > 14 ? `${p.title.slice(0, 14)}…` : p.title}
                    </span>
                  ))
                )}
                {items.length > 3 && <span className="muted">+{items.length - 3}</span>}
              </div>
            </button>
          );
        })}
      </div>

      {/* 选中车间：产品横向工艺轴 */}
      {selectedNo && (
        <div className="fac-detail">
          {(() => {
            const w = workshops.find((x) => x.no === selectedNo);
            if (!w) return null;
            return (
              <div className="fac-detail-head">
                <h2>
                  <span className="fac-no big">{w.no}号车间</span>
                  {w.name} · {w.product}
                </h2>
                <span className="muted">{w.desc}</span>
              </div>
            );
          })()}

          {workshopProducts.length === 0 ? (
            <div className="fac-empty">
              该车间暂无在制产品。产品的制作工艺由 CCO 在「制作评估」环节参考 13 类工艺选定，
              选定后自动进入对应车间加工。
            </div>
          ) : (
            workshopProducts.map((p) => (
              <div key={p.itemId} className="fac-product">
                <div className="fac-product-head">
                  <div className="fac-product-title">
                    <b>{p.title}</b>
                    <span className={`fac-run s-${p.run.status}`}>{RUN_LABEL[p.run.status] || p.run.status}</span>
                  </div>
                  <div className="fac-product-meta muted">
                    工艺：{p.craftName}（{p.craftId}）· 出片模式：{p.renderMode || "-"} · 决策：{p.decidedBy} ·{" "}
                    {shortTime(p.decidedAt)} · trace {p.traceId}
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
                    {stages.map((st, idx) => {
                      const status = nodeStatus(p, st.id);
                      const step = p.steps.find((s) => s.stage_id === st.id);
                      return (
                        <button
                          key={st.id}
                          type="button"
                          className={`fac-node n-${status}${status === "running" ? " pulse" : ""}`}
                          title={`${st.name} · ${STATUS_LABEL[status] || status}`}
                          onClick={() => openStep(p.traceId, st.id, st.name)}
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
                          <span className="fac-node-name">{st.name}</span>
                          <span className="fac-node-kind">{KIND_LABEL[st.kind] || st.kind}</span>
                          {step?.om_stage && <span className="fac-node-om">OM·{step.om_stage}</span>}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      )}

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
