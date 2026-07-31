# Document PDF 输入准入边界

日期：2026-07-31

本轮只收紧 `alltonote.document-note@1` 已支持的首个 born-digital PDF
纵切，不扩展 OCR、PPTX、URL、加密 PDF 或长文档能力。

在计算全文件 SHA-256 和创建 durable Job 之前，Recipe Adapter 现在按顺序确认：

- 输入解析为现存的常规文件；
- 文件扩展名为 `.pdf`；
- 文件长度至少为 5 bytes，且不超过 64 MiB；
- 文件前 5 bytes 为 `%PDF-`。

不满足这些条件时返回稳定的 `document_input_unsupported /
invalid_request`，不会读取完整文件，也不会创建 Job。64 MiB 是当前
born-digital PDF 纵切和 Docling worker 共用的能力边界，不代表所有 PDF、
长文档或未来 Document Recipe 的永久产品上限。

检查、magic、实际读取上限和 SHA-256 绑定到同一个打开文件句柄；哈希结束
后再复验文件身份、长度和修改时间，并把这一最终状态写入 Job 请求。增长中
的文件最多读取到第 64 MiB + 1 byte 就会停止，不会无界追随 EOF。

验证覆盖错误扩展名、错误 PDF magic、逻辑长度超过上限的稀疏文件、伪装为
`.pdf` 的目录，以及 magic 检查后继续增长的文件；每个用例都证明
DocumentService 未收到提交。解析 worker继续在隔离进程前重复验证同一
边界，作为执行时防线。

本轮没有声明对恶意并发替换文件路径、PDF 对象数量/解压膨胀、页面数量、
解析器内存或 CPU 的完整资源隔离已经完成；这些仍属于 DOC-01、DOC-13 和
后续真实长文档/恶意文档 Gate。
