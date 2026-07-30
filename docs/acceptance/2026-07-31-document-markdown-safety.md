# Document 不可信 Markdown 呈现验收

日期：2026-07-31
范围：`alltonote.document-note@1` 的主 Draft 装配与发布资格判定
结论：通过；仅关闭不可信 PDF 文本获得 Markdown 权限这一项 P0

## 根因与修复边界

Docling 输出的是解析块、页码、bbox、类型和文本。此前 AllToNote 把 `block.text` 直接拼接进主 Draft，因此用户 PDF 中的标题、链接、图片、HTML、列表或 Mermaid fence 会被 Markdown 渲染器解释。Evidence 脚注和页尾定义同样是 AllToNote 自己加入的，不是 Docling 的产品呈现。

修复后的不变量：

- 人类 Draft 中的解析文本默认只有“可见字面量”权限；
- 裸 HTTP(S)、`www` 与邮箱文本以可读、可复制的行内代码呈现，避免 GFM 在转义解析后重新生成活动链接；
- Docling 表格保留行列结构，单元格文本没有活动 Markdown 权限；
- normalized content 与 EvidenceSet 保留 exact raw text、page、bbox 与 hash；
- 最终 Draft 必须通过既有全局 Markdown safety validator；
- 最终验证失败时必须 fail closed，Quality Report、Produce result 与 receipt 一致为 `overall=fail`、`publish_eligible=false`。

## TDD 与回归证据

- RED：新增恶意 PDF block 与最终 validator 拒绝测试后，原实现出现 2 个失败；原始 Mermaid/链接结构可进入 Draft，且没有最终 fail-closed 判定。
- GREEN：Document/Portable 聚焦回归 `298 passed`。
- 完整 backend 回归：`1940 passed, 2 skipped, 1 warning, 3 subtests passed`；warning 是既有 `ctranslate2` 对 `pkg_resources` 的弃用提示。
- `git diff --check`：通过。
- 独立只读复审：恶意链接/图片、GFM 裸链接、单字符 Setext、表格管道实体和 fail-closed 一致性均无剩余 P0/P1。

## 真实 PDF 验证

| 样本 | Source SHA-256 | Job / Bundle | 结果 |
|---|---|---|---|
| `J3BakedVolumetricGI技术分析精简版.pdf` | `cc2f703aaf3e1fbb9172304a16598a4387b9f90939ffc0eeef013aca62ba77b1` | `job_019fb442-b43d-72e5-828b-60de435bb97f` / `bnd_019fb442-b43d-7692-9509-0575e1b91ff5` | 6 页、109 块、8 表格；safety pass；raw text/hash 往返一致；publishable |
| `SA2023_RealTimeReflection.pdf` | `155f56096e8196b08f0aab9d6a162daea0196d308ad323ab1aebc7fb749db6b1` | `job_019fb443-e458-75d4-9cd7-bce1336a0d38` / `bnd_019fb443-e458-7027-a054-a5f9d056d917` | 4 页、82 块；6 个 URL/邮箱均为行内代码；raw text/hash 往返一致；publishable |

两个新 Draft 的 Evidence 脚注和 `Document page` 标签均为 0；Portable inspection 与 Markdown safety validation 通过。源文件验证前后 SHA-256 不变。首次中文验证脚本因 PowerShell 管道内联中文路径转码而在产品执行前失败，改为通过 Unicode 环境变量传递路径后完成；该 harness 失误没有修改源文件或产品工作区。

## 未关闭事项

本验收不代表整个 Document 产品已经可发布。以下事项仍未关闭：

- 默认 CLI/Pack 安装与发现路径，以及通用 Document result 投影；
- 扫描件、OCR、figure/page 覆盖与 partial/empty page 质量判定；
- Docling worker 的 OS、网络、环境变量与用户权限隔离；
- 取消、deadline、候选目录抗投毒与敏感输入持久化边界；
- SQLite/Job authority 与高并发资源准入、隔离和负载验证。

因此，本轮只证明“已支持的 born-digital PDF 不会让未信任文本取得活动 Markdown 权限，并且安全失败不会被标记为可发布”。
