# The web interface

```bash
chatlore serve        # then open http://127.0.0.1:8000
```

`chatlore serve` hosts a web interface next to the REST API. It is plain HTML,
CSS, and JavaScript served from the package: no build step, no CDN, nothing
loaded from the internet, so it works offline like the rest of ChatLore. It
follows the system's light or dark setting.

## Views

- **Ask.** A chat thread. Start from a suggested question about one of your
  largest topics, or type your own; Enter asks, Shift+Enter starts a new line.
  The answer streams in with each claim cited as a small numbered badge, and the
  cited sources follow as cards. Clicking a badge points at its source; clicking
  a source opens the conversation at that message.
- **Search.** Smart search finds messages by words, meaning, and the entities they
  mention, and shows which of those matched each result; Exact words finds the
  words only, with the matches highlighted.
- **Conversations.** Every conversation, newest first, with a filter by title.
  Clicking one opens it as a chat.
- **Topics.** The topic reports from `chatlore extract` as cards, largest first,
  with a word filter. A topic opens with its summary, findings, and entities, and
  can be opened in the graph.
- **Graph.** The knowledge graph, drawn with a force-directed layout: each entity
  is a circle sized by how often it is mentioned and coloured by its topic, and
  related entities are linked.

## Your data

**Your data**, in the navigation, imports an export and downloads the library:

- Drop a file on the dialog, or choose one: a ChatGPT or Claude export (.zip),
  Gemini Takeout (.zip or MyActivity.json), Markdown notes (.zip or .md), or a
  ChatLore archive. Uploads may be up to 200 MB, or what `--max-upload-mb` sets.
- The import runs in the background, like `chatlore import`, `chatlore
  process`, and `chatlore extract` one after another, and the dialog shows each
  step with its progress: importing, preparing search, reading with the language
  model, summarising, linking names for the same thing, and topics. Without a
  model key, everything but the knowledge graph is built. When it is done,
  **Show the library** reloads the page on it.
- **ChatLore archive** downloads the whole library, graph and caches included,
  to import anywhere; **Markdown** downloads one readable file per conversation.
  See [export.md](export.md).

On your own machine the upload goes into the library itself. On a public server
that takes uploads, it goes into a private library of the visitor's own; see
[hosting.md](hosting.md#visitors-own-data).

## Exploring the graph

The graph fills the page, with a floating toolbar, a legend of the largest
topics shown, and the twenty most mentioned entities labelled. It starts with
the hundred most mentioned entities that have relationships. From there:

- type a name into **Find an entity** to show it with its closest neighbours;
- choose a topic from the list, or click one in the legend, to show its entities;
- click an entity to open a drawer with its summary, other names, topic,
  relationships, and the conversations it came up in;
- **Add neighbours** adds an entity's related entities to what is drawn, and
  **Focus** redraws the graph around it;
- drag entities or the background, scroll to zoom, and double-click or use the
  fit button to frame the graph; the reset button goes back to the overview.

Hovering or selecting an entity highlights its links and fades everything else.
The layout settles in a few seconds and then stops, so a large graph does not
keep the processor busy.

The graph view is backed by `GET /graph`, which returns entities, the links
among them, and their topics: around an entity with `entity=<id>`, for a topic
with `topic=<id>`, or the most mentioned entities otherwise, up to `limit`.
