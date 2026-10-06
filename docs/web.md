# The web interface

```bash
chatlore serve        # then open http://127.0.0.1:8000
```

`chatlore serve` hosts a web interface next to the REST API. It is plain HTML,
CSS, and JavaScript served from the package: no build step, no CDN, nothing
loaded from the internet, so it works offline like the rest of ChatLore. It
has a light and a dark theme: it follows the system's setting until you choose
one with the toggle at the bottom of the sidebar, and remembers that choice.

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
  can be explored in the graph.
- **Explore.** The knowledge graph and the chats it came from, side by side with
  a panel to read them in; see below.

## Your data

**Your data**, in the navigation, imports an export and downloads the library:

- Drop files or folders on the dialog, or choose files or a whole folder: chat
  exports, documents, notes, email, data files, and archives of any of them, as
  listed in [importers.md](importers.md#what-can-be-imported). Each file is sent
  with its path in its folder, then all of them are imported together. Uploads
  may be up to 200 MB in all, or what `--max-upload-mb` sets; folders such as
  `.git` and `node_modules` are left out before anything is sent.
- The import runs in the background, like `chatlore import`, `chatlore
  process`, and `chatlore extract` one after another, and the dialog shows each
  step with its progress: importing, preparing search, reading with the language
  model, summarising, linking names for the same thing, and topics. It then
  says what came from where, and lists the files it skipped with the reason.
  Without a model key, everything but the knowledge graph is built. When it is
  done, **Show the library** reloads the page on it.
- **ChatLore archive** downloads the whole library, graph and caches included,
  to import anywhere; **Markdown** downloads one readable file per conversation.
  See [export.md](export.md).

On your own machine the upload goes into the library itself. On a public server
that takes uploads, it goes into a private library of the visitor's own; see
[hosting.md](hosting.md#visitors-own-data).

## Exploring your conversations through the graph

**Explore** draws the knowledge graph together with the conversations it came
from, so the graph is a way into your chat history: entities are circles,
coloured by topic and sized by how often they come up, and chats are rounded
squares, coloured by where they came from. Solid lines link related entities,
dotted lines link an entity to the chats that mention it.

- It starts with an overview: the most mentioned entities and the chats that
  talk about them most.
- Click an entity and the graph centres on it, with the entities related to it
  and the chats that mention it. The panel beside the graph shows its summary,
  the facts established about it, each linking to the message that said it, the
  chats it came up in, and related entities.
- Click a chat and it opens in the panel to read, while the graph centres on it:
  the entities it mentions, and the other chats that share the most of them.
  Every entity the chat mentions is a link, in a row at the top and wherever its
  name appears in the text; clicking one steers the graph to that entity while
  the chat stays open. Tool output from coding agents is folded away.
- A trail above the graph lists where you have been, from the overview on;
  click any step to go back to it. The panel has its own back button.
- **Find an entity or a chat** suggests both as you type, by any part of a name.
- The timeline at the bottom shows how many chats started each month; drag
  across it to keep only the chats from those months, and **All time** to undo.
  The source buttons in the toolbar keep only the chats from some sources.
- Click a topic in the legend to see its entities and the chats about them.
- Drag nodes or the background, scroll to zoom, and double-click or use the fit
  button to frame the graph; the reset button goes back to the overview and
  clears the filters.

Hovering over a node shows what it is and highlights its links. The layout
settles in a few seconds and then stops, so it does not keep the processor busy,
and the nodes still shown keep their place when you move on.

A chat opened from **Search**, **Ask**, or **Conversations** also shows its
entities as links, and **Explore in the graph** opens it there.

Explore is backed by `GET /explore`, which returns entities, chats, the links
among them, and the entities' topics: around an entity with `entity=<id>`, a chat
with `conversation=<id>`, or a topic with `topic=<id>`, or an overview. `source`
(repeatable), `since`, and `until` (a month such as `2026-03`, or a day) keep
only some chats, by where they came from and the date they started. `entities`
and `conversations` set how many of each, 40 and 16 by default. `GET
/explore/timeline` counts the chats started each month, and `GET
/conversations/{id}` lists the entities a chat mentions with the messages they
are in. `GET /graph`, the entity-only graph, is still there.
