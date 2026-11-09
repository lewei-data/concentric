import React, { useEffect, useState } from "react";
import {
  AbsoluteFill,
  Audio,
  continueRender,
  delayRender,
  interpolate,
  staticFile,
  useCurrentFrame,
} from "remotion";
import {
  CHAPTERS,
  DURATION_SECONDS,
  FPS,
  HEIGHT,
  SEGMENTS,
  WIDTH,
  Chapter,
  Seg,
} from "./timeline";

/* 同心圆 · 第二支《AI 时代的数字资产》TTS 口播版
 * 节奏完全由 206 tts MCP 产出的句级时间轴驱动（src/timeline.ts）。
 * 字体：思源黑体 Noto Sans SC Regular（SIL OFL 1.1）
 * 视觉：全部 SVG / CSS 代码绘制，零外部图片
 * 音频：206 自制 BGM + TTS 口播 ducking 混音（public/ep2-mix.wav）
 */

const GOLD = "#D8B24A";
const GOLD_DIM = "#C9A227";
const WHITE = "#F2EDE4";
const DIM = "#8B877F";

/** 关键落点词上金色（按句 index） */
const HIGHLIGHTS: Record<number, string[]> = {
  0: ["什么还算是你的"],
  1: ["稀缺"],
  2: ["二零二六年七月"],
  3: ["三权分置"],
  4: ["自然人"],
  5: ["可登记的凭证"],
  6: ["上线试运行"],
  7: ["越吃数据"],
  8: ["四件"],
  9: ["留痕", "确权", "守界", "复用"],
  10: ["合规地复用"],
  11: ["署名"],
};

/** 每章底部角标（事实来源，不出现平台 LOGO / 真实界面） */
const FOOTNOTES: Record<number, string> = {
  0: "同心圆 · 系列第二支",
  1: "国家数据局《数据产权登记工作指引（试行）》｜国数综政策〔2026〕35 号",
  2: "指引：自然人对其合法采集、生成、获取的数据可登记",
  3: "国家数据产权登记服务系统上线试运行｜国家数据集团挂牌｜2026",
  4: "个人侧落点：留痕 → 确权 → 守界 → 复用（由登记规则推导，非收益承诺）",
  5: "事实依据：国家数据局公开文件，2026.11｜不承诺收益、不构成投资或法律意见",
};

/** 字体必须等加载完成，否则首帧回退到系统字体 */
const useEmbeddedFont = () => {
  const [handle] = useState(() => delayRender("loading-noto-sans-sc"));
  useEffect(() => {
    let alive = true;
    const face = new FontFace("NotoSansSC", `url(${staticFile("fonts/NotoSansSC-Regular.otf")})`);
    face
      .load()
      .then((loaded) => {
        if (!alive) return;
        (document.fonts as unknown as { add: (f: FontFace) => void }).add(loaded);
        continueRender(handle);
      })
      .catch(() => {
        if (alive) continueRender(handle);
      });
    return () => {
      alive = false;
    };
  }, [handle]);
};

const rnd = (i: number, salt = 0) => {
  const x = Math.sin(i * 12.9898 + salt * 78.233) * 43758.5453;
  return x - Math.floor(x);
};

const clamp01 = (v: number) => Math.max(0, Math.min(1, v));

/** 把句子切成 1–2 行，优先在标点处断行 */
const splitLines = (text: string): string[] => {
  if (text.length <= 14) return [text];
  const cuts = "，、？。";
  const mid = Math.ceil(text.length / 2);
  let cut = mid;
  for (let d = 0; d < 6; d++) {
    for (const c of [mid - d, mid + d]) {
      if (c > 3 && c < text.length - 3 && cuts.includes(text[c])) {
        cut = c + 1;
        return [text.slice(0, cut), text.slice(cut)];
      }
    }
  }
  return [text.slice(0, mid), text.slice(mid)];
};

