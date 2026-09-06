# 发版手册（Release Manager）

本文档面向**发版执行者**，描述 AllToNote 仓库已有 Web、浏览器扩展与 Tauri 发布流程。
2026-09-06 核验时远端主线为 `master`，没有 `develop`；发布分支从最新 `origin/master` 创建。
Runtime 目录候选及签名 Pack 的验收另见 [`docs/acceptance/`](./docs/acceptance/)，
不能把扩展或桌面发布流程当作 Runtime 已公开发布的证明。日常提交规范见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

---

## 流程总览

```
origin/master → release/X.Y.Z → PR → master → tag vX.Y.Z
                                              ↓
                                CI 构建产物并上传 GitHub Release
                                              ↓
                                人工上传商店（Chrome/Edge/Firefox）
```

---

## 1. 切发布分支

```bash
git fetch origin
git checkout -b release/X.Y.Z origin/master
```

版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)：`MAJOR.MINOR.PATCH`。

## 2. 写 CHANGELOG，更新版本号

在 `release/X.Y.Z` 上：

- 编辑 [`CHANGELOG.md`](./CHANGELOG.md)，新增 `## [X.Y.Z] - YYYY-MM-DD` 段，按 Keep a Changelog 分类（Added / Changed / Fixed / Removed / Security / Internal）
- 编辑 [`README.md`](./README.md) 顶部标题中的版本号 + 新增"vX.Y.Z 新增"摘要段
- 重大变更也同步更新 [`CLAUDE.md`](./CLAUDE.md)

```bash
git commit -am "docs: vX.Y.Z CHANGELOG + README 版本"
git push -u origin release/X.Y.Z
```

## 3. 合并到 master

在 GitHub 上发起发布 PR：

| PR | base | 合并方式 | 合并后 commit 标题 |
|---|---|---|---|
| `release/X.Y.Z` → `master` | `master` | **Merge commit (--no-ff)** | `chore(release): vX.Y.Z` |

> ⚠️ Merge commit 的标题**必须**符合 `type(scope): subject` 格式（commitlint 在 push 到 master/develop 时会校验）。
> 历史上用过 `Release vX.Y.Z` 这种形式，会被 commitlint 报 `type-empty` / `subject-empty`。

合并前满足当前仓库实际配置的分支保护、review 和 CI 要求；不向不存在的 `develop` 回灌。

## 4. 打 tag

```bash
git checkout master && git pull origin master
git tag -a vX.Y.Z -m "BiliNote vX.Y.Z

主线：
- ...

详见 CHANGELOG.md"
git push origin vX.Y.Z
```

push tag **会自动触发 [`.github/workflows/release-extension.yml`](.github/workflows/release-extension.yml)**：构建插件并把 `.zip` / `.xpi` / `.crx` 挂到对应 GitHub Release。

## 5. 创建 GitHub Release（如果还没有）

CI 默认会创建 / 更新 `vX.Y.Z` 对应的 Release。如果你想自己写 release notes：

1. 打开 https://github.com/xrunmasterx/AllToNote/releases/new
2. Tag: 选 `vX.Y.Z`
3. Title: `vX.Y.Z`
4. Body: 直接贴 [`CHANGELOG.md`](./CHANGELOG.md) 的对应段
5. CI 跑完后 Release 页面会自动出现 `bilinote-extension-X.Y.Z.zip` / `.xpi` / `.crx`

## 6. 上传到各商店（人工）

商店审核普遍 1-3 个工作日。建议**先上 Chrome → Edge → Firefox**（Edge 接受同一份 zip）。

### Chrome Web Store

1. https://chrome.google.com/webstore/devconsole
2. 选 BiliNote → 左侧 **Package** → **Upload new package**
3. 上传 `bilinote-extension-X.Y.Z.zip`
4. 检查 listing（描述 / 图标 / 截图无变化可保持），点 **Submit for review**

### Microsoft Edge Add-ons

1. https://partner.microsoft.com/dashboard/microsoftedge
2. 选 BiliNote → **New submission**
3. 上传同一份 `.zip`（Edge Add-ons 与 Chrome 完全兼容 MV3）
4. 提交审核

### Firefox Add-ons (AMO)

1. https://addons.mozilla.org/developers/
2. 选 BiliNote → **Upload New Version**
3. 上传 `bilinote-extension-X.Y.Z.xpi`
4. 选择"在 AMO 公开"或"自托管"
5. 提交审核

### 桌面端 (Tauri)

仓库已有 GitHub Actions 在 `v*` tag 时构建桌面端安装包并自动挂到 GitHub Release，无需额外操作。

## 7. 清理

```bash
# release 分支已合到 master，删掉
git push origin --delete release/X.Y.Z
git branch -d release/X.Y.Z
```

---

## 自动发布到商店（可选）

`.github/workflows/release-extension.yml` 末尾有三段商店自动发布的 job 注释。要启用：

1. 在 https://github.com/xrunmasterx/AllToNote/settings/secrets/actions 加 secrets：

| 商店 | 需要的 secret |
|---|---|
| Chrome | `CHROME_EXTENSION_ID`、`CHROME_CLIENT_ID`、`CHROME_CLIENT_SECRET`、`CHROME_REFRESH_TOKEN` |
| Edge | `EDGE_PRODUCT_ID`、`EDGE_CLIENT_ID`、`EDGE_API_KEY` |
| Firefox | `FIREFOX_ADDON_UUID`、`FIREFOX_API_KEY`、`FIREFOX_API_SECRET` |

2. 解开 workflow 文件末尾的 `publish-chrome` / `publish-edge` / `publish-firefox` job 注释。
3. 推 tag 时即自动发布。

> Chrome 各 secret 的获取方式：[chrome-webstore-upload-cli 文档](https://github.com/fregante/chrome-webstore-upload-cli/blob/main/How%20to%20generate%20Google%20API%20keys.md)
> Edge：[Edge Add-ons API](https://learn.microsoft.com/en-us/microsoft-edge/extensions-chromium/publish/api/using-addons-api)
> Firefox：https://addons.mozilla.org/en-US/developers/addon/api/key/

---

## 紧急 hotfix 发版

线上紧急问题不走 `release/*`，走 `hotfix/*`：

```bash
git checkout master && git pull
git checkout -b hotfix/<scope>-<事项>
# … 修复 ...
# PR base=master 合入
```

合入 master 后通常打 patch tag（如 `v2.1.1`），CI 流程同上。

---

## 历史发布快查

| Version | Date | Tag |
|---|---|---|
| 2.1.0 | 2026-05-07 | [`v2.1.0`](https://github.com/JefferyHcool/BiliNote/releases/tag/v2.1.0) |
| 2.0.0 | (上游 web 端 v2.0.0) | [`v2.0.0`](https://github.com/JefferyHcool/BiliNote/releases/tag/v2.0.0) |
