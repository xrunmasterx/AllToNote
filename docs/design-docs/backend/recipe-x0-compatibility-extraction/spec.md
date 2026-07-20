# Feature: Recipe X0-A 最小兼容接缝

- 日期：2026-07-19
- 状态：修订后待逐任务实施
- 决策：替代原 13 项“大一统兼容抽取”方案；当前可执行范围以本目录 tasks.md 为准
- 上位约束：AllToNote 总体架构、Recipe 最小扩展合同、Runtime/CLI 设计

## 0. 执行摘要

本修订不接受“文档里已经写成平台，所以当前实现已经是平台”的推论。

目标架构的方向正确：

    AllToNote
      = 上层知识生产、积累、审阅、发布与复用工具

    Production
      = 用户把来源转成可积累知识的用例

    Recipe
      = AllToNote 内部版本化的生产扩展单位

    Video / Document / PPT / Article / Codebase / UE5 / Personal
      = 并列官方 Recipe

    CLI / Desktop / MCP
      = 同一 ProduceService 的不同入口

但当前代码尚未达到这个状态。SDK、Runtime、CLI、Job 查询和 SQLite 结果提交仍被 Video 类型塑形；同一 Workspace 中的 Job 执行也基本串行。

原 X0 方案同时要求：

1. 入口解耦；
2. 八类通用 Recipe DTO；
3. preflight、plan、output、result 的统一生命周期；
4. 三套 CLI 动词；
5. Job 查询、结果 codec 与原子提交去 Video 化；
6. 全量兼容回归。

这是一项高复杂度、中大型重构，却仍不交付高并发。抽象投入与产品收益不匹配。

本修订将工作拆成三个独立问题：

| 阶段 | 解决的问题 | 本文是否实施 |
|---|---|---|
| X0-A | CLI/SDK/Runtime 不再把 VideoService 当公共入口；建立最小 Recipe 调用接缝 | 是 |
| X0-B | Job result、atomic commit、Artifact/Bundle 由真实非 Video 消费者驱动去 Video 化 | 否，作为第一个 Document/PPT 纵切的合入 Gate |
| Parallel Production | 同一 Workspace 多 Job、多 Agent、detach、资源调度与进程监督 | 否，见独立并发规格 |

因此，X0-A 的准确承诺是：

> 建立可演进的多 Recipe 控制面接缝，并保持 Video 零语义变化；它不是完整的多 Recipe 数据面，也不是高并发执行器。

### 0.1 上位文档收敛说明

当前上位总体架构仍把 add、produce、run 三套入口以及完整 RecipePlan/ProduceResult、Job/Repository 去 Video 化共同列入 Multi-Recipe X0。本文基于真实代码和三个未来消费者的反证，主动收缩了实施边界。

这不是推翻总体架构，而是对实施顺序的轻量校准：

- 保留 AllToNote > Production > Recipe、统一 ProduceService、CLI-first、薄 Desktop 和按需 Engine；
- 将 add、独立 run、完整 plan/result 和 Repository 泛化从 X0-A 后移；
- 将数据面解耦改为真实 Document/PPT 消费者驱动的 X0-B；
- 将高并发改为独立 Parallel Production。

在 X0-A 生产代码开始前，应对上位文档的 Phase 3B.5、CLI 命令树和退出 Gate 做同义的小幅修订，避免两份有效文档给出不同执行范围；不需要重写总体架构主体。

## 1. 第一性原理与产品不变量

### 1.1 用户真正购买的结果

用户需要的不是一个通用工作流平台，而是尽快得到可被人和 Agent 直接使用的开放 Markdown 知识。

核心价值按优先级排序：

1. 从来源到首份可用 Markdown 的时间短；
2. 失败可解释、可恢复，不重复昂贵外部操作；
3. 新增一种 Production 不复制 CLI、Job、恢复和发布基础设施；
4. 可以批量提交和并行执行；
5. 结果有来源、版本和 Artifact 身份，可积累、可复用；
6. 治理能力存在，但不阻塞个人草稿的快速使用。

