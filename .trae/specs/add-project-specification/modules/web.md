# 模块规格 · web（Refine 新版控制台）

> 上级文档：[spec.md](../spec.md)。本文件只描述 `web/`。
> 规模：`web/src` 下 13 个 `.ts/.tsx` + 1 个 CSS，共 3,488 行；其中页面 1,856 行、数据/鉴权 318 行、样式 1,206 行（`wc -l`）。

## 1. 模块定位

`web/` 是工作台的**新版单页控制台**：以 Refine v4 提供资源列表/详情，以 React 18 + Vite 5 构建，产物由 `prototype/server.py` 以 `/app` 路径托管（`server.py:3389-3399`）。

- 与旧版 `prototype/index.html`（`/`）并存，`/app/legacy` 302 回旧版（`server.py:3391-3395`）。
- **只读为主**：`dataProvider` 的 `create/update/deleteOne` 一律 reject，写操作仅存在于组织配置页的少量直连接口（见 §4.3）。

## 2. 目录与文件清单（`wc -l`）

| 文件 | 行数 | 职责 |
| --- | ---: | --- |
| `src/main.tsx` | 10 | React 挂载入口 |
| `src/App.tsx` | 48 | Refine 初始化、路由、4 个 resource 声明 |
| `src/authProvider.ts` | 77 | 登录/登出/会话检查/401 处理 |
| `src/dataProvider.ts` | 90 | 列表/详情数据适配，写操作拒绝 |
| `src/types.ts` | 151 | 领域类型（Agent / Work / Hotspot / Knowledge / 事件等） |
| `src/components/Layout.tsx` | 38 | 侧边导航壳 |
| `src/components/MarkdownBody.tsx` | 11 | Markdown 渲染（`react-markdown` + `remark-gfm`） |
| `src/pages/HomePage.tsx` | 656 | 驾驶舱：状态快照、SSE 事件流、指令、审批 |
| `src/pages/WorksPage.tsx` | 388 | 作品档案列表 + 详情 |
| `src/pages/OrgPage.tsx` | 428 | 组织配置：制度/岗位/人员/技能/记忆 |
| `src/pages/HotspotsPage.tsx` | 157 | 热点列表 + 详情 |
| `src/pages/KnowledgePage.tsx` | 165 | 知识库列表 + 详情 |
| `src/pages/LoginPage.tsx` | 62 | 登录表单 |
| `src/styles.css` | 1,206 | 全站样式 |

## 3. 技术栈与构建

| 项 | 值 | 出处 |
| --- | --- | --- |
| Refine | `@refinedev/core` / `@refinedev/react-router-v6` `^4.0.0` | `package.json:12-13` |
| React | `^18.3.1` | `package.json:14-15` |
| 路由 | `react-router-dom ^6.0.0` | `package.json:17` |
| Markdown | `react-markdown ^10.1.0` + `remark-gfm ^4.0.1` | `package.json:16,18` |
| 构建 | `tsc --noEmit && vite build`，输出 `dist/` | `package.json:8`、`vite.config.ts:7-9` |
| 基路径 | `base: "/app/"` | `vite.config.ts:5` |
| 开发代理 | `/api`、`/login.html`、`/avatars` → `http://127.0.0.1:8848` | `vite.config.ts:10-17` |

## 4. 路由与数据契约

### 4.1 路由表

| 路径（浏览器） | 组件 | 鉴权 | 出处 |
| --- | --- | --- | --- |
| `/app/login` | `LoginPage` | 公开 | `App.tsx:29` |
| `/app/` | `HomePage` | `Authenticated` 包裹 | `App.tsx:32-37` |
| `/app/works` | `WorksPage` | 同上 | `App.tsx:38` |
| `/app/hotspots` | `HotspotsPage` | 同上 | `App.tsx:39` |
| `/app/knowledge` | `KnowledgePage` | 同上 | `App.tsx:40` |
| `/app/org` | `OrgPage` | 同上 | `App.tsx:41` |
| `/app/*` 其他 | 重定向到 `/` | — | `App.tsx:43` |

未登录时 `Authenticated` 以 `<Navigate to="/login" replace />` 回登录页（`App.tsx:32`）。

### 4.2 数据提供者（`dataProvider.ts`）

| 方法 | 映射 | 约定 |
| --- | --- | --- |
| `getList` | `GET /api/{resource}?page&size[&q&project&stage&status&sort]` | 响应 `{ok,total,page,size,items}`，取 `items/total`（`dataProvider.ts:50-70`） |
| `getOne` | `GET /api/{resource}/{id}` | 直接返回记录对象（`dataProvider.ts:72-80`） |
| `create/update/deleteOne` | — | 一律 `Promise.reject(new Error("not implemented"))`（`dataProvider.ts:87-89`） |
| 401 处理 | — | `window.location.assign("/app/login")`（`dataProvider.ts:20-27`） |
| 过滤字段白名单 | `q/project/stage/status/sort` | 其他字段忽略（`dataProvider.ts:34-48`） |

resource 仅声明 4 个：`works`、`hotspots`、`knowledge`、`org`（`App.tsx:21-26`）。

