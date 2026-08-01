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
- [x] legacy JobStore 可无损打开。

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
- 已新增离线 Windows directory Runtime assembler 与固定输入 lock；由工具组装的 `runtime-portable-sqlite-v5` 候选从自身通过 version/info/doctor、中文空格路径 Workspace 初始化和完整 WAL Gate，并把实际加载的 `python314.dll`、`_sqlite3.pyd`、`sqlite3.dll` 约束在 artifact root 内；694 个 payload 文件由相对路径、byte length、SHA-256 清单覆盖，且无 worktree/input/builder 绝对路径、`direct_url.json` 或生成式 pip launcher；
- Task 2 仍待：clean non-admin VM/Defender/中文用户 Gate、稳定 launcher/installer、签名/SBOM/license，以及从同一正式 artifact 完成 Video/Document E2E；在这些结束前不打开并行 Job。
- 2026-08-01 的 `runtime-portable-sqlite-v9` 已把最新的 JobStore/MachineLease WAL+FULL fail-closed、`machine_lease_store_busy` 可重试分类和 `parallel_job_execution_enabled=false` 明确报告带入同一 Windows 候选；候选自身及发布后复跑均通过 1/4/8/16 connection WAL Gate，且 v1 JobStore 在候选解释器中迁移、重开、结果与子记录保持一致。公开发行和真正多 Job 执行仍未因此解锁。

## Task 3: [x] 实现按需 Engine 单实例生命周期

- 风险：高
- 目标：提供轻量、空闲退出的本地 scheduler leader，不复制 Recipe 业务。
- 主要范围：
  - Engine launcher/handshake/liveness
  - state-root scoped single-instance identity
  - Runtime client adapter
  - lifecycle tests

验收：

- [x] foreground 单次 wait 在无 Engine 时仍可直接执行；
- [x] detach、batch 或 Desktop 原子 ensure Engine；
- [x] 并发 ensure 最终只有一个 leader；
- [x] 已有 Engine 时 CLI 提交并观察同一 Job，不抢 leadership；
- [x] Job 在返回前 durable；
- [x] Engine 不 eager load Recipe、Whisper、Torch、Agent 或模型；
- [x] Engine 冷启动 p95 小于 2 秒；
- [x] idle 内存目标小于 100 MB；
- [x] idle grace 后退出；
- [x] Engine 被杀后可重新 ensure 和 reconcile；
- [x] 不监听 LAN/Internet。

Stop condition：

- Engine 需要成为所有 CLI 命令的强制前置；
- 出现第二套 ProduceService、JobStore 或 Recipe Runner；
- 单实例只能靠不可靠的进程名扫描实现。

完成记录（2026-08-01）：

- 已交付显式 `engine start|ensure|status|stop` 生命周期控制面；按用户身份、canonical state-root 与 Runtime major 派生 scope，使用稳定 launch/lifetime lock、PID 创建身份和仅本机 IPC，不依赖进程名扫描；
- Windows V10 真实候选使用 20 次冷启动、32 路并发 ensure、强杀恢复、idle 退出和最终清理完成 Gate：冷启动 p95 `559.301 ms`，最大 idle RSS `38,539,264 bytes`，单实例、父进程退出后重连、强杀后新 identity 与 descriptor 清理均通过；
- 候选绑定源码提交 `54019ea58a280dea6b508044fc0dbe0558684203`，完整证据见 [`Runtime Windows V10 Engine 生命周期验收`](../../../acceptance/2026-08-01-runtime-v10-engine-lifecycle.md)；
- 后续实现已接通 `produce video --detach` 与 Document `produce --detach`：提交先持久化同一 Job，再原子 ensure Engine；已有 Engine 只接收持久 Job 通知，不复制 ProduceService；
- Video 与 Document 的跨进程 E2E 均证明调用者退出后独立 Worker 可以恢复同一 Job、完成 commit，并由新 CLI 进程继续 `job wait`；Engine 启动时扫描并 reconcile 遗留的 Engine-owned queued/running Job；
- Task 3 的完成只代表按需单实例生命周期和 durable handoff 已闭环。dispatcher 仍保持单 active Worker，`runtime info` 仍明确报告 `parallel_job_execution_enabled=false`；batch、Desktop、容量并发和正式发布证据分别属于 Task 5–7 与 Release Gate。

## Task 4: [x] 分离 scheduler leadership 与 Job-scoped generation authority

- 风险：极高
- 目标：一个 leader 可以安全管理多个并行 Job，每个 Job 有独立 authority。
- 主要范围：
  - JobStore schema migration
  - Job-scoped generation authority / Step Attempt / fencing
  - heartbeat/reconcile
  - compatibility dual-open tests

