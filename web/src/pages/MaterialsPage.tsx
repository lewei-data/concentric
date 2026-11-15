import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { InspirationPage } from "./InspirationPage";
import { CollectTasks } from "./CollectTasks";
import { ToolboxGallery } from "../components/ToolboxGallery";
import { AnchoredPanel } from "../components/AnchoredPanel";
import { siteUrl } from "../lib/siteUrl";

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
  /** 目录指纹：内容一变就变，前端拿它做长轮询判断要不要重新拉 */
  sig?: string;
  /** root=private 时由服务端回传本人手机号，用于拼 /private/<手机号>/... */
  phone?: string;
};

const TABS = [
  { key: "index", label: "素材清单" },
  { key: "toolbox", label: "工具箱" },
  { key: "browse", label: "共享资料" },
  { key: "collect", label: "自动采集" },
  { key: "personal", label: "个人 Obsidian" },
  { key: "idea", label: "创意灵感" },
] as const;
type TabKey = (typeof TABS)[number]["key"];
type RootKey = "vault" | "published" | "downloads" | "private" | "collect";

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

/** 缩略图墙每次多铺多少张，滚到底再续 */
const GRID_PAGE = 60;

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

// 浏览器认不了的图片格式：别硬塞 <img>，直接提示 + 给原图链接
const NO_INLINE_IMAGE = new Set(["heic", "heif", "tiff", "tif"]);

/** 素材预览：加载中给提示，失败时把真实 URL 亮出来（否则只剩一块空白，分不清是路径错、没登录还是文件没了） */
function MatMedia({ kind, url, name, ext, full }: {
  kind: "image" | "audio" | "video"; url: string; name: string; ext: string; full?: boolean;
}) {
  const [state, setState] = useState<"loading" | "ok" | "err">("loading");
  const [zoom, setZoom] = useState(false);
  useEffect(() => { setState("loading"); setZoom(false); }, [url]);

  if (kind === "audio") return <audio src={url} controls autoPlay onError={() => setState("err")} />;
  if (kind === "video") return <video src={url} controls autoPlay onError={() => setState("err")} />;

  const unsupported = NO_INLINE_IMAGE.has(String(ext || "").toLowerCase().replace(/^\./, ""));
  const broken = state === "err" || unsupported;
  return (
    <>
      {broken && (
        <div className={`mat-media-fallback${full ? " is-full" : ""}`}>
          <strong>{unsupported ? "这个格式浏览器不能直接显示" : "图片没加载出来"}</strong>
          <code>{url}</code>
          {!unsupported && (
            <span>
              常见原因：文件已被删除（404）、登录已过期（401）、或还在生成中。点下面可以直接打开原图确认。
            </span>
          )}
          <a href={url} target="_blank" rel="noreferrer">
            {unsupported ? "直接打开原图（可能需下载后查看）" : "直接打开原图"}
          </a>
        </div>
      )}
      {!unsupported && (
        <img
          src={url}
          alt={name}
          decoding="async"
          data-zoom={zoom ? "1" : "0"}
          style={state === "loading" ? { opacity: 0 } : undefined}
          onClick={full ? () => setZoom((z) => !z) : undefined}
          onLoad={() => setState("ok")}
          onError={() => setState("err")}
          title={full ? (zoom ? "点击：适应窗口" : "点击：1:1 原图") : undefined}
        />
      )}
      {state === "loading" && !unsupported && (
        <div className={`mat-media-fallback${full ? " is-full" : ""} is-loading`}>图片加载中…</div>
      )}
    </>
  );
}

// 出不了缩略图的格式按用途分组配色，方块上用扩展名当封面，比一个通用问号好认
const TONE_RULES: { tone: string; exts: string[] }[] = [
  { tone: "doc", exts: ["md", "markdown", "txt", "rtf", "doc", "docx", "pdf", "pages", "odt", "epub"] },
  { tone: "data", exts: ["json", "jsonl", "csv", "tsv", "xls", "xlsx", "yaml", "yml", "xml", "toml", "ini", "db", "sqlite"] },
  { tone: "code", exts: ["js", "mjs", "cjs", "ts", "tsx", "jsx", "py", "sh", "bash", "css", "scss", "html", "htm", "sql", "go", "rs", "java", "c", "cpp", "h", "vue", "lua", "php"] },
  { tone: "pkg", exts: ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "skill", "dmg", "iso", "pkg"] },
];

