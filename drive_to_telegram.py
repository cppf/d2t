#!/usr/bin/env python3
"""
Drive -> Telegram

One file at a time: download, extract real video metadata + a thumbnail,
upload as a native Telegram video, delete the local copy, then move to
the next file.

Why this version exists (see README for the full story):

  - Videos now look exactly like a manual upload: Telegram shows the
    correct duration badge, a real thumbnail (first frame), and the
    native play button instead of a plain document/download icon.
    This requires sending real duration/width/height metadata and a
    JPEG thumbnail alongside the video, which the previous version
    never generated.

  - Google Drive downloads now back off and retry instead of hammering
    Drive back-to-back. Downloading many large files with no delay is
    what was tripping Drive's per-IP throttle after a handful of
    videos. There is also a small pause between files.

  - Processing is strictly sequential per file (download -> upload ->
    delete -> next), which is both what was asked for and, as a side
    effect, much gentler on Drive than the old concurrent-batch design.

  - Resolves TELEGRAM_CHAT_ID as an integer/entity, fixing the
    "Cannot find any entity corresponding to '-100...'" error.
"""

import os
import sys
import time
import shutil
import asyncio
import argparse
import subprocess
import json
from pathlib import Path

from log import Log, Timer, format_bytes, format_duration, format_eta

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".m4a", ".3gp"}

# Google Drive's anonymous-download throttle tends to show up as one of
# these signatures somewhere in the exception text. Matched case-insensitively.
DRIVE_RATE_LIMIT_SIGNATURES = (
    "too many users",
    "quota exceeded",
    "quota has been exceeded",
    "cannot retrieve the public link",
    "download quota",
    "429",
    "rate limit",
    "access denied",
)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _require_env(name):
    value = os.environ.get(name)
    return value.strip() if value else None


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("false", "0", "no")


def load_config():
    cfg = {
        "api_id": _require_env("TELEGRAM_API_ID"),
        "api_hash": _require_env("TELEGRAM_API_HASH"),
        "chat_id": _require_env("TELEGRAM_CHAT_ID"),
        "drive_url": _require_env("GDRIVE_FOLDER_URL"),
        "download_dir": Path(os.environ.get("DOWNLOAD_DIR", "/app/data/drive_downloads")),
        "thumb_dir": Path(os.environ.get("THUMB_DIR", "/app/data/thumbnails")),
        "session": Path(os.environ.get(
            "TELEGRAM_SESSION", "/app/data/drive_to_telegram_session"
        )),
        "max_upload_mb": int(os.environ.get("MAX_UPLOAD_MB", "2048")),
        "delete_after_send": _env_bool("DELETE_AFTER_SEND", True),
        "check_destination": _env_bool("CHECK_DESTINATION", True),
        "download_retries": int(os.environ.get("DOWNLOAD_RETRIES", "5")),
        "download_retry_base_seconds": float(os.environ.get("DOWNLOAD_RETRY_BASE_SECONDS", "20")),
        "pause_between_files_seconds": float(os.environ.get("PAUSE_BETWEEN_FILES_SECONDS", "8")),
        "thumbnail_offset_seconds": float(os.environ.get("THUMBNAIL_OFFSET_SECONDS", "1")),
        "no_color": _env_bool("NO_COLOR", False),
    }

    if cfg["api_id"] is not None:
        try:
            cfg["api_id"] = int(cfg["api_id"])
        except ValueError:
            print(f"TELEGRAM_API_ID must be a number, got: {cfg['api_id']!r}")
            sys.exit(1)

    missing = []
    if not cfg["api_id"]:
        missing.append("TELEGRAM_API_ID")
    if not cfg["api_hash"]:
        missing.append("TELEGRAM_API_HASH")
    return cfg, missing


# --------------------------------------------------------------------------
# Drive manifest + single-file download with backoff
# --------------------------------------------------------------------------

def _drive_file_local_path(download_dir, item):
    rel = getattr(item, "path", None) or getattr(item, "local_path", None)
    if not rel:
        raise RuntimeError(f"gdown returned an item without a path: {item!r}")
    return download_dir / Path(rel)


def get_drive_manifest(cfg, log):
    """Read Drive metadata only; no file bytes are downloaded here."""
    import gdown

    if not cfg["drive_url"]:
        log.error("GDRIVE_FOLDER_URL is not set. Add it to your .env file.")
        sys.exit(1)

    cfg["download_dir"].mkdir(parents=True, exist_ok=True)
    cfg["thumb_dir"].mkdir(parents=True, exist_ok=True)

    log.section("Reading Google Drive folder")
    log.line(cfg["drive_url"])

    try:
        remote_items = gdown.download_folder(
            url=cfg["drive_url"],
            output=str(cfg["download_dir"]),
            quiet=True,
            use_cookies=False,
            skip_download=True,
        )
    except Exception as e:
        log.error(f"Could not read Drive folder: {e}")
        log.line(
            "Common causes:\n"
            "    - The folder isn't shared as 'Anyone with the link'\n"
            "    - The URL is not a Drive folder URL\n"
            "    - Google is rate-limiting anonymous access right now"
        )
        sys.exit(1)

    if not remote_items:
        log.warn("No files found in that folder. Nothing to send.")
        sys.exit(0)

    items = []
    for item in remote_items:
        if getattr(item, "type", "") == "folder":
            continue
        local_path = _drive_file_local_path(cfg["download_dir"], item)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        items.append((item, local_path))

    log.success(f"Found {len(items)} file(s) in the Drive folder")
    return items


