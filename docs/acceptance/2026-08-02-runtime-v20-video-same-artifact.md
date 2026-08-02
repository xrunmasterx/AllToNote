# Runtime V20 视频同制品验收

日期：2026-08-02

状态：当前 Windows 主机候选通过；不是公开安装包或干净虚拟机认证。

机器可读记录：[`2026-08-02-runtime-v20-video-same-artifact.json`](2026-08-02-runtime-v20-video-same-artifact.json)

## 1. 结论

本轮关闭了旧 V8 真实视频证据与当前源码不属于同一 Runtime 制品的问题。V20 从源码提交 `68d517f1fb5e0ce79429c36e56cb7b3c2acbd447` 离线构建，重新运行了项目自有本地视频和用户冻结的三个 Bilibili 输入；四个正常 Job 全部成功、质量通过且可发布。两个签名 Video Pack 在运行前后动态 doctor 均通过。

本轮只证明匿名 Bilibili 无平台字幕时的媒体下载与 CPU ASR 分支。三份 Bundle 都明确记录 `subtitle_acquisition=generated` 和 `transcript_basis=asr-transcript`，不得把它描述为平台字幕正路径通过。

## 2. 固定制品

- Runtime 源码：`68d517f1fb5e0ce79429c36e56cb7b3c2acbd447`。
- Runtime wheel：601,241 bytes；SHA-256 `ea2228ca8d4e4bb3203ba73d93eec2e425e42bf86508af108c9ff50f1da62792`。
- 可移植候选：715 files / 41,084,745 bytes；`release/file-manifest.json` SHA-256 `c1369d1603d2248ffc1bb07d028e7a1e860873b1289dc130640e52a6cbed1956`。
- `media-basic`：`sha256:c50a8edb2b530b70fdccade4fb7ddfebf8c3a6792702e660eb85f69f5c189e24`。
- `transcribe-cpu`：`sha256:d47c7568cc0e27b4f75fb63b86a8195ddc09a1f260e9512238e17220b8e3f970`。
- 转写器：`faster-whisper/1.1.1/small/cpu-int8@536b0662742c02347bc0e980a01041f333bce120`。
- 执行容量：1；三个正式网络 Job 严格串行。

候选在组装阶段通过 Builder/Wheel attestation、Engine 生命周期、Unicode Workspace、SQLite WAL 和旧 JobStore 迁移 Gate；真实 Job 结束后又以固定 manifest 摘要独立复核一次。

## 3. 真实结果

| 样本 | Job / Bundle | Job 耗时 | Draft / reading | Evidence | 结果 |
| --- | --- | ---: | ---: | ---: | --- |
| 本地夹具 | `job_019fc059-e593-7943-af2b-0290d9125e03` / `bnd_019fc059-e593-73dc-8d76-47796eb850ff` | 74.345 s | 289 / 176 bytes | 1 | pass；publishable |
| V01 | `job_019fc05b-9eb6-79bb-8ca7-c3b8283670d6` / `bnd_019fc05b-9eb6-7c39-985f-fd493b651205` | 427.635 s | 42,546 / 11,500 bytes | 275 | pass；publishable |
| V02 | `job_019fc062-fa97-7851-bdb2-ceba5a486cf2` / `bnd_019fc062-fa97-76e6-a646-d4952e3fa042` | 294.269 s | 28,406 / 7,755 bytes | 183 | pass；publishable |
| V03 | `job_019fc067-f904-7d8f-9d75-f2cf3f98b047` / `bnd_019fc067-f904-7cfd-ba6c-9e2fdd62ecfd` | 474.784 s | 16,316 / 8,990 bytes | 65 | pass；publishable |

V01/V02 各调用模型一次；V03 使用三次调用、两个顺序 wave；三条均无 quality repair、无 Job retry。三条正式输入的 acquire / ASR / draft 生成阶段耗时已经写入机器记录。

## 4. Evidence 与专有名词

reading 投影中的系统 Evidence 标记三条均为 0；audit 投影分别保留 550、366、130 个引用标记。`review show` 对每条样本都返回了来源链接、毫秒区间和原始转录摘录，说明默认降噪没有删除审计能力。

V02 reading 中 `DeepSeek=1`、`OpenClaw=5`、`Claude Code=4`，旧污染词 `DBC/OpenCrawl/OpenCrew/Cloud Code` 均为 0。Evidence 摘录仍保留 ASR 原文中的 `DBC`，因此 Draft 的规范拼写没有篡改审计事实。

V03 reading 中 `Claude Code=9`、`Grok=1`、`Issues=2`，`Cloud Code/Omapad/Growk/Asus` 均为 0。该次 Draft 没有提及 Amp；机器记录没有把“未提及”伪装成名称识别成功。

## 5. 可靠性

- 引擎停止后，V03 的 Job、Bundle、commit 和 Draft 引用仍可读取；重新 `ensure` 产生新的 Engine ID，结果引用保持一致。
- 新本地 Job 进入 running 后收到 cancel，689 ms 内变为 cancelled，没有 Bundle；`job wait` 返回稳定的 `job_cancelled`。
- 对 cancelled Job 提交精确版本的 retry request 后，子 Job 保留 `retry_of_job_id`，继承两个 Pack 摘要和本地输入 SHA，并在 67.352 s 后成功。
- 这项 retry 是冻结配置下的重新执行，不是跨 Job checkpoint 续跑。

## 6. 不得外推的结论

本记录不证明平台字幕正路径、YouTube/Douyin/Kuaishou、截图、capacity=2、多用户、干净 Windows 用户、Defender、签名安装包、Pack 在线分发、更新、回滚、卸载或公开稳定版。当前 Provider 复用了本机已经登录的 Codex app-server 会话，且 Provider 没有返回可认证的 token usage，因此不能给出精确模型成本结论。

旧三样本用户价值判定仍是有效的历史产品输入，但没有被静默复用为这三份新 Draft 的人工评分。本轮对新 Draft 的结论限定为自动质量、Evidence 可追溯、reading 降噪和目标专有名词检查通过。
