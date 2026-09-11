"""Telegram HTML rendering. Everything the user sees is built here."""
from __future__ import annotations

import html
import time

from .config import CFG
from .pipeline.analyzer import Analyzer, Candidate, TokenAnalysis
from .pipeline.events import HEADLINE, PositionEvent
from .pipeline.portfolio import Portfolio, Sizing
from .pipeline.scoring import Score
from .providers.base import WalletProfile, short

MAX_MSG = 3900


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def usd(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    if value >= 0.01 or value <= 0:
        return f"${value:,.2f}"
    # Meme coin prices live well below a cent; keep four significant digits.
    return f"${value:.4g}"


def ago(ts: int) -> str:
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    if delta < 86400:
        return f"{delta / 3600:.0f}h ago"
    return f"{delta / 86400:.0f}d ago"


def clip(text: str) -> str:
    return text if len(text) <= MAX_MSG else text[:MAX_MSG] + "\n...(truncated)"


def score_bar(total: float) -> str:
    filled = int(round(total))
    return "#" * filled + "." * (10 - filled)


def render_score(score: Score) -> str:
    lines = [f"<b>{score.total}/10</b>  <code>{score_bar(score.total)}</code>  "
             f"<b>{score.verdict}</b>"]
    for c in score.components:
        lines.append(f"  {c.points}/{c.maximum}  {esc(c.label)} - <i>{esc(c.note)}</i>")
    if score.flags:
        lines.append("\n<b>Red flags</b>")
        lines.extend(f"  (!) {esc(f)}" for f in score.flags)
    return "\n".join(lines)


def render_candidate(cand: Candidate, az: Analyzer, rank: int | None = None) -> str:
    m, s = cand.market, cand.score
    head = f"{rank}. " if rank else ""
    sym = esc(cand.symbol)
    title = f"{head}<b>{sym}</b>"
    if s:
        title += f" - <b>{s.total}/10</b> {s.verdict}"
    lines = [title]
    if m:
        lines.append(
            f"   {usd(m.price_usd)} | liq {usd(m.liquidity_usd)} | "
            f"vol24h {usd(m.volume_24h)} | {m.age_days:.1f}d old"
        )
        lines.append(f"   24h {m.price_change_24h:+.0f}%  |  FDV {usd(m.fdv)}")
    if cand.wallets:
        lines.append(f"   held by <b>{len(cand.wallets)}</b> tracked wallet(s)")
    lines.append(f"   <code>{esc(cand.token)}</code>")
    links = [f'<a href="{az.explorer_token(cand.chain, cand.token)}">explorer</a>']
    if m and m.pair_url:
        links.append(f'<a href="{m.pair_url}">dexscreener</a>')
    lines.append("   " + " | ".join(links))
    if s and s.flags:
        lines.append("   (!) " + esc("; ".join(s.flags[:2])))
    return "\n".join(lines)


def render_wallet(prof: WalletProfile, az: Analyzer, rank: int) -> str:
    if prof.dead:
        status = "DEAD"
    elif prof.is_bot:
        status = f"BOT ({esc(prof.bot_reason)})"
    else:
        status = "ALIVE"
    return (
        f"{rank}. <a href=\"{az.explorer_wallet(prof.chain, prof.address)}\">"
        f"<code>{short(prof.address, 6, 6)}</code></a> - {status}\n"
        f"   last active {ago(prof.last_active_ts)} | "
        f"{prof.tx_count_30d} tx/{CFG.active_window_days}d"
    )


def render_analysis(res: TokenAnalysis, az: Analyzer) -> str:
    name = esc(res.market.symbol if res.market else res.token[:8])
    lines = [f"<b>Analysis: {name}</b> on {esc(res.chain)}",
             f"<code>{esc(res.token)}</code>", ""]

    if res.error:
        lines.append(f"(!) {esc(res.error)}")
        return clip("\n".join(lines))

    lines.append(
        f"<b>Steps 2-4.</b> {len(res.early_buyers)} early buyers -> "
        f"{res.dead_count} dead, {res.bot_count} bots, "
        f"<b>{len(res.survivors)} survivors</b>"
    )
    for i, prof in enumerate(res.survivors[:8], 1):
        lines.append(render_wallet(prof, az, i))

    if not res.survivors:
        lines.append("\nNothing survived the filter. Try another coin from that cycle.")
        return clip("\n".join(lines))

    lines.append("")
    if not res.candidates:
        lines.append(
            f"<b>Steps 5-7.</b> No token appeared in "
            f"{CFG.overlap_min_wallets}+ of these wallets recently. "
            f"That is a normal outcome - widen the sample with another coin."
        )
        return clip("\n".join(lines))

    lines.append(f"<b>Steps 5-7. Overlapping buys, scored:</b>\n")
    for i, cand in enumerate(res.candidates[:6], 1):
        lines.append(render_candidate(cand, az, i))
        lines.append("")

    buy_grade = [c for c in res.candidates
                 if c.score and c.score.total >= CFG.score_buy_threshold
                 and not c.score.flags]
    if buy_grade:
        names = ", ".join(esc(c.symbol) for c in buy_grade)
        lines.append(f"<b>Clears your {CFG.score_buy_threshold}/10 bar:</b> {names}")
    else:
        lines.append(
            f"<i>Nothing clears {CFG.score_buy_threshold}/10. "
            f"Per your own rule: do not buy.</i>"
        )
    lines.append(
        "\n/track a wallet above to get alerted the moment it opens a new position."
    )
    return clip("\n".join(lines))


def render_sizing(sizing: Sizing) -> str:
    """Their conviction in dollars, and what the same conviction is worth to you."""
    if not sizing.known:
        return "<i>Could not price their portfolio, so no size read on this one.</i>"
    lines = [
        f"<b>Their size</b> {usd(sizing.their_usd)} - "
        f"<b>{sizing.their_share:.1%}</b> of a {usd(sizing.portfolio_usd)} book"
    ]
    if sizing.your_bankroll > 0:
        lines.append(
            f"<b>Same conviction for you</b> {usd(sizing.your_usd)} "
            f"of your {usd(sizing.your_bankroll)}"
        )
    else:
        lines.append("<i>Set /bankroll to see what that conviction is worth on your "
                     "account.</i>")
    if sizing.note:
        lines.append(f"<i>{esc(sizing.note)}</i>")
    return "\n".join(lines)


def render_portfolio(pf: Portfolio, az: Analyzer, label: str | None = None) -> str:
    who = esc(label) if label else short(pf.wallet, 6, 6)
    lines = [
        f'<b><a href="{az.explorer_wallet(pf.chain, pf.wallet)}">{who}</a></b> '
        f"on {esc(pf.chain)}",
        f"<b>{usd(pf.total_usd)}</b> total"
        + (f" ({usd(pf.native_usd)} in {esc(pf.native_symbol)})" if pf.native_usd else ""),
        "",
    ]
    top = pf.top(10)
    if not top:
        lines.append("No token positions found.")
        return clip("\n".join(lines))

    lines.append("<b>Largest positions</b>")
    for position in top:
        share = position.usd / pf.total_usd if pf.total_usd > 0 else 0
        value = usd(position.usd) if position.priced else "unpriced"
        lines.append(f"  {esc(position.symbol)} - {value} ({share:.1%})")
    if pf.unpriced:
        lines.append(f"\n<i>{pf.unpriced} holding(s) have no market price and are "
                     f"excluded from the total.</i>")
    if pf.truncated:
        lines.append(f"<i>Only the first {CFG.max_priced_holdings} holdings "
                     f"were priced.</i>")
    return clip("\n".join(lines))


def render_alert(cand: Candidate, wallet: str, az: Analyzer, label: str | None,
                 event: PositionEvent | None = None,
                 sizing: Sizing | None = None) -> str:
    who = esc(label or short(wallet, 6, 6))
    kind = event.kind if event else "BUY"
    headline = HEADLINE.get(kind, "NEW BUY")
    action = event.describe() if event else "opened a position"

    lines = [f"<b>{headline}</b> - {who} {action}",
             f'<a href="{az.explorer_wallet(cand.chain, wallet)}">wallet</a>', ""]
    lines.append(render_candidate(cand, az))

    if sizing:
        lines.append("")
        lines.append(render_sizing(sizing))

    # On the way out, the quality of the coin is beside the point.
    if cand.score and not (event and event.is_exit):
        lines.append("")
        lines.append(render_score(cand.score))

    if event and event.is_exit:
        lines.append(
            "\n<i>You are seeing this after they sold. Polling cannot beat them out "
            "the door - it only stops you holding a bag they already dropped.</i>"
        )
    else:
        lines.append("\n<i>Their size is not your size. Mirror the conviction, "
                     "not the ticket.</i>")
    return clip("\n".join(lines))
