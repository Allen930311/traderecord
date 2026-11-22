"""Shared BACKTEST registration service: one write path for every Agent."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from test_operational_closeout import _copy_operational_inputs
from trading_os.backtest_registration import (
    BacktestRegistrationError,
    commit_backtest_registration,
    preview_backtest_registration,
)
from trading_os.operational_lifecycle import LifecycleStore

SOURCE = Path(__file__).resolve().parents[1]
APPROVAL = {
    "confirmed_in_chat": True,
    "confirmed_at": "2026-10-12T17:40:00+08:00",
    "agent_id": "synthetic-agent-fixture",
    "owner_statement": "SYNTHETIC FIXTURE ONLY; NOT OWNER APPROVAL",
}
ENTRY = "registration-intake/bt-fixture/entry.png"
EXIT = "registration-intake/bt-fixture/exit.png"


def _fixture(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "repo"
    _copy_operational_inputs(SOURCE, root)
    for rel, payload in ((ENTRY, b"entry"), (EXIT, b"exit")):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic " + payload)
    intake = {
        "schema_version": "backtest-chart-intake.v1",
        "strategy": {"strategy_id": "stg-example-a", "rule_version": "stg-example-a:v1"},
        "mode": "BACKTEST",
        "source_method": "tradingview_replay",
        "instrument": "NAS100",
        "direction": "long",
        "owner_verbatim": "synthetic Owner words for a fixture sample",
        "images": [{"role": "entry_chart", "ref": ENTRY}, {"role": "exit_chart", "ref": EXIT}],
        "prices": {
            "entry": {"value": 100.0, "provenance": "chart_annotation", "evidence_ref": ENTRY},
            "stop": {"value": 95.0, "provenance": "chart_annotation", "evidence_ref": ENTRY},
            "target": {"value": 110.0, "provenance": "chart_annotation", "evidence_ref": EXIT},
        },
        "simulated_entry_at": None,
        "outcome": "target_area_reached_on_chart_not_execution_proof",
        "timeframe_context": "15m",
    }
    return root, intake


def test_preview_is_read_only_and_binds_content(tmp_path):
    root, intake = _fixture(tmp_path)
    before = (root / "canonical/sequences/numbers.json").read_bytes()
    preview = preview_backtest_registration(root, intake)
    assert preview["status"] == "READY_FOR_OWNER_CONFIRMATION"
    assert preview["expected_trade_no"] == "BT000001"
    assert preview["planned_rr_from_chart"] == "2.000"
    assert (root / "canonical/sequences/numbers.json").read_bytes() == before
    assert not (root / "canonical/lifecycle/events.jsonl").exists()
    changed = copy.deepcopy(intake)
    changed["prices"]["stop"]["value"] = 96.0
    assert preview_backtest_registration(root, changed)["preview_digest"] != preview["preview_digest"]


def test_replay_timing_is_optional_digest_bound_and_validated_at_preview(tmp_path):
    root, intake = _fixture(tmp_path)
    plain = preview_backtest_registration(root, intake)
    assert "replay_timing" not in plain["content"]
    timed = copy.deepcopy(intake)
    timed["replay_timing"] = {"entry_at": "2026-09-08T13:40:00+08:00",
                              "exit_at": "2026-09-08T13:55:00+08:00", "source": "owner_stated"}
    p1 = preview_backtest_registration(root, timed)
    assert p1["content"]["replay_timing"] == timed["replay_timing"]
    assert p1["preview_digest"] != plain["preview_digest"]
    changed = copy.deepcopy(timed)
    changed["replay_timing"]["exit_at"] = "2026-09-08T14:00:00+08:00"
    assert preview_backtest_registration(root, changed)["preview_digest"] != p1["preview_digest"]
    for key, value in (("entry_at", "2026-09-08T13:40:00"),
                       ("exit_at", "2026-09-08T13:30:00+08:00"), ("source", "")):
        invalid = copy.deepcopy(timed)
        invalid["replay_timing"][key] = value
        with pytest.raises(BacktestRegistrationError):
            preview_backtest_registration(root, invalid)


def test_replay_timing_is_written_once_and_late_timing_only_appends_event(tmp_path):
    root, intake = _fixture(tmp_path)
    intake["replay_timing"] = {"entry_at": "2026-09-08T13:40:00+08:00",
                               "exit_at": "2026-09-08T13:55:00+08:00", "source": "owner_stated"}
    preview = preview_backtest_registration(root, intake)
    first = commit_backtest_registration(root, intake, preview_digest=preview["preview_digest"], approval=APPROVAL)
    assert first["timing_recorded"] is True
    record = LifecycleStore(root).current()["records"][first["trade_id"]]
    assert record["backtest_timing"]["entry_at"] == intake["replay_timing"]["entry_at"]
    assert record["backtest_timing"]["exit_at"] == intake["replay_timing"]["exit_at"]
    events = (root / "canonical/lifecycle/events.jsonl").read_bytes()
    numbers = (root / "canonical/sequences/numbers.json").read_bytes()
    retry = commit_backtest_registration(root, intake, preview_digest=preview["preview_digest"], approval=APPROVAL)
    assert retry["status"] == "ALREADY_REGISTERED" and retry["timing_recorded"] is True
    assert (root / "canonical/lifecycle/events.jsonl").read_bytes() == events
    assert (root / "canonical/sequences/numbers.json").read_bytes() == numbers

    root2, intake2 = _fixture(tmp_path / "late")
    p = preview_backtest_registration(root2, intake2)
    registered = commit_backtest_registration(root2, intake2, preview_digest=p["preview_digest"], approval=APPROVAL)
    late = copy.deepcopy(intake2)
    late["replay_timing"] = intake["replay_timing"]
    p2 = preview_backtest_registration(root2, late)
    appended = commit_backtest_registration(root2, late, preview_digest=p2["preview_digest"], approval=APPROVAL)
    assert p2["status"] == "ALREADY_REGISTERED"
    assert appended["status"] == "ALREADY_REGISTERED" and appended["trade_no"] == registered["trade_no"]
    assert appended["timing_recorded"] is True
    assert len(LifecycleStore(root2).current()["records"][registered["trade_id"]]["backtest_timing_events"]) == 1


def test_conflicting_replay_timing_fails_closed(tmp_path):
    root, intake = _fixture(tmp_path)
    intake["replay_timing"] = {"entry_at": "2026-09-08T13:40:00+08:00",
                               "exit_at": "2026-09-08T13:55:00+08:00", "source": "owner_stated"}
    p = preview_backtest_registration(root, intake)
    commit_backtest_registration(root, intake, preview_digest=p["preview_digest"], approval=APPROVAL)
    changed = copy.deepcopy(intake)
    changed["replay_timing"]["exit_at"] = "2026-09-08T14:00:00+08:00"
    p2 = preview_backtest_registration(root, changed)
    before = (root / "canonical/lifecycle/events.jsonl").read_bytes()
    with pytest.raises(BacktestRegistrationError, match="different content"):
        commit_backtest_registration(root, changed, preview_digest=p2["preview_digest"], approval=APPROVAL)
    assert (root / "canonical/lifecycle/events.jsonl").read_bytes() == before


def test_commit_registers_once_and_identical_retry_writes_nothing(tmp_path):
    root, intake = _fixture(tmp_path)
    digest = preview_backtest_registration(root, intake)["preview_digest"]
    first = commit_backtest_registration(root, intake, preview_digest=digest, approval=APPROVAL)
    assert first["status"] == "REGISTERED" and first["trade_no"] == "BT000001"
    record = LifecycleStore(root).current()["records"][first["trade_id"]]
    assert record["trade_type"] == "backtest" and record["symbol"] == "NAS100"
    assert record.get("provider_pnl") is None
    assert (record.get("entry") or {}).get("execution_at") is None
    shas = {hashlib.sha256((root / p).read_bytes()).hexdigest() for p in (ENTRY, EXIT)}
    assert shas <= {item.get("sha256") for item in record["evidence"]}
    events = (root / "canonical/lifecycle/events.jsonl").read_bytes()
    numbers = (root / "canonical/sequences/numbers.json").read_bytes()
    second = commit_backtest_registration(root, intake, preview_digest=digest, approval=APPROVAL)
    assert second["status"] == "ALREADY_REGISTERED" and second["trade_no"] == "BT000001"
    assert (root / "canonical/lifecycle/events.jsonl").read_bytes() == events
    assert (root / "canonical/sequences/numbers.json").read_bytes() == numbers


def test_commit_rejects_missing_approval_or_drift(tmp_path):
    root, intake = _fixture(tmp_path)
    digest = preview_backtest_registration(root, intake)["preview_digest"]
    with pytest.raises(BacktestRegistrationError, match="approval"):
        commit_backtest_registration(root, intake, preview_digest=digest,
                                     approval={**APPROVAL, "confirmed_in_chat": False})
    (root / EXIT).write_bytes(b"\x89PNG\r\n\x1a\nswapped after preview")
    with pytest.raises(BacktestRegistrationError, match="changed since the Owner preview"):
        commit_backtest_registration(root, intake, preview_digest=digest, approval=APPROVAL)
    assert not (root / "canonical/lifecycle/events.jsonl").exists()


@pytest.mark.parametrize("ref", ["../outside.png", "/etc/passwd.png", "canonical/x.png",
                                 "registration-intake/missing.png"])
def test_images_must_be_committed_originals_in_intake_paths(tmp_path, ref):
    root, intake = _fixture(tmp_path)
    intake["images"][1]["ref"] = ref
    intake["prices"]["target"]["evidence_ref"] = ref
    with pytest.raises(BacktestRegistrationError):
        preview_backtest_registration(root, intake)


def test_unknown_strategy_version_and_unknown_entry_fail_closed(tmp_path):
    root, intake = _fixture(tmp_path)
    bad = copy.deepcopy(intake)
    bad["strategy"]["rule_version"] = "stg-example-a:v99"
    with pytest.raises(BacktestRegistrationError, match="frozen strategy version"):
        preview_backtest_registration(root, bad)
    bad = copy.deepcopy(intake)
    bad["prices"]["entry"] = {"value": None, "provenance": "unknown", "evidence_ref": None}
    with pytest.raises(BacktestRegistrationError, match="entry price"):
        preview_backtest_registration(root, bad)


def test_real_bt000001_charts_are_recognised_as_already_registered():
    """Re-submitting BT000001's original charts must never allocate a new BT number."""
    template = json.loads((SOURCE / "Research/backtest-candidates/nas100-issue-144/agent-intake.v1.json")
                          .read_text(encoding="utf-8"))
    template["instrument"] = "NAS100"
    preview = preview_backtest_registration(SOURCE, template)
    assert preview["status"] == "ALREADY_REGISTERED"
    assert preview["existing_trade_no"] == "BT000001"


