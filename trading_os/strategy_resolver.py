"""Resolve registered setup rules or a first-class Owner retrospective attestation.

The caller can no longer just *assert* ``strategy_id``. It either supplies
per-rule observations (each with evidence refs), which this module checks
against every active frozen rule set, or a registry-mapped retrospective
Owner attestation with a separate lane for post-trade replay observations.
Resolver outcomes include:

MATCHED | NO_MATCH | AMBIGUOUS | INSUFFICIENT_EVIDENCE

Only MATCHED and NO_MATCH are "resolved". NO_MATCH is a legitimate, final answer
(a real off-strategy trade); AMBIGUOUS and INSUFFICIENT_EVIDENCE mean "not decided yet".
An unresolved result is still a valid LIVE registration attribution sub-state.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .registry import load_effective_rules, load_registry, load_version, usable_rule_versions
from .historical_attribution import (
    REGISTRY_VERSION as RETROSPECTIVE_ATTRIBUTION_REGISTRY_VERSION,
    expression_contract,
    historical_attribution_registry_digest,
    normalize_context_id,
)
from .vocabularies import PLAN_FIELD_UNKNOWN_VALUES

RESOLVER_VERSION = "strategy-resolver.v1"
RESOLVED_STATUSES = {"MATCHED", "NO_MATCH"}
_DEFAULT_TIMINGS = {"pre_trade", "execution"}
OWNER_RETROSPECTIVE_AUTHORITY = "owner_retrospective_strategy_attribution"
_OWNER_PLAN_FACTS = {"entry_price", "stop_loss_price", "target_price", "planned_rr"}
_FACT_TIMINGS = {"pre_trade", "execution", "post_trade"}
_OBSERVATION_BASES = {
    "contemporaneous_capture",
    "provider_execution",
    "governed_adjudication",
}


class ResolverError(ValueError):
    """Raised on malformed resolver input (unknown rule IDs, missing fields)."""


def active_strategies(root: Path, only_versions: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Active strategies and every rule version new trades may use.

    A new version does not retire the previous one: every listed version that is active and not
    in the strategy's `retired_rule_versions` stays usable until the Owner stops it. The
    top-level `rule_version`/`rules` are the newest usable version (the default); all usable
    versions, oldest first, are in `usable_versions`.

    `only_versions` is for replaying a frozen decision: exactly those versions are evaluated,
    whether or not they are still usable today and whatever the strategy's status now is.
    """

    exact = set(only_versions) if only_versions is not None else None
    out = []
    for strategy in load_registry(root).get("strategies", []):
        if exact is not None:
            versions = [v for v in strategy.get("rule_versions", []) if isinstance(v, str) and v in exact]
        elif strategy.get("status") != "active":
            continue
        else:
            versions = usable_rule_versions(root, strategy)
        if not versions:
            continue
        usable = [{"rule_version": v, "rules": load_effective_rules(root, v)} for v in versions]
        out.append(
            {
                "strategy_id": strategy["strategy_id"],
                "parent_strategy_id": strategy.get("parent_strategy_id"),
                "rule_version": usable[-1]["rule_version"],
                "rules": usable[-1]["rules"],
                "usable_versions": usable,
            }
        )
    return out


def _get(obj: Mapping[str, Any] | None, dotted: str) -> Any:
    value: Any = obj
    for part in dotted.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _fact_timing(item: Mapping[str, Any]) -> str | None:
    # ``timing`` is accepted for old resolver inputs only. New inputs name the
    # fact time explicitly so it cannot be confused with attached artifact timing.
    value = item.get("supports_fact_timing", item.get("timing"))
    return value if isinstance(value, str) else None


def _rule_state(
    rule: Mapping[str, Any], obs: list[Mapping[str, Any]], plan: Mapping[str, Any]
) -> tuple[str, str, list[str], str | None, list[str]]:
    operands = rule.get("operands") if isinstance(rule.get("operands"), Mapping) else {}
    field = str(operands.get("field", ""))
    if rule.get("operator") == "exists" and field.startswith("plan."):
        # Preserve an exact per-rule observation when one was supplied. The
        # canonical plan value alone can still resolve strategy attribution,
        # but it is not sufficient machine evidence for Strategy Health when
        # this frozen rule requires a cited artifact.
        allowed = set(rule.get("allowed_evidence_timing") or _DEFAULT_TIMINGS)
        usable = [item for item in obs if _fact_timing(item) in allowed]
        value = _get({"plan": plan}, field)
        present = value is not None and not (
            isinstance(value, str) and value.strip().lower() in PLAN_FIELD_UNKNOWN_VALUES
        )
        if present and usable:
            states = {item["state"] for item in usable}
            refs = sorted({ref for item in usable for ref in item["evidence_refs"]})
            if states == {"PASS"}:
                selected = usable[0]
                return "PASS", "observed_plan_fact", refs, _fact_timing(selected), list(selected.get("basis", []))
            selected = usable[0]
            return "UNKNOWN", "conflicting_plan_observation", refs, _fact_timing(selected), list(selected.get("basis", []))
        if present:
            return "PASS", "plan_fact_present", [f"plan:{field}"], None, []
        return "UNKNOWN", "plan_fact_missing", [], None, []
    if not obs:
        return "UNKNOWN", "no_observation", [], None, []
    states = {o["state"] for o in obs}
    if len(states) > 1:
        selected = obs[0]
        return "UNKNOWN", "conflicting_observations", sorted({r for o in obs for r in o["evidence_refs"]}), _fact_timing(selected), list(selected.get("basis", []))
    allowed = set(rule.get("allowed_evidence_timing") or _DEFAULT_TIMINGS)
    usable = [o for o in obs if _fact_timing(o) in allowed]
    if not usable:
        selected = obs[0]
        return "UNKNOWN", "timing_missing_or_disallowed", [], _fact_timing(selected), list(selected.get("basis", []))
    selected = usable[0]
    return (
        selected["state"],
        "observed",
        sorted({r for o in usable for r in o["evidence_refs"]}),
        _fact_timing(selected),
        list(selected.get("basis", [])),
    )


