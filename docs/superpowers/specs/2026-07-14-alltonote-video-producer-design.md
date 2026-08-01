# AllToNote Video Recipe 架构设计（历史名 Video Producer）

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-14-alltonote-portable-artifact-source-bundle-design.md
downstream:
  - 2026-07-16-alltonote-long-video-knowledge-compilation-design.md
  - ../plans/2026-07-18-alltonote-video-release-implementation-plan.md
implementation_status: core-cli-bundle-mostly-complete-release-matrix-pending
last_verified_at: 2026-07-19
```

- 日期：2026-07-14
- 状态：已确认，当前有效；核心大体实现，发布 Gate 待闭环
- 对应阶段：Portable Artifact 设计中的 Phase P2（Video Note Headless）
- 上位设计：`2026-07-13-alltonote-knowledge-compiler-architecture-design.md`
- 数据合同：`2026-07-14-alltonote-portable-artifact-source-bundle-design.md`
- 相关设计：`2026-07-13-alltonote-cli-first-vault-workspace-design.md`
- 产品基线：Headless-first、本地优先、CLI 一等入口、开放 Markdown、薄 Desktop、托管式独立 Runtime
- 首要平台：Windows 11 x64；macOS Apple Silicon 为低成本第二平台 Gate

## 1. 文档目的

本文定义 AllToNote 第一个正式 Knowledge Compiler Recipe：从视频 URL 或本地视频开始，复用现有下载、字幕、转写、LLM 和截图能力，生产一份可追溯、可审阅、可恢复、可由 iwiki 原子提交的 Portable Source Bundle。

为保持历史链接、实施计划和验收记录稳定，本文文件路径及 `REC-VIDEO-001` ID 不变；规范术语统一为 **Video Recipe**。文中的 “Video Producer” 仅表示该 Recipe 的产品外观或历史名称，不表示它拥有独立 Runtime、CLI、Job、Bundle、Review 或 Publisher。AllToNote 是上层平台，Video、Document、Article、Codebase 和 Personal 是并列官方 Recipe。

它回答：

1. 当前 `NoteGenerator` 中的能力如何按 Source、Transcript、Model、Quality 和 Commit 边界拆开；
2. CLI、旧 FastAPI、未来 Desktop API 和 MCP 如何共享唯一 Application Service；
3. Bilibili、YouTube、Douyin/TikTok、Kuaishou、本地视频和现有转写/模型实现如何接入统一 Port；
4. 视频元数据、Transcript、Evidence、Draft、截图、QualityReport 和 Receipt 如何映射到 Portable Contract；
5. 分钟级至小时级前台任务如何 checkpoint、取消、恢复、处理外部付费调用和避免重复 Bundle；
6. 什么是 P0 的测试、性能、安全和发布 Gate；
7. 哪些内容明确延后，避免把 Desktop、MCP、daemon、Publisher 和后续来源一起塞入首个 Producer。

本文是可直接转化为实施计划的下位架构规格，不包含实现代码。本文没有重新定义 iwiki Workspace、Portable Bundle 或 Publisher；这些协议域继续由各自上位设计和 iwiki 公共合同拥有。

## 2. 规范优先级与本次校正

出现冲突时使用以下优先级：

```text
iwiki 已发布的 Workspace / Portable Contract
  > Knowledge Compiler 总体架构
  > Portable Artifact 与 Source Bundle 设计
  > 本 Video Producer 设计
  > 实施计划
  > 当前旧代码
```

交互设计讨论中有几处术语或状态机描述与已经确认的上位不变量不一致。书面规格按上位合同作以下显式校正，不把冲突静默带入代码：

1. **机器协议使用 Workspace Root。** CLI 的规范参数是 `--workspace`，DTO 字段是 `workspace_root`；产品 UI 可以显示“知识库”或“Vault”，但不能把包含 `raw/` 与 `wiki/` 的 Workspace Root 和 Published Vault 混为同一内部概念。
2. **Job 终态不可复活。** `succeeded`、`failed`、`cancelled` Job 不回到 `running`；非终态 Job 的恢复创建新的 Step Attempt，终态后的再次生产通过 `job retry` 创建带 `retry_of_job_id` 的新 Job。
3. **Attempt 是 Step Attempt。** 它不是“整次 CLI 进程的别名”；每个 Step 的每次具体执行都有独立 Attempt、fencing token 和 `retry_of_attempt_id`。
4. **JobStore 按本机 Workspace 实例隔离。** 不使用一个全局 `runtime.db`；使用 `%LOCALAPPDATA%/AllToNote/workspaces/<local-workspace-instance-id>/jobs.sqlite` 和对应工作目录，避免复制 Workspace 后错误共享任务、锁和幂等绑定。
5. **沿用既有 Automation Protocol。** JSON 顶层使用 `alltonote_cli_protocol_version`，退出码沿用上位设计的 `0/2/10/20/30/40/50/60/70/130`，Job 命令使用既有 `job status/wait/events/cancel/retry/respond` 语义。
6. **最终 Bundle 包含 `commit.json`。** AllToNote candidate 不创建该文件；iwiki 在权威 commit 临界区生成并绑定它。
7. **字幕优先与截图分开。** 默认字幕黄金路径不下载媒体；只有用户明确启用截图且 Draft 产生通过校验的截图请求时，才延迟获取所需媒体并提取帧。

本设计对上位文档中的临时用户别名作一项明确细化：通用机器入口与 Video 用户入口都属于单一 `produce` 命令族；通用自动化使用 `alltonote produce --recipe <selector> --request <request>`，Video 的兼容入口是 `alltonote produce video`。两者只构造同一标准 Recipe Request，不拥有第二套 Pipeline。旧文档中的独立 `run`、`add` 或 `alltonote video` 示例均未发布，不构成兼容承诺。

多 Recipe 公共接缝由 `REC-CONTRACT-001` 细化；本设计继续拥有 Video 的 acquisition、transcript、编译、时间证据、截图和质量语义。二者冲突时，ARCH-001 的平台边界优先；不得为了复用而把 Video 的领域字段提升为通用合同。

## 3. 当前实现事实

### 3.1 当前主流程

现有 `backend/app/services/note.py` 中的 `NoteGenerator` 同时负责：

- 从 `SUPPORT_PLATFORM_MAP` 选择 Downloader；
- 从旧 Provider/Model SQLite 选择 GPT；
- 从旧 Transcriber 配置选择转写器；
- 读取任务缓存文件；
- 优先获取平台字幕；
- 下载音频或视频；
- 调用转写器；
- 调用 LLM 生成 Markdown；
- 从 Markdown marker 提取截图时间并执行 FFmpeg；
- 将 `/static/screenshots/...` 链接写入 Markdown；
- 写状态文件和旧视频任务数据库；
- 返回 `NoteResult`。

这条路径已经证明视频转笔记的产品价值，但存在以下边界问题：

- Core 逻辑依赖 FastAPI、Pydantic Web 类型、旧 DAO 和全局环境；
- 状态文件、缓存、数据库和最终结果没有统一成功线性化点；
- Transcript 同时保存 segment 和 `full_text`，可能形成双真相；
- Markdown、截图和来源没有 Portable Artifact ID、hash、EvidenceRef 和 Receipt；
- LLM 输出 marker 决定内部行为，缺少类型化边界；
- CLI 无法独立运行同一完整流程；
- 恢复语义是读取散落缓存，不是校验后的 Step checkpoint；
- `SUCCESS` 不能证明 Source Bundle 已由 iwiki 原子提交。

### 3.2 当前已注册 Source 能力

当前生产支持映射包含：

- YouTube；
- Bilibili；
- Douyin；
- TikTok（复用 Douyin Downloader）；
- Kuaishou；
- Local Video。

仓库中还存在 `xiaoyuzhoufm_download.py` 等未进入当前 `SUPPORT_PLATFORM_MAP` 的实现。迁移时必须先做能力清单和 Characterization Test：已注册能力进入 P0 支持矩阵；未注册或无调用者的实现不得仅因文件存在就被静默宣传为正式支持，也不得在未调查前删除。

### 3.3 当前 Transcriber 能力

现有工厂和实现至少包含：

- 平台字幕；
- Faster Whisper；
- Groq；
- BCut；
- Kuaishou；
- MLX Whisper。

P0 要求这些能力进入统一 Transcript Port 并通过契约测试，但不要求每个 Transcriber 与每个 Source、每个 LLM 形成实时公网全排列测试。

### 3.4 当前 Knowledge Model 能力

现有模型层至少包含：

- OpenAI-compatible / Universal GPT；
- OpenAI；
- DeepSeek；
- Qwen；
- Codex app-server。

这些实现需要通过 Legacy Adapter 转为统一 Knowledge Model Port。Codex app-server 当前是文本生成 Provider，不等同于未来拥有工具权限的 AgentExecutor；Video Recipe P0 也不授予任何模型 Shell、文件系统或网络工具。

### 3.5 iwiki 前置条件

Video Producer 依赖 iwiki 已发布并由 Runtime 固定兼容组合的公开 SDK 能力：

- `inspect_portable_contract`；
- `validate_bundle`；
- `prepare_bundle_commit`；
- `commit_prepared_bundle`。

AllToNote 只依赖窄 `PortableWorkspacePort` 和公开 DTO，不导入 iwiki 私有模块，不复制 Schema，不自行 rename 最终 Bundle。

## 4. 目标、非目标与成功语义

### 4.1 P0 目标

1. 不启动 Desktop 或网页，只通过 CLI 从视频 URL 或本地视频生成完整 Source Bundle。
2. 所有当前已注册 Downloader、Transcriber 和 Knowledge Model 实现进入统一 Port。
3. Bilibili/YouTube 平台字幕路径和本地视频/Faster Whisper 路径成为两条端到端黄金路径。
4. Transcript 是权威文本证据；Draft 的关键知识可以通过 EvidenceRef 回到视频毫秒区间。
5. 任务具备持久 Job、Step Attempt、事件、取消、checkpoint、reconcile、幂等提交和外部调用不确定性处理。
6. 最终产物只能通过 iwiki 公共 SDK 提交到 Workspace 合同返回的 `raw_personal` Bundle Store。
7. CLI、旧 FastAPI 和未来入口调用同一 Application Facade；不能形成第二条新 Pipeline。
8. 基础 Runtime 命令不加载 Whisper、模型权重、视频处理或 FastAPI 重依赖。
9. 新 Core 不依赖旧 Provider/Model/VideoTask SQLite。
10. Bundle 中的 Markdown 能由 Obsidian、普通编辑器和任意 Agent 直接读取。

### 4.2 非目标

P0 不实现：

- Desktop Producer UI；
- Desktop 文件树、Markdown 阅读和搜索；
- Production MCP；
- daemon、`--detach` 或后台 Engine；
- Publisher、Draft approval 或写入 `wiki/`；
- 自动发布 `common`；
- Article、Wiki、PPT/PDF、UE5、代码库、Git 或 Personal Recipe；
- 通用 Workflow DSL；
- 第三方 Plugin SDK 或动态 Python 插件；
- 任意 URL 抓取器；
- 全视频多模态理解；
- 云端账号、同步、任务队列或凭据同步；
- 一次性删除所有 Legacy 代码。

### 4.3 Job 成功语义

`alltonote produce video` 只有在以下条件全部成立后才成功：

```text
Recipe 声明的输出完整
  -> Artifact content schema 通过
  -> Evidence 与 Draft 引用通过
  -> QualityReport 已生成
  -> bundle.json 与 receipt.json 完整
  -> iwiki semantic validation 通过
  -> iwiki 原子 commit 完成
  -> CommitResult 写入 JobStore
  -> Job 进入 succeeded
