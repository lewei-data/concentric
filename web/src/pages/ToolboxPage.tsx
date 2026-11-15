import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { MediaViewer } from "../components/MediaViewer";
import { siteUrl } from "../lib/siteUrl";
import { appRoot } from "../appRoot";

type Kind = "image" | "audio" | "video";

interface ParamSpec {
  key: string;
  label: string;
  type: "int" | "float" | "text" | "select" | string;
  default: unknown;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
  /** 参数说明 + 建议值，由后端渠道注册表给出（模型实际能力推导，不要在前端另编） */
  hint?: string;
}

interface Channel {
  name: string;
  kind: string;
  label: string;
  notes: string;
  group?: string;
  endpoint?: string;
  modes?: string[];
  /** ComfyUI 单队列：前面还有几个任务在跑/排队 */
  queue?: number;
  available: boolean;
  reason: string;
  params: ParamSpec[];
  maxSec?: number | null;
}

interface GroupMeta {
  key: string;
  label: string;
  desc: string;
}

interface VoiceInfo {
  id: string;
  description: string;
  prompt_text: string;
  cloned: boolean;
  sec: number;
  /** 已微调轮次 */
  rounds?: number;
}

interface VoiceProfile {
  id: string;
  count: number;
  maxRounds: number;
  rounds: { n: number; label: string; direction: string; sec: number }[];
  directions: { key: string; label: string }[];
  current: { text: string; sec: number };
}

interface Job {
  id: string;
  kind: string;
  channel: string;
  prompt: string;
  status: string;
  progress: number;
  outputUrl: string;
  error: string;
  note: string;
  durationSec: number;
  createdAt: string;
}

const KINDS: { key: Kind; label: string; sub: string }[] = [
  { key: "image", label: "图片生成", sub: "提示词 → 单张成品" },
  { key: "audio", label: "口播语音", sub: "台词 + 音色 → wav" },
  { key: "video", label: "短视频", sub: "文生视频 → 工厂排队出片" },
];

const STATUS_TEXT: Record<string, string> = {
  queued: "排队中",
  running: "生成中",
  done: "已完成",
  failed: "失败",
  canceled: "已停止",
};

function defaultsOf(ch?: Channel | null): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  (ch?.params || []).forEach((p) => { out[p.key] = p.default; });
  return out;
}

