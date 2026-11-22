from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import yaml

from .contracts import validate_strategy
from .vocabularies import EVIDENCE_METHODS, EVIDENCE_TIMINGS, RULE_CATEGORIES, STRATEGY_OPERATORS


VERSION_ID_RE = re.compile(r"^stg-[a-z0-9-]+:v[0-9]+$")
RULE_VERSION_STATUSES = {"candidate", "active", "retired", "historical-unknown"}
FREEZE_SCHEMA_VERSION = "strategy-rule-version.v1"
FREEZE_CLAIM_CLASSES = {
    "REQUIRED",
    "OPTIONAL/CONFIRMATION",
    "CONTEXT",
    "EXECUTION",
    "RISK/INVALIDATION",
    "TARGET/EXIT",
    "EXTERNAL POLICY",
    "UNRESOLVED",
}

DEFAULT_REGISTRY = {
    "registry_version": "strategy-registry.v1",
    "strategies": [
        {
            "strategy_id": "stg-vegas-tunnel",
            "display_name": "Vegas Tunnel",
            "legacy_number": 1,
            "classification": "independent_system",
            "status": "proposed",
            "rule_versions": [],
        },
        {
            "strategy_id": "stg-example-b",
            "display_name": "OP",
            "legacy_number": 11,
            "classification": "independent_system",
            "status": "proposed",
            "rule_versions": [],
        },
    ],
}


