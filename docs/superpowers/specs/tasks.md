# AllToNote 长视频知识编译实施任务清单

```yaml
doc_type: tasks
status: active
authority: execution
upstream:
  - 2026-07-16-alltonote-long-video-knowledge-compilation-design.md
downstream:
  - ../../acceptance/2026-07-18-long-video-knowledge-compilation-v2.md
implementation_status: tasks-1-11-and-13-15-completed-task-12-live-acquisition-blocked
last_verified_at: 2026-07-18
```

> 来源规格：`2026-07-16-alltonote-long-video-knowledge-compilation-design.md`
> 实现工作树：`G:\AllToNote-video-producer`
> 实现分支：`codex/alltonote-video-producer`
> 任务总数：15
> 原则：每个任务目标代码改动小于约 200 行；接近上限时继续拆分；完成一个任务后停止并等待 Review。

## 已冻结的实现假设

- v1 request、Job、Bundle 和 CLI 投影保持现有语义，不迁移、不重写。
- 先交付真实可用的 Knowledge Note v2 balanced 路径，再实现多 Draft 与 Faithful Edition。
- 第一阶段保持当前单 Runtime 单活动 Job 语义；只在单个模型 fan-out 阶段引入受限并发，不复用 Legacy `task_serial_executor` 作为 Core 调度器。
- 模型 Adapter 只执行一次 Provider 请求；重试、结果持久化、outcome unknown 和取消由 Application Coordinator 拥有。
- Portable 多 Draft 必须通过真实 iwiki 合同；不能只修改 AllToNote 私有 manifest 解释。
- 第一版 Faithful Edition 只实现 balanced、默认保留来源语言、不生成截图；中文翻译必须显式选择。

## 阶段 A：v2 请求与模型执行底座

### Task 1: [Completed] 冻结 v2 多输出请求合同并保持 v1 默认不变

- 文件：`backend/app/core/domain/video.py`、`backend/tests/core/test_domain_contracts.py`
- 说明：新增 `VideoDocumentKind`、`FaithfulLanguagePolicy` 和 v2 `requested_outputs` 规范化/校验；v1 构造与默认知识笔记语义保持不变。此任务不开放 CLI v2，也不改变 VideoService preflight。
- 验收标准：v1 现有测试通过；v2 默认/多输出/重复/空集合/未知输出/翻译语言组合具有精确测试；无执行路径把 v2 静默当成 v1。

### Task 2: [Completed] 持久化和恢复规范化 v2 Job Request

- 文件：`backend/app/core/application/video_service.py`、对应 Job/恢复测试
- 说明：让 request serialization、严格字段集合和 request hash 支持规范化 v2 输出与语言策略；继续读取 v1 Job。
- 验收标准：v1 request hash/golden 行为不变；v2 相同集合不同输入顺序得到相同 hash；恢复不丢失 Recipe Binding 或语言策略。

### Task 3: [Completed] 新增单次模型执行合同

- 文件：`backend/app/core/ports/model_executor.py`、`backend/tests/core/test_model_executor_contract.py`
- 说明：实现冻结 Binding、Provider-independent request/result 和取消端口；不包含 Video、Bundle、Tool 或 Provider raw。
- 验收标准：字段/边界/模型身份/敏感值规则测试通过；导入 Core 不加载 Provider SDK 或 Web 重依赖。

### Task 4: [Completed] 实现可恢复的 ModelCallCoordinator 最小闭环

- 文件：`backend/app/core/application/model_call_coordinator.py`、模型 operation result store、对应单元测试
- 说明：实现 request hash、成功结果复用、ExternalOperation、结果先持久化后成功、已知失败有限重试和 outcome unknown；第一版同步执行，不引入并发池。
- 验收标准：成功恢复零重复调用；未知结果不自动重放；Auth/合同错误不重试；取消和 fencing 拒绝迟到写入。

## 阶段 B：Knowledge Note v2 balanced

### Task 5: [Completed] 实现 Transcript Quality Assessment 与 v2 Chunk Plan

- 文件：`backend/app/core/recipes/video/compilation/contracts.py`、`pipeline.py`、对应纯逻辑测试
- 说明：实现真实 Transcript hash、质量评估、token/字节安全预算和 deterministic chunk refs；不复制整份 Transcript 到计划。
- 验收标准：短/长/超长、未知时长、乱序/重复/空洞输入具有测试；规划为线性复杂度；相同输入得到相同计划。

### Task 6: [Completed] 实现 Knowledge Map 合同、Parser 与稳定内部 ID

- 文件：`backend/app/core/recipes/video/compilation/contracts.py`、`pipeline.py`、对应测试
- 说明：实现最小 Knowledge Item taxonomy、segment 允许集合、Core ID 分配和有界 JSON Parser。
- 验收标准：未知/重复 segment、模型伪造可信 ID、超预算数组/文本被拒绝；同一成功响应重复解析产生相同内部 ID。

### Task 7: [Completed] 实现 balanced Map-Compose Compiler

- 文件：`backend/app/core/application/video_compiler.py`、Recipe prompt/parser、集成测试
- 说明：长 Transcript 执行 Knowledge Map 后 Global Composer，短 Transcript 使用安全 direct composer；使用真实 Coordinator 加 Deterministic ModelExecutor 测试。
- 验收标准：生成唯一文章而不是拼接多个 Markdown；正常 balanced 最多两个顺序模型波次；结果按 chunk ordinal 稳定；不把 IR 暴露给 VideoService。

### Task 8: [Completed] 实现知识笔记文本 Gate 与一次定向修复

