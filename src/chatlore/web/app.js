// ChatLore web interface: plain JavaScript over the REST API, no build step.
"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const number = (value) => Number(value ?? 0).toLocaleString();

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
  return response.json();
}

// -- text ------------------------------------------------------------------------------

/** The Markdown models tend to write: paragraphs, lists, code, bold, and [n] citations. */
function markdown(text) {
  const inline = (line) =>
    esc(line)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[(\d+)\]/g, '<span class="cite" data-n="$1">$1</span>');
  const html = [];
  const parts = String(text).split("```");
  parts.forEach((part, index) => {
    if (index % 2) {
      html.push(`<pre><code>${esc(part.replace(/^[\w+-]*\n/, ""))}</code></pre>`);
      return;
    }
    let list = null;
    let paragraph = [];
    const flush = () => {
      if (paragraph.length) html.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
      paragraph = [];
    };
    const close = () => {
      if (list) html.push(`</${list}>`);
      list = null;
    };
    for (const line of part.split("\n")) {
      const bullet = line.match(/^\s*[-*]\s+(.*)/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (bullet || ordered) {
        flush();
        const kind = bullet ? "ul" : "ol";
        if (list !== kind) {
          close();
          html.push(`<${kind}>`);
          list = kind;
        }
        html.push(`<li>${inline((bullet || ordered)[1])}</li>`);
      } else if (!line.trim()) {
        flush();
        close();
      } else {
        close();
        paragraph.push(line.replace(/^#+\s*/, ""));
      }
    }
    flush();
    close();
  });
  return html.join("");
}

/** Full-text snippets mark matched words with [brackets]; show them highlighted. */
const highlight = (text, marked) => (marked ? esc(text).replace(/\[([^\]]+)\]/g, "<mark>$1</mark>") : esc(text));
/** A readable date. A bare "2026-08-21" is a calendar day, not midnight UTC, so it
 * is read as local time; otherwise it would show as the day before west of UTC. */
const date = (value) => {
  if (!value) return "";
  const moment = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00` : value);
  return moment.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
};

/** Read a server-sent event stream from a fetch response, one event at a time. */
async function* events(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) yield { event, data: JSON.parse(data) };
    }
  }
}

// -- navigation ------------------------------------------------------------------------

const opened = new Set();

/** Show a view; Explore starts on the overview unless ``overview`` is false. */
function show(view, overview = true) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (!opened.has(view)) {
    opened.add(view);
    if (view === "conversations") Conversations.load();
    if (view === "topics") Topics.load();
    if (view === "explore") {
      Explore.start();
      if (overview) Explore.go(null);
    }
  }
  if (view === "explore") Explore.resize();
  if (view === "ask") $("#ask-input").focus();
}

// -- theme -----------------------------------------------------------------------------

const Theme = {
  current() {
    return document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  },

  toggle() {
    const next = this.current() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("chatlore-theme", next);
    } catch {
      // the choice lasts for this page only
    }
    this.render();
    Explore.renderSources();
  },

  render() {
    const dark = this.current() === "dark";
    $("#theme-toggle").classList.toggle("dark", dark);
    $("#theme-toggle").setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    $("#theme-toggle .theme-label").textContent = dark ? "Light mode" : "Dark mode";
  },
};

$("#theme-toggle").addEventListener("click", () => Theme.toggle());
Theme.render();

document.querySelectorAll("nav button[data-view]").forEach((button) => button.addEventListener("click", () => show(button.dataset.view)));
document.querySelectorAll("dialog [data-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
document.querySelectorAll("dialog").forEach((dialog) =>
  dialog.addEventListener("click", (event) => event.target === dialog && dialog.close()),
);

api("/stats")
  .then((stats) => {
    const values = [stats.conversations, stats.entities, stats.topics];
    document.querySelectorAll("#stats dd").forEach((dd, index) => (dd.textContent = number(values[index])));
  })
  .catch(() => {});

// A public server, such as the hosted demo, says so, unless the visitor brought their own library.
api("/library")
  .then((library) => {
    Data.info = library;
    Data.render();
    if (library.import?.running) Data.poll();
    if (!library.public) return;
    if (library.own) {
      $("#ask-hero p").textContent = "Your own conversations, kept on this server only for you. Every answer cites its sources.";
    } else {
      $("#ask-hero p").textContent = "A demo on made-up conversations. Every answer cites the chats it came from.";
      $("#demo-note").hidden = false;
    }
  })
  .catch(() => {});

// -- your data -------------------------------------------------------------------------

const STAGES = {
  waiting: "Waiting to start",
  importing: "Importing conversations",
  embedding: "Preparing search",
  reading: "Reading with the language model",
  summarising: "Summarising entities",
  linking: "Linking names for the same thing",
  topics: "Writing topic reports",
  done: "Done",
  failed: "The import failed",
  cancelled: "Stopped",
};

const Data = {
  info: null,
  timer: null,

  hoursLeft() {
    const left = (new Date(this.info.expires_at) - Date.now()) / 3_600_000;
    return left < 1 ? "in less than an hour" : `in about ${Math.round(left)} hours`;
  },

  render() {
    const info = this.info;
    if (!info) return;
    $("#data-open").hidden = false;
    $("#data-max").textContent = number(info.max_upload_mb);
    $("#data-drop").hidden = !info.uploads;
    $("#data-delete").hidden = !(info.public && info.own);
    const privacy = $("#data-privacy");
    if (info.public && info.uploads) {
      privacy.hidden = false;
      privacy.textContent =
        `Your upload goes to this server only, into a library nobody else can see, tied to this browser. ` +
        `It is deleted after ${info.keep_hours} hours, or now with Delete. Building the knowledge graph ` +
        `sends your conversations to the language model this server uses.`;
    }
    $("#data-meta").textContent = !info.public
      ? "Import exports into this library, or download it."
      : info.own
        ? `Your private library on this server, deleted ${this.hoursLeft()}.`
        : info.uploads
          ? "Try ChatLore on your own conversations."
          : "Download the demo library.";
    const status = info.import;
    $("#data-progress").hidden = !status;
    if (status) this.show(status);
  },

  show(status, uploaded) {
    $("#data-drop").classList.toggle("busy", Boolean(status.running));
    $("#data-stage").textContent = uploaded === undefined ? STAGES[status.stage] || status.stage : "Uploading";
    const bar = $("#data-bar");
    if (uploaded !== undefined) {
      bar.value = uploaded;
      $("#data-count").textContent = `${Math.round(uploaded * 100)}%`;
    } else if (status.total > 0 && status.running) {
      bar.value = Math.min(1, status.done / status.total);
      $("#data-count").textContent = `${number(status.done)} of ${number(status.total)}`;
    } else {
      bar.value = status.running ? 0 : 1;
      bar.toggleAttribute("value", !status.running || status.total > 0);
      $("#data-count").textContent = "";
    }
    const summary = [];
    if (status.file) summary.push(status.file);
    if (status.conversations) summary.push(`${number(status.conversations)} conversations added`);
    const sources = Object.entries(status.sources || {});
    if (sources.length) summary.push(sources.map(([source, count]) => `${number(count)} from ${source}`).join(", "));
    if (status.skipped) summary.push(`${number(status.skipped)} records skipped`);
    const skipped = $("#data-skipped");
    skipped.hidden = !status.skipped_files;
    if (status.skipped_files) {
      $("summary", skipped).textContent = `${number(status.skipped_files)} ${status.skipped_files === 1 ? "file" : "files"} skipped`;
      const shown = status.skipped_shown || [];
      $("ul", skipped).innerHTML =
        shown.map(([path, reason]) => `<li>${esc(path)}: ${esc(reason)}</li>`).join("") +
        (status.skipped_files > shown.length ? `<li>and ${number(status.skipped_files - shown.length)} more</li>` : "");
    }
    $("#data-summary").innerHTML = esc(summary.join(" · ")) + (status.error ? `<br><span class="error">${esc(status.error)}</span>` : "");
    const notes = [...(status.notes || [])];
    $("#data-notes").innerHTML = notes.map((note) => `<li>${esc(note)}</li>`).join("");
    if (status.stage === "done" && !status.running) {
      $("#data-notes").insertAdjacentHTML("beforeend", '<li><button type="button" class="primary" id="data-show">Show the library</button></li>');
      $("#data-show").addEventListener("click", () => location.reload());
    }
  },

  async refresh() {
    this.info = await api("/library");
    this.render();
  },

  poll() {
    clearTimeout(this.timer);
    this.timer = setTimeout(async () => {
      try {
        await this.refresh();
      } catch {
        // try again on the next tick
      }
      if (this.info?.import?.running) this.poll();
    }, 1500);
  },

  /** Upload files, each ``{file, path}`` with its path in the folder it came from, then import them. */
  async upload(files) {
    files = files.filter(({ path }) => !path.split("/").some((part) => SKIPPED_FOLDERS.has(part)));
    if (!files.length) return;
    $("#data-progress").hidden = false;
    const fail = (error) => this.show({ file: label(files), stage: "failed", running: false, error });
    const total = files.reduce((sum, { file }) => sum + file.size, 0);
    if (total > this.info.max_upload_mb * 1_000_000) {
      fail(`That is ${number(Math.ceil(total / 1_000_000))} MB; this server takes up to ${number(this.info.max_upload_mb)} MB at a time.`);
      return;
    }
    const batch = Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
    let sent = 0;
    try {
      for (const [index, item] of files.entries()) {
        const shown = files.length > 1 ? `${index + 1} of ${number(files.length)}: ${item.path}` : item.path;
        await send(`library/files?batch=${batch}`, item.file, item.path, (loaded) =>
          this.show({ file: shown, running: true }, total ? (sent + loaded) / total : 1),
        );
        sent += item.file.size;
      }
      this.show(await send(`library/import?batch=${batch}`, null, label(files)));
      this.poll();
    } catch (error) {
      fail(error.message);
    }
  },

  async remove() {
    if (!confirm("Delete your library from this server now? This cannot be undone.")) return;
    const response = await fetch("library", { method: "DELETE", headers: { "X-ChatLore": "1" } });
    if (response.ok) location.reload();
  },
};

$("#data-open").addEventListener("click", () => {
  Data.refresh().catch(() => {});
  $("#data").showModal();
});
// Folders nobody means to import, left out before anything is uploaded.
const SKIPPED_FOLDERS = new Set([".git", "node_modules", "__MACOSX", ".venv", "__pycache__", ".Trash"]);

const label = (files) => (files.length === 1 ? files[0].path : `${number(files.length)} files`);

/** POST a file (or nothing) with the interface's header, reporting upload progress; the answer's JSON. */
function send(url, file, path, progress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", url);
    request.setRequestHeader("X-ChatLore", "1");
    request.setRequestHeader("X-Filename", encodeURIComponent(path));
    request.setRequestHeader("Content-Type", "application/octet-stream");
    if (progress) request.upload.onprogress = (event) => event.lengthComputable && progress(event.loaded);
    request.onload = () => {
      let body = {};
      try {
        body = JSON.parse(request.responseText);
      } catch {
        // an empty or plain-text answer
      }
      if (request.status >= 200 && request.status < 300) resolve(body);
      else reject(new Error(body.detail || `The server answered ${request.status}.`));
    };
    request.onerror = () => reject(new Error("The upload did not reach the server."));
    request.send(file);
  });
}

/** The files of a drop, folders walked, each with its path; read before the drop event ends. */
async function dropped(transfer) {
  const entries = [...transfer.items].map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) return [...transfer.files].map((file) => ({ file, path: file.name }));
  const found = [];
  const walk = async (entry, prefix) => {
    if (entry.isFile) {
      found.push({ file: await new Promise((done, failed) => entry.file(done, failed)), path: prefix + entry.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) {
        const children = await new Promise((done, failed) => reader.readEntries(done, failed));
        if (!children.length) break;
        for (const child of children) await walk(child, `${prefix}${entry.name}/`);
      }
    }
  };
  for (const entry of entries) await walk(entry, "");
  return found;
}

const chosen = (input) => [...input.files].map((file) => ({ file, path: file.webkitRelativePath || file.name }));

$("#data-file").addEventListener("change", (event) => {
  Data.upload(chosen(event.target));
  event.target.value = "";
});
$("#data-folder-pick").addEventListener("click", () => $("#data-folder").click());
$("#data-folder").addEventListener("change", (event) => {
  Data.upload(chosen(event.target));
  event.target.value = "";
});
$("#data-delete").addEventListener("click", () => Data.remove());
const drop = $("#data-drop");
drop.addEventListener("dragover", (event) => {
  event.preventDefault();
  drop.classList.add("over");
});
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (event) => {
  event.preventDefault();
  drop.classList.remove("over");
  dropped(event.dataTransfer).then((files) => Data.upload(files));
});

// -- conversations ---------------------------------------------------------------------

/** A conversation's messages as chat bubbles; tool output is folded away. */
function renderMessages(messages, marked) {
  return messages
    .map((m) =>
      m.role === "tool"
        ? `<details class="tool-output" id="msg-${esc(m.id)}"><summary>Tool output</summary><pre>${esc(m.text)}</pre></details>`
        : `<div class="bubble ${esc(m.role)} ${m.id === marked ? "highlight" : ""}" id="msg-${esc(m.id)}">
            <div class="bubble-meta">${esc(m.role === "user" ? "You" : m.role)}${m.created_at ? " · " + esc(date(m.created_at)) : ""}</div>
            ${markdown(m.text)}</div>`,
    )
    .join("");
}

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Turn the names of ``entities`` in the text under ``root`` into buttons carrying
 * ``data-<attribute>`` with the entity's id, longest names first, whole words only,
 * and never inside code. */
function linkEntities(root, entities, attribute = "steer") {
  const named = entities.filter((e) => e.name && e.name.length >= 2).sort((a, b) => b.name.length - a.name.length);
  if (!root || !named.length) return;
  const byName = new Map(named.map((e) => [e.name.toLowerCase(), e]));
  const pattern = new RegExp(`(?<![\\p{L}\\p{N}_])(?:${named.map((e) => escapeRegExp(e.name)).join("|")})(?![\\p{L}\\p{N}_])`, "giu");
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) =>
      node.parentElement.closest("pre, code, button, summary, .bubble-meta") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const texts = [];
  while (walker.nextNode()) texts.push(walker.currentNode);
  for (const node of texts) {
    const value = node.nodeValue;
    const matches = [...value.matchAll(pattern)];
    if (!matches.length) continue;
    const fragment = document.createDocumentFragment();
    let at = 0;
    for (const match of matches) {
      const entity = byName.get(match[0].toLowerCase());
      if (!entity) continue;
      fragment.append(value.slice(at, match.index));
      const button = document.createElement("button");
      button.type = "button";
      button.className = "mention";
      button.dataset[attribute] = entity.id;
      button.dataset.label = entity.name;
      button.textContent = match[0];
      fragment.append(button);
      at = match.index + match[0].length;
    }
    fragment.append(value.slice(at));
    node.replaceWith(fragment);
  }
}

async function openConversation(id, messageId) {
  const sheet = $("#conversation");
  $("#conversation-title").textContent = "Loading…";
  $("#conversation-meta").textContent = "";
  $("#conversation-body").innerHTML = "";
  $("#conversation-explore").dataset.id = id;
  if (!sheet.open) sheet.showModal();
  try {
    const conversation = await api(`/conversations/${encodeURIComponent(id)}`);
    $("#conversation-title").textContent = conversation.title || "Untitled conversation";
    $("#conversation-meta").textContent = [sourceName(conversation.source), date(conversation.created_at), `${conversation.messages.length} messages`]
      .filter(Boolean)
      .join(" · ");
    $("#conversation-body").innerHTML = renderMessages(conversation.messages, messageId);
    linkEntities($("#conversation-body"), conversation.entities, "exploreEntity");
    const target = messageId && document.getElementById(`msg-${messageId}`);
    if (target) target.scrollIntoView({ block: "center" });
  } catch (error) {
    $("#conversation-body").innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}

$("#conversation-explore").addEventListener("click", (event) => {
  $("#conversation").close();
  explore("conversation", event.currentTarget.dataset.id, $("#conversation-title").textContent);
});

$("#conversation-body").addEventListener("click", (event) => {
  const mention = event.target.closest("[data-explore-entity]");
  if (!mention) return;
  $("#conversation").close();
  explore("entity", mention.dataset.exploreEntity, mention.dataset.label);
});

const Conversations = {
  all: [],

  async load() {
    const list = $("#conversations-list");
    list.innerHTML = '<p class="empty">Loading…</p>';
    try {
      this.all = await api("/conversations?limit=500");
      $("#conversations-count").textContent = `${number(this.all.length)} conversations, newest first.`;
      this.render();
    } catch (error) {
      list.innerHTML = `<p class="error">${esc(error.message)}</p>`;
    }
  },

  render() {
    const filter = $("#conversations-filter").value.trim().toLowerCase();
    const shown = filter ? this.all.filter((c) => (c.title || "").toLowerCase().includes(filter)) : this.all;
    $("#conversations-list").innerHTML = shown.length
      ? shown
          .map(
            (c) => `<button type="button" class="row" data-conversation="${esc(c.id)}">
              <span class="row-title">${esc(c.title || "Untitled conversation")}</span>
              <span class="tag">${esc(c.source)}</span>
              <span class="row-meta">${c.messages} messages</span>
              <span class="row-meta">${esc(date(c.updated_at || c.created_at))}</span></button>`,
          )
          .join("")
      : `<p class="empty">${this.all.length ? "No conversation title matches." : "Nothing imported yet. Import an export under Your data."}</p>`;
  },
};

$("#conversations-filter").addEventListener("input", () => Conversations.render());

// Any element carrying a conversation id opens it.
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-conversation]");
  if (target) openConversation(target.dataset.conversation, target.dataset.message);
});

// -- ask -------------------------------------------------------------------------------

const Ask = {
  busy: false,

  async suggest() {
    try {
      const topics = await api("/topics?limit=4");
      $("#ask-suggestions").innerHTML = topics
        .map(
          (t) => `<button type="button" class="suggestion" data-question="${esc(`What did I work out about ${t.title.toLowerCase()}?`)}">
            <span>${esc(t.title)}</span>What did I work out about this?</button>`,
        )
        .join("");
    } catch {
      /* suggestions are optional */
    }
  },

  sources(sources, cited) {
    const shown = cited.length ? sources.filter((s) => cited.includes(s.number)) : sources;
    if (!shown.length) return "";
    return `<div class="sources"><div class="sources-title">Sources</div><div class="source-grid">${shown
      .map(
        (s) => `<button type="button" class="source" data-n="${s.number}" data-conversation="${esc(s.conversation_id)}" data-message="${esc(s.message_id)}">
          <span class="cite">${s.number}</span>
          <span><span class="source-title">${esc(s.title || "Untitled conversation")}</span>
          <span class="source-meta">${esc([s.source, date(s.date)].filter(Boolean).join(" · "))}</span></span></button>`,
      )
      .join("")}</div></div>`;
  },

  async ask(question) {
    if (this.busy || !question) return;
    this.busy = true;
    $("#ask-form .send").disabled = true;
    $("#ask-hero").hidden = true;
    const turn = document.createElement("div");
    turn.className = "turn";
    turn.innerHTML = `<div class="question">${esc(question)}</div>
      <div class="answer"><div class="answer-body"><span class="typing"><i></i><i></i><i></i></span></div></div>`;
    $("#ask-thread").append(turn);
    turn.scrollIntoView({ behavior: "smooth", block: "end" });
    const body = $(".answer-body", turn);
    let text = "";
    let sources = [];
    try {
      const response = await fetch("/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
      for await (const { event, data } of events(response)) {
        if (event === "sources") sources = data;
        else if (event === "token") {
          text += data.text;
          body.innerHTML = markdown(text);
        } else if (event === "done") {
          if (!data.found) body.innerHTML = '<p class="muted">Nothing in your conversations matches that question.</p>';
          $(".answer", turn).insertAdjacentHTML("beforeend", this.sources(sources, data.cited));
        } else if (event === "error") {
          body.innerHTML = (text ? markdown(text) : "") + `<p class="error">${esc(data.message)}</p>`;
        }
      }
    } catch (error) {
      body.innerHTML = `<p class="error">${esc(error.message)}</p>`;
    } finally {
      this.busy = false;
      $("#ask-form .send").disabled = false;
    }
  },
};

const askInput = $("#ask-input");
const growInput = () => {
  askInput.style.height = "auto";
  askInput.style.height = `${askInput.scrollHeight}px`;
};
askInput.addEventListener("input", growInput);
askInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#ask-form").requestSubmit();
  }
});

$("#ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = askInput.value.trim();
  askInput.value = "";
  growInput();
  Ask.ask(question);
});

$("#ask-suggestions").addEventListener("click", (event) => {
  const suggestion = event.target.closest("[data-question]");
  if (suggestion) Ask.ask(suggestion.dataset.question);
});

// A citation in an answer points at its source card.
$("#ask-thread").addEventListener("click", (event) => {
  const cite = event.target.closest(".answer-body .cite");
  const source = cite && $(`.source[data-n="${cite.dataset.n}"]`, cite.closest(".answer"));
  if (!source) return;
  source.scrollIntoView({ behavior: "smooth", block: "nearest" });
  source.classList.add("flash");
  setTimeout(() => source.classList.remove("flash"), 1400);
});

Ask.suggest();

// -- search ----------------------------------------------------------------------------

let searchMode = "hybrid";

async function search() {
  const q = $("#search-input").value.trim();
  const list = $("#search-results");
  if (!q) {
    list.innerHTML = "";
    return;
  }
  list.innerHTML = '<p class="empty">Searching…</p>';
  try {
    const response = await fetch(`/search?${new URLSearchParams({ q, limit: 25, mode: searchMode })}`);
    if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
    const hits = await response.json();
    // The first search on a new machine can come before the model that searches by
    // meaning has downloaded; the server then answers by words and says so.
    const note = response.headers.get("X-ChatLore-Meaning") === "loading"
      ? '<p class="search-note">Smart search is still starting up, so these are matches by words only. The first time, it downloads a small model; search again in a minute.</p>'
      : "";
    list.innerHTML = note + (hits.length
      ? hits
          .map(
            (h) => `<button type="button" class="result" data-conversation="${esc(h.conversation_id)}" data-message="${esc(h.message_id)}">
              <div class="result-head">
                <span class="result-title">${esc(h.title || "Untitled conversation")}</span>
                <span class="tag">${esc(h.source || "")}</span>
                <span class="spacer"></span>
                ${h.matched.map((m) => `<span class="tag accent">${esc(m)}</span>`).join("")}
              </div>
              <div class="result-text">${highlight(h.snippet.slice(0, 340), h.matched.includes("words"))}${h.snippet.length > 340 ? "…" : ""}</div></button>`,
          )
          .join("")
      : '<p class="empty">No matches. Try other words, or switch to Smart search.</p>');
  } catch (error) {
    list.innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}

$("#search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  search();
});

document.querySelectorAll(".segmented button").forEach((button) =>
  button.addEventListener("click", () => {
    searchMode = button.dataset.mode;
    document.querySelectorAll(".segmented button").forEach((b) => b.classList.toggle("on", b === button));
    search();
  }),
);

// -- topics ----------------------------------------------------------------------------

const Topics = {
  async load(q) {
    const grid = $("#topics-grid");
    grid.innerHTML = '<p class="empty">Loading…</p>';
    try {
      const topics = await api(`/topics?${new URLSearchParams(q ? { q, limit: 100 } : { limit: 300 })}`);
      const largest = Math.max(1, ...topics.map((t) => t.size));
      grid.innerHTML = topics.length
        ? topics
            .map(
              (t) => `<button type="button" class="topic-card" data-topic="${esc(t.id)}">
                <h3>${esc(t.title)}</h3>
                <p>${esc(t.summary)}</p>
                <div class="chips">${t.entities.slice(0, 4).map((name) => `<span class="chip">${esc(name)}</span>`).join("")}</div>
                <div class="size-bar" title="${t.size} entities"><i style="width:${Math.max(6, (100 * t.size) / largest)}%"></i></div>
              </button>`,
            )
            .join("")
        : '<p class="empty">No topics match. Topics are built by chatlore extract.</p>';
    } catch (error) {
      grid.innerHTML = `<p class="error">${esc(error.message)}</p>`;
    }
  },

  async open(id) {
    const sheet = $("#topic");
    $("#topic-title").textContent = "Loading…";
    $("#topic-meta").textContent = "";
    $("#topic-body").innerHTML = "";
    if (!sheet.open) sheet.showModal();
    try {
      const topic = await api(`/topics/${encodeURIComponent(id)}`);
      $("#topic-title").textContent = topic.title;
      $("#topic-meta").textContent = `${topic.size} entities`;
      $("#topic-body").innerHTML = `<p>${esc(topic.summary)}</p>
        ${topic.findings.length ? `<h4>Findings</h4><ul>${topic.findings.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
        <h4>Entities</h4>
        <div class="chips">${topic.members
          .map((m) => `<button type="button" class="chip" data-entity="${esc(m.id)}">${esc(m.name)}</button>`)
          .join("")}</div>
        <div class="sheet-actions"><button type="button" class="primary" data-graph-topic="${esc(topic.id)}" data-label="${esc(topic.title)}">Explore in the graph</button></div>`;
    } catch (error) {
      $("#topic-body").innerHTML = `<p class="error">${esc(error.message)}</p>`;
    }
  },
};

