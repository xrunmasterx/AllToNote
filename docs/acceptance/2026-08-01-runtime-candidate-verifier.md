# Runtime 候选物只读完整性验证

日期：2026-08-01

状态：本地发布工具 Gate 通过；这不是签名、来源证明或公开发布认证。

## 目标

Windows Runtime 目录候选物在组装完成后，还会经历复制、杀毒扫描和向测试机转移。
本 Gate 使用发布记录中独立保存的 `release/file-manifest.json` SHA-256 作为信任锚，
在不执行候选程序、不访问网络、不写入候选目录的前提下，重新核验完整文件树。

验证内容：

- 清单是无重复键、无非有限数字的 canonical JSON；
- 清单路径、顺序、大小、SHA-256 和文件集完全一致；
- 文件和目录不允许 symlink、junction/reparse 或多硬链接文件；
- 遍历错误 fail closed，不把无权读取的子树当作空目录；
- `runtime-inputs.json`、`wheelhouse-lock.json` 和 `acceptance.json` 的再次读取字节
  重新绑定到已核验的文件树行，拒绝混合快照；
- Runtime 平台、源码提交、candidate-pass、WAL Gate 和
  `parallel_job_execution_enabled=false` 保持一致；
- JSON 成功结果不包含候选物绝对路径。

## 命令

```powershell
python -m tools.runtime_windows_release verify `
  --candidate <runtime-directory> `
  --expected-manifest-sha256 <64-lowercase-hex> `
  --expected-source-commit <40-lowercase-hex>
```

`--expected-manifest-sha256` 必须来自候选物之外的受控发布记录；从待验证目录现场计算后
再传回命令只能检查内部一致性，不能证明交付物身份。

## V10 复核结果

- 候选物：`runtime-portable-engine-v10`
- Runtime 源码提交：`54019ea58a280dea6b508044fc0dbe0558684203`
- 清单 SHA-256：`c53f4ea579f631383eabafe6f65e199d3e06e4f67f3b98aeebcf197718b48142`
- payload 文件：704
- payload 字节：40,803,999
- 结果：pass

自动化证据：Runtime 发布工具聚焦测试 31 项通过；四个发布工具联合回归 52 项通过；
全量后端回归 `2537 passed, 3 skipped, 3 subtests passed`。

## 尚未解除的边界

该 Gate 只证明“受控清单所绑定的候选字节在本次读取时一致”。它不证明 Builder Python/pip
可信，也不证明 wheel 安装结果完全来自锁定 wheel；下一切片必须新增 build-provenance Gate，
并只对新组装候选物出具证明，不能追认 V10。公开发布仍需要 clean non-admin VM、Defender、
完整 Runtime SBOM/license、稳定 installer/discovery、签名以及同一正式 artifact 的
Video/Document E2E。
