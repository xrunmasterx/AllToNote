# Video 三样本 Pilot 技术结果报告

```yaml
doc_type: acceptance-report
status: pilot-reliability-gap-user-value-3-of-3-after-terminology-fix
authority: stage-evidence
upstream:
  - spec.md
  - tasks.md
  - samples.md
last_verified_at: 2026-07-30
```

## 1. 判定

本轮不是“首次系统 `3/3`”。V01 首次正式运行失败，V02、V03 首次成功；在两轮允许的最小修复后，三个冻结输入在全新隔离 Workspace 中原样顺序重跑，达到修复后系统 `3/3`。

因此当前判定为：

- 系统技术 Gate：**修复后 PASS（3/3）**；
- 可靠性分类：**reliability gap**，因为首次只有 `2/3`；
- 用户价值 Gate：**修正后 PASS（3/3）**；首次阅读判定 V01、V02 通过、V03 不通过，术语修正后用户重新判定 V03 通过；
- 产品/发布结论：**尚未成立**，不得外推为 10 样本、平台字幕、本地文件、长视频、干净安装、多用户或公开发布已经验证。

## 2. 固定执行环境

| 项目 | 冻结值 |
|---|---|
| 工作树 | `G:\.worktrees\AllToNote\video-dogfood-validation` |
| 分支 | `codex/video-dogfood-validation` |
| Python | 3.11.15，隔离 `.venv` |
| CLI | `alltonote` Runtime 0.1.0 / CLI protocol 1 |
| LLM | Codex app-server / `gpt-5.6-sol` |
| 转写 | `faster-whisper/small-cpu-int8` |
| FFmpeg | 动态 doctor PASS |
| Workspace | `.venv/pilot-workspace-final`，`workspace_id=alltonote-video-dogfood-pilot-final` |
| 调度 | V01 → V02 → V03，任一时刻只有一个真实 Job |

动态 doctor 总体 healthy；唯一 warning 是未使用的 Groq 可选能力。三条视频的匿名平台字幕列表和正式 Runtime 字幕发现均为空，最终全部走媒体下载与本地 ASR 回退。未配置 Bilibili Cookie；yt-dlp 使用可匿名取得的音频格式。

## 3. 首次失败与两轮修复

### 修复循环 1：无字幕媒体回退

V01 首次 Job `job_019fb25f-56c1-7498-9397-cf89f07beed9` 以 `source_metadata_invalid` 失败。根因有两个直接部分：平台 Runtime 只有字幕路径，没有在字幕不存在时下载媒体并调用本地转写；Bilibili yt-dlp patch 的 `_download_playinfo` 包装器不能接收新版调用传入的 `fatal` 关键字。

修复先增加失败测试，再补齐：

- 无字幕时二次 acquisition 只请求媒体；
- 媒体哈希后进入 attempt storage，再由固定转写器读取；
- Bilibili patch 透传额外关键字参数；
- 提供转录仍优先于平台字幕和媒体回退。

### 修复循环 2：Portable 审计身份

诊断成功 Bundle 的正文和质量报告有效，但回执把真实 Whisper 错记为 `fake/transcriber-v1`，V01 retry 的 `parameters.summary.retry_of_job_id` 也为 `null`。

修复同样先增加失败测试，再把冻结的生成式转写器身份注入 VideoService，并在 retry Bundle 组装时投影仓库已有的父 Job ID。没有使用 `source.extensions`，因为 Portable 安全 Gate 明确拒绝所有非空 source extensions。没有修改 Prompt、Recipe、正文生成或逐视频参数。

受影响回归 85/85 通过；完整 backend test suite 为 1908 passed、2 skipped、3 subtests passed，耗时 100.26 秒。三条 warning 来自既有 Douyin/Kuaishou 正则转义弃用提示与 ctranslate2 对 `pkg_resources` 的弃用提示，本轮未扩大范围处理。

## 4. 最终三个 Bundle

| ID | Job / Bundle | 总耗时 / 转写 | 内容与证据 | 自动质量 |
|---|---|---:|---|---|
| V01 | `job_019fb286-e6b7-7988-b5ff-4348b74b2fd4` / `bnd_019fb286-e6b7-7be1-8128-9c6ea054c306` | 425.2 / 198.019 秒 | 327 源片段；327 cited；327 coverage inputs；48,168 bytes | PASS；13 项检查；repair 0；model calls 1 |
| V02 | `job_019fb28d-c1fb-7f85-b75e-bff86fb62450` / `bnd_019fb28d-c1fb-721e-b869-f7dae723f3ca` | 282.1 / 120.263 秒 | 184 源片段；182 cited；184 coverage inputs；28,581 bytes | PASS；13 项检查；repair 0；model calls 1 |
| V03 | `job_019fb292-70b5-74d0-b78f-53792a5e5198` / `bnd_019fb292-70b5-7971-b15a-76c9774ce8ef` | 460.6 / 222.895 秒 | 359 源片段；61 cited；20 个编译 coverage units；15,659 bytes | PASS；13 项检查；repair 0；model calls 3 |

