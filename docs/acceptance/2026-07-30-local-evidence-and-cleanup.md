# 本地证据收敛与 Worktree 清理验收

> 日期：2026-07-30
> 状态：PASS；本记录进入 `master` 并推送后，允许按本文列出的精确目标执行清理
> 集成提交：`87df7661a88f3cfe6dd093800999571f7c55a4da`
> 备份：`G:/AllToNote-backups/2026-07-30-before-worktree-cleanup.bundle`
> Bundle SHA-256：`A90BFA22A2DEE17CECE02AA6CBE2D4C1842F009B6AF5555F2FFA5D66C2ACF10F`

## 1. 结论

X0-A 已在外置工作树 `G:/AllToNote-video-producer` 原地完成、通过 Gate、拆分为产品与文档两个提交，并快进合并到 `master`。因此不再把该工作树迁移到 `G:/AllToNote` 下：迁移只会保留一份已完成工作的重复 checkout，并使路径绑定的 Python venv 和 Orca 记录失效。正确收口方式是先完成远端与 bundle 保护，再通过 Git 移除该工作树。

五个 `.claude/worktrees/agent-*` 分支均位于 `bbe1ef20b186ddf70dfa2cf70c7995c848e93dbf`，没有独有提交。其 3 份完全重复文档和 8 份变体草稿的逐项语义决议已记录在 `2026-07-30-stale-worktree-document-consolidation.md`。本报告进一步收敛散落的 G0 分析、iWiki review diff、YouTube 验收运行和可再生缓存。

## 2. 远端与可恢复性 Gate

清理前已满足：

- `origin/master`：`87df7661a88f3cfe6dd093800999571f7c55a4da`；
- `origin/codex/alltonote-x0a`：`87df7661a88f3cfe6dd093800999571f7c55a4da`；
- `origin/codex/iwiki-readonly-client`：`21f71a729390709e3684884d3a86e389e565e33d`；
- `git bundle verify`：完整历史、SHA-1 object format、30 refs，验证通过；
- bundle 同时保存清理前的 `master`、X0-A、iWiki、Wave 0、旧 Video 分支和五个 Agent 分支。

## 3. 主工作树中间状态文档

主工作树曾有三份未提交的 Task 5 阶段状态文档。它们的有效内容（尤其 Wave 0 错误 SHA 更正）已被 X0-A 最终文档吸收，而任务状态已由 Tasks 1–8 PASS 取代。恢复 tracked 文件前记录的 blob 为：

| 文件 | 中间快照 blob | 处理 |
|---|---|---|
| `docs/README.md` | `06e144887609ecbab04b9f32d90c0f0b1b7d79cf` | 由最终 README 的 X0-A PASS 与同一 SHA 更正取代 |
| `docs/tasks/alltonote-design-coverage-matrix.md` | `ef5222da504e664fa268ec58b06785e6e035e8d3` | Task 5 中间状态由 Tasks 1–8 PASS 取代 |
| `docs/tasks/alltonote-master-tasks.md` | `a696de145e06aad5f51a888b70d4450bf4c27183` | Task 5 中间执行序列由 C0 下一阶段取代 |

## 4. G0 分析与任务报告

这些本地分析的结论已经进入 Wave 0 PASS、X0-A Tasks 1–8 acceptance、handoff、README、coverage matrix 和 master tasks。文件本身不是产品输入，也不再是唯一证据；以下 SHA-256 清单保留其来源身份，推送本报告后可删除原文件。

| 文件 | SHA-256 |
|---|---|
| `.claude-codex-g0-analysis.txt` | `ADB603CC34054D453E4EF3B0E454C179F29ED51CA99A2D64F4C144523D012272` |
| `.claude-gemini-g0-analysis.txt` | `FF29F7C500B62E3F958C6BB2843A00BFBBB9FB50D08B0D3BEAD543FB6F01FB63` |
| `.superpowers/sdd/alltonote-task1-report.md` | `E7BFFEB58FD7F9B01FE912DC09F90009F3A630AAE5F2FB9FC5D4C29051E04DF6` |
| `.superpowers/sdd/alltonote-task2-report.md` | `6B11D98BF377A9815B767B2C881C7AAD1B928FFC4F11E91BDE7E635008B71A47` |
| `.superpowers/sdd/alltonote-task3-report.md` | `96D395CE5107068CF3556B2817A2D3AE6BA8D9986A4E1DEE329588176A707224` |
| `.superpowers/sdd/alltonote-task4-report.md` | `FCC787ACFEE448B857D525BE4A00A5B2D606B207409B5DF7BBB093349FA09178` |
| `.superpowers/sdd/alltonote-final-fix-report.md` | `F6AD443466B7B233196C1930B8746B4BCC50ED142B51075270501EA6C5D9B517` |
| `.superpowers/sdd/alltonote-final-gate-report.md` | `D31E18AD46565AF6E1668BE21C3174B41B04D3960DE7A44ABA69394E3914DFAF` |

## 5. iWiki Review Diff 包

iWiki 分支的 9 个独有提交已推送并写入 bundle，因此这些可由 Git 提交区间重建的 review diff 不再承担唯一备份职责。

