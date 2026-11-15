import { useCallback, useEffect, useRef, useState } from "react";
import { siteUrl } from "../lib/siteUrl";

/**
 * 素材「自动采集」的定时任务设置页（页内弹层）。
 *
 * 一个任务 = 周期 + 数据源 + 目标数量 + 采集任务描述。
 * 到点后由 hermes 的 cco 角色执行：先把描述读懂再采集，输出到 users/<手机号>/downloads；
 * 采完自动做一轮 CCO 分析（结合公司知识库）→「创意灵感 → 采集洞察」，选题时优先采用。
 */

type Task = {
  id: string;
  title: string;
  sources: string;
  sourcesList?: string[];
  schedule: string;
  scheduleLabel?: string;
  target_count: number;
  brief: string;
  enabled: number;
  status: string;
  last_run: string;
  last_note: string;
  next_run: string;
  updated_at?: string;
};
type Source = { key: string; label: string };
type Run = {
  id: string;
  task_id: string;
  started_at: string;
  finished_at: string;
  ok: number;
  file_count: number;
  idea_count: number;
  out_dir: string;
  note: string;
};

const FALLBACK_SOURCES: Source[] = [
  { key: "douyin", label: "抖音短视频" },
  { key: "xiaohongshu", label: "小红书图文" },
  { key: "bilibili", label: "B 站视频" },
  { key: "wechat", label: "公众号文章" },
  { key: "article", label: "网页文章 / 新闻" },
  { key: "own_account", label: "自己账号的内容" },
];

const STATUS_TEXT: Record<string, string> = {
  idle: "待运行",
  running: "采集中",
  ok: "已完成",
  error: "失败",
};

