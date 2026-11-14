import { useCallback, useEffect, useState } from "react";
import { useGetIdentity } from "@refinedev/core";
import type { OpcUser } from "../types";

type LlmModel = {
  id: string;
  name: string;
  url: string;
  apiKey: string;
  protocol: string;
  provider: string;
  alias: string;
  priority: number;
  status: string;
  isDefault: boolean;
  contextLength: number;
  maxOutputTokens: number;
  totalTokens: number;
  usedTokens: number;
  note: string;
  updatedAt: string;
  updatedBy: string;
};

type ScheduleRule = {
  id: string;
  modelId: string;
  startHour: number;
  endHour: number;
  label: string;
  enabled: boolean;
  priority: number;
  active: boolean;
  note: string;
};

type Effective = { id: string; name: string; source: string; rule: string } | null;

type ManageView = {
  ok?: boolean;
  error?: string;
  reason?: string;
  available: boolean;
  models: LlmModel[];
  schedule: ScheduleRule[];
  effective: Effective;
};

type FormState = {
  id: string;
  name: string;
  alias: string;
  url: string;
  apiKey: string;
  protocol: string;
  provider: string;
  priority: string;
  contextLength: string;
  maxOutputTokens: string;
  totalTokens: string;
  note: string;
  status: string;
  isDefault: boolean;
};

type RuleForm = {
  ruleId: string;
  modelId: string;
  startHour: string;
  endHour: string;
  label: string;
  enabled: boolean;
};

const EMPTY_FORM: FormState = {
  id: "", name: "", alias: "", url: "", apiKey: "",
  protocol: "openai-chat", provider: "custom", priority: "100",
  contextLength: "131072", maxOutputTokens: "16384", totalTokens: "0",
  note: "", status: "enabled", isDefault: false,
};

const EMPTY_RULE: RuleForm = {
  ruleId: "", modelId: "", startHour: "22", endHour: "8",
  label: "夜间", enabled: true,
};

const STATUS_CN: Record<string, string> = {
  enabled: "启用", disabled: "已停用", exhausted: "额度耗尽",
};

const SOURCE_CN: Record<string, string> = {
  schedule: "时段规则", default: "全局默认", priority: "优先级回退",
};

async function post(payload: Record<string, unknown>): Promise<ManageView> {
  const res = await fetch("/api/ops/llm-manage", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const json = (await res.json()) as ManageView;
  if (!res.ok || json.ok === false) {
    throw new Error(json.error || json.reason || `HTTP ${res.status}`);
  }
  return json;
}

function fmtTokens(n?: number) {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n < 0) return "不限额";
  if (n >= 1e8) return `${(n / 1e8).toFixed(2)}亿`;
  if (n >= 1e4) return `${(n / 1e4).toFixed(1)}万`;
  return String(n);
}

function hourLabel(s: number, e: number) {
  const p = (h: number) => `${String(h).padStart(2, "0")}:00`;
  if (s === e) return "全天 24h";
  return `${p(s)} – ${p(e)}`;
}

function toForm(m: LlmModel): FormState {
  return {
    id: m.id, name: m.name, alias: m.alias, url: m.url, apiKey: m.apiKey,
    protocol: m.protocol || "openai-chat", provider: m.provider || "custom",
    priority: String(m.priority ?? 100),
    contextLength: String(m.contextLength ?? 0),
    maxOutputTokens: String(m.maxOutputTokens ?? 0),
    totalTokens: String(m.totalTokens ?? 0),
    note: m.note || "", status: m.status || "enabled", isDefault: !!m.isDefault,
  };
}

function toPayload(f: FormState) {
  return {
    id: f.id.trim(),
    name: f.name.trim(),
    alias: f.alias.trim(),
    url: f.url.trim(),
    apiKey: f.apiKey.trim(),
    protocol: f.protocol.trim() || "openai-chat",
    provider: f.provider.trim() || "custom",
    priority: Number(f.priority) || 100,
    contextLength: Number(f.contextLength) || 0,
    maxOutputTokens: Number(f.maxOutputTokens) || 0,
    totalTokens: Number(f.totalTokens) || 0,
    note: f.note.trim(),
    status: f.status,
    isDefault: f.isDefault,
  };
}