```

只生成 Markdown、只写 staging、只通过 LLM 调用或只完成转写都不是成功。

Quality `fail` 与执行失败分开：合法但质量不合格的 Bundle 仍可提交到 Source Store，Job 为 `succeeded`，`quality.overall=fail`、`publish_eligible=false`；后续 Review/Publisher 才阻止正式发布。

## 5. 目标架构

### 5.1 采用新 Core 垂直切片

采用以下路线：

1. 在现有 `backend/` 内建立新 Core/Application/Ports；
2. 用 Adapter 包装现有 Downloader、Transcriber 和 GPT 实现；
3. CLI 直接调用 Application Facade；
4. 旧 FastAPI 在后续迁移里通过 Legacy Request/Result Adapter 调用同一 Facade；
5. Core 通过 `PortableWorkspacePort` 直接调用 iwiki 公共 SDK；
6. 不把 Portable Exporter 追加到 `NoteGenerator` 尾部；
7. 不做一次性文件大搬迁；
8. 不再向 `NoteGenerator` 添加新来源或新生产语义。

拒绝“先让旧 NoteGenerator 跑完，再把 `NoteResult` 包装成 Bundle”的原因：

- 旧流程已经提前写状态、缓存、截图和数据库；
- 无法建立准确 Step checkpoint 和外部操作记录；
- Portable success 只能成为事后导出，而不是唯一成功语义；
- CLI 仍然依赖 Web-era 组件；
- 新来源会继续进入大类，迁移永远无法完成。

### 5.2 建议物理布局

P0 保留 `backend/` 作为 Python Runtime 源码与分发根，不为了命名整齐改成新的顶级 `runtime/`：

```text
backend/
├─ pyproject.toml
├─ app/
│  ├─ core/
│  │  ├─ domain/
│  │  ├─ application/
│  │  ├─ ports/
│  │  ├─ jobs/
│  │  ├─ recipes/
│  │  │  └─ video/
│  │  ├─ quality/
│  │  ├─ portable/
│  │  ├─ config/
│  │  ├─ sdk.py
│  │  └─ errors.py
│  ├─ adapters/
│  │  ├─ sources/
│  │  ├─ transcription/
│  │  ├─ models/
│  │  ├─ screenshots/
│  │  ├─ credentials/
│  │  ├─ jobs/
│  │  └─ iwiki/
│  ├─ cli/
│  ├─ downloaders/       # Legacy implementation，P0 由 Adapter 包装
│  ├─ transcriber/       # Legacy implementation，P0 由 Adapter 包装
│  ├─ gpt/               # Legacy implementation，P0 由 Adapter 包装
│  ├─ routers/           # Legacy HTTP Adapter
│  └─ services/          # NoteGenerator 暂存，停止扩展
└─ tests/
```

这不是承诺一次性创建所有空目录；实施按垂直切片只增加当前任务需要的最小文件。

### 5.3 依赖规则

必须满足：

- Domain 不导入 FastAPI、SQLAlchemy、CLI parser、文件系统、网络 SDK 或 iwiki 实现；
- Application 只依赖 Domain 和 Ports；
- Adapter 可以依赖现有 Legacy 实现，但 Legacy 实现不反向导入 Core；
- CLI 只依赖 Application Facade 和 CLI DTO renderer；
- FastAPI 只负责 Request/Result/Task 兼容映射；
- iwiki Adapter 只依赖公开 SDK；
- Core 不读取旧 `bili_note.db`；
- JobStore、Source Identity Registry 和 Machine Cache 不进入 Portable Bundle；
- 任何最终 Workspace mutation 都经过 iwiki；
- CLI、FastAPI、Desktop API、MCP 不复制 Recipe 分支。
- 通用 Application/Domain/Job Repository 不导入 Video-specific DTO；Video 实现只依赖通用 Recipe/Produce 合同。

### 5.4 Application Facade

目标公共进程内入口是通用 `ProduceService.submit(ProduceRequest)`；Video 的参数由 Video Recipe schema 校验。迁移期间保留窄、类型化兼容用例，但它必须委托同一 ProduceService/Job 路径：

- `SubmitVideoJob`（兼容 facade，不是第二条 Pipeline）；
- `WaitJob`；
- `GetJobStatus`；
- `ListJobs`；
- `CancelJob`；
- `RespondToChallenge`；
- `RetryJob`；
- `StreamJobEvents`；
- `DoctorRuntime`；
- `ListRecipes` / `DescribeRecipe`。

P0 的 Facade 供官方 CLI、FastAPI 兼容层、未来 Desktop API 和 MCP 使用。第三方在 P0 优先使用 CLI Automation Protocol，不承诺所有 Python 内部模块长期稳定。

### 5.5 Core Ports

P0 最小 Port：

| Port | 职责 | 不拥有 |
|---|---|---|
| `VideoSourcePort` | 解析身份、元数据、平台字幕、媒体获取 | Job、LLM、Bundle commit |
| `TranscriptPort` | 把字幕或音频转为标准 Transcript | Draft、Evidence ID、final Workspace 写入 |
| `KnowledgeModelPort` | 从 Transcript 产生类型化 Draft proposal | 可信 ID、文件写入、工具执行 |
| `ScreenshotPort` | 从已验证时间请求提取图片 | LLM、Markdown 结构、commit |
| `CredentialBrokerPort` | 按 Profile 提供最小凭据 | 把 Secret 写入 DTO/日志 |
| `JobRepositoryPort` | Job/Step/Attempt/Event/Challenge/ExternalOperation | Portable knowledge |
| `AttemptStoragePort` | 私有 staging、checkpoint 和中间文件 | 最终 Bundle rename |
| `PortableWorkspacePort` | inspect、validate、prepare、commit | 视频业务和 Prompt |
| `Clock/Id/Event/Cancellation` | 确定性基础能力 | 业务分支 |

机器级 GPU 资源租约属于 Job/执行基础设施，不要求 Source 或 Transcript Domain 感知 SQLite。

### 5.6 官方 Recipe

P0 注册代码定义的：

```text
历史 v1 顶层 Recipe：alltonote.video-course-note@1
当前多 Draft 顶层 Recipe：alltonote.video-producer@2
输出编译绑定：alltonote.video-course-note@2、alltonote.video-faithful-edition@1
```

`alltonote.video-producer@2` 是 `produce video`/通用 Registry 选择的顶层可执行 Recipe；两个输出绑定固定各 Draft 的 compiler identity、request hash 和 provenance，不创建第二个 Job 或 Pipeline。v1 identity 继续兼容读取/恢复，不重写历史 Bundle。

它声明：

- 输入种类：支持的视频 URL 或本地视频；
- Source/Transcript/Model/Screenshot 能力要求；
- 参数 schema；
- Step 顺序与条件；
- output roles；
- Quality Profile；
- 重试、checkpoint、取消和资源策略；
- 支持的平台和 Feature Pack；
- Portable Contract 版本。

P0 不把 Recipe 表达为任意 YAML DAG，不运行远端 Recipe 代码。未来新增 Article、PPT 或 UE5 Recipe 时复用 Job、Artifact、Evidence、Quality 和 Commit，不扩充 `NoteGenerator`。

## 6. 请求、结果与身份

### 6.1 Video Produce Request

规范化请求逻辑字段：

```text
request_schema_version
workspace_root
input
recipe_id + recipe_version
provider_profile
model_override (optional)
transcriber_profile
output_language
quality_preset
style
screenshot_policy
client_request_id (optional)
principal
```

规则：

- `workspace_root` 必须通过 iwiki inspect；
- URL 与本地路径在 Source Adapter 边界区分；
- Secret 只能通过 Profile；
- Recipe、Capability、Model policy 和 Portable Contract 在 Job 创建时固定；
- Job 运行中不能静默切换 Provider、Transcriber 或 Recipe；
- 所有实际生效的安全参数摘要和 hash 写入 Receipt。

### 6.2 成功结果

Application Result 至少包含：

```text
job_id
run_id
bundle_id
manifest_sha256
commit_sha256
workspace_relative_bundle_path
source_id
source_revision_id
primary_draft_artifact_id
transcript_artifact_id
evidence_set_artifact_id
quality_report_artifact_id
display_asset_ids[]
quality.overall
quality.publish_eligible
usage summary
warnings[]
idempotent
```

Step Attempt ID 属于高级诊断与错误引用，不作为普通成功结果的主要产品身份。

### 6.3 Source Identity Registry

Machine State 为每个本机 Workspace 实例维护可重建映射：

```text
(local_workspace_instance_id, connector_id, canonical_identity)
  -> source_id
  -> owning_bundle_id
  -> manifest_sha256
