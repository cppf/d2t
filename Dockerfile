FROM python:3.12-slim

WORKDIR /app

# Only two dependencies, no compiled extensions needed, so a plain
# pip install is fine without extra build tooling.
RUN pip install --no-cache-dir gdown telethon

COPY drive_to_telegram.py .

# No files are copied from a host download/session directory and none
# are declared as volumes here on purpose - per your setup, the
# session file and downloaded files live only inside the container
# and are lost when it's removed. If you'd rather they persist across
# rebuilds (recommended - see README), mount a volume in
# docker-compose.yml instead of changing this file.

ENTRYPOINT ["python3", "drive_to_telegram.py"]
