# AllToNote Review 与 Publisher 设计

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-12-alltonote-llm-iwiki-desktop-design.md
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-14-alltonote-portable-artifact-source-bundle-design.md
downstream:
  - ../plans/2026-07-18-alltonote-review-publisher-implementation-plan.md
implementation_status: not-started
last_verified_at: 2026-07-18
```

## 1. 决策摘要

Review/Publisher 是知识生产与正式知识之间唯一的安全边界：

```text
Producer
  -> raw/personal 中的 Source / Transcript / Evidence / Draft / Quality
  -> 用户审阅“这个确切 hash 的 Draft”
  -> Publisher dry-run 生成 PublishPlan
  -> 用户确认
  -> 只通过 iwiki 已发布 SDK/CLI 合同原子发布
  -> wiki/personal（默认）

wiki/common
  -> 必须由用户明确选择目标
  -> 再通过 common 专用确认/授权
  -> 不允许默认、推断或静默自动发布
```

Quality pass 只说明机器检查通过，不等于用户审批。Review approval 只对一个不可混淆的 Draft 内容 hash 有效；内容一旦改变，审批自动失效。

## 2. 用户问题

没有 Review/Publisher 时，当前系统只能证明“能生成高质量草稿并提交 Bundle”，还没有形成个人知识库产品闭环。用户需要：

- 同时查看原始来源、Transcript、Evidence 与生成笔记；
- 快速判断遗漏、误解、幻觉、翻译偏差和引用错误；
- 编辑草稿但不覆盖来源事实；
- 知道发布会创建或修改哪些正式文件；
- 防止 Publisher 覆盖 Obsidian/编辑器中的并发修改；
- 一键发布到 `wiki/personal`，但不会误发到 `wiki/common`；
- 失败后能重试、恢复或撤销，不产生半发布状态；
- 让 CLI/Agent 可以协助审阅，但不能绕过人的最终控制。

## 3. 目标与非目标

### 3.1 目标

- 完成第一条 `produce -> review -> publish -> read` 闭环；
- 保留 Source/Draft/Published 的可追溯关系；
- 所有写入原子、幂等、可预览、可冲突检测；
- CLI、Desktop 共用相同 Core；
- 对外部编辑友好；
- 默认个人发布、公共发布双重保护；
- 遵守 iwiki 对 Workspace、publish、index 和 schema 的所有权。

### 3.2 非目标

MVP 不实现：

- Google Docs 式实时协作；
- 自动三方语义合并；
- 多人云审批流；
- 自动发布到 common；
- 让 Agent 直接改正式 Wiki；
- 在 Publisher 内重新调用 LLM；
- 把 Review 数据建成新的云端主数据库；
- 以 Git commit 作为所有用户的强制前置条件；
- 绕过 iwiki SDK 直接写其控制文件或索引。

## 4. 领域不变量

1. Source、Transcript 和 Evidence 是来源事实，不因审阅编辑而改变。
2. Draft 是候选知识，不是正式知识。
3. Review approval 必须绑定 `draft_id + content_hash + quality_report_id`。
4. 发布前必须重新验证 Draft hash、Quality、Workspace、目标和当前目标文件 hash。
5. `wiki/personal` 是默认目标；没有显式目标时绝不进入 common。
6. common 需要独立 `PublishGrant` 或现场二次确认；普通 Workspace write grant 不足够。
7. Publisher 只消费已验证 Bundle/Artifact；不接受任意未登记临时文件作为可信来源。
8. 任何冲突都 fail closed；不以“最后写入者获胜”覆盖用户编辑。
9. PublishPlan 是快照；apply 时任一前置状态变化都使 plan stale。
10. 正式写入只通过当前 iwiki capability/SDK/CLI，AllToNote 不拥有 schema/index 的私有扩展权。
11. 终态 publish Job 不复活；重试创建新 Job 并引用原计划/失败记录。
12. 发布失败不改变 Source Bundle 的事实内容，也不产生半个 Wiki 文档。

## 5. 核心模型

### 5.1 ReviewCandidate

```json
{
  "review_candidate_id": "rc_...",
  "workspace_id": "...",
  "bundle_id": "...",
  "source_revision_id": "...",
  "draft_id": "...",
  "draft_kind": "knowledge-note",
  "content_hash": "sha256:...",
  "quality_report_id": "...",
  "quality_status": "pass",
  "created_by_job_id": "job_...",
  "recommended_target": "personal",
  "recommended_path": "topic/example.md"
}
```

Candidate 是投影，可从 Bundle/Job 重新构建，不成为新的正文事实源。

### 5.2 ReviewRecord

```json
{
  "review_id": "review_...",
  "candidate": {
    "draft_id": "...",
    "content_hash": "sha256:...",
    "quality_report_id": "..."
  },
  "decision": "approved",
  "reviewer": {"kind": "local-user", "subject_ref": "..."},
  "decided_at": "...",
  "comment": "optional, bounded",
  "checks": {
    "source_compared": true,
    "citations_checked": true,
    "target_confirmed": true
  },
  "supersedes_review_id": null
}
```

MVP 决策：

- `approved`：允许为相同 hash 建立计划；
- `rejected`：不允许发布，可附原因；
- `superseded`：存在更新 Draft/Review；
- `revoked`：用户撤销尚未应用的审批。

`stale` 和 `published` 是根据当前事实计算的状态，不应修改旧 ReviewRecord 的历史决定。

### 5.3 PublishTarget

```json
{
  "space": "personal",
  "relative_path": "topic/example.md",
  "workspace_id": "..."
}
```

约束：

- 只接受规范化相对路径；
- 不允许 `..`、绝对路径、设备路径、ADS、保留名称或越权 reparse point；
- `space` 只能是当前 iwiki capability 声明的正式发布空间；
- common 目标必须额外标记 `sensitivity=public/shared`。

### 5.4 PublishPlan

PublishPlan 是 dry-run 结果：

```json
{
  "publish_plan_id": "pp_...",
  "plan_version": 1,
  "created_at": "...",
  "expires_at": "...",
  "workspace_contract": {"version": 1, "schema_hash": "..."},
  "draft": {"id": "...", "content_hash": "sha256:..."},
  "review": {"id": "...", "decision": "approved"},
  "target": {"space": "personal", "relative_path": "..."},
  "base": {"exists": true, "content_hash": "sha256:...", "document_id": "..."},
  "operations": [
    {"kind": "update-document", "path": "..."},
    {"kind": "update-index", "owner": "iwiki"}
  ],
  "warnings": [],
  "plan_digest": "sha256:..."
}
```

AllToNote 可描述 iwiki 将执行的高层操作，但不得伪造或持久化其私有内部写入步骤。

### 5.5 PublishReceipt

apply 成功后记录：

```json
{
  "publish_receipt_id": "pr_...",
  "publish_plan_id": "pp_...",
  "job_id": "job_...",
  "draft_id": "...",
  "draft_hash": "...",
  "review_id": "...",
  "target": {"space": "personal", "relative_path": "..."},
  "before_hash": "...",
  "after_hash": "...",
  "iwiki_commit_receipt_ref": "...",
  "committed_at": "..."
}
```

Receipt 的持久化位置遵循 iwiki 已发布合同或机器级 Job 审计。若 iwiki 尚未发布可移植 publish receipt 位置，AllToNote 不得自行向 Workspace 控制区写私有文件。

## 6. Draft 编辑模型

### 6.1 Draft 不原地“变成正式文档”

Draft 保留在 raw/personal 范围；发布产生或更新 wiki/personal 文档。这样可以：

- 保留来源生成历史；
- 比较 Draft 与用户最终编辑；
- 避免移动文件破坏 Bundle；
- 支持同一来源的 Knowledge Note 和 Faithful Edition 独立发布。

### 6.2 外部编辑

用户可用 AllToNote、Obsidian 或编辑器修改 Draft 文件。Publisher 每次读取当前内容并计算 hash：

- 与审批 hash 相同：继续；
- 已改变：旧审批显示为 stale，必须重新审阅/批准；
- 文件缺失或无法解析：Candidate invalid；
- Evidence 引用损坏：Quality 重新验证，不允许直接发布。

MVP 不要求用户通过 AllToNote 保存，但保存后的文件必须仍符合 Draft Artifact 合同。

### 6.3 编辑后的质量

用户编辑不触发完整 LLM 重写。确定性检查至少重跑：

- Markdown 语法/标题层级；
- Evidence 引用完整性；
- 禁止路径/链接；
- 必需 metadata；
- 目标空间策略；
- 可选敏感信息扫描。

模型型质量检查只有用户显式请求且预算确认后运行，并生成新的 QualityReport。

## 7. 审阅体验

### 7.1 必需视图

Desktop 的 Review Workspace 至少有：

1. Draft 阅读/编辑；
2. Source metadata 与原链接/文件定位；
3. Transcript/Evidence 定位面板；
4. Quality 问题列表；
5. 变更 diff（相对初始 Draft 或上次发布版本）；
6. 目标空间/路径预览；
7. approve/reject/revoke；
8. PublishPlan dry-run 和冲突说明。

不要求把所有面板同时铺满。默认阅读 Draft；点击引用时定位 Evidence；发布前显示结构化摘要。

### 7.2 Evidence 导航

- 视频：时间范围、字幕片段、可用截图；
- 网页：快照段落/DOM locator；
- PDF/PPT：页/slide/bbox；
- 代码：repo/commit/file/line/symbol；
- Personal：来源日志/commit/会议片段。

Review UI 只使用统一 EvidenceRef，再由对应 Source adapter 解析展示，不硬编码每种 Recipe 的业务规则。

### 7.3 风险提示

以下情况在 approve 前必须显式显示：

- Quality 非 pass 或有 waived issue；
- 来源不完整/低置信 OCR/自动字幕；
- 翻译型 Faithful Edition；
- 目标已有文档；
- 引用到外部不可移植资源；
- 可能含个人/敏感信息；
- common 发布。

## 8. 发布生命周期

```text
candidate inspect
  -> deterministic validation
  -> approve exact hash
  -> create PublishPlan (dry-run)
  -> show create/update/no-op/conflict + target
  -> confirm
  -> create publish Job
  -> revalidate every precondition
  -> call iwiki apply/commit atomically
  -> verify returned receipt and target hash
  -> persist Job result/PublishReceipt
  -> refresh derived index/read model
