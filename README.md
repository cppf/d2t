# Drive → Telegram

This version is designed for repeated runs without re-downloading or
re-uploading the same files.

## What changed

### 1. Telegram login is persistent

The Telegram session is stored in:

```text
/app/data/drive_to_telegram_session.session
```

and `/app/data` is mounted to `./data` on the host.

So:

```bash
docker compose run --rm app
```

does **not** require a phone number/login code every time.

You only authenticate once. Keep the `data/` directory private because
the `.session` file is effectively an authentication credential.

### 2. Existing local files are not downloaded again

Before downloading anything, the program asks gdown for the Drive
folder's metadata with `skip_download=True`. It then checks the exact
local destination path.

If the file already exists locally, it prints:

```text
LOCAL EXISTS - /app/data/drive_downloads/filename.mp4
```

and reuses that file.

Only missing files are downloaded. Interrupted downloads use gdown's
resume support.

### 3. Existing Telegram destination files are skipped

Before uploading a local file, the program searches the destination
chat for the filename.

It skips the upload only when both:

- Telegram's document filename exactly matches the local filename
- Telegram's document byte size exactly matches the local file size

This avoids treating two different files with the same filename as
duplicates.

### 4. Fixed the `-100...` entity error

The old code passed the value from `.env` directly as a string:

```text
"-1003947101828"
```

Telethon can fail to resolve that string as an entity.

The new code converts numeric chat IDs to an integer and explicitly
resolves the entity before sending. If necessary, it refreshes the
account's dialogs.

So a normal channel/group ID such as:

```text
-1003947101828
```

can be used directly in `.env`.

## First run

From the project directory:

```bash
mkdir -p data
cp .env.example .env
```

Fill in `.env`, then build:

```bash
docker compose build
```

Find your chats:

```bash
docker compose run --rm app --list-chats
```

On this first run, log in once if Telegram asks.

Then put the destination ID in `.env`, for example:

```text
TELEGRAM_CHAT_ID=-1003947101828
```

and run:

```bash
docker compose run --rm app
```

After the first successful login, the session remains in:

```text
./data/drive_to_telegram_session.session
```

and future runs reuse it.

## Persistent downloaded files

Downloaded files are stored under:

```text
./data/drive_downloads/
```

A successful upload (or destination duplicate) is deleted when:

```text
DELETE_AFTER_SEND=true
```

Failed uploads are kept locally so the next run can retry them without
downloading from Drive again.

If a file is already present in `drive_downloads`, the next run does
not download it again.

## Duplicate behavior

Suppose Drive contains:

```text
Lecture 01.mp4
```

and the destination already contains a Telegram document with:

```text
filename = Lecture 01.mp4
size     = 474123456 bytes
```

The program reports:

```text
SKIPPED - identical file already exists in destination.
```

It will not upload another copy.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_API_ID` | — | Telegram API ID |
| `TELEGRAM_API_HASH` | — | Telegram API hash |
| `TELEGRAM_CHAT_ID` | — | Destination numeric ID |
| `GDRIVE_FOLDER_URL` | — | Public Google Drive folder |
| `DOWNLOAD_DIR` | `/app/data/drive_downloads` | Persistent download directory |
| `TELEGRAM_SESSION` | `/app/data/drive_to_telegram_session` | Persistent Telethon session |
| `MAX_UPLOAD_MB` | `2048` | Maximum file size |
| `DELETE_AFTER_SEND` | `true` | Delete successful/duplicate local files |
| `CHECK_DESTINATION` | `true` | Enable Telegram duplicate checking |

## Important

Do not delete the `data/drive_to_telegram_session.session` file unless
you intentionally want to authenticate the Telegram account again.

Do not commit `data/` or `.env` to GitHub.

## 5-file batch pipeline

The app processes files in batches. By default it:

1. Downloads 5 files **one after another**.
2. Uploads those 5 files to Telegram **concurrently**.
3. Waits until that upload batch finishes.
4. Downloads the next 5 sequentially.
5. Repeats until all files are processed.

Set `BATCH_SIZE` in `.env` to change the batch size (default `5`).

Note: concurrent Telegram uploads can be subject to Telegram/server bandwidth and rate limits. If you encounter rate-limit errors, reduce `BATCH_SIZE` (for example to `2` or `3`).
