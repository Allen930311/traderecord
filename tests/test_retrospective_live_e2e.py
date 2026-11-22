"""Proves retrospective LIVE reporting already works end-to-end.

A real execution reported late must stay type=live, with its historical
fact times (created_at / execution_at / closed_at) preserved and only
``recorded_at`` reflecting "now". This composes only existing
LifecycleStore primitives (create_draft(recording_timing="retrospective")
through mark_entry_pending / create_identity_link / verify_entry / finalize
/ partial_close / final_close / verify_exit / review) -- no new lifecycle
logic was needed for this to hold.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from resolver_helpers import matching_observations, no_match_observations
from trading_os.operational_lifecycle import LifecycleStore
from trading_os.unified_capture import capture_retrospective_live


def _copy_registry(source: Path, destination: Path) -> None:
    shutil.copytree(source / "registry", destination / "registry")
    (destination / "canonical" / "sequences").mkdir(parents=True)
    (destination / "canonical" / "records" / "trades").mkdir(parents=True)
    (destination / "canonical" / "adjudications").mkdir(parents=True)
    (destination / "canonical" / "sequences" / "numbers.json").write_text(
        json.dumps({"live_next": 1, "backtest_next": 1, "allocated": [], "tombstones": []}) + "\n",
        encoding="utf-8",
    )
    (destination / "canonical" / "sequences" / "finalizations.json").write_text(
        json.dumps({"schema_version": "finalization-index.v1", "records": {}}) + "\n",
        encoding="utf-8",
    )


# The trade genuinely happened on this historical date...
HISTORICAL_EXECUTION_AT = "2026-06-01T14:00:00Z"
HISTORICAL_CREATED_AT = "2026-06-01T13:55:00Z"
HISTORICAL_CLOSED_AT = "2026-06-01T16:00:00Z"
# ...but the owner is only entering it into the system today.
LATE_RECORDED_AT = "2026-09-11T09:00:00Z"


def test_retrospective_live_preserves_fact_time_and_is_countable(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)

    entry_evidence = {
        "source_ref": "fixture://late-propw-fill",
        "timing": "execution",
        "type": "broker_fill",
        "category": "entry",
        "sha256": "a" * 64,
        "captured_at": HISTORICAL_EXECUTION_AT,
    }
    result = capture_retrospective_live(
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
        market_context="late-reported real trade",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis="retrospective thesis",
        chart_refs=["fixture://chart"],
        created_at=HISTORICAL_CREATED_AT,
        provider_evidence=entry_evidence,
        provider="propw",
        provider_lifecycle_id="late-open-1",
        provider_execution_timestamp=HISTORICAL_EXECUTION_AT,
        candidate_source="fixture://late-candidate-match",
        actual_entry=100.0,
        outcome_known_at_recording=True,
        recorded_at=LATE_RECORDED_AT,
    )
    draft_id = result["draft_id"]
    trade_id = result["trade_id"]
    assert result["trade_no"].startswith("T")

    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    assert trade["type"] == "live"
    assert trade["recording_timing"] == "retrospective"
    assert trade["operational_capture"]["outcome_known_at_recording"] is True
    # Fact time is the real historical execution instant, not "now".
    assert trade["actual"]["entry_at"] == HISTORICAL_EXECUTION_AT
    assert trade["identity"]["event_date"] == HISTORICAL_CREATED_AT[:10]

    # Durable append order recorded the late entry at the late recorded_at,
    # while the domain fact (occurred_at) stayed historical.
    events = [e for e in store._read_events() if e["payload"].get("draft_id") == draft_id or e["payload"].get("trade_id") == trade_id]
    assert events, "expected lifecycle events for the retrospective draft"
    draft_created = next(e for e in events if e["event_type"] == "draft_created")
    assert draft_created["occurred_at"] == HISTORICAL_CREATED_AT
    assert draft_created["recorded_at"] == LATE_RECORDED_AT

    # Close and verify the exit -- still composing only existing primitives.
    store.final_close(
        trade_id,
        exit_price=110.0,
        closed_at=HISTORICAL_CLOSED_AT,
        realized_r=2.0,
        source_ref="fixture://late-final-exit",
        recorded_at=LATE_RECORDED_AT,
    )
    store.attach_evidence(
        draft_id,
        {
            "source_ref": "fixture://late-final-exit",
            "timing": "post_trade",
            "type": "broker_fill",
            "category": "exit",
            "sha256": "b" * 64,
            "captured_at": HISTORICAL_CLOSED_AT,
        },
        recorded_at=LATE_RECORDED_AT,
    )
    store.verify_exit(
        trade_id,
        authority_class="owner_attestation",
        evidence_refs=["fixture://late-final-exit"],
        realized_r=2.0,
        recorded_at=LATE_RECORDED_AT,
    )

    projection = store.current()
    record = projection["records"][trade_id]
    assert record["state"] == "CLOSED"
    assert record["exit_verification"]["state"] == "confirmed"
    assert record["realized_r"] == 2.0
    # Closed_at is the historical fact time, unaffected by the late report.
    assert record["closed_at"] == HISTORICAL_CLOSED_AT

    # Countability itself does not special-case recording_timing: identity
    # verified + entry confirmed + exit confirmed + realized R known is
    # exactly the same bar a contemporaneous live trade must clear.
    identity_verified = record["identity_status"] == "MATCHED"
    entry_confirmed = record["execution_verification"]["entry"]["state"] == "confirmed"
    exit_confirmed = record["exit_verification"]["state"] == "confirmed"
    assert identity_verified and entry_confirmed and exit_confirmed and record["realized_r"] is not None
