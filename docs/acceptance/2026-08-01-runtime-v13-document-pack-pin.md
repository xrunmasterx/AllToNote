# Runtime Windows V13：Document Pack 代际绑定与 Doctor 真实性 Gate

日期：2026-08-01

状态：本机 Windows x86_64 portable directory candidate 通过；仍不是签名安装包或公开发布认证。

## 结论

V13 将 Runtime 候选精确绑定到提交 `6d76f8ccfb480fd7d4711d2986bb4aa7bd1fb4ec`。该提交修复了两个直接影响发布真实性的问题：

1. Engine 模式的 Document Job 在提交时冻结 `document-basic` Pack 的精确版本、平台和 manifest digest；恢复执行时按该 digest 解析，不受后来 `active.json` 切换影响。
2. Runtime 发布装配不再只接受 `runtime doctor` 命令退出成功；还要求 `data.healthy=true`、存在检查项，且每项状态只能为 `pass` 或 `warn`。

V13 装配、候选内 Gate、发布后只读 verifier 和独立空机器状态 CLI smoke 均通过。SQLite 报告支持并行 Job 存储，但 `parallel_job_execution_enabled` 仍固定为 `false`；本记录不把存储能力表述为 ProduceService 已具备多 Job 并发。

## 固定身份

- Runtime 源码提交：`6d76f8ccfb480fd7d4711d2986bb4aa7bd1fb4ec`
- Runtime 版本：`0.1.0`
- Runtime wheel：`alltonote_runtime-0.1.0-py3-none-any.whl`
- Runtime wheel 字节数：`578649`
- Runtime wheel SHA-256：`0a6a404592f6198b99de0c30b5547de13665cdb6e50685917adc586d1acb4870`
- 候选目录：`G:\.alltonote-release\runtime-portable-document-pin-v13`
- 平台：Windows x86_64
- CPython：`3.14.6`
- SQLite：`3.53.4`
- SQLite source id：`2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc`
- payload 文件数：`710`
- payload 字节数：`40,950,089`
- `release/file-manifest.json` SHA-256：`16605cfffbcc43ac1aa0af9f37d5701f8ec4c92b14d67fd3b3ccf4419a6f1cd3`

manifest hash 由候选目录之外的本记录固定。发布后 verifier 使用该外部值重新扫描完整候选，并同时校验 source commit、平台、版本、acceptance、Runtime 输入、wheelhouse 和构建证明。

## 构建与安装证明

- 锁定 wheel：`15`
- wheel 源文件核对：`621`
- pip 允许生成的控制文件：`30`
- 最终安装文件核对：`666`
- 最终 `site-packages` tree SHA-256：`3b10ca0f80b188a0ff6a5e538a286c98bbceaef0ac8dd6678868925e19866228`
- Builder toolchain SHA-256：`e1e41b540264330c7915f87c3e6979e359ea40e95ff897d798f8ba220638efb0`
- wheel install attestation SHA-256：`1711918bd300398fb04aacdaa3b439899b839be85dac0461f0870b5cac6a3f74`

Builder 身份仍为锁定的 CPython `3.14.0`、`win32`、`AMD64`、64-bit；其可执行文件、运行库、pip tree 和完整 builder tree 均与 `runtime-windows-x86_64.lock.json` 一致。

## 真实运行 Gate

候选自身通过：

- `version --json`
- `runtime info --json`
- `runtime doctor --json`
- 中文和空格路径的 `workspace init`
- SQLite WAL 多连接 Gate：`1 / 4 / 8 / 16`
- schema v1 到 schema v5 的旧 JobStore 迁移、重开和数据一致性复核
- 20 次 Engine 冷启动，p95 `579.629 ms`
- Engine idle RSS `42,254,336 bytes`
- 32 路并发 `engine ensure` 收敛为单实例
- 父 CLI 退出后重连、强制终止后重新拉起、短 idle 自动退出及最终 stop

发布后在新的 `ALLTONOTE_MACHINE_STATE_ROOT` 下再次运行候选 CLI：

- Runtime 版本为 `0.1.0`
- Engine 初始状态为 `stopped`
- `runtime doctor` 返回 `ok=true` 且 `healthy=true`
- `document-basic`、`media-basic`、`transcribe-cpu` 均因未安装而返回明确 `warn` 和安装/修复动作
- `storage.sqlite.parallel-jobs` 为 `pass`
- `parallel_job_execution_enabled=false`

这证明 Doctor 允许可恢复的可选 Pack 缺失，但不会接受 `healthy=false` 或包含 `fail` 的候选。

## Document Pack 代际绑定行为

本次 Runtime 变更冻结以下最小合同：

- 新的 Engine Document Job 在写入 snapshot 前必须解析出一个可管理的当前 Pack 代际；缺失、开发环境 override 或不完整代际会在重型初始化前失败。
- Job 事件 `execution.pack-environment.v1` 只持久化一次，并在重试/恢复时复用。
- 恢复解析使用事件中的精确 manifest digest，忽略后来 `active.json` 的切换。
- 精确代际已丢失、receipt 不匹配或平台不匹配时 fail closed，不会静默使用新 active Pack。
- 现有 foreground 兼容路径保持不变；没有把本次 Engine 安全约束扩散成无关重构。

代码提交前的全量后端回归为 `2563 passed, 3 skipped, 3 warnings, 3 subtests passed`。独立只读 Gate 复跑 45 项相关测试并给出 PASS，无 P0/P1/P2 finding。

## 尚未解除的发布与产品边界

V13 仍是 unsigned portable directory candidate，不能宣称公开可发布。仍需完成：

1. Runtime 完整 SBOM/license、外部受控 manifest 锚点、签名、installer/discovery/PATH、update/rollback/uninstall；
2. clean Windows 非管理员用户/VM、中文与空格用户目录、Defender 开启、无源码 checkout 和无开发 Python 的复验；
3. 在同一个最终 Runtime 候选上安装正式签名的 `document-basic`、`media-basic`、`transcribe-cpu` Pack，并重新运行两份真实 PDF 与三个真实视频；
4. 两份真实 PDF 必须使用当前 fail-closed 双模型语义 verifier 重新产出，旧 bundle 不能替代当前版本证据；
5. capacity-1 Engine supervisor 完成资源预准入、Job authority 一次性交接、独立 heartbeat、取消和进程树清理之前，不启用多 Job 并发。

因此，V13 的准确结论是：Runtime 候选真实性和 Document Pack 代际绑定通过；整套 Video/Document 产品的公开发布与高并发目标仍在继续推进。
