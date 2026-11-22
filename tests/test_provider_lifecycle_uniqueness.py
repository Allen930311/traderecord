"""One provider lifecycle may bind to at most one canonical draft, regardless of provider casing."""

from __future__ import annotations

from pathlib import Path

import pytest

from test_retrospective_live_e2e import (
    HISTORICAL_CREATED_AT,
    HISTORICAL_EXECUTION_AT,
    LATE_RECORDED_AT,
    _copy_registry,
)
from resolver_helpers import matching_observations, no_match_observations
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore, canonical_provider
from trading_os.unified_capture import capture_retrospective_live


def _capture(store: LifecycleStore, *, provider: str, lifecycle_id: str, created_at: str, thesis: str, sha: str):
    return capture_retrospective_live(
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
        market_context="uniqueness fixture",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis=thesis,
        chart_refs=["fixture://chart"],
        created_at=created_at,
        provider_evidence={
            "source_ref": f"fixture://fill-{sha}",
            "timing": "execution",
            "type": "broker_fill",
            "category": "entry",
            "sha256": sha * 64,
            "captured_at": HISTORICAL_EXECUTION_AT,
        },
        provider=provider,
        provider_lifecycle_id=lifecycle_id,
        provider_execution_timestamp=HISTORICAL_EXECUTION_AT,
        candidate_source="fixture://candidate",
        actual_entry=100.0,
        outcome_known_at_recording=True,
        recorded_at=LATE_RECORDED_AT,
    )


def test_canonical_provider_is_lowercase() -> None:
    assert canonical_provider(" PropW ") == "propw"
    assert canonical_provider(None) == ""


def test_rerunning_the_same_lifecycle_returns_the_original_trade(tmp_path: Path) -> None:
    _copy_registry(Path(__file__).resolve().parents[1], tmp_path)
    store = LifecycleStore(tmp_path)
    first = _capture(store, provider="propw", lifecycle_id="dup-1", created_at=HISTORICAL_CREATED_AT, thesis="a", sha="a")
    again = _capture(store, provider="PropW", lifecycle_id="dup-1", created_at="2026-06-01T13:50:00Z", thesis="b", sha="b")
    assert again["already_registered"] is True and again["registered"] is True
    assert again["trade_no"] == first["trade_no"] and again["draft_id"] == first["draft_id"]
    links = [l for l in store._read_identity_links() if l["provider_lifecycle_id"] == "dup-1"]
    assert len(links) == 1 and links[0]["provider"] == "propw"  # no second draft / link / T-number


def test_low_level_identity_link_still_rejects_a_second_draft(tmp_path: Path) -> None:
    from live_resolution_fixture import second_draft_link_attempt

    _copy_registry(Path(__file__).resolve().parents[1], tmp_path)
    store = LifecycleStore(tmp_path)
    _capture(store, provider="propw", lifecycle_id="dup-2", created_at=HISTORICAL_CREATED_AT, thesis="a", sha="a")
    with pytest.raises(LifecycleError, match="ALREADY_REGISTERED"):
        second_draft_link_attempt(store, "PropW", "dup-2")


def test_distinct_lifecycles_still_register(tmp_path: Path) -> None:
    _copy_registry(Path(__file__).resolve().parents[1], tmp_path)
    store = LifecycleStore(tmp_path)
    a = _capture(store, provider="propw", lifecycle_id="x-1", created_at=HISTORICAL_CREATED_AT, thesis="a", sha="a")
    b = _capture(store, provider="PropW", lifecycle_id="x-2", created_at="2026-06-01T13:50:00Z", thesis="b", sha="b")
    assert a["trade_no"] != b["trade_no"]