```

规则：

- Registry 是加速和正确关联工具，不是 Portable truth；
- 命中后必须验证目标 committed Bundle 与 manifest hash；
- 丢失时可从 Portable Bundle/iwiki index 重建；
- 无法可靠重建时允许分配新 Source ID，但不能错误链接到另一个旧 Source；
- P0 不做跨 Workspace 全局视频去重；
- Signed CDN URL、短期重定向 URL 和 Cookie 不参与 canonical identity；
- Connector 的 canonicalization 规则由 Capability 版本固定。

### 6.4 SourceRevision 物化策略

- 网络视频默认 `reference_only`：保存稳定来源身份、采集时间、观测 hash/元数据、许可与隐私分类，不默认归档完整原视频；
- 本地视频默认 `external_local`：Portable 层保存逻辑引用和内容 hash，本机绝对路径只保存在 External Local Binding/Machine State；
- 平台字幕或转写 Transcript 作为可携带 Evidence Artifact 保存；
- 明确选择归档媒体且许可允许时才可进入未来 `archived` 策略，P0 不默认实现网络原视频归档。

## 7. Video Recipe 数据流

### 7.1 Stage 0：Preflight

在任何付费或长网络操作前完成：

- Runtime/Feature Pack 兼容；
- 配置 schema；
- Credential Profile 是否存在；
- Workspace inspect 与 Portable capability；
- JobStore 与工作目录可写；
- FFmpeg/Transcriber/Model Adapter 可加载；
- Recipe 参数组合是否支持；
- 截图与 Provider 能力是否冲突；
- 磁盘空间的保守检查。

Preflight 不发送真实 Transcript，不执行付费 LLM 调用。

### 7.2 Stage 1：Resolve Source

输出：

- Connector ID/version；
- canonical identity；
- canonical URI 或本地逻辑引用；
- Source ID；
- Revision ID 预分配；
- 平台视频 ID；
- 初步 title/author/duration/language；
- materialization policy。

平台由 Connector Registry 判断，CLI 不要求普通用户手写 `--platform`。

### 7.3 Stage 2：Acquire

获取顺序：

1. 视频元数据；
2. 平台字幕；
3. 若字幕不可用，获取转写所需音频/媒体；
4. 若字幕可用，默认不下载媒体；
5. 若用户明确启用截图，先完成 Draft 并验证截图请求，再延迟获取最小必要媒体。

平台字幕失败必须区分：

- 确认无字幕，可以回退；
- 瞬时网络失败，可有限重试；
- Credential/授权缺失，进入 PendingChallenge；
- Source 不存在或不支持，确定失败。

不能把所有异常都吞掉后假装“无字幕”并下载媒体。

### 7.4 Stage 3：Normalize Transcript

平台字幕和所有转写器统一输出 `evidence.transcript.v1`：

- NDJSON；
- 第一行 header；
- 后续每行一个 segment；
- 本地 segment ID；
- 整数毫秒半开区间；
- segment 按 start 非递减；
- UTF-8、LF、末尾 LF；
- 不复制另一个权威 `full_text`；
- provider 私有原始响应不进入标准 Transcript。

Transcript 是后续 Draft、Evidence 和截图定位的唯一权威文本时间线。

### 7.5 Stage 4：Create SourceRevision

建立并校验：

- Source 与 SourceRevision；
- 采集/转写内容 hash；
- Source metadata；
- Transcript lineage；
- materialization、license/privacy/freshness；
- 外部本地文件绑定。

### 7.6 Stage 5：Generate Draft Proposal

Knowledge Model 接收：

- 安全的 Recipe instruction；
- 不可信 Source metadata；
- Transcript segment 文本与 ID；
- 目标语言、风格和质量参数；
- 允许的输出 schema。

返回类型化：

```text
GeneratedVideoDraft
  markdown
  citations[]            # 只引用 segment_id
  screenshot_requests[]  # 可选，引用 segment/range
  model_identity
  usage
  warnings[]