### 4.3 页面级直连接口

| 页面 | 调用 | 用途 | 出处 |
| --- | --- | --- | --- |
| HomePage | `GET /api/state` | 首屏快照 | `HomePage.tsx:81-89` |
| HomePage | `EventSource /api/events` | 实时事件；忽略 `heartbeat`，`state` 帧覆盖快照，其余入 feed（最多 80 条） | `HomePage.tsx:176-202` |
| HomePage | `POST /api/commands` | 下发自然语言指令 | `HomePage.tsx:246-253` |
| HomePage | `POST /api/approvals/{id}` | 老板审批（approve/reject） | `HomePage.tsx:282-290` |
| OrgPage | `GET /api/org` | 员工/制度/项目 | `OrgPage.tsx:75` |
| OrgPage | `GET /api/org/agent/{id}` | 员工详情 | `OrgPage.tsx:93` |
| OrgPage | `GET /api/org/policy?key=` | 制度正文 | `OrgPage.tsx:107` |
| OrgPage | `GET /api/org/audit?limit=30` | 变更审计 | `OrgPage.tsx:119` |
| OrgPage | `POST /api/org/agent/{id}` | 保存岗位/人员/性格 | `OrgPage.tsx:126` |
| OrgPage | `POST /api/org/agent/{id}/soul` | 保存人格 | `OrgPage.tsx:145` |
| OrgPage | `POST /api/org/agent/{id}/memory` | 保存记忆 | `OrgPage.tsx:156` |
| OrgPage | `POST /api/org/agent/{id}/skill` | 保存技能 | `OrgPage.tsx:181` |
| OrgPage | `POST /api/org/agent/{id}/config` | 保存个人配置 | `OrgPage.tsx:194` |
| OrgPage | `POST /api/org/policy` | 保存制度 | `OrgPage.tsx:205` |
| OrgPage | `POST /api/org/reload` | 重载配置 | `OrgPage.tsx:399` |

## 5. 鉴权契约

| 能力 | 实现 | 出处 |
| --- | --- | --- |
| 会话检查 | `fetch("/api/state")`：200=true，401=false 并 `redirectTo:"/login"`，其他错误带 `AuthError` | `authProvider.ts:7-15` |
| 登录 | `POST /api/login` JSON `{account,password}`，成功后写 `localStorage["opc.user"]` 并跳 `/` | `authProvider.ts:18-39` |
| 身份 | 优先 `GET /api/session`，网络异常回退 localStorage | `authProvider.ts:41-60` |
| 登出 | `POST /api/logout`，再清本地状态并跳 `/login` | `authProvider.ts:61-69` |
| 全局 401 | `onError` 跳 `/app/login` 并 `{logout:true}` | `authProvider.ts:70-76` |

## 6. 部署与缓存行为

- 构建产物由后端 `_web_dist` 提供：`/app/assets/*` 走 `public, max-age=31536000, immutable`；`index.html` 与其他路径 `no-cache`（`server.py:3389-3390, 3535-3538`）。
- 未命中文件回退 `web/dist/index.html`，支持 SPA 深链（`server.py:3520-3529`）。
- `web/dist` 未构建时返回 404 并提示「先在 web/ 执行 npm run build」（`server.py:3528-3529`）。

## 7. 失败与降级

| 场景 | 行为 | 出处 |
| --- | --- | --- |
| 会话过期（任意 API 401） | 强制跳 `/app/login` | `dataProvider.ts:20-27`、`authProvider.ts:70-76` |
| SSE 断开 | 仅关闭连接，无自动重连逻辑（浏览器 `EventSource` 默认行为除外）；未在本模块实现重试 | `HomePage.tsx:176-202`（已核实无 `onerror`） |
| 后端不可达 | 登录 `catch` 提示失败；登出仍清本地 | `authProvider.ts:27-36, 61-67` |
| 作品档案模块未加载 | `/api/works` 500，页面展示空列表 | `server.py:3429-3430` |
| 无 `dist` | `/app` 404 提示构建 | `server.py:3528-3529` |

## 8. 已知限制

1. `dataProvider` 只实现读，热点/知识无写接口，页面按钮若依赖写操作会直接 reject（`dataProvider.ts:87-89`）。
2. 首页 SSE 未处理 `onerror`，网络抖动后不会主动重建连接，需刷新页面。
3. 组织页为直连 `fetch`，错误处理各自实现，与 Refine 通知体系不统一。
4. `types.ts` 的 151 行领域类型与后端返回结构未做运行时校验，字段漂移只能靠 TS 编译期发现。
5. 前端无权限分级：`org` 页面所有登录用户可见，写操作由后端 403 兜底（`server.py:3706-3710`）。

## 9. 验收手段

- 构建：`cd web && npm ci && npm run build`，应生成 `web/dist/index.html` 与 `web/dist/assets/*`。
- 托管：`curl -s http://127.0.0.1:8848/app | head`，应返回 `index.html`。
- 深链：`curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8848/app/works`，应 200。
- 鉴权：清 Cookie 后访问 `/app/works`，应被重定向到 `/app/login`。
