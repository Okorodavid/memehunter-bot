"""Telegram front end: commands, buttons, and the background watcher job."""
from __future__ import annotations

import asyncio
import logging
import time
from functools import wraps
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, ContextTypes, ConversationHandler,
                          MessageHandler, filters)

from . import db, render
from .config import CFG
from .pipeline.analyzer import Analyzer
from .pipeline.events import diff_positions, looks_like_read_failure
from .pipeline.leaderboard import (build_scorecards, stale_wallets, summarise,
                                   underperformers)
from .pipeline.portfolio import size_for_you
from .providers.base import EVM_CHAINS, detect_chain, extract_addresses, short

ASK_ADDRESS, ASK_CHAIN, ASK_LABEL = range(3)

log = logging.getLogger("memehunter")

HELP = """<b>Memehunter</b> - the seven-step smart-money hunt, automated.

<b>Find wallets</b>
/wallets &lt;token&gt; - steps 2-4: earliest buyers, minus the dead ones, minus the bots
/analyze &lt;token&gt; - all seven steps: wallets, their recent buys, overlaps, scores

<b>Track them</b>
/harvest &lt;token&gt; [n] - scan a coin and track every surviving early buyer at once
/add - add a wallet by hand, guided step by step (paste one or many)
/track &lt;wallet&gt; [label] [chain] - same thing in one line
/label &lt;wallet&gt; &lt;name&gt; - rename a tracked wallet
/untrack &lt;wallet&gt;
/tracked - list what you are watching
/scan - run steps 5-7 across every wallet you track, right now

<b>Let it run by itself</b>
/autoscan on|off - run /scan on a timer, reporting only what changed
/queue &lt;token&gt; - line up past runners to be mined for early buyers
/autoharvest on|off - work through that queue, one coin per cycle
/trending - what is being boosted right now, to pick from

<b>Rank your wallets</b>
/leaderboard - grade every wallet on how its calls to you actually performed

Or just paste an address into the chat and I will ask whether it is a wallet
to track or a token to analyse.

<b>Score</b>
/score &lt;token&gt; - run one token through the scoring system

<b>Position size</b>
/wallet &lt;address&gt; - price their whole book and see what each position is worth
/bankroll &lt;usd&gt; - your account size, so alerts can scale their conviction to you

<b>Alerts</b>
/watch on|off - background alerts on every entry AND exit
/sells on|off - exit alerts specifically (on by default)
/minscore &lt;n&gt; - only alert on entries at or above this score (default 6)
/settings - show current configuration

Paste a Solana mint or an EVM contract address anywhere a &lt;token&gt; is asked for.

<i>Exits are as public as entries, but you always see them second.
Nothing here is financial advice.</i>"""


# --------------------------------------------------------------------------- auth
def restricted(func: Callable) -> Callable:
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        user = update.effective_user
        if not user or (CFG.allowed_user_ids and user.id not in CFG.allowed_user_ids):
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"Not authorised. Add {user.id if user else '?'} to ALLOWED_USER_IDS."
                )
            return None
        return await func(update, context, *a, **kw)
    return wrapper


