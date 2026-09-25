# Hosting the demo

```bash
docker build -t chatlore-demo .
docker run -p 7860:7860 -e OPENROUTER_API_KEY=sk-or-... chatlore-demo
```

The image in the repository's `Dockerfile` is the hosted demo: the made-up
library from `chatlore demo`, served to anyone who opens it. Visitors get the
web interface at `/`, the REST API with its docs at `/docs`, and the MCP server
at `/mcp`, so they can connect ChatGPT, Claude, or another assistant to it
without installing anything.

The demo library and the embedding model are built into the image, so a
container answers as soon as it starts. It used about 400 MB of memory once
searches had loaded the embedding model; give it 1 GB to be safe. It listens on
port 7860, or on `$PORT` when the host sets it.

## The model key

Search, conversations, topics, the graph, and every MCP tool work without a
key. Only asking in the web interface or through `POST /chat` needs a language
model, and that is paid for with the host's key. Pass it as a secret, never in
the image:

| Variable | Meaning |
|---|---|
| `OPENROUTER_API_KEY` | Key for OpenRouter, the default provider. |
| `CHATLORE_LLM_MODEL`, `CHATLORE_LLM_BASE_URL` | Another model or provider; see [models.md](models.md). |

Without a key, asking says that a key is needed and everything else works.
OpenRouter lets a key have a credit limit; give the demo's key one.

## Public mode

The image runs `chatlore serve --public`. Anyone can run the same on a library
they are happy for anyone to read:

```bash
chatlore --home ~/.chatlore-demo serve --public --host 0.0.0.0 --port 7860
```

`--public` is for a server behind a proxy on the internet, such as the HTTPS
front of a hosting service:

- Questions that reach the language model are limited: 10 an hour for each
  visitor and 300 a day for everyone together, by default. Change them with
  `--questions-per-hour` and `--questions-per-day`. A question that finds no
  passages is not counted, since it never reaches the model. When a limit is
  reached, the visitor is told why and how to install ChatLore instead.
- Visitors are told apart by the address the proxy reports, from
  `X-Forwarded-For`. Without a proxy in front, visitors could claim any address,
  but the daily limit still holds.
- `/mcp` accepts requests for any host name. A private server only answers
  requests addressed to this machine, which keeps websites from reaching it
  through a visitor's browser.
- The web interface says it is a demo, and `/health` reports `"public": true`.

The library is only ever read. Never serve your own library this way: anyone
who finds the address can read every conversation in it.

## Where to host it

Any service that builds and runs a Dockerfile from a Git repository works, and
gives the container an HTTPS address. ChatGPT only connects to HTTPS.

CI builds the image on every pull request, starts it, and checks the web
interface, the API, and a tool call over MCP.

## Connecting assistants to it

The MCP endpoint is the demo's address followed by `/mcp`, with no
authentication. See [mcp.md](mcp.md#over-http) for each assistant.
