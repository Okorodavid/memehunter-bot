# Getting Memehunter running

Written for this PC (Windows, repo already at `C:\Users\Uche\memehunter-bot`).
Steps 1–3 are done in a browser and take about ten minutes. Steps 4–6 are the terminal.

---

## 1. Make the Telegram bot

1. Open Telegram, search **@BotFather**, send `/newbot`.
2. Give it a name (anything) and a username ending in `bot`.
3. He replies with a token like `8123456789:AAH...`. **Copy it.**

## 2. Get your own Telegram user ID

Search **@userinfobot**, press Start. It replies with your numeric ID, e.g. `641239887`.

This locks the bot to you. Leave it locked — anyone else who finds your bot
would burn your API quota.

## 3. Get the data keys

All free, about two minutes each.

| Where | What you get | Worth it? |
|---|---|---|
| [solscan.io/apis](https://solscan.io/apis) | earliest buyers on Solana in one call | **Get this one.** It is the difference between a 5-second lookup and a 3-minute one |
| [helius.dev](https://helius.dev) | a private RPC instead of the public one | Get it. Free tier is 1M credits/month, far more than this uses |
| [etherscan.io/apis](https://etherscan.io/apis) | Ethereum, Base, BSC, **Robinhood Chain, Arc**, Arbitrum, Polygon, Avalanche | **Get this one too.** One key covers all eight |

The bot runs without any of them — it falls back to the public Solana RPC — but
early-buyer lookups get slow and rate-limited. Market data comes from DexScreener,
which needs no key at all.

**One deadline worth knowing:** Etherscan's free community endpoints for Robinhood
Chain and Arc run out on **15 October 2026**. After that those two chains need a
Lite plan or above. The older chains (Ethereum, Base, BSC, Arbitrum, Polygon,
Avalanche) stay on the free tier.

## 4. Create your .env

Open **PowerShell** and run these two lines. The first makes the file, the second
opens it:

```powershell
cd C:\Users\Uche\memehunter-bot
Copy-Item .env.example .env
notepad .env
```

Paste your values next to the two required lines:

```
TELEGRAM_BOT_TOKEN=8123456789:AAH-paste-yours-here
ALLOWED_USER_IDS=641239887
```

Then the keys from step 3, if you got them. Save and close.

Use the copy command rather than creating the file by hand — Notepad silently saves
new files as `.env.txt`, and the bot will not see it.

## 5. Install and check

Still in PowerShell:

```powershell
pip install -r requirements.txt
python selftest.py
```

You want `244 passed, 0 failed`. That runs entirely offline — it does not touch
your keys or the network, so it passing only means the code is sound, not that
your keys work. Step 6 is what proves those.

*If `python` is not recognised:* install Python 3.11+ from
[python.org](https://python.org/downloads) and tick **"Add Python to PATH"** during setup.

## 6. Start it

```powershell
python main.py
```

Leave that window open — closing it stops the bot. You should see
`memehunter ready`.

Now open your bot in Telegram and send `/start`. If you get the help text back,
everything is wired up.

---

## First run, in order

```
/queue DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263     ← BONK, as an example
/autoharvest on      finds its early buyers and tracks the survivors
/watch on            alerts when those wallets buy or sell
/autoscan on         every 6h, tells you what several of them are crowding into
/bankroll 500        so alerts scale their position size to your account
```

An EVM wallet you track is watched on **every** EVM chain automatically, so a wallet
you found on Base reports its first Arc or Robinhood buy too. Solana keys only exist
on Solana, so those are watched there alone.

Queue three or four coins that already ran, not one. The method works on overlap —
a coin appearing in one wallet is luck, in three it is a signal — and you cannot
get overlap from a single cohort.

Then leave it alone. Check `/tracked` after a day and `/leaderboard` after two weeks.

---

## Things that will bite you

**The bot dies when you close PowerShell or the PC sleeps.** That is the real
constraint on running this at home. Two fixes:

- *Quick:* Windows Settings → System → Power → set "Screen and sleep" to Never while
  it is running.
- *Proper:* deploy it to [Railway](https://railway.app) free tier so it runs 24/7
  without your PC. Ask me and I will set that up — it is a 10-minute job and
  removes the problem entirely.

**Silence is normal and does not mean it broke.** Auto-scan only messages you when
something actually changed. If you want to confirm it is alive, `/scan` runs the
same thing on demand and always replies.

**The leaderboard is empty for a fortnight.** It grades wallets on how their calls
to *you* performed, so it needs alerts to have fired and prices to have moved. A
wallet with two calls tells you nothing.

**`/trending` is not a list of past winners.** Those are tokens someone *paid* to
promote — close to the opposite of what you want for step 1. Use it to spot names,
then judge for yourself before queueing.

**Your first `/harvest` on a huge coin may refuse.** Without a Solscan key the bot
pages backwards through signature history, and on a coin with millions of
transactions it will tell you it could not reach the first buyers rather than hand
you wallets that were never actually early. That refusal is correct behaviour.

---

## If something goes wrong

| Symptom | Cause |
|---|---|
| `Missing required settings in .env` | Step 4 not saved, or the file is called `.env.txt`. Run `dir .env` to check it exists |
| `Not authorised. Add 123... to ALLOWED_USER_IDS` | Wrong ID in `.env`. Copy the number it prints |
| Bot never replies to `/start` | `python main.py` is not running, or the window was closed |
| `No module named telegram` | Step 5's `pip install` did not run in this folder |
| Early-buyer lookups time out | No Solscan key — see step 3 |
| `ETHERSCAN_API_KEY is not set, so base is unavailable` | Etherscan key missing — every EVM chain needs it |
| `That token trades on sui, which this bot cannot query` | Correct — Sui needs its own provider, see below |

Nothing here is financial advice, and most meme coins go to zero regardless of who
bought them first.
