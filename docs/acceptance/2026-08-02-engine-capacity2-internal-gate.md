# Engine capacity-2 internal Gate

日期：2026-08-02

## 结论

本增量通过“可配置但默认关闭”的 capacity-2 正确性 Gate：`EngineJobDispatcher` 可以在显式配置 `maximum_active_workers=2` 时同时监督两个独立 Worker，并通过两个固定 Machine Resource slot 把总量第三个 Job 挡在准入边界之外。单个 Worker runner 异常不会停止另一个 Worker；graceful shutdown 会等待两个活动 Worker 都完成。

这不是公开并行发布声明。生产默认值仍为 1，`runtime info` 和 SQLite Gate 仍报告 `parallel_job_execution_enabled=false`。在真实双 Docling/FFmpeg 负载、内存门槛、Windows portable Runtime 与 clean-machine Gate 通过前，不提高 Engine Host 默认容量。

## 冻结的最小合同

- 活动 Worker 数量只来自构造参数，合法范围为 1–2，且不能大于既有 bounded wake queue 容量；
- 容量 1 继续使用兼容既有调用的 `produce:heavy:v1`；显式容量 2 按固定顺序使用该 legacy slot 与新增的 `produce:heavy:slot-2:v1`，合同是 machine-wide `active_total <= 2`。legacy/foreground holder 会占用 slot 1，因此不能与新 Engine 形成三个互不相交的重任务；没有 Recipe 资源策略、通用资源 DSL、优化器、DAG 或可变 slot registry；
- supervisor 仍在启动 Worker 前获取资源 handoff 与精确 Job authority。Video/Document Worker 接受两个固定 heavy slot，并继续校验同一 process-instance owner 与同一 Job generation；
- 每个活动 Job 由独立非 daemon supervisor thread 管理既有独立 Worker process。Worker 的异常、launch budget、cancel watchdog、资源/authority release 与失败收敛保持 Job-scoped；
- 资源不足不会写入 Job failure。Job 保持 `queued` 或可恢复的 `running`，并追加一次路径无关的 `scheduler.waiting.v1`：`reason=resource_capacity`、`resource_class=produce:heavy`；随后成功准入时追加 `scheduler.admitted.v1`；
- 事件仍通过既有 `alltonote job events` / JSONL Automation Protocol 暴露，不新建 scheduler 数据库或第二套状态机。Engine 重启后会根据事件尾部去重 waiting，并在后续准入时闭合为 admitted；
- graceful shutdown 清空尚未启动的内存 wake queue，但不杀死活动 Worker；force close 复用既有 stop/check/kill-tree 边界并等待所有 supervisor thread 回收。

## 验证证据

- RED 先证明旧 scalar `_active`、单全局 lease 与同步 runner 无法同时启动两个 Worker；
- capacity-2 测试同时启动一个 Video 与一个 Document Job，观察到两个不同固定 heavy slot；总量第三个 Job 在任一 Worker 释放前不会启动；
- legacy/foreground holder 占用 slot 1 时，capacity-2 Engine 只能启动一个 slot-2 Worker，证明跨版本/跨入口 machine-wide 总量仍受 2 的上界约束；
- 单 Worker runner 抛出 `OSError` 时，另一个 Job 仍独立完成；
- 双 Worker graceful shutdown 在第一个 Worker 完成后仍不可关闭，第二个完成后才进入 `ready_to_close`；
- 两个 slot 均被占用时不启动 Worker、不终态化 Job，只持久化一个 waiting；释放资源并重建 dispatcher 后，Job 被准入并追加唯一 admitted；
- slot 2 的 handoff 已通过 Engine Worker、Video Runtime 与 Document Runtime 的 adopted-resource 路径；
- focused Video/Document/Engine 集成回归：159 passed；
- 完整 Engine 回归：116 passed, 1 skipped；dispatcher 单文件 35 passed；
- 完整 Backend 回归：2640 passed, 3 skipped, 3 warnings, 3 subtests passed；3 条 warning 均为既有 downloader/第三方依赖 warning；
- 独立最终 Gate `task_627221a16480` 结论为 P0/P1=0。

## 尚未解锁

- Engine Host 默认容量仍为 1；CLI/Desktop 没有用户并发配置或 queue position；
- 首版只提供两个通用 heavy slot；还没有基于实测资源包络区分 Video、Document、FFmpeg、OCR、GPU、Provider，也没有 Job 内 fan-out 预算；
- Docling 已测峰值工作集约 1.47 GB，因此真实双 Document 并发必须先有内存/吞吐记录和低内存 fail-closed 或降级策略；
- 尚未完成两个真实独立 Worker process 的同时 Video/Document portable publish、Engine crash/orphan、cancel-vs-commit 与 Windows process-tree 故障矩阵；
- Windows portable Runtime、签名 Runtime/Pack、clean non-admin/Defender/Unicode 路径和正式发布复跑仍是 Release Gate。

下一步应使用该显式 capacity-2 开关完成受控双进程负载与故障 Gate；只有证据表明吞吐提高且内存、SQLite、publish、取消和恢复都保持边界，才把 Engine Host 默认容量和 `parallel_job_execution_enabled` 一起切换，不能只改诊断布尔值。
