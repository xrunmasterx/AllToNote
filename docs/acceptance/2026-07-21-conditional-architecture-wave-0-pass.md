# AllToNote 条件式架构重构 Wave 0 通过报告

> 日期：2026-07-21
> 状态：PASS
> 权威文档基线：`3a75d0e4101460b1924a4be2a47736efb4b29ed5`
> 实现集成基线：`066884da43105e000e00e389ab213274ca2fd6c5`
> 实现分支：`codex/alltonote-x0a`

## 目标

关闭 handoff 定义的 G0-1 至 G0-7，使 Recipe X0-A 可以从已验证、可追溯的统一基线开始。

## 基线集成

1. Video Wave 1A/runtime 实现由 `2b9e2b066e38d50ce040436d6b1995b845c61c28` 固化；
2. handoff、Recipe X0-A 与 Local Parallel 下位规格由 `ff5e5de679771f21e089ae5d72cf72378b1a32be` 固化；
3. 权威文档在独立分支 `codex/alltonote-wave0-baseline` 形成提交 `3a75d0e4101460b1924a4be2a47736efb4b29ed5`；
4. 该权威文档内容已集成到实现分支提交 `066884da43105e000e00e389ab213274ca2fd6c5`，提交消息记录完整 `Authority-Baseline` 来源；
5. `.superpowers/` 与 `config/downloader.json` 未纳入提交，未被清理、覆盖或重置。

## 测试环境

```text
Python: 3.11.15
SQLite: 3.50.4
FFmpeg: 8.1.2-essentials_build-www.gyan.dev
Whisper model: tiny
Device: cpu
Compute type: int8
```

SQLite 3.50.4 仍是 Wave 2 并发 C0 的阻塞条件；它不阻止只做控制面兼容抽取的 X0-A。

## 集成后验证

环境：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_ADDOPTS = '-p no:cacheprovider'
```

全量 Backend：

```powershell
Set-Location G:\AllToNote-video-producer\backend
..\.venv\Scripts\python.exe -m pytest -q
```

结果：

```text
1820 passed, 2 skipped, 1 warning, 3 subtests passed in 112.09s
```

Windows 本地视频 smoke：

```powershell
$env:ALLTONOTE_SMOKE_FFMPEG = 'C:\Users\dezhengu\AppData\Local\AllToNote\ffmpeg\ffmpeg.exe'
$env:ALLTONOTE_SMOKE_MODEL_CACHE = 'G:\AllToNote-video-producer\.superpowers\sdd\model-cache\faster-whisper'
..\.venv\Scripts\python.exe -m pytest -m windows_smoke tests\integration\test_local_video_golden_path.py -q -s
```

结果：

```text
1 passed, 14 deselected, 1 warning in 14.68s
source_cache_unchanged=true
ffmpeg_version=8.1.2-essentials_build-www.gyan.dev
```

Git：

```text
git diff --check: PASS
tracked worktree/index: clean before status synchronization
untracked exclusions preserved: .superpowers/, config/
```

## Gate

| Gate | 状态 | 证据 |
|---|---|---|
| G0-1 | PASS | 精确环境、命令、full backend 与 Windows smoke 结果已记录 |
| G0-2 | PASS | 历史 1820 基线在集成提交后再次复跑为 1820 passed、2 skipped |
| G0-3 | PASS | README、master tasks、coverage matrix 与 ARCH/REC-CONTRACT/RUNTIME/ENGINE 采用同一 AllToNote/Production/Recipe 解释 |
| G0-4 | PASS | 当前 X0-A 以实现工作树 `recipe-x0-compatibility-extraction/spec.md` 与 `tasks.md` 为执行源；旧大一统 RX 计划已标记部分取代 |
| G0-5 | PASS | master tasks 与 coverage matrix 同步为 Wave 0 complete、X0-A Task 1 in progress |
| G0-6 | PASS | 权威文档来源提交与实现集成提交均有稳定 SHA 和来源标记，且集成后测试通过 |
| G0-7 | PASS | Wave 0 仅集成和同步文档，没有生产代码或产品语义变化 |

## 明确未完成

- Recipe X0-A 尚未完成；当前只允许执行 Task 1；
- X0-B、真实 Document/PPT、Review/Publisher、Engine、Worker、AgentExecutor 与 Thin Desktop 均未解锁；
- SQLite 并发支持版本、CI 与打包 EXE 运行时版本仍由 Wave 2 和 Release Gate 验证；
- 本地提交尚未 push、merge 到共享主分支或发布。

## 下一步

按 `docs/design-docs/backend/recipe-x0-compatibility-extraction/tasks.md` 执行 Task 1：冻结兼容与当前执行能力基线。Task 1 不修改生产代码；所有 characterization 通过后才能进入 JobState 归位。
