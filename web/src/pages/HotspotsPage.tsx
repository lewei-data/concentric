import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useList, useOne } from "@refinedev/core";
import type { Hotspot } from "../types";

const PAGE_SIZE = 20;

type FeedItem = {
  id: number;
  source: string;
  title: string;
  url: string;
  rank: number;
  score: number;
  extra: Record<string, unknown>;
  collectedAt: string;
};
type FeedSource = {
  id: string;
  name: string;
  category: string;
  live: boolean;
  note?: string;
  count: number;
  lastRun: string;
  ok: boolean;
  error: string;
};
type FeedResp = {
  ok: boolean;
  sources: FeedSource[];
  items: FeedItem[];
  subscribed: string[];
};

export function HotspotsPage() {
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // 工作流热点卡（原有：AI 员工产出的业务热点）
  const list = useList<Hotspot>({
    resource: "hotspots",
    pagination: { current: page, pageSize: PAGE_SIZE },
    filters: q ? [{ field: "q", operator: "eq", value: q }] : [],
    // 15 秒自动重取，界面跟着 OPC 里落盘的数据走（后端每 60 秒同步一次）
    queryOptions: { refetchInterval: 15000 },
  });

  const detail = useOne<Hotspot>({
    resource: "hotspots",
    id: selectedId ?? "",
    queryOptions: { enabled: selectedId !== null },
  });

  // 共享热点消息流（系统定时采集，公用区域，全用户可见）
  const [feed, setFeed] = useState<FeedResp | null>(null);
  const [feedSource, setFeedSource] = useState<string>("all");
  const [feedErr, setFeedErr] = useState("");

  const loadFeed = async () => {
    try {
      const res = await fetch("/api/hotspot/feed", { credentials: "include" });
      const body = (await res.json()) as FeedResp;
      setFeed(body);
      setFeedErr("");
    } catch (e) {
      setFeedErr(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    void loadFeed();
    const t = setInterval(() => void loadFeed(), 60000);
    return () => clearInterval(t);
  }, []);

  const subscribed = feed?.subscribed || [];
  const sourceById = new Map((feed?.sources || []).map((s) => [s.id, s]));
  const feedItems = (feed?.items || []).filter((i) => {
    if (feedSource !== "all") return i.source === feedSource;
    // 未订阅时显示全部；有订阅默认只显示订阅源
    if (!subscribed.length) return true;
    return subscribed.includes(i.source);
  });

  const items = list.data?.data ?? [];
  const total = list.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const handleSearch = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPage(1);
    setQ(input.trim());
  };

  return (
    <section className="page">
      <div className="page-head">
        <h1>热点</h1>
        <form className="search" onSubmit={handleSearch}>
          <input
            className="input"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="搜索工作流热点卡：标题 / 来源 / 标签"
          />
          <button type="submit" className="btn">
            搜索
          </button>
        </form>
      </div>

      {/* 共享热点消息流：系统定时采集（抖音 / 快手 / GitHub / HN 等），全用户可见 */}
      <div className="card">
        <div className="detail-head">
          <h2>热点快讯 <small className="muted">（系统定时采集 · 全用户共享 · 30 分钟一轮）</small></h2>
          <button type="button" className="btn btn-ghost" onClick={() => void loadFeed()}>
            刷新
          </button>
        </div>
        <p className="muted">
          我的订阅：
          {subscribed.length
            ? subscribed.map((s) => sourceById.get(s)?.name || s).join("、")
            : "未订阅（显示全部源）"}
          —— <Link to="/account/settings">到「系统配置」修改订阅方向 →</Link>
        </p>
        {feedErr && <p className="error">热点流加载失败：{feedErr}</p>}
        <div className="chip-row" role="tablist" aria-label="热点源筛选">
          <button
            type="button"
            className={`chip-btn ${feedSource === "all" ? "active" : ""}`}
            onClick={() => setFeedSource("all")}
          >
            全部
          </button>
          {(feed?.sources || []).map((s) => (
            <button
              key={s.id}
              type="button"
              className={`chip-btn ${feedSource === s.id ? "active" : ""}`}
              onClick={() => setFeedSource(s.id)}
              title={s.live ? `最近采集 ${s.lastRun || "—"}` : s.error || "待接入"}
            >
              {s.name}{!s.live && "（待接入）"} · {s.count}
            </button>
          ))}
        </div>
        {feedItems.length === 0 ? (
          <p className="muted">
            {feedSource !== "all"
              ? "该源暂无数据（等待下一轮采集）。"
              : "热点流暂无数据（采集器刚启动，稍后自动补充）。"}
          </p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th className="num">#</th>
                  <th>标题</th>
                  <th>来源</th>
                  <th className="num">热度</th>
                  <th>采集时间</th>
                </tr>
              </thead>
              <tbody>
                {feedItems.map((i) => (
                  <tr key={`${i.source}-${i.id}`}>
                    <td className="num">{i.rank}</td>
                    <td className="title-cell">
                      {i.url
                        ? <a href={i.url} target="_blank" rel="noreferrer">{i.title}</a>
                        : i.title}
                    </td>
                    <td>{sourceById.get(i.source)?.name || i.source}</td>
                    <td className="num">{i.score}</td>
                    <td className="nowrap">{(i.collectedAt || "").replace("T", " ").slice(5, 16)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 工作流热点卡（AI 员工产出，按用户隔离） */}
      <div className="card">
        <h2>工作流热点卡 <small className="muted">（AI 员工采集与复核产出，按用户隔离）</small></h2>
        {list.isLoading ? (
          <p className="muted">加载中…</p>
        ) : list.isError ? (
          <p className="error">加载失败</p>
        ) : items.length === 0 ? (
          <p className="muted">暂无热点卡</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>标题</th>
                  <th>来源</th>
                  <th className="num">评分</th>
                  <th>标签</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr
                    key={item.id}
                    className={selectedId === item.id ? "row-active" : ""}
                    onClick={() => setSelectedId(item.id)}
                  >
                    <td className="title-cell">{item.title}</td>
                    <td>{item.source || "-"}</td>
                    <td className="num">
                      {typeof item.score === "number" ? item.score.toFixed(2) : "-"}
                    </td>
                    <td>{item.tags?.length ? item.tags.join(" / ") : "-"}</td>
                    <td className="nowrap">{item.at || "-"}</td>
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
            第 {page} / {totalPages} 页 · 共 {total} 条
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

      {selectedId !== null && (
        <aside className="card detail">
          <div className="detail-head">
            <h2>热点详情</h2>
            <button type="button" className="btn btn-ghost" onClick={() => setSelectedId(null)}>
              关闭
            </button>
          </div>
          {detail.isLoading ? (
            <p className="muted">加载中…</p>
          ) : detail.isError ? (
            <p className="error">详情加载失败</p>
          ) : !detail.data?.data ? (
            <p className="muted">未找到该热点</p>
          ) : (
            <dl className="kv">
              <dt>标题</dt>
              <dd>{detail.data.data.title}</dd>
              <dt>来源</dt>
              <dd>{detail.data.data.source || "-"}</dd>
              <dt>评分</dt>
              <dd>
                {typeof detail.data.data.score === "number"
                  ? detail.data.data.score.toFixed(2)
                  : "-"}
              </dd>
              <dt>标签</dt>
              <dd>{detail.data.data.tags?.length ? detail.data.data.tags.join(" / ") : "-"}</dd>
              <dt>时间</dt>
              <dd>{detail.data.data.at || "-"}</dd>
              <dt>Trace</dt>
              <dd>{detail.data.data.traceId || "-"}</dd>
            </dl>
          )}
        </aside>
      )}
    </section>
  );
}
