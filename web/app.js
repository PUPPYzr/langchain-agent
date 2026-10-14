/**
 * Weather Agent Web UI.
 *
 * Talks to the existing Python HTTP boundary (`service/`) and renders only what
 * that boundary actually reports. The answer is rendered once it arrives: the
 * backend does not emit token deltas, so no fake typing animation is used.
 */

import * as api from "./api.js";
import { renderMarkdown } from "./markdown.js";

const THEME_KEY = "weather-agent:theme";

const el = {
  app: document.getElementById("app"),
  sidebar: document.querySelector(".sidebar"),
  messages: document.getElementById("messages"),
  sessionList: document.getElementById("session-list"),
  composer: document.getElementById("composer"),
  input: document.getElementById("input"),
  send: document.getElementById("send"),
  stop: document.getElementById("stop"),
  hint: document.getElementById("hint"),
  newSession: document.getElementById("new-session"),
  collapse: document.getElementById("collapse"),
  expand: document.getElementById("expand"),
  settingsBtn: document.getElementById("settings-btn"),
  settings: document.getElementById("settings"),
  apiKey: document.getElementById("api-key"),
  saveKey: document.getElementById("save-key"),
  clearKey: document.getElementById("clear-key"),
  themeBtn: document.getElementById("theme-btn"),
  contextMode: document.getElementById("context-mode"),
  toast: document.getElementById("toast"),
};

const state = {
  sessions: [],
  activeId: null,
  streaming: false,
  runId: null,
  controller: null,
  lastUserMessage: "",
  nearBottom: true,
  contextMode: "unknown",
};

/* ---------- small helpers ---------- */

let toastTimer = null;
function toast(message) {
  el.toast.textContent = message;
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.toast.hidden = true;
  }, 2600);
}

function timestamp(iso) {
  const date = iso ? new Date(iso) : new Date();
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* ignore storage failures */
  }
}

function initTheme() {
  let stored = null;
  try {
    stored = localStorage.getItem(THEME_KEY);
  } catch {
    stored = null;
  }
  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  applyTheme(stored || (prefersDark ? "dark" : "light"));
}

/* ---------- sidebar ---------- */

function renderSessions() {
  el.sessionList.replaceChildren();
  if (!state.sessions.length) {
    const empty = document.createElement("li");
    empty.className = "sidebar__empty";
    empty.textContent = "还没有对话，点击「新建对话」开始。";
    el.sessionList.append(empty);
    return;
  }
  for (const session of state.sessions) {
    const item = document.createElement("li");
    item.className = "session-item";
    if (session.session_id === state.activeId) item.classList.add("is-active");

    const open = document.createElement("button");
    open.type = "button";
    open.className = "session-item__title";
    open.style.cssText = "all:unset;cursor:pointer;flex:1;";
    open.textContent = session.title || "新对话";
    open.title = session.title || "新对话";
    open.addEventListener("click", () => selectSession(session.session_id));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "session-item__delete";
    remove.textContent = "✕";
    remove.setAttribute("aria-label", `删除对话：${session.title || "新对话"}`);
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      removeSession(session.session_id);
    });

    item.append(open, remove);
    el.sessionList.append(item);
  }
}

/* ---------- message rendering ---------- */

function nearBottom() {
  const { scrollTop, scrollHeight, clientHeight } = el.messages;
  return scrollHeight - (scrollTop + clientHeight) < 120;
}

function scrollToBottom() {
  el.messages.scrollTop = el.messages.scrollHeight;
}

function shouldStick() {
  return state.nearBottom;
}

function welcome() {
  el.messages.replaceChildren();
  const section = document.createElement("section");
  section.className = "welcome";
  const heading = document.createElement("h1");
  heading.textContent = "今天想了解哪里的天气？";
  const text = document.createElement("p");
  text.textContent =
    "这是一个真实运行的天气智能体：它会解析地点、调用 Open-Meteo 获取实时数据，" +
    "再给出中文说明。执行过程会实时展示在回答上方。";
  const chips = document.createElement("div");
  chips.className = "welcome__examples";
  for (const example of [
    "合肥今天天气怎么样？",
    "北京未来三天天气怎么样？",
    "比较合肥、上海和北京的天气",
  ]) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "example-chip";
    chip.textContent = example;
    chip.addEventListener("click", () => {
      el.input.value = example;
      autoResize();
      send(example);
    });
    chips.append(chip);
  }
  section.append(heading, text);

  if (state.contextMode === "stateless") {
    const notice = document.createElement("p");
    notice.style.color = "var(--danger)";
    notice.textContent =
      "当前运行时未启用 Checkpoint，多轮上下文不会保留。请设置 AGENT_RUNTIME_TYPE=langgraph 后重启服务。";
    section.append(notice);
  }
  section.append(chips);
  el.messages.append(section);
}