def _check_adjudication(root: Path, item: Mapping[str, Any]) -> None:
    """An observation may lean on a governed Owner adjudication (e.g. attesting pre-trade observability)."""

    adjudication_id = item.get("adjudication_id")
    path = Path(root) / "canonical" / "adjudications" / f"{adjudication_id}.json"
    if not isinstance(adjudication_id, str) or not adjudication_id.startswith("adj_") or "/" in adjudication_id or not path.exists():
        raise ResolverError(f"observation cites an unknown adjudication: {adjudication_id}")
    decision = json.loads(path.read_text(encoding="utf-8"))
    cited = set(item["evidence_refs"])
    if decision.get("decision_type") != "evidence" or decision.get("decision") != "accept" or not decision.get("reviewer"):
        raise ResolverError(f"adjudication is not an accepted evidence decision: {adjudication_id}")
    if not (cited - {f"adjudication:{adjudication_id}"}) <= set(decision.get("evidence_refs", [])):
        raise ResolverError(f"observation evidence is not covered by adjudication {adjudication_id}")


def _validate_owner_setup_attribution(raw: Any) -> dict[str, Any] | None:
    """Validate an Owner fact about whether the actual entry had a setup.

    This is intentionally different from owner_no_match. owner_no_match is a
    strategy-coverage claim ("I reviewed every active strategy and none applies")
    and therefore keeps its exhaustive coverage gate. An explicit Owner statement
    that the actual entry itself had no setup is an authority fact about the
    Owner's own historical entry intent; it must not be overridden by chart
    patterns that happen to match a frozen strategy.
    """

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ResolverError("owner_setup_attribution must be an object")
    if raw.get("setup_status") != "NO_SETUP":
        raise ResolverError("owner_setup_attribution.setup_status must equal NO_SETUP")
    reason = raw.get("reason")
    refs = raw.get("evidence_refs")
    if not isinstance(reason, str) or not reason.strip():
        raise ResolverError("owner_setup_attribution requires a non-empty reason")
    if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs):
        raise ResolverError("owner_setup_attribution requires non-empty evidence_refs")
    return {"setup_status": "NO_SETUP", "reason": reason.strip(), "evidence_refs": list(refs)}


