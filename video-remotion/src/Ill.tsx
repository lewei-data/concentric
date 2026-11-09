import React from "react";
import { interpolate, spring } from "remotion";
import { BEAT_FRAMES } from "./scenes";

const GOLD = "#C9A227";
const GOLD_L = "#F4DE9A";
const W_ = "#F2EDE4";
const DIM = "#7A786F";

const cl = (v: number, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const eo = (t: number) => 1 - Math.pow(1 - cl(t), 3);
const P = (f: number, dur: number, frac = 0.7) => cl(f / (dur * frac));

export const Defs: React.FC = () => (
  <defs>
    <linearGradient id="gGold" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stopColor="#8A6A12" />
      <stop offset="35%" stopColor={GOLD} />
      <stop offset="55%" stopColor={GOLD_L} />
      <stop offset="100%" stopColor="#9A7715" />
    </linearGradient>
    <linearGradient id="gGoldSoft" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stopColor={GOLD} stopOpacity={0.75} />
      <stop offset="100%" stopColor={GOLD} stopOpacity={0.15} />
    </linearGradient>
    <linearGradient id="gWhite" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stopColor={W_} stopOpacity={0.9} />
      <stop offset="100%" stopColor={W_} stopOpacity={0.25} />
    </linearGradient>
    <radialGradient id="gGlow">
      <stop offset="0%" stopColor={GOLD_L} stopOpacity={0.40} />
      <stop offset="45%" stopColor={GOLD} stopOpacity={0.11} />
      <stop offset="100%" stopColor={GOLD} stopOpacity={0} />
    </radialGradient>
    <radialGradient id="gCore">
      <stop offset="0%" stopColor="#FFF8DC" />
      <stop offset="55%" stopColor={GOLD_L} />
      <stop offset="100%" stopColor={GOLD} />
    </radialGradient>
    <linearGradient id="gSheen" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stopColor="#FFFFFF" stopOpacity={0} />
      <stop offset="50%" stopColor="#FFFFFF" stopOpacity={0.55} />
      <stop offset="100%" stopColor="#FFFFFF" stopOpacity={0} />
    </linearGradient>
    <filter id="fGlow" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation={6} result="b" />
      <feMerge>
        <feMergeNode in="b" />
        <feMergeNode in="SourceGraphic" />
      </feMerge>
    </filter>
    <filter id="fSoft" x="-50%" y="-50%" width="200%" height="200%">
      <feGaussianBlur stdDeviation={3} />
    </filter>
  </defs>
);

const Halo: React.FC<{ r?: number; o?: number; cy?: number }> = ({ r = 380, o = 1, cy = 420 }) => (
  <circle cx={540} cy={cy} r={r} fill="url(#gGlow)" opacity={o} />
);

const Orb: React.FC<{ x: number; y: number; r: number; o?: number; glow?: boolean }> = ({ x, y, r, o = 1, glow }) => (
  <g opacity={o}>
    {glow && <circle cx={x} cy={y} r={r * 5} fill="url(#gGlow)" />}
    <circle cx={x} cy={y} r={r} fill="url(#gCore)" filter={glow ? "url(#fGlow)" : undefined} />
  </g>
);

// ============ S01 仪表 / 你做好被蒸馏的准备了吗 ============
const Gauge: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.85);
  const bp = (f % BEAT_FRAMES) / BEAT_FRAMES;
  const rot = f * 0.55;
  const ticks = Array.from({ length: 48 });
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={430} o={0.9} />
      {[238, 312, 388].map((r, i) => (
        <circle key={i} cx={540} cy={420} r={r * (0.55 + 0.45 * eo(p))} fill="none" stroke="url(#gGold)" strokeWidth={i === 0 ? 2.5 : 1.6} opacity={(0.10 + i * 0.06) * eo(p)} />
      ))}
      <g transform={`rotate(${rot} 540 420)`} opacity={eo(p)}>
        {ticks.map((_, i) => {
          const a = (i / ticks.length) * Math.PI * 2;
          const long = i % 6 === 0;
          const r1 = 402;
          const r2 = r1 + (long ? 28 : 12);
          return <line key={i} x1={540 + Math.cos(a) * r1} y1={420 + Math.sin(a) * r1} x2={540 + Math.cos(a) * r2} y2={420 + Math.sin(a) * r2} stroke={long ? GOLD : W_} strokeWidth={long ? 2.4 : 1.2} opacity={long ? 0.55 : 0.16} />;
        })}
      </g>
      <circle cx={540} cy={420} r={178} fill="none" stroke="url(#gGoldSoft)" strokeWidth={10} strokeDasharray={`${2 * Math.PI * 178}`} strokeDashoffset={(2 * Math.PI * 178) * (1 - 0.72 * eo(p))} opacity={0.5} />
      <Orb x={540} y={420} r={13 + 5 * Math.sin(bp * Math.PI)} glow />
    </svg>
  );
};

