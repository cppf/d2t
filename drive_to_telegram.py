#!/usr/bin/env python3
"""
drive_to_telegram.py

Downloads every file from a PUBLIC Google Drive folder, then sends each
file to a Telegram chat using your own Telegram account (via Telethon).

Why a user account and not a bot: Telegram BOT accounts can only send
files up to 50 MB. Regular user accounts can send up to 2 GB (4 GB with
Telegram Premium). Since your files are 1.5-2 GB, a bot cannot do this -
this script logs in as you instead.

-----------------------------------------------------------------------
CONFIG
-----------------------------------------------------------------------
All config comes from environment variables (see .env.example), so this
script runs the same way locally or inside Docker. Required:

    TELEGRAM_API_ID       from https://my.telegram.org
    TELEGRAM_API_HASH     from https://my.telegram.org
    GDRIVE_FOLDER_URL     the public Drive folder to copy from

Optional:

    TELEGRAM_CHAT_ID      required for a normal run; NOT required for
                           --list-chats, since that's how you find it
    MAX_UPLOAD_MB         defaults to 2048 (2 GB). Set to 4096 if your
                           account has Telegram Premium.
    DOWNLOAD_DIR          defaults to ./drive_downloads

See README.md for full setup steps (getting API credentials, sharing
the Drive folder publicly, finding your chat ID).

-----------------------------------------------------------------------
SECURITY NOTE
-----------------------------------------------------------------------
On first login this script creates a file named
"drive_to_telegram_session.session". That file lets anyone who has it
access your Telegram account without your password or 2FA. Keep it
private - never upload it, commit it to git, or share it. Delete it if
you want to revoke this script's access (you'll just need to log in
again next run).
"""

import os
import sys
import argparse
from pathlib import Path


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        return None
    return value.strip()


def load_config():
    """Reads config from environment variables. Returns a dict, and a
    list of any required variables that were missing - the caller
    decides how strict to be, since --list-chats needs less than a
    full run does."""
    cfg = {
        "api_id": _require_env("TELEGRAM_API_ID"),
        "api_hash": _require_env("TELEGRAM_API_HASH"),
        "chat_id": _require_env("TELEGRAM_CHAT_ID"),
        "drive_url": _require_env("GDRIVE_FOLDER_URL"),
        "download_dir": Path(os.environ.get("DOWNLOAD_DIR", "./drive_downloads")),
        "max_upload_mb": int(os.environ.get("MAX_UPLOAD_MB", "2048")),
        # On by default: without a host volume mount, disk space inside
        # the container is the main constraint (see README), so each
        # file is deleted right after it sends rather than waiting
        # until the whole batch finishes. Set to "false" to keep local
        # copies instead - e.g. if you've set up persistent storage.
        "delete_after_send": os.environ.get("DELETE_AFTER_SEND", "true").strip().lower()
        not in ("false", "0", "no"),
    }

    # api_id must be an int for Telethon, not a string.
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


def download_drive_folder(cfg):
    """Downloads all files from the public Drive folder using gdown.
    Returns a list of local file paths that were downloaded."""
    import gdown

    if not cfg["drive_url"]:
        print("GDRIVE_FOLDER_URL is not set. Add it to your .env file.")
        sys.exit(1)

    cfg["download_dir"].mkdir(parents=True, exist_ok=True)
    before = set(cfg["download_dir"].rglob("*"))

    print(f"Downloading files from Drive folder:\n  {cfg['drive_url']}")
    print(f"Saving to: {cfg['download_dir'].resolve()}\n")

    try:
        gdown.download_folder(
            url=cfg["drive_url"],
            output=str(cfg["download_dir"]),
            quiet=False,
            use_cookies=False,
        )
    except Exception as e:
        print(f"\nDownload failed: {e}")
        print(
            "\nCommon causes:\n"
            "  - The folder isn't shared as 'Anyone with the link'\n"
            "  - The URL is for a single file, not a folder "
            "(use the file's own download flow if so)\n"
            "  - Google is rate-limiting anonymous downloads - wait a "
            "bit and retry"
        )
        sys.exit(1)

    after = set(cfg["download_dir"].rglob("*"))
    new_files = sorted(p for p in (after - before) if p.is_file())

    if not new_files:
        # Folder may have been downloaded before; fall back to
        # everything currently in the directory.
        new_files = sorted(p for p in cfg["download_dir"].rglob("*") if p.is_file())

    if not new_files:
        print("No files found in that folder. Nothing to send.")
        sys.exit(0)

    print(f"\nDownloaded {len(new_files)} file(s).")
    return new_files


def _delete_local_file(path):
    """Best-effort delete with its own error handling, so a permission
    quirk on cleanup doesn't get confused with an upload failure or
    crash a run that otherwise succeeded."""
    try:
        path.unlink()
        print(f"  Deleted local copy ({path.name}).")
    except OSError as e:
        print(f"  Warning: couldn't delete local file {path.name}: {e}")


