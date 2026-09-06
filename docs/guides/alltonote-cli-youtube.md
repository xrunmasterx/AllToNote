# AllToNote CLI：YouTube 视频转笔记与本地逐字稿

```yaml
doc_type: guide
status: active
authority: execution
upstream:
  - docs/superpowers/specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
  - docs/superpowers/specs/2026-07-14-alltonote-video-producer-design.md
implementation_status: current-master
last_verified_at: 2026-08-20
verified_commit: 913a3a270fe7e81c3add23beae0c992e37454275
```

本文给人和自动化 AI 提供当前 `master` 上可直接执行的 Windows PowerShell 流程：

1. 输入 YouTube 链接，调用 LLM 生成结构化 Markdown 笔记；
2. 输入 YouTube 链接，只在本机转写为带时间戳的文本，不调用 Agent、LLM 或远程模型 API。

这里的“不用 AI”准确指“不使用生成式 LLM”。本地转写仍使用离线
`faster-whisper small / CPU / int8` 语音识别模型。当前 CLI 不能在完全不做语音识别或
语言模型处理的情况下，把无字幕视频变成文本。

## 1. 当前能力边界

| 目标 | 当前是否可用 | 真实入口 |
|---|---:|---|
| YouTube 链接直接生成笔记 | 否 | 先用 `yt-dlp` 下载，再把本地视频传给 `alltonote produce video` |
| 本地视频生成结构化笔记 | 是 | `alltonote produce video --recipe-version 2 ...` |
| 本地视频生成忠实整理稿 | 是，但仍调用 LLM | `--output faithful-edition` |
| 本地视频只生成逐字稿 | 是，内部入口 | 调用已安装 `transcribe-cpu` Pack 的 worker |
| `alltonote transcribe ...` | 否 | 当前没有该公开子命令，不得虚构 |

重要：`alltonote runtime capabilities` 当前可能把
`recipe.video.acquire.youtube` 显示为 `installed: true`，但生产组合中的
`PackedBilibiliVideoSourceAdapter` 仍不接收 YouTube URL。不要用 capability 输出代替真实
输入测试；直接传 YouTube URL 会得到 `source_unsupported`。

## 2. 一次性准备

以下命令从仓库根目录执行。需要 Python 3.11、Node.js、`yt-dlp`，以及已经登录的本机
Codex（仅生成 LLM 笔记时需要）。

### 2.1 安装当前源码 CLI

```powershell
cd G:\AllToNote
python --version
python -m venv .venv
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $Python -m pip install --upgrade pip
& $Python -m pip install --editable .\backend
$AllToNote = (Resolve-Path .\.venv\Scripts\alltonote.exe).Path
& $AllToNote version
```

如果使用 Windows V20 目录候选，把 `$AllToNote` 改为候选目录中的 `alltonote.cmd`。
下载步骤仍使用上面虚拟环境中的 `$Python`，不要向候选自带的 Python 安装下载依赖。
V20 基于 `68d517f` 构建，早于本文核验的源码提交；它不是公开稳定安装包。

### 2.2 检查 Runtime 与 Pack

```powershell
& $AllToNote runtime doctor --dynamic --json
& $AllToNote pack doctor media-basic --json
& $AllToNote pack doctor transcribe-cpu --json
```

继续执行的条件：

- `runtime doctor` 的顶层 `ok` 为 `true`；
- `media-basic` 和 `transcribe-cpu` 均为 `installed: true, healthy: true`；
- 生成 LLM 笔记时，`dynamic.model.codex-app-server` 为 `pass`。

如果 Pack 未安装，只能从可信的 AllToNote 签名 Pack 目录安装：

```powershell
& $AllToNote pack install media-basic --source '<SIGNED_MEDIA_PACK_DIR>' --json
& $AllToNote pack install transcribe-cpu --source '<SIGNED_TRANSCRIBE_PACK_DIR>' --json
```