def busy_guard(func: Callable) -> Callable:
    """One long-running analysis per chat; the APIs behind this are rate limited."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        running: set = context.application.bot_data.setdefault("running", set())
        chat_id = update.effective_chat.id
        if chat_id in running:
            await update.effective_message.reply_text(
                "Still working on your last request. Give it a moment.")
            return None
        running.add(chat_id)
        try:
            return await func(update, context, *a, **kw)
        finally:
            running.discard(chat_id)
    return wrapper


def az(context: ContextTypes.DEFAULT_TYPE) -> Analyzer:
    return context.application.bot_data["analyzer"]


def conn(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["db"]


async def send(update: Update, text: str, **kw) -> Any:
    return await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kw)


def cohort_keyboard(context: ContextTypes.DEFAULT_TYPE, res, token: str):
    """Buttons for a finished scan: track the whole cohort, or pick wallets off it.

    The cohort is stashed in user_data rather than in callback data, so pressing
    'track all' does not re-run the scan that produced it.
    """
    if not res.survivors:
        return None
    symbol = (res.market.symbol if res.market else "") or token[:6]
    context.user_data["cohort"] = {
        "token": token,
        "chain": res.chain,
        "symbol": symbol,
        "wallets": [p.address for p in res.survivors],
    }
    rows = [[InlineKeyboardButton(
        f"Track all {len(res.survivors)} wallets", callback_data="harvall")]]
    rows += [
        [InlineKeyboardButton(f"Track {short(p.address, 5, 5)}",
                              callback_data=f"trk|{p.chain}|{p.address}")]
        for p in res.survivors[:6]
    ]
    return InlineKeyboardMarkup(rows)


class Progress:
    """Edits a single status message instead of spamming the chat."""

    def __init__(self, message):
        self.message = message
        self._last = ""

    async def __call__(self, text: str) -> None:
        if text == self._last:
            return
        self._last = text
        try:
            await self.message.edit_text(text)
        except BadRequest:
            pass


# ----------------------------------------------------------------------- commands
@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send(update, HELP)


@restricted
@busy_guard
async def cmd_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send(update, "Usage: <code>/wallets &lt;token address&gt;</code>")
        return
    token = context.args[0]
    limit = CFG.early_buyer_count
    if len(context.args) > 1 and context.args[1].isdigit():
        limit = max(5, min(50, int(context.args[1])))

    status = await update.effective_message.reply_text("Step 2 - resolving token...")
    progress = Progress(status)
    try:
        res = await az(context).find_smart_wallets(token, progress, limit)
    except ValueError as exc:
        await status.edit_text(str(exc))
        return
    except Exception as exc:
        log.exception("wallets failed")
        await status.edit_text(f"Lookup failed: {exc}")
        return

    if res.error:
        await status.edit_text(res.error)
        return

    lines = [
        f"<b>{render.esc(res.market.symbol if res.market else token[:8])}</b> "
        f"on {res.chain}",
        f"{len(res.early_buyers)} early buyers -> {res.dead_count} dead, "
        f"{res.bot_count} bots, <b>{len(res.survivors)} worth tracking</b>\n",
    ]
    for i, prof in enumerate(res.survivors[:12], 1):
        lines.append(render.render_wallet(prof, az(context), i))
    if not res.survivors:
        lines.append("Every early buyer is dead or automated. Pick another coin.")

    keyboard = cohort_keyboard(context, res, token)
    await status.delete()
    await send(update, render.clip("\n".join(lines)), reply_markup=keyboard)


@restricted
@busy_guard
async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send(update, "Usage: <code>/analyze &lt;token address&gt;</code>")
        return
    status = await update.effective_message.reply_text("Step 1 - resolving token...")
    progress = Progress(status)
    try:
        res = await az(context).analyze_token(context.args[0], progress)
    except ValueError as exc:
        await status.edit_text(str(exc))
        return
    except Exception as exc:
        log.exception("analyze failed")
        await status.edit_text(f"Analysis failed: {exc}")
        return

    await status.delete()
    text = render.render_analysis(res, az(context))
    await send(update, text,
               reply_markup=cohort_keyboard(context, res, context.args[0]))


@restricted
@busy_guard
async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send(update, "Usage: <code>/score &lt;token address&gt;</code>")
        return
    status = await update.effective_message.reply_text("Scoring...")
    try:
        cand, chain = await az(context).score_one(context.args[0])
    except ValueError as exc:
        await status.edit_text(str(exc))
        return
    except Exception as exc:
        log.exception("score failed")
        await status.edit_text(f"Scoring failed: {exc}")
        return
    await status.delete()
    body = render.render_candidate(cand, az(context))
    body += "\n\n" + render.render_score(cand.score)
    if cand.score.total < CFG.score_buy_threshold or cand.score.flags:
        body += (f"\n\n<i>Below your {CFG.score_buy_threshold}/10 bar. "
                 f"The rule says skip it.</i>")
    await send(update, render.clip(body))


@restricted
async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Direct form: /track <wallet> [label] [chain]. See /add for the guided version."""
    if not context.args:
        await send(update, (
            "Usage: <code>/track &lt;wallet&gt; [label] [chain]</code>\n"
            "Or just use /add and I will walk you through it, "
            "or paste an address straight into the chat."
        ))
        return
    chain_arg = next((a for a in context.args if a.lower() in EVM_CHAINS), None)
    addresses = extract_addresses(" ".join(context.args))
    if not addresses:
        await send(update, "That is not a valid Solana or EVM address.")
        return
    label = " ".join(
        a for a in context.args[1:]
        if a != chain_arg and a not in addresses
    ) or None
    await do_track(update, context, addresses, label, chain_arg)


def harvest_labels(symbol: str, addresses: list[str]) -> dict[str, str]:
    """Name harvested wallets after the coin that surfaced them, in entry order."""
    tag = (symbol or "?").strip()[:12] or "?"
    return {a: f"{tag} early #{i}" for i, a in enumerate(addresses, 1)}


async def do_track(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   addresses: list[str], label: str | None = None,
                   evm_chain: str | None = None,
                   source_token: str | None = None,
                   labels: dict[str, str] | None = None,
                   status: Any = None) -> int:
    """Add wallets, seed their current positions, and report what happened.

    Insertion happens first and seeding second, in one concurrent pass per chain --
    seeding a harvested cohort one wallet at a time would take minutes.
    """
    chat_id = update.effective_chat.id
    labels = labels or {}
    added: list[tuple[str, str]] = []
    duplicate: list[str] = []
    by_chain: dict[str, list[str]] = {}

    for address in addresses:
        kind = detect_chain(address)
        if kind is None:
            continue
        primary = "solana" if kind == "solana" else (evm_chain or "ethereum")
        # One label across a batch would be ambiguous, so only a single add gets it.
        this_label = labels.get(address) or (label if len(addresses) == 1 else None)

        if not await db.add_wallet(
                conn(context), chat_id, primary, address, this_label, source_token):
            duplicate.append(address)
            continue
        added.append((primary, address))
        by_chain.setdefault(primary, []).append(address)

        # An EVM address is the same person on every EVM chain, so watch it
        # everywhere rather than only where it was first spotted. Solana keys
        # have no equivalent elsewhere, so they are never expanded.
        if kind == "evm" and CFG.multichain:
            for extra in CFG.multichain_chains:
                if extra == primary:
                    continue
                if await db.add_wallet(conn(context), chat_id, extra, address,
                                       this_label, source_token, auto=True):
                    by_chain.setdefault(extra, []).append(address)

    if not added and not duplicate:
        await send(update, "No valid addresses in that.")
        return 0

    # Seed the position snapshot so the first alert is a genuinely new buy. A wallet
    # that fails here is left unseeded on purpose: the watcher seeds it silently on
    # its first pass rather than alerting on a month of history.
    seeded_total, failed = 0, 0
    for chain, wallets in by_chain.items():
        if status:
            await status(f"Snapshotting {len(wallets)} {chain} wallet(s) so you only "
                         f"get alerted on what changes from here...")
        try:
            snapshots = await az(context).holdings(chain, wallets)
        except Exception:
            log.exception("seeding failed for %s", chain)
            failed += len(wallets)
            continue
        for wallet, holdings in snapshots.items():
            # Balances, not just token names: without amounts the first watcher pass
            # cannot tell a sale from a position it has simply never seen.
            await db.save_snapshot(
                conn(context), chain, wallet, {h.token: h.amount for h in holdings})
            await db.record_probe(conn(context), chat_id, chain, wallet,
                                  bool(holdings), CFG.multichain_dormant_after)
            seeded_total += len(holdings)

    lines = []
    if added:
        lines.append(f"<b>Now tracking {len(added)} wallet(s)</b>")
        for chain, address in added[:12]:
            name = labels.get(address) or (label if len(added) == 1 else None)
            suffix = f" - <b>{render.esc(name)}</b>" if name else ""
            lines.append(f"- <code>{short(address, 6, 6)}</code> ({chain}){suffix}")
        if len(added) > 12:
            lines.append(f"...and {len(added) - 12} more")
        lines.append(f"\nSnapshotted {seeded_total} existing position(s).")
        if failed:
            lines.append(f"{failed} wallet(s) could not be snapshotted now - they will "
                         f"be caught up on the next watcher pass.")
    if duplicate:
        lines.append("\nAlready tracked: "
                     + ", ".join(f"<code>{short(a, 4, 4)}</code>"
                                 for a in duplicate[:10]))

    settings = await db.get_settings(conn(context), chat_id)
    if added and not (settings and settings["watch"]):
        lines.append("\nAlerts are off. Turn them on with /watch on.")
    await send(update, render.clip("\n".join(lines)))
    return len(added)