type Part = { t: string; gold: boolean };
/** 把高亮词切成金色片段 */
const colorize = (text: string, highs: string[]): Part[] => {
  let parts: Part[] = [{ t: text, gold: false }];
  for (const h of highs) {
    const next: Part[] = [];
    for (const p of parts) {
      if (p.gold || !h) {
        next.push(p);
        continue;
      }
      let rest = p.t;
      let idx = rest.indexOf(h);
      while (idx >= 0) {
        if (idx > 0) next.push({ t: rest.slice(0, idx), gold: false });
        next.push({ t: h, gold: true });
        rest = rest.slice(idx + h.length);
        idx = rest.indexOf(h);
      }
      if (rest) next.push({ t: rest, gold: false });
    }
    parts = next;
  }
  return parts;
};

const ColoredLine: React.FC<{ text: string; highs: string[] }> = ({ text, highs }) => (
  <span>
    {colorize(text, highs).map((p, i) => (
      <span key={i} style={{ color: p.gold ? GOLD : WHITE }}>{p.t}</span>
    ))}
  </span>
);

/** 背景：近黑渐变 + 金色径向光晕 + 代码生成的颗粒，无外部图片 */
const Backdrop: React.FC<{ f: number; t: number }> = ({ f, t }) => (
  <AbsoluteFill style={{ background: "#0A0A0D" }}>
    <AbsoluteFill style={{ background: "linear-gradient(168deg,#121218 0%,#0B0B0E 45%,#07070A 100%)" }} />
    <AbsoluteFill
      style={{
        background:
          "radial-gradient(ellipse 700px 560px at 50% 26%, rgba(201,162,39,0.11) 0%, rgba(201,162,39,0.035) 45%, rgba(0,0,0,0) 74%)",
      }}
    />
    <AbsoluteFill
      style={{
        background:
          "radial-gradient(ellipse 80% 64% at 50% 46%, rgba(0,0,0,0) 48%, rgba(0,0,0,0.58) 100%)",
      }}
    />
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} width={WIDTH} height={HEIGHT}>
      {Array.from({ length: 46 }, (_, i) => {
        const y0 = rnd(i, 2) * HEIGHT;
        const y = (((y0 - t * (5 + rnd(i, 3) * 10)) % HEIGHT) + HEIGHT) % HEIGHT;
        const x = rnd(i, 1) * WIDTH + Math.sin(f * 0.02 + i) * 8;
        const tw = 0.45 + 0.55 * Math.sin(f * 0.07 + i * 1.7);
        return (
          <circle
            key={i}
            cx={x}
            cy={y}
            r={1 + rnd(i, 4) * 1.8}
            fill={i % 9 === 0 ? GOLD : WHITE}
            opacity={(i % 9 === 0 ? 0.22 : 0.11) * Math.max(0, tw)}
          />
        );
      })}
    </svg>
  </AbsoluteFill>
);

/** 常驻同心圆：细金环 + 缓慢呼吸，章节切换时外扩一次 */
const Rings: React.FC<{ t: number; chapter: Chapter; chapterP: number }> = ({ t, chapter, chapterP }) => {
  const radii = [150, 220, 290, 360, 430, 500];
  const expand = interpolate(chapterP, [0, 0.25, 1], [1, 1.045, 1], { extrapolateRight: "clamp" });
  return (
    <svg viewBox="0 0 1080 760" width={1080} height={760} style={{ position: "absolute", top: 300 }}>
      {radii.map((r, i) => {
        const breathe = 1 + 0.012 * Math.sin(t * 0.55 + i * 0.7);
        const a = 0.055 + 0.035 * Math.sin(t * 0.4 + i * 0.9);
        return (
          <circle
            key={i}
            cx={540}
            cy={380}
            r={r * breathe * expand}
            fill="none"
            stroke={GOLD_DIM}
            strokeWidth={i % 2 === 0 ? 1.2 : 0.8}
            opacity={Math.max(0.025, a)}
          />
        );
      })}
      <circle cx={540} cy={380} r={5} fill={GOLD} opacity={0.85} />
      <circle cx={540} cy={380} r={16 + 5 * Math.sin(t * 1.1)} fill="none" stroke={GOLD} strokeWidth={1} opacity={0.35} />
    </svg>
  );
};

