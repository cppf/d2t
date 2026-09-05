# Drive → Telegram

Copies files from a public Google Drive folder into a Telegram chat,
sent under your own Telegram account so files up to 2 GB go through in
one piece (a bot account is capped at 50 MB — too small for files this
size).

## What this actually does, step by step

For each file in the Drive folder, in order:

1. **Downloads it** from Google Drive to disk inside the container
   (`gdown` handles this)
2. **Uploads it** from that disk copy to your Telegram chat (`Telethon`
   handles this)
3. **Deletes the local copy** once the upload is confirmed sent

Nothing streams directly from Drive to Telegram — each file is fully
downloaded before it starts uploading, and for the short window
between those two steps, that file's full size is sitting on disk
inside the container. For a 2 GB file, that means up to ~2 GB of
container disk in use, temporarily, per file.

Step 3 is the part worth knowing about specifically: **by default,
each file is deleted right after it sends**, rather than waiting until
every file in the folder has been processed. That caps disk usage at
roughly one file's worth at a time, instead of the whole folder's
worth piling up before anything gets cleaned up. If a file fails to
send, its local copy is kept on purpose — so you can retry without
re-downloading from Drive — and skipped files (over Telegram's size
limit) are deleted immediately since there's nothing further to do
with them.

You can turn this off with `DELETE_AFTER_SEND=false` in `.env` if
you'd rather keep local copies of everything — see the **Configuration
reference** section below. That only really makes sense if you've also
set up persistent storage (see **Making the session persist**), since
otherwise those files just vanish anyway the moment the container
exits.

## ⚠️ Before you start: read this

This setup keeps everything — session login, downloaded files —
**inside the container only**. Nothing is written to your host
machine. That's simpler to reason about, but it has one real
consequence worth knowing before you start:

**You will need to log in again (phone number + login code) every
single time you run the container** — not just the first time. Docker
containers are disposable by design, so every `docker compose run`
starts fresh with nothing saved from the last run.

If that sounds annoying, it's because it is — most people find it gets
old fast. The fix is one small addition: mount a folder from your host
so the login session (and optionally downloaded files) persist between
runs. See **"Making the session persist"** below if you'd rather do
that from the start. Otherwise, the steps below work as-is, and you
can always add persistence later — it's a small edit, not a rebuild
from scratch.

## Setup (once)

**1. Get Telegram API credentials**