@restricted
@busy_guard
async def cmd_harvest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Steps 2-4 on a coin, then track every wallet that survives the filters."""
    if not context.args:
        await send(update, (
            "Usage: <code>/harvest &lt;token&gt; [n]</code>\n\n"
            "Runs the early-buyer scan on a coin and adds every surviving wallet to "
            "your tracking list in one go."
        ))
        return
    token = context.args[0]
    limit = CFG.early_buyer_count
    if len(context.args) > 1 and context.args[1].isdigit():
        limit = max(5, min(50, int(context.args[1])))

    message = await update.effective_message.reply_text("Resolving token...")
    progress = Progress(message)
    try:
        res = await az(context).find_smart_wallets(token, progress, limit)
    except ValueError as exc:
        await message.edit_text(str(exc))
        return
    except Exception as exc:
        log.exception("harvest failed")
        await message.edit_text(f"Harvest failed: {exc}")
        return

    if res.error:
        await message.edit_text(res.error)
        return
    if not res.survivors:
        await message.edit_text(
            f"{len(res.early_buyers)} early buyers, but {res.dead_count} are dead and "
            f"{res.bot_count} are bots. Nothing left to track - try another coin."
        )
        return

    symbol = (res.market.symbol if res.market else "") or token[:6]
    wallets = [p.address for p in res.survivors]
    await progress(
        f"{len(res.early_buyers)} early buyers -> {res.dead_count} dead, "
        f"{res.bot_count} bots. Adding {len(wallets)} survivors..."
    )
    added = await do_track(
        update, context, wallets,
        evm_chain=res.chain, source_token=token,
        labels=harvest_labels(symbol, wallets), status=progress,
    )
    try:
        await message.delete()
    except BadRequest:
        pass
    if added:
        await send(update, (
            f"Harvested <b>{render.esc(symbol)}</b>. Use /scan to see what these "
            f"wallets are already crowding into, or /watch on for live alerts."
        ))


@restricted
async def cmd_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await send(update, "Usage: <code>/label &lt;wallet&gt; &lt;name&gt;</code>")
        return
    updated = await db.set_label(
        conn(context), update.effective_chat.id, context.args[0],
        " ".join(context.args[1:]))
    await send(update, "Renamed." if updated else "That wallet is not tracked.")


# ------------------------------------------------------- guided add (conversation)
def chain_keyboard(prefix: str, address: str) -> InlineKeyboardMarkup:
    chains = list(EVM_CHAINS)
    rows = [
        [InlineKeyboardButton(c.capitalize(), callback_data=f"{prefix}|{c}|{address}")
         for c in chains[i:i + 3]]
        for i in range(0, len(chains), 3)
    ]
    return InlineKeyboardMarkup(rows)


@restricted
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Guided manual add, so nobody has to remember the /track argument order."""
    context.user_data.pop("pending", None)
    if context.args:
        addresses = extract_addresses(" ".join(context.args))
        if addresses:
            return await _stage_addresses(update, context, addresses)
    await send(update, (
        "Paste the wallet address you want to track.\n\n"
        "You can paste several at once (one per line, or separated by commas) and an "
        "explorer URL works too. /cancel to stop."
    ))
    return ASK_ADDRESS


async def on_add_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    addresses = extract_addresses(update.effective_message.text or "")
    if not addresses:
        await send(update, (
            "I could not find an address in that. Paste a Solana or EVM wallet "
            "address, or /cancel."
        ))
        return ASK_ADDRESS
    return await _stage_addresses(update, context, addresses)


async def _stage_addresses(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           addresses: list[str]) -> int:
    context.user_data["pending"] = addresses
    if any(detect_chain(a) == "evm" for a in addresses):
        await send(
            update,
            f"Found {len(addresses)} address(es). Which chain is that EVM wallet on?",
            reply_markup=chain_keyboard("wch", "x"),
        )
        return ASK_CHAIN
    return await _ask_label(update, context, len(addresses))


async def _ask_label(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     count: int) -> int:
    if count > 1:
        await do_track(update, context, context.user_data.pop("pending", []),
                       None, context.user_data.pop("chain", None))
        return ConversationHandler.END
    await send(update, (
        "Give it a name so alerts are readable - "
        "<i>e.g. \"early BONK whale\"</i>.\n/skip to leave it unnamed."
    ))
    return ASK_LABEL


async def on_add_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) < 2:
        return ASK_CHAIN
    context.user_data["chain"] = parts[1]
    await query.edit_message_text(f"Chain: {parts[1]}")
    return await _ask_label(update, context, len(context.user_data.get("pending", [])))


