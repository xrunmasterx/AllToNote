# AllToNote PDF / PPT / OCR Document Recipe 设计

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-14-alltonote-portable-artifact-source-bundle-design.md
  - 2026-07-18-alltonote-recipe-extension-contract-design.md
downstream:
  - ../plans/2026-07-18-alltonote-document-recipe-implementation-plan.md
implementation_status: not-started-x0-a-prerequisite-x0-b-joint-slice
last_verified_at: 2026-07-20
```

## 1. 决策摘要

Document Recipe 使用“原生结构优先、视觉/OCR 补充、Evidence 统一”的策略：

```text
PDF / PPTX / 受控 URL
  -> 文件类型、完整性和安全探测
  -> 原生文本/对象/Notes/表格/图片/布局提取
  -> 逐页/逐 slide 判断缺口
  -> 仅对需要的页面渲染、OCR 或视觉理解
  -> Page/Slide/BBox/Shape Evidence
  -> 结构化知识地图 -> 全局文档
  -> Quality -> Bundle -> Review
```

不采用“全部页面截图后逐页扔给视觉模型”作为默认路径，因为它会丢失机器可读结构、成本高、速度慢、难以准确引用，也会把短文档和长文档统一拖入最重路径。

Document 是 AllToNote 的并列官方 Recipe，不是新的“PDF Production 应用”或第二套 Runtime。它只拥有文件身份、页面/slide 提取、OCR/视觉路由、DocumentBlock、领域 Evidence 和质量策略；ProduceService、RecipeRegistry、Job/Checkpoint、Artifact/Bundle、Review/Publisher、CLI envelope 和 Workspace 写入由平台共享实现。

本 Recipe 同时承担真实第二消费者职责，但实施顺序拆为两段：先完成 implementation-worktree `recipe-x0-compatibility-extraction/spec.md` 与 `tasks.md` 定义的 X0-A 最小控制面接缝，再由 Document/PPT 的一个最小真实纵切与 X0-B 联合抽取 Job result、Artifact、Repository、atomic commit 和 generic reconnect。首个纵切只选一个真实 born-digital PDF 或原生 PPTX，不以 Fake Recipe/fake-only fixture 代替，不同时扩展全部 PDF/PPTX、OCR、Vision、旧格式或长文档矩阵。page/slide/bbox/Notes 等领域字段不得提升为所有 Recipe 的通用必填字段；Article/Wiki 验证前仍不冻结 internal v1。

## 2. 用户目标

- 本地 PDF/PPTX 或直链可以转换为高质量知识笔记；
- 扫描 PDF 能自动识别需要 OCR 的页，不重复 OCR 原生文本；
- PPT 的标题、正文、Notes、图表、表格、图片关系和 slide 顺序尽量保留；
- 重要结论可定位到页码/bbox 或 slide/shape；
- 100 页以上文档仍可生成连贯文章，不是逐页摘要拼接；
- 处理进度、耗时、成本和降级原因可见；
- 原始文件保持不变；
- Draft 进入同一 Review/Publisher 闭环；
- OCR、LibreOffice、视觉模型是可选 Pack，不膨胀最小 Runtime/Desktop。

## 3. 非目标

- 完整复刻 Office/PDF 渲染引擎；
- 修改或回写原 PDF/PPT；
- 默认执行 VBA、JavaScript、嵌入附件或外部链接；
- 支持所有历史/私有 Office 格式而不降级；
- 以 OCR 结果覆盖原文件；
- 逐页无界视觉模型调用；
- 通用文档管理系统；
- 法律级 OCR/版面鉴定保证；
- 自动将多个无关文件融合为一个事实源；
- 自动发布到正式知识库。

## 4. 支持范围

### 4.0 首个真实纵切（X0-B 验证范围）

在 X0-A 完成后，先从以下二者中选择一个：

- 一份小型、无密码、born-digital 的本地 PDF，完成原生文本、page Evidence 与可用 Markdown Draft；或
- 一份小型、无宏的本地 PPTX，完成原生文本/Notes、slide Evidence 与可用 Markdown Draft。

该纵切必须使用真实 parser、真实文件 hash、真实 durable Job/result、Artifact commit 与 crash/reopen；测试中的模型输出可以 deterministic fake，但不得以 Fake Recipe 或纯内存/fake Repository 作为多 Recipe 验收。首个纵切明确不做另一种格式、OCR、视觉理解、URL、旧 `.ppt`、多文件 collection 或长文档全矩阵。

### 4.1 MVP

- `.pdf`：born-digital、混合、扫描；
- `.pptx`：Open XML presentation；
- HTTP(S) 直接文件 URL，经 Web 安全下载策略；
- 单文件输入；
- Draft kind：`knowledge-note`；
- 来源语言默认、显式中文输出；
- local CPU OCR 可选。

### 4.2 后续

- `.docx/.epub` 等通过验证后的 Document Adapter；
- 旧 `.ppt` 通过隔离 Office conversion Pack；
- 多文件 collection/课程资料；
- 文档高保真精编稿；
- 复杂公式/手写/专业 OCR Provider；
- 图表数据抽取专项。

扩展格式先作为 Adapter/Recipe capability，不把 Apache Tika 的“能抽到文本”直接等同于正式支持质量。

## 5. 输入与 SourceRevision

### 5.1 本地文件

输入通过 Local Root Grant/path token。读取时：

- canonical path/reparse point 检查；
- 打开稳定文件 handle 或复制到 staging；
- 计算 size + cryptographic hash；
- MIME/magic 与扩展交叉验证；
- 记录 modified time 仅作诊断；
- 编译过程中源文件改变则失败或新 revision，不能混读。

Identity 默认基于用户逻辑 source identity；Revision 基于 file content hash。相同文件名改变内容为新 revision；同内容移动路径可按用户选择关联到已有 source。

### 5.2 URL 文件

- 先按 Web Recipe 的 SSRF/redirect/size/MIME 策略下载；
- 最终 immutable file hash 决定 revision；
- URL/canonical/ETag/Last-Modified 作为来源元数据；
- 登录下载使用 Browser Capture/用户提供本地文件，不把 Cookie 交给通用下载器。

### 5.3 原文件保留策略

```text
copy              完整复制进受控 Source Artifact
reference          只保存 hash/metadata/外部路径 token
copy-if-under-limit（默认） 小文件复制，超限请求用户选择
```

无论是否复制，Evidence 都绑定编译时 file hash。reference 源离线/移动时 Draft 仍可读，但无法完整复核时必须显示 source unavailable。

## 6. 安全探测

处理前：

- 文件大小、页/slide 数预估；
- container/ZIP 解压比和条目数限制；
- 加密/密码保护；
- 数字签名/损坏提示（不声称验证作者真实性，除非专门实现）；
- PDF active content/embedded files；
- PPT external relationships/macros/embedded OLE；
- 字体/图片/对象数量；
- 可用 Pack 和磁盘预算。

规则：

- 不执行脚本、宏、OLE、嵌入程序或外链；
- 密码只做 ephemeral Secret 输入，不写 Job/日志/Bundle；
- 损坏文件可以由隔离工具尝试只读恢复，但必须记录 warning；
- 解析器崩溃在 worker 隔离，不拖垮 Runtime；
- 文件过大在昂贵处理前给出 plan/预算。

## 7. PDF 流水线

### 7.1 Page inventory

每页建立：

```json
{
  "page": 17,
  "width": 595,
  "height": 842,
  "native_text_chars": 1320,
  "image_coverage": 0.18,
  "font_count": 5,
  "rotation": 0,
  "classification": "born-digital",
  "ocr_required": false,
  "render_required": false,
  "warnings": []
}
```

分类：born-digital、scanned、mixed、image-heavy、layout-complex、empty/unsupported。

### 7.2 原生提取

提取：

- text spans + bbox + font/size/reading hints；
- page labels/outline/bookmarks；
- links/annotations（只作数据）；
- images + bbox + caption candidate；
- vector/table candidates；
- metadata；
- reading order candidates。

不把 PDF 内部 object ID 当成跨解析器稳定合同；Artifact 保存解析器 identity 与原始 page/bbox。

### 7.3 OCR 路由

只对以下页面 OCR：

- native text 极少且图像覆盖高；
- 文本明显乱码/不可用；
- 用户显式请求；
- mixed page 中明确缺失区域。

OCR 过程：

1. 选择语言（自动候选 + 用户确认/配置）；
2. 以适当 DPI 渲染需要区域；
3. deskew/rotation 等可选预处理；
4. 生成文字+bbox+confidence；
5. 与 native text 去重/对齐；
6. 标记 `basis=ocr`；
7. 不覆盖原 PDF。

OCRmyPDF 可作为生成可搜索副本的可选工具，但 AllToNote 的知识 Evidence 仍绑定原文件 hash、页和 OCR Artifact，不把新 PDF 当成未说明的原来源。

### 7.4 Reading order

阅读顺序结合：

- PDF text order；
- bbox/栏布局；
- heading/font signals；
- table/figure exclusion；
- OCR block order；
- 可选 layout model。

多栏、页眉页脚、脚注、边栏的低置信排序必须记录。模型可帮助判别，但不得改变原 block/bbox Evidence。

## 8. PPTX 流水线

### 8.1 原生结构

逐 slide 提取：

- slide number/relationship；
- title/body placeholders；
- text runs、层级、语言；
- speaker notes；
- shapes/groups 和 z-order；
- tables/charts 数据与标签（能力允许时）；
- images/captions/alt text；
- hyperlinks；
- master/layout/theme metadata；
- animation/transition 只记录存在，MVP 不复现时序。

### 8.2 视觉渲染

原生对象无法表达以下语义时，选择性渲染 slide：

- 架构图/流程图；
- 大量空间关系；
- 图表趋势；
- 图片加少量标签；
- 组合/group/SmartArt 提取不完整；
- 字体/版式决定阅读顺序。

渲染可用 LibreOffice headless、PowerPoint automation（仅明确平台/用户许可）或其他官方 Pack。渲染结果 hash 与工具版本进入 Artifact/Receipt。

### 8.3 Notes

Notes 是高价值来源：

- 与 slide 本体分开保存；
- Evidence locator 指明 `slide-notes`；
- 不把 Notes 中的 presenter instruction 自动当作公开正文；
- privacy classification 默认继承 personal；
- 编译时可用于解释 slide，但 Draft 必须可区分 slide 可见内容与 Notes 补充。

### 8.4 旧 PPT

`.ppt` 需要独立 conversion Pack：

- 在隔离临时 profile 下转换为 PPTX/PDF；
- 记录原文件 hash、转换工具/version、输出 hash和 warnings；
- 转换结果是派生 Artifact；
- 如果转换丢失/失败，明确 unsupported，不静默读取错误文本。

## 9. 统一 DocumentBlock

```json
{
  "block_id": "db_...",
  "source_unit": {"kind": "page", "number": 17},
  "kind": "heading|paragraph|list|table|code|quote|figure|chart|note|formula",
  "text": "...",
  "bbox": [72, 110, 520, 180],
  "structure": {},
  "reading_order": 23,
  "extraction_basis": "native|ocr|visual|derived",
  "confidence": 0.96,
  "content_hash": "sha256:..."
}
```

PPT 使用 `source_unit.kind=slide` 并可增加 shape ID；PDF 使用 page+bbox。视觉模型的解释必须作为 derived block，并引用其输入图像和原生 blocks，不能冒充原始可见文字。

## 10. Artifact 与 Evidence

### 10.1 Artifact

| Kind | 说明 |
|---|---|
| `source-metadata` | 文件/URL identity、hash、页/slide 数、语言 |
| `document/source-copy` | 可选原始文件副本 |
| `document/inventory` | page/slide 分类 |
| `document/native-structure` | 原生 block/object/notes |
| `document/page-render` / `slide-render` | 选择性渲染 |
| `document/ocr-layer` | OCR text/bbox/confidence |
| `document/normalized-content` | 统一 blocks/reading order |
| `evidence-set` | locator 集合 |
| `draft` | Knowledge Note |
| `quality-report` | coverage/structure/citation |
| `receipt` | 解析/OCR/渲染/模型 identity |

### 10.2 Evidence locator

PDF：

```json
{
  "locator_kind": "document-page-bbox",
  "locator": {
    "file_hash": "sha256:...",
    "page": 17,
    "bbox": [72, 110, 520, 180],
    "block_ids": ["db_..."],
    "basis": "native"
  }
}
```

PPT：

```json
{
  "locator_kind": "presentation-slide-shape",
  "locator": {
    "file_hash": "sha256:...",
    "slide": 8,
    "shape_ids": ["shape-12"],
    "bbox": ["..."],
    "basis": "native+render"
  }
}
```

若 shape ID 只在某解析器稳定，则同时记录 content hash/bbox/ordinal，避免单点脆弱。

## 11. 编译策略

### 11.1 内容单位

- PDF：优先按 outline/heading/section，page 只作证据单位；
- PPT：按章节/slide sequence/topic，保留演示叙事顺序；
- 不让每页/slide 直接成为一篇独立摘要；
- 表格、图表、公式和图片作为相邻结构化知识输入；
- Notes 与 visible slide 分层。

### 11.2 长文档

```text
units inventory
 -> deterministic topic-aware chunk plan
 -> 每块结构化 knowledge map + evidence coverage
 -> 全局术语/实体/主题去重
 -> global outline
 -> coherent compose
 -> citation freeze
 -> deterministic quality
 -> targeted repair
