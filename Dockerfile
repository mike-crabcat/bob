# Bob — agent image (server + built dashboard + claude CLI harness + core skills).
# The agent spawns the `claude` CLI and skill scripts in bash, so the runtime
# stage carries Node + git + document tooling. See docs/bob-docker-plan.md.

# ---- dashboard SPA build ----
FROM node:22-bookworm-slim AS ui
WORKDIR /build
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --silent
COPY ui/ ./
# repo outDir (../server/ui_dist) doesn't exist in this stage; emit stage-local
RUN npm run build -- --outDir dist --emptyOutDir

# ---- python deps (editable project install at /app) ----
FROM python:3.12-slim-bookworm AS deps
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY server/ ./server/
RUN uv sync --frozen --no-dev

# ---- runtime ----
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      nodejs npm git curl ca-certificates tzdata \
      pandoc poppler-utils wkhtmltopdf fonts-liberation \
      libpango-1.0-0 libpangoft2-1.0-0 \
    && npm install -g @anthropic-ai/claude-code@2.1.232 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m bob

WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY server/ /app/server/
COPY --from=ui /build/dist/ /app/server/ui_dist/
COPY skills/ /app/skills/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && chown -R bob:bob /app /home/bob /entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/home/bob \
    TZ=UTC \
    PYTHONUNBUFFERED=1

USER bob
EXPOSE 8420
ENTRYPOINT ["/entrypoint.sh"]
CMD ["bob", "serve", "--host", "0.0.0.0", "--port", "8420", \
     "--data-dir", "/home/bob/data", "--config-dir", "/home/bob/config", \
     "--db-path", "/home/bob/data/bob.db"]