async def on_add_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    label = None if text.startswith("/skip") else text[:64]
    await do_track(update, context, context.user_data.pop("pending", []),
                   label, context.user_data.pop("chain", None))
    return ConversationHandler.END


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("pending", None)
    context.user_data.pop("chain", None)
    await send(update, "Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------- pasted addresses
@restricted
async def on_loose_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A bare address in the chat is ambiguous - ask rather than guess."""
    addresses = extract_addresses(update.effective_message.text or "")
    if not addresses:
        return
    if len(addresses) > 1:
        context.user_data["pending"] = addresses
        buttons = [[InlineKeyboardButton(
            f"Track all {len(addresses)} as wallets", callback_data="addall")]]
        await send(update, f"Found {len(addresses)} addresses.",
                   reply_markup=InlineKeyboardMarkup(buttons))
        return

    address = addresses[0]
    is_evm = detect_chain(address) == "evm"
    buttons = [
        [InlineKeyboardButton(
            "Track as wallet",
            callback_data=("addc|" if is_evm else "add|solana|") + address)],
        [InlineKeyboardButton("Analyze as token", callback_data=f"tok|{address}")],
    ]
    await send(update, (
        f"<code>{render.esc(address)}</code>\n\nWhat is this?"
    ), reply_markup=InlineKeyboardMarkup(buttons))


@restricted
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    action = parts[0]

    if action == "trk" and len(parts) == 3:
        await do_track(update, context, [parts[2]], None, parts[1])
    elif action == "add" and len(parts) == 3:
        await do_track(update, context, [parts[2]], None, parts[1])
    elif action == "addc" and len(parts) == 2:
        # An EVM address alone does not say which chain, so let them pick.
        await query.edit_message_text(
            "Which chain is this wallet on?",
            reply_markup=chain_keyboard("add", parts[1]),
        )
    elif action == "addall":
        await do_track(update, context, context.user_data.pop("pending", []), None)
    elif action == "harvall":
        cohort = context.user_data.pop("cohort", None)
        if not cohort:
            await send(update,
                       "That scan has expired. Run <code>/harvest &lt;token&gt;</code>.")
            return
        wallets = cohort["wallets"]
        await do_track(
            update, context, wallets,
            evm_chain=cohort["chain"], source_token=cohort["token"],
            labels=harvest_labels(cohort["symbol"], wallets),
        )
    elif action == "tok" and len(parts) == 2:
        context.args = [parts[1]]
        await cmd_analyze(update, context)


@restricted
async def cmd_untrack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send(update, "Usage: <code>/untrack &lt;wallet&gt;</code>")
        return
    removed = await db.remove_wallet(
        conn(context), update.effective_chat.id, context.args[0])
    await send(update, "Removed." if removed else "That wallet was not tracked.")


@restricted
async def cmd_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await db.list_wallets(conn(context), update.effective_chat.id)
    if not rows:
        await send(update, "No wallets tracked yet. Start with /wallets &lt;token&gt;.")
        return
    # One entry per wallet, not one per chain: an EVM wallet is watched on every
    # chain, and listing eight rows for it would bury how many people you follow.
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = grouped.setdefault(r["address"], {
            "label": r["label"], "source": r["source_token"],
            "live": [], "parked": [], "home": r["chain"],
        })
        entry["label"] = entry["label"] or r["label"]
        (entry["parked"] if r["dormant"] else entry["live"]).append(r["chain"])
        if not r["auto"]:
            entry["home"] = r["chain"]

    lines = [f"<b>Tracking {len(grouped)} wallet(s)</b>\n"]
    for address, entry in grouped.items():
        name = render.esc(entry["label"]) if entry["label"] else short(address, 6, 6)
        origin = (f" via {short(entry['source'], 4, 4)}" if entry["source"] else "")
        where = ", ".join(sorted(entry["live"])) or "no activity found yet"
        lines.append(
            f'- <a href="{az(context).explorer_wallet(entry["home"], address)}">'
            f"{name}</a> <code>{short(address, 4, 4)}</code>{origin}")
        line = f"   active on: {where}"
        if entry["parked"]:
            line += f" (+{len(entry['parked'])} quiet)"
        lines.append(line)
    settings = await db.get_settings(conn(context), update.effective_chat.id)
    watching = bool(settings and settings["watch"])
    lines.append(f"\nAlerts: <b>{'ON' if watching else 'OFF'}</b> (/watch on|off)")
    await send(update, render.clip("\n".join(lines)))


async def run_overlap_scan(database, analyzer: Analyzer, wallets_by_chain: dict,
                           progress=None) -> dict[str, list]:
    """Steps 5-7 across a set of wallets. Shared by /scan and the autoscan job
    so the timer can never drift from what the command does."""
    results: dict[str, list] = {}
    for chain, wallets in wallets_by_chain.items():
        if progress:
            await progress(f"Scanning {len(wallets)} {chain} wallets...")
        try:
            buys = await analyzer.recent_buys(chain, wallets)
            results[chain] = await analyzer.overlap_and_score(chain, buys)
        except Exception:
            log.exception("scan failed for %s", chain)
            results[chain] = []
    return results


def wallets_by_chain(rows) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["chain"], []).append(r["address"])
    return out


@restricted
@busy_guard
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await db.list_wallets(conn(context), update.effective_chat.id)
    if not rows:
        await send(update, "Nothing to scan. Track some wallets first.")
        return
    status = await update.effective_message.reply_text(
        f"Scanning {len(rows)} wallets for overlapping buys...")

    analyzer = az(context)
    results = await run_overlap_scan(
        conn(context), analyzer, wallets_by_chain(rows), Progress(status))

    blocks: list[str] = []
    for chain, cands in results.items():
        if not cands:
            blocks.append(
                f"<b>{chain}</b>: no token repeats across "
                f"{CFG.overlap_min_wallets}+ of these wallets.")
            continue
        blocks.append(f"<b>{chain} - overlapping buys</b>\n")
        for i, cand in enumerate(cands[:6], 1):
            blocks.append(render.render_candidate(cand, analyzer, i) + "\n")

    await status.delete()
    await send(update, render.clip("\n".join(blocks)))


@restricted
async def cmd_autoscan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run /scan on a timer, reporting only what changed."""
    arg = (context.args[0].lower() if context.args else "")
    chat_id = update.effective_chat.id
    if arg not in ("on", "off"):
        settings = await db.get_settings(conn(context), chat_id)
        on = bool(settings and settings["autoscan"])
        await send(update, (
            f"Auto-scan is <b>{'ON' if on else 'OFF'}</b>, every "
            f"{CFG.autoscan_hours:g}h.\n\n"
            "Turn it on with <code>/autoscan on</code>. It runs the same steps 5-7 "
            "as /scan, but only messages you when a token reaches the overlap bar "
            "for the first time, picks up another wallet, or its score moves.\n\n"
            "<i>So silence means nothing changed, not that nothing ran. Change the "
            "interval with AUTOSCAN_INTERVAL_HOURS in .env.</i>"
        ))
        return
    await db.set_flag(conn(context), chat_id, "autoscan", arg == "on")
    if arg == "on":
        await send(update, (
            f"Auto-scan <b>ON</b> - every {CFG.autoscan_hours:g}h across every wallet "
            f"you track.\n\nYou will only hear about changes. First run in a minute."
        ))
    else:
        await send(update, "Auto-scan <b>OFF</b>. /scan still works on demand.")


@restricted
async def cmd_autoharvest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0].lower() if context.args else "")
    chat_id = update.effective_chat.id
    if arg not in ("on", "off"):
        settings = await db.get_settings(conn(context), chat_id)
        on = bool(settings and settings["autoharvest"])
        pending = [r for r in await db.queue_list(conn(context), chat_id)
                   if r["status"] == "pending"]
        await send(update, (
            f"Auto-harvest is <b>{'ON' if on else 'OFF'}</b>, one coin every "
            f"{CFG.autoharvest_hours:g}h. Queue: <b>{len(pending)}</b> pending.\n\n"
            "It works through /queue, mining each coin for early buyers and "
            "tracking the survivors automatically.\n\n"
            "<i>One coin per cycle on purpose: early-buyer lookups are the most "
            "expensive thing this bot does, and a queue of twenty run at once "
            "would rate limit you out.</i>"
        ))
        return
    await db.set_flag(conn(context), chat_id, "autoharvest", arg == "on")
    await send(update, (
        f"Auto-harvest <b>ON</b> - one queued coin every {CFG.autoharvest_hours:g}h."
        if arg == "on" else "Auto-harvest <b>OFF</b>."
    ))


