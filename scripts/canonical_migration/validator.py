"""Validation rules for the fixture migration substrate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .execution_verification import (
    ExecutionVerificationReport,
    result_metric_eligibility_gate,
    result_metric_minimum_floor,
    result_metrics_gate,
    sequence_metrics_gate,
    trade_count_gate,
    validate_execution_contract,
    validate_execution_verification,
    verification_disclosure,
)
from .normalizer import TYPE_VALUES


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "ValidationReport") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


def _has_reviewable_chart(record: dict[str, Any], role: str) -> bool:
    return any(
        item.get("chart_role") == role
        and item.get("source_available") is True
        and item.get("logic_relevance") == f"{role}_logic"
        for item in record.get("evidence_capture", [])
    )


def canonical_trade_gate(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return whether a post-adjudication record may become canonical."""

    reasons: list[str] = []
    if record.get("record_kind") != "trade":
        reasons.append("canonicalization_requires_executed_trade")
    if record.get("type") not in TYPE_VALUES:
        reasons.append("canonicalization_requires_resolved_type")
    if record.get("classification_status") != "resolved":
        reasons.append("canonicalization_requires_resolved_classification")
    actual = record.get("actual") or {}
    nested_verification = actual.get("execution_verification")
    if isinstance(nested_verification, dict):
        execution_report = validate_execution_contract(record)
        if not execution_report.ok:
            reasons.append("canonicalization_requires_valid_execution_verification")
        entry = nested_verification.get("entry")
        if not isinstance(entry, dict) or entry.get("state") != "confirmed":
            reasons.append("canonicalization_requires_confirmed_entry_execution")
    else:
        # Phase 3 records still carry this scalar as a compatibility field.
        # It is not the Phase 4B.1 SSOT and is only accepted on legacy records
        # that have not adopted the nested contract yet.
        if nested_verification != "confirmed":
            reasons.append("canonicalization_requires_confirmed_execution")
        if actual.get("entry_execution_confirmed") is not True:
            reasons.append("canonicalization_requires_confirmed_entry_execution")
    if actual.get("execution_status") not in {"entered", "exited"} or actual.get("entry_price") is None:
        reasons.append("canonicalization_requires_entered_or_exited_position")
    if not _has_reviewable_chart(record, "entry"):
        reasons.append("canonicalization_requires_entry_chart")
    if actual.get("position_status") == "closed" and not _has_reviewable_chart(record, "exit"):
        reasons.append("canonicalization_requires_exit_chart")
    return not reasons, reasons


