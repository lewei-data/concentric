import { lazy, Suspense, useEffect, useState } from "react";
import { useList, useOne } from "@refinedev/core";
import { useNavigate, useParams } from "react-router-dom";
import type { Work } from "../types";

// 档案正文是 Markdown，按需加载渲染器，避免拖大首屏
const MarkdownBody = lazy(() => import("../components/MarkdownBody"));

const PAGE_SIZE = 20;
const PROJECTS = [
  { id: "", name: "全部项目" },
  { id: "wenren", name: "产品一组" },
  { id: "gou", name: "产品二组" },
  { id: "tongxinyuan", name: "产品三组" },
];
/* 状态与制片工厂生命周期一致：出厂=质检审核通过 */
const STATUSES = ["", "生产中", "已出厂", "已完工", "已中断"];
const DOC_ORDER = ["topic", "copy", "storyboard", "assessment", "review"];

function shortTime(value?: string) {
  return value ? value.replace("T", " ").slice(0, 16) : "-";
}

export function WorksPage() {
  const { id } = useParams<{ id: string }>();
  return id ? <WorkDetailPage id={id} /> : <WorksListPage />;
}

/* ---------- 详情页：左侧文字介绍，右侧手机比例视频播放 ---------- */
function WorkDetailPage({ id }: { id: string }) {
  const navigate = useNavigate();
  const [docKey, setDocKey] = useState("topic");

  const detail = useOne<Work>({ resource: "works", id });
  const work = detail.data?.data;

  useEffect(() => {
    if (!work?.docs) return;
    if (!work.docs[docKey]) {
      const first = DOC_ORDER.find((key) => work.docs?.[key]);
      if (first) setDocKey(first);
    }
  }, [work, docKey]);

  const video = work?.files.find((f) => (f.key === "final" || f.key === "preview") && f.exists);
  const cover = work?.files.find((f) => f.key === "cover" && f.exists);

  return (
    <section className="page works-detail-page">
      <div className="work-back-row">
        <button type="button" className="btn btn-ghost" onClick={() => navigate("/works")}>
          ← 返回作品列表
        </button>
      </div>

      {detail.isLoading ? (
        <p className="muted">加载中…</p>
      ) : detail.isError ? (
        <p className="error">档案加载失败</p>
      ) : !work ? (
        <p className="muted">未找到该作品</p>
      ) : (
        <>
          <div className="page-head work-detail-head">
            <div>
              <div className="work-code mono">{work.code}</div>
              <h1>{work.title}</h1>
            </div>
            <span className={`status-pill status-${work.statusKey}`}>{work.status}</span>
          </div>

          <div className="work-detail-layout">
            {/* 左侧：文字介绍 */}
            <div className="card work-detail-main">
              <dl className="kv work-kv">
                <dt>项目</dt>
                <dd>{work.projectName}</dd>
                <dt>负责人</dt>
                <dd>{work.owner || "-"}</dd>
                <dt>阶段 / 状态</dt>
                <dd>{work.stageName} · {work.status}</dd>
                {work.serialNo && (
                  <>
                    <dt>流水号</dt>
                    <dd className="mono">{work.serialNo}</dd>
                    <dt>车间 / 工艺</dt>
                    <dd>{work.workshopNo}号车间 · {work.craftName}</dd>
                    <dt>CCO 审批</dt>
                    <dd>{shortTime(work.decidedAt)}</dd>
                    {work.shippedAt && (
                      <>
                        <dt>出厂时间</dt>
                        <dd>{shortTime(work.shippedAt)}</dd>
                      </>
                    )}
                  </>
                )}
                <dt>创建时间</dt>
                <dd>{shortTime(work.createdAt)}</dd>
                <dt>更新时间</dt>
                <dd>{shortTime(work.updatedAt)}</dd>
                <dt>完整度</dt>
                <dd>{work.completeness}% · 素材 {work.assetCount} 个</dd>
                <dt>Trace</dt>
                <dd className="mono">{work.traceId}</dd>
                <dt>内容 ID</dt>
                <dd className="mono">{work.contentId}</dd>
              </dl>
              {work.folder && (
                <p className="muted" style={{ marginTop: "-8px", marginBottom: "12px" }}>
                  <a href={work.folder} target="_blank" rel="noopener noreferrer">
                    打开 Obsidian 目录 ↗
                  </a>
                </p>
              )}

              {work.craftSteps && work.craftSteps.length > 0 && (
                <>
                  <h4 className="section-title">
                    工艺工序{work.craftProgress ? `（${work.craftProgress} 过站）` : ""}
                  </h4>
                  <div className="work-files">
                    {work.craftSteps.map((cs, idx) => (
                      <span
                        key={`${cs.id}-${idx}`}
                        className={`work-file${cs.status === "pending" ? " is-missing" : ""}`}
                        title={cs.status}
                      >
                        <span>{cs.status === "done" ? "✓" : cs.status === "pending" ? "×" : "…"}</span>
                        {cs.name}
                      </span>
                    ))}
                  </div>
                </>
              )}

              <h4 className="section-title">处理流程</h4>
              <ol className="work-timeline">
                {work.timeline.map((step) => (
                  <li key={step.key}>
                    <span className="timeline-label">{step.label}</span>
                    <span className="timeline-time">{shortTime(step.at)}</span>
                  </li>
                ))}
                {work.timeline.length === 0 && <li className="muted">暂无落盘记录</li>}
              </ol>

              <h4 className="section-title">文件清单</h4>
              <div className="work-files">
                {work.files.map((file) => (
                  <a
                    key={file.key}
                    className={`work-file${file.exists ? "" : " is-missing"}`}
                    href={file.exists ? file.url : undefined}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={file.exists ? `${file.label} · ${Math.round(file.size / 1024)} KB` : `${file.label} 未生成`}
                  >
                    <span>{file.exists ? "✓" : "×"}</span>
                    {file.label}
                  </a>
                ))}
              </div>

              <h4 className="section-title">评审与评估</h4>
              <div className="work-note">
                <div className="work-note-head">
                  <span>终审意见</span>
                  <span className={
                    work.reviewPass === null ? "muted" : work.reviewPass ? "ok-text" : "error"
                  }>
                    {work.reviewPass === null ? "未出结论" : work.reviewPass ? "通过" : "未通过"}
                  </span>
                </div>
                <p>{work.reviewNote || "暂无终审意见"}</p>
              </div>
              <div className="work-note">
                <div className="work-note-head"><span>评估结论</span></div>
                <p>{work.assessment || "暂无制作评估"}</p>
              </div>

              <h4 className="section-title">审批记录</h4>
              {work.approvals?.length ? (
                <ul className="work-approvals">
                  {work.approvals.map((approval, index) => (
                    <li key={approval.id || index}>
                      <span>{approval.title || approval.action || "审批"}</span>
                      <span className="muted">
                        {approval.status === "approved" ? "已通过" : approval.status === "rejected" ? "已驳回" : "待审批"}
                        {" · "}
                        {approval.approver || "-"}
                        {" · "}
                        {shortTime(approval.decidedAt || approval.createdAt)}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">本轮未产生审批记录</p>
              )}

              <h4 className="section-title">档案原文</h4>
              <div className="tabbar work-tabs">
                {DOC_ORDER.filter((key) => work.docs?.[key]).map((key) => (
                  <button
                    key={key}
                    type="button"
                    className={`chip-btn${docKey === key ? " is-active" : ""}`}
                    onClick={() => setDocKey(key)}
                  >
                    {work.docs?.[key]?.label}
                  </button>
                ))}
              </div>
              <div className="detail-body work-doc">
                {work.docs?.[docKey] ? (
                  <Suspense fallback={<span className="muted">正文渲染中…</span>}>
                    <MarkdownBody text={work.docs[docKey].text} />
                  </Suspense>
                ) : (
                  <span className="muted">该档案暂无文档</span>
                )}
              </div>
            </div>

            {/* 右侧：手机比例播放位（竖屏 9:16） */}
            <aside className="work-detail-side">
              {video ? (
                <div className="work-phone-frame">
                  <video
                    className="work-phone-video"
                    controls
                    preload="metadata"
                    poster={cover?.url}
                    src={video.url}
                  />
                </div>
              ) : cover ? (
                <div className="work-phone-frame">
                  <img className="work-phone-video" src={cover.url} alt={`${work.title} 封面`} />
                </div>
              ) : (
                <div className="work-phone-frame is-empty">暂无成片</div>
              )}
              <p className="muted work-side-caption">
                {video ? `${video.label} · ${Math.round(video.size / 1024 / 1024)} MB` : "尚未出片"}
              </p>
            </aside>
          </div>
        </>
      )}
    </section>
  );
}

/* ---------- 列表页 ---------- */
function WorksListPage() {
  const navigate = useNavigate();
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [project, setProject] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState("updated");
  const [page, setPage] = useState(1);

  const filters: { field: string; operator: "eq"; value: string }[] = [];
  if (q) filters.push({ field: "q", operator: "eq", value: q });
  if (project) filters.push({ field: "project", operator: "eq", value: project });
  if (status) filters.push({ field: "status", operator: "eq", value: status });
  if (sort !== "updated") filters.push({ field: "sort", operator: "eq", value: sort });

  const list = useList<Work>({
    resource: "works",
    pagination: { current: page, pageSize: PAGE_SIZE },
    filters,
    queryOptions: { refetchInterval: 30000 },
  });

  const items = list.data?.data ?? [];
  const total = list.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const resetPage = () => setPage(1);

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <h1>作品档案</h1>
          <p className="muted home-sub">
            以制片工厂过程库为准的作品档案：只收录经 CCO 审批进厂（有流水号）的作品，
            阶段/状态随工厂生命周期（生产中 → 质检审核通过出厂），每一步进展与车间数据一致。
          </p>
        </div>
        <form
          className="search"
          onSubmit={(event) => {
            event.preventDefault();
            resetPage();
            setQ(input.trim());
          }}
        >
          <input
            className="input"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="按名称 / 编号 / 负责人 / trace 搜索"
          />
          <button type="submit" className="btn">搜索</button>
        </form>
      </div>

      <div className="works-toolbar">
        <div className="chip-group">
          {PROJECTS.map((item) => (
            <button
              key={item.id || "all"}
              type="button"
              className={`chip-btn${project === item.id ? " is-active" : ""}`}
              onClick={() => {
                setProject(item.id);
                resetPage();
              }}
            >
              {item.name}
            </button>
          ))}
        </div>
        <div className="chip-group">
          {STATUSES.map((item) => (
            <button
              key={item || "all-status"}
              type="button"
              className={`chip-btn${status === item ? " is-active" : ""}`}
              onClick={() => {
                setStatus(item);
                resetPage();
              }}
            >
              {item || "全部状态"}
            </button>
          ))}
        </div>
        <div className="chip-group">
          {[
            { id: "updated", name: "按时间" },
            { id: "name", name: "按名称" },
          ].map((item) => (
            <button
              key={item.id}
              type="button"
              className={`chip-btn${sort === item.id ? " is-active" : ""}`}
              onClick={() => {
                setSort(item.id);
                resetPage();
              }}
            >
              {item.name}
            </button>
          ))}
        </div>
      </div>

      <div className="panel-grid">
        <div className="card">
          {list.isLoading ? (
            <p className="muted">加载中…</p>
          ) : list.isError ? (
            <p className="error">加载失败</p>
          ) : items.length === 0 ? (
            <p className="muted">暂无作品</p>
          ) : (
            <div className="table-wrap">
              <table className="table works-table">
                <thead>
                  <tr>
                    <th>编号</th>
                    <th>名称 / 阶段</th>
                    <th>项目</th>
                    <th>负责人</th>
                    <th>状态</th>
                    <th className="num">完整度</th>
                    <th>更新时间</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr
                      key={item.id}
                      onClick={() => navigate(`/works/${encodeURIComponent(item.id)}`)}
                    >
                      <td className="nowrap mono">{item.code}</td>
                      <td>
                        <div className="title-cell">{item.title}</div>
                        <div className="summary">
                          {item.workshopNo
                            ? `${item.workshopNo}号车间 · ${item.craftName}`
                            : item.stageName}
                          {item.craftProgress ? ` · 工序 ${item.craftProgress}` : ""}
                          {item.hasFinal ? " · 已有最终成片" : item.hasPreview ? " · 已有预览样片" : ""}
                        </div>
                      </td>
                      <td className="nowrap">{item.projectName}</td>
                      <td className="nowrap">{item.owner || "-"}</td>
                      <td className="nowrap">
                        <span className={`status-dot status-${item.statusKey}`} />
                        {item.status}
                      </td>
                      <td className="num">{item.completeness}%</td>
                      <td className="nowrap">{shortTime(item.updatedAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="pager">
            <button
              type="button"
              className="btn btn-ghost"
              disabled={page <= 1}
              onClick={() => setPage((value) => Math.max(1, value - 1))}
            >
              上一页
            </button>
            <span className="muted">
              第 {page} / {totalPages} 页 · 共 {total} 件作品
            </span>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={page >= totalPages}
              onClick={() => setPage((value) => Math.min(totalPages, value + 1))}
            >
              下一页
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
