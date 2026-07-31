# Document 原生提取质量边界

日期：2026-07-31

结论：`alltonote.document-note@1` 当前能够生成安全、完整、可读的
born-digital PDF Draft，但它尚未执行知识重组、语义覆盖、事实一致性和引用
充分性等 Knowledge Note 质量 Gate。因此：

- 原生文本、逐页覆盖、parser completeness、page/bbox、源哈希与 Markdown
  safety 全部通过时，`quality_overall=pass`；
- Quality Report profile 为
  `alltonote.document-native-extraction@1`；
- Quality Report 明确记录
  `knowledge-note-quality=skipped / reason=not-evaluated`；
- `publish_eligible=false`，直到后续真正的 Knowledge Note 编译与质量 Gate
  给出独立通过结论；
- Job 仍然 `succeeded`，Draft、Evidence 与 Portable Bundle 仍然提交并可供
  人阅读，不把“暂不可自动发布”误表示为“解析失败”。

这是结果语义收紧，不是新工作流。没有新增 Recipe、DTO、schema version、
数据库迁移、模型调用、Review 状态或 CLI 分支。

## 兼容边界

旧 Bundle 和旧 Job result 是不可变历史事实，不进行回写。修复前由
Document native-extraction 检查产生的 `publish_eligible=true`，只能按旧实现
解释，不能再作为当前 Publisher 的可信准入依据。当前或未来 Publisher 必须
要求受支持的 Quality profile，并拒绝仅凭旧 Document flag 自动发布。

Video 的 Knowledge Note 有独立的编译与质量 Gate，本次修改不改变 Video
语义。Automation Protocol v1、`RecipeProduceResult`、Artifact 类型、ID、
foreground CLI 以及 Draft 阅读入口保持不变。

## 验证

- RED：在真实 Document Bundle 与 durable reopen 测试中新增语义断言后，
  原实现出现 4 个失败，证明此前仍返回 `publish_eligible=true`；
- GREEN：Document Bundle/runtime 聚焦测试 `14 passed`；
- Document/CLI/Portable 相关回归 `63 passed`；
- 完整 backend：
  `2170 passed, 2 skipped, 3 warnings, 3 subtests passed`；
- 独立只读审计结论：该 bounded native-extraction 语义为最小正确改动，无需
  新质量枚举、Recipe 版本、结果 DTO、数据库迁移或 Pipeline。

三个 warning 均为既有下载器正则或上游依赖弃用提示，不是本轮失败。