$("#topics-form").addEventListener("submit", (event) => {
  event.preventDefault();
  Topics.load($("#topics-input").value.trim());
});

$("#topics-grid").addEventListener("click", (event) => {
  const card = event.target.closest("[data-topic]");
  if (card) Topics.open(card.dataset.topic);
});

$("#topic-body").addEventListener("click", (event) => {
  const chip = event.target.closest("[data-entity]");
  const button = event.target.closest("[data-graph-topic]");
  if (!chip && !button) return;
  $("#topic").close();
  if (chip) explore("entity", chip.dataset.entity, chip.textContent.trim());
  else explore("topic", button.dataset.graphTopic, button.dataset.label);
});

// -- explore ---------------------------------------------------------------------------
//
// The knowledge graph as a way into the conversations: entities and the chats they
// came up in, drawn together. Clicking an entity centres the graph on it and shows
// what is known about it; clicking a chat opens it beside the graph, with the
// entities it mentions as links that steer the graph. A trail keeps the way back,
// and the timeline and source filters narrow everything to some chats.

const PALETTE = ["#6d5dfc", "#f0644f", "#14a38b", "#e8a317", "#c44a8f", "#2f8ad6", "#62a83a",
  "#d9708e", "#8a63d2", "#e0822b", "#2bb07a", "#ad5aa0"];