```

### 8.1 Create

目标不存在时，计划为 create。apply 前若目标突然出现，计划 stale，返回 conflict，不转为 update。

### 8.2 Update

目标存在时，PublishPlan 记录 `before_hash`。apply 前 hash 不同即冲突。MVP 不自动合并。

### 8.3 No-op

目标规范化内容 hash 与待发布内容一致时：

- 返回成功 no-op；
- 可记录审阅/来源关联更新，但只能通过 iwiki 支持的合同；
- 不改文件 mtime 或制造无意义 revision。

### 8.4 Rename/new document

冲突后用户可选择新路径，必须生成全新 PublishPlan；旧 plan 不可修改后重用。

### 8.5 Replace/supersede

“替代旧文档”是业务语义，不等于强制覆盖。必须显示旧文档 identity、引用/backlink 影响和差异；由 iwiki 支持的方式记录 supersession。若合同不支持，不私自发明 frontmatter 字段。

## 9. common 发布保护

### 9.1 默认拒绝

- UI 默认选 personal；
- CLI 未指定 `--target` 时为 personal；
- MCP Production 默认不暴露 common publish；
- Agent 不能从正文内容或 Prompt 推断 common；
- `--yes` 不能绕过 common 专用授权。

### 9.2 两种允许方式

交互式：

1. 用户显式选择 common；
2. 显示公开/共享影响、目标和 diff；
3. 用户执行第二次确认；
4. 生成短期一次性 consent token，绑定 plan digest。

自动化：

- 用户事先创建有范围、有效期、调用者身份和路径限制的 `CommonPublishGrant`；
- grant 明确是否仍需 human approval；
- Job 记录 grant ID，不记录 Secret；
- 超出范围或过期 fail closed；
- 默认产品不提供无限期全空间 common 自动发布授权。

## 10. 冲突与恢复

### 10.1 冲突类型

| Code | 含义 | 用户动作 |
|---|---|---|
| `DRAFT_CHANGED_AFTER_REVIEW` | Draft hash 已变化 | 重新审阅 |
| `QUALITY_REPORT_STALE` | Quality 不对应当前内容 | 重跑确定性检查/质量流程 |
| `TARGET_CREATED_AFTER_PLAN` | create 目标已出现 | 查看目标、选择 update 或新路径 |
| `TARGET_CHANGED_AFTER_PLAN` | update base hash 已变化 | 重新 diff/plan |
| `WORKSPACE_CONTRACT_CHANGED` | iwiki contract/schema 漂移 | 重新 inspect/validate/plan |
| `GRANT_EXPIRED` | 授权失效 | 重新授权 |
| `COMMON_CONSENT_REQUIRED` | 缺少 common 二次确认 | 现场确认或新 grant |

### 10.2 崩溃恢复

Publisher 把外部 iwiki commit 视为 ExternalOperation：

- 调用前记录 intended plan digest；
- 返回成功先持久化 provider receipt，再完成 Job；
- 崩溃后若 outcome unknown，先向 iwiki reconcile/inspect target；
- target hash/receipt 可证明成功时完成 Job；
- 无法证明时停在 `needs-reconciliation`，不盲目重放。

### 10.3 撤销

撤销是新的显式操作：

- 只有目标仍等于该 PublishReceipt 的 `after_hash` 时，才可自动创建 reverse plan；
- 如果之后被用户编辑，返回冲突；
- reverse plan 仍需要 dry-run 和确认；
- 默认保留历史，不直接物理删除来源 Bundle；
- 删除/恢复语义服从 iwiki 合同。

## 11. CLI 合同

```text
alltonote review list --workspace <ref> [--status pending]
alltonote review show <draft-id> [--json]
alltonote review validate <draft-id> [--json]
alltonote review approve <draft-id> --content-hash <hash>
alltonote review reject <draft-id> --reason-code <code>
alltonote review revoke <review-id>

