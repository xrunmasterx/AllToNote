# AllToNote 按需 Engine 与 Production MCP 设计

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-18-alltonote-runtime-cli-feature-pack-design.md
  - 2026-07-18-alltonote-review-publisher-design.md
  - 2026-07-18-alltonote-knowledge-access-mcp-design.md
downstream:
  - ../plans/2026-07-18-alltonote-engine-production-mcp-implementation-plan.md
implementation_status: demand-triggered-blocked-by-wave-0-4
last_verified_at: 2026-07-20
```

## 1. 决策摘要

Engine 是可选的、按需启动的单机任务执行器。它只在“调用者退出后任务仍需继续”或“多个任务需要资源调度”时启用，不是 AllToNote CLI、Vault、Knowledge MCP 或 Desktop 的基础依赖。

```text
当前阶段
CLI foreground -> Job/Checkpoint -> 当前进程执行 -> 完成

Engine 阶段
CLI/Desktop/Production MCP -> durable Job
  -> 本机 Engine 获取 JobExecutionAuthority
  -> 按资源启动隔离 worker/Pack
  -> checkpoint/event/reconcile
  -> Engine 空闲后退出
```

Production MCP 是 Engine 的受控提交客户端，不是第二套 Producer：

- 只调用现有 Application Service/Job；
- 默认提供 capabilities、submit、get/wait/events/cancel 和结果检查；
- 不默认提供 review approve、publish 或 common；
- 本地第一版只支持 stdio；
- MCP experimental Tasks 最多作为兼容投影，内部真相始终是稳定 AllToNote Job。

## 2. 产品触发已满足，实施仍受前置 Gate 约束

同 Workspace 高并行、批量后台执行和完整本地 Agent 调度已经构成真实产品需求，Engine 需求已触发，不再等待产品触发门。但需求触发不等于实施解锁：Engine 实施明确受 Wave 0–4 阻塞，不属于 X0-A，也不得提前成为 foreground CLI 或 Recipe 的依赖。只有 Wave 0–4 全部完成并通过各自 Gate 后，才进入 Engine lifecycle、JobExecutionAuthority 和隔离 Worker。

已确认的触发证据是：

1. 用户明确要求同 Workspace 高并行与批量后台执行；
2. 用户明确要求完整的本地 Agent 调度；
3. 这些需求要求调用者退出后 Job 仍可继续，并要求多个任务共享本机 GPU、Whisper、浏览器和 Provider 配额；
4. Production MCP Host 的生命周期不能成为 Job 生命周期。

“已有至少两类真实 Recipe 验证共同 Job/Checkpoint 语义”不是当前触发证据：真实 Recipe 纵切属于 Wave 0–4 的前置验收，尚不能用未完成的实现反向证明 Engine 已可开工。Personal Digest 定时触发也不是本轮 Engine lifecycle 的启动依据，它保留为后续 Scheduler trigger。

不构成触发条件：

- “未来也许需要”；
- 只为了显示后台托盘图标；
- 单个 CLI 任务偶尔运行十分钟；
- 为了模仿云端工作流产品；
- MCP 规范出现了 experimental Tasks。

## 3. 目标

- 单机 durable execution；
- 调用者退出后任务继续；
- 不重复下载、转写、付费模型调用或发布；
- CPU/GPU/内存/磁盘/网络/Provider 配额可控；
- 可取消、等待、恢复、reconcile；
- Runtime/Recipe/Pack/模型策略固定到 Job；
- worker 崩溃不拖垮 Engine 或其他 Job；
- 空闲时无常驻资源；
- CLI、Desktop、MCP 看到同一 Job/事件真相；
- 维持本地优先，不引入云队列或分布式共识。

## 4. 非目标

- 多机 worker 集群；
- 高可用 Engine；
- Kubernetes/Temporal/Celery 服务部署；
- 任意用户脚本调度平台；
- 通用 DAG 编辑器；
- 无限并发；
- 跨 OS 共享同一个 JobStore；
- 把 Engine 数据库放进 Vault/云盘；
- Engine 直接拥有 Publisher 规则；
- 远端互联网直接访问本地 Engine；
- 以 MCP Task 状态替代内部 Job 状态机。

## 5. 单机组件

```text
Engine Supervisor
  ├─ Local IPC Server
  ├─ SchedulerAuthority
  ├─ JobExecutionAuthority Manager
  ├─ Worker Manager
  ├─ Recovery/Reconciler
  ├─ Resource Ledger
  └─ Idle Shutdown