RESAVED = "registration-intake/bt-fixture/entry-resaved.png"


def _register_then_resave(tmp_path):
    """Register one sample, then offer the same replay trade with re-saved chart bytes."""
    root, intake = _fixture(tmp_path)
    digest = preview_backtest_registration(root, intake)["preview_digest"]
    first = commit_backtest_registration(root, intake, preview_digest=digest, approval=APPROVAL)
    (root / RESAVED).write_bytes(b"\x89PNG\r\n\x1a\nsame replay trade, saved again")
    again = copy.deepcopy(intake)
    again["images"] = [{"role": "entry_chart", "ref": RESAVED}]
    for name in ("entry", "stop", "target"):
        again["prices"][name]["evidence_ref"] = RESAVED
    return root, first, again


def test_resaved_chart_of_a_registered_sample_is_held_as_possible_duplicate(tmp_path):
    root, first, again = _register_then_resave(tmp_path)
    preview = preview_backtest_registration(root, again)
    assert preview["status"] == "POSSIBLE_DUPLICATE"
    assert preview["possible_duplicates"] == [first["trade_no"]]
    numbers = (root / "canonical/sequences/numbers.json").read_bytes()
    with pytest.raises(BacktestRegistrationError, match="looks like already registered BT000001"):
        commit_backtest_registration(root, again, preview_digest=preview["preview_digest"], approval=APPROVAL)
    assert (root / "canonical/sequences/numbers.json").read_bytes() == numbers


