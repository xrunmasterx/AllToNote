# Official Video Packs 实现验收

日期：2026-07-31

范围：`media-basic`、`transcribe-cpu` 的固定合同、签名发布、安装解析与 Video Recipe 运行时装配

结论：实现 Gate 通过；三条真实 Bilibili 输入的干净安装验收仍待执行

## 固定合同

| Pack | 固定版本 | 已签名 manifest SHA-256 |
|---|---|---|
| `media-basic` | `yt-dlp-2026.7.4-ffmpeg-8.1.2-r1` | `sha256:c50a8edb2b530b70fdccade4fb7ddfebf8c3a6792702e660eb85f69f5c189e24` |
| `transcribe-cpu` | `faster-whisper-1.1.1-small-536b0662-r1` | `sha256:d47c7568cc0e27b4f75fb63b86a8195ddc09a1f260e9512238e17220b8e3f970` |

签名私钥不进入仓库或 Pack；Runtime 只内置公钥并验证签名、manifest、文件哈希、入口点和固定平台合同。

## 已关闭的实现风险

- Video Job 在提交事件中冻结两个 Pack 的 ID、版本、平台与 manifest digest。
- 恢复旧 Job 时按已记录 digest 调用 `resolve_exact`；不会静默切换到当前 active generation。精确 generation 不存在时安全失败。
- `receipt.json` 记录实际执行的两个 Pack digest，而不是恢复进程启动时的 active digest。
- Bilibili Cookie 只发送到官方 API；字幕正文请求使用独立的无 Cookie 请求头，并限制为 HTTPS 官方字幕域名且禁止重定向。
- Worker 请求限制为 64 KiB；输出写入自动删除的临时文件，并在运行期间按上限主动终止，父进程不会无界缓冲 stdout。
- Worker 使用最小环境变量集合，超时、取消和输出违规都会终止所创建的进程树。

## 验证证据

- Video Pack adapters、安装器、验证器、解析器、worker 与发布工具：`51 passed`
- Pack CLI 与 Video CLI：`56 passed`
- 平台字幕路径与 Portable Bundle 集成：`263 passed`
- 完整 backend：`2163 passed, 2 skipped, 1 warning, 3 subtests passed`
- `python -m compileall -q app tools`：通过
- `git diff --check`：通过

唯一 warning 来自 `ctranslate2` 对上游 `pkg_resources` 弃用的提示；不是本轮引入的测试失败。

## 本地发布材料

已签名 Pack 保存在仓库外：

- `G:\.alltonote-release\video-packs-v1\media-basic-signed`
- `G:\.alltonote-release\video-packs-v1\transcribe-cpu-signed`

这些目录是本机发布输入，不属于 Git 源码。下一 Gate 是从干净 Runtime wheel 安装两个 Pack，运行 `pack doctor`，再执行用户指定的 V01、V02、V03 三条真实 Bilibili 输入；在该 Gate 完成前，本记录不宣称 Video Production 已整体发布。
