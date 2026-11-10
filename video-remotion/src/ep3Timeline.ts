/* 同心圆 · 第三支《重复的事，交给分身》—— 时间轴数据源
 * 上游：江佑泽 2026-11-10 07:22 交件（INBOX uid11）口播稿＋逐镜字幕，50s／净字203。
 * 本文件只落时间码与逐字原句，正文一字未改、题面未改。
 * 交付口径：1080×1920／30fps＝1500 帧（《公司/项目配置.md》第 48 行 tongxinyuan）。
 */

export const FPS3 = 30;
export const WIDTH3 = 1080;
export const HEIGHT3 = 1920;
export const DURATION3 = 50;
export const TOTAL3 = Math.round(DURATION3 * FPS3); // 1500

export type Shot = { id: string; name: string; start: number; end: number };

export const SHOTS: Shot[] = [
  { id: "S1", name: "钩子", start: 0, end: 4 },
  { id: "S2", name: "痛点", start: 4, end: 14 },
  { id: "S3", name: "循环", start: 14, end: 26 },
  { id: "S4", name: "接管", start: 26, end: 38 },
  { id: "S5", name: "归还", start: 38, end: 46 },
  { id: "S6", name: "收束", start: 46, end: 50 },
];

/** 两处黑场：17.0s＝50s×34%，25.0s＝50s×50%，各 0.4s，全黑、无字幕、无声 */
export const BLACKS: Array<[number, number]> = [
  [0, 0.8], // 起黑（镜01 指令：0.0–0.8 起黑）
  [17.0, 17.4],
  [25.0, 25.4],
];

export type Sub = { t0: number; t1: number; text: string; shot: number };

/** 逐镜字幕：时间码与逐字原句，逐条照抄交件（不改词、不合并、不增删） */
export const SUBS: Sub[] = [
  { t0: 0.800, t1: 1.616, text: "同一张表，", shot: 1 },
  { t0: 1.656, t1: 3.900, text: "你还记得这个月填过几遍？", shot: 1 },
  { t0: 4.000, t1: 5.191, text: "每天，同一张表。", shot: 2 },
  { t0: 5.231, t1: 6.223, text: "同样的几行，", shot: 2 },
  { t0: 6.263, t1: 7.850, text: "换个地方再填一遍。", shot: 2 },
  { t0: 7.900, t1: 8.512, text: "改一遍，", shot: 2 },
  { t0: 8.552, t1: 9.980, text: "别处跟着改一遍。", shot: 2 },
  { t0: 10.020, t1: 10.428, text: "填的，", shot: 2 },
  { t0: 10.468, t1: 12.100, text: "其实是同一份东西。", shot: 2 },
  { t0: 14.000, t1: 14.670, text: "日历上，", shot: 3 },
  { t0: 14.710, t1: 16.720, text: "格子一格一格被填满。", shot: 3 },
  { t0: 17.400, t1: 18.133, text: "填满一页，", shot: 3 },
  { t0: 18.173, t1: 18.723, text: "翻过去，", shot: 3 },
  { t0: 18.763, t1: 19.680, text: "还是这一页。", shot: 3 },
  { t0: 19.750, t1: 21.010, text: "你记得住动作，", shot: 3 },
  { t0: 21.050, t1: 22.520, text: "记不住是哪一遍。", shot: 3 },
  { t0: 22.560, t1: 23.190, text: "就这样，", shot: 3 },
  { t0: 23.230, t1: 24.280, text: "一遍又一遍。", shot: 3 },
  { t0: 26.000, t1: 26.796, text: "这些重复，", shot: 4 },
  { t0: 26.836, t1: 27.632, text: "交给分身。", shot: 4 },
  { t0: 27.672, t1: 29.264, text: "你只把口径说清楚，", shot: 4 },
  { t0: 29.304, t1: 30.100, text: "它照着做：", shot: 4 },
  { t0: 30.150, t1: 31.203, text: "同样的字段，", shot: 4 },
  { t0: 31.243, t1: 32.296, text: "同样的措辞，", shot: 4 },
  { t0: 32.336, t1: 33.600, text: "同样的时间点。", shot: 4 },
  { t0: 33.650, t1: 34.579, text: "不用你盯，", shot: 4 },
  { t0: 34.619, t1: 35.780, text: "也不用你催。", shot: 4 },
  { t0: 35.820, t1: 37.213, text: "做完只回一句，", shot: 4 },
  { t0: 37.253, t1: 37.950, text: "办好了。", shot: 4 },
  { t0: 38.000, t1: 39.161, text: "还回来的时间，", shot: 5 },
  { t0: 39.201, t1: 40.750, text: "不是让你多干一份。", shot: 5 },
  { t0: 40.800, t1: 41.827, text: "陪人吃顿饭，", shot: 5 },
  { t0: 41.867, t1: 42.689, text: "走一段路，", shot: 5 },
  { t0: 42.729, t1: 44.372, text: "把没看完的书看完，", shot: 5 },
  { t0: 44.412, t1: 45.850, text: "把想说的话说完。", shot: 5 },
];

/** 镜06 落点句（逐字，不改写） */
export const PUNCH: Array<{ t: number; text: string }> = [
  { t: 46.4, text: "分身不替你生活，" },
  { t: 47.85, text: "只替你重复。" },
];
export const PUNCH_END = 48.8;
export const TAILCARD_START = 48.8;

/** 净字自检：只计汉字，不含标点（交件口径 203，硬顶 220） */
export const NET_CHARS = [
  "同一张表你还记得这个月填过几遍",
  "每天同一张表同样的几行换个地方再填一遍改一遍别处跟着改一遍填的其实是同一份东西",
  "日历上格子一格一格被填满填满一页翻过去还是这一页你记得住动作记不住是哪一遍就这样一遍又一遍",
  "这些重复交给分身你只把口径说清楚它照着做同样的字段同样的措辞同样的时间点不用你盯也不用你催做完只回一句办好了",
  "还回来的时间不是让你多干一份陪人吃顿饭走一段路把没看完的书看完把想说的话说完",
  "分身不替你生活只替你重复",
].reduce((a, s) => a + s.length, 0);
