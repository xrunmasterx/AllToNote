# AllToNote Codebase / UE5 Knowledge Recipe 设计

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-14-alltonote-portable-artifact-source-bundle-design.md
  - 2026-07-18-alltonote-recipe-extension-contract-design.md
downstream:
  - ../plans/2026-07-18-alltonote-codebase-ue5-recipe-implementation-plan.md
implementation_status: not-started
last_verified_at: 2026-07-18
```

## 1. 决策摘要

Codebase/UE5 Recipe 不是“把仓库切块后向量检索再总结”，而是以固定 Git revision、确定性代码索引、受限 AgentExecutor 和源码 Evidence 组合生产专业知识文档：

```text
Repo + revision + scope
  -> immutable source snapshot identity
  -> file/module/build/symbol/dependency inventory
  -> Agent 在只读 grant 下分阶段取证
  -> 每个事实绑定 commit/file/line/symbol/content hash
  -> architecture synthesis + independent verification
  -> module briefing / deep analysis / change digest Draft
  -> deterministic quality + evidence audit
  -> Bundle -> Review
```

UE5 是独立 Feature Pack，通用 Core 只认识 repo、revision、symbol 和 Evidence，不依赖 Unreal Engine 安装、UBT 或项目专有规则。

## 2. 用户目标

- 将大型代码库或 UE5 模块整理为可长期维护的知识文档；
- 文档说明“谁负责、何时执行、数据如何流动、关键不变量与失败方式”，而非文件清单；
- 每个重要结论可跳到固定 commit 的具体源码；
- Agent 只能读取用户授权范围，不修改代码、不执行任意仓库指令；
- 新 commit 到来时增量更新受影响文档，不必全库重跑；
- 可以生成模块概览、深度原理分析和 commit/MR 变更知识；
- 生成的 Markdown 可由 Obsidian、Git、其他 Agent 使用；
- 大型 UE5 仓库只分析明确模块/主题，时间和模型成本可预估。

## 3. 非目标

- 通用代码补全 IDE；
- 自动修改、编译或提交代码；
- 仅靠 embedding 声称理解整个仓库；
- 默认执行仓库脚本、构建、测试或下载依赖；
- 为所有语言一次性提供深语义分析；
- 把工作区未提交状态静默混入 commit 事实；
- 解析所有 `.uasset`/蓝图/二进制资产；
- 替代正式 code review 或运行时性能分析；
- 自动将 Agent 输出发布为 common；
- 在通用 Core 中写 ProjectH 或 UE5 专有条件分支。

## 4. Draft 类型

### 4.1 `module-briefing`

面向首次接手者的 10–15 分钟模块概览：职责、边界、核心类型、关键执行链、配置、扩展点、调试入口和风险。

### 4.2 `deep-analysis`

面向资深工程师的原理文档：生命周期、所有权、线程/并发、数据结构、算法、平台差异、错误恢复、性能和源码证据。

### 4.3 `change-digest`

基于 commit/range/MR diff：用户可见变化、执行流变化、风险、测试、迁移和受影响知识。它不是复制 commit message。

MVP 先实现 `module-briefing`，再验证 `deep-analysis`；`change-digest` 复用 revision/diff Evidence 后实现。

## 5. Repo Source 与 Revision

### 5.1 Repo identity

```json
{
  "kind": "git-repository",
  "repo_id": "repo_...",
  "remote_fingerprint": "sha256:normalized-remotes-or-null",
  "root_identity": "opaque-local-root-id",
  "vcs": "git"
}
```

本地绝对路径不是 portable identity。remote 只作辅助；无 remote 的仓库仍可使用用户赋予的 stable repo_id。

### 5.2 Clean revision（默认）

```json
{
  "commit": "<full-object-id>",
  "tree": "<tree-object-id>",
  "worktree_state": "clean",
  "submodules": [],
  "lfs_state": "..."
}
```

必须使用完整 object ID；branch 名只作用户输入 selector，解析后固定 commit。

### 5.3 Dirty snapshot

默认发现 dirty worktree 时停止并提示：

- 使用 clean commit；
- 显式 `--include-dirty`；
- 选择只分析 `HEAD`。

显式 dirty 模式保存：

- HEAD commit/tree；
- tracked diff hash/Artifact；
- untracked included file manifest/hash；
- excluded files；
- dirty snapshot digest；
- source privacy=personal。

不自动 `git add`、stash、commit、checkout 或修改 worktree。大/敏感 untracked 文件必须通过 scope/ignore/size policy。

### 5.4 Submodule/LFS

- 每个 submodule 固定 path + commit；
- 未初始化/缺对象明确为 incomplete；
- Git LFS pointer 与实际对象状态分开；
- 不自动联网 fetch，除非用户显式 network grant；
- Evidence 不得指向未实际读取的对象。

## 6. Scope

用户必须选择有限 scope：

```json
{
  "kind": "module|path|symbol|change-range|question",
  "selectors": ["Engine/Source/Runtime/RHI"],
  "include_dependencies": "direct",
  "exclude": ["Binaries/**", "Intermediate/**", "Saved/**"],
  "max_files": 5000,
  "max_bytes": 50000000
}
```

默认排除 generated/build/cache/binary/vendor 大目录；使用 `.gitignore`、官方默认和可选 `.alltonoteignore`，但不能让仓库文件通过 ignore 规则扩张到 grant 外。

全库分析是特殊显式任务，需要 plan 显示 files/bytes/languages/estimated calls；不作为默认。

## 7. 确定性索引

### 7.1 通用层

- `git ls-tree/ls-files` 等固定 revision 文件清单；
- language/size/hash；
- Tree-sitter 等 parser 的 syntax tree/symbol candidates；
- imports/includes/references 的静态图；
- README/config/build files；
- test 与 source 关系；
- commit history/diff（按任务范围）；
- content-addressed file/symbol index。

全文/FTS/向量都是检索辅助，不是源码事实。Agent 引用前必须读取固定 revision 的实际内容。

### 7.2 C/C++ 语义层

有 `compile_commands.json`/编译数据库时使用 clangd/clang tooling 获得更可信的：

- definitions/references；
- include graph；
- type/function/macro identity；
- conditional compilation context；
- diagnostics（只作证据，不等于构建通过）。

没有编译数据库时明确降级为 syntax/text evidence，不能声称完整跨文件语义。

### 7.3 UE5 Pack

`ue5-analysis` Pack 增加：

- `.uproject/.uplugin`；
- `*.Build.cs`、`*.Target.cs`；
- Module/Public/Private 结构；
- UObject/UClass/USTRUCT/UENUM/UFUNCTION/UPROPERTY 宏上下文；
- Engine/Plugin/Game module dependency；
- config/console variable/commandlet/Subsystem/Module lifecycle hints；
- platform/RHI/render thread/task graph 等已验证模式；
- 项目维护路径和引擎版本 identity。

Pack 不把模式匹配当成事实。所有解释必须回到实际源码/构建配置；无法确定的引擎约定标记 inference。

### 7.4 Asset/Blueprint

MVP 不解析二进制资产。若知识问题依赖 Blueprint/asset：

- 标记 evidence gap；
- 允许用户提供官方导出文本/资产注册表/命令行报告作为独立 Source；
- 后续 Asset Pack 必须独立设计版本、引擎执行、安全和 Evidence；
- 不让模型从 C++ 猜测资产配置后写成事实。

## 8. AgentExecutor

### 8.1 与 ModelExecutor 区分

ModelExecutor 处理一个结构化请求；AgentExecutor 可在受控范围内进行多步读取/检索/验证。AgentExecutor 仍由 Core 协调，不等于授予 Codex 任意本机权限。

### 8.2 ExecutionGrant

```json
{
  "grant_id": "eg_...",
  "repo_revision_id": "sr_...",
  "allowed_tools": ["repo_tree", "code_search", "code_read", "symbol_lookup", "git_diff", "evidence_record"],
  "scope": {},
  "network": "deny",
  "write": "deny",
  "shell": "deny",
  "max_steps": 60,
  "max_wall_time_seconds": 1800,
  "max_model_budget": "...",
  "expires_at": "..."
}
```

默认无 shell、无写、无网络、无构建。若某专项需要执行测试/工具，必须使用另一个明确的 ToolExecutionGrant，命令 allowlist、工作目录、超时、输出和副作用均受控；它不是 MVP 默认。

### 8.3 工具

- `repo_tree(scope, cursor)`；
- `code_search(query, scope, mode)`；
- `code_read(file_id, revision, line_range)`；
- `symbol_lookup(symbol_id/name)`；
- `references(symbol_id)`（能力允许）；
- `git_diff(base, head, path scope)`；
- `build_metadata_read(id)`；
- `evidence_record(locator, claim_key)`。

不提供 arbitrary file、shell、network、edit、commit、credential 工具。

### 8.4 Agent 运行记录

记录结构化 step 摘要、tool input IDs、result hashes、成本和最终 evidence graph。默认不把完整 chain-of-thought 或 Provider raw 写入 Bundle；保存可审计的事实操作与输出引用即可。

## 9. 多阶段知识生产

### 9.1 Phase A：Scope map

确定模块边界、build metadata、核心目录/类型候选、依赖和未知问题。输出结构化 `ScopeMap`，不写最终文章。

### 9.2 Phase B：Evidence collection

按文档目标建立问题清单：职责、入口、生命周期、数据、线程、配置、失败、性能、平台、测试。Agent 对每项收集 file/line/symbol Evidence 和 confidence。

### 9.3 Phase C：Architecture synthesis

基于 Evidence graph 生成全局大纲和章节草稿。禁止引用未在 graph 中出现的源码事实；通用背景知识标记为 context/inference。

### 9.4 Phase D：Independent verification

第二遍 verifier 不负责“润色”，而是：

- 检查每个核心 claim 的 Evidence；
- 回读实际源码范围；
- 查找相反证据/漏掉的分支；
- 验证调用方向、线程、所有权、平台条件；
- 标记 unsupported/ambiguous；
- 只要求修复不合格章节。

可以使用相同模型的独立上下文，或不同模型/规则；关键是输入和角色分离，不把一次生成自评当证据。

### 9.5 Phase E：Compile

统一术语、章节、图示（若必要）、源码引用、限制和维护指南，生成 Draft + Quality。图示来自已验证结构关系，不做装饰性大图。

## 10. Evidence

```json
{
  "locator_kind": "code-file-line",
  "locator": {
    "repo_id": "repo_...",
    "commit": "<full-id>",
    "path": "Engine/Source/.../File.cpp",
    "start_line": 120,
    "end_line": 151,
    "symbol": "FExample::Initialize",
    "content_hash": "sha256:..."
  },
  "confidence": {"level": "high", "basis": "source-at-fixed-revision"}
}
```

规则：

- line range 尽量紧；
- content hash 验证 locator 对应内容；
- symbol 是辅助，不替代 range；
- generated/vendor/third-party 必须标明所有权；
- 对 diff 使用 base/head + hunk/file Evidence；
- 对运行时行为的推论必须标 `inference`，除非有测试/trace/log Source；
- 发布文档引用可以用稳定的 repo URL template，但 Bundle 内 Evidence 不依赖远端 URL 可用。

## 11. Artifact

| Kind | 内容 |
|---|---|
| `source-metadata` | repo/revision/scope/engine version |
| `code/file-manifest` | 固定 revision 文件/hash/language |
| `code/build-model` | module/target/dependency/compile metadata |
| `code/symbol-index` | 符号/引用/能力等级 |
| `code/change-set` | base/head diff（若适用） |
| `code/scope-map` | 初步范围图 |
| `code/evidence-graph` | claim -> Evidence |
| `agent/execution-receipt` | grant/tool/result hash/成本/身份 |
| `draft` | module briefing/deep analysis/change digest |
| `quality-report` | evidence/coverage/consistency |
| `receipt` | parser/indexer/Agent/compiler identity |

索引可以很大；Portable Bundle 可采用 manifest + content-addressed artifact/外部可重建策略，必须服从已发布 schema。不得因方便把整个 `.git` 或源码仓库复制进 Bundle。

## 12. 增量更新

### 12.1 变化检测

新 revision 到来时计算：

- changed files/hunks/symbols；
- build/module dependency changes；
- public header/interface changes；
- callers/callees/配置/测试影响；
- 原文档 Evidence 是否仍解析到相同内容；
- 受影响章节。

### 12.2 更新策略

- 不受影响且 Evidence hash 有效的章节可复用；
- 受影响章节重新取证和生成；
- 全局摘要/术语/大纲做一致性检查；
- 用户对已发布文档的编辑不被覆盖；
- 新 Draft 以旧 published/draft 为 base，进入 Review diff；
- 不以“只改了一个文件”假设影响局部，依赖图/接口变化需扩张 scope。

### 12.3 失效

parser/indexer/Recipe/Prompt 的语义版本改变时，可能需要重新验证索引或文档。失效规则进入 Receipt，不按日期猜测。

## 13. Quality Gate

确定性：

- repo/revision/scope 完整；
- Evidence path/range/hash 在固定 revision 有效；
- 每个核心章节 Evidence 数量/覆盖；
- 无越权路径/文件；
- 唯一 H1、标题层级、重复章节；
- 引用定义完整；
- Draft kind 模板必需部分；
- Agent grant/receipt 完整。

语义/专业：

- 职责与边界不是文件罗列；
- 执行时序和所有权有源码支持；
- 线程/并发/生命周期声明有证据或明确 inference；
- build/config/platform 条件被考虑；
- 相反证据和限制披露；
- 不引用不存在 API/类型；
- change digest 与实际 diff 一致；
- UE5 引擎惯例不覆盖项目实际实现；
- 文档可用于实际定位和维护。

发布 eligibility 要求无 unresolved critical claim；低置信项可保留为“待验证”，不能写成确定事实。

## 14. CLI

```text
alltonote produce codebase \
  --repo <repo-grant-or-catalog-id> \
  --revision <commit-or-ref> \
  --scope <module/path/symbol> \
  --draft module-briefing \
  --workspace <ref>

