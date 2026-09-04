FROM python:3.13-slim

WORKDIR /app

# Non-root by default. This service reads an API key and writes into a mounted
# vault; there is no reason for it to run as root, and postmark's parse-worker
# already sets the precedent.
RUN useradd -m appuser

# git is needed because cortex-utils is a git dependency pinned to a SHA.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

RUN uv sync --frozen --no-dev

RUN chown -R appuser:appuser /app
USER appuser

# Secrets are mounted read-only at runtime; nothing is baked into the image.
VOLUME /app/secrets

# Overridden per service in docker-compose. The poller is the default because
# it is the one that must always be running.
CMD ["uv", "run", "python", "-m", "cortex_granola.services.sync"]