// ============ S02 气泡消散 ============
const Bubbles: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur);
  const pos = [
    [-330, -210, 1], [20, -280, 0.85], [330, -160, 1], [-360, 20, 0.9],
    [300, 40, 1], [-250, 240, 0.8], [120, 250, 0.95], [400, 200, 0.75], [-60, 60, 0.9],
  ] as const;
  const shown = eo(cl(f / (dur * 0.25)));
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={440} o={0.55} />
      {pos.map(([dx, dy, sc], i) => {
        const d = cl((p - 0.28 - i * 0.05) / 0.5);
        const k = 1 - eo(d);
        if (k <= 0.01) return null;
        const w = 190 * sc, h = 118 * sc;
        const cx = 540 + dx * (1 + 0.34 * eo(d));
        const cy = 420 + dy * (1 + 0.34 * eo(d));
        return (
          <g key={i} opacity={k * 0.9 * shown} transform={`translate(${cx} ${cy}) scale(${sc * (1 - 0.35 * eo(d))})`}>
            <rect x={-w / 2} y={-h / 2} width={w} height={h} rx={22} fill="#0E0E12" fillOpacity={0.5} stroke={W_} strokeOpacity={0.28} strokeWidth={1.6} />
            <path d={`M ${-w * 0.22} ${h / 2} L ${w * 0.02} ${h / 2} L ${-w * 0.18} ${h / 2 + 22}`} fill="none" stroke={W_} strokeOpacity={0.22} strokeWidth={1.6} />
            {[0, 1, 2].map((j) => (
              <circle key={j} cx={-46 + j * 30} cy={2} r={4.5} fill={W_} opacity={0.35} />
            ))}
          </g>
        );
      })}
      <Orb x={540} y={420} r={11} o={eo(cl((p - 0.4) / 0.4))} glow />
    </svg>
  );
};

// ============ S03 砖墙坍塌 ============
const Wall: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur);
  const bw = 132, bh = 54, gap = 8, cols = 5, rows = 4;
  const ox = 540 - (cols * bw + (cols - 1) * gap) / 2;
  const oy = 420 - (rows * bh + (rows - 1) * gap) / 2;
  const crack = eo(cl((p - 0.2) / 0.6));
  const bricks: React.ReactNode[] = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const x = ox + c * (bw + gap) + (r % 2 ? bw / 2 : 0);
      const y = oy + r * (bh + gap);
      const left = c < 3;
      const fall = left ? eo(cl((p - 0.28 - (3 - c) * 0.07) / 0.42)) : 0;
      const dy = fall * (86 + r * 52);
      const dx = -fall * (24 + (3 - c) * 14);
      const rr = left ? fall * (-6 + (3 - c) * 4) : 0;
      bricks.push(
        <g key={`${r}-${c}`} transform={`translate(${x + dx} ${y + dy}) rotate(${rr} ${bw / 2} ${bh / 2})`} opacity={1 - fall * 0.55}>
          <rect width={bw} height={bh} rx={4} fill="#17171E" fillOpacity={0.9} stroke={left ? GOLD : "#9C9789"} strokeOpacity={left ? 0.55 - fall * 0.25 : 0.78} strokeWidth={2} />
          <rect width={bw} height={5} rx={2.5} fill={W_} opacity={left ? 0.06 : 0.1} />
        </g>,
      );
    }
  }
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={400} o={0.5} />
      {bricks}
      {crack > 0 && (
        <g opacity={crack} filter="url(#fGlow)">
          {[[-16, 3], [22, 2]].map(([off, wid], i) => (
            <polyline
              key={i}
              points={Array.from({ length: 9 }, (_, k) => `${540 + off + 18 * Math.sin(k * 1.8) * crack},${300 + k * 34 * crack}`).join(" ")}
              fill="none"
              stroke="url(#gGold)"
              strokeWidth={wid}
              opacity={i ? 0.5 : 0.9}
            />
          ))}
        </g>
      )}
    </svg>
  );
};

