# The trained win-probability model and run-magnitude baseline table
# (data/artifacts/) are one-time batch outputs of the historical pipeline
# (backend/historical/) and get baked into the image at BUILD time: free
# hosting tiers have ephemeral filesystems, so the running app only ever reads
# what was copied in below. Only requirements-runtime.txt is installed (the
# full requirements.txt adds the offline pipeline and the test tools).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

# .dockerignore keeps tests, the offline pipeline, scripts and local caches out.
COPY backend/ backend/
COPY frontend/ frontend/
COPY data/artifacts/ data/artifacts/
COPY data/replay_fixtures/ data/replay_fixtures/

# Nothing here needs root, and the app writes nothing to disk.
RUN useradd --system --no-create-home app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4)"

# Shell form so $PORT (set by most PaaS hosts, e.g. Render) is honored, with
# 8000 as a sane local-Docker default when it's unset. The per-client limits
# count visitors by the proxy's X-Forwarded-For entry only when TRUST_PROXY=1
# (set it on a host that puts a proxy in front, e.g. Render; see render.yaml).
CMD uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000}
