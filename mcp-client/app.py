import argparse
import asyncio
import inspect
import json
import logging
import os
import sys
from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openai import AsyncOpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from pydantic import BaseModel, ValidationError


logger = logging.getLogger("mcp-client")

SYSTEM_PROMPT = """Ты помощник по учёту рабочего времени.
Для фактических ответов используй MCP-инструменты и вызывай только один инструмент за шаг.
Сначала найди сотрудника по табельному номеру, затем используй employee_id из результата
для запроса рабочего времени. Не придумывай аргументы инструментов.

Каждый инструмент возвращает объект ToolResult:
- success=true: используй поле data и при необходимости вызови следующий инструмент;
- success=false: не вызывай другие инструменты, объясни ошибку по полям error и message.

Преобразуй пользовательскую дату в YYYY-MM-DD. Отвечай кратко на русском языке.
"""


class ToolResult(BaseModel):
    success: bool
    data: Any | None = None
    error: str | None = None
    message: str | None = None


class AgentState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    last_tool_name: str
    last_tool_result: ToolResult
    response: str


def env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LangGraph-клиент для MCP и Ollama")
    parser.add_argument("query", nargs="*", help="Запрос на естественном языке")
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Проверить MCP-соединение и вывести доступные инструменты",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Выполнить один запрос и завершить работу",
    )
    return parser.parse_args()


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def exception_details(error: BaseException) -> str:
    nested = getattr(error, "exceptions", None)
    if nested:
        details = [exception_details(item) for item in nested]
        return "; ".join(dict.fromkeys(detail for detail in details if detail))
    return str(error) or error.__class__.__name__


def mcp_tools_to_openai(tools: Sequence[Any]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        schema = getattr(tool, "inputSchema", None)
        if schema is None:
            schema = getattr(tool, "input_schema", {"type": "object"})
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": schema,
                },
            }
        )
    return converted


def assistant_message_dict(message: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "role": "assistant",
        "content": message.content or "",
    }
    if message.tool_calls:
        if len(message.tool_calls) > 1:
            logger.warning(
                "LLM запросила %s инструментов одновременно; выполняю первый",
                len(message.tool_calls),
            )
        call = message.tool_calls[0]
        value["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
        ]
    return value


def get_last_tool_call(messages: list[dict[str, Any]]) -> dict[str, Any]:
    if not messages:
        raise ValueError("История сообщений пуста")

    tool_calls = messages[-1].get("tool_calls") or []
    if not tool_calls:
        raise ValueError("Последнее сообщение не содержит вызова инструмента")

    call = tool_calls[-1]
    function = call.get("function") or {}
    raw_arguments = function.get("arguments") or "{}"
    arguments = (
        json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    )
    if not isinstance(arguments, dict):
        raise ValueError("Аргументы инструмента должны быть JSON-объектом")

    return {
        "id": call.get("id", "tool-call"),
        "name": function.get("name", ""),
        "args": arguments,
    }


def extract_mcp_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured

    content = getattr(result, "content", [])
    for item in content:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return None


def build_graph(
    mcp: ClientSession,
    llm: AsyncOpenAI,
    model: str,
    tools: list[dict[str, Any]],
):
    async def agent(state: AgentState) -> AgentState:
        last_result = state.get("last_tool_result")
        logger.info("Agent → отправляю контекст в Ollama")

        request: dict[str, Any] = {
            "model": model,
            "messages": state["messages"],
            "temperature": 0,
        }
        if last_result is None or last_result.success:
            request["tools"] = tools
            request["tool_choice"] = "required" if last_result is None else "auto"
        else:
            logger.info("Agent: ToolResult.success=false, новые tools отключены")

        completion = await llm.chat.completions.create(**request)
        message = assistant_message_dict(completion.choices[0].message)
        messages = [*state["messages"], message]

        if message.get("tool_calls"):
            name = message["tool_calls"][0]["function"]["name"]
            logger.info("Agent ← выбран инструмент: %s", name)
            return {"messages": messages, "response": ""}

        response = message.get("content", "")
        logger.info("Agent ← сформирован итоговый ответ")
        return {"messages": messages, "response": response}

    async def execute_tool(state: AgentState) -> AgentState:
        tool_call = get_last_tool_call(state["messages"])
        logger.info(
            "MCP → %s(%s)",
            tool_call["name"],
            json.dumps(tool_call["args"], ensure_ascii=False),
        )

        try:
            raw_result = await mcp.call_tool(tool_call["name"], tool_call["args"])
            payload = extract_mcp_payload(raw_result)
            if isinstance(payload, str):
                payload = json.loads(payload)
            tool_result = ToolResult.model_validate(payload)
        except ValidationError as error:
            tool_result = ToolResult(
                success=False,
                error="invalid_tool_result",
                message=f"MCP tool вернул результат не в формате ToolResult: {error}",
            )
        except Exception as error:
            tool_result = ToolResult(
                success=False,
                error="tool_execution_error",
                message=exception_details(error),
            )

        serialized = tool_result.model_dump_json()
        logger.info("MCP ← %s", serialized)
        messages = [
            *state["messages"],
            {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": tool_call["name"],
                "content": serialized,
            },
        ]
        return {
            "messages": messages,
            "last_tool_name": tool_call["name"],
            "last_tool_result": tool_result,
        }

    async def tool_error(state: AgentState) -> AgentState:
        result = state["last_tool_result"]
        logger.warning(
            "Tool error: tool=%s error=%s message=%s",
            state.get("last_tool_name", "unknown"),
            result.error,
            result.message,
        )
        return {"response": result.message or result.error or "Ошибка инструмента"}

    def agent_router(state: AgentState) -> str:
        return "execute_tool" if state["messages"][-1].get("tool_calls") else "end"

    def tool_result_router(state: AgentState) -> str:
        return "agent" if state["last_tool_result"].success else "tool_error"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("execute_tool", execute_tool)
    graph.add_node("tool_error", tool_error)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        agent_router,
        {"execute_tool": "execute_tool", "end": END},
    )
    graph.add_conditional_edges(
        "execute_tool",
        tool_result_router,
        {"agent": "agent", "tool_error": "tool_error"},
    )
    graph.add_edge("tool_error", "agent")
    return graph.compile()


