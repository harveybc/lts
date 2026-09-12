"""R4 (order 2026-09-11): a wrapped connection failure is still transient.

The observed defect: the Alpaca broker layer wraps every transport error
with ``raise AlpacaPaperError(...) from exc``. The runner's classifier
looked only at the exception it was handed, called the result fatal, and
parked on the hour-long fatal cadence — while a read-only observer on the
same host was reconnecting normally.

These tests pin both halves of the correction:

  * classification follows the EXPLICIT ``__cause__`` chain and nothing
    else. Not the message ("account request failed: ConnectionError" is
    exactly the text a message-sniffing classifier would be fooled by),
    and not ``__context__``, which would let a config refusal raised
    inside an ``except ConnectionError`` block retry forever;
  * fatal stays fatal. A wrong account, a bad artifact, a schema refusal
    and a binding refusal are fatal whether or not they are wrapped.

Nothing here grants permission to trade, cancels an open paper order or
runs an extraordinary tick: every test drives pure functions or a tick
callable supplied by the test itself.
"""
from __future__ import annotations

import errno
import json
import threading

import pytest

from app.runner_retry_taxonomy import (MAX_CAUSE_DEPTH, backoff_seconds,
                                       classify_runner_exception,
                                       transient_cause)


class AlpacaPaperError(RuntimeError):
    """Shape-identical to app.alpaca_paper_lab.AlpacaPaperError."""


class RequestsConnectionError(Exception):
    """`requests.exceptions.ConnectionError` is NOT the builtin one."""


RequestsConnectionError.__name__ = "ConnectionError"


class RequestsReadTimeout(Exception):
    pass


RequestsReadTimeout.__name__ = "ReadTimeout"


def wrap(inner: BaseException, message: str) -> AlpacaPaperError:
    """Reproduce the broker's `raise ... from` exactly."""
    try:
        raise inner
    except BaseException as exc:                         # noqa: BLE001
        outer = AlpacaPaperError(message)
        outer.__cause__ = exc
        return outer


# ------------------------------------------------------------ transient
def test_direct_connection_error_is_transient():
    assert classify_runner_exception(ConnectionError("refused")) == "transient"


def test_wrapped_connection_error_is_transient():
    """The exact live failure: `account request failed: ConnectionError`."""
    exc = wrap(ConnectionError("connection refused"),
               "account request failed: ConnectionError")
    assert classify_runner_exception(exc) == "transient"
    assert type(transient_cause(exc)).__name__ == "ConnectionError"


def test_wrapped_requests_connection_error_is_transient():
    """requests' ConnectionError does not subclass the builtin one."""
    exc = wrap(RequestsConnectionError("max retries exceeded"),
               "/v2/account request failed: ConnectionError")
    assert classify_runner_exception(exc) == "transient"


@pytest.mark.parametrize("inner", [
    TimeoutError("timed out"),
    RequestsReadTimeout("read timed out"),
    OSError(errno.EHOSTUNREACH, "unreachable"),
])
def test_wrapped_timeouts_are_transient(inner):
    exc = wrap(inner, "/v2/account request failed")
    assert classify_runner_exception(exc) == "transient"


def test_a_transient_cause_two_levels_down_is_still_found():
    inner = wrap(ConnectionError("refused"), "session request failed")
    outer = wrap(inner, "account request failed")
    assert classify_runner_exception(outer) == "transient"


# ---------------------------------------------------------------- fatal
def test_wrong_account_stays_fatal_even_when_wrapped():
    exc = wrap(RuntimeError("Alpaca account fingerprint changed"),
               "account request failed: RuntimeError")
    assert classify_runner_exception(exc) == "fatal"


def test_artifact_error_stays_fatal_even_when_wrapped():
    exc = wrap(FileNotFoundError("model artifact missing"),
               "/v2/account request failed: FileNotFoundError")
    assert classify_runner_exception(exc) == "fatal"


@pytest.mark.parametrize("exc", [
    AlpacaPaperError("Only the Alpaca Paper trading endpoint is allowed"),
    AlpacaPaperError("Unsupported or missing Alpaca Paper config schema"),
    AlpacaPaperError("Credentials must not be embedded in tracked config"),
    AlpacaPaperError("/v2/account returned HTTP 403: forbidden"),
])
def test_unwrapped_configuration_refusals_are_fatal(exc):
    """These carry no cause at all; a refusal is not a network event."""
    assert exc.__cause__ is None
    assert classify_runner_exception(exc) == "fatal"


def test_invalid_json_stays_fatal_although_it_is_wrapped():
    exc = wrap(ValueError("Expecting value"), "/v2/account returned "
                                              "invalid JSON")
    assert classify_runner_exception(exc) == "fatal"


def test_the_message_is_never_read():
    """A fatal refusal whose text advertises connectivity stays fatal."""
    exc = AlpacaPaperError(
        "cannot connect: the client cannot connect to Alpaca Live")
    assert classify_runner_exception(exc) == "fatal"


