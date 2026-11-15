import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { InspirationPage } from "./InspirationPage";
import { ToolboxGallery } from "../components/ToolboxGallery";

type MatItem = {
  id: string;
  category: string;
  title: string;
  desc: string;
  url: string;
  meta: string;
};

type BrowseDir = { name: string };
type BrowseFile = { name: string; size: number; mtime: number; ext: string };
type BrowseData = {
  ok: boolean;
  root?: RootKey;
  path?: string;
  dirs?: BrowseDir[];
  files?: BrowseFile[];
  counts?: { dirs: number; files: number };
  skipped?: number;
  error?: string;
  /** root=private 时由服务端回传本人手机号，用于拼 /private/<手机号>/... */
  phone?: string;
};

const TABS = [
  { key: "index", label: "素材清单" },
  { key: "toolbox", label: "工具箱" },
  { key: "browse", label: "目录浏览" },
  { key: "personal", label: "个人 Obsidian" },
  { key: "idea", label: "创意灵感" },
] as const;
type TabKey = (typeof TABS)[number]["key"];
type RootKey = "vault" | "published" | "downloads" | "private";

function humanSize(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

function shortDate(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function MaterialsPage() {
  // 支持深链 /materials?tab=browse&path=工具箱/2026-11-15/图片
  // 工具箱出片后直接跳到这里，省得用户自己在目录里翻
  const boot = useMemo(() => {
    const q = new URLSearchParams(window.location.search);
    const t = q.get("tab");
    return {
      tab: (TABS.some((x) => x.key === t) ? t : "index") as TabKey,
      root: (["published", "downloads", "private"].includes(q.get("root") || "")
        ? q.get("root") : "vault") as RootKey,
      path: (q.get("path") || "").replace(/^\/+/, ""),
    };
  }, []);
  // 「个人 Obsidian」默认直接进 obsidian 子目录（个人空间根下还有 工具箱）
  const personalPath = "obsidian";

  const [tab, setTab] = useState<TabKey>(boot.tab);
  const [items, setItems] = useState<MatItem[]>([]);
  const [updated, setUpdated] = useState("");
  const [indexErr, setIndexErr] = useState("");

  const [root, setRoot] = useState<RootKey>(boot.root);
  const [path, setPath] = useState(
    boot.tab === "personal" && !boot.path ? "obsidian" : boot.path);
  const [browse, setBrowse] = useState<BrowseData | null>(null);
  const [browsing, setBrowsing] = useState(false);

  useEffect(() => {
    fetch("/api/materials", { credentials: "include" })
      .then((r) => r.json())
      .then((d: { items?: MatItem[]; updated?: string }) => {
        setItems(Array.isArray(d.items) ? d.items : []);
        setUpdated(d.updated ?? "");
      })
      .catch(() => setIndexErr("素材清单读取失败"));
  }, []);

  const loadBrowse = useCallback(async (r: RootKey, p: string) => {
    setBrowsing(true);
    try {
      const res = await fetch(
        `/api/materials/browse?root=${r}&path=${encodeURIComponent(p)}`,
        { credentials: "include" },
      );
      setBrowse((await res.json()) as BrowseData);
    } catch {
      setBrowse({ ok: false, error: "目录读取失败" });
    } finally {
      setBrowsing(false);
    }
  }, []);

  // 「个人 Obsidian」显示的是当前登录账号自己的目录，根由服务端按会话决定，
  // 前端不传手机号（也没法传），切换根按钮在这个 tab 下不生效。
  const effRoot: RootKey = tab === "personal" ? "private" : root;

  useEffect(() => {
    if (tab === "browse" || tab === "personal") void loadBrowse(effRoot, path);
  }, [tab, effRoot, path, loadBrowse]);

  // 换 tab 时回到各自的入口目录；首次挂载跳过，否则会清掉深链里的 path
  const firstTabRef = useRef(true);
  useEffect(() => {
    if (firstTabRef.current) {
      firstTabRef.current = false;
      return;
    }
    setPath(tab === "personal" ? personalPath : "");
  }, [tab, personalPath]);

  const crumbs = useMemo(() => path.split("/").filter(Boolean), [path]);

  const grouped = useMemo(() => {
    const map = new Map<string, MatItem[]>();
    for (const it of items) {
      const k = it.category || "未分类";
      const arr = map.get(k) ?? [];
      arr.push(it);
      map.set(k, arr);
    }
    return [...map.entries()];
  }, [items]);

  return (
    <div className="mat-page">
      <div className="page-head">
        <div>
          <h1>素材</h1>
          <p className="mat-sub">
            原料层：外部采集、对标原片、音视频与图文素材。成品请到「作品档案」，结论请到「知识库」
          </p>
        </div>
        <div className="mat-head-meta">
          {updated && <span className="muted">清单更新于 {updated}</span>}
        </div>
      </div>

      <div className="mat-tabs" role="tablist" aria-label="素材视图">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={tab === t.key ? "mat-tab active" : "mat-tab"}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "index" && (
        <section className="mat-section">
          {indexErr && <div className="error-banner">{indexErr}</div>}
          {!indexErr && items.length === 0 && (
            <p className="muted">清单为空。素材实际存放在 vault 的「素材」目录，切到「目录浏览」查看。</p>
          )}
          {grouped.map(([cat, list]) => (
            <div key={cat} className="mat-group">
              <h3 className="mat-group-title">
                {cat}
                <span className="muted">　{list.length} 项</span>
              </h3>
              <div className="mat-grid">
                {list.map((it) => (
                  <article key={it.id} className="mat-card">
                    <h4>{it.title}</h4>
                    {it.desc && <p className="mat-desc">{it.desc}</p>}
                    <div className="mat-card-foot">
                      {it.meta && <span className="mat-meta">{it.meta}</span>}
                      {it.url && (
                        <a className="btn btn-ghost mat-open" href={it.url} target="_blank" rel="noreferrer">
                          打开
                        </a>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            </div>
          ))}
        </section>
      )}

      {tab === "toolbox" && (
        <section className="mat-section">
          <ToolboxGallery />
        </section>
      )}

      {(tab === "browse" || tab === "personal") && (
        <section className="mat-section">
          {tab === "personal" && (
            <p className="muted mat-private-hint">
              仅当前登录账号可见 · 个人空间：<code>conring/users/&lt;手机号&gt;/</code>
              （含 <code>工具箱</code>、<code>obsidian</code>，可点上面包屑回到上级）
            </p>
          )}
          <div className="mat-browse-bar">
            <div className="mat-roots">
              {tab === "personal" ? (
                <span className="mat-root active">个人 Obsidian</span>
              ) : (
                <>
                  <button
                    type="button"
                    className={root === "vault" ? "mat-root active" : "mat-root"}
                    onClick={() => { setRoot("vault"); setPath(""); }}
                  >
                    vault 素材
                  </button>
                  <button
                    type="button"
                    className={root === "published" ? "mat-root active" : "mat-root"}
                    onClick={() => { setRoot("published"); setPath(""); }}
                  >
                    发布素材
                  </button>
                  <button
                    type="button"
                    className={root === "downloads" ? "mat-root active" : "mat-root"}
                    onClick={() => { setRoot("downloads"); setPath(""); }}
                    title="其它渠道下载到 conring/downloads 的文件"
                  >
                    外部下载
                  </button>
                </>
              )}
            </div>
            <nav className="mat-crumbs" aria-label="路径">
              <button type="button" onClick={() => setPath("")}>根目录</button>
              {crumbs.map((c, i) => (
                <span key={`${c}-${i}`}>
                  <span className="mat-sep">/</span>
                  <button type="button" onClick={() => setPath(crumbs.slice(0, i + 1).join("/"))}>
                    {c}
                  </button>
                </span>
              ))}
            </nav>
          </div>

          {browsing && <p className="muted">读取中…</p>}
          {!browsing && browse?.error && <div className="error-banner">{browse.error}</div>}
          {!browsing && browse?.ok && (
            <>
              <div className="mat-counts muted">
                目录 {browse.counts?.dirs ?? 0} 个 · 文件 {browse.counts?.files ?? 0} 个
                {browse.skipped ? ` · 跳过 ${browse.skipped} 个不可读项` : ""}
                {(browse.counts?.files ?? 0) > 500 && "　（仅显示前 500 个文件）"}
              </div>
              {tab === "personal"
                && (browse.counts?.dirs ?? 0) === 0
                && (browse.counts?.files ?? 0) === 0 && (
                  <div className="tg-empty">
                    <b>{path ? "这个目录还是空的" : "个人空间还是空的"}</b>
                    <p>
                      位置是 <code>conring/users/&lt;手机号&gt;/{path || ""}</code>，只有你登录后才能看到。
                      往里面放笔记，或把已有库同步进来，就会出现在这里。
                    </p>
                  </div>
                )}
              {(browse.dirs?.length ?? 0) > 0 && (
                <ul className="mat-list">
                  {browse.dirs?.map((d) => (
                    <li key={d.name}>
                      <button
                        type="button"
                        className="mat-entry mat-dir"
                        onClick={() => setPath(path ? `${path}/${d.name}` : d.name)}
                      >
                        <span className="mat-icon" aria-hidden="true">▸</span>
                        <span className="mat-name">{d.name}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {(browse.files?.length ?? 0) > 0 && (
                <ul className="mat-list">
                  {browse.files?.map((f) => {
                    // 各根对应各自的只读路由：private 走 /my-obsidian/（服务端按会话校验手机号）
                    const segs = path.split("/").filter(Boolean);
                    const tail = [...segs, f.name].map(encodeURIComponent).join("/");
                    const href = effRoot === "published"
                      ? `/materials/${tail}`
                      : effRoot === "downloads"
                        ? `/downloads/${tail}`
                        : effRoot === "private"
                          ? `/private/${browse?.phone ?? ""}/${tail}`
                          : `/vault/${["素材", ...segs, f.name].map(encodeURIComponent).join("/")}`;
                    return (
                      <li key={f.name}>
                        <a className="mat-entry" href={href} target="_blank" rel="noreferrer">
                          <span className="mat-icon" aria-hidden="true">·</span>
                          <span className="mat-name">{f.name}</span>
                          <span className="mat-file-meta">
                            {f.ext ? `${f.ext.toUpperCase()}　` : ""}{humanSize(f.size)}　{shortDate(f.mtime)}
                          </span>
                        </a>
                      </li>
                    );
                  })}
                </ul>
              )}
            </>
          )}
        </section>
      )}

      {tab === "idea" && <InspirationPage embedded />}
    </div>
  );
}