def _is_drive_rate_limit(exc):
    text = str(exc).lower()
    return any(sig in text for sig in DRIVE_RATE_LIMIT_SIGNATURES)


def download_one_with_retry(item, local_path, cfg, log):
    """
    Download one Drive file, retrying with exponential backoff on Drive's
    anonymous-download throttle instead of failing the whole run.

    Reuses an existing local copy if present, so a prior interrupted run
    (or an interrupted download resumed by gdown) is not re-fetched.
    """
    if local_path.is_file() and local_path.stat().st_size > 0:
        log.step("Download", f"already on disk, skipping fetch ({format_bytes(local_path.stat().st_size)})")
        return local_path

    import gdown

    attempts = max(1, cfg["download_retries"])
    for attempt in range(1, attempts + 1):
        timer = Timer()
        progress = log.progress_bar("Download")
        try:
            def _hook(current, total, filename=None):
                if total:
                    progress.update(current, total)

            result = gdown.download(
                id=item.id,
                output=str(local_path),
                quiet=True,
                use_cookies=False,
                resume=True,
            )
            progress.finish()

            if not result or not local_path.is_file() or local_path.stat().st_size == 0:
                raise RuntimeError("gdown did not produce a usable local file")

            log.step("Download", f"done in {format_duration(timer.elapsed())} "
                                   f"({format_bytes(local_path.stat().st_size)})", ok=True)
            return local_path

        except Exception as e:
            progress.finish(failed=True)
            rate_limited = _is_drive_rate_limit(e)
            partial = local_path.with_suffix(local_path.suffix + ".part")
            if partial.exists():
                partial.unlink(missing_ok=True)

            if attempt >= attempts:
                reason = "Drive rate limit" if rate_limited else "download error"
                log.step("Download", f"failed permanently after {attempts} attempt(s) [{reason}]: {e}", ok=False)
                return None

            wait = cfg["download_retry_base_seconds"] * (2 ** (attempt - 1))
            wait = min(wait, 600)  # cap a single backoff at 10 minutes
            reason = "Google Drive is rate-limiting anonymous downloads" if rate_limited else "download failed"
            log.step("Download", f"attempt {attempt}/{attempts} failed ({reason}). "
                                  f"Waiting {format_duration(wait)} before retrying...", ok=False)
            time.sleep(wait)

    return None


def _delete_local_file(path, log):
    try:
        path.unlink()
        log.step("Cleanup", f"deleted local copy of {path.name}", ok=True)
    except OSError as e:
        log.step("Cleanup", f"could not delete {path.name}: {e}", ok=False)


# --------------------------------------------------------------------------
# Video metadata + thumbnail (this is what makes uploads look "manual")
# --------------------------------------------------------------------------

def is_video_file(path):
    return path.suffix.lower() in VIDEO_EXTENSIONS


