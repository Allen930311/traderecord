"""Append-only adjudication storage and the Phase 4B decision contract.

The Phase 3 pilot wrote small fixture-only revisions with :class:`AdjudicationLog`.
Phase 4B keeps that compatibility path, while adding a strict durable revision
API and an intentionally small allow-list of machine rules.  This module is not
a general-purpose rules engine: unknown rules and human-only rules fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


AUTHORITY_CLASSES = frozenset(
    {
        "deterministic_machine_rule",
        "human_adjudication_required",
        "insufficient_evidence_keep_unknown",
    }
)
MACHINE_AUTHORITY = "deterministic_machine_rule"
HUMAN_AUTHORITY = "human_adjudication_required"
UNKNOWN_AUTHORITY = "insufficient_evidence_keep_unknown"
DURABLE_REVISION_SCHEMA_VERSION = "phase4b.adjudication-revision.v1"


class RuleContractError(ValueError):
    """Raised when a rule or revision is not an approved Phase 4B contract."""


_MACHINE_RESULT_TOKEN = object()
_MACHINE_REVISION_TOKEN = object()


class MachineRuleResult(dict[str, Any]):
    """Handler output carrying an in-process provenance marker.

    Durable callers should obtain this object from ``execute_machine_rule`` and
    pass it to ``build_machine_revision``.  The marker prevents a plain caller
    supplied mapping from masquerading as a handler result in that path; the
    durable row also stores a deterministic output digest for later audits.
    """

    def __init__(self, payload: Mapping[str, Any], *, _token: object | None = None):
        if _token is not _MACHINE_RESULT_TOKEN:
            raise RuleContractError("machine rule results must come from execute_machine_rule")
        super().__init__(payload)
        self._execution_marker = _MACHINE_RESULT_TOKEN

    @classmethod
    def _from_handler(cls, payload: Mapping[str, Any]) -> "MachineRuleResult":
        """Create the opaque result wrapper for an approved handler only."""

        return cls(payload, _token=_MACHINE_RESULT_TOKEN)

    def as_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self, ensure_ascii=False, sort_keys=True))


class MachineDurableRevision(dict[str, Any]):
    """Builder-only envelope accepted by the durable machine write boundary."""

    def __init__(self, payload: Mapping[str, Any], *, _token: object | None = None):
        if _token is not _MACHINE_REVISION_TOKEN:
            raise RuleContractError("machine durable revisions must come from build_machine_revision")
        super().__init__(payload)
        self._builder_marker = _MACHINE_REVISION_TOKEN

    @classmethod
    def _from_builder(cls, payload: Mapping[str, Any]) -> "MachineDurableRevision":
        return cls(payload, _token=_MACHINE_REVISION_TOKEN)


@dataclass(frozen=True)
class RuleContract:
    """Machine-readable description of one versioned adjudication rule."""

    rule_id: str
    rule_version: str
    domain: str
    authority_class: str
    preconditions: tuple[str, ...]
    required_evidence: tuple[str, ...]
    allowed_transition: Mapping[str, Any]
    prohibited_shortcuts: tuple[str, ...]
    non_goals: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RuleContract":
        required = {
            "rule_id",
            "rule_version",
            "domain",
            "authority_class",
            "preconditions",
            "required_evidence",
            "allowed_transition",
            "prohibited_shortcuts",
            "non_goals",
        }
        missing = sorted(required - set(value))
        if missing:
            raise RuleContractError("rule contract missing: " + ", ".join(missing))
        authority = value["authority_class"]
        if authority not in AUTHORITY_CLASSES:
            raise RuleContractError(f"unsupported authority class: {authority}")
        if not isinstance(value["rule_id"], str) or not value["rule_id"].strip():
            raise RuleContractError("rule_id must be non-empty")
        if not isinstance(value["rule_version"], str) or not value["rule_version"].strip():
            raise RuleContractError("rule_version must be non-empty")
        if not isinstance(value["domain"], str) or not value["domain"].strip():
            raise RuleContractError("rule domain must be non-empty")
        for field in ("preconditions", "required_evidence", "prohibited_shortcuts", "non_goals"):
            if not isinstance(value[field], list) or not all(isinstance(item, str) and item.strip() for item in value[field]):
                raise RuleContractError(f"{field} must be a non-empty-string list")
        if not isinstance(value["allowed_transition"], dict):
            raise RuleContractError("allowed_transition must be an object")
        return cls(
            rule_id=value["rule_id"],
            rule_version=value["rule_version"],
            domain=value["domain"],
            authority_class=authority,
            preconditions=tuple(value["preconditions"]),
            required_evidence=tuple(value["required_evidence"]),
            allowed_transition=value["allowed_transition"],
            prohibited_shortcuts=tuple(value["prohibited_shortcuts"]),
            non_goals=tuple(value["non_goals"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "domain": self.domain,
            "authority_class": self.authority_class,
            "preconditions": list(self.preconditions),
            "required_evidence": list(self.required_evidence),
            "allowed_transition": dict(self.allowed_transition),
            "prohibited_shortcuts": list(self.prohibited_shortcuts),
            "non_goals": list(self.non_goals),
        }


class RuleRegistry:
    """Validated lookup for the explicitly approved Phase 4B rule contracts."""

    def __init__(self, rules: Iterable[RuleContract]):
        self._rules: dict[tuple[str, str], RuleContract] = {}
        for rule in rules:
            key = (rule.rule_id, rule.rule_version)
            if key in self._rules:
                raise RuleContractError(f"duplicate rule contract: {rule.rule_id}@{rule.rule_version}")
            self._rules[key] = rule

    @classmethod
    def from_json(cls, path: Path | str) -> "RuleRegistry":
        source = Path(path)
        payload = json.loads(source.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "phase4b.rule-registry.v1":
            raise RuleContractError("unsupported rule registry schema")
        rules = payload.get("rules")
        if not isinstance(rules, list):
            raise RuleContractError("rule registry rules must be a list")
        return cls(RuleContract.from_mapping(item) for item in rules)

    def get(self, rule_id: str, rule_version: str) -> RuleContract | None:
        return self._rules.get((rule_id, rule_version))

    def require(self, rule_id: str, rule_version: str) -> RuleContract:
        rule = self.get(rule_id, rule_version)
        if rule is None:
            raise RuleContractError(f"unknown or unapproved rule: {rule_id}@{rule_version}")
        return rule

    def require_machine(self, rule_id: str, rule_version: str) -> RuleContract:
        rule = self.require(rule_id, rule_version)
        if rule.authority_class != MACHINE_AUTHORITY:
            raise RuleContractError(
                f"rule is not machine-executable: {rule_id}@{rule_version} "
                f"({rule.authority_class})"
            )
        return rule

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "phase4b.rule-registry.v1",
            "rules": [
                self._rules[key].as_dict()
                for key in sorted(self._rules)
            ],
        }


def default_rule_registry() -> RuleRegistry:
    """Load the committed Phase 4B registry without creating any records."""

    path = Path(__file__).resolve().parents[2] / "canonical" / "adjudications" / "rule-registry.json"
    return RuleRegistry.from_json(path)


def durable_revision_id(
    decision_domain: str,
    decision_type: str,
    record_ids: Iterable[str],
    rule_id: str | None,
    rule_version: str | None,
    actor: str,
    recorded_at: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> str:
    """Return a stable opaque revision identity from the decision envelope."""

    material = json.dumps(
        {
            "decision_domain": decision_domain,
            "decision_type": decision_type,
            "record_ids": sorted(record_ids),
            "rule_id": rule_id,
            "rule_version": rule_version,
            "actor": actor,
            "recorded_at": recorded_at,
            "before": before,
            "after": after,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "adjv1-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _machine_output_digest(payload: Mapping[str, Any]) -> str:
    material = json.dumps(
        {
            "rule_id": payload.get("rule_id"),
            "rule_version": payload.get("rule_version"),
            "domain": payload.get("domain"),
            "authority_class": payload.get("authority_class"),
            "resolution": payload.get("resolution"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "outv1-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _iso_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_durable_revision(
    revision: Mapping[str, Any],
    *,
    registry: RuleRegistry | None = None,
) -> None:
    """Validate the append-only Phase 4B revision envelope.

    The legacy fixture writer intentionally remains permissive. Serialized
    durable rows may be checked here for read/audit purposes; production-facing
    machine writes must use ``build_machine_revision`` and ``append_durable``.
    """

    active_registry = registry or default_rule_registry()
    required = {
        "schema_version",
        "revision_id",
        "decision_domain",
        "decision_type",
        "status",
        "rule_id",
        "rule_version",
        "actor",
        "authority_class",
        "recorded_at",
        "record_ids",
        "before",
        "after",
        "reason",
        "evidence_refs",
        "previous_revision_id",
        "reopens_revision_id",
        "supersedes_revision_id",
    }
    missing = sorted(required - set(revision))
    if missing:
        raise RuleContractError("durable revision missing: " + ", ".join(missing))
    if revision["schema_version"] != DURABLE_REVISION_SCHEMA_VERSION:
        raise RuleContractError("unsupported durable revision schema")
    if not isinstance(revision["revision_id"], str) or not revision["revision_id"].strip():
        raise RuleContractError("revision_id must be non-empty")
    if not isinstance(revision["decision_domain"], str) or not revision["decision_domain"].strip():
        raise RuleContractError("decision_domain must be non-empty")
    if not isinstance(revision["decision_type"], str) or not revision["decision_type"].strip():
        raise RuleContractError("decision_type must be non-empty")
    if not isinstance(revision["status"], str) or not revision["status"].strip():
        raise RuleContractError("status must be non-empty")
    authority = revision["authority_class"]
    if authority not in AUTHORITY_CLASSES:
        raise RuleContractError(f"unsupported authority class: {authority}")
    if not isinstance(revision["actor"], str) or not revision["actor"].strip():
        raise RuleContractError("actor must be non-empty")
    if not _iso_timestamp(revision["recorded_at"]):
        raise RuleContractError("recorded_at must be an ISO-8601 timestamp")
    record_ids = revision["record_ids"]
    if not isinstance(record_ids, list) or not record_ids or not all(isinstance(item, str) and item.strip() for item in record_ids):
        raise RuleContractError("record_ids must be a non-empty list of strings")
    if len(set(record_ids)) != len(record_ids):
        raise RuleContractError("record_ids must be unique")
    if not isinstance(revision["before"], dict) or not isinstance(revision["after"], dict):
        raise RuleContractError("before and after must be objects")
    if not isinstance(revision["reason"], str) or not revision["reason"].strip():
        raise RuleContractError("reason must be non-empty")
    if not isinstance(revision["evidence_refs"], list) or not all(isinstance(item, str) and item.strip() for item in revision["evidence_refs"]):
        raise RuleContractError("evidence_refs must be a list of strings")
    for relation in ("previous_revision_id", "reopens_revision_id", "supersedes_revision_id"):
        value = revision[relation]
        if value is not None and (not isinstance(value, str) or not value.strip() or value == revision["revision_id"]):
            raise RuleContractError(f"{relation} must be null or a different revision ID")
    rule_id = revision["rule_id"]
    rule_version = revision["rule_version"]
    if authority == MACHINE_AUTHORITY:
        if not isinstance(rule_id, str) or not isinstance(rule_version, str):
            raise RuleContractError("machine revisions require rule_id and rule_version")
        rule = active_registry.require_machine(rule_id, rule_version)
        if revision["decision_domain"] != rule.domain:
            raise RuleContractError(
                f"machine revision domain does not match rule: "
                f"{revision['decision_domain']} != {rule.domain}"
            )
        machine_result = revision.get("machine_result")
        if not isinstance(machine_result, dict):
            raise RuleContractError("machine revisions require the execute_machine_rule result")
        expected_result_keys = {
            "rule_id",
            "rule_version",
            "domain",
            "authority_class",
            "resolution",
            "output_digest",
        }
        if set(machine_result) != expected_result_keys:
            raise RuleContractError("machine_result must be an exact execute_machine_rule result")
        if machine_result["rule_id"] != rule_id or machine_result["rule_version"] != rule_version:
            raise RuleContractError("machine_result rule identity does not match revision")
        if machine_result["domain"] != rule.domain or machine_result["authority_class"] != MACHINE_AUTHORITY:
            raise RuleContractError("machine_result authority/domain does not match registered rule")
        if not isinstance(machine_result["resolution"], dict):
            raise RuleContractError("machine_result resolution must be an object")
        if machine_result["output_digest"] != _machine_output_digest(machine_result):
            raise RuleContractError("machine_result output digest is invalid")
        if revision["after"] != machine_result["resolution"]:
            raise RuleContractError("machine revision after state must equal handler output")
    elif "machine_result" in revision:
        raise RuleContractError("machine_result is only valid for machine revisions")
    elif rule_id is not None or rule_version is not None:
        if not isinstance(rule_id, str) or not isinstance(rule_version, str):
            raise RuleContractError("rule_id and rule_version must be both null or both strings")
        rule = active_registry.require(rule_id, rule_version)
        if rule.authority_class != authority:
            raise RuleContractError(
                f"cited rule authority does not match revision authority: "
                f"{rule.authority_class} != {authority}"
            )


def _type_marker(context: Mapping[str, Any]) -> dict[str, Any]:
    assertions = list(context.get("type_assertions") or [])
    explicit = context.get("explicit_type")
    if explicit is not None:
        assertions.append(explicit)
    values = {value for value in assertions if value in {"live", "backtest"}}
    if len(values) == 1 and all(value in {"live", "backtest"} for value in assertions):
        return {"type": next(iter(values)), "classification_status": "resolved", "status": "resolved"}
    if len(values) > 1 or any(value not in {"live", "backtest"} for value in assertions):
        return {"type": None, "classification_status": "pending", "status": "conflicting"}
    return {"type": None, "classification_status": "pending", "status": "pending"}


def _execution_claim_separation(context: Mapping[str, Any]) -> dict[str, Any]:
    """Classify claims without ever promoting them to a fill confirmation."""

    return {
        "execution_verification": "unverified",
        "entry_execution_confirmed": False,
        "status": "keep_unknown",
        "reason": "actual_entry, chart logic, and missing override are not fill proof",
    }


def _identity_candidate(context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "dedupe_status": "review_candidate",
        "duplicate_of": None,
        "lifecycle_identity": "unresolved",
        "status": "candidate",
    }


def _date_identity(context: Mapping[str, Any]) -> dict[str, Any]:
    values = [value for value in (context.get("date_assertions") or []) if value]
    if values and len(set(values)) == 1:
        return {"event_date": values[0], "status": "resolved"}
    return {"event_date": None, "status": "conflicting" if values else "pending"}


def _strategy_alias(context: Mapping[str, Any]) -> dict[str, Any]:
    candidate = context.get("candidate_strategy_id")
    return {
        "strategy_id": "unknown",
        "strategy_id_candidate": candidate,
        "alias_status": "candidate" if candidate else "unresolved",
        "promoted": False,
        "status": "candidate" if candidate else "keep_unknown",
    }


def _evidence_state(context: Mapping[str, Any]) -> dict[str, Any]:
    items = list(context.get("evidence") or [])
    entry = any(item.get("chart_role") == "entry" and item.get("source_available") is True and item.get("logic_relevance") == "entry_logic" for item in items)
    exit_chart = any(item.get("chart_role") == "exit" and item.get("source_available") is True and item.get("logic_relevance") == "exit_logic" for item in items)
    return {
        "entry_chart_reviewable": entry,
        "exit_chart_reviewable": exit_chart,
        "replay_penalty": 0,
        "status": "resolved" if entry or exit_chart else "missing",
    }


def _result_breakeven(context: Mapping[str, Any]) -> dict[str, Any]:
    value = context.get("realized_r_gross")
    explicit = bool(context.get("explicit_breakeven"))
    if explicit and value == 0:
        return {"outcome": "breakeven", "realized_r_gross": 0, "status": "resolved"}
    if value == 0 or context.get("result") in {None, "", "pending"}:
        return {"outcome": "unknown", "realized_r_gross": None, "status": "keep_unknown"}
    return {"outcome": "unknown", "realized_r_gross": value, "status": "pending"}


def _observation_route(context: Mapping[str, Any]) -> dict[str, Any]:
    explicit = context.get("explicit_no_trade") is True
    return {
        "route": "observation" if explicit else "keep_candidate",
        "observation_id": None,
        "status": "routed" if explicit else "keep_unknown",
    }


def _execution_confirmation(
    context: Mapping[str, Any],
    *,
    basis: str,
    expected_type: str,
) -> dict[str, Any]:
    """Confirm one execution leg only from the existing strict claim validator.

    The handler deliberately accepts a record, rather than an ``after`` mapping,
    as its input.  It constructs the proposed claim locally and validates a
    copy of the record with that claim before returning the handler result.  A
    caller therefore cannot use a registered rule as a generic authorization
    token for unrelated state.
    """

    record = context.get("record")
    leg_name = context.get("leg")
    if not isinstance(record, Mapping):
        raise RuleContractError("execution confirmation requires a source record")
    if leg_name not in {"entry", "exit"}:
        raise RuleContractError("execution confirmation requires leg=entry or leg=exit")
    if record.get("record_kind") not in {"trade_candidate", "trade"}:
        raise RuleContractError("execution confirmation requires a trade-shaped source candidate")
    if record.get("type") != expected_type or record.get("classification_status") != "resolved":
        raise RuleContractError("execution confirmation requires a resolved mode and classification")

    actual = record.get("actual")
    if not isinstance(actual, Mapping):
        raise RuleContractError("execution confirmation requires an actual object")
    verification = actual.get("execution_verification")
    if not isinstance(verification, Mapping) or verification.get("schema_version") != "execution-verification.v1":
        raise RuleContractError("execution confirmation requires execution-verification.v1")
    prior_claim = verification.get(leg_name)
    if not isinstance(prior_claim, Mapping):
        raise RuleContractError("execution confirmation requires the selected leg claim")
    if prior_claim.get("state") not in {"unknown", "unverified"}:
        raise RuleContractError("execution confirmation only transitions unknown/unverified claims")

    from .execution_verification import validate_execution_contract

    # Validate the source contract before proposing a transition.  In
    # particular, a malformed/reserved basis (such as declared_simulated) must
    # not be hidden by replacing only the selected leg.
    source_report = validate_execution_contract(record)
    if not source_report.ok:
        raise RuleContractError("source execution contract is invalid: " + "; ".join(source_report.errors))

    execution_evidence = actual.get("execution_evidence")
    evidence_for_leg = execution_evidence.get(leg_name) if isinstance(execution_evidence, Mapping) else None
    evidence = evidence_for_leg.get(basis) if isinstance(evidence_for_leg, Mapping) else None
    if not isinstance(evidence, Mapping):
        raise RuleContractError(f"{leg_name}.{basis} evidence is required")
    source_ref = evidence.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise RuleContractError(f"{leg_name}.{basis} evidence requires source_ref")

    claim = {
        "state": "confirmed",
        "basis": basis,
        "attestation_support": None,
        "evidence_refs": [source_ref.strip()],
        "adjudication_revision_ref": None,
    }
    proposed = deepcopy(dict(record))
    proposed_actual = deepcopy(dict(actual))
    proposed_verification = deepcopy(dict(verification))
    proposed_verification[leg_name] = claim
    proposed_actual["execution_verification"] = proposed_verification
    proposed["actual"] = proposed_actual
    # Source candidates remain candidates.  The validator needs the
    # trade-shaped contract only to check the execution claim itself; this
    # temporary projection is never persisted as a canonical record.
    proposed["record_kind"] = "trade"

    report = validate_execution_contract(proposed)
    if not report.ok:
        raise RuleContractError("execution confirmation failed closed: " + "; ".join(report.errors))
    return {
        "leg": leg_name,
        "execution_claim": claim,
        "status": "resolved",
    }


_MACHINE_HANDLERS: dict[tuple[str, str], Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    ("type.explicit-source-marker", "1.0"): _type_marker,
    ("execution.claim-separation", "1.0"): _execution_claim_separation,
    ("identity.candidate-discovery", "1.0"): _identity_candidate,
    ("identity.date-consensus", "1.0"): _date_identity,
    ("strategy.alias-candidate", "1.0"): _strategy_alias,
    ("evidence.chart-role-state", "1.0"): _evidence_state,
    ("result.trustworthy-breakeven-zero", "1.0"): _result_breakeven,
    ("observation.explicit-route", "1.0"): _observation_route,
    ("execution.external-fill-confirmation", "1.0"): lambda context: _execution_confirmation(
        context, basis="external_fill", expected_type="live"
    ),
    ("execution.simulator-run-confirmation", "1.0"): lambda context: _execution_confirmation(
        context, basis="simulator_run", expected_type="backtest"
    ),
}


def execute_machine_rule(
    rule_id: str,
    rule_version: str,
    context: Mapping[str, Any],
    *,
    registry: RuleRegistry | None = None,
) -> MachineRuleResult:
    """Execute one explicit machine rule; unknown/human rules fail closed."""

    active_registry = registry or default_rule_registry()
    rule = active_registry.require_machine(rule_id, rule_version)
    handler = _MACHINE_HANDLERS.get((rule.rule_id, rule.rule_version))
    if handler is None:
        raise RuleContractError(f"machine rule has no explicit handler: {rule_id}@{rule_version}")
    payload = {
        "rule_id": rule.rule_id,
        "rule_version": rule.rule_version,
        "domain": rule.domain,
        "authority_class": rule.authority_class,
        "resolution": handler(context),
    }
    payload["output_digest"] = _machine_output_digest(payload)
    return MachineRuleResult._from_handler(payload)


def build_machine_revision(
    execution_result: MachineRuleResult,
    *,
    decision_type: str,
    record_ids: list[str],
    actor: str,
    recorded_at: str,
    before: dict[str, Any],
    reason: str,
    evidence_refs: list[str] | None = None,
    status: str = "resolved",
    previous_revision_id: str | None = None,
    reopens_revision_id: str | None = None,
    supersedes_revision_id: str | None = None,
    registry: RuleRegistry | None = None,
) -> MachineDurableRevision:
    """Build a durable machine revision from an actual handler result.

    ``after`` is deliberately not an argument: it is copied from the approved
    handler output, and ``validate_durable_revision`` binds the stored state to
    that output and to the registered rule domain.
    """

    if (
        not isinstance(execution_result, MachineRuleResult)
        or getattr(execution_result, "_execution_marker", None) is not _MACHINE_RESULT_TOKEN
    ):
        raise RuleContractError("machine revision requires execute_machine_rule output")
    payload = execution_result.as_dict()
    rule = (registry or default_rule_registry()).require_machine(payload["rule_id"], payload["rule_version"])
    if payload["domain"] != rule.domain:
        raise RuleContractError("machine result domain does not match registered rule")
    after = payload["resolution"]
    revision = {
        "schema_version": DURABLE_REVISION_SCHEMA_VERSION,
        "revision_id": durable_revision_id(
            payload["domain"],
            decision_type,
            record_ids,
            payload["rule_id"],
            payload["rule_version"],
            actor,
            recorded_at,
            before,
            after,
        ),
        "decision_domain": payload["domain"],
        "decision_type": decision_type,
        "status": status,
        "rule_id": payload["rule_id"],
        "rule_version": payload["rule_version"],
        "actor": actor,
        "authority_class": MACHINE_AUTHORITY,
        "recorded_at": recorded_at,
        "record_ids": list(record_ids),
        "before": before,
        "after": after,
        "reason": reason,
        "evidence_refs": list(evidence_refs or []),
        "previous_revision_id": previous_revision_id,
        "reopens_revision_id": reopens_revision_id,
        "supersedes_revision_id": supersedes_revision_id,
        "machine_result": payload,
    }
    validate_durable_revision(revision, registry=registry)
    return MachineDurableRevision._from_builder(revision)


def revision_id(fixture_id: str, decision_type: str, record_refs: Iterable[str]) -> str:
    material = "|".join([fixture_id, decision_type, *sorted(record_refs)])
    return "adj-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def opaque_lifecycle_id(kind: str, lifecycle_key: str) -> str:
    """Derive a deterministic opaque ID from adjudication state only.

    ``lifecycle_key`` is an explicit adjudication resolution key.  It is not
    assembled from source path, source hash, date, pair, direction, strategy,
    or any other mutable source attribute.
    """

    if kind not in {"trade", "observation"}:
        raise ValueError(f"unsupported lifecycle kind: {kind}")
    if not lifecycle_key or not isinstance(lifecycle_key, str):
        raise ValueError("lifecycle_key is required for an opaque lifecycle ID")
    material = f"issue3-adjudicated-lifecycle\0{kind}\0{lifecycle_key}"
    return f"{kind[:5] if kind == 'trade' else 'obs'}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


@dataclass
class AdjudicationLog:
    """A line-oriented log; callers append revisions and never edit old lines."""

    path: Path

    def append(self, revision: dict[str, Any]) -> None:
        """Append a legacy fixture revision.

        Phase 3 fixture exports use a deliberately small envelope.  Durable
        callers must use :meth:`append_durable`, which validates the complete
        Phase 4B contract before writing the same append-only JSONL store.
        """

        required = {"revision_id", "decision_type", "record_refs", "reviewer", "recorded_at", "reason"}
        missing = sorted(required - set(revision))
        if missing:
            raise ValueError("adjudication revision missing: " + ", ".join(missing))
        if self.path.exists():
            existing = list(self.iter_revisions())
            if any(item.get("revision_id") == revision.get("revision_id") for item in existing):
                raise ValueError(f"duplicate revision_id: {revision['revision_id']}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(revision, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    def append_durable(
        self,
        revision: Mapping[str, Any],
        *,
        registry: RuleRegistry | None = None,
    ) -> None:
        """Validate and append a Phase 4B durable revision."""

        if revision.get("authority_class") == MACHINE_AUTHORITY and (
            not isinstance(revision, MachineDurableRevision)
            or getattr(revision, "_builder_marker", None) is not _MACHINE_REVISION_TOKEN
        ):
            raise RuleContractError(
                "machine durable revisions must be built by build_machine_revision"
            )
        validate_durable_revision(revision, registry=registry)
        self._validate_relation_scope(revision)
        durable = dict(revision)
        # Keep the two Phase 3 compatibility aliases in the JSONL row while
        # making the Phase 4B names (`record_ids`, `actor`) authoritative.
        durable.setdefault("record_refs", list(revision["record_ids"]))
        durable.setdefault("reviewer", revision["actor"])
        self.append(durable)

    def _validate_relation_scope(self, revision: Mapping[str, Any]) -> None:
        """Ensure linked durable revisions preserve domain and decision type."""

        linked_ids = {
            revision[relation]
            for relation in ("previous_revision_id", "reopens_revision_id", "supersedes_revision_id")
            if revision.get(relation) is not None
        }
        if not linked_ids:
            return
        existing = {
            item.get("revision_id"): item
            for item in self.iter_revisions()
            if item.get("revision_id")
        }
        for relation in ("previous_revision_id", "reopens_revision_id", "supersedes_revision_id"):
            linked_id = revision.get(relation)
            if linked_id is None:
                continue
            prior = existing.get(linked_id)
            if prior is None:
                raise RuleContractError(f"linked revision does not exist: {linked_id}")
            if (
                prior.get("decision_domain") != revision["decision_domain"]
                or prior.get("decision_type") != revision["decision_type"]
            ):
                raise RuleContractError(
                    f"{relation} must stay within the same decision domain and type"
                )

    # Explicit alias for callers that prefer the contract's terminology.
    append_revision = append_durable

    def iter_revisions(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        revisions: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                revisions.append(json.loads(line))
        return revisions

    def reopen(self, prior_revision_id: str, *, reason: str, reviewer: str, recorded_at: str, record_refs: list[str]) -> dict[str, Any]:
        """Append a new revision that reopens a prior decision without erasing it."""

        prior = next((item for item in self.iter_revisions() if item.get("revision_id") == prior_revision_id), None)
        if prior is None:
            raise ValueError(f"unknown prior revision: {prior_revision_id}")
        if "decision_domain" in prior and "record_ids" in prior:
            return self.reopen_durable(
                prior_revision_id,
                reason=reason,
                actor=reviewer,
                recorded_at=recorded_at,
                record_ids=record_refs,
            )
        new_id = "adj-" + hashlib.sha256(f"{prior_revision_id}|{reason}|{recorded_at}".encode("utf-8")).hexdigest()[:20]
        revision = {
            "revision_id": new_id,
            "decision_type": prior.get("decision_type", "unknown"),
            "status": "reopened",
            "record_refs": record_refs,
            "reviewer": reviewer,
            "recorded_at": recorded_at,
            "reason": reason,
            "evidence_refs": [],
            "previous_revision_id": prior_revision_id,
            "reopens_revision_id": prior_revision_id,
        }
        self.append(revision)
        return revision

    def reopen_durable(
        self,
        prior_revision_id: str,
        *,
        reason: str,
        actor: str,
        recorded_at: str,
        record_ids: list[str],
        evidence_refs: list[str] | None = None,
        authority_class: str = HUMAN_AUTHORITY,
        machine_result: MachineRuleResult | None = None,
        rule_id: str | None = None,
        rule_version: str | None = None,
        registry: RuleRegistry | None = None,
    ) -> dict[str, Any]:
        """Append a durable reopen revision while retaining the prior row.

        The new action owns its authority.  Human reopen is the safe default;
        a machine reopen must provide a fresh handler result rather than
        inheriting the prior rule/authority.
        """

        prior = next((item for item in self.iter_revisions() if item.get("revision_id") == prior_revision_id), None)
        if prior is None:
            raise ValueError(f"unknown prior revision: {prior_revision_id}")
        if authority_class == MACHINE_AUTHORITY:
            if machine_result is None:
                raise RuleContractError("machine reopen requires a new execute_machine_rule result")
            if rule_id is not None and rule_id != machine_result["rule_id"]:
                raise RuleContractError("machine reopen rule_id does not match handler result")
            if rule_version is not None and rule_version != machine_result["rule_version"]:
                raise RuleContractError("machine reopen rule_version does not match handler result")
            if machine_result["domain"] != prior["decision_domain"]:
                raise RuleContractError("machine reopen must remain in the prior decision domain")
            return self._append_machine_transition(
                prior,
                machine_result,
                status="reopened",
                reason=reason,
                actor=actor,
                recorded_at=recorded_at,
                record_ids=record_ids,
                evidence_refs=evidence_refs,
                reopens_revision_id=prior_revision_id,
                registry=registry,
            )
        if authority_class not in {HUMAN_AUTHORITY, UNKNOWN_AUTHORITY}:
            raise RuleContractError(f"unsupported new revision authority: {authority_class}")
        if machine_result is not None:
            raise RuleContractError("non-machine reopen cannot carry a machine result")
        if (rule_id is None) != (rule_version is None):
            raise RuleContractError("rule_id and rule_version must be both null or both strings")
        after = {"status": "reopened", "resolution": None}
        revision = {
            "schema_version": DURABLE_REVISION_SCHEMA_VERSION,
            "revision_id": durable_revision_id(
                prior["decision_domain"],
                prior["decision_type"],
                record_ids,
                rule_id,
                rule_version,
                actor,
                recorded_at,
                prior.get("after") or {},
                after,
            ),
            "decision_domain": prior["decision_domain"],
            "decision_type": prior["decision_type"],
            "status": "reopened",
            "rule_id": rule_id,
            "rule_version": rule_version,
            "actor": actor,
            "authority_class": authority_class,
            "recorded_at": recorded_at,
            "record_ids": record_ids,
            "before": prior.get("after") or {},
            "after": after,
            "reason": reason,
            "evidence_refs": evidence_refs or [],
            "previous_revision_id": prior_revision_id,
            "reopens_revision_id": prior_revision_id,
            "supersedes_revision_id": None,
        }
        self.append_durable(revision, registry=registry)
        return revision

    def supersede_durable(
        self,
        prior_revision_id: str,
        *,
        after: dict[str, Any] | None = None,
        reason: str,
        actor: str,
        recorded_at: str,
        record_ids: list[str],
        evidence_refs: list[str] | None = None,
        authority_class: str = HUMAN_AUTHORITY,
        machine_result: MachineRuleResult | None = None,
        rule_id: str | None = None,
        rule_version: str | None = None,
        registry: RuleRegistry | None = None,
    ) -> dict[str, Any]:
        """Append a replacement revision; the superseded row remains intact.

        A machine supersede must cite a new approved rule and carry its actual
        handler result.  Human/unknown supersedes intentionally use their own
        authority and do not inherit the prior machine provenance.
        """

        prior = next((item for item in self.iter_revisions() if item.get("revision_id") == prior_revision_id), None)
        if prior is None:
            raise ValueError(f"unknown prior revision: {prior_revision_id}")
        if authority_class == MACHINE_AUTHORITY:
            if machine_result is None:
                raise RuleContractError("machine supersede requires a new execute_machine_rule result")
            if rule_id is not None and rule_id != machine_result["rule_id"]:
                raise RuleContractError("machine supersede rule_id does not match handler result")
            if rule_version is not None and rule_version != machine_result["rule_version"]:
                raise RuleContractError("machine supersede rule_version does not match handler result")
            if machine_result["domain"] != prior["decision_domain"]:
                raise RuleContractError("machine supersede must remain in the prior decision domain")
            if after is not None and after != machine_result["resolution"]:
                raise RuleContractError("machine supersede after state must equal handler output")
            return self._append_machine_transition(
                prior,
                machine_result,
                status="superseded",
                reason=reason,
                actor=actor,
                recorded_at=recorded_at,
                record_ids=record_ids,
                evidence_refs=evidence_refs,
                supersedes_revision_id=prior_revision_id,
                registry=registry,
            )
        if authority_class not in {HUMAN_AUTHORITY, UNKNOWN_AUTHORITY}:
            raise RuleContractError(f"unsupported new revision authority: {authority_class}")
        if machine_result is not None:
            raise RuleContractError("non-machine supersede cannot carry a machine result")
        if after is None:
            raise RuleContractError("human/unknown supersede requires after state")
        if (rule_id is None) != (rule_version is None):
            raise RuleContractError("rule_id and rule_version must be both null or both strings")
        revision = {
            "schema_version": DURABLE_REVISION_SCHEMA_VERSION,
            "revision_id": durable_revision_id(
                prior["decision_domain"],
                prior["decision_type"],
                record_ids,
                rule_id,
                rule_version,
                actor,
                recorded_at,
                prior.get("after") or {},
                after,
            ),
            "decision_domain": prior["decision_domain"],
            "decision_type": prior["decision_type"],
            "status": "superseded",
            "rule_id": rule_id,
            "rule_version": rule_version,
            "actor": actor,
            "authority_class": authority_class,
            "recorded_at": recorded_at,
            "record_ids": record_ids,
            "before": prior.get("after") or {},
            "after": after,
            "reason": reason,
            "evidence_refs": evidence_refs or [],
            "previous_revision_id": prior_revision_id,
            "reopens_revision_id": None,
            "supersedes_revision_id": prior_revision_id,
        }
        self.append_durable(revision, registry=registry)
        return revision

    def _append_machine_transition(
        self,
        prior: Mapping[str, Any],
        machine_result: MachineRuleResult,
        *,
        status: str,
        reason: str,
        actor: str,
        recorded_at: str,
        record_ids: list[str],
        evidence_refs: list[str] | None,
        reopens_revision_id: str | None = None,
        supersedes_revision_id: str | None = None,
        registry: RuleRegistry | None = None,
    ) -> dict[str, Any]:
        """Append a new machine transition with fresh rule provenance."""

        return_revision = build_machine_revision(
            machine_result,
            decision_type=prior["decision_type"],
            record_ids=record_ids,
            actor=actor,
            recorded_at=recorded_at,
            before=prior.get("after") or {},
            reason=reason,
            evidence_refs=evidence_refs,
            status=status,
            previous_revision_id=prior["revision_id"],
            reopens_revision_id=reopens_revision_id,
            supersedes_revision_id=supersedes_revision_id,
            registry=registry,
        )
        self.append_durable(return_revision, registry=registry)
        return return_revision
