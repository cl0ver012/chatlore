# Language models

Importing and searching never call a language model. Extraction and chat do,
and they talk to any OpenAI-compatible chat API. The model runs wherever you
point it: a hosted router or a server on your own machine.

```bash
chatlore doctor   # shows the configured model and whether a key is set
```

## OpenRouter (the default)

[OpenRouter](https://openrouter.ai) serves many open models, most of them
inexpensive, behind one API key. The default model is `z-ai/glm-5.3-flash`.

```bash
export OPENROUTER_API_KEY=sk-or-...
export CHATLORE_LLM_MODEL=qwen/qwen3-235b-a22b   # optional: any OpenRouter model id
```

Or keep the key in a `.env` file where you run `chatlore`. The nearest `.env`
in the current folder or a parent folder is read on start, and variables already
set in the shell win over it. Keep `.env` out of version control; this repository
already ignores it.

```bash
echo "OPENROUTER_API_KEY=sk-or-..." > .env
```

## A local server

Ollama, vLLM, LM Studio, and llama.cpp all serve the same API. Point ChatLore
at one and nothing leaves your machine. Local servers need no key.

```bash
export CHATLORE_LLM_BASE_URL=http://localhost:11434/v1   # Ollama
export CHATLORE_LLM_MODEL=qwen3:8b
```

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CHATLORE_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Where the OpenAI-compatible API lives |
| `CHATLORE_LLM_MODEL` | `z-ai/glm-5.3-flash` | Model id as that server names it |
| `CHATLORE_LLM_API_KEY` | none | Key for the server; wins over `OPENROUTER_API_KEY` |
| `OPENROUTER_API_KEY` | none | Used only when the base URL is OpenRouter |

`OPENROUTER_API_KEY` is never sent to any other server, so switching the base
URL to a local or third-party server cannot leak it. Use `CHATLORE_LLM_API_KEY`
for a server that needs its own key. `chatlore doctor` says whether a key is set
but never prints it.
