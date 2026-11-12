import { useCallback, useEffect, useMemo, useState } from "react";

type ChatThread = {
  id: string;
  source: string;
  title: string;
  participants?: string[];
  created_at?: string;
  last_at?: string;
  msg_count?: number;
};

type ChatMessage = {
  id: number;
  thread_id: string;
  source: string;
  modality: string;
  direction: string;
  sender_id?: string;
  sender_name?: string;
  content?: string;
  content_kind?: string;
  origin_table?: string;
  origin_id?: string;
  trace_id?: string;
  created_at?: string;
  ingested_at?: string;
};

type ChatStats = {
  bySource?: Array<{ source: string; n: number }>;
  byModality?: Array<{ source: string; modality: string; n: number }>;
  byDay?: Array<{ day: string; source: string; n: number }>;
};

const PAGE = 50;

const SOURCES = [
  { value: "", label: "全部来源" },
  { value: "feishu", label: "飞书" },
  { value: "app", label: "APP" },
  { value: "web", label: "Web" },
];

const DIRECTIONS = [
  { value: "", label: "全部方向" },
  { value: "inbound", label: "收到" },
  { value: "outbound", label: "发出" },
];

const MODALITIES = [
  { value: "", label: "全部模态" },
  { value: "text", label: "文字" },
  { value: "voice", label: "语音" },
  { value: "video", label: "视频" },
];

const SOURCE_LABEL: Record<string, string> = { feishu: "飞书", app: "APP", web: "Web" };
const MODALITY_LABEL: Record<string, string> = { text: "文字", voice: "语音", video: "视频" };
const KIND_LABEL: Record<string, string> = {
  text: "消息",
  transcript: "转写",
  task: "任务",
  command: "指令",
};

class ChatApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function chatGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { credentials: "include" });
  if (res.status === 401) {
    window.location.assign("/app/login");
    throw new ChatApiError(401, "登录已过期");
  }
  const body = (await res.json().catch(() => ({}))) as Record<string, any>;
  if (!res.ok || body?.ok === false) {
    throw new ChatApiError(res.status, String(body?.error || `HTTP ${res.status}`));
  }
  return body as T;
}

// 2026-11-12T11:16:41+08:00 -> 11-12 11:16
function fmtTime(value?: string): string {
  if (!value) return "-";
  const text = value.replace("T", " ");
  return text.length >= 16 ? text.slice(5, 16) : text;
}

