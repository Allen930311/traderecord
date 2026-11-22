from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from test_operational_closeout import _make_fixture
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore

ROOT = Path(__file__).resolve().parents[1]
CONFIRMED = "2026-11-10T02:45:00+08:00"
ENTRY = "2026-09-08T13:40:00+08:00"
EXIT = "2026-09-08T13:55:00+08:00"


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


def _backtest(tmp_path: Path) -> tuple[LifecycleStore, str]:
    """A finalized BACKTEST sample, built the same way as the missed-setup fixture."""
    _copy_registry(ROOT, tmp_path)
    store = LifecycleStore(tmp_path)
    draft_id = store.create_draft(
        strategy_id="stg-vegas-tunnel",
        rule_version="stg-vegas-tunnel:v1",
        symbol="NAS100",
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
        chart_refs=["fixture://chart"],
        created_at="2026-09-11T00:00:00Z",
        trade_type="backtest",
        source_method="live_market_unexecuted",
        outcome_known_at_recording=True,
    )["draft_id"]
    store.attach_evidence(draft_id, {
        "source_ref": "fixture://replay-chart", "timing": "pre_trade",
        "type": "chart_snapshot", "category": "entry", "sha256": "e" * 64,
    })
    store.confirm_backtest_entry(
        draft_id,
        actual_entry=100.0,
        execution_at="2026-09-11T00:01:00Z",
        run_id="run-1",
        parameters_ref="fixture://params/run-1",
        dataset_ref="fixture://dataset/nas100-5m",
        data_period={"from": "2026-01-01", "to": "2026-09-01"},
        source_ref="fixture://replay-chart",
    )
    trade_id = store.finalize(draft_id, occurred_at="2026-09-11T00:02:00Z")["trade_id"]
    return store, trade_id


def test_backtest_timing_is_recorded_and_projected(tmp_path: Path) -> None:
    store, trade_id = _backtest(tmp_path)
    before = copy.deepcopy(store.project()["records"][trade_id])
    event = store.record_backtest_timing(
        trade_id, entry_at=ENTRY, exit_at=EXIT, source_id="owner-chat-bt-timing", confirmed_at=CONFIRMED
    )
    assert event["event_type"] == "backtest_timing_recorded"
    record = store.project()["records"][trade_id]
    assert record["backtest_timing"]["entry_at"] == ENTRY
    assert record["backtest_timing"]["exit_at"] == EXIT
    assert record["backtest_timing"]["owner_identity"] == "example-owner"
    assert len(record["backtest_timing_events"]) == 1
    # Owner-stated replay times change only the timing fields, never prices or lifecycle state.
    changed = {k for k in set(before) | set(record) if before.get(k) != record.get(k)}
    assert changed <= {"backtest_timing", "backtest_timing_events", "lifecycle_event_ids"}, changed


def test_timing_is_refused_for_a_live_trade(tmp_path: Path) -> None:
    store, _, trade_id = _make_fixture(ROOT, tmp_path, write_review=False)
    with pytest.raises(LifecycleError, match="BACKTEST"):
        store.record_backtest_timing(
            trade_id, entry_at=ENTRY, exit_at=EXIT, source_id="live-timing", confirmed_at=CONFIRMED
        )


def test_timing_requires_an_existing_trade(tmp_path: Path) -> None:
    store, _ = _backtest(tmp_path)
    with pytest.raises(LifecycleError, match="existing canonical trade"):
        store.record_backtest_timing(
            "trd_missing", entry_at=ENTRY, exit_at=EXIT, source_id="missing", confirmed_at=CONFIRMED
        )


