import React from "react";
import { Composition } from "remotion";
import { Video } from "./Video";
import { Ep3 } from "./Ep3";
import { FPS, HEIGHT, TOTAL_FRAMES, WIDTH } from "./timeline";
import { FPS3, HEIGHT3, TOTAL3, WIDTH3 } from "./ep3Timeline";

/** 时长来自 206 tts MCP 的句级时间轴（见 src/timeline.ts），不再是音乐节拍推算。 */
export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="Tongxinyuan"
        component={Video}
        durationInFrames={TOTAL_FRAMES}
        fps={FPS}
        width={WIDTH}
        height={HEIGHT}
      />
      {/* 第三支《重复的事，交给分身》—— 无旁白字幕卡样片，50s／30fps */}
      <Composition
        id="TongxinyuanEp3"
        component={Ep3}
        durationInFrames={TOTAL3}
        fps={FPS3}
        width={WIDTH3}
        height={HEIGHT3}
      />
    </>
  );
};
