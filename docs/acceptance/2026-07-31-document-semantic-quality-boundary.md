# Document Knowledge Note 语义质量边界

日期：2026-07-31

## 结论

当前 `alltonote.document-note@1` 的 Knowledge Compiler 已能生成干净的阅读
Draft，并要求每个生成条目引用真实存在的 `source_block_ids`。Bundle Gate
还会计算被引用正文 bytes 和页面覆盖率。这些检查证明引用可解析、来源有
覆盖，但不能证明生成主张被所引用的原文语义支持。

因此，编译后的 Document Knowledge Note 采用如下保守合同：

- Job 可以 `succeeded`，Draft、Knowledge Map、Evidence 和其他 Artifact
  仍会原子提交；
- `source-coverage` 继续独立报告 `pass|fail`；
- 未配置独立 verifier 时，持久化 schema v2 请求并使用 composer 做同模型
  review；`knowledge-note-quality=skipped`，原因是
  `same-model-review-not-independent`，且 `quality.overall=fail`、
  `publish_eligible=false`；
- 配置 `default_verifier_provider_profile` 且其 frozen model identity 与 composer
  不同时，持久化 schema v3 请求；只有独立 review、extraction、source/page
  coverage 和全部 claim verdict 同时通过，才允许
  `quality.overall=pass` 与 `publish_eligible=true`；
- 标题与章节标题仍是必须引用来源、必须获得 `supported` 的 claim，但它们的
  角色是结构性标签：只要是所引 block 的忠实简洁概括且没有新增事实，就不
  要求逐字出现；overview、paragraph 和 key point 仍按每个实质性细节均由
  所引 block 蕴含的事实标准复核；
- Publisher 不得把“引用 ID 存在”或“覆盖率达标”解释为事实一致性通过。

## 回归证明

现有集成样本故意让编译结果写出“来源支持主要结论”，同时只提供标题、
问题和方法等普通原文 block。旧实现仅凭 block ID 与覆盖率会把它判为
`knowledge-note-quality=pass / publish_eligible=true`；当前回归要求它保留
可读 Draft 和 Knowledge Map，但必须
`same-model-review-not-independent / fail / false`。
低覆盖样本同时保留 `source-coverage=fail`，避免语义未知掩盖确定性的覆盖
缺口。

## 未完成边界

本轮没有引入关键词重叠、字符串包含或“模型自称已核验”等伪语义 Gate。
“独立 verifier”在当前合同中只表示 verifier 的 frozen/actual model identity
必须与 composer 不同；它不等价于供应商独立、训练数据独立或统计独立。

## 后续实现状态

当前代码已加入第二个结构化 claim review stage：逐条 claim 只携带其引用的
block，并把 verdict 绑定到 compiled note digest、source/parser identity 和
被引用 block 的声明 hash 与实际文本 hash；生成与复核使用不同
ModelCallCoordinator shard，可分别
恢复而不重复成功的付费调用。Quality Artifact 记录 `method=model`，Knowledge
Map 保存逐 claim verdict。

2026-08-02 的 V16 真实中文表格 PDF 失败证据进一步区分了结构标签与事实
claim：编写器现在要求 overview、paragraph 和 key point 的每个实质性细节都被
该条目的引用 block 支持，否则应删除该细节；验证请求显式携带
`structural-label` 或 `factual` 角色。该变化没有放宽 `passed` 条件，任何 claim 的 `unsupported`、
`insufficient-evidence` 或 `contradicted` 仍会使自动发布失败。

产品 Runtime 现在支持可选的独立 verifier binding。未配置时继续接受并生成
schema v2 请求，保持同模型 advisory；配置有效且不同的 verifier profile/model
时生成 schema v3 请求，并分别冻结 composer 与 verifier 的 profile/model。
重连从持久化请求恢复这四个选择，不读取新的模型选择覆盖旧 Job。CLI
Automation Protocol 仍为 v1，没有新增 Document 专用命令或 envelope。

当前集成回归已证明同一双 binding Runtime 会把 schema v2 review 路由回 composer，
而 schema v3 只调用独立 verifier。另一个 Windows `spawn` 回归会在 compose 与
verify 两条 external operation 及 result file 均成功持久化、candidate checkpoint
尚未写入时令子进程直接退出；父进程在 scheduler lease 过期后重新打开 SQLite，
以新 fencing token 接管 Attempt，并使用禁止调用的 executor 完成恢复。恢复前后
两条 operation、两份 model-result 文件及 parse checkpoint 的 bytes/mtime 均不变，
最终只产生一个 Portable commit。该证明覆盖本地持久化与零重放语义，不把 fixture
模型扩大解释为外部供应商可用性验收。
