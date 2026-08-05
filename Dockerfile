# Talent-Pilot — dashboard + API in one image.
#
# Both processes share a filesystem, which is what lets the Streamlit sidebar
# and the extension API see the same workspaces. Mount a volume at /data to
# keep accounts and job records across restarts.

FROM python:3.12-slim

# Faster, quieter, and no stale .pyc files in the layer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits do not invalidate the install layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir supervisor

COPY . .

# Run as an unprivileged user; give it ownership of the data volume mount.
RUN useradd --create-home --shell /bin/bash talentpilot \
    && mkdir -p /data /app/logs \
    && chown -R talentpilot:talentpilot /app /data

ENV DATA_DIR=/data \
    LOG_DIR=/app/logs \
    API_PORT=8000

USER talentpilot

EXPOSE 8501 8000

# Checks the API specifically — Streamlit can be up while uvicorn has died.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if 'talent-pilot-api' in urllib.request.urlopen('http://localhost:8000/health', timeout=4).read().decode() else 1)"

CMD ["supervisord", "-c", "/app/docker/supervisord.conf"]