def validate_rule_version(version: Mapping[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not VERSION_ID_RE.fullmatch(str(version.get("version_id", ""))):
        errors.append({"path": "version_id", "message": "must be a stable stg-*:vN identifier"})
    if not isinstance(version.get("strategy_id"), str) or not version.get("strategy_id", "").startswith("stg-"):
        errors.append({"path": "strategy_id", "message": "must be a stable stg- slug"})
    if version.get("status") not in RULE_VERSION_STATUSES:
        errors.append({"path": "status", "message": "unknown rule-version status"})
    if version.get("schema_version") is not None and version.get("schema_version") != FREEZE_SCHEMA_VERSION:
        errors.append({"path": "schema_version", "message": f"must equal {FREEZE_SCHEMA_VERSION}"})
    is_frozen_contract = version.get("schema_version") == FREEZE_SCHEMA_VERSION
    frozen_evidence_ids: set[str] = set()
    if is_frozen_contract:
        required = (
            "rule_version",
            "lifecycle_at_freeze",
            "classification",
            "freeze_date",
            "freeze_baseline_sha",
            "authority_class",
            "evidence_refs",
        )
        for key in required:
            if key not in version:
                errors.append({"path": key, "message": "required by the frozen rule-version contract"})
        if version.get("rule_version") != version.get("version_id"):
            errors.append({"path": "rule_version", "message": "must equal version_id"})
        if version.get("lifecycle_at_freeze") not in {"active", "retired", "experimental", "proposed"}:
            errors.append({"path": "lifecycle_at_freeze", "message": "unknown lifecycle at freeze"})
        if version.get("classification") not in {
            "independent_system", "variant", "implementation", "component", "experimental", "unresolved"
        }:
            errors.append({"path": "classification", "message": "unknown strategy classification"})
        freeze_date = version.get("freeze_date")
        try:
            if not isinstance(freeze_date, str):
                raise ValueError
            date.fromisoformat(freeze_date)
        except ValueError:
            errors.append({"path": "freeze_date", "message": "must be an ISO date"})
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(version.get("freeze_baseline_sha", ""))):
            errors.append({"path": "freeze_baseline_sha", "message": "must be a full commit SHA"})
        if not isinstance(version.get("authority_class"), str) or not version.get("authority_class"):
            errors.append({"path": "authority_class", "message": "must be a non-empty authority class"})
        evidence_refs = version.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            errors.append({"path": "evidence_refs", "message": "must be a non-empty list"})
        else:
            evidence_ids: set[str] = set()
            for index, evidence in enumerate(evidence_refs):
                prefix = f"evidence_refs[{index}]"
                if not isinstance(evidence, Mapping):
                    errors.append({"path": prefix, "message": "must be an object"})
                    continue
                evidence_id = evidence.get("evidence_id")
                if not isinstance(evidence_id, str) or not evidence_id:
                    errors.append({"path": f"{prefix}.evidence_id", "message": "required"})
                elif evidence_id in evidence_ids:
                    errors.append({"path": f"{prefix}.evidence_id", "message": "duplicate evidence_id"})
                else:
                    evidence_ids.add(evidence_id)
                if not isinstance(evidence.get("source_ref"), str) or not evidence.get("source_ref"):
                    errors.append({"path": f"{prefix}.source_ref", "message": "required"})
                if evidence.get("timing") not in {"pre_trade", "execution"}:
                    errors.append({"path": f"{prefix}.timing", "message": "frozen evidence must be pre_trade or execution"})
                if not isinstance(evidence.get("locator"), str) or not evidence.get("locator"):
                    errors.append({"path": f"{prefix}.locator", "message": "required"})
            frozen_evidence_ids = evidence_ids
        unresolved = version.get("unresolved", [])
        if not isinstance(unresolved, list):
            errors.append({"path": "unresolved", "message": "must be a list"})
        else:
            for index, item in enumerate(unresolved):
                prefix = f"unresolved[{index}]"
                if not isinstance(item, Mapping):
                    errors.append({"path": prefix, "message": "must be an object"})
                    continue
                if item.get("status") != "unresolved":
                    errors.append({"path": f"{prefix}.status", "message": "must be unresolved"})
                if not isinstance(item.get("decision_packet"), str) or not item.get("decision_packet"):
                    errors.append({"path": f"{prefix}.decision_packet", "message": "required"})
                blocking = item.get("blocking")
                eligibility_impact = item.get("eligibility_impact")
                if blocking not in {True, False}:
                    errors.append({"path": f"{prefix}.blocking", "message": "must be boolean"})
                elif blocking is False and eligibility_impact is not False:
                    errors.append({
                        "path": f"{prefix}.eligibility_impact",
                        "message": "non-blocking unresolved items must not affect strategy eligibility",
                    })
                elif blocking is True and eligibility_impact is not True:
                    errors.append({
                        "path": f"{prefix}.eligibility_impact",
                        "message": "blocking unresolved items must affect strategy eligibility",
                    })
        inherits_from = version.get("inherits_from")
        if inherits_from is not None and not VERSION_ID_RE.fullmatch(str(inherits_from)):
            errors.append({"path": "inherits_from", "message": "must be a stable rule-version identifier"})
    rules = version.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append({"path": "rules", "message": "must be a non-empty list"})
        return errors
    rule_ids: set[str] = set()
    evidence_ids = frozen_evidence_ids
    for index, rule in enumerate(rules):
        prefix = f"rules[{index}]"
        if not isinstance(rule, Mapping):
            errors.append({"path": prefix, "message": "must be an object"})
            continue
        if not rule.get("rule_id"):
            errors.append({"path": f"{prefix}.rule_id", "message": "required"})
        elif rule.get("rule_id") in rule_ids:
            errors.append({"path": f"{prefix}.rule_id", "message": "duplicate rule_id"})
        else:
            rule_ids.add(str(rule.get("rule_id")))
        claim_class = rule.get("claim_class")
        if is_frozen_contract and claim_class not in FREEZE_CLAIM_CLASSES:
            errors.append({"path": f"{prefix}.claim_class", "message": "unknown freeze claim class"})
        if rule.get("category") not in RULE_CATEGORIES:
            errors.append({"path": f"{prefix}.category", "message": "unknown rule category"})
        operator = rule.get("operator")
        if operator not in STRATEGY_OPERATORS:
            errors.append({"path": f"{prefix}.operator", "message": "operator is not allow-listed"})
        operands = rule.get("operands")
        if not isinstance(operands, Mapping):
            errors.append({"path": f"{prefix}.operands", "message": "must be an object"})
        elif operator in {"equals", "not_equals", "in", "not_in", "gte", "lte", "between", "timeframe_gte", "exists"} and not operands.get("field"):
            errors.append({"path": f"{prefix}.operands.field", "message": "exact evaluated field is required"})
        elif operator in {"tag_present", "tag_absent"} and operands.get("field") != "strategy.setup_tags":
            errors.append({"path": f"{prefix}.operands.field", "message": "tag rules must evaluate strategy.setup_tags"})
        timings = rule.get("allowed_evidence_timing", ["pre_trade", "execution"])
        if not isinstance(timings, list) or any(item not in EVIDENCE_TIMINGS for item in timings):
            errors.append({"path": f"{prefix}.allowed_evidence_timing", "message": "contains an unknown evidence timing"})
        methods = rule.get("evidence_requirements", [])
        if not isinstance(methods, list) or any(item not in EVIDENCE_METHODS for item in methods):
            errors.append({"path": f"{prefix}.evidence_requirements", "message": "contains an unknown evidence method"})
        if is_frozen_contract:
            rule_evidence = rule.get("evidence_refs", [])
            if not isinstance(rule_evidence, list) or any(item not in evidence_ids for item in rule_evidence):
                errors.append({"path": f"{prefix}.evidence_refs", "message": "must reference declared evidence_id values"})
            timings = rule.get("allowed_evidence_timing", [])
            if "post_trade" in timings or "unknown" in timings:
                errors.append({"path": f"{prefix}.allowed_evidence_timing", "message": "frozen rules cannot use post-trade or unknown evidence"})
            if not isinstance(rule.get("statement"), str) or not rule.get("statement"):
                errors.append({"path": f"{prefix}.statement", "message": "required by the frozen rule-version contract"})
    return errors


def validate_registry(registry: dict[str, Any], root: Path | None = None) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if registry.get("registry_version") != "strategy-registry.v1":
        errors.append({"path": "registry_version", "message": "must equal strategy-registry.v1"})
    strategies = registry.get("strategies")
    if not isinstance(strategies, list):
        return errors + [{"path": "strategies", "message": "must be a list"}]
    all_ids = {s.get("strategy_id") for s in strategies if isinstance(s, Mapping)}
    known_ids: set[str] = set()
    for index, strategy in enumerate(strategies):
        if not isinstance(strategy, Mapping):
            errors.append({"path": f"strategies[{index}]", "message": "must be an object"})
            continue
        strategy_errors = validate_strategy(strategy)
        for error in strategy_errors:
            errors.append({"path": f"strategies[{index}].{error.path}", "message": error.message})
        strategy_id = strategy.get("strategy_id")
        if isinstance(strategy_id, str):
            if strategy_id in known_ids:
                errors.append({"path": f"strategies[{index}].strategy_id", "message": "duplicate strategy_id"})
            known_ids.add(strategy_id)
        parent = strategy.get("parent_strategy_id")
        if isinstance(parent, str) and parent not in all_ids:
            errors.append({"path": f"strategies[{index}].parent_strategy_id", "message": "parent_strategy_id is not a known strategy"})
        listed = [v for v in strategy.get("rule_versions", []) if isinstance(v, str)]
        numbers = [int(v.rsplit(":v", 1)[1]) for v in listed if re.fullmatch(r"stg-[a-z0-9-]+:v[0-9]+", v)]
        if numbers != sorted(set(numbers)) or len(numbers) != len(listed):
            errors.append({
                "path": f"strategies[{index}].rule_versions",
                "message": "must list versions in ascending version order without duplicates (the newest is the default)",
            })
        for version_index, version in enumerate(strategy.get("rule_versions", [])):
            if not isinstance(version, str):
                continue
            if root is None:
                continue
            try:
                version_payload = load_version(root, version)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                errors.append({"path": f"strategies[{index}].rule_versions[{version_index}]", "message": str(exc)})
                continue
            if version_payload.get("strategy_id") != strategy_id:
                errors.append({
                    "path": f"strategies[{index}].rule_versions[{version_index}]",
                    "message": "version belongs to a different strategy",
                })
    return errors


def registry_digest(registry: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# The registry as the B1/B3 research freeze saw it: these strategies and exactly these rule
# versions. Never changes. Everything else in the registry is "after B3": new strategies, new
# rule versions appended to a baseline strategy, and `retired_rule_versions` are free to be
# added without touching the historical freeze, while editing or deleting anything below is not.
B3_BASELINE_RULE_VERSIONS: dict[str, tuple[str, ...]] = {
    "stg-vegas-tunnel": ("stg-vegas-tunnel:v1",),
    "stg-po3": ("stg-po3:v1",),
    "stg-example-b": ("stg-example-b:v1",),
    "stg-ict-liquidity": ("stg-ict-liquidity:v1",),
    "stg-triple-tap": ("stg-triple-tap:v1",),
    "stg-liq-sweep": (),
    "stg-17ema": (),
    "stg-ma-cluster": (),
    "stg-fib-rsi": (),
    "stg-unicorn": (),
    "stg-ifvg-mss": (),
    "stg-ott-auto": (),
    "stg-1272-fakeout": (),
}


def registry_as_of_b3(registry: dict[str, Any]) -> dict[str, Any]:
    """Project a registry back to what B3 froze: baseline strategies with baseline versions only."""

    strategies = []
    for item in registry.get("strategies", []):
        if not isinstance(item, Mapping) or item.get("strategy_id") not in B3_BASELINE_RULE_VERSIONS:
            continue
        baseline_versions = B3_BASELINE_RULE_VERSIONS[item["strategy_id"]]
        projected = {key: value for key, value in item.items() if key != "retired_rule_versions"}
        projected["rule_versions"] = [v for v in item.get("rule_versions", []) if v in baseline_versions]
        strategies.append(projected)
    return {**registry, "strategies": strategies}


def usable_rule_versions(root: Path, strategy: Mapping[str, Any]) -> list[str]:
    """Rule versions new trades may use: listed, not retired by the Owner, and active."""

    retired = set(strategy.get("retired_rule_versions") or [])
    return [
        version
        for version in strategy.get("rule_versions", [])
        if isinstance(version, str) and version not in retired and load_version(root, version).get("status") == "active"
    ]


def strategy_version_digest(root: Path, version_id: str) -> str:
    """Digest of a frozen rule version and every version it inherits from.

    The parsed content (independent of file line endings) of the selected version and of
    each ancestor reached through `inherits_from` is hashed in order, so a change to an
    inherited parent rule changes the digest of the child that relies on it.
    """

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = version_id
    while current:
        if current in seen:
            raise ValueError(f"strategy version inheritance cycle: {current}")
        seen.add(current)
        payload = load_version(root, current)
        chain.append(payload)
        parent = payload.get("inherits_from")
        current = str(parent) if parent else None
    return hashlib.sha256(
        json.dumps(chain, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _registry_path(root: Path) -> Path:
    return root.resolve() / "registry" / "strategy-registry.yaml"


def resolve_version_path(root: Path, version_id: str) -> Path:
    if not VERSION_ID_RE.fullmatch(version_id):
        raise ValueError("strategy version must be a stable stg-*:vN identifier")
    versions_dir = (root.resolve() / "registry" / "versions").resolve()
    path = (versions_dir / (version_id.replace(":", "-") + ".yaml")).resolve()
    try:
        path.relative_to(versions_dir)
    except ValueError as exc:
        raise ValueError("strategy version resolves outside registry/versions") from exc
    if not path.exists():
        raise FileNotFoundError(f"strategy version not found: {version_id}")
    return path


def load_version(root: Path, version_id: str) -> dict[str, Any]:
    path = resolve_version_path(root, version_id)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"strategy version is not a mapping: {version_id}")
    if payload.get("version_id") != version_id:
        raise ValueError(f"strategy version file/id mismatch: {version_id}")
    errors = validate_rule_version(payload)
    if errors:
        raise ValueError("invalid strategy version: " + "; ".join(error["message"] for error in errors))
    return payload


def load_effective_rules(root: Path, version_id: str) -> list[dict[str, Any]]:
    """Resolve a frozen version's explicit parent chain without duplicating rules."""

    def walk(current_id: str, seen: set[str]) -> list[dict[str, Any]]:
        if current_id in seen:
            raise ValueError(f"strategy version inheritance cycle: {current_id}")
        seen.add(current_id)
        version = load_version(root, current_id)
        parent_id = version.get("inherits_from")
        inherited = walk(str(parent_id), seen) if parent_id else []
        own = [dict(rule) for rule in version.get("rules", [])]
        ids = [rule.get("rule_id") for rule in inherited + own]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate rule_id in effective version: {current_id}")
        return inherited + own

    return walk(version_id, set())


def load_registry(root: Path) -> dict[str, Any]:
    path = _registry_path(root)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("strategy registry is not a mapping")
    errors = validate_registry(payload, root.resolve())
    if errors:
        raise ValueError("invalid strategy registry: " + "; ".join(error["message"] for error in errors))
    return payload


def load_strategy(root: Path, strategy_id: str) -> dict[str, Any]:
    registry = load_registry(root)
    for strategy in registry.get("strategies", []):
        if strategy.get("strategy_id") == strategy_id:
            return dict(strategy)
    raise ValueError(f"strategy not found: {strategy_id}")
