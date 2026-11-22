"""Owner-confirmed chart exit closes a BACKTEST sample and makes it countable."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_backtest_registration import APPROVAL, _fixture
from trading_os.backtest_exit import BacktestExitError, commit_backtest_exit, preview_backtest_exit
from trading_os.backtest_registration import commit_backtest_registration, preview_backtest_registration
from trading_os.operational_lifecycle import LifecycleStore

ENTRY_AT = "2026-09-08T13:40:00+08:00"
EXIT_AT = "2026-09-08T13:55:00+08:00"


def _registered(tmp_path: Path):
    root, intake = _fixture(tmp_path)
    intake["strategy"]["rule_version"] = "stg-example-a:v2"
    intake["replay_timing"] = {"entry_at": ENTRY_AT, "exit_at": EXIT_AT, "source": "owner_stated"}
    intake["simulated_entry_at"] = ENTRY_AT
    digest = preview_backtest_registration(root, intake)["preview_digest"]
    result = commit_backtest_registration(root, intake, preview_digest=digest, approval=APPROVAL)
    row = next(r for r in LifecycleStore(root).current()["records"].values() if r["trade_no"] == result["trade_no"])
    exit_ref = next(e["locator"] for e in row["evidence"] if e.get("original_filename") == "exit.png")
    return root, result["trade_no"], exit_ref


def _args(trade_no, exit_ref, **over):
    base = dict(trade_no=trade_no, exit_price=110.0, closed_at=EXIT_AT, exit_evidence_ref=exit_ref)
    base.update(over)
    return base


def _analytics_row(root, trade_no):
    path = root / "exports/operational/analytics/current.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [o for k in ("observations", "performance_observations", "canonical_read_model") for o in data.get(k, []) if o.get("trade_no") == trade_no]
    assert rows, f"{trade_no} missing from analytics"
    return rows[0]


def test_registered_backtest_is_open_and_uncounted_before_exit(tmp_path):
    root, trade_no, _ = _registered(tmp_path)
    row = _analytics_row(root, trade_no)
    assert row["performance_eligible"] is False and row["exclusion_reason"] == "exit_unverified"


def test_preview_derives_r_and_is_read_only(tmp_path):
    root, trade_no, ref = _registered(tmp_path)
    before = (root / "canonical/lifecycle/events.jsonl").read_bytes()
    preview = preview_backtest_exit(root, **_args(trade_no, ref))
    assert preview["status"] == "READY_FOR_OWNER_APPROVAL"
    assert preview["content"]["realized_r"] == "2.000000"  # (110-100)/(100-95)
    assert preview["content"]["basis"] == "owner_chart_judgment_not_provider_fill"
    assert (root / "canonical/lifecycle/events.jsonl").read_bytes() == before
    assert preview_backtest_exit(root, **_args(trade_no, ref, exit_price=95.0))["content"]["realized_r"] == "-1.000000"


def test_commit_closes_verifies_and_counts(tmp_path):
    root, trade_no, ref = _registered(tmp_path)
    preview = preview_backtest_exit(root, **_args(trade_no, ref))
    result = commit_backtest_exit(root, **_args(trade_no, ref), preview_digest=preview["preview_digest"], approval=APPROVAL)
    assert result["status"] == "EXIT_RECORDED"
    assert result["exit_basis"] == "owner_attestation" and result["exit_verification"] == "confirmed"
    row = _analytics_row(root, trade_no)
    assert row["performance_eligible"] is True, row.get("exclusion_reason")
    assert float(row["realized_r"]) == 2.0
    # an identical retry writes nothing
    again = preview_backtest_exit(root, **_args(trade_no, ref))
    assert again["status"] == "ALREADY_VERIFIED"


def test_commit_needs_matching_digest_and_owner_approval(tmp_path):
    root, trade_no, ref = _registered(tmp_path)
    preview = preview_backtest_exit(root, **_args(trade_no, ref))
    with pytest.raises(BacktestExitError, match="approval"):
        commit_backtest_exit(root, **_args(trade_no, ref), preview_digest=preview["preview_digest"], approval={})
    with pytest.raises(BacktestExitError, match="changed since"):
        commit_backtest_exit(root, **_args(trade_no, ref, exit_price=108.0), preview_digest=preview["preview_digest"], approval=APPROVAL)


def test_rejects_wrong_time_unsaved_evidence_and_r_mismatch(tmp_path):
    root, trade_no, ref = _registered(tmp_path)
    with pytest.raises(BacktestExitError, match="differs from the Owner-confirmed"):
        preview_backtest_exit(root, **_args(trade_no, ref, closed_at="2026-09-08T14:30:00+08:00"))
    with pytest.raises(BacktestExitError, match="already saved"):
        preview_backtest_exit(root, **_args(trade_no, "artifact:" + "0" * 64 + ".png"))
    with pytest.raises(BacktestExitError, match="disagrees"):
        preview_backtest_exit(root, **_args(trade_no, ref, realized_r=5))


def test_entry_time_without_offset_is_owner_local_time():
    from trading_os.backtest_exit import _entry_time
    assert _entry_time("2026-09-03T13:05:00").isoformat() == "2026-09-03T13:05:00+08:00"
    assert _entry_time("2026-09-02 13:30").isoformat() == "2026-09-02T13:30:00+08:00"
    assert _entry_time("2026-09-08T13:40:00+08:00").isoformat() == "2026-09-08T13:40:00+08:00"


def _intake_with_exit(tmp_path: Path, **exit_over):
    root, intake = _fixture(tmp_path)
    intake["strategy"]["rule_version"] = "stg-example-a:v2"
    intake["replay_timing"] = {"entry_at": ENTRY_AT, "exit_at": EXIT_AT, "source": "owner_stated"}
    intake["simulated_entry_at"] = ENTRY_AT
    intake["exit"] = {"price": 110.0, "closed_at": EXIT_AT, **exit_over}
    return root, intake


def test_registration_with_exit_closes_and_counts_in_one_approval(tmp_path):
    root, intake = _intake_with_exit(tmp_path)
    preview = preview_backtest_registration(root, intake)
    assert preview["content"]["exit"]["realized_r"] == "2.000000"
    assert preview["result_counted_after_commit"] is True
    result = commit_backtest_registration(root, intake, preview_digest=preview["preview_digest"], approval=APPROVAL)
    assert result["status"] == "REGISTERED" and result["result_counted"] is True and result["state"] == "CLOSED"
    row = _analytics_row(root, result["trade_no"])
    assert row["performance_eligible"] is True, row.get("exclusion_reason")
    assert float(row["realized_r"]) == 2.0
    # identical retry writes nothing
    events = (root / "canonical/lifecycle/events.jsonl").read_bytes()
    again = commit_backtest_registration(root, intake, preview_digest=preview["preview_digest"], approval=APPROVAL)
    assert again["status"] == "ALREADY_REGISTERED" and again["result_counted"] is True
    assert (root / "canonical/lifecycle/events.jsonl").read_bytes() == events


def test_exit_is_part_of_the_approved_digest_and_validated(tmp_path):
    root, intake = _intake_with_exit(tmp_path)
    digest = preview_backtest_registration(root, intake)["preview_digest"]
    changed = json.loads(json.dumps(intake))
    changed["exit"]["price"] = 108.0
    with pytest.raises(Exception, match="changed since the Owner preview"):
        commit_backtest_registration(root, changed, preview_digest=digest, approval=APPROVAL)
    for n, (bad, match) in enumerate(((dict(closed_at="2026-09-08T14:30:00+08:00"), "differs from replay_timing"),
                                      (dict(closed_at="2026-09-08T13:30:00+08:00"), "after the entry time"),
                                      (dict(realized_r=5), "disagrees"))):
        root2, intake2 = _intake_with_exit(tmp_path / f"b{n}", **bad)
        with pytest.raises(Exception, match=match):
            preview_backtest_registration(root2, intake2)
    root3, intake3 = _intake_with_exit(tmp_path / "c")
    intake3["images"] = [i for i in intake3["images"] if i["role"] != "exit_chart"]
    intake3["prices"]["target"]["evidence_ref"] = intake3["prices"]["entry"]["evidence_ref"]
    with pytest.raises(Exception, match="exit_chart"):
        preview_backtest_registration(root3, intake3)


def test_retry_with_exit_closes_an_already_open_sample(tmp_path):
    root, intake = _intake_with_exit(tmp_path)
    no_exit = {k: v for k, v in intake.items() if k != "exit"}
    first = commit_backtest_registration(root, no_exit, approval=APPROVAL,
                                         preview_digest=preview_backtest_registration(root, no_exit)["preview_digest"])
    assert first["result_counted"] is False
    digest = preview_backtest_registration(root, intake)["preview_digest"]
    closed = commit_backtest_registration(root, intake, preview_digest=digest, approval=APPROVAL)
    assert closed["status"] == "ALREADY_REGISTERED" and closed["result_counted"] is True
