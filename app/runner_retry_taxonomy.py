"""Retry taxonomy for continuous model runners (AUD-F2-20260804-103).

Only EXPLICIT transient connection/session failures may be retried with
backoff. Everything else — account mismatch, invalid config/schema,
missing artifacts, authorization failures, programming errors — is fatal:
the runner keeps an advancing degraded heartbeat labeled ``phase=fatal``
on a slow fixed cadence so the condition pages immediately and is never
mislabeled as connectivity.

The classification is strictly type/errno-based. Message sniffing is
deliberately absent: config refusals often contain words like "connect"
and must never be retried as if the network were at fault.

R4 (order 2026-09-11) — THE CAUSE CHAIN IS PART OF THE TYPE. The
classifier used to inspect only the exception it was handed. A broker
layer that does

    except requests.RequestException as exc:
        raise AlpacaPaperError(f"{endpoint} request failed: ...") from exc

therefore turned a transient connection failure into a fatal one: the
runner froze on the hour-long fatal cadence while the network had
already come back. The fix is not to read the message — the wrapper's
text says "ConnectionError" and reading that would be exactly the
message sniffing this module refuses. It is to follow the EXPLICIT
cause chain that ``raise ... from`` builds.

Only ``__cause__`` is followed, never ``__context__``. An explicit
cause is an assertion by the raising code that THIS is the underlying
failure; an implicit context is merely "something else was being
handled at the time", and following it would let a config refusal
raised inside an ``except ConnectionError`` block be retried forever.
Fatal stays fatal: a wrapper whose cause is a ValueError, a bad
account or a missing artifact has no transient link anywhere in its
chain.
"""
from __future__ import annotations

import errno

TRANSIENT_ERRNOS = frozenset({
    errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
    errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH,
    errno.ENETDOWN, errno.EPIPE, errno.EAGAIN,
})

#: exception type names from broker/session layers that are transient by
#: contract even when they do not subclass ConnectionError. The
#: ``requests`` timeout family is named explicitly: ``ReadTimeout`` and
#: ``Timeout`` subclass neither ``TimeoutError`` nor ``ConnectionError``.
TRANSIENT_TYPE_NAMES = frozenset({
    "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
    "ConnectionAbortedError", "BrokenPipeError", "TimeoutError",
    "SocketTimeout", "ApiTimeout",
    "Timeout", "ConnectTimeout", "ReadTimeout", "ProxyError",
})

#: how far down an explicit ``raise ... from`` chain to look. Deep
#: enough for broker -> session -> transport, bounded so a malformed
#: chain cannot cost unbounded work.
MAX_CAUSE_DEPTH = 8


def _is_transient_here(exc: BaseException) -> bool:
    """Classify ONE exception, ignoring anything it was raised from."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, OSError) and exc.errno in TRANSIENT_ERRNOS:
        return True
    return type(exc).__name__ in TRANSIENT_TYPE_NAMES


def transient_cause(exc: BaseException) -> BaseException | None:
    """Return the exception in the explicit cause chain that makes this
    failure transient, or ``None``. Returning the cause rather than a
    bool lets the caller name WHAT was transient in its heartbeat."""
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(MAX_CAUSE_DEPTH):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        if _is_transient_here(current):
            return current
        current = current.__cause__
    return None


def classify_runner_exception(exc: BaseException) -> str:
    """Return ``"transient"`` or ``"fatal"``. Unknown is fatal."""
    return "transient" if transient_cause(exc) is not None else "fatal"


def backoff_seconds(consecutive: int, *, base: float, cap: float) -> float:
    """Bounded exponential backoff for CONSECUTIVE transient failures.

    ``consecutive`` is 1 for the first failure after a good tick. The
    growth is bounded by ``cap`` so a long outage settles into a fixed
    polling cadence instead of drifting towards never retrying — a
    runner that stops asking cannot notice that the venue came back.
    """
    if consecutive < 1:
        raise ValueError("consecutive failures start at 1")
    if base <= 0 or cap <= 0:
        raise ValueError("backoff base and cap must be positive")
    return float(min(cap, base * (2 ** (consecutive - 1))))
