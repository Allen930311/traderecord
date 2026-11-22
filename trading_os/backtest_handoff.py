"""Evidence-gated improvement and generic backtest handoff contract."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .operational_analytics import load_current
from .strategy_health import POLICY, load_current_health


HANDOFF_SCHEMA = "backtest-handoff.v1"
DEFAULT_OUTPUT = Path("exports/operational/handoff")


class BacktestHandoffError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _eligible_candidate(item: Mapping[str, Any], analytics: Mapping[str, Any]) -> dict[str, Any] | None:
    candidate = item.get("improvement_candidate")
    if not isinstance(candidate, Mapping):
        return None
    required = {
        "candidate_id", "strategy_id", "current_rule_version", "evidence_refs",
        "observed_failure_mode", "research_hypothesis", "proposed_rule_delta",
        "success_metrics", "failure_metrics", "minimum_test_sample",
        "holdout_requirement", "frozen_controls", "predeclared_gate",
        "evidence_window", "sample_n", "method_eligible_n",
        "affected_rule_component", "research_question", "limitations",
    }
    if any(field not in candidate for field in required):
        return None
    gate = candidate.get("predeclared_gate")
    if not isinstance(gate, Mapping) or gate.get("passed") is not True or gate.get("evidence_required") is not True or gate.get("observability") != "machine_observable":
        return None
    if item.get("health_state") not in {"watch", "investigate"}:
        return None
    sample_count = item.get("sample_n")
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < POLICY["minimum_count"] or item.get("confidence") == "insufficient":
        return None
    if item.get("diagnostic_classes") != ["METHOD"]:
        return None
    operational = item.get("operational_observability")
    if not isinstance(operational, Mapping) or operational.get("status") != "machine_observable":
        return None
    reviewed_count = item.get("operational_reviewed_n")
    if isinstance(reviewed_count, bool) or not isinstance(reviewed_count, int) or reviewed_count < POLICY["minimum_operational_reviewed_sample"]:
        return None
    operational_method_count = item.get("operational_method_eligible_n")
    if isinstance(operational_method_count, bool) or not isinstance(operational_method_count, int) or operational_method_count < POLICY["minimum_operational_method_eligible_sample"]:
        return None
    if item.get("candidate_improvement_id") != candidate.get("candidate_id") or item.get("strategy_id") != candidate.get("strategy_id"):
        return None
    if not isinstance(candidate.get("evidence_refs"), list) or not candidate["evidence_refs"] or any(not isinstance(ref, str) or not ref for ref in candidate["evidence_refs"]):
        return None
    if not isinstance(candidate.get("evidence_window"), Mapping) or not isinstance(candidate.get("limitations"), list) or not candidate["limitations"]:
        return None
    if not isinstance(candidate.get("sample_n"), int) or isinstance(candidate.get("sample_n"), bool) or candidate["sample_n"] <= 0:
        return None
    method_eligible_n = candidate.get("method_eligible_n")
    if isinstance(method_eligible_n, bool) or not isinstance(method_eligible_n, int) or method_eligible_n <= 0:
        return None
    gate_method_n = gate.get("method_eligible_n")
    if isinstance(gate_method_n, bool) or not isinstance(gate_method_n, int) or gate_method_n != method_eligible_n:
        return None
    minimum = candidate.get("minimum_test_sample")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0 or method_eligible_n < minimum or int(item.get("operational_method_eligible_n") or 0) < minimum:
        return None
    if not isinstance(candidate.get("success_metrics"), list) or not candidate["success_metrics"]:
        return None
    if not isinstance(candidate.get("failure_metrics"), list) or not candidate["failure_metrics"]:
        return None
    if not isinstance(candidate.get("frozen_controls"), list) or not candidate["frozen_controls"]:
        return None
    active_rule = analytics.get("source_version_pins", {}).get("b3", {}).get("active_rule_hashes", {}).get(candidate.get("strategy_id"), {})
    if not isinstance(active_rule, Mapping) or candidate.get("current_rule_version") != active_rule.get("rule_version"):
        return None
    gate_sample_n = gate.get("sample_n")
    if isinstance(gate_sample_n, bool) or not isinstance(gate_sample_n, int) or gate_sample_n != candidate.get("sample_n") or gate_sample_n < POLICY["minimum_count"]:
        return None
    if gate.get("confidence") == "insufficient" or gate.get("checks", {}).get("minimum_total_sample") is not True or gate.get("checks", {}).get("confidence_not_insufficient") is not True:
        return None
    return dict(candidate)


def _eligible_candidates(item: Mapping[str, Any], analytics: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_candidates = item.get("improvement_candidates")
    if raw_candidates is None:
        raw_candidates = [item.get("improvement_candidate")] if item.get("improvement_candidate") is not None else []
    if not isinstance(raw_candidates, list):
        return []
    valid: list[dict[str, Any]] = []
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            continue
        candidate_item = dict(item)
        candidate_item["improvement_candidate"] = raw
        candidate_item["candidate_improvement_id"] = raw.get("candidate_id")
        candidate = _eligible_candidate(candidate_item, analytics)
        if candidate is not None:
            valid.append(candidate)
    return valid


def emit_handoff(root: Path, *, generated_at: str | None = None, source_commit: str | None = None, output_dir: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    analytics = load_current(root)
    health = load_current_health(root)
    generated_at = generated_at or analytics.get("generated_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    source_commit = source_commit or analytics.get("source_commit", "unknown")
    output = output_dir or DEFAULT_OUTPUT
    output = (root / output if not output.is_absolute() else output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    strategy_candidates = [candidate for item in health.get("strategies", []) if isinstance(item, Mapping) for candidate in _eligible_candidates(item, analytics)]
    global_candidates = health.get("eligible_improvement_candidates")
    global_ids = [candidate.get("candidate_id") for candidate in global_candidates] if isinstance(global_candidates, list) and all(isinstance(candidate, Mapping) for candidate in global_candidates) else None
    strategy_ids = [candidate.get("candidate_id") for candidate in strategy_candidates]
    valid_candidates = strategy_candidates if global_ids == strategy_ids else []
    candidate = next(iter(valid_candidates), None) if len(valid_candidates) == 1 else None
    if not valid_candidates:
        status = "no_eligible_candidate"
    elif len(valid_candidates) == 1:
        status = "ready_for_backtest_review"
    else:
        status = "multiple_candidates_require_review"
    if candidate is not None:
        candidate["analytics_artifact_digest"] = analytics.get("artifact_digest")
        candidate["health_artifact_digest"] = health.get("artifact_digest")
    payload = {
        "schema_version": HANDOFF_SCHEMA,
        "generated_at": generated_at,
        "source_commit": source_commit,
        "status": status,
        "candidate": candidate,
        "candidate_count": len(valid_candidates),
        "candidates": [dict(item) for item in valid_candidates] if len(valid_candidates) > 1 else [],
        "candidate_id": candidate.get("candidate_id") if candidate else None,
        "strategy_id": candidate.get("strategy_id") if candidate else None,
        "current_rule_version": candidate.get("current_rule_version") if candidate else None,
        "evidence": candidate.get("evidence_refs", []) if candidate else [],
        "observed_failure_mode": candidate.get("observed_failure_mode") if candidate else None,
        "research_hypothesis": candidate.get("research_hypothesis") if candidate else None,
        "proposed_rule_delta": candidate.get("proposed_rule_delta") if candidate else None,
        "frozen_controls": candidate.get("frozen_controls") if candidate else ["B3 rule version remains unchanged", "no parameter search", "no threshold tuning", "no loser exclusion", "no symbol filtering", "no hindsight relabeling"],
        "success_metrics": candidate.get("success_metrics", []) if candidate else [],
        "failure_metrics": candidate.get("failure_metrics", []) if candidate else [],
        "minimum_test_sample": candidate.get("minimum_test_sample") if candidate else None,
        "holdout_requirement": candidate.get("holdout_requirement") if candidate else "required before any future candidate can be considered",
        "source_artifact_digests": {"analytics": analytics.get("artifact_digest"), "health": health.get("artifact_digest")},
        "candidate_policy": {"requires_health_gate": True, "requires_predeclared_question": True, "requires_current_rule_version": True, "requires_holdout": True, "requires_minimum_sample": True, "multiple_candidates_require_review": True, "automatic_selection": False},
        "source_version_pins": {"analytics_artifact_digest": analytics.get("artifact_digest"), "health_artifact_digest": health.get("artifact_digest"), "b3": analytics.get("source_version_pins", {}).get("b3", {})},
        "prohibited_hindsight_operations": ["parameter_search", "threshold_tuning", "loser_exclusion", "symbol_filtering", "rule_mutation", "v2_activation", "v1_retirement", "backtest_execution"],
        "synthetic_fixture_contract_tested": True,
        "guardrails": {"optimization_performed": False, "backtest_executed": False, "strategy_mutation": False, "deployment": False},
    }
    payload["artifact_digest"] = _digest(payload)
    path = output / "current.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    manifest = {"schema_version": "backtest-handoff-manifest.v1", "artifact": {"path": "exports/operational/handoff/current.json", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "artifact_digest": payload["artifact_digest"]}, "status": status, "source_commit": source_commit, "optimization_performed": False}
    manifest["manifest_digest"] = _digest(manifest)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest
