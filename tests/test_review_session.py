from __future__ import annotations

from pathlib import Path

import pytest

from review_test_support import review_candidate
from test_operational_closeout import _make_fixture
from trading_os.review_authorization import (
    ReviewAuthorizationError,
    _authorize_local_review_after_owner_confirmation,
    authorize_review_from_conversation,
)
import trading_os.review_service as service
from trading_os.review_session import preview_review, save_confirmed_review
from trading_os.review_service import commit_review
from trading_os.__main__ import main as review_cli_main
import json

BASE = {
    "what_went_well": "plan was recorded before entry",
    "what_went_wrong": "none observed",
    "rule_compliance": "compliant",
    "execution_deviation": "none observed",
    "thesis_validity": "supported by verified close",
    "entry_quality": "observed",
    "exit_quality": "observed",
    "evidence_completeness": "complete",
    "owner_notes": "chat confirmation",
    "diagnostic_class": "METHOD",
}


def _setup(tmp_path: Path):
    source = Path(__file__).resolve().parents[1]
    store, _, trade_id = _make_fixture(source, tmp_path, write_review=False)
    return store, trade_id, review_candidate(store, trade_id, dict(BASE))


def test_preview_is_read_only_then_chat_confirmation_saves(tmp_path: Path) -> None:
    store, trade_id, review = _setup(tmp_path)
    events_before = len(store._read_events())
    preview = preview_review(tmp_path, trade_id, review)
    assert len(store._read_events()) == events_before
    result = save_confirmed_review(
        tmp_path, trade_id, review, reviewed_at="2026-09-11T00:08:00Z",
        shown_preview_digest=preview["preview_digest"], trusted_owner_approval=_authorize_local_review_after_owner_confirmation,
    )
    record = store.current()["records"][trade_id]
    assert record["state"] == "REVIEWED"
    assert record["review"]["owner_authorization"]["source"] == "active_review_confirmation"
    assert result["verify"]["status"] == "CURRENT"


def test_changed_review_after_owner_saw_preview_is_rejected(tmp_path: Path) -> None:
    store, trade_id, review = _setup(tmp_path)
    preview = preview_review(tmp_path, trade_id, review)
    changed = {**review, "owner_notes": "edited after the Owner approved"}
    with pytest.raises((ReviewAuthorizationError, SystemExit)):
        save_confirmed_review(
            tmp_path, trade_id, changed, reviewed_at="2026-09-11T00:08:00Z",
            shown_preview_digest=preview["preview_digest"], trusted_owner_approval=_authorize_local_review_after_owner_confirmation,
        )
    assert store.current()["records"][trade_id]["state"] == "CLOSED"


def test_missing_owner_reply_is_rejected(tmp_path: Path) -> None:
    store, trade_id, review = _setup(tmp_path)
    preview = preview_review(tmp_path, trade_id, review)
    with pytest.raises((ReviewAuthorizationError, SystemExit)):
        save_confirmed_review(
            tmp_path, trade_id, review, reviewed_at="2026-09-11T00:08:00Z",
            shown_preview_digest=preview["preview_digest"], trusted_owner_approval=None,
        )
    assert store.current()["records"][trade_id]["state"] == "CLOSED"


@pytest.mark.parametrize("reply", ["確認保存", "不要保存", "先等等", "有問題，先修改", "  "])
def test_untrusted_conversation_reply_never_authorizes_a_write(tmp_path: Path, reply: str) -> None:
    store, trade_id, review = _setup(tmp_path)
    preview = preview_review(tmp_path, trade_id, review)
    with pytest.raises(ReviewAuthorizationError, match="cannot authorize"):
        authorize_review_from_conversation(
            preview, shown_preview_digest=preview["preview_digest"], owner_reply_verbatim=reply,
        )
    with pytest.raises(ReviewAuthorizationError, match="No confirmed Agent decision"):
        save_confirmed_review(
            tmp_path, trade_id, review,
            reviewed_at="2026-09-11T00:08:00Z",
            shown_preview_digest=preview["preview_digest"],
        )
    assert store.project()["records"][trade_id]["state"] == "CLOSED"