export function ModelsPage() {
  const { data: identity } = useGetIdentity<OpcUser>();
  const isOwner = String(identity?.role ?? "").includes("老板");
  const [view, setView] = useState<ManageView | null>(null);
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);
  const [editing, setEditing] = useState<FormState | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [showKey, setShowKey] = useState(false);
  const [rule, setRule] = useState<RuleForm>(EMPTY_RULE);
  const [showRuleForm, setShowRuleForm] = useState(false);

  const load = useCallback(async () => {
    try {
      setView(await post({ action: "list" }));
    } catch (e) {
      setFeedback({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const run = async (key: string, payload: Record<string, unknown>, okText: string,
                     confirmText?: string): Promise<boolean> => {
    if (confirmText && !window.confirm(confirmText)) return false;
    setBusy(key);
    setFeedback(null);
    try {
      const next = await post(payload);
      setView(next);
      setFeedback({ ok: true, text: okText });
      return true;
    } catch (e) {
      setFeedback({ ok: false, text: e instanceof Error ? e.message : String(e) });
      return false;
    } finally {
      setBusy("");
    }
  };

  const saveModel = async () => {
    if (!editing) return;
    const p = toPayload(editing);
    if (!p.id || !p.name || !p.alias) {
      setFeedback({ ok: false, text: "通道 ID / 模型名 / Hermes 别名均不能为空" });
      return;
    }
    const ok = await run("save", { action: isNew ? "create" : "update", id: p.id, model: p },
      isNew ? `已新增通道 ${p.name}` : `已保存 ${p.name}`);
    if (ok) setEditing(null);
  };

  const models = view?.models ?? [];
  const rules = view?.schedule ?? [];
  const modelName = (id: string) => models.find((m) => m.id === id)?.name || id;
  const currentHour = new Date().getHours();

  if (!isOwner) {
    return (
      <div className="models-page">
        <section className="ops-card">
          <h2>模型管理</h2>
          <p className="muted">仅老板可查看与修改大模型配置。</p>
        </section>
      </div>
    );
  }

  return (
    <div className="models-page">
      <div className="models-head">
        <div>
          <h1>模型管理</h1>
          <p className="muted">
            管理 Hermes 调用通道：启停、默认模型、时段调度、端点与凭据、上下文与额度。
            修改实时写入过程库，并同步到主配置与全员 profile 的 hermes 别名。
          </p>
        </div>
        <div className="models-head-actions">
          <button type="button" className="btn btn-ghost" disabled={!!busy} onClick={() => void load()}>
            刷新
          </button>
          <button type="button" className="btn" onClick={() => {
            setEditing({ ...EMPTY_FORM }); setIsNew(true); setShowKey(false);
            setFeedback(null);
          }}>
            + 新增模型
          </button>
        </div>
      </div>

      {feedback && (
        <div className={`models-feedback ${feedback.ok ? "ok" : "bad"}`} role="status">
          {feedback.text}
        </div>
      )}

      {!view ? (
        <section className="ops-card"><p className="muted">加载中…</p></section>
      ) : !view.available ? (
        <section className="ops-card"><p className="muted">{view.error || "大模型注册表不可用"}</p></section>
      ) : (
        <>
          <section className="ops-card models-effective">
            <h2>当前生效通道 <small className="muted">（本地时间 {String(currentHour).padStart(2, "0")}:xx）</small></h2>
            <div className="models-effective-body">
              <div className="models-effective-main">
                <b>{view.effective?.name || "—"}</b>
                {view.effective && (
                  <span className="models-tag">
                    {SOURCE_CN[view.effective.source] || view.effective.source}
                    {view.effective.rule ? ` · ${view.effective.rule}` : ""}
                  </span>
                )}
                <small className="muted">
                  选择顺序：指定通道 → 时段规则 → 全局默认 → 优先级回退；停用或额度耗尽的通道自动跳过。
                </small>
              </div>
              <div className="models-effective-actions">
                <button type="button" className="btn btn-ghost models-mini" disabled={!!busy}
                        onClick={() => void run("off-all", { action: "schedule_toggle_all", enabled: false },
                          "已停用全部时段规则，全局默认全天生效", "确认停用全部时段规则？")}>
                  停用全部时段
                </button>
                <button type="button" className="btn btn-ghost models-mini" disabled={!!busy}
                        onClick={() => void run("on-all", { action: "schedule_toggle_all", enabled: true },
                          "已启用全部时段规则")}>
                  启用全部时段
                </button>
              </div>
            </div>
          </section>

          <section className="ops-card" aria-label="大模型通道列表">
            <h2>通道列表 <small className="muted">（共 {models.length} 个 · 启用 {models.filter((m) => m.status === "enabled").length} 个）</small></h2>
            <div className="table-wrap">
              <table className="table ops-table models-table">
                <thead>
                  <tr>
                    <th>模型 / 通道 ID</th>
                    <th>状态</th>
                    <th>端点 URL</th>
                    <th>上下文 / 输出</th>
                    <th>额度（已用 / 总）</th>
                    <th>时段</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {models.map((m) => {
                    const mRules = rules.filter((r) => r.modelId === m.id);
                    return (
                      <tr key={m.id} className={m.status === "disabled" ? "models-row-off" : undefined}>
                        <td>
                          <b>{m.name}</b>
                          {m.isDefault && <span className="models-tag default">默认</span>}
                          <small className="muted mono"> · {m.id}</small>
                          <small className="muted"> · 别名 {m.alias || "—"} · 优先级 {m.priority}</small>
                          {m.note && <small className="muted"> · {m.note}</small>}
                        </td>
                        <td>
                          <span className={`ops-pill ${m.status === "enabled" ? "ok" : m.status === "exhausted" ? "bad" : "warn"}`}>
                            {STATUS_CN[m.status] || m.status}
                          </span>
                        </td>
                        <td className="mono models-url" title={m.url}>{m.url || "—"}</td>
                        <td className="mono nowrap">
                          {fmtTokens(m.contextLength)} / {fmtTokens(m.maxOutputTokens)}
                        </td>
                        <td className="nowrap">
                          {m.totalTokens > 0
                            ? `${fmtTokens(m.usedTokens)} / ${fmtTokens(m.totalTokens)}`
                            : `${fmtTokens(m.usedTokens)} / 不限`}
                        </td>
                        <td>
                          {mRules.length ? mRules.map((r) => (
                            <span key={r.id} className={`models-slot ${r.enabled ? (r.active ? "on" : "") : "off"}`}
                                  title={r.enabled ? "已启用" : "已停用"}>
                              {hourLabel(r.startHour, r.endHour)}
                              {r.active && r.enabled ? " ●" : ""}
                            </span>
                          )) : <small className="muted">随时段回退</small>}
                        </td>
                        <td className="nowrap">
                          <div className="models-actions">
                            <button type="button" className="btn btn-ghost models-mini"
                                    onClick={() => {
                                      setEditing(toForm(m)); setIsNew(false); setShowKey(false);
                                      setFeedback(null);
                                    }}>
                              编辑
                            </button>
                            <button type="button" className="btn btn-ghost models-mini" disabled={!!busy}
                                    onClick={() => void run(`status:${m.id}`,
                                      { action: "set_status", id: m.id,
                                        status: m.status === "enabled" ? "disabled" : "enabled" },
                                      `${m.name} 已${m.status === "enabled" ? "停用" : "启用"}`)}>
                              {m.status === "enabled" ? "停用" : "启用"}
                            </button>
                            <button type="button" className="btn btn-ghost models-mini" disabled={!!busy || m.isDefault}
                                    onClick={() => void run(`default:${m.id}`, { action: "set_default", id: m.id },
                                      `${m.name} 已设为全局默认`)}>
                              {m.isDefault ? "已是默认" : "设为默认"}
                            </button>
                            <button type="button" className="btn btn-ghost models-mini models-danger" disabled={!!busy}
                                    onClick={() => void run(`del:${m.id}`, { action: "delete", id: m.id },
                                      `已删除 ${m.name}`,
                                      `确认删除通道 ${m.name}？该通道的时段规则与 hermes 别名会一并移除，此操作不可恢复。`)}>
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          <section className="ops-card" aria-label="时段调度规则">
            <h2>时段调度 <small className="muted">（按时段自动切换默认模型；起止相同 = 全天，跨零点如 22–08 表示夜间）</small></h2>
            <div className="models-rule-list">
              {rules.length === 0 && <p className="muted">暂无时段规则，当前按「全局默认」通道调用。</p>}
              {rules.map((r) => (
                <div key={r.id} className={`models-rule ${r.enabled ? "" : "off"} ${r.active && r.enabled ? "active" : ""}`}>
                  <span className="models-rule-time mono">{hourLabel(r.startHour, r.endHour)}</span>
                  <span className="models-rule-label">{r.label || "—"}</span>
                  <span className="models-rule-arrow">→</span>
                  <b>{modelName(r.modelId)}</b>
                  {r.active && r.enabled && <span className="models-tag on">当前命中</span>}
                  {!r.enabled && <span className="models-tag off">已停用</span>}
                  <div className="models-rule-actions">
                    <button type="button" className="btn btn-ghost models-mini" disabled={!!busy}
                            onClick={() => void run(`rule:${r.id}`,
                              { action: "schedule_set", ruleId: r.id, enabled: !r.enabled },
                              `规则 ${r.label} 已${r.enabled ? "停用" : "启用"}`)}>
                      {r.enabled ? "停用" : "启用"}
                    </button>
                    <button type="button" className="btn btn-ghost models-mini models-danger" disabled={!!busy}
                            onClick={() => void run(`ruledel:${r.id}`,
                              { action: "schedule_delete", ruleId: r.id }, `已删除规则 ${r.label}`,
                              `确认删除时段规则 ${r.label}？`)}>
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>
            <div className="models-rule-add">
              {!showRuleForm ? (
                <button type="button" className="btn btn-ghost" onClick={() => {
                  setRule({ ...EMPTY_RULE, modelId: models.find((m) => m.isDefault)?.id || models[0]?.id || "" });
                  setShowRuleForm(true);
                }}>
                  + 新增时段规则
                </button>
              ) : (
                <div className="models-rule-form">
                  <label className="field">
                    <span>模型通道</span>
                    <select className="input" value={rule.modelId}
                            onChange={(e) => setRule({ ...rule, modelId: e.target.value })}>
                      {models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                    </select>
                  </label>
                  <label className="field">
                    <span>起始小时 (0–23)</span>
                    <input className="input" type="number" min={0} max={23} value={rule.startHour}
                           onChange={(e) => setRule({ ...rule, startHour: e.target.value })} />
                  </label>
                  <label className="field">
                    <span>结束小时 (0–23)</span>
                    <input className="input" type="number" min={0} max={23} value={rule.endHour}
                           onChange={(e) => setRule({ ...rule, endHour: e.target.value })} />
                  </label>
                  <label className="field">
                    <span>标签</span>
                    <input className="input" value={rule.label}
                           onChange={(e) => setRule({ ...rule, label: e.target.value })} />
                  </label>
                  <div className="models-rule-form-actions">
                    <button type="button" className="btn" disabled={!!busy || !rule.modelId}
                            onClick={() => void run("rule-add", {
                              action: "schedule_set", ruleId: rule.ruleId.trim(),
                              modelId: rule.modelId, startHour: Number(rule.startHour),
                              endHour: Number(rule.endHour), label: rule.label.trim(),
                              enabled: rule.enabled,
                            }, "时段规则已保存").then((ok) => { if (ok) setShowRuleForm(false); })}>
                      保存规则
                    </button>
                    <button type="button" className="btn btn-ghost" onClick={() => setShowRuleForm(false)}>
                      取消
                    </button>
                  </div>
                </div>
              )}
            </div>
          </section>
        </>
      )}

      {editing && (
        <div className="models-modal-mask" onMouseDown={(e) => {
          if (e.target === e.currentTarget) setEditing(null);
        }}>
          <div className="models-modal" role="dialog" aria-modal="true" aria-label={isNew ? "新增模型" : "编辑模型"}>
            <div className="models-modal-head">
              <h3>{isNew ? "新增大模型通道" : `编辑 · ${editing.name}`}</h3>
              <button type="button" className="models-close" onClick={() => setEditing(null)} aria-label="关闭">×</button>
            </div>
            <div className="models-form">
              <label className="field">
                <span>通道 ID {isNew ? "(唯一，字母/数字/._-)" : "(不可修改)"}</span>
                <input className="input mono" value={editing.id} disabled={!isNew}
                       placeholder="qwen38flash-bailian"
                       onChange={(e) => setEditing({ ...editing, id: e.target.value })} />
              </label>
              <label className="field">
                <span>模型名（API 的 model 参数）</span>
                <input className="input mono" value={editing.name} placeholder="qwen3.8-flash"
                       onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
              </label>
              <label className="field">
                <span>Hermes 别名（调用时 --model 参数，必填）</span>
                <input className="input mono" value={editing.alias} placeholder="qwen38flash"
                       onChange={(e) => setEditing({ ...editing, alias: e.target.value })} />
              </label>
              <label className="field models-span-2">
                <span>端点 URL（OpenAI 兼容 /v1）</span>
                <input className="input mono" value={editing.url}
                       placeholder="https://.../compatible-mode/v1"
                       onChange={(e) => setEditing({ ...editing, url: e.target.value })} />
              </label>
              <label className="field models-span-2">
                <span>Token Key（API Key，仅老板可见）</span>
                <div className="models-key-row">
                  <input className="input mono" type={showKey ? "text" : "password"} value={editing.apiKey}
                         placeholder="sk-..."
                         onChange={(e) => setEditing({ ...editing, apiKey: e.target.value })} />
                  <button type="button" className="btn btn-ghost models-mini"
                          onClick={() => setShowKey((v) => !v)}>{showKey ? "隐藏" : "显示"}</button>
                </div>
              </label>
              <label className="field">
                <span>协议</span>
                <select className="input" value={editing.protocol}
                        onChange={(e) => setEditing({ ...editing, protocol: e.target.value })}>
                  <option value="openai-chat">openai-chat</option>
                  <option value="hermes-cli">hermes-cli</option>
                </select>
              </label>
              <label className="field">
                <span>Provider</span>
                <input className="input mono" value={editing.provider}
                       onChange={(e) => setEditing({ ...editing, provider: e.target.value })} />
              </label>
              <label className="field">
                <span>上下文大小（token）</span>
                <input className="input" type="number" min={0} value={editing.contextLength}
                       onChange={(e) => setEditing({ ...editing, contextLength: e.target.value })} />
              </label>
              <label className="field">
                <span>最大输出（token）</span>
                <input className="input" type="number" min={0} value={editing.maxOutputTokens}
                       onChange={(e) => setEditing({ ...editing, maxOutputTokens: e.target.value })} />
              </label>
              <label className="field">
                <span>优先级（越小越先，故障转移顺序）</span>
                <input className="input" type="number" min={1} value={editing.priority}
                       onChange={(e) => setEditing({ ...editing, priority: e.target.value })} />
              </label>
              <label className="field">
                <span>总额度（0 = 不限额）</span>
                <input className="input" type="number" min={0} value={editing.totalTokens}
                       onChange={(e) => setEditing({ ...editing, totalTokens: e.target.value })} />
              </label>
              <label className="field">
                <span>状态</span>
                <select className="input" value={editing.status}
                        onChange={(e) => setEditing({ ...editing, status: e.target.value })}>
                  <option value="enabled">启用</option>
                  <option value="disabled">停用</option>
                </select>
              </label>
              <label className="field models-check">
                <span>默认模型</span>
                <label className="models-checkbox">
                  <input type="checkbox" checked={editing.isDefault}
                         onChange={(e) => setEditing({ ...editing, isDefault: e.target.checked })} />
                  设为全局默认（唯一；无时段规则命中时生效）
                </label>
              </label>
              <label className="field models-span-2">
                <span>备注</span>
                <input className="input" value={editing.note}
                       onChange={(e) => setEditing({ ...editing, note: e.target.value })} />
              </label>
            </div>
            <div className="models-modal-foot">
              <small className="muted">保存后会自动同步到 Hermes 主配置与全部员工 profile 的别名定义。</small>
              <div>
                <button type="button" className="btn btn-ghost" onClick={() => setEditing(null)}>取消</button>
                <button type="button" className="btn" disabled={!!busy} onClick={saveModel}>
                  {busy === "save" ? "保存中…" : "保存"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
