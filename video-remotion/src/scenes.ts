export const FPS = 25;
export const WIDTH = 1080;
export const HEIGHT = 1920;
// 实测 112.8 BPM → 每拍帧数
export const BEAT = 60 / 112.8;
export const BEAT_FRAMES = BEAT * FPS;
// 原曲起点（秒）
export const AUDIO_START = 17.02;

export type Line = { text: string; gold?: boolean; size: number };
export type Scene = {
  id: string;
  ill: string;
  beats: number;
  lines?: Line[];
  start: number; // 帧
  dur: number; // 帧
};

const RAW: Array<{ ill: string; beats: number; lines?: Line[] }> = [
  { ill: "gauge", beats: 8, lines: [{ text: "你做好", size: 118 }, { text: "被蒸馏的准备", size: 118 }, { text: "了吗？", size: 118, gold: true }] },
  { ill: "bubbles", beats: 6, lines: [{ text: "AI 时代", size: 108, gold: true }, { text: "技能不再稀缺", size: 108 }] },
  { ill: "wall", beats: 4, lines: [{ text: "知识的围墙", size: 104 }, { text: "塌了", size: 104, gold: true }] },
  { ill: "converge", beats: 8, lines: [{ text: "人类几千年的积累", size: 98 }, { text: "被压进一个个模型", size: 98, gold: true }] },
  { ill: "twoCards", beats: 6, lines: [{ text: "你写过的文章", size: 98 }, { text: "拍过的视频", size: 98 }] },
  { ill: "distill", beats: 8, lines: [{ text: "随时可以被蒸馏成", size: 98 }, { text: "一个 SKILL", size: 98, gold: true }] },
  { ill: "none", beats: 2 },
  { ill: "modelVoid", beats: 6, lines: [{ text: "但模型", size: 110 }, { text: "不知道你是谁", size: 110, gold: true }] },
  { ill: "calcJudge", beats: 6, lines: [{ text: "它替你算", size: 108 }, { text: "不替你判断", size: 108, gold: true }] },
  { ill: "onlyYou", beats: 6, lines: [{ text: "知道的", size: 112 }, { text: "只有你自己", size: 112, gold: true }] },
  { ill: "selfDistill", beats: 4, lines: [{ text: "主动蒸馏自己", size: 110, gold: true }] },
  { ill: "calendar", beats: 6, lines: [{ text: "从重复最多的", size: 102 }, { text: "那件事开始", size: 102 }] },
  { ill: "duplicate", beats: 6, lines: [{ text: "让 AI 成为", size: 108 }, { text: "你的分身", size: 108, gold: true }] },
  { ill: "loop", beats: 4, lines: [{ text: "替你干", size: 106 }, { text: "重复的活", size: 106, gold: true }] },
  { ill: "hourglass", beats: 6, lines: [{ text: "把时间还给", size: 102 }, { text: "更值得的事", size: 102, gold: true }] },
  { ill: "steps", beats: 6, lines: [{ text: "开启", size: 106 }, { text: "高价值的人生", size: 106, gold: true }] },
  { ill: "logo", beats: 10, lines: [{ text: "同 心 圆", size: 44 }, { text: "同心同频", size: 100 }, { text: "共谱华章", size: 100, gold: true }] },
];

export const SCENES: Scene[] = (() => {
  let acc = 0;
  const out: Scene[] = [];
  for (let i = 0; i < RAW.length; i++) {
    const r = RAW[i];
    const start = Math.round(acc * BEAT_FRAMES);
    acc += r.beats;
    const end = Math.round(acc * BEAT_FRAMES);
    out.push({ id: `S${String(i + 1).padStart(2, "0")}`, ill: r.ill, beats: r.beats, lines: r.lines, start, dur: end - start });
  }
  return out;
})();

export const TOTAL_FRAMES = Math.round(17 * BEAT_FRAMES * 0 + SCENES[SCENES.length - 1].start + SCENES[SCENES.length - 1].dur);
export const DURATION_SECONDS = TOTAL_FRAMES / FPS;
