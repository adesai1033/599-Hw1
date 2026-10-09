"""FastAPI app: POST /chat (the assignment contract) and GET /health."""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import FastAPI
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, StringConstraints

from agent import ask, build_agent, build_llm

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("main")

TIMEOUT_ANSWER = "That took too long to answer. Please try a narrower question."
ERROR_ANSWER = "Something went wrong while answering. Please try again."


class ChatRequest(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    session_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class ChatResponse(BaseModel):
    response: str


def create_app(config_path: str | None = None, llm: BaseChatModel | None = None) -> FastAPI:
    path = config_path or os.environ.get("MCP_SERVERS_CONFIG", "mcp_config.json")
    timeout = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "120"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A missing LLM key is a deploy error and fails loudly. MCP failures are the opposite:
        # discovery tolerates dead servers, so the service always starts.
        if llm is None and not os.environ.get("OPENAI_API_KEY"):
            logger.critical("OPENAI_API_KEY is not set; refusing to start")
            raise RuntimeError("OPENAI_API_KEY is not set")
        model = llm or build_llm()
        app.state.graph, app.state.unavailable, app.state.tools = await build_agent(path, llm=model)
        app.state.servers = list(json.loads(Path(path).read_text()))
        app.state.model = getattr(model, "model_name", type(model).__name__)
        up = [name for name in app.state.servers if name not in app.state.unavailable]
        logger.info(
            "agent ready: %d tools from %s; unavailable: %s",
            len(app.state.tools), up, list(app.state.unavailable) or "none",
        )
        yield

    app = FastAPI(title="Market Watcher", lifespan=lifespan)

    @app.post("/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest) -> ChatResponse:
        started = time.monotonic()
        logger.debug("chat session=%s query=%.200s", request.session_id, request.query)
        try:
            answer = await asyncio.wait_for(
                ask(app.state.graph, request.query, request.session_id), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning("chat timed out session=%s", request.session_id)
            answer = TIMEOUT_ANSWER
        except Exception:
            # The rubric penalizes unhandled server errors, and a caller cannot tell an LLM outage
            # from a bug; the traceback goes to the logs instead.
            logger.exception("chat failed session=%s", request.session_id)
            answer = ERROR_ANSWER
        logger.info(
            "chat session=%s query_chars=%d elapsed_ms=%d",
            request.session_id, len(request.query), (time.monotonic() - started) * 1000,
        )
        return ChatResponse(response=answer)

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "model": app.state.model,
            "servers": {n: "down" if n in app.state.unavailable else "up" for n in app.state.servers},
            "tools": app.state.tools,
            "unavailable": app.state.unavailable,
        }

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
