# ADR-0002：Docling 只作为可替换的 Document Parsing Engine

- 状态：Accepted
- 日期：2026-07-30
- 作用域：`REC-DOC-001`、`RUNTIME-001`、X0-B

## Context

首个真实 Document 纵切使用 4 页双栏 born-digital PDF。`pdfplumber==0.11.8` 能快速提供原生文字和 bbox，但真实输出出现词间空格丢失、左右栏交错，且没有标题、章节、图注或表格语义。若以它直接完成纵切，AllToNote 必须自行实现布局、阅读顺序、表格与图注启发式。

`docling-slim[format-pdf,models-local]==2.117.0` 在同一文件上恢复了 14 个结构标题、13 个图注和 1 个表格，双栏正文可读，所有 111 个归一化块均有合法 page/bbox；代价是约 5.4 秒解析、约 1.47 GB 峰值工作集、约 975 MB Python 环境和约 172 MB layout 模型。Windows 安装还暴露了未声明的 `scipy` 导入依赖。

## Decision

首个 PDF 纵切选择 Docling 作为 `document-basic` Pack 内固定版本的解析引擎，并在 AllToNote anti-corruption adapter 后归一化为私有、解析器无关的 Document DTO。

AllToNote 继续独占以下权威：原文件完整 SHA-256、Source/Revision、DocumentBlock/Evidence ID、Job/Attempt/Checkpoint、Quality、Artifact/Bundle、原子提交、迁移与重连。`DoclingDocument`、Docling 内部对象 ID 和 Docling JSON 只能作为诊断数据，不是 durable domain schema 或 Evidence 权威。

正式产品路径必须通过可选、固定版本、离线可准备的隔离 worker/Pack 执行 Docling；不得把 Torch、模型或 Docling 导入最小 Runtime 冷路径。当前受信任 PDF 的进程级 Spike 只证明解析质量，不放宽恶意文档隔离 Gate。

## Consequences

- 获得现成的布局、阅读顺序、标题、图注和表格语义，避免 AllToNote 重复造解析器。
- 必须显式管理约 1 GB 运行环境、模型下载/许可证、CPU/内存预算和 `scipy` 依赖缺口。
- 未安装或版本不匹配时 preflight 必须 fail closed，并报告所需 Pack；不能联网临时拉模型后继续。
- `pdfplumber` 保留为 DOC-00 对照基线，不进入首个产品纵切；未来只有真实输入证明更简单时才重新决策。

## Evidence

- [`Document born-digital PDF + X0-B Spike 报告`](../design-docs/document-born-digital-pdf-x0b/report.md)
- [`冻结 fixture manifest`](../design-docs/document-born-digital-pdf-x0b/fixture.json)