def validate_record(record: dict[str, Any]) -> ValidationReport:
    report = ValidationReport()
    record_id = record.get("record_id", "<missing>")
    prefix = f"{record_id}: "
    required = {
        "schema_version",
        "record_id",
        "record_kind",
        "trade_id",
        "observation_id",
        "trade_no",
        "type",
        "classification_status",
        "legacy_source",
        "strategy",
        "plan",
        "actual",
        "result",
        "evidence_capture",
        "review",
        "dedupe",
        "analytics",
        "strategy_learning_eligibility",
        "raw_legacy",
    }
    missing = sorted(required - set(record))
    if missing:
        report.errors.append(prefix + "missing fields: " + ", ".join(missing))
        return report

    # The Phase 3 normalizer intentionally emits scalar compatibility fields.
    # When a record adopts the Phase 4B.1 nested object, validate it here as a
    # real contract; do not force a historical backfill merely to run this
    # source-candidate validator.
    if isinstance((record.get("actual") or {}).get("execution_verification"), dict):
        execution_report = validate_execution_contract(record)
        report.errors.extend(prefix + error for error in execution_report.errors)
        report.warnings.extend(prefix + warning for warning in execution_report.warnings)

    if record.get("type") not in TYPE_VALUES | {None}:
        report.errors.append(prefix + "type must be live, backtest or null")
    if record.get("type") is None and record.get("classification_status") != "pending":
        report.errors.append(prefix + "null type requires classification_status=pending")
    if record.get("record_kind") == "observation":
        if record.get("type") is not None:
            report.errors.append(prefix + "observation type must be null")
        if record.get("trade_id") is not None or record.get("trade_no") is not None:
            report.errors.append(prefix + "observation cannot have trade_id/trade_no")
    if record.get("trade_no") is not None:
        report.errors.append(prefix + "pilot must not allocate a human trade_no")
    if record.get("strategy_learning_eligibility") not in {"eligible", "excluded", "pending", "unknown"}:
        report.errors.append(prefix + "strategy_learning_eligibility has an invalid value")
    if record.get("record_kind") == "observation" and record.get("analytics", {}).get("eligibility") != "excluded":
        report.errors.append(prefix + "observation must be excluded from trade-performance analytics")
    if record.get("record_kind") == "observation" and record.get("strategy_learning_eligibility") == "eligible":
        research_evidence = any(item.get("source_available") is True for item in record.get("evidence_capture", []))
        if not research_evidence or record.get("review", {}).get("state") not in {"partial", "complete"}:
            report.errors.append(prefix + "observation research eligibility requires evidence and review value")
    if not _has_reviewable_chart(record, "entry") and record.get("strategy_learning_eligibility") == "eligible":
        report.errors.append(prefix + "missing Entry Chart cannot be strategy-learning eligible")
    if (record.get("actual") or {}).get("position_status") == "closed" and not _has_reviewable_chart(record, "exit") and record.get("strategy_learning_eligibility") == "eligible":
        report.errors.append(prefix + "missing Exit Chart cannot be strategy-learning eligible")
    if record.get("recording_timing") == "replay":
        report.errors.append(prefix + "replay must be evidence provenance, not recording_timing")
    if any(item.get("method") == "replay" for item in record.get("evidence_capture", [])):
        report.errors.append(prefix + "replay must use method=replay_chart")

    source = record.get("legacy_source") or {}
    for field in ("repository", "path", "filename", "content_sha256", "original_frontmatter", "original_frontmatter_text"):
        if field not in source:
            report.errors.append(prefix + f"legacy_source.{field} is required")
    raw = record.get("raw_legacy") or {}
    if not isinstance(raw.get("fields"), dict):
        report.errors.append(prefix + "raw_legacy.fields must retain a mapping")

    evidence = record.get("evidence_capture") or []
    for index, item in enumerate(evidence):
        for field in ("method", "chart_role", "source_ref", "assertion", "logic_relevance", "confidence", "recording_timing"):
            if field not in item:
                report.errors.append(prefix + f"evidence[{index}].{field} is required")
        if item.get("method") == "replay_chart" and item.get("recording_timing") != "retrospective":
            report.errors.append(prefix + f"evidence[{index}] replay_chart must be retrospective provenance")

    canonical_status = (record.get("canonicalization") or {}).get("status")
    if canonical_status == "canonical":
        gate_ok, gate_reasons = canonical_trade_gate(record)
        if not gate_ok:
            report.errors.extend(prefix + reason for reason in gate_reasons)
        if not (record.get("trade_id")):
            report.errors.append(prefix + "canonical trade must have trade_id")
    if canonical_status == "observation":
        if record.get("record_kind") != "observation":
            report.errors.append(prefix + "observation resolution must reference an observation")
        if not record.get("observation_id"):
            report.errors.append(prefix + "resolved observation must have observation_id")
    if canonical_status in {"source_candidate", "candidate", "blocked", "observation"} and record.get("trade_id") is not None:
        report.errors.append(prefix + "non-canonical candidate cannot have trade_id")

    if (record.get("actual") or {}).get("position_status") == "closed" and not _has_reviewable_chart(record, "exit"):
        if "exit_chart_missing" not in (raw.get("parse_warnings") or []):
            report.errors.append(prefix + "closed record must make missing Exit Chart explicit")
    if not _has_reviewable_chart(record, "entry") and record.get("record_kind") != "observation":
        if "entry_chart_missing" not in (raw.get("parse_warnings") or []):
            report.errors.append(prefix + "missing Entry Chart must be explicit")

    dedupe_status = (record.get("dedupe") or {}).get("status")
    if dedupe_status == "duplicate" and not (record.get("dedupe") or {}).get("duplicate_of"):
        report.errors.append(prefix + "duplicate status requires an adjudicated duplicate_of")
    return report


# Public re-exports keep the validator module as the stable import surface for
# callers that already use ``canonical_migration.validator``.
__all__ = [
    "ExecutionVerificationReport",
    "ValidationReport",
    "canonical_trade_gate",
    "result_metric_eligibility_gate",
    "result_metric_minimum_floor",
    "result_metrics_gate",
    "sequence_metrics_gate",
    "trade_count_gate",
    "validate_execution_contract",
    "validate_execution_verification",
    "validate_record",
    "validate_run",
    "verification_disclosure",
]


def validate_run(records: Iterable[dict[str, Any]], source_manifest: Iterable[dict[str, Any]]) -> ValidationReport:
    report = ValidationReport()
    seen_ids: set[str] = set()
    for record in records:
        record_id = record.get("record_id")
        if record_id in seen_ids:
            # A source may appear in two fixtures, but the normalized output is
            # deduplicated before this validator is called.
            report.errors.append(f"duplicate normalized record_id: {record_id}")
        seen_ids.add(record_id)
        report.extend(validate_record(record))

    seen_paths: set[str] = set()
    for entry in source_manifest:
        path = entry.get("path")
        if path in seen_paths:
            report.errors.append(f"duplicate source manifest path: {path}")
        seen_paths.add(path)
        if not entry.get("sha256"):
            report.errors.append(f"source manifest missing sha256: {path}")
        if "raw_frontmatter" not in entry or "raw_fields" not in entry:
            report.errors.append(f"source manifest missing raw values: {path}")
    return report
