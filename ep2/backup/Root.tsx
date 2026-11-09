import React from "react";
import { Composition } from "remotion";
import { Video } from "./Video";
import { FPS, HEIGHT, TOTAL_FRAMES, WIDTH } from "./scenes";

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
