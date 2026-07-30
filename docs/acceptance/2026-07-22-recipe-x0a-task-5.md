# Recipe X0-A Task 5 验收报告

> 日期：2026-07-22
> 状态：PASS
> 实现工作树：`G:/AllToNote-video-producer`
> 分支：`codex/alltonote-x0a`
> 验收时 HEAD：`b29e4a4464cf8d05d31a0e1d52b3a64822aeb047`
> 说明：Task 5 文件仍为未提交工作树改动；本次未 stage、commit、push、merge 或改写历史。

## 目标

把通用 `ProduceRequest` 确定性翻译为既有 `VideoProduceRequest`，继续委托 `VideoService.submit_video()`，并证明 legacy/generic v1/v2 durable identity 与配置快照保持一致。

## 生产代码

- `backend/app/core/recipes/video/adapter.py`：Video v1/v2 兼容 Adapter；只做校验、字段转换和委托。
- `backend/app/core/recipes/video/descriptor.py`：静态、冷路径可读的 Video descriptors。

未修改 `VideoService`、JobStore、SQLite schema、legacy result wire、hash 算法、Checkpoint、Portable 或 atomic commit。

## 测试代码

- `backend/tests/core/test_video_recipe_adapter.py`：v1/v2 映射、显式 output bindings/config snapshot、错误边界和 cold import。
- `backend/tests/core/test_video_request_persistence.py`：generic/legacy v1/v2 Job ID、canonical request、Job hash、Video/checkpoint hash、principal 和 config snapshot parity。

## 环境

```text
Python: 3.11.15
SQLite: 3.50.4
Runtime: 0.1.0
llm-iwiki: 0.1.2
FFmpeg: 8.1.2-essentials_build-www.gyan.dev
Codex CLI: 0.144.1
Claude Code: 2.1.215
Provider: deterministic fake/local test providers；未调用真实远程 Provider
Whisper smoke: tiny / cpu / int8
```

SQLite 3.50.4 不阻塞不改 schema、不启用并发的 X0-A；它继续阻塞 Wave 2 C0 及后续多连接 WAL/Engine Gate。

## 验证

### Task 5 聚焦集

```powershell
G:/AllToNote-video-producer/.venv/Scripts/python.exe -m pytest -q --rootdir=G:/AllToNote-video-producer/backend G:/AllToNote-video-producer/backend/tests/core/test_video_recipe_adapter.py G:/AllToNote-video-producer/backend/tests/core/test_video_request_persistence.py
```

```text
16 passed in 0.40s
```

### Recipe 控制面兼容集

```powershell
G:/AllToNote-video-producer/.venv/Scripts/python.exe -m pytest -q --rootdir=G:/AllToNote-video-producer/backend G:/AllToNote-video-producer/backend/tests/core/test_recipe_contracts.py G:/AllToNote-video-producer/backend/tests/core/test_recipe_registry.py G:/AllToNote-video-producer/backend/tests/core/test_produce_service.py G:/AllToNote-video-producer/backend/tests/core/test_video_recipe_adapter.py G:/AllToNote-video-producer/backend/tests/core/test_video_request_persistence.py
```

```text
50 passed in 0.73s
```

### 冻结核心兼容集

```powershell
G:/AllToNote-video-producer/.venv/Scripts/python.exe -m pytest -q --rootdir=G:/AllToNote-video-producer/backend G:/AllToNote-video-producer/backend/tests/core/test_video_request_persistence.py G:/AllToNote-video-producer/backend/tests/core/test_job_service.py G:/AllToNote-video-producer/backend/tests/contracts/test_cli_envelope_golden.py G:/AllToNote-video-producer/backend/tests/core/test_checkpoint_runner.py G:/AllToNote-video-producer/backend/tests/core/test_model_call_coordinator.py
```

```text
71 passed in 2.21s
```

### 全量 Backend

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_ADDOPTS = '-p no:cacheprovider'
G:/AllToNote-video-producer/.venv/Scripts/python.exe -m pytest -q --rootdir=G:/AllToNote-video-producer/backend G:/AllToNote-video-producer/backend/tests
```

```text
1871 passed, 2 skipped, 1 warning, 3 subtests passed in 91.81s
```

### Windows 本地视频 smoke

```powershell
$env:ALLTONOTE_SMOKE_FFMPEG = 'C:/Users/dezhengu/AppData/Local/AllToNote/ffmpeg/ffmpeg.exe'
$env:ALLTONOTE_SMOKE_MODEL_CACHE = 'G:/AllToNote-video-producer/.superpowers/sdd/model-cache/faster-whisper'
G:/AllToNote-video-producer/.venv/Scripts/python.exe -m pytest -q -s -m windows_smoke --rootdir=G:/AllToNote-video-producer/backend G:/AllToNote-video-producer/backend/tests/integration/test_local_video_golden_path.py
```

```text
1 passed, 14 deselected, 1 warning in 4.37s
source_cache_unchanged=true
ffmpeg_version=8.1.2-essentials_build-www.gyan.dev
```

### Git 与审查

```text
git diff --check: PASS；只有 core.autocrlf 导致的 LF→CRLF warning
独立 Task 5 代码审查：no blockers
```

## Task 5 Gate

- Video descriptor cold path：PASS。
- v1/v2 确定性字段转换：PASS。
- generic/legacy 同 client request identity 的 Job ID、canonical request、Job hash、Video/checkpoint hash：PASS。
- requested outputs、resolved bindings、language policy、principal、config snapshot：PASS。
- Adapter 仅委托 `VideoService.submit_video()`：PASS。
- 未复制 Video pipeline、未修改 schema/result wire/atomic commit：PASS。
- legacy 默认 v1 与显式 v2 parity：PASS。

## 工作树安全

- `.superpowers/`、`config/` 与既有未跟踪资产保留。
- 未执行 reset、checkout、clean、stash、stage、commit、merge、rebase 或 push。
- 未触碰 SDK、Runtime、CLI、SQLite migration、result decoder 或 atomic commit。

## 下一步

Task 6：改造 SDK 与 Runtime 组合根。Task 6 必须先写 characterization/failing tests，并保持所有现有 factory、workspace/machine root、reopen/job wait/cancel 行为不变。
