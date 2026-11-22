from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from test_operational_closeout import _make_fixture
from trading_os.evidence_store import EvidenceStore
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.registration_census import registration_completeness
from trading_os.projections import refresh_projections


ROOT = Path(__file__).resolve().parents[1]


CONFIRMED = "2026-10-11T08:00:00Z"
WORDS_SHA = hashlib.sha256("原話".encode("utf-8")).hexdigest()


def _attach_image(store: LifecycleStore, trade_id: str, name: str) -> str:
    raw = b"\x89PNG\r\n\x1a\n" + hashlib.sha256(name.encode()).digest()
    path = Path(store.root) / f"{name}.png"
    path.write_bytes(raw)
    info = EvidenceStore(store.root).ingest(path)
    store.attach_evidence(
        store.resolve_draft_id(trade_id),
        {
            **info,
            "source_ref": info["locator"],
            "category": "review",
            "timing": "post_trade",
            "type": "replay_chart",
            "captured_at": "2026-09-11T00:07:00Z",
        },
    )
    return info["locator"]


def _setup(tmp_path: Path):
    source = Path(__file__).resolve().parents[1]
    store, _, trade_id = _make_fixture(source, tmp_path, write_review=False)
    return store, trade_id, _attach_image(store, trade_id, "entry"), _attach_image(store, trade_id, "exit")


