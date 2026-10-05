# Language models

Importing and searching never call a language model. Extraction and chat do,
and they talk to any OpenAI-compatible chat API, or to Anthropic's or Google's
own API. The model runs wherever you point it: a hosted router, a model
provider, or a server on your own machine.

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

## Anthropic and Gemini

Claude and Gemini models can also be used through their makers' own APIs, with
the official SDKs, which install as extras:

```bash
uv tool install 'chatlore[anthropic]'      # or: pip install 'chatlore[anthropic]'
export CHATLORE_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

```bash
uv tool install 'chatlore[gemini]'
export CHATLORE_LLM_PROVIDER=gemini
export GEMINI_API_KEY=...                   # or GOOGLE_API_KEY
```

| Provider | Default model | Key |
|---|---|---|
| `anthropic` | `claude-opus-5-5` | `ANTHROPIC_API_KEY` |
| `gemini` | `gemini-3.5-flash-lite` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |

`CHATLORE_LLM_MODEL` picks another model, such as `claude-sonnet-5-5`,
`claude-haiku-4-5`, or `gemini-3.8-flash`. Each key is only sent to its own
provider. A model that declines a passage, or stops at the token limit, is
treated like an empty answer: extraction skips that batch and asks again next
time. Claude Opus 5.5, Opus 5, Sonnet 5.5, and Fable 5.1 ask the API to rerun a
declined request on its recommended fallback model.

Current Claude and Gemini models always think a little, so `off` asks for the
least thinking they allow: effort `low` for Claude, thinking level `minimal` for
Gemini. `low`, `medium`, and `high` are passed on as they are. Claude Haiku 4.5
takes no effort setting; use it with `CHATLORE_LLM_REASONING=default`.

These clients are new and were tested against recorded answers, not measured on
a real export like the models below.

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
| `CHATLORE_LLM_PROVIDER` | `openai` | `openai` for any OpenAI-compatible API, `anthropic`, or `gemini` |
| `CHATLORE_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Where the API lives; each provider has its own default |
| `CHATLORE_LLM_MODEL` | `deepseek/deepseek-v4-flash` | Model id as that server names it; each provider has its own default |
| `CHATLORE_LLM_REASONING` | `off` | `off`, `low`, `medium`, `high`, or `default` |
| `CHATLORE_LLM_API_KEY` | none | Key for the server; wins over `OPENROUTER_API_KEY` |
| `OPENROUTER_API_KEY` | none | Used only when the base URL is OpenRouter |
| `ANTHROPIC_API_KEY` | none | Used only with `CHATLORE_LLM_PROVIDER=anthropic` |
| `GEMINI_API_KEY`, `GOOGLE_API_KEY` | none | Used only with `CHATLORE_LLM_PROVIDER=gemini` |

`OPENROUTER_API_KEY` is never sent to any other server, so switching the base
URL to a local or third-party server cannot leak it. Use `CHATLORE_LLM_API_KEY`
for a server that needs its own key. `chatlore doctor` says whether a key is set
but never prints it.
