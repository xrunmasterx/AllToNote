# Durable stage events 验收

日期：2026-08-01
分支：`codex/video-dogfood-validation`
基线：`83bacbe3a51dfdae6fb063932a3563ca01a6763a`

## 结论

Video 与 Document 的权威 Attempt 生命周期现在投影为同一条 SQLite durable 事件流。事件由 `SqliteJobRepository` 在 Attempt 创建或状态转换的同一事务内追加，不由 Checkpoint runner、Recipe、Worker 或 CLI 重复发布。

本增量通过，但不代表 Worker Pool、资源准入、batch、`max-parallel` 或 Production MCP 已完成；`parallel_job_execution_enabled` 继续为 `false`。

## 固定契约

事件类型为 `stage.changed.v1`，payload 只包含：

```json
{
  "attempt_id": "att_...",
  "generation": 1,
  "schema_version": 1,
  "stage": "generate_draft",
  "state": "running"
}
```

- `stage` 来自持久化 Attempt 的 `step_id`；
- `generation` 来自持久化 Attempt 的 fencing token，`0` 明确表示 pending、尚未绑定执行权；
- 不携带路径、正文、Prompt、Provider raw、Artifact 内容或 Secret；
- 不生成没有稳定总量依据的百分比；
- Job、Attempt 和 checkpoint 继续是事实权威，事件不用于重放业务状态；
- `job get` 本轮不新增“当前阶段”字段，消费者使用现有按 sequence 可恢复的事件流；
- succeeded、failed、cancelled 的阶段事件先写，终态 `job.state.v1` 仍是最后一条事件。

## 覆盖的权威变化

- Attempt 创建为 pending；
- pending 启动为 running，包括崩溃后 claim 恢复；
- 正常 succeeded、failed、cancelled、interrupted、needs_input、skipped；
- generation 接管：旧 Attempt interrupted，新 Attempt pending 后 running；
- 外部结果未知时 needs_input；
- pending/running 批量取消，按创建时间和 Attempt ID 稳定排序；
- Video/Recipe portable commit 成功；
- 绑定 Attempt 的原子失败。

被 fencing、非法转换、重复 cancel、只修改 Job 状态、checkpoint 读取和外部操作更新均不会产生虚假阶段事件。通用 `append_event` 不能伪造内部 `stage.changed.v1`。

## 验证结果

- JobStore、Checkpoint、恢复和 CLI focused：`350 passed`；
- Video、Document、Engine-owned local Video 跨进程 detach E2E：`3 passed`；
- 完整 backend：`2530 passed, 3 skipped, 3 subtests passed`；
- 全量测试使用 `PYTHONDONTWRITEBYTECODE=1` 和 `-p no:cacheprovider`，没有创建 pytest cache；
- `git diff --check` 在提交前执行；
- 独立 Gate Review 的任务与结论记录在本次编排上下文中。

## 后续边界

下一步先依据上层 Engine 设计评估 Worker Pool 与资源准入的最小正确切片。未先建立 admission、进程监督、取消/超时和 pack generation pin 之前，不因已有 durable stage events 就打开多 Job 并行。
