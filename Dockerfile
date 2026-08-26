# 后端镜像（FastAPI + LangGraph）。
#
# 刻意不打包模型：LLM / embedding / reranker 全部走外部服务（Ollama），
# 由 RAGENT_LLM_BASE_URL / RAGENT_EMBEDDING_BASE_URL 指向。
# 把几个 GB 的权重塞进镜像会让每次构建和分发都极慢，而且模型版本
# 应该独立于代码版本演进。
#
# 两阶段构建：依赖层和代码层分开，改代码不会让依赖重装
# （206 个包，装一次几分钟）。

FROM python:3.12-slim AS deps

# psycopg/asyncpg 与若干科学计算包需要编译工具；只在构建阶段装，不进最终镜像
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock .

# ⚠️ 用 requirements.lock 而不是 pyproject.toml：
# pyproject 里 21 条依赖全部只有 >= 下界，直接装会得到不可复现的版本组合。
RUN pip install --no-cache-dir -r requirements.lock


FROM python:3.12-slim AS runtime

# curl 保留给 HEALTHCHECK 用
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 不用 root 跑
RUN useradd --create-home --shell /bin/bash app
WORKDIR /app

COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

COPY --chown=app:app src/ ./src/
COPY --chown=app:app config/ ./config/
COPY --chown=app:app pyproject.toml ./

# 数据目录挂卷进来（Chroma / BM25 索引 / 上传文件），不烘进镜像
RUN mkdir -p /app/data /app/logs && chown -R app:app /app/data /app/logs

USER app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RAGENT_PORT=8000 \
    RAGENT_LOG_DEST=stdout \
    RAGENT_LOG_FORMAT=json

EXPOSE 8000

# ⚠️ 启动要 ~20s：lifespan 里同步预热 reranker/embedding/LLM，换来冷热差 ≤0.5s
# （CLAUDE.md §2）。start-period 必须留够，否则编排器会误判为启动失败并反复重启。
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:${RAGENT_PORT}/health || exit 1

CMD ["python", "-m", "uvicorn", "src.ragent_backend.app:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]
