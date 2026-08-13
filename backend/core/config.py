from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-5"

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"

    embedding_provider: str = "local"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"

    database_url: str = "postgresql+psycopg://eqa:eqa@localhost:5432/eqa"
    database_url_readonly: str = "postgresql+psycopg://eqa_readonly:eqa_readonly@localhost:5432/eqa"

    jwt_secret: str = "change-me-in-production"

    frontend_origins: str = "http://localhost:5173"

    @property
    def frontend_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.frontend_origins.split(",") if origin.strip()]

    chunk_size: int = 800
    chunk_overlap: int = 120
    retrieval_top_k: int = 5
    sql_row_limit: int = 200


@lru_cache
def get_settings() -> Settings:
    return Settings()
