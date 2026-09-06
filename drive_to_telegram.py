#!/usr/bin/env python3
"""
Drive -> Telegram

- Keeps the Telethon login session in persistent /app/data by default.
- Lists the public Drive folder first, so existing local files are NOT
  downloaded again.
- Checks the destination Telegram chat before uploading, so an already
  uploaded file (same filename + size) is skipped.
- Resolves TELEGRAM_CHAT_ID as an integer/entity, fixing the
  "Cannot find any entity corresponding to '-100...'" error.
"""

import os
import sys
import asyncio
import argparse
import json
from pathlib import Path


def _require_env(name):
    value = os.environ.get(name)
    return value.strip() if value else None


def load_config():
    cfg = {
        "api_id": _require_env("TELEGRAM_API_ID"),
        "api_hash": _require_env("TELEGRAM_API_HASH"),
        "chat_id": _require_env("TELEGRAM_CHAT_ID"),
        "drive_url": _require_env("GDRIVE_FOLDER_URL"),
        "download_dir": Path(os.environ.get("DOWNLOAD_DIR", "/app/data/drive_downloads")),
        "session": Path(os.environ.get(
            "TELEGRAM_SESSION", "/app/data/drive_to_telegram_session"
        )),
        "max_upload_mb": int(os.environ.get("MAX_UPLOAD_MB", "2048")),
        "delete_after_send": os.environ.get("DELETE_AFTER_SEND", "true").strip().lower()
        not in ("false", "0", "no"),
        "check_destination": os.environ.get("CHECK_DESTINATION", "true").strip().lower()
        not in ("false", "0", "no"),
        "batch_size": int(os.environ.get("BATCH_SIZE", "5")),
    }

    if cfg["batch_size"] < 1:
        print("BATCH_SIZE must be at least 1")
        sys.exit(1)

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


def _drive_file_local_path(download_dir, item):
    # gdown's skip_download result has path/local_path. Prefer path because
    # it is the path relative to the Drive folder.
    rel = getattr(item, "path", None) or getattr(item, "local_path", None)
    if not rel:
        raise RuntimeError(f"gdown returned an item without a path: {item!r}")
    return download_dir / Path(rel)


def get_drive_manifest(cfg):
    """Read Drive metadata only; no file bytes are downloaded."""
    import gdown

    if not cfg["drive_url"]:
        print("GDRIVE_FOLDER_URL is not set. Add it to your .env file.")
        sys.exit(1)

    cfg["download_dir"].mkdir(parents=True, exist_ok=True)
    print(f"Reading Drive folder:\n  {cfg['drive_url']}")
    print("Checking local files before downloading...\n")

    try:
        remote_items = gdown.download_folder(
            url=cfg["drive_url"],
            output=str(cfg["download_dir"]),
            quiet=True,
            use_cookies=False,
            skip_download=True,
        )
    except Exception as e:
        print(f"\nCould not read Drive folder: {e}")
        print(
            "\nCommon causes:\n"
            "  - The folder isn't shared as 'Anyone with the link'\n"
            "  - The URL is not a Drive folder URL\n"
            "  - Google is rate-limiting anonymous access"
        )
        sys.exit(1)

    if not remote_items:
        print("No files found in that folder. Nothing to send.")
        sys.exit(0)

    items = []
    for item in remote_items:
        if getattr(item, "type", "") == "folder":
            continue
        local_path = _drive_file_local_path(cfg["download_dir"], item)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        items.append((item, local_path))

    print(f"Drive files found: {len(items)}")
    return items



def download_one(item, local_path):
    """Download one Drive file, reusing/resuming an existing local copy."""
    import gdown

    if local_path.is_file() and local_path.stat().st_size > 0:
        print(f"LOCAL EXISTS - {local_path}")
        return local_path

    print(f"\nDownloading: {local_path}")
    try:
        result = gdown.download(
            id=item.id,
            output=str(local_path),
            quiet=False,
            use_cookies=False,
            resume=True,
        )
        if not result or not local_path.is_file():
            raise RuntimeError("gdown did not produce the expected local file")
        print("Download completed")
        return local_path
    except Exception as e:
        print(f"  FAILED download - {e}")
        return None


