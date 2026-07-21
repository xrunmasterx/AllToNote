# AllToNote CLI-First Vault 浏览、Markdown 阅读与搜索设计

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-12-alltonote-llm-iwiki-desktop-design.md
downstream:
  - ../plans/2026-07-18-alltonote-vault-desktop-implementation-plan.md
implementation_status: core-cli-desktop-loop-mostly-not-implemented
last_verified_at: 2026-07-18
```

- 日期：2026-07-13
- 状态：当前有效；实现待闭环
- 对应阶段：`2026-07-12-alltonote-llm-iwiki-desktop-design.md` 的 Phase 2
- 产品基线：开放磁盘合同 + 稳定 iwiki CLI/SDK + 托管式独立 AllToNote Runtime
- 首发平台：Windows 10/11 x64（Tier 1）
- 次级平台：macOS Apple Silicon（Tier 2，通过独立发布 Gate 后支持）

> 局部修正：本文后文如出现把 `jobs.sqlite` 放入 Workspace `.cache` 的未来建议，该建议已被上位架构替代。Job、lease、运行日志和 scheduler 状态属于单机操作状态，必须存放在平台用户状态目录；Vault 只保存可移植知识资产与可重建索引。

## 1. 文档目的

本文细化 AllToNote 下一阶段的只读知识工作区能力：选择本地 Vault、浏览文件树、阅读 Markdown、搜索已发布知识，并建立可以同时服务 Desktop、命令行和未来 Agent/MCP 的共享内核。

本阶段不是给现有视频笔记页面加一个文件选择器，也不是把全部业务塞进 Tauri 可执行文件。它要先建立一条稳定的产品主干：

1. 用户拥有普通的本地 Markdown Vault，知识内容不被 AllToNote 数据库或 UI 锁定。
2. `llm-iwiki` 独立负责工作区合同、校验、查询、索引和后续发布能力。
3. AllToNote Runtime 以共享 Core/SDK 为唯一业务实现，分别向 CLI 和临时 Desktop API 暴露能力。
4. Desktop 只负责轻量交互、目录选择、Runtime 生命周期和结果展示。
5. 即使不安装或不打开 Desktop，用户、脚本和 Agent 仍可通过 `alltonote` CLI 使用同一套能力。

本文只定义 Phase 2 的只读闭环。视频生产流程改造成 Producer、草稿审阅、Publisher、只读 MCP 和更多知识来源，继续遵循总体设计，但不进入本阶段实现范围。

## 2. 已确认的产品边界

### 2.1 本阶段交付能力

本阶段交付以下用户能力：

- 从系统目录选择器打开一个有效的 llm-iwiki Workspace。
- 记住多个最近使用的 Vault，但同一时刻只激活一个。
- 只浏览 `wiki/personal` 和 `wiki/common` 中的正式知识，不展示 `raw/`。
- 按目录懒加载文件树，不在启动时递归扫描全部文档。
- 安全渲染 Markdown，并解析受支持的本地图片、附件和文档链接。
- 按 `personal`、`common` 或 `combined` 范围搜索知识。
- 发现 Obsidian、编辑器或 Agent 对当前 Vault 的外部修改并刷新界面。
- 在没有 Desktop 的情况下，通过独立 CLI 完成工作区检查、校验、浏览、读取和搜索。
- 在 Desktop 启动时发现、安装、校验和托管兼容的独立 Runtime。

### 2.2 明确不做

Phase 2 不包含：

- Markdown 编辑器以及新建、移动、重命名、删除文件。
- `raw/` 来源与草稿浏览。
- 视频 Producer 重构、文章抓取、Publisher 或正式知识写入。
- 自动初始化普通目录、自动创建 manifest 或自动迁移旧 Workspace。
- 自动刷新、重建或写入 QMD 索引。
- 多个同时激活的 Vault、跨 Vault 搜索或跨 Vault 链接解析。
- 反向链接、知识图谱、历史版本、Git 管理、分屏工作区和 Agent 对话。
- 常驻 daemon。
- 把普通 Obsidian Vault 直接视为完整 llm-iwiki Workspace。
- 将个人 Markdown 同步到网站数据库。

### 2.3 成功标准

本阶段完成时必须同时证明：

1. Desktop 能打开有效 Workspace，并在不建立知识正文数据库的前提下浏览和阅读正式知识。
2. 同样的浏览、读取与搜索能力可由 CLI 独立调用，不依赖 Desktop 进程或前端代码。
3. Desktop、CLI 和未来适配器复用同一 Core/SDK，不形成两套业务规则。
4. Obsidian 外部编辑不会被覆盖，变更可在合理时间内反映到阅读界面。
5. 路径越界、symlink/junction 逃逸、恶意 Markdown 和本地 API 未授权访问被阻止。
6. 10,000 篇文档工作区达到规定的启动、浏览、读取和搜索性能基线。
7. 卸载 Desktop 或 Runtime 都不会删除或破坏 Vault。

## 3. 现状与实施基础

### 3.1 AllToNote 当前结构

当前 AllToNote 是 FastAPI + React + 可选 Tauri 的视频笔记应用：

- FastAPI 的业务实现主要位于 `backend/app/`。
- React 的现有 `MarkdownViewer` 与视频任务状态紧密耦合，不能直接作为通用知识阅读器。
- Tauri 当前存在读取完整环境变量、查找任意可执行文件和按环境运行命令的宽泛命令接口。
- `backend/main.py` 面向 Web/扩展场景，默认监听 `0.0.0.0:8483`，CORS 范围也比桌面本地服务需要的范围大。
- `backend/build.sh` 当前把完整后端打成 sidecar，`requirements.txt` 也包含大量视频、AI 和导出依赖。

因此，Phase 2 不应直接扩大现有 Web 后端和 Tauri 权限，而应建立独立的 Runtime 入口与受限 Desktop API 模式。

### 3.2 已有只读集成基础

AllToNote 分支 `codex/iwiki-readonly-client` 的提交 `21f71a7` 已提供 `IWikiClient` 基础能力：

- `inspect`
- `validate`
- `query`
- `index_status`

该实现是 Phase 2 的前置基础，但当前各方法会重复调用 `inspect` 做能力检查。Phase 2 在兼容既有公共方法的同时增加工作区会话级 `IWikiSession`/Gateway 缓存，避免每次浏览或搜索重复启动能力探测进程。

### 3.3 llm-iwiki 提供方基础

llm-iwiki 分支 `codex/iwiki-stable-cli` 的提交 `2b6db85` 已建立：

- manifest/工作区检查与校验。
- 原生查询和索引状态能力。
- Publish Plan 与原子发布能力。
- QMD 索引恢复能力。

Phase 2 只消费其中的 `inspect`、`validate`、`query` 和 `index status`，不调用发布或索引写入命令。AllToNote 不导入 llm-iwiki 内部 Python 模块，也不复制它的 schema 规则。

## 4. 核心架构决策

### 4.1 四层结构

系统分为四个相互解耦的层次：

1. **用户拥有的 Vault**：保存 Markdown、图片、附件和工作区 manifest，是长期知识的唯一事实源。
2. **独立 llm-iwiki**：定义磁盘合同，执行工作区检查、校验、搜索、索引和后续发布。
3. **独立 AllToNote Runtime**：包含共享 Core/SDK、CLI、临时 Desktop API、平台适配器以及后续 Producer。
4. **轻量 AllToNote Desktop**：Tauri + React，只承载 UI、系统目录选择和 Runtime 管理。

依赖方向固定为 Desktop/CLI/其他适配器依赖 AllToNote Core，AllToNote 通过稳定协议依赖 llm-iwiki，llm-iwiki 不反向依赖 AllToNote。Vault 本身不依赖任一应用才能被读取。

### 4.2 托管式独立 Runtime

“托管式独立 Runtime”同时满足低门槛和解耦：

- 在线 Desktop 安装器可以帮助用户安装一份兼容 Runtime。
- 离线组合安装器可以同时携带 Desktop 与 Runtime，适应中国大陆网络和离线环境。
- 组合安装只是安装便利包；安装后 Desktop、Runtime 和 Vault 仍是独立组件，拥有独立目录、版本和卸载生命周期。
- 提供 CLI-only 安装包，服务器、脚本用户和 Agent 环境无需安装 Desktop。
- 基础 Desktop 不内嵌完整 Python 后端、Whisper、FFmpeg、模型权重和全部 Producer 依赖。
- 视频、转录、本地模型等重型能力以后作为按需功能包安装，不拖累只读知识管理的启动与更新。

Phase 2 不引入常驻 daemon。Desktop 打开时启动 `alltonote desktop-api`，Desktop 退出时结束该进程。未来长任务确实需要跨 UI 生命周期运行时，再用独立设计引入可选 daemon。

### 4.3 CLI-first，而非 CLI 包装 UI

Core/SDK 是业务真相，CLI 与 Desktop API 都是适配器：

- CLI 不调用 Desktop API。
- Desktop API 不通过解析 CLI stdout 来调用 Core。
- React 不直接调用系统命令或读取文件。
- Tauri 不实现 Vault 目录规则、Markdown 读取或搜索逻辑。

这保证未来 MCP、Codex、Claude Code、自动化脚本或其他应用可以直接复用 Core/CLI，而不必启动 exe 或模拟 UI。

### 4.4 读路径与查询路径分工

Phase 2 采用明确的混合边界：

- Tauri 只负责让用户选择目录，并把选择结果交给 Runtime。
- Runtime 的本地文件适配器负责安全的目录浏览、Markdown 读取和附件流式读取。
- `iwiki` 负责 `inspect`、`validate`、`query` 和 `index status`。
- Runtime 的 `IWikiGateway` 负责协议、进程、超时、错误和能力协商。

目录和正文读取不必为每次点击启动 iwiki 子进程；工作区语义、兼容性和索引搜索也不会在 AllToNote 中产生第二套实现。

## 5. Runtime 内部组件

### 5.1 共享 Core/SDK

建议在现有 `backend/` 中逐步建立清晰包边界，而不是一次性搬迁全部旧业务：

```text
backend/alltonote/
  core/
  contracts/
  adapters/
  cli/
  desktop_api/
