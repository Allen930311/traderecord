"""Owner-confirmed Review runtime contract; isolated synthetic canonical fixtures only."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from test_operational_closeout import _make_fixture
from review_test_support import owner_review_authorization, review_candidate
from trading_os.evidence_store import EvidenceStore, EvidenceStoreError
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.review_preview import (
    ReviewPreviewError,
    prepare_review_evidence_additions,
    prepare_review_preview,
)


ROOT = Path(__file__).resolve().parents[1]
REVIEWED_AT = "2026-10-11T10:30:00+08:00"
CONFIRMED_AT = "2026-10-11T10:29:00+08:00"


def _closed_trade(tmp_path: Path, *, realized_r: float = 2.0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return _make_fixture(ROOT, tmp_path, write_review=False, realized_r=realized_r, include_partial=False)


def _compliant_candidate(store: LifecycleStore, trade_id: str):
    return review_candidate(store, trade_id, {
        "what_went_well": "synthetic evidence supports the frozen setup",
        "what_went_wrong": "none observed",
        "rule_compliance": "compliant",
        "execution_deviation": "none observed",
        "thesis_validity": "supported",
        "entry_quality": "observed",
        "exit_quality": "observed",
        "evidence_completeness": "complete",
        "owner_notes": "synthetic Owner decision",
        "diagnostic_class": "METHOD",
    })


def test_evidence_description_precomputes_identity_without_writing(tmp_path: Path) -> None:
    source = tmp_path / "review-intake.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic original bytes")
    evidence_store = EvidenceStore(tmp_path)

    described = evidence_store.describe(source, original_filename="review-intake.png")
    assert not evidence_store.directory.exists()
    ingested = evidence_store.ingest(source, original_filename="review-intake.png")
    assert {key: described[key] for key in ("sha256", "locator", "media_type", "byte_size", "original_filename")} == {
        key: ingested[key] for key in ("sha256", "locator", "media_type", "byte_size", "original_filename")
    }

    mismatch_store = EvidenceStore(tmp_path / "mismatch")
    with pytest.raises(EvidenceStoreError, match="confirmed preview"):
        mismatch_store.ingest(source, original_filename="review-intake.png", expected_sha256="0" * 64)
    assert not mismatch_store.directory.exists()


def test_explicit_local_image_intake_outside_repo_is_previewed_read_only(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    store, _, trade_id = _closed_trade(root)
    source = tmp_path / "owner-uploads" / "review-intake.png"
    source.parent.mkdir()
    source.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic external review image")
    intake = tmp_path / "review-intake.json"
    intake.write_text(json.dumps({
        "schema_version": "trade-review-evidence-intake.v1",
        "images": [{"path": str(source), "type": "chart_snapshot"}],
    }), encoding="utf-8")
    before = {path.name for path in (root / "canonical/evidence/artifacts").iterdir()}

    additions = prepare_review_evidence_additions(root, intake)
    preview = prepare_review_preview(root, store, trade_id, _compliant_candidate(store, trade_id), evidence_additions=additions)

    assert any(item["sha256"] == additions[0]["sha256"] for item in preview["content"]["evidence_inventory"])
    assert {path.name for path in (root / "canonical/evidence/artifacts").iterdir()} == before


def test_valid_setup_loss_keeps_setup_and_execution_quality_separate(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path, realized_r=-1.0)
    candidate = _compliant_candidate(store, trade_id)
    analysis = candidate["review_analysis"]

    assert store.current()["records"][trade_id]["realized_r"] == -1.0
    assert analysis["setup_quality"] == "valid"
    assert analysis["execution_quality"] == "aligned"
    assert all(row["status"] == "YES" for row in analysis["checklist"] if row["importance"] == "required")
    assert candidate["rule_compliance"] == "compliant"


def test_no_setup_remains_not_observable_and_cannot_be_recast_as_rule_violation(tmp_path: Path) -> None:
    record = {
        "trade_id": "trd_no_setup",
        "trade_no": "T000099",
        "state": "CLOSED",
        "trade_type": "live",
        "execution_environment": "LIVE",
        "strategy_id": None,
        "rule_version": None,
        "strategy_resolution": {
            "status": "no_match",
            "owner_attestation": {"setup_status": "NO_SETUP"},
        },
        "exit_verification": {"state": "confirmed"},
        "evidence": [],
        "lifecycle_event_ids": [],
    }

    class ReadOnlyStore:
        root = tmp_path

        def current(self):
            return {"records": {record["trade_id"]: record}}

    store = ReadOnlyStore()
    candidate = review_candidate(store, record["trade_id"], {
        "rule_compliance": "not_observable",
        "diagnostic_class": "INSUFFICIENT",
        "owner_notes": "Owner says there was no setup.",
    })
    assert candidate["review_analysis"]["setup_quality"] == "not_observable"
    assert candidate["review_analysis"]["checklist"] == []
    assert prepare_review_preview(tmp_path, store, record["trade_id"], candidate)["preview_digest"]

    candidate["rule_compliance"] = "violation"
    with pytest.raises(ReviewPreviewError, match="NO_SETUP"):
        prepare_review_preview(tmp_path, store, record["trade_id"], candidate)


def test_real_t000033_no_match_owner_statement_shape_is_not_a_strategy_violation(tmp_path: Path) -> None:
    canonical = json.loads((ROOT / "canonical/lifecycle/current.json").read_text(encoding="utf-8"))
    actual = next(row for row in canonical["records"].values() if row.get("trade_no") == "T000033")
    # Reuse only the real persisted resolution and Owner statement shape in an
    # in-memory fixture. This never opens the real record through a writer.
    record = {
        "trade_id": "trd_fixture_t000033_shape",
        "trade_no": "T000033",
        "state": "CLOSED",
        "strategy_id": None,
        "rule_version": None,
        "strategy_resolution": copy.deepcopy(actual["strategy_resolution"]),
        "owner_statements": copy.deepcopy(actual["owner_statements"]),
        "exit_verification": {"state": "confirmed"},
        "evidence": [],
        "lifecycle_event_ids": [],
    }

    class ReadOnlyStore:
        root = tmp_path

        def current(self):
            return {"records": {record["trade_id"]: record}}

    store = ReadOnlyStore()
    statement = record["owner_statements"][0]
    assert statement["authority"] == "owner_supplied_verbatim"
    assert hashlib.sha256(statement["verbatim_text"].encode("utf-8")).hexdigest() == statement["sha256"]
    assert "沒有任何的進場條件" in statement["verbatim_text"]

    no_setup = review_candidate(store, record["trade_id"], {
        "rule_compliance": "not_observable",
        "diagnostic_class": "INSUFFICIENT",
        "owner_notes": "Synthetic assessment; the Owner's actual statement remains in its own source field.",
    })
    assert no_setup["review_analysis"]["strategy_pin"]["status"] == "NOT_OBSERVABLE"
    assert no_setup["review_analysis"]["checklist"] == []
    assert prepare_review_preview(tmp_path, store, record["trade_id"], no_setup)["preview_digest"]
    # No frozen strategy prevents claiming rule compliance, not independent
    # Agent quality analysis of the Owner's clearly stated entry plan.
    no_setup["review_analysis"]["setup_quality"] = "invalid"
    no_setup["review_analysis"]["agent_assessment"]["setup"] = (
        "Agent judges this Owner-confirmed no-setup entry qualitatively invalid; "
        "frozen-strategy rule compliance remains not observable."
    )
    assert prepare_review_preview(tmp_path, store, record["trade_id"], no_setup)["preview_digest"]

    violation = review_candidate(store, record["trade_id"], {
        "rule_compliance": "violation",
        "diagnostic_class": "METHOD",
        "owner_notes": "An Owner no-setup statement is not proof of a frozen rule violation.",
    })
    # The Owner's words are preserved but not parsed by a phrase-list.
    with pytest.raises(ReviewPreviewError, match="exact frozen strategy pin"):
        prepare_review_preview(tmp_path, store, record["trade_id"], violation)
    violation["review_analysis"]["setup_interpretation"] = {
        "status": "NO_SETUP",
        "rationale": "Agent interprets the Owner's original statement and trade context.",
    }
    with pytest.raises(ReviewPreviewError, match="Owner-attributed NO_SETUP"):
        prepare_review_preview(tmp_path, store, record["trade_id"], violation)


def test_no_match_without_owner_no_setup_statement_stays_epistemic(tmp_path: Path) -> None:
    record = {
        "trade_id": "trd_generic_no_match",
        "trade_no": "T000098",
        "state": "CLOSED",
        "strategy_id": None,
        "rule_version": None,
        "strategy_resolution": {"status": "no_match", "reason": "No strategy matched the available evidence."},
        "owner_statements": [],
        "exit_verification": {"state": "confirmed"},
        "evidence": [],
        "lifecycle_event_ids": [],
    }

    class ReadOnlyStore:
        root = tmp_path

        def current(self):
            return {"records": {record["trade_id"]: record}}

    store = ReadOnlyStore()
    candidate = review_candidate(store, record["trade_id"], {
        "rule_compliance": "violation",
        "diagnostic_class": "METHOD",
        "owner_notes": "Insufficient strategy attribution.",
    })
    with pytest.raises(ReviewPreviewError, match="exact frozen strategy pin"):
        prepare_review_preview(tmp_path, store, record["trade_id"], candidate)


def test_insufficient_strategy_evidence_cannot_be_promoted_to_violation(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path)
    candidate = review_candidate(store, trade_id, {
        "rule_compliance": "not_observable",
        "diagnostic_class": "INSUFFICIENT",
    })
    candidate["rule_compliance"] = "violation"
    candidate["diagnostic_class"] = "METHOD"
    candidate["review_analysis"]["setup_quality"] = "unknown"
    for row in candidate["review_analysis"]["checklist"]:
        if row["importance"] == "required":
            row["status"] = "UNKNOWN"
            row["evidence_refs"] = []
            row["rationale"] = "Synthetic evidence is insufficient to decide this required condition."
    with pytest.raises(ReviewPreviewError, match="evidence-linked required NO"):
        prepare_review_preview(tmp_path, store, trade_id, candidate)


def test_execution_deviation_does_not_rewrite_valid_setup_assessment(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path)
    candidate = review_candidate(store, trade_id, {
        "rule_compliance": "violation",
        "execution_deviation": "entered before the required confirmation",
        "diagnostic_class": "EXECUTION",
    })

    assert candidate["review_analysis"]["setup_quality"] == "valid"
    assert candidate["review_analysis"]["execution_quality"] == "deviated"
    assert all(row["status"] == "YES" for row in candidate["review_analysis"]["checklist"] if row["importance"] == "required")
    assert prepare_review_preview(tmp_path, store, trade_id, candidate)["preview_digest"]


def test_checklist_does_not_override_agent_setup_quality(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path)
    candidate = _compliant_candidate(store, trade_id)
    required_setup = next(
        row for row in candidate["review_analysis"]["checklist"]
        if row["importance"] == "required" and row["lane"] == "setup"
    )
    required_setup["status"] = "NO"
    required_setup["rationale"] = "The referenced chart shows the condition failed."
    candidate["rule_compliance"] = "violation"
    candidate["diagnostic_class"] = "METHOD"
    candidate["review_analysis"]["setup_quality"] = "unknown"
    candidate["review_analysis"]["agent_assessment"]["setup"] = (
        "One condition failed, but the complete setup quality remains uncertain "
        "after reviewing the available chart evidence."
    )
    unknown_preview = prepare_review_preview(tmp_path, store, trade_id, candidate)
    assert unknown_preview["preview_digest"]
    candidate["review_analysis"]["setup_quality"] = "invalid"
    candidate["review_analysis"]["agent_assessment"]["setup"] = (
        "Agent concludes invalid overall setup based on the cited failed condition."
    )
    invalid_preview = prepare_review_preview(tmp_path, store, trade_id, candidate)
    assert unknown_preview["preview_digest"] != invalid_preview["preview_digest"]


def test_replay_unknown_keeps_capture_and_market_time_separate(tmp_path: Path) -> None:
    store, draft_id, trade_id = _closed_trade(tmp_path)
    source = tmp_path / "replay.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic replay image bytes")
    artifact = EvidenceStore(tmp_path).ingest(source)
    store.attach_evidence(
        draft_id,
        {
            **artifact,
            "source_ref": "fixture://review/replay",
            "category": "review",
            "timing": "post_trade",
            "type": "replay_chart",
            "captured_at": "2026-10-11T10:10:00+08:00",
            "represented_market_time": {"start": "2026-09-11T00:00:00Z", "end": "2026-09-11T00:06:00Z"},
            "provenance": {"capture_mode": "retrospective_replay"},
            "role": "historical_market_reconstruction",
        },
    )
    candidate = review_candidate(store, trade_id, {"rule_compliance": "unknown", "diagnostic_class": "INSUFFICIENT"})
    preview = prepare_review_preview(tmp_path, store, trade_id, candidate)
    replay = next(item for item in preview["content"]["evidence_inventory"] if item["source_ref"] == "fixture://review/replay")

    assert candidate["review_analysis"]["execution_quality"] == "unknown"
    assert replay["captured_at"] != replay["represented_market_time"]["end"]
    assert replay["represented_market_time"]["end"] == "2026-09-11T00:06:00Z"


def test_owner_replay_image_remains_eligible_as_plan_evidence_with_true_provenance(tmp_path: Path) -> None:
    store, draft_id, trade_id = _closed_trade(tmp_path)
    source = tmp_path / "owner-replay.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic owner replay chart")
    artifact = EvidenceStore(tmp_path).ingest(source, original_filename="owner-replay.png")
    store.attach_evidence(
        draft_id,
        {
            **artifact,
            "source_ref": artifact["locator"],
            "category": "review",
            "timing": "post_trade",
            "type": "replay_chart",
            "captured_at": "2026-10-11T10:10:00+08:00",
            "represented_market_time": {"start": "2026-09-11T00:00:00Z", "end": "2026-09-11T00:06:00Z"},
            "provenance": {"capture_mode": "retrospective_replay"},
            "role": "historical_market_reconstruction",
        },
    )
    candidate = _compliant_candidate(store, trade_id)
    replay_ref = {"sha256": artifact["sha256"], "source_ref": artifact["locator"]}
    for row in candidate["review_analysis"]["checklist"]:
        if row["importance"] == "required" and row["lane"] == "setup":
            row["evidence_refs"] = [replay_ref]

    preview = prepare_review_preview(tmp_path, store, trade_id, candidate)
    replay = next(item for item in preview["content"]["evidence_inventory"] if item["sha256"] == artifact["sha256"])
    assert replay["timing"] == "post_trade"
    assert replay["type"] == "replay_chart"
    assert replay["provenance"]["capture_mode"] == "retrospective_replay"
    assert replay["role"] == "historical_market_reconstruction"
    assert candidate["review_analysis"]["setup_quality"] == "valid"
    assert any(
        row["status"] == "YES" and replay_ref in row["evidence_refs"]
        for row in candidate["review_analysis"]["checklist"]
        if row["lane"] == "setup"
    ), "Replay evidence must support plan/rule assessments regardless of capture time"


def test_owner_replay_image_preview_digest_is_stable_after_interrupted_attach(tmp_path: Path) -> None:
    store, draft_id, trade_id = _closed_trade(tmp_path)
    source = tmp_path / "owner-replay.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nreplayed plan chart")
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps({
        "schema_version": "trade-review-evidence-intake.v1",
        "images": [{"path": str(source), "type": "replay_chart"}],
    }), encoding="utf-8")
    addition = prepare_review_evidence_additions(tmp_path, intake_path)[0]
    candidate = _compliant_candidate(store, trade_id)
    before = prepare_review_preview(tmp_path, store, trade_id, candidate, evidence_additions=[addition])
    EvidenceStore(tmp_path).ingest(source)
    store.attach_evidence(draft_id, {k: v for k, v in addition.items() if k != "source_path"})
    after = prepare_review_preview(tmp_path, store, trade_id, candidate, evidence_additions=[addition])
    assert before["preview_digest"] == after["preview_digest"]
    assert sum(item["sha256"] == addition["sha256"] for item in after["content"]["evidence_inventory"]) == 1
    tampered = {**addition, "captured_at": "2026-10-11T10:10:00+08:00"}
    with pytest.raises(ReviewPreviewError, match="contradictory metadata"):
        prepare_review_preview(tmp_path, store, trade_id, candidate, evidence_additions=[tampered])


def test_no_setup_and_unpinned_strategy_cannot_be_marked_compliant(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path)
    candidate = _compliant_candidate(store, trade_id)
    candidate["review_analysis"]["strategy_pin"] = {
        "status": "NOT_OBSERVABLE", "strategy_id": None,
        "rule_version": None, "reason": "no_match",
    }

    record = {
        "trade_id": "trd_no_setup_compliant",
        "trade_no": "T000099",
        "state": "CLOSED",
        "strategy_id": None,
        "rule_version": None,
        "strategy_resolution": {"status": "no_match", "owner_attestation": {"setup_status": "NO_SETUP"}},
        "exit_verification": {"state": "confirmed"},
        "evidence": [],
        "lifecycle_event_ids": [],
    }
    class ReadOnlyStore:
        root = tmp_path
        def current(self):
            return {"records": {record["trade_id"]: record}}
    unresolved = ReadOnlyStore()
    from review_test_support import review_candidate
    proposed = review_candidate(unresolved, record["trade_id"], {"rule_compliance": "compliant"})
    with pytest.raises(ReviewPreviewError, match="cannot be declared strategy compliant"):
        prepare_review_preview(tmp_path, unresolved, record["trade_id"], proposed)


def test_no_setup_can_have_separate_owner_attested_execution_violation(tmp_path: Path) -> None:
    from review_test_support import review_candidate
    record = {
        "trade_id": "trd_no_setup_exec",
        "trade_no": "T000099",
        "state": "CLOSED",
        "strategy_id": None,
        "rule_version": None,
        "strategy_resolution": {"status": "no_match", "owner_attestation": {"setup_status": "NO_SETUP"}},
        "exit_verification": {"state": "confirmed"},
        "owner_statements": [],
        "evidence": [],
        "lifecycle_event_ids": [],
    }

    class ReadOnlyStore:
        root = tmp_path
        def current(self):
            return {"records": {record["trade_id"]: record}}
    store = ReadOnlyStore()
    candidate = review_candidate(store, record["trade_id"], {
        "rule_compliance": "violation",
        "diagnostic_class": "EXECUTION",
        "execution_deviation": "Owner confirms premature protection after an entry without setup",
    })
    assert candidate["review_analysis"]["setup_quality"] == "not_observable"
    assert candidate["review_analysis"]["execution_quality"] == "deviated"
    assert candidate["review_analysis"]["checklist"] == []
    assert prepare_review_preview(tmp_path, store, record["trade_id"], candidate)["preview_digest"]

    unsupported = copy.deepcopy(candidate)
    unsupported["review_analysis"].pop("execution_deviation_support")
    with pytest.raises(ReviewPreviewError, match="Owner-attributed NO_SETUP"):
        prepare_review_preview(tmp_path, store, record["trade_id"], unsupported)


def test_execution_deviation_uses_agent_fields_not_free_text(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path)
    candidate = review_candidate(store, trade_id, {
        "rule_compliance": "violation",
        "execution_deviation": "stopped early before confirmation",
        "diagnostic_class": "EXECUTION",
    })
    for words in ("none observed", "none", "unknown", "尚待討論，不能靠文字判定"):
        candidate["execution_deviation"] = words
        assert prepare_review_preview(tmp_path, store, trade_id, candidate)["preview_digest"]
    candidate["review_analysis"]["execution_quality"] = "aligned"
    with pytest.raises(ReviewPreviewError, match="requires an explicit EXECUTION deviation assessment"):
        prepare_review_preview(tmp_path, store, trade_id, candidate)


def test_execution_deviation_requires_specific_attestation_or_evidence(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path)
    from review_test_support import review_candidate
    candidate = review_candidate(store, trade_id, {
        "rule_compliance": "violation",
        "execution_deviation": "premature stop move",
        "diagnostic_class": "EXECUTION",
    })
    assert prepare_review_preview(tmp_path, store, trade_id, candidate)["preview_digest"]
    missing = copy.deepcopy(candidate)
    del missing["review_analysis"]["execution_deviation_support"]
    # Remove any execution-lane NO: this tests the explicit deviation path,
    # which must not rely on an unrelated image merely being present.
    for row in missing["review_analysis"]["checklist"]:
        if row["lane"] == "execution" and row["status"] == "NO":
            row["status"] = "YES"
    with pytest.raises(ReviewPreviewError, match="specifically supported execution deviation"):
        prepare_review_preview(tmp_path, store, trade_id, missing)
    fabricated = copy.deepcopy(missing)
    fabricated["review_analysis"]["execution_deviation_support"] = {
        "authority": "owner_attestation",
        "owner_statement": "Agent invented a stop move",
        "evidence_refs": [],
        "rationale": "An unverified statement",
    }
    with pytest.raises(ReviewPreviewError, match="must be verbatim"):
        prepare_review_preview(tmp_path, store, trade_id, fabricated)


def test_missing_or_stale_owner_confirmation_writes_no_lifecycle_event(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path)
    candidate = _compliant_candidate(store, trade_id)
    authorization = owner_review_authorization(store, trade_id, candidate)
    confirmation = authorization.confirmation_payload()
    before_events = store._read_events()
    before_trade_files = {path.name: path.read_bytes() for path in (tmp_path / "canonical/records/trades").glob("*.json")}

    with pytest.raises(LifecycleError, match="validated active-interaction Owner confirmation"):
        store.review(trade_id, candidate, reviewed_at=REVIEWED_AT)
    # A matching digest and a plausible timestamp are consistency data, not an Owner action.
    with pytest.raises(LifecycleError, match="validated active-interaction Owner"):
        store.review(trade_id, candidate, reviewed_at=REVIEWED_AT, owner_confirmation=confirmation)
    altered = copy.deepcopy(candidate)
    altered["owner_notes"] = "Changed after the preview."
    with pytest.raises(LifecycleError, match="stale or does not match"):
        store.review(
            trade_id, altered, reviewed_at=REVIEWED_AT,
            owner_confirmation=confirmation, owner_authorization=authorization,
        )

    assert store._read_events() == before_events
    assert {path.name: path.read_bytes() for path in (tmp_path / "canonical/records/trades").glob("*.json")} == before_trade_files


def test_confirmed_review_retry_is_idempotent_and_interrupted_retry_recovers(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path)
    candidate = _compliant_candidate(store, trade_id)
    authorization = owner_review_authorization(store, trade_id, candidate)
    confirmation = authorization.confirmation_payload()

    first = store.review(trade_id, candidate, reviewed_at=REVIEWED_AT, owner_confirmation=confirmation, owner_authorization=authorization)
    count = len(store._read_events())
    second = store.review(trade_id, candidate, reviewed_at=REVIEWED_AT, owner_confirmation=confirmation, owner_authorization=authorization)
    assert first["event_id"] == second["event_id"]
    assert len(store._read_events()) == count

    interrupted, _, interrupted_id = _closed_trade(tmp_path / "interrupted")
    interrupted_candidate = _compliant_candidate(interrupted, interrupted_id)
    interrupted_authorization = owner_review_authorization(interrupted, interrupted_id, interrupted_candidate)
    interrupted_confirmation = interrupted_authorization.confirmation_payload()
    start_time = "2026-10-11T10:29:59+08:00"
    interrupted.append(
        "review_started",
        {"trade_id": interrupted_id},
        idempotency_key=f"review-start:{interrupted_id}",
        occurred_at=start_time,
    )
    before = len(interrupted._read_events())
    reviewed = interrupted.review(
        interrupted_id,
        interrupted_candidate,
        reviewed_at=REVIEWED_AT,
        owner_confirmation=interrupted_confirmation,
        owner_authorization=interrupted_authorization,
    )
    assert reviewed["event_type"] == "reviewed"
    assert len(interrupted._read_events()) == before + 1
    assert interrupted.current()["records"][interrupted_id]["state"] == "REVIEWED"


def test_frozen_rule_file_drift_invalidates_confirmation(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path)
    candidate = _compliant_candidate(store, trade_id)
    authorization = owner_review_authorization(store, trade_id, candidate)
    confirmation = authorization.confirmation_payload()
    pin = candidate["review_analysis"]["strategy_pin"]
    path = tmp_path / pin["version_file"]
    path.write_bytes(path.read_bytes() + b"\n# post-preview rule pin drift\n")

    with pytest.raises(LifecycleError, match="strategy pin is stale"):
        store.review(trade_id, candidate, reviewed_at=REVIEWED_AT, owner_confirmation=confirmation, owner_authorization=authorization)
    assert not any(event["event_type"] in {"review_started", "reviewed"} for event in store._read_events())


def test_mode_change_changes_the_confirmed_preview_digest(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path)
    candidate = _compliant_candidate(store, trade_id)
    original = prepare_review_preview(tmp_path, store, trade_id, candidate)

    class PaperProjection:
        root = tmp_path

        def current(self):
            projection = copy.deepcopy(store.current())
            projection["records"][trade_id]["execution_environment"] = "PAPER"
            return projection

        def _read_events(self):
            return store._read_events()

    changed = prepare_review_preview(tmp_path, PaperProjection(), trade_id, candidate)
    assert original["content"]["trade_snapshot"].get("execution_environment") != "PAPER"
    assert changed["content"]["trade_snapshot"]["execution_environment"] == "PAPER"
    assert original["preview_digest"] != changed["preview_digest"]


def test_missing_or_corrupt_attached_image_bytes_reject_preview(tmp_path: Path) -> None:
    store, _, trade_id = _closed_trade(tmp_path)
    candidate = _compliant_candidate(store, trade_id)
    record = store.current()["records"][trade_id]
    evidence = next(item for item in record["evidence"] if item.get("type") == "chart_snapshot")
    artifact = tmp_path / "canonical/evidence/artifacts" / evidence["locator"].split(":", 1)[1]
    exact = artifact.read_bytes()
    assert prepare_review_preview(tmp_path, store, trade_id, candidate)["preview_digest"]

    artifact.write_bytes(exact + b"changed")
    with pytest.raises(ReviewPreviewError, match="missing or invalid"):
        prepare_review_preview(tmp_path, store, trade_id, candidate)
    artifact.unlink()
    with pytest.raises(ReviewPreviewError, match="missing or invalid"):
        prepare_review_preview(tmp_path, store, trade_id, candidate)
