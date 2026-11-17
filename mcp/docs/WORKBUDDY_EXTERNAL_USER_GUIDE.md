# WorkBuddy 接入 OPC MCP — 外部用户首次使用说明

适用对象：第一次使用、尚未接触过 OPC 内部环境、**本机操作系统/路径未知**的外部用户。
你只需要在 WorkBuddy 里填一个 URL 和一串个人 key，不需要安装 Python、不需要任何本地脚本、不需要填手机号。

---

## 0. 先拿到你的个人 key

1. 打开浏览器，访问：

   ```
   https://ai.boraydata.com/workbench/mcp-key
   ```

2. 用你的 **OPC 工作台账号**登录（就是绑定你手机号的那个账号）。
3. 在「外部 Agent API Key」页面点 **「生成 / 重新生成 key」**。
4. 立即复制页面显示的 key（形如 `opc_xxxxxxxx...`），**它只显示这一次**。
5. 页面会同时显示你这把 key 属于哪个 OPC 根目录和 vault，确认是自己的账号。

> 手机号只在这一步用于领取 key。以后在 WorkBuddy 里**不需要、也不要填手机号**。
>
> key 相当于密码：不要发到聊天群、不要提交到代码库、不要截图外传。忘记或怀疑泄漏就回这个页面重新生成，旧 key 会立刻失效。

---

## 1. 在 WorkBuddy 里配置

### 方式 A：界面配置（推荐）

1. 打开 WorkBuddy。
2. 进入 **插件 → MCP 服务器 → 配置 MCP**（不同版本可能叫「连接器 / 自定义 MCP」）。
3. 粘贴下面 JSON 并保存：

```json
{
  "mcpServers": {
    "opc-kb": {
      "type": "http",
      "url": "https://ai.boraydata.com/mcp",
      "headers": {
        "X-Mail-Agent": "<你的个人key>"
      }
    }
  }
}
```

4. 如果界面有「信任 / 启用 / 重启」按钮，点一下；状态变绿即连接成功。

> 若你的 WorkBuddy 版本不认识 `"type": "http"`，把它改成 `"type": "streamable-http"`，其余不变。

### 方式 B：改配置文件

| 系统 | WorkBuddy 用户级配置 | CLI 配置 |
| --- | --- | --- |
| Windows | `%USERPROFILE%\.workbuddy\mcp.json` | `%USERPROFILE%\.codebuddy\.mcp.json` |
| macOS / Linux | `~/.workbuddy/mcp.json` | `~/.codebuddy/.mcp.json` |

把上面 JSON 放进 `mcpServers` 里。**如果文件里已经有别的 MCP（如 sefs / mail / tts），只新增 `opc-kb` 这一项，不要整份覆盖。**

建议把配置文件权限设为仅自己可读（macOS/Linux：`chmod 600 ~/.workbuddy/mcp.json`）。

---

## 2. 验证是否连上

在 WorkBuddy 对话里直接说：

> 调用 `opc_whoami`，告诉我当前身份和 OPC 根目录。

正常情况下会返回你的手机号、姓名、角色，以及类似：

```text
tenant: 189****9286
dataDir: /mnt/sefs/conring/users/<你的手机号>
workspace: /mnt/sefs/conring/users/<你的手机号>/mcp
```

如果能看到这些，说明 key、网络、连接器都通了。

---

## 2.5 这把 key 能看到什么（资源互通）

key 自带手机号，因此你能通过 MCP 访问**该手机号名下的全部资源**，不局限于你用 MCP 上传的素材：

| 来源 | 具体资源 | 访问工具 |
| --- | --- | --- |
| OPC 数据库 | 知识条目（含 Obsidian/Web 内化的）、报告、内容稿、消息、流水线、审批 | `kb_search` / `opc_resources` / `opc_resource_list` / `opc_resource_read` |
| Obsidian vault | `OPC/知识库`、`技术研究`、`交付物` 等目录下的 md | `opc_resource_list(kind=vault)` / `opc_resource_read(ref=vault:相对路径)` |
| MCP 自有素材 | 你上传的素材、切块、知识卡片 | `material_list` / `kb_search` / `material_download` |
| 全局共享（所有 key 可见） | 全公司知识（含 vault 内化条目）、共享素材清单、公开下载区、共享热点流 | `opc_resources.shared` / `opc_resource_list(kind=shared_* 或 downloads)` / `opc_resource_read(ref=shared_*)` |

