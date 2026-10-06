# Importing your data

```bash
chatlore import <path>...         # anything: files, folders, archives
chatlore import <path> --source chatgpt
chatlore import <path> --dry-run  # parse and report, write nothing
chatlore stats                    # what the library holds
```

Give it as many files, folders, and archives as you like, and it works out what
each one is from its content: the export zip exactly as you downloaded it, the
extracted folder, a folder of documents, or all of them at once. The web
interface does the same under **Your data**, for files or a whole folder.
Importing is idempotent: run it again after a fresh export and only new or
changed conversations are written.

Everything lands in `~/.chatlore/conversations/<source>/<id>.json` (override
the location with `--home` or `CHATLORE_HOME`). The files are plain JSON and stay readable
without ChatLore. The same import also updates `~/.chatlore/chatlore.db`, a
SQLite database holding the graph and the search indexes.

An archive written by `chatlore export` is recognised too, and brings its
knowledge graph and caches along; see [export.md](export.md).

## What can be imported

| What | Files | Becomes |
|---|---|---|
| Chat exports | ChatGPT and Claude `conversations.json`, Gemini `MyActivity.json`, wherever they sit | conversations |
| Coding agent sessions | Claude Code and Codex CLI session files (`.jsonl`), wherever they sit | one conversation per session, source `claude_code` or `codex` |
| Notes | Markdown and text (`.md`, `.markdown`, `.txt`), Obsidian vaults | notes, source `markdown` |
| Documents | PDF, Word (`.docx`), PowerPoint (`.pptx`), Excel (`.xlsx`), OpenDocument (`.odt`, `.odp`, `.ods`), EPUB, RTF, web pages (`.html`) | notes, source `document` |
| Data | CSV and TSV, JSON and JSON Lines, XML | notes: a line per row, or `key.path: value` lines |
| Code and text | source code, `.log`, `.rst`, `.org`, subtitles, and any other file that is plain text | notes; code keeps its language |
| Email | `.eml`, and `.mbox` mailboxes | one conversation per email, source `email` |
| Archives | zip, tar, tar.gz, tgz, tar.bz2, tar.xz, gz, 7z, rar | whatever they hold |

**Archives** are unpacked, and archives inside them too, four levels deep, so a
zip of folders of zips works, and so does a Google Takeout split into several
zips. zip, tar, and gz need nothing extra. 7z and rar are unpacked with
`bsdtar`, which comes with Windows 10 and later and with macOS; on Linux,
install `libarchive-tools`. Together they may unpack to at most 2 GB, 100,000
files, and nothing is ever written outside the folder they are unpacked into.

**Chat exports** are found by their content, not their names or places. The
other files of an export, such as ChatGPT's `chat.html`, which holds the same
chats again, are skipped. In a Google Takeout, Gemini's activity is imported
and other products' activity is skipped, while other files, such as Drive
documents, are read like any others.

**Documents** become notes of one message each, titled from the document when
it names itself (a Word title, a PDF's metadata, a web page's `<title>`) and
otherwise by file name. Each is known by its path within what was imported, so
importing the same folder again updates the same notes. A scanned PDF with no
text layer has no text to read and is skipped.

**Skipped files** are listed at the end with the reason: pictures, audio,
video, programs, files that are not text, and documents that could not be read.
Folders such as `.git` and `node_modules` are left out, and so are system files
like `.DS_Store`.

## Searching

```bash
chatlore search "postgres index"          # every word must match, any order
chatlore search "why was my query slow" --semantic   # match by meaning
chatlore search "slow postgres query" --hybrid       # words, meaning, and entities together
chatlore search "cat" --source claude     # limit to one source, repeatable
chatlore search "cat" --limit 25
chatlore index --rebuild                  # rebuild the database from the library
```

## Processing

```bash
chatlore process              # chunk, then embed; safe to repeat and to interrupt
chatlore process --no-embed   # chunk only
chatlore process --reembed    # drop all vectors first, needed after changing the model
```

Processing prepares the library for semantic search in two stages.

**Chunking.** Each message is split into chunks of about 400 tokens made of whole
paragraphs and whole code blocks, with a short overlap between prose chunks. Only
what you and the assistant wrote is chunked. Tool output and attachment dumps are
left out on purpose: they stay findable through full-text search, but embedding
them would bury your actual conversations under scraped pages and JSON.

**Embedding.** Every chunk gets a vector from a small model that runs on your own
machine (`BAAI/bge-small-en-v1.5` through fastembed, about 130 MB, downloaded once
into `~/.chatlore/cache/models`). Nothing is sent anywhere. Expect a few chunks per
second on a laptop CPU, so a couple of thousand chunks take several minutes the
first time. Set `CHATLORE_EMBEDDING_MODEL` to use another fastembed model, then
run `chatlore process --reembed`.

Both stages only touch what changed. Vectors are also cached by text hash in
`~/.chatlore/cache/embeddings.db`, so rebuilding the database with
`chatlore index --rebuild` does not cost embedding time again.

