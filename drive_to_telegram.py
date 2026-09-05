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


def _drive_file_local_path(download_dir, item):
    # gdown's skip_download result has path/local_path. Prefer path because
    # it is the path relative to the Drive folder.
    rel = getattr(item, "path", None) or getattr(item, "local_path", None)
    if not rel:
        raise RuntimeError(f"gdown returned an item without a path: {item!r}")
    return download_dir / Path(rel)


def download_drive_folder(cfg):
    """
    First asks gdown for the Drive manifest with skip_download=True.
    This is metadata only: no file bytes are downloaded.

    Then downloads ONLY files that are not already present locally.
    Existing files are reused.
    """
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

    files = []
    to_download = []

    for item in remote_items:
        # Skip folders if gdown ever returns one in the manifest.
        item_path = getattr(item, "path", "")
        item_type = getattr(item, "type", "")
        if item_type == "folder":
            continue

        local_path = _drive_file_local_path(cfg["download_dir"], item)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        if local_path.is_file() and local_path.stat().st_size > 0:
            print(f"LOCAL EXISTS - {local_path}")
            files.append(local_path)
        else:
            to_download.append((item, local_path))

    print(
        f"\nDrive files: {len(files) + len(to_download)} | "
        f"already local: {len(files)} | needs download: {len(to_download)}"
    )

    for item, local_path in to_download:
        print(f"\nDownloading: {local_path}")
        try:
            # Download directly to the exact destination. resume=True also
            # lets gdown continue an interrupted partial download.
            result = gdown.download(
                id=item.id,
                output=str(local_path),
                quiet=False,
                use_cookies=False,
                resume=True,
            )
            if not result or not local_path.is_file():
                raise RuntimeError("gdown did not produce the expected local file")
            files.append(local_path)
        except Exception as e:
            print(f"  FAILED download - {e}")

    files = sorted(set(p for p in files if p.is_file()))

    if not files:
        print("\nNo files are available locally to send.")
        sys.exit(0)

    print(f"\nReady to process {len(files)} file(s).")
    return files


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


async def send_files_to_telegram(
    client, entity, files, max_upload_bytes, delete_after_send, check_destination
):
    sent, skipped, failed, duplicates = [], [], [], []

    for i, path in enumerate(files, 1):
        try:
            size = path.stat().st_size
        except OSError as e:
            print(f"\n[{i}/{len(files)}] {path.name}")
            print(f"  FAILED - cannot stat local file: {e}")
            failed.append((path, str(e)))
            continue

        size_mb = size / (1024 * 1024)
        print(f"\n[{i}/{len(files)}] {path.name} ({size_mb:.1f} MB)")

        if size > max_upload_bytes:
            limit_mb = max_upload_bytes / (1024 * 1024)
            print(
                f"  SKIPPED - {size_mb:.1f} MB exceeds the "
                f"{limit_mb:.0f} MB limit."
            )
            skipped.append(path)
            if delete_after_send:
                _delete_local_file(path)
            continue

        if check_destination:
            print("  Checking destination for duplicate...")
            if await telegram_destination_has_file(client, entity, path.name, size):
                print("  SKIPPED - identical file already exists in destination.")
                duplicates.append(path)
                if delete_after_send:
                    _delete_local_file(path)
                continue

        try:
            def progress(current, total):
                pct = current / total * 100 if total else 0
                print(f"  Uploading... {pct:.0f}%", end="\r")

            await client.send_file(
                entity,
                str(path),
                caption=path.name,
                progress_callback=progress,
            )
            print(f"  Sent.{' ' * 20}")
            sent.append(path)
            if delete_after_send:
                _delete_local_file(path)
        except Exception as e:
            print(f"  FAILED - {e}")
            print("  Keeping local copy so you can retry without re-downloading.")
            failed.append((path, str(e)))

    return sent, skipped, failed, duplicates


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

    files = download_drive_folder(cfg)
    max_upload_bytes = cfg["max_upload_mb"] * 1024 * 1024

    print(
        f"\nSending {len(files)} file(s)... "
        f"(destination duplicate check: {'ON' if cfg['check_destination'] else 'OFF'})"
    )

    sent, skipped, failed, duplicates = await send_files_to_telegram(
        client,
        entity,
        files,
        max_upload_bytes,
        cfg["delete_after_send"],
        cfg["check_destination"],
    )

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

    import asyncio
    asyncio.run(main_async(args, cfg))


if __name__ == "__main__":
    main()
