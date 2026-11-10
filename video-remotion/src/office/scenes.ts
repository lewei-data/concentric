export const FPS = 30;
export const WIDTH = 1080;
export const HEIGHT = 1920;

export type Character = {
  id: string;
  name: string;
  role: string;
  accent: string;
  line: string;
  /** 相对 public 的路径，按顺序串联成"多个动作" */
  clips: string[];
};

export const INTRO_FRAMES = 105;
export const CHAR_FRAMES = 170;
export const OUTRO_FRAMES = 115;
/** 相邻场景重叠的帧数，用于做淡入淡出转场 */
export const OVERLAP = 26;

export const CHARACTERS: Character[] = [
  {
    id: "boss",
    name: "蒿色莫",
    role: "老板",
    accent: "#8FA8C8",
    line: "先想清楚要什么，再让别人动手。",
    clips: ["motion/smooth/boss__idle.mp4", "motion/smooth/boss__nod.mp4"],
  },
  {
    id: "secretary",
    name: "小秘书",
    role: "老板影子分身",
    accent: "#C08AA0",
    line: "重复的事，交给我。",
    clips: [
      "motion/smooth/secretary__idle.mp4",
      "motion/smooth/secretary__smile.mp4",
    ],
  },
  {
    id: "ceo",
    name: "陆清安",
    role: "CEO",
    accent: "#7FA6D9",
    line: "把方向定下来，剩下的交给系统。",
    clips: ["motion/smooth/ceo__idle.mp4", "motion/smooth/ceo__turn.mp4"],
  },
  {
    id: "cco",
    name: "沈青禾",
    role: "首席创意官",
    accent: "#D18FA8",
    line: "好点子不该死在会议纪要里。",
    clips: ["motion/smooth/cco__idle.mp4", "motion/smooth/cco__smile.mp4"],
  },
  {
    id: "cto",
    name: "扶频",
    role: "CTO",
    accent: "#6FB3A6",
    line: "能自动化的，就别让人熬夜。",
    clips: ["motion/smooth/cto__idle.mp4", "motion/smooth/cto__nod.mp4"],
  },
  {
    id: "pm_wenren",
    name: "贺平生",
    role: "产品经理",
    accent: "#8FA3C4",
    line: "需求写清楚，比说得漂亮重要。",
    clips: ["motion/smooth/pm_wenren__idle.mp4", "motion/smooth/pm_wenren__lean.mp4"],
  },
];

const sceneLen = (n: number) => INTRO_FRAMES + n * CHAR_FRAMES + OUTRO_FRAMES;
export const TOTAL_FRAMES =
  sceneLen(CHARACTERS.length) - (CHARACTERS.length + 1) * OVERLAP;