验收：

- [x] migration 有明确 schema version、事务和回滚策略；
- [x] 旧 schema v1 JobStore 可升级且历史 Job 可查询/恢复；
- [x] scheduler leadership 与 Job execution authority 分表或分语义；
- [x] 每个 authority 包含 job、owner/worker、generation、heartbeat、expiry；
- [x] generation 单调递增；release 不重置 generation；takeover 严格递增；
- [x] 一个 Job generation 下可以顺序创建多个 Step Attempt，每个 Attempt 记录继承的 generation；
- [x] 一个 Job release 不影响其他 Job；
- [x] stale generation 无法 start Attempt，也无法写 external operation、checkpoint、Artifact、result 或 terminal state；
- [x] cancel、finish、fail、takeover 有确定线性化点；
- [x] 同 Job 不能被两个有效 worker 同时执行；
- [x] 32 个并发 claim 无重复领取；
- [x] zero-replay 和 outcome unknown 语义保持。

Stop condition：

- 只能通过删除 fencing 或原子 CAS 获得并发；
- migration 不能安全回滚或无法识别旧 schema；
- 需要改变 legacy Video request/result wire。

完成记录（2026-08-01）：

- JobStore schema v4 将 scheduler leadership 保留在 `leases`，并新增按 `job_id` 唯一的 `job_execution_leases`，持久化 owner、generation、heartbeat 与 expiry；schema v5 不改表字段，只冻结 durable `job.state.v1` 生命周期事件语义，并为全部可写表安装 writer-protocol triggers；
- v1/v2/v3 先在一个 `BEGIN IMMEDIATE` 事务内完成 v4 结构迁移，再与已有 v4 一起升级到 v5；升级会为每个旧 Job 追加一次当前状态快照，使旧终态 Job 的事件流也以终态记录收尾，并在提交前安装数据库级 writer fence，使迁移前已打开的 v4 连接在迁移后无法继续写入。v3 存在未过期旧 lease 时以 retryable `job_store_migration_busy` fail closed 且不改 schema；任何结构、事件回填或 trigger 安装失败都完整回滚；
- 新 claim 的 generation 高于该 Job 已存在 Attempt 的最大 fencing token，避免旧 scheduler token 与新 Job generation 的命名空间碰撞；release 仅将 expiry 置零，takeover 严格递增；
- Video、Document、Checkpoint heartbeat 与 Engine failure convergence 已改用 Job-scoped heartbeat/release；scheduler API 拒绝 Job authority，防止两个 authority 域被误混用；
- 迁移、32 路同 Job 争用、跨进程唯一领取、不同 Job 独立领取、stale generation 全写面 fencing、candidate 按 Job 排除、Video/Document outcome-unknown/recovery 与 Runtime release migration Gate 均有回归测试；
- Task 4 完成不等于已开放并行生产：Engine dispatcher 仍单 active Worker，全局 `produce:heavy:v1` 准入仍串行化重任务，`parallel_job_execution_enabled` 保持 false，直到 Task 5/6 和发布验收完成。

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

容量 1 权限移交进度（2026-08-01）：

- Engine dispatcher 在创建 Worker 前先获取全局 `produce:heavy:v1`，再创建一次性、限时、绑定目标 `ResourceOwner` 的 handoff，并以同一 process-instance owner 预 claim 既有 Job；资源繁忙或 spawn 失败都不会启动未准入 Worker；
- 私有 `EngineWorkerLaunchV1` 通过有界 canonical JSON stdin 绑定 Workspace instance、Job、资源 handoff 与精确 Job generation；Worker 先原子 adopt 资源，再验证同 owner 的 Job claim 必须保持完全相同的 authority，之后才创建 Runtime；
- supervisor 从预 claim 到 Worker 退出持续 heartbeat 精确 Job 与源/已 adopt 资源，轮询 durable cancel；权限丢失或取消宽限到期复用既有 process-tree kill，永久 pre-runtime failure 只使用 dispatcher 持有的 expected authority 收敛；
- Video 与 Document Service 已支持“前台自行准入”或“Engine 已 adopt + expected authority”两个互斥模式；Engine 模式不再二次获取全局资源，checkpoint heartbeat 与最终 stale-safe release 继续复用既有路径；
- 三种跨进程 detach 验收（fixture Video、local Video、Document）均证明提交者退出后独立 Worker 使用新 launch envelope 完成原 Job；完整后端回归为 `2586 passed, 3 skipped, 3 subtests passed`；
- 这只是 Task 5/6 的 capacity-1 正确性地基，不宣称并发 Worker、资源池、排队可观测或公开发布完成。dispatcher 仍只有一个 active Worker，`parallel_job_execution_enabled=false` 保持不变，因此 Task 5 与 Task 6 继续未完成。

