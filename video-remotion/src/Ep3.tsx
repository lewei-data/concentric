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
  BLACKS,
  FPS3,
  HEIGHT3,
  PUNCH,
  PUNCH_END,
  SHOTS,
  SUBS,
  TAILCARD_START,
  TOTAL3,
  WIDTH3,
} from "./ep3Timeline";

/* 同心圆 · 第三支《重复的事，交给分身》—— 无旁白字幕卡样片（TTS 音色未签发）
 * 视觉按 CCO 沈青禾 2026-11-10 签发：黑金＋暖灰。
 *   暖灰只做承载层（纸面／方格墙），黑金只做结论层（金句卡、黑场、收口）。
 *   同一帧不同时出现金色花字与暖灰大字卡 —— 纸面镜字幕用近黑，结论镜字幕用金。
 *   #7C3AED 仅作尾帧识别色，面积 < 5%，不铺底。
 * 画面全部 SVG/DOM 代码绘制，零外部素材、无人物、无第三方界面。
 */

const PAPER = "#E8E3DA";
const INK = "#1B1710";
const INK_DIM = "#6A6153";
const GOLD = "#C9A227";
const GOLD_HI = "#D8B24A";
const DEEP = "#14100B";
const ID_PURPLE = "#7C3AED";

const clamp01 = (v: number) => Math.max(0, Math.min(1, v));
const cl = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;

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

/** 纸面：暖灰承载层，无实拍、无纹理外部图 */
const Paper: React.FC<{ t: number }> = ({ t }) => (
  <AbsoluteFill style={{ backgroundColor: PAPER }}>
    <AbsoluteFill
      style={{
        background: "radial-gradient(ellipse 880px 700px at 50% 32%, rgba(255,255,255,0.18) 0%, rgba(255,255,255,0) 72%)",
      }}
    />
    <AbsoluteFill
      style={{
        background: "radial-gradient(ellipse 120% 92% at 50% 52%, rgba(0,0,0,0) 44%, rgba(62,52,38,0.20) 100%)",
      }}
    />
    <AbsoluteFill style={{ opacity: 0.05 }}>
      <svg viewBox={`0 0 ${WIDTH3} ${HEIGHT3}`} width={WIDTH3} height={HEIGHT3}>
        {Array.from({ length: 90 }, (_, i) => {
          const x = ((i * 733) % 1080) + Math.sin(t * 0.2 + i) * 3;
          const y = ((i * 317) % 1920) + Math.cos(t * 0.17 + i) * 3;
          return <circle key={i} cx={x} cy={y} r={1.6} fill={INK} opacity={0.5} />;
        })}
      </svg>
    </AbsoluteFill>
  </AbsoluteFill>
);

/* ───────── 方格墙几何（全片唯一视觉锚点：起／转／收三次以上） ───────── */
const COLS = 6;
const ROWS = 8;
const CELL = 100;
const GAP = 18;
const WALL_W = COLS * CELL + (COLS - 1) * GAP; // 690
const WALL_H = ROWS * CELL + (ROWS - 1) * GAP; // 926
const WALL_X = (WIDTH3 - WALL_W) / 2;
const WALL_Y = 430;

const cellPos = (col: number, row: number) => ({
  x: WALL_X + col * (CELL + GAP),
  y: WALL_Y + row * (CELL + GAP),
});
/** 自下而上的填充序（底行先满） */
const rankBottomUp = (col: number, row: number) => (ROWS - 1 - row) * COLS + col;

const Cell: React.FC<{
  x: number;
  y: number;
  s: number;
  fill: number;
  opacity?: number;
  stroke?: string;
  gold?: string;
}> = ({ x, y, s, fill, opacity = 1, stroke = INK, gold = GOLD }) => (
  <div style={{ position: "absolute", left: x, top: y, width: s, height: s, opacity }}>
    <div style={{ position: "absolute", inset: 0, border: `2px solid ${stroke}`, opacity: 0.42 }} />
    <div style={{ position: "absolute", inset: 0, backgroundColor: gold, opacity: 0.9 * clamp01(fill) }} />
  </div>
);

