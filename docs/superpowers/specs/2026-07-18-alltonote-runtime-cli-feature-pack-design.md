# AllToNote 托管式独立 Runtime、CLI 与 Feature Pack 设计

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-13-alltonote-cli-first-vault-workspace-design.md
  - 2026-07-14-alltonote-portable-artifact-source-bundle-design.md
downstream:
  - ../plans/2026-07-18-alltonote-runtime-cli-feature-pack-implementation-plan.md
  - ../plans/2026-07-18-alltonote-video-release-implementation-plan.md
supersedes:
  - 旧桌面安装包内嵌全部 Python/模型/FFmpeg 并由 UI 拥有业务能力的假设
implementation_status: partial-foundation-video-command-only-multi-recipe-routing-pending
last_verified_at: 2026-07-19
```

## 1. 决策摘要

AllToNote 采用“托管式独立 Runtime”模式：

- `alltonote` CLI、Core/SDK 和 JobStore 属于独立 Runtime；
- Desktop EXE 是薄客户端，只通过版本化协议调用 Runtime；
- 官方一体化安装器可以同时安装 Desktop 与 Runtime，但这只是安装便利，不改变组件所有权；
- Video、Browser、Document、Code/UE5 等重能力以 Feature Pack/Adapter Pack 延迟安装和升级；
- FFmpeg、yt-dlp、Whisper 模型、浏览器、OCR、LibreOffice、代码索引器不进入最小 Desktop 包；
- 无 Desktop 时，用户和 Agent 仍能完成所有生产、查询、审阅和发布操作；
- Video、Article、Document、Codebase 和 Personal 等官方 Recipe 通过同一 ProduceService/Registry 被调用，不各自创建 Runtime 或 CLI Pipeline；
- 无网站、无账号、离线时，本地 Runtime 与 Vault 基础能力仍可工作；
- Engine 的业务触发条件已经成立，但实施仍受 Wave 0-4 阻塞；Wave 0-4 完成前不发布 `--detach`、后台续跑或 Engine 执行面。

这不是“把所有组件都拆成微服务”。Runtime 默认仍是一个本地进程内的模块化单体；只把生命周期、依赖或安全边界明显不同的重工具隔离成外部进程或 Pack。

## 2. 用户目标

### 2.1 普通用户

- 下载一个官方安装器即可开始；
- 第一次使用时能清楚看到 Runtime、必要 Pack、模型和磁盘占用；
- Desktop 关闭后，正在前台运行的 CLI 行为可预测；未来 detach 任务可继续；
- 卸载 Desktop 不删除 Vault；
- Runtime 或 Pack 升级失败可以回滚；
- 中国大陆网络不稳定时可使用离线安装包和镜像。

### 2.2 CLI/Agent 用户

- `alltonote` 是稳定的一等产品接口，不是调试脚本；
- 命令支持结构化 JSON、稳定退出码、取消、等待、重试和幂等键；
- Agent 不需要启动 Desktop，也不需要解析人类日志；
- 能提前探测能力、依赖、Provider、Pack 和预算，而不是运行到一半才发现缺失；
- 任何模型、浏览器 Cookie、API Key 和系统路径都不会被意外输出。

### 2.3 开发者

- Core 业务规则只有一份；
- CLI、Desktop API、MCP 只是调用适配层；
- Pack 可以独立升级和失败隔离；
- 能在没有重依赖的单元测试中验证领域行为；
- 能以真实 Pack contract tests 验证平台能力。

## 3. 非目标

本设计不提供：

- 公共第三方插件市场；
- 任意代码在 Runtime 进程内动态加载；
- 通用 DAG/Workflow 语言；
- 把 Python SDK 暴露为长期跨语言 ABI；
- 跨机器分布式任务集群；
- Desktop 与 Runtime 永久保持后台连接；
- 自动安装任意来源、未签名或未校验的二进制；
- 用配置文件保存 API Key、Cookie 或浏览器 storage state；
- 强制所有用户安装 GPU、Whisper 或 Office 工具。

## 4. 第一性原理与不变量

### 4.1 单一业务内核

```text
CLI ──────────┐
Desktop API ──┼──> ProduceService / Application Services ──> Domain/Core ──> Ports
MCP ──────────┘                 │                                      │
                                └──> RecipeRegistry                 Adapters/Packs
