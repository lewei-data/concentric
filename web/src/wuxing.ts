/**
 * 五行气运：以当日干支 + 节气月令推算「今日宜用之行」，并驱动整套界面主题。
 *
 * 象义模型（自洽的轻量玄学，不依赖黄历接口）：
 *   - 月令（节气月）定当令之气，权重最高；
 *   - 日干为日主、日支为辅；
 *   - 相生加气、调候补偏（冬取火、夏取水、春取金火、秋取火水、四季取木水）；
 *   - 得分最高者即当日主题，一天之内稳定，不随页面刷新/时辰跳动。
 * 时辰只用于说明「此刻时气」，不参与选行；允许手动预览 / 锁定任意一行。
 */

import { useEffect, useState } from "react";

export type WuXing = "wood" | "fire" | "earth" | "metal" | "water";

export const WUXING_ORDER: WuXing[] = ["wood", "fire", "earth", "metal", "water"];

export interface WuXingTheme {
  key: WuXing;
  name: string;
  fullName: string;
  motto: string;
  classic: string;
  direction: string;
  season: string;
  verse: string;
  color: string;
  color2: string;
  vars: Record<string, string>;
  /** 48×48 圆徽内部图形（currentColor 便于 favicon 复染） */
  mark: string;
  /** 28×28 字标小符号 */
  sigil: string;
}

const GAN = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"];
const ZHI = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"];
const GAN_EL: WuXing[] = ["wood", "wood", "fire", "fire", "earth", "earth", "metal", "metal", "water", "water"];
const ZHI_EL: WuXing[] = ["water", "earth", "wood", "wood", "earth", "fire", "fire", "earth", "metal", "metal", "earth", "water"];

/** 节气月（公历近似日期）：寅卯木、巳午火、申酉金、亥子水、辰戌丑未土 */
const SOLAR_MONTHS: { m: number; d: number; zhi: number; label: string }[] = [
  { m: 1, d: 5, zhi: 1, label: "小寒" },
  { m: 2, d: 4, zhi: 2, label: "立春" },
  { m: 3, d: 5, zhi: 3, label: "惊蛰" },
  { m: 4, d: 5, zhi: 4, label: "清明" },
  { m: 5, d: 5, zhi: 5, label: "立夏" },
  { m: 6, d: 6, zhi: 6, label: "芒种" },
  { m: 7, d: 7, zhi: 7, label: "小暑" },
  { m: 8, d: 7, zhi: 8, label: "立秋" },
  { m: 9, d: 7, zhi: 9, label: "白露" },
  { m: 10, d: 8, zhi: 10, label: "寒露" },
  { m: 11, d: 7, zhi: 11, label: "立冬" },
  { m: 12, d: 7, zhi: 0, label: "大雪" },
];

const SHICHEN_TEXT = ["夜半", "鸡鸣", "平旦", "日出", "食时", "隅中", "日中", "日昳", "晡时", "日入", "黄昏", "人定"];

export interface WuXingReading {
  element: WuXing;
  dateKey: string;
  ganzhi: string;
  gan: string;
  zhi: string;
  monthLabel: string;
  monthElement: WuXing;
  dayElement: WuXing;
  hourLabel: string;
  hourElement: WuXing;
  scores: Record<WuXing, number>;
  reason: string;
  advice: string;
}

const texture = {
  wood:
    "repeating-linear-gradient(48deg, rgba(var(--accent-rgb), .022) 0 1px, transparent 1px 30px)," +
    "radial-gradient(760px 420px at 86% 12%, rgba(var(--halo-1-rgb), .05), transparent 70%)",
  fire:
    "radial-gradient(900px 520px at 82% 6%, rgba(var(--halo-2-rgb), .09), transparent 68%)," +
    "repeating-linear-gradient(120deg, rgba(var(--accent-rgb), .018) 0 2px, transparent 2px 34px)",
  earth:
    "repeating-linear-gradient(90deg, rgba(var(--accent-rgb), .026) 0 1px, transparent 1px 72px)," +
    "repeating-linear-gradient(0deg, rgba(var(--accent-rgb), .026) 0 1px, transparent 1px 72px)",
  metal:
    "repeating-linear-gradient(135deg, rgba(var(--halo-2-rgb), .05) 0 1px, transparent 1px 44px)," +
    "radial-gradient(900px 460px at 12% 100%, rgba(var(--halo-1-rgb), .07), transparent 68%)",
  water:
    "radial-gradient(620px 620px at 78% 8%, transparent 0 90px, rgba(var(--accent-rgb), .028) 91px 92px, transparent 93px 182px, rgba(var(--accent-rgb), .022) 183px 184px, transparent 185px)," +
    "radial-gradient(780px 520px at 10% 112%, rgba(var(--halo-2-rgb), .07), transparent 70%)",
};