三个 Bundle 的 Workspace validate 和 Portable semantic validate 均为 valid、0 issues。最终工作区恰好包含这三个 committed Bundle；三份回执的 transcriber 都是 `faster-whisper/small-cpu-int8`，model 都是 `gpt-5.6-sol`，最终首跑 Job 的 retry parent 均正确为空。

草稿哈希：

- V01：`sha256:c14a54ecc74bf5531299244c8890677022fe335eab6d4d5ec598b6a88d9c7970`；
- V02：`sha256:510886057d11f9d2bed95b0902cea71d3774e159eecb3a228f9f9c3edafb2a74`；
- V03：`sha256:c15ed9326954a66653c1774c2c148f1183c16fd13041858b6b99e02a090ece62`。

V03 的 20 个 coverage inputs 是长输入经知识映射形成的编译单元，不代表只有 20/359 个原始片段被处理；因此不能把该数字直接解释为原始片段覆盖率。

## 5. 内容抽查与人审风险

三份正文都具有清晰标题、层级结构、来源限定和 Evidence 脚注；对于视频口述价格、榜单、效率或产品能力，正文通常能标明“来自演示陈述、未独立核验”。自动 Gate 主要证明 Markdown 安全、引用完整、heading 结构、coverage ledger 和 Portable 一致性。

自动 Gate 不证明专有名词正确。抽查发现：

- V01：`Yuzha`、`Fiber5`、`GPT 5.6` 等疑似 ASR 错误；正文已把部分名称和指标标为未核验；
- V02：`DBC`、`OpenCrawl`、`OpenCrew`、`Cloud Code` 等疑似名称错误；虽明确写成“被转写为”，仍影响成品感；
- V03：`Cloud Code`、`Omapad`、`Growk`、`Asus` 等疑似名称错误；正文对 `Asus` 等保留不确定性边界；
- 引用密度较高：V01/V02/V03 分别有 327/182/61 个脚注定义。用户实际审阅确认，正文连续编号和文末时间段列表会遮蔽主要内容，默认呈现不能接受。

ASR 专有名词问题不能在 Pilot 结果生成后手工修正文，否则会污染对产品原始输出的测量。Evidence 呈现则是三个样本共同暴露的确定性产品缺陷，应在展示边界统一修复，而不是让用户逐篇删除。

## 6. Evidence 呈现修正与回放

第一性原理边界是：Evidence 的目标是让系统和审阅者在需要时验证结论，不是让所有内部定位信息永久占据默认阅读流。既有 Portable/Video 规范又明确要求 canonical Draft 使用 Evidence ID 完成引用完整性校验，因此本轮没有删除审计证据，而是分离两个表示：

- `reading`：`draft show` 默认表示；隐藏可见的系统 Evidence 引用和文末定义，供阅读、复制和后续用户评分；
- `audit`：`draft show --presentation audit`；返回 committed canonical Markdown，保留完整引用以供核验；
- 两者共享同一 Draft、EvidenceSet 与 Quality Report；投影为只读纯函数，不创建第二事实源。

真实 Bundle 回放结果：

| 样本 | canonical Evidence ID | 审计稿 Evidence 标记 | 阅读稿 Evidence 标记 | 审计稿 / 阅读稿 bytes | canonical SHA-256 |
|---|---:|---:|---:|---:|---|
| V01 | 327 | 654 | 0 | 48,168 / 11,246 | `c14a54ecc74b...` |
| V02 | 182 | 364 | 0 | 28,581 / 8,036 | `510886057d11...` |
| V03 | 61 | 122 | 0 | 15,659 / 8,788 | `c15ed9326954...` |

三份阅读稿的 `Video 时间段` 定义均为 0，均未截断；回放未调用转录或模型，且前后 Workspace 文件大小、mtime 与 SHA-256 快照一致。聚焦测试为 `19 passed`，完整 backend 回归为 `1913 passed, 2 skipped, 1 warning, 3 subtests passed`。

这次修正不声称完成了逐结论的可视化 Evidence 抽屉、点击跳转播放器或 GUI 集成；这些只有在用户确认干净阅读版有价值后才值得进入后续设计。

## 7. 用户价值 Gate

### 7.1 客观复审结果

在不改写 Pilot 原始输出的前提下，已逐篇审读三份 `reading` Markdown，并用产品官方资料核对能够确定的专有名词。该复审用于估算编辑负担，不替代用户的保留意愿。

| 样本 | 结构与信息价值 | 明显专有名词风险 | 预计编辑负担 | 代理建议 |
|---|---|---|---:|---|
| V01 | 实验设计、样本外测试、压力测试与风险边界完整；数字声明通常带来源限定 | `Fiber5`、`GPT 5.6` 共 2 处无法仅凭现有材料确定 | 约 3–6 分钟 | 倾向保留；`≤10 分钟` Gate 大概率可通过 |
| V02 | Agent 组成与执行循环清楚，但对 4 分 37 秒视频而言略显重复 | `DBC`、`OpenCrawl`、`OpenCrew`、`Cloud Code` 共 7 处；上下文高概率分别指向 `DeepSeek`、`OpenClaw`、`Claude Code` | 约 6–10 分钟 | 临界保留；需要用户确认是否接受篇幅和名称修订 |
| V03 | Worktree、Panel、恢复、移动端、Issue 与自动化覆盖完整；当前 Orca 官方资料能支持大部分功能框架 | `Cloud Code`、`Omapad`、`Growk`、`Asus`、`隐形`、`GitLab` 共 15 处；其中前四类高概率是 `Claude Code`、`Amp`、`Grok`、`Issues`，其余仍需回听 | 约 10–15 分钟 | 按原 `≤10 分钟` Oracle 倾向不通过，除非用户认为这些名称不影响保留 |

