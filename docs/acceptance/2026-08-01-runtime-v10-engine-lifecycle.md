# Runtime Windows V10 Engine 生命周期验收

日期：2026-08-01

状态：当前 Windows 主机候选通过；本轮只验收按需 Engine 生命周期控制面，不启用并行 Job、detach、scheduler、worker 或 reconcile。

## 1. 本轮目标与边界

本轮解决的是一个更小的问题：AllToNote 是否能够在不加载 Recipe、Whisper、Torch、Agent 或模型的前提下，按需启动一个用户与 state-root 隔离的本地 Engine，保证并发 ensure 收敛为单实例，并在 stop、进程被杀或 idle grace 到期后安全退出。

明确不在本轮范围内：

- Job 提交与执行；
- `produce --detach`、batch、Desktop 接入；
- scheduler leadership、Job-scoped generation authority 与 reconcile；
- worker pool、资源 admission 与真正的多 Job 并行。

`parallel_job_execution_enabled` 继续为 `false`，因此本验收不能解释为 Video/Document 已启用并行生产。

## 2. 固定身份

- Engine 生命周期源码提交：`54019ea58a280dea6b508044fc0dbe0558684203`
- 前置主功能提交：`d516c429865a722e318ef8ad9b353164a9367bb8`
- stop 线性化修复提交：`e1b28ee2f5ed87e645e172a5fa4099c98a75d930`
- 对应 `git archive --format=zip` SHA-256：`3201e9c731d0e1072a989c3e3d3e9f74d9373c05aba3e4c06a7c3abadb88d119`
- 候选目录：`G:\.alltonote-release\runtime-portable-engine-v10`
- Runtime wheel：`alltonote_runtime-0.1.0-py3-none-any.whl`
- Runtime wheel byte length：`551199`
- Runtime wheel SHA-256：`edcc3ecf6ecd44098141be7afb69a08f786ac0f3ebb3542b8d6c63f382011306`
- CPython：3.14.6，Windows x86_64
- SQLite：3.53.4
- SQLite source id：`2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc`
- 候选 payload 清单：704 个文件；`file-manifest.json` 自身不列入 payload，因此磁盘总文件数为 705
- `release/file-manifest.json` SHA-256：`c53f4ea579f631383eabafe6f65e199d3e06e4f67f3b98aeebcf197718b48142`
- `release/wheelhouse-lock.json` SHA-256：`9d27acc856e5e0725ffff749de4c7cd462728c0902abf065ab968de041798e17`
- `release/acceptance.json` SHA-256：`d7721de10362db68dcdbf097b98d35aff11818c0bd9402da1fb67dca823ed712`

## 3. 真实候选 Engine Gate

组装工具从上述精确 wheel 创建候选，并由候选自己的 CPython 3.14.6 执行全部 Gate。结果：

- 20 次冷启动全部成功；原始样本（毫秒）：`541.440, 559.301, 527.599, 548.407, 527.895, 546.322, 548.885, 547.772, 539.707, 484.542, 529.980, 589.127, 515.766, 490.225, 494.045, 471.759, 525.635, 500.878, 553.261, 541.701`；
- 冷启动 p95：`559.301 ms`，低于 `2000 ms` Gate；
- 最大 idle RSS：`38,539,264 bytes`，低于 `100 MiB` Gate；
- 32 路并发 `engine ensure` 收敛为同一个 `engine_id`，且恰好一个调用报告 `started=true`；
- CLI 父进程退出后可重新连接同一 Engine；
- 强制终止 Engine 后，下一次 ensure 创建新的 `engine_id`；
- `idle_seconds=0.25` 的候选内探针正常退出，descriptor 不残留；
- 最终 stop 成功，Gate 后无 Engine 进程与 descriptor 残留；
- 同一候选继续通过 version、runtime info、runtime doctor、中文空格路径 Workspace init、SQLite WAL Gate 与 legacy JobStore migration。

`runtime info --json` 同时报告：

- `engine.supported=true`
- `engine.running=false`（最终 stop 后）
- `engine.state=stopped`
- `storage.parallel_job_execution_supported=true`
- `storage.parallel_job_execution_enabled=false`

## 4. Gate 发现并修复的问题

真实打包 Gate 不是形式验证；它在候选发布前发现了两个 Windows shutdown 竞态：

1. `stop` 曾在进程创建身份已经不可查询、但 lifetime lock 尚未释放时过早返回。现在完成点要求“旧进程身份消失且 lifetime lock 已释放”，并在返回前清理 stale descriptor。
2. named-pipe listener 的 `accept` 线程曾在 idle shutdown 时保持阻塞。主线程 timed join 后退出，会在 CPython 3.14 触发 semaphore fatal error。现在 host 在关闭前通过同一受认证本地 pipe 自唤醒 listener，再完成 join 和资源释放。

两项修复均保持 launch/lifetime lock、PID 创建身份、用户 scope 和本地 IPC 安全边界；没有通过放宽超时绕过失败。

## 5. 自动化与独立复核

- 最终全量后端回归：`2358 passed, 3 skipped, 3 subtests passed`；
- Engine/CLI/release 聚焦回归：`42 passed, 1 skipped`；
- Runtime Windows release/lock 测试：`21 passed`；
- 独立代码 Gate Review：P0=0、P1=0、PASS；
- 候选发布后，在独立 CLI 会话中再次执行 `engine status -> engine ensure -> runtime info -> engine stop -> engine status`：状态依次为 stopped、running、running、stopped、stopped；
- 独立复核结束后，没有发现存活的 `app.engine` 进程。

## 6. 尚未解除的边界

V10 仍是 unsigned portable directory candidate，而不是公开发行认证。以下工作仍未完成：

1. clean Windows 非管理员用户/VM、中文空格用户目录、Defender 开启、无源码 checkout 与无开发 Python 的复验；
2. installer/discovery/PATH、Authenticode、完整 Runtime SBOM/license、update/rollback/uninstall；
3. detach/batch/Desktop 到 Engine 的 Job 提交协议；
4. Job durable-before-return、scheduler leadership、Job-scoped generation authority、heartbeat 与 reconcile；
5. 隔离 worker、资源 admission 与真正的多 Job 执行；
6. IPC 小消息发送路径的独立 write deadline 仍是 P2 后续硬化项；当前 connect/auth/read 已有 deadline。

因此 Task 3 仍保持未完成。下一步应先设计并验证 durable Job submit + detach/reconcile 接缝，不能因为生命周期 Gate 通过就删除现有串行保护或把 `parallel_job_execution_enabled` 改为 `true`。
