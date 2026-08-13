# 企业智能问答系统 (Enterprise QA Agent)

RAG + 单 Agent 架构的企业级智能问答系统骨架：既能检索企业文档知识库，也能查询结构化业务数据，支持多 LLM Provider 切换。

## 架构

```
frontend/   React + TS + Vite：对话页 + 知识库管理页
backend/    FastAPI：ingestion / vectorstore / agent(LangGraph) / api
```

- **Agent 编排**：LangGraph（`backend/agent/graph.py`），ReAct 风格单 agent，两个工具：
  - `retrieve_documents`：pgvector 相似度检索企业文档，返回带来源引用的片段
  - `query_structured_data`：text-to-SQL，针对白名单业务表（`customers`/`orders`）生成只读 SQL 并用独立的只读数据库角色执行
- **LLM**：`backend/llm/provider.py`，通过 `LLM_PROVIDER`（`anthropic` / `openai` / `ollama`）+ `LLM_MODEL` 切换，每次请求也可覆盖
- **向量检索 + 结构化数据**：同一个 Postgres 实例，`pgvector` 扩展
- **Embedding**：默认本地开源模型（`sentence-transformers`，无需 API key），可在 `vectorstore/embedder.py` 切换到 Voyage/OpenAI

## 快速开始

### 1. 启动 Postgres（含 pgvector）

```bash
docker compose up -d
```

> 如果本地已有 Postgres 服务在跑，也可以不用 docker，直接在你自己的 Postgres 里
> `CREATE EXTENSION vector;` 并跑一遍 `backend/db/initdb/` 里的两个 SQL 脚本。

### 2. 后端

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp ../.env.example ../.env
# 编辑 .env：填入 ANTHROPIC_API_KEY（或改 LLM_PROVIDER=openai 并填 OPENAI_API_KEY，
# 或改 LLM_PROVIDER=ollama 走本地模型）

uvicorn main:app --reload --port 8000
```

启动时会自动建表、给只读角色授权、灌入 demo 业务数据（`customers`/`orders`）。

打开 http://localhost:8000/health 应看到 `{"status": "ok"}`。

### 3. 前端

```bash
cd frontend
npm install
npm run dev
```

打开 http://localhost:5173。

- **对话页**：流式提问，命中知识库时会展示"调用工具"折叠区
- **知识库页**：上传 PDF / DOCX / TXT / Markdown，自动解析 → 分块 → 向量化 → 入库，状态会自动轮询刷新

## 现状与已知限制（下一步要做的事）

- 鉴权只是占位（当前无登录校验），后续需要接入企业 SSO/OAuth；`backend/core` 是接入中间件的位置
- 完整 RBAC / 多租户 / 审计日志尚未实现
- 结构化数据的 demo schema（`customers`/`orders`）需要替换成真实业务表；替换后同步更新
  `backend/db/session.py:SQL_TOOL_WHITELISTED_TABLES` 和 `backend/agent/tools.py:_SCHEMA_DESCRIPTION`
- Embedding 默认本地模型，中文效果一般；检索质量不够时可切换到 Voyage/OpenAI（改 `vectorstore/embedder.py` 一个文件）
- 还没有可观测性（tracing）和评估集，先用日志顶着
- 文档摄取目前是 FastAPI `BackgroundTasks`（进程内），文档量大或多副本部署时需要换成任务队列（Celery/RQ）