function ParamField({
  spec,
  value,
  onChange,
}: {
  spec: ParamSpec;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const hint = spec.hint ? <small className="tb-hint">{spec.hint}</small> : null;
  if (spec.type === "select") {
    return (
      <label className="tb-field">
        <span>{spec.label}</span>
        <select value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
          {(spec.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
        {hint}
      </label>
    );
  }
  if (spec.type === "int" || spec.type === "float") {
    const num = Number(value);
    return (
      <label className="tb-field">
        <span>{spec.label}</span>
        <input
          type="number"
          value={Number.isFinite(num) ? num : ""}
          min={spec.min}
          max={spec.max}
          step={spec.step ?? (spec.type === "int" ? 1 : 0.1)}
          onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
        />
        {hint}
      </label>
    );
  }
  return (
    <label className="tb-field tb-field--wide">
      <span>{spec.label}</span>
      <input type="text" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />
      {hint}
    </label>
  );
}

/** 从成品 URL 取出所在目录（相对浏览根）。
 *  工具箱成品现在落在个人空间：/private/<手机号>/工具箱/2026-11-15/图片/x.png
 *  历史成品可能还是老的 /vault/素材/... 形态，两种都认。
 */
function matDirOf(url: string): string {
  const p = /^\/private\/\d+\/(.*\/)[^/]*$/.exec(url || "");
  if (p) {
    try {
      return decodeURIComponent(p[1]).replace(/\/$/, "");
    } catch {
      return "";
    }
  }
  const v = /^\/vault\/素材\/(.*\/)[^/]*$/.exec(url || "");
  return v ? v[1].replace(/\/$/, "") : "";
}

function JobRow({ job, onOpen }: { job: Job; onOpen: (j: Job) => void }) {
  const live = job.status === "queued" || job.status === "running";
  const ready = job.status === "done" && !!job.outputUrl;
  const dir = ready ? matDirOf(job.outputUrl) : "";
  return (
    <li className={`tb-job tb-job--${job.status}`}>
      {ready && job.kind === "image" ? (
        <button
          type="button"
          className="tb-job-preview tb-job-preview--btn"
          onClick={() => onOpen(job)}
          title="点开看大图"
        >
          <img src={siteUrl(job.outputUrl)} alt={job.prompt.slice(0, 40)} loading="lazy" />
        </button>
      ) : (
        <div className="tb-job-preview">
          {ready && job.kind === "video" && (
            <video src={siteUrl(job.outputUrl)} preload="metadata" muted />
          )}
          {ready && job.kind === "audio" && (
            <button type="button" className="tb-job-play" onClick={() => onOpen(job)} title="播放">
              ♪
            </button>
          )}
          {!live && !ready && <span className="tb-job-x">×</span>}
          {live && <span className="tb-job-pct">{job.progress}%</span>}
        </div>
      )}
      <div className="tb-job-main">
        <p className="tb-job-prompt">{job.prompt || job.note || "（无提示词）"}</p>
        <div className="tb-job-meta">
          <span className={`tb-pill tb-pill--${job.status}`}>{STATUS_TEXT[job.status] || job.status}</span>
          <span>{job.channel}</span>
          {job.durationSec ? <span>{Number(job.durationSec).toFixed(1)}s</span> : null}
          <span>{String(job.createdAt).replace("T", " ").slice(5, 16)}</span>
        </div>
        {live && (
          <div className="tb-bar"><i style={{ width: `${Math.max(2, job.progress)}%` }} /></div>
        )}
        {job.error && <p className="tb-job-error">{job.error}</p>}
        {job.status === "done" && !job.outputUrl && (
          <p className="muted">成品已从素材页删除</p>
        )}
        {ready && (
          <div className="tb-job-links">
            <button type="button" className="tb-link" onClick={() => onOpen(job)}>
              打开查看
            </button>
            <a className="tb-link" href={siteUrl(job.outputUrl)} target="_blank" rel="noreferrer">
              新窗口
            </a>
            {/* 整页跳转：让素材页按深链初始化 path（SPA 内跳会被切 tab 的重置逻辑覆盖） */}
            {dir && (
              <a
                className="tb-link"
                href={`${appRoot()}app/materials?tab=personal&path=${encodeURIComponent(dir)}`}
              >
                所在目录
              </a>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

export function ToolboxPage() {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [groups, setGroups] = useState<GroupMeta[]>([]);
  const [cap, setCap] = useState(0);
  const [modes, setModes] = useState<{ i2v: boolean; i2vReason: string }>({ i2v: false, i2vReason: "" });
  const [loading, setLoading] = useState(true);
  const [fatal, setFatal] = useState("");

  const [kind, setKind] = useState<Kind>("image");
  const [chName, setChName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [line, setLine] = useState("");
  const [sec, setSec] = useState(3);
  const [mode, setMode] = useState<"t2v" | "i2v">("t2v");
  const [frame, setFrame] = useState<{ name: string; url: string } | null>(null);
  const [frameBusy, setFrameBusy] = useState(false);
  const [vals, setVals] = useState<Record<string, unknown>>({});

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [toast, setToast] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [zoom, setZoom] = useState<Job | null>(null);

  // 音色克隆（口播语音面板）
  const [voices, setVoices] = useState<VoiceInfo[]>([]);
  const [cloneOpen, setCloneOpen] = useState(false);
  const [cloneFile, setCloneFile] = useState<File | null>(null);
  const [cloneName, setCloneName] = useState("");
  const [cloneText, setCloneText] = useState("");
  const [cloneBusy, setCloneBusy] = useState(false);

  // 网页录音（克隆参考 / 微调换参考共用一套录音器）
  const recRef = useRef<{ mr: MediaRecorder; timer: number } | null>(null);
  const [recOn, setRecOn] = useState(false);
  const [recSec, setRecSec] = useState(0);
  const [recTarget, setRecTarget] = useState<"clone" | "tune">("clone");

  // 音色微调：按方向一轮轮叠加，每轮可试听 / 可撤销
  const [tuneFor, setTuneFor] = useState("");
  const [tuneDir, setTuneDir] = useState("");
  const [tuneFile, setTuneFile] = useState<File | null>(null);
  const [tuneText, setTuneText] = useState("");
  const [tuneBusy, setTuneBusy] = useState(false);
  const [tuneInfo, setTuneInfo] = useState<VoiceProfile | null>(null);

  const kindChans = useMemo(() => channels.filter((c) => c.kind === kind), [channels, kind]);
  const active = useMemo(
    () => kindChans.find((c) => c.name === chName) ?? null,
    [kindChans, chName],
  );

  // 按服务分组渲染（ComfyUI 远程工作站 / 局域网媒体服务），顺序由后端给
  const grouped = useMemo(() => {
    const order = groups.map((g) => g.key);
    const buckets = new Map<string, Channel[]>();
    kindChans.forEach((c) => {
      const k = c.group || "lan";
      buckets.set(k, [...(buckets.get(k) || []), c]);
    });
    return [...buckets.entries()]
      .sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]))
      .map(([key, items]) => ({
        meta: groups.find((g) => g.key === key) || { key, label: key, desc: "" },
        items,
      }));
  }, [kindChans, groups]);

  // 图生视频要「全局有可用渠道」且「当前渠道自己支持 i2v」
  const chanI2v = !!active?.modes?.includes("i2v");
  const canI2v = modes.i2v && chanI2v;

  const loadChannels = useCallback(async () => {
    setLoading(true);
    setFatal("");
    try {
      // 字面量 /api/... 不用补前缀：公网 nginx 会把 JS 里的 "/api/ 改写成 "/workbench/api/
      const r = await fetch("/api/toolbox/channels", { credentials: "include" });
      const d = await r.json() as {
        ok?: boolean; error?: string; channels?: Channel[]; groups?: GroupMeta[]; maxSec?: number;
        modes?: { i2v: boolean; i2vReason: string };
      };
      if (!r.ok || !d.ok) { setFatal(d.error || "渠道清单读取失败"); return; }
      setChannels(d.channels || []);
      setGroups(d.groups || []);
      setCap(Number(d.maxSec) || 6);
      if (d.modes) setModes(d.modes);
    } catch {
      setFatal("渠道清单读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadJobs = useCallback(async () => {
    try {
      const r = await fetch("/api/toolbox/jobs", { credentials: "include" });
      const d = await r.json() as { ok?: boolean; items?: Job[] };
      if (d.ok) setJobs(d.items || []);
    } catch {
      /* 轮询失败不打扰用户 */
    }
  }, []);

  useEffect(() => { void loadChannels(); }, [loadChannels]);

  useEffect(() => {
    void loadJobs();
    const t = window.setInterval(() => { void loadJobs(); }, 4000);
    return () => window.clearInterval(t);
  }, [loadJobs]);

  // 切类型时自动落到第一个可用渠道，并把参数重置为该渠道默认值
  useEffect(() => {
    if (loading) return;
    const usable = kindChans.filter((c) => c.available);
    const pick = usable[0] || kindChans[0];
    setChName(pick ? pick.name : "");
    setVals(defaultsOf(pick));
  }, [kindChans, loading]);

  useEffect(() => {
    if (active) setVals(defaultsOf(active));
    // 换到不支持首帧的渠道时别把「图生视频」留在选中态，否则用户以为能出片
    if (active && !(active.modes || []).includes("i2v")) setMode("t2v");
  }, [active?.name]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(""), 4000);
    return () => window.clearTimeout(t);
  }, [toast]);

  

  const submit = async () => {
    setErr("");
    if (!active) { setErr("请选择渠道"); return; }
    if (!active.available) { setErr(`渠道不可用：${active.reason || "未就绪"}`); return; }
    if (mode === "i2v") {
      if (!chanI2v) { setErr(modes.i2vReason || "当前渠道不支持图生视频"); return; }
      if (!frame) { setErr("图生视频需要先选一张首帧图"); return; }
    }
    setBusy(true);
    try {
      const r = await fetch("/api/toolbox/jobs", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind,
          channels: [active.name],
          prompt: kind === "audio" ? "" : prompt,
          line: kind === "audio" ? line : "",
          sec: kind === "video" ? sec : 0,
          params: { [active.name]: vals },
          frame: kind === "video" && mode === "i2v" && frame ? frame.name : "",
        }),
      });
      const d = await r.json() as { ok?: boolean; error?: string; note?: string; warnings?: string[] };
      if (!r.ok || !d.ok) { setErr(d.error || "提交失败"); return; }
      setToast(d.note || "已发布任务到工厂");
      if (d.warnings?.length) setErr(d.warnings.join("；"));
      void loadJobs();
    } catch {
      setErr("提交请求失败");
    } finally {
      setBusy(false);
    }
  };

  const canSubmit = !busy && !!active && active.available
    && (kind === "audio" ? !!line.trim() : !!prompt.trim())
    && (kind !== "video" || mode === "t2v" || (canI2v && !!frame));

  /** 首帧上传：直传二进制，后端落 .cache/frames，提交时只带文件名 */
  const uploadFrame = async (file: File) => {
    setErr("");
    setFrameBusy(true);
    try {
      const r = await fetch("/api/toolbox/frame", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      });
      const d = await r.json() as { ok?: boolean; frame?: string; error?: string };
      if (!r.ok || !d.ok || !d.frame) { setErr(d.error || "首帧上传失败"); return; }
      setFrame({ name: d.frame, url: URL.createObjectURL(file) });
    } catch {
      setErr("首帧上传失败");
    } finally {
      setFrameBusy(false);
    }
  };

  /** 音色表：media-engine 的 voices.json，clone- 开头的是自己克隆的 */
  const loadVoices = async () => {
    try {
      const r = await fetch("/api/toolbox/voices", { credentials: "include" });
      const d = await r.json() as { ok?: boolean; voices?: VoiceInfo[] };
      if (d.ok) setVoices(d.voices || []);
    } catch { /* 音色表读不到不阻塞配音 */ }
  };

  const doClone = async () => {
    if (!cloneFile) { setErr("请先选择参考录音"); return; }
    setErr(""); setToast(""); setCloneBusy(true);
    try {
      const q = new URLSearchParams({ name: cloneName.trim(), text: cloneText.trim() });
      const r = await fetch(`/api/toolbox/voice/clone?${q.toString()}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": cloneFile.type || "application/octet-stream" },
        body: cloneFile,
      });
      const d = await r.json() as {
        ok?: boolean; id?: string; error?: string; warning?: string; sec?: number;
      };
      if (!r.ok || !d.ok || !d.id) { setErr(d.error || "克隆失败"); return; }
      setCloneFile(null); setCloneName(""); setCloneText("");
      setToast(d.warning || `已创建音色 ${d.id}（参考 ${d.sec ?? 0}s），可从上方音色下拉直接使用`);
      await loadVoices();
      await loadChannels();               // 音色下拉随之刷新
      if ((active?.params || []).some((p) => p.key === "voice")) {
        setVals((prev) => ({ ...prev, voice: d.id as string }));
      }
    } catch {
      setErr("克隆失败：网络错误");
    } finally {
      setCloneBusy(false);
    }
  };

  const delVoice = async (id: string) => {
    if (!window.confirm(`删除音色 ${id}？已生成的配音不受影响`)) return;
    try {
      const r = await fetch("/api/toolbox/voice/delete", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      const d = await r.json() as { ok?: boolean; error?: string };
      if (!r.ok || !d.ok) { setErr(d.error || "删除失败"); return; }
      setToast(`已删除 ${id}`);
      await loadVoices();
      await loadChannels();
    } catch {
      setErr("删除失败：网络错误");
    }
  };

  useEffect(() => {
    void loadVoices();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  /** 录音最长 15 秒：太长 zero-shot 反而漂，用户重录也比等长录音快 */
  const REC_MAX = 15;

  const stopRec = useCallback(() => {
    const r = recRef.current;
    if (!r) return;
    window.clearInterval(r.timer);
    recRef.current = null;
    setRecOn(false);
    try { r.mr.stop(); } catch { /* 已经停了 */ }
  }, []);

  // 录音中切走页面：一定要把麦克风关掉，否则浏览器/系统一直亮着录音图标
  useEffect(() => () => {
    const r = recRef.current;
    if (r) {
      window.clearInterval(r.timer);
      try { r.mr.stream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
      recRef.current = null;
    }
  }, []);

  const startRec = async (target: "clone" | "tune") => {
    setErr("");
    if (typeof MediaRecorder === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      setErr("当前浏览器不支持网页录音；另外网页录音要求 HTTPS 或 localhost 访问，内网 http 地址会被浏览器禁用，请改用上传文件");
      return;
    }
    if (!window.isSecureContext) {
      setErr("当前地址不是 HTTPS（或 localhost），浏览器禁止网页录音，请用 https 域名访问，或改用上传文件");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus"
        : MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "";
      const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      const chunks: Blob[] = [];
      mr.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      mr.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const type = mr.mimeType || "audio/webm";
        const ext = type.includes("mp4") ? "mp4" : type.includes("ogg") ? "ogg" : "webm";
        const stamp = new Date().toTimeString().slice(0, 8).replace(/:/g, "");
        const file = new File([new Blob(chunks, { type })], `录音-${stamp}.${ext}`, { type });
        if (target === "clone") {
          setCloneFile(file);
          setCloneName((n) => n || "我的音色");
        } else {
          setTuneFile(file);
        }
        setToast("录音已就绪，记得把刚才说的那句话逐字填进「参考文本」");
      };
      mr.start(200);
      setRecSec(0);
      setRecTarget(target);
      setRecOn(true);
      recRef.current = {
        mr,
        timer: window.setInterval(() => {
          setRecSec((prev) => {
            const n = prev + 1;
            if (n >= REC_MAX) window.setTimeout(() => stopRec(), 0);
            return n;
          });
        }, 1000),
      };
    } catch (e) {
      const name = (e as { name?: string })?.name || "";
      setErr(name === "NotAllowedError" || name === "SecurityError"
        ? "浏览器没有给麦克风权限：请点地址栏的图标允许麦克风后重试，或改用上传文件"
        : "打不开麦克风（可能被其他程序占用），请检查系统录音设备后重试");
    }
  };

  const loadTune = async (id: string) => {
    try {
      const r = await fetch(`/api/toolbox/voice/profile?id=${encodeURIComponent(id)}`, { credentials: "include" });
      const d = await r.json() as VoiceProfile & { ok?: boolean; error?: string };
      if (d.ok) setTuneInfo(d);
    } catch { /* 档案读不到就只展示方向，微调本身仍可用 */ }
  };

  const openTune = async (id: string) => {
    if (tuneFor === id) { setTuneFor(""); return; }
    setTuneFor(id);
    setTuneDir("");
    setTuneFile(null);
    setTuneText("");
    setTuneInfo(null);
    await loadTune(id);
  };

  const doTune = async () => {
    if (!tuneFor) return;
    if (!tuneDir && !tuneFile) { setErr("请选一个微调方向，或换一段参考录音"); return; }
    if (tuneFile && !tuneText.trim()) { setErr("换了新录音，就要填这段录音里逐字说的话"); return; }
    setErr(""); setToast(""); setTuneBusy(true);
    try {
      const q = new URLSearchParams({
        id: tuneFor, direction: tuneDir, text: tuneFile ? tuneText.trim() : "",
      });
      const r = await fetch(`/api/toolbox/voice/tune?${q.toString()}`, {
        method: "POST",
        credentials: "include",
        headers: tuneFile ? { "Content-Type": tuneFile.type || "application/octet-stream" } : undefined,
        body: tuneFile || undefined,
      });
      const d = await r.json() as {
        ok?: boolean; rounds?: number; label?: string; error?: string; warning?: string;
      };
      if (!r.ok || !d.ok) { setErr(d.error || "微调失败"); return; }
      setToast(d.warning || `第 ${d.rounds} 轮微调完成（${d.label}），点上面的播放器听听像不像`);
      setTuneDir(""); setTuneFile(null); setTuneText("");
      await loadVoices();
      await loadChannels();
      const cur = tuneFor;
      // 刷新渠道参数会把音色选回默认值，这里把它拨回刚微调的这个音色
      if ((active?.params || []).some((p) => p.key === "voice")) {
        setVals((prev) => ({ ...prev, voice: cur }));
      }
      await loadTune(cur);
    } catch {
      setErr("微调失败：网络错误");
    } finally {
      setTuneBusy(false);
    }
  };

  const undoTune = async (id: string) => {
    setErr(""); setTuneBusy(true);
    try {
      const r = await fetch("/api/toolbox/voice/undo", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      const d = await r.json() as { ok?: boolean; rounds?: number; undone?: string; error?: string };
      if (!r.ok || !d.ok) { setErr(d.error || "撤销失败"); return; }
      setToast(d.undone ? `已撤销「${d.undone}」，回到上一版音色` : "已撤销上一轮微调");
      await loadVoices();
      if ((active?.params || []).some((p) => p.key === "voice")) {
        setVals((prev) => ({ ...prev, voice: id }));
      }
      await loadTune(id);
    } catch {
      setErr("撤销失败：网络错误");
    } finally {
      setTuneBusy(false);
    }
  };

  const clonedVoices = voices.filter((v) => v.cloned);

  return (
    <div className="page tb-page">
      <div className="page-head">
        <div>
          <h1>工具箱</h1>
          <p className="tb-sub">
            单技能手工验证 · 提交即进工厂队列，无需走完整流水线 · 成品统一落到「素材」
          </p>
        </div>
        <Link className="btn btn-ghost" to="/materials">去素材查看</Link>
      </div>

      <div className="tb-kinds" role="tablist" aria-label="技能类型">
        {KINDS.map((k) => (
          <button
            key={k.key}
            type="button"
            role="tab"
            aria-selected={kind === k.key}
            className={`tb-kind ${kind === k.key ? "active" : ""}`}
            onClick={() => setKind(k.key)}
          >
            <b>{k.label}</b>
            <small>{k.sub}</small>
          </button>
        ))}
      </div>

      {fatal && <div className="error-banner">{fatal}</div>}

      {loading ? (
        <p className="muted">正在探测可用渠道…</p>
      ) : (
        <div className="tb-grid">
          <section className="tb-card">
            <h2 className="tb-card-title">参数</h2>

            {grouped.map(({ meta, items }) => (
              <div key={meta.key} className="tb-chan-group">
                <div className="tb-chan-group-head">
                  <b>{meta.label}</b>
                  {meta.desc && <small>{meta.desc}</small>}
                </div>
                <div className="tb-chans">
                  {items.map((c) => (
                    <button
                      key={c.name}
                      type="button"
                      className={`tb-chan ${chName === c.name ? "active" : ""} ${c.available ? "" : "off"}`}
                      onClick={() => setChName(c.name)}
                      title={c.notes}
                    >
                      <b>
                        {c.label}
                        {c.endpoint && <em className="tb-chan-ep">{c.endpoint}</em>}
                        {c.available && !!c.queue && (
                          <em className="tb-chan-ep tb-chan-ep--busy">排队 {c.queue}</em>
                        )}
                      </b>
                      <small>{c.available ? c.notes : (c.reason || "不可用")}</small>
                    </button>
                  ))}
                </div>
              </div>
            ))}
            {kindChans.length === 0 && <p className="muted">该类型暂无渠道</p>}

            {kind === "video" && (
              <div className="tb-modes">
                <button
                  type="button"
                  className={`tb-mode ${mode === "t2v" ? "active" : ""}`}
                  onClick={() => setMode("t2v")}
                >
                  文生视频
                </button>
                <button
                  type="button"
                  className={`tb-mode ${mode === "i2v" ? "active" : ""}`}
                  disabled={!canI2v}
                  title={chanI2v ? modes.i2vReason : "当前渠道没有首帧输入能力"}
                  onClick={() => { if (canI2v) setMode("i2v"); }}
                >
                  图生视频（首帧）
                </button>
                {!canI2v && (
                  <span className="tb-mode-note">
                    {chanI2v ? modes.i2vReason : "当前渠道不支持图生视频，切到「ComfyUI · Wan2.2 TI2V 5B」再试"}
                  </span>
                )}
              </div>

            )}
            {kind === "video" && mode === "i2v" && canI2v && (
              <div className="tb-frame">
                {frame ? (
                  <img className="tb-frame-img" src={frame.url} alt="首帧" />
                ) : (
                  <div className="tb-frame-img tb-frame-img--empty">未选</div>
                )}
                <div className="tb-frame-main">
                  <label className="tb-frame-pick">
                    <input
                      type="file"
                      accept="image/png,image/jpeg,image/webp"
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        if (f) void uploadFrame(f);
                        e.target.value = "";
                      }}
                    />
                    <span>{frameBusy ? "上传中…" : frame ? "更换首帧" : "选择首帧图"}</span>
                  </label>
                  <small className="tb-hint">
                    首帧决定构图、人物和画幅，视频第一帧就是它。PNG / JPG / WebP，≤ 8MB；
                    建议先用「图片生成」出一张满意的大图再拿来当首帧
                  </small>
                </div>
              </div>
            )}

            {kind === "audio" ? (
              <>
                <label className="tb-field tb-field--wide">
                  <span>台词</span>
                  <textarea
                    rows={5}
                    value={line}
                    placeholder="要念出来的内容，标点会影响停顿…"
                    onChange={(e) => setLine(e.target.value)}
                  />
                </label>

                <div className="tb-clone">
                  <button
                    type="button"
                    className="tb-clone-toggle"
                    onClick={() => setCloneOpen((v) => !v)}
                  >
                    <b>克隆音色</b>
                    <small>
                      用自己的录音建一个音色，之后在「音色」下拉里直接选它配音
                      {clonedVoices.length > 0 ? ` · 已有 ${clonedVoices.length} 个` : ""}
                    </small>
                    <em>{cloneOpen ? "收起" : "展开"}</em>
                  </button>

                  {cloneOpen && (
                    <div className="tb-clone-body">
                      <div className="tb-rec-row">
                        <label className="tb-frame-pick">
                          <input
                            type="file"
                            accept="audio/wav,audio/mpeg,audio/mp4,audio/x-m4a,audio/ogg,.wav,.mp3,.m4a,.ogg"
                            onChange={(e) => {
                              const f = e.target.files?.[0] || null;
                              setCloneFile(f);
                              if (f && !cloneName) setCloneName(f.name.replace(/\.[^.]+$/, ""));
                              e.target.value = "";
                            }}
                          />
                          <span>{cloneFile ? cloneFile.name : "选择参考录音（3–15 秒人声）"}</span>
                        </label>
                        {recOn && recTarget === "clone" ? (
                          <button type="button" className="tb-rec-btn tb-rec-on" onClick={stopRec}>
                            <i className="tb-rec-dot" />录音中 {recSec}s · 停止并采用
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="tb-rec-btn"
                            disabled={recOn}
                            onClick={() => void startRec("clone")}
                          >
                            立即录音
                          </button>
                        )}
                      </div>
                      <small className="tb-hint">
                        直接用电脑麦克风录一段：点「立即录音」→ 正常说一句话（3–15 秒）→ 停止并采用。
                        录音需要 https 或 localhost 访问，内网 http 地址浏览器会禁用麦克风。
                      </small>

                      <label className="tb-field tb-field--wide">
                        <span>参考文本（必填）</span>
                        <textarea
                          rows={2}
                          value={cloneText}
                          placeholder="把这段录音里说的那句话逐字写下来…"
                          onChange={(e) => setCloneText(e.target.value)}
                        />
                        <small className="tb-hint">
                          必须一字不差地照抄录音内容：CosyVoice 靠它对齐音色，
                          写错或抄短会合成出几乎无声的音频（后端会当场拦住并回滚）
                        </small>
                      </label>

                      <label className="tb-field">
                        <span>音色名称</span>
                        <input
                          type="text"
                          value={cloneName}
                          placeholder="例如 老板、阿May"
                          onChange={(e) => setCloneName(e.target.value)}
                        />
                        <small className="tb-hint">会作为音色 id 出现在下拉里（自动加 clone- 前缀）</small>
                      </label>

                      <button
                        type="button"
                        className="btn"
                        disabled={cloneBusy || !cloneFile || !cloneText.trim()}
                        onClick={() => void doClone()}
                      >
                        {cloneBusy ? "克隆中…（约 10–30 秒）" : "开始克隆"}
                      </button>

                      {clonedVoices.length > 0 && (
                        <div className="tb-clone-list">
                          {clonedVoices.map((v) => (
                            <div key={v.id} className="tb-clone-item">
                              <div className="tb-clone-meta">
                                <b>{v.id.replace(/^clone-/, "")}</b>
                                <small>
                                  {v.description || `参考 ${v.sec}s`}
                                  {v.rounds ? ` · 已微调 ${v.rounds} 轮` : ""}
                                </small>
                              </div>
                              <audio
                                controls
                                preload="none"
                                src={`/api/toolbox/voice/ref?id=${encodeURIComponent(v.id)}&v=${v.rounds || 0}`}
                              />
                              <button
                                type="button"
                                className="tb-clone-tune"
                                onClick={() => void openTune(v.id)}
                              >
                                {tuneFor === v.id ? "收起" : "微调"}
                              </button>
                              <button
                                type="button"
                                className="tb-clone-del"
                                onClick={() => void delVoice(v.id)}
                              >
                                删除
                              </button>

                              {tuneFor === v.id && (
                                <div className="tb-tune">
                                  <div className="tb-tune-dirs">
                                    {(tuneInfo?.directions || []).map((d) => (
                                      <button
                                        key={d.key}
                                        type="button"
                                        className={`tb-chip${tuneDir === d.key ? " on" : ""}`}
                                        onClick={() => setTuneDir(tuneDir === d.key ? "" : d.key)}
                                      >
                                        {d.label}
                                      </button>
                                    ))}
                                  </div>
                                  <small className="tb-hint">
                                    每点一次「应用微调」就往这个方向挪一小步，可以反复叠加不同方向，
                                    一轮轮逼近想要的音色；每轮都会当场试合成一句，听不像就撤销。
                                  </small>
                                  <div className="tb-rec-row">
                                    <label className="tb-frame-pick">
                                      <input
                                        type="file"
                                        accept="audio/wav,audio/mpeg,audio/mp4,audio/x-m4a,audio/ogg,.wav,.mp3,.m4a,.ogg,.webm"
                                        onChange={(e) => {
                                          setTuneFile(e.target.files?.[0] || null);
                                          e.target.value = "";
                                        }}
                                      />
                                      <span>{tuneFile ? tuneFile.name : "换一段更理想的参考录音（可选）"}</span>
                                    </label>
                                    {recOn && recTarget === "tune" ? (
                                      <button type="button" className="tb-rec-btn tb-rec-on" onClick={stopRec}>
                                        <i className="tb-rec-dot" />录音中 {recSec}s · 停止并采用
                                      </button>
                                    ) : (
                                      <button
                                        type="button"
                                        className="tb-rec-btn"
                                        disabled={recOn}
                                        onClick={() => void startRec("tune")}
                                      >
                                        立即录音
                                      </button>
                                    )}
                                  </div>
                                  {tuneFile && (
                                    <label className="tb-field tb-field--wide">
                                      <span>新录音的参考文本（必填）</span>
                                      <textarea
                                        rows={2}
                                        value={tuneText}
                                        placeholder="把新录音里说的那句话逐字写下来…"
                                        onChange={(e) => setTuneText(e.target.value)}
                                      />
                                    </label>
                                  )}
                                  <div className="tb-tune-foot">
                                    <small>
                                      {tuneInfo
                                        ? `已微调 ${tuneInfo.count}/${tuneInfo.maxRounds} 轮` +
                                          (tuneInfo.rounds.length
                                            ? `：${tuneInfo.rounds.map((r) => r.label).join(" → ")}`
                                            : "")
                                        : "正在读取微调档案…"}
                                    </small>
                                    <button
                                      type="button"
                                      className="btn"
                                      disabled={tuneBusy || (!tuneDir && !tuneFile)}
                                      onClick={() => void doTune()}
                                    >
                                      {tuneBusy ? "微调中…" : "应用微调"}
                                    </button>
                                    <button
                                      type="button"
                                      className="tb-clone-del"
                                      disabled={tuneBusy || !(tuneInfo?.count)}
                                      onClick={() => void undoTune(v.id)}
                                    >
                                      撤销上一轮
                                    </button>
                                  </div>
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </>
            ) : (
              <label className="tb-field tb-field--wide">
                <span>提示词</span>
                <textarea
                  rows={5}
                  value={prompt}
                  placeholder={kind === "image" ? "画面内容、风格、构图、光线…" : "主体动作、镜头运动、场景氛围…"}
                  onChange={(e) => setPrompt(e.target.value)}
                />
              </label>
            )}

            {kind === "video" && (
              <label className="tb-field">
                <span>时长（秒，上限 {cap}s）</span>
                <input
                  type="number"
                  value={sec}
                  min={1}
                  max={cap}
                  step={1}
                  onChange={(e) => setSec(Math.min(cap, Math.max(1, Number(e.target.value) || 1)))}
                />
              </label>
            )}

            <div className="tb-params">
              {(active?.params || []).map((p) => (
                <ParamField
                  key={p.key}
                  spec={p}
                  value={vals[p.key]}
                  onChange={(v) => setVals((prev) => ({ ...prev, [p.key]: v }))}
                />
              ))}
            </div>

            {err && <div className="error-banner">{err}</div>}
            {toast && <div className="tb-toast">{toast}</div>}

            <button
              type="button"
              className="btn tb-submit"
              disabled={!canSubmit}
              onClick={() => void submit()}
            >
              {busy ? "提交中…" : "生成"}
            </button>
          </section>

          <section className="tb-card tb-card--jobs">
            <h2 className="tb-card-title">
              最近任务
              <small>每 4 秒刷新</small>
            </h2>
            {jobs.length === 0 ? (
              <p className="muted">还没有手工任务。</p>
            ) : (
              <ul className="tb-jobs">
                {jobs.map((j) => <JobRow key={j.id} job={j} onOpen={setZoom} />)}
              </ul>
            )}
          </section>
        </div>
      )}

      <MediaViewer
        item={zoom ? {
          url: zoom.outputUrl,
          kind: zoom.kind,
          title: `${zoom.channel} · ${zoom.prompt.slice(0, 50) || zoom.kind}`,
        } : null}
        onClose={() => setZoom(null)}
      />
    </div>
  );
}