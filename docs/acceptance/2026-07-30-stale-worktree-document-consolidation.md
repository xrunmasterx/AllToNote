# 旧 Agent Worktree 文档收敛记录

> 日期：2026-07-30
> 状态：PASS；允许在本记录进入受保护提交后移除五个零独有提交的旧 Agent worktree
> 范围：`G:/AllToNote/.claude/worktrees/agent-*`

## 1. 方法与结论

逐一比较五个旧 Agent worktree 的全部未跟踪文件与当前 `codex/alltonote-x0a` 权威文档。所有 worktree 分支均停在 `origin/master` 的 `bbe1ef20b186ddf70dfa2cf70c7995c848e93dbf`，相对其他本地分支无独有提交。

结论：三份文件与当前受跟踪文档 blob 完全相同；其余八份是 2026-07-20/22 的中间草稿。八份草稿中的差异要么已被当前文档吸收，要么已被后续架构决策显式取代，没有需要复制回权威文档的独有有效要求。不得按时间戳覆盖当前文档；本记录保存其来源、blob 和语义决议。

## 2. 完全相同的副本

| Agent worktree | 文件 | Git blob | 结论 |
|---|---|---|---|
| `agent-a0d5591f48bb09b8a` | `docs/superpowers/plans/2026-07-18-alltonote-document-recipe-implementation-plan.md` | `453bcef` | 与当前受跟踪文件相同，可删除副本 |
| `agent-a8d0297abb2a3b435` | `docs/superpowers/plans/2026-07-18-alltonote-engine-production-mcp-implementation-plan.md` | `c272990` | 与当前受跟踪文件相同，可删除副本 |
| `agent-a8d0297abb2a3b435` | `docs/superpowers/specs/2026-07-18-alltonote-engine-production-mcp-design.md` | `b772b5e` | 与当前受跟踪文件相同，可删除副本 |

## 3. 非相同草稿的语义决议

| Agent worktree | 文件 | 草稿 blob | 当前 blob（比较时） | 决议 |
|---|---|---:|---:|---|
| `agent-a0d5591f48bb09b8a` | `docs/superpowers/plans/2026-07-18-alltonote-recipe-extension-contract-implementation-plan.md` | `70044ee` | `631f40c` | 当前版补充“下位执行文档不能覆盖上位架构”，并把 RX-04/RX-07 明确延后；语义更严格，草稿已被包含/取代 |
| `agent-a0d5591f48bb09b8a` | `docs/superpowers/specs/2026-07-18-alltonote-document-recipe-design.md` | `10cd446` | `5c14ba2` | 当前版删除对独立 `add/run` 的预承诺，只允许 generic `produce` 和经 Gate 的同路由别名；这是后续明确决策，草稿被取代 |
| `agent-acda2896c2120426c` | `docs/superpowers/specs/2026-07-13-alltonote-knowledge-compiler-architecture-design.md` | `5f8a278` | `06f9846` | 当前版分离目标架构与 X0-A 过渡接缝，并用 per-Job `JobExecutionAuthority generation` 取代把 scheduler lease 当作写权限；草稿被更安全的并发模型取代 |
| `agent-acda2896c2120426c` | `docs/superpowers/specs/2026-07-18-alltonote-recipe-extension-contract-design.md` | `b207720` | `0f11c54` | 当前版将 descriptor/InputDescriptor 缩为 X0-A 已证明字段，并增加 `describe`；草稿的大而全字段留作 X0-B/Pack 候选，不复制回 X0-A |
| `agent-acda2896c2120426c` | `docs/superpowers/specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md` | `196d9e7` | `4a13d4a` | 当前版采用 Wave 1A 已验收 Automation Protocol v1 与现有退出码，取代草稿的第二套 `api_version`/exit-code 设计 |
| `agent-ac832ef0eea41dab8` | `docs/README.md` | `37fc8ad` | 当前文件后续演进 | 草稿停在 Wave 0 partial；当前版已记录 Wave 0 与 X0-A PASS、真实 SHA 更正及下一阶段 C0，草稿过期 |
| `agent-ac832ef0eea41dab8` | `docs/tasks/alltonote-design-coverage-matrix.md` | `dd58a32` | 当前文件后续演进 | 草稿把 KMCP/Web/Personal 误画成 Engine 硬依赖并停在 Wave 0 partial；当前版区分实线硬依赖和虚线非阻塞关系，草稿被取代 |
| `agent-ac832ef0eea41dab8` | `docs/tasks/alltonote-master-tasks.md` | `d29371d` | 当前文件后续演进 | 草稿停在 Wave 0/X0-A pending；当前版保留发布缺口并更新到 X0-A Tasks 1–8 PASS，草稿过期 |

## 4. 本地配置

`agent-a11264323d304fdcd/config/downloader.json` 是 `{}`，与主工作树和 Video worktree 的本地副本内容相同，但配置不按文档重复项处理。删除旧 Agent worktree 时只删除该旧副本；主工作树和 Video worktree 中的 `.env`、数据库、配置、日志均保持不动。

## 5. 清理 Gate

只有同时满足以下条件才移除旧 worktree 与分支：

1. 本记录和 X0-A Task 8 验收已提交并完成远端或 Git bundle 备份；
2. 再次确认五个 `worktree-agent-*` 分支无独有提交；
3. 使用 `git worktree remove`/Orca worktree 命令移除注册，不能先删目录；
4. 移除后执行 `git worktree list`、`git branch --contains` 与工作树状态审计；
5. 不删除 `.env`、SQLite、主/Video 配置、日志、正式 acceptance/design/ADR 或最新通过的验收资产。
