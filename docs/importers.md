# Importing your data

```bash
chatlore import <path>            # detects the source from the content
chatlore import <path> --source chatgpt
chatlore import <path> --dry-run  # parse and report, write nothing
chatlore stats                    # what the library holds
```

`<path>` can be the zip exactly as you downloaded it, the extracted folder, or
the JSON file itself. Importing is idempotent: run it again after a fresh
export and only new or changed conversations are written.

Everything lands in `~/.chatlore/conversations/<source>/<id>.json` (override
the location with `CHATLORE_HOME`). The files are plain JSON and stay readable
without ChatLore.

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

What is kept: every message, text extracted from attachments, tool calls and
their results as text, and attachment names. Thinking blocks are dropped.
Newer exports record retries and edits as branches. The export does not say
which branch was on screen, so the branch ending in the newest message is
marked as active. Older exports are imported as a simple sequence.

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
