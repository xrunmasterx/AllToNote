# Feature: Local Parallel Production

- 日期：2026-07-19
- 状态：技术设计保留；2026-07-30 起 deferred，等待 Video 可信复用后的产品 re-admission
- 目标：在保持 CLI-first、无常驻重型服务和可恢复语义的前提下，交付同一 Workspace 多 Job、多 Agent 的高吞吐执行

## 0. 结论

> 2026-07-30 admission 更新：本规格不属于当前 [`Video 三样本 Pilot`](../../video-dogfood-validation/spec.md) 关键路径。首阶段固定单活跃 Job；只有现有单 Job 内部真实 model fan-out 暴露 turn 隔离问题时，才条件式执行 Task 1 的最小 characterization/fix。Task 2 以及多 Job/Engine Tasks 3–9 等待可信复用后出现真实并发/后台瓶颈并重新评审。本更新调整实施顺序，不删除下述技术设计和正确性 Gate。

当前实现的强项是 durable Job、Checkpoint、fencing、crash/reopen 和避免重复昂贵操作；弱项是同一 Workspace 的执行吞吐基本等于 1。

Recipe X0-A 只解决“不同 Recipe 如何进入平台”，不会解决“多少 Job 同时运行”。把 Engine、worker、资源调度和 AgentExecutor 塞进 X0-A，会使兼容接缝重构至少扩大一倍，并让 Video 零语义变化难以验证。

本阶段的产品需求已经触发，但 Engine、worker 和 per-job authority 的生产实现必须等待 Wave 0 文档与基线、Wave 1 X0-A、Wave 2 并发 C0、Wave 3 真实 Document/PPT + X0-B、Wave 4 Artifact/Review/Publisher 全部通过。Task 1 和 Task 2 可按 Wave 2 提前建立只读 characterization，不得据此启用并发或移除现有保护。

本阶段采用：

    一个按需、空闲退出的本地 Engine leader
      -> 多个隔离 worker process
      -> 每个 Job 独立 generation authority 与 fencing
      -> 一个 Job generation 下顺序创建多个 Step Attempt
      -> 最小资源准入
      -> 每个 Job 独立 staging/checkpoint
      -> workspace publish 短暂串行

本阶段不采用：

- 分布式调度；
- Kubernetes、Redis、PostgreSQL 等服务依赖；
- 常驻重型 daemon；
- 通用 DAG 或 no-code 画布；
- 无界并发；
- 把 Agent 降级成单次 Model call。

## 1. 产品目标

### 1.1 用户体验

单个任务：

    alltonote produce video <input> --wait

保持前台直接、轻量、可取消。没有 Engine 时不因一条简单命令强制启动后台服务。

后台或批量任务：

    alltonote produce <input> --recipe <id>@<version> --detach
    alltonote produce --request batch.jsonl --max-parallel auto

按需启动 Engine。CLI 返回 durable Job ID；关闭终端后任务继续。

Desktop：

- 第一次生产时确保 Engine 可用；
- 使用 typed local protocol 观察 Job 和事件；
- 不解析人类 CLI stdout；
- Engine 空闲后退出；
- UI 只展示运行、排队原因、需要输入、失败恢复和结果，不展示 fencing 等内部术语。

### 1.2 “非常多任务”的准确含义

系统应支持很多 queued Job，但 active Job 必须受机器资源和用户配置约束。

目标是：

- 队列可深；
- 轻任务可高并发；
- GPU、Agent、Provider 等稀缺资源按 slot 控制；
- 用户可以提高或覆盖默认容量；
- 调度器不修改 Agent 的推理和工具语义；
- 资源不足时等待并给出原因，而不是把机器拖入 OOM、thrashing 或 Provider ban。

高性能的指标是有效吞吐、完成时间和恢复成本，不是同时创建的进程数量。

## 2. 当前实现差距

### 2.1 全 Job 串行

backend/app/core/application/video_service.py 的 VideoService 在实例级创建 execution lock，wait_job 在完整执行期间持有它。两个不同 Job 使用同一 Runtime 时也会串行。

### 2.2 单 Workspace scheduler owner 被等同于单执行者

SQLite leases schema 只允许 scheduler 一条记录。另一进程持有时，新 CLI 返回 scheduler_busy。

一个 scheduler leader 本身不是问题；问题是当前 leader 没有多 Job claim 和 worker pool。

