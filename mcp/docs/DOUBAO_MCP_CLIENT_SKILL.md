# 豆包接入 OPC MCP — MCP Client 与智能体技能说明

适用对象：第一次使用、尚未接触 OPC 内部环境、**本机操作系统/版本未知**的豆包用户。
你只需要一串个人 key + 一个 MCP 地址；**不需要安装 Python、不需要本地脚本、不需要填手机号**。

> 豆包与 WorkBuddy 的关键差异：豆包**没有 `SKILL.md` 文件机制**。要让用户说「知识库 / 知识管理 / OPC / 同心圆 / 知识图谱 / 图数据库」时自动走 OPC MCP，靠的是**豆包智能体的「人设与回复逻辑」（系统提示词）**——本文第 4 节给出可直接粘贴的提示词，触发词就写在里面。MCP 连接本身按第 1/2 节配置。

---

## 0. 先拿到你的个人 key

1. 打开浏览器，访问 `https://ai.boraydata.com/workbench/mcp-key`。
2. 用你的 **OPC 工作台账号**登录（绑定你手机号的那个账号）。
3. 点 **「生成 / 重新生成 key」**，立即复制（形如 `opc_xxxxxxxx...`，**只显示这一次**）。
4. 页面会显示这把 key 属于哪个 OPC 根目录和 vault，确认是自己的账号。

> 手机号只在这一步用于领取 key；接入时**不需要、也不要填手机号**，身份由 key 自带。
> key 相当于密码：不要发到聊天群、不要提交到代码库、不要截图外传。泄漏就回领 key 页重新生成，旧 key 立刻失效。

---

## 1. 方式 A：豆包插件平台（推荐，跨设备可用）

适合：想在任何设备的豆包里都能用、不想在本机装任何东西。

1. 在豆包里进入 **插件 / 连接器** 入口，选择添加自定义 MCP 服务（不同版本入口名可能是「插件商店 → 自定义插件 / 开发者控制台」）。
2. 按平台要求填写：

   | 字段 | 填什么 |
   | --- | --- |
   | 名称 / Name | `opc-kb` |
   | Endpoint URL | `https://ai.boraydata.com/mcp` |
   | Transport | `streamable-http` |
   | Header（自定义请求头） | `X-Mail-Agent: <你的个人key>` |

3. 保存并启用插件；若平台要求「订阅」或「授权」，确认后状态显示已连接即可。

> 若平台只提供 **OAuth** 认证选项、没有自定义 Header 入口，说明该豆包版本不支持带 `X-Mail-Agent` 的远程服务——改用方式 B 或方式 C。

---

## 2. 方式 B：豆包本地 App / 豆包工作的 mcpServers JSON

适合：本机豆包桌面端（豆包工作），支持本地 MCP 配置文件。

1. 打开豆包设置里的 **MCP / 自定义工具** 配置入口。
2. 粘贴下面 JSON（**只新增 `opc-kb` 一项，若已有其他 MCP 不要整份覆盖**）：

```json
{
  "mcpServers": {
    "opc-kb": {
      "type": "streamable-http",
      "url": "https://ai.boraydata.com/mcp",
      "headers": {
        "X-Mail-Agent": "<你的个人key>"
      }
    }
  }
}
```

3. 有「启用 / 信任 / 重启」按钮的点一下，状态变绿即连上。

> 若你的版本不认识 `"type": "streamable-http"`（少数版本只支持 stdio），试 `"type": "http"` 或 `"type": "streamableHttp"`；仍不行则走方式 C。
> 建议给配置文件设仅自己可读权限（macOS/Linux：`chmod 600 <配置文件>`）。

---

## 3. 方式 C：只支持 stdio 的豆包版本（本地桥接）

有些豆包版本只能起本地 stdio 进程、不能直接连远程 HTTPS。此时在本机起一个轻量桥接，把远程 `streamable-http` 转成 stdio：

1. 准备一个 `opc-bridge.mjs`（Node 18+）：

