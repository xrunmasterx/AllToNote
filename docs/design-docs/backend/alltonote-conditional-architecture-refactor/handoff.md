# AllToNote 条件式最优架构重构交接与验收手册

> 状态：READY FOR IMPLEMENTATION HANDOFF
> 日期：2026-07-19
> 实现工作树：G:/AllToNote-video-producer
> 当前分支：codex/alltonote-video-producer
> 文档定位：执行索引、问题台账、阶段依赖、禁止事项与验收总表
> 非目标：本文件不是与总体架构竞争的第二份总体设计，也不授权覆盖工作树、重写 Video 内核、建设动态插件市场或通用工作流平台

---

## 0. 一页交接结论

后续 Agent 应当实施下面这套架构：

~~~text
Core-centric
CLI-first
模块化单体
静态官方 Recipe Registry
一个薄 ProduceService
一个 durable JobStore
前台任务直接运行
后台/批量任务使用按需 Engine
多进程 Worker
资源感知但不阉割 Agent
开放 Markdown/Artifact
个人 Draft 立即可用
Review/Publish 按目标治理
~~~

这不是轻量微调，也不应成为一次大爆炸重构。客观工作量如下：

| 工作包 | 性质 | 粗略规模 | 当前必要性 |
|---|---|---:|---|
| Recipe X0-A 最小入口接缝 | 中等兼容重构 | 约 900–1,600 行生产与测试变更 | 必要 |
| X0-B 结果与提交边界抽取 | 中高风险兼容重构 | 约 600–1,200 行，最终由真实第二消费者决定 | 条件满足后必要 |
| Local Parallel Production | 高复杂度运行时重构 | 约 2,500–5,000 行，单工程师约 3–6 周 | 用户明确要求已触发 |
| 开放 Artifact 与 Review/Publisher | 产品数据闭环 | 必须先复用已有 Portable/iwiki 能力再估算 | 必要 |
| 薄 Desktop / EXE | 产品集成，不是 Core 重写 | 盘点现有 typed bridge 后再估算 | Core/Engine 稳定后必要 |

整体属于“大型、分阶段重构”，但每个合入单元必须小、可回归、可停止、可逆。禁止把所有阶段合并成一个无法定位回归来源的大改动。

本轮重构的目的不是让所有代码看起来通用，而是同时解决以下已经确认的问题：

1. AllToNote 的公共生产入口仍被 VideoService 塑形；
2. Job 查询、结果和原子提交仍存在 Video 反向依赖；
3. 同一 Runtime 和同一 Workspace 当前基本只能完整执行一个 Job；
4. 没有按需后台领取者，CLI 退出后 queued Job 不会自行推进；
5. Codex client 的并发重入基线已于 2026-07-31 通过 2/4/8 路受控测试与双路真实 app-server 探针，但这不授权开启多 Job worker；
6. 当前 SQLite 3.50.4 不应作为多连接 WAL 的生产并发基线；
7. 当前 bridge 是结构化 ModelExecutor，不是完整 AgentExecutor；
8. 开放 Draft/Artifact 的独立可读、可迁移和删除 JobStore 验证尚未形成独立 Gate；
9. Review/Publisher 仍主要是原则，未形成完整产品闭环 Gate；
10. 薄 Desktop/EXE 缺少独立的 typed bridge、打包、冷启动和业务语义唯一性验收。

“条件式最优”表示：在单用户、本地优先、Windows Tier 1、官方 Recipe、CLI-first、开放 Markdown、单机多进程这些当前约束下，这是成本和能力的最佳平衡。未来若出现第三方不可信插件、多机调度或远程多租户，再基于新证据重评；现在不为它们预付复杂度。

---

## 1. 产品使命与北极星

AllToNote 的核心结果不是 Workflow、Job、Engine 或 UI，而是：

> 尽快把输入转化为人和不同 Agent 都能直接消费的开放 Markdown 知识，并在失败后安全恢复，不重复昂贵或付费副作用。

产品优先级：

1. 从输入到首份可用 Markdown Draft 的时间；
2. 失败可恢复，且不重复模型、Agent、下载、转写或发布副作用；
3. CLI 可以独立完成整个生产流程；
4. 新增 Production 不复制 CLI、JobStore、恢复、Desktop 或 Publisher；
5. 同一机器可以排队大量 Job，并按实际资源有效并行；
6. Draft 立即可用，治理流程按发布目标启用；
7. 删除 JobStore、索引或 AllToNote 本身后，已提交知识仍可读取。

以下不是北极星：

- 进程数；
- 同时启动的 Agent 数；
- 工作流节点数量；
- 抽象接口数量；
- 生成文件数量；
- Desktop 停留时间。

最终产品心智模型必须保持为：

~~~text
AllToNote
  = 上层知识生产、积累、审阅、发布与复用工具

Production
  = 用户发起“把来源变成知识”的用例

Recipe
  = AllToNote 内部版本化的知识生产实现单元

Video / Document / PPT / Article / UE5 / Codebase / Personal
  = 并列的官方 Recipe

CLI / Desktop / MCP
  = 同一应用服务的不同入口

Job / Checkpoint / Artifact / Evidence / Quality / Engine / Publisher
  = 只实现一次的共享平台能力
~~~

任何改动如果让普通用户必须理解 Attempt、Fencing、Codec、Lease、Pack 或内部 Recipe DTO，均视为产品层回归。

---

## 2. 权威性、阅读顺序与冲突处理

### 2.1 开始前必须阅读

1. G:/AllToNote/AGENTS.md；
2. C:/Users/dezhengu/.codex/attachments/53b00b0a-e2ef-4386-8e74-e6e93c056865/pasted-text-1.txt；
3. G:/AllToNote/docs/README.md；
4. G:/AllToNote/docs/tasks/alltonote-master-tasks.md；
5. G:/AllToNote/docs/tasks/alltonote-design-coverage-matrix.md；
6. ../recipe-x0-compatibility-extraction/spec.md；
7. ../recipe-x0-compatibility-extraction/tasks.md；
8. ../local-parallel-production/spec.md；
9. ../local-parallel-production/tasks.md；
10. 本文件；
11. 当前工作树中的代码和测试，而不是只阅读 Git HEAD。

### 2.2 权威关系

1. 总体 ARCH、REC-CONTRACT、RUNTIME、ENGINE 文档负责产品使命、领域不变量和最终组件边界；
2. Recipe X0-A spec/tasks 负责控制面兼容接缝；
3. Local Parallel Production spec/tasks 负责本地并发执行；
4. 本文件负责解释为何重构、实施顺序、阶段出口、禁止事项和最终完成定义；
5. 代码和测试是“当前真正实现了什么”的事实来源；
6. 用户本轮明确提出的轻量、可扩展、高性能、高并行和完整本地 Agent 能力，是 Engine 优先级已经触发的新产品证据。

### 2.3 当前必须在 Wave 0 解决的文档冲突

旧 Recipe Extension 实施计划仍包含：

- 8 个通用 DTO；
- add / produce / run 三个主入口；
- X0 内完成 Repository 去 Video 化；
- 在没有真实第二消费者时冻结完整 Result/Commit。

这些旧描述与修订后的 X0-A/X0-B 边界冲突。旧计划中的 RX-01、RX-03、RX-05、RX-06，以及旧 Document plan 中把完整 X0 作为前置的描述，在权威文档完成修订前不得作为实施依据。

修订后的唯一实施口径是：

~~~text
X0-A
  = 最小通用提交接缝、静态 Registry、薄 ProduceService、Video Adapter、SDK/Runtime/CLI 兼容路由

X0-B
  = 由第一个真实 Document/PPT 消费者驱动的 Result、Artifact、Repository 和 atomic commit 抽取

Parallel Production
  = 独立的按需 Engine、per-job claim、多进程 Worker、资源准入和 AgentExecutor
~~~

当前 master tasks 仍把 Engine 标成延后条件项。用户已经明确要求同 Workspace 高并行、批量后台执行和完整本地 Agent 调度，因此产品触发条件已经满足；但 Engine 仍不得混入 X0-A。

Local Parallel Production 现有 spec 第 6 节和 Task 4 还把最小 execution claim 表述为包含单一 attempt_id。现有 Video Job 会在同一个 Job execution authority 下顺序创建多个 step Attempt，因此该表述必须在 Wave 0 同步为第 9.3 节的 Job-scoped generation 模型；不能让实现 Agent在两种 claim 语义中自行选择。

### 2.4 冲突处理规则

如果文档间发生冲突：

1. 不静默选择；
2. 记录冲突原文、代码事实和用户目标；
3. 优先保护已发布兼容行为和数据；
4. 先修订权威文档或取得用户决策；
5. 再修改生产代码。

本文件不能用来绕过兼容要求，也不能用来推翻已经验收的 Wave 1A。

---

## 3. 当前已验证的基线与问题台账

### 3.1 已完成工作

Wave 1A 的 RCP-00 至 RCP-07 已完成并有既有验收记录。后续 Agent 不得重新实现：

- Runtime / CLI 基础；
- Video v1/v2 入口；
- durable Job、Attempt、Checkpoint 与恢复；
- Portable Artifact / Bundle；
- ModelExecutor；
- 配置快照；
- 取消、重试、恢复和 zero-replay 语义；
- 既有 CLI JSON/Human envelope。