alltonote publish plan <draft-id> --target personal --path <relative> --json
alltonote publish apply <plan-id> [--confirm-plan-digest <digest>] --json
alltonote publish status <job-id>
alltonote publish reconcile <job-id>
alltonote publish undo-plan <receipt-id> --json
```

约束：

- `approve` 必须传或交互确认当前 hash；
- `publish apply` 不接受任意 Markdown 路径，只接受有效 plan；
- plan 默认短期有效；
- common 不能仅靠 `--yes`；
- `--json` 遵守 Runtime CLI envelope；
- 所有命令均可在无 Desktop 环境执行。

## 12. Desktop API

Desktop API 暴露 Application Service 投影：

```text
GET  /v1/reviews
GET  /v1/reviews/{draft_id}
POST /v1/reviews/{draft_id}/validate
POST /v1/reviews/{draft_id}/decision
POST /v1/publish/plans
GET  /v1/publish/plans/{id}
POST /v1/publish/plans/{id}/apply
GET  /v1/jobs/{id}
```

不得提供 `POST /write-file-anywhere` 或直接调用 iwiki 私有控制文件的 API。Desktop 的 Markdown 编辑保存走受控 Draft service，仍触发 hash/validation。

## 13. Agent 与 MCP 边界

- Knowledge MCP 只读取已发布知识，不负责 Review/Publish；
- Production MCP 可以创建 Draft、查询 Candidate 和请求 Review，但默认不能代替本地用户批准；
- Agent 可以提出修订建议，结果生成新的 Draft revision；
- Agent 不能复用旧 approval 发布新内容；
- Agent 不能通过 Prompt 声称“用户已同意 common”；
- Publisher 不向模型发送整个私人 Vault；只按任务显式选择最小上下文。

## 14. 安全

- Review UI 渲染不可信 Markdown，禁用脚本和任意 scheme；
- Source URL、HTML、PDF 和模型正文都不成为 Publisher 指令；
- 所有路径做 canonical/containment/reparse point 验证；
- target diff 不加载远程图片或执行嵌入内容；
- 防止 Markdown 链接写入 `file://`、UNC、设备路径等敏感目标（策略可按 iwiki 合同）；
- comment、reason 和 metadata 有长度/字符限制；
- Review/Publish audit 不保存完整私密正文，使用 ID/hash/问题摘要；
- common 前可选执行敏感信息扫描，但扫描 pass 仍不能替代确认。

