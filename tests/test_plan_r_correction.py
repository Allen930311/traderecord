from __future__ import annotations

import copy
from pathlib import Path

import pytest

from test_chart_roles_event import CONFIRMED, _attach_image
from test_operational_closeout import _make_fixture
from trading_os.operational_analytics import _corrected_plan, _r_authority
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.registration_executor import RegistrationExecutorError, _supplement_plan

ROOT = Path(__file__).resolve().parents[1]


def _setup(tmp_path: Path, *, include_partial: bool = True):
    # The fixture is a long filled at 100.0. With the partial leg, half closed at 104 and the
    # rest at 110 (weighted exit 107); without it the whole position closed at 110.
    store, _, trade_id = _make_fixture(ROOT, tmp_path, write_review=False, include_partial=include_partial)
    return store, trade_id, _attach_image(store, trade_id, "plan-chart")


def _chart(ref: str, entry=100, stop=96, target=108) -> dict:
    return {
        name: {"value": value, "provenance": "chart_annotation", "evidence_ref": ref}
        for name, value in (("entry", entry), ("stop", stop), ("target", target))
    }


def test_chart_plan_replaces_plan_and_realized_r_append_only(tmp_path: Path) -> None:
    store, trade_id, ref = _setup(tmp_path)
    event = store.record_plan_r_correction(
        trade_id, _chart(ref), source_id="plan-r-test", confirmed_at=CONFIRMED
    )
    record = store.project()["records"][trade_id]
    # 1R = actual fill 100.0 to chart stop 96 = 4. Half the position left at 104 and half at 110,
    # so the weighted exit is 107 and the trade made +7 = 1.75R, not the last leg's +10.
    assert record["realized_r"] == 1.75
    assert record["plan_correction"]["plan"]["planned_rr"] == 2.0
    assert record["plan_correction"]["plan"]["risk_distance"] == 4.0
    assert record["plan_correction"]["plan"]["r_basis"] == "actual_entry_to_chart_stop"
    assert record["plan_correction"]["prior_realized_r"] == 2.0
    assert record["reported_realized_r"] == 2.0  # history is kept, not overwritten
    assert record["plan_correction_events"][0]["event_id"] == event["event_id"]
    assert record["plan_correction"]["owner_identity"] == "example-owner"


def test_a_later_correction_supersedes_the_earlier_one(tmp_path: Path) -> None:
    store, trade_id, ref = _setup(tmp_path)
    store.record_plan_r_correction(trade_id, _chart(ref, stop=96), source_id="first", confirmed_at=CONFIRMED)
    store.record_plan_r_correction(trade_id, _chart(ref, stop=98, target=112), source_id="second", confirmed_at=CONFIRMED)
    record = store.project()["records"][trade_id]
    assert record["realized_r"] == 3.5  # (107 - 100) / 2
    assert [e["source_id"] for e in record["plan_correction_events"]] == ["first", "second"]
    assert record["plan_correction"]["source_id"] == "second"


def test_levels_must_cite_an_image_saved_on_the_trade(tmp_path: Path) -> None:
    store, trade_id, _ = _setup(tmp_path)
    with pytest.raises(LifecycleError, match="not an attached image"):
        store.record_plan_r_correction(
            trade_id, _chart("artifact:" + "0" * 64 + ".png"), source_id="plan-r-test", confirmed_at=CONFIRMED
        )


def test_levels_that_contradict_the_direction_are_rejected(tmp_path: Path) -> None:
    store, trade_id, ref = _setup(tmp_path)
    with pytest.raises(LifecycleError, match="contradict"):
        store.record_plan_r_correction(
            trade_id, _chart(ref, stop=104), source_id="plan-r-test", confirmed_at=CONFIRMED
        )


@pytest.mark.parametrize("bad", [None, "", "not-a-time"])
def test_the_owner_confirmation_time_is_required(tmp_path: Path, bad) -> None:
    store, trade_id, ref = _setup(tmp_path)
    with pytest.raises(LifecycleError, match="confirmed_at"):
        store.record_plan_r_correction(trade_id, _chart(ref), source_id="plan-r-test", confirmed_at=bad)


def test_retry_is_idempotent_and_changed_content_fails_closed(tmp_path: Path) -> None:
    store, trade_id, ref = _setup(tmp_path)
    first = store.record_plan_r_correction(trade_id, _chart(ref), source_id="plan-r-test", confirmed_at=CONFIRMED)
    again = store.record_plan_r_correction(trade_id, _chart(ref), source_id="plan-r-test", confirmed_at=CONFIRMED)
    assert again["event_id"] == first["event_id"]
    assert len(store.project()["records"][trade_id]["plan_correction_events"]) == 1
    with pytest.raises(LifecycleError, match="changed content"):
        store.record_plan_r_correction(trade_id, _chart(ref, stop=97), source_id="plan-r-test", confirmed_at=CONFIRMED)


