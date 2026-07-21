# AllToNote 长视频知识编译设计

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-14-alltonote-video-producer-design.md
  - 2026-07-14-alltonote-portable-artifact-source-bundle-design.md
downstream:
  - ../plans/2026-07-18-alltonote-video-release-implementation-plan.md
implementation_status: knowledge-and-faithful-core-complete-live-youtube-acquisition-blocked
last_verified_at: 2026-07-19
```

- 日期：2026-07-16
- 状态：设计已确认；Knowledge Note v2 与 Faithful Edition 核心已实现并通过长视频缓存字幕 E2E
- 对应顶层可执行 Video Recipe：`alltonote.video-producer@2`；内部输出编译绑定：`alltonote.video-course-note@2`、`alltonote.video-faithful-edition@1`
- 上位设计：`2026-07-13-alltonote-knowledge-compiler-architecture-design.md`
- 数据合同：`2026-07-14-alltonote-portable-artifact-source-bundle-design.md`
- 下位基础：`2026-07-14-alltonote-video-producer-design.md`
- 产品基线：Headless-first、本地优先、CLI 一等入口、开放 Markdown、薄 Desktop、托管式独立 Runtime

## 1. 背景与问题

### 1.1 背景

AllToNote Video Recipe（历史名 Video Producer）已经能够把视频 URL 或本地视频转换为 Transcript、Evidence、Markdown Draft、Quality Report 和 Portable Source Bundle，并通过 iwiki 进行语义校验和原子提交。现有 Core、Job、Attempt、checkpoint、Evidence、Portable Bundle 和 CLI 边界继续有效。

长视频会超过单次安全模型请求的输入或输出预算，因此当前模型 Adapter 会按 Transcript segment 分块，为每个分块独立生成 Markdown，再把结果直接拼接。这个实现保证全部分块都能被处理，但每个分块都把自己当作一篇完整文章，最终容易出现多个 H1、重复章节、术语不一致、章节顺序机械和局部内容缺少 Evidence 等问题。

### 1.2 要解决的核心问题

本设计解决的不是单纯的模型上下文限制，而是长视频知识合成问题：

> 如何把多个 Transcript 分块编译为一篇结构完整、重要知识覆盖可检查、事实证据可追溯、适合人类学习和长期复用的 Markdown 文档。

目标管线为：

```text
Transcript 分块
  -> 每块提取结构化知识
  -> 全局知识汇总和去重
  -> 生成全局文章大纲
  -> 按大纲撰写整篇文章或章节
  -> 总编辑统一成一篇文章
  -> 确定性质量验证
  -> 只修复不合格部分
