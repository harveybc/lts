"""Forward inference is causal without inheriting the research split cutoff."""
from copy import deepcopy
from datetime import datetime, timezone

import numpy as np
import pytest

from app.forward_pool.core import PoolRefusal
from app.forward_pool.selected70_feed import (
    HOUR_NS, build_forward_observation, build_windows, latest_valid, observation_at,
)


def fixture():
    names = ["return", "level"]
    recipe = {
        "schema": "expert_guided_consumer_recipe.v2",
        "list": {"columns": names},
        "tensor": {
            "columns": [dict(store_column=n, tensor_name=n, consumer_ops=[]) for n in names],
            "window": dict(observations=13, stride_rows=2, includes_current_completed_bar=True),
        },
        "scaler": dict(policy="fixed_train", features=list(names), mean=[0., 10.], std=[1., 2.]),
        "business": {"agent_state": "none: no agent feedback in the observation"},
        "split": {"test_closed_from": "2025-01-01"},
    }
    base = int(datetime(2026, 11, 8, tzinfo=timezone.utc).timestamp()) * 10**9
    stamps = base + np.arange(30, dtype=np.int64) * HOUR_NS
    values = np.column_stack([np.arange(30, dtype=float), np.full(30, 12.)])
    published = np.broadcast_to((stamps + HOUR_NS)[:, None], values.shape).copy()
    return recipe, stamps, values, published


def call(recipe, stamps, values, published, index=29):
    close = int(stamps[index]) + HOUR_NS
    return build_forward_observation(
        recipe, stamps, values, published_ns=published,
        decision_time_ns=close, expected_close_ns=close,
    )


def test_forward_uses_current_bar_without_mutating_historical_contract():
    recipe, stamps, values, published = fixture()
    original = deepcopy(recipe)
    with pytest.raises(PoolRefusal, match="FEATURE_ABSENT"):
        build_windows(recipe, stamps, values)
    result = call(recipe, stamps, values, published)
    assert result["bar_close_utc_ns"] == int(stamps[-1]) + HOUR_NS
    assert result["features"].shape == (13, 2)
    assert result["features"][-1].tolist() == [29., 1.]
    assert recipe == original


def test_forward_matches_historical_transform_before_cutoff():
    recipe, stamps, values, published = fixture()
    stamps -= 3 * 365 * 24 * HOUR_NS
    published -= 3 * 365 * 24 * HOUR_NS
    for ops in ([], ["DIFF"]):
        recipe["tensor"]["columns"][0]["consumer_ops"] = ops
        name = "return@diff" if ops else "return"
        recipe["tensor"]["columns"][0]["tensor_name"] = name
        recipe["scaler"]["features"][0] = name
        table = build_windows(recipe, stamps, values)
        assert np.array_equal(call(recipe, stamps, values, published)["features"],
                              observation_at(table, latest_valid(table)))


def test_future_rows_do_not_change_requested_observation():
    recipe, stamps, values, published = fixture()
    before = call(recipe, stamps, values, published, index=26)["features"]
    values[27:] = 1e10
    after = call(recipe, stamps, values, published, index=26)["features"]
    assert np.array_equal(before, after)


@pytest.mark.parametrize("missing", ["bar", "feature", "publication"])
def test_no_stale_fallback_for_incomplete_requested_bar(missing):
    recipe, stamps, values, published = fixture()
    expected = int(stamps[-1]) + HOUR_NS
    if missing == "bar":
        stamps, values, published = stamps[:-1], values[:-1], published[:-1]
    elif missing == "feature":
        values[-1, 0] = np.nan
    else:
        published[-1, 0] = expected + 1
    with pytest.raises(PoolRefusal, match="EXPECTED_BAR_ABSENT|FEATURE_ABSENT"):
        build_forward_observation(recipe, stamps, values, published_ns=published,
                                  decision_time_ns=expected, expected_close_ns=expected)


def test_incomplete_hour_and_untyped_publication_are_refused():
    recipe, stamps, values, published = fixture()
    expected = int(stamps[-1]) + HOUR_NS
    with pytest.raises(PoolRefusal, match="DECISION_TIME"):
        build_forward_observation(recipe, stamps, values, published_ns=published,
                                  decision_time_ns=expected - 1, expected_close_ns=expected)
    with pytest.raises(PoolRefusal, match="PUBLICATION_TIME"):
        call(recipe, stamps, values, published.astype(float))