这些是本次抽取要保护和复用的资产，不是需要推倒的障碍。

### 3.2 问题台账

| ID | 事实级别 | 已确认问题与证据 | 解决阶段 |
|---|---|---|---|
| P-01 | 已验证 | backend/app/core/sdk.py 直接持有 VideoService，只有 submit_video | X0-A |
| P-02 | 已验证 | backend/app/runtime.py 的 Runtime 只有 Video-specific submit | X0-A |
| P-03 | 已验证 | backend/app/cli/main.py 的 protocol、请求和结果导入 Video 类型 | X0-A |
| P-04 | 已验证 | job_queries.py、jobs.py、sqlite_repository.py 的 result/query/commit 仍由 Video 塑形 | 真实非 Video + X0-B |
| P-05 | 已验证 | video_service.py 的 _execution_lock 覆盖完整执行 | Parallel Production |
| P-06 | 已验证 | SQLite 只有一个 scheduler lease，leadership 被等同于单执行者 | Engine / per-job claim |
| P-07 | 已验证 | submit without wait 只创建 queued Job，没有后台领取者 | 按需 Engine |
| P-08 | 已验证并修复 | app-server 每 turn 的 request ID、response、timeout、subprocess 与 stderr 已隔离，cancel/timeout 只清理目标 turn | 保留 2/4/8 路与真实双路探针回归 |
| P-09 | 已验证 | 当前 Python sqlite3.sqlite_version 为 3.50.4 | 并发 C0 |
| P-10 | 已验证 | 现有 bridge 是一次结构化 completion，不是完整 Agent CLI runtime | AgentExecutor |
| P-11 | 已验证 | 开放 Draft/Artifact 删除 JobStore 后独立可读未形成任务 | Artifact 产品闭环 |
| P-12 | 已验证 | Review/Publisher 只有原则，缺少状态、CLI 和事务 Gate | Review/Publisher |
| P-13 | 已验证 | Desktop 缺独立 typed bridge、EXE、冷启动和语义唯一性验收 | Thin Desktop |
| P-14 | 已验证 | 上位旧 X0 计划与修订后的 X0-A/X0-B 冲突 | Wave 0 |
| P-15 | 已验证 | 实现和权威文档分处两个大量 dirty 且未集成的工作树 | Wave 0 |
| P-16 | 已验证 | backend/app/job_runtime.py 的 reconnect job wait 硬编码 create_codex_app_server_runtime_for_workspace | X0-B，且为 Engine 前置 |
| P-17 | 已验证 | 3 个集成测试当前精确扫描有 18 处直接访问 runtime._sdk._video_service 私有字段（10/6/2） | X0-A 测试 fixture 重接线 |

### 3.3 已复现测试事实

当前锁定虚拟环境位于 G:/AllToNote-video-producer/.venv。

审查时该环境报告 Python 3.11.15、SQLite 3.50.4；这些只是当前快照，开发、CI 和最终 EXE 都必须在各自 Gate 中重新报告实际版本。

只读审查已复现：

- 一组核心兼容快速测试：68 passed；
- 一组关键 crash/reopen/zero-replay 样本：6 passed；
- 另一组更广的 58 个定向测试：51 passed、7 failed；其中 3 个失败与本地缺 tomli_w 有关，4 个与本机 iwiki 版本和 runtime-lock 漂移有关；
- 历史 acceptance 记录声称 Wave 1A 约 1820 tests passed，但实现、文档和验收仍散落于两个未提交工作树，不能直接当作新重构基线。

这些结果不矛盾：它们覆盖的集合不同。它们也不代表全量基线已经干净。Wave 0 必须使用项目锁定环境重新运行完整 suite，并逐项登记全部失败。

### 3.4 当前不能宣称

当前不能被描述为：

- 已完成多 Recipe 平台；
- 同一 Workspace 高并发；
- 已有完整 AgentExecutor；
- 已完成薄 Desktop 与新 Core 的统一接入；
- 已证明通用 Result/Commit 合同；
- 已具备第三方插件 SDK。

准确表述是：

~~~text
目标架构已经明确；
Wave 1A 已完成；
Video 纵向能力较完整；
Recipe 公共入口、真实第二消费者、开放产品闭环与高并发 Engine 尚未完成。
~~~

### 3.5 工作树交接风险

实现工作树：

~~~text
G:/AllToNote-video-producer
branch: codex/alltonote-video-producer
HEAD: 32891d352c7df5c9fed0bda19f00e0558b9eb52a
no upstream
relative to master: ahead about 58 commits
staged: 0
tracked dirty files: about 36
tracked diff: about +7613 / -735
untracked files in total: about 573
  - .superpowers schema/acceptance/runtime artifacts: about 508
  - product code/test/config/design files, including this handoff: about 65
~~~

权威文档工作树：

~~~text
G:/AllToNote
branch: master
HEAD: 1b0320924900d8e191e27d7bed3667d18ddd7590
upstream: origin/master
relative to origin/master: ahead about 9 commits
staged: 0
tracked design documents modified: about 6
untracked documents: about 50
~~~

以上数字是 2026-07-19 的快照，实施时必须重新记录。后续 Agent 绝不能直接开生产重构。Wave 0 必须先由集成 owner：

