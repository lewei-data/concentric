import React from "react";
import { Composition } from "remotion";
import { Video } from "./Video";
import { FPS, HEIGHT, TOTAL_FRAMES, WIDTH } from "./timeline";

/** 时长来自 206 tts MCP 的句级时间轴（见 src/timeline.ts），不再是音乐节拍推算。 */
export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Tongxinyuan"
      component={Video}
      durationInFrames={TOTAL_FRAMES}
      fps={FPS}
      width={WIDTH}
      height={HEIGHT}
    />
  );
};
