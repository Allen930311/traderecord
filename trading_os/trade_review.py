"""Trade Review entrypoint: post-trade review of an already-registered canonical trade.

This is a guard around ``LifecycleStore.review`` -- the single canonical review
writer. It never creates provider identity, drafts or T-numbers; registration
(``unified_capture.capture_live``) is a separate workflow that leaves
``review.status == "not_started"``.
"""
from __future__ import annotations

from typing import Any, Mapping

from .operational_lifecycle import LifecycleError, LifecycleStore


def review_registered_trade(
    store: LifecycleStore,
    trade_id: str,
    review: Mapping[str, Any],
    *,
    reviewed_at: str,
    owner_confirmation: Mapping[str, Any] | None = None,
    owner_authorization: Any = None,
    reviewer: str = "owner",
    recorded_at: str | None = None,
) -> dict[str, Any]:
    record = store.current().get("records", {}).get(trade_id) if isinstance(trade_id, str) and trade_id.startswith("trd_") else None
    if not isinstance(record, Mapping) or not record.get("trade_no"):
        raise LifecycleError("Trade Review only accepts an already-registered canonical trade (trd_ id with a T-number)")
    return store.review(
        trade_id,
        review,
        reviewed_at=reviewed_at,
        owner_confirmation=owner_confirmation,
        owner_authorization=owner_authorization,
        reviewer=reviewer,
        recorded_at=recorded_at,
    )
