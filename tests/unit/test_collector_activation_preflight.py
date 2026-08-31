"""C15: the activation judge refuses invented JSON. The frozen PRE
counterexample (bare dicts, digests hashing nothing, no heartbeat)
is a permanent refusal regression; a GO requires REAL artifacts that
exist and hash to their sealed manifests, a bound fresh heartbeat,
review identity with a content digest, and digest-bound rollback.
The judge still cannot act, and an open position always answers
COORDINATED_WINDOW_REQUIRED."""
from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timedelta, timezone

from tools.collector_activation_preflight import (
    _canonical_digest, evaluate)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
FP_A = "a" * 16
FP_S = "b" * 16
BUILD = 4620


def snapshot(**kw):
    base = {
        "schema": "lts.mt5_snapshot.v1",
        "account_fingerprint": FP_A,
        "observed_at": NOW.isoformat(),
        "currency": "USD",
        "balance": 1000.0, "equity": 1000.0,
        "margin": 0.0, "free_margin": 1000.0,
        "positions": [], "orders": [],
        "symbols": [{"symbol": "ETHUSD", "bid": 100.0,
                     "ask": 100.1, "point": 0.01,
                     "volume_min": 0.01, "volume_max": 10.0,
                     "volume_step": 0.01, "trade_mode": 4,
                     "observed_at": NOW.isoformat()}],
        "bars": [],
    }
    base.update(kw)
    return base


def heartbeat(**kw):
    base = {
        "schema": "lts.mt5_heartbeat.v1",
        "adapter_version": "1.4",
        "account_fingerprint": FP_A,
        "server_fingerprint": FP_S,
        "environment": "demo",
        "connected": True,
        "trade_allowed": True,
        "terminal_build": BUILD,
        "terminal_ping_ms": 20.0,
        "observed_at": NOW.isoformat(),
    }
    base.update(kw)
    return base


def real_kit(tmp_path):
    """REAL artifacts: files on disk whose hashes the manifests
    declare, sealed, cross-bound."""
    root = tmp_path / "backup"
    root.mkdir(exist_ok=True)
    artifacts = []
    for name in ("ea_source", "ea_compiled", "bridge_config"):
        path = root / f"{name}.bin"
        path.write_bytes(f"real {name} bytes".encode())
        artifacts.append({
            "name": name, "path": f"{name}.bin",
            "sha256": hashlib.sha256(
                path.read_bytes()).hexdigest()})
    manifest = {"artifacts": artifacts}
    manifest["seal_sha256"] = _canonical_digest(manifest)
    diff = root / "ea_session_publication.diff"
    diff.write_bytes(b"+ publish sessions only")
    review = {
        "differs_only_by": "session_evidence_publication",
        "reviewed_by": {"identity": "auditor",
                        "review_reference": "audit-doc-ref"},
        "diff_path": diff.name,
        "diff_sha256": hashlib.sha256(
            diff.read_bytes()).hexdigest()}
    script = root / "rollback.sh.txt"
    script.write_bytes(b"restore the digest-bound backups")
    rollback = {
        "tested": True, "order_effects": 0,
        "script_path": script.name,
        "script_sha256": hashlib.sha256(
            script.read_bytes()).hexdigest(),
        "backup_manifest_sha256": manifest["seal_sha256"]}
    return {"heartbeat": heartbeat(),
            "backup_manifest": manifest, "backup_root": root,
            "ea_diff_review": review,
            "rollback_evidence": rollback}


def run_kit(kit, snap=None):
    return evaluate(snap or snapshot(),
                    expected_account_fingerprint=FP_A,
                    expected_server_fingerprint=FP_S,
                    expected_symbol="ETHUSD",
                    expected_terminal_build=BUILD,
                    now=NOW, **kit)


def run(tmp_path, snap=None, **overrides):
    kit = real_kit(tmp_path)
    kit.update(overrides)
    return run_kit(kit, snap)


class TestFrozenCounterexample:

    def test_invented_json_no_longer_gets_go(self, tmp_path):
        """PRE FROZEN (C15): this exact invented kit obtained
        GO_READ_ONLY_COLLECTOR_ONLY with zero failures. It must
        refuse forever."""
        result = evaluate(
            snapshot(symbols=[]),
            expected_account_fingerprint=FP_A,
            expected_server_fingerprint=FP_S,
            expected_symbol="ETHUSD",
            expected_terminal_build=BUILD,
            heartbeat=None,
            backup_manifest={"artifacts": [
                {"name": "anything", "sha256": "b" * 64}]},
            backup_root=None,
            ea_diff_review={
                "differs_only_by": "session_evidence_publication",
                "reviewed_by": "me, trust me"},
            rollback_evidence={"tested": True, "order_effects": 0},
            now=NOW)
        assert result["verdict"] == "COORDINATED_WINDOW_REQUIRED"
        text = json.dumps(result["failures"])
        assert "no heartbeat" in text
        assert "SEALED" in text
        assert "binds nothing" in text or "hashes nothing" in text


