# 首个 born-digital PDF 纵切与 X0-B 规格

```yaml
id: VAL-DOC-PDF-X0B-001
status: active
authority: stage
upstream:
  - ../../README.md
  - ../../decisions/ADR-0002-docling-as-document-parsing-engine.md
  - ../../superpowers/specs/2026-07-18-alltonote-document-recipe-design.md
  - ../../superpowers/specs/2026-07-18-alltonote-recipe-extension-contract-design.md
downstream:
  - tasks.md
  - fixture.json
  - report.md
implementation_status: passed-born-digital-pdf-slice-and-x0b
last_verified_at: 2026-07-30
```

## 1. 要回答的问题

只用一份真实 born-digital PDF，验证以下最小纵切：

1. 固定版本的真实解析器能离线提取可读原生结构，并生成 page/bbox Evidence；
2. Document 经 X0-A 的同一 `ProduceService -> RecipeRegistry` 入口提交，不建立 PDF 专用应用壳；
3. Video 与 Document 共同需要的 result、Artifact reference、Repository、atomic commit 和 reconnect 能以最小 X0-B 合同持久化；
4. legacy Video 数据 dual-read、schema migration、crash/reopen 和零重复昂贵副作用通过。

本阶段不证明扫描 PDF、OCR、Vision、URL、PPTX、长文档、多文件、公共插件 SDK 或完整并发 Engine。

## 2. 冻结输入

唯一输入为 `SA2023_RealTimeReflection.pdf`，完整 SHA-256 为 `155f56096e8196b08f0aab9d6a162daea0196d308ad323ab1aebc7fb749db6b1`，大小 `6,200,363` 字节，共 4 页。结构 Oracle 与来源处置见 [`fixture.json`](fixture.json)。

第三方论文原文件不提交进 Git；正式验证读取用户提供的外部只读文件，并在处理前后重复计算完整 SHA-256。测试开始后不得替换失败输入。

## 3. DOC-00 Parser Gate

同一文件、同一 parser-neutral 私有 DTO 比较：

- `pdfplumber==0.11.8`：轻量原生文本+bbox 基线；
- `docling-slim[format-pdf,models-local]==2.117.0` + `scipy==1.17.1`：关闭 OCR、表格模型和远程服务，只使用本地 layout 模型。

选择标准按优先级为：

1. 双栏正文阅读顺序和词边界可供后续编译；
2. 标题、章节、图注、表格等结构无需 AllToNote 自建复杂启发式；
3. 每个原生块具有合法 page/bbox；
4. 可固定版本并在完全离线模式重跑；
5. Windows 安装、启动时间、峰值内存、磁盘和许可证成本可被 Pack 明示管理。

DOC-00 已选择 Docling；定量证据和缺陷见 [`report.md`](report.md)。

## 4. 实现边界

- Document Domain Kernel 与 Document Parsing Engine 分离，术语以根目录 [`CONTEXT.md`](../../../CONTEXT.md) 为准；
- Docling 只存在于 `document-basic` adapter/worker，通用 Core、Repository 和 CLI 不导入 Docling；
- durable 数据只保存 AllToNote DTO、Artifact 与 Evidence；Docling JSON 可选诊断，不作为恢复输入的唯一事实；
- 原文件完整 SHA-256 由 AllToNote 计算，不依赖 Docling origin hash；
- page、bbox、block kind、parser warning 保留在 Document Artifact/Evidence，不提升为所有 Recipe 必填字段；
- 正式路径固定 Pack identity、parser version、model revision 和 offline artifacts；缺失时 fail closed；
- 当前 Spike 的受信任本地文件仍在独立 Python 进程执行；正式接收不受信任文件前必须补齐资源限额、超时和 worker 隔离。

## 5. X0-B 最小公共数据面

只允许抽取 Video 与 Document 都真实需要的字段：

- result discriminator 与 schema version；
- recipe key、executor/Pack/runtime identity；
- durable Artifact references/manifest；
- generic result query；
- source identity 与 terminal result 的原子提交边界；
- persisted identity 驱动的 exact reconnect factory。

DocumentBlock、page/bbox、Docling label、PDF metadata 不进入通用 Job/Repository 类型。X0-B 不是完整 `ProduceResult` 公共 SDK，也不发布动态插件 ABI。

## 6. 验收 Gate

1. 冻结文件处理前后 SHA-256 完全相同；
2. 离线 Docling 提取 4 页，标题、Abstract、Introduction、Method、Result、References 可按阅读顺序定位；
3. page/bbox 合法，page Evidence 能回到原文件 hash；
4. 生成可用 Markdown Draft 和 Artifact manifest；
5. generic `produce` 提交/查询/等待/取消不导入 Document 类型；
6. schema migration 可重复、重开后 `integrity_check`/foreign key 通过；
7. legacy Video 成功 Job 可查询、未完成 Job 可恢复；
8. Video 与 Document 在解析、Artifact commit、terminal transition 前后故障注入均不暴露半提交或重复有效副作用；
9. reconnect 只按 persisted Recipe/Pack/runtime identity 恢复原 Job，缺失版本 fail closed；
10. Video 既有 request/result wire、Bundle 与零重放回归不变。
