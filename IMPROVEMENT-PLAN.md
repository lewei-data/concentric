# OPC 工作台页面改进计划（结合开源项目 Refine）

> 生成日期：2026-11-07 · 数据基准：本机仓库实测 + GitHub API（当日）
> 适用对象：`/home/boray/opc-workbench` 全部页面（首页仪表盘 / 3D 办公区 / 会议 3D / 登录）

## 1. 现状盘点（实测）

| 文件 | 行数 | 作用 |
|---|---|---|
| [prototype/index.html](prototype/index.html) | 1,110 | 首页仪表盘：流水线、交付成果、素材库、热点与知识库、标签页 |
| [prototype/office3d.html](prototype/office3d.html) | 2,139 | 3D 办公区（three.js 本地 `vendor/three.module.js`，自研场景） |
| [prototype/meeting3d.html](prototype/meeting3d.html) | 1,412 | 3D 会议室 |
| [prototype/login.html](prototype/login.html) | 167 | 登录页 |
| [prototype/server.py](prototype/server.py) | 2,918 | Python 标准库 HTTP 服务 + 业务逻辑（19 个 `/api/*` 路径） |
| [prototype/deliverables.py](prototype/deliverables.py) | 856 | 交付成果 / 热点 / 知识库的读取与解析 |
| 合计 | ~10,010 | 无构建、无测试、单进程 `ThreadingHTTPServer` |

### 页面层已确认的 5 个缺陷

