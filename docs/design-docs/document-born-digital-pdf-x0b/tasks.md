# 首个 born-digital PDF 纵切与 X0-B 任务

> 来源：本目录 `spec.md`
> 当前状态：DOC-00 与真实 PDF 产品纵切已通过；X0-B 最小数据面已关闭
> 执行规则：一次只扩大一个已由 Video + Document 共同证明的数据面合同

## Task 0：[x] 对齐权威文档和术语

- [x] 确认 Document 是并列 Recipe，不是独立 Production/Runtime；
- [x] 建立 Document Domain Kernel 与 Document Parsing Engine 术语边界；
- [x] 记录 ADR-0002；
- [x] 不删除或重写既有上层/下层规范。

## Task 1：[x] 冻结唯一真实 PDF 与 Oracle

- [x] 固定文件名、大小、mtime、完整 SHA-256；
- [x] 确认 4 页、无密码、born-digital 原生文字可读；
- [x] 固定标题和主要章节结构 Oracle；
- [x] 原论文不提交 Git，只提交无正文的 fixture manifest；
- [x] 处理前后源文件 SHA-256 不变。

## Task 2：[x] 完成 parser-neutral DOC-00 Spike

- [x] 建立只包含 source hash、page、bbox、kind、text、basis 和 receipt 的私有 DTO；
- [x] 用 `pdfplumber==0.11.8` 跑轻量基线；
- [x] 用完全离线 `docling-slim==2.117.0` 跑同一输入；
- [x] 记录 Windows 依赖缺口、模型准备、磁盘、耗时和峰值内存；
- [x] 选择 Docling，保留轻量基线为比较证据；
- [x] Spike DTO 聚焦测试通过。

## Task 3：[x] 实现最小 Document 原生纵切

- [x] 建立 AllToNote-owned Document Source/Block/Evidence/Result 类型；
- [x] `document-basic` 以可选固定 Pack/worker 调用 Docling，不进入最小 Runtime 冷导入；
- [x] 由外部文件 hash + page/bbox 生成可审计 Evidence；
- [x] 使用确定性原生结构投影生成可用 Markdown，不引入 OCR/Vision/模型调用；
- [x] 经 X0-A generic `produce` 提交真实 durable Job；
- [x] 生成 Artifact manifest、Draft、Evidence 与 receipt；
- [x] crash/reopen 后不重复已完成解析。

## Task 4：[x] 由 Video + Document 完成 X0-B

- [x] 先用失败测试固定 legacy v1 数据库、成功/未完成 Video Job 和旧 result wire；
- [x] 只抽取两个真实 Recipe 共同需要的 result discriminator/schema、Artifact refs 与 persisted execution identity；
- [x] 实现可重复 schema migration、dual-read 和 rollback oracle；
- [x] 抽取 generic atomic result commit，不让通用 Repository 导入任一 Recipe domain；
- [x] reconnect factory 按 persisted Recipe/Pack/runtime identity resolve exact executor；
- [x] 对解析、Artifact commit、result/terminal transition 注入故障并验证零半提交/零重复；
- [x] Video 聚焦、CLI contract、integration 和完整 backend 回归通过。

## Task 5：[x] 最小化硬化 `document-basic`

- [x] 固定 Docling、SciPy、CPU Torch、layout revision 与权重 hash；
- [x] Document Runtime 创建 machine state 前运行离线 doctor；
- [x] 真实导入依赖并校验本地模型完整性，错配时 fail closed；
- [x] worker stdout/stderr 不做无界内存收集；
- [x] 同一真实 PDF 经硬化路径再次通过，源文件不变；
- [x] 聚焦测试、完整 backend 回归与 `git diff --check` 通过；
- [x] 不扩展通用 Pack 管理器、PPTX、OCR 或新插件框架。

## Task 6：[x] 中文路径与表格密集 PDF

- [x] 冻结 6 页中文、8 表格真实 PDF 的 identity 与代表页视觉 Oracle；
- [x] 复现并最小修复 Docling 在 Windows 中文路径上的加载失败；
- [x] 保留第一次失败 Job，不覆盖或伪装失败证据；
- [x] 客观判定关闭 TableFormer 时的单大单元格与重复块不通过；
- [x] 只启用 Docling 官方 TableFormer `accurate`，不自建表格 heuristic；
- [x] 固定 OpenCV/NumPy、TableFormer revision、配置与权重 hash；
- [x] 新 Pack 正式产出 6 页、109 块、8 张多行表格和 5 类 Artifact；
- [x] 原英文双栏论文经新 Pack 回归通过，两个源文件均保持不变。

## Task 7：[x] 默认阅读呈现去除系统 Evidence 噪声

- [x] 失败测试证明 Document 主 Draft 同时暴露逐块 Evidence 脚注和页尾定义；
- [x] 主 Draft 改为干净的人类阅读入口，不再输出 `[^ev_*]` 或 `Document page` 定义；
- [x] 独立 EvidenceSet 继续逐块保留 Evidence ID、page、bbox、excerpt hash 和 normalized target；
- [x] 不新增隐藏 HTML 协议、第二份审计 Markdown 或 Document 专用 reading 投影；
- [x] 中文表格与英文双栏 PDF 均通过新 Bundle 回归，主 Draft 系统脚注为 0，Evidence 记录数分别为 109 和 82。

## Task 8：[x] 不可信 PDF 文本的 Markdown 安全与质量诚实

- [x] 失败测试证明 raw Docling block 可以把标题、链接、图片、列表、HTML 与 Mermaid fence 注入主 Draft；
- [x] 普通文本按 Markdown 字面量投影，裸 URL/邮箱按行内代码投影，未经信任的解析文本不再获得链接、HTML 或图表执行权限；
- [x] Docling Markdown 表格继续保留表格结构，仅把单元格内容按字面量处理；
- [x] exact raw text 继续只保存在 normalized content 与 Evidence 中，不损失审计证据；
- [x] 最终 Markdown 安全验证失败时输出固定安全占位内容，并统一记录 `overall=fail`、`publish_eligible=false`；
- [x] 中文表格与英文双栏 PDF 均生成新 Bundle，安全验证、Portable inspection、无噪声检查、raw text/hash 往返与源文件 hash 检查通过；
- [x] 聚焦回归 `298 passed`、完整 backend 回归 `1940 passed, 2 skipped, 1 warning, 3 subtests passed`，独立只读复审无剩余 P0/P1，`git diff --check` 通过。

## Stop conditions

- 必须把 `DoclingDocument` 或 page/bbox 放进通用 Job schema 才能继续；
- 必须同时实现 OCR、PPTX、URL 或完整 Document MVP 才能证明纵切；
- generic wait/reconnect 仍硬编码 Video factory或通过重新 submit 恢复；
- 迁移不能 dual-read 旧成功/未完成 Job；
- Docling 在未声明网络访问、未固定模型或未隔离重依赖时进入正式路径。
