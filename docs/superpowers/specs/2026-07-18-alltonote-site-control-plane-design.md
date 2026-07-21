# AllToNote 网站、账号、设备与公共知识控制面设计

```yaml
doc_type: product
status: active
authority: subsystem
upstream:
  - 2026-07-12-alltonote-llm-iwiki-desktop-design.md
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-18-alltonote-runtime-cli-feature-pack-design.md
  - 2026-07-18-alltonote-knowledge-access-mcp-design.md
downstream:
  - ../plans/2026-07-18-alltonote-site-control-plane-implementation-plan.md
implementation_status: not-started
last_verified_at: 2026-07-18
```

## 1. 决策摘要

网站是可选云控制面，不是个人知识正文或本地编译流水线的宿主：

```text
网站拥有
  账号 / 邀请 / entitlement / 设备 / 下载 / 版本 / Pack
  公共知识包目录 / 公共远端 MCP / 订阅与配额 / 支持

本地 Runtime/Vault 拥有
  个人 Source / Transcript / Draft / Markdown / Job / Secret
  本地模型与 Agent / Review / personal Publisher
```

邀请制适合早期封闭测试，用于控制支持成本、发布节奏和风险，但不应成为永久身份模型。邀请码只授予一个 entitlement；账号才是长期身份。未来开放注册时，只需停止要求 `beta_access`，无需迁移用户知识或更换账号系统。

账号不应成为读取本地 Vault 或运行已安装基础能力的在线前置。登录用于下载受控版本、设备管理、公共服务、订阅/配额和未来付费能力；网站故障不能锁死用户自己的 Markdown。

## 2. 产品目标

- 支持邀请内测并平滑过渡到开放账号；
- 用户能管理账号、设备、下载、版本和 Pack；
- Desktop/CLI 可安全登录、刷新和撤销设备；
- 中国大陆用户可稳定访问，不依赖 Google/GitHub 等作为唯一登录/下载链路；
- 公共知识可以远端 MCP 读取，也可以按许可下载到本地；
- Runtime/Pack/公共知识更新有签名、hash、兼容和回滚信息；
- 只收集实现服务所需的最少云数据；
- 未来商业化不迫使个人正文上云；
- 本地账号注销/云服务终止后，开放 Vault 仍可正常读取。

## 3. 非目标

- 云端个人 Markdown 数据库；
- 云端个人全文搜索/RAG；
- 浏览器内运行本地 Producer；
- 网页直接读取用户本地 Vault；
- 强制在线激活所有本地功能；
- 社交笔记社区；
- 多人实时协作知识库；
- 将 Cookie/API Key/本地模型凭据同步到账号；
- 把公共知识与用户个人知识混在同一授权域；
- 用邀请码作为永久用户主键或权限表；
- 在未完成法律/安全评审前上线中国大陆生产服务。

## 4. 邀请制评估

### 4.1 合理使用场景

早期 AllToNote 包含下载器、模型、长视频、Pack、平台风控和大量本地环境组合，支持成本与外部平台不确定性很高。邀请制可用于：

- 限制同时涌入量；
- 选择能提供有效反馈的用户；
- 控制高成本公共服务配额；
- 逐步扩大 Windows 机器/网络/Provider 覆盖；
- 对安装、更新和失败恢复做小规模验证。

### 4.2 不合理用法

- 把邀请码直接当登录密码；
- 一个永久共享码无限使用；
- 邀请码决定所有后续角色/资源权限；
- 将本地知识加密钥匙绑定邀请码；
- 邀请过期导致本地 Vault 无法读取；
- 无法追踪/撤销滥用；
- 开放注册后仍在代码里到处判断 invite code。

### 4.3 推荐模型

```text
Account（长期身份）
  + Invite Redemption（一次历史事件）
  -> Entitlement: beta_access（可撤销/到期/分批）
  -> 可下载 beta channel / 使用特定公共服务
```

邀请码字段：id、hashed token、campaign、issuer、expires_at、max_redemptions、redemption_count、allowed region/channel、status。明文只在创建时展示；数据库存 hash。

### 4.4 开放注册迁移

阶段：

1. Closed alpha：创建账号必须邀请码；
2. Closed beta：可注册/候补，需 entitlement 下载 beta；
3. Open beta：任何账号可获得基础 entitlement，高成本能力仍有配额；
4. General availability：注册与付费/免费计划分开；邀请码只用于活动、团队或特殊渠道。

