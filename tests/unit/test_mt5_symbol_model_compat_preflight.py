"""§2.5 compatibility preflight tests (order 2026-08-23 P0)."""
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "mt5_symbol_model_compat_preflight",
        REPO / "tools" / "mt5_symbol_model_compat_preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _load()


def _envelope(**over):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(minute=0, second=0,
                                             microsecond=0)
    start = now - timedelta(hours=4 * 14)
    start = start.replace(hour=(start.hour // 4) * 4)
    bars = []
    for i in range(14):
        t = start + timedelta(hours=4 * i)
        bars.append({"t": t.isoformat(), "o": 1.34, "h": 1.35,
                     "l": 1.33, "c": 1.345, "v": 100})
    doc = {"schema": "lts.mt5.bars_evidence.v1",
           "account_fingerprint": "a" * 24, "symbol": "USDCAD",
           "timeframe": "H4",
           "captured_at_utc": now.isoformat(),
           "symbol_facts": {"trade_mode": 4, "volume_min": 0.01,
                            "volume_step": 0.01, "volume_max": 100.0,
                            "digits": 5, "point": 1e-5},
           "bars": bars}
    doc.update(over)
    return doc


def _store_envelope(db_path, doc):
    import hashlib, sys
    sys.path.insert(0, str(REPO))
    from app.mt5_execution_bridge import Mt5ExecutionStore
    store = Mt5ExecutionStore(db_path)
    payload = json.dumps(doc)
    store.record_bars_evidence(
        symbol=doc["symbol"], payload=payload,
        digest=hashlib.sha256(payload.encode()).hexdigest())


def _profile(**over):
    base = {
        "schema": "lts.mt5.model_runner.v1",
        "model": {"manifest_file": "/x/manifest.json",
                  "expected_asset_id": "fx:USD/CAD",
                  "expected_timeframe": "4h",
                  "execution_tier": "demo_research_canary"},
        "route": {"symbol": "USDCAD", "timeframe": "4h"},
        "ea_magic": 26080302,
        "service": {"asset_instrument_bindings":
                    {"fx:USD/CAD": "USDCAD"}},
    }
    for key, value in over.items():
        if isinstance(value, dict) and key in base:
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


class TestProfileChecks:
    def test_valid_profile_passes(self, tool):
        facts = tool.check_profile(_profile())
        assert facts == {"symbol": "USDCAD", "asset": "fx:USD/CAD",
                         "magic": 26080302}

    def test_undeclared_asset_binding_refuses(self, tool):
        with pytest.raises(tool.CompatRefused, match="DECLARED"):
            tool.check_profile(_profile(
                service={"asset_instrument_bindings": {}}))

    def test_timeframe_disagreement_refuses(self, tool):
        with pytest.raises(tool.CompatRefused, match="disagree"):
            tool.check_profile(_profile(
                route={"symbol": "USDCAD", "timeframe": "1h"}))

    def test_missing_magic_refuses(self, tool):
        with pytest.raises(tool.CompatRefused, match="ea_magic"):
            tool.check_profile(_profile(ea_magic=None))

    def test_missing_magic_in_compared_profile_refuses(self, tool):
        """304: no defaults in validation — an undeclared magic
        REFUSES instead of being guessed."""
        eth = {"route": {"symbol": "ETHUSD"}}
        with pytest.raises(tool.CompatRefused, match="declare"):
            tool.check_magic_unique(_profile(), [eth])

    def test_magic_collision_refuses(self, tool):
        eth = {"route": {"symbol": "ETHUSD"}, "ea_magic": 26080301}
        tool.check_magic_unique(_profile(), [eth])
        with pytest.raises(tool.CompatRefused, match="collides"):
            tool.check_magic_unique(
                _profile(ea_magic=26080301), [eth])


class TestBarTiming:
    def _evidence(self, hours, minute=0):
        return {"bar_open_times_utc": [
            f"2026-08-2{d}T{h:02d}:{minute:02d}:00+00:00"
            for d in (0, 1) for h in hours]}

    def test_utc_aligned_h4_passes(self, tool):
        out = tool.check_bar_timing(
            self._evidence([0, 4, 8, 12, 16, 20]), timeframe="4h")
        assert out["utc_aligned"] is True

    def test_shifted_server_time_refuses(self, tool):
        with pytest.raises(tool.CompatRefused,
                           match="not UTC-aligned"):
            tool.check_bar_timing(
                self._evidence([1, 5, 9, 13, 17, 21]), timeframe="4h")

    def test_offset_minutes_refuse(self, tool):
        with pytest.raises(tool.CompatRefused,
                           match="not UTC-aligned"):
            tool.check_bar_timing(
                self._evidence([0, 4, 8, 12, 16, 20], minute=30),
                timeframe="4h")

    def test_naive_timestamps_refuse(self, tool):
        with pytest.raises(tool.CompatRefused, match="timezone"):
            tool.check_bar_timing(
                {"bar_open_times_utc":
                 ["2026-08-20T00:00:00"] * 12}, timeframe="4h")

    def test_thin_sample_refuses(self, tool):
        with pytest.raises(tool.CompatRefused, match="thin"):
            tool.check_bar_timing(
                {"bar_open_times_utc":
                 ["2026-08-20T00:00:00+00:00"]}, timeframe="4h")


class TestEndToEnd:
    def test_refusal_is_typed_and_exit_2(self, tool, tmp_path,
                                         monkeypatch):
        """A hand-written evidence file has no attestation path at
        all now — an absent envelope refuses."""
        profile = tmp_path / "profile.json"
        profile.write_text(json.dumps(dict(_profile(),
                                           order_volume=0.01)))
        bridge = tmp_path / "bridge.json"
        bridge.write_text(json.dumps({
            "allowed_symbols": ["ETHUSD", "USDCAD"],
            "symbol_magics": {"ETHUSD": 26080301, "USDCAD": 26080302},
            "max_volume": 0.01, "max_open_commands_per_day": 4,
            "account_fingerprint": "a" * 24}))
        out = tmp_path / "out.json"
        rc = tool.main(["--profile", str(profile),
                        "--bridge-db", str(tmp_path / "empty.sqlite"),
                        "--bridge-config", str(bridge),
                        "--out-json", str(out)])
        assert rc == 2
        doc = json.loads(out.read_text())
        assert doc["outcome"] == "REFUSED"
        assert "attested" in doc["reason"]

    def test_pass_requires_real_manifest_gate(self, tool, tmp_path,
                                              monkeypatch):
        profile = tmp_path / "profile.json"
        profile.write_text(json.dumps(dict(_profile(),
                                           order_volume=0.01)))
        bridge = tmp_path / "bridge.json"
        bridge.write_text(json.dumps({
            "allowed_symbols": ["ETHUSD", "USDCAD"],
            "symbol_magics": {"ETHUSD": 26080301, "USDCAD": 26080302},
            "max_volume": 0.01, "max_open_commands_per_day": 4,
            "account_fingerprint": "a" * 24}))
        db = tmp_path / "bridge.sqlite"
        _store_envelope(db, _envelope())
        out = tmp_path / "out.json"

        class _Gate:
            manifest_sha256 = "m" * 64
            manifest = {"model_id": "usdcad-4h-linear-live-v1",
                        "artifact_sha256": "a" * 64}

        monkeypatch.setattr(tool, "check_manifest_gate",
                            lambda p: _Gate())
        rc = tool.main(["--profile", str(profile),
                        "--bridge-db", str(db),
                        "--bridge-config", str(bridge),
                        "--out-json", str(out)])
        assert rc == 0
        doc = json.loads(out.read_text())
        assert doc["outcome"] == "PASS"
        assert doc["model_id"] == "usdcad-4h-linear-live-v1"


class TestBridgeBinding:
    """AUD-F2-20260823-303."""

    def _bridge(self, tmp_path, **over):
        doc = {"allowed_symbols": ["ETHUSD", "USDCAD"],
               "symbol_magics": {"ETHUSD": 26080301,
                                 "USDCAD": 26080302},
               "max_volume": 0.01, "max_open_commands_per_day": 4}
        doc.update(over)
        path = tmp_path / "bridge.json"
        path.write_text(json.dumps(doc))
        return path

    def _profile(self):
        return dict(_profile(), order_volume=0.01)

    def test_valid_binding_passes(self, tool, tmp_path):
        out = tool.check_bridge_binding(
            self._profile(), expected_symbols=["ETHUSD", "USDCAD"],
            bridge_config_path=self._bridge(tmp_path))
        assert out["declared_chart_magic"] == 26080302
        assert len(out["bridge_config_sha256"]) == 64

    def test_missing_mandate_symbol_refuses(self, tool, tmp_path):
        with pytest.raises(tool.CompatRefused, match="lacks symbols"):
            tool.check_bridge_binding(
                self._profile(),
                expected_symbols=["ETHUSD", "USDCAD"],
                bridge_config_path=self._bridge(
                    tmp_path, allowed_symbols=["ETHUSD"]))

    def test_magic_mismatch_refuses(self, tool, tmp_path):
        with pytest.raises(tool.CompatRefused, match="chart magic"):
            tool.check_bridge_binding(
                self._profile(),
                expected_symbols=["USDCAD"],
                bridge_config_path=self._bridge(
                    tmp_path,
                    symbol_magics={"ETHUSD": 26080301,
                                   "USDCAD": 999}))

    def test_volume_over_ceiling_refuses(self, tool, tmp_path):
        profile = self._profile()
        profile["order_volume"] = 0.05
        with pytest.raises(tool.CompatRefused, match="ceiling"):
            tool.check_bridge_binding(
                profile, expected_symbols=["USDCAD"],
                bridge_config_path=self._bridge(tmp_path))

    def test_account_identifier_never_in_result(self, tool, tmp_path):
        bridge = self._bridge(
            tmp_path, account_fingerprint="c" * 24)
        out = tool.check_bridge_binding(
            self._profile(), expected_symbols=["USDCAD"],
            bridge_config_path=bridge)
        assert "c" * 24 not in json.dumps(out)


class TestSymbolFacts:
    """AUD-F2-20260823-303: executable symbol facts."""

    def _facts(self, **over):
        base = {"trade_mode": 4, "volume_min": 0.01,
                "volume_step": 0.01, "volume_max": 100.0,
                "digits": 5, "point": 1e-5}
        base.update(over)
        return {"symbol_facts": base}

    def test_valid_facts_pass(self, tool):
        out = tool.check_symbol_facts(self._facts(),
                                      order_volume=0.01)
        assert out["trade_mode"] == 4

    def test_wrong_trade_mode_refuses(self, tool):
        with pytest.raises(tool.CompatRefused, match="trade_mode"):
            tool.check_symbol_facts(self._facts(trade_mode=2),
                                    order_volume=0.01)

    def test_step_misalignment_refuses(self, tool):
        with pytest.raises(tool.CompatRefused, match="step"):
            tool.check_symbol_facts(
                self._facts(volume_step=0.02),
                order_volume=0.02)  # min 0.01 + n*0.02 never hits 0.02

    def test_missing_facts_refuse(self, tool):
        with pytest.raises(tool.CompatRefused, match="symbol_facts"):
            tool.check_symbol_facts({}, order_volume=0.01)

    def test_below_min_refuses(self, tool):
        with pytest.raises(tool.CompatRefused, match="bounds"):
            tool.check_symbol_facts(self._facts(volume_min=0.1),
                                    order_volume=0.01)



class TestAttestedEvidence:
    """AUD-F2-20260823-302: only stored, signed envelopes count."""

    def _load_ok(self, tool, tmp_path, doc=None):
        db = tmp_path / "b.sqlite"
        _store_envelope(db, doc or _envelope())
        return tool.load_attested_evidence(
            db, symbol="USDCAD", account_fingerprint="a" * 24,
            max_age_hours=48)

    def test_fresh_envelope_loads(self, tool, tmp_path):
        doc = self._load_ok(tool, tmp_path)
        assert len(doc["bar_open_times_utc"]) == 14
        assert len(doc["_storage_digest"]) == 64

    def test_absent_envelope_refuses(self, tool, tmp_path):
        with pytest.raises(tool.CompatRefused, match="attested"):
            tool.load_attested_evidence(
                tmp_path / "none.sqlite", symbol="USDCAD",
                account_fingerprint="a" * 24, max_age_hours=48)

    def test_stale_capture_refuses(self, tool, tmp_path):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc)
               - timedelta(hours=100)).isoformat()
        with pytest.raises(tool.CompatRefused, match="stale"):
            self._load_ok(tool, tmp_path,
                          _envelope(captured_at_utc=old))

    def test_gap_in_bars_refuses(self, tool, tmp_path):
        doc = _envelope()
        del doc["bars"][5]
        doc["bars"].append(dict(doc["bars"][-1]))
        doc["bars"][-1]["t"] = doc["bars"][-1]["t"]  # keep count>=12
        with pytest.raises(tool.CompatRefused,
                           match="not exactly 4h|duplicate"):
            self._load_ok(tool, tmp_path, doc)

    def test_duplicate_bar_refuses(self, tool, tmp_path):
        doc = _envelope()
        doc["bars"][3] = dict(doc["bars"][2])
        with pytest.raises(tool.CompatRefused,
                           match="duplicate|not exactly"):
            self._load_ok(tool, tmp_path, doc)

    def test_broken_ohlc_geometry_refuses(self, tool, tmp_path):
        doc = _envelope()
        doc["bars"][4]["l"] = 2.0  # low above high
        with pytest.raises(tool.CompatRefused, match="geometry"):
            self._load_ok(tool, tmp_path, doc)

    def test_wrong_symbol_refuses(self, tool, tmp_path):
        db = tmp_path / "b.sqlite"
        _store_envelope(db, _envelope(symbol="EURUSD"))
        with pytest.raises(tool.CompatRefused, match="attested"):
            tool.load_attested_evidence(
                db, symbol="USDCAD", account_fingerprint="a" * 24,
                max_age_hours=48)

    def test_foreign_account_refuses(self, tool, tmp_path):
        with pytest.raises(tool.CompatRefused, match="account"):
            self._load_ok(
                tool, tmp_path,
                _envelope(account_fingerprint="b" * 24))
