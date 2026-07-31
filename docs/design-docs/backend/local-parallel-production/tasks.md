# Local Parallel Production 实施任务

> 来源：本目录 spec.md
> 任务数：9
> 前置：2026-07-30 起本阶段 deferred；当前 Video 三样本 Pilot 固定单活跃 Job。只有单 Job 内部真实 model fan-out 暴露隔离问题时才条件式执行 Task 1；Task 2–9 等待可信复用后的产品与技术 re-admission
> 原则：先证明正确，再增加并发；一次只开启一个能力层

当前阶段入口：[`Video 三样本 Pilot`](../../video-dogfood-validation/spec.md)。本任务清单保留为后续技术规格，不得把“历史需求已触发”解释为当前自动开工授权。

## Task 1: [x] 修复模型客户端并发正确性基线

- 风险：高
- 目标：在增加 Job 并发前，证明同一 Runtime 内多个模型 turn 不会串号、串日志或死等。
- 主要文件：
  - backend/app/gpt/codex_app_server_client.py
  - backend/app/adapters/models/codex_app_server_bridge.py
  - 对应 client/bridge tests

验收：

- [x] 先写并发 turn 的失败测试，再实现修复；
- [x] request ID 为单次 turn 局部状态，不通过共享 next_id - 1 推断；
- [x] stderr、response buffer、timeout 与 subprocess handle 每 turn 隔离；
- [x] 单 turn 兼容行为、错误映射和 receipt 不变；
- [x] 2、4、8 个受控并发 turn 无串号、死锁或状态污染；
- [x] cancel/timeout 只终止目标 turn；
- [x] 不在本 Task 增加 Job worker pool。

完成证据（2026-07-31）：

- 自动化测试先失败后修复；共享 client 的 2/4/8 路 JSON-RPC 交错、per-turn stderr、目标 turn cancel 与 timeout 均通过；
- v1/v2 ProduceService 的 `CancellationTokenPort` 均通过内部 legacy bridge 传入 app-server 等待循环，50ms 有界轮询后只清理目标 subprocess；Core `ModelExecutor` 公共契约未改变；
- 使用真实 Codex CLI 0.146.0 / `gpt-5.6-sol` 同时运行两路固定标记探针，两路均返回各自标记，stderr 均为空；
- 全量后端回归：`2105 passed, 2 skipped, 3 subtests passed`。

Stop condition：

- 修复需要改变 ModelExecutor 公共语义；
- 真实 Codex protocol 无法区分 per-process request identity；
- 需要先升级外部 Agent CLI 才能可靠验证。

## Task 2: [ ] 升级并验证 SQLite WAL 并发基础

- 风险：高
- 目标：确保开发与打包 Runtime 使用官方修复 WAL-reset bug 的 SQLite，并建立 writer 基线。
- 主要文件：
  - Runtime/打包依赖与版本报告
  - SQLite Repository tests
  - 新增并发 benchmark/fault fixture

验收：

- [ ] 开发、CI 和安装包报告实际 SQLite 版本；
- [ ] 最低版本为 3.50.7、3.51.3、3.53.4 或项目实测的同版本线后续修复版本；
- [ ] 不满足最低版本时高并发模式 fail closed 或明确降级；
- [x] 记录 1、4、8、16 connection 的短写、读写、checkpoint 和 busy 延迟；
- [x] 记录 portable commit writer-lock 持有时间；
- [x] busy/locked 被映射为稳定、可诊断的运行时错误或重试策略；
- [ ] 不因预期未来压力迁移到 PostgreSQL/Redis；
- [ ] legacy JobStore 可无损打开。

Stop condition：

- 只能通过未维护的 SQLite fork 修复；
- 打包环境无法确定实际 SQLite 动态库；
- schema/transaction 变更会破坏现有 atomic commit。

当前进展（2026-07-31，Task 2 尚未完成）：