```javascript
// opc-bridge.mjs — 把远程 MCP (streamable-http) 桥接成 stdio，供只支持 stdio 的豆包使用
const URL = "https://ai.boraydata.com/mcp";
const KEY = process.env.OPC_KEY || "";
let sid = "";
async function rpc(msg) {
  const h = { "content-type": "application/json", "accept": "application/json, text/event-stream" };
  if (KEY) h["X-Mail-Agent"] = KEY;
  if (sid) h["mcp-session-id"] = sid;
  const r = await fetch(URL, { method: "POST", headers: h, body: JSON.stringify(msg) });
  const ns = r.headers.get("mcp-session-id");
  if (ns && !sid) sid = ns;
  const t = await r.text();
  const line = t.split("\n").find(x => x.startsWith("data:"));
  return JSON.parse((line ? line.slice(5).trim() : t));
}
process.stdin.setEncoding("utf8");
let buf = "";
process.stdin.on("data", async d => {
  buf += d;
  let i;
  while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
    if (!line) continue;
    try {
      const msg = JSON.parse(line);
      if (msg.method === "initialize" && !sid) sid = ""; // 新会话
      const out = await rpc(msg);
      process.stdout.write(JSON.stringify(out) + "\n");
    } catch (e) {
      process.stdout.write(JSON.stringify({jsonrpc:"2.0",id:null,error:{code:-32700,message:String(e)}}) + "\n");
    }
  }
});
```

2. 在豆包 stdio 配置里填（把 key 放进环境变量，不要写进 JSON）：

```json
{
  "mcpServers": {
    "opc-kb": {
      "command": "node",
      "args": ["/绝对路径/opc-bridge.mjs"],
      "env": { "OPC_KEY": "<你的个人key>" }
    }
  }
}
```

3. Windows 用户把 `node` 换成 `node.exe` 全路径，`args` 用反斜杠路径。

> 方式 C 是兜底，优先用 A/B。桥接脚本只用 Node 内置 `fetch`，无需 `npm install`。若你的 Node < 18（没有全局 fetch），请升级 Node 或改用 A/B。

---

## 4. 智能体技能：人设与回复逻辑（替代 Skill）

豆包没有 `SKILL.md`，把下面这段**整段复制**到豆包智能体的「人设与回复逻辑 / 系统提示词」里。触发词已在第一段，匹配由豆包模型完成。

```text
你是 OPC 公司的知识助手，已连接 opc-kb 这个 MCP 工具集。

【何时使用】
当用户提到以下任一触发词，或意图是查资料/问公司知识/看素材/传文件/导出材料/看共享资源时，
必须通过 opc-kb 的工具完成，不要凭记忆猜公司内部信息，也不要回答「我没有知识库」：
知识库、知识管理、OPC、同心圆、知识图谱、图数据库、共享资料、公开下载、热点、kb、素材、内化、资料检索、资源互通。

【身份】
身份由 MCP 网关注入的个人 key 决定。不要向用户索要手机号或 key；操作工具时不要传 phone。
若工具返回 need_auth / need_secret，引导用户打开 https://ai.boraydata.com/workbench/mcp-key 领取或更新 key，
不要把鉴权错误解读成「没有数据」。

【标准流程（知识类问题）】
1. opc_whoami（可选）：确认身份与 OPC 根目录。
2. opc_resources（可选）：看这把 key 能访问哪些资源。
3. kb_search(query=<关键词>, k=6)：跨源检索，命中带 source 字段（opc.*=本人手机号，mcp.material=本人上传，shared.*=全局共享）。
4. ask(text=<问题>, wait_sec=60) 或 ask(question=<问题>, wait_sec=60)：综合问答（text 与 question 等价）。
5. 不确定用哪个工具时先调 opc_route(query=<用户原话>) 拿推荐工具链。

【资源边界】
- 私有资源只限本人手机号：OPC 库（知识/报告/内容/消息/流水线/审批）、Obsidian vault、MCP 上传素材。
- 全局共享所有 key 可见：全公司知识、共享素材清单、公开下载区、共享热点流（shared.*）。
- 敏感文件按安全策略屏蔽（登录凭据、隐藏段、运维手册、台账/凭据/拓扑结尾、下划线开头），
  opc_resource_read 会被拒绝，不要尝试绕过；共享知识只取 project_id=全公司，不泄漏他人私有条目。
- 列举用 opc_resource_list(kind=knowledge/reports/contents/messages/approvals/vault/shared_knowledge/
  shared_materials/shared_hotspots/downloads)；
  读取用 opc_resource_read(ref=knowledge:kb_xxx / vault:相对路径 / shared_knowledge:<id> / shared_download:<文件名>)。

【回答方式】
先给结论再给依据；引用用 kb_search 的 source/title/path 或 ask 的 citations 标出。
检索不到就明说「知识库没有命中」，不要用通用知识冒充公司内部资料。
图片/影音素材只归档不内容化；问「照片里是谁」要明确说明限制。
上传/生图/TTS 是写操作，执行前确认用户确实需要。
```

