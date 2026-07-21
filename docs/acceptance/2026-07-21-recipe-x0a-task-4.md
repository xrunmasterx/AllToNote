# Recipe X0-A Task 4 验收报告

> 日期：2026-07-21
> 状态：PASS
> 任务：静态 Registry 与薄 ProduceService

## 实现

- `RecipeRegistry` 只在构造时显式接收 `(RecipeDescriptor, RecipeEndpoint)`；
- 构造后使用 tuple 和只读 lookup，无 register/freeze/global singleton；
- list 按 `(recipe_id, recipe_version)` 稳定排序；
- invalid selector、unknown ID/version、duplicate key 与 invalid endpoint 使用稳定错误；
- metadata list/describe 不访问 endpoint 实例；
- `ProduceService` 仅校验 typed envelope、resolve、submit、验证最小 submission 并返回；
- endpoint 与 Registry 错误原样传播；
- 不导入 Video，不创建线程、queue、worker、Result、Plan 或资源模型。

Selector 在本层明确为 `RecipeKey`；`id@version` 字符串解析属于后续 CLI/request decoder，不进入 Registry。

## 审查修复

对抗审查发现 Registry 原实现允许 endpoint 为 `None`，会把错误推迟为 `AttributeError`。已改为在构造期通过类级 `submit` 检查 fail closed，同时不访问 endpoint 实例属性，保持 metadata cold path 无副作用。

## 测试

专项 Registry/Service/contract：

```text
34 passed
```

Task 4 兼容集：

```text
105 passed
```

全量 Backend：

```text
1861 passed, 2 skipped, 1 warning, 3 subtests passed
```

cold-import tests 证明 registry/service 不加载 Video、Runtime、FastAPI、Torch、Whisper、Downloader、模型 client 或 SQLAlchemy，也不创建新线程。

## 下一步

Task 5：VideoRecipeAdapter。Adapter 必须确定性映射现有 v1/v2 字段并继续调用 `VideoService.submit_video`；不得直接调用 JobService、修改 schema/hash/config snapshot 或创建通用 Result。
