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
        profile = tmp_path / "profile.json"
        profile.write_text(json.dumps(_profile()))
        bars = tmp_path / "bars.json"
        bars.write_text(json.dumps({"bar_open_times_utc": [
            f"2026-08-20T{h:02d}:00:00+03:00" for h in
            (1, 5, 9, 13, 17, 21)] * 2}))
        out = tmp_path / "out.json"
        rc = tool.main(["--profile", str(profile),
                        "--bars-evidence", str(bars),
                        "--out-json", str(out)])
        # +03:00 offset hours 1,5,... are UTC 22,2,... => misaligned
        assert rc == 2
        doc = json.loads(out.read_text())
        assert doc["outcome"] == "REFUSED"

    def test_pass_requires_real_manifest_gate(self, tool, tmp_path,
                                              monkeypatch):
        profile = tmp_path / "profile.json"
        profile.write_text(json.dumps(_profile()))
        bars = tmp_path / "bars.json"
        bars.write_text(json.dumps({"bar_open_times_utc": [
            f"2026-08-2{d}T{h:02d}:00:00+00:00"
            for d in (0, 1) for h in (0, 4, 8, 12, 16, 20)]}))
        out = tmp_path / "out.json"

        class _Gate:
            manifest_sha256 = "m" * 64
            manifest = {"model_id": "usdcad-4h-linear-live-v1",
                        "artifact_sha256": "a" * 64}

        monkeypatch.setattr(tool, "check_manifest_gate",
                            lambda p: _Gate())
        rc = tool.main(["--profile", str(profile),
                        "--bars-evidence", str(bars),
                        "--out-json", str(out)])
        assert rc == 0
        doc = json.loads(out.read_text())
        assert doc["outcome"] == "PASS"
        assert doc["model_id"] == "usdcad-4h-linear-live-v1"
