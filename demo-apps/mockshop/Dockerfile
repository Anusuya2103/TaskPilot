FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# python-multipart is required: the app mounts form endpoints (the lab-report
# form), and FastAPI refuses to start without it. It is easy to miss because
# the local dev venv already has it via backend/requirements.txt.
RUN pip install --no-cache-dir "fastapi>=0.115,<0.120" "uvicorn[standard]>=0.32,<0.40" \
    "python-multipart>=0.0.12"

COPY app.py /app/app.py

EXPOSE 9000
HEALTHCHECK --interval=20s --timeout=4s --start-period=8s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9000/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
