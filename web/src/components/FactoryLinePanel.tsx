import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import type { FactoryLine } from "../factoryLines";
import { siteUrl } from "../lib/siteUrl";
import { AnchoredPanel } from "./AnchoredPanel";
import { MediaViewer, type ViewerItem } from "./MediaViewer";

/** 主页面的产品摘要（与 FactoryPage 的 Product 兼容） */
export interface ProductLite {
  itemId: string;
  traceId: string;
  serialNo: string;
  title: string;
  workshopNo: string;
  craftName: string;
  shippedAt: string;
  run: { status: string; stage: string };
}

interface ParamSpec {
  key: string;
  label: string;
  type: "select" | "int" | "float" | "text";
  default: unknown;
  options?: string[];
  /** 选项对应的中文说明，后端渠道注册表直接给 */
  optionLabels?: Record<string, string>;
  min?: number;
  max?: number;
  step?: number;
  hint?: string;
}

interface ChannelSpec {
  title: string;
  limits: string[];
  presets?: { name: string; values: Record<string, unknown> }[];
}

interface Channel {
  name: string;
  label: string;
  kind: string;
  group?: string;
  endpoint?: string;
  notes: string;
  available: boolean;
  reason: string;
  modes?: string[];
  queue?: number;
  params: ParamSpec[];
  /** 出图/出片规格红线，后端渠道注册表给，前端不硬编码 */
  spec?: ChannelSpec;
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

/** 标准化流程的可编辑工单：5 道工序共用一份草稿 */
interface Workflow {
  /** 第 1 步：台词/提示词，一行一条 */
  text: string;
  /** 第 2 步：渠道 + 音色 */
  channel: string;
  voice: string;
  /** 第 3 步：选中渠道的参数 */
  params: Record<string, unknown>;
  /** 第 4 步：配乐备注 */
  bgm: string;
  volume: number;
  /** 第 5 步：交付说明 */
  output: string;
}

const BLANK_WORKFLOW: Workflow = {
  text: "", channel: "", voice: "", params: {}, bgm: "", volume: 0.16, output: "",
};

const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "加工中",
  done: "已完成",
  failed: "失败",
  canceled: "已停止",
};

/** 流水线阶段 → 所属要素车间（用于「关联在制流水线」） */
const STAGE_LINE: Record<string, string> = {
  assessment: "text",
  copy: "text",
  sample: "text",
  sample_reel: "video",
  render: "video",
  sample_review: "video",
  final: "video",
  review: "video",
};