export const WUXING: Record<WuXing, WuXingTheme> = {
  wood: {
    key: "wood",
    name: "木",
    fullName: "木行",
    motto: "曲直生发",
    classic: "木曰曲直",
    direction: "东",
    season: "春",
    verse: "如木向荣，一日一新",
    color: "#3ea874",
    color2: "#123f2c",
    vars: {
      "--accent": "#2e8b62", "--accent-deep": "#1b6b47", "--accent-soft": "#e8f5ed", "--accent-rgb": "46, 139, 98",
      "--violet": "#6fb98a", "--violet-rgb": "111, 185, 138",
      "--bg": "#f3f8f4", "--bg-deep": "#e7f1ea", "--surface": "#ffffff", "--surface-soft": "#f7fbf8",
      "--border": "#dcebe0", "--border-strong": "#c4ddcc",
      "--text": "#1a2c23", "--text-soft": "#354c3e", "--muted": "#6d8574",
      "--page-1": "#f6fbf7", "--page-2": "#e9f3ec",
      "--halo-1-rgb": "46, 139, 98", "--halo-2-rgb": "111, 185, 138",
      "--bar-1": "#0d2b20", "--bar-2": "#174a34", "--bar-3": "#2b704c",
      "--bar-line-1": "#52c78c", "--bar-line-2": "#a8e6bd", "--bar-line-3": "#2e8b62",
      "--glow-1-rgb": "120, 216, 165", "--glow-2-rgb": "86, 176, 125",
      "--logo-1": "#3ea874", "--logo-2": "#123f2c",
      "--wx-texture": texture.wood, "--radius": "16px", "--radius-sm": "11px",
      "--shadow-tint-rgb": "20, 62, 42",
    },
    mark: `<circle cx="24" cy="24" r="16.5" fill="none" stroke="currentColor" stroke-opacity=".3" stroke-width="1"/>
      <path d="M24 35V17" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
      <path d="M24 26C18.5 26 15 22.5 15 17c5.5 0 9 3.5 9 9Z" fill="currentColor" fill-opacity=".9"/>
      <path d="M24 22c5.5 0 9-3.5 9-9-5.5 0-9 3.5-9 9Z" fill="currentColor"/>
      <path d="M16 35c2.6 1.6 5.1 2.4 8 2.4s5.4-.8 8-2.4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
      <circle cx="24" cy="13.5" r="1.5" fill="currentColor" fill-opacity=".75"/>`,
    sigil: `<path d="M14 25V11" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
      <path d="M14 18C9 18 6.5 15 6.5 10.5c5 0 7.5 3 7.5 7.5Z" fill="currentColor"/>
      <path d="M14 15c5 0 7.5-3 7.5-7.5-5 0-7.5 3-7.5 7.5Z" fill="currentColor" fill-opacity=".7"/>
      <path d="M6 25c2.5 1.6 5 2.3 8 2.3s5.5-.7 8-2.3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>`,
  },
  fire: {
    key: "fire",
    name: "火",
    fullName: "火行",
    motto: "炎上光明",
    classic: "火曰炎上",
    direction: "南",
    season: "夏",
    verse: "心火明亮，万事可照",
    color: "#f26a45",
    color2: "#7c1d13",
    vars: {
      "--accent": "#d9503f", "--accent-deep": "#a83227", "--accent-soft": "#fdeeea", "--accent-rgb": "217, 80, 63",
      "--violet": "#f0a03c", "--violet-rgb": "240, 160, 60",
      "--bg": "#fdf5f2", "--bg-deep": "#f8e9e3", "--surface": "#fffdfc", "--surface-soft": "#fff8f5",
      "--border": "#f1ddd6", "--border-strong": "#e6c3b8",
      "--text": "#33201c", "--text-soft": "#56342c", "--muted": "#8d6c62",
      "--page-1": "#fff8f4", "--page-2": "#faeae3",
      "--halo-1-rgb": "217, 80, 63", "--halo-2-rgb": "240, 160, 60",
      "--bar-1": "#35100d", "--bar-2": "#6e2018", "--bar-3": "#b13b26",
      "--bar-line-1": "#ff9a6a", "--bar-line-2": "#ffd08a", "--bar-line-3": "#d9503f",
      "--glow-1-rgb": "255, 160, 110", "--glow-2-rgb": "255, 110, 70",
      "--logo-1": "#f26a45", "--logo-2": "#7c1d13",
      "--wx-texture": texture.fire, "--radius": "12px", "--radius-sm": "8px",
      "--shadow-tint-rgb": "88, 30, 20",
    },
    mark: `<circle cx="24" cy="24" r="16.5" fill="none" stroke="currentColor" stroke-opacity=".28" stroke-width="1"/>
      <path d="M24 9c1.2 5.4 5.7 8.4 7.8 12.6 1.1 2.3 1.6 4.3 1.6 6.2A9.4 9.4 0 0 1 24 37.2a9.4 9.4 0 0 1-9.4-9.4c0-2.7 1-5 2.8-7.1.5 2.2 1.6 3.5 3.2 4.3.2-4.7 1-9.5 3.4-16Z" fill="currentColor"/>
      <path d="M24 25.4c1.7 2.3 3.2 3.7 3.2 5.6a3.5 3.5 0 0 1-7 0c0-1.8 1.8-3.3 3.8-5.6Z" fill="#2b0d08" fill-opacity=".28"/>
      <circle cx="11.5" cy="15" r="1.4" fill="currentColor" fill-opacity=".8"/>
      <circle cx="36.5" cy="14" r="1.1" fill="currentColor" fill-opacity=".65"/>
      <circle cx="37" cy="32" r="1.5" fill="currentColor" fill-opacity=".5"/>`,
    sigil: `<path d="M14 3c.8 4 3.9 6.2 5.3 9.3.8 1.7 1.1 3.1 1.1 4.6a6.9 6.9 0 0 1-13.8 0c0-2 .7-3.7 2-5.2.4 1.6 1.2 2.6 2.4 3.2.1-3.5.7-7 3-11.9Z" fill="currentColor"/>
      <path d="M14 15c1.2 1.7 2.3 2.7 2.3 4.1a2.6 2.6 0 0 1-5.2 0c0-1.3 1.3-2.4 2.9-4.1Z" fill="#2b0d08" fill-opacity=".3"/>`,
  },
  earth: {
    key: "earth",
    name: "土",
    fullName: "土行",
    motto: "稼穑载物",
    classic: "土爰稼穑",
    direction: "中",
    season: "四季",
    verse: "厚土载物，步步生根",
    color: "#d4a746",
    color2: "#5b441a",
    vars: {
      "--accent": "#b8862c", "--accent-deep": "#8a6118", "--accent-soft": "#fbf2df", "--accent-rgb": "184, 134, 44",
      "--violet": "#c98a5b", "--violet-rgb": "201, 138, 91",
      "--bg": "#faf7f0", "--bg-deep": "#f1eadb", "--surface": "#fffdf8", "--surface-soft": "#fbf8f0",
      "--border": "#eae0cb", "--border-strong": "#d8c9a9",
      "--text": "#2f2a1e", "--text-soft": "#504735", "--muted": "#857960",
      "--page-1": "#fcf9f2", "--page-2": "#f3eddf",
      "--halo-1-rgb": "184, 134, 44", "--halo-2-rgb": "201, 138, 91",
      "--bar-1": "#2b2314", "--bar-2": "#55431f", "--bar-3": "#8c6c2c",
      "--bar-line-1": "#e0bb63", "--bar-line-2": "#f2d99a", "--bar-line-3": "#b8862c",
      "--glow-1-rgb": "232, 196, 116", "--glow-2-rgb": "196, 152, 74",
      "--logo-1": "#d4a746", "--logo-2": "#5b441a",
      "--wx-texture": texture.earth, "--radius": "8px", "--radius-sm": "6px",
      "--shadow-tint-rgb": "72, 56, 24",
    },
    mark: `<circle cx="24" cy="24" r="16.5" fill="none" stroke="currentColor" stroke-opacity=".3" stroke-width="1" stroke-dasharray="3 3"/>
      <path d="m9.5 33.5 9-12 5 6.5 5.5-9.5 9.5 15Z" fill="currentColor" fill-opacity=".9"/>
      <path d="M10 34h28" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
      <rect x="20.2" y="11.2" width="7.6" height="7.6" rx="1" transform="rotate(45 24 15)" fill="none" stroke="currentColor" stroke-width="1.7"/>
      <circle cx="24" cy="15" r="1.5" fill="currentColor"/>
      <path d="M14 38h20" stroke="currentColor" stroke-opacity=".55" stroke-width="1.6" stroke-linecap="round"/>`,
    sigil: `<path d="m2 22 6.5-8.5 3.5 4.5 4-6.5L24 22Z" fill="currentColor" fill-opacity=".9"/>
      <path d="M2 23h22" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
      <rect x="10.2" y="2.2" width="5.6" height="5.6" rx=".6" transform="rotate(45 13 5)" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <path d="M5 26h16" stroke="currentColor" stroke-opacity=".55" stroke-width="1.4" stroke-linecap="round"/>`,
  },
  metal: {
    key: "metal",
    name: "金",
    fullName: "金行",
    motto: "从革成器",
    classic: "金曰从革",
    direction: "西",
    season: "秋",
    verse: "精金百炼，条理分明",
    color: "#b8c4d8",
    color2: "#3b465e",
    vars: {
      "--accent": "#71809e", "--accent-deep": "#4b5872", "--accent-soft": "#eef1f7", "--accent-rgb": "113, 128, 158",
      "--violet": "#c2a15a", "--violet-rgb": "194, 161, 90",
      "--bg": "#f7f8fb", "--bg-deep": "#eaedf3", "--surface": "#ffffff", "--surface-soft": "#fafbfd",
      "--border": "#e0e4ec", "--border-strong": "#cbd2de",
      "--text": "#1e2430", "--text-soft": "#3a4356", "--muted": "#6d778c",
      "--page-1": "#fbfcfe", "--page-2": "#eceff5",
      "--halo-1-rgb": "113, 128, 158", "--halo-2-rgb": "194, 161, 90",
      "--bar-1": "#171c28", "--bar-2": "#333c52", "--bar-3": "#5f6c88",
      "--bar-line-1": "#c9d6ea", "--bar-line-2": "#e5c98a", "--bar-line-3": "#71809e",
      "--glow-1-rgb": "190, 205, 230", "--glow-2-rgb": "214, 190, 140",
      "--logo-1": "#b8c4d8", "--logo-2": "#3b465e",
      "--wx-texture": texture.metal, "--radius": "6px", "--radius-sm": "4px",
      "--shadow-tint-rgb": "32, 40, 60",
    },
    mark: `<circle cx="24" cy="24" r="16.5" fill="none" stroke="currentColor" stroke-opacity=".32" stroke-width="1"/>
      <path d="M24 8.5 27.7 20.3 39.5 24 27.7 27.7 24 39.5 20.3 27.7 8.5 24 20.3 20.3Z" fill="currentColor" fill-opacity=".92"/>
      <path d="M24 15.5 32.5 24 24 32.5 15.5 24Z" fill="none" stroke="#101828" stroke-opacity=".28" stroke-width="1.4"/>
      <circle cx="24" cy="24" r="3.1" fill="#fff" fill-opacity=".95"/>
      <circle cx="24" cy="6.5" r="1.1" fill="currentColor" fill-opacity=".7"/>
      <circle cx="41.5" cy="24" r="1.1" fill="currentColor" fill-opacity=".7"/>
      <circle cx="24" cy="41.5" r="1.1" fill="currentColor" fill-opacity=".7"/>
      <circle cx="6.5" cy="24" r="1.1" fill="currentColor" fill-opacity=".7"/>`,
    sigil: `<path d="M14 1.5 17 11 26.5 14 17 17 14 26.5 11 17 1.5 14 11 11Z" fill="currentColor"/>
      <path d="M14 8.5 19.5 14 14 19.5 8.5 14Z" fill="none" stroke="#101828" stroke-opacity=".3" stroke-width="1.2"/>
      <circle cx="14" cy="14" r="2" fill="#fff"/>`,
  },
  water: {
    key: "water",
    name: "水",
    fullName: "水行",
    motto: "润下流通",
    classic: "水曰润下",
    direction: "北",
    season: "冬",
    verse: "上善若水，周流不殆",
    color: "#3f8fd6",
    color2: "#0d2a52",
    vars: {
      "--accent": "#2c62a8", "--accent-deep": "#17457e", "--accent-soft": "#e8f1fb", "--accent-rgb": "44, 98, 168",
      "--violet": "#4fa3c7", "--violet-rgb": "79, 163, 199",
      "--bg": "#f2f6fc", "--bg-deep": "#e5edf8", "--surface": "#fdfdff", "--surface-soft": "#f7fafd",
      "--border": "#dbe5f2", "--border-strong": "#c1d2e8",
      "--text": "#18263a", "--text-soft": "#344a68", "--muted": "#6d809c",
      "--page-1": "#f7faff", "--page-2": "#e8f0fa",
      "--halo-1-rgb": "44, 98, 168", "--halo-2-rgb": "79, 163, 199",
      "--bar-1": "#08122a", "--bar-2": "#122a55", "--bar-3": "#224d8c",
      "--bar-line-1": "#5aa9e6", "--bar-line-2": "#8fd0f0", "--bar-line-3": "#2c62a8",
      "--glow-1-rgb": "120, 180, 245", "--glow-2-rgb": "90, 140, 225",
      "--logo-1": "#3f8fd6", "--logo-2": "#0d2a52",
      "--wx-texture": texture.water, "--radius": "18px", "--radius-sm": "12px",
      "--shadow-tint-rgb": "16, 38, 74",
    },
    mark: `<circle cx="24" cy="24" r="16.5" fill="none" stroke="currentColor" stroke-opacity=".3" stroke-width="1"/>
      <path d="M24 9.5c0 0-6.5 8-6.5 12.3a6.5 6.5 0 0 0 13 0C30.5 17.5 24 9.5 24 9.5Z" fill="currentColor"/>
      <path d="M20.6 22.4c0 2.2 1.5 3.8 3.5 4" fill="none" stroke="#0b1f3c" stroke-opacity=".35" stroke-width="1.5" stroke-linecap="round"/>
      <path d="M10 31c3.3-2.5 6.6-2.5 9.9 0 3.3 2.5 6.6 2.5 9.9 0 3.3-2.5 6.6-2.5 9.9 0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
      <path d="M13 37c2.6-1.9 5.2-1.9 7.8 0 2.6 1.9 5.2 1.9 7.8 0 2.6-1.9 5.2-1.9 7.8 0" fill="none" stroke="currentColor" stroke-opacity=".55" stroke-width="1.7" stroke-linecap="round"/>`,
    sigil: `<path d="M14 2c0 0-6 7.4-6 11.4a6 6 0 0 0 12 0C20 9.4 14 2 14 2Z" fill="currentColor"/>
      <path d="M11 14c0 2 1.4 3.4 3 3.7" fill="none" stroke="#0b1f3c" stroke-opacity=".35" stroke-width="1.3" stroke-linecap="round"/>
      <path d="M2 22c3-2.2 6-2.2 9 0 3 2.2 6 2.2 9 0 3-2.2 6-2.2 6 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
      <path d="M4 26.5c2.4-1.6 4.8-1.6 7.2 0 2.4 1.6 4.8 1.6 7.2 0 2.4-1.6 4.8-1.6 7.2 0" fill="none" stroke="currentColor" stroke-opacity=".55" stroke-width="1.5" stroke-linecap="round"/>`,
  },
};

