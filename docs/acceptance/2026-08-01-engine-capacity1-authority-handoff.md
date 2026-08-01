# Engine capacity-1 authority handoff 验收

日期：2026-08-01
基线：`64fa796` 之后的当前候选改动
范围：Engine 单 active Worker 的预准入、一次性资源移交、精确 Job authority 与 supervisor watchdog

## 结论

本增量关闭了一个具体的执行正确性缺口：Engine 不再先启动 Worker、再由 Worker 自己竞争全局重任务资源。dispatcher 现在先占有资源和既有 Job generation，再用一个有界、一次性的启动合同把两者移交给独立 Worker；Worker 只有在资源 adopt 与 Job authority 都精确匹配后才能进入 Video/Document Runtime。

该结论只适用于 capacity-1。dispatcher 仍保持单 active Worker，全局重任务仍由 `produce:heavy:v1` 串行化，`parallel_job_execution_enabled=false`。本记录不是多 Job 并行、资源调度器、正式签名 Runtime 或公开发行证明。

## 已冻结的合同

- `MachineResourceLeaseStore` schema v2 在原资源行内保存一个目标 owner、expiry 和 32-byte URL-safe nonce；adopt 是原子的一次性 CAS，旧 v1 数据在事务内迁移且不改变有效租约身份。
- `EngineWorkerLaunchV1` 是最大 4 KiB 的 canonical UTF-8 JSON 行，只包含 launch version、Job reference、resource handoff 和精确 `JobExecutionAuthority`；重复键、未知键、非 canonical bytes、超限和 CLI/reference 不一致均在执行前拒绝。
- dispatcher 在 spawn 前依次完成资源 acquire、handoff 和同 owner Job claim；任一步失败都不启动 Worker，并 stale-safe 清理已经取得的 authority。
- Worker 先 adopt resource handoff，再验证 Job 属于 Engine/local-user，并要求 same-owner `claim_job` 返回与 launch 完全相同的 generation；Runtime 只收到已 adopt lease 与 expected authority，不会获取第二份全局资源。
- supervisor 在 Worker 存活期间 heartbeat 精确 Job authority 与源/已 adopt 资源，并轮询 durable cancellation；取消、权限丢失、timeout 和 host stop 均复用既有进程树终止边界。
- pre-runtime 永久错误使用 dispatcher 预 claim 的 expected authority 收敛；随机 recovery owner 只保留给没有 launch envelope 的兼容路径。

## 验证结果

聚焦 Engine、MachineLease、Document/Video Service、Pack reconnect 与跨进程 detach 回归：

```text
240 passed, 1 skipped
```

三条真实跨进程测试路径：

```text
fixture Video detach: pass
local Video detach: pass
Document detach: pass
```

完整后端回归（禁用 bytecode 与 pytest cache）：

```text
2587 passed, 3 skipped, 3 subtests passed
```

跳过项均为原有平台/环境条件；三条 warning 为既有 downloader 转义与第三方 `pkg_resources` 弃用提示，不属于本 diff。

独立 Gate `task_def1f6dd8317` 首轮确认无 P0/P1，并指出 canonical 输入与启动 TTL 两项 P2；修复后定向 re-Gate `task_d5122c30f389` 通过，结论为 P0/P1/P2 均无剩余 finding，定向测试 `33 passed`。

## 明确未完成

- 没有把 dispatcher active capacity 从 1 提高；
- 没有 agent/provider/GPU/CPU/memory 资源池或通用资源 DSL；
- 没有 workspace publish slot、队列原因/位置或 `max-parallel`；
- 没有声称恶意同用户可写 machine-state 的 OS 级安全隔离；
- 没有新的 Runtime/Pack 签名候选、clean non-admin VM 或公开下载链路。

下一步只有在本 capacity-1 authority 边界保持全绿后，才能单独设计 Task 6 的最小可观测容量准入；不能直接把并行开关改为 `true`。
