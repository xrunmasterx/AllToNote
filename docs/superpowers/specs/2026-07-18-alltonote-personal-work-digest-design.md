# AllToNote Personal Work Digest Recipe 设计

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-14-alltonote-portable-artifact-source-bundle-design.md
  - 2026-07-18-alltonote-recipe-extension-contract-design.md
  - 2026-07-18-alltonote-review-publisher-design.md
downstream:
  - ../plans/2026-07-18-alltonote-personal-work-digest-implementation-plan.md
implementation_status: not-started
last_verified_at: 2026-07-18
```

## 1. 决策摘要

Personal Work Digest 将用户明确授权的本地工作痕迹整理为可审阅的每日、每周或项目知识草稿：

```text
Git / 本地日志 / Markdown Journal / 会议记录 / 显式连接器
  -> ActivityEvent ledger（本地、可追溯、去重）
  -> 固定时间窗与项目归类
  -> 事实提取、决策/成果/风险/下一步组织
  -> Evidence-backed Digest Draft
  -> Review -> wiki/personal
```

它不是后台监控软件：

- 默认只处理用户选择的目录、仓库和文件；
- 默认手动运行；
- 定时运行只有按需 Engine 成熟后启用；
- 不采集键盘、屏幕、浏览历史、任意进程或全盘文件；
- 不自动发布，更不进入 common；
- 原始工作痕迹和模型上下文默认保持 personal。

## 2. 用户目标

- 自动整理“今天/本周真正做了什么”；
- 从 commit、日志和会议中提取成果、决定、问题、风险和待办；
- 同一事件不因多次运行重复出现；
- 每条事实能回到 commit、日志行或会议时间段；
- 能按项目而不是按数据来源组织；
- 用户可补充/纠正后发布为个人工作日志或项目知识；
- 延迟到达的日志/commit 可在下一次运行中被识别；
- 不需要保持 Desktop 常开；
- 隐私和模型上传范围清楚可控。

## 3. 非目标

- 员工监控、工时考勤或生产力评分；
- 键盘记录、截图、摄像头、全局浏览历史；
- 未经授权读取邮箱、聊天、日历或云盘；
- 自动向老板/团队发送报告；
- 自动把所有 commit message 当成真实成果；
- 取代 issue tracker/项目管理；
- 默认常驻 daemon；
- 自动发布 common；
- 把个人日志存入网站云数据库；
- 从缺失证据推断用户做过某件事。

## 4. 输出类型

### 4.1 Daily Digest

- 今日摘要；
- 按项目的完成事项；
- 关键决定与理由；
- 问题/风险；
- 未完成和下一步；
- 来源覆盖与缺口。

### 4.2 Weekly Review

- 本周目标与结果；
- 里程碑/主题；
- 跨天持续问题；
- 决策演进；
- 下周重点；
- 可选量化统计（commit 数等仅作辅助，不作生产力评价）。

### 4.3 Project Chronicle

按项目/时间范围生成连续演进记录，聚合关键事件、变更、决策和知识链接。

MVP 先实现 manual Daily Digest + Git/Markdown log；Weekly 在真实数据上验证后加入。

## 5. Source Connector

### 5.1 MVP Connector

#### Git activity

- 用户选定 repo；
- commit author identity/time range；
- commit ID/message/stats/changed paths；
- 可选 diff 摘要，受 scope/大小限制；
- dirty worktree 只在用户显式允许时作为 snapshot event；
- 不以 commit 数量评价工作价值。

#### Markdown/文本日志

- 用户选择 journal/log 目录；
- 文件 hash + line/range；
- frontmatter/date/heading；
- 增量读取；
- 外部编辑保持开放文件事实源。

#### 会议 Transcript/Notes 文件

- 已存在本地 transcript/notes；
- 会议 identity、时间、参与者（可选/敏感）；
- 时间段/段落 Evidence；
- 不在本 Recipe 内录音或偷偷采集麦克风。

### 5.2 后续 Connector

- calendar/ICS；
- issue tracker；
- Git hosting MR/PR；
- email/chat；
- time tracker；
- AllToNote Job/Review/Publish 活动。

每个云 Connector 必须独立 OAuth/最小 scope/缓存/撤销设计；不能因为网站账号已登录就默认读取其他服务。

## 6. ActivityEvent

```json
{
  "event_id": "ae_...",
  "source_id": "src_...",
  "source_revision_id": "sr_...",
  "connector": "git",
  "external_id": "commit:<full-id>",
  "occurred_at": "...",
  "observed_at": "...",
  "timezone": "Asia/Shanghai",
  "project_id": "project_...",
  "kind": "commit|log-entry|meeting-note|decision|task-update",
  "title": "...",
  "summary_input": "...",
  "locator": {},
  "content_hash": "sha256:...",
  "privacy": "personal",
  "confidence": {"level": "high", "basis": "git-object"}
}
```

### 6.1 Stable ID

`event_id` 由 connector namespace + source identity + external ID/revision + content hash 确定，避免同一 commit/日志段重复。不得只用时间+标题。

### 6.2 occurred 与 observed

- `occurred_at`：事件实际时间；
- `observed_at`：AllToNote 首次看到时间。

这允许处理迟到 commit、离线日志和 connector 延迟。

### 6.3 Ledger

Activity ledger 是 raw/personal 的可追溯 Artifact/Bundle 与机器级索引投影，不是新的不可读数据库事实源：

- 原始来源仍为 Git/Markdown/会议文件；
- ledger 可导出/验证；
- 机器级去重索引可重建；
- 不把整个个人活动发送到网站。

## 7. 时间窗

### 7.1 Window identity

```json
{
  "kind": "daily",
  "timezone": "Asia/Shanghai",
  "start": "2026-07-18T00:00:00+08:00",
  "end": "2026-07-19T00:00:00+08:00",
  "grace_period": "P2D",
  "window_id": "..."
}
```

使用半开区间 `[start, end)`，显式时区并测试 DST。不能用“最近 24 小时”代替本地自然日。

### 7.2 Watermark 与 late events

每个 connector 记录：

- last successful scan cursor；
- event-time watermark；
- source revision/hash；
- lookback grace。

每次扫描回看 grace 范围并以 event ID 去重。晚到事件：

- 尚未发布：更新同一 window 的新 Draft revision；
- 已发布：生成 amendment Candidate/diff，不直接覆盖正式日志；
- 超出 grace：列入下次 digest 的“补录”或用户显式重建。

## 8. Project mapping

项目归类优先级：

1. 用户显式 repo/folder -> project mapping；
2. Source metadata/frontmatter；
3. 用户维护的 alias/rule；
4. 模型候选（必须标低置信，可人工确认）。

LLM 不得凭内容把私密事件移动到另一个 grant/project。未分类事件进入 `unassigned`，而不是静默丢弃。

## 9. 生产流水线

### 9.1 Collect

- 在授权 scope 中增量读取；
- 固定 source revision；
- 产生 ActivityEvent；
- connector 失败按源隔离，保留成功源；
- 结果明确列出 incomplete connectors。

### 9.2 Normalize/deduplicate

- time/timezone；
- author/project identity；
- commit/log/meeting结构；
- exact event ID 去重；
- cross-source 相似事件只建立 relation，不自动删除；
- 过滤机器噪声需确定规则且可见。

### 9.3 Fact map

每个项目提取：

- accomplishment；
- change；
- decision；
- problem/risk；
- question；
- next action；
- learning/knowledge link。

每项绑定 event Evidence。模型不能把计划写成已完成，也不能从 commit message 单独推断用户影响。

### 9.4 Compose

- 按项目/主题而非 connector 排列；
- 合并同一工作的多个事件但保留引用；
- 区分事实、用户表述、模型归纳；
- 不生成虚假“高产”评价；
- 公开缺失 connector/低置信 mapping；
- Weekly 从已验证 daily/event ledger 聚合，但仍可回到原 event。

### 9.5 Quality

- window coverage；
- event dedup；
- project mapping；
- accomplished vs planned；
- decision/action 主体；
- Evidence 有效；
- privacy/secret scan；
- Markdown 结构；
- 未完成 connector 披露。

## 10. Evidence

Git：

```json
{
  "locator_kind": "activity-event-range",
  "locator": {
    "kind": "git-commit",
    "repo_id": "...",
    "commit": "<full-id>",
    "paths": ["..."],
    "event_id": "ae_..."
  }
}
```

日志：file hash + line/heading range；会议：transcript artifact + timestamp/paragraph。Published Digest 默认引用 opaque evidence token；是否显示绝对路径由 UI/Grant 决定。

## 11. Artifact

| Kind | 内容 |
|---|---|
| `personal/connector-snapshot` | connector cursor/revision/coverage |
| `personal/activity-ledger` | normalized ActivityEvents |
| `personal/project-map` | project mapping 与 confidence |
| `personal/fact-map` | facts/actions/risks + evidence |
| `draft` | daily/weekly/project digest |
| `quality-report` | coverage/dedup/privacy/evidence |
| `receipt` | connector/model/compiler identity |

Connector credential/token 永不进入 Artifact。

## 12. 手动与调度

### 12.1 手动优先

```text
alltonote produce work-digest --date 2026-07-18 --workspace <ref>
```

手动模式使用前台 durable Job，无 Engine 也可完成。这用于验证用户价值、Connector 和输出质量。

### 12.2 定时触发

Engine 产品需求已经触发，但只有 Wave 0–4 Gate 和 Engine lifecycle/authority 验收完成后才支持：

```text
alltonote schedule create work-digest \
  --daily-at 20:00 --timezone Asia/Shanghai --profile <ref>
