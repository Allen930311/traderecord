from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from resolver_helpers import MATCHING_OBSERVATIONS_FILE, live_resolution
from trading_os.backtest_handoff import _digest as handoff_digest, emit_handoff, _eligible_candidate
from trading_os.evidence_store import EvidenceStore
from trading_os.operational_analytics import (
    _chronology_quality,
    _operational_rule_observability,
    _group_metrics,
    _summary_by_mode,
    chronology_key,
    refresh_analytics,
)
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.strategy_health import refresh_health
from trading_os.trade_view import build_trade_view
from trading_os.writer import CanonicalWriter
from review_test_support import attach_fixture_image, owner_confirmed_review, review_candidate


FIXED_GENERATED_AT = "2026-09-11T00:00:00Z"
FIXED_SOURCE_COMMIT = "a" * 40


def _copy_operational_inputs(source: Path, destination: Path) -> None:
    for relative in ("registry", "exports/method-validation", "exports/trader-dna/v1"):
        shutil.copytree(source / relative, destination / relative)
    (destination / "canonical" / "adjudications").mkdir(parents=True)
    (destination / "canonical" / "sequences").mkdir(parents=True)
    (destination / "canonical" / "records" / "trades").mkdir(parents=True)
    (destination / "canonical" / "records" / "observations").mkdir(parents=True)
    (destination / "canonical" / "records" / "superseded").mkdir(parents=True)
    (destination / "canonical" / "sequences" / "numbers.json").write_text(
        json.dumps({"live_next": 1, "backtest_next": 1, "allocated": [], "tombstones": []}) + "\n",
        encoding="utf-8",
    )
    (destination / "canonical" / "sequences" / "finalizations.json").write_text(
        json.dumps({"schema_version": "finalization-index.v1", "records": {}}) + "\n",
        encoding="utf-8",
    )


def _make_fixture(source: Path, destination: Path, *, write_review: bool = True, realized_r: float = 2.0, include_partial: bool = True) -> tuple[LifecycleStore, str, str]:
    _copy_operational_inputs(source, destination)
    store = LifecycleStore(destination)
    draft_id = "rec_11111111111111111111"
    store.create_draft(
        strategy_id="stg-vegas-tunnel",
        strategy_resolution=live_resolution("stg-vegas-tunnel", evidence_ref="fixture://pretrade"),
        rule_version="stg-vegas-tunnel:v1",
        symbol="BTCUSDT",
        direction="long",
        intended_entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        planned_rr=2.0,
        timeframe_context="1h",
        timeframe_trigger="15m",
        market_context="fixture market context",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis="fixture thesis",
        chart_refs=["fixture://chart/pretrade"],
        created_at="2026-09-11T00:00:00Z",
        draft_id=draft_id,
    )
    attach_fixture_image(store, draft_id, source_ref="fixture://pretrade", captured_at="2026-09-11T00:01:00Z")
    store.mark_entry_pending(draft_id, provider={"provider": "fixture"}, occurred_at="2026-09-11T00:02:00Z")
    store.attach_evidence(
        draft_id,
        {
            "source_ref": "fixture://propw-fill",
            "timing": "execution",
            "type": "broker_fill",
            "category": "entry",
            "sha256": "b" * 64,
            "captured_at": "2026-09-11T00:02:30Z",
        },
    )
    store.create_identity_link(
        draft_id,
        provider="fixture",
        provider_lifecycle_id="fixture-open-1",
        instrument="BTCUSDT",
        direction="long",
        provider_execution_timestamp="2026-09-11T00:03:00Z",
        candidate_source="fixture://candidate-match",
        match_disposition="MATCHED",
        evidence_refs=["fixture://propw-fill"],
        created_at="2026-09-11T00:02:45Z",
        authority="fixture_deterministic_match",
    )
    store.verify_entry(
        draft_id,
        instrument="BTCUSDT",
        direction="long",
        actual_entry=100.0,
        execution_at="2026-09-11T00:03:00Z",
        source_ref="fixture://propw-fill",
        provider_lifecycle_id="fixture-open-1",
        provider="fixture",
    )
    finalized = store.finalize(draft_id, occurred_at="2026-09-11T00:04:00Z")
    trade_id = finalized["trade_id"]
    if include_partial:
        store.partial_close(
            trade_id,
            exit_price=104.0,
            closed_at="2026-09-11T00:05:00Z",
            quantity_fraction=0.5,
            provider_pnl=4.0,
            source_ref="fixture://partial-exit",
        )
    store.final_close(
        trade_id,
        exit_price=100.0 + (5.0 * realized_r),
        closed_at="2026-09-11T00:06:00Z",
        realized_r=realized_r,
        provider_pnl=5.0 * realized_r,
        source_ref="fixture://final-exit",
    )
    store.attach_evidence(
        draft_id,
        {
            "source_ref": "fixture://final-exit",
            "timing": "post_trade",
            "type": "broker_fill",
            "category": "exit",
            "sha256": "c" * 64,
            "captured_at": "2026-09-11T00:06:30Z",
        },
    )
    store.verify_exit(
        trade_id,
        authority_class="owner_attestation",
        evidence_refs=["fixture://final-exit"],
        realized_r=realized_r,
        provider_pnl=5.0 * realized_r,
        provider_pnl_authority="provider_read_only",
    )
    if write_review:
        owner_confirmed_review(
            store,
            trade_id,
            {
            "what_went_well": "fixture setup was recorded before entry",
            "what_went_wrong": "none observed",
            "rule_compliance": "compliant",
            "execution_deviation": "none observed",
            "thesis_validity": "supported by fixture close",
            "entry_quality": "observed",
            "exit_quality": "observed",
            "evidence_completeness": "complete",
            "owner_notes": "fixture review",
            "review_status": "reviewed",
            "diagnostic_class": "METHOD",
            },
            reviewed_at="2026-09-11T00:08:00Z",
        )
    return store, draft_id, trade_id


def test_owner_statement_append_preserves_exact_text_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(__file__).resolve().parents[1]
    store, _, trade_id = _make_fixture(source, tmp_path / "owner-statement")
    exact_text = "我認為沒有任何的進場條件，\n後續撞到套保。"
    generated_now = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    monkeypatch.setattr("trading_os.operational_lifecycle._now", lambda: generated_now)

    first = store.record_owner_statement(
        trade_id,
        verbatim_text=exact_text,
        source_id="owner-correction:T000033:phase-2c-closeout",
    )
    second = store.record_owner_statement(
        trade_id,
        verbatim_text=exact_text,
        source_id="owner-correction:T000033:phase-2c-closeout",
    )

    assert first["event_id"] == second["event_id"]
    assert first["event_type"] == "owner_statement_recorded"
    assert first["payload"]["verbatim_text"] == exact_text
    assert first["payload"]["authority"] == "owner_supplied_verbatim"
    assert first["occurred_at"] == first["recorded_at"] == generated_now
    record = store.project()["records"][trade_id]
    assert len(record["owner_statements"]) == 1
    assert record["owner_statements"][0]["verbatim_text"] == exact_text
    assert record["state"] == "REVIEWED"

    with pytest.raises(LifecycleError, match="retry changed text"):
        store.record_owner_statement(
            trade_id,
            verbatim_text=exact_text + " changed",
            source_id="owner-correction:T000033:phase-2c-closeout",
        )


@pytest.mark.parametrize("invalid_text", ["", "   ", None])
def test_owner_statement_append_rejects_missing_verbatim_text(tmp_path: Path, invalid_text: object) -> None:
    source = Path(__file__).resolve().parents[1]
    store, _, trade_id = _make_fixture(source, tmp_path / "owner-statement-invalid")
    with pytest.raises(LifecycleError, match="non-empty verbatim text"):
        store.record_owner_statement(
            trade_id,
            verbatim_text=invalid_text,  # type: ignore[arg-type]
            source_id="owner-correction:T000033:phase-2c-closeout",
        )


def _make_pending(source: Path, destination: Path, *, disposition: str = "MATCHED", instrument: str = "BTCUSDT", direction: str = "long", provider_lifecycle_id: str = "fixture-open-1") -> tuple[LifecycleStore, str, str]:
    _copy_operational_inputs(source, destination)
    store = LifecycleStore(destination)
    draft_id = "rec_22222222222222222222"
    source_ref = "fixture://identity-fill"
    store.create_draft(
        strategy_id="stg-vegas-tunnel", rule_version="stg-vegas-tunnel:v1", strategy_resolution=live_resolution("stg-vegas-tunnel"), symbol="BTCUSDT", direction=direction,
        intended_entry=100.0, stop_loss=95.0, take_profit=110.0, planned_rr=2.0,
        timeframe_context="1h", timeframe_trigger="15m", market_context="identity fixture",
        setup_tags=["vegas_tunnel"], confirmation_tags=[], thesis="identity thesis",
        chart_refs=["fixture://chart"], created_at="2026-09-11T01:00:00Z", draft_id=draft_id,
    )
    store.mark_entry_pending(draft_id, provider={"provider": "propw"}, occurred_at="2026-09-11T01:01:00Z")
    store.attach_evidence(draft_id, {"source_ref": source_ref, "timing": "execution", "type": "broker_fill", "category": "entry", "sha256": "d" * 64, "captured_at": "2026-09-11T01:01:30Z"})
    store.create_identity_link(
        draft_id, provider="propw", provider_lifecycle_id=provider_lifecycle_id,
        instrument=instrument, direction=direction, provider_execution_timestamp="2026-09-11T01:02:00Z",
        candidate_source="fixture://candidate", match_disposition=disposition, evidence_refs=[source_ref],
        created_at="2026-09-11T01:01:45Z", authority="fixture_deterministic_match",
    )
    return store, draft_id, source_ref


