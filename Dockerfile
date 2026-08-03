FROM python:3.12-slim

# ffmpeg  -> mp3 conversion + streaming
# libopus0 -> discord.py voice encoding
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libopus0 \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

RUN useradd --create-home --uid 1000 botuser \
    && mkdir -p /data \
    && chown -R botuser:botuser /data /app
USER botuser

VOLUME ["/data"]

# tini reaps the ffmpeg processes discord.py spawns per track
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "bot"]
