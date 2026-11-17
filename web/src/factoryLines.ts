// 加工厂 · 要素生产线统一定义
//
// 加工厂 = 所有要素统一处理的车间总和。每条生产线负责一类原料要素，
// 从进厂到出厂走各自的标准化工序；二级导航按生产线切换，统一管理。
//
// 本文件是「分类 + 标准化流程」的唯一事实源（前端）。
// 真实加工能力来自后端 /api/factory/channels（渠道注册表）与
// /api/toolbox/jobs（手工加工任务），不在这里重复编造模型/端点。

export type LineKey = "all" | "text" | "audio" | "image" | "video" | "material";

export interface LineStep {
  /** 工序名 */
  name: string;
  /** 工序要点：进厂条件 → 处理动作 → 出厂形态 */
  desc: string;
}

export interface FactoryLine {
  key: LineKey;
  /** 二级导航名 */
  label: string;
  /** 车间简称（卡片徽章） */
  short: string;
  /** 一句话定位 */
  subtitle: string;
  /** 车间定义：处理什么要素、边界在哪 */
  desc: string;
  /** 关联工具箱作业类型（image/audio/video），用于拉取真实任务 */
  jobKind?: "image" | "audio" | "video";
  /** 关联渠道 kind，用于拉取真实可用加工渠道 */
  channelKind?: "image" | "audio" | "video";
  /** 标准化工序序列（进厂 → 出厂） */
  steps: LineStep[];
  /** 标准出厂物 */
  outputs: string[];
  /** 补充说明（边界 / 兜底 / 注意事项） */
  note?: string;
  /** 关联页面入口（可选）：{ label, href } */
  entry?: { label: string; href: string };
}

