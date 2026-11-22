"""Agent-facing Review orchestration built on the shared tool-neutral service.

Owner approval belongs to the conversation, not to a CLI. A confirmed
review uses the same writer, projection checks and retry path through any
supported adapter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .review_authorization import (
    OwnerReviewAuthorization,
    ReviewAuthorizationError,
    verify_owner_review_authorization,
)
from .review_service import prepare_review, commit_review

TrustedOwnerApproval = Callable[[Mapping[str, Any]], OwnerReviewAuthorization]


def preview_review(
    root: Path, trade_id: str, review: Mapping[str, Any],
    *, evidence_intake: Path | None = None,
) -> dict[str, Any]:
    """Return a read-only preview without invoking CLI."""
    return prepare_review(root, trade_id, review, evidence_intake=evidence_intake)


def save_confirmed_review(
    root: Path,
    trade_id: str,
    review: Mapping[str, Any],
    *,
    reviewed_at: str,
    shown_preview_digest: str,
    trusted_owner_approval: TrustedOwnerApproval | None = None,
    agent_approval: Mapping[str, Any] | None = None,
    evidence_intake: Path | None = None,
) -> dict[str, Any]:
    """Finish approved review through the sole shared service.

    The original digest may also repair an already-persisted identical review
    after a projection failure; no second Owner approval is requested.
    """

    def host_confirmation(preview: Mapping[str, Any]) -> OwnerReviewAuthorization:
        if trusted_owner_approval is None:
            raise ReviewAuthorizationError("No authenticated Owner confirmation was supplied")
        authorization = trusted_owner_approval(preview)
        verify_owner_review_authorization(
            authorization,
            authorization.confirmation_payload() if isinstance(authorization, OwnerReviewAuthorization) else None,
            expected_digest=shown_preview_digest,
        )
        if authorization.channel != "local_owner_interaction":
            raise ReviewAuthorizationError("Local trusted host approval requires the active Owner interaction")
        return authorization

    return commit_review(
        root, trade_id, review,
        reviewed_at=reviewed_at,
        shown_preview_digest=shown_preview_digest,
        agent_approval=agent_approval,
        trusted_owner_approval=host_confirmation if trusted_owner_approval is not None else None,
        evidence_intake=evidence_intake,
    )
