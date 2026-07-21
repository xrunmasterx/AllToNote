# AllToNote 按需 Engine 与 Production MCP 实施计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-18-alltonote-engine-production-mcp-design.md
  - ../specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
implementation_status: demand-triggered-blocked-by-wave-0-4
last_verified_at: 2026-07-20
```

## 1. 启动条件与前置阻塞

Engine 产品需求已经由同 Workspace 高并行、批量后台执行和完整本地 Agent 调度触发，不再等待触发门。Production MCP Host 与提交客户端断连后 Job 仍需继续，也是同一生命周期要求的直接结果。

实施尚未解锁：本计划受 Wave 0–4 全部阻塞。只有 Wave 0–4 完成并通过各自 Gate 后，才能开始 Phase E1 及后续代码工作；阻塞期间保持前台 CLI + durable checkpoint，优先完成这些前置 Wave。

“至少两类真实 Recipe 已验证共同 Job/Checkpoint 语义”不是已满足的触发证据，而是 Wave 0–4 尚待完成的前置验收，不能据此提前开工。Personal Digest 定时需求只解锁后续 Scheduler Trigger，不解锁 Engine lifecycle。

## 2. Phase E0：已完成需求判定，等待 Wave 0–4

### ENG-00

将 Go/No-Go 记录为：产品需求为 Go，实施为 No-Go until Wave 0–4 complete。Job duration、调用者中断、并发数、GPU/Provider 冲突和 MCP Host timeout 继续作为基线测量，不再用于重复判定需求是否触发。

### ENG-01

Wave 0–4 通过后，检查现有 `jobs/*`、SQLite repository、machine resource authority、checkpoint/external operation；列出可复用和缺口。禁止另建第二 Job 状态机。

Gate：Wave 0–4 全部完成且用户/架构负责人确认开始；否则计划保持 blocked。

## 3. Phase E1：单实例与 IPC

### ENG-10：Engine contracts

新增 `backend/app/engine/contracts.py`，冻结 protocol version、endpoint descriptor、commands、errors、health。写 serialization/golden tests。

### ENG-11：单实例

实现 state-root scoped lock、PID + process-start identity、stale descriptor recovery。测试两个并发启动、PID reuse、损坏 descriptor、不同 test root。

### ENG-12：本地 IPC

Windows named pipe、macOS UDS 抽象；默认不 TCP。实现 health/submit/get/cancel/events/shutdown，ACL 限当前用户，bounded message/frame。

### ENG-13：CLI

`engine start|ensure|status|stop`；`produce --detach` 只有握手并持久化 Job 后返回。无 Engine 时 `--submit-only` 明确不保证继续。

Gate：Engine 冷启动、重复 ensure、空闲退出、协议不兼容全绿。

## 4. Phase E2：Execution Authority/Fencing/Worker

### ENG-20：Job schema migration

添加 `scheduler_authority`、`job_execution_authorities`、`step_attempts`、resource claims 和 events 所需最小字段；Job 持久化固定的 Recipe/Runtime identity；migration backup/idempotency；不把 DB 移到 Vault。

### ENG-21：SchedulerAuthority 与 JobExecutionAuthority

明确分离机器级 `SchedulerAuthority` 与 Job 级 `JobExecutionAuthority(job_id, owner_engine_id, worker_id, generation)`。SQLite 短事务实现 acquire/heartbeat/release/takeover：续租与 release 保留 generation，takeover 原子递增 generation。checkpoint、ExternalOperation、event、terminal transition 同时校验 job、owner/worker 与 generation；旧 generation 或旧 owner/worker 的 stale write 必须拒绝。

测试首次授权、正常续租、release 后写入拒绝、takeover generation 递增、旧 Worker 恢复回写、sleep/clock jump，以及两个 Worker 竞争同一 Job。

### ENG-22：Worker protocol

独立进程仅接收并恢复已持久化的 `job_id`、`recipe_identity`、`runtime_identity`、checkpoint 与当前 generation；Step Attempt 继承该 generation。Worker 恢复入口不得调用 submit、创建替代 Job 或重新解析 Recipe/Runtime identity。另实现 structured event/result、Secret 非 argv、stdout bound、kill tree、临时目录/grant。

### ENG-23：Recovery evaluator

expired authority -> persisted Job/Recipe/Runtime identity -> checkpoint/external operation -> replay/reconcile/needs-attention。replay-safe 恢复先 takeover 并递增 generation，再启动 Worker；所有路径复用原 Job，绝不重新提交。覆盖每个边界。

Gate：同 Job 永远只有一个当前 `JobExecutionAuthority`；release 保留 generation、takeover 递增 generation；旧执行者所有写入均被拒绝；Engine/Worker kill 后不重新提交 Job、不重复外部操作。

## 5. Phase E3：Video detach 真实纵切

### ENG-30

只接当前成熟 Video Recipe，不同时迁移 Web/Document。

### ENG-31

本地视频真实：submit -> CLI 退出 -> worker transcribe/compile -> Bundle/iwiki -> 新 CLI wait/result。

### ENG-32

Bilibili 字幕真实：断连/Engine restart/zero replay。

### ENG-33

取消、磁盘满、Pack缺失、模型 outcome unknown、iwiki reconcile。

Gate：真实而非 Fake 纵切通过；Fake 仅用于故障注入。

## 6. Phase E4：最小资源调度

### ENG-40：Resource model

CPU slot、memory、disk temp、GPU、Provider bucket、exclusive browser profile。RecipePlan 声明，不由 Adapter偷偷占用。

### ENG-41：Scheduler

interactive/background priority + FIFO + aging；小并发；无通用 DAG/优化器。

### ENG-42：Resource ledger

dispatch 前原子 claim、worker 结束释放、crash reconcile。GPU/磁盘/provider测试。

### ENG-43：Backpressure

队列上限、per-grant并发、预算、等待原因/预计动作。

Gate：资源不足时等待/拒绝可解释，不 OOM/磁盘打满/Provider storm。

## 7. Phase E5：事件与生命周期

### ENG-50

durable sequence events，CLI JSONL/Desktop SSE 使用同一投影；不以事件重放决定状态。

### ENG-51

cancel requested -> safe point -> kill tree -> reconcile；force kill warning。

### ENG-52

idle grace/keepalive/active schedule；无任务最终退出，重 Pack/模型释放。

### ENG-53

event retention/compaction，不删除关键 state/error/receipt。

## 8. Phase E6：Production Grant

### PMCP-00

领域模型：audience/workspace/recipes/input roots/domains/model profiles/budget/concurrency/output=raw/personal/expiry/revoke；approve/publish/common 默认 false。

### PMCP-01

CLI/Desktop grant create/list/revoke；Secret 只引用 profile；测试越域/过期/重放。

### PMCP-02

网络 URL redirect/SSRF、local root/path、预算每次外部操作前检查。

Gate：一个 grant 无法使用未列 Recipe/Provider/path/domain，也不能发布。

## 9. Phase E7：Production MCP

### PMCP-10

锁定官方 MCP SDK/protocol，建立独立 `backend/app/mcp/production/`；不要在 Knowledge Server 增加生产工具。

### PMCP-11

实现 capabilities/submit/get/wait/events/cancel/result-inspect；stdout framing、bounded wait、stable errors。

### PMCP-12

Host submit 后退出，Engine 继续；新 session 查询。MCP disconnect 不 cancel。

### PMCP-13

验证工具列表没有 shell/file/credential/approve/publish/common/pack update。

### PMCP-14

若官方 SDK/Host 已稳定支持 Tasks，单独 feature flag 做 job projection；不作为 Gate。

## 10. Phase E8：Scheduler Trigger（后续）

只有 Personal Digest 已验证：

- schedule domain/store；
- timezone/DST/missed-run/idempotent window；
- Engine OS wake/start integration（平台允许范围）；
- background budget；
- 只生成 Candidate，不自动发布；
- pause/delete/audit。

## 11. 故障矩阵

每个阶段强制 kill：Engine、Worker、CLI、MCP Host；模拟休眠、release/takeover 竞态、磁盘满、Pack 更新、DB 锁、Provider timeout、outcome unknown、cancel。验证 Step Attempt 继承 generation、stale write 被拒绝、Worker 只恢复持久化 Job/Recipe/Runtime identity 且不重新提交；对所有外部操作证明“成功结果先持久化、未知先 reconcile、终态不复活”。

## 12. 性能 Gate

- cold start < 2s；
- submit/get < 200ms 基线；
- idle < 100MB 且最终退出；
- 事件等待不高频轮询；
- 重 worker/GPU 空闲释放；
- 1000 queued lightweight Job 的分页/调度压力；
- 性能优化不得取消 SchedulerAuthority、JobExecutionAuthority/generation fencing 或 checkpoint。

## 13. 最终 E2E

```text
Production MCP Host
 -> grant/capabilities
 -> submit local video
 -> Host 退出
 -> Engine/worker 执行
 -> 中途 Engine kill/restart
 -> no duplicate model/transcribe
 -> 新 Host wait/result
 -> ReviewCandidate only
 -> 无 publish tool
 -> idle Engine exit
```

另跑两个并发 Job 的 GPU/Provider调度、cancel 和 Pack pinned update。

## 14. 验收与状态

输出 `docs/acceptance/engine-production-mcp-v1.md`，更新 `ENGINE-01`。只有 Phase E1–E7、真实 Video 纵切以及执行权威故障矩阵通过，才标 Engine/Production MCP 完成；Scheduler Trigger 独立标记。当前状态必须保持“需求已触发、实施受 Wave 0–4 阻塞”。