- `runtime info` 已报告当前进程实际加载的 SQLite 版本、source id、compile options、DB-API threadsafety 与 `parallel_job_execution_supported`；这些是运行时诊断，不是安装包二进制来源证明；
- `runtime doctor` 对未进入项目验证白名单的版本给出结构化 warn，现有单 Job/单 writer 保护保持不变；
- 当前白名单仅含已知修复并按本项目版本线确认的 3.44.6+、3.50.7+、3.51.3+、3.53.4+；仍不按版本大小自动放行未知的新版本线；
- JobStore 已按 SQLite primary result code 将 `SQLITE_BUSY`/`SQLITE_LOCKED`（含 extended code）映射为脱敏、可重试的 `job_store_busy`，Video/Document 不再把该临时争用永久写成失败；没有加入事务自动重放，也没有解除现有串行保护；
- 已增加显式 `runtime sqlite-wal-gate`：使用隔离临时 JobStore、spawn 子进程与 1/4/8/16 connection 覆盖短写、混合读写、在线 PASSIVE checkpoint、forced busy 后显式重试、portable commit writer-lock、进程崩溃/重开、最终 TRUNCATE/integrity/foreign-key 检查；子进程 SQLite version/source id 必须与父进程一致；
- Gate 对未进入项目白名单的 SQLite 在创建临时状态和 spawn 前 fail closed。2026-07-31 使用 SHA-256 为 `df901e84a896ff1ee720ad03377e0c8d8c2244fda79808aeeaff6316df1cb75c` 的官方 CPython 3.14.6 embeddable x64 与 SHA3-256 为 `deddee963c810d1eeac3ce5e15c7c41da21a1c54d7a39cf54fbf577d2f50de3a` 的官方 SQLite 3.53.4 x64 DLL 完成 ABI 探针；实际加载 source id 为 `2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc`；
- 同一 SQLite 3.53.4 二进制随后通过完整 1/4/8/16 connection WAL Gate：短写、混合读写、在线 PASSIVE checkpoint、forced busy 后调用方显式重试、portable commit writer-lock、未提交/已确认崩溃恢复、最终 TRUNCATE、integrity、foreign-key 与 schema 版本检查均通过；Gate 仍明确报告 `parallel_job_execution_enabled=false`，尚未打开服务并行；
- Task 2 仍待：升级并锁定开发、CI 与正式安装包的 SQLite，使用最终安装包解释器运行完整 Gate，绑定安装包内实际 SQLite 二进制清单/哈希，并补齐 legacy JobStore 的安装包回归证据。

## Task 3: [ ] 实现按需 Engine 单实例生命周期

- 风险：高
- 目标：提供轻量、空闲退出的本地 scheduler leader，不复制 Recipe 业务。
- 主要范围：
  - Engine launcher/handshake/liveness
  - state-root scoped single-instance identity
  - Runtime client adapter
  - lifecycle tests

验收：

- [ ] foreground 单次 wait 在无 Engine 时仍可直接执行；
- [ ] detach、batch 或 Desktop 原子 ensure Engine；
- [ ] 并发 ensure 最终只有一个 leader；
- [ ] 已有 Engine 时 CLI 提交并观察同一 Job，不抢 leadership；
- [ ] Job 在返回前 durable；
- [ ] Engine 不 eager load Recipe、Whisper、Torch、Agent 或模型；
- [ ] Engine 冷启动 p95 小于 2 秒；
- [ ] idle 内存目标小于 100 MB；
- [ ] idle grace 后退出；
- [ ] Engine 被杀后可重新 ensure 和 reconcile；
- [ ] 不监听 LAN/Internet。

Stop condition：

- Engine 需要成为所有 CLI 命令的强制前置；
- 出现第二套 ProduceService、JobStore 或 Recipe Runner；
- 单实例只能靠不可靠的进程名扫描实现。

## Task 4: [ ] 分离 scheduler leadership 与 Job-scoped generation authority

- 风险：极高
- 目标：一个 leader 可以安全管理多个并行 Job，每个 Job 有独立 authority。
- 主要范围：
  - JobStore schema migration
  - Job-scoped generation authority / Step Attempt / fencing
  - heartbeat/reconcile
  - compatibility dual-open tests

验收：