正确目标是：

    Workspace / state root
      一个 scheduler leader

    scheduler leader 内
      多个 worker
      每个 Job 独立 JobExecutionAuthority
      每个 authority 下可有多个 Step Attempt

不能简单删除全局 lease 或 execution lock。当前 lease 的 acquire/release 语义并非多 Job 引用计数；直接并行可能使一个 Job release 后另一个 Job 失去 authority。

### 2.3 没有后台领取者

当前 submit 不 wait 时只产生 queued Job；没有 daemon 或 worker loop 自动领取。job wait 才可能在调用进程中成为执行 owner。

因此当前没有真正的 detach、后台持续执行或客户端断开后继续。

### 2.4 资源租约未接入主 Runtime

机器资源 lease 已有局部实现和测试，但生产 Runtime 没有组装它。不同 Workspace 可同时运行，却不会统一协调 GPU、Whisper、Agent CLI、Provider、CPU 或磁盘。

### 2.5 Codex client 并发正确性基线已通过

2026-07-31 已完成 Task 1：每次 turn 使用独立 subprocess、request ID、response buffer、timeout、handle 与线程局部 stderr；v1/v2 ProduceService 的 Job cancellation 均已穿透到 app-server 等待循环，并只终止目标 turn。2/4/8 路受控交错测试与双路真实 Codex app-server 探针均通过。

这只证明 Model client 的可重入基线，不授权增加 Job worker pool，也不解除 SQLite、generation authority、资源租约和 publish 串行化 Gate。

### 2.6 SQLite writer 与长事务

当前 Repository 使用 WAL 和 BEGIN IMMEDIATE。SQLite 支持并发读，但同时只有一个 writer。

portable commit callback 当前位于写事务范围内，可能跨越文件系统或 iwiki 操作。全 Workspace 串行执行掩盖了这个瓶颈；多 worker 后必须测量并限制 workspace publish。

### 2.7 SQLite 版本风险

当前项目虚拟环境：

    sqlite3.sqlite_version = 3.50.4

SQLite 官方在 2026 年披露 WAL-reset bug：多连接、并发 write/checkpoint 的 WAL 场景可能受影响。官方列出的修复版本包括 3.50.7 与 3.51.3。

进入多 writer 负载验证前，必须：

- 升级打包 Runtime 的 SQLite；
- 启动时报告实际版本；
- 对不满足最低版本的高并发模式 fail closed 或降级为单 writer 安全模式；
- 在安装包而不只是开发机中验证。

版本准入采用项目验证白名单而不是简单的 `>=`。因此 3.52/3.53 等后续版本即使从上游继承修复，在开发、CI 与安装包的多连接 WAL Gate 完成前仍保持 fail-closed；通过 Gate 后再显式加入兼容版本线。

## 3. 设计原则

1. 单次前台生产仍可直接执行；
2. detach、批量、Desktop 和已存在 Engine 的调用共享按需 Engine；
3. 一个 Engine leader 可以管理多个 Job，不把 leadership lease 当 Job execution lease；
4. 每个 Job 有独立的 generation authority、heartbeat、expiry、fencing 和 cancellation；其下的 Step Attempt 记录并继承当前 generation；
5. Worker 使用进程隔离，不用共享 Python 对象承担 Agent 隔离；
6. Scheduler 只负责生命周期和资源准入，不理解 Video/PDF/UE5 内部 stage；
7. Recipe 可在 Job 内 fan-out，但必须继承 Job 资源预算；
8. Artifact 正文进入文件/staging，数据库保存 metadata、状态、事件和短事务；
9. 计算并行，workspace publish 和冲突资源短暂串行；
10. 用户授权决定 Agent 能力，资源容量决定同时运行数量；两者不混为一谈；
11. 单个 Worker 崩溃不拖垮 Engine 或其他 Job；
12. 没有 Engine 时现有前台兼容路径仍能工作。

## 4. 目标架构

    CLI / Desktop / Production MCP
      -> ProduceService
        -> durable JobStore
          -> foreground owner（单次 --wait 且无 Engine）
          -> on-demand Engine leader
               -> admission queue
               -> worker process A -> Recipe/Agent -> staging/checkpoint
               -> worker process B -> Recipe/Model -> staging/checkpoint
               -> worker process C -> Recipe/OCR   -> staging/checkpoint
               -> workspace.publish claim -> atomic portable commit

Engine 与 foreground owner 复用同一 Core、Recipe 和 Repository 语义。Engine 不复制第二套 Pipeline。