```

任何入口不得重新实现：Recipe 选择、Job 状态机、Checkpoint、Bundle、Quality、Review、Publisher、路径授权或错误分类。

### 4.2 知识与机器状态分离

- Vault 保存可移植 Source Bundle、草稿和正式 Markdown；
- JobStore、日志、缓存、下载中的模型、Pack 安装和 Desktop 会话是机器级状态；
- 机器状态目录不得位于 Vault；
- 用户可以安全删除可重建缓存和索引；
- JobStore 不进入 Dropbox/OneDrive/Git/Obsidian 同步范围。

此条明确替代早期 Vault 文档中“未来 `jobs.sqlite` 可放在 Workspace `.cache`”的局部设想。

### 4.3 便利安装不等于静态耦合

一体化安装器可以同时携带兼容版本，但安装后必须可分别识别：

- Desktop version；
- Runtime version；
- Core contract version；
- CLI API version；
- Desktop API version；
- Portable API/schema version；
- 每个 Pack 的版本、内容 hash、兼容范围和许可证。

### 4.4 Fail closed

出现以下情况时不得“尽力继续”：

- Runtime/Desktop 协议不兼容；
- Pack 签名/hash 不匹配；
- Workspace Grant 不覆盖请求路径；
- Secret backend 不安全或凭据引用不存在；
- Source identity 不确定；
- Job 恢复时 compiler/recipe/prompt/model policy 漂移；
- 外部操作 outcome unknown 且不满足安全重试策略。

## 5. 组件模型

### 5.1 最小 Runtime

最小安装包含：

```text
alltonote executable/launcher
alltonote_core
alltonote_cli
ProduceService + static official Recipe registry
JobStore + config + credential references
Portable/iwiki client contract
文本与 Markdown 基础工具
runtime doctor/capability/pack manager
```

不包含：本地模型权重、Whisper、FFmpeg、yt-dlp、Playwright 浏览器、OCR、Tika、LibreOffice、clangd、UE5 工具。

### 5.2 Feature Pack

Feature Pack 是官方受控、版本化的部署单元，不是无限制插件。第一阶段允许的 Pack：

| Pack | 典型内容 | 加载方式 |
|---|---|---|
| `media-basic` | FFmpeg/ffprobe、yt-dlp、JS runtime/ejs | 子进程 |
| `transcribe-cpu` | faster-whisper CPU runtime，可选模型引用 | 独立 worker/子进程 |
| `transcribe-gpu-*` | 平台/GPU 特定运行库 | 独立 worker |
| `browser-capture` | Playwright runtime/browser 或已安装浏览器桥 | 独立受控进程 |
| `document-basic` | PPTX/PDF 原生结构提取器 | Python adapter 或隔离 worker |
| `document-ocr` | OCRmyPDF/Tesseract/语言包 | 子进程 |
| `document-office` | LibreOffice headless/Tika 等 | 隔离 sidecar |
| `code-basic` | Git/Tree-sitter/通用索引 | 子进程或 adapter |
| `ue5-analysis` | clangd/编译数据库/UE5 专用工作流 | 独立 worker |
| `model-*` | Provider bridge 或本地推理连接器 | adapter/子进程 |

Pack 不得直接写 Vault；只返回已声明的临时产物和结构化结果，由 Core 校验后持久化。

### 5.3 薄 Desktop

Desktop 只拥有：

- Runtime 发现、安装建议、兼容性握手；
- 启动/停止属于当前 Desktop 会话的临时 `desktop-api`；
- Vault 选择与授权 UI；
- 文件树、阅读、搜索、任务、审阅和设置视图；
- OS 原生文件选择、通知、打开外部应用等客户端职责。

Desktop 不拥有：

- Downloader/Transcriber/Compiler；
- Job 状态真相；
- Vault 路径解析规则；
- Provider Secret；
- Review/Publish 规则；
- 任何只在 React/Tauri 中存在的业务流水线。

## 6. 平台目录合同

目录通过 `platformdirs` 等系统惯例解析，不硬编码用户目录。

### 6.1 逻辑目录

```text
config_dir/
  config.toml
  profiles/<name>.toml
  grants.json
  trusted-update-keys.json

