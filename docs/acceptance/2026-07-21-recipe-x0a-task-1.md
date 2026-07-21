# Recipe X0-A Task 1 验收报告

> 日期：2026-07-21
> 状态：PASS
> 分支：`codex/alltonote-x0a`
> 任务：冻结兼容与当前执行能力基线
> 生产代码变更：0 行

## 新增或强化的 characterization

1. 直接调用 `AllToNoteSDK.submit_video`，冻结 durable queued facade；
2. 为 Video v2 固定完整 canonical request JSON、Job request hash 与 Video/checkpoint hash；
3. 固定 Human positional warning 的 stdout、stderr 和 exit code；
4. 证明同一 Runtime 上两个不同 Job 的完整执行保持串行；
5. 证明 submit-only Job 在 Runtime 重开后可由后续 wait 执行；
6. 保留并复核 source identity conflict 不产生第二次 commit 或第二个正式 Bundle。

## 既有直接证据

- config snapshot：`tests/runtime/test_runtime_config_service.py`；
- legacy raw result 与 v2 documents round-trip：`tests/adapters/test_sqlite_job_repository.py`；
- crash-after-rename、runtime reopen、historical candidate 与 zero replay：`tests/integration/test_fake_video_producer.py`；
- Portable manifest、Bundle identity 与字节确定性：`tests/integration/test_video_bundle_assembly.py`、`tests/adapters/test_iwiki_portable_gateway.py`；
- scheduler_busy 与 fencing takeover：`tests/adapters/test_sqlite_job_repository.py`；
- CLI JSON/Human、wait、timeout、Ctrl+C/cancel：`tests/cli/test_produce_video_cli.py`、`tests/cli/test_job_cli.py`、`tests/contracts/test_cli_envelope_golden.py`。

## 聚焦验证

新增测试初始聚焦集合：

```text
10 passed in 2.58s
```

不同 Job 串行测试使用 Barrier/Event/Lock 和有界 timeout，不使用 sleep；独立进程重复执行五次：

```text
5 x 1 passed
```

Task 1 完整兼容面：

```powershell
..\.venv\Scripts\python.exe -m pytest -q `
  tests/core/test_video_request_persistence.py `
  tests/adapters/test_sqlite_job_repository.py `
  tests/contracts/test_cli_envelope_golden.py `
  tests/integration/test_fake_video_producer.py `
  tests/cli/test_produce_video_cli.py `
  tests/cli/test_job_cli.py `
  tests/runtime/test_runtime_config_service.py `
  tests/core/test_checkpoint_recovery.py `
  tests/core/test_execution_safety.py
```

结果：

```text
388 passed in 28.26s
```

## 纠正的测试假设

审查期间曾尝试断言 source conflict 后 `.staging` 为空。现有实现和 Portable gateway 合同表明，staging 内容可作为未提交 candidate/recovery 数据保留；真正冻结的不变量是：

- 第二个 Job 为 `source_identity_conflict`；
- 第二个 Job 无 result；
- 正式 committed Bundle 仍只有第一个；
- portable commit 调用总数仍为 1。

因此没有把“清空 staging”升级为新产品合同，也没有为错误测试修改生产代码。

## Gate

- legacy SDK submit：PASS；
- Video v1/v2 canonical request：PASS；
- 两套 hash：PASS；
- config snapshot：PASS；
- raw result round-trip：PASS；
- crash/reopen/historical/zero replay：PASS；
- Portable identity/source conflict rollback：PASS；
- CLI envelope/warning/exit/wait/cancel：PASS；
- same Runtime serial：PASS；
- second scheduler owner busy：PASS；
- queued submit then later wait：PASS；
- production changes：none。

## 下一步

Task 2 只归位 `JobState` 所有权。不得同时迁移 `JobSnapshot`、result JSON、Repository 或 atomic commit。