1. **标题硬截断**：知识库归档时 `title[:80]`，前端把 `projectId` 直接拼在标题后，表现为 `…（+2,548★，JavaSc tongxinyuan`（[server.py#L2362](prototype/server.py#L2362)）。
2. **同热点重复**：去重键是 `_hotkey(title)`（按标题），同一热点被三个项目各归档一次，列表出现 3 条近似重复（[server.py#L1138-L1148](prototype/server.py#L1138-L1148)）。
3. **列表点不开详情**：热点/知识库在首页只是纯文本 `<div>`，没有详情路由与详情接口（[index.html#L802-L804](prototype/index.html#L802-L804)）。
4. **无分页/无服务端搜索**：知识库 48 条一次全量下发；仅素材库有前端过滤（[index.html#L1040-L1058](prototype/index.html#L1040-L1058)）。
5. **单体前端**：1,110 行内联 HTML+CSS+JS，无组件化、无路由、无状态管理，新增一个卡片要在三处改（结构/样式/脚本）。

## 2. 开源选型对比

指标均取自 GitHub API，抓取日 2026-11-07。

| 项目 | ★ | 许可 | 定位 | 与本项目的适配度 |
|---|---|---|---|---|
| [refinedev/refine](https://github.com/refinedev/refine) | 36,011 | MIT | 内部工具 / 管理后台框架（React+TS） | **高**：列表/详情/创建/编辑、过滤、分页、权限、REST data provider 全部内建 |
| [appsmithorg/appsmith](https://github.com/appsmithorg/appsmith) | 41,187 | Apache-2.0 | 低代码内部工具平台 | 中：搭建快，但 UI 锁在平台内，难版本化，SSE/自定义逻辑受限 |
| [tabler/tabler](https://github.com/tabler/tabler) | 42,018 | MIT | HTML 管理后台 UI 套件 | 中：可原地换皮，但只解决样式，不解决详情/分页/数据层 |
| [workadventure/workadventure](https://github.com/workadventure/workadventure) | 5,916 | 未识别 SPDX（需人工核对 LICENSE） | 开源虚拟办公室（Tiled 地图 + WebRTC） | 低-中：只覆盖 3D 办公区，需 Docker/WebRTC 一整套基础设施 |
| [n8n-io/n8n](https://github.com/n8n-io/n8n) | 209,735 | 公平代码许可（Sustainable Use，非 OSI） | 工作流自动化 | 低：适合采集调度，不解决页面问题 |
| [apache/superset](https://github.com/apache/superset) | 75,444 | Apache-2.0 | BI 分析 | 低：只做图表，接不住业务流 |
| [grafana/grafana](https://github.com/grafana/grafana) | 77,736 | AGPL-3.0 | 监控可视化 | 低：定位不符 |
| [mrdoob/three.js](https://github.com/mrdoob/three.js) | 117,753 | MIT | 3D 渲染库 | —— 项目已在用，不作为改造主体 |

## 3. 结论：选 Refine

理由（按权重）：

1. **问题对症**：本项目缺的正是「列表→详情→编辑」的页面骨架、分页与服务端过滤，Refine 原生提供 data provider / auth provider / 路由 / 资源 CRUD，[官方文档](https://refine.dev/docs/) 明确面向 internal tools。
2. **MIT，商用无忧**，无 AGPL/公平代码约束。
3. **不侵入后端**：`@refinedev/simple-rest` 只要 REST 契约（`GET /api/x?page&size&q`、`GET /api/x/:id`），现有 Python 业务逻辑可原样保留。
4. **改造边界清晰**：Refine 接管 2D 仪表盘；`office3d.html`/`meeting3d.html` 的 three.js 场景继续独立运行，用链接/iframe 接入，避免高风险重写。
5. **可持续**：组件化 + 类型 + 可测试，替代 1,110 行内联脚本。

不选 Appsmith（平台锁定、版本化差）、不选 Tabler（只换皮）、不选 WorkAdventure（只解决 3D，且许可需核对）、不选 n8n/Superset/Grafana（定位不符）。

## 4. 目标架构

```
┌──────────────────────────────┐
│ web/  Refine (React+TS+Vite) │   ← 新建，替换 index.html 的 2D 部分
│  /            首页仪表盘      │
│  /hotspots    热点列表+详情   │
│  /knowledge   知识库列表+详情 │
│  /materials   素材库          │
│  /deliverables 交付成果       │
└──────────────┬───────────────┘
               │ REST（cookie 会话复用）
┌──────────────┴───────────────┐        ┌─────────────────────┐
│ server.py 业务与数据层（保留）│        │ office3d / meeting3d│
│  + 分页/搜索/详情/去重修复    │        │ three.js（不动）    │
└──────────────────────────────┘        └─────────────────────┘
```

- 构建产物 `web/dist` 由现有 `server.py` 静态托管，`/legacy` 保留旧 `index.html` 作回滚入口。
- 登录沿用 `/api/login`，Refine `authProvider` 包一层；会话 cookie 同源，无需改鉴权模型。

## 5. 分阶段计划（按里程碑，不估工期）

### P0 · 先修数据层（不改 UI，可独立上线）

- 归档不再截断：存完整标题，另存 `summary`（前 80 字）供列表用。
- 去重改指纹：`source + url + date`，同一热点跨项目合并为 1 条，附 `refCount`。
- 新增详情接口：`GET /api/hotspots/:id`、`GET /api/knowledge/:id`。
- 列表接口加 `page/size/q`，返回 `{items,total,page}`。
- 验收：首页旧 UI 下，标题完整、重复消失、48 条可分页。

### P1 · Refine 骨架 + 三个列表/详情页

- 初始化 `web/`，实现 `dataProvider`（simple-rest 适配上述契约）与 `authProvider`。
- 资源：`hotspots`、`knowledge`、`materials`（列表 + 详情抽屉/页面 + 搜索 + 分页 + 分类过滤）。
- 验收：三条主链路可点开详情；页面无内联脚本；移动端可用。

### P2 · 交付成果 / 审批 / 会议迁移

- `deliverables` 资源与状态徽标；审批流沿用现有接口。
- 实时刷新：优先 Refine live provider；若成本高，退化为 10s 轮询。
- 验收：老板审批链路（提交→审批→结果）在 Refine 页面闭环。

### P3 · 首页整合与 3D 接入

- 首页改为 Refine 自定义 dashboard 页，复刻现有卡片信息层级（流水线/交付/素材/热点/知识库）。
- 3D 办公区以独立路由 iframe 嵌入，登录态同源继承。
- 验收：首页信息不丢失；3D 首屏可加载；旧页面仍可 `/legacy` 访问。

### P4 ·（可选评估）WorkAdventure 替代自研 3D

- 仅当「多人同时在场 + 语音」成为刚需时启动；先做 2 周 spike，核对 LICENSE 与部署成本，再决定。

## 6. 验收指标

| 项 | 现状 | 目标 |
|---|---|---|
| 标题截断 | 80 字符硬截断 | 存储完整，列表 CSS 两行省略 + 详情全文 |
| 同热点重复 | 3 条/热点 | 1 条 + 引用项目数 |
| 详情可达 | 0 个详情页 | 热点/知识库/交付 3 类均可打开 |
| 列表分页 | 无 | 默认 20 条/页，服务端 `total` |
| 前端可维护性 | 1 文件 1,110 行 | 组件化 + 路由；单文件 < 300 行 |
| 自动化测试 | 无 | Playwright 3 条主链路 + pytest 接口冒烟 |

## 7. 风险与回滚

| 风险 | 应对 |
|---|---|
| 引入 Node 构建链 | `dist` 产物入库或服务器侧 `npm ci && build`；运行时不依赖 Node |
| 双系统并行数据不一致 | 两套 UI 共用同一 REST 契约；P0 先修数据层，UI 后迁 |
| 登录态/同源问题 | cookie `SameSite=Lax` 同源复用；iframe 不做跨域 |
| 3D 场景回归 | 3D 文件零改动，仅新增入口；保留原 URL |
| 迁移期业务中断 | `/legacy` 一键回滚，保留一个发布周期 |

## 8. 明确不做

- 不重写 three.js 场景（价值高、Refine 无收益）。
- 不用 Appsmith 承载主流程（平台锁定、代码不可评审）。
- 不把 Superset/Grafana 接进业务页（BI 工具，非工作流）。
- 不在 P0-P3 引入 n8n 替代审批（审批属业务状态机，迁移成本高于收益）。

## 9. 来源

1. 本机实测：`wc -l prototype/*.html prototype/*.py`（合计 10,010 行）、`grep` 统计 `/api/*` 路径 19 个。
2. 缺陷行号见第 1 节各链接（均为本仓库文件）。
3. 选型数据：GitHub REST API `/repos/{owner}/{repo}`，抓取时间 2026-11-07（★ 与 license 字段）。
4. Refine 定位与文档：https://refine.dev/docs/（HTTP 200 实测）、https://github.com/refinedev/refine（MIT，API 返回 `spdx_id=MIT`）。
5. WorkAdventure / n8n 许可：GitHub API 未返回 SPDX（`license=None`），需人工核对仓库 LICENSE 后再决策。
