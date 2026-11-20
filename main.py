"""Entry point: python main.py

Started by hand, or by Windows at logon through the scheduled task that
`install_autostart.ps1` registers. Nothing here assumes a console: the log goes
to a file so the bot can run under pythonw.exe with no window at all.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import socket
import sys
import time

from telegram.error import Conflict, InvalidToken, NetworkError

from memehunter import dns
from memehunter.bot import build_app
from memehunter.config import CFG

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

# How long to keep retrying a startup that cannot reach Telegram. At logon the
# bot often wins the race against the Wi-Fi, and a process that gives up then
# would leave the laptop running all day with no bot on it.
RETRY_DELAYS = (5, 15, 30, 60, 120, 300)

# Held for the life of the process. A second copy cannot bind it, which is how
# a duplicate start is detected.
_instance_lock: socket.socket | None = None


def claim_this_machine(port: int = 47653) -> bool:
    """True when no other copy of the bot is already running here.

    Two copies both poll Telegram, and Telegram hands each update to whichever
    asked first, so commands land in one process or the other at random. That
    looks like the bot ignoring you rather than like a second instance, and the
    only hint is a `Conflict: terminated by other getUpdates request` buried in
    a log nobody is reading.

    A socket is used rather than a PID file because the operating system drops
    it when the process ends, however it ends -- a lock file left behind by a
    crash would block every later start until someone deleted it by hand.
    """
    global _instance_lock
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR on purpose: reusing the address is exactly what must
        # not be allowed here.
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        sock.close()
        return False
    _instance_lock = sock
    return True


def start_logging() -> None:
    """Log to a rotating file as well as the console.

    Under the scheduled task there is no console to read, and the old setup
    depended on the shell redirecting output. A bot that runs unattended has to
    keep its own record.
    """
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        level=logging.INFO, handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def run_forever() -> int:
    """Poll Telegram, waiting out a network that is not up yet.

    Only the connection is retried. A bad token or a second copy of the bot are
    both permanent, and retrying either would just repeat the same failure
    every few minutes for as long as the laptop is on.
    """
    for attempt, delay in enumerate((*RETRY_DELAYS, None)):
        try:
            build_app().run_polling(drop_pending_updates=True)
            return 0
        except (InvalidToken, Conflict) as exc:
            logging.error("cannot start: %s", exc)
            return 1
        except NetworkError as exc:
            if delay is None:
                logging.error("still cannot reach Telegram, giving up: %s", exc)
                return 1
            logging.warning("cannot reach Telegram (%s); retrying in %ds", exc, delay)
            time.sleep(delay)
    return 1


def main() -> int:
    start_logging()

    # Before anything opens a connection: this network may answer
    # api.telegram.org with an address that is not Telegram's.
    if CFG.dns_fix:
        dns.install(CFG.dns_hosts, CFG.dns_resolvers)

    if not claim_this_machine():
        logging.error(
            "Another copy of the bot is already running on this machine. Two "
            "copies fight over the same updates and commands go missing, so "
            "this one is stopping.")
        return 1

    missing = CFG.missing_required()
    if missing:
        logging.error("Missing required settings in .env: %s", ", ".join(missing))
        return 1

    if not (CFG.helius_key or CFG.solscan_key):
        logging.warning(
            "No Helius or Solscan key set - falling back to the public Solana RPC. "
            "It works, but early-buyer lookups will be slow and may rate limit."
        )
    if not CFG.etherscan_key:
        logging.warning("No Etherscan key set - EVM chains are disabled.")

    return run_forever()


if __name__ == "__main__":
    sys.exit(main())
