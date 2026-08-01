# AllToNote Review 与 Publisher 实施计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-18-alltonote-review-publisher-design.md
  - ../specs/2026-07-14-alltonote-portable-artifact-source-bundle-design.md
  - ../specs/2026-07-13-alltonote-cli-first-vault-workspace-design.md
implementation_status: in-progress
last_verified_at: 2026-08-01
```

## 1. 前置 Gate

- Portable Bundle/iwiki semantic validate/commit 稳定；
- Vault Core 能安全读取 Draft/Source/Evidence/PublishedDocument；
- Runtime CLI envelope/Job 合同稳定；
- 当前 iwiki capability 已验证，不假设私有 publish API；
- Video 至少一个真实 Bundle 可作为首个 Candidate fixture。

若 iwiki 没有本计划需要的原子 publish/compare-and-apply 能力，先在 iwiki 所有者仓库按其合同流程设计/发布，AllToNote 不私造控制文件或直接写索引。

## 2. 成功标准

- exact-hash approve/reject/revoke；
- Draft/Quality 改变后 approval stale；
- dry-run create/update/no-op/conflict；
- apply 前重验且原子；
- personal 默认；
- common 双重授权；
- crash/outcome unknown reconcile；
- CLI-only 与 Desktop 共用服务；
- Obsidian 并发修改不会被覆盖。

## 3. Task RP-00：iwiki Publish Capability Spike

只做 read-only/临时 fixture 验证：

1. inspect current SDK/CLI capability；
2. 找到 supported create/update/validate/commit/receipt/conflict API；
3. 证明 compare base hash/atomic apply 的能力或缺口；
4. 证明 index 更新由 iwiki 所有；
5. 记录 rollback/delete/supersede 合同；
6. 在临时 Workspace 运行，不改用户 Vault；
7. 把结果写 contract test/decision note。

Gate：每个 Publisher 操作有已发布 API；否则停止实施该操作。

## 4. Task RP-01：Review 领域模型

目标文件建议：

- `backend/app/core/domain/review.py`
- `backend/app/core/domain/publish.py`
- `backend/tests/core/test_review_domain.py`

先写测试：

- Candidate identity/hash/Quality；
- decision state；
- stale 计算；
- terminal/history immutable；
- personal default；
- common grant requirement；
- plan expiry/digest。

实现纯领域对象，不读文件/数据库/iwiki。

## 5. Task RP-02：ReviewCandidate Query Service

状态（2026-08-01）：P0 单 Candidate 只读切片已完成。`alltonote review show <draft-id>` 从已提交 Portable Bundle 重建 Candidate，默认返回 Source 与完整 Quality checks/messages；`--evidence-id` 按需展开一个 Video time-range/transcript excerpt，`--note-item-id` 按需展开一个 Document semantic claim 及其 page/bbox/source blocks。所有投影均绑定 Draft hash、source revision 与 artifact parent lineage，保持有界、只读、无模型调用且不保存第二份正文。

后续真实性修正（2026-08-01）：共享 CLI renderer 已在 CP936/GBK stdout/stderr 上改为严格 UTF-8，真实 V03 的 `🚀` 标题在 human/JSON Review 中可逆保留；ReviewCandidate 同时增加当前 Quality admission。旧 `alltonote.document-note@1`/native-extraction 历史 flag 不再获得当前发布资格；历史同名 Document Knowledge Note 也必须由已提交 Quality Report 与 Knowledge Map 证明独立语义验证，不能仅凭 profile 名称准入；Video `pass_with_warnings` 保持可发布，未知 profile fail closed。该修正只收紧只读投影，不回写旧 Bundle，也不表示 ReviewRecord 或 Publisher 已完成。验收见 [`CLI Unicode 与 Review 发布准入验收`](../../acceptance/2026-08-01-cli-unicode-quality-admission.md)。

本切片基于最小必要面没有提前创建 `review_store.py` 或 ReviewRecord 领域对象；它们仍属于 RP-01/RP-03 的 decision/approval 阶段。RP-02 尚未完成 list/filter/cursor，也未包含审批、发布或 Desktop UI。

目标文件：

- `backend/app/core/application/review_candidate_service.py`
- `backend/app/core/ports/review_store.py`
- `backend/tests/core/test_review_candidate_service.py`

步骤：

1. 从 Job result/Bundle/PortableGateway 投影 Candidate；
2. 多 Draft 独立 candidate；
3. 读取 Quality/publish eligibility/warnings；
4. Source/Transcript/Evidence bounded navigation；
5. list/filter/cursor；
6. 当前 Draft hash 真实重读；
7. 不把 Candidate 作为第二正文副本；
8. 损坏/missing/stale 错误。

## 6. Task RP-03：ReviewRecord Store

第一版 machine-local SQLite/Job audit store，除非 iwiki 发布了 portable review record 位置。

步骤：

1. schema/migration；
2. append immutable decision；
3. reviewer local subject ref；
4. approve exact current hash；
5. reject/revoke/supersede；
6. bounded comment/reason；
7. 并发 compare；
8. 不保存 Draft 正文；
9. backup/restore/retention。

## 7. Task RP-04：确定性 Draft Revalidation

复用 Portable/Markdown/Quality，不调用 LLM：

- hash；
- Markdown/title/citations；
- Evidence refs；
- metadata；
- target policy；
- sensitive/unsafe links（策略）；
- QualityReport 与 current content 关联。

用户编辑后若需要新的 QualityReport，生成 typed deterministic report；旧 report 不改写。

## 8. Task RP-05：PublishPlan Service

目标文件：

- `backend/app/core/application/publish_plan_service.py`
- `backend/app/core/ports/publisher.py`
- `backend/tests/core/test_publish_plan.py`

步骤：

1. 输入 candidate/review/target；
2. personal 默认和 relative path policy；
3. inspect target exists/hash/document identity；
4. 计算 create/update/no-op；
5. conflict/unsupported；
6. 捕获 workspace contract/schema hash；
7. operations/warnings/expiry/digest；
8. plan machine store；
9. 计划无 LLM、无写入；
10. common 仅在专用授权存在时生成可应用 plan。

## 9. Task RP-06：IWikiPublisher Adapter

目标文件：

- `backend/app/adapters/iwiki/publisher.py`
- 扩展现有 gateway，不重复 SDK boot
- `backend/tests/adapters/test_iwiki_publisher.py`

步骤：

1. typed dry-run/apply/reconcile adapter；
2. 所有写走 published SDK；
3. base hash/contract/grant compare；
4. provider receipt 持久化；
5. SDK error -> conflict/commit/outcome unknown；
6. 不读写私有 schema/index；
7. malicious path；
8. failure injection。

## 10. Task RP-07：Publish Job/Application Service

目标文件：

- `backend/app/core/application/publisher_service.py`
- Job checkpoint/external operation integration
- tests

步骤：

1. apply 只接受 plan ID/digest；
2. 获取 plan、review、grant；
3. 重读 Draft/target/contract；
4. stale 则失败，不自动 re-plan；
5. 外部 operation intended -> call -> result persist -> terminal；
6. restart reconcile；
7. no-op 不制造文件变更；
8. receipt projection；
9. retry 新 Job；
10. refresh derived index/event。

## 11. Task RP-08：CLI

实现设计中的 `review list/show/validate/approve/reject/revoke` 与 `publish plan/apply/status/reconcile/undo-plan`。

测试：

- JSON golden；
- hash required；
- plan digest/expiry；
- common `--yes` 不足；
- conflict exit code；
- outcome unknown action；
- bounded content/path redaction；
- Desktop 不运行的完整闭环。

## 12. Task RP-09：common Grant/Consent

步骤：

1. `CommonPublishGrant` domain/store；
2. path/space/audience/expiry/one-time policy；
3. interactive consent token 绑定 plan digest；
4. CLI/Desktop 二次确认；
5. Agent/Production MCP 默认不可创建 grant；
6. revoke/audit；
7. common敏感提示/scan；
8. tests：旧 token、跨 plan、重放、过期、越域。

## 13. Task RP-10：Undo/Reconcile

前提：iwiki capability 支持相应语义。

1. receipt -> current target hash；
2. unchanged 才能生成 reverse plan；
3. changed -> conflict；
4. dry-run/confirm/apply；
5. 保留历史；
6. source Bundle 不删除；
7. kill before/after commit 的 reconcile matrix；
8. 无法证明 outcome -> needs-attention。

## 14. Task RP-11：Desktop API

在临时 Desktop API 添加 Candidate/decision/plan/apply/job routes，全部调用 Application Services。

安全：session auth、Origin、bounded range、无 arbitrary write/path、common consent 不在 URL、Markdown sanitize。

## 15. Task RP-12：Desktop Review UI

建议 `src/features/review/`：

1. candidate inbox/filter；
2. Draft Reader/Editor（MVP 可先外部编辑 + 只读）；
3. Source/Evidence/Transcript locator；
4. Quality issues；
5. Draft/base diff；
6. approve/reject/revoke；
7. target/path；
8. dry-run summary；
9. conflict/stale repair flow；
10. personal apply/progress/receipt；
11. common 二次确认；
12. accessibility/keyboard/large transcript lazy load。

不要在 React 中计算 approval/plan/conflict。

## 16. Task RP-13：并发与故障 E2E

必须覆盖：

- approve 后外部改 Draft；
- plan 后 Obsidian 改 target；
- 两 Publisher 同 target；
- Workspace move/contract change；
- disk full/permission；
- Runtime kill before/during/after commit；
- outcome unknown；
- common grant revoke；
- undo 后又编辑；
- index watcher stale。

## 17. Task RP-14：首个真实产品闭环

使用已验收 Video Bundle：

```text
Knowledge Note + Faithful Edition
 -> 两个 Candidate
 -> Source/Evidence navigation
 -> 编辑其中一个 Draft
 -> stale -> revalidate -> approve
 -> personal plan/diff
 -> apply -> iwiki receipt
 -> Vault read/search
 -> Obsidian 打开
 -> restart zero duplicate
```

另跑 common 无授权拒绝和双确认成功的隔离测试 Workspace。

## 18. Task RP-15：验收与交接

运行领域、adapter、CLI、Desktop、全量回归；输出 `docs/acceptance/review-publisher-v1.md`，记录 IDs/hash/operation/result/冲突/恢复，不保存正文/Secret/路径。

更新 master tasks：`REVIEW-01` 只有在 CLI-only personal + Desktop + common protection + crash matrix 都通过后标完成。

## 19. 执行顺序

```text
RP-00
 -> RP-01..04
 -> RP-05..07
 -> RP-08
 -> RP-09/10
 -> RP-11/12
 -> RP-13/14/15
```
