# The web interface

```bash
chatlore serve        # then open http://127.0.0.1:8000
```

`chatlore serve` hosts a web interface next to the REST API. It is plain HTML,
CSS, and JavaScript served from the package: no build step, no CDN, nothing
loaded from the internet, so it works offline like the rest of ChatLore. It
follows the system's light or dark setting.

## Views

- **Ask.** Type a question and the answer streams in, with each claim cited as a
  small numbered badge. Clicking a badge shows its source; clicking a source
  opens the conversation at that message.
- **Search.** Finds messages by words, meaning, and the entities they mention,
  and shows which of those matched each result.
- **Conversations.** Every conversation, newest first, fifty at a time. Clicking
  one opens it.
- **Topics.** The topic reports from `chatlore extract`, largest first, with a
  word filter. A topic shows its summary, findings, and entities, and can be
  opened in the graph.
- **Graph.** The knowledge graph, drawn with a force-directed layout: each entity
  is a circle sized by how often it is mentioned and coloured by its topic, and
  related entities are linked.

## Exploring the graph

The graph starts with the hundred most mentioned entities that have
relationships. From there:

- type a name into **Find an entity** to show it with its closest neighbours;
- choose a topic from the list, or click one in the legend, to show its entities;
- click an entity to read its summary, other names, topic, relationships, and
  the conversations it came up in;
- **Add neighbours** adds an entity's related entities to what is drawn, and
  **Focus** redraws the graph around it;
- drag entities or the background, scroll to zoom, and double-click to fit the
  graph to the window.

Hovering or selecting an entity highlights its links and fades everything else.
The layout settles in a few seconds and then stops, so a large graph does not
keep the processor busy.

The graph view is backed by `GET /graph`, which returns entities, the links
among them, and their topics: around an entity with `entity=<id>`, for a topic
with `topic=<id>`, or the most mentioned entities otherwise, up to `limit`.