```

除知识笔记外，本设计同时允许用户明确请求独立的“高保真精编稿”。它以保留 Transcript 原始信息、时间顺序、限定条件、例子和表达演进为第一目标，只提高可读性；知识笔记仍以知识重组、压缩、去重和学习效率为目标。两者共享同一来源与执行基础设施，但不是同一种 Draft，也不共用同一套质量结论。

### 1.3 当前实现约束

当前实现中：

- `KnowledgeModelPort.generate()` 一次返回 `GeneratedVideoDraft`；
- `LegacyKnowledgeModelAdapter` 内部完成 Transcript 分块、逐块模型调用和 Markdown 拼接；
- Application 只能看到一个 `generate_draft` 逻辑步骤，无法独立 checkpoint 知识提取、大纲、章节写作和总编辑；
- Quality 模块已经表达最多一次修复，但生产调用尚未提供真实修复器；
- 当前确定性 Quality Gate 已覆盖 Evidence 完整性、Markdown 安全、引用闭合、实质 H2 Evidence 和占位符检查，但尚未覆盖唯一 H1、重复章节、知识项分配、术语一致性和全局结构。

### 1.4 设计前提

本设计不推倒现有 Video Producer。Source 获取、Transcript、Evidence、截图、Portable Bundle、iwiki commit、Job/Attempt、Credential 和 CLI 协议保持现有所有权；本设计只细化从 Transcript 到最终 Draft 和 Quality 的知识编译阶段。

## 2. 目标、非目标与成功标准

### 2.1 目标

1. 多 Transcript 分块必须生成一篇统一文章，不得把多个局部 Markdown 直接作为最终 Draft 拼接。
2. 每个局部分块先生成可校验的结构化知识，而不是自行决定全篇标题和章节结构。
3. 全局规划必须对重要知识进行汇总、去重、分配和覆盖追踪。
4. 最终文章必须有唯一 H1、合法标题层级、统一术语、清晰学习路径和有效 Evidence。
5. 总编辑只能重组、合并和改写已有知识，不得新增无证据事实。
6. 每个模型阶段必须可 checkpoint、可恢复、可统计 usage，并服从 ExternalOperation 的不确定结果规则。
7. 确定性结构与 Evidence Gate 和模型/人工语义评估必须分开表达，不虚假宣称语义质量可以被纯规则完全证明。
8. 短视频避免无必要的多阶段成本；长视频和高质量预设启用完整知识编译。
9. 编译语义属于共享 Core/Application Recipe，不锁在模型 Adapter、CLI、Desktop、MCP 或 EXE 中。
10. 最终 Draft 继续使用开放 GFM-compatible Markdown，可被 Obsidian、普通编辑器和任意 Agent 直接读取。
11. 原始 Transcript 始终作为不可变来源 Artifact 保存，高保真精编稿和知识笔记只能作为派生 Draft，不能覆盖 Transcript。
12. 用户可以独立请求知识笔记、高保真精编稿或二者；对外合同使用可扩展的输出集合，不引入 `both` 特殊枚举。
13. 高保真精编稿保持时间顺序和低压缩，只允许保守编辑；AI 章节总结与关键点必须和高保真正文明显分离。
14. 高保真精编稿与知识笔记分别编译、分别质量验收，并可作为多个 Draft 原子提交到同一个 Portable Source Bundle。
15. “高保真”必须明确其依据是官方字幕、上传者字幕或 AI 转写；仅基于 Transcript 的编辑不能被宣传为已经逐句核对原始音频。

### 2.2 非目标

本设计不实现：

- Desktop UI、Desktop 文件树或 Markdown 阅读器；
- MCP 工具和资源接口；
- Publisher、Draft approval 或写入 `wiki/personal`、`wiki/common`；
- Article、Wiki、PPT/PDF、UE5、代码库、Git 或 Personal Recipe；
- 任意 YAML DAG、可视化 Workflow 画布或通用 Agent 框架；
- 视频逐帧多模态理解；
- 云端正文数据库、账号、同步或远端任务队列；
- 由模型创建可信 Bundle、Artifact、Evidence 或 Source ID；
- 依赖 LLM Judge 替代确定性 Evidence 校验；
- 保证所有模型输出 bit-for-bit 可复现；
- 把 BiliNote 实现原样复制进 AllToNote。
- 用高保真精编稿替代原始 Transcript；
- 第一版对所有视频默认生成两份长文；
- 在未重新检查音频的情况下承诺精编稿与视频逐字一致；
- 为高保真精编稿建立第二套 Runtime、通用 Agent 框架或新的 Portable Bundle 体系。

### 2.3 成功标准

对已用于验收的 65 分钟 YouTube 视频，目标实现至少满足：

1. 下载、转写、Evidence、Bundle 和 iwiki commit 继续通过；
2. 最终 Markdown 恰好包含一个 H1；
3. 不存在分块文章拼接痕迹和重复 H2 标题；
4. 每个实质 H2 至少包含一个有效 Evidence；
5. 所有高重要性知识项被分配并声明覆盖，或者记录明确省略原因；
6. 最终文章术语一致，章节顺序服务于理解路径而非机械复制视频时间线；
7. Quality Gate 为 pass，或经过最多一次有边界的定向修复后为 pass；
8. 任一编译阶段中断后能够从最近合法 checkpoint 恢复；
9. 已确认成功的付费模型操作不会因普通恢复被无故重放；
10. 最终 Bundle 删除 Machine State 后仍可独立阅读、验证和复用；
11. CLI、未来 Desktop 和 MCP 使用同一 Recipe/Application 语义；
12. 固定质量样本中，新管线相对 v1 直接拼接在事实忠实、核心覆盖、结构、去重、引用、术语和可读性上无不可接受退化。
13. 请求高保真精编稿时，最终 Bundle 同时保留原始 Transcript 和独立精编 Draft；精编 Draft 不改变 segment 时间顺序，不把 AI 总结混入精编正文。
14. 高保真质量报告分别表达 segment 引用覆盖、顺序、Anchor 保留、长度变化和语义评估方法，不把 ID 覆盖率伪装成语义保真证明。
15. 同时请求两种文档时，二者都直接以不可变 Transcript 为权威输入；任一支线不得把另一支线的 AI 派生文本当作唯一事实来源。

## 3. 需求

### 3.1 功能需求

#### 3.1.1 自适应编译

- 系统根据安全模型预算、Transcript 规模、预期输出规模和质量预设选择单次生成或多阶段编译；
- `fast` 对安全可容纳的 Transcript 使用一次全局生成，对长 Transcript 使用并行局部压缩加一次全局合并；
- `balanced` 是默认高质量路径：安全可容纳时使用一次高质量全局生成，否则使用并行 Evidence-aware Knowledge Map 加一次 Global Composer；
- `balanced` 的正常路径最多两个顺序模型波次，质量修复后最多三个；只有 Knowledge Map 仍无法安全容纳时才增加有进度保证的分层汇总；
- `thorough` 才执行全局汇总、独立 Article Plan、并行 Section Writing、Global Editorial Pass 和可选模型 Reviewer；
- 选择结果必须记录在 Receipt，不得在 Job 运行中静默切换 Recipe 或模型身份。

#### 3.1.2 Transcript 质量

- Source/Transcript 层必须在编译前检查空内容、时间覆盖、乱序、异常重叠、重复字幕、异常长 segment、语言冲突、低置信度和异常时间空洞；
- 明显无效的平台字幕必须回退到其他字幕或转写路径，不能直接进入知识编译；
- 确定性清理不得覆盖原始 Transcript provenance；
- 正常模式不为一般标点或口语问题默认增加一次 LLM 清洗调用；只有 `thorough` 或明确质量异常时才允许受约束的模型 normalization。

#### 3.1.3 结构化知识提取

- 每个 Transcript chunk 生成类型化知识结果；
- 知识结果至少能够表达主题、知识陈述、概念、示例、步骤、限制或警告、重要性和支撑 segment ID；
- 模型只能引用允许的 segment ID；
- Core 校验模型输出后分配内部可信知识项 ID；
- Transcript 和所有模型派生内容继续作为不可信数据传递，不得执行其中的指令。

#### 3.1.4 全局汇总与大纲

- `balanced` 的 Global Composer 可以在一次模型调用内完成去重、大纲、写作和编辑，不要求把每个逻辑动作拆成独立模型调用；
- `thorough` 或超长输入的独立全局汇总必须保留所有输入知识项的 lineage；
- 语义去重结果必须记录 `merged_from` 或等价映射，不能静默丢弃知识项；
- 大纲必须产生唯一文章标题和有序章节；
- 每个高重要性知识项必须分配到一个章节，或者带明确省略原因；
- 模型不能创建可信 Evidence ID，最终 Evidence 映射由 Core 完成。

#### 3.1.5 写作与总编辑

- 当全局写作请求和预计输出在安全预算内时，可以一次生成完整文章；
- 超过预算时，按 Article Plan 分章节或章节组写作，再确定性组装；
- 总编辑接收 Article Plan、汇总知识、章节草稿、术语约束和 Evidence 允许集合；
- 总编辑可以重排、去重、统一术语和改善表达，但不得新增无 Evidence 事实；
- 输出必须同时提供最终 Markdown 和机器可校验的知识覆盖声明。

#### 3.1.6 质量与修复

- 确定性 Gate 至少检查唯一 H1、标题层级、重复标题、Citation/Evidence 闭合、实质 H2 Evidence、知识项分配闭合、占位符和 Markdown 安全；
- 语义覆盖、语义去重、教学质量和表达质量必须标注为模型或人工评估，不伪装为确定性证明；
- 只允许最多一次有边界修复；
- 修复请求只包含失败范围、相关知识项、Evidence 和质量报告，不重新执行无关下载、转写和知识提取；
- 修复后必须重新执行全部确定性 Draft Gate；
- 修复失败或修复后仍不合格时，Bundle 可以合法提交到 Source Store，但必须 `publish_eligible=false`。

#### 3.1.7 checkpoint 与 lineage

- 每条实际执行路径的物化阶段具有版本化 checkpoint；未执行的 thorough-only 阶段不创建空 checkpoint 或占位 Artifact；
- balanced 至少 checkpoint 冻结计划、Knowledge Map aggregate、Global Composer Draft 和 Repair Result；thorough 额外 checkpoint 汇总知识、Article Plan、Section Draft 和 Editorial Draft；
- checkpoint 的输入 hash 必须包含 Recipe/stage/prompt 版本、模型身份、上游内容 hash 和有效参数；
- prompt 或 stage 语义变化必须使旧 checkpoint 失效；
- 最终 Receipt 记录阶段版本、输入/输出 hash、usage、模型非敏感身份、重试和修复摘要；
- 中间结果默认属于 Machine State，不默认扩展 Portable Bundle 的必需输出集合。

#### 3.1.8 多文档产物与高保真精编稿

- 请求使用有序、去重的 `requested_outputs` 集合表达，第一版至少支持 `knowledge-note` 与 `faithful-edition`；未指定时继续默认生成知识笔记；
- `quality_preset` 与输出类型正交，但其内部语义由产物决定：知识笔记的更高质量增加知识编译深度，高保真精编稿的更高质量主要增加核对和验证，不增加无边界改写自由度；
- 高保真精编稿必须按原始时间顺序组织阅读章节，保留数字、否定、可能性、限制、术语、例子和观点修正；无法可靠纠正的内容保留原形式并标记不确定；
- 纯机械口吃和无语义重复可以保守清理，但表达立场、确定性、程度、因果、转折或适用范围的所谓“语气词”不得按词表盲删；
- 每个高保真章节必须声明来源 segment，并把精编正文、AI 章节总结、AI 关键点和待复核项作为不同语义区域表达；
- segment ID 引用闭合只证明记账覆盖，不能证明精编正文保留了全部语义；质量报告必须分开表达确定性检查、模型评估和人工评估；
- 同时请求两种 Draft 时，两条支线共享 Source、Transcript、Evidence、执行基础设施和安全边界，但第一版不通过一次模型调用同时承担忠实编辑与知识重组，也不让知识笔记串行依赖精编稿；
- 上位 Portable 合同继续允许一个 Bundle 包含多个 Draft；每个 Draft 必须有独立 Artifact ID、文档类型、内容 hash 和 QualityReport，并保留一个 primary Draft 兼容现有读取方；
- 所有用户明确请求的输出都完成并通过 Portable 完整性校验后，Producer Job 才能成功；未请求的输出不参与成功条件。

### 3.2 非功能需求

#### 3.2.1 架构与兼容

- Recipe 编排属于共享 Application/Core；
- 模型 Adapter 只拥有 Provider 协议、单次调用、响应归一化和 Provider 错误映射；
- 不在 v1 Recipe 上静默改变生产语义，重大语义变化使用新 Recipe 版本；
- 不改变 Portable Workspace、Publisher 或 iwiki 协议所有权；
- 不新增云端正文数据库或 UI 私有数据格式。

#### 3.2.2 性能与资源

- Transcript chunk 规划必须是线性复杂度；
- 局部知识提取和章节写作允许受限并发；
- 并行调用的结果按稳定 ordinal 组装，等待时间不得按 chunk 数量强制串行增长；
- `fast` 和 `balanced` 优先常见快路径；`balanced` 正常路径最多两个顺序模型波次，修复后最多三个；
- `thorough` 的额外汇总、规划、章节、编辑和 Reviewer 波次必须由用户明确选择；
- 模型预算同时考虑上下文 token、输出 token、提示词、Evidence 元数据、安全余量和请求体字节硬上限；
- 未知 tokenizer 使用保守估算，不把 HTTP 字节上限当作模型上下文上限；
- 视频、Transcript 和模型中间结果不得形成明显平方级内存或时间增长；
- 必须记录下载、字幕、音频抽取、转写、每个模型阶段、截图和 commit 的耗时；本地转写记录 `RTF=转写时长/音频时长`；
- 相同 Transcript 仅变更 style 或输出表达时，应复用仍然有效的 Transcript 和 Knowledge Map checkpoint；
- 同时请求知识笔记和高保真精编稿时，两条支线可以在统一 Provider 并发预算内并行执行，以减少墙钟等待时间；并行不等于减少总 token 成本；
- 高保真精编稿的输出规模通常接近 Transcript，超长视频必须分章节生成、按 ordinal 组装并只修复失败章节，不得以全文重写作为默认收尾；
- 基础 CLI 命令继续不加载模型、Whisper、FFmpeg 或 Web 重依赖。

#### 3.2.3 可靠性

- 每个外部模型调用继续使用 ExternalOperation 语义；
- 已成功调用的规范化结果必须在 Step 成功前持久保存；
- Provider outcome unknown 不得被当作普通 transient 直接重放；
- 并发 worker 结果按稳定 ordinal 确定性组装；
- 旧 fencing token 不能提交 checkpoint、Draft 或 Bundle；
- 恢复不能产生重复章节、重复知识项或重复 Bundle。
- 多 Draft 恢复不得重复生成已成功支线，不得把只完成部分声明输出的 Bundle 标记为成功。

#### 3.2.4 安全与隐私

- Transcript、Source metadata、知识中间结果和旧 Draft 全部视为不可信数据；
- 所有模型阶段不得获得工具、Shell、文件系统、网络或 Credential 权限；
- Secret、Cookie、完整 Prompt、绝对路径和未经证明安全的 Provider 原始数据不得进入 Bundle；
- 最终 Markdown 和资源继续通过现有路径、active content 和 Portable 安全校验。

#### 3.2.5 可评测性

- 固定质量语料至少覆盖长视频、口语重复、广告、专有名词、英文转中文、代码讲解和 Prompt Injection；
- v1、BiliNote 风格合并和 v2 编译结果应支持盲评比较；
- 质量评估至少覆盖事实忠实、核心覆盖、幻觉、结构、去重、压缩、引用、术语、中文质量、学习效率和可复用性；
- 高保真精编稿另行评估时间顺序、数字/否定/限定词/专有名词保留、语义遗漏、无来源新增、长度变化、不确定性标记和人工复核效率；
- Bundle 合法、Job succeeded 和内容高质量继续是三个不同结论。

## 4. 系统设计

### 4.1 方案概览

#### 4.1.1 核心方案

采用 **Application 层双文档编译支线 + Recipe 专用合同 + 低层模型执行器**：

> Video Recipe v2 在 Application 层新增可恢复的 `VideoKnowledgeCompiler`，负责结构化知识提取、全局汇总、大纲、写作、总编辑和定向修复；模型 Adapter 只负责执行一次模型请求，不拥有长视频编排逻辑。

高保真精编稿作为与知识笔记并列的 `FaithfulEditionCompiler` 支线：两者共享 Source、不可变 Transcript、Evidence、模型执行、checkpoint 和 Portable 提交能力，但在 Transcript Quality Gate 后分别冻结计划、生成 Draft 并独立验收。它们不合并为一篇文档，也不把相反目标塞进同一个 Prompt 或编译器类。

不在当前 `LegacyKnowledgeModelAdapter.generate()` 后简单追加最终合并，也不允许 CLI、Runtime、Desktop 或 MCP 决定长视频编译算法。

#### 4.1.2 模块划分

`VideoService` 继续拥有完整 Video Producer 用例：Source、Transcript、SourceRevision、Evidence、声明输出编译、截图、Portable Bundle、iwiki commit 和 Job 最终成功语义。它根据冻结的 `requested_outputs` 调用 `VideoKnowledgeCompiler` 和/或 `FaithfulEditionCompiler`，不直接实现各模型 Prompt、文章算法或高保真编辑规则。

`VideoKnowledgeCompiler` 是 Recipe v2 的 Application 服务，拥有：

- 编译路径选择；
- Transcript chunk 规划；
- 局部知识提取；
- fast、balanced、thorough 路径选择与执行；
- balanced 的 Global Composer；
- thorough-only 的全局汇总、Article Plan、分章节写作和总编辑；
- 文本质量检查和一次定向修复；
- 编译阶段 checkpoint、恢复、版本和稳定组装顺序。

`FaithfulEditionCompiler` 是独立但最小的 Application 支线，只拥有高保真精编稿的计划、按时间/主题形成阅读章节、受约束章节编辑、稳定时间顺序组装、精编文本质量和一次局部修复。它不生成 Knowledge Map、Article Plan 或全局知识重排，也不建立第二套 Runtime、Provider Adapter 或 Portable 提交路径。

高保真精编与知识笔记可以共享 token 估算、segment 边界、主题切分和稳定 ordinal 等分块基础能力，但各自冻结符合自身目标的 Chunk Plan；知识编译允许窗口重叠和主题重组，高保真精编必须避免正文重复并保持时间顺序。

`VideoCompilationPolicy` 根据 `quality_preset`、Transcript 规模、模型上下文和输出能力、Prompt 成本、预期文章长度、Provider 并发限制和 Runtime 安全预算，在首次付费调用前冻结：

```text
VideoCompilationPlan
  quality_profile: fast | balanced | thorough
  topology: direct | map_compose | hierarchical_compose | planned_sections
  transcript_chunks[]
  extraction_concurrency
  writing_mode: whole_article | section_batches
  section_batch_budget
  editorial_mode: whole_article | bounded_sections
  reviewer_enabled
  max_repair_attempts
