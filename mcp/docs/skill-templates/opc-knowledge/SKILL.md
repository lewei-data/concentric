---
name: opc-knowledge
description: 查询 OPC 内化知识库、素材与跨源资源；库内事实问答、素材删除与出处追溯时使用
whenToUse: 用户问公司内部资料/规范/盘点，或要求查素材、删自己上传的素材、追出处
---

# OPC 知识库与素材

## 身份

可选：先调用 `mcp__opc__opc_whoami` 确认身份是否识别成功。返回里的 `dataDir` / `workspace` 只用于连通性自检，不要转述给用户。

## 库内事实问答

库内事实、规范、盘点、配置类问题，直接用 `mcp__opc__ask` 并带 `with_brief=true`：

- 简报自带按五行三才分组的图谱信息，有字符硬预算，通常不必先调 `mcp__opc__kb_search`。
- 该模式同时按对外口径处理内网地址与服务端路径，无需额外过滤。
- 检索不到就明确说没有，不要用通用知识冒充公司内部资料。

## 素材

- 用户说"我上传的素材"：先 `mcp__opc__material_list` 拿 id。
- 删除前跟用户确认，再调用 `mcp__opc__material_delete`。
- 导出用 `mcp__opc__material_download`，按返回结果告知用户如何访问。

## 出处追溯

- 完整出处以 `knowledge_brief.items[].source` 或 `citations[].path` 为准；正文里的 `→ 路径` 是**截断显示，不可直接读取**。
- 取到完整 vault 相对路径后，用 `mcp__opc__opc_resource_read(ref=vault:完整路径)` 读原文；不要使用本地读文件命令。
- `citations` 命中 `opc.message` 时 `path` 为空，这类消息引用**不可追溯原文**，不要尝试构造路径。

## 对外红线

- 只用公网入口连接。
- 回复里不转述服务端绝对路径、`dataDir` / `workspace`、内网 IP 或内部端口。
- 删除类操作执行前先确认。