`<SIGNED_..._PACK_DIR>` 不是仓库源码目录；如果没有发布方提供的签名 Pack，停止并向用户
索取，不要下载来历不明的替代文件。

### 2.3 创建 Workspace

Workspace 保存 Job、逐字稿、Evidence、质量报告和最终 Markdown。它不应位于 AllToNote
机器状态目录内。

本地输入、工作区、模型和发布候选统一放在仓库的 `local-data/` 下；该目录同时由
`.gitignore` 和 `.dockerignore` 排除，不提交浏览器配置、Cookie、模型或个人笔记。

```powershell
$Workspace = 'G:\AllToNote\local-data\workspaces\youtube-notes'
& $AllToNote workspace init $Workspace --name youtube-notes --set-default --json
```

已经初始化时可直接复用，并在后续命令中始终显式传 `--workspace $Workspace`。

### 2.4 准备 `yt-dlp`

```powershell
& $Python -m pip install --upgrade yt-dlp
node --version
& $Python -m yt_dlp --version
```

Node.js 用于解决 YouTube 的 JavaScript challenge。下载视频需要网络；本地转写阶段不需要
网络。

## 3. 公共步骤：把 YouTube 链接下载为本地视频

先设置本次输入和下载目录：

```powershell
$YouTubeUrl = 'https://www.youtube.com/watch?v=FOaPOP1GO_Q'
$DownloadDir = 'G:\AllToNote\local-data\inputs\FOaPOP1GO_Q'
New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null
```

先尝试匿名下载：

```powershell
$downloadOutput = & $Python -m yt_dlp `
  --js-runtimes node `
  --remote-components ejs:github `
  --format 'b[ext=mp4]/b' `
  --output (Join-Path $DownloadDir '%(id)s.%(ext)s') `
  --print 'after_move:filepath' `
  $YouTubeUrl
$downloadExit = $LASTEXITCODE
$videoPath = [string]($downloadOutput | Select-Object -Last 1)
$videoPath = $videoPath.Trim()

if ($downloadExit -ne 0 -or -not $videoPath -or
    -not (Test-Path -LiteralPath $videoPath)) {
  throw 'YouTube 下载失败'
}
$videoPath = (Resolve-Path -LiteralPath $videoPath).Path
```

若出现 `Sign in to confirm you're not a bot`、年龄限制或账号可见性限制，使用从正常浏览器
导出的 Netscape 格式 Cookie 文件重试：

```powershell
$CookieFile = 'C:\secure\www.youtube.com_cookies.txt'
$downloadOutput = & $Python -m yt_dlp `
  --cookies $CookieFile `
  --js-runtimes node `
  --remote-components ejs:github `
  --format 'b[ext=mp4]/b' `
  --output (Join-Path $DownloadDir '%(id)s.%(ext)s') `
  --print 'after_move:filepath' `
  $YouTubeUrl
$downloadExit = $LASTEXITCODE
$videoPath = [string]($downloadOutput | Select-Object -Last 1)
$videoPath = $videoPath.Trim()

if ($downloadExit -ne 0 -or -not $videoPath -or
    -not (Test-Path -LiteralPath $videoPath)) {
  throw '带 Cookie 的 YouTube 下载仍然失败'
}
$videoPath = (Resolve-Path -LiteralPath $videoPath).Path
```

Cookie 是账号凭据：不要显示内容、不要复制进日志或文档、不要提交到 Git，且只对用户明确
授权的视频使用。`--remote-components ejs:github` 会联网获取 yt-dlp 的 EJS challenge 组件。

## 4. 方式 A：调用 LLM 生成结构化笔记

当前已真实验证的组合是 Video Recipe v2、`knowledge-note`、本地 CPU 转写和
`gpt-5.6-terra`。模型可按当前 Codex 可用模型替换；模型不可用时先运行
`runtime doctor --dynamic --json`，不要盲目重试。

```powershell
$Model = 'gpt-5.6-terra'
$NoteFile = Join-Path $DownloadDir 'note.md'