data_dir/
  jobs/jobs.sqlite3
  jobs/events/
  packs/installed.json
  packs/<pack-id>/<version>/
  catalog/workspaces.json
  runtime/active.json

cache_dir/
  downloads/
  models/
  extraction/
  indexes/
  updates/

state_dir/
  locks/
  sessions/
  desktop-api/
  engine/                  # 只有 Engine 启用后存在

log_dir/
  runtime/
  jobs/
  audit/
```

Windows 默认映射到合适的 `AppData` 范围；macOS 默认映射到 `~/Library` 对应目录。确切路径只能由 `alltonote runtime paths --json` 返回，不应成为外部合同。

### 6.2 Vault 目录

Vault 由用户选择，路径不在上述机器级目录树中。Runtime 只能通过显式 Workspace Grant 访问。卸载、升级和清缓存不得遍历或删除 Vault。

### 6.3 Secret

Secret 正文存入 OS keyring/credential store；配置和数据库只保存：

```json
{
  "secret_ref": "alltonote/provider/openai/default",
  "kind": "api_key",
  "created_at": "...",
  "last_validated_at": "..."
}
```

允许 CI/临时 Agent 通过环境变量注入 Secret，但：

- 环境变量只在当前进程有效；
- `doctor --json` 只报告 `present/absent/invalid`；
- 日志、异常、Receipt 和 child process argv 不得出现值；
- 若操作系统没有安全 backend，交互式桌面应阻止持久化；CLI 必须要求显式 ephemeral 模式，不自动退化成明文文件。

## 7. 配置合同

### 7.1 优先级

```text
内建安全默认值
  < 用户 config.toml
  < 显式 profile
  < 允许列表内的环境变量
  < CLI flags
```

Secret 值不参与普通配置合并，只通过 `secret_ref` 或 ephemeral injection 解析。

### 7.2 配置分域

```toml
[runtime]
[jobs]
[workspace]
[models]
[recipes.video]
[recipes.web]
[recipes.document]
[packs]
[privacy]
[telemetry]
[updates]
```

Recipe 配置只能设置上位设计允许的策略；不能借配置改变 ID、状态机、Bundle schema、publish safety 或权限边界。

### 7.3 配置快照

创建 Job 时持久化无 Secret 的有效配置快照和稳定 digest。恢复时：

- 与结果相关的编译配置漂移：阻止原 Job 恢复，要求新 Job；
- 仅 UI/日志配置漂移：允许继续；
- 可安全重选的外部 adapter：必须由 Recipe 明确声明；
- Secret 轮换不写入 digest，但凭据身份/Provider endpoint 变化要记录审计。

## 8. Runtime 能力与版本握手

### 8.1 `runtime info`

结构化结果至少包含：

```json
{
  "runtime_version": "0.1.0",
  "core_api_version": 1,
  "cli_api_version": 1,
  "desktop_api_versions": [1],
  "portable_api_version": 1,
  "iwiki_contract": {
    "package_version": "0.1.2",
    "contract_version": 1,
    "schema_id": "...",
    "schema_hash": "..."
  },
  "platform": {"os": "windows", "arch": "x86_64"},
  "engine": {"supported": false, "running": false},
  "packs": [],
  "capabilities": []
}
```

不得返回用户名、Vault 绝对路径、API Key、Cookie、Prompt 或 Provider 原始配置。

### 8.2 capability key

能力使用稳定命名空间：

```text
recipe.video.acquire.youtube
recipe.video.acquire.bilibili
recipe.video.transcribe.local.cpu
recipe.web.capture.http
recipe.web.capture.browser
recipe.document.pdf.native
recipe.document.pdf.ocr
recipe.code.ue5
model.openai-compatible
model.codex-app-server
transport.desktop-api.v1
mcp.knowledge.stdio.v1
```

能力只说明“已安装并通过静态探测”，不保证外部网络、Cookie 或 Provider 当前可用。动态可用性由 `doctor` 或具体 Recipe 的现有诊断路径报告；X0-A 不发布通用 Preflight API。

### 8.3 兼容规则

- Desktop 先检查 Desktop API major；
- Runtime/Core major 不匹配时 fail closed；
- minor 新能力通过 capability negotiation；
- Portable/iwiki schema hash 与锁定文件不一致时禁止 commit；
- Pack 声明 `runtime_api_min/max` 与 Recipe contract version；
- 更新不得通过“忽略版本检查”继续运行生产任务。

## 9. CLI 产品合同

### 9.1 命令层级

当前 Video-first 兼容面和多 Recipe 目标稳定面共同定义如下；具体命令只有在所属 Recipe/子系统通过发布 Gate 后才标为可用：

```text
alltonote version
alltonote runtime info|doctor|paths
alltonote config get|set|validate|profiles
alltonote credential set|status|delete
alltonote pack list|info|doctor|install|update|rollback|remove

