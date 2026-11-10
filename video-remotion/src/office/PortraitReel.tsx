import React, { useEffect, useState } from "react";
import {
  AbsoluteFill,
  OffthreadVideo,
  Sequence,
  continueRender,
  delayRender,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {
  CHARACTERS,
  CHAR_FRAMES,
  Character,
  INTRO_FRAMES,
  OUTRO_FRAMES,
  OVERLAP,
} from "./scenes";

const GOLD = "#D8B26A";
const WHITE = "#F2F4F8";
const DIM = "#9AA3B2";

const useEmbeddedFont = () => {
  const [handle] = useState(() => delayRender("loading-noto-sans-sc"));
  useEffect(() => {
    let alive = true;
    const face = new FontFace(
      "NotoSansSC",
      `url(${staticFile("fonts/NotoSansSC-Regular.otf")})`,
    );
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

const fadeEdges = (frame: number, duration: number, edge = OVERLAP) =>
  Math.min(
    1,
    interpolate(frame, [0, edge], [0, 1], { extrapolateRight: "clamp" }),
    interpolate(frame, [duration - edge, duration], [1, 0], {
      extrapolateLeft: "clamp",
    }),
  );

/** 缓慢流动的底色光斑，给静态画面一点呼吸感 */
const Background: React.FC<{ accent: string }> = ({ accent }) => {
  const frame = useCurrentFrame();
  const t = frame / 30;
  const x1 = 30 + 8 * Math.sin(t * 0.35);
  const y1 = 22 + 5 * Math.cos(t * 0.28);
  const x2 = 72 + 7 * Math.cos(t * 0.22);
  const y2 = 78 + 6 * Math.sin(t * 0.31);
  return (
    <AbsoluteFill style={{ backgroundColor: "#07090E" }}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(60% 45% at ${x1}% ${y1}%, ${accent}33 0%, transparent 62%),
                       radial-gradient(55% 40% at ${x2}% ${y2}%, ${GOLD}18 0%, transparent 60%),
                       linear-gradient(180deg, #0A0E16 0%, #06080C 100%)`,
        }}
      />
      <AbsoluteFill
        style={{
          backgroundImage:
            "linear-gradient(rgba(255,255,255,0.028) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.028) 1px, transparent 1px)",
          backgroundSize: "90px 90px",
          maskImage: "radial-gradient(70% 60% at 50% 45%, #000 30%, transparent 100%)",
          WebkitMaskImage:
            "radial-gradient(70% 60% at 50% 45%, #000 30%, transparent 100%)",
        }}
      />
    </AbsoluteFill>
  );
};

const ClipWindow: React.FC<{
  clips: string[];
  accent: string;
  progress: number;
}> = ({ clips, accent, progress }) => {
  const sub = CHAR_FRAMES / Math.max(1, clips.length);
  return (
    <div
      style={{
        width: 760,
        height: 760,
        borderRadius: 44,
        overflow: "hidden",
        position: "relative",
        boxShadow: `0 40px 120px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.10), 0 0 90px ${accent}22`,
      }}
    >
      {clips.map((clip, i) => {
        const from = Math.round(i * sub) - (i === 0 ? 0 : 8);
        const dur = Math.round(sub) + 8;
        return (
          <Sequence key={clip} from={from} durationInFrames={dur} layout="none">
            <ClipLayer clip={clip} index={i} sub={sub} />
          </Sequence>
        );
      })}
      <div
        style={{
          position: "absolute",
          inset: 0,
          borderRadius: 40,
          background: `linear-gradient(180deg, transparent 55%, rgba(4,6,10,0.55) 100%)`,
        }}
      />
      <div
        style={{
          position: "absolute",
          left: 0,
          bottom: 0,
          height: 3,
          width: `${progress * 100}%`,
          background: `linear-gradient(90deg, ${GOLD}, ${accent})`,
        }}
      />
    </div>
  );
};

const ClipLayer: React.FC<{
  clip: string;
  index: number;
  sub: number;
}> = ({ clip, index, sub }) => {
  const frame = useCurrentFrame();
  const fade = interpolate(
    frame,
    [0, 8, Math.round(sub), Math.round(sub) + 8],
    [0, 1, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );
  const drift = interpolate(frame, [0, Math.round(sub) + 8], [1.0, 1.07], {
    extrapolateRight: "clamp",
  });
  const panY = interpolate(frame, [0, Math.round(sub) + 8], [10, -10], {
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill style={{ opacity: index === 0 ? 1 : fade }}>
      <OffthreadVideo
        src={staticFile(clip)}
        muted
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          transform: `scale(${drift}) translateY(${panY}px)`,
        }}
      />
    </AbsoluteFill>
  );
};

const CharacterScene: React.FC<{ c: Character; index: number; total: number }> = ({
  c,
  index,
  total,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const opacity = fadeEdges(frame, CHAR_FRAMES);
  const enter = spring({ frame: frame - 8, fps, config: { damping: 200 } });
  const enter2 = spring({ frame: frame - 20, fps, config: { damping: 200 } });
  const lineIn = spring({ frame: frame - 38, fps, config: { damping: 200 } });
  const progress = interpolate(frame, [0, CHAR_FRAMES - OVERLAP], [0, 1], {
    extrapolateRight: "clamp",
  });
  // 文字比画面更早退场，避免转场重叠时出现"上一张脸配下一个名字"
  const textFade = interpolate(
    frame,
    [0, 16, CHAR_FRAMES - 56, CHAR_FRAMES - 30],
    [0, 1, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );

  return (
    <AbsoluteFill style={{ opacity, fontFamily: "NotoSansSC, sans-serif" }}>
      <Background accent={c.accent} />
      <AbsoluteFill
        style={{
          alignItems: "center",
          justifyContent: "center",
          paddingTop: 60,
          paddingBottom: 40,
        }}
      >
        <div
          style={{
            fontSize: 24,
            letterSpacing: 10,
            color: GOLD,
            opacity: 0.8 * enter * textFade,
            transform: `translateY(${(1 - enter) * 18}px)`,
          }}
        >
          0{index + 1} / 0{total}
        </div>
        <div
          style={{
            marginTop: 34,
            opacity: enter,
            transform: `translateY(${(1 - enter) * 26}px)`,
          }}
        >
          <ClipWindow clips={c.clips} accent={c.accent} progress={progress} />
        </div>
        <div
          style={{
            marginTop: 74,
            fontSize: 76,
            letterSpacing: 6,
            color: WHITE,
            opacity: enter2 * textFade,
            transform: `translateY(${(1 - enter2) * 24}px)`,
          }}
        >
          {c.name}
        </div>
        <div
          style={{
            marginTop: 22,
            display: "flex",
            alignItems: "center",
            gap: 14,
            opacity: enter2 * textFade,
          }}
        >
          <div style={{ width: 42, height: 1, backgroundColor: c.accent, opacity: 0.7 }} />
          <div style={{ fontSize: 28, letterSpacing: 5, color: c.accent }}>{c.role}</div>
          <div style={{ width: 42, height: 1, backgroundColor: c.accent, opacity: 0.7 }} />
        </div>
        <div
          style={{
            marginTop: 52,
            padding: "0 110px",
            fontSize: 34,
            lineHeight: 1.7,
            letterSpacing: 1,
            color: DIM,
            textAlign: "center",
            opacity: lineIn * textFade,
            transform: `translateY(${(1 - lineIn) * 20}px)`,
          }}
        >
          {c.line}
        </div>
      </AbsoluteFill>
      <div
        style={{
          position: "absolute",
          bottom: 96,
          width: "100%",
          display: "flex",
          justifyContent: "center",
          gap: 12,
          opacity: 0.9 * enter * textFade,
        }}
      >
        {Array.from({ length: total }).map((_, i) => (
          <div
            key={i}
            style={{
              width: i === index ? 30 : 8,
              height: 4,
              borderRadius: 2,
              backgroundColor: i === index ? GOLD : "rgba(255,255,255,0.22)",
            }}
          />
        ))}
      </div>
    </AbsoluteFill>
  );
};

const Intro: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const opacity = fadeEdges(frame, INTRO_FRAMES);
  const a = spring({ frame: frame - 10, fps, config: { damping: 200 } });
  const b = spring({ frame: frame - 34, fps, config: { damping: 200 } });
  const lineW = interpolate(frame, [12, 60], [0, 300], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill
      style={{
        opacity,
        fontFamily: "NotoSansSC, sans-serif",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Background accent="#8FA8C8" />
      <div style={{ fontSize: 26, letterSpacing: 12, color: GOLD, opacity: 0.85 * a }}>
        OPC · HERMES
      </div>
      <div
        style={{
          marginTop: 40,
          fontSize: 104,
          letterSpacing: 18,
          color: WHITE,
          opacity: a,
          transform: `translateY(${(1 - a) * 30}px)`,
        }}
      >
        分身办公室
      </div>
      <div style={{ marginTop: 46, width: lineW, height: 2, backgroundColor: GOLD, opacity: 0.7 }} />
      <div
        style={{
          marginTop: 46,
          fontSize: 32,
          letterSpacing: 6,
          color: DIM,
          opacity: b,
        }}
      >
        六个数字员工 · 一个调度中心
      </div>
      <div style={{ marginTop: 16, fontSize: 24, letterSpacing: 3, color: DIM, opacity: 0.55 * b }}>
        AnimateDiff 微动 × SD1.5 写实肖像 × Remotion 合成
      </div>
    </AbsoluteFill>
  );
};

const Outro: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const opacity = fadeEdges(frame, OUTRO_FRAMES);
  const a = spring({ frame: frame - 14, fps, config: { damping: 200 } });
  const b = spring({ frame: frame - 34, fps, config: { damping: 200 } });
  return (
    <AbsoluteFill
      style={{
        opacity,
        fontFamily: "NotoSansSC, sans-serif",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Background accent="#C08AA0" />
      <div style={{ fontSize: 82, letterSpacing: 10, color: WHITE, opacity: a }}>
        重复的事，交给分身
      </div>
      <div
        style={{
          marginTop: 44,
          fontSize: 30,
          letterSpacing: 6,
          color: GOLD,
          opacity: 0.9 * b,
        }}
      >
        你只负责判断
      </div>
      <div style={{ marginTop: 90, fontSize: 22, letterSpacing: 2, color: DIM, opacity: 0.5 * b }}>
        SD1.5 + AnimateDiff v1.5.2 · 纯 CPU 本地生成
      </div>
    </AbsoluteFill>
  );
};

export const PortraitReel: React.FC = () => {
  useEmbeddedFont();
  const step = CHAR_FRAMES - OVERLAP;
  const charStart = (i: number) => INTRO_FRAMES - OVERLAP + i * step;
  const outroStart = charStart(CHARACTERS.length - 1) + step;
  return (
    <AbsoluteFill style={{ backgroundColor: "#07090E" }}>
      <Sequence durationInFrames={INTRO_FRAMES} layout="none">
        <Intro />
      </Sequence>
      {CHARACTERS.map((c, i) => (
        <Sequence
          key={c.id}
          from={charStart(i)}
          durationInFrames={CHAR_FRAMES}
          layout="none"
        >
          <CharacterScene c={c} index={i} total={CHARACTERS.length} />
        </Sequence>
      ))}
      <Sequence from={outroStart} durationInFrames={OUTRO_FRAMES} layout="none">
        <Outro />
      </Sequence>
      <AbsoluteFill
        style={{
          pointerEvents: "none",
          background:
            "radial-gradient(120% 80% at 50% 50%, transparent 55%, rgba(0,0,0,0.55) 100%)",
        }}
      />
    </AbsoluteFill>
  );
};