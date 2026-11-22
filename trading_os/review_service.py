"""Transport-neutral, independently executable single-trade Review workflow.

The Owner decides whether to accept the Agent's analysis; the Agent records that
decision. CLI and Python are interchangeable adapters. The same service owns
canonical write, projection closure, read-back and identical retry recovery.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .evidence_store import EvidenceStore
from .operational_lifecycle import LifecycleStore, RULE_COMPLIANCE_VALUES
from .projections import refresh_projections, verify_projections
from .review_authorization import (
    OwnerReviewAuthorization,
    ReviewAuthorizationError,
    authorize_agent_review,
    verify_owner_review_authorization,
)
from .review_preview import (
    prepare_review_evidence_additions,
    prepare_review_preview,
    verify_owner_confirmation,
)
from .trade_review import review_registered_trade


def prepare_review(
    root: Path,
    trade_id: str,
    review: Mapping[str, Any],
    *,
    evidence_intake: Path | None = None,
) -> dict[str, Any]:
    """Prepare a read-only Owner-facing preview without invoking a CLI."""
    root = Path(root).resolve()
    return prepare_review_preview(
        root, LifecycleStore(root), trade_id, review,
        evidence_additions=prepare_review_evidence_additions(root, evidence_intake),
    )


def _same_review(record: Mapping[str, Any], review: Mapping[str, Any]) -> bool:
    saved = dict(record.get("review") or {})
    for key in ("schema_version", "reviewer", "review_status", "owner_confirmation", "owner_authorization"):
        saved.pop(key, None)
    candidate = dict(review)
    # LifecycleStore always normalizes an accepted review_status to 'reviewed'.
    # It is not a material change when the candidate included that field.
    if candidate.get("review_status") in (None, "reviewed"):
        candidate.pop("review_status", None)
    return saved == candidate


def _check_identical_retry(
    record: Mapping[str, Any],
    trade_id: str,
    review: Mapping[str, Any],
    *,
    agent_approval: Mapping[str, Any] | None,
    shown_preview_digest: str | None,
    additions: list[Mapping[str, Any]],
) -> None:
    """Allow only the exact previously approved content to repair projections."""
    if not _same_review(record, review):
        raise ReviewAuthorizationError(
            "Already-reviewed trade differs from the confirmed review; use the correction workflow"
        )
    saved_review = record.get("review") or {}
    confirmed = saved_review.get("owner_confirmation") or {}
    authorized = saved_review.get("owner_authorization") or {}
    digest = confirmed.get("preview_digest")
    if not isinstance(digest, str):
        raise ReviewAuthorizationError("Previously reviewed trade has no auditable preview digest")
    if shown_preview_digest is not None and shown_preview_digest != digest:
        raise ReviewAuthorizationError("Already-reviewed trade differs from the confirmed preview digest")
    if agent_approval is not None:
        # The repeated Agent decision must be the same one that wrote the review.
        if (
            not isinstance(agent_approval, Mapping)
            or set(agent_approval) != {
                "schema_version", "decision", "trade_id", "preview_digest",
                "confirmed_by", "confirmed_at", "agent_id",
            }
            or agent_approval.get("schema_version") != "agent-review-approval.v1"
            or agent_approval.get("decision") != "approved"
            or agent_approval.get("trade_id") != trade_id
            or agent_approval.get("preview_digest") != digest
            or agent_approval.get("confirmed_by") != confirmed.get("confirmed_by")
            or agent_approval.get("confirmed_at") != confirmed.get("confirmed_at")
            or authorized.get("channel") != "agent_delegated_review"
            or authorized.get("source") != f"agent:{agent_approval.get('agent_id')}:owner_confirmed_preview"
        ):
            raise ReviewAuthorizationError("Review retry approval differs from the original approved decision")
    elif shown_preview_digest is None:
        raise ReviewAuthorizationError("Review retry requires the previously confirmed preview digest or Agent approval")
    attached = [x for x in (record.get("evidence") or []) if isinstance(x, Mapping)]
    for addition in additions:
        if not any(
            all(existing.get(key) == value for key, value in addition.items() if key != "source_path")
            for existing in attached
        ):
            raise ReviewAuthorizationError("Review retry contains changed or unbound image evidence")


def _finish(
    root: Path,
    store: LifecycleStore,
    trade_id: str,
    review: Mapping[str, Any],
    *,
    written: Mapping[str, Any],
    status: str,
) -> dict[str, Any]:
    refreshed = refresh_projections(root, target_trade=trade_id)
    verified = verify_projections(root, target_trade=trade_id)
    final = store.current().get("records", {}).get(trade_id)
    if not isinstance(final, Mapping) or final.get("state") != "REVIEWED" or not _same_review(final, review):
        raise ValueError("canonical review read-back differs from the confirmed result")
    if verified.get("status") != "CURRENT":
        raise ValueError("review projection verification is stale")
    return {
        "status": status,
        "written": dict(written),
        "event_type": written.get("event_type"),
        "refresh": refreshed,
        "verify": verified,
    }


def _preflight_writer_inputs(review: Mapping[str, Any], reviewed_at: str, recorded_at: str | None) -> None:
    """Reject writer-invalid inputs before ingesting any newly supplied image bytes.

    LifecycleStore remains the sole authority and repeats these checks before
    canonical append. This early guard prevents an avoidable partial evidence
    attachment when the requested review could never be written.
    """
    allowed = {
        "what_went_well", "what_went_wrong", "rule_compliance",
        "execution_deviation", "thesis_validity", "entry_quality",
        "exit_quality", "evidence_completeness", "owner_notes",
        "review_status", "diagnostic_class", "review_analysis",
    }
    if not isinstance(review, Mapping) or set(review) - allowed:
        raise ReviewAuthorizationError("review contains unsupported fields")
    if review.get("rule_compliance") not in RULE_COMPLIANCE_VALUES:
        raise ReviewAuthorizationError("invalid canonical rule_compliance")
    if review.get("diagnostic_class") not in {
        None, "METHOD", "EXECUTION", "PROCESS-RISK", "INSUFFICIENT", "MIXED",
    }:
        raise ReviewAuthorizationError("invalid canonical diagnostic_class")
    if review.get("review_status") not in {None, "reviewed"}:
        raise ReviewAuthorizationError("review_status must be reviewed")
    for field, value in (("reviewed_at", reviewed_at), ("recorded_at", recorded_at)):
        if field == "recorded_at" and value is None:
            continue
        if not isinstance(value, str):
            raise ReviewAuthorizationError(field + " must be a timezone-aware ISO-8601 timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReviewAuthorizationError(field + " must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ReviewAuthorizationError(field + " must include a timezone")


def commit_review(
    root: Path,
    trade_id: str,
    review: Mapping[str, Any],
    *,
    reviewed_at: str,
    agent_approval: Mapping[str, Any] | None = None,
    trusted_owner_approval: Callable[[Mapping[str, Any]], OwnerReviewAuthorization] | None = None,
    shown_preview_digest: str | None = None,
    evidence_intake: Path | None = None,
    reviewer: str = "owner",
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Complete an Owner-approved review, independently of the chosen tool.

    Successful return means canonical write + projection refresh + independent
    verification + read-back. If closure fails after the append, repeating the
    exact approved review repairs projections without a second review event.
    """
    _preflight_writer_inputs(review, reviewed_at, recorded_at)
    if agent_approval is not None and trusted_owner_approval is not None:
        raise ReviewAuthorizationError("Choose one Owner confirmation channel")
    root = Path(root).resolve()
    store = LifecycleStore(root)
    additions = prepare_review_evidence_additions(root, evidence_intake)
    current = store.current().get("records", {}).get(trade_id, {})
    if isinstance(current, Mapping) and current.get("state") == "REVIEWED":
        _check_identical_retry(
            current, trade_id, review, agent_approval=agent_approval,
            shown_preview_digest=shown_preview_digest, additions=additions,
        )
        return _finish(
            root, store, trade_id, review,
            written={"status": "ALREADY_REVIEWED"}, status="ALREADY_REVIEWED",
        )
    if (agent_approval is None) == (trusted_owner_approval is None):
        raise ReviewAuthorizationError(
            "No confirmed Agent decision or trusted host approval was supplied"
        )

    attached = current.get("evidence", []) if isinstance(current, Mapping) else []
    pending = []
    for addition in additions:
        matches = [
            item for item in attached
            if isinstance(item, Mapping)
            and item.get("sha256") == addition.get("sha256")
            and item.get("source_ref") == addition.get("source_ref")
            and item.get("category") == addition.get("category")
        ]
        if not (
            matches and any(
                all(item.get(key) == addition.get(key) for key in addition if key != "source_path")
                for item in matches
            )
        ):
            pending.append(addition)
    preview = prepare_review_preview(root, store, trade_id, review, evidence_additions=pending)
    if shown_preview_digest is not None and preview["preview_digest"] != shown_preview_digest:
        raise ReviewAuthorizationError(
            "Review content or evidence changed since the Owner preview; a new approval is required"
        )
    authorization = (
        authorize_agent_review(preview, agent_approval)
        if agent_approval is not None else trusted_owner_approval(preview)
    )
    confirmation = authorization.confirmation_payload()
    verify_owner_review_authorization(
        authorization, confirmation, expected_digest=preview["preview_digest"],
    )
    if pending:
        verify_owner_confirmation(
            root, store, trade_id, review, confirmation, evidence_additions=pending,
        )
        evidence_store = EvidenceStore(root)
        for addition in pending:
            info = evidence_store.describe(
                addition["source_path"], original_filename=addition["original_filename"],
            )
            if any(
                info.get(key) != addition.get(key)
                for key in ("sha256", "locator", "media_type", "byte_size", "original_filename")
            ):
                raise ValueError("review evidence bytes changed after Owner confirmation")
        draft_id = store.resolve_draft_id(trade_id)
        for addition in pending:
            info = evidence_store.ingest(
                addition["source_path"],
                original_filename=addition["original_filename"],
                expected_sha256=addition["sha256"],
            )
            if any(
                info.get(key) != addition.get(key)
                for key in ("sha256", "locator", "media_type", "byte_size", "original_filename")
            ):
                raise ValueError("review evidence bytes changed during ingest")
            store.attach_evidence(
                draft_id, {key: value for key, value in addition.items() if key != "source_path"},
            )
    written = review_registered_trade(
        store, trade_id, review,
        reviewed_at=reviewed_at, owner_confirmation=confirmation,
        owner_authorization=authorization, reviewer=reviewer, recorded_at=recorded_at,
    )
    return _finish(
        root, store, trade_id, review,
        written=written, status="REVIEW_COMPLETE",
    )
