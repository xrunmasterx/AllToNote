# AllToNote Knowledge Compiler 总体架构设计

- 日期：2026-07-13
- 状态：终稿，待用户确认
- 文档层级：AllToNote 知识生产系统上位设计
- 适用阶段：Phase 3 及以后；同时约束 Phase 2 必须保留的扩展边界
- 目标平台：Windows Tier 1；macOS Tier 2
- 产品基线：Headless-first、本地优先、开放 Markdown、CLI 一等入口、Desktop 为薄 UI

## 1. 文档目的与规范关系

### 1.1 文档目的

本文定义 AllToNote 从“视频转笔记工具”演进为通用知识编译系统时必须长期稳定的产品边界、领域模型、执行模型、数据合同、接口分工、安全边界和演进顺序。

AllToNote 的最终职责不是保存一种专有笔记格式，也不是提供通用 Agent 工作流画布，而是把视频、文章、Wiki、PPT、PDF、代码库、UE5 模块、Git 活动和个人工作资料等来源，可靠地转换为：

- 可追溯到来源证据；
- 可检查质量；
- 可由用户审阅；
- 可安全发布；
- 可被 Obsidian、Git、CLI、MCP 和任意 Agent 长期使用；
- 不依赖 AllToNote 才能读取的开放知识资产。

本文把这个系统称为 **Knowledge Compiler**。其核心产品定义为：

> AllToNote 是一个 Headless-first、本地优先、以开放 Artifact 和 Markdown Workspace 为数据底座、以可恢复知识生产流程为执行模型、以 CLI、MCP 和薄 Desktop 为多种入口的知识编译系统。

### 1.2 与已有设计的关系

已有设计继续有效，但职责层级调整如下：

1. `2026-07-12-alltonote-llm-iwiki-desktop-design.md` 保留产品边界、开放磁盘合同、AllToNote 与 llm-iwiki 解耦、Producer/Publisher 分离、personal/common 发布规则、网站职责和平台基线。
2. 本文取代该文档中“每个来源对应一个大 Producer”“视频专用 Source Bundle 可直接推广到所有来源”“生产、审阅和发布共用一个状态机”“JobStore 默认位于 Workspace”“Codex 可作为普通 GPT Provider 表达完整 Agent 能力”等过于粗粒度的设计。
3. `2026-07-13-alltonote-cli-first-vault-workspace-design.md` 继续作为 Phase 2 只读工作区的下位设计，本文不扩大其实施范围。
4. Phase 2 中的 Core/SDK、独立 CLI、临时 Desktop API、薄 Tauri Desktop、托管式独立 Runtime、路径安全和性能目标全部保留。
5. Phase 3 开始前，所有 Producer、任务、Agent、MCP 写能力和 Publisher 设计必须符合本文。

出现冲突时采用以下优先级：

```text
llm-iwiki 已发布的 Workspace/schema/publish/index 合同（仅限其拥有的协议域）
  > 本 Knowledge Compiler 总体架构设计（AllToNote 产品、领域和运行时边界）
  > 适用且已确认的下位设计（包括 Phase 2 和未来各子系统设计）
  > 2026-07-12 总体桌面系统设计中未迁入本文的同主题旧描述
  > 实施计划和当前代码
```

不同协议域之间按所有权协作，不互相越权覆盖。下位设计只能细化本文，不能静默改变本文的不变量；当前代码也不自动成为目标架构。若确需改变，必须先修订本文并记录架构决策。

### 1.3 设计范围分解

Knowledge Compiler 涉及多个可独立实施的子系统，不能由一份实施计划一次完成。本文只冻结上位边界，后续至少拆分为：

- Portable Artifact 与 Source Bundle 合同；
- Job Engine 与本地持久任务合同；
- Video Note Recipe 垂直切片；
- Review 与 Publisher；
- ModelExecutor 与 AgentExecutor；
- Knowledge MCP 与 Production MCP；
- Runtime、Feature Pack 与安装更新。

每个子系统分别形成下位设计、实施计划和验收 Gate。

## 2. 背景与现状问题

### 2.1 当前能力

现有 AllToNote 已具备视频链接识别、下载、字幕或语音转写、LLM 总结、截图、Markdown 展示和多模型配置等能力。当前实现以 FastAPI 后端、React 前端和可选 Tauri sidecar 为主。

现有视频主流程集中在 `NoteGenerator` 中，下载、转写、模型调用、截图、任务状态和结果保存彼此耦合；任务主要依赖 Web 进程、线程池和状态文件；Codex app-server 被适配成普通 LLM Provider；Tauri 仍以固定完整后端 sidecar 为主要运行模型。

这套结构可以支持现有视频功能，但不能直接承载以下长期需求：

- 同一来源选择不同知识生产方案；
- 一次任务消费多个来源或产生多篇知识；
- 文章、PPT、PDF、Wiki 和代码证据的不同定位方式；
- 分钟级至小时级任务的取消、恢复和客户端重连；
- LLM 生成和 Agent 工具执行的不同权限；
- 来源、草稿、质量报告和正式文档之间的完整 lineage；
- CLI、MCP 和 Desktop 共享完全一致的业务语义；
- 以按需 Feature Pack 扩展能力而不持续增大基础 Runtime。

### 2.2 产品问题

用户真正需要的不是“创建更多 Markdown 文件”，而是：

1. 提交一个链接、文件、目录、仓库或公共知识查询；
2. 系统识别来源并推荐合适的知识生产 Recipe；
3. 在执行前说明本地/远端数据边界、所需能力和预期成本；
4. 保留足以复核结果的来源与证据；
5. 生成结构清楚、事实有依据的知识草稿；
6. 展示质量问题和未解决风险；
7. 经用户审阅后发布为正式 Markdown；
8. 不打开 Desktop 时，用户、脚本和 Agent 仍能完成相同流程；
9. 卸载 AllToNote 后，所有已提交知识仍可使用。

因此，AllToNote 的主价值链是：

```text
Capture -> Evidence -> Compile -> Evaluate -> Review -> Publish -> Reuse
```

文件树、Markdown 阅读和搜索是支撑能力，不是产品的主要差异化。

## 3. 目标、非目标与成功标准

### 3.1 架构目标

1. **开放资产。** 正式知识、来源证据和必要 provenance 以开放文件长期保存。
2. **Headless 完整性。** 核心能力无需启动 Desktop，可由 CLI 独立完成。
3. **单一业务语义。** CLI、MCP、Desktop API 和后续适配器复用同一 Core。
4. **来源可扩展。** 新增来源不需要复制任务、质量、发布和恢复逻辑。
5. **Recipe 可扩展。** 同一来源可使用不同知识生产方案；一个 Recipe 可组合确定性步骤、模型步骤和受控 Agent 步骤。
6. **证据可追溯。** 关键结论能够定位到视频时间、页面、段落、Slide、Wiki revision、Git commit、文件或 symbol。
7. **质量可评测。** “高质量”由结构化 Quality Report、Recipe 评测集和人工审阅共同定义，而不是只依赖 Prompt。
8. **失败可恢复。** 长任务在进程崩溃、客户端退出、网络失败和系统重启后具有确定状态。
9. **发布可控制。** Producer 和 Agent 不通过 AllToNote 接口直接写正式知识；所有正式写入经过 iwiki Publisher。
10. **基础 Runtime 轻量。** 浏览、CLI 元数据和任务管理不加载 Whisper、OCR、视频处理或 Agent 重依赖。
11. **供应商可替换。** 模型、转录器、Agent 和索引实现不进入知识磁盘合同。
12. **分发低门槛。** 逻辑组件保持解耦，但普通用户通过经过兼容测试的安装单元完成安装和更新。

### 3.2 产品目标

AllToNote 的一级用户概念统一为第 24.2 节的规范词汇：知识库、来源、知识方案、生产任务、草稿、来源证据和正式知识。用户界面统一使用“生产任务”，只在技术协议和诊断信息中使用 Job/Run。

QualityReport 是草稿的质量信息，Review 是从草稿到正式知识的产品阶段和操作集合，不再作为与草稿并列的另一套一级实体。普通用户不需要理解 Artifact、Capability DAG、Attempt、Executor、进程或协议版本。产品界面应把内部复杂性翻译为“视频课程笔记”“文章深度笔记”“UE5 模块知识”等可理解方案。

### 3.3 明确非目标

近期不建设：

- 通用 Markdown 编辑器或 Obsidian 替代品；
- 通用低代码/可视化 Workflow 画布；
- Dify、n8n、Temporal、Airflow 或 LangGraph 的完整替代品；
- 通用 Agent 框架；
- 第三方插件市场和不可信代码沙箱；
- 云端知识正文数据库；
- 通用文件同步和多人实时协作；
- 以聊天或 RAG 作为主产品价值；
- 自动发布到 `wiki/common`；
- Agent 任意 shell 或任意 Workspace 写权限；
- 保证 LLM/Agent 输出 bit-for-bit 可复现；
- 开机常驻并预加载重型模型的后台服务；
- 首版承诺 Linux 或 Intel Mac。

### 3.4 总体成功标准

1. 不安装 Desktop 时，CLI 能完成至少一个从 Video Note 生产、审阅、批准到 personal 正式发布的完整闭环。
2. Desktop 调用的生产、任务和发布语义与 CLI 完全一致。
3. 一个 Job 可以产生一个或多个 Draft，并在 Job 完成后独立进入审阅流程。
4. 删除本机 JobStore 和索引后，已提交 Source Bundle、Draft、Quality Report 和正式知识仍然完整。
5. 任一正式知识可以追溯到支撑它的 SourceRevision、Evidence、Recipe 和生成摘要。
6. 文章/Wiki 和 PPT/PDF 接入时不复制视频任务状态机、Publisher 或凭据逻辑。
7. UE5/代码库 Recipe 能使用 AgentExecutor，而不把 Codex 伪装成普通 LLM Provider。
8. Desktop 退出、MCP 客户端断开或终端关闭时，后台模式 Job 仍有确定状态。
9. 通过 AllToNote Production MCP 启动的任务不能直接发布正式知识。
10. Runtime 基础命令不加载任何重型 Feature Pack。
11. Windows 安装、更新、回滚和 CLI-only 使用通过端到端验证。
12. 用户可以用 Obsidian 直接打开 `wiki/`，并可用其他 Agent 直接读取 Markdown。

## 4. 术语与架构不变量

### 4.1 存储术语

- **Workspace Root**：包含 `.llm-wiki/`、`raw/`、`wiki/` 的开放工作区根目录。
- **Published Vault**：Workspace 内的 `wiki/`，可直接由 Obsidian 打开。
- **Source Store**：Workspace 内的 `raw/`，保存来源、证据、草稿和质量产物。
- **Machine State**：本机 Job、事件、租约、运行缓存和安装状态，不属于可携带知识。
- **Machine Cache**：AllToNote 自有的可删除、可重建 Step cache、下载缓存、模型缓存和中间文件；iwiki 拥有的 Workspace Index 不并入该定义。
- **Source Bundle**：`raw/personal` 中经过校验并原子提交的一组可携带来源、证据、草稿和生产摘要。
- **personal/common**：Workspace 内的发布分类；`common` 不等于已经公开到互联网，也不自动授予外部访问权。上传、共享和远端同步必须是另一项显式操作。

后续文档不得把 Workspace Root 和 Published Vault 都简称为“Vault”。面向用户时可以显示“知识库”，内部协议必须使用明确术语。

### 4.2 运行组件术语

- **Core**：领域模型、业务不变量和应用用例；不感知 CLI、HTTP、Tauri 或 MCP。
- **Engine**：执行长任务、持久化 Job、监督子进程、调度资源和发送事件的本地执行进程。
- **Runtime**：Core、CLI、Engine launcher、Desktop bridge、MCP adapter 和基础平台适配器的安装/分发边界。
- **CLI**：最重要、最稳定的一等公开自动化入口，但不是业务逻辑的唯一实现位置。
- **Desktop**：薄 UI 和 Runtime 生命周期客户端，不拥有业务真相。
- **Feature Pack**：按需安装的可信能力集合，例如 video、whisper、document、codex-agent。
- **llm-iwiki**：独立的 Workspace schema、校验、查询、索引和发布语义实现。

### 4.3 领域术语

- **Source**：长期来源身份，例如一个 Bilibili 视频、网页、文件或 Git 仓库。
- **SourceRevision**：某次实际采集的不可变来源版本。
- **Artifact**：某个 Step 产生并经过校验、提交的类型化产物。
- **EvidenceRef**：从知识陈述指向 SourceRevision 中具体位置的稳定引用。
- **Recipe**：从输入到知识产物的版本化生产方案。
- **Capability**：Recipe 可以调用的具体能力声明。
- **Job**：一次可查询、可取消的 Recipe 执行。
- **Step**：Recipe 中的一个稳定逻辑步骤。
- **Attempt**：Step 的一次具体执行尝试。
- **Draft**：未发布的知识候选产物。
- **QualityReport**：对 Draft、Evidence 和结构进行检查的结果。
- **PublishPlan**：由 iwiki 生成、尚未应用的正式写入计划。
- **Publication**：已经通过 Publisher 提交的正式知识结果。

### 4.4 不可破坏的不变量

