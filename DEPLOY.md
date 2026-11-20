# Running the bot 24/7 without your laptop

The bot is a long-running process. Close the terminal or sleep the machine and
it stops, so anything a tracked wallet does while it is off goes unseen. This is
how to put it somewhere that stays up.

## What it costs, honestly

**There is no good free 24/7 host any more.** Railway's free tier ended and
Fly.io's went with it. Anyone still saying otherwise is quoting 2023.

| Option | Real cost | Effort | Notes |
|---|---|---|---|
| **Koyeb / Back4App** | £0, no card | 15 min | Free forever, but no disk — needs the Telegram backup below |
| **Railway** | $5 trial credit, then ~$2–3/mo | 10 min | Easiest by far. Hobby plan is $5/mo minimum |
| **Oracle Cloud Always Free** | £0, genuinely forever | 1–2 hrs | An ARM VM that never expires. Card needed to verify, not charged. Fiddly signup |
| **Any cheap VPS** (Hetzner, Contabo) | ~£4/mo | 30 min | A real machine you control |
| **Raspberry Pi / old laptop at home** | £0 if you own one | 30 min | Needs your internet to stay up |
| **Keep it on your laptop** | £0 | none | Only runs while the laptop is awake |

This bot is tiny — roughly 150 MB of RAM and almost no CPU — so on usage-based
hosts you are near the bottom of any bill.

---

## Free hosting, and the disk problem

Free container hosts (Koyeb, Back4App, Render) give you **no persistent disk**.
The filesystem is rebuilt on every redeploy, restart and idle recycle. For this
bot that is not cosmetic: the database holds your tracked wallets, the position
baselines that make a first buy detectable, and the whole alert history behind
`/leaderboard`. Lose it and the bot keeps running while silently knowing nothing.

So the bot backs itself up to the one durable store it already has — **your
Telegram chat**. It sends the database as a file every 6 hours and pins it. On
startup, if the database is empty, it reads the pinned message and restores.

The pin matters: a file id would have to be stored in the database, which is the
thing that just vanished. A pinned message can be found with nothing but the
chat id, so recovery needs no prior state.

```
/backup     send the database to this chat and pin it, now
/restore    restore from the pinned backup
            (or reply /restore to any older backup file to go back to that one)
```

Restore is deliberately cautious: it only happens automatically when there are
no tracked wallets AND no alert history, so it can never overwrite real work.
Set `BACKUP_AUTO_RESTORE=0` if you would rather always do it by hand.

### Koyeb (free, no card)

1. Push to GitHub (see the Railway steps below — same repo prep).
2. [koyeb.com](https://koyeb.com) → **Create Service** → GitHub → your repo.
3. Service type **Worker**, not Web — the bot serves no HTTP.
4. Instance: **Free**.
5. Paste the same environment variables as the Railway list below, but leave
   `DB_PATH` unset — with no volume there is nowhere better than the default.
6. Deploy, then send `/backup` once so a pinned copy exists from day one.

Free tiers idle out and restart more than paid ones do. That is fine here, as
long as a backup exists — the bot restores itself on the way back up.

**Do the first `/backup` manually.** The automatic one is 15 minutes after
startup, and a redeploy before then would leave nothing to restore from.

## Railway (the quick path)

### 1. Push the repo to GitHub

Railway deploys from a repo. `.gitignore` already excludes `.env` and `*.db`,
so your keys are not committed — check that before pushing.

```powershell
cd C:\Users\Uche\memehunter-bot
git add -A
git commit -m "Deploy config"
git push
```

### 2. Create the project

[railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
→ pick `memehunter-bot`. It finds the `Dockerfile` on its own.

### 3. Add a volume — do not skip this

**Settings → Volumes → New Volume**, mount path `/data`.

Without it the container's disk is wiped on every redeploy and restart, taking
your tracked wallets, position baselines and the entire leaderboard history with
it. The bot would come back looking like a fresh install and you would not
necessarily notice — it would just quietly have nothing.

### 4. Set the variables

**Variables** tab. Copy each line from your local `.env`:

```
TELEGRAM_BOT_TOKEN=...
ALLOWED_USER_IDS=...
HELIUS_API_KEY=...
SOLSCAN_API_KEY=...
ETHERSCAN_API_KEY=...
DB_PATH=/data/memehunter.db
```

`DB_PATH` must point inside the volume or step 3 was pointless.

### 5. Check it started

**Deployments → View Logs.** You want `memehunter ready`.

Then send `/start` in Telegram. **Stop the bot on your laptop first** — see below.

---

## Only ever run one copy

Telegram allows one long-polling connection per bot token. Two instances — your
laptop and the server — fight over it, and both drop messages with
`Conflict: terminated by other getUpdates request` in the logs.

So when the server copy is live, `Ctrl+C` the laptop one and leave it off. If
you want to develop locally later, stop the server deployment first, or make a
second bot with @BotFather and use its token for testing.

For the same reason `railway.json` pins `numReplicas: 1`. Do not raise it.

---

## Moving your existing data across

If you have already tracked wallets locally, upload `memehunter.db` into the
volume rather than starting over — it holds your baselines and alert history.
Easiest route is the Railway CLI:

```powershell
npm i -g @railway/cli
railway login
railway link
railway run --service <name> -- cp memehunter.db /data/memehunter.db
```

If you have not harvested anything yet, skip it. A fresh database costs you
nothing but the re-adding.

---

## Once it is up

Nothing about how you use it changes — same commands, same Telegram chat. Two
differences worth knowing:

- **Logs live in Railway now**, not your PowerShell window. That is where the
  Solscan header line and any errors appear.
- **Redeploys restart the bot.** It re-snapshots every wallet silently on the
  next watcher pass, so you do not get a flood of fake alerts, but there is a
  gap of one watcher interval where changes are absorbed into the new baseline
  rather than reported.

## If it will not start

| Log says | Cause |
|---|---|
| `Missing required settings in .env` | Variables not set in step 4 — the container has no `.env` file, only the variables tab |
| `Conflict: terminated by other getUpdates` | Two copies running. Stop the laptop one |
| Database empty after a redeploy | On a paid host: no volume, or `DB_PATH` not at `/data`. On a free host: no backup existed yet — send `/backup` |
| `No pinned backup found` | Nothing has been backed up. Send `/backup` while the data is still there |
| Build fails at `python selftest.py` | Deliberate — the image refuses to build if the checks fail. Read which one |