const SOURCE_NAMES = { chatgpt: "ChatGPT", claude: "Claude", gemini: "Gemini", claude_code: "Claude Code",
  codex: "Codex", markdown: "Notes", note: "Notes", document: "Documents", email: "Email", chatlore: "ChatLore" };
const sourceName = (source) => SOURCE_NAMES[source] || source || "";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthLabel = (month) => `${MONTHS[Number(month.slice(5, 7)) - 1]} ${month.slice(0, 4)}`;
const clip = (text, length) => (text.length > length ? `${text.slice(0, length - 1)}…` : text);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const Explore = {
  nodes: [],
  edges: [],
  byId: new Map(),
  view: { scale: 1, x: 0, y: 0 },
  alpha: 0,
  hover: null,
  focus: null,
  pointer: null,
  topicColors: new Map(),
  topicTitles: new Map(),
  trail: [],
  target: null,
  sources: new Set(),
  allSources: [],
  range: null,
  months: [],
  panel: [],

  start() {
    this.canvas = $("#graph-canvas");
    this.context = this.canvas.getContext("2d");
    this.bindPointer();
    this.bindTimeline();
    window.addEventListener("resize", () => this.resize());
    this.resize();
    api("/conversations?limit=500")
      .then((all) => {
        Search.conversations = all;
        this.allSources = [...new Set(all.map((c) => c.source))].sort();
        this.renderSources();
      })
      .catch(() => {});
    this.loadTimeline();
    requestAnimationFrame((time) => this.frame(time));
  },

  resize() {
    if (!this.canvas) return;
    const ratio = window.devicePixelRatio || 1;
    const { width, height } = this.canvas.getBoundingClientRect();
    if (!width || !height) return;
    this.canvas.width = width * ratio;
    this.canvas.height = height * ratio;
    this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.width = width;
    this.height = height;
  },

  color(topic) {
    if (!topic) return css("--node-plain") || "#9aa0b0";
    if (!this.topicColors.has(topic)) this.topicColors.set(topic, PALETTE[this.topicColors.size % PALETTE.length]);
    return this.topicColors.get(topic);
  },

  sourceColor(source) {
    return css(`--src-${source}`) || css("--src-other");
  },

  radius(node) {
    if (node.kind === "conversation") return Math.min(13, 7 + Math.sqrt(node.messages || 1) * 0.9);
    return Math.min(22, 5 + Math.sqrt(node.mentions || 1) * 1.7);
  },

  label(node) {
    return node.kind === "conversation" ? node.title || "Untitled conversation" : node.name;
  },

  // -- where we are -------------------------------------------------------------------

  /** Centre the graph on ``target`` ({kind, id, label}), or the overview for null. */
  go(target, { record = true } = {}) {
    this.target = target;
    if (record) {
      const last = this.trail[this.trail.length - 1];
      if (!target) this.trail = [];
      else if (!last || last.id !== target.id) this.trail.push(target);
    }
    this.renderTrail();
    return this.load();
  },

  params() {
    const params = new URLSearchParams();
    if (this.target) params.set(this.target.kind, this.target.id);
    this.sources.forEach((source) => params.append("source", source));
    if (this.range) {
      params.set("since", this.months[this.range[0]].month);
      params.set("until", this.months[this.range[1]].month);
    }
    return params;
  },

  async load() {
    $("#graph-info").textContent = "Loading…";
    const request = (this.request = (this.request || 0) + 1);
    try {
      const data = await api(`/explore?${this.params()}`);
      if (request !== this.request) return; // a newer view was asked for meanwhile
      const previous = this.byId;
      const anchor = data.focus && previous.get(data.focus);
      this.nodes = [];
      this.byId = new Map();
      data.topics.forEach((t) => this.topicTitles.set(t.id, t.title));
      for (const node of data.nodes) {
        const kept = previous.get(node.id);
        const angle = Math.random() * Math.PI * 2;
        const spread = (anchor ? 80 : 240) * (0.3 + Math.random());
        const entry = kept
          ? { ...node, x: kept.x, y: kept.y, vx: 0, vy: 0 }
          : { ...node, x: (anchor?.x ?? 0) + Math.cos(angle) * spread, y: (anchor?.y ?? 0) + Math.sin(angle) * spread, vx: 0, vy: 0 };
        this.nodes.push(entry);
        this.byId.set(node.id, entry);
      }
      this.edges = data.edges
        .filter((e) => this.byId.has(e.source) && this.byId.has(e.target))
        .map((e) => ({ ...e, source: this.byId.get(e.source), target: this.byId.get(e.target) }));
      this.focus = data.focus ? this.byId.get(data.focus) : null;
      if (this.focus) this.focus.pinned = true;
      [...this.nodes].filter((n) => n.kind === "entity").sort((a, b) => b.mentions - a.mentions).forEach((n) => this.color(n.topic));
      this.alpha = previous.size ? 0.7 : 1;
      this.fitted = false;
      const chats = this.nodes.filter((n) => n.kind === "conversation").length;
      const things = this.nodes.length - chats;
      $("#graph-info").textContent = this.nodes.length
        ? `${number(things)} ${things === 1 ? "entity" : "entities"} · ${number(chats)} ${chats === 1 ? "chat" : "chats"}`
        : "Nothing in this stretch of time";
      $("#explore-empty").hidden = this.nodes.length > 0;
      this.legend();
    } catch (error) {
      $("#graph-info").textContent = error.message.startsWith("404") ? "Not found" : "Could not load the graph";
    }
  },

  renderTrail() {
    const trail = $("#explore-trail");
    const steps = this.trail.map(
      (step, index) => `<span class="trail-sep">›</span>
        <button type="button" data-trail="${index}" class="${index === this.trail.length - 1 ? "current" : ""}">
          <span class="trail-dot ${step.kind}"></span>${esc(clip(step.label, 34))}</button>`,
    );
    trail.innerHTML = `<button type="button" data-trail="-1" class="${this.trail.length ? "" : "current"}">Overview</button>${steps.join("")}`;
  },

  renderSources() {
    $("#explore-sources").innerHTML = this.allSources.length > 1
      ? this.allSources
          .map((s) => `<button type="button" class="source-chip ${this.sources.size && !this.sources.has(s) ? "off" : ""}" data-source="${esc(s)}">
            <span class="source-dot" style="background:${this.sourceColor(s)}"></span>${esc(sourceName(s))}</button>`)
          .join("")
      : "";
  },

  toggleSource(source) {
    const shown = this.sources.size ? new Set(this.sources) : new Set(this.allSources);
    if (shown.has(source) && shown.size > 1) shown.delete(source);
    else shown.add(source);
    this.sources = shown.size === this.allSources.length ? new Set() : shown;
    this.renderSources();
    this.loadTimeline();
    this.load();
  },

  legend() {
    const counts = new Map();
    this.nodes.forEach((n) => n.topic && counts.set(n.topic, (counts.get(n.topic) || 0) + 1));
    const top = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 5);
    $("#graph-legend").innerHTML = top.length
      ? '<div class="legend-title">Topics</div>' +
        top
          .map(
            ([id, n]) => `<button type="button" class="legend-item" data-legend-topic="${esc(id)}">
              <span class="swatch" style="background:${this.color(id)}"></span>
              <span>${esc(this.topicTitles.get(id) || "Topic")}</span><span class="count">${n}</span></button>`,
          )
          .join("") +
        `<div class="legend-key"><span><i class="key-entity"></i>Entity</span><span><i class="key-chat"></i>Chat</span></div>`
      : "";
  },

  // -- timeline -----------------------------------------------------------------------

  async loadTimeline() {
    const params = new URLSearchParams();
    this.sources.forEach((source) => params.append("source", source));
    try {
      const months = await api(`/explore/timeline?${params}`);
      const changed = months.map((m) => m.month).join() !== this.months.map((m) => m.month).join();
      this.months = months;
      if (changed) this.range = null;
      this.renderTimeline();
    } catch {
      $("#explore-timeline").hidden = true;
    }
  },

  renderTimeline() {
    const box = $("#explore-timeline");
    box.hidden = this.months.length < 2;
    if (box.hidden) return;
    const most = Math.max(...this.months.map((m) => m.conversations));
    $("#timeline-bars").innerHTML = this.months
      .map((m, index) => {
        const on = !this.range || (index >= this.range[0] && index <= this.range[1]);
        return `<div class="bar ${on ? "on" : ""}" data-month="${index}" title="${esc(monthLabel(m.month))}: ${m.conversations} chats">
          <i style="height:${Math.max(10, (100 * m.conversations) / most)}%"></i></div>`;
      })
      .join("");
    const [first, last] = this.range || [0, this.months.length - 1];
    $("#timeline-label").innerHTML = this.range
      ? `${esc(monthLabel(this.months[first].month))} – ${esc(monthLabel(this.months[last].month))}
         <button type="button" class="link" id="timeline-clear">All time</button>`
      : `${esc(monthLabel(this.months[first].month))} – ${esc(monthLabel(this.months[last].month))} · drag to narrow`;
  },

  bindTimeline() {
    const bars = $("#timeline-bars");
    let start = null;
    const at = (event) => {
      const bar = document.elementFromPoint(event.clientX, event.clientY)?.closest?.("[data-month]");
      return bar ? Number(bar.dataset.month) : null;
    };
    bars.addEventListener("pointerdown", (event) => {
      start = at(event);
      if (start === null) return;
      bars.setPointerCapture(event.pointerId);
      this.range = [start, start];
      this.renderTimeline();
    });
    bars.addEventListener("pointermove", (event) => {
      if (start === null) return;
      const here = at(event);
      if (here === null) return;
      this.range = [Math.min(start, here), Math.max(start, here)];
      this.renderTimeline();
    });
    bars.addEventListener("pointerup", () => {
      if (start === null) return;
      start = null;
      if (this.range && this.range[0] === 0 && this.range[1] === this.months.length - 1) this.range = null;
      this.renderTimeline();
      this.load();
    });
    $("#explore-timeline").addEventListener("click", (event) => {
      if (!event.target.closest("#timeline-clear")) return;
      this.range = null;
      this.renderTimeline();
      this.load();
    });
  },

  // -- layout -------------------------------------------------------------------------

  // Force layout: nodes push each other apart, links pull their ends together, and
  // a weak pull keeps everything near the middle. The node in focus stays put at
  // the centre. It cools down and stops.
  step() {
    const nodes = this.nodes;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) {
          dx = Math.random() - 0.5;
          dy = Math.random() - 0.5;
          d2 = 0.25;
        }
        const d = Math.sqrt(d2);
        const force = (2800 / d2) * this.alpha;
        const fx = (dx / d) * force;
        const fy = (dy / d) * force;
        a.vx -= fx;
        a.vy -= fy;
        b.vx += fx;
        b.vy += fy;
      }
    }
    for (const edge of this.edges) {
      const { source: a, target: b } = edge;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const rest = (edge.kind === "mentions" ? 90 : 70) + this.radius(a) + this.radius(b);
      const strength = edge.kind === "mentions" ? 0.025 : 0.04 * Math.min(1, 0.4 + (edge.weight || 1) / 20);
      const f = (d - rest) * strength * this.alpha;
      a.vx += (dx / d) * f;
      a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f;
      b.vy -= (dy / d) * f;
    }
    for (const node of nodes) {
      if (node === this.pointer?.node) continue;
      if (node === this.focus) {
        node.x += (0 - node.x) * 0.08;
        node.y += (0 - node.y) * 0.08;
        node.vx = node.vy = 0;
        continue;
      }
      node.vx = (node.vx - node.x * 0.004 * this.alpha) * 0.82;
      node.vy = (node.vy - node.y * 0.004 * this.alpha) * 0.82;
      node.x += node.vx;
      node.y += node.vy;
    }
    this.alpha *= 0.985;
  },

  frame(now = performance.now()) {
    // Steps follow elapsed time, not frames, so the layout settles just as fast
    // when the browser draws fewer frames, for example in a background tab.
    const steps = Math.min(12, Math.max(1, Math.round((now - (this.last || now)) / 16)));
    this.last = now;
    for (let i = 0; i < steps && this.alpha > 0.01 && this.nodes.length; i++) {
      this.step();
      if (this.alpha < 0.08 && !this.fitted && !this.pointer) {
        this.fit();
        this.fitted = true;
      }
    }
    this.draw();
    requestAnimationFrame((time) => this.frame(time));
  },

  /** Zoom and pan to the bulk of the graph, leaving room for the floating panels.
   * The outermost 3% on each side are left out, so a few stragglers on long links
   * do not shrink everything else. */
  fit() {
    if (!this.nodes.length || !this.width) return;
    const xs = this.nodes.map((n) => n.x).sort((a, b) => a - b);
    const ys = this.nodes.map((n) => n.y).sort((a, b) => a - b);
    const edge = Math.floor(this.nodes.length * 0.03);
    const last = this.nodes.length - 1 - edge;
    const [left, right, top, bottom] = [xs[edge], xs[last], ys[edge], ys[last]];
    const width = this.width - 140;
    const height = this.height - 260;
    const scale = Math.max(0.25, Math.min(width / (right - left || 1), height / (bottom - top || 1), 1.8));
    this.view = { scale, x: -(left + right) / 2, y: -(top + bottom) / 2 - 10 / scale };
  },

  toScreen(x, y) {
    return [this.width / 2 + (x + this.view.x) * this.view.scale, this.height / 2 + (y + this.view.y) * this.view.scale];
  },

  toWorld(sx, sy) {
    return [(sx - this.width / 2) / this.view.scale - this.view.x, (sy - this.height / 2) / this.view.scale - this.view.y];
  },

  neighbours(node) {
    const set = new Set();
    for (const e of this.edges) {
      if (e.source === node) set.add(e.target);
      if (e.target === node) set.add(e.source);
    }
    return set;
  },

  // -- drawing ------------------------------------------------------------------------

  draw() {
    const ctx = this.context;
    if (!ctx || !this.width) return;
    const text = css("--text");
    const halo = css("--canvas");
    const accent = css("--accent");
    const edgeColor = css("--edge");
    const glow = css("--glow") === "1";
    ctx.clearRect(0, 0, this.width, this.height);
    const reading = this.reading && this.byId.get(this.reading);
    const lit = this.hover || reading || this.focus;
    const near = lit ? this.neighbours(lit) : null;
    const scale = this.view.scale;

    for (const e of this.edges) {
      const touching = lit && (e.source === lit || e.target === lit);
      ctx.globalAlpha = lit && !touching ? 0.25 : 1;
      const [x1, y1] = this.toScreen(e.source.x, e.source.y);
      const [x2, y2] = this.toScreen(e.target.x, e.target.y);
      ctx.strokeStyle = touching ? accent : edgeColor;
      ctx.setLineDash(e.kind === "mentions" ? [3, 4] : []);
      ctx.lineWidth = touching ? 1.8 : e.kind === "mentions" ? 1 : Math.min(2.6, 0.8 + (e.weight || 1) / 14);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    const labels = [];
    const entities = this.nodes.filter((n) => n.kind === "entity");
    const known = new Set(entities.sort((a, b) => b.mentions - a.mentions).slice(0, 18));
    for (const node of this.nodes) {
      const [x, y] = this.toScreen(node.x, node.y);
      const r = this.radius(node) * Math.sqrt(scale);
      const faded = lit && node !== lit && !near.has(node);
      ctx.globalAlpha = faded ? 0.18 : 1;
      if (node === this.focus || node === reading) {
        ctx.fillStyle = accent;
        ctx.globalAlpha = 0.18;
        ctx.beginPath();
        ctx.arc(x, y, r + 10, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
      }
      const fill = node.kind === "conversation" ? this.sourceColor(node.source) : this.color(node.topic);
      if (glow && !faded) {
        ctx.shadowColor = fill;
        ctx.shadowBlur = node === lit ? 22 : 12;
      }
      ctx.fillStyle = fill;
      ctx.strokeStyle = halo;
      ctx.lineWidth = 2;
      ctx.beginPath();
      if (node.kind === "conversation") {
        const s = r * 1.7;
        ctx.roundRect(x - s / 2, y - s / 2, s, s, 4);
      } else {
        ctx.arc(x, y, r, 0, Math.PI * 2);
      }
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.stroke();
      if (node.kind === "conversation") {
        // A speech mark, so a chat reads as a chat at a glance.
        ctx.strokeStyle = "rgba(255,255,255,.9)";
        ctx.lineWidth = 1.4;
        const w = r * 0.8;
        ctx.beginPath();
        ctx.moveTo(x - w / 2, y - w / 5);
        ctx.lineTo(x + w / 2, y - w / 5);
        ctx.moveTo(x - w / 2, y + w / 5);
        ctx.lineTo(x + w / 5, y + w / 5);
        ctx.stroke();
      }
      const isChat = node.kind === "conversation";
      const labelled = node === lit || near?.has(node) || (!isChat && known.has(node)) || scale > 1.4;
      if (labelled && !faded) labels.push({ node, x, y: y + r + (isChat ? 17 : 14), strong: node === lit, isChat });
    }
    ctx.globalAlpha = 1;

    // Labels last, with a halo in the canvas colour so lines never run through them.
    ctx.textAlign = "center";
    ctx.lineJoin = "round";
    for (const { node, x, y, strong, isChat } of labels) {
      ctx.font = `${strong ? 650 : isChat ? 450 : 520} ${strong ? 13 : 12}px ${css("--font")}`;
      const words = clip(this.label(node), isChat && !strong ? 30 : 48);
      ctx.strokeStyle = halo;
      ctx.lineWidth = 4;
      ctx.strokeText(words, x, y);
      ctx.fillStyle = isChat ? css("--text-2") : text;
      ctx.fillText(words, x, y);
    }
  },

  nodeAt(sx, sy) {
    for (let i = this.nodes.length - 1; i >= 0; i--) {
      const node = this.nodes[i];
      const [x, y] = this.toScreen(node.x, node.y);
      const r = this.radius(node) * Math.sqrt(this.view.scale) + 4;
      if ((sx - x) ** 2 + (sy - y) ** 2 <= r * r) return node;
    }
    return null;
  },

  bindPointer() {
    const canvas = this.canvas;
    const position = (event) => {
      const rect = canvas.getBoundingClientRect();
      return [event.clientX - rect.left, event.clientY - rect.top];
    };
    canvas.addEventListener("pointerdown", (event) => {
      const [sx, sy] = position(event);
      this.pointer = { node: this.nodeAt(sx, sy), sx, sy, moved: false, view: { ...this.view } };
      canvas.setPointerCapture(event.pointerId);
      canvas.classList.add("dragging");
    });
    canvas.addEventListener("pointermove", (event) => {
      const [sx, sy] = position(event);
      const p = this.pointer;
      if (!p) {
        this.hover = this.nodeAt(sx, sy);
        canvas.style.cursor = this.hover ? "pointer" : "grab";
        this.tooltip(this.hover, sx, sy);
        return;
      }
      if (Math.abs(sx - p.sx) + Math.abs(sy - p.sy) > 3) p.moved = true;
      if (p.node) {
        [p.node.x, p.node.y] = this.toWorld(sx, sy);
        p.node.vx = p.node.vy = 0;
        this.alpha = Math.max(this.alpha, 0.3);
      } else {
        this.view.x = p.view.x + (sx - p.sx) / this.view.scale;
        this.view.y = p.view.y + (sy - p.sy) / this.view.scale;
      }
    });
    canvas.addEventListener("pointerup", () => {
      const p = this.pointer;
      this.pointer = null;
      canvas.classList.remove("dragging");
      if (p && !p.moved && p.node) this.open(p.node);
    });
    canvas.addEventListener("pointerleave", () => {
      this.hover = null;
      this.tooltip(null);
    });
    canvas.addEventListener("dblclick", () => this.fit());
    canvas.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        const [sx, sy] = position(event);
        const [wx, wy] = this.toWorld(sx, sy);
        this.view.scale = Math.min(4, Math.max(0.2, this.view.scale * (event.deltaY < 0 ? 1.12 : 1 / 1.12)));
        const [nx, ny] = this.toWorld(sx, sy);
        this.view.x += nx - wx;
        this.view.y += ny - wy;
      },
      { passive: false },
    );
  },

  tooltip(node, sx, sy) {
    const tip = $("#graph-tip");
    if (!node) {
      tip.hidden = true;
      return;
    }
    tip.innerHTML =
      node.kind === "conversation"
        ? `<strong>${esc(node.title || "Untitled conversation")}</strong><span>${esc([sourceName(node.source), date(node.created_at), node.messages ? `${node.messages} messages` : ""].filter(Boolean).join(" · "))}</span>`
        : `<strong>${esc(node.name)}</strong><span>${esc([node.type, `${number(node.mentions)} mentions`, this.topicTitles.get(node.topic)].filter(Boolean).join(" · "))}</span>`;
    tip.hidden = false;
    tip.style.left = `${sx + 14}px`;
    tip.style.top = `${sy + 14}px`;
  },

  /** A click on a node: centre the graph on it and show it beside the graph. */
  open(node) {
    const target = { kind: node.kind, id: node.id, label: this.label(node) };
    this.go(target);
    if (node.kind === "conversation") Panel.open({ kind: "conversation", id: node.id }, true);
    else Panel.open({ kind: "entity", id: node.id }, true);
  },
};