/** 章节几何：6 种状态，全部代码绘制，随章节内进度点亮 */
const ChapterGeometry: React.FC<{ ill: string; p: number; pulse: number }> = ({ ill, p, pulse }) => {
  const cx = 540, cy = 380;
  const lit = (i: number, n: number) => clamp01((p - (i / n) * 0.72) / 0.28);
  const node = (x: number, y: number, a: number, r = 9) => (
    <g key={`${x}-${y}`}>
      <circle cx={x} cy={y} r={r + 9 * a} fill={GOLD} opacity={0.08 * a} />
      <circle cx={x} cy={y} r={r} fill={GOLD} opacity={0.35 + 0.55 * a} />
      <circle cx={x} cy={y} r={r + 10} fill="none" stroke={GOLD} strokeWidth={1} opacity={0.5 * a} />
    </g>
  );

  if (ill === "spark") {
    return (
      <svg viewBox="0 0 1080 760" width={1080} height={760} style={{ position: "absolute", top: 300 }}>
        {[0, 1, 2].map((k) => {
          const q = clamp01((p * 2.2 - k * 0.42) / 0.9);
          return <circle key={k} cx={cx} cy={cy} r={20 + q * (180 + k * 70)} fill="none" stroke={GOLD} strokeWidth={1.4} opacity={0.5 * (1 - q)} />;
        })}
        <circle cx={cx} cy={cy} r={7 + 4 * pulse} fill={GOLD} />
      </svg>
    );
  }
  if (ill === "certificate") {
    const a = clamp01(p / 0.35);
    return (
      <svg viewBox="0 0 1080 760" width={1080} height={760} style={{ position: "absolute", top: 300 }}>
        <g opacity={a}>
          <rect x={365} y={205} width={350} height={350} rx={22} fill="#12131A" stroke={GOLD_DIM} strokeWidth={1.5} opacity={0.9} />
          {[0, 1, 2].map((k) => (
            <line key={k} x1={405} y1={285 + k * 56} x2={675 - k * 55} y2={285 + k * 56} stroke={WHITE} strokeWidth={2} opacity={0.28 + 0.35 * clamp01((p - 0.2 - k * 0.18) / 0.3)} />
          ))}
          <circle cx={640} cy={480} r={44} fill="none" stroke={GOLD} strokeWidth={2.4} opacity={clamp01((p - 0.55) / 0.3)} />
          <circle cx={640} cy={480} r={33} fill="none" stroke={GOLD} strokeWidth={1} opacity={clamp01((p - 0.6) / 0.3) * 0.6} />
        </g>
      </svg>
    );
  }
  if (ill === "three_rings") {
    return (
      <svg viewBox="0 0 1080 760" width={1080} height={760} style={{ position: "absolute", top: 300 }}>
        {[0, 1, 2].map((k) => {
          const a = lit(k, 3);
          return (
            <g key={k}>
              <circle cx={cx} cy={cy} r={110 + k * 95} fill="none" stroke={GOLD} strokeWidth={2.2} opacity={0.12 + 0.55 * a} />
              <circle cx={cx} cy={cy} r={110 + k * 95} fill="none" stroke={GOLD} strokeWidth={7} opacity={0.06 * a} />
              {node(cx + (110 + k * 95) * Math.cos(-Math.PI / 2 + k * 0.9), cy + (110 + k * 95) * Math.sin(-Math.PI / 2 + k * 0.9), a, 7)}
            </g>
          );
        })}
      </svg>
    );
  }
  if (ill === "lattice") {
    return (
      <svg viewBox="0 0 1080 760" width={1080} height={760} style={{ position: "absolute", top: 300 }}>
        {Array.from({ length: 18 }, (_, i) => {
          const col = i % 6, row = Math.floor(i / 6);
          const a = lit(i, 18);
          return (
            <g key={i}>
              <rect x={300 + col * 82} y={220 + row * 105} width={54} height={54} rx={8} fill="#12131A" stroke={GOLD_DIM} strokeWidth={1} opacity={0.25 + 0.55 * a} />
              <circle cx={327 + col * 82} cy={247 + row * 105} r={4} fill={GOLD} opacity={0.2 + 0.7 * a} />
            </g>
          );
        })}
      </svg>
    );
  }
  if (ill === "four_nodes") {
    const labels = ["留痕", "确权", "守界", "复用"];
    return (
      <svg viewBox="0 0 1080 760" width={1080} height={760} style={{ position: "absolute", top: 300 }}>
        {[0, 1, 2, 3].map((k) => {
          const ang = -Math.PI / 2 + (k * Math.PI) / 2;
          const x = cx + 225 * Math.cos(ang), y = cy + 225 * Math.sin(ang);
          const a = lit(k, 4);
          return (
            <g key={k}>
              <line x1={cx} y1={cy} x2={x} y2={y} stroke={GOLD_DIM} strokeWidth={1} opacity={0.1 + 0.4 * a} />
              {node(x, y, a, 10)}
              <text x={x} y={y + (k === 2 ? 58 : -44)} textAnchor="middle" fontFamily="NotoSansSC" fontSize={27} fill={WHITE} opacity={0.25 + 0.7 * a}>
                {labels[k]}
              </text>
            </g>
          );
        })}
      </svg>
    );
  }
  // collapse
  return (
    <svg viewBox="0 0 1080 760" width={1080} height={760} style={{ position: "absolute", top: 300 }}>
      {[0, 1, 2, 3, 4].map((k) => {
        const r = (460 - k * 70) * (1 - 0.34 * p) + 14;
        return <circle key={k} cx={cx} cy={cy} r={Math.max(10, r)} fill="none" stroke={GOLD} strokeWidth={1.2} opacity={0.1 + 0.4 * (1 - p) + k * 0.03} />;
      })}
      <circle cx={cx} cy={cy} r={8 + 5 * p} fill={GOLD} />
    </svg>
  );
};

