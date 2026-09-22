# Extracting entities

```bash
chatlore process                 # chunk and embed first
chatlore extract --limit 50      # try the model on a few chunks
chatlore extract                 # read the rest; safe to repeat and to interrupt
```

`chatlore extract` sends your chunks to the configured language model (see
[models.md](models.md)) and asks which entities each one mentions and how they
relate. The answers become a graph: every entity is a node, related entities are
linked, and every chunk links to the entities it mentions, so anything in the
graph leads back to the messages it came from.

## What it costs

Each request carries a few chunks, and a few requests run at once. With the
default model, reading the sample exports in `tests/fixtures` (29 chunks) took
26 seconds and about half a US cent. The summary after each run shows the tokens
used, and `--limit` reads only that many new chunks, so you can check the output
and the cost on a small slice first. `--batch-size` and `--workers` change how
many chunks go into one request and how many requests run at the same time.

## Only new text is read

Answers are stored in `~/.chatlore/cache/extractions.db`, keyed by the model,
the prompt version, and the chunk text. A second run reads nothing it has read
before, an interrupted run resumes where it stopped, and an answer that could
not be read is asked again next time. Switching models reads everything again
with the new model.

The graph is rebuilt from those stored answers at the end of every run. Text
removed by a re-import takes its entities with it, and after
`chatlore index --rebuild` and `chatlore process`, `chatlore extract` restores
the whole graph without calling the model.

## What an entity is

The model picks a type for each entity. It is asked to prefer person,
organization, project, tool, concept, place, and event, and may use another
single word when none fits. Mentions whose names differ only in case or spacing
are one entity; every distinct description is kept. Merging different names for
the same thing, summarising descriptions, and grouping entities into topics come
next.
