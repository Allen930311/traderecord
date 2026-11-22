"""Correction pass #3 (third review pass, Blocker 2 of PR #43's issue #42):
BACKTEST strict evidence must be bound to the EXACT verification evidence
refs, not just "any strong evidence attached anywhere on the trade."

Correction pass #2 added `_backtest_entry_evidence_is_strict` /
`_backtest_exit_evidence_is_strict`, but they searched ANY entry-category /
exit-category evidence attached to the trade for something strong + durable +
hash-verified -- regardless of whether that evidence is what was actually
cited by `confirm_backtest_entry`/`verify_exit` to establish the claim. A
weak `manual_note` execution claim, sitting alongside an unrelated strong
`chart_snapshot` attached elsewhere on the same draft (e.g. a stray
context/review-category chart, or one attached after the fact and never
cited), incorrectly counted as strict-countable.

This module tests the fix: entry remains bound by its exact source_ref/category
guard, while exit evidence must have its exact identity persisted in the
actual verification's `evidence_bindings` (with `evidence_refs` retained as a
compatibility projection). It also tests the hardened `_verified_artifact_locator`
containment/identity checks (C4): a traversal-shaped locator, and a locator
whose filename-encoded sha256 does not match the recorded sha256 metadata,
both fail closed without ever reading outside the evidence store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_os.evidence_store import evidence_binding, evidence_identity
from trading_os.operational_analytics import _verified_artifact_locator, refresh_analytics
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.unified_capture import capture_backtest

from test_correction_pass_blockers import (
    _backtest_kwargs,
    _copy_operational_inputs,
    _ingest_entry_evidence,
    _ingest_exit_evidence,
)


FIXED_GENERATED_AT = "2026-09-12T00:00:00Z"
FIXED_SOURCE_COMMIT = "d" * 40


def _row_for(analytics: dict, trade_id: str) -> dict:
    for row in analytics["canonical_read_model"]:
        if row.get("trade_id") == trade_id:
            return row
    raise AssertionError(f"no canonical_read_model row for {trade_id}")


def test_unrelated_strong_entry_evidence_does_not_upgrade_weak_claim(tmp_path: Path) -> None:
    """1. manual_declared_execution entry cites only a weak manual_note in
    evidence_refs; a genuinely strong, durably-ingested chart_snapshot is
    also attached to the draft under category="entry" but is NOT part of
    entry.evidence_refs -- must not upgrade the claim. counted=false."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)

    weak_entry_evidence = {
        "source_ref": "fixture://exact-binding-weak-entry",
        "timing": "execution",
        "type": "manual_note",
        "category": "entry",
        "sha256": "1" * 64,
    }
    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=weak_entry_evidence))
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    # A genuinely strong, durably-ingested entry-category chart is attached
    # to the same draft, but its source_ref was never cited by
    # confirm_backtest_entry's evidence_refs -- it must not count.
    unrelated_strong_entry = _ingest_entry_evidence(tmp_path, label="unrelated-strong-entry", captured_at="2026-09-12T00:11:00Z")
    store.attach_evidence(draft_id, unrelated_strong_entry)

    exit_evidence = _ingest_exit_evidence(tmp_path, label="exact-binding-entry-case-exit", captured_at="2026-09-12T01:10:00Z")
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=exit_evidence["source_ref"])
    store.attach_evidence(draft_id, exit_evidence)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[exit_evidence["source_ref"]], realized_r=2.0)

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is False
    assert row["exclusion_reason"] == "backtest_evidence_insufficient"
    assert row["evidence_quality"] == "owner_declared_unverified"


