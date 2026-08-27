# 归档：历史设计文档

> **这些文档全部已实施或已废弃，仅记录"当初为什么这么决定"。**
> **不要按它们改代码——当前状态见 `CLAUDE.md` 与 `docs/architecture.md`。**
>
> 归档于 2026-08-25。按 `CLAUDE.md` §7.4 文档规则：
> 记录"当时为什么"的内容写完即冻结，不需要维护；
> 描述"现在是什么"的内容只能存在于 CLAUDE.md 与 architecture.md。

| 文档 | 状态 |
|---|---|
| `role.md` | 已实施，但权限模型此后又演进两轮（kb_group 拆分→合并）。当前模型见 `CLAUDE.md` §3 |
| `work-flow.md` / `work-flow-web.md` / `work-flow-v2.md` | 均已实施。三份讲同一件事（856 行），**未合并**——因为都已冻结，合并收益低于成本 |
| `auto-operations.md` | **部分实施**：前端监控壳子（`OperationsDashboard.jsx`）有了，自动运维内核未实现 |
| `attendance-tenant-federation.md` | **核心路由仍在使用中**（`tenant_identity_store` 接在 `builtin_tools.py:108`）。注意与知识库联邦不同 |
| `knowledge-base-tenant-federation.md` | 架构已于 2026-08-23 拆除，代码路径残留待清理 |
| `TODO.md` | 停在 2026-08-13，"未完成"条目多已解决。已被代码审计与规模重估取代 |