async def send_files_to_telegram(
    client, chat_id, files, max_upload_bytes, delete_after_send
):
    """Sends each file to the given chat, one at a time. Skips (with a
    clear message) any file over Telegram's size limit instead of
    letting the upload fail partway through.

    If delete_after_send is True, each file is removed from local
    storage right after it's confirmed sent - so at most one file's
    worth of disk space is used at a time, rather than the whole
    downloaded batch sitting there until the run finishes. Oversized
    (skipped) files are deleted too, since keeping them serves no
    purpose - they were never going to send. Files that FAIL to send
    are kept regardless of this setting, so a failed upload can be
    retried without re-downloading from Drive."""
    sent, skipped, failed = [], [], []

    for i, path in enumerate(files, 1):
        size = path.stat().st_size
        size_mb = size / (1024 * 1024)

        print(f"\n[{i}/{len(files)}] {path.name} ({size_mb:.1f} MB)")

        if size > max_upload_bytes:
            limit_mb = max_upload_bytes / (1024 * 1024)
            print(
                f"  SKIPPED - {size_mb:.1f} MB exceeds the "
                f"{limit_mb:.0f} MB limit. This file cannot be sent "
                f"via Telegram in one piece."
            )
            skipped.append(path)
            if delete_after_send:
                _delete_local_file(path)
            continue

        try:
            def progress(current, total):
                pct = current / total * 100
                print(f"  Uploading... {pct:.0f}%", end="\r")

            await client.send_file(
                chat_id,
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
            print(f"  Keeping local copy so you can retry without re-downloading.")
            failed.append((path, str(e)))

    return sent, skipped, failed


async def list_chats(client):
    """Prints the user's chats with their IDs, so they can pick which
    one to put in TELEGRAM_CHAT_ID."""
    print("\nYour chats (use the ID on the right in TELEGRAM_CHAT_ID):\n")
    async for dialog in client.iter_dialogs():
        kind = "group/channel" if dialog.is_group or dialog.is_channel else "user"
        print(f"  {dialog.id:>15}   [{kind:14}]  {dialog.name}")
    print(
        "\nCopy the ID for the chat you want, put it into "
        "TELEGRAM_CHAT_ID in your .env file, then re-run "
        "without --list-chats."
    )


async def main_async(args, cfg):
    from telethon import TelegramClient

    session_name = "drive_to_telegram_session"
    client = TelegramClient(session_name, cfg["api_id"], cfg["api_hash"])

    print("Connecting to Telegram...")
    print(
        "(First run only: you'll be asked for your phone number, then "
        "a login code Telegram sends you. After that, the session file "
        f"'{session_name}.session' keeps you logged in - as long as it "
        "isn't deleted.)\n"
    )
    await client.start()

    if args.list_chats:
        await list_chats(client)
        await client.disconnect()
        return

    if not cfg["chat_id"]:
        print(
            "TELEGRAM_CHAT_ID is not set. Run with --list-chats first:\n"
            "  python3 drive_to_telegram.py --list-chats\n"
            "then add the ID it prints for your target chat to .env."
        )
        await client.disconnect()
        sys.exit(1)

    files = download_drive_folder(cfg)

    max_upload_bytes = cfg["max_upload_mb"] * 1024 * 1024

    if cfg["delete_after_send"]:
        print(
            f"\nSending {len(files)} file(s) to chat {cfg['chat_id']}... "
            "(each will be deleted from local storage right after it sends)"
        )
    else:
        print(f"\nSending {len(files)} file(s) to chat {cfg['chat_id']}...")

    sent, skipped, failed = await send_files_to_telegram(
        client, cfg["chat_id"], files, max_upload_bytes, cfg["delete_after_send"]
    )

    print("\n" + "=" * 50)
    print("DONE")
    print(f"  Sent:    {len(sent)}")
    print(f"  Skipped (too large): {len(skipped)}")
    print(f"  Failed:  {len(failed)}")
    if skipped:
        note = " (deleted locally too)" if cfg["delete_after_send"] else ""
        print(f"\nSkipped files (over {cfg['max_upload_mb']} MB limit){note}:")
        for p in skipped:
            print(f"  - {p.name}")
    if failed:
        kept_note = (
            " Local copies were kept so you can retry without re-downloading."
            if cfg["delete_after_send"]
            else ""
        )
        print(f"\nFailed files:{kept_note}")
        for p, err in failed:
            print(f"  - {p.name}: {err}")
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

    # --list-chats only needs Telegram credentials, not the Drive URL
    # or chat ID - those aren't known yet, that's the point of this flag.
    if missing:
        print("Missing required environment variables:")
        for name in missing:
            print(f"  - {name}")
        print(
            "\nSet these in a .env file (see .env.example) or as "
            "environment variables. See README.md for how to get them."
        )
        sys.exit(1)

    if not args.list_chats and not cfg["drive_url"]:
        print("GDRIVE_FOLDER_URL is not set. Add it to your .env file.")
        sys.exit(1)

    import asyncio
    asyncio.run(main_async(args, cfg))


if __name__ == "__main__":
    main()