```

运行过程中不得因成本、超时或 Provider 波动静默降低质量预设或更换 Recipe/模型；能力不足在 preflight 或规划阶段失败或产生明确 Challenge。

Video Recipe v2 的知识笔记合同、预算、Prompt、Parser、校验和组装逻辑位于 `core/recipes/video/compilation` 功能域；高保真精编稿的专用合同和质量规则位于并列的 `core/recipes/video/faithful_edition` 功能域。两者可以复用底层纯工具，但不提前抽象通用 Workflow DSL、万能 Document Compiler 或所有来源共享的万能 Knowledge IR；物理实现先收敛为少量文件，只有实际体积和变化频率证明需要时才继续拆分。

`ModelCallCoordinator` 是共享 Application 组件，负责一次模型操作的 ExternalOperation、request hash、调用、规范化结果持久化、outcome unknown、retry、usage、checkpoint、取消和 heartbeat。

`ModelExecutorPort` 是 Provider-independent 的低层单次模型调用端口。Provider Adapter 只负责请求格式、调用、响应归一化和错误映射，不理解视频、文章章节、Bundle 或 iwiki。

现有 `KnowledgeModelPort.generate()` 和 `LegacyKnowledgeModelAdapter` 暂时只服务 Recipe v1；Recipe v2 使用 `VideoKnowledgeCompiler` 和 `ModelExecutorPort`。v2 验收后 CLI 默认切换到 v2，v1 保留显式复现和对照期，删除另行设计。

#### 4.1.3 依赖方向

依赖方向固定为：

```text
CLI / Future Desktop / Future MCP
  -> AllToNote SDK / Application Facade
      -> VideoService
          -> VideoKnowledgeCompiler
              -> Video Compilation contracts and policies
              -> shared ModelCallCoordinator
          -> FaithfulEditionCompiler
              -> Faithful Edition contracts and policies
              -> shared ModelCallCoordinator
          -> Shared transcript and chunk utilities
          -> Job / Attempt / Checkpoint Ports
          -> ModelCallCoordinator
              -> ModelExecutorPort
                  <- Provider Adapters
          -> Evidence / Screenshot / Quality / Bundle
          -> PortableWorkspacePort
              <- iwiki Adapter