```

模型不能：

- 创建 Bundle/Source/Revision/Artifact/Evidence 可信 ID；
- 直接写文件或 Workspace；
- 调用 Shell、文件系统、网络或 Credential；
- 把 Transcript 中的命令视为系统指令；
- 决定 commit 或 publish；
- 返回未经 Core 校验就执行的截图路径或时间。

Core 根据 segment ID、Transcript hash 和已预分配 ID 确定性构建 EvidenceRef 与 Markdown footnote。

### 7.7 Stage 6：Optional Screenshots

P0 默认截图策略为关闭；用户或 Runtime Profile 可以明确开启。

开启后：

1. 只接受引用合法 Transcript segment 的截图请求；
2. Core 验证时间范围并选择确定性时间点；
3. 必要时延迟获取视频媒体；
4. Screenshot Adapter 通过 FFmpeg 提取 WebP；
5. 图片成为 `evidence.asset.v1` Artifact；
6. Draft 使用 Bundle 内相对链接；
7. 不再依赖 `/static/screenshots` HTTP 路径。

P0 不做全视频帧采样、拼图视觉理解或多模态 Agent。

### 7.8 Stage 7：Assemble Candidate Bundle

逻辑布局：

```text
<raw_personal>/.staging/<local-instance-id>/<job-id>.<nonce>/bundle.partial/
├─ bundle.json
├─ receipt.json
├─ sources/
│  └─ video-metadata.json       # source.metadata.video.v1
├─ evidence/
│  ├─ transcript.jsonl          # evidence.transcript.v1
│  └─ evidence-set.jsonl        # evidence.reference-set.v1
├─ drafts/
│  └─ art_<id>.md               # knowledge.draft.markdown.v1
├─ quality/
│  └─ art_<id>.json             # quality.report.v1
└─ assets/
   └─ art_<id>.webp             # evidence.asset.v1，可选
```

`raw_personal` 和 staging 实际位置必须由 iwiki inspect/contract 返回，不能由 AllToNote 硬编码。

Candidate 中不得存在 `commit.json`。iwiki commit 成功后的最终 Bundle 为：

```text
<raw_personal>/bundles/bnd_<id>/
├─ bundle.json
├─ receipt.json
├─ commit.json                  # iwiki 权威 Committer 生成
└─ ...全部已声明 Artifact
```

`bundle.json.outputs` 显式声明：

- `primary_draft`；
- `transcript`；
- `evidence_set`；
- `quality_reports`；
- `source_snapshots` 或 metadata；
- `display_assets`。

CLI、Desktop 和 Publisher 不通过“第一个 Markdown”猜输出。

### 7.9 Stage 8：Quality 与 Portable Validation

确定性 Quality Profile 至少检查：

- Transcript 非空；
- segment 时间合法；
- Draft 可解析且非空；
- 无未替换模板和明显占位符；
- Citation 引用的 segment 全部存在；
- 每个实质性 H2 章节至少有有效 Evidence；
- Evidence locator 和 excerpt/hash 可验证；
- 截图时间、Artifact 和相对路径合法；
- Markdown 无危险 active content；
- Bundle 无 Secret、绝对路径和路径穿越；
- Provenance、Recipe、Capability 和参数摘要完整；
- QualityReport 精确绑定最终 Draft hash。

允许最多一次有边界的 Draft 质量修复。质量修复与网络重试分开计数，不能无限循环。

LLM Judge 不是 P0 强制 Gate；它可以以后作为 `method=model` 的辅助 Quality check，不能替代确定性校验与人工黄金样本。

### 7.10 Stage 9：Prepare 与 Commit

执行顺序继承 Portable Contract：

1. 关闭所有 Candidate Writer；
2. fsync payload、控制文件和 staging 目录；
3. 调用 iwiki `prepare_bundle_commit` 执行完整校验；
4. 获取 JobStore Bundle Commit Guard；
5. 检查 Job、cancel、scheduler/Attempt fencing；
6. 调用 `commit_prepared_bundle`；
7. iwiki 生成 `commit.json` 并同卷原子 rename；
8. 返回 Bundle/manifest/commit hash；
9. JobStore 事务记录 CommitResult 与 Job `succeeded`；
10. 更新可重建 Source Identity Registry。

锁顺序固定：先 JobStore Commit Guard，再由 iwiki 取得 Workspace Portable Mutation Lock。不得反向持锁。

## 8. Checkpoint 与恢复

### 8.1 Checkpoint 完成条件

只有当 Stage 输出：

- 完整写入；
- 文件句柄关闭；
- content schema 通过；
- hash 和 byte length 已记录；
- 输入 fingerprint 匹配；
- 当前 Attempt fencing 和 cancel 仍有效；
- JobStore 原子记录完成；

才是可恢复的 `checkpointed` Artifact。数据库布尔值、存在但未校验的文件或 staging 输出都不能作为完成证据。

### 8.2 主要恢复边界

- Source resolved；
- Metadata/subtitle/media acquired；
- Transcript normalized；
- SourceRevision created；
- Draft proposal received and normalized；
- Screenshots extracted；
- Candidate Bundle assembled；
- Candidate semantic validated；
- CommitResult persisted。

每次恢复重新检查：

- schema version；
- request/input fingerprint；
- 文件存在性；
- byte length/hash；
- content schema；
- 上游 Artifact hash；
- Recipe/Capability/Contract 版本；
- 当前 Workspace identity。

损坏 Checkpoint 从最近有效上游边界重做，不把错误产物继续传递。

### 8.3 恢复计划

RecoveryCoordinator 根据 Artifact 状态计算剩余 Step，而不是只保存一个“执行到第 N 步”的整数。

例如 Transcript 已 checkpointed、Draft 未完成时，只创建新的 Draft Step Attempt；不重新下载和转写。Draft 已 checkpointed、Bundle 未组装时，不重新调用 LLM。

P0 没有 daemon。`produce ... --wait` 通常由当前 CLI 进程获得 scheduler lease 并执行；进程异常退出后，下一次 `job wait <job-id>` 可在 reconcile 后继续尚未终态的 Job。它会为未完成 Step 创建新 Attempt，不会把旧 Attempt 改回 `running`。

### 8.4 Commit 崩溃恢复

如果 iwiki 已完成 rename，但 JobStore 尚未写成功：

- RecoveryCoordinator 以预期 Bundle ID、manifest hash、Receipt/run ID 对账；
- final 合法且 hash 一致时补记 Job `succeeded`；
- 不重新调用 Source、Transcriber 或 LLM；
- 返回 `idempotent=true`；
- Bundle ID 相同但 manifest hash 不同时报告 `bundle_id_conflict`，永不覆盖。

## 9. Job、Attempt、取消和外部调用

### 9.1 继承上位状态机

Job：

```text
queued -> running -> succeeded
                  -> failed
                  -> cancelled
                  -> waiting_for_input -> queued
                                       -> failed
                                       -> cancelled
queued -> cancelled
```

Step Attempt：

```text
pending -> running -> succeeded
                   -> failed
                   -> cancelled
                   -> interrupted
                   -> needs_input
pending -> skipped
        -> cancelled
