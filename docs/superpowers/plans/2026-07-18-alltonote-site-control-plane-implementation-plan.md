# AllToNote 网站控制面实施计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-18-alltonote-site-control-plane-design.md
  - ../specs/2026-07-18-alltonote-platform-release-design.md
implementation_status: not-started
last_verified_at: 2026-07-18
```

## 1. 开始条件

至少满足：Windows 内测 artifact可下载、Runtime/Pack signed manifest稳定、账号不会决定本地Vault可读、数据清单/合规负责人明确。网站可先做静态官网，但账号/公共服务不应抢在本地闭环前成为主工程风险。

## 2. 成功标准

邀请->账号->beta entitlement；设备安全绑定/撤销；国内双源签名下载；Pack catalog；公共知识 pull；远端只读 MCP；数据库/对象存储无个人正文；网站故障不影响本地能力；安全/隐私/合规/备份恢复Gate。

## 3. Task SITE-00：技术选型 ADR

基于中国大陆部署、PostgreSQL事务、对象存储/CDN、成熟认证、MCP Streamable HTTP、可观测/备份，比较自托管与托管方案。验证数据驻留、域名/备案、供应商可用性、成本/锁定/迁移。写 ADR，不因前端 Sites/静态托管工具而假设其有完整生产数据库/身份/设备/配额能力。

## 4. Task SITE-01：Cloud Data Boundary Test

先建立 schema allowlist和自动测试：任何 API/表/对象不得接受 personal Markdown、Transcript、Prompt、Cookie、API Key、本地path、Job checkpoint。用canary payload证明被拒绝/日志不保留。

## 5. Task SITE-02：静态官网/文档/公开 manifest

产品边界、下载、校验、隐私、支持矩阵、known issues；公开 stable signed manifest/API；国内主域+备用镜像；不登录也可下载公开基础版。Sites可用于此展示层，但发布产物信任来自签名manifest。

## 6. Task SITE-03：Account/Auth Foundation

成熟框架；email verification/password/passkey roadmap；sessions/refresh rotation/MFA/admin；rate limit/account enumeration/CSRF/XSS；locale/timezone；export/delete。不要Google/GitHub-only。

## 7. Task SITE-04：Invite/Entitlement

invite hash/campaign/expiry/max uses/concurrency transaction/redemption audit；兑换授予beta_access；feature flag控制 closed/open registration；existing account不迁移。测试并发、重放、撤销、滥用。

## 8. Task SITE-05：Device Binding

Runtime本地key pair、public key/fingerprint、authorization-code+PKCE/device flow、keyring token、list/rename/revoke/rotate/reinstall。最小OS/arch/version，无硬件序列指纹。token audience/scopes。

## 9. Task SITE-06：Distribution Service

release/artifact/mirror/compatibility/rollout/revoke schema；对象存储短期URL；signed manifest；beta entitlement；断点/多源；Runtime update check。下载server不生成信任，client验签/hash。

## 10. Task SITE-07：国内可达/离线

两条独立下载路径、DNS/TLS/CDN大文件、断点、限速/丢包、镜像故障；offline bundle/校验；安装无GitHub/pip临时依赖。记录真实省份/运营商样本时注意隐私。

## 11. Task SITE-08：Pack Catalog

pack/version/capability/platform/compatibility/license/SBOM/privacy/network/revoke；Runtime search/info/install；签名验证；无第三方市场/动态任意代码。

## 12. Task SITE-09：Public Knowledge Package

publisher/license/version/manifest/signature/object；catalog/search/detail/download；Runtime pull/validate/import/update/uninstall；不覆盖personal。建立版权/许可审核流程和撤回策略。

## 13. Task SITE-10：Remote Public MCP

独立只读服务/DB权限；Streamable HTTP；MCP Authorization/OAuth audience；resources/tools同Knowledge read模型；quota/rate limit/version/freshness/license；不接受local path，不连接personal Vault。

## 14. Task SITE-11：Subscription/Quota（需求后）

plan/entitlement/quota ledger/idempotent usage/订单支付/退款/发票需独立产品与合规设计。基础本地能力和Vault read不依赖online quota。未定商业模式前只实现抽象entitlement，不填虚构价格。

## 15. Task SITE-12：Admin/Support

最小管理后台、强MFA/RBAC/audit；invite/release/pack/package/revoke；support bundle只接收用户主动上传的脱敏诊断，不远程读设备/Vault。

## 16. Task SITE-13：Privacy/Compliance Gate

由合格人员确认备案/许可、个人信息、跨境、未成年人、版权、AI服务、支付、协议；工程完成数据清单/consent version/retention/export/delete/third-party registry/feature kill switches。未通过不生产开放。

## 17. Task SITE-14：Security/Operations

threat model、OWASP Web/API、penetration test、dependency/SBOM、secret management、WAF/rate limit、backup/PITR/restore drill、key rotation、incident runbook、status page、SLO/alerts。日志不含正文/token。

## 18. Task SITE-15：Account/Runtime E2E

```text
invite -> account -> device bind
 -> beta signed Runtime/Pack download
 -> offline local Vault/produce
 -> website outage, local still works
 -> public package pull
 -> remote public MCP
 -> device revoke/token fail
 -> local Vault remains readable
 -> account delete, server data removed/retained per policy
```

## 19. Task SITE-16：开放注册迁移

用feature flag/entitlement issuance切换closed beta -> open beta；邀请码退回活动用途；验证existing account/device/subscription/knowledge package不迁移。

## 20. 验收

输出 `docs/acceptance/site-control-plane-v1.md` 和公开隐私/数据边界文档。只有production域名、国内网络、安全/合规/恢复真实通过才标CLOUD-01完成；静态官网完成不能冒充账号/公共MCP完成。

## 21. 顺序

```text
SITE-00/01
 -> SITE-02
 -> SITE-03/04
 -> SITE-05/06/07
 -> SITE-08
 -> SITE-09/10
 -> SITE-12..15
 -> SITE-11/16（产品时机）
```
