# Chat and the REST API

```bash
chatlore ask "Can I run a 27B model on my RTX 4060?"   # answer with sources
chatlore serve                                         # REST API on http://127.0.0.1:8000
```

## Asking

`chatlore ask` answers a question from your own conversations and cites the
passages it used, like `[2]`, then lists them:

```text
No, you cannot run a 27B model comfortably on an RTX 4060 (8GB VRAM) [2]. A
typical Q4_K_M quantized 27B model is ~16–17 GB, far exceeding 8 GB [2]. You can
partially offload to system RAM, but speeds drop to ~2–5 tok/s [2].

Sources
  [2] Running 27B model on RTX 4060  claude | 2026-08-21
```

It needs a model key (see [models.md](models.md)) and works best after
`chatlore process`, which embeds the chunks, and `chatlore extract`, which builds
the entities and topics. Without embeddings it still answers from the entities
the question names, and says that `process` would help. `--sources` sets how
many passages the answer may draw on; the default is 8.

On a real Claude export with the default model, answers streamed in 7 to 9
seconds and cost a fraction of a US cent each.

### How the passages are chosen

Two ranked lists of chunks are merged with reciprocal rank fusion:

- the chunks nearest the question's meaning, when embeddings exist;
- the chunks that mention an entity the question names. Every run of up to four
  words is compared with the entities' names, so "claude-code" finds Claude Code.
  With embeddings, each entity offers the chunks among its mentions that are
  nearest the question; without them, its most recent ones.

The model also gets the summaries of the named entities and the reports of their
topics as background. It is told to answer only from the numbered passages, to
cite every claim, to prefer the most recent passage when they disagree, and to
say so when the passages do not hold the answer. When nothing matches at all,
the model is not called.

## The REST API

`chatlore serve` starts the API, with interactive documentation at `/docs` and
the web interface at `/` (see [web.md](web.md)). It listens on `127.0.0.1` only,
so nothing outside this machine can reach your library; `--host 0.0.0.0` changes
that and prints a warning. `--port` sets the port, 8000 by default.

| Endpoint | What it returns |
|---|---|
| `GET /health` | `{"status": "ok", "version": ...}` |
| `GET /stats` | Counts of conversations, messages, chunks, embeddings, entities, and topics |
| `GET /search?q=...` | Matching messages; `mode=hybrid` (default) or `words`, `limit`, repeatable `source` |
| `GET /conversations` | Conversations, newest first; `source`, `limit`, `offset` |
| `GET /conversations/{id}` | One conversation with its messages |
| `GET /entities?q=...` | Entities matching the words, or the most mentioned ones |
| `GET /entities/{id}` | An entity with its descriptions, other names, topic, relationships, and conversations |
| `GET /topics?q=...` | Topics matching the words, or all topics, largest first |
| `GET /topics/{id}` | A topic's report and its entities |
| `GET /graph` | Entities, the links among them, and their topics, for drawing; `entity`, `topic`, `limit` |
| `POST /chat` | A streamed answer, below |

### Streaming chat

`POST /chat` takes `{"question": "...", "sources": 8}` and answers with
[server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html),
each carrying JSON:

| Event | Data |
|---|---|
| `sources` | The numbered passages the answer may cite: title, source, date, role, conversation and message ids, text |
| `token` | `{"text": "..."}`, the next piece of the answer |
| `done` | `{"cited": [2, 4], "found": true}`; `found` is false when nothing matched |
| `error` | `{"message": "..."}` when the model could not answer |

`sources` always comes first, then any number of `token` events, then `done` or
`error`.

```bash
curl -N -X POST http://127.0.0.1:8000/chat \
  -H "content-type: application/json" \
  -d '{"question": "Can I run a 27B model on my RTX 4060?"}'
```