```

Job 进入终态前，所有未完成 Attempt 必须收敛为终态。旧 Attempt 永不重新变回 `running`。

### 9.2 Machine State

Windows：

```text
%LOCALAPPDATA%/AllToNote/workspaces/<local-workspace-instance-id>/
├─ jobs.sqlite
├─ jobs/<job-id>/
│  ├─ checkpoints/
│  ├─ work/
│  ├─ events.jsonl
│  └─ logs/
├─ locks/
└─ source-identity-cache/
```

macOS：

```text
~/Library/Application Support/AllToNote/workspaces/<local-workspace-instance-id>/
```

SQLite 只保存轻量操作状态：Job、Step、Attempt、Event、Challenge、ExternalOperation、lease、fencing、fingerprint、CommitResult 和幂等绑定。媒体、Transcript、Draft 和大响应放文件工作区。它不是知识数据库，删除后已 committed Bundle 仍完整。

### 9.3 并发与租约

- 同一 Job 同一时刻只有一个有效 scheduler owner；
- 每个 Attempt 使用 fencing token；
- 旧 owner 即使仍运行也不能 checkpoint 或 commit；
- 不同 Job 可以并发；
- 本地 GPU Whisper 默认使用机器级独占资源租约，繁忙时返回稳定 `resource_busy`，避免随机 OOM；
- P0 不建设通用优先级队列和分布式 scheduler。

### 9.4 取消

入口：

```text
Ctrl+C
alltonote job cancel <job-id>
```

取消采用协作式 CancellationToken：

- 每个 Stage、重试、分块、下载回调和子进程边界检查；
- 不再启动新外部工作；
- 受控终止下载、FFmpeg 和转写子进程；
- 保留已经验证的 checkpoint；
- 未提交 Candidate 不进入 final；
- commit guard 与 cancel 条件更新竞争；
- cancel 先获胜则拒绝 commit；
- atomic rename 先完成则 Job `succeeded` 获胜，不能回写 `cancelled`。

Ctrl+C 的当前前台命令退出 `130`。结构化取消完成时 API/Job 语义使用退出码类别 `60`。终态 cancelled Job 如需再次生产，使用 `job retry` 创建新 Job。

### 9.5 自动重试

只在 Adapter 边界对明确瞬时错误有限重试：

- 网络读取最多 3 次；
- 已明确返回可重试失败的 LLM 调用最多 2 次；
- 尊重 `Retry-After`；
- 使用指数退避和抖动；
- Draft 质量修复最多 1 次，单独计数；
- Auth、Policy、输入无效、Source 不存在、Contract invalid 不自动重试；
- 本地转写崩溃不无限重启。

具体值由 Recipe/Capability v1 固定并写入执行摘要，不向 P0 用户暴露大量调参开关。

### 9.6 ExternalOperation

所有可能计费或产生外部副作用的调用在发送前持久化：

```text
external_operation_id
job_id
step_id
attempt_id
provider
request_hash
operation_idempotency_key
provider_request_id (optional)
outcome
safe_effect/cost summary
```

Provider 支持幂等键时复用稳定 operation key。Provider 不支持且进程在 `started` 与持久化响应之间崩溃时，operation 标记 `external_outcome_unknown`，当前 Attempt 终结，Job 进入带 PendingChallenge 的 `waiting_for_input` 或按政策失败。

系统不会静默再次计费。终态 Job 的重试必须提交版本化 retry request，逐项确认原 Job 中所有 unknown operation ID，并使用新的 `client_request_id` 创建新 Job。

### 9.7 Job 提交幂等

幂等范围：Workspace Grant、稳定 principal 和 `client_request_id`。

- 相同 key + 相同 request hash：返回原 Job；
- 相同 key + 不同 request hash：`idempotency_conflict`；
- 原 Job 已 failed/cancelled：普通 submit 仍返回原 Job；
- `job retry` 使用新 key、新 Job，并保留 `retry_of_job_id`；
- 未提供 key 时不承诺 Job submit 去重；
- Job submit 幂等、Step cache、Provider 幂等是三层不同机制。

## 10. Portable Artifact 映射

### 10.1 Source Metadata

`source.metadata.video.v1` JSON 至少表达：

- Connector/platform；
- stable video ID；
- canonical identity/URI；
- title、author/channel；
- duration；
- published_at（可用时）；
- observed_at；
- language；
- subtitle availability/acquisition mode；
- safe source link；
- license/privacy/freshness；
- materialization；
- metadata/extensions。

临时 CDN、Cookie、签名查询参数和本地绝对路径不得进入。

如果 `source.metadata.video.v1` 尚未属于当前 iwiki 核心 Artifact Type 集，Video Recipe v1 必须同时发布其 content schema，并在 `required_contracts` 中声明可验证的 Recipe/Artifact contract；不能只写一个新类型字符串就要求 Reader 猜测内容。

### 10.2 Transcript

使用 Portable Contract 的 `evidence.transcript.v1` NDJSON。Header 绑定 SourceRevision；segment ID 是 Transcript 内局部 ID。Provider raw、重复 `full_text` 和本地临时路径不进入。

### 10.3 EvidenceSet

使用 `evidence.reference-set.v1` NDJSON：

- 第一行 header；
- 后续每行一个 EvidenceRef；
- locator 为 `video-time-range.v1`；
- 使用整数毫秒半开区间；
- target 绑定 Transcript Artifact ID/hash；
- excerpt hash 可验证；
- Evidence ID 由 Core 分配。

### 10.4 Draft

使用 `knowledge.draft.markdown.v1`：

- GFM-compatible Markdown；
- UTF-8、LF；
- 用户可直接用 Obsidian 阅读；
- 关键陈述使用 `[^ev_<id>]` footnote；
- 脚注显示文字不是权威 locator；
- 本地图片只指向 Bundle 内声明的 Asset；
- 修改后进入合同必须创建新 Artifact/Bundle，不能原地改 committed Bundle。

### 10.5 QualityReport

使用 `quality.report.v1`，`profile.id=alltonote.video-course-note`、`profile.version=1`，精确绑定 Draft ID/hash。`overall` 为 `pass`、`pass_with_warnings` 或 `fail`；每个 skipped check 必须说明原因。

### 10.6 Receipt

`receipt.json` 包含：

- `run_id`；
- 可关联的 Job/Step Attempt 摘要；
- Recipe/Capability/Runtime/Contract 版本；
- 安全参数 hash/摘要；
- SourceRevision 和输出 Artifact refs；
- 模型与转写器非敏感标识；
- usage；
- Quality 摘要；
- retry/parent run refs；
- redaction summary；
- started/completed time。

不包含 Secret、Cookie、完整 Prompt、provider raw、完整事件流、PID/lease/fencing、绝对路径或未经证明安全的 provider request ID。

## 11. CLI 与自动化契约

### 11.1 通用与用户入口

通用 Recipe 入口：

```powershell
alltonote produce --recipe alltonote.video-producer@2 `
  --request .\video-request.json `
  --workspace "E:\Agent_Learning\llm-iwiki" `
  --wait `
  --json
```

首选用户入口：

```powershell
alltonote produce video --input "https://www.bilibili.com/video/BV..." `
  --workspace "E:\Agent_Learning\llm-iwiki"
```

本地视频：

```powershell
alltonote produce video --input "D:\Videos\course.mp4" `
  --workspace "E:\Agent_Learning\llm-iwiki"
```

Agent：

```powershell
alltonote produce video --input "https://www.youtube.com/watch?v=..." `
  --workspace "E:\Agent_Learning\llm-iwiki" `
  --client-request-id "codex-task-20260714-001" `
  --wait `
  --json
