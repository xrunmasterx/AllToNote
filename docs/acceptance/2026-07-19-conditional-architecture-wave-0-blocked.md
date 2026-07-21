# AllToNote 条件式架构重构 Wave 0 阻塞报告

> 日期：2026-07-19
> 状态：PARTIAL — Windows smoke PASS，待权威文档收敛与 G0-6 Git 集成
> 实现工作树：`G:/AllToNote-video-producer`
> 权威文档工作树：`G:/AllToNote`
> 剩余 Gate：G0-3、G0-4、G0-5、G0-6

## Wave

Wave 0：权威文档、资产归属与可重复基线。

## 目标

在不覆盖、清理或提交现有 dirty linked worktree 的前提下：

1. 建立两个工作树的可恢复证据包；
2. 在隔离恢复副本中复现兼容、恢复和全量测试基线；
3. 检查 Windows smoke 前置能力；
4. 只有前置 Gate 全部通过后才允许修订权威文档并建立可追溯 Git 基线。

## 状态

**PARTIAL**：资产、测试和 Windows smoke Gate 已通过；权威文档与 Git 集成 Gate 尚未完成。

确定性证据和测试均已通过。首次 smoke 使用 MonoGame NuGet 附带的 2014 年 FFmpeg，在截图提取阶段失败；随后检查发现符合 AllToNote 本机目录规范的既有缓存 `C:\Users\dezhengu\AppData\Local\AllToNote\ffmpeg`。使用其中的 FFmpeg 8.1.2 与现有完整 tiny 模型缓存后，真实 Windows smoke 全流程通过。

## 解决的问题 ID

- P-15：已建立受限本地可恢复证据包，验证 dirty linked worktree 的产品资产可精确恢复。
- Wave 0 测试事实：已用当前锁定环境复跑快速集、恢复集、完整兼容面和两次全量 deterministic suite。

## 假设与已确认决策

- 修订版 handoff、X0-A spec/tasks 和 Local Parallel spec/tasks 是本轮实施口径。
- CLI 使用单一 `produce` 心智，不新增 `add` 或独立 `run` 主入口。
- Engine 产品需求已触发，但不属于 X0-A。
- 证据采用受限本地恢复包；允许恢复必要字节中保留机器路径，`publishable=false`。
- 私密/ignored 运行资产只做无路径聚合，不复制正文、不承诺 Machine State 恢复。
- tracked 文件模式以 Git index 为权威。

## 明确未解决

- Windows smoke 已通过：AllToNote 本地 FFmpeg 8.1.2、真实 Faster Whisper tiny CPU/int8、真实截图/WebP、Bundle/iwiki 语义验证和源模型缓存不变均通过。
- CI 和打包 EXE 的 Python/SQLite/iwiki/Core/bridge 版本尚未验证。
- 当前 SQLite 仍为 3.50.4，不满足后续并发 C0/Release Gate。
- 权威文档冲突尚未改写；按阶段规则，测试 Gate 被阻塞后停止后续写入。
- G0-6 尚未完成：当前没有 stage/commit/integration 授权，证据包不能替代可追溯 Git 基线。
- Codex client 同一实例的并发重入风险仍是待 Wave 2 复现的高置信风险，不是已确认 runtime 缺陷。

## 生产代码

无生产代码修改。

## 文档代码

新增本阻塞报告：

- `docs/acceptance/2026-07-19-conditional-architecture-wave-0-blocked.md`

未修订上位/下位权威设计，因为 Windows smoke Gate 已触发 Stop。

## 证据包

最终有效证据包：

```text
G:/AllToNote-wave0-evidence/wave0-20260719T152202Z
```

验证摘要：

- final envelope SHA-256：`sha256:f0ef2738bf7fd8050faa0c518853d33a4c0e412c43822e761629b6b57b86560f`
- payload manifest SHA-256：`sha256:10544d76983a43da2eba9ae1a25d7323b8d5d19190fe580ff2351ec6b59ba1fd`
- 182 个稳定 payload 记录全部哈希与长度匹配；
- 42 个 tracked dirty 文件原始字节精确恢复；
- 100 个 product untracked 文件精确恢复；
- 三个逻辑 worktree 的 HEAD、refs、index mode、patch 与 `git diff --check` 通过；
- 91,768 + 37,943 个候选全部分类，`unclassified_count=0`；
- ignored/private runtime 仅聚合，`paths_disclosed=false`、`content_copied=false`；
- `.superpowers` 产品载荷中只包含两份明确批准的 acceptance runner；
- 源工作树在捕获和独立复核后保持不变。

