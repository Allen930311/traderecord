"""Provider P&L after the open fee and signed funding (PropW netProfit excludes both)."""
import json
from pathlib import Path

import pytest

from trading_os.operational_analytics import (
    PROVIDER_BILL_EVENTS_DIR,
    PROVIDER_LEDGER_PATH,
    PROVIDER_TRADE_EVENTS_DIR,
    _metrics,
    _provider_net_pnl,
    provider_fee_index,
)

REPO = Path(__file__).resolve().parents[1]
H = 3600 * 1000
# 2025-06-04 01:00 UTC; held across the 08:00 and 16:00 UTC settlements.
OPEN_MS = 1748998800000
CLOSE_MS = OPEN_MS + 17 * H


def _iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jsonl(path, payloads):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"event_id": p.get("id"), "payload": p}, ensure_ascii=False) + "\n"
                            for p in payloads), encoding="utf-8")


def _funding(bill_id, position, amount, at):
    return {"id": bill_id, "type": "Funding Fee", "size": str(amount), "createdDate": at, "instrument": "AAVE",
            "note": f"结算资金费率,逐仓,仓位id:{position},金额:{abs(amount)}"}


def _fixture(root, funding_bills, *, open_fee_bill=-0.89306, net_profit="-16.14"):
    ledger = root / PROVIDER_LEDGER_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"open_id": "1001", "lifecycle": {
        "entry_time": _iso(OPEN_MS), "exit_time": _iso(CLOSE_MS), "net_profit_sum": net_profit,
    }}) + "\n", encoding="utf-8")
    _jsonl(root / PROVIDER_TRADE_EVENTS_DIR / "2025-06.jsonl", [
        {"id": "e1", "openId": "1001", "status": "open", "instrument": "AAVE", "createdDate": OPEN_MS, "fee": 0.8931},
        {"id": "e2", "openId": "1001", "status": "close", "instrument": "AAVE", "createdDate": CLOSE_MS, "fee": 0.88},
    ])
    bills = list(funding_bills)
    if open_fee_bill is not None:
        bills.append({"id": "of1", "type": "Open Fee", "size": str(open_fee_bill), "instrument": "AAVE", "createdDate": OPEN_MS})
    _jsonl(root / PROVIDER_BILL_EVENTS_DIR / "2025-06.jsonl", bills)


FULL_FUNDING = [_funding("f1", "1001", -0.40, OPEN_MS + 7 * H), _funding("f2", "1001", 0.15, OPEN_MS + 15 * H)]


def test_open_fee_and_signed_funding_are_attributed_exactly(tmp_path):
    _fixture(tmp_path, FULL_FUNDING + [_funding("f1", "1001", -0.40, OPEN_MS + 7 * H)])  # f1 repeated = one bill
    index, pin = provider_fee_index(tmp_path)
    assert index["1001"] == {"net_profit_sum": -16.14, "open_fee": pytest.approx(0.89306), "open_fee_source": "open_fee_bill",
                             "funding": pytest.approx(-0.25), "funding_complete": True,
                             "funding_missing_settlements": 0}
    assert pin["unattributed_funding_bills"] == 0 and pin["bill_events_sha256"] and pin["trade_events_sha256"]


def test_a_settlement_without_a_bill_is_disclosed_not_presented_as_complete(tmp_path):
    """Codex: a present-but-truncated bill month must not pass as proven funding."""
    _fixture(tmp_path, FULL_FUNDING[:1])
    fees = provider_fee_index(tmp_path)[0]["1001"]
    assert fees["funding"] == pytest.approx(-0.40) and fees["funding_complete"] is False
    assert fees["funding_missing_settlements"] == 1
    _fixture(tmp_path, [])
    fees = provider_fee_index(tmp_path)[0]["1001"]
    assert fees["funding"] == 0.0 and fees["funding_complete"] is False
    assert fees["funding_missing_settlements"] == 2