1. 知识资产的寿命必须大于 AllToNote、llm-iwiki、模型、Agent 和索引引擎的寿命。
2. Markdown、附件、来源证据和 portable provenance 是长期资产；JobStore 和索引不是。
3. Core 是 CLI、MCP 和 Desktop 的唯一业务语义来源。
4. Producer/Recipe 只能提交 Source Store 产物，不能通过 AllToNote 写入 Published Vault。
5. 正式发布必须通过 `iwiki plan-publish/apply-publish` 或其后续兼容协议。
6. `personal` 是默认发布目标；`common` 必须是额外明确操作。
7. Job、Draft、PublishTransaction 和 Index 分别维护生命周期，不能共用一个大状态枚举。
8. Job 成功的含义是生产产物完整提交，不是用户已经完成审阅或发布。
9. Agent 只能消费被授予的输入并写 staging；其输出先成为 Artifact。
10. AllToNote 只能保证经自身接口的发布纪律；拥有操作系统写权限的外部程序仍可直接编辑 Markdown。
11. Credential 只通过 Credential Profile 间接引用，不进入 Artifact、日志、Markdown 或命令行参数。
12. Feature Pack、Recipe 和执行器版本在 Job 创建时固定，运行中不得静默替换。
13. watcher、索引和缓存只能提升性能，不能成为正确性来源。
14. 基础 Runtime 路径不得导入重型 Feature Pack。
15. 任何远端传输都必须可识别目标服务、数据范围和凭据 Profile。

## 5. 架构候选与最终决策

### 5.1 方案 A：继续按来源增加大型 Producer

做法是在现有 `NoteGenerator` 旁增加 ArticleProducer、PptProducer、WikiProducer、CodebaseProducer。

优点：

- 视频垂直功能最快；
- 初始抽象较少。

缺点：

- 每个 Producer 重复任务、重试、缓存、日志、质量、凭据和发布逻辑；
- 多来源、多输出和 Agent 场景难以表达；
- 很快形成多个 God Class。

结论：只可作为迁移期间的临时外观，不作为目标架构。

### 5.2 方案 B：把 CLI 处理器作为技术内核

做法是让 Desktop 和 MCP 每次启动 CLI 子进程并解析 stdout。

优点：

- 所有能力表面上都能从命令行访问；
- 无需长期本地服务。

缺点：

- 重复冷启动；
- watcher、事件流、取消和长任务恢复困难；
- stdout 被迫成为内部 RPC；
- Desktop、MCP 和 CLI 生命周期耦合；
- CLI handler 容易承载业务逻辑。

结论：拒绝。采用 Core-centric、CLI-first，而不是 CLI-handler-centric。

### 5.3 方案 C：直接引入通用 Workflow 平台

做法是将 Dify、n8n、Temporal、LangGraph 等作为产品内核或建设可视化 DAG 编辑器。

优点：

- 表面扩展能力强；
- 已有丰富节点和编排概念。

缺点：

- 安装、数据库和服务显著变重；
- 用户被迫理解通用编排；
- 本地权限、离线分发和 Artifact 合同仍需自行解决；
- 产品会从知识生产工具偏移为工作流平台。

结论：拒绝作为产品内核。只借鉴 Job、Activity、checkpoint、event 和 manifest 等成熟概念。

### 5.4 方案 D：最小 Typed Knowledge Compiler

做法是以 SourceRevision、Artifact、EvidenceRef、Recipe、Job 和 QualityReport 为最小中间合同；Core 保存业务语义；前台命令直接使用 Core，后台长任务使用按需 Engine；CLI、MCP 和 Desktop 都作为适配器。

优点：

- 保持开放和 Headless；
- 支持长任务和恢复；
- 不引入通用工作流平台；
- 能逐步接入多种来源和 Agent；
- 能用 Feature Pack 控制安装体积和运行成本。

代价：

- 需要先建立 Artifact、Job 和 Recipe 合同；
- 比继续堆叠现有 Producer 有更多前期设计工作；
- Engine、版本和兼容性需要独立测试。

结论：**采用方案 D。**

## 6. 总体架构与依赖方向

### 6.1 逻辑层次

系统按职责分为五组组件：

1. **入口与 Host**：CLI、Production MCP、Knowledge MCP 聚合器、Desktop bridge，以及只负责后台承载的 Engine Host。
2. **共享应用服务**：RecipeCatalog、JobService、ArtifactService、ReviewService、PublishService，以及 JobCoordinator、RecipeRunner、AttemptRunner、ArtifactCommitter 和恢复决策。前台 CLI 与后台 Engine Host 必须复用这一组服务。
3. **Knowledge Compiler Core**：领域对象、不变量、策略和端口接口。
4. **基础设施适配器**：Source Connector、ModelExecutor、AgentExecutor、ArtifactStore、JobRepository、ProcessSupervisor、CredentialStore、ResourceScheduler、IWikiGateway 和平台适配器。
5. **组装与分发**：Runtime 组合根、Feature Pack 发现和版本兼容，不拥有第二套业务语义。

持久数据和外部 peer 不被伪装成一个“开放数据层”，而是严格分开：

- **开放持久数据**：Workspace；
- **独立外部协议提供方**：llm-iwiki；
- **本机操作存储**：Machine State；
- **Runtime 管理资产**：Feature Pack、模型和工具。

### 6.2 依赖规则

依赖方向固定为：

```text
CLI foreground / Engine Host / MCP / Desktop
    -> shared Application Services
        -> Core domain, policies and ports
            <- infrastructure adapters implement ports

AllToNote -> public iwiki protocol -> llm-iwiki -> Workspace contract
```

Engine Host 不实现另一套 Job/Recipe 编排，只承载 IPC、进程生命周期、scheduler lease、事件传输和组件组装。业务状态推进、恢复决策和 Artifact 提交协议都位于共享应用服务中，因此前台与后台执行路径不会分叉。

禁止：

- Core 导入 FastAPI、Tauri、MCP SDK 或 CLI parser；
- Desktop 直接实现 Source、Recipe、Quality 或 Publish 规则；
- MCP 复制另一套 Job 或权限逻辑；
- llm-iwiki 反向依赖 AllToNote；
- Feature Pack 修改 Core 私有状态；
- Source Connector 直接写 `wiki/`；
- AgentExecutor 获得 Publisher 内部接口。

### 6.3 Runtime 不是额外业务层

Runtime 是安装和版本管理单元，包含：

- Core；
- CLI；
- Engine launcher/client；
- Desktop bridge；
- MCP adapter；
- 基础平台适配器；
- 经过兼容测试的 iwiki peer 或其托管安装描述。

Runtime 不拥有第二套领域模型。CLI-only 安装和 Desktop 托管安装使用同一 Runtime 内容。

### 6.4 读路径与生产路径

只读快路径允许在无需 Engine 的情况下直接使用 Core：

```text
CLI tree/read/search -> Core -> IWikiGateway / safe file adapter
```

生产路径通过 JobService：

```text
CLI --wait
  -> 无 Engine owner 且 CLI 获得 scheduler lease
       -> shared Execution Application Services -> Core
  -> Engine 已持有 scheduler lease
       -> Engine Host -> 同一组 Execution Application Services -> Core

CLI/Desktop/MCP detached/background
  -> Engine Host -> 同一组 Execution Application Services -> Core
```

这样可以避免所有简单命令都启动后台进程，同时保证长任务拥有持久生命周期。

## 7. 核心组件设计

### 7.1 应用服务

| 组件 | 拥有的职责 | 明确不拥有 |
|---|---|---|
| `WorkspaceService` | 打开、授权和描述 Workspace | iwiki schema 规则 |
| `SourceService` | 创建 Source、SourceRevision 和采集请求 | 平台下载实现 |
| `RecipeCatalog` | 列出、解析和固定 Recipe 版本 | 执行任意 Recipe 代码 |
| `JobService` | submit/get/list/wait/cancel/respond/retry | UI 状态和 Publisher 状态 |
| `ArtifactService` | 注册、查询、校验和追溯 Artifact | 任意路径文件浏览 |
| `QualityService` | 运行 Gate 并聚合 QualityReport | 把模型自评当唯一结论 |
| `ReviewService` | Draft 审阅状态和审阅记录 | 正式文件写入 |
| `PublishService` | 创建和应用 PublishPlan | 复制 iwiki 发布规则 |
| `CapabilityService` | list/inspect/doctor 能力 | 自动信任第三方代码 |

应用服务只通过 Core 端口访问基础设施，不直接依赖具体 Whisper、FFmpeg、Codex 或模型 SDK。

### 7.2 Core 端口

首轮稳定内部端口限定为：

- `WorkspaceGateway`；
- `IWikiGateway`；
- `SourceConnector`；
- `ArtifactStore`；
- `JobRepository`；
- `EventRepository`；
- `RecipeProvider`；
- `CapabilityRunner`；
- `ModelExecutor`；
- `AgentExecutor`；
- `QualityGate`；
- `CredentialStore`；
- `ProcessSupervisor`；
- `ResourceScheduler`；
- `Clock` 和 `IdGenerator`。

这些端口是实现隔离边界，不承诺立即成为第三方公共 SDK。公共自动化协议优先稳定 CLI JSON、Engine client protocol 和 MCP。

### 7.3 共享执行应用服务与 Engine Host

| 分类 | 组件 | 职责 |
|---|---|---|
| 共享执行应用服务 | `JobCoordinator` | 创建 Job、推进状态并协调恢复 |
| 共享执行应用服务 | `RecipeRunner` | 解析固定 Recipe 并逐 Step 执行 |
| 共享执行应用服务 | `AttemptRunner` | 为一次执行建立隔离上下文、超时和取消 |
| 共享执行应用服务 | `ArtifactCommitter` | 校验 staging、创建 checkpoint 并提交 portable Bundle |
| 共享执行应用服务 | `RecoveryCoordinator` | 根据 Artifact、Attempt、租约和策略决定恢复、等待或终结 |
| Engine Host | `EngineServer` | 本机认证 IPC、客户端连接和协议协商 |
| Engine Host | `HostLifecycle` | 按需启动、空闲退出、版本 drain 和组件组装 |
| Engine Host | `SchedulerLeaseHost` | 持有单一 scheduler lease 和 fencing token |
| Engine Host | `EventTransport` | 向客户端重放由 EventRepository 持久化的规范事件 |
| 基础设施适配器 | `ProcessSupervisor` | 启动、取消、contain/kill process tree、收集退出状态 |
| 基础设施适配器 | `ResourceScheduler` | 分配 CPU、GPU、模型、供应商并发和磁盘 I/O 租约 |
| 基础设施适配器 | `CredentialBroker` | 只向当前 Step 注入声明需要的最小凭据 |

前台 CLI 只有在成功获得 scheduler lease 时才直接调用共享执行应用服务；已有 Engine owner 时，CLI 通过 Engine client 提交/连接同一 Job 并等待结果。后台模式由 Engine Host 调用同一组服务。两条路径都只执行已经注册并固定版本的 Recipe/Capability，不运行来自 Markdown 或远端 MCP 响应中的任意代码。

### 7.4 基础设施适配器

第一阶段适配器包括：

- 视频平台 Source Connector；
- 本地文件 Source Connector；
- FFmpeg；
- 字幕获取和 Transcriber；
- OpenAI-compatible Model Adapter；
- 本地模型 Adapter；
- Codex Agent Adapter；
- iwiki CLI Gateway；
- Windows Credential/Process/FileSystem Adapter；
- macOS 对应 Tier 2 Adapter。

平台特定来源逻辑必须停留在 Connector/Feature Pack，不能进入 Core。

## 8. 数据架构与所有权

### 8.1 三类数据

系统数据分为三类：

#### 可携带长期资产

保存于 Workspace：

- Source Bundle manifest；
- SourceRevision 元数据和允许保存的快照；
- Evidence；
- Draft；
- QualityReport；
- portable provenance/run summary；
- Publication 引用；
- 正式 Markdown 和正式附件。

#### 本机操作状态

默认保存于：

```text
Windows: %LOCALAPPDATA%/AllToNote/workspaces/<local-workspace-instance-id>/
macOS:   ~/Library/Application Support/AllToNote/workspaces/<local-workspace-instance-id>/
```

包含：

- `jobs.sqlite`；
- Step/Attempt/Event；
- external call 状态；
- resource lease；
- Engine discovery；
- 本机 Runtime/Pack compatibility 状态。

#### 可重建缓存和大型依赖

按所有权分别保存：

- AllToNote Machine Cache：下载缓存、临时媒体和 Step cache；
- iwiki-owned Workspace Index：QMD/其他 Workspace 索引，其位置、生命周期、刷新和重建完全由 iwiki 合同拥有；
- Runtime 管理资产：模型权重、OCR/Whisper 缓存、Feature Pack 和工具安装内容。

AllToNote Runtime 只负责发现兼容的 iwiki peer，并通过 IWikiGateway 请求索引操作；不能直接管理、移动、清理或解释 iwiki 的 QMD/Workspace Index。

### 8.2 JobStore 移出 Workspace

旧设计中的 `.cache/alltonote/jobs.sqlite` 不再作为默认 JobStore。原因是 Workspace 可能被 Git、OneDrive、Dropbox 或其他工具复制和同步，而 Job 是机器、进程、依赖和凭据相关状态。

Workspace 内允许保留：

- 为同卷原子提交服务的短期 staging；
- iwiki 发布事务日志；
- 可安全复制的 run summary；
- 不含本机进程和凭据信息的 portable manifest。

删除本机 JobStore 会失去任务历史、未完成任务恢复点和 `client_request_id` 幂等绑定，但不会失去任何 portable committed 长期资产。删除前必须明确提示这些后果。

### 8.3 Workspace 身份

必须区分：

