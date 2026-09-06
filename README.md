# Drive → Telegram

Copies files from a public Google Drive folder into a Telegram chat,
**one file at a time**: download → upload → delete the local copy →
next file. Designed for repeated runs without re-downloading or
re-uploading the same files, and for videos to look exactly like a
manual upload in Telegram.

## What this version fixes

### 1. Videos now look like a real Telegram video, not a document

Telegram only shows the native player card - duration badge, real
thumbnail, play button - when the upload includes actual video
metadata (duration/width/height) and a thumbnail image. Previously
none of that was generated, so videos showed up as a generic file with
a download icon.

This version now:

- Runs `ffprobe` on every video to read its **exact duration, width,
  and height**.
- Runs `ffmpeg` to extract a **real JPEG thumbnail** from an early
  frame of the video (by default at 1 second in - frame 0 is often
  black on many encoders, which is why Telegram's own apps don't use
  it either).
- Sends the video with that metadata and thumbnail attached, and
  `supports_streaming` enabled.

The result is the same duration badge, thumbnail, and play button you
get from uploading the file by hand in the Telegram app.

If `ffprobe`/`ffmpeg` can't read a particular file (corrupted or
unusual codec), that one file is logged clearly and still uploaded -
just without the extra metadata - rather than stopping the whole run.

### 2. Google Drive no longer rate-limits/blocks the run after a few videos

The previous version downloaded files back-to-back with no delay and
no retry logic, which is exactly what trips Google Drive's anonymous
per-IP throttle after a handful of large files - after which every
later download failed.

This version now:

- **Retries a failed download with exponential backoff** (waits
  longer after each failed attempt, `DOWNLOAD_RETRIES` times, default
  5 attempts: ~20s, 40s, 80s, 160s, 320s).
- **Detects Drive's specific rate-limit responses** and says so
  clearly in the log, instead of a generic error.
- **Pauses between files** (`PAUSE_BETWEEN_FILES_SECONDS`, default 8s)
  so requests to Drive stay spaced out instead of hammering it.
- Processes **one file fully at a time**, which is both what was
  asked for and, as a side effect, much gentler on Drive than
  downloading/uploading many files concurrently.

If you still see Drive errors, the first thing to try is raising
`PAUSE_BETWEEN_FILES_SECONDS` (e.g. to 20-30) in `.env`.

### 3. A genuinely readable process log

Every file gets its own clearly separated block in the log, with a
status line per stage (download, metadata, thumbnail, upload,
cleanup), a live progress bar for download/upload percentage, and an
ETA. The run ends with a clean summary: how many were sent, already in
the destination, skipped, or failed - with failed files listed by
name.

### 4. Telegram login is persistent

The Telegram session is stored in:

```text
/app/data/drive_to_telegram_session.session
```

and `/app/data` is mounted to `./data` on the host, so:

```bash
docker compose run --rm app
```

does **not** require a phone number/login code every time. You only
authenticate once. Keep the `data/` directory private - the
`.session` file is effectively an authentication credential.

### 5. Existing local and destination files are not re-downloaded/re-uploaded

Before downloading anything, the program reads the Drive folder's
metadata only (no bytes) and checks the exact local destination path.
If a file already exists locally, it's reused as-is.

Before uploading, the program searches the destination Telegram chat
for a document with the same filename **and** the same byte size, and
skips the upload if both match. A filename-only match is not enough,
since two different files can share a name.

### 6. Fixed the `-100...` entity error

Numeric chat IDs from `.env` are converted to an integer and the
entity is explicitly resolved (refreshing dialogs if needed) before
sending, fixing:

```text
Cannot find any entity corresponding to "-100..."
```

## First run

From the project directory:

```bash
mkdir -p data
cp .env.example .env
```

Fill in `.env` (see the comments in that file), then build:

```bash
docker compose build
```

Find your chats:

```bash
docker compose run --rm app --list-chats
```

On this first run, log in once if Telegram asks. Then put the
destination ID in `.env`, for example:

```text
TELEGRAM_CHAT_ID=-1003947101828
```

and run:

```bash
docker compose run --rm app
```

After the first successful login, the session remains in
`./data/drive_to_telegram_session.session` and future runs reuse it.

## How a run behaves

For every file in the Drive folder, in order:

1. **Download** - skipped if already on disk; otherwise downloaded
   with automatic retry/backoff on failure.
2. **Metadata** - `ffprobe` reads duration/width/height (videos only).
3. **Thumbnail** - `ffmpeg` extracts a frame as a JPEG (videos only).
4. **Upload** - skipped if an identical file (name + size) is already
   in the destination chat; otherwise sent as a native video (with the
   metadata/thumbnail above) or as a document for non-video files.
5. **Cleanup** - the local copy is deleted after a successful upload
   or a confirmed duplicate. **Failed files are always kept locally**
   so the next run can retry them without downloading from Drive
   again.
6. A short pause, then the next file.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_API_ID` | — | Telegram API ID |
| `TELEGRAM_API_HASH` | — | Telegram API hash |
| `TELEGRAM_CHAT_ID` | — | Destination numeric ID |
| `GDRIVE_FOLDER_URL` | — | Public Google Drive folder |
| `DOWNLOAD_DIR` | `/app/data/drive_downloads` | Persistent download directory |
| `THUMB_DIR` | `/app/data/thumbnails` | Where generated thumbnails are written (auto-deleted after each upload) |
| `TELEGRAM_SESSION` | `/app/data/drive_to_telegram_session` | Persistent Telethon session |
| `MAX_UPLOAD_MB` | `2048` | Maximum file size (raise to `4096` if you have Telegram Premium) |
| `DELETE_AFTER_SEND` | `true` | Delete local files after a successful upload/duplicate |
| `CHECK_DESTINATION` | `true` | Enable Telegram duplicate checking |
| `DOWNLOAD_RETRIES` | `5` | Max attempts per file before giving up on it |
| `DOWNLOAD_RETRY_BASE_SECONDS` | `20` | First retry wait; doubles each attempt after that |
| `PAUSE_BETWEEN_FILES_SECONDS` | `8` | Pause between finishing one file and starting the next - raise this if Drive is still rate-limiting you |
| `THUMBNAIL_OFFSET_SECONDS` | `1` | Where in the video to grab the thumbnail frame from |
| `NO_COLOR` | `false` | Disable ANSI colors in the log |

## Duplicate behavior

Suppose Drive contains `Lecture 01.mp4` and the destination chat
already has a document with filename `Lecture 01.mp4` and the exact
same byte size. The program reports:

```text
skipped - identical file already exists in the destination chat
```

and does not upload another copy.

## Important

- Do not delete `data/drive_to_telegram_session.session` unless you
  intentionally want to re-authenticate.
- Do not commit `data/` or `.env` to GitHub.
- Failed downloads/uploads keep their local file on disk on purpose -
  re-running the command will retry them without hitting Drive again
  for files it already has.