def test_no_settlement_crossed_means_funding_is_proven_zero(tmp_path):
    _fixture(tmp_path, [])
    ledger = tmp_path / PROVIDER_LEDGER_PATH
    row = json.loads(ledger.read_text(encoding="utf-8"))
    row["lifecycle"]["exit_time"] = _iso(OPEN_MS + 2 * H)
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
    fees = provider_fee_index(tmp_path)[0]["1001"]
    assert fees["funding"] == 0.0 and fees["funding_complete"] is True


@pytest.mark.parametrize("bill", [
    _funding("fx", "1001", -0.1, CLOSE_MS + 9 * H),
    {"id": "fx", "type": "Funding Fee", "size": "-0.1", "createdDate": OPEN_MS + 7 * H, "note": "no position"},
])
def test_funding_outside_the_hold_or_unattributed_makes_funding_unknown(tmp_path, bill):
    _fixture(tmp_path, FULL_FUNDING + [bill])
    assert provider_fee_index(tmp_path)[0]["1001"]["funding"] is None


def test_a_bill_that_disagrees_with_the_event_fee_leaves_the_open_fee_unknown(tmp_path):
    _fixture(tmp_path, FULL_FUNDING, open_fee_bill=-0.95)
    assert provider_fee_index(tmp_path)[0]["1001"]["open_fee"] is None


def test_without_an_open_fee_bill_the_trade_event_fee_is_used_and_labelled(tmp_path):
    _fixture(tmp_path, FULL_FUNDING, open_fee_bill=None)
    fees = provider_fee_index(tmp_path)[0]["1001"]
    assert fees["open_fee"] == 0.8931 and fees["open_fee_source"] == "trade_event_fee"


def test_missing_provider_files_leave_fees_unknown(tmp_path):
    index, pin = provider_fee_index(tmp_path)
    assert index == {} and pin == {"normalized_ledger_sha256": None, "trade_events_sha256": None,
                                   "bill_events_sha256": None, "unattributed_funding_bills": 0,
                                   "untrusted_trade_event_rows": 0, "untrusted_bill_rows": 0,
                                   "mean_abs_funding_bill": None}


def test_bill_change_changes_the_source_pin(tmp_path):
    _fixture(tmp_path, FULL_FUNDING)
    before = provider_fee_index(tmp_path)[1]
    _fixture(tmp_path, FULL_FUNDING[:1])
    assert provider_fee_index(tmp_path)[1] != before


FEES = {"net_profit_sum": 10.0, "open_fee": 1.0, "open_fee_source": "open_fee_bill", "funding": -0.5, "funding_complete": True}


def test_net_pnl_requires_verified_reconciled_pnl_and_every_fee():
    assert _provider_net_pnl(10.0, True, FEES) == {
        "provider_open_fee": 1.0, "provider_open_fee_source": "open_fee_bill",
        "provider_funding_fee": -0.5, "provider_funding_complete": True,
        "provider_funding_missing_settlements": None,
        "provider_net_pnl": 8.5, "provider_net_pnl_complete": True,
        "provider_net_pnl_funding_reconciled": True,
    }
    gap = _provider_net_pnl(10.0, True, {**FEES, "funding_complete": False})
    assert gap["provider_net_pnl_complete"] is True and gap["provider_net_pnl_funding_reconciled"] is False
    assert _provider_net_pnl(10.0, False, FEES)["provider_net_pnl"] is None
    assert _provider_net_pnl(10.0, True, {**FEES, "funding": None})["provider_net_pnl_complete"] is False
    assert _provider_net_pnl(10.0, True, None)["provider_net_pnl"] is None


def test_canonical_pnl_that_no_longer_matches_the_ledger_is_not_combined_with_fresh_fees():
    """Codex: a later sync of the same position must not mix stale canonical P&L with new fees."""
    assert _provider_net_pnl(10.0, True, {**FEES, "net_profit_sum": 12.0})["provider_net_pnl_complete"] is False