也就是说：**用 Web 或 Obsidian 生成的资料，通过同一把 key 在 MCP 里也能检索和读取**；反过来 MCP 产生的问答消息也会进入 Web 消息中心，实现双向互通。**全局共享部分不按手机号隔离，任何 key 都能看到**——公司制度、共享素材、公开下载、系统热点采集等（这与 Web 上 `/materials/`、`/downloads/`、共享热点流对所有用户开放一致）。

跨源检索工具 `kb_search` 会一次搜齐 OPC 知识库/报告/内容/消息、MCP 素材和全局共享内容，命中结果带 `source` 字段标明出处：`opc.*`（本人手机号）、`mcp.material`（本人上传）、`shared.*`（全局共享）。

安全边界：

- 私有资源只限**自己手机号**；别人手机号的私有资料不可见。上表的「全局共享」部分例外，所有 key 可见。
- 敏感文件按 Web 同口径屏蔽：`公司/登录凭据.md`、隐藏目录、含「运维手册」的路径、文件名以「台账/凭据/拓扑」结尾的 md、下划线开头的文件——这些文件**用 `opc_resource_read` 会被拒绝**，不要尝试绕过。
- 全局共享知识只暴露标记为「全公司」的条目（`project_id=全公司`），其他手机号/项目的私有条目不会泄漏。
- vault 读取限定在自己的 vault 目录内，越界路径（如 `../`）会被拒绝。

---

## 3. 日常怎么用

直接用自然语言让 WorkBuddy 调工具即可，例如：

| 你想做的事 | 可以这样说 |
| --- | --- |
| 看这把 key 能访问什么 | 「调用 `opc_resources`，列出我手机号下所有资源」 |
| 查公司知识库（含 OPC/Obsidian/Web 资料） | 「用 `kb_search` 查「分片上传的 offset 语义」」 |
| 让 OPC 总结回答 | 「用 `ask` 的 text 参数问：大文件上限是多少？并给引用」 |
| 列出 OPC 库条目 | 「用 `opc_resource_list` 列 kind=knowledge / reports / contents / vault」 |
| 读某条资料原文 | 「用 `opc_resource_read` 读 ref=knowledge:kb_xxx 或 vault:OPC/知识库/a.md」 |
| 看知识库状态 | 「调用 `kb_status`，告诉我素材数、OPC 库条目数和可检索片段数」 |
| 看已有素材 | 「用 `material_list` 列出我的素材」 |
| 导出某个素材 | 「用 `material_download` 把 mat_xxx 导出」 |
| 生成语音/图片 | 「用 `tts` 说一句欢迎词」/「用 `image` 画一张图」 |
| 传大文件 | 「用 `upload_begin` 建单，我要上传一个 200MB 的 zip」 |

注意：

- `ask` 的问题内容用 **`text` 或 `question`** 任一传入都可以（两者等价，推荐 `text`）。
- 图片类素材只归档，不做像素级分析，也不会进入检索片段；问「照片里是谁」会被明确拒答。
- 上传/生图/TTS 都是真实写操作，会产生文件；不需要时不要重复触发。

---

## 4. 上传文件（超过 100MB 走这里）

小于 100MB 的文件可以让 WorkBuddy 用 `material_upload` 的 `content_base64` 一次传完；更大的文件必须走 `upload_begin` 分片通道（单文件上限 2GB）。

分片通道支持四种协议，任选其一：

| 协议 | 关键写法 |
| --- | --- |
| 原生 offset | `PUT /v1/uploads/<id>?offset=<已传字节>` |
| Content-Range | `Content-Range: bytes 0-99/200`；`bytes */*` 可查进度 |
| 片号 | `PUT ?part=0`，**从 0 开始**，偏移 = 片号 × chunk_size |
| Tus 1.0 | `POST /files` → `HEAD/PATCH /files/<id>` |