def _iso_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate_owner_attestation(
    root: Path,
    raw: Any,
    *,
    strategies: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Validate the Owner's first-class retrospective decision attestation.

    Owner attestation records historical intent; it never changes evidence
    provenance or provider execution facts. Positive strategy identity is
    required and mapped through the active frozen registry. Entry expression
    is optional and is validated only when the selected frozen strategy
    defines that dimension. A retrospective replay is allowed as corroborating
    evidence, but the caller must keep its true timing in
    ``retrospective_observations``.
    """

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ResolverError("owner_attestation must be an object")
    if raw.get("timing") != "retrospective":
        raise ResolverError("owner_attestation.timing must equal retrospective")
    setup_status = raw.get("setup_status")
    if setup_status not in {"HAS_SETUP", "NO_SETUP"}:
        raise ResolverError("owner_attestation.setup_status must equal HAS_SETUP or NO_SETUP")
    rationale = raw.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ResolverError("owner_attestation requires a non-empty rationale")
    refs = raw.get("evidence_refs", [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ResolverError("owner_attestation.evidence_refs must be a list of non-empty strings")
    if setup_status == "NO_SETUP":
        return {
            "timing": "retrospective",
            "setup_status": "NO_SETUP",
            "rationale": rationale.strip(),
            "evidence_refs": list(refs),
        }

    active = strategies if strategies is not None else active_strategies(Path(root))
    by_id = {str(item.get("strategy_id")): item for item in active}
    strategy_id = raw.get("primary_strategy_id")
    selected = by_id.get(strategy_id) if isinstance(strategy_id, str) else None
    if selected is None:
        raise ResolverError("owner_attestation.primary_strategy_id must identify an active strategy")
    # With several usable versions the Owner may name one; otherwise the newest is used.
    versions = selected.get("usable_versions") or [
        {"rule_version": selected["rule_version"], "rules": selected["rules"]}
    ]
    requested_version = raw.get("rule_version")
    if requested_version is None:
        chosen_version = versions[-1]
    else:
        chosen_version = next((v for v in versions if v["rule_version"] == requested_version), None)
        if chosen_version is None:
            raise ResolverError(
                "owner_attestation.rule_version must be a usable version of the primary strategy: "
                + ", ".join(v["rule_version"] for v in versions)
            )
    selected = {**selected, "rule_version": chosen_version["rule_version"], "rules": chosen_version["rules"]}

    expression = raw.get("entry_expression")
    expression_rules = [
        rule for rule in selected.get("rules", [])
        if isinstance(rule, Mapping) and str(rule.get("rule_id", "")).endswith(".entry_expression")
    ]
    if expression is not None and not expression_rules:
        raise ResolverError(
            "owner_attestation.entry_expression is unsupported because the active frozen strategy does not define that dimension"
        )
    allowed_expressions = {
        str(value)
        for rule in expression_rules
        for value in (rule.get("operands", {}).get("values", []) if isinstance(rule.get("operands"), Mapping) else [])
    }
    if expression is not None and (not isinstance(expression, str) or expression not in allowed_expressions):
        raise ResolverError(
            "owner_attestation.entry_expression must be allowed by the active frozen strategy rules"
        )

    triggers = raw.get("entry_triggers", [])
    if (
        not isinstance(triggers, list)
        or any(not isinstance(value, str) or not value.strip() for value in triggers)
        or len({value.upper() for value in triggers}) != len(triggers)
    ):
        raise ResolverError("owner_attestation.entry_triggers must be a list of unique non-empty strings when supplied")
    raw_confluences = raw.get("confluences", [])
    if not isinstance(raw_confluences, list):
        raise ResolverError("owner_attestation.confluences must be a list")
    normalized_confluences: list[str] = []
    confluence_types: dict[str, str] = {}
    for value in raw_confluences:
        if isinstance(value, str):
            strategy_confluence_id = value if value in by_id else None
            context_confluence_id = normalize_context_id(Path(root), value)
            is_strategy = strategy_confluence_id is not None
            is_context = context_confluence_id is not None
            if is_strategy == is_context:
                raise ResolverError(
                    f"owner_attestation confluence must identify exactly one active strategy or governed context: {value}"
                )
            kind = "strategy" if is_strategy else "context"
            confluence_id = strategy_confluence_id if is_strategy else context_confluence_id
        elif isinstance(value, Mapping):
            kind = value.get("kind")
            confluence_id = value.get("id")
            if kind not in {"strategy", "context"} or not isinstance(confluence_id, str) or not confluence_id.strip():
                raise ResolverError("confluence objects require kind strategy/context and a non-empty id")
            if kind == "strategy" and confluence_id not in by_id:
                raise ResolverError(f"strategy-derived confluence is not active in registry: {confluence_id}")
            if kind == "context":
                normalized_context = normalize_context_id(Path(root), confluence_id)
                if normalized_context is None:
                    raise ResolverError(f"context confluence is not active in retrospective attribution registry: {confluence_id}")
                confluence_id = normalized_context
        else:
            raise ResolverError("owner_attestation.confluences entries must be IDs or typed objects")
        if confluence_id == strategy_id or confluence_id in confluence_types:
            raise ResolverError("owner_attestation.confluences must be unique and secondary to the primary strategy")
        normalized_confluences.append(confluence_id)
        confluence_types[confluence_id] = kind

    contract = (
        expression_contract(Path(root), strategy_id, selected["rule_version"], expression)
        if expression is not None
        else None
    )
    if contract is not None:
        required_components = {str(value).upper() for value in contract["required_components"]}
        if not required_components <= {value.upper() for value in triggers}:
            raise ResolverError(
                "owner_attestation.entry_triggers conflict with the frozen registry expression contract; "
                "required: " + ", ".join(sorted(required_components))
            )
    plan_facts = raw.get("plan_facts", {})
    if not isinstance(plan_facts, Mapping) or set(plan_facts) - _OWNER_PLAN_FACTS:
        raise ResolverError("owner_attestation.plan_facts contains unsupported fields")
    for key, value in plan_facts.items():
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ResolverError(f"owner_attestation.plan_facts.{key} must be a scalar fact or unknown marker")
    return {
        "timing": "retrospective",
        "setup_status": "HAS_SETUP",
        "primary_strategy_id": strategy_id,
        "rule_version": selected["rule_version"],
        **({"entry_expression": expression} if expression is not None else {}),
        "entry_triggers": [value.upper() for value in triggers],
        "confluences": normalized_confluences,
        "confluence_types": confluence_types,
        "rationale": rationale.strip(),
        "plan_facts": dict(plan_facts),
        "evidence_refs": list(refs),
    }


def _validate_retrospective_observations(raw: Any, attestation: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if attestation is None or attestation.get("setup_status") != "HAS_SETUP":
        raise ResolverError("retrospective_observations require a positive owner_attestation")
    if not isinstance(raw, list):
        raise ResolverError("retrospective_observations must be a list")
    claims = {f"entry_trigger:{value}" for value in attestation["entry_triggers"]}
    claims.update(f"confluence:{value}" for value in attestation["confluences"])
    claims.update(f"plan:{value}" for value in attestation["plan_facts"])
    normalized = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ResolverError("retrospective observation must be an object")
        claim_id = item.get("claim_id")
        if claim_id not in claims:
            raise ResolverError(f"retrospective observation cites an unasserted owner claim: {claim_id}")
        finding = item.get("finding")
        if finding not in {"corroborates", "contradicts", "not_observable"}:
            raise ResolverError("retrospective observation finding must be corroborates, contradicts, or not_observable")
        timing = item.get("timing")
        if timing not in {"pre_trade", "execution", "post_trade", "unknown"}:
            raise ResolverError("retrospective observation requires its true evidence timing")
        refs = item.get("evidence_refs")
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            raise ResolverError("retrospective observation requires non-empty evidence_refs")
        if any(ref.startswith("owner:") for ref in refs):
            raise ResolverError("retrospective visual observations must cite attached evidence, not owner-attestation refs")
        observed_at = item.get("observed_at")
        if observed_at is not None and not _iso_timestamp(observed_at):
            raise ResolverError("retrospective observation observed_at must be an ISO timestamp")
        if finding == "not_observable" and observed_at is not None:
            raise ResolverError("not_observable retrospective observations cannot include observed_at")
        if observed_at is not None and not claim_id.startswith("entry_trigger:"):
            raise ResolverError("observed_at is only valid for an entry-trigger observation")
        contradiction_code = item.get("contradiction_code")
        observed_fact = item.get("observed_fact")
        if finding == "contradicts":
            if not isinstance(contradiction_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", contradiction_code):
                raise ResolverError("contradicts requires an inspectable contradiction_code")
            if not isinstance(observed_fact, str) or not observed_fact.strip():
                raise ResolverError("contradicts requires a concrete observed_fact")
            if contradiction_code == "trigger_after_provider_entry" and (
                not claim_id.startswith("entry_trigger:") or observed_at is None
            ):
                raise ResolverError("trigger_after_provider_entry requires an entry-trigger claim and observed_at")
        elif contradiction_code is not None or observed_fact is not None:
            raise ResolverError("contradiction_code/observed_fact are only valid when finding is contradicts")
        normalized.append(
            {
                "claim_id": claim_id,
                "finding": finding,
                "timing": timing,
                "evidence_refs": list(refs),
                **({"contradiction_code": contradiction_code} if contradiction_code is not None else {}),
                **({"observed_fact": observed_fact.strip()} if isinstance(observed_fact, str) else {}),
                **({"observed_at": observed_at} if observed_at else {}),
            }
        )
    return normalized


def _validate_observations(raw: Any, known_rule_ids: set[str], root: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    by_rule: dict[str, list[dict[str, Any]]] = {}
    for item in raw or []:
        if not isinstance(item, Mapping):
            raise ResolverError("observation must be an object")
        rule_id = item.get("rule_id")
        if rule_id not in known_rule_ids:
            raise ResolverError(f"observation references unknown rule_id: {rule_id}")
        if item.get("state") not in {"PASS", "FAIL"}:
            raise ResolverError(f"observation state must be PASS or FAIL: {rule_id}")
        if "owner_attestation" in item:
            raise ResolverError(
                "per-rule owner_attestation is not an authority schema; use first-class owner_attestation and retrospective_observations"
            )
        refs = item.get("evidence_refs")
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs):
            raise ResolverError(f"observation requires non-empty evidence_refs: {rule_id}")
        supports_fact_timing = item.get("supports_fact_timing", item.get("timing"))
        if supports_fact_timing is not None and supports_fact_timing not in _FACT_TIMINGS:
            raise ResolverError(f"supports_fact_timing is invalid: {rule_id}")
        if item.get("supports_fact_timing") is not None and item.get("timing") is not None and item.get("timing") != supports_fact_timing:
            raise ResolverError(f"observation timing fields conflict: {rule_id}")
        basis = item.get("basis", ["contemporaneous_capture"])
        if (
            not isinstance(basis, list)
            or not basis
            or any(not isinstance(value, str) or value not in _OBSERVATION_BASES for value in basis)
            or len(set(basis)) != len(basis)
        ):
            raise ResolverError(f"observation basis must contain unique supported values: {rule_id}")
        normalized = dict(item)
        if supports_fact_timing is not None:
            normalized["supports_fact_timing"] = supports_fact_timing
        normalized["basis"] = list(basis)
        if item.get("adjudication_id") is not None:
            if root is None:
                raise ResolverError("adjudication citations need a repository root")
            _check_adjudication(root, item)
        by_rule.setdefault(rule_id, []).append(normalized)
    return by_rule


def resolve_strategy(
    root: Path,
    observations: Mapping[str, Any],
    *,
    provider_entry_at: str | None = None,
    provider_execution_verified: bool = False,
    plan_context: Mapping[str, Any] | None = None,
    only_versions: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Resolve strategy rules or an Owner's retrospective intent attestation.

    Rule observations retain the frozen rule's timing constraints. A separate
    retrospective visual-observation lane accepts truthful post-trade evidence
    as corroboration or contradiction without treating it as pre-trade rule
    evidence. Provider entry time is supplied separately as execution authority.
    """

    strategies = active_strategies(root, only_versions=only_versions)
    known = {rule["rule_id"] for s in strategies for v in s["usable_versions"] for rule in v["rules"]}
    by_rule = _validate_observations(observations.get("observations"), known, root)
    owner_setup = _validate_owner_setup_attribution(observations.get("owner_setup_attribution"))
    owner_attestation = validate_owner_attestation(
        root, observations.get("owner_attestation"), strategies=strategies
    )
    retrospective_observations = _validate_retrospective_observations(
        observations.get("retrospective_observations"), owner_attestation
    )
    if owner_attestation is not None and (
        by_rule or owner_setup is not None or observations.get("owner_no_match") is not None
    ):
        raise ResolverError(
            "owner_attestation is a standalone retrospective authority path; use retrospective_observations for replay evidence and do not mix it with rule observations or other Owner adjudications"
        )
    if provider_entry_at is not None and not _iso_timestamp(provider_entry_at):
        raise ResolverError("provider_entry_at must be an ISO timestamp from provider execution evidence")
    plan = observations.get("plan") if isinstance(observations.get("plan"), Mapping) else {}
    if isinstance(plan_context, Mapping):
        # An already-persisted plan is authoritative for later amendments and
        # cannot be overridden by a caller-provided resolver JSON file.
        plan = {**dict(plan), **dict(plan_context)}

    matrix = []
    for s, version in ((s, v) for s in strategies for v in s["usable_versions"]):
        rules = []
        for rule in version["rules"]:
            if rule.get("status") == "inactive" or rule.get("applicable") is False or not rule.get("required", False):
                continue
            state, code, refs, fact_timing, basis = _rule_state(rule, by_rule.get(rule["rule_id"], []), plan)
            rule_result = {"rule_id": rule["rule_id"], "state": state, "code": code, "evidence_refs": refs}
            if fact_timing is not None:
                rule_result["supports_fact_timing"] = fact_timing
            if basis:
                rule_result["basis"] = basis
            rules.append(rule_result)
        if any(r["state"] == "FAIL" for r in rules):
            result = "FAIL"
        elif any(r["state"] == "UNKNOWN" for r in rules):
            result = "INSUFFICIENT"
        else:
            result = "MATCH"
        # A child's own identity rule comes last (inherited parent rules come first).
        identity_rules = [r for r in rules if r["rule_id"].endswith(".identity")]
        identity_pass = bool(identity_rules) and identity_rules[-1]["state"] == "PASS"
        matrix.append(
            {
                "strategy_id": s["strategy_id"],
                "parent_strategy_id": s["parent_strategy_id"],
                "rule_version": version["rule_version"],
                "result": result,
                "identity_pass": identity_pass,
                "rules": rules,
            }
        )

    # A strategy may have several usable versions. They are alternatives of one strategy, not
    # competing strategies: the newest version that matches represents it, else the newest.
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for m in matrix:
        by_strategy.setdefault(m["strategy_id"], []).append(m)
    representatives = [
        next((e for e in reversed(entries) if e["result"] == "MATCH"), entries[-1]) for entries in by_strategy.values()
    ]
    matched = [m for m in representatives if m["result"] == "MATCH"]
    parents_of_matched = {m["parent_strategy_id"] for m in matched}
    matched = [m for m in matched if m["strategy_id"] not in parents_of_matched]  # child is more specific than its parent
    competing = [
        m for m in representatives
        if m["result"] == "INSUFFICIENT" and m["identity_pass"] and m["strategy_id"] not in parents_of_matched
    ]
    owner = observations.get("owner_no_match") if isinstance(observations.get("owner_no_match"), Mapping) else None

    status, reason, chosen = "INSUFFICIENT_EVIDENCE", "at least one active strategy cannot be decided from the supplied evidence", None
    resolution_authority = "strategy_rules"
    material_conflicts: list[dict[str, Any]] = []
    confidence = "medium"
    if owner_attestation is not None and owner_attestation["setup_status"] == "NO_SETUP":
        status = "NO_MATCH"
        reason = "Owner retrospectively attested that the actual entry had no setup: " + owner_attestation["rationale"]
        resolution_authority = OWNER_RETROSPECTIVE_AUTHORITY
    elif owner_attestation is not None:
        base = next(s for s in strategies if s["strategy_id"] == owner_attestation["primary_strategy_id"])
        attested_version = next(v for v in base["usable_versions"] if v["rule_version"] == owner_attestation["rule_version"])
        chosen = {**base, "rule_version": attested_version["rule_version"], "rules": attested_version["rules"]}
        resolution_authority = OWNER_RETROSPECTIVE_AUTHORITY
        for item in retrospective_observations:
            conflict_reason = None
            if item["finding"] == "contradicts":
                conflict_reason = item["observed_fact"]
                code = item["contradiction_code"]
                if code == "trigger_after_provider_entry":
                    if provider_entry_at is None or not provider_execution_verified:
                        raise ResolverError(
                            "trigger_after_provider_entry requires provider_entry_at from verified provider execution"
                        )
                    claim_at = datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
                    entry_at = datetime.fromisoformat(provider_entry_at.replace("Z", "+00:00"))
                    if claim_at <= entry_at:
                        raise ResolverError(
                            "trigger_after_provider_entry does not match the supplied event and provider timestamps"
                        )
            elif (
                item.get("observed_at") is not None
                and provider_entry_at is not None
                and provider_execution_verified
            ):
                claim_at = datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
                entry_at = datetime.fromisoformat(provider_entry_at.replace("Z", "+00:00"))
                if claim_at > entry_at:
                    conflict_reason = (
                        f"{item['claim_id']} was observed at {item['observed_at']}, after verified "
                        f"provider entry at {provider_entry_at}"
                    )
                    code = "trigger_after_provider_entry"
                else:
                    code = None
            elif item.get("observed_at") is not None:
                # Without a provider-verified timestamp, chronology is
                # unobservable even when the chart itself has a timestamp.
                confidence = "low"
                code = None
            else:
                code = item.get("contradiction_code")
            if conflict_reason:
                material_conflicts.append(
                    {
                        "claim_id": item["claim_id"],
                        "reason": conflict_reason,
                        "contradiction_code": code,
                        "observed_fact": conflict_reason,
                        "evidence_refs": list(item["evidence_refs"]),
                        **({"observed_at": item["observed_at"]} if item.get("observed_at") else {}),
                        **({"provider_entry_at": provider_entry_at} if provider_entry_at else {}),
                    }
                )
        for field, owner_value in owner_attestation["plan_facts"].items():
            recorded_value = plan.get(field)
            if recorded_value in (None, "unknown", "not_observable") or owner_value in ("unknown", "not_observable"):
                continue
            try:
                agrees = abs(float(recorded_value) - float(owner_value)) <= 1e-9
            except (TypeError, ValueError):
                agrees = recorded_value == owner_value
            if not agrees:
                material_conflicts.append(
                    {
                        "claim_id": f"plan:{field}",
                        "contradiction_code": "plan_fact_mismatch",
                        "reason": "Owner-recalled plan fact conflicts with the frozen plan fact",
                        "observed_fact": (
                            f"Owner recalled {field}={owner_value}, but the persisted plan records "
                            f"{field}={recorded_value}"
                        ),
                        "evidence_refs": list(owner_attestation.get("evidence_refs", [])),
                        "compared_fact_source": "persisted_registration_plan",
                        "owner_value": owner_value,
                        "recorded_value": recorded_value,
                    }
                )
        if any(item["finding"] == "not_observable" for item in retrospective_observations):
            confidence = "medium"
        if material_conflicts:
            status = "INSUFFICIENT_EVIDENCE"
            chosen = None
            reason = "Material retrospective conflict: " + "; ".join(
                str(item.get("observed_fact") or item.get("reason")) for item in material_conflicts
            )
        else:
            status = "MATCHED"
            reason = "Owner retrospectively attested the actual entry intent; registry mapping is valid and no material evidence conflict was found"
    elif owner_setup is not None:
        status = "NO_MATCH"
        reason = "owner explicitly states the actual entry had no setup: " + owner_setup["reason"]
        resolution_authority = "owner_setup_attribution"
    elif len(matched) > 1:
        status, reason = "AMBIGUOUS", "multiple unrelated strategies match: " + ", ".join(m["strategy_id"] for m in matched)
    elif len(matched) == 1 and competing:
        status = "AMBIGUOUS"
        reason = f"{matched[0]['strategy_id']} matches but {competing[0]['strategy_id']} identity also passes with undecided rules"
    elif len(matched) == 1:
        status, chosen, reason = "MATCHED", matched[0], "all required frozen rules pass"
    elif competing:
        reason = f"{competing[0]['strategy_id']} identity passes but required rules are undecided"
    elif all(m["result"] == "FAIL" for m in matrix):
        status, reason = "NO_MATCH", "every active strategy has at least one failed required rule"
    elif owner is not None:
        reviewed = set(owner.get("reviewed_strategy_ids") or [])
        refs = owner.get("evidence_refs")
        if (
            reviewed >= {s["strategy_id"] for s in strategies}
            and isinstance(owner.get("reason"), str) and owner["reason"].strip()
            and isinstance(refs, list) and refs and all(isinstance(r, str) and r.strip() for r in refs)
        ):
            status, reason = "NO_MATCH", "owner reviewed all active strategies and none applies: " + owner["reason"].strip()
            resolution_authority = "owner_no_match"
        else:
            reason = "owner_no_match must cover every active strategy and carry a reason and evidence_refs"

    result = {
        "resolver_version": RESOLVER_VERSION,
        "status": status,
        "resolved": status in RESOLVED_STATUSES,
        "reason": reason,
        "strategy_id": chosen["strategy_id"] if chosen else None,
        "rule_version": chosen["rule_version"] if chosen else None,
        "resolution_authority": resolution_authority,
        "matrix": matrix,
    }
    if owner_attestation is not None:
        result["owner_attestation"] = owner_attestation
        result["retrospective_observations"] = retrospective_observations
        result["confidence"] = confidence
        result["evidence_mode"] = "retrospective_reconstruction"
        result["evidence_quality"] = [
            *( ["provider_verified"] if provider_execution_verified and provider_entry_at is not None else [] ),
            *( ["provider_entry_unverified"] if provider_entry_at is not None and not provider_execution_verified else [] ),
            "owner_attested",
            *( ["retrospective_reconstructed"] if retrospective_observations else [] ),
            *( ["not_observable"] if any(item["finding"] == "not_observable" for item in retrospective_observations) else [] ),
            *( ["materially_contradicted"] if material_conflicts else [] ),
        ]
        if owner_attestation["setup_status"] == "HAS_SETUP":
            result.update(_owner_resolution_dimensions(owner_attestation))
            if status == "MATCHED" and chosen is not None and not material_conflicts:
                projection = _derive_retrospective_rule_facts(
                    Path(root), chosen, owner_attestation, retrospective_observations, plan
                )
                if projection is not None:
                    result["retrospective_rule_facts"] = projection
    if material_conflicts:
        result["material_conflicts"] = material_conflicts
    return result


def _owner_resolution_dimensions(owner: Any) -> dict[str, Any]:
    if not isinstance(owner, Mapping) or owner.get("setup_status") != "HAS_SETUP":
        return {}
    dimensions: dict[str, Any] = {
        "entry_triggers": list(owner.get("entry_triggers", [])),
        "confluences": list(owner.get("confluences", [])),
        "confluence_types": dict(owner.get("confluence_types", {})),
        "plan_facts": dict(owner.get("plan_facts", {})),
    }
    if owner.get("entry_expression") is not None:
        dimensions["entry_expression"] = owner["entry_expression"]
    return dimensions


def _derive_retrospective_rule_facts(
    root: Path,
    strategy: Mapping[str, Any],
    owner: Mapping[str, Any],
    retrospective_observations: list[Mapping[str, Any]],
    plan: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Project validated Owner authority into exact frozen-rule evidence.

    No second statement-shaped attestation is accepted. A fact is projected
    only when the frozen rule maps to an Owner claim that retrospective
    observations corroborate with true post-trade evidence.
    """

    if owner.get("setup_status") != "HAS_SETUP":
        return None
    by_claim: dict[str, list[Mapping[str, Any]]] = {}
    for item in retrospective_observations:
        claim_id = item.get("claim_id")
        if isinstance(claim_id, str):
            by_claim.setdefault(claim_id, []).append(item)

    def corroborated_refs(claim_ids: list[str]) -> list[str] | None:
        refs: set[str] = set()
        for claim_id in claim_ids:
            items = by_claim.get(claim_id, [])
            if not items or any(
                item.get("finding") != "corroborates" or item.get("timing") != "post_trade"
                for item in items
            ):
                return None
            refs.update(
                ref for item in items for ref in item.get("evidence_refs", [])
                if isinstance(ref, str) and ref.strip()
            )
        return sorted(refs) if refs else None

    facts: list[dict[str, Any]] = []
    strategy_tag = str(strategy.get("strategy_id", "")).removeprefix("stg-").replace("-", "_")
    for rule in strategy.get("rules", []):
        if not isinstance(rule, Mapping):
            continue
        operands = rule.get("operands") if isinstance(rule.get("operands"), Mapping) else {}
        field = str(operands.get("field", ""))
        operator = rule.get("operator")
        claim_ids: list[str] | None = None

        if field == "strategy.variant" and operator == "in":
            expression = owner.get("entry_expression")
            contract = expression_contract(
                root, str(strategy.get("strategy_id")), str(strategy.get("rule_version")), str(expression)
            ) if isinstance(expression, str) else None
            allowed = operands.get("values")
            if contract and isinstance(allowed, list) and expression in allowed:
                claim_ids = [f"entry_trigger:{value}" for value in contract["required_components"]]
        elif field.startswith("plan.") and operator == "exists":
            plan_field = field.split(".", 1)[1]
            value = owner.get("plan_facts", {}).get(plan_field)
            recorded_value = plan.get(plan_field)
            if (
                value is not None
                and recorded_value is not None
                and not (isinstance(value, str) and value.strip().lower() in PLAN_FIELD_UNKNOWN_VALUES)
                and not (
                    isinstance(recorded_value, str)
                    and recorded_value.strip().lower() in PLAN_FIELD_UNKNOWN_VALUES
                )
            ):
                try:
                    same_value = abs(float(recorded_value) - float(value)) <= 1e-9
                except (TypeError, ValueError):
                    same_value = recorded_value == value
                if same_value:
                    claim_ids = [f"plan:{plan_field}"]
        elif operator == "tag_present" and field == "strategy.setup_tags":
            expected = operands.get("value")
            if isinstance(expected, str):
                exact_context = f"confluence:{expected}"
                if exact_context in by_claim:
                    claim_ids = [exact_context]
                elif expected == strategy_tag and owner.get("primary_strategy_id") == strategy.get("strategy_id"):
                    expression = owner.get("entry_expression")
                    contract = expression_contract(
                        root, str(strategy.get("strategy_id")), str(strategy.get("rule_version")), str(expression)
                    ) if isinstance(expression, str) else None
                    triggers = contract.get("required_components", []) if contract else owner.get("entry_triggers", [])
                    if isinstance(triggers, list) and triggers:
                        claim_ids = [f"entry_trigger:{value}" for value in triggers]
                elif expected in owner.get("entry_triggers", []):
                    claim_ids = [f"entry_trigger:{expected}"]

        refs = corroborated_refs(claim_ids) if claim_ids else None
        allowed_timing = set(rule.get("allowed_evidence_timing") or _DEFAULT_TIMINGS)
        if refs and "pre_trade" in allowed_timing:
            facts.append(
                {
                    "rule_id": rule.get("rule_id"),
                    "state": "PASS",
                    "supports_fact_timing": "pre_trade",
                    "basis": ["owner_attestation", "retrospective_replay"],
                    "evidence_refs": refs,
                    "source_claim_ids": claim_ids,
                }
            )

    if not facts:
        return None
    return {
        "schema_version": "retrospective-rule-facts.v1",
        "authority": OWNER_RETROSPECTIVE_AUTHORITY,
        "strategy_id": strategy["strategy_id"],
        "rule_version": strategy["rule_version"],
        "retrospective_attribution_registry_version": RETROSPECTIVE_ATTRIBUTION_REGISTRY_VERSION,
        "retrospective_attribution_registry_digest": historical_attribution_registry_digest(root),
        "rules": facts,
    }


def to_lifecycle_resolution(result: Mapping[str, Any], *, contemporaneous: bool, confidence: str = "high") -> dict[str, Any]:
    """Map any resolver outcome onto the operational ``strategy_resolution`` object."""

    if result.get("status") == "MATCHED":
        resolution = {
            "status": "contemporaneous_confirmed" if contemporaneous else "replay_reconstructed",
            "strategy_id": result["strategy_id"],
            "rule_version": result["rule_version"],
            "strategy_stats_eligible": True,
            "resolution_source": RESOLVER_VERSION,
            "confidence": result.get("confidence", confidence),
            "reason": result["reason"],
        }
        if result.get("resolution_authority") == OWNER_RETROSPECTIVE_AUTHORITY:
            owner = result["owner_attestation"]
            resolution.update(
                {
                    "resolution_authority": OWNER_RETROSPECTIVE_AUTHORITY,
                    "owner_attestation": owner,
                    "retrospective_observations": result.get("retrospective_observations", []),
                    "evidence_mode": result.get("evidence_mode", "retrospective_reconstruction"),
                    "evidence_quality": result.get("evidence_quality", []),
                    **_owner_resolution_dimensions(owner),
                    **(
                        {"retrospective_rule_facts": result["retrospective_rule_facts"]}
                        if isinstance(result.get("retrospective_rule_facts"), Mapping)
                        else {}
                    ),
                }
            )
        selected = next(
            (
                item
                for item in result.get("matrix", [])
                if item.get("strategy_id") == result["strategy_id"]
                and item.get("rule_version") == result["rule_version"]
            ),
            None,
        )
        if isinstance(selected, Mapping):
            resolution["rule_observations"] = {
                "schema_version": "resolved-rule-observations.v2",
                "strategy_id": result["strategy_id"],
                "rule_version": result["rule_version"],
                "rules": [
                    {
                        "rule_id": item.get("rule_id"),
                        "state": item.get("state"),
                        "code": item.get("code"),
                        "evidence_refs": list(item.get("evidence_refs", [])),
                        **({"supports_fact_timing": item["supports_fact_timing"]} if item.get("supports_fact_timing") else {}),
                        **({"basis": list(item["basis"])} if isinstance(item.get("basis"), list) else {}),
                    }
                    for item in selected.get("rules", [])
                ],
            }
        return resolution
    if result.get("status") == "NO_MATCH":
        resolution = {
            "status": "no_match",
            "strategy_id": None,
            "rule_version": None,
            "strategy_stats_eligible": False,
            "resolution_source": RESOLVER_VERSION,
            "confidence": confidence,
            "reason": result["reason"],
        }
        if result.get("resolution_authority") == OWNER_RETROSPECTIVE_AUTHORITY:
            resolution.update(
                {
                    "resolution_authority": OWNER_RETROSPECTIVE_AUTHORITY,
                    "owner_attestation": result.get("owner_attestation"),
                    "evidence_mode": result.get("evidence_mode", "owner_attested"),
                    "evidence_quality": result.get("evidence_quality", ["owner_attested"]),
                }
            )
        return resolution
    if result.get("status") in {"AMBIGUOUS", "INSUFFICIENT_EVIDENCE"}:
        resolution = {
            "status": "unresolved",
            "strategy_id": None,
            "rule_version": None,
            "strategy_stats_eligible": False,
            "resolution_source": RESOLVER_VERSION,
            "confidence": "low" if confidence == "high" else confidence,
            "reason": result["reason"],
            "resolver_status": result["status"],
        }
        if result.get("resolution_authority") == OWNER_RETROSPECTIVE_AUTHORITY:
            owner = result.get("owner_attestation")
            resolution.update(
                {
                    "resolution_authority": OWNER_RETROSPECTIVE_AUTHORITY,
                    "owner_attestation": owner,
                    "retrospective_observations": result.get("retrospective_observations", []),
                    "evidence_mode": result.get("evidence_mode", "retrospective_reconstruction"),
                    "evidence_quality": result.get("evidence_quality", []),
                    "material_conflicts": result.get("material_conflicts", []),
                    **_owner_resolution_dimensions(owner),
                }
            )
        return resolution
    raise ResolverError(f"cannot persist an unresolved strategy result: {result.get('status')}")
