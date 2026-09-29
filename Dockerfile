# The hosted ChatLore demo: the made-up demo library behind the public web
# interface, API, and MCP endpoint. See docs/hosting.md.
#
#   docker build -t chatlore-demo .
#   docker run -p 7860:7860 -e OPENROUTER_API_KEY=... chatlore-demo

FROM ghcr.io/astral-sh/uv:0.12-python3.12-trixie-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so changing the code does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Hugging Face Spaces and many other hosts run containers as user 1000.
RUN useradd --create-home --uid 1000 chatlore
USER chatlore
ENV HOME=/home/chatlore PATH=/app/.venv/bin:$PATH

# Load the demo library and download the embedding model while building, so
# the container answers as soon as it starts.
RUN chatlore demo --no-serve \
    && chatlore --home "$HOME/.chatlore-demo" search --semantic "warm up" > /dev/null

EXPOSE 7860
# PORT is set by hosts such as Render and Fly.io; Hugging Face Spaces uses 7860.
CMD ["sh", "-c", "exec chatlore --home \"$HOME/.chatlore-demo\" serve --public --host 0.0.0.0 --port \"${PORT:-7860}\""]
