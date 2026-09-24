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
