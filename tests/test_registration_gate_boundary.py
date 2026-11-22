"""Provider verification gates LIVE T-ID; unresolved attribution stays in overall performance."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from resolver_helpers import MATCHING_OBSERVATIONS_FILE, live_resolution, matching_observations, no_match_observations
from test_historical_unresolved_live import _copy_operational_inputs
from test_registration_resolver_gate import CREATED, EXEC_AT, _kwargs
from trading_os.operational_analytics import refresh_analytics
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.unified_capture import capture_retrospective_live

ROOT = Path(__file__).resolve().parents[1]
NAMED = ("stg-vegas-tunnel", "stg-po3", "stg-ict-liquidity", "stg-triple-tap")


def _store(tmp_path):
    _copy_operational_inputs(ROOT, tmp_path)
    return LifecycleStore(tmp_path)


def _draft_kwargs(**overrides):
    values = dict(
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
        market_context="boundary fixture",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis="boundary fixture thesis",
        chart_refs=["fixture://chart"],
        created_at="2026-09-11T00:00:00Z",
    )
    values.update(overrides)
    return values


def _drive_to_open(store, draft_id):
    """The low-level draft -> evidence -> entry-pending -> identity-link -> verify-entry steps."""

    ref = "fixture://low-level-fill"
    store.attach_evidence(
        draft_id,
        {"source_ref": ref, "timing": "execution", "type": "broker_fill", "category": "entry", "sha256": "c" * 64, "captured_at": "2026-09-11T00:01:00Z"},
    )
    store.mark_entry_pending(draft_id, provider={"provider": "propw"})
    store.create_identity_link(
        draft_id,
        provider="propw",
        provider_lifecycle_id="low-level-1",
        instrument="BTCUSDT",
        direction="long",
        provider_execution_timestamp="2026-09-11T00:01:00Z",
        candidate_source="fixture://c",
        match_disposition="MATCHED",
        evidence_refs=[ref],
    )
    store.verify_entry(
        draft_id,
        instrument="BTCUSDT",
        direction="long",
        actual_entry=100.0,
        execution_at="2026-09-11T00:01:00Z",
        source_ref=ref,
        provider_lifecycle_id="low-level-1",
        provider="propw",
    )


def test_low_level_live_draft_without_resolver_is_refused(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(LifecycleError, match="requires a strategy_resolution"):
        store.create_draft(**_draft_kwargs())
    forged = {
        "status": "contemporaneous_confirmed",
        "strategy_id": "stg-vegas-tunnel",
        "rule_version": "stg-vegas-tunnel:v1",
        "strategy_stats_eligible": True,
        "resolution_source": "owner-said-so",
        "confidence": "high",
        "reason": "hand built",
    }
    with pytest.raises(LifecycleError, match="from the strategy resolver"):
        store.create_draft(**_draft_kwargs(strategy_resolution=forged))
    with pytest.raises(LifecycleError, match="does not match"):
        store.create_draft(**_draft_kwargs(strategy_id="stg-po3", rule_version="stg-po3:v1", strategy_resolution=live_resolution("stg-vegas-tunnel")))
    assert not (tmp_path / "canonical" / "lifecycle" / "events.jsonl").exists() or not (tmp_path / "canonical" / "lifecycle" / "events.jsonl").read_text(encoding="utf-8").strip()


def test_resolver_produced_resolution_allows_the_low_level_path(tmp_path):
    store = _store(tmp_path)
    draft = store.create_draft(**_draft_kwargs(draft_id="rec_" + "e1" * 10, strategy_resolution=live_resolution("stg-vegas-tunnel")))
    _drive_to_open(store, draft["draft_id"])
    assert store.finalize(draft["draft_id"])["trade_no"].startswith("T")


def test_finalize_fails_closed_for_a_live_draft_that_lost_its_resolution(tmp_path):
    store = _store(tmp_path)
    draft_id = "rec_" + "e2" * 10
    store.create_draft(**_draft_kwargs(draft_id=draft_id, strategy_resolution=live_resolution("stg-vegas-tunnel")))
    _drive_to_open(store, draft_id)
    path = tmp_path / "canonical" / "drafts" / f"{draft_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop("strategy_resolution")
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(LifecycleError, match="valid resolver-produced attribution state"):
        store.finalize(draft_id)
    numbers = json.loads((tmp_path / "canonical/sequences/numbers.json").read_text(encoding="utf-8"))
    assert numbers["allocated"] == []  # no T-number minted


def test_backtest_drafts_are_not_resolver_gated(tmp_path):
    store = _store(tmp_path)
    draft = store.create_draft(**_draft_kwargs(trade_type="backtest", source_method="manual_backtest"))
    assert draft["state"] == "PRE_TRADE_DRAFT"


def _cli(tmp_path, *args):
    return subprocess.run(
        [sys.executable, "-m", "trading_os", *args], cwd=tmp_path, capture_output=True, text=True, env={**__import__("os").environ, "PYTHONPATH": str(ROOT)}
    )


def test_cli_live_draft_cannot_declare_a_strategy(tmp_path):
    _copy_operational_inputs(ROOT, tmp_path)
    common = [
        "capture", "draft", "--symbol", "BTCUSDT", "--direction", "long", "--entry", "100", "--sl", "95", "--tp", "110",
        "--planned-rr", "2", "--timeframe-context", "1h", "--timeframe-trigger", "15m", "--market-context", "m", "--thesis", "t",
        "--created-at", "2026-09-11T00:00:00Z",
    ]
    declared = _cli(tmp_path, *common, "--strategy-id", "stg-vegas-tunnel", "--rule-version", "stg-vegas-tunnel:v1")
    assert declared.returncode != 0 and "cannot declare" in (declared.stderr + declared.stdout)
    bare = _cli(tmp_path, *common)
    assert bare.returncode == 0, bare.stderr
    draft_result = json.loads(bare.stdout)
    draft = json.loads((tmp_path / "canonical/drafts" / f"{draft_result['draft_id']}.json").read_text(encoding="utf-8"))
    assert draft["strategy"]["strategy_id"] is None
    assert draft["strategy_resolution"]["status"] == "unresolved"
    ok = _cli(tmp_path, *common, "--strategy-observations-file", MATCHING_OBSERVATIONS_FILE)
    assert ok.returncode == 0, ok.stderr
    unknown = [*common]
    unknown[unknown.index("--entry") + 1] = "unknown"
    unknown[unknown.index("--sl") + 1] = "unknown"
    unknown[unknown.index("--tp") + 1] = "unknown"
    unknown[unknown.index("--planned-rr") + 1] = "unknown"
    partial = _cli(tmp_path, *unknown)
    assert partial.returncode == 0, partial.stderr
    partial_result = json.loads(partial.stdout)
    partial_draft = json.loads((tmp_path / "canonical/drafts" / f"{partial_result['draft_id']}.json").read_text(encoding="utf-8"))
    assert partial_draft["plan"]["completeness"] == "partial"


def _close(store, draft_id, trade_id, tag, r=-1.0, pnl=-10.0):
    ref = f"fixture://exit-{tag}"
    store.final_close(trade_id, exit_price=263.07, closed_at="2025-06-04T10:13:00+08:00", realized_r=r, provider_pnl=pnl, source_ref=ref)
    store.attach_evidence(
        draft_id,
        {"source_ref": ref, "timing": "post_trade", "type": "broker_fill", "category": "exit", "sha256": tag * 64, "captured_at": "2025-06-04T10:13:00+08:00"},
    )
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[ref], realized_r=r)


def test_no_match_is_in_owner_performance_but_not_named_strategy_stats(tmp_path):
    store = _store(tmp_path)
    refresh_analytics(tmp_path, generated_at="2026-10-05T00:00:00Z", source_commit="a" * 40)
    before = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    base_live, base_global = before["summary_by_mode"]["live"], before["metrics"]["global"]
    base_vegas = before["metrics"]["by_strategy"].get("stg-vegas-tunnel", {"count": 0, "total_r": 0.0})
    named = capture_retrospective_live(store, **_kwargs(matching_observations("stg-vegas-tunnel"), lifecycle_id="2506040949134235", sha="a"))
    off = capture_retrospective_live(store, **{**_kwargs(no_match_observations(), lifecycle_id="2506040953464365", sha="c"), "created_at": "2025-06-04T09:52:00+08:00"})
    _close(store, named["draft_id"], named["trade_id"], "b", r=2.0, pnl=20.0)
    _close(store, off["draft_id"], off["trade_id"], "d", r=-1.0, pnl=-10.0)

    refresh_analytics(tmp_path, generated_at="2026-10-06T00:00:00Z", source_commit="a" * 40)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    rows = {r["trade_id"]: r for r in analytics["canonical_read_model"]}
    off_row, named_row = rows[off["trade_id"]], rows[named["trade_id"]]

    assert off_row["performance_eligible"] is True and off_row["strategy_stats_eligible"] is False and off_row["counted"] is False
    assert named_row["performance_eligible"] is True and named_row["strategy_stats_eligible"] is True

    live = analytics["summary_by_mode"]["live"]
    # +2R named trade and the -1R off-strategy trade both move owner LIVE totals
    assert live["count"] == base_live["count"] + 2
    assert live["total_r"] == pytest.approx(base_live["total_r"] + 1.0, abs=1e-5)
    assert live["win_count"] == base_live["win_count"] + 1 and live["loss_count"] == base_live["loss_count"] + 1
    glob = analytics["metrics"]["global"]
    assert glob["count"] == base_global["count"] + 2 and glob["total_r"] == pytest.approx(base_global["total_r"] + 1.0, abs=1e-5)
    assert off["trade_id"] in {o["trade_id"] for o in analytics["performance_observations"]}

    # named-strategy views never see the NO_MATCH trade
    by_strategy = analytics["metrics"]["by_strategy"]
    assert "unknown" not in by_strategy
    # only the MATCHED trade (+2R) joined the Vegas metrics; the -1R NO_MATCH did not
    assert by_strategy["stg-vegas-tunnel"]["count"] == base_vegas["count"] + 1
    assert by_strategy["stg-vegas-tunnel"]["total_r"] == pytest.approx(base_vegas["total_r"] + 2.0, abs=1e-5)
    for sid in NAMED[1:]:
        assert by_strategy.get(sid, {"count": 0})["count"] == before["metrics"]["by_strategy"].get(sid, {"count": 0})["count"]
    assert off["trade_id"] not in {o["trade_id"] for o in analytics["observations"]}


def test_post_b3_active_frozen_strategy_counts_in_named_live_stats_without_changing_b3(tmp_path):
    store = _store(tmp_path)
    b3_manifest_path = tmp_path / "exports/method-validation/b3/manifest.json"
    b3_manifest_before = b3_manifest_path.read_bytes()
    b3 = json.loads(b3_manifest_before)
    assert "stg-example-a" not in b3["active_strategy_ids"]

    kwargs = _kwargs(matching_observations("stg-example-a"), lifecycle_id="aa-btc-stats-1")
    kwargs["symbol"] = "BTCUSDT"
    result = capture_retrospective_live(store, **kwargs)
    _close(store, result["draft_id"], result["trade_id"], "a")

    refresh_analytics(tmp_path, generated_at="2026-10-13T00:00:00Z", source_commit="c" * 40)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in analytics["canonical_read_model"] if item["trade_id"] == result["trade_id"])

    assert row["strategy_id"] == "stg-example-a" and row["rule_version"] == "stg-example-a:v2"
    assert row["performance_eligible"] is True
    assert row["counted"] is True and row["strategy_stats_eligible"] is True
    assert row["operational_method_eligible"] is False  # method qualification remains a separate gate
    assert analytics["metrics"]["by_strategy"]["stg-example-a"]["count"] == 1
    assert analytics["metrics"]["by_rule_version"]["stg-example-a:v2"]["count"] == 1
    assert analytics["named_strategy_summary_by_mode"]["stg-example-a"]["live"]["count"] == 1
    registry_pin = analytics["source_version_pins"]["named_strategy_registry"]
    assert "stg-example-a" in registry_pin["active_strategy_ids"]
    assert registry_pin["registered_rule_version_digests"]["stg-example-a"]["stg-example-a:v2"]
    assert b3_manifest_path.read_bytes() == b3_manifest_before


def test_retired_post_b3_strategy_stays_out_of_named_live_stats(tmp_path):
    store = _store(tmp_path)
    kwargs = _kwargs(matching_observations("stg-example-a"), lifecycle_id="aa-retired-stats-1")
    result = capture_retrospective_live(store, **kwargs)
    _close(store, result["draft_id"], result["trade_id"], "c")

    registry_path = tmp_path / "registry/strategy-registry.yaml"
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    aa = next(item for item in registry["strategies"] if item["strategy_id"] == "stg-example-a")
    aa["status"] = "retired"
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False, allow_unicode=True), encoding="utf-8")

    refresh_analytics(tmp_path, generated_at="2026-10-13T00:00:00Z", source_commit="d" * 40)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in analytics["canonical_read_model"] if item["trade_id"] == result["trade_id"])

    assert row["performance_eligible"] is True
    assert row["counted"] is False and row["strategy_stats_eligible"] is False
    assert row["exclusion_reason"] == "inactive_strategy"
    assert result["trade_id"] in {item["trade_id"] for item in analytics["performance_observations"]}
    assert result["trade_id"] not in {item["trade_id"] for item in analytics["observations"]}


def test_active_strategy_requires_the_trade_frozen_rule_version_pin(tmp_path):
    store = _store(tmp_path)
    kwargs = _kwargs(matching_observations("stg-example-a"), lifecycle_id="aa-unregistered-pin-1")
    result = capture_retrospective_live(store, **kwargs)
    _close(store, result["draft_id"], result["trade_id"], "e")

    registry_path = tmp_path / "registry/strategy-registry.yaml"
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    aa = next(item for item in registry["strategies"] if item["strategy_id"] == "stg-example-a")
    aa["rule_versions"] = []
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False, allow_unicode=True), encoding="utf-8")

    refresh_analytics(tmp_path, generated_at="2026-10-13T00:00:00Z", source_commit="e" * 40)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in analytics["canonical_read_model"] if item["trade_id"] == result["trade_id"])

    assert row["counted"] is False and row["strategy_stats_eligible"] is False
    assert row["exclusion_reason"] == "strategy_rule_version_unregistered"


def test_active_strategy_requires_a_frozen_rule_version(tmp_path):
    store = _store(tmp_path)
    kwargs = _kwargs(matching_observations("stg-example-a"), lifecycle_id="aa-unfrozen-pin-1")
    result = capture_retrospective_live(store, **kwargs)
    _close(store, result["draft_id"], result["trade_id"], "f")

    version_path = tmp_path / "registry/versions/stg-example-a-v2.yaml"
    version = yaml.safe_load(version_path.read_text(encoding="utf-8"))
    version.pop("schema_version")
    version_path.write_text(yaml.safe_dump(version, sort_keys=False, allow_unicode=True), encoding="utf-8")

    refresh_analytics(tmp_path, generated_at="2026-10-13T00:00:00Z", source_commit="f" * 40)
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    row = next(item for item in analytics["canonical_read_model"] if item["trade_id"] == result["trade_id"])

    assert row["counted"] is False and row["strategy_stats_eligible"] is False
    assert row["exclusion_reason"] == "strategy_rule_version_not_frozen"
