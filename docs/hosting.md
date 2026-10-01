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

The server's library is only ever read. Never serve your own library this way:
anyone who finds the address can read every conversation in it.

## Visitors' own data

The image also runs with `--uploads`, which lets visitors try ChatLore on their
own conversations:

```bash
chatlore --home ~/.chatlore-demo serve --public --uploads --host 0.0.0.0 --port 7860
```

- A visitor who uploads an export under **Your data** gets a library of their
  own, and everything they then see, search, and ask is theirs alone. Everyone
  else still sees the server's library.
- The library is tied to the visitor's browser by a random token in a cookie
  (HttpOnly; over HTTPS also Secure, SameSite=None, and Partitioned, so it works
  when another site shows the demo in a frame, as a Hugging Face Space's page
  does, kept apart for each such site). Its folder is named after a
  hash of the token, so the server's files do not give the token away.
- It is deleted after 24 hours, or `--keep-hours`, and at once when the visitor
  chooses **Delete my library**; an import still running is stopped first. The
  server looks for expired libraries every ten minutes. They are kept in
  `~/.chatlore-spaces`, or `--spaces-dir`, each with its graph in its own SQLite
  file, even when the server's library is on FalkorDB.
- Uploads are imported one at a time, so the server keeps answering while one
  runs, and each visitor has one import at a time. An upload may be up to
  200 MB, or `--max-upload-mb`; a zip may unpack to at most 2 GB of text.
- Every change (an upload, a deletion) must carry the header the web interface
  sends, `X-ChatLore: 1`, so another website cannot make a visitor's browser
  upload or delete.
- Building the knowledge graph from an upload reads all of it with the
  language model on the server's key, as `chatlore extract` does locally, so a
  large export costs as much as extracting it at home. `--extract-limit` caps
  how many passages of each upload the model reads; the rest stays searchable.
  The dialog tells visitors that their conversations go to that model.
- Visitors download their library as a ChatLore archive or as Markdown.

Assistants connected over `/mcp` see the server's library, since they carry no
visitor's cookie.

## Where to host it

Any service that builds and runs a Dockerfile from a Git repository works, and
gives the container an HTTPS address. ChatGPT only connects to HTTPS.

### Hugging Face Spaces

The repository deploys the demo to a [Hugging Face Space](https://huggingface.co/docs/hub/spaces-sdks-docker),
which is free on the basic CPU hardware (2 vCPUs, 16 GB of memory), with the
workflow in `.github/workflows/deploy.yml`:

1. Create a Hugging Face [access token](https://huggingface.co/settings/tokens)
   with write access, and add it to the GitHub repository as the secret
   `HF_TOKEN` (Settings → Secrets and variables → Actions).
2. Add the repository variable `HF_SPACE` with the Space's name, such as
   `your-name/chatlore`, on the same page.
3. Run the **Deploy** workflow from the Actions tab. It creates the Space on the
   first run and uploads the Dockerfile with what it builds; the Space then
   builds the image and starts it, which takes a few minutes. Every version
   tagged afterwards is deployed the same way.
4. In the Space's settings, add the secret `OPENROUTER_API_KEY` for asking, with
   a credit limit on the key. The Space restarts with it.

The demo is then at `https://huggingface.co/spaces/<owner>/<name>`, and on its
own at `https://<owner>-<name>.hf.space`, which is the address to give
assistants: `https://<owner>-<name>.hf.space/mcp`.

A free Space sleeps after two days without visitors and wakes when someone
opens it. Its disk is emptied whenever it restarts, which also deletes
visitors' libraries early; the demo library is in the image and comes back.

CI builds the image on every pull request, starts it, and checks the web
interface, the API, and a tool call over MCP. It then uploads an export as a
visitor, waits for the import, checks that only that visitor sees it, downloads
it, and deletes it.

## Connecting assistants to it

The MCP endpoint is the demo's address followed by `/mcp`, with no
authentication. See [mcp.md](mcp.md#over-http) for each assistant.