export const FACTORY_LINES: FactoryLine[] = [
  {
    key: "all",
    label: "全厂总览",
    short: "总厂",
    subtitle: "所有要素统一处理的车间总和",
    desc:
      "加工厂统一接收文章、声音、图片、视频及其它原料，按要素类型分车间加工、按标准化工序出厂。" +
      "本页汇总全厂在制流水线与历史产品；切换到各要素车间可查看该类的标准工序、可用渠道与最近加工任务。",
    steps: [
      { name: "进厂", desc: "内容单经 CCO 审批通过、发放流水号（WO-YYYYMMDD-NNN）后进入对应要素车间" },
      { name: "加工", desc: "按该要素车间的标准工序逐站处理，工位状态实时回写过程库" },
      { name: "质检", desc: "样片/成品经 CCO 与审核员双门评审，不过则返工重加工" },
      { name: "出厂", desc: "流水结束（出厂/完工/受阻终止）转入历史产品，交付物归档到成品仓库" },
    ],
    outputs: ["在制流水线", "历史产品", "13 类工艺车间", "交付物与成品仓库"],
    note: "进厂前的选题调度与出厂后的发布/归档由 hermes 编排，不属于车间工序。",
  },
  {
    key: "text",
    label: "文案车间",
    short: "文章",
    subtitle: "文章 / 脚本 / 分镜 —— 文字要素的加工线",
    desc:
      "处理选题、评估报告、文案成稿、分镜脚本等一切文字要素。CCO 从知识库内化提炼选题，PM 细化成稿，" +
      "经 CCO 审批后作为声音、图片、视频车间的上料源。",
    steps: [
      { name: "选题研究", desc: "CCO 从知识库归档认知中提炼选题，每个议题唯一归属一个项目组，只出一个最终产品" },
      { name: "制作评估", desc: "PM 细化逐镜（画面/台词/时长/素材）并拆分派单，CCO 确定出片模式" },
      { name: "文案成稿", desc: "PM 在 CCO 初稿上细化 script.json，落 checkpoint_script，CCO 审批后放行制片" },
      { name: "分镜方案", desc: "逐镜剧本 / 封面 / 配音 / TTS 方案（scene_plan.json），CCO 审批后进入下料" },
    ],
    outputs: ["评估报告", "script.json 文案成稿", "scene_plan.json 分镜方案", "逐镜表（画面/台词/秒）"],
    note: "文案车间是上游：其产出直接作为声音、图片、视频车间的工单输入。",
  },
  {
    key: "audio",
    label: "声音车间",
    short: "声音",
    subtitle: "台词 / TTS / 配音 / 音色 / BGM —— 听觉要素的加工线",
    desc:
      "把文案车间产出的台词加工为可交付音频：台词提取、音色选定、TTS 合成、克隆音色微调、BGM 配乐与混音。" +
      "渠道注册表中 kind=audio 的 Qwen3-TTS、CosyVoice、阿里云百炼（云服务）等均归本车间统一管理。",
    jobKind: "audio",
    channelKind: "audio",
    steps: [
      { name: "台词定稿", desc: "从分镜/脚本提取逐镜台词，确认语言、情绪与语速" },
      { name: "音色选定", desc: "从内置音色或克隆音色中选定配音音色（音色管理复用工具箱音色表）" },
      { name: "TTS 合成", desc: "按台词 + 音色合成对白/旁白 wav，逐镜落 assets/audio" },
      { name: "BGM 配乐", desc: "从素材库选取 BGM，按分镜时间码对齐并控制音量" },
      { name: "混音交付", desc: "对白/旁白与 BGM 混音，交付给小样与成片车间复用" },
    ],
    outputs: ["逐镜对白 wav", "旁白 wav", "克隆音色", "BGM 混音轨"],
    note: "TTS/ASR 后端为 9311/9312，MCP 445/9101/9102 对外；音色克隆写入即生效，无需重启引擎。" +
      "阿里云百炼 Qwen-Audio-3.0-TTS-Plus 走云服务，按字符计费，不占本机算力。",
    entry: { label: "音色与试听", href: "/app/toolbox" },
  },
  {
    key: "image",
    label: "图片车间",
    short: "图片",
    subtitle: "文生图 / 图生图 / 插图 / 封面 / 关键帧 —— 视觉静态要素的加工线",
    desc:
      "把逐镜画面描述加工为图像：文生图与图生图（ComfyUI 157 的 Qwen-Image、阿里云百炼 qwen-image-3.0-pro）、" +
      "ComfyUI SD2.1、media-gen SD1.5 写实、插图规格化、封面设计与分镜关键帧。" +
      "渠道注册表中 kind=image 的渠道归本车间统一管理，每个渠道的规格红线与画质档位在设置框里可见。",
    jobKind: "image",
    channelKind: "image",
    steps: [
      { name: "提示词上料", desc: "取分镜「画面」为提示词，一行一条；图生图时每行是对参考图的编辑指令" },
      { name: "生图模式", desc: "选文生图或图生图；图生图需上传参考图，只有支持的渠道可切换" },
      { name: "规格裁剪", desc: "一键画质档位填入安全参数，再按渠道红线微调尺寸/步数/CFG" },
      { name: "关键帧/插图", desc: "核对渠道规格红线与当前参数，避免越界导致画质塌方" },
      { name: "素材登记", desc: "确认后批量提交，成品落交付目录并参与五行五维评分" },
    ],
    outputs: ["分镜关键帧", "封面图", "插图", "品牌 KV / 形象图"],
    note: "ComfyUI（含 192.168.21.157 的 Qwen-Image）为远程纯 CPU 推理，单张约 1–3 分钟；" +
      "百炼图像是云服务按量计费，图片 URL 及时下载。排队数在渠道卡片上可见。",
    entry: { label: "手工出图", href: "/app/toolbox" },
  },
  {
    key: "video",
    label: "视频车间",
    short: "视频",
    subtitle: "文生视频 / 图生视频 / 剪辑 / 成片 —— 动态要素的加工线",
    desc:
      "把关键帧、对白与字幕加工为视频：文生视频 / 首帧图生视频（Wan2.2 TI2V / WAN2.1）、多镜剪辑合成、终版成片与质检出厂。" +
      "13 类 OpenMontage 工艺（动画讲解、数字人口播、电影感大片等）在本车间按标准工序落地。",
    jobKind: "video",
    channelKind: "video",
    steps: [
      { name: "分镜上料", desc: "逐镜确认提示词、台词、时长与渠道，支持一镜多渠道并发试产" },
      { name: "视频生成", desc: "文生视频 / 图生视频（Wan2.1 TI2V 5B / WAN2.1 文生视频），按首帧驱动" },
      { name: "简化小样", desc: "关键帧 + 对白 + BGM 按时间码对齐，产出 720×1280 小样（失败只降级不阻断）" },
      { name: "剪辑合成", desc: "OpenMontage / Remotion 多镜拼接，旁白 + 画面按工艺模板合成" },
      { name: "终版成片", desc: "锁定 05-final.mp4，按交付合同复检（分辨率/编码/时长/音轨）" },
      { name: "质检出厂", desc: "CCO + 审核员双门评审，通过后出厂并转成品仓库" },
    ],
    outputs: ["05-sample-reel.mp4 小样", "05-final.mp4 成片", "封面", "质检报告"],
    note: "成片仍以 OpenMontage 工艺为准；视频渠道为 CPU 推理，时长 3s/480p 约 10–30 分钟。",
    entry: { label: "手工出片", href: "/app/toolbox" },
  },
  {
    key: "material",
    label: "原料车间",
    short: "其它",
    subtitle: "热点 / 素材 / 文档 / 形象 —— 其它原料的采集与预处理线",
    desc:
      "处理不属于内容流水线主链路的其它原料：热点采集、外部素材采集、文档转文本、数字人口播形象等。" +
      "统一做采集 → 归一 → 登记 → 评分 → 归档，供各要素车间复用。",
    steps: [
      { name: "采集", desc: "热点采集 / 外部素材采集 / 文档（pdf/doc/xls/ppt）转文本" },
      { name: "归一", desc: "结构化清洗，按画面/台词/秒等统一字段归一，剔除无效来源" },
      { name: "登记", desc: "素材库与知识库双写登记，标注来源与标签，建立可检索索引" },
      { name: "评分", desc: "五行五维评分（引用/质量/新鲜/检索/底座），分优质/可用/待观察/候选" },
      { name: "归档", desc: "保护类保底保留；淘汰候选冷静期 30 天，满期人工审批归档，脚本绝不删文件" },
    ],
    outputs: ["素材库条目", "知识库条目", "热点线索", "评分报告"],
    note: "本车间做预处理与入库，不直接产出成片；产物可在素材页检索复用。",
    entry: { label: "去素材页", href: "/app/materials" },
  },
];

export const LINE_BY_KEY: Record<LineKey, FactoryLine> = Object.fromEntries(
  FACTORY_LINES.map((l) => [l.key, l]),
) as Record<LineKey, FactoryLine>;