def test_unrelated_strong_exit_evidence_does_not_upgrade_weak_attestation(tmp_path: Path) -> None:
    """2. owner_attestation exit cites only a weak manual_note in its
    evidence_refs; a genuinely strong, durably-ingested exit chart is also
    attached but NOT included in the exit verification's evidence_refs --
    must not upgrade the attestation. counted=false."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)

    entry_evidence = _ingest_entry_evidence(tmp_path, label="exact-binding-exit-case-entry", captured_at="2026-09-12T00:10:30Z")
    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    weak_exit_evidence = {
        "source_ref": "fixture://exact-binding-weak-exit",
        "timing": "post_trade",
        "type": "manual_note",
        "category": "exit",
        "sha256": "2" * 64,
    }
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=weak_exit_evidence["source_ref"])
    store.attach_evidence(draft_id, weak_exit_evidence)

    # A genuinely strong, durably-ingested exit-category chart is attached
    # to the same draft, but its source_ref was never cited by verify_exit's
    # evidence_refs -- it must not count.
    unrelated_strong_exit = _ingest_exit_evidence(tmp_path, label="unrelated-strong-exit", captured_at="2026-09-12T01:11:00Z")
    store.attach_evidence(draft_id, unrelated_strong_exit)

    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[weak_exit_evidence["source_ref"]], realized_r=2.0)

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is False
    assert row["exclusion_reason"] == "backtest_evidence_insufficient"
    assert row["evidence_quality"] == "owner_declared_unverified"


def test_exact_bound_strong_entry_and_exit_is_counted(tmp_path: Path) -> None:
    """5. Entry verification's evidence_refs includes the source_ref of a
    real, durably-ingested strong-typed entry evidence item, AND exit
    verification's evidence_refs includes the source_ref of a real,
    durably-ingested strong-typed exit evidence item -> counted=true,
    evidence_quality=governed_supported. (Regression guard for the exact-
    binding fix: properly cited strong evidence must still count.)"""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)

    entry_evidence = _ingest_entry_evidence(tmp_path, label="exact-bound-entry", captured_at="2026-09-12T00:10:30Z")
    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    exit_evidence = _ingest_exit_evidence(tmp_path, label="exact-bound-exit", captured_at="2026-09-12T01:10:00Z")
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=exit_evidence["source_ref"])
    store.attach_evidence(draft_id, exit_evidence)
    verification_event = store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[exit_evidence["source_ref"]], realized_r=2.0)
    assert verification_event["payload"]["exit_verification"]["evidence_bindings"] == [evidence_binding(exit_evidence)]

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is True
    assert row["exclusion_reason"] is None
    assert row["evidence_quality"] == "governed_supported"


def test_cross_category_source_ref_cannot_upgrade_owner_attestation(tmp_path: Path) -> None:
    """A review-category ref must remain the identity used by verification.

    Adding a later strong EXIT projection under the same source_ref is legal,
    but it must not upgrade the already-persisted owner-attestation binding.
    """

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="cross-category-entry", captured_at="2026-09-12T00:10:30Z")
    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    source_ref = "fixture://cross-category-exit"
    weak_review = {
        "source_ref": source_ref,
        "timing": "post_trade",
        "type": "manual_note",
        "category": "review",
        "sha256": "b" * 64,
        "locator": "sha256:" + "b" * 64,
    }
    store.attach_evidence(draft_id, weak_review)
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=source_ref)
    verification_event = store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[source_ref], realized_r=2.0)

    strong_exit = _ingest_exit_evidence(tmp_path, label="cross-category-strong-exit", captured_at="2026-09-12T01:11:00Z")
    strong_exit["source_ref"] = source_ref
    store.attach_evidence(draft_id, strong_exit)

    binding = verification_event["payload"]["exit_verification"]["evidence_bindings"]
    assert binding == [evidence_binding(weak_review)]
    assert binding[0]["category"] == "review"
    assert {item["category"] for item in store._attached_evidence(draft_id) if item["source_ref"] == source_ref} == {"review", "exit"}
    assert store.current()["records"][trade_id]["exit_verification"]["evidence_bindings"] == binding

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is False
    assert row["exclusion_reason"] == "backtest_evidence_insufficient"


def test_traversal_shaped_locator_is_rejected_by_verified_artifact_locator(tmp_path: Path) -> None:
    """3a. Unit-level: a traversal-shaped locator is rejected by
    _verified_artifact_locator directly -- never crashes, never reads
    outside the evidence store."""

    (tmp_path / "canonical" / "evidence" / "artifacts").mkdir(parents=True)
    # Plant a file outside the evidence artifacts directory that a
    # traversal-shaped locator might otherwise reach.
    outside = tmp_path / "outside-secret.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nsecret")
    import hashlib

    secret_sha = hashlib.sha256(outside.read_bytes()).hexdigest()

    assert _verified_artifact_locator(tmp_path, "artifact:../../outside-secret.png", secret_sha) is False
    assert _verified_artifact_locator(tmp_path, "artifact:..%2f..%2foutside-secret.png", secret_sha) is False
    assert _verified_artifact_locator(tmp_path, f"artifact:{secret_sha}/../../../outside-secret.png", secret_sha) is False
    # Not even a well-formed-looking absolute path escapes.
    assert _verified_artifact_locator(tmp_path, f"artifact:/{secret_sha}.png", secret_sha) is False


def test_traversal_shaped_locator_e2e_fails_closed(tmp_path: Path) -> None:
    """3b. End-to-end: a record whose evidence carries a traversal-shaped
    locator ends up counted=false rather than crashing analytics."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)

    traversal_sha = "3" * 64
    entry_evidence = {
        "source_ref": "fixture://traversal-entry",
        "timing": "execution",
        "type": "chart_snapshot",
        "category": "entry",
        "sha256": traversal_sha,
        "locator": f"artifact:../../{traversal_sha}.png",
    }
    with pytest.raises(LifecycleError, match="evidence artifact is not verified"):
        capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))