def test_owner_confirmed_distinct_sample_registers_with_the_decision_in_its_digest(tmp_path):
    root, first, again = _register_then_resave(tmp_path)
    held = preview_backtest_registration(root, again)["preview_digest"]
    again["distinct_from"] = [first["trade_no"]]
    preview = preview_backtest_registration(root, again)
    assert preview["status"] == "READY_FOR_OWNER_CONFIRMATION"
    assert preview["preview_digest"] != held
    assert preview["content"]["distinct_from"] == ["BT000001"]
    with pytest.raises(BacktestRegistrationError, match="distinct_from_confirmed"):
        commit_backtest_registration(root, again, preview_digest=preview["preview_digest"], approval=APPROVAL)
    second = commit_backtest_registration(root, again, preview_digest=preview["preview_digest"],
                                          approval={**APPROVAL, "distinct_from_confirmed": ["BT000001"]})
    assert second["status"] == "REGISTERED" and second["trade_no"] == "BT000002"


@pytest.mark.parametrize("pre_authorised", [["BT000009"], ["BT000001", "BT000009"]])
def test_distinct_from_cannot_pre_authorise_other_numbers(tmp_path, pre_authorised):
    """Codex: distinct_from must answer exactly the duplicates shown, never more or other."""
    root, _, again = _register_then_resave(tmp_path)
    again["distinct_from"] = pre_authorised
    with pytest.raises(BacktestRegistrationError, match="exactly the possible duplicates BT000001"):
        preview_backtest_registration(root, again)