因此产品流程应是：

    Produce
      -> 立即得到可用 Markdown Draft

    Curate（按需）
      -> Quality / Review / Publish
      -> 提升为正式或共享知识

个人知识生产不应被强制经过完整 Review/Publisher 流程；发布到 common 或其他共享目标时才执行更严格的治理 Gate。

### 1.2 CLI-first 不等于 CLI-as-core

“底层本质还是 CLI”应解释为：

- CLI 是一等、完整、稳定的公共入口；
- Desktop、CLI、MCP 归一化成同一应用请求；
- 业务真相位于 Core/Application Service；
- 薄 Desktop 通过 typed bridge、SDK 或按需 Engine 调用同一内核。

不应解释为 Desktop 每次点击都启动 CLI 并解析人类 stdout。那会把进程启动、取消、事件流和兼容逻辑推回 UI，形成第二套脆弱协议。

### 1.3 高并发不等于无界并发

“不限制本地 Agent”应解释为不阉割其推理、工具和工作区能力；不能解释为无界启动进程或允许多个 Agent 无协调地写同一目标。

真正高吞吐需要：

- 高队列深度；
- 用户可配置、资源感知的有界运行并发；
- 每个 Agent 独立进程和 staging/worktree；
- CPU、内存、GPU、Provider、Agent slot 和 workspace commit 的准入；
- 计算阶段并行，最终发布/提交短暂串行；
- 不修改 Agent 内部语义，只协调共享资源和生命周期。

资源准入是吞吐和健壮性机制，不是对 Agent 能力的任意限制。

### 1.4 抽象 Gate

只有一个真实 Recipe 时，只抽取调用它所必需的最小接口。

- Fake 只证明接口能调用，不能证明字段真正通用；
- Preflight、Plan、Output、Result、Artifact role 和 Bundle manifest 必须由至少两个真实消费者验证；
- 第三方插件 SDK 必须在多类官方 Recipe 稳定后再评估；
- 不为未来猜测动态插件、通用 DAG、YAML DSL 或微服务拓扑。

## 2. 当前实现事实

### 2.1 公共入口仍由 Video 主导

当前主路径为：

    CLI
      -> AllToNoteRuntime.submit_video
        -> AllToNoteSDK.submit_video
          -> VideoService.submit_video / wait_job

已确认：

- backend/app/core/sdk.py 直接导入并持有 VideoService；
- backend/app/runtime.py 的公共生产入口和 factory 直接组装 VideoService；
- backend/app/cli/main.py 只有 produce video，Runtime Protocol 和结果投影均为 Video-specific；
- core/jobs 与 Job application service 从 domain.video 获取通用 Job 类型；
- Job query port 固定返回 VideoProduceResult；
- core/ports/jobs.py 暴露 VideoResultPlan 和 commit_video_result_atomic；
- SQLite Repository 直接校验、构造、编码和解码 Video result。

结论：

> 当前目标文档是多 Recipe 平台设计，当前实现仍是 Video-first 垂直切片。二者不能混为一谈。

### 2.2 同一 Workspace 当前不是多 Job 并发

- VideoService 在实例级持有一把 execution lock，wait_job 在完整执行期间持锁；
- SQLite leases schema 只允许 lease_name 为 scheduler 的单一记录；
- 第二个同 Workspace CLI owner 会得到 scheduler_busy；
- 不带 wait 的 submit 只留下 queued Job，当前没有后台 worker 自动领取；
- 单个 Video 内部存在有限 chunk 并行，但这不是多个 Production Job 并行。

准确能力矩阵：

| 能力 | 当前 | X0-A 后 |
|---|---:|---:|
| 多个 Job 可持久排队 | 是 | 是 |
| 同一 Workspace 多 Job 同时执行 | 否 | 否 |
| 不同 Workspace 多 CLI 并行 | 可行但资源无统一协调 | 不变 |
| detach 后后台继续 | 否 | 否 |
| 多 Recipe 统一提交入口 | 否 | 是 |
| 完整本地 AgentExecutor | 否 | 否 |
| 资源感知调度 | 否 | 否 |