```

禁止：

- `VideoKnowledgeCompiler` 导入 Provider SDK 或 Codex client；
- `FaithfulEditionCompiler` 导入 Provider SDK、覆盖 Transcript 或依赖知识笔记 Draft；
- Provider Adapter 依赖 `VideoService`、BundleAssembler 或 JobRepository；
- Quality 依赖 Provider Adapter；
- CLI、Desktop、MCP 或 Runtime 复制 `VideoCompilationPolicy` 和 Recipe 分支；
- iwiki 反向依赖 AllToNote Recipe。

Runtime 只装配实现，不拥有第二套编译流程。

#### 4.1.4 数据所有权

- **Transcript**：标准 `TranscriptDocument` 是权威文本证据；模型不能修改 segment ID、文本和时间范围。
- **FaithfulEditionSection**：模型提出保守精编文本、章节摘要、关键点和不确定项，Core 校验来源 segment、时间顺序和结构后 checkpoint；它默认属于 Machine State，不替代 Transcript。
- **ChunkKnowledge**：模型提出、Core 校验并分配内部知识项 ID；由 `VideoKnowledgeCompiler` checkpoint，默认属于 Machine State。
- **ConsolidatedKnowledge**：只在 hierarchical/thorough 路径物化，保留每个输入知识项的 `merged_from` lineage，默认属于 Machine State。
- **ArticlePlan**：只在 planned-sections 路径独立物化，拥有唯一标题、章节顺序、知识项分配、术语约束和目标篇幅，默认属于 Machine State。
- **Final Drafts**：知识笔记和高保真精编稿是独立 Markdown Portable Artifact；模型阶段只使用 segment citation，Core 最终映射 Evidence ID。AI 总结和关键点不得混入高保真正文语义区域。
- **QualityReports**：每个 Draft 分别绑定精确内容 hash 并由对应 Portable Quality 拥有；模型 Reviewer 只能提供 `method=model` 结果，不能自行宣告确定性 PASS。segment ID 覆盖、顺序和 Anchor 检查不等于语义保真的确定性证明。

中间编译结果默认不进入 v2 必需 Portable 输出。Receipt 记录声明输出、各支线 Recipe/stage/prompt 版本、阶段输入输出 hash、模型非敏感身份、usage、重试和修复摘要。删除 Machine State 后，最终 Transcript、Evidence、一个或多个 Draft、对应 Quality 和 Receipt 仍可独立读取和验证。

#### 4.1.5 总体数据流

所有平台先归一化为同一 Transcript，平台差异不进入编译拓扑：

```text
Bilibili / YouTube / Douyin / Kuaishou / Local Video
  -> Source and Transcript adapters
  -> Transcript Quality Gate
  -> normalized TranscriptDocument
  -> freeze requested outputs and per-output plans
```

随后按声明输出分叉：

```text
normalized TranscriptDocument
  |-> FaithfulEditionCompiler
  |     -> chronological readable sections
  |     -> constrained faithful editing
  |     -> faithful text quality
  |     -> at most one scoped section repair
  |
  `-> VideoKnowledgeCompiler
        -> fast | balanced | thorough knowledge topology
        -> knowledge-note text quality
        -> at most one scoped repair
```

两条支线都直接以不可变 Transcript 为权威输入。第一版不把高保真章节总结作为 Knowledge Map 的唯一来源，也不通过一次模型请求同时完成忠实编辑和知识重构；同时请求时可以在统一 Provider 并发预算内并行运行。

`fast`：

```text
safe Transcript -> one global draft
long Transcript -> parallel local compression -> one global merge/draft
```

`balanced`：

```text
safe Transcript -> one high-quality Global Composer
long Transcript -> bounded parallel Knowledge Map -> one Global Composer
extra-long Knowledge Map -> progress-guaranteed hierarchical consolidation -> Global Composer
```

`thorough`：

```text
bounded parallel Knowledge Map
  -> ConsolidatedKnowledge
  -> ArticlePlan
  -> bounded parallel SectionDrafts
  -> Global Editorial Pass or bounded boundary editing
  -> optional Reviewer
```

知识笔记的三条路径随后统一进入：

```text
Draft
  -> deterministic text quality
  -> at most one scoped repair
  -> deterministic text quality again
  -> final screenshot requests
  -> screenshot capture and asset binding
  -> final Portable Quality
  -> Bundle assembly
  -> iwiki validate and commit
```

文本质量和一次修复发生在截图媒体获取之前，避免修复删除截图导致无用 Asset、修复新增截图却没有资源、Quality 绑定旧 Draft，或提前支付不必要的视频获取和 FFmpeg 成本。截图绑定后仍执行最终 Portable Quality，验证资源、相对路径、Draft hash 和 Bundle 完整关系。

高保真精编稿采用不同质量目标：检查 segment 引用闭合、时间顺序、数字/日期/比例/版本/路径等 Anchor、否定和限定词、专有名词、无来源新增、长度变化、不确定项和 Markdown 安全。`fast` 可以减少加工，`balanced` 使用受约束章节编辑与最多一次局部修复，`thorough` 主要增加独立复核、异常片段重转写或交叉验证，而不是增加全文改写自由度。所有用户声明的 Draft 及其 QualityReport 完成后，才进入同一 Bundle 的原子校验和提交。

#### 4.1.6 方案选择与取舍

拒绝只在现有 Adapter 后追加 BiliNote 式 Markdown 合并：它能快速减少多个 H1，但继续把编排藏在 Adapter，缺少结构化知识、覆盖 lineage、独立 checkpoint 和后续 Recipe 复用能力。

拒绝为知识笔记的提取、汇总、规划、写作、编辑和修复各建一个独立 Port：同一知识笔记 Recipe 内拆成六套端口会产生不必要的 Adapter、组合根和测试膨胀。阶段差异由 Recipe 的类型化请求、Prompt 和 Parser 表达，基础设施只实现一个低层 `ModelExecutorPort`。

拒绝把高保真精编稿实现成 `VideoKnowledgeCompiler` 内部的 `output_kind` 条件树：两者的顺序、压缩和质量不变量相反，强行合并会让 Prompt、计划、修复和质量分支互相污染。只新增一个并列的最小 `FaithfulEditionCompiler`，共享现有执行底座。

拒绝用 `faithful-edition | knowledge-note | both` 单值枚举表达产物：`both` 无法随未来 study guide、FAQ 或 tutorial 等文档自然扩展。对外使用 `requested_outputs` 集合，`quality_preset` 独立表达计算投入；同一质量名称在不同产物中保持“更高投入”的产品语义，但不强制拥有相同内部阶段。

拒绝为了节省调用让知识笔记串行依赖高保真精编稿，或让同一模型响应同时承担忠实编辑和知识重构。第一版优先保持两条语义链独立和可评测；只有真实语料证明某种中间结果复用不会造成质量退化时才增加优化。

采用本方案会让 v1/v2 短期并存，并为长视频增加 Knowledge Map 和 checkpoint；换取统一文章、可验证 lineage、有边界恢复、Provider/UI 解耦和未来 Recipe 可复用的模型执行底座。为避免过度复杂化，默认 `balanced` 不把汇总、大纲、写作和编辑固定拆成四次调用，而是优先使用两个顺序模型波次；完整逻辑阶段只在 `thorough` 或预算确实要求时物化。

绝对完成时间取决于字幕可用性、网络、Provider、模型、本地硬件和转写器，不能只由架构预先承诺。发布 Gate 使用顺序模型波次、阶段耗时、Transcription RTF、token/调用量和盲评质量共同验证；若 `thorough` 的额外阶段没有稳定提升人工质量，必须合并或删除，而不是因为设计已经存在就保留。

### 4.2 组件设计

#### 4.2.1 核心类与模块

组件设计采用最小可实现集合。模块分离用于隔离真实变化，不为每个逻辑阶段创建 Port、Service、继承类型或动态注册节点。

##### `VideoService`