```

Scheduler 只创建 Job，不在 scheduler 内实现 Recipe。约束：

- 机器关机错过时按 policy skip/run-once，不补跑无界历史；
- 同 window idempotency；
- 活动 Job/重叠 window 防重；
- 后台预算和网络 policy；
- 生成 Candidate 后通知，不自动 approve/publish；
- 用户可暂停/删除 schedule，不影响既有 Draft/知识。

## 13. CLI

```text
alltonote work-source add git --repo <repo-id> --project <project-id>
alltonote work-source add journal --root <grant-id> --pattern "**/*.md"
alltonote work-source list|doctor|remove

alltonote produce work-digest \
  --daily 2026-07-18 \
  --timezone Asia/Shanghai \
  --workspace <ref> \
  [--projects ...] [--profile balanced] [--json]

alltonote produce work-digest --weekly 2026-W29 ...
alltonote work-digest rescan --window <id>
```

Source 配置保存 opaque grant/connector refs，不保存云 token。

## 14. 隐私与安全

- 所有 connector 默认 opt-in；
- 每个 source 显示将读取的范围和例子；
- 不扫描用户 home/全盘；
- Git 只读固定 objects/worktree grant，不执行 hooks/scripts；
- Markdown 中 Prompt injection 只作内容；
- Secret/PII scan 在模型前和 Draft 后分层；
- 远端模型前显示/应用 privacy policy：local-only、metadata-only、content-allowed；
- 可对敏感项目强制本地模型或跳过；
- 日志不保存活动正文；
- 网站不接收个人 ledger；
- common publish 默认不可用；
- 参与者/邮件/客户信息需要最小化和用户审阅；
- 删除 connector 不删除原始 Git/日志，也不自动删除已生成知识。

## 15. 质量与用户信任

产品必须显示：

- 扫描了哪些 Source；
- 哪些 Source 失败/未授权；
- window/timezone；
- 多少事件去重/未分类；
- 模型是否使用、发送了哪类内容；
- Draft 与上次版本差异；
- 每个结论的 Evidence。

不显示“效率分”“工作热度”等容易误导的单一评分。统计只作导航，例如 commit/会议/日志事件数量，并附“数量不等于价值”。

## 16. 性能与成本

- Git/日志增量 cursor，避免每日全量扫描；
- 文件 hash/commit ID 去重；
- 先确定性过滤/归类，再向模型发送短 fact inputs；
- 不把完整 diff/会议 transcript 全量放进 compose；
- 每个 connector checkpoint；
- 同 window 无新事件返回 no-op，不调用模型；
- Weekly 尽量复用 ActivityEvent/fact map，不复用旧 Draft 作为唯一事实；
- 后台 Job 有每日模型预算和并发限制；
- 大 repo diff 只在用户选择下深入 Codebase Recipe。

## 17. 测试矩阵

### 17.1 时间与增量

- Asia/Shanghai 自然日；
- DST timezone；
- 午夜边界；
- late event/lookback；
- 多次运行去重；
- 已发布后补录；
- 机器关机错过 schedule；
- clock/timezone 变化。

### 17.2 Connector

- 多 Git repo/author；
- merge/revert/empty commit；
- dirty worktree 显式/拒绝；
- Markdown 外部编辑/重命名；
- 会议 transcript；
- connector partial failure；
- revoked grant；
- Unicode/path/reparse point。

### 17.3 质量/隐私

- planned 不被写成 completed；
- 同一工作多来源 relation；
- unassigned project；
- Secret/PII；
- Prompt injection；
- 远端模型 policy 拒绝；
- 无 Evidence claim；
- 无事件 no-op。

### 17.4 真实 E2E

1. 两个本地 repo + Markdown journal 的 daily digest；
2. 同 window 重跑零重复/无无谓模型调用；
3. late event 生成新 Draft diff；
4. partial connector failure 明确披露；
5. Draft -> Review -> personal publish；
6. Weekly 聚合并回溯原 event；
7. Engine schedule（后续）创建 Candidate 不自动发布；
8. Desktop 关闭/CLI-only；
9. privacy local-only；
10. 原 repo/日志前后 hash/状态不变。

## 18. 分期

### Phase PD0：Manual Git Daily MVP

单/多 repo、ActivityEvent、固定日窗、Evidence、Daily Draft。

### Phase PD1：Markdown/Meeting Source

增量日志、会议文件、项目归类、partial coverage。

### Phase PD2：Weekly/Project

跨天 dedup、决策演进、late event/amendment。

### Phase PD3：Engine Schedule

真实需求与 Engine 成熟后，按需唤醒、idempotent window、预算和通知。

### Phase PD4：受控云 Connector

逐个设计 OAuth scope、撤销、缓存和隐私；不一次接入所有服务。

## 19. 完成定义

1. 手动 CLI 可从授权 Git/Markdown 来源生成 Daily Digest；
2. 时间窗、时区、late event 和去重语义确定；
3. 每个成果/决定/风险有 ActivityEvent Evidence；
4. planned 与 completed 不混淆；
5. 无新事件 no-op，不调用模型；
6. 默认不监控、不常驻、不上传网站、不自动发布；
7. Source/Secret/PII 范围可见且可撤销；
8. 调度只在 Engine 后实现，仍只生成 Candidate；
9. 外部 Git/日志不被修改；
10. Daily/Weekly/Review/Publisher 真实 E2E 通过。
