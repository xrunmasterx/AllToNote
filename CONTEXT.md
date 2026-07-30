# AllToNote 领域词汇

本表只固定跨文档容易混淆的领域边界；实现细节仍由对应设计与 ADR 拥有。

| Term | Definition | Avoid |
|---|---|---|
| Production | 用户把一种来源转换为可积累知识的完整用例。 | 把某个解析库或文件格式称为独立 Production。 |
| Recipe | AllToNote 内部版本化的生产扩展单位；Video、Document、Article 等是并列 Recipe。 | 把 Recipe 当成第二套 Runtime、CLI 或公开插件 ABI。 |
| Document Domain Kernel | AllToNote 对文件身份、DocumentBlock、Document Evidence、质量、Artifact 与恢复语义的所有权。 | 用第三方解析器对象充当领域模型或持久化合同。 |
| Document Parsing Engine | 把受支持文档转换为候选结构、文本与位置的可替换工具。Docling 属于这一层。 | 把解析成功等同于知识成品完成。 |
| Document Evidence | 由 AllToNote 绑定原文件完整 SHA-256、页码、bbox、内容哈希与提取 basis 的来源定位。 | 使用解析器内部对象 ID 作为跨版本权威身份。 |
| Feature Pack | 固定版本、可探测、可独立安装或隔离执行的重能力集合。 | 把重解析/OCR/模型依赖塞进最小 Runtime 冷启动路径。 |