function shortTime(value?: string) {
  return value ? value.replace("T", " ").slice(5, 19) : "-";
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include", cache: "no-store" });
  if (res.status === 401) {
    window.location.assign("/app/login");
    throw new Error("登录已过期");
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

function defaultsOf(ch?: Channel | null): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  (ch?.params || []).forEach((p) => { out[p.key] = p.default; });
  return out;
}

/** 渠道里的音色字段：CosyVoice/百炼叫 voice，Qwen3-TTS 叫 speaker */
function voiceSpecOf(ch?: Channel | null): ParamSpec | undefined {
  return (ch?.params || []).find((p) => p.key === "voice" || p.key === "speaker");
}

function labelOf(spec: ParamSpec | undefined, value: unknown): string {
  const v = String(value ?? "");
  if (!v) return "未选";
  return spec?.optionLabels?.[v] || v;
}

/** 规格红线：直接展示后端渠道注册表里的限制，避免前端各写一份走样 */
function SpecBlock({ spec, current }: { spec?: ChannelSpec; current?: Record<string, unknown> }) {
  if (!spec) return null;
  return (
    <div className="fac-spec">
      <b className="fac-spec-title">{spec.title}</b>
      <ul className="fac-spec-list">
        {spec.limits.map((line, i) => <li key={i}>{line}</li>)}
      </ul>
      {current && Object.keys(current).length > 0 && (
        <p className="fac-spec-current">
          当前参数：{Object.entries(current).map(([k, v]) => `${k}=${String(v)}`).join(" · ")}
        </p>
      )}
    </div>
  );
}

function ParamField({
  spec, value, onChange, onPreview, previewing,
}: {
  spec: ParamSpec;
  value: unknown;
  onChange: (v: unknown) => void;
  onPreview?: () => void;
  previewing?: boolean;
}) {
  const hint = spec.hint ? <small className="fac-field-hint">{spec.hint}</small> : null;
  const isVoice = spec.key === "voice" || spec.key === "speaker";
  return (
    <label className="fac-field">
      <span className="fac-field-label">{spec.label}</span>
      <span className="fac-field-control">
        {spec.type === "select" ? (
          <select value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
            {(spec.options || []).map((o) => (
              <option key={o} value={o}>{spec.optionLabels?.[o] || o}</option>
            ))}
          </select>
        ) : spec.type === "int" || spec.type === "float" ? (
          <input
            type="number"
            value={Number.isFinite(Number(value)) ? Number(value) : ""}
            min={spec.min}
            max={spec.max}
            step={spec.step ?? (spec.type === "int" ? 1 : 0.1)}
            onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
          />
        ) : (
          <input type="text" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />
        )}
        {isVoice && onPreview && (
          <button type="button" className="fac-btn-mini" onClick={onPreview} disabled={previewing}>
            {previewing ? "合成中…" : "试听"}
          </button>
        )}
      </span>
      {hint}
    </label>
  );
}

function SettingBody({ children, onSaved, saveLabel = "保存到流程" }: {
  children: ReactNode;
  onSaved: () => void;
  saveLabel?: string;
}) {
  return (
    <>
      <div className="fac-set-inner">{children}</div>
      <div className="fac-set-foot">
        <button type="button" className="btn btn-primary fac-btn-mini" onClick={onSaved}>{saveLabel}</button>
      </div>
    </>
  );
}

export function FactoryLinePanel({ line, products }: { line: FactoryLine; products: ProductLite[] }) {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  // 就近弹出的设置框：加工渠道 / 标准化流程步骤各用一个锚点，同时只开一个
  const [channelAnchor, setChannelAnchor] = useState<HTMLElement | null>(null);
  const [channelName, setChannelName] = useState("");
  const [form, setForm] = useState<Record<string, unknown>>({});
  const [stepIndex, setStepIndex] = useState<number | null>(null);
  const [stepAnchor, setStepAnchor] = useState<HTMLElement | null>(null);
  const [confirming, setConfirming] = useState(false);

  const [wf, setWf] = useState<Workflow>(BLANK_WORKFLOW);
  const [mode, setMode] = useState<"t2i" | "i2i">("t2i");
  const [initImage, setInitImage] = useState<{ name: string; url: string } | null>(null);
  const [uploading, setUploading] = useState(false);
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewErr, setPreviewErr] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const previewObjectUrl = useRef("");

  const [viewer, setViewer] = useState<{ item: ViewerItem; anchor: HTMLElement | null } | null>(null);
  const [toast, setToast] = useState("");
  const [submitErr, setSubmitErr] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    const tasks: Promise<unknown>[] = [];
    if (line.channelKind) {
      tasks.push(
        getJson<{ ok?: boolean; channels?: Channel[] }>("/api/factory/channels")
          .then((d) => setChannels(d.channels ?? [])),
      );
    }
    if (line.jobKind) {
      tasks.push(
        getJson<{ ok?: boolean; items?: Job[] }>("/api/toolbox/jobs?limit=60")
          .then((d) => setJobs(d.items ?? [])),
      );
    }
    if (!tasks.length) {
      setLoading(false);
      return;
    }
    try {
      await Promise.all(tasks);
      setErr("");
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setLoading(false);
    }
  }, [line.channelKind, line.jobKind]);

  useEffect(() => {
    setLoading(true);
    setChannels([]);
    setJobs([]);
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  useEffect(() => {
    setWf(BLANK_WORKFLOW);
    setChannelAnchor(null);
    setStepAnchor(null);
    setStepIndex(null);
    setViewer(null);
  }, [line.key]);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(""), 4000);
    return () => window.clearTimeout(t);
  }, [toast]);

  useEffect(() => () => {
    if (previewObjectUrl.current) URL.revokeObjectURL(previewObjectUrl.current);
  }, []);

  useEffect(() => () => {
    if (initImage?.url) URL.revokeObjectURL(initImage.url);
  }, [initImage]);

  const lineChannels = useMemo(
    () => (line.channelKind ? channels.filter((c) => c.kind === line.channelKind) : []),
    [channels, line.channelKind],
  );
  const lineJobs = useMemo(
    () => (line.jobKind ? jobs.filter((j) => j.kind === line.jobKind) : []),
    [jobs, line.jobKind],
  );
  const activeJobs = lineJobs.filter((j) => j.status === "queued" || j.status === "running").length;
  const doneJobs = lineJobs.filter((j) => j.status === "done").length;
  const availChannels = lineChannels.filter((c) => c.available).length;
  const relatedProducts = useMemo(
    () => products.filter((p) => !p.shippedAt && STAGE_LINE[p.run?.stage || ""] === line.key),
    [products, line.key],
  );

  const isImage = line.key === "image";
  const isAudio = line.key === "audio";
  const activeChannel = lineChannels.find((c) => c.name === wf.channel) || null;
  const formChannel = lineChannels.find((c) => c.name === channelName) || null;
  const voice = voiceSpecOf(activeChannel);
  const supportsI2I = !!(isImage && (activeChannel?.modes || []).includes("i2i"));

  useEffect(() => {
    if (!supportsI2I && mode === "i2i") setMode("t2i");
  }, [supportsI2I, mode]);

  const applyPreset = (values: Record<string, unknown>) => {
    setWf((prev) => ({ ...prev, params: { ...prev.params, ...values } }));
    setToast("已按画质预设填入参数，可再微调");
  };

  const uploadInitImage = async (file: File) => {
    setUploading(true);
    setSubmitErr("");
    try {
      const r = await fetch("/api/toolbox/frame", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": file.type || "application/octet-stream" }, body: file,
      });
      const d = await r.json() as { ok?: boolean; frame?: string; error?: string };
      if (!r.ok || !d.ok || !d.frame) throw new Error(d.error || "上传失败");
      if (initImage?.url) URL.revokeObjectURL(initImage.url);
      setInitImage({ name: d.frame, url: URL.createObjectURL(file) });
    } catch (e) {
      setSubmitErr(`参考图上传失败：${(e as Error).message || e}`);
    } finally {
      setUploading(false);
    }
  };


  // 首次拿到渠道清单：默认落到第一个可用渠道，参数取该渠道默认值
  useEffect(() => {
    if (!lineChannels.length || wf.channel) return;
    const pick = lineChannels.find((c) => c.available) || lineChannels[0];
    const params = defaultsOf(pick);
    const vspec = voiceSpecOf(pick);
    setWf((prev) => ({
      ...prev,
      channel: pick.name,
      voice: vspec ? String(params[vspec.key] ?? "") : "",
      params,
    }));
  }, [lineChannels, wf.channel]);

  const pickChannel = (name: string) => {
    const ch = lineChannels.find((c) => c.name === name);
    if (!ch) return;
    const params = defaultsOf(ch);
    const vspec = voiceSpecOf(ch);
    setWf((prev) => ({
      ...prev,
      channel: name,
      voice: vspec ? String(params[vspec.key] ?? "") : "",
      params,
    }));
  };

  const setVoice = (value: string) => {
    if (!voice) return;
    setWf((prev) => ({ ...prev, voice: value, params: { ...prev.params, [voice.key]: value } }));
  };

  const closeChannel = () => { setChannelAnchor(null); setChannelName(""); };
  const closeStep = () => { setStepAnchor(null); setStepIndex(null); setConfirming(false); };

  const openChannel = (ch: Channel, el: HTMLElement) => {
    setWf((prev) => {
      const values = prev.channel === ch.name && Object.keys(prev.params).length
        ? { ...prev.params } : defaultsOf(ch);
      setForm(values);
      return prev;
    });
    setChannelName(ch.name);
    setChannelAnchor(el);
  };

  const saveChannel = () => {
    if (!formChannel) return;
    const vspec = voiceSpecOf(formChannel);
    setWf((prev) => ({
      ...prev,
      channel: formChannel.name,
      voice: vspec ? String(form[vspec.key] ?? "") : "",
      params: { ...form },
    }));
    setToast(`已把「${formChannel.label}」的参数写入流程`);
    closeChannel();
  };

  const preview = async (chName: string, vid: string) => {
    setPreviewing(true);
    setPreviewErr("");
    if (previewObjectUrl.current) {
      URL.revokeObjectURL(previewObjectUrl.current);
      previewObjectUrl.current = "";
    }
    setPreviewUrl("");
    try {
      const url = `/api/factory/voice/preview?channel=${encodeURIComponent(chName)}`
        + `&voice=${encodeURIComponent(vid)}&v=${Date.now()}`;
      const r = await fetch(url, { credentials: "include" });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.error || `HTTP ${r.status}`);
      }
      const blob = await r.blob();
      previewObjectUrl.current = URL.createObjectURL(blob);
      setPreviewUrl(previewObjectUrl.current);
    } catch (e) {
      setPreviewErr(`试听失败：${(e as Error).message || e}`);
    } finally {
      setPreviewing(false);
    }
  };

  const submitWorkflow = async () => {
    if (!line.jobKind) { setSubmitErr("该车间暂未开放在线加工提交"); return; }
    if (!activeChannel) { setSubmitErr("请先在流程里选定加工渠道"); return; }
    if (!activeChannel.available) { setSubmitErr(`渠道不可用：${activeChannel.reason}`); return; }
    const items = wf.text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (!items.length) { setSubmitErr("请先在步骤 1 填写台词或提示词（一行一条）"); return; }
    setSubmitting(true);
    setSubmitErr("");
    try {
      for (const text of items) {
        const r = await fetch("/api/toolbox/jobs", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            kind: line.jobKind,
            channels: [wf.channel],
            prompt: line.jobKind === "audio" ? "" : text,
            line: line.jobKind === "audio" ? text : "",
            sec: 0,
            params: { [wf.channel]: wf.params },
            frame: isImage && mode === "i2i" && initImage ? initImage.name : "",
          }),
        });
        const d = await r.json() as { ok?: boolean; error?: string };
        if (!r.ok || !d.ok) throw new Error(d.error || "提交失败");
      }
      setToast(`已发布 ${items.length} 条加工任务，可在「最近加工任务」看进度`);
      closeStep();
      void load();
    } catch (e) {
      setSubmitErr(`提交失败：${(e as Error).message || e}`);
    } finally {
      setSubmitting(false);
    }
  };

  const openJob = (j: Job, el: HTMLElement) => {
    if (!j.outputUrl) return;
    setViewer({ item: { url: j.outputUrl, kind: j.kind, title: j.prompt || j.note || "加工成品" }, anchor: el });
  };

  const channelSelect = (
    <label className="fac-field">
      <span className="fac-field-label">加工渠道</span>
      <select value={wf.channel} onChange={(e) => pickChannel(e.target.value)}>
        {lineChannels.map((c) => (
          <option key={c.name} value={c.name}>
            {c.label}{c.available ? "" : "（不可用）"}
          </option>
        ))}
      </select>
      <small className="fac-field-hint">{activeChannel?.notes || "渠道参数来自后端注册表"}</small>
    </label>
  );

  const inputImageField = (
    <>
      <label className="fac-field">
        <span className="fac-field-label">生成模式</span>
        <span className="fac-field-control">
          <button type="button" className={`fac-mode-btn ${mode === "t2i" ? "is-active" : ""}`}
            onClick={() => setMode("t2i")}>文生图</button>
          <button type="button" className={`fac-mode-btn ${mode === "i2i" ? "is-active" : ""}`}
            disabled={!supportsI2I} onClick={() => setMode("i2i")}>
            图生图{supportsI2I ? "" : "（不支持）"}
          </button>
        </span>
        <small className="fac-field-hint">
          {supportsI2I
            ? "图生图需上传一张参考图；输出尺寸以所选渠道的规格为准，不是照搬参考图。"
            : "当前渠道只做文生图（media-gen SD1.5 服务端没有图生图管道），可换 ComfyUI 157 或百炼。"}
        </small>
      </label>
      {mode === "i2i" && (
        <label className="fac-field">
          <span className="fac-field-label">参考图（PNG / JPG / WebP，≤8MB）</span>
          <input type="file" accept="image/png,image/jpeg,image/webp"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) void uploadInitImage(f); }} />
          {uploading && <small className="fac-field-hint">上传中…</small>}
          {initImage && (
            <>
              <img className="fac-init-preview" src={initImage.url} alt="参考图预览" />
              <small className="fac-field-hint">已上传：{initImage.name}（提交时随工单传给渠道）</small>
            </>
          )}
          {submitErr && <p className="fac-set-error">{submitErr}</p>}
        </label>
      )}
    </>
  );

  const finalStep = (ch: Channel | null) => (
    <>
      <label className="fac-field">
        <span className="fac-field-label">交付说明</span>
        <input type="text" value={wf.output}
          placeholder={isImage ? "例：小秘书宣传片·第 2 镜关键帧" : "例：小秘书宣传片·第 3 批旁白"}
          onChange={(e) => setWf((p) => ({ ...p, output: e.target.value }))} />
      </label>
      <div className="fac-set-summary">
        <div><span>{isImage ? "提示词条数" : "台词条数"}</span><b>{wf.text.split(/\r?\n/).filter((s) => s.trim()).length} 条</b></div>
        <div><span>加工渠道</span><b>{ch?.label || "未选"}</b></div>
        {isAudio ? (
          <>
            <div><span>音色</span><b>{labelOf(voice, wf.voice)}</b></div>
            <div><span>配乐</span><b>{wf.bgm || "未填"}</b></div>
          </>
        ) : (
          <div><span>生成模式</span><b>{mode === "i2i" ? `图生图 · ${initImage?.name || "未上传参考图"}` : "文生图"}</b></div>
        )}
        <div><span>交付说明</span><b>{wf.output || "未填"}</b></div>
      </div>
      {isImage && mode === "i2i" && !initImage && (
        <p className="fac-set-error">图生图必须先上传参考图，请回到第 2 步。</p>
      )}
      {!confirming ? (
        <button type="button" className="btn btn-primary fac-set-start"
          onClick={() => setConfirming(true)}>
          开始整体处理 →
        </button>
      ) : (
        <div className="fac-confirm">
          <p>确认按以上参数提交 <b>{wf.text.split(/\r?\n/).filter((s) => s.trim()).length}</b> 条加工任务？</p>
          <p className="muted">提交后可在「最近加工任务」看进度，完成后点开直接查看/试听。</p>
          {submitErr && <p className="fac-set-error">{submitErr}</p>}
          <div className="fac-confirm-actions">
            <button type="button" className="btn btn-ghost fac-btn-mini" onClick={() => setConfirming(false)}>返回修改</button>
            <button type="button" className="btn btn-primary fac-btn-mini" disabled={submitting}
              onClick={submitWorkflow}>
              {submitting ? "提交中…" : "确认开始"}
            </button>
          </div>
        </div>
      )}
    </>
  );

  const imageSteps: ((ch: Channel | null) => ReactNode)[] = [
    () => (
      <>
        <label className="fac-field">
          <span className="fac-field-label">画面提示词（一行一条，逐张出图）</span>
          <textarea className="fac-textarea" rows={6} value={wf.text}
            placeholder="例：一位穿米色风衣的女孩站在雨后的老街，电影感侧光，浅景深\n第二张画面写在下一行"
            onChange={(e) => setWf((p) => ({ ...p, text: e.target.value }))} />
          <small className="fac-field-hint">每行单独提交一条出图任务；图生图时每行都是对同一张参考图的编辑指令。</small>
        </label>
      </>
    ),
    () => <>{channelSelect}{inputImageField}</>,
    (ch) => ch ? (
      <>
        {(ch.spec?.presets || []).length > 0 && (
          <div className="fac-field">
            <span className="fac-field-label">画质档位（一键填入安全参数）</span>
            <span className="fac-preset-list">
              {ch.spec!.presets!.map((p) => (
                <button key={p.name} type="button" className="fac-btn-mini"
                  onClick={() => applyPreset(p.values)}>{p.name}</button>
              ))}
            </span>
          </div>
        )}
        {ch.params.map((p) => (
          <ParamField key={p.key} spec={p} value={wf.params[p.key]}
            onChange={(v) => setWf((prev) => ({ ...prev, params: { ...prev.params, [p.key]: v } }))} />
        ))}
      </>
    ) : <p className="fac-field-hint">请先在第 2 步选定加工渠道。</p>,
    (ch) => (
      <>
        <SpecBlock spec={ch?.spec} current={wf.params} />
        {mode === "i2i" && initImage && (
          <img className="fac-init-preview" src={initImage.url} alt="参考图预览" />
        )}
        <p className="fac-field-hint">
          参数被系统按各渠道上限做了夹取；上面列出的红线是画质底线，越界会明显掉质量。
        </p>
      </>
    ),
    finalStep,
  ];

  const audioSteps: ((ch: Channel | null) => ReactNode)[] = [
    () => (
      <>
        <label className="fac-field">
          <span className="fac-field-label">逐镜台词（一行一条）</span>
          <textarea className="fac-textarea" rows={6} value={wf.text}
            placeholder="例：今天我们聊聊内存池化。\n第二段台词写在下一行。"
            onChange={(e) => setWf((p) => ({ ...p, text: e.target.value }))} />
          <small className="fac-field-hint">每行会单独提交一条加工任务，逐条看进度、逐条试听。</small>
        </label>
      </>
    ),
    () => (
      <>
        {channelSelect}
        {voice ? (
          <>
            <label className="fac-field">
              <span className="fac-field-label">音色</span>
              <span className="fac-field-control">
                <select value={wf.voice} onChange={(e) => setVoice(e.target.value)}>
                  {(voice.options || []).map((o) => (
                    <option key={o} value={o}>{voice.optionLabels?.[o] || o}</option>
                  ))}
                </select>
                <button type="button" className="fac-btn-mini" disabled={previewing || !wf.voice}
                  onClick={() => preview(wf.channel, wf.voice)}>
                  {previewing ? "合成中…" : "试听"}
                </button>
              </span>
              <small className="fac-field-hint">试听用固定短句合成，同一渠道+音色会缓存，不重复计费。</small>
            </label>
            {previewErr && <p className="fac-set-error">{previewErr}</p>}
            {previewUrl && <audio className="fac-preview-audio" controls autoPlay src={previewUrl} />}
          </>
        ) : <p className="fac-field-hint">该渠道没有音色参数，直接下一步设置加工参数。</p>}
      </>
    ),
    (ch) => ch ? (
      <>
        {ch.params.map((p) => (
          <ParamField key={p.key} spec={p} value={wf.params[p.key]}
            onChange={(v) => setWf((prev) => ({ ...prev, params: { ...prev.params, [p.key]: v } }))}
            onPreview={(p.key === "voice" || p.key === "speaker")
              ? () => preview(wf.channel, String(wf.params[p.key] ?? "")) : undefined}
            previewing={previewing} />
        ))}
        {previewErr && <p className="fac-set-error">{previewErr}</p>}
        {previewUrl && <audio className="fac-preview-audio" controls src={previewUrl} />}
      </>
    ) : <p className="fac-field-hint">请先在第 2 步选定加工渠道。</p>,
    () => (
      <>
        <label className="fac-field">
          <span className="fac-field-label">配乐素材（名称或路径）</span>
          <input type="text" value={wf.bgm} placeholder="例：deliverables/小秘书宣传片/片头曲.mp3"
            onChange={(e) => setWf((p) => ({ ...p, bgm: e.target.value }))} />
          <small className="fac-field-hint">作为本批工单的配乐备注随任务记录，混音在出厂工序完成。</small>
        </label>
        <label className="fac-field">
          <span className="fac-field-label">配乐音量</span>
          <input type="number" min={0} max={1} step={0.01} value={wf.volume}
            onChange={(e) => setWf((p) => ({ ...p, volume: Number(e.target.value) }))} />
          <small className="fac-field-hint">0.16 左右为人声打底，超过 0.3 容易盖过台词。</small>
        </label>
      </>
    ),
    finalStep,
  ];

  const genericSteps: ((ch: Channel | null) => ReactNode)[] = [
    () => (
      <label className="fac-field">
        <span className="fac-field-label">加工描述（一行一条）</span>
        <textarea className="fac-textarea" rows={6} value={wf.text}
          onChange={(e) => setWf((p) => ({ ...p, text: e.target.value }))} />
      </label>
    ),
    () => channelSelect,
    (ch) => ch ? (
      <>
        {ch.params.map((p) => (
          <ParamField key={p.key} spec={p} value={wf.params[p.key]}
            onChange={(v) => setWf((prev) => ({ ...prev, params: { ...prev.params, [p.key]: v } }))} />
        ))}
        <SpecBlock spec={ch.spec} />
      </>
    ) : <p className="fac-field-hint">请先选定加工渠道。</p>,
    () => <SpecBlock spec={activeChannel?.spec} current={wf.params} />,
    finalStep,
  ];

  const stepFields = isImage ? imageSteps : (isAudio ? audioSteps : genericSteps);

  return (
    <div className="fac-line">
      <div className="fac-line-intro">
        <div className="fac-line-mark">{line.short}</div>
        <div className="fac-line-intro-main">
          <h2>
            {line.label}
            <span className="fac-line-sub">{line.subtitle}</span>
          </h2>
          <p className="fac-line-desc">{line.desc}</p>
        </div>
        <div className="fac-line-stats">
          {line.channelKind && (
            <div>
              <b>{availChannels}/{lineChannels.length}</b>
              <span>可用渠道</span>
            </div>
          )}
          {line.jobKind && (
            <>
              <div>
                <b>{activeJobs}</b>
                <span>在制任务</span>
              </div>
              <div>
                <b>{doneJobs}</b>
                <span>已完工</span>
              </div>
            </>
          )}
          {relatedProducts.length > 0 && (
            <div>
              <b>{relatedProducts.length}</b>
              <span>在制流水</span>
            </div>
          )}
        </div>
      </div>

      {err && <div className="fac-error">车间数据加载失败：{err}</div>}
      {toast && <div className="fac-note">{toast}</div>}

      {/* 1. 加工渠道在前：点卡片就近弹设置框 */}
      {line.channelKind && (
        <div className="fac-line-card">
          <div className="fac-line-card-head">
            <h3>加工渠道</h3>
            <span className="muted">来自渠道注册表 · 点卡片设置参数与试听</span>
          </div>
          {loading ? (
            <div className="fac-empty">渠道加载中…</div>
          ) : lineChannels.length === 0 ? (
            <div className="fac-empty">未登记该要素类型的加工渠道</div>
          ) : (
            <div className="fac-ch-grid">
              {lineChannels.map((c) => (
                <button className={`fac-ch ${c.available ? "is-ok" : "is-off"}`} key={c.name}
                  type="button" onClick={(e) => openChannel(c, e.currentTarget)}>
                  <div className="fac-ch-head">
                    <b>{c.label}</b>
                    <span className="fac-ch-state">{c.available ? "可用" : "不可用"}</span>
                  </div>
                  <div className="fac-ch-meta">
                    <code>{c.endpoint || "-"}</code>
                    <span>{c.group || "-"}</span>
                    {c.queue != null && <span>队列 {c.queue}</span>}
                  </div>
                  <p className="fac-ch-notes">{c.notes}</p>
                  {!c.available && <p className="fac-ch-reason">{c.reason}</p>}
                  <span className="fac-ch-tip">点此设置参数 · 试听音色 ↗</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 2. 标准化流程在后：5 步都能点开改数据，末步确认后整体提交 */}
      <div className="fac-line-card">
        <div className="fac-line-card-head">
          <h3>标准化流程</h3>
          <span className="muted">{line.steps.length} 道工序 · 点任一步设置数据</span>
        </div>
        <div className="fac-flow">
          {line.steps.map((s, i) => (
            <button className={`fac-flow-item ${stepIndex === i ? "is-active" : ""}`} key={s.name}
              type="button" onClick={(e) => {
                setStepAnchor(e.currentTarget);
                setStepIndex(i);
                setConfirming(false);
                setSubmitErr("");
              }}>
              <div className="fac-flow-num">{i + 1}</div>
              <div className="fac-flow-body">
                <b>{s.name}</b>
                <p>{s.desc}</p>
              </div>
              <span className="fac-flow-edit">设置 ✎</span>
              {i < line.steps.length - 1 && <span className="fac-flow-arrow">→</span>}
            </button>
          ))}
        </div>
      </div>

      {line.jobKind && (
        <div className="fac-line-card">
          <div className="fac-line-card-head">
            <h3>最近加工任务</h3>
            <span className="muted">全厂「{line.label}」作业 · 每 5 秒刷新 · 完成后可直接试听</span>
          </div>
          {lineJobs.length === 0 ? (
            <div className="fac-empty">暂无该要素的加工任务，可在上方流程末步提交</div>
          ) : (
            <ul className="fac-job-list">
              {lineJobs.slice(0, 20).map((j) => (
                <li className={`fac-job s-${j.status}`} key={j.id}>
                  <button type="button" className="fac-job-preview" disabled={!j.outputUrl}
                    onClick={(e) => openJob(j, e.currentTarget)}
                    title={j.outputUrl ? "点开查看/试听" : "尚未产出"}>
                    {j.status === "done" && j.outputUrl && j.kind === "image" && (
                      <img src={siteUrl(j.outputUrl)} alt="" loading="lazy" />
                    )}
                    {j.status === "done" && j.outputUrl && j.kind === "video" && (
                      <video src={siteUrl(j.outputUrl)} preload="metadata" muted />
                    )}
                    {j.status === "done" && j.outputUrl && j.kind === "audio" && (
                      <span className="fac-job-audio">♪</span>
                    )}
                    {(j.status === "queued" || j.status === "running") && (
                      <span className="fac-job-pct">{j.progress}%</span>
                    )}
                    {!["queued", "running", "done"].includes(j.status) && <span className="fac-job-x">×</span>}
                  </button>
                  <div className="fac-job-main">
                    <p className="fac-job-prompt">{j.prompt || j.note || "（无台词）"}</p>
                    <div className="fac-job-meta">
                      <span className={`fac-job-pill s-${j.status}`}>{STATUS_LABEL[j.status] || j.status}</span>
                      <span>{j.channel}</span>
                      {j.durationSec ? <span>{Number(j.durationSec).toFixed(1)}s</span> : null}
                      <span>{shortTime(j.createdAt)}</span>
                    </div>
                    {(j.status === "queued" || j.status === "running") && (
                      <div className="fac-job-bar">
                        <i style={{ width: `${Math.max(2, j.progress)}%` }} />
                      </div>
                    )}
                    {j.error && <p className="fac-job-error">{j.error}</p>}
                  </div>
                  {j.status === "done" && j.outputUrl && (
                    j.kind === "audio" ? (
                      <button type="button" className="fac-job-open" onClick={(e) => openJob(j, e.currentTarget)}>
                        试听 ♪
                      </button>
                    ) : (
                      <a className="fac-job-open" href={siteUrl(j.outputUrl)} target="_blank" rel="noreferrer">
                        成品 ↗
                      </a>
                    )
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {relatedProducts.length > 0 && (
        <div className="fac-line-card">
          <div className="fac-line-card-head">
            <h3>关联在制流水线</h3>
            <span className="muted">当前处在本车间工序的流水线（点全局查看工艺轴）</span>
          </div>
          <div className="fac-rel-list">
            {relatedProducts.map((p) => (
              <div className="fac-rel" key={p.traceId}>
                <b>{p.serialNo ? `${p.serialNo} · ${p.title}` : p.title}</b>
                <span className="muted">
                  {p.craftName} · 当前工位 {p.run?.stage || "-"} · trace {p.traceId}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="fac-line-card">
        <div className="fac-line-card-head">
          <h3>标准出厂物</h3>
          <span className="muted">各车间统一交付形态</span>
        </div>
        <div className="fac-out-list">
          {line.outputs.map((o) => (
            <span className="fac-out" key={o}>
              {o}
            </span>
          ))}
        </div>
        {line.note && <p className="fac-line-note">{line.note}</p>}
      </div>

      {line.entry && (
        <div className="fac-line-actions">
          <Link className="btn btn-ghost fac-btn" to={line.entry.href}>
            {line.entry.label} →
          </Link>
        </div>
      )}

      {formChannel && channelAnchor && (
        <AnchoredPanel anchor={channelAnchor} onClose={closeChannel}
          className="fac-set-panel" ariaLabel={`${formChannel.label} 参数设置`}
          anchorLabel={`渠道设置 · ${formChannel.label}`}>
          <div className="fac-set-title">
            <b>{formChannel.label}</b>
            <span className={formChannel.available ? "fac-set-ok" : "fac-set-off"}>
              {formChannel.available ? "可用" : formChannel.reason}
            </span>
          </div>
          <SettingBody onSaved={saveChannel}>
            {(formChannel.spec?.presets || []).length > 0 && (
              <div className="fac-field">
                <span className="fac-field-label">画质档位（一键填入安全参数）</span>
                <span className="fac-preset-list">
                  {formChannel.spec!.presets!.map((p) => (
                    <button key={p.name} type="button" className="fac-btn-mini"
                      onClick={() => setForm((prev) => ({ ...prev, ...p.values }))}>
                      {p.name}
                    </button>
                  ))}
                </span>
              </div>
            )}
            {formChannel.params.map((p) => (
              <ParamField key={p.key} spec={p} value={form[p.key]}
                onChange={(v) => setForm((prev) => ({ ...prev, [p.key]: v }))}
                onPreview={(p.key === "voice" || p.key === "speaker")
                  ? () => preview(formChannel.name, String(form[p.key] ?? "")) : undefined}
                previewing={previewing} />
            ))}
            <SpecBlock spec={formChannel.spec} current={form} />
            {isImage && (formChannel.modes || []).includes("i2i") && (
              <p className="fac-field-hint">
                本渠道支持图生图：在标准化流程第 2 步切「图生图」并上传参考图，参数会沿用这里保存的设置。
              </p>
            )}
            {previewErr && <p className="fac-set-error">{previewErr}</p>}
            {previewUrl && <audio className="fac-preview-audio" controls src={previewUrl} />}
          </SettingBody>
        </AnchoredPanel>
      )}

      {stepIndex !== null && stepAnchor && (
        <AnchoredPanel anchor={stepAnchor} onClose={closeStep}
          className="fac-set-panel fac-step-panel"
          ariaLabel={`流程步骤 ${stepIndex + 1}：${line.steps[stepIndex].name}`}
          anchorLabel={`流程 ${stepIndex + 1} · ${line.steps[stepIndex].name}`}>
          <div className="fac-set-title">
            <b>{stepIndex + 1}. {line.steps[stepIndex].name}</b>
            <span className="muted">{line.steps[stepIndex].desc}</span>
          </div>
          <div className="fac-set-inner">{stepFields[stepIndex](activeChannel)}</div>
          {stepIndex < line.steps.length - 1 && (
            <div className="fac-set-foot">
              <button type="button" className="btn btn-primary fac-btn-mini" onClick={closeStep}>保存并关闭</button>
            </div>
          )}
        </AnchoredPanel>
      )}

      <MediaViewer item={viewer?.item || null} anchor={viewer?.anchor}
        onClose={() => setViewer(null)} />
    </div>
  );
}
