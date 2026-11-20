"""Name resolution that does not depend on the network's own DNS server.

Some networks answer `api.telegram.org` with an address that is not Telegram's.
On this one the answer is 108.160.166.137, which belongs to Dropbox and never
completes a handshake, so every Telegram call fails with `httpx.ConnectError:
All connection attempts failed` and the bot dies at startup. That reads like
the bot being offline. It is one wrong lookup: Telegram's real addresses route
fine from the same machine.

Pointing at a public resolver is not enough on its own. Measured here, half of
the plain-UDP queries sent to 8.8.8.8 came back with the forged address --
something on the path answers port 53 before the real resolver can, whoever
the query was addressed to:

    20 queries to 8.8.8.8 -> 10 real answers, 10 forged

So the lookup goes over HTTPS instead. A forged reply cannot survive TLS, and
the resolvers are addressed by IP so this needs no working DNS to bootstrap.
The answers are then served to everything in the process by standing in front
of `socket.getaddrinfo`, which every library here resolves through -- httpx for
Telegram, the providers, anything else -- so one hook covers all of them.

No VPN, and nothing about the machine's settings changes.

The system resolver is still tried first for everything except the names known
to be wrong here, and is still used when the public one cannot be reached, so
this degrades to the old behaviour rather than replacing it.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time

import httpx

log = logging.getLogger("memehunter.dns")

# Addressed by IP: a resolver whose own name needs resolving is no use when
# resolution is what is broken. Each of these presents a certificate valid for
# its address, so TLS verification still means something. `dns.google` is last
# and does need a working lookup, as a fallback if the IPs are ever blocked.
DOH_RESOLVERS = (
    "https://8.8.8.8/resolve",
    "https://8.8.4.4/resolve",
    "https://1.0.0.1/dns-query",
    "https://dns.google/resolve",
)

# Answers that mean "there is nothing here", used by resolvers that blackhole a
# name rather than refuse it. Treated as a failed lookup, not as an address.
BLACKHOLE = {"0.0.0.0", "::", "127.0.0.1", "::1"}

MIN_TTL = 60.0
MAX_TTL = 3600.0
TIMEOUT = 8.0

_lock = threading.Lock()
_cache: dict[str, tuple[list[str], float]] = {}
_override: set[str] = set()
_resolvers: tuple[str, ...] = ()
_real_getaddrinfo = socket.getaddrinfo
_installed = False
# Set while this module is resolving, so the lookups its own HTTPS request
# makes cannot re-enter the hook and recurse.
_busy = threading.local()


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def query(host: str, resolvers: tuple[str, ...], timeout: float = TIMEOUT
          ) -> tuple[list[str], float]:
    """Ask each DNS-over-HTTPS resolver in turn until one answers."""
    last = ""
    for url in resolvers:
        try:
            # trust_env=False: a proxy picked up from the environment would put
            # the lookup back on someone else's path, which is the thing this
            # is here to avoid.
            with httpx.Client(timeout=timeout, trust_env=False,
                              follow_redirects=True) as client:
                resp = client.get(url, params={"name": host, "type": "A"},
                                  headers={"accept": "application/dns-json"})
                resp.raise_for_status()
                body = resp.json()
        except Exception as exc:
            last = f"{url}: {exc}"
            continue

        if not isinstance(body, dict) or body.get("Status") != 0:
            last = f"{url}: status {(body or {}).get('Status')!r}"
            continue

        addresses, ttl = [], MAX_TTL
        for answer in body.get("Answer") or []:
            if answer.get("type") != 1:        # A records only; skip CNAMEs
                continue
            address = str(answer.get("data") or "")
            if address and address not in BLACKHOLE:
                addresses.append(address)
                ttl = min(ttl, float(answer.get("TTL") or MAX_TTL))
        if addresses:
            return addresses, max(MIN_TTL, min(ttl, MAX_TTL))
        last = f"{url}: no usable address"

    if last:
        log.debug("DNS over HTTPS could not resolve %s: %s", host, last)
    return [], MIN_TTL


def resolve(host: str) -> list[str]:
    """Cached DNS-over-HTTPS lookup. Empty when every resolver failed."""
    key = host.lower().rstrip(".")
    if _is_ip(key) or getattr(_busy, "active", False):
        return []
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and cached[1] > now:
            return list(cached[0])

    _busy.active = True
    try:
        addresses, ttl = query(key, _resolvers)
    finally:
        _busy.active = False

    if addresses:
        with _lock:
            _cache[key] = (addresses, now + ttl)
        log.info("resolved %s to %s over HTTPS", key, ", ".join(addresses))
    return addresses


def _blackholed(results: list) -> bool:
    return bool(results) and all(r[4][0] in BLACKHOLE for r in results)


def _entries(addresses: list[str], port, sock_type: int, proto: int) -> list:
    if isinstance(port, str):
        port = int(port) if port.isdigit() else socket.getservbyname(port, "tcp")
    return [
        (socket.AF_INET, sock_type or socket.SOCK_STREAM, proto, "", (address, port or 0))
        for address in addresses
    ]


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
    """`socket.getaddrinfo`, with the names this network answers wrongly fixed.

    Order matters. A name known to be poisoned here never reaches the system
    resolver. Everything else does, and only falls through to DNS over HTTPS
    when the system resolver fails or blackholes it -- so a normal network
    keeps resolving exactly as it did, including private and internal names.
    """
    # httpcore passes the host as bytes, not str. Skipping those sent every
    # Telegram request back to the resolver this module exists to work around,
    # while a direct call from our own code looked fixed.
    name = host
    if isinstance(name, (bytes, bytearray)):
        try:
            name = bytes(name).decode("ascii")
        except UnicodeDecodeError:
            name = None
    name = name.lower().rstrip(".") if isinstance(name, str) else None
    usable = bool(name) and family != socket.AF_INET6 and not _is_ip(name)

    if usable and name in _override:
        addresses = resolve(name)
        if addresses:
            return _entries(addresses, port, type, proto)

    try:
        results = _real_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror as exc:
        results, error = [], exc
    else:
        error = None
    if results and not _blackholed(results):
        return results

    if usable:
        addresses = resolve(name)
        if addresses:
            log.info("system DNS could not answer %s; used DNS over HTTPS", name)
            return _entries(addresses, port, type, proto)

    if error is not None:
        raise error
    return results


def install(hosts, resolvers=DOH_RESOLVERS) -> bool:
    """Route lookups through this module for the rest of the process.

    Returns whether the hook is now in place. Calling it again updates the
    lists rather than wrapping the wrapper.
    """
    global _installed, _override, _resolvers
    _override = {h.lower().rstrip(".") for h in hosts if h.strip()}
    _resolvers = tuple(r for r in resolvers if r.strip())
    if not _resolvers:
        log.warning("no DNS resolvers configured; leaving name resolution alone")
        return False
    if not _installed:
        socket.getaddrinfo = _getaddrinfo
        _installed = True
        log.info("DNS over HTTPS in use for %s",
                 ", ".join(sorted(_override)) or "failed lookups only")
    return True


def uninstall() -> None:
    """Restore the standard resolver. Used by the tests."""
    global _installed
    socket.getaddrinfo = _real_getaddrinfo
    _installed = False
    with _lock:
        _cache.clear()


def check(host: str) -> tuple[bool, str]:
    """What each resolver says about one name, for /diag."""
    public = resolve(host)
    try:
        system = sorted({r[4][0] for r in _real_getaddrinfo(host, 443, socket.AF_INET)})
    except socket.gaierror as exc:
        system, system_note = [], f"system DNS failed ({exc.strerror or exc})"
    else:
        system_note = ", ".join(system) or "system DNS returned nothing"

    if not public:
        return bool(system), f"{system_note}; DNS over HTTPS unreachable"
    if not system or set(system) & BLACKHOLE:
        return True, f"{system_note}; using {', '.join(public)} over HTTPS"
    if set(system) & set(public):
        return True, f"{system_note}, matches public DNS"
    return True, (f"system DNS says {system_note}, public DNS says "
                  f"{', '.join(public)}; using the public answer")
