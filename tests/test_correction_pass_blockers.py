"""Correction-pass tests for Blockers 1, 2, 3, and 6 of PR #43's review.

Covers:
  * Blocker 1 -- BACKTEST has its own type-aware countability gate in
    operational_analytics._canonical_observations (registered strategy +
    real registered rule_version + governed backtest entry + closed +
    exit-verified + realized R known), separate from the unchanged LIVE gate.
    Both active and experimental registered strategies are countable for
    descriptive analytics, without ever becoming an improvement-candidate or
    mutating registry status (strategy_health.py / backtest_handoff.py).
  * Blocker 2 -- confirm_backtest_entry's run_id/parameters_ref/dataset_ref/
    data_period are optional; omitting all four uses the new
    manual_declared_execution basis instead of simulator_run.
  * Blocker 3 -- retrospective LIVE capture may leave specific pre-trade plan
    fields as the explicit "unknown"/"not_observable" sentinel; contemporaneous
    capture still requires real values.
  * Blocker 6 -- Unified Capture's capture_backtest fails closed on
    rule_version="unfrozen:experimental" even though the lower-level
    LifecycleStore.create_draft still accepts it for a non-Unified-Capture
    caller (see test_backtest_capture_fields.py).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from resolver_helpers import matching_observations, no_match_observations, write_observations
from trading_os.evidence_store import EvidenceStore
from trading_os.operational_analytics import refresh_analytics
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.strategy_health import refresh_health
from trading_os.unified_capture import capture_backtest, capture_retrospective_live
from trading_os.vocabularies import UNFROZEN_EXPERIMENTAL_PLACEHOLDER


FIXED_GENERATED_AT = "2026-09-12T00:00:00Z"
FIXED_SOURCE_COMMIT = "b" * 40


def _copy_operational_inputs(source: Path, destination: Path) -> None:
    for relative in ("registry", "exports/method-validation", "exports/trader-dna/v1"):
        shutil.copytree(source / relative, destination / relative)
    (destination / "canonical" / "adjudications").mkdir(parents=True)
    (destination / "canonical" / "sequences").mkdir(parents=True)
    (destination / "canonical" / "records" / "trades").mkdir(parents=True)
    (destination / "canonical" / "sequences" / "numbers.json").write_text(
        json.dumps({"live_next": 1, "backtest_next": 1, "allocated": [], "tombstones": []}) + "\n",
        encoding="utf-8",
    )
    (destination / "canonical" / "sequences" / "finalizations.json").write_text(
        json.dumps({"schema_version": "finalization-index.v1", "records": {}}) + "\n",
        encoding="utf-8",
    )


def _add_experimental_registered_strategy(destination: Path) -> None:
    """Register a real (non-placeholder) rule_version for an experimental strategy."""

    registry_path = destination / "registry" / "strategy-registry.yaml"
    payload = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    payload["strategies"].append(
        {
            "strategy_id": "stg-experimental-fixture",
            "display_name": "Experimental Fixture",
            "classification": "experimental",
            "status": "experimental",
            "rule_versions": ["stg-experimental-fixture:v1"],
        }
    )
    registry_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    version_path = destination / "registry" / "versions" / "stg-experimental-fixture-v1.yaml"
    version_path.write_text(
        yaml.safe_dump(
            {
                "version_id": "stg-experimental-fixture:v1",
                "strategy_id": "stg-experimental-fixture",
                "status": "candidate",
                "rules": [
                    {
                        "rule_id": "r1",
                        "category": "setup",
                        "operator": "exists",
                        "operands": {"field": "strategy.thesis"},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _ingest_entry_evidence(root: Path, *, label: str, captured_at: str) -> dict:
    fixture_dir = root / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    path = fixture_dir / f"{label}.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + label.encode("utf-8") * 4)
    ingested = EvidenceStore(root).ingest(path)
    return {
        "source_ref": f"evidence://{ingested['sha256']}/{label}",
        "timing": "execution",
        "type": "chart_snapshot",
        "category": "entry",
        "sha256": ingested["sha256"],
        "locator": ingested["locator"],
        "media_type": ingested["media_type"],
        "byte_size": ingested["byte_size"],
        "original_filename": ingested["original_filename"],
        "captured_at": captured_at,
    }


def _ingest_exit_evidence(root: Path, *, label: str, captured_at: str) -> dict:
    """Durable, hash-verified, strong-typed EXIT evidence (correction pass #2,
    Blocker 2: a bare manual_note/owner_attestation claim no longer counts as
    strict-countable -- see _backtest_exit_evidence_is_strict)."""

    fixture_dir = root / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    path = fixture_dir / f"{label}.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + label.encode("utf-8") * 4)
    ingested = EvidenceStore(root).ingest(path)
    return {
        "source_ref": f"evidence://{ingested['sha256']}/{label}",
        "timing": "post_trade",
        "type": "chart_snapshot",
        "category": "exit",
        "sha256": ingested["sha256"],
        "locator": ingested["locator"],
        "media_type": ingested["media_type"],
        "byte_size": ingested["byte_size"],
        "original_filename": ingested["original_filename"],
        "captured_at": captured_at,
    }


def _backtest_kwargs(**overrides) -> dict:
    """Keyword arguments for unified_capture.capture_backtest."""

    base = dict(
        strategy_id="stg-vegas-tunnel",
        rule_version="stg-vegas-tunnel:v1",
        symbol="BTCUSDT",
        direction="long",
        intended_entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        planned_rr=2.0,
        timeframe_context="1h",
        timeframe_trigger="15m",
        market_context="missed a live setup, scoring it as a backtest",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis="setup formed, not taken live",
        chart_refs=["fixture://chart/pretrade"],
        created_at="2026-09-12T00:10:00Z",
        source_method="live_market_unexecuted",
        actual_entry=100.0,
        execution_at="2026-09-12T00:10:30Z",
    )
    base.update(overrides)
    return base


def _draft_kwargs(**overrides) -> dict:
    """Keyword arguments for LifecycleStore.create_draft directly."""

    base = dict(
        strategy_id="stg-vegas-tunnel",
        rule_version="stg-vegas-tunnel:v1",
        symbol="BTCUSDT",
        direction="long",
        intended_entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        planned_rr=2.0,
        timeframe_context="1h",
        timeframe_trigger="15m",
        market_context="missed a live setup, scoring it as a backtest",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis="setup formed, not taken live",
        chart_refs=["fixture://chart/pretrade"],
        created_at="2026-09-12T00:10:00Z",
        trade_type="backtest",
        source_method="live_market_unexecuted",
    )
    base.update(overrides)
    return base


def test_true_e2e_backtest_counts_in_analytics(tmp_path: Path) -> None:
    """Blocker 1: a governed single-sample BACKTEST reaches OPERATIONAL analytics as counted."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="e2e-entry", captured_at="2026-09-12T00:10:30Z")

    result = capture_backtest(
        store,
        **_backtest_kwargs(entry_evidence=entry_evidence),
    )
    draft_id, trade_id = result["draft_id"], result["trade_id"]

    exit_evidence = _ingest_exit_evidence(tmp_path, label="e2e-exit", captured_at="2026-09-12T01:10:00Z")
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-12T01:10:00Z", realized_r=2.0, source_ref=exit_evidence["source_ref"])
    store.attach_evidence(draft_id, exit_evidence)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[exit_evidence["source_ref"]], realized_r=2.0)

    before = refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    live_before = analytics["summary_by_mode"]["live"]["count"]

    row = next(item for item in analytics["observations"] if item.get("trade_id") == trade_id)
    assert row["counted"] is True
    assert row["source_kind"] == "post_cutover_operational"
    assert row["exclusion_reason"] is None
    assert row["provider_pnl"] is None
    assert row["provider_pnl_verified"] is False
    assert row["identity_verification"]["match_disposition"] is None

    assert analytics["summary_by_mode"]["backtest"]["count"] >= 1
    assert trade_id in {r["trade_id"] for r in analytics["observations"] if r.get("sample_origin") == "backtest" and r.get("counted")}
    assert analytics["summary_by_mode"]["live"]["count"] == live_before
    assert analytics["metrics"]["by_strategy"]["stg-vegas-tunnel"]["backtest_count"] >= 1