X0-A 必须冻结并诚实记录当前串行语义，不得把它包装为“已支持高并行”。

### 2.3 SQLite 的边界

当前 Repository 使用 WAL、busy timeout 和短连接。SQLite 适合本地工具的 metadata 与短事务，不需要因为未来并发立即换成服务端数据库。

但 SQLite 同时只允许一个 writer。并发阶段必须：

- 缩短 writer transaction；
- 避免在事务内执行长时间模型、OCR、Agent 或文件解析；
- 对 portable/workspace commit 做明确串行资源控制；
- 压测 4、8、16 worker 下的 busy、checkpoint 与 commit 延迟；
- 只在实测成为瓶颈后再考虑更重存储。

当前项目虚拟环境报告 SQLite 3.50.4。SQLite 官方在 2026 年公布了影响多连接 WAL 并发写/检查点的 WAL-reset bug；进入多 writer 压测前，打包环境必须升级到修复版本 3.50.7、3.51.3 或更高兼容版本。

### 2.4 当前并非完整 Agent 调度

现有 Codex bridge 是受限、一次性的 ModelExecutor 适配器，不等于 UE5/Codebase 所需的 AgentExecutor。

Codebase/UE5 Recipe 需要：

- 多轮工具调用；
- repo revision 和 dirty snapshot 固定；
- 授权范围内的读、构建、检索和 staging 写入；
- 独立 Agent subprocess；
- 进度、取消、超时和 process tree 回收；
- 多 Agent 并行调研与独立验证。

这些属于 Parallel Production / AgentExecutor 阶段，不应伪装成 Recipe Registry 功能。

## 3. 未来消费者反证

### 3.1 Document/PPT

Document/PPT 会需要页或 slide inventory、原生结构提取、选择性 OCR/vision、页码或 bbox Evidence、多 Artifact 和多份 Markdown。

它不会自然复用 Transcript、transcriber identity、timeline、Video document kind 或 Video result JSON。

而且打开文件、确认页数和扫描比例可能本身有成本，不能强制放在 durable Job 创建之前。因此 X0-A 不冻结通用 preflight -> deterministic plan -> submit 生命周期。

### 3.2 UE5/Codebase

Agent 调研是自适应过程：发现新入口后可能继续检索、构建、验证和修正路径。

真正需要固定的是：

- SourceRevision；
- Recipe/version；
- 用户参数；
- Agent/Model identity；
- grant、预算和影响结果的配置。

不需要提前固定完整工具调用序列或通用 stage DAG。

### 3.3 Personal / 多 Agent 汇总

这类 Recipe 的输入可能是时间窗口或一组 Job/Artifact refs；可能有多个 SourceRevision、部分失败、no-op 成功和 cursor/watermark。

因此通用结果不能假设：

- 单一 Source；
- 单一 primary Draft；
- 必填 Transcript；
- 必填 Quality；
- 固定 display asset；
- 固定 Video-shaped result。

这些反证支持只冻结最小 submission envelope。

## 4. X0-A 范围

### 4.1 目标

X0-A 建立：

    CLI / SDK / Runtime
      -> ProduceService
        -> immutable RecipeRegistry
          -> VideoRecipeAdapter
            -> existing VideoService

完成后：

1. 新入口可以显式选择版本化 Recipe；
2. Video 只是 Registry 中的官方实现之一；
3. SDK/Runtime 的通用生产入口不直接依赖 VideoService；
4. legacy submit_video 和 produce video 继续可用；
5. Video request JSON、两套 hash、Checkpoint、Portable/iwiki、结果 wire 和恢复语义不变；
6. 通用控制面不新增线程、后台服务、全局可变状态或重型依赖。

### 4.2 最小合同

X0-A 只实现：

