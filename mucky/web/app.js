// Browser front end for `mucky serve`. The server keeps the connections, logs,
// scrollback and command history; this page only renders them and keeps
// per-viewer state (active tab, unread counts, scroll position, history cursor).
// The protocol is described in mucky/server.py.

import { renderLine } from "./ansi.js";

// Output lines kept in the page per session (the server keeps its own copy).
const MAX_LINES = 5000;
// Pixels from the bottom that still count as "following" live output.
const FOLLOW_SLACK = 40;
const TOKEN_KEY = "mucky.token";
const HELP_TEXT =
  "Commands: /connect <character>  /disconnect  /reconnect  /reload  /clear  /quit (detach this page)\n" +
  "Keys: Enter send, Shift+Enter new line, Up/Down history, Alt+1..9 or Ctrl+PageUp/PageDown switch character";

const $ = (id) => document.getElementById(id);
const tabsEl = $("tabs");
const outputsEl = $("outputs");
const emptyEl = $("empty");
const inputEl = $("input");
const counterEl = $("counter");
const linkEl = $("link");
const openBtn = $("open-btn");
const openMenu = $("open-menu");
const noticesEl = $("notices");
const loginDlg = $("login");

let ws = null;
let token = null;
let profiles = [];
// conn_id -> {profile, state, history, tab, output, unread, histPos, draft}
const sessions = new Map();
let active = null;
let retryDelay = 1000;
let retryTimer = null;
let detached = false;

// ----- storage (per-viewer convenience only) ---------------------------------

function loadToken() {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}

function saveToken(value) {
  try {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* storage unavailable: token lives for this page only */ }
}

// ----- connection to the mucky server ----------------------------------------

function wsUrl() {
  const url = new URL("ws", location.href);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}

function connect() {
  clearTimeout(retryTimer);
  detached = false;
  setLink("attaching…", false);
  const sock = new WebSocket(wsUrl());
  ws = sock;
  let authFailed = false;

  sock.onopen = () => sock.send(JSON.stringify({ type: "hello", token }));

  sock.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "error") {
      // The login box reports a rejected token; anything else is a notice.
      if (/auth/i.test(msg.text)) authFailed = true;
      else notice(msg.text, "error");
      return;
    }
    handle(msg);
  };

  sock.onclose = () => {
    if (ws !== sock) return;
    ws = null;
    if (authFailed) {
      saveToken(null);
      askToken("That token was not accepted.");
      setLink("not attached", true);
    } else if (detached) {
      setLink("detached · click to reattach", true);
    } else {
      setLink(`server unreachable · retrying`, true);
      retryTimer = setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30000);
    }
  };
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
    return true;
  }
  notice("Not attached to the server; that wasn't sent.", "warning");
  return false;
}

function setLink(text, down) {
  linkEl.textContent = text;
  linkEl.classList.toggle("down", down);
  linkEl.style.cursor = down && detached ? "pointer" : "";
}

linkEl.addEventListener("click", () => {
  if (!ws && detached) connect();
});

// ----- server messages -------------------------------------------------------

function handle(msg) {
  switch (msg.type) {
    case "welcome": {
      retryDelay = 1000;
      setLink("attached", false);
      profiles = msg.profiles;
      const previous = active;
      for (const id of [...sessions.keys()]) removeSession(id);
      for (const s of msg.sessions) {
        const sess = addSession(s.conn_id, s.profile, s.state, s.history);
        appendLines(sess, s.scrollback, false);
      }
      activate(sessions.has(previous) ? previous : sessions.keys().next().value ?? null);
      break;
    }
    case "lines": {
      const sess = sessions.get(msg.conn_id);
      if (sess) appendLines(sess, msg.lines, true);
      break;
    }
    case "state": {
      const sess = sessions.get(msg.conn_id);
      if (sess) { sess.state = msg.state; drawTab(sess); }
      break;
    }
    case "opened":
      if (!sessions.has(msg.conn_id)) {
        addSession(msg.conn_id, msg.profile, "disconnected", []);
        if (active === null) activate(msg.conn_id);
      }
      break;
    case "closed": {
      removeSession(msg.conn_id);
      if (active === msg.conn_id) activate(sessions.keys().next().value ?? null);
      break;
    }
    case "cleared": {
      const sess = sessions.get(msg.conn_id);
      if (sess) { sess.output.replaceChildren(); sess.unread = 0; drawTab(sess); }
      break;
    }
    case "focus":
      if (sessions.has(msg.conn_id)) activate(msg.conn_id);
      break;
    case "reloaded":
      profiles = msg.profiles;
      for (const sess of sessions.values()) {
        const p = profiles.find((x) => x.id === sess.id);
        if (p) { sess.profile = p; drawTab(sess); }
      }
      break;
    case "notice":
      notice(msg.text, msg.severity, msg.timeout);
      break;
    case "help":
      showHelp();
      break;
  }
}

