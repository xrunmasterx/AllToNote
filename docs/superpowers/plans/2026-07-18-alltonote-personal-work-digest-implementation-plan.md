# AllToNote Personal Work Digest 实施计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-18-alltonote-personal-work-digest-design.md
implementation_status: not-started
last_verified_at: 2026-07-18
```

## 1. 原则

先做手动 Daily + Git/Markdown，证明用户价值；不先做常驻监控、云 Connector 或 scheduler。所有来源 opt-in、只读、personal；调度依赖 Engine 完成。

## 2. 成功标准

两个 repo + journal 的 daily digest；固定时区/窗口；重复运行零重复/无新事件 no-op；late event生成新Draft；planned/completed不混淆；Evidence可回到commit/日志；Review/personal发布；不改源、不上传网站。

## 3. Task PD-00：用户样本与 Rubric

用脱敏真实一周工作痕迹建立：成果、决定、风险、下一步、计划vs完成、跨项目、迟到事件。定义人工评分，不用commit数/篇幅当质量。

## 4. Task PD-01：WorkSource Catalog/Grant

Git repo、journal root、project mapping；machine config只存opaque refs；add/list/doctor/remove；revoke后不再扫描。不扫描home/全盘。

## 5. Task PD-02：ActivityEvent/Window

领域模型、stable event ID、occurred/observed、timezone、[start,end)、watermark/lookback/late。测试上海自然日、DST、午夜、clock/timezone change。

## 6. Task PD-03：Git Connector

固定 repo/commit objects；author/time window；commit/message/stats/paths；可选bounded diff；merge/revert/empty；无hooks/network/write。event Evidence=repo+commit。不要从message单独判定成果。

## 7. Task PD-04：Markdown Journal Connector

文件hash/heading/line range/date/frontmatter；增量cursor、rename/edit；symlink/reparse/secret patterns；Markdown Prompt injection只作内容。

## 8. Task PD-05：Activity Ledger/Dedup

Artifact + machine index projection；exact event去重；cross-source relation不删除；connector coverage/partial failure；同window重扫稳定。

## 9. Task PD-06：Project Mapping

显式repo/folder映射 > metadata > user rules > LLM候选。低置信进入unassigned/确认，不让LLM跨grant移动。

## 10. Task PD-07：Fact Map

结构化 accomplishment/change/decision/problem/question/next-action/learning；每项Evidence；planned/completed枚举；模型输入最小化；无Evidence事实拒绝或标inference。

## 11. Task PD-08：Daily Compiler/Quality

按项目组织、合并关联事件、明确coverage/incomplete connectors、无生产力评分。Quality：window/dedup/project/planned-vs-done/evidence/privacy/Markdown。

## 12. Task PD-09：Job/Bundle/CLI

`work-source`命令与 `produce work-digest --daily --timezone`；checkpoint/cancel/restart；相同window+source revisions no-op不调用模型；raw/personal Bundle/ReviewCandidate。

## 13. Task PD-10：Late Event/Amendment

lookback发现晚到：未发布则new Draft revision；已发布则amendment Candidate/diff；不直接覆盖。超grace补录策略。

## 14. Task PD-11：Meeting File Connector

本地 transcript/notes only；meeting identity/time、paragraph/time Evidence、参与者privacy；不录音/麦克风。partial source coverage。

## 15. Task PD-12：Weekly/Project Chronicle

复用ledger/fact map而非旧Draft作为事实；跨天主题/决定演进/未解决问题；仍回到原event；周边界/时区测试。

## 16. Task PD-13：Privacy/Security

Secret/PII pre-model/post-Draft、local-only/provider policy、敏感项目skip、本地路径脱敏、日志无正文、repo不执行、网站零上传、common不可用。用canary验证。

## 17. Task PD-14：Review/Publisher

Daily/Weekly Candidate -> source Evidence -> edit/approve -> personal publish；late amendment冲突；MCP Read只读published digest/explicit evidence。

## 18. Task PD-15：Scheduler（Engine 后）

只在 Engine完成：schedule store/timezone/missed-run/idempotent window/background budget/pause/delete；创建Candidate通知，不approve/publish；机器关机不补跑无界历史。

## 19. Task PD-16：云 Connector（逐个专项）

calendar/issue/email/chat均需独立最小OAuth scope、撤销、缓存、privacy设计。优先以用户导出文件/ICS验证价值；不因网站账号存在自动接入。

## 20. Task PD-17：真实验收

连续多日真实数据：重复/late/partial/no-op/跨项目/周汇总；记录event counts/coverage/model calls/人工评分/privacy，不保存正文或私有路径。输出 `docs/acceptance/personal-work-digest-v1.md`。

## 21. 执行顺序

```text
PD-00..05
 -> PD-06..10
 -> PD-11/12
 -> PD-13/14
 -> PD-17
 -> PD-15（Engine后）
 -> PD-16（逐项需求后）
```
