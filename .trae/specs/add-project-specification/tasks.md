# Tasks

> 目标：把 `spec.md` 的 13 个能力域逐条与代码核对、补全出处，产出可直接评审的《全量工程规格说明书》，**不改任何运行代码**。
> 说明：Task 1~8 彼此独立，可并行；Task 9 依赖 1~8；Task 10 依赖 9。

- [x] Task 1: 核实 C1 进程运维与 C2 配置数据源两章
  - [x] SubTask 1.1: 逐行核对 `opc.sh` 子命令、默认主机/端口、PID/日志轮转、探测逻辑与规格一致
  - [x] SubTask 1.2: 核对 `prototype/server.py` 中 `OPC_VAULT`、配置解析、回退默认、交付合同解析的行号
  - [x] SubTask 1.3: 修正任何与代码不符的表述（含行号漂移），补「已核实/推断」标注

- [x] Task 2: 核实 C3 组织模型与 C4 事件总线两章
  - [x] SubTask 2.1: 核对员工对象字段、`/api/org/*` 读写路由与老板权限判定
  - [x] SubTask 2.2: 用静态提取复核事件 `kind` 全集与 SSE 首帧/心跳行为
  - [x] SubTask 2.3: 核对消息对象字段、payload 是否截断、消息列表 4000→2000 裁剪边界

- [x] Task 3: 核实 C5 流水线协作与 C6 审批门两章
  - [x] SubTask 3.1: 核对阶段机、选题会/巡检/串味/派单/指令五类协作语义与代码分支
  - [x] SubTask 3.2: 核对双审批门的状态机、超时后果、`blockedBy` 取值、拒绝发布计数
  - [x] SubTask 3.3: 核对成本护栏三个阈值与标定注释，标注「来源为代码注释」

- [x] Task 4: 核实 C7 交付物、作品档案与样片渲染
  - [x] SubTask 4.1: 核对交付物文件全集（实测 11 个）与 `deliverables.py` / `om_render.py` / `server.py` 的实际写入点
  - [x] SubTask 4.2: 核对 `/api/works` 契约与 `works.py` 分页/详情参数
  - [x] SubTask 4.3: 核对 `om_render.py` 的 TTS/Remotion/验收门链路与降级条件

- [x] Task 5: 核实 C8 媒体引擎与 Hermes 插件接线
  - [x] SubTask 5.1: 逐条核对 media-engine 全部路由、入参、出参、鉴权与 TTL/体积限制
  - [x] SubTask 5.2: 核对 TTS/ASR 模型选择、worker 默认值、run.sh 覆盖项
  - [x] SubTask 5.3: 核对 `hermes-plugins/media-platform` 的 URL 与四类超时默认值

- [x] Task 6: 核实 C9 三套前端
  - [x] SubTask 6.1: 核对 Refine 路由/资源/dataProvider 契约与实际 `web/src` 代码
  - [x] SubTask 6.2: 核对 `/app` 静态托管、长缓存与 SPA 回退行为
  - [x] SubTask 6.3: 核对旧版控制台与 3D 页面依赖的接口清单

- [x] Task 7: 核实 C10 Android 客户端
  - [x] SubTask 7.1: 核对包名、SDK、版本号、构建产物位置（以 `build.gradle` 为准）
  - [x] SubTask 7.2: 核对 MCP 协议版本、入口 URL、素材路径、调度 WS 通道
  - [x] SubTask 7.3: 核对功能面清单与 Manifest/strings 的对应关系；未真机验证处标注「未验证」

- [x] Task 8: 核实 C11 归档导出、C12 安全、C13 构建同步
  - [x] SubTask 8.1: 核对知识/热点字段与分页详情接口
  - [x] SubTask 8.2: 核对 `vault_export.py` 输出的 8 个文件路径与数据来源
  - [x] SubTask 8.3: 核对双层鉴权、登录锁定、公开白名单、路径穿越校验
  - [x] SubTask 8.4: 核对 `.gitignore` 排除项、`sync-github.sh` 行为、`verify.py` 用例清单

- [x] Task 9: 汇总并分级「已知不一致」清单（D1~D11）
  - [x] SubTask 9.1: 为每条不一致补齐可复现证据（文件:行号或命令）
  - [x] SubTask 9.2: 按影响分级（阻断启动 / 文档滞后 / 运维预期偏差），标注证据强度
  - [x] SubTask 9.3: 明确「本变更不修复」，给出后续独立变更建议标题
  - [x] SubTask 9.4: 模块规格阶段补充 D11（媒体引擎地址三处口径不一致）

- [x] Task 10: 建立并全稿交叉校验追溯表
  - [x] SubTask 10.1: 逐能力域填写「规格条目 → 代码位置 → 验收手段」
  - [x] SubTask 10.2: 全稿扫描所有数字与断言，确认均有出处或明确标注推断/未验证
  - [x] SubTask 10.3: 执行 `git status` 确认变更范围仅文档，无代码改动

- [x] Task 11: 为 5 个能力模块补齐独立规格文档
  - [x] SubTask 11.1: 勘察 `prototype/`（server/交付物/渲染/作品档案）并核实行数
  - [x] SubTask 11.2: 勘察 `media-engine/`（server/tts/asr/video）路由、鉴权、TTL、指标、环境变量
  - [x] SubTask 11.3: 勘察 `web/`（Refine 路由、dataProvider/authProvider、页面直连接口、构建托管）
  - [x] SubTask 11.4: 勘察 `android/`（52 个 Java 文件、构建产物、MCP/dispatch/WS 契约、权限、离线降级）
  - [x] SubTask 11.5: 勘察 `hermes-plugins/media-platform`（注册契约、8 工具映射、错误语义）
  - [x] SubTask 11.6: 在 `spec.md` 增加「17. 模块规格索引」，并在各模块文档补齐验收手段与已知限制

- [x] Task 12: 按 2026-11-12 线上 `/app` 新版本校对并补齐 web 部分（**不改运行代码**）
  - [x] SubTask 12.1: 抓取 `http://192.168.31.206:8848/app` 的 `index.html` 与带哈希 bundle，核对 `web/dist` 与 `web/src` 一致、无「线上有而源码没有」的页面
  - [x] SubTask 12.2: 登录后实测 `/api/state`、`/api/hotspots`、`/api/knowledge`、`/api/works`、`/api/org`、`/api/chat/*` 的响应键与总数，并只读查 `prototype/opc.db` 表行数
  - [x] SubTask 12.3: 重写 `modules/web.md`：补齐消息中心 / 3D 场景两页、9 条显式路由、DB 数据契约（chat 四接口、SSE `chat.message`、trace 补齐、snake_case 字段、FTS 与游标）、老板权限、降级与验收命令
  - [x] SubTask 12.4: 同步 `spec.md` 的拓扑、C2 数据源、C4 事件枚举、C9 前端、C12 路由表、D5/D12/D13、追溯表与模块索引
  - [x] SubTask 12.5: 同步 `checklist.md` 的规模数字与新增验收项；登记「数据访问已改成 db」的真实边界为待办 P1，而非宣称全站已切换
  - [x] SubTask 12.6: 同步 `README.md` 的 `/app` 页面清单、配置来源与接口表，以及 `PROCESS-DATA-DB-PLAN.md` 的头部状态与 §13 遗留清单（消息中心已完成）

# Task Dependencies

- Task 9 依赖 Task 1~8
- Task 10 依赖 Task 9
- Task 11 依赖 Task 1~10（复用已核实的系统级事实，避免重复取证）
- Task 12 依赖 Task 11 的 `modules/web.md`；仅改规格文档，不改运行代码