def probe_video(path, log):
    """
    Run ffprobe to get exact duration/width/height, the same fields
    Telegram itself reads when you upload a video by hand. Returns
    (duration_seconds, width, height) or None on failure.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-show_entries", "format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            log.step("Metadata", f"ffprobe failed: {proc.stderr.strip()[:200]}", ok=False)
            return None

        data = json.loads(proc.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})

        if not streams:
            log.step("Metadata", "ffprobe found no video stream", ok=False)
            return None

        width = int(streams[0].get("width", 0) or 0)
        height = int(streams[0].get("height", 0) or 0)
        duration = float(fmt.get("duration", 0) or 0)

        if not (width and height and duration):
            log.step("Metadata", "ffprobe returned incomplete data (missing width/height/duration)", ok=False)
            return None

        log.step("Metadata", f"{width}x{height}, {format_duration(duration)}", ok=True)
        return duration, width, height

    except FileNotFoundError:
        log.step("Metadata", "ffprobe is not installed in this container", ok=False)
        return None
    except subprocess.TimeoutExpired:
        log.step("Metadata", "ffprobe timed out", ok=False)
        return None
    except Exception as e:
        log.step("Metadata", f"ffprobe error: {e}", ok=False)
        return None


def generate_thumbnail(path, thumb_dir, duration, offset_seconds, log):
    """
    Extract a single JPEG frame to use as the Telegram thumbnail, the
    same way Telegram's own client grabs an early frame when you upload
    a video manually. Returns the thumbnail Path, or None on failure
    (upload can still proceed without one).
    """
    thumb_path = thumb_dir / f"{path.stem}.jpg"

    # Pick a safe timestamp: configured offset, but never past the clip
    # and never frame 0 (often black on many encoders).
    if duration and duration > 0:
        seek = min(offset_seconds, max(duration * 0.1, 0.1))
    else:
        seek = offset_seconds

    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{seek:.2f}",
                "-i", str(path),
                "-frames:v", "1",
                "-vf", "scale='min(320,iw)':'-2'",
                "-q:v", "4",
                str(thumb_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0 or not thumb_path.is_file() or thumb_path.stat().st_size == 0:
            log.step("Thumbnail", f"ffmpeg failed: {proc.stderr.strip()[-200:]}", ok=False)
            return None

        log.step("Thumbnail", f"extracted frame at {seek:.1f}s ({format_bytes(thumb_path.stat().st_size)})", ok=True)
        return thumb_path

    except FileNotFoundError:
        log.step("Thumbnail", "ffmpeg is not installed in this container", ok=False)
        return None
    except subprocess.TimeoutExpired:
        log.step("Thumbnail", "ffmpeg timed out", ok=False)
        return None
    except Exception as e:
        log.step("Thumbnail", f"ffmpeg error: {e}", ok=False)
        return None


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

async def resolve_chat(client, chat_id):
    """
    Convert '-100123...' to an integer and explicitly resolve the entity.
    Passing the raw string '-100...' to Telethon can cause:
      Cannot find any entity corresponding to "-100..."
    """
    try:
        numeric_id = int(str(chat_id).strip())
    except ValueError:
        return await client.get_entity(str(chat_id).strip())

    try:
        return await client.get_entity(numeric_id)
    except ValueError:
        async for dialog in client.iter_dialogs():
            if dialog.id == numeric_id:
                return dialog.entity
        raise RuntimeError(
            f"Telegram entity {numeric_id} could not be resolved. "
            "Run --list-chats and make sure the account is a member of the "
            "target chat/channel."
        )


async def telegram_destination_has_file(client, entity, filename, size):
    """
    Search the destination chat for the exact filename and verify the
    document size, so an already-uploaded file is not sent twice. A
    filename-only match is not enough since two different files can
    share a name.
    """
    try:
        async for message in client.iter_messages(entity, search=filename, limit=100):
            document = getattr(getattr(message, "media", None), "document", None)
            if not document:
                continue

            remote_name = None
            for attr in getattr(document, "attributes", []):
                if hasattr(attr, "file_name"):
                    remote_name = attr.file_name
                    break

            if remote_name == filename and getattr(document, "size", None) == size:
                return True
    except Exception:
        return False
    return False


async def upload_one(client, entity, path, cfg, log):
    """
    Upload a single file. Videos are sent with real DocumentAttributeVideo
    metadata (duration/width/height) plus a generated thumbnail, so
    Telegram renders the native player card - duration badge, play
    button, thumbnail - exactly like a manual upload, instead of a plain
    document with a download icon.
    """
    from telethon.tl.types import DocumentAttributeVideo

    size = path.stat().st_size
    max_upload_bytes = cfg["max_upload_mb"] * 1024 * 1024

    if size > max_upload_bytes:
        limit_mb = cfg["max_upload_mb"]
        log.step("Upload", f"skipped - {format_bytes(size)} exceeds the {limit_mb} MB limit", ok=False)
        return "skipped"

    if cfg["check_destination"]:
        if await telegram_destination_has_file(client, entity, path.name, size):
            log.step("Upload", "skipped - identical file already exists in the destination chat", ok=True)
            return "duplicate"

    video_meta = None
    thumb_path = None
    is_video = is_video_file(path)

    if is_video:
        probed = probe_video(path, log)
        if probed:
            duration, width, height = probed
            video_meta = DocumentAttributeVideo(
                duration=int(round(duration)),
                w=width,
                h=height,
                supports_streaming=True,
            )
            thumb_path = generate_thumbnail(path, cfg["thumb_dir"], duration, cfg["thumbnail_offset_seconds"], log)
        else:
            log.warn("Could not read video metadata - upload will still proceed, "
                     "but Telegram may not show duration/thumbnail correctly.")

    progress = log.progress_bar("Upload")

    def _hook(sent, total):
        if total:
            progress.update(sent, total)

    send_kwargs = {
        "caption": path.name,
        "force_document": not is_video,
        "progress_callback": _hook,
    }
    if is_video:
        send_kwargs["supports_streaming"] = True
        if video_meta is not None:
            send_kwargs["attributes"] = [video_meta]
        if thumb_path is not None:
            send_kwargs["thumb"] = str(thumb_path)
        if path.suffix.lower() == ".mp4":
            send_kwargs["mime_type"] = "video/mp4"

    try:
        await client.send_file(entity, str(path), **send_kwargs)
        progress.finish()
        log.step("Upload", f"sent ({format_bytes(size)})", ok=True)
        return "sent"
    except Exception as e:
        progress.finish(failed=True)
        log.step("Upload", f"failed - {e}", ok=False)
        return "failed"
    finally:
        if thumb_path and thumb_path.exists():
            thumb_path.unlink(missing_ok=True)


async def list_chats(client, log):
    log.section("Your chats")
    log.line("(use the ID on the right in TELEGRAM_CHAT_ID)")
    async for dialog in client.iter_dialogs():
        kind = "group/channel" if dialog.is_group or dialog.is_channel else "user"
        log.line(f"  {dialog.id:>15}   [{kind:14}]  {dialog.name}")


# --------------------------------------------------------------------------
# Main pipeline: strictly one file at a time
# --------------------------------------------------------------------------

async def main_async(args, cfg, log):
    from telethon import TelegramClient

    cfg["session"].parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(cfg["session"]), cfg["api_id"], cfg["api_hash"])

    log.section("Connecting to Telegram")
    log.line("First run with this data folder: Telegram will ask for login once.")
    log.line("Later runs reuse the saved session automatically.")
    await client.start()
    log.success("Connected")

    if args.list_chats:
        await list_chats(client, log)
        await client.disconnect()
        return

    if not cfg["chat_id"]:
        log.error("TELEGRAM_CHAT_ID is not set. Run with --list-chats first, then add the target ID to .env.")
        await client.disconnect()
        sys.exit(1)

    entity = await resolve_chat(client, cfg["chat_id"])
    dest_name = getattr(entity, "title", None) or getattr(entity, "username", None) or entity.id
    log.success(f"Destination resolved: {dest_name}")

    drive_items = get_drive_manifest(cfg, log)
    total_files = len(drive_items)

    log.section(f"Starting pipeline - {total_files} file(s), one at a time")
    log.line("For each file: download -> upload -> delete -> next file.")
    log.line(f"Destination duplicate check: {'ON' if cfg['check_destination'] else 'OFF'}")

    sent, skipped, failed, duplicates = [], [], [], []
    run_timer = Timer()

    for index, (item, target_path) in enumerate(drive_items, 1):
        display_name = target_path.name
        eta = format_eta(run_timer.elapsed(), index - 1, total_files)
        log.file_header(index, total_files, display_name, eta)

        local_path = download_one_with_retry(item, target_path, cfg, log)
        if not local_path:
            failed.append((display_name, "download failed"))
            continue

        status = await upload_one(client, entity, local_path, cfg, log)

        if status == "sent":
            sent.append(local_path.name)
        elif status == "skipped":
            skipped.append(local_path.name)
        elif status == "duplicate":
            duplicates.append(local_path.name)
        else:
            failed.append((local_path.name, "upload failed"))

        # Delete local copy after any terminal outcome except a genuine
        # failure, so a failed upload can be retried without re-downloading.
        if cfg["delete_after_send"] and status in ("sent", "duplicate", "skipped"):
            _delete_local_file(local_path, log)
        elif status == "failed":
            log.step("Cleanup", "keeping local copy so this file can be retried without re-downloading", ok=True)

        if index < total_files and cfg["pause_between_files_seconds"] > 0:
            time.sleep(cfg["pause_between_files_seconds"])

    log.summary(
        total=total_files,
        sent=sent,
        duplicates=duplicates,
        skipped=skipped,
        failed=failed,
        elapsed=run_timer.elapsed(),
    )

    await client.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="Copy public Google Drive files into a Telegram chat, one file at a time."
    )
    parser.add_argument(
        "--list-chats",
        action="store_true",
        help="Log in and print your chats with their IDs, then exit.",
    )
    args = parser.parse_args()

    cfg, missing = load_config()
    log = Log(no_color=cfg["no_color"])

    if missing:
        log.error("Missing required environment variables:")
        for name in missing:
            log.line(f"  - {name}")
        log.line("Set them in .env.")
        sys.exit(1)

    if not args.list_chats and not cfg["drive_url"]:
        log.error("GDRIVE_FOLDER_URL is not set. Add it to your .env file.")
        sys.exit(1)

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        log.warn(
            "ffmpeg/ffprobe not found on PATH. Videos will still upload, but "
            "without duration/thumbnail metadata, so they will look like a "
            "plain document instead of a native Telegram video."
        )

    try:
        asyncio.run(main_async(args, cfg, log))
    except KeyboardInterrupt:
        log.warn("Interrupted by user. Local files already downloaded were kept.")
        sys.exit(130)


if __name__ == "__main__":
    main()