# 单一 Production 命令族
alltonote produce video ...
alltonote produce <input> --recipe <id>@<version>
alltonote produce --request <request.json> [--wait|--detach] --json

# Recipe 发现
alltonote recipe list|describe ...

alltonote job get|list|wait|events|cancel|respond|retry
alltonote artifact inspect
alltonote draft inspect
alltonote vault inspect|validate|tree|read|search|index-status
alltonote review ...
alltonote publish ...
alltonote mcp knowledge
alltonote desktop-api
```

`produce web/document/codebase/work-digest` 只在相应 Recipe 设计和真实验收完成后发布。未发布命令不得以空壳或 Fake 结果出现在 capability 中。`produce video` 的现有兼容语义保留，但实现必须迁移到同一 Registry/ProduceService 路由。

#### 9.1.1 命令路由不变量

```text
argv / Desktop DTO / MCP arguments
  -> versioned ProduceRequest
      -> ProduceService
          -> RecipeRegistry -> fixed Recipe
          -> shared Job / Artifact / Quality / Bundle
```

- `produce <kind>` 是用户友好别名，只翻译参数；
- `produce --recipe` 与 `produce --request` 是同一 `produce` 命令族的显式自动化形式；
- 不发布独立 `add` 或 `run` 主命令；
- CLI handler 不得直接依赖 VideoService、DocumentService 等专用 Service；
- Desktop API 和 MCP 不得通过绕过 ProduceService 获得额外 Recipe 语义；
- 当前 RCP-00..07 的 Runtime/CLI 基础已验收，但 RecipeRegistry、ProduceService、generic `produce` 和 `recipe list/describe` 仍未实现，属于 X0-A 待实施能力。

### 9.2 输入与输出

人类模式默认写简洁进度到 stderr、结果到 stdout；`--json` 模式：

- stdout 只输出一个最终 JSON envelope，或 `--jsonl` 输出版本化事件流；
- stderr 只允许可忽略诊断，自动化不得依赖其文本；
- 所有日期为 RFC 3339 UTC；
- ID、枚举、error code 和字段稳定；
- 绝对路径默认脱敏，只有用户显式 `--show-paths` 且本地交互时返回；
- Provider raw、Prompt、Secret 永不返回。

统一 envelope 沿用 Wave 1A 已验收的 Automation Protocol v1，不引入第二套 `api_version` 或退出码：

```json
{
  "alltonote_cli_protocol_version": 1,
  "ok": true,
  "command": "job.get",
  "data": {},
  "error": null,
  "job": null,
  "artifacts": [],
  "capabilities": {},
  "versions": {}
}
```

失败时仍使用同一 envelope；细粒度语义由稳定 `error.code`、`error.category`、`error.retryable` 和 `error.details` 表达。不得修改既有 JSON/Human golden、字段语义或 CLI 协议版本。

### 9.3 退出码

| Code | 含义 |
|---:|---|
| 0 | 成功 |
| 2 | 参数或请求错误 |
| 10 | Workspace、schema 或合同不兼容 |
| 20 | 冲突、前置条件或部分操作失败 |
| 30 | 临时 Runtime、网络或依赖失败 |
| 40 | policy、credential、grant 或 capability 拒绝 |
| 50 | Job 或 Recipe 执行失败 |
| 60 | 结构化取消 |
| 70 | Runtime 内部错误 |
| 130 | 前台 Ctrl+C 中断 |

更细语义只通过稳定 `error.code` 表达，不增加或重编号公共退出码。

### 9.4 前台、等待与未来 detach

- 默认 `produce` 在当前进程创建 durable Job 并等待终态；
- X0-A 的 `ProduceSubmission` 是内部提交接缝，不发布独立 `--submit-only`；
- Engine 已触发，但受 Wave 0-4 阻塞；Wave 0-4 完成且 Engine 验收前不允许 `--detach`、后台续跑或 Engine 接管 lease；
- `job wait` 可被中断，不取消 Job；
- `job cancel` 是显式状态操作；
- Ctrl+C 第一次请求取消当前等待/前台执行，第二次才强制退出；外部操作 outcome unknown 必须被持久化。

### 9.5 幂等与重试

- 可选 `--idempotency-key` 在同一 operation scope 内去重提交；
- 终态 Job 不复活；
- `job retry <id>` 创建新 Job，并记录 `retry_of`；
- 已验证的 immutable Artifact 可复用；
- 外部付费调用只有在幂等键或明确安全策略成立时自动重试；
- CLI 网络超时不等于 Job 失败，调用者必须用 Job ID 查询。

## 10. Pack 合同

### 10.1 Manifest

```json
{
  "manifest_version": 1,
  "pack_id": "media-basic",
  "version": "2026.07.1",
  "platform": "windows-x86_64",
  "runtime_api": {"min": 1, "max": 1},
  "recipe_contracts": {"video": [1]},
  "capabilities": ["recipe.video.acquire.youtube"],
  "entrypoints": [{"name": "yt-dlp", "type": "process", "relative_path": "bin/yt-dlp.exe"}],
  "files": [{"path": "bin/yt-dlp.exe", "sha256": "..."}],
  "licenses": [{"component": "yt-dlp-binary", "spdx": "GPL-3.0-or-later", "file": "licenses/..."}],
  "publisher": "alltonote-official",
  "signature": "..."
}
```

### 10.2 安装与激活

```text
下载到临时目录
 -> 校验 manifest 签名、长度、hash、平台和兼容范围
 -> 解压到不可变版本目录
 -> 静态 probe
 -> 真实最小 smoke（不访问用户 Vault/Secret）
 -> 原子更新 installed.json / active pointer
 -> 保留上一个可回滚版本