def test_first_pass_preview_needs_no_owner_final_decision(tmp_path: Path) -> None:
    store, trade_id, review = _setup(tmp_path)
    review["review_analysis"].pop("owner_decision_verbatim")
    preview = preview_review(tmp_path, trade_id, review)
    assert "owner_decision_verbatim" not in preview["content"]["review_candidate"]
    assert store.project()["records"][trade_id]["state"] == "CLOSED"


def test_projection_failure_recovers_without_rewriting_review(tmp_path: Path, monkeypatch) -> None:
    store, trade_id, review = _setup(tmp_path)
    preview = preview_review(tmp_path, trade_id, review)
    real_refresh = service.refresh_projections
    fail_once = {"remaining": 1}

    def interrupted(root, *, target_trade=None):
        if fail_once["remaining"]:
            fail_once["remaining"] -= 1
            raise RuntimeError("simulated projection interruption")
        return real_refresh(root, target_trade=target_trade)

    monkeypatch.setattr(service, "refresh_projections", interrupted)
    with pytest.raises(RuntimeError, match="simulated projection interruption"):
        save_confirmed_review(
            tmp_path, trade_id, review,
            reviewed_at="2026-09-11T00:08:00Z",
            shown_preview_digest=preview["preview_digest"],
            trusted_owner_approval=_authorize_local_review_after_owner_confirmation,
        )
    before = [event for event in store._read_events() if event["event_type"] == "reviewed"]
    assert len(before) == 1
    assert store.project()["records"][trade_id]["state"] == "REVIEWED"

    recovered = save_confirmed_review(
        tmp_path, trade_id, review,
        reviewed_at="2026-09-11T00:08:00Z",
        shown_preview_digest=preview["preview_digest"],
    )
    assert recovered["written"]["status"] == "ALREADY_REVIEWED"
    assert recovered["verify"]["status"] == "CURRENT"
    after = [event for event in store._read_events() if event["event_type"] == "reviewed"]
    assert len(after) == 1


def test_projection_recovery_rejects_changed_review_and_digest(tmp_path: Path) -> None:
    store, trade_id, review = _setup(tmp_path)
    preview = preview_review(tmp_path, trade_id, review)
    save_confirmed_review(
        tmp_path, trade_id, review,
        reviewed_at="2026-09-11T00:08:00Z",
        shown_preview_digest=preview["preview_digest"],
        trusted_owner_approval=_authorize_local_review_after_owner_confirmation,
    )
    with pytest.raises(ReviewAuthorizationError, match="differs"):
        save_confirmed_review(
            tmp_path, trade_id, {**review, "owner_notes": "unapproved change"},
            reviewed_at="2026-09-11T00:08:00Z",
            shown_preview_digest=preview["preview_digest"],
        )
    with pytest.raises(ReviewAuthorizationError, match="differs"):
        save_confirmed_review(
            tmp_path, trade_id, review,
            reviewed_at="2026-09-11T00:08:00Z",
            shown_preview_digest="0" * 64,
        )


def _delegated_approval(preview, agent_id="codex"):
    return {
        "schema_version": "agent-review-approval.v1",
        "decision": "approved",
        "trade_id": preview["trade_id"],
        "preview_digest": preview["preview_digest"],
        "confirmed_by": "example-owner",
        "confirmed_at": "2026-10-11T04:00:00+08:00",
        "agent_id": agent_id,
    }


