#!/usr/bin/env node
/* 206 本机执行：拼接 TTS 口播 + 自制 BGM + ducking -> public/ep2-mix.wav
 * 同时生成 src/timeline.ts（Remotion 的句级时间轴数据源）
 * 依赖：Node 原生 fs  only；所有音频均为本项目自制/206 TTS MCP 自产。
 */
const fs = require('fs');
const path = require('path');

const BASE = path.resolve(__dirname);           // ~/opc-workbench/ep2
const ROOT = path.resolve(BASE, '..');          // ~/opc-workbench
const TL = JSON.parse(fs.readFileSync(path.join(BASE, 'ep2-timeline.json'), 'utf8'));
const OUT_WAV = path.join(ROOT, 'video-remotion', 'public', 'ep2-mix.wav');
const OUT_TS = path.join(ROOT, 'video-remotion', 'src', 'timeline.ts');

const SR = 48000;              // 输出采样率
const FPS = 25;
const TOTAL_SEC = TL.duration;
const N = Math.round(TOTAL_SEC * SR);
const voice = new Float32Array(N);
const bgm = new Float32Array(N);
const duck = new Float32Array(N);   // 0..1 目标增益

// ---------- 1. 放置口播（24k mono -> 48k 双声道，线性插值） ----------
function readWavMono(file) {
  const b = fs.readFileSync(file);
  // 找 data chunk
  let off = 12, fmt = null, dataOff = -1, dataLen = 0;
  while (off + 8 <= b.length) {
    const id = b.toString('ascii', off, off + 4);
    const sz = b.readUInt32LE(off + 4);
    if (id === 'fmt ') fmt = { sr: b.readUInt32LE(off + 12), bits: b.readUInt16LE(off + 22), ch: b.readUInt16LE(off + 10) };
    if (id === 'data') { dataOff = off + 8; dataLen = sz; break; }
    off += 8 + sz + (sz % 2);
  }
  if (dataOff < 0 || !fmt || fmt.bits !== 16 || fmt.ch !== 1) throw new Error('unsupported wav: ' + file);
  const n = Math.floor(dataLen / 2);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = b.readInt16LE(dataOff + i * 2) / 32768;
  return { samples: out, sr: fmt.sr };
}

for (const s of TL.segments) {
  const { samples, sr } = readWavMono(s.wav);
  const ratio = sr / SR;
  const startIdx = Math.round(s.start * SR);
  const len = Math.round((samples.length / sr) * SR);
  for (let i = 0; i < len; i++) {
    const src = i * ratio;
    const i0 = Math.floor(src), i1 = Math.min(samples.length - 1, i0 + 1);
    const f = src - i0;
    const v = samples[i0] * (1 - f) + samples[i1] * f;
    // 句首/句尾 12ms 去咔哒
    const env = Math.min(1, i / (0.012 * SR), (len - i) / (0.012 * SR));
    const p = startIdx + i;
    if (p >= 0 && p < N) voice[p] += v * Math.max(0, env);
  }
  // ducking：口播区间压低 BGM，句间抬升
  const a = Math.max(0, Math.round((s.start - 0.12) * SR));
  const e = Math.min(N, Math.round((s.end + 0.18) * SR));
  for (let i = a; i < e; i++) duck[i] = 0.20;
}
// 未标记区间默认抬升
for (let i = 0; i < N; i++) if (duck[i] === 0) duck[i] = 0.62;
// 平滑 ducking（0.18s ramp），避免增益跳变
const ramp = Math.round(0.18 * SR);
const duckSmooth = new Float32Array(N);
let cur = duck[0];
for (let i = 0; i < N; i++) {
  const target = duck[i];
  cur += (target - cur) / ramp;
  duckSmooth[i] = cur;
}

// ---------- 2. 自制 BGM：按章节和弦 + 句起点柔和脉冲 ----------
// 6 章各一个和弦（A 小调色彩），频率 Hz
const CHORDS = [
  [110.00, 261.63, 329.63],   // ch1 Am
  [87.31, 220.00, 261.63],    // ch2 F
  [130.81, 329.63, 392.00],   // ch3 C
  [98.00, 246.94, 293.66],    // ch4 G
  [146.83, 174.61, 440.00],   // ch5 Dm
  [110.00, 261.63, 329.63],   // ch6 Am 回归
];
const TWO_PI = Math.PI * 2;

