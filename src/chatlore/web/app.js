// ChatLore web interface: plain JavaScript over the REST API, no build step.
"use strict";

const $ = (selector) => document.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
  return response.json();
}

// -- small helpers -----------------------------------------------------------------

/** Markdown the model tends to write: code blocks, inline code, bold, citations. */
function markdown(text) {
  return String(text)
    .split("```")
    .map((part, index) => {
      if (index % 2) return `<pre><code>${esc(part.replace(/^[\w+-]*\n/, ""))}</code></pre>`;
      return esc(part)
        .replace(/`([^`\n]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\[(\d+)\]/g, '<span class="cite" data-n="$1">$1</span>')
        .replace(/\n/g, "<br>");
    })
    .join("");
}

/** Full-text snippets mark matched words with [brackets]; show them highlighted. */
function snippet(text, marked) {
  const safe = esc(text);
  return marked ? safe.replace(/\[([^\]]+)\]/g, "<mark>$1</mark>") : safe;
}

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

// -- navigation ----------------------------------------------------------------------

const opened = new Set();

function show(view) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (!opened.has(view)) {
    opened.add(view);
    if (view === "topics") loadTopics();
    if (view === "conversations") loadConversations();
    if (view === "graph") Graph.start();
  }
  if (view === "graph") Graph.resize();
}

document.querySelectorAll("nav button").forEach((button) => button.addEventListener("click", () => show(button.dataset.view)));

api("/stats")
  .then((s) => {
    const n = (value) => value.toLocaleString();
    $("#stats").textContent = `${n(s.conversations)} conversations · ${n(s.entities)} entities · ${n(s.topics)} topics`;
  })
  .catch(() => ($("#stats").textContent = "Library unavailable"));

// -- conversations -------------------------------------------------------------------

async function openConversation(id, messageId) {
  const dialog = $("#conversation");
  $("#conversation-title").textContent = "Loading…";
  $("#conversation-body").innerHTML = "";
  dialog.showModal();
  try {
    const conversation = await api(`/conversations/${encodeURIComponent(id)}`);
    $("#conversation-title").textContent = conversation.title || "(untitled)";
    $("#conversation-body").innerHTML = conversation.messages
      .map(
        (m) => `<div class="conversation-message ${esc(m.role)} ${m.id === messageId ? "highlight" : ""}" id="msg-${esc(m.id)}">
          <div class="role">${esc(m.role)}${m.created_at ? " · " + esc(m.created_at.slice(0, 10)) : ""}</div>
          <div>${markdown(m.text)}</div></div>`,
      )
      .join("");
    const target = messageId && document.getElementById(`msg-${messageId}`);
    if (target) target.scrollIntoView({ block: "center" });
  } catch (error) {
    $("#conversation-body").innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}

$("#conversation-close").addEventListener("click", () => $("#conversation").close());

const PAGE = 50;
let conversationsShown = 0;

/** The next page of conversations, newest first. */
async function loadConversations() {
  const list = $("#conversations-list");
  try {
    const page = await api(`/conversations?${new URLSearchParams({ limit: PAGE, offset: conversationsShown })}`);
    if (!conversationsShown && !page.length) list.innerHTML = '<p class="muted">Nothing imported yet.</p>';
    list.insertAdjacentHTML(
      "beforeend",
      page
        .map(
          (c) => `<div class="card" data-conversation="${esc(c.id)}">
            <div class="card-title">${esc(c.title || "(untitled)")}
              <span class="card-meta">${esc([c.source, (c.updated_at || c.created_at || "").slice(0, 10)].filter(Boolean).join(" · "))} · ${c.messages} messages</span></div></div>`,
        )
        .join(""),
    );
    conversationsShown += page.length;
    $("#conversations-more").hidden = page.length < PAGE;
  } catch (error) {
    list.innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}

$("#conversations-list").addEventListener("click", (event) => {
  const card = event.target.closest(".card");
  if (card) openConversation(card.dataset.conversation);
});

$("#conversations-more").addEventListener("click", loadConversations);

// -- ask -----------------------------------------------------------------------------

function renderSources(sources, cited) {
  const shown = cited.length ? sources.filter((s) => cited.includes(s.number)) : sources;
  $("#ask-sources").innerHTML = shown.length
    ? "<h3>Sources</h3>" +
      shown
        .map(
          (s) => `<div class="card" data-n="${s.number}" data-conversation="${esc(s.conversation_id)}" data-message="${esc(s.message_id)}">
            <div class="card-title"><span class="cite">${s.number}</span> ${esc(s.title || "(untitled)")}
              <span class="card-meta">${esc([s.source, s.date].filter(Boolean).join(" · "))}</span></div>
            <div class="card-text">${esc(s.text.slice(0, 280))}${s.text.length > 280 ? "…" : ""}</div></div>`,
        )
        .join("")
    : "";
}

$("#ask-sources").addEventListener("click", (event) => {
  const card = event.target.closest(".card");
  if (card) openConversation(card.dataset.conversation, card.dataset.message);
});

$("#ask-answer").addEventListener("click", (event) => {
  const cite = event.target.closest(".cite");
  const card = cite && document.querySelector(`#ask-sources .card[data-n="${cite.dataset.n}"]`);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1200);
});

$("#ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("#ask-input").value.trim();
  if (!question) return;
  const answer = $("#ask-answer");
  answer.innerHTML = '<p class="muted">Reading your conversations…</p>';
  $("#ask-sources").innerHTML = "";
  let text = "";
  let sources = [];
  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
    for await (const { event: name, data } of events(response)) {
      if (name === "sources") sources = data;
      else if (name === "token") {
        text += data.text;
        answer.innerHTML = markdown(text);
      } else if (name === "done") {
        if (!data.found) answer.innerHTML = '<p class="muted">Nothing in your conversations matches that question.</p>';
        renderSources(sources, data.cited);
      } else if (name === "error") {
        answer.innerHTML = (text ? markdown(text) : "") + `<p class="error">${esc(data.message)}</p>`;
      }
    }
  } catch (error) {
    answer.innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
});

