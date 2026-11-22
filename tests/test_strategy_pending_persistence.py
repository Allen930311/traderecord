"""Legacy STRATEGY_PENDING rows remain readable and Census-compatible."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from resolver_helpers import matching_observations
from test_historical_unresolved_live import _copy_operational_inputs
from test_registration_resolver_gate import _kwargs
from trading_os.operational_lifecycle import LifecycleStore
from trading_os.registration_census import build_census, select_next
from trading_os.registration_pending import load_pending, pending_path, record_pending
from trading_os.strategy_resolver import resolve_strategy
from trading_os.unified_capture import capture_retrospective_live

ROOT = Path(__file__).resolve().parents[1]
FIRST = "2506040949134235"
SECOND = "2506040953464365"


def _setup(tmp_path):
    _copy_operational_inputs(ROOT, tmp_path)
    ledger = tmp_path / "provider-ledgers" / "propw" / "normalized"
    ledger.mkdir(parents=True)
    rows = [
        {"open_id": oid, "lifecycle": {"open_id": oid, "instrument": "AAVE", "direction": "long", "entry_time": t, "entry_price": "100", "exit_time": t, "weighted_exit_price": "101", "open_size": "1", "closed_size": "1", "is_closed": 1, "is_partial": 0, "normalization_status": "ok", "provider_lifecycle_phase": "CLOSED"}}
        for oid, t in ((FIRST, "2025-06-04T01:49:13Z"), (SECOND, "2025-06-04T01:53:46Z"))
    ]
    (ledger / "trade-lifecycles.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return LifecycleStore(tmp_path)


def _status(tmp_path, oid):
    return {r["open_id"]: r for r in build_census(tmp_path, "propw")["lifecycles"]}[oid]


def _ambiguous():
    obs = matching_observations("stg-vegas-tunnel")
    obs["observations"].append({"rule_id": "po3.identity", "state": "PASS", "timing": "pre_trade", "evidence_refs": ["x"]})
    return obs


def _legacy_pending(store, oid, observations):
    result = resolve_strategy(store.root, observations)
    return record_pending(
        store,
        provider="propw",
        provider_lifecycle_id=oid,
        instrument="AAVEUSDT",
        direction="long",
        provider_execution_timestamp="2025-06-04T01:49:13Z" if oid == FIRST else "2025-06-04T01:53:46Z",
        result=result,
        observations=observations,
    )


def test_legacy_pending_is_read_and_census_stops_treating_it_as_unregistered(tmp_path):
    store = _setup(tmp_path)
    assert _status(tmp_path, FIRST)["status"] == "UNREGISTERED"

    record = _legacy_pending(store, FIRST, {})
    assert record["resolver_status"] == "INSUFFICIENT_EVIDENCE" and record["attempt"] == 1
    assert record["provider_lifecycle_key"] == f"propw:{FIRST}"

    # durable, committed-shape file; still no draft / identity link / T-number
    assert pending_path(tmp_path).exists() and load_pending(tmp_path)[f"propw:{FIRST}"]["digest"] == record["digest"]
    assert not (tmp_path / "canonical" / "lifecycle" / "identity-links.jsonl").exists()
    assert json.loads((tmp_path / "canonical/sequences/numbers.json").read_text(encoding="utf-8"))["allocated"] == []

    row = _status(tmp_path, FIRST)
    assert row["status"] == "STRATEGY_PENDING" and row["pending"]["attempt"] == 1 and "INSUFFICIENT_EVIDENCE" in row["detail"]
    census = build_census(tmp_path, "propw")
    assert census["counts"]["STRATEGY_PENDING"] == 1 and census["counts"]["UNREGISTERED"] == 1
    assert census["strategy_pending"] == [f"propw:{FIRST}"]


def test_next_never_reselects_a_legacy_pending_lifecycle(tmp_path):
    store = _setup(tmp_path)
    census = build_census(tmp_path, "propw")
    assert select_next(census)["open_id"] == FIRST  # earliest closed UNREGISTERED

    _legacy_pending(store, FIRST, {})
    for _ in range(3):  # repeating "next" is stable and never returns the pending trade
        assert select_next(build_census(tmp_path, "propw"))["open_id"] == SECOND
    assert select_next(build_census(tmp_path, "propw"), instrument="BTC") is None


def test_pending_record_is_idempotent_and_versions_on_change(tmp_path):
    store = _setup(tmp_path)
    _legacy_pending(store, FIRST, {})
    _legacy_pending(store, FIRST, {})  # same observations -> no new line
    assert len(pending_path(tmp_path).read_text(encoding="utf-8").splitlines()) == 1

    record = _legacy_pending(store, FIRST, _ambiguous())
    assert record["resolver_status"] == "AMBIGUOUS" and record["attempt"] == 2
    lines = pending_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # append-only history
    assert _status(tmp_path, FIRST)["pending"]["resolver_status"] == "AMBIGUOUS"


def test_resolving_later_registers_and_supersedes_the_pending_record(tmp_path):
    store = _setup(tmp_path)
    _legacy_pending(store, FIRST, {})
    assert _status(tmp_path, FIRST)["status"] == "STRATEGY_PENDING"

    done = capture_retrospective_live(store, **_kwargs(matching_observations("stg-vegas-tunnel"), lifecycle_id=FIRST))
    assert done["registered"] is True and done["registration_status"] == "REGISTRATION_COMPLETE"
    row = _status(tmp_path, FIRST)
    assert row["status"] == "REGISTERED_INCOMPLETE" and row["trade_no"] == done["trade_no"]  # registration wins over pending
    assert build_census(tmp_path, "propw")["strategy_pending"] == []

    again = capture_retrospective_live(store, **_kwargs(_ambiguous(), lifecycle_id=FIRST))  # re-run: original T-ID, no new pending
    assert again["already_registered"] is True and again["trade_no"] == done["trade_no"]
    assert len(pending_path(tmp_path).read_text(encoding="utf-8").splitlines()) == 1


def test_only_unresolved_results_can_be_recorded_as_pending(tmp_path):
    from trading_os.operational_lifecycle import LifecycleError
    from trading_os.registration_pending import record_pending
    from trading_os.strategy_resolver import resolve_strategy

    store = _setup(tmp_path)
    matched = resolve_strategy(ROOT, {**matching_observations("stg-vegas-tunnel"), "plan": {"stop_loss_price": 1.0, "target_price": 2.0}})
    with pytest.raises(LifecycleError):
        record_pending(store, provider="propw", provider_lifecycle_id=FIRST, instrument="AAVE", direction="long",
                       provider_execution_timestamp="2025-06-04T01:49:13Z", result=matched, observations={})
