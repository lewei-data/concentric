import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 桌面精灵（小秘书）：老板登录后浮动在工作区右下角。
 * - 打招呼 → 缩到屏幕边缘待命；
 * - 点击弹出对话框：文字 / 语音（录音转写）/ 文件 与小秘书互动；
 * - 回复带语音（/chat-files/*.wav），自动播报；无音频时回退浏览器朗读；
 * - 监听 SSE chat.message（APP 指令链路的回复也会出现在对话流中）。
 */

type ChatMsg = {
  direction: "inbound" | "outbound";
  senderId?: string;
  senderName?: string;
  modality?: string;
  content: string;
  source?: string;
  meta?: { audio?: string; file?: string; name?: string; size?: number };
  createdAt: string;
};

const GREETINGS = [
  "老板好！我在的，随时吩咐。",
  "老板好！今天的巡检都正常，有需要随时叫我。",
  "老板好！小秘书已上线，会议、资料、催办都可以交给我。",
];

const CHAT_THREAD = "web:secretary";
const APP_THREADS = ["app:secretary", "app:boss", "web:workbench"];

async function getJson<T>(url: string): Promise<T | null> {
  try {
    const res = await fetch(url, { credentials: "include" });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

function postRaw(url: string, body: Blob | ArrayBuffer | string, name?: string) {
  const u = new URL(url, window.location.origin);
  if (name) u.searchParams.set("name", name);
  return fetch(u.toString(), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/octet-stream" },
    body,
  }).then(async (r) => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j as { error?: string }).error || `HTTP ${r.status}`);
    return j;
  });
}