Plain search matches words, with accents and punctuation ignored. `--semantic`
matches meaning instead, using the embeddings from `chatlore process`, and shows a
similarity score per hit. `--hybrid` runs both and merges the two rankings with
reciprocal rank fusion, so a message found by its words and by its meaning comes
first, and each hit says which way it was found. Tool output that was never
chunked still turns up through its words. After `chatlore extract`, messages
that mention an entity matching the query join in as well; see
[extraction.md](extraction.md).
The library is the source of truth: if the database is ever deleted or an
upgrade changes its layout, `chatlore index --rebuild` recreates it.

## ChatGPT

1. ChatGPT > Settings > Data controls > Export data.
2. Wait for the email and download the zip.
3. `chatlore import chatgpt-export.zip`

What is kept: every visible message, including answers you regenerated or
messages you edited. Those are stored as branches, and the branch that was on
screen is marked as the active one. Code blocks, tool output, browsing quotes,
and attachment names are kept. Hidden system prompts and the model's internal
reasoning records are dropped. Images are recorded as `[image]` placeholders;
the image files themselves are not imported yet.

## Claude

1. Claude > Settings > Privacy > Export data.
2. Wait for the email and download the zip.
3. `chatlore import claude-export.zip`

Newer exports arrive as a manifest plus several zips split by category
(`conversations-000.zip`, `projects-000.zip`, and so on). Import the
conversations zip; the others are not read yet.

What is kept: every message, text extracted from attachments, tool calls and
their results as text, and attachment names. Thinking blocks and system-injected
prompts are dropped, as are tool blocks the app hid from the chat. Newer exports
record retries and edits as branches. The export does not say which branch was
on screen, so the branch ending in the newest message is marked as active.
Older exports are imported as a simple sequence.

Expect a large share of skipped records. Real exports contain many conversations
whose every message is empty, with no title, text, or content, sometimes only a
file reference. They are reported once as `no readable messages` and skipped.

## Gemini

1. Go to [takeout.google.com](https://takeout.google.com), deselect everything,
   then select **My Activity**.
2. Under "All activity data included" pick only **Gemini Apps**, and under
   "Multiple formats" set the activity format to **JSON**.
3. `chatlore import takeout.zip`

Limitations you should know about:

- Takeout has one entry per prompt and **no conversation ids**, so the original
  conversations cannot be rebuilt. Prompts less than 30 minutes apart are
  grouped into one conversation. This is a heuristic.
- Some entries come without the response. The prompt is still imported.
- Only the JSON format is supported. The HTML format is rejected with a message
  telling you how to re-export.
- Non-prompt activity such as feedback is ignored.

## Claude Code and the Codex CLI

```bash
chatlore import ~/.claude/projects        # every Claude Code session
chatlore import ~/.codex/sessions         # every Codex CLI session
```

Coding agents keep each session as a JSON Lines file: Claude Code under
`~/.claude/projects/<project>/`, the Codex CLI under
`~/.codex/sessions/<year>/<month>/<day>/`. Each session becomes one
conversation, recognised by its records wherever the file sits, so a zip of
session files imports too. Importing again adds what was said since.

- What you asked and what the agent answered are kept as text, and are chunked,
  embedded, and read into the knowledge graph like any chat.
- Tool calls and their results are kept as parts of the messages, each
  shortened to 2,000 characters, since they are mostly file contents and logs.
  They are found by word search but not chunked.
- Hidden reasoning, subagent side chains, slash commands and their output, and
  the context the tools add to your turn, such as Codex's environment details,
  are left out.
- The title is the one you gave the session in Claude Code, else its summary,
  else the first line you typed. The project folder and git branch are kept as
  metadata.
- A Claude Code session that was compacted stays one conversation from start to
  end.

Cursor keeps its chats in an internal database rather than in files, and is
not read yet.

## Markdown and Obsidian

```bash
chatlore import ~/Documents/my-vault
chatlore import notes/meeting.md
```

Every `.md`, `.markdown`, and `.txt` file becomes one entry. The title comes
from the frontmatter `title`, then the first `# heading`, then the file name.
Frontmatter `created` / `date` and `updated` / `modified` become timestamps,
the rest of the frontmatter is kept as metadata, and `[[wikilinks]]` are
recorded. `.obsidian`, `.git`, `.trash`, and `node_modules` are skipped. A file
is identified by its path inside the imported folder, so moving the vault does
not create duplicates.

## Notes

```bash
chatlore note "Ask Sam about the staging database" --title "Follow up"
```

## When a record is skipped

A record that cannot be parsed is skipped and listed at the end of the import
with the reason. One odd conversation never aborts the rest.

The importers were written against the export structures as documented by the
community and are tested with synthetic fixtures. Vendors change their formats
without notice. If a real export fails or loses content, please open an issue
with the error line and, if you can, a redacted sample of the record.
