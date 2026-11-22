from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .identifiers import is_record_identifier, is_trade_number, is_uuid7_identifier
from .vocabularies import *


@dataclass(frozen=True)
class ValidationError:
    path: str
    message: str


def _errors_for_enum(value: Any, allowed: set[str], path: str, errors: list[ValidationError]) -> None:
    if value is not None and value not in allowed:
        errors.append(ValidationError(path, f"must be one of {sorted(allowed)}"))


def validate_record(record: Mapping[str, Any]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    required = ("schema_version", "record_id", "record_kind", "trade_id", "observation_id", "trade_no", "classification_status", "type", "execution_environment", "recording_timing", "legacy_source", "identity", "strategy", "plan", "actual", "result", "evidence", "review", "dedupe", "analytics", "raw_legacy", "provenance")
    for key in required:
        if key not in record:
            errors.append(ValidationError(key, "required"))
    if record.get("schema_version") != "trade.v1":
        errors.append(ValidationError("schema_version", "must equal trade.v1"))
    if not is_record_identifier(record.get("record_id")):
        errors.append(ValidationError("record_id", "must be a normalized rec_<hash> identity; src_* is manifest-only"))
    _errors_for_enum(record.get("record_kind"), RECORD_KINDS, "record_kind", errors)
    _errors_for_enum(record.get("classification_status"), CLASSIFICATION_STATUSES, "classification_status", errors)
    _errors_for_enum(record.get("type"), TRADE_TYPES, "type", errors)
    _errors_for_enum(record.get("recording_timing"), {"contemporaneous", "retrospective", "unknown"}, "recording_timing", errors)
    _errors_for_enum(record.get("execution_environment"), {"real_account", "prop_challenge", "paper", "unknown"}, "execution_environment", errors)
    _errors_for_enum(record.get("execution_mode"), EXECUTION_MODES, "execution_mode", errors)
    if record.get("execution_mode") == "PAPER" and record.get("execution_environment") != "paper":
        errors.append(ValidationError("execution_environment", "PAPER records require execution_environment=paper"))
    actual = record.get("actual") or {}
    result = record.get("result") or {}
    _errors_for_enum(actual.get("execution_status"), EXECUTION_STATUSES, "actual.execution_status", errors)
    _errors_for_enum(actual.get("position_status"), POSITION_STATUSES, "actual.position_status", errors)
    _errors_for_enum(result.get("outcome"), OUTCOMES, "result.outcome", errors)
    record_kind = record.get("record_kind")
    if record_kind == "trade_candidate":
        for key in ("trade_id", "observation_id", "trade_no"):
            if record.get(key) is not None:
                errors.append(ValidationError(key, "trade candidates cannot receive permanent identity"))
    if record_kind == "observation":
        if record.get("type") is not None:
            errors.append(ValidationError("type", "observation type must be null"))
        if record.get("trade_id") is not None or record.get("trade_no") is not None:
            errors.append(ValidationError("trade_id/trade_no", "observations cannot receive trade identity or number"))
        if not is_uuid7_identifier(record.get("observation_id"), "obs"):
            errors.append(ValidationError("observation_id", "canonical observation requires obs_<uuid7>"))
    if record_kind == "trade" and record.get("classification_status") == "resolved":
        if record.get("type") not in TRADE_TYPES:
            errors.append(ValidationError("type", "resolved trade requires live or backtest"))
        if not is_uuid7_identifier(record.get("trade_id"), "trd"):
            errors.append(ValidationError("trade_id", "canonical trade requires trd_<uuid7>"))
        if record.get("observation_id") is not None:
            errors.append(ValidationError("observation_id", "trades cannot have observation identity"))
        if not is_trade_number(record.get("trade_no"), record.get("type")):
            errors.append(ValidationError("trade_no", "canonical trade requires a mode-matched permanent number"))
        if actual.get("execution_status") not in {"entered", "exited"}:
            errors.append(ValidationError("actual.execution_status", "canonical trade requires confirmed entry"))
    if record.get("trade_id") is not None and not is_uuid7_identifier(record.get("trade_id"), "trd"):
        errors.append(ValidationError("trade_id", "must be trd_<uuid7>"))
    if record.get("observation_id") is not None and not is_uuid7_identifier(record.get("observation_id"), "obs"):
        errors.append(ValidationError("observation_id", "must be obs_<uuid7>"))
    if record.get("trade_no") is not None and not is_trade_number(record.get("trade_no"), record.get("type")):
        errors.append(ValidationError("trade_no", "number prefix and shape must match resolved type"))
    signal = record.get("signal")
    if signal is not None:
        if signal.get("family") not in {None, "VT"}: errors.append(ValidationError("signal.family", "unsupported signal family"))
        if signal.get("direction") not in {None, "long", "short"}: errors.append(ValidationError("signal.direction", "must be long, short, or null"))
        if signal.get("occurrence") not in {None, "unknown"} and (not isinstance(signal.get("occurrence"), int) or signal.get("occurrence") < 1): errors.append(ValidationError("signal.occurrence", "must be a positive integer or unknown"))
    return errors


def validate_strategy(strategy: Mapping[str, Any]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for key in ("strategy_id", "classification", "status", "rule_versions"):
        if key not in strategy:
            errors.append(ValidationError(key, "required"))
    if not isinstance(strategy.get("strategy_id"), str) or not strategy.get("strategy_id", "").startswith("stg-"):
        errors.append(ValidationError("strategy_id", "must be a stable stg- slug"))
    _errors_for_enum(strategy.get("classification"), STRATEGY_CLASSIFICATIONS, "classification", errors)
    _errors_for_enum(strategy.get("status"), STRATEGY_STATUSES, "status", errors)
    aliases = strategy.get("legacy_aliases")
    if aliases is not None and (not isinstance(aliases, list) or any(not isinstance(item, str) for item in aliases)):
        errors.append(ValidationError("legacy_aliases", "must be a list of strings"))
    parent = strategy.get("parent_strategy_id")
    if parent is not None and (not isinstance(parent, str) or not parent.startswith("stg-")):
        errors.append(ValidationError("parent_strategy_id", "must be a stable stg- slug"))
    versions = strategy.get("rule_versions")
    retired = strategy.get("retired_rule_versions")
    if retired is not None:
        if not isinstance(retired, list) or any(not isinstance(item, str) for item in retired):
            errors.append(ValidationError("retired_rule_versions", "must be a list of version references"))
        elif isinstance(versions, list) and any(item not in versions for item in retired):
            errors.append(ValidationError("retired_rule_versions", "may only retire versions listed in rule_versions"))
    if not isinstance(versions, list):
        errors.append(ValidationError("rule_versions", "must be a list"))
    else:
        for i, version in enumerate(versions):
            if isinstance(version, str):
                if not re.fullmatch(r"stg-[a-z0-9-]+:v[0-9]+", version):
                    errors.append(ValidationError(f"rule_versions[{i}]", "must be a stable version reference"))
                continue
            if not isinstance(version, Mapping):
                errors.append(ValidationError(f"rule_versions[{i}]", "must be an object"))
                continue
            if not re.fullmatch(r"stg-[a-z0-9-]+:v[0-9]+", str(version.get("version_id", ""))) or not isinstance(version.get("rules"), list):
                errors.append(ValidationError(f"rule_versions[{i}]", "requires version_id and rules"))
            for j, rule in enumerate(version.get("rules", [])):
                if not isinstance(rule, Mapping) or not rule.get("rule_id") or not rule.get("operator"):
                    errors.append(ValidationError(f"rule_versions[{i}].rules[{j}]", "requires rule_id and operator"))
                _errors_for_enum((rule or {}).get("category") if isinstance(rule, Mapping) else None, RULE_CATEGORIES, f"rule_versions[{i}].rules[{j}].category", errors)
                if isinstance(rule, Mapping) and rule.get("operator") not in STRATEGY_OPERATORS:
                    errors.append(ValidationError(f"rule_versions[{i}].rules[{j}].operator", "operator is not allow-listed"))
    return errors


def validate_evidence_manifest(manifest: Mapping[str, Any]) -> list[ValidationError]:
    errors=[]
    for key in ("manifest_version", "source_record_id", "relative_path", "media_type", "size_bytes", "sha256", "parser_status", "errors", "warnings"):
        if key not in manifest: errors.append(ValidationError(key, "required"))
    if manifest.get("manifest_version") != "evidence-manifest.v1": errors.append(ValidationError("manifest_version", "must equal evidence-manifest.v1"))
    _errors_for_enum(manifest.get("parser_status"), PARSER_STATUSES, "parser_status", errors)
    return errors


def validate_adjudication(decision: Mapping[str, Any]) -> list[ValidationError]:
    errors=[]
    for key in ("adjudication_version", "adjudication_id", "subject_refs", "decision_type", "before", "after", "decision", "reason", "evidence_refs", "reviewer", "decided_at", "supersedes"):
        if key not in decision: errors.append(ValidationError(key, "required"))
    if decision.get("adjudication_version") != "adjudication.v1": errors.append(ValidationError("adjudication_version", "must equal adjudication.v1"))
    _errors_for_enum(decision.get("decision_type"), DECISION_TYPES, "decision_type", errors)
    _errors_for_enum(decision.get("decision"), DECISIONS, "decision", errors)
    if not isinstance(decision.get("subject_refs"), list) or not decision.get("subject_refs"): errors.append(ValidationError("subject_refs", "must be non-empty list"))
    return errors
