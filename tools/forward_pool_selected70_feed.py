"""Build the selected70 window and call the saved categorical weights.

Paths come from the environment and are never printed. The observation
comparison uses the production ``FrozenRecipeInputs`` loader in a child whose
import path is the categorical runtime. The weight call uses that same runtime
in the interpreter that can import its loader. A historical close is replay
evidence. This tool submits no order and writes no live ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.forward_pool.adapter import AGENT_STATE_NONE, greedy_signal
from app.forward_pool.selected70_feed import (
    build_windows,
    inventory_document,
    latest_valid,
    live_status,
    observation_at,
)

MODEL_SHA = "079bef0ea6f3b9327772f4204e5d5dbbd4be69a5ab58c32b424aa3cb7c686404"
RUNTIME_CHILD = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    import numpy as np
    root, recipe_path, store, manifest, out = sys.argv[1:6]
    sys.path.insert(0, root)
    from app.frozen_recipe_inputs import FrozenRecipeInputs
    recipe = json.loads(Path(recipe_path).read_text())
    built = FrozenRecipeInputs.load(recipe, store, manifest)
    np.savez(
        out,
        windows=np.asarray(built.windows),
        valid=np.asarray(built.valid),
        row_close=np.asarray(built.row_ts_ns),
    )
    """
)
MODEL_CHILD = textwrap.dedent(
    """
    import hashlib, json, sys
    from pathlib import Path
    import numpy as np
    import torch
    root, bundle, window_path = sys.argv[1:4]
    sys.path.insert(0, root)
    import app.structural_refit as refit
    model, manifest = refit.load_bundle(bundle, device="cpu")
    payload = np.load(window_path)
    features = torch.tensor(np.asarray(payload["windows"]), device=model.device)
    with torch.inference_mode():
        logits = model.surfaces(features).detach().cpu().numpy()
    digest = hashlib.sha256(Path(bundle, "model.pt").read_bytes()).hexdigest()
    json.dump({
        "model_sha256": digest,
        "manifest_schema": manifest.get("schema"),
        "manifest_codec": manifest.get("codec"),
        "logits": logits.tolist(),
        "evaluation_device": "cpu",
        "mode": "greedy",
    }, sys.stdout)
    """
)


def _fail(code: str, **fields: object) -> int:
    body = {"ok": False, "code": code, "shadow_observing": False, "paper_demo_executing": False, "orders_submitted": 0}
    body.update(fields)
    sys.stdout.write(json.dumps(body, sort_keys=True) + "\n")
    return 2


