# DOC-00 真实 born-digital PDF Parser Spike 报告

## 1. 结论

**DOC-00 PASS，首个 X0-B PDF 纵切选择 Docling。**

选择是有条件的：Docling 只作为可替换的 `document-basic` Parsing Engine，以固定版本、本地模型和隔离 worker 运行；AllToNote 保持 Document Domain Kernel、完整 source SHA-256、Evidence、Job、Artifact、迁移与恢复所有权。

**后续产品纵切与 X0-B 也已通过。** 这只关闭一个 4 页 born-digital PDF 的 foreground durable 纵切，不扩大为扫描件、OCR、PPTX、URL、长文档或并发 Engine 已通过。

## 2. 固定环境

- Windows，Python `3.11.15`，CPU 模式；
- 输入：[`fixture.json`](fixture.json)；
- 轻量基线：`pdfplumber==0.11.8`；
- Docling：`docling-slim[format-pdf,models-local]==2.117.0`、`docling-core==2.88.0`、`docling-parse==7.8.1`、`docling-ibm-models==3.13.3`、CPU `torch==2.13.0`；
- Docling layout model：`docling-project/docling-layout-heron`，下载 revision `8f39ad3c0b4c58e9c2d2c84a38465abf757272d8`，模型卡声明 Apache-2.0；
- Docling 运行时设置：OCR=false、table structure model=false、remote services=false、external plugins=false、CPU 4 threads；
- 离线验证：`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`，只读取本地 artifacts path。

## 3. 结果矩阵

| 指标 | pdfplumber | Docling | 解释 |
|---|---:|---:|---|
| 解析耗时 | 310 ms | 5,426 ms | 均为独立进程；Docling 需加载 layout 权重 |
| 峰值工作集 | 81,924,096 B | 1,472,827,392 B | Docling 必须由 Pack 预算与隔离 |
| 隔离环境磁盘 | 43,877,160 B | 974,691,975 B | Docling 数值含显式补齐的 SciPy |
| 本地 layout 模型 | 0 | 171,766,102 B | 首次下载耗时 261.2 s，发生一次断流后续传成功 |
| 页数 | 4 | 4 | 与 Oracle 一致 |
| 归一化块 | 257 行块 | 111 语义块 | 数量不作为质量优劣 |
| 文字字符 | 17,788 | 20,488 | Docling 恢复了更多可读词边界/结构 |
| 合法 page/bbox | 257/257 | 111/111 | 均通过 |
| 结构标签 | 0 | 14 section headers、13 captions、1 table | Docling 明显胜出 |
| 完整标题单块 | 否 | 是 | 基线把标题拆成两行 |
| 双栏正文 | 大量词粘连、左右栏同一行交错 | 基本按栏和语义块可读 | 决策性差异 |
| 远程运行依赖 | 无 | 离线重跑通过 | 模型仍须预先准备 |

所有运行前后 source SHA-256 均为 `155f56096e8196b08f0aab9d6a162daea0196d308ad323ab1aebc7fb749db6b1`。

## 4. 真实缺陷

1. `docling-slim[format-pdf,models-local]==2.117.0` 在当前 Windows/Python 环境安装成功后，首次导入 `DocumentConverter` 因 `ModuleNotFoundError: scipy` 失败；隔离 Spike 显式固定 `scipy==1.17.1` 后继续。正式 Pack 必须把它写入自己的锁和 doctor，不依赖偶然传递依赖。
2. layout 模型首次匿名下载约 172 MB，耗时 261.2 秒且中途断流；工具能够续传，但产品不得在 Job 中隐式联网下载。安装器/Pack resolver 必须预取、校验并报告进度。
3. Docling 峰值工作集约 1.47 GB，远高于基线；并发调度与超时/崩溃隔离是生产 Gate，不因 4 页论文成功而豁免。
4. Docling 的内部 provenance 可能把一个 item 分成多个 bbox；adapter 必须按 `charspan` 切分，不能把完整 item 文本复制到每个 Evidence。Spike 已修正并用同一 DTO 复跑。

## 5. 为什么不选择轻量基线

轻量基线的速度、内存和安装体积显著更好；若输入只是单栏纯文本，它可能是更简单的答案。但本次唯一真实样本本身就是典型双栏技术论文：正文出现 `Estimationofglossyreflections...` 一类词粘连，Abstract 与 Introduction 同行交错，图注和表格没有语义类型。

把这些缺口补成可用 DocumentBlock 需要 AllToNote 自建栏检测、reading order、heading、caption、table 和页眉页脚启发式。那将把“少装依赖”转换成长期维护一个不完整 PDF 布局引擎，与项目的简单性原则冲突。Docling 在本样本上带来的质量提升足以抵消 Pack 级资源成本。

## 6. 可复现命令

从 `backend/` 执行：

```powershell
G:\.alltonote-spike-envs\pdfplumber-0.11.8\Scripts\python.exe `
  -m spikes.document_parsing.run --parser pdfplumber `
  --input E:\Agent_Learning\Paper\SA2023_RealTimeReflection.pdf `
  --output G:\.alltonote-spike-results\SA2023_RealTimeReflection\pdfplumber.json