def _make_open_trade(source: Path, destination: Path) -> tuple[LifecycleStore, str, str]:
    store, draft_id, source_ref = _make_pending(source, destination)
    store.verify_entry(draft_id, instrument="BTCUSDT", direction="long", actual_entry=100.0, execution_at="2026-09-11T01:02:00Z", source_ref=source_ref, provider_lifecycle_id="fixture-open-1")
    trade_id = store.finalize(draft_id, occurred_at="2026-09-11T01:03:00Z")["trade_id"]
    return store, draft_id, trade_id


def _run_cli(root: Path, *args: str) -> dict:
    source = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([sys.executable, "-m", "trading_os", *args], cwd=root, env=env, capture_output=True, text=True, check=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"trade_id": result.stdout.strip()}


def _run_cli_owner_confirmed(root: Path, preview_digest: str, *args: str) -> None:
    """Run the local review orchestration with one synthetic Owner approval."""
    from trading_os.__main__ import main as trading_os_main
    from trading_os.review_authorization import _authorize_local_review_after_owner_confirmation

    previous = Path.cwd()
    approval_calls = []
    os.chdir(root)
    try:
        def owner_approval(preview):
            approval_calls.append(preview["preview_digest"])
            if preview.get("preview_digest") != preview_digest:
                return None
            return _authorize_local_review_after_owner_confirmation(preview)

        trading_os_main(list(args), owner_approval=owner_approval)
    finally:
        os.chdir(previous)
    return approval_calls


def _complete_operational_trade(
    store: LifecycleStore,
    *,
    index: int,
    realized_r: float,
    diagnostic_class: str = "METHOD",
    rule_compliance: str = "compliant",
    symbol: str = "BTCUSDT",
    strategy_id: str = "stg-vegas-tunnel",
    rule_version: str | None = None,
    setup_tags: list[str] | None = None,
    confirmation_tags: list[str] | None = None,
    variant: str | None = None,
    target_price: float | None = None,
) -> tuple[str, str]:
    """Create one post-cutover trade through the public lifecycle APIs."""

    draft_id = f"rec_{index:020d}"
    base_minute = 10 + index
    date = "2026-09-12"
    created_at = f"{date}T00:{base_minute:02d}:00Z"
    entry_at = f"{date}T00:{base_minute:02d}:30Z"
    closed_at = f"{date}T01:{base_minute:02d}:00Z"
    rule_version = rule_version or f"{strategy_id}:v1"
    setup_tags = list(setup_tags) if setup_tags is not None else ["vegas_tunnel"]
    confirmation_tags = list(confirmation_tags) if confirmation_tags is not None else []
    pre_ref = f"fixture://dna/{index}/pre"
    entry_ref = f"fixture://dna/{index}/entry"
    exit_ref = f"fixture://dna/{index}/exit"
    store.create_draft(
        strategy_id=strategy_id,
        strategy_resolution=live_resolution(strategy_id, evidence_ref=pre_ref),
        rule_version=rule_version,
        symbol=symbol,
        direction="long",
        intended_entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        planned_rr=2.0,
        timeframe_context="1h",
        timeframe_trigger="15m",
        market_context="operational synthetic fixture",
        setup_tags=setup_tags,
        confirmation_tags=confirmation_tags,
        thesis="synthetic operational observation",
        chart_refs=[pre_ref],
        created_at=created_at,
        draft_id=draft_id,
        variant=variant,
    )
    if target_price is not None:
        # Triple Tap inherits the frozen ICT target operand.  Keep the exact
        # canonical field explicit in this isolated fixture rather than
        # inferring it from take_profit_price in production analytics.
        store._update_draft(draft_id, {"plan": {"target_price": target_price}})
    attach_fixture_image(store, draft_id, source_ref=pre_ref, captured_at=created_at)
    store.mark_entry_pending(draft_id, provider={"provider": "propw"}, occurred_at=entry_at)
    store.attach_evidence(
        draft_id,
        {"source_ref": entry_ref, "timing": "execution", "type": "broker_fill", "category": "entry", "sha256": hashlib.sha256(entry_ref.encode()).hexdigest(), "captured_at": entry_at},
    )
    store.create_identity_link(
        draft_id,
        provider="propw",
        provider_lifecycle_id=f"dna-open-{index}",
        instrument=symbol,
        direction="long",
        provider_execution_timestamp=entry_at,
        candidate_source=f"fixture://dna/{index}/candidate",
        match_disposition="MATCHED",
        evidence_refs=[entry_ref],
        decision_created_at=f"{date}T00:{base_minute:02d}:40Z",
        authority="fixture_deterministic_match",
    )
    store.verify_entry(
        draft_id,
        instrument=symbol,
        direction="long",
        actual_entry=100.0,
        execution_at=entry_at,
        source_ref=entry_ref,
        provider_lifecycle_id=f"dna-open-{index}",
        provider="propw",
    )
    trade_id = store.finalize(draft_id, occurred_at=f"{date}T00:{base_minute:02d}:50Z")["trade_id"]
    store.final_close(trade_id, exit_price=100.0 + realized_r, closed_at=closed_at, realized_r=realized_r, source_ref=exit_ref)
    store.attach_evidence(
        draft_id,
        {"source_ref": exit_ref, "timing": "post_trade", "type": "broker_fill", "category": "exit", "sha256": hashlib.sha256(exit_ref.encode()).hexdigest(), "captured_at": closed_at},
    )
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[exit_ref], realized_r=realized_r)
    owner_confirmed_review(
        store,
        trade_id,
        {
            "what_went_well": "evidence captured",
            "what_went_wrong": "synthetic fixture observation",
            "rule_compliance": rule_compliance,
            "execution_deviation": "observed" if diagnostic_class == "EXECUTION" else "none observed",
            "thesis_validity": "observed",
            "entry_quality": "observed",
            "exit_quality": "observed",
            "evidence_completeness": "complete",
            "owner_notes": "synthetic fixture",
            "review_status": "reviewed",
            "diagnostic_class": diagnostic_class,
        },
        reviewed_at=f"{date}T01:{base_minute:02d}:30Z",
    )
    return draft_id, trade_id


