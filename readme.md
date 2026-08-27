# Enterprise QA Agent

企业级 RAG 问答 Agent，多租户 SaaS 形态，面向 10000+ 员工的大企业。

> **状态：开发中（2026-08-25）。当前阶段为"停止新增功能、先补设计"。**

## 从哪里开始

| 你想 | 读这个 |
|---|---|
| **了解项目全貌 / 参与开发** | **[`CLAUDE.md`](CLAUDE.md)** —— 唯一事实来源，包含定位、未闭环、硬性规则 |
| 看架构图与核心链路 | [`docs/architecture.md`](docs/architecture.md) |
| 了解性能现状与目标 | [`docs/architecture.md`](docs/architecture.md) §3、[`docs/scale_slo_and_priorities.md`](docs/scale_slo_and_priorities.md) |
| 了解开发流程与协作规范 | [`docs/collaboration_retrospective.md`](docs/collaboration_retrospective.md) |
| 查历史设计（已冻结） | [`docs/archive/`](docs/archive/) |

## 技术栈

FastAPI + LangGraph · React + Vite · PostgreSQL · ChromaDB + BM25 · 本地 Ollama
（`qwen2.5:7b` 生成 + LoRA 微调的 `qwen2.5-1.5b-router` 意图路由）

## 本地运行

需要 PostgreSQL、Ollama，以及 `.env`（参考 [`.env.example`](.env.example)）。

> ⚠️ **`RAGENT_JWT_SECRET` 必须设置**，否则服务拒绝启动
> （本地开发可用 `RAGENT_DEBUG=true` 走内置开发密钥）。

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # 生成密钥
```

## 已知重要缺陷

见 [`CLAUDE.md`](CLAUDE.md) §4「已知未闭环」——**上线前必须处理**，
其中包含 3 条会产生错误答案或阻塞承载的 P0。
