// 自动生成，勿手改。数据源: ep2-timeline.json（206 tts MCP 句级时间轴）
export const FPS = 25;
export const WIDTH = 1080;
export const HEIGHT = 1920;
export const DURATION_SECONDS = 58.032;
export const TOTAL_FRAMES = Math.round(DURATION_SECONDS * FPS);

export type Seg = { index: number; chapter: number; ill: string; text: string; start: number; end: number };
export type Chapter = { index: number; id: string; title: string; ill: string; start: number; end: number; segmentIndices: number[] };

export const SEGMENTS: Seg[] = [
  {
    "index": 0,
    "chapter": 0,
    "ill": "spark",
    "text": "当模型替你记住一切，什么还算是你的？",
    "start": 1.2,
    "end": 4.662
  },
  {
    "index": 1,
    "chapter": 0,
    "ill": "spark",
    "text": "人工智能让表达变得便宜，你的记录反而变得稀缺。",
    "start": 4.982,
    "end": 9.495
  },
  {
    "index": 2,
    "chapter": 1,
    "ill": "certificate",
    "text": "二零二六年七月，国家数据局发布数据产权登记工作指引。",
    "start": 10.195,
    "end": 15.137
  },
  {
    "index": 3,
    "chapter": 1,
    "ill": "certificate",
    "text": "持有权、使用权、经营权，三权分置。",
    "start": 15.457,
    "end": 18.953
  },
  {
    "index": 4,
    "chapter": 2,
    "ill": "three_rings",
    "text": "指引写得很清楚，自然人也可以申请登记。",
    "start": 19.653,
    "end": 23.195
  },
  {
    "index": 5,
    "chapter": 2,
    "ill": "three_rings",
    "text": "你合法采集、生成、获取的数据，有了可登记的凭证。",
    "start": 23.515,
    "end": 27.131
  },
  {
    "index": 6,
    "chapter": 3,
    "ill": "lattice",
    "text": "九月，国家数据产权登记服务系统上线试运行。",
    "start": 27.831,
    "end": 31.986
  },
  {
    "index": 7,
    "chapter": 3,
    "ill": "lattice",
    "text": "十月，国家数据集团正式挂牌，模型越大，越吃数据。",
    "start": 32.306,
    "end": 37.209
  },
  {
    "index": 8,
    "chapter": 4,
    "ill": "four_nodes",
    "text": "落到个人，其实是四件很朴素的事。",
    "start": 37.909,
    "end": 40.987
  },
  {
    "index": 9,
    "chapter": 4,
    "ill": "four_nodes",
    "text": "先留痕，再确权，然后守界，最后复用。",
    "start": 41.307,
    "end": 45.035
  },
  {
    "index": 10,
    "chapter": 4,
    "ill": "four_nodes",
    "text": "把产出整理好，申请登记，授权限定场景和时限，再让数据合规地复用。",
    "start": 45.355,
    "end": 51.808
  },
  {
    "index": 11,
    "chapter": 5,
    "ill": "collapse",
    "text": "模型学得会你的知识，学不走你的署名。",
    "start": 52.508,
    "end": 55.532
  }
];
export const CHAPTERS: Chapter[] = [
  {
    "index": 0,
    "id": "ch1",
    "title": "钩子",
    "ill": "spark",
    "start": 1.2,
    "end": 9.495,
    "segmentIndices": [
      0,
      1
    ]
  },
  {
    "index": 1,
    "id": "ch2",
    "title": "事实起点",
    "ill": "certificate",
    "start": 10.195,
    "end": 18.953,
    "segmentIndices": [
      2,
      3
    ]
  },
  {
    "index": 2,
    "id": "ch3",
    "title": "关键落点",
    "ill": "three_rings",
    "start": 19.653,
    "end": 27.131,
    "segmentIndices": [
      4,
      5
    ]
  },
  {
    "index": 3,
    "id": "ch4",
    "title": "基础设施",
    "ill": "lattice",
    "start": 27.831,
    "end": 37.209,
    "segmentIndices": [
      6,
      7
    ]
  },
  {
    "index": 4,
    "id": "ch5",
    "title": "个人四步",
    "ill": "four_nodes",
    "start": 37.909,
    "end": 51.808,
    "segmentIndices": [
      8,
      9,
      10
    ]
  },
  {
    "index": 5,
    "id": "ch6",
    "title": "收束",
    "ill": "collapse",
    "start": 52.508,
    "end": 55.532,
    "segmentIndices": [
      11
    ]
  }
];
