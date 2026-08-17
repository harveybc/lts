"""Finding 269 adversarial suite (AUD-F2-20260816-269).

The defect: promotion DECLARED activity mandatory while no executable
trading-activity predicate existed in this repository. These tests pin
the correction and the ordered adversarial set (MUSASHI_RESPONSE
2026-08-16, order 3):

- a viable-but-inactive candidate refuses;
- absent activity evidence refuses (typed, never a default pass);
- a mechanics screen verdict presented as evidence refuses even when it
  says VIABLE everywhere and even when it smuggles
  ``promotion_eligible: true``;
- no production caller can bypass: ``activity_report`` is a required
  keyword of ``promote_paper_champion`` with no default, and the
  refusal fires before any capability, drain or manifest work.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.champion_succession import (  # noqa: E402
    ACTIVITY_ARTIFACT_MISMATCH,
    ACTIVITY_EVIDENCE_FROM_MECHANICS_SCREEN,
    ACTIVITY_RECORD_NOT_PROMOTION_ELIGIBLE,
    ACTIVITY_RECORD_SCHEMA_UNSUPPORTED,
    ACTIVITY_STATUS_NOT_ACTIVE,
    NO_ACTIVITY_EVIDENCE,
    VERDICT_ACTIVITY_EVIDENT,
    VERDICT_ACTIVITY_NOT_EVIDENT,
    SuccessionError,
    candidate_activity_report,
    promote_paper_champion,
    require_activity_evidence,
)
from tests.unit.test_champion_succession import (  # noqa: E402
    NOW,
    make_activity_record,
    make_candidate,
    promote,
    valid_activity_report,
)


@pytest.fixture()
def candidate(tmp_path):
    return make_candidate(tmp_path)


def _codes(report: dict) -> set:
    return {item["code"] for item in report["problems"]}


class TestActivityReport:
    def test_direct_active_record_is_evident(self, candidate):
        report = valid_activity_report(candidate)
        assert report["verdict"] == VERDICT_ACTIVITY_EVIDENT
        assert report["problems"] == []
        assert report["terminal_record_sha256"]
        assert require_activity_evidence(report, candidate) == \
            report["report_sha256"]

    def test_absent_record_is_typed_refusal_not_default(self, candidate):
        report = candidate_activity_report(candidate, None, now=NOW)
        assert report["verdict"] == VERDICT_ACTIVITY_NOT_EVIDENT
        assert _codes(report) == {NO_ACTIVITY_EVIDENCE}
        with pytest.raises(SuccessionError, match=NO_ACTIVITY_EVIDENCE):
            require_activity_evidence(report, candidate)

    def test_missing_record_file_refuses(self, candidate, tmp_path):
        report = candidate_activity_report(
            candidate, tmp_path / "never_written.json", now=NOW)
        assert _codes(report) == {NO_ACTIVITY_EVIDENCE}

    def test_viable_but_inactive_record_refuses(self, candidate):
        # THE cell this cycle documented: mechanics-VIABLE, zero trades.
        path = make_activity_record(
            candidate, activity_status="inactive",
            promotion_eligible=False,
            inactive_cause="no_activity_eligible_checkpoint",
            best_model_path=None, best_model_sha256=None)
        report = candidate_activity_report(candidate, path, now=NOW)
        assert report["verdict"] == VERDICT_ACTIVITY_NOT_EVIDENT
        assert ACTIVITY_STATUS_NOT_ACTIVE in _codes(report)
        assert ACTIVITY_RECORD_NOT_PROMOTION_ELIGIBLE in _codes(report)
        with pytest.raises(SuccessionError, match="finding 269"):
            require_activity_evidence(report, candidate)

    def test_mechanics_screen_verdict_is_never_activity_evidence(
            self, candidate, tmp_path):
        # Adversarial: a screen verdict that says VIABLE everywhere AND
        # smuggles promotion_eligible=true. Still refused: viability is
        # not activity (finding 263).
        screen = tmp_path / "screen_verdict.json"
        screen.write_text(json.dumps({
            "schema": "agent_multi.p1_difficulty_lr_screen_verdict.v1",
            "outcome": "SCREEN_VIABLE_REGION",
            "promotion_eligible": True,
            "viable_cells": [{"seed": s, "cell": c,
                              "handoff_viability": "VIABLE"}
                             for s in (101, 202, 303, 404)
                             for c in ("P1E_LR3E5", "P1N_LR3E5")],
            "activity": {"active_cells": 0, "cells_expected": 16},
        }))
        report = candidate_activity_report(candidate, screen, now=NOW)
        assert report["verdict"] == VERDICT_ACTIVITY_NOT_EVIDENT
        assert ACTIVITY_EVIDENCE_FROM_MECHANICS_SCREEN in _codes(report)

    def test_unknown_schema_refuses(self, candidate):
        path = make_activity_record(candidate, schema="bogus.v9")
        report = candidate_activity_report(candidate, path, now=NOW)
        assert ACTIVITY_RECORD_SCHEMA_UNSUPPORTED in _codes(report)

    def test_activity_on_other_bytes_proves_nothing(self, candidate):
        # An active, eligible record whose checkpoint is NOT the
        # candidate artifact: activity measured elsewhere never
        # transfers.
        path = make_activity_record(candidate,
                                    best_model_sha256="f" * 64)
        report = candidate_activity_report(candidate, path, now=NOW)
        assert report["verdict"] == VERDICT_ACTIVITY_NOT_EVIDENT
        assert _codes(report) == {ACTIVITY_ARTIFACT_MISMATCH}

    def test_tampered_report_refuses_at_consumption(self, candidate):
        report = valid_activity_report(candidate)
        report["activity_status"] = "definitely_active"
        with pytest.raises(SuccessionError, match="digest mismatch"):
            require_activity_evidence(report, candidate)

    def test_report_for_another_candidate_refuses(self, tmp_path):
        first = make_candidate(tmp_path)
        report = valid_activity_report(first)
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        # different BYTES, so the artifact sha genuinely differs
        (other_dir / "candidate_model.json").write_text(
            json.dumps({"model": "a-different-artifact"}))
        other = make_candidate(other_dir, model_id="other-model")
        with pytest.raises(SuccessionError,
                           match="different candidate artifact"):
            require_activity_evidence(report, other)

    @pytest.mark.parametrize("empty", [None, {}, []])
    def test_no_report_shapes_all_refuse(self, candidate, empty):
        with pytest.raises(SuccessionError, match=NO_ACTIVITY_EVIDENCE):
            require_activity_evidence(empty, candidate)


class TestNoProductionBypass:
    def test_activity_report_is_required_with_no_default(self):
        parameter = inspect.signature(
            promote_paper_champion).parameters["activity_report"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    def test_promotion_refuses_before_capability_on_inactive_record(
            self, tmp_path, promotion_world):
        olap, seat, candidate, compat, shadow, cap_store, signers = (
            promotion_world)
        path = make_activity_record(
            candidate, activity_status="inactive",
            promotion_eligible=False, best_model_sha256=None)
        bad = candidate_activity_report(candidate, path, now=NOW)
        with pytest.raises(SuccessionError, match="finding 269"):
            promote(olap, seat, candidate, cap_store, signers,
                    compatibility_report=compat, shadow_report=shadow,
                    activity_report=bad)
        # nothing consumed: the same capability still promotes with the
        # real evidence — proof the refusal fired before any burn.
        good = promote(olap, seat, candidate, cap_store, signers,
                       compatibility_report=compat,
                       shadow_report=shadow)
        assert good["state"] == "promoted"
        assert good["audit"]["activity_report_sha256"]


# reuse the promotion_world fixture from the main suite
from tests.unit.test_champion_succession import (  # noqa: E402,F401
    cap_store,
    olap,
    promotion_world,
    seat,
    sessions_schema,
    signer,
)
