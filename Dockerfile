# The trained win-probability model and run-magnitude baseline table
# (data/artifacts/) are one-time batch outputs of the historical pipeline
# (backend/historical/) and get baked into the image at BUILD time -- same
# reasoning as father-time-lebron: free hosting tiers have ephemeral
# filesystems, so the running app only ever reads what was copied in below.
FROM python:3.13-slim

WORKDIR /app

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY data/artifacts/ data/artifacts/
COPY data/replay_fixtures/ data/replay_fixtures/

EXPOSE 8000

# Shell form so $PORT (set by most PaaS hosts, e.g. Render) is honored, with
# 8000 as a sane local-Docker default when it's unset.
CMD uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000}