## 15. 性能与可用性

- 打开 Candidate 不应同步加载完整视频/全部 PDF 页面；Evidence 按需读取；
- 大 Transcript 使用范围读取和虚拟列表；
- diff 先做文本 hash/line diff，超大文件限制渲染行数但不跳过验证；
- PublishPlan 为纯确定性操作，不调用 LLM；
- apply 不重新运行 Producer；
- 读取已有 Bundle/索引的 Candidate 列表 p95 目标 < 500 ms；
- 普通单文档 plan p95 目标 < 1 s（不含慢文件系统/外部 iwiki 实现）；
- 所有慢操作产生 Job/event，UI 可离开页面后恢复。

## 16. 测试矩阵

### 16.1 领域测试

- approval 精确绑定 hash；
- Draft 改变后 stale；
- Quality stale 阻止 plan；
- personal 默认；
- common 无额外授权必拒绝；
- plan digest/expiry；
- create/update/no-op/conflict；
- 终态 Job retry 新建；
- undo 只在 target 未改变时允许。

### 16.2 文件与并发

- Obsidian 在 plan 后修改目标；
- 外部编辑器在 approve 后修改 Draft；
- symlink/junction/大小写/Unicode/保留名；
- watcher 延迟不影响 apply 前的真实重读；
- 两个 Publisher 同时更新同一目标只能一个成功；
- 磁盘满、权限变化、杀进程、iwiki commit 超时；
- outcome unknown reconcile。

