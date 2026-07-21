# Recipe X0-A Task 2 验收报告

> 日期：2026-07-21
> 状态：PASS
> 分支：`codex/alltonote-x0a`
> 任务：只归位 JobState 所有权

## 生产改动

- `app/core/jobs/model.py` 成为唯一 `JobState` 定义位置；
- `app/core/domain/video.py` 兼容重导出同一个类型对象；
- `app/core/jobs/state_machine.py` 从通用 Job model 导入；
- `JobSnapshot`、`RetryJobRequest`、`VideoProduceResult` 保持原位。

## 不变量

- Enum 六个成员及字符串值不变；
- 九条合法状态转换不变；
- `domain.video.JobState is jobs.model.JobState`；
- 现有 `is JobState.*` 判断继续使用同一成员对象；
- SQLite schema、request/result JSON、CLI wire 均未修改；
- Repository、port、CLI、Runtime 没有改动。

## 测试

实施前聚焦测试按预期失败：

```text
1 failed, 2 passed
```

失败原因是旧所有者仍为 `app.core.domain.video`。

核心与持久化：

```text
247 passed
```

Job submit/get/wait/cancel/retry 兼容面：

```text
338 passed
```

全量 Backend：

```text
1827 passed, 2 skipped, 1 warning, 3 subtests passed
```

`git diff --check` 通过；生产代码中 `class JobState` 仅命中 `app/core/jobs/model.py`。

## 下一步

Task 3 建立最小 Recipe submission 合同。不得加入 Result、Preflight、Plan、Artifact、资源或动态插件合同。