```

`produce video` 和 generic `produce --recipe/--request` 只翻译参数为同一顶层 Recipe Request；历史 v1 Job/Bundle 继续按原 `alltonote.video-course-note@1` identity 读取和恢复。对等输入下，两种入口的 canonical request、两套 hash、Job identity、Artifact 和错误语义必须一致。当前不发布独立 `add` 或 `run`。

### 11.2 Video 参数

P0 支持：

```text
--workspace <path>
--provider <profile>
--model <override>
--transcriber <profile>
--language <language>
--quality <fast|balanced|high>
--style <official-style>
--screenshots | --no-screenshots
--client-request-id <id>
--wait
--json | --jsonl
--debug
```

规则：

- `--workspace` 可由本机 default workspace 配置补充，但自动化推荐显式传入；
- 不猜当前目录；
- `--transcriber` 是字幕不可用时的转写 Profile，不默认强制跳过平台字幕；
- Secret 不接受命令行参数；
- `--json` 输出单个最终 envelope；
- `--jsonl` 输出同一 Job 的结构化事件并以终态事件结束；
- 二者互斥；
- stdout 不混日志、颜色和提示；
- 诊断与人类进度写 stderr；
- `--debug` 仍必须脱敏。

### 11.3 Job 命令

P0 提供既有协议的最小闭环：

```text
alltonote job status <job-id> --json
alltonote job wait <job-id> --json
alltonote job events <job-id> --jsonl [--follow]
alltonote job cancel <job-id> --json
alltonote job retry <job-id> --request <retry-request.json> --json
alltonote job respond <job-id> --challenge <id> --response <response.json> --json
```

`job events --follow` 以 SQLite JobStore 为权威源，`--after-sequence` 是排他游标，`--limit` 是单次分页上限而非总输出上限；积压必须跨页排空后才进入等待。事件按 Job 内 sequence 递增，每行立即 flush。`succeeded`、`failed`、`cancelled` 的 durable `job.state.v1` 必须是流的最后记录；schema v5 通过覆盖全部可写表的 writer-protocol triggers 阻止迁移前存活的旧 Runtime 在迁移后继续写入并破坏该语义。`waiting_for_input` 不是 Job 终态，但作为非交互观察调用的成功停止边界，调用方读取 challenge 后通过独立 `job respond` 恢复。Ctrl+C 只停止观察并以 Automation Protocol v1 错误对象结束 JSONL，不取消或改写 Job。

可以增加 `job list` 和安全的 `job clean --dry-run` 作为本机管理命令，但不能改变终态/retry 语义，也不能删除 Workspace Bundle。

### 11.4 JSON Envelope

沿用 Automation Protocol：

```json
{
  "alltonote_cli_protocol_version": 1,
  "ok": true,
  "command": "produce video",
  "correlation_id": "corr_...",
  "data": {
    "job_id": "job_...",
    "state": "succeeded",
    "run_id": "run_...",
    "bundle_id": "bnd_...",
    "manifest_sha256": "sha256:...",
    "quality": {
      "overall": "pass_with_warnings",
      "publish_eligible": true
    }
  },
  "warnings": []
}
```

失败包含 stable category/code、safe message、retryable、next_actions、受限 details 和 job/step/attempt refs。自动化根据 code 分支，不解析本地化 message。

### 11.5 Exit Code

| Code | 含义 |
|---:|---|
| 0 | 命令成功 |
| 2 | 参数/请求无效 |
| 10 | Workspace/Schema/Contract 不兼容 |
| 20 | 冲突、前置条件或部分操作失败 |
| 30 | 临时 Runtime/网络/依赖失败 |
| 40 | Policy/Credential/Grant/Capability 不允许 |
| 50 | Job/Recipe 执行失败 |
| 60 | 结构化取消 |
| 70 | 内部错误 |
| 130 | 当前前台命令被 Ctrl+C 中断 |

查询到一个 failed Job 的 `job status` 命令本身退出 0；`produce --wait` 等待的 Job 失败时退出非零。Quality fail 但 Bundle 已合法提交时退出 0。

## 12. Runtime 配置与 CredentialBroker

### 12.1 配置分工

非 Secret 用户配置：

```text
Windows: %APPDATA%/AllToNote/config.toml
macOS:   ~/Library/Application Support/AllToNote/config.toml
```

Machine Job/Cache 状态位于 LocalAppData/Application Support 的 workspace instance 目录；二者不能混为一个全局知识数据库。

配置保存：

- `config_version`；
- default workspace；
- default Provider/Transcriber Profile；
- provider type/base URL/default model/credential ref；
- Whisper model/device/compute type；
- FFmpeg path；
- Recipe 非敏感默认参数；
- 日志和工作目录策略。

配置不保存 API Key、Cookie、OAuth Token 或密码。未知字段报错；更高主版本 fail closed；写入使用锁和原子替换。

### 12.2 优先级

非 Secret：

```text
CLI 显式参数 > 环境变量 > 用户 Runtime 配置 > Recipe v1 默认值
```

Secret：

```text
本次进程环境注入 > OS Credential Store > 明确导入的 Legacy 凭据
```

Windows 使用 Credential Manager；macOS 使用 Keychain。环境变量适合 CI/临时 Agent，不自动写回配置。Cookie 也属于 Credential。

### 12.3 管理命令

最小命令面：

```text
alltonote config path
alltonote config show [--effective]
alltonote config validate
alltonote config set-default-workspace <path>
alltonote credentials set <profile> [--stdin]
alltonote credentials list
alltonote credentials delete <profile>
alltonote config import-legacy --dry-run
alltonote config import-legacy
alltonote doctor [--workspace <path>] [--recipe video] [--json]
```

Legacy import：

- 显式执行；
- 先支持 dry-run；
- 不删除或修改旧 SQLite；
- 非 Secret 写 TOML；
- Secret 写 CredentialBroker；
- 冲突不静默覆盖；
- 输出迁移报告；
- 新 CLI 正常运行不要求旧 DB 存在。

### 12.4 Doctor

默认无付费调用，检查：

- Runtime/平台/配置；
- Workspace 与 iwiki Portable capability；
- JobStore/工作目录；
- FFmpeg；
- Video Feature Pack；
- Source/Transcript/Model Adapter 可加载性；
- 本地模型存在状态；
- Provider Profile 和 Credential 是否存在；
- 未完成或 waiting_for_input Job。

Desktop 将来调用同一个 Doctor，不实现第二套检查。

## 13. Legacy FastAPI 迁移

### 13.1 兼容链路

```text
旧 React
  -> 现有 Router
  -> Legacy Request Adapter
  -> Application Facade
  -> Video Recipe
  -> Portable Bundle
  -> Legacy Result Adapter
  -> 旧轮询/Markdown 响应
