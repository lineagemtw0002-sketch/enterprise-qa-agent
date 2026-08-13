"""Multi-provider chat model factory.

Switching LLM backend is a config change (LLM_PROVIDER / LLM_MODEL env vars, or an
explicit override per call), not a code change. Anthropic, OpenAI and local models
served through Ollama are all supported via LangChain's unified chat model interface,
so the rest of the app (agent, tools) only ever talks to `BaseChatModel`.
"""

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from core.config import get_settings

_PROVIDER_ENV_KEY = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
}


def get_chat_model(
    provider: str | None = None,
    model: str | None = None,
    *,
    streaming: bool = True,
    temperature: float = 0.0,
) -> BaseChatModel:
    settings = get_settings()
    provider = provider or settings.llm_provider
    model = model or settings.llm_model

    if provider in _PROVIDER_ENV_KEY:
        api_key = getattr(settings, _PROVIDER_ENV_KEY[provider])
        if not api_key:
            raise RuntimeError(
                f"LLM_PROVIDER={provider!r} requires {_PROVIDER_ENV_KEY[provider].upper()} to be set"
            )

    kwargs: dict = {"streaming": streaming, "temperature": temperature}
    if provider == "ollama":
        kwargs["base_url"] = settings.ollama_base_url

    return init_chat_model(model, model_provider=provider, **kwargs)
