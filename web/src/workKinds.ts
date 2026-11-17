// 成品仓库 · 要素成品分类定义
//
// 成品仓库 = 加工厂所有要素成品的统一入库、检验、归档中心。
// 与加工厂（文章/声音/图片/视频/其它原料）一一对应，按成品形态分库管理。
//
// kind 作为 ?kind= 透传给 /api/works，后端基于每件作品的 files/hasFinal 推导，
// 分类有真实数据支撑（一件作品可同时属于多个分类，如既有视频也有封面图）。

export type WorkKind = "all" | "video" | "article" | "image" | "audio" | "material";

export interface KindDef {
  key: WorkKind;
  /** 二级导航名 */
  label: string;
  /** 一句话定位 */
  subtitle: string;
  /** 该分类收录什么 */
  desc: string;
  /** 入库口径（对应后端推导） */
  rule: string;
  /** 空库提示 */
  empty: string;
}

export const WORK_KINDS: KindDef[] = [
  {
    key: "all",
    label: "全部成品",
    subtitle: "所有要素成品的统一仓库",
    desc:
      "收录加工厂各要素车间出厂的全部成品：视频成片、文案文稿、封面图、配音音频与素材原料。" +
      "每件成品带项目编号、流水号、车间工艺与评审结论，状态与加工厂生命周期一致。",
    rule: "经 CCO 审批进厂、有流水号的作品（与加工厂过程库同源）",
    empty: "仓库暂无成品，内容单经 CCO 审批进厂后，完成加工即入库。",
  },
  {
    key: "video",
    label: "视频成片",
    subtitle: "最终成片 / 预览样片 / 简化小样",
    desc:
      "视频车间出厂的动态成品：05-final.mp4 最终成片、05-preview.mp4 预览样片与 05-sample-reel.mp4 简化小样。" +
      "支持在仓库内直接播放、查看质检与终审结论。",
    rule: "存在 05-final / 05-preview / 05-sample-reel 任一视频文件",
    empty: "暂无视频成品，成片经质检出厂后自动入库。",
  },
  {
    key: "article",
    label: "文案文稿",
    subtitle: "选题 / 文案 / 分镜 / 评估 / 评审",
    desc:
      "文案车间出厂的文字成品：选题文稿、文案成稿、分镜剧本、制作评估与评审记录。" +
      "可在详情页阅读 Markdown 原文，追溯每一步加工结论。",
    rule: "存在任一 .md 文稿（选题/文案/分镜/评估/评审）",
    empty: "暂无文稿成品，文案车间成稿后自动入库。",
  },
  {
    key: "image",
    label: "封面图片",
    subtitle: "封面 / 关键帧 / 插图",
    desc:
      "图片车间出厂的静态视觉成品：04-cover.png 封面图，以及交付目录中的插图与关键帧。" +
      "缩略图视图可直接预览封面。",
    rule: "存在 04-cover.png 封面图",
    empty: "暂无图片成品，封面与插图出厂后自动入库。",
  },
  {
    key: "audio",
    label: "声音成品",
    subtitle: "配音 / 旁白 / TTS 音频",
    desc:
      "声音车间出厂的听觉成品：交付目录 _render/ 下的 TTS 旁白与配音 wav。" +
      "每件作品取一段代表音频归档，完整音轨随成片一起入库。",
    rule: "交付目录 _render/ 下存在 narr*.wav 配音音频",
    empty: "暂无声音成品，配音合成后自动入库。",
  },
  {
    key: "material",
    label: "素材原料",
    subtitle: "插图配置 / 复用素材 / 原料件",
    desc:
      "原料车间预处理的成品：10-illustrations.json 插图配置，以及 assets/ 下的可复用素材件。" +
      "供各要素车间再次上料复用。",
    rule: "存在插图配置 10-illustrations.json 或 assets/ 下有素材文件",
    empty: "暂无素材成品，原料入库后自动归档。",
  },
];

export const KIND_BY_KEY: Record<WorkKind, KindDef> = Object.fromEntries(
  WORK_KINDS.map((k) => [k.key, k]),
) as Record<WorkKind, KindDef>;