Worker 自动恢复预算与 schema v6 硬化进度（2026-08-01）：

- JobStore schema v6 为每个 Job execution lease 持久化 `worker_launch_count`；dispatcher 在 spawn 前记账，同一次 activation 最多自动启动 3 次，Engine 重启不能重置 poison Worker 的隐式预算，显式 retry child 则从 0 开始；
- 普通 Worker runner 异常被限制在单次监督边界，取消继续优先；未知外部结果进入 WAITING_FOR_INPUT。`create_challenge` 与 `pause_for_external_outcome_atomic` 在提交 WAITING 的同一 SQLite 事务中清零预算，关闭 Engine 在状态提交与父进程清理之间退出时遗留旧预算的窗口；
- idle deadline 在 dispatcher 有 work 时不再重复执行 registry + SQLite reconcile；Windows release 的 legacy JobStore migration Gate 也已同步要求 schema v6；
- 独立最终 Gate `task_81a684c5fd74` 为 P0/P1=0；最终完整后端回归为 `2622 passed, 3 skipped`。这仍是 capacity-1 的有界失败收敛，不代表 Task 5 完成，也没有启用 `parallel_job_execution_enabled`。

Workspace portable publish 前置闭环（2026-08-02）：

- Video 与 Document 的 Workspace Runtime 现在按 `workspace_id + canonical physical root` 生成不泄露路径的 `workspace:publish:v1:<sha256>` Machine Resource key；前台执行与 Engine adopted Worker 共用同一 machine lease store，因此同一物理 Workspace 的最终 portable publish 互斥，复制到不同路径的 Vault 不会被误当成同一物理写目标；
- publish claim 只在 `prepare_candidate` 完成后获取，并覆盖 portable `commit_prepared` callback、结果/source binding 的 SQLite commit 或 rollback；Docling、转写、模型调用和 candidate 组装等长计算不持有该 claim；
- Worker 永久失败恢复与 dispatcher 取消收敛在重新 claim 得到不同 generation 时，都会释放该 replacement claim，避免被 fencing 的恢复路径泄漏 300 秒 Job authority；
- Video/Document publish 边界、资源释放、异常释放、publish 争用不终态化和两条 replacement-claim RED 回归均已覆盖；完整后端回归为 `2630 passed, 3 skipped, 3 subtests passed`，独立最终 Gate `task_f9aa97be8d60` 为 P0/P1=0。本增量仍保持单 active Worker 和全局 `produce:heavy:v1`，资源等待事件、capacity > 1 监督与 CLI/Desktop 排队可观测仍属于后续 Task 5/6。

Capacity-2 内部正确性 Gate（2026-08-02）：

- dispatcher 已把 scalar active Worker 改为有界集合；显式 `maximum_active_workers=2` 时由两个独立 supervisor thread 管理既有独立 Worker process，第三个 Job 在容量释放前不会启动，单 Worker runner 异常不会停止同伴，graceful shutdown 等待全部活动 Worker；
- 首个资源模型保留 legacy `produce:heavy:v1` 作为 slot 1，只增加固定的 `produce:heavy:slot-2:v1`；显式容量 2 按固定顺序申请两个槽，约束 machine-wide `active_total <= 2`，同时保证 legacy/foreground holder 与新 Engine 不会形成三个互不相交的重任务。Worker handoff、精确 Job authority、Video/Document adopted-resource 校验与 stale-safe release 保持原合同，没有引入 Recipe 资源策略、资源 DSL、slot registry、DAG 或第二套 scheduler state；
- `scheduler.waiting.v1` / `scheduler.admitted.v1` 通过既有 Job event stream 提供不含路径的资源等待原因；资源不足保持 Job 可恢复且不写 failure，Engine 重启后 waiting 事件去重并可在后续准入时闭合；
- focused Video/Document/Engine 集成回归为 `159 passed`，完整 Engine 为 `116 passed, 1 skipped`，dispatcher 单文件为 `35 passed`，完整 Backend 为 `2640 passed, 3 skipped, 3 warnings, 3 subtests passed`；独立最终 Gate `task_627221a16480` 为 P0/P1=0；验收见 [`Engine capacity-2 internal Gate`](../../../acceptance/2026-08-02-engine-capacity2-internal-gate.md)。
- 该能力仍为显式内部开关，默认容量保持 1，`parallel_job_execution_enabled=false` 不变。真实双 Docling/FFmpeg 的内存与吞吐、双进程故障矩阵、Windows portable/clean-machine Gate 未通过前，不对用户启用 capacity 2，因此 Task 5/6 仍未完成。

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
- [x] workspace.publish:<workspace> exclusive（Video/Document capacity-1 前置闭环）；
- [ ] repo.write:<worktree> exclusive。