// -- the panel beside the graph: an entity, or a conversation to read ---------------------

const Panel = {
  stack: [],

  /** Show ``item`` ({kind, id, message?}); ``fresh`` starts a new history instead of adding to it. */
  async open(item, fresh = false) {
    if (fresh) this.stack = [];
    const top = this.stack[this.stack.length - 1];
    if (!top || top.kind !== item.kind || top.id !== item.id) this.stack.push(item);
    else this.stack[this.stack.length - 1] = item;
    await this.render();
  },

  back() {
    this.stack.pop();
    if (this.stack.length) this.render();
    else this.close();
  },

  close() {
    this.stack = [];
    Explore.reading = null;
    $("#explore-panel").hidden = true;
    $("#view-explore").classList.remove("reading");
    requestAnimationFrame(() => Explore.resize());
  },

  async render() {
    const item = this.stack[this.stack.length - 1];
    const panel = $("#explore-panel");
    const opening = panel.hidden;
    panel.hidden = false;
    $("#view-explore").classList.add("reading");
    if (opening) requestAnimationFrame(() => Explore.resize());
    $("#panel-back").hidden = this.stack.length < 2;
    $("#panel-kicker").textContent = item.kind === "conversation" ? "Conversation" : "Entity";
    const body = $("#panel-body");
    body.innerHTML = '<p class="muted panel-loading">Loading…</p>';
    body.scrollTop = 0;
    Explore.reading = item.id;
    const request = (this.request = (this.request || 0) + 1);
    try {
      const path = item.kind === "conversation" ? "conversations" : "entities";
      const data = await api(`/${path}/${encodeURIComponent(item.id)}`);
      if (request !== this.request) return; // something else was opened meanwhile
      if (item.kind === "conversation") this.conversation(item, data, body);
      else this.entity(data, body);
    } catch (error) {
      body.innerHTML = `<p class="error">${esc(error.message)}</p>`;
    }
  },

  entity(e, body) {
    const topic = e.topic ? `<span class="tag"><span class="swatch" style="background:${Explore.color(e.topic.id)}"></span>${esc(e.topic.title)}</span>` : "";
    body.innerHTML = `<h2 class="panel-title">${esc(e.name)}</h2>
      <div class="panel-meta"><span class="tag accent">${esc(e.type)}</span><span class="tag">${number(e.mentions)} mentions</span>${topic}</div>
      <p class="panel-summary">${esc(e.summary || "")}</p>
      ${e.also_called.length ? `<p class="muted small">Also called ${esc(e.also_called.join(", "))}</p>` : ""}
      ${e.facts.length ? `<h4>What you established</h4><ul class="facts">${e.facts
        .slice(0, 8)
        .map((f) => {
          const said = f.sources[0];
          return `<li><p>${esc(f.statement)}</p>${said ? `<button type="button" class="link-quiet" data-read="${esc(said.conversation_id)}" data-message="${esc(said.message_id)}">${esc(said.title || "Untitled conversation")}${said.created_at ? ` · ${esc(date(said.created_at))}` : ""}</button>` : ""}</li>`;
        })
        .join("")}</ul>` : ""}
      ${e.conversations.length ? `<h4>Talked about in ${e.conversations.length === 1 ? "1 chat" : `${e.conversations.length} chats`}</h4><div class="panel-chats">${e.conversations
        .slice(0, 12)
        .map((c) => `<button type="button" class="panel-chat" data-read="${esc(c.id)}"><span class="chat-icon"></span>${esc(c.title || "Untitled conversation")}</button>`)
        .join("")}</div>` : ""}
      ${e.related.length ? `<h4>Related</h4><div class="chips">${e.related
        .slice(0, 14)
        .map((r) => `<button type="button" class="chip" data-steer="${esc(r.id)}" data-label="${esc(r.name)}" title="${esc(r.relationship || "")}">${esc(r.name)}</button>`)
        .join("")}</div>` : ""}`;
  },

  conversation(item, c, body) {
    const mentioned = c.entities.slice(0, 16);
    body.innerHTML = `<h2 class="panel-title">${esc(c.title || "Untitled conversation")}</h2>
      <div class="panel-meta"><span class="tag"><span class="source-dot" style="background:${Explore.sourceColor(c.source)}"></span>${esc(sourceName(c.source))}</span>
        ${c.created_at ? `<span class="tag">${esc(date(c.created_at))}</span>` : ""}<span class="tag">${c.messages.length} messages</span></div>
      ${mentioned.length ? `<div class="chips in-chat">${mentioned
        .map((e) => `<button type="button" class="chip" data-steer="${esc(e.id)}" data-label="${esc(e.name)}">${esc(e.name)}</button>`)
        .join("")}</div>` : ""}
      <div class="chat reader">${renderMessages(c.messages, item.message)}</div>`;
    linkEntities($(".reader", body), c.entities);
    const target = item.message && document.getElementById(`msg-${item.message}`);
    if (target) target.scrollIntoView({ block: "center" });
  },
};

