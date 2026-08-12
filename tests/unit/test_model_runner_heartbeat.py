from types import SimpleNamespace

from app.model_runner_heartbeat import linear_model_identity


def _selector(*, first_mean=1.0):
    return SimpleNamespace(
        manifest={"config_sha256": "c" * 64},
        manifest_sha256="m" * 64,
        policy=SimpleNamespace(
            model_id="fixture-linear-v1",
            artifact_sha256="a" * 64,
            feature_names=("return_1", "range_1"),
            means=(first_mean, 2.0),
            scales=(3.0, 4.0),
        ),
    )


def test_linear_model_identity_is_complete_and_deterministic():
    first = linear_model_identity(_selector())
    second = linear_model_identity(_selector())

    assert first == second
    assert first["model_id"] == "fixture-linear-v1"
    assert first["artifact_sha256"] == "a" * 64
    assert first["config_sha256"] == "c" * 64
    assert first["manifest_sha256"] == "m" * 64
    assert len(first["input_feature_sha256"]) == 64
    assert len(first["preprocessing_sha256"]) == 64


def test_preprocessing_hash_tracks_loaded_scaler_values_only():
    baseline = linear_model_identity(_selector())
    changed = linear_model_identity(_selector(first_mean=9.0))

    assert changed["input_feature_sha256"] == baseline["input_feature_sha256"]
    assert changed["preprocessing_sha256"] != baseline["preprocessing_sha256"]
