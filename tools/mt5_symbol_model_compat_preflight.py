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
   on UTC-aligned bars);
5. bridge binding (AUD-F2-20260823-303): the EFFECTIVE bridge config
   named by the profile is loaded and hashed (the account identifier
   never enters the result); both route symbols must be in its
   mandate; the profile magic must equal the config's declared chart
   magic; the declared order volume must fit the Demo ceiling;
6. symbol facts (303): trade mode, volume min/step/max, digits and
   point bind into the executable result; an order volume not aligned
   to the broker step REFUSES.
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


def check_bridge_binding(profile: dict, *, expected_symbols,
                         bridge_config_path=None) -> dict:
    """AUD-F2-20260823-303: executable bridge-config compatibility."""
    path = _expand(str(bridge_config_path
                       or profile.get("bridge_config_file") or ""))
    if not path.is_file():
        raise CompatRefused(f"bridge config not found at {path}")
    raw = path.read_bytes()
    import hashlib
    digest = hashlib.sha256(raw).hexdigest()
    doc = json.loads(raw)
    allowed = {str(v).upper() for v in doc.get("allowed_symbols", [])}
    missing = [s_ for s_ in expected_symbols if s_ not in allowed]
    if missing:
        raise CompatRefused(
            f"bridge mandate lacks symbols {missing}; dual-symbol "
            "operation requires both")
    symbol = str((profile.get("route") or {}).get("symbol") or "")
    magics = doc.get("symbol_magics") or {}
    declared = magics.get(symbol)
    if declared is None:
        raise CompatRefused(
            f"bridge config declares no chart magic for {symbol}")
    if declared != profile.get("ea_magic"):
        raise CompatRefused(
            f"profile ea_magic {profile.get('ea_magic')} != bridge "
            f"declared chart magic {declared} for {symbol}")
    max_volume = float(doc.get("max_volume") or 0)
    order_volume = profile.get("order_volume")
    if not isinstance(order_volume, (int, float))             or isinstance(order_volume, bool) or order_volume <= 0:
        raise CompatRefused("profile must declare a positive "
                            "order_volume")
    if float(order_volume) > max_volume:
        raise CompatRefused(
            f"order_volume {order_volume} exceeds the bridge Demo "
            f"ceiling {max_volume}")
    return {"bridge_config_sha256": digest,
            "mandate_symbols": sorted(allowed),
            "declared_chart_magic": declared,
            "max_volume": max_volume,
            "daily_open_budget": doc.get("max_open_commands_per_day")}


def check_symbol_facts(evidence: dict, *, order_volume: float) -> dict:
    """AUD-F2-20260823-303: direct symbol facts become executable
    acceptance, not prose."""
    facts = evidence.get("symbol_facts")
    if not isinstance(facts, dict):
        raise CompatRefused("evidence lacks symbol_facts; trade mode "
                            "and volume geometry are unproven")
    if facts.get("trade_mode") != 4:
        raise CompatRefused(
            f"symbol trade_mode {facts.get('trade_mode')!r} != 4 "
            "(full access); refusing")
    vmin = facts.get("volume_min")
    vstep = facts.get("volume_step")
    vmax = facts.get("volume_max")
    for name, value in (("volume_min", vmin), ("volume_step", vstep),
                        ("volume_max", vmax)):
        if not isinstance(value, (int, float))                 or isinstance(value, bool) or value <= 0:
            raise CompatRefused(f"symbol fact {name} missing/invalid")
    if not (vmin <= order_volume <= vmax):
        raise CompatRefused(
            f"order_volume {order_volume} outside broker bounds "
            f"[{vmin}, {vmax}]")
    steps = round((order_volume - vmin) / vstep)
    if abs((vmin + steps * vstep) - order_volume) > 1e-9:
        raise CompatRefused(
            f"order_volume {order_volume} is not aligned to the "
            f"broker volume step {vstep}; refusing rather than "
            "rounding silently")
    for name in ("digits", "point"):
        if facts.get(name) in (None, 0):
            raise CompatRefused(f"symbol fact {name} missing")
    return {"trade_mode": 4, "volume_min": vmin, "volume_step": vstep,
            "volume_max": vmax, "digits": facts["digits"],
            "point": facts["point"]}


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
    parser.add_argument("--bridge-config", type=Path, default=None)
    parser.add_argument("--expected-symbols",
                        default="ETHUSD,USDCAD")
    parser.add_argument("--out-json", type=Path, required=True)
    args = parser.parse_args(argv)
    profile = json.loads(args.profile.read_text())
    others = [json.loads(p.read_text()) for p in args.other_profile]
    try:
        facts = check_profile(profile)
        check_magic_unique(profile, others)
        evidence = json.loads(args.bars_evidence.read_text())
        timing = check_bar_timing(
            evidence,
            timeframe=profile["model"]["expected_timeframe"])
        bridge = check_bridge_binding(
            profile,
            expected_symbols=[v.strip().upper() for v in
                              args.expected_symbols.split(",")
                              if v.strip()],
            bridge_config_path=args.bridge_config)
        symbol_facts = check_symbol_facts(
            evidence, order_volume=float(profile["order_volume"]))
        policy = check_manifest_gate(profile)
        result = {
            "schema": "lts.mt5.symbol_model_compat_preflight.v1",
            "outcome": "PASS",
            "route": facts,
            "bar_timing": timing,
            "bridge_binding": bridge,
            "symbol_facts": symbol_facts,
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