Worker Process
  ├─ pinned Runtime/Core/Recipe/Pack identity
  ├─ one Job or bounded compatible workload
  ├─ checkpoint/external operation reporting
  └─ no direct publish authority

JobStore (machine-local SQLite WAL)
  ├─ jobs
  ├─ step_attempts
  ├─ job_execution_authorities
  ├─ checkpoints
  ├─ external_operations
  ├─ events
  └─ resource_claims
```

Engine 与 worker 仍复用共享 Core/SDK。Engine 只负责执行生命周期和资源所有权，不复制 Recipe 业务逻辑。

## 6. 启动、发现与退出

### 6.1 启动方式

```text
alltonote engine start             # 显式启动
alltonote produce ... --detach     # 不存在时按需启动
alltonote engine ensure            # Desktop/Production MCP 使用
```

启动过程：

1. 解析当前用户 machine-state root；
2. 获取单实例启动锁；
3. 检查已有 Engine liveness/identity；
4. 校验 Runtime/JobStore schema；
5. 绑定本地命名管道（Windows）或 Unix domain socket（macOS/Linux）；
6. 写入受保护 endpoint descriptor；
7. 恢复过期 JobExecutionAuthority/outcome unknown；
8. 开始接受提交。

默认不监听 TCP。若未来需要 loopback，也必须独立设计 token/Origin，不直接复用 Desktop API。

### 6.2 单实例

范围为：`OS user + state root + runtime major`。不同用户、测试 state root 或不兼容 major 不共享 Engine。

endpoint descriptor 包含：engine_id、PID、process start identity、protocol version、socket/pipe name、nonce、started_at。客户端必须验证 liveness 和 protocol，不能仅信旧 PID 文件。

### 6.3 空闲退出

当同时满足：

- 无 running/starting/reconciling Job；
- 无有效 Worker/JobExecutionAuthority；
- 无等待中的调度触发；
- 无活跃客户端持有显式 keepalive；
- 超过 idle grace；

Engine 有序退出。默认 grace 在实现计划中固定，例如 10–15 分钟；用户可缩短或禁用自动启动，但不能把 Engine 设为所有 CLI 的强制前置。

## 7. 调度权威、Job 执行权威与 Step Attempt

### 7.1 SchedulerAuthority

`SchedulerAuthority` 是 Engine 单实例对“哪些 Job 可以被调度、资源如何分配”的机器级权威。它只负责排队、资源 claim 与 dispatch，不直接授权 Job 状态写入，也不能替 Worker 重新提交 Job。

### 7.2 Job 稳定身份

Job 描述用户意图、输入身份、Recipe、配置快照和期望产物。Job 创建后，其 `job_id`、`recipe_identity` 与 `runtime_identity` 持久化并固定；终态不可变。

### 7.3 JobExecutionAuthority

每个 Job 的执行写权限由 JobStore 中唯一的 `JobExecutionAuthority(job_id, owner_engine_id, worker_id, generation)` 表示：

```json
{
  "job_id": "job_...",
  "owner_engine_id": "engine_...",
  "worker_id": "worker_...",
  "generation": 17,
  "acquired_at": "...",
  "heartbeat_at": "...",
  "expires_at": "..."
}
```

`generation` 是 Job 级 fencing generation，而不是 Attempt 私有 token。所有 Step Attempt、checkpoint、ExternalOperation、事件和终态迁移都携带并继承当前 generation；JobStore 只接受与当前 `JobExecutionAuthority` 完全匹配的写入。

### 7.4 Step Attempt

Step Attempt 记录同一 Job 内某一步的执行或恢复，不创建新的 Job/Recipe/Runtime 身份：

```json
{
  "step_attempt_id": "step_attempt_...",
  "job_id": "job_...",
  "step_id": "transcribe",
  "number": 2,
  "owner_engine_id": "engine_...",
  "worker_id": "worker_...",
  "generation": 17,
  "started_at": "...",
  "ended_at": null,
  "resume_checkpoint_id": "cp_...",
  "outcome": null
}
```

Step Attempt 失败不等于创建新 Job：

- 同一 Job 内可按确定策略恢复 replay-safe step；
- Job 已终态后，用户 retry 才创建新 Job；
- 付费/外部操作 outcome unknown 先 reconcile，不能自动再调用。

## 8. Authority 生命周期与 fencing

### 8.1 获取、续租与释放

- 使用 SQLite 短事务比较并更新；
- 同一 Job 同时只有一个有效 `JobExecutionAuthority`；
- 首次授权或确认旧 owner 不再拥有执行权后的 takeover，原子递增 generation；
- 正常续租和同一 owner/worker 的显式 release 保留 generation；release 只清除当前占用，不制造新的写入时代；
- release 后旧 owner/worker 不得继续写；下一执行者必须执行 takeover，并获得递增后的 generation；
- worker 定期 heartbeat；
- Engine/worker 不以系统墙钟单独决定业务成功；
- 机器休眠后允许 authority 过期，但恢复必须检查 process identity 和 operation 状态；
- authority timeout 不是自动重放授权。

所有写路径同时校验 `job_id + owner_engine_id + worker_id + generation`。任何 owner、worker 或 generation 不匹配的 checkpoint、ExternalOperation、事件和终态写入均作为 stale write 拒绝，防止暂停或失联的旧执行者覆盖接管后的状态。

### 8.2 Worker 恢复契约

Worker 由 Engine 以已持久化的 `job_id`、`recipe_identity`、`runtime_identity`、checkpoint 和当前 `JobExecutionAuthority` 启动或恢复。Worker 只继续该 Job 的既有执行，绝不调用 submit、创建替代 Job，或重新解析出新的 Recipe/Runtime 身份；恢复入口与新提交入口必须分离。

### 8.3 回收与接管

过期 authority 的 Job 进入 `recovery-evaluation`：

1. 查最后 checkpoint；
2. 查未决 ExternalOperation；
3. 校验 Job 固定的 Recipe/Runtime/Pack identity 是否仍可用；
4. replay-safe：原子 takeover、递增 generation，再启动恢复 Worker；
5. outcome unknown：进入 reconcile；
6. 不能确定：`needs-attention`，等待用户/Adapter 决策。

reconcile 或 takeover 均复用原 Job 及其持久化身份，不经过提交路径。

## 9. Scheduler

### 9.1 先做最小调度器

MVP 使用明确的资源声明和 FIFO + 优先级，不建设通用优化器：

```json
{
  "cpu_slots": 2,
  "memory_mb": 4096,
  "gpu": {"required": false, "device_class": null, "memory_mb": 0},
  "disk_temp_mb": 8000,
  "network": ["youtube.com", "model-provider"],
  "provider_buckets": ["openai:default"],
  "exclusive": ["browser-profile:youtube"]
}
```

### 9.2 队列规则

- 用户交互任务优先于后台 digest；
- 同优先级 FIFO；
- 明确防止后台任务永久饥饿（aging）；
- 单用户默认小并发；
- GPU worker 按显存能力串行或小并发；
- Provider bucket 遵守 rate limit/backoff；
- 磁盘预算不足在下载前失败；
- 不允许 Adapter 私自启动无界子进程。

### 9.3 资源探测

资源能力在 Engine 启动和 Pack 激活时探测，但 Job dispatch 前重新检查关键资源。探测错误不应污染 Job；调度等待超过阈值产生可解释事件。

## 10. Worker 隔离

- worker 为 Engine 子进程，使用最小环境；
- Job/Pack 临时目录独立；
- Secret 通过受保护 IPC/stdin 或短期凭据传递，不进 argv/磁盘明文；
- worker 只能访问 Job grant 的输入、临时区和允许的 raw 输出 staging；
- worker 不拥有 `wiki` publish grant；
- stdout/stderr 有上限、脱敏和结构化协议；
- Engine 负责 kill process tree；
- 资源超限、hang、协议损坏均产生稳定 Step Attempt outcome；
- 大模型/本地模型 worker 可在相同兼容配置下短期复用，但复用不是 MVP 必需，且不能跨 Secret/用户边界。

## 11. Event 模型

事件为追加、序号化、大小受限的业务投影：

```json
{
  "event_id": 1024,
  "job_id": "job_...",
  "sequence": 37,
  "type": "stage.progress",
  "stage": "transcribe",
  "timestamp": "...",
  "step_attempt_id": "...",
  "generation": 17,
  "data": {"completed": 17, "total": 50, "unit": "chunks"}
}
```

约束：

- 状态由 Job 表/领域状态机决定，不能只靠重放 UI event 推断；
- 事件不保存完整正文/Prompt/Provider raw；
- 同 Job sequence 单调；
- 客户端可用 `after_sequence` 恢复；
- 旧细粒度 progress 可按保留策略压缩，关键状态/错误/receipt 事件保留；
- SSE/MCP progress/CLI JSONL 都是该事件流的传输投影。

## 12. 取消与终止

### 12.1 取消语义

```text
cancel requested
 -> Job 标记 cancel-requested
 -> Engine 通知 worker
 -> worker 在安全点 checkpoint/停止
 -> 外部子进程有序终止，超时 kill tree
 -> 若外部操作 outcome unknown，进入 reconcile
 -> terminal canceled 或 needs-attention