def test_metrics_sum_net_only_over_complete_rows_and_report_the_gap():
    base = {"counted": True, "realized_r": 1.0, "provider_pnl_verified": True, "event_date": "2026-01-01"}
    rows = [
        {**base, "provider_pnl": 10.0, **_provider_net_pnl(10.0, True, FEES)},
        {**base, "provider_pnl": -4.0, **_provider_net_pnl(-4.0, True, {"net_profit_sum": -4.0, "open_fee": 2.0, "funding": 0.25, "funding_complete": False})},
        {**base, "provider_pnl": 3.0, **_provider_net_pnl(3.0, True, {"net_profit_sum": 3.0, "open_fee": 1.0, "funding": None})},
    ]
    metrics = _metrics(rows, {})
    assert metrics["verified_provider_pnl"] == 9.0
    assert metrics["verified_provider_net_pnl"] == 2.75
    assert metrics["provider_net_pnl_observation_count"] == 2
    assert metrics["provider_net_pnl_incomplete_count"] == 1
    assert metrics["provider_open_fee_total"] == 3.0
    assert metrics["provider_funding_fee_total"] == -0.25
    assert metrics["provider_net_pnl_funding_gap_count"] == 1
    assert metrics["provider_net_pnl_funding_missing_settlements"] == 0  # rows here carry no count
    # Only the fully reconciled trade (+8.5) is in the strictest figure.
    assert metrics["provider_net_pnl_funding_reconciled_count"] == 1
    assert metrics["verified_provider_net_pnl_funding_reconciled"] == 8.5


def test_committed_ledger_reconciles_with_propw_bills_and_canonical_pnl():
    """Real data: proven open fees equal their bills, and every counted LIVE P&L equals the ledger's."""
    if not (REPO / PROVIDER_BILL_EVENTS_DIR).is_dir():
        pytest.skip("no committed PropW bill ledger")
    index, pin = provider_fee_index(REPO)
    assert pin["unattributed_funding_bills"] == 0
    assert all(item["open_fee"] is not None for item in index.values())
    # The 2026-10-20 provider update added two positions without a separate
    # Open Fee bill. Keep the event-fee fallback identities and amounts exact
    # so later missing bills cannot hide behind a widened count tolerance.
    event_fee_fallbacks = {
        open_id: (item["open_fee_source"], item["open_fee"])
        for open_id, item in index.items()
        if item["open_fee_source"] != "open_fee_bill"
    }
    assert event_fee_fallbacks == {
        "2506161010102823": ("trade_event_fee", 10.34),
        "2509191026044109": ("trade_event_fee", 6.7297),
        "2610161221511768": ("trade_event_fee", 1.6394),
        "2610191130343384": ("trade_event_fee", 3.9907),
        # Owner-confirmed: positions with only a trade_event_fee and no missing Open Fee bill.
        "2610261134461739": ("trade_event_fee", 2.3171),
        "2610291319257574": ("trade_event_fee", 1.4979),
    }
    current = json.loads((REPO / "canonical/lifecycle/current.json").read_text(encoding="utf-8"))["records"]
    for record in current.values():
        if record.get("trade_type") == "live" and record.get("provider_pnl") is not None:
            fees = index[(record.get("identity_link") or {})["provider_lifecycle_id"]]
            assert fees["net_profit_sum"] == pytest.approx(float(record["provider_pnl"]), abs=1e-6)


