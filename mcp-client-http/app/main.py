from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from .chat import ChatService, ConversationStore
from .mcp_client import MCPGateway


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("mcp-client-http")

STATIC_DIR = Path(__file__).parent / "static"


class Settings:
    mcp_url = os.getenv("MCP_URL", "http://mcp-server:8000/mcp")
    mcp_token = os.getenv("MCP_AUTH_TOKEN", "").strip()
    mcp_timeout = float(os.getenv("MCP_TIMEOUT", "60"))
    ollama_url = os.getenv(
        "OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1"
    ).rstrip("/")
    ollama_api_key = os.getenv("OLLAMA_API_KEY", "ollama")
    model = os.getenv("QWEN_MODEL", "qwen2.5:7b-instruct-q4_K_M")
    web_api_token = os.getenv("WEB_API_TOKEN", "").strip()
    conversation_ttl = int(os.getenv("CONVERSATION_TTL", "3600"))
    cors_origins = [
        value.strip()
        for value in os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if value.strip()
    ]


settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    gateway = MCPGateway(settings.mcp_url, settings.mcp_token, settings.mcp_timeout)
    llm = AsyncOpenAI(base_url=settings.ollama_url, api_key=settings.ollama_api_key)
    app.state.gateway = gateway
    app.state.chat = ChatService(
        gateway,
        llm,
        settings.model,
        ConversationStore(ttl_seconds=settings.conversation_ttl),
    )
    logger.info("HTTP-клиент запущен: MCP=%s, model=%s", settings.mcp_url, settings.model)
    try:
        yield
    finally:
        await gateway.close()
        await llm.close()


app = FastAPI(
    title="Factory Pulse MCP HTTP client",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Client-Token"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10_000)
    conversation_id: str | None = Field(default=None, alias="conversationId")


def require_client_token(
    x_client_token: Annotated[str | None, Header()] = None,
) -> None:
    if settings.web_api_token and x_client_token != settings.web_api_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный X-Client-Token")


def sse(event: str, data: Any) -> str:
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {serialized}\n\n"


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    response = {"status": "ok"}
    logger.info("Browser ← GET /api/health response=%s", response)
    return response


@app.get("/api/tools", dependencies=[Depends(require_client_token)])
async def tools(request: Request) -> dict[str, Any]:
    logger.info("Browser → GET /api/tools")
    listed = await request.app.state.gateway.list_tools()
    response = {"tools": listed}
    logger.info(
        "Browser ← GET /api/tools response=%s",
        json.dumps(response, ensure_ascii=False, default=str),
    )
    return response


@app.post("/api/chat", dependencies=[Depends(require_client_token)])
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    message = body.message.strip()
    if not message:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Сообщение пусто")

    async def events() -> AsyncIterator[str]:
        stream = request.app.state.chat.run(message, body.conversation_id)
        pending: asyncio.Task[Any] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(stream))
                done, _ = await asyncio.wait({pending}, timeout=15)
                if not done:
                    if await request.is_disconnected():
                        return
                    logger.info("Browser ← SSE keepalive")
                    yield ": keepalive\n\n"
                    continue
                try:
                    event, payload = pending.result()
                except StopAsyncIteration:
                    return
                finally:
                    pending = None
                if await request.is_disconnected():
                    return
                logger.info(
                    "Browser ← SSE event=%s data=%s",
                    event,
                    sse(event, payload).strip(),
                )
                yield sse(event, payload)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("Ошибка обработки сообщения")
            logger.info("Browser ← SSE event=error message=%s", error)
            yield sse("error", {"message": str(error) or error.__class__.__name__})
            logger.info("Browser ← SSE event=done")
            yield sse("done", {})
        finally:
            if pending:
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
            await stream.aclose()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete(
    "/api/conversations/{conversation_id}",
    dependencies=[Depends(require_client_token)],
)
async def delete_conversation(conversation_id: str, request: Request) -> JSONResponse:
    logger.info("Browser → DELETE /api/conversations/%s", conversation_id)
    deleted = await request.app.state.chat.store.delete(conversation_id)
    logger.info(
        "Browser ← DELETE /api/conversations/%s response=%s",
        conversation_id,
        {"deleted": deleted},
    )
    return JSONResponse({"deleted": deleted})
