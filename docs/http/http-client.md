# Подключение MCP-клиента по HTTP

## Параметры подключения

| Параметр | Значение |
| --- | --- |
| Transport | MCP Streamable HTTP |
| Endpoint | `https://<host>/api/ai/mcp` |
| Аутентификация | Laravel Sanctum: `Authorization: Bearer <token>` |
| Формат запросов | JSON-RPC 2.0 |
| Версия MCP | `2025-11-25` |
| Методы HTTP | `POST`, `DELETE`, `OPTIONS` |

Для локального окружения endpoint обычно имеет вид
`http://localhost/api/ai/mcp`. В рабочем окружении `<host>` необходимо заменить
на публичный адрес Factory Pulse.

Клиент должен отправлять с каждым `POST`-запросом следующие заголовки:

```http
Authorization: Bearer <token>
Content-Type: application/json
Accept: application/json, text/event-stream
```

После инициализации к ним добавляются заголовки:

```http
Mcp-Session-Id: <session-id>
MCP-Protocol-Version: 2025-11-25
```

`Mcp-Session-Id` возвращается сервером в заголовке ответа на `initialize`.
Значение действует только в рамках созданной сессии и не должно быть заранее
сгенерировано клиентом.

> Endpoint защищён middleware `auth:sanctum`. Токен можно получить через
> `POST /api/identity/auth/login` либо другим принятым в конкретном окружении
> способом. Токен нельзя включать в URL, логи или конфигурацию репозитория.

Запрос токена выполняется отдельно от MCP-сессии:

```http
POST /api/identity/auth/login
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "<password>"
}
```

При успешной аутентификации токен находится в `data.token`:

```json
{
  "success": true,
  "data": {
    "message": "Welcome!",
    "token": "<sanctum-token>",
    "token_type": "Bearer"
  }
}
```

## Короткое описание для конфигурации клиента

Если MCP-клиент поддерживает Streamable HTTP, ему достаточно передать такие
параметры (точный формат конфигурационного файла зависит от клиента):

```json
{
  "name": "factory-pulse",
  "transport": "streamable-http",
  "url": "https://<host>/api/ai/mcp",
  "headers": {
    "Authorization": "Bearer <token>"
  }
}
```

Клиент должен самостоятельно выполнить стандартный MCP lifecycle:

1. Отправить `initialize`.
2. Сохранить ответный заголовок `Mcp-Session-Id`.
3. Отправить `notifications/initialized` с идентификатором сессии.
4. Использовать ту же сессию для `tools/list` и `tools/call`.
5. Завершить сессию запросом `DELETE`.

## Проверка через curl

В примерах ниже используются переменные:

```bash
MCP_URL="https://<host>/api/ai/mcp"
MCP_TOKEN="<sanctum-token>"
```

### 1. Инициализация

```bash
curl --include --request POST "$MCP_URL" \
  --header "Authorization: Bearer $MCP_TOKEN" \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json, text/event-stream' \
  --data '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-11-25",
      "capabilities": {},
      "clientInfo": {
        "name": "factory-pulse-client",
        "version": "1.0.0"
      }
    }
  }'
```

Успешный ответ имеет статус `200 OK` и содержит заголовок:

```http
Mcp-Session-Id: <session-id>
```

Сохраните его для следующих запросов:

```bash
MCP_SESSION_ID="<session-id-from-response>"
```

### 2. Подтверждение инициализации

```bash
curl --request POST "$MCP_URL" \
  --header "Authorization: Bearer $MCP_TOKEN" \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json, text/event-stream' \
  --header "Mcp-Session-Id: $MCP_SESSION_ID" \
  --header 'MCP-Protocol-Version: 2025-11-25' \
  --data '{
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
    "params": {}
  }'
```

Для notification нормальным ответом является `202 Accepted` без JSON-тела.

### 3. Получение списка инструментов

