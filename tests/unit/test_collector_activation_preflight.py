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
    # C16: the review is a SEALED ACTA artifact on disk; a textual
    # reference does not authorize
    acta_body = {
        "differs_only_by": "session_evidence_publication",
        "reviewer_identity": "musashi",
        "review_reference": "audit-doc-ref",
        "diff_path": diff.name,
        "diff_sha256": hashlib.sha256(
            diff.read_bytes()).hexdigest()}
    acta_body["seal_sha256"] = _canonical_digest(
        {k: v for k, v in acta_body.items()
         if k != "seal_sha256"})
    acta = root / "review_acta.json"
    acta.write_text(json.dumps(acta_body, sort_keys=True))
    review = {
        "acta_path": acta.name,
        "acta_sha256": hashlib.sha256(
            acta.read_bytes()).hexdigest()}
    script = root / "rollback.sh.txt"
    script.write_bytes(b"restore the digest-bound backups")
    rollback = {
        "tested": True, "order_effects": 0,
        "script_path": script.name,
        "script_sha256": hashlib.sha256(
            script.read_bytes()).hexdigest(),
        "backup_manifest_sha256": manifest["seal_sha256"]}
    import os as os_mod
    for path in root.iterdir():
        os_mod.chmod(path, 0o600)
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
                    expected_reviewer_identity="musashi",
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
            expected_reviewer_identity="musashi",
            now=NOW)
        assert result["verdict"] == "COORDINATED_WINDOW_REQUIRED"
        text = json.dumps(result["failures"])
        assert "no heartbeat" in text
        assert "SEALED" in text
        assert "acta" in text, (
            "the invented textual review must fail the acta "
            "requirement")
        assert "not bound to the sealed backup manifest" in text


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
        assert any("does not exist under the root" in f
                   for f in result["failures"])

    def test_a_tampered_artifact_refuses(self, tmp_path):
        kit = real_kit(tmp_path)
        target = kit["backup_root"] / "ea_source.bin"
        target.write_bytes(b"tampered")
        import os as os_mod
        os_mod.chmod(target, 0o600)
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

    def test_textual_reference_does_not_authorize(self, tmp_path):
        result = run(tmp_path, ea_diff_review={
            "reviewed_by": "me, trust me",
            "differs_only_by": "session_evidence_publication"})
        assert any("acta" in f for f in result["failures"])

    def test_wrong_reviewer_identity_refuses(self, tmp_path):
        """The expected identity is FIXED by the order; an acta
        signed by anyone else refuses."""
        kit = real_kit(tmp_path)
        acta = kit["backup_root"] / "review_acta.json"
        body = json.loads(acta.read_text())
        body.pop("seal_sha256")
        body["reviewer_identity"] = "someone_else"
        body["seal_sha256"] = _canonical_digest(body)
        acta.write_text(json.dumps(body, sort_keys=True))
        import os as os_mod
        os_mod.chmod(acta, 0o600)
        kit["ea_diff_review"] = {
            "acta_path": acta.name,
            "acta_sha256": hashlib.sha256(
                acta.read_bytes()).hexdigest()}
        result = run_kit(kit)
        assert any("not the identity fixed by the order" in f
                   for f in result["failures"])

    def test_unsealed_acta_refuses(self, tmp_path):
        kit = real_kit(tmp_path)
        acta = kit["backup_root"] / "review_acta.json"
        body = json.loads(acta.read_text())
        body.pop("seal_sha256")
        acta.write_text(json.dumps(body, sort_keys=True))
        import os as os_mod
        os_mod.chmod(acta, 0o600)
        kit["ea_diff_review"] = {
            "acta_path": acta.name,
            "acta_sha256": hashlib.sha256(
                acta.read_bytes()).hexdigest()}
        result = run_kit(kit)
        assert any("SEALED" in f for f in result["failures"])

    def test_diff_content_mismatch_refuses(self, tmp_path):
        kit = real_kit(tmp_path)
        diff = kit["backup_root"] / "ea_session_publication.diff"
        diff.write_bytes(b"other diff")
        import os as os_mod
        os_mod.chmod(diff, 0o600)
        result = run_kit(kit)
        assert any("EXACT digest the sealed acta names" in f
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
        script = kit["backup_root"] / "rollback.sh.txt"
        script.write_bytes(b"changed")
        import os as os_mod
        os_mod.chmod(script, 0o600)
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


class TestC16PathAndDescriptorDiscipline:

    def test_escaping_paths_refuse(self, tmp_path):
        from tools.collector_activation_preflight import (
            ArtifactPathError, _contained_path)
        import pytest as pt
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "outside.bin").write_bytes(b"x")
        for bad in ("/etc/hosts", "../outside.bin",
                    "a/../../outside.bin", "a/./b", ""):
            with pt.raises(ArtifactPathError):
                _contained_path(root, bad)

    def test_symlinked_root_refuses(self, tmp_path):
        from tools.collector_activation_preflight import (
            ArtifactPathError, _contained_path)
        import os as os_mod
        import pytest as pt
        real = tmp_path / "real"
        real.mkdir()
        (real / "a.bin").write_bytes(b"x")
        link = tmp_path / "link"
        os_mod.symlink(real, link)
        with pt.raises(ArtifactPathError, match="non-symlink"):
            _contained_path(link, "a.bin")

    def test_symlinked_artifact_refuses_descriptor_first(
            self, tmp_path):
        from tools.collector_activation_preflight import (
            ArtifactPathError, _sha256_descriptor_first)
        import os as os_mod
        import pytest as pt
        root = tmp_path / "root"
        root.mkdir()
        target = root / "real.bin"
        target.write_bytes(b"x")
        os_mod.chmod(target, 0o600)
        os_mod.symlink(target, root / "alias.bin")
        with pt.raises(ArtifactPathError):
            _sha256_descriptor_first(root, "alias.bin")

    def test_loose_mode_refuses(self, tmp_path):
        from tools.collector_activation_preflight import (
            ArtifactPathError, _sha256_descriptor_first)
        import os as os_mod
        import pytest as pt
        root = tmp_path / "root"
        root.mkdir()
        target = root / "a.bin"
        target.write_bytes(b"x")
        os_mod.chmod(target, 0o666)
        with pt.raises(ArtifactPathError,
                       match="group/other-writable"):
            _sha256_descriptor_first(root, "a.bin")

    def test_escaping_artifact_in_manifest_is_named(self, tmp_path):
        kit = real_kit(tmp_path)
        (tmp_path / "outside.bin").write_bytes(b"x")
        body = {"artifacts": kit["backup_manifest"]["artifacts"][:2]
                + [{"name": "bridge_config",
                    "path": "../outside.bin",
                    "sha256": hashlib.sha256(b"x").hexdigest()}]}
        body["seal_sha256"] = _canonical_digest(body)
        result = run(tmp_path, backup_manifest=body)
        assert any("not a normalized contained relative path" in f
                   for f in result["failures"])

    def test_trade_allowed_is_deliberately_not_required(
            self, tmp_path):
        """C16 DECISION: the collector is read-only and runs under
        least privilege — a heartbeat with trade_allowed=False must
        still satisfy P5."""
        result = run(tmp_path,
                     heartbeat=heartbeat(trade_allowed=False))
        assert result["verdict"] == "GO_READ_ONLY_COLLECTOR_ONLY"
        assert result["failures"] == []


