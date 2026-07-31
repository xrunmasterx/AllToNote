# Official Video Packs 与真实视频验收

日期：2026-07-31

范围：`media-basic`、`transcribe-cpu` 的固定合同、签名发布、安装解析、Video Recipe 运行时装配，V01/V02/V03 三条真实 Bilibili 输入，以及项目自建本地视频夹具

结论：实现 Gate、三条真实 Bilibili 输入和项目自建本地视频正式链路均通过。当前证据支持 Windows x86_64 本机发布候选 dogfood；不扩张为独立非管理员 clean-machine、安装器、自动更新或跨平台公开发布声明。

## 固定合同

| Pack | 固定版本 | 已签名 manifest SHA-256 |
|---|---|---|
| `media-basic` | `yt-dlp-2026.7.4-ffmpeg-8.1.2-r1` | `sha256:c50a8edb2b530b70fdccade4fb7ddfebf8c3a6792702e660eb85f69f5c189e24` |
| `transcribe-cpu` | `faster-whisper-1.1.1-small-536b0662-r1` | `sha256:d47c7568cc0e27b4f75fb63b86a8195ddc09a1f260e9512238e17220b8e3f970` |

签名私钥不进入仓库或 Pack；Runtime 只内置公钥并验证签名、manifest、文件哈希、入口点和固定平台合同。

## 已关闭的实现风险

- Video Job 在提交事件中冻结实际参与的 Pack ID、版本、平台与 manifest digest；恢复旧 Job 时按已记录 digest 调用 `resolve_exact`，不会静默切换到当前 active generation。
- 平台字幕路径只要求 `media-basic`。`transcribe-cpu` 已安装时才冻结进 Job；没有字幕并且没有 ASR Pack 时明确失败，不在 Runtime 启动阶段提前阻断。
- `receipt.json` 的 Attempt ID、逐步状态、重试序号和起止时间来自 SQLite JobStore；不再为所有步骤生成同一个合成时间，也不再声称候选组装、质量验证和 commit 已在收据生成前完成。
- Bilibili 的 `share_source`、`vd_source` 在请求持久化与幂等哈希前移除；未知参数和其他平台输入不做推测性重写。
- Bilibili Cookie 只发送到官方 API；字幕正文请求使用独立的无 Cookie 请求头，并限制为 HTTPS 官方字幕域名且禁止重定向。
- Worker 请求限制为 64 KiB；输出写入自动删除的临时文件，并在运行期间按上限主动终止，父进程不会无界缓冲 stdout。
- Worker 使用最小环境变量集合，超时、取消和输出违规都会终止所创建的进程树。

## 三条真实输入

三条输入均在隔离的 clean-machine 目录中，通过离线安装的 Runtime wheel 和两个签名 Pack 执行。它们是用户提供的含分享参数 URL；连接器解析的 BVID 与冻结样本一致。分享参数持久化问题是在三条运行后发现并修复的，因此内容 E2E 与隐私合同测试分别记录，不声称重新执行了三条昂贵生成。

| 样本 | Job / Bundle | 用时 | 结果 |
|---|---|---:|---|
| V01 Kimi K3 + Freqtrade | `job_019fb649-fa56-7846-9042-b8b00621309b` / `bnd_019fb649-fa56-71e1-af9b-220d0e1ae2b3` | 388.5 s | quality `pass`，publishable；17 个 H2 均有正文 Evidence；复用既有 `source_id` 并生成新 Revision |
| V02 什么是 Agent | `job_019fb651-b593-7e32-840a-a007ae0e79fd` / `bnd_019fb651-b593-7eec-9ce5-2490318b1d3b` | 242.6 s | quality `pass`，publishable；9 个 H2 均有正文 Evidence；Agent、LLM、API、memory 等术语正确 |
| V03 Orca ADE | `job_019fb655-dcbd-71e5-83ea-99111f1e47f1` / `bnd_019fb655-dcbd-762a-b868-637d2a7de230` | 326.6 s | quality `pass`，publishable；14 个 H2 均有正文 Evidence；Orca、Agent、Git worktree、Claude Code、Codex、OpenCode、GPT-4o 等术语正确 |