```

FastAPI 可以继续使用其进程承载前台/会话绑定 Job，但不能组织第二套 Pipeline。旧 task ID 映射到新 Job ID；结果从 committed Bundle 的 outputs 读取，而不是从旧散落缓存猜测。

### 13.2 Legacy Provider Resolver

过渡期只有 FastAPI 边界可以把旧 `provider_id/model_id` 解析为标准 Provider Profile。Resolver 属于 Legacy Adapter；Core 不导入 DAO。Secret 通过临时 Credential handle/Broker 注入，不进入普通 Request DTO。

### 13.3 Cutover

顺序：

1. 先完成 CLI 两条黄金路径；
2. 增加 Legacy Request/Result/Task Bridge；
3. 使用 Characterization Test 对比；
4. 通过显式部署配置整条路由切换；
5. 不做单请求静默 fallback，避免双下载、双计费、双 Bundle；
6. 正式切换后 Router 不再调用 `NoteGenerator`；
7. 旧编排标记 Legacy，后续单独删除无调用者代码。

P0 不顺便重写整个 React 页面或清理所有旧数据库。

## 14. 测试策略

### 14.1 测试矩阵原则

采用：

- 所有 Adapter 通过 Port Contract；
- 两类黄金路径完整 E2E；
- 不执行 Source × Transcriber × Model 全排列；
- 公网真实测试与确定性 CI 分开。

### 14.2 Domain/Unit

覆盖：

- ID、fingerprint、canonical identity；
- Job/Attempt/PendingChallenge 状态机；
- checkpoint 验证和恢复计划；
- cancellation/commit 竞争；
- client request 幂等和 retry 新 Job；
- ExternalOperation unknown；
- Transcript schema/时间；
- Evidence 映射；
- Draft citation；
- Quality aggregation；
- 配置优先级；
- Secret redaction；
- 路径和 Markdown 安全。

### 14.3 Adapter Contract

Source：Bilibili、YouTube、Douyin/TikTok、Kuaishou、Local；未注册实现先分类再决定支持状态。

Transcript：平台字幕、Faster Whisper、Groq、BCut、Kuaishou、MLX Whisper。

Knowledge Model：Universal/OpenAI-compatible、OpenAI、DeepSeek、Qwen、Codex app-server 和仍有调用者的现有实现。

每个 Contract 测试输入输出、错误归一化、取消、Credential 边界、Secret、无直接 Workspace 写入。

### 14.4 Application Integration

使用真实 Core、JobStore、Checkpoint、Bundle Assembly、Quality、iwiki SDK 与 Fake Source/Transcript/Model，故障注入：

- 每个 Step 前后崩溃；
- Transcript/Draft/Bundle checkpoint 损坏；
- transient retry；
- external outcome unknown；
- quality repair；
- cancel 各阶段；
- concurrent wait/recovery；
- stale lease；
- prepare 前后；
- rename 后 JobStore 前；
- Bundle ID/hash conflict。

### 14.5 确定性 E2E

必须通过 CLI 子进程和临时 iwiki Workspace：

1. Bilibili 固定字幕 Fixture → deterministic Model → Bundle；
2. YouTube 固定字幕 Fixture → deterministic Model → Bundle；
3. 本地短视频 Fixture →测试 Transcript Adapter → Bundle；
4. Windows 发布 Gate：本地短视频 →真实 FFmpeg + Faster Whisper → deterministic/真实受控 Model → Bundle。

Fixture 必须来自自制或可合法分发素材，不把版权不明的完整网络视频提交仓库。

### 14.6 发布前真实 Smoke

单独执行：

- 一个公开 Bilibili 视频；
- 一个公开 YouTube 视频；
- 一个本地视频；
- 至少一个真实 OpenAI-compatible Provider；
- Windows FFmpeg/Faster Whisper；
- 真实临时 iwiki Workspace。

公网 Smoke 失败必须分类为产品、平台、Credential、网络、Provider 或 Fixture 问题；普通 CI 不因公网不稳定失去确定性。

### 14.7 内容质量样本

固定样本至少覆盖：

- 中文字幕技术讲解；
- 标点差的中文口语；
- 英文教程生成中文；
- 本地 Whisper；
- 代码/专有名词；
- 长 Transcript；
- 广告与重复；
- Prompt Injection 文本。

人工评估：事实忠实、核心覆盖、幻觉、结构、压缩、引用正确、可追溯、术语、中文质量、复用性。Bundle 合法不等于内容优秀；新架构如明显弱于旧 NoteGenerator 的固定样本，不算完成。

## 15. 性能、轻量与资源 Gate

### 15.1 基础命令

`alltonote --help`、`version`、`job status/list` 不得导入：

- torch；
- faster-whisper；
- MLX；
- Downloader 重依赖；
- LLM SDK 全集；
- FastAPI；
- 旧 SQLAlchemy Model。

预算：

- `--help`/`version` Windows 暖启动 P95 目标不超过 1 秒；
- 普通 `job status --json` P95 目标不超过 500 ms；
- 基础 CLI RSS 目标约 150 MB 以内；
- 无 daemon 时空闲 CPU/内存为零。

首次实施先测量并记录真实基线；若预算不现实，必须解释依赖和调整依据，不能隐藏。

### 15.2 字幕路径

默认截图关闭且平台字幕可用时：

- 不下载视频/音频；
- 不启动 FFmpeg；
- 不加载 Whisper；
- 不创建媒体临时文件。

显式开启截图时只在 Draft 产生有效请求后延迟获取必要媒体。

### 15.3 本地视频与长 Transcript

- 视频不整体读入内存；
- FFmpeg 使用受控子进程；
- Transcript 流式写 NDJSON；
- 截图按请求时间点提取；
- 默认不复制原始视频进 Bundle；
- 长 Transcript 分块不能形成明显平方级内存增长；
- Chunk checkpoint 允许只重做未完成 Chunk；
- 记录音频时长、转写时长、RTF、设备、模型、compute type；
- 同基准机相对稳定版本退化超过约 15% 必须调查。

### 15.4 依赖分层

Base Runtime 包含 Core、CLI、Job、Config、Credential、Portable/iWiki 和 Doctor，不包含模型权重。Video/Whisper 作为按需 Feature Pack/extra；模型单独下载或复用缓存。Desktop EXE 将来不内嵌完整 Runtime、FFmpeg、Whisper 和模型。

## 16. 安全与隐私

### 16.1 不可信 Source

标题、描述、字幕和视频文本全部是不可信数据。Video Recipe Model：

- 无 Shell；
- 无文件系统工具；
- 无网络工具；
- 无 Credential；
- 无 Workspace write；
- 无 publish；
- 不执行来源中的指令。

未来 Agent Recipe 需要独立 ExecutionGrant 和沙箱设计。

### 16.2 Markdown/路径

拒绝或隔离：

- `<script>`、危险 iframe、`javascript:`；
- `file:`、UNC、绝对路径；
- 未允许的 `data:`；
- Bundle 越界相对路径；
- symlink/junction/reparse point；
- Windows 保留名、大小写冲突、非法 Unicode 路径；
- 未声明文件和悬空图片引用。

Preview 层仍执行 HTML/SVG/Mermaid active content 清理，形成第二道防线。

### 16.3 子进程/URL

- 参数数组调用，不拼 Shell 字符串；
- 默认不使用 `shell=True`；
- 工作目录限制在 Attempt workspace；
- 检查退出码、超时和取消；
- 不因模型文本执行命令；
- 只接受明确支持平台的 HTTP/HTTPS URL 或明确本地文件；
- 拒绝 `file://`、FTP 和任意自定义协议；
- 平台 Connector 校验重定向和最终来源。

### 16.4 Secret 与遥测

- API Key/Cookie/Header 不进 CLI 参数、日志、事件、Bundle、Receipt；
- Debug 仍经过统一 redactor；
- P0 默认无产品遥测；
- 不上传 Workspace 路径、URL、Transcript 或 Draft；
- 用户配置的远端 LLM/转写服务会收到的内容必须可识别并说明。

## 17. 平台范围

### 17.1 Windows Tier 1

必须验证：

- Windows 11 x64、Python 3.11；
- PowerShell；
- 中文/空格/长路径；
- Credential Manager；
- FFmpeg/Faster Whisper；
- SQLite/WAL、文件锁、租约、Ctrl+C；
- 同卷 atomic rename、sharing violation retry；
- iwiki commit 与 crash recovery。

Windows Gate 不通过不能发布 P0。

### 17.2 macOS Tier 2

Core/CLI 不写死 Windows。持续验证 macOS 路径、Keychain、锁、原子写、字幕黄金路径、iwiki commit、Ctrl+C；Apple Silicon 环境执行 Faster Whisper/MLX Smoke。

macOS CLI 如无需显著扩大范围则同步发布；macOS Desktop、Intel Mac 和全部后端等价不属于 P0 承诺。macOS 特有打包问题不阻塞 Windows 主线，但不能把未通过 Gate 的平台宣传为完整支持。

## 18. 实施里程碑

为避免与 Portable 设计的 Phase P0/P1/P2 混淆，本节使用 V0–V7 作为 Video Producer 内部里程碑。

### V0：保护网

- 固定 iwiki SDK/Contract 兼容组合；
- 盘点现有 Adapter；
- 为 NoteGenerator 关键行为补 Characterization Test；
- 建立合法 Fixture 和质量基线；
- 建立 Secret redaction 测试。

Gate：能解释所有当前正式 Adapter 的输入、输出、错误和依赖。

### V1：Fake 垂直切片

- Runtime packaging/CLI；
- Core/Application/Ports；
- JobStore、状态机、Attempt、Event、Checkpoint；
- Config/Credential 接口；
- Fake Source/Transcript/Model；
- 真实 iwiki Gateway。

Gate：CLI-only Fake Recipe 产生并原子提交合法 Bundle，commit crash 可恢复。

### V2：平台字幕黄金路径

- Bilibili/YouTube Source Adapter；
- 字幕标准化；
- SourceRevision/metadata；
- Knowledge Model/Draft/Evidence/Quality；
- Bundle Assembly。

Gate：Bilibili 和 YouTube Fixture 完整通过；默认不下载媒体。

### V3：本地视频黄金路径

- Local Source；
- FFmpeg；
- Faster Whisper；
- 转写 checkpoint；
- 可选截图；
- GPU resource lease；
- cancellation。

Gate：Windows 真实本地视频路径通过，恢复不重复转写。

### V4：其余 Adapter 接线

- Douyin/TikTok、Kuaishou；
- Groq、BCut、Kuaishou、MLX；
- 其余现用 Knowledge Model；
- Codex app-server 文本路径。

Gate：全部通过对应 Port Contract 和必要 Smoke，不做全排列。

### V5：可靠性与自动化

- client request 幂等；
- retry 新 Job；
- PendingChallenge/respond；
- ExternalOperation unknown；
- stale lease/fencing；
- JSON/JSONL；
- Doctor；
- Legacy config import；
- fault injection、安全、性能 Gate。

Gate：Agent 仅用 CLI 即可提交、等待、查询、取消、响应、重试和判断结果。

### V6：FastAPI 兼容切换

- Request/Result/Task Bridge；
- Legacy Provider Resolver；
- 显式 route cutover；
- 禁止双 Pipeline/双写。

Gate：旧页面通过同一 Core 产生 Portable Bundle，Router 不再调用 NoteGenerator。