def test_distinct_from_without_any_duplicate_is_refused(tmp_path):
    root, intake = _fixture(tmp_path)
    intake["distinct_from"] = ["BT000001"]
    with pytest.raises(BacktestRegistrationError, match=r"\(none\)"):
        preview_backtest_registration(root, intake)


def test_naive_and_aware_times_of_one_instant_do_not_hide_a_duplicate(tmp_path):
    """Codex: mixed timezone forms compare as unknown, so the stop still decides."""
    from trading_os.backtest_registration import _same_time

    assert _same_time("2026-10-12T04:11:42Z", "2026-10-12T04:11:42") is None
    assert _same_time("2026-10-12T04:11:42Z", "2026-10-12T12:11:42+08:00") is True
    assert _same_time("2026-10-12T04:11:42Z", "2026-10-12T05:11:42Z") is False


def test_reusing_a_registered_chart_for_another_entry_is_not_already_registered(tmp_path):
    """Codex: a subset image match with a different plan must not be swallowed as ALREADY_REGISTERED."""
    root, intake = _fixture(tmp_path)
    digest = preview_backtest_registration(root, intake)["preview_digest"]
    commit_backtest_registration(root, intake, preview_digest=digest, approval=APPROVAL)
    other = copy.deepcopy(intake)
    other["images"] = [{"role": "entry_chart", "ref": ENTRY}]
    for name in ("entry", "stop", "target"):
        other["prices"][name]["evidence_ref"] = ENTRY
    other["prices"]["entry"]["value"] = 101.0
    assert preview_backtest_registration(root, other)["status"] == "READY_FOR_OWNER_CONFIRMATION"


def test_concurrent_resaved_copies_register_only_once(tmp_path):
    """Codex: the duplicate check and the write are serialised."""
    import threading

    root, intake = _fixture(tmp_path)
    copies = []
    for index in range(2):
        ref = f"registration-intake/bt-fixture/entry-copy-{index}.png"
        (root / ref).write_bytes(b"\x89PNG\r\n\x1a\nconcurrent copy " + bytes([index]))
        item = copy.deepcopy(intake)
        item["images"] = [{"role": "entry_chart", "ref": ref}]
        for name in ("entry", "stop", "target"):
            item["prices"][name]["evidence_ref"] = ref
        copies.append((item, preview_backtest_registration(root, item)["preview_digest"]))
    outcomes = []

    def register(item, digest):
        try:
            outcomes.append(commit_backtest_registration(root, item, preview_digest=digest, approval=APPROVAL)["status"])
        except BacktestRegistrationError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=register, args=pair) for pair in copies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("REGISTERED") == 1
    backtests = [r for r in LifecycleStore(root).current()["records"].values() if r.get("trade_type") == "backtest"]
    assert len(backtests) == 1


@pytest.mark.parametrize("change", [
    {"stop": 96.0},
    {"entry": 100.5},
])
def test_different_entry_or_stop_is_not_a_duplicate(tmp_path, change):
    root, _, again = _register_then_resave(tmp_path)
    for name, value in change.items():
        again["prices"][name]["value"] = value
    assert preview_backtest_registration(root, again)["status"] == "READY_FOR_OWNER_CONFIRMATION"