def test_the_caller_cannot_assert_an_r_that_the_chart_does_not_produce(tmp_path: Path) -> None:
    store, trade_id, ref = _setup(tmp_path)
    good = store.record_plan_r_correction(trade_id, _chart(ref), source_id="plan-r-test", confirmed_at=CONFIRMED)
    forged = copy.deepcopy(good["payload"])
    forged["source_id"] = "forged"
    forged["plan"]["realized_r"] = 9.0
    with pytest.raises(LifecycleError, match="does not match the chart prices"):
        store.append("plan_r_corrected", forged, idempotency_key="plan-r-correction:forged")


def test_a_backtest_or_unknown_trade_is_not_correctable(tmp_path: Path) -> None:
    store, trade_id, ref = _setup(tmp_path)
    with pytest.raises(LifecycleError, match="existing canonical trade"):
        store.record_plan_r_correction("trd_missing", _chart(ref), source_id="plan-r-test", confirmed_at=CONFIRMED)


def test_analytics_counts_a_chart_corrected_r_even_when_registration_had_no_stop() -> None:
    corrected = {"plan_correction": {"plan": {"stop_loss": 96.0, "planned_rr": 2.0}}}
    assert _corrected_plan(corrected) == {"stop_loss": 96.0, "planned_rr": 2.0}
    assert _r_authority(ROOT, corrected) == (True, "chart_plan_corrected")
    assert _corrected_plan({"plan_correction": None}) is None
    assert _corrected_plan(None) is None


def test_supplement_plan_accepts_plan_r_correction_batches_and_rejects_bad_input() -> None:
    kind, entries = _supplement_plan({
        "schema": "plan-r-correction.v1",
        "items": [{"trade_id": "trd_a", "chart": {"entry": {}}}, {"trade_id": "trd_b", "chart": {}}],
    })
    assert kind == "plan_r_correction"
    assert [e["trade_id"] for e in entries] == ["trd_a", "trd_b"]
    for bad in ({"schema": "plan-r-correction.v1"}, {"schema": "plan-r-correction.v1", "items": []},
                {"schema": "plan-r-correction.v1", "items": [{"trade_id": "t", "chart": {}}, {"trade_id": "t", "chart": {}}]}):
        with pytest.raises(RegistrationExecutorError):
            _supplement_plan(bad)


def test_plan_r_correction_persists_through_the_git_flow_and_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from test_chart_roles_event import _git, _git_chart_roles_fixture, _stub_test_lane
    from trading_os.registration_executor import record_chart_roles_supplement

    _stub_test_lane(monkeypatch)
    source, remote, trade_id, entry, _ = _git_chart_roles_fixture(tmp_path)
    request = tmp_path / "plan-r.json"
    request.write_text(json.dumps({
        "schema": "plan-r-correction.v1", "source_id": "plan-r-git", "confirmed_at": CONFIRMED,
        "items": [{"trade_id": trade_id, "chart": _chart(entry)}],
    }), encoding="utf-8")

    result = record_chart_roles_supplement(source, request, expected_kind="plan_r_correction")
    assert result["status"] == "PLAN_R_CORRECTED"
    assert result["results"][0]["realized_r"] == 1.75
    assert result["results"][0]["plan_correction"]["stop_loss"] == 96.0
    assert result["remote_readback"]["status"] == "VERIFIED"
    assert _git(remote, "rev-parse", "refs/heads/main") == result["git_commit"]

    # An identical retry changes nothing and pushes no second commit.
    again = record_chart_roles_supplement(source, request, expected_kind="plan_r_correction")
    assert again["status"] == "NO_CHANGE"
    assert _git(remote, "rev-parse", "refs/heads/main") == result["git_commit"]

    # A different supplement kind is refused for this command.
    with pytest.raises(RegistrationExecutorError, match="records chart_roles"):
        record_chart_roles_supplement(source, request, expected_kind="chart_roles")



def test_a_trade_closed_in_one_piece_is_measured_on_its_single_exit(tmp_path: Path) -> None:
    store, trade_id, ref = _setup(tmp_path, include_partial=False)
    store.record_plan_r_correction(trade_id, _chart(ref), source_id="whole", confirmed_at=CONFIRMED)
    assert store.project()["records"][trade_id]["realized_r"] == 2.5  # (110 - 100) / 4


def test_partial_legs_must_leave_a_remainder_for_the_final_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, trade_id, ref = _setup(tmp_path)
    real = store.current

    def current():
        projection = copy.deepcopy(real())
        projection["records"][trade_id]["partial_closes"][0]["quantity_fraction"] = 1.0
        return projection

    monkeypatch.setattr(store, "current", current)
    with pytest.raises(LifecycleError, match="remainder"):
        store.record_plan_r_correction(trade_id, _chart(ref), source_id="bad-legs", confirmed_at=CONFIRMED)