def _two_positions(root, extra_bills=(), open_fee_rows=None):
    """Two AAVE/ETH positions held across the same settlements, each with its own funding."""
    ledger = root / PROVIDER_LEDGER_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(json.dumps({"open_id": open_id, "lifecycle": {
        "instrument": instrument, "entry_time": _iso(OPEN_MS), "exit_time": _iso(CLOSE_MS), "net_profit_sum": "1.0"}}) + "\n"
        for open_id, instrument in (("1001", "AAVE"), ("1002", "ETH"))), encoding="utf-8")
    _jsonl(root / PROVIDER_TRADE_EVENTS_DIR / "2025-06.jsonl", [
        {"id": f"e{open_id}", "openId": open_id, "status": "open", "instrument": instrument, "createdDate": OPEN_MS, "fee": 0.5}
        for open_id, instrument in (("1001", "AAVE"), ("1002", "ETH"))])
    bills = [
        _funding("fa1", "1001", -0.1, OPEN_MS + 7 * H), _funding("fa2", "1001", -0.1, OPEN_MS + 15 * H),
        {**_funding("fe1", "1002", -0.2, OPEN_MS + 7 * H), "instrument": "ETH"},
        {**_funding("fe2", "1002", -0.2, OPEN_MS + 15 * H), "instrument": "ETH"},
    ]
    rows = open_fee_rows if open_fee_rows is not None else [
        {"id": "ofa", "type": "Open Fee", "size": "-0.5", "instrument": "AAVE", "createdDate": OPEN_MS},
        {"id": "ofe", "type": "Open Fee", "size": "-0.5", "instrument": "ETH", "createdDate": OPEN_MS}]
    _jsonl(root / PROVIDER_BILL_EVENTS_DIR / "2025-06.jsonl", bills + list(extra_bills) + rows)


def test_one_unattributed_funding_bill_taints_only_the_positions_it_could_belong_to(tmp_path):
    """Codex round 2: a stray funding bill must not mark unrelated valid positions incomplete."""
    stray = {"id": "fx", "type": "Funding Fee", "size": "-0.3", "instrument": "ETH", "createdDate": OPEN_MS + 7 * H, "note": "no position"}
    _two_positions(tmp_path, [stray])
    index, pin = provider_fee_index(tmp_path)
    assert pin["unattributed_funding_bills"] == 1
    assert index["1002"]["funding"] is None                     # same instrument, inside the hold
    assert index["1001"]["funding"] == pytest.approx(-0.2)      # unrelated AAVE position unaffected
    assert index["1001"]["funding_complete"] is True


def test_an_unattributed_bill_with_no_instrument_or_time_could_be_any_position(tmp_path):
    stray = {"id": "fx", "type": "Funding Fee", "size": "-0.3", "note": "no position"}
    _two_positions(tmp_path, [stray])
    index, _ = provider_fee_index(tmp_path)
    assert index["1001"]["funding"] is None and index["1002"]["funding"] is None


def test_a_malformed_open_fee_row_is_ambiguous_not_absent(tmp_path):
    """Codex round 2: a matching but unparseable Open Fee row must not fall back to the event fee."""
    rows = [{"id": "ofa", "type": "Open Fee", "size": "garbage", "instrument": "AAVE", "createdDate": OPEN_MS},
            {"id": "ofe", "type": "Open Fee", "size": "-0.5", "instrument": "ETH", "createdDate": OPEN_MS}]
    _two_positions(tmp_path, open_fee_rows=rows)
    index, _ = provider_fee_index(tmp_path)
    assert index["1001"]["open_fee"] is None and index["1001"]["open_fee_source"] is None
    assert index["1002"]["open_fee"] == 0.5 and index["1002"]["open_fee_source"] == "open_fee_bill"


def test_a_duplicated_open_fee_row_is_ambiguous(tmp_path):
    rows = [{"id": "ofa", "type": "Open Fee", "size": "-0.5", "instrument": "AAVE", "createdDate": OPEN_MS},
            {"id": "ofa2", "type": "Open Fee", "size": "-0.5", "instrument": "AAVE", "createdDate": OPEN_MS}]
    _two_positions(tmp_path, open_fee_rows=rows)
    assert provider_fee_index(tmp_path)[0]["1001"]["open_fee"] is None