@restricted
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The list of past runners waiting to be mined for early buyers."""
    chat_id = update.effective_chat.id
    if not context.args:
        rows = await db.queue_list(conn(context), chat_id)
        await send(update, render.render_queue(rows, az(context)))
        return

    if context.args[0].lower() == "clear":
        removed = await db.queue_clear(conn(context), chat_id, only_done=True)
        await send(update, f"Cleared {removed} finished entr(ies).")
        return

    added, skipped = 0, 0
    for token in extract_addresses(" ".join(context.args)):
        if await db.queue_add(conn(context), chat_id, token, None, "manual"):
            added += 1
        else:
            skipped += 1
    if not added and not skipped:
        await send(update, "No valid token address in that. "
                           "Usage: <code>/queue &lt;token&gt;</code>")
        return
    note = f"{added} queued" + (f", {skipped} already there" if skipped else "")
    await send(update, (
        f"{note}. Turn on <code>/autoharvest on</code> to work through them, or "
        f"<code>/harvest &lt;token&gt;</code> to do one right now."
    ))


@restricted
@busy_guard
async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boosted tokens to pick from. Defaults to Solana; /trending base for EVM."""
    chain = (context.args[0].lower() if context.args else "solana")
    if chain not in EVM_CHAINS and chain != "solana":
        await send(update, (
            "Usage: <code>/trending [chain]</code>\n"
            f"Chains: solana, {', '.join(sorted(EVM_CHAINS))}"
        ))
        return
    status = await update.effective_message.reply_text(
        f"Pulling the boosted list on {chain}...")
    try:
        markets = await az(context).dex.boosted(chain)
    except Exception as exc:
        log.exception("trending failed")
        await status.edit_text(f"Could not reach DexScreener: {exc}")
        return
    await status.delete()
    await send(update, render.render_trending(markets, az(context), chain))


