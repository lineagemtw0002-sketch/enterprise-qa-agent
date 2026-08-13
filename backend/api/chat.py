import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from pydantic import BaseModel

from agent.graph import build_agent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    provider: str | None = None
    model: str | None = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


async def _stream_response(req: ChatRequest) -> AsyncGenerator[str, None]:
    try:
        agent = build_agent(provider=req.provider, model=req.model)
    except Exception as exc:  # noqa: BLE001 - e.g. missing API key for chosen provider
        yield _sse("error", {"message": str(exc)})
        yield _sse("done", {})
        return

    inputs = {"messages": [HumanMessage(content=req.message)]}

    try:
        async for msg, _metadata in agent.astream(inputs, stream_mode="messages"):
            if isinstance(msg, ToolMessage):
                yield _sse("tool_result", {"tool": msg.name, "content": str(msg.content)})
            elif isinstance(msg, AIMessageChunk):
                text = _extract_text(msg.content)
                if text:
                    yield _sse("token", {"content": text})
    except Exception as exc:  # noqa: BLE001 - surfaced to the client rather than a 500 mid-stream
        logger.exception("Error while streaming agent response")
        yield _sse("error", {"message": str(exc)})
    finally:
        yield _sse("done", {})


@router.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    return StreamingResponse(_stream_response(req), media_type="text/event-stream")