function userMessage(text) {
  const wrapper = document.createElement("article");
  wrapper.className = "msg msg--user";
  const avatar = document.createElement("div");
  avatar.className = "msg__avatar";
  avatar.textContent = "我";
  const body = document.createElement("div");
  body.className = "msg__body";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  const meta = document.createElement("div");
  meta.className = "msg__meta";
  meta.textContent = timestamp();
  body.append(bubble, meta);
  wrapper.append(avatar, body);
  if (shouldStick()) {
    el.messages.append(wrapper);
    scrollToBottom();
  } else {
    el.messages.append(wrapper);
  }
  return wrapper;
}

function assistantMessage() {
  const wrapper = document.createElement("article");
  wrapper.className = "msg msg--assistant";
  const avatar = document.createElement("div");
  avatar.className = "msg__avatar";
  avatar.textContent = "AI";
  const body = document.createElement("div");
  body.className = "msg__body";

  const trace = document.createElement("details");
  trace.className = "trace";
  trace.open = true;
  const summary = document.createElement("summary");
  summary.textContent = "执行过程";
  const list = document.createElement("ul");
  list.className = "trace__list";
  trace.append(summary, list);

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const typing = document.createElement("span");
  typing.className = "typing";
  typing.innerHTML = "<i></i><i></i><i></i>";
  bubble.append(typing);

  const meta = document.createElement("div");
  meta.className = "msg__meta";
  meta.textContent = timestamp();

  body.append(trace, bubble, meta);
  wrapper.append(avatar, body);
  el.messages.append(wrapper);
  if (shouldStick()) scrollToBottom();

  return {
    wrapper,
    trace,
    list,
    summary,
    bubble,
    meta,
    contentRendered: false,
    statusStep: addStatusStep(list),
    steps: new Map(),
  };
}

function addStatusStep(list) {
  const item = document.createElement("li");
  item.className = "trace__step is-running";
  const dot = document.createElement("span");
  dot.className = "trace__dot";
  const text = document.createElement("span");
  text.textContent = "正在处理…";
  item.append(dot, text);
  list.append(item);
  return { item, text };
}

function addToolStep(refs, event) {
  const item = document.createElement("li");
  item.className = "trace__step is-running";
  const dot = document.createElement("span");
  dot.className = "trace__dot";
  const box = document.createElement("div");
  const name = document.createElement("div");
  name.className = "trace__name";
  name.textContent = `调用工具 ${event.name}`;
  const args = document.createElement("div");
  args.className = "trace__args";
  args.textContent = JSON.stringify(event.arguments ?? {});
  box.append(name, args);
  item.append(dot, box);
  refs.list.append(item);
  refs.steps.set(event.id, { item, name, box });
  if (shouldStick()) scrollToBottom();
}

function completeToolStep(refs, event) {
  const step = refs.steps.get(event.id);
  if (!step) {
    addToolStep(refs, { id: event.id, name: event.name, arguments: {} });
    return completeToolStep(refs, event);
  }
  step.item.classList.remove("is-running");
  step.item.classList.add(event.ok ? "is-ok" : "is-error");
  step.name.textContent = `${event.ok ? "已完成" : "失败"} ${event.name}`;
  const result = document.createElement("div");
  result.className = "trace__result";
  result.textContent = event.error_type
    ? `${event.error_type}: ${event.summary}`
    : event.summary;
  step.box.append(result);
  if (shouldStick()) scrollToBottom();
}

function showAnswer(refs, content) {
  refs.bubble.replaceChildren();
  refs.bubble.innerHTML = renderMarkdown(content);
  refs.contentRendered = true;
  attachCopyButton(refs);
}

function showError(refs, message, onRetry) {
  refs.bubble.replaceChildren();
  const alert = document.createElement("div");
  alert.className = "alert";
  const text = document.createElement("span");
  text.textContent = message;
  alert.append(text);
  if (onRetry) {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "ghost-btn";
    retry.textContent = "重新发送";
    retry.addEventListener("click", onRetry);
    alert.append(retry);
  }
  refs.bubble.append(alert);
}

function attachCopyButton(refs) {
  if (refs.wrapper.querySelector(".msg__meta .copy-answer")) return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "ghost-btn copy-answer";
  button.style.flex = "0 0 auto";
  button.textContent = "复制回答";
  button.addEventListener("click", async () => {
    const text = refs.bubble.innerText.trim();
    try {
      await navigator.clipboard.writeText(text);
      toast("已复制回答");
    } catch {
      toast("复制失败，请手动选择文本");
    }
  });
  refs.meta.prepend(button);
}

