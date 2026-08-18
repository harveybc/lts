import pytest

from app.model_position_control import decide_position_control, model_close_consumed_bar


@pytest.mark.parametrize(
    ("signal", "exposure", "pending", "expected"),
    [
        ("long", 0.0, False, "enter_long"),
        ("short", 0.0, False, "enter_short"),
        ("hold", 0.0, False, "hold"),
        ("close", 0.0, False, "hold"),
        ("long", 1.0, False, "monitor"),
        ("hold", 1.0, False, "monitor"),
        ("close", 1.0, False, "close"),
        ("short", 1.0, False, "close"),
        ("long", -1.0, False, "close"),
        ("short", -1.0, False, "monitor"),
        ("close", 0.0, True, "close"),
        ("short", 0.0, True, "close"),
        ("hold", 0.0, True, "monitor"),
    ],
)
def test_position_control_never_reverses_in_one_step(
    signal, exposure, pending, expected
):
    decision = decide_position_control(
        signal, current_exposure=exposure, pending_entry=pending
    )
    assert decision.disposition == expected


def test_unknown_signal_refuses() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        decide_position_control("guess", current_exposure=0.0)


def test_model_close_consumes_only_the_exact_model_bar() -> None:
    record = {
        "venue": "mt5_demo",
        "model_id": "eth-policy",
        "timeframe": "4h",
        "bar_close": "2026-08-18T05:00:00Z",
        "outcome": "model_close_requested",
    }
    assert model_close_consumed_bar(
        [record], venue="mt5_demo", model_id="eth-policy", timeframe="4h",
        bar_close="2026-08-18T05:00:00Z",
    )
    assert not model_close_consumed_bar(
        [record], venue="mt5_demo", model_id="eth-policy", timeframe="4h",
        bar_close="2026-08-18T09:00:00Z",
    )


def test_live_runners_apply_the_bar_gate_before_another_close_or_entry() -> None:
    import inspect

    from app import alpaca_model_runner, ibkr_model_runner, mt5_model_runner

    for runner in (
        alpaca_model_runner.AlpacaModelRunner,
        ibkr_model_runner.IbkrModelRunner,
        mt5_model_runner.Mt5ModelRunner,
    ):
        source = inspect.getsource(runner.tick)
        gate = source.index("if close_consumed:")
        close = source.index('if control.disposition == "close"')
        assert gate < close