1. 生成逐路径资产清单；
2. 复跑既有 Gate；
3. 明确每组修改的归属；
4. 在得到相应授权后，将实现、验收和权威文档分别形成可追溯的基线提交；
5. 把权威文档基线带入实现分支；
6. 从该已验证基线新建干净的 codex/* 工作树再开始重构。

如果没有提交或集成授权，必须停在资产清单和基线报告，不得擅自 stage/commit 用户修改，也不得继续叠加高风险生产重构。

冻结方案按优先级：

1. 经授权的本地 checkpoint commits
   对生产代码、测试、经审查的 config、spec/tasks 使用显式路径 staging；实现与权威文档分别形成可审查提交；不 push 除非另有授权。明确排除 .superpowers、jobs.sqlite、Checkpoint、Transcript、Evidence、Model result、Bundle 和 acceptance runtime 产物；config/downloader.json 按本地用户配置单独审查，不能盲目纳入。

2. 无提交授权时的可恢复证据包
   保存 tracked binary patch、65 个产品相关 untracked 文件清单与副本、SHA-256 manifest、branch/HEAD/status/diff stat，并在隔离目录验证能恢复。只保存 git diff 不合格，因为它遗漏 untracked 文件。

两个目录是同一 Git 仓库的 linked worktree，共享 objects、refs、remotes、config 和 stash，但拥有独立 worktree/index。不得删除、prune、move 或强制切换任何 worktree。仓库另有 iwiki worktree，也不在本轮范围。

始终禁止：

- git add -A；
- git reset --hard；
- git checkout --；
- git clean；
- stash -u；
- git add .；
- git commit -am；
- 从 HEAD 复制热点文件覆盖当前版本；
- 把未归属修改当作自己的清理对象。

高冲突热点：

- backend/app/adapters/jobs/sqlite_repository.py；
- backend/app/cli/main.py；
- backend/app/core/application/video_service.py；
- backend/app/core/ports/jobs.py；
- backend/app/core/sdk.py；
- backend/app/runtime.py；
- backend/app/gpt/codex_app_server_client.py。

多 Agent 协作时，每个热点只能有一个写 owner。

当前共享 Git 配置 core.autocrlf=true，且缺少统一根 .gitattributes。不得在架构重构中顺便做全仓 EOL 归一化、修改共享 autocrlf 或运行会整文件重写的 formatter；出现大面积仅 EOL diff 时立即停止。

---

## 4. 目标架构与责任边界

~~~mermaid
flowchart TD
    CLI["CLI（一等产品入口）"]
    Desktop["Desktop / EXE（薄 UI）"]
    MCP["MCP（可选入口）"]
    Produce["共享应用服务 / 薄 ProduceService"]
    Registry["静态官方 Recipe Registry"]
    Video["Video Recipe"]
    Document["Document / PPT Recipe"]
    AgentRecipe["UE5 / Codebase Agent Recipe"]
    JobStore["一个 durable JobStore"]
    Direct["Foreground Direct Executor"]
    Engine["按需 Engine"]
    Workers["多进程 Workers"]
    Resources["Resource Admission"]
    Artifacts["开放 Markdown / Artifact"]
    Review["Review / Publisher"]

    CLI --> Produce
    Desktop --> Produce
    MCP --> Produce
    Produce --> Registry
    Produce --> JobStore
    Registry --> Video
    Registry --> Document
    Registry --> AgentRecipe
    Produce --> Direct
    Produce --> Engine
    Engine --> Workers
    Engine --> Resources
    Direct --> Video
    Direct --> Document
    Direct --> AgentRecipe
    Workers --> Video
    Workers --> Document
    Workers --> AgentRecipe
    Video --> Artifacts
    Document --> Artifacts
    AgentRecipe --> Artifacts
    Artifacts --> Review
~~~

### 4.1 Core-centric

- Core/Application 是 CLI、Desktop、MCP 的唯一业务语义来源；
- CLI handler、Desktop command、Engine Host 只做输入适配、传输和生命周期；
- Desktop 不解析 human CLI stdout；
- Engine Host 不拥有第二套 Job/Recipe Pipeline；
- Desktop 不拥有 Source、Recipe、Quality、Review 或 Publish 规则；
- 通用 Core/Application/Repository 最终不导入 Video、Document、UE5 类型；
- 基础设施实现 Port，不能反向成为领域真相。

### 4.2 CLI-first，不是 CLI-as-core

不安装 Desktop 时，CLI 必须完成：

- produce；
- list / wait / cancel / retry；
- detach / batch；
- 读取结果；
- 可选 quality / review / publish；
- 查看资源等待原因；
- 选择 Agent 授权模式。

CLI JSON/JSONL 是自动化合同；human 输出不是内部 RPC。Desktop 使用同一 typed service 或本地 typed transport。

### 4.3 模块化单体

模块化单体不等于单 OS 进程。Engine 和 Worker 是本地生命周期和故障隔离边界，不是微服务。当前阶段：

- 不拆 per-Recipe 服务；
- 不引入消息中间件；
- 不引入远程控制面；
- 不引入 Redis/PostgreSQL；
- 不为第三方 Recipe 建动态加载；
- 通过 contracts、组合根和 import Gate 保持模块边界。

### 4.4 静态官方 Recipe Registry

Registry 负责：

- 组合根显式注册官方 Recipe descriptor 和 endpoint；
- 确定性 list / describe / resolve；
- 重复 key 和未知版本稳定失败；
- 构建后只读；
- Job 创建时固定 Recipe、Pack、Runtime 和 Executor 版本。

Registry 不负责：

- Python entry point 或目录扫描；
- 动态下载执行；
- YAML DAG；
- Worker 或资源调度；
- Result 持久化；
- Review / Publish。

未来官方可选 Pack 可以通过受信任安装清单加入组合根。Descriptor 可轻量读取，重实现只在真正选择 Recipe 后加载。静态 Registry 不意味着把 OCR、Whisper、Agent 等所有重 Pack 打进基础 EXE。

### 4.5 薄 ProduceService

X0-A 中 ProduceService 只负责：

1. 通用 envelope 校验；
2. selector 固定和 Recipe resolve；
3. request/recipe identity；
4. 委托 submit；
5. 返回 durable ProduceSubmission。

X0-A 的 RecipeEndpoint.submit 只是包裹现有 VideoService 的兼容迁移接缝，不是最终 Job 所有权边界。

最终不变量：

- durable Job 创建由共享 ProduceService/JobService 负责；
- Recipe 可以校验、准备和执行领域步骤；
- Recipe 不自建 JobStore 或 Job 生命周期；
- Recipe 不绕过共享 atomic commit；
- Recipe 不直接写正式知识库；
- 最终字段由 Video 与第一个真实非 Video Recipe 共同抽取。

ProduceService 永远不负责 Video 下载、PDF OCR、UE5 探索步骤、CLI 渲染、通用 DAG 或强制 personal Review。

### 4.6 一个 durable JobStore

一个 JobStore 是一个逻辑事实源，不要求所有数据塞在一张表。

它保存：

- Job identity、Recipe/Pack/Executor/config identity；
- 请求引用和 client idempotency；
- Attempt、Checkpoint、状态、事件和 cursor；
- claim、heartbeat、fencing 和取消；
- Artifact/result metadata；
- 恢复所需最小配置。

大体积 Markdown、截图、转录、日志和 Bundle 正文走文件 Artifact。Machine State 位于 Vault 之外。JobStore 损坏时 fail closed；删除 JobStore 不得删除已 portable committed 的知识。

### 4.7 Foreground Direct 与按需 Engine

Foreground：

- 当前进程直接执行；
- 仍先建立 durable Job；
- 不要求 Engine；
- 保持既有 wait、取消和恢复语义。

Detach / Batch / Desktop background：

- 按需 ensure Engine；
- 一个 state root 最多一个 scheduler leader；
- leadership 不等于 execution ownership；
- 每个 Job 独立 claim；
- Engine 空闲退出；
- Worker 使用独立进程。

Engine 只监听本地 IPC，必须有同用户 ACL/认证、版本握手和 fail-closed 兼容检查，不监听 LAN/Internet。

### 4.8 资源感知但不阉割 Agent

必须分开两个概念：

~~~text
ExecutionGrant
  = Agent 可以做什么

ResourceAdmission
  = 现在可以同时运行多少个
~~~

资源控制递进实施：

| 层 | 能力 | 何时实施 |
|---|---|---|
| R0 | worker 上限、agent/provider slot、workspace.publish、repo.write | 必做 |
| R1 | GPU exclusive/shared、FFmpeg/OCR CPU slot | 有真实消费者后 |
| R2 | memory watermark、复杂 provider rate bucket | 有基准证据后 |

不建立通用资源 DSL、求解器或 stage DAG。

仓库已有 MachineResourceLeaseStore / ResourceOwner / ResourceLease。R0/R1 应先评估并复用其机器资源独占与 fencing 语义，避免发明第二套资源 lease store；但它不携带 job_id，不能充当 JobExecutionAuthority，也不能替代 JobStore 内与 checkpoint/commit 同事务验证的 execution claim。

Agent 授权模式：

| 模式 | 场景 | 能力 |
|---|---|---|
| safe | 网页、PDF、Video 等不可信输入 | 来源只读、staging 写、受控工具 |
| project | 用户明确授权的 repo/worktree | 可构建、索引、调用项目工具和写中间产物 |
| native-trusted | 用户明确选择的本地 Codex/Claude CLI | 保留原 CLI 正常能力；平台只监督进程、预算、目录、提交和发布 |

调度器可以延迟启动、限制并发、管理预算和生命周期；不能改写 Agent 推理语义、偷偷删除工具或让来源文本扩大授权。

### 4.9 开放 Markdown/Artifact

- Draft 和正式知识都以开放文件存在；
- Markdown、附件和 portable provenance 是长期资产；
- JobStore、索引和缓存不是知识真相；
- Evidence 可以定位到视频时间、PDF 页、Slide、Git revision 或代码位置；
- Agent 可以不通过 AllToNote 私有 API 直接读取 Markdown；
- Artifact commit 具有原子可见性；
- 删除 JobStore、索引和缓存后，已提交知识仍能被独立工具读取。

### 4.10 Review/Publish 按目标治理

~~~text
Produce succeeded
  = Draft/Artifact 已完整提交并可立即使用

Review/Publish
  = 对需要成为正式或共享知识的 Draft 执行治理
~~~

- Quality 默认不阻塞 personal Draft；
- Job success 不等于 approved/published；
- Review 和 PublishTransaction 使用独立状态机；
- personal 是轻量默认目标；
- common 需要额外明确确认；
- Publisher 使用 plan/apply、冲突检测和幂等；
- Recipe、Worker、Agent 和 MCP 不得通过 AllToNote 接口绕过 Publisher。

### 4.11 薄 Desktop / EXE

Desktop 可以拥有来源选择、Recipe profile、进度、资源等待、cancel/retry、预览和 Review/Publish 操作。

Desktop 不能拥有 Recipe Pipeline、Job 状态推进、恢复决策、Publisher 规则、第二份 JobStore、human stdout 解析、任意命令执行或 Secret 暴露。

---

## 5. 最小公共合同与暂缓抽象

### 5.1 X0-A 允许的最小合同

~~~text
RecipeKey
RecipeDescriptor
InputDescriptor
ProduceRequest
ProduceSubmission
RecipeEndpoint.submit
RecipeRegistry.list / describe / resolve
ProduceService.submit
~~~

约束：

- immutable 或等价不可变；
- descriptor collections 使用稳定 snapshot；
- selector 解析确定；
- Recipe 参数归 Recipe 所有；
- durable request 只保存 secret reference，不保存明文 Secret；
- ProduceSubmission 不携带 Video-specific result；
- submit 与 wait 分离；
- ProduceService 不创建线程、不执行重工作、不保存全局 active Recipe/Workspace。

### 5.2 X0-A 不允许提前冻结

只有 Video 一个真实消费者时，不得建立：

- 通用 PreflightReport；
- 通用 RecipePlan；
- 通用 RecipeOutput；
- 通用 ProduceResult 树；
- codec registry；
- 通用 atomic result commit SPI；
- 第三方插件 SDK；
- 任意 Recipe DAG。

### 5.3 Video 兼容边界

~~~text
Generic ProduceRequest
  -> ProduceService
  -> Static Registry
  -> VideoRecipeAdapter
  -> existing VideoProduceRequest
  -> existing VideoService
  -> existing Job / Checkpoint / Portable / result_json
~~~

Adapter 继续调用现有 Video 请求持久化、Job hash、Video/Checkpoint hash、config snapshot 和 atomic commit。禁止复制这些算法。

### 5.4 默认版本

无版本 legacy produce video 继续默认 Video v1。X0-A 不得静默切到 v2。未来默认升级必须独立决策、独立迁移和独立通知。

### 5.5 CLI 心智模型

~~~text
alltonote produce <input> [--recipe <id>@<version>]
alltonote produce --request <request-or-manifest>
alltonote recipe list
alltonote recipe describe <selector>
alltonote job list
alltonote job wait <job-id>
alltonote job cancel <job-id>
alltonote job retry <job-id>
~~~

保留 legacy：

~~~text
alltonote produce video ...
~~~

X0-A 不新增 add，也不新增独立 run 作为主产品动词。

---

## 6. 分阶段实施计划

上一阶段 Gate 未通过，下一阶段不得通过增加抽象、跳过恢复或扩大并发继续。

### Wave 0：权威文档、资产归属与可重复基线

#### 目标

把两个 dirty 工作树收敛为可追溯基线，消除旧 X0 和 Engine 状态歧义。

#### 必须完成

1. 保存 branch、HEAD、status、diff stat 和逐路径资产清单；
2. 由集成 owner 明确每组修改归属；
3. 使用锁定 .venv 运行全量和关键定向测试；
4. 将失败分类为产品缺陷、环境漂移、工作树未完成或不稳定测试；
5. 记录 Python、SQLite、FFmpeg、Agent CLI、Provider、Runtime 和 iwiki 版本；
6. 同步 ARCH、REC-CONTRACT、RUNTIME、ENGINE、master tasks 和 coverage matrix；
7. 删除旧 add/run/8 DTO/X0 Repository 泛化冲突；
8. 明确 Wave 1A 完成、X0-A/X0-B/Parallel 未完成；
9. 明确 Engine 已被产品需求触发，但仍不属于 X0-A；
10. 得到授权后形成可追溯基线提交，并从验证后的基线创建干净 codex/* 工作树。

#### Gate

- G0-1：精确记录测试命令、通过/失败数和失败原因；
- G0-2：历史 1820 passed 记录已复跑或明确标记为非当前证据；
- G0-3：权威文档只有一种 AllToNote/Production/Recipe 解释；
- G0-4：旧 RX 与修订 X0-A/X0-B 不再冲突；
- G0-5：master tasks 和 coverage matrix 状态一致；
- G0-6：实现、验收和权威文档已经进入同一可追溯基线；
- G0-7：没有生产语义改动。

#### Stop

- 无法确定依赖锁；
- 基线失败无法复现或归因；
- 无权 stage/commit 现有资产；
- 需要删除或覆盖未提交工作；
- 上位文档要求静默改变 Video 默认版本。

### Wave 1：Recipe X0-A 最小兼容接缝

按 ../recipe-x0-compatibility-extraction/tasks.md 的 8 个任务执行：

1. 冻结兼容和执行基线；
2. 只归位 JobState；
3. 最小 Recipe submission 合同；
4. 静态 Registry 与薄 ProduceService；
5. Video Adapter；
6. SDK/Runtime 组合根；
7. 单一 produce CLI 心智；
8. 架构、兼容和冷路径 Gate。

必须保持：

- legacy submit_video；
- Video v1/v2 canonical request；
- Job idempotency hash；
- Video/Checkpoint hash；
- config snapshot；
- legacy raw request_json/result_json；
- historical candidate；
- crash/reopen/zero replay；
- Portable manifest、Bundle identity、字节确定性；
- source identity CAS；
- cancel/retry/outcome unknown/fencing；
- CLI JSON/Human golden、exit code、error category。

禁止：

- DB schema 或 result_json wire 变化；
- Result/Commit 泛化；
- Engine、Worker、线程池；
- 动态插件或 DAG；
- add/独立 run；
- 重写 Video 内核；
- 用 object/arbitrary mapping 伪通用化 JobSnapshot.result。

Gate：

- G1-1：generic 和 legacy 对同请求产生相同 Job identity、canonical request、两套 hash 和 config snapshot；
- G1-2：显式 generic v2 与 legacy v2 的 result/Portable 等价；
- G1-3：无版本 legacy 仍是 v1；
- G1-4：contracts、registry、produce_service 无 Video import；
- G1-5：recipe list/describe 不加载 Runtime、Downloader、Whisper、Torch、FFmpeg、FastAPI 或模型 client；
- G1-6：旧数据库可打开，旧成功 Job 可查，旧未完成 Job 可恢复；
- G1-7：全量回归 0 failed，或只剩 Wave 0 明确登记且完全相同的环境阻塞；
- G1-8：未引入新全局锁或全局 mutable request。
- G1-9：SDK 不为满足测试而继续暴露或持有生产级 _video_service 反向依赖；需要注入 VideoService 的测试由 fixture 显式返回 service/runtime。

Stop：

- 必须改变 request/hash/CLI golden 才能接入；
- Adapter 复制 VideoService；
- 为 Fake Recipe 发明 DTO；
- 需要 schema change；
- diff 超出约 1,600 行且不能逐项映射到 8 个任务。
- 为保留测试私有访问而保留错误的生产依赖；test_fake_video_producer.py、test_local_media_golden_path.py、test_platform_subtitle_golden_paths.py 的重接线必须由一个 owner 串行完成。

### Wave 2：并发正确性 C0

#### Codex client

使用 Barrier/Latch 构造同一 client 的 2、4、8 个并发 turn，验证 request ID、response、stderr、error、approval 和 cancel 不串线。只有测试真实失败后才重构 correlator；若未复现，保留生产代码并提交反证。

#### SQLite

1. 记录开发、CI、最终 EXE 三处实际 sqlite3.sqlite_version；
2. 锁定官方已修复相应 WAL reset 问题的项目验证版本；
3. 不能用简单 version >= 3.50.7 判定安全，因为 3.51.0–3.51.2 会被错误放行；
4. 至少表达为经过验证的 allowlist/range，例如 3.50.x 的 x >= 7、3.51.x 的 x >= 3，3.52+ 仍需项目锁定与验证；
5. 运行 1/4/8/16 多连接 WAL、busy、checkpoint、crash/reopen 和 integrity 测试；
6. 不预设采用哪个 Python 包装方式，以 Windows EXE 实际链接版本为准。

Gate：

- G2-1：并发 turn 无错配、悬挂或共享 stderr 污染；
- G2-2：cancel/timeout 只影响目标 turn；
- G2-3：开发、CI、EXE 使用同一受支持能力基线；
- G2-4：integrity_check 为 ok，foreign_key_check 为空；
- G2-5：多连接无 corruption、lost row、hang 或重复 commit；
- G2-6：尚未移除 execution lock 或 scheduler lease。

Stop：

- 只有猜测，没有失败测试；
- 测试环境升级但 EXE 仍旧；
- 通过全局 client 锁掩盖重入错误；
- WAL 出现数据损坏或不确定 commit；
- 需要改变 Video atomic result 语义。

### Wave 3：真实第二消费者纵切与 X0-B

选择一个最小但真实的 local PDF 或 PPTX 纵切，不同时实现 OCR、Vision、所有文档格式和全部模板。

最小纵切：

~~~text
real local document
  -> extract / normalize
  -> deterministic fake model in tests
  -> usable Markdown Draft
  -> page/slide Evidence
  -> durable Job/result query
  -> crash/reopen without duplicate expensive work
~~~

只把 Video 和 Document/PPT 都真实需要的概念提升到 Core：

- result envelope；
- Artifact manifest/reference；
- Recipe/result discriminator 和 schema version；
- durable result query；
- atomic Artifact commit 与 Job terminal transition；
- 真实共享的 source/evidence identity。

Transcript、timeline、citation、page、bbox 等单消费者字段留在 Recipe。

同时完成 X0-A 的迁移债务：durable Job 创建最终归共享 ProduceService/JobService，Recipe 不自建 JobStore、Job 生命周期或 commit。

还必须抽取通用 JobExecutionRuntimeFactory：

- reconnect、job wait、Engine 和 Worker 按已持久化的 Recipe/Pack/Runtime identity resolve 执行器；
- Worker 接管的是既有 Job authority，不得再次调用 RecipeEndpoint.submit 创建第二个 Job；
- legacy Video runtime factory 可以作为具体注册项保留，但不能再成为 generic reconnect 的硬编码唯一入口；
- 缺失 exact Recipe/Pack 版本时 fail closed，并给出可恢复诊断，不能静默换成当前默认版本。

Gate：

- G3-1：两个真实 Recipe 共享 ProduceService、JobStore、Checkpoint 和 commit；
- G3-2：Fake 不再是多 Recipe 唯一证据；
- G3-3：每个 common 字段都能指出两个真实消费者；
- G3-4：通用层不使用 arbitrary mapping 伪装 Recipe-specific rich result；
- G3-5：legacy Video result dual-read；
- G3-6：旧成功和旧未完成 Job 可查询/恢复；
- G3-7：两 Recipe crash/reopen 不重复昂贵副作用；
- G3-8：通用 Job/Repository 无 Recipe-specific import；
- G3-9：schema migration 可重复、可重开，integrity 和 foreign key 检查通过；
- G3-10：第三方插件 SDK 仍未冻结。
- G3-11：generic reconnect 根据 persisted Recipe/Pack resolve；不存在 Video-only job wait。

Stop：

- 第二消费者只是 Fake；
- Document 被迫模拟 Video transcript；
- common 字段只有 Video 使用；
- migration 无旧库 fixture；
- atomic commit 变成可观察半提交；
- SQLite migration 与 Engine migration 被并行编辑。
- Worker 需要通过 submit 重新建 Job 才能执行。

### Wave 4：开放 Artifact 与 Review/Publisher 产品闭环

#### Artifact

1. Produce 成功后 Draft 在不启动 Review/Publisher 时可读；
2. Video 和 Document/PPT 都输出开放 Markdown、附件和 provenance；
3. 删除 JobStore、索引和缓存后，用独立 Markdown parser、iwiki/Obsidian 或不依赖 AllToNote 的测试程序读取；
4. 验证相对附件链接和 Evidence；
5. Runtime 不可用时内容仍可使用；
6. crash 在 portable commit 前后不会产生被误判为成功的半成品。

#### Review/Publisher

1. Job、Review、PublishTransaction 使用独立状态机；
2. Draft 在 review 前立即可用；
3. rejection 不把已成功 Produce Job 改成 failed；
4. personal publish 使用轻量显式调用；
5. common 额外确认；
6. Publisher 使用 plan/apply、冲突检查和幂等；
7. 内容改变后旧 approval/plan 失效；
8. Recipe/Worker/Agent/MCP 不能通过平台接口直接写 common；
9. CLI-only 可以完成 review/publish。

Gate：

- G4-1：删除 Machine State 后已提交知识仍独立可读；
- G4-2：Draft/正式知识都不是私有二进制格式；
- G4-3：personal Draft 不被 Quality/Review 阻塞；
- G4-4：Review rejection 不倒退 Job；
- G4-5：同一 PublishPlan 重复 apply 幂等；
- G4-6：common publish 有额外确认和冲突检测；
- G4-7：Artifact commit、source identity、result 和 terminal transition 保持原子。

Stop：

- 知识正文只存在数据库或索引；
- 删除 JobStore 会删除正式知识；
- Review 复用 JobState；
- Recipe 或 Agent 可以绕过 Publisher；
- 为 personal Draft 强制完整治理。

### Wave 5：按需 Engine、per-job claim 与隔离 Worker

顺序必须是：

~~~text
Engine lifecycle
  -> per-job claim/fencing
    -> isolated Worker
      -> 保持 max concurrency = 1
        -> 完成故障 Gate
          -> 才允许提高并发
~~~

必须实现：

- 一个 state root 一个 leader；
- local IPC 同用户安全与版本握手；
- foreground 不依赖 Engine；
- detach/background 才 ensure Engine；
- Engine idle 退出；
- 每 Job 独立 generation authority、heartbeat、expiry、fencing；其下可顺序创建多个 Step Attempt；
- Engine/Worker 通过 JobExecutionRuntimeFactory 恢复既有 Job，不调用 RecipeEndpoint.submit；
- stale Worker 不能 checkpoint/commit；
- Worker 独立进程；
- protocol version、stdout/stderr/event size 和 backpressure；
- Windows process tree cleanup；
- Engine/Worker crash reconcile。

Gate：

- G5-1：32 个竞争 claim 对同一 Job 最多一个成功；
- G5-2：32 个不同 Job 可被独立 claim；
- G5-3：fencing 单调，stale 写全部拒绝；
- G5-4：foreground 无 Engine 完成；
- G5-5：并发 ensure 最终只有一个 leader；
- G5-6：Engine kill/restart 可 reconcile；
- G5-7：单 Worker kill 不影响其他 Job；
- G5-8：orphan Worker 无 authority；
- G5-9：Windows 父子进程树有界回收；
- G5-10：冷启动 p95 < 2 秒，idle 目标 < 100 MB；
- G5-11：Engine 不 eager-load 重 Pack。

Stop：

- 先删全局锁再碰运气；
- global lock 只是换名字；
- claim 只在内存；
- Engine 成为所有 CLI 必经常驻进程；
- Worker 共享可变 VideoService singleton；
- Windows 无法回收进程树；
- IPC 损坏可误写 success。

### Wave 6：资源准入、detach、batch 与高并行

先完成 R0，再由真实消费者决定 R1/R2。

必须实现：

- worker/agent/provider capacity；
- workspace.publish 和 repo.write；
- 资源等待原因；
- 用户可配置并发上限；
- Job 内 fan-out 和 Job 间并发使用同一层级预算；
- detach 返回前 Job durable；
- CLI 退出后继续；
- batch 每项独立 Job、cancel、retry 和恢复；
- 完成后明确输出 Markdown/Artifact 路径。

Gate：

- G6-1：1/4/8/16 Worker 受控负载完成；
- G6-2：active count 不超用户上限和资源容量；
- G6-3：4 Worker 在受控非外部依赖 fixture 上至少达到 1 Worker 的 2.4 倍吞吐；
- G6-4：8/16 Worker 无正确性退化、corruption 或 hang；
- G6-5：32 Job 无丢失、重复 claim 或重复副作用；
- G6-6：Provider/GPU/Agent/publish/repo contention 可解释；
- G6-7：资源不足是 waiting，不是业务 failed；
- G6-8：单 batch 项失败不阻塞其他项；
- G6-9：真实 Provider 不以固定倍数验收，而记录成功率、限流和等待原因；
- G6-10：Engine idle 后退出。

Stop：

- Scheduler 硬编码 Recipe stage；
- 资源控制改变 Agent 能力；
- 无界队列直接映射无界进程；
- 16 Worker 被误解为 16 个 GPU/Agent 重任务必须 active；
- 为吞吐破坏 atomic commit；
- 后台任务依赖原 CLI 存活。

### Wave 7：完整 AgentExecutor 与 UE5/Codebase 纵切

ModelExecutor 与 AgentExecutor 保持独立。

必须实现：

- 每 Agent Attempt 独立 subprocess 和 staging/worktree；
- 多轮、工具、项目读取、授权构建和 staging 写；
- 记录 Agent/Runtime 版本、grant、source revision、预算、目录和 receipt；
- progress/event、cancel、timeout、heartbeat 和 process tree cleanup；
- safe/project/native-trusted；
- native-trusted 与锁定 Agent CLI 的正常能力等价；
- 来源不能提升 grant；
- Agent 结果先成为 Artifact；
- 正式知识仍经 Publisher；
- 一个真实 UE5/Codebase 最小纵切。

Gate：

- G7-1：三个 grant 模式有能力矩阵和测试；
- G7-2：Prompt Injection fixture 无法提权；
- G7-3：真实 Agent 支持多轮和至少一个工具调用；
- G7-4：至少两个 Agent 并行且不串 stdout/stderr/session；
- G7-5：取消一个不影响另一个；
- G7-6：hang 能回收完整进程树；
- G7-7：unknown outcome 不自动重跑完整 Agent；
- G7-8：native-trusted 未被静默 read-only/no-tools；
- G7-9：Agent 不能绕过 Publisher；
- G7-10：UE5/Codebase 输出开放 Markdown/Artifact。

Stop：

- AgentExecutor 只是单次 completion 别名；
- 多 Agent 共享可写 worktree；
- 来源修改 grant；
- native-trusted 名义开放、实际阉割；
- 无法区分并发 Agent session；
- Agent 可直接写 common。

### Wave 8：薄 Desktop / EXE

必须实现：

- Desktop 与 CLI 使用同一 typed Produce/Job/Review/Publish API；
- 不解析 human stdout；
- 不维护第二套状态机或数据库；
- 打开 UI 不启动 Engine 或重 Pack；
- 第一次后台 Production 才 ensure Engine；
- 多来源拖入、进度、等待原因、cancel/retry、Artifact 打开；
- UI 关闭后 detached Job 继续；
- personal Draft 立即可用；
- shared/common 走相同 Publisher；
- 基础安装与可选 Pack 分离；
- Windows 安装包真实 E2E。

Gate：

- G8-1：CLI 创建、Desktop 观察、CLI 取消同一 Job；
- G8-2：Desktop 与 CLI 错误 category 和 event 一致；
- G8-3：UI 重连不重复提交；
- G8-4：UI 关闭不终止 detached Job；
- G8-5：Desktop cold open 不启动 Engine/Worker；
- G8-6：32 Job 事件更新不阻塞 UI；
- G8-7：Tauri command 是白名单 typed API；
- G8-8：EXE 实际 SQLite/Python/Core/bridge 版本与验收环境一致。

Stop：

- UI 拥有 Job/Recipe/Publisher 逻辑；
- Desktop 解析 human stdout；
- UI 建第二份 JobStore；
- 每次单任务启动常驻 Engine；
- 打包环境与开发环境能力漂移。

### Wave 9：最终架构验收

只有第 11 节所有条件和第 8 节 Release Gate 全部通过，才能宣称达到条件式最优框架。

---

## 7. 依赖与并行开发规则

硬依赖：

~~~text
Wave 0
  -> Wave 1 X0-A
    -> Wave 2 C0
      -> Wave 3 real consumer + X0-B
        -> Wave 4 Artifact / Review / Publisher
          -> Wave 5 Engine / claims / Worker
            -> Wave 6 resources / detach / batch
              -> Wave 7 AgentExecutor
                -> Wave 8 Desktop
                  -> Wave 9 final release
~~~

可以并行准备：

- 文档同步与只读代码盘点；
- 不同测试文件的 characterization；
- Engine benchmark harness 与 Document fixture；
- Desktop UX 研究与 Core 实现；
- Agent grant 测试设计与 Worker 基础研究。

必须串行编辑：

- SQLite schema/migration；
- legacy result decoder；
- atomic commit；
- Runtime composition root；
- CLI parser/routing；
- Video request/hash/persistence；
- Codex client correlation。

每个热点只有一个写 owner。推荐每个 Wave 再拆成：

1. characterization/failing test；
2. 最小生产改动；
3. compatibility adapter；
4. migration；
5. fault/concurrency test；
6. docs/status sync。

禁止先改完所有生产代码再一次补测试。

---

## 8. 验证策略与发布 Gate

### 8.1 三层 Gate

1. Task Gate
   当前任务的单元、契约和定向集成测试。失败时只允许修当前任务。

2. Phase Gate
   当前 Wave 全部测试，加上此前冻结的兼容与恢复测试。失败时不得启用新入口、迁移下一版 schema 或提高并发。

3. Release Gate
   全量 Backend、真实 Windows/EXE、真实 Agent CLI、4/8/16 Worker、故障注入、冷启动和 UX 场景。只有这一层通过，才能宣称相应能力完成。

### 8.2 测试实现规则

- 单元测试遵守 FIRST；
- 使用 Fake clock、Barrier、Event、Latch 和显式注入点；
- 不使用 sleep 制造竞态；
- 真实 subprocess 可用有界 timeout 等待退出，但不能靠猜测时间判断状态；
- SQLite 跨进程测试使用多个独立连接，不能用同连接多线程冒充；
- Golden 失败不能自动更新 fixture；
- 性能 Gate 记录机器、Python、SQLite、Agent CLI、CPU、RAM、GPU、磁盘和容量；
- 每项并发测试覆盖正常、边界容量、取消、竞争、崩溃、幂等和 stale owner；
- 每个 migration 都有真实旧库 fixture、升级、重开、重复升级和故障注入；
- crash 测试不仅断言最终成功，还断言副作用调用次数；
- 不能通过扩大 timeout、吞异常或删除断言通过 Gate。

### 8.3 当前基础命令

~~~powershell
Set-Location G:\AllToNote-video-producer
git status --short --branch
git diff --stat
git diff --check

$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_ADDOPTS = '-p no:cacheprovider'

Set-Location G:\AllToNote-video-producer\backend
$AllToNotePython = '..\.venv\Scripts\python.exe'
& $AllToNotePython -m pytest -q
~~~

核心兼容快速集：

~~~powershell
& $AllToNotePython -m pytest -q tests\core\test_video_request_persistence.py tests\core\test_job_service.py tests\contracts\test_cli_envelope_golden.py tests\core\test_checkpoint_runner.py tests\core\test_model_call_coordinator.py
~~~

当前复现结果为 68 passed。关键恢复样本当前复现为 6 passed。这些只是快速基线，不替代全量 suite。

完整兼容集合至少覆盖：

- tests/core/test_video_request_persistence.py；
- tests/core/test_job_service.py；
- tests/adapters/test_sqlite_job_repository.py；
- tests/core/test_checkpoint_recovery.py；
- tests/core/test_checkpoint_runner.py；
- tests/core/test_execution_safety.py；
- tests/core/test_model_call_coordinator.py；
- tests/adapters/test_iwiki_portable_gateway.py；
- tests/integration/test_fake_video_producer.py；
- tests/integration/test_platform_subtitle_golden_paths.py；
- tests/integration/test_local_media_golden_path.py；
- tests/integration/test_video_bundle_assembly.py；
- tests/cli/test_produce_video_cli.py；
- tests/cli/test_job_cli.py；
- tests/contracts/test_cli_envelope_golden.py；
- tests/cli/test_runtime_bootstrap.py；
- tests/runtime/。

最终仍必须运行：

~~~powershell
& $AllToNotePython -m pytest -q
& $AllToNotePython -m pytest -q -m windows_smoke
git diff --check
~~~

如果某些 opt-in 测试不存在，当前 Wave 负责创建，而不是静默跳过。

### 8.4 总体验证矩阵

| Gate | 场景 | 方法 | 通过标准 |
|---|---|---|---|
| V-01 | legacy Video v1 | frozen canonical fixture | request JSON、Job hash、Video hash 完全一致 |
| V-02 | explicit Video v2 | legacy 与 generic 双入口 | request、binding、Job、result、Artifact 等价 |
| V-03 | config snapshot | submit + reopen | 漂移被检测，冻结配置可恢复 |
| V-04 | raw result compatibility | 真实旧 SQLite fixture | 旧成功 Job 可查询 |
| V-05 | checkpoint recovery | 每个边界故障注入 | 不重复 download/model/commit |
| V-06 | Portable determinism | 同 fixture 两次 | manifest、inventory、Bundle identity 一致 |
| V-07 | Registry | duplicate/unknown/list/resolve | 稳定错误、确定顺序、无动态扫描 |
| V-08 | cold import | 独立 subprocess import probe | list/describe 不加载重 Runtime/Pack |
| V-09 | dependency | AST/import Gate | common contracts/service 无 Recipe import |
| V-10 | second Recipe | 真实 PDF/PPTX fixture | 同一 Produce/Job，输出可用 Markdown |
| V-11 | result migration | legacy/new dual-read | 两种结果均可查、未知类型 fail closed |
| V-12 | client correlation | 2/4/8 concurrent turns | 响应、stderr、error、cancel 不串线 |
| V-13 | concurrent submit | 32 fake jobs | 无丢失、重复身份、重复领取 |
| V-14 | fencing | stale generation injection | stale start/checkpoint/commit 全拒绝 |
| V-15 | cancel vs commit | deterministic race | 只有规范允许的确定终态 |
| V-16 | Engine restart | kill leader/restart | reconcile 且无重复副作用 |
| V-17 | Worker crash | kill one worker | 其他 Job 继续，目标 Job 可恢复 |
| V-18 | resource admission | provider/GPU/publish/repo contention | active 不超容量，等待原因正确 |
| V-19 | throughput | Worker 1/4/8/16 | 4 Worker 受控 fixture 至少 2.4x |
| V-20 | lightweight Engine | 20 次 cold start + idle RSS | p95 < 2s，idle 目标 < 100 MB |
| V-21 | foreground | 单次 produce | 不启动 Engine，行为与基线一致 |
| V-22 | detached | 关闭 CLI/Desktop | Job 继续并可重连 |
| V-23 | Agent grants | safe/project/native fixtures | 不越权且能力不被暗中阉割 |
| V-24 | open Artifact | Video/Document/UE5 outputs | Markdown 无 AllToNote 私有读取依赖 |
| V-25 | Machine State deletion | 删除 JobStore/index/cache | 已提交知识、附件、provenance 仍可读 |
| V-26 | governance | personal/shared targets | personal 立即可用，shared 必经策略 |
| V-27 | cross-entry truth | CLI create/Desktop observe/CLI cancel | 同 Job、状态、事件和错误 |
| V-28 | packaging | built EXE runtime probe | SQLite/Core/bridge 版本与验收一致 |

### 8.5 两套 Hash 必须分别冻结

| Hash | 含义 | 验证 |
|---|---|---|
| Job request hash | canonical durable request 的幂等身份 | 比较冻结 request bytes 和 repository request_hash |
| Video/Checkpoint hash | 执行输入、Checkpoint 复用和恢复身份 | 比较现有 Video fingerprint/input hash |

Generic 与 legacy 必须得到相同：

- canonical Video request bytes；
- Job request hash；
- Video/Checkpoint hash；
- config snapshot；
- Job ID；
- initial event。

预期常量不能由被测实现自己重新计算再与自己比较，必须使用 frozen fixture 或独立 oracle。

### 8.6 Zero-replay 独立 Gate

| 外部或昂贵操作状态 | 重启后自动重发 |
|---|---|
| 未开始的纯计算 | 可以 |
| 已 durable 成功的模型调用 | 不允许，复用结果 |
| 已 STARTED、结果未知的模型/Agent 调用 | 不允许，进入 outcome unknown |
| 已 durable transcript | 不允许重新转写 |
| 已 durable download/source snapshot | 不允许无理由重新下载 |
| 已完成 candidate Bundle | reconcile，不重复模型工作 |
| 已提交 Portable Bundle | 不允许第二次 commit |
| known retryable failure 且仍有持久化预算 | 可以 |
| 用户显式 retry unknown outcome | 新 Attempt，保留 lineage 和确认 |

Fake provider、transcriber、downloader、Agent 和 portable gateway 都必须有 durable call counter。恢复测试必须检查 counter，而不只比较最终 Markdown。

### 8.7 并发与性能 harness

建议建立独立入口：

- tests/performance/run_parallel_production_benchmark.py；
- tests/performance/test_foreground_cold_path.py；
- tests/performance/test_engine_startup.py。

建议命令：

~~~powershell
& $AllToNotePython tests\performance\run_parallel_production_benchmark.py --jobs 32 --workers 1 4 8 16 --repetitions 5 --output .artifacts\parallel-production-benchmark.json
~~~

受控负载至少包含：

- 同 Workspace 32 个轻量 fake Job；
- 1/4/8/16 Worker；
- 至少 4 个真实 Video/Document-like Job；
- Video + fake Agent 混合；
- 多 Workspace；
- Job 内 fan-out 与 Job 间并发；
- GPU exclusive；
- Provider/Agent capacity；
- workspace.publish exclusive。

规则：

- 先预热一次，再至少 5 次测量；
- cold start 不包含预热样本；
- 真实 Provider/Agent 不做固定倍数断言；
- 受控非外部 fixture 用于固定吞吐 Gate；
- 记录 p50、p95、成功率、重复副作用、SQLite busy、writer lock 和资源等待；
- 8/16 Worker 不要求线性扩展，但不能出现 correctness 回退；
- 如果吞吐相对最佳较低配置下降超过 10%，必须有资源饱和或 profile 解释；
- 16 Worker 是压力配置，不代表 16 个 GPU/Agent 重任务必须同时 active。

time-to-first-Markdown 和 foreground cold path 先由 Wave 0 建基线。后续同 fixture 不能无解释回退；不得凭空设置一个脱离现状的绝对数字。

### 8.8 Packaged Release Gate

最终打包产物必须完成等价操作：

~~~text
alltonote.exe version --json
alltonote.exe runtime info --json
alltonote.exe recipe list --json
alltonote.exe produce video <fixture> --detach --json
alltonote.exe job wait <job-id> --json
~~~

必须从打包进程本身读取实际 SQLite、Python/Runtime、Recipe/Pack、Engine protocol 和 Agent bridge 版本，不能只检查 requirements 或源码环境。

---

## 9. 数据、Claim、事务与恢复规则

### 9.1 X0-A

- SQLite schema 不变；
- legacy Video request_json/result_json 不变；
- Registry 不落库；
- 不拆 Portable commit 与 Job terminal transition；
- JobState 如移动，Video 原模块 re-export 同一个 enum，不能复制第二个类型。

### 9.2 X0-B

- 新结果有 kind 和 schema_version；
- 旧 Video 使用 legacy decoder；
- dual-read 先于新写切换；
- 未知 schema 稳定失败；
- migration 覆盖旧成功、运行中、失败 Job；
- common envelope 不塞 Recipe-specific 大对象。

### 9.3 Job-scoped execution authority

现有 Video 在同一个执行 authority 下会按 step 顺序创建多个 Attempt，因此最小 claim 不能绑定一个单独 step attempt_id。

三层必须分开：

~~~text
Scheduler leadership
  = 谁负责发现和分配可执行 Job

Job execution claim
  = JobExecutionAuthority(job_id + owner/worker + generation) + heartbeat/expiry

Step Attempt
  = 某一步的执行记录，创建时记录并继承当前 Job generation
~~~

规则：

- claim 是 Job-scoped authority；
- SchedulerAuthority 与 JobExecutionAuthority 是两个不同类型，不能用一个含混的 ExecutionAuthority 同时表达 scheduler、Job、Attempt 和 resource lease；
- generation 单调递增；
- 每个 step Attempt 记录当前 generation；
- stale generation 不能 start step、写 external operation、checkpoint、Artifact 或 terminal result；
- scheduler leader、Job claim 和 step Attempt 不能合并成一条 lease；
- Job release/takeover 不得错误修改其他 Job；
- Agent/Worker receipt 必须关联 Job generation 和 step Attempt；
- release 只让目标 Job claim 过期并保留 generation，不能删除后从 generation 1 重建；
- takeover 必须严格递增 generation，避免旧 Worker token 碰撞。

### 9.4 JobStore v1 到 v2

当前 JobStore v1 使用完整 schema 指纹，且 leases 表的 CHECK 仅允许 scheduler。per-job claim 不能通过随手 add table 或只改 user_version 实现。

必须设计显式 v1→v2 migration：

- 真实 v1 数据库 fixture；
- migration 采用 offline/maintenance Gate：确认旧 Engine/CLI executor 已停止，存在有效旧 scheduler owner 时返回 migration busy；
- 使用 SQLite backup API 或经过验证的在线备份；WAL 活跃时不能只复制 jobs.sqlite；
- schema 指纹更新；
- leases 迁移或新 execution_claims 结构的完整决策；
- 旧 leases 建议重命名为 scheduler_leases，或提供同等级 mixed-version fail-closed 围栏；
- 显式保留 EXPECTED_SCHEMA_V1、EXPECTED_SCHEMA_V2 和 MIGRATION_V1_TO_V2，不得覆盖掉输入版本定义；
- user_version 只在完整迁移成功后更新；
- migration 中途故障只能保留完整 v1 或完整 v2；
- 重复执行幂等；
- reopen、integrity_check、foreign_key_check；
- 旧二进制对 v2 的 read-only 或明确拒绝策略；
- legacy request/result/event/checkpoint/source binding 全保留；
- 迁移后先以 v2 serial compatibility 模式跑完整 legacy regression，再实现或启用 per-job claims；
- v1/v2 binary 不允许同时写一个 Store。

### 9.5 事务原则

- 元数据事务短；
- 下载、模型、Agent、FFmpeg、OCR 和大文件操作不在 DB 事务内；
- Artifact 正文不进 SQLite；
- workspace.publish 短时独占；
- Attempt authority、source identity CAS、portable receipt、result record 和 terminal transition 的原子性不能破坏；
- start/transition Attempt、external operation、checkpoint、cancel/fail settlement、source CAS、portable guard、result 和 terminal transition 都必须在各自写事务内验证当前 JobExecutionAuthority；禁止先在一个事务检查 claim、再在另一个事务写状态；
- 如果 Portable callback 当前不能安全移出 writer transaction，先测量，不制造半提交；
- 只有 profile 证明 SQLite writer 是主要瓶颈后，才评估进一步拆分或替换；
- 当前不换分布式数据库。

---

## 10. 轻量性与不过度设计 Gate

每个新抽象必须回答：

1. 它解决哪个已验证问题？
2. 它有几个真实消费者？
3. 只有一个消费者时，为何不能留在 Recipe？
4. 删除它会破坏哪个 Gate？
5. 是否增加普通用户步骤？
6. 是否让单任务启动更多常驻进程？
7. 是否把 rich Recipe 数据伪装成 arbitrary mapping？

默认判定为过度设计：

- 只有 Video 时建立 codec registry；
- 只有官方 Recipe 时建立动态插件市场；
- 为几个固定 Recipe 建通用 DAG/no-code；
- 为静态 Registry 做微基准而不测模型、Agent 和调度；
- add/run/produce 三个主动作；
- 每 Recipe 自建 JobStore、Worker、Review 或 Publisher；
- Desktop 复制 CLI/Core；
- 所有 personal 知识都强制 Review；
- 为无限并发移除资源保护；
- 为安全把所有 Agent 固定为 read-only 单次调用；
- 无 profile 就替换 SQLite；
- 拆 per-Recipe 微服务。

抽象提升条件：

| 抽象 | 最低触发条件 |
|---|---|
| 通用 Result/Commit | Video + 一个真实 Document/PPT 消费者 |
| 公共 Recipe SDK | 至少两个官方 Recipe 稳定，且出现真实外部扩展需求 |
| AgentExecutor | UE5/Codebase 真实纵切需要完整 Agent CLI |
| R1/R2 资源模型 | R0 无法表达已测量瓶颈 |
| DAG/Workflow DSL | 至少三个真实 Recipe 重复同一不可维护编排 |
| 数据库替换 | 修复版 SQLite、短事务和单 writer 优化后仍有可复现瓶颈 |
| 多机调度 | 单机能力不足且产品明确要求跨机 |

---

## 11. 条件式最优框架完成定义

只有以下全部满足，才能宣布完成。

### 11.1 产品结果

- 普通用户用一个 produce 心智得到可立即使用的 Markdown Draft；
- Video、一个真实 Document/PPT、一个 Agent 型 Recipe 走同一平台路径；
- Review/Publish 不阻塞 personal Draft；
- personal/common 治理规则通过；
- CLI、Desktop 暴露的是生产任务、草稿、来源证据和正式知识，不是内部 lease/codec。

### 11.2 解耦

- 新 Recipe 正常只改 Recipe 包、descriptor/manifest 和组合根；
- 不修改 CLI 主路由、ProduceService、JobStore、Desktop 状态机或 Publisher；
- 通用 Application/Domain/Repository 无 Recipe-specific import；
- Video 不再拥有平台 Job、Artifact、Review、Publish 语义；
- common 字段经至少两个真实消费者证明；
- 未发布动态第三方插件 SDK。

### 11.3 轻量

- help/version/read/list 不加载重 Pack；
- foreground 不要求 Engine；
- Desktop 打开不启动 Engine；
- Engine 空闲退出；
- 重 Worker 不常驻；
- 基础安装不携带所有模型/Pack；
- Engine cold start p95 < 2 秒；
- Engine idle 目标 < 100 MB；
- time-to-first-Markdown 和 foreground cold path 无未解释回退。

### 11.4 并发与性能

- 一个 leader 并行管理多个 Job；
- 每 Job 独立 generation/fencing；
- 32 Job 无丢失和重复领取；
- 4/8/16 Worker 正确性 Gate 通过；
- 4 Worker 受控 fixture 至少 2.4x；
- Job 内和 Job 间并发服从统一层级预算；
- Provider、GPU、Agent、publish、repo write 不无界；
- 单 Worker 故障不影响其他 Job。

### 11.5 Agent

- AgentExecutor 不是 ModelExecutor 别名；
- native-trusted 保留用户授权的本地 Agent CLI 正常能力；
- 调度器只控制资源和生命周期，不改写工具语义；
- 每 Agent 独立 process 和 staging/worktree；
- Prompt Injection 不能扩大 grant；
- Agent 不能通过平台接口直接写正式知识。

### 11.6 健壮

- Video v1/v2、hash、Checkpoint、恢复、Portable、CLI 零语义回退；
- 旧数据库和 result 可迁移、重开；
- crash/reopen、zero-replay、outcome unknown、cancel vs commit、stale fencing 有故障注入；
- 不重复付费调用、下载、Agent 或 publish；
- SQLite 开发/CI/EXE 均使用项目验证修复版本；
- JobStore 损坏 fail closed；
- 删除 JobStore/index 不影响已提交知识；
- atomic source/portable/result/terminal 不被并发重构破坏。

### 11.7 开放与治理

- Markdown、附件、Evidence、provenance 不依赖 AllToNote 才能读取；
- Draft 在 Review 前可用；
- Job、Review、PublishTransaction 生命周期独立；
- CLI-only 可完成 Produce 到可选 Publish；
- Desktop 只是共享服务的薄适配器。

---

## 12. 全局 Stop / No-go Conditions

出现任一项必须停止当前 Wave并报告：

1. 需要破坏 Video v1/v2 request、任一 hash、result wire 或 CLI golden；
2. 需要 reset、checkout、clean、stash -u 或覆盖用户修改；
3. 基线无法复现或失败无法归因；
4. migration 可能丢旧 Job、Result、Checkpoint 或 Artifact 引用；
5. Codex client 风险没有失败测试却准备大改 transport；
6. 从 Wave 2 并发 C0 起，开发、CI、EXE SQLite 版本不一致或仍为 3.50.4；该条件不阻塞不改变 schema、不启用多连接 WAL 或并发执行的 Wave 1 X0-A；
7. per-job fencing 未完成就删除全局串行保护；
8. 通用抽象只有一个真实消费者；
9. Engine leader 再次等同于单执行者；
10. foreground 被迫依赖常驻 Engine；
11. Worker 不是隔离进程；
12. Worker/Engine crash 会重复 Provider、Agent 或 Portable 副作用；
13. outcome unknown 被自动 replay；
14. 资源调度只能靠无限并发或固定单并发；
15. Agent 完整能力与安全授权尚未分离；
16. Agent 来源内容可以提升 grant；
17. UI 复制 Core 状态机、解析 human stdout 或建第二 JobStore；
18. personal Draft 必须 Review 后才能用；
19. 知识正文只保存在 SQLite、索引或私有格式；
20. Recipe/Agent/MCP 可绕过 Publisher；
21. 性能 Gate 失败且无 profile；
22. 两个 Agent 同时修改同一 SQLite migration 或热点；
23. 需要破坏性迁移、新用户授权或外部状态变更；
24. 多个高风险 Wave 被合并成一个大 PR。

停止报告必须包含：

- Gate ID；
- 已验证事实；
- 已尝试的非破坏性方案；
- 仍未知的事实；
- 所需用户决策或外部状态；
- 当前工作树安全状态。

---

## 13. 每个 Wave 的交付报告模板

~~~text
Wave:
目标:
状态: PASS / BLOCKED / NOT STARTED

解决的问题 ID:
- ...

假设:
- ...

明确未解决:
- ...

生产代码:
- file: reason

测试代码:
- file: behavior

兼容证据:
- exact command
- pass/fail count

并发与故障证据:
- scenario
- result
- side-effect counters

性能环境:
- CPU/RAM/GPU/disk
- Python/SQLite/Runtime/Agent CLI
- worker/resources

性能结果:
- p50/p95/throughput/RSS/busy/wait reasons

Schema/Protocol:
- old fixture
- migration
- reopen/idempotence
- old binary policy

工作树安全:
- before status
- after status
- unrelated changes preserved

Gate:
- Gx-y PASS/FAIL + evidence

Stop Conditions:
- none / triggered

下一步:
- first permitted next task
~~~

不得只报告“测试通过”；必须给出精确命令、通过/失败数、版本和已知基线例外。

---

## 14. Start Here

后续 Agent 的第一项允许动作是 Wave 0 的只读资产与测试基线，不是直接编辑 ProduceService 或 SQLite。

清单：

1. 阅读第 2.1 节全部文件；
2. 确认操作路径，不要混淆 G:/AllToNote 与 G:/AllToNote-video-producer；
3. 保存两个工作树的 HEAD/status/diff；
4. 检查是否有其他写 Agent；
5. 指定集成 owner 和热点 owner；
6. 运行 Wave 0 基线；
7. 核对依赖锁和开发/CI/EXE SQLite；
8. 未获基线集成授权时停止，不擅自提交；
9. 基线成立后只执行第一个未完成 Wave；
10. 先写 failing/characterization test；
11. 最小生产改动；
12. 定向测试、全量测试、git diff --check；
13. 更新权威任务状态和交付报告；
14. 前一 Wave 未通过时不开始下一 Wave。

---

## 15. 文件地图

### 当前实现热点

- backend/app/core/sdk.py
- backend/app/runtime.py
- backend/app/cli/main.py
- backend/app/core/application/job_service.py
- backend/app/core/application/video_service.py
- backend/app/core/ports/jobs.py
- backend/app/core/ports/job_queries.py
- backend/app/adapters/jobs/sqlite_repository.py
- backend/app/gpt/codex_app_server_client.py
- backend/app/adapters/models/codex_app_server_bridge.py

### 兼容与恢复测试

- backend/tests/core/test_video_request_persistence.py
- backend/tests/core/test_job_service.py
- backend/tests/core/test_checkpoint_runner.py
- backend/tests/core/test_execution_safety.py
- backend/tests/contracts/test_cli_envelope_golden.py
- backend/tests/adapters/test_sqlite_job_repository.py
- backend/tests/integration/test_fake_video_producer.py
- backend/tests/integration/test_platform_subtitle_golden_paths.py
- backend/tests/integration/test_video_bundle_assembly.py
- backend/tests/cli/test_produce_video_cli.py
- backend/tests/cli/test_runtime_bootstrap.py
- backend/tests/runtime/
- backend/tests/test_codex_app_server_client.py

### 本轮下位规格

- ../recipe-x0-compatibility-extraction/spec.md
- ../recipe-x0-compatibility-extraction/tasks.md
- ../local-parallel-production/spec.md
- ../local-parallel-production/tasks.md

### 上位文档

- G:/AllToNote/docs/README.md
- G:/AllToNote/docs/tasks/alltonote-master-tasks.md
- G:/AllToNote/docs/tasks/alltonote-design-coverage-matrix.md
- G:/AllToNote/docs/superpowers/specs/2026-07-13-alltonote-knowledge-compiler-architecture-design.md
- G:/AllToNote/docs/superpowers/specs/2026-07-18-alltonote-recipe-extension-contract-design.md
- G:/AllToNote/docs/superpowers/specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
- G:/AllToNote/docs/superpowers/specs/2026-07-18-alltonote-engine-production-mcp-design.md
- G:/AllToNote/docs/superpowers/specs/2026-07-18-alltonote-document-recipe-design.md
- G:/AllToNote/docs/superpowers/specs/2026-07-18-alltonote-codebase-ue5-recipe-design.md

### 外部技术依据

- SQLite WAL：https://www.sqlite.org/wal.html
- SQLite transactions：https://www.sqlite.org/lang_transaction.html
- Python ProcessPoolExecutor：https://docs.python.org/3/library/concurrent.futures.html#processpoolexecutor
- Python multiprocessing：https://docs.python.org/3/library/multiprocessing.html
- Tauri sidecar：https://v2.tauri.app/develop/sidecar/

---

## 16. 最终交接语

不得以“未来可能扩展”为理由提前建平台，也不得以“保持轻量”为理由继续保留单 Workspace 单执行者。

正确收敛顺序：

~~~text
先整理和冻结已验证基线
  -> 保护 Video 行为并建立最小通用入口
    -> 证明客户端与 SQLite 并发基础正确
      -> 让真实第二消费者决定数据面抽象
        -> 验证开放 Artifact 与可选治理闭环
          -> 建按需 Engine、Job-scoped authority 和隔离 Worker
            -> 建资源准入、detach、batch 与高并行
              -> 开放完整 AgentExecutor
                -> 最后让薄 Desktop 呈现同一 Core
~~~

当且仅当第 11 节全部完成且第 8 节 Release Gate 通过，AllToNote 才达到本轮定义的条件式最优框架。