```

Phase 2 的 Core 组件如下：

- `AllToNoteRuntime`：组装依赖并向 CLI/Desktop API 提供统一入口。
- `WorkspaceInspector`：通过 IWikiGateway 检查、校验并打开 Workspace。
- `VaultBrowser`：列目录、读取文档、读取资源并执行路径授权。
- `KnowledgeSearch`：按范围调用 iwiki 查询并规范化结果。
- `WorkspaceCatalog`：维护最近打开记录。
- `RuntimeCapabilities`：报告 Runtime、协议和可选能力。
- `IWikiGateway`：隔离稳定 iwiki CLI 协议。
- `IWikiSession`：在单个已打开 Workspace 生命周期内缓存 inspect/capability 结果。
- `LocalVaultFileAdapter`：执行可取消的磁盘读取与平台路径检查。
- `WorkspaceWatcher`：监听当前工作区的外部变化。
- `DesktopWorkspaceRegistry`：持有当前 Desktop 会话的唯一活动 Workspace。
- `DesktopSessionGuard`：为临时本地 API 提供会话鉴权。

已有 FastAPI 视频接口不需要在本阶段整体迁移。新增 Desktop API 只作为上述 Core 的薄适配器，避免大爆炸式重构。

### 5.2 CLI 适配器

Phase 2 至少提供：

```text
alltonote runtime info --json
alltonote vault inspect --workspace <path> --json
alltonote vault validate --workspace <path> --json
alltonote vault tree --workspace <path> [--path <relative>] [--cursor <opaque>] --json
alltonote vault read --workspace <path> --path <relative> --json
alltonote vault search --workspace <path> --query <text> --scope <personal|common|combined> --json
alltonote desktop-api
```

命令必须支持非交互调用，不能在后台等待终端输入。以后新增 `alltonote produce video`、`produce article` 等命令时，仍复用同一 Core，不把生产能力锁进 Desktop。

### 5.3 Desktop API 适配器

Desktop API 是 Runtime 的临时 loopback 服务，只在 `alltonote desktop-api` 模式注册。普通 Web/Docker 后端不得注册 Vault 路由。

职责只有：

- 把 HTTP 请求转换为 Core 输入。
- 校验 Desktop 会话令牌。
- 处理 ETag、二进制流和 SSE。
- 把稳定领域错误映射成 HTTP 状态与错误载荷。

它不包含第二套目录规则、搜索实现或 Runtime 安装逻辑。

### 5.4 Tauri Runtime Resolver

Tauri 层只保留受限系统能力：

- 打开系统目录选择器。
- 从已注册位置发现 Runtime。
- 在安装/更新/Runtime 变更时调用并验证 `alltonote runtime info --json`。
- 启动 `alltonote desktop-api`，读取一次性握手，并在 Desktop 退出时终止进程。
- 触发受签名和校验约束的 Runtime 安装/更新流程。
- 按白名单打开 Obsidian、系统文件管理器或系统浏览器。

现有“获取全部环境变量”“查找任意可执行文件”“运行任意命令”的通用接口必须删除或收敛成最小白名单，不能成为 Desktop 的隐式插件系统。

### 5.5 React 组件

新增知识工作区页面由以下组件组成：

- `KnowledgeWorkspacePage`
- `VaultSwitcher`
- `KnowledgeTree`
- `SearchPanel`
- `KnowledgeDocumentView`
- `MarkdownRenderer`
- `DocumentOutline`
- `WorkspaceStatus`
- `WorkspaceStore`

现有视频页面的 `MarkdownViewer` 应提取出无任务状态依赖的纯 `MarkdownRenderer`。视频页面与知识阅读页分别用 `VideoNoteView` 和 `KnowledgeDocumentView` 组织自己的业务状态，最终复用同一渲染内核。

## 6. 工作区与 Core 契约

### 6.1 有效 Workspace

用户选择的目录只有满足以下条件才可打开：

- 根目录包含 `.llm-wiki/manifest.yaml`。
- `iwiki inspect` 可以识别其 schema 和能力。
- `iwiki validate` 没有阻断级错误。

普通文件夹不会被自动初始化；旧 schema 不会被自动迁移。校验警告可以允许只读打开，阻断错误必须拒绝打开并给出可操作原因。若 schema 高于当前 Runtime 支持范围，只有 iwiki 明确报告“可安全只读”及相应 capability 时才能降级打开。

### 6.2 Workspace 身份

系统区分三种身份：

- `manifest_workspace_id`：来自 manifest，随 Vault 长期存在的稳定 ID。
- `desktop_workspace_id`：当前 Desktop 会话中的临时句柄，不写入 Vault。
- `runtime_instance_id`：当前 Runtime 进程实例 ID，用于诊断与握手。

HTTP 和 UI 在已打开后使用 `desktop_workspace_id`，避免反复传输绝对路径。CLI 每次命令仍可显式接收 `--workspace`，便于无状态自动化。

### 6.3 打开工作区

Core 的概念入口为：

```python
runtime = AllToNoteRuntime.create_default()
opened = runtime.open_workspace(path)
```

`OpenedWorkspace` 至少包含：

- 三种必要身份中的 manifest 与会话身份。
- 显示名称与规范化根路径的内部引用。
- schema、iwiki CLI protocol 和 capability。
- 校验 warning。
- personal/common 根的可用性。
- 索引状态。
- 是否只读降级及原因。

绝对路径可以用于本机 Desktop 展示与诊断，但不得进入遥测、崩溃报告或网站同步。

### 6.4 文件树

文件树请求使用 Workspace 相对路径：

```text
scope: personal | common
path: 规范化相对目录，根目录为空
limit: 默认 200，最大 1000
cursor: 不透明分页游标
```

返回节点只包含当前一层：

- `kind`: `directory` 或 `document`
- `name`
- `path`
- `scope`
- `has_children`（目录）
- `size`、`mtime`（文档可用时）

默认排序为目录在前、名称按平台一致的自然排序。分页游标不承诺跨目录修改长期稳定；目录发生变化后，客户端应从该目录第一页重新加载。

仅 `.md` 作为文档节点进入第一版树；本地图片和附件通过 Markdown 引用解析读取，不作为主文件树中的知识文档展示。

### 6.5 文档读取

文档读取返回：

- `path`
- `scope`
- `content`
- `content_hash`
- `mtime`
- `size`
- `media_type`（第一版为 UTF-8 Markdown）

只接受 `wiki/personal` 或 `wiki/common` 内的 `.md` 相对路径。非 UTF-8、超出单文档大小上限或读取中持续变化时返回明确错误，不能用空正文伪装成功。

### 6.6 引用与资源解析

资源请求同时携带源文档和引用，Core 按以下顺序解析：

1. 当前文档内锚点。
2. 相对当前文档目录的路径。
3. 相对当前 scope 根或 Workspace 的受支持路径。
4. Obsidian wikilink 的精确路径/文件名匹配。
5. manifest/元数据中的别名和更丰富规则。

Phase 2 首版必须完成前四级；第五级在 provider 明确提供稳定语义后扩展。存在多个匹配时返回 `ambiguous_reference`，不猜测目标。

图片和附件由 Desktop API 以二进制流返回，不放入 JSON base64。每次解析仍执行路径授权，禁止通过编码、绝对路径、`..`、symlink 或 Windows junction 逃出允许根目录。

### 6.7 搜索

搜索输入：

- `query`
- `scope`: `personal`、`common` 或 `combined`
- `limit`: 1 到 100
- provider 支持时使用的不透明游标

`KnowledgeSearch` 把请求交给 iwiki，而不是先查询全部内容再在 AllToNote 侧按 scope 过滤。每条结果至少包含：

- 文档路径与 scope。
- 标题。
- 匹配摘要或片段。
- provider 可用的相关性信息。
- 索引状态和可能过期提示。

索引不可用、超时或失败必须返回搜索错误；文件树和文档阅读仍可正常工作。

## 7. 版本、CLI 与 Desktop API 协议

### 7.1 独立版本维度

以下版本不得混为一个数字：

- Workspace `schema_version`
- iwiki `cli_protocol_version`
- AllToNote `alltonote_cli_protocol_version`
- Desktop API `desktop_api_protocol_version`
- Desktop、Runtime、iwiki 的软件版本

功能是否可用优先依据 capability，不通过软件版本号猜测。

### 7.2 CLI JSON 合同

每个 `--json` 命令在 stdout 只输出一个完整 JSON envelope：

```json
{
  "alltonote_cli_protocol_version": 1,
  "ok": true,
  "command": "vault.read",
  "data": {}
}
```

失败时使用：

```json
{
  "alltonote_cli_protocol_version": 1,
  "ok": false,
  "command": "vault.read",
  "error": {
    "code": "document_not_found",
    "message": "Document does not exist.",
    "retryable": false,
    "details": {}
  }
}
```

约束如下：

- stdout 不混入日志、进度条、颜色控制符或安装提示。
- 诊断和人类可读日志进入 stderr。
- 退出码稳定区分成功、输入/合同错误、运行时错误和取消。
- Ctrl+C 取消返回 130。
- 错误载荷不包含会话令牌、密钥、正文或未经脱敏的绝对路径。

### 7.3 Runtime 启动握手

Desktop 启动 Runtime 时，由子进程 stdout 管道接收一次握手：

```json
{
  "event": "desktop_api_ready",
  "runtime_instance_id": "...",
  "port": 49152,
  "session_token": "...",
  "desktop_api_protocol_version": 1,
  "alltonote_cli_protocol_version": 1,
  "runtime_version": "...",
  "capabilities": ["vault.read", "vault.search", "watch.sse"]
}
```

Runtime 仅监听 `127.0.0.1` 的随机可用端口。令牌只存在于 Desktop 和 Runtime 内存中，通过 `Authorization: Bearer` 传递，不进入 URL、localStorage、日志或最近记录。

正常启动路径直接使用已登记且最近验证过的 Runtime 启动 `desktop-api`，避免每次先额外执行一个 `runtime info` 进程。安装、更新或 Runtime 路径变化时才独立执行 `runtime info`。最终以握手中的完整协议与能力再次校验；不兼容时 Desktop 立即终止该进程，不继续发送请求。

### 7.4 Desktop API

路由前缀为 `/api/desktop/v1`，建议端点如下：

```text
GET    /runtime
GET    /workspaces/recent
POST   /workspaces/open
GET    /workspaces/current
GET    /workspaces/current/tree
GET    /workspaces/current/document
POST   /workspaces/current/resolve-reference
GET    /workspaces/current/asset
GET    /workspaces/current/search
GET    /events
```

协议规则：

- 除健康握手所需入口外全部要求会话令牌。
- CORS 仅允许当前打包 Tauri origin；不能复用现有 Web/浏览器扩展的宽泛 CORS。
- 文档响应支持 `ETag`/`If-None-Match`。
- 附件使用流式响应和正确媒体类型。
- `/events` 使用 SSE 发送规范化 watcher、索引和 Runtime 状态事件。
- API 只在 desktop-api 模式存在；访问普通 Web/Docker 服务时应得到 404，而不是未授权的本地 Vault 能力。

### 7.5 稳定错误类型

Core 至少定义以下可映射错误：

- `runtime_incompatible`
- `iwiki_missing`
- `iwiki_incompatible`
- `workspace_manifest_missing`
- `workspace_schema_unsupported`
- `workspace_validation_failed`
- `workspace_not_open`
- `path_invalid`
- `path_outside_workspace`
- `document_not_found`
- `document_not_utf8`
- `document_too_large`
- `file_changing`
- `ambiguous_reference`
- `index_unavailable`
- `search_timeout`
- `request_cancelled`
- `internal_error`

CLI、HTTP 和 UI 可以使用不同表现形式，但必须保留同一个稳定 `code`，不能靠解析英文 message 判断分支。

## 8. 本地数据与生命周期

### 8.1 长期数据

唯一需要长期保存的知识数据位于 Vault：

- `.llm-wiki/manifest.yaml`
- `wiki/personal/`
- `wiki/common/`
- `raw/`（本阶段不显示但仍属于总体合同）
- 图片、附件与来源证据

AllToNote 不为 Markdown 建立镜像正文数据库。QMD、图谱、缩略图、搜索缓存和后续任务状态均是可删除派生数据。

### 8.2 最近 Vault 记录

`WorkspaceCatalog` 在操作系统用户配置目录保存一个小型 JSON 文件，内容只包括：

- 本机路径。
- 最近打开时间。
- 显示名称。
- manifest workspace ID。
- 可选的最后选中文档等轻量偏好。

写入使用临时文件 + 原子替换，并用进程级文件锁避免两个实例互相覆盖。文件损坏时只丢失最近列表，不影响 Vault；Runtime 应隔离损坏文件并重新创建空目录记录。

### 8.3 UI 与凭据存储

- Desktop 可持久化布局、主题、折叠状态等 UI 偏好。
- 浏览器式存储不得保存绝对 Vault 路径、会话令牌、Markdown 正文或 API Key。
- Windows 密钥放入 Credential Manager 或 DPAPI 保护的应用存储。
- macOS 密钥放入 Keychain。
- Desktop API session token 永不落盘。

### 8.4 索引与未来任务数据库

QMD 缓存由 iwiki 管理；Phase 2 只读取状态和查询，不自动写入、刷新或重建。

未来 Producer 需要可恢复长任务时，可在 `.cache/alltonote/jobs.sqlite` 保存任务状态。该数据库不在 Phase 2 创建，也永远不能成为知识事实源。

### 8.5 安装与卸载

- 卸载 Desktop 默认保留 Runtime、CLI 和 Vault。
- 卸载 Runtime 默认保留 Desktop 设置与 Vault，Desktop 下次启动提示修复 Runtime。
- 卸载 CLI/Runtime 不删除 QMD 之外的任何用户内容；清理派生缓存也必须单独确认。
- Runtime 升级失败应保留或恢复上一个可运行版本，不修改 Vault。

## 9. 并发、文件监听与一致性

### 9.1 不锁定 Vault

Phase 2 不对 Vault 获取全局锁。AllToNote、Obsidian、编辑器和 Agent 可以同时读取，外部工具也可以写入。只读阶段不需要写入排他锁。

### 9.2 稳定读取

为避免读取 Obsidian 正在替换的半个文件，单文档读取采用：

1. 完成路径授权并打开文件。
2. 记录读取前的 size/mtime 或平台等价元数据。
3. 读取内容并计算 hash。
4. 再次读取文件元数据。
5. 若发生变化，短暂让出后重试一次。
6. 再次变化则返回 `file_changing`，保留界面上一份已成功内容并提示稍后重试。

路径授权不能只在打开前做字符串检查。平台适配器需要验证最终文件句柄或最终解析路径仍位于授权根内，防止检查后由 symlink/junction 替换造成竞态逃逸。

### 9.3 单活动工作区与 generation

Runtime 同一 Desktop 会话只维护一个活动 `WorkspaceContext` 和一个 watcher。切换 Vault 时：

- 递增 workspace generation。
- 取消旧请求和 watcher。
- 清空工作区级缓存。
- 打开并验证新 Workspace。
- 只有 generation 匹配的异步结果和事件才能更新状态。

这避免用户快速切换 Vault 时，旧搜索或旧文件事件污染新工作区。

### 9.4 Watcher 语义

Watcher 只监听当前活动 Workspace 的 `wiki/personal`、`wiki/common` 与 manifest；忽略 `.cache`、`.git`、`.obsidian` 和应用临时文件。

原始平台事件经过规范化、去重、防抖和合并后：

1. 先使受影响的目录/文档缓存失效。
2. 再通过 SSE 通知 Desktop。
3. 当前已打开文档发生变化时自动重新读取。
4. 文档删除时进入明确的 deleted 状态，不显示空白成功页。

Watcher 是性能优化，不是事实源。事件溢出、应用从休眠恢复、监听器重启或平台报告不可靠时，执行轻量 reconcile，重新检查当前展开目录和当前文档，而不是全量重建知识数据库。

只读页面自动重载时尽量保留当前标题锚点与滚动位置。若标题结构已变化，回退到最接近的相对滚动位置。

### 9.5 搜索新鲜度

Watcher 观察到本地 Markdown 变化后，Runtime 记录 `local_changes_seen`。因为 Phase 2 不主动写索引，搜索结果需要同时显示 iwiki 的索引状态和 `may_be_stale` 提示。

不能在搜索超时、索引失败或索引缺失时返回“0 条结果”；这会把失败误导为真实空结果。

### 9.6 异步与取消

- FastAPI 事件循环不执行阻塞磁盘读取或同步子进程等待。
- 阻塞任务进入有界 worker，避免大 Vault 或慢盘耗尽线程。
- 任何锁都不能跨磁盘 I/O、子进程等待或网络请求持有。
- 搜索、读取、打开工作区和子进程调用都有独立超时与取消传播。
- Desktop 切换 Vault、关闭窗口或发起新搜索时取消旧请求。

## 10. 错误恢复与安全

### 10.1 用户错误层级

UI 将错误分成四类：

- **阻断错误**：Runtime/iwiki 不兼容、manifest 缺失、schema 不支持、工作区严重无效。显示专用恢复页。
- **降级错误**：索引不可用、watcher 失效、某个 Markdown 扩展渲染失败。文件阅读继续可用。
- **局部错误**：单文件不存在、引用歧义、资源损坏。只影响当前文档或资源。
- **瞬时错误**：文件正在变化、短暂超时。允许明确重试。

### 10.2 Runtime 与 iwiki 恢复

- Runtime 缺失时，Desktop 提供安装或选择已安装 Runtime 的操作。
- Runtime 协议不兼容时，不尝试“凑合运行”，明确提示升级 Desktop 或 Runtime。
- Runtime 意外退出后 Desktop 最多自动重启一次；再次失败则停止重启循环并展示脱敏诊断。
- iwiki 缺失或协议不兼容时，Workspace 不进入伪打开状态。
- iwiki 查询超时时只影响搜索，已打开文档保持可读。
- `IWikiSession` 在 Runtime/iwiki/manifest 变化后失效并重新 inspect，不能永久缓存旧 capability。

### 10.3 重试政策

自动重试仅用于幂等只读操作，并且次数有界：

- 正在变化的本地文件：一次短重试。
- Runtime 崩溃：Desktop 生命周期内一次自动重启。
- 瞬时只读查询：只有 provider 明确表明可安全重试时才进行一次有抖动的重试。

写操作、未来付费 AI 请求、发布操作和未知完成状态的任务不能盲目重试。

### 10.4 文件系统安全

统一 Path Policy 必须：

- 拒绝绝对路径、UNC/设备路径和 `..` 越界。
- 规范化 URL 编码、分隔符、大小写和 Unicode。
- 限制访问到当前 Workspace 的 `wiki/personal` 与 `wiki/common`。
- 对 Windows junction/reparse point 和 macOS symlink 检查最终目标。
- 防御检查与打开之间的路径替换竞态。
- 为单文档与附件设置可解释的大小限制。
- 不允许 HTTP 参数直接指定任意本机绝对路径。

### 10.5 Markdown 安全

`MarkdownRenderer` 默认不执行原始 HTML 脚本，使用明确白名单清洗渲染结果：

- 禁止 `javascript:`、危险 data URL 和事件处理属性。
- 远程图片默认阻止加载并显示占位与显式加载操作，避免泄漏 IP、Referer 和阅读行为。
- 本地资源只能经 Desktop API 的受权解析端点读取。
- Mermaid、KaTeX、代码高亮和单个媒体块隔离错误；一个扩展失败不应让整篇文档空白。
- SVG 和 Mermaid 输出必须再次清洗或在受限环境渲染。
- 外部链接通过系统浏览器打开前展示真实目标并执行协议白名单。

### 10.6 API 与诊断安全

- Desktop API 只监听 loopback 随机端口，拒绝无 token 请求。
- token 不进入 URL、日志、崩溃报告或磁盘。
- 结构化日志包含 runtime instance、操作类型、稳定错误码和耗时，不包含 Markdown 正文、API Key 或未经脱敏的绝对路径。
- 普通 Web/Docker 模式完全不注册本地 Vault 路由。
- Tauri 不保留任意命令执行和完整环境变量读取能力。

## 11. 性能设计

### 11.1 基准范围与目标

10,000 篇 Markdown 是强制基线，50,000 篇是压力观察规模。Windows Tier 1 固定基准机和确定性 fixture 上的 p95 目标：

- Desktop shell 首次可见：不超过 2 秒。
- Desktop API 会话就绪：不超过 2 秒。
- 打开并验证有效 Workspace、首个树节点可操作：不超过 3 秒。
- 返回一个最多 200 节点的目录页：不超过 200 毫秒。
- 普通 Markdown 从点击到可阅读：不超过 300 毫秒。
- 索引 ready 时搜索：不超过 500 毫秒。
- 正常平台事件下外部编辑反映到 UI：不超过 2 秒。

性能验收记录硬件、磁盘类型、冷/热缓存、文档规模和 iwiki/QMD 版本，避免不可复现数字。

### 11.2 启动路径

- React shell 与 Runtime 启动并行，先显示确定的启动状态。
- 正常启动不额外运行一次 `runtime info` 子进程。
- 当前 iwiki `validate` 只做结构性检查，允许与 inspect 同步执行；不得在打开阶段递归解析 10,000 篇文档。
- `IWikiSession` 每个 Workspace 会话只完成一次 inspect/capability 探测，validate/query/status 复用结果。

### 11.3 文件树与阅读器

- 文件树只加载当前展开目录，默认 200 节点分页。
- 不在前端一次保存 10,000 个完整节点或正文。
- Markdown 先渲染 GFM、标题、列表和链接等基础内容，再按需加载 Mermaid、KaTeX、额外语言高亮和图片。
- 代码高亮按语言动态加载，不打包所有语法。
- 图片和附件使用流式响应/Object URL，并在文档切换时释放 Blob。
- Desktop 本地 API 默认不启用 GZip；loopback 上对小 Markdown 压缩通常得不偿失，二进制资源直接流式传输。

### 11.4 搜索

- 输入采用约 200–300ms 的可调防抖，并正确处理中文输入法 composition。
- 新查询取消旧查询，只允许最新 generation 更新 UI。
- 单次搜索只启动一次 provider 查询，不为结果逐条启动进程。
- 搜索结果保留在左侧面板，打开文档不重新发起相同查询。

### 11.5 可观测性

Runtime 记录每个关键阶段的脱敏耗时：

- Runtime spawn 与 handshake。
- iwiki inspect/validate/query。
- 目录枚举。
- 路径授权与稳定读取。
- Markdown 传输与 UI 渲染。
- watcher 事件到 UI 刷新。

只有分阶段指标才能区分慢在进程启动、磁盘、索引还是前端渲染。

## 12. Desktop 交互设计

### 12.1 信息架构

Desktop 的产品主导航调整为：

- `Knowledge`：默认主页，浏览、阅读和搜索 Vault。
- `Produce`：保留现有视频笔记入口，后续承载更多 Producer。
- `Settings`：Runtime、iwiki、模型、转录和应用设置。

现有 Web 版和浏览器扩展入口继续兼容原有视频流程。只有 Desktop 模式显示本地 Vault 管理能力；普通 Web 不显示一个注定无法访问本机磁盘的入口。

建议路由：

```text
/knowledge
/produce/video
/settings
```

### 12.2 无 Vault 状态

未打开 Vault 时显示：

- “选择知识库”主操作。
- 最近 Vault 列表。
- Runtime/iwiki 就绪状态。
- 简短说明：只接受包含 `.llm-wiki/manifest.yaml` 的 Workspace。

此页面不要求先登录。已安装且已授权设备在离线状态下应能浏览本地知识；网站账号不拥有或托管本地 Markdown。

### 12.3 主工作区布局

采用轻量三层布局：

- 最左为窄全局导航栏。
- 左侧边栏在 Files/Search 两个标签间切换。
- 中央为 Markdown 阅读器。
- 右侧可选文档目录，窗口较窄时优先折叠。
- 底部低干扰显示 Runtime、watcher 和索引状态。

不增加 Obsidian 式多 pane、图谱和复杂拖放，保持 Phase 2 的只读主线。

### 12.4 文件树

- 顶层明确显示 `Personal` 与 `Common`，不显示 `raw`。
- 目录按需展开并有加载/失败/重试状态。
- 当前文档高亮，键盘上下移动、左右展开/折叠、Enter 打开。
- 右键菜单只提供复制路径、在 Obsidian 打开、在文件管理器显示等只读操作。
- 不提供新建、重命名、移动和删除，以免产生未经 Publisher 管理的写路径。

### 12.5 搜索

- `Ctrl+K` 聚焦搜索。
- 提供 Personal、Common、Combined 范围切换。
- 搜索结果显示标题、路径、scope 和摘要。
- 打开结果后列表继续保留，便于连续阅读。
- 索引 missing/stale/failed 时显示真实状态，不把失败伪装成空结果。

### 12.6 Markdown 阅读器

阅读器头部显示：

- breadcrumb 与 scope。
- 修改时间。
- 复制相对路径。
- 在 Obsidian 打开。
- 在文件管理器显示。

正文首版支持：

- CommonMark / GFM。
- YAML frontmatter 的折叠摘要。
- 标题锚点和文档目录。
- 相对 Markdown 链接与 Obsidian wikilink。
- 本地图片和附件。
- 代码块与按需语法高亮。
- Mermaid。
- KaTeX。
- 常见 callout。

远程图片默认阻止；引用歧义时展示选择/错误状态，不静默跳转。右侧面板首版只显示当前文档 TOC，不做反向链接、来源历史或知识图谱。

### 12.7 状态与无障碍

- Runtime 正常时不持续弹出状态提示。
- watcher 降级、索引过期等非阻断问题显示低干扰状态条。
- 阻断问题进入专用恢复页面，提供唯一明确下一步。
- 树、搜索结果、阅读器和按钮具备键盘操作、焦点可见性、语义标签和屏幕阅读器名称。
- 中文是首要体验，同时保留现有 i18n 结构，不在组件中硬编码只有中文可理解的控制逻辑。

## 13. 主要风险与已拒绝方案

### 13.1 主要风险

1. **路径安全**：Windows reparse point、junction、大小写和设备路径使简单前缀检查不可靠。必须集中实现并做真实文件系统测试。
2. **版本漂移**：Desktop、Runtime 和 iwiki 独立发布会产生兼容矩阵。通过独立协议版本、capability 和启动握手收敛。
3. **CLI/Desktop 分叉**：若各自实现业务，未来 Agent 会得到不同结果。Core/SDK 必须是唯一业务入口。
4. **旧视频流程回归**：现有 MarkdownViewer、路由和 FastAPI 启动模式与新结构有交叉。采用增量提取和回归 Gate，不做一次性搬迁。
5. **范围膨胀为 Obsidian 克隆**：编辑、图谱、pane、插件系统会掩盖知识生产主线。Phase 2 明确只读非目标。
6. **中国大陆安装环境**：在线依赖和模型下载可能不稳定。必须提供离线组合安装、校验、断点与镜像策略，但保持组件独立。
7. **Watcher 不可靠**：平台会丢事件或批量合并。以磁盘读取和 reconcile 保证正确性。
8. **索引过期误导**：Phase 2 不自动刷新索引，UI 必须呈现新鲜度，不能把 0 结果当成功。
9. **基础 Runtime 变重**：如果继续复用单一 requirements/build，轻量 Desktop 只是表面。Runtime 需要基础包与可选 feature pack 的发布边界。

### 13.2 已拒绝方案

- **单体 Desktop**：把 Core、Python、FFmpeg、Whisper、模型和 UI 全部打入 exe。拒绝原因是安装大、更新耦合、CLI/Agent 无法独立复用。
- **Tauri 直接读取所有文件**：拒绝原因是业务规则进入 UI 壳、难以复用、路径安全与 CLI 行为容易分叉。
- **每次 UI 点击调用一次 CLI**：进程开销高，难以维护活动 Workspace、watcher、SSE 和取消；CLI 是公共适配器，不是 Desktop 内部 RPC。
- **立即引入常驻 daemon**：Phase 2 没有跨 UI 生命周期长任务需求，增加安装、端口、权限和升级复杂度。
- **所有读取都经过 iwiki 子进程**：目录展开和 Markdown 正文读取会产生不必要延迟；iwiki 应拥有语义与索引规则，Runtime 可按开放磁盘合同安全读取。
- **为知识正文建立 SQLite/向量数据库副本**：产生双重事实源和同步问题，违背开放 Vault。
- **把普通 Obsidian Vault 当作第一版输入**：缺少 manifest 与稳定语义，会迫使 AllToNote 猜测目录、scope 和 schema。
- **立即自动迁移/初始化**：本阶段是只读闭环，隐式写盘会放大数据风险和范围。

## 14. 测试与验收设计

### 14.1 Core 单元测试

`WorkspaceInspector`：

- 有效 manifest、警告和阻断校验。
- manifest 缺失、非法、旧/新 schema。
- capability 缺失和只读降级。
- 不自动初始化或迁移。

Path Policy 与 `VaultBrowser`：

- personal/common 合法路径。
- 绝对路径、`..`、编码变体、混合分隔符、UNC 和设备路径。
- Windows 大小写、中文、空格、长路径和不同盘符。
- symlink/junction/reparse point 越界与检查后替换竞态。
- 稳定读取成功、一次重试、持续变化报错。
- 非 UTF-8、超大文件、不存在和删除中读取。
- 目录分页、自然排序、游标失效和 1,000 上限。

引用与资源：

- 锚点、相对链接、Workspace 路径和 wikilink。
- 同名歧义、缺失资源和跨 scope 越界。
- 本地图片/附件媒体类型与流式读取。

`KnowledgeSearch`：

- personal/common/combined 范围原样下推。
- limit 校验、结果规范化、取消和超时。
- missing/stale/failed 状态以及失败不返回空结果。

Catalog/Registry/Watcher：

- 最近记录原子更新、损坏恢复和并发锁。
- 单活动 Workspace、generation 拒绝旧结果。
- watcher 去重、防抖、溢出 reconcile 和当前文档刷新。

### 14.2 iwiki 契约测试

使用真实 `iwiki` 二进制/入口和固定 golden Workspace 验证：

- inspect/validate/query/index status 的 JSON、退出码和 stderr 约束。
- 支持与不支持的 schema/capability。
- scope 查询确实在 provider 内约束。
- 查询超时、进程崩溃、损坏 JSON 和协议版本不兼容。
- `IWikiSession` 在一个 Workspace 会话只 inspect 一次，validate/query/status 复用能力结果。
- manifest、iwiki 路径或 Runtime 变化后 session 正确失效并重新 inspect。

测试不使用 mock 代替全部边界；单元测试可 mock，至少一层集成测试必须运行真实 provider。

### 14.3 CLI 合同测试

- 每个命令的 JSON golden fixture。
- stdout 恰好一个 JSON envelope，日志只在 stderr。
- 稳定错误码与退出码。
- 中文、空格和长路径。
- 非交互环境不会等待 stdin。
- Ctrl+C 传播和退出 130。
- 未安装 Desktop 时，CLI-only 安装环境可完成 inspect/tree/read/search。
- CLI 与 Core 直接调用对同一输入返回语义一致结果。

### 14.4 Desktop API 测试

- 随机 loopback 端口和握手字段。
- 无 token、错误 token、过期进程 token 均被拒绝。
- token 不出现在 URL、访问日志和错误体。
- 普通 Web/Docker 模式下 Vault 路由为 404。
- open/tree/document/reference/asset/search 的 HTTP 映射。
- ETag 与 304。
- 二进制流媒体类型和取消。
- SSE 连接、事件顺序、断线重连和 generation 过滤。
- Runtime 崩溃一次恢复与第二次停止循环。

### 14.5 Tauri 测试

使用受控假 Runtime 进程验证：

- 已登记 Runtime 的正常快速启动。
- 安装/更新后 `runtime info` 校验。
- 握手超时、无效 JSON、协议不兼容、能力缺失和进程提前退出。
- Desktop 退出后子进程清理。
- 目录选择结果只交给 Runtime 打开，不由前端自行扫描。
- 不存在任意命令执行、完整环境变量读取或不受限可执行文件查找接口。
- 白名单 Obsidian/文件管理器打开行为。

### 14.6 React 组件测试

- 无 Vault、最近 Vault、打开中、阻断错误和降级状态。
- 文件树懒加载、分页、键盘导航、切换 scope。
- 中文输入法 composition 下搜索防抖和取消。
- 搜索结果保留与索引过期提示。
- 文档 ETag 刷新、删除、file changing 和滚动/锚点保持。
- frontmatter、wikilink、相对图片、Mermaid、KaTeX、callout 和代码高亮。
- `script`、事件属性、`javascript:`、恶意 SVG/Mermaid 和远程跟踪图片被阻止。
- 窄窗口折叠、焦点顺序和屏幕阅读器语义。
- 现有视频页面在提取纯 MarkdownRenderer 后保持原行为。

### 14.7 真实文件系统集成测试

Windows Tier 1 必须在 NTFS 上运行：

- 中文、空格、长路径、大小写和不同盘符。
- junction、symlink、reparse point 以及目标在授权后被替换。
- Obsidian 常见原子保存、临时文件替换和批量改名事件。
- watcher 溢出、进程休眠/恢复和网络/可移动磁盘断开。
- 杀死 Runtime、iwiki 子进程和读取期间删除文件。

macOS Tier 2 发布前在 APFS 上运行对应 symlink、FSEvents、Keychain、签名进程和 Apple Silicon 端到端测试。

### 14.8 故障注入

至少模拟：

- Runtime/iwiki 缺失、协议不兼容、崩溃和超时。
- manifest 半写、损坏或在会话中变化。
- 目录无权限、磁盘拔出、文件持续变化。
- 索引 missing/stale/failed 与查询损坏输出。
- watcher 丢事件、重复事件和溢出。
- Desktop API token 泄漏探测和恶意本机网页请求。
- Markdown 渲染扩展单块崩溃。

### 14.9 端到端验收场景

1. 只安装 Runtime/CLI，不安装 Desktop；命令行成功检查、浏览、读取和搜索 Vault。
2. Desktop 首次启动发现 Runtime 缺失，通过托管安装完成后打开有效 Vault。
3. 选择普通文件夹，系统明确拒绝且不写入任何文件。
4. 文件树只显示 personal/common；`raw` 不可见也无法通过 API 越权读取。
5. 打开包含本地图片、wikilink、Mermaid 和 KaTeX 的文档并安全渲染。
6. 在 Obsidian 修改当前文档，Desktop 两秒内刷新且不覆盖修改。
7. 外部删除当前文档，UI 显示删除状态而不是空白成功。
8. 索引 ready 时按三个 scope 搜索；索引过期时结果带明确提示。
9. 构造 junction/symlink 越界，CLI 与 Desktop API 都拒绝读取。
10. Runtime 被杀死后自动恢复一次；连续失败后不无限重启。
11. 卸载 Desktop 后 CLI 和 Vault 可继续使用；卸载 Runtime 后 Vault 可被 Obsidian 正常使用。
12. 普通 Web/Docker 后端无法访问本机 Vault 路由。

### 14.10 性能与安装测试

构建确定性 10,000 篇 fixture，固定目录深度、文件大小、中文路径、图片、wikilink、Mermaid 和搜索命中分布；50,000 篇使用同一生成规则进行压力观察。

自动测量第 11 节全部 p95 指标，并保存分阶段耗时。性能测试必须分别记录冷启动、热启动、索引 ready/stale 和 SATA SSD/NVMe 等环境差异。

安装矩阵覆盖：

- Windows 在线 Desktop 安装 + 托管 Runtime。
- Windows CLI-only 安装。
- Windows 离线组合安装，组件独立登记与卸载。
- Runtime 升级、失败回滚、Desktop/Runtime 版本不匹配。
- 中国大陆网络下下载源不可用、断点、校验失败和离线恢复。
- macOS Tier 2 的签名、notarization、Keychain 和卸载保留 Vault。

### 14.11 CI 分层

- 每次提交：Core/CLI/React 单元测试、格式/类型检查、协议 golden tests。
- 每个 PR：真实 iwiki 集成、Desktop API、Tauri 假进程、基础 Windows 文件系统测试、现有视频流程回归。
- Nightly：10,000 篇性能、真实 Windows E2E、watcher/故障注入。
- 发布候选：离线/在线安装、升级/回滚/卸载、恶意路径与 Markdown 安全、50,000 篇压力观察。
- macOS Tier 2 发布分支：独立 APFS、签名、notarization 和 Apple Silicon E2E Gate。

## 15. 分阶段实施顺序

### Phase 2A：Core 与安全文件访问

- 合入/移植 `IWikiClient` 前置基础。
- 建立 Runtime 包边界、WorkspaceInspector、IWikiSession、Path Policy、VaultBrowser、KnowledgeSearch 和 Catalog。
- 完成真实 iwiki 合同测试、路径安全和 10,000 篇基础 fixture。

验收：无需 FastAPI 或 React，Core 测试可以打开、浏览、读取和搜索真实 Workspace。

### Phase 2B：独立 CLI

- 增加 Runtime 打包元数据与 `alltonote` 入口。
- 完成 `runtime info`、`vault inspect/validate/tree/read/search`。
- 固定 JSON envelope、退出码和非交互行为。

验收：一台没有 Desktop 的 Windows 环境可通过 CLI 完成只读闭环。

### Phase 2C：临时 Desktop API 与 Runtime Resolver

- 增加 `desktop-api` 模式、loopback 随机端口、token、握手、SSE 和路由隔离。
- Tauri 实现目录选择与 Runtime 发现/启动/终止。
- 删除或收敛宽泛 Tauri 系统命令。
- 建立托管安装、离线组合安装和兼容检查的最小发布链路。

验收：Desktop 能安全连接独立 Runtime，普通 Web 无 Vault 路由，Runtime 不兼容时 fail closed。

### Phase 2D：知识工作区 UI

- 提取纯 MarkdownRenderer。
- 增加 Knowledge 页面、Vault 切换、懒加载树、搜索、阅读器、TOC 和状态反馈。
- 保留现有视频入口和 Web 行为。

验收：用户能完成选择、浏览、阅读、搜索、外部编辑刷新完整流程。

### Phase 2E：安全、性能与发布 Gate

- 完成真实 NTFS、恶意路径、恶意 Markdown、watcher 故障和 Runtime 崩溃测试。
- 达成 10,000 篇 p95 指标。
- 验证在线、CLI-only、离线组合安装及升级/回滚/卸载。
- 完成旧视频流程回归。

验收：第 14 节 Windows Tier 1 发布 Gate 全部通过。

### macOS Tier 2 Gate

只在共享 Core 和平台接口已稳定后补齐 macOS Runtime/Desktop 打包、Keychain、APFS/FSEvents、签名和 notarization。未通过独立 Gate 前不对外承诺与 Windows 同步首发。

## 16. 完成定义

Phase 2 只有在以下条件同时满足时才算完成：

1. Core/SDK 是 CLI 和 Desktop API 的唯一业务实现。
2. CLI-only 场景在无 Desktop 环境通过端到端测试。
3. Desktop 安装包保持轻量，重型视频/模型依赖不进入基础 UI 包。
4. 有效 Workspace 能打开；普通目录、非法 schema 和阻断校验不会被隐式修改。
5. 文件树、阅读器、引用、本地附件和三个 scope 的搜索工作正常。
6. 路径授权、最终句柄/目标检查、API token 和 Markdown 清洗通过安全测试。
7. Obsidian 外部编辑、删除、watcher 溢出和 Runtime 崩溃具有确定恢复行为。
8. 索引失败不阻断文件阅读，也不被伪装为空搜索结果。
9. 10,000 篇基线达到性能目标，50,000 篇压力结果被记录且无数据损坏。
10. 普通 Web/Docker 模式没有本地 Vault API。
11. 现有视频笔记核心流程通过回归测试。
12. 安装、升级、回滚和卸载不会删除或改变用户 Vault。

## 17. 后续入口

本文通过最终审阅后，下一步只编写可执行实施计划，按照 Phase 2A 至 2E 拆成小任务，并为每个任务指定：目标文件、先写的失败测试、最小实现、验证命令和提交边界。

实施计划不得把以下未来能力偷渡进 Phase 2：Producer 重构、Publisher、知识编辑、常驻 daemon、MCP 写入、跨 Vault 搜索或网站同步。完成只读底座后，再分别设计：

- VideoNoteProducer 与 `raw/personal` Source Bundle。
- 审阅和 Publisher 到 `wiki/personal`，以及显式发布 `wiki/common`。
- 本地只读 MCP，让 Agent 访问已发布知识。
- 视频之外的网页、PDF、Wiki、代码库、Git 和工作日志 Producer。
- 长任务确有跨 Desktop 生命周期需求时的可选 daemon。

这样可以保证知识库继续是独立、开放、可长期复用的底层资产，而 AllToNote 始终只是可替换的生产、管理和交互工具。