- `workspace_lineage_id`：AllToNote 对当前 iwiki manifest `workspace_id` 的逻辑称呼/映射，可随 Workspace 复制；它不是 AllToNote 单方面新增的 manifest 字段；
- `local_workspace_instance_id`：lineage ID、规范路径和卷/文件身份组合出的本机身份；
- `desktop_workspace_id`：单次 Desktop Runtime 会话中的短期 opaque handle。

复制 Workspace 不得让原目录和副本共享 JobStore。显式 clone/fork 操作可以通过 iwiki 的 versioned contract 生成新的 `workspace_id`/lineage；普通文件复制即使保留相同 `workspace_id`，也必须因路径和文件身份不同而得到独立本机实例。只有 llm-iwiki 正式发布新 schema 后，AllToNote 才能使用额外 lineage manifest 字段。

### 8.4 Source 与 SourceRevision

`Source` 表示长期身份，`SourceRevision` 表示不可变采集结果。

示例：

- 视频：平台、视频 ID、字幕/媒体内容 hash、采集时间；
- 网页：canonical URL、retrieved_at、内容 hash；
- Wiki：页面 ID、revision ID；
- 本地文件：规范引用、文件 hash；
- Git 仓库：remote identity、commit/tree hash；
- Git 活动：commit range；
- Remote MCP：server identity、tool/query、响应 hash、retrieved_at。

同一 URL 不等于同一 SourceRevision。动态来源必须根据 freshness policy 重新采集并产生新 revision。

每个 SourceRevision 必须声明来源物化方式：

- `archived`：许可允许的来源快照已保存到 Workspace，可离线校验；
- `reference_only`：只保存稳定身份、采集时间、观察到的 revision/hash 和许可信息，正文不归档；
- `external_local`：内容仍在用户控制的 Workspace 外部文件或仓库中，portable manifest 只保存逻辑引用与内容 hash，本机绝对路径映射属于 Machine State。

SourceRevision 还必须记录适用的 canonical identity、captured/retrieved time、内容 hash 或不可取得原因、license/privacy 分类和 freshness 信息。`reference_only` 或 `external_local` 在来源无法重新解析时必须产生明确 Quality warning 或按 Recipe 规则阻止发布，不能伪装成可离线验证的证据。

### 8.5 Artifact Envelope

所有 portable committed Artifact 在逻辑上至少需要以下信息。下列 YAML 只展示逻辑最小字段集合，不冻结物理文件名、字段名或序列化格式：

```yaml
artifact_schema_version: 1
artifact_id: "..."
artifact_type: "evidence.transcript.v1"
media_type: "text/markdown"
workspace_relative_path: "..."
sha256: "..."
size: 0
created_at: "..."
parents: []
source_revision_ids: []
generated_by:
  job_id: "..."
  step_id: "..."
  attempt_id: "..."
  capability_id: "..."
  capability_version: "..."
generation:
  recipe_id: "..."
  recipe_version: "..."
quality_report_ids: []
```

精确 JSON/YAML schema、路径和迁移只能由 llm-iwiki schema 或双方共同发布的 Portable Artifact 下位合同冻结，AllToNote 不能仅凭此示例创建私有磁盘格式。

只有不可变、已校验并完成 portable commit 的文件才能被 Source Bundle manifest 引用。临时路径、绝对路径、Secret 和进程 ID 不进入 portable Artifact。

Artifact 提交采用明确的两级可见性：

```text
staged -> checkpointed -> portable_committed
   \             \
    +-------------> quarantined
```

- `staged`：Attempt 私有的待校验输出；
- `checkpointed`：已经校验并持久化到当前 Job 的同卷 staging/checkpoint 区，可用于本 Job 恢复和后续 Step，但对其他 Job、ReviewService 和 Publisher 不可见；
- `portable_committed`：包含该 Artifact 的 Source Bundle manifest、portable receipt 和声明输出已经整体原子提交到 `raw/personal`，之后才成为可跨 Job 引用的长期资产；
- `quarantined`：校验失败、来源不可信、fencing 失效或恢复时无法确认完整性的输出，只供诊断，不能作为有效输入或发布依据。

修改 `portable_committed` Artifact 必须创建新的 Artifact ID 和内容 hash。状态转换由 ArtifactCommitter 负责；portable Artifact envelope 只描述 `portable_committed` 结果，staged/checkpointed/quarantined 运行状态保存在本机操作状态或隔离区。

首版通用 Artifact 类型保持最小：

```text
source.snapshot.v1
evidence.document.v1
evidence.transcript.v1
evidence.asset.v1
knowledge.draft.markdown.v1
quality.report.v1
publish.plan-ref.v1
knowledge.published-ref.v1
```

专业 Recipe 可以增加 namespaced 类型，例如：

```text
ue5.module-map.v1
code.symbol-index.v1
git.change-set.v1
```

禁止为了“统一”而把所有专业数据塞入一个不断膨胀的 Artifact 类型。

### 8.6 EvidenceRef

EvidenceRef 至少包含：

```text
evidence_id
source_revision_id
artifact_id
locator_scheme
locator
content_hash 或 excerpt_hash
```

首轮支持的 locator scheme：

- `video-time-range`；
- `audio-time-range`；
- `document-page`；
- `presentation-slide`；
- `web-section`；
- `wiki-revision-section`；
- `git-file-lines`；
- `code-symbol`；
- `git-commit`；
- `record-id`。

EvidenceRef 必须可由确定性代码解析和校验。只保存一段人类可读来源说明不能替代 EvidenceRef。

### 8.7 Provenance

Portable provenance 使用简单 DAG 表达：

```text
SourceRevision
  -> Evidence Artifact
  -> Normalized Artifact
  -> Draft Artifact
  -> QualityReport
  -> PublishPlan
  -> Published Markdown Ref
```

系统保证可审计和可重新执行，不保证不同时间、模型和外部服务下得到字节完全相同的结果。

详细事件可以从 JobStore 清理，但支撑正式知识可信度的 lineage 摘要必须随 Source Bundle 长期保存。

### 8.8 Portable Run Receipt

每次成功提交 Source Bundle 都必须生成最小、可携带、可长期读取的运行回执。它不是 JobStore 备份，也不保存进程、租约、Secret 或完整事件流，至少记录：

- receipt schema version 和稳定 run ID；
- Recipe ID/version、规范化参数摘要与模板/prompt hash；
- 输入 SourceRevision ID/hash；
- 输出 Artifact ID/type/hash；
- Capability ID/version 和执行模式；
- 模型或 Agent 的非敏感标识及已知用量摘要；
- started/finished time 和最终结果；
- QualityReport ID、结论与未解决 warning；
- 必要的父 run/重试关系。

供应商原始响应、完整 Agent 事件和 provider request ID 默认留在本机 JobStore；只有当审计价值明确且隐私策略允许时，才把经过裁剪的摘要写入 portable receipt。删除 JobStore 后，receipt 必须仍足以回答“这份草稿由什么来源、哪版 Recipe 和哪些能力产生”。

### 8.9 Source Bundle 概念布局

上位合同采用：

```text
raw/personal/<bundle-id>/
├─ bundle.yaml
├─ sources/
├─ evidence/
├─ assets/
├─ drafts/
├─ quality/
└─ run-summary.json
```

约束：

- 一个 Bundle 可以包含多个 SourceRevision；
- 一个 Bundle 可以包含多个 Draft；
- 一个 Draft 可以引用多个 SourceRevision；
- SourceRevision 和 Evidence 不被重新生成操作覆盖；
- Draft 新版本以新 Artifact 表达；
- Bundle 只有在所有声明输出、manifest 和 portable receipt 通过完整性校验后才从同卷 staging 原子提交；
- Bundle manifest/目录原子切换成功是 Artifact 从 `checkpointed` 变为 `portable_committed` 的唯一可见性线性化点；
- 线性化完成前，其他 Job、ReviewService 和 Publisher 不能引用其中 Artifact；
- Producer Job 只有在线性化完成并持久记录 commit 结果后才能进入 `succeeded`；
- 缓存清理不能删除 portable committed Bundle；
- 网络视频默认不归档原始媒体，除非用户明确选择且许可允许；
- 精确文件命名和 JSON Schema 由 Portable Artifact 下位设计冻结。

### 8.10 Schema 演进

每个 portable schema 独立版本化。读取方必须：

- 忽略声明为可忽略的未知字段；
- 对不支持的主版本 fail closed；
- 不静默重写旧 Bundle；
- 迁移前生成 dry-run；
- 迁移完成后保留来源 hash 和 lineage；
- 不使用 Runtime 软件版本替代数据 schema 版本。

## 9. Recipe 与 Capability 模型

### 9.1 Recipe 是产品扩展单位

来源类型不等于 Recipe。一个视频可以选择“课程深度笔记”“快速摘要”“逐章节学习卡片”；同一个 Recipe 也可以消费视频字幕和配套 PPT。

Recipe 至少声明：

```yaml
recipe_schema_version: 1
id: "alltonote.video-course-note"
version: "1.0.0"
accepted_input_kinds: []
parameter_schema: {}
required_capabilities: []
output_artifact_types: []
required_quality_gates: []
permissions: {}
resource_hints: {}
review_policy: "required"
default_publish_scope: "personal"
```

Recipe 版本进入 Job 固定配置、Artifact provenance 和评测基线。

### 9.2 首版使用代码定义 Recipe

首版 Recipe 用普通代码定义并通过测试验证，不建设：

- 拖拽式 Workflow 编辑器；
- 任意循环和脚本的 YAML DSL；
- 运行时下载并执行远端 Recipe 代码；
- 用户不可理解的通用节点图。

上位模型允许顺序 Step、条件、有限 fan-out/fan-in、重试、checkpoint 和取消。只有真实 Recipe 证明需要后，才扩展 DAG 语义。

### 9.3 Capability Manifest

每个可执行能力声明：

```text
capability_protocol_version
id
version
kind
accepted_artifact_types
produced_artifact_types
parameter_schema
supported_platforms
required_runtime_features
permissions
credentials
network_access
resource_claims
isolation_mode
retry_semantics
idempotency_semantics
checkpoint_support
```

首轮 Capability kind 限定为：

- source；
- extractor；
- normalizer；
- transform；
- model；
- agent；
- validator。

这些分类是发现和政策元数据，不要求建立复杂继承体系。

正式知识发布和 Workspace Index 不属于普通 Recipe/Feature Pack Capability：发布只能走 `PublishService -> IWikiGateway -> iwiki`，Workspace 索引只能走 IWikiGateway 请求 iwiki。Pack、Recipe 和 Agent 都不能注册 `publisher` 或 Workspace `indexer` 绕过该路径；Recipe 私有的临时分析索引属于内部 transform/cache，不取得 Workspace Index 所有权。

### 9.4 Feature Pack 信任模型

第一阶段仅支持：

- Base Runtime 内置能力；
- 官方签名并由用户安装的 Feature Pack；
- 用户显式配置的外部可执行程序，例如 Codex。

第三方 Capability 必须在出现真实需求后采用独立进程协议。不得把任意第三方 Python 包直接 import 到 Engine，并宣称进程 manifest 等同于安全沙箱。

### 9.5 公共扩展协议 Gate

只有以下 Recipe 都通过端到端验证后，才冻结第三方 Recipe/Capability Protocol：

1. Video Note；
2. Article/Wiki Note；
3. PPT/PDF Note；
4. UE5/Codebase Knowledge 中至少一个 Agent Recipe 原型。

在此之前，内部接口允许演进，但 portable Artifact 和 CLI 自动化合同按版本兼容。

## 10. Job 与执行模型

### 10.1 四种独立生命周期

#### Job

```text
queued -> running -> succeeded
                  -> failed
                  -> cancelled
                  -> waiting_for_input -> queued
                                       -> failed
                                       -> cancelled
queued -> cancelled
```

`waiting_for_input` 只用于执行中确实缺少 Cookie、授权、预算确认或用户选择的情况，不用于等待 Draft 审阅。`running -> waiting_for_input` 时必须先终结当前 Attempt，并持久化一个 PendingChallenge；有效响应消费该 challenge、创建新 Attempt，并把 Job 重新放回 `queued`。

#### Step Attempt

```text
pending -> running -> succeeded
                   -> failed
                   -> cancelled
                   -> interrupted
                   -> needs_input
pending -> skipped
        -> cancelled
```

`interrupted` 表示执行进程或 Engine 消失，且没有足够证据确认成功；`needs_input` 表示本次 Attempt 已安全停下并创建 PendingChallenge。Attempt 一旦进入终态就不再改回 `running`；恢复、响应或重试必须创建新的 Attempt，并通过 `retry_of_attempt_id` 保留关系。

#### Draft Review

```text
unreviewed -> approved
           -> rejected
           -> superseded
```

#### Publish Transaction

```text
planned -> applying -> committed
                    -> conflict
                    -> failed
                    -> rolled_back
```

Index 状态继续由 iwiki 单独管理。

任一 Job 进入 `succeeded`、`failed` 或 `cancelled` 终态前，所有未完成 Attempt 都必须被收敛为确定终态，不能遗留 `pending` 或 `running`。

#### PendingChallenge

同一 Job 同时最多有一个 active challenge，至少包含：

```text
challenge_id
originating_attempt_id
reason_category
safe_prompt
response_schema
allowed_response_kinds
created_at
expires_at
```

`job get` 返回不含 Secret 的 challenge 元数据。`job respond` 必须带 challenge ID 和结构化 response。消费规则按规范化 response hash 唯一确定：

