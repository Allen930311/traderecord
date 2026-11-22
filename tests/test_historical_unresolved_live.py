"""Historical LIVE executions may remain strategy-unresolved without losing owner-performance truth."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from resolver_helpers import matching_observations, no_match_observations, write_observations
from trading_os.operational_analytics import (
    _active_rule_pins,
    _canonical_observations,
    _group_metrics,
    _in_population,
    _load_b1,
    _summary_by_mode,
    refresh_analytics,
)
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.unified_capture import capture_retrospective_live
from review_test_support import owner_confirmed_review


def _copy_operational_inputs(source: Path, destination: Path) -> None:
    for relative in ("registry", "exports/method-validation", "exports/trader-dna/v1"):
        shutil.copytree(source / relative, destination / relative)
    for relative in (
        "canonical/adjudications",
        "canonical/sequences",
        "canonical/records/trades",
        "canonical/records/observations",
        "canonical/records/superseded",
    ):
        (destination / relative).mkdir(parents=True)
    (destination / "canonical/sequences/numbers.json").write_text(
        json.dumps({"live_next": 1, "backtest_next": 1, "allocated": [], "tombstones": []}) + "\n",
        encoding="utf-8",
    )
    (destination / "canonical/sequences/finalizations.json").write_text(
        json.dumps({"schema_version": "finalization-index.v1", "records": {}}) + "\n",
        encoding="utf-8",
    )


def _unresolved_kwargs(**overrides):
    values = {
        "strategy_observations": no_match_observations(),
        "symbol": "AAVEUSDT",
        "direction": "long",
        "intended_entry": "unknown",
        "stop_loss": "unknown",
        "take_profit": "unknown",
        "planned_rr": "unknown",
        "timeframe_context": "unknown",
        "timeframe_trigger": "15m",
        "market_context": "unknown",
        "setup_tags": [],
        "confirmation_tags": [],
        "thesis": "unknown",
        "chart_refs": [],
        "created_at": "2025-06-04T09:49:00+08:00",
        "provider_evidence": {
            "source_ref": "fixture://propw-unresolved-entry",
            "timing": "execution",
            "type": "broker_fill",
            "category": "entry",
            "sha256": "a" * 64,
            "captured_at": "2025-06-04T09:49:00+08:00",
        },
        "provider": "propw",
        "provider_lifecycle_id": "2506040949134235",
        "provider_execution_timestamp": "2025-06-04T09:49:00+08:00",
        "candidate_source": "fixture://propw-unresolved-match",
        "actual_entry": 265.79,
        "outcome_known_at_recording": True,
    }
    values.update(overrides)
    return values


def test_retrospective_live_no_match_uses_null_identity_and_stays_out_of_named_stats(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)

    result = capture_retrospective_live(store, **_unresolved_kwargs())
    draft_id = result["draft_id"]
    trade_id = result["trade_id"]

    draft = json.loads((tmp_path / "canonical/drafts" / f"{draft_id}.json").read_text(encoding="utf-8"))
    assert draft["type"] == "live"
    assert draft["recording_timing"] == "retrospective"
    assert draft["strategy"] == {
        "strategy_id": None,
        "rule_version": None,
        "setup_tags": [],
        "confirmation_tags": [],
        "variant": None,
    }
    assert store.current()["records"][trade_id]["strategy_id"] is None
    assert store.current()["records"][trade_id]["strategy_resolution"]["status"] == "no_match"

    exit_ref = "fixture://propw-unresolved-exit"
    store.final_close(
        trade_id,
        exit_price=263.07,
        closed_at="2025-06-04T10:13:00+08:00",
        realized_r=-1.0,
        provider_pnl=-10.0,
        source_ref=exit_ref,
    )
    store.attach_evidence(
        draft_id,
        {
            "source_ref": exit_ref,
            "timing": "post_trade",
            "type": "broker_fill",
            "category": "exit",
            "sha256": "b" * 64,
            "captured_at": "2025-06-04T10:13:00+08:00",
        },
    )
    store.verify_exit(
        trade_id,
        authority_class="owner_attestation",
        evidence_refs=[exit_ref],
        realized_r=-1.0,
    )
    owner_confirmed_review(
        store,
        trade_id,
        {
            "rule_compliance": "not_observable",
            "diagnostic_class": "INSUFFICIENT",
            "entry_quality": "unknown",
            "evidence_completeness": "complete",
        },
        reviewed_at="2025-06-04T10:14:00+08:00",
    )

    refresh_analytics(tmp_path, generated_at="2026-09-22T00:00:00Z", source_commit="a" * 40)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in analytics["canonical_read_model"] if item["trade_id"] == trade_id)
    assert row["strategy_id"] is None
    assert row["rule_version"] is None
    assert row["counted"] is False
    assert row["exclusion_reason"] == "strategy_no_match"
    assert all(item["trade_id"] != trade_id for item in analytics["canonical_read_model"] if item["strategy_id"] == "stg-vegas-tunnel")


def test_unresolved_strategy_placeholder_is_retrospective_live_only(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)

    with pytest.raises(LifecycleError, match="requires a strategy_resolution"):
        store.create_draft(
            strategy_id="unknown",
            rule_version="historical-unknown",
            symbol="AAVEUSDT",
            direction="long",
            intended_entry=265.79,
            stop_loss=260.0,
            take_profit=270.0,
            planned_rr=1.0,
            timeframe_context="15m",
            timeframe_trigger="15m",
            market_context="historical live capture",
            setup_tags=[],
            confirmation_tags=[],
            thesis="historical live capture",
            chart_refs=[],
            created_at="2025-06-04T09:49:00+08:00",
            trade_type="live",
            recording_timing="contemporaneous",
        )


def test_v2_review_correction_preserves_execution_identity_and_evidence(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    store = LifecycleStore(tmp_path)
    result = capture_retrospective_live(store, **_unresolved_kwargs())
    draft_id = result["draft_id"]
    trade_id = result["trade_id"]
    exit_ref = "fixture://v2-correction-exit"
    store.final_close(
        trade_id,
        exit_price=263.07,
        closed_at="2025-06-04T10:13:00+08:00",
        realized_r=-1.0,
        provider_pnl=-10.0,
        source_ref=exit_ref,
    )
    store.attach_evidence(
        draft_id,
        {
            "source_ref": exit_ref,
            "timing": "post_trade",
            "type": "broker_fill",
            "category": "exit",
            "sha256": "b" * 64,
            "captured_at": "2025-06-04T10:13:00+08:00",
        },
    )
    store.verify_exit(
        trade_id,
        authority_class="owner_attestation",
        evidence_refs=[exit_ref],
        realized_r=-1.0,
        provider_pnl=-10.0,
        provider_pnl_authority="provider_read_only",
    )
    owner_confirmed_review(
        store,
        trade_id,
        {"rule_compliance": "not_observable", "diagnostic_class": "INSUFFICIENT", "entry_quality": "unknown", "evidence_completeness": "complete"},
        reviewed_at="2025-06-04T10:14:00+08:00",
    )
    record_path = tmp_path / "canonical/records/trades" / f"{trade_id}.json"
    before = json.loads(record_path.read_text(encoding="utf-8"))
    immutable_before = {key: before[key] for key in ("trade_id", "trade_no", "identity", "actual", "evidence", "strategy")}

    store.correct_review(
        trade_id,
        {"entry_quality": "owner_observed_no_meaningful_setup_at_actual_entry"},
        {
            "status": "unresolved",
            "strategy_id": None,
            "rule_version": None,
            "resolution_source": "tradingview_replay_owner_review",
            "confidence": "high_for_unresolved",
            "reason": "No recognized setup is observable at the actual entry timestamp.",
            "strategy_stats_eligible": False,
        },
        corrected_at="2026-09-22T10:00:00Z",
        correction_id="v2-aave-2506040949134235",
    )

    current = store.current()["records"][trade_id]
    after = json.loads(record_path.read_text(encoding="utf-8"))
    assert current["review"]["entry_quality"] == "owner_observed_no_meaningful_setup_at_actual_entry"
    assert current["strategy_resolution"]["status"] == "unresolved"
    assert current["strategy_resolution"]["strategy_id"] is None
    assert current["strategy_resolution"]["strategy_stats_eligible"] is False
    assert after["strategy_resolution"]["status"] == "unresolved"
    assert after["review"]["entry_quality"] == "owner_observed_no_meaningful_setup_at_actual_entry"
    assert {key: after[key] for key in immutable_before} == immutable_before

    refresh_analytics(tmp_path, generated_at="2026-09-22T00:00:00Z", source_commit="a" * 40)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in analytics["canonical_read_model"] if item["trade_id"] == trade_id)
    assert row["strategy_resolution_status"] == "unresolved"
    assert row["strategy_stats_eligible"] is False
    assert row["review_entry_quality"] == "owner_observed_no_meaningful_setup_at_actual_entry"
    assert row["counted"] is False
    assert row["performance_eligible"] is True
    assert row["exclusion_reason"] == "strategy_unresolved"
    assert row["provider_pnl_verified"] is True
    assert any(item["trade_id"] == trade_id for item in analytics["performance_observations"])
    assert all(item["trade_id"] != trade_id for item in analytics["observations"])
    assert analytics["summary_by_mode"]["live"]["provider_pnl_observation_count"] == 1
    assert analytics["summary_by_mode"]["live"]["verified_provider_pnl"] == -10.0
    assert analytics["strategy_summary_by_mode"]["live"]["provider_pnl_observation_count"] == 0
    assert analytics["summary_by_mode"]["live"]["count"] == analytics["strategy_summary_by_mode"]["live"]["count"] + 1


AFFECTED_UNRESOLVED_PROPW_TRADES = {
    "T000009", "T000010", "T000011", "T000012", "T000013", "T000015", "T000017",
    "T000018", "T000019", "T000021", "T000022", "T000023", "T000024",
}


def _copy_real_analytics_inputs(source: Path, destination: Path) -> None:
    for relative in ("canonical", "registry", "exports/method-validation", "exports/trader-dna/v1"):
        shutil.copytree(source / relative, destination / relative)


def test_current_verified_unresolved_propw_trades_count_overall_only(tmp_path: Path) -> None:
    """Issue #95 regression over the real checked-in PropW population."""
    root = Path(__file__).resolve().parents[1]
    b1_rows, _, _ = _load_b1(root)
    active, version_by_strategy, _, _ = _active_rule_pins(root)
    projection = json.loads((root / "canonical/lifecycle/current.json").read_text(encoding="utf-8"))
    rows, _ = _canonical_observations(root, b1_rows, set(active), version_by_strategy, projection)

    affected = [row for row in rows if row.get("trade_no") in AFFECTED_UNRESOLVED_PROPW_TRADES]
    assert {row["trade_no"] for row in affected} == AFFECTED_UNRESOLVED_PROPW_TRADES
    for row in affected:
        assert row["strategy_resolution_status"] == "unresolved"
        assert row["performance_eligible"] is True
        assert row["strategy_stats_eligible"] is False
        assert row["counted"] is False
        assert row["exclusion_reason"] == "strategy_unresolved"
        assert row["provider_pnl_verified"] is True
        assert isinstance(row["realized_r"], (int, float))

    affected_count = len(affected)
    affected_total_r = sum(float(row["realized_r"]) for row in affected)
    affected_provider_pnl_rows = [
        row for row in affected
        if row.get("provider_pnl_verified") is True and isinstance(row.get("provider_pnl"), (int, float))
    ]
    affected_verified_provider_pnl = sum(float(row["provider_pnl"]) for row in affected_provider_pnl_rows)
    affected_provider_pnl_contributor_count = len(affected_provider_pnl_rows)
    assert affected_count == len(AFFECTED_UNRESOLVED_PROPW_TRADES) == 13
    assert affected_provider_pnl_contributor_count == affected_count

    # Refresh against a disposable copy of checked-in canonical truth. The
    # production checkout and its generated files must not be mutated by tests.
    projection_root = tmp_path / "fresh-real-projection"
    _copy_real_analytics_inputs(root, projection_root)
    refresh_analytics(
        projection_root,
        generated_at="2026-10-08T00:00:00Z",
        source_commit="a" * 40,
    )
    analytics = json.loads(
        (projection_root / "exports/operational/analytics/current.json").read_text(encoding="utf-8")
    )

    performance_rows = analytics["performance_observations"]
    strategy_rows = analytics["observations"]
    performance_trade_nos = {row.get("trade_no") for row in performance_rows}
    strategy_trade_nos = {row.get("trade_no") for row in strategy_rows}
    assert AFFECTED_UNRESOLVED_PROPW_TRADES <= performance_trade_nos
    assert not (AFFECTED_UNRESOLVED_PROPW_TRADES & strategy_trade_nos)

    chronology = analytics["chronology_quality"]
    overall_live = analytics["summary_by_mode"]["live"]
    unaffected_live_rows = [
        row for row in performance_rows
        if row.get("sample_origin") == "live" and row.get("trade_no") not in AFFECTED_UNRESOLVED_PROPW_TRADES
    ]
    unaffected_live_summary = _summary_by_mode(unaffected_live_rows, chronology)["live"]
    # LIVE R counts only observations with verified initial-risk authority.
    # T000035 has a provider-confirmed close/P&L but its R uses a later SL
    # proxy; do not accidentally reinclude it in the historical Issue #95
    # additive-R assertion. Provider P&L remains independently countable.
    t35 = next(row for row in unaffected_live_rows if row.get("trade_no") == "T000035")
    assert t35["performance_eligible"] is True
    assert t35["r_performance_eligible"] is False
    assert t35["provider_pnl_verified"] is True
    unaffected_total_r = sum(
        float(row["realized_r"]) for row in unaffected_live_rows
        if _in_population(row) and isinstance(row.get("realized_r"), (int, float))
    )
    unaffected_provider_pnl_rows = [
        row for row in unaffected_live_rows
        if row.get("provider_pnl_verified") is True and isinstance(row.get("provider_pnl"), (int, float))
    ]
    unaffected_provider_pnl = sum(float(row["provider_pnl"]) for row in unaffected_provider_pnl_rows)

    assert overall_live["count"] == unaffected_live_summary["count"] + affected_count
    assert overall_live["total_r"] == pytest.approx(round(unaffected_total_r + affected_total_r, 6), abs=1e-6)
    assert overall_live["verified_provider_pnl"] == pytest.approx(
        round(unaffected_provider_pnl + affected_verified_provider_pnl, 6), abs=1e-6
    )
    assert overall_live["provider_pnl_observation_count"] == (
        len(unaffected_provider_pnl_rows) + affected_provider_pnl_contributor_count
    )

    named_live = analytics["strategy_summary_by_mode"]["live"]
    assert analytics["strategy_summary_by_mode"]["live"] == _summary_by_mode(strategy_rows, chronology)["live"]
    assert analytics["metrics"]["by_strategy"] == _group_metrics(strategy_rows, "strategy_id", chronology)
    assert "unknown" not in analytics["metrics"]["by_strategy"]
    assert named_live["count"] == len([row for row in strategy_rows if row.get("sample_origin") == "live"])

    counts = analytics["counts"]
    assert counts["performance_observation_count"] == len(performance_rows)
    assert counts["strategy_observation_count"] == len(strategy_rows)
    assert counts["overall_live_performance_count"] == overall_live["count"]
    assert counts["named_strategy_live_count"] == named_live["count"]
    verified_provider_pnl_contributor_count = sum(
        row.get("provider_pnl_verified") is True and isinstance(row.get("provider_pnl"), (int, float))
        for row in performance_rows
    )
    assert counts["verified_provider_pnl_contributor_count"] == verified_provider_pnl_contributor_count
    assert analytics["metrics"]["global"]["provider_pnl_observation_count"] == verified_provider_pnl_contributor_count

    t000001 = next(row for row in rows if row.get("trade_no") == "T000001")
    assert t000001["status"] == "linked_to_historical_seed"
    assert t000001["counted"] is False
    assert t000001["performance_eligible"] is True
    episode_ids = t000001["canonical_episode_ids"]
    assert len(episode_ids) == 1
    episode_id = episode_ids[0]
    b1_episode_rows = [row for row in b1_rows if row.get("canonical_episode_id") == episode_id]
    assert len(b1_episode_rows) == 1
    assert b1_episode_rows[0]["performance_counting_eligibility"] is False
    assert b1_episode_rows[0]["performance_exclusion_reason"] == "non_strategy_outcome"
    refreshed_t000001 = next(
        row for row in analytics["canonical_read_model"] if row.get("trade_no") == "T000001"
    )
    assert refreshed_t000001["status"] == "linked_to_historical_seed"
    assert refreshed_t000001["counted"] is False
    assert refreshed_t000001["performance_eligible"] is True
    assert refreshed_t000001["strategy_stats_eligible"] is False
    t000001_performance_rows = [row for row in performance_rows if row.get("trade_no") == "T000001"]
    assert len(t000001_performance_rows) == 1
    assert t000001_performance_rows[0]["canonical_episode_id"] == episode_id
    assert t000001_performance_rows[0]["realized_r"] == pytest.approx(1.5544136344)
    assert t000001_performance_rows[0]["provider_pnl"] == pytest.approx(38.86034086)
    assert t000001_performance_rows[0]["provider_pnl_verified"] is True
    assert sum(row.get("canonical_episode_id") == episode_id for row in performance_rows) == 1
    assert not any(row.get("canonical_episode_id") == episode_id for row in strategy_rows)
