"""Correction pass #2 (second review pass, Blocker 2 of PR #43's issue #42):
BACKTEST strict-countable evidence quality.

manual_declared_execution's entry confirmation, and a bare owner_attestation
exit, previously required only a syntactically-present evidence ref -- not a
durable, hash-verified artifact of a type that can actually support an
execution/realized-R claim. A bare manual_note (or a fabricated locator/sha256)
flowed straight through to `counted=true` in analytics, indistinguishable from
a properly evidenced backtest.

This module tests the new analytics-side (not lifecycle-blocking) strict-
evidence classification added to operational_analytics.py:
  * `_backtest_entry_evidence_is_strict` / `_backtest_exit_evidence_is_strict`
  * the `backtest_evidence_insufficient` exclusion reason
  * the `evidence_quality` audit field (governed_supported /
    owner_declared_unverified / insufficient)
  * Trade View still showing a `counted=false` BACKTEST row (never hidden)

Every recording path here still succeeds (draft -> evidence -> confirm entry
-> finalize -> close -> verify_exit): C2 requires a weak-evidence sample to
remain fully recordable, never silently blocked or deleted. Only strict
countability in analytics differs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from resolver_helpers import matching_observations, no_match_observations, write_observations
from trading_os.operational_analytics import refresh_analytics
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.strategy_health import refresh_health
from trading_os.trade_view import build_trade_view
from trading_os.unified_capture import capture_backtest, capture_live

from test_correction_pass_blockers import (
    _backtest_kwargs,
    _copy_operational_inputs,
    _ingest_entry_evidence,
    _ingest_exit_evidence,
)


FIXED_GENERATED_AT = "2026-09-12T00:00:00Z"
FIXED_SOURCE_COMMIT = "c" * 40


def _row_for(analytics: dict, trade_id: str) -> dict:
    for row in analytics["canonical_read_model"]:
        if row.get("trade_id") == trade_id:
            return row
    raise AssertionError(f"no canonical_read_model row for {trade_id}")


def test_strong_entry_and_exit_evidence_is_governed_supported(tmp_path: Path) -> None:
    """1. manual_declared_execution + durable strong entry & exit evidence
    -> counted=true, evidence_quality=governed_supported."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="strong-entry", captured_at="2026-09-12T00:10:30Z")

    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    exit_evidence = _ingest_exit_evidence(tmp_path, label="strong-exit", captured_at="2026-09-12T01:10:00Z")
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=exit_evidence["source_ref"])
    store.attach_evidence(draft_id, exit_evidence)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[exit_evidence["source_ref"]], realized_r=2.0)

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is True
    assert row["exclusion_reason"] is None
    assert row["evidence_quality"] == "governed_supported"


def test_manual_note_entry_with_no_real_artifact_is_recorded_but_not_counted(tmp_path: Path) -> None:
    """2. Bare manual_note entry evidence with an arbitrary syntactically-valid
    sha256 (no real ingested artifact) -> record IS created/closed/finalized,
    but counted=false, exclusion_reason=backtest_evidence_insufficient."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    weak_entry_evidence = {
        "source_ref": "fixture://weak-entry-claim",
        "timing": "execution",
        "type": "manual_note",
        "category": "entry",
        "sha256": "1" * 64,
    }

    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=weak_entry_evidence))
    draft_id, trade_id = result["draft_id"], result["trade_id"]
    assert trade_id.startswith("trd_")  # BT number allocated -- never blocked (C2)

    weak_exit_evidence = {
        "source_ref": "fixture://weak-exit-claim",
        "timing": "post_trade",
        "type": "manual_note",
        "category": "exit",
        "sha256": "2" * 64,
    }
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=weak_exit_evidence["source_ref"])
    store.attach_evidence(draft_id, weak_exit_evidence)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[weak_exit_evidence["source_ref"]], realized_r=2.0)

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["status"] == "REVIEWED" or row["status"] == "CLOSED"
    assert row["counted"] is False
    assert row["exclusion_reason"] == "backtest_evidence_insufficient"
    assert row["evidence_quality"] == "owner_declared_unverified"


def test_locator_with_no_backing_file_is_rejected_before_evidence_is_written(tmp_path: Path) -> None:
    """3. New content-addressed attachments require bytes in EvidenceStore."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    phantom_sha = "3" * 64
    entry_evidence = {
        "source_ref": "fixture://phantom-entry",
        "timing": "execution",
        "type": "chart_snapshot",
        "category": "entry",
        "sha256": phantom_sha,
        "locator": f"artifact:{phantom_sha}.png",
    }

    with pytest.raises(LifecycleError, match="evidence artifact is not verified"):
        capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))

    artifact_path = tmp_path / "canonical" / "evidence" / "artifacts" / f"{phantom_sha}.png"
    assert not artifact_path.exists()


