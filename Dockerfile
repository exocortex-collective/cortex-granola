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

# No VOLUME for secrets. Both services share this image, and only granola-sync
# has a compose bind mount at /app/secrets -- so a VOLUME here made Docker
# create an anonymous, root-owned volume in the parse worker instead, leaking a
# fresh one on every recreate (`docker compose down` does not remove anonymous
# volumes without -v). Compose declares the real mount where it is needed.

# Overridden per service in docker-compose. The poller is the default because
# it is the one that must always be running.
CMD ["uv", "run", "python", "-m", "cortex_granola.services.sync"]