def test_agent_after_owner_confirmation_saves_without_cli_or_host_callback(tmp_path: Path, monkeypatch) -> None:
    store, trade_id, review = _setup(tmp_path)
    preview = preview_review(tmp_path, trade_id, review)

    def forbid_cli(*args, **kwargs):
        raise AssertionError("shared service must not depend on CLI entrypoint")

    monkeypatch.setattr("trading_os.__main__.main", forbid_cli)
    committed = commit_review(
        tmp_path, trade_id, review,
        reviewed_at="2026-10-11T04:00:02+08:00",
        agent_approval=_delegated_approval(preview),
    )
    record = store.current()["records"][trade_id]
    assert committed["event_type"] == "reviewed"
    assert committed["status"] == "REVIEW_COMPLETE"
    assert committed["refresh"]["status"] == "CURRENT"
    assert committed["verify"]["status"] == "CURRENT"
    assert record["state"] == "REVIEWED"
    assert record["review"]["owner_authorization"]["channel"] == "agent_delegated_review"
    assert record["review"]["owner_authorization"]["source"] == "agent:codex:owner_confirmed_preview"


def test_agent_review_session_commits_then_verifies_projections(tmp_path: Path) -> None:
    store, trade_id, review = _setup(tmp_path)
    preview = preview_review(tmp_path, trade_id, review)
    result = save_confirmed_review(
        tmp_path, trade_id, review,
        reviewed_at="2026-10-11T04:00:02+08:00",
        shown_preview_digest=preview["preview_digest"],
        agent_approval=_delegated_approval(preview),
    )
    assert result["verify"]["status"] == "CURRENT"
    assert store.current()["records"][trade_id]["state"] == "REVIEWED"


@pytest.mark.parametrize("field,value", [
    ("decision", "declined"),
    ("trade_id", "trd_other"),
    ("preview_digest", "0" * 64),
    ("confirmed_by", "somebody_else"),
    ("agent_id", ""),
])
def test_invalid_delegated_approval_cannot_write(tmp_path: Path, field: str, value: str) -> None:
    store, trade_id, review = _setup(tmp_path)
    preview = preview_review(tmp_path, trade_id, review)
    decision = {**_delegated_approval(preview), field: value}
    with pytest.raises((ReviewAuthorizationError, SystemExit)):
        save_confirmed_review(
            tmp_path, trade_id, review,
            reviewed_at="2026-10-11T04:00:02+08:00",
            shown_preview_digest=preview["preview_digest"],
            agent_approval=decision,
        )
    assert store.current()["records"][trade_id]["state"] == "CLOSED"


def test_agent_approval_cannot_cover_changed_final_review(tmp_path: Path) -> None:
    store, trade_id, review = _setup(tmp_path)
    preview = preview_review(tmp_path, trade_id, review)
    with pytest.raises(ReviewAuthorizationError, match="changed since the Owner preview"):
        save_confirmed_review(
            tmp_path, trade_id, {**review, "owner_notes": "different after approval"},
            reviewed_at="2026-10-11T04:00:02+08:00",
            shown_preview_digest=preview["preview_digest"],
            agent_approval=_delegated_approval(preview),
        )
    assert store.current()["records"][trade_id]["state"] == "CLOSED"