- RecipeKey：recipe_id 与 recipe_version；
- RecipeDescriptor：key、display name、支持的 input/output kind；
- InputDescriptor：kind、value/ref、轻量 attributes；
- ProduceRequest：contract version、RecipeKey、input、workspace ref、requested outputs、Recipe-owned parameters、principal、client request identity；
- ProduceSubmission：job_id、RecipeKey、JobState；
- RecipeEndpoint.submit(request)；
- RecipeRegistry.list、describe、resolve；
- ProduceService.submit。

约束：

- DTO 为 internal、可演进合同，不是第三方公共 ABI；
- Recipe-owned parameters 是 JSON-safe、不可变、由已固定 Recipe 校验；
- durable request 只持久化 secret reference，不持久化明文 Secret；
- 不使用模糊的“字符串像 Secret”启发式拒绝合法 URL、路径或 ID；
- Registry 由组合根显式注册，构建后只读；
- 不做目录扫描、Python entry point、远端下载或动态代码执行。

### 4.3 ProduceService 职责

ProduceService 只负责：

1. 校验最小通用 envelope；
2. 固定 Recipe ID 与版本；
3. 通过 Registry resolve；
4. 委托 RecipeEndpoint.submit；
5. 返回 ProduceSubmission。

ProduceService 不负责：

- CLI parser、Human/JSON 渲染或 wait；
- Video acquisition、转写、编译和质量；
- 通用 preflight/plan/output/result 生命周期；
- Job 线程、Engine、资源调度；
- Recipe-specific result codec；
- Review 或 Publisher。

### 4.4 Video 兼容 Adapter

VideoRecipeAdapter 必须把 ProduceRequest 确定性翻译为现有 VideoProduceRequest，并继续调用现有 VideoService.submit_video。

不得绕过现有 VideoService 直接创建 Job，因为现有路径同时负责：

- canonical request JSON；
- Job idempotency hash；
- Video/checkpoint input hash；
- config snapshot；
- v1/v2 requested outputs 和 bindings；
- crash/reopen 和 zero-replay；
- Portable/iwiki commit。

不得修改：

- SQLite schema v1；
- legacy request/result JSON；
- checkpoint step/schema ID；
- candidate compatibility hash；
- source identity conflict；
- atomic commit 范围；
- CLI JSON/Human golden；
- 无版本 produce video 的 v1 默认。

显式 v2 入口必须与 generic ProduceRequest 等价；默认版本从 v1 迁移到 v2 是单独产品兼容决定，不夹带在 X0-A。

### 4.5 SDK 与 Runtime

目标组装：

    VideoService
      -> VideoRecipeAdapter
      -> RecipeRegistry
      -> ProduceService
      -> AllToNoteSDK
      -> AllToNoteRuntime

SDK/Runtime 提供通用 submit，并保留 submit_video 兼容 facade。兼容 facade 内部走同一个 ProduceService，不保留第二条业务路径。

所有现有 Runtime factory 名称、参数、workspace/machine root 解析和 reopen 行为不变。

### 4.6 CLI

普通用户只有一个主动作：produce。

X0-A 支持：

    alltonote produce video <input> ...
    alltonote produce <input> --recipe <id>@<version> ...
    alltonote produce --request <request.json>
    alltonote recipe list
    alltonote recipe describe <id>@<version>

约束：

- produce video 是兼容且用户友好的别名；
- generic produce 是 Agent/自动化入口；
- recipe list/describe 是高级发现入口，不进入普通生产心流；
- 不新增独立 run 动词；
- 不新增 add 自动路由；
- Human、JSON envelope、warning、exit code、wait 与 Ctrl+C/cancel 保持兼容；
- recipe list/describe 不加载 Runtime、Downloader、Whisper、Torch、FFmpeg、Model client 或 FastAPI。

自动 add 只有在至少两个真实 Recipe 存在、MIME/magic/URL 冲突和昂贵探测语义被验证后才评估。

### 4.7 Job 类型