def test_experimental_registered_strategy_backtest_is_descriptive_only(tmp_path: Path) -> None:
    """Blocker 1 section C: experimental (not just active) registered strategies are
    countable for descriptive BACKTEST/ALL analytics, but never become an
    improvement candidate and never mutate the strategy registry status."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    _add_experimental_registered_strategy(tmp_path)
    registry_before = (tmp_path / "registry" / "strategy-registry.yaml").read_text(encoding="utf-8")
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="experimental-entry", captured_at="2026-09-12T00:20:30Z")

    result = capture_backtest(
        store,
        **_backtest_kwargs(
            strategy_id="stg-experimental-fixture",
            rule_version="stg-experimental-fixture:v1",
            created_at="2026-09-12T00:20:00Z",
            execution_at="2026-09-12T00:20:30Z",
            allow_experimental=True,
            entry_evidence=entry_evidence,
        ),
    )
    draft_id, trade_id = result["draft_id"], result["trade_id"]
    exit_evidence = _ingest_exit_evidence(tmp_path, label="experimental-exit", captured_at="2026-09-12T01:20:00Z")
    store.final_close(trade_id, exit_price=95.0, closed_at="2026-09-12T01:20:00Z", realized_r=-1.0, source_ref=exit_evidence["source_ref"])
    store.attach_evidence(draft_id, exit_evidence)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[exit_evidence["source_ref"]], realized_r=-1.0)

    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in analytics["observations"] if item.get("trade_id") == trade_id)
    assert row["counted"] is True
    assert row["strategy_id"] == "stg-experimental-fixture"
    assert analytics["metrics"]["by_strategy"]["stg-experimental-fixture"]["backtest_count"] == 1
    assert analytics["metrics"]["by_sample_origin"]["backtest"]["count"] >= 1

    refresh_health(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    health = json.loads((tmp_path / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    experimental_health = next((item for item in health["strategies"] if item["strategy_id"] == "stg-experimental-fixture"), None)
    assert experimental_health is not None
    assert experimental_health["candidate_improvement_id"] is None
    assert experimental_health["improvement_candidate"] is None
    assert experimental_health["candidate_improvement_ids"] == []

    assert (tmp_path / "registry" / "strategy-registry.yaml").read_text(encoding="utf-8") == registry_before
    updated_registry = yaml.safe_load((tmp_path / "registry" / "strategy-registry.yaml").read_text(encoding="utf-8"))
    experimental_entry = next(item for item in updated_registry["strategies"] if item["strategy_id"] == "stg-experimental-fixture")
    assert experimental_entry["status"] == "experimental"


def test_single_backtest_without_run_dataset_period_uses_manual_declared_execution(tmp_path: Path) -> None:
    """Blocker 2: no run_id/parameters_ref/dataset_ref/data_period required, and
    nothing is silently invented for them."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="manual-entry", captured_at="2026-09-12T00:30:30Z")

    result = capture_backtest(
        store,
        **_backtest_kwargs(created_at="2026-09-12T00:30:00Z", execution_at="2026-09-12T00:30:30Z", entry_evidence=entry_evidence),
    )
    trade_id = result["trade_id"]
    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    entry_verification = trade["actual"]["execution_verification"]["entry"]
    assert entry_verification["basis"] == "manual_declared_execution"
    assert entry_verification["state"] == "confirmed"
    execution_evidence = trade["actual"]["execution_evidence"]["entry"]["manual_declared_execution"]
    assert "run_id" not in execution_evidence
    assert "parameters_ref" not in execution_evidence
    assert "dataset_ref" not in execution_evidence
    assert "data_period" not in execution_evidence
    assert trade["dedupe"].get("run_id") is None