// ============ S04 粒子汇聚成模型 ============
const Converge: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.8);
  const q = eo(cl(p / 0.72));
  const cubeIn = eo(cl((p - 0.55) / 0.45));
  const pts = Array.from({ length: 46 }, (_, i) => {
    const a = i * (Math.PI * 2 / 46) + i * 0.12;
    const rr = 300 + (i % 5) * 46;
    return { a, rr, s: 1.2 + (i % 3) * 0.8 };
  });
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={440} o={0.7} />
      {pts.map((pt, i) => {
        const r = pt.rr * (1 - q) + 118 * q;
        const x = 540 + Math.cos(pt.a) * r * 1.16;
        const y = 420 + Math.sin(pt.a) * r * 0.86;
        const tw = 0.5 + 0.5 * Math.sin(f * 0.16 + i);
        return <circle key={i} cx={x} cy={y} r={pt.s} fill={GOLD_L} opacity={(0.35 + 0.5 * tw) * eo(cl(f / 12))} />;
      })}
      <g opacity={cubeIn} transform={`translate(540 420) scale(${0.9 + 0.1 * cubeIn})`} filter="url(#fGlow)">
        <path d="M -120 -70 L 0 -134 L 120 -70 L 0 -6 Z" fill="url(#gGold)" opacity={0.5} />
        <path d="M -120 -70 L 0 -6 L 0 118 L -120 54 Z" fill="url(#gGold)" opacity={0.24} />
        <path d="M 120 -70 L 0 -6 L 0 118 L 120 54 Z" fill="url(#gGold)" opacity={0.34} />
        <path d="M -120 -70 L 0 -134 L 120 -70 L 0 -6 Z M -120 -70 L 0 -6 L 0 118 L -120 54 Z M 120 -70 L 0 -6 L 0 118 L 120 54 Z" fill="none" stroke={GOLD_L} strokeWidth={2} opacity={0.65} />
      </g>
    </svg>
  );
};

// ============ S05 文章卡 + 视频卡 ============
const TwoCards: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const a = eo(cl(f / (dur * 0.45)));
  const b = eo(cl((f - dur * 0.16) / (dur * 0.4)));
  const sheen = cl((f % 60) / 60);
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={420} o={0.6} />
      <g opacity={a} transform={`translate(${-120 + 12 * (1 - a)} 0) rotate(-4 340 420)`}>
        <rect x={168} y={230} width={330} height={400} rx={18} fill="#111116" stroke={GOLD} strokeOpacity={0.55} strokeWidth={2.4} />
        <rect x={168} y={230} width={330} height={400} rx={18} fill="url(#gGold)" opacity={0.05} />
        {[0, 1, 2, 3, 4, 5].map((k) => (
          <rect key={k} x={206} y={300 + k * 46} width={k % 3 === 0 ? 250 : 180} height={5} rx={2.5} fill={W_} opacity={0.32 + 0.35 * cl((f - k * 3) / 12)} />
        ))}
        <rect x={206} y={610} width={110} height={5} rx={2.5} fill={GOLD} opacity={0.8} />
      </g>
      <g opacity={b} transform={`translate(${120 - 12 * (1 - b)} 0) rotate(4 740 420)`}>
        <rect x={582} y={252} width={330} height={400} rx={18} fill="#111116" stroke={GOLD} strokeOpacity={0.55} strokeWidth={2.4} />
        <rect x={582} y={252} width={330} height={400} rx={18} fill="url(#gGold)" opacity={0.05} />
        <circle cx={747} cy={420} r={62} fill="none" stroke={GOLD} strokeOpacity={0.8} strokeWidth={2.6} />
        <path d="M 730 388 L 776 420 L 730 452 Z" fill="url(#gGold)" opacity={0.9} />
        <rect x={582} y={252} width={160 * sheen} height={400} rx={18} fill="url(#gSheen)" opacity={0.14} />
      </g>
    </svg>
  );
};