class TestC17ActaDescriptorConsumed:
    """C17: the acta is read from ONE verified descriptor and parsed
    from those returned bytes — never reopened by path. The frozen
    TOCTOU (verify good bytes, consume a swapped replacement) is
    dead."""

    def test_the_reader_returns_bytes_and_their_own_digest(
            self, tmp_path):
        from tools.collector_activation_preflight import (
            _read_descriptor_first)
        import os as os_mod
        root = tmp_path / "r"
        root.mkdir()
        f = root / "a.json"
        f.write_bytes(b'{"k": 1}')
        os_mod.chmod(f, 0o600)
        data, digest = _read_descriptor_first(root, "a.json")
        assert data == b'{"k": 1}'
        assert digest == hashlib.sha256(data).hexdigest()

    def test_evaluate_never_reopens_a_verified_structured_artifact(
            self):
        """STRUCTURAL: the evaluate() body parses the acta from the
        bytes _read_descriptor_first returned and contains no
        path-reopen (read_text/read_bytes/open of a root-relative
        artifact) after verification. The only read_text calls live
        in main() over operator-supplied CLI JSON, outside the
        canonical root."""
        import inspect
        import tools.collector_activation_preflight as mod
        body = inspect.getsource(mod.evaluate)
        assert "read_text" not in body
        assert "read_bytes" not in body
        assert "acta_bytes.decode" in body, (
            "the acta must be parsed from the verified bytes")

    def test_symlinked_acta_is_refused_by_nofollow(self, tmp_path):
        import os as os_mod
        kit = real_kit(tmp_path)
        acta = kit["backup_root"] / "review_acta.json"
        real = kit["backup_root"] / "real_acta.json"
        acta.rename(real)
        os_mod.symlink(real, acta)
        result = run_kit(kit)
        assert any("symlink refused" in f or "acta" in f
                   for f in result["failures"])

    def test_a_rename_swap_to_attacker_bytes_is_not_consumed(
            self, tmp_path):
        """Even a rename swap cannot be consumed: the verified digest
        is computed from the same descriptor whose bytes are parsed,
        so an acta whose declared digest no longer matches the file
        refuses rather than being read."""
        import os as os_mod
        kit = real_kit(tmp_path)
        acta = kit["backup_root"] / "review_acta.json"
        attacker = dict(json.loads(acta.read_text()))
        attacker.pop("seal_sha256", None)
        attacker["reviewer_identity"] = "attacker"
        attacker["seal_sha256"] = _canonical_digest(attacker)
        # the declared digest still names the ORIGINAL acta; the file
        # now holds attacker bytes
        acta.write_text(json.dumps(attacker, sort_keys=True))
        os_mod.chmod(acta, 0o600)
        result = run_kit(kit)
        assert result["verdict"] == "COORDINATED_WINDOW_REQUIRED"
        assert any("does not hash to its declared digest" in f
                   for f in result["failures"])
        # the attacker reviewer was NEVER consumed
        assert not any("attacker" in f for f in result["failures"])

    def test_malformed_verified_bytes_refuse(self, tmp_path):
        import os as os_mod
        kit = real_kit(tmp_path)
        acta = kit["backup_root"] / "review_acta.json"
        acta.write_bytes(b"not json at all")
        os_mod.chmod(acta, 0o600)
        kit["ea_diff_review"] = {
            "acta_path": acta.name,
            "acta_sha256": hashlib.sha256(
                acta.read_bytes()).hexdigest()}
        result = run_kit(kit)
        assert any("verified acta bytes are malformed" in f
                   for f in result["failures"])

    def test_digest_mismatch_refuses_before_parsing(self, tmp_path):
        kit = real_kit(tmp_path)
        review = dict(kit["ea_diff_review"], acta_sha256="e" * 64)
        result = run_kit({**kit, "ea_diff_review": review})
        assert any("does not hash to its declared digest" in f
                   for f in result["failures"])

    def test_the_clean_go_path_still_holds(self, tmp_path):
        result = run(tmp_path)
        assert result["verdict"] == "GO_READ_ONLY_COLLECTOR_ONLY"
        assert result["failures"] == []