现有 `VideoService` 继续拥有完整视频生产用例：Source、Transcript、SourceRevision、Evidence、解析声明输出、调用一个或多个 Draft 编译支线、截图、逐 Draft Portable Quality、Bundle 和 iwiki commit。Recipe v1 继续使用当前单 Draft 生成边界；Recipe v2 根据冻结的 `requested_outputs` 委托 `VideoKnowledgeCompiler` 和/或 `FaithfulEditionCompiler`。它不包含平台无关的 Prompt、模型 JSON 解析、知识文章算法或高保真编辑规则。

##### `VideoKnowledgeCompiler`

这是 Recipe v2 唯一有状态的知识编排组件。它负责冻结计划、执行 direct/map-compose/hierarchical/planned-sections 拓扑、传递阶段结果、稳定组装、文本质量和一次修复，并汇总 usage/warnings。预算、Prompt/Parser、模型调用持久化和 Markdown 基础分析由下游纯逻辑或执行组件负责，避免形成新的 God Class。

`VideoService` 与知识编译器之间只保留一个极窄的 `VideoDraftCompiler` Protocol；它可以与调用方放在同一模块，不发展为独立框架或六个阶段 Port。高保真支线使用同等狭窄的调用边界，是否与知识编译共用一个最小 Protocol 在 4.2.2 接口设计中确认，不提前建立通用编译器注册表。

##### `FaithfulEditionCompiler`

这是高保真精编稿唯一有状态的编排组件。它负责冻结高保真计划、形成保持时间顺序的阅读章节、执行受约束章节编辑、按稳定 ordinal 组装、运行高保真文本质量和最多一次局部修复，并汇总 usage、依据类型和不确定项。它不生成 Knowledge Map、ArticlePlan 或全局知识重排，不覆盖 Transcript，也不把 AI 章节总结混入精编正文。

第一版只要求实现并验证 `balanced` 路径；`fast` 与 `thorough` 在真实语料证明有独立价值后再增加。`thorough` 若实现，额外成本用于复核、异常片段重转写或交叉验证，不用于无边界全文重写。

##### `CheckpointedStepRunner`

把现有 `VideoService._checkpointed()` 和 heartbeat 执行语义原样提取为共享 Application helper，供现有外层步骤和 v2 物化阶段复用。它只拥有 Step/Attempt/checkpoint/fencing/heartbeat，不理解模型或文章语义。提取首先保持 v1 行为不变，并由现有恢复测试证明无回归。

fan-out 内的每个模型 shard 不创建动态 Job Step。一个逻辑阶段仍对应一个稳定 Step；单个 shard 的成功结果由 ExternalOperation result store 保存，aggregate 完成后再提交阶段 checkpoint。阶段重启时复用已成功 shard，避免动态 Step 状态机膨胀。

##### `ModelCallCoordinator`

它负责在 Job/Attempt 权限下执行一次模型 ExternalOperation：计算 request hash、恢复已成功结果、登记和启动操作、调用 Provider、先持久化规范化结果再标记成功、记录 usage、处理取消、heartbeat、已知重试和 outcome unknown。它不构造 Video Prompt、不解析 Video DTO、不决定下一阶段，也不访问 Workspace。

##### `ModelExecutorPort`

这是 v2 唯一新增的模型执行 Port，语义是执行一次冻结的 Provider-independent 模型请求并返回规范化响应。OpenAI-compatible、Codex app-server 和仍受支持的 Legacy Provider 通过 Adapter 实现它。Adapter 不拥有分块、长视频合并、checkpoint、质量或 Bundle。

现有 `KnowledgeModelPort.generate()` 暂时只服务 v1；两者短期并存，不合并为充满可选字段的万能接口。

##### 模型操作结果存储

现有 `ModelChunkResultStore` 最小泛化为按 operation ID 保存规范化模型结果的 Machine State store。第一版不建设独立复杂存储子系统；仅在 Application 测试替换确实需要时提供窄 Protocol。它原子写入并以 hash 校验，不保存完整 Prompt、Credential、Cookie 或 Provider raw，不进入 Portable Bundle。

##### Recipe v2 纯逻辑

知识笔记第一版物理实现收敛为：

```text
core/recipes/video/compilation/
  contracts.py   # 冻结 DTO、schema version 和不变量
  pipeline.py    # 预算、模式选择、Prompt/Parser、稳定组装
  quality.py     # Transcript/Text Gate 和最小修复范围
```

只有实际代码体积、独立变化频率或测试隔离证明必要时，才从 `pipeline.py` 拆出 `stages.py`、`assembly.py` 或 `codecs.py`。不创建六个阶段 Service、六个阶段 Port、通用 Pipeline 基类、动态 Stage Registry 或 Workflow DSL。

高保真精编稿第一版预计收敛为并列的少量文件：

```text
core/recipes/video/faithful_edition/
  contracts.py   # 章节、segment mapping、不确定项和版本化 DTO
  pipeline.py    # 章节计划、受约束 Prompt/Parser 与稳定组装
  quality.py     # 顺序、Anchor、长度变化和保真风险检查
```

这是最大预期布局，不要求先创建空文件。若第一版体积足够小，可以先合并文件；不得为了将来可能的文档类型引入通用 Recipe Registry、继承体系或 Workflow DSL。

##### 文本与 Portable Quality

知识笔记在截图前检查唯一 H1、标题层级、重复标题、segment citation、实质 H2、coverage 声明、占位符和文本安全。高保真精编稿另行检查 segment 引用闭合、时间顺序、Anchor、限定词、专有名词、无来源新增、长度变化、不确定项和正文/AI 导航区分离。截图后的 Portable Quality 对每个 Draft 继续检查 Evidence ID、Asset、路径、最终内容 hash 和 Bundle 语义。两种产物共享同一个 Markdown 安全与脚注分析能力，不共享相反的质量结论。

##### Runtime 与 Fake

Runtime 只装配 `VideoKnowledgeCompiler`、可选的 `FaithfulEditionCompiler`、`CheckpointedStepRunner`、`ModelCallCoordinator`、`ModelExecutorPort` 和现有 VideoService，不建立高保真专用 Runtime。正式失败不得回退到 Fake。VideoService 单元测试可以使用最小 Fake Compiler；各 Recipe 集成测试必须使用真实 Compiler 加 Deterministic ModelExecutor，确保自适应拓扑、时间顺序与质量规则本身被测试。

##### 目标物理布局

```text
backend/app/core/application/
  video_service.py
  video_compiler.py
  faithful_edition_compiler.py
  checkpoint_runner.py
  model_call_coordinator.py

backend/app/core/ports/
  model.py              # v1 兼容期
  model_executor.py     # v2 单次模型执行

backend/app/core/recipes/video/compilation/
  contracts.py
  pipeline.py
  quality.py

backend/app/core/recipes/video/faithful_edition/
  contracts.py
  pipeline.py
  quality.py

backend/app/adapters/models/
  legacy_gpt.py         # v1 兼容期
  legacy_model_executor.py
  model_operation_result_store.py
```

该布局是最大预期边界，不要求第一个实现任务一次创建全部文件。每个新增文件必须能追溯到已实现的职责，不能先建空扩展点。当前 BundleAssembler 仍只接受一个 `primary_draft` 和一个 Quality 结果；多 Draft 是已确认但待实现的下位扩展，不得在验收前宣称现有实现已经支持。

#### 4.2.2 接口设计

接口采用“旧请求保持不变、新请求显式多产物、编译请求专用化、最终结果兼容投影”的策略。公共接口不暴露 Recipe 中间 IR、Prompt、checkpoint 路径、Provider SDK 对象或 Portable 写入能力。

##### 请求版本与输出选择

现有 request schema v1 保持原语义，始终规范化为单一知识笔记 v1；不在旧 Job 上静默增加高保真产物。新增 request schema v2，通过 `requested_outputs` 表达用户声明输出：

```text
requested_outputs:
  - knowledge-note
  - faithful-edition
```

约束如下：

- 未提供时默认 `knowledge-note`，保持现有使用方式；
- 空集合拒绝为 `requested_outputs_empty`；
- 重复值在进入 request hash 前去重并规范化为固定顺序；
- 未知值拒绝为 `output_kind_unsupported`；
- 同时选择时 `knowledge-note` 默认成为 primary Draft；只选择 `faithful-edition` 时它成为 primary Draft；
- 第一版不增加用户自定义 primary 的参数；
- v2 在 Job 创建前把每个输出冻结为独立 Recipe Binding，至少为 `alltonote.video-course-note@2` 与 `alltonote.video-faithful-edition@1`；
- 每个 Binding 都进入规范化 Job Request、request hash、checkpoint 输入 hash 和 Receipt，不把映射只留在 Runtime 内存。