def test_filename_sha256_mismatch_locator_is_rejected(tmp_path: Path) -> None:
    """4. A locator whose filename-encoded sha256 does not match the
    recorded sha256 metadata field is rejected, even when a real artifact
    with that other sha256 genuinely exists on disk (i.e. an attacker citing
    someone else's real, durably-ingested artifact under a false claimed
    identity must not be accepted)."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)

    # A real artifact is ingested (this is "someone else's" real evidence).
    real = _ingest_entry_evidence(tmp_path, label="real-other-artifact", captured_at="2026-09-12T00:09:00Z")

    claimed_sha = "9" * 64
    entry_evidence = {
        "source_ref": "fixture://mismatched-locator-entry",
        "timing": "execution",
        "type": "chart_snapshot",
        "category": "entry",
        # The recorded sha256 metadata field claims one identity...
        "sha256": claimed_sha,
        # ...but the locator's filename-encoded sha256 is a genuinely
        # different (real, durably-ingested) artifact's identity.
        "locator": real["locator"],
    }
    with pytest.raises(LifecycleError, match="not verified against its sha256"):
        capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))


def test_simulator_run_and_governed_manual_adjudication_still_pass_without_bound_chart(tmp_path: Path) -> None:
    """6. Regression: simulator_run entry / governed_manual_adjudication exit
    remain strict-by-construction and do not need any evidence_refs-bound
    chart -- the exact-binding fix must not add new requirements to these
    formal-authority paths."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)

    entry_evidence = {
        "source_ref": "fixture://simulator-exact-binding-entry",
        "timing": "execution",
        "type": "manual_note",  # deliberately weak -- simulator_run needs no image
        "category": "entry",
        "sha256": "6" * 64,
    }
    result = capture_backtest(
        store,
        **_backtest_kwargs(
            entry_evidence=entry_evidence,
            run_id="run-002",
            parameters_ref="fixture://params2",
            dataset_ref="fixture://dataset2",
            data_period="2026-01-01/2026-06-01",
        ),
    )
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    exit_evidence = {
        "source_ref": "fixture://simulator-exact-binding-exit",
        "timing": "post_trade",
        "type": "manual_note",  # deliberately weak -- governed_manual_adjudication needs no image
        "category": "exit",
        "sha256": "7" * 64,
    }
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=exit_evidence["source_ref"])
    store.attach_evidence(draft_id, exit_evidence)
    store.verify_exit(trade_id, authority_class="governed_manual_adjudication", evidence_refs=[exit_evidence["source_ref"]], realized_r=2.0)

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is True
    assert row["exclusion_reason"] is None
    assert row["evidence_quality"] == "governed_supported"


def test_entry_source_ref_alias_cannot_upgrade_weak_claim(tmp_path: Path) -> None:
    """A later strong entry artifact cannot replace the weak identity cited
    by confirm_backtest_entry; both lifecycle and direct-writer paths fail
    closed, leaving the sample non-countable."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    weak_entry = {
        "source_ref": "fixture://alias-entry",
        "timing": "execution",
        "type": "manual_note",
        "category": "entry",
        "sha256": "8" * 64,
    }
    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=weak_entry))
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    strong_entry = _ingest_entry_evidence(tmp_path, label="alias-entry-strong", captured_at="2026-09-12T00:11:00Z")
    strong_entry["source_ref"] = weak_entry["source_ref"]
    with pytest.raises(LifecycleError, match="evidence identity conflict"):
        store.attach_evidence(draft_id, strong_entry)
    with pytest.raises(ValueError, match="evidence identity conflict"):
        store.writer.attach_evidence(draft_id, {**strong_entry, "method": "chart_snapshot"})

    exit_evidence = _ingest_exit_evidence(tmp_path, label="alias-entry-exit", captured_at="2026-09-12T01:10:00Z")
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=exit_evidence["source_ref"])
    store.attach_evidence(draft_id, exit_evidence)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[exit_evidence["source_ref"]], realized_r=2.0)

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is False
    assert row["exclusion_reason"] == "backtest_evidence_insufficient"


def test_exit_source_ref_alias_can_never_become_strict_countable(tmp_path: Path) -> None:
    """A later strong exit artifact cannot replace the weak identity cited
    by owner_attestation exit verification."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="alias-exit-entry", captured_at="2026-09-12T00:10:30Z")
    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    weak_exit = {
        "source_ref": "fixture://alias-exit",
        "timing": "post_trade",
        "type": "manual_note",
        "category": "exit",
        "sha256": "9" * 64,
    }
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=weak_exit["source_ref"])
    store.attach_evidence(draft_id, weak_exit)
    strong_exit = _ingest_exit_evidence(tmp_path, label="alias-exit-strong", captured_at="2026-09-12T01:11:00Z")
    strong_exit["source_ref"] = weak_exit["source_ref"]
    with pytest.raises(LifecycleError, match="evidence identity conflict"):
        store.attach_evidence(draft_id, strong_exit)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[weak_exit["source_ref"]], realized_r=2.0)

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is False
    assert row["exclusion_reason"] == "backtest_evidence_insufficient"


