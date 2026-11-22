"""Owner authorization for Review writes.

A preview digest binds content; it is not an approval credential. Runtime
writers accept an authorization object minted from an accountable Agent
attestation of an explicit Owner decision or a trusted host confirmation.
Historical reviews minted from the retired Owner-issue channel stay readable.
The digest binds content;
Agent attestation is not cryptographic authentication of a chat participant.
The authorization object itself cannot be carried in a JSON request file.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

OWNER_LOGIN = "example-owner"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEAL = object()


class ReviewAuthorizationError(ValueError):
    """A review write lacks an Owner action from a supported channel."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class OwnerReviewAuthorization:
    confirmed_by: str
    confirmed_at: str
    preview_digest: str
    owner_action_at: str
    channel: str
    source: str
    _seal: object = field(repr=False, compare=False)

    def confirmation_payload(self) -> dict[str, str]:
        return {
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "preview_digest": self.preview_digest,
        }

    def audit_record(self) -> dict[str, str]:
        return {
            "schema_version": "owner-review-authorization.v1",
            **self.confirmation_payload(),
            "owner_action_at": self.owner_action_at,
            "channel": self.channel,
            "source": self.source,
        }


def _mint(
    *,
    confirmed_by: str,
    confirmed_at: str,
    preview_digest: str,
    owner_action_at: str,
    channel: str,
    source: str,
) -> OwnerReviewAuthorization:
    return OwnerReviewAuthorization(
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
        preview_digest=preview_digest,
        owner_action_at=owner_action_at,
        channel=channel,
        source=source,
        _seal=_SEAL,
    )


def _authorize_local_review_after_owner_confirmation(
    preview: Mapping[str, Any],
) -> OwnerReviewAuthorization:
    """Mint an ephemeral local capability ONLY for a trusted interaction host.

    An authenticated host must check the Owner's affirmative action on the
    complete preview before calling this PRIVATE helper. This helper cannot
    authenticate a chat message. Never expose it as an Agent/chat tool;
    the bare CLI and untrusted chat transports cannot mint this capability.
    """
    digest = preview.get("preview_digest")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ReviewAuthorizationError("a valid review preview is required before local Owner confirmation")
    confirmed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return _mint(
        confirmed_by=OWNER_LOGIN,
        confirmed_at=confirmed_at,
        preview_digest=digest,
        owner_action_at=confirmed_at,
        channel="local_owner_interaction",
        source="active_review_confirmation",
    )


def authorize_review_from_conversation(
    preview: Mapping[str, Any],
    *,
    shown_preview_digest: str,
    owner_reply_verbatim: str,
) -> OwnerReviewAuthorization:
    """Deprecated fail-closed adapter: an Agent-supplied string is not Owner proof.

    Only a trusted interaction host may grant the local capability. A generic
    chat transcript or verbatim string is not an authenticated event, even when
    its words say "confirm" and its digest matches. The GitHub route instead
    verifies the Owner-authored Actions issue event.
    """
    raise ReviewAuthorizationError(
        "A conversation reply string cannot authorize a Review write; "
        "use a trusted host approval callback or the Owner-authored GitHub approval route"
    )


def authorize_agent_review(
    preview: Mapping[str, Any], approval: Mapping[str, Any],
) -> OwnerReviewAuthorization:
    """Agent-attested Owner decision, independent of CLI or host callbacks.

    The Agent is accountable for having shown the exact preview and obtained
    Owner confirmation in its active interaction. This is a delegation record,
    NOT independent cryptographic proof of who wrote a chat message. The
    application/tool may choose a separate stronger identity mechanism.
    """
    required = {
        "schema_version", "decision", "trade_id", "preview_digest",
        "confirmed_by", "confirmed_at", "agent_id",
    }
    if not isinstance(approval, Mapping) or set(approval) != required:
        raise ReviewAuthorizationError("a complete Agent-attested review approval is required")
    if approval["schema_version"] != "agent-review-approval.v1" or approval["decision"] != "approved":
        raise ReviewAuthorizationError("Agent review approval must be an explicit affirmative decision")
    if approval["trade_id"] != preview.get("trade_id"):
        raise ReviewAuthorizationError("Agent approval does not match the selected trade")
    digest = preview.get("preview_digest")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None or approval["preview_digest"] != digest:
        raise ReviewAuthorizationError("Agent approval does not match the exact preview digest")
    if approval["confirmed_by"] != OWNER_LOGIN:
        raise ReviewAuthorizationError("Agent cannot attribute confirmation to a different Owner")
    agent_id = approval["agent_id"]
    if not isinstance(agent_id, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", agent_id) is None:
        raise ReviewAuthorizationError("Agent approval requires an accountable agent_id")
    confirmed_at = approval["confirmed_at"]
    if not isinstance(confirmed_at, str):
        raise ReviewAuthorizationError("Agent approval requires an ISO-8601 confirmation time")
    try:
        timestamp = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewAuthorizationError("Agent approval time is invalid") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ReviewAuthorizationError("Agent approval time must have a timezone")
    return _mint(
        confirmed_by=OWNER_LOGIN, confirmed_at=confirmed_at, preview_digest=digest,
        owner_action_at=confirmed_at, channel="agent_delegated_review",
        source=f"agent:{agent_id}:owner_confirmed_preview",
    )


def verify_owner_review_authorization(
    authorization: OwnerReviewAuthorization | None,
    confirmation: Mapping[str, Any] | None,
    *,
    expected_digest: str | None = None,
) -> dict[str, str]:
    if (
        not isinstance(authorization, OwnerReviewAuthorization)
        or authorization._seal is not _SEAL
        or authorization.channel not in {"local_owner_interaction", "github_owner_issue_event", "agent_delegated_review"}
    ):
        raise ReviewAuthorizationError("a validated active-interaction Owner confirmation or Owner-authored GitHub approval is required")
    if not isinstance(confirmation, Mapping) or dict(confirmation) != authorization.confirmation_payload():
        raise ReviewAuthorizationError("Owner confirmation fields do not match the validated approval action")
    if expected_digest is not None and authorization.preview_digest != expected_digest:
        raise ReviewAuthorizationError("Owner authorization is not bound to the recomputed preview digest")
    return authorization.audit_record()