### 16.3 产品 E2E

至少覆盖：

1. Video Knowledge Note -> Review -> personal create；
2. Faithful Edition -> 独立 Review -> personal create；
3. 目标已有内容 -> diff -> update；
4. 计划后外部修改 -> conflict -> re-plan；
5. Draft 编辑 -> 旧 approval stale -> 重新 approve；
6. common 无授权拒绝；
7. common 双重确认成功；
8. apply 中断 -> 重启 reconcile -> 无重复写；
9. undo plan -> 恢复且保留审计；
10. CLI-only 与 Desktop 使用相同服务得到相同结果。

## 17. 分期

### Phase P0：只读 Candidate/Validate

从现有 Bundle 建立 Candidate 投影，完成 Source/Evidence/Draft/Quality 对照和确定性验证。

### Phase P1：Review 决策

完成 exact-hash approve/reject/revoke、stale 计算与 CLI；不写正式 Wiki。

### Phase P2：personal Publisher

完成 dry-run、create/update/no-op/conflict、iwiki atomic apply、receipt/reconcile。

### Phase P3：Desktop Review UI

完成并排阅读、Evidence 定位、diff、审批和 personal 发布体验。

### Phase P4：common 保护与撤销

完成专用 grant/二次确认、敏感提示、undo plan 和审计。

## 18. 完成定义

1. 用户能从当前 Video Bundle 完成 personal 发布闭环；
2. approval 与 Draft hash/Quality 精确绑定；
3. 所有计划可 dry-run 且 apply 前重新验证；
4. Obsidian/外部编辑冲突不会被覆盖；
5. 崩溃后能确定 reconcile，绝不盲目重放；
6. 默认 personal，common 无双重授权绝不成功；
7. CLI-only 和 Desktop 共用同一 Core；
8. Publisher 只通过 iwiki 公开合同写入；
9. Vault 中不存在 AllToNote 私造的 iwiki 控制文件；
10. 全部领域、并发、安全和 E2E Gate 通过。