def test_implicit_context_is_not_followed():
    """`raise X` inside `except ConnectionError` sets __context__ only.

    Following it would make every refusal discovered during an outage
    retryable forever.
    """
    try:
        try:
            raise ConnectionError("refused")
        except ConnectionError:
            raise AlpacaPaperError("Only the Alpaca Paper endpoint is allowed")
    except AlpacaPaperError as exc:
        assert exc.__context__ is not None
        assert exc.__cause__ is None
        assert classify_runner_exception(exc) == "fatal"


def test_a_cause_cycle_terminates():
    a = AlpacaPaperError("a")
    b = AlpacaPaperError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert classify_runner_exception(a) == "fatal"


def test_a_chain_longer_than_the_bound_is_fatal_not_unbounded():
    exc: BaseException = ConnectionError("refused")
    for _ in range(MAX_CAUSE_DEPTH + 2):
        exc = wrap(exc, "wrapped again")
    assert classify_runner_exception(exc) == "fatal"


# -------------------------------------------------------------- backoff
def test_backoff_is_exponential_and_bounded():
    got = [backoff_seconds(n, base=15.0, cap=300.0) for n in range(1, 8)]
    assert got == [15.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0]


def test_backoff_refuses_nonsense():
    with pytest.raises(ValueError):
        backoff_seconds(0, base=15.0, cap=300.0)
    with pytest.raises(ValueError):
        backoff_seconds(1, base=0.0, cap=300.0)


# --------------------------------------------------- the runner's loop
def _run_loop(ticks, tmp_path, *, iterations):
    """Drive app.alpaca_model_runner.main's loop body with a fake runner.

    The real main() is not called: it would build a broker session. This
    exercises the same decision structure with a tick callable the test
    owns, so no order, session or account is touched.
    """
    from app.runner_retry_taxonomy import (backoff_seconds as bo,
                                           classify_runner_exception as cls,
                                           transient_cause as tc)
    heartbeats: list[dict] = []
    waits: list[float] = []
    consecutive = 0
    config = {"loop_seconds": 60.0, "fatal_retry_seconds": 3600.0,
              "transient_backoff_base_seconds": 15.0,
              "transient_backoff_cap_seconds": 300.0}
    for i in range(iterations):
        try:
            heartbeats.append({"state": "ok", **ticks[i]()})
            consecutive = 0
        except Exception as exc:                          # noqa: BLE001
            kind = cls(exc)
            cause = tc(exc)
            hb = {"state": "degraded_error",
                  "phase": "connect" if kind == "transient" else "fatal",
                  "error": f"{type(exc).__name__}: {exc}",
                  "transient_cause": (type(cause).__name__ if cause
                                      else "UNAVAILABLE")}
            if kind == "transient":
                consecutive += 1
                wait = bo(consecutive,
                          base=config["transient_backoff_base_seconds"],
                          cap=config["transient_backoff_cap_seconds"])
                hb["consecutive_transient"] = consecutive
                hb["retry_in_seconds"] = wait
                waits.append(wait)
            else:
                waits.append(config["fatal_retry_seconds"])
            heartbeats.append(hb)
            continue
        waits.append(config["loop_seconds"])
    return heartbeats, waits


def test_an_outage_backs_off_then_a_good_tick_resets(tmp_path):
    boom = lambda: (_ for _ in ()).throw(                 # noqa: E731
        wrap(ConnectionError("refused"), "account request failed"))
    ticks = [boom, boom, boom, lambda: {"orders_submitted": 0}, boom]
    hbs, waits = _run_loop(ticks, tmp_path, iterations=5)
    assert waits == [15.0, 30.0, 60.0, 60.0, 15.0], waits
    assert [h["phase"] for h in hbs if "phase" in h] == \
        ["connect", "connect", "connect", "connect"]
    assert hbs[0]["transient_cause"] == "ConnectionError"
    assert hbs[-1]["consecutive_transient"] == 1, "a good tick resets it"


def test_a_fatal_failure_still_parks_on_the_slow_cadence(tmp_path):
    boom = lambda: (_ for _ in ()).throw(                 # noqa: E731
        AlpacaPaperError("Only the Alpaca Paper trading endpoint is allowed"))
    hbs, waits = _run_loop([boom], tmp_path, iterations=1)
    assert waits == [3600.0]
    assert hbs[0]["phase"] == "fatal"
    assert hbs[0]["transient_cause"] == "UNAVAILABLE"
    assert "retry_in_seconds" not in hbs[0]


def test_the_shipped_runner_wires_backoff_and_names_the_cause():
    """The correction must be in the file, not only in this test."""
    import inspect
    from app import alpaca_model_runner as mod

    src = inspect.getsource(mod.main)
    assert "backoff_seconds(" in src
    assert "transient_cause" in src
    assert "consecutive_transient = 0" in src
    assert "fatal_retry_seconds" in src, "fatal cadence must survive"
