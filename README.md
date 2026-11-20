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
| `HELIUS_API_KEY` | yes | a coin's first buyers in one call, whatever its size, plus parsed swap history and a private RPC. **The one that matters most.** |
| `SOLSCAN_API_KEY` | no | the Pro API answers a free-plan key with `401 Please upgrade your api key level`. Only worth setting on a paid plan |
| `ETHERSCAN_API_KEY` | partly | one V2 key, but the free plan now covers only Ethereum, Arbitrum and Polygon. Base and Avalanche fall back to keyless public explorers (Blockscout, Routescan); BSC, Robinhood Chain and Arc need a paid plan |
| `BLOCKSCOUT_API_KEY` | yes | optional. Only raises Blockscout's rate limits; Base works without it |

Market data comes from DexScreener, which needs no key at all.

Without Helius or Solscan the bot falls back to paginating the public Solana RPC backwards
through a token's signature history. That works for small and mid-size coins. For a coin
with millions of transactions it will tell you it could not reach the first buyers rather
than hand you a plausible-looking list of wallets that were never actually early.

## Commands

```
/wallets <token> [n]   steps 2-4 - the early buyers worth tracking, with Track buttons
/analyze <token>       all seven steps - wallets, their recent buys, overlaps, scores
/score <token>         step 7 only - run one coin through the scoring system

/harvest <token> [n]   scan a coin and track every surviving early buyer at once
/traders <token> [n]   the wallets that put in least and took out most
/add                   guided manual add - paste one wallet or a whole column
/track <wallet> [label] [chain]     the same thing in one line
/label <wallet> <name> rename a tracked wallet
/untrack               list every wallet with an Untrack button, and a sweep for idle ones
/untrack <wallet>      stop following one wallet, or several pasted at once
/tracked
/scan                  steps 5-7 across every wallet you track, on demand

/wallet <address>      price their whole book, position by position
/bankroll <usd>        your account size, so alerts scale their conviction to you

/watch on|off          background alerts on every entry AND exit
/sells on|off          exit alerts specifically (on by default)
/minscore <n>          only alert on entries at or above this score (default 6)
/settings

/autoscan on|off       run /scan on a timer, reporting only what changed
/queue <token>         line up past runners to be mined for early buyers
/autoharvest on|off    work through that queue, one coin per cycle
/trending              what is being boosted right now, to pick from

/leaderboard           grade every wallet on how its calls to you performed
/diag                  test every API key and name which one is broken
```

## Pruning without typing an address

`/untrack` on its own lists every wallet you follow, eight to a page, each with
its own Untrack button. Wallets that have gone quiet on every chain they are
watched on are marked `(idle)`, and one button at the bottom drops all of them
at once after asking.

The leaderboard carries the same buttons. You are already looking at the
evidence for dropping a wallet there, so you should not have to copy its
address into another command to act on it. Pressing one removes the button and
leaves the board itself alone, so the ranks you are reading do not renumber
under you.

Idle means dormant on every chain, which is only set after several consecutive
empty reads. One failed lookup does not mark a wallet dead.

## Who made money, not just who was early

Being early is a timestamp. Turning twenty dollars into two thousand is a
decision taken twice, once to size and once to sell. `/traders <token>` ranks
the wallets that did it:

```
1. 7xKq..3mAe  60x
   in $13.00 (3.5M / 1 tx)  out $783.00 (3.5M / 1 tx)
   profit $770.00
```

`/harvest` runs the same scan and tracks those wallets alongside the early
buyers, labelled `VCAT 60x` rather than `VCAT early #3`. Set `HARVEST_TRADERS=0`
to keep harvests to early buyers only.

The ranking is on the **multiple**, not the profit, or it would only ever find
whoever traded biggest. `TRADER_MIN_SPEND_USD` keeps out the wallet that risked
a fraction of a cent and shows a five-figure multiple.

**Where the numbers come from.** DexScreener shows this table on its own site
but does not serve it through its API — `io.dexscreener.com` answers every
request with a bot challenge — so the bot computes it from the chain:

