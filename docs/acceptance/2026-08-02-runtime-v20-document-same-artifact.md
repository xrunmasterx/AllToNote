# Runtime V20 Document 同制品验收

日期：2026-08-02

状态：当前 Windows 用户候选通过；不是公开安装包或干净虚拟机认证。

机器可读记录：[`2026-08-02-runtime-v20-document-same-artifact.json`](2026-08-02-runtime-v20-document-same-artifact.json)

## 1. 结论

同一个 V20 Runtime 候选完成了两份真实 born-digital PDF 的 Document Recipe 全流程：英文图形学论文 4 页，以及包含中文与多张表格的技术文档 6 页。两项 Job 都从隔离 machine state 和全新 Unicode Workspace 启动，使用同一个正式签名 `document-basic` Pack，最终均为 `quality=pass`、`publish_eligible=true`。

这轮直接验证了用户此前指出的 Evidence 呈现问题已经在当前制品中关闭：Evidence Set 继续保留逐块、逐页、带 bbox 和摘要哈希的审计事实，但 reading Draft 中没有 `[^ev_*]`、`Document page` 或连续数字引用噪声。换言之，Docling 负责结构化解析，内部 Evidence 负责可追溯性，而默认阅读投影只呈现面向人的正文；三者没有被混成一层。

英文 Draft 是一篇围绕实时光泽反射与两级辐射度缓存的完整中文技术笔记；中文 Draft 保留了 `J3BakedVolumetricGI`、`UJ3BakedVolumetricGIData` 等关键专有名词和表格信息。两份正文均完整返回，没有截断。

## 2. 固定制品与环境

- Runtime 源码提交：`68d517f1fb5e0ce79429c36e56cb7b3c2acbd447`。
- Runtime wheel：601,241 bytes，SHA-256 `ea2228ca8d4e4bb3203ba73d93eec2e425e42bf86508af108c9ff50f1da62792`。
- 可移植候选：715 files / 41,084,745 bytes；`release/file-manifest.json` SHA-256 `c1369d1603d2248ffc1bb07d028e7a1e860873b1289dc130640e52a6cbed1956`。
- Runtime 候选在 Job 完成和 Engine 重启后重新执行逐文件 verifier，仍为 `candidate-pass`。
- `document-basic@docling-2.117.0-tableformer-v2.3.0`：manifest `sha256:7b72fe809a18ca62a2d7d80122a8350314f6bd9b8a4c56dba16ec17725e90d0f`，签名 key ID `alltonote-document-basic-2026-01`。
- Document Pack 首次安装成功，安装前缺失，安装后静态与动态 doctor 均通过；重启后动态 doctor 仍通过。
- Runtime Python 3.14.6；Document Pack Python 3.11.15；SQLite 3.53.4。
- 隔离验收目录最终为 33,848 files / 1,701,274,530 bytes。
- 执行容量固定为 1；两个 detach Job 连续提交，中文 Job 先记录 `resource_capacity` 等待，再在英文 Job 完成后获得 admission，没有并行重载 Docling/模型执行。

## 3. 配置与模型边界

- Composer：`codex-app-server` / `gpt-5.6-sol`。
- 独立 verifier：`codex-app-server` / `gpt-5.6-terra`。
- 输出：`zh-CN`、`balanced`、`structured`。
- 两个 Job 共留下 8 个模型操作结果：4 个 composer、4 个 verifier；每份文档经历一次受控 quality repair 后通过。
- Provider 未返回可信 token usage，finish reason 也标为 unavailable，因此本记录不声称精确 token 数量或模型成本。

## 4. 真实结果

| 样本 | Job / Bundle | 实际执行耗时 | 结构化解析 | Reading Draft | 结果 |
| --- | --- | ---: | --- | --- | --- |
| 英文论文 | `job_019fc0ab-3eba-715a-87ea-d3e208e25870` / `bnd_019fc0ab-3eba-7976-a2cb-c6a53600eb3e` | 465.616 s | 4 页 / 82 blocks / 1 table | 9,389 bytes / 7 headings | pass；publishable |
| 中文表格文档 | `job_019fc0ab-6fc4-798d-8481-2cd65bbde0bb` / `bnd_019fc0ab-6fc4-7aca-a59f-aae6f01d82de` | 462.733 s（另有容量等待） | 6 页 / 109 blocks / 8 tables | 11,772 bytes / 9 headings | pass；publishable |

英文 Bundle manifest 为 `sha256:94b51f196f2b6c605e3f18beef2fbbb762542e78f542dce4aa5a468609a2c210`，Draft 为 `sha256:fdf491c23965cb42320144fe51e8f6d59e58537b0d50d62bef079549b50e3792`。

中文 Bundle manifest 为 `sha256:fce2bdfc81acc1ce7d3f64f91d9d091b30d0f219786b0678b7152c5108e505e3`，Draft 为 `sha256:3c07f9937de121364b34e4f5d72a783c8f1ea12a1a2e78145e14d97cc1a5bd9c`。

## 5. Evidence 与语义质量

英文结果：

- 82 个 normalized blocks 对应 82 条 Evidence，覆盖第 1–4 页；Knowledge Map 有 48 项。
- page、normalized bbox、excerpt SHA-256、locator scheme、Knowledge Map block 引用全部有效，Evidence ID 无重复。
- page coverage 为 1.0，source coverage 为 0.7432009391508511。
- reading Draft 中系统 Evidence 标记、`Document page` 标记和数字引用串均为 0。

中文结果：

- 109 个 normalized blocks 对应 109 条 Evidence，覆盖第 1–6 页；Knowledge Map 有 58 项。
- 8 张表格保留为结构化 table blocks；page、bbox、摘要哈希、locator 和 Knowledge Map 引用全部有效，Evidence ID 无重复。
- page coverage 为 1.0，source coverage 为 0.8906131373561299。
- reading Draft 中系统 Evidence 标记、`Document page` 标记和数字引用串均为 0。
- 正文保留关键模块与类型名称，没有回退到此前讨论过的错误泛化名称。

## 6. 重启恢复与不变性

- 初始 Engine ID 为 `a3bb9668-1525-4747-8f78-455cdc50ba90`；停止后重新 `ensure` 得到 `3d5d7752-e79c-4545-a85b-b9f091345c19`。
- 重启后两个 `job wait` 直接返回原 succeeded 结果；英文事件保持 17 条、中文保持 19 条，最大 sequence 分别保持 17/19。
- 模型操作结果文件保持 8 个，没有重新调用 composer/verifier；Bundle、commit、Draft ID 与 SHA 均未变化。
- 重启后的 `draft show` 与 `review show` 返回同一 Draft SHA、同一 pass 质量报告和同一发布准入结果。
- 两份源 PDF、Runtime file manifest、签名 Pack manifest 与已安装 Pack `active.json` 的运行前后 SHA-256 完全一致。
- Engine 最后再次正常停止。

## 7. 不得外推的结论

本记录不证明扫描件/OCR、PPTX、DOCX、macOS、capacity=2、多用户、干净 Windows 账户、Defender、公开 Runtime 签名、安装器、在线 Pack 分发、更新、回滚、卸载或长期性能容量。它也没有新增真实 Document cancel/retry 场景；这些路径由现有自动化测试覆盖，但不属于本次双 PDF 同制品实跑证据。

当前最准确的产品结论是：V20 当前用户候选已经用正式签名 Document Pack 和两份真实 PDF 证明了 Document Recipe 的可读输出、内部 Evidence 可追溯性、中文/表格解析、独立语义验证、capacity=1 串行调度和 Engine 重启可恢复性；公开发布生命周期仍需后续工作。
