# AllToNote Vault Core、CLI 与薄 Desktop 实施计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-13-alltonote-cli-first-vault-workspace-design.md
  - ../specs/2026-07-12-alltonote-llm-iwiki-desktop-design.md
  - ../specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
downstream:
  - 2026-07-18-alltonote-review-publisher-implementation-plan.md
  - 2026-07-18-alltonote-knowledge-access-mcp-implementation-plan.md
implementation_status: foundation-plan-exists-core-not-complete
last_verified_at: 2026-07-18
```

## 0. 权威和已知修正

先读现有 `2026-07-12-alltonote-iwiki-readonly-client.md`，它只覆盖只读 Client foundation，不等于完整 Vault/Desktop 计划。

必须采用上位修正：JobStore、日志、索引、Desktop session 等机器状态位于 platformdirs machine state，不放入 Workspace `.cache`。Vault 保存开放知识和 iwiki 明确拥有的控制文件。

Desktop 是薄客户端，不复用旧 BiliNote FastAPI/React Pipeline 作为新业务真相。

## 1. 成功标准

- 无 Desktop 时 Core/CLI 能 inspect、validate、tree、read、search；
- 用户可选择/记住多个 Vault，目录移动后可修复；
- 所有读路径受 Workspace Grant、canonical containment 和 iwiki contract 约束；
- 10k 文档树/搜索满足预算；
- Desktop 通过临时 loopback API，选择 Vault、文件树、Markdown 阅读、TOC、搜索和外部编辑刷新；
- 恶意 Markdown/路径/reparse point 不能越权；
- Desktop/Runtime 崩溃不损坏 Vault；
- Windows clean machine E2E，macOS 留出平台接口但不冒充已支持。

## 2. Task VLT-00：现状与 iwiki Capability 锁定

步骤：

1. 检查 `llm-iwiki==0.1.2`、runtime-lock、schema ID/hash；
2. 在真实最小 Workspace 上运行官方 inspect/capability/validate；
3. 列出 AllToNote 允许调用的 SDK/CLI；
4. 禁止读取/写入 iwiki 私有内部实现；
5. 对比已有 `portable_gateway.py` 和 foundation plan；
6. 建立 `backend/tests/fixtures/vault/` 小、中、10k 生成器；
7. 固定性能机和测量方法。

Gate：有明确 capability matrix；任何“假设 iwiki 有某 API”的项先阻塞设计/Provider实现。

## 3. Task VLT-01：IWikiSession/Gateway

目标文件建议：

- `backend/app/core/ports/workspace.py`
- `backend/app/core/application/workspace_service.py`
- `backend/app/adapters/iwiki/workspace_gateway.py`
- 扩展现有 `portable_gateway.py`，不重复 SDK 初始化
- `backend/tests/adapters/test_iwiki_workspace_gateway.py`

步骤：

1. `inspect/open/close/capabilities/validate` typed port；
2. session 固定 workspace identity/contract/capability；
3. 不兼容 fail closed；
4. SDK exception -> Core error；
5. session 不缓存 Secret/无限文件 handle；
6. Workspace 移动后通过 identity/catalog 重新解析；
7. 测试损坏/旧 schema/缺控制文件/read-only media。

## 4. Task VLT-02：WorkspaceCatalog 与 Grant

目标文件：

- `backend/app/core/domain/workspace.py`
- `backend/app/core/application/workspace_catalog_service.py`
- `backend/app/adapters/workspaces/catalog_store.py`
- `backend/app/adapters/workspaces/grant_store.py`

步骤：

1. workspace_id 与当前 path 分离；
2. catalog 保存 display name、last opened、path ref、contract、health；
3. grant 保存 spaces/path prefixes/capabilities/audience；
4. Desktop file picker 创建/更新 grant；
5. path 移动/盘符改变修复；
6. grant revoke/expiry；
7. machine store 原子/权限/脱敏；
8. 不把 catalog/grant 写 Vault。

## 5. Task VLT-03：LocalVaultFileAdapter 安全边界

目标文件：

- `backend/app/adapters/workspaces/local_vault_files.py`
- `backend/app/core/ports/vault_files.py`
- `backend/tests/security/test_vault_paths.py`

先写恶意测试：

- `..`/绝对路径；
- Windows drive/UNC/device/ADS/reserved names；
- symlink/junction/reparse point；
- Unicode normalization/case collision；
- TOCTOU rename；
- file replaced between check/read；
- `.git/.obsidian`/private control paths；
- huge file/binary/invalid UTF-8。

实现最小 read/list/stat/range API；业务层只用 opaque document ID/relative token，不接受任意路径字符串。

Gate：所有 containment/security test 先红后绿；不提供通用 write。

## 6. Task VLT-04：WorkspaceInspector/VaultBrowser

目标文件：

- `backend/app/core/application/vault_browser.py`
- `backend/app/core/domain/vault_nodes.py`
- `backend/tests/core/test_vault_browser.py`

步骤：

1. `inspect` 返回 identity/capability/health/index status；
2. lazy tree，cursor/page/depth 限制；
3. space personal/common/combined 明确；
4. document_id/relative logical path；
5. read 支持 bounded range/heading；
6. Markdown/frontmatter metadata 解析失败可诊断；
7. 不进入 raw/private 区，除非调用服务明确有 grant；
8. 外部修改时 content hash/mtime generation。

## 7. Task VLT-05：Markdown 安全阅读模型

后端：

- 规范化 Markdown 读取结果、resource resolver、outline/link/image tokens；
- local asset 仅在 grant/workspace 内；
- 远程资源默认不自动加载；
- HTML/script/unsafe scheme 标记或移除；
- max bytes/line/heading。

前端后续只渲染受控 AST/Markdown，不直接 `dangerouslySetInnerHTML` 未 sanitize 内容。

测试：恶意 HTML、SVG、iframe、javascript/data/file/UNC 链接、超大嵌套/表格/code、Mermaid 安全策略。

## 8. Task VLT-06：KnowledgeSearch 与派生索引

目标文件：

- `backend/app/core/application/knowledge_search.py`
- `backend/app/core/ports/search_index.py`
- `backend/app/adapters/search/sqlite_fts.py`
- `backend/tests/search/`

步骤：

1. 先实现文件扫描/FTS5 派生索引；
2. index 位于 machine cache/data，不在 Vault；
3. document content hash + generation；
4. lexical query/filters/path/space/heading/snippet；
5. parameterize FTS，限制 query；
6. 增量 upsert/delete；
7. watcher overflow -> dirty/full reconcile；
8. stale index 结果标 generation，read 读当前内容；
9. rebuild/delete index；
10. semantic/hybrid 不在 MVP，保留 capability。

性能：1k/10k/50k fixture 记录 build、增量、top10 p95、峰值内存。

## 9. Task VLT-07：Vault CLI

命令：

```text
alltonote vault add|list|remove|repair
alltonote vault inspect|validate
alltonote vault tree|read|search|index-status|index-rebuild
```

步骤：

1. 只调用 Application Services；
2. 人类/JSON envelope；
3. cursor/bounded output；
4. 默认不显示绝对 path；
5. read 只接受 document ID/logical path token；
6. grant/audience；
7. 错误/退出码；
8. CLI-only E2E。

Gate：没有 FastAPI/React 也可完成选择（通过 add path）、inspect/tree/read/search。

## 10. Task VLT-08：临时 Desktop API

目标文件建议：

- `backend/app/desktop_api/app.py`
- `backend/app/desktop_api/auth.py`
- `backend/app/desktop_api/routes/vault.py`
- `backend/app/desktop_api/routes/search.py`
- `backend/app/desktop_api/events.py`
- tests

步骤：

1. `alltonote desktop-api --port 0 --session-secret-stdin`；
2. loopback only、random port、一次 session secret；
3. Origin/CORS、nonce/version/capability handshake；
4. 父 Desktop liveness/idle shutdown；
5. API 映射同一 Application Services；
6. SSE/event bounded reconnect；
7. 不暴露 arbitrary path/write/shell；
8. 普通网页请求拒绝；
9. crash/restart 后 Desktop 可重连，Job/Vault 不损坏。

是否使用 FastAPI 只是 transport 选择；不得复用旧 `main.py` 全量 API/全局状态作为新 Desktop API。

## 11. Task VLT-09：Tauri Runtime Resolver 与权限

依赖 Runtime plan RCP-10。

目标：

- resolver/managed install status；
- 启动临时 API、secret 不进 argv；
- capability/major handshake；
- session lifecycle；
- Tauri capabilities 最小化；
- WebView 无任意 shell/filesystem；
- missing/incompatible/repair UI state。

测试 malicious runtime path、同名 exe、端口抢占、token/Origin、Runtime crash、中文路径。

## 12. Task VLT-10：Desktop 信息架构与状态层

不要直接把 Vault 功能塞进旧 `HomePage`。建议新结构：

```text
BillNote_frontend/src/features/runtime/
BillNote_frontend/src/features/vault/
BillNote_frontend/src/features/search/
BillNote_frontend/src/features/jobs/
BillNote_frontend/src/features/review/   # 后续
BillNote_frontend/src/services/runtimeApi/
```

状态原则：

- Server/Runtime 是 Job/Vault 真相；
- 前端 store 只缓存 UI 投影；
- workspace switch 清理相关 query/cache；
- 使用 typed generated/manual API client；
- 取消请求不等于 cancel Job；
- error code 映射可行动 UI。

先做 shell/navigation/runtime health/workspace context，不做视觉大改或旧页面全面重构。

## 13. Task VLT-11：Vault 选择与最近列表 UI

流程：

1. OS directory picker；
2. Runtime inspect/validate；
3. 显示 workspace identity/contract/问题；
4. 创建 grant；
5. 保存最近列表；
6. reopen/missing/moved/repair；
7. 不合规目录不能“仍然打开看看”绕过；
8. Obsidian vault 只作为普通目录，不修改 `.obsidian`。

验收：新用户、已有 iwiki、损坏/普通文件夹、网络盘、中文路径、多 Vault。

## 14. Task VLT-12：懒加载文件树

- 根/目录按需分页；
- 10k 节点不一次返回/渲染；
- expand/loading/error/empty；
- keyboard navigation/accessibility；
- selected document 与 URL/state；
- external rename/delete refresh；
- space filter personal/common/combined；
- 不展示私有控制文件。

前后端测量首屏、展开、大目录、连续切换。

## 15. Task VLT-13：Markdown Reader

- 安全 Markdown；
- heading TOC/anchor；
- code/table/callout/footnote；
- local image/resource token；
- remote resource opt-in；
- internal link resolve；
- copy/link/open source；
- scroll/TOC sync；
- large document virtual/分块策略；
- external editor/Obsidian open；
- 不在 MVP 做富文本编辑器。

视觉测试必须使用真实中文长文档、代码、表格、图片；安全测试优先于美化。

## 16. Task VLT-14：Search UI

- debounced lexical search；
- query cancel/stale response；
- scope/path/tag/space filters；
- snippet/highlight 安全；
- results -> document/heading；
- index building/stale/error/rebuild；
- keyboard navigation；
- no-results/large-results；
- 不把 query 自动发远端模型。

## 17. Task VLT-15：Watcher 与外部编辑

步骤：

1. WatchService 只作 invalidation hint；
2. coalesce create/modify/rename/delete；
3. overflow -> full dirty；
4. 自身读取不制造循环；
5. open Obsidian/默认编辑器；
6. 返回应用时验证 current hash；
7. Desktop 未开时索引可在下次启动 reconcile；
8. network/sync drive 降级轮询/手动 refresh；
9. 不监视 Workspace 外路径。

## 18. Task VLT-16：性能、安全、崩溃 Gate

必须测：

- 10k 文档 p95；
- 50k 压力记录；
- 超大单文档；
- watcher storm/overflow；
- Runtime/Desktop kill；
- 磁盘拔出/权限变化；
- malicious Markdown/path/reparse point；
- WebView CSP/loopback auth；
- external editor race；
- index delete/rebuild；
- uninstall/update Vault hash unchanged。

## 19. Task VLT-17：产品 E2E 与验收

Windows clean user：

```text
安装 Runtime + Desktop
 -> 选择真实 iwiki Vault
 -> inspect/validate
 -> 展开 10k 文件树
 -> 阅读 Markdown/图片/TOC
 -> lexical search -> heading
 -> Obsidian 修改
 -> AllToNote 刷新当前内容/索引
 -> Runtime crash/reconnect
 -> 卸载 Desktop/Runtime
 -> Vault hash unchanged
```

CLI-only 同样跑 add/inspect/tree/read/search。输出 `docs/acceptance/vault-desktop-v1.md`。

## 20. 建议执行波次

```text
Wave 1: VLT-00..03   iwiki/identity/grant/path gate
Wave 2: VLT-04..06   browser/read/search Core
Wave 3: VLT-07       CLI-only product gate
Wave 4: VLT-08..10   Desktop transport/runtime shell
Wave 5: VLT-11..15   user experience
Wave 6: VLT-16..17   release/security/performance
```

任何 Wave 都不能通过在 React/Tauri 复制业务逻辑来跳过 Core/CLI。
