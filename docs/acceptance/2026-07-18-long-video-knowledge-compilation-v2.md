# 长视频 Knowledge Note v2 / Faithful Edition 验收摘要

```yaml
doc_type: acceptance
status: completed
authority: evidence
upstream:
  - ../superpowers/specs/2026-07-14-alltonote-video-producer-design.md
  - ../superpowers/specs/2026-07-16-alltonote-long-video-knowledge-compilation-design.md
  - ../superpowers/specs/2026-07-14-alltonote-portable-artifact-source-bundle-design.md
implementation_status: accepted-from-persisted-real-youtube-transcript-live-acquisition-separately-blocked
last_verified_at: 2026-07-18
```

## 1. 验收范围

本摘要记录 Video Producer 在已经取得真实平台字幕后的长视频知识编译、质量检查、Portable Bundle 提交与恢复能力。它不把 YouTube 当前实时下载成功作为既成事实，也不保存 Cookie、Token、Provider 原始响应、完整 Prompt 或用户私人正文。

公开输入身份：

- 平台：YouTube；
- Video ID：`iBdMSwiyuRQ`；
- 验收材料：此前成功获取并持久化的真实平台 Transcript；
- 时长：`3934.55 s`，约 65 分钟；
- Transcript：`1494` segments。

## 2. 编译与质量结果

| 指标 | 结果 |
|---|---:|
| Knowledge Map 调用 | 7 |
| Global Compose 调用 | 1 |
| 执行 wave | 2 |
| 定向 Repair | 0 |
| 总耗时 | 568.4 s |
| H1 | 1 |
| H2 | 9 |
| 重复 H2 | 0 |
| 文内 citation | 87 |
| Evidence definitions | 84 |
| Quality | pass |
| Publish eligibility | eligible |
| iwiki semantic validation | pass |
| iwiki atomic commit | pass |

该结果证明的是：长 Transcript 可以按确定性 Chunk Plan 提取结构化知识，再经全局 compose 形成一篇标题层级统一、证据可追溯、可由 Publisher 审阅的文档，而不是把多篇分块笔记直接拼接。

## 3. 恢复与零重放

使用已经完成的 Job/Checkpoint 重新进入流程：

- 恢复耗时约 `1.4 s`；
- 已完成的模型阶段没有重放；
- 已提交 Bundle 没有重复提交；
- 说明持久化结果、checkpoint 与幂等边界在本次场景中有效。

## 4. 回归证据

最近一次 Video Producer 完整回归记录：

```text
1731 passed
2 skipped
3 subtests passed
0 failed
```

独立定向审查回归：

```text
181 passed
未发现可复现 P0/P1
```

这些数字是历史验收快照；任何新代码合入前仍必须在当前实现 worktree 重新运行适用测试，不得把本摘要当作永不过期的绿色状态。

## 5. 未覆盖与明确阻塞

本验收没有证明以下事项：

- 当前网络环境下从 YouTube URL 实时取得字幕或媒体；
- Cookie 导入在所有 Windows/Chrome 版本下稳定；
- Bilibili、本地文件、抖音、快手的 clean-machine 正式发布矩阵；
- Windows 安装包、Feature Pack、FFmpeg/转写器安装和升级闭环；
- 2–3 小时视频的质量/成本/时延预算；
- 不同来源语言和显式中文翻译型高保真精编稿的完整真实矩阵。

实时 YouTube acquisition 在本次环境中仍受平台 anti-bot 阻塞。它是 Acquisition Adapter/外部条件问题，不应倒推为 Transcript 后知识编译失败，也不应被无限重试拖住其他平台、Runtime、Vault 和 Publisher 的开发。

## 6. 发布前仍需通过的 Gate

1. 使用当前分支和依赖重新运行全量测试与 `git diff --check`；
2. Bilibili 与本地视频各完成至少一条无开发缓存的真实 E2E；
3. clean-machine 安装 Runtime、所需 Pack 和外部工具后执行同一 CLI；
4. 验证失败退出码、JSON envelope、取消、重启和 outcome-unknown 策略；
5. 生成脱敏 Receipt/Quality/Bundle 标识，并由 iwiki 当前发布 Validator 再验；
6. YouTube 仅在外部网络/平台条件改变后重新验收，不以绕过反自动化或使用不透明服务作为默认方案。
