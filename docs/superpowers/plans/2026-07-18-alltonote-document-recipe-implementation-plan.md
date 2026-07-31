# AllToNote PDF / PPT / OCR Document Recipe 实施计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-18-alltonote-document-recipe-design.md
  - ../specs/2026-07-18-alltonote-recipe-extension-contract-design.md
implementation_status: doc-00-and-doc-03-x0-b-passed-full-mvp-pending
last_verified_at: 2026-07-31
```

## 1. 成功标准

born-digital PDF、扫描/混合 PDF、PPTX Notes/表格/图片、100+ 页长文档均有真实验收；原生结构优先、局部 OCR/渲染、page/slide Evidence；原文件不变；重启不重复 OCR/模型；恶意文档隔离；Bundle/Review/Publisher闭环。Document 作为 Video 后的第二个真实 Recipe，必须复用同一 ProduceService/Registry/Job/Bundle，不建立 PDF 专用应用壳。

### 1.1 前置 Recipe X0-A Gate

任何 Document/PPT 生产代码开始前，只完成实现工作树中以下当前可执行文档定义的 X0-A：

- `docs/design-docs/backend/recipe-x0-compatibility-extraction/spec.md`；
- `docs/design-docs/backend/recipe-x0-compatibility-extraction/tasks.md`。

X0-A 仅包含最小 submission DTO、静态 Registry、薄 ProduceService、Video Adapter、SDK/Runtime 与单一 generic `produce` 兼容路由。不得在 X0-A 实现 `add`、独立 `run`、完整 Preflight/Plan/Output/Result DTO、JobSnapshot/result codec/Repository 泛化、atomic commit 抽取、schema migration、Engine 或 generic reconnect。Document 工具 Spike 与真实 fixture 准备可并行，但不能先创建独立 DocumentService、CLI、JobStore 或 commit 路径规避 Gate。

### 1.2 首个真实纵切与 X0-B 联合 Gate

X0-A 完成后，首个生产纵切只选择一份真实本地 born-digital PDF 或一份真实本地原生 PPTX。该纵切必须真实完成文件 hash、原生提取、page/slide Evidence、可用 Markdown Draft、durable Job/result、Artifact/portable commit 和 crash/reopen；测试可使用 deterministic fake model，但 Fake Recipe、纯内存 Repository 或 fake-only fixture 不构成验收。

该纵切与 X0-B 同步完成：legacy Video result dual-read；可重复、可重开的 schema migration 与 rollback oracle；source identity、Artifact/portable commit、result、Job terminal transition 的原子协议；按 persisted Recipe/Pack/Runtime identity resolve 的 generic reconnect；旧成功/未完成 Job 查询与恢复；两 Recipe 故障恢复不重复昂贵副作用。不得同时实现 PDF+PPTX、OCR、Vision、URL、旧格式或长文档全矩阵。

## 2. Task DOC-00：格式/工具 Spike

在独立临时 fixture 对候选 PDF/PPTX parser、renderer、OCR 工具做：结构覆盖、bbox、Notes、chart/table、中文、许可证、二进制大小、Windows/macOS支持、安全历史、启动性能。输出 decision matrix；不要先把 Tika/LibreOffice变成硬依赖。

状态：**PASS（2026-07-30）**。唯一真实输入、对照指标、Windows 依赖缺口与选择记录见 `docs/design-docs/document-born-digital-pdf-x0b/`。首个 PDF 纵切选择 `docling-slim==2.117.0` 作为 `document-basic` 的可替换隔离解析引擎；AllToNote 继续拥有 parser-neutral DTO、source SHA-256、Evidence 与 durable schema。轻量 `pdfplumber==0.11.8` 只保留为比较基线。

实用硬化状态：**PASS（2026-07-31）**。`document-basic` 已固定 Docling/SciPy/CPU Torch/OpenCV/NumPy、layout 与 TableFormer revision/权重；Document Runtime 在写 machine state 前执行离线 doctor，解析 worker 不再无界收集 stdout/stderr。第二份 6 页中文、8 表格真实 PDF 关闭了 Windows 中文路径和表格结构缺口，Pack 升级为 `docling-2.117.0-tableformer-v2.3.0`；原英文论文回归通过。当前没有实现通用 Pack 安装器或 repair，也不因此扩展 PPTX/OCR。

## 3. Task DOC-01：文件身份与安全探测

实现 local root grant、stable copy/handle、file hash、magic/MIME、ZIP ratio/object/page/slide limits、encrypted/damaged/active content。测试编译中修改、symlink/reparse、密码不进argv/log。

当前增量（2026-07-31）：`alltonote.document-note@1` 已在全文件 SHA-256 和 Job 创建前拒绝非常规文件、非 `.pdf`、错误 `%PDF-` magic、少于 5 bytes 或超过 64 MiB 的输入；Recipe Adapter 与 Docling worker 共用同一上限。该增量只关闭明显不支持输入造成的无谓全文件读取，不代表恶意路径竞态、PDF 对象/页面/解压膨胀或解析器资源隔离已完成。验证边界见 [`Document PDF 输入准入边界`](../../acceptance/2026-07-31-document-input-admission.md)。

## 4. Task DOC-02：Source Copy Policy

`copy|reference|copy-if-under-limit`、hash/metadata、超限交互、source unavailable。原文件前后 hash测试；不复制整个用户目录。

## 5. Task DOC-03：首个真实原生切片与 X0-B

状态：**PASS（2026-07-30）**。正式链路通过 generic `produce` 完成 4 页/111 块解析、page/bbox Evidence、五类 Artifact、Portable commit、重开与故障原子性；X0-B 的 result discriminator、Artifact role、execution binding、dual-read migration、generic atomic commit 和 exact reconnect 已由 Video + Document 两个消费者共同验证。完整数据见 `docs/design-docs/document-born-digital-pdf-x0b/report.md`。

从小型 born-digital PDF 或原生 PPTX 中只选一种：使用真实 parser 逐页/slide 提取原生文本与最小结构，生成 file hash、page/slide Evidence、可用 Markdown Draft 和 Artifact manifest，并经 X0-A 的 generic `produce` 进入 durable Job。

本轮已固定 PDF `SA2023_RealTimeReflection.pdf`（4 页，SHA-256 `155f56096e8196b08f0aab9d6a162daea0196d308ad323ab1aebc7fb749db6b1`），不得替换为 PPTX 或 fake fixture。Docling 必须由可选固定 Pack/worker 离线调用；未固定模型或依赖不完整时 fail closed。

同时只抽取 Video 与该消费者共同需要的数据面合同：result discriminator/schema version、durable result query、Artifact reference/manifest、atomic commit 边界和 generic JobExecutionRuntimeFactory。page/slide/bbox/Notes 保留在 Document Artifact/Evidence。故障 Gate 必须覆盖 legacy dual-read、旧成功/未完成 Job、可重复/可重开 migration、rollback oracle、原子终态提交、persisted identity 重连和 crash/reopen 零重复。

Stop：第二消费者只是 Fake；需要两种格式或 OCR 才能证明；common 字段只有一个消费者；migration 无旧库 fixture；commit 可观察半提交；generic wait 仍硬编码 Video factory；Worker 需要重新 submit 创建 Job。

## 6. Task DOC-04：PDF Normalized Blocks/Reading Order（选择 PDF 纵切时最小实现；否则后续）

heading/paragraph/list/table/figure/footnote；多栏/页眉页脚/rotation；basis/confidence。先确定性 heuristic，layout model可选。低置信不静默。

## 7. Task DOC-05：Page/BBox Evidence（选择 PDF 纵切时最小实现；否则后续）

file hash + page + bbox + block hash；range reader/preview overlay；测试重复文本、rotation/crop、parser version、无 bbox降级。

## 8. Task DOC-06：短/中 PDF Compiler

Knowledge Note + deterministic Quality；来源语言/中文；section-aware inputs；表格/figure引用；没有可用文本时先要求 OCR，不让模型猜。

当前增量（2026-07-31）：Compiler 已生成结构化 Knowledge Note 与独立 Knowledge Map；第二个可恢复的结构化 stage 会逐 claim、仅基于其引用 block 给出 model review，并绑定 compiled/source/parser、block 声明 hash 与实际文本 hash。产品 Runtime 支持可选 `default_verifier_provider_profile`：未配置时保持 schema v2 同模型 advisory，固定为 `same-model-review-not-independent`、`quality.overall=fail`、`publish_eligible=false`；配置不同 frozen model identity 时生成 schema v3，分别持久化并重连 composer/verifier profile 与 model，只有独立 review 和既有 extraction/coverage Gate 全部通过才恢复自动发布资格。当前“独立”只证明 model identity 不同，不扩大为供应商或统计独立；CLI Automation Protocol 仍为 v1。Windows `spawn` 集成回归已证明在 compose/verify 两条成功调用落盘后进程退出，SQLite reopen 与新 fencing Attempt 会复用原 operation/result/checkpoint，禁止调用的 executor 为零调用并最终只提交一次 Portable Bundle；该结果证明本地零重放，不替代外部供应商可用性验收。验证边界见 [`Document Knowledge Note 语义质量边界`](../../acceptance/2026-07-31-document-semantic-quality-boundary.md)。

## 9. Task DOC-07：局部 OCR Pack

manifest/probe/languages；只处理 classified pages/regions；render DPI/rotation/deskew；text+bbox+confidence；与 native去重；per-page checkpoint；OCR低置信/语言错误；不覆盖原PDF。

## 10. Task DOC-08：长 PDF map/compose

outline/heading/page-aware chunk -> knowledge map -> global compose -> citation freeze -> quality/repair。100+页测试调用数/覆盖/内存/恢复；不是逐页摘要拼接。

## 11. Task DOC-09：PPTX Native Extract（选择 PPTX 纵切时最小实现；否则后续）

slide/title/placeholders/text runs/Notes/shapes/groups/z-order/table/chart data/images/hyperlinks/layout metadata。输出 slide blocks、shape IDs/bbox和 warnings。

## 12. Task DOC-10：PPT Evidence/Compiler（选择 PPTX 纵切时最小实现；否则后续）

slide/shape/bbox/notes Evidence；按章节/sequence map/compose；区分 visible content 与 Notes；图表/SmartArt缺口披露；真实含 Notes PPTX验收。

## 13. Task DOC-11：选择性 Render/Vision

先做 render Pack/isolated profile/timeout/hash，再根据 inventory选择 diagram/chart/image-heavy slide/page。视觉输出标 derived并引用render/native blocks。未选择 thorough/无缺口时零视觉调用。

## 14. Task DOC-12：后续 CLI/Bundle 扩面

X0-B 首个纵切只复用 X0-A generic `produce` 与既有 envelope，不新增 `add` 或独立 `run`。X0-B 稳定后，才可评估 `produce document`、URL、OCR 选项和其他别名；若开放，必须翻译为同一 ProduceRequest，不复制 CLI envelope、Job、Bundle、Publisher 或 Pipeline。

## 15. Task DOC-13：恶意文档/隔离

PDF JS/embedded file、Office macro/OLE/external relationship、ZIP bomb、图片像素bomb、parser crash/hang、LibreOffice profile污染、OCR/renderer输出异常、Prompt injection、Secret/PII。worker kill tree/配额/日志脱敏。

## 16. Task DOC-14：故障恢复矩阵

在 inventory/native/page OCR/render/model map/compose/Artifact/portable commit/iwiki 每个边界 kill。首个纵切先验证 native extract、result query、atomic commit 和 generic reconnect；验证相同 file hash 不重复有效提取/模型/commit，legacy Video result dual-read，旧成功与旧未完成 Job 可查询/恢复，migration 可重复且可重开，Pack/Runtime identity 固定，outcome unknown 可 reconcile。OCR/视觉边界只在后续对应能力实现后加入。

## 17. Task DOC-15：真实质量/性能

基线分别记录：20页born-digital、中文扫描、混合多栏、100+页、50 slide PPTX。人工检查事实/结构/table/chart/notes/Evidence/未处理unit。记录时间、内存、磁盘、OCR/vision/model calls。

## 18. Task DOC-16：Review/Publisher

PDF/PPT Draft -> Candidate -> 点击页/bbox或slide/shape -> edit/approve -> personal publish；source reference移动/不可用提示；原文件无修改。

## 19. Task DOC-17：旧格式/Collection（后续）

单文件稳定后才做：`.ppt`隔离 conversion、工具/version/hash/warnings；多文件 collection先manifest/预算、每文件独立revision。不要让 Tika“能抽文本”直接升级为正式格式支持。

## 20. Task DOC-18：验收与交接

输出 `docs/acceptance/document-recipe-v1.md`；更新 `RECIPE-DOC-01` 和 Recipe contract反馈。记录 Video + Document 的字段消费者矩阵，但仍标记为 internal candidate，不在 Article/Wiki 验证前冻结 v1。声明支持必须按 PDF/PPTX/扫描/视觉具体矩阵，不能笼统写“所有文档”。

## 21. 执行顺序

```text
implementation-worktree X0-A spec/tasks
 -> DOC-00（可与 X0-A 并行准备真实 fixture）
 -> DOC-01/02（仅首个纵切所需）
 -> DOC-03：选择一个真实 PDF 或 PPTX + 联合完成 X0-B
 -> dual-read / migration / atomicity / generic reconnect / recovery Gate
 -> 若首切为 PDF：DOC-04..06；若首切为 PPTX：DOC-09/10
 -> DOC-07/08/11（后续 OCR、长文档、视觉）
 -> DOC-12..16（后续产品扩面与完整矩阵）
 -> DOC-17（后续）
 -> DOC-18
```

首个 X0-B Gate 通过前，不得并行启动 PDF 与 PPTX 两条生产实现，也不得用 Fake Recipe、全格式声明或 OCR demo 替代真实 durable/commit/recovery 证据。
