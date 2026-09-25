# Moving and exporting a library

```bash
chatlore export chatlore.zip                 # everything, to import elsewhere
chatlore import chatlore.zip                 # on the other machine, or into another library
chatlore export conversations/ --markdown    # one readable file per conversation
```

## Archives

`chatlore export <file>` writes the whole library into one zip file:

- every conversation, with all its branches;
- the knowledge graph built from them: chunks, entities, relationships, and topics;
- the cached embeddings and model answers.

`chatlore import <file>` recognises an archive and restores all of it. The graph
is back at once, with no model calls and no API key, and `chatlore process`
then takes the embeddings from the archive instead of computing them. The
database itself is not in the archive, since it is rebuilt from these parts.

Use it to move a library to another machine, to keep a backup, or to share a
library with someone. Remember that an archive holds your conversations in full.

Importing works like any other import:

- Importing the same archive twice changes nothing.
- An archive can go into a library that already has conversations. Both sets
  are kept, and a conversation in both is replaced by the archive's copy only
  when the two differ.
- The caches are merged, so a later `chatlore extract` with the same model reads
  nothing twice. With another model, `chatlore extract` rebuilds the entity
  graph from that model's answers instead.
- `--dry-run` counts what the archive holds without writing anything.

An archive is a zip holding `manifest.json`, `conversations.jsonl` with one
conversation per line in ChatLore's own format, `graph.jsonl` with one node or
edge per line, and the caches as SQLite files under `cache/`. The manifest says
which version of the format it uses; a ChatLore too old for it says so instead
of importing half of it.

## Markdown

`chatlore export <folder> --markdown` writes one Markdown file per
conversation, in a folder per source:

```
conversations/
  chatgpt/2026-06-02 Moving Tidewater from Postgres to SQLite.md
  claude/2026-02-03 Choosing a stack for Tidewater.md
  note/2026-05-02 Weekend.md
```

Each file starts with front matter (title, source, id, dates, model), then has
a heading per message with who wrote it and when. Code keeps its fences.
Where a conversation branched because a message was edited or regenerated, the
file holds the branch the conversation ended on, as ChatLore shows it.
Exporting again into the same folder overwrites the same files.

The files open in any editor or notes app. They are for reading, not for moving
a library: importing them back would add them as Markdown notes next to the
original conversations. Use an archive for that.