def _public_error(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    tail = lines[-1] if lines else "CHILD_FAILED"
    kept = [token for token in tail.split() if "/" not in token and "~" not in token and "\\" not in token]
    return " ".join(kept)[:240] or "CHILD_FAILED"


def _run(python: str, root: str, script: str, extra: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [python, "-c", script, root, *extra],
        cwd="/tmp",
        env={**os.environ, "PYTHONPATH": root, "PYTHONNOUSERSITE": "1", "PYTHONWARNINGS": "ignore"},
        capture_output=True,
        text=True,
        check=False,
    )


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(int(ns) // 1_000_000_000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _same(left: np.ndarray, right: np.ndarray) -> tuple[bool, float | None]:
    if left.shape != right.shape or left.dtype != right.dtype:
        return False, None
    missing_left = ~np.isfinite(left)
    missing_right = ~np.isfinite(right)
    if not np.array_equal(missing_left, missing_right):
        return False, None
    if bool(missing_left.all()):
        return True, 0.0
    delta = float(np.max(np.abs(left[~missing_left].astype(np.float64) - right[~missing_right].astype(np.float64))))
    return delta == 0.0, delta


def _refuse_live(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    text = str(resolved)
    return resolved.name == "lts_trading.db" or any(
        marker in text for marker in ("/.local/state/lts", "/.config/lts", "/.lts/")
    )


def _write(path: Path, body: dict) -> None:
    if _refuse_live(path):
        raise SystemExit(_fail("LIVE_LEDGER_PATH"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _producers(manifest: dict) -> list[dict]:
    rows = []
    for name, body in (manifest.get("inputs") or {}).items():
        rows.append(
            {
                "name": name,
                "role": "feature_pool",
                "file": body.get("file"),
                "sha256": body.get("sha256"),
                "source_sha256": body.get("source_sha256"),
            }
        )
    consumer = manifest.get("consumer_scope") or {}
    rows.append(
        {
            "name": consumer.get("name"),
            "role": "learned_producer_scope",
            "learned_producers": ["activity", "kalman", "gmm"],
            "fit_window": consumer.get("fit_window"),
            "changed_columns": consumer.get("changed_columns"),
            "report_sha256": consumer.get("report_sha256"),
            "parquet_sha256": consumer.get("parquet_sha256"),
        }
    )
    parent = manifest.get("parent_pool") or {}
    rows.append({"name": "parent_pool", "role": "parent_store", "file": parent.get("file"), "sha256": parent.get("sha256")})
    return rows


def main() -> int:
    bundle = os.environ.get("FORWARD_POOL_BUNDLE")
    store = os.environ.get("FORWARD_POOL_STORE")
    manifest_path = os.environ.get("FORWARD_POOL_MANIFEST")
    code_root = os.environ.get("FORWARD_POOL_CODE_ROOT")
    model_python = os.environ.get("FORWARD_POOL_PYTHON")
    if not bundle or not store or not manifest_path or not code_root or not model_python:
        return _fail("FEED_INPUT_MISSING")
    recipe_path = Path(bundle) / "RECIPE.json"
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if recipe.get("list", {}).get("n") != 70 or len(recipe.get("list", {}).get("columns") or []) != 70:
        return _fail("COLUMN_ORDER")
    if (recipe.get("business") or {}).get("agent_state") != AGENT_STATE_NONE:
        return _fail("AGENT_STATE_INPUT")
    store_sha = _sha(Path(store))
    manifest_sha = _sha(Path(manifest_path))
    model_sha = _sha(Path(bundle) / "model.pt")
    if store_sha != recipe.get("store", {}).get("sha256") or manifest_sha != recipe.get("store", {}).get("manifest_sha256"):
        return _fail("STORE_PIN")
    if model_sha != MODEL_SHA:
        return _fail("MODEL_SHA")
    cutoff = (recipe.get("split") or {}).get("test_closed_from")
    pandas_cutoff = int(pd.Timestamp(cutoff, tz="UTC").value)
    python_cutoff = int(datetime(int(cutoff[:4]), int(cutoff[5:7]), int(cutoff[8:10]), tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    if pandas_cutoff != python_cutoff:
        return _fail("CLOCK_CUTOFF")
    names = [item["store_column"] for item in recipe["tensor"]["columns"]]
    frame = pd.read_parquet(
        store,
        columns=["timestamp", *names],
        filters=[("timestamp", "<", pd.Timestamp(cutoff, tz="UTC"))],
    )
    stamps = pd.DatetimeIndex(pd.to_datetime(frame.timestamp, utc=True)).as_unit("ns").asi8.copy()
    values = frame[names].to_numpy(dtype=np.float64, copy=True)
    table = build_windows(recipe, stamps, values)
    with tempfile.TemporaryDirectory(prefix="selected70-feed-") as scratch:
        reference_path = str(Path(scratch) / "reference.npz")
        completed = _run(sys.executable, code_root, RUNTIME_CHILD, [str(recipe_path), store, manifest_path, reference_path])
        if completed.returncode != 0:
            return _fail("RUNTIME_CHILD", detail=_public_error(completed.stderr))
        reference = np.load(reference_path)
        equal, delta = _same(np.asarray(table["windows"]), np.asarray(reference["windows"]))
        valid_equal = np.array_equal(np.asarray(table["valid"]), np.asarray(reference["valid"]))
        close_equal = np.array_equal(np.asarray(table["bar_close_utc_ns"]), np.asarray(reference["row_close"]))
        if not equal or not valid_equal or not close_equal:
            return _fail(
                "WINDOW_MISMATCH",
                max_abs_error=delta,
                valid_equal=bool(valid_equal),
                close_equal=bool(close_equal),
                rows=int(len(table["valid"])),
            )
        last = latest_valid(table)
        valid_rows = np.flatnonzero(np.asarray(table["valid"], dtype=bool))
        earlier = int(valid_rows[len(valid_rows) // 2])
        window_path = str(Path(scratch) / "call.npz")
        np.savez(
            window_path,
            windows=np.stack([observation_at(table, earlier), observation_at(table, last)]),
        )
        called = _run(model_python, code_root, MODEL_CHILD, [bundle, window_path])
        if called.returncode != 0:
            return _fail("MODEL_CHILD", detail=_public_error(called.stderr))
        result = json.loads(called.stdout)
    logits = np.asarray(result["logits"], dtype=np.float64)
    if logits.shape != (2, 3) or not np.isfinite(logits).all():
        return _fail("ACTION_MAPPING")
    if result.get("model_sha256") != MODEL_SHA:
        return _fail("MODEL_SHA")
    if result.get("manifest_schema") != "structural_sac.inference.v1" or result.get("manifest_codec") != "NOP_BUY_SELL_CLOSE_OPPOSITE_V1":
        return _fail("BUNDLE_SCHEMA")
    actions = [greedy_signal(row) for row in logits]
    if actions != [int(np.argmax(row)) for row in logits]:
        return _fail("ACTION_MAPPING")
    last_close = int(table["bar_close_utc_ns"][last])
    status = live_status(last_close, int(datetime.now(timezone.utc).timestamp()) * 1_000_000_000)
    if status["evidence_class"] != "HISTORICAL_REPLAY" or status["shadow_observing"] is not False:
        return _fail("STORE_ENDS_BEFORE_CURRENT_HOUR")
    heartbeat = {
        "schema": "forward_pool.selected70_feed_heartbeat.v1",
        "checkpoint_sha256": MODEL_SHA,
        "column_count": 70,
        "observations": 13,
        "stride_rows": 2,
        "scaler_policy": "fixed_train",
        "last_finalized_bar_close_utc": _iso(last_close),
        "last_inference": {
            "action": actions[1],
            "logits": [float(item) for item in logits[1]],
            "mode": "greedy",
            "evaluation_device": "cpu",
        },
        "comparison_inference": {
            "row_close_utc": _iso(int(table["bar_close_utc_ns"][earlier])),
            "action": actions[0],
            "logits": [float(item) for item in logits[0]],
        },
        "window_parity": {
            "max_abs_error": 0.0,
            "rows": int(len(table["valid"])),
            "valid_rows": int(len(valid_rows)),
            "exact": True,
        },
        "last_ledger_event": None,
        "evidence_class": "HISTORICAL_REPLAY",
        "shadow_observing": False,
        "paper_demo_executing": False,
        "orders_submitted": 0,
    }
    availability = inventory_document(
        recipe,
        {
            "source_sha256": manifest.get("source_sha256"),
            "store_sha256": store_sha,
            "manifest_sha256": manifest_sha,
            "feature_list_sha256": (recipe.get("list") or {}).get("sha256"),
            "store_rows": int(len(frame)),
            "row_count": int(len(table["bar_start_utc_ns"])),
            "leading_nonfinite_rows_dropped": int(len(frame) - len(table["bar_start_utc_ns"])),
            "last_observation_bar_start_utc": _iso(int(table["bar_start_utc_ns"][last])),
            "last_publication_bar_close_utc": _iso(last_close),
            "refresh_reason": "STORE_ENDS_BEFORE_CURRENT_HOUR",
            "evidence_class": "HISTORICAL_REPLAY",
            "shadow_observing": False,
            "producers": _producers(manifest),
        },
    )
    availability_path = os.environ.get("FORWARD_POOL_AVAILABILITY")
    heartbeat_path = os.environ.get("FORWARD_POOL_HEARTBEAT")
    if availability_path:
        _write(Path(availability_path), availability)
    if heartbeat_path:
        _write(Path(heartbeat_path), heartbeat)
    sys.stdout.write(json.dumps({"ok": True, **heartbeat}, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