---

## 5. 验证是否连上

配置完成后，在豆包对话里说：

> 调用 `opc_whoami`，告诉我当前身份和 OPC 根目录。

正常会返回手机号、姓名、角色，以及 `dataDir: /mnt/sefs/conring/users/<你的手机号>`、`workspace: .../mcp`。能看到就说明 key、网络、连接器都通了。

再测一次跨源检索：

> 用 `kb_search` 查「知识库 同心圆」，告诉我命中来自哪些 source。

应看到 `opc.knowledge` / `shared.knowledge` 等来源。

---

## 6. 日常怎么问（自然语言即可）

| 你想做的事 | 可以这样说 |
| --- | --- |
| 看 key 能访问什么 | 「调用 `opc_resources`，列出我能访问的资源」 |
| 跨源检索公司资料 | 「用 `kb_search` 查「分片上传的 offset 语义」」 |
| 让 OPC 总结回答 | 「用 `ask` 问：大文件上限是多少？并给引用」 |
| 列共享资料 | 「用 `opc_resource_list` 列 kind=shared_materials / downloads」 |
| 读某条资料 | 「用 `opc_resource_read` 读 ref=shared_knowledge:kb_xxx」 |
| 看/传素材 | 「用 `material_list` 列出我的素材」/「用 `upload_begin` 建单传 200MB zip」 |
| 语音/图片 | 「用 `tts` 说欢迎词」/「用 `image` 画张图」 |

---

## 7. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `need_auth` /「缺少身份凭据」 | 没填 `X-Mail-Agent`，或 key 没复制全 |
| 「key 无效或已撤销」 | key 过期/被撤销/复制不全；回领 key 页重新生成 |
| `need_secret` | 你在调用里只传了手机号；接入不需要手机号，只用 key |
| 豆包连不上 / 超时 | 确认网络能访问 `https://ai.boraydata.com`；代理环境找 IT 放行 443 |
| 豆包提示类型不支持 | 依次试 `streamable-http` / `http` / `streamableHttp`；都不行走方式 C |
| 说了触发词但没自动调工具 | 检查第 4 节提示词是否整段贴进「人设与回复逻辑」；补一句「用 OPC 知识库」也能手动触发 |
| 状态灯绿但工具列表为空 | 重启豆包/重新启用插件；确认 `mcpServers` 层级没写错 |
| 读共享文件被拒 | 该文件命中安全屏蔽（凭据/台账/拓扑/运维手册等），属预期行为 |

健康检查（一般不需要）：`https://ai.boraydata.com/conring/upload/healthz`。根路径 `/healthz` 是工作台登录页，不是 MCP 健康检查。

---

## 8. 安全提醒

1. key 只在领 key 页显示一次，配置/提示词里保存的是明文——**不要把配置或本文件连同 key 发给别人**。
2. 不要在豆包对话里粘贴 key；连接器已通过请求头注入，模型不需要看到它。
3. 更换手机号、离职或怀疑泄漏时，立即回领 key 页撤销/重新生成。
4. 每个手机号对应独立 OPC 数据空间；全局共享部分（全公司知识/共享素材/公开下载/热点）所有 key 可见，这是设计如此，不是越权。