function extTone(ext: string, kind: string): string {
  const e = (ext || "").toLowerCase().replace(/^\./, "");
  if (kind === "audio" || kind === "video") return "media";
  for (const r of TONE_RULES) if (r.exts.includes(e)) return r.tone;
  if (kind === "skill") return "pkg";
  return "file";
}

/** 方块封面上写的字：能拿到扩展名就写扩展名，Skill 包写 SKILL */
function extLabel(ext: string, kind: string): string {
  const e = (ext || "").toLowerCase().replace(/^\./, "");
  if (kind === "skill" && (e === "md" || e === "skill" || e === "")) return "SKILL";
  if (!e) return "FILE";
  return e.slice(0, 5).toUpperCase();
}

// 浏览器能自己渲染的格式：点开就是新标签页里的原生查看器，不需要页内弹窗
const BROWSER_VIEWABLE = new Set([
  "md", "markdown", "txt", "text", "log", "json", "jsonl", "csv", "tsv", "yaml", "yml",
  "xml", "toml", "ini", "conf", "cfg", "env", "sql", "diff", "patch",
  "js", "mjs", "cjs", "ts", "tsx", "jsx", "py", "sh", "bash", "zsh", "css", "scss", "less",
  "html", "htm", "go", "rs", "java", "c", "cpp", "h", "hpp", "php", "vue", "lua", "rb",
  "pdf", "rtf", "vtt", "srt", "ics",
]);

// 认不出来的类型：告诉用户这是什么、该怎么打开，别让人对着空白页发懵
const TYPE_LABEL: Record<string, string> = {
  zip: "ZIP 压缩包", rar: "RAR 压缩包", "7z": "7z 压缩包", tar: "TAR 归档", gz: "GZIP 压缩包",
  bz2: "BZIP2 压缩包", xz: "XZ 压缩包", tgz: "TGZ 压缩包",
  doc: "Word 文档", docx: "Word 文档", odt: "ODT 文档", rtf: "RTF 文档",
  xls: "Excel 表格", xlsx: "Excel 表格", ods: "ODS 表格",
  ppt: "PowerPoint 演示文稿", pptx: "PowerPoint 演示文稿", key: "Keynote 演示文稿",
  pages: "Pages 文档", numbers: "Numbers 表格",
  psd: "Photoshop 工程文件", ai: "Illustrator 工程文件", sketch: "Sketch 设计文件",
  fig: "Figma 设计文件", aep: "After Effects 工程", prproj: "Premiere 工程",
  exe: "Windows 可执行程序", msi: "Windows 安装包", dmg: "macOS 磁盘映像",
  apk: "Android 安装包", deb: "Debian 安装包", rpm: "RPM 安装包",
  db: "数据库文件", sqlite: "SQLite 数据库", sqlite3: "SQLite 数据库",
  bin: "二进制数据", dat: "二进制数据", iso: "光盘映像",
  ttf: "字体文件", otf: "字体文件", woff: "字体文件", woff2: "字体文件",
  epub: "EPUB 电子书", mobi: "MOBI 电子书",
};

