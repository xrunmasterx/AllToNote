# Engine capacity-2 跨进程与 Docling 负载 Gate

日期：2026-08-02

## 结论

capacity 2 的两类关键证据通过，但仍不构成公开默认启用声明：

1. Windows Engine Host 可以在同一 Workspace 上同时监督两个真实 OS Worker process；第三个 Job 在资源边界等待，一个 Worker 非零退出后，第三个 Job 补位、未崩溃同伴继续运行，失败 Job 随后在既有 launch budget 内由新 Worker 恢复；
2. 两个真实 Docling doctor + born-digital PDF parse 可以并行完成，墙钟吞吐明显优于顺序执行，但进程树峰值聚合 RSS 达到 3.015 GiB。

因此，`maximum_active_workers=2` 的实现方向成立，跨进程正确性与解析层吞吐收益已有直接证据；Engine Host 默认容量继续保持 1，`parallel_job_execution_enabled=false` 继续保持不变。没有可用内存准入和最终 portable Runtime/签名 Pack 的整链复跑前，不对普通用户启用容量 2。

这里的产品结构是一套 `ProduceService` / Recipe Registry 生命周期承载 Video 与 Document 两条 Recipe，不是两套彼此独立的 ProduceService。该 Gate 验证的是两条 Recipe 共享的 Job、Engine、恢复和发布边界。

## Engine 真实进程 Gate

测试入口：`backend/tests/integration/test_video_detach_engine_e2e.py`。

新增场景分别覆盖 `alltonote.document-note@1` 和本地 Video Recipe：

- 在同一个中文加空格 Workspace 上提交三个 detached Job；
- Host 显式使用 `maximum_active_workers=2`；
- 任意 release 前恰好出现两个不同 PID 的 Worker，Host、提交进程和 Worker PID 相互独立；
- 第三个 Job 没有 Attempt、没有 Worker launch budget 消耗，保持 `queued` 并持久化唯一 `scheduler.waiting.v1`；
- 第一个 Worker 收到一次性非零退出注入后结束，第三个 Job 获得空出的 slot，并持久化唯一 `scheduler.admitted.v1`；
- 另一个原 Worker 在补位期间仍保持运行；
- 非零退出 Job 由新的 Worker PID 自动恢复；
- 原始本地 PDF/Video 文件在提交完成后删除，三个 Worker 仍从持久化输入快照完成；
- 三个 Job 最终全部 `succeeded`，每个预期 Recipe 操作恰好执行三次，同一 Workspace 中得到三个完整 `commit.json`；
- finally 路径释放所有 barrier，并通过 Engine stop 或进程终止收回 Host/Worker process tree。

完整 detached E2E 文件结果：`5 passed`。其中新增 capacity-2 参数化场景为 `2 passed`。

相关 Engine、Document 与 Video 矩阵结果为 `218 passed, 1 skipped`；完整 backend 回归为 `2642 passed, 3 skipped, 3 warnings, 3 subtests passed`。三条 warning 均为既有下载器转义弃用提示或第三方 `pkg_resources` 提示。

独立只读最终 Gate `task_086020d968ed` 复跑新增参数化场景 `2 passed`，结论为 P0/P1=0；该 Gate 没有复跑真实 Docling 测量，因此资源数字仍以本记录的原始测量为准。

该测试使用确定性的 fake source/parser/model adapter，以隔离调度、进程、SQLite、resource handoff、恢复和 portable publish 合同；它没有把 fake adapter 的速度或内存冒充真实 Recipe 性能。

## 真实 Docling 双并发测量

测量直接使用冻结的 Docling 2.117.0 / TableFormer 模型与用户提供的两份真实 born-digital PDF。两个独立 Docling parser process 同时执行离线 doctor 和 parse；外层只读采样 Windows 进程树工作集。

| 输入 | SHA-256 | 结果 | 单任务用时 |
|---|---|---|---:|
| `SA2023_RealTimeReflection.pdf` | `155f56096e8196b08f0aab9d6a162daea0196d308ad323ab1aebc7fb749db6b1` | 4 页、82 block、1 表 | 23.479 s |
| `J3BakedVolumetricGI技术分析精简版.pdf` | `cc2f703aaf3e1fbb9172304a16598a4387b9f90939ffc0eeef013aca62ba77b1` | 6 页、109 block、8 表 | 27.755 s |

聚合结果：

- 双任务墙钟：27.762 s；
- 峰值聚合 RSS：3,237,113,856 bytes，即 3.015 GiB；
- 峰值进程数：9；
- 内存采样数：227；
- 两份源文件前后 SHA-256 完全不变；
- 临时 parser snapshot 在受控临时目录中创建并在结束后清理；
- 使用离线变量和既有隔离 worker 合同，没有下载模型或访问网络。

此前同两份 PDF 的顺序 Produce 验收用时合计约 45.861 s；本次并行解析层墙钟为 27.762 s，说明容量 2 对重解析吞吐有实际收益。两次测量边界不同，不能把差值解释为严格端到端加速比，但足以否定“并发没有吞吐价值”。

测量主机具有 93.6 GiB 物理内存；测量后可用内存约 7.08 GiB。该主机通过不代表 8 GiB 或 16 GiB 用户设备安全。3.015 GiB 仅是这两个 parser process 树的峰值，还没有包含完整 Engine Host、两个知识模型请求、Video ASR、FFmpeg、Desktop 与其他应用的联合上界。

## 尚未关闭

- 用最终 portable Runtime 和正式签名 document-basic Pack，从 Engine Host 启动两个真实 Document Worker 并复跑上述两份 PDF；
- 双 Video 的真实 FFmpeg/ASR 负载，以及 Video + Document 混合负载；
- 记录完整 Engine/Worker/Pack/Provider 的聚合内存、CPU、临时磁盘和墙钟，而不只测 parser 层；
- 在可用内存不足时 fail closed 或保持 capacity 1 的主机准入策略；
- 预期的 SQLite busy、resource busy 或同 Workspace publish contention 需要结构化、带 authority 的可恢复结果；当前通用非零退出会消耗同一份三次 Worker launch budget，不能用容量 2 放大这一风险；
- 瞬时 heartbeat busy 的有界恢复，以及 Engine Host crash/restart、双 Worker cancel-vs-commit、stale Worker 与 successor、Windows kill-tree 与 Defender 干预矩阵；
- 使用项目明确准入的 SQLite 构建复跑；当前开发虚拟环境的 SQLite 3.50.4 低于 3.50 分支的 3.50.7 准入下限，只能作为源码回归环境；
- 非管理员 clean Windows、中文和空格用户目录、签名 Runtime/Pack、安装/更新/回滚完整发布 Gate。

本 Gate 的准确含义是：capacity 2 已从线程内模型推进到真实 OS Worker 正确性和真实 Docling 资源包络；它仍是默认关闭的内部能力，不是高并发公开发布完成。
