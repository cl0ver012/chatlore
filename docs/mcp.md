# Using ChatLore from AI assistants (MCP)

```bash
chatlore mcp          # started by the assistant, not by hand
```

`chatlore mcp` is an [MCP](https://modelcontextprotocol.io) server. Once an
assistant such as Claude Desktop, Claude Code, or Cursor knows about it, the
assistant can look things up in your past conversations while you talk to it:
"how did I set up Litestream last time?", "what did I decide about the JR Pass?",
"what do I know about Proxmox?". It finds the passages, answers with its own
model, and cites the conversations they came from.

The server runs on your machine and talks to the assistant over standard input
and output, so it opens no port. It only reads the library. It needs no model key:
the assistant's model writes the answers, and only the local embedding model runs
here. Keep in mind that whatever the tools return is sent to the model the
assistant uses, as with anything else you paste into it.

The library should be imported and processed first; `chatlore extract` adds the
entities and topics that the `entity` and `topic` tools use.

## Setting it up

The assistant starts `chatlore mcp` itself, so it needs to find the `chatlore`
command. Either install it with `uv tool install chatlore`, or run it from a
clone with `uv --directory /path/to/chatlore run chatlore mcp`. Desktop apps do
not always see the same `PATH` as your terminal; if one cannot start the server,
give the full path to `chatlore` (`where chatlore` on Windows, `which chatlore`
elsewhere).

The library is the default one (`~/.chatlore`, or `CHATLORE_HOME`). To use
another, pass `--home` before `mcp`, or set `CHATLORE_HOME` in the server's
environment as below.

**Claude Code**

```bash
claude mcp add chatlore -- chatlore mcp
claude mcp add chatlore -e CHATLORE_HOME=/path/to/library -- chatlore mcp   # another library
```

Add `--scope user` to have it in every project. To try it on the made-up
library from `chatlore demo` first:

```bash
claude mcp add chatlore-demo -- chatlore --home ~/.chatlore-demo mcp
```

**Claude Desktop.** Settings, Developer, Edit Config opens
`claude_desktop_config.json`. Add the server and restart Claude Desktop:

```json
{
  "mcpServers": {
    "chatlore": {
      "command": "chatlore",
      "args": ["mcp"]
    }
  }
}
```

From a clone instead:

```json
{
  "mcpServers": {
    "chatlore": {
      "command": "uv",
      "args": ["--directory", "/path/to/chatlore", "run", "chatlore", "mcp"],
      "env": { "CHATLORE_HOME": "/path/to/library" }
    }
  }
}
```

**Cursor.** The same `mcpServers` entry goes in `~/.cursor/mcp.json` for every
project, or `.cursor/mcp.json` for one.

**Other clients.** Any MCP client that starts local servers works the same way:
the command is `chatlore` with the argument `mcp`. ChatGPT only connects to
servers on the internet, so it uses the hosted demo, below.

## Over HTTP

`chatlore serve` serves the same tools at `/mcp` over streamable HTTP, for
assistants that connect to a URL instead of starting a command:

```bash
chatlore serve                                                        # then:
claude mcp add --transport http chatlore http://127.0.0.1:8000/mcp
```

On your machine, `/mcp` only answers requests addressed to `127.0.0.1` or
`localhost`, so websites cannot reach it through your browser.

The hosted demo serves its made-up library the same way at its address followed
by `/mcp`, with no authentication (see [hosting.md](hosting.md)):

- **ChatGPT**: in developer mode, add a connector with that URL and no
  authentication.
- **Claude**: add it as a custom connector, or in Claude Code with
  `claude mcp add --transport http chatlore-demo <url>`.
- **Cursor**: an `mcpServers` entry with `"url": "<url>"` instead of a command.

Never put your own library on the internet this way: anyone with the address
could read it.

## Tools

| Tool | What it returns |
|---|---|
| `ask_context` | The passages that best answer a question, numbered for citation, with notes on the entities and topics it names. The same retrieval as `chatlore ask`, without the answer. |
| `search` | Messages matching words, meaning, and the entities they mention, optionally from one source. |
| `conversation` | A conversation's messages. Long ones come in parts of about 30,000 characters: from the start, or around a message found by the other tools. |
| `entity` | What the graph knows about a person, project, tool, or place: summary, other names, topic, relationships, and the conversations it came up in. |
| `topics` | The topics, largest first, or those whose report mentions some words. |
| `topic` | A topic's report: summary, findings, and every entity in it. |

Results are plain text and carry the ids that the other tools take, so an
assistant can go from a passage to its conversation, or from an entity to its
topic. Every tool is marked read-only. The server also tells the assistant when
to use it: when you refer to something you discussed or decided before.

## Checking it

`chatlore mcp` prints nothing: its output is the protocol. The
[MCP Inspector](https://github.com/modelcontextprotocol/inspector) lists the
tools and calls them by hand:

```bash
npx @modelcontextprotocol/inspector chatlore mcp
```

The first call that searches by meaning loads the embedding model, which takes a
few seconds; later calls are quick.