def test_sha256_mismatch_with_real_artifact_fails_closed(tmp_path: Path) -> None:
    """4. Evidence's recorded sha256 does not match the real on-disk artifact's
    actual hash (metadata tampering/corruption) -- strict check fails closed."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    real_entry_evidence = _ingest_entry_evidence(tmp_path, label="mismatch-entry", captured_at="2026-09-12T00:10:30Z")
    tampered_entry_evidence = {**real_entry_evidence, "sha256": "4" * 64}

    with pytest.raises(LifecycleError, match="not verified against its sha256"):
        capture_backtest(store, **_backtest_kwargs(entry_evidence=tampered_entry_evidence))


def test_strong_entry_weak_exit_is_not_counted(tmp_path: Path) -> None:
    """5. Strong entry evidence but weak/insufficient exit evidence ->
    counted=false (both sides must be strict)."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="mixed-entry", captured_at="2026-09-12T00:10:30Z")

    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=entry_evidence))
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    weak_exit_evidence = {
        "source_ref": "fixture://mixed-weak-exit",
        "timing": "post_trade",
        "type": "manual_note",
        "category": "exit",
        "sha256": "5" * 64,
    }
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=weak_exit_evidence["source_ref"])
    store.attach_evidence(draft_id, weak_exit_evidence)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[weak_exit_evidence["source_ref"]], realized_r=2.0)

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is False
    assert row["exclusion_reason"] == "backtest_evidence_insufficient"
    assert row["evidence_quality"] == "owner_declared_unverified"


def test_simulator_run_remains_valid_without_any_image(tmp_path: Path) -> None:
    """6. A fully-structured simulator_run (run_id/dataset_ref/etc.) remains
    counted=true without needing any image -- confirms C4: formal simulator
    authority is untouched/still valid on its own. Uses a formal governed
    exit authority (governed_manual_adjudication) so the exit side is also
    strict-by-construction without needing an image."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = {
        "source_ref": "fixture://simulator-entry-ref",
        "timing": "execution",
        "type": "manual_note",  # deliberately weak type -- simulator_run needs no image
        "category": "entry",
        "sha256": "6" * 64,
    }

    result = capture_backtest(
        store,
        **_backtest_kwargs(
            entry_evidence=entry_evidence,
            run_id="run-001",
            parameters_ref="fixture://params",
            dataset_ref="fixture://dataset",
            data_period="2026-01-01/2026-06-01",
        ),
    )
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    assert trade["actual"]["execution_verification"]["entry"]["basis"] == "simulator_run"

    exit_evidence = {
        "source_ref": "fixture://simulator-exit-ref",
        "timing": "post_trade",
        "type": "manual_note",  # also deliberately weak -- governed_manual_adjudication needs no image
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


def test_live_regression_unaffected_by_backtest_evidence_gate(tmp_path: Path) -> None:
    """7. LIVE regression: a normal LIVE record through the existing full
    provider-verified path is still counted=true exactly as before. The
    evidence-binding field is a BACKTEST analytics guard and must not alter
    LIVE countability or its evidence-quality projection."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = {
        "source_ref": "fixture://live-fill",
        "timing": "execution",
        "type": "broker_fill",
        "category": "entry",
        "sha256": "8" * 64,
    }
    result = capture_live(
        store,
        strategy_observations=matching_observations("stg-vegas-tunnel"),
        symbol="BTCUSDT",
        direction="long",
        intended_entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        planned_rr=2.0,
        timeframe_context="1h",
        timeframe_trigger="15m",
        market_context="live setup",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis="taken live",
        chart_refs=["fixture://chart/pretrade"],
        created_at="2026-09-12T00:10:00Z",
        provider_evidence=entry_evidence,
        provider="propw",
        provider_lifecycle_id="live-open-1",
        provider_execution_timestamp="2026-09-12T00:10:30Z",
        candidate_source="fixture://live-candidate-match",
        actual_entry=100.0,
    )
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref="fixture://live-exit-claim")
    store.attach_evidence(
        draft_id,
        {"source_ref": "fixture://live-exit-claim", "timing": "post_trade", "type": "manual_note", "category": "exit", "sha256": "9" * 64},
    )
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=["fixture://live-exit-claim"], realized_r=2.0)

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = _row_for(analytics, trade_id)
    assert row["counted"] is True
    assert row["exclusion_reason"] is None
    assert row["evidence_quality"] is None  # not a BACKTEST-only field for LIVE rows



def test_trade_view_shows_not_counted_backtest_row(tmp_path: Path) -> None:
    """8. Trade View visibility: a counted=false BT row still appears in
    trade-view-data.json with its trade_no, mode=backtest, and the exclusion
    reason visible -- never silently hidden."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    weak_entry_evidence = {
        "source_ref": "fixture://tv-weak-entry",
        "timing": "execution",
        "type": "manual_note",
        "category": "entry",
        "sha256": "a" * 64,
    }
    result = capture_backtest(store, **_backtest_kwargs(entry_evidence=weak_entry_evidence))
    draft_id, trade_id = result["draft_id"], result["trade_id"]
    trade_no = result["trade_no"]

    weak_exit_evidence = {
        "source_ref": "fixture://tv-weak-exit",
        "timing": "post_trade",
        "type": "manual_note",
        "category": "exit",
        "sha256": "b" * 64,
    }
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=weak_exit_evidence["source_ref"])
    store.attach_evidence(draft_id, weak_exit_evidence)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[weak_exit_evidence["source_ref"]], realized_r=2.0)

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    refresh_health(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    build_trade_view(tmp_path)

    view = json.loads((tmp_path / "exports/operational/trade-view/trade-view-data.json").read_text(encoding="utf-8"))
    row = next(r for r in view["rows"] if r["reference"] == trade_id)
    assert row["mode"] == "backtest"
    assert row["counted"] is False
    assert row["exclusion_reason"] == "backtest_evidence_insufficient"
    assert trade_no.startswith("BT")
