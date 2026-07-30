# Video 可信复用验证规格

```yaml
id: VAL-VIDEO-REUSE-001
title: Video 可信复用验证
status: active
owner: product-validation
upstream:
  - VAL-VIDEO-001
downstream:
  - REC-DOC-001
  - REC-CONTRACT-001
implementation_status: observation-ready
last_verified_at: 2026-07-30
```

## 1. 要回答的问题

`VAL-VIDEO-001` 已证明三个冻结输入最终都能生成用户认可的阅读结果，但一次性认可不能证明产品价值。当前只回答一个更小的问题：**用户是否会保留这些结果，并在不重看视频的情况下再次检索或用于真实任务。**

本阶段验证行为，不验证更多模型能力。不能把被要求再次阅读、增加新样本或再跑一次模型当作可信复用证据。

## 2. 第一性原理边界

可信复用至少需要同时成立：

1. 结果在观察期内仍可找到、打开且内容未漂移；
2. 间隔一段时间后，用户能只依靠笔记找回关键知识；
3. 至少一份笔记进入一个真实下游任务，而不是只为通过测试而阅读；
4. 使用时没有出现超过原 Pilot `10 分钟` 上限的阻塞性人工清洗。

“阅读起来不错”“愿意保留”和“按要求回答测试题”都是弱证据；真正的产品拉力来自用户自然返回并使用结果。

## 3. 冻结材料

只使用 `VAL-VIDEO-001` 的三个最终 reading 投影，不增加视频、不重跑转写或模型、不改写 canonical Transcript/Draft/Bundle。

| 样本 | 冻结 reading | Bytes | SHA-256 | canonical 证据 |
|---|---|---:|---|---|
| V01 | `.venv/pilot-reading/V01-kimi-freqtrade-reading.md` | 11,246 | `70849e4b170c5e86b1d06ff44ef6aa7890341698c86609e638d7e3396bc4b0eb` | `bnd_019fb286-e6b7-7be1-8128-9c6ea054c306` / `art_019fb286-e6b7-78cb-ba11-cd734dc54eac` |
| V02 | `.venv/pilot-reading-v2/V02-agent-terminology-reading.md` | 6,647 | `c64344bb76bb7cb5bcf9b438ba18f991717ed38ad8eba922c1d3c55ab4a91628` | `bnd_019fb321-0732-7e08-82f6-6a7130daba25` / `art_019fb321-0732-7e7a-9604-eb7ed4d57030` |
| V03 | `.venv/pilot-reading-v2/V03-orca-ade-terminology-reading.md` | 8,021 | `e838da464af523a61aca92b012c0359f8b813b2f8f3fbb77310eed38a1871b42` | `bnd_019fb327-2cd4-7407-aef1-1b8b35e2bdad` / `art_019fb327-2cd4-7faf-9f68-389f85823fc2` |

`.venv` 中的 reading 只是本地观察材料，不进入 Git；Bundle/Draft ID 与哈希负责把观察结果绑定到已验收版本。若本地材料丢失，只能从相应 canonical Draft 重新执行确定性的 `reading` 投影，不得重新调用模型。

## 4. 时间窗与观察方法

- 开始：2026-07-30；
- 最早结束：2026-08-06，至少跨越 7 个自然日；
- 前 72 小时不进行计划性检索测试；自然使用可以随时记录；
- 第 4–7 天完成一次延迟检索；
- 整个窗口持续记录自然复用，不为了制造 PASS 而安排虚假下游任务。

延迟检索固定为：

| 样本 | 固定检索问题 | 通过条件 |
|---|---|---|
| V01 | 作者如何区分策略是否只是历史过拟合，结论边界是什么？ | 只查 reading，在 3 分钟内定位到方法与边界，不重看视频 |
| V02 | Agent 的最小组成和执行循环是什么？ | 只查 reading，在 3 分钟内定位到组成和循环，不重看视频 |
| V03 | Orca ADE 如何用多 Agent/worktree 隔离工作，失败后如何恢复？ | 只查 reading，在 3 分钟内定位到隔离与恢复，不重看视频 |

自然复用事件必须来自本来就要完成的真实任务，并记录触发原因、使用了哪份笔记、是否改变了决策或减少了重看视频的需要。仅打开文件、重复评分或执行上述固定检索不算自然复用。

## 5. 冻结 Gate

### PASS

必须全部满足：

1. 观察结束时三份 reading 哈希仍匹配，且均愿意继续保留；
2. 三个延迟检索均在 3 分钟内完成，且没有重看视频；
3. 至少记录 1 次自然下游复用，并能说明它支持了什么真实任务；
4. 任一使用事件的必要人工清洗均不超过 10 分钟；
5. 没有事实错误严重到阻止使用。

### NO-SIGNAL

三份延迟检索均通过，但 7 天内没有自然复用。该结果不等于技术失败，也不能支持继续扩张；默认停止新实现，保留结果并由用户决定延长观察或冻结项目。

### FAIL

任一情况成立即 FAIL：

- reading 丢失或无法从对应 canonical Draft 确定性恢复；
- 任一固定检索无法在 3 分钟内完成或必须重看视频；
- 真实使用暴露事实错误、术语污染或结构问题，且需要超过 10 分钟清洗；
- 为通过 Gate 必须增加新模型阶段、逐视频词表、第二条 Pipeline 或大规模产品实现。

观察期间发现问题只记录，不边测边改。结束后最多选择一个首要根因进入新的显式修复任务，原日志和结论不回写。

## 6. Gate 后路线

- `PASS`：允许选择一个真实 born-digital PDF 建立最小纵切，由 Video 与 PDF 共同驱动 X0-B；仍不自动启动完整 C0、Review/Publisher、Engine、Desktop 或公共插件 SDK。
- `NO-SIGNAL`：停止架构扩张；由用户决定是否延长自然观察。
- `FAIL`：停止第二 Recipe；只报告首要复用障碍，等待用户决定修复或结束。

## 7. 非目标

- 新增第 4 个 Video 输入；
- 重新调用 ASR 或 LLM；
- 长视频、平台字幕、本地文件或 clean-machine 发布验证；
- Vault、搜索 UI、Desktop、Publisher、MCP、多 Job、C0 或 Engine 实现；
- 用自动化指标替代用户的真实使用行为。