三个 Bundle 的收据均绑定上表两个签名 Pack digest。Evidence 采用正文内可读引用，Draft 不再附带面向机器的逐段脚注列表。

## 本地视频正式链路

运行输入为仓库内项目自建夹具 `backend/tests/fixtures/video/local-course.mp4`，12 秒、83,228 字节，SHA-256 为 `sha256:4493bc26df912798b56a754ede158f229970d2ecb81d2892c13aed058b2ac08e`。它不包含第三方视频字节。

- Runtime 来源提交：`34cbf9550edae6fabae1fd13c04cc623bd5c401b`
- Runtime wheel SHA-256：`sha256:ee310bec3e54ab6fd79e11c7b1fc53cedc8c456fdba86d6b810d4c9fb4c23edc`
- Job / Bundle：`job_019fb7db-24bf-77d5-bb1e-ef66e58a4958` / `bnd_019fb7db-24bf-7ccf-afed-606009582202`
- 首次 Produce：54.798 秒，quality `pass`、publishable
- Source：`canonical_uri=null`、`materialization.kind=external_local`、`duration_ms=12000`
- Transcript：`faster-whisper/1.1.1/small/cpu-int8@536b0662742c02347bc0e980a01041f333bce120`
- Pack：收据固定 `media-basic` 与 `transcribe-cpu` 的上述签名 manifest digest
- Portable：semantic validate `valid=true`、`issues=[]`；manifest SHA-256 为 `sha256:f1ee8bd1b023c05d4155b0de51c9b62a606c941a6e49ff27e51b096fe3507b9d`
- Evidence：最终 Markdown 只有正文引用和一个可读时间范围脚注，没有面向机器的逐段 Evidence 列表
- Restart：新 CLI 进程 `job wait` 用时 0.355 秒；前后事件数均为 2、末序号均为 2，Bundle 与源文件哈希均不变

该链路使用独立源码快照、独立 Runtime wheel/venv、离线依赖仓和已安装签名 Packs，但仍运行在同一 Windows 用户/机器上。因此它关闭实现与本机发布候选 Gate，不冒充独立非管理员用户或干净 VM Gate。新建 Workspace 的 strict validate 仍报告缺少 `wiki/common/index.md`；Bundle semantic validate 与 commit 不受影响，该问题归属 Workspace-init 后续合同。

## 验证证据

- 请求持久化、Runtime 工厂、恢复收据与 media-only 字幕路径：`13 passed`
- 平台字幕路径：`31 passed`
- Video Producer 与 Portable Bundle 集成：`289 passed`
- Video CLI 与请求持久化：`48 passed`
- SQLite JobStore：`205 passed`
- 完整 backend（本地正式链路修复后）：`2230 passed, 2 skipped, 3 warnings`
- `git diff --check`：通过

三个 warning 分别来自两个既有下载器正则表达式转义弃用提示，以及 `ctranslate2` 对上游 `pkg_resources` 的弃用提示；不是本轮引入的测试失败。

## 本地发布材料

已签名 Pack 和隔离验收材料保存在仓库外：

- `G:\.alltonote-release\video-packs-v1\media-basic-signed`
- `G:\.alltonote-release\video-packs-v1\transcribe-cpu-signed`
- `G:\.alltonote-release\video-packs-v1\clean-machine`
- `G:\.alltonote-release\video-packs-v1\local-video-34cbf95\测试 User`

Git 仓库不追踪 Pack、模型、clean-machine 虚拟环境、下载缓存或用户生成的 Bundle。公开发行仍需单独完成正式安装器、自动更新、完整离线依赖 wheelhouse、非管理员 Windows VM 和支持矩阵 Gate。