```

借鉴长视频编译原则，但 chunk 依据 section/page/slide，而不是时间。

### 11.3 Profile

#### fast

- 原生结构；
- 只 OCR 明显扫描页；
- 不做视觉解释；
- 短文档单次 compile；
- 基础 Quality。

#### balanced（默认）

- page/slide inventory；
- 局部 OCR；
- 选择性 render；
- 长文档 map/compose；
- 表格/Notes/图表基本处理；
- targeted repair。

#### thorough

- 更严格 layout/coverage；
- 重要 diagram/chart 视觉解释；
- 低置信页复核；
- 更高 Evidence coverage；
- 先显示预计页数、OCR/视觉调用和成本。

## 12. Quality Gate

确定性：

- file hash/unit locator 有效；
- 页/slide 范围合法；
- Evidence block/bbox/shape 存在；
- Markdown 结构、唯一 H1、引用定义；
- 表格/code fence/公式语法；
- 每个核心 H2 有 Evidence；
- Draft language policy；
- 原始文件未被修改。

Document-specific：

- unit coverage（处理/跳过/失败清单）；
- native/OCR/visual basis 披露；
- OCR 低置信和语言；
- reading order warning；
- table/chart/notes preservation；
- page/slide map coverage；
- 不把视觉推断写成原文直接陈述；
- 长文档章节去重与全局连贯。

任何未处理页/slide 必须在 Quality 中列出，不能静默忽略。

## 13. CLI

X0-A 不新增 `add` 或独立 `run`，也不冻结 Document 的完整 result/plan 合同。首个纵切通过 generic `produce --recipe` 或 request 文件进入 X0-A 的同一 ProduceService；`produce document` 只在兼容 Gate 通过后作为同一路由别名开放。独立 `add` 或 `run` 不属于当前合同，未来如有新产品证据必须另行决策，不能由本设计预承诺。

长期期望的用户友好别名：

```text
alltonote produce document --file <path-token> --workspace <ref>
alltonote produce document --url <https-file-url> --workspace <ref>
  [--profile fast|balanced|thorough]
  [--output-language source|zh-CN]
  [--ocr auto|off|required]
  [--ocr-languages zh,en]
  [--source-copy copy|reference|copy-if-under-limit]
  [--json]
