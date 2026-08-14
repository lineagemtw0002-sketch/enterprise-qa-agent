"""配置加载与校验模块。

职责：
1. 从 YAML 文件读取项目配置。
2. 用 pydantic 做类型校验、默认值处理——替代原来手写的一整套 `_require_*` 断言函数。
3. 在启动早期执行校验，尽早失败（fail-fast）；校验错误统一包成 `SettingsError`，
   调用方不需要关心底层校验引擎是 pydantic 还是别的。

设计目标：
- 路径解析与当前工作目录无关，保证在不同启动位置都能稳定加载配置。
- 每个配置分区都是不可变（frozen）对象，运行期不应该被悄悄修改。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

# ---------------------------------------------------------------------------
# Repo root & path resolution
# ---------------------------------------------------------------------------
# Anchored to this file's location: <repo>/src/core/settings.py → parents[2]
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH: Path = REPO_ROOT / "config" / "settings.yaml"


def resolve_path(relative: str | Path) -> Path:
    """将仓库相对路径解析为绝对路径（已经是绝对路径则原样返回）。"""
    p = Path(relative)
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


class SettingsError(ValueError):
    """配置异常：字段缺失、类型不对或语义不合法时抛出。"""


class _Section(BaseModel):
    """所有配置分区的公共基类：构造后不可变，忽略 YAML 里没声明的多余字段。"""

    model_config = ConfigDict(frozen=True, extra="ignore")


# ---------------------------------------------------------------------------
# 配置分区：字段名与 config/settings.yaml 的结构一一对应
# ---------------------------------------------------------------------------
class LLMSettings(_Section):
    provider: str
    model: str
    temperature: float
    max_tokens: int
    # Azure/OpenAI 专用可选字段
    api_key: Optional[str] = None
    api_version: Optional[str] = None
    azure_endpoint: Optional[str] = None
    deployment_name: Optional[str] = None
    # Ollama 专用可选字段
    base_url: Optional[str] = None


class EmbeddingSettings(_Section):
    provider: str
    model: str
    dimensions: int
    api_key: Optional[str] = None
    api_version: Optional[str] = None
    azure_endpoint: Optional[str] = None
    deployment_name: Optional[str] = None
    base_url: Optional[str] = None


class VectorStoreSettings(_Section):
    provider: str
    persist_directory: str
    collection_name: str


class RetrievalSettings(_Section):
    dense_top_k: int
    sparse_top_k: int
    fusion_top_k: int
    rrf_k: int


class RerankSettings(_Section):
    enabled: bool
    provider: str
    model: str
    top_k: int


class EvaluationSettings(_Section):
    enabled: bool
    provider: str
    metrics: List[str]


class ObservabilitySettings(_Section):
    log_level: str
    trace_enabled: bool
    trace_file: str
    structured_logging: bool


class VisionLLMSettings(_Section):
    enabled: bool
    provider: str
    model: str
    max_image_size: int
    api_key: Optional[str] = None
    api_version: Optional[str] = None
    azure_endpoint: Optional[str] = None
    deployment_name: Optional[str] = None
    base_url: Optional[str] = None


class IngestionSettings(_Section):
    chunk_size: int
    chunk_overlap: int
    splitter: str
    batch_size: int
    chunk_refiner: Optional[Dict[str, Any]] = None  # 动态配置，直接透传
    metadata_enricher: Optional[Dict[str, Any]] = None  # 动态配置，直接透传


class MCPServerConfig(_Section):
    """单个 MCP Server 的配置。"""

    transport: str = "stdio"  # "stdio" | "sse"
    # stdio 参数
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    cwd: Optional[str] = None
    # sse 参数
    url: Optional[str] = None
    # 通用参数
    timeout_seconds: float = 30.0


class Settings(_Section):
    llm: LLMSettings
    embedding: EmbeddingSettings
    vector_store: VectorStoreSettings
    retrieval: RetrievalSettings
    rerank: RerankSettings
    evaluation: EvaluationSettings
    observability: ObservabilitySettings
    # 可选能力模块：YAML 里没写这块，就是 None
    ingestion: Optional[IngestionSettings] = None
    vision_llm: Optional[VisionLLMSettings] = None
    mcp_servers: Optional[Dict[str, MCPServerConfig]] = None


# ---------------------------------------------------------------------------
# 环境变量覆盖
# ---------------------------------------------------------------------------
# (分区, 字段) -> (环境变量名, 类型转换函数)。变量名沿用原来的命名（不加双下划线），
# 避免破坏现有的 .env / .env.example 和部署脚本。
_ENV_OVERRIDES: dict[tuple[str, str], tuple[str, type]] = {
    ("llm", "provider"): ("RAGENT_LLM_PROVIDER", str),
    ("llm", "model"): ("RAGENT_LLM_MODEL", str),
    ("llm", "base_url"): ("RAGENT_LLM_BASE_URL", str),
    ("llm", "api_key"): ("RAGENT_LLM_API_KEY", str),
    ("llm", "temperature"): ("RAGENT_LLM_TEMPERATURE", float),
    ("llm", "max_tokens"): ("RAGENT_LLM_MAX_TOKENS", int),
    ("embedding", "provider"): ("RAGENT_EMBEDDING_PROVIDER", str),
    ("embedding", "model"): ("RAGENT_EMBEDDING_MODEL", str),
    ("embedding", "base_url"): ("RAGENT_EMBEDDING_BASE_URL", str),
    ("embedding", "api_key"): ("RAGENT_EMBEDDING_API_KEY", str),
    ("embedding", "dimensions"): ("RAGENT_EMBEDDING_DIMENSIONS", int),
    ("vision_llm", "enabled"): ("RAGENT_VISION_LLM_ENABLED", bool),
    ("vision_llm", "provider"): ("RAGENT_VISION_LLM_PROVIDER", str),
    ("vision_llm", "model"): ("RAGENT_VISION_LLM_MODEL", str),
    ("vision_llm", "base_url"): ("RAGENT_VISION_LLM_BASE_URL", str),
    ("vision_llm", "api_key"): ("RAGENT_VISION_LLM_API_KEY", str),
    ("vision_llm", "max_image_size"): ("RAGENT_VISION_LLM_MAX_IMAGE_SIZE", int),
}

_TRUE_STRINGS = {"true", "1", "yes", "on"}


def _apply_env_overrides(data: Dict[str, Any]) -> None:
    """用环境变量覆盖 YAML 配置中的对应字段（原地修改 data）。

    支持哪些环境变量见 `_ENV_OVERRIDES`；无法按目标类型转换的值会被忽略，
    交给后面的 pydantic 校验去报出更明确的错误。
    """
    for (section, field), (env_name, caster) in _ENV_OVERRIDES.items():
        raw = os.getenv(env_name)
        if not raw:
            continue
        try:
            value = (raw.strip().lower() in _TRUE_STRINGS) if caster is bool else caster(raw)
        except ValueError:
            continue
        if not isinstance(data.get(section), dict):
            data[section] = {}
        data[section][field] = value


def load_settings(path: Optional[str | Path] = None) -> Settings:
    """从 YAML 文件加载并校验配置。

    参数：
    - path: 配置文件路径。为空时默认使用 `<repo>/config/settings.yaml`，
      且与当前工作目录无关。
    """
    settings_path = resolve_path(path) if path is not None else DEFAULT_SETTINGS_PATH
    if not settings_path.exists():
        raise SettingsError(f"Settings file not found: {settings_path}")

    # 使用 safe_load 避免执行任意 YAML 标签构造逻辑。
    with settings_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    if not isinstance(data, dict):
        raise SettingsError("Settings root must be a mapping")

    _apply_env_overrides(data)

    try:
        return Settings(**data)
    except ValidationError as exc:
        raise SettingsError(str(exc)) from exc
