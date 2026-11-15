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
  { key: "browse", label: "共享资料" },
  { key: "personal", label: "个人 Obsidian" },
  { key: "idea", label: "创意灵感" },
] as const;
type TabKey = (typeof TABS)[number]["key"];
type RootKey = "vault" | "published" | "downloads" | "private";

// 「共享资料」按用途分标签：素材库里绝大部分是这几类，其它一律归"其它"
type KindKey = "all" | "video" | "audio" | "image" | "skill" | "other";
const KINDS: { key: KindKey; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "video", label: "短视频" },
  { key: "audio", label: "音乐" },
  { key: "image", label: "图片" },
  { key: "skill", label: "Skill 文件" },
  { key: "other", label: "其它格式" },
];

const EXT_VIDEO = new Set(["mp4", "mov", "webm", "mkv", "avi", "m4v", "flv", "wmv", "mpg", "mpeg", "ts"]);
const EXT_AUDIO = new Set(["mp3", "wav", "flac", "m4a", "aac", "ogg", "opus", "wma", "aiff", "amr"]);
const EXT_IMAGE = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif", "heic", "ico", "tiff"]);

function kindOf(name: string, ext: string): Exclude<KindKey, "all"> {
  const e = (ext || "").toLowerCase().replace(/^\./, "");
  if (EXT_VIDEO.has(e)) return "video";
  if (EXT_AUDIO.has(e)) return "audio";
  if (EXT_IMAGE.has(e)) return "image";
  const low = name.toLowerCase();
  // 技能说明本身就是 markdown（SKILL.md），加上 .skill 包和文件名带 skill 的，一起归这一类
  if (e === "skill" || e === "md" || low === "skill.md" || low.includes("skill")) return "skill";
  return "other";
}