$("#panel-close").addEventListener("click", () => Panel.close());
$("#panel-back").addEventListener("click", () => Panel.back());

$("#explore-panel").addEventListener("click", (event) => {
  const read = event.target.closest("[data-read]");
  const steer = event.target.closest("[data-steer]");
  if (read) {
    event.stopPropagation();
    Panel.open({ kind: "conversation", id: read.dataset.read, message: read.dataset.message });
    const title = read.textContent.split(" · ")[0].trim();
    Explore.go({ kind: "conversation", id: read.dataset.read, label: title });
  } else if (steer) {
    // An entity in the panel steers the graph; the panel stays where it is.
    Explore.go({ kind: "entity", id: steer.dataset.steer, label: steer.dataset.label || steer.textContent.trim() });
  }
});

$("#explore-trail").addEventListener("click", (event) => {
  const step = event.target.closest("[data-trail]");
  if (!step) return;
  const index = Number(step.dataset.trail);
  if (index < 0) {
    Explore.go(null);
    return;
  }
  Explore.trail = Explore.trail.slice(0, index + 1);
  Explore.go(Explore.trail[index], { record: false });
});

$("#explore-sources").addEventListener("click", (event) => {
  const chip = event.target.closest("[data-source]");
  if (chip) Explore.toggleSource(chip.dataset.source);
});