const TYPE_ADVICE: { test: (e: string) => boolean; text: string }[] = [
  {
    test: (e) => ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz"].includes(e),
    text: "浏览器没法直接看压缩包。建议下载解压，把里面的单个文件再传上来，就能预览了。",
  },
  {
    test: (e) => ["doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "pages", "numbers", "key"].includes(e),
    text: "Office / 文稿类文件浏览器渲染不了。建议下载后用 Office、WPS 或 iWork 打开；需要在线看可以先导出成 PDF 再上传。",
  },
  {
    test: (e) => ["psd", "ai", "sketch", "fig", "aep", "prproj"].includes(e),
    text: "这是设计/剪辑工程文件，需要对应的软件才能打开。建议下载后在本机软件里处理，导出成图片或视频再上传。",
  },
  {
    test: (e) => ["exe", "msi", "dmg", "apk", "deb", "rpm", "iso"].includes(e),
    text: "安装包/磁盘映像，出于安全不建议在线打开。确实需要时下载后先确认来源再运行。",
  },
  {
    test: (e) => ["db", "sqlite", "sqlite3", "bin", "dat"].includes(e),
    text: "数据库或二进制文件，需要用专门的工具读取。建议下载后用对应客户端打开。",
  },
  {
    test: (e) => ["ttf", "otf", "woff", "woff2"].includes(e),
    text: "字体文件，双击预览依赖系统字体管理器。建议下载后安装查看。",
  },
  {
    test: (e) => ["epub", "mobi"].includes(e),
    text: "电子书格式，浏览器不支持。建议下载后用阅读软件打开，或转成 PDF。",
  },
];

function typeLabel(ext: string): string {
  const e = (ext || "").toLowerCase().replace(/^\./, "");
  if (!e) return "无扩展名（无法判断类型）";
  return `${e.toUpperCase()}　${TYPE_LABEL[e] || ""}`.trim();
}

function typeAdvice(ext: string): string {
  const e = (ext || "").toLowerCase().replace(/^\./, "");
  const hit = TYPE_ADVICE.find((r) => r.test(e));
  return hit ? hit.text : "浏览器不支持这种格式的预览，建议下载到本地用对应软件打开。";
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
  // 预览面板贴着被点的方块/按钮弹出；anchor 记的就是那个元素
  const [preview, setPreview] = useState<
    { url: string; kind: "image" | "audio" | "video"; name: string; ext?: string; anchor: HTMLElement | null } | null
  >(null);
  // 全屏观看层：媒体铺满屏幕，左上角带返回
  const [previewFull, setPreviewFull] = useState(false);
  // 浏览器打不开的格式：给一条说明 + 建议
  const [notice, setNotice] = useState<
    { name: string; ext: string; url: string; anchor: HTMLElement | null } | null
  >(null);

  useEffect(() => {
    fetch(siteUrl("/api/materials"), { credentials: "include" })
      .then((r) => r.json())
      .then((d: { items?: MatItem[]; updated?: string }) => {
        setItems(Array.isArray(d.items) ? d.items : []);
        setUpdated(d.updated ?? "");
      })
      .catch(() => setIndexErr("素材清单读取失败"));
  }, []);

  const loadBrowse = useCallback(async (r: RootKey, p: string, silent = false) => {
    if (!silent) setBrowsing(true);
    try {
      const res = await fetch(
        siteUrl(`/api/materials/browse?root=${r}&path=${encodeURIComponent(p)}`),
        { credentials: "include" },
      );
      setBrowse((await res.json()) as BrowseData);
    } catch {
      // 自动刷新失败时保留原列表，别因为一次网络抖动把页面清空
      if (!silent) setBrowse({ ok: false, error: "目录读取失败" });
    } finally {
      if (!silent) setBrowsing(false);
    }
  }, []);

  // 「个人 Obsidian」「自动采集」都是当前登录账号自己的目录，根由服务端按会话决定，
  // 前端不传手机号（也没法传），切换根按钮在这两个 tab 下不生效。
  const effRoot: RootKey = tab === "personal" ? "private" : tab === "collect" ? "collect" : root;

  useEffect(() => {
    if (tab === "browse" || tab === "personal" || tab === "collect") void loadBrowse(effRoot, path);
  }, [tab, effRoot, path, loadBrowse]);

  // 目录监听：在服务器上直接增删改文件（同步盘、scp、命令行 rm）时页面自己跟上。
  // 走长轮询——没变化时服务端挂着不返回，一变就在 1 秒内回。
  useEffect(() => {
    if (tab !== "browse" && tab !== "personal") return;
    if (!browse?.ok || !browse.sig) return;
    let alive = true;
    let sig = browse.sig;
    const ctrl = new AbortController();
    const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms));
    void (async () => {
      while (alive) {
        if (document.visibilityState === "hidden") {
          await sleep(4000);
          continue;
        }
        try {
          const res = await fetch(
            siteUrl(`/api/materials/watch?root=${effRoot}&path=${encodeURIComponent(path)}&sig=${sig}`),
            { credentials: "include", signal: ctrl.signal },
          );
          const d = (await res.json()) as { ok?: boolean; changed?: boolean; sig?: string };
          if (!alive) return;
          if (d.changed) {
            await loadBrowse(effRoot, path, true);
            if (d.sig) sig = d.sig;
            continue;
          }
          if (d.sig) sig = d.sig;
          if (!d.ok) await sleep(3000);
        } catch {
          if (!alive) return;
          await sleep(3000);
        }
      }
    })();
    // 从别的标签页切回来时立刻对一次，不用干等长轮询自然到期
    const onVis = () => {
      if (document.visibilityState === "visible") void loadBrowse(effRoot, path, true);
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      ctrl.abort();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [tab, effRoot, path, browse?.ok, browse?.sig, loadBrowse]);

  // 删除只发生在个人空间；公共目录（vault / 发布 / 外部下载）根本不渲染这个入口
  // 列表 / 缩略图两种看法：照片墙一次能看很多张，选过的偏好记在本地
  const [viewMode, setViewMode] = useState<"list" | "grid">(() => {
    try {
      return window.localStorage.getItem("mat.view") === "grid" ? "grid" : "list";
    } catch {
      return "list";
    }
  });
  const [limit, setLimit] = useState(GRID_PAGE);
  const gridEndRef = useRef<HTMLDivElement>(null);

  const [delBusy, setDelBusy] = useState("");
  // 「自动采集」标签里的定时任务设置面板（页内弹层，不跳走丢上下文）
  const [collectTasks, setCollectTasks] = useState(false);
  const doDelete = useCallback(async (rel: string, isDir: boolean) => {
    const label = rel.split("/").pop() || rel;
    if (!window.confirm(isDir
      ? `删除目录「${label}」？里面的内容会一并删除，且无法恢复。`
      : `删除文件「${label}」？删除后无法恢复。`)) {
      return;
    }
    setDelBusy(rel);
    try {
      const send = (recursive: boolean) => fetch(siteUrl("/api/materials/delete"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ root: effRoot, path: rel, recursive }),
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
    if (effRoot === "published") return siteUrl(`/materials/${tail}`);
    if (effRoot === "downloads") return siteUrl(`/downloads/${tail}`);
    if (effRoot === "collect") return siteUrl(`/private/${browse?.phone ?? ""}/downloads/${tail}`);
    if (effRoot === "private") return siteUrl(`/private/${browse?.phone ?? ""}/${tail}`);
    return siteUrl(`/vault/${["素材", ...segs, name].map(encodeURIComponent).join("/")}`);
  }, [path, effRoot, browse?.phone]);

  const shownFiles = useMemo(
    () => (browse?.files ?? []).filter((f) => kind === "all" || kindOf(f.name, f.ext) === kind),
    [browse?.files, kind],
  );

  // 缩略图地址：照片墙一次铺几十上百张，原图太大，交给服务端压到 420px 宽
  const thumbUrl = useCallback(
    // 必须走 siteUrl 补前缀：公网挂在 /workbench/app 时，裸 /api/... 会打到主站接口（401），缩略图全空
    (name: string) => siteUrl(
      `/api/materials/thumb?root=${effRoot}&path=${encodeURIComponent(path)}&name=${encodeURIComponent(name)}`),
    [effRoot, path],
  );

  const setView = useCallback((v: "list" | "grid") => {
    setViewMode(v);
    try {
      window.localStorage.setItem("mat.view", v);
    } catch {
      // 隐私模式下 localStorage 不可写，只影响记住偏好
    }
  }, []);

  // 换目录、换分类、换根都从第一屏重新开始
  useEffect(() => {
    setLimit(GRID_PAGE);
  }, [tab, effRoot, path, kind]);

  // 滚到底自动续一屏，避免一次性渲染几百个节点
  useEffect(() => {
    if (viewMode !== "grid") return;
    const el = gridEndRef.current;
    if (!el) return;
    const ob = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setLimit((l) => Math.min(l + GRID_PAGE, shownFiles.length));
        }
      },
      { rootMargin: "600px 0px" },
    );
    ob.observe(el);
    return () => ob.disconnect();
  }, [viewMode, shownFiles.length, limit]);

  const openFile = useCallback(
    (f: BrowseFile, el?: HTMLElement | null) => {
      const pv = previewKind(f.name, f.ext);
      const url = fileUrl(f.name);
      if (pv) {
        setNotice(null);
        setPreviewFull(false);
        setPreview({ url, kind: pv, name: f.name, ext: f.ext, anchor: el ?? null });
        return;
      }
      const e = (f.ext || "").toLowerCase().replace(/^\./, "");
      // 文本、代码、PDF 这类浏览器自己就能渲染，照旧新标签打开，不在页内再套一层
      if (BROWSER_VIEWABLE.has(e) || kindOf(f.name, f.ext) === "skill") {
        window.open(url, "_blank", "noopener");
        return;
      }
      // 剩下的格式：给个说明，别开出空白页
      setPreview(null);
      setNotice({ name: f.name, ext: e, url, anchor: el ?? null });
    },
    [fileUrl],
  );

  // 全屏观看时 Esc 先退回面板
  useEffect(() => {
    if (!previewFull) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPreviewFull(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [previewFull]);

  // 切目录时顺手关掉，避免残留上一个文件
  useEffect(() => {
    setPreview(null);
    setPreviewFull(false);
    setNotice(null);
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
                        <a className="btn btn-ghost mat-open" href={siteUrl(it.url)} target="_blank" rel="noreferrer">
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

      {(tab === "browse" || tab === "personal" || tab === "collect") && (
        <section className="mat-section">
          {tab === "personal" && (
            <p className="muted mat-private-hint">
              仅当前登录账号可见，右侧可删除 · 个人空间：<code>conring/users/&lt;手机号&gt;/</code>
              （含 <code>工具箱</code>、<code>obsidian</code>，点面包屑可回到上级）
            </p>
          )}
          {tab === "collect" && (
            <p className="muted mat-private-hint">
              自动采集的输出目录：<code>users/&lt;手机号&gt;/downloads</code>，只你自己可见 ·
              定时任务（周期 / 数据源 / 目标数量 / 任务描述）在这里设，采完由 CCO 结合知识库出灵感，进「创意灵感 → 采集洞察」。
            </p>
          )}
          <div className="mat-browse-bar">
            <div className="mat-roots">
              {tab === "collect" ? (
                <>
                  <span className="mat-root active">自动采集输出</span>
                  <button
                    type="button"
                    className="btn btn-ghost mat-collect-btn"
                    onClick={() => setCollectTasks(true)}
                  >
                    定时任务设置
                  </button>
                </>
              ) : tab === "personal" ? (
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
              <div className="mat-counts">
                <span className="muted">
                  目录 {browse.counts?.dirs ?? 0} 个 · 文件 {browse.counts?.files ?? 0} 个
                  {kind !== "all" && `（当前分类 ${shownFiles.length} 个）`}
                  {browse.skipped ? ` · 跳过 ${browse.skipped} 个不可读项` : ""}
                  {(browse.counts?.files ?? 0) > 500 && "　（仅显示前 500 个文件）"}
                  {viewMode === "grid" && shownFiles.length > limit && `　已铺 ${limit} / ${shownFiles.length}`}
                </span>
                <div className="mat-views" role="group" aria-label="查看方式">
                  <button
                    type="button"
                    className={viewMode === "list" ? "active" : ""}
                    onClick={() => setView("list")}
                    title="一行一个，信息看得全"
                  >
                    列表
                  </button>
                  <button
                    type="button"
                    className={viewMode === "grid" ? "active" : ""}
                    onClick={() => setView("grid")}
                    title="方块缩略图，一屏看很多"
                  >
                    缩略图
                  </button>
                </div>
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
              {tab === "collect" && !browsing
                && (browse.counts?.dirs ?? 0) === 0
                && (browse.counts?.files ?? 0) === 0 && (
                  <div className="tg-empty">
                    <b>还没有采集结果</b>
                    <p>
                      采集任务到点会自动跑，结果落在这里（<code>users/&lt;手机号&gt;/downloads</code>）。
                      也可以现在点上面的「定时任务设置」建一条，或对已有任务点「立即采集」先跑一轮。
                    </p>
                    <button type="button" className="btn" onClick={() => setCollectTasks(true)}>
                      去设定时任务
                    </button>
                  </div>
                )}
              {/* 缩略图墙：目录和文件都铺成方块，滚到底自动续下一屏 */}
              {viewMode === "grid" && (
                <div className="mat-tiles">
                  {browse.dirs?.map((d) => {
                    const relDir = path ? `${path}/${d.name}` : d.name;
                    return (
                      <div className="mat-tile mat-tile-dir" key={`dir-${d.name}`}>
                        <button
                          type="button"
                          className="mat-tile-hit"
                          onClick={() => setPath(relDir)}
                          title={d.name}
                        >
                          <span className="mat-tile-glyph" aria-hidden="true">▸</span>
                          <span className="mat-tile-cap">
                            <b>{d.name}</b>
                            <small>目录</small>
                          </span>
                        </button>
                        {(effRoot === "private" || effRoot === "collect") && (
                          <button
                            type="button"
                            className="mat-tile-del"
                            disabled={delBusy === relDir}
                            onClick={() => void doDelete(relDir, true)}
                            title="删除这个目录（连同里面的内容）"
                          >
                            {delBusy === relDir ? "…" : "×"}
                          </button>
                        )}
                      </div>
                    );
                  })}
                  {shownFiles.slice(0, limit).map((f) => {
                    const k = kindOf(f.name, f.ext);
                    const relPath = [...path.split("/").filter(Boolean), f.name].join("/");
                    return (
                      <div className={`mat-tile mat-tile-${k}`} key={`file-${f.name}`}>
                        <button
                          type="button"
                          className="mat-tile-hit"
                          onClick={(e) => openFile(f, e.currentTarget)}
                          title={`${f.name}　${humanSize(f.size)}`}
                        >
                          {/* 底层永远是类型封面：能出图的格式会被缩略图盖住，出不了的直接露出扩展名 */}
                          <span className={`mat-tile-type tone-${extTone(f.ext, k)}`} aria-hidden="true">
                            <span className="mat-tile-type-ext">{extLabel(f.ext, k)}</span>
                          </span>
                          {k === "video" && <span className="mat-tile-flag" aria-hidden="true">▶</span>}
                          {k === "audio" && <span className="mat-tile-flag" aria-hidden="true">♫</span>}
                          {(k === "image" || k === "video") && (
                            <img
                              className="mat-tile-img"
                              src={thumbUrl(f.name)}
                              alt=""
                              loading="lazy"
                              decoding="async"
                              onError={(e) => { e.currentTarget.style.display = "none"; }}
                            />
                          )}
                          <span className="mat-tile-cap">
                            <b>{f.name}</b>
                            <small>
                              {f.ext ? `${f.ext.toUpperCase()} · ` : ""}{humanSize(f.size)}
                            </small>
                          </span>
                        </button>
                        {(effRoot === "private" || effRoot === "collect") && (
                          <button
                            type="button"
                            className="mat-tile-del"
                            disabled={delBusy === relPath}
                            onClick={() => void doDelete(relPath, false)}
                            title="删除这个文件"
                          >
                            {delBusy === relPath ? "…" : "×"}
                          </button>
                        )}
                      </div>
                    );
                  })}
                  <div ref={gridEndRef} className="mat-grid-end" aria-hidden="true" />
                </div>
              )}
              {viewMode === "list" && (browse.dirs?.length ?? 0) > 0 && (
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
                        {(effRoot === "private" || effRoot === "collect") && (
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
              {viewMode === "list" && shownFiles.length > 0 && (
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
                            onClick={(e) => openFile(f, e.currentTarget)}
                            title={pv === "image" ? "在当前页查看图片" : "在当前页播放"}
                          >
                            {pv === "image" ? "查看" : "播放"}
                          </button>
                        )}
                        {(effRoot === "private" || effRoot === "collect") && (
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

      {/* 页内预览：面板贴着被点的方块/按钮弹出来，不用滚动去找 */}
      {preview && !previewFull && (
        <AnchoredPanel
          anchor={preview.anchor}
          onClose={() => setPreview(null)}
          className="mat-pop mat-pop-media"
          ariaLabel={preview.name}
        >
          <div className="mat-pop-head">
            <strong title={preview.name}>{preview.name}</strong>
            <div className="mat-pop-acts">
              <button type="button" onClick={() => setPreviewFull(true)}>全屏观看</button>
              <a href={preview.url} target="_blank" rel="noreferrer">新窗口</a>
              <button type="button" className="mat-pop-x" onClick={() => setPreview(null)} aria-label="关闭">×</button>
            </div>
          </div>
          <div className="mat-pop-stage">
            <MatMedia kind={preview.kind} url={preview.url} name={preview.name} ext={preview.ext ?? ""} />
          </div>
        </AnchoredPanel>
      )}

      {/* 全屏观看：整个屏幕给画面，左上角随时能返回 */}
      {preview && previewFull && (
        <div className="mat-fullview" role="dialog" aria-modal="true" aria-label={preview.name}>
          <div className="mat-fullview-bar">
            <button type="button" className="mat-fullview-back" onClick={() => setPreviewFull(false)}>
              ← 返回
            </button>
            <strong title={preview.name}>{preview.name}</strong>
            <a href={preview.url} target="_blank" rel="noreferrer">新窗口</a>
            <button type="button" className="mat-fullview-x" onClick={() => setPreview(null)} aria-label="关闭">×</button>
          </div>
          <div className="mat-fullview-stage">
            {preview.kind !== "audio" && (
              <MatMedia kind={preview.kind} url={preview.url} name={preview.name} ext={preview.ext ?? ""} full />
            )}
            {preview.kind === "audio" && (
              <div className="mat-fullview-audio">
                <p>{preview.name}</p>
                <audio src={preview.url} controls autoPlay />
              </div>
            )}
          </div>
        </div>
      )}

      {/* 浏览器打不开的格式：说清是什么、怎么办 */}
      {notice && (
        <AnchoredPanel
          anchor={notice.anchor}
          onClose={() => setNotice(null)}
          className="mat-pop mat-pop-note"
          ariaLabel={notice.name}
        >
          <div className="mat-pop-head">
            <strong title={notice.name}>{notice.name}</strong>
            <button type="button" className="mat-pop-x" onClick={() => setNotice(null)} aria-label="关闭">×</button>
          </div>
          <p className="mat-note-type">类型：{typeLabel(notice.ext)}</p>
          <p className="mat-note-tip">{typeAdvice(notice.ext)}</p>
          <div className="mat-note-acts">
            <a className="btn btn-ghost" href={notice.url} target="_blank" rel="noreferrer">下载 / 新窗口打开</a>
            <button type="button" className="btn btn-ghost" onClick={() => setNotice(null)}>知道了</button>
          </div>
        </AnchoredPanel>
      )}

      {tab === "idea" && <InspirationPage embedded />}

      {collectTasks && (
        <CollectTasks
          onClose={() => setCollectTasks(false)}
          onRan={() => { void loadBrowse("collect", path); }}
        />
      )}
    </div>
  );
}