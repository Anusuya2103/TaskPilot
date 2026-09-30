# Playwright's official image already ships Chromium + all system libraries, which
# is far more reliable than installing them by hand.
#
# Use the *-noble variant: the older *-jammy tag ships Python 3.10, and this
# project requires >=3.11 (`from datetime import UTC` needs 3.11). Noble has
# 3.12. The browser builds are pinned to the Playwright version, so Chromium
# stays in step with the pip package.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TASKPILOT_ARTIFACT_DIR=/app/artifacts \
    TASKPILOT_BROWSER_HEADLESS=true

WORKDIR /app

# Dependencies first so the layer caches across code changes.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

# `uvx` is how the MCP bridge launches stdio servers (see TASKPILOT_MCP_SERVERS).
# The Playwright base image has no uv and no Node, so without this every
# configured MCP server dies with FileNotFoundError and the Settings page shows
# a red "error" per server. Installing uv (not Node) keeps the image small:
# the Python-hosted servers all run through `uvx`.
#
# The cache is pre-warmed here and chowned to the non-root runtime user below, so
# the servers also start on a machine with no network — the alternative is a demo
# that works on the developer's wifi and silently shows zero MCP tools anywhere
# else. Server *launch* stays tolerant of failure: MCPManager.start() reports
# rather than raises, and /ready surfaces `mcp: ok: false`.
ENV UV_CACHE_DIR=/app/.uv-cache
# `|| true`: a warm-up that fails (no network at build time) must not break the
# image. The servers still resolve on first launch, just with a cold cache.
RUN pip install "uv==0.5.11" && \
    mkdir -p "$UV_CACHE_DIR" && \
    (uvx mcp-server-fetch --help >/dev/null 2>&1 || true) && \
    (uvx mcp-server-git --help >/dev/null 2>&1 || true)

COPY backend/ /app/
# demo-apps lives outside backend/ in the repo, so mount or copy it for eval runs.
COPY demo-apps/ /app/demo-apps/

RUN mkdir -p /app/artifacts && \
    adduser --disabled-password --gecos "" --uid 10001 taskpilot && \
    chown -R taskpilot:taskpilot /app
USER taskpilot

WORKDIR /app

# Apply migrations, then serve. If the database is unreachable the server still
# starts and /ready reports the failure rather than crash-looping.
RUN printf '%s\n' \
  '#!/bin/sh' \
  'set -e' \
  'alembic upgrade head || echo "[taskpilot] migration failed; continuing (the API will report it on /ready)"' \
  'exec "$@"' \
  > /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
