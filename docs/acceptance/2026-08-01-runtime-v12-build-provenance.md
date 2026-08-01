# Runtime Windows V12 构建溯源与隔离 Gate

日期：2026-08-01

状态：本机 Windows x86_64 portable directory candidate 通过；仍不是签名安装包或公开发布认证。

## 目标与结论

本轮把 Runtime 候选从“文件清单一致”推进到“构建器、输入 wheel、安装结果和运行时 Gate 可追溯”。V12 由锁定的 Builder CPython、15 个锁定 wheel 和固定的 CPython/SQLite 输入组装，安装结果按每个 wheel 的 `RECORD` 逐项核对，随后由候选自身执行 CLI、Engine、Workspace、SQLite 和旧 JobStore 迁移 Gate。

结果为 pass。`parallel_job_execution_enabled` 继续固定为 `false`；本轮没有以发布验证为理由提前启用多 Job 并发。

## 固定身份

- Runtime 源码提交：`daf82e790c7214385f4f5bb0ac5db604d01b41b2`
- Runtime wheel：`alltonote_runtime-0.1.0-py3-none-any.whl`
- Runtime wheel 字节数：`577338`
- Runtime wheel SHA-256：`9b73e78319d5b1f7c152f6b243e2c3246f576ac9b116103c1f6f9236febebaa0`
- 候选目录：`G:\.alltonote-release\runtime-portable-build-provenance-v12`
- 平台：Windows x86_64
- Runtime：`0.1.0`
- CPython：`3.14.6`
- SQLite：`3.53.4`
- SQLite source id：`2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc`
- payload 文件数：`710`
- payload 字节数：`40,942,085`
- `release/file-manifest.json` SHA-256：`e79fcdf75c6c9d1bd1b3eb2d7065d33dd5691c4762c4428be024be6f19e50859`

上面的 manifest SHA-256 已用于本机只读 verifier 功能验证。正式交付时仍必须把该值保存在候选目录之外的受控发布记录中；从候选目录现场计算后再传回 verifier 只能证明内部一致性，不能单独证明来源身份。

## Builder 与 wheel 安装溯源

Builder 在执行前先核对完整根目录文件树，再执行 `-I -B` 身份探针：

- Builder：CPython `3.14.0`，`win32` / `AMD64` / 64-bit
- Builder `python.exe` SHA-256：`467014615a5255aca450ae88100dd2caf887da87657f00e3c2171ec44a685aec`
- Builder `python314.dll` SHA-256：`f1722bd369d79fecbc85f3ed2790c30c330b9413fd74332f95b086e60dfacc2a`
- pip：`25.2`
- pip tree SHA-256：`efe1bd4b245d602d84b97bf591d5d3aee4c91c0349996a9f0c5c73633f3e585b`
- Builder root tree SHA-256：`931b2c04dad774969bb321c788ece6c9991750f7485baccac856a0c2c6cf7200`
- 候选内 `builder-toolchain.json` SHA-256：`e1e41b540264330c7915f87c3e6979e359ea40e95ff897d798f8ba220638efb0`

wheel 安装核对结果：

- 锁定 wheel：`15`
- wheel 源文件：`621`
- pip 允许生成的控制文件：`30`（每个 wheel 的 `INSTALLER` 和 `REQUESTED`）
- 最终安装文件：`666`
- 最终 `site-packages` tree SHA-256：`90164ab76f4c5756b4af73d95ccdddf5b1304bf6fe25be2378d9e9fe7ac94cd5`
- 候选内 `wheel-install-attestation.json` SHA-256：`57805f851db61e3fc3c5f26dd5f704d9eee3eb113bcae4fb9ec5b3201e7aca42`

核对会拒绝 wheel 外层 hash/大小漂移、无解释的安装文件、`RECORD` 路径/hash/大小不一致、源 wheel 预先占用 pip 生成文件，以及最终文件树中的额外、缺失或改变内容。

## 真实运行 Gate

候选自身通过以下检查：

- `version --json`
- `runtime info --json`
- `runtime doctor --json`
- 中文与空格路径的 `workspace init`
- SQLite WAL 多连接 Gate：`1 / 4 / 8 / 16`
- schema v1 到当前 schema v5 的旧 JobStore 迁移与重开复核
- 20 次 Engine 冷启动，p95 `619.007 ms`
- Engine 最大 idle RSS `42,344,448 bytes`
- 32 路并发 `engine ensure` 收敛为一个实例
- 父 CLI 退出后重连、强制终止后重新拉起、短 idle 自动退出和最终 stop

独立 CLI 会话再次确认 Runtime 为 `0.1.0`、Engine 最终为 `stopped`、Runtime doctor 为 healthy；三个可选生产 Pack 在隔离根中均为未安装 warning，符合无 Pack 的干净 Runtime 预期。

## Gate 实际发现并修复的问题

第一次真实候选运行发现发布工具的短 idle 探针仍使用旧版 `run_engine_host(engine_root=..., log_root=..., scope_id=...)` 调用，而当前 Runtime 合同已经是 `run_engine_host(paths=...)`。该调用已更新，并加入直接执行探针脚本的回归测试。

第二次运行发现 Windows `platformdirs` 通过系统 API 解析用户目录，不接受发布工具原先设置的 `LOCALAPPDATA` / `APPDATA` 替换。Gate 因而误用了真实用户 machine state；已有 Job 会阻止短 idle Engine 退出。修复后：

- `ALLTONOTE_MACHINE_STATE_ROOT` 提供明确的进程级隔离根；
- 值必须为非空绝对路径，相对路径和空白值 fail closed；
- 显式 `machine_state_root` / `local_data_parent` 参数保持权威，不被环境覆盖；
- 解析本身不创建目录；
- CLI 启动的 Engine 子进程继承同一隔离根；
- 正常未设置该变量的用户继续使用原有 `platformdirs` 路径。

发布子进程错误同时增加了不含绝对路径的阶段标签，能够区分 Builder、wheel 安装、Runtime CLI、Engine inspect/terminate/idle 和旧 JobStore 迁移失败。

## 自动化证据

- Runtime path 与 Runtime Windows release 聚焦测试：`61 passed`
- 更新锁后 Runtime release / wheelhouse / paths 测试：`73 passed`
- V12 组装：pass，96 秒
- 候选只读 verifier：pass，manifest/source/platform/status/文件树一致
- 候选独立 `version` / `runtime info` / `runtime doctor`：pass
- 全量后端回归：`2555 passed, 3 skipped, 3 subtests passed`

## 尚未解除的发布边界

V12 仍是 unsigned portable directory candidate。以下事项尚未完成：

1. clean Windows 非管理员用户/VM、中文和空格用户目录、Defender 开启、无源码 checkout 与无开发 Python 的复验；
2. Runtime 完整 SBOM/license、受控外部 manifest 锚点、签名、installer/discovery/PATH、update/rollback/uninstall；
3. 在同一最终候选物上安装正式签名的 document-basic、media-basic、transcribe-cpu Pack，并完成真实 Document PDF 与三个 Video 输入 E2E；
4. Task 2 的 capacity-1 supervisor、资源 admission 与 lease transfer/adoption 验证完成前，不启用多 Job 并发。