export const Video: React.FC = () => {
  useEmbeddedFont();
  const f = useCurrentFrame();
  const t = f / FPS;

  // 当前句 / 当前章
  const seg: Seg | undefined = SEGMENTS.find((s) => t >= s.start && t < s.end);
  const lastEnd = SEGMENTS[SEGMENTS.length - 1].end;
  const isOutro = t >= lastEnd;
  const activeSeg = seg ?? SEGMENTS.find((s) => t >= s.end) ?? SEGMENTS[0];
  const chapter: Chapter = CHAPTERS.find((c) => t >= c.start && t <= c.end + 0.5) ?? CHAPTERS[CHAPTERS.length - 1];
  const chapterP = clamp01((t - chapter.start) / Math.max(0.001, chapter.end - chapter.start));
  const pulse = seg ? clamp01(1 - (t - seg.start) / 0.5) : 0;

  // 字幕淡入淡出（0.3s）
  const segOpacity = seg
    ? interpolate(t, [seg.start, seg.start + 0.3, seg.end - 0.28, seg.end], [0, 1, 1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : 0;
  const lines = splitLines(activeSeg.text);
  const maxLen = Math.max(...lines.map((l) => l.length));
  const fontSize = Math.min(78, Math.floor(880 / Math.max(1, maxLen)));
  const highs = HIGHLIGHTS[activeSeg.index] ?? [];
  const introFade = interpolate(t, [0, 0.9], [0, 1], { extrapolateRight: "clamp" });
  const outroFade = interpolate(t, [lastEnd, lastEnd + 0.7], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });

  return (
    <AbsoluteFill style={{ backgroundColor: "#0A0A0D", fontFamily: "NotoSansSC, sans-serif" }}>
      <Audio src={staticFile("ep2-mix.wav")} />
      <Backdrop f={f} t={t} />
      {/* 片尾时几何淡出，避免落版文字压在圆环上 */}
      <AbsoluteFill style={{ opacity: 1 - outroFade * 0.9 }}>
        <Rings t={t} chapter={chapter} chapterP={chapterP} />
        <ChapterGeometry ill={chapter.ill} p={chapterP} pulse={pulse} />
      </AbsoluteFill>

      {/* 章节标题（片尾隐藏） */}
      {!isOutro && (
        <div style={{ position: "absolute", top: 150, width: WIDTH, textAlign: "center", opacity: introFade * 0.9 }}>
          <div style={{ fontSize: 26, letterSpacing: 8, color: GOLD, opacity: 0.75 }}>
            {String(chapter.index + 1).padStart(2, "0")} / {chapter.title}
          </div>
          <div style={{ margin: "18px auto 0", width: 64, height: 1, background: GOLD_DIM, opacity: 0.55 }} />
        </div>
      )}

      {/* 逐句字幕 */}
      {!isOutro && (
        <AbsoluteFill style={{ top: 1150, height: 340, justifyContent: "center", alignItems: "center", opacity: segOpacity }}>
          <div style={{ width: 940, textAlign: "center", lineHeight: 1.42 }}>
            {lines.map((ln, i) => (
              <div key={i} style={{ fontSize, fontWeight: 400, letterSpacing: 2, whiteSpace: "nowrap" }}>
                <ColoredLine text={ln} highs={highs} />
              </div>
            ))}
          </div>
        </AbsoluteFill>
      )}

      {/* 句进度点：12 个点，当前句点亮（片尾隐藏） */}
      {!isOutro && (
        <div style={{ position: "absolute", top: 1535, width: WIDTH, display: "flex", justifyContent: "center", gap: 14, opacity: introFade }}>
          {SEGMENTS.map((s) => {
            const on = seg && s.index === seg.index;
            const done = t > s.end;
            return (
              <div key={s.index} style={{ width: on ? 22 : 7, height: 7, borderRadius: 4, backgroundColor: on ? GOLD : WHITE, opacity: on ? 0.95 : done ? 0.3 : 0.14 }} />
            );
          })}
        </div>
      )}

      {/* 角标 */}
      {!isOutro && (
        <div style={{ position: "absolute", top: 1700, width: WIDTH, textAlign: "center", opacity: introFade * 0.62 }}>
          <div style={{ fontSize: 24, color: DIM, letterSpacing: 1, padding: "0 90px", lineHeight: 1.5 }}>
            {FOOTNOTES[chapter.index]}
          </div>
        </div>
      )}

      {/* 片尾落版 */}
      {isOutro && (
        <AbsoluteFill style={{ opacity: outroFade }}>
          <AbsoluteFill style={{ background: "radial-gradient(ellipse 720px 520px at 50% 50%, rgba(8,8,11,0.92) 0%, rgba(8,8,11,0.62) 58%, rgba(8,8,11,0) 100%)" }} />
          <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
          <div style={{ fontSize: 62, letterSpacing: 18, color: WHITE }}>同 心 圆</div>
          <div style={{ marginTop: 34, fontSize: 28, letterSpacing: 4, color: GOLD, opacity: 0.85 }}>模型学得会知识 · 学不走署名</div>
          <div style={{ marginTop: 70, fontSize: 21, color: DIM, opacity: 0.7 }}>事实依据：国家数据局公开文件，2026.11</div>
          <div style={{ marginTop: 14, fontSize: 19, color: DIM, opacity: 0.55 }}>不承诺收益 · 不构成投资或法律意见</div>
          </AbsoluteFill>
        </AbsoluteFill>
      )}
    </AbsoluteFill>
  );
};