账号、设备、订单、知识包不需要迁移；只改变 entitlement issuance policy。

## 5. 账号模型

### 5.1 身份与登录

推荐分期：

- v1：邮箱 + 密码或邮箱验证，支持安全恢复；
- v1.1：Passkey/WebAuthn 作为优先二次/无密码方式；
- 中国大陆需求验证后：手机号 OTP/本地身份提供方；
- 企业/团队后：OIDC/SAML，不提前实现。

不把 Google/GitHub 登录设为唯一选项。手机号并非天然优于邮箱：它带来短信成本、攻击与号码换绑问题，应在真实转化数据和合规方案确定后接入。

密码使用成熟认证框架和现代自适应 hash；会话使用短期 access + 可撤销 refresh/session。认证实现优先采用经过审计的现成身份服务或成熟库，不自造密码学。

### 5.2 Account 与 Profile

- Account：登录、安全、状态、region、created/deleted；
- Profile：display name、locale、timezone、偏好；
- Entitlement：可用产品/渠道/配额；
- Subscription/Order：未来商业数据；
- 不在 Account 表放邀请码、设备密钥或个人知识正文。

### 5.3 删除与停用

- 用户可导出账号侧元数据；
- 删除流程处理会话、设备、token、订阅和法定保留；
- 本地 Vault 不在服务器控制范围，不受删除影响；
- Runtime 显示账号已注销，但仍允许离线读取和开放文件操作；
- 需要在线 entitlement 的服务明确不可用，不删除本地结果。

## 6. 设备模型

### 6.1 设备身份

首次绑定时 Runtime 在本地生成设备 key pair：

- private key 存 OS credential/key store；
- server 只保存 public key/fingerprint；
- 用户可命名设备；
- 收集最小 OS/arch/app version，不采集硬件序列号或全量指纹；
- reinstall 可生成新 device；
- 用户在网站可 revoke。

### 6.2 设备登录

Desktop：系统浏览器 OAuth/authorization-code + PKCE 或受控 device flow；CLI/headless：短码 + 浏览器确认。回调使用 loopback/自定义 scheme 时必须验证 state/PKCE。

Token：

- access token 短期；
- refresh token 绑定 device，存 keyring；
- 下载 token/Pack URL 短期、scope 限定；
- 公共 MCP token 有独立 audience；
- 本地 ProductionGrant 不是网站 token；
- token 永不写普通 config/日志/Bundle。

### 6.3 离线

- 已安装、许可允许的本地能力可离线工作；
- 网站无法访问时不阻止 Vault read/CLI local produce；
- 需要在线公共 MCP/下载/配额的操作明确失败；
- 若未来商业 license 需要离线凭证，使用签名 entitlement snapshot + 合理 grace，并明确哪些能力受限；
- 不远程删除用户知识或已生成 Markdown。

## 7. 下载与版本目录

### 7.1 Product Artifact

服务器记录：

```json
{
  "artifact_id": "...",
  "product": "runtime|desktop|pack|offline-bundle",
  "version": "...",
  "channel": "stable|beta|nightly",
  "platform": "windows-x86_64",
  "size": 0,
  "sha256": "...",
  "signature": "...",
  "manifest_url": "...",
  "compatibility": {},
  "mirrors": [],
  "released_at": "...",
  "revoked_at": null
}
```

下载页面必须展示：版本、平台、大小、内容、依赖、许可证、校验、发布日期、已知问题和支持的更新路径。

### 7.2 渠道

- stable：默认，满足完整发布 Gate；
- beta：邀请/opt-in，允许收集显式反馈；
- nightly：开发者 opt-in，不面向普通用户承诺；
- security rollback/revoke：可标记不可再安装版本，但不删除本地数据。

### 7.3 中国大陆可达性

- 官方国内主域名/对象存储/CDN；
- 至少一个独立备用源；
- 客户端按 signed manifest 验证，镜像不成为信任根；
- 提供离线组合包和独立组件包；
- 安装过程不临时从 GitHub 拉取必需依赖；
- 断点续传、限速、重试和镜像切换；
- DNS/TLS/证书/大文件真实网络验收；
- 状态页和手动下载校验说明可达。

## 8. Pack Catalog

网站目录保存官方 Pack 的：

- manifest/signature/hash；
- capability；
- Runtime/Recipe compatibility；
- platform/arch/GPU；
- size/license/SBOM；
- privacy/network behavior；
- channel/revocation；
- mirror URLs。

