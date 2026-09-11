"""Telegram front end: commands, buttons, and the background watcher job."""
from __future__ import annotations

import asyncio
import logging
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
        chain = "solana" if kind == "solana" else (evm_chain or "ethereum")
        # One label across a batch would be ambiguous, so only a single add gets it.
        this_label = labels.get(address) or (label if len(addresses) == 1 else None)
        if not await db.add_wallet(
                conn(context), chat_id, chain, address, this_label, source_token):
            duplicate.append(address)
            continue
        added.append((chain, address))
        by_chain.setdefault(chain, []).append(address)

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
    lines = [f"<b>Tracking {len(rows)} wallet(s)</b>\n"]
    for r in rows:
        name = render.esc(r["label"]) if r["label"] else short(r["address"], 6, 6)
        origin = (f" via {short(r['source_token'], 4, 4)}"
                  if r["source_token"] else "")
        lines.append(
            f'- <a href="{az(context).explorer_wallet(r["chain"], r["address"])}">'
            f"{name}</a> <code>{short(r['address'], 4, 4)}</code> "
            f"({r['chain']}{origin})")
    settings = await db.get_settings(conn(context), update.effective_chat.id)
    watching = bool(settings and settings["watch"])
    lines.append(f"\nAlerts: <b>{'ON' if watching else 'OFF'}</b> (/watch on|off)")
    await send(update, render.clip("\n".join(lines)))


@restricted
@busy_guard
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await db.list_wallets(conn(context), update.effective_chat.id)
    if not rows:
        await send(update, "Nothing to scan. Track some wallets first.")
        return
    status = await update.effective_message.reply_text(
        f"Scanning {len(rows)} wallets for overlapping buys...")

    by_chain: dict[str, list[str]] = {}
    for r in rows:
        by_chain.setdefault(r["chain"], []).append(r["address"])

    analyzer = az(context)
    blocks: list[str] = []
    for chain, wallets in by_chain.items():
        await Progress(status)(f"Scanning {len(wallets)} {chain} wallets...")
        try:
            buys = await analyzer.recent_buys(chain, wallets)
            cands = await analyzer.overlap_and_score(chain, buys)
        except Exception as exc:
            log.exception("scan failed")
            blocks.append(f"{chain}: scan failed ({exc})")
            continue
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
        f"Watcher interval: {CFG.watcher_interval_min}m\n"
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
        rows = await db.list_wallets(database, chat_id)
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
        app.job_queue.run_repeating(
            watcher_job,
            interval=CFG.watcher_interval_min * 60,
            first=60,
            name="watcher",
        )
    return app
