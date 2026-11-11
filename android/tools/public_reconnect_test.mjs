// 公网换号重连验证：断线后用「新 channel_id」直连，服务端应接管同一会话。
// 用法：node tools/public_reconnect_test.mjs
const BASE = 'https://ai.boraydata.com/conring/dispatch';
const log = (...a) => console.log(...a);

async function submit() {
  const r = await fetch(BASE + '/submit', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: '语音通话接通', source: 'boss', modality: 'voice', channel: 'ws', profile: 'secretary' }),
  });
  return await r.json();
}

function openChannel(url) {
  return new Promise((resolve) => {
    const ws = new WebSocket(url);
    const t0 = Date.now();
    const frames = [];
    let started = null, helloOk = false, taskId = '';
    ws.onopen = () => ws.send(JSON.stringify({ type: 'hello' }));
    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      const j = JSON.parse(ev.data);
      frames.push(j.type);
      if (j.type === 'session_started') { started = Date.now() - t0; taskId = j.task_id || ''; }
      if (j.type === 'hello_ok') { helloOk = true; taskId = taskId || j.task_id || ''; }
      if (starterSeen() ) resolve({ ws, frames, started, helloOk, taskId });
    };
    const starterSeen = () => helloOk || started !== null;
    ws.onclose = (e) => resolve({ ws, frames, started, helloOk, taskId, closed: e.code });
    setTimeout(() => resolve({ ws, frames, started, helloOk, taskId, timeout: true }), 6000);
  });
}

log('===== 换号重连 =====');
const a = await submit();
log(`A submit task=${a.task_id} ch=${a.channel_id} resumed=${a.resumed || false}`);
const r1 = await openChannel(a.realtime_url.replace('<channel_id>', a.channel_id));
log(`A 连上 session_started=${r1.started}ms hello_ok=${r1.helloOk} task=${r1.taskId || a.task_id}`);

// 模拟断线：不发 close，直接断 socket（服务端应保留 300s）
try { r1.ws.close(); } catch (e) { }
log('A 已断开（不发 close），立即用新 id 直连');
await new Promise(r => setTimeout(r, 800));

const fake = 'ch_verify_' + Math.random().toString(36).slice(2, 8);
const url2 = a.realtime_url.replace(/channel\/[^/]+$/, 'channel/' + fake);
const r2 = await openChannel(url2);
log(`B 新 id=${fake} session_started=${r2.started}ms hello_ok=${r2.helloOk} task=${r2.taskId || '(无)'} frames=${r2.frames.join(',')}`);
const adopted = r2.helloOk || r2.started !== null;
const sameTask = (r2.taskId || '') === a.task_id;
log(`结果: 接管成功=${adopted} 同一 task=${sameTask}`);

// 接管成功的话再推一句，确认上下文延续
if (adopted) {
  try { r2.ws.send(JSON.stringify({ type: 'close' })); } catch (e) { }
}
await new Promise(r => setTimeout(r, 1200));
process.exit(adopted ? 0 : 2);
