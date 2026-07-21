# AllToNote Windows Tier 1 与 macOS Tier 2 发布设计

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-18-alltonote-runtime-cli-feature-pack-design.md
downstream:
  - ../plans/2026-07-18-alltonote-platform-release-implementation-plan.md
implementation_status: not-started
last_verified_at: 2026-07-18
```

## 1. 决策摘要

发布采用“独立组件 + 官方组合安装器”而不是单一巨型 EXE：

```text
Desktop package（薄 UI）
Runtime package（CLI/Core/Job）
Feature Pack packages（媒体/OCR/浏览器/模型/代码）
Offline bundle（验证过的组合便利包）
```

Windows 是 Tier 1，先形成可安装、签名、升级、回滚、卸载、离线和中国大陆镜像的完整闭环。macOS 是 Tier 2，在共享 Core/CLI 合同稳定后完成 Keychain、APFS/FSEvents、签名、公证、DMG 和平台 Pack 验收。

Windows v1 主渠道推荐签名的 Tauri NSIS/同类 per-user EXE 安装器 + 独立 Runtime 安装器；MSIX/Store 作为后续渠道评估，不作为首发唯一格式。原因是 AllToNote 需要稳定 CLI PATH、独立 Runtime/Pack 更新和开放 Vault 路径，首发应先减少容器化/虚拟化带来的发布不确定性。

Runtime 推荐 directory-based bundle（例如 PyInstaller onedir 或等价方案），避免每次 CLI 启动都从 one-file 包解压，便于冷启动、组件校验、差分更新和故障定位。是否最终使用 PyInstaller 是实现选择，不能改变 Runtime/CLI 合同。

## 2. 目标

- 普通用户一个组合安装器即可开始；
- 高级用户可只安装 Runtime/CLI；
- Desktop 和 Runtime 可独立升级、兼容握手和回滚；
- Pack/模型按需下载，不进入最小安装；
- 所有正式产物签名、hash、SBOM、许可证齐全；
- 中国大陆可直接下载、断点续传和离线安装；
- 更新/卸载绝不删除或修改 Vault；
- clean machine 能完成真实 Video/Vault/Review 流程；
- Windows 无管理员权限可完成默认 per-user 安装；
- macOS 正式版通过 Developer ID/Hardened Runtime/notarization；
- 发布失败可以快速停止 rollout 并恢复上一兼容组合。

## 3. 非目标

- 首发同时覆盖 Windows/macOS/Linux/移动端全部组合；
- 把所有 GPU/模型/浏览器/Office 依赖塞进安装器；
- 未签名正式包；
- 安装时从随机第三方地址运行脚本；
- 强制系统级管理员安装；
- 让 Desktop 更新器直接修改用户 Vault；
- 用自动更新替代可下载的完整离线包；
- 运行时临时 `pip install` 生产依赖；
- 在 Windows Gate 未闭合前承诺 macOS 同日首发；
- 首发公共插件市场。

## 4. 发布单元

### 4.1 Desktop

包含 Tauri shell、Web assets、Runtime Resolver、Desktop API client、UI 和最小原生集成。不包含 Python Core、FFmpeg、Whisper、模型、OCR 或浏览器 runtime。

### 4.2 Runtime

包含稳定 `alltonote` launcher、Core/SDK、CLI、JobStore migration、iwiki locked contract、Pack Manager。安装后可完全无 Desktop 运行。

### 4.3 Pack

按 `pack_id/version/platform/arch` 的不可变目录分发，含 signed manifest、hash、license、SBOM、entrypoint/probe。模型资产可以独立于 executable Pack，但同样有 manifest/hash/license。

### 4.4 Combined installer

组合安装器：

- 安装一组已验证的 Desktop + Runtime；
- 不把两者物理融合为同一进程；
- 允许用户取消 Desktop 或选择最小 Pack；
- 写入组件注册与卸载条目；
- 完成版本握手 smoke；
- 不默认下载大模型；
- 可提供完全离线版。

### 4.5 Portable/zip

仅为高级用户/诊断提供签名 zip/portable Runtime 时：

- 清楚说明 PATH/更新/凭据/目录行为；
- machine state 仍使用 platformdirs，不写进程序目录，除非显式 portable mode；
- portable mode 不得误把可执行目录当 Vault；
- 正式支持矩阵与 installer 分开。

## 5. 版本

独立版本：

- Desktop SemVer；
- Runtime SemVer；
- core/CLI/Desktop API major；
- Portable/iwiki contract；
- Pack version；
- model asset version；
- offline bundle release ID。

Compatibility Matrix 进入 signed release manifest：

```json
{
  "desktop": "1.2.0",
  "runtime": {"min": "0.9.0", "max_exclusive": "2.0.0"},
  "desktop_api": [1],
  "portable_api": 1,
  "packs_tested": {},
  "platform": "windows-x86_64"
}
```

“最新”不自动等于兼容。Desktop 在更新 Runtime 前先检查活动 Job 和 rollback 条件。

## 6. Windows 安装

### 6.1 主格式

首发采用 per-user signed EXE installer（优先利用 Tauri NSIS 生态）和独立 signed Runtime installer：

- 默认安装到用户应用目录；
- 不需要管理员权限；
- CLI PATH 写用户环境并提示新终端生效；
- Desktop shortcut/start menu；
- Runtime 注册到稳定 discovery location；
- Pack/data/config/cache 与 program files 分离；
- 卸载器枚举组件但不触碰 Vault。

需要 all-users/企业安装时另做 MSI/管理策略，不让首发复杂度污染普通用户路径。

### 6.2 MSIX/Store

MSIX 提供可靠卸载、签名、包身份和差分更新，但 AllToNote 需验证：

- 外部 CLI PATH/alias；
- 开放 Vault/任意用户目录；
- Runtime/Pack 独立更新；
- sidecar/子进程/模型目录；
- AppContainer/文件系统虚拟化；
- Store policy 与 GPL/第三方工具分发。

验证通过后可作为额外渠道，不替换独立离线/CLI 包。Microsoft Store 可以降低签名/SmartScreen门槛，但不能成为中国大陆唯一分发渠道。

### 6.3 Code signing

生产包必须使用受信任 Authenticode/MSIX 签名并带安全时间戳。选择 Store、可信 CA、符合主体/地区条件的托管签名服务之一；不得把开发 self-signed 包发给普通用户。

签名密钥：

- 不存在仓库或普通 CI secret；
- 使用硬件/托管密钥或受控签名服务；
- 最小人员权限和审计；
- key rotation/revocation；
- 每个 exe/dll/installer/manifest 按发布策略签名；
- 公布 hash/signature verification 指南。

### 6.4 SmartScreen

新签名/低下载量可能触发信誉警告。发布计划需：

- 保持稳定 publisher identity；
- 避免频繁更换证书/文件名；
- 监测下载/告警反馈；
- 不教用户永久关闭安全防护；
- 提供签名验证和官方域名说明；
- 可选 Store 渠道。

## 7. Windows Runtime/Pack

### 7.1 架构

Tier 1 必须支持 Windows x86_64。ARM64 在真实需求和依赖矩阵成熟后增加；不得把 x64 仿真结果当原生支持。

### 7.2 外部依赖

- media Pack 固定 FFmpeg/yt-dlp/JS runtime；
- Whisper CPU/GPU Pack 分开；
- CUDA/DirectML 等按真实支持拆分，不自动覆盖系统 driver；
- Browser Pack 使用受控 browser 或系统浏览器桥；
- Office/OCR 独立；
- 每个 Pack 在 clean machine probe。

### 7.3 Windows 文件系统

正式 Gate 包括：

- NTFS reparse point/junction/symlink；
- UNC/network share policy；
- long path；
- case-insensitive collision；
- reserved name/ADS；
- Defender/杀软扫描和文件锁；
- OneDrive/同步 Vault；
- 中文用户名/路径；
- FAT/exFAT 降级（如支持）；
- 文件 watcher overflow/rename。

## 8. macOS Tier 2

### 8.1 支持基线

在实施计划中基于 Tauri/WebView/Python/Pack 依赖锁定最低 macOS 版本。推荐先以仍获安全更新、Tauri 和 Python runtime 可靠支持的版本为基线，不在设计文档硬编码过早过宽的承诺。

Apple Silicon arm64 是首要 Gate。Intel x86_64 是否 GA 取决于用户比例和 Whisper/Office/浏览器 Pack 的可维护性；若提供 universal Desktop 但 Runtime/Pack 分架构，也必须在安装器/manifest 中清楚表达。

### 8.2 签名与公证

直接分发必须：

- Developer ID 签名；
- 所有嵌套 executable/dylib/sidecar/Pack 具有效签名；
- Hardened Runtime；
- secure timestamp；
- 合理 entitlements；
- `notarytool`/Notary API 提交；
- stapling；
- Gatekeeper clean-machine 验证。

签名顺序从最内层依赖到外层 app/DMG。任何可动态安装 Pack 都需独立签名/manifest，并验证 macOS quarantine/Gatekeeper 行为。

### 8.3 分发

- signed/notarized DMG 为直接下载主格式；
- 独立 Runtime pkg/zip 依据 CLI 安装体验评估；
- Homebrew cask/formula 作为后续辅助渠道，不作为唯一渠道；
- App Store sandbox 是否适配开放 Vault、CLI、Pack/sidecar 需专项验证，非首发 Gate。

### 8.4 macOS 平台差异

- Keychain；
- APFS case sensitivity/clone/snapshot；
- FSEvents/watch overflow；
- file bookmark/用户选择权限；
- quarantine；
- app translocation；
- shell PATH（GUI 与 terminal 不同）；
- arm64/x86_64/Rosetta；
- MLX Whisper 仅相应硬件 Pack；
- external volume/network share；
- 中文路径；
- sleep/wake 和 long Job。

## 9. 更新架构

### 9.1 Desktop

使用签名的 Tauri updater 或等价机制：

- signed manifest/artifact；
- channel；
- staged rollout；
- update available/required policy；
- 下载后验证再安装；
- 更新失败保留旧版本；
- 不在 UI bundle 中夹带 Runtime 数据迁移。

### 9.2 Runtime

Runtime updater/installer：

1. 检查 compatibility 和 active Job；
2. 下载到 staging；
3. 签名/hash/SBOM/manifest 验证；
4. 解压/安装到新不可变版本；
5. `runtime doctor` + smoke；
6. 原子切换 active；
7. 保留上一版；
8. 新 Job 使用新版本，旧 Job 固定旧版或等待完成；
9. 失败自动回滚 active pointer。

### 9.3 Pack/model

同样采用 immutable version + active pointer。大模型资产可跨 Pack 复用 content-addressed blob，但引用/许可证/完整性必须明确。

### 9.4 数据迁移

- JobStore schema migration 与程序激活分阶段；
- 迁移前备份；
- migration idempotent/可检测；
- 不兼容 migration 不自动降级读取；
- 未完成 Job 在 major 迁移前处理；
- Vault schema 只由 iwiki 合同迁移工具拥有；
- 更新器不批量改用户 Markdown。

## 10. 回滚

Rollback Matrix：

| 单元 | 回滚 |
|---|---|
| Desktop | 恢复上一签名版本，兼容 Runtime |
| Runtime | 切回上一不可变版本，先验证 JobStore schema 可读 |
| Pack | 新 Job 切回旧 active；运行 Job继续 pinned version |
| Model | 恢复旧 manifest/blob ref |
| Public Knowledge | 选择旧 package version，不覆盖 personal |
| JobStore migration | 只有有明确 reverse/backup restore policy 时 |

不能用程序回滚恢复/覆盖 Vault。用户已发布知识通过 Review/Publisher/iwiki revision 处理。

## 11. 卸载

卸载 UI 显示独立选项：

- Desktop；
- Runtime；
- Packs/models/cache；
- machine Job/log/config；
- account token。

默认保留 config/Job history 可供重装恢复，或由用户明确选择删除。无论选择如何，Vault 永不自动删除。卸载前后对测试 Vault 计算完整 hash tree 验证。

## 12. Release manifest 与供应链

每次发布产出：

- source commit/tag；
- reproducible/build provenance（能力范围内）；
- Desktop/Runtime/Pack artifact manifest；
- SHA-256 或更强 hash；
- signatures/timestamps/notarization IDs；
- SBOM；
- third-party license notices；
- dependency/Pack compatibility；
- migration notes；
- known issues；
- test/acceptance summary；
- rollback target；
- mirror list。

CI 使用 pinned actions/toolchains/dependencies、最小权限、protected release environment。发布签名与普通 build 分离；PR 不可获得 production signing key。

## 13. 中国大陆分发

- 官方国内域名、TLS 和对象存储/CDN；
- 备用独立镜像；
- manifest 签名使镜像只负责传输；
- installer 不依赖在线 pip/npm/GitHub；
- offline bundle 包含选定 Runtime/Desktop/media Pack/许可证/校验；
- 大模型包单独下载，支持断点/镜像；
- 客户端允许用户手动导入已下载 Pack；
- 下载失败给出 mirror/校验/代理诊断，不自动关闭 TLS；
- 网站/更新/下载/账号各自有可达性 smoke；
- 对 YouTube 等外部平台不可达/风控与 AllToNote 安装可用性分开说明。

## 14. 发布 Gate

### 14.1 Windows Tier 1

支持矩阵至少覆盖当前受支持 Windows 10/11 x64 版本（确切构建在实施时冻结）：

- clean per-user install；
- CLI PATH/version/runtime doctor；
- Desktop Resolver/API；
- media Pack install；
- 本地视频真实 transcribe/compile；
- Bilibili/可用平台真实路径；
- Vault tree/read/search；
- Review/personal publish；
- restart zero replay；
- update/rollback/uninstall；
- offline bundle；
- 中文路径；
- Defender/非管理员；
- Vault hash unchanged；
- signatures/SBOM/license。

### 14.2 macOS Tier 2

- clean arm64 install；
- Gatekeeper/no manual bypass；
- CLI PATH/GUI shell；
- Keychain；
- APFS/FSEvents；
- media/transcribe Pack；
- Vault/Review/Publisher；
- update/rollback/uninstall；
- sleep/wake/restart；
- notarization/stapling；
- Vault hash unchanged。

Intel 若发布必须独立跑同级 Gate，不能只证明 Desktop 打开。

## 15. 性能预算

- Desktop 最小安装包和 Runtime 安装包分别记录大小，禁止用组合包掩盖；
- 最小安装不含模型，目标大小由基线测量后锁定；
- CLI `version/info` 冷启动满足 Runtime 设计预算；
- Desktop 冷启动到 shell/握手满足预算；
- onedir bundle 不每次解压；
- updater 只下载需要的组件/差分（可行时）；
- Pack 未使用不加载；
- idle 无 Engine/worker；
- 安装/更新峰值磁盘在开始前预检。

## 16. 发布流程

```text
freeze source + contract locks
 -> build per platform/arch
 -> unit/integration/security
 -> sign inner binaries/packs
 -> package/sign installer/app
 -> notarize macOS
 -> clean-machine E2E
 -> generate SBOM/licenses/manifest/acceptance
 -> upload origin + mirrors
 -> verify downloaded bytes/signatures
 -> staged beta rollout
 -> monitor explicit crash/support signals
 -> promote stable or rollback