```bash
curl --request POST "$MCP_URL" \
  --header "Authorization: Bearer $MCP_TOKEN" \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json, text/event-stream' \
  --header "Mcp-Session-Id: $MCP_SESSION_ID" \
  --header 'MCP-Protocol-Version: 2025-11-25' \
  --data '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }'
```

Сервер возвращает JSON-RPC response со списком доступных инструментов и их
входными JSON Schema. Клиенту рекомендуется получать список динамически, а не
хранить схему инструментов локально.

### 4. Вызов инструмента

Получение сотрудника по табельному номеру:

```bash
curl --request POST "$MCP_URL" \
  --header "Authorization: Bearer $MCP_TOKEN" \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json, text/event-stream' \
  --header "Mcp-Session-Id: $MCP_SESSION_ID" \
  --header 'MCP-Protocol-Version: 2025-11-25' \
  --data '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "personnel.get_employee_by_tab_number",
      "arguments": {
        "tabNumber": "00001"
      }
    }
  }'
```

Получение времени первого входа и последнего выхода сотрудника за день:

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "personnel.get_clock_event_by_employee_and_date",
    "arguments": {
      "employeeId": 123,
      "date": "2026-10-05"
    }
  }
}
```

Результат инструмента содержит `structuredContent` единого формата:

```json
{
  "success": true,
  "data": {},
  "error": null,
  "message": "Описание результата"
}
```

Ожидаемая бизнес-ошибка возвращается как MCP tool error (`isError: true`):

```json
{
  "success": false,
  "data": null,
  "error": "employee_not_found",
  "message": "Сотрудник не найден"
}
```

Клиент должен проверять одновременно MCP-поле `isError` и содержимое
`structuredContent`.

### 5. Завершение сессии

```bash
curl --include --request DELETE "$MCP_URL" \
  --header "Authorization: Bearer $MCP_TOKEN" \
  --header "Mcp-Session-Id: $MCP_SESSION_ID" \
  --header 'MCP-Protocol-Version: 2025-11-25'
```

Успешное завершение возвращает `200 OK`. После него идентификатор сессии
использовать нельзя.

## Особенности ответов

- Обычный ответ приходит с `Content-Type: application/json`.
- Если обработка требует двустороннего обмена, сервер может вернуть
  `Content-Type: text/event-stream`. Клиент должен уметь читать SSE-события
  `event: message` и извлекать JSON-RPC payload из поля `data`.
- `GET` для открытия отдельного SSE-канала текущей реализацией не используется;
  поток при необходимости возвращается в ответ на `POST`.
- Сессии хранятся ограниченное время. По умолчанию TTL составляет 3600 секунд.
  При ответе о неизвестной или истёкшей сессии клиент должен повторить
  инициализацию, а не переиспользовать старый ID.
- Максимальный размер тела одного `POST` по умолчанию равен 4 MiB.

## Типовые ошибки

| HTTP status | Причина | Действие клиента |
| --- | --- | --- |
| `400` | Некорректный JSON-RPC, версия протокола, session ID или заголовки | Исправить запрос; не повторять без изменений |
| `401` | Sanctum-токен отсутствует или недействителен | Получить новый токен |
| `403` | `Host` или `Origin` не разрешён конфигурацией сервера | Обратиться к администратору сервера |
| `404` | Сессия не найдена или истекла | Выполнить `initialize` заново |
| `405` | Метод не поддерживается MCP transport | Использовать `POST`, `DELETE` или `OPTIONS` |
| `413` | Тело запроса превышает допустимый размер | Уменьшить payload |
| `500` | Необработанная ошибка сервера | Зафиксировать JSON-RPC id и обратиться к администратору |

## Требования к серверному окружению

Для внешнего домена администратор должен разрешить hostname в
`MCP_ALLOWED_HOSTS`. Для browser-based клиента также необходимо добавить его
origin в `MCP_ALLOWED_ORIGINS`, а hostname этого origin — в
`MCP_ALLOWED_HOSTS`. При горизонтальном масштабировании все экземпляры
приложения должны использовать общее хранилище MCP-сессий, например Redis.