$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
G:\.alltonote-spike-envs\docling-2.117.0\Scripts\python.exe `
  -m spikes.document_parsing.run --parser docling `
  --input E:\Agent_Learning\Paper\SA2023_RealTimeReflection.pdf `
  --output G:\.alltonote-spike-results\SA2023_RealTimeReflection\docling-v2.json `
  --artifacts-path G:\.alltonote-spike-envs\docling-models-2.117.0
```

Spike JSON 位于工作树外，只用于诊断；仓库不提交论文正文或解析全文。

## 7. 正式产品纵切结果

正式验证不再调用 Spike harness，而是通过：

`ProduceService -> RecipeRegistry -> alltonote.document-note@1 -> Job -> document-basic@docling-2.117.0 -> Portable commit`

真实运行结果：

| 项目 | 结果 |
|---|---|
| Job 终态 | `succeeded` |
| 页数 / 结构块 | `4 / 111` |
| Draft / normalized / Evidence / quality / source metadata | 5 个 Artifact 全部生成 |
| Portable semantic validation | 通过 |
| Source SHA-256 前后 | 完全相同 |
| 冷进程完整纵切 | 15.3 s（首次产品运行） |
| 同源第二次产品运行 | 13.5 s；复用同一 Source ID，生成独立 Bundle |
| Runtime 重开 | 只读恢复同一 Job/Result，不调用解析器 |
| 默认 Evidence 呈现 | canonical Draft 保留 111 个审计脚注；`reading` 投影隐藏系统引用和定义 |

最终正式运行 Job 为 `job_019fb3d1-ed49-730e-bde4-acb20ad706eb`，Bundle 为 `bnd_019fb3d1-ed49-7dee-b1fa-105731676182`。运行工作区与 machine state 位于仓库外 `G:\.alltonote-x0b-real`，不属于可提交产品内容。

## 8. X0-B 关闭证据

1. SQLite schema v2 为每个 Job 持久化精确 Recipe、executor 和 Pack identity；v1 先做精确旧 schema 校验，再在事务内迁移和回填；重复打开、foreign key 与 integrity check 通过。
2. 新 Recipe 结果使用 `result_schema_version=1` 与 `result_kind`，Artifact 只以角色映射进入通用 Job 结果；page/bbox、Docling label 和 PDF metadata 留在 Document Artifact。
3. legacy Video result JSON 保持原 wire 并 dual-read；其兼容类型移入 Job 层 `legacy_video_result`，通用 SQLite Repository 不再导入 Recipe domain。
4. `commit_recipe_result_atomic` 将 Source identity、Result 与 Job terminal transition 置于同一 SQLite commit guard；Portable rename 后、SQLite commit 前注入崩溃，重启只得到一个有效 Bundle，最终 `idempotent=true`。
5. 解析器崩溃时 Job 保持可恢复且没有 Result/commit；重启后从安全边界继续。已完成解析和 candidate checkpoint 不重复执行。
6. exact reconnect 只按持久化 binding 路由；缺失 Pack 版本返回 `job_executor_unavailable`，不会重新 submit 或猜测当前默认实现。
7. 同一文件的后续 Job 复用已经原子提交的 Source ID；不会通过放宽 identity 唯一性制造多个 Source。

## 9. `document-basic` 实用硬化

2026-07-31 在不引入通用 Pack 管理器、安装器或新插件抽象的前提下，补齐首个正式 PDF 路径的两个真实缺口：

1. Pack 代码固定声明 `docling-slim==2.117.0`、`scipy==1.17.1`、CPU `torch==2.13.0+cpu`，以及后续真实表格样本证明必需的 `numpy==2.2.6`、`opencv-python-headless==4.12.0.88`；版本不再由调用方自由覆盖。
2. `create_document_runtime` 在创建 machine state 前运行隔离 doctor。doctor 强制离线，实际导入全部直接运行依赖，并校验 layout 与 TableFormer 的固定 revision、配置和权重 SHA-256；缺失或错配时返回 `document_pack_invalid`，不进入 Job。
3. 解析 worker 继续使用参数列表、独立 Python、120 秒超时和 64 MiB 输入上限；stdout/stderr 直接写入 `DEVNULL`，避免第三方日志被无界收集到主进程内存。
4. 同一真实 PDF 经硬化后的正式 Runtime 再次离线通过：Job `job_019fb3e4-a390-72f8-a011-afad9bd4d6a9`、Bundle `bnd_019fb3e4-a390-7d39-901a-d97623664524`，仍为 4 页、111 块、5 类 Artifact，源 SHA-256 不变。验证状态位于仓库外 `G:\.alltonote-x0b-pack-doctor`。
5. 最新聚焦回归 276 项通过；完整 backend 回归为 `1932 passed, 2 skipped, 3 subtests passed`。唯一 warning 仍是既有 ctranslate2 对 `pkg_resources` 的弃用提示。

本轮不实现 Windows Job Object、通用 `pack install/repair`、许可证聚合、PPTX、OCR 或多格式矩阵。这些属于分发或后续真实输入 Gate，不应为了包装成熟的 Docling 而提前扩展 AllToNote。

## 10. 中文与表格密集 PDF 验证

第二份真实输入为 `E:\Note\J3BakeGI\J3BakedVolumetricGI技术分析精简版.pdf`：588,906 bytes、6 页、SHA-256 `cc2f703aaf3e1fbb9172304a16598a4387b9f90939ffc0eeef013aca62ba77b1`。PDFium 对第 1、5、6 页的视觉抽查确认中文、代码文字和多张表格清晰，无裁切或乱码。

该样本连续暴露并关闭了两个真实缺口：

1. 初次正式 Job `job_019fb3f2-1522-7c8d-96d2-00f03dc93152` 在解析前失败。根因是 Windows 下 `docling-parse` 错误转码中文文件路径，不是 PDF 内容损坏。adapter 现在把不超过 64 MiB 的已校验输入复制到既有临时目录的 ASCII `input.pdf`，解析后恢复原始中文文件名；源文件与 Evidence identity 始终使用原 SHA-256。
2. 路径修复后的旧 Pack 虽然生成成功 Job `job_019fb3f3-fc4c-7890-9249-90399f22fc5b`，但得到 640 块，其中 579 个普通文本块；8 张表均退化为只有 2 行的单大单元格 Markdown，并与表内文本重复，不能判为表格通过。
3. 启用 Docling 官方 TableFormer V1 `accurate` 后，固定模型 `docling-project/docling-models@fc0f2d45e2218ea24bce5045f58a389aed16dc23`（tag `v2.3.0`），不新增 AllToNote 表格 heuristic。Pack identity 升级为 `document-basic@docling-2.117.0-tableformer-v2.3.0`。
4. 最终正式 Job `job_019fb3fb-1adb-76c1-a1dc-1758729e5185`、Bundle `bnd_019fb3fb-1adb-7b22-b6c9-8c2745926863` 成功：6 页、109 块、8 张表分别恢复为 5/16/8/11/8/8/6/6 行 Markdown，5 类 Artifact 完整，中文原文件名和源 SHA-256 不变。
5. 原英文双栏论文用新 Pack 回归为 Job `job_019fb3fc-ba8c-75ac-81c0-4ff2905ca6ea`、Bundle `bnd_019fb3fc-ba8c-71ff-a966-0e24936b6d19`：4 页、82 块、无 warning、源文件不变。
6. 中文路径单元回归、Document/X0-B 聚焦回归和完整 backend 回归全部通过；`git diff --check` 无 whitespace error。

验证状态与中间模型均位于仓库外 `G:\.alltonote-doc-real-j3bakedvolumetricgi` 和 `G:\.alltonote-spike-envs\docling-models-tableformer-2.117.0`。仓库不提交用户 PDF、解析全文或模型权重。

## 11. Document Evidence 默认呈现修复

用户对中文表格样本的真实阅读暴露出一个 AllToNote 呈现问题：Document Bundle 装配器把每个解析块生成为 Markdown 系统脚注，并在文末追加同等数量的 `Document page N` 定义。Docling 只提供 block、page、bbox、kind、text 与 reading order；这些脚注和定义由 AllToNote 自己引入。

本轮按最小职责边界修复：

1. `primary_draft` 是默认人类阅读入口，只保留标题、正文、表格和 caption，不再输出 `[^ev_*]` 或页尾 Evidence 定义；
2. `evidence.reference-set.v1` 仍是 Document 的审计入口，每个 normalized block 继续保留独立 Evidence ID、source revision、page、归一化 bbox、excerpt SHA-256 与 basis；
3. Draft 的 Artifact parents 仍绑定 normalized content 和 EvidenceSet，因此没有删除来源、定位或完整性证据；
4. 不引入隐藏 HTML 注释、第二份审计 Markdown 或新的 Document presentation 协议。旧 Bundle 保持不可变，只有新 Bundle 使用干净呈现。

真实回归结果：

| 样本 | 新 Job / Bundle | Draft 系统脚注 | Evidence 记录 | 结果 |
|---|---|---:|---:|---|
| 中文表格 PDF | `job_019fb412-b713-72eb-88e4-54426cc57c0c` / `bnd_019fb412-b713-79e0-9ac3-a7baeee9edfb` | 0 | 109 | 6 页，表格与正文保留，源 SHA-256 不变 |
| 英文双栏 PDF | `job_019fb413-b693-7599-a8c4-52714b6bd0d8` / `bnd_019fb413-b693-7915-a07f-d08a8889dbf1` | 0 | 82 | 4 页，无 warning，源 SHA-256 不变 |

第 7 节“canonical Draft 保留 111 个审计脚注”仍是当时旧 Bundle 的历史事实，不回写或伪装；当前合同以上述新 Bundle 为准。
