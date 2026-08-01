# Video detach 本地输入快照验收

日期：2026-08-01
范围：`alltonote.video-note@1` 的 Engine-owned 本地视频输入

## 结论

PASS。Engine-owned 本地视频在 Job 创建前被复制到 machine state；提交进程退出或原文件随后被删除时，独立 Worker 仍可依据同一个持久 Job 完成生产。Foreground、远程 URL 和历史无绑定 Job 的行为保持不变。

## 冻结合同

- 只有 `execution_owner=engine` 且词法上存在的安全本地普通文件会在提交阶段创建快照；远程 URL、fixture 和 foreground 不进入该路径。
- 快照先完成流式复制、SHA-256 计算和完整校验，再创建 Job；输入不安全或快照存储失败时不留下 Job。
- `video.input-snapshot.v1` 只包含精确的 `schema_version`、`sha256` 和 `byte_length`，不保存原路径或 machine-state 路径，并与 Job 在同一提交事务中绑定。
- 快照按内容摘要寻址。同摘要目录使用稳定、拒绝 symlink/reparse/hardlink 的跨进程文件锁串行发布；并发提交复用一个物理快照。
- 绑定存在时，执行前重新校验受管快照，随后只把该快照交给 local source adapter；缺失、篡改或身份不匹配均在 source resolve/acquire 前失败，不回退原文件。
- retry 子 Job 显式继承绑定；历史无绑定 Job 只使用其原始输入，不因磁盘上恰好存在同摘要快照而越权采用。
- 内部快照名 `source.mp4` 不进入人类标题；最终 Portable metadata 继续使用原始文件名派生的标题。
- 原始 Video request、request hash 和 checkpoint schema 不因受管路径而改变。

## 验证

- Engine 本地视频提交后删除原文件，foreground worker 与独立 Windows `spawn` Worker 均完成 Job。
- 篡改受管快照后，在 resolver、acquire 和 transcriber 调用前 fail closed，原文件存在也不回退。
- 8 路线程并发提交连续执行 3 轮，Job ID 均唯一，物理内容快照均唯一。
- 取消后 retry 在原文件删除后成功；历史无绑定 Job 不采用已有快照。
- 远程 Engine 提交和 foreground 本地提交均不创建绑定或快照。
- hardlink 输入与注入的快照存储失败均在 Job 创建前拒绝。
- 领域合同拒绝重复 JSON key、布尔 schema、额外字段、非法摘要和负字节数。
- 定向回归：`148 passed`。
- 完整 backend 回归：`2525 passed, 3 skipped, 3 warnings, 3 subtests passed`。
- 独立只读 P0/P1 Gate：PASS，0 P0 / 0 P1。

## 未扩大的范围

本增量没有实现快照引用计数或 GC、远程 URL 内容快照、batch、并行 Worker 池、资源准入或 queue position；`parallel_job_execution_enabled` 仍为 `false`。同一用户恶意并发改写目录命名空间的 handle-level 防护，以及把 Document/Video 快照代码抽成公共原语，保留为 P2 后续，不作为本合同已证明的能力。正式签名 Runtime/Pack、clean non-admin VM、安装/更新/回滚/卸载仍属于 Release Gate。