X0-A 只迁移无争议的 JobState 所有权：

- 将唯一 JobState 定义移动到通用 Job 模块；
- domain.video 重新导出同一个类型对象；
- 禁止复制第二份等值 Enum；
- 不在本阶段迁移仍携带 VideoProduceResult 的 JobSnapshot；
- Retry、query result 和 completion 类型随 X0-B 的真实结果边界处理。

## 5. 明确非目标

X0-A 不实现：

- Document、PPT、Article、UE5、Codebase 或 Personal Recipe；
- 通用 PreflightReport、RecipePlan、RecipeOutput、ProduceResult；
- 自动 add 或模糊路由；
- 通用 Artifact/Bundle assembler；
- Job result codec registry；
- Repository atomic commit 去 Video 化；
- SQLite schema migration；
- 动态插件、公共 SDK、通用 DAG 或 Workflow Engine；
- detach、daemon、worker pool、资源调度；
- 完整 AgentExecutor；
- Desktop UI；
- Review/Publisher 新流程。

## 6. X0-B：第二消费者驱动的必要后续

X0-A 不能被宣称为完整数据面解耦。Job query、result JSON、atomic commit 和部分 Bundle assembler 仍为 Video-specific，这是有意保留且必须登记的迁移债务。

X0-B 由第一个真实非 Video 纵切驱动，首选最小 Document/PPT Recipe。只有此时才回答：

- result 是 opaque record、Artifact manifest 还是其他结构；
- SourceRevision 和 primary Artifact 是否为复数；
- no-op 与 partial success 如何表达；
- Quality 是否只是 Artifact role；
- 如何为 legacy Video result dual-read；
- atomic commit 的通用 callback/factory 边界；
- 哪些 Bundle primitive 真正由 Video 与 Document 共享。

X0-B 完成 Gate：

1. Video 与一个真实非 Video Recipe 共享同一 JobStore、Checkpoint 和 portable commit；
2. 通用 Job/Repository 不导入任一 Recipe-specific domain；
3. legacy Video request/result 可读、可恢复、可回滚；
4. atomic source identity、portable commit、result 和 Job terminal transition 仍在一个可证明的提交协议中；
5. 公共字段有两个真实消费者，单消费者字段保留在 namespaced extension 或 Recipe Artifact；
6. 不把 Video Bundle assembler 假装成通用 Bundle assembler。

在 X0-B 前，不允许以 Fake Recipe 通过为由冻结公共插件合同。

## 7. 并发边界

X0-A 的 concurrency-ready 只表示：

- Registry 构建后只读；
- ProduceService 不保存当前活动 Job、Workspace 或 Recipe 的可变全局状态；
- 每次 submit 使用独立请求；
- submit 不执行下载、模型、转写或 Agent；
- submit 不创建后台线程；
- X0-A 不增加新的全局锁或串行点。

它不表示：

- 多 Job 已并发执行；
- scheduler lease 已变成 per-job claim；
- Codex client 已证明线程安全；
- Engine 或 worker pool 已存在；
- Desktop 退出后任务继续。

独立 Parallel Production 规格负责这些能力。

## 8. 健壮性与兼容 Gate

### 8.1 必须保持

- legacy SDK submit；
- Video v1/v2 canonical request；
- Job request hash；
- Video/checkpoint hash；
- config snapshot；
- legacy raw result round-trip；
- historical candidate；
- crash/reopen/zero replay；
- Portable manifest 和 bundle identity；
- source identity conflict rollback；
- cancel、retry、outcome unknown 和 fencing；
- CLI JSON/Human golden。

### 8.2 现状 characterization

测试必须明确记录但不美化：

- 同一 Runtime 当前完整执行串行；
- 同一 Workspace 第二 scheduler owner 返回 scheduler_busy；
- submit 不 wait 时只创建 queued Job；
- 后续 job wait 可以成为执行 owner；
- 不把旧 task_serial_executor 接入新 Recipe/ProduceService 路径。

