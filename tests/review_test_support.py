"""Synthetic-only helpers that exercise the production Owner confirmation gate."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from trading_os.evidence_store import EvidenceStore

from trading_os.review_preview import (
    ANALYSIS_SCHEMA,
    _relevant_rules,
    _strategy_pin,
    prepare_review_preview,
)
from trading_os.review_authorization import OwnerReviewAuthorization, _authorize_local_review_after_owner_confirmation


def review_candidate(store: Any, trade_id: str, review: Mapping[str, Any]) -> dict[str, Any]:
    """Build a fixture-only review with an explicit unknown-first analysis."""
    projection = store.project() if callable(getattr(store, "project", None)) else store.current()
    record = projection["records"][trade_id]
    pin, rules = _strategy_pin(store.root, record)
    compliance = dict(review).get("rule_compliance")
    chart_refs = [
        {"sha256": item["sha256"], "source_ref": item["source_ref"]}
        for item in record.get("evidence", [])
        if isinstance(item, Mapping) and item.get("type") in {"chart_snapshot", "replay_chart"}
    ]
    diagnostic = dict(review).get("diagnostic_class")
    deviation = dict(review).get("execution_deviation")
    explicit_execution_deviation = (
        compliance == "violation"
        and diagnostic == "EXECUTION"
        and isinstance(deviation, str)
        and deviation.strip().casefold() not in {"", "none", "none observed", "unknown"}
    )
    evidence_supported = bool(chart_refs) and compliance in {"compliant", "violation"}
    rules_for_review = _relevant_rules(rules)
    strategy_violation_row = next(
        (row["rule_id"] for row in rules_for_review if row["importance"] == "required" and row["lane"] == "setup"),
        None,
    )
    execution_violation_row = next(
        (row["rule_id"] for row in rules_for_review if row["importance"] == "required" and row["lane"] == "execution"),
        None,
    )
    checklist = []
    for row in rules_for_review:
        required = row["importance"] == "required"
        status = "UNKNOWN"
        rationale = "Synthetic fixture leaves this rule unobserved."
        if evidence_supported and required:
            status = "YES"
            rationale = "Synthetic fixture evidence supports this required condition."
            if compliance == "violation" and not explicit_execution_deviation and row["rule_id"] == strategy_violation_row:
                status = "NO"
                rationale = "Synthetic fixture evidence supports a strategy-condition failure."
            elif explicit_execution_deviation and row["rule_id"] == execution_violation_row:
                status = "NO"
                rationale = "Synthetic fixture evidence supports an execution-rule failure."
        checklist.append({
            **row,
            "rule_version": pin["rule_version"],
            "status": status,
            "evidence_refs": chart_refs[:1] if evidence_supported and required else [],
            "rationale": rationale,
        })
    required_setup = [row for row in checklist if row["importance"] == "required" and row["lane"] == "setup"]
    setup_quality = (
        "invalid" if any(row["status"] == "NO" for row in required_setup)
        else "unknown" if any(row["status"] in {"UNKNOWN", "N/A"} for row in required_setup)
        else "valid" if pin["status"] == "PINNED" and required_setup
        else "not_applicable" if pin["status"] == "PINNED" and not required_setup
        else "unknown" if pin["status"] == "PINNED" else "not_observable"
    )
    candidate = {
        **dict(review),
        "review_analysis": {
            "schema_version": ANALYSIS_SCHEMA,
            "strategy_pin": pin,
            "checklist": checklist,
            "setup_quality": setup_quality,
            "execution_quality": (
                "deviated" if compliance == "violation" and dict(review).get("execution_deviation") not in {None, "none", "none observed"}
                else "aligned" if compliance == "compliant" else "unknown"
            ),
            "agent_assessment": {
                "setup": "Synthetic fixture: required setup conditions are supported by the cited chart evidence." if evidence_supported else "Synthetic fixture: not observed.",
                "risk": "Synthetic fixture: not observed.",
                "execution": "Synthetic fixture: execution deviation is recorded separately." if compliance == "violation" else "Synthetic fixture: execution aligned with the recorded plan." if compliance == "compliant" else "Synthetic fixture: not observed.",
                "exit": "Synthetic fixture: not observed.",
                "lesson": "Synthetic fixture: no inference.",
            },
            "owner_decision_verbatim": "Synthetic test Owner confirms this preview.",
        },
    }
    if explicit_execution_deviation:
        candidate["review_analysis"]["execution_deviation_support"] = {
            "authority": "owner_attestation",
            "owner_statement": candidate["review_analysis"]["owner_decision_verbatim"],
            "evidence_refs": [],
            "rationale": "Synthetic Owner explicitly attests this execution deviation.",
        }
    return candidate


def owner_review_authorization(store: Any, trade_id: str, candidate: Mapping[str, Any]) -> OwnerReviewAuthorization:
    preview = prepare_review_preview(store.root, store, trade_id, candidate)
    # This fixture stands in for the trusted conversational adapter: the
    # capability is minted only after the Owner confirms the complete preview.
    return _authorize_local_review_after_owner_confirmation(preview)


def owner_confirmed_review(
    store: Any,
    trade_id: str,
    review: Mapping[str, Any],
    *,
    reviewed_at: str,
    via_registered_writer: bool = False,
):
    """Build a fixture-only unknown-first review and confirm its exact digest."""
    candidate = review_candidate(store, trade_id, review)
    authorization = owner_review_authorization(store, trade_id, candidate)
    confirmation = authorization.confirmation_payload()
    if via_registered_writer:
        from trading_os.trade_review import review_registered_trade

        return review_registered_trade(
            store,
            trade_id,
            candidate,
            reviewed_at=reviewed_at,
            owner_confirmation=confirmation,
            owner_authorization=authorization,
        )
    return store.review(
        trade_id,
        candidate,
        reviewed_at=reviewed_at,
        owner_confirmation=confirmation,
        owner_authorization=authorization,
    )


def attach_fixture_image(
    store: Any,
    draft_id: str,
    *,
    source_ref: str,
    category: str = "pre_trade_context",
    timing: str = "pre_trade",
    captured_at: str,
) -> dict[str, Any]:
    """Attach exact synthetic PNG bytes for fixtures that model chart evidence."""
    Path(store.root).mkdir(parents=True, exist_ok=True)
    raw = b"\x89PNG\r\n\x1a\n" + hashlib.sha256(source_ref.encode("utf-8")).digest()
    path = Path(store.root) / ("fixture-chart-" + hashlib.sha256(source_ref.encode("utf-8")).hexdigest() + ".png")
    path.write_bytes(raw)
    info = EvidenceStore(store.root).ingest(path)
    store.attach_evidence(
        draft_id,
        {
            **info,
            "source_ref": source_ref,
            "category": category,
            "timing": timing,
            "type": "chart_snapshot",
            "captured_at": captured_at,
        },
    )
    return info