上传接口也认你那把 key，直接放在请求头即可：

```bash
curl -X POST https://ai.boraydata.com/conring/upload/v1/uploads \
  -H "X-Mail-Agent: <你的个人key>" \
  -H "Content-Type: application/json" \
  -d '{"filename":"demo.zip","size":12345,"chunk_size":8388608}'
```

PowerShell 用户把 `curl` 换成 `curl.exe`，引号规则相同。后续 `PUT` 时继续带同一个 `X-Mail-Agent` 头。

中断后重新查进度 URL 拿当前 offset，从那里继续即可；重复传同一片会返回 409 并告诉你应从哪个 offset 继续，不会传重。

---

## 5. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `need_auth` /「缺少身份凭据」 | 没填 `X-Mail-Agent`，或 key 没复制到配置里 |
| 「个人 API key 无效或已撤销」 | key 过期/被撤销/复制不全；回领 key 页重新生成一次，旧 key 立即失效 |
| `need_secret` | 你在调用里只传了手机号；接入不需要手机号，只用 key |
| 连不上 / 超时 | 确认网络能访问 `https://ai.boraydata.com`；公司外网或代理环境请找 IT 放行 443 |
| 提示 MCP 类型不支持 | 把 `type` 从 `http` 改成 `streamable-http` |
| 状态灯绿但工具列表为空 | 重启连接器；仍不行则删掉配置重新粘贴，注意 `mcpServers` 层级不要写错 |
| 找不到 key 页面 | 确认你有工作台账号且账号绑定了手机号；没有账号请联系管理员开通 |

上传服务健康检查（一般不需要）：

```
https://ai.boraydata.com/conring/upload/healthz
```

注意根路径 `https://ai.boraydata.com/healthz` 是工作台登录页，不是 MCP 健康检查。

---

## 6. 让 WorkBuddy 自动关联 OPC（推荐）

MCP 本身不会监听你说的话；要让「知识库 / 知识管理 / OPC / 同心圆 / 知识图谱 / 图数据库」这类词一出现就自动走 OPC MCP，需要给 WorkBuddy 装一个小技能（Skill）。

1. 随本说明附带的 `WORKBUDDY_OPC_KNOWLEDGE_SKILL.md` 就是技能文件，内容原样保存为：

   | 系统 | 保存路径 |
   | --- | --- |
   | Windows | `%USERPROFILE%\.workbuddy\skills\opc-knowledge\SKILL.md` |
   | macOS / Linux | `~/.workbuddy/skills/opc-knowledge/SKILL.md` |

   路径中的 `opc-knowledge` 目录需要自己新建。若 WorkBuddy 版本不支持用户级技能，改用其界面里的「自定义技能 / 触发器」，把技能说明里的触发词填进去。

2. 保存后在 WorkBuddy 里刷新技能列表（或重启一次）。

3. 之后直接用自然语言说话即可，不必再手动指定工具，例如「帮我查一下知识库里关于同心圆的说明」「用 OPC 总结一下图数据库的方案」。技能会按这句话自动调用 `kb_search` / `ask`；不确定时先调 `opc_route` 拿推荐工具链。

> 触发词已写进技能的 `description` 和 MCP 工具描述，匹配由 WorkBuddy/模型完成；若某次没自动触发，补一句「用 OPC 知识库」即可。

---

## 7. 安全提醒

1. key 只在领 key 页面显示一次，WorkBuddy 配置里保存的是明文，**不要把配置文件发给别人**。
2. 不要在 WorkBuddy 对话里粘贴你的 key；连接器已经通过请求头注入，模型不需要看到它。
3. 更换手机号、离职或怀疑泄漏时，立即回领 key 页撤销/重新生成。
4. 每个手机号对应独立的 OPC 数据空间，别人的 key 看不到你的素材和知识库，反之亦然。
