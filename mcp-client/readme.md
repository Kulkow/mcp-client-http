Единый формат результата MCP tools

````python
from typing import Any
from pydantic import BaseModel

class ToolResult(BaseModel):
    success: bool
    data: Any | None = None
    error: str | None = None
    message: str | None = None
````

 State LangGraph

`````
from typing import TypedDict, Any


class AgentState(TypedDict, total=False):
    messages: list

    last_tool_name: str
    last_tool_result: ToolResult

    response: str
`````

Универсальный Tool Node
````
async def execute_tool(state: AgentState):
    tool_call = get_last_tool_call(state["messages"])

    result = await mcp.call_tool(
        tool_call["name"],
        tool_call["args"],
    )

    tool_result = ToolResult.model_validate(result)

    return {
        "last_tool_name": tool_call["name"],
        "last_tool_result": tool_result,
    }
````

tool_result_router

````
def tool_result_router(state: AgentState) -> str:
    result = state["last_tool_result"]

    if result.success:
        return "agent"

    return "tool_error"

    builder.add_conditional_edges(
    "execute_tool",
    tool_result_router,
    {
        "agent": "agent",
        "tool_error": "tool_error",
    },
)

````

````
                  ┌──────────────┐
                  │              │
                  ▼              │
Agent ──────► execute_tool       │
                  │              │
                  ▼              │
         tool_result_router      │
              /       \          │
         success      error      │
            │           │        │
            └───────────┘        │
            │                    │
            ▼                    │
          Agent ─────────────────┘
````
