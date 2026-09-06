FROM python:3.12-slim

WORKDIR /app

# ffmpeg provides both `ffmpeg` (thumbnail frame extraction) and
# `ffprobe` (duration/width/height). These are what let videos show up
# in Telegram with a real duration badge, thumbnail, and play button -
# exactly like a manual upload - instead of a plain document icon.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir gdown telethon

COPY drive_to_telegram.py log.py ./

# Runtime data is persisted by docker-compose through ./data:/app/data.
# The image itself contains no Telegram session or downloaded files.

ENTRYPOINT ["python3", "drive_to_telegram.py"]