/* ───────── 镜01 钩子 0.0–4.0 ───────── */
const Shot1: React.FC<{ t: number }> = ({ t }) => {
  const since = t - 0.8; // 起黑 0.0–0.8
  const barW = interpolate(since, [0, 0.24], [0, 268], cl);
  const sq = interpolate(since, [0, 0.55], [0.9, 1], cl);
  const sqOp = interpolate(since, [0, 0.45], [0, 0.95], cl);
  return (
    <>
      <Paper t={t} />
      {/* 主色条左展 240ms ease-out（金） */}
      <div
        style={{
          position: "absolute",
          left: WIDTH3 / 2 - 134,
          top: 800,
          width: barW,
          height: 3,
          backgroundColor: GOLD,
          opacity: 0.92,
        }}
      />
      {/* 中心黑金空心方格 */}
      <div
        style={{
          position: "absolute",
          left: WIDTH3 / 2 - 116,
          top: 890,
          width: 232,
          height: 232,
          border: `3px solid ${GOLD}`,
          transform: `scale(${sq})`,
          opacity: sqOp,
        }}
      />
    </>
  );
};

/* ───────── 镜02 痛点 4.0–14.0 ───────── */
const Shot2: React.FC<{ t: number }> = ({ t }) => {
  const local = t - 4.0;
  const shift = interpolate(t, [7.8, 8.6], [0, 36], cl); // 位移 ≤40px
  const C2 = 4;
  const R2 = 5;
  const S2 = 112;
  const G2 = 26;
  const w2 = C2 * S2 + (C2 - 1) * G2;
  const h2 = R2 * S2 + (R2 - 1) * G2;
  const x2 = (WIDTH3 - w2) / 2;
  const y2 = 505;
  const hotCell = 6; // 单格填充变化
  const hot = clamp01((local - 1.9) / 0.3);
  return (
    <>
      <Paper t={t} />
      {/* 另一块暖灰底（阵列平移的目的地） */}
      <div
        style={{
          position: "absolute",
          left: x2 + shift - 26,
          top: y2 - 26,
          width: w2 + 52,
          height: h2 + 52,
          backgroundColor: PAPER,
          border: `1px solid ${INK}`,
          opacity: 0.16,
        }}
      />
      {Array.from({ length: C2 * R2 }, (_, i) => {
        const col = i % C2;
        const row = Math.floor(i / C2);
        const appear = clamp01((local - i * 0.1) / 0.2); // 逐格淡入 200ms
        const isHot = i === hotCell;
        const glow = isHot ? hot : 0.14 * hot; // 单格填充时其余格同步亮度响应
        return (
          <Cell
            key={i}
            x={x2 + col * (S2 + G2) + shift}
            y={y2 + row * (S2 + G2)}
            s={S2}
            fill={isHot ? hot : glow}
            opacity={appear * 0.96}
          />
        );
      })}
    </>
  );
};

/* ───────── 镜03 循环 14.0–26.0 ───────── */
const Shot3: React.FC<{ t: number }> = ({ t }) => {
  const filling = t < 25.4;
  const fill = clamp01((t - 14.6) / (23.0 - 14.6)); // 匀速填充
  const flip = interpolate(t, [18.2, 19.8], [0, 1], cl); // 纯几何位移翻页
  const clear = clamp01((t - 25.4) / 0.6); // 25.4–26.0 清空为暖灰空格
  const wallOpacity = 1 - 0.55 * clear;
  return (
    <>
      <Paper t={t} />
      {filling ? (
        <>
          <div style={{ position: "absolute", left: 0, top: -flip * WALL_H, width: WIDTH3, height: HEIGHT3, opacity: 1 - flip }}>
            <Wall fill={fill} />
          </div>
          <div style={{ position: "absolute", left: 0, top: (1 - flip) * WALL_H, width: WIDTH3, height: HEIGHT3, opacity: flip }}>
            {/* 同构图、同填充点：新页与旧页一致 */}
            <Wall fill={fill} />
          </div>
        </>
      ) : (
        <div style={{ opacity: wallOpacity }}>
          <Wall fill={0} />
        </div>
      )}
    </>
  );
};

