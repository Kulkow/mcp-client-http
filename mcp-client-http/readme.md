# MCP Client HTTP

HTTP-шлюз между браузером, OpenAI-compatible LLM (по умолчанию Ollama) и
сторонним MCP-сервером. Подключение к MCP выполняется по
`docs/http/http-client.md`: JSON-RPC 2.0, Streamable HTTP, версия протокола
`2025-11-25`, заголовки `Mcp-Session-Id` и `MCP-Protocol-Version`.

Sanctum-токен остаётся только на backend. Встроенная страница чата доступна по
`http://localhost:8086` и не требует Node.js или отдельной сборки.

## Запуск

```bash
cp .env.example .env
# Заполните MCP_URL и MCP_AUTH_TOKEN для рабочего сервера.
docker compose up --build mcp-client-http
```

Для встроенного тестового MCP-сервера оставьте значения из `.env.example` и
запустите оба сервиса: `docker compose up --build mcp-server mcp-client-http`.

Ollama должна быть доступна по `OLLAMA_HOST_URL`, а модель из `QWEN_MODEL`
должна быть заранее загружена.

Основные переменные:

| Переменная | Назначение |
| --- | --- |
| `MCP_URL` | Полный URL стороннего MCP endpoint |
| `MCP_AUTH_TOKEN` | Sanctum-токен внешнего MCP; в browser API не выдаётся |
| `MCP_TIMEOUT` | Таймаут запроса в секундах |
| `OLLAMA_HOST_URL` | URL Ollama без `/v1` для Docker Compose |
| `QWEN_MODEL` | Имя chat-модели с поддержкой tools |
| `WEB_API_TOKEN` | Необязательный отдельный токен browser API |
| `CORS_ORIGINS` | Разрешённые origin внешнего Vue-приложения через запятую |
| `CONVERSATION_TTL` | Время жизни истории в памяти, секунды |

Если сервис публикуется не только на localhost, задайте `WEB_API_TOKEN` или
поставьте перед ним обычную авторизацию reverse proxy. Это отдельный секрет —
не используйте для него `MCP_AUTH_TOKEN`.

## Browser API

### `POST /api/chat`

Запрос:

```json
{
  "message": "Во сколько пришёл сотрудник 00001 сегодня?",
  "conversationId": null
}
```

Ответ имеет `Content-Type: text/event-stream`. Возможные события:

- `session` — созданный или продолженный `conversationId`;
- `status` — текстовый статус;
- `tool` — начало/результат вызова MCP-инструмента;
- `message` — итоговый ответ ассистента;
- `error` — ошибка запроса;
- `done` — поток завершён.

Это SSE в потоковом ответе на `POST`, поэтому в браузере используется `fetch`,
а не `EventSource` (он умеет только `GET` и не позволяет передать тело запроса).

Минимальный пример для Vue 3:

```js
const response = await fetch("http://localhost:8086/api/chat", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    // "X-Client-Token": "отдельный-WEB_API_TOKEN",
  },
  body: JSON.stringify({
    message: prompt.value,
    conversationId: conversationId.value,
  }),
});

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = "";

while (true) {
  const { value, done } = await reader.read();
  buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
  buffer = buffer.replaceAll("\r\n", "\n");
  let end;
  while ((end = buffer.indexOf("\n\n")) >= 0) {
    const block = buffer.slice(0, end);
    buffer = buffer.slice(end + 2);
    const event = block.match(/^event:\s*(.+)$/m)?.[1];
    const data = JSON.parse(block.match(/^data:\s*(.+)$/m)?.[1] || "{}");
    if (event === "session") conversationId.value = data.conversationId;
    if (event === "message") messages.value.push(data.content);
  }
  if (done) break;
}
```

Дополнительно доступны:

- `GET /api/tools` — проверить соединение и получить актуальные MCP tools;
- `DELETE /api/conversations/{id}` — удалить историю browser-диалога;
- `GET /api/health` — liveness-проверка без подключения к MCP и Ollama;
- `GET /docs` — OpenAPI-схема HTTP API.

История диалогов хранится в памяти процесса. Для нескольких реплик Docker
Compose запускайте один worker; для production-масштабирования хранилище следует
заменить на Redis или базу данных.

## Логи

При `LOG_LEVEL=INFO` сервис выводит в консоль тела browser-, LLM- и MCP-запросов
и ответов, включая SSE-события и результаты инструментов. Заголовки авторизации,
`MCP_AUTH_TOKEN`, `X-Client-Token` и полный ID MCP-сессии не логируются.

```bash
docker compose logs -f mcp-client-http
```