async def open_mcp_transport(
    stack: AsyncExitStack,
    mcp_url: str,
    headers: dict[str, str] | None,
) -> tuple[Any, Any]:
    parameters = inspect.signature(streamable_http_client).parameters
    options: dict[str, Any] = {}

    if "headers" in parameters:
        logger.info("MCP transport: API с прямой передачей HTTP-заголовков")
        options["headers"] = headers
    elif "http_client" in parameters:
        logger.info("MCP transport: API с отдельным HTTP-клиентом")
        try:
            import httpx2 as http_library
        except ImportError:
            import httpx as http_library

        http_client = http_library.AsyncClient(
            headers=headers or {},
            timeout=http_library.Timeout(30, read=300),
        )
        await stack.enter_async_context(http_client)
        options["http_client"] = http_client

    streams = await stack.enter_async_context(
        streamable_http_client(mcp_url, **options)
    )
    return streams[0], streams[1]


async def run() -> int:
    args = parse_args()
    mcp_url = os.getenv("MCP_URL", "http://mcp-server:8000/mcp")
    token = os.getenv("MCP_AUTH_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else None

    logger.info("Подключение к MCP-серверу: %s", mcp_url)
    async with AsyncExitStack() as stack:
        read_stream, write_stream = await open_mcp_transport(stack, mcp_url, headers)
        async with ClientSession(read_stream, write_stream) as mcp:
            await mcp.initialize()
            logger.info("MCP-сессия инициализирована")
            listed = await mcp.list_tools()
            logger.info(
                "Доступные инструменты: %s",
                ", ".join(tool.name for tool in listed.tools),
            )

            if args.list_tools:
                for tool in listed.tools:
                    print(f"{tool.name}: {tool.description or ''}")
                return 0

            if not env_flag("USE_LLM"):
                raise ValueError("USE_LLM выключен, а клиент работает через Ollama")

            ollama_url = os.getenv(
                "OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1"
            ).rstrip("/")
            model = os.getenv("QWEN_MODEL", "qwen2.5:7b-instruct-q4_K_M")
            logger.info("Ollama: %s, модель: %s", ollama_url, model)

            llm = AsyncOpenAI(
                base_url=ollama_url,
                api_key=os.getenv("OLLAMA_API_KEY", "ollama"),
            )
            try:
                graph = build_graph(mcp, llm, model, mcp_tools_to_openai(listed.tools))
                initial_query = (
                    " ".join(args.query).strip() or os.getenv("QUERY", "").strip()
                )
                conversation: list[dict[str, Any]] = [
                    {"role": "system", "content": SYSTEM_PROMPT}
                ]
                prompt: PromptSession[str] = PromptSession(history=InMemoryHistory())
                print(
                    "Интерактивный режим. Для выхода: exit, quit, выход или Ctrl+D.",
                    flush=True,
                )

                while True:
                    if initial_query:
                        query = initial_query
                        initial_query = ""
                        print(f"\nВы> {query}", flush=True)
                    else:
                        try:
                            query = (await prompt.prompt_async("\nВы> ")).strip()
                        except EOFError:
                            print()
                            logger.info("Получен EOF, завершаю работу")
                            break
                        except KeyboardInterrupt:
                            print("Для выхода введите 'выход' или нажмите Ctrl+D.")
                            continue

                    if query.lower() in {"exit", "quit", "выход"}:
                        logger.info("Завершение по команде пользователя")
                        break
                    if not query:
                        continue

                    logger.info("Новый вопрос: %s", query)
                    previous_messages = list(conversation)
                    request_messages = [*conversation, {"role": "user", "content": query}]
                    try:
                        state = await graph.ainvoke(
                            {"messages": request_messages},
                            config={"recursion_limit": 16},
                        )
                    except Exception as error:
                        details = exception_details(error)
                        logger.error("Не удалось обработать вопрос: %s", details)
                        logger.debug("Подробности ошибки вопроса", exc_info=True)
                        print(
                            f"\nАссистент> Ошибка: {details}\n"
                            "Можно исправить причину и задать вопрос ещё раз.",
                            flush=True,
                        )
                        conversation = previous_messages
                        if args.once:
                            raise
                        continue

                    conversation = state["messages"]
                    answer = state.get("response") or conversation[-1].get("content", "")
                    print(f"\nАссистент> {answer}", flush=True)
                    if args.once:
                        break
            finally:
                await llm.close()
                logger.info("Соединение с Ollama закрыто")
    return 0


def main() -> None:
    configure_logging()
    try:
        raise SystemExit(asyncio.run(run()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        logger.error("Ошибка: %s", exception_details(error))
        logger.debug("Подробности ошибки", exc_info=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