// 章内和弦包络 + 轻微失谐 pad
for (const ch of TL.chapters) {
  const a = Math.round(ch.start * SR), e = Math.round(Math.min(TOTAL_SEC, ch.end + 0.9) * SR);
  const freqs = CHORDS[ch.index % CHORDS.length];
  const len = e - a;
  for (let i = 0; i < len; i++) {
    const t = i / SR;
    const fade = Math.min(1, i / (0.9 * SR), (len - i) / (0.9 * SR));
    let v = 0;
    for (let k = 0; k < freqs.length; k++) {
      const f = freqs[k];
      // 三个轻微失谐层做 pad 厚度
      v += Math.sin(TWO_PI * f * t + k * 1.1) * 0.55;
      v += Math.sin(TWO_PI * f * 1.003 * t + k * 2.3) * 0.28;
      v += Math.sin(TWO_PI * f * 0.997 * t + k * 0.7) * 0.22;
    }
    // 极慢呼吸
    v *= 0.82 + 0.18 * Math.sin(TWO_PI * 0.075 * t);
    const p = a + i;
    if (p < N) bgm[p] += v * fade * 0.055;
  }
}

// 低频 drone（全程，自制）
for (let i = 0; i < N; i++) {
  const t = i / SR;
  const env = Math.min(1, t / 1.4, (TOTAL_SEC - t) / 2.2);
  bgm[i] += (Math.sin(TWO_PI * 55 * t) * 0.65 + Math.sin(TWO_PI * 82.41 * t) * 0.25) * env * 0.055;
}

// 句起点柔和脉冲：新节奏锚点（替代原 112.8 BPM 固定拍点）
for (const s of TL.segments) {
  const a = Math.round(s.start * SR);
  const len = Math.round(0.34 * SR);
  for (let i = 0; i < len; i++) {
    const t = i / SR;
    const decay = Math.exp(-t * 13);
    const v = (Math.sin(TWO_PI * 523.25 * t) * 0.7 + Math.sin(TWO_PI * 784.0 * t) * 0.3) * decay;
    const p = a + i;
    if (p < N) bgm[p] += v * 0.075;
  }
}

// ---------- 3. 混合 + 软限幅 ----------
const mix = new Float32Array(N);
for (let i = 0; i < N; i++) {
  mix[i] = voice[i] * 0.92 + bgm[i] * duckSmooth[i];
}
let peak = 0;
for (let i = 0; i < N; i++) peak = Math.max(peak, Math.abs(mix[i]));
const target = Math.pow(10, -1.0 / 20);   // -1 dBFS
const g = peak > target ? target / peak : 1;
for (let i = 0; i < N; i++) {
  // tanh 软限幅，避免偶发尖峰
  mix[i] = Math.tanh(mix[i] * g * 1.05) * 0.96;
}

// ---------- 4. 写 WAV (48kHz / stereo / s16le) ----------
const buf = Buffer.alloc(44 + N * 4);
buf.write('RIFF', 0); buf.writeUInt32LE(36 + N * 4, 4); buf.write('WAVE', 8);
buf.write('fmt ', 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20);
buf.writeUInt16LE(2, 22); buf.writeUInt32LE(SR, 24);
buf.writeUInt32LE(SR * 4, 28); buf.writeUInt16LE(4, 32); buf.writeUInt16LE(16, 34);
buf.write('data', 36); buf.writeUInt32LE(N * 4, 40);
for (let i = 0; i < N; i++) {
  const v = Math.max(-1, Math.min(1, mix[i]));
  const s16 = Math.round(v * 32767);
  buf.writeInt16LE(s16, 44 + i * 4);
  buf.writeInt16LE(s16, 44 + i * 4 + 2);
}
fs.mkdirSync(path.dirname(OUT_WAV), { recursive: true });
fs.writeFileSync(OUT_WAV, buf);

// ---------- 5. 生成 Remotion 时间轴模块 ----------
const segs = TL.segments.map(s => ({
  index: s.index, chapter: s.chapter, ill: s.ill,
  text: s.text, start: s.start, end: s.end,
}));
const chapters = TL.chapters.map(c => ({
  index: c.index, id: c.id, title: c.title, ill: c.ill,
  start: c.start, end: c.end, segmentIndices: c.segment_indices,
}));
const ts = `// 自动生成，勿手改。数据源: ep2-timeline.json（206 tts MCP 句级时间轴）
export const FPS = ${FPS};
export const WIDTH = 1080;
export const HEIGHT = 1920;
export const DURATION_SECONDS = ${TOTAL_SEC};
export const TOTAL_FRAMES = Math.round(DURATION_SECONDS * FPS);

export type Seg = { index: number; chapter: number; ill: string; text: string; start: number; end: number };
export type Chapter = { index: number; id: string; title: string; ill: string; start: number; end: number; segmentIndices: number[] };

export const SEGMENTS: Seg[] = ${JSON.stringify(segs, null, 2)};
export const CHAPTERS: Chapter[] = ${JSON.stringify(chapters, null, 2)};
`;
fs.mkdirSync(path.dirname(OUT_TS), { recursive: true });
fs.writeFileSync(OUT_TS, ts, 'utf8');

console.log('mix wav  ->', OUT_WAV, (buf.length / 1048576).toFixed(2) + ' MB');
console.log('timeline ->', OUT_TS);
console.log('duration ', TOTAL_SEC.toFixed(3) + 's', '| segments', segs.length, '| chapters', chapters.length, '| peak', peak.toFixed(3));