@restricted
async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rank tracked wallets by how their calls to you actually performed."""
    chat_id = update.effective_chat.id
    rows = await db.outcomes_for_chat(conn(context), chat_id)
    tracked = await db.list_wallets(conn(context), chat_id)
    labels = {r["address"]: r["label"] for r in tracked if r["label"]}

    cards = build_scorecards(rows, labels)
    summary = summarise(cards)
    await send(update, render.render_leaderboard(cards, summary, az(context)))

    dead = stale_wallets(cards)
    weak = underperformers(cards)
    if dead or weak:
        lines = ["<b>Worth pruning</b>"]
        for card in weak[:5]:
            who = render.esc(card.label or short(card.wallet, 5, 5))
            lines.append(f"  {who} - {card.hits}/{card.signals}, "
                         f"avg peak {render.multiple(card.avg_peak)}")
        for card in dead[:5]:
            who = render.esc(card.label or short(card.wallet, 5, 5))
            lines.append(f"  {who} - silent since {render.ago(card.last_signal_ts)}")
        lines.append("\n<i>Step 3 of the method is dropping the dead ones, and a "
                     "wallet you tracked last month can die like any other. "
                     "/untrack when you agree.</i>")
        await send(update, render.clip("\n".join(lines)))


@restricted
async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        await send(update, "Usage: <code>/watch on</code> or <code>/watch off</code>")
        return
    await db.set_watch(conn(context), update.effective_chat.id, arg == "on")
    await send(
        update,
        f"Alerts <b>{arg.upper()}</b>. Checking every {CFG.watcher_interval_min} minutes."
        if arg == "on" else "Alerts <b>OFF</b>.",
    )


@restricted
async def cmd_minscore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send(update, "Usage: <code>/minscore 7.5</code>")
        return
    try:
        value = max(0.0, min(10.0, float(context.args[0])))
    except ValueError:
        await send(update, "Give me a number between 0 and 10.")
        return
    await db.set_min_score(conn(context), update.effective_chat.id, value)
    await send(update, f"Only alerting at <b>{value}/10</b> and above.")


@restricted
async def cmd_bankroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        settings = await db.get_settings(conn(context), update.effective_chat.id)
        current = (settings["bankroll"] if settings and settings["bankroll"]
                   else CFG.bankroll_usd)
        await send(update, (
            f"Bankroll: <b>{render.usd(current)}</b>\n\n"
            "Set it with <code>/bankroll 2000</code>. It is used to translate a "
            "wallet's conviction into a position size for your account - "
            "they risk 8% of their book, you risk 8% of yours."
        ))
        return
    raw = context.args[0].replace("$", "").replace(",", "").lower()
    multiplier = 1000 if raw.endswith("k") else 1
    try:
        amount = float(raw.rstrip("k")) * multiplier
    except ValueError:
        await send(update, "Give me a number, like <code>/bankroll 2000</code>.")
        return
    if amount < 0:
        await send(update, "That has to be positive.")
        return
    await db.set_bankroll(conn(context), update.effective_chat.id, amount)
    await send(update, (
        f"Bankroll set to <b>{render.usd(amount)}</b>. Alerts will now show what "
        f"each wallet's conviction is worth on your account."
    ))


@restricted
async def cmd_sells(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        await send(update, "Usage: <code>/sells on</code> or <code>/sells off</code>")
        return
    await db.set_sell_alerts(conn(context), update.effective_chat.id, arg == "on")
    await send(update, (
        "Exit alerts <b>ON</b>. You will hear when a tracked wallet trims or closes "
        "a position - after the fact, but before you find out from the chart."
        if arg == "on" else "Exit alerts <b>OFF</b>. Entries only."
    ))


@restricted
@busy_guard
async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """What one wallet is holding and how much of its book each position is."""
    if not context.args:
        await send(update, "Usage: <code>/wallet &lt;address&gt;</code>")
        return
    address = context.args[0]
    kind = detect_chain(address)
    if kind is None:
        await send(update, "That is not a valid Solana or EVM address.")
        return
    chain_arg = next((a for a in context.args[1:] if a.lower() in EVM_CHAINS), None)
    chain = "solana" if kind == "solana" else (chain_arg or "ethereum")

    status = await update.effective_message.reply_text("Pricing their book...")
    try:
        portfolio = await az(context).portfolio(chain, address)
    except Exception as exc:
        log.exception("wallet failed")
        await status.edit_text(f"Could not read that wallet: {exc}")
        return

    rows = await db.list_wallets(conn(context), update.effective_chat.id)
    label = next((r["label"] for r in rows
                  if r["address"].lower() == address.lower()), None)
    await status.delete()
    await send(update, render.render_portfolio(portfolio, az(context), label))


@restricted
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = await db.get_settings(conn(context), update.effective_chat.id)
    min_score = (settings["min_score"] if settings and settings["min_score"] is not None
                 else 6.0)
    bankroll = (settings["bankroll"] if settings and settings["bankroll"]
                else CFG.bankroll_usd)
    sell_alerts = not settings or settings["sell_alerts"]
    autoscan = bool(settings and settings["autoscan"])
    autoharvest = bool(settings and settings["autoharvest"])
    sources = []
    sources.append("Solscan Pro" if CFG.solscan_key else None)
    sources.append("Helius" if CFG.helius_key else None)
    sources.append("Etherscan V2" if CFG.etherscan_key else None)
    active = ", ".join(s for s in sources if s) or "public RPC only (slow)"
    await send(update, (
        "<b>Settings</b>\n"
        f"Early buyers pulled: {CFG.early_buyer_count}\n"
        f"Active window: {CFG.active_window_days}d\n"
        f"Recent-buy window: {CFG.recent_buy_window_days}d\n"
        f"Overlap threshold: {CFG.overlap_min_wallets} wallets\n"
        f"Buy bar: {CFG.score_buy_threshold}/10\n"
        f"Bot filter: median gap &lt; {CFG.bot_min_median_gap_sec}s "
        f"or &gt; {CFG.bot_max_tx_per_day:.0f} tx/day\n"
        f"Alert threshold: {min_score}/10\n"
        f"Bankroll: {render.usd(bankroll)}\n"
        f"Exit alerts: {'ON' if sell_alerts else 'OFF'}\n\n"
        "<b>Schedules</b>\n"
        f"Watcher (entries + exits): every {CFG.watcher_interval_min}m\n"
        f"Auto-scan: {'ON' if autoscan else 'OFF'}, "
        f"every {CFG.autoscan_hours:g}h at {CFG.autoscan_min_score}/10+\n"
        f"Auto-harvest: {'ON' if autoharvest else 'OFF'}, "
        f"one coin every {CFG.autoharvest_hours:g}h\n"
        f"Outcome re-pricing: every {CFG.outcome_check_min}m "
        f"for {CFG.outcome_track_hours:g}h per call\n"
        f"Leaderboard win bar: {CFG.outcome_win_multiple:g}x peak\n\n"
        f"Data sources: {active}\n\n"
        "<i>Change these in .env and restart.</i>"
    ))


# ------------------------------------------------------------------- watcher job
async def watcher_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diff every tracked wallet's balances against the last snapshot and report."""
    database = context.application.bot_data["db"]
    analyzer: Analyzer = context.application.bot_data["analyzer"]
    await db.cache_sweep(database)

    for chat_id in await db.all_watching_chats(database):
        # Live wallet-chain pairs, plus a few parked ones due a retry. Chains a
        # wallet has never touched stop costing anything after the first passes,
        # which is what makes watching every chain at once affordable.
        rows = await db.wallets_to_poll(
            database, chat_id,
            reprobe_after_seconds=int(CFG.multichain_reprobe_hours * 3600),
            probe_budget=CFG.multichain_probes_per_cycle,
        )
        if not rows:
            continue
        settings = await db.get_settings(database, chat_id)
        min_score = (settings["min_score"]
                     if settings and settings["min_score"] is not None else 6.0)
        bankroll = (settings["bankroll"] if settings and settings["bankroll"] else
                    CFG.bankroll_usd)
        sell_alerts = not settings or settings["sell_alerts"]
        labels = {(r["chain"], r["address"]): r["label"] for r in rows}

        by_chain: dict[str, list[str]] = {}
        for r in rows:
            by_chain.setdefault(r["chain"], []).append(r["address"])

        for chain, wallets in by_chain.items():
            try:
                snapshots = await analyzer.holdings(chain, wallets)
            except Exception:
                log.exception("watcher: holdings failed for %s", chain)
                continue

            for wallet, holdings in snapshots.items():
                balances = {h.token: h.amount for h in holdings}
                previous = await db.position_snapshot(database, chain, wallet)

                # Park chains this wallet shows no history on, and wake them the
                # moment it does. Done before the seeding check so a re-probe
                # that finds activity brings the pair straight back to life.
                await db.record_probe(database, chat_id, chain, wallet,
                                      bool(holdings), CFG.multichain_dormant_after)

                if not await db.has_snapshot(database, chain, wallet):
                    # First sighting: record where they stand, never alert on it.
                    await db.save_snapshot(database, chain, wallet, balances)
                    continue
                if looks_like_read_failure(previous, balances):
                    log.warning("watcher: empty balances for %s, skipping this pass",
                                wallet)
                    continue

                events = diff_positions(previous, balances)
                await db.save_snapshot(database, chain, wallet, balances)

                for event in events[:6]:
                    if event.is_exit and not sell_alerts:
                        continue
                    if not await db.should_alert(
                            database, chat_id, chain, wallet, event.token, event.kind):
                        continue
                    await _send_event(context, chat_id, analyzer, chain, wallet,
                                      event, holdings, bankroll, min_score,
                                      labels.get((chain, wallet)))
                    await asyncio.sleep(0.5)