// ============ S06 蒸馏成 SKILL ============
const Distill: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.8);
  const cards = eo(cl(f / (dur * 0.3)));
  const move = eo(cl((p - 0.16) / 0.55));
  const drop = eo(cl((p - 0.62) / 0.36));
  const sheen = cl((f % 72) / 72);
  const cardX = 128;
  const cardW = 168;
  const cardH = 96;
  const ys = [196, 372, 548];
  const mouth = [
    [444, 232],
    [560, 228],
    [676, 232],
  ];
  const dropY = 486 + 52 * drop;
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={410} o={0.62} cy={390} />
      {/* 三张素材卡 */}
      {ys.map((y, i) => (
        <g key={i} opacity={cards} transform={`translate(${-14 * (1 - cards)} 0)`}>
          <rect x={cardX} y={y} width={cardW} height={cardH} rx={12} fill="#14141A" fillOpacity={0.92} stroke={i === 1 ? GOLD : DIM} strokeOpacity={i === 1 ? 0.6 : 0.4} strokeWidth={2} />
          <rect x={cardX} y={y} width={cardW} height={cardH} rx={12} fill="url(#gGold)" opacity={0.04} />
          <rect x={cardX + 18} y={y + 16} width={40} height={3} rx={1.5} fill={GOLD} opacity={0.7} />
          <rect x={cardX + 18} y={y + 40} width={118} height={5} rx={2.5} fill={W_} opacity={0.3} />
          <rect x={cardX + 18} y={y + 58} width={86} height={5} rx={2.5} fill={W_} opacity={0.18} />
          <rect x={cardX + 18} y={y + 76} width={104} height={5} rx={2.5} fill={W_} opacity={0.12} />
          <path
            d={`M ${cardX + cardW} ${y + cardH / 2} Q 360 ${y + cardH / 2} ${mouth[i][0]} ${mouth[i][1]}`}
            fill="none"
            stroke="url(#gGold)"
            strokeWidth={2.2}
            opacity={0.12 + 0.55 * move}
          />
        </g>
      ))}
      {/* 蒸馏漏斗 */}
      <g opacity={0.55 + 0.45 * move}>
        <path d="M 420 214 L 700 214 L 604 404 L 516 404 Z" fill="url(#gGold)" opacity={0.07 + 0.09 * move} />
        <path d="M 420 214 L 700 214 L 604 404 L 516 404 Z" fill="none" stroke="url(#gGold)" strokeWidth={2.2} opacity={0.7} />
        <path d="M 420 214 L 700 214" stroke={GOLD_L} strokeWidth={4} opacity={0.75} />
        <rect x={516} y={404} width={88} height={26} rx={3} fill="none" stroke={GOLD} strokeOpacity={0.6} strokeWidth={1.8} />
        <path d={`M 520 330 L 600 330 L 596 396 L 524 396 Z`} fill="url(#gGold)" opacity={0.10 + 0.18 * move} />
        <rect x={430} y={216} width={250 * sheen} height={186} fill="url(#gSheen)" opacity={0.05} />
      </g>
      {/* 下滴 */}
      <line x1={560} y1={432} x2={560} y2={dropY - 34} stroke={GOLD_L} strokeWidth={2} strokeDasharray="8 10" opacity={0.18 + 0.4 * drop} />
      <g opacity={drop} transform={`translate(560 ${dropY})`} filter="url(#fGlow)">
        <path d="M 0 -74 C 26 -37 44 -14 44 10 C 44 36 24 56 0 56 C -24 56 -44 36 -44 10 C -44 -14 -26 -37 0 -74 Z" fill="url(#gGold)" />
      </g>
      {/* SKILL 标签 */}
      <g opacity={eo(cl((p - 0.78) / 0.22))}>
        <rect x={632} y={492} width={168} height={56} rx={9} fill="#101014" fillOpacity={0.6} stroke={W_} strokeOpacity={0.45} strokeWidth={1.6} />
        <text x={656} y={530} fill={W_} opacity={0.8} fontFamily="NotoSansSC" fontSize={26} letterSpacing={5}>
          SKILL
        </text>
        <circle cx={780} cy={478} r={5} fill={GOLD_L} />
      </g>
    </svg>
  );
};