def _delete_local_file(path):
    try:
        path.unlink()
        print(f"  Deleted local copy ({path.name}).")
    except OSError as e:
        print(f"  Warning: couldn't delete local file {path.name}: {e}")


async def resolve_chat(client, chat_id):
    """
    Convert '-100123...' to an integer and explicitly resolve the entity.
    Passing the raw string '-100...' to Telethon can cause:
      Cannot find any entity corresponding to "-100..."
    """
    try:
        numeric_id = int(str(chat_id).strip())
    except ValueError:
        # Also allow a username / @username as a convenience.
        return await client.get_entity(str(chat_id).strip())

    try:
        return await client.get_entity(numeric_id)
    except ValueError:
        # If the entity is not in the local entity cache, refresh dialogs.
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
    document size. This avoids uploading the same file twice.

    A filename-only match is NOT enough: the same filename can legitimately
    be used for a different file. We therefore require filename + byte size.
    """
    try:
        async for message in client.iter_messages(entity, search=filename, limit=100):
            document = getattr(getattr(message, "media", None), "document", None)
            if not document:
                continue

            remote_name = None
            for attr in getattr(document, "attributes", []):
                # DocumentAttributeFilename has a .file_name attribute.
                if hasattr(attr, "file_name"):
                    remote_name = attr.file_name
                    break

            if remote_name == filename and getattr(document, "size", None) == size:
                return True
    except Exception as e:
        print(f"  Warning: destination duplicate check failed: {e}")
        print("  Continuing with upload.")
    return False


async def _upload_one(
    client, entity, path, max_upload_bytes, delete_after_send, check_destination, index, total
):
    """Check and upload one file. Designed to run concurrently with other uploads."""
    try:
        size = path.stat().st_size
    except OSError as e:
        print(f"\n[{index}/{total}] {path.name}: FAILED - cannot stat local file: {e}")
        return "failed", path, str(e)

    size_mb = size / (1024 * 1024)
    print(f"\n[{index}/{total}] {path.name} ({size_mb:.1f} MB) - preparing upload")

    if size > max_upload_bytes:
        limit_mb = max_upload_bytes / (1024 * 1024)
        print(f"  SKIPPED - {size_mb:.1f} MB exceeds the {limit_mb:.0f} MB limit.")
        if delete_after_send:
            _delete_local_file(path)
        return "skipped", path, None

    if check_destination:
        print(f"  [{index}/{total}] Checking Telegram destination...")
        if await telegram_destination_has_file(client, entity, path.name, size):
            print(f"  [{index}/{total}] SKIPPED - identical file already exists in destination.")
            if delete_after_send:
                _delete_local_file(path)
            return "duplicate", path, None

    try:
        print(f"  [{index}/{total}] UPLOADING...")
        # Send videos through Telegram's media/video path rather than as
        # generic documents. This allows Telegram to recognize compatible
        # videos (especially MP4) as streamable media.
        is_video = path.suffix.lower() in {
            ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"
        }
        send_kwargs = {
            "caption": path.name,
            "force_document": not is_video,
        }
        if is_video:
            send_kwargs["supports_streaming"] = True
            if path.suffix.lower() == ".mp4":
                send_kwargs["mime_type"] = "video/mp4"

        await client.send_file(
            entity,
            str(path),
            **send_kwargs,
        )
        print(f"  [{index}/{total}] SENT - {path.name}")
        if delete_after_send:
            _delete_local_file(path)
        return "sent", path, None
    except Exception as e:
        print(f"  [{index}/{total}] FAILED - {e}")
        print("  Keeping local copy so you can retry without re-downloading.")
        return "failed", path, str(e)


async def upload_batch(
    client, entity, files, batch_number, max_upload_bytes, delete_after_send, check_destination
):
    """Upload up to five files concurrently."""
    total = len(files)
    print("\n" + "=" * 60)
    print(f"UPLOAD BATCH {batch_number}: {total} file(s) simultaneously")
    print("=" * 60)

    results = await asyncio.gather(*[
        _upload_one(
            client, entity, path, max_upload_bytes, delete_after_send,
            check_destination, i, total
        )
        for i, path in enumerate(files, 1)
    ])
    return results


async def list_chats(client):
    print("\nYour chats (use the ID on the right in TELEGRAM_CHAT_ID):\n")
    async for dialog in client.iter_dialogs():
        kind = "group/channel" if dialog.is_group or dialog.is_channel else "user"
        print(f"  {dialog.id:>15}   [{kind:14}]  {dialog.name}")
    print("\nPut the target numeric ID into TELEGRAM_CHAT_ID.")


async def main_async(args, cfg):
    from telethon import TelegramClient

    # The parent directory is mounted by docker-compose, so this survives
    # `docker compose run --rm` and the account is not re-authenticated.
    cfg["session"].parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(
        str(cfg["session"]),
        cfg["api_id"],
        cfg["api_hash"],
    )

    print("Connecting to Telegram...")
    print("If this is the first run with this persistent data folder, "
          "Telegram will ask for login once. Later runs reuse the session.\n")
    await client.start()

    if args.list_chats:
        await list_chats(client)
        await client.disconnect()
        return

    if not cfg["chat_id"]:
        print(
            "TELEGRAM_CHAT_ID is not set. Run with --list-chats first, "
            "then add the target ID to .env."
        )
        await client.disconnect()
        sys.exit(1)

    entity = await resolve_chat(client, cfg["chat_id"])
    print(f"Telegram destination resolved: {getattr(entity, 'title', None) or getattr(entity, 'username', None) or entity.id}")

    drive_items = get_drive_manifest(cfg)
    max_upload_bytes = cfg["max_upload_mb"] * 1024 * 1024
    batch_size = cfg["batch_size"]

    print(
        f"\nPipeline: download {batch_size} file(s) sequentially, then "
        f"upload {batch_size} simultaneously."
    )
    print(
        f"Destination duplicate check: {'ON' if cfg['check_destination'] else 'OFF'}"
    )

    sent, skipped, failed, duplicates = [], [], [], []
    total_files = len(drive_items)

    for batch_start in range(0, total_files, batch_size):
        batch_items = drive_items[batch_start:batch_start + batch_size]
        batch_files = []

        print("\n" + "#" * 60)
        print(
            f"DOWNLOAD BATCH {batch_start // batch_size + 1}: "
            f"{len(batch_items)} file(s), one after another"
        )
        print("#" * 60)

        # Intentionally sequential: file 2 starts only after file 1 finishes.
        for item, local_path in batch_items:
            downloaded = download_one(item, local_path)
            if downloaded:
                batch_files.append(downloaded)

        if not batch_files:
            print("No files successfully downloaded/available in this batch.")
            continue

        results = await upload_batch(
            client, entity, batch_files, batch_start // batch_size + 1,
            max_upload_bytes, cfg["delete_after_send"], cfg["check_destination"]
        )

        for status, path, error in results:
            if status == "sent":
                sent.append(path)
            elif status == "skipped":
                skipped.append(path)
            elif status == "duplicate":
                duplicates.append(path)
            else:
                failed.append((path, error))

    print("\n" + "=" * 50)
    print("DONE")
    print(f"  Sent:                  {len(sent)}")
    print(f"  Already in destination:{len(duplicates)}")
    print(f"  Skipped (too large):   {len(skipped)}")
    print(f"  Failed:                {len(failed)}")

    if duplicates:
        print("\nDestination duplicates:")
        for p in duplicates:
            print(f"  - {p.name}")

    if skipped:
        print("\nSkipped files:")
        for p in skipped:
            print(f"  - {p.name}")

    if failed:
        print("\nFailed files:")
        for p, err in failed:
            print(f"  - {p.name}: {err}")
        print("\nFailed local files were kept so you can retry without re-downloading.")

    print("=" * 50)
    await client.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="Copy public Google Drive files into a Telegram chat."
    )
    parser.add_argument(
        "--list-chats",
        action="store_true",
        help="Log in and print your chats with their IDs, then exit.",
    )
    args = parser.parse_args()

    cfg, missing = load_config()

    if missing:
        print("Missing required environment variables:")
        for name in missing:
            print(f"  - {name}")
        print("\nSet them in .env.")
        sys.exit(1)

    if not args.list_chats and not cfg["drive_url"]:
        print("GDRIVE_FOLDER_URL is not set. Add it to your .env file.")
        sys.exit(1)

    asyncio.run(main_async(args, cfg))


if __name__ == "__main__":
    main()