Runtime 查询 catalog 只得到元数据；安装仍由本地 Pack Manager 验证。网站账号不能让 Runtime 执行未签名代码。

公共第三方 Pack 市场不在当前范围。未来若有，必须单独解决 publisher identity、审核、沙箱、权限、签名、恶意更新、许可证和兼容承诺。

## 9. 公共知识

### 9.1 Public Knowledge Package

```json
{
  "knowledge_package_id": "pk_...",
  "name": "...",
  "version": "...",
  "publisher": "...",
  "license": "...",
  "language": ["zh-CN"],
  "topics": [],
  "schema_contract": {},
  "bundle_manifest": "...",
  "sha256": "...",
  "signature": "...",
  "download_policy": "free|entitled",
  "remote_mcp_endpoint": "...",
  "updated_at": "..."
}
```

### 9.2 两种消费方式

#### 拉取到本地

- 下载签名知识包；
- validate/import 到用户选择的 common/package 范围；
- 保留 publisher/license/version；
- 可离线读；
- 更新产生新版本，不覆盖用户 personal 编辑；
- 卸载公共包不删除用户从中派生的 personal 知识，需显示断链影响。

#### 远端 MCP

- Streamable HTTP；
- 只读；
- token audience/scopes；
- 配额/订阅；
- 明确 freshness/版本；
- 不访问本地 Vault；
- 结果标明 public source/publisher/license。

### 9.3 公共和个人隔离

- 数据库、object prefix、授权 scope 和审计分开；
- 用户 personal Source/Draft 不上传为“公共知识”；
- common 发布不是自动上传网站；
- 未来用户投稿需独立投稿/审核/许可合同，不能复用 personal Publisher 的本地确认。

## 10. 云端数据模型

关系数据库适合控制面事务，表域建议：

```text
identity:
  accounts, login_identities, sessions, recovery, audit_security

access:
  invites, invite_redemptions, entitlements, plans, subscriptions, quotas

devices:
  devices, device_keys, device_sessions, revocations

distribution:
  releases, artifacts, mirrors, manifests, compatibility, rollouts

packs:
  pack_versions, capabilities, licenses, sboms, revocations

public_knowledge:
  publishers, packages, package_versions, licenses, mcp_endpoints

operations:
  service_usage_aggregates, support_cases, consent_records, audit_events
```

大二进制放对象存储/CDN，数据库只存 manifest/metadata。个人 Markdown、Prompt、Transcript、Cookie、API Key、本地路径、Job checkpoints 不进入这些表。

## 11. API 边界

### 11.1 Account API

注册/验证/登录/会话/恢复/删除/locale。所有高风险操作重新认证。

### 11.2 Device API

bind/list/rename/revoke/rotate key；返回最少设备状态。

### 11.3 Distribution API

channel manifest、artifact metadata、signed download authorization、rollout/rollback。客户端可以匿名获取 stable public manifest；beta/private artifact 需 entitlement。

### 11.4 Catalog API

Pack/知识包搜索、详情、版本、compatibility、license。下载与远端 MCP scope 分开。

### 11.5 不提供

- upload personal vault；
- arbitrary local file；
- local job database；
- provider credential sync；
- remote command execution on device；
- 后台读取设备 Vault。

## 12. Runtime/网站交互

```text
alltonote account login
alltonote account status
alltonote account logout
alltonote device list|rename|revoke
alltonote update check
alltonote pack search|install
alltonote public-knowledge search|pull|connect
```

Runtime 在网络失败时返回云能力不可用，但不影响：

- `vault read/search`；
- 已安装 Recipe 的离线/自有 Provider 生产；
- Review/Publisher 到本地 personal；
- Knowledge MCP 本地 stdio。

## 13. 安全

- 成熟身份框架、MFA/Passkey、rate limit、credential stuffing 防护；
- invite token hash、一次性/限次/到期；
- CSRF/XSS/CSP/session fixation/open redirect 防护；
- authorization 以 server-side entitlement，不信客户端声明；
- device public key、PKCE、state、token audience；
- refresh token rotation/revocation；
- signed manifest/artifact/hash/SBOM；
- 镜像和 CDN 不是信任根；
- 管理后台最小权限、强 MFA、审计；
- 公共 MCP 按账号/计划/endpoint rate limit；
- object storage 私有 bucket + 短期 URL；
- 不在 analytics/support/log 记录 token、正文或完整敏感 query；
- 数据备份、恢复、密钥轮换、事故响应另有运维 Runbook。