- Go to https://my.telegram.org and log in with your phone number
- Click **API development tools**
- Create an app (any name/description is fine — it's just a label)
- Copy the `api_id` and `api_hash` it gives you

**2. Create your `.env` file**

```bash
cp .env.example .env
```

Open `.env` and fill in `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` with
the values from step 1. Leave `TELEGRAM_CHAT_ID` and
`GDRIVE_FOLDER_URL` blank for now — you'll fill those in during steps
5 and 6.

**3. Build the image**

```bash
docker compose build
```

**4. Make your Drive folder public**

In Google Drive: right-click the folder → **Share** → **General
access** → **Anyone with the link**. If it's still restricted to
specific people, downloading will fail later with a permissions error.

**5. Find your Telegram chat ID**

```bash
docker compose run --rm app --list-chats
```

This logs you in — asks for your phone number, then a login code
Telegram texts you — and prints all your chats with their numeric IDs.
Copy the ID of the chat/channel you want files sent to.

**6. Finish your `.env` file**

Add the chat ID from step 5, and your Drive folder's URL, to `.env`:

```
TELEGRAM_CHAT_ID=-1001234567890
GDRIVE_FOLDER_URL=https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
```

## Running it

```bash
docker compose run --rm app
```

Because of the "login every time" tradeoff above, this will ask for
your phone number and login code again, then download each file in
the Drive folder and send it to your chosen chat one at a time —
deleting each local copy as soon as it's confirmed sent (see **What
this actually does** above). Files over 2 GB are skipped with a clear
message rather than failing partway through an upload — that's a hard
Telegram limit for regular accounts (4 GB with Telegram Premium — set
`MAX_UPLOAD_MB=4096` in `.env` if that's you).

Note this uses `docker compose run`, not `docker compose up` — `run`
gives the container a real terminal to prompt you through login,
which `up` (built for detached background services) won't reliably
forward.

## Configuration reference

All settings live in `.env` (copied from `.env.example`):

| Variable | Required? | Default | What it does |
|---|---|---|---|
| `TELEGRAM_API_ID` | Yes | — | From my.telegram.org |
| `TELEGRAM_API_HASH` | Yes | — | From my.telegram.org |
| `TELEGRAM_CHAT_ID` | Yes (for a normal run) | — | Target chat; find via `--list-chats` |
| `GDRIVE_FOLDER_URL` | Yes (for a normal run) | — | Must be shared as "Anyone with the link" |
| `MAX_UPLOAD_MB` | No | `2048` | Telegram's send limit; use `4096` if you have Premium |
| `DELETE_AFTER_SEND` | No | `true` | Delete each file locally right after it sends — see above |

## Making the session persist

If you'd rather not log in on every run, mount a folder on your host
so the session file — and optionally downloaded files — survive
between container runs. Add a `volumes` section to the `app` service
in `docker-compose.yml`:

```yaml
services:
  app:
    build: .
    env_file:
      - .env
    stdin_open: true
    tty: true
    volumes:
      - ./data:/app/data
    environment:
      - DOWNLOAD_DIR=/app/data/drive_downloads
```

You'd also want the session file itself to land in that mounted
folder rather than `/app` directly — for that, change `session_name`
in `drive_to_telegram.py` (search for `drive_to_telegram_session`) to
`data/drive_to_telegram_session`, and create the folder locally before
your first run: `mkdir -p data`. After that, you'll only need to log
in once — `data/` (already covered by `.gitignore`) stays out of
version control automatically.

With this in place, you may also want to set `DELETE_AFTER_SEND=false`
in `.env` if you'd like a running local archive of everything sent —
otherwise files still get cleaned up after sending even with
persistent storage configured, since that setting is independent of
where the files live.

## Keep this private

- **`.env`** — contains your API credentials. Already in
  `.gitignore`. Never commit it, never share it, never paste its
  contents into a chat with anyone (including an AI assistant).
- **`*.session` files** (only relevant if you set up persistence
  above) — these work like a saved password for your Telegram
  account. Anyone with a copy can access your account without your
  password or 2FA. Already in `.gitignore`. Delete the file any time
  to revoke access and force a fresh login next run.

## Pushing this to GitHub

This repo is ready to push as-is:

```bash
git init
git add .
git commit -m "Initial commit"
```

Then create an empty repository on GitHub (no README/license/
.gitignore — you already have those here), and follow the "push an
existing repository" instructions GitHub shows you, which will look
like:

```bash
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git branch -M main
git push -u origin main
```

`.env` and any `*.session` files won't be pushed — `.gitignore`
excludes them automatically. Still worth a quick `git status` before
your first commit to confirm neither shows up as staged, just in case.

## If something goes wrong

- **Download fails** → the folder likely isn't shared as "Anyone with
  the link," or the URL is for a single file rather than a folder.
- **A file won't send** → check the size printed next to it; anything
  over 2 GB (2048 MB, or 4096 if you set `MAX_UPLOAD_MB=4096`) is a
  hard Telegram limit, not a bug.
- **"No space left on device" or similar disk errors** → Docker's
  default storage allowance for a container's writable layer can be
  smaller than you'd expect. `DELETE_AFTER_SEND=true` (the default)
  keeps usage to roughly one file at a time, but if you've disabled it
  or have several very large files queued, you may still hit this —
  check your Docker daemon's disk settings, or process files in
  smaller batches by pointing `GDRIVE_FOLDER_URL` at a subfolder.
- **Login prompt seems stuck / never appears** → make sure you're
  using `docker compose run --rm app ...`, not `docker compose up`.
  `up` doesn't reliably forward the interactive prompts this script
  needs for login.
- **A failed file won't stop showing up** → failed sends intentionally
  keep their local copy so you can retry without re-downloading. Check
  the error message printed next to that file for why the send
  failed — once fixed, just run the script again.
