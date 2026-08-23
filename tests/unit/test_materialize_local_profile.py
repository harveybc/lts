"""AUD-SEC-20260823-305 materializer tests."""
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "materialize_local_profile",
        REPO / "tools" / "materialize_local_profile.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tool():
    return _load()


def test_tracked_examples_carry_no_fingerprints():
    for name in ("mt5_eth_model_runner_v1.json",
                 "mt5_usdcad_model_runner_v1.json",
                 "mt5_execution_bridge_demo_v2.json"):
        text = (REPO / "examples" / "configs" / name).read_text()
        assert "<ACCOUNT_FINGERPRINT_24HEX>" in text
        import re
        assert not re.search(r'"account_fingerprint":\s*"[0-9a-f]{24}"',
                             text)


def test_missing_env_refuses(tool, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LTS_MT5_ACCOUNT_FINGERPRINT", raising=False)
    t = tmp_path / "t.json"
    t.write_text('{"account_fingerprint": "<ACCOUNT_FINGERPRINT_24HEX>"}')
    assert tool.main(["--template", str(t), "--name", "x.json"]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_render_writes_only_under_config_and_never_prints_value(
        tool, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LTS_MT5_ACCOUNT_FINGERPRINT", "a" * 24)
    monkeypatch.setenv("HOME", str(tmp_path))
    t = tmp_path / "t.json"
    t.write_text('{"account_fingerprint": "<ACCOUNT_FINGERPRINT_24HEX>"}')
    assert tool.main(["--template", str(t), "--name", "p.json"]) == 0
    out = capsys.readouterr().out
    assert "a" * 24 not in out
    dest = tmp_path / ".config" / "lts" / "p.json"
    assert json.loads(dest.read_text())["account_fingerprint"] == "a" * 24


def test_path_escape_refused(tool, tmp_path, capsys):
    t = tmp_path / "t.json"
    t.write_text("{}")
    assert tool.main(["--template", str(t),
                      "--name", "../evil.json"]) == 2


class TestNoFollowAtomicInstall:
    """AUD-SEC-20260823-314 adversarial fixtures."""

    def _env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LTS_MT5_ACCOUNT_FINGERPRINT", "a" * 24)
        monkeypatch.setenv("HOME", str(tmp_path))
        t = tmp_path / "t.json"
        t.write_text(
            '{"account_fingerprint": "<ACCOUNT_FINGERPRINT_24HEX>"}')
        cfg = tmp_path / ".config" / "lts"
        cfg.mkdir(parents=True)
        return t, cfg

    def test_symlink_destination_refuses_and_never_leaks(
            self, tool, tmp_path, monkeypatch, capsys):
        t, cfg = self._env(tmp_path, monkeypatch)
        outside = tmp_path / "outside_leak.json"
        (cfg / "p.json").symlink_to(outside)
        rc = tool.main(["--template", str(t), "--name", "p.json"])
        assert rc == 2
        assert "REFUSED" in capsys.readouterr().out
        assert not outside.exists()

    def test_symlinked_config_dir_refuses(self, tool, tmp_path,
                                          monkeypatch, capsys):
        monkeypatch.setenv("LTS_MT5_ACCOUNT_FINGERPRINT", "a" * 24)
        monkeypatch.setenv("HOME", str(tmp_path))
        t = tmp_path / "t.json"
        t.write_text(
            '{"account_fingerprint": "<ACCOUNT_FINGERPRINT_24HEX>"}')
        real = tmp_path / "elsewhere"; real.mkdir()
        (tmp_path / ".config").mkdir()
        (tmp_path / ".config" / "lts").symlink_to(real)
        rc = tool.main(["--template", str(t), "--name", "p.json"])
        assert rc == 2
        assert "symlink" in capsys.readouterr().out

    def test_stale_tmp_race_artifact_refuses(self, tool, tmp_path,
                                             monkeypatch, capsys):
        t, cfg = self._env(tmp_path, monkeypatch)
        (cfg / "p.json.tmp").write_text("stale")
        rc = tool.main(["--template", str(t), "--name", "p.json"])
        assert rc == 2
        assert "temporary file" in capsys.readouterr().out

    def test_success_enforces_0700_dir_and_0600_file(
            self, tool, tmp_path, monkeypatch):
        import stat
        t, cfg = self._env(tmp_path, monkeypatch)
        cfg.chmod(0o755)  # loose perms get tightened
        rc = tool.main(["--template", str(t), "--name", "p.json"])
        assert rc == 0
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o700
        assert stat.S_IMODE((cfg / "p.json").stat().st_mode) == 0o600
        assert not (cfg / "p.json.tmp").exists()