@pytest.mark.parametrize("bad", ["BT000001", ["T000001"], [1]])
def test_distinct_from_must_list_bt_numbers(tmp_path, bad):
    root, intake = _fixture(tmp_path)
    intake["distinct_from"] = bad
    with pytest.raises(BacktestRegistrationError, match="distinct_from"):
        preview_backtest_registration(root, intake)


def test_reusing_a_registered_chart_with_a_different_stop_is_not_already_registered(tmp_path):
    """Codex round 2: the subset match must compare the stop too, not only the entry price."""
    root, intake = _fixture(tmp_path)
    digest = preview_backtest_registration(root, intake)["preview_digest"]
    commit_backtest_registration(root, intake, preview_digest=digest, approval=APPROVAL)
    other = copy.deepcopy(intake)
    other["images"] = [{"role": "entry_chart", "ref": ENTRY}]
    for name in ("entry", "stop", "target"):
        other["prices"][name]["evidence_ref"] = ENTRY
    other["prices"]["stop"]["value"] = 96.0
    preview = preview_backtest_registration(root, other)
    assert preview["status"] == "READY_FOR_OWNER_CONFIRMATION" and preview["existing_trade_no"] is None


def test_a_crash_left_lock_file_does_not_block_registration(tmp_path):
    """Codex round 2: the lock is an OS lock, so a killed holder cannot wedge later commits."""
    import subprocess
    import sys
    import textwrap

    root, intake = _fixture(tmp_path)
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent("""
        import sys, time
        sys.path.insert(0, sys.argv[2])
        from pathlib import Path
        from trading_os.backtest_registration import _commit_lock
        with _commit_lock(Path(sys.argv[1])):
            print("locked", flush=True)
            time.sleep(600)
    """), str(root), str(SOURCE)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        holder.kill()  # the holder dies with the lock held and never runs any cleanup
        holder.wait()
        assert (root / "canonical/registration/backtest-commit.lock").exists()
        digest = preview_backtest_registration(root, intake)["preview_digest"]
        result = commit_backtest_registration(root, intake, preview_digest=digest, approval=APPROVAL)
        assert result["status"] == "REGISTERED"
    finally:
        holder.kill()


def test_same_entry_time_but_a_different_stop_is_not_silently_already_registered():
    """Codex round 3: an equal known entry time must not hide a different known stop.

    Tested on the rule itself: a BACKTEST with a known entry time cannot be committed on
    main today (a separate lifecycle bug), so no such record can be built end to end.
    """
    from trading_os.backtest_registration import _same_plan

    row = {"entry": {"actual_entry": 100.0, "execution_at": "2026-10-12T04:11:42Z"},
           "planned": {"stop_loss_price": 95.0}}

    def intake(stop, at="2026-10-12T04:11:42Z"):
        return {"prices": {"entry": {"value": "100.0"}, "stop": {"value": stop}}, "simulated_entry_at": at}

    assert _same_plan(row, intake("96.0"), strict=False) is True      # still shown to the Owner as a candidate
    assert _same_plan(row, intake("96.0"), strict=True) is False      # but never silently ALREADY_REGISTERED
    assert _same_plan(row, intake("95.0"), strict=True) is True
    assert _same_plan(row, intake("95.0", at="2026-10-12T05:11:42Z"), strict=True) is False


def test_identical_images_with_other_prices_stay_one_sample_but_say_the_plan_differs(tmp_path):
    root, intake = _fixture(tmp_path)
    digest = preview_backtest_registration(root, intake)["preview_digest"]
    commit_backtest_registration(root, intake, preview_digest=digest, approval=APPROVAL)
    same = preview_backtest_registration(root, intake)
    assert same["status"] == "ALREADY_REGISTERED" and same["existing_plan_matches"] is True
    reread = copy.deepcopy(intake)
    reread["prices"]["stop"]["value"] = 96.0
    preview = preview_backtest_registration(root, reread)
    assert preview["status"] == "ALREADY_REGISTERED" and preview["existing_plan_matches"] is False