$("#graph-legend").addEventListener("click", (event) => {
  const item = event.target.closest("[data-legend-topic]");
  if (item) Explore.go({ kind: "topic", id: item.dataset.legendTopic, label: Explore.topicTitles.get(item.dataset.legendTopic) || "Topic" });
});

$("#graph-fit").addEventListener("click", () => Explore.fit());
$("#graph-reset").addEventListener("click", () => {
  $("#graph-input").value = "";
  Explore.sources = new Set();
  Explore.range = null;
  Explore.renderSources();
  Explore.loadTimeline();
  Panel.close();
  Explore.go(null);
});

// -- finding a place to start: entities and chats by name -----------------------------------

const Search = {
  conversations: [],
  timer: null,

  async suggest(q) {
    const box = $("#graph-suggest");
    if (!q) {
      box.hidden = true;
      return;
    }
    const lower = q.toLowerCase();
    const chats = this.conversations.filter((c) => (c.title || "").toLowerCase().includes(lower)).slice(0, 5);
    // Names containing the letters typed, those starting with them first; then
    // word matches on names and summaries, for entities beyond the most mentioned.
    this.entities ??= api("/entities?limit=500").catch(() => []);
    const named = (await this.entities)
      .filter((e) => (e.name || "").toLowerCase().includes(lower))
      .sort((a, b) => Number(!a.name.toLowerCase().startsWith(lower)) - Number(!b.name.toLowerCase().startsWith(lower)) || b.mentions - a.mentions);
    let worded = [];
    if (named.length < 6) {
      try {
        worded = await api(`/entities?${new URLSearchParams({ q, limit: 6 })}`);
      } catch {
        // word search is optional here
      }
    }
    const seen = new Set();
    const things = [...named, ...worded].filter((e) => !seen.has(e.id) && seen.add(e.id)).slice(0, 6);
    if ($("#graph-input").value.trim() !== q) return;
    box.innerHTML =
      (things.length ? `<div class="suggest-title">Entities</div>${things
        .map((e) => `<button type="button" data-pick="entity" data-id="${esc(e.id)}" data-label="${esc(e.name)}"><span class="trail-dot entity"></span>${esc(e.name)}<span class="muted">${esc(e.type || "")}</span></button>`)
        .join("")}` : "") +
      (chats.length ? `<div class="suggest-title">Chats</div>${chats
        .map((c) => `<button type="button" data-pick="conversation" data-id="${esc(c.id)}" data-label="${esc(c.title || "Untitled conversation")}"><span class="trail-dot conversation"></span>${esc(c.title || "Untitled conversation")}<span class="muted">${esc(date(c.created_at))}</span></button>`)
        .join("")}` : "") ||
      '<p class="muted suggest-empty">Nothing by that name.</p>';
    box.hidden = false;
  },

  pick(button) {
    $("#graph-suggest").hidden = true;
    $("#graph-input").value = "";
    const target = { kind: button.dataset.pick, id: button.dataset.id, label: button.dataset.label };
    Explore.go(target);
    Panel.open({ kind: target.kind, id: target.id }, true);
  },
};

$("#graph-input").addEventListener("input", (event) => {
  clearTimeout(Search.timer);
  const q = event.target.value.trim();
  Search.timer = setTimeout(() => Search.suggest(q), 160);
});
$("#graph-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const first = $("#graph-suggest [data-pick]");
  if (first) Search.pick(first);
});
$("#graph-suggest").addEventListener("click", (event) => {
  const button = event.target.closest("[data-pick]");
  if (button) Search.pick(button);
});
document.addEventListener("click", (event) => {
  if (!event.target.closest("#graph-form")) $("#graph-suggest").hidden = true;
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || document.querySelector("dialog[open]")) return;
  if (!$("#graph-suggest").hidden) $("#graph-suggest").hidden = true;
  else if (!$("#explore-panel").hidden) Panel.close();
});

/** Open the Explore view centred on an entity, a conversation, or a topic. */
function explore(kind, id, label) {
  show("explore", false);
  const target = kind ? { kind, id, label } : null;
  Explore.go(target);
  if (kind === "entity" || kind === "conversation") Panel.open({ kind, id }, true);
}
