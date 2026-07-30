# Recipe X0-A Task 8 验收报告

> 日期：2026-07-30
> 状态：PASS
> 工作树：`G:/AllToNote-video-producer`
> 分支：`codex/alltonote-x0a`
> 验收前 HEAD：`b29e4a4464cf8d05d31a0e1d52b3a64822aeb047`
> 提交边界：本报告与 Tasks 5–8 的生产代码、测试和直接状态文档由同一后续提交固化；旧工作树文档整合、Wave 0 SHA 更正和全局索引同步使用独立文档提交；`.superpowers/` 与 `config/` 未纳入。

## 1. 结论与边界

X0-A 的最小 submission/control-plane 接缝完成：静态 Recipe contracts/descriptor/registry、薄 ProduceService、Video compatibility adapter、SDK/Runtime 通用 submit facade，以及单一 `produce` CLI 心智模型均已接线。legacy Video v1/v2、Job identity、hash、config snapshot、恢复、Portable/iWiki 和 CLI envelope 保持兼容。

本 Gate 不宣称以下能力完成：

- 同一 Workspace 多 Job 并发；
- `--detach`、常驻 Engine、worker scheduler 或 AgentExecutor；
- Job/Repository/Bundle 数据面去 Video 化；
- 动态插件、公共插件 SDK、DAG 或新的数据库 schema；
- 完整多 Recipe 扩展合同。该合同必须由第一个真实非 Video Recipe（Document/PPT）与 Video 共同验证，并在 X0-B 中完成。

## 2. 验收快照

最终全量测试前后对全部 18 个 X0-A 生产代码和测试文件执行 `git hash-object`。下列内容哈希绑定实际运行的代码快照；Task 8 勾选、最终状态同步与本报告是测试通过后的文档-only 变更。

```text
7ff62dcd3a59e6453367ba65fa82d55d6d7111c2  backend/app/cli/main.py
577b628ca67054270b8ae23cfd4a0ab20835cf77  backend/app/cli/produce_request.py
943f12687f1417813b0d824d9e46211904604699  backend/app/cli/recipe_commands.py
edd3720f29216d2925c770ad976de2f6f8643a60  backend/app/core/recipes/video/adapter.py
fdf2b12ddef9d69d95064dbd4c24465283725c12  backend/app/core/recipes/video/descriptor.py
195957430a0663a0a25e41cc066a4d78cb05b11c  backend/app/core/sdk.py
e66860e5ffb6c5c86d72d2b9a7413b40a7d33899  backend/app/runtime.py
1386a2337b161b079c1350251c877cd9c3346628  backend/tests/cli/test_produce_request_file.py
da62420645117ef06a20afd22c8add7a0826de7b  backend/tests/cli/test_produce_video_cli.py
d370c3c76a35a4a9ee3de263e1de32b37a4fb1da  backend/tests/cli/test_recipe_cli.py
6a3456bc24751fb3a2fd174a381424c5d24131ec  backend/tests/cli/test_runtime_bootstrap.py
b91b3a1f02a4b3457dac49e97597924460dd1c2f  backend/tests/core/test_sdk.py
7809ff30ec05f359b8812c4772ef04b12ef2d402  backend/tests/core/test_video_recipe_adapter.py
03bb3e7e3c7abe8e4e654d87ed1d21449068c86b  backend/tests/core/test_video_request_persistence.py
568adb18ba940705b5860180885144abaa28c831  backend/tests/integration/test_fake_video_producer.py
42aaa7ed9a212774a6081326e417c7cc8dc25980  backend/tests/integration/test_local_media_golden_path.py
9e0f1a3cf039ec129fb7fd860be8d79ff12a0e7f  backend/tests/integration/test_platform_subtitle_golden_paths.py
4cc0398c0fea060bab5c93707f135aaef49135a9  backend/tests/runtime/test_runtime_paths.py
```

`config/downloader.json` 与 `.superpowers/` 是预先存在的本地状态/验收资产，不属于 X0-A 提交快照。

## 3. 验证结果

### 3.1 架构与冷路径

- `contracts.py`、`registry.py`、`produce_service.py` 无 Video import：PASS。
- ProduceService/Recipe 通用路径无 `task_serial_executor`：PASS。
- X0-A 相关改动无新线程池、daemon、Engine、DAG、动态插件、DB schema、全局锁或活动 Job 状态：PASS。
- `recipe list` / `recipe describe` 独立进程中未加载 Runtime、JobRuntime、Video adapter、Downloader、Transcriber、GPT/model client 或 FastAPI：PASS。
- `backend/tests/contracts/fixtures` diff 为空：PASS。

### 3.2 定向兼容门禁

命令覆盖 Task 1 characterization、Recipe contracts/registry/ProduceService、Video adapter、SDK/Runtime、generic/legacy CLI、Job/SQLite/恢复、Portable/iWiki、本地媒体和平台字幕路径：

```text
796 passed in 41.27s
```

### 3.3 全量 backend

```text
1906 passed, 2 skipped, 1 warning, 3 subtests passed in 57.97s
```

两个 skip 是显式平台/真实环境用例；warning 是 `ctranslate2` 对 `pkg_resources` 的既有 deprecation，不是 X0-A 回归。

### 3.4 Windows 本地视频 smoke

```text
1 passed, 14 deselected, 1 warning in 6.37s
model=tiny; device=cpu; compute_type=int8
ffmpeg_version=8.1.2-full_build-www.gyan.dev
fixture_sha256=4493bc26df912798b56a754ede158f229970d2ecb81d2892c13aed058b2ac08e
source_cache_unchanged=true
```

### 3.5 独立复审与修复闭环

独立复审先发现 2 个 blocker 和 2 个重要风险，均以失败测试固定后完成最小修复：

1. `input.attributes` 必须是真实 JSON object，list 形态不能绕过 Secret 检查；
2. request-file 禁止 caller 提供 `config_snapshot`，CLI 注入当前 `EffectiveRuntimeConfig` 快照并验证重启后的语义漂移；
3. Video adapter 对非空 `input.attributes` fail closed，不再静默丢弃；
4. `--recipe=` / `--request=` 正常预分流，公共帮助不暴露内部 `_generic` 名称。

修复后复审：PASS，无剩余代码 blocker。复审指出的恢复测试盲点也已补齐：错误快照重启执行必须返回 `effective_config_drift`，正确快照重启执行成功。

### 3.6 Git 与工作树安全

- `git diff --check`：PASS；只有 `core.autocrlf` 导致的 LF→CRLF 提示，无 whitespace error。
- X0-A 产品提交只包含上述 18 个生产/测试文件与 Tasks 5–8 的直接验收/状态文档；旧工作树文档整合、Wave 0 SHA 更正和全局索引同步拆分到独立文档提交。
- 未执行 reset、checkout、clean、stash、rebase 或覆盖用户本地资产。
- `.superpowers/` 和 `config/` 保留未跟踪状态，不进入提交。

## 4. 下一步

X0-A 关闭。下一条允许的实现主线是并发正确性 C0；C0 完成后，才能由最小真实 Document/PPT 纵切驱动 X0-B。不得从本验收直接启动 Engine、AgentExecutor、动态插件或通用数据面重写。