export function MessagesPage() {
  const [source, setSource] = useState("");
  const [direction, setDirection] = useState("");
  const [modality, setModality] = useState("");
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");

  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [threadsLoading, setThreadsLoading] = useState(true);
  const [activeId, setActiveId] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [msgLoading, setMsgLoading] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const [stats, setStats] = useState<ChatStats | null>(null);
  const [fatal, setFatal] = useState<{ status: number; message: string } | null>(null);
  const [notice, setNotice] = useState("");
  const [exporting, setExporting] = useState(false);

  const handleError = useCallback((err: unknown) => {
    const e = err as ChatApiError;
    const status = typeof e?.status === "number" ? e.status : 0;
    // 403/503 说明整个页面不可用（非老板 / 过程库未启用），直接进入空态
    if (status === 403 || status === 503) {
      setFatal({ status, message: e.message });
      return;
    }
    setNotice(status === 0 ? String(e?.message || e) : `${e.message}（HTTP ${status}）`);
  }, []);

  const loadThreads = useCallback(async () => {
    try {
      const query = new URLSearchParams({ limit: "100" });
      if (source) query.set("source", source);
      const body = await chatGet<{ threads: ChatThread[] }>(`/api/chat/threads?${query}`);
      setThreads(body.threads ?? []);
    } catch (err) {
      handleError(err);
    } finally {
      setThreadsLoading(false);
    }
  }, [source, handleError]);

  const loadStats = useCallback(async () => {
    try {
      const body = await chatGet<{ stats: ChatStats }>("/api/chat/stats?days=7");
      setStats(body.stats ?? null);
    } catch {
      // 统计只是辅助信息，失败不打断消息浏览
    }
  }, []);

  useEffect(() => {
    setThreadsLoading(true);
    loadThreads();
  }, [loadThreads]);

  useEffect(() => {
    loadStats();
  }, [loadStats]);

  // 切换来源时，已选会话可能不属于新来源，直接回到「全部消息」
  useEffect(() => {
    setActiveId("");
  }, [source]);

  const msgQuery = useMemo(() => {
    const query = new URLSearchParams({ limit: String(PAGE) });
    if (activeId) query.set("thread", activeId);
    if (source) query.set("source", source);
    if (direction) query.set("direction", direction);
    if (modality) query.set("modality", modality);
    if (q.trim()) query.set("q", q.trim());
    return query.toString();
  }, [activeId, source, direction, modality, q]);

  useEffect(() => {
    let cancelled = false;
    setMsgLoading(true);
    chatGet<{ messages: ChatMessage[] }>(`/api/chat/messages?${msgQuery}`)
      .then((body) => {
        if (cancelled) return;
        const rows = body.messages ?? [];
        setMessages(rows);
        setHasMore(rows.length >= PAGE);
        setNotice("");
      })
      .catch((err) => {
        if (!cancelled) handleError(err);
      })
      .finally(() => {
        if (!cancelled) setMsgLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [msgQuery, handleError]);

  // 摄取新消息时后端会推 chat.message，这里只增量合并，不打断已翻页的历史
  useEffect(() => {
    let timer = 0;
    const es = new EventSource("/api/events");
    es.onmessage = (event) => {
      let env: any;
      try {
        env = JSON.parse(event.data);
      } catch {
        return;
      }
      if (env?.kind !== "chat.message") return;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        loadThreads();
        loadStats();
        chatGet<{ messages: ChatMessage[] }>(`/api/chat/messages?${msgQuery}`)
          .then((body) => {
            const rows = body.messages ?? [];
            if (!rows.length) return;
            setMessages((current) => {
              const seen = new Set(current.map((item) => item.id));
              const fresh = rows.filter((item) => !seen.has(item.id));
              if (!fresh.length) return current;
              return [...fresh, ...current].sort((a, b) => b.id - a.id);
            });
            if (rows.length >= PAGE) setHasMore(true);
          })
          .catch(() => {
            // 推送刷新失败时保持现状，页面仍有 30 秒一次的手动刷新入口
          });
      }, 600);
    };
    return () => {
      window.clearTimeout(timer);
      es.close();
    };
  }, [msgQuery, loadThreads, loadStats]);

  const loadMore = async () => {
    if (!messages.length || loadingMore) return;
    const before = messages[messages.length - 1].id;
    const query = new URLSearchParams(msgQuery);
    query.set("before", String(before));
    setLoadingMore(true);
    try {
      const body = await chatGet<{ messages: ChatMessage[] }>(`/api/chat/messages?${query}`);
      const rows = body.messages ?? [];
      setMessages((current) => [...current, ...rows]);
      setHasMore(rows.length >= PAGE);
    } catch (err) {
      handleError(err);
    } finally {
      setLoadingMore(false);
    }
  };

  const handleSearch = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setQ(input.trim());
  };

  const handleExport = async () => {
    setExporting(true);
    setNotice("");
    try {
      const res = await fetch("/api/chat/export?limit=20000", { credentials: "include" });
      const body = (await res.json().catch(() => ({}))) as Record<string, any>;
      if (!res.ok || body?.ok === false) {
        throw new Error(String(body?.error || `HTTP ${res.status}`));
      }
      const blob = new Blob([JSON.stringify(body.messages ?? [], null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `chat-messages-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setNotice(`导出失败：${(err as Error).message}`);
    } finally {
      setExporting(false);
    }
  };

  const activeThread = threads.find((item) => item.id === activeId) || null;
  const sourceCounts = useMemo(() => {
    const map = new Map<string, number>();
    for (const row of stats?.bySource ?? []) map.set(row.source, row.n);
    return map;
  }, [stats]);

  if (fatal) {
    return (
      <section className="page">
        <div className="page-head">
          <h1>消息中心</h1>
        </div>
        <div className="card">
          <p className="error">
            {fatal.status === 403 ? "仅「老板」角色可查看统一消息" : "过程数据库未启用，消息暂时不可查看"}
          </p>
          <p className="muted">
            {fatal.status === 403
              ? "当前账号没有消息查看权限，如需开通请联系管理员调整角色。"
              : `后端返回：${fatal.message}。请确认 OPC 过程库已启用后再刷新本页。`}
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <h1>消息中心</h1>
          <p className="muted home-sub">
            飞书 / APP / Web 三源统一只读视图 · 后端每 30 秒摄取，新消息实时推送
          </p>
        </div>
        <div className="chat-actions">
          <form className="search" onSubmit={handleSearch}>
            <input
              className="input"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="搜索消息内容（3 字以上走全文索引）"
            />
            <button type="submit" className="btn">
              搜索
            </button>
          </form>
          <button type="button" className="btn btn-ghost" onClick={handleExport} disabled={exporting}>
            {exporting ? "导出中…" : "导出 JSON"}
          </button>
        </div>
      </div>

      <div className="toolbar chat-filters">
        <label className="filter-field">
          <span className="muted">来源</span>
          <select className="input" value={source} onChange={(e) => setSource(e.target.value)}>
            {SOURCES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span className="muted">方向</span>
          <select className="input" value={direction} onChange={(e) => setDirection(e.target.value)}>
            {DIRECTIONS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span className="muted">模态</span>
          <select className="input" value={modality} onChange={(e) => setModality(e.target.value)}>
            {MODALITIES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        {sourceCounts.size > 0 && (
          <span className="pill-count">
            累计：飞书 {sourceCounts.get("feishu") ?? 0} · APP {sourceCounts.get("app") ?? 0} · Web{" "}
            {sourceCounts.get("web") ?? 0}
          </span>
        )}
        {q && (
          <button type="button" className="btn btn-ghost" onClick={() => { setInput(""); setQ(""); }}>
            清除搜索「{q}」
          </button>
        )}
      </div>

      {notice && <div className="banner error-banner">{notice}</div>}

      <div className="chat-grid">
        <aside className="card chat-threads">
          <div className="detail-head">
            <h2>会话（{threads.length}）</h2>
            {activeId && (
              <button type="button" className="btn btn-ghost" onClick={() => setActiveId("")}>
                全部
              </button>
            )}
          </div>
          {threadsLoading ? (
            <p className="muted">加载中…</p>
          ) : threads.length === 0 ? (
            <p className="muted">暂无会话</p>
          ) : (
            <ul className="thread-list">
              {threads.map((thread) => (
                <li
                  key={thread.id}
                  className={thread.id === activeId ? "thread-item active" : "thread-item"}
                  onClick={() => setActiveId((current) => (current === thread.id ? "" : thread.id))}
                >
                  <div className="thread-top">
                    <span className={`badge src-${thread.source}`}>
                      {SOURCE_LABEL[thread.source] ?? thread.source}
                    </span>
                    <span className="thread-title" title={thread.title}>
                      {thread.title || thread.id}
                    </span>
                  </div>
                  <div className="thread-meta">
                    <span className="thread-id">{thread.id}</span>
                    <span className="muted">
                      {thread.msg_count ?? 0} 条 · {fmtTime(thread.last_at)}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <div className="card chat-messages">
          <div className="detail-head">
            <h2>{activeThread ? activeThread.title || activeThread.id : "全部消息"}</h2>
            <span className="muted">
              已加载 {messages.length} 条{q ? ` · 搜索「${q}」` : ""}
            </span>
          </div>

          {msgLoading ? (
            <p className="muted">加载中…</p>
          ) : messages.length === 0 ? (
            <p className="muted">
              {q ? "没有匹配的消息，试试换关键词" : "暂无消息"}
            </p>
          ) : (
            <div className="msg-list">
              {messages.map((message) => (
                <article
                  key={message.id}
                  className={`msg msg-${message.direction === "outbound" ? "out" : "in"}`}
                >
                  <div className="msg-head">
                    <span className="msg-sender">{message.sender_name || message.sender_id || "未知"}</span>
                    <span className={`badge src-${message.source}`}>
                      {SOURCE_LABEL[message.source] ?? message.source}
                    </span>
                    <span className="badge">{message.direction === "outbound" ? "发出" : "收到"}</span>
                    {message.modality && message.modality !== "text" && (
                      <span className="badge badge-warn">
                        {MODALITY_LABEL[message.modality] ?? message.modality}
                      </span>
                    )}
                    {message.content_kind && message.content_kind !== "text" && (
                      <span className="badge badge-kind">
                        {KIND_LABEL[message.content_kind] ?? message.content_kind}
                      </span>
                    )}
                    <time className="msg-time muted" title={message.created_at || ""}>
                      {fmtTime(message.created_at)}
                    </time>
                  </div>
                  <div className="msg-body">{message.content || "（空消息）"}</div>
                  <div className="msg-foot">
                    <span className="thread-id">{message.thread_id}</span>
                    {message.ingested_at && message.ingested_at !== message.created_at && (
                      <span className="muted">入库 {fmtTime(message.ingested_at)}</span>
                    )}
                  </div>
                </article>
              ))}
            </div>
          )}

          <div className="pager">
            <span className="muted">按时间倒序，最新的在最上面</span>
            {hasMore && (
              <button type="button" className="btn btn-ghost" onClick={loadMore} disabled={loadingMore}>
                {loadingMore ? "加载中…" : "加载更早"}
              </button>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
