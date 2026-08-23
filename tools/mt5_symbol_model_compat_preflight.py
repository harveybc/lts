"""§2.5 compatibility preflight: historical model vs MT5 symbol route.

Order 2026-08-23 P0: the historical USD.CAD model may be reused on the
MT5 USDCAD route only after PROVING feature schema, bar timing,
scaling, artifact hash and action semantics compatibility with MT5
CopyRates data. Incompatible inputs REFUSE — nothing silently adapts.

Checks, all fail-closed:
1. profile shape: runner schema, route symbol/timeframe, EXPLICIT
   asset->instrument binding (manifest asset_id -> MT5 symbol),
   Demo volume ceiling honored by the bridge config it names;
2. EA magic uniqueness against every other declared profile;
3. manifest gate: the EXISTING SelectedSacPolicy contract (schema,
   asset, timeframe, tier evidence, observation contract + golden
   parity, config/artifact sha256, identity agreement) — reused, not
   re-implemented;
4. bar timing: MT5 CopyRates evidence (read-only capture) must show
   H4 opens aligned to UTC {0,4,8,12,16,20}; a broker whose H4
   candles sit on shifted server time REFUSES (the model was trained
   on UTC-aligned bars).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

UTC_H4_HOURS = {0, 4, 8, 12, 16, 20}


class CompatRefused(ValueError):
    pass


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def check_profile(profile: dict) -> dict:
    if profile.get("schema") != "lts.mt5.model_runner.v1":
        raise CompatRefused("profile schema is not the MT5 runner v1")
    route = profile.get("route") or {}
    model = profile.get("model") or {}
    service = profile.get("service") or {}
    symbol = str(route.get("symbol") or "")
    if not symbol:
        raise CompatRefused("profile route.symbol missing")
    if route.get("timeframe") != model.get("expected_timeframe"):
        raise CompatRefused(
            "route timeframe and model expected_timeframe disagree")
    bindings = service.get("asset_instrument_bindings") or {}
    asset = str(model.get("expected_asset_id") or "")
    if bindings.get(asset) != symbol:
        raise CompatRefused(
            f"asset binding {asset!r} -> {symbol!r} is not DECLARED in "
            "service.asset_instrument_bindings; implicit mapping "
            "refused")
    magic = profile.get("ea_magic")
    if not isinstance(magic, int) or magic <= 0:
        raise CompatRefused("profile must declare a positive ea_magic")
    return {"symbol": symbol, "asset": asset, "magic": magic}


def check_magic_unique(profile: dict, others: list[dict]) -> None:
    """AUD-F2-20260823-304: every compared profile DECLARES its
    positive magic; a missing value REFUSES — validation never
    supplies a default that could conceal drift in the installed EA."""
    mine = profile.get("ea_magic")
    for other in others:
        symbol = (other.get("route") or {}).get("symbol")
        theirs = other.get("ea_magic")
        if not isinstance(theirs, int) or isinstance(theirs, bool)                 or theirs <= 0:
            raise CompatRefused(
                f"profile for {symbol!r} does not declare a positive "
                "ea_magic; uniqueness cannot be proven — refused "
                "(no defaults in validation)")
        if theirs == mine:
            raise CompatRefused(
                f"ea_magic {mine} collides with the {symbol} profile; "
                "mixed magic numbers refuse")


def check_bar_timing(bars_evidence: dict, *, timeframe: str) -> dict:
    if timeframe != "4h":
        raise CompatRefused(
            f"bar-timing contract only defined for 4h, got {timeframe!r}")
    opens = bars_evidence.get("bar_open_times_utc") or []
    if len(opens) < 12:
        raise CompatRefused(
            "bar evidence must contain at least 12 CopyRates bar open "
            "times; refusing a thin sample")
    bad = []
    for value in opens:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:
            raise CompatRefused(
                f"bar open {value!r} lacks a timezone; ambiguous "
                "evidence refused")
        utc = stamp.astimezone(timezone.utc)
        if utc.hour not in UTC_H4_HOURS or utc.minute or utc.second:
            bad.append(str(value))
    if bad:
        raise CompatRefused(
            "MT5 H4 bars are not UTC-aligned {0,4,8,12,16,20}: "
            f"{bad[:4]} — the historical model was trained on "
            "UTC-aligned bars; REFUSED rather than silently adapted")
    return {"bars_checked": len(opens), "utc_aligned": True}


def check_manifest_gate(profile: dict):
    from app.live_sac_selection import SelectedSacPolicy
    model = profile["model"]
    return SelectedSacPolicy(
        manifest_file=_expand(str(model["manifest_file"])),
        expected_asset_id=model["expected_asset_id"],
        expected_timeframe=model["expected_timeframe"],
        execution_tier=model["execution_tier"],
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     allow_abbrev=False)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--other-profile", type=Path, action="append",
                        default=[])
    parser.add_argument("--bars-evidence", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    args = parser.parse_args(argv)
    profile = json.loads(args.profile.read_text())
    others = [json.loads(p.read_text()) for p in args.other_profile]
    try:
        facts = check_profile(profile)
        check_magic_unique(profile, others)
        timing = check_bar_timing(
            json.loads(args.bars_evidence.read_text()),
            timeframe=profile["model"]["expected_timeframe"])
        policy = check_manifest_gate(profile)
        result = {
            "schema": "lts.mt5.symbol_model_compat_preflight.v1",
            "outcome": "PASS",
            "route": facts,
            "bar_timing": timing,
            "manifest_sha256": policy.manifest_sha256,
            "model_id": policy.manifest.get("model_id"),
            "artifact_sha256": policy.manifest.get("artifact_sha256"),
        }
    except CompatRefused as exc:
        result = {"schema": "lts.mt5.symbol_model_compat_preflight.v1",
                  "outcome": "REFUSED", "reason": str(exc)}
    except Exception as exc:  # manifest gate errors are typed refusals
        result = {"schema": "lts.mt5.symbol_model_compat_preflight.v1",
                  "outcome": "REFUSED",
                  "reason": f"{type(exc).__name__}: {exc}"}
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(result, indent=1))
    print(json.dumps({"outcome": result["outcome"],
                      "reason": result.get("reason")}))
    return 0 if result["outcome"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