| Chain | How | Coverage |
|---|---|---|
| Solana | the coin's transactions, read from both ends, per-owner balance deltas | every trader, unless the coin is older than `TRADER_SCAN_PAGES` allows |
| EVM | per wallet: its transfers of the coin, what it spent, and the internal call that paid it back | the early-buyer cohort only |

The EVM limit is not a shortcut. Base caps `eth_getLogs` near 2,000 blocks and a
Uniswap v4 coin has no pair contract to filter on, so reading a whole token's
history keylessly is not possible. The output says so when it applies.

**Multiples are exact; dollars are not.** A multiple is computed in the quote
asset, SOL or ETH, before any conversion. The dollar figures price old trades at
today's rate, so read them as scale rather than as a statement of what someone
banked.

## Running it unattended

Four jobs run on their own once the bot is up. They start staggered so a restart
does not fire all of them into the same rate limit.

| Job | Default | What it does |
|---|---|---|
| Watcher | every 10m | diffs balances, alerts on entries and exits |
| Outcomes | every 20m | re-prices open alerts so the leaderboard has data |
| Auto-scan | every 6h | steps 5–7 across every tracked wallet |
| Auto-harvest | every 12h | mines one queued coin for early buyers |

**Auto-scan only messages you when something moved** — a token reaching the overlap
bar for the first time, picking up another wallet, or its score shifting half a point.
A digest that repeated the same overlap every six hours would train you to ignore it.
The cost of that choice is worth stating plainly: silence means nothing changed, not
that nothing ran. `/settings` shows which jobs are on and how often they fire; if you
want to confirm a scan actually happened, `/scan` runs the same thing on demand.

**Auto-harvest does one coin per cycle**, not the whole queue. Early-buyer lookups are
the most expensive call the bot makes, and firing twenty at once is the fastest way to
get rate limited off the free tiers. A queue of ten works itself off over five days.

Step 1 of the method — which past winner to mine — stays yours. `/queue` is a list you
fill. `/trending` shows what DexScreener is boosting so you have names to pick from, but
read that list for what it is: tokens somebody **paid** to put in front of you, which is
close to the opposite of what step 1 asks for. It is a place to spot names, not a source
of past winners. No free API exposes "coins that already did 100x", so that judgement
does not get automated here.

## When a lookup comes back empty

Every data source here fails quietly by default. Etherscan answers a rejected key
with the same shape it uses for "no results". Solscan answers one with a bare 401.
The Solana RPC just returns nothing. Each of those used to surface as *"no early
buyers found"* — a statement about the coin, when it was really a statement about
a broken credential.

That is fixed in two places. Each provider now keeps the API's own error text and
the bot repeats it verbatim, and **`/diag`** asks every source a question it must be
able to answer:

```
[ok] DexScreener        SOL at $105.55
[ok] Helius RPC         at slot 312,884,201
[!!] Solscan            key rejected (401)
[ok] Blockscout / base  chainid 8453, answered
```

The explorer named is the one that answered. Etherscan's free plan refuses Base, so
the bot hands Base to Blockscout on the first refusal and says so here.

`/diag` starts with Telegram, because a bot that cannot reach Telegram cannot report
anything, including its own diagnostics. That line shows what each resolver says
about `api.telegram.org`, which is what the next section is about.

## Starting it with the laptop

```powershell
powershell -ExecutionPolicy Bypass -File install_autostart.ps1
```

That registers a scheduled task which starts the bot a minute after you log in,
restarts it up to three times if it dies, and keeps it running on battery. It
runs as you, needs no administrator rights and stores no password. Remove it
with the same command and `-Uninstall`.

| Command | What it does |
|---|---|
| `Start-ScheduledTask -TaskName 'Memehunter bot'` | start it now without logging out |
| `Stop-ScheduledTask -TaskName 'Memehunter bot'` | stop it |
| `Get-ScheduledTaskInfo -TaskName 'Memehunter bot'` | when it last ran |
| `Get-Content bot.log -Tail 20` | what it is doing |