// ----- sessions, tabs and output ---------------------------------------------

function addSession(id, profile, state, history) {
  const tab = document.createElement("button");
  tab.type = "button";
  tab.className = "tab";
  tab.setAttribute("role", "tab");
  tab.innerHTML = '<span class="dot"></span><span class="name"></span><span class="unread"></span>';
  tab.addEventListener("click", () => { activate(id); inputEl.focus(); });
  tabsEl.appendChild(tab);

  const output = document.createElement("div");
  output.className = "output";
  output.setAttribute("role", "log");
  output.hidden = true;
  outputsEl.appendChild(output);

  const sess = {
    id, profile, state, history: [...history], tab, output,
    unread: 0, histPos: history.length, draft: "",
  };
  sessions.set(id, sess);
  drawTab(sess);
  updateEmpty();
  return sess;
}

function removeSession(id) {
  const sess = sessions.get(id);
  if (!sess) return;
  sess.tab.remove();
  sess.output.remove();
  sessions.delete(id);
  updateEmpty();
}

function drawTab(sess) {
  const p = sess.profile;
  sess.tab.querySelector(".name").textContent = p ? p.tab_name : sess.id;
  sess.tab.title = p ? `${p.char_name} on ${p.server_name} (${sess.state})` : sess.state;
  sess.tab.querySelector(".dot").className = `dot ${sess.state}`;
  sess.tab.querySelector(".unread").textContent = sess.unread ? String(sess.unread) : "";
  sess.tab.setAttribute("aria-selected", String(sess.id === active));
}

function activate(id) {
  const prev = sessions.get(active);
  if (prev) prev.draft = inputEl.value;
  active = id;
  for (const sess of sessions.values()) {
    const on = sess.id === id;
    sess.output.hidden = !on;
    if (on) {
      sess.unread = 0;
      inputEl.value = sess.draft;
      inputChanged();
    }
    drawTab(sess);
  }
  const sess = sessions.get(id);
  if (sess) {
    sess.tab.scrollIntoView({ block: "nearest", inline: "nearest" });
    // Newly shown panes start at the bottom unless the viewer scrolled up.
    if (sess.follow !== false) scrollToBottom(sess);
    document.title = `${sess.profile ? sess.profile.tab_name : id} · mucky`;
  } else {
    document.title = "mucky";
  }
}

function updateEmpty() {
  emptyEl.hidden = sessions.size > 0;
}

function atBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_SLACK;
}

function scrollToBottom(sess) {
  sess.output.scrollTop = sess.output.scrollHeight;
}

function appendLines(sess, lines, live) {
  if (!lines.length) return;
  const el = sess.output;
  const visible = sess.id === active;
  const follow = !visible || atBottom(el);
  const frag = document.createDocumentFragment();
  for (const line of lines) frag.appendChild(renderLine(line));
  el.appendChild(frag);
  let extra = el.childElementCount - MAX_LINES;
  while (extra-- > 0) el.firstElementChild.remove();
  if (follow) scrollToBottom(sess);
  if (live && !visible) {
    sess.unread += lines.filter((l) => !l.status).length;
    drawTab(sess);
  }
}

// Remember per pane whether the viewer is following the bottom, so switching
// tabs doesn't yank someone who was reading back.
outputsEl.addEventListener("scroll", (ev) => {
  const sess = [...sessions.values()].find((s) => s.output === ev.target);
  if (sess && !sess.output.hidden) sess.follow = atBottom(sess.output);
}, true);

// ----- notices and help ------------------------------------------------------

function notice(text, severity = "information", timeout = null) {
  const div = document.createElement("div");
  div.className = `notice ${severity}`;
  div.textContent = text;
  div.addEventListener("click", () => div.remove());
  noticesEl.appendChild(div);
  setTimeout(() => div.remove(), (timeout ?? 5) * 1000);
}

function showHelp() {
  const sess = sessions.get(active);
  if (!sess) { notice(HELP_TEXT, "information", 12); return; }
  const div = document.createElement("div");
  div.className = "help";
  div.textContent = HELP_TEXT;
  sess.output.appendChild(div);
  scrollToBottom(sess);
}

// ----- open-character menu ---------------------------------------------------

function buildOpenMenu() {
  openMenu.replaceChildren();
  const closed = profiles.filter((p) => !sessions.has(p.id));
  if (!closed.length) {
    const none = document.createElement("div");
    none.className = "none";
    none.textContent = profiles.length ? "All characters are open." : "No characters configured.";
    openMenu.appendChild(none);
  }
  for (const p of closed) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.innerHTML = '<span class="n"></span> <span class="sub"></span>';
    b.querySelector(".n").textContent = p.tab_name;
    b.querySelector(".sub").textContent = `${p.char_name}@${p.server_name}`;
    b.addEventListener("click", () => {
      send({ type: "open", conn_id: p.id });
      closeOpenMenu();
      inputEl.focus();
    });
    openMenu.appendChild(b);
  }
}

