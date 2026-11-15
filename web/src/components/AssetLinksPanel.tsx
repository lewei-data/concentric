import { useCallback, useEffect, useState } from "react";

type AssetKind = "raw" | "work";

interface LinkRow {
  id: string;
  kb_id: string;
  asset_kind: AssetKind;
  asset_ref: string;
  role: string;
  note?: string;
}

const ROLE_LABEL: Record<string, string> = {
  evidence: "证据支撑",
  applied: "应用",
  produced: "产出",
  verified: "验证",
  refutes: "推翻",
};

// 原料只能「证明」结论或「推翻」结论；成品则被结论「产出」或反向「验证」
const ROLES_BY_KIND: Record<AssetKind, string[]> = {
  raw: ["evidence", "refutes"],
  work: ["produced", "verified", "applied"],
};

const KIND_LABEL: Record<AssetKind, string> = { raw: "原料", work: "成品" };

export function AssetLinksPanel({ kbId }: { kbId: string }) {
  const [links, setLinks] = useState<LinkRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const [kind, setKind] = useState<AssetKind>("raw");
  const [role, setRole] = useState("evidence");
  const [ref, setRef] = useState("");
  const [note, setNote] = useState("");

  const [rawOptions, setRawOptions] = useState<string[]>([]);
  const [workOptions, setWorkOptions] = useState<string[]>([]);

  const load = useCallback(async () => {
    if (!kbId) return;
    setLoading(true);
    setErr("");
    try {
      const res = await fetch(`/api/knowledge/assets?kb_id=${encodeURIComponent(kbId)}`,
        { credentials: "include" });
      const d = (await res.json()) as { ok?: boolean; items?: LinkRow[]; error?: string };
      setLinks(d.items ?? []);
      if (d.error) setErr(d.error);
    } catch {
      setErr("关联读取失败");
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => { void load(); }, [load]);

  // 候选值：原料取发布清单，成品取作品档案编号
  useEffect(() => {
    fetch("/api/materials", { credentials: "include" })
      .then((r) => r.json())
      .then((d: { items?: { url?: string; title?: string }[] }) =>
        setRawOptions((d.items ?? []).map((i) => i.url ?? "").filter(Boolean)))
      .catch(() => setRawOptions([]));
    fetch("/api/works?size=100", { credentials: "include" })
      .then((r) => r.json())
      .then((d: { items?: { code?: string }[] }) =>
        setWorkOptions((d.items ?? []).map((i) => i.code ?? "").filter(Boolean)))
      .catch(() => setWorkOptions([]));
  }, []);

  const submit = async () => {
    const value = ref.trim();
    if (!value) return;
    setBusy(true);
    try {
      const res = await fetch("/api/knowledge/assets", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "upsert", kb_id: kbId, asset_kind: kind,
          asset_ref: value, role, note: note.trim() }),
      });
      const d = (await res.json()) as { ok?: boolean; error?: string };
      if (!d.ok) setErr(d.error ?? "挂接失败");
      else { setRef(""); setNote(""); await load(); }
    } catch {
      setErr("挂接请求失败");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    try {
      await fetch("/api/knowledge/assets", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "delete", id }),
      });
      await load();
    } finally {
      setBusy(false);
    }
  };

  const options = kind === "raw" ? rawOptions : workOptions;
  const listId = `al-options-${kind}`;

  return (
    <section className="al-panel">
      <h3 className="al-title">
        关联原料与成品
        <span className="muted">　{links.length} 条</span>
      </h3>

      {loading && <p className="muted al-hint">读取中…</p>}
      {err && <div className="error-banner">{err}</div>}

      {!loading && links.length === 0 && (
        <p className="muted al-hint">还没有挂接。原料用于支撑结论，成品用于检验结论。</p>
      )}

      {links.length > 0 && (
        <ul className="al-list">
          {links.map((l) => (
            <li key={l.id} className="al-row">
              <span className={`al-kind al-kind-${l.asset_kind}`}>{KIND_LABEL[l.asset_kind]}</span>
              <span className="al-ref" title={l.asset_ref}>{l.asset_ref}</span>
              <span className="al-role">{ROLE_LABEL[l.role] ?? l.role}</span>
              {l.note && <span className="al-note" title={l.note}>{l.note}</span>}
              <button type="button" className="al-del" disabled={busy}
                onClick={() => void remove(l.id)} aria-label="移除">
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="al-form">
        <select className="al-input" value={kind}
          onChange={(e) => {
            const k = e.target.value as AssetKind;
            setKind(k);
            setRole(ROLES_BY_KIND[k][0]);
          }}>
          <option value="raw">原料</option>
          <option value="work">成品</option>
        </select>
        <input className="al-input al-ref-input" list={listId} value={ref}
          placeholder={kind === "raw" ? "素材路径，如 douyin/xxx.mp4" : "作品编号，如 W-xxx"}
          onChange={(e) => setRef(e.target.value)} />
        <datalist id={listId}>
          {options.map((o) => <option key={o} value={o} />)}
        </datalist>
        <select className="al-input" value={role} onChange={(e) => setRole(e.target.value)}>
          {ROLES_BY_KIND[kind].map((r) => (
            <option key={r} value={r}>{ROLE_LABEL[r] ?? r}</option>
          ))}
        </select>
        <input className="al-input al-note-input" value={note} placeholder="备注（可选）"
          onChange={(e) => setNote(e.target.value)} />
        <button type="button" className="btn btn-ghost al-add" disabled={busy || !ref.trim()}
          onClick={() => void submit()}>
          挂接
        </button>
      </div>
    </section>
  );
}