- `challenge_id + 相同 response hash`：即使 challenge 已消费，也幂等返回第一次成功结果，包括已创建的 Attempt ID 和当前 Job 状态，不创建新 Attempt；
- 已消费 challenge 收到不同 response hash：返回 stable specific code=`challenge_already_consumed`；
- 首次响应前 challenge 已过期：返回 specific code=`challenge_expired`；
- challenge 不属于当前 Job：返回 `job_conflict`；不属于当前 Workspace Grant/principal：返回 `workspace_not_granted` 或 `policy_denied`；
- 首次消费、response hash 记录、创建新 Attempt 和 Job 回到 `queued` 必须处于同一事务或受同一唯一约束保护，两个并发响应只能有一个成为首次成功结果。

需要 Cookie/API key 时 response 只能引用 Credential Profile，不能把明文 Secret 放入命令行、事件或 JobStore 普通字段。challenge 过期且无法自动恢复时 Job 进入 `failed`；用户也可以在等待期间取消 Job。

### 10.2 Job 成功语义

Producer Job 在所有声明输出 Artifact、QualityReport、Bundle manifest 和 portable receipt 完成 `portable_committed` 后进入 `succeeded`。它不等待用户审阅，也不负责把 Draft 发布到 `wiki/`。

一个成功 Job 允许：

- 产生零个 Draft，但必须以结构化结果说明原因，例如来源无可用正文；
- 产生一个 Draft；
- 产生多个 Draft；
- 产生 Source/Evidence 而暂不生成 Draft，但 Recipe 必须明确声明这种结果类型。

“没有异常”不能作为成功条件；Job 输出必须满足 Recipe 的 output contract。

### 10.3 Step 提交协议

每个 Step 遵循：

```text
读取跨 Job 的 portable_committed Artifact 或本 Job 的 checkpointed Artifact
  -> 创建 Attempt 和 staging
  -> 执行 Capability
  -> 校验输出
  -> 校验 scheduler/Attempt fencing token、Job 状态和取消标志
  -> 原子创建当前 Job 可见的 checkpointed Artifact
  -> 在 JobStore 事务中标记 Attempt succeeded
```

所有 Step 完成后，Job finalizer 遵循：

```text
校验所有声明输出
  -> 生成并校验 Bundle manifest 与 portable receipt
  -> 取得带 fencing token 的 bundle commit guard
  -> 再次确认 Job=running、Attempt 归属有效且 cancellation_requested=false
  -> 原子切换完整 Bundle 为 portable_committed
  -> 持久记录 commit 结果并将 Job 标记 succeeded
```

Bundle 原子切换是对外可见性的线性化点。取消标志若先于 commit guard 生效，提交必须拒绝并把迟到输出隔离；portable commit 若先完成，Job 成功终态获胜，随后 cancel 返回“已完成”而不能把已提交资产改成 cancelled。Job 已进入 `failed`/`cancelled`、scheduler fencing token 过期或 Attempt 归属失效后，旧 Worker 不能再 checkpoint 或 portable commit Artifact。

崩溃恢复时：

- 本 Job 内 checkpointed 且 hash 正确的 Artifact 可以恢复复用，但不能被其他 Job 或 Publisher 使用；
- portable committed 且 hash 正确的 Artifact 不重复生成；
- staged 输出不作为有效输入；
- staging 可以按 Capability recovery policy 清理或继续；
- 从第一个未完成 Step 恢复；
- 恢复前重新校验输入 Artifact hash；
- 不静默切换 Recipe 或 Capability 版本；
- Bundle 已线性化但 JobStore 尚未记为 succeeded 时，RecoveryCoordinator 根据 manifest/receipt/hash 对账为 succeeded，不重复生产。

### 10.4 幂等与缓存

Step cache key 至少包含：

```text
input artifact hashes
capability id + version
normalized parameter hash
recipe id + version
prompt/template version or hash
model/agent execution policy
```

远端 URL 先形成 SourceRevision，不能仅用 URL 作为缓存键。

所有自动化入口接受可选 `client_request_id`，用于防止 Agent、MCP 或脚本在超时后重复提交同一个逻辑 Job：

- 幂等范围是 Workspace Grant、调用方身份和 `client_request_id` 的组合；
- 相同 key 与相同规范化请求 hash 返回已有 Job；
- 相同 key 与不同请求 hash 返回稳定 specific code=`idempotency_conflict`；
- 调用方身份必须是跨 CLI/MCP 重启稳定的 principal，不得使用一次性 server session ID 或临时 grant token；
- 原 `client_request_id` 永久绑定其原 Job；即使原 Job 已 failed/cancelled，普通 submit 仍返回原 Job，不能把该 key 重绑定到新执行；
- `job retry` 创建带 `retry_of_job_id` 的新 Job，并要求新的 `client_request_id`；自动化调用方显式提供，人类模式由 CLI 在提交前本地生成并在传输重试中复用，不要求用户手写；对同一次 retry 请求重放仍返回同一个新 Job；
- Attempt 自动重试属于原 Job：同一逻辑外部操作复用稳定 operation idempotency key；新 Job retry 默认创建新的外部 operation key；
- 原 Job 含 `external_outcome_unknown` 时，retry 必须要求调用方明确确认可能再次计费或重复副作用，不能由通用重试静默绕过；
- 幂等绑定至少保留到对应 Job 历史被显式删除；删除 JobStore/历史后该保证终止，CLI 必须醒目提示；
- 未提供 key 时不承诺提交去重；
- Job 提交幂等、Step cache 和外部供应商幂等是三层不同机制，不得混为一谈。

### 10.5 外部付费调用

所有可能计费或产生外部副作用的调用，在执行前先持久化一等 `ExternalOperation` 记录：

```text
external_operation_id
job_id
step_id
attempt_id
provider
request_hash
operation_idempotency_key
provider_request_id (optional)
started_at
outcome
safe_effect_summary
known_cost_or_side_effect_summary
```

`external_operation_id` 是 AllToNote 生成的稳定 opaque ID，不能直接复用可能缺失、变化或泄露供应商信息的 provider request ID。`outcome` 至少区分 prepared/running/succeeded/failed/cancelled/`external_outcome_unknown`。

如果外部调用成功但本地未收到结果，标记为 `external_outcome_unknown`。无供应商查询或幂等能力时，不自动盲目重试并再次计费。

`job get/status` 必须返回安全、可用于 retry preflight 的：

```text
unknown_external_operations[]
  external_operation_id
  provider
  safe_effect_summary
  known_cost_or_side_effect_summary
```

它不返回原始请求、Secret 或不必要的供应商响应。retry request 中的确认列表引用原 Job 的 unknown operation；新 Job 真正发起调用时生成新的 `external_operation_id`。

RetryService 必须拒绝不存在、重复、属于其他 Job、已经完成确定性对账或遗漏任一待确认 unknown operation 的 ID。只有原 Job 当前全部待确认 operation 都被逐项覆盖时，才允许创建新 Job。

### 10.6 前台与后台模式

目标架构最终支持以下模式，但按阶段引入：

- Phase 3A/3B：只承诺 `--wait`；调用方等待最终结果并流式接收事件，此阶段没有 Engine 时 CLI 通常取得 scheduler lease 并在当前进程执行；
- Phase 3C：Desktop 可以通过会话绑定的临时 Desktop API 执行任务；正常关闭先请求取消并使 Job 进入 `cancelled`，宿主异常消失则把当前 Attempt 标记 `interrupted`，Job 在下次 reconcile 时按策略创建新 Attempt、进入 `waiting_for_input` 或进入 `failed`；
- Phase 4：增加 `--detach`，提交到 on-demand Engine 并立即返回 Job ID；
- Phase 4：Desktop 可以提交退出后继续运行的后台任务；
- Phase 4：Production MCP 只提交 Job，不在 MCP stdio 生命周期内直接执行小时级任务。

`--wait` 定义调用方行为，不固定执行所有者。Phase 4 中若 Engine 已持有 scheduler lease，CLI 必须向 Engine 提交并作为等待客户端，不能为坚持“当前进程执行”而绕过 Engine。各阶段使用相同 Job、Attempt、Artifact 和 Recipe 语义；前台或会话绑定模式中断时不能留下永久 `running`，Phase 4 的后台模式才承诺客户端断开后继续执行。

### 10.7 按需 Engine 生命周期

Phase 2 临时 Desktop API 继续随 Desktop 启停。生产 Engine 在出现以下能力时引入：

- `--detach`；
- Desktop 关闭后继续任务；
- Production MCP 长任务；
- 多客户端提交；
- 需要统一 GPU、Whisper 或供应商并发调度。

Engine 行为：

- 第一个后台 Job 提交时按需启动；
- 只允许当前操作系统用户连接；
- 客户端可断开并重新连接；
- Job 运行时不因 Desktop 退出而终止；
- 无运行/排队 Job 且超过空闲时间后退出；
- 不随系统开机预加载模型；
- Runtime 更新前等待 Job drain 或保留旧版本执行环境。

### 10.8 并发与资源调度

不能仅用通用线程池大小控制所有任务。ResourceScheduler 至少理解：

```text
cpu.parse
cpu.ffmpeg
gpu.local
whisper.model.<id>
provider.<id>.concurrency
provider.<id>.rate-limit
agent.<id>
index.io
workspace.publish
```

资源租约具有持有者、超时和恢复规则。Engine 崩溃后不得永久保留幽灵租约。

### 10.9 多 Workspace

Desktop UI 在 Phase 2 可以只有一个活动 Workspace，但 Core、JobService 和 Engine 不得使用全局单活动 Workspace。每个 Job 显式绑定 `local_workspace_instance_id` 和授权 Workspace handle，以便 CLI、MCP 和未来多个任务并行服务不同 Workspace。

## 11. ModelExecutor 与 AgentExecutor

### 11.1 必须分离的原因

模型生成通常是：

```text
structured input -> model -> text/structured output
```

Agent 执行通常是：

```text
goal -> plan -> read/search/tool calls -> intermediate artifacts -> final artifacts
```

两者在权限、生命周期、事件、成本和失败方式上不同，不能用一个 `UniversalGPT` 接口表达。

### 11.2 ModelExecutor

ModelExecutor 至少支持：

- 单次和流式生成；
- 结构化输出 schema；
- 模型和供应商选择；
- token、费用、速率限制；
- timeout 和取消；
- provider request ID；
- 响应使用量；
- 数据发送目标声明；
- 不自主调用本机工具。

适用场景：

- 摘要；
- 分类；
- 大纲；
- 结构化抽取；
- 基于明确 Evidence 的 Markdown 草稿生成；
- 受限质量评价。

### 11.3 AgentExecutor

AgentExecutor 至少支持：

- 动态工具调用；
- 多轮执行；
- 结构化事件；
- 用户审批或中断；
- 时间、token、费用和工具次数预算；
- 受限工作目录；
- 结构化 Artifact 输出；
- 取消和超时；
- 可审计的最终执行摘要。

适用场景：

- UE5 模块知识生产；
- 代码库研究；
- 多来源事实核验；
- 需要检索、编译或读取项目状态的专业 Recipe。

### 11.4 ExecutionGrant

每个 Agent Attempt 必须得到显式 ExecutionGrant：

```text
readable_artifact_ids
readable_external_roots
writable_staging_root
allowed_tools
allowed_network_targets
credential_profile_ids
max_wall_time
max_tokens_or_cost
max_tool_calls
approval_policy
```

约束：

- Agent 唯一默认可写目录是当前 Attempt staging；
- 代码仓库按指定 root 和 revision 只读授权；
- Agent 不获得 Published Vault 写权限；
- Agent 不获得 `apply-publish`；
- Agent 输出必须经过 Artifact 校验和 Quality Gate；
- 来源内容中的文字不能扩大 ExecutionGrant。

### 11.5 Codex 适配路线

当前 Codex app-server 作为 Markdown 文本生成器的兼容实现可以保留到视频流程迁移完成，但不作为最终抽象。

后续分别提供：

- Codex automation/SDK Adapter：面向批处理、CI 和无 UI Job；
- Codex app-server Adapter：面向需要线程、工具事件、审批和富交互的 Desktop 场景；
- Codex CLI discovery：只发现用户已经安装且明确授权的 Codex，不重新分发 Codex 本体。

两种 Adapter 都实现 AgentExecutor 语义，不能把 Agent 事件压缩成普通 Chat Completion 后再丢失工具、审批和 Artifact 信息。

### 11.6 Agent 不等于控制平面

Workflow/Recipe 决定可执行边界，Agent 只是在某个 Step 内动态完成受控目标。Agent 不得：

- 自行安装 Capability；
- 修改 Recipe；
- 扩大权限；
- 改变发布目标；
- 直接更新 JobStore；
- 直接宣布 Quality Gate 通过；
- 直接发布正式知识。

## 12. Quality Gate 与“高质量”定义

### 12.1 质量不是一个 Prompt

每个产生 Draft 的 Recipe 必须同时产生 QualityReport。QualityReport 至少区分：

- `pass`：满足该 Recipe 的强制 Gate；
- `warning`：允许进入审阅，但必须展示；
- `fail`：不得创建 PublishPlan；
- `not_applicable`：该 Gate 对当前 Recipe 不适用并有明确原因。

### 12.2 Gate 顺序

默认顺序为：

```text
确定性结构检查
  -> Artifact/路径/资源检查
  -> Evidence 和引用检查
  -> 领域事实检查
  -> 可选模型评价
  -> 人工审阅
  -> Publisher
```

LLM-as-judge 只能作为辅助信号，不得成为唯一质量证明。

### 12.3 通用 Gate

首版至少支持：