def test_operational_e2e_is_deterministic_and_review_queue_clears(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, draft_id, trade_id = _make_fixture(source, tmp_path)

    projection = store.current()
    record = projection["records"][trade_id]
    assert record["state"] == "REVIEWED"
    assert record["draft_id"] == draft_id
    assert trade_id in {record["trade_id"], trade_id}
    assert {item["category"] for item in record["evidence"]} == {"pre_trade_context", "entry", "exit"}
    assert all(item["linked_draft_id"] == draft_id for item in record["evidence"])
    assert all(item["linked_trade_id"] == trade_id for item in record["evidence"])
    assert len(store._read_events()) == 13
    assert store.finalize(draft_id, occurred_at="2026-09-11T00:04:00Z")["trade_id"] == trade_id
    assert len(store._read_events()) == 13

    first = refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics_path = tmp_path / "exports/operational/analytics/current.json"
    first_bytes = analytics_path.read_bytes()
    analytics = json.loads(first_bytes)
    assert analytics["counts"]["historical_seed_count"] == 133
    assert analytics["counts"]["countable_observation_count"] == 134
    assert analytics["counts"]["migrated_overlap_records"] == 0
    assert analytics["counts"]["new_post_cutover_records"] == 1
    new_row = next(row for row in analytics["observations"] if row["trade_id"] == trade_id)
    assert new_row["source_kind"] == "post_cutover_operational"
    assert new_row["counted"] is True
    assert new_row["realized_r"] == 2.0
    assert new_row["provider_pnl"] == 10.0
    assert new_row["exit_verification"]["state"] == "confirmed"
    assert new_row["identity_verification"]["state"] == "confirmed"
    assert new_row["method_observability"]["status"] == "machine_observable"
    assert new_row["method_observability"]["required_rules"][0]["evidence_refs"] == ["fixture://pretrade"]
    assert new_row["operational_method_eligible"] is True
    assert analytics["source_version_pins"]["trader_dna_v1"]["manifest_digest"]

    second = refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    assert first == second
    assert analytics_path.read_bytes() == first_bytes

    health_manifest = refresh_health(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    health = json.loads((tmp_path / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    vegas = next(item for item in health["strategies"] if item["strategy_id"] == "stg-vegas-tunnel")
    assert vegas["operational_observability"]["operational_observed_n"] == 1
    assert vegas["operational_observability"]["operational_method_eligible_n"] == 1
    assert vegas["diagnostic_separation"]["execution"] == "observed"
    assert vegas["execution_quality"]["rule_violation_rate"] == 0.0
    assert vegas["candidate_improvement_id"] is None
    assert health_manifest["optimization_performed"] is False

    view_manifest = build_trade_view(tmp_path)
    view_data_path = tmp_path / "exports/operational/trade-view/trade-view-data.json"
    view_bytes = view_data_path.read_bytes()
    view = json.loads(view_bytes)
    assert "status" in view["filters"]
    # The generated markup lives in app.js, not the static index.html shell
    # (just <link>/<script> tags); Trade View V3 builds the status filter
    # client-side as `journal-status`; see trade_view.py APP_JS_V3 `toolbarHtml()`.
    assert 'id="journal-status"' in (tmp_path / "exports/operational/trade-view/app.js").read_text(encoding="utf-8")
    assert any(row["reference"] == trade_id and row["close_date"] == "2026-09-11T00:06:00Z" for row in view["rows"])
    references = [row["reference"] for row in view["rows"]]
    assert len(references) == len(set(references))
    assert references.count(trade_id) == 1
    assert view["review_queue"] == []
    assert view_manifest["local_only"] is True
    assert view_data_path.read_bytes() == view_bytes

    handoff = emit_handoff(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    handoff_payload = json.loads((tmp_path / "exports/operational/handoff/current.json").read_text(encoding="utf-8"))
    assert handoff["status"] == "no_eligible_candidate"
    assert handoff_payload["candidate"] is None
    assert handoff_payload["proposed_rule_delta"] is None
    assert handoff_payload["guardrails"]["backtest_executed"] is False


def test_canonical_review_cli_persists_then_refreshes_and_verifies_projections(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, _, trade_id = _make_fixture(source, tmp_path, write_review=False)
    assert store.current()["records"][trade_id]["state"] == "CLOSED"

    review_path = tmp_path / "review.json"
    review_payload = review_candidate(store, trade_id, {
        "what_went_well": "plan was recorded before entry",
        "what_went_wrong": "none observed",
        "rule_compliance": "compliant",
        "execution_deviation": "none observed",
        "thesis_validity": "supported by verified close",
        "entry_quality": "observed",
        "exit_quality": "observed",
        "evidence_completeness": "complete",
        "owner_notes": "single trade review regression",
        "diagnostic_class": "METHOD",
    })
    review_path.write_text(json.dumps(review_payload), encoding="utf-8")
    review_image = tmp_path / "review-intake.png"
    review_image.write_bytes(b"\x89PNG\r\n\x1a\nlocal review image original bytes")
    evidence_intake = tmp_path / "review-evidence-intake.json"
    evidence_intake.write_text(json.dumps({
        "schema_version": "trade-review-evidence-intake.v1",
        "images": [{"path": str(review_image), "type": "chart_snapshot", "captured_at": "2026-09-11T00:07:00Z"}],
    }), encoding="utf-8")
    artifact_dir = tmp_path / "canonical/evidence/artifacts"
    artifacts_before = {path.name for path in artifact_dir.iterdir()}
    event_count_before = len(store._read_events())
    preview = _run_cli(tmp_path, "capture", "review-preview", "--trade-id", trade_id, "--review-file", str(review_path), "--evidence-intake", str(evidence_intake))
    assert {path.name for path in artifact_dir.iterdir()} == artifacts_before
    assert len(store._read_events()) == event_count_before
    with pytest.raises(subprocess.CalledProcessError):
        _run_cli(
            tmp_path,
            "capture", "review", "--trade-id", trade_id,
            "--reviewed-at", "2026-09-11T00:08:00Z", "--review-file", str(review_path),
            "--evidence-intake", str(evidence_intake),
        )
    assert {path.name for path in artifact_dir.iterdir()} == artifacts_before
    assert len(store._read_events()) == event_count_before
    with pytest.raises(subprocess.CalledProcessError):
        _run_cli(
            tmp_path,
            "capture", "review", "--trade-id", trade_id,
            "--reviewed-at", "2026-09-11T00:08:00Z", "--review-file", str(review_path),
            "--owner-confirmation-digest", preview["preview_digest"],
            "--owner-confirmed-at", "2026-09-11T00:07:30Z",
        )
    assert {path.name for path in artifact_dir.iterdir()} == artifacts_before
    assert len(store._read_events()) == event_count_before
    approval_calls = _run_cli_owner_confirmed(
        tmp_path,
        preview["preview_digest"],
        "capture", "review", "--trade-id", trade_id,
        "--reviewed-at", "2026-09-11T00:08:00Z", "--review-file", str(review_path),
        "--evidence-intake", str(evidence_intake),
    )
    assert approval_calls == [preview["preview_digest"]]
    assert store.current()["records"][trade_id]["state"] == "REVIEWED"
    assert store.current()["records"][trade_id]["trade_no"] == "T000001"
    attached_image = next(item for item in store.current()["records"][trade_id]["evidence"] if item.get("original_filename") == "review-intake.png")
    assert EvidenceStore(tmp_path).read(attached_image["locator"], expected_sha256=attached_image["sha256"]) == review_image.read_bytes()

    refreshed = _run_cli(tmp_path, "projections", "refresh", "--target-trade", trade_id)
    assert refreshed["status"] == "CURRENT"
    verified = _run_cli(tmp_path, "projections", "verify", "--target-trade", trade_id)
    assert verified["status"] == "CURRENT"
    assert verified["target_visibility"]["canonical_present"] is True
    assert verified["target_visibility"]["analytics_present"] is True
    assert verified["target_visibility"]["trade_view_present"] is True


def test_retrospective_replay_without_fact_binding_remains_not_observable(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    record = {
        "strategy": {"strategy_id": "stg-vegas-tunnel", "rule_version": "stg-vegas-tunnel:v1", "setup_tags": ["vegas_tunnel"]},
        "plan": {},
    }
    retrospective_only = [{
        "source_ref": "fixture://replay",
        "timing": "post_trade",
        "type": "replay_chart",
        "category": "review",
        "sha256": "a" * 64,
        "locator": "artifact:" + "a" * 64 + ".png",
        "provenance": {"capture_mode": "retrospective_replay"},
        "role": "historical_market_reconstruction",
    }]
    observed = _operational_rule_observability(tmp_path, record, retrospective_only)
    assert observed["status"] == "not_observable"
    assert observed["required_rules"][0]["state"] == "UNKNOWN"
    assert observed["required_rules"][0]["evidence_refs"] == []


def test_free_standing_replay_observation_cannot_upgrade_pre_trade_timing(tmp_path: Path) -> None:
    from trading_os.strategy_resolver import ResolverError, resolve_strategy

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    observation = {
        "rule_id": "vegas.tunnel.touch",
        "state": "PASS",
        "supports_fact_timing": "pre_trade",
        "basis": ["retrospective_replay", "owner_attestation"],
        "owner_attestation": {
            "status": "confirmed",
            "statement": "The setup existed before entry.",
        },
        "evidence_refs": ["fixture://replay"],
    }
    with pytest.raises(ResolverError, match="per-rule owner_attestation is not an authority schema"):
        resolve_strategy(tmp_path, {"observations": [observation], "plan": {}})

def test_operational_guardrails_fail_closed(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, draft_id, _ = _make_fixture(source, tmp_path)
    with pytest.raises(LifecycleError, match="psychological"):
        store.review("trd_00000000-0000-7000-8000-000000000000", {"emotional_state": "calm"}, reviewed_at=FIXED_GENERATED_AT)
    with pytest.raises(LifecycleError, match="category"):
        store.attach_evidence(
            draft_id,
            {"source_ref": "fixture://bad", "timing": "pre_trade", "type": "manual_note", "category": "emotion", "sha256": "c" * 64},
        )
    with pytest.raises(LifecycleError, match="sha256"):
        store.attach_evidence(
            draft_id,
            {"source_ref": "fixture://bad", "timing": "pre_trade", "type": "manual_note", "category": "review", "sha256": "not-a-digest"},
        )
    with pytest.raises(LifecycleError, match="review/post_trade"):
        store.attach_evidence(
            draft_id,
            {
                "source_ref": "fixture://relabeled-replay",
                "timing": "pre_trade",
                "type": "replay_chart",
                "category": "pre_trade_context",
                "sha256": "d" * 64,
                "provenance": {"capture_mode": "retrospective_replay"},
            },
        )
    with pytest.raises(ValueError, match="direct canonical revisions are retired"):
        CanonicalWriter(tmp_path).update_revision("trd_00000000-0000-7000-8000-000000000000", {}, "test")


def test_frozen_inputs_are_not_mutated_by_operational_refresh(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    pinned = [
        "exports/method-validation/b1-v3/canonical-trade-dataset.jsonl",
        "exports/method-validation/b1-v3/manifest.json",
        "canonical/adjudications/method-validation/episode-identity-resolution-20260911.jsonl",
        "exports/method-validation/b3/manifest.json",
        "exports/method-validation/b2/manifest.json",
        "exports/method-validation/b2/strategy-performance.json",
        "exports/trader-dna/v1/manifest.json",
    ]
    before = {path: hashlib.sha256((source / path).read_bytes()).hexdigest() for path in pinned}
    fixture = tmp_path / "operational-refresh"
    _copy_operational_inputs(source, fixture)
    refresh_analytics(fixture, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    refresh_health(fixture, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    build_trade_view(fixture)
    emit_handoff(fixture, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    after = {path: hashlib.sha256((source / path).read_bytes()).hexdigest() for path in pinned}
    assert before == after


def _attach_exit_evidence(store: LifecycleStore, draft_id: str, source_ref: str = "fixture://verified-exit") -> None:
    store.attach_evidence(
        draft_id,
        {
            "source_ref": source_ref,
            "timing": "post_trade",
            "type": "broker_fill",
            "category": "exit",
            "sha256": hashlib.sha256(source_ref.encode("utf-8")).hexdigest(),
            "captured_at": "2026-09-11T01:03:30Z",
        },
    )


def test_public_cli_finalization_is_single_idempotent_path(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    draft_id = "rec_33333333333333333333"
    common = (
        "--strategy-observations-file", MATCHING_OBSERVATIONS_FILE,
        "--symbol", "BTCUSDT", "--direction", "long", "--entry", "100", "--sl", "95", "--tp", "110",
        "--planned-rr", "2", "--timeframe-context", "1h", "--timeframe-trigger", "15m",
        "--market-context", "cli fixture", "--thesis", "cli thesis", "--created-at", "2026-09-11T02:00:00Z",
        "--draft-id", draft_id,
    )
    _run_cli(tmp_path, "capture", "draft", *common)
    _run_cli(tmp_path, "capture", "evidence", "--draft-id", draft_id, "--source-ref", "fixture://cli-pre",
             "--sha256", "a" * 64, "--category", "pre_trade_context", "--timing", "pre_trade", "--type", "manual_note", "--captured-at", "2026-09-11T02:00:30Z")
    _run_cli(tmp_path, "capture", "evidence", "--draft-id", draft_id, "--source-ref", "fixture://cli-pre",
             "--sha256", "a" * 64, "--category", "pre_trade_context", "--timing", "pre_trade", "--type", "manual_note", "--captured-at", "2026-09-11T02:00:30Z")
    _run_cli(tmp_path, "capture", "entry-pending", "--draft-id", draft_id, "--provider", "propw", "--occurred-at", "2026-09-11T02:01:00Z")
    _run_cli(tmp_path, "capture", "evidence", "--draft-id", draft_id, "--source-ref", "fixture://cli-fill",
             "--sha256", "b" * 64, "--category", "entry", "--timing", "execution", "--type", "broker_fill", "--captured-at", "2026-09-11T02:01:30Z")
    link_args = (
        "--draft-id", draft_id, "--provider", "propw", "--provider-lifecycle-id", "cli-open-1",
        "--instrument", "BTCUSDT", "--direction", "long", "--provider-execution-at", "2026-09-11T02:02:00Z",
        "--candidate-source", "fixture://cli-candidate", "--match-disposition", "MATCHED", "--evidence-ref", "fixture://cli-fill",
        "--created-at", "2026-09-11T02:01:45Z",
    )
    link = _run_cli(tmp_path, "capture", "identity-link", *link_args)
    _run_cli(tmp_path, "capture", "verify-entry", "--draft-id", draft_id, "--provider", "propw", "--instrument", "BTCUSDT",
             "--direction", "long", "--actual-entry", "100", "--execution-at", "2026-09-11T02:02:00Z",
             "--source-ref", "fixture://cli-fill", "--provider-lifecycle-id", "cli-open-1")
    _run_cli(tmp_path, "capture", "verify-entry", "--draft-id", draft_id, "--provider", "propw", "--instrument", "BTCUSDT",
             "--direction", "long", "--actual-entry", "100", "--execution-at", "2026-09-11T02:02:00Z",
             "--source-ref", "fixture://cli-fill", "--provider-lifecycle-id", "cli-open-1")
    final = _run_cli(tmp_path, "capture", "finalize", "--draft-id", draft_id, "--reviewer", "owner", "--occurred-at", "2026-09-11T02:02:30Z")
    replay_capture = _run_cli(tmp_path, "capture", "finalize", "--draft-id", draft_id, "--reviewer", "owner", "--occurred-at", "2026-09-11T02:02:30Z")
    replay_legacy = _run_cli(tmp_path, "finalize", draft_id, "--reviewer", "owner")
    assert final["trade_id"] == replay_capture["trade_id"] == replay_legacy["trade_id"]
    trade_id = final["trade_id"]
    _run_cli(tmp_path, "capture", "evidence", "--draft-id", draft_id, "--source-ref", "fixture://cli-exit",
             "--sha256", "c" * 64, "--category", "exit", "--timing", "post_trade", "--type", "broker_fill", "--captured-at", "2026-09-11T02:02:40Z")
    _run_cli(tmp_path, "capture", "evidence", "--draft-id", draft_id, "--source-ref", "fixture://cli-exit",
             "--sha256", "c" * 64, "--category", "exit", "--timing", "post_trade", "--type", "broker_fill", "--captured-at", "2026-09-11T02:02:40Z")
    _run_cli(tmp_path, "capture", "partial-close", "--trade-id", trade_id, "--exit-price", "104", "--closed-at", "2026-09-11T02:03:00Z", "--quantity-fraction", "0.5")
    _run_cli(tmp_path, "capture", "partial-close", "--trade-id", trade_id, "--exit-price", "104", "--closed-at", "2026-09-11T02:03:00Z", "--quantity-fraction", "0.5")
    _run_cli(tmp_path, "capture", "close", "--trade-id", trade_id, "--exit-price", "110", "--closed-at", "2026-09-11T02:04:00Z", "--realized-r", "2", "--provider-pnl", "10", "--source-ref", "fixture://cli-exit")
    _run_cli(tmp_path, "capture", "close", "--trade-id", trade_id, "--exit-price", "110", "--closed-at", "2026-09-11T02:04:00Z", "--realized-r", "2", "--provider-pnl", "10", "--source-ref", "fixture://cli-exit")
    _run_cli(tmp_path, "capture", "verify-exit", "--trade-id", trade_id, "--authority-class", "owner_attestation", "--evidence-ref", "fixture://cli-exit", "--realized-r", "2", "--provider-pnl", "10", "--provider-pnl-authority", "provider_read_only")
    _run_cli(tmp_path, "capture", "verify-exit", "--trade-id", trade_id, "--authority-class", "owner_attestation", "--evidence-ref", "fixture://cli-exit", "--realized-r", "2", "--provider-pnl", "10", "--provider-pnl-authority", "provider_read_only")
    review_path = tmp_path / "cli-review.json"
    review_payload = review_candidate(LifecycleStore(tmp_path), trade_id, {
        "what_went_well": "pre-trade evidence was captured",
        "what_went_wrong": "none observed",
        "rule_compliance": "unknown",
        "execution_deviation": "none observed",
        "thesis_validity": "observed",
        "entry_quality": "observed",
        "exit_quality": "observed",
        "evidence_completeness": "complete",
        "owner_notes": "cli fixture",
        "review_status": "reviewed",
        "diagnostic_class": "METHOD",
    })
    review_path.write_text(json.dumps(review_payload), encoding="utf-8")
    preview = _run_cli(tmp_path, "capture", "review-preview", "--trade-id", trade_id, "--review-file", str(review_path))
    review_args = ["capture", "review", "--trade-id", trade_id, "--reviewed-at", "2026-09-11T02:05:00Z", "--review-file", str(review_path)]
    assert _run_cli_owner_confirmed(tmp_path, preview["preview_digest"], *review_args) == [preview["preview_digest"]]
    _run_cli(tmp_path, "analytics", "refresh", "--generated-at", FIXED_GENERATED_AT, "--source-commit", FIXED_SOURCE_COMMIT)
    _run_cli(tmp_path, "health", "refresh", "--generated-at", FIXED_GENERATED_AT, "--source-commit", FIXED_SOURCE_COMMIT)
    _run_cli(tmp_path, "trade-view", "--build-only")
    handoff = _run_cli(tmp_path, "handoff", "--generated-at", FIXED_GENERATED_AT, "--source-commit", FIXED_SOURCE_COMMIT)
    assert handoff["status"] == "no_eligible_candidate"
    _run_cli(tmp_path, "capture", "draft", *common)
    _run_cli(tmp_path, "capture", "entry-pending", "--draft-id", draft_id, "--provider", "propw", "--occurred-at", "2026-09-11T02:01:00Z")
    _run_cli(tmp_path, "capture", "identity-link", *link_args)
    assert link["payload"]["identity_link_id"].startswith("oil_")
    events = LifecycleStore(tmp_path)._read_events()
    assert sum(event["event_type"] == "reviewed" for event in events) == 1
    assert sum(event["event_type"] == "canonical_finalized" for event in events) == 1
    assert len(events) == 13
    assert LifecycleStore(tmp_path).current()["records"][trade_id]["state"] == "REVIEWED"
    assert len((tmp_path / "canonical" / "records" / "trades").glob("*.json").__iter__().__next__().read_bytes()) > 0


def test_identity_link_dispositions_and_exact_conflicts_fail_closed(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    ambiguous, draft_id, source_ref = _make_pending(source, tmp_path / "ambiguous", disposition="AMBIGUOUS")
    with pytest.raises(LifecycleError, match="MATCHED governed identity link"):
        ambiguous.verify_entry(draft_id, instrument="BTCUSDT", direction="long", actual_entry=100, execution_at="2026-09-11T01:02:00Z", source_ref=source_ref, provider_lifecycle_id="fixture-open-1")
    unmatched, draft_id, source_ref = _make_pending(source, tmp_path / "unmatched", disposition="UNMATCHED")
    with pytest.raises(LifecycleError, match="MATCHED governed identity link"):
        unmatched.verify_entry(draft_id, instrument="BTCUSDT", direction="long", actual_entry=100, execution_at="2026-09-11T01:02:00Z", source_ref=source_ref, provider_lifecycle_id="fixture-open-1")
    conflict, draft_id, source_ref = _make_pending(source, tmp_path / "conflict", disposition="CONFLICT")
    with pytest.raises(LifecycleError, match="MATCHED governed identity link"):
        conflict.verify_entry(draft_id, instrument="BTCUSDT", direction="long", actual_entry=100, execution_at="2026-09-11T01:02:00Z", source_ref=source_ref, provider_lifecycle_id="fixture-open-1")
    mismatch, draft_id, source_ref = _make_pending(source, tmp_path / "mismatch", disposition="AMBIGUOUS")
    with pytest.raises(LifecycleError, match="conflicts with draft symbol or direction"):
        mismatch.create_identity_link(draft_id, provider="propw", provider_lifecycle_id="fixture-open-2", instrument="ETHUSDT", direction="long", provider_execution_timestamp="2026-09-11T01:02:00Z", candidate_source="fixture://candidate", match_disposition="MATCHED", evidence_refs=[source_ref], created_at="2026-09-11T01:01:50Z", authority="fixture_deterministic_match")
    mismatch = _make_pending(source, tmp_path / "verify-mismatch")[0]
    draft_id = "rec_22222222222222222222"
    source_ref = "fixture://identity-fill"
    with pytest.raises(LifecycleError, match="conflicts with governed identity link"):
        mismatch.verify_entry(draft_id, instrument="ETHUSDT", direction="long", actual_entry=100, execution_at="2026-09-11T01:02:00Z", source_ref=source_ref, provider_lifecycle_id="fixture-open-1")
    provider_mismatch = _make_pending(source, tmp_path / "provider-mismatch")[0]
    with pytest.raises(LifecycleError, match="MATCHED governed identity link"):
        provider_mismatch.verify_entry("rec_22222222222222222222", instrument="BTCUSDT", direction="long", actual_entry=100, execution_at="2026-09-11T01:02:00Z", source_ref="fixture://identity-fill", provider_lifecycle_id="other-open-1")
    wrong_direction, draft_id, source_ref = _make_pending(source, tmp_path / "wrong-direction", direction="short")
    with pytest.raises(LifecycleError, match="conflicts with governed identity link"):
        wrong_direction.verify_entry(draft_id, instrument="BTCUSDT", direction="long", actual_entry=100, execution_at="2026-09-11T01:02:00Z", source_ref=source_ref, provider_lifecycle_id="fixture-open-1")
    matched, draft_id, source_ref = _make_pending(source, tmp_path / "matched")
    before = len(matched._read_events())
    first = matched.create_identity_link(draft_id, provider="propw", provider_lifecycle_id="fixture-open-1", instrument="BTCUSDT", direction="long", provider_execution_timestamp="2026-09-11T01:02:00Z", candidate_source="fixture://candidate", match_disposition="MATCHED", evidence_refs=[source_ref], created_at="2026-09-11T01:01:45Z", authority="fixture_deterministic_match")
    second = matched.create_identity_link(draft_id, provider="propw", provider_lifecycle_id="fixture-open-1", instrument="BTCUSDT", direction="long", provider_execution_timestamp="2026-09-11T01:02:00Z", candidate_source="fixture://candidate", match_disposition="MATCHED", evidence_refs=[source_ref], created_at="2026-09-11T01:01:45Z", authority="fixture_deterministic_match")
    assert first["event_id"] == second["event_id"]
    assert len(matched._read_events()) == before


def test_identity_evidence_must_be_attached_entry_provider_execution_evidence(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, draft_id, source_ref = _make_pending(source, tmp_path)
    with pytest.raises(LifecycleError, match="not attached"):
        store.create_identity_link(
            draft_id, provider="propw", provider_lifecycle_id="missing-evidence", instrument="BTCUSDT", direction="long",
            provider_execution_timestamp="2026-09-11T01:02:00Z", candidate_source="fixture://candidate",
            match_disposition="MATCHED", evidence_refs=["fixture://does-not-exist"], created_at="2026-09-11T01:01:45Z",
        )
    store.attach_evidence(draft_id, {"source_ref": "fixture://manual-entry", "timing": "execution", "type": "manual_note", "category": "entry", "sha256": "e" * 64, "captured_at": "2026-09-11T01:02:10Z"})
    with pytest.raises(LifecycleError, match="provider execution evidence"):
        store.create_identity_link(
            draft_id, provider="propw", provider_lifecycle_id="manual-evidence", instrument="BTCUSDT", direction="long",
            provider_execution_timestamp="2026-09-11T01:02:00Z", candidate_source="fixture://candidate",
            match_disposition="MATCHED", evidence_refs=["fixture://manual-entry"], created_at="2026-09-11T01:01:45Z",
        )
    store.attach_evidence(draft_id, {"source_ref": "fixture://pretrade-only", "timing": "pre_trade", "type": "chart_snapshot", "category": "pre_trade_context", "sha256": "2" * 64, "captured_at": "2026-09-11T01:02:11Z"})
    with pytest.raises(LifecycleError, match="not attached"):
        store.create_identity_link(
            draft_id, provider="propw", provider_lifecycle_id="pretrade-evidence", instrument="BTCUSDT", direction="long",
            provider_execution_timestamp="2026-09-11T01:02:00Z", candidate_source="fixture://candidate",
            match_disposition="MATCHED", evidence_refs=["fixture://pretrade"], created_at="2026-09-11T01:01:45Z",
        )
    with pytest.raises(LifecycleError, match="provider execution evidence"):
        store.create_identity_link(
            draft_id, provider="propw", provider_lifecycle_id="pretrade-evidence-attached", instrument="BTCUSDT", direction="long",
            provider_execution_timestamp="2026-09-11T01:02:00Z", candidate_source="fixture://candidate",
            match_disposition="MATCHED", evidence_refs=["fixture://pretrade-only"], created_at="2026-09-11T01:01:45Z",
        )
    other_id = "rec_55555555555555555555"
    store.create_draft(
        strategy_id="stg-vegas-tunnel", rule_version="stg-vegas-tunnel:v1", strategy_resolution=live_resolution("stg-vegas-tunnel"), symbol="BTCUSDT", direction="long",
        intended_entry=100, stop_loss=95, take_profit=110, planned_rr=2, timeframe_context="1h", timeframe_trigger="15m",
        market_context="wrong draft fixture", setup_tags=["vegas_tunnel"], confirmation_tags=[], thesis="wrong draft",
        chart_refs=[], created_at="2026-09-11T01:10:00Z", draft_id=other_id,
    )
    store.attach_evidence(other_id, {"source_ref": "fixture://other-draft-fill", "timing": "execution", "type": "broker_fill", "category": "entry", "sha256": "3" * 64, "captured_at": "2026-09-11T01:10:30Z"})
    with pytest.raises(LifecycleError, match="not attached"):
        store.create_identity_link(
            draft_id, provider="propw", provider_lifecycle_id="wrong-draft-evidence", instrument="BTCUSDT", direction="long",
            provider_execution_timestamp="2026-09-11T01:02:00Z", candidate_source="fixture://candidate",
            match_disposition="MATCHED", evidence_refs=["fixture://other-draft-fill"], created_at="2026-09-11T01:01:45Z",
        )
    assert store.create_identity_link(
        draft_id, provider="propw", provider_lifecycle_id="fixture-open-2", instrument="BTCUSDT", direction="long",
        provider_execution_timestamp="2026-09-11T01:02:00Z", candidate_source="fixture://candidate",
        match_disposition="MATCHED", evidence_refs=[source_ref], created_at="2026-09-11T01:01:45Z",
    )["event_type"] == "identity_linked"


def test_fact_time_and_decision_time_are_distinct_and_replay_by_recorded_order(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    draft_id = "rec_44444444444444444444"
    fact_entry = "2026-09-11T23:10:00Z"
    store.create_draft(
        strategy_id="stg-vegas-tunnel", rule_version="stg-vegas-tunnel:v1", strategy_resolution=live_resolution("stg-vegas-tunnel"), symbol="BTCUSDT", direction="long",
        intended_entry=100, stop_loss=95, take_profit=110, planned_rr=2, timeframe_context="1h", timeframe_trigger="15m",
        market_context="fact-time fixture", setup_tags=["vegas_tunnel"], confirmation_tags=[], thesis="time fixture",
        chart_refs=["fixture://time-pre"], created_at="2026-09-11T23:00:00Z", recorded_at="2026-09-11T23:00:01Z", draft_id=draft_id,
    )
    store.attach_evidence(draft_id, {"source_ref": "fixture://time-pre", "timing": "pre_trade", "type": "chart_snapshot", "category": "pre_trade_context", "sha256": "f" * 64, "captured_at": "2026-09-11T23:01:00Z"}, recorded_at="2026-09-11T23:01:01Z")
    store.mark_entry_pending(draft_id, provider={"provider": "propw"}, occurred_at="2026-09-11T23:05:00Z", recorded_at="2026-09-11T23:05:01Z")
    store.attach_evidence(draft_id, {"source_ref": "fixture://time-fill", "timing": "execution", "type": "broker_fill", "category": "entry", "sha256": "0" * 64, "captured_at": fact_entry}, recorded_at="2026-09-11T23:10:01Z")
    store.create_identity_link(
        draft_id, provider="propw", provider_lifecycle_id="time-open-1", instrument="BTCUSDT", direction="long",
        provider_execution_timestamp=fact_entry, candidate_source="fixture://time-candidate", match_disposition="MATCHED",
        evidence_refs=["fixture://time-fill"], decision_created_at="2026-09-11T23:20:00Z", recorded_at="2026-09-11T23:20:00Z",
    )
    store.verify_entry(
        draft_id, instrument="BTCUSDT", direction="long", actual_entry=100, execution_at=fact_entry,
        source_ref="fixture://time-fill", provider_lifecycle_id="time-open-1", provider="propw", recorded_at="2026-09-11T23:21:00Z",
    )
    events = store._read_events()
    event_types = [event["event_type"] for event in events]
    assert event_types.index("identity_linked") < event_types.index("entry_verified")
    assert events[event_types.index("identity_linked")]["occurred_at"] == "2026-09-11T23:20:00Z"
    projected = store.current()["records"][draft_id]
    assert projected["identity_link"]["provider_execution_at"] == fact_entry
    assert projected["identity_link"]["decision_created_at"] == "2026-09-11T23:20:00Z"
    assert projected["identity_link"]["recorded_at"] == "2026-09-11T23:20:00Z"
    assert projected["entry"]["execution_at"] == fact_entry
    assert store.project() == store.project()


def test_provider_pnl_requires_provider_evidence_even_when_owner_verifies_r(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, draft_id, trade_id = _make_open_trade(source, tmp_path)
    manual_ref = "fixture://manual-exit-review"
    store.attach_evidence(draft_id, {"source_ref": manual_ref, "timing": "post_trade", "type": "manual_note", "category": "review", "sha256": "1" * 64, "captured_at": "2026-09-11T01:03:30Z"})
    store.final_close(trade_id, exit_price=110, closed_at="2026-09-11T01:04:00Z", realized_r=2.0, provider_pnl=10.0, source_ref=manual_ref)
    with pytest.raises(LifecycleError, match="provider exit evidence"):
        store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[manual_ref], realized_r=2.0, provider_pnl=10.0, provider_pnl_authority="provider_read_only")
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[manual_ref], realized_r=2.0, provider_pnl=10.0)
    row = store.current()["records"][trade_id]
    assert row["realized_r"] == 2.0
    assert row["provider_pnl"] is None
    assert row["provider_pnl_verification"]["state"] == "pending"


def _refresh_operational_outputs(root: Path) -> tuple[dict, dict, dict]:
    analytics_manifest = refresh_analytics(root, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    health_manifest = refresh_health(root, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    handoff_manifest = emit_handoff(root, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    return analytics_manifest, health_manifest, handoff_manifest


def test_strategy_health_positive_candidate_is_runtime_generated_without_health_patch(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    control = tmp_path / "control"
    _copy_operational_inputs(source, control)
    control_store = LifecycleStore(control)
    for index in range(1, 6):
        _complete_operational_trade(control_store, index=index, realized_r=1.0)
    _refresh_operational_outputs(control)
    control_health = json.loads((control / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    control_vegas = next(item for item in control_health["strategies"] if item["strategy_id"] == "stg-vegas-tunnel")
    assert control_health["eligible_improvement_candidates"] == []
    assert control_vegas["candidate_improvement_id"] is None
    assert json.loads((control / "exports/operational/handoff/current.json").read_text(encoding="utf-8"))["status"] == "no_eligible_candidate"

    investigation = tmp_path / "investigation"
    _copy_operational_inputs(source, investigation)
    investigation_store = LifecycleStore(investigation)
    for index in range(1, 6):
        _complete_operational_trade(investigation_store, index=index, realized_r=-1.0)
    analytics_manifest, health_manifest, handoff_manifest = _refresh_operational_outputs(investigation)
    health = json.loads((investigation / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    vegas = next(item for item in health["strategies"] if item["strategy_id"] == "stg-vegas-tunnel")
    assert vegas["operational_observability"]["status"] == "machine_observable"
    assert vegas["operational_observability"]["operational_method_eligible_n"] == 5
    assert vegas["health_state"] == "investigate"
    assert vegas["candidate_improvement_id"]
    assert vegas["candidate_improvement_ids"] == [vegas["candidate_improvement_id"]]
    assert len(vegas["improvement_candidates"]) == 1
    assert vegas["candidate_selection_state"] == "single_candidate"
    assert len(health["eligible_improvement_candidates"]) == 1
    candidate = health["eligible_improvement_candidates"][0]
    assert candidate["method_eligible_n"] == 5
    assert candidate["predeclared_gate"]["evidence_required"] is True
    assert candidate["predeclared_gate"]["execution_excluded"] is True
    assert not {"best_strategy", "rank", "winner", "top_strategy"} & set(candidate)
    handoff = json.loads((investigation / "exports/operational/handoff/current.json").read_text(encoding="utf-8"))
    assert handoff["status"] == "ready_for_backtest_review"
    assert handoff["candidate_count"] == 1
    assert handoff["candidates"] == []
    assert handoff["candidate_id"] == candidate["candidate_id"]
    assert handoff["current_rule_version"] == "stg-vegas-tunnel:v1"
    assert handoff["guardrails"]["backtest_executed"] is False
    assert analytics_manifest["optimization_performed"] is False
    assert health_manifest["optimization_performed"] is False
    assert handoff_manifest["optimization_performed"] is False
    health_bytes = (investigation / "exports/operational/health/current.json").read_bytes()
    handoff_bytes = (investigation / "exports/operational/handoff/current.json").read_bytes()
    assert _refresh_operational_outputs(investigation) == (analytics_manifest, health_manifest, handoff_manifest)
    assert (investigation / "exports/operational/health/current.json").read_bytes() == health_bytes
    assert (investigation / "exports/operational/handoff/current.json").read_bytes() == handoff_bytes


def test_strategy_health_negative_e2es_fail_closed_for_small_sample_and_execution_only(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    small = tmp_path / "small"
    _copy_operational_inputs(source, small)
    small_store = LifecycleStore(small)
    for index in range(1, 3):
        _complete_operational_trade(small_store, index=index, realized_r=-1.0)
    _refresh_operational_outputs(small)
    small_health = json.loads((small / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    small_vegas = next(item for item in small_health["strategies"] if item["strategy_id"] == "stg-vegas-tunnel")
    assert small_vegas["health_state"] == "insufficient_evidence"
    assert small_vegas["candidate_improvement_id"] is None
    assert small_vegas["candidate_improvement_ids"] == []
    assert small_vegas["improvement_candidates"] == []
    assert json.loads((small / "exports/operational/handoff/current.json").read_text(encoding="utf-8"))["status"] == "no_eligible_candidate"

    execution = tmp_path / "execution"
    _copy_operational_inputs(source, execution)
    execution_store = LifecycleStore(execution)
    for index in range(1, 6):
        _complete_operational_trade(execution_store, index=index, realized_r=-1.0, diagnostic_class="EXECUTION", rule_compliance="violation")
    _refresh_operational_outputs(execution)
    execution_health = json.loads((execution / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    execution_vegas = next(item for item in execution_health["strategies"] if item["strategy_id"] == "stg-vegas-tunnel")
    assert execution_vegas["health_state"] == "investigate"
    assert execution_vegas["diagnostic_classes"] == ["EXECUTION"]
    assert execution_vegas["operational_observability"]["operational_method_eligible_n"] == 0
    assert execution_vegas["candidate_improvement_id"] is None
    assert json.loads((execution / "exports/operational/handoff/current.json").read_text(encoding="utf-8"))["status"] == "no_eligible_candidate"


def test_triple_tap_historical_plus_operational_sample_stays_insufficient(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    triple_tap_tags = ["ict_liquidity", "triple_tap", "liquidity_touch_3x", "rsi_divergence", "mss"]
    for index in range(1, 6):
        _complete_operational_trade(
            store,
            index=index,
            realized_r=-1.0,
            strategy_id="stg-triple-tap",
            rule_version="stg-triple-tap:v1",
            setup_tags=triple_tap_tags,
            variant="ob-mss",
            target_price=110.0,
        )
    _refresh_operational_outputs(tmp_path)
    health = json.loads((tmp_path / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    triple = next(item for item in health["strategies"] if item["strategy_id"] == "stg-triple-tap")
    assert triple["sample_n"] == 7
    assert triple["confidence"] == "insufficient"
    assert triple["health_state"] == "insufficient_evidence"
    assert triple["operational_method_eligible_n"] == 5
    assert triple["method_failure_patterns"]
    assert triple["candidate_improvement_id"] is None
    assert triple["improvement_candidate"] is None
    assert triple["candidate_improvement_ids"] == []
    assert triple["improvement_candidates"] == []
    assert not any(candidate["strategy_id"] == "stg-triple-tap" for candidate in health["eligible_improvement_candidates"])
    handoff = json.loads((tmp_path / "exports/operational/handoff/current.json").read_text(encoding="utf-8"))
    assert handoff["status"] == "no_eligible_candidate"
    assert handoff["candidate_count"] == 0
    assert handoff["candidate"] is None


def test_multiple_method_patterns_require_review_without_arbitrary_selection(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    for index in range(1, 6):
        _complete_operational_trade(
            store,
            index=index,
            realized_r=-1.0,
            setup_tags=["vegas_tunnel", "component_a"],
        )
    _refresh_operational_outputs(tmp_path)
    health = json.loads((tmp_path / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    vegas = next(item for item in health["strategies"] if item["strategy_id"] == "stg-vegas-tunnel")
    assert vegas["health_state"] == "investigate"
    assert vegas["candidate_improvement_id"] is None
    assert vegas["improvement_candidate"] is None
    assert len(vegas["candidate_improvement_ids"]) == 2
    assert len(vegas["improvement_candidates"]) == 2
    assert vegas["candidate_selection_state"] == "multiple_candidates_require_review"
    assert len(health["eligible_improvement_candidates"]) == 2
    assert {candidate["affected_rule_component"] for candidate in vegas["improvement_candidates"]} == {
        "setup_tags:component_a",
        "setup_tags:vegas_tunnel",
    }
    handoff = json.loads((tmp_path / "exports/operational/handoff/current.json").read_text(encoding="utf-8"))
    assert handoff["status"] == "multiple_candidates_require_review"
    assert handoff["candidate"] is None
    assert handoff["candidate_id"] is None
    assert handoff["candidate_count"] == 2
    assert len(handoff["candidates"]) == 2
    assert "chosen_candidate_id" not in handoff
    assert handoff["guardrails"]["backtest_executed"] is False
    assert handoff["guardrails"]["optimization_performed"] is False
    assert handoff["guardrails"]["strategy_mutation"] is False


def test_close_recorded_without_verified_exit_is_not_countable(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, draft_id, trade_id = _make_open_trade(source, tmp_path)
    _attach_exit_evidence(store, draft_id)
    first = store.final_close(trade_id, exit_price=110, closed_at="2026-09-11T01:04:00Z", realized_r=10.0, provider_pnl=10.0, source_ref="fixture://verified-exit")
    second = store.final_close(trade_id, exit_price=110, closed_at="2026-09-11T01:04:00Z", realized_r=10.0, provider_pnl=10.0, source_ref="fixture://verified-exit")
    assert first["event_id"] == second["event_id"]
    analytics = refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    payload = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in payload["canonical_read_model"] if item["trade_id"] == trade_id)
    assert payload["counts"]["countable_observation_count"] == 133
    assert row["counted"] is False
    assert row["exclusion_reason"] == "exit_unverified"
    assert row["realized_r"] is None
    assert row["provider_pnl_verified"] is False
    assert analytics["optimization_performed"] is False


def test_verified_exit_controls_r_and_provider_pnl_separately(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, draft_id, trade_id = _make_open_trade(source, tmp_path)
    _attach_exit_evidence(store, draft_id, "fixture://r-only-exit")
    store.final_close(trade_id, exit_price=110, closed_at="2026-09-11T01:04:00Z", realized_r=2.0, provider_pnl=10.0, source_ref="fixture://r-only-exit")
    store.verify_exit(trade_id, authority_class="governed_manual_adjudication", evidence_refs=["fixture://r-only-exit"], realized_r=2.0)
    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    payload = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in payload["canonical_read_model"] if item["trade_id"] == trade_id)
    assert row["counted"] is True
    assert row["realized_r"] == 2.0
    assert row["provider_pnl"] == 10.0
    assert row["provider_pnl_verified"] is False
    assert payload["metrics"]["global"]["verified_provider_pnl"] is None
    assert payload["counts"]["countable_observation_count"] == 134


def test_unknown_review_compliance_derives_null(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, draft_id, trade_id = _make_open_trade(source, tmp_path)
    _attach_exit_evidence(store, draft_id, "fixture://unknown-exit")
    store.final_close(trade_id, exit_price=100, closed_at="2026-09-11T01:04:00Z", realized_r=0.0, source_ref="fixture://unknown-exit")
    store.verify_exit(trade_id, authority_class="governed_manual_adjudication", evidence_refs=["fixture://unknown-exit"], realized_r=0.0)
    owner_confirmed_review(store, trade_id, {"rule_compliance": "unknown", "diagnostic_class": "INSUFFICIENT"}, reviewed_at="2026-09-11T01:06:00Z")
    record = store.current()["records"][trade_id]
    assert record["review"]["rule_compliance"] == "unknown"
    assert record["rule_violation"] is None


def test_execution_review_is_not_reclassified_as_method(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, draft_id, trade_id = _make_open_trade(source, tmp_path)
    _attach_exit_evidence(store, draft_id, "fixture://execution-exit")
    store.final_close(trade_id, exit_price=110, closed_at="2026-09-11T01:04:00Z", realized_r=2.0, source_ref="fixture://execution-exit")
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=["fixture://execution-exit"], realized_r=2.0)
    owner_confirmed_review(store, trade_id, {"what_went_well": "none", "what_went_wrong": "fill deviation", "rule_compliance": "violation", "execution_deviation": "observed", "thesis_validity": "unknown", "entry_quality": "unknown", "exit_quality": "observed", "evidence_completeness": "complete", "owner_notes": "execution-only fixture", "review_status": "reviewed", "diagnostic_class": "EXECUTION"}, reviewed_at="2026-09-11T01:06:00Z")
    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    refresh_health(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    health = json.loads((tmp_path / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    vegas = next(item for item in health["strategies"] if item["strategy_id"] == "stg-vegas-tunnel")
    assert vegas["execution_quality"]["rule_violation_rate"] == 1.0
    assert vegas["diagnostic_classes"] == ["EXECUTION"]
    assert vegas["diagnostic_separation"]["method"] == "not_observable"


def test_handoff_parser_rejects_incomplete_candidate_without_mutating_health(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _make_fixture(source, tmp_path)
    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    refresh_health(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    item = {
        "health_state": "investigate", "candidate_improvement_id": "cand-vegas-v1", "strategy_id": "stg-vegas-tunnel",
        "sample_n": 10, "confidence": "low", "diagnostic_classes": ["METHOD"],
        "operational_observability": {"status": "machine_observable"}, "operational_reviewed_n": 5,
        "operational_method_eligible_n": 5,
        "improvement_candidate": {
        "candidate_id": "cand-vegas-v1", "strategy_id": "stg-vegas-tunnel", "current_rule_version": "stg-vegas-tunnel:v1",
        "evidence_refs": ["exports/operational/analytics/current.json"], "observed_failure_mode": "execution sample fixture",
        "research_hypothesis": "future candidate only", "proposed_rule_delta": "must be tested separately",
        "success_metrics": ["predeclared metric"], "failure_metrics": ["predeclared failure"], "minimum_test_sample": 5,
        "holdout_requirement": "required", "frozen_controls": ["B3 unchanged"],
        "evidence_window": {"start_event_date": "2026-09-11", "end_event_date": "2026-09-11"}, "sample_n": 10, "method_eligible_n": 5,
        "affected_rule_component": "strategy.setup_tags:vegas_tunnel", "research_question": "future test?", "limitations": ["future only"],
        "predeclared_gate": {"passed": True, "evidence_required": True, "observability": "machine_observable", "method_eligible_n": 5, "sample_n": 10, "confidence": "low", "checks": {"minimum_total_sample": True, "confidence_not_insufficient": True}},
        },
    }
    assert _eligible_candidate(item, analytics) is not None
    item["improvement_candidate"].pop("failure_metrics")
    assert _eligible_candidate(item, analytics) is None


def test_chronology_orders_known_times_before_missing_and_invalid() -> None:
    rows = [
        {"event_date": "2026-09-11", "time_entry": "13:00", "source_path": "z", "observation_id": "two"},
        {"event_date": "2026-09-11", "time_entry": "", "source_path": "a", "observation_id": "three"},
        {"event_date": "2026-09-11", "time_entry": "09:00", "source_path": "z", "observation_id": "one"},
        {"event_date": "2026-09-11", "time_entry": "99:99", "source_path": "b", "observation_id": "four"},
    ]
    ordered = sorted(rows, key=chronology_key)
    assert [row["observation_id"] for row in ordered] == ["one", "two", "three", "four"]


def test_illegal_lifecycle_transitions_are_rejected(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, draft_id, _ = _make_pending(source, tmp_path / "pending")
    with pytest.raises(LifecycleError, match="entry-verified open lifecycle"):
        store.finalize(draft_id)
    with pytest.raises(LifecycleError, match="open canonical trade"):
        store.partial_close("trd_00000000-0000-7000-8000-000000000000", exit_price=1, closed_at=FIXED_GENERATED_AT, quantity_fraction=0.5)
    with pytest.raises(LifecycleError, match="closed canonical trade"):
        store.review("trd_00000000-0000-7000-8000-000000000000", {"rule_compliance": "unknown"}, reviewed_at=FIXED_GENERATED_AT)
    open_store, _, trade_id = _make_open_trade(source, tmp_path / "open")
    with pytest.raises(LifecycleError, match="closed canonical trade"):
        open_store.review(trade_id, {"rule_compliance": "unknown"}, reviewed_at=FIXED_GENERATED_AT)
    late_fact = open_store.append(
        "partial_close",
        {"trade_id": trade_id, "close": {"exit_price": 101, "closed_at": "2026-09-11T00:00:00Z", "quantity_fraction": 0.5}},
        idempotency_key="late-fact-close",
        occurred_at="2026-09-11T00:00:00Z",
    )
    assert late_fact["occurred_at"] == "2026-09-11T00:00:00Z"
    assert late_fact["recorded_at"]
    with pytest.raises(LifecycleError, match="recorded_at must not move backwards"):
        open_store.append(
            "partial_close",
            {"trade_id": trade_id, "close": {"exit_price": 102, "closed_at": "2026-09-11T00:01:00Z", "quantity_fraction": 0.25}},
            idempotency_key="recorded-time-regression",
            occurred_at="2026-09-11T00:01:00Z",
            recorded_at="2026-09-10T00:00:00Z",
        )


def test_synthetic_by_sample_origin_and_summary_by_mode_are_additive_over_metrics() -> None:
    """Pure unit test against the metrics engine -- no lifecycle/B1 fixture.

    LIVE=2, BACKTEST=3: by_sample_origin splits exactly, summary_by_mode.all
    combines all 5 and is explicitly flagged combined_descriptive so it can
    never be misread as one coherent population.
    """

    rows = [
        {"counted": True, "realized_r": 1.0, "sample_origin": "live", "event_date": "2026-01-01"},
        {"counted": True, "realized_r": -1.0, "sample_origin": "live", "event_date": "2026-01-02"},
        {"counted": True, "realized_r": 2.0, "sample_origin": "backtest", "source_method": "manual_backtest", "event_date": "2026-01-03"},
        {"counted": True, "realized_r": -0.5, "sample_origin": "backtest", "source_method": "manual_backtest", "event_date": "2026-01-04"},
        {"counted": True, "realized_r": 0.5, "sample_origin": "backtest", "source_method": "paper_trade", "event_date": "2026-01-05"},
    ]
    chronology = _chronology_quality(rows)
    by_sample_origin = _group_metrics(rows, "sample_origin", chronology)
    assert by_sample_origin["live"]["count"] == 2
    assert by_sample_origin["backtest"]["count"] == 3

    summary = _summary_by_mode(rows, chronology)
    assert summary["live"]["count"] == 2
    assert summary["backtest"]["count"] == 3
    assert summary["all"]["count"] == 5
    assert summary["all"]["combined_descriptive"] is True
    # live/backtest blocks are plain metrics blocks, never carrying the flag.
    assert "combined_descriptive" not in summary["live"]
    assert "combined_descriptive" not in summary["backtest"]

    by_source_method = _group_metrics([row for row in rows if row["sample_origin"] == "backtest"], "source_method", chronology)
    assert sorted(by_source_method) == ["manual_backtest", "paper_trade"]
    assert by_source_method["manual_backtest"]["count"] == 2
    assert by_source_method["paper_trade"]["count"] == 1


def test_by_rule_version_breakdown_stays_separate_not_silently_merged() -> None:
    rows = [
        {"counted": True, "realized_r": 1.0, "sample_origin": "backtest", "rule_version": "stg-x:v0.1", "event_date": "2026-01-01"},
        {"counted": True, "realized_r": -1.0, "sample_origin": "backtest", "rule_version": "stg-x:v0.1", "event_date": "2026-01-02"},
        {"counted": True, "realized_r": 0.5, "sample_origin": "backtest", "rule_version": "stg-x:v0.1", "event_date": "2026-01-03"},
        {"counted": True, "realized_r": 2.0, "sample_origin": "backtest", "rule_version": "stg-x:v0.2", "event_date": "2026-01-04"},
        {"counted": True, "realized_r": -0.5, "sample_origin": "backtest", "rule_version": "stg-x:v0.2", "event_date": "2026-01-05"},
    ]
    chronology = _chronology_quality(rows)
    by_rule_version = _group_metrics(rows, "rule_version", chronology)
    assert by_rule_version["stg-x:v0.1"]["count"] == 3
    assert by_rule_version["stg-x:v0.2"]["count"] == 2
    # Two rule versions must never collapse into a single merged bucket.
    assert set(by_rule_version) == {"stg-x:v0.1", "stg-x:v0.2"}


def test_analytics_e2e_exposes_sample_origin_source_method_and_recording_timing(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, draft_id, trade_id = _make_fixture(source, tmp_path)

    # Attach a real durable evidence artifact (via evidence_store.EvidenceStore)
    # as a post-close review attachment so the analytics row carries an
    # evidence_artifacts entry with a store-backed locator.
    evidence_store = EvidenceStore(tmp_path)
    artifact_source = tmp_path / "review-chart.png"
    artifact_source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    ingested = evidence_store.ingest(artifact_source)
    store.attach_evidence(
        draft_id,
        {**ingested, "source_ref": "fixture://review-artifact", "timing": "post_trade", "type": "chart_snapshot", "category": "review", "sha256": ingested["sha256"]},
    )

    analytics = refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    payload = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))

    assert "by_sample_origin" in payload["metrics"]
    assert "by_source_method" in payload["metrics"]
    global_count = payload["metrics"]["global"]["count"]
    live_count = payload["metrics"]["by_sample_origin"]["live"]["count"]
    backtest_count = payload["metrics"]["by_sample_origin"]["backtest"]["count"]
    assert live_count + backtest_count == global_count

    summary = payload["summary_by_mode"]
    assert summary["live"]["count"] == live_count
    assert summary["backtest"]["count"] == backtest_count
    assert summary["all"]["count"] == global_count
    assert summary["all"]["combined_descriptive"] is True

    new_row = next(row for row in payload["observations"] if row["trade_id"] == trade_id)
    assert new_row["sample_origin"] == "live"
    assert new_row["recording_timing"] == "contemporaneous"
    # source_method is backtest-only by the closed vocabulary; a live row
    # must stay explicitly None rather than inventing a value.
    assert new_row["source_method"] is None
    artifact_entries = [item for item in new_row["evidence_artifacts"] if item["category"] == "review"]
    assert len(artifact_entries) == 1
    assert artifact_entries[0]["locator"] == ingested["locator"]
    assert artifact_entries[0]["media_type"] == "image/png"

    # Historical B1 v3 seed rows predate these vocabularies: explicit None,
    # never backfilled or guessed.
    seed_row = next(row for row in payload["observations"] if row["source_kind"] == "b1_v3_historical_seed")
    assert seed_row["source_method"] is None
    assert seed_row["recording_timing"] is None
    assert seed_row["evidence_artifacts"] == []
    assert analytics["counts"] == payload["counts"]


def test_trade_view_exposes_mode_summaries_and_evidence_link(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    store, draft_id, trade_id = _make_fixture(source, tmp_path)

    evidence_store = EvidenceStore(tmp_path)
    artifact_source = tmp_path / "review-chart.png"
    artifact_source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 40)
    ingested = evidence_store.ingest(artifact_source)
    store.attach_evidence(
        draft_id,
        {**ingested, "source_ref": "fixture://review-artifact-2", "timing": "post_trade", "type": "chart_snapshot", "category": "review", "sha256": ingested["sha256"]},
    )

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    refresh_health(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    build_trade_view(tmp_path)

    view = json.loads((tmp_path / "exports/operational/trade-view/trade-view-data.json").read_text(encoding="utf-8"))
    assert set(view["summary_by_mode"]) == {"live", "paper", "backtest", "all"}
    assert view["summary_by_mode"]["all"]["combined_descriptive"] is True

    # Trade View V2 renders the LIVE/BACKTEST/ALL segmented control and its
    # combined-descriptive warning from app.js (see trade_view.py APP_JS
    # `renderModeSelector`/`renderKpis`), not from static markup in
    # index.html -- assert the equivalent structural marker there instead.
    app_js = (tmp_path / "exports/operational/trade-view/app.js").read_text(encoding="utf-8")
    assert 'id="mode-selector"' in app_js
    assert "combined-warning" in app_js

    row = next(r for r in view["rows"] if r["reference"] == trade_id)
    assert row["evidence_links"], "expected at least one durable evidence link on the reviewed row"
    artifact_name = ingested["locator"].split(":", 1)[1]
    artifact_path = tmp_path / "canonical" / "evidence" / "artifacts" / artifact_name
    # Evidence links are served through the safe /evidence/<sha256><ext>
    # route (see trade_view.make_trade_view_handler), not a relative
    # filesystem path -- a relative path cannot be traversed by the local
    # HTTP server, which is rooted at the trade-view output directory and
    # has no access to canonical/evidence/artifacts/ above it.
    link = next(item for item in row["evidence_links"] if item["href"] == f"/evidence/{artifact_name}")
    assert link["href"] == f"/evidence/{artifact_name}"
    assert artifact_path.exists()

    # strategy_health source-aware descriptive fields, unrelated to
    # candidate/promotion gates.
    health = json.loads((tmp_path / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    assert "sample_origin_summary" in health
    assert health["sample_origin_summary"]["combined_descriptive"] is True
    vegas = next(item for item in health["strategies"] if item["strategy_id"] == "stg-vegas-tunnel")
    assert vegas["live_n"] + vegas["backtest_n"] == vegas["combined_n"]
    assert vegas["live_n"] >= 1
