from datetime import date as date_type
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel


class ToolResult(BaseModel):
    success: bool
    data: Any | None = None
    error: str | None = None
    message: str | None = None


mcp = FastMCP(
    "employee-time-tracking",
    host="0.0.0.0",
    port=8000,
)

EMPLOYEES: dict[str, dict[str, Any]] = {
    "tab-21": {
        "id": 21,
        "tab_number": "tab-21",
        "last_name": "Петров",
        "first_name": "Иван",
        "middle_name": "Сергеевич",
        "department": "Разработка",
    },
    "tab-42": {
        "id": 42,
        "tab_number": "tab-42",
        "last_name": "Смирнова",
        "first_name": "Анна",
        "middle_name": "Олеговна",
        "department": "Бухгалтерия",
    },
}

CLOCK_EVENTS: dict[tuple[int, str], dict[str, Any]] = {
    (21, "2026-08-28"): {
        "employee_id": 21,
        "date": "2026-08-28",
        "arrival_time": "08:47:00",
        "departure_time": "17:34:00",
    },
    (42, "2026-08-28"): {
        "employee_id": 42,
        "date": "2026-08-28",
        "arrival_time": "09:03:00",
        "departure_time": "18:11:00",
    },
}


def normalize_tab_number(tab_number: str) -> str:
    normalized = tab_number.strip().lower().replace(" ", "")
    if normalized.isdigit():
        return f"tab-{normalized}"
    if normalized.startswith("tab"):
        return f"tab-{normalized[3:].lstrip('-')}"
    return normalized


@mcp.tool()
def get_employee_by_tab_number(tab_number: str) -> ToolResult:
    """Получить данные о сотруднике по табельному номеру."""
    normalized = normalize_tab_number(tab_number)
    employee = EMPLOYEES.get(normalized)
    if employee is None:
        return ToolResult(
            success=False,
            error="employee_not_found",
            message=f"Сотрудник с табельным номером {tab_number} не найден",
        )
    return ToolResult(
        success=True,
        data=employee,
        message="Сотрудник найден",
    )


@mcp.tool()
def get_clock_event_id(employee_id: int, date: str) -> ToolResult:
    """Получить время входа и выхода сотрудника по ID и дате YYYY-MM-DD."""
    try:
        normalized_date = str(date_type.fromisoformat(date))
    except (TypeError, ValueError):
        return ToolResult(
            success=False,
            error="invalid_date",
            message="Дата должна быть в формате YYYY-MM-DD",
        )

    event = CLOCK_EVENTS.get((employee_id, normalized_date))
    if event is None:
        return ToolResult(
            success=False,
            error="clock_event_not_found",
            message=(
                f"События сотрудника {employee_id} за {normalized_date} не найдены"
            ),
        )
    return ToolResult(
        success=True,
        data=event,
        message="Событие рабочего времени найдено",
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