- [ ] migration 有明确 schema version、事务和回滚策略；
- [ ] 旧 schema v1 JobStore 可升级且历史 Job 可查询/恢复；
- [ ] scheduler leadership 与 Job execution authority 分表或分语义；
- [ ] 每个 authority 包含 job、owner/worker、generation、heartbeat、expiry；
- [ ] generation 单调递增；release 不重置 generation；takeover 严格递增；
- [ ] 一个 Job generation 下可以顺序创建多个 Step Attempt，每个 Attempt 记录继承的 generation；
- [ ] 一个 Job release 不影响其他 Job；
- [ ] stale generation 无法 start Attempt，也无法写 external operation、checkpoint、Artifact、result 或 terminal state；
- [ ] cancel、finish、fail、takeover 有确定线性化点；
- [ ] 同 Job 不能被两个有效 worker 同时执行；
- [ ] 32 个并发 claim 无重复领取；
- [ ] zero-replay 和 outcome unknown 语义保持。

Stop condition：

- 只能通过删除 fencing 或原子 CAS 获得并发；
- migration 不能安全回滚或无法识别旧 schema；
- 需要改变 legacy Video request/result wire。

## Task 5: [ ] 实现隔离 Worker 与进程监督

- 风险：极高
- 目标：Engine 可以并发启动多个 Recipe/Agent worker，单 Worker 故障不拖垮其他 Job。
- 主要范围：
  - worker protocol
  - subprocess/process-tree supervision
  - per-job staging
  - heartbeat/event/cancel

验收：

- [ ] Worker 为独立进程，不以共享 Python mutable state 作为隔离；
- [ ] protocol 固定既有 Job/Recipe/Runtime identity 和当前 Job generation；Worker 不调用 RecipeEndpoint.submit 创建第二个 Job；
- [ ] Step Attempt 在执行期间创建并继承当前 generation；
- [ ] 每个 Worker 只写自己的 staging/checkpoint；
- [ ] stdout/stderr/event 有边界和脱敏；
- [ ] graceful cancel、timeout、terminate 和 kill process tree 均可验证；
- [ ] Worker 崩溃只终止目标 Attempt；
- [ ] Engine 崩溃后孤儿 Worker 无法迟到提交；
- [ ] frozen EXE/Windows 打包路径通过真实验证；
- [ ] 不使用旧 task_serial_executor 作为 durable scheduler。

Stop condition：

- Worker 必须共享 VideoService singleton；
- 无法在 Windows 回收完整子进程树；
- IPC 损坏会直接写入 Job 成功。

## Task 6: [ ] 加入最小资源准入与 workspace publish 控制

- 风险：高
- 目标：高吞吐而非无界进程爆发。
- 主要范围：
  - admission queue
  - resource capacity/config
  - scheduling events
  - workspace publish claim

首版资源：

- [ ] agent:<profile> slot；
- [ ] provider:<profile> concurrency/rate bucket；
- [ ] gpu:<device> shared/exclusive；
- [ ] cpu:ffmpeg 与 cpu:ocr slot；
- [ ] memory watermark；
- [ ] workspace.publish:<workspace> exclusive；
- [ ] repo.write:<worktree> exclusive。

验收：

- [ ] active Job 永不超过用户配置和资源容量；
- [ ] 排队原因可在 CLI/Desktop 观察；
- [ ] 用户可覆盖默认并发，但不能绕过真实互斥资源；
- [ ] Job 内 fan-out 继承预算，避免 Job 数乘以内部并发形成无界爆炸；
- [ ] 长计算不持有 workspace.publish；
- [ ] 最终 source/portable commit 保持原子；
- [ ] 资源不足不会被写成 Job failure；
- [ ] 无通用资源 DSL、优化求解器或 DAG。

Stop condition：

- Scheduler 必须理解 Video/PDF/UE5 内部 stage；
- 为资源控制修改 Agent 推理或工具语义；
- 为提高吞吐拆开不可恢复的 atomic commit。

## Task 7: [ ] 接入 detach、batch 与统一 Job UX

- 风险：高
- 目标：从 CLI 和薄 Desktop 便捷控制大量任务。
- 主要范围：
  - produce --detach
  - request manifest/JSONL
  - max-parallel
  - job list/wait/cancel/retry/events
  - typed Desktop bridge

验收：