- Artifact hash 和 schema 有效；
- Markdown 可解析；
- frontmatter 满足目标类型要求；
- 内部链接和附件可解析；
- EvidenceRef 可定位；
- 关键结论 Evidence 覆盖率；
- 无明显重复章节；
- 无未处理生成占位符；
- 来源、Recipe 和生成摘要存在；
- 远端来源新鲜度可识别；
- 许可、版权或隐私警告已记录；
- 不包含已识别 Secret；
- 目标 Published Vault 路径可安全规划。

### 12.4 Recipe 专用 Gate

示例：

- 视频：章节时间范围单调、时间戳不越界、字幕覆盖率；
- PPT/PDF：Slide/页码引用存在，OCR 低置信度区域被标记；
- 网页/Wiki：来源 revision、抓取完整性、外链或付费墙限制；
- 代码/UE5：commit 固定、symbol 存在、文件行定位有效、事实与源码一致；
- 工作摘要：时间范围明确、来源记录去重、敏感信息策略通过。

### 12.5 评测基线

每个官方 Recipe 在发布或升级前必须运行固定评测集。至少记录：

- 来源采集成功率；
- Draft 生成成功率；
- EvidenceRef 可解析率；
- 关键结论引用覆盖率；
- 已知事实错误率；
- 用户接受并发布比例；
- 用户编辑距离；
- 失败恢复成功率；
- 平均耗时、token 和费用；
- 不同模型/Recipe 版本的回归差异。

模型或 Prompt 升级不能只验证“接口仍返回 Markdown”。

### 12.6 personal 与 common 的质量差异

`wiki/personal` 默认要求用户审阅和强制 Gate 通过。

`wiki/common` 额外要求：

- 显示完整 diff；
- 显示来源和许可信息；
- 更高 Evidence 覆盖阈值；
- 不包含个人 Secret 或私密上下文；
- 额外明确确认；
- 不把“common”解释为已经上传互联网。

## 13. Review 与 Publisher

### 13.1 Review 是独立产品阶段

ReviewService 展示：

- Source 与 Draft 对照；
- Evidence 定位和跳转；
- QualityReport；
- 未解决 warning；
- Draft 版本差异；
- 预期发布范围；
- 远端模型/Agent 使用摘要。

用户可以：

- 基于当前 Draft 创建并编辑新版本；
- 重新运行全部或部分 Recipe；
- approve；
- reject；
- 将旧 Draft 标记 superseded；
- 请求 PublishPlan。

ReviewService 不直接写 Published Vault。

Review 使用 copy-on-write：AllToNote UI/CLI 对 Draft 的“编辑”必须从旧 Draft 创建新的 immutable Draft Artifact/version，旧 Artifact 不原地修改。新版本初始为 `unreviewed`，必须重新生成绑定新内容 hash 的 QualityReport；approve、reject 和 supersede 也都绑定操作时的精确 Draft content hash。

外部编辑器若直接改动 portable committed Draft 文件，完整性校验必须报告 hash mismatch，并要求把当前内容导入为新 Draft 版本；不能静默把旧 Artifact ID 解释为新内容。正式知识 Markdown 仍可由 Obsidian 等外部工具按开放磁盘合同直接编辑，这与 Draft Artifact 的不可变提交语义是两个边界。

### 13.2 Publisher 边界

PublishService 只负责把已批准 Draft 转换为 iwiki Publish Request，并调用：

```text
iwiki plan-publish
iwiki apply-publish
```

或后续等价稳定协议。

以下语义继续由 iwiki 拥有：

- Workspace schema；
- 目标路径规则；
- personal/common scope；
- base/current/proposed hash；
- 冲突；
- 原子写入；
- 发布事务恢复；
- 索引刷新语义。

AllToNote 不复制第二套发布实现。

### 13.3 正式附件

正式 Markdown 必须能够在 Published Vault 中独立显示。Publisher 将正式文档依赖的附件复制或 materialize 到 iwiki 约定的 Published Vault 资产目录，例如 `wiki/_assets/`，不能要求 Obsidian 通过 `../raw` 或 AllToNote 私有 API 才能显示。

### 13.4 冲突与重入

PublishPlan 包含目标文件 precondition hash。应用前必须重新检查：

- hash 一致：允许 apply；
- hash 不同：返回 conflict；
- 新文件目标已存在：返回 conflict；
- Plan 过期或对应 Draft 已 superseded：拒绝 apply；
- 重复 apply 已 committed Plan：返回已有结果，不重复写入。

不做自动语义合并。首版允许重新规划、另存为、或用户使用外部编辑器手工合并。

### 13.5 开放磁盘的权限承诺

AllToNote 的正式承诺限定为：

> 通过 AllToNote Core、受限 CLI/MCP profile 和 Desktop 触发的生产流程，Draft 不能绕过 Publisher 写入 Published Vault。

拥有操作系统写权限的 Obsidian、编辑器、shell 或外部 Agent 仍可直接修改 Markdown。AllToNote 通过 watcher、reconcile、hash precondition 和审计检测变化，而不是声称拥有全局强制写权限。

## 14. CLI 公共自动化合同

### 14.1 定位

CLI 是最稳定的一等公共入口，必须满足：

- 不安装、不启动 Desktop 也能使用；
- 业务逻辑不写在 CLI handler；
- 非交互模式不等待隐藏输入；
- stdout 和 stderr 分离；
- JSON 和事件协议版本化；
- 错误使用稳定 code；
- 路径和 Workspace Grant 与其他入口一致。

### 14.2 推荐目标命令面

下列命令描述稳定公共接口的目标形态，实际可用范围遵循第 26 节发布阶段；列出 `--detach` 和 MCP 不代表 Phase 3 提前承诺 Engine。

```text
# Runtime 与能力
alltonote runtime info --json
alltonote capability list --json
alltonote capability inspect <id> --json
alltonote capability doctor <id> --json

# Recipe
alltonote recipe list --json
alltonote recipe describe <id>@<version> --json

# 通用执行
alltonote run <recipe> --input <key=value> --workspace <path> --wait
alltonote run <recipe> --request <request.json> --detach --json

# Job
alltonote job status <job-id> --json
alltonote job list --json
alltonote job wait <job-id> --json
alltonote job events <job-id> --jsonl --follow
alltonote job cancel <job-id> --json
alltonote job respond <job-id> --challenge <challenge-id> --response <response.json> --json
alltonote job retry <job-id> [--request <retry-request.json>] [--json]

# Artifact 与 Draft
alltonote artifact show <artifact-id> --json
alltonote artifact lineage <artifact-id> --json
alltonote draft list --json
alltonote draft show <draft-id> --json
alltonote draft create-version <draft-id> --from <path> --json
alltonote draft check <draft-id> --json
alltonote draft approve <draft-id> --content-hash <sha256> --json
alltonote draft reject <draft-id> --content-hash <sha256> --reason <text> --json
alltonote draft supersede <draft-id> --by <new-draft-id> --content-hash <sha256> --json

# 发布
alltonote publish plan --draft <draft-id> --target personal --json
alltonote publish apply --plan <plan-id> --json

# MCP
alltonote mcp serve --profile knowledge-readonly --stdio
alltonote mcp serve --profile producer --stdio
```

用户友好别名可以提供：

```text
alltonote video <url>
alltonote article <url>
alltonote ppt <path-or-url>
```

别名只构造标准 Recipe Request，不拥有独立业务语义。

### 14.3 输出合同

- `--json`：stdout 输出一个版本化最终 envelope；
- `--jsonl`：stdout 每行一个版本化事件；
- 人类模式：stdout 输出简洁结果，进度可使用终端表现层；
- stderr：诊断、warning 和 debug；
- 大正文默认通过 Artifact、`--output` 或显式读取命令访问，不强制嵌入单个巨大 JSON；
- Secret 不允许作为命令行参数，只允许 Credential Profile 名称；
- CLI 退出码和 envelope error code 分别稳定。

`job retry` 不把原 Job 或 Attempt 改回 `running`，而是创建带 `retry_of_job_id` 的新 Job 并返回新 Job ID。版本化 retry request 至少包含：

```json
{
  "retry_request_schema_version": 1,
  "client_request_id": "...",
  "expected_original_job_state": "failed",
  "confirm_external_side_effect_operation_ids": []
}
```

自动化调用方必须提供新的 `client_request_id`；`expected_original_job_state` 防止在调用方依据的状态已变化后盲目 retry。原 Job 存在 `external_outcome_unknown` 时，每个可能重复计费或产生副作用的 operation ID 都必须显式出现在确认列表中，不能使用可跨 Job/operation 复用的全局布尔开关。

人类 TTY 模式可以省略手写 request：CLI 先通过 `job status` 读取 unknown operations，在提交前本地生成并持久复用 `client_request_id`，显示原 Job 状态和每个安全 operation 摘要，取得逐项确认后构造同一版本化请求。非交互或 `--json` 模式必须提供 `--request`，且不进行交互式补全。

`job respond` 只响应当前 PendingChallenge，并在成功后创建新 Attempt；它不是把旧 Attempt 改回运行。response 文件必须符合 challenge schema，Secret 只能通过 Credential Profile ID 间接引用。

Draft 新版本、check、approve、reject 和 supersede 都是 Automation Protocol 的正式用例，不是 Desktop 私有动作。Review 动作必须提交期望的 Draft content hash；不一致时返回冲突，不能批准调用方未看到的内容。

### 14.4 协议兼容

CLI JSON 属于 AllToNote Automation Protocol。新增可选字段允许向后兼容；删除字段、改变语义或改变错误 code 需要新的主协议版本。

人类文本输出不作为自动化合同。

## 15. MCP 架构

### 15.1 不把 MCP 当内部消息总线

MCP 是面向 Agent 的外部适配器，不是 Core、Engine、llm-iwiki 和 Desktop 之间的内部 RPC。AllToNote 必须先有稳定 Job、Artifact 和 Publish API，再映射为 MCP。

### 15.2 Knowledge Read-only Profile

知识读取优先使用 iwiki MCP 或由 AllToNote 聚合 iwiki 能力。

Resources/Resource Templates 用于：

- Published Markdown；
- Workspace metadata；
- 文档来源摘要；
- index status；
- 可公开的 Artifact lineage 摘要。

Tools 用于：

- `knowledge_search`；
- `knowledge_related`；
- `knowledge_sources`；
- `knowledge_recent`。

该 Profile 默认不读取 `raw/personal`。

### 15.3 Production Profile

Production MCP 提供：

- `recipes_list`；
- `recipe_describe`；
- `job_start`；
- `job_get`；
- `job_cancel`；
- `job_respond`；
- `job_events` 或事件查询；
- `artifact_get`；
- `draft_get`；
- `publish_plan`。

首版禁止：

- 任意 shell；
- 任意绝对路径；
- 任意环境变量；
- 读取 Credential 明文；
- 任意 Feature Pack 安装；
- `publish_apply`；
- 直接写 `wiki/personal`；
- 写 `wiki/common`。

### 15.4 Workspace Grant

每个 MCP Server 实例或调用上下文必须绑定显式 Workspace Grant：

- local workspace instance；
- 允许读取的 scope；
- 是否允许查看 Draft；
- 是否允许启动付费任务；
- 是否允许响应 Job challenge；
- 可用 Recipe；
- 预算上限；
- 过期时间。

不得依赖“最近在 Desktop 打开的 Workspace”。

### 15.5 长任务映射

AllToNote 的稳定内部模型始终是：

```text
job_start -> job_id
job_get(job_id)
job_cancel(job_id)
job_respond(job_id, challenge_id, response)
job_result(job_id)
```

如果 MCP client/server 协商支持稳定的 Task 能力，可以把内部 Job 映射为 MCP Task；不支持时仍返回 Job ID 并通过 Tools 查询。`job_respond` 只有在 Workspace Grant 明确允许、challenge schema 校验通过时可用，且同样只能引用 Credential Profile。MCP stdio 进程不直接承载小时级工作。

### 15.6 远端公共知识

Remote Knowledge Provider 与本地 MCP 分开管理：

1. 临时查询：结果只用于当前任务，不写 Workspace；
2. 来源导入：响应按许可允许的范围形成 SourceRevision 和 Evidence；
3. 公共知识包：只读安装，用户修改时派生到 personal；
4. 任何远端结果都不能自动发布到 common。

## 16. Desktop 与网站边界

### 16.1 Desktop 信息架构

长期一级导航按产品价值排序：

1. `Produce`：提交来源并选择知识方案；
2. `Runs`：查看运行、进度、失败和恢复；
3. `Review`：来源、证据、质量和 Draft 审阅；
4. `Library`：最小知识浏览、阅读和搜索；
5. `Settings`：Runtime、Pack、模型、凭据和系统集成。

Phase 2 可以先交付 Library，但不改变长期产品中心。

### 16.2 Desktop 职责

Desktop 负责：

- 目录和文件选择；
- Runtime/Engine 安装、发现和生命周期；
- 输入与 Recipe 选择；
- 隐私、能力、预计成本和资源预览；
- Job 事件展示；
- Source/Draft/Evidence/Quality 对照；
- PublishPlan diff 和用户确认；
- 打开 Obsidian 和系统文件位置；
- 受限平台集成。

Desktop 不负责：

- 下载、转写、OCR、模型或 Agent 业务实现；
- 任务持久化；
- Artifact 提交；
- Publisher 路径规则；
- 直接扫描 Workspace 形成第二套规则；
- 任意命令执行；
- 返回完整环境变量。

### 16.3 Library 范围 Gate

Library 首轮只做：

- 选择和打开 Workspace；
- 文件树；
- 安全 Markdown 阅读；
- 搜索；
- 打开 Obsidian；
- 明确显示索引状态。