## 5. 运行模式

### 5.1 Foreground

- 适合一次性任务；
- CLI 持有当前兼容 scheduler authority；
- 进程退出则按现有恢复语义收敛；
- 不自动加载 Engine；
- 若 Engine 已活跃，CLI 提交给 Engine 后等待同一 Job，而不与其争抢 scheduler leadership。

### 5.2 Detach

- Job 先持久化，再返回；
- 原子 ensure 一个 Engine leader；
- Engine 接管 queued Job；
- CLI 退出不影响 Job；
- Engine 空闲 grace 到期退出；
- 不预加载未使用 Recipe、模型或 Agent。

### 5.3 Batch

- 输入为版本化 manifest/JSONL；
- 每项形成独立 Job，失败隔离；
- max-parallel 是调度上限，不覆盖 GPU/provider 等更小资源容量；
- 支持单项 retry/cancel；
- 结果按 Job 独立输出，不建立大而全 batch transaction。

## 6. Job claim 与 fencing

Scheduler leadership 与 Job execution 必须分离。

最小 Job execution authority：

    job_id
    owner_engine_id
    worker_id
    generation
    acquired_at
    heartbeat_at
    expires_at

Step Attempt 是该 authority 下的步骤执行记录，拥有自己的 attempt_id，并记录其继承的 generation；它不是 Job claim 的 identity。

必须支持：

- claim queued/recoverable Job；
- generation 单调递增；
- release 只让目标 claim 过期并保留 generation；
- takeover 必须严格增加 generation；
- heartbeat；
- request_cancel；
- finish/fail；
- stale claim takeover；
- stale generation 不能 start Step Attempt，也不能写 external operation、checkpoint、Artifact、result 或 terminal state；
- Engine 重启 reconcile；
- 单个 Job release 不影响其他正在执行的 Job；
- cancel vs commit 有确定的线性化点。

SQLite schema migration 应在本阶段明确版本化和 dual-open 策略，不夹带进 Recipe X0-A。

## 7. 最小资源模型

第一版只实现真实互斥或容量资源：

| 资源 | 建议语义 |
|---|---|
| agent:<profile> | 可配置 slot；完整 Agent subprocess |
| provider:<profile> | 并发和速率容量 |
| gpu:<device> | shared units 或 exclusive |
| cpu:ffmpeg / cpu:ocr | 有界 slot |
| memory | 估计值与 admission watermark |
| workspace.publish:<id> | 独占、短持有 |
| repo.write:<worktree> | 独占；每 Agent 优先独立 worktree/staging |

不在第一版实现：

- 通用资源 DSL；
- 分布式公平调度；
- 自动最优解求解器；
- 任意 stage DAG；
- 按 prompt 内容猜测资源。

Recipe 提供粗粒度 claim；Worker 运行时可报告实际使用。容量由默认探测、用户配置和命令覆盖共同决定。

## 8. AgentExecutor

ModelExecutor 继续用于 Video/PPT 等结构化模型调用；UE5/Codebase 使用独立 AgentExecutor。

建议的授权模式：

| 模式 | 场景 | 能力 |
|---|---|---|
| safe | Video、PDF、网页等不可信来源 | 来源只读，写 staging，有限工具 |
| project | 用户授权的 repo/worktree | 允许构建、索引、生成中间产物和项目工具 |
| native-trusted | 用户明确选择的本地 Agent CLI | 保留该 CLI 的正常能力；平台只监督进程、预算、目录与提交 |

无论哪种模式：

- 每个 Agent Attempt 使用独立 subprocess；
- 记录版本、配置、授权和 execution receipt；
- 支持取消、timeout、heartbeat、事件和 process tree 回收；
- 不直接并发写正式知识库；
- 正式 publish 仍经过 workspace.publish 与 Publisher 规则；
- 来源内容不能静默提升自己的授权。

## 9. SQLite 与提交策略

暂不换数据库。

优化顺序：

1. 升级到修复 WAL bug 的 SQLite；
2. 一个 Engine leader；
3. 多 Worker 并发计算；
4. per-job claim；
5. 缩短 metadata writer transaction；
6. workspace publish 独占；
7. 4、8、16 Worker 压测；
8. 只有证据表明 writer 是主要瓶颈时，再评估事务拆分或更重存储。

必须保留原子性：

- Attempt authority；
- Source identity CAS；
- portable commit receipt；
- result record；
- Job terminal transition。