def test_a_correction_needs_a_confirmed_exit_so_it_can_never_be_lost_on_a_later_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, trade_id, ref = _setup(tmp_path)
    real = store.current

    for state, exit_state in (("OPEN", "pending"), ("PARTIALLY_CLOSED", "pending"), ("CLOSED", "pending")):
        def current(state=state, exit_state=exit_state):
            projection = copy.deepcopy(real())
            record = projection["records"][trade_id]
            record["state"] = state
            record["exit_verification"] = {"state": exit_state}
            return projection

        monkeypatch.setattr(store, "current", current)
        monkeypatch.setattr(store, "project", current)  # append() validates against project()
        with pytest.raises(LifecycleError, match="illegal lifecycle transition|confirmed exit"):
            store.record_plan_r_correction(trade_id, _chart(ref), source_id=f"early-{state}", confirmed_at=CONFIRMED)


def test_a_correction_is_allowed_on_a_trade_that_was_already_reviewed(tmp_path: Path) -> None:
    from test_operational_closeout import _make_fixture as make

    store, _, trade_id = make(ROOT, tmp_path, write_review=True)
    ref = _attach_image(store, trade_id, "plan-chart")
    store.record_plan_r_correction(trade_id, _chart(ref), source_id="after-review", confirmed_at=CONFIRMED)
    record = store.project()["records"][trade_id]
    assert record["state"] == "REVIEWED"
    assert record["realized_r"] == 1.75
    assert record["reported_realized_r"] == 2.0  # the registration-time value is still on record


def test_the_review_preview_shows_the_corrected_plan_and_its_digest_changes(tmp_path: Path) -> None:
    from review_test_support import review_candidate
    from trading_os.review_preview import prepare_review_preview

    store, _, trade_id = _make_fixture(ROOT, tmp_path, write_review=False, include_partial=False)
    ref = _attach_image(store, trade_id, "plan-chart")

    def preview():
        candidate = review_candidate(store, trade_id, {
            "what_went_well": "w", "what_went_wrong": "none observed", "rule_compliance": "compliant",
            "execution_deviation": "none observed", "thesis_validity": "supported", "entry_quality": "observed",
            "exit_quality": "observed", "evidence_completeness": "complete", "owner_notes": "n",
            "diagnostic_class": "METHOD",
        })
        return prepare_review_preview(store.root, store, trade_id, candidate)

    before = preview()
    store.record_plan_r_correction(trade_id, _chart(ref, stop=96, target=108), source_id="review-view", confirmed_at=CONFIRMED)
    after = preview()
    snapshot = after["content"]["trade_snapshot"]
    assert snapshot["planned"]["stop_loss_price"] == 96.0
    assert snapshot["planned"]["planned_rr"] == 2.0
    assert snapshot["plan_correction"]["realized_r"] == 2.5
    assert before["preview_digest"] != after["preview_digest"]


def test_a_correction_after_the_review_is_flagged_and_a_trade_before_review_is_not(tmp_path: Path) -> None:
    reviewed_store, _, reviewed_id = _make_fixture(ROOT, tmp_path / "reviewed", write_review=True)
    ref = _attach_image(reviewed_store, reviewed_id, "plan-chart")
    assert reviewed_store.project()["records"][reviewed_id]["plan_corrected_after_review"] is False
    reviewed_store.record_plan_r_correction(reviewed_id, _chart(ref), source_id="late", confirmed_at=CONFIRMED)
    record = reviewed_store.project()["records"][reviewed_id]
    assert record["plan_corrected_after_review"] is True
    assert record["plan_correction"]["reviewed_before_correction"] is True
    assert record["review_status"] == "reviewed" and record["review"]  # the review itself is untouched

    open_store, open_id, open_ref = _setup(tmp_path / "unreviewed")
    open_store.record_plan_r_correction(open_id, _chart(open_ref), source_id="early", confirmed_at=CONFIRMED)
    assert open_store.project()["records"][open_id]["plan_corrected_after_review"] is False


def test_the_flag_reaches_analytics_and_trade_view(tmp_path: Path) -> None:
    import json

    from trading_os.projections import refresh_projections

    store, _, trade_id = _make_fixture(ROOT, tmp_path, write_review=True)
    ref = _attach_image(store, trade_id, "plan-chart")
    store.record_plan_r_correction(trade_id, _chart(ref), source_id="late", confirmed_at=CONFIRMED)
    refresh_projections(tmp_path, target_trade=trade_id)
    view = json.loads((tmp_path / "exports/operational/trade-view/trade-view-data.json").read_text(encoding="utf-8"))
    rows = [row for row in view["rows"] if row.get("reference") == trade_id]
    assert [row["plan_corrected_after_review"] for row in rows] == [True]
    assert rows[0]["plan"]["stop_loss"] == 96.0 and rows[0]["planned_rr"] == 2.0
    app = (tmp_path / "exports/operational/trade-view/app.js").read_text(encoding="utf-8")
    assert "plan-corrected-after-review" in app
