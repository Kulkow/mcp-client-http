from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from .mcp_client import MCPError, MCPGateway


logger = logging.getLogger("mcp-client-http.chat")


def _json_log(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, indent=2)


SYSTEM_PROMPT = """Ты русскоязычный помощник.
Для получения фактических данных используй доступные MCP-инструменты. Не выдумывай
аргументы и результаты. Если инструмент вернул isError=true либо
structuredContent.success=false, понятно объясни ошибку пользователю. Отвечай кратко. Сам ответов не придумывай
"""


def _openai_tool_name(name: str, used: set[str]) -> str:
    alias = re.sub(r"[^a-zA-Z0-9_-]", "__", name)
    if not alias:
        alias = "mcp_tool"
    if len(alias) > 64 or alias in used:
        digest = hashlib.sha256(name.encode()).hexdigest()[:8]
        alias = f"{alias[:55]}_{digest}"
    used.add(alias)
    return alias


def mcp_tools_to_openai(
    tools: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    converted: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    used: set[str] = set()
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        alias = _openai_tool_name(name, used)
        aliases[alias] = name
        schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        description = tool.get("description")
        if not isinstance(description, str):
            description = ""
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": alias,
                    "description": (f"MCP tool `{name}`. " + description).strip(),
                    "parameters": schema,
                },
            }
        )
    return converted, aliases


def tool_result_payload(result: dict[str, Any]) -> Any:
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    for item in result.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return result


def fallback_tool_answer(tool_results: list[tuple[str, Any, bool]]) -> str:
    """Build a useful answer when an OpenAI-compatible model returns no text."""
    parts: list[str] = []
    for name, payload, is_error in tool_results:
        if isinstance(payload, dict):
            message = payload.get("message")
            data = payload.get("data")
            error = payload.get("error")
            heading = message or error or ("Ошибка инструмента" if is_error else name)
            if data is not None:
                rendered = json.dumps(data, ensure_ascii=False, indent=2)
                parts.append(f"{heading}\n{rendered}")
            else:
                parts.append(str(heading))
        elif isinstance(payload, str):
            parts.append(payload)
        else:
            parts.append(json.dumps(payload, ensure_ascii=False, indent=2))
    return "\n\n".join(part for part in parts if part.strip())


@dataclass
class Conversation:
    messages: list[dict[str, Any]] = field(
        default_factory=lambda: [{"role": "system", "content": SYSTEM_PROMPT}]
    )
    touched_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConversationStore:
    def __init__(self, ttl_seconds: int = 3600, max_messages: int = 80) -> None:
        self._ttl = ttl_seconds
        self._max_messages = max_messages
        self._items: dict[str, Conversation] = {}
        self._lock = asyncio.Lock()

    async def get(self, conversation_id: str | None) -> tuple[str, Conversation]:
        async with self._lock:
            now = time.monotonic()
            expired = [
                key for key, value in self._items.items() if now - value.touched_at > self._ttl
            ]
            for key in expired:
                self._items.pop(key, None)

            if conversation_id and conversation_id in self._items:
                conversation = self._items[conversation_id]
            else:
                conversation_id = str(uuid.uuid4())
                conversation = Conversation()
                self._items[conversation_id] = conversation
            conversation.touched_at = now
            return conversation_id, conversation

    async def delete(self, conversation_id: str) -> bool:
        async with self._lock:
            return self._items.pop(conversation_id, None) is not None

    def trim(self, conversation: Conversation) -> None:
        if len(conversation.messages) <= self._max_messages:
            return
        cutoff = len(conversation.messages) - self._max_messages + 1
        while (
            cutoff < len(conversation.messages)
            and conversation.messages[cutoff].get("role") != "user"
        ):
            cutoff += 1
        if cutoff == len(conversation.messages):
            cutoff = next(
                (
                    index
                    for index in range(len(conversation.messages) - 1, 0, -1)
                    if conversation.messages[index].get("role") == "user"
                ),
                len(conversation.messages) - 1,
            )
        conversation.messages = [
            conversation.messages[0],
            *conversation.messages[cutoff:],
        ]