如果当前 portable callback 不能从 writer transaction 中移出，第一版允许将 workspace.publish 完整串行，但必须测量其持锁时间和对其他 metadata write 的影响。不能为了吞吐拆成两个可能产生半提交的事务。

## 10. 性能与健壮性 Gate

### 10.1 参考环境

每次基准记录：

- CPU、逻辑核；
- RAM；
- GPU；
- 磁盘；
- Python、SQLite、Runtime、Agent CLI 版本；
- Provider profile；
- Worker 和资源容量配置。

### 10.2 受控负载

必须覆盖：

- 同 Workspace 32 个轻量 fake Job 的并发 submit；
- worker 配置 1、4、8、16；
- 至少 4 个真实 Video/Document-like Job；
- Video + Fake Agent 混合；
- 多 Workspace；
- Provider slot、GPU exclusive 和 workspace.publish；
- Job 内 fan-out 与 Job 间并发同时存在。

目标：

- durable submit 在昂贵执行前返回；
- 32 个 Job 身份唯一、无丢失、无重复领取；
- active count 永不超过配置和资源容量；
- 受控非 I/O fixture 在 4 worker 时相对 1 worker 至少达到 2.4 倍吞吐；
- Engine 冷启动 p95 小于 2 秒，不加载重 Pack；
- idle Engine 内存目标小于 100 MB；
- 增加并发后不重复 model、download、Agent 或 portable commit；
- 单 Worker 崩溃不影响其他 Job；
- stale fencing、cancel vs commit、Engine crash/restart 均得到确定结果；
- SQLite busy、WAL checkpoint 和 commit writer-lock p95 被记录并设回归阈值。

真实 Agent/Provider 工作负载不以固定倍数验收，因为外部限流和模型时延不可控；以成功率、资源等待原因、吞吐和无重复副作用验收。

## 11. 非目标

- 多机、远端或云调度；
- HA Engine；
- LAN/Internet API；
- 分布式数据库；
- 任意第三方代码自动下载；
- 无限 Agent 并发；
- Agent IDE；
- 通用 workflow/no-code 平台；
- 在本阶段重写每个 Recipe 内部并行策略；
- 为追求并发破坏 source/portable commit 原子性。

## 12. 工作量

这是高复杂度运行时重构，不是 Recipe X0-A 的附带优化。

粗略量级：

- 8 至 10 个实施任务；
- 约 20 至 35 个生产/测试文件；
- 约 2,500 至 5,000 行总变更，含 migration、worker protocol、故障测试与 CLI；
- 单工程师约 3 至 6 周，取决于 Windows process supervision、SQLite migration 和 AgentExecutor 的既有可复用程度。

应先完成并发正确性 C0，再逐层开启能力；不能一次删除锁后用压力测试碰运气。

## 13. 完成定义

1. 单次 foreground 不依赖 Engine；
2. detach/batch/Desktop 自动确保按需 Engine；
3. 同一 Workspace 一个 scheduler leader 可并发管理多个 Job；
4. 每个 Job 有独立 claim、Attempt 和 fencing；
5. Worker 为隔离进程，崩溃不拖垮其他任务；
6. 资源容量可配置，排队原因可观察；
7. Agent 能力由显式授权决定，不被固定为单次 Model call；
8. workspace publish 与冲突写入受控；
9. CLI 关闭后 detached Job 继续；
10. Engine 空闲退出；
11. SQLite 使用官方修复版本并通过多连接 WAL 测试；
12. 4、8、16 worker 负载、故障注入和 zero-replay Gate 通过；
13. Desktop 与 CLI 观察同一 Job/事件真相；
14. 未使用并发能力时，AllToNote 仍保持轻量。

## 14. 参考

- https://www.sqlite.org/wal.html
- https://www.sqlite.org/lang_transaction.html
- https://docs.python.org/3/library/concurrent.futures.html
- https://docs.python.org/3/library/multiprocessing.html
- https://v2.tauri.app/develop/sidecar/
- G:/AllToNote/docs/superpowers/specs/2026-07-18-alltonote-engine-production-mcp-design.md
- G:/AllToNote/docs/superpowers/specs/2026-07-13-alltonote-knowledge-compiler-architecture-design.md
- backend/app/core/application/video_service.py
- backend/app/adapters/jobs/sqlite_repository.py
- backend/app/gpt/codex_app_server_client.py