// ============ S07 模型不知道你是谁 ============
const ModelVoid: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.75);
  const box = eo(cl(f / (dur * 0.35)));
  const dot = eo(cl((p - 0.45) / 0.5));
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={380} o={0.45} cy={420} />
      <g opacity={box}>
        <rect x={330} y={250} width={420} height={340} rx={22} fill="#0D0D11" fillOpacity={0.5} stroke={W_} strokeOpacity={0.35} strokeWidth={2.4} />
        {Array.from({ length: 5 }, (_, k) => (
          <line key={k} x1={330 + 84 * (k + 1)} y1={250} x2={330 + 84 * (k + 1)} y2={590} stroke={DIM} strokeOpacity={0.18} strokeWidth={1} />
        ))}
        <rect x={330} y={250} width={420} height={340} rx={22} fill="url(#gGold)" opacity={0.03} />
        <circle cx={540} cy={420} r={96} fill="none" stroke={W_} strokeOpacity={0.1} strokeWidth={1.4} strokeDasharray="6 10" />
        <circle cx={540} cy={420} r={46} fill="none" stroke={W_} strokeOpacity={0.07} strokeWidth={1.2} />
      </g>
      <g opacity={dot}>
        {Array.from({ length: 9 }, (_, k) => (
          <line key={k} x1={760} y1={420} x2={760 + 30 * (k + 1) * dot} y2={420} stroke={GOLD} strokeOpacity={0.35} strokeWidth={2} strokeDasharray="10 12" />
        ))}
        <Orb x={760 + 270 * dot} y={420} r={14} glow />
      </g>
    </svg>
  );
};

// ============ S08 算 vs 判断 ============
const CalcJudge: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.75);
  const g = eo(cl(f / (dur * 0.5)));
  const br = eo(cl((p - 0.2) / 0.55));
  const cells = Array.from({ length: 16 });
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={420} o={0.5} />
      {cells.map((_, i) => {
        const r = Math.floor(i / 4), c = i % 4;
        const on = cl((g - i * 0.045) / 0.3);
        return (
          <rect key={i} x={160 + c * 78} y={300 + r * 78} width={64} height={64} rx={8}
            fill={on > 0.2 ? "url(#gGold)" : "none"} fillOpacity={0.35 * on}
            stroke={GOLD} strokeOpacity={0.25 + 0.45 * on} strokeWidth={2} />
        );
      })}
      <g opacity={br}>
        <line x1={600} y1={420} x2={600 + 120 * br} y2={420} stroke={W_} strokeOpacity={0.55} strokeWidth={3} />
        <line x1={720} y1={420} x2={720 + 150 * br} y2={420 - 110 * br} stroke="url(#gGold)" strokeWidth={3.5} filter="url(#fGlow)" />
        <line x1={720} y1={420} x2={720 + 150 * br} y2={420 + 110 * br} stroke={W_} strokeOpacity={0.28} strokeWidth={2} strokeDasharray="14 12" />
        <Orb x={720 + 150 * br} y={420 - 110 * br} r={7} glow />
      </g>
    </svg>
  );
};

// ============ S09 只有你自己 ============
const OnlyYou: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.8);
  const ring = eo(cl(f / (dur * 0.45)));
  const bp = (f % BEAT_FRAMES) / BEAT_FRAMES;
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={400} o={0.7} />
      <circle cx={540} cy={420} r={238 * ring} fill="none" stroke="url(#gGold)" strokeWidth={2.4} opacity={0.5} />
      <circle cx={540} cy={420} r={168 * ring} fill="none" stroke={GOLD} strokeWidth={1.4} opacity={0.24} />
      <circle cx={540} cy={420} r={330 * ring} fill="none" stroke={GOLD} strokeWidth={1} opacity={0.14} strokeDasharray="4 14" />
      {Array.from({ length: 14 }, (_, k) => {
        const a = k * (Math.PI * 2 / 14) + 0.12;
        return <circle key={k} cx={540 + Math.cos(a) * 330} cy={420 + Math.sin(a) * 330} r={4} fill="#8A887E" opacity={0.42 * ring} />;
      })}
      <Orb x={540} y={420} r={15 + 5 * Math.sin(bp * Math.PI)} o={eo(cl((p - 0.35) / 0.4))} glow />
    </svg>
  );
};

