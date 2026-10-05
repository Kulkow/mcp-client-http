import json

import httpx
import pytest

from app.mcp_client import PROTOCOL_VERSION, StreamableHttpMCPClient, _decode_sse
from app.chat import fallback_tool_answer, mcp_tools_to_openai, tool_result_payload


def test_decode_sse_message_and_keepalive() -> None:
    result = _decode_sse(
        ': keepalive\n\nevent: message\ndata: {"jsonrpc":"2.0",\ndata: "id":7}\n\n'
    )
    assert result == [{"jsonrpc": "2.0", "id": 7}]


def test_tool_names_are_safe_and_keep_remote_mapping() -> None:
    tools, aliases = mcp_tools_to_openai(
        [
            {
                "name": "personnel.get_employee_by_tab_number",
                "inputSchema": {"type": "object"},
            }
        ]
    )
    alias = tools[0]["function"]["name"]
    assert "." not in alias
    assert aliases[alias] == "personnel.get_employee_by_tab_number"


def test_structured_content_has_priority() -> None:
    result = {
        "isError": True,
        "structuredContent": {"success": False, "error": "employee_not_found"},
        "content": [{"type": "text", "text": "ignored"}],
    }
    assert tool_result_payload(result) == {
        "success": False,
        "error": "employee_not_found",
    }


def test_fallback_tool_answer_uses_message_and_data() -> None:
    answer = fallback_tool_answer(
        [
            (
                "personnel.get_employee",
                {
                    "success": True,
                    "message": "Сотрудник найден",
                    "data": {"id": 21, "name": "Иван"},
                },
                False,
            )
        ]
    )
    assert "Сотрудник найден" in answer
    assert '"id": 21' in answer


@pytest.mark.asyncio
async def test_full_mcp_lifecycle_with_json_and_sse() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)

        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-42"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": PROTOCOL_VERSION},
                },
            )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/list":
            data = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tools": [{"name": "personnel.get_employee"}]},
                }
            )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=f"event: message\ndata: {data}\n\n",
            )
        raise AssertionError(body)

    client = StreamableHttpMCPClient(
        "https://factory.example/api/ai/mcp",
        "secret",
        transport=httpx.MockTransport(handler),
    )
    await client.initialize()
    assert await client.list_tools() == [{"name": "personnel.get_employee"}]
    await client.close()

    initialize, initialized, listed, deleted = calls
    assert "Mcp-Session-Id" not in initialize.headers
    assert initialize.headers["Authorization"] == "Bearer secret"
    for request in (initialized, listed, deleted):
        assert request.headers["Mcp-Session-Id"] == "session-42"
        assert request.headers["MCP-Protocol-Version"] == PROTOCOL_VERSION