function fmtTime(ts: string) {
  try {
    return new Date(ts).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

export default function AssistantWidget() {
  const [open, setOpen] = useState(false);
  const [greet, setGreet] = useState("");
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [recording, setRecording] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [drag, setDrag] = useState<{ x: number; y: number } | null>(null);
  const [pos, setPos] = useState<{ dx: number; dy: number } | null>(null);

  const listRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const lastTsRef = useRef<string>("");

  // —— 打招呼（每次进入页面，1.5s 后冒泡） ——
  useEffect(() => {
    const t = setTimeout(() => setGreet(GREETINGS[Math.floor(Math.random() * GREETINGS.length)]), 1500);
    const t2 = setTimeout(() => setGreet(""), 9000);
    return () => { clearTimeout(t); clearTimeout(t2); };
  }, []);

  // —— 拉取对话流（Web 线程 + APP 指令线程合并展示） ——
  const load = useCallback(async () => {
    const ids = [CHAT_THREAD, ...APP_THREADS];
    const all: ChatMsg[] = [];
    for (const id of ids) {
      const body = await getJson<{ ok?: boolean; messages?: Record<string, unknown>[] }>(
        `/api/chat/messages?thread=${encodeURIComponent(id)}&limit=40`);
      for (const m of body?.messages || []) {
        let meta: ChatMsg["meta"];
        try { meta = (typeof m.meta === "string" ? JSON.parse(m.meta) : m.meta) as ChatMsg["meta"]; }
        catch { meta = undefined; }
        all.push({
          direction: (m.direction === "inbound" ? "inbound" : "outbound") as ChatMsg["direction"],
          senderId: String(m.sender_id ?? ""),
          senderName: String(m.sender_name ?? ""),
          modality: String(m.modality ?? "text"),
          content: String(m.content ?? ""),
          source: String(m.source ?? id.split(":")[0]),
          meta,
          createdAt: String(m.created_at ?? ""),
        });
      }
    }
    all.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
    const recent = all.slice(-60);
    setMsgs(recent);
    if (recent.length) lastTsRef.current = recent[recent.length - 1].createdAt;
  }, []);

  useEffect(() => { if (open) void load(); }, [open, load]);

  // —— 轮询 + SSE ——
  useEffect(() => {
    const poll = setInterval(() => { if (open) void load(); }, 6000);
    const es = new EventSource("/api/events", { withCredentials: true } as EventSourceInit);
    es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d?.kind === "chat.message") void load();
      } catch { /* 忽略 */ }
    };
    es.onerror = () => { /* EventSource 自动重连 */ };
    return () => { clearInterval(poll); es.close(); };
  }, [open, load]);

  // —— 自动滚动 / 自动播报最后一条新回复 ——
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [msgs]);

  useEffect(() => {
    const last = msgs[msgs.length - 1];
    if (!last || last.direction !== "outbound") return;
    if (last.meta?.audio) {
      setSpeaking(true);
      const a = new Audio(last.meta.audio);
      a.onended = () => setSpeaking(false);
      a.onerror = () => { setSpeaking(false); speakFallback(last.content); };
      void a.play().catch(() => { setSpeaking(false); speakFallback(last.content); });
    } else {
      speakFallback(last.content);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [msgs.length]);

  function speakFallback(text: string) {
    try {
      const u = new SpeechSynthesisUtterance(text.slice(0, 120));
      u.lang = "zh-CN";
      u.onend = () => setSpeaking(false);
      setSpeaking(true);
      window.speechSynthesis.speak(u);
    } catch {
      setSpeaking(false);
    }
  }

  // —— 发文字 ——
  async function sendText() {
    const t = text.trim();
    if (!t || busy) return;
    setText("");
    setBusy(true);
    try {
      const res = await fetch("/api/chat/send", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: t }),
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok || !j.ok) throw new Error((j as { error?: string }).error || `HTTP ${res.status}`);
      await load();
    } catch (e) {
      setNotice(`发送失败：${(e as Error).message}`);
      setTimeout(() => setNotice(""), 5000);
    } finally {
      setBusy(false);
    }
  }

  // —— 语音（MediaRecorder → /api/chat/voice） ——
  async function toggleVoice() {
    if (recording) {
      recRef.current?.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunksRef.current = [];
      const rec = new MediaRecorder(stream, { mimeType: pickMime() });
      rec.ondataavailable = (e) => { if (e.data.size) chunksRef.current.push(e.data); };
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        setBusy(true);
        setNotice("语音识别中…");
        try {
          const blob = new Blob(chunksRef.current, { type: rec.mimeType || "audio/webm" });
          const ext = (rec.mimeType || "audio/webm").includes("ogg") ? "ogg" : "webm";
          const j = await postRaw(`/api/chat/voice?ext=${ext}`, blob) as { transcript?: string };
          setNotice(`已识别：${j.transcript || "（空）"}`);
          setTimeout(() => setNotice(""), 4000);
          await load();
        } catch (e) {
          setNotice(`语音失败：${(e as Error).message}`);
          setTimeout(() => setNotice(""), 5000);
        } finally {
          setBusy(false);
        }
      };
      recRef.current = rec;
      rec.start();
      setRecording(true);
    } catch {
      setNotice("无法访问麦克风（请检查浏览器权限）");
      setTimeout(() => setNotice(""), 5000);
    }
  }

  // —— 文件 ——
  async function sendFile(f: File) {
    setBusy(true);
    setNotice(`上传 ${f.name}…`);
    try {
      const j = await postRaw(`/api/chat/upload?name=${encodeURIComponent(f.name)}`, f, f.name);
      setNotice(`已发给小秘书：${j.file as string}`);
      setTimeout(() => setNotice(""), 4000);
      await load();
    } catch (e) {
      setNotice(`上传失败：${(e as Error).message}`);
      setTimeout(() => setNotice(""), 5000);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  // —— 精灵拖拽 ——
  function onSpritePointerDown(e: React.PointerEvent) {
    if (open) return;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    setDrag({ x: e.clientX, y: e.clientY });
  }
  function onSpritePointerMove(e: React.PointerEvent) {
    if (!drag) return;
    setPos((p) => ({
      dx: Math.max(0, (p?.dx ?? 0) + (e.clientX - drag.x)),
      dy: Math.max(0, (p?.dy ?? 0) + (e.clientY - drag.y)),
    }));
    setDrag({ x: e.clientX, y: e.clientY });
  }
  function onSpritePointerUp() {
    if (drag) {
      setDrag(null);
      if (pos) localStorage.setItem("sec-pos", JSON.stringify(pos));
    }
  }

  const offset = pos ?? (() => {
    try { return JSON.parse(localStorage.getItem("sec-pos") || "null") as { dx: number; dy: number } | null; }
    catch { return null; }
  })();

  return (
    <>
      {/* 桌面精灵 */}
      <div
        className="sec-sprite"
        style={{ transform: `translate(${offset?.dx ?? 0}px, ${offset?.dy ?? 0}px)` }}
        onClick={() => { setGreet(""); setOpen((v) => !v); }}
        onPointerDown={onSpritePointerDown}
        onPointerMove={onSpritePointerMove}
        onPointerUp={onSpritePointerUp}
        title="小秘书 · 点击对话，可拖动"
        role="button"
        aria-label="小秘书"
      >
        <img src="/avatars/secretary.png" alt="" draggable={false}
             onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }} />
        <span className="sec-dot" />
        {speaking && <span className="sec-wave" aria-hidden />}
      </div>
      {greet && !open && (
        <div className="sec-greet" style={{ transform: `translate(${offset?.dx ?? 0}px, ${offset?.dy ?? 0}px)` }}>
          {greet}
        </div>
      )}

      {/* 对话框 */}
      {open && (
        <div className="sec-panel" role="dialog" aria-label="与小秘书对话">
          <div className="sec-head">
            <b>小秘书</b>
            <span className={`sec-status ${busy ? "busy" : ""}`}>{busy ? "处理中…" : "在线"}</span>
            <button className="sec-close" onClick={() => setOpen(false)} aria-label="收起">×</button>
          </div>
          <div className="sec-tip">文字 / 按住🎤说话 / 📎传文件；APP 发来的语音指令也会同步显示在这里。</div>
          <div className="sec-list" ref={listRef}>
            {msgs.length === 0 && (
              <div className="sec-empty">还没有对话。打个招呼，或直接吩咐一件事吧。</div>
            )}
            {msgs.map((m, i) => (
              <div key={i} className={`sec-msg ${m.direction === "inbound" ? "me" : "bot"}`}>
                <div className="sec-meta">
                  {m.senderName || (m.direction === "inbound" ? "老板" : "小秘书")}
                  {m.direction === "inbound" && m.source === "app" && " · 来自APP"}
                  <small> · {fmtTime(m.createdAt)}</small>
                </div>
                <div className="sec-bubble">
                  {m.modality === "voice" && <span className="sec-voice-tag">🎤 语音</span>}
                  {m.meta?.audio && (
                    <audio controls preload="none" src={m.meta.audio} className="sec-audio" />
                  )}
                  {m.meta?.file && (
                    <a href={m.meta.file} target="_blank" rel="noreferrer" className="sec-file">
                      📎 {m.meta.name || m.meta.file}
                    </a>
                  )}
                  <div className="sec-text">{m.content}</div>
                </div>
              </div>
            ))}
          </div>
          {notice && <div className="sec-notice">{notice}</div>}
          <div className="sec-input">
            <button className="sec-btn" onClick={() => fileRef.current?.click()}
                    disabled={busy} title="传文件">📎</button>
            <input ref={fileRef} type="file" hidden
                   onChange={(e) => { const f = e.target.files?.[0]; if (f) void sendFile(f); }} />
            <button className={`sec-btn ${recording ? "rec" : ""}`} onClick={() => void toggleVoice()}
                    disabled={busy} title={recording ? "停止录音" : "语音输入"}>
              {recording ? "⏹" : "🎤"}
            </button>
            <input
              className="sec-entry"
              placeholder="吩咐小秘书…"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) void sendText(); }}
              disabled={busy}
            />
            <button className="sec-btn primary" onClick={() => void sendText()} disabled={busy || !text.trim()}>
              发送
            </button>
          </div>
        </div>
      )}
    </>
  );
}

function pickMime(): string {
  for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", ""]) {
    if (!m || (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(m))) return m || "";
  }
  return "";
}