核验边界：Orca 当前官方文档明确列出 Claude Code、Codex、Grok、OpenCode、Amp 等 Agent，并确认 Worktree 隔离、标签页/分屏、会话恢复、GitHub/GitLab Issues、移动端和定时自动化等能力；OpenClaw 官方文档确认其正式名称和 Agent 定位。这些资料可以高置信识别部分 ASR 错词，但不能证明视频录制当时的每个 UI 文案，因此未直接改写 Pilot 产物。

三个样本还有一个共同的阅读边界：干净 Markdown 中没有 compact source attribution 或一键打开原视频的入口。canonical Bundle 仍保有 Source/Evidence，但普通读者需要切换到 `audit` 才能核验。这不影响本轮 Evidence 降噪修正成立，却是是否进入下一阶段“按需 Evidence/来源入口”设计的真实输入。

### 7.2 术语修正前的用户判定

用户对默认 `reading` Markdown 的最终判定为：

| 样本 | 用户判定 | 计入价值 Gate | 首要结论 |
|---|---|---|---|
| V01 | 通过 | PASS | 愿意保留 |
| V02 | 通过 | PASS | 愿意保留；名称问题仍应修正 |
| V03 | 不通过 | FAIL | 专有名词污染使编辑负担不可接受 |

该次用户价值为 `2/3`，未达到 `3/3` Pilot Gate。用户显式选择下一步先修专有名词识别；术语修正必须是跨样本机制，不得为 V02/V03 编写逐视频替换表，也不得改写本报告记录的原始 Pilot 结果。

## 8. 专有名词修正复验

原 Pilot 与用户 `2/3` 判定保持不变。后续修正没有回写 Transcript 或旧 Bundle，而是在新隔离 Workspace `.venv/pilot-workspace-terminology-v2` 使用同一 `faster-whisper/small-cpu-int8`、`gpt-5.6-sol` 和冻结输入重跑 V02、V03。

第一次保守提示实验在 V02 自动质量 PASS，但仍保留全部错误名并标注“不确定”，客观术语 Gate FAIL，因此没有继续运行 V03。第二版把模型已有公共名称知识的权限严格限定为 spelling：单一匹配清楚时必须 canonicalize，多候选时才保留不确定性；仍不联网、不使用逐视频词表，也不新增模型阶段。

| 样本 | Job / Bundle | 拓扑与调用 | canonical 名称 | 旧污染词 | 结果 |
|---|---|---|---|---|---|
| V02 | `job_019fb321-0732-77a7-865d-b4c68b4fdc4b` / `bnd_019fb321-0732-7e08-82f6-6a7130daba25` | DIRECT；model calls 1；repair 0 | `DeepSeek=1`、`OpenClaw=3`、`Claude Code=2` | `DBC/OpenCrawl/OpenCrew/Cloud Code=0` | 客观术语 PASS |
| V03 | `job_019fb327-2cd4-7297-a652-eb10077991b5` / `bnd_019fb327-2cd4-7407-aef1-1b8b35e2bdad` | MAP_COMPOSE；model calls 3；waves 2；repair 0 | `Claude Code=9`、`Amp=1`、`Grok=1`、`Issues=1` | `Cloud Code/Omapad/Growk/Asus=0` | 客观术语 PASS |

V03 的 `隐形` 未进入 Draft，未被硬猜成其他名称；`GitLab` 保留 1 次，语境为演示中的仓库克隆来源，不具备明确错误证据。V03 仍保留 Worktree、历史恢复、分屏、多 Agent、Mobile、Voice、Issue 和 Automation 等主体内容，因此旧词归零不是删段造成的。

两份 Bundle 的 Portable semantic validate 均 valid、0 issues，Workspace validate valid；完整 backend 回归为 `1913 passed, 2 skipped, 1 warning, 3 subtests passed`。

### 8.1 用户重新评分

用户阅读新的 V03 reading 后明确回复“V03 通过”。因此修正后的最终用户判定为：

| 样本 | 修正后用户判定 | 计入价值 Gate |
|---|---|---|
| V01 | 通过 | PASS |
| V02 | 通过 | PASS |
| V03 | 通过 | PASS |

修正后用户价值 Gate 为 `3/3`。首次系统只有 `2/3`、首次 V03 用户判定 FAIL 的事实继续保留，所以可靠性分类仍为 `reliability gap`；当前结果也不能外推为 10 样本、平台字幕、本地文件、长视频、干净安装、多用户或公开发布已经验证。是否进入 Video 可信复用或其他阶段，仍必须由用户另行显式决定。