| 文件 | SHA-256 |
|---|---|
| `review-alltonote-final-58fc206..21f71a7.diff` | `8D00F51512EC4D3312B83FB2DCA9327C3F54434F22539D71539D0A397FA0CE47` |
| `review-alltonote-final-58fc206..8bbd128.diff` | `7CEB0D3A2E019FB3AE0CF6E448AA4C5AEBE2C59AF18A40E8EB0381CD279EC61D` |
| `review-alltonote-final-58fc206..e1250c2.diff` | `D38874BA902EFFB090D873842B375B1F54046EE55EAF1E4C51354B7FCB8D9308` |
| `review-alltonote-task1-58fc206..5d86052.diff` | `D99C722EC2DF5F4136446511CFBDAAD46EED3F7CA97162BAF5855D6D669AC92E` |
| `review-alltonote-task1-58fc206..c9f0e5d.diff` | `28F8BD41477182835502182D3DC10152756EBF12E7FBAC4E76E0C385F6CF1AB8` |
| `review-alltonote-task2-5d86052..90adc4f.diff` | `8067B5F4AB9AC4ABAA6A074A2F48255773E8B46E7D696419B00BD48A476EF9CB` |
| `review-alltonote-task2-5d86052..a380e77.diff` | `9EBB69515984840037E04E651FF9510780457E829D084E90DFE79F1E6123D1D3` |
| `review-alltonote-task3-a380e77..5050885.diff` | `D884FBD4E6B1751AEC85004F56B04C74DFFC7F6EB7571777029CA62129F50B94` |
| `review-alltonote-task4-5050885..5b3fdec.diff` | `4E22D189FC582B156C62FDCDA1E06D0BE3A5C474EA95260463118958FD410473` |
| `review-alltonote-task4-5050885..8bbd128.diff` | `EF63B92BAFA724A76C6136C6B838ED83059B497D7E662703D30648E45287A8BB` |

## 6. YouTube V2 验收运行

九个运行目录中，前三个没有形成 `acceptance-report.json`；其余六个报告的演进如下：

| 运行 | 报告 SHA-256 | 结果 |
|---|---|---|
| `20260716-183803` | 无报告 | 中断的早期运行 |
| `20260716-190749` | 无报告 | 中断的早期运行 |
| `20260716-191941` | 无报告 | 中断的早期运行 |
| `20260716-194049` | `D6A3C929BBD3C3B935B09EE70ED3AF7B784EBA7FD234F925573D3572B0404A13` | `knowledge_map_response_invalid` |
| `20260716-194538` | `924FC396AAA6C5F204EF8DB7D869D9796655CD59B3C66A42F6885613A2D6D6DF` | `video_bundle_input_invalid` |
| `20260716-200227` | `F58686134E6365D0C5F2DAB8966AF352B4ACAE0C233A408314EBF5BE4BF2ECAD` | `knowledge_note_quality_failed` |
| `20260716-202024` | `D7963F640B942C7640B1C5683DC4FBED77BAB793001860C659B46743A2305ED1` | `model_citation_definition_forbidden` |
| `20260716-203126` | `B8925A41056DAB0042A70A21B90C2436B88DEB38C5ED9A964431C7FE512382EA` | Job succeeded；`quality_overall=fail`；`publish_eligible=false` |
| `20260716-204525` | `CFE89AA4BE39367611EDC46AE29AB41C7D09760E092C32D74F4245BDAD4A2A8E` | Job succeeded；`quality_overall=pass`；`publish_eligible=true`；`acceptance_passed=true` |

最终通过报告还确认：84 个 citation definition、87 次 citation use、citation IDs closed、9 个 H2、无重复 H2、8 次 model call、0 次 repair。这里保存验收结论和原报告哈希；历史运行目录属于完成后可删除的本地证据副本。

## 7. 可再生缓存与空间

清理前主要可再生目录：

| 目录 | 字节 | 决议 |
|---|---:|---|
| `G:/AllToNote/BillNote_frontend/node_modules` | 760,956,575 | 删除；可由 pnpm 重建 |
| `G:/.worktrees/AllToNote/iwiki-readonly-client/BillNote_frontend/node_modules` | 637,859,697 | 删除；可由 pnpm 重建 |
| `G:/AllToNote-video-producer/.venv` | 864,282,032 | 随已完成 worktree 移除；可由 requirements 重建 |
| `G:/AllToNote-video-producer/.superpowers/sdd/tools` | 1,006,532,978 | 随已完成 worktree 移除；生成/下载工具缓存 |
| `G:/AllToNote-video-producer/.superpowers/sdd/model-cache` | 78,203,659 | 随已完成 worktree 移除；可重新下载 |
| `G:/AllToNote-video-producer/.superpowers/codex-app-server-schema-01441` | 3,159,797 | 随已完成 worktree 移除；生成 schema |

另外清除三个工作树中的 `__pycache__`、`.pytest_cache`。主工作树约 148 MB 的 Whisper 模型缓存不在本轮删除范围，避免无必要的重新下载。

## 8. 明确保留

以下内容不是缓存或旧文档，不得删除：

- `G:/AllToNote/AGENTS.md`；
- `.env` 与任何凭据；
- `backend/app/db/bili_note.db` 及其他用户数据库；
- 主工作树的 `config/`、backend 配置和日志；
- 所有已跟踪 acceptance、design、ADR、task、handoff 文档；
- `codex/iwiki-readonly-client` 远端分支，直到独立集成决策完成；
- 经验证的 Git bundle。

## 9. 执行顺序

1. 提交并推送本报告；
2. 精确删除第 4、5 节列出的本地散落证据；
3. 通过 Git 移除五个旧 Agent worktree，再删除其零独有本地分支；
4. 通过 Git 移除已合并的外置 Video worktree；
5. 删除已合并且有远端/bundle 保护的旧本地分支；
6. 清理精确列出的可再生缓存；
7. 执行 `git fsck`、worktree/branch/remote/status 终审。
