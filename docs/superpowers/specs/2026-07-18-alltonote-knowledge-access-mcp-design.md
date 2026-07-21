# AllToNote Knowledge Access MCP 设计

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-13-alltonote-cli-first-vault-workspace-design.md
  - 2026-07-14-alltonote-portable-artifact-source-bundle-design.md
downstream:
  - ../plans/2026-07-18-alltonote-knowledge-access-mcp-implementation-plan.md
implementation_status: not-started
last_verified_at: 2026-07-18
```

## 1. 决策摘要

AllToNote 提供一个独立、默认只读的 Knowledge Access MCP Server，让本地 Codex、Claude Code、IDE Agent 或其他 MCP Host 在不启动 Desktop 的情况下读取用户已发布知识。

```text
Agent/MCP Host
  -> stdio 启动 alltonote mcp knowledge
  -> capability + Workspace Grant
  -> inspect/tree/search/read/backlinks
  -> 默认只读 wiki/personal + 用户显式选中的 common
  -> 可选 provenance grant 只读最小 Source/Evidence/Transcript range
```

它与 Production MCP 完全分离：

- Knowledge MCP 没有 produce、review approve、publish、delete、write-file 或 shell 工具；
- 默认无需 daemon，Server 是由 MCP Host 启动的短生命周期 stdio 子进程；
- 本地 stdio 不使用远端 OAuth；权限来自 OS 用户边界、显式 Workspace Grant 和启动配置；
- 公共远端知识通过另一个 Streamable HTTP 服务暴露，不能借此读取个人本地 Vault。

## 2. 为什么必须拆成独立 MCP

“读取已有知识”与“调用模型/下载器生产知识”在四个维度不同：

| 维度 | Knowledge Access | Production |
|---|---|---|
| 副作用 | 无 | 创建 Job、网络访问、付费、写 raw |
| 默认权限 | 最小只读 | 必须显式授权 |
| 延迟 | 毫秒到秒 | 分钟到小时 |
| 安全风险 | 私密内容泄露、路径越权 | 另加成本、外部操作、Prompt injection、发布越权 |

把两者放在一个 Server 会导致 Host 只为搜索笔记就获得生产甚至发布能力，也会让工具列表、授权和审计难以理解。

## 3. 目标

- 让任意支持 MCP 的本地 Agent 可检索并读取已发布 Markdown；
- 不依赖 Desktop、网站或常驻 Engine；
- 不暴露绝对路径、控制文件、JobStore、Secret 或 raw 全库；
- 提供稳定、分页、大小受限、可引用的结构化结果；
- 保持 Markdown 为事实源，索引可删除重建；
- 支持文档元数据、backlinks 和可选 Evidence 定位；
- 对 Prompt injection 和恶意 Markdown 给出明确内容边界；
- 未来公共远端知识可使用同一逻辑读模型，但使用独立传输和授权。

## 4. 非目标

- 不做聊天/RAG 会话管理；
- 不替 Agent 选择最终答案；
- 不把整个 Vault 自动塞进模型上下文；
- 不提供写入、重命名、删除或发布；
- 不默认暴露 `raw/personal`、Draft、完整 Transcript 或私密 Source；
- 不以向量数据库作为事实源；
- 不返回任意文件系统路径或任意文件读取；
- 不在 stdio Server 内启动长时间 Producer；
- 不依赖 MCP experimental Tasks；
- MVP 不实现资源订阅和主动推送。

## 5. 权限模型

### 5.1 Workspace Grant

用户先通过 CLI/Desktop 创建 grant：

```json
{
  "grant_id": "wg_...",
  "workspace_id": "...",
  "audience": {"kind": "local-mcp-client", "name": "codex"},
  "spaces": ["wiki/personal"],
  "path_prefixes": ["engineering/", "learning/"],
  "capabilities": ["inspect", "tree", "search", "read", "backlinks"],
  "provenance": "none",
  "expires_at": null,
  "created_by": "local-user",
  "revoked_at": null
}
```

Grant 保存于机器级受保护配置，不写入 Vault。Server 启动只接收 grant ID，不接受调用者随意传入任意 Vault path + `allow-all`。

### 5.2 权限层级

| Level | 默认 | 可访问内容 |
|---|---|---|
| `published` | 是 | grant 范围内正式 Markdown 与其公开元数据 |
| `published+provenance-ranges` | 否 | 另加正式文档已引用的最小 Evidence/Source/Transcript 范围 |
| `raw-read` | 否，MVP 不提供给通用 Agent | raw Bundle 全量读取 |

provenance range 访问必须从已发布文档中的合法 EvidenceRef 反向解析；调用者不能自行构造任意 raw 路径。

### 5.3 本地 stdio 身份

- MCP Host 以当前 OS 用户启动 Server；
- Server 继承该用户权限，但仍执行 AllToNote Grant；
- 不在 stdio 上套用 HTTP OAuth；
- grant ID 不是 Secret，但只有当前用户可读取 grant store；
- 对需要隔离的 Host，用户创建不同 grant；
- 进程环境、cwd、argv 和日志不能扩张 grant 范围。

### 5.4 公共远端知识

公共 MCP 是网站控制面的独立部署：

- Streamable HTTP；
- 遵循 MCP Authorization/OAuth 与 audience binding；
- 只访问服务器明确发布的公共知识包；
- 不接受本地 Vault path；
- 不共享本地 grant store；
- 远端 token 不能用于本地 stdio Server；
- 配额、订阅和许可由网站控制面负责。

## 6. Server 生命周期

启动命令：

```text
alltonote mcp knowledge --grant <grant-id>
```

生命周期：

1. 读取 Runtime/iwiki 合同锁；
2. 解析 grant 和 WorkspaceCatalog 中的 workspace identity；
3. inspect/validate Workspace；
4. 初始化只读 IWikiSession/VaultBrowser/KnowledgeSearch；
5. 通过 stdio 完成 MCP initialize/capability negotiation；
6. 按请求读取，断开 stdin 后退出；
7. 不留下常驻进程。

启动失败必须在 MCP 协议允许范围内报告稳定错误；不得向 stdout 写启动 banner 或普通日志破坏 JSON-RPC framing。

## 7. MCP 能力面

### 7.1 Resources

使用不含绝对路径的 URI：

```text
alltonote://workspace/<workspace-id>/document/<document-id>
alltonote://workspace/<workspace-id>/document/<document-id>/metadata
alltonote://workspace/<workspace-id>/document/<document-id>/outline
alltonote://workspace/<workspace-id>/evidence/<evidence-id>?start=...&end=...
```

MVP Resources：

- `document`：规范化 Markdown 正文；
- `metadata`：title、space、logical path、tags、updated hash、source refs；
- `outline`：heading tree 与稳定锚点；
- `evidence range`：仅 provenance grant 且 EvidenceRef 合法。

资源列表可分页；大正文读取必须支持范围/章节参数或返回“需要工具分页”的结构化提示。

### 7.2 Tools

#### `knowledge_workspace_inspect`

输入：无或 workspace selector（只能在 grant 内）。

输出：workspace identity、contract/capability、允许 spaces/path prefixes、索引状态、更新时间；不返回绝对路径。

#### `knowledge_tree`

输入：`parent_id/path_token`、depth（上限）、cursor、page_size。

输出：文件夹/文档逻辑节点、title、document_id、是否有子节点。

#### `knowledge_search`

输入：

```json
{
  "query": "...",
  "scope": {"spaces": ["personal"], "path_prefix": "engineering/"},
  "mode": "lexical",
  "limit": 10,
  "cursor": null,
  "include_snippets": true
}
```

MVP `mode` 只保证 lexical/FTS；未来 semantic/hybrid 必须作为 capability 宣告，不能改变默认排序语义而不升版本。

输出包含 document_id、title、logical path token、heading、短 snippet、score type、content hash。snippet 长度和匹配数量受限。

#### `knowledge_read`

输入：document_id、可选 heading/range、最大字符数、cursor。

输出：Markdown 内容块、title、content hash、range/continuation、引用 metadata。不得接受任意文件路径。

#### `knowledge_outline`

输入 document_id。输出 heading ID、层级、标题和范围 token，帮助 Agent 精确选择章节。

#### `knowledge_backlinks`

输入 document_id。输出 grant 范围内引用该文档的文档摘要；不会因 backlink 泄露未授权文档标题或路径。

#### `knowledge_evidence_read`

仅 provenance grant。输入必须是从已授权 PublishedDocument 返回的 `evidence_token`，而不是任意 source ID/path。输出最小证据范围和 source type/locator/quality，不默认返回完整 Transcript。

### 7.3 Prompts

MVP 不必须提供 Prompt。若未来提供，只能是无 Secret 的使用模板，例如“先搜索、再读章节、最后引用 document/evidence token”；Prompt 不获得额外权限。

## 8. 返回合同

每个结果包含：

```json
{
  "api_version": 1,
  "workspace_id": "...",
  "content_revision": "sha256:...",
  "data": {},
  "truncated": false,
  "next_cursor": null,
  "warnings": [],
  "trust": {
    "kind": "user-authored-or-published-knowledge",
    "instructional_authority": "untrusted-content"
  }
}
```

`trust` 明确告诉 Host：返回正文是知识数据，不是系统/工具指令。无法强制所有 Host 正确处理，但 Server 不应省略边界。

### 8.1 大小限制

默认：

- search query 最大长度；
- page size 最大 50；
- snippet 每条最大约 1,000 字符；
- 单次 read 默认最大约 20,000 字符，可配置上限但不能无限；
- evidence range 默认最大若干分钟/若干页/若干行；
- 返回总字节硬上限；
- 超限使用 cursor/section token，不静默截断。

确切常量由实现计划锁定并通过兼容测试。

### 8.2 错误

稳定错误至少包括：

```text
MCP_GRANT_NOT_FOUND
MCP_GRANT_REVOKED
MCP_SCOPE_DENIED
MCP_WORKSPACE_UNAVAILABLE
MCP_WORKSPACE_CONTRACT_INCOMPATIBLE
MCP_INDEX_STALE
MCP_DOCUMENT_NOT_FOUND
MCP_CONTENT_CHANGED
MCP_CURSOR_INVALID
MCP_RESULT_TOO_LARGE
MCP_PROVENANCE_NOT_ALLOWED
MCP_EVIDENCE_TOKEN_INVALID
MCP_INTERNAL_ERROR
```

权限错误不泄露目标是否存在。

## 9. 搜索与索引

### 9.1 事实源

Markdown + iwiki metadata 是事实源。FTS、QMD、向量或 backlink cache 都是派生数据：

- 可删除重建；
- 不被 MCP 当成独立正文；
- 每条结果携带对应 content hash；
- read 时发现 hash 漂移则以当前文件为准并触发/提示重建；
- 索引失败不应阻止直接 tree/read，但 search 可明确降级或报 stale。

### 9.2 外部编辑

Obsidian/编辑器修改后：

- watcher 仅用于快速失效；
- 每次 read 做必要的真实文件检查；
- watcher overflow 触发全量 dirty 标记，不假装索引最新；
- MCP search 返回索引 generation；
- 调用者可选择接受 stale snippet 后再 read 当前内容。

### 9.3 semantic search

未来语义搜索是可选 Pack：

- 本地 embedding 默认，不自动上传私人正文；
- 如果使用远端 embedding，必须显式 consent/provider policy；
- 向量条目绑定 document content hash；
- semantic score 与 lexical score 类型分开；
- 不因向量结果绕过 grant/path filter；
- 删除索引不影响知识可读。

## 10. 路径与内容安全

- URI/document_id 到路径映射由 VaultBrowser/IWikiSession 完成；
- canonical containment、Windows reparse point、UNC、ADS、大小写和 Unicode 归一化必须测试；
- 不读取 symlink/junction 指向 grant 之外的内容；
- 不返回 `.git`、`.obsidian`、iwiki 私有控制区、JobStore、Secret 或临时文件；
- Markdown 中的远程图片/链接只作为文本返回，Server 不主动抓取；
- HTML/script 不执行；
- frontmatter 中未知字段按数据处理；
- 文档内“忽略之前指令”等内容不改变工具权限或行为；
- 搜索 query 不拼接到 shell/SQL，FTS 使用参数化和限定语法。

## 11. 隐私与审计

默认不记录正文和 query 全文。可记录有限本地审计：

- server session ID；
- grant ID；
- tool name；
- document/evidence opaque ID；
- result bytes/count；
- duration/error code；
- timestamp。

审计保存在机器级日志，可关闭或按保留期清理。公共远端 MCP 的服务端日志、配额和隐私策略必须另行公开，不能沿用本地默认。

## 12. 性能预算

在已有健康索引、10,000 Markdown 文档基线上：

| 操作 | 目标 |
|---|---|
| stdio Server 启动并 initialize | p95 < 1 s |
| workspace inspect | p95 < 200 ms |
| tree 首屏 | p95 < 300 ms |
| lexical search top 10 | p95 < 500 ms |
| read 20k 字符 | p95 < 200 ms（本地 SSD） |
| outline | p95 < 200 ms |

50,000 文档作为压力记录，不作为 MVP 所有机器的硬承诺；必须记录峰值内存、索引时间和 watcher 恢复。

## 13. 测试矩阵

### 13.1 MCP 合同

- initialize/capability；
- stdout 纯 JSON-RPC、stderr 不破坏 framing；
- tools/resources schema golden；
- pagination/cursor/content hash；
- 大结果截断与 continuation；
- 稳定错误 mapping；
- client disconnect 后退出。

### 13.2 授权

- 默认只读 published；
- grant path/space 过滤；
- revoked/expired grant；
- 未授权存在与不存在返回不泄露；
- provenance token 只能从授权文档产生；
- token 不可跨 workspace/document 使用；
- 不存在任何写/produce/publish tool；
- 多 Host 使用不同 grant。

### 13.3 文件与恶意内容

- `..`、绝对路径、UNC、ADS、symlink/junction；
- 恶意 Markdown/HTML/frontmatter；
- Prompt injection 文本；
- 超长 query/正则/FTS 特殊字符；
- 索引 stale/watcher overflow；
- 文档在 search 后 read 前改变；
- Vault 被卸载/移动/权限变化。

### 13.4 客户端 E2E

至少在两个真实 MCP Host 上验证：

1. 注册 stdio Server；
2. inspect -> search -> outline -> read；
3. 默认无法读取 raw；
4. provenance grant 读取一个合法 evidence range；
5. Desktop 未启动；
6. Runtime/Server 退出后无常驻进程；
7. Obsidian 修改后返回当前内容；
8. 10k fixture 性能达标。

## 14. 分期

### Phase M0：只读 Core 投影

完成 Workspace Grant、document ID、tree/search/read/backlink application services；先通过 CLI/单元测试验证。

### Phase M1：stdio MCP MVP

暴露 inspect/tree/search/read/outline，完成 schema、限制、错误和真实 Host smoke。

### Phase M2：Provenance Range

增加 evidence token 和最小 Source/Transcript/page/line range 读取；不开放 raw 全库。

### Phase M3：可选 semantic search

只有 lexical 与真实用户反馈表明需要后再做，并维持可重建与隐私边界。

### Phase M4：公共远端知识

由网站控制面部署独立 Streamable HTTP Server；复用只读领域服务，不复用本地授权实现。

## 15. 完成定义

1. Desktop/Engine/网站关闭时，本地 Agent 能通过 stdio 检索和读取已发布知识；
2. 默认 grant 无法读取 raw、Draft、JobStore、Secret 或任意文件；
3. Server 不暴露任何写入/生产/发布工具；
4. 所有结果分页、大小受限、带 content revision；
5. 索引只是派生数据，删除后可重建；
6. Evidence 只通过已授权 PublishedDocument 的 opaque token 按范围读取；
7. 路径穿越、reparse point、恶意 Markdown 和 Prompt injection 测试通过；
8. 至少两个真实 MCP Host E2E 通过；
9. 10k 文档性能 Gate 通过；
10. 公共远端 MCP 与个人本地 Vault 的授权和部署完全隔离。
