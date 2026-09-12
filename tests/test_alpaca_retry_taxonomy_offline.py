"""P2/E2 offline: the Alpaca wrapper's noise, classified WITHOUT ever
contacting the venue.

No socket is opened, no credential is read, no order is placed. Every
case is a real exception object built here and handed to the
classifier, which is the whole surface the runner uses to decide
between retrying and paging.

The wrapper in app/alpaca_paper_lab.py does

    except requests.RequestException as exc:
        raise AlpacaPaperError(f"{endpoint} request failed: ...") from exc

so every transport failure reaches the runner dressed as an
AlpacaPaperError. Before R4 that was FATAL, and a runner froze on the
hour-long fatal cadence while the network had already come back. What
must hold is that the explicit `from exc` chain — and ONLY the explicit
chain — restores the transient classification, while a genuine config
refusal stays fatal however transient-sounding its text.
"""
from __future__ import annotations

import errno
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.runner_retry_taxonomy import (  # noqa: E402
    backoff_seconds, classify_runner_exception, transient_cause)


class AlpacaPaperError(RuntimeError):
    """The wrapper type, reproduced here so the test needs no venue."""


def _from(outer: Exception, cause: BaseException) -> Exception:
    outer.__cause__ = cause
    return outer


# ------------------------------------------------------- transient
@pytest.mark.parametrize("cause", [
    ConnectionResetError(errno.ECONNRESET, "reset by peer"),
    ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
    TimeoutError("read timed out"),
    socket.timeout("timed out"),
    OSError(errno.EHOSTUNREACH, "no route to host"),
    OSError(errno.ENETDOWN, "network is down"),
    BrokenPipeError(errno.EPIPE, "broken pipe"),
])
def test_a_wrapped_transport_failure_is_retried(cause):
    exc = _from(AlpacaPaperError("/v2/clock request failed"), cause)
    assert classify_runner_exception(exc) == "transient"
    assert transient_cause(exc) is cause, "the runner can NAME it"


def test_a_requests_style_timeout_that_subclasses_nothing_useful():
    """requests.ReadTimeout subclasses neither TimeoutError nor
    ConnectionError; it is named by type."""
    class ReadTimeout(Exception):
        pass
    cause = ReadTimeout("read timed out")
    exc = _from(AlpacaPaperError("/v2/orders request failed"), cause)
    assert classify_runner_exception(exc) == "transient"


def test_a_three_layer_chain_still_resolves():
    transport = ConnectionResetError(errno.ECONNRESET, "reset")
    session = _from(RuntimeError("session lost"), transport)
    broker = _from(AlpacaPaperError("/v2/account request failed"),
                   session)
    assert classify_runner_exception(broker) == "transient"
    assert transient_cause(broker) is transport


# ----------------------------------------------------------- fatal
def test_an_invalid_json_response_is_fatal_not_noise():
    """The SAME wrapper type, a DIFFERENT cause. A venue answering
    with garbage is not a network blip and must page."""
    exc = _from(AlpacaPaperError("/v2/clock returned invalid JSON"),
                ValueError("Expecting value: line 1 column 1"))
    assert classify_runner_exception(exc) == "fatal"


@pytest.mark.parametrize("cause", [
    ValueError("account id mismatch"),
    KeyError("APCA_API_KEY_ID"),
    FileNotFoundError("model artifact missing"),
    PermissionError("insufficient permissions"),
    TypeError("bad argument"),
])
def test_a_config_or_programming_failure_stays_fatal(cause):
    exc = _from(AlpacaPaperError("/v2/orders request failed"), cause)
    assert classify_runner_exception(exc) == "fatal"


def test_a_refusal_whose_TEXT_sounds_transient_stays_fatal():
    """Message sniffing is what this module refuses to do. A config
    refusal saying "could not connect to the configured account" must
    not be retried forever."""
    exc = AlpacaPaperError(
        "could not connect account: ConnectionError in config")
    assert classify_runner_exception(exc) == "fatal"
    assert transient_cause(exc) is None


def test_an_IMPLICIT_context_is_never_followed():
    """A config refusal raised INSIDE an `except ConnectionError`
    block gets __context__, not __cause__. Following it would retry a
    permanent refusal forever."""
    try:
        try:
            raise ConnectionResetError(errno.ECONNRESET, "reset")
        except ConnectionResetError:
            raise AlpacaPaperError("account key is not configured")
    except AlpacaPaperError as exc:
        assert exc.__context__ is not None
        assert exc.__cause__ is None
        assert classify_runner_exception(exc) == "fatal"


def test_an_unknown_exception_is_fatal_by_default():
    class Weird(Exception):
        pass
    assert classify_runner_exception(Weird("?")) == "fatal"


def test_a_cyclic_cause_chain_terminates():
    a = AlpacaPaperError("a")
    b = AlpacaPaperError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert classify_runner_exception(a) == "fatal"


def test_a_chain_deeper_than_the_bound_does_not_run_forever():
    from app.runner_retry_taxonomy import MAX_CAUSE_DEPTH
    exc = ConnectionResetError(errno.ECONNRESET, "reset")
    for i in range(MAX_CAUSE_DEPTH + 3):
        exc = _from(AlpacaPaperError(f"layer {i}"), exc)
    assert classify_runner_exception(exc) == "fatal", \
        "beyond the bound it is unknown, and unknown is fatal"


# --------------------------------------------------------- backoff
def test_backoff_grows_then_settles_into_a_fixed_cadence():
    seq = [backoff_seconds(n, base=2.0, cap=60.0)
           for n in range(1, 9)]
    assert seq[:5] == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert seq[5:] == [60.0, 60.0, 60.0]


def test_a_runner_that_stops_asking_cannot_notice_recovery():
    """The cap exists so a long outage never drifts towards never
    retrying."""
    assert backoff_seconds(50, base=2.0, cap=60.0) == 60.0


@pytest.mark.parametrize("bad", [(0, 2.0, 60.0), (1, 0.0, 60.0),
                                 (1, 2.0, 0.0), (1, -1.0, 60.0)])
def test_impossible_backoff_parameters_refuse(bad):
    n, base, cap = bad
    with pytest.raises(ValueError):
        backoff_seconds(n, base=base, cap=cap)


# ---------------------------------------------------- no venue used
def test_this_module_opens_no_socket_and_reads_no_credential():
    import ast
    src = Path(__file__).read_text()
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    assert "connect" not in names and "getenv" not in names
    assert "requests" not in names
