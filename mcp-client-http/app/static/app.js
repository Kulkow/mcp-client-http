const form = document.querySelector("#chat-form");
const prompt = document.querySelector("#prompt");
const send = document.querySelector("#send");
const messages = document.querySelector("#messages");
const activity = document.querySelector("#activity");
const tokenInput = document.querySelector("#client-token");
const newChat = document.querySelector("#new-chat");

let conversationId = sessionStorage.getItem("mcp-conversation-id");
let controller = null;

tokenInput.value = sessionStorage.getItem("mcp-client-token") || "";
tokenInput.addEventListener("change", () => {
  sessionStorage.setItem("mcp-client-token", tokenInput.value);
});

function addMessage(role, content) {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "Вы" : "FP";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = content;
  article.append(avatar, bubble);
  messages.append(article);
  article.scrollIntoView({ behavior: "smooth", block: "end" });
}

function setActivity(text = "") {
  activity.textContent = text;
  activity.hidden = !text;
}

function parseEventBlock(block) {
  let event = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  return { event, data: JSON.parse(data.join("\n")) };
}

function handleEvent({ event, data }) {
  if (event === "session") {
    conversationId = data.conversationId;
    sessionStorage.setItem("mcp-conversation-id", conversationId);
  } else if (event === "status") {
    setActivity(data.message);
  } else if (event === "tool") {
    const suffix = data.state === "start" ? "вызывается…" : "готов";
    setActivity(`Инструмент ${data.name}: ${suffix}`);
  } else if (event === "message") {
    addMessage("assistant", data.content);
    setActivity();
  } else if (event === "error") {
    addMessage("assistant", `Ошибка: ${data.message}`);
    setActivity();
  } else if (event === "done") {
    setActivity();
  }
}

async function readSse(response) {
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  if (!response.body) throw new Error("Браузер не поддерживает потоковые ответы");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    buffer = buffer.replaceAll("\r\n", "\n");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const parsed = parseEventBlock(block);
      if (parsed) handleEvent(parsed);
    }
    if (done) break;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = prompt.value.trim();
  if (!text || controller) return;

  addMessage("user", text);
  prompt.value = "";
  prompt.style.height = "auto";
  send.disabled = true;
  controller = new AbortController();
  setActivity("Отправляю запрос…");

  const headers = { "Content-Type": "application/json" };
  if (tokenInput.value) headers["X-Client-Token"] = tokenInput.value;

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers,
      body: JSON.stringify({ message: text, conversationId }),
      signal: controller.signal,
    });
    await readSse(response);
  } catch (error) {
    if (error.name !== "AbortError") addMessage("assistant", `Ошибка: ${error.message}`);
    setActivity();
  } finally {
    controller = null;
    send.disabled = false;
    prompt.focus();
  }
});

prompt.addEventListener("input", () => {
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 180)}px`;
});

prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

newChat.addEventListener("click", async () => {
  if (controller) controller.abort();
  const oldId = conversationId;
  conversationId = null;
  sessionStorage.removeItem("mcp-conversation-id");
  messages.innerHTML = "";
  addMessage("assistant", "Новый диалог начат. Чем помочь?");
  setActivity();
  if (oldId) {
    const headers = {};
    if (tokenInput.value) headers["X-Client-Token"] = tokenInput.value;
    fetch(`/api/conversations/${encodeURIComponent(oldId)}`, {
      method: "DELETE",
      headers,
    }).catch(() => {});
  }
});