const Wall: React.FC<{ fill: number; dim?: number }> = ({ fill, dim = 1 }) => (
  <>
    {Array.from({ length: COLS * ROWS }, (_, i) => {
      const col = i % COLS;
      const row = Math.floor(i / COLS);
      const rank = rankBottomUp(col, row);
      const p = clamp01((fill - (rank / (COLS * ROWS))) * (COLS * ROWS) * 1.35);
      const { x, y } = cellPos(col, row);
      return <Cell key={i} x={x} y={y} s={CELL} fill={p} opacity={dim} />;
    })}
  </>
);

/* ───────── 镜04 接管 26.0–38.0 ───────── */
const Shot4: React.FC<{ t: number }> = ({ t }) => {
  const local = t - 26.0;
  const wallDim = interpolate(local, [0, 3.0], [0.85, 0.16], cl);
  const colX = 928;
  return (
    <>
      <Paper t={t} />
      <div style={{ opacity: wallDim }}>
        <Wall fill={1} />
      </div>
      {Array.from({ length: 12 }, (_, k) => {
        const row = ROWS - 1 - (k < 6 ? 0 : 1);
        const col = k % 6;
        const { x, y } = cellPos(col, row);
        const start = k * 0.62;
        const p = clamp01((local - start) / 1.15);
        const tx = colX;
        const ty = 640 + k * 52;
        const px = x + (tx - x) * ease(p);
        const py = y + (ty - y) * ease(p);
        const s = CELL + (52 - CELL) * ease(p);
        return <Cell key={`d${k}`} x={px} y={py} s={s} fill={1} opacity={clamp01(p * 1.6) * 0.92} />;
      })}
      {/* 列末抽象回执方块：纯色块，无文字、无界面 */}
      <div
        style={{
          position: "absolute",
          left: colX + 26 - 42,
          top: 640 + 11 * 52 + 74,
          width: 84,
          height: 84,
          backgroundColor: GOLD,
          opacity: clamp01((local - 9.0) / 0.4) * 0.95,
          transform: `scale(${interpolate(local, [9.0, 9.4], [0.82, 1], cl)})`,
        }}
      />
    </>
  );
};

const ease = (p: number) => (p <= 0 ? 0 : p >= 1 ? 1 : 1 - Math.pow(1 - p, 3));

/* ───────── 镜05 归还 38.0–46.0 ───────── */
const Shot5: React.FC<{ t: number }> = ({ t }) => {
  const local = t - 38.0;
  const grow = interpolate(local, [0, 1.6], [0, 1], cl);
  const path = "240,760 400,760 400,900 560,900 560,820 720,820 720,980 862,980";
  const draw = clamp01(local / 1.68); // 每段 240ms 左展
  return (
    <>
      <Paper t={t} />
      <AbsoluteFill
        style={{
          background: "radial-gradient(ellipse 520px 420px at 50% 46%, rgba(255,255,255,0.55) 0%, rgba(255,255,255,0) 70%)",
          opacity: grow * 0.75,
        }}
      />
      <svg viewBox={`0 0 ${WIDTH3} ${HEIGHT3}`} width={WIDTH3} height={HEIGHT3} style={{ position: "absolute", inset: 0 }}>
        <polyline
          points={path}
          fill="none"
          stroke={GOLD}
          strokeWidth={2.4}
          strokeLinecap="square"
          pathLength={1}
          strokeDasharray={1}
          strokeDashoffset={1 - draw}
          opacity={0.9}
        />
      </svg>
    </>
  );
};