```

运行中的 Job 固定 Pack version；更新只影响新 Job。旧版本只有在没有 active Job/lease 引用且超过保留期后才回收。

### 10.3 进程权限

- 工作目录为 Job 临时目录；
- 输入通过显式文件/pipe 传递；
- 输出只允许声明目录；
- argv 不传 Secret；
- 环境变量使用最小 allowlist；
- 有超时、内存/CPU/磁盘预算与 kill tree；
- stdout/stderr 经过大小限制和脱敏；
- Pack 无权直接发布或修改正式 Wiki。

## 11. Desktop Runtime Resolver

发现顺序：

1. 用户显式选择的 Runtime；
2. 当前 Desktop 管理的 Runtime 注册；
3. 系统标准安装位置；
4. `PATH` 中的 `alltonote`；
5. 都不存在时给出安装/修复入口。

每个候选都必须执行无副作用的 `runtime info --json`，验证 publisher、版本和协议。不得只因文件名相同就执行未知程序。

### 11.1 临时 Desktop API

Desktop 启动：

```text
生成一次性 session secret
 -> 启动 alltonote desktop-api --port 0 --session-secret-stdin
 -> Runtime 绑定 127.0.0.1/::1 随机端口
 -> 通过受保护 pipe/启动握手返回端口和证书/nonce
 -> Desktop 做版本/capability 握手
