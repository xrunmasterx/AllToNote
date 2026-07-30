# Video 三样本 Pilot 冻结登记

```yaml
doc_type: acceptance-input
status: frozen
authority: stage-input
last_verified_at: 2026-07-30
```

## 1. 冻结输入

| ID | BVID | 标题 | 作者 | 时长 | Canonical URL |
|---|---|---|---|---:|---|
| `V01` | `BV1Np3j6QEQc` | 【深度测评】用 Kimi K3 + Freqtrade 写量化交易策略 | frank-quant | 535 秒（08:55） | <https://www.bilibili.com/video/BV1Np3j6QEQc/> |
| `V02` | `BV1jbXKBGECC` | 从零实现自己的agent第一期：什么是agent | 小单说AI | 277 秒（04:37） | <https://www.bilibili.com/video/BV1jbXKBGECC/> |
| `V03` | `BV1jDKH6gE3b` | Orca ADE彻底改变AI编程方式：多Agent并行、语音输入、定时审查、Git Worktree隔离与结构化编排 | AI超元域 | 692 秒（11:32） | <https://www.bilibili.com/video/BV1jDKH6gE3b/> |

用户明确指定这三条就是本轮最终输入，不再增加独立校准集或额外正式样本。测试开始后不得替换失败样本。

## 2. 预检证据

2026-07-30 使用 Bilibili 官方公开接口读取每个 BVID 的 view metadata 与首个 CID，再查询 player/v2 字幕列表：

- 三个 view 请求均返回 `code=0`；
- 标题与用户提供内容一致；
- 每个视频均为单 P；
- 三个匿名 player/v2 请求均返回 `code=0`，字幕列表为空；
- 该结果只证明匿名预检未发现字幕，不排除正式 Runtime 使用受控 Cookie 后发现可靠字幕；
- 未下载媒体、字幕正文或模型输入；
- 用户 URL 中的 `share_source` 与 `vd_source` 已删除，不进入后续命令和记录。

## 3. 覆盖限制

本样本集覆盖：

- 三个不同作者的中文技术视频；
- AI Agent、AI 编程工作流和量化交易三个主题；
- Bilibili 真实输入；
- 总时长约 25 分 04 秒。

本样本集不覆盖：

- 已确认的平台字幕成功路径；
- 本地视频文件；
- 超过 60 分钟的长视频；
- 较差录音或强口音的明确分层；
- 干净 Windows 安装；
- 多用户或重复使用；
- 统计性 `8/10` 产品 Gate。

## 4. 结果

| ID | 首次系统结果 | 修复后结果 | Artifact | 用户价值结果 | 备注 |
|---|---|---|---|---|---|
| `V01` | FAIL：`source_metadata_invalid`，无字幕媒体回退缺失 | PASS | `bnd_019fb286-e6b7-7be1-8128-9c6ea054c306` / `art_019fb286-e6b7-78cb-ba11-cd734dc54eac` | PASS | 425.2 秒；327 个源片段 |
| `V02` | PASS | PASS | `bnd_019fb28d-c1fb-721e-b869-f7dae723f3ca` / `art_019fb28d-c1fb-7ca1-8a3a-d2514b7fedf0` | PASS | 原 Pilot 名称有风险；后续术语复验客观 PASS |
| `V03` | PASS | PASS | `bnd_019fb292-70b5-7971-b15a-76c9774ce8ef` / `art_019fb292-70b5-754b-b46c-39b1e414e9cc` | PASS（新稿） | 原 Pilot FAIL；术语修正稿 `bnd_019fb327-2cd4-7407-aef1-1b8b35e2bdad` / `art_019fb327-2cd4-7faf-9f68-389f85823fc2` 经用户重评 PASS |

系统最终结果为修复后 `3/3`，但首次结果是 `2/3`。用户原判定为 V01/V02 PASS、V03 FAIL；新 V03 术语修正稿经用户重新判定 PASS，因此修正后用户价值为 `3/3`。详细证据见 [`report.md`](report.md)。
