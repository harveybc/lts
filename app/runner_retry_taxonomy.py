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
"""
from __future__ import annotations

import errno

TRANSIENT_ERRNOS = frozenset({
    errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
    errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH,
    errno.ENETDOWN, errno.EPIPE, errno.EAGAIN,
})

#: exception type names from broker/session layers that are transient by
#: contract even when they do not subclass ConnectionError.
TRANSIENT_TYPE_NAMES = frozenset({
    "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
    "ConnectionAbortedError", "BrokenPipeError", "TimeoutError",
    "SocketTimeout", "ApiTimeout",
})


def classify_runner_exception(exc: BaseException) -> str:
    """Return ``"transient"`` or ``"fatal"``. Unknown is fatal."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "transient"
    if isinstance(exc, OSError) and exc.errno in TRANSIENT_ERRNOS:
        return "transient"
    if type(exc).__name__ in TRANSIENT_TYPE_NAMES:
        return "transient"
    return "fatal"
