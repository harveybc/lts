import pytest

from app.model_position_control import decide_position_control


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
