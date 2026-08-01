# Document detach 输入快照验收

日期：2026-08-01
范围：`alltonote.document-note@1` 的 Engine-owned 本地 PDF 输入

## 结论

通过。`--detach` 返回前，PDF 已复制到 machine state；提交进程退出或原文件随后被删除，独立 Worker 仍能依据同一持久 Job 完成生产。Foreground Document 行为不变。

## 冻结合同

- 快照在 Job 创建前完成；失败时不创建 Job。
- 原请求继续保存原始文件作为来源事实，不把 machine-state 路径写入请求或 Portable Bundle。
- `document.input-snapshot.v1` 只含 `schema_version`、`sha256`、`byte_length`，与 Job 在同一 SQLite 事务创建。
- 绑定存在时，快照是唯一执行输入；缺失、内容变化、hardlink、symlink/junction/reparse 或受控目录逃逸均在 Parser 前失败，不回退原文件。
- 同一摘要使用持久 machine-state 文件锁串行发布；并发提交复用一个内容寻址快照。
- retry 显式继承绑定；历史无绑定 Job 只使用其原始输入，不采用磁盘上碰巧存在的同摘要快照。

## 验证

- 删除原 PDF 后执行成功，且 stored request 仍保留原始来源名。
- 篡改、删除、leaf link/hardlink、祖先 reparse 和不安全锁对象均失败关闭。
- 8 路线程内相同输入提交重复运行 10 轮，以及 4 个 Windows `spawn` 提交进程同时竞争，均全部成功且物理快照唯一。
- 取消后 retry 在原文件已删除时成功；历史无绑定 Job 不越权采用新快照。
- Windows `spawn` E2E 在提交进程退出后删除原 PDF，独立 Engine Worker 仍完成 Job。
- 后端全量回归：`2505 passed, 3 skipped, 3 warnings, 3 subtests passed`。

## 未扩大的范围

本增量没有实现快照 GC、通用 blob store、Video 输入快照、远端缓存、batch 或并行 Worker 池。真实 Docling Pack、真实外部模型、签名 Runtime 与 clean non-admin VM 仍属于发布验收，而不是本合同的一部分。
