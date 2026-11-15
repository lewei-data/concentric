import { useCallback, useEffect, useState } from "react";

interface Question {
  id: string;
  question: string;
  status: string;
  asked_by?: string;
  origin_ref?: string;
  answer_kb_id?: string;
  answer_title?: string;
  created_at?: string;
}

interface KbOption {
  id: string;
  title: string;
}

function shortDay(v?: string): string {
  const s = (v || "").trim();
  if (!s) return "";
  return s.slice(0, 10);
}

/**
 * 问题池：库里只有答案没有疑问，认知就失去生长动力。
 * 待解决 = 提出但还没有知识条目认领的问题。
 */
export function QuestionsPanel({ onCountChange }: { onCountChange?: (n: number) => void }) {
  const [items, setItems] = useState<Question[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  const [answering, setAnswering] = useState<string | null>(null);
  const [pick, setPick] = useState("");
  const [kbOptions, setKbOptions] = useState<KbOption[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const res = await fetch("/api/knowledge/questions?status=open", { credentials: "include" });
      const d = (await res.json()) as { ok?: boolean; items?: Question[]; error?: string };
      const rows = d.items ?? [];
      setItems(rows);
      onCountChange?.(rows.length);
      if (d.error) setErr(d.error);
    } catch {
      setErr("问题池读取失败");
    } finally {
      setLoading(false);
    }
  }, [onCountChange]);

  useEffect(() => { void load(); }, [load]);

  // 认领问题时要选「哪条知识回答了它」，候选取最近的归档条目
  useEffect(() => {
    fetch("/api/knowledge?size=100", { credentials: "include" })
      .then((r) => r.json())
      .then((d: { items?: { id?: string; title?: string }[] }) =>
        setKbOptions((d.items ?? [])
          .filter((k) => k.id)
          .map((k) => ({ id: String(k.id), title: k.title || String(k.id) }))))
      .catch(() => setKbOptions([]));
  }, []);

  const ask = async () => {
    const q = text.trim();
    if (!q) return;
    setBusy(true);
    try {
      const res = await fetch("/api/knowledge/questions", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
      });
      const d = (await res.json()) as { ok?: boolean; error?: string };
      if (!d.ok) setErr(d.error ?? "提问失败");
      else { setText(""); await load(); }
    } catch {
      setErr("提问请求失败");
    } finally {
      setBusy(false);
    }
  };

  const answer = async (qid: string) => {
    if (!pick) return;
    setBusy(true);
    try {
      const res = await fetch("/api/knowledge/questions", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "answer", id: qid, answer_kb_id: pick }),
      });
      const d = (await res.json()) as { ok?: boolean; error?: string };
      if (!d.ok) setErr(d.error ?? "认领失败");
      else { setAnswering(null); setPick(""); await load(); }
    } catch {
      setErr("认领请求失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="kb-q-wrap">
      <div className="kb-q-ask">
        <input
          className="kb-q-input"
          value={text}
          placeholder="还有什么没想清楚的？写下来，等一条知识来认领"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void ask(); }}
        />
        <button type="button" className="btn" disabled={busy || !text.trim()} onClick={() => void ask()}>
          提出
        </button>
      </div>

      {err && <div className="error-banner">{err}</div>}

      {loading && <p className="kb-hint">正在读取问题池…</p>}

      {!loading && items.length === 0 && (
        <div className="kb-q-empty">
          <b>问题池是空的</b>
          <p>
            这不一定是好事。如果所有问题都已经有答案，说明要么想得足够透，
            要么——没有在记录自己还没想明白的部分。
          </p>
        </div>
      )}

      {items.length > 0 && (
        <ol className="kb-q-list">
          {items.map((it, i) => (
            <li key={it.id} className="kb-q-item">
              <div className="kb-q-main">
                <span className="kb-q-no">{String(i + 1).padStart(2, "0")}</span>
                <div className="kb-q-body">
                  <p className="kb-q-text">{it.question}</p>
                  <div className="kb-q-meta">
                    {it.asked_by && <span>提问：{it.asked_by}</span>}
                    {shortDay(it.created_at) && <span>{shortDay(it.created_at)}</span>}
                    {it.origin_ref && <span className="kb-q-origin">{it.origin_ref}</span>}
                  </div>
                </div>
                {answering === it.id ? null : (
                  <button
                    type="button"
                    className="btn btn-ghost kb-q-act"
                    onClick={() => { setAnswering(it.id); setPick(""); }}
                  >
                    已回答
                  </button>
                )}
              </div>

              {answering === it.id && (
                <div className="kb-q-pick">
                  <select
                    className="kb-q-select"
                    value={pick}
                    onChange={(e) => setPick(e.target.value)}
                  >
                    <option value="">选择回答了它的知识条目…</option>
                    {kbOptions.map((k) => (
                      <option key={k.id} value={k.id}>{k.title}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="btn"
                    disabled={busy || !pick}
                    onClick={() => void answer(it.id)}
                  >
                    确认
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => { setAnswering(null); setPick(""); }}
                  >
                    取消
                  </button>
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}