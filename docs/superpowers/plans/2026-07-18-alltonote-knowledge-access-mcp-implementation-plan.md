# AllToNote Knowledge Access MCP 实施计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-18-alltonote-knowledge-access-mcp-design.md
  - ../specs/2026-07-13-alltonote-cli-first-vault-workspace-design.md
implementation_status: not-started
last_verified_at: 2026-07-18
```

## 1. 前置与成功标准

前置：Vault Core/CLI 的 inspect/tree/read/search/grant 已通过，不依赖 Desktop/Engine。

完成标准：两个真实 MCP Host 可通过 stdio inspect/search/read；默认只读 published；无 raw/write/produce/publish；结果有界、可分页、无绝对路径；可选 provenance token 只读最小 Evidence；10k 性能与恶意路径/内容通过。

## 2. Task KMCP-00：锁定 MCP SDK/Protocol

1. 选择官方维护、支持目标 Python 与协议版本的 MCP SDK；
2. 锁定版本与 license；
3. 用最小 echo Server 验证 stdio framing、initialize、tools/resources；
4. stdout 不能有普通日志；
5. 确认两个目标 Host 的注册格式和协议兼容；
6. 不因 SDK 支持 experimental Tasks 就引入；
7. 建立 protocol contract fixture。

## 3. Task KMCP-01：KnowledgeGrant

目标文件：

- `backend/app/core/domain/grants.py`（复用 Vault grant，必要时扩展）
- `backend/app/core/application/grant_service.py`
- machine grant store
- tests

步骤：

1. audience/spaces/path prefixes/capabilities/provenance/expiry；
2. create/list/show/revoke CLI；
3. 默认 `wiki/personal`，common 由用户选择；
4. raw-read 不在通用 MVP；
5. grant ID 无法扩张 scope；
6. 权限错误不泄露存在性；
7. 多 Host 分 grant。

## 4. Task KMCP-02：稳定 Document ID/URI/Token

1. document ID 从 iwiki/workspace identity 投影，不用绝对 path；
2. 定义 `alltonote://` resource URI；
3. heading/range/cursor token 有版本、workspace/document binding 和完整性；
4. token 不含路径/正文；
5. content hash 变化时旧 range token明确 stale；
6. URI parse/property tests；
7. 跨 workspace/document/token tamper 拒绝。

## 5. Task KMCP-03：只读 Application Facade

目标文件：

- `backend/app/core/application/knowledge_access.py`
- tests

Facade 只聚合现有 Vault services：

- inspect；
- tree；
- lexical search；
- read range；
- outline；
- backlinks。

每次调用先 grant filter；结果统一 content revision/trust/truncation/cursor。不要让 MCP adapter 自己读文件或 SQL。

## 6. Task KMCP-04：stdio Server 骨架

目标文件建议：

- `backend/app/mcp/knowledge/server.py`
- `backend/app/mcp/knowledge/tools.py`
- `backend/app/mcp/knowledge/resources.py`
- `backend/app/mcp/common/errors.py`
- `backend/tests/mcp/`

步骤：

1. `alltonote mcp knowledge --grant ...`；
2. startup inspect/grant/contract；
3. initialize capability；
4. stderr structured diagnostic、stdout protocol only；
5. stdin EOF/cancel 后退出；
6. 无 daemon/port；
7. error mapping；
8. process timeout/leak test。

## 7. Task KMCP-05：Tools v1

逐个测试/实现：

1. `knowledge_workspace_inspect`；
2. `knowledge_tree`；
3. `knowledge_search`；
4. `knowledge_read`；
5. `knowledge_outline`；
6. `knowledge_backlinks`。

对每个工具验证 input JSON Schema、limit/cursor、scope、content hash、trust metadata、超大结果和稳定 errors。

## 8. Task KMCP-06：Resources v1

1. document/metadata/outline resources；
2. resources/list pagination；
3. resources/read bounded；
4. 未授权 URI 与不存在一致；
5. 不返回控制文件/raw/path；
6. content MIME/encoding；
7. 大正文引导使用 range/read tool。

## 9. Task KMCP-07：Provenance Range

依赖 Review/Portable Evidence navigation。

1. PublishedDocument 返回 opaque `evidence_token`；
2. token 绑定 document/content hash/evidence/workspace/grant；
3. `knowledge_evidence_read`；
4. Video time range、Web block、PDF page、Code line 的统一投影；
5. 默认最大范围；
6. 不允许枚举 raw；
7. Source privacy policy；
8. token 过期/stale/tamper tests。

若某 Source Adapter 尚未实现 range reader，只返回 capability unavailable，不直接读其私有文件。

## 10. Task KMCP-08：索引一致性

1. search 返回 index generation/content hash；
2. read 读当前文件；
3. stale/overflow 行为；
4. index missing 时 inspect/tree/read 可用；
5. rebuild command 在 MCP 外由用户触发；
6. MCP 不启动远端 embedding；
7. grant filter 在搜索查询与结果后双层保证。

## 11. Task KMCP-09：安全/Prompt Injection

测试：

- 文档内容要求调用写/泄密；
- HTML/script/恶意 Markdown；
- FTS injection/超长 query；
- path/URI traversal/UNC/ADS/reparse；
- 未授权 backlink/snippet/title 泄漏；
- grant revoke during session；
- malicious Host arguments；
- result byte bomb；
- stdout log pollution。

结果中的 trust metadata 固定；Server 不解释/执行知识正文。

## 12. Task KMCP-10：真实 Host E2E

至少选择当前可用的两种 Host（例如 Codex 与另一 MCP Host）：

```text
register stdio server
 -> initialize
 -> inspect
 -> search
 -> outline
 -> read selected section
 -> read one provenance range (explicit grant)
 -> attempt raw/write and fail/no tool
 -> modify document externally
 -> read current revision
 -> disconnect and verify process exits
```

保存 Host/SDK/protocol/runtime 版本与脱敏结果。

## 13. Task KMCP-11：性能

10k fixture：startup、inspect、tree、search top10、read 20k、outline、backlinks；记录 p50/p95/峰值内存。50k 做压力记录。

优化只能发生在测量后；不得通过缓存未授权数据、取消真实 hash 检查或常驻 daemon 达标。

## 14. Task KMCP-12：验收与文档

- CLI `mcp knowledge --help`；
- Host 配置示例（不含用户绝对路径/Secret，使用 grant ID）；
- security/grant/revoke 指南；
- `docs/acceptance/knowledge-mcp-v1.md`；
- master tasks `MCP-READ-01`；
- protocol/schema golden。

## 15. 后续明确不做

在本计划完成前/内不加入：semantic search、remote personal MCP、write tools、Production tools、MCP Tasks、subscriptions、Prompt catalog。它们各自需要证据和设计。

## 16. 执行顺序

```text
KMCP-00..03
 -> KMCP-04
 -> KMCP-05/06
 -> KMCP-07/08
 -> KMCP-09
 -> KMCP-10/11/12
```