def test_cli_is_optional_adapter_using_same_review_service(tmp_path: Path, monkeypatch, capsys) -> None:
    store, trade_id, review = _setup(tmp_path)
    review["review_status"] = "reviewed"
    preview = preview_review(tmp_path, trade_id, review)
    review_file = tmp_path / "review-for-cli.json"
    approval_file = tmp_path / "agent-review-decision.json"
    review_file.write_text(json.dumps(review), encoding="utf-8")
    approval_file.write_text(json.dumps(_delegated_approval(preview)), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    review_cli_main([
        "capture", "review", "--trade-id", trade_id,
        "--reviewed-at", "2026-10-11T04:00:02+08:00",
        "--review-file", str(review_file),
        "--approval-file", str(approval_file),
    ])
    assert store.current()["records"][trade_id]["state"] == "REVIEWED"
    first_result = json.loads(capsys.readouterr().out)
    assert first_result["status"] == "REVIEW_COMPLETE"
    assert first_result["verify"]["status"] == "CURRENT"
    events_before = len([event for event in store._read_events() if event["event_type"] == "reviewed"])
    review_cli_main([
        "capture", "review", "--trade-id", trade_id,
        "--reviewed-at", "2026-10-11T04:00:02+08:00",
        "--review-file", str(review_file),
        "--approval-file", str(approval_file),
    ])
    retry_result = json.loads(capsys.readouterr().out)
    assert retry_result["status"] == "ALREADY_REVIEWED"
    assert retry_result["verify"]["status"] == "CURRENT"
    assert len([event for event in store._read_events() if event["event_type"] == "reviewed"]) == events_before


def test_shared_direct_retry_rebuilds_projection_without_second_event(tmp_path: Path, monkeypatch) -> None:
    store, trade_id, review = _setup(tmp_path)
    preview = preview_review(tmp_path, trade_id, review)
    approval = _delegated_approval(preview)
    real_refresh = service.refresh_projections
    attempts = {"count": 0}

    def fail_first_refresh(root, *, target_trade=None):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("post-write projection failure")
        return real_refresh(root, target_trade=target_trade)

    monkeypatch.setattr(service, "refresh_projections", fail_first_refresh)
    with pytest.raises(RuntimeError, match="post-write projection failure"):
        commit_review(
            tmp_path, trade_id, review,
            reviewed_at="2026-10-11T04:00:02+08:00",
            agent_approval=approval,
        )
    assert store.current()["records"][trade_id]["state"] == "REVIEWED"
    before = len([e for e in store._read_events() if e["event_type"] == "reviewed"])
    retry = commit_review(
        tmp_path, trade_id, review,
        reviewed_at="2026-10-11T04:00:02+08:00",
        agent_approval=approval,
    )
    assert retry["status"] == "ALREADY_REVIEWED"
    assert retry["refresh"]["status"] == "CURRENT"
    assert retry["verify"]["status"] == "CURRENT"
    assert len([e for e in store._read_events() if e["event_type"] == "reviewed"]) == before


def test_direct_retry_rejects_changed_candidate_and_different_agent_approval(tmp_path: Path) -> None:
    store, trade_id, review = _setup(tmp_path)
    preview = preview_review(tmp_path, trade_id, review)
    approval = _delegated_approval(preview)
    commit_review(
        tmp_path, trade_id, review,
        reviewed_at="2026-10-11T04:00:02+08:00",
        agent_approval=approval,
    )
    with pytest.raises(ReviewAuthorizationError, match="differs"):
        commit_review(
            tmp_path, trade_id, {**review, "owner_notes": "unconfirmed revision"},
            reviewed_at="2026-10-11T04:00:02+08:00",
            agent_approval=approval,
        )
    with pytest.raises(ReviewAuthorizationError, match="differs"):
        commit_review(
            tmp_path, trade_id, review,
            reviewed_at="2026-10-11T04:00:02+08:00",
            agent_approval={**approval, "agent_id": "unrelated-agent"},
        )
    assert len([e for e in store._read_events() if e["event_type"] == "reviewed"]) == 1


@pytest.mark.parametrize(
    "patch,reviewed_at",
    [
        ({"review_status": "pending"}, "2026-09-11T00:08:00Z"),
        ({"rule_violation": True}, "2026-09-11T00:08:00Z"),
        ({"diagnostic_class": "MADE_UP"}, "2026-09-11T00:08:00Z"),
        ({}, "2026-09-11T00:08:00"),
        ({}, "not-a-timestamp"),
    ],
)
def test_writer_invalid_inputs_fail_before_review_events(
    tmp_path: Path, patch: dict, reviewed_at: str,
) -> None:
    store, trade_id, review = _setup(tmp_path)
    review = {**review, **patch}
    events_before = len(store._read_events())
    with pytest.raises(ReviewAuthorizationError):
        commit_review(
            tmp_path, trade_id, review,
            reviewed_at=reviewed_at,
            trusted_owner_approval=_authorize_local_review_after_owner_confirmation,
        )
    assert len(store._read_events()) == events_before
    assert store.project()["records"][trade_id]["state"] == "CLOSED"