/* ───────── 镜06 收束 46.0–50.0 ───────── */
const Shot6: React.FC<{ t: number }> = ({ t }) => {
  const local = t - 46.0;
  const dark = clamp01(local / 0.5);
  const punchOp = (i: number) => {
    const p = PUNCH[i];
    const on = clamp01((t - p.t) / 0.18);
    const off = clamp01((t - (PUNCH_END - 0.2)) / 0.2);
    return on * (1 - off);
  };
  const tail = t >= TAILCARD_START ? 1 : 0; // 硬切进入，保证尾卡 1.2s 真定格
  return (
    <>
      <AbsoluteFill style={{ opacity: 1 - dark }}>
        <Paper t={t} />
      </AbsoluteFill>
      <AbsoluteFill style={{ backgroundColor: DEEP, opacity: dark }} />
      {/* 结论层：金色主句，居中两行（暖灰大字卡不同帧共存） */}
      {local < PUNCH_END - 46.0 + 0.2 && (
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
          {PUNCH.map((p, i) => (
            <div
              key={i}
              style={{
                fontSize: 92,
                letterSpacing: 4,
                color: GOLD_HI,
                lineHeight: 1.5,
                opacity: punchOp(i),
              }}
            >
              {p.text}
            </div>
          ))}
        </AbsoluteFill>
      )}
      {/* 尾卡定格 1.2s：同系列落版＋#7C3AED 识别色（仅尾帧，面积 < 5%） */}
      {t >= TAILCARD_START && (
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", opacity: tail }}>
          <div style={{ fontSize: 64, letterSpacing: 20, color: "#F2EDE4" }}>同 心 圆</div>
          <div style={{ marginTop: 22, width: 92, height: 1, backgroundColor: GOLD, opacity: 0.7 }} />
          <div style={{ marginTop: 26, fontSize: 26, letterSpacing: 3, color: INK_DIM }}>
            第三支｜重复的事，交给分身
          </div>
          <div
            style={{
              marginTop: 40,
              width: 26,
              height: 26,
              borderRadius: 13,
              border: `2px solid ${ID_PURPLE}`,
              opacity: 0.85,
            }}
          />
        </AbsoluteFill>
      )}
    </>
  );
};

/* ───────── 字幕层：单行 ≤14 字，主字幕 1 行＋承接 1 行（≤2 行） ───────── */
const Subtitle: React.FC<{ t: number }> = ({ t }) => {
  const idx = SUBS.findIndex((s) => t >= s.t0 && t < s.t1);
  if (idx < 0) return null;
  const cur = SUBS[idx];
  if (cur.shot === 6) return null; // 镜06 落点句由 Shot6 居中呈现
  const prev = idx > 0 && SUBS[idx - 1].shot === cur.shot ? SUBS[idx - 1] : null;
  const op = interpolate(t, [cur.t0, cur.t0 + 0.06, cur.t1 - 0.06, cur.t1], [0, 1, 1, 0], cl);
  const size = Math.min(64, Math.floor(900 / Math.max(1, cur.text.length)));
  return (
    <AbsoluteFill
      style={{ top: 1392, height: 330, justifyContent: "flex-start", alignItems: "center", opacity: op }}
    >
      {prev && (
        <div
          style={{
            fontSize: 40,
            letterSpacing: 2,
            color: INK_DIM,
            opacity: 0.5,
            marginBottom: 26,
          }}
        >
          {prev.text}
        </div>
      )}
      <div style={{ fontSize: size, letterSpacing: 2, color: INK, lineHeight: 1.3, whiteSpace: "nowrap" }}>
        {cur.text}
      </div>
    </AbsoluteFill>
  );
};

/* ───────── 主合成 ───────── */
export const Ep3: React.FC = () => {
  useEmbeddedFont();
  const f = useCurrentFrame();
  const t = f / FPS3;
  const isBlack = BLACKS.some(([a, b]) => t >= a && t < b);

  const shot = SHOTS.find((s) => t >= s.start && t < s.end) ?? SHOTS[SHOTS.length - 1];

  return (
    <AbsoluteFill style={{ backgroundColor: PAPER, fontFamily: "NotoSansSC, sans-serif" }}>
      <Audio src={staticFile("ep3-bed.wav")} />
      {!isBlack && (
        <>
          {shot.id === "S1" && <Shot1 t={t} />}
          {shot.id === "S2" && <Shot2 t={t} />}
          {shot.id === "S3" && <Shot3 t={t} />}
          {shot.id === "S4" && <Shot4 t={t} />}
          {shot.id === "S5" && <Shot5 t={t} />}
          {shot.id === "S6" && <Shot6 t={t} />}
          <Subtitle t={t} />
        </>
      )}
      {/* 黑场：全黑、无字幕、无声（0.0–0.8／17.0–17.4／25.0–25.4） */}
      {isBlack && <AbsoluteFill style={{ backgroundColor: "#000000" }} />}
    </AbsoluteFill>
  );
};

export const EP3_DURATION = TOTAL3;
