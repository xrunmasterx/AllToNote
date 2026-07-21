# AllToNote Windows / macOS 发布实施计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-18-alltonote-platform-release-design.md
  - ../specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
implementation_status: not-started
last_verified_at: 2026-07-18
```

## 1. 开始条件

Windows packaging可早做开发构建，但 stable发布必须等待 Runtime CLI、Video Bilibili/本地、Vault/Desktop、Review/personal Publisher最小闭环。macOS开始于共享Core/CLI/Pack合同稳定和Windows发布问题收敛后。

## 2. Task REL-00：支持矩阵/基线机

冻结目标Windows版本/x64、CPU/RAM/disk、非管理员/中文用户名/Defender；macOS最低版本/arm64后定。列出每个Pack/模型/依赖矩阵和“不支持”。建立clean VM/image，不在开发机冒充发布验收。

## 3. Task REL-01：可重复 Runtime Build

比较 PyInstaller onedir/等价工具：冷启动、大小、动态库、签名、杀软、更新、许可证。锁定toolchain/deps；生成version/runtime-lock/SBOM/license/hash；clean machine `version/info/doctor`。

## 4. Task REL-02：独立 Desktop Build

Tauri production config/CSP/capabilities/icon/version；不内嵌业务Runtime/模型；resolver指向受信managed Runtime；签名前安全测试。

## 5. Task REL-03：Windows Runtime Installer

per-user installer、stable discovery、user PATH、uninstall entry、machine state分离、upgrade/repair。测试无admin、中文/空格、已有旧版、多版、PATH冲突、silent enterprise options（若支持）。

## 6. Task REL-04：Windows Desktop/Combined Installer

签名Tauri NSIS/同类；组合安装兼容Desktop+Runtime，可选择Pack；离线模式；安装后handshake smoke。组合包不改变独立组件注册/版本。

## 7. Task REL-05：Pack Artifact Pipeline

media/transcribe/browser/document等manifest/platform/hash/signature/license/SBOM；immutable version/active/rollback；model asset separate；clean probe。生产签名key不入仓库/PR CI。

## 8. Task REL-06：Windows Code Signing

选择适合发布主体/地区的可信签名渠道或Store；密钥托管/RBAC/audit/timestamp/rotation；签inner binaries/installer/manifest；从官网下载后验证publisher/hash。开发self-signed只用于测试。

## 9. Task REL-07：Updater/Rollback

Desktop signed updater；Runtime staging/doctor/active pointer；Pack/model独立；active Job pin旧版本；JobStore migration backup；失败rollback。测试网络断、磁盘满、杀进程、manifest篡改、版本不兼容。

## 10. Task REL-08：中国大陆分发

国内主origin/CDN + 备用镜像；signed manifest；断点续传；offline bundle；不在线pip/GitHub；手动Pack导入；真实DNS/TLS/大文件/丢包/镜像故障。安装文档不要求关闭TLS/Defender。

## 11. Task REL-09：Windows 文件系统/安全

NTFS reparse/UNC/long path/case/reserved/ADS/OneDrive/锁/杀软；malicious Markdown/Pack/loopback；卸载/升级Vault hash。修复只能触碰program/state，不遍历删除Vault。

## 12. Task REL-10：Windows 产品 E2E

clean VM：install -> CLI -> mediaPack -> localVideo/Bilibili -> Bundle -> Vault tree/read/search -> Review/personal publish -> Obsidian edit -> restart -> update/rollback -> uninstall -> Vault hash unchanged。记录性能/版本/IDs，不保存正文/Secret。

## 13. Task REL-11：Windows Beta/Stable

staged beta；support/crash explicit feedback；known issues；rollback drill；SmartScreen/signature UX；通过后promote stable。YouTube external block单列，不阻塞已声明平台，但官网支持矩阵准确。

## 14. Task REL-12：额外 Windows 渠道（后续）

分别验证 WinGet manifest、MSIX/Store、MSI enterprise、ARM64。MSIX必须验证CLI PATH/open Vault/Pack sidecar/update/container/许可证；不因包能安装就替换主渠道。

## 15. Task MAC-00：macOS Dependency Matrix

在真实arm64 Mac审计 Python/Tauri/WebView/FFmpeg/yt-dlp/Whisper/Keychain/PDF/PPT Pack；锁定最低OS。Intel只在用户/维护价值成立后独立支持。

## 16. Task MAC-01：Core/CLI/State

arm64 Runtime build、platformdirs、Keychain、APFS case、FSEvents、external volume、GUI vs terminal PATH、sleep/wake。CLI-only Video/Vault/Review真实E2E。

## 17. Task MAC-02：Desktop/Resolver

Tauri app、runtime discovery、file bookmark/选择权限、temporary API、CSP、crash/reconnect；不假设Windows路径/registry。

## 18. Task MAC-03：Pack Matrix

arm64 media/transcribe；MLX仅独立capability；LibreOffice/OCR/browser；每Pack签名/manifest/quarantine/rollback。无Pack时清晰降级。

## 19. Task MAC-04：Signing/Notarization

Developer ID、nested signing、Hardened Runtime/entitlements/timestamp、notarytool/API、staple、Gatekeeper clean machine。签名从inner到outer；DMG/Runtime installer/Pack分别验证。

## 20. Task MAC-05：Updater/DMG/E2E

signed/notarized DMG、Runtime/Pack update/rollback、offline、uninstall/Vault unchanged；sleep/wake/restart；中文路径。输出独立macOS验收。

## 21. Task MAC-06：Intel/Store/Homebrew（可选）

真实用户与维护数据后选择；每种架构/渠道跑完整Gate，不能用universal Desktop掩盖Runtime/Pack缺失。

## 22. Supply Chain/CI

protected release environment、pinned actions/toolchains、provenance、SBOM/license、signing secrets隔离、artifact retention、release manifest、mirror verification、revoke/runbook。PR构建永不获得production key。

## 23. 验收文件

- `docs/acceptance/windows-release-v1.md`
- `docs/acceptance/macos-release-v1.md`（完成后）

记录 artifact hash/signature/OS/arch/install/update/rollback/product E2E/performance/Vault hash/known blocked。Windows完成不自动将macOS标完成。

## 24. 顺序

```text
REL-00..06
 -> REL-07..10
 -> REL-11
 -> REL-12（可选）

Windows stable/Core稳定
 -> MAC-00..05
 -> MAC-06（可选）
```