class TestGoRequiresRealArtifacts:

    def test_the_real_kit_is_go(self, tmp_path):
        result = run(tmp_path)
        assert result["failures"] == []
        assert result["verdict"] == "GO_READ_ONLY_COLLECTOR_ONLY"

    def test_a_digest_hashing_nothing_refuses(self, tmp_path):
        kit = real_kit(tmp_path)
        manifest = kit["backup_manifest"]
        body = {"artifacts": manifest["artifacts"][:2] + [{
            "name": "bridge_config", "path": "missing.bin",
            "sha256": "c" * 64}]}
        body["seal_sha256"] = _canonical_digest(
            {"artifacts": body["artifacts"]})
        result = run(tmp_path, backup_manifest=body)
        assert any("hashes nothing" in f
                   for f in result["failures"])

    def test_a_tampered_artifact_refuses(self, tmp_path):
        kit = real_kit(tmp_path)
        (kit["backup_root"] / "ea_source.bin").write_bytes(
            b"tampered")
        result = run_kit(kit)
        assert any("invented or stale" in f
                   for f in result["failures"])

    def test_an_unsealed_or_reforged_manifest_refuses(self,
                                                      tmp_path):
        kit = real_kit(tmp_path)
        unsealed = {"artifacts": kit["backup_manifest"]["artifacts"]}
        result = run(tmp_path, backup_manifest=unsealed)
        assert any("SEALED" in f for f in result["failures"])
        edited = dict(kit["backup_manifest"])
        edited["artifacts"] = list(edited["artifacts"])[:-1]
        result = run(tmp_path, backup_manifest=edited)
        assert any("seal digest does not match" in f
                   for f in result["failures"])

    def test_missing_required_artifact_refuses(self, tmp_path):
        kit = real_kit(tmp_path)
        body = {"artifacts": kit["backup_manifest"]["artifacts"][:2]}
        body["seal_sha256"] = _canonical_digest(body)
        result = run(tmp_path, backup_manifest=body)
        assert any("required artifacts missing" in f
                   for f in result["failures"])

    def test_uppercase_digest_refuses(self, tmp_path):
        kit = real_kit(tmp_path)
        body = {"artifacts": [dict(kit["backup_manifest"]
                                   ["artifacts"][0],
                                   sha256="B" * 64)]}
        body["seal_sha256"] = _canonical_digest(body)
        result = run(tmp_path, backup_manifest=body)
        assert any("canonical lowercase" in f
                   for f in result["failures"])


class TestHeartbeatBinding:

    def test_missing_heartbeat_refuses(self, tmp_path):
        result = run(tmp_path, heartbeat=None)
        assert any("no heartbeat" in f for f in result["failures"])

    def test_foreign_server_refuses(self, tmp_path):
        result = run(tmp_path,
                     heartbeat=heartbeat(server_fingerprint="z" * 16))
        assert any("foreign server" in f
                   for f in result["failures"])

    def test_disconnected_or_wrong_build_refuses(self, tmp_path):
        result = run(tmp_path, heartbeat=heartbeat(connected=False))
        assert any("not connected" in f
                   for f in result["failures"])
        result = run(tmp_path,
                     heartbeat=heartbeat(terminal_build=1))
        assert any("terminal build" in f
                   for f in result["failures"])

    def test_stale_heartbeat_refuses(self, tmp_path):
        result = run(tmp_path, heartbeat=heartbeat(
            observed_at=(NOW - timedelta(minutes=10)).isoformat()))
        assert any("not fresh" in f for f in result["failures"])

    def test_unpublished_symbol_refuses(self, tmp_path):
        result = run(tmp_path, snap=snapshot(symbols=[]))
        assert any("does not publish" in f
                   for f in result["failures"])


class TestReviewAndRollbackBinding:

    def test_bare_reviewer_name_refuses(self, tmp_path):
        kit = real_kit(tmp_path)
        review = dict(kit["ea_diff_review"],
                      reviewed_by="me, trust me")
        result = run(tmp_path, ea_diff_review=review)
        assert any("binds nothing" in f
                   for f in result["failures"])

    def test_diff_content_mismatch_refuses(self, tmp_path):
        kit = real_kit(tmp_path)
        (kit["backup_root"] /
         "ea_session_publication.diff").write_bytes(b"other diff")
        result = run_kit(kit)
        assert any("does not hash to the reviewed digest" in f
                   for f in result["failures"])

    def test_rollback_unbound_to_the_manifest_refuses(self,
                                                      tmp_path):
        kit = real_kit(tmp_path)
        rollback = dict(kit["rollback_evidence"],
                        backup_manifest_sha256="d" * 64)
        result = run(tmp_path, rollback_evidence=rollback)
        assert any("not bound to the sealed backup manifest" in f
                   for f in result["failures"])

    def test_rollback_script_mismatch_refuses(self, tmp_path):
        kit = real_kit(tmp_path)
        (kit["backup_root"] / "rollback.sh.txt").write_bytes(
            b"changed")
        result = run_kit(kit)
        assert any("does not hash to its declared digest" in f
                   for f in result["failures"])


class TestBlockingInvariants:

    def test_an_open_position_always_blocks(self, tmp_path):
        snap = snapshot(positions=[{
            "ticket": "t1", "symbol": "ETHUSD", "side": "sell",
            "volume": 1.0, "price_open": 100.0,
            "time_open_unix": 1_700_000_000,
            "stop_loss": 120.0, "take_profit": 80.0,
            "profit": 0.0}])
        result = run(tmp_path, snap=snap)
        assert result["verdict"] == "COORDINATED_WINDOW_REQUIRED"
        assert any("never restart or replace" in f
                   for f in result["failures"])

    def test_go_scope_never_covers_trading_logic(self, tmp_path):
        result = run(tmp_path)
        assert "weekly-flat trading logic stays blocked" in \
            result["scope"]

    def test_the_judge_itself_cannot_act(self):
        import tools.collector_activation_preflight as mod
        source = inspect.getsource(mod)
        # strip the judge's OWN forbidden-surface literal (the
        # multi-line tuple in the P6 block) before scanning for
        # real usages: drop pure string-literal lines
        lines = [line for line in source.split("\n")
                 if "forbidden" not in line
                 and not line.strip().startswith('"')]
        cleaned = "\n".join(lines)
        for surface in ("OrderSend", "requests.", "urllib",
                        "socket", "subprocess"):
            assert surface not in cleaned, surface