// ============ S10 主动蒸馏自己 ============
const SelfDistill: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.8);
  const fig = eo(cl(f / (dur * 0.4)));
  const flow = eo(cl((p - 0.35) / 0.5));
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={340} o={0.5} cy={380} />
      <g opacity={fig * 0.85}>
        <circle cx={540} cy={258} r={42} fill="none" stroke={W_} strokeOpacity={0.45} strokeWidth={2.6} />
        <path d="M 500 306 L 466 470 L 614 470 L 580 306 Z" fill="#121216" fillOpacity={0.6} stroke={W_} strokeOpacity={0.32} strokeWidth={2.4} />
        {[0, 1, 2].map((k) => (
          <line key={k} x1={498 + k * 4} y1={340 + k * 44} x2={578 - k * 4} y2={340 + k * 44} stroke={GOLD} strokeOpacity={0.18 + 0.12 * k} strokeWidth={1.6} />
        ))}
      </g>
      <g opacity={flow}>
        <path d={`M 540 470 L 540 ${470 + 90 * flow}`} stroke="url(#gGold)" strokeWidth={3} opacity={0.7} />
        {Array.from({ length: 7 }, (_, k) => (
          <circle key={k} cx={540 + Math.sin((k + f * 0.2) * 1.4) * 6} cy={470 + ((k * 18 + flow * 60) % 92)} r={3} fill={GOLD_L} opacity={0.7} />
        ))}
      </g>
      <g opacity={eo(cl((p - 0.6) / 0.4))} transform="translate(540 590)" filter="url(#fGlow)">
        <path d="M 0 -70 C 26 -34 44 -12 44 12 C 44 40 24 60 0 60 C -24 60 -44 40 -44 12 C -44 -12 -26 -34 0 -70 Z" fill="url(#gGold)" />
      </g>
    </svg>
  );
};

// ============ S11 日历 ============
const Calendar: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.8);
  const bp = (f % BEAT_FRAMES) / BEAT_FRAMES;
  const g = eo(cl(f / (dur * 0.5)));
  const cw = 102, ch = 70, gx = 20, gy = 16;
  const ox = 540 - (7 * cw + 6 * gx) / 2;
  const oy = 292;
  const hot = 2 + 4 * 1 + 0; // 第二周 周四
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={420} o={0.55} cy={420} />
      <rect x={ox - 22} y={oy - 52} width={7 * cw + 6 * gx + 44} height={4 * ch + 3 * gy + 78} rx={16} fill="#0E0E12" fillOpacity={0.45} stroke={DIM} strokeOpacity={0.3} strokeWidth={1.6} />
      {Array.from({ length: 5 }, (_, k) => (
        <circle key={k} cx={ox + 14 + k * 22} cy={oy - 26} r={4} fill={GOLD} opacity={0.4} />
      ))}
      {Array.from({ length: 28 }, (_, i) => {
        const r = Math.floor(i / 7), c = i % 7;
        const x = ox + c * (cw + gx), y = oy + r * (ch + gy);
        const on = cl((g - i * 0.012) / 0.2);
        if (i === hot) {
          return (
            <g key={i} opacity={on}>
              <rect x={x} y={y} width={cw} height={ch} rx={9} fill="url(#gGold)" fillOpacity={0.35 + 0.25 * (1 - bp)} />
              <rect x={x} y={y} width={cw} height={ch} rx={9} fill="none" stroke={GOLD_L} strokeWidth={2.6} filter="url(#fGlow)" />
            </g>
          );
        }
        return <rect key={i} x={x} y={y} width={cw} height={ch} rx={9} fill="none" stroke={DIM} strokeOpacity={0.14 + 0.35 * on} strokeWidth={1.6} />;
      })}
    </svg>
  );
};

