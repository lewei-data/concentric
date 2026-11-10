import React from "react";
import { Composition } from "remotion";
import { Video } from "./Video";
import { Ep3 } from "./Ep3";
import { FPS, HEIGHT, TOTAL_FRAMES, WIDTH } from "./timeline";
import { FPS3, HEIGHT3, TOTAL3, WIDTH3 } from "./ep3Timeline";
import { PortraitReel } from "./office/PortraitReel";
import {
  FPS as OFFICE_FPS,
  HEIGHT as OFFICE_HEIGHT,
  TOTAL_FRAMES as OFFICE_TOTAL,
  WIDTH as OFFICE_WIDTH,
} from "./office/scenes";

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
      {/* 第三支《重复的事，交给分身》—— 口播版，50s／30fps */}
      <Composition
        id="TongxinyuanEp3"
        component={Ep3}
        durationInFrames={TOTAL3}
        fps={FPS3}
        width={WIDTH3}
        height={HEIGHT3}
      />
      {/* 分身办公室：AnimateDiff 微动片段串联 + Remotion 合成 */}
      <Composition
        id="PortraitReel"
        component={PortraitReel}
        durationInFrames={OFFICE_TOTAL}
        fps={OFFICE_FPS}
        width={OFFICE_WIDTH}
        height={OFFICE_HEIGHT}
      />
    </>
  );
};
