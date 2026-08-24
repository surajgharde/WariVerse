FROM python:3.11-slim

# The vision stack is ~2 GB and the default `CROWD_SOURCE=sim` needs none of it.
# Build the slim image for development and demos:
#     docker compose build ai-engine
# Build the full one when there is a camera to point at:
#     docker compose build --build-arg WITH_VISION=1 ai-engine
ARG WITH_VISION=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# libgl1 and libglib2.0-0 are OpenCV's runtime deps. Installed unconditionally
# and cheaply (~40 MB) so the two image variants share this layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-vision.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$WITH_VISION" = "1" ]; then pip install --no-cache-dir -r requirements-vision.txt; fi

COPY . .

EXPOSE 8100

# The engine is healthy when it can answer, not when it is measuring — a camera
# outage is reported through /health/deep and the core API's alert feed, not by
# killing this container. Restarting it would not bring the camera back.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8100/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