Mermaid、KaTeX、代码高亮、Callout 和本地附件属于优秀阅读体验；编辑器、图谱、多 Pane、数据库视图和通用插件系统不属于近期范围。

### 16.4 网站职责

网站继续只承担：

- 账号、登录和邀请；
- 设备和许可证；
- 下载、更新通道和公告；
- 公共 Recipe/Feature Pack 目录；
- Remote MCP/公共知识服务目录；
- 可选订阅、托管模型额度和企业服务。

本地基础 CLI、BYOK、本地模型、Workspace 读取和用户自有 Markdown 不依赖每次联网登录。许可证使用有期限的离线 entitlement，而不是每条命令在线鉴权。

## 17. Runtime、Feature Pack 与分发

### 17.1 Base Runtime

Base Runtime 包含：

- Core；
- CLI；
- Job/Artifact 基础合同；
- on-demand Engine launcher/client；
- Desktop bridge；
- MCP adapter；
- Credential、Process、Filesystem 平台适配；
- iwiki 兼容检查和托管安装信息。

不包含：

- 模型权重；
- 本地 Whisper 模型；
- 全部文档解析/OCR 依赖；
- Codex 本体；
- UE5 工具链；
- 所有未来 Source Connector。

### 17.2 推荐 Feature Pack

```text
video
local-whisper
document
ocr
web-wiki
codex-agent
ue5-knowledge
personal-work-digest
```

一个 Pack 可以提供多个 Capability，但不能在安装时修改 Core 业务规则。

### 17.3 架构独立与共同分发

Desktop、Runtime 和 iwiki 保持逻辑独立，但默认发布单元是经过兼容测试的组合：

- Desktop 在线安装器；
- CLI-only 安装器；
- 离线组合安装器；
- 单独 Feature Pack；
- 高级用户覆盖系统已有兼容 iwiki 的入口。

普通用户不需要手工维护三个 PATH 和兼容矩阵。共同分发不意味着代码合并；iwiki 仍是独立可执行程序、独立协议和可被其他 Agent 直接调用的能力。

### 17.4 更新与回滚

更新系统必须支持：

- 签名 manifest；
- 下载 hash 校验；
- A/B 或 last-known-good 版本槽；
- 失败自动回滚；
- 运行中 Job 固定 Runtime/Pack 版本；
- 更新前等待 Job drain，或保留旧版本完成 Job；
- Base Runtime 与 Pack 兼容范围；
- 用户可控更新通道；
- 离线 Pack 安装和校验；
- 不因 Desktop 更新而删除 CLI、Runtime 或 Workspace。

### 17.5 平台优先级

Windows 10/11 x64 为 Tier 1，必须覆盖：

- 中文、空格、长路径和不同盘符；
- Credential Manager/DPAPI；
- process tree 取消；
- reparse point/junction；
- 杀毒软件和下载隔离常见行为；
- 签名安装和更新；
- 离线安装、断点续传和可替换镜像。

macOS Apple Silicon 为 Tier 2，必须单独通过：

- 签名和 notarization；
- Keychain；
- Unix Domain Socket；
- APFS/FSEvents；
- Pack 和外部程序权限；
- 完整端到端测试。

## 18. 安全与隐私模型

### 18.1 信任边界

系统区分：

- 用户明确选择的本地文件；
- 不可信远端来源内容；
- 官方可信 Capability；
- 用户显式信任的外部 Agent；
- 远端模型和 MCP；
- 不可信第三方 Pack（首版不支持）。

来源内容永远作为数据，而不是系统指令或权限声明。

### 18.2 路径安全

所有来自 CLI、Desktop、MCP、Markdown、Artifact 或远端元数据的路径统一经过 PathPolicy：

- 拒绝未授权绝对路径；
- 拒绝 `..` 越界；
- 规范化 Unicode 和 URL encoding；
- 校验 Windows UNC、设备路径、junction、reparse point；
- 校验 macOS symlink；
- 最终句柄/目标仍在授权根目录；
- 防御 check/use 间目标替换；
- 发布只能由 iwiki 在 Workspace 合同内执行。

### 18.3 网络采集安全

Source Connector 必须定义：

- 允许协议；
- localhost、内网和 link-local 策略；
- redirect 数量和跨域规则；
- DNS rebinding/SSRF 防护；
- MIME sniffing；
- 最大下载大小；
- 解压炸弹；
- 连接和总超时；
- Cookie/Credential Profile；
- 版权和登录限制；
- 失败时的结构化状态。

抓取失败不能被转换成空文档后继续生成。

### 18.4 Prompt Injection

网页、PDF、字幕、Wiki 和代码注释中的指令都属于不可信来源数据。系统 Prompt、Recipe Policy 和 ExecutionGrant 由控制平面提供，来源内容不能：

- 修改允许工具；
- 请求 Secret；
- 扩大可读目录；
- 改变网络出口；
- 修改发布目标；
- 禁用 Quality Gate；
- 触发 common 发布。

### 18.5 凭据

凭据保存于 Windows Credential Manager/DPAPI 或 macOS Keychain。Job、Artifact 和 CLI 只保存 `credential_profile_id`。

CredentialBroker 只向当前 Step 注入声明需要的最小 Secret，并且：

- 不继承全量父进程环境；
- 不写 stdout/stderr；
- 不进入 run summary；
- 不进入 crash report；
- 不返回给 React 或 MCP；
- 任务开始前向用户显示目标服务和数据范围。

### 18.6 进程隔离的真实边界

首轮执行分为：

1. in-process：可信纯函数、schema 校验和小型转换；
2. trusted subprocess：FFmpeg、Whisper、Pandoc、Codex 和官方 Pack；
3. untrusted third-party process：不在首轮承诺范围。

普通子进程、Windows Job Object 或 manifest 权限声明都不等于真正安全沙箱。第三方市场必须在单独的 Windows/macOS 隔离设计通过后才能开放。

ProcessSupervisor 只能启动已注册 Capability manifest 解析出的固定可执行入口，并对参数执行类型化校验；用户输入、Markdown、远端 MCP 响应和模型输出都不能直接选择任意二进制、拼接 shell 命令或扩大工作目录。外部可执行程序必须先由用户显式注册，注册不等于获得沙箱保证。

子进程 containment 不能依赖 ProcessSupervisor 一直存活：

- Windows Tier 1 对可控 Worker 使用 Job Object，并在语义允许时启用 kill-on-job-close 或等价受控生命周期；
- macOS Tier 2 使用独立 process group 加 watchdog pipe/launcher，明确父进程死亡后的收敛方式；
- 不能随 owner 死亡立即终止的外部程序，必须持久化包含进程创建时间、可执行身份、Capability 和 Attempt 的可验证 process identity，不能只记录可能复用的 PID；
- RecoveryCoordinator 在下一次 reconcile 时验证身份并回收遗留进程；不能验证归属时先隔离 staging 和提交权限，不盲目 kill 无关进程；
- 孤儿 Worker 即使暂时继续运行，也会因 scheduler/Attempt fencing token 失效而无法 checkpoint 或 portable commit Artifact。

因此系统分别承诺“受 OS containment 的进程随 owner 关闭而终止”和“其他进程在下一次 reconcile 时回收”；不能笼统承诺强杀 CLI/Engine 后所有外部程序都会立即消失。

### 18.7 本地传输

Phase 2 临时 Desktop API 可以使用随机 loopback 端口和内存 token。长期 Engine 优先使用：

- Windows Named Pipe + 当前用户 ACL；
- macOS Unix Domain Socket + 文件权限。

若使用 Streamable HTTP，必须绑定 localhost、验证 Origin、使用认证并禁止无意暴露到局域网。

### 18.8 现有 Tauri 收敛要求

生产 Desktop 必须移除或替换现有：

- 完整环境变量读取；
- 任意程序执行；
- 任意可执行文件发现；
- 向 React 暴露 Secret；
- 固定完整后端 sidecar 假设。

Tauri 只暴露按用例白名单化的 typed command 和 event。

## 19. 轻量与性能设计

### 19.1 “轻量”的定义

轻量不是只看 `exe` 文件大小，而是同时满足：

- Desktop 冷启动和空闲资源低；
- CLI 元数据命令冷启动快；
- 基础安装不携带所有模型和重依赖；
- 空闲时不常驻重型 Worker；
- 重任务不阻塞 UI；
- 用户只为实际使用的能力下载 Pack；
- 任务失败后不需要从头重复昂贵步骤。

### 19.2 冷路径隔离

以下命令不得导入 Whisper、Torch、AV、OCR、视频下载器或 Agent SDK：

- `alltonote --help`；
- `runtime info`；
- `capability list`；
- Workspace inspect/tree/read；
- 已有索引的 search；
- Job status/list。

Pack 仅在 Recipe 解析后由 CapabilityRunner 加载。

### 19.3 重型 Worker 生命周期

- 重型能力在独立 Worker 进程运行；
- 任务完成后可以立即退出并释放显存；
- 对连续同类任务允许短 TTL warm pool，但必须有内存/显存上限；
- Engine 空闲不保持模型常驻；
- 大型模型和 Pack 不进入 Desktop 更新包。

### 19.4 I/O 与流式处理

- 大媒体和文档使用流式下载、hash 和解析；
- 不把完整视频或大 PDF 读入 UI 内存；
- Artifact 内容通过文件/stream 传递，DTO 只传 metadata 和 handle；
- 事件流具备背压、分页或游标；
- 搜索和 tree 继续遵循 Phase 2 的分页与虚拟化设计。

### 19.5 缓存原则

- 只缓存已定义可重用语义的 Step；
- cache key 包含完整版本和输入 hash；
- 缓存损坏必须可检测；
- 缓存命中不跳过 Artifact 校验；
- 用户可以清理缓存而不删除长期资产；
- 活动 Job、活动发布事务和 portable committed Artifact 不能被普通“清缓存”删除。

### 19.6 性能 Gate

Phase 2 的 10,000 文档 p95 指标继续有效。Knowledge Compiler 下位计划必须额外为固定测试集量化：

- Base Runtime/CLI 冷启动；
- Job submit/status 延迟；
- Event 重连和重放；
- 视频各 Step 耗时；
- Artifact cache hit；
- Worker 峰值内存/显存；
- 多任务资源调度；
- Engine 空闲退出；
- Pack 安装、升级和回滚。

在没有基准机和 profiling 数据前，不在本文虚构具体数值；下位实施计划必须先冻结基准机、fixture 和测量脚本，再设硬 Gate。

## 20. 可观测性与隐私

### 20.1 结构化事件

Job 事件至少包含：

```text
event_schema_version
event_id
job_id
step_id/attempt_id（可选）
sequence
timestamp
kind
status/progress
stable_code
safe_summary
artifact_refs
```

sequence 在单个 Job 内单调，客户端可按 cursor 重连和补读。事件不以内存 SSE 为唯一来源。

### 20.2 日志分层

- 用户事件：可安全展示；
- 诊断日志：本机保存、默认脱敏；
- 子进程 stderr：按 Attempt 归档并过滤 Secret；
- portable run summary：只保存长期解释结果所需信息；
- 产品遥测：默认不包含知识正文、Prompt、转录、文件路径或 Secret。

### 20.3 关键指标

产品和工程指标至少覆盖：

- 从安装到第一篇正式知识的时间；
- Source 采集成功率；
- Draft 接受率和编辑距离；
- Evidence 覆盖率；
- Job 成功、取消、恢复和 unknown outcome 比例；
- 每个 Recipe 耗时、token 和费用；
- Artifact cache 命中；
- Worker 崩溃率；
- CLI-only 成功率；
- MCP Job 最终产生可审阅 Draft 的比例；
- 发布冲突率；
- 发布后被 Obsidian、Git 或 Agent 使用的可选本地统计。

默认不上传个人知识正文。任何外发遥测必须单独说明字段、目的和关闭方式。

## 21. 错误、故障恢复与一致性

### 21.1 稳定错误分类

跨 CLI、Desktop 和 MCP 的顶层错误至少分为：

```text
invalid_input
workspace_not_granted
workspace_incompatible
capability_unavailable
policy_denied
credential_unavailable
source_unavailable
external_service_failed
external_outcome_unknown
artifact_invalid
job_conflict
job_store_corrupted
publish_conflict
cancelled
engine_incompatible
internal_error
```

每个错误包含：

- stable category；
- specific code；
- 是否可重试；
- 安全的人类说明；
- 用户可执行下一步；
- correlation ID；
- 可选的 job/step/attempt 引用。

空内容、空 Markdown 或 `None` 不能作为成功结果。公共错误体不得包含 Secret、Cookie、完整环境变量或不必要的绝对路径。

### 21.2 重试分类

每个 Step/Capability 必须声明：

- `pure_idempotent`：可自动重试；
- `side_effect_idempotent`：有稳定 idempotency key 时可自动重试；
- `non_idempotent_or_billable`：默认不自动重试；
- `manual_reconciliation`：结果未知，需要外部查询或用户决定。

系统不承诺外部模型、Agent 或网站调用的 exactly-once。系统承诺记录 Attempt、使用可用的幂等能力，并在结果未知时停止而不是盲目重复副作用。

### 21.3 Engine 崩溃恢复

Engine 启动时执行 reconcile：

1. 获取单一调度所有权；
2. 查找 lease 已过期的 running Attempt；
3. 检查是否已有通过校验的 portable committed Bundle，若有则按 output contract 对账 Job 终态；
4. 只有本 Job 的 checkpointed Artifact 时，校验 fencing/provenance 后供恢复复用，但不对外发布；
5. 无完整 checkpoint 时按 retry classification 决定创建新 Attempt、失败或创建 PendingChallenge；
6. 将失去执行进程且没有完成证据的 Attempt 标记为 interrupted；
7. 清理或隔离不完整 staging；
8. 终止确认属于旧 Attempt 的遗留子进程树；
9. 不因恢复自动创建 PublishPlan 或执行发布。

