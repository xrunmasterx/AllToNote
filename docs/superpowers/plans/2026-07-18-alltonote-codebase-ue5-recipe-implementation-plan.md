# AllToNote Codebase / UE5 Recipe 实施计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-18-alltonote-codebase-ue5-recipe-design.md
  - ../specs/2026-07-18-alltonote-recipe-extension-contract-design.md
implementation_status: not-started
last_verified_at: 2026-07-18
```

## 1. 前置

- Recipe internal contract 至少经 Web/Document 基础验证；
- ModelExecutor稳定；
- 独立 AgentExecutor/ExecutionGrant设计按本设计实现；
- 先用公开小/中仓库，真实私有 UE5项目需用户授权且不把源码上传未批准 Provider；
- 原型可前台运行，生产长任务/Production MCP需 Engine Gate。

## 2. 成功标准

固定 repo/revision/scope；Agent默认无写/shell/network；核心 claim有 commit/file/line/symbol Evidence；真实 C++和UE5模块专业评测；增量 revision不覆盖用户文档；仓库状态前后不变。

## 3. Task CODE-00：评测集与文档模板

选择：小型公开多文件仓库、中型 C++仓库、一个可授权 UE5模块。为 module briefing建立人工问题/关键事实/错误诱饵/证据金标；先定义质量 rubric，不以“看起来专业”验收。

## 4. Task CODE-01：Repo Grant/Revision

实现 repo catalog、root grant、full commit/tree、clean/dirty/submodule/LFS；默认 dirty fail，显式 snapshot manifest/diff。禁止 Git写操作/hooks/fetch。测试 worktree前后 `git status`一致。

## 5. Task CODE-02：Scope/Manifest

module/path/symbol/change-range selectors；git tree/ls-files固定revision；ignore/size/language/hash；generated/vendor/binary排除；plan显示files/bytes。全库需显式确认。

## 6. Task CODE-03：Read/Search/Evidence Tools

新增 repo_tree/code_search/code_read/git_diff/build_metadata_read/evidence_record，全部 opaque file IDs + fixed revision + scope。line range/content hash，path traversal/symlink/submodule边界。

## 7. Task CODE-04：Syntax/Symbol Index

Tree-sitter或选定 parser；symbols/import/include；content-addressed per file；能力/语言显式。无编译数据库不声称深语义。

## 8. Task CODE-05：C/C++ Semantic Pack

compile_commands/clangd/clang tooling spike；definitions/references/include/conditional context；静态/动态索引能力等级；缺/旧编译数据库降级。Pack隔离/version/timeout。

## 9. Task CODE-06：ExecutionGrant/AgentExecutor

新增 Core port/application coordinator与一个实际 Adapter（例如本地 Codex bridge）：allowed tools/scope/no network/write/shell/max steps/time/budget。保存tool/result hashes和结构化receipt，不保存chain-of-thought。

攻击测试：repo内AGENTS/README要求越权、path/secret读取、shell、无限循环、伪造Evidence。

## 10. Task CODE-07：Scope Map/Evidence Graph

阶段A只输出模块边界/候选/问题；阶段B逐项收集职责/入口/生命周期/数据/线程/配置/失败/性能证据。Schema validator拒绝无效path/range/hash/越权Evidence。

## 11. Task CODE-08：Module Briefing Compiler

global outline、章节生成、唯一H1、术语/时序/所有权；claim只能引用Evidence graph；context/inference显式；不做文件列表式总结。

## 12. Task CODE-09：Independent Verifier

独立上下文回读源码、检查相反分支/平台/线程/引用；输出 issue list/targeted repair。无critical unresolved才publish eligible。确定性验证每条citation。

## 13. Task CODE-10：UE5 Pack

解析 `.uproject/.uplugin/Build.cs/Target.cs`、module dependencies、Public/Private、UObject宏/Subsystem/Module lifecycle、engine version/platform hints。所有模式回到实际源码；asset依赖标gap。通用Core无UE5 if。

## 14. Task CODE-11：UE5真实评测

由熟悉模块的工程师按 rubric检查：职责/边界/启动时序/所有权/线程/配置/平台/调试/风险/源码引用。至少一次盲评对比 baseline“简单RAG总结”。不因文章更长判优。

## 15. Task CODE-12：Deep Analysis

在 briefing质量达标后扩展并发/生命周期/算法/性能/平台/失败；扩大证据问题集和预算，不改变基础Evidence/Grant。

## 16. Task CODE-13：Change Digest/Incremental

base/head diff、changed symbols/build dependency、impact范围、旧Evidence失效、affected章节。复用未受影响章节需 hash证明；全局一致性复核；新Draft进入Review diff。

## 17. Task CODE-14：Job/Bundle/CLI

`produce codebase --repo --revision --scope --draft`；Agent/tool/model external operations/checkpoint/cancel/restart；Artifact/Receipt；raw/personal commit；CLI JSON/错误。

## 18. Task CODE-15：安全/故障

恶意repo、secret files、symlink/submodule、huge generated file、parser/clangd/Agent crash、budget、Provider unknown、cancel、Pack更新、dirty变化。默认无代码执行；远端模型 policy测试。

## 19. Task CODE-16：Review/Publisher/MCP Read

Draft -> source code Evidence定位 -> approve/personal publish；新 commit生成新Candidate/diff；Knowledge MCP读published文档及explicit provenance range，不暴露整个repo/raw。

## 20. Task CODE-17：验收

输出 `docs/acceptance/codebase-ue5-recipe-v1.md`：repo公开ID/commit/scope、文档kind、claim/citation/coverage、专业评分、Agent calls/cost、security、restart；私有路径/源码正文/Prompt不写。

## 21. 执行顺序

```text
CODE-00..05
 -> CODE-06/07
 -> CODE-08/09
 -> CODE-10/11
 -> CODE-12
 -> CODE-13/14
 -> CODE-15..17
```

任何阶段若 Agent 需要任意 shell/write 才能继续，停止并先设计最小 ToolExecutionGrant；不得静默扩权。
