import pytest

from plugins_portfolio.default_portfolio import DefaultPortfolio


def test_net_instrument_target_equals_sum_of_virtual_cell_targets():
    cells = [
        {"cell_id": "btc-fast", "instrument": "BTC/USD", "target_units": 1.25},
        {"cell_id": "btc-slow", "instrument": "BTC/USD", "target_units": -0.40},
        {"cell_id": "eth-fast", "instrument": "ETH/USD", "target_units": 2.00},
        {"cell_id": "eth-slow", "instrument": "ETH/USD", "target_units": -0.50},
    ]

    result = DefaultPortfolio.net_instrument_targets(cells)

    for instrument, net_target in result["net_targets"].items():
        attributed = sum(
            row["target_units"]
            for row in result["cell_attribution"].values()
            if row["instrument"] == instrument
        )
        assert net_target == pytest.approx(attributed)
    assert result["net_targets"] == pytest.approx(
        {"BTC/USD": 0.85, "ETH/USD": 1.5}
    )


def test_net_instrument_targets_are_permutation_invariant():
    cells = [
        {"cell_id": "a", "instrument": "EUR/USD", "target_units": 1000},
        {"cell_id": "b", "instrument": "EUR/USD", "target_units": -250},
        {"cell_id": "c", "instrument": "USD/JPY", "target_units": 500},
    ]

    forward = DefaultPortfolio.net_instrument_targets(cells)
    reverse = DefaultPortfolio.net_instrument_targets(list(reversed(cells)))

    assert forward["net_targets"] == reverse["net_targets"]


def test_net_instrument_targets_reject_duplicate_cell_identity():
    cells = [
        {"cell_id": "same", "instrument": "EUR/USD", "target_units": 1000},
        {"cell_id": "same", "instrument": "EUR/USD", "target_units": -250},
    ]

    with pytest.raises(ValueError, match="duplicate cell_id"):
        DefaultPortfolio.net_instrument_targets(cells)