```

X0-A 目标只发布 generic `produce --recipe/--request`；首个 Document/PPT X0-B 纵切通过该 generic 路由进入。`produce document` 仅在相关兼容与发布 Gate 通过后作为同一路由别名开放；当前不发布独立 `add` 或 `run`。所有入口必须构造同一 ProduceRequest 并进入同一 ProduceService/Registry；CLI 不得直接实例化 PDF/PPT/OCR Pipeline，Document 也不得定义第二套 JSON envelope、退出码或结果 DTO。

密码通过安全交互/credential ref，不支持 `--password plaintext` 出现在 shell history。

## 14. Pack 设计

- `document-basic`：文件探测、PDF/PPTX 原生提取；
- `document-render`：PDF/PPTX 渲染；
- `document-ocr`：OCR engine + 语言包；
- `document-office`：LibreOffice/Tika/旧格式转换；
- `model-vision-*`：可选视觉模型连接器。

最小 Runtime 不依赖它们；basic Pack 未安装时 preflight 给出明确安装大小/来源/许可证。Job 固定 Pack version。

## 15. 安全与隐私

- 恶意 PDF/Office 在隔离 worker 中解析；
- 禁止宏、脚本、外链、OLE、embedded executable；
- ZIP bomb/对象数/图片像素/页数/字体限制；
- 临时目录配额和清理；
- 密码 ephemeral；
- 不把私密原文件上传模型，除非用户选择的 Provider policy 明确允许；
- 视觉调用优先发送必要页/裁剪区域，不发送整本文档；
- OCR/模型原始响应不进普通日志；
- source copy 的 privacy/classification 明确；
- common 发布仍需 Publisher common grant。

## 16. 性能与成本

- inventory/native extraction 流式处理；
- OCR/渲染按页 checkpoint，可并行但受 CPU/内存预算；
- 已有 native text 页不加载 OCR；
- 未用视觉模式不调用视觉 Provider；
- page render 可 content-addressed cache；
- 100 页文档不一次性把所有图片驻留内存；
- 相同 file hash 重启复用 inventory/native/OCR/render/model；
- 计划阶段估计 pages、scanned ratio、render/vision calls、磁盘；
- 默认 balanced 给出可接受的质量/时间，thorough 明确更慢更贵。

性能基线需分别记录 20 页 born-digital PDF、100 页混合 PDF、50 slide PPTX、扫描 PDF；不以一个平均数掩盖差异。

## 17. 测试矩阵

### 17.1 PDF

- 纯文本、扫描、混合、多栏、旋转；
- 表格/图表/图片/脚注/书签；
- 中文/英文/混合；
- 加密、损坏、超大、恶意 active content；
- OCR 语言错误/低置信；
- 页面在 source copy/reference 下复核。

### 17.2 PPTX

- 标题/列表/Notes；
- 表格/图表/图片/SmartArt/group；
- 自定义 layout/master；
- 空 slide/隐藏 slide；
- 外链/OLE/宏容器；
- 渲染有/无 LibreOffice；
- 中文字体与跨平台差异。

### 17.3 失败与恢复

- 文件编译中被修改；
- 磁盘满；
- parser/OCR/renderer crash/hang；
- 模型失败；
- 取消；
- 每页 checkpoint 后重启；
- Pack 更新但 Job 固定旧版；
- iwiki commit outcome unknown。

### 17.4 真实 E2E

1. 公开 born-digital PDF；
2. 中文扫描 PDF；
3. 混合 PDF；
4. 包含 Notes/图表/图片的 PPTX；
5. 100+ 页长文档；
6. file -> Bundle -> iwiki -> restart zero replay；
7. ReviewCandidate -> personal publish；
8. CLI JSON/error；
9. OCR Pack 缺失的确定降级；
10. 原文件 hash 前后不变。

### 17.5 平台接缝合同

- `produce document` 与 generic `produce --recipe/--request` 的 canonical request/plan digest 等价；
- 与 Video 共用 ProduceService、Registry、Job/Checkpoint、ArtifactCommitter、Bundle、ReviewCandidate 和 Publisher；
- 通用 Application/Domain/Repository 不导入 Document-specific 类型；
- 未安装 document Pack 时基础 Runtime/Recipe 发现仍可启动，preflight 返回确定的 capability 缺口；
- page/slide/bbox/OCR 数据只存在于 Document Artifact/Evidence/extension；
- Document 接入不改变 Video golden、Job identity、Portable/iwiki 或零重放语义。

### 17.6 X0-B 迁移、原子性与恢复 Gate

- 使用真实旧 Video 数据库 fixture 验证 legacy result dual-read；旧成功 Job 可查询，旧未完成 Job 可恢复；
- schema migration 可重复执行、迁移后可重开，`integrity_check` 与 foreign key 检查通过，并保留明确 rollback oracle；
- source identity、Artifact/portable commit、result 与 Job terminal transition 处于一个可证明的原子提交协议；在每个事务边界故障注入时，不暴露“结果可见但 Artifact 缺失”或反向半提交；
- generic reconnect/job wait 根据持久化的 Recipe、Pack、Runtime identity resolve exact executor；缺失版本 fail closed，不得硬编码 Video factory，也不得重新 submit 创建第二个 Job；
- Video 与真实 Document/PPT 在提取、模型、Artifact commit 前后 crash/reopen 均不重复有效昂贵副作用；outcome unknown 有 reconcile；
- migration、Repository/atomic commit 与 Engine migration 不并行修改同一热点；通用 Job/Repository 不导入任一 Recipe-specific domain。

## 18. 分期

### Phase D0：最小真实 Document/PPT 纵切 + X0-B

前置只要求完成 implementation-worktree X0-A spec/tasks。随后选择一份真实 born-digital PDF 或一份真实原生 PPTX，完成原生提取、page/slide Evidence、可用 Markdown、durable result query、Artifact/portable atomic commit 与 crash/reopen，并以该消费者联合完成 X0-B 的 dual-read、migration、generic reconnect 和恢复 Gate。不得在此阶段同时实现 PDF 与 PPTX，更不得扩展 OCR/视觉/长文档全矩阵。

### Phase D1：PDF OCR/长文档

混合页分类、局部 OCR、section map/compose、coverage/repair。

### Phase D2：PPTX 原生结构

text/notes/table/image/slide Evidence 与 Knowledge Note。

### Phase D3：选择性渲染/视觉

diagram/chart/复杂 slide，明确 derived Evidence 和成本。

### Phase D4：旧格式/collection

隔离 Office conversion 与受限多文档 collection；真实需求后实施。

## 19. 完成定义

1. PDF/PPTX 原生结构优先，OCR/视觉只按缺口启用；
2. 每个结论可回到 file hash + page/bbox 或 slide/shape；
3. 未处理/低置信 unit 明确出现在 Quality；
4. 长文档是全局 compose，不是逐页摘要拼接；
5. 原文件不被修改，copy/reference 策略可解释；
6. 重 Pack 可选且固定版本；
7. 恶意文档在隔离 worker 中 fail closed；
8. restart 不重复 OCR/视觉/模型；
9. Bundle/iwiki/Review/Publisher E2E 通过；
10. 真实 PDF/PPTX 质量、性能、安全矩阵达标；
11. 未复制 ProduceService、JobStore、Checkpoint、Bundle、Review、Publisher 或 CLI Pipeline；
12. 首个纵切经 X0-A 的 generic `produce` 进入同一请求语义；`produce document` 兼容别名不得形成第二条 Pipeline，当前不发布独立 `add` 或 `run`；
13. Video + 一个最小真实 PDF 或 PPTX 消费者已通过 dual-read、migration、atomicity、generic reconnect 与 crash/reopen Gate；Fake 不作为验收；
14. Article/Wiki 验证完成前不冻结 internal v1、更不发布公共插件 SDK；
15. 全 PDF/PPTX、OCR、Vision 和长文档矩阵属于后续阶段，不得由首个纵切冒充完成。