不能把所有旧 `running` 状态简单重置为 `queued`。

### 21.4 单一调度所有权

同一 JobStore 任一时刻只有一个 active scheduler lease：

- Engine 已持有 lease 时，CLI 向 Engine 提交；
- Engine 不存在且使用 `--wait` 时，CLI 可以临时成为 scheduler owner；
- 两个 CLI 不得同时领取同一 Job；
- lease 具有 owner、heartbeat、expiry 和 fencing token；
- 旧 owner 恢复后不能继续提交新 Artifact；
- SQLite lock 失败必须返回明确冲突，不通过第二个数据库副本绕过。

### 21.5 取消语义

取消流程：

1. cancel 以 JobStore 中的条件更新设置 `cancellation_requested`，与 bundle commit guard 竞争同一顺序；
2. Job 已经终态时返回现有终态，不回写 cancelled；
3. 取消先获胜时，当前 Attempt 收到协作式取消，未开始 Attempt 进入 cancelled；
4. 超过 grace period 后由 ProcessSupervisor 终止整个受控子进程树；
5. staged 输出和取消后迟到的 checkpoint/commit 请求被拒绝并隔离；取消前已有 checkpointed Artifact 只留作诊断或显式重试复用，不对外可见；
6. portable commit 先完成时资产保留且 Job 成功终态获胜；取消先获胜时 Job 在所有 Attempt 收敛后进入 `cancelled`；
7. 取消绝不触发 Publisher。

取消后的重新生产使用新的 Job 或显式 retry/clone，不把旧 cancelled Job 改回 running。

### 21.6 Draft 新版本与失效规则

Draft 的审阅、QualityReport、approval 和 PublishPlan 都绑定精确内容 hash：

- AllToNote 内的编辑通过 copy-on-write 创建新的 immutable Draft Artifact/version；
- 新版本创建后，旧 QualityReport 对新版本无效；
- 旧 approval 失效；
- 旧 PublishPlan 不能 apply；
- 重新检查后产生新的 QualityReport；
- 重新批准后才能创建新 Plan。

外部程序直接改变 portable committed Draft 文件时必须先报告 hash mismatch，再把当前内容导入为新版本；不能原地更新旧 Artifact envelope。文件名不变不能被当作内容未变。

### 21.7 磁盘故障

Artifact/Bundle 提交必须通过故障注入证明以下中断后只会出现完整旧版本或完整新版本：

- 写入中磁盘满；
- 内容文件已写完但 manifest 未提交；
- manifest 提交前进程退出；
- atomic rename 失败；
- 杀毒软件临时占用；
- staging 与目标不在同卷；
- 外部程序同时修改目标。

声明完成但缺少核心文件的 Bundle 必须被隔离并返回 `artifact_invalid`。

### 21.8 Runtime/Pack 版本缺失

Job 固定 Recipe、Capability、Runtime major 和必要 Pack 版本。恢复时若原版本不可用：

- 可重新安装原版本时，Job 进入 `waiting_for_input` 并携带 error category=`capability_unavailable` 的 PendingChallenge；
- 原版本不可恢复、challenge 过期或用户终止时，Job 进入 `failed`，error category 仍为 `capability_unavailable`；
- 不静默使用新版本；
- 用户可以安装旧版本、显式迁移为新 Job 或终止；
- portable committed Artifact 保持可读；
- Runtime 更新器不得删除仍被活动 Job 引用的版本。

### 21.9 JobStore 损坏

JobStore 无法通过完整性检查时必须 fail closed：

1. scheduler 停止领取和创建 Job；
2. 损坏数据库及可用 WAL/sidecar 被只读隔离和备份，不在原文件上“尽力继续”；
3. 对能确认属于该 JobStore 的活动 Worker 执行 containment/回收，无法确认的进程至少失去 fencing 提交权；
4. portable committed `raw/` 和 `wiki/` 保持不变；
5. 未完成 Job 明确标记为不可恢复，不能根据 staging 猜测为 succeeded；
6. 用户明确确认后才创建新 JobStore；
7. UI/CLI 醒目提示任务历史、未完成恢复点和 `client_request_id` 幂等绑定已经丢失；
8. 若未来支持备份/WAL 恢复，必须由 Job Engine 下位设计定义独立、可故障注入验证的协议。

### 21.10 故障行为矩阵

| 故障 | 默认行为 |
|---|---|
| 网络 timeout/断连 | 按 Step retry policy 退避或失败 |
| 429/供应商限流 | 尊重 retry-after，受预算和取消控制 |
| 鉴权过期 | `waiting_for_input`，不记录明文凭据 |
| 付费请求结果未知 | `external_outcome_unknown`，不盲目重试 |
| Worker 崩溃 | Attempt interrupted，校验 Artifact 后决定恢复 |
| Desktop/CLI/MCP 客户端退出 | detached Job 继续；会话绑定 Job 按第 10.6 节收敛，Job 不使用未声明的 interrupted 状态 |
| Engine 崩溃 | 重启 reconcile；复用有效 checkpoint，不重复 portable committed Bundle |
| 磁盘满 | 不提交半成品，返回可操作错误 |
| Artifact hash 不匹配 | 隔离，不作为输入 |
| Obsidian 并发编辑 | Publish apply conflict，禁止覆盖 |
| iwiki/索引失败 | 发布失败或索引 stale；不伪装成功 |
| Runtime 更新 | 活动 Job 留在原版本，或等待 drain |
| JobStore 损坏 | 停止调度并隔离，保留 portable 资产，提示恢复和幂等历史丢失 |

## 22. 版本与协议治理

### 22.1 独立版本维度

以下版本不能合并为一个软件版本：

1. Workspace schema version；
2. Portable Artifact/Bundle schema version；
3. iwiki protocol version；
4. AllToNote Automation Protocol；
5. Engine client/event protocol；
6. Recipe ID/version；
7. Capability Protocol/version；
8. Feature Pack version；
9. Desktop/Runtime/iwiki 软件版本。

Desktop 不解析 CLI stdout，因此 Desktop/Engine 握手不需要关心 CLI 人类输出版本，只关心 Engine protocol、capability 和 Runtime compatibility。

### 22.2 协议协商

所有进程边界返回：

- protocol min/max；
- software version；
- capabilities；
- supported schema range；
- optional feature flags；
- stable error codes。

调用方根据 capability 使用功能，不根据软件版本字符串猜测。

### 22.3 Portable schema 所有权

Knowledge Compiler Core 定义 Source、Artifact、Evidence、Draft、Quality 和 lineage 的逻辑不变量；它们在 Workspace 中的路径、文件名、序列化、验证和迁移属于开放磁盘合同，必须由 llm-iwiki schema 或双方共同发布的 versioned contract 管理。

AllToNote 不得单方面在 `raw/` 中建立只能由自己解释的私有 Bundle schema。下位 Portable Artifact 设计必须同步提供：

- llm-iwiki 校验；
- JSON/YAML schema；
- golden fixtures；
- 迁移策略；
- 其他 Agent 可实现的公开说明。

### 22.4 Core/SDK 公共稳定性

Core/SDK 在第一阶段是 Runtime 内部同版本接口，不立即承诺稳定 Python 类 API。对外优先稳定：

- CLI JSON/JSONL；
- Engine client protocol；
- MCP；
- portable Workspace contract；
- iwiki protocol。

出现真实嵌入式调用者后，再发布最小 SDK，避免过早锁定内部类结构。

### 22.5 兼容测试矩阵

每个 Runtime release 记录经过测试的：

- Workspace schema 范围；
- iwiki protocol 范围；
- Engine protocol 范围；
- 官方 Pack 范围；
- Windows/macOS 平台范围；
- Desktop compatibility 范围。

用户默认安装经过验证的组合，高级覆盖模式才允许使用系统已有独立组件。

## 23. 测试与验证策略

### 23.1 Core 单元测试

覆盖：

- Source/Revision 不变量；
- Artifact 和 Evidence 校验；
- Recipe 解析和版本固定；
- Job、Attempt、PendingChallenge、Draft 和 Publish 独立状态机；
- staged/checkpointed/portable_committed 可见性与 fencing/cancel 竞争；
- Quality Gate 聚合；
- PathPolicy；
- policy/permission；
- idempotency key；
- stable error mapping。

### 23.2 合同测试

覆盖：

- Portable Bundle schema golden fixtures；
- iwiki inspect/validate/query/plan/apply；
- CLI JSON/JSONL golden tests；
- `job respond` challenge/response、`job retry` 和 `client_request_id` 合同；
- `ExternalOperation` preflight：两个 unknown operation 时，遗漏确认、错误 ID、重复 ID 和其他 Job ID 均拒绝，完整逐项确认才创建新 Job；
- Engine handshake/events；
- Capability manifest；
- ModelExecutor/AgentExecutor fake adapters；
- MCP Resources/Tools 和 Workspace Grant。

### 23.3 Recipe 评测

每个官方 Recipe 有固定 golden corpus、预期 Artifact 集和质量基线。首批分别覆盖：

- 有字幕视频；
- 无字幕需转写视频；
- 超长视频；
- 动态网页和静态文章；
- Wiki revision；
- 文本型和图像型 PDF/PPT；
- 固定 commit 的代码库；
- UE5 多来源模块知识。

### 23.4 端到端验收

至少证明：

1. Phase 3C：只安装 Runtime/CLI、不安装 Desktop，完成视频生产、可选 Draft 新版本创建、Quality 重新检查、approve、personal PublishPlan 和 apply，最终正式 Markdown 可由 iwiki/Obsidian 读取。
2. Phase 4：`--wait` 与 `--detach` 对同一固定请求生成语义等价的 Artifact 集。
3. Phase 4：`--detach` 返回前 Job 已持久化。
4. Phase 4：Desktop 退出后 detached Job 继续，Engine 空闲后退出。
5. 同一 client request ID 重试不重复创建付费 Job。
6. Job succeeded 后 Draft 独立处于 unreviewed。
7. 创建 Draft 新版本或检测到 portable committed Draft 内容 hash 改变后，旧 Quality/approval/Plan 失效。
8. Plan 后 Obsidian 修改目标，apply 返回 conflict。
9. 同一 committed Plan 重复 apply 返回同一结果。
10. Producer、Agent 和 MCP producer profile 都无法经 AllToNote 直接写 `wiki/`。
11. common 发布需要绑定当前 Plan 的额外明确确认。
12. 索引失败不回滚已提交 Markdown。
13. 删除 JobStore 和索引后，已提交 raw/wiki 可被 Obsidian 和 iwiki 使用。
14. Source 中的 Prompt Injection 无法扩大 Agent 权限。
15. CLI、Desktop 和 MCP 对同一错误返回相同稳定 category。
16. Job 进入 waiting_for_input 后，CLI 可用结构化 challenge response 创建新 Attempt 并继续；过期、重复和错误 challenge 确定失败。
17. cancel 与 portable commit 并发时只有一个线性化结果，旧 Worker 和过期 fencing token 不能迟到提交。
18. JobStore 损坏时停止调度、隔离数据库并保留 portable assets，不把 staging 猜测为成功。

### 23.5 故障注入

在下载、转写、模型、Agent、Artifact checkpoint、portable Bundle commit 和 Publish apply 各阶段注入：

- kill worker；
- kill engine；
- kill client；
- 网络断连；
- 429；
- unknown external outcome；
- 磁盘满；
- 权限变化；
- 杀毒软件文件占用；
- symlink/junction 越界；
- Runtime/Pack 升级；
- corrupted Artifact；
- corrupted JobStore；
- duplicate submission。

kill CLI/Engine 的用例必须同时验证 OS containment、孤儿进程回收和 fencing 拒绝迟到提交；corrupted JobStore 用例以第 21.9 节 fail-closed 行为为验收 oracle。

恢复承诺必须由真实故障注入验证，不能只依赖 happy-path 单元测试。

### 23.6 安全测试

- SSRF 和 redirect；
- 压缩炸弹和超大文件；
- 恶意 MIME；
- Markdown/HTML/SVG/Mermaid XSS；
- Prompt Injection；
- Agent path/tool/credential escalation；
- MCP Workspace Grant 绕过；
- Secret scanner 覆盖 JobStore、Bundle、日志、事件和 crash report；
- Named Pipe/Unix Socket 其他用户访问；
- Tauri command allowlist。

### 23.7 性能与分发测试

- 继承 Phase 2 10,000 文档基线；
- Base Runtime 不加载重依赖检查；
- CLI/Desktop/Engine 冷启动；
- Worker 内存和显存；
- 多任务资源租约；
- 大文件流式处理；
- Windows 在线、CLI-only、离线组合安装；
- 镜像切换、断点续传、签名失败和回滚；
- macOS Tier 2 独立 Gate。

## 24. 产品范围 Gate

### 24.1 目标用户优先级

1. Windows 上使用本地 Markdown、Obsidian、Codex/Claude Code 等工具的开发者、工程师、研究者和高强度知识工作者；
2. 通过 CLI/MCP 调用知识生产能力的脚本、Agent 和自动化系统；
3. 需要云端多人协作、移动同步或纯 SaaS 笔记体验的普通团队用户不属于首期目标。

### 24.2 规范用户词汇映射

