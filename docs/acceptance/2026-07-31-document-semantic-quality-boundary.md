# Document Knowledge Note 语义质量边界

日期：2026-07-31

## 结论

当前 `alltonote.document-note@1` 的 Knowledge Compiler 已能生成干净的阅读
Draft，并要求每个生成条目引用真实存在的 `source_block_ids`。Bundle Gate
还会计算被引用正文 bytes 和页面覆盖率。这些检查证明引用可解析、来源有
覆盖，但不能证明生成主张被所引用的原文语义支持。

因此，在独立语义验证完成前，编译后的 Document Knowledge Note 采用如下
保守合同：

- Job 可以 `succeeded`，Draft、Knowledge Map、Evidence 和其他 Artifact
  仍会原子提交；
- `source-coverage` 继续独立报告 `pass|fail`；
- `knowledge-note-quality=skipped`，原因是 `semantic-not-evaluated`；
- `quality.overall=fail` 且 `publish_eligible=false`；
- Publisher 不得把“引用 ID 存在”或“覆盖率达标”解释为事实一致性通过。

## 回归证明

现有集成样本故意让编译结果写出“来源支持主要结论”，同时只提供标题、
问题和方法等普通原文 block。旧实现仅凭 block ID 与覆盖率会把它判为
`knowledge-note-quality=pass / publish_eligible=true`；当前回归要求它保留
可读 Draft 和 Knowledge Map，但必须 `semantic-not-evaluated / fail / false`。
低覆盖样本同时保留 `source-coverage=fail`，避免语义未知掩盖确定性的覆盖
缺口。

## 未完成边界

本轮没有引入关键词重叠、字符串包含或“模型自称已核验”等伪语义 Gate。
后续可信发布需要独立于生成响应的 claim-level 验证结果，绑定 claim、来源
block、模型/规则身份和可恢复执行记录；只有该结果通过后，才可重新开放
`knowledge-note-quality=pass` 与自动发布资格。

## 后续实现状态

当前代码已加入第二个结构化 claim review stage：逐条 claim 只携带其引用的
block，并把 verdict 绑定到 compiled note digest、source/parser identity 和
被引用 block 的声明 hash 与实际文本 hash；生成与复核使用不同
ModelCallCoordinator shard，可分别
恢复而不重复成功的付费调用。Quality Artifact 记录 `method=model`，Knowledge
Map 保存逐 claim verdict。

正式 Runtime 当前仍使用同一个 frozen model binding 完成生成与复核。这种
复核只作为 advisory：即使全部返回 `supported`，仍记录
`same-model-review-not-independent`，保持 `quality.overall=fail` 与
`publish_eligible=false`。只有显式配置不同 verifier identity 的内部合同测试
会证明组合 Gate 可以恢复发布；产品 Runtime 尚未提供独立 verifier binding，
因此没有静默放宽当前发布边界。
