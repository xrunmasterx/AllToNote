# Recipe X0-A Task 7 验收报告

> 日期：2026-07-22
> 状态：PASS
> 工作树：`G:/AllToNote-video-producer`
> 分支：`codex/alltonote-x0a`
> 验收时 HEAD：`b29e4a4464cf8d05d31a0e1d52b3a64822aeb047`
> 说明：变更仍在工作树；未 stage、commit、push、merge 或改写历史。

## 目标

建立单一 `produce` 心智模型，同时保留既有 `alltonote produce video`：

```text
alltonote produce video ...
alltonote produce <input> --recipe <id>@<version>
alltonote produce --request <request.json>
alltonote recipe list
alltonote recipe describe <id>@<version>
```

## 生产代码

- `backend/app/cli/main.py`
  - 保留 legacy parser/handler、默认 v1、JSON/Human 和错误语义。
  - 新增轻量 generic argv 预分流与 Runtime.submit 路径。
  - generic/legacy 共用 wait/Ctrl+C/cancel helper 和 snapshot renderer。
- `backend/app/cli/produce_request.py`
  - 严格 selector 和 request JSON loader；拒绝 duplicate key、非有限值、未知字段、未知 contract 及 caller-controlled mappings 中的明文 Secret。
- `backend/app/cli/recipe_commands.py`
  - 直接读取权威静态 descriptors；不构造 Runtime、Registry endpoint 或重 Pack。

未新增 add、run、Engine、动态插件、通用结果 DTO 或第二套 renderer。

## 关键兼容修复

独立审查初次发现并已修复：

1. direct generic 使用 provider default model，保持 legacy v2 request/Job identity；
2. request loader 同时检查 parameters 和 input.attributes 的明文 Secret；
3. `--recipe`/`--request` 作为 generic 明确信号，允许字面输入 `video`；
4. request-file 与 `--workspace` 冲突时 fail closed；
5. generic/legacy 共享 wait/cancel helper，不复制控制 Pipeline。

复核结果：PASS，无剩余 blocker。

## 验证

```text
Legacy CLI baseline before change: 33 passed
Lightweight recipe/request tests: 11 passed
Focused Task 7 gate after fixes: 54 passed
Complete CLI/contracts: 90 passed in 10.47s
Full backend: 1898 passed, 2 skipped, 1 warning, 3 subtests passed in 105.91s
Windows smoke: 1 passed, 14 deselected, 1 warning in 5.49s
Independent review: PASS
Cold import recipe list/describe: PASS
Legacy golden fixture diff: empty
git diff --check: PASS（仅既有 LF→CRLF warning）
```

## Gate

- legacy produce video 参数、warning、golden、exit code、Ctrl+C/cancel：PASS。
- legacy 无版本默认 v1：PASS。
- explicit v2 generic/legacy request 与 Job identity：PASS。
- request-file fail closed、Secret/path 不泄漏：PASS。
- recipe list/describe 稳定 envelope：PASS。
- discovery 不加载 Runtime/VideoService/Adapter/Downloader/Transcriber/model client/FastAPI/重型库：PASS。
- single renderer 与共享 wait/cancel：PASS。
- 无 add/run/plugin/Engine/result generalization：PASS。

## 工作树安全

- `.superpowers/`、`config/` 和既有未跟踪资产保留。
- 未执行 reset、checkout、clean、stash、stage、commit、merge、rebase 或 push。

## 后续复核更正（2026-07-30）

Task 8 的更宽独立复审在本报告之后发现 2 个 blocker 和 2 个重要风险：list 形态 `input.attributes` 可绕过 Secret 检查、request-file 未绑定当前配置快照、Video adapter 静默丢弃 attributes，以及等号形式选项/内部帮助名问题。它们已通过失败测试固定并完成最小修复；配置恢复测试进一步覆盖错误快照拒绝与正确快照成功。最终结果和哈希以 `2026-07-30-recipe-x0a-task-8.md` 为准。本节保留 Task 7 当时的历史验收事实，同时纠正“无剩余 blocker”不应跨越后续更宽审计的解释。

## 下一步

Task 8：执行 X0-A 架构、兼容与冷路径 Phase Gate。只允许补 Gate 测试和修复本阶段暴露的缺陷；不得新增产品能力。
