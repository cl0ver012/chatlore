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

function show(view) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (!opened.has(view)) {
    opened.add(view);
    if (view === "conversations") Conversations.load();
    if (view === "topics") Topics.load();
    if (view === "graph") Graph.start();
  }
  if (view === "graph") Graph.resize();
  if (view === "ask") $("#ask-input").focus();
}

document.querySelectorAll("nav button").forEach((button) => button.addEventListener("click", () => show(button.dataset.view)));
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

// A public server, such as the hosted demo, says so.
api("/health")
  .then((health) => {
    if (!health.public) return;
    $("#ask-hero p").textContent = "A demo on made-up conversations. Every answer cites the chats it came from.";
    $("#demo-note").hidden = false;
  })
  .catch(() => {});

// -- conversations ---------------------------------------------------------------------

async function openConversation(id, messageId) {
  const sheet = $("#conversation");
  $("#conversation-title").textContent = "Loading…";
  $("#conversation-meta").textContent = "";
  $("#conversation-body").innerHTML = "";
  if (!sheet.open) sheet.showModal();
  try {
    const conversation = await api(`/conversations/${encodeURIComponent(id)}`);
    $("#conversation-title").textContent = conversation.title || "Untitled conversation";
    $("#conversation-meta").textContent = [conversation.source, date(conversation.created_at), `${conversation.messages.length} messages`]
      .filter(Boolean)
      .join(" · ");
    $("#conversation-body").innerHTML = conversation.messages
      .map(
        (m) => `<div class="bubble ${esc(m.role)} ${m.id === messageId ? "highlight" : ""}" id="msg-${esc(m.id)}">
          <div class="bubble-meta">${esc(m.role)}${m.created_at ? " · " + esc(date(m.created_at)) : ""}</div>
          ${markdown(m.text)}</div>`,
      )
      .join("");
    const target = messageId && document.getElementById(`msg-${messageId}`);
    if (target) target.scrollIntoView({ block: "center" });
  } catch (error) {
    $("#conversation-body").innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}

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
      : `<p class="empty">${this.all.length ? "No conversation title matches." : "Nothing imported yet. Run chatlore import."}</p>`;
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
    const hits = await api(`/search?${new URLSearchParams({ q, limit: 25, mode: searchMode })}`);
    list.innerHTML = hits.length
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
      : '<p class="empty">No matches. Try other words, or switch to Smart search.</p>';
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
        <div class="sheet-actions"><button type="button" class="primary" data-graph-topic="${esc(topic.id)}">Open in graph</button></div>`;
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
  show("graph");
  if (chip) Graph.load({ entity: chip.dataset.entity, limit: 60 });
  else {
    $("#graph-topic").value = button.dataset.graphTopic;
    Graph.load({ topic: button.dataset.graphTopic });
  }
});

// -- graph -----------------------------------------------------------------------------

const PALETTE = ["#5b5bd6", "#e5604d", "#1f9e8f", "#e8a317", "#b0417f", "#2f86c9", "#5e9e3a",
  "#d06a86", "#8062c4", "#d9822b", "#2aa872", "#a8559c"];
const OVERVIEW = { limit: 100 };

const Graph = {
  nodes: [],
  edges: [],
  byId: new Map(),
  view: { scale: 1, x: 0, y: 0 },
  alpha: 0,
  hover: null,
  selected: null,
  pointer: null,
  topicColors: new Map(),
  topicTitles: new Map(),

  start() {
    this.canvas = $("#graph-canvas");
    this.context = this.canvas.getContext("2d");
    this.bindPointer();
    window.addEventListener("resize", () => this.resize());
    this.resize();
    api("/topics?limit=300").then((topics) => {
      $("#graph-topic").innerHTML =
        '<option value="">Most connected entities</option>' +
        topics.map((t) => `<option value="${esc(t.id)}">${esc(t.title)} · ${t.size}</option>`).join("");
    });
    this.load(OVERVIEW);
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
    if (!topic) return "#9aa0b0";
    if (!this.topicColors.has(topic)) this.topicColors.set(topic, PALETTE[this.topicColors.size % PALETTE.length]);
    return this.topicColors.get(topic);
  },

  radius(node) {
    return Math.min(22, 5 + Math.sqrt(node.mentions || 1) * 1.7);
  },

  /** Load part of the graph; with ``merge`` it is added to what is already drawn. */
  async load(params, merge = false) {
    $("#graph-info").textContent = "Loading…";
    try {
      const data = await api(`/graph?${new URLSearchParams(params)}`);
      if (!merge) {
        this.nodes = [];
        this.edges = [];
        this.byId = new Map();
        this.topicColors = new Map();
        this.view = { scale: 1, x: 0, y: 0 };
        this.close();
      }
      data.topics.forEach((t) => this.topicTitles.set(t.id, t.title));
      const anchor = params.entity && this.byId.get(params.entity);
      for (const node of data.nodes) {
        if (this.byId.has(node.id)) continue;
        const angle = Math.random() * Math.PI * 2;
        const spread = (anchor ? 60 : 260) * Math.random();
        const entry = { ...node, x: (anchor?.x ?? 0) + Math.cos(angle) * spread, y: (anchor?.y ?? 0) + Math.sin(angle) * spread, vx: 0, vy: 0 };
        this.nodes.push(entry);
        this.byId.set(node.id, entry);
      }
      const seen = new Set(this.edges.map((e) => `${e.source.id}|${e.target.id}`));
      for (const edge of data.edges) {
        const key = `${edge.source}|${edge.target}`;
        if (seen.has(key) || !this.byId.has(edge.source) || !this.byId.has(edge.target)) continue;
        seen.add(key);
        this.edges.push({ ...edge, source: this.byId.get(edge.source), target: this.byId.get(edge.target) });
      }
      [...this.nodes].sort((a, b) => b.mentions - a.mentions).forEach((n) => this.color(n.topic));
      this.alpha = 1;
      this.fitted = false;
      $("#graph-info").textContent = `${number(this.nodes.length)} entities · ${number(this.edges.length)} links`;
      this.legend();
      if (params.entity) this.select(this.byId.get(params.entity));
    } catch (error) {
      $("#graph-info").textContent = error.message.startsWith("404") ? "Not found" : "Could not load the graph";
    }
  },

  legend() {
    const counts = new Map();
    this.nodes.forEach((n) => n.topic && counts.set(n.topic, (counts.get(n.topic) || 0) + 1));
    const top = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 6);
    $("#graph-legend").innerHTML = top.length
      ? '<div class="legend-title">Topics</div>' +
        top
          .map(
            ([id, n]) => `<button type="button" class="legend-item" data-legend-topic="${esc(id)}">
              <span class="swatch" style="background:${this.color(id)}"></span>
              <span>${esc(this.topicTitles.get(id) || "Topic")}</span><span class="count">${n}</span></button>`,
          )
          .join("")
      : "";
  },

  // Force layout: nodes push each other apart, links pull their ends together,
  // and a weak pull keeps everything near the middle. It cools down and stops.
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
        const force = (2600 / d2) * this.alpha;
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
      const rest = 70 + this.radius(a) + this.radius(b);
      const f = (d - rest) * 0.04 * Math.min(1, 0.4 + (edge.weight || 1) / 20) * this.alpha;
      a.vx += (dx / d) * f;
      a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f;
      b.vy -= (dy / d) * f;
    }
    for (const node of nodes) {
      if (node === this.pointer?.node) continue;
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
    const room = this.selected ? 380 : 0;
    const width = this.width - room - 120;
    const height = this.height - 170;
    const scale = Math.max(0.2, Math.min(width / (right - left || 1), height / (bottom - top || 1), 2));
    this.view = { scale, x: -(left + right) / 2 - room / 2 / scale, y: -(top + bottom) / 2 + 10 / scale };
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

  draw() {
    const ctx = this.context;
    if (!ctx || !this.width) return;
    const styles = getComputedStyle(document.documentElement);
    const text = styles.getPropertyValue("--text").trim();
    const surface = styles.getPropertyValue("--bg").trim();
    const accent = styles.getPropertyValue("--accent").trim();
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    ctx.clearRect(0, 0, this.width, this.height);
    const focus = this.hover || this.selected;
    const near = focus ? this.neighbours(focus) : null;
    const scale = this.view.scale;

    for (const e of this.edges) {
      const lit = focus && (e.source === focus || e.target === focus);
      if (focus && !lit) ctx.globalAlpha = 0.35;
      const [x1, y1] = this.toScreen(e.source.x, e.source.y);
      const [x2, y2] = this.toScreen(e.target.x, e.target.y);
      ctx.strokeStyle = lit ? accent : dark ? "rgba(160,168,190,0.22)" : "rgba(80,88,110,0.18)";
      ctx.lineWidth = lit ? 2 : Math.min(2.6, 0.7 + (e.weight || 1) / 14);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    const labels = [];
    const known = new Set(this.nodes.slice().sort((a, b) => b.mentions - a.mentions).slice(0, 20));
    for (const node of this.nodes) {
      const [x, y] = this.toScreen(node.x, node.y);
      const r = this.radius(node) * Math.sqrt(scale);
      const faded = focus && node !== focus && !near.has(node);
      ctx.globalAlpha = faded ? 0.2 : 1;
      if (node === this.selected) {
        ctx.fillStyle = accent;
        ctx.globalAlpha = 0.18;
        ctx.beginPath();
        ctx.arc(x, y, r + 9, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
      }
      ctx.fillStyle = this.color(node.topic);
      ctx.strokeStyle = surface;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      const labelled = node === focus || near?.has(node) || known.has(node) || scale > 1.5;
      if (labelled && !faded) labels.push({ node, x, y: y + r + 14, strong: node === focus });
    }
    ctx.globalAlpha = 1;

    // Labels last, with a halo in the background colour so lines never run through them.
    ctx.textAlign = "center";
    ctx.lineJoin = "round";
    for (const { node, x, y, strong } of labels) {
      ctx.font = `${strong ? 650 : 500} ${strong ? 13 : 12}px Inter, system-ui, sans-serif`;
      ctx.strokeStyle = surface;
      ctx.lineWidth = 4;
      ctx.strokeText(node.name, x, y);
      ctx.fillStyle = text;
      ctx.fillText(node.name, x, y);
    }
  },

  nodeAt(sx, sy) {
    for (let i = this.nodes.length - 1; i >= 0; i--) {
      const node = this.nodes[i];
      const [x, y] = this.toScreen(node.x, node.y);
      const r = this.radius(node) * Math.sqrt(this.view.scale) + 3;
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
      if (p && !p.moved) (p.node ? this.select(p.node) : this.close());
    });
    canvas.addEventListener("pointerleave", () => (this.hover = null));
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

  close() {
    this.selected = null;
    $("#graph-drawer").hidden = true;
    $("#view-graph").classList.remove("has-drawer");
  },

  async select(node) {
    if (!node) return this.close();
    this.selected = node;
    const drawer = $("#graph-drawer");
    const body = $("#graph-drawer-body");
    drawer.hidden = false;
    $("#view-graph").classList.add("has-drawer");
    body.innerHTML = `<h2>${esc(node.name)}</h2><p class="muted">Loading…</p>`;
    try {
      const e = await api(`/entities/${encodeURIComponent(node.id)}`);
      body.innerHTML = `<h2>${esc(e.name)}</h2>
        <div class="drawer-meta">
          <span class="tag accent">${esc(e.type)}</span>
          <span class="tag">${number(e.mentions)} mentions</span>
          ${e.topic ? `<span class="tag"><span class="swatch" style="background:${this.color(e.topic.id)};margin-right:6px"></span>${esc(e.topic.title)}</span>` : ""}
        </div>
        <p>${esc(e.summary || "")}</p>
        ${e.also_called.length ? `<p class="muted">Also called ${esc(e.also_called.join(", "))}</p>` : ""}
        <div class="drawer-actions">
          <button type="button" class="primary" data-expand="${esc(e.id)}">Add neighbours</button>
          <button type="button" data-focus="${esc(e.id)}">Focus</button>
        </div>
        ${e.related.length ? `<h4>Related</h4><ul class="related">${e.related
          .slice(0, 12)
          .map((r) => `<li><button type="button" data-focus="${esc(r.id)}"><strong>${esc(r.name)}</strong> · ${esc(r.relationship || "")}</button></li>`)
          .join("")}</ul>` : ""}
        ${e.conversations.length ? `<h4>Mentioned in</h4><div class="mentions">${e.conversations
          .slice(0, 10)
          .map((c) => `<button type="button" data-conversation="${esc(c.id)}">${esc(c.title || "Untitled conversation")}</button>`)
          .join("")}</div>` : ""}`;
    } catch (error) {
      body.innerHTML = `<p class="error">${esc(error.message)}</p>`;
    }
  },
};

$("#graph-drawer-close").addEventListener("click", () => Graph.close());

$("#graph-drawer-body").addEventListener("click", (event) => {
  const expand = event.target.closest("[data-expand]");
  const focus = event.target.closest("[data-focus]");
  if (expand) Graph.load({ entity: expand.dataset.expand, limit: 30 }, true);
  else if (focus) Graph.load({ entity: focus.dataset.focus, limit: 60 });
});

$("#graph-legend").addEventListener("click", (event) => {
  const item = event.target.closest("[data-legend-topic]");
  if (!item) return;
  $("#graph-topic").value = item.dataset.legendTopic;
  Graph.load({ topic: item.dataset.legendTopic });
});

$("#graph-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const q = $("#graph-input").value.trim();
  if (!q) return Graph.load(OVERVIEW);
  const found = await api(`/entities?${new URLSearchParams({ q, limit: 1 })}`);
  if (found.length) Graph.load({ entity: found[0].id, limit: 60 });
  else $("#graph-info").textContent = `No entity matches “${q}”`;
});

$("#graph-topic").addEventListener("change", (event) => Graph.load(event.target.value ? { topic: event.target.value } : OVERVIEW));
$("#graph-fit").addEventListener("click", () => Graph.fit());
$("#graph-reset").addEventListener("click", () => {
  $("#graph-topic").value = "";
  $("#graph-input").value = "";
  Graph.load(OVERVIEW);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !document.querySelector("dialog[open]")) Graph.close();
});