It runs under `pythonw.exe`, so nothing appears on screen. The bot writes
`bot.log` itself rather than relying on a shell to redirect its output, and
rotates it at 2 MB.

A logon trigger can fire before the Wi-Fi associates, so a start that cannot
reach Telegram now waits and retries, backing off from 5 seconds to 5 minutes
before giving up. A bad token or a second copy already running are treated as
permanent and stop immediately, because retrying either would only repeat the
same failure every few minutes for as long as the laptop is on.

The task only runs while you are logged in. Closing the lid or logging out
stops the bot, and anything a tracked wallet does in the meantime is missed.
A machine that stays on is still the only way to watch continuously.

## Only one copy at a time

Two copies of the bot both ask Telegram for updates, and Telegram gives each
update to whichever asked first. Commands then land in one process or the other
at random, which looks like the bot ignoring you. The only clue is a line buried
in the log:

```
telegram.error.Conflict: Conflict: terminated by other getUpdates request
```

So a second copy now refuses to start and says why. It claims a local port for
as long as it runs, which the operating system releases whenever the process
ends - a lock file left behind by a crash would block every later start.

## When the network lies about an address

Some networks answer `api.telegram.org` with an address that is not Telegram's. Here
it comes back as `108.160.166.137`, which belongs to Dropbox and never completes a
handshake, so the bot dies at startup with:

```
telegram.error.NetworkError: httpx.ConnectError: All connection attempts failed
```

That reads like the bot being offline. It is one wrong lookup - Telegram's real
addresses route fine from the same machine, with no VPN.

Pointing at a public DNS server does not fix it on its own. Measured on this network,
half the plain-UDP queries **sent to 8.8.8.8** came back with the forged address:
something answers port 53 first, whoever you asked.

```
20 queries to 8.8.8.8 -> 10 real answers, 10 forged
```

So the bot resolves those names over HTTPS instead, where a forged reply cannot
survive TLS. The resolvers are addressed by IP, so this needs no working DNS to get
started, and the answers are served to every library in the process. Nothing about
the machine's settings changes, and no VPN is involved.

| Setting | Default | What it does |
|---|---|---|
| `DNS_FIX` | `1` | turn the whole thing off with `0` |
| `DNS_OVERRIDE_HOSTS` | `api.telegram.org` | names resolved over HTTPS instead of by the network |
| `DNS_RESOLVERS` | Google, Cloudflare | DNS-over-HTTPS endpoints, tried in order |

Every other name still goes to your normal resolver first. A name only falls through
to HTTPS when that resolver fails or blackholes it, so private and internal names keep
working as before.

If a chain fails there, anything on it will come back empty — and that emptiness is
the failure, not a verdict on a coin.

## Chains

| Chain | Needs | Notes |
|---|---|---|
| Solana | Helius key (or a paid Solscan plan) | the deepest history, and what the method was written for |
| Ethereum, Arbitrum, Polygon | Etherscan V2 key (free plan), or keyless Blockscout | mature, years of past cycles to mine |
| Base | nothing - keyless Blockscout; Etherscan only on a paid plan | most of the EVM memecoin flow |
| Avalanche | nothing - keyless Routescan; Etherscan only on a paid plan | thin memecoin flow |
| BSC | Etherscan V2 key on a paid plan | no keyless public explorer |
| Robinhood Chain (4663) | Etherscan V2 key | Arbitrum Orbit L2, live July 2026 |
| Arc (5042) | Etherscan V2 key | Circle L1, live September 2026. Gas is USDC, not ETH |

Chain is detected from the address you paste; there is no mode to switch. Etherscan's
free community endpoints for Robinhood Chain and Arc expire **15 October 2026**, after
which those two need a Lite plan.