以下历史尝试已明确 `INVALIDATED`，不得替代最终证据包：

- `wave0-20260719T133758Z`
- `wave0-20260719T143538Z`
- `wave0-20260719T143702Z`
- `wave0-20260719T145940Z`

## 性能与运行环境

```text
Python: 3.11.15
pip: 26.1.2
pytest: 9.1.1
SQLite: 3.50.4
llm-iwiki: 0.1.2
tomli-w: 1.2.0
faster-whisper: 1.1.1
ctranslate2: 4.6.0
yt-dlp: 2026.7.4
```

当前 smoke 使用并验证：

```text
ALLTONOTE_SMOKE_FFMPEG=C:\Users\dezhengu\AppData\Local\AllToNote\ffmpeg\ffmpeg.exe
ALLTONOTE_SMOKE_MODEL_CACHE=G:\AllToNote-video-producer\.superpowers\sdd\model-cache\faster-whisper
FFmpeg=8.1.2-essentials_build-www.gyan.dev
FFmpeg SHA-256=1326dde4c84ff1f96fe6b8916c5bed29e163e9b5dccf995f6f3db069d143ec5e
```

说明：用户提供的 `G:\AllToNote-video-producer\.venv\Lib\site-packages\faster_whisper` 是 Python 包目录，不是模型缓存；实际 smoke 使用已有的 Hugging Face cache root。MonoGame NuGet 的旧 FFmpeg `N-63930-g1c5aa64` 已被真实 smoke 证明不兼容截图提取，不再作为验收候选。

## 测试与兼容证据

所有测试均在证据包的隔离恢复副本中运行，设置：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_ADDOPTS = '-p no:cacheprovider'
```

### Collection

```powershell
& $AllToNotePython -m pytest --collect-only -q -p no:cacheprovider
```

结果：

```text
1822 tests collected in 6.00s
```

Windows marker collection：

```powershell
& $AllToNotePython -m pytest --collect-only -q -p no:cacheprovider -m windows_smoke
```

结果：

```text
1 selected, 1821 deselected
```

### 快速兼容集

```powershell
& $AllToNotePython -m pytest -q `
  tests/core/test_video_request_persistence.py `
  tests/core/test_job_service.py `
  tests/contracts/test_cli_envelope_golden.py `
  tests/core/test_checkpoint_runner.py `
  tests/core/test_model_call_coordinator.py
```

结果：

```text
68 passed in 2.70s
```

### Recovery / zero-replay 基线

冻结清单位于：

```text
G:/AllToNote-wave0-evidence/wave0-20260719T152202Z-recovery-nodes.txt
```

14 个节点展开为 17 个测试实例：

```text
17 passed in 5.84s
```

覆盖 durable model success、missing success anchor、non-retryable、unknown outcome、result-store failure、checkpoint rewind、acquisition checkpoint 复用、started model 不重发、crash-after-rename、draft/screenshot recovery、FFmpeg 不重复和 source snapshot 恢复。

### 完整兼容面

按 handoff 8.3 的文件集合执行：

```text
794 passed in 57.88s
```

### 全量 deterministic suite，第 1 次

```powershell
& $AllToNotePython -m pytest -q
```

结果：

```text
1820 passed, 2 skipped, 3 warnings, 3 subtests passed in 80.42s
```

### 全量 deterministic suite，第 2 次

从同一证据包建立第二份全新隔离恢复副本后运行同一命令：

```text
1820 passed, 2 skipped, 3 warnings, 3 subtests passed in 81.86s
```

两次结果一致；未检测到 nondeterminism。三个 warning 为两个既有无效转义 DeprecationWarning 和 `ctranslate2/pkg_resources` 第三方弃用 warning。

### Windows smoke

首次使用旧 MonoGame FFmpeg 的失败证据保留在：

```text
G:/AllToNote-wave0-evidence/wave0-20260719T152202Z-test-windows-smoke.txt
```

该运行完成 transcript 后在 screenshot extraction 失败：

```text
1 failed, 14 deselected, 1 warning in 5.33s
```

随后使用 AllToNote 本机缓存执行：

```powershell
$env:ALLTONOTE_SMOKE_FFMPEG = 'C:\Users\dezhengu\AppData\Local\AllToNote\ffmpeg\ffmpeg.exe'
$env:ALLTONOTE_SMOKE_MODEL_CACHE = 'G:\AllToNote-video-producer\.superpowers\sdd\model-cache\faster-whisper'
& $AllToNotePython -m pytest -m windows_smoke tests/integration/test_local_video_golden_path.py -q -s
```

最终结果：

