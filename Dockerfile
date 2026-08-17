# Projekt Blaulicht — single-container image (web panel + orchestrator).
FROM python:3.12-slim

# Non-root runtime user.
RUN adduser --system --no-create-home --group bot

WORKDIR /app

# Install dependencies first for layer caching.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir ".[web]"

# Salvaged assets + reference data (mounted over by the volume in compose, but
# baked in so the image runs standalone too).
COPY data ./data

USER bot
EXPOSE 8080

# Data (sqlite + assets) lives under /data via the compose volume.
ENV LOG_LEVEL=info TZ=Europe/Berlin
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
