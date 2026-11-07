import { useState } from "react";
import { useList, useOne } from "@refinedev/core";
import type { Hotspot } from "../types";

const PAGE_SIZE = 20;

export function HotspotsPage() {
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);

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
            placeholder="按标题 / 来源 / 标签搜索"
          />
          <button type="submit" className="btn">
            搜索
          </button>
        </form>
      </div>

      <div className="panel-grid">
        <div className="card">
          {list.isLoading ? (
            <p className="muted">加载中…</p>
          ) : list.isError ? (
            <p className="error">加载失败</p>
          ) : items.length === 0 ? (
            <p className="muted">暂无热点</p>
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
      </div>
    </section>
  );
}