### V7：发布整理

- Windows 安装/升级/回滚验证；
- Runtime/Video capability/FFmpeg/Credential 文档；
- CLI/JSON/Agent/恢复/取消/错误文档；
- macOS 支持矩阵与已知限制；
- 质量样本报告。

Gate：不理解内部 Python 的外部 Agent 能依据公开协议可靠调用。

## 19. P0 发布 Gate 与 Definition of Done

### 19.1 架构 Gate

- Core 不导入 FastAPI、旧 Router 或旧 SQLAlchemy Model；
- CLI 不调用 `NoteGenerator` 或旧业务 DB；
- Adapter 不直接写 final Bundle；
- 所有 final mutation 经过 iwiki 公共 SDK；
- CLI/FastAPI 不存在两套新 Pipeline；
- LLM 不创建可信 ID 或执行工具；
- JobStore 不成为知识本体；
- 基础 Runtime 不导入重型 Feature Pack。

### 19.2 功能 Gate

- Bilibili 字幕路径通过；
- YouTube 字幕路径通过；
- 本地视频 Faster Whisper 路径通过；
- Metadata、Transcript、Evidence、Draft、Quality、Receipt 完整；
- 可选截图成为 Bundle Asset；
- Bundle 通过 iwiki semantic validation；
- Bundle 原子提交到合同返回的 `raw_personal`；
- 不写 `wiki/` 或 `common`；
- Obsidian/Agent 可直接读取 Draft；
- CLI JSON 返回 Bundle/Artifact 标识。

### 19.3 可靠性 Gate

- Step checkpoint/reconcile；
- cancel/commit 竞争；
- concurrent owner/fencing；
- stale lease；
- idempotency conflict；
- retry 新 Job；
- PendingChallenge；
- external outcome unknown；
- rename 后 JobStore 前崩溃恢复；
- 无半 Bundle、重复 Bundle和无限重试。

### 19.4 质量 Gate

- Citation 全部解析；
- 时间区间合法；
- 实质章节有 Evidence；
- QualityReport hash 精确；
- Prompt Injection 不改变系统行为；
- 无未替换模板；
- Quality fail 准确标识；
- 固定样本无不可接受退化；
- Markdown 在普通渲染器和 Obsidian 可读。

### 19.5 安全 Gate

- Secret/Cookie/Header 不进日志和 Bundle；
- 绝对路径不进 Portable Artifact；
- Markdown active content、路径穿越、子进程注入测试通过；
- CredentialBroker 通过；
- Debug 仍脱敏。

### 19.6 轻量与平台 Gate

- 默认字幕路径不下载媒体/加载 Whisper；
- 基础 CLI 不加载重依赖；
- 视频不整体进内存；
- 长 Transcript 无明显平方级增长；
- Windows 11 x64 全流程、中文路径、Ctrl+C、Credential、FFmpeg、Whisper、iwiki commit 通过；
- macOS 支持范围诚实记录并通过对应 Gate。

### 19.7 完成定义

只有同时满足以下条件，才可宣称 Video Producer P0 完成：

1. 用户无需 Desktop/Web，运行 `alltonote produce video --input <input> --workspace <path>` 可得到 committed Bundle；
2. Bilibili、YouTube、本地视频两类黄金路径成立；
3. 所有正式现有 Adapter 进入统一 Port 并通过 Contract；
4. 产物满足 Portable Contract，并可被 Obsidian、Agent 和普通工具读取；
5. Job 可查询、等待、取消、响应 challenge、从非终态恢复，并可对终态创建审计清晰的新 retry Job；
6. 崩溃不会无故重复下载、转写、付费调用或 commit；
7. CLI Automation Protocol 稳定；
8. Core 与旧 Web/SQLite 解耦；
9. FastAPI 最终通过兼容层共享同一 Application Facade；
10. Windows 发布 Gate 和内容质量基线通过。

## 20. 后续能力如何复用本设计

完成 P0 后：

- Article/Wiki Recipe 复用 Job、ExternalOperation、Draft、Evidence、Quality、Bundle、CLI；只增加 Source/Locator/Recipe；
- PPT/PDF Recipe 增加 document-page/presentation-slide Evidence；
- UE5/Codebase Recipe 增加受控 AgentExecutor、Git/code locator 和 ExecutionGrant，不把 Codex 继续当成万能 GPT；
- Personal/Git Log Recipe 增加相应 Source 与 privacy policy；
- Desktop 通过临时 Desktop API 调 Application Facade；
- Production MCP 通过 stdio Adapter 提交 Job；
- daemon/Engine 只在 `--detach`、多客户端和跨 UI 长任务出现时引入，仍复用相同 Job/Attempt/Recipe；
- Publisher 只消费 committed Draft/Quality/Evidence，通过 iwiki plan/apply 写正式知识。

这些后续能力不能重新发明任务状态、Portable Bundle、Credential、CLI JSON 或 Workspace 写入协议。

在第一个非 Video Recipe 合入前，先完成 `REC-CONTRACT-001` X0-A：建立最小 Request/Submission、RecipeEndpoint、静态 Registry、薄 ProduceService、Video Adapter 及 SDK/Runtime/单一 produce 路由；X0-A 不清除 Job/Repository 数据面的 Video-specific 类型。随后由 Video 与真实 Document/PPT 第二消费者共同完成 X0-B，抽取 Result/Artifact/Repository/atomic commit、migration 和 generic reconnect，并验证 `produce video --input` 与 generic `produce --recipe/--request` 的 canonical request、两套 hash、Job identity、result 和 Portable 等价。该 Gate 不反向阻止 Video P0 独立发布，但阻止把 Video 专用接缝复制到第二个 Recipe。

## 21. 最终架构决策摘要

1. Video Recipe 是 Knowledge Compiler 的首个正式垂直 Recipe，不是 NoteGenerator 的导出补丁，也不是 AllToNote 平台本身。
2. Core/Application 是唯一业务语义；CLI、FastAPI、Desktop、MCP 都是 Adapter。
3. P0 前台 CLI + 持久 Job/Step Attempt；daemon 延后。
4. Job 终态不可复活；终态 retry 创建新 Job。
5. Machine State 按本机 Workspace 实例隔离，不进入知识资产。
6. Transcript 是唯一权威文本时间线；Draft 通过 EvidenceRef 引用毫秒区间。
7. LLM 返回类型化 proposal，不能创建可信 ID、执行工具或写 Workspace。
8. 默认平台字幕路径不下载媒体；截图是显式、延迟、可选阶段。
9. Quality fail 与执行失败分开，合法失败质量 Bundle 仍可审计。
10. Candidate 由 AllToNote 构建，最终校验与原子 commit 由 iwiki 公共 SDK 拥有。
11. CLI 规范使用 `--workspace`；`produce video` 只是标准 Recipe 的用户入口。
12. Runtime 配置独立，Secret/Cookie 只通过 CredentialBroker。
13. 所有现有 Adapter 接线并做契约测试；Bilibili/YouTube 字幕和本地 Whisper 是发布黄金路径。
14. Windows 是 Tier 1；macOS 保持低成本、诚实的 Tier 2。
15. FastAPI 迁移后不得保留第二条视频生产 Pipeline。
16. MCP、Desktop Producer、Publisher、daemon 和后续来源全部在稳定 CLI/Core/Portable 契约之上继续演进。
17. `SubmitVideoJob`/`submit_video` 只可作为迁移期兼容 facade；多 Recipe 阶段的规范入口是通用 ProduceService/Registry。

## 22. 进入实施计划的 Gate

在开始实现前必须满足：

1. 用户审阅并确认本书面规格；
2. iwiki SDK/Portable Contract 的可消费版本与安装方式固定；
3. 第一份实施计划只覆盖本设计 V0–V7 的 Video Producer，不混入 Desktop/MCP/Publisher；
4. 实施计划逐任务列出精确文件、先写失败测试、验证命令和小提交边界；
5. 执行采用用户已选择的 Subagent-Driven Development，并在独立 worktree 中进行；
6. 每个任务完成后进行规格符合性检查和代码质量检查；
7. 最终使用真实 Windows Gate 与 iwiki commit 证据验收，不凭口头宣称完成。
