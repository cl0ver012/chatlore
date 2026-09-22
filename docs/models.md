# Language models

Importing and searching never call a language model. Extraction and chat do,
and they talk to any OpenAI-compatible chat API. The model runs wherever you
point it: a hosted router or a server on your own machine.

```bash
chatlore doctor   # shows the configured model, reasoning, and whether a key is set
```

## OpenRouter (the default)

[OpenRouter](https://openrouter.ai) serves many open models, most of them
inexpensive, behind one API key. The default model is
`deepseek/deepseek-v4-flash` with reasoning switched off.

```bash
export OPENROUTER_API_KEY=sk-or-...
export CHATLORE_LLM_MODEL=qwen/qwen3-235b-a22b-2507   # optional: any OpenRouter model id
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

## Reasoning

Many models think before they answer. Extraction asks thousands of short,
well-specified questions, where that thinking mostly costs time and money, so
ChatLore asks for no reasoning by default. `CHATLORE_LLM_REASONING` sets it to
`off`, `low`, `medium`, or `high`, or to `default` to send nothing and leave it
to the model; use `default` for a server or model that rejects the setting.

The default model was chosen by measuring six fast models and one reasoning
model on the same chunks of a real Claude export, four chunks per request:

| Model | Seconds per request | Cost per 1,000 chunks | Usable answers | Notes |
|---|---|---|---|---|
| `deepseek/deepseek-v4-flash`, reasoning off | 7 | $0.08 | 12 of 12 | Precise entities, fewer relationships |
| `qwen/qwen3-235b-a22b-2507` | 23 | $0.33 | 12 of 12 | Precise, as many relationships as entities |
| `qwen/qwen3-30b-a3b-instruct-2507` | 26 | $0.20 | 12 of 12 | Many generic entities |
| `mistralai/mistral-small-3.2-24b-instruct` | 113 | $0.14 | 12 of 12 | Slow |
| `openai/gpt-4.1-nano` | 13 | $0.14 | 8 of 12 | Unusable answers |
| `google/gemini-2.5-flash-lite` | 13 | $0.43 | 8 of 12 | Unusable answers, many generic entities |
| `z-ai/glm-5.3-flash`, reasoning always on | 65 to 90 | about $1 | 12 of 12 | 70% of its output was hidden reasoning |

For a richer graph at about four times the cost, set
`CHATLORE_LLM_MODEL=qwen/qwen3-235b-a22b-2507`.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CHATLORE_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Where the OpenAI-compatible API lives |
| `CHATLORE_LLM_MODEL` | `deepseek/deepseek-v4-flash` | Model id as that server names it |
| `CHATLORE_LLM_REASONING` | `off` | `off`, `low`, `medium`, `high`, or `default` |
| `CHATLORE_LLM_API_KEY` | none | Key for the server; wins over `OPENROUTER_API_KEY` |
| `OPENROUTER_API_KEY` | none | Used only when the base URL is OpenRouter |

`OPENROUTER_API_KEY` is never sent to any other server, so switching the base
URL to a local or third-party server cannot leak it. Use `CHATLORE_LLM_API_KEY`
for a server that needs its own key. `chatlore doctor` says whether a key is set
but never prints it.