```text
1 passed, 14 deselected, 1 warning in 7.11s
```

真实运行摘要：

```json
{"compute_type":"int8","device":"cpu","ffmpeg_version":"8.1.2-essentials_build-www.gyan.dev","fixture_sha256":"4493bc26df912798b56a754ede158f229970d2ecb81d2892c13aed058b2ac08e","model":"tiny","source_cache_unchanged":true}
```

通过范围包括：真实 Whisper tiny 转写、真实 FFmpeg screenshot/WebP、Job 成功终态、开放 Bundle、iwiki semantic validation，以及调用方模型缓存前后完全不变。


## 并发与故障证据

本轮没有实现或启用并发 Engine。现有 global execution lock 和 scheduler lease 保持不变。

已通过的恢复测试验证了：

- durable success 不重复 provider；
- unknown outcome 不自动 replay；
- acquisition/transcript/draft/screenshot checkpoint 被复用；
- crash-after-rename 可 reconcile；
- FFmpeg 和模型副作用 counter 不重复。

## Schema / Protocol

- SQLite schema 未修改；
- legacy request/result wire 未修改；
- Video v1/v2 默认与 hash 语义未修改；
- 没有执行 migration；
- 没有移除 execution lock 或 scheduler lease；
- 证据包的 private Machine State 未恢复，符合已确认的 aggregate-only 策略。

## 工作树安全

### 执行前

- `G:/AllToNote`：`master`，HEAD `1b0320924900d8e191e27d7bed3667d18ddd7590`，dirty；
- `G:/AllToNote-video-producer`：`codex/alltonote-video-producer`，HEAD `32891d352c7df5c9fed0bda19f00e0558b9eb52a`，dirty；
- 两个暂存区均为空。

### 执行后

- 在生成本报告之前，branch、HEAD、refs、index digest、status digest、tracked dirty bytes 和 product untracked bytes 与最终证据快照一致；
- 本报告是证据冻结完成后的唯一受控新增文档，不属于 `wave0-20260719T152202Z` 已封存 payload；其路径与 SHA-256 在最终交付摘要中单独记录；
- 未执行 `git add`、commit、merge、cherry-pick、stash、reset、checkout、clean；
- 未安装、升级或下载依赖；
- 测试只在外部隔离恢复副本中运行；
- 无用户资产被删除或覆盖。

## Gate

| Gate | 状态 | 证据 |
|---|---|---|
| G0-1 | PASS | 精确命令、版本、快速/recovery/兼容/两次全量和真实 Windows smoke 结果均已记录 |
| G0-2 | PASS | 历史约 1820 passed 已以当前 1822 collection、1820 pass + 2 skip 结果重新核实，不再作为孤立历史声明 |
| G0-3 | BLOCKED | 文档冲突已定位，但按 Stop 尚未改写 |
| G0-4 | BLOCKED | 旧 RX 与修订 X0-A/X0-B 仍待文档同步 |
| G0-5 | BLOCKED | master tasks 与 coverage matrix 仍待同步 |
| G0-6 | BLOCKED | 未获 stage/commit/integration 授权，尚无统一可追溯 Git 基线 |
| G0-7 | PASS（本轮） | 本轮没有生产语义改动；只新增验收报告和外部证据 |

## Stop Conditions

已触发但已解决：

```text
WINDOWS-SPECIFIC / 旧 MonoGame FFmpeg screenshot extraction incompatible
解决：使用既有 AllToNote 本机 FFmpeg 8.1.2 缓存，真实 smoke PASS
```

仍阻塞：

```text
G0-3 / G0-4 / G0-5 authority documents not reconciled
G0-6 / no traceable integrated Git baseline yet
```

未触发：

- 证据包恢复失败；
- 源工作树竞争变化；
- 不可归因 baseline failure；
- 测试 nondeterminism；
- 产品回归；
- 删除或覆盖未提交工作；
- EOL 归一化；
- Video 默认版本变化；
- 生产 schema/wire/hash 变化。

## 下一步

第一项允许动作是继续 Wave 0 文档冲突收敛：

1. 同步 ARCH、REC-CONTRACT、RUNTIME、ENGINE、master tasks 与 coverage matrix；
2. 统一 X0-A/X0-B、单一 `produce`、Engine 已触发但不属于 X0-A、Job-scoped generation claim；
3. 对文档 Gate 做独立只读审查；
4. 另行以显式路径 stage/commit/integration 建立 G0-6 可追溯基线；
5. 只有全部 G0 Gate 通过后，才从已集成基线新建干净 `codex/*` 工作树并开始 X0-A。
