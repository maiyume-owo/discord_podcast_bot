FROM python:3.12-slim

# ffmpeg   -> mp3 conversion + streaming
# libopus0 -> discord.py voice encoding
# deno     -> JS runtime yt-dlp needs to solve YouTube's "n" challenge;
#             without it YouTube serves no audio formats at all
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libopus0 \
        ca-certificates \
        tini \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/deno.zip \
        "https://github.com/denoland/deno/releases/latest/download/deno-$(uname -m)-unknown-linux-gnu.zip" \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && chmod 755 /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && deno --version

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
