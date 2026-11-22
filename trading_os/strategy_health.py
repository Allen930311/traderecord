"""Pre-declared, fail-closed strategy health and improvement-candidate projection."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .operational_analytics import load_current


HEALTH_SCHEMA = "strategy-health.v1"
HEALTH_MANIFEST_SCHEMA = "strategy-health-manifest.v1"
DEFAULT_OUTPUT = Path("exports/operational/health")
POLICY = {
    "policy_id": "strategy-health-policy.v1",
    "minimum_count": 10,
    "minimum_live_count": 1,
    "recent_window_days": 90,
    "recent_window_minimum_count": 5,
    "review_completeness_required": 0.8,
    "minimum_operational_method_eligible_sample": 5,
    "minimum_operational_reviewed_sample": 5,
    "minimum_failure_pattern_count": 3,
    "failure_pattern_rate": 0.6,
    "failure_condition": "realized_r < 0 on reviewed, rule-compliant, method-observable post-cutover observations",
    "failure_component_fields": ["setup_tags", "confirmation_tags"],
    "method_health_requires_machine_observable_required_rules": True,
    "execution_error_separation": "execution/process-risk observations cannot be attributed to method",
    "candidate_eligibility": {
        "minimum_total_sample": 10,
        "minimum_confidence": "not_insufficient",
        "requires_machine_observable_operational_method": True,
        "requires_reviewed_compliant_method": True,
        "requires_evidence_refs": True,
        "no_optimization": True,
        "no_hindsight_mutation": True,
    },
    "candidate_multiplicity": {
        "zero": "no_eligible_candidate",
        "one": "ready_for_backtest_review",
        "many": "multiple_candidates_require_review",
        "selection": "none",
    },
    "states": ["stable", "watch", "investigate", "insufficient_evidence", "not_observable"],
    "confidence": {"insufficient": "n < 10 or live_n == 0", "low": "n >= 10 and n < 30 or live_n < 5", "moderate": "n >= 30 and live_n >= 5", "stronger": "not a quality claim; reserved for future review"},
    "predeclared_before_current_data": True,
    "ranking": False,
    "optimization": False,
    "candidate_generation": "deterministic repeated component failure pattern only; no result-driven thresholds",
}


class StrategyHealthError(ValueError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _health_confidence(n: int, live_n: int) -> str:
    if n < 10 or live_n == 0:
        return "insufficient"
    if n < 30 or live_n < 5:
        return "low"
    return "moderate"


def _recent_count(rows: list[Mapping[str, Any]]) -> int:
    dates = [str(row.get("event_date")) for row in rows if row.get("event_date")]
    if not dates:
        return 0
    latest = datetime.fromisoformat(max(dates)).date()
    return sum(
        bool(row.get("event_date"))
        and 0 <= (latest - datetime.fromisoformat(str(row["event_date"])).date()).days <= POLICY["recent_window_days"]
        for row in rows
    )


def _observability_status(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "not_observable"
    required = [item for item in value if isinstance(item, Mapping) and item.get("required")]
    if not required:
        return "not_observable"
    if any(item.get("status") == "NOT_MACHINE_OBSERVABLE" for item in required):
        return "not_observable"
    if any(item.get("status") == "UNKNOWN" for item in required):
        return "mixed"
    return "machine_observable"


def _operational_rows(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if row.get("source_kind") == "post_cutover_operational"]


def _method_eligible(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("operational_method_eligible") is True
        and row.get("review_complete") is True
        and row.get("rule_compliance") == "compliant"
        and row.get("diagnostic_class") == "METHOD"
        and (row.get("method_observability") or {}).get("status") == "machine_observable"
    )


def _operational_status(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    operational = _operational_rows(rows)
    observed = [row for row in operational if (row.get("method_observability") or {}).get("status") == "machine_observable"]
    reviewed = [row for row in operational if row.get("review_complete") is True]
    eligible = [row for row in operational if _method_eligible(row)]
    if not operational:
        status = "not_observable"
    elif len(observed) == len(operational):
        status = "machine_observable"
    elif observed:
        status = "mixed"
    else:
        status = "not_observable"
    return {"status": status, "operational_observed_n": len(observed), "operational_reviewed_n": len(reviewed), "operational_method_eligible_n": len(eligible), "operational_n": len(operational)}


def _component_key(field: str, tag: Any) -> str:
    return f"{field}:{tag}"


def _failure_patterns(strategy_id: str, rule_version: str | None, rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    eligible = [row for row in _operational_rows(rows) if _method_eligible(row) and rule_version and row.get("rule_version") == rule_version]
    total_by_component: dict[str, int] = defaultdict(int)
    failures_by_component: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in eligible:
        components: set[str] = set()
        for field in POLICY["failure_component_fields"]:
            values = row.get(field)
            if isinstance(values, list):
                components.update(_component_key(field, value) for value in values if isinstance(value, str) and value)
        for component in sorted(components):
            total_by_component[component] += 1
            if _number(row.get("realized_r")) is not None and float(row["realized_r"]) < 0:
                failures_by_component[component].append(row)
    patterns: list[dict[str, Any]] = []
    for component in sorted(total_by_component):
        component_n = total_by_component[component]
        failures = failures_by_component.get(component, [])
        failure_n = len(failures)
        rate = failure_n / component_n if component_n else 0.0
        if component_n < POLICY["minimum_failure_pattern_count"] or rate < POLICY["failure_pattern_rate"]:
            continue
        component_rows = [
            row for row in eligible
            if any(component == _component_key(field, value) for field in POLICY["failure_component_fields"] for value in (row.get(field) if isinstance(row.get(field), list) else []))
        ]
        evidence_refs = sorted({str(row.get("evidence_ref")) for row in component_rows if row.get("evidence_ref")})
        dates = sorted(str(row.get("event_date")) for row in failures if row.get("event_date"))
        candidate_id = "cand-" + _digest({"policy_id": POLICY["policy_id"], "strategy_id": strategy_id, "rule_version": rule_version, "component": component})[:16]
        patterns.append({
            "candidate_id": candidate_id,
            "strategy_id": strategy_id,
            "current_rule_version": rule_version,
            "component": component,
            "component_n": component_n,
            "failure_n": failure_n,
            "failure_rate": _round(rate),
            "sample_n": len(_operational_rows(rows)),
            "method_eligible_n": len(eligible),
            "evidence_refs": evidence_refs,
            "evidence_window": {"start_event_date": dates[0] if dates else None, "end_event_date": dates[-1] if dates else None},
        })
    return patterns


def _candidate_gate(
    pattern: Mapping[str, Any],
    *,
    sample_n: int,
    confidence: str,
    operational: Mapping[str, Any],
    rule_version: str | None,
) -> dict[str, Any]:
    """Return the single closed gate for emitting a method candidate."""

    method_n = int(operational.get("operational_method_eligible_n") or 0)
    reviewed_n = int(operational.get("operational_reviewed_n") or 0)
    minimum_total_sample = POLICY["minimum_count"]
    checks = {
        "minimum_total_sample": sample_n >= minimum_total_sample,
        "confidence_not_insufficient": confidence != "insufficient",
        "minimum_operational_method_eligible_sample": method_n >= POLICY["minimum_operational_method_eligible_sample"],
        "minimum_operational_reviewed_sample": reviewed_n >= POLICY["minimum_operational_reviewed_sample"],
        "operational_observability": operational.get("status") == "machine_observable",
        "exact_current_rule_version": bool(rule_version) and pattern.get("current_rule_version") == rule_version,
        "repeated_failure_pattern": (
            int(pattern.get("component_n") or 0) >= POLICY["minimum_failure_pattern_count"]
            and float(pattern.get("failure_rate") or 0.0) >= POLICY["failure_pattern_rate"]
        ),
        "evidence_refs": bool(pattern.get("evidence_refs")),
        "no_optimization": POLICY["candidate_eligibility"]["no_optimization"] is True,
        "no_hindsight_mutation": POLICY["candidate_eligibility"]["no_hindsight_mutation"] is True,
    }
    return {
        "policy_id": POLICY["policy_id"],
        "passed": all(checks.values()),
        "checks": checks,
        "minimum_total_sample": minimum_total_sample,
        "sample_n": sample_n,
        "confidence": confidence,
        "minimum_method_eligible_n": POLICY["minimum_operational_method_eligible_sample"],
        "method_eligible_n": method_n,
        "minimum_reviewed_n": POLICY["minimum_operational_reviewed_sample"],
        "reviewed_n": reviewed_n,
        "required_rule_observability": True,
        "evidence_required": True,
        "observability": operational.get("status"),
        "execution_excluded": True,
        "process_risk_excluded": True,
        "minimum_failure_pattern_count": POLICY["minimum_failure_pattern_count"],
        "failure_pattern_rate": pattern.get("failure_rate"),
    }


def _candidate(
    pattern: Mapping[str, Any],
    *,
    sample_n: int,
    gate: Mapping[str, Any],
) -> dict[str, Any]:
    component = str(pattern["component"])
    return {
        "candidate_id": pattern["candidate_id"],
        "strategy_id": pattern["strategy_id"],
        "current_rule_version": pattern["current_rule_version"],
        "evidence_window": dict(pattern["evidence_window"]),
        "sample_n": sample_n,
        "operational_sample_n": pattern["sample_n"],
        "method_eligible_n": pattern["method_eligible_n"],
        "observed_failure_mode": POLICY["failure_condition"],
        "affected_rule_component": component,
        "research_question": f"Does the repeated failure pattern for {component} persist under a separately versioned future rule test?",
        "research_hypothesis": f"The observed post-cutover failure pattern associated with {component} may warrant a future controlled test; current evidence does not authorize a rule change.",
        "proposed_rule_delta": f"Future test candidate only: evaluate an explicit treatment of {component} in a separately frozen candidate rule; do not activate or mutate the current rule.",
        "success_metrics": ["predeclared out-of-sample method outcome metric", "predeclared rule-observability completeness"],
        "failure_metrics": ["predeclared holdout failure rate", "predeclared evidence or execution contamination rate"],
        "minimum_test_sample": POLICY["minimum_operational_method_eligible_sample"],
        "holdout_requirement": "required; holdout must remain untouched until the candidate test is frozen",
        "frozen_controls": ["current B3 rule version unchanged", "no parameter search", "no threshold tuning", "no loser exclusion", "no symbol filtering", "no hindsight relabeling"],
        "evidence_refs": list(pattern["evidence_refs"]),
        "limitations": ["This is a descriptive future-test candidate, not a strategy conclusion or approved rule change.", "The detector uses only reviewed, compliant, method-observable post-cutover observations."],
        "predeclared_gate": dict(gate),
    }


def refresh_health(root: Path, *, generated_at: str | None = None, source_commit: str | None = None, output_dir: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    analytics = load_current(root)
    generated_at = generated_at or analytics.get("generated_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    source_commit = source_commit or analytics.get("source_commit", "unknown")
    output = output_dir or DEFAULT_OUTPUT
    output = (root / output if not output.is_absolute() else output).resolve()
    policy_digest = _digest(POLICY)
    output.mkdir(parents=True, exist_ok=True)
    (output / "policy.json").write_text(json.dumps({"schema_version": POLICY["policy_id"], **POLICY, "policy_digest": policy_digest}, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    observations = [row for row in analytics.get("observations", []) if row.get("counted") is True]
    by_strategy: dict[str, list[Mapping[str, Any]]] = {}
    for row in observations:
        by_strategy.setdefault(str(row.get("strategy_id") or "unknown"), []).append(row)
    outputs: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    historical_by_strategy = analytics.get("historical_rule_observability_by_strategy", analytics.get("rule_observability_by_strategy", {}))
    operational_by_strategy = analytics.get("operational_observability_by_strategy", {})
    active_strategies = analytics.get("source_version_pins", {}).get("b3", {}).get("active_strategy_ids", [])
    for strategy_id in sorted(set(active_strategies) | set(by_strategy)):
        rows = by_strategy.get(strategy_id, [])
        metrics = analytics.get("metrics", {}).get("by_strategy", {}).get(strategy_id, {})
        n = int(metrics.get("count") or 0)
        live_n = int(metrics.get("live_count") or 0)
        recent_n = _recent_count(rows)
        historical_status = _observability_status(historical_by_strategy.get(strategy_id))
        op_summary = dict(operational_by_strategy.get(strategy_id) or _operational_status(rows))
        op_status = op_summary.get("status", "not_observable")
        op_rows = _operational_rows(rows)
        op_reviewed_n = int(op_summary.get("operational_reviewed_n") or 0)
        op_method_n = int(op_summary.get("operational_method_eligible_n") or 0)
        rule_version = analytics.get("source_version_pins", {}).get("b3", {}).get("active_rule_hashes", {}).get(strategy_id, {}).get("rule_version")
        execution_rows = [row for row in op_rows if row.get("diagnostic_class") == "EXECUTION" and isinstance(row.get("rule_violation"), bool)]
        execution_rate = sum(row.get("rule_violation") is True for row in execution_rows) / len(execution_rows) if execution_rows else None
        confidence = _health_confidence(n, live_n)
        patterns = _failure_patterns(strategy_id, rule_version, rows)
        candidate_gates = [_candidate_gate(pattern, sample_n=n, confidence=confidence, operational=op_summary, rule_version=rule_version) for pattern in patterns]
        candidates = [_candidate(pattern, sample_n=n, gate=gate) for pattern, gate in zip(patterns, candidate_gates) if gate["passed"]]
        all_candidates.extend(candidates)
        if confidence == "insufficient":
            state = "insufficient_evidence"
            evidence_class = "insufficient_evidence"
        elif candidates:
            state = "investigate"
            evidence_class = "descriptive_pattern"
        elif execution_rate is not None and execution_rate > 0.2:
            state = "investigate"
            evidence_class = "descriptive_pattern"
        elif op_rows and op_method_n < POLICY["minimum_operational_method_eligible_sample"] and op_summary.get("operational_reviewed_n", 0) > 0:
            state = "insufficient_evidence"
            evidence_class = "insufficient_evidence"
        elif op_rows and op_status == "not_observable":
            state = "not_observable"
            evidence_class = "descriptive_pattern"
        elif not op_rows and historical_status == "not_observable":
            state = "not_observable"
            evidence_class = "descriptive_pattern"
        elif metrics.get("review_completion_rate") is None or metrics.get("review_completion_rate") < POLICY["review_completeness_required"] or recent_n < POLICY["recent_window_minimum_count"]:
            state = "watch"
            evidence_class = "descriptive_pattern"
        else:
            state = "stable"
            evidence_class = "descriptive_pattern"
        if confidence == "insufficient":
            diagnostic_class = "INSUFFICIENT"
        elif execution_rate is not None and execution_rate > 0.2:
            diagnostic_class = "EXECUTION"
        elif candidates:
            diagnostic_class = "METHOD"
        elif op_status == "mixed":
            diagnostic_class = "MIXED"
        elif state in {"not_observable", "insufficient_evidence"}:
            diagnostic_class = "INSUFFICIENT"
        else:
            diagnostic_class = "METHOD"
        candidate = next(iter(candidates), None) if len(candidates) == 1 else None
        candidate_ids = [item["candidate_id"] for item in candidates]
        if not candidates:
            candidate_selection_state = "no_candidate"
        elif len(candidates) == 1:
            candidate_selection_state = "single_candidate"
        else:
            candidate_selection_state = POLICY["candidate_multiplicity"]["many"]
        outputs.append({
            "strategy_id": strategy_id,
            "rule_version": rule_version,
            "sample_n": n,
            "recent_n": recent_n,
            "performance_summary": {key: metrics.get(key) for key in ("win_rate", "expectancy_r", "profit_factor", "mean_r", "median_r", "max_observed_drawdown_r", "live_count", "backtest_count")},
            # Descriptive source-origin split only -- does not feed
            # _candidate_gate, POLICY, or health-state derivation. Sourced
            # straight from the existing by_strategy metrics (which already
            # carry live_count/backtest_count); this is a passthrough, not a
            # second metrics engine.
            "live_n": int(metrics.get("live_count") or 0),
            "backtest_n": int(metrics.get("backtest_count") or 0),
            "combined_n": int(metrics.get("live_count") or 0) + int(metrics.get("backtest_count") or 0),
            "historical_observability": {"status": historical_status, "required_rule_evidence": historical_by_strategy.get(strategy_id, [])},
            "operational_observability": op_summary,
            "combined_observability_state": "operational_machine_observable" if op_status == "machine_observable" else "operational_mixed" if op_status == "mixed" else "historical_machine_observable" if historical_status == "machine_observable" else "historical_mixed" if historical_status == "mixed" else "not_observable",
            "execution_quality": {"rule_violation_rate": execution_rate if execution_rate is not None else metrics.get("rule_violation_rate"), "status": "not_observable" if execution_rate is None and metrics.get("rule_violation_rate") is None else "observed"},
            "rule_observability": {"status": op_status if op_rows else historical_status, "historical_status": historical_status, "operational_status": op_status, "required_rule_evidence": historical_by_strategy.get(strategy_id, [])},
            "review_completeness": {"rate": metrics.get("review_completion_rate"), "status": "not_observable" if metrics.get("review_completion_rate") is None else "observed"},
            "operational_observed_n": op_summary.get("operational_observed_n", 0),
            "operational_reviewed_n": op_reviewed_n,
            "operational_method_eligible_n": op_method_n,
            "diagnostic_separation": {"method": "observed" if op_method_n else ("not_observable" if historical_status != "machine_observable" else "observed"), "execution": "observed" if execution_rate is not None or metrics.get("rule_violation_rate") is not None else "not_observable", "process_risk": "not_observable", "overall": state, "boundary": "execution/process-risk observations are not method claims"},
            "diagnostic_classes": [diagnostic_class],
            "health_state": state,
            "evidence_class": evidence_class,
            "confidence": confidence,
            "evidence_refs": ["exports/operational/analytics/current.json", "exports/method-validation/b2/strategy-performance.json", "exports/method-validation/b3/manifest.json"],
            "limitations": ["Health states are descriptive diagnostics, not future performance predictions or strategy-change instructions.", "Historical rule observability is never retroactively upgraded by operational evidence.", "Execution and process-risk observations cannot produce a method improvement candidate.", "A candidate is a future research contract only; it does not authorize a rule change or backtest."],
            "candidate_improvement_id": candidate.get("candidate_id") if candidate else None,
            "improvement_candidate": candidate,
            "candidate_improvement_ids": candidate_ids,
            "improvement_candidates": candidates,
            "candidate_selection_state": candidate_selection_state,
            "method_failure_patterns": patterns,
        })
    summary_by_mode = analytics.get("summary_by_mode", {})
    sample_origin_summary = {
        "live_n": int((summary_by_mode.get("live") or {}).get("count") or 0),
        "backtest_n": int((summary_by_mode.get("backtest") or {}).get("count") or 0),
        "combined_n": int((summary_by_mode.get("all") or {}).get("count") or 0),
        "combined_descriptive": True,
    }
    current = {"schema_version": HEALTH_SCHEMA, "generated_at": generated_at, "source_commit": source_commit, "policy": {"policy_id": POLICY["policy_id"], "policy_digest": policy_digest}, "analytics_artifact_digest": analytics.get("artifact_digest"), "guardrails": {"predeclared_policy": True, "candidate_generation_enabled": True, "candidate_generation_is_not_optimization": True, "no_ranking": True, "no_optimization": True, "no_strategy_mutation": True}, "strategies": outputs, "eligible_improvement_candidates": all_candidates, "status_summary": {state: sum(item["health_state"] == state for item in outputs) for state in POLICY["states"]}, "sample_origin_summary": sample_origin_summary}
    current["artifact_digest"] = _digest(current)
    current_path = output / "current.json"
    current_path.write_text(json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    manifest = {"schema_version": HEALTH_MANIFEST_SCHEMA, "generated_at": generated_at, "source_commit": source_commit, "artifact": {"path": "exports/operational/health/current.json", "sha256": hashlib.sha256(current_path.read_bytes()).hexdigest(), "artifact_digest": current["artifact_digest"]}, "policy": {"path": "exports/operational/health/policy.json", "sha256": hashlib.sha256((output / "policy.json").read_bytes()).hexdigest(), "policy_digest": policy_digest}, "analytics_artifact_digest": analytics.get("artifact_digest"), "optimization_performed": False}
    manifest["manifest_digest"] = _digest(manifest)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def load_current_health(root: Path, path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT_OUTPUT / "current.json"
    target = root / target if not target.is_absolute() else target
    payload = json.loads(target.read_text(encoding="utf-8"))
    if payload.get("schema_version") != HEALTH_SCHEMA:
        raise StrategyHealthError("invalid strategy health schema")
    return payload