class ChatService:
    def __init__(
        self,
        gateway: MCPGateway,
        llm: AsyncOpenAI,
        model: str,
        store: ConversationStore,
        max_tool_steps: int = 8,
    ) -> None:
        self.gateway = gateway
        self.llm = llm
        self.model = model
        self.store = store
        self.max_tool_steps = max_tool_steps

    async def run(
        self,
        message: str,
        conversation_id: str | None,
    ):
        conversation_id, conversation = await self.store.get(conversation_id)
        logger.info(
            "Browser → chat conversation=%s message=%s",
            conversation_id,
            _json_log(message),
        )
        yield "session", {"conversationId": conversation_id}

        async with conversation.lock:
            original = list(conversation.messages)
            working = [*original, {"role": "user", "content": message}]
            tool_results: list[tuple[str, Any, bool]] = []
            allow_tools = True
            try:
                yield "status", {"message": "Подключаюсь к MCP-серверу…"}
                mcp_tools = await self.gateway.list_tools()
                tools, tool_aliases = mcp_tools_to_openai(mcp_tools)
                yield "status", {"message": f"Доступно инструментов: {len(tools)}"}

                for step in range(self.max_tool_steps + 1):
                    request: dict[str, Any] = {
                        "model": self.model,
                        "messages": working,
                        "temperature": 0,
                    }
                    if tools and allow_tools:
                        request["tools"] = tools
                        request["tool_choice"] = "auto"
                    logger.info("LLM → request=%s", _json_log(request))
                    completion = await self.llm.chat.completions.create(**request)
                    answer = completion.choices[0].message
                    logger.info("LLM ← response=%s", _json_log(answer.model_dump()))

                    # Some local models return an empty first response instead of a
                    # tool call when tool_choice=auto. Retry once with a required tool.
                    if (
                        not answer.tool_calls
                        and not (answer.content or "").strip()
                        and not tool_results
                        and tools
                        and allow_tools
                    ):
                        yield "status", {
                            "message": "Модель не выбрала инструмент, повторяю запрос…"
                        }
                        retry_request = {**request, "tool_choice": "required"}
                        logger.info("LLM → retry request=%s", _json_log(retry_request))
                        completion = await self.llm.chat.completions.create(**retry_request)
                        answer = completion.choices[0].message
                        logger.info("LLM ← retry response=%s", _json_log(answer.model_dump()))

                    # After tools have returned data, ask for plain text without
                    # advertising tools again. This works around empty final messages
                    # produced by several Ollama tool-calling templates.
                    if (
                        not answer.tool_calls
                        and not (answer.content or "").strip()
                        and tool_results
                    ):
                        yield "status", {"message": "Формирую итоговый ответ…"}
                        final_request: dict[str, Any] = {
                            "model": self.model,
                            "messages": [
                                working[0],
                                {
                                    "role": "system",
                                    "content": (
                                        "Сформулируй итоговый ответ пользователю только "
                                        "по уже полученным результатам инструментов. "
                                        "Не вызывай инструменты и не добавляй факты."
                                    ),
                                },
                                *working[1:],
                            ],
                            "temperature": 0,
                        }
                        logger.info("LLM → final request=%s", _json_log(final_request))
                        completion = await self.llm.chat.completions.create(
                            **final_request
                        )
                        answer = completion.choices[0].message
                        logger.info("LLM ← final response=%s", _json_log(answer.model_dump()))

                    assistant: dict[str, Any] = {
                        "role": "assistant",
                        "content": answer.content or "",
                    }
                    if answer.tool_calls:
                        assistant["tool_calls"] = [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.function.name,
                                    "arguments": call.function.arguments or "{}",
                                },
                            }
                            for call in answer.tool_calls
                        ]
                    working.append(assistant)

                    if not answer.tool_calls:
                        final = (answer.content or "").strip()
                        if not final and tool_results:
                            final = fallback_tool_answer(tool_results)
                        if not final:
                            raise RuntimeError(
                                "LLM вернула пустой ответ и не выбрала MCP-инструмент. "
                                "Проверьте, что QWEN_MODEL поддерживает tool calling."
                            )
                        assistant["content"] = final
                        working[-1] = assistant
                        conversation.messages = working
                        conversation.touched_at = time.monotonic()
                        self.store.trim(conversation)
                        yield "message", {"content": final}
                        yield "done", {}
                        return

                    if step >= self.max_tool_steps:
                        raise RuntimeError("Превышено число последовательных вызовов инструментов")

                    for call in answer.tool_calls:
                        alias = call.function.name
                        name = tool_aliases.get(alias, alias)
                        try:
                            arguments = json.loads(call.function.arguments or "{}")
                        except json.JSONDecodeError as error:
                            raise ValueError(
                                f"Модель передала некорректные аргументы для {name}: {error}"
                            ) from error
                        if not isinstance(arguments, dict):
                            raise ValueError(f"Аргументы {name} должны быть JSON-объектом")

                        yield "tool", {"name": name, "arguments": arguments, "state": "start"}
                        logger.info(
                            "Chat → MCP tool=%s arguments=%s",
                            name,
                            _json_log(arguments),
                        )
                        try:
                            result = await self.gateway.call_tool(name, arguments)
                        except MCPError as error:
                            logger.exception(
                                "MCP tool завершился протокольной ошибкой: tool=%s",
                                name,
                            )
                            result = {
                                "isError": True,
                                "structuredContent": {
                                    "success": False,
                                    "data": None,
                                    "error": "mcp_tool_error",
                                    "message": str(error),
                                },
                            }
                        logger.info("Chat ← MCP tool=%s result=%s", name, _json_log(result))
                        payload = tool_result_payload(result)
                        business_error = (
                            isinstance(payload, dict) and payload.get("success") is False
                        )
                        is_error = bool(result.get("isError")) or business_error
                        tool_results.append((name, payload, is_error))
                        if is_error:
                            allow_tools = False
                        yield "tool", {
                            "name": name,
                            "state": "finish",
                            "isError": is_error,
                            "result": payload,
                        }
                        working.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "name": alias,
                                "content": json.dumps(payload, ensure_ascii=False),
                            }
                        )
            except asyncio.CancelledError:
                conversation.messages = original
                raise
            except Exception:
                conversation.messages = original
                raise
