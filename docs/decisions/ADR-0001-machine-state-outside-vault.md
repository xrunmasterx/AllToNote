# ADR-0001：机器运行状态必须位于 Vault 之外

```yaml
doc_type: architecture-decision
status: active
authority: system
upstream:
  - ../superpowers/specs/2026-07-13-alltonote-knowledge-compiler-architecture-design.md
downstream:
  - ../superpowers/specs/2026-07-13-alltonote-cli-first-vault-workspace-design.md
  - ../superpowers/specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
  - ../superpowers/specs/2026-07-18-alltonote-engine-production-mcp-design.md
implementation_status: applies-to-all-new-work-migration-audit-pending
last_verified_at: 2026-07-18
```

## 状态

已接受，适用于所有新增实现。它明确替代早期 Vault 设计中“未来可把 `jobs.sqlite` 放进 Workspace `.cache`”的局部建议。

## 背景

AllToNote 的长期价值来自开放、可搬迁、可由 Obsidian、Git、CLI、MCP 和其他 Agent 直接使用的 Markdown 知识资产。Job、Attempt、lease、scheduler、进程心跳、临时日志和 ExternalOperation 却描述某一台机器上的执行现实：它们可能包含绝对路径、PID、锁、资源占用和未完成外部调用，不能随 Vault 同步到另一台机器后继续解释。

把两类数据放在同一个 Vault 会产生四个根本冲突：

1. 可移植知识与不可移植进程状态混在一起；
2. Obsidian/Git/同步工具会传播高频噪声、锁和临时状态；
3. 两台设备可能同时认为自己拥有同一个 Job lease；
4. 用户复制或恢复 Vault 时会意外恢复过期任务和绝对路径。

## 决策

- Vault 保存长期事实：Markdown、附件、Source、Transcript、Evidence、Draft、Quality、Receipt、已发布知识和可重建索引的配置。
- 机器状态目录保存操作状态：JobStore、Attempt、Checkpoint、ExternalOperation、lease/fencing token、scheduler、运行日志、下载/转写临时缓存和 Pack 安装状态。
- 机器状态目录必须通过平台标准用户目录解析，不硬编码到仓库或 Vault；Windows 和 macOS 路径由统一 Runtime path service 提供。
- SQLite 可以作为机器本地 JobStore/索引实现，但永远不是知识事实源；删除它不得删除正式知识资产。
- Vault 内如需要展示任务来源，只保存稳定的 Job/Receipt 引用或已提交审计摘要，不复制活跃 lease/进程状态。
- Desktop、CLI、Engine 和 MCP 必须调用同一个 Runtime path service，不能各自决定状态路径。

## 备选方案

### 方案 A：全部放入 Vault `.cache`

优点是表面上容易“随 Vault 搬迁”。缺点是进程状态没有跨机器语义，且会制造同步冲突和恢复歧义，因此拒绝。

### 方案 B：知识正文也放入 Runtime 数据库

可以简化部分查询，但会锁定数据、破坏卸载后可读和 Obsidian/Git 互操作，因此拒绝。

### 方案 C：本机状态目录 + Vault 开放资产

边界与数据生命周期一致，允许缓存删除重建，并保持 Vault 可移植，因此采用。

## 影响

正面影响：

- Vault 更干净、可同步、可版本控制；
- Runtime/Engine 崩溃恢复有明确所有权；
- 多设备不会通过文件同步共享活跃 lease；
- 卸载 AllToNote 后知识仍可读取。

代价：

- 复制 Vault 不会自动复制未完成 Job；
- 设备间任务迁移未来需要显式 export/import 协议，而不能依赖同步 SQLite；
- 实现必须维护稳定的 `vault_id` 与机器本地路径映射。

## 迁移方式

1. 搜索是否已有 Vault 内 `jobs.sqlite`、lease 或 scheduler 写入；
2. 新版本只从平台状态目录打开 JobStore；
3. 如发现旧数据库，只提供显式、一次性、可回滚的任务元数据迁移，不移动知识资产；
4. 迁移前备份旧文件并记录 hash；
5. 迁移成功后旧文件仅标记可删除，不由程序自动递归删除；
6. 为 Vault copy、双设备打开、Job recovery 和删除 JobStore 重建编写集成测试。

## 回滚条件

只有出现“机器状态本身成为跨设备长期用户资产”的经验证产品需求，且已经设计独立的任务导出、所有权转移、冲突和加密协议时，才允许重新评估。不能因为路径管理不方便而回滚此决策。