// Delegate code-block copy buttons (they are recreated on every render).
el.messages.addEventListener("click", async (event) => {
  const button = event.target.closest(".copy-code");
  if (!button) return;
  const code = button.closest(".code")?.querySelector("code");
  if (!code) return;
  try {
    await navigator.clipboard.writeText(code.textContent);
    toast("已复制代码");
  } catch {
    toast("复制失败，请手动选择文本");
  }
});

/* ---------- sessions ---------- */

async function loadSessions() {
  const data = await api.listSessions();
  state.sessions = data.sessions || [];
  if (!state.sessions.length) {
    const created = await api.createSession();
    state.sessions = [created];
    state.activeId = created.session_id;
  } else if (!state.activeId || !state.sessions.some((s) => s.session_id === state.activeId)) {
    state.activeId = state.sessions[0].session_id;
  }
  renderSessions();
}

async function selectSession(sessionId) {
  if (state.activeId === sessionId) {
    if (window.innerWidth <= 820) el.app.classList.add("is-collapsed");
    return;
  }
  abortStreaming();
  state.activeId = sessionId;
  renderSessions();
  if (window.innerWidth <= 820) el.app.classList.add("is-collapsed");
  await renderTranscript(sessionId);
}

async function renderTranscript(sessionId) {
  el.messages.replaceChildren();
  const data = await api.sessionMessages(sessionId);
  const messages = data.messages || [];
  if (!messages.length) {
    welcome();
    return;
  }
  for (const message of messages) {
    if (message.role === "user") {
      userMessage(message.content);
    } else {
      const refs = assistantMessage();
      showAnswer(refs, message.content);
      refs.trace.remove();
      refs.statusStep = null;
      refs.meta.textContent = timestamp(message.created_at);
    }
  }
  scrollToBottom();
  state.nearBottom = true;
}

async function removeSession(sessionId) {
  try {
    await api.deleteSession(sessionId);
  } catch (error) {
    toast(error.message || "删除失败");
    return;
  }
  state.sessions = state.sessions.filter((s) => s.session_id !== sessionId);
  if (state.activeId === sessionId) {
    abortStreaming();
    state.activeId = state.sessions[0]?.session_id ?? null;
    if (state.activeId) await renderTranscript(state.activeId);
    else welcome();
  }
  renderSessions();
  toast("已删除对话");
}

async function newSession() {
  abortStreaming();
  const created = await api.createSession();
  state.sessions = [created, ...state.sessions];
  state.activeId = created.session_id;
  renderSessions();
  welcome();
  el.messages.scrollTop = 0;
  state.nearBottom = true;
  if (window.innerWidth <= 820) el.app.classList.add("is-collapsed");
}

/* ---------- streaming ---------- */

function setStreaming(value) {
  state.streaming = value;
  el.send.disabled = value;
  el.stop.hidden = !value;
  el.input.disabled = value;
  el.hint.textContent = value
    ? "正在生成，可点击「停止生成」中断"
    : "Enter 发送 · Shift+Enter 换行";
  if (!value) {
    state.runId = null;
    el.input.focus();
  }
}

function abortStreaming() {
  state.controller?.abort();
  state.controller = null;
  state.runId = null;
  state.streaming = false;
  el.send.disabled = false;
  el.stop.hidden = true;
  el.input.disabled = false;
}

async function send(text) {
  const message = (text ?? el.input.value).trim();
  if (!message || state.streaming) return;
  if (!api.hasApiKey()) {
    toast("请先在「连接设置」中填写访问密钥");
    el.settings.showModal();
    return;
  }
  if (!state.activeId) {
    await newSession();
  }

  const sessionId = state.activeId;
  state.lastUserMessage = message;
  el.input.value = "";
  autoResize();

  if (el.messages.querySelector(".welcome")) el.messages.replaceChildren();
  userMessage(message);
  const refs = assistantMessage();
  setStreaming(true);

  const controller = new AbortController();
  state.controller = controller;

  const onRetry = () => send(state.lastUserMessage);

  try {
    await api.streamChat({
      sessionId,
      message,
      signal: controller.signal,
      onEvent: (event) => {
        // A different session became active: drop late events from this turn.
        if (state.activeId !== sessionId) return;
        handleEvent(event, refs, onRetry);
      },
    });
  } catch (error) {
    if (error.name === "AbortError") {
      finishTurn(refs, "已停止");
    } else {
      showError(refs, error.message || "请求失败", onRetry);
    }
  } finally {
    if (state.controller === controller) {
      state.controller = null;
      setStreaming(false);
      await refreshSessionTitles();
    }
  }
}

