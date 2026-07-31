# Document signed Pack 与当前 Runtime 真实 PDF 验收

日期：2026-07-31

范围：当前 `codex/video-dogfood-validation` Runtime wheel、正式签名的
`document-basic@docling-2.117.0-tableformer-v2.3.0`、默认
`alltonote.document-note@1` Recipe，以及两份真实 born-digital PDF。

结论：本轮定义的本地 Windows x86_64 验收通过。签名 Pack 可以从空的隔离
machine state 安装、重复安装、动态 doctor，并通过同一 ProduceService
依次生成英文双栏论文和中文表格文档。两份输入的原文件哈希不变，Portable
Bundle、Evidence、Draft、Quality、Source 与 Revision 身份齐全。

这不是完整 Windows 公开发行通过：本轮使用已安装 wheel 的 Python 解释器和
显式注入的隔离 `RuntimePaths`，没有证明独立安装器、全新 Windows 用户/VM、
默认 `platformdirs` 路径切换或 Authenticode。

## 冻结输入

| 项目 | 身份 |
|---|---|
| Runtime commit | `2cd0aa1bad4a99f35c125f2c6b9f542f12fbb30b` |
| Runtime wheel | `alltonote_runtime-0.1.0-py3-none-any.whl` / SHA-256 `6616b3a2c86f4677abb04992a5dde9da6d4e805fdf5745d40d999f0a4e6c2d85` |
| iWiki wheel | `llm_iwiki-0.1.2-py3-none-any.whl` / SHA-256 `f2207cfe306416f99f4d378067d8e16d87723879c205cbf36fcdfb388a5ada59` |
| Document Pack | `document-basic@docling-2.117.0-tableformer-v2.3.0` |
| Pack manifest | `sha256:7b72fe809a18ca62a2d7d80122a8350314f6bd9b8a4c56dba16ec17725e90d0f` |
| English PDF | SHA-256 `155f56096e8196b08f0aab9d6a162daea0196d308ad323ab1aebc7fb749db6b1` |
| Chinese/table PDF | SHA-256 `cc2f703aaf3e1fbb9172304a16598a4387b9f90939ffc0eeef013aca62ba77b1` |

Runtime wheel 与 iWiki wheel 的独立内容验证均通过。完整 wheelhouse release
verifier 没有被记为通过：本地 `llm-iwiki-0.1.2` build source 目录缺少自己的
`.git`，Git 向上解析到了 AllToNote commit，与 `runtime-lock.json` 中固定的
iWiki source commit 不一致。这是本地 release source/provenance 准备缺口，
不是 wheel 内容验证成功可以替代的 Gate。

## 隔离安装与 doctor

验收从不存在 `document-basic` 的隔离 machine state 开始：

| 操作 | 结果 | 用时 |
|---|---|---:|
| `pack doctor document-basic` | `installed=false`、`healthy=false` | 0.010 s |
| 首次 `pack install document-basic` | `result=installed`，签名、文件、doctor 与激活通过 | 125.285 s |
| 重复安装相同 Pack | `result=already_active` | 153.196 s |
| `pack doctor document-basic --dynamic` | static/dynamic 均为 `pass` | 9.539 s |
| `workspace init` | 创建可写 V2 Workspace | 0.023 s |

每个 JSON 结果都是 stdout 上恰好一行，stderr 为空，退出码为 0。安装使用正式
信任根和正式签名目录，没有使用 `ALLTONOTE_DOCUMENT_BASIC_PYTHON` 或
`ALLTONOTE_DOCUMENT_BASIC_ARTIFACTS` 开发覆盖。

重复安装仍需约 153 秒，因为当前实现会重新物化并校验 1.58 GiB 左右的来源与
generation。结果正确且幂等，但交互性能不理想；在不削弱签名、文件哈希和
immutable generation 语义的前提下，需要单独优化 fast no-op 路径。

## 两份真实 PDF

| 输入 | 用时 | Portable 结果 |
|---|---:|---|
| 英文双栏论文 | 20.218 s | 4 页、82 个 block、1 张表、83 条 Evidence 记录（含 1 条 header）、quality `pass` |
| 中文表格文档 | 25.643 s | 6 页、109 个 block、8 张表、110 条 Evidence 记录（含 1 条 header）、quality `pass` |

英文结果：

- Bundle：`bnd_019fb6a5-5dd6-777b-9eb6-c80c173f8b2b`
- Draft：`art_019fb6a5-5dd6-755e-855a-d030d32423ff`
- EvidenceSet：`art_019fb6a5-5dd6-74df-834b-db51653031e6`
- Source：`src_019fb6a5-5dd6-7ce0-a9ab-c48bf51bef66`
- Revision：`rev_019fb6a5-5dd6-7663-8dcf-9f4a0bf4239e`

中文结果：

- Bundle：`bnd_019fb6a5-d45f-7202-96de-00205d410ea9`
- Draft：`art_019fb6a5-d45f-7727-af64-a281e5fe3060`
- EvidenceSet：`art_019fb6a5-d45f-7137-bf48-f8a122cf85da`
- Source：`src_019fb6a5-d45f-795a-b301-79cd7f881403`
- Revision：`rev_019fb6a5-d45f-768c-95c3-7b11e2eafba3`

两份主 Draft 的 `Document page` 标签和 `[^ev_]` 系统 Evidence 标记均为 0；
Evidence 仍作为独立 Artifact 保留 page、bbox、raw text 与 hash。中文标题按
UTF-8 字节读取为 `J3BakedGI 模块概览`，表格和正文码点正确。PowerShell
默认控制台展示曾把正确 UTF-8 显示成乱码；字节级 JSON/UTF-8 复核排除了产品
数据损坏。

两份源 PDF 在执行前后的 SHA-256 完全一致。结果 JSON 同时返回
`source_id`、`source_revision_id` 与 `evidence_set_artifact_id`，foreground
CLI 完成后给出 `alltonote draft show <draft-id>` 的明确阅读入口。

## 代码与回归 Gate

- foreground CLI、result identity 与 Draft handoff 聚焦测试：`50 passed`；
- CLI/contracts 回归：`140 passed`；
- 完整 backend：`2170 passed, 2 skipped, 3 warnings, 3 subtests passed`；
- 独立 Gate Review：无剩余 P0/P1；
- `git diff --check`：通过。

三个 warning 为既有依赖/正则弃用提示，不是本轮失败。

## 尚未关闭

以下事项继续作为后续发布 Gate，不由本次通过替代：

- 在新的非管理员 Windows 用户或 VM 中，用最终安装器/onedir 运行，不依赖
  checkout、现有 Python 或测试注入；
- 修复并证明 iWiki build-source provenance，使完整 Runtime wheelhouse
  verifier 通过；
- 为相同已激活 Pack 提供可信且快速的 no-op 安装路径；
- 把 Document 的逻辑 Source 身份与内容 Revision 身份分开，避免相同内容的
  不同文件被误合并、同一路径修改后历史断裂；
- 实现真正的 Document Knowledge Note 编译与语义质量 Gate；后续语义收紧已
  将当前 native-extraction 结果固定为 `publish_eligible=false`，见
  [`Document 原生提取质量边界`](2026-07-31-document-native-extraction-quality-boundary.md)；
- Document worker 的取消、超时进程树收束，以及 SQLite 并发/锁竞争 Gate；
- 扫描 PDF、OCR、混合文档、长文档和 PPTX 仍未进入支持声明。

因此，本报告只证明：当前签名 Document Pack、当前 Runtime 与两个已冻结的
born-digital PDF，在隔离本地状态下形成了一条真实、可复核、对人可读的
foreground ProduceService 链路。