const elName = (el: WuXing) => WUXING[el].name;
const nextEl = (el: WuXing) => WUXING_ORDER[(WUXING_ORDER.indexOf(el) + 1) % 5];
const prevEl = (el: WuXing) => WUXING_ORDER[(WUXING_ORDER.indexOf(el) + 4) % 5];
/** 克我者：相克序为 相隔两位，(i+3) 为克我 */
const counterEl = (el: WuXing) => WUXING_ORDER[(WUXING_ORDER.indexOf(el) + 3) % 5];

function jiaziIndex(d: Date): number {
  // 1900-01-01 为甲戌（六十甲子序号 10），按 UTC 日历天差推算，避免时区导致跨日抖动
  const days = Math.floor(
    (Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) - Date.UTC(1900, 0, 1)) / 86400000,
  );
  return ((10 + days) % 60 + 60) % 60;
}

function solarMonth(d: Date): { zhi: number; label: string; el: WuXing } {
  const m = d.getMonth() + 1;
  const day = d.getDate();
  let found = SOLAR_MONTHS[SOLAR_MONTHS.length - 1];
  for (const t of SOLAR_MONTHS) {
    if (m > t.m || (m === t.m && day >= t.d)) found = t;
  }
  return { zhi: found.zhi, label: found.label, el: ZHI_EL[found.zhi] };
}