def _stub_test_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace only the heavy test-lane steps; begin, Git commit, push and read-back stay real.

    The lane runs the repository's whole focused suite, including a check of the real 33-trade
    history that a synthetic one-trade fixture cannot satisfy. The lane has its own tests.
    """

    from trading_os import registration_executor as executor

    real = executor._call_cli

    def call(root, *args, **kwargs):
        if args[:2] == ("registration-test-lane", "run"):
            return {"test_lane": "focused", "returncode": 0, "stubbed": True}
        if args[:2] == ("registration-test-lane", "verify-tested-diff"):
            return {"status": "stubbed"}
        return real(root, *args, **kwargs)

    monkeypatch.setattr(executor, "_call_cli", call)


def _role(*refs: str, rationale: str = "The Agent read this chart.") -> dict:
    return {"evidence_refs": list(refs), "rationale": rationale}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _git_chart_roles_fixture(tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
    store, _, trade_id = _make_fixture(ROOT, source, write_review=False)
    entry = _attach_image(store, trade_id, "entry")
    exit_ = _attach_image(store, trade_id, "exit")
    refresh_projections(source, target_trade=trade_id)

    for relative in (".github", "scripts", "trading_os", "tests", "workflows"):
        shutil.copytree(ROOT / relative, source / relative)
    for relative in ("AUTHORITY.md", "REVIEW_RULES.md"):
        shutil.copy2(ROOT / relative, source / relative)
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Chart Roles Test")
    _git(source, "config", "user.email", "chart-roles-test@example.invalid")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "synthetic chart roles fixture")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-u", "origin", "main")
    return source, remote, trade_id, entry, exit_


def test_agent_chart_roles_are_recorded_append_only_and_projected(tmp_path: Path) -> None:
    store, trade_id, entry, exit_ = _setup(tmp_path)
    event = store.record_chart_roles(
        trade_id,
        {"entry_chart": _role(entry), "exit_chart": _role(exit_)},
        source_id="chart-roles-test",
        confirmed_at=CONFIRMED,
    )
    record = store.project()["records"][trade_id]
    assert record["chart_roles"]["entry_chart"]["evidence_refs"] == [entry]
    assert record["chart_roles"]["exit_chart"]["evidence_refs"] == [exit_]
    assert record["chart_roles_events"][0]["event_id"] == event["event_id"]
    assert record["chart_roles_events"][0]["owner_identity"] == "example-owner"
    assert record["chart_roles_events"][0]["approval_source"] == "same_conversation_agent_attestation"
    assert record["chart_roles_events"][0]["confirmed_at"] == CONFIRMED


def test_roles_must_cite_images_saved_on_the_trade(tmp_path: Path) -> None:
    store, trade_id, entry, _ = _setup(tmp_path)
    with pytest.raises(LifecycleError, match="images saved on this trade"):
        store.record_chart_roles(
            trade_id, {"entry_chart": _role("artifact:" + "0" * 64 + ".png")},
            source_id="chart-roles-test", confirmed_at=CONFIRMED,
        )


@pytest.mark.parametrize("bad", [{"other_chart": {"evidence_refs": ["x"], "rationale": "r"}}])
def test_only_entry_and_exit_roles_are_accepted(tmp_path: Path, bad: dict) -> None:
    store, trade_id, *_ = _setup(tmp_path)
    with pytest.raises(LifecycleError, match="only declare entry_chart and/or exit_chart"):
        store.record_chart_roles(trade_id, bad, source_id="chart-roles-test", confirmed_at=CONFIRMED)


def test_a_role_without_a_rationale_is_rejected(tmp_path: Path) -> None:
    store, trade_id, entry, _ = _setup(tmp_path)
    with pytest.raises(LifecycleError, match="rationale"):
        store.record_chart_roles(
            trade_id, {"entry_chart": _role(entry, rationale="  ")},
            source_id="chart-roles-test", confirmed_at=CONFIRMED,
        )


def test_retry_is_idempotent_and_changed_content_fails_closed(tmp_path: Path) -> None:
    store, trade_id, entry, exit_ = _setup(tmp_path)
    roles = {"entry_chart": _role(entry)}
    first = store.record_chart_roles(trade_id, roles, source_id="chart-roles-test", confirmed_at=CONFIRMED)
    again = store.record_chart_roles(trade_id, roles, source_id="chart-roles-test", confirmed_at=CONFIRMED)
    assert first["event_id"] == again["event_id"]
    with pytest.raises(LifecycleError, match="changed content"):
        store.record_chart_roles(trade_id, {"entry_chart": _role(exit_)}, source_id="chart-roles-test", confirmed_at=CONFIRMED)


def test_each_event_is_a_complete_snapshot_so_a_role_can_be_withdrawn(tmp_path: Path) -> None:
    store, trade_id, entry, exit_ = _setup(tmp_path)
    store.record_chart_roles(trade_id, {"entry_chart": _role(entry), "exit_chart": _role(exit_)}, source_id="first", confirmed_at=CONFIRMED)
    store.record_chart_roles(trade_id, {"entry_chart": _role(entry)}, source_id="second", confirmed_at=CONFIRMED)
    record = store.project()["records"][trade_id]
    assert set(record["chart_roles"]) == {"entry_chart"}
    assert len(record["chart_roles_events"]) == 2
    words = [{"authority": "owner_supplied_verbatim", "verbatim_text": "原話", "sha256": WORDS_SHA}]
    assert registration_completeness({**record, "owner_statements": words}, root=store.root)["missing"] == ["exit_chart"]
    store.record_chart_roles(trade_id, {}, source_id="third", confirmed_at=CONFIRMED)
    assert store.project()["records"][trade_id]["chart_roles"] == {}


@pytest.mark.parametrize("bad", [None, "", "not-a-time"])
def test_the_owner_confirmation_time_is_required(tmp_path: Path, bad) -> None:
    store, trade_id, entry, _ = _setup(tmp_path)
    with pytest.raises(LifecycleError, match="confirmed_at"):
        store.record_chart_roles(trade_id, {"entry_chart": _role(entry)}, source_id="chart-roles-test", confirmed_at=bad)


def test_agent_cli_records_roles_and_refreshes_projections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_os.registration_executor import record_chart_roles_supplement

    _stub_test_lane(monkeypatch)
    source, _, trade_id, entry, exit_ = _git_chart_roles_fixture(tmp_path)
    request = tmp_path / "confirmation.json"
    request.write_text(json.dumps({
        "schema": "chart-roles-confirmation.v1", "trade_id": trade_id, "source_id": "cli-test",
        "confirmed_at": CONFIRMED, "chart_roles": {"entry_chart": _role(entry), "exit_chart": _role(exit_)},
    }), encoding="utf-8")
    first = record_chart_roles_supplement(source, request)
    again = record_chart_roles_supplement(source, request)
    assert first["status"] == "CHART_ROLES_RECORDED"
    assert first["persistence"]["status"] == "PUSHED"
    assert first["remote_readback"]["status"] == "VERIFIED"
    assert first["test_lane"] == "focused"
    assert "canonical/lifecycle/events.jsonl" in first["changed_paths"]
    assert not any(
        path.startswith(("canonical/records/", "canonical/evidence/"))
        for path in first["changed_paths"]
    )
    assert first["event_id"] == again["event_id"]
    assert again["status"] == "NO_CHANGE"
    assert again["persistence"]["status"] == "NO_CHANGE"
    assert again["git_commit"] == first["git_commit"]
    assert again["test_receipt"] is None
    assert set(first["chart_roles"]) == {"entry_chart", "exit_chart"}


def test_chart_roles_changed_content_is_rejected_without_a_second_remote_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_os.registration_executor import RegistrationExecutorError, record_chart_roles_supplement

    _stub_test_lane(monkeypatch)
    source, remote, trade_id, entry, exit_ = _git_chart_roles_fixture(tmp_path)
    request = tmp_path / "confirmation.json"
    request.write_text(json.dumps({
        "schema": "chart-roles-confirmation.v1", "trade_id": trade_id, "source_id": "cli-test",
        "confirmed_at": CONFIRMED, "chart_roles": {"entry_chart": _role(entry), "exit_chart": _role(exit_)},
    }), encoding="utf-8")
    first = record_chart_roles_supplement(source, request)
    remote_before = _git(source, "rev-parse", "origin/main")
    commit_count = _git(source, "rev-list", "--count", "origin/main")

    request.write_text(json.dumps({
        "schema": "chart-roles-confirmation.v1", "trade_id": trade_id, "source_id": "cli-test",
        "confirmed_at": CONFIRMED, "chart_roles": {"entry_chart": _role(exit_), "exit_chart": _role(entry)},
    }), encoding="utf-8")
    with pytest.raises((LifecycleError, RegistrationExecutorError), match="changed content"):
        record_chart_roles_supplement(source, request)

    assert _git(source, "rev-parse", "origin/main") == remote_before == first["git_commit"]
    assert _git(source, "rev-list", "--count", "origin/main") == commit_count
    assert _git(remote, "rev-parse", "refs/heads/main") == remote_before


def test_completeness_uses_the_recorded_agent_judgment(tmp_path: Path) -> None:
    store, trade_id, entry, exit_ = _setup(tmp_path)
    words = [{"authority": "owner_supplied_verbatim", "verbatim_text": "原話", "sha256": WORDS_SHA}]
    record = store.project()["records"][trade_id]
    assert registration_completeness({**record, "owner_statements": words}, root=store.root)["missing"] == ["entry_chart", "exit_chart"]
    store.record_chart_roles(
        trade_id, {"entry_chart": _role(entry), "exit_chart": _role(exit_)},
        source_id="chart-roles-test", confirmed_at=CONFIRMED,
    )
    record = store.project()["records"][trade_id]
    assert registration_completeness({**record, "owner_statements": words}, root=store.root) == {"complete": True, "missing": []}
    # An entry-only judgment leaves the trade incomplete and names the missing exit chart.
    only_entry = {**record, "owner_statements": words, "chart_roles": {"entry_chart": record["chart_roles"]["entry_chart"]}}
    assert registration_completeness(only_entry, root=store.root)["missing"] == ["exit_chart"]


def test_generated_trade_view_files_are_allowed_but_unrelated_paths_are_not() -> None:
    from trading_os.registration_executor import _CHART_ROLES_MUTATION_PATHS as allowed

    for generated in (
        "exports/operational/trade-view/app.js",
        "exports/operational/trade-view/index.html",
        "exports/operational/trade-view/style.css",
    ):
        assert generated in allowed
    for forbidden in ("canonical/records/trades/x.json", "canonical/evidence/artifacts/x.png", "trades/2026/07/x.md", "trading_os/x.py"):
        assert forbidden not in allowed


# --- Owner words that cannot be recovered (acknowledgement, never a substitute) ---------------

REASON = "Registered before the verbatim rule; only an Agent summary survives."


def test_unrecoverable_owner_words_are_recorded_append_only_and_do_not_complete_the_trade(tmp_path: Path) -> None:
    from trading_os.registration_census import registration_acknowledged_gaps

    store, trade_id, entry, exit_ = _setup(tmp_path)
    store.record_chart_roles(trade_id, {"entry_chart": _role(entry), "exit_chart": _role(exit_)}, source_id="charts", confirmed_at=CONFIRMED)
    event = store.record_owner_words_unrecoverable(trade_id, source_id="legacy", confirmed_at=CONFIRMED, reason=REASON)
    record = store.project()["records"][trade_id]
    assert record["owner_words_unrecoverable"]["event_id"] == event["event_id"]
    assert record["owner_words_unrecoverable"]["approval_source"] == "same_conversation_agent_attestation"
    assert record["owner_statements"] == []  # nothing is invented as the Owner's words
    assert registration_acknowledged_gaps(record) == ["owner_words"]
    # Still incomplete: an acknowledgement is not the Owner's words.
    assert registration_completeness(record, root=store.root) == {"complete": False, "missing": ["owner_words"]}


def test_unrecoverable_words_are_refused_when_verbatim_words_exist(tmp_path: Path) -> None:
    store, trade_id, *_ = _setup(tmp_path)
    store.record_owner_statement(trade_id, verbatim_text="原話", source_id="words")
    with pytest.raises(LifecycleError, match="already has the Owner's verbatim words"):
        store.record_owner_words_unrecoverable(trade_id, source_id="legacy", confirmed_at=CONFIRMED, reason=REASON)


@pytest.mark.parametrize("bad", [None, "", "not-a-time"])
def test_unrecoverable_words_require_a_confirmation_time(tmp_path: Path, bad) -> None:
    store, trade_id, *_ = _setup(tmp_path)
    with pytest.raises(LifecycleError, match="confirmed_at"):
        store.record_owner_words_unrecoverable(trade_id, source_id="legacy", confirmed_at=bad, reason=REASON)


@pytest.mark.parametrize("bad", [None, "", "   ", "x" * 2001])
def test_unrecoverable_words_require_a_reason(tmp_path: Path, bad) -> None:
    store, trade_id, *_ = _setup(tmp_path)
    with pytest.raises(LifecycleError, match="reason"):
        store.record_owner_words_unrecoverable(trade_id, source_id="legacy", confirmed_at=CONFIRMED, reason=bad)


def test_unrecoverable_words_retry_is_idempotent_and_changed_content_fails_closed(tmp_path: Path) -> None:
    store, trade_id, *_ = _setup(tmp_path)
    first = store.record_owner_words_unrecoverable(trade_id, source_id="legacy", confirmed_at=CONFIRMED, reason=REASON)
    again = store.record_owner_words_unrecoverable(trade_id, source_id="legacy", confirmed_at=CONFIRMED, reason=REASON)
    assert first["event_id"] == again["event_id"]
    with pytest.raises(LifecycleError, match="changed content"):
        store.record_owner_words_unrecoverable(trade_id, source_id="legacy", confirmed_at=CONFIRMED, reason="a different reason")


def test_a_later_verbatim_statement_ends_the_acknowledgement(tmp_path: Path) -> None:
    from trading_os.registration_census import registration_acknowledged_gaps

    store, trade_id, *_ = _setup(tmp_path)
    store.record_owner_words_unrecoverable(trade_id, source_id="legacy", confirmed_at=CONFIRMED, reason=REASON)
    store.record_owner_statement(trade_id, verbatim_text="後來補的原話", source_id="later")
    assert registration_acknowledged_gaps(store.project()["records"][trade_id]) == []


def test_supplement_plan_accepts_single_and_batch_shapes_and_rejects_bad_input() -> None:
    from trading_os.registration_executor import RegistrationExecutorError, _supplement_plan

    roles = {"entry_chart": {"evidence_refs": ["x"], "rationale": "r"}}
    kind, entries = _supplement_plan({"schema": "chart-roles-confirmation.v1", "trade_id": "a", "chart_roles": roles})
    assert kind == "chart_roles" and [e["trade_id"] for e in entries] == ["a"]
    kind, entries = _supplement_plan({"schema": "chart-roles-confirmation.v2", "items": [
        {"trade_id": "a", "chart_roles": roles}, {"trade_id": "b", "chart_roles": {}},
    ]})
    assert [e["trade_id"] for e in entries] == ["a", "b"]
    kind, entries = _supplement_plan({"schema": "owner-words-unrecoverable.v1", "trade_ids": ["a", "b"], "reason": REASON})
    assert kind == "owner_words_unrecoverable" and all(e["reason"] == REASON for e in entries)
    for bad in (
        {"schema": "nope"},
        {"schema": "chart-roles-confirmation.v2", "items": []},
        {"schema": "chart-roles-confirmation.v2", "items": [{"trade_id": "a", "chart_roles": {}}, {"trade_id": "a", "chart_roles": {}}]},
        {"schema": "owner-words-unrecoverable.v1", "trade_ids": [], "reason": REASON},
        {"schema": "owner-words-unrecoverable.v1", "trade_ids": ["a", "a"], "reason": REASON},
        {"schema": "owner-words-unrecoverable.v1", "trade_ids": [""], "reason": REASON},
    ):
        with pytest.raises(RegistrationExecutorError):
            _supplement_plan(bad)


def test_unrecoverable_words_persist_through_the_git_flow_and_a_wrong_command_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_os.registration_executor import RegistrationExecutorError, record_chart_roles_supplement

    _stub_test_lane(monkeypatch)
    source, remote, trade_id, *_ = _git_chart_roles_fixture(tmp_path)
    request = tmp_path / "unrecoverable.json"
    request.write_text(json.dumps({
        "schema": "owner-words-unrecoverable.v1", "trade_ids": [trade_id], "source_id": "legacy",
        "confirmed_at": CONFIRMED, "reason": REASON,
    }), encoding="utf-8")
    with pytest.raises(RegistrationExecutorError, match="this command records chart_roles"):
        record_chart_roles_supplement(source, request, expected_kind="chart_roles")
    assert _git(source, "rev-parse", "origin/main") == _git(remote, "rev-parse", "refs/heads/main")

    first = record_chart_roles_supplement(source, request, expected_kind="owner_words_unrecoverable")
    again = record_chart_roles_supplement(source, request, expected_kind="owner_words_unrecoverable")
    assert first["status"] == "OWNER_WORDS_UNRECOVERABLE_RECORDED"
    assert first["persistence"]["status"] == "PUSHED"
    assert first["remote_readback"]["status"] == "VERIFIED"
    assert first["remote_readback"]["verified"][0]["kind"] == "owner_words_unrecoverable"
    assert not any(p.startswith(("canonical/records/", "canonical/evidence/")) for p in first["changed_paths"])
    assert again["status"] == "NO_CHANGE" and again["git_commit"] == first["git_commit"]
    assert _git(remote, "rev-parse", "refs/heads/main") == first["git_commit"]


def test_chart_roles_batch_persists_one_commit_for_every_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_os.registration_executor import record_chart_roles_supplement

    _stub_test_lane(monkeypatch)
    source, remote, trade_id, entry, exit_ = _git_chart_roles_fixture(tmp_path)
    request = tmp_path / "batch.json"
    request.write_text(json.dumps({
        "schema": "chart-roles-confirmation.v2", "source_id": "batch", "confirmed_at": CONFIRMED,
        "items": [{"trade_id": trade_id, "chart_roles": {"entry_chart": _role(entry), "exit_chart": _role(exit_)}}],
    }), encoding="utf-8")
    result = record_chart_roles_supplement(source, request, expected_kind="chart_roles")
    assert result["status"] == "CHART_ROLES_RECORDED"
    assert [r["trade_id"] for r in result["results"]] == [trade_id]
    assert result["results"][0]["completeness"]["missing"] == ["owner_words"]
    assert _git(remote, "rev-parse", "refs/heads/main") == result["git_commit"]
