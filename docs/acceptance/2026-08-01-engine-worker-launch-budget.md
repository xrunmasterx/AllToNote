# Engine Worker 自动恢复预算验收

日期：2026-08-01

范围：capacity-1 Engine 的 Worker 启动失败收敛、跨 Engine 重启的持久预算、WAITING/取消优先级、idle 重复 reconcile 抑制，以及 JobStore schema v6 发布迁移 Gate。

## 结论

本增量通过。Engine 对同一次 Job activation 最多自动启动 Worker 3 次；启动次数在 spawn 前写入 JobStore，因此 Engine 或 dispatcher 重启不会重新获得一套隐式预算。普通 Worker runner 异常不再杀死 dispatcher 线程，连续 poison failure 会在有界次数内进入确定的 FAILED 或 WAITING_FOR_INPUT，而不是形成无限重启循环。

该结论仍只适用于 capacity-1。dispatcher 仍只有一个 active Worker，全局重任务仍由 `produce:heavy:v1` 串行化，`parallel_job_execution_enabled=false`。本记录不是并发 Worker、资源池、batch、正式 Windows 发行或完整 Task 5/6 验收。

## 已冻结的行为

- JobStore schema v6 在既有 `job_execution_leases` 行中增加非负 `worker_launch_count`；v1–v5 均事务迁移到 v6，v5 writer-protocol triggers 继续作为冻结的旧协议边界。
- dispatcher 每次自动 spawn 前先持久增加启动次数；第 3 次之后不再自动启动第 4 个 Worker，Engine 重启后仍读取同一 durable count。
- Worker 正常终态、稳定 WAITING_FOR_INPUT 和显式 retry child 会进入新的恢复边界：终态/等待清理旧预算，用户显式 retry 创建的新 Job 从 0 开始。
- `create_challenge` 与 `pause_for_external_outcome_atomic` 在把 Job 提交为 WAITING_FOR_INPUT 的同一个 SQLite 事务中把 `worker_launch_count` 清零。不存在“WAITING 已持久化、预算稍后由 dispatcher 清理”的崩溃窗口。
- 最后一次自动启动期间若用户已取消，取消在原子失败提交内优先；存在未知外部副作用时不伪造失败，而是进入 WAITING_FOR_INPUT 交给用户确认。
- 普通 `Exception` 被限制在单次 Worker 监督边界内；controlled stop、取消与 authority fencing 仍使用原有显式路径。
- Engine idle deadline 只有在 dispatcher 没有待处理/active work 时才做 registry + SQLite reconcile，并在取得 launch lock 后再次检查，避免名义 20 Hz 下重复扫描活跃 JobStore。
- Windows release 的 legacy JobStore migration Gate 与测试已同步要求 `schema_after=6`，不会把 schema v6 候选按旧 v5 合同误判。

## Gate 发现与修复

第一轮独立 Gate `task_9bc7b8474361` 发现一个 P1：Worker 已在 SQLite 中提交 WAITING_FOR_INPUT 后才由父 dispatcher 清理启动预算；若 Engine 在两者之间退出，用户响应后会继承旧预算。修复方式不是增加恢复分支，而是把 WAITING 状态与预算清零放进同一个 SQLite 事务。

后续只读复审 `task_991f899fc04f` 又识别出 Windows release migration Gate 仍硬编码 `schema_after=5`。发布工具和精确测试已同步到 v6，聚焦测试通过。最终只读 Gate `task_81a684c5fd74` 对 10 个改动文件复核后结论为 PASS，P0/P1 均为 0。

## 验证结果

修复 WAITING 原子性的两个定向测试：

```text
2 passed
```

SQLite JobStore 完整测试：

```text
266 passed
```

Engine dispatcher、lifecycle 与 Worker 测试：

```text
64 passed
```

Windows release schema Gate 定向测试：

```text
1 passed
```

最终完整后端回归（禁用 bytecode 与 pytest cache）：

```text
2622 passed, 3 skipped, 3 warnings in 158.63s
```

开发中第一轮全量曾出现一次不可复现的 shutdown timeout；该用例单独复跑通过，随后修复前的第二轮全量与本次修复后的最终全量均通过。因此它被如实记录为瞬态测试现象，而不是通过放宽超时或删除断言掩盖。

`git diff --check` 无 whitespace error；PowerShell 仅报告现有 LF/CRLF 工作区提示。

## 明确未完成

- 没有把 active Worker capacity 从 1 提高到 2 或更多；
- 没有 agent/provider/GPU/CPU/memory 容量模型、资源池或调度 DSL；
- 没有 workspace publish slot、排队原因/位置或 `max-parallel`；
- 没有改变 `parallel_job_execution_enabled=false`；
- 没有声称 Runtime/Pack、installer、签名、clean non-admin VM 或公开发行已经完成。

下一步应作为独立增量设计最小的、资源感知的双槽执行，不应在本提交中顺手打开并行开关。
