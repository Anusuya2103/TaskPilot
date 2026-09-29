# Playwright's official image already ships Chromium + all system libraries, which
# is far more reliable than installing them by hand.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TASKPILOT_ARTIFACT_DIR=/app/artifacts \
    TASKPILOT_BROWSER_HEADLESS=true

WORKDIR /app

# Dependencies first so the layer caches across code changes.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

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
