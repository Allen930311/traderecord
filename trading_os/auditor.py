from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .vocabularies import STRATEGY_OPERATORS


_TIMEFRAME_ORDER = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
_POST_TRADE_FIELD_MARKERS = (
    "result",
    "realized_r",
    "r_realized",
    "profit",
    "outcome",
    "exit_reason",
    "actual.exit",
    "full_position_exit",
)


def _get(obj: Mapping[str, Any], dotted: str) -> Any:
    value: Any = obj
    for part in dotted.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _token(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _base_result(
    trade: Mapping[str, Any],
    strategy: Mapping[str, Any],
    version: Mapping[str, Any],
    evidence: list[dict[str, Any]],
    explanation_codes: list[str],
    registry: Mapping[str, Any] | None,
) -> dict[str, Any]:
    registry_value = registry if registry is not None else strategy
    return {
        "audit_version": "rule-audit.v1",
        "audit_id": "aud_" + _digest([trade, version, evidence, registry_value])[:24],
        "trade_id": trade.get("trade_id"),
        "strategy_id": strategy.get("strategy_id"),
        "strategy_version": version.get("version_id"),
        "matched_variant": _get(trade, "strategy.variant"),
        "match_confidence": "high" if version.get("status") == "active" else "medium",
        "rule_matrix": [],
        "required_rule_compliance": {
            "passed": 0,
            "failed": 0,
            "unknown": 0,
            "applicable": 0,
            "score": None,
        },
        "evidence_sufficiency": "unknown",
        "classification": "unknown",
        "verdict_semantics": "compliance_only_not_causal_attribution",
        "missing_evidence": [],
        "registry_digest": _digest(registry_value),
        "trade_revision_digest": _digest(trade),
        "explanation_codes": explanation_codes,
        "outcome_used": False,
    }


def _is_post_trade_field(field_name: str) -> bool:
    normalized = field_name.lower()
    return any(
        normalized == marker or normalized.startswith(marker + ".")
        for marker in _POST_TRADE_FIELD_MARKERS
    )


def _assertion_value(item: Mapping[str, Any], operator: str) -> Any:
    if "assertion_value" in item:
        return item.get("assertion_value")
    if operator in {"tag_present", "tag_absent"} and "tag" in item:
        return item.get("tag")
    return None


def _evidence_assertion_matches(item: Mapping[str, Any], operator: str) -> bool:
    """Require an explicit assertion matching the rule's evaluated field."""

    if "field" not in item or "assertion" not in item:
        return False
    if "assertion_value" not in item and not (operator in {"tag_present", "tag_absent"} and "tag" in item):
        return False
    assertion = str(item.get("assertion"))
    compatible = {
        "equals": {"equals"},
        "not_equals": {"not_equals"},
        "in": {"in", "contains"},
        "not_in": {"not_in", "not_contains"},
        "gte": {"gte"},
        "lte": {"lte"},
        "between": {"between"},
        "timeframe_gte": {"timeframe_gte", "gte"},
        "exists": {"exists"},
        "tag_present": {"tag_present", "contains"},
        "tag_absent": {"tag_absent", "absent"},
    }
    if assertion not in compatible.get(operator, set()):
        return False
    # The value is checked against the canonical field below.  Do not compare
    # it to the rule target here: a correctly bound assertion of the actual
    # value must be able to produce a deterministic FAIL, and two different
    # bound assertions must remain visible as a conflict.
    return _assertion_value(item, operator) is not None


def _eligible_evidence(
    evidence: list[dict[str, Any]],
    field_name: str,
    operator: str,
    allowed_timings: set[str],
    methods: set[str],
) -> list[dict[str, Any]]:
    eligible: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        timing = item.get("timing", item.get("recording_timing", "unknown"))
        if timing not in allowed_timings or item.get("method") not in methods:
            continue
        if item.get("field") != field_name:
            # A missing field is not a wildcard.  This is deliberately a
            # hard binding because an image about another fact must not prove
            # this rule.
            continue
        if not item.get("source_ref") or not item.get("locator") or not item.get("sha256"):
            continue
        if not _evidence_assertion_matches(item, operator):
            continue
        eligible.append(dict(item))
    return eligible


def _rule_status(
    trade: Mapping[str, Any],
    rule: Mapping[str, Any],
    evidence: list[dict[str, Any]],
) -> tuple[str, str, list[dict[str, Any]], list[str], str | None, Any]:
    operator = str(rule.get("operator", ""))
    if operator not in STRATEGY_OPERATORS:
        return "UNKNOWN", "operator_not_approved_v1", [], [], None, None
    operands = rule.get("operands") if isinstance(rule.get("operands"), Mapping) else {}
    field_name = str(operands.get("field", ""))
    expected = operands.get("value")
    if operator in {"in", "not_in"}:
        expected = operands.get("values", [])
    if operator == "between":
        expected = operands.get("range", operands.get("values"))
    if operator in {"tag_present", "tag_absent"} and field_name != "strategy.setup_tags":
        return "UNKNOWN", "tag_rule_field_not_bound", [], [], field_name or None, expected
    if not field_name:
        return "UNKNOWN", "evaluated_field_missing", [], [], None, expected
    if rule.get("category") == "setup" and _is_post_trade_field(field_name):
        return "UNKNOWN", "setup_rule_post_trade_field", [], [], field_name, expected

    allowed_timings = set(rule.get("allowed_evidence_timing", ["pre_trade", "execution"]))
    methods = set(rule.get("evidence_requirements", []))
    actual = _get(trade, field_name)
    eligible = _eligible_evidence(evidence, field_name, operator, allowed_timings, methods)
    refs = [item.get("source_ref") for item in eligible if item.get("source_ref")]
    values = {_token(_assertion_value(item, operator)) for item in eligible}
    if len(values) > 1:
        return "UNKNOWN", "conflicting_eligible_evidence", eligible, refs, field_name, expected
    if methods and not eligible:
        has_same_method = any(item.get("method") in methods for item in evidence if isinstance(item, Mapping))
        code = "evidence_field_or_assertion_mismatch" if has_same_method else "evidence_missing_or_invalid"
        return "UNKNOWN", code, [], [], field_name, expected

    if actual is not None and eligible:
        if operator in {"tag_present", "tag_absent"}:
            assertions_match_rule = all(_assertion_value(item, operator) == expected for item in eligible)
        elif operator == "exists":
            assertions_match_rule = all(
                _assertion_value(item, operator) in {True, False, "present", "absent"}
                for item in eligible
            )
        else:
            assertions_match_rule = all(
                _token(_assertion_value(item, operator)) == _token(actual)
                for item in eligible
            )
        if not assertions_match_rule:
            return "UNKNOWN", "evidence_assertion_conflict", eligible, refs, field_name, expected
    if operator == "exists":
        status = "PASS" if actual is not None else "FAIL"
        code = "field_present" if status == "PASS" else "field_absent"
    elif actual is None:
        return "UNKNOWN", "evaluated_field_missing", eligible, refs, field_name, expected
    elif operator == "equals":
        status = "PASS" if actual == expected else "FAIL"
        code = "comparison_" + status.lower()
    elif operator == "not_equals":
        status = "PASS" if actual != expected else "FAIL"
        code = "comparison_" + status.lower()
    elif operator == "in":
        status = "PASS" if actual in (expected or []) else "FAIL"
        code = "membership_" + status.lower()
    elif operator == "not_in":
        status = "PASS" if actual not in (expected or []) else "FAIL"
        code = "membership_" + status.lower()
    elif operator in {"gte", "lte"}:
        try:
            status = "PASS" if (actual >= expected if operator == "gte" else actual <= expected) else "FAIL"
        except TypeError:
            return "UNKNOWN", "comparison_not_comparable", eligible, refs, field_name, expected
        code = "comparison_" + status.lower()
    elif operator == "between":
        bounds = expected if isinstance(expected, list) else []
        if len(bounds) != 2:
            return "UNKNOWN", "between_bounds_invalid", eligible, refs, field_name, expected
        try:
            status = "PASS" if bounds[0] <= actual <= bounds[1] else "FAIL"
        except TypeError:
            return "UNKNOWN", "comparison_not_comparable", eligible, refs, field_name, expected
        code = "between_" + status.lower()
    elif operator == "timeframe_gte":
        target = operands.get("value")
        if actual not in _TIMEFRAME_ORDER or target not in _TIMEFRAME_ORDER:
            return "UNKNOWN", "timeframe_unrecognized", eligible, refs, field_name, expected
        status = "PASS" if _TIMEFRAME_ORDER[actual] >= _TIMEFRAME_ORDER[target] else "FAIL"
        code = "timeframe_" + status.lower()
    elif operator == "tag_present":
        status = "PASS" if expected in actual else "FAIL"
        code = "tag_" + status.lower()
    elif operator == "tag_absent":
        status = "PASS" if expected not in actual else "FAIL"
        code = "tag_" + status.lower()
    else:
        return "UNKNOWN", "operator_unsupported", eligible, refs, field_name, expected
    return status, code, eligible, refs, field_name, expected


def audit_trade(
    trade: dict,
    strategy: dict,
    version: dict,
    evidence: list[dict] | None = None,
    registry: dict | None = None,
) -> dict:
    evidence = evidence if evidence is not None else trade.get("evidence", [])
    if not isinstance(evidence, list):
        evidence = []
    result = _base_result(trade, strategy, version, evidence, [], registry)
    requested_strategy_id = strategy.get("strategy_id")
    trade_strategy_id = _get(trade, "strategy.strategy_id")
    version_strategy_id = version.get("strategy_id")
    trade_version = _get(trade, "strategy.rule_version")
    version_id = version.get("version_id")
    if not all(isinstance(value, str) and value for value in (requested_strategy_id, trade_strategy_id, version_strategy_id)) or len({requested_strategy_id, trade_strategy_id, version_strategy_id}) != 1:
        result["explanation_codes"] = ["trade_strategy_binding_mismatch"]
        return result
    if not isinstance(version_id, str) or trade_version != version_id:
        result["explanation_codes"] = ["trade_version_binding_mismatch"]
        return result

    matrix: list[dict[str, Any]] = []
    for rule in version.get("rules", []):
        if rule.get("status") == "inactive":
            continue
        if rule.get("applicable") is False:
            matrix.append({
                "rule_id": rule.get("rule_id"),
                "status": "N/A",
                "required": bool(rule.get("required", False)),
                "evidence_refs": [],
                "missing_evidence": [],
                "explanation_code": "explicitly_not_applicable",
                "category": rule.get("category", "unknown"),
                "evaluated_field": (rule.get("operands") or {}).get("field"),
                "evaluated_assertion": rule.get("operands", {}),
            })
            continue
        methods = set(rule.get("evidence_requirements", []))
        status, code, eligible, refs, field_name, expected = _rule_status(trade, rule, evidence)
        observed_methods = {item.get("method") for item in eligible}
        missing = sorted(methods - observed_methods) if methods else []
        matrix.append({
            "rule_id": rule.get("rule_id"),
            "status": status,
            "required": bool(rule.get("required", False)),
            "evidence_refs": refs,
            "missing_evidence": missing,
            "explanation_code": code,
            "category": rule.get("category", "unknown"),
            "evaluated_field": field_name,
            "evaluated_assertion": {"operator": rule.get("operator"), "value": expected},
        })

    required = [item for item in matrix if item["required"] and item["status"] != "N/A"]
    known = [item for item in required if item["status"] in {"PASS", "FAIL"}]
    failed = [item for item in required if item["status"] == "FAIL"]
    categories = sorted({item["category"] for item in failed})
    if any(item["status"] == "UNKNOWN" for item in required):
        classification = "unknown"
    elif len(categories) != 1 and failed:
        classification = "unknown"
    elif failed:
        classification = {
            "execution": "execution_error",
            "risk": "risk_error",
            "management": "management_error",
            "behaviour": "behaviour_error",
        }.get(categories[0], "unknown")
    else:
        classification = "clean_strategy"
    result.update({
        "rule_matrix": matrix,
        "required_rule_compliance": {
            "passed": sum(item["status"] == "PASS" for item in required),
            "failed": len(failed),
            "unknown": sum(item["status"] == "UNKNOWN" for item in required),
            "applicable": len(required),
            "score": sum(item["status"] == "PASS" for item in known) / len(known) if known else None,
        },
        "evidence_sufficiency": "high" if all(item["evidence_refs"] or not item["required"] for item in matrix) else "low",
        "classification": classification,
        "missing_evidence": sorted({missing for item in matrix for missing in item["missing_evidence"]}),
        "explanation_codes": sorted({item["explanation_code"] for item in matrix}),
    })
    return result
