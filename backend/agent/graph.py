from langgraph.prebuilt import create_react_agent

from agent.prompts import SYSTEM_PROMPT
from agent.tools import query_structured_data, retrieve_documents
from llm.provider import get_chat_model


def build_agent(provider: str | None = None, model: str | None = None):
    llm = get_chat_model(provider=provider, model=model, streaming=True)
    return create_react_agent(
        llm,
        tools=[retrieve_documents, query_structured_data],
        prompt=SYSTEM_PROMPT,
    )