// ============ S12 分身 ============
const Duplicate: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.8);
  const sp = eo(cl(f / (dur * 0.6)));
  const dx = 158 * sp;
  const bp = (f % BEAT_FRAMES) / BEAT_FRAMES;
  const link = eo(cl((p - 0.3) / 0.5));
  const grow = eo(cl(f / (dur * 0.5)));
  const cx1 = 540 - dx;
  const cx2 = 540 + dx;
  const rings = [34, 62, 90];
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={430} o={0.7} />
      {/* 中轴：镜像的对称面 */}
      <line x1={540} y1={270} x2={540} y2={570} stroke={W_} strokeOpacity={0.18 * link} strokeWidth={1.4} strokeDasharray="6 12" />
      {[cx1, cx2].map((cx, k) => (
        <g key={k}>
          <circle cx={cx} cy={420} r={118} fill="url(#gGlow)" opacity={0.55} />
          {rings.map((r, i) => (
            <circle
              key={i}
              cx={cx}
              cy={420}
              r={r * (0.62 + 0.38 * grow)}
              fill="none"
              stroke="url(#gGold)"
              strokeWidth={i === 0 ? 2.6 : 1.6}
              strokeDasharray={k === 1 && i === 2 ? "12 10" : undefined}
              opacity={(0.3 + i * 0.14) * (k === 1 ? 0.88 : 1)}
            />
          ))}
          <Orb x={cx} y={420} r={13 + 3 * Math.sin((bp + k * 0.5) * Math.PI)} glow />
        </g>
      ))}
      {/* 复制箭头：从本体指向分身 */}
      <g opacity={link}>
        <path d={`M ${cx1 + 34} 356 Q 540 246 ${cx2 - 34} 356`} fill="none" stroke={GOLD_L} strokeWidth={2.4} opacity={0.85} />
        <path d={`M ${cx2 - 58} 384 L ${cx2 - 34} 356 L ${cx2 - 64} 346`} fill="none" stroke={GOLD_L} strokeWidth={2.4} opacity={0.85} />
        <circle cx={540} cy={286} r={4.5} fill={GOLD_L} opacity={0.9} />
      </g>
      {sp < 0.9 && <Orb x={540} y={420} r={13 * (1 - sp)} o={1 - sp} glow />}
    </svg>
  );
};

// ============ S13 循环 ============
const Loop: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.8);
  const q = eo(cl(f / (dur * 0.45)));
  const arrow = eo(cl((p - 0.3) / 0.55));
  const run = cl((f % (BEAT_FRAMES * 4)) / (BEAT_FRAMES * 4));
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={430} o={0.5} cy={420} />
      {Array.from({ length: 5 }, (_, k) => {
        const x = 118 + k * 176;
        const dim = k === 4 ? 1 : 0.55;
        return (
          <g key={k} opacity={q * dim}>
            <rect x={x} y={342} width={150} height={156} rx={12} fill="#111116" fillOpacity={0.55} stroke={GOLD} strokeOpacity={k === 4 ? 0.75 : 0.3} strokeWidth={2} />
            <rect x={x + 22} y={374} width={106} height={5} rx={2.5} fill={W_} opacity={0.3} />
            <rect x={x + 22} y={398} width={70} height={5} rx={2.5} fill={W_} opacity={0.18} />
            <rect x={x + 22} y={422} width={92} height={5} rx={2.5} fill={W_} opacity={0.12} />
            {k === 4 && <circle cx={x + 120} cy={468} r={10} fill="url(#gGold)" filter="url(#fGlow)" />}
          </g>
        );
      })}
      <g opacity={arrow}>
        <path d="M 110 470 A 430 430 0 0 1 968 462" fill="none" stroke="url(#gGold)" strokeWidth={3} opacity={0.55} />
        <path d="M 968 462 A 430 430 0 0 1 110 470" fill="none" stroke={GOLD} strokeWidth={3} opacity={0.25} strokeDasharray="18 16" />
        <path d="M 948 492 L 968 462 L 936 452" fill="none" stroke={GOLD_L} strokeWidth={3} opacity={0.8} />
        <circle cx={540 - 430 * Math.cos(run * Math.PI)} cy={470 - 430 * Math.sin(run * Math.PI)} r={5.5} fill={GOLD_L} opacity={0.85} filter="url(#fGlow)" />
      </g>
    </svg>
  );
};