- 文件：`backend/app/core/recipes/video/compilation/quality.py`、共享 Markdown 分析、对应测试
- 说明：检查唯一 H1、标题层级、重复标题、segment citation、实质 H2、coverage ledger 和安全；最多修复一次失败范围。
- 验收标准：已知 3 H1/重复 H2 回归用例失败；合法文章通过；修复不重新执行 Knowledge Map；失败后不伪装 PASS。

### Task 9: [Completed] 将 Knowledge Note v2 接入 VideoService/Runtime

- 文件：`backend/app/core/application/video_service.py`、`backend/app/runtime.py`、checkpoint codecs、对应恢复测试
- 说明：在 v2 请求下调用真实 Compiler/Coordinator，v1 继续走 Legacy 路径；接入稳定阶段 checkpoint、usage 和安全摘要。
- 验收标准：v1 golden 全通过；v2 中断可从计划/Map/Composer/Repair 最近合法状态恢复；成功付费调用不因普通恢复重放。

### Task 10: [Completed] 暴露 v2 Knowledge Note CLI 与结果摘要

- 文件：`backend/app/cli/main.py`、SDK/result projection、CLI 测试
- 说明：增加 `--quality`、v2 Recipe 选择和可选 compilation 摘要；默认命令行为在发布 Gate 前仍按兼容策略冻结。
- 验收标准：JSON stdout 仍为单 envelope；旧 primary 字段保留；未知字段兼容规则有测试；错误码稳定且不泄漏路径/Prompt/Secret。

## 阶段 C：真实 Provider 与性能/恢复验收

### Task 11: [Completed] 接入 Legacy/OpenAI-compatible 单次 ModelExecutor Adapter

- 文件：`backend/app/adapters/models/legacy_model_executor.py`、Runtime 装配、Adapter 测试
- 说明：复用现有 Provider 能力但每次只执行一个冻结请求；移除 v2 对 Legacy 分块/拼接路径的依赖。
- 验收标准：Adapter 一次调用只产生一次 Provider request；模型身份不符失败；timeout/错误分类正确；不拥有重试或长视频编排。

### Task 12: [Blocked: Live YouTube Acquisition] 完成 65 分钟 YouTube Knowledge Note v2 端到端验收

- 文件：集成/验收测试与安全的本机验收记录；仅在缺陷明确时修改对应实现
- 说明：使用现有 YouTube 测试视频和可用 Cookie/Provider，验证下载/字幕或转写、v2 balanced、Quality、Bundle 和 iwiki commit。
- 验收标准：一个 H1、无分块拼接痕迹、每个实质 H2 Evidence 合法、Quality/commit 通过；记录调用数、顺序波次、耗时和恢复行为。
- 2026-07-16 验收记录：使用该视频已获取的真实 1494 段、3934.55 秒平台字幕完成 acquisition 后全链路验收；7 次 Knowledge Map + 1 次 Global Compose，共 2 个顺序波次、无 Repair，568.4 秒完成。最终 Markdown 为 1 个 H1、9 个 H2、无重复 H2；87 次正文引用对应 84 个 Evidence，Portable Quality 13/13 通过，`publish_eligible=true`，真实 iwiki semantic validation/commit 通过。相同 Job 重启 1.4 秒完成，metadata/subtitle/model 均零重放。
- 当前阻塞：从本机 IP 使用用户最新导出的 Cookie 实时访问 YouTube 时，yt-dlp 仍返回 `Sign in to confirm you're not a bot`；启用本机 Node.js runtime 后结果不变。因此“实时 YouTube URL 获取”尚不能宣称通过，不能将本 Task 标记 Completed。

## 阶段 D：Portable 多 Draft 与高保真精编稿

### Task 13: [Completed] 升级 Portable Video Bundle 为多 Draft 合同

- 文件：`backend/app/core/portable/bundle_assembler.py`、domain/result plan/checkpoint、iwiki 合同 fixtures、集成测试
- 说明：新增 `drafts[]`、逐 Draft Quality、documents 映射、primary 兼容投影和外层 `alltonote.video-producer@2`；先证明真实 iwiki 支持。
- 验收标准：v1 Bundle byte/golden 不变；v2 单/双 Draft 通过真实 iwiki validate/commit；未知 required contract fail closed；每个 Quality 绑定精确 Draft hash。

### Task 14: [Completed] 实现 Faithful Edition balanced Compiler 与保真 Gate

- 文件：`backend/app/core/recipes/video/faithful_edition/*`、`faithful_edition_compiler.py`、对应测试
- 说明：连续无重叠 section、paragraph 级 segment 映射、正文/总结/关键点/不确定项分离、preserve-source/显式翻译和一次局部修复。
- 验收标准：非排除 segment 正文覆盖闭合且顺序正确；数字/技术 token/限定词风险可报告；翻译稿明确标记；不生成截图、不输出虚假 fidelity score。

### Task 15: [Completed] 接入双输出并完成最终端到端与回归 Gate

- 文件：VideoService/Runtime/CLI/result projection、全量集成/恢复/性能/安全测试
- 说明：支持 `--output` 重复选择，Knowledge/Faithful 独立编译、逐 Draft Quality、同 Bundle 原子提交；第一版文档支线按当前单活动 Attempt 语义顺序执行，单阶段 shard 并发另行按确认的并发设计启用。
- 验收标准：单 Knowledge、单 Faithful、双输出、翻译型 Faithful、取消、恢复、outcome unknown 和 commit 对账全部通过；完整测试无 v1 回归；输出可被 Obsidian/Agent 直接读取。