@pytest.mark.parametrize("entry_at, exit_at", [
    ("2026-09-08T13:40:00", EXIT),          # naive entry
    (ENTRY, "2026-09-08T13:55:00"),          # naive exit
    ("not-a-time", EXIT),
    (None, EXIT),
])
def test_naive_or_invalid_timestamps_are_rejected(tmp_path: Path, entry_at, exit_at) -> None:
    store, trade_id = _backtest(tmp_path)
    with pytest.raises(LifecycleError, match="UTC offset|ISO timestamp"):
        store.record_backtest_timing(
            trade_id, entry_at=entry_at, exit_at=exit_at, source_id="bad-time", confirmed_at=CONFIRMED
        )


def test_exit_before_entry_is_rejected(tmp_path: Path) -> None:
    store, trade_id = _backtest(tmp_path)
    with pytest.raises(LifecycleError, match="precede"):
        store.record_backtest_timing(
            trade_id, entry_at=EXIT, exit_at=ENTRY, source_id="reversed", confirmed_at=CONFIRMED
        )


@pytest.mark.parametrize("bad", [None, "", "not-a-time"])
def test_owner_confirmation_time_is_required(tmp_path: Path, bad) -> None:
    store, trade_id = _backtest(tmp_path)
    with pytest.raises(LifecycleError, match="confirmed_at"):
        store.record_backtest_timing(
            trade_id, entry_at=ENTRY, exit_at=EXIT, source_id="no-attest", confirmed_at=bad
        )


def test_retry_is_idempotent_and_changed_content_fails_closed(tmp_path: Path) -> None:
    store, trade_id = _backtest(tmp_path)
    first = store.record_backtest_timing(
        trade_id, entry_at=ENTRY, exit_at=EXIT, source_id="retry-timing", confirmed_at=CONFIRMED
    )
    again = store.record_backtest_timing(
        trade_id, entry_at=ENTRY, exit_at=EXIT, source_id="retry-timing", confirmed_at=CONFIRMED
    )
    assert again["event_id"] == first["event_id"]
    assert len(store.project()["records"][trade_id]["backtest_timing_events"]) == 1
    with pytest.raises(LifecycleError, match="changed content"):
        store.record_backtest_timing(
            trade_id, entry_at=ENTRY, exit_at="2026-09-08T13:58:00+08:00",
            source_id="retry-timing", confirmed_at=CONFIRMED,
        )


def test_a_forged_timing_event_without_attestation_is_refused(tmp_path: Path) -> None:
    store, trade_id = _backtest(tmp_path)
    good = store.record_backtest_timing(
        trade_id, entry_at=ENTRY, exit_at=EXIT, source_id="forge-base", confirmed_at=CONFIRMED
    )
    forged = copy.deepcopy(good["payload"])
    forged["source_id"] = "forged"
    forged["authority"] = "none"
    with pytest.raises(LifecycleError):
        store.append("backtest_timing_recorded", forged, idempotency_key="backtest-timing:forged")


def test_backtest_rule_version_amendment_is_append_only_and_registry_bound(tmp_path):
    store, trade_id = _backtest(tmp_path)
    row = store.current()["records"][trade_id]
    strategy_id = row["strategy_id"]
    with pytest.raises(LifecycleError, match="registered for the trade's own strategy"):
        store.amend_backtest_rule_version(trade_id, rule_version=f"{strategy_id}:v999", source_id="amend-1", reason="x", confirmed_at=CONFIRMED)
    registry = json.loads(json.dumps(__import__("trading_os.registry", fromlist=["load_registry"]).load_registry(tmp_path)))
    entry = next(s for s in registry["strategies"] if s["strategy_id"] == strategy_id)
    version = entry["rule_versions"][-1]
    event = store.amend_backtest_rule_version(trade_id, rule_version=version, source_id="amend-1", reason="Owner re-pin", confirmed_at=CONFIRMED)
    assert store.amend_backtest_rule_version(trade_id, rule_version=version, source_id="amend-1", reason="Owner re-pin", confirmed_at=CONFIRMED) == event
    after = store.current()["records"][trade_id]
    assert after["rule_version"] == version and after["rule_version_amendments"][-1]["reason"] == "Owner re-pin"