```

约束：

- secret 不出现在 argv、日志或 URL；
- 只接受 loopback；
- Origin/CORS allowlist；
- session 与 Desktop PID/liveness 绑定；
- 空闲与父进程退出后自动停止；
- 普通网页无法调用；
- API 只映射 Application Service，不形成第二条业务 Pipeline。

## 12. 更新、回滚与卸载

### 12.1 独立更新单元

- Desktop；
- Runtime；
- 每个 Feature Pack；
- 模型资产；
- 公共知识包。

它们使用各自版本，但由 compatibility matrix 决定允许组合。组合安装器只选择一组已验证组合。

### 12.2 更新原则

- manifest 与产物必须签名并验证 hash；
- 下载可断点续传，但激活必须原子；
- 更新前运行更新子系统自己的兼容性检查，活动 Job 固定旧版本；该检查不是 Recipe Preflight 公共合同；
- 新版本 smoke 失败自动保持旧 active pointer；
- 数据库迁移前备份且只做向前兼容的可恢复迁移；
- Runtime major 升级不能静默迁移未完成 Job；
- 离线安装包包含完整 manifest、依赖许可证和校验文件。

### 12.3 卸载

默认只删除选中的程序、Pack、缓存和机器状态；必须单独确认才删除 Job 历史/日志。永远不自动删除：

- 用户 Vault；
- Vault 中的 raw/wiki 文件；
- 外部 Obsidian 配置；
- 用户未纳入 AllToNote 管理的模型或工具。

## 13. 安全与隐私

威胁面至少包括：恶意 URL/HTML/PDF、路径穿越、符号链接/reparse point、恶意 Markdown、伪造 Pack、更新劫持、日志泄密、浏览器 Cookie 泄密、本地 loopback 攻击、Prompt injection、Agent 越权和第三方工具供应链。

必要控制：

- 所有外部内容为不可信数据，不成为系统指令；
- HTML sanitize + Desktop CSP；
- Workspace Grant + canonical path/reparse point 检查；
- Pack/更新签名与 hash；
- Secret store 与结构化脱敏；
- 子进程最小环境和输出限制；
- Review/Publisher 是正式知识写入唯一业务入口；
- `common` 发布双重显式确认；
- 默认 telemetry off；如未来启用，只允许用户 opt-in 的匿名性能/错误码，不含正文、URL、路径和模型内容。

## 14. 性能预算

基线目标（普通 Windows x64 SSD，具体验收机型需在计划中固定）：

| 操作 | 目标 |
|---|---|
| `alltonote version` | p95 < 150 ms |
| `runtime info --json` | p95 < 300 ms |
| `job get` warm | p95 < 100 ms |
| `vault tree` 首屏（已有索引） | p95 < 500 ms |
| Desktop 冷启动到可见 shell | p95 < 1.5 s |
| Desktop 到 Runtime 握手完成 | p95 < 2.0 s |
| 未用 Recipe 不加载其重 Pack | 0 次进程/模型加载 |
| Runtime idle（无 Engine） | 0 常驻进程 |

性能目标不能通过绕过验证、跳过 Job 持久化或把所有组件常驻内存实现。

## 15. 可观测性

每个请求/Job 使用：

- `request_id`：一次入口调用；
- `job_id`：业务长任务；
- `operation_id`：外部操作；
- `artifact_id`/`bundle_id`：产物；
- `pack_id@version`：实际执行环境。

结构化事件包含 stage、start/end、duration、attempt、outcome、error code、resource usage 和 cache/reuse 标记。不含正文、Secret、完整 Prompt、Provider raw 或默认绝对路径。

`runtime doctor` 分层报告：

1. Runtime 静态完整性；
2. JobStore/目录权限；
3. iwiki 合同锁；
4. Pack 静态 probe；
5. 可选动态 external probe；
6. Vault grant/health（仅显式指定）；
7. 建议修复动作。

## 16. 测试策略

### 16.1 单元/合同

- 配置优先级和 Secret 隔离；
- JSON envelope/exit code/error taxonomy golden tests；
- 单一 `produce` 命令族归一化、Recipe 发现和未知/重复/不兼容 Recipe fail-closed tests；
- CLI handler 只依赖 ProduceService/通用 DTO、基础命令不 eager import 重型 Recipe Pack 的依赖测试；
- Runtime capability/version negotiation；
- Pack manifest/signature/hash/compatibility；
- active pointer 原子更新和回滚；
- Job 固定 Runtime/Pack 版本；
- Desktop resolver 信任规则；
- loopback token、Origin、父进程退出；
- 日志和错误脱敏。

### 16.2 集成

- clean Windows 用户安装最小 Runtime；
- CLI-only 完成 Video 已缓存/本地输入闭环；
- Video 与至少一个非 Video Recipe 通过同一 ProduceService/Registry/Job/Bundle 完成确定性纵切；
- `produce video` 与 generic `produce --recipe alltonote.video-producer@2` 对等价输入产生相同请求 digest 和提交语义；
- 缺 Pack 时由现有 Video 路径或 `doctor` 返回确定动作；X0-A 不发布通用 Preflight 合同；
- 安装 Pack 后不重装 Desktop 即获得能力；
- Pack 更新失败保留旧版；
- Desktop 管理安装并连接 Runtime；
- Desktop 崩溃后临时 API 退出，JobStore/Artifact 不损坏；
- 卸载后 Vault hash 不变。

### 16.3 发布 Gate

- 完整 SBOM/第三方许可证；
- 签名产物与离线校验；
- 不联网 clean-machine smoke；
- 中国大陆可达镜像 smoke；
- 更新、降级、回滚、卸载；
- 恶意路径/HTML/Pack 测试；
- CLI 兼容 golden corpus；
- 现有 Video/Portable/iwiki 全回归。

## 17. 分期

### Phase R0：合同收敛

冻结 Runtime info、CLI envelope、error/exit code、目录、config/secret、Pack manifest v1。

### Phase R1：CLI 产品化

完成 runtime/job/artifact/draft 命令和结构化输出；前台 Job 可完整自动化。

多 Recipe 路由只通过单一 `produce` 命令族与 `recipe list/describe` 补齐；X0-A 在同一 CLI 组合根建立最小 Request/Submission、`RecipeEndpoint.submit`、静态 Registry、薄 ProduceService 和 Video Adapter，不创建另一个 executable、命令框架或公开 Preflight/Plan/Output/Result/Repository/commit/schema。真实 Document/PPT 再驱动 X0-B。

### Phase R2：Pack 管理基础

先接管现有 media/transcribe 依赖，不建设第三方 SDK；实现静态 probe、固定版本、hash、回滚。

### Phase R3：Desktop 托管

实现 Runtime Resolver、临时 API、组合安装器和修复体验。

### Phase R4：正式分发

完成 Windows 签名/更新/离线/镜像 Gate；macOS 按独立平台计划执行。

### Phase R5：Engine 已触发，受 Wave 0-4 阻塞

Engine 的业务触发门已经成立，但必须等待 Wave 0-4 完成并通过专项验收后才能实施和发布 detach；此前不改变前台 CLI 合同，不暴露 Engine 执行面。

## 18. 完成定义

本子系统实现完成必须同时满足：

1. 没有 Desktop 也能通过稳定 CLI 完成所有已发布能力；
2. Desktop 不包含第二套业务 Pipeline；
3. 最小 Runtime 不携带未使用的重依赖/模型；
4. Pack 可探测、固定、升级、回滚并提供许可证清单；
5. 机器状态与 Vault 完全分离；
6. Secret 不落普通配置、日志、Job、Bundle 或命令行；
7. 版本不兼容时 fail closed；
8. 安装、升级、回滚和卸载不改变 Vault；
9. CLI JSON、退出码和错误合同有兼容测试；
10. Windows clean-machine 与离线验收通过；
11. 所有 Production 命令只调用同一 ProduceService，Video 不再是 Runtime/CLI 通用层的静态所有者；
12. 生产入口只有单一 `produce` 命令族；不发布独立 `add` 或 `run` 主命令；
13. 未安装某个 Recipe Pack 不影响 `version`、`runtime info`、`recipe list` 等基础命令启动，并由 `doctor` 或当前 Recipe 路径明确报告缺失能力；X0-A 不因此公开 Preflight；
14. X0-A 仅包含 Request/Submission、`RecipeEndpoint.submit`、静态 Registry、薄 ProduceService 和 Video Adapter；Document/PPT 驱动 X0-B；Engine 虽已触发但在 Wave 0-4 前保持阻塞。