function emptyDraft(): Partial<Task> {
  return {
    title: "",
    sources: "douyin,xiaohongshu,article",
    schedule: "daily@09:00",
    target_count: 20,
    brief: "",
    enabled: 1,
  };
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(siteUrl(url), { credentials: "include" });
  if (res.status === 401) {
    window.location.assign(siteUrl("/app/login"));
    throw new Error("unauthorized");
  }
  return (await res.json()) as T;
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(siteUrl(url), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (res.status === 401) {
    window.location.assign(siteUrl("/app/login"));
    throw new Error("unauthorized");
  }
  return (await res.json()) as T;
}

/** daily@09:30 ↔ {kind:"daily", hh:9, mm:30}；every@6 ↔ {kind:"every", n:6} */
function splitSchedule(schedule?: string): { kind: "daily" | "every"; hh: string; mm: string; n: string } {
  const raw = String(schedule || "daily@09:00");
  let m = /^daily@(\d{1,2}):(\d{2})$/.exec(raw);
  if (m) {
    return { kind: "daily", hh: (m[1] ?? "09").padStart(2, "0"), mm: m[2] ?? "00", n: "6" };
  }
  m = /^(?:every|interval)@(\d{1,3})$/.exec(raw);
  if (m) return { kind: "every", hh: "09", mm: "00", n: m[1] ?? "6" };
  return { kind: "daily", hh: "09", mm: "00", n: "6" };
}

function joinSchedule(kind: "daily" | "every", hh: string, mm: string, n: string): string {
  if (kind === "every") return `every@${Math.max(1, Math.min(168, Number(n) || 6))}`;
  return `daily@${String(Number(hh) || 0).padStart(2, "0")}:${String(Number(mm) || 0).padStart(2, "0")}`;
}

function fmtTime(s?: string): string {
  if (!s) return "—";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function CollectTasks({ onClose, onRan }: { onClose: () => void; onRan?: () => void }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [sources, setSources] = useState<Source[]>(FALLBACK_SOURCES);
  const [draft, setDraft] = useState<Partial<Task> | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const pollRef = useRef<number | null>(null);

  const load = useCallback(async (keepMsg = true) => {
    try {
      const r = await getJson<{ ok: boolean; tasks?: Task[]; sources?: Source[]; error?: string }>(
        "/api/collect/tasks");
      if (!r.ok) {
        setErr(r.error || "读取采集任务失败");
        return;
      }
      setErr("");
      setTasks(r.tasks || []);
      if (r.sources?.length) setSources(r.sources);
      if (!keepMsg) setMsg("");
    } catch {
      setErr("读取采集任务失败（网络或登录状态）");
    }
  }, []);

  useEffect(() => { void load(false); }, [load]);

  // 采集中时自动刷新状态（最多 3 分钟）
  const pollRunning = useCallback(() => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    let n = 0;
    pollRef.current = window.setInterval(() => {
      n += 1;
      void load();
      if (n > 45) {
        if (pollRef.current) window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    }, 4000);
  }, [load]);

  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current); }, []);

  const loadRuns = useCallback(async (taskId: string) => {
    try {
      const r = await getJson<{ ok: boolean; runs?: Run[] }>(
        `/api/collect/runs?taskId=${encodeURIComponent(taskId)}`);
      setRuns(r.ok ? (r.runs || []) : []);
    } catch {
      setRuns([]);
    }
  }, []);

  const editing = draft !== null;

  function startNew() {
    setDraft(emptyDraft());
    setRuns([]);
    setMsg("");
    setErr("");
  }

  function startEdit(t: Task) {
    setDraft({ ...t });
    setMsg("");
    setErr("");
    void loadRuns(t.id);
  }

  async function save() {
    if (!draft) return;
    setBusy(true);
    setErr("");
    try {
      const r = await postJson<{ ok: boolean; task?: Task; error?: string }>(
        "/api/collect/tasks", {
          id: draft.id,
          title: draft.title,
          sources: draft.sources,
          schedule: draft.schedule,
          targetCount: Number(draft.target_count) || 20,
          brief: draft.brief,
          enabled: draft.enabled,
        });
      if (!r.ok) {
        setErr(r.error || "保存失败");
        return;
      }
      setMsg(draft.id ? "已保存" : "任务已创建，到点自动采集");
      setDraft({ ...(r.task || draft) });
      await load();
      if (r.task?.id) void loadRuns(r.task.id);
    } catch {
      setErr("保存失败（网络或登录状态）");
    } finally {
      setBusy(false);
    }
  }

  async function runNow(t: Task) {
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      const r = await postJson<{ ok: boolean; error?: string; busy?: boolean }>(
        "/api/collect/run", { id: t.id });
      if (!r.ok) {
        setErr(r.error || "触发失败");
        return;
      }
      setMsg("已开始采集：hermes 会先理解任务描述再抓取，完事后 CCO 出灵感（可关掉此页，稍后回来看）");
      pollRunning();
      await load();
      onRan?.();
    } catch {
      setErr("触发失败（网络或登录状态）");
    } finally {
      setBusy(false);
    }
  }

  async function remove(t: Task) {
    if (!window.confirm(`删除任务「${t.title || "自动采集"}」？已采集的文件不会删除。`)) return;
    setBusy(true);
    try {
      const r = await postJson<{ ok: boolean; error?: string }>("/api/collect/delete", { id: t.id });
      if (!r.ok) {
        setErr(r.error || "删除失败");
        return;
      }
      setMsg("任务已删除");
      if (draft?.id === t.id) setDraft(null);
      await load();
    } catch {
      setErr("删除失败（网络或登录状态）");
    } finally {
      setBusy(false);
    }
  }

  const sched = splitSchedule(draft?.schedule);
  const picked = new Set((draft?.sources || "").split(",").filter(Boolean));

  function toggleSource(key: string) {
    if (!draft) return;
    const next = new Set(picked);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    if (next.size === 0) next.add(key);   // 至少留一个数据源
    setDraft({ ...draft, sources: [...next].join(",") });
  }

  function setSchedule(kind: "daily" | "every", patch: Partial<{ hh: string; mm: string; n: string }>) {
    if (!draft) return;
    const cur = splitSchedule(draft.schedule);
    const v = { ...cur, ...patch, kind };
    setDraft({ ...draft, schedule: joinSchedule(kind, v.hh, v.mm, v.n) });
  }

  return (
    <div className="ct-mask" role="dialog" aria-modal="true" aria-label="自动采集定时任务">
      <div className="ct-card">
        <header className="ct-head">
          <div>
            <h3>自动采集 · 定时任务</h3>
            <p className="muted">
              采集输出统一落 <code>users/&lt;手机号&gt;/downloads</code>；采完 CCO 结合公司知识库出「灵感 + 结合点」，
              进「创意灵感 → 采集洞察」，选题时优先采用。
            </p>
          </div>
          <button type="button" className="btn btn-ghost" onClick={onClose}>关闭</button>
        </header>

        {err && <div className="error-banner">{err}</div>}
        {msg && <div className="ct-ok">{msg}</div>}

        <div className="ct-body">
          <section className="ct-list">
            <div className="ct-list-head">
              <b>任务（{tasks.length}）</b>
              <button type="button" className="btn" onClick={startNew} disabled={busy}>＋ 新建任务</button>
            </div>
            {tasks.length === 0 && (
              <p className="muted ct-empty">
                还没有任务。设一条：定好周期、数据源、目标数量和「采集任务描述」，
                到点 hermes 自动去抓，结果直接出现在「自动采集」标签里。
              </p>
            )}
            {tasks.map((t) => (
              <article className={`ct-item${draft?.id === t.id ? " active" : ""}`} key={t.id}>
                <div className="ct-item-main">
                  <div className="ct-item-title">
                    <b>{t.title || "自动采集"}</b>
                    <span className={`ct-pill s-${t.status || "idle"}`}>
                      {STATUS_TEXT[t.status || "idle"] || t.status}
                    </span>
                    {!t.enabled && <span className="ct-pill s-off">已停用</span>}
                  </div>
                  <div className="ct-item-meta">
                    {t.scheduleLabel || t.schedule} · 目标 {t.target_count} 条 ·{" "}
                    {(t.sourcesList || []).map((k) => SOURCE_LABEL(k, sources)).join(" / ")}
                  </div>
                  <div className="ct-item-meta muted">
                    上次 {fmtTime(t.last_run)} · 下次 {t.enabled ? fmtTime(t.next_run) : "—"}
                    {t.last_note ? ` · ${t.last_note.slice(0, 80)}` : ""}
                  </div>
                </div>
                <div className="ct-item-acts">
                  <button type="button" className="btn btn-ghost" disabled={busy || t.status === "running"}
                          onClick={() => void runNow(t)}>
                    {t.status === "running" ? "采集中…" : "立即采集"}
                  </button>
                  <button type="button" className="btn btn-ghost" onClick={() => startEdit(t)}>编辑</button>
                  <button type="button" className="btn btn-ghost danger" onClick={() => void remove(t)}>删除</button>
                </div>
              </article>
            ))}
          </section>

          {editing && (
            <section className="ct-form">
              <div className="ct-form-head">
                <h4>{draft?.id ? "编辑任务" : "新建任务"}</h4>
                <div className="ct-acts">
                  <button type="button" className="btn" onClick={() => void save()} disabled={busy}>
                    {busy ? "保存中…" : "保存"}
                  </button>
                  <button type="button" className="btn btn-ghost" onClick={() => setDraft(null)} disabled={busy}>
                    取消
                  </button>
                </div>
              </div>
              <p className="muted ct-form-note">
                保存后下次运行：{draft?.enabled ? fmtTime(draft?.next_run) : "不自动运行"}
              </p>

              <label className="ct-field">
                <span>任务名称</span>
                <input
                  value={draft?.title || ""}
                  placeholder="例如：美妆行业短视频 · 每日盯 20 条"
                  onChange={(e) => setDraft({ ...draft!, title: e.target.value })}
                />
              </label>

              <div className="ct-field">
                <span>定时周期</span>
                <div className="ct-inline">
                  <select
                    value={sched.kind}
                    onChange={(e) => setSchedule(e.target.value as "daily" | "every", {})}
                  >
                    <option value="daily">每天</option>
                    <option value="every">每隔 N 小时</option>
                  </select>
                  {sched.kind === "daily" ? (
                    <div className="ct-inline">
                      <input
                        type="time"
                        value={`${sched.hh}:${sched.mm}`}
                        onChange={(e) => {
                          const [hh, mm] = e.target.value.split(":");
                          setSchedule("daily", { hh: hh || "09", mm: mm || "00" });
                        }}
                      />
                      <span className="muted">（服务器时间）</span>
                    </div>
                  ) : (
                    <div className="ct-inline">
                      <input
                        type="number"
                        min={1}
                        max={168}
                        value={sched.n}
                        onChange={(e) => setSchedule("every", { n: e.target.value })}
                      />
                      <span className="muted">小时跑一次</span>
                    </div>
                  )}
                </div>
              </div>

              <div className="ct-field">
                <span>数据源</span>
                <div className="ct-chips">
                  {sources.map((s) => (
                    <button
                      type="button"
                      key={s.key}
                      className={picked.has(s.key) ? "ct-chip on" : "ct-chip"}
                      onClick={() => toggleSource(s.key)}
                    >
                      {s.label}
                    </button>
                  ))}
                </div>
                <span className="muted ct-hint">
                  只采集公开可访问的内容；「自己账号的内容」按账号已登录状态读取，不绕过登录与验证码。
                </span>
              </div>

              <label className="ct-field">
                <span>目标数量</span>
                <div className="ct-inline">
                  <input
                    type="number"
                    min={1}
                    max={200}
                    value={draft?.target_count ?? 20}
                    onChange={(e) => setDraft({ ...draft!, target_count: Number(e.target.value) })}
                  />
                  <span className="muted">条 / 每轮</span>
                </div>
              </label>

              <label className="ct-field">
                <span>采集任务描述</span>
                <textarea
                  rows={5}
                  value={draft?.brief || ""}
                  placeholder={
                    "写清要什么、不要什么，hermes 会先用大模型把这段描述理解一遍再动手。\n" +
                    "例如：只盯近 7 天、播放量 10 万以上的美妆测评短视频；关注「成分党」「平价替代」两个方向；" +
                    "排除纯带货和素人开箱；每条要标题、链接和一句话卖点。"
                  }
                  onChange={(e) => setDraft({ ...draft!, brief: e.target.value })}
                />
                <span className="muted ct-hint">
                  大模型会先解析这段描述，生成采集口径（抓什么、怎么判相关、怎么去重），再交给浏览器执行。
                </span>
              </label>

              <label className="ct-check">
                <input
                  type="checkbox"
                  checked={!!draft?.enabled}
                  onChange={(e) => setDraft({ ...draft!, enabled: e.target.checked ? 1 : 0 })}
                />
                <span>启用（关掉只是不自动跑，任务和已采内容都保留）</span>
              </label>

              {draft?.id && (
                <div className="ct-runs">
                  <b>运行记录</b>
                  {runs.length === 0 && <p className="muted">还没有跑过。点任务上的「立即采集」可以先跑一轮。</p>}
                  {runs.map((r) => (
                    <div className="ct-run" key={r.id}>
                      <span className={`ct-pill s-${r.ok ? "ok" : "error"}`}>{r.ok ? "成功" : "失败"}</span>
                      <span className="muted">{fmtTime(r.started_at)}</span>
                      <span>文件 {r.file_count} · 灵感 {r.idea_count}</span>
                      <code>{r.out_dir || "—"}</code>
                      {r.note && <span className="ct-run-note">{r.note}</span>}
                    </div>
                  ))}
                </div>
              )}
            </section>
          )}
        </div>
      </div>
    </div>
  );
}

function SOURCE_LABEL(key: string, list: Source[]): string {
  return list.find((s) => s.key === key)?.label || key;
}