function closeOpenMenu() {
  openMenu.hidden = true;
  openBtn.setAttribute("aria-expanded", "false");
}

openBtn.addEventListener("click", (ev) => {
  ev.stopPropagation();
  if (openMenu.hidden) {
    buildOpenMenu();
    openMenu.hidden = false;
    openBtn.setAttribute("aria-expanded", "true");
    openMenu.querySelector("button")?.focus();
  } else {
    closeOpenMenu();
  }
});
document.addEventListener("click", (ev) => {
  if (!openMenu.hidden && !openMenu.contains(ev.target)) closeOpenMenu();
});
openMenu.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { closeOpenMenu(); openBtn.focus(); }
});

// ----- input line ------------------------------------------------------------

// Called whenever the input's text changes, typed or set from code: grow the
// box to fit and refresh the character count, as the Textual app did.
function inputChanged() {
  inputEl.style.height = "auto";
  inputEl.style.height = `${inputEl.scrollHeight}px`;
  counterEl.textContent = inputEl.value.length;
}

function submit() {
  const text = inputEl.value;
  const sess = sessions.get(active);
  // A pasted block goes up one line at a time, like typing each one.
  const lines = text.split(/\r?\n/);
  for (const line of lines) {
    if (line.trim() === "/quit") detached = true;
    if (!send({ type: "input", conn_id: active, text: line })) return;
    if (sess && !line.startsWith("/") && line.trim() &&
        sess.history[sess.history.length - 1] !== line) {
      sess.history.push(line);
    }
  }
  inputEl.value = "";
  if (sess) { sess.histPos = sess.history.length; sess.draft = ""; sess.follow = true; scrollToBottom(sess); }
  inputChanged();
}

function recall(step) {
  const sess = sessions.get(active);
  if (!sess || !sess.history.length) return;
  if (sess.histPos === sess.history.length) sess.draft = inputEl.value;
  sess.histPos = Math.max(0, Math.min(sess.history.length, sess.histPos + step));
  inputEl.value = sess.histPos === sess.history.length ? sess.draft : sess.history[sess.histPos];
  inputChanged();
  inputEl.setSelectionRange(inputEl.value.length, inputEl.value.length);
}

function cycle(step) {
  const ids = [...sessions.keys()];
  if (!ids.length) return;
  const i = ids.indexOf(active);
  activate(ids[(i + step + ids.length) % ids.length]);
}

inputEl.addEventListener("input", inputChanged);

inputEl.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
    ev.preventDefault();
    submit();
  } else if (ev.key === "ArrowUp" && !inputEl.value.slice(0, inputEl.selectionStart).includes("\n")) {
    ev.preventDefault();
    recall(-1);
  } else if (ev.key === "ArrowDown" && !inputEl.value.slice(inputEl.selectionEnd).includes("\n")) {
    ev.preventDefault();
    recall(1);
  }
});

$("input-form").addEventListener("submit", (ev) => { ev.preventDefault(); submit(); });

document.addEventListener("keydown", (ev) => {
  if (ev.altKey && !ev.ctrlKey && /^Digit[1-9]$/.test(ev.code)) {
    const id = [...sessions.keys()][Number(ev.code.slice(5)) - 1];
    if (id) { ev.preventDefault(); activate(id); }
  } else if (ev.ctrlKey && (ev.key === "PageUp" || ev.key === "PageDown")) {
    ev.preventDefault();
    cycle(ev.key === "PageUp" ? -1 : 1);
  } else if (ev.key === "PageUp" || ev.key === "PageDown") {
    const sess = sessions.get(active);
    if (sess && document.activeElement === inputEl) {
      ev.preventDefault();
      const el = sess.output;
      el.scrollBy({ top: (ev.key === "PageUp" ? -1 : 1) * el.clientHeight * 0.9 });
    }
  }
});

// Typing anywhere (outside the login box) goes to the input line, but don't
// steal a click-drag text selection in the output.
outputsEl.addEventListener("mouseup", () => {
  if (!String(window.getSelection())) inputEl.focus();
});

// ----- login -----------------------------------------------------------------

function askToken(message = "") {
  $("login-error").textContent = message;
  $("token").value = "";
  if (!loginDlg.open) loginDlg.showModal();
  $("token").focus();
}

$("login-form").addEventListener("submit", () => {
  token = $("token").value;
  if ($("remember").checked) saveToken(token);
  connect();
});

loginDlg.addEventListener("cancel", (ev) => ev.preventDefault());
// The dialog hands focus back to whatever had it before; we want the input.
loginDlg.addEventListener("close", () => inputEl.focus());

token = loadToken();
if (token) connect();
else askToken();
inputEl.focus();