// ============ S14 沙漏 ============
const Hourglass: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.85);
  const fw = eo(cl(f / (dur * 0.35)));
  const sand = eo(cl((p - 0.15) / 0.65));
  const cx = 540, top = 240, bot = 600, hw = 170;
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={360} o={0.55} cy={420} />
      <g opacity={fw}>
        <path d={`M ${cx - hw} ${top} L ${cx + hw} ${top} L ${cx} ${(top + bot) / 2} L ${cx + hw} ${bot} L ${cx - hw} ${bot} L ${cx} ${(top + bot) / 2} Z`} fill="url(#gGold)" fillOpacity={0.06} stroke={W_} strokeOpacity={0.42} strokeWidth={2.6} />
        <line x1={cx - hw - 26} y1={top - 16} x2={cx + hw + 26} y2={top - 16} stroke={W_} strokeOpacity={0.5} strokeWidth={3} />
        <line x1={cx - hw - 26} y1={bot + 16} x2={cx + hw + 26} y2={bot + 16} stroke={W_} strokeOpacity={0.5} strokeWidth={3} />
        <rect x={cx - hw + 10} y={top + 6} width={26} height={150} rx={13} fill="url(#gSheen)" opacity={0.08} />
      </g>
      {Array.from({ length: 9 }, (_, k) => {
        const prog = cl((sand - k * 0.06) / 0.5);
        const y = top + 24 + ((k * 34 + f * 1.6) % (bot - top - 60));
        return <circle key={k} cx={cx + Math.sin(k * 2.1 + f * 0.08) * 5} cy={y} r={2.8} fill={GOLD_L} opacity={0.75 * prog} />;
      })}
      <g opacity={eo(sand)}>
        <path d={`M ${cx - 46} ${bot - 8} Q ${cx} ${bot - 8 - 54 * sand} ${cx + 46} ${bot - 8} Z`} fill="url(#gGold)" opacity={0.85} />
        <circle cx={cx} cy={bot - 14} r={16} fill="url(#gGlow)" />
      </g>
    </svg>
  );
};

// ============ S15 上升柱 ============
const Steps: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.8);
  const bp = (f % BEAT_FRAMES) / BEAT_FRAMES;
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={400} o={0.6} cy={470} />
      {Array.from({ length: 6 }, (_, k) => {
        const g = eo(cl((p - k * 0.08) / 0.42));
        const h = (70 + k * 58) * g;
        const x = 168 + k * 128;
        const last = k === 5;
        return (
          <g key={k}>
            <rect x={x} y={600 - h} width={92} height={h} rx={6} fill={last ? "url(#gGold)" : "#111116"} fillOpacity={last ? 0.55 : 0.6} stroke={last ? GOLD_L : DIM} strokeOpacity={last ? 0.9 : 0.35} strokeWidth={last ? 2.6 : 1.8} filter={last ? "url(#fGlow)" : undefined} />
            {last && <rect x={x} y={600 - h} width={92} height={26 * bp} rx={6} fill={W_} opacity={0.12} />}
          </g>
        );
      })}
      <line x1={140} y1={604} x2={940} y2={604} stroke={W_} strokeOpacity={0.18} strokeWidth={2} />
    </svg>
  );
};

// ============ S16 落版 ============
const Logo: React.FC<{ f: number; dur: number }> = ({ f, dur }) => {
  const p = P(f, dur, 0.85);
  const bp = (f % BEAT_FRAMES) / BEAT_FRAMES;
  return (
    <svg viewBox="0 0 1080 880" width={1080} height={880}>
      <Defs />
      <Halo r={460} o={0.85} cy={400} />
      {[286, 208, 132].map((r, i) => {
        const g = eo(cl((p - i * 0.11) / 0.5));
        return (
          <circle
            key={i}
            cx={540}
            cy={420}
            r={r * (0.72 + 0.28 * g)}
            fill="none"
            stroke="url(#gGold)"
            strokeWidth={i === 0 ? 3.2 : i === 1 ? 2.4 : 1.8}
            opacity={g * (0.26 + i * 0.2)}
            filter={i === 2 ? "url(#fGlow)" : undefined}
          />
        );
      })}
      <circle cx={540} cy={420} r={286 + 34 * (1 - bp)} fill="none" stroke={GOLD_L} strokeWidth={1.4} opacity={0.45 * bp} />
      <circle cx={540} cy={420} r={64} fill="url(#gGlow)" opacity={0.9} />
      <Orb x={540} y={420} r={13 + 4 * Math.sin(bp * Math.PI)} glow />
    </svg>
  );
};

export const ILLS: Record<string, React.FC<{ f: number; dur: number }>> = {
  gauge: Gauge,
  bubbles: Bubbles,
  wall: Wall,
  converge: Converge,
  twoCards: TwoCards,
  distill: Distill,
  modelVoid: ModelVoid,
  calcJudge: CalcJudge,
  onlyYou: OnlyYou,
  selfDistill: SelfDistill,
  calendar: Calendar,
  duplicate: Duplicate,
  loop: Loop,
  hourglass: Hourglass,
  steps: Steps,
  logo: Logo,
};
