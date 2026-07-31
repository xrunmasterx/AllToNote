# Document Pack 可信快速 no-op 验收

日期：2026-07-31

范围：`document-basic@docling-2.117.0-tableformer-v2.3.0` 已由正式信任根验证并激活后，再次执行普通 `pack install` 的幂等路径。

## 结论

真实 1.58 GiB 级签名 Pack 的重复安装从历史基线 `153.196 s` 降至 `39.840 s`，耗时下降约 `74.0%`。命令返回 `result=already_active`、退出码 `0`，`active.json` 的字节与修改时间均保持不变。

本优化没有引入缓存索引、mtime 信任、Merkle 清单或第二套 Pack 状态：

- 来源目录只稳定读取并认证规范化 `manifest.json`，校验完整 manifest contract、平台、Runtime/Recipe 兼容性、许可证/SBOM 声明及 Ed25519 官方签名；
- 来源 manifest digest 必须与当前有效 `active.json` 完全一致；
- 实际继续使用的 managed generation 仍通过现有 generation verifier，逐文件校验精确文件集、链接/reparse/hardlink 边界、每个 payload 的长度和 SHA-256，以及规范化 verified receipt；
- generation 校验完成后再次读取 `active.json`，只有指针未漂移才返回 `already_active`；
- no-op 不创建 staging、不复制来源 payload、不重写 receipt/active，也不重复运行动态 doctor；
- 显式 `--repair`、首次安装、缺失 generation、无效 active、不同 digest 和其他前置条件失败仍走既有完整事务或 fail-closed。

no-op 的语义是“认证用户提供的来源身份，并重新验证当前实际使用的安装内容”，不是“验证来源介质的每个 payload 字节”。如果需要验证运行时依赖和模型可执行健康状态，继续显式运行：

```text
alltonote pack doctor document-basic --dynamic --json
```

## 真实测量

输入：

- 来源：正式签名目录 `document-basic-signed-v1`
- manifest：`sha256:7b72fe809a18ca62a2d7d80122a8350314f6bd9b8a4c56dba16ec17725e90d0f`
- 当前 active：同一 manifest digest 的 managed generation
- 命令：`alltonote pack install document-basic --source <signed-pack> --json`

结果：

| 项目 | 结果 |
|---|---|
| 退出码 | `0` |
| CLI protocol | `1` |
| command | `pack install` |
| result | `already_active` |
| 用时 | `39.840 s` |
| active 字节 | 未变化 |
| active mtime | 未变化 |
| staging / 来源复制 | 未发生 |
| managed generation | 完整逐文件复验通过 |

历史同一机器、同一 Pack 的旧实现重复安装为 `153.196 s`；该基线会重新物化来源、重复校验大体量树并运行 doctor。新路径保留最终使用对象的完整内容校验，只删除与 no-op 结果无关的来源复制和动态执行。

## 自动化 Gate

- RED：新增 manifest-only API 和 installer fast path 前，测试因缺少 API 而在收集阶段失败；
- GREEN：verifier、installer、Pack CLI 聚焦测试 `62 passed`；
- 关联回归：Pack verifier/installer/resolver/trust、Docling worker launcher、Pack CLI、Runtime bootstrap/info 共 `116 passed`；
- 独立只读 Gate：PASS；要求仅普通 install 使用 fast path，`--repair` 保持完整慢路径，且返回前复读 active 指针；
- `git diff --check`：通过，仅有仓库既有的 Windows 换行提示。

## 边界

该验收证明本地受管理 Pack 的逻辑激活和重复安装性能边界，不扩大为对恶意同用户持续篡改、掉电持久化、独立 Windows 安装器、Authenticode 或完整公开发行的证明。任何有限时刻的文件校验都无法阻止同权限进程在校验结束后再次改写 managed state；这一威胁边界与优化前一致。