function handleEvent(event, refs, onRetry) {
  switch (event.type) {
    case "started":
      state.runId = event.run_id;
      break;
    case "status": {
      const labels = {
        started: "已开始处理…",
        working: "正在处理…",
        retrying: `重试第 ${event.attempt ?? "?"} 次…`,
        completed: "已完成",
        cancelled: "已取消",
        interrupted: "已暂停（等待人工恢复）",
        resumed: "已恢复",
      };
      const label = labels[event.phase] || event.phase;
      if (refs.statusStep) refs.statusStep.text.textContent = label;
      if (event.phase === "completed" || event.phase === "cancelled") {
        refs.statusStep?.item.classList.remove("is-running");
        refs.statusStep?.item.classList.add(
          event.phase === "completed" ? "is-ok" : "is-error",
        );
        refs.trace.open = false;
      }
      break;
    }
    case "tool_call":
      addToolStep(refs, event);
      if (refs.statusStep) refs.statusStep.text.textContent = `正在调用 ${event.name}…`;
      break;
    case "tool_result":
      completeToolStep(refs, event);
      if (refs.statusStep) refs.statusStep.text.textContent = "正在整理回答…";
      break;
    case "answer":
      showAnswer(refs, event.content);
      break;
    case "error":
      showError(refs, event.message || "Agent 执行失败", onRetry);
      refs.statusStep?.item.classList.remove("is-running");
      refs.statusStep?.item.classList.add("is-error");
      break;
    case "done":
      finishTurn(refs);
      break;
    default:
      break;
  }
}

function finishTurn(refs, label) {
  if (refs.finished) return;
  refs.finished = true;
  if (refs.statusStep) {
    refs.statusStep.item.classList.remove("is-running");
    refs.statusStep.text.textContent =
      label || refs.statusStep.text.textContent || "已完成";
  }
  if (!refs.list.children.length) refs.trace.remove();
  else refs.trace.open = false;
  refs.meta.textContent = timestamp();
}

async function refreshSessionTitles() {
  try {
    const data = await api.listSessions();
    state.sessions = data.sessions || state.sessions;
    renderSessions();
  } catch {
    /* title refresh is cosmetic */
  }
}

async function stop() {
  if (!state.streaming) return;
  const runId = state.runId;
  if (runId) {
    try {
      await api.cancelRun(runId);
    } catch (error) {
      if (error.status !== 409 && error.status !== 404) {
        toast(error.message || "停止失败");
      }
    }
  }
  state.controller?.abort();
}

/* ---------- composer ---------- */

function autoResize() {
  el.input.style.height = "auto";
  el.input.style.height = `${Math.min(el.input.scrollHeight, 200)}px`;
}

el.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  send();
});

el.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});

el.input.addEventListener("input", autoResize);
el.stop.addEventListener("click", stop);

el.messages.addEventListener("scroll", () => {
  state.nearBottom = nearBottom();
});

/* ---------- chrome ---------- */

el.newSession.addEventListener("click", () => {
  newSession().catch((error) => toast(error.message || "新建对话失败"));
});

el.collapse.addEventListener("click", () => el.app.classList.add("is-collapsed"));
el.expand.addEventListener("click", () => el.app.classList.remove("is-collapsed"));

el.themeBtn.addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});

el.settingsBtn.addEventListener("click", () => {
  el.apiKey.value = api.getApiKey();
  el.settings.showModal();
});

el.saveKey.addEventListener("click", () => {
  api.setApiKey(el.apiKey.value.trim());
  el.settings.close();
  toast("已保存访问密钥");
});

el.clearKey.addEventListener("click", () => {
  api.setApiKey("");
  el.apiKey.value = "";
  toast("已清除访问密钥");
});

window.addEventListener("beforeunload", () => state.controller?.abort());

/* ---------- boot ---------- */

async function boot() {
  initTheme();
  if (window.innerWidth <= 820) el.app.classList.add("is-collapsed");
  try {
    const info = await api.health();
    state.contextMode = info.context_mode || "unknown";
    el.contextMode.textContent =
      state.contextMode === "checkpointed"
        ? "多轮上下文已启用"
        : state.contextMode === "stateless"
          ? "未启用多轮上下文"
          : "运行时状态未知";
  } catch {
    el.contextMode.textContent = "服务不可用";
  }

  if (!api.hasApiKey()) {
    welcome();
    renderSessions();
    el.settings.showModal();
    return;
  }
  try {
    await loadSessions();
    await renderTranscript(state.activeId);
  } catch (error) {
    if (error.status === 401) {
      welcome();
      toast("访问密钥无效，请重新配置");
      el.settings.showModal();
    } else {
      welcome();
      toast(error.message || "加载会话失败");
    }
  }
}

boot();