验收：

- [ ] active Job 永不超过用户配置和资源容量；
- [ ] 排队原因可在 CLI/Desktop 观察；
- [ ] 用户可覆盖默认并发，但不能绕过真实互斥资源；
- [ ] Job 内 fan-out 继承预算，避免 Job 数乘以内部并发形成无界爆炸；
- [x] 长计算不持有 workspace.publish；
- [x] 最终 source/portable commit 保持原子；
- [x] 资源不足不会被写成 Job failure；
- [x] 无通用资源 DSL、优化求解器或 DAG。

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

- [x] detach 返回前 Job 已 durable；
- [x] 关闭 CLI 后任务继续；
- [ ] batch 每项为独立 Job；
- [ ] max-parallel 只是上限，仍服从资源容量；
- [ ] 单项失败不阻塞其他项；
- [x] Job 可独立 retry/cancel；
- [x] CLI 重连可继续 wait/events；
- [ ] Desktop 与 CLI 观察同一状态和事件；
- [ ] Desktop 不解析人类 CLI stdout；
- [ ] 普通单任务命令不要求理解 Engine；
- [ ] 未启动 Engine 时 job wait 的执行/轮询语义在帮助文本中不再含糊。

Stop condition：

- batch 被实现为一个无法局部恢复的大事务；
- Desktop 建立第二套状态机；
- 为 GUI 方便绕过 ProduceService/JobStore。

当前进展（2026-08-01，Task 7 尚未完成）：

- Video 与 Document 的 `produce --detach` 已复用同一 durable Job/Engine handoff，提交进程退出后 Worker 继续执行；
- Engine-owned Document 在 Job 创建前持久化 machine-state 内容快照；Job 事件只绑定 schema、摘要与字节数，不暴露路径。相同 PDF 的并发提交复用一个快照，retry 继承绑定，旧无绑定 Job 不按摘要路径猜测权威；
- Engine-owned 本地 Video 现已采用同一产品语义：Job 创建前完成内容寻址快照，事件只绑定摘要与字节数，独立 Worker 可在原视频删除后继续执行；retry 显式继承，历史无绑定 Job 不猜测快照。验收见 [`Video detach 本地输入快照`](../../../acceptance/2026-08-01-video-detached-input-snapshot.md)；
- `alltonote job events JOB --jsonl --follow` 已提供 SQLite 权威的单 Job 增量事件流：`after_sequence` 为排他游标，`limit` 为分页上限，积压跨页完整排空；每行立即 flush，Ctrl+C 只终止观察并输出最终协议错误行，不取消 Job；
- Job 状态变化与 `job.state.v1` 在同一事务提交，终态后禁止追加事件；v5 迁移为旧 Job 回填当前状态。Attempt 创建和每次权威状态转换现在同事务追加 `stage.changed.v1`，固定携带 schema、stage、state、attempt_id 和 Job fencing generation；Video 与 Document 共用该路径，不生成伪精确百分比，也不从事件反推 Job 状态。验收见 [`durable stage events`](../../../acceptance/2026-08-01-durable-stage-events.md)；
- `waiting_for_input` 是本次观察调用的成功停止边界，调用方随后使用独立 respond 命令恢复；终态 Attempt 事件始终先于终态 `job.state.v1`，因此 follow 仍可用终态 Job 事件作为稳定收尾；
- 控制面执行 `job retry` 或 `job respond` 后，若目标 Job 为 Engine-owned，会在 durable mutation 成功后复用 `LocalEngineClient.notify_job()` 完成原子 ensure+notify；通知失败保留 queued Job，并返回可见 Job ID 与明确恢复动作。Engine 内部恢复和 foreground Job 不触发这条控制面唤醒；
- 空闲跟随采用 0.1、0.2、0.4、0.8、1.0 秒有界退避，避免固定 50 ms 高频读取；CLI 参数关系在 Workspace/JobStore bootstrap 前校验；
- batch request manifest、`max-parallel`、资源准入队列和 typed Desktop bridge 仍未实现，因此 Task 7 保持未完成，`parallel_job_execution_enabled` 继续为 false。

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
