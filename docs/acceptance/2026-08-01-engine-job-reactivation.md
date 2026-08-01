# Engine-owned Job 重新唤醒验收

## 结论

PASS。控制面执行 `job retry` 或 `job respond` 后，若 durable Job 的执行所有者为 Engine，Runtime 会在 SQLite mutation 成功之后复用既有 `LocalEngineClient.notify_job()`，由它原子完成 Engine ensure 与 Job notify。

本次验收只关闭“Engine 已空闲退出时，retry/respond 可能永久停留在 queued”的执行活性缺口，不扩大并发能力。`parallel_job_execution_enabled` 继续为 `false`。

## 固定语义

- retry 子 Job 或 respond 后的原 Job 必须先以 `queued` 状态持久化，再允许通知 Engine；
- foreground-owned Job 不启动 Engine；
- Engine-authorized Runtime 是执行方自身，不触发控制面反向通知；
- `LocalEngineClient.notify_job()` 保持既有原子 ensure+notify 和幂等调度语义；
- 通知失败不回滚 durable Job，也不创建第二个 Job；
- `DomainError`、用户中断和非预期通知器异常统一投影为 `engine_job_reactivation_failed`，只暴露 Job ID、状态和稳定原因码；
- 人类错误文本包含 durable Job ID，并提示先运行 `alltonote engine ensure`，再按 ID 继续 `job wait`；异常消息、路径和 traceback 不进入公开结果。

## RED→GREEN 与回归

新增测试先证明以下行为在修改前失败：

- Engine-owned retry 在子 Job durable 后通知；
- Engine-owned respond 在原 Job 回到 queued 后通知；
- foreground retry 不通知；
- Engine-authorized retry 不要求控制面通知；
- 通知失败保留 durable 子 Job；
- DomainError、KeyboardInterrupt 和普通 Exception 均保留安全恢复上下文；
- 人类错误输出可见 Job ID 和准确恢复动作。

最终验证：

- `backend/tests/cli/test_job_cli.py`：40 passed；
- Engine dispatcher、Engine lifecycle、Engine CLI 与 Runtime 冷导入定向回归：38 passed；
- 首轮完整 backend 回归发现 Engine 内部 Document retry 的边界回归，修正后对应测试通过；
- 最终完整 backend 回归：2513 passed，3 skipped；
- 独立 Re-Gate：PASS，0 P0 / 0 P1。

## 保留边界

- `job wait` 仍是观察者；本次不把它改为隐式 Engine 控制命令；
- 没有 worker pool、batch、`max-parallel`、资源准入或 queue position；
- 没有 durable stage progress；该能力需要单独的 authority-fenced、transaction-coupled 事件切片；
- 本验收证明控制面活性闭环，不代替 clean-machine、签名 Runtime/Pack、安装/更新/回滚/卸载和多 Worker 负载 Gate。