```

取消不是删除。已生成且验证的 immutable Source/Artifact 可按清理策略保留；未原子提交的 staging 不进入正式 Bundle/Wiki。

### 12.2 强制 kill

用户可强制 kill worker，但 UI/CLI 必须说明可能产生 outcome unknown。Engine 仍不能把 kill 直接记作安全失败后自动重试。

## 13. 崩溃与重启

恢复矩阵：

| 崩溃点 | 恢复动作 |
|---|---|
| acquisition 前 | 重新开始安全 preflight |
| 下载中且支持 range | 校验 partial metadata 后续传；否则重建临时文件 |
| transcript 已持久化 | 跳过 acquisition/transcribe |
| Model call 未发出 | 安全执行 |
| Model call 已发出、无结果 | reconcile/idempotency policy；未知则人工处理 |
| Model result 已持久化、checkpoint 未推进 | 复用结果后推进 |
| Bundle staging 中 | validate staging，不能见到半 commit |
| iwiki commit outcome unknown | inspect/receipt/hash reconcile，不盲重放 |

Engine 更新/停止前先停止 dispatch 和获取新的 JobExecutionAuthority，等待安全点；超时留下可恢复状态。

## 14. Production Grant

Production MCP/外部 Agent 需要独立授权：

```json
{
  "grant_id": "pg_...",
  "audience": {"kind": "local-mcp-client", "name": "codex"},
  "workspace_id": "...",
  "recipes": ["video", "web"],
  "input_policies": {
    "allow_urls": true,
    "allow_local_files_under": ["<opaque-root-id>"],
    "allowed_domains": ["youtube.com", "bilibili.com"]
  },
  "outputs": ["raw/personal"],
  "model_profiles": ["balanced-default"],
  "budget": {"per_job": "...", "per_day": "..."},
  "max_concurrent_jobs": 1,
  "can_request_review": true,
  "can_approve": false,
  "can_publish_personal": false,
  "can_publish_common": false,
  "expires_at": "..."
}
```

Grant 不包含 Secret 值；只引用用户已配置的模型/平台凭据。用户可随时 revoke，新提交立即拒绝；已运行 Job 按安全政策取消或完成当前不可中断操作后停止。

## 15. Production MCP 工具

启动：

```text
alltonote mcp production --grant <production-grant-id>
```

Server 启动时必须确认 Engine capability 可用；不能偷偷退化为依赖 MCP Server 进程存活的“伪 detach”。

### 15.1 MVP 工具

#### `production_capabilities`

返回允许 Recipe、input、model profile、预算、Pack 健康和 Engine 状态；不返回 Secret/路径。

#### `production_submit`

输入统一 envelope：recipe、input descriptor、mode/profile、requested drafts、idempotency key、可选 workspace target。Server 做 grant/preflight 后创建 Job，返回 job_id 和初始状态。

不接受任意 workflow、shell command、Prompt override 或 publish target。

#### `production_job_get`

返回稳定 Job 投影、stage、progress、warnings、result refs 和可执行 next actions。

#### `production_job_wait`

有明确最大等待时间，超时返回非终态而不是工具失败；调用者可继续 wait。MCP Host 断开不取消 Job。

#### `production_job_events`

按 sequence 分页返回结构化、脱敏事件。

#### `production_job_cancel`

请求取消 grant 创建/允许控制的 Job。

#### `production_result_inspect`

返回 Bundle/Draft/Quality 摘要与 ReviewCandidate ID；不返回整个私人 raw 正文，除非另有 Knowledge/Review grant。

### 15.2 明确不暴露

MVP 无：

- arbitrary shell/file write；
- credential get；
- model raw prompt；
- review approve；
- publish apply；
- common；
- pack install/update；
- runtime update；
- Engine admin/kill other clients' jobs。

### 15.3 MCP Tasks 兼容

若 Host 与 SDK 稳定支持 experimental Tasks，可将一次工具调用映射为 MCP task：

- `taskId` 与 AllToNote job_id 建立映射；
- 授权仍绑定 ProductionGrant；
- MCP task expiry 不删除内部 Job；
- 内部 Job 状态机、Receipt、retry 语义不改变；
- 不支持 Tasks 的 Host 仍使用 submit/get/wait；
- 此兼容层不得成为 Engine MVP Gate。

## 16. 安全

- 本地 Production MCP 只使用 stdio，不开放 LAN/Internet；
- production 与 knowledge 使用不同命令、grant 和工具列表；
- URL/文件/网页内容均不可信，不能扩大工具权限；
- Agent 传入的“请发布/忽略预算”等文本只作为内容；
- 网络 destination allowlist 和 redirect 后最终目标都检查，防 SSRF；
- 本地文件只通过 opaque root grant，防路径穿越/reparse point；
- Secret 不返回 MCP Host；
- 预算在 Job 创建和每次外部付费操作前检查；
- Pack/Runtime 固定并验证签名；
- worker 无 Publisher 权限；
- audit 记录 caller/grant/job/tool/error/成本摘要，不保存正文。

## 17. 性能与容量

MVP 面向单用户工作站：

- 默认最多 1 个重转写/本地模型任务；
- 默认最多 2–4 个轻网络/LLM stage，受 Provider limit 限制；
- Engine 冷启动 p95 < 2 s（不加载重 Pack）；
- submit/get p95 < 200 ms；
- event wait 不轮询高频 SQLite；
- idle Engine 内存目标 < 100 MB，优先更低；
- 无 Job 时最终退出，释放全部重 worker/GPU；
- JobStore 事件/日志有保留和压缩策略；
- 磁盘不足在获取大媒体/模型前预检。

## 18. 测试矩阵

### 18.1 状态与执行权威

- 两 Engine 启动竞争；
- 两 Worker 争同一 Job；
- release 保留 generation；
- takeover 递增 generation；
- fencing 拒绝旧 owner/worker/generation；
- Step Attempt 继承当前 generation；
- Worker 恢复持久化 Job/Recipe/Runtime identity 且不重新提交；
- heartbeat 丢失/机器休眠；
- Engine/worker/CLI 分别被杀；
- 终态 Job 不复活；
- retry 新 Job；
- event sequence/恢复。

### 18.2 外部操作

- 下载/转写/模型/iwiki commit 前后每个 crash point；
- success result 持久化前后；
- outcome unknown reconcile；
- idempotency key；
- Provider rate limit/backoff；
- cancel/force kill；
- Pack update 时 active Job 固定旧版本。

### 18.3 资源

- CPU/GPU/内存/磁盘不足；
- GPU 单占与等待；
- Provider bucket；
- 后台 digest aging；
- worker 子进程树清理；
- 恶意/损坏 Pack worker 输出；
- idle shutdown。

### 18.4 Production MCP E2E

1. 创建最小 Video grant；
2. Host submit 后立即退出；
3. Engine 继续并完成 Job；
4. 新 Host get/wait/result；
5. 超预算/越域 URL/本地路径拒绝；
6. 无 approve/publish/common 工具；
7. cancel 长任务；
8. Engine 崩溃重启后不重复模型调用；
9. Desktop 与 CLI 同时观察一致事件；
10. Engine idle 后退出。

## 19. 分期

### Phase E0：需求已触发，等待 Wave 0–4

保留中断、并发、调度和 Production MCP 基线测量；产品需求判定已为 Go，但实施在 Wave 0–4 完成前保持 blocked。

### Phase E1：Engine 单实例与执行权威

实现 IPC、单实例、SchedulerAuthority、JobExecutionAuthority/generation fencing、Worker 恢复和 CLI `engine` 命令；先用 Fake Adapter 做 crash matrix，但不得用 Fake 代替真实发布验收。

### Phase E2：Video detach

只接一个已成熟 Recipe，完成真实本地视频/Bilibili 的 submit-exit-recover E2E。

### Phase E3：资源调度

加入简单 CPU/GPU/Provider/Disk claims；不做通用 DAG。

### Phase E4：Production MCP

实现 grant、stdio tools、真实 Host submit/disconnect/reconnect；默认无发布。

### Phase E5：Scheduler trigger

只有 Personal Digest 需要后增加本地定时触发，并保持 Engine 按需唤醒/空闲退出。

## 20. 完成定义

1. 未使用 detach/生产 MCP 时，AllToNote 仍无常驻 Engine；
2. 调用者退出后，Engine 接管的 Job 可继续；
3. SchedulerAuthority 与 `JobExecutionAuthority(job, owner/worker, generation)` 保证单 Job 单执行者，release 保留 generation、takeover 递增 generation，stale write 被拒绝；
4. 每个 crash point 不产生重复付费或半提交；
5. Runtime/Recipe/Pack/配置固定到 Job；
6. worker 崩溃不拖垮 Engine/其他 Job；
7. Production MCP 受独立 grant、预算和输入范围约束；
8. Production MCP 默认没有审批/发布/common；
9. MCP Tasks 不是内部真相或发布前置；
10. Engine 空闲退出，CLI/Knowledge MCP/Desktop 不依赖它。