`quality_preset` 继续使用 `fast | balanced | thorough`，与产物类型正交。同一名称只保证“投入更高计算以实现该产物目标”的产品语义，不保证内部阶段相同。Capability/Preflight 必须声明实际支持的组合；第一版只要求 `faithful-edition + balanced`，请求尚未实现的组合返回 `output_quality_unsupported`，不得静默切换、降级或删除已声明输出。

##### 高保真语言策略

高保真精编稿必须显式冻结语言策略：

```text
faithful_language_policy:
  preserve-source
  translate-to-output
```

`preserve-source` 是默认值：精编正文保持 Transcript 来源语言，最大限度减少额外翻译误差。

`translate-to-output` 允许中国用户显式把英文等来源内容精编并翻译为 `output_language`，例如 `zh-CN`。该产物必须在 Markdown 头部、QualityReport 和 Receipt 中声明来源语言、目标语言和翻译属性，并标记其保真结论是“翻译型精编”，不能宣称与讲者逐字表达一致。原始 Transcript 在两种语言策略下都继续作为不可变 Artifact 保存。

##### 双编译器调用边界

知识笔记与高保真精编使用两个窄 Protocol，不建立 `UniversalVideoDocumentCompiler`、动态 Compiler Registry 或带大量可选字段的通用请求：

```python
class KnowledgeDraftCompiler(Protocol):
    def compile(
        self,
        request: KnowledgeCompilationRequest,
        context: VideoCompilationContext,
    ) -> CompiledVideoDocument: ...


class FaithfulEditionDraftCompiler(Protocol):
    def compile(
        self,
        request: FaithfulEditionRequest,
        context: VideoCompilationContext,
    ) -> CompiledVideoDocument: ...
```

`KnowledgeCompilationRequest` 只包含知识编译必需信息：schema/Recipe 版本、Transcript、Transcript 质量评估、来源标题与语言、输出语言、质量预设、style、截图策略和冻结模型 Binding。

`FaithfulEditionRequest` 只包含高保真精编必需信息：schema/Recipe 版本、Transcript、Transcript 质量评估、来源标题与语言、Transcript 依据类型、语言策略、翻译时的输出语言、质量预设和冻结模型 Binding。第一版固定包含精编正文、AI 章节总结、AI 关键点和待复核项，不为每个区域增加配置开关。

两个请求都不得包含 Workspace/本地媒体绝对路径、视频 URL、Cookie、Credential 明文、Provider base URL、Provider SDK DTO、iwiki client、BundleAssembler、JobRepository 或 Portable writer。

##### 统一执行上下文

两个编译器共享极窄的执行上下文：

```python
@dataclass(frozen=True)
class VideoCompilationContext:
    job_id: str
    authority: ExecutionAuthority
    cancellation_token: CancellationTokenPort
    heartbeat: HeartbeatPort
```

上下文只允许编译器检查取消、维持 heartbeat、使用正确 fencing authority 并通过共享 Coordinator 发起模型 ExternalOperation。它不授予 Workspace、文件系统、Portable Bundle、CLI event sink 或 Credential 访问能力。

##### 统一候选文档外壳

两个 Compiler 返回同一个最小 Application 候选文档外壳：

```python
@dataclass(frozen=True)
class CompiledVideoDocument:
    document_kind: VideoDocumentKind
    markdown: str
    cited_segment_ids: tuple[str, ...]
    screenshot_requests: tuple[ScreenshotRequest, ...]
    text_assessment: DocumentTextAssessment
    execution_summary: DocumentCompilationSummary
    usage: Mapping[str, int | float | str]
    warnings: tuple[str, ...]
```

它不是 Portable Artifact 或最终 QualityReport。Markdown 此时仍引用 segment ID，交回 `VideoService` 后由 Core 映射 Evidence、绑定可选 Asset、重新执行 Portable Quality 并生成最终 Artifact。

知识笔记摘要可以安全投影 profile、topology、chunk/knowledge item 数量、模型调用数、顺序模型波次、修复与 Reviewer 摘要；高保真摘要可以投影 profile、Transcript 依据、语言策略、章节数、模型调用数、修复数、不确定项数、Anchor 警告和 segment 引用覆盖。segment 引用覆盖不得命名为 `semantic_fidelity`。

编译器不得返回完整 Prompt、Provider raw、Knowledge Map/ArticlePlan/高保真章节的完整中间正文、checkpoint 路径、绝对路径、Credential 或 Cookie。高保真第一版不生成截图，`screenshot_requests` 必须为空；同时请求两种文档时，Bundle 只包含知识笔记合法请求的截图。只请求高保真稿却显式要求截图时 Preflight 返回不支持，不得静默忽略。

##### 单次模型执行端口

两条编译支线只共享一个低层单次调用 Port：

```python
class ModelExecutorPort(Protocol):
    def complete(
        self,
        request: ModelExecutionRequest,
        token: CancellationTokenPort,
    ) -> ModelExecutionResult: ...
```

冻结的 `ModelExecutionBinding` 至少表达 Provider 类型、模型身份、Credential Profile 引用、上下文窗口、最大输出 token、最大并发、结构化输出能力、temperature 能力和 timeout。Binding 在首次付费调用前冻结并进入 request hash/Receipt；Provider 实际模型身份与冻结身份不一致时失败。

`ModelExecutionRequest` 只表达 schema、stage/prompt 版本、system instruction、user content、输出模式、响应 schema、temperature、最大输出 token 和 timeout。当前合同不提供 Tool、Shell、文件系统、浏览器、网络、图片或 Provider-specific DTO；未来多模态是独立合同升级，不预埋空字段。

`ModelExecutionResult` 只返回规范化文本、实际模型身份、可用的输入/输出 token、finish reason、安全 Provider request ID 和 warnings，不返回 Provider 原始对象。Adapter 只执行一次调用和错误映射，不拥有 Video Prompt、长视频分块、重试、质量或 Bundle。

##### ModelCallCoordinator

所有模型操作通过：

```python
ModelCallCoordinator.execute(
    binding,
    request,
    execution,
    shard_key,
    token,
)
```

Coordinator 负责确定性 request hash、ExternalOperation 注册、成功结果复用、调用 Executor、规范化结果持久化、usage、heartbeat、取消、已知失败有限重试和 outcome unknown。request hash 至少绑定模型 Binding、stage/prompt 版本、system/user 内容、响应 schema、temperature 和最大输出 token；单纯 timeout 调整通常不使已成功结果失效，模型、Prompt、Schema 或内容变化必须失效。

明确临时失败可以有限重试；Auth、Policy、无效输入和响应合同错误不重试；Provider outcome unknown 产生暂停或 Challenge，不自动重放一次可能已计费的调用。

##### Step、shard 与恢复接口

一个逻辑阶段继续对应一个稳定 Job Step。Knowledge chunk 和 Faithful section 的 fan-out 单元使用稳定 `chunk-0000`、`section-0000` 等 ExternalOperation shard key，不创建动态 Job Step。

单个 shard 的规范化结果先持久化；全部 shard 成功并按 ordinal 稳定聚合后才提交阶段 checkpoint。阶段恢复复用已成功 shard，worker 不并发追加共享 Markdown。`CheckpointedStepRunner` 继续接收 step ID、input hash、authority、action 和直接 encode/decode 函数；第一版不建立 codec 类层次。

##### 多 Draft Bundle 输入

`VideoBundleInput` 从单 Draft 扩展为：

```text
VideoBundleInput
  source
  transcript
  evidence_set
  drafts[]
  primary_draft_artifact_id
  receipt
  display_assets[]
```

每个 `VideoDraftBundleInput` 至少表达 document kind、Draft Artifact ID、最终 Markdown、QualityReport Artifact ID 和绑定最终内容 hash 的质量结果。约束如下：

