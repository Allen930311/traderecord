"""Low-level helper: open a second LIVE draft and try to bind it to an already-bound provider lifecycle."""

from __future__ import annotations

from resolver_helpers import live_resolution


def second_draft_link_attempt(store, provider: str, lifecycle_id: str):
    draft = store.create_draft(
        strategy_id="stg-vegas-tunnel", rule_version="stg-vegas-tunnel:v1", strategy_resolution=live_resolution("stg-vegas-tunnel"),
        symbol="BTCUSDT", direction="long", intended_entry=100.0, stop_loss=95.0, take_profit=110.0, planned_rr=2.0,
        timeframe_context="1h", timeframe_trigger="15m", market_context="second", setup_tags=["vegas_tunnel"], confirmation_tags=[],
        thesis="second draft", chart_refs=["fixture://c"], created_at="2026-06-01T13:51:00Z", recording_timing="retrospective",
        draft_id="rec_" + "f9" * 10,
    )
    ref = "fixture://second-fill"
    store.attach_evidence(draft["draft_id"], {"source_ref": ref, "timing": "execution", "type": "broker_fill", "category": "entry", "sha256": "9" * 64, "captured_at": "2026-06-01T14:00:00Z"})
    store.mark_entry_pending(draft["draft_id"], provider={"provider": provider})
    return store.create_identity_link(
        draft["draft_id"], provider=provider, provider_lifecycle_id=lifecycle_id, instrument="BTCUSDT", direction="long",
        provider_execution_timestamp="2026-06-01T14:00:00Z", candidate_source="fixture://c", match_disposition="MATCHED", evidence_refs=[ref],
    )