async def _send_event(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                      analyzer: Analyzer, chain: str, wallet: str, event,
                      holdings, bankroll: float, min_score: float,
                      label: str | None) -> None:
    try:
        cand, _ = await analyzer.score_one(event.token, overlap_wallets=1)
    except Exception:
        log.exception("watcher: scoring %s failed", event.token)
        return

    # A weak score is a reason not to buy, never a reason to hide that the wallet
    # you follow is getting out.
    if not event.is_exit and (not cand.score or cand.score.total < min_score):
        return

    cand.wallets = [wallet]
    sizing = None
    try:
        portfolio = await analyzer.portfolio(chain, wallet, holdings)
        sizing = size_for_you(portfolio, event.token, bankroll)
    except Exception:
        log.exception("watcher: sizing %s failed", wallet)

    try:
        await context.bot.send_message(
            chat_id,
            render.render_alert(cand, wallet, analyzer, label, event, sizing),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("watcher: send failed")
        return

    # Grade the call from here. Only entries are tracked: an exit alert has no
    # "did it go up" to measure, and the entry it closes was already recorded.
    if not event.is_exit and cand.market and cand.market.price_usd > 0:
        try:
            await db.record_outcome(
                context.application.bot_data["db"], chat_id, chain, wallet,
                event.token, cand.symbol, event.kind,
                cand.score.total if cand.score else None,
                cand.market.price_usd,
            )
        except Exception:
            log.exception("watcher: could not record outcome for %s", event.token)


# ------------------------------------------------------------- automatic scanning
async def autoscan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Steps 5-7 on a timer. Reports only what moved since the last run.

    An interval scan that re-sent the same overlap every cycle would train you
    to ignore it, so a token has to be new, hold one more wallet than before, or
    have moved half a point in score before it is worth a message.
    """
    database = context.application.bot_data["db"]
    analyzer: Analyzer = context.application.bot_data["analyzer"]

    for chat_id in await db.chats_with(database, "autoscan"):
        rows = await db.list_wallets(database, chat_id)
        if not rows:
            continue
        try:
            results = await run_overlap_scan(
                database, analyzer, wallets_by_chain(rows))
        except Exception:
            log.exception("autoscan: scan failed for chat %s", chat_id)
            continue

        changes: list[tuple[str, Any, str]] = []
        for chain, cands in results.items():
            for cand in cands[:8]:
                score = cand.score.total if cand.score else 0.0
                if score < CFG.autoscan_min_score:
                    continue
                try:
                    verdict = await db.scan_delta(
                        database, chat_id, chain, cand.token,
                        len(cand.wallets), score)
                except Exception:
                    log.exception("autoscan: delta failed")
                    continue
                if verdict:
                    changes.append((chain, cand, verdict))

        if not changes:
            log.info("autoscan: nothing changed for chat %s", chat_id)
            continue
        try:
            await context.bot.send_message(
                chat_id,
                render.render_scan_digest(changes[:6], analyzer),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("autoscan: send failed")


async def autoharvest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mine one queued coin per cycle for early buyers and track the survivors.

    One per cycle is deliberate. Early-buyer lookups are the most expensive call
    the bot makes, and firing a whole queue at once is the fastest way to get
    rate limited off the free tiers.
    """
    database = context.application.bot_data["db"]
    analyzer: Analyzer = context.application.bot_data["analyzer"]

    for chat_id in await db.chats_with(database, "autoharvest"):
        entry = await db.queue_next(database, chat_id)
        if not entry:
            continue
        token = entry["token"]
        try:
            res = await analyzer.find_smart_wallets(
                token, limit=CFG.early_buyer_count)
        except Exception as exc:
            log.exception("autoharvest: %s failed", token)
            await db.queue_mark(database, chat_id, token, "failed", str(exc)[:120])
            continue

        if res.error:
            await db.queue_mark(database, chat_id, token, "failed", res.error[:120])
            try:
                await context.bot.send_message(
                    chat_id,
                    f"Auto-harvest skipped <code>{render.esc(short(token, 5, 5))}</code>: "
                    f"{render.esc(res.error)}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                log.exception("autoharvest: notice failed")
            continue

        symbol = (res.market.symbol if res.market else "") or token[:6]
        survivors = res.survivors[:CFG.autoharvest_max_wallets]
        labels = harvest_labels(symbol, len(survivors))
        added = 0
        for prof, label in zip(survivors, labels):
            if await db.add_wallet(database, chat_id, res.chain, prof.address,
                                   label, token):
                added += 1

        await db.queue_mark(
            database, chat_id, token, "done",
            f"{len(res.survivors)} survivors, {added} new")

        try:
            await context.bot.send_message(
                chat_id,
                (f"<b>Auto-harvest: {render.esc(symbol)}</b>\n"
                 f"{len(res.early_buyers)} early buyers -> {res.dead_count} dead, "
                 f"{res.bot_count} bots, <b>{len(res.survivors)} survivors</b>\n"
                 f"Tracking <b>{added}</b> new wallet(s).\n\n"
                 f"<i>The next watcher pass snapshots what they already hold "
                 f"without alerting, so the first thing you hear from them is a "
                 f"genuinely new buy rather than a replay of last month.</i>"),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            log.exception("autoharvest: send failed")


async def outcome_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-price every open alert so the leaderboard has something real to rank.

    Peak is carried forward rather than recomputed, because a coin that went 4x
    and came back to flat was a good call badly managed -- grading it purely on
    where it sits now would tell you to stop following a wallet that keeps
    handing you 4x.
    """
    database = context.application.bot_data["db"]
    analyzer: Analyzer = context.application.bot_data["analyzer"]

    rows = await db.open_outcomes(database)
    if not rows:
        return

    cutoff = time.time() - CFG.outcome_track_hours * 3600
    by_token = {r["token"] for r in rows}
    try:
        markets = await analyzer.dex.markets(list(by_token))
    except Exception:
        log.exception("outcome: pricing failed")
        return

    for row in rows:
        market = markets.get(row["token"]) or markets.get(row["token"].lower())
        expired = row["opened_at"] < cutoff
        if not market or market.price_usd <= 0:
            # No price now does not mean zero -- it usually means the pair went
            # illiquid. Close it out at the last price we trusted rather than
            # recording a fake wipeout.
            if expired:
                await db.update_outcome(database, row["id"], row["last_price"],
                                        row["peak_price"], closed=True)
            continue
        price = market.price_usd
        peak = max(row["peak_price"], price)
        await db.update_outcome(database, row["id"], price, peak, closed=expired)


# ------------------------------------------------------------------------ wiring
async def _post_init(app: Application) -> None:
    app.bot_data["db"] = await db.connect()
    app.bot_data["analyzer"] = Analyzer()
    log.info("memehunter ready")


async def _post_shutdown(app: Application) -> None:
    analyzer = app.bot_data.get("analyzer")
    if analyzer:
        await analyzer.aclose()
    database = app.bot_data.get("db")
    if database:
        await database.close()


def build_app() -> Application:
    app = (ApplicationBuilder()
           .token(CFG.telegram_token)
           .post_init(_post_init)
           .post_shutdown(_post_shutdown)
           .build())

    # Registered first so an in-progress /add wizard captures text before the
    # loose-address handler sees it.
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            ASK_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                         on_add_address)],
            ASK_CHAIN: [CallbackQueryHandler(on_add_chain, pattern=r"^wch\|")],
            ASK_LABEL: [
                CommandHandler("skip", on_add_label),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_add_label),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=300,
        per_message=False,
    ))

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("wallets", cmd_wallets))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(CommandHandler("harvest", cmd_harvest))
    app.add_handler(CommandHandler("untrack", cmd_untrack))
    app.add_handler(CommandHandler("tracked", cmd_tracked))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("autoscan", cmd_autoscan))
    app.add_handler(CommandHandler("autoharvest", cmd_autoharvest))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler(["leaderboard", "board"], cmd_leaderboard))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("sells", cmd_sells))
    app.add_handler(CommandHandler("bankroll", cmd_bankroll))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("minscore", cmd_minscore))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("label", cmd_label))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_button))
    # Last: anything else that looks like an address gets the what-is-this prompt.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_loose_address))

    if app.job_queue:
        # Staggered starts so a restart does not fire every job in the same
        # second and rate limit the providers out of the gate.
        app.job_queue.run_repeating(
            watcher_job,
            interval=CFG.watcher_interval_min * 60,
            first=60,
            name="watcher",
        )
        app.job_queue.run_repeating(
            outcome_job,
            interval=CFG.outcome_check_min * 60,
            first=180,
            name="outcomes",
        )
        app.job_queue.run_repeating(
            autoscan_job,
            interval=CFG.autoscan_hours * 3600,
            first=300,
            name="autoscan",
        )
        app.job_queue.run_repeating(
            autoharvest_job,
            interval=CFG.autoharvest_hours * 3600,
            first=600,
            name="autoharvest",
        )
    else:
        log.warning(
            "No job queue available - install python-telegram-bot[job-queue]. "
            "Every automatic feature (watcher, autoscan, autoharvest, "
            "leaderboard grading) is disabled without it."
        )
    return app