- `drafts` 非空，Artifact ID 唯一；
- 每个 Draft 恰好有一个独立 QualityReport；
- primary ID 必须引用 `drafts` 成员；
- Quality 不得跨 Draft 共用；
- Manifest 必须声明每个 Draft 的文档类型；
- 中间 Machine State 不进入 Portable Bundle；
- 所有用户声明输出及其 QualityReport 完整后才原子提交同一个 Bundle；
- 第一版不按文档类型拆成多个 Bundle，避免复制 SourceRevision、Transcript 和 Evidence。

##### 结果与 CLI 向后兼容

`VideoProduceResult` 保留现有 primary 投影：

```text
primary_draft_artifact_id
quality_report_artifact_id
quality_overall
publish_eligible
```

并新增 `documents` 列表；每项至少返回 document kind、Draft Artifact ID、QualityReport Artifact ID、quality overall 和 publish eligibility。旧调用方继续读取 primary，新调用方可以发现同 Bundle 的全部 Draft。

CLI 通过可重复 `--output` 选择产物：

```powershell
alltonote produce video --input "https://..." `
  --workspace "E:\MyVault" `
  --output knowledge-note `
  --output faithful-edition `
  --quality balanced `
  --wait --json
```

翻译型高保真稿显式使用：

```powershell
alltonote produce video --input "https://..." `
  --workspace "E:\MyVault" `
  --output faithful-edition `
  --faithful-language translate-to-output `
  --output-language zh-CN `
  --quality balanced `
  --wait --json
```

未传 `--output` 时仍生成知识笔记。CLI JSON 保留现有字段并新增 `documents`，继续使用当前主协议版本；正式合同补充“读取方必须忽略未知 JSON 字段”的前向兼容规则。字段删除、改名、类型变化或既有字段语义变化才升级主协议版本。

Job/JSONL 事件使用稳定阶段名并携带 `document_kind`，只在实际执行时发出对应 knowledge map、global composition、faithful editing、text quality、repair、screenshot、portable validation/commit 事件，不暴露 IR 正文、Prompt 或 checkpoint 路径。

##### 兼容与原子成功语义

- v1 Job、checkpoint、Bundle 和结果按旧语义读取，不迁移为多 Draft；
- v2 Job 冻结每个输出的 Recipe、stage、prompt、schema、模型 Binding 和语言策略；
- 不兼容 Machine State 从最早安全阶段重算，不修改已提交 Bundle；
- 所有用户声明输出完成并通过 Portable 完整性校验后 Job 才成功；
- 任一声明输出失败时，其他已完成支线保留为可恢复 Machine State，但不得提交一个伪装完整的 Bundle；
- 未请求的输出不参与成功条件；
- Portable Bundle 角色扩展必须先通过 iwiki 合同兼容测试，不能只在 AllToNote 私有解释中增加字段。

#### 4.2.3 数据模型

数据模型按生命周期分为不可变来源证据、可删除 Machine State 和最终 Portable Artifact。不得为了恢复便利把全部中间状态升级为长期知识资产，也不得让删除 Job 缓存破坏最终 Bundle 的独立可读性。

##### 生命周期与存储边界

| 数据类别 | 代表数据 | 存储位置 | 可删除 | Portable |
|---|---|---|---:|---:|
| 来源证据 | SourceRevision、Transcript、Evidence | Source Bundle | 否 | 是 |
| Machine State | Plan、Knowledge Map、ArticlePlan、章节中间结果 | Job/checkpoint/result store | 是 | 否 |
| 最终产物 | Knowledge Note、Faithful Edition、QualityReport、Receipt | Source Bundle | 否 | 是 |

权威关系固定为：

```text
TranscriptDocument
  |-> Knowledge Machine State -> Knowledge Note Draft
  `-> Faithful Machine State  -> Faithful Edition Draft
```

Machine State 删除后，Transcript、Evidence、一个或多个 Draft、对应 QualityReport 和 Receipt 仍必须独立读取和验证。Machine State 不写入云端正文数据库，不引入新的 ORM/SQLite 所有权。

##### 顶层 Video Recipe 与输出编译绑定身份

多产物 Bundle 的 `producer.recipe` 使用顶层 `alltonote.video-producer@2`，准确表达 Source/Transcript/Evidence、声明文档编译、Bundle 组装和 commit。这里的 `producer` 是 Portable provenance 字段和历史 identity，不表示 Video 拥有 AllToNote 平台。每个 Draft Artifact 的 `generated_by` 分别记录 `alltonote.video-course-note@2` 或 `alltonote.video-faithful-edition@1`。

外层身份不是第三套执行管线，只是避免把同时含两种 Draft 的 Bundle 错误标记成单一知识笔记 Recipe。v1 Bundle 继续保留原 Recipe 身份，不迁移或重写。

##### 公共输出与语言枚举

第一版只增加：

```python
class VideoDocumentKind(StrEnum):
    KNOWLEDGE_NOTE = "knowledge-note"
    FAITHFUL_EDITION = "faithful-edition"


class FaithfulLanguagePolicy(StrEnum):
    PRESERVE_SOURCE = "preserve-source"
    TRANSLATE_TO_OUTPUT = "translate-to-output"
```

规范化后的 `ResolvedVideoOutput` 至少包含 document kind、Recipe ID/version 和 quality preset。v2 Job Request 保存外层 Producer Binding、canonical `requested_outputs`、模型/转写 Profile、output language、style、截图策略和高保真语言策略。`translate-to-output` 必须具有合法目标语言；`preserve-source` 不得静默使用 output language 翻译正文。style/截图第一版只影响 Knowledge Note。

##### Transcript 质量评估

共享 `TranscriptQualityAssessmentV1` 至少表达：

```text
schema_version
transcript_sha256
status
transcript_basis
source_language
detected_languages[]
duration_known
source_duration_ms
transcript_start_ms
transcript_end_ms
coverage_ratio
duplicate_ratio
empty_segment_count
out_of_order_count
overlap_issue_count
abnormal_gap_count
abnormal_segment_count
confidence_available
confidence_summary
checks[]
warnings[]
```

`transcript_basis` 使用 `uploader-caption | platform-caption | human-transcript | asr-transcript | unknown`。来源时长未知时 coverage 为 null；转写器不提供置信度时明确 `confidence_available=false`，不能伪造高置信度。Assessment 绑定精确 Transcript hash，Transcript 变化即失效。Checkpoint 引用 Transcript Artifact/hash 和 segment 范围，不重复嵌入整份 Transcript。

##### 知识编译计划与 Chunk 引用

`VideoCompilationPlanV1` 至少包含 Recipe/profile/topology、Transcript/模型 Binding hash、stage/prompt 版本、Chunk 引用、最大并发、预期顺序模型波次、writing/editorial mode、Reviewer 和最大修复次数。

`TranscriptChunkRefV1` 只保存 ordinal、起止 segment ordinal/ID、起止时间、估算输入 token 和 segment ID 列表 hash，不复制文本。知识 Chunk 可以按冻结计划重叠，但 worker 不得自行改变边界；恢复使用相同计划和稳定 ordinal。

##### Knowledge Map

第一版使用最小 `ChunkKnowledgeMapV1`，包含 chunk ordinal/hash、items、术语候选和 warnings。`KnowledgeItemV1` 只表达：

```text
knowledge_item_id
kind: concept | claim | procedure | example | constraint | warning
title
statement
importance: core | supporting | context
source_segment_ids[]
```

模型只返回局部 ordinal、内容和允许的 segment ID；Core 校验后按 stage/version、Transcript digest、chunk/item ordinal 和规范化内容 digest 分配稳定内部 ID。内部 ID 不使用 Portable Artifact 命名空间。第一版不引入通用知识图谱、ontology、embedding、跨视频实体解析或万能 Knowledge IR。

##### ConsolidatedKnowledge 与覆盖账本

`ConsolidatedKnowledgeV1` 只在 hierarchical/thorough 实际执行时物化，包含输入 map digest、汇总 items、omissions 和 coverage ledger。汇总 item 保留 `merged_from[]` 和最终 source segment IDs。