### 8.3 架构 Gate

以下模块不得导入 Video 实现：

- core/recipes/contracts.py；
- core/recipes/registry.py；
- core/application/produce_service.py。

完成 X0-B 后，Gate 才扩展到：

- core/jobs 的 result/query 部分；
- core/ports/jobs.py；
- core/ports/job_queries.py；
- adapters/jobs/sqlite_repository.py。

### 8.4 UX Gate

- 普通 Video 生产仍只需一个命令；
- 不要求普通用户编写 request JSON 或理解 Recipe ID；
- 完成 Produce 后直接给出 Markdown/Bundle 路径；
- Review/Publish 不阻塞 personal Draft 的可用性；
- 内部 Attempt、fencing、codec、lease 等术语不进入普通 UI。

## 9. 工作量与风险结论

原方案：

- 13 个任务；
- 约 18 至 28 个生产/测试文件；
- 估计 1,900 至 3,300 行总变更；
- 高持久化兼容风险；
- 完成后仍为单 Workspace 单执行者。

修订后的 X0-A：

- 8 个任务；
- 估计 10 至 18 个生产/测试文件；
- 估计 900 至 1,600 行总变更；
- 风险集中在 Video 请求等价、SDK/Runtime 组合根和 CLI golden；
- 不触碰最危险的 result_json 与 atomic commit 事务。

X0-B 没有被删除，而是移动到第一个真实非 Video 消费者的纵切中。预计仍是高风险、约 600 至 1,200 行兼容重构，实际范围由 Document/PPT fixture 决定。

Parallel Production 是独立的运行时重构，不能用 Recipe X0 的代码量估算。其复杂度显著高于 X0-A。

## 10. 完成定义

X0-A 只有同时满足以下条件才完成：

1. 通用 ProduceService、静态 RecipeRegistry 和最小 RecipeEndpoint 存在；
2. Video 通过 Adapter 注册，Video 内核未重写；
3. SDK/Runtime 的通用入口不直接依赖 VideoService；
4. submit_video 和 produce video 仍兼容并委托同一路由；
5. generic produce 可以显式调用 Video Recipe；
6. 无版本 produce video 仍保持 v1；
7. legacy request/result、两套 hash、Checkpoint、恢复和 Portable/iwiki 行为不变；
8. cold path 不加载重型 Recipe 实现；
9. X0-A 没有新增 Engine、线程池、动态插件、DAG 或 DB schema；
10. 文档明确记录：Job/Repository/Bundle 的剩余 Video 耦合属于 X0-B，当前高并发属于 Parallel Production；
11. 定向和全量回归通过，或只剩实施前已登记且可复现的环境基线失败；
12. git diff 仅包含 X0-A 直接需要的手术式改动。

## 11. 参考

- G:/AllToNote/docs/superpowers/specs/2026-07-13-alltonote-knowledge-compiler-architecture-design.md
- G:/AllToNote/docs/superpowers/specs/2026-07-18-alltonote-recipe-extension-contract-design.md
- G:/AllToNote/docs/superpowers/specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
- G:/AllToNote/docs/superpowers/specs/2026-07-18-alltonote-engine-production-mcp-design.md
- backend/app/core/application/video_service.py
- backend/app/adapters/jobs/sqlite_repository.py
- backend/app/core/sdk.py
- backend/app/runtime.py
- backend/app/cli/main.py
- backend/tests/core/test_video_request_persistence.py
- backend/tests/integration/test_fake_video_producer.py
- backend/tests/contracts/test_cli_envelope_golden.py

## 12. Changelog

| 日期 | 变更 |
|---|---|
| 2026-07-19 | 原始 X0 方案：入口、完整通用 DTO、CLI 扩面、Job/Repository 泛化合并为 13 个任务 |
| 2026-07-19 | 第一性原理复审：拆为 X0-A、真实第二消费者驱动的 X0-B、独立 Parallel Production；收缩为最小 submission 合同 |
