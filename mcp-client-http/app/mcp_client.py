from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx


PROTOCOL_VERSION = "2025-11-25"
logger = logging.getLogger("mcp-client-http.mcp")


def _json_log(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class MCPError(RuntimeError):
    """An HTTP, transport, or JSON-RPC error returned by the MCP server."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code

    @property
    def is_expired_session(self) -> bool:
        return self.status_code in {404, 410} or self.code == -32001


def _decode_sse(text: str) -> list[Any]:
    """Decode JSON values from SSE `data` fields.

    The MCP Streamable HTTP transport normally uses `event: message`. Unknown
    event types and keep-alive comments are ignored.
    """
    messages: list[Any] = []
    data_lines: list[str] = []
    event_name = "message"

    def flush() -> None:
        nonlocal data_lines, event_name
        if data_lines and event_name in {"", "message"}:
            raw = "\n".join(data_lines)
            try:
                messages.append(json.loads(raw))
            except json.JSONDecodeError as error:
                raise MCPError(f"MCP вернул некорректное SSE data: {error}") from error
        data_lines = []
        event_name = "message"

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line:
            flush()
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    flush()
    return messages


class StreamableHttpMCPClient:
    """Small MCP Streamable HTTP client following docs/http/http-client.md."""

    def __init__(
        self,
        url: str,
        token: str = "",
        *,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._url = url
        self._http = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout, read=max(timeout, 300.0)),
            transport=transport,
        )
        self._session_id: str | None = None
        self._request_id = 0

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _session_headers(self) -> dict[str, str]:
        if not self._session_id:
            return {}
        return {
            "Mcp-Session-Id": self._session_id,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }

    def _safe_response_text(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    async def _post(
        self,
        method: str,
        params: dict[str, Any],
        *,
        notification: bool = False,
        initialize: bool = False,
    ) -> dict[str, Any] | None:
        request_id = None if notification else self._next_id()
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        if request_id is not None:
            payload["id"] = request_id

        headers = {} if initialize else self._session_headers()
        logger.info("MCP → POST %s payload=%s", self._url, _json_log(payload))
        try:
            response = await self._http.post(self._url, headers=headers, json=payload)
        except httpx.HTTPError as error:
            logger.exception("MCP ← ошибка соединения: %s", error)
            raise MCPError(f"Не удалось обратиться к MCP-серверу: {error}") from error

        logger.info(
            "MCP ← HTTP %s content-type=%s body=%s",
            response.status_code,
            response.headers.get("content-type", ""),
            self._safe_response_text(response.text),
        )

        if initialize:
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self._session_id = session_id

        if response.status_code == 202 and notification:
            return None
        if response.is_error:
            detail = response.text.strip()[:1000]
            suffix = f": {detail}" if detail else ""
            raise MCPError(
                f"MCP HTTP {response.status_code}{suffix}",
                status_code=response.status_code,
            )
        if not response.content:
            if notification:
                return None
            raise MCPError("MCP-сервер вернул пустой ответ")

        content_type = response.headers.get("content-type", "").lower()
        try:
            candidates = (
                _decode_sse(response.text)
                if "text/event-stream" in content_type
                else [response.json()]
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise MCPError(f"MCP-сервер вернул некорректный JSON: {error}") from error

        result = next(
            (
                candidate
                for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("id") == request_id
            ),
            None,
        )
        if result is None and notification:
            return None
        if result is None:
            raise MCPError("В MCP-ответе нет JSON-RPC результата для запроса")
        if "error" in result:
            error = result.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise MCPError(f"MCP JSON-RPC {code}: {message}", code=code)
        value = result.get("result")
        if not isinstance(value, dict):
            raise MCPError("Поле result в MCP-ответе должно быть объектом")
        return value

    async def initialize(self) -> dict[str, Any]:
        result = await self._post(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "factory-pulse-http-client",
                    "version": "1.0.0",
                },
            },
            initialize=True,
        )
        if not self._session_id:
            raise MCPError("MCP-сервер не вернул заголовок Mcp-Session-Id")
        await self._post("notifications/initialized", {}, notification=True)
        return result or {}

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._post("tools/list", {})
        tools = (result or {}).get("tools")
        if not isinstance(tools, list):
            raise MCPError("MCP tools/list не вернул массив tools")
        return [tool for tool in tools if isinstance(tool, dict)]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._post(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        return result or {}

    async def close(self) -> None:
        close_error: MCPError | None = None
        if self._session_id:
            try:
                logger.info("MCP → DELETE %s", self._url)
                response = await self._http.delete(
                    self._url,
                    headers=self._session_headers(),
                )
                logger.info(
                    "MCP ← DELETE HTTP %s body=%s",
                    response.status_code,
                    self._safe_response_text(response.text),
                )
                if response.is_error and response.status_code not in {404, 410}:
                    close_error = MCPError(
                        f"Не удалось завершить MCP-сессию: HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
            except httpx.HTTPError as error:
                close_error = MCPError(f"Не удалось завершить MCP-сессию: {error}")
            finally:
                self._session_id = None
        await self._http.aclose()
        if close_error:
            raise close_error


class MCPGateway:
    """Serializes access to one remote MCP session and renews expired sessions."""

    def __init__(self, url: str, token: str, timeout: float = 60.0) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self._client: StreamableHttpMCPClient | None = None
        self._tools: list[dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> StreamableHttpMCPClient:
        if self._client and self._client.session_id:
            return self._client
        if self._client:
            await self._client.close()
        self._client = StreamableHttpMCPClient(
            self._url,
            self._token,
            timeout=self._timeout,
        )
        await self._client.initialize()
        self._tools = None
        return self._client

    async def _reset(self) -> None:
        client, self._client = self._client, None
        self._tools = None
        if client:
            try:
                await client.close()
            except MCPError:
                # Reset must still succeed when the remote session has expired or
                # the server is temporarily unavailable during DELETE.
                pass

    async def list_tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        async with self._lock:
            if self._tools is not None and not refresh:
                return self._tools
            for attempt in range(2):
                client = await self._connect()
                try:
                    self._tools = await client.list_tools()
                    return self._tools
                except MCPError as error:
                    if attempt == 0 and error.is_expired_session:
                        await self._reset()
                        continue
                    raise
            raise MCPError("Не удалось получить список MCP-инструментов")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            for attempt in range(2):
                client = await self._connect()
                try:
                    return await client.call_tool(name, arguments)
                except MCPError as error:
                    if attempt == 0 and error.is_expired_session:
                        await self._reset()
                        continue
                    raise
            raise MCPError("Не удалось вызвать MCP-инструмент")

    async def close(self) -> None:
        async with self._lock:
            await self._reset()


async def iter_sse_payloads(lines: AsyncIterator[str]) -> AsyncIterator[Any]:
    """Incremental SSE decoder kept public for clients/tests that need it."""
    buffer: list[str] = []
    async for line in lines:
        if line:
            buffer.append(line)
            continue
        for payload in _decode_sse("\n".join(buffer) + "\n\n"):
            yield payload
        buffer.clear()
    if buffer:
        for payload in _decode_sse("\n".join(buffer) + "\n\n"):
            yield payload
