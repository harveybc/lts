"""Exact greedy parity against a structural_sac.inference.v1 bundle.

Paths come from the environment and are never printed. The comparison runs in
a child process whose import path is the supplied code root, so this
repository's ``app`` package cannot be mistaken for the categorical loader.
Stochastic sampling is refused. A missing bundle is an incomplete receipt,
not a passed parity check.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap


CHILD = textwrap.dedent(
    """
    import hashlib, json, os, sys
    import numpy as np
    root = sys.argv[1]
    bundle = sys.argv[2]
    sys.path.insert(0, root)
    import app.structural_refit as refit
    from env_plugins.three_signal_native import command_for
    model, manifest = refit.load_bundle(bundle, device="cpu")
    probe = np.load(os.path.join(bundle, "PROBE.npz"))
    import torch
    with torch.inference_mode():
        logits = model.surfaces(torch.tensor(probe["inputs"], device=model.device)).cpu().numpy()
    error = float(np.max(np.abs(logits - probe["logits"])))
    disagreements = int(np.count_nonzero(logits.argmax(-1) != probe["logits"].argmax(-1)))
    pairs = []
    for signal in (0, 1, 2):
        for position in (-1, 0, 1):
            pairs.append(int(command_for(signal, position)))
    digest = hashlib.sha256(open(os.path.join(bundle, "model.pt"), "rb").read()).hexdigest()
    json.dump({
        "model_sha256": digest,
        "manifest_codec": manifest.get("codec"),
        "manifest_schema": manifest.get("schema"),
        "probe_device": (manifest.get("backend") or {}).get("device"),
        "probe_torch": (manifest.get("backend") or {}).get("torch"),
        "rows": int(len(probe["inputs"])),
        "max_abs_error": error,
        "action_disagreements": disagreements,
        "native_commands": pairs,
        "evaluation_device": "cpu",
        "mode": "greedy",
    }, sys.stdout)
    """
)

SIZING = textwrap.dedent(
    """
    import json, sys
    from app.short_horizon.execution import Sizing
    sizing = Sizing(rel_volume=0.1, leverage=50.0, basis="margin", min_units=1)
    grid = []
    for balance in (10000.0, 100.0, 50.0, 10.0):
        for price in (1.1, 1.0, 100.0):
            for q in (1.0, 0.5, 0.01, 0.0001):
                units = sizing.order_units(balance, price, q, risk_accepts_minimum=False)
                if units is not None and sizing.required_margin(units, price) > 0.1 * balance + 1e-9:
                    units = None
                grid.append(units)
    json.dump({"units": grid}, sys.stdout)
    """
)


def _run(root: str, script: str, extra: list[str]) -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", script, root, *extra],
        cwd=root,
        env={**os.environ, "PYTHONPATH": root, "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        sys.stdout.write(json.dumps({"ok": False, "code": "PARITY_CHILD", "returncode": completed.returncode}) + "\n")
        sys.stderr.write(completed.stderr[-2000:])
        raise SystemExit(2)
    return json.loads(completed.stdout)


def main() -> int:
    bundle = os.environ.get("FORWARD_POOL_BUNDLE")
    code_root = os.environ.get("FORWARD_POOL_CODE_ROOT")
    engine_root = os.environ.get("FORWARD_POOL_ENGINE_ROOT")
    if not bundle or not code_root or not engine_root:
        sys.stdout.write(json.dumps({"ok": False, "code": "PARITY_INPUT_MISSING", "parity_verified": False}) + "\n")
        return 2
    if os.environ.get("FORWARD_POOL_MODE", "greedy") != "greedy":
        sys.stdout.write(json.dumps({"ok": False, "code": "STOCHASTIC_MODE", "parity_verified": False}) + "\n")
        return 2
    probe = _run(code_root, CHILD, [bundle])
    native = _run(engine_root, SIZING, [])
    from app.forward_pool.core import command_for, order_units

    local_commands = [command_for(signal, position) for signal in (0, 1, 2) for position in (-1, 0, 1)]
    local_units = []
    for balance in (10000.0, 100.0, 50.0, 10.0):
        for price in (1.1, 1.0, 100.0):
            for q in (1.0, 0.5, 0.01, 0.0001):
                local_units.append(order_units(balance, price, q))
    command_disagreements = sum(left != right for left, right in zip(local_commands, probe["native_commands"]))
    unit_disagreements = sum(left != right for left, right in zip(local_units, native["units"]))
    action_parity = (
        probe["action_disagreements"] == 0
        and probe["manifest_codec"] == "NOP_BUY_SELL_CLOSE_OPPOSITE_V1"
        and probe["manifest_schema"] == "structural_sac.inference.v1"
        and command_disagreements == 0
        and unit_disagreements == 0
        and probe["model_sha256"] == "079bef0ea6f3b9327772f4204e5d5dbbd4be69a5ab58c32b424aa3cb7c686404"
    )
    logit_exact = probe["max_abs_error"] == 0.0
    sys.stdout.write(
        json.dumps(
            {
                "ok": action_parity,
                "action_parity": action_parity,
                "logit_exact": logit_exact,
                "model_sha256": probe["model_sha256"],
                "rows": probe["rows"],
                "max_abs_error": probe["max_abs_error"],
                "action_disagreements": probe["action_disagreements"],
                "command_disagreements": command_disagreements,
                "unit_disagreements": unit_disagreements,
                "evaluation_device": "cpu",
                "probe_device": probe.get("probe_device"),
                "probe_torch": probe.get("probe_torch"),
                "mode": "greedy",
                "paper_demo_executing": False,
                "shadow_observing": False,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if action_parity else 2


if __name__ == "__main__":
    raise SystemExit(main())
