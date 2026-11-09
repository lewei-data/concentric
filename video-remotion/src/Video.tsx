import React, { useEffect, useState } from "react";
import {
  AbsoluteFill,
  Audio,
  continueRender,
  delayRender,
  Easing,
  Img,
  interpolate,
  staticFile,
  useCurrentFrame,
} from "remotion";
import { BEAT_FRAMES, FPS, HEIGHT, SCENES, Scene, TOTAL_FRAMES, WIDTH } from "./scenes";
import { ILLS } from "./Ill";

const GOLD = "#D8B24A";
const WHITE = "#F2EDE4";

const GOLDEN = Easing.out(Easing.cubic);

/** 确定性伪随机，保证每帧可重放 */
const rnd = (i: number, salt = 0) => {
  const x = Math.sin(i * 12.9898 + salt * 78.233) * 43758.5453;
  return x - Math.floor(x);
};

/** 字体：渲染前必须等字体就绪，否则首帧会用回退字体 */
const useEmbeddedFont = () => {
  const [handle] = useState(() => delayRender("loading-font"));
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

const sceneAt = (f: number) => {
  for (let i = SCENES.length - 1; i >= 0; i--) {
    if (f >= SCENES[i].start) return i;
  }
  return 0;
};

/** 背景：深色基底 + 中心暖光 + 极慢旋转光晕，做出"布面"质感 */
const Backdrop: React.FC<{ f: number }> = ({ f }) => {
  const spin = (f / FPS) * 6; // 6°/s
  return (
    <AbsoluteFill style={{ background: "#0B0B0E" }}>
      <AbsoluteFill
        style={{
          background: "linear-gradient(168deg, #121218 0%, #0B0B0E 45%, #07070A 100%)",
        }}
      />
      <AbsoluteFill
        style={{
          background:
            "radial-gradient(ellipse 700px 560px at 50% 26%, rgba(201,162,39,0.10) 0%, rgba(201,162,39,0.035) 45%, rgba(0,0,0,0) 74%)",
        }}
      />
      <AbsoluteFill
        style={{
          transform: `rotate(${spin}deg) scale(1.6)`,
          background:
            "conic-gradient(from 0deg at 50% 50%, rgba(201,162,39,0.045) 0deg, rgba(0,0,0,0) 70deg, rgba(201,162,39,0.02) 160deg, rgba(0,0,0,0) 250deg, rgba(201,162,39,0.045) 360deg)",
          opacity: 0.45,
        }}
      />
      <AbsoluteFill
        style={{
          background:
            "radial-gradient(ellipse 78% 62% at 50% 46%, rgba(0,0,0,0) 48%, rgba(0,0,0,0.55) 100%)",
        }}
      />
    </AbsoluteFill>
  );
};

/** 漂浮金尘：给整幅画面一层空气感，不依赖每个场景单独实现 */
const Dust: React.FC<{ f: number }> = ({ f }) => {
  return (
    <AbsoluteFill style={{ top: 90, height: 880 }}>
      <svg viewBox="0 0 1080 880" width={WIDTH} height={880}>
        {Array.from({ length: 42 }, (_, i) => {
          const sp = 5 + rnd(i, 3) * 11; // px/s
          const x0 = rnd(i, 1) * 1080;
          const y0 = rnd(i, 2) * 880;
          const y = (((y0 - f / FPS * sp) % 880) + 880) % 880;
          const tw = 0.45 + 0.55 * Math.sin(f * 0.07 + i * 1.7);
          const r = 1.1 + rnd(i, 4) * 1.9;
          const hot = i % 9 === 0;
          return (
            <circle
              key={i}
              cx={x0 + Math.sin(f * 0.02 + i) * 9}
              cy={y}
              r={hot ? r * 1.6 : r}
              fill={hot ? GOLD : "#F2EDE4"}
              opacity={(hot ? 0.24 : 0.13) * Math.max(0, tw)}
            />
          );
        })}
      </svg>
    </AbsoluteFill>
  );
};

/** 每拍一次的脉冲环：画面节奏的视觉锚点 */
const BeatRing: React.FC<{ f: number }> = ({ f }) => {
  const beat = Math.floor(f / BEAT_FRAMES);
  const local = (f - beat * BEAT_FRAMES) * (FPS / FPS); // 帧
  const p = local / (BEAT_FRAMES * 0.85);
  if (p <= 0 || p >= 1) return null;
  const e = GOLDEN(p);
  const r = 150 + 360 * e;
  const o = 0.45 * Math.pow(1 - p, 1.2);
  return (
    <AbsoluteFill style={{ top: 90, height: 880 }}>
      <svg viewBox="0 0 1080 880" width={WIDTH} height={880}>
        <circle cx={540} cy={420} r={r} fill="none" stroke={GOLD} strokeWidth={2.4} opacity={o} />
        <circle cx={540} cy={420} r={r - 7} fill="none" stroke="#F4DE9A" strokeWidth={1} opacity={o * 0.6} />
      </svg>
    </AbsoluteFill>
  );
};

/** 单场景插图 + 缓慢推近 / 漂移 / 交叉淡入 */
const SceneLayer: React.FC<{ scene: Scene; f: number; opacity: number }> = ({ scene, f, opacity }) => {
  if (opacity <= 0.01) return null;
  const Ill = ILLS[scene.ill];
  if (!Ill) return null;
  const local = f - scene.start;
  const drift = Math.sin(((f / FPS) / 7.8) * Math.PI * 2) * 5;
  const push = 1.0 + 0.05 * Math.min(1, local / scene.dur);
  return (
    <AbsoluteFill
      style={{
        top: 90,
        height: 880,
        opacity,
        transform: `translateY(${drift}px) scale(${push})`,
        transformOrigin: "540px 460px",
      }}
    >
      <Ill f={Math.max(0, local)} dur={scene.dur} />
    </AbsoluteFill>
  );
};

/** 字幕：逐行错位入场，退场在切换拍之前收完 */
const Subtitles: React.FC<{ scene: Scene; f: number }> = ({ scene, f }) => {
  if (!scene.lines) return null;
  const local = f - scene.start;
  const IN = 8.5; // 340ms
  const OUT = 6; // 240ms
  const STAGGER = 2; // 85ms ≈ 2 帧
  const outAt = scene.dur - OUT;
  const heights = scene.lines.map((l) => l.size * 1.42 + (l.size < 60 ? 56 : 0));
  const total = heights.reduce((a, b) => a + b, 0);
  const top = 1315 - total / 2;
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", left: 0, top, width: WIDTH }}>
        {scene.lines.map((line, i) => {
          const s = i * STAGGER;
          const inP = interpolate(local, [s, s + IN], [0, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
            easing: GOLDEN,
          });
          const outP = interpolate(local, [outAt, outAt + OUT], [0, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
            easing: Easing.in(Easing.cubic),
          });
          const alpha = inP * (1 - outP);
          if (alpha <= 0.01) return null;
          const y = 26 * (1 - inP) - 14 * outP;
          return (
            <div
              key={i}
              style={{
                height: heights[i],
                lineHeight: `${heights[i]}px`,
                textAlign: "center",
                color: line.gold ? GOLD : WHITE,
                fontSize: line.size,
                fontFamily: "NotoSansSC",
                letterSpacing: line.size < 60 ? 2 : 0,
                opacity: alpha,
                transform: `translateY(${y}px)`,
                textShadow: line.gold ? "0 0 26px rgba(216,178,74,0.35)" : "0 2px 18px rgba(0,0,0,0.55)",
              }}
            >
              {line.text}
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

export const Video: React.FC = () => {
  useEmbeddedFont();
  const f = useCurrentFrame();

  const idx = sceneAt(f);
  const cur = SCENES[idx];
  const prev = idx > 0 ? SCENES[idx - 1] : null;
  const local = f - cur.start;
  const XF = 6;
  const fadeIn = clamp01(local / XF);
  const prevOpacity = prev && local < XF ? 1 - fadeIn : 0;

  return (
    <AbsoluteFill style={{ backgroundColor: "#08080A" }}>
      <Backdrop f={f} />

      {prev && <SceneLayer scene={prev} f={f} opacity={prevOpacity} />}
      <SceneLayer scene={cur} f={f} opacity={local < XF ? fadeIn : 1} />

      <Dust f={f} />
      <BeatRing f={f} />

      <Subtitles scene={cur} f={f} />

      {/* 底部压暗：把抖音评论区/按钮区让出来 */}
      <AbsoluteFill
        style={{
          top: 1560,
          height: HEIGHT - 1560,
          background: "linear-gradient(to bottom, rgba(0,0,0,0) 0%, rgba(0,0,0,0.62) 70%, rgba(0,0,0,0.78) 100%)",
        }}
      />
      {/* 顶部细压暗 */}
      <AbsoluteFill
        style={{
          height: 120,
          background: "linear-gradient(to bottom, rgba(0,0,0,0.45), rgba(0,0,0,0))",
        }}
      />

      {/* 颗粒：让深色渐变不再出现色带 */}
      <Img
        src={staticFile("grain.png")}
        style={{
          position: "absolute",
          width: WIDTH,
          height: HEIGHT,
          objectFit: "cover",
          opacity: 0.10,
          mixBlendMode: "overlay",
        }}
      />

      <Audio
        src={staticFile("bgm_cut.wav")}
        volume={(fr) => {
          const fin = interpolate(fr, [0, 1.5 * FPS], [0, 1], { extrapolateRight: "clamp" });
          const fout = interpolate(fr, [TOTAL_FRAMES - 4 * FPS, TOTAL_FRAMES], [1, 0], { extrapolateLeft: "clamp" });
          return Math.min(fin, fout);
        }}
      />
    </AbsoluteFill>
  );
};

function clamp01(v: number) {
  return Math.max(0, Math.min(1, v));
}
