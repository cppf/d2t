FROM python:3.12-slim

WORKDIR /app

# Only two dependencies, no compiled extensions needed, so a plain
# pip install is fine without extra build tooling.
RUN pip install --no-cache-dir gdown telethon

COPY drive_to_telegram.py .

# Runtime data is persisted by docker-compose through ./data:/app/data.
# The image itself contains no Telegram session or downloaded files.

ENTRYPOINT ["python3", "drive_to_telegram.py"]
