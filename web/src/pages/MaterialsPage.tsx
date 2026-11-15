import { useCallback, useEffect, useMemo, useState } from "react";
import { InspirationPage } from "./InspirationPage";

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
  root?: "vault" | "published";
  path?: string;
  dirs?: BrowseDir[];
  files?: BrowseFile[];
  counts?: { dirs: number; files: number };
  skipped?: number;
  error?: string;
};

const TABS = [
  { key: "index", label: "素材清单" },
  { key: "browse", label: "目录浏览" },
  { key: "idea", label: "创意灵感" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

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
  const [tab, setTab] = useState<TabKey>("index");
  const [items, setItems] = useState<MatItem[]>([]);
  const [updated, setUpdated] = useState("");
  const [indexErr, setIndexErr] = useState("");

  const [root, setRoot] = useState<"vault" | "published">("vault");
  const [path, setPath] = useState("");
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

  const loadBrowse = useCallback(async (r: "vault" | "published", p: string) => {
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

  useEffect(() => {
    if (tab === "browse") void loadBrowse(root, path);
  }, [tab, root, path, loadBrowse]);

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

      {tab === "browse" && (
        <section className="mat-section">
          <div className="mat-browse-bar">
            <div className="mat-roots">
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
                    const href = root === "published"
                      ? `/materials/${path ? `${path}/` : ""}${encodeURIComponent(f.name)}`
                      : "";
                    return (
                      <li key={f.name}>
                        {href ? (
                          <a className="mat-entry" href={href} target="_blank" rel="noreferrer">
                            <span className="mat-icon" aria-hidden="true">·</span>
                            <span className="mat-name">{f.name}</span>
                            <span className="mat-file-meta">
                              {f.ext ? `${f.ext.toUpperCase()}　` : ""}{humanSize(f.size)}　{shortDate(f.mtime)}
                            </span>
                          </a>
                        ) : (
                          <span className="mat-entry mat-file-static" title="文件在 vault，暂不支持在线预览">
                            <span className="mat-icon" aria-hidden="true">·</span>
                            <span className="mat-name">{f.name}</span>
                            <span className="mat-file-meta">
                              {f.ext ? `${f.ext.toUpperCase()}　` : ""}{humanSize(f.size)}　{shortDate(f.mtime)}
                            </span>
                          </span>
                        )}
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