def test_identical_retry_after_a_distinct_sample_registered_is_a_no_op(tmp_path):
    """Reviewer P1: the exact-list rule must not reject the retry of an already registered sample."""
    root, first, again = _register_then_resave(tmp_path)
    again["distinct_from"] = [first["trade_no"]]
    preview = preview_backtest_registration(root, again)
    approval = {**APPROVAL, "distinct_from_confirmed": [first["trade_no"]]}
    second = commit_backtest_registration(root, again, preview_digest=preview["preview_digest"], approval=approval)
    assert second["status"] == "REGISTERED" and second["trade_no"] == "BT000002"
    events = (root / "canonical/lifecycle/events.jsonl").read_bytes()
    numbers = (root / "canonical/sequences/numbers.json").read_bytes()

    retry_preview = preview_backtest_registration(root, again)
    assert retry_preview["status"] == "ALREADY_REGISTERED"
    assert retry_preview["preview_digest"] == preview["preview_digest"]
    retry = commit_backtest_registration(root, again, preview_digest=preview["preview_digest"], approval=approval)
    assert retry["status"] == "ALREADY_REGISTERED" and retry["trade_no"] == "BT000002"
    assert (root / "canonical/lifecycle/events.jsonl").read_bytes() == events
    assert (root / "canonical/sequences/numbers.json").read_bytes() == numbers


def test_a_later_similar_sample_does_not_change_an_earlier_samples_retry_digest(tmp_path):
    root, first, again = _register_then_resave(tmp_path)
    again["distinct_from"] = [first["trade_no"]]
    preview = preview_backtest_registration(root, again)
    approval = {**APPROVAL, "distinct_from_confirmed": [first["trade_no"]]}
    commit_backtest_registration(root, again, preview_digest=preview["preview_digest"], approval=approval)
    third = copy.deepcopy(again)
    ref = "registration-intake/bt-fixture/entry-third.png"
    (root / ref).write_bytes(b"\x89PNG\r\n\x1a\nthird copy")
    third["images"] = [{"role": "entry_chart", "ref": ref}]
    for name in ("entry", "stop", "target"):
        third["prices"][name]["evidence_ref"] = ref
    third["distinct_from"] = ["BT000001", "BT000002"]
    p3 = preview_backtest_registration(root, third)
    commit_backtest_registration(root, third, preview_digest=p3["preview_digest"],
                                 approval={**APPROVAL, "distinct_from_confirmed": ["BT000001", "BT000002"]})
    assert preview_backtest_registration(root, again)["preview_digest"] == preview["preview_digest"]


def test_a_known_entry_time_registers_and_the_time_rule_separates_duplicates(tmp_path):
    """#175: end to end with a known entry time, and the #172 time discriminator is now reachable."""
    root, intake = _fixture(tmp_path)
    intake["simulated_entry_at"] = "2026-10-12T04:11:42Z"
    digest = preview_backtest_registration(root, intake)["preview_digest"]
    first = commit_backtest_registration(root, intake, preview_digest=digest, approval=APPROVAL)
    record = LifecycleStore(root).current()["records"][first["trade_id"]]
    assert first["status"] == "REGISTERED" and record["entry"]["execution_at"] == "2026-10-12T04:11:42Z"
    assert record.get("provider_pnl") is None and first["result_counted"] is False

    def resaved(at, stop=95.0):
        ref = "registration-intake/bt-fixture/entry-time-" + "".join(ch for ch in at + str(stop) if ch.isalnum()) + ".png"
        (root / ref).write_bytes(b"\x89PNG\r\n\x1a\nresaved " + at.encode() + str(stop).encode())
        item = copy.deepcopy(intake)
        item["images"] = [{"role": "entry_chart", "ref": ref}]
        for name in ("entry", "stop", "target"):
            item["prices"][name]["evidence_ref"] = ref
        item["prices"]["stop"]["value"] = stop
        item["simulated_entry_at"] = at
        return item

    same_instant = preview_backtest_registration(root, resaved("2026-10-12T04:11:42Z"))
    assert same_instant["status"] == "POSSIBLE_DUPLICATE" and same_instant["possible_duplicates"] == ["BT000001"]
    same_instant_other_stop = preview_backtest_registration(root, resaved("2026-10-12T04:11:42Z", stop=96.0))
    assert same_instant_other_stop["status"] == "POSSIBLE_DUPLICATE"        # equal time + other stop is still shown
    other_instant = preview_backtest_registration(root, resaved("2026-10-12T09:30:00Z"))
    assert other_instant["status"] == "READY_FOR_OWNER_CONFIRMATION" and other_instant["possible_duplicates"] == []