alltonote produce codebase --repo ... --base <commit> --head <commit> \
  --draft change-digest

options:
  --include-dirty
  --profile fast|balanced|thorough
  --agent-profile <ref>
  --output-language zh-CN|source
  --json
```

`--revision` 解析后输出固定 full commit；dirty 模式必须在 plan/result 中醒目标记。

## 15. 安全

- repo 中的 AGENTS.md/README/注释/Prompt injection 只是被分析内容，不能改变 ExecutionGrant；
- 默认不执行代码、构建、测试、Git hook、submodule fetch 或 package manager；
- 不读取 `.env`、credential、用户 home 或 scope 外路径；敏感文件 pattern 可在 preflight 阻止；
- Git object/path/symlink/submodule 进行 containment；
- Agent 工具使用 opaque IDs，不接受任意路径；
- model context 只包含所需代码范围；
- 远端模型上传源码需要用户/组织明确 policy；默认可选择本地 Codex/模型；
- 输出做 secret scanning 和 license/third-party 标记；
- Pack 签名/version 固定；
- Recipe 无写 repo/publish 权限。

## 16. 性能与规模

- 先 inventory/范围缩减，再建深索引；
- symbol/text index content-addressed，按 commit/file hash 增量；
- 不默认对全仓库生成 embedding；
- 大文件/生成代码/二进制/第三方按策略排除；
- Agent 每步使用检索 shortlist，避免把全库塞上下文；
- plan 显示文件数、字节、语言、是否有编译数据库、预计 Agent step/成本；
- 取消和每阶段 checkpoint；
- 同 repo/revision/scope 重启不重建有效索引或重复 Agent operation；
- UE5 全引擎级任务需要独立高预算/时间确认。

## 17. 测试矩阵

### 17.1 Repo

- clean/dirty/untracked；
- branch/ref 解析；
- submodule/LFS；
- symlink/path/Unicode/case；
- shallow/missing object；
- 无 remote；
- large monorepo；
- multi-language；
- compile_commands 有/无。

### 17.2 Agent

- 尝试 shell/write/network 越权；
- repo Prompt injection；
- max steps/time/budget；
- Provider failure/outcome unknown；
- citation fabrication；
- verifier 找到相反证据；
- restart 不重复已持久化 tool/model result；
- Agent identity drift 阻止恢复。

### 17.3 UE5

- Engine module、Plugin module、Game module；
- Build.cs/Target.cs 依赖；
- UObject/Subsystem/Module lifecycle；
- platform conditional/RHI/render thread；
- 缺 engine source/compile database；
- asset-dependent gap；
- generated headers 排除/定位。

### 17.4 真实验收

1. 小型公开仓库 module briefing；
2. 中型 C++ 仓库 deep analysis；
3. 实际 UE5 模块 briefing；
4. 固定 commit 的每条引用复核；
5. commit range change digest；
6. 新 commit 增量更新，仅重做受影响章节；
7. repo -> Bundle -> iwiki -> ReviewCandidate；
8. CLI JSON/错误；
9. 恶意 repo 安全测试；
10. 仓库 hash/worktree 前后不变。

## 18. 分期

### Phase C0：通用 Repo/Evidence 基础

repo/revision/scope、file manifest、read/search、file-line Evidence；无 Agent 写权限。

### Phase C1：Module Briefing Prototype

小/中型仓库，受限 AgentExecutor，多阶段 evidence/synthesis/verify。

### Phase C2：UE5 Pack

Build.cs/Target/module/UObject/引擎版本，真实 UE5 模块验收。

### Phase C3：Deep Analysis

并发/生命周期/平台/性能专项、更严格 verifier 和专业评测集。

### Phase C4：Incremental/Change Digest

commit range、影响分析、章节失效和 Review diff。

### Phase C5：受控执行证据（可选）

只有真实需要后设计 build/test/trace ToolExecutionGrant；不在只读 MVP 中夹带。

## 19. 完成定义

1. 任何文档绑定明确 repo/revision/scope；
2. dirty 状态不被静默混入；
3. 重要 claim 有可复核 commit/file/line/symbol Evidence；
4. Agent 默认无 shell/write/network；
5. 通用索引、AgentExecutor、UE5 Pack 分层；
6. 文档解释原理/时序/所有权，不是文件列表；
7. verifier 能发现 citation/推论/遗漏问题；
8. 新 revision 可增量生成 Draft 且不覆盖用户编辑；
9. repo 本身前后不变，恶意 repo 无法越权；
10. 真实通用 C++ 与 UE5 模块 E2E/专业质量评测通过。