def test_confirm_backtest_entry_requires_all_or_none_of_run_metadata(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="partial-entry", captured_at="2026-09-12T00:40:30Z")
    draft = store.create_draft(**_draft_kwargs())
    store.attach_evidence(draft["draft_id"], dict(entry_evidence))
    with pytest.raises(LifecycleError, match="together or not at all"):
        store.confirm_backtest_entry(
            draft["draft_id"],
            actual_entry=100.0,
            execution_at="2026-09-12T00:40:30Z",
            source_ref=entry_evidence["source_ref"],
            run_id="run-1",
        )


def test_retrospective_live_with_missing_plan_fields(tmp_path: Path) -> None:
    """Blocker 3: a real, already-closed trade the owner can't fully recall the
    pre-trade plan for -- specific plan fields stay the explicit sentinel,
    never fabricated from actual/exit facts."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = {
        "source_ref": "fixture://late-propw-fill",
        "timing": "execution",
        "type": "broker_fill",
        "category": "entry",
        "sha256": "a" * 64,
        "captured_at": "2026-06-01T14:00:00Z",
    }
    result = capture_retrospective_live(
        store,
        strategy_observations=matching_observations("stg-vegas-tunnel"),
        symbol="BTCUSDT",
        direction="long",
        intended_entry=100.0,
        stop_loss=95.0,
        take_profit="not_observable",
        planned_rr="unknown",
        timeframe_context="1h",
        timeframe_trigger="15m",
        market_context="late-reported real trade",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis="not_observable",
        chart_refs=["fixture://chart"],
        created_at="2026-06-01T13:55:00Z",
        provider_evidence=entry_evidence,
        provider="propw",
        provider_lifecycle_id="late-open-1",
        provider_execution_timestamp="2026-06-01T14:00:00Z",
        candidate_source="fixture://late-candidate-match",
        actual_entry=100.0,
        recorded_at="2026-09-12T09:00:00Z",
    )
    draft_id, trade_id = result["draft_id"], result["trade_id"]
    assert trade_id.startswith("trd_")
    assert result["trade_no"].startswith("T")

    store.final_close(trade_id, exit_price=115.0, closed_at="2026-06-01T16:00:00Z", realized_r=3.0, source_ref="fixture://late-final-exit", recorded_at="2026-09-12T09:00:00Z")
    store.attach_evidence(
        draft_id,
        {"source_ref": "fixture://late-final-exit", "timing": "post_trade", "type": "broker_fill", "category": "exit", "sha256": "b" * 64, "captured_at": "2026-06-01T16:00:00Z"},
        recorded_at="2026-09-12T09:00:00Z",
    )
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=["fixture://late-final-exit"], realized_r=3.0, recorded_at="2026-09-12T09:00:00Z")

    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    assert trade["recording_timing"] == "retrospective"
    assert trade["plan"]["take_profit_price"] == "not_observable"
    assert trade["plan"]["rr_pretrade"] == "unknown"
    assert trade["plan"]["thesis"] == "not_observable"
    # Never silently backfilled from the real exit price/realized R.
    assert trade["plan"]["take_profit_price"] != 115.0
    assert trade["actual"]["entry_price"] == 100.0

    projection = store.current()
    record = projection["records"][trade_id]
    assert record["state"] == "CLOSED"
    identity_verified = record["identity_status"] == "MATCHED"
    entry_confirmed = record["execution_verification"]["entry"]["state"] == "confirmed"
    exit_confirmed = record["exit_verification"]["state"] == "confirmed"
    assert identity_verified and entry_confirmed and exit_confirmed and record["realized_r"] == 3.0


def test_contemporaneous_plan_fields_still_required(tmp_path: Path) -> None:
    """Regression: recording_timing=contemporaneous must not silently accept
    the retrospective-only unknown/not_observable sentinel."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    with pytest.raises(LifecycleError, match="recording_timing=retrospective"):
        store.create_draft(**_draft_kwargs(recording_timing="contemporaneous", take_profit="not_observable"))
    with pytest.raises(LifecycleError, match="recording_timing=retrospective"):
        store.create_draft(**_draft_kwargs(recording_timing="contemporaneous", thesis="unknown"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"confirmation_tags": ["near_tp_reversal"]},
        {"thesis": "support breakdown -> short; manual exit after reversal"},
        {"market_context": "bullish break followed the entry"},
    ],
)
def test_retrospective_plan_rejects_review_only_semantics(tmp_path: Path, overrides: dict) -> None:
    """Review/outcome conclusions must not be durable pre-trade plan facts."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    with pytest.raises(LifecycleError, match="post-trade review marker"):
        store.create_draft(**_draft_kwargs(recording_timing="retrospective", **overrides))
    assert not list((tmp_path / "canonical" / "drafts").glob("*.json"))


def test_unified_capture_rejects_unfrozen_experimental_placeholder(tmp_path: Path) -> None:
    """Blocker 6: capture_backtest fails closed on the placeholder rule_version."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    entry_evidence = _ingest_entry_evidence(tmp_path, label="placeholder-entry", captured_at="2026-09-12T00:50:30Z")
    with pytest.raises(LifecycleError, match=UNFROZEN_EXPERIMENTAL_PLACEHOLDER):
        capture_backtest(
            store,
            **_backtest_kwargs(rule_version=UNFROZEN_EXPERIMENTAL_PLACEHOLDER, entry_evidence=entry_evidence),
        )
