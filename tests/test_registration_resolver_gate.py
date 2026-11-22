"""E2E: the strategy resolver is a mandatory gate on registration, and registration != review."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from resolver_helpers import matching_observations, no_match_observations
from test_historical_unresolved_live import _copy_operational_inputs
from trading_os.operational_analytics import refresh_analytics
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.historical_attribution import REGISTRY_VERSION, historical_attribution_registry_digest
from trading_os.strategy_resolver import ResolverError
from trading_os.trade_review import review_registered_trade
from trading_os.unified_capture import capture_live, capture_retrospective_live
from review_test_support import owner_confirmed_review

ROOT = Path(__file__).resolve().parents[1]
CREATED = "2025-06-04T09:48:00+08:00"
EXEC_AT = "2025-06-04T09:49:00+08:00"


def _kwargs(observations, lifecycle_id="2506040949134235", sha="a"):
    return dict(
        strategy_observations=observations,
        symbol="AAVEUSDT",
        direction="long",
        intended_entry=265.79,
        stop_loss=258.0,
        take_profit=280.0,
        planned_rr=1.8,
        timeframe_context="1h",
        timeframe_trigger="15m",
        market_context="gate fixture",
        setup_tags=[],
        confirmation_tags=[],
        thesis="gate fixture thesis",
        chart_refs=["fixture://chart"],
        created_at=CREATED,
        provider_evidence={
            "source_ref": f"fixture://fill-{lifecycle_id}",
            "timing": "execution",
            "type": "broker_fill",
            "category": "entry",
            "sha256": sha * 64,
            "captured_at": EXEC_AT,
        },
        provider="propw",
        provider_lifecycle_id=lifecycle_id,
        provider_execution_timestamp=EXEC_AT,
        candidate_source="fixture://match",
        actual_entry=265.79,
        outcome_known_at_recording=True,
    )


def _store(tmp_path):
    _copy_operational_inputs(ROOT, tmp_path)
    return LifecycleStore(tmp_path)


def _close(store, draft_id, trade_id):
    ref = "fixture://exit"
    store.final_close(trade_id, exit_price=263.07, closed_at="2025-06-04T10:13:00+08:00", realized_r=-1.0, provider_pnl=-10.0, source_ref=ref)
    store.attach_evidence(
        draft_id,
        {"source_ref": ref, "timing": "post_trade", "type": "broker_fill", "category": "exit", "sha256": "b" * 64, "captured_at": "2025-06-04T10:13:00+08:00"},
    )
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[ref], realized_r=-1.0)


def _no_writes(tmp_path):
    drafts = tmp_path / "canonical" / "drafts"
    assert not (drafts.exists() and list(drafts.glob("*.json")))
    assert not (tmp_path / "canonical" / "lifecycle" / "identity-links.jsonl").exists()
    numbers = json.loads((tmp_path / "canonical/sequences/numbers.json").read_text(encoding="utf-8"))
    assert numbers["allocated"] == []


def test_caller_cannot_assert_strategy_id(tmp_path):
    store = _store(tmp_path)
    kwargs = _kwargs(matching_observations("stg-vegas-tunnel"))
    with pytest.raises(TypeError):
        capture_retrospective_live(store, strategy_id="stg-po3", rule_version="stg-po3:v1", **kwargs)
    # forging a rule the registry does not define is rejected, and nothing is written
    bogus = {"observations": [{"rule_id": "made.up", "state": "PASS", "timing": "pre_trade", "evidence_refs": ["x"]}]}
    with pytest.raises(ResolverError):
        capture_retrospective_live(store, **_kwargs(bogus))
    _no_writes(tmp_path)
    # no observations creates unresolved attribution, not a fake strategy or a pending registration
    result = capture_retrospective_live(store, **_kwargs({}))
    assert result["registered"] is True and result["trade_no"].startswith("T")
    assert result["strategy_result"]["status"] == "INSUFFICIENT_EVIDENCE"
    record = store.current()["records"][result["trade_id"]]
    assert record["strategy_id"] is None and record["rule_version"] is None
    assert record["strategy_resolution"]["status"] == "unresolved"
    assert record["strategy_resolution"]["strategy_stats_eligible"] is False


def test_matched_registration_succeeds_and_is_unreviewed(tmp_path):
    store = _store(tmp_path)
    result = capture_retrospective_live(store, **_kwargs(matching_observations("stg-vegas-tunnel")))
    assert result["registered"] is True and result["registration_status"] == "REGISTRATION_COMPLETE"
    assert result["review_status"] == "not_started"
    assert result["strategy_result"]["status"] == "MATCHED"
    record = store.current()["records"][result["trade_id"]]
    assert record["trade_no"].startswith("T")
    assert record["strategy_id"] == "stg-vegas-tunnel" and record["rule_version"] == "stg-vegas-tunnel:v1"
    assert record["strategy_resolution"]["status"] == "replay_reconstructed"
    assert record["strategy_resolution"]["strategy_stats_eligible"] is True
    canonical = json.loads((tmp_path / "canonical/records/trades" / f"{result['trade_id']}.json").read_text(encoding="utf-8"))
    assert canonical["plan"]["target_price"] == canonical["plan"]["take_profit_price"] == 280.0
    assert record["state"] != "REVIEWED"


@pytest.mark.parametrize(
    "observations",
    [{}, {"observations": [{"rule_id": "po3.identity", "state": "PASS", "timing": "pre_trade", "evidence_refs": ["x"]}]}],
)
def test_insufficient_registers_with_unresolved_attribution(tmp_path, observations):
    store = _store(tmp_path)
    result = capture_retrospective_live(store, **_kwargs(observations))
    assert result["registered"] is True and result["registration_status"] == "REGISTRATION_COMPLETE"
    assert result["trade_no"].startswith("T")
    assert result["strategy_result"]["status"] == "INSUFFICIENT_EVIDENCE"
    record = store.current()["records"][result["trade_id"]]
    assert record["strategy_id"] is None and record["rule_version"] is None
    assert record["strategy_resolution"]["status"] == "unresolved"
    assert record["strategy_resolution"]["resolver_status"] == "INSUFFICIENT_EVIDENCE"


def test_ambiguous_registers_with_unresolved_attribution(tmp_path):
    store = _store(tmp_path)
    obs = matching_observations("stg-vegas-tunnel")
    obs["observations"].append({"rule_id": "po3.identity", "state": "PASS", "timing": "pre_trade", "evidence_refs": ["x"]})
    result = capture_retrospective_live(store, **_kwargs(obs))
    assert result["registered"] is True and result["strategy_result"]["status"] == "AMBIGUOUS"
    record = store.current()["records"][result["trade_id"]]
    assert record["strategy_id"] is None and record["rule_version"] is None
    assert record["strategy_resolution"]["status"] == "unresolved"
    assert record["strategy_resolution"]["resolver_status"] == "AMBIGUOUS"


def test_no_match_registers_real_trade_but_is_not_named_strategy_eligible(tmp_path):
    store = _store(tmp_path)
    result = capture_live(store, **{**_kwargs(no_match_observations()), "recording_timing": "contemporaneous"})
    assert result["registered"] is True and result["strategy_result"]["status"] == "NO_MATCH"
    trade_id, draft_id = result["trade_id"], result["draft_id"]
    record = store.current()["records"][trade_id]
    assert record["trade_no"].startswith("T")
    assert record["strategy_id"] is None and record["rule_version"] is None
    res = record["strategy_resolution"]
    assert res["status"] == "no_match" and res["strategy_id"] is None and res["rule_version"] is None
    assert res["strategy_stats_eligible"] is False

    _close(store, draft_id, trade_id)
    owner_confirmed_review(
        store, trade_id, {"rule_compliance": "not_observable", "diagnostic_class": "INSUFFICIENT"}, reviewed_at="2025-06-04T10:14:00+08:00", via_registered_writer=True
    )
    refresh_analytics(tmp_path, generated_at="2026-10-06T00:00:00Z", source_commit="a" * 40)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in analytics["canonical_read_model"] if item["trade_id"] == trade_id)
    assert row["counted"] is False and row["strategy_stats_eligible"] is False
    named = [item for item in analytics["canonical_read_model"] if item["strategy_id"] == "stg-vegas-tunnel"]
    assert all(item["trade_id"] != trade_id for item in named)


def test_verified_closed_unresolved_trade_counts_overall_only(tmp_path):
    store = _store(tmp_path)
    result = capture_live(store, **{**_kwargs({}), "recording_timing": "contemporaneous"})
    trade_id, draft_id = result["trade_id"], result["draft_id"]
    assert result["registered"] is True
    assert result["trade_no"].startswith("T")
    _close(store, draft_id, trade_id)
    record = store.current()["records"][trade_id]
    assert record["state"] == "CLOSED"
    assert record["review_status"] == "not_started"
    assert record["strategy_id"] is None
    assert record["strategy_resolution"]["status"] == "unresolved"
    assert record["reported_provider_pnl"] == -10.0

    refresh_analytics(tmp_path, generated_at="2026-10-06T00:00:00Z", source_commit="a" * 40)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in analytics["canonical_read_model"] if item["trade_id"] == trade_id)
    assert row["performance_eligible"] is True
    assert row["strategy_stats_eligible"] is False and row["counted"] is False
    assert row["exclusion_reason"] == "strategy_unresolved"
    assert trade_id in {item["trade_id"] for item in analytics["performance_observations"]}
    assert all(item["trade_id"] != trade_id for item in analytics["observations"])


def test_live_registration_accepts_missing_plan_and_marks_it_partial(tmp_path):
    store = _store(tmp_path)
    kwargs = _kwargs({})
    kwargs.update(
        intended_entry=None,
        stop_loss=None,
        take_profit="unknown",
        planned_rr=None,
        timeframe_context="unknown",
        timeframe_trigger="15m",
        market_context=None,
        thesis="unknown",
    )
    result = capture_live(store, **kwargs)
    assert result["registered"] is True and result["trade_no"].startswith("T")
    record = json.loads((tmp_path / "canonical/records/trades" / f"{result['trade_id']}.json").read_text(encoding="utf-8"))
    assert record["plan"]["entry_price"] is None
    assert record["plan"]["stop_loss_price"] is None
    assert record["plan"]["take_profit_price"] == "unknown"
    assert record["plan"]["rr_pretrade"] is None
    assert record["plan"]["completeness"] == "partial"
    assert record["strategy"]["strategy_id"] is None


def test_later_strategy_attribution_amends_same_trade_and_analytics(tmp_path):
    store = _store(tmp_path)
    result = capture_live(store, **{**_kwargs({}), "recording_timing": "contemporaneous"})
    trade_id, draft_id, trade_no = result["trade_id"], result["draft_id"], result["trade_no"]
    _close(store, draft_id, trade_id)
    chart_ref = "fixture://attribution-chart"
    store.attach_evidence(
        draft_id,
        {"source_ref": chart_ref, "timing": "pre_trade", "type": "chart_snapshot", "category": "pre_trade_context", "sha256": "d" * 64, "captured_at": EXEC_AT},
    )
    resolver_input = matching_observations("stg-vegas-tunnel")
    for observation in resolver_input["observations"]:
        observation["evidence_refs"] = [chart_ref]

    refresh_analytics(tmp_path, generated_at="2026-10-06T00:00:00Z", source_commit="a" * 40)
    before = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    before_row = next(item for item in before["canonical_read_model"] if item["trade_id"] == trade_id)
    assert before_row["performance_eligible"] is True and before_row["strategy_stats_eligible"] is False

    event = store.resolve_strategy_attribution(
        trade_id,
        resolver_input=resolver_input,
        amendment_id="owner-attribution-1",
        reason="Owner confirmed the strategy attribution after reviewing attached entry evidence.",
        resolved_at="2026-10-07T10:00:00Z",
    )
    repeated = store.resolve_strategy_attribution(
        trade_id,
        resolver_input=resolver_input,
        amendment_id="owner-attribution-1",
        reason="Owner confirmed the strategy attribution after reviewing attached entry evidence.",
        resolved_at="2026-10-07T10:00:00Z",
    )
    assert event["event_id"] == repeated["event_id"]
    current = store.current()["records"][trade_id]
    assert current["trade_id"] == trade_id and current["trade_no"] == trade_no
    assert current["strategy_id"] == "stg-vegas-tunnel"
    assert current["strategy_resolution"]["status"] == "replay_reconstructed"
    assert current["strategy_attribution_amendment"]["amendment_id"] == "owner-attribution-1"
    canonical = json.loads((tmp_path / "canonical/records/trades" / f"{trade_id}.json").read_text(encoding="utf-8"))
    assert canonical["trade_no"] == trade_no and canonical["strategy"]["strategy_id"] is None
    numbers = json.loads((tmp_path / "canonical/sequences/numbers.json").read_text(encoding="utf-8"))
    assert len([item for item in numbers["allocated"] if item == trade_no]) == 1

    refresh_analytics(tmp_path, generated_at="2026-10-07T00:00:00Z", source_commit="b" * 40)
    after = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    after_row = next(item for item in after["canonical_read_model"] if item["trade_id"] == trade_id)
    assert after_row["strategy_id"] == "stg-vegas-tunnel"
    assert after_row["performance_eligible"] is True and after_row["strategy_stats_eligible"] is True
    assert after_row["counted"] is True
    assert trade_id in {item["trade_id"] for item in after["observations"]}


def test_retrospective_attribution_amendment_pins_registry_authority_immutably(tmp_path):
    store = _store(tmp_path)
    captured = capture_live(store, **{**_kwargs({}), "recording_timing": "contemporaneous"})
    trade_id, draft_id = captured["trade_id"], captured["draft_id"]
    _close(store, draft_id, trade_id)

    replay_ref = "fixture://registry-pinned-owner-replay"
    store.attach_evidence(
        draft_id,
        {
            "source_ref": replay_ref,
            "timing": "post_trade",
            "type": "replay_chart",
            "category": "review",
            "sha256": "c" * 64,
            "provenance": {"capture_mode": "retrospective_replay"},
            "role": "historical_market_reconstruction",
        },
    )
    resolver_input = {
        "owner_attestation": {
            "timing": "retrospective",
            "setup_status": "HAS_SETUP",
            "primary_strategy_id": "stg-ict-liquidity",
            "entry_expression": "ob-mss",
            "entry_triggers": ["MSS", "OB_RETEST"],
            "rationale": "Owner confirms ICT OB plus MSS entry intent.",
            "evidence_refs": [replay_ref],
        },
    }
    digest_before = historical_attribution_registry_digest(tmp_path)
    event = store.resolve_strategy_attribution(
        trade_id,
        resolver_input=resolver_input,
        amendment_id="owner-attribution-authority-pin",
        resolved_at="2025-06-04T10:20:00+08:00",
    )
    expected_authority = {
        "strategy_id": "stg-ict-liquidity",
        "rule_version": "stg-ict-liquidity:v1",
        "retrospective_attribution_registry_version": REGISTRY_VERSION,
        "retrospective_attribution_registry_digest": digest_before,
    }
    assert event["payload"]["amendment"]["authority"] == expected_authority

    event_readback = next(item for item in store._read_events() if item["event_id"] == event["event_id"])
    assert event_readback["payload"]["amendment"]["authority"] == expected_authority

    refresh_analytics(tmp_path, generated_at="2026-10-08T00:00:00Z", source_commit="c" * 40)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in analytics["canonical_read_model"] if item["trade_id"] == trade_id)
    assert row["method_observability"]["status"] == "not_observable"

    registry_path = tmp_path / "registry/retrospective-attribution.yaml"
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    registry["confluence_contexts"][0]["aliases"].append("mutated-after-amendment")
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    assert historical_attribution_registry_digest(tmp_path) != digest_before

    event_after_registry_mutation = next(
        item for item in store._read_events() if item["event_id"] == event["event_id"]
    )
    assert event_after_registry_mutation["payload"]["amendment"]["authority"] == expected_authority
    projected = LifecycleStore(tmp_path).current()["records"][trade_id]
    assert projected["strategy_attribution_amendment"]["authority"] == expected_authority


def test_owner_retrospective_replay_projects_bound_frozen_pretrade_rule_facts(tmp_path):
    store = _store(tmp_path)
    captured = capture_live(store, **{**_kwargs({}), "recording_timing": "contemporaneous"})
    trade_id, draft_id = captured["trade_id"], captured["draft_id"]
    _close(store, draft_id, trade_id)

    replay_ref = "fixture://owner-ict-historical-replay"
    store.attach_evidence(
        draft_id,
        {
            "source_ref": replay_ref,
            "timing": "post_trade",
            "type": "replay_chart",
            "category": "review",
            "sha256": "d" * 64,
            "captured_at": "2025-06-04T10:14:00+08:00",
            "provenance": {
                "capture_mode": "retrospective_replay",
                "captured_at": "2025-06-04T10:14:00+08:00",
            },
            "role": "historical_market_reconstruction",
        },
    )
    resolver_input = {
        "owner_attestation": {
            "timing": "retrospective",
            "setup_status": "HAS_SETUP",
            "primary_strategy_id": "stg-ict-liquidity",
            "entry_expression": "ob-mss",
            "entry_triggers": ["MSS", "OB_RETEST"],
            "plan_facts": {"stop_loss_price": 258.0, "target_price": 280.0},
            "rationale": "Owner confirms the ICT OB-MSS entry and its planned risk/target.",
            "evidence_refs": [replay_ref],
        },
        "retrospective_observations": [
            *[
                {
                    "claim_id": f"entry_trigger:{trigger}",
                    "finding": "corroborates",
                    "timing": "post_trade",
                    "evidence_refs": [replay_ref],
                }
                for trigger in ("MSS", "OB_RETEST")
            ],
            *[
                {
                    "claim_id": f"plan:{field}",
                    "finding": "corroborates",
                    "timing": "post_trade",
                    "evidence_refs": [replay_ref],
                }
                for field in ("stop_loss_price", "target_price")
            ],
        ],
    }
    event = store.resolve_strategy_attribution(
        trade_id,
        resolver_input=resolver_input,
        amendment_id="owner-ict-replay-observability",
        resolved_at="2025-06-04T10:15:00+08:00",
    )
    resolution = event["payload"]["strategy_resolution"]
    projection = resolution["retrospective_rule_facts"]
    assert projection["schema_version"] == "retrospective-rule-facts.v1"
    assert projection["strategy_id"] == "stg-ict-liquidity"
    assert projection["rule_version"] == "stg-ict-liquidity:v1"
    assert projection["retrospective_attribution_registry_digest"] == event["payload"]["amendment"]["authority"]["retrospective_attribution_registry_digest"]
    assert [item["rule_id"] for item in projection["rules"]] == [
        "ict_liquidity.identity",
        "ict_liquidity.entry_expression",
        "ict_liquidity.invalidation",
        "ict_liquidity.target",
    ]
    assert all(item["state"] == "PASS" for item in projection["rules"])
    assert all(item["supports_fact_timing"] == "pre_trade" for item in projection["rules"])
    assert projection["rules"][1]["source_claim_ids"] == [
        "entry_trigger:MSS",
        "entry_trigger:OB_RETEST",
    ]

    record = store.current()["records"][trade_id]
    replay = next(item for item in record["evidence"] if item["source_ref"] == replay_ref)
    assert replay["timing"] == "post_trade"
    assert replay["category"] == "review"
    assert replay["type"] == "replay_chart"
    assert replay["provenance"]["capture_mode"] == "retrospective_replay"
    assert replay["role"] == "historical_market_reconstruction"

    refresh_analytics(tmp_path, generated_at="2026-10-08T00:00:00Z", source_commit="d" * 40)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in analytics["canonical_read_model"] if item["trade_id"] == trade_id)
    assert row["method_observability"]["status"] == "machine_observable"
    facts = row["method_observability"]["required_rules"]
    assert [item["rule_id"] for item in facts if item["required"]] == [
        "ict_liquidity.identity",
        "ict_liquidity.entry_expression",
        "ict_liquidity.invalidation",
        "ict_liquidity.target",
    ]
    assert all(item["state"] == "SATISFIED" for item in facts if item["required"])
    assert all(item["supports_fact_timing"] == "pre_trade" for item in facts if item["required"])
    assert all(item["basis"] == ["owner_attestation", "retrospective_replay"] for item in facts if item["required"])
    assert all(item["evidence_refs"] == [replay_ref] for item in facts if item["required"])


def test_retrospective_owner_attestation_uses_post_trade_refs_and_keeps_conflict_unresolved(tmp_path):
    store = _store(tmp_path)
    result = capture_live(store, **{**_kwargs({}), "recording_timing": "contemporaneous"})
    trade_id, draft_id, trade_no = result["trade_id"], result["draft_id"], result["trade_no"]
    replay_ref = "artifact:retrospective-replay.png"
    store.attach_evidence(
        draft_id,
        {
            "source_ref": replay_ref,
            "timing": "post_trade",
            "type": "replay_chart",
            "category": "review",
            "sha256": "e" * 64,
            "captured_at": "2025-06-04T10:14:00+08:00",
            "provenance": {
                "capture_mode": "retrospective_replay",
                "captured_at": "2025-06-04T10:14:00+08:00",
            },
            "role": "historical_market_reconstruction",
        },
    )
    resolver_input = {
        "owner_attestation": {
            "timing": "retrospective",
            "setup_status": "HAS_SETUP",
            "primary_strategy_id": "stg-ict-liquidity",
            "entry_expression": "ob-mss",
            "entry_triggers": ["MSS", "OB_RETEST"],
            "confluences": ["stg-vegas-tunnel"],
            "rationale": "MSS above Vegas Tunnel with support OB retest.",
            "evidence_refs": [replay_ref],
        },
        "retrospective_observations": [
            {
                "claim_id": "entry_trigger:MSS",
                "finding": "contradicts",
                "timing": "post_trade",
                "contradiction_code": "trigger_after_provider_entry",
                "observed_fact": "MSS first appeared at 10:14:00+08:00, after the verified provider entry at 09:49:00+08:00.",
                "observed_at": "2025-06-04T10:14:00+08:00",
                "evidence_refs": [replay_ref],
            },
        ],
    }
    with pytest.raises(LifecycleError, match="clarify this exact fact") as exc_info:
        store.resolve_strategy_attribution(
            trade_id,
            resolver_input=resolver_input,
            amendment_id="material-conflict",
            resolved_at="2025-06-04T10:15:00Z",
        )
    assert "MSS first appeared at 10:14:00+08:00" in str(exc_info.value)
    current = store.current()["records"][trade_id]
    assert current["trade_no"] == trade_no
    assert current["strategy_id"] is None
    assert current["strategy_resolution"]["status"] == "unresolved"
    assert not any(event["event_type"] == "strategy_attribution_resolved" for event in store._read_events())


def test_retrospective_owner_attestation_does_not_relabel_review_image_as_pre_trade(tmp_path):
    store = _store(tmp_path)
    result = capture_live(store, **{**_kwargs({}), "recording_timing": "contemporaneous"})
    trade_id, draft_id = result["trade_id"], result["draft_id"]
    replay_ref = "artifact:replay-true-post-trade.png"
    # The real attachment is review/post_trade. The schema must reject an
    # observation that claims those same bytes were pre_trade.
    store.attach_evidence(
        draft_id,
        {
            "source_ref": replay_ref,
            "timing": "post_trade",
            "type": "replay_chart",
            "category": "review",
            "sha256": "f" * 64,
            "captured_at": "2025-06-04T10:14:00+08:00",
            "provenance": {
                "capture_mode": "retrospective_replay",
                "captured_at": "2025-06-04T10:14:00+08:00",
            },
            "role": "historical_market_reconstruction",
        },
    )
    resolver_input = {
        "owner_attestation": {
            "timing": "retrospective",
            "setup_status": "HAS_SETUP",
            "primary_strategy_id": "stg-ict-liquidity",
            "entry_expression": "ob-mss",
            "entry_triggers": ["MSS", "OB_RETEST"],
            "confluences": ["stg-vegas-tunnel"],
            "rationale": "MSS above Vegas Tunnel with support OB retest.",
            "evidence_refs": [replay_ref],
        },
        "retrospective_observations": [
            {"claim_id": "entry_trigger:MSS", "finding": "corroborates", "timing": "pre_trade", "evidence_refs": [replay_ref]},
        ],
    }
    with pytest.raises(LifecycleError, match="provenance does not match"):
        store.resolve_strategy_attribution(
            trade_id,
            resolver_input=resolver_input,
            amendment_id="reject-false-provenance",
            resolved_at="2025-06-04T10:15:00Z",
        )
    assert store.current()["records"][trade_id]["strategy_resolution"]["status"] == "unresolved"


def test_registration_may_stay_unreviewed_then_review_runs_independently(tmp_path):
    store = _store(tmp_path)
    result = capture_retrospective_live(store, **_kwargs(matching_observations("stg-vegas-tunnel")))
    trade_id, draft_id = result["trade_id"], result["draft_id"]
    _close(store, draft_id, trade_id)
    record = store.current()["records"][trade_id]
    assert record["state"] == "CLOSED"  # fully registered and closed, still not reviewed
    events = [e["event_type"] for e in store._read_events()]
    assert "reviewed" not in events and "review_started" not in events
    numbers_path = tmp_path / "canonical/sequences/numbers.json"
    before = json.loads(numbers_path.read_text(encoding="utf-8"))["allocated"]

    owner_confirmed_review(store, trade_id, {"rule_compliance": "unknown", "diagnostic_class": "INSUFFICIENT"}, reviewed_at="2025-06-04T10:14:00+08:00", via_registered_writer=True)
    after = store.current()["records"][trade_id]
    assert after["state"] == "REVIEWED" and after["trade_no"] == record["trade_no"]
    assert json.loads(numbers_path.read_text(encoding="utf-8"))["allocated"] == before
    assert after["strategy_resolution"] == record["strategy_resolution"]  # review does not rewrite registration facts


@pytest.mark.parametrize("ref", ["rec_abc", "trd_does_not_exist", "T000001", ""])
def test_trade_review_rejects_unregistered_references(tmp_path, ref):
    store = _store(tmp_path)
    with pytest.raises(LifecycleError, match="already-registered"):
        review_registered_trade(store, ref, {"rule_compliance": "compliant"}, reviewed_at="2025-06-04T10:14:00+08:00")
    _no_writes(tmp_path)
