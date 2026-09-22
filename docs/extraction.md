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
graph leads back to the messages it came from. Entities mentioned in several
places then get a short summary.

## What it costs

Each request carries four chunks and eight requests run at once. The model
reasons before it answers, so a request takes 10 to 30 seconds; most of the cost
is the answer, not the text sent. On a real Claude export with the default model:

| | Chunks | Time | Cost |
|---|---|---|---|
| Reading chunks, 4 per request, 12 at once | 50 | 3 min 10 s | about 4 US cents |
| Reading chunks, 8 per request, 12 at once | 48 | 3 min 28 s | about 2 US cents |
| Summarising 219 entities | | 3 min | about 2 US cents |

Eight chunks per request halves the cost but the model finds fewer entities,
about 4 per chunk instead of 6 to 7. The summary after each run shows the
tokens used, and `--limit` reads only that many new chunks, so check the output
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
the whole graph, summaries included, without calling the model.

## What an entity is

The model picks a type for each entity. It is asked to prefer person,
organization, project, tool, concept, place, and event, and may use another
single word when none fits; on a real export 93% of entities used the seven.
Types that clearly mean one of them, such as company, city, or language, are
counted as that one.

Mentions are one entity when their names match ignoring case, spacing, hyphens
and underscores between words, trailing punctuation, and a plural "s" on the
last word: "Drive D:" and "drive d", or "Challenge fees" and "Challenge fee".
Punctuation inside a name counts, so "C++", "C#", and "C" stay apart, and names
that only look alike, such as "Llama" and "Ollama", are never merged.

## Summaries

Every distinct description of an entity is kept. An entity described once uses
that description as its summary. One described more often gets a summary of at
most 50 words from the model, written from up to 20 of its descriptions and
cached by the entity's name and set of descriptions, so it is written again only
when a new description turns up.