```

不得在 clean-machine E2E 前把 artifact 标 stable。

## 17. 测试与验收记录

每个 artifact 关联不可变 acceptance summary：

- build ID/hash；
- OS/arch；
- install/update source；
- test suite/结果；
-真实输入 identity（脱敏）；
- Bundle/Quality/Receipt IDs；
- performance；
- known blocked external platforms；
- rollback/uninstall/Vault hash；
- signer/notarization status。

不记录 Cookie、API Key、用户正文、完整 Prompt 或 Provider raw。

## 18. 分期

### Phase PR0：可重复 Runtime/Desktop 构建

独立 artifact、version info、SBOM/license、clean CLI smoke。

### Phase PR1：Windows 内测安装

per-user signed installer、Runtime discovery、组合安装、media Pack、手动更新/回滚。

### Phase PR2：Windows 正式更新/镜像

signed updater、staged rollout、离线 bundle、国内双源、完整产品 E2E。

### Phase PR3：Windows stable

签名/SmartScreen/支持矩阵/安全/卸载/Vault不变 Gate 全通过。

### Phase PR4：macOS Core/CLI

arm64 Runtime、Keychain/APFS/FSEvents、CLI/Vault/Video Pack。

### Phase PR5：macOS Desktop/distribution

Tauri app、DMG、Developer ID/Hardened Runtime/notarization/updater/E2E。

### Phase PR6：额外渠道/架构

MSIX/Store/WinGet/Homebrew/Intel/Windows ARM64 按用户数据和维护成本逐项加入。

## 19. 完成定义

1. Desktop、Runtime、Pack 是独立签名/版本化单元；
2. 组合安装器只提供便利，不改变 CLI/Core 所有权；
3. Windows 普通用户无管理员权限可完成安装和真实产品闭环；
4. 中国大陆有在线双源与离线包，安装不依赖 GitHub；
5. 更新失败可回滚，活动 Job 固定旧版本；
6. 卸载/更新前后 Vault hash 不变；
7. 正式包有可信签名、SBOM、许可证、manifest 和验收摘要；
8. Windows Tier 1 全矩阵通过后才标 stable；
9. macOS arm64 通过签名、公证、Keychain/APFS/真实 E2E 后才宣称支持；
10. 额外渠道/架构不降低主渠道的开放 CLI、独立 Runtime 和离线能力。