$produceRaw = & $AllToNote produce video `
  --input $videoPath `
  --workspace $Workspace `
  --wait `
  --json `
  --recipe-version 2 `
  --provider-profile default `
  --transcriber-profile default `
  --model $Model `
  --quality balanced `
  --style structured `
  --screenshot-policy off `
  --output knowledge-note `
  --output-language zh-CN

$produceExit = $LASTEXITCODE
$produce = $produceRaw | ConvertFrom-Json
if ($produceExit -ne 0 -or -not $produce.ok -or $produce.job.state -ne 'succeeded') {
  throw "AllToNote 生成失败：$($produce.error.code) $($produce.error.message)"
}

$draftId = $produce.job.result_refs.primary_draft_artifact_id
$draftRaw = & $AllToNote draft show $draftId `
  --workspace $Workspace `
  --presentation reading `
  --body-bytes 262144 `
  --json

$draftExit = $LASTEXITCODE
$draft = $draftRaw | ConvertFrom-Json
if ($draftExit -ne 0 -or -not $draft.ok -or $draft.data.body_truncated) {
  throw '读取完整 Markdown 笔记失败'
}
[IO.File]::WriteAllText(
  $NoteFile,
  [string]$draft.data.body,
  [Text.UTF8Encoding]::new($false)
)
Write-Output $NoteFile
```

成功判据不是只有进程退出码：

- 顶层 `ok == true`；
- `job.state == "succeeded"`；
- `job.result_refs.primary_draft_artifact_id` 存在；
- 对高质量可发布笔记，还应检查
  `job.result_refs.quality_overall == "pass"` 和
  `job.result_refs.publish_eligible == true`。

如果目标是尽量忠实于原话的整理稿，可把上面命令改为：

```text
--output faithful-edition --faithful-language preserve-source
```

`faithful-edition` 仍会调用 LLM，并不是“无 AI”模式。

## 5. 方式 B：不调用 LLM，只生成本地逐字稿

当前没有公开的 `alltonote transcribe` 子命令。以下是当前版本唯一可复用的本地入口：从
Runtime 的 `active.json` 解析固定、签名且已安装的 `transcribe-cpu` Pack，然后调用其
内部 worker。该接口属于内部实现，升级后必须重新核对本文。

从仓库根目录运行：

```powershell
$InputVideo = $videoPath
$TranscriptJson = Join-Path $DownloadDir 'transcript.json'
$TranscriptText = Join-Path $DownloadDir 'transcript.txt'
$CpuThreads = [Math]::Min([Environment]::ProcessorCount, 8)

$pathsEnvelope = & $AllToNote runtime paths --show-paths --json | ConvertFrom-Json
if (-not $pathsEnvelope.ok) { throw '无法读取 AllToNote Runtime 路径' }
$dataDir = ($pathsEnvelope.data.paths | Where-Object role -eq 'data').path

$packVersion = 'faster-whisper-1.1.1-small-536b0662-r1'
$packRoot = Join-Path $dataDir "packs\transcribe-cpu\$packVersion"
$active = Get-Content -Raw -Encoding UTF8 (Join-Path $packRoot 'active.json') |
  ConvertFrom-Json
$generationHash = ([string]$active.manifest_sha256) -replace '^sha256:', ''

$generation = Join-Path $dataDir "pack-store-v1\t\$generationHash"
if (-not (Test-Path -LiteralPath $generation)) {
  $generation = Join-Path $packRoot "installs\$generationHash"
}

$packPython = Join-Path $generation 'python\python.exe'
$modelPath = Join-Path $generation 'models\small'
if (-not (Test-Path -LiteralPath $packPython) -or
    -not (Test-Path -LiteralPath $modelPath)) {
  throw 'transcribe-cpu Pack generation 不完整'
}

$backendPath = (Resolve-Path '.\backend').Path
$previousPythonPath = $env:PYTHONPATH
$previousHfOffline = $env:HF_HUB_OFFLINE
$previousTransformersOffline = $env:TRANSFORMERS_OFFLINE
$env:PYTHONPATH = $backendPath
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$previousConsoleOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$driver = "import json,sys; from app.adapters.video_packs.transcribe_cpu_worker import transcribe_request; request={'schema_version':1,'media_path':sys.argv[1],'model_path':sys.argv[2],'cpu_threads':int(sys.argv[3])}; result=json.dumps(transcribe_request(request),ensure_ascii=False,allow_nan=False).encode('utf-8'); sys.stdout.buffer.write(result)"

try {
  $responseRaw = & $packPython -B -c $driver `
    (Resolve-Path -LiteralPath $InputVideo).Path `
    (Resolve-Path -LiteralPath $modelPath).Path `
    ([string]$CpuThreads)
  if ($LASTEXITCODE -ne 0 -or -not $responseRaw) {
    throw '本地转写 worker 失败'
  }
} finally {
  $env:PYTHONPATH = $previousPythonPath
  $env:HF_HUB_OFFLINE = $previousHfOffline
  $env:TRANSFORMERS_OFFLINE = $previousTransformersOffline
  [Console]::OutputEncoding = $previousConsoleOutputEncoding
}

