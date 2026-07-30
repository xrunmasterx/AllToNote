# Recipe X0-A Task 6 验收报告

> 日期：2026-07-22
> 状态：PASS
> 工作树：`G:/AllToNote-video-producer`
> 分支：`codex/alltonote-x0a`
> 验收时 HEAD：`b29e4a4464cf8d05d31a0e1d52b3a64822aeb047`
> 说明：所有变更仍在工作树；未 stage、commit、push、merge 或改写历史。

## 目标

把唯一生产提交链改为：

```text
VideoService -> VideoRecipeAdapter -> RecipeRegistry -> ProduceService -> AllToNoteSDK -> AllToNoteRuntime
```

同时保留 legacy `submit_video`、全部公开 runtime factory、机器路径、reopen、query、wait 和 cancel 行为。

## 生产代码

- `backend/app/core/recipes/video/adapter.py`
  - 增加 legacy `VideoProduceRequest` 到 `ProduceRequest` 的纯转换。
  - 对 legacy 无效 recipe/version 保留原请求 identity，由 Video preflight 维持原错误语义。
- `backend/app/core/sdk.py`
  - 只持有 `ProduceService`、窄 `JobControl` 和 legacy adapter。
  - 新增通用 `submit`；legacy `submit_video` 经过同一 generic submit 后查询 durable snapshot。
  - 不再导入、持有或暴露 `VideoService` / `_video_service`。
- `backend/app/runtime.py`
  - 新增通用 `submit`。
  - 三个直接 factory 只构造一个 VideoService，并统一装配 Adapter、Registry、ProduceService 和 SDK。
  - 五个公开 factory 名称、参数、默认值和返回类型不变。
  - 私有 components factories 仅供测试 fixture 显式取得同一 VideoService，不进入公开 Runtime facade。

未修改 CLI、VideoService 内核、JobStore、SQLite schema、result wire、Checkpoint、Portable 或 atomic commit。

## 测试代码

- `backend/tests/core/test_sdk.py`：generic 单次委托、legacy 单路径、JobControl 委托、无 `_video_service`。
- `backend/tests/core/test_video_recipe_adapter.py`：legacy v1/v2 无损 round-trip。
- `backend/tests/core/test_video_request_persistence.py`：SDK durable queued boundary 与 v1/v2 identity parity。
- `backend/tests/runtime/test_runtime_paths.py`：generic/legacy 同 identity 与 same-machine-root reopen。
- 三个 integration fixture：显式持有测试用 VideoService，不再访问 `runtime._sdk._video_service`。

## 验证证据

### Task 6 受影响回归

```text
186 passed in 60.75s
```

覆盖 SDK、Adapter、request persistence、Runtime paths、legacy CLI、Fake/Local/Platform integration。

### 全量 Backend

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_ADDOPTS = '-p no:cacheprovider'
G:/AllToNote-video-producer/.venv/Scripts/python.exe -m pytest -q --rootdir=G:/AllToNote-video-producer/backend G:/AllToNote-video-producer/backend/tests
```

```text
1877 passed, 2 skipped, 1 warning, 3 subtests passed in 151.05s
```

### Windows smoke

```text
1 passed, 14 deselected, 1 warning in 7.14s
source_cache_unchanged=true
ffmpeg_version=8.1.2-essentials_build-www.gyan.dev
```

### 架构检查

```text
公开 factory signatures: unchanged
SDK VideoService runtime import: none
_sdk._video_service repository matches: none
independent review: no blockers
git diff --check: PASS（仅既有 LF→CRLF warning）
```

## Gate

- 通用提交链唯一：PASS。
- SDK/Runtime generic submit：PASS。
- legacy submit_video 签名、durable identity 与错误语义：PASS。
- factory、路径、reopen/query/wait/cancel：PASS。
- SDK 无生产级 VideoService 反向依赖：PASS。
- 三个 integration fixture 显式取得 service：PASS。
- 无 CLI/X0-B/schema/Video 内核越界：PASS。

## 工作树安全

- `.superpowers/`、`config/` 和既有未跟踪资产保留。
- 未执行 reset、checkout、clean、stash、stage、commit、merge、rebase 或 push。

## 下一步

Task 7：收敛为单一 `produce` CLI 心智模型。先冻结 legacy CLI JSON/Human golden、exit code 与 default v1，再新增 generic `produce` 和 `recipe list/describe`，全部路由到同一个 Runtime/ProduceService。
