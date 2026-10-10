"""Keeping the database alive on a host that has no disk.

Free container hosts give you no persistent volume: the filesystem is rebuilt
on every redeploy, restart and idle recycle. For this bot that is not a cosmetic
loss. The database holds the wallets you track, the position baselines that make
a first buy detectable, and the entire alert history the leaderboard is computed
from. Losing it silently turns a working bot into one that reports nothing and
looks fine doing it.

So the bot backs itself up to the one piece of durable storage it already has:
your Telegram chat. It sends the database as a file and pins it. On startup, if
the database is missing or empty, it reads the pinned message and restores.

Why pinning rather than remembering a file id: the file id would live in the
database, which is the thing that just vanished. A pinned message can be found
with nothing but the chat id, so recovery needs no prior state at all.
"""
from __future__ import annotations

import gzip
import logging
import shutil
import time
from pathlib import Path

import aiosqlite

from .config import CFG

log = logging.getLogger("memehunter.backup")

PREFIX = "memehunter-backup"
CAPTION = "memehunter database backup"
# Telegram lets a bot download files up to 20 MB. This database is far smaller,
# but check rather than fail a restore in a confusing way.
MAX_RESTORE_BYTES = 20 * 1024 * 1024


def backup_name() -> str:
    return f"{PREFIX}-{time.strftime('%Y%m%d-%H%M%S')}.db.gz"


async def snapshot(conn: aiosqlite.Connection, dest: Path) -> Path:
    """Write a consistent, compressed copy of the live database.

    VACUUM INTO rather than copying the file: the bot is mid-flight and a plain
    copy can catch a half-written page, producing a backup that only fails when
    you finally need it.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_suffix(".tmp")
    if raw.exists():
        raw.unlink()

    # VACUUM INTO does not accept bound parameters, so the path is inlined.
    # Quotes are escaped; the path is ours, never user input.
    await conn.execute(f"VACUUM INTO '{str(raw).replace(chr(39), chr(39) * 2)}'")
    await conn.commit()

    with raw.open("rb") as src, gzip.open(dest, "wb", compresslevel=6) as gz:
        shutil.copyfileobj(src, gz)
    raw.unlink()
    return dest


async def is_empty(conn: aiosqlite.Connection) -> bool:
    """True when this looks like a fresh database worth restoring over.

    Deliberately conservative: it only reports empty when there are no tracked
    wallets AND no recorded alerts. Restoring over a database that holds real
    work would be far worse than skipping a restore.
    """
    for table in ("tracked_wallets", "alert_outcomes"):
        try:
            row = await (await conn.execute(f"SELECT 1 FROM {table} LIMIT 1")).fetchone()
        except aiosqlite.Error:
            continue
        if row is not None:
            return False
    return True


async def send(bot, chat_id: int, conn: aiosqlite.Connection) -> bool:
    """Snapshot the database, send it to the chat, and pin it.

    The previous backup is unpinned so `get_chat().pinned_message` always points
    at the newest one. Old backups stay in the chat history as a manual fallback.
    """
    path = Path(CFG.db_path).parent / backup_name()
    try:
        await snapshot(conn, path)
    except Exception:
        log.exception("backup: could not snapshot the database")
        return False

    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            message = await bot.send_document(
                chat_id,
                document=fh,
                filename=path.name,
                caption=f"{CAPTION} - {size / 1024:.0f} KB",
                disable_notification=True,
            )
        try:
            await bot.unpin_all_chat_messages(chat_id)
        except Exception:
            log.debug("backup: nothing to unpin")
        await bot.pin_chat_message(chat_id, message.message_id,
                                   disable_notification=True)
        log.info("backup: sent and pinned %s (%d bytes)", path.name, size)
        return True
    except Exception:
        log.exception("backup: could not send the backup to Telegram")
        return False
    finally:
        path.unlink(missing_ok=True)


def _document_from(message) -> object | None:
    """The backup document on a message, if it is one of ours."""
    doc = getattr(message, "document", None)
    if not doc:
        return None
    name = (getattr(doc, "file_name", "") or "")
    return doc if name.startswith(PREFIX) else None


async def restore(bot, chat_id: int, message=None) -> str:
    """Pull a backup out of Telegram and write it over the database file.

    Uses the given message if one is supplied (so you can reply /restore to any
    older backup), otherwise the pinned one. Returns a human-readable result.

    The caller must not hold an open connection to the database when this runs.
    """
    document = None
    if message is not None:
        document = _document_from(message)
        if document is None:
            return "That message has no memehunter backup attached."
    else:
        try:
            chat = await bot.get_chat(chat_id)
        except Exception as exc:
            return f"Could not read the chat to find a pinned backup ({exc})."
        pinned = getattr(chat, "pinned_message", None)
        if pinned is None:
            return "No pinned backup found. Reply /restore to a backup file instead."
        document = _document_from(pinned)
        if document is None:
            return ("The pinned message is not a memehunter backup. Reply /restore "
                    "to a backup file instead.")

    size = getattr(document, "file_size", 0) or 0
    if size > MAX_RESTORE_BYTES:
        return f"That backup is {size / 1e6:.1f} MB, too large for a bot to download."

    target = Path(CFG.db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    archive = target.with_name(f"{PREFIX}-incoming.db.gz")
    staged = target.with_name(f"{PREFIX}-incoming.db")

    try:
        handle = await bot.get_file(document.file_id)
        await handle.download_to_drive(custom_path=str(archive))

        with gzip.open(archive, "rb") as gz, staged.open("wb") as out:
            shutil.copyfileobj(gz, out)

        # Prove it is a working database before overwriting anything.
        probe = await aiosqlite.connect(staged)
        try:
            cur = await probe.execute("SELECT COUNT(*) FROM tracked_wallets")
            wallets = (await cur.fetchone())[0]
        finally:
            await probe.close()

        if target.exists():
            shutil.copyfile(target, target.with_suffix(".db.replaced"))
        shutil.move(str(staged), str(target))
        return (f"Restored {wallets} tracked wallet(s) from "
                f"{getattr(document, 'file_name', 'the backup')}.")
    except Exception as exc:
        log.exception("restore failed")
        return f"Restore failed: {exc}"
    finally:
        archive.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
