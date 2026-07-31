# Runtime Windows V9 SQLite 并发前置条件验收

日期：2026-08-01

状态：当前 Windows 主机候选通过；不是公开发行认证，也没有启用并行 Job

## 1. 本轮目标

V8 已证明 CPython 3.14.6 / SQLite 3.53.4 的多连接 WAL 行为，但不包含随后完成的三个并发前置修复：

1. JobStore 与机器资源租约 Store 必须实际进入 WAL，并使用 `synchronous=FULL`；否则 fail closed。
2. 机器资源租约 Store 的物理 `SQLITE_BUSY` / `SQLITE_LOCKED` 必须成为脱敏、可重试的 `machine_lease_store_busy`，不能误报 schema/store 损坏。
3. Runtime 必须区分 SQLite 构建“支持并行”与产品“已经启用并行”；本轮仍保持后者为 `false`。

V9 的目的，是从同一个可移植 Runtime 候选重新证明这些修复、完整 WAL Gate 和 legacy JobStore 迁移，而不是删除现有串行保护。

## 2. 固定身份

- AllToNote 源提交：`86290c1efdcbd6d52deca909175284e0335a1edd`
- 对应 `git archive --format=zip` SHA-256：`0ae51e383a28d99478530b1372333e6ead9c3d08f96b6a06c2c0a8c1d6c496c0`
- 候选目录：`G:\.alltonote-release\runtime-portable-sqlite-v9`
- Runtime wheel：`alltonote_runtime-0.1.0-py3-none-any.whl`
- Runtime wheel byte length：`534459`
- Runtime wheel SHA-256：`b1fd4472660c3c6d32c142ec5ee9c672b2c705ad48f964885c3feedceae28130`
- CPython：3.14.6，Windows x86_64
- SQLite：3.53.4
- SQLite source id：`2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc`
- SQLite compile mode：`THREADSAFE=1`、`MUTEX_W32`，不含 `OMIT_WAL`
- 候选文件清单：696 个 payload 文件
- `release/file-manifest.json` SHA-256：`bafacdd46629a513e066fe508f7c461c68b729b1d9904ce51e439ca0d667513a`

发布后独立逐文件复核结果：696/696；缺失 0、额外 0、长度错误 0、SHA-256 错误 0。清单文件自身不列入 payload 清单。

## 3. 候选自己的 Runtime 报告

V9 的 `runtime info --json` 报告：

- `sqlite_version=3.53.4`
- `sqlite_threadsafety=3`
- `parallel_job_execution_supported=true`
- `parallel_job_execution_enabled=false`
- `engine.supported=false`
- `engine.running=false`

这表示二进制和 Store 基础满足进入后续并行工程的条件，但当前 Video/Document 的实例执行锁、scheduler lease 和机器级 `produce:heavy:v1` 独占租约仍然有效。

## 4. 发布后 WAL Gate

组装期间的 Gate 与发布后的第二次独立 Gate 均通过。第二次 Gate 使用新建的隔离 machine-state，覆盖：

- 1/4/8/16 connection 短写；
- 1/4/8/16 connection 混合读写；
- 在线 `PASSIVE` checkpoint 与真实重叠握手；
- 强制 writer busy，稳定错误后由调用方显式重试；
- Portable commit callback 位于 writer transaction 内的持锁测量；
- 未提交进程崩溃与已确认提交后的进程崩溃；
- 重开后的继续写入；
- 最终 `TRUNCATE` checkpoint、`integrity_check`、foreign keys、WAL 与 schema version 检查。

结果：

- 正常短写：232/232 成功，busy 0；16 connection p95 `3.755 ms`。
- 混合读写：116 次写和 116 次读全部成功，busy 0；16 connection 写 p95 `3.804 ms`、读 p95 `1.341 ms`。
- checkpoint：32/32 成功，所有连接数均完成 overlap handshake；checkpoint busy 0。
- forced busy：4/8/16 connection 分别观察 3/7/15 次稳定 busy，随后 3/7/15 次显式重试全部成功。
- Portable commit：请求 callback `50 ms`，实测 writer lock `53.753 ms`，完整 transaction `55.065 ms`。
- crash recovery：未提交行不存在；已确认行、execution binding 和事件均存在；崩溃后继续写成功。
- 最终 `integrity_check=ok`、foreign-key violation 0、WAL frame remaining 0、`journal_mode=wal`、`user_version=2`。

这些时间是当前主机 characterization，不是跨机器性能 SLA。forced-busy 约 5.5 秒对应当前 5 秒 busy timeout 加进程/调度开销。

## 5. Legacy JobStore 发行 Gate

V9 使用仓库内按 SHA-256 固定的 v1 SQL fixture，并由候选自己的 CPython/SQLite 执行迁移。结果：

- schema 1 → 2；
- Job 继续是 `succeeded`；
- Video result、Bundle ID 与 `quality=pass` 保持；
- execution binding 保持；
- Attempt、Checkpoint、Event、SourceIdentity 均保持；
- foreign-key error 0，`integrity_check=ok`；
- 再次重开后数据和 binding 保持一致。

## 6. 自动化与审查证据

- SQLite/WAL/Runtime/发行工具聚焦测试：54 passed。
- 并发前置条件提交前完整后端回归：2318 passed，2 skipped，3 subtests passed。
- 独立 SQLite 并发前置条件 Gate Review：P0=0、P1=0、PASS；独立复跑 348 passed。
- V9 assembler 自检：version、runtime info、runtime doctor、中文空格路径 Workspace init、WAL Gate、legacy JobStore migration 全部通过。
- V9 发布后 WAL Gate：再次通过。
- V9 发布后文件清单：696/696 通过。

## 7. 尚未解除的发布边界

V9 仍是 unsigned portable directory candidate。以下工作仍未完成：

1. 全新 Windows 非管理员用户/VM、中文空格用户目录、Defender 开启、无源码 checkout 与无开发 Python 的复验。
2. 稳定 launcher、per-user installer/discovery/PATH、Authenticode、timestamp、Runtime SBOM 与完整 license aggregation。
3. 固定并证明用于离线 wheel 安装的 builder Python/pip 工具链；本轮 builder 能正确执行 CPython 3.14 wheel 安装，但其身份尚未进入发行锁。
4. update、rollback、repair、uninstall，并证明 Vault 不变。
5. 从同一正式 artifact 与签名 Video/Document Packs 完成真实输入 E2E。
6. Job-scoped generation authority、按需 Engine、隔离 worker pool 和容量 admission。

因此，V9 关闭的是“SQLite 二进制与 Store 并发前置条件是否可信”的当前主机证据，不是“Video/Document 已经高并发”这一最终目标。