function shichenIndex(d: Date): number {
  return Math.floor(((d.getHours() + 1) % 24) / 2);
}

export function getTodayReading(date = new Date()): WuXingReading {
  const jz = jiaziIndex(date);
  const gan = GAN[jz % 10];
  const zhi = ZHI[jz % 12];
  const dayElement = GAN_EL[jz % 10];
  const branchElement = ZHI_EL[jz % 12];
  const month = solarMonth(date);
  const sc = shichenIndex(date);

  const scores: Record<WuXing, number> = { wood: 0, fire: 0, earth: 0, metal: 0, water: 0 };
  const bump = (el: WuXing, n: number) => { scores[el] += n; };

  bump(month.el, 5);                 // 当令之气
  bump(dayElement, 4);              // 日主
  bump(branchElement, 3);           // 日支
  bump(prevEl(month.el), 2);        // 生我者，气有来处
  bump(nextEl(month.el), 1);        // 我生者，气有去处
  if (dayElement === month.el) bump(month.el, 2); // 日主得令

  // 调候：寒暖燥湿补偏
  switch (month.el) {
    case "water": bump("fire", 3); bump("earth", 1); break;
    case "fire": bump("water", 3); bump("metal", 1); break;
    case "wood": bump("metal", 2); bump("fire", 1); break;
    case "metal": bump("fire", 2); bump("water", 1); break;
    case "earth": bump("wood", 2); bump("water", 1); break;
  }

  const rank = [...WUXING_ORDER].sort((a, b) => {
    if (scores[b] !== scores[a]) return scores[b] - scores[a];
    const tie = (el: WuXing) => (el === month.el ? 2 : el === dayElement ? 1 : 0);
    return tie(b) - tie(a);
  });
  const element = rank[0];
  const winner = WUXING[element];
  const dateKey = `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;

  const source =
    element === month.el ? "与月令同气，得时而旺"
      : element === dayElement ? "日主得势，扶之为用"
        : element === prevEl(month.el) ? "承月令之生，源远流长"
          : "调候补偏，以平为期";
  const reason =
    `${gan}${zhi}日 · ${month.label}令（${elName(month.el)}旺），日主${gan}属${elName(dayElement)}，` +
    `日支${zhi}属${elName(branchElement)}；${elName(element)}气最厚，${source}，故今日以${elName(element)}为用。`;
  const advice = `宜${elName(element)} · 辅${elName(nextEl(element))} · 避${elName(counterEl(element))}`;

  return {
    element,
    dateKey,
    ganzhi: `${gan}${zhi}`,
    gan,
    zhi,
    monthLabel: `${month.label}令`,
    monthElement: month.el,
    dayElement,
    hourLabel: `${ZHI[sc]}时·${SHICHEN_TEXT[sc]}`,
    hourElement: ZHI_EL[sc],
    scores,
    reason,
    advice: `${advice}　｜　${winner.classic}`,
  };
}

const MANUAL_KEY = "opc.wuxing.manual";
const AUTO_EVENT = "opc:wuxing";
const TICK_EVENT = "opc:wuxing:tick";

let active: WuXing | null = null;

export function getManualWuXing(): WuXing | null {
  try {
    const v = localStorage.getItem(MANUAL_KEY);
    return v && WUXING_ORDER.includes(v as WuXing) ? (v as WuXing) : null;
  } catch {
    return null;
  }
}

function resolve(): WuXing {
  return getManualWuXing() ?? getTodayReading().element;
}

export function getActiveWuXing(): WuXing {
  return active ?? resolve();
}

export function faviconDataUri(el: WuXing): string {
  const t = WUXING[el];
  const art = t.mark.replace(/currentColor/g, "#ffffff");
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">` +
    `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">` +
    `<stop offset="0" stop-color="${t.color}"/><stop offset="1" stop-color="${t.color2}"/>` +
    `</linearGradient></defs>` +
    `<circle cx="24" cy="24" r="23" fill="url(#g)"/>` +
    `<circle cx="24" cy="24" r="20" fill="none" stroke="#ffffff" stroke-opacity=".35" stroke-width="1"/>` +
    art + `</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

export function applyWuXing(el: WuXing): void {
  const root = document.documentElement;
  root.dataset.wuxing = el;
  root.dataset.wuxingName = WUXING[el].name;
  const vars = WUXING[el].vars;
  for (const key of Object.keys(vars)) root.style.setProperty(key, vars[key]);

  const icon = document.querySelector<HTMLLinkElement>('link[rel~="icon"]');
  if (icon) icon.href = faviconDataUri(el);
  let meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.appendChild(meta);
  }
  meta.content = vars["--bar-1"];
  active = el;
  window.dispatchEvent(new CustomEvent(AUTO_EVENT, { detail: el }));
}

export function setManualWuXing(el: WuXing | null): void {
  try {
    if (el) localStorage.setItem(MANUAL_KEY, el);
    else localStorage.removeItem(MANUAL_KEY);
  } catch {
    /* 隐私模式下忽略 */
  }
  applyWuXing(resolve());
}

export function initWuxing(): void {
  applyWuXing(resolve());
  // 跨天时自动换令；同一天内仅广播 tick，供面板刷新「此刻时气」
  window.setInterval(() => {
    const want = resolve();
    if (want !== active) applyWuXing(want);
    else window.dispatchEvent(new CustomEvent(TICK_EVENT));
  }, 60_000);
}

export interface WuXingState {
  element: WuXing;
  manual: WuXing | null;
  reading: WuXingReading;
}

export const WUXING_EVENTS = { change: AUTO_EVENT, tick: TICK_EVENT } as const;

/** 供 React 订阅主题变化（手动切换 / 跨天换令 / 多标签页同步） */
export function useWuxing(): WuXingState {
  const [state, setState] = useState<WuXingState>(() => ({
    element: getActiveWuXing(),
    manual: getManualWuXing(),
    reading: getTodayReading(),
  }));

  useEffect(() => {
    const refresh = () => setState({
      element: getActiveWuXing(),
      manual: getManualWuXing(),
      reading: getTodayReading(),
    });
    window.addEventListener(WUXING_EVENTS.change, refresh);
    window.addEventListener(WUXING_EVENTS.tick, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(WUXING_EVENTS.change, refresh);
      window.removeEventListener(WUXING_EVENTS.tick, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return state;
}
