from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import ValidationError, validate_adjudication
from .identifiers import new_identifier


# An adjudication is an append-only assertion about one domain.  The reducer
# must not allow a convenient ``after`` dict to become a second write path for
# another domain (for example, a strategy decision changing execution or
# outcome fields).
AUTHORITY_ALLOWED_FIELDS: dict[str, set[str]] = {
    "identity": {
        "record_id", "record_kind", "identity", "identity_status",
        "source_candidates", "source_relationship", "relationship",
        "independent_trades", "is_distinct", "same_as",
    },
    "duplicate": {
        "canonical_source", "canonical_record_id", "duplicate_sources",
        "duplicate_of", "duplicate_group", "dedupe_status", "source_candidates",
        "source_refs", "record_ids", "trades", "same_as",
        "canonical_record_reuse", "status",
    },
    "type": {"type", "trade_type", "type_status"},
    "date": {
        "date_policy", "dates", "date_conflicts", "event_date",
        "recorded_date", "event_date_source", "frontmatter_date_semantics",
        "event_date_and_recorded_date_are_distinct",
        "frontmatter_dates_retained_as_raw_provenance",
    },
    "strategy": {
        "strategy", "strategy_id", "strategy_candidate", "strategy_version",
        "variant", "variant_status", "classification", "implementation_id",
        "parity_status", "aliases", "components", "component_name",
        "independent_system", "setup_tags", "rule_violation",
        "violation_reason", "alias_status", "promoted", "review_state",
        "legacy_strategy_raw", "labels",
    },
    "evidence": {
        "evidence", "evidence_refs", "evidence_status", "entry_evidence_status",
        "exit_evidence_status", "entry_chart_reviewable", "exit_chart_reviewable",
        "field_provenance",
    },
    "rejection": {
        "classification_status", "rejection_reason", "analytics_eligibility",
        "status", "review_state",
    },
    "trade_number_void": {"trade_no", "tombstone", "void_reason", "number_status"},
}

# These names are never treated as harmless grouping keys when they occur in
# a domain that does not own them.  This is what makes nested fixture wrappers
# such as ``f01`` safe without allowing ``f01: {type: ...}`` to leak through.
RESTRICTED_FIELDS = {
    "type", "trade_id", "observation_id", "trade_no", "strategy_id",
    "strategy_candidate", "strategy_version", "variant", "execution_status",
    "position_status", "exit_reason", "outcome", "realized_r", "full_position_exit",
    "actual", "result", "execution", "fill", "profit", "r_realized",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _project_value(
    value: Any,
    allowed: set[str],
    path: str,
    blocked: list[str],
) -> Any:
    """Return the portion of *value* owned by one adjudication domain.

    Unknown mapping keys are retained only as grouping keys when they contain
    at least one owned descendant.  Unknown scalar keys and restricted names
    are dropped and reported.  Lists preserve scalar values (for example
    ``duplicate_sources``) but apply the same recursion to mapping members.
    """

    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}" if path else key
            if key in allowed:
                projected[key] = _project_value(child, allowed, child_path, blocked)
                continue
            if key in RESTRICTED_FIELDS:
                blocked.append(child_path)
                continue
            if isinstance(child, (Mapping, list)):
                nested = _project_value(child, allowed, child_path, blocked)
                if nested not in ({}, []):
                    projected[key] = nested
                else:
                    blocked.append(child_path)
                continue
            blocked.append(child_path)
        return projected

    if isinstance(value, list):
        projected_list: list[Any] = []
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            if isinstance(child, (Mapping, list)):
                nested = _project_value(child, allowed, child_path, blocked)
                if nested not in ({}, []):
                    projected_list.append(nested)
                else:
                    blocked.append(child_path)
            else:
                projected_list.append(child)
        return projected_list

    return value


def project_after(decision_type: str, after: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Project an ``after`` patch onto the fields owned by ``decision_type``."""

    allowed = AUTHORITY_ALLOWED_FIELDS.get(decision_type)
    if allowed is None:
        return {}, [f"<unknown decision type: {decision_type}>"]
    projected = _project_value(after or {}, allowed, "", blocked := [])
    return projected if isinstance(projected, dict) else {}, blocked


def validate_adjudication_authority(decision: Mapping[str, Any]) -> list[ValidationError]:
    """Validate the domain authority of an adjudication patch.

    ``validate_adjudication`` remains the shape validator for historical
    immutable files.  New writes and the public apply command use this stricter
    validator so legacy contaminated artifacts cannot be applied again.
    """

    decision_type = decision.get("decision_type")
    if not isinstance(decision.get("after"), Mapping):
        return [ValidationError("after", "must be an object")]
    _, blocked = project_after(str(decision_type), decision.get("after"))
    return [
        ValidationError(
            f"after.{path}",
            f"field is outside {decision_type!r} adjudication authority",
        )
        for path in blocked
    ]


def _validate_for_write(payload: Mapping[str, Any]) -> None:
    errors = validate_adjudication(payload)
    errors.extend(validate_adjudication_authority(payload))
    if errors:
        raise ValueError("invalid adjudication: " + "; ".join(e.message for e in errors))


def append_decision(
    root: Path,
    subject_refs: list[str],
    decision_type: str,
    decision: str,
    before: dict[str, Any],
    after: dict[str, Any],
    reason: str,
    evidence_refs: list[str],
    reviewer: str,
    supersedes: str | None = None,
) -> dict[str, Any]:
    if not reviewer or not reason:
        raise ValueError("reviewer and reason are required")
    payload = {
        "adjudication_version": "adjudication.v1",
        "adjudication_id": new_identifier("adj"),
        "subject_refs": subject_refs,
        "decision_type": decision_type,
        "before": before,
        "after": after,
        "decision": decision,
        "reason": reason,
        "evidence_refs": evidence_refs,
        "reviewer": reviewer,
        "decided_at": _now(),
        "supersedes": supersedes,
    }
    _validate_for_write(payload)
    directory = root / "canonical" / "adjudications"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{payload['adjudication_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return payload


def apply_decision_file(root: Path, source: Path) -> dict[str, Any]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    _validate_for_write(payload)
    directory = root / "canonical" / "adjudications"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{payload['adjudication_id']}.json"
    if target.exists():
        raise ValueError("adjudication already exists")
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return payload


def reduce_decisions(decisions: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reduce an ordered append-only log with supersession and fail-closed patches."""

    items = list(decisions)
    superseded = {
        item.get("supersedes")
        for item in items
        if isinstance(item.get("supersedes"), str) and item.get("supersedes")
    }
    state: dict[str, dict[str, Any]] = {}
    for decision in sorted(items, key=lambda item: (item.get("decided_at", ""), item.get("adjudication_id", ""))):
        if decision.get("adjudication_id") in superseded:
            continue
        decision_type = decision.get("decision_type")
        if decision_type in AUTHORITY_ALLOWED_FIELDS:
            after, _ = project_after(str(decision_type), decision.get("after"))
        else:
            # Keep compatibility with the tiny historical reducer API while
            # refusing to apply an unknown v1 adjudication domain.
            if decision_type is not None:
                continue
            after = decision.get("after") or {}
        for ref in decision.get("subject_refs", []):
            current = state.setdefault(ref, {})
            current.update(after)
            current["last_adjudication_id"] = decision.get("adjudication_id")
    return state