- [ ] detach 返回前 Job 已 durable；
- [ ] 关闭 CLI 后任务继续；
- [ ] batch 每项为独立 Job；
- [ ] max-parallel 只是上限，仍服从资源容量；
- [ ] 单项失败不阻塞其他项；
- [ ] Job 可独立 retry/cancel；
- [ ] CLI 重连可继续 wait/events；
- [ ] Desktop 与 CLI 观察同一状态和事件；
- [ ] Desktop 不解析人类 CLI stdout；
- [ ] 普通单任务命令不要求理解 Engine；
- [ ] 未启动 Engine 时 job wait 的执行/轮询语义在帮助文本中不再含糊。

Stop condition：

- batch 被实现为一个无法局部恢复的大事务；
- Desktop 建立第二套状态机；
- 为 GUI 方便绕过 ProduceService/JobStore。

## Task 8: [ ] 实现完整本地 AgentExecutor

- 风险：极高
- 目标：让 UE5/Codebase 等 Recipe 调度真正的本地 Agent CLI，而不是受限单次 Model call。
- 主要范围：
  - Agent profile/identity
  - safe/project/native-trusted grant
  - subprocess adapter
  - execution receipt
  - staging/worktree policy

验收：

- [ ] ModelExecutor 与 AgentExecutor 职责分离；
- [ ] Agent 支持多轮、工具、项目读取和用户授权的构建/写入；
- [ ] safe、project、native-trusted 的权限差异可解释、可测试；
- [ ] native-trusted 不偷偷阉割现有 Agent CLI 能力；
- [ ] 未授权来源内容不能提升权限；
- [ ] 每个 Agent 有独立 process、staging/worktree 和 receipt；
- [ ] 预算、timeout、cancel、heartbeat 和日志脱敏生效；
- [ ] 多 Agent 可并行，仍服从 agent/resource slot；
- [ ] Agent 不能绕过正式知识 Publisher 直接写 common。

Stop condition：

- 只能通过 approval never + read-only + no-tools 冒充 AgentExecutor；
- 多个 Agent 必须共享同一可写 worktree；
- 来源文本能够修改 grant。

## Task 9: [ ] 完成负载、故障注入与轻量性 Gate

- 风险：极高
- 目标：证明并发提高有效吞吐，同时保持恢复与轻量性。

负载矩阵：

- [ ] 同 Workspace 32 个轻量 Job submit；
- [ ] worker 1、4、8、16；
- [ ] 至少 4 个真实 Video/Document-like Job；
- [ ] Video + Fake/real Agent 混合；
- [ ] 多 Workspace；
- [ ] Job 内 fan-out 与 Job 间并发叠加；
- [ ] GPU、Provider、Agent 和 workspace.publish 资源等待。

故障矩阵：

- [ ] 两 Engine 启动竞争；
- [ ] CLI、Engine、Worker 分别被杀；
- [ ] stale fencing；
- [ ] cancel vs commit；
- [ ] SQLite busy/locked/checkpoint；
- [ ] Agent hang/协议损坏/process tree；
- [ ] provider timeout/outcome unknown；
- [ ] portable callback failure；
- [ ] restart/reconcile；
- [ ] Desktop/CLI 断开重连。

性能 Gate：

- [ ] 4 worker 受控非 I/O fixture 相对 1 worker至少 2.4 倍吞吐；
- [ ] 32 Job 无丢失、重复领取或重复副作用；
- [ ] Engine 冷启动 p95 小于 2 秒；
- [ ] idle 内存目标小于 100 MB；
- [ ] SQLite writer、WAL checkpoint、workspace publish p50/p95 有记录和回归阈值；
- [ ] active count 始终不超过容量；
- [ ] 单任务 foreground 冷路径没有结构性回归；
- [ ] Engine idle 后退出；
- [ ] 全量兼容、zero-replay、Portable/iwiki 和 CLI golden 通过。

## 全局 Stop Conditions

出现以下任一情况，停止扩大并发：

- SQLite 未升级到官方修复版本；
- Codex/Model client 并发正确性未通过；
- per-job fencing 未完成就删除全局串行保护；
- Worker 无法隔离或无法回收进程树；
- 为吞吐牺牲 source/portable commit 原子性；
- 无界启动 Agent、OCR、FFmpeg 或 Provider 调用；
- Engine 成为所有简单 CLI 的强制常驻依赖；
- 引入分布式基础设施但没有本地基准证明必要；
- 单 Worker 故障可拖垮整个 Engine；
- 增加并发后出现重复付费调用或重复发布。