**An EVM wallet is watched on every EVM chain.** Track `0xABC` on Base and it is
tracked on Arc, Robinhood, BSC and the rest too, because the same address is the same
person everywhere — a wallet with a good record on Base is exactly the one you want to
hear about the first time it appears on a new chain. `/tracked` shows one entry per
wallet with the chains it is actually active on. Solana keys have no equivalent
elsewhere, so they are never expanded. Turn it off with `MULTICHAIN=0`, or narrow it
with `MULTICHAIN_CHAINS=base,arc`.

Watching eight chains would cost eight times as much if it were naive, so chains a
wallet has no history on are **parked** after three consecutive empty reads and retried
every 12 hours. Three, not one, because Etherscan returns an empty list for a failed
request as well as for a genuinely empty wallet, and parking on a single blip would
silently stop watching a chain. The retry is what lets a wallet that starts using Arc
next month still get picked up.

**Overlap never crosses chains.** Step 6 — the same coin in three wallets — is computed
per chain, because a Solana mint and a Base contract are different tokens. Tracking ten
Solana wallets and ten Arc wallets is two separate hunts, not one with twenty. Pick a
chain and go deep before adding another.

**Young chains break step 1.** The method starts with a coin from a *previous cycle*, and
then asks which of its early buyers are still active 30 days later. Arc is days old and
Robinhood Chain is months old, so on those chains there is barely a past to mine and the
"still active after 30 days" filter has almost nothing to cut. They will work properly
once they have history; today they are a bet that early movers there become the wallets
worth following later.

**Sui is not supported.** It is not an EVM chain and Etherscan does not cover it, so it
needs a provider of its own rather than a line in the chain table.

## Surviving a host with no disk

Free container hosts rebuild the filesystem on every redeploy, which would take
the tracked wallets, position baselines and alert history with it. So the bot backs
its database up to your Telegram chat every 6 hours and pins it, and restores from
the pinned copy when it starts up empty. `/backup` and `/restore` do it by hand;
`DEPLOY.md` has the detail.

Restore only fires automatically when there are no tracked wallets *and* no alert
history, so it can never overwrite real work.

## The leaderboard

`/leaderboard` ranks your tracked wallets by **how their calls to you actually turned
out** — not by their own historical PnL.

That distinction is the whole point. A wallet's lifetime PnL is measured on entries you
could not take, at prices you never got, mostly from positions opened long before you
started watching. It tells you the wallet was good once. So every entry alert records the
price at the moment it fired, the price is re-checked every 20 minutes for 72 hours, and
the wallet is graded on that record alone.

| Column | Meaning |
|---|---|
| `2/3 hit 1.50x` | how many of its calls reached the win bar |
| `peak avg` | average best price each call reached |
| `now avg` | where those calls stand today |
| grade | hit rate shrunk toward a 25% base rate by sample size |

Three things that ranking gets right, and are worth knowing before you trust it:

- **Peak, not last.** A coin that went 4x and came back to flat was a good call badly
  managed. Grading it as a loss would tell you to drop a wallet that keeps handing you
  4x. Both numbers are shown, and a wallet whose calls consistently round-trip is
  labelled **"good entries, you must exit yourself"** rather than praised.
- **Small samples are shrunk.** A wallet that is 1-for-1 is not better than one that is
  12-for-20, so confidence rises with sample size instead of being asserted. Under three
  calls a wallet grades `NEW` and is not ranked seriously.
- **It prunes on evidence.** Wallets silent for three weeks, or graded `D` over five or
  more calls, are listed as worth dropping. Step 3 of the method is discarding dead
  wallets, and one you tracked last month can die like any other.

The honest limit: this needs time. A wallet with two calls tells you nothing, and the
board is only as good as the alert history behind it. Expect a fortnight before the
ranking means much, and treat anything above it as provisional.

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
python selftest.py          # 273 offline checks, no network or keys needed
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
tests_auto.py               scheduling, queue and leaderboard checks
memehunter/
  bot.py                    commands, buttons, and the four scheduled jobs
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
    leaderboard.py          grading wallets on how their calls actually performed
```
