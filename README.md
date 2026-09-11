# Memehunter

A Telegram bot that runs the seven-step smart-money method end to end: take a coin that
already ran, find who was early, throw out the dead wallets and the bots, watch what the
survivors are buying now, and only act on names that show up in several of them.

## The seven steps, and where each one lives

| Step | What it does | Code |
|---|---|---|
| 1. Pick a coin from a past cycle | you supply the address | `/analyze <token>` |
| 2. Top 20 early buyers | earliest wallets to receive the token | `providers/*.early_buyers` |
| 3. Keep only wallets active in 30d | last transaction timestamp | `providers/*.wallet_profile` |
| 4. Weed out bots | median gap between txs, txs per day, sub-second bursts | `solana.classify_bot` |
| 5. What did the survivors buy recently | acquisitions in the last 30d | `providers/*.wallet_recent_buys` |
| 6. Names that repeat across wallets | 1 wallet is luck, 3 is a signal | `analyzer.overlap_and_score` |
| 7. Score every candidate | 7 components, 10 points, below 9 you do not buy | `pipeline/scoring.py` |

## Setup

```bash
cd memehunter-bot
pip install -r requirements.txt
cp .env.example .env      # then fill it in
python main.py
```

**Required:** `TELEGRAM_BOT_TOKEN` from [@BotFather](https://t.me/BotFather), and
`ALLOWED_USER_IDS` with your Telegram user ID (get it from
[@userinfobot](https://t.me/userinfobot)). The bot ignores everyone else — leave this
locked down, since anyone with access burns your API quota.

**Data keys** — all optional, but the bot is much better with them:

| Key | Free tier | Buys you |
|---|---|---|
| `SOLSCAN_API_KEY` | yes | earliest buyers on Solana in one call. **The one that matters most.** |
| `HELIUS_API_KEY` | yes | fast parsed swap history + a private RPC instead of the public one |
| `ETHERSCAN_API_KEY` | yes | every EVM chain (ETH, Base, BSC, Arbitrum, Polygon, Avalanche) through one V2 key |

Market data comes from DexScreener, which needs no key at all.

Without a Solscan key the bot falls back to paginating the public Solana RPC backwards
through a token's signature history. That works for small and mid-size coins. For a coin
with millions of transactions it will tell you it could not reach the first buyers rather
than hand you a plausible-looking list of wallets that were never actually early.

## Commands

```
/wallets <token> [n]   steps 2-4 - the early buyers worth tracking, with Track buttons
/analyze <token>       all seven steps - wallets, their recent buys, overlaps, scores
/score <token>         step 7 only - run one coin through the scoring system

/harvest <token> [n]   scan a coin and track every surviving early buyer at once
/add                   guided manual add - paste one wallet or a whole column
/track <wallet> [label] [chain]     the same thing in one line
/label <wallet> <name> rename a tracked wallet
/untrack <wallet>
/tracked
/scan                  steps 5-7 across every wallet you track, on demand

/wallet <address>      price their whole book, position by position
/bankroll <usd>        your account size, so alerts scale their conviction to you

/watch on|off          background alerts on every entry AND exit
/sells on|off          exit alerts specifically (on by default)
/minscore <n>          only alert on entries at or above this score (default 6)
/settings
```

Tracking a wallet first snapshots what it already holds, so your first alert is a genuinely
new buy rather than a replay of the last month.

### Harvesting a coin

`/harvest <token>` is the whole front half of the method in one command: it pulls the
earliest buyers, drops the dead wallets and the bots, and adds every survivor to your
tracking list, labelled after the coin that surfaced them (`BONK early #1`, `#2`, ...).
The same thing is one button on any `/wallets` or `/analyze` result — **Track all N
wallets** — which reuses the scan you just ran instead of paying for it twice.

Harvest as many coins as you like. Wallets accumulate into one list, and a wallet that was
early on two different coins is merged rather than duplicated — it keeps the label and the
source coin from the first harvest that found it. That overlap is itself the signal the
method is built on: a wallet showing up in two separate winning cohorts is not luck.

Adding wallets is the cheap half. Once a few cohorts are in, `/scan` is where the method
pays off — it runs steps 5 to 7 across everything you track and shows you what they are
crowding into now.

### Adding wallets by hand

Three ways in, for wallets that never came out of an `/analyze` run — one someone sent you,
one you found on an explorer, or a list you have been keeping:

- **`/add`** walks you through it: paste the address, pick the chain if it is EVM, give it a
  name. Paste several at once — one per line, comma separated, or as explorer URLs — and it
  adds the lot.
- **`/track <wallet> [label] [chain]`** does it in one line when you already know the syntax.
- **Paste an address straight into the chat.** A bare address is genuinely ambiguous, so
  rather than guess the bot asks whether it is a wallet to track or a token to analyse.

Solana wallets need no chain. An EVM address alone does not say which chain it is on, so you
get a chain picker; `/track 0x... base` skips it.

You can drive the same pipeline from a terminal without Telegram:

```bash
python -m memehunter.cli analyze <token>
python selftest.py          # 119 offline checks, no network or keys needed
```

## The scoring system

Ten points across seven components, every one of them printed with the number that
produced it:

| Component | Max | Rewards |
|---|---|---|
| Smart-money overlap | 3.0 | how many tracked wallets hold it — 5+ earns the full 3.0 |
| Liquidity | 1.5 | $20k–$150k, deep enough to exit and small enough to still run |
| Age | 1.0 | days old, not hours and not months |
| Volume / liquidity | 1.5 | 0.5x–5x turnover, active without looking washed |
| Buy pressure | 1.0 | share of 24h trades that are buys |
| Contract safety | 1.5 | Solana: mint + freeze revoked, top-10 concentration. EVM: verified, not a proxy |
| Momentum | 0.5 | green on both 6h and 24h |

Red flags — live mint or freeze authority, a top-10 that owns half the supply, sub-$3k
liquidity, an unverified or upgradeable contract — force an `AVOID` no matter what the
number says. Tune every band in `pipeline/scoring.py`; it is the only file that encodes an
opinion.

## Exits

Sells are as public as buys. The reason they feel invisible is that nobody diffs
balances — so that is what the watcher does. Every poll it snapshots what each tracked
wallet holds and compares it against last time, which turns silence into four distinct
events:

| Event | Trigger |
|---|---|
| `NEW POSITION` | a token appears that the wallet did not hold |
| `ADDING` | the balance grows by more than `TRIM_ALERT_PCT` |
| `TRIMMING` | the balance shrinks by more than `TRIM_ALERT_PCT` |
| `EXITED` | the balance hits zero, or falls under `EXIT_DUST_PCT` of its peak |

Trims are measured against the position's **peak**, not the previous poll, so a wallet
bleeding out in five separate sales still reads as "80% of the top is gone" rather than
five small trims that each look survivable.

Two deliberate choices. A weak score suppresses *entry* alerts but never exit alerts —
a bad grade is a reason not to buy, never a reason to hide that the wallet you follow is
leaving. And a wallet whose balances come back empty when it held several positions a
moment ago is treated as a failed read, not a liquidation, because a flaky RPC should
never fire a panic alert.

**What this still cannot do is make you faster than them.** Polling means you learn
minutes after the fact, never before. Shorten `WATCHER_INTERVAL_MIN` and you narrow the
gap; you never close it. What it buys you is not front-running their exit — it is not
holding a bag they quietly dropped two days ago.

## Position size

`/wallet <address>` prices a wallet's entire book — every token holding plus the native
balance — so you can see what each position is actually worth to them.

That is what makes an alert readable. "They bought TOKEN" is noise. "They put $47k in,
8.2% of a $580k book" is a signal, and it is a different signal from the same wallet
putting in 0.3%.

Set `/bankroll 2000` and every alert also shows what that conviction is worth on your
account. **It scales the fraction, not the ticket** — copying their dollar amount is the
mistake, because $10k is a rounding error to them and your whole account to you. What
transfers between accounts of different sizes is the percentage committed.

The bot also names the extremes rather than leaving you to spot them: over 25% of a book
in one coin is flagged as unusual conviction, over 50% as gambling, and under 1% as a
lottery ticket they will not miss if it goes to zero — which is worth knowing before you
mirror it at 1% of *your* account and call it the same trade.

## What this still cannot do

- **You are always second out.** See above. Exits are visible but never early.
- **Sizing is a snapshot, not a cost basis.** A position is priced at what it is worth
  now, which is not what they paid for it. A wallet sitting on a 40x shows a huge
  position that may have started tiny.
- **Unpriced holdings understate a book.** Tokens with no DEX pair cannot be valued, so
  a portfolio total is a floor. The bot says how many it skipped.
- **The bot filter is heuristic.** It catches obvious speed and volume, not a patient
  automated strategy that trades like a person.
- **A wallet that was early once may just have been lucky once.** The method assumes
  repeatable process; three wallets agreeing is evidence, not proof.

Nothing here is financial advice, and none of it changes the fact that most meme coins go
to zero.

## Layout

```
main.py                     entry point
selftest.py                 offline checks
tests_exits.py              exit-detection and sizing checks
memehunter/
  bot.py                    commands, buttons, watcher job
  render.py                 all Telegram output
  config.py  db.py          settings, SQLite
  providers/
    base.py                 types, chain detection, rate-limited HTTP
    solana.py  evm.py       chain access
    prices.py               DexScreener
  pipeline/
    analyzer.py             the seven steps wired together
    scoring.py              step 7
    events.py               balance diffing: buys, adds, trims, exits
    portfolio.py            pricing a book, and scaling conviction to your bankroll
```