每个输入 Knowledge Item 必须恰好进入某个 `merged_from`，或者进入带明确原因的 omissions；不能静默丢弃或被多个汇总项重复声明完整拥有。Coverage Ledger 只证明 ID 记账闭合，不命名为或宣称 `semantic_coverage_proof`。

##### ArticlePlan、SectionDraft 与 EditorialResult

`ArticlePlanV1` 只在 planned-sections 路径物化，包含唯一 title、有序 sections、术语约束、omissions 和 coverage ledger。每个 `ArticleSectionPlanV1` 只保存 Core 分配的 section ID、ordinal、标题、目的、知识项分配和目标篇幅提示，不保存最终 Markdown。

高重要性知识项必须分配到一个主章节或有明确省略原因；辅助跨章节引用与主分配分开表达。术语约束第一版只需要 canonical term、variants 和来源 segment。

`VideoSectionDraftV1` 保存 section ID/ordinal、Markdown、覆盖知识项、引用 segment、截图请求和 warnings；`VideoEditorialResultV1` 保存最终 Markdown、覆盖/省略账本、引用 segment、截图请求和 warnings。模型不能返回可信 Artifact/Evidence ID，所有引用集合由 Core 校验。

##### 高保真计划与严格时间分区

`FaithfulEditionPlanV1` 至少包含 Recipe/profile、Transcript hash/basis、来源语言、语言策略、目标语言、模型 Binding、stage/prompt 版本、sections、最大并发和最大修复次数。

每个 `FaithfulSectionRefV1` 保存 Core 分配的 section ID、ordinal、连续 segment 起止范围、起止 ID/时间和估算 token。高保真 sections 对所有可编辑 Transcript segment 形成连续、无交叉、无重叠的时间分区；每个非排除 segment 恰好属于一个 section，避免知识编译式 overlap 导致正文重复。

排除 segment 只能由 Transcript/Core 规则确定，第一版仅允许 `empty | music | noise | confirmed-asr-duplicate`。广告、闲聊或重复解释不能因为“价值低”在高保真模式中被模型排除。

##### 高保真章节与段落级来源映射

`FaithfulEditionSectionV1` 包含 section ID/ordinal、标题、起止时间、paragraphs、summary、key points、uncertainties 和 warnings。

正文使用：

```text
FaithfulParagraphV1
  paragraph_ordinal
  text
  source_segment_ids[]
```

每个 paragraph 至少引用一个 segment，paragraph ordinal 连续，来源 segment 按时间非递减；每个非排除 segment 第一版恰好分配给一个正文 paragraph。仅在 summary/key points 中引用不计作正文覆盖，同一个 segment 第一版不拆到多个正文 paragraph。这不能证明语义完整，但把可复核范围从整个长章节缩小到阅读段落。

章节 summary 独立保存文本和来源 segment；每个 key point 保存 ordinal、文本和来源 segment；每个 uncertainty 保存 ordinal、category、描述和来源 segment。uncertainty category 第一版为 `asr-term | person-or-organization | number | code-or-command | language | unclear-audio | other`。不确定项只能说明风险和位置，不能把模型猜测当成已确认修正。

##### 高保真质量模型

不生成虚假精确的单一 fidelity score。`FaithfulTextAssessmentV1` 绑定 Transcript/Draft hash、语言策略、checks、metrics、failed scopes 和 repairable 状态。

每个 `QualityCheckV1` 使用：

```text
check_id
method: deterministic | model | human
status: pass | warning | fail | not_applicable
severity
scope
safe_details
```

Metrics 至少记录正文 segment 引用覆盖、顺序错误、未知引用、正文重复分配、源/目标字符数、长度比例、数字/技术 token 不匹配、否定或限定词警告和 uncertainty 数量。100% segment 引用只表示记账覆盖；模型语义审查必须标为 `method=model`。翻译型高保真稿不使用与同语言精编相同的字符长度 Gate，中英文长度比例只作为观测指标。

##### Portable Draft 表达

两种 Markdown 继续使用通用 `knowledge.draft.markdown.v1`，因为它们都是可审阅、可发布、Evidence-aware 的 GFM Draft。文档语义通过版本化 Artifact extension 表达，而不是依靠文件名、目录顺序、H1 或“第一个 Markdown”猜测：

```json
{
  "extensions": {
    "alltonote.video:draft": {
      "schema_version": 1,
      "document_kind": "faithful-edition",
      "transcript_basis": "platform-caption",
      "source_language": "en",
      "language_policy": "translate-to-output",
      "target_language": "zh-CN"
    }
  }
}
```

Knowledge Note 使用相同 extension schema，document kind 为 `knowledge-note`。每个 Draft envelope 的 `generated_by` 记录对应文档 Recipe。

##### 多 Draft Output Profile

Video Bundle v2 output profile 保留 `primary_draft`，新增 `drafts[]`，并继续列出 Transcript、Evidence、Quality Reports、Source snapshots 和 display assets。`alltonote.video:bundle` extension 升级 `video_bundle_schema_version=2`，使用 documents 列表把 document kind、Draft Artifact ID 与独立 QualityReport Artifact ID 关联。

该映射影响正确解释，必须由版本化 Recipe/Portable Contract 声明并通过 iwiki validator；不认识必需多 Draft 合同时 fail closed。具体 required contract URN 在下位 Portable 实现任务冻结，不能先在 AllToNote 私有代码中自说自话。

##### 逐 Draft Quality 与 Receipt

每个 Draft 独立绑定 Artifact ID、最终内容 SHA-256、QualityReport Artifact ID、overall、publish eligibility 和 method summary。一个 QualityReport 不得同时验证两份 Markdown。旧 `quality_report_artifact_id`、`quality_overall` 和 `publish_eligible` 继续投影 primary Draft。

Receipt 的 outputs 摘要按文档记录 Artifact/Quality ID、Recipe、profile、语言策略、模型 Binding、调用/顺序波次/修复、usage 和 warnings。高保真额外记录 Transcript basis、语言、section/uncertainty/Anchor warning 数和正文 segment 引用覆盖。Receipt 不保存 Prompt、模型原始响应、Knowledge Map/章节中间正文、Secret、Cookie、绝对路径或 Provider raw。

##### ID 所有权

- Bundle/Artifact/Source/SourceRevision/Evidence/QualityReport 等 Portable ID 由 Core 分配并在对应计划冻结后跨恢复保持不变；
- Transcript segment ID 在 normalization 后不可变，模型只能引用；
- Knowledge Item、Article/Faithful Section 等 Machine State ID 由 Core 根据冻结计划、ordinal 和规范化内容稳定生成，不进入 Portable ID 命名空间；
- 模型只允许返回局部 ordinal、Core 已提供 ID 和允许的 segment ID；模型自行生成可信 `art_*`、`ev_*`、`src_*` 等值必须拒绝。

##### 编码、约束与 Schema 演进

Machine State 使用 canonical JSON、UTF-8、LF、显式 schema version、严格字段集合、有界字符串/数组、内容 SHA-256 和原子写入，不引入 Protobuf、数据库表或新 ORM。模型 DTO 的数量和文本上限由冻结计划预算决定，Parser 必须拒绝超预算嵌套/数组，避免大响应导致明显平方级解析或内存增长。

新代码遇到不兼容 Machine State 不执行复杂迁移，而是从最早安全阶段重算；只有 request hash、模型结果和 Parser 合同仍兼容时才复用已成功 ExternalOperation。旧 checkpoint 不原地改写。

Portable v1 Bundle 永久按 v1 读取；v2 多 Draft 使用新 output profile/extension schema。新 Reader 支持 v1/v2，旧 Bundle 不原地升级，不认识 required contract 时返回 `unsupported_schema`。

##### 第一版明确不建立的数据模型

第一版不创建 Universal Document IR、通用 Workflow Stage Schema、Recipe Registry 数据库、Knowledge Graph、Entity Resolution、向量索引、Transcript diff 数据库、逐字编辑审计日志、每阶段 Portable Artifact、每段独立 Markdown、高保真专用 Vault 或云端正文数据库。只有真实使用和评测证明必要时再扩展。
