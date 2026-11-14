import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 桌面精灵（小秘书）：老板登录后浮动在工作区右下角。
 * - 打招呼 → 缩到屏幕边缘待命；
 * - 点击弹出对话框：文字 / 语音（录音转写）/ 文件 与小秘书互动；
 * - 回复带语音（/chat-files/*.wav），自动播报；无音频时回退浏览器朗读；
 * - 监听 SSE chat.message（APP 指令链路的回复也会出现在对话流中）。
 */

type ChatMsg = {
  id?: number;
  direction: "inbound" | "outbound";
  senderId?: string;
  senderName?: string;
  modality?: string;
  content: string;
  source?: string;
  meta?: { audio?: string; file?: string; name?: string; size?: number; navigate?: string; ack?: number };
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
  // 精灵绝对坐标（自由拖动到屏幕任意位置），null = 默认右下角
  const [pos, setPos] = useState<{ x: number; y: number } | null>(() => {
    try {
      const v = JSON.parse(localStorage.getItem("sec-pos") || "null");
      if (v && typeof v.x === "number" && typeof v.y === "number") return v;
    } catch { /* 忽略 */ }
    return null;
  });

  const listRef = useRef<HTMLDivElement>(null);
  const spriteRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const lastTsRef = useRef<string>("");
  const dragRef = useRef<{ px: number; py: number; sx: number; sy: number } | null>(null);
  const dragMovedRef = useRef(false);
  const posRef = useRef<{ x: number; y: number } | null>(pos);
  posRef.current = pos;
  // 已播报/已跳转水位线：首次载入不重播历史消息
  const playUpToRef = useRef<string | null>(null);
  const navUpToRef = useRef<string | null>(null);
  // 对话框：位置/大小（跟随精灵摆放，可拖动表头移动、右下角拉伸改变大小）
  const [panel, setPanel] = useState<{ x: number; y: number; w: number; h: number } | null>(() => {
    try {
      const v = JSON.parse(localStorage.getItem("sec-panel") || "null");
      if (v && typeof v.x === "number" && typeof v.w === "number") return v;
    } catch { /* 忽略 */ }
    return null;
  });
  const panelDragRef = useRef<{ px: number; py: number; sx: number; sy: number;
                                sw: number; sh: number; mode: "move" | "resize" } | null>(null);
  const panelRef = useRef(panel);
  panelRef.current = panel;
  // 出场特效（每次浏览器会话一次）
  const [entrance, setEntrance] = useState<{ greeting: string; items: { kind: string; text: string }[] } | null>(null);

  // —— 出场特效：全屏展示小秘书，播报最新消息 ——
  useEffect(() => {
    if (sessionStorage.getItem("sec-hello")) return;
    sessionStorage.setItem("sec-hello", "1");
    (async () => {
      const b = await getJson<{ greeting?: string; items?: { kind: string; text: string }[] }>(
        "/api/secretary/brief");
      if (!b) return;
      setEntrance({ greeting: b.greeting || "老板好！", items: b.items || [] });
      const text = [b.greeting, ...(b.items || []).map((i) => i.text)].join("。");
      try {
        const u = new SpeechSynthesisUtterance(text.slice(0, 200));
        u.lang = "zh-CN";
        u.rate = 1.05;
        window.speechSynthesis.speak(u);
      } catch { /* 浏览器不支持时仅文字展示 */ }
    })();
  }, []);

  useEffect(() => {
    if (!entrance) return;
    const t = setTimeout(() => closeEntrance(), 18000);
    return () => clearTimeout(t);
  }, [entrance]);

  function closeEntrance() {
    setEntrance(null);
    try { window.speechSynthesis.cancel(); } catch { /* 忽略 */ }
  }

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
          id: Number(m.id ?? 0),
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
    // 同秒消息按入库 id 稳定排序（回执紧随原消息）
    all.sort((a, b) => (a.createdAt === b.createdAt
      ? (a.id ?? 0) - (b.id ?? 0) : a.createdAt.localeCompare(b.createdAt)));
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

  // —— 自动滚动 / 自动播报最后一条新回复 / 小秘书指挥整页跳转（MPA） ——
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [msgs]);

  useEffect(() => {
    const last = msgs[msgs.length - 1];
    if (!last || last.direction !== "outbound") return;
    if (navUpToRef.current === null) { navUpToRef.current = last.createdAt; return; }
    if (last.createdAt <= navUpToRef.current) return;
    navUpToRef.current = last.createdAt;
    const nav = last.meta?.navigate;
    if (nav && nav !== window.location.pathname) {
      setNotice(`小秘书正在打开 ${nav} …`);
      // MPA 整页跳转：按老板指令把页面整个切过去（小秘书操作界面参考此模式）
      const t = setTimeout(() => window.location.assign(nav), 2500);
      return () => clearTimeout(t);
    }
  }, [msgs.length]);

  useEffect(() => {
    const last = msgs[msgs.length - 1];
    if (!last || last.direction !== "outbound") return;
    if (playUpToRef.current === null) { playUpToRef.current = last.createdAt; return; }
    if (last.createdAt <= playUpToRef.current) return;
    playUpToRef.current = last.createdAt;
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

  // —— 精灵拖拽（自由拖到屏幕任意位置，边界内 clamp） ——
  function onSpritePointerDown(e: React.PointerEvent) {
    if (open) return;
    const rect = spriteRef.current?.getBoundingClientRect();
    if (!rect) return;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    dragRef.current = { px: e.clientX, py: e.clientY, sx: rect.left, sy: rect.top };
    dragMovedRef.current = false;
    e.preventDefault();
  }
  function onSpritePointerMove(e: React.PointerEvent) {
    const d = dragRef.current;
    if (!d) return;
    if (Math.abs(e.clientX - d.px) + Math.abs(e.clientY - d.py) > 4) dragMovedRef.current = true;
    const w = spriteRef.current?.offsetWidth || 58;
    const h = spriteRef.current?.offsetHeight || 58;
    const x = Math.min(Math.max(6, d.sx + e.clientX - d.px), window.innerWidth - w - 6);
    const y = Math.min(Math.max(6, d.sy + e.clientY - d.py), window.innerHeight - h - 6);
    const next = { x, y };
    posRef.current = next;
    setPos(next);
  }
  function onSpritePointerUp() {
    if (dragRef.current) {
      dragRef.current = null;
      if (posRef.current) localStorage.setItem("sec-pos", JSON.stringify(posRef.current));
    }
  }
  function onSpriteClick() {
    if (dragMovedRef.current) { dragMovedRef.current = false; return; }  // 拖拽结束不当作点击
    setGreet("");
    setOpen(true);
    // 对话框锚定在精灵旁边（精灵在右半屏→往左弹，否则往右弹；记住用户手动调整过的大小）
    setPanel((p) => {
      const vw = window.innerWidth, vh = window.innerHeight;
      const w = Math.min(p?.w ?? 390, vw - 24);
      const h = Math.min(p?.h ?? 540, vh - 90);
      const rect = spriteRef.current?.getBoundingClientRect();
      let x: number, y: number;
      if (p) { x = p.x; y = p.y; }
      else if (rect) {
        x = rect.left > vw / 2 ? rect.left - w - 14 : rect.right + 14;
        y = Math.min(Math.max(64, rect.top - h / 2), vh - h - 14);
      } else { x = vw - w - 22; y = vh - h - 100; }
      x = Math.max(8, Math.min(x, Math.max(8, vw - w - 8)));
      y = Math.max(8, Math.min(y, Math.max(8, vh - h - 8)));
      return { x, y, w, h };
    });
  }

  // —— 对话框拖动（表头）与改变大小（右下角手柄） ——
  function onPanelPointerDown(e: React.PointerEvent, mode: "move" | "resize") {
    if ((e.target as HTMLElement).closest("button")) return;
    const el = (e.currentTarget as HTMLElement).closest(".sec-panel");
    const rect = el?.getBoundingClientRect();
    if (!rect) return;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    panelDragRef.current = { px: e.clientX, py: e.clientY, sx: rect.left, sy: rect.top,
                             sw: rect.width, sh: rect.height, mode };
    e.preventDefault();
  }
  function onPanelPointerMove(e: React.PointerEvent) {
    const d = panelDragRef.current;
    const cur = panelRef.current;
    if (!d || !cur) return;
    const vw = window.innerWidth, vh = window.innerHeight;
    if (d.mode === "move") {
      const x = Math.max(8, Math.min(d.sx + e.clientX - d.px, Math.max(8, vw - cur.w - 8)));
      const y = Math.max(8, Math.min(d.sy + e.clientY - d.py, Math.max(8, vh - cur.h - 8)));
      setPanel({ ...cur, x, y });
    } else {
      const w = Math.max(320, Math.min(d.sw + e.clientX - d.px, vw - cur.x - 8));
      const h = Math.max(400, Math.min(d.sh + e.clientY - d.py, vh - cur.y - 8));
      setPanel({ ...cur, w, h });
    }
  }
  function onPanelPointerUp() {
    panelDragRef.current = null;
    if (panelRef.current) localStorage.setItem("sec-panel", JSON.stringify(panelRef.current));
    if (posRef.current) localStorage.setItem("sec-pos", JSON.stringify(posRef.current));
  }

  const spriteStyle = pos
    ? { left: pos.x, top: pos.y, right: "auto" as const, bottom: "auto" as const }
    : undefined;
  const greetStyle = pos
    ? { left: Math.max(8, pos.x - 268), top: Math.max(8, pos.y - 8),
        right: "auto" as const, bottom: "auto" as const }
    : undefined;

  const showGreet = greet && !open && !entrance;
  return (
    <>
      {/* 出场特效：全屏展示小秘书并播报最新消息 */}
      {entrance && (
        <div className="sec-entrance" onClick={closeEntrance}>
          <div className="sec-entrance-card" onClick={(e) => e.stopPropagation()}>
            <div className="sec-entrance-avatar">
              <img src="/avatars/secretary.png" alt="小秘书"
                   onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }} />
              <span className="sec-wave" aria-hidden />
            </div>
            <h2>{entrance.greeting}</h2>
            <ul className="sec-entrance-list">
              {entrance.items.map((it, i) => <li key={i}>{it.text}</li>)}
            </ul>
            <button className="sec-btn primary" onClick={closeEntrance}>知道了，开始工作</button>
          </div>
        </div>
      )}

      {/* 桌面精灵 */}
      <div
        ref={spriteRef}
        className="sec-sprite"
        style={spriteStyle}
        onClick={onSpriteClick}
        onPointerDown={onSpritePointerDown}
        onPointerMove={onSpritePointerMove}
        onPointerUp={onSpritePointerUp}
        title="小秘书 · 点击对话，可拖到任意位置"
        role="button"
        aria-label="小秘书"
      >
        <img src="/avatars/secretary.png" alt="" draggable={false}
             onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }} />
        <span className="sec-dot" />
        {speaking && <span className="sec-wave" aria-hidden />}
      </div>
      {showGreet && (
        <div className="sec-greet" style={greetStyle}>
          {greet}
        </div>
      )}

      {/* 对话框（位置跟随精灵，可拖动表头移动、右下角改变大小） */}
      {open && (
        <div
          className="sec-panel"
          role="dialog"
          aria-label="与小秘书对话"
          style={panel ? { left: panel.x, top: panel.y, width: panel.w, height: panel.h,
                           right: "auto" as const, bottom: "auto" as const,
                           maxWidth: "none" as const, maxHeight: "none" as const } : undefined}
          onPointerMove={onPanelPointerMove}
          onPointerUp={onPanelPointerUp}
        >
          <div className="sec-head" onPointerDown={(e) => onPanelPointerDown(e, "move")}
               style={{ cursor: "grab" }}>
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
          <div className="sec-resize" onPointerDown={(e) => onPanelPointerDown(e, "resize")}
               title="拖动改变大小" aria-label="改变对话框大小" />
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