$response = $responseRaw | ConvertFrom-Json
[IO.File]::WriteAllText(
  $TranscriptJson,
  ($response | ConvertTo-Json -Depth 8),
  [Text.UTF8Encoding]::new($false)
)

$lines = foreach ($segment in $response.segments) {
  $start = [TimeSpan]::FromSeconds([double]$segment.start)
  $hours = [Math]::Floor($start.TotalHours)
  $timestamp = '{0:D2}:{1:D2}:{2:D2}' -f `
    [int]$hours, [int]$start.Minutes, [int]$start.Seconds
  "[$timestamp] $($segment.text)"
}
[IO.File]::WriteAllLines(
  $TranscriptText,
  [string[]]$lines,
  [Text.UTF8Encoding]::new($false)
)
Write-Output $TranscriptText
```

这个流程不会调用 Codex、Agent、LLM 或在线转写 API；Pack 安装完成后，转写阶段可以离线
运行。输出是逐字稿而不是经过总结、重组和语义校验的知识笔记。

## 6. AI 执行约束

后续 AI 使用本文时必须遵守：

1. 先核对当前提交和 `alltonote ... --help`；若实现已变化，以当前代码和帮助为准并更新本文。
2. 不要把 YouTube URL 直接传给当前 `produce video`，除非真实 E2E 已证明适配器支持。
3. 不要声称存在 `alltonote transcribe`；当前无 LLM 流程调用的是内部 Pack worker。
4. 不读取或输出 Cookie 内容；Cookie 文件只作为 `yt-dlp --cookies` 的路径参数。
5. 所有路径使用绝对路径，所有 JSON 命令同时检查退出码、顶层 `ok` 和 Job 最终状态。
6. `runtime capabilities` 是声明性探针，不是 YouTube 下载成功的证据。
7. `knowledge-note` 和 `faithful-edition` 都会调用 LLM；只有第 5 节不调用 LLM。

## 7. 已验证事实

2026-08-20 在提交 `913a3a270fe7e81c3add23beae0c992e37454275` 上使用
`https://www.youtube.com/watch?v=FOaPOP1GO_Q` 验证：

- 直接 URL 任务以 `source_unsupported` 失败；
- `yt-dlp 2026.07.04 + Node + ejs:github + Netscape Cookie` 成功下载 821.847 秒 MP4；
- 本地 `transcribe-cpu` 生成 294 条记录，末段时间为 804.720 秒；
- `gpt-5.6-terra` 的 Recipe v2 `knowledge-note` 成功，质量报告为 `pass`，含 21 个时间戳引用；
- 同一输入使用 `gpt-5.6-sol` 曾在 `generate_draft` 阶段以
  `knowledge_coverage_invalid` 失败，因此模型调用成功不等于整条 Job 必然成功。
