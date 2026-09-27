# Signal Stack — single-image deploy.
#
# The app is two processes (FastAPI + Streamlit) but only ONE port is exposed.
# That is deliberate: a hosted container usually gets a single public port, and
# the product is the UI. So the UI is the public surface on 8501, and the API
# binds 127.0.0.1:8000 inside the container, reachable only by the UI over
# loopback. The API is never exposed to the internet, which also means no CORS
# to configure and no public endpoint to rate-limit or abuse.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Public surface: the Streamlit UI. This is the port the host must route.
    PORT=8501 \
    # The API stays on loopback, inside the container only.
    API_HOST=127.0.0.1 \
    API_PORT=8000 \
    # The UI reaches the API over loopback.
    SIGNAL_STACK_API=http://127.0.0.1:8000 \
    # Streamlit must not try to phone home / prompt for an email on first boot.
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true \
    # Seed the demo dataset on first boot if memory is empty.
    AUTOSEED=1

WORKDIR /app

# curl is used by the container HEALTHCHECK below. ca-certificates is needed for
# TLS to api.hindsight.vectorize.io and api.groq.com.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency metadata first so the pip layer is cached independently of
# source edits — a code change shouldn't reinstall the world.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/
COPY data/seed_signals.json ./data/seed_signals.json
COPY start.sh ./start.sh
RUN chmod +x ./start.sh

# Never bake credentials into the image. They arrive as environment variables
# at run time. Fail the build if someone tries to COPY a .env in anyway.
RUN test ! -f .env || (echo "ERROR: .env must not be in the build context" && exit 1)

# Run unprivileged.
RUN useradd --create-home --uid 10001 signalstack \
    && chown -R signalstack:signalstack /app
USER signalstack

EXPOSE 8501

# Probe the UI, not the private API: 8501 is what the host depends on.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8501/_stcore/health || exit 1

# Handles PID 1 duties itself: starts the API in the background, execs
# Streamlit in the foreground, and reaps the child on shutdown.
CMD ["./start.sh"]