## 14. 隐私与合规 Gate（中国大陆）

本节是工程 Gate，不替代律师意见。上线前必须由合格法律/合规人员依据实际业务确认：

- 网站/域名/服务器上线所需备案与许可；
- 账号信息、手机号/邮箱、设备、日志、支付数据的告知、同意、最小化、保存和删除；
- 跨境数据和第三方服务；
- 未成年人策略；
- 公共知识/视频/文章/PDF 的版权和许可；
- 算法/生成式 AI 服务是否触发额外义务；
- 网络安全、等级保护或其他适用要求；
- 用户协议、隐私政策、开源许可证和退款/订阅规则。

工程上先做到：

- region-aware deployment；
- 数据清单与处理目的；
- consent version 记录；
- 最小日志；
- 数据导出/删除；
- 第三方处理者清单；
- 不默认上传个人知识；
- 公共知识 license machine-readable；
- feature flag 可在合规未完成时关闭相关服务。

## 15. 可用性与性能

- 首页、登录、下载、状态在中国大陆真实网络测量；
- 登录/下载不依赖不可达第三方脚本；
- 静态页面/CDN 缓存；
- release manifest 小且可缓存，签名验证本地完成；
- 大文件多源/断点续传；
- 公共 MCP 有明确 latency/availability/quotas，不影响本地 read；
- 邮件/SMS provider 有失败监控与备用策略；
- 网站功能降级时保留离线下载说明和校验；
- 控制面数据库和对象存储备份恢复定期演练。

## 16. 测试矩阵

### 16.1 Invite/account

- 到期/限次/并发兑换/撤销；
- 账号重复/验证/恢复/删除；
- invite -> entitlement；
- 开放注册 feature flag；
- rate limit/枚举防护；
- session/MFA/Passkey。

### 16.2 Device

- Desktop/CLI device flow；
- token/key 存 keyring；
- revoke/rotate/reinstall；
- 多设备上限；
- 离线；
- account deletion；
- token audience 不能互用。

### 16.3 Distribution

- 签名/hash/manifest；
- 镜像篡改；
- 断点续传；
- rollout/rollback/revoke；
- beta entitlement；
- 离线 bundle；
- Windows/macOS compatibility；
- 中国大陆至少两条真实下载链路。

### 16.4 Public knowledge

- package license/version/signature；
- pull/import/update/uninstall；
- remote MCP OAuth/audience/quota；
- 个人/公共隔离；
- endpoint failure 本地知识仍可用；
- 不含 personal data 的数据库/对象审计。

### 16.5 安全/隐私

- OWASP Web/API 基线；
- invite/session/token/download abuse；
- XSS/CSRF/open redirect；
- object authorization；
- admin audit；
- data export/delete；
- logs/analytics/backup 内容审计；
- 不上传 Vault 的自动化回归。

## 17. 分期

### Phase S0：静态官网与公开下载

产品说明、文档、公开 stable manifest、签名下载、离线说明；不需要账号也可下载公开基础版。

### Phase S1：账号与邀请

account、invite redemption、beta entitlement、基础安全/审计；不接个人知识。

### Phase S2：设备与受控渠道

device key/session、beta download、Runtime account CLI、revoke/offline。

### Phase S3：Pack Catalog

官方 Pack manifest/compatibility/license/SBOM 与镜像。

### Phase S4：公共知识包

签名 package 目录、pull/import/update/license。

### Phase S5：公共远端 MCP

Streamable HTTP、OAuth audience、quota/subscription，与本地 Vault 完全隔离。

### Phase S6：开放注册/商业化

根据产品数据移除 beta invite gate，增加计划/订单/配额；不改变本地开放数据基线。

## 18. 完成定义

1. 邀请只授予 entitlement，不是长期身份或本地知识钥匙；
2. 可从邀请平滑切换开放注册；
3. 网站数据库不含个人 Markdown/Transcript/Prompt/Secret/Job；
4. 账号/网站故障不锁死本地 Vault 和基础 Runtime；
5. 设备使用本地私钥 + server public key，可撤销；
6. Runtime/Desktop/Pack/知识包都通过签名 manifest 分发；
7. 中国大陆有不依赖 GitHub/Google 的登录、下载和更新链路；
8. 公共知识支持本地 pull 和远端只读 MCP；
9. 公共和个人授权/存储完全隔离；
10. 安全、隐私、合规、备份恢复和真实网络 Gate 通过后才生产上线。