| 用户概念 | 内部概念 |
|---|---|
| 知识库 | Workspace Root |
| 来源 | Source/SourceRevision |
| 知识方案 | Recipe |
| 生产任务 | Job/Run |
| 草稿 | Draft Artifact |
| 来源证据 | EvidenceRef |
| 正式知识 | Publication/Published Markdown |

这是产品界面、中文文案、帮助文档和用户可见 CLI 文本的唯一一级词汇表。QualityReport 作为草稿的质量信息展示，Review 作为产品阶段和操作集合展示，不新增为独立知识实体。Artifact、Step、Attempt、Executor 和 Capability 只出现在高级诊断、开发文档或设置中，不成为普通用户主导航。

### 24.3 Feature Admission Gate

任何新功能进入 Core Roadmap 前必须同时回答：

1. 是否直接提升“来源到可信正式知识”的速度、质量、可审阅性或可复用性？
2. 是否可以通过 Core/CLI 使用，而不是只能在 Desktop 点击？
3. 是否保持开放 Markdown/Artifact，而不是创造专有事实源？
4. 本地基础场景是否不依赖网站账号和持续联网？
5. 重依赖是否可以放入按需 Feature Pack？
6. 是否可以作为现有 Recipe/Adapter 能力实现，而不修改所有核心层？

任何一项无法回答，不进入 Core Roadmap。

### 24.4 四条产品红线

- **Library Gate**：只实现服务于审阅、验证和复用的阅读能力；通用知识组织优先交给 Obsidian。
- **Workflow Gate**：用户选择版本化知识方案，不向普通用户暴露任意低代码节点图。
- **Agent Gate**：Agent 只在一个 Recipe、一个明确 Workspace Grant 和预算内运行；不发展通用 Agent IDE。
- **Abstraction Gate**：没有至少两个真实实现需求的抽象不进入 Core；第三方扩展合同必须经过多类内部 Recipe 验证。

### 24.5 北极星指标

主产品指标为：

> 每个活跃用户在固定周期内，经审阅、带可解析来源证据并成功发布的可信知识数量。

不把生成文件数量、LLM 调用量、Desktop 停留时间或导入来源数量作为核心成功指标。

## 25. 从当前代码迁移

### 25.1 不进行一次性重写

现有视频功能继续可用，新架构通过垂直切片逐步接管。迁移期间允许旧 FastAPI 入口调用新的 Application Service，但禁止新 Core 反向调用旧 HTTP 路由。

### 25.2 NoteGenerator 拆分顺序

从现有 `NoteGenerator` 逐步提取：

1. 视频 Source Connector；
2. metadata/subtitle/media Artifact；
3. Transcribe Step；
4. Model Step；
5. Draft assembly；
6. Quality Gate；
7. Source Bundle commit；
8. CLI Recipe 入口。

每一步先有 characterization/contract tests，再切换调用者。不要同时重构所有下载器、Provider 和 UI。

### 25.3 旧任务入口

旧 `/generate_note` 在过渡期可以：

- 构造标准 `video-note` Recipe Request；
- 提交新 Job；
- 把新 Job 事件映射为旧前端状态；
- 不再创建另一套任务执行。

迁移完成后再移除旧状态文件和 BackgroundTasks 路径。

### 25.4 Codex 兼容层

旧 `CodexAppServerGPT` 可以继续为现有文本生成提供兼容，但新增 Agent Recipe 只能使用 AgentExecutor。完成 Video Recipe 迁移后再评估是否删除兼容层，不在本阶段重写所有 GPT Provider。

### 25.5 Desktop 收敛

Phase 2 新 Runtime/CLI/Desktop API 建立后，逐步移除 Tauri 中固定完整 PyInstaller sidecar、任意命令执行和完整环境读取。React 只迁移到明确 typed API，不直接访问文件或 Runtime 进程。

## 26. 分阶段路线与发布 Gate

### Phase 2：最小只读 Workspace

继续执行已确认下位设计，范围限制为：

- Workspace 选择和验证；
- 文件树；
- 安全 Markdown 阅读；
- 搜索和索引状态；
- 外部编辑刷新；
- 打开 Obsidian；
- CLI-only 读取；
- 托管式独立 Runtime。

Gate：通过现有安全、10,000 文档和安装验收后结束，不因缺少 Obsidian 级编辑/图谱而延期。

### Phase 3A：Knowledge Compiler 合同

完成：

- Source/SourceRevision；
- Artifact/EvidenceRef；
- Recipe/Capability；
- Job/Step/Attempt/Event；
- portable run summary；
- foreground CLI；
- QualityReport v1；
- 本机 JobStore 与同卷 staging 边界。

Gate：使用 fake capabilities 证明状态、checkpoint/portable commit、取消、错误和 CLI 协议。

### Phase 3B：Video Note Headless 垂直切片

迁移现有视频能力，产出：

- SourceRevision；
- metadata；
- transcript 和时间戳 Evidence；
- Draft；
- QualityReport；
- portable receipt；
- CLI-only 完整运行。

Gate：没有 Desktop 也能从 URL 产生完整可审阅草稿；不得继续向 `NoteGenerator` 增加新来源分支。

### Phase 3C：Review 与 Publisher MVP

完成：

- Source/Evidence/Draft 对照；
- approve/reject/supersede；
- Draft hash 失效规则；
- PublishPlan；
- personal 默认发布；
- common 强确认；
- conflict 和原子 apply；
- Produce/Review Desktop UI。

Gate：这是第一个完整 Knowledge Compiler 产品闭环。

### Phase 4：按需 Engine 与 Production MCP

在真实后台需求出现后完成：

- `--detach`；
- on-demand Engine；
- scheduler lease；
- durable events；
- cancel/reconcile；
- resource-aware scheduling；
- Production MCP；
- Runtime/Pack 活动版本保护。

Gate：杀死 Desktop、CLI、Worker 和 Engine 后，不产生半提交 Artifact，任务状态确定。

### Phase 5：Article/Wiki Recipe

验证：

- canonical URL；
- SourceRevision/freshness；
- 网页快照；
- 段落 Evidence；
- 登录/付费墙/版权状态；
- Browser Extension 或分享入口的标准 Recipe Request。

### Phase 6：PPT/PDF Recipe

验证：

- page/slide Evidence；
- 图片和表格；
- OCR；
- 多 Artifact；
- 文档大文件流式处理。

完成 Phase 5 和 6 后只评估公共 Recipe/Capability Protocol 的缺口，不正式冻结。

### Phase 7：UE5/Codebase Agent Recipe

完成：

- repo/commit snapshot；
- path/line/symbol Evidence；
- AgentExecutor；
- read-only repository grant；
- 多阶段事实回验；
- 多 Draft；
- 增量分析；
- 专业评测集。

UE5 专业逻辑进入独立 Feature Pack，不进入通用 Core。

只有本阶段至少一个 Agent 型 UE5/Codebase Recipe 原型也通过端到端验证后，才满足第 9.5 节 Gate，并允许冻结第三方公共 Recipe/Capability Protocol。

### Phase 8：Personal Work Digest

处理 Git 活动、日志、会议和周期性整理。此阶段再设计 scheduler、watch trigger、增量 checkpoint 和后台预算策略。

### Phase 9：公共知识与生态

在核心闭环稳定后增加：

- 公共知识包；
- Remote MCP 目录；
- 官方 Recipe/Pack 分发；
- 团队审批策略；
- 第三方扩展协议安全 Gate；
- macOS Tier 2 完整发布。

## 27. 风险与 Trade-off

| 风险 | 设计应对 |
|---|---|
| 过度抽象后迟迟无法交付视频 | 所有抽象必须由 Video 垂直切片验证，首版 Recipe 用代码定义 |
| 每个来源形成独立孤岛 | Connector/Artifact/Recipe/Job/Quality/Publish 统一合同 |
| 变成 Obsidian 克隆 | Library Gate 和 Phase 2 timebox |
| 变成 Dify/n8n 克隆 | 不做通用画布和任意 DSL，Recipe 是产品单位 |
| Runtime 逻辑独立导致安装复杂 | 一个经过验证的发行套件，共同分发但协议独立 |
| 基础 Runtime 持续变重 | Feature Pack、lazy import、Worker 隔离和打包 Gate |
| JobStore 与同步 Workspace 冲突 | 本机 JobStore、portable receipt、同卷 staging 分离 |
| Agent 越权或 Prompt Injection | ExecutionGrant、staging-only、CredentialBroker、独立控制通道 |
| 开放 Markdown 无法全局阻止外部写入 | 明确承诺范围，使用 hash/reconcile/iwiki transaction |
| LLM 输出不可复现 | 保存输入、版本、配置、Evidence 和 receipt，不承诺字节重放 |
| MCP 协议继续演进 | 内部 Job API 独立，MCP Tasks 只做协商映射 |
| Feature Pack 版本漂移 | Job 固定版本、兼容矩阵、last-known-good |
| 大陆网络导致安装失败 | 离线包、镜像、断点续传、签名和 hash 一致 |
| 用户误以为 common 已公开上网 | 明确 common 只是 Workspace 本地分类，远端发布单独建模 |

## 28. 已拒绝方案

1. 把 Core、Python、FFmpeg、Whisper、模型和全部 Pack 打进 Desktop 单体。
2. Desktop 每次操作启动 CLI 并解析 stdout。
3. 把现有 FastAPI Web 后端直接扩展为长期本地任务平台。
4. 从 Phase 2 开始部署永久常驻 daemon。
5. 每种来源复制一个包含全链路逻辑的大 Producer。
6. 使用一个全局状态机覆盖生产、审阅、发布和索引。
7. 把 Codex/Claude Code 当作普通 LLM Provider。
8. 把 Dify、n8n、Temporal、LangGraph 或向量数据库作为产品核心。
9. 在 Video、Article/Wiki、PPT/PDF 和至少一个 Agent 型 UE5/Codebase 原型全部验证前冻结第三方插件 SDK。
10. 让 MCP 机械镜像全部 CLI 命令。
11. 允许 Production MCP 直接发布或写 common。
12. 把 JobStore 作为 Workspace 内可同步文件。
13. 把 Artifact/Source Bundle 做成 AllToNote 私有格式。
14. 用数据库、索引或云服务替代 Markdown 正式知识事实源。
15. 对用户承诺 AllToNote 可以阻止任何具有 OS 权限的外部程序修改 Vault。

## 29. 架构决策记录

1. **采用 Knowledge Compiler 定位。** 生产可信知识，不扩张为通用笔记或工作流平台。
2. **采用 Core-centric、CLI-first。** Core 是业务内核，CLI 是一等公共入口。
3. **采用开放 Workspace。** 长期资产可脱离产品读取。
4. **llm-iwiki 独立。** Workspace schema、索引和正式发布语义不复制。
5. **采用 Typed Artifact。** 不以固定视频文件列表作为通用中间合同。
6. **采用 Source/SourceRevision。** 动态来源不覆盖历史版本。
7. **采用 Recipe。** 来源类型、知识方案和执行器彼此解耦。
8. **采用 Artifact checkpoint。** 不序列化任意 Python 内存作为恢复基础。
9. **拆分四种状态机。** Job、Attempt、Draft Review、PublishTransaction 独立。
10. **JobStore 本机化。** 不随 Workspace 同步；portable receipt 随知识携带。
11. **Model/Agent 分离。** Agent 是受控 Recipe Step，不是控制平面。
12. **采用 Quality Gate。** 确定性检查优先，模型评价辅助，人工审阅最终确认。
13. **采用按需 Engine。** Phase 2 无 daemon；后台生产出现时按需启动并空闲退出。
14. **MCP 分 Profile。** Knowledge Read 和 Production 权限分离。
15. **共同分发但逻辑独立。** 普通用户得到兼容套件，组件仍可单独使用。
16. **Windows Tier 1。** macOS 在共享接口稳定后通过独立 Gate。
17. **不承诺 bit-for-bit AI 重放。** 承诺可审计、可解释和可重新执行。
18. **不承诺全局文件写入控制。** 承诺经 AllToNote 接口的 Publisher 纪律。

## 30. 本设计的完成定义

本文只有在以下条件同时满足时可标记为“已确认”：

1. 产品使命、目标用户和非目标无相互冲突；
2. Workspace、Published Vault、Source Store 和 Machine State 术语唯一；
3. AllToNote、llm-iwiki、Runtime、Engine、CLI、MCP 和 Desktop 责任明确；
4. Source、Revision、Artifact、Evidence、Recipe、Job、Draft、Quality 和 Publish 逻辑完整；
5. 生产、审阅、发布和索引生命周期分离；
6. 前台 CLI、后台 Engine 和 MCP 长任务关系明确；
7. ModelExecutor 与 AgentExecutor 分离；
8. Agent 权限和开放磁盘限制被准确表述；
9. JobStore、portable asset 和 cache 的数据所有权明确；
10. 性能、分发、安全、故障恢复和测试具有可验证 Gate；
11. Phase 2 范围未被扩大；
12. 后续实施被拆分成独立下位设计，不形成一次性大重写。

## 31. 后续下位设计入口

本文确认后，按以下顺序继续：

1. `AllToNote Portable Artifact 与 Source Bundle 设计`；
2. `AllToNote Job Engine 与 Automation Protocol 设计`；
3. `AllToNote Video Note Recipe 设计`；
4. `AllToNote Review 与 Publisher 设计`；
5. `AllToNote Production MCP 设计`；
6. `AllToNote Runtime 与 Feature Pack 分发设计`。

每份下位设计分别进入实施计划，不创建覆盖所有阶段的单一超大计划。Phase 2 仍按照现有只读工作区下位设计独立推进。
