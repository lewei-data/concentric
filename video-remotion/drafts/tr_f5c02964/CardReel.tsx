import React, { useEffect, useState } from "react";
import {
  AbsoluteFill,
  Img,
  Sequence,
  continueRender,
  delayRender,
  interpolate,
  staticFile,
  useCurrentFrame,
} from "remotion";

/**
 * tr_f5c02964 备料件（草案，未注册进 Root.tsx、未渲染）。
 * 字卡＋静帧·无旁白；1080×1920／30fps／1350 帧＝45.0s。
 * 卡文字逐字取自 03-storyboard.md，未改一字；卡号/镜号不上屏。
 */

export const MAIN = "#7C3AED";
const SAFE = 108;

type Card = {
  no: number;
  shot: 1 | 2 | 3 | 4;
  lines: string[];
  from: number;
  frames: number;
};

export const CARDS: Card[] = [
  { no: 1, shot: 1, lines: ["它只写方案"], from: 0, frames: 165 },
  { no: 2, shot: 1, lines: ["动手那一步留给你"], from: 165, frames: 165 },
  { no: 3, shot: 2, lines: ["方案单可出"], from: 330, frames: 172 },
  { no: 4, shot: 2, lines: ["改动必须回客户签"], from: 502, frames: 173 },
  { no: 5, shot: 3, lines: ["建议，确认，", "执行，留痕"], from: 675, frames: 172 },
  { no: 6, shot: 3, lines: ["四段卡，一步一张"], from: 847, frames: 173 },
  { no: 7, shot: 4, lines: ["留痕按时间戳复算"], from: 1020, frames: 165 },
  { no: 8, shot: 4, lines: ["同种子只复现参数"], from: 1185, frames: 165 },
];

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
      .catch(() => continueRender(handle));
    return () => {
      alive = false;
    };
  }, [handle]);
};

const CardText: React.FC<{ lines: string[]; duration: number }> = ({ lines, duration }) => {
  const frame = useCurrentFrame();
  const fadeIn = interpolate(frame, [0, 9], [0, 1], { extrapolateRight: "clamp" });
  const fadeOut = interpolate(frame, [duration - 9, duration - 1], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const opacity = Math.min(fadeIn, fadeOut);
  const rise = interpolate(frame, [0, 14], [16, 0], { extrapolateRight: "clamp" });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        paddingLeft: SAFE,
        paddingRight: SAFE,
      }}
    >
      <div
        style={{
          opacity,
          transform: `translateY(${rise}px)`,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
        }}
      >
        {lines.map((line, i) => (
          <div
            key={i}
            style={{
              fontFamily: "NotoSansSC, sans-serif",
              fontSize: 96,
              lineHeight: 1.38,
              fontWeight: 400,
              color: "#FFFFFF",
              textAlign: "center",
              whiteSpace: "nowrap",
            }}
          >
            {line}
          </div>
        ))}
        <div
          style={{
            marginTop: 46,
            width: 120,
            height: 6,
            backgroundColor: MAIN,
            borderRadius: 3,
          }}
        />
      </div>
    </AbsoluteFill>
  );
};

export const CardReel: React.FC = () => {
  useEmbeddedFont();
  return (
    <AbsoluteFill style={{ backgroundColor: "#0D0916" }}>
      {CARDS.map((c) => (
        <Sequence key={c.no} from={c.from} durationInFrames={c.frames} name={`card-${c.no}`}>
          <AbsoluteFill>
            <Img
              src={staticFile(`tr_f5c02964/plate-shot${c.shot}.png`)}
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
            />
            <CardText lines={c.lines} duration={c.frames} />
          </AbsoluteFill>
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