// -- search --------------------------------------------------------------------------

$("#search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const q = $("#search-input").value.trim();
  if (!q) return;
  const list = $("#search-results");
  list.innerHTML = '<p class="muted">Searching…</p>';
  try {
    const hits = await api(`/search?${new URLSearchParams({ q, limit: 20 })}`);
    list.innerHTML = hits.length
      ? hits
          .map(
            (h) => `<div class="card" data-conversation="${esc(h.conversation_id)}" data-message="${esc(h.message_id)}">
              <div class="card-title">${esc(h.title || "(untitled)")}<span class="card-meta">${esc(h.source || "")}</span>
                ${h.matched.map((m) => `<span class="badge">${esc(m)}</span>`).join("")}</div>
              <div class="card-text">${snippet(h.snippet.slice(0, 320), h.matched.includes("words"))}</div></div>`,
          )
          .join("")
      : '<p class="muted">No matches.</p>';
  } catch (error) {
    list.innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
});

$("#search-results").addEventListener("click", (event) => {
  const card = event.target.closest(".card");
  if (card) openConversation(card.dataset.conversation, card.dataset.message);
});

// -- topics --------------------------------------------------------------------------

async function loadTopics(q) {
  const list = $("#topics-list");
  list.innerHTML = '<p class="muted">Loading…</p>';
  try {
    const topics = await api(`/topics?${new URLSearchParams(q ? { q, limit: 100 } : { limit: 300 })}`);
    list.innerHTML = topics.length
      ? topics
          .map(
            (t) => `<div class="card" data-topic="${esc(t.id)}">
              <div class="card-title">${esc(t.title)}<span class="card-meta">${t.size} entities</span></div>
              <div class="card-text">${esc(t.entities.slice(0, 5).join(", "))}</div></div>`,
          )
          .join("")
      : '<p class="muted">No topics match. Topics are built by <code>chatlore extract</code>.</p>';
  } catch (error) {
    list.innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}

async function showTopic(id) {
  const detail = $("#topic-detail");
  try {
    const topic = await api(`/topics/${encodeURIComponent(id)}`);
    detail.innerHTML = `<h2>${esc(topic.title)}</h2>
      <p class="muted">${topic.size} entities</p>
      <p>${esc(topic.summary)}</p>
      ${topic.findings.length ? `<h3>Findings</h3><ul>${topic.findings.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
      <div class="actions"><button type="button" data-graph-topic="${esc(topic.id)}">Show in graph</button></div>
      <h3>Entities</h3>
      <div class="chips">${topic.members.map((m) => `<span class="chip" data-entity="${esc(m.id)}">${esc(m.name)}</span>`).join("")}</div>`;
  } catch (error) {
    detail.innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}

$("#topics-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadTopics($("#topics-input").value.trim());
});

$("#topics-list").addEventListener("click", (event) => {
  const card = event.target.closest(".card");
  if (card) showTopic(card.dataset.topic);
});

$("#topic-detail").addEventListener("click", (event) => {
  const chip = event.target.closest("[data-entity]");
  const button = event.target.closest("[data-graph-topic]");
  if (chip) {
    show("graph");
    Graph.load({ entity: chip.dataset.entity });
  } else if (button) {
    show("graph");
    $("#graph-topic").value = button.dataset.graphTopic;
    Graph.load({ topic: button.dataset.graphTopic });
  }
});

// -- graph ---------------------------------------------------------------------------

const PALETTE = ["#4f5bd5", "#e4572e", "#29a19c", "#f3a712", "#a23b72", "#2e86ab", "#6a994e",
  "#c1666b", "#7b5ea7", "#d17a22", "#3c9d5d", "#b8336a"];

const Graph = {
  canvas: null,
  context: null,
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
        topics.map((t) => `<option value="${esc(t.id)}">${esc(t.title)} (${t.size})</option>`).join("");
    });
    this.load({ limit: 100 });
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
    if (!topic) return "#9aa0b4";
    if (!this.topicColors.has(topic)) this.topicColors.set(topic, PALETTE[this.topicColors.size % PALETTE.length]);
    return this.topicColors.get(topic);
  },

  radius(node) {
    return Math.min(24, 4 + Math.sqrt(node.mentions || 1) * 1.8);
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
      }
      data.topics.forEach((t) => this.topicTitles.set(t.id, t.title));
      const anchor = params.entity && this.byId.get(params.entity);
      for (const node of data.nodes) {
        if (this.byId.has(node.id)) continue;
        const angle = Math.random() * Math.PI * 2;
        const spread = anchor ? 60 : 250;
        const entry = {
          ...node,
          x: (anchor ? anchor.x : 0) + Math.cos(angle) * spread * Math.random(),
          y: (anchor ? anchor.y : 0) + Math.sin(angle) * spread * Math.random(),
          vx: 0,
          vy: 0,
        };
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
      this.nodes.forEach((n) => this.color(n.topic));
      this.alpha = 1;
      this.fitted = false;
      $("#graph-info").textContent = `${this.nodes.length} entities · ${this.edges.length} links`;
      this.legend();
      if (params.entity) this.select(this.byId.get(params.entity));
    } catch (error) {
      $("#graph-info").textContent = error.message.startsWith("404") ? "Not found." : error.message;
    }
  },

  legend() {
    const counts = new Map();
    this.nodes.forEach((n) => n.topic && counts.set(n.topic, (counts.get(n.topic) || 0) + 1));
    const top = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 10);
    $("#graph-legend").innerHTML = top.length
      ? "<h3>Topics</h3>" +
        top
          .map(
            ([id, n]) => `<div class="legend-item" data-topic="${esc(id)}"><span class="swatch" style="background:${this.color(id)}"></span>
              ${esc(this.topicTitles.get(id) || "Topic")} <span class="muted">${n}</span></div>`,
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
        const force = (2600 / d2) * this.alpha;
        const d = Math.sqrt(d2);
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
      const strength = 0.04 * Math.min(1, 0.4 + (edge.weight || 1) / 20) * this.alpha;
      const f = (d - rest) * strength;
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
      // Frame the graph once the layout has mostly settled, unless someone is moving it.
      if (this.alpha < 0.08 && !this.fitted && !this.pointer) {
        this.fit();
        this.fitted = true;
      }
    }
    this.draw();
    requestAnimationFrame((time) => this.frame(time));
  },

  /** Zoom and pan so every entity is in view. */
  fit() {
    if (!this.nodes.length || !this.width) return;
    const xs = this.nodes.map((n) => n.x);
    const ys = this.nodes.map((n) => n.y);
    const [left, right, top, bottom] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const margin = 60;
    const scale = Math.min((this.width - margin * 2) / (right - left || 1), (this.height - margin * 2) / (bottom - top || 1), 2);
    this.view = { scale: Math.max(0.2, scale), x: -(left + right) / 2, y: -(top + bottom) / 2 };
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
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    ctx.clearRect(0, 0, this.width, this.height);
    const focus = this.hover || this.selected;
    const near = focus ? this.neighbours(focus) : null;
    const { scale } = this.view;

    for (const e of this.edges) {
      const lit = focus && (e.source === focus || e.target === focus);
      const [x1, y1] = this.toScreen(e.source.x, e.source.y);
      const [x2, y2] = this.toScreen(e.target.x, e.target.y);
      ctx.strokeStyle = lit ? (dark ? "rgba(160,170,255,0.9)" : "rgba(79,91,213,0.8)") : dark ? "rgba(150,155,175,0.18)" : "rgba(90,95,120,0.16)";
      ctx.lineWidth = lit ? 1.8 : Math.min(3, 0.6 + (e.weight || 1) / 12);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }

    for (const node of this.nodes) {
      const [x, y] = this.toScreen(node.x, node.y);
      const r = this.radius(node) * Math.sqrt(scale);
      const faded = focus && node !== focus && !near.has(node);
      ctx.globalAlpha = faded ? 0.25 : 1;
      ctx.fillStyle = this.color(node.topic);
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
      if (node === this.selected) {
        ctx.lineWidth = 3;
        ctx.strokeStyle = dark ? "#fff" : "#1d1f27";
        ctx.stroke();
      }
      const labelled = node === focus || (near && near.has(node)) || r > 11 || scale > 1.6;
      if (labelled && !faded) {
        ctx.font = `${node === focus ? "600 " : ""}12px system-ui, sans-serif`;
        ctx.fillStyle = dark ? "#e7e8ee" : "#1d1f27";
        ctx.textAlign = "center";
        ctx.fillText(node.name, x, y + r + 13);
      }
    }
    ctx.globalAlpha = 1;
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
      const node = this.nodeAt(sx, sy);
      this.pointer = { node, sx, sy, moved: false, view: { ...this.view } };
      canvas.setPointerCapture(event.pointerId);
      canvas.classList.add("dragging");
    });
    canvas.addEventListener("pointermove", (event) => {
      const [sx, sy] = position(event);
      const p = this.pointer;
      if (!p) {
        const hover = this.nodeAt(sx, sy);
        this.hover = hover;
        canvas.style.cursor = hover ? "pointer" : "grab";
        return;
      }
      if (Math.abs(sx - p.sx) + Math.abs(sy - p.sy) > 3) p.moved = true;
      if (p.node) {
        const [x, y] = this.toWorld(sx, sy);
        p.node.x = x;
        p.node.y = y;
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
      if (p && !p.moved) this.select(p.node);
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

  async select(node) {
    this.selected = node || null;
    const panel = $("#graph-panel");
    if (!node) {
      panel.innerHTML = '<p class="muted">Click an entity to see what the graph knows about it.</p><div id="graph-legend"></div>';
      this.legend();
      return;
    }
    panel.innerHTML = `<h2>${esc(node.name)}</h2><p class="muted">Loading…</p>`;
    try {
      const e = await api(`/entities/${encodeURIComponent(node.id)}`);
      panel.innerHTML = `<h2>${esc(e.name)}</h2>
        <p class="muted">${esc(e.type)} · mentioned in ${e.mentions} passages</p>
        <p>${esc(e.summary || "")}</p>
        ${e.also_called.length ? `<p class="muted">Also called ${esc(e.also_called.join(", "))}</p>` : ""}
        ${e.topic ? `<p><span class="swatch" style="display:inline-block;background:${this.color(e.topic.id)}"></span> ${esc(e.topic.title)}</p>` : ""}
        <div class="actions">
          <button type="button" data-expand="${esc(e.id)}">Add neighbours</button>
          <button type="button" data-focus="${esc(e.id)}">Focus</button>
        </div>
        ${e.related.length ? `<h3>Related</h3><ul class="related">${e.related
          .slice(0, 12)
          .map((r) => `<li><span class="name" data-focus="${esc(r.id)}">${esc(r.name)}</span> ${esc(r.relationship || "")}</li>`)
          .join("")}</ul>` : ""}
        ${e.conversations.length ? `<h3>Mentioned in</h3>${e.conversations
          .slice(0, 10)
          .map((c) => `<div class="card" data-conversation="${esc(c.id)}"><div class="card-title">${esc(c.title || "(untitled)")}</div></div>`)
          .join("")}` : ""}`;
    } catch (error) {
      panel.innerHTML = `<p class="error">${esc(error.message)}</p>`;
    }
  },
};

$("#graph-panel").addEventListener("click", (event) => {
  const expand = event.target.closest("[data-expand]");
  const focus = event.target.closest("[data-focus]");
  const card = event.target.closest("[data-conversation]");
  const legend = event.target.closest(".legend-item");
  if (expand) Graph.load({ entity: expand.dataset.expand, limit: 30 }, true);
  else if (focus) Graph.load({ entity: focus.dataset.focus, limit: 60 });
  else if (card) openConversation(card.dataset.conversation);
  else if (legend) {
    $("#graph-topic").value = legend.dataset.topic;
    Graph.load({ topic: legend.dataset.topic });
  }
});

$("#graph-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const q = $("#graph-input").value.trim();
  if (!q) return Graph.load({ limit: 100 });
  const found = await api(`/entities?${new URLSearchParams({ q, limit: 1 })}`);
  if (found.length) Graph.load({ entity: found[0].id, limit: 60 });
  else $("#graph-info").textContent = `No entity matches “${q}”.`;
});

$("#graph-topic").addEventListener("change", (event) => {
  Graph.load(event.target.value ? { topic: event.target.value } : { limit: 100 });
});