def test_same_artifact_can_link_to_multiple_categories(tmp_path: Path) -> None:
    """Category remains part of the scope boundary: one durable artifact may
    support entry and review without duplicating bytes or becoming an alias
    conflict."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="multi-category-artifact", captured_at="2026-09-12T00:10:30Z")
    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))
    review_evidence = {**entry_evidence, "category": "review"}
    store.attach_evidence(result["draft_id"], review_evidence)

    attached = store._attached_evidence(result["draft_id"])
    matching = [item for item in attached if item["source_ref"] == entry_evidence["source_ref"]]
    assert {item["category"] for item in matching} == {"entry", "review"}
    assert {item["sha256"] for item in matching} == {entry_evidence["sha256"]}
    assert len({item["locator"] for item in matching}) == 1


def test_equivalent_canonical_and_lifecycle_copies_do_not_trigger_ambiguity(tmp_path: Path) -> None:
    """The finalized canonical record and lifecycle projection both carry
    the same evidence identity; duplicate projections collapse and remain
    strictly countable."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="duplicate-projection-entry", captured_at="2026-09-12T00:10:30Z")
    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))
    draft_id, trade_id = result["draft_id"], result["trade_id"]
    exit_evidence = _ingest_exit_evidence(tmp_path, label="duplicate-projection-exit", captured_at="2026-09-12T01:10:00Z")
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=exit_evidence["source_ref"])
    store.attach_evidence(draft_id, exit_evidence)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[exit_evidence["source_ref"]], realized_r=2.0)

    canonical = json.loads((tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json").read_text(encoding="utf-8"))
    projected = store.current()["records"][trade_id]
    entry_identity = evidence_identity(entry_evidence)
    canonical_identities = [evidence_identity(item) for item in canonical["evidence"]]
    projected_identities = [evidence_identity(item) for item in projected["evidence"]]
    # Finalization predates the later exit attachment, so only the entry is
    # expected to be copied into both durable projections in this fixture.
    assert canonical_identities.count(entry_identity) == 1
    assert projected_identities.count(entry_identity) == 1
    assert (canonical_identities + projected_identities).count(entry_identity) == 2

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is True
    assert row["exclusion_reason"] is None


def test_analytics_rejects_historical_conflicting_alias(tmp_path: Path) -> None:
    """Analytics independently rejects a conflicting cited alias even when
    the conflict is already present in a historical canonical copy and the
    lifecycle projection still contains the original strong evidence."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="historical-alias-entry", captured_at="2026-09-12T00:10:30Z")
    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))
    draft_id, trade_id = result["draft_id"], result["trade_id"]
    exit_evidence = _ingest_exit_evidence(tmp_path, label="historical-alias-exit", captured_at="2026-09-12T01:10:00Z")
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=exit_evidence["source_ref"])
    store.attach_evidence(draft_id, exit_evidence)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[exit_evidence["source_ref"]], realized_r=2.0)

    record_path = tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json"
    canonical = json.loads(record_path.read_text(encoding="utf-8"))
    conflicting = {
        **entry_evidence,
        "type": "manual_note",
        "sha256": "a" * 64,
        "locator": f"artifact:{'a' * 64}.png",
    }
    canonical["evidence"].append(conflicting)
    record_path.write_text(json.dumps(canonical, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is False
    assert row["exclusion_reason"] == "backtest_evidence_insufficient"


def test_identical_attachment_replay_remains_idempotent(tmp_path: Path) -> None:
    """Replaying the exact attachment returns the original lifecycle event
    and does not add another durable event."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="idempotent-attachment", captured_at="2026-09-12T00:10:30Z")
    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))
    before = store._read_events()
    replay = store.attach_evidence(result["draft_id"], entry_evidence)
    after = store._read_events()
    assert replay["event_id"] in {event["event_id"] for event in before}
    assert after == before