function previewKind(name: string, ext: string): "image" | "audio" | "video" | "" {
  const k = kindOf(name, ext);
  return k === "image" || k === "audio" || k === "video" ? k : "";
}

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
  const [kind, setKind] = useState<KindKey>("all");
  const [preview, setPreview] = useState<
    { url: string; kind: "image" | "audio" | "video"; name: string } | null
  >(null);

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

  // 删除只发生在个人空间；公共目录（vault / 发布 / 外部下载）根本不渲染这个入口
  const [delBusy, setDelBusy] = useState("");
  const doDelete = useCallback(async (rel: string, isDir: boolean) => {
    const label = rel.split("/").pop() || rel;
    if (!window.confirm(isDir
      ? `删除目录「${label}」？里面的内容会一并删除，且无法恢复。`
      : `删除文件「${label}」？删除后无法恢复。`)) {
      return;
    }
    setDelBusy(rel);
    try {
      const send = (recursive: boolean) => fetch("/api/materials/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ root: "private", path: rel, recursive }),
      });
      let res = await send(false);
      let data = (await res.json()) as { ok?: boolean; error?: string; needConfirm?: boolean; count?: number };
      if (res.status === 409 && data.needConfirm) {
        if (!window.confirm(`目录里还有 ${data.count ?? 0} 项，确认连同一起删除？`)) return;
        res = await send(true);
        data = (await res.json()) as typeof data;
      }
      if (!data.ok) {
        window.alert(data.error || "删除失败");
        return;
      }
      await loadBrowse(effRoot, path);
    } catch {
      window.alert("删除请求失败");
    } finally {
      setDelBusy("");
    }
  }, [loadBrowse, effRoot, path]);

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

  // 各根对应各自的只读路由：private 走 /private/（服务端按会话或个人 token 校验）
  const fileUrl = useCallback((name: string) => {
    const segs = path.split("/").filter(Boolean);
    const tail = [...segs, name].map(encodeURIComponent).join("/");
    if (effRoot === "published") return `/materials/${tail}`;
    if (effRoot === "downloads") return `/downloads/${tail}`;
    if (effRoot === "private") return `/private/${browse?.phone ?? ""}/${tail}`;
    return `/vault/${["素材", ...segs, name].map(encodeURIComponent).join("/")}`;
  }, [path, effRoot, browse?.phone]);

  const shownFiles = useMemo(
    () => (browse?.files ?? []).filter((f) => kind === "all" || kindOf(f.name, f.ext) === kind),
    [browse?.files, kind],
  );

  // Esc 关预览；切目录时也顺手关掉，避免残留上一个文件
  useEffect(() => {
    if (!preview) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPreview(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [preview]);

  useEffect(() => {
    setPreview(null);
  }, [path, effRoot]);

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
            <p className="muted">清单为空。素材实际存放在 vault 的「素材」目录，切到「共享资料」查看。</p>
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
              仅当前登录账号可见，右侧可删除 · 个人空间：<code>conring/users/&lt;手机号&gt;/</code>
              （含 <code>工具箱</code>、<code>obsidian</code>，点面包屑可回到上级）
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

          <div className="mat-kinds" role="tablist" aria-label="文件类型">
            {KINDS.map((k) => {
              const files = browse?.files ?? [];
              const n = k.key === "all"
                ? files.length
                : files.filter((f) => kindOf(f.name, f.ext) === k.key).length;
              return (
                <button
                  key={k.key}
                  type="button"
                  role="tab"
                  aria-selected={kind === k.key}
                  className={kind === k.key ? "mat-kind active" : "mat-kind"}
                  onClick={() => setKind(k.key)}
                >
                  {k.label}
                  <span className="mat-kind-n">{n}</span>
                </button>
              );
            })}
          </div>

          {browsing && <p className="muted">读取中…</p>}
          {!browsing && browse?.error && <div className="error-banner">{browse.error}</div>}
          {!browsing && browse?.ok && (
            <>
              <div className="mat-counts muted">
                目录 {browse.counts?.dirs ?? 0} 个 · 文件 {browse.counts?.files ?? 0} 个
                {kind !== "all" && `（当前分类 ${shownFiles.length} 个）`}
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
                  {browse.dirs?.map((d) => {
                    const relDir = path ? `${path}/${d.name}` : d.name;
                    return (
                      <li key={d.name}>
                        <button
                          type="button"
                          className="mat-entry mat-dir"
                          onClick={() => setPath(relDir)}
                        >
                          <span className="mat-icon" aria-hidden="true">▸</span>
                          <span className="mat-name">{d.name}</span>
                        </button>
                        {effRoot === "private" && (
                          <button
                            type="button"
                            className="mat-del"
                            disabled={delBusy === relDir}
                            onClick={() => void doDelete(relDir, true)}
                            title="删除这个目录（连同里面的内容）"
                          >
                            {delBusy === relDir ? "删除中…" : "删除"}
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
              {(browse.files?.length ?? 0) > 0 && shownFiles.length === 0 && (
                <p className="muted">
                  这个目录里没有「{KINDS.find((k) => k.key === kind)?.label}」类文件，换个标签看看。
                </p>
              )}
              {shownFiles.length > 0 && (
                <ul className="mat-list">
                  {shownFiles.map((f) => {
                    const href = fileUrl(f.name);
                    const pv = previewKind(f.name, f.ext);
                    const relPath = [...path.split("/").filter(Boolean), f.name].join("/");
                    return (
                      <li key={f.name}>
                        <a className="mat-entry" href={href} target="_blank" rel="noreferrer">
                          <span className="mat-icon" aria-hidden="true">·</span>
                          <span className="mat-name">{f.name}</span>
                          <span className="mat-file-meta">
                            {f.ext ? `${f.ext.toUpperCase()}　` : ""}{humanSize(f.size)}　{shortDate(f.mtime)}
                          </span>
                        </a>
                        {pv && (
                          <button
                            type="button"
                            className="mat-view"
                            onClick={() => setPreview({ url: href, kind: pv, name: f.name })}
                            title={pv === "image" ? "在当前页查看图片" : "在当前页播放"}
                          >
                            {pv === "image" ? "查看" : "播放"}
                          </button>
                        )}
                        {effRoot === "private" && (
                          <button
                            type="button"
                            className="mat-del"
                            disabled={delBusy === relPath}
                            onClick={() => void doDelete(relPath, false)}
                            title="删除这个文件"
                          >
                            {delBusy === relPath ? "删除中…" : "删除"}
                          </button>
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

      {/* 页内预览：图片直接看，音视频直接播，不跳走 */}
      {preview && (
        <div
          className="mat-preview"
          role="dialog"
          aria-modal="true"
          aria-label={preview.name}
          onClick={() => setPreview(null)}
        >
          <div className="mat-preview-box" onClick={(e) => e.stopPropagation()}>
            <div className="mat-preview-head">
              <strong title={preview.name}>{preview.name}</strong>
              <div className="mat-preview-acts">
                <a href={preview.url} target="_blank" rel="noreferrer">新标签打开</a>
                <button type="button" onClick={() => setPreview(null)}>关闭</button>
              </div>
            </div>
            {preview.kind === "image" && <img src={preview.url} alt={preview.name} />}
            {preview.kind === "audio" && <audio src={preview.url} controls autoPlay />}
            {preview.kind === "video" && <video src={preview.url} controls autoPlay />}
          </div>
        </div>
      )}

      {tab === "idea" && <InspirationPage embedded />}
    </div>
  );
}