def test_github_analyze_receipt_carries_the_funding_gap_count(tmp_path, monkeypatch):
    """Codex round 2: the response must not present the net figure without its gap warning."""
    from trading_os import github_agent_runner as runner
    from trading_os import projections

    live = {"count": 51, "win_rate": 0.4, "expectancy_r": 0.1, "total_r": 5.0,
            "verified_provider_pnl": 168.6, "provider_pnl_observation_count": 51,
            "verified_provider_net_pnl": 34.6, "provider_net_pnl_observation_count": 51,
            "provider_net_pnl_incomplete_count": 0, "provider_net_pnl_funding_gap_count": 7,
            "provider_open_fee_total": 131.7, "provider_funding_fee_total": -2.3}
    for rel, payload in (("exports/operational/trade-view/trade-view-data.json", {"summary_by_mode": {"live": live}}),
                         ("exports/operational/analytics/manifest.json", {"canonical_lifecycle_events_sha256": "x"}),
                         ("exports/operational/health/current.json", {})):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "git", lambda *a, **k: "deadbeef")
    monkeypatch.setattr(projections, "verify_projections", lambda *a, **k: {"status": "CURRENT"})
    receipt = runner._analyze({"operation": "analyze", "analysis_mode": "performance", "population": "live", "strategy_id": None})
    assert receipt["summary"]["provider_net_pnl_funding_gap_count"] == 7
    assert receipt["summary"]["verified_provider_net_pnl"] == 34.6


def test_two_opens_at_one_instrument_and_instant_cannot_share_one_open_fee_bill(tmp_path):
    """Reviewer P1: one bill must never be attributed to two positions."""
    _two_positions(tmp_path)
    events = tmp_path / PROVIDER_TRADE_EVENTS_DIR / "2025-06.jsonl"
    rows = [json.loads(line)["payload"] for line in events.read_text(encoding="utf-8").splitlines()]
    rows[1]["instrument"] = "AAVE"  # both positions now open on AAVE at the same instant; one bill exists
    _jsonl(events, rows)
    index, _ = provider_fee_index(tmp_path)
    assert index["1001"]["open_fee"] is None and index["1002"]["open_fee"] is None


def test_conflicting_or_idless_rows_are_reported_not_overwritten(tmp_path):
    """Reviewer P2: same id with different content, and rows with no id, are untrusted facts."""
    _two_positions(tmp_path)
    bills = tmp_path / PROVIDER_BILL_EVENTS_DIR / "2025-06.jsonl"
    rows = [json.loads(line)["payload"] for line in bills.read_text(encoding="utf-8").splitlines()]
    conflicting = {**next(r for r in rows if r["id"] == "ofa"), "size": "-9.0"}  # same id, other amount
    idless = {"type": "Funding Fee", "size": "-0.1", "instrument": "ETH", "createdDate": OPEN_MS + 7 * H, "note": "x"}
    _jsonl(bills, rows + [conflicting, idless])
    index, pin = provider_fee_index(tmp_path)
    assert pin["untrusted_bill_rows"] == 3  # both versions of ofa + the id-less row
    assert index["1001"]["open_fee"] is None                      # ofa is now ambiguous, not "last one wins"
    assert index["1002"]["funding"] is None                       # the id-less ETH funding bill taints ETH
    assert index["1001"]["funding"] == pytest.approx(-0.2)        # unrelated AAVE funding unaffected


def test_identical_repeats_of_one_row_across_sync_files_are_still_one_row(tmp_path):
    _two_positions(tmp_path)
    bills = tmp_path / PROVIDER_BILL_EVENTS_DIR
    (bills / "2025-07.jsonl").write_text((bills / "2025-06.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    index, pin = provider_fee_index(tmp_path)
    assert pin["untrusted_bill_rows"] == 0
    assert index["1001"]["funding"] == pytest.approx(-0.2) and index["1001"]["open_fee"] == 0.5
