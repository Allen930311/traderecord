"""Operational Trading Record lifecycle.

The historical canonical records are immutable.  This module adds the small
append-only operational layer needed after a trade is finalized: lifecycle
events are durable evidence, while ``current.json`` is a deterministic
projection.  It deliberately composes :class:`CanonicalWriter` instead of
creating another canonical writer.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping

from .contracts import validate_record
from .evidence_store import EvidenceStore, EvidenceStoreError, evidence_binding, evidence_identity
from .evidence_time import EvidenceTimeError, normalize_evidence_time_metadata, represented_market_time_contains
from .identifiers import is_record_identifier, new_identifier
from .live_chart_plan import LiveChartPlanError, derive_live_chart_plan
from .historical_attribution import (
    REGISTRY_VERSION as RETROSPECTIVE_ATTRIBUTION_REGISTRY_VERSION,
    historical_attribution_registry_digest,
)
from .registry import load_registry
from .vocabularies import (
    EXECUTION_MODES,
    EVIDENCE_METHODS,
    HISTORICAL_UNRESOLVED_RULE_VERSION,
    HISTORICAL_UNRESOLVED_STRATEGY_ID,
    OUTCOME_KNOWN_AT_RECORDING_VALUES,
    PLAN_FIELD_UNKNOWN_VALUES,
    RETROSPECTIVE_REVIEW_ONLY_TAGS,
    RETROSPECTIVE_REVIEW_ONLY_TEXT_MARKERS,
    SOURCE_METHODS,
    STRATEGY_RESOLUTION_STATUSES,
    UNFROZEN_EXPERIMENTAL_PLACEHOLDER,
)
from .strategy_resolver import (
    OWNER_RETROSPECTIVE_AUTHORITY,
    RESOLVER_VERSION,
    validate_owner_attestation,
)
from .oslock import os_file_lock
from .writer import CanonicalWriter


LIFECYCLE_SCHEMA = "canonical-lifecycle-event.v2"
LEGACY_LIFECYCLE_SCHEMAS = {"canonical-lifecycle-event.v1", LIFECYCLE_SCHEMA}
PROJECTION_SCHEMA = "canonical-lifecycle-projection.v2"
CAPTURE_SCHEMA = "operational-capture.v1"
IDENTITY_LINK_SCHEMA = "operational-execution-identity-link.v1"
REVIEW_SCHEMA = "operational-trade-review.v2"
STRATEGY_RESOLUTION_SCHEMA = "operational-strategy-resolution.v1"
IDENTITY_DISPOSITIONS = {"MATCHED", "AMBIGUOUS", "UNMATCHED", "CONFLICT"}
RULE_COMPLIANCE_VALUES = {"compliant", "violation", "unknown", "not_observable"}
PROVIDER_EXECUTION_EVIDENCE_TYPES = {"broker_fill", "sheet_export", "provider_export", "provider_execution_artifact"}
OWNER_EXIT_EVIDENCE_CATEGORIES = {"exit", "review"}
EVIDENCE_CATEGORIES = {"pre_trade_context", "entry", "exit", "review"}
EXIT_AUTHORITY_CLASSES = {
    "deterministic_execution_calculation",
    "owner_attestation",
    "governed_manual_adjudication",
}
LIFECYCLE_STATES = (
    "PRE_TRADE_DRAFT",
    "ENTRY_PENDING_VERIFICATION",
    "OPEN",
    "PARTIALLY_CLOSED",
    "CLOSED",
    "REVIEW_PENDING",
    "REVIEWED",
)
EVENT_TYPES = {
    "draft_created",
    "evidence_attached",
    "evidence_locator_corrected",
    "entry_pending_verification",
    "identity_linked",
    "entry_verified",
    "backtest_entry_confirmed",
    "canonical_finalized",
    "partial_close",
    "final_close",
    "exit_verified",
    "review_started",
    "reviewed",
    "review_corrected",
    "strategy_attribution_resolved",
    "owner_statement_recorded",
    "chart_roles_recorded",
    "owner_words_unrecoverable_recorded",
    "plan_r_corrected",
    "backtest_timing_recorded",
    "backtest_rule_version_amended",
}


class LifecycleError(ValueError):
    """Raised when an operational lifecycle cannot fail-closed."""


def canonical_provider(provider: Any) -> str:
    """Provider IDs are compared case-insensitively; the canonical form is lowercase."""

    return provider.strip().lower() if isinstance(provider, str) else ""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for _ in range(200):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    raise TimeoutError(f"timed out replacing {path}")


def _merge(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def derive_rule_violation(review: Mapping[str, Any] | None) -> bool | None:
    """Derive the compatibility boolean from the canonical review enum."""

    if not isinstance(review, Mapping):
        return None
    compliance = review.get("rule_compliance")
    if compliance == "compliant":
        return False
    if compliance == "violation":
        return True
    return None


def _validate_strategy_resolution(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate strategy identity provenance without inventing an identity.

    ``strategy_id``/``rule_version`` remain nullable here.  Historical LIVE
    compatibility placeholders stay in ``record.strategy``; this object says
    whether an actual identity was established and why.
    """

    if not isinstance(value, Mapping):
        raise LifecycleError("strategy_resolution must be an object")
    status = value.get("status")
    if status not in STRATEGY_RESOLUTION_STATUSES:
        raise LifecycleError("strategy_resolution.status is unsupported")
    source = value.get("resolution_source")
    confidence = value.get("confidence")
    reason = value.get("reason")
    if not all(isinstance(item, str) and item.strip() for item in (source, confidence, reason)):
        raise LifecycleError("strategy_resolution requires source, confidence, and reason")
    if not isinstance(value.get("strategy_stats_eligible"), bool):
        raise LifecycleError("strategy_resolution.strategy_stats_eligible must be boolean")
    strategy_id = value.get("strategy_id")
    rule_version = value.get("rule_version")
    if status in {"unresolved", "no_match"}:
        if strategy_id is not None or rule_version is not None or value["strategy_stats_eligible"]:
            raise LifecycleError("unresolved strategy_resolution must have null identity and false stats eligibility")
    elif not isinstance(strategy_id, str) or not strategy_id.strip() or not isinstance(rule_version, str) or not rule_version.strip():
        raise LifecycleError("resolved strategy_resolution requires strategy_id and rule_version")
    rule_observations = value.get("rule_observations")
    if rule_observations is not None:
        if status not in {"contemporaneous_confirmed", "replay_reconstructed"}:
            raise LifecycleError("rule_observations require resolved strategy attribution")
        if (
            not isinstance(rule_observations, Mapping)
            or rule_observations.get("schema_version") not in {"resolved-rule-observations.v1", "resolved-rule-observations.v2"}
            or rule_observations.get("strategy_id") != strategy_id
            or rule_observations.get("rule_version") != rule_version
            or not isinstance(rule_observations.get("rules"), list)
        ):
            raise LifecycleError("rule_observations must pin the resolved strategy and frozen rule version")
        seen_rule_ids: set[str] = set()
        for item in rule_observations["rules"]:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("rule_id"), str)
                or item.get("state") not in {"PASS", "FAIL", "UNKNOWN"}
                or not isinstance(item.get("code"), str)
                or not isinstance(item.get("evidence_refs"), list)
                or any(not isinstance(ref, str) or not ref.strip() for ref in item["evidence_refs"])
            ):
                raise LifecycleError("rule_observations contains an invalid per-rule observation")
            if item["rule_id"] in seen_rule_ids:
                raise LifecycleError("rule_observations contains duplicate rule IDs")
            seen_rule_ids.add(item["rule_id"])
            if rule_observations.get("schema_version") == "resolved-rule-observations.v2":
                fact_timing = item.get("supports_fact_timing")
                basis = item.get("basis", [])
                if fact_timing is not None and fact_timing not in {"pre_trade", "execution", "post_trade"}:
                    raise LifecycleError("rule_observations supports_fact_timing is invalid")
                if not isinstance(basis, list) or any(
                    not isinstance(value, str)
                    or value not in {"contemporaneous_capture", "provider_execution", "governed_adjudication"}
                    for value in basis
                ):
                    raise LifecycleError("rule_observations basis is invalid")
                if "owner_attestation" in item or "retrospective_replay" in basis:
                    raise LifecycleError(
                        "retrospective rule facts must use the pinned projection from first-class Owner attribution"
                    )
    retrospective_facts = value.get("retrospective_rule_facts")
    if retrospective_facts is not None:
        if (
            status != "replay_reconstructed"
            or value.get("resolution_authority") != OWNER_RETROSPECTIVE_AUTHORITY
            or not isinstance(retrospective_facts, Mapping)
            or retrospective_facts.get("schema_version") != "retrospective-rule-facts.v1"
            or retrospective_facts.get("authority") != OWNER_RETROSPECTIVE_AUTHORITY
            or retrospective_facts.get("strategy_id") != strategy_id
            or retrospective_facts.get("rule_version") != rule_version
            or retrospective_facts.get("retrospective_attribution_registry_version") != RETROSPECTIVE_ATTRIBUTION_REGISTRY_VERSION
            or not isinstance(retrospective_facts.get("retrospective_attribution_registry_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", retrospective_facts["retrospective_attribution_registry_digest"])
            or not isinstance(retrospective_facts.get("rules"), list)
        ):
            raise LifecycleError("retrospective_rule_facts must pin the Owner attribution and frozen rule authority")
        seen_fact_rules: set[str] = set()
        for item in retrospective_facts["rules"]:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("rule_id"), str)
                or item.get("state") != "PASS"
                or item.get("supports_fact_timing") != "pre_trade"
                or item.get("basis") != ["owner_attestation", "retrospective_replay"]
                or not isinstance(item.get("evidence_refs"), list)
                or not item["evidence_refs"]
                or any(not isinstance(ref, str) or not ref.strip() for ref in item["evidence_refs"])
                or not isinstance(item.get("source_claim_ids"), list)
                or not item["source_claim_ids"]
                or any(not isinstance(claim, str) or not claim.strip() for claim in item["source_claim_ids"])
            ):
                raise LifecycleError("retrospective_rule_facts contains an invalid supported frozen-rule fact")
            if item["rule_id"] in seen_fact_rules:
                raise LifecycleError("retrospective_rule_facts contains duplicate rule IDs")
            seen_fact_rules.add(item["rule_id"])
    return {"schema_version": STRATEGY_RESOLUTION_SCHEMA, **deepcopy(dict(value))}


def normalize_strategy_resolution(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``value`` in the exact shape LifecycleStore persists (schema_version stamped)."""

    return _validate_strategy_resolution(value)


def _iso_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _validate_retrospective_plan(
    *,
    recording_timing: str,
    setup_tags: list[str],
    confirmation_tags: list[str],
    market_context: str,
    thesis: str,
) -> None:
    """Keep post-trade review vocabulary out of retrospective pre-trade plan.

    This is deliberately a small explicit vocabulary guard, not an attempt to
    infer hindsight from arbitrary prose. Facts that cannot be recovered
    remain valid only as ``unknown``/``not_observable``; review conclusions
    belong in ``review`` and are never silently copied into ``plan``.
    """

    if recording_timing != "retrospective":
        return
    for field_name, tags in (("setup_tags", setup_tags), ("confirmation_tags", confirmation_tags)):
        marker = next((tag for tag in tags if tag in RETROSPECTIVE_REVIEW_ONLY_TAGS), None)
        if marker is not None:
            raise LifecycleError(
                f"{field_name} contains post-trade review marker {marker!r}; "
                "keep it in review or use unknown/not_observable"
            )
    for field_name, value in (("market_context", market_context), ("thesis", thesis)):
        if isinstance(value, str) and value.strip().lower() in PLAN_FIELD_UNKNOWN_VALUES:
            continue
        normalized = value.casefold() if isinstance(value, str) else ""
        marker = next((item for item in RETROSPECTIVE_REVIEW_ONLY_TEXT_MARKERS if item in normalized), None)
        if marker is not None:
            raise LifecycleError(
                f"{field_name} contains post-trade review marker {marker!r}; "
                "keep it in review or use unknown/not_observable"
            )


def _weighted_exit_price(record: Mapping[str, Any], close: Mapping[str, Any]) -> Decimal:
    """The trade's whole-position exit price: partial legs by fraction plus the final leg's remainder.

    A provider-registered trade carries the provider's weighted exit as its single close and has
    no partial legs. A trade closed in stages records each stage with its quantity_fraction and the
    final leg closes whatever is left, so the realized R must be measured on the weighted price.
    """

    try:
        final = Decimal(str(close["exit_price"]))
        legs = [
            (Decimal(str(leg["quantity_fraction"])), Decimal(str(leg["exit_price"])))
            for leg in record.get("partial_closes") or []
        ]
    except (KeyError, TypeError, ArithmeticError) as exc:
        raise LifecycleError("a plan/R correction needs the trade's recorded exit price and partial-close legs") from exc
    fractions = sum((fraction for fraction, _ in legs), Decimal(0))
    if any(fraction <= 0 for fraction, _ in legs) or fractions >= 1:
        raise LifecycleError("partial-close fractions must be positive and leave a remainder for the final exit")
    weighted = sum((fraction * price for fraction, price in legs), Decimal(0)) + (1 - fractions) * final
    return weighted


def _derive_plan_correction(record: Mapping[str, Any], chart: Any) -> dict[str, Any]:
    """Recompute a trade's plan and R from chart prices and its actual fill and exit."""

    if not isinstance(chart, Mapping):
        raise LifecycleError("a plan/R correction requires the chart prices")
    entry = record.get("entry") if isinstance(record.get("entry"), Mapping) else {}
    close = record.get("close") if isinstance(record.get("close"), Mapping) else {}
    saved_images = {
        ref
        for item in record.get("evidence", [])
        if isinstance(item, Mapping) and str(item.get("media_type", "")).startswith("image/")
        for ref in (item.get("source_ref"), item.get("locator"))
        if isinstance(ref, str) and ref
    }
    try:
        return derive_live_chart_plan(
            chart,
            direction=str(record.get("direction")),
            provider_entry_price=entry.get("actual_entry"),
            provider_exit_price=_weighted_exit_price(record, close),
            attached_refs=saved_images,
        )
    except LiveChartPlanError as exc:
        raise LifecycleError(f"plan/R correction is invalid: {exc}") from exc


def _require_agent_attested_confirmation(payload: Mapping[str, Any], what: str) -> None:
    """The Agent attests the Owner's same-conversation confirmation; it is not platform-verified."""

    if (
        payload.get("authority") != "agent_attested_owner_confirmation"
        or payload.get("owner_identity") != "example-owner"
        or payload.get("approval_source") != "same_conversation_agent_attestation"
    ):
        raise LifecycleError(f"{what} must carry the Agent's attestation of the Owner's same-conversation confirmation")
    confirmed_at = payload.get("confirmed_at")
    try:
        if isinstance(confirmed_at, str):
            datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
    except ValueError:
        confirmed_at = None
    if not isinstance(confirmed_at, str) or not confirmed_at:
        raise LifecycleError(f"{what} require the ISO confirmed_at time of the Owner's confirmation")


class LifecycleStore:
    """Append-only lifecycle event store and deterministic current projection."""

    def __init__(self, root: Path, writer: CanonicalWriter | None = None) -> None:
        self.root = root.resolve()
        self.writer = writer or CanonicalWriter(self.root)
        self.directory = self.root / "canonical" / "lifecycle"
        self.events_path = self.directory / "events.jsonl"
        self.identity_links_path = self.directory / "identity-links.jsonl"
        self.projection_path = self.directory / "current.json"
        self.lock_path = self.events_path.with_suffix(".lock")

    @contextmanager
    def _locked(self):
        with os_file_lock(self.lock_path, "timed out waiting for lifecycle event lock"):
            yield

    def _read_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line_number, line in enumerate(self.events_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LifecycleError(f"invalid lifecycle event at line {line_number}") from exc
            if not isinstance(event, dict) or event.get("schema_version") not in LEGACY_LIFECYCLE_SCHEMAS:
                raise LifecycleError(f"invalid lifecycle schema at line {line_number}")
            for key in ("event_id", "idempotency_key", "event_type", "occurred_at", "payload"):
                if key not in event:
                    raise LifecycleError(f"lifecycle event missing {key} at line {line_number}")
            if event["event_type"] not in EVENT_TYPES or not isinstance(event["payload"], dict):
                raise LifecycleError(f"invalid lifecycle event type at line {line_number}")
            if not _iso_timestamp(event.get("occurred_at")):
                raise LifecycleError(f"invalid lifecycle occurred_at at line {line_number}")
            # v1 events predate the explicit fact/recorded-time contract.  Keep
            # them readable without rewriting their immutable source; new
            # events always persist recorded_at explicitly.
            event.setdefault("recorded_at", event["occurred_at"])
            if not _iso_timestamp(event.get("recorded_at")):
                raise LifecycleError(f"invalid lifecycle recorded_at at line {line_number}")
            events.append(event)
        return events

    def _read_identity_links(self) -> list[dict[str, Any]]:
        if not self.identity_links_path.exists():
            return []
        links: list[dict[str, Any]] = []
        for line_number, line in enumerate(self.identity_links_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                link = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LifecycleError(f"invalid identity link at line {line_number}") from exc
            if not isinstance(link, dict) or link.get("schema_version") != IDENTITY_LINK_SCHEMA:
                raise LifecycleError(f"invalid identity link schema at line {line_number}")
            required = (
                "identity_link_id", "draft_id", "provider", "provider_lifecycle_id",
                "instrument", "direction", "provider_execution_timestamp",
                "candidate_source", "match_disposition", "evidence_refs",
                "created_at", "authority", "digest", "idempotency_key",
            )
            if any(key not in link for key in required):
                raise LifecycleError(f"identity link missing required field at line {line_number}")
            if link.get("match_disposition") not in IDENTITY_DISPOSITIONS:
                raise LifecycleError(f"invalid identity link disposition at line {line_number}")
            if not isinstance(link.get("evidence_refs"), list) or any(not isinstance(item, str) or not item for item in link["evidence_refs"]):
                raise LifecycleError(f"invalid identity link evidence refs at line {line_number}")
            link.setdefault("provider_execution_at", link.get("provider_execution_timestamp"))
            link.setdefault("decision_created_at", link.get("created_at"))
            if not _iso_timestamp(link.get("provider_execution_at")) or not _iso_timestamp(link.get("decision_created_at")):
                raise LifecycleError(f"invalid identity link time fields at line {line_number}")
            if link.get("recorded_at") is not None and not _iso_timestamp(link.get("recorded_at")):
                raise LifecycleError(f"invalid identity link recorded_at at line {line_number}")
            links.append(link)
        return links

    def resolve_draft_id(self, ref: str) -> str:
        """Resolve a draft_id or trade_id to its governing draft_id.

        Evidence stays attached to the draft record for the trade's whole
        life (pre-trade, entry, exit, and review evidence all land there),
        so callers holding only a trade_id still need a way back to it.
        """

        record = self._projected_record(ref)
        draft_id = record.get("draft_id") if isinstance(record, Mapping) else None
        if not isinstance(draft_id, str) or not draft_id:
            raise LifecycleError(f"no draft is known for reference: {ref}")
        return draft_id

    def _draft(self, draft_id: str) -> dict[str, Any]:
        path = self.writer.drafts / f"{draft_id}.json"
        if not path.exists():
            raise LifecycleError(f"draft not found: {draft_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LifecycleError(f"draft is not readable: {draft_id}") from exc
        if not isinstance(payload, dict):
            raise LifecycleError(f"draft is not an object: {draft_id}")
        return payload

    def _attached_evidence(self, draft_id: str) -> list[dict[str, Any]]:
        projected = self._projected_record(draft_id)
        evidence = projected.get("evidence") if isinstance(projected, Mapping) else None
        if not isinstance(evidence, list):
            evidence = self._draft(draft_id).get("evidence", [])
        if not isinstance(evidence, list):
            raise LifecycleError("draft evidence must be a list")
        return [dict(item) for item in evidence if isinstance(item, Mapping)]

    def _resolve_evidence(
        self,
        draft_id: str,
        refs: list[str],
        *,
        categories: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            raise LifecycleError("evidence refs must be a non-empty list of strings")
        attached = self._attached_evidence(draft_id)
        resolved: list[dict[str, Any]] = []
        for ref in refs:
            matches = [item for item in attached if item.get("source_ref") == ref]
            if categories is not None:
                matches = [item for item in matches if item.get("category") in categories]
            if not matches:
                raise LifecycleError(f"evidence ref is not attached to draft: {ref}")
            resolved.append(matches[0])
        return resolved

    @staticmethod
    def _is_provider_evidence(evidence: Mapping[str, Any]) -> bool:
        return evidence.get("type") in PROVIDER_EXECUTION_EVIDENCE_TYPES or evidence.get("method") in PROVIDER_EXECUTION_EVIDENCE_TYPES or evidence.get("source_class") in {"provider", "broker", "provider_execution"}

    def _event_by_idempotency(self, idempotency_key: str, event_type: str | None = None) -> dict[str, Any] | None:
        for event in self._read_events():
            if event.get("idempotency_key") == idempotency_key and (event_type is None or event.get("event_type") == event_type):
                return event
        return None

    def _projected_record(self, ref: str) -> Mapping[str, Any] | None:
        projection = self.current()
        record = projection.get("records", {}).get(ref)
        if isinstance(record, Mapping):
            return record
        for item in projection.get("records", {}).values():
            if isinstance(item, Mapping) and ref in {item.get("draft_id"), item.get("trade_id"), item.get("trade_no")}:
                return item
        return None

    def _require_state(self, ref: str, allowed: set[str], message: str) -> Mapping[str, Any]:
        record = self._projected_record(ref)
        if not isinstance(record, Mapping) or record.get("state") not in allowed:
            raise LifecycleError(message)
        return record

    def _validate_transition(self, event_type: str, payload: Mapping[str, Any], projection: Mapping[str, Any]) -> None:
        ref = self._ref(payload)
        record = projection.get("records", {}).get(ref)
        if not isinstance(record, Mapping):
            for item in projection.get("records", {}).values():
                if isinstance(item, Mapping) and ref in {item.get("draft_id"), item.get("trade_id")}:
                    record = item
                    break
        state = record.get("state") if isinstance(record, Mapping) else None
        allowed = {
            "draft_created": {None},
            "evidence_attached": set(LIFECYCLE_STATES),
            "evidence_locator_corrected": set(LIFECYCLE_STATES),
            "entry_pending_verification": {"PRE_TRADE_DRAFT"},
            "identity_linked": {"ENTRY_PENDING_VERIFICATION"},
            "entry_verified": {"ENTRY_PENDING_VERIFICATION"},
            "backtest_entry_confirmed": {"PRE_TRADE_DRAFT"},
            "canonical_finalized": {"OPEN"} | ({None} if payload.get("legacy_compatibility") is True else set()),
            "partial_close": {"OPEN", "PARTIALLY_CLOSED"},
            "final_close": {"OPEN", "PARTIALLY_CLOSED"},
            "exit_verified": {"CLOSED"},
            "review_started": {"CLOSED"},
            "reviewed": {"REVIEW_PENDING"},
            "review_corrected": {"REVIEWED"},
            "strategy_attribution_resolved": {"OPEN", "PARTIALLY_CLOSED", "CLOSED", "REVIEW_PENDING", "REVIEWED"},
            "owner_statement_recorded": set(LIFECYCLE_STATES),
            "chart_roles_recorded": set(LIFECYCLE_STATES),
            "owner_words_unrecoverable_recorded": set(LIFECYCLE_STATES),
            "plan_r_corrected": {"CLOSED", "REVIEW_PENDING", "REVIEWED"},
            "backtest_timing_recorded": set(LIFECYCLE_STATES),
            "backtest_rule_version_amended": set(LIFECYCLE_STATES),
        }
        if state not in allowed[event_type]:
            raise LifecycleError(f"illegal lifecycle transition: {state or 'MISSING'} -> {event_type}")
        if event_type == "evidence_attached":
            evidence = payload.get("evidence")
            if not isinstance(evidence, Mapping):
                raise LifecycleError("evidence_attached event requires an evidence object")
            locator = evidence.get("locator")
            if not isinstance(locator, str) or not locator.strip() or locator == "artifact:full":
                raise LifecycleError("new evidence requires a non-legacy locator; use EvidenceStore for binary artifacts")
            try:
                normalize_evidence_time_metadata(evidence)
            except EvidenceTimeError as exc:
                raise LifecycleError(f"attached evidence timing metadata is invalid: {exc}") from exc
        if event_type == "evidence_locator_corrected":
            required = ("source_ref", "category", "sha256", "prior_locator", "locator", "reason")
            if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in required):
                raise LifecycleError("evidence locator correction requires exact source, category, hash, locators, and reason")
            sha256 = payload["sha256"]
            if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise LifecycleError("evidence locator correction sha256 must be lower-case hexadecimal")
            if payload["locator"] == "artifact:full" or payload["locator"] == payload["prior_locator"]:
                raise LifecycleError("evidence locator correction requires a distinct durable locator")
            if payload.get("byte_size") is not None and (not isinstance(payload["byte_size"], int) or payload["byte_size"] <= 0):
                raise LifecycleError("evidence locator correction byte_size must be a positive integer")
            current_evidence = record.get("evidence", []) if isinstance(record, Mapping) else []
            matches = [
                item for item in current_evidence
                if isinstance(item, Mapping)
                and item.get("source_ref") == payload["source_ref"]
                and item.get("category") == payload["category"]
                and item.get("sha256") == sha256
                and item.get("locator") == payload["prior_locator"]
            ]
            if len(matches) != 1:
                raise LifecycleError("evidence locator correction must match exactly one current attached item")
            try:
                EvidenceStore(self.root).read(payload["locator"], expected_sha256=sha256)
            except EvidenceStoreError as exc:
                raise LifecycleError(f"corrected evidence artifact is not verified: {exc}") from exc
            metadata = EvidenceStore(self.root).metadata(sha256)
            if not isinstance(metadata, Mapping) or metadata.get("locator") != payload["locator"]:
                raise LifecycleError("corrected evidence locator has no matching EvidenceStore metadata")
            for key in ("media_type", "byte_size", "original_filename"):
                if key in payload and payload.get(key) != metadata.get(key):
                    raise LifecycleError(f"corrected evidence metadata mismatch: {key}")
        if event_type == "owner_statement_recorded":
            statement = payload.get("verbatim_text")
            source_id = payload.get("source_id")
            if not isinstance(record, Mapping) or record.get("trade_id") != payload.get("trade_id"):
                raise LifecycleError("Owner statement supplement requires an existing canonical trade")
            if not isinstance(statement, str) or not statement.strip():
                raise LifecycleError("Owner statement must preserve non-empty verbatim text")
            if len(statement) > 20000:
                raise LifecycleError("Owner statement exceeds the 20000 character preservation limit")
            if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", source_id):
                raise LifecycleError("Owner statement source_id must be a stable safe identifier")
            expected_sha256 = hashlib.sha256(statement.encode("utf-8")).hexdigest()
            if payload.get("sha256") != expected_sha256:
                raise LifecycleError("Owner statement SHA256 does not match the exact UTF-8 text")
            if payload.get("authority") != "owner_supplied_verbatim":
                raise LifecycleError("Owner statement authority is invalid")
        if event_type == "backtest_timing_recorded":
            if not isinstance(record, Mapping) or record.get("trade_id") != payload.get("trade_id"):
                raise LifecycleError("backtest timing requires an existing canonical trade")
            if record.get("trade_type") != "backtest":
                raise LifecycleError("backtest timing applies only to BACKTEST trades")
            if not isinstance(payload.get("source_id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", payload["source_id"]):
                raise LifecycleError("backtest timing source_id must be a stable safe identifier")
            _require_agent_attested_confirmation(payload, "backtest timing")
            times = []
            for name in ("entry_at", "exit_at"):
                value = payload.get(name)
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
                except ValueError:
                    parsed = None
                if parsed is None or parsed.tzinfo is None:
                    raise LifecycleError(f"backtest timing {name} must be an ISO timestamp with a UTC offset")
                times.append(parsed)
            if times[1] < times[0]:
                raise LifecycleError("backtest timing exit_at must not precede entry_at")
        if event_type == "backtest_rule_version_amended":
            # Owner decision 2026-11-22 (BT000001 -> stg-example-a:v2). A BACKTEST pins its rule version at
            # registration; this is the only append-only way to re-pin it, and only to a version the
            # registry already lists for the same strategy.
            if not isinstance(record, Mapping) or record.get("trade_id") != payload.get("trade_id"):
                raise LifecycleError("backtest rule version amendment requires an existing canonical trade")
            if record.get("trade_type") != "backtest":
                raise LifecycleError("backtest rule version amendment applies only to BACKTEST trades")
            if not isinstance(payload.get("source_id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", payload["source_id"]):
                raise LifecycleError("backtest rule version amendment source_id must be a stable safe identifier")
            if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
                raise LifecycleError("backtest rule version amendment requires a reason")
            _require_agent_attested_confirmation(payload, "backtest rule version amendment")
            strategy_id = record.get("strategy_id")
            new_version = payload.get("rule_version")
            entry = next((x for x in load_registry(self.root).get("strategies", []) if x.get("strategy_id") == strategy_id), None)
            if entry is None or new_version not in (entry.get("rule_versions") or []):
                raise LifecycleError("rule_version must be registered for the trade's own strategy")
        if event_type == "chart_roles_recorded":
            if not isinstance(record, Mapping) or record.get("trade_id") != payload.get("trade_id"):
                raise LifecycleError("chart roles require an existing canonical trade")
            if not isinstance(payload.get("source_id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", payload["source_id"]):
                raise LifecycleError("chart roles source_id must be a stable safe identifier")
            _require_agent_attested_confirmation(payload, "chart roles")
            roles = payload.get("chart_roles")
            # A complete snapshot: a role left out is withdrawn, so an empty mapping is valid.
            if not isinstance(roles, Mapping) or set(roles) - {"entry_chart", "exit_chart"}:
                raise LifecycleError("chart_roles may only declare entry_chart and/or exit_chart")
            saved_images = {
                ref
                for item in record.get("evidence", [])
                if isinstance(item, Mapping) and str(item.get("media_type", "")).startswith("image/")
                for ref in (item.get("source_ref"), item.get("locator"))
                if isinstance(ref, str) and ref
            }
            for role, declared in roles.items():
                if not isinstance(declared, Mapping) or set(declared) != {"evidence_refs", "rationale"}:
                    raise LifecycleError(f"{role} must have exactly evidence_refs and rationale")
                refs = declared["evidence_refs"]
                rationale = declared["rationale"]
                if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in saved_images for ref in refs):
                    raise LifecycleError(f"{role} must cite images saved on this trade")
                if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 2000:
                    raise LifecycleError(f"{role} requires a non-empty rationale of at most 2000 characters")
        if event_type == "owner_words_unrecoverable_recorded":
            if not isinstance(record, Mapping) or record.get("trade_id") != payload.get("trade_id"):
                raise LifecycleError("an unrecoverable-words declaration requires an existing canonical trade")
            if not isinstance(payload.get("source_id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", payload["source_id"]):
                raise LifecycleError("unrecoverable-words source_id must be a stable safe identifier")
            _require_agent_attested_confirmation(payload, "unrecoverable-words declaration")
            reason = payload.get("reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise LifecycleError("an unrecoverable-words declaration requires a non-empty reason of at most 2000 characters")
            if any(
                isinstance(item, Mapping)
                and item.get("authority") == "owner_supplied_verbatim"
                and str(item.get("verbatim_text", "")).strip()
                for item in record.get("owner_statements") or []
            ):
                raise LifecycleError("this trade already has the Owner's verbatim words; they are not unrecoverable")
        if event_type == "plan_r_corrected":
            if not isinstance(record, Mapping) or record.get("trade_id") != payload.get("trade_id"):
                raise LifecycleError("a plan/R correction requires an existing canonical trade")
            if record.get("trade_type") != "live":
                raise LifecycleError("a plan/R correction applies to a registered LIVE trade only")
            if (record.get("exit_verification") or {}).get("state") != "confirmed":
                raise LifecycleError("a plan/R correction needs a closed trade with a confirmed exit")
            if not isinstance(payload.get("source_id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", payload["source_id"]):
                raise LifecycleError("plan/R correction source_id must be a stable safe identifier")
            _require_agent_attested_confirmation(payload, "plan/R correction")
            # The stored plan must be exactly what the chart prices produce for this
            # trade's actual fill and exit; the Agent cannot assert R directly.
            expected = _derive_plan_correction(record, payload.get("chart"))
            if payload.get("plan") != expected:
                raise LifecycleError("plan/R correction does not match the chart prices and the trade's actual fill")
        if event_type == "identity_linked" and payload.get("match_disposition") != "MATCHED":
            # A non-MATCHED link is durable evidence, but it cannot advance
            # the lifecycle and therefore never emits an identity_linked event.
            raise LifecycleError("only MATCHED identity links can enter the lifecycle")
        if event_type == "strategy_attribution_resolved":
            if not isinstance(record, Mapping) or record.get("trade_type") != "live":
                raise LifecycleError("strategy attribution amendments require a registered LIVE trade")
            current_resolution = record.get("strategy_resolution")
            if not isinstance(current_resolution, Mapping) or current_resolution.get("status") != "unresolved":
                raise LifecycleError("strategy attribution amendments require unresolved attribution")
            if payload.get("prior_strategy_resolution") != dict(current_resolution):
                raise LifecycleError("strategy attribution amendment prior state is stale")
            try:
                resolution = _validate_strategy_resolution(payload.get("strategy_resolution"))
            except LifecycleError as exc:
                raise LifecycleError("strategy attribution amendment requires a valid resolver resolution") from exc
            if resolution.get("resolution_source") != RESOLVER_VERSION or resolution.get("status") not in {"no_match", "contemporaneous_confirmed", "replay_reconstructed"}:
                raise LifecycleError("strategy attribution amendment must resolve to NO_MATCH or a registered strategy")
            if payload.get("strategy_id") != resolution.get("strategy_id") or payload.get("rule_version") != resolution.get("rule_version"):
                raise LifecycleError("strategy attribution amendment identity does not match its resolver resolution")
            if resolution.get("status") == "no_match":
                if payload.get("strategy_id") is not None or payload.get("rule_version") is not None:
                    raise LifecycleError("NO_MATCH amendment must retain null strategy identity")
            else:
                strategy_id = payload.get("strategy_id")
                rule_version = payload.get("rule_version")
                strategy = next((item for item in load_registry(self.root).get("strategies", []) if item.get("strategy_id") == strategy_id), None)
                if not strategy or strategy.get("status") != "active" or rule_version not in (strategy.get("rule_versions") or []):
                    raise LifecycleError("strategy attribution amendment must use an active registered strategy and rule version")
            if resolution.get("resolution_authority") == OWNER_RETROSPECTIVE_AUTHORITY:
                owner = resolution.get("owner_attestation")
                try:
                    normalized_owner = validate_owner_attestation(self.root, owner)
                except ValueError as exc:
                    raise LifecycleError(f"strategy attribution amendment has invalid Owner attestation: {exc}") from exc
                amendment = payload.get("amendment")
                if not isinstance(amendment, Mapping) or amendment.get("owner_attestation") != normalized_owner:
                    raise LifecycleError("strategy attribution amendment must preserve its Owner attestation")
                authority = amendment.get("authority")
                if (
                    not isinstance(authority, Mapping)
                    or authority.get("strategy_id") != payload.get("strategy_id")
                    or authority.get("rule_version") != payload.get("rule_version")
                    or authority.get("retrospective_attribution_registry_version") != RETROSPECTIVE_ATTRIBUTION_REGISTRY_VERSION
                    or not isinstance(authority.get("retrospective_attribution_registry_digest"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", authority["retrospective_attribution_registry_digest"])
                ):
                    raise LifecycleError("Owner-retrospective strategy attribution amendment requires a pinned authority snapshot")
                retrospective_facts = resolution.get("retrospective_rule_facts")
                if isinstance(retrospective_facts, Mapping) and any(
                    retrospective_facts.get(key) != authority.get(key)
                    for key in (
                        "strategy_id",
                        "rule_version",
                        "retrospective_attribution_registry_version",
                        "retrospective_attribution_registry_digest",
                    )
                ):
                    raise LifecycleError("retrospective rule-fact projection must match the immutable amendment authority pin")
                if isinstance(retrospective_facts, Mapping):
                    retrospective_items = amendment.get("retrospective_observations")
                    if not isinstance(retrospective_items, list):
                        raise LifecycleError("retrospective rule-fact projection requires preserved retrospective observations")
                    for fact in retrospective_facts.get("rules", []):
                        source_claims = set(fact.get("source_claim_ids", []))
                        supporting = [
                            item for item in retrospective_items
                            if isinstance(item, Mapping)
                            and item.get("claim_id") in source_claims
                            and item.get("finding") == "corroborates"
                            and item.get("timing") == "post_trade"
                        ]
                        exact_refs = {
                            ref for item in supporting for ref in item.get("evidence_refs", [])
                            if isinstance(ref, str)
                        }
                        if not source_claims or {item.get("claim_id") for item in supporting} != source_claims or exact_refs != set(fact.get("evidence_refs", [])):
                            raise LifecycleError("retrospective rule-fact projection is not exactly bound to corroborating observations")
                if normalized_owner.get("setup_status") == "HAS_SETUP":
                    if payload.get("strategy_id") != normalized_owner.get("primary_strategy_id"):
                        raise LifecycleError("Owner primary strategy must match the resolved strategy identity")
                    if (
                        resolution.get("entry_expression") != normalized_owner.get("entry_expression")
                        or resolution.get("entry_triggers") != normalized_owner.get("entry_triggers")
                        or resolution.get("confluences") != normalized_owner.get("confluences")
                        or resolution.get("confluence_types") != normalized_owner.get("confluence_types")
                    ):
                        raise LifecycleError("Owner entry expression and confluences must remain distinct attribution dimensions")
                    if resolution.get("status") != "replay_reconstructed":
                        raise LifecycleError("retrospective Owner attribution must remain replay_reconstructed")
            refs = payload.get("evidence_refs")
            owner_attested = (
                resolution.get("resolution_authority") == OWNER_RETROSPECTIVE_AUTHORITY
                and isinstance(resolution.get("owner_attestation"), Mapping)
            )
            if not isinstance(refs, list) or (not refs and not owner_attested) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
                raise LifecycleError("strategy attribution amendment requires evidence_refs")
            if not isinstance(payload.get("resolver_input_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", payload["resolver_input_sha256"]):
                raise LifecycleError("strategy attribution amendment requires resolver_input_sha256")

    def _validate_recorded_order(self, recorded_at: str, events: list[Mapping[str, Any]]) -> None:
        """Protect durable append order without conflating it with fact time."""

        try:
            current_time = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise LifecycleError("recorded_at must be an ISO timestamp") from exc
        previous: list[datetime] = []
        for event in events:
            value = event.get("recorded_at") or event.get("occurred_at")
            if not isinstance(value, str):
                continue
            try:
                event_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if event_time.tzinfo is None:
                    event_time = event_time.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            previous.append(event_time)
        if previous and current_time < max(previous):
            raise LifecycleError("recorded_at must not move backwards in durable append order")

    @staticmethod
    def _ref(payload: Mapping[str, Any]) -> str:
        draft_id = payload.get("draft_id")
        trade_id = payload.get("trade_id")
        ref = draft_id or trade_id
        if not isinstance(ref, str) or not ref:
            raise LifecycleError("lifecycle payload requires draft_id or trade_id")
        return ref

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        occurred_at: str | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            raise LifecycleError(f"unsupported lifecycle event: {event_type}")
        if not idempotency_key or "\x00" in idempotency_key:
            raise LifecycleError("idempotency_key is required")
        if occurred_at is not None and not _iso_timestamp(occurred_at):
            raise LifecycleError("occurred_at must be an ISO timestamp")
        if recorded_at is not None and not _iso_timestamp(recorded_at):
            raise LifecycleError("recorded_at must be an ISO timestamp")
        self._ref(payload)
        payload_dict = dict(payload)
        event_id = "lev_" + _digest(
            {"event_type": event_type, "payload": payload_dict, "idempotency_key": idempotency_key}
        )[:24]
        with self._locked():
            events = self._read_events()
            for existing in events:
                if existing.get("idempotency_key") == idempotency_key:
                    if (
                        existing.get("event_type") != event_type
                        or existing.get("payload") != payload_dict
                    ):
                        raise LifecycleError("idempotency key reused with a different event")
                    return existing
                if existing.get("event_id") == event_id:
                    return existing
            event_time = occurred_at or _now()
            append_time = recorded_at or _now()
            self._validate_recorded_order(append_time, events)
            self._validate_transition(event_type, payload_dict, self.project())
            event = {
                "schema_version": LIFECYCLE_SCHEMA,
                "event_id": event_id,
                "idempotency_key": idempotency_key,
                "event_type": event_type,
                "occurred_at": event_time,
                "recorded_at": append_time,
                "payload": payload_dict,
            }
            with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        self.write_projection()
        return event

    def project(self) -> dict[str, Any]:
        # JSONL order is the durable append order.  ``occurred_at`` remains
        # the domain fact time and is never used to replay state transitions.
        events = self._read_events()
        aliases: dict[str, str] = {}
        for event in events:
            payload = event["payload"]
            draft_id = payload.get("draft_id")
            trade_id = payload.get("trade_id")
            if isinstance(draft_id, str) and isinstance(trade_id, str):
                canonical = aliases.get(trade_id, trade_id)
                aliases[draft_id] = canonical
                aliases[trade_id] = canonical
        records: dict[str, dict[str, Any]] = {}
        for event in events:
            payload = event["payload"]
            ref = aliases.get(self._ref(payload), self._ref(payload))
            state = records.setdefault(
                ref,
                {
                    "draft_id": payload.get("draft_id"),
                    "trade_id": payload.get("trade_id"),
                    "state": None,
                    "identity_status": "not_started",
                    "review_status": "not_started",
                    "evidence": [],
                    "owner_statements": [],
                    "chart_roles": {},
                    "chart_roles_events": [],
                    "owner_words_unrecoverable": None,
                    "owner_words_unrecoverable_events": [],
                    "plan_correction": None,
                    "plan_correction_events": [],
                    "plan_corrected_after_review": False,
                    "partial_closes": [],
                    "lifecycle_event_ids": [],
                    "provider": {},
                    "review": {},
                },
            )
            state["lifecycle_event_ids"].append(event["event_id"])
            if payload.get("trade_id"):
                state["trade_id"] = payload["trade_id"]
            if payload.get("draft_id"):
                state["draft_id"] = payload["draft_id"]
            et = event["event_type"]
            if et == "draft_created":
                state.update(
                    {
                        "state": "PRE_TRADE_DRAFT",
                        "strategy_id": payload.get("strategy_id"),
                        "rule_version": payload.get("rule_version"),
                        **({"strategy_alias_status": payload.get("strategy_alias_status")} if payload.get("strategy_alias_status") else {}),
                        "symbol": payload.get("symbol"),
                        "direction": payload.get("direction"),
                        "trade_type": payload.get("trade_type"),
                        "created_at": payload.get("created_at"),
                        "planned": deepcopy(payload.get("planned", {})),
                        "source_method": payload.get("source_method"),
                        "outcome_known_at_recording": payload.get("outcome_known_at_recording", "unknown"),
                        **({"strategy_resolution": deepcopy(payload["strategy_resolution"])} if isinstance(payload.get("strategy_resolution"), Mapping) else {}),
                        **({"registration_packet_sha256": payload.get("registration_packet_sha256")} if payload.get("registration_packet_sha256") else {}),
                    }
                )
            elif et == "evidence_attached":
                evidence = deepcopy(payload.get("evidence", {}))
                key = (evidence.get("sha256"), evidence.get("source_ref"), evidence.get("category"))
                if not any((item.get("sha256"), item.get("source_ref"), item.get("category")) == key for item in state["evidence"]):
                    state["evidence"].append(evidence)
            elif et == "evidence_locator_corrected":
                matches = [
                    item for item in state["evidence"]
                    if item.get("source_ref") == payload.get("source_ref")
                    and item.get("category") == payload.get("category")
                    and item.get("sha256") == payload.get("sha256")
                    and item.get("locator") == payload.get("prior_locator")
                ]
                if len(matches) != 1:
                    raise LifecycleError("evidence locator correction no longer matches its source event")
                evidence = matches[0]
                evidence["locator"] = payload["locator"]
                for key in ("media_type", "byte_size", "original_filename"):
                    if key in payload:
                        evidence[key] = payload[key]
                evidence["locator_corrected_by_event_id"] = event["event_id"]
            elif et == "owner_statement_recorded":
                state["owner_statements"].append(
                    {
                        "source_id": payload["source_id"],
                        "verbatim_text": payload["verbatim_text"],
                        "sha256": payload["sha256"],
                        "authority": payload["authority"],
                        "event_id": event["event_id"],
                        "occurred_at": event["occurred_at"],
                        "recorded_at": event["recorded_at"],
                    }
                )
            elif et == "chart_roles_recorded":
                # Append-only history; each event is a complete snapshot and the latest is current.
                state["chart_roles_events"].append(
                    {
                        "source_id": payload["source_id"],
                        "chart_roles": payload["chart_roles"],
                        "authority": payload["authority"],
                        "owner_identity": payload["owner_identity"],
                        "approval_source": payload["approval_source"],
                        "confirmed_at": payload["confirmed_at"],
                        "event_id": event["event_id"],
                        "recorded_at": event["recorded_at"],
                    }
                )
                state["chart_roles"] = dict(payload["chart_roles"])
            elif et == "owner_words_unrecoverable_recorded":
                # Append-only history; the latest declaration is current. A trade that
                # later receives the Owner's verbatim words simply stops needing it.
                declaration = {
                    "source_id": payload["source_id"],
                    "reason": payload["reason"],
                    "authority": payload["authority"],
                    "owner_identity": payload["owner_identity"],
                    "approval_source": payload["approval_source"],
                    "confirmed_at": payload["confirmed_at"],
                    "event_id": event["event_id"],
                    "recorded_at": event["recorded_at"],
                }
                state["owner_words_unrecoverable_events"].append(declaration)
                state["owner_words_unrecoverable"] = declaration
            elif et == "backtest_timing_recorded":
                # Append-only history; the latest timing is current. These are Owner-stated
                # replay times, never provider execution facts, so they stay out of entry/close.
                timing = {
                    "source_id": payload["source_id"],
                    "entry_at": payload["entry_at"],
                    "exit_at": payload["exit_at"],
                    "authority": payload["authority"],
                    "owner_identity": payload["owner_identity"],
                    "approval_source": payload["approval_source"],
                    "confirmed_at": payload["confirmed_at"],
                    "event_id": event["event_id"],
                    "recorded_at": event["recorded_at"],
                }
                state.setdefault("backtest_timing_events", []).append(timing)
                state["backtest_timing"] = timing
            elif et == "backtest_rule_version_amended":
                state.setdefault("rule_version_amendments", []).append({
                    "from_rule_version": state.get("rule_version"), "to_rule_version": payload["rule_version"],
                    "source_id": payload["source_id"], "reason": payload["reason"],
                    "confirmed_at": payload["confirmed_at"], "event_id": event["event_id"],
                })
                state["rule_version"] = payload["rule_version"]
            elif et == "plan_r_corrected":
                # Append-only: the latest correction is current and replaces the plan
                # levels and realized R used downstream; earlier values stay in history.
                correction = {
                    "source_id": payload["source_id"],
                    "chart": deepcopy(payload["chart"]),
                    "plan": deepcopy(payload["plan"]),
                    "prior_realized_r": state.get("realized_r"),
                    "authority": payload["authority"],
                    "owner_identity": payload["owner_identity"],
                    "approval_source": payload["approval_source"],
                    "confirmed_at": payload["confirmed_at"],
                    "event_id": event["event_id"],
                    "recorded_at": event["recorded_at"],
                }
                # A review written against the earlier plan is left untouched; the flag only tells the
                # Owner it may no longer match. A later review_corrected clears it.
                correction["reviewed_before_correction"] = state.get("state") == "REVIEWED"
                state["plan_correction_events"].append(correction)
                state["plan_correction"] = correction
                state["plan_corrected_after_review"] = correction["reviewed_before_correction"]
                state["realized_r"] = payload["plan"]["realized_r"]
            elif et == "entry_pending_verification":
                state["state"] = "ENTRY_PENDING_VERIFICATION"
                _merge(state, {"provider": payload.get("provider", {})})
            elif et == "identity_linked":
                state["identity_status"] = "MATCHED"
                state["identity_link"] = deepcopy(payload.get("identity_link", {}))
            elif et == "entry_verified":
                state["state"] = "OPEN"
                state["identity_status"] = "MATCHED"
                state["entry"] = deepcopy(payload.get("entry", {}))
                state["provider"] = deepcopy(payload.get("provider", {}))
                state["execution_verification"] = deepcopy(payload.get("execution_verification", {}))
            elif et == "backtest_entry_confirmed":
                # The backtest-shaped counterpart to entry_verified: no
                # provider/identity-link exists for a sample with no real
                # execution, so this reaches OPEN directly from the draft.
                state["state"] = "OPEN"
                state["entry"] = deepcopy(payload.get("entry", {}))
                state["execution_verification"] = deepcopy(payload.get("execution_verification", {}))
            elif et == "canonical_finalized":
                state["state"] = "OPEN"
                state["trade_id"] = payload.get("trade_id", state.get("trade_id"))
                state["trade_no"] = payload.get("trade_no")
            elif et == "partial_close":
                state["state"] = "PARTIALLY_CLOSED"
                state["partial_closes"].append(deepcopy(payload.get("close", payload)))
            elif et == "final_close":
                state["state"] = "CLOSED"
                state["close"] = deepcopy(payload.get("close", payload))
                state["reported_realized_r"] = payload.get("realized_r")
                state["reported_provider_pnl"] = payload.get("provider_pnl")
                state["realized_r"] = None
                state["provider_pnl"] = None
                state["exit_verification"] = deepcopy(payload.get("exit_verification", {"state": "pending"}))
                state["provider_pnl_verification"] = deepcopy(payload.get("provider_pnl_verification", {"state": "not_observable"}))
                state["closed_at"] = payload.get("closed_at") or event.get("occurred_at")
            elif et == "exit_verified":
                state["exit_verification"] = deepcopy(payload.get("exit_verification", {}))
                state["realized_r"] = payload.get("realized_r")
                state["provider_pnl_verification"] = deepcopy(payload.get("provider_pnl_verification", {}))
                state["provider_pnl"] = payload.get("provider_pnl")
            elif et == "review_started":
                state["state"] = "REVIEW_PENDING"
                state["review_status"] = "pending"
            elif et == "reviewed":
                state["state"] = "REVIEWED"
                state["review_status"] = "reviewed"
                state["review"] = deepcopy(payload.get("review", {}))
                state["rule_violation"] = derive_rule_violation(state["review"])
                if isinstance(payload.get("strategy_resolution"), Mapping):
                    state["strategy_resolution"] = deepcopy(payload["strategy_resolution"])
            elif et == "review_corrected":
                # A correction is an append-only replacement of the review
                # interpretation.  Execution, identity, and evidence facts
                # are intentionally untouched by this reducer branch.
                state["state"] = "REVIEWED"
                state["review_status"] = "reviewed"
                state["review"] = deepcopy(payload.get("review", {}))
                state["rule_violation"] = derive_rule_violation(state["review"])
                state["strategy_resolution"] = deepcopy(payload["strategy_resolution"])
                state["review_correction"] = deepcopy(payload.get("correction", {}))
                state["plan_corrected_after_review"] = False
            elif et == "strategy_attribution_resolved":
                state["strategy_id"] = payload.get("strategy_id")
                state["rule_version"] = payload.get("rule_version")
                state["strategy_resolution"] = deepcopy(payload["strategy_resolution"])
                state["strategy_attribution_amendment"] = deepcopy(payload.get("amendment", {}))
        for state in records.values():
            trade_id = state.get("trade_id")
            if trade_id:
                for evidence in state.get("evidence", []):
                    evidence["linked_trade_id"] = evidence.get("linked_trade_id") or trade_id
        return {
            "schema_version": PROJECTION_SCHEMA,
            "projection_policy": "reduce append-only events in durable recorded_at/file order; occurred_at is domain fact time",
            "events_sha256": _digest(events),
            "record_count": len(records),
            "records": records,
        }

    def write_projection(self) -> dict[str, Any]:
        projection = self.project()
        _atomic_write(self.projection_path, projection)
        return projection

    def current(self) -> dict[str, Any]:
        if self.projection_path.exists():
            payload = json.loads(self.projection_path.read_text(encoding="utf-8"))
            if payload.get("schema_version") == PROJECTION_SCHEMA:
                return payload
        return self.write_projection()

    def _update_draft(self, draft_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        path = self.writer.drafts / f"{draft_id}.json"
        if not path.exists():
            raise LifecycleError(f"draft not found: {draft_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        _merge(record, patch)
        errors = validate_record(record)
        if errors:
            raise LifecycleError("invalid draft update: " + "; ".join(error.message for error in errors))
        # This is a pre-canonical draft update through the existing writer's
        # atomic primitive.  Canonical finalized records are never overwritten.
        self.writer._write_atomic(path, record)
        return record

    def create_draft(
        self,
        *,
        strategy_id: str | None,
        rule_version: str | None,
        symbol: str,
        direction: str,
        intended_entry: float | str | None,
        stop_loss: float | str | None,
        take_profit: float | str | None,
        planned_rr: float | str | None,
        timeframe_context: str,
        timeframe_trigger: str,
        market_context: str,
        setup_tags: list[str],
        confirmation_tags: list[str],
        thesis: str,
        chart_refs: list[str],
        created_at: str,
        recording_timing: str = "contemporaneous",
        trade_type: str = "live",
        execution_environment: str = "prop_challenge",
        draft_id: str | None = None,
        variant: str | None = None,
        recorded_at: str | None = None,
        allow_experimental: bool = False,
        allow_retired: bool = False,
        source_method: str | None = None,
        outcome_known_at_recording: bool | str = "unknown",
        execution_mode: str | None = None,
        strategy_resolution: Mapping[str, Any] | None = None,
        registration_packet_sha256: str | None = None,
    ) -> dict[str, Any]:
        if direction not in {"long", "short"}:
            raise LifecycleError("direction must be long or short")
        if registration_packet_sha256 is not None and (
            not isinstance(registration_packet_sha256, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", registration_packet_sha256)
        ):
            raise LifecycleError("registration_packet_sha256 must be a 64-character hex digest")
        if trade_type not in {"live", "backtest"}:
            raise LifecycleError("trade_type must be live or backtest")
        execution_mode = execution_mode or ("LIVE" if trade_type == "live" else "BACKTEST")
        if execution_mode not in EXECUTION_MODES:
            raise LifecycleError("execution_mode must be LIVE, PAPER, or BACKTEST")
        if execution_mode == "LIVE" and trade_type != "live":
            raise LifecycleError("LIVE execution_mode requires trade_type=live")
        if execution_mode in {"PAPER", "BACKTEST"} and trade_type != "backtest":
            raise LifecycleError("PAPER/BACKTEST execution_mode requires trade_type=backtest")
        if execution_mode == "PAPER" and execution_environment != "paper":
            raise LifecycleError("PAPER execution_mode requires execution_environment=paper")
        if recording_timing not in {"contemporaneous", "retrospective", "unknown"}:
            raise LifecycleError("recording_timing is invalid")
        if trade_type == "backtest":
            if source_method not in SOURCE_METHODS:
                raise LifecycleError(f"backtest source_method must be one of {sorted(SOURCE_METHODS)}")
        elif source_method is not None:
            raise LifecycleError("source_method is only valid for trade_type=backtest")
        if outcome_known_at_recording not in OUTCOME_KNOWN_AT_RECORDING_VALUES:
            raise LifecycleError("outcome_known_at_recording must be true, false, or unknown")
        if not isinstance(symbol, str) or not symbol.strip():
            raise LifecycleError("symbol is required")
        # Plan completeness is independent from execution truth. LIVE may keep
        # genuinely unknown or absent pre-trade facts; none are backfilled from
        # actual/exit facts. PAPER keeps its existing unknown-plan allowance.
        allow_unknown_plan_fields = trade_type == "live" or recording_timing == "retrospective" or execution_mode == "PAPER"
        numeric_plan_fields = {
            "intended_entry": intended_entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "planned_rr": planned_rr,
        }
        text_plan_fields = {
            "timeframe_context": timeframe_context,
            "timeframe_trigger": timeframe_trigger,
            "market_context": market_context,
            "thesis": thesis,
        }
        for field_name, value in numeric_plan_fields.items():
            unknown_value = (value is None and trade_type == "live") or (isinstance(value, str) and value in PLAN_FIELD_UNKNOWN_VALUES)
            if unknown_value:
                if not allow_unknown_plan_fields:
                    raise LifecycleError(
                        f"{field_name} may only be unknown when recording_timing=retrospective, "
                        "or for LIVE/PAPER capture"
                    )
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LifecycleError(f"{field_name} must be a real number or unknown")
        for field_name, value in text_plan_fields.items():
            unknown_value = (value is None and trade_type == "live") or (isinstance(value, str) and value in PLAN_FIELD_UNKNOWN_VALUES)
            if unknown_value:
                if not allow_unknown_plan_fields:
                    raise LifecycleError(
                        f"{field_name} may only be unknown when recording_timing=retrospective, "
                        "or for LIVE/PAPER capture"
                    )
                continue
            if not isinstance(value, str) or not value.strip():
                raise LifecycleError(f"{field_name} is required or unknown")
        _validate_retrospective_plan(
            recording_timing=recording_timing,
            setup_tags=setup_tags,
            confirmation_tags=confirmation_tags,
            market_context=market_context,
            thesis=thesis,
        )
        if not _iso_timestamp(created_at):
            raise LifecycleError("created_at must be an ISO timestamp")
        if recorded_at is not None and not _iso_timestamp(recorded_at):
            raise LifecycleError("recorded_at must be an ISO timestamp")
        legacy_unresolved_identity = (
            trade_type == "live"
            and recording_timing == "retrospective"
            and strategy_id == HISTORICAL_UNRESOLVED_STRATEGY_ID
            and rule_version == HISTORICAL_UNRESOLVED_RULE_VERSION
        )
        resolution = None
        if trade_type == "live" and strategy_resolution is None:
            raise LifecycleError("a LIVE draft requires a strategy_resolution produced by the strategy resolver")
        if strategy_resolution is not None:
            resolution = _validate_strategy_resolution(strategy_resolution)
            if resolution.get("resolution_source") != RESOLVER_VERSION:
                raise LifecycleError("strategy_resolution must come from the strategy resolver")
            if resolution["status"] in {"no_match", "unresolved"}:
                # New records use true null identity. The historical sentinel
                # remains readable for old retrospective records and NO_MATCH
                # compatibility callers; it is never a strategy registry entry.
                null_identity = strategy_id is None and rule_version is None
                legacy_no_match_identity = (
                    resolution["status"] == "no_match"
                    and strategy_id == HISTORICAL_UNRESOLVED_STRATEGY_ID
                    and rule_version == HISTORICAL_UNRESOLVED_RULE_VERSION
                )
                if trade_type != "live" or not (null_identity or legacy_no_match_identity or legacy_unresolved_identity):
                    raise LifecycleError(f"{resolution['status']} resolution requires null LIVE strategy identity")
                legacy_unresolved_identity = legacy_unresolved_identity or legacy_no_match_identity
            elif resolution["status"] in {"contemporaneous_confirmed", "replay_reconstructed"}:
                if resolution["strategy_id"] != strategy_id or resolution["rule_version"] != rule_version:
                    raise LifecycleError("draft strategy does not match the strategy resolver result")
            else:
                raise LifecycleError("an unresolved strategy result cannot create a registered draft")
        registry = load_registry(self.root)
        strategy = next((item for item in registry.get("strategies", []) if item.get("strategy_id") == strategy_id), None)
        # Frozen historical B1/B3 strategy registry is immutable. A manual
        # Replay BACKTEST may use an independently versioned experimental
        # research rule without pretending to be an active LIVE strategy. This
        # also preserves historical candidate versions when that strategy has
        # since gained a different active version in the operational registry.
        if (
            trade_type == "backtest"
            and source_method == "tradingview_replay"
            and allow_experimental
            and isinstance(rule_version, str)
            and (
                strategy is None
                or (
                    strategy.get("status") == "active"
                    and rule_version not in strategy.get("rule_versions", [])
                )
            )
        ):
            from .registry import load_version
            try:
                experimental_version = load_version(self.root, rule_version)
            except (OSError, ValueError):
                experimental_version = None
            if (
                isinstance(experimental_version, Mapping)
                and experimental_version.get("strategy_id") == strategy_id
                and experimental_version.get("status") == "candidate"
                and experimental_version.get("lifecycle_at_freeze") == "experimental"
            ):
                strategy = {"strategy_id": strategy_id, "status": "experimental", "rule_versions": [rule_version]}
        if legacy_unresolved_identity:
            # A late historical execution may be real and fully provider-
            # verified even when its original strategy identity is absent.
            # Keep the existing fail-closed placeholder vocabulary; do not
            # turn it into a registry strategy or rule authority.
            strategy_payload_alias_status = "unresolved"
        elif strategy_resolution is not None and strategy_resolution.get("status") in {"unresolved", "no_match"}:
            # Null strategy identity is the actual sub-state; no placeholder or
            # alias is stored on new canonical records.
            strategy_payload_alias_status = None
        else:
            if strategy_id is None or rule_version is None:
                raise LifecycleError("a LIVE strategy identity must be null together with a resolved attribution or use a registered strategy")
            if not strategy:
                raise LifecycleError(f"strategy is not registered: {strategy_id}")
            status = strategy.get("status")
            if status == "retired" and not allow_retired:
                raise LifecycleError("retired strategy requires explicit allow_retired=true")
            if status == "experimental" and not allow_experimental:
                raise LifecycleError("experimental strategy requires explicit allow_experimental=true")
            versions = strategy.get("rule_versions", [])
            if rule_version not in versions and not (status == "experimental" and rule_version == UNFROZEN_EXPERIMENTAL_PLACEHOLDER):
                raise LifecycleError("rule_version is not registered for the selected strategy")
            strategy_payload_alias_status = None
        if not is_record_identifier(draft_id) and draft_id is not None:
            raise LifecycleError("draft_id must be a rec_<hash> identifier")
        plan_values_known = all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in numeric_plan_fields.values()
        ) and all(
            isinstance(value, str) and value.strip() and value not in PLAN_FIELD_UNKNOWN_VALUES
            for value in text_plan_fields.values()
        )
        plan_completeness = "complete" if plan_values_known else "partial"
        planned = {
            "entry_price": intended_entry,
            "stop_loss_price": stop_loss,
            "take_profit_price": take_profit,
            # Frozen ICT rules refer to plan.target_price. Keep the exact
            # owner-supplied TP fact under that canonical field as well as the
            # existing take_profit_price field; this is a semantic alias, not
            # an inferred value.
            "target_price": take_profit,
            "planned_rr": planned_rr,
            "timeframe_context": timeframe_context,
            "timeframe_trigger": timeframe_trigger,
            "market_context": market_context,
            "thesis": thesis,
            "setup_tags": list(setup_tags),
            "confirmation_tags": list(confirmation_tags),
            "chart_refs": list(chart_refs),
            "completeness": plan_completeness,
        }
        stable_key = draft_id or _digest({
            "strategy_id": strategy_id,
            "symbol": symbol,
            "created_at": created_at,
            "planned": planned,
            **({"registration_packet_sha256": registration_packet_sha256.lower()} if registration_packet_sha256 else {}),
        })[:20]
        record_id = draft_id or "rec_" + stable_key
        record = {
            "schema_version": "trade.v1",
            "record_id": record_id,
            "record_kind": "trade_candidate",
            "trade_id": None,
            "observation_id": None,
            "trade_no": None,
            "classification_status": "pending",
            "type": trade_type,
            "execution_mode": execution_mode,
            "execution_environment": execution_environment,
            "recording_timing": recording_timing,
            "legacy_source": {"repository": "trading-record", "layer": "operational_capture", "path": f"operational/captures/{record_id}.json"},
            "identity": {"event_date": created_at[:10], "direction": direction, "instrument": {"symbol": symbol}},
            "strategy": {
                "strategy_id": strategy_id,
                "rule_version": rule_version,
                "setup_tags": list(setup_tags),
                "confirmation_tags": list(confirmation_tags),
                "variant": variant,
                **({"alias_status": strategy_payload_alias_status} if strategy_payload_alias_status else {}),
            },
            "plan": {"entry_price": intended_entry, "stop_loss_price": stop_loss, "take_profit_price": take_profit, "target_price": take_profit, "rr_pretrade": planned_rr, "timeframe_context": timeframe_context, "timeframe_trigger": timeframe_trigger, "market_context": market_context, "thesis": thesis, "chart_refs": list(chart_refs), "completeness": plan_completeness},
            "actual": {"execution_status": "planned", "position_status": "planned"},
            "result": {"outcome": "unknown"},
            "evidence": [],
            "review": {"status": "not_started"},
            **({"strategy_resolution": resolution} if resolution else {}),
            "dedupe": {"status": "pending"},
            "analytics": {"eligibility": "pending"},
            "raw_legacy": {},
            "provenance": {
                "capture_schema": CAPTURE_SCHEMA,
                "operational_capture_id": record_id,
                "created_at": created_at,
                **({"registration_packet_sha256": registration_packet_sha256.lower()} if registration_packet_sha256 else {}),
            },
            "operational_capture": {
                "schema_version": CAPTURE_SCHEMA,
                "planned": planned,
                "created_at": created_at,
                "source_method": source_method,
                "outcome_known_at_recording": outcome_known_at_recording,
                "execution_mode": execution_mode,
                **({"registration_packet_sha256": registration_packet_sha256.lower()} if registration_packet_sha256 else {}),
            },
        }
        existing_event = self._event_by_idempotency(f"draft:{record_id}", "draft_created")
        if existing_event is not None:
            return {"draft_id": record_id, "event_id": existing_event["event_id"], "state": "PRE_TRADE_DRAFT"}
        if recorded_at is not None:
            self._validate_recorded_order(recorded_at, self._read_events())
        result = self.writer.create_candidate(record)
        if result.get("validation_errors"):
            raise LifecycleError(json.dumps(result["validation_errors"], ensure_ascii=False))
        draft_event_payload = {
            "draft_id": record_id,
            "record_id": record_id,
            "strategy_id": strategy_id,
            "rule_version": rule_version,
            "symbol": symbol,
            "direction": direction,
            "trade_type": trade_type,
            "execution_mode": execution_mode,
            "created_at": created_at,
            "variant": variant,
            "planned": planned,
            "source_method": source_method,
            "outcome_known_at_recording": outcome_known_at_recording,
            **({"registration_packet_sha256": registration_packet_sha256.lower()} if registration_packet_sha256 else {}),
        }
        if strategy_payload_alias_status:
            draft_event_payload["strategy_alias_status"] = strategy_payload_alias_status
        if resolution:
            draft_event_payload["strategy_resolution"] = resolution
        event = self.append(
            "draft_created",
            draft_event_payload,
            idempotency_key=f"draft:{record_id}",
            occurred_at=created_at,
            recorded_at=recorded_at,
        )
        return {"draft_id": record_id, "event_id": event["event_id"], "state": "PRE_TRADE_DRAFT"}

    def correct_evidence_locator(
        self,
        draft_id: str,
        *,
        source_ref: str,
        sha256: str,
        category: str,
        prior_locator: str,
        locator: str,
        reason: str,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        """Append an auditable correction for a verified evidence-store locator.

        The original evidence event is never edited. The correction is allowed
        only when one current attachment matches the exact source reference,
        category, digest, and prior locator, and the replacement bytes verify
        through the existing EvidenceStore.
        """

        if not all(isinstance(value, str) and value.strip() for value in (source_ref, sha256, category, prior_locator, locator, reason)):
            raise LifecycleError("evidence locator correction requires non-empty exact identifiers and a reason")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise LifecycleError("evidence locator correction sha256 must be lower-case hexadecimal")
        if category not in EVIDENCE_CATEGORIES:
            raise LifecycleError("evidence locator correction category is invalid")
        evidence_store = EvidenceStore(self.root)
        try:
            evidence_store.read(locator, expected_sha256=sha256)
        except EvidenceStoreError as exc:
            raise LifecycleError(f"corrected evidence artifact is not verified: {exc}") from exc
        metadata = evidence_store.metadata(sha256)
        if not isinstance(metadata, Mapping) or metadata.get("locator") != locator:
            raise LifecycleError("corrected evidence locator has no matching EvidenceStore metadata")

        record = self._projected_record(draft_id)
        payload: dict[str, Any] = {
            "draft_id": draft_id,
            "source_ref": source_ref,
            "category": category,
            "sha256": sha256,
            "prior_locator": prior_locator,
            "locator": locator,
            "reason": reason.strip(),
            "media_type": metadata.get("media_type"),
            "byte_size": metadata.get("byte_size"),
            "original_filename": metadata.get("original_filename"),
        }
        if isinstance(record, Mapping) and record.get("trade_id"):
            payload["trade_id"] = record["trade_id"]
        identity = json.dumps(
            [draft_id, source_ref, category, sha256, prior_locator, locator],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        key = f"evidence-locator-corrected:{hashlib.sha256(identity).hexdigest()}"
        # Match by the full correction identity as well as the new key so
        # corrections already appended by the earlier key format remain
        # idempotent when replayed after this code is deployed.
        existing_event = next(
            (
                event
                for event in self._read_events()
                if event.get("event_type") == "evidence_locator_corrected"
                and isinstance(event.get("payload"), Mapping)
                and all(event["payload"].get(field) == payload.get(field) for field in (
                    "draft_id", "source_ref", "category", "sha256", "prior_locator", "locator",
                    "reason", "media_type", "byte_size", "original_filename", "trade_id",
                ))
            ),
            None,
        )
        if existing_event is not None:
            if existing_event.get("payload") != payload:
                raise LifecycleError("evidence locator correction idempotency key reused with different details")
            return existing_event

        current_evidence = self._attached_evidence(draft_id)
        matches = [
            item for item in current_evidence
            if item.get("source_ref") == source_ref
            and item.get("category") == category
            and item.get("sha256") == sha256
            and item.get("locator") == prior_locator
        ]
        if len(matches) != 1:
            raise LifecycleError("evidence locator correction must match exactly one current attached item")
        timestamp = recorded_at or _now()
        return self.append(
            "evidence_locator_corrected",
            payload,
            idempotency_key=key,
            occurred_at=timestamp,
            recorded_at=timestamp,
        )

    def record_owner_statement(
        self,
        trade_id: str,
        *,
        verbatim_text: str,
        source_id: str,
    ) -> dict[str, Any]:
        """Append exact Owner wording without changing the frozen packet or trade.

        ``source_id`` identifies the confirmed registration packet or governed
        supplemental source. It is also the retry key: replaying the same source
        and text returns the existing event, while changing text under that
        source fails closed. Event timestamps are generated now by ``append``;
        callers cannot backdate the Owner statement.
        """

        if not isinstance(trade_id, str) or not trade_id.strip():
            raise LifecycleError("Owner statement requires a canonical trade_id")
        if not isinstance(verbatim_text, str) or not verbatim_text.strip():
            raise LifecycleError("Owner statement must preserve non-empty verbatim text")
        if len(verbatim_text) > 20000:
            raise LifecycleError("Owner statement exceeds the 20000 character preservation limit")
        if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", source_id):
            raise LifecycleError("Owner statement source_id must be a stable safe identifier")
        record = self._projected_record(trade_id)
        if not isinstance(record, Mapping) or record.get("trade_id") != trade_id:
            raise LifecycleError("Owner statement supplement requires an existing canonical trade")

        payload = {
            "trade_id": trade_id,
            "source_id": source_id,
            "verbatim_text": verbatim_text,
            "sha256": hashlib.sha256(verbatim_text.encode("utf-8")).hexdigest(),
            "authority": "owner_supplied_verbatim",
        }
        key = f"owner-statement:{trade_id}:{source_id}"
        existing = self._event_by_idempotency(key, "owner_statement_recorded")
        if existing is not None:
            if existing.get("payload") != payload:
                raise LifecycleError("Owner statement retry changed text or authority for the same source_id")
            return existing
        return self.append(
            "owner_statement_recorded",
            payload,
            idempotency_key=key,
        )

    def record_chart_roles(
        self,
        trade_id: str,
        chart_roles: Mapping[str, Any],
        *,
        source_id: str,
        confirmed_at: str,
    ) -> dict[str, Any]:
        """Append the Agent's entry/exit chart judgment for an already-registered trade.

        The Agent decides which saved image is the entry or exit chart; this only
        records that judgment and checks that each cited image is saved on the trade.
        ``chart_roles`` is the complete current judgment: a role left out is withdrawn.
        The Agent attests that the Owner confirmed it in the same conversation at
        ``confirmed_at``; this is the Agent's claim, not platform-verified identity,
        and the orchestration layer must enforce the single confirmation before calling.
        ``source_id`` is the retry key: the same source and content returns the
        existing event; changed content under it fails closed.
        """

        if not isinstance(trade_id, str) or not trade_id.strip():
            raise LifecycleError("chart roles require a canonical trade_id")
        record = self._projected_record(trade_id)
        if not isinstance(record, Mapping) or record.get("trade_id") != trade_id:
            raise LifecycleError("chart roles require an existing canonical trade")
        payload = {
            "trade_id": trade_id,
            "source_id": source_id,
            "chart_roles": json.loads(json.dumps(dict(chart_roles))),
            "authority": "agent_attested_owner_confirmation",
            "owner_identity": "example-owner",
            "approval_source": "same_conversation_agent_attestation",
            "confirmed_at": confirmed_at,
        }
        key = f"chart-roles:{trade_id}:{source_id}"
        existing = self._event_by_idempotency(key, "chart_roles_recorded")
        if existing is not None:
            if existing.get("payload") != payload:
                raise LifecycleError("chart roles retry changed content for the same source_id")
            return existing
        return self.append("chart_roles_recorded", payload, idempotency_key=key)

    def record_plan_r_correction(
        self,
        trade_id: str,
        chart: Mapping[str, Any],
        *,
        source_id: str,
        confirmed_at: str,
    ) -> dict[str, Any]:
        """Append a chart-derived plan and realized R for an already-registered LIVE trade.

        The Agent reads Entry/SL/TP from the Owner's chart and passes them with the saved
        image each came from; 1R is the actual fill to the chart stop. R is recomputed here
        from the trade's own fill and exit, never taken from the caller. The Agent attests
        the Owner's same-conversation confirmation at ``confirmed_at``. The same source and
        content returns the existing event; changed content under it fails closed.
        """

        if not isinstance(trade_id, str) or not trade_id.strip():
            raise LifecycleError("a plan/R correction requires a canonical trade_id")
        record = self._projected_record(trade_id)
        if not isinstance(record, Mapping) or record.get("trade_id") != trade_id:
            raise LifecycleError("a plan/R correction requires an existing canonical trade")
        chart_copy = json.loads(json.dumps(dict(chart)))
        payload = {
            "trade_id": trade_id,
            "source_id": source_id,
            "chart": chart_copy,
            "plan": _derive_plan_correction(record, chart_copy),
            "authority": "agent_attested_owner_confirmation",
            "owner_identity": "example-owner",
            "approval_source": "same_conversation_agent_attestation",
            "confirmed_at": confirmed_at,
        }
        key = f"plan-r-correction:{trade_id}:{source_id}"
        existing = self._event_by_idempotency(key, "plan_r_corrected")
        if existing is not None:
            if existing.get("payload") != payload:
                raise LifecycleError("plan/R correction retry changed content for the same source_id")
            return existing
        return self.append("plan_r_corrected", payload, idempotency_key=key)

    def record_backtest_timing(
        self,
        trade_id: str,
        *,
        entry_at: str,
        exit_at: str,
        source_id: str,
        confirmed_at: str,
    ) -> dict[str, Any]:
        """Append Owner-stated entry and exit times for an already-registered BACKTEST trade.

        Both times must carry a UTC offset. They are the Owner's replay timing, not provider
        execution facts, and never change the trade's prices or lifecycle state. The Agent
        attests the Owner's same-conversation confirmation at ``confirmed_at``. The same source
        and content returns the existing event; changed content under it fails closed.
        """

        if not isinstance(trade_id, str) or not trade_id.strip():
            raise LifecycleError("backtest timing requires a canonical trade_id")
        record = self._projected_record(trade_id)
        if not isinstance(record, Mapping) or record.get("trade_id") != trade_id:
            raise LifecycleError("backtest timing requires an existing canonical trade")
        payload = {
            "trade_id": trade_id,
            "source_id": source_id,
            "entry_at": entry_at,
            "exit_at": exit_at,
            "authority": "agent_attested_owner_confirmation",
            "owner_identity": "example-owner",
            "approval_source": "same_conversation_agent_attestation",
            "confirmed_at": confirmed_at,
        }
        key = f"backtest-timing:{trade_id}:{source_id}"
        existing = self._event_by_idempotency(key, "backtest_timing_recorded")
        if existing is not None:
            if existing.get("payload") != payload:
                raise LifecycleError("backtest timing retry changed content for the same source_id")
            return existing
        return self.append("backtest_timing_recorded", payload, idempotency_key=key)

    def amend_backtest_rule_version(self, trade_id: str, *, rule_version: str, source_id: str, reason: str, confirmed_at: str) -> dict[str, Any]:
        """Append an Owner-approved re-pin of a BACKTEST's rule version (same strategy, registered version)."""

        payload = {
            "trade_id": trade_id,
            "rule_version": rule_version,
            "source_id": source_id,
            "reason": reason,
            "authority": "agent_attested_owner_confirmation",
            "owner_identity": "example-owner",
            "approval_source": "same_conversation_agent_attestation",
            "confirmed_at": confirmed_at,
        }
        key = f"backtest-rule-version:{trade_id}:{source_id}"
        existing = self._event_by_idempotency(key, "backtest_rule_version_amended")
        if existing is not None:
            if existing.get("payload") != payload:
                raise LifecycleError("backtest rule version retry changed content for the same source_id")
            return existing
        return self.append("backtest_rule_version_amended", payload, idempotency_key=key)

    def record_owner_words_unrecoverable(
        self,
        trade_id: str,
        *,
        source_id: str,
        confirmed_at: str,
        reason: str,
    ) -> dict[str, Any]:
        """Append the Agent's attestation that this trade's verbatim Owner words cannot be recovered.

        This is an honest acknowledgement, not a substitute for the words: the trade stays
        incomplete and nothing is written into ``owner_statements``. It is refused for a trade
        that already has the Owner's verbatim words. The Agent attests the Owner confirmed it in
        the same conversation at ``confirmed_at``; this is the Agent's claim, not platform-verified
        identity. ``source_id`` is the retry key: the same content returns the existing event and
        changed content under it fails closed.
        """

        if not isinstance(trade_id, str) or not trade_id.strip():
            raise LifecycleError("an unrecoverable-words declaration requires a canonical trade_id")
        record = self._projected_record(trade_id)
        if not isinstance(record, Mapping) or record.get("trade_id") != trade_id:
            raise LifecycleError("an unrecoverable-words declaration requires an existing canonical trade")
        payload = {
            "trade_id": trade_id,
            "source_id": source_id,
            "reason": reason,
            "authority": "agent_attested_owner_confirmation",
            "owner_identity": "example-owner",
            "approval_source": "same_conversation_agent_attestation",
            "confirmed_at": confirmed_at,
        }
        key = f"owner-words-unrecoverable:{trade_id}:{source_id}"
        existing = self._event_by_idempotency(key, "owner_words_unrecoverable_recorded")
        if existing is not None:
            if existing.get("payload") != payload:
                raise LifecycleError("unrecoverable-words retry changed content for the same source_id")
            return existing
        return self.append("owner_words_unrecoverable_recorded", payload, idempotency_key=key)

    def attach_evidence(self, draft_id: str, evidence: Mapping[str, Any], *, idempotency_key: str | None = None, recorded_at: str | None = None) -> dict[str, Any]:
        required = ("source_ref", "timing", "type", "category", "sha256")
        if any(not evidence.get(key) for key in required):
            raise LifecycleError("evidence requires source_ref, timing, type, category, and sha256")
        self._draft(draft_id)
        if evidence.get("category") not in EVIDENCE_CATEGORIES:
            raise LifecycleError("evidence category must be pre_trade_context, entry, exit, or review")
        digest = str(evidence.get("sha256"))
        if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
            raise LifecycleError("evidence sha256 must be a 64-character hexadecimal digest")
        digest = digest.lower()
        normalized = dict(evidence)
        normalized["sha256"] = digest
        try:
            normalized.update(normalize_evidence_time_metadata(normalized))
        except EvidenceTimeError as exc:
            raise LifecycleError(f"evidence timing metadata is invalid: {exc}") from exc
        normalized.setdefault("linked_draft_id", draft_id)
        normalized.setdefault("linked_trade_id", None)
        normalized.setdefault("method", normalized["type"] if normalized["type"] in EVIDENCE_METHODS else "manual_note")
        locator = normalized.get("locator")
        if not isinstance(locator, str) or not locator.strip():
            normalized["locator"] = f"sha256:{digest}"
        elif locator == "artifact:full":
            raise LifecycleError("artifact:full is a legacy-only locator; ingest binary evidence through EvidenceStore")
        elif locator.startswith("artifact:"):
            try:
                EvidenceStore(self.root).read(locator, expected_sha256=digest)
            except EvidenceStoreError as exc:
                raise LifecycleError(f"evidence artifact is not verified against its sha256: {exc}") from exc
        elif locator.startswith("sha256:") and locator != f"sha256:{digest}":
            raise LifecycleError("metadata-only evidence locator must match its sha256")
        normalized["recorded_at"] = recorded_at or normalized.get("recorded_at") or _now()
        if not _iso_timestamp(normalized["recorded_at"]):
            raise LifecycleError("evidence recorded_at must be an ISO timestamp")
        candidate_identity = evidence_identity(normalized)
        existing_attached = self._attached_evidence(draft_id)
        for existing in existing_attached:
            if (
                existing.get("source_ref") == normalized["source_ref"]
                and existing.get("category") == normalized["category"]
                and evidence_identity(existing) != candidate_identity
            ):
                raise LifecycleError(
                    "evidence identity conflict for draft/category/source_ref"
                )
        key = idempotency_key or f"evidence:{draft_id}:{normalized['sha256']}:{normalized['category']}"
        existing_event = self._event_by_idempotency(key, "evidence_attached")
        if existing_event is not None:
            event_payload = existing_event.get("payload", {})
            existing_payload = event_payload.get("evidence", {}) if isinstance(event_payload, Mapping) else {}
            if (
                not isinstance(existing_payload, Mapping)
                or existing_payload.get("source_ref") != normalized["source_ref"]
                or existing_payload.get("category") != normalized["category"]
                or evidence_identity(existing_payload) != candidate_identity
            ):
                raise LifecycleError("evidence idempotency key reused with a different identity")
            return existing_event
        for event in self._read_events():
            if event.get("event_type") != "evidence_attached":
                continue
            payload = event.get("payload", {})
            attached = payload.get("evidence", {}) if isinstance(payload, Mapping) else {}
            if (
                payload.get("draft_id") == draft_id
                and isinstance(attached, Mapping)
                and attached.get("source_ref") == normalized["source_ref"]
                and attached.get("category") == normalized["category"]
                and evidence_identity(attached) == candidate_identity
            ):
                return event
        self._validate_recorded_order(normalized["recorded_at"], self._read_events())
        self.writer.attach_evidence(draft_id, normalized)
        return self.append(
            "evidence_attached",
            {"draft_id": draft_id, "evidence": normalized},
            idempotency_key=key,
            occurred_at=normalized.get("captured_at"),
            recorded_at=normalized.get("recorded_at"),
        )

    def mark_entry_pending(self, draft_id: str, *, provider: Mapping[str, Any] | None = None, occurred_at: str | None = None, recorded_at: str | None = None) -> dict[str, Any]:
        if not (self.writer.drafts / f"{draft_id}.json").exists():
            raise LifecycleError(f"draft not found: {draft_id}")
        existing_event = self._event_by_idempotency(f"entry-pending:{draft_id}", "entry_pending_verification")
        if existing_event is not None:
            return existing_event
        return self.append("entry_pending_verification", {"draft_id": draft_id, "provider": dict(provider or {})}, idempotency_key=f"entry-pending:{draft_id}", occurred_at=occurred_at, recorded_at=recorded_at)

    @staticmethod
    def _same_link_key(existing: Mapping[str, Any], key: str, draft_id: str, provider: str, lifecycle_id: str) -> bool:
        """Match by idempotency key, or by the same draft+provider+lifecycle regardless of provider case.

        Legacy links were written with ``PropW`` while newer ones use ``propw``;
        history is append-only, so equality is normalized on read, not rewritten.
        """

        if existing.get("idempotency_key") == key:
            return True
        return (
            existing.get("draft_id") == draft_id
            and canonical_provider(existing.get("provider")) == provider
            and existing.get("provider_lifecycle_id") == lifecycle_id
            and str(existing.get("idempotency_key", "")).startswith("identity-link:")
        )

    @staticmethod
    def _link_field_differs(existing: Mapping[str, Any], field: str, value: Any) -> bool:
        if field == "provider":
            return canonical_provider(existing.get(field)) != canonical_provider(value)
        if field == "idempotency_key":
            return False
        return existing.get(field) != value

    @staticmethod
    def _require_unbound_provider_lifecycle(links: list[dict[str, Any]], draft_id: str, provider: str, lifecycle_id: str) -> None:
        """One provider lifecycle may map to at most one canonical draft (global, not per-draft)."""

        bound = {
            link.get("draft_id")
            for link in links
            if link.get("match_disposition") == "MATCHED"
            and canonical_provider(link.get("provider")) == provider
            and link.get("provider_lifecycle_id") == lifecycle_id
        }
        if len(bound) > 1:
            raise LifecycleError(f"DATA_INTEGRITY_ERROR: {provider}:{lifecycle_id} is bound to multiple drafts")
        if bound and draft_id not in bound:
            raise LifecycleError(f"ALREADY_REGISTERED: {provider}:{lifecycle_id} is already bound to draft {next(iter(bound))}")

    def create_identity_link(
        self,
        draft_id: str,
        *,
        provider: str,
        provider_lifecycle_id: str,
        instrument: str,
        direction: str,
        provider_execution_timestamp: str,
        candidate_source: str,
        match_disposition: str,
        evidence_refs: list[str],
        created_at: str | None = None,
        decision_created_at: str | None = None,
        recorded_at: str | None = None,
        authority: str = "deterministic_match",
        idempotency_key: str | None = None,
        registration_packet_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Persist the post-cutover provider-to-draft identity decision."""

        provider = canonical_provider(provider)
        if not provider or not provider_lifecycle_id or not instrument or direction not in {"long", "short"}:
            raise LifecycleError("identity link requires provider, lifecycle ID, instrument, and direction")
        if not _iso_timestamp(provider_execution_timestamp):
            raise LifecycleError("provider_execution_timestamp must be an ISO timestamp")
        if not candidate_source or not authority:
            raise LifecycleError("identity link requires candidate source and authority")
        if registration_packet_sha256 is not None and (
            not isinstance(registration_packet_sha256, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", registration_packet_sha256)
        ):
            raise LifecycleError("registration_packet_sha256 must be a 64-character hex digest")
        if match_disposition not in IDENTITY_DISPOSITIONS:
            raise LifecycleError("match_disposition must be MATCHED, AMBIGUOUS, UNMATCHED, or CONFLICT")
        if not isinstance(evidence_refs, list) or any(not isinstance(item, str) or not item.strip() for item in evidence_refs):
            raise LifecycleError("identity link evidence_refs must be a list of non-empty strings")
        if match_disposition == "MATCHED" and not evidence_refs:
            raise LifecycleError("MATCHED identity link requires evidence refs")
        decision_time = decision_created_at or created_at or _now()
        if not _iso_timestamp(decision_time):
            raise LifecycleError("decision_created_at must be an ISO timestamp")
        if recorded_at is not None and not _iso_timestamp(recorded_at):
            raise LifecycleError("recorded_at must be an ISO timestamp")
        resolved_evidence = self._resolve_evidence(draft_id, evidence_refs)
        if match_disposition == "MATCHED":
            if any(item.get("category") != "entry" or not self._is_provider_evidence(item) or item.get("timing") != "execution" for item in resolved_evidence):
                raise LifecycleError("MATCHED identity link requires attached provider execution evidence")
        key = idempotency_key or f"identity-link:{draft_id}:{provider}:{provider_lifecycle_id}"
        immutable = {
            "draft_id": draft_id,
            "provider": provider,
            "provider_lifecycle_id": provider_lifecycle_id,
            "instrument": instrument,
            "direction": direction,
            "provider_execution_timestamp": provider_execution_timestamp,
            "provider_execution_at": provider_execution_timestamp,
            "candidate_source": candidate_source,
            "match_disposition": match_disposition,
            "evidence_refs": list(evidence_refs),
            "authority": authority,
            "idempotency_key": key,
            "decision_created_at": decision_time,
            **({"registration_packet_sha256": registration_packet_sha256.lower()} if registration_packet_sha256 else {}),
        }
        # Idempotent retries must replay even after the lifecycle has advanced.
        for existing in self._read_identity_links():
            if self._same_link_key(existing, key, draft_id, provider, provider_lifecycle_id):
                if any(self._link_field_differs(existing, field, value) for field, value in immutable.items()):
                    raise LifecycleError("identity-link idempotency key reused with different evidence")
                if existing.get("match_disposition") == "MATCHED":
                    linked_event = self._event_by_idempotency(
                        f"identity-linked:{draft_id}:{existing['identity_link_id']}", "identity_linked"
                    )
                    if linked_event is not None:
                        return linked_event
                return existing
        if match_disposition == "MATCHED":
            self._require_unbound_provider_lifecycle(self._read_identity_links(), draft_id, provider, provider_lifecycle_id)
        self._require_state(draft_id, {"ENTRY_PENDING_VERIFICATION"}, "identity link requires entry verification pending")
        draft = self._draft(draft_id)
        draft_symbol = ((draft.get("identity") or {}).get("instrument") or {}).get("symbol")
        draft_direction = (draft.get("identity") or {}).get("direction")
        if match_disposition == "MATCHED" and (draft_symbol != instrument or draft_direction != direction):
            raise LifecycleError("MATCHED identity link conflicts with draft symbol or direction")
        with self._locked():
            links = self._read_identity_links()
            for existing in links:
                if self._same_link_key(existing, key, draft_id, provider, provider_lifecycle_id):
                    if any(self._link_field_differs(existing, field, value) for field, value in immutable.items()):
                        raise LifecycleError("identity-link idempotency key reused with different evidence")
                    return existing
            if match_disposition == "MATCHED":
                self._require_unbound_provider_lifecycle(links, draft_id, provider, provider_lifecycle_id)
            created = decision_time
            link_recorded_at = recorded_at or _now()
            self._validate_recorded_order(link_recorded_at, links)
            link = {
                "schema_version": IDENTITY_LINK_SCHEMA,
                "identity_link_id": "oil_" + _digest({**immutable, "created_at": created})[:24],
                **immutable,
                "created_at": created,
                "recorded_at": link_recorded_at,
            }
            link["digest"] = _digest(link)
            with self.identity_links_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(link, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        if match_disposition != "MATCHED":
            return link
        return self.append(
            "identity_linked",
            {"draft_id": draft_id, "identity_link": link, "identity_link_id": link["identity_link_id"], "match_disposition": "MATCHED"},
            idempotency_key=f"identity-linked:{draft_id}:{link['identity_link_id']}",
            occurred_at=link["decision_created_at"],
            recorded_at=link["recorded_at"],
        )

    def verify_entry(
        self,
        draft_id: str,
        *,
        instrument: str,
        direction: str,
        actual_entry: float,
        execution_at: str,
        source_ref: str,
        provider_lifecycle_id: str,
        provider: str = "propw",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if direction not in {"long", "short"} or not provider_lifecycle_id or not provider:
            raise LifecycleError("verified entry requires direction and provider lifecycle identity")
        key = idempotency_key or f"entry-verified:{draft_id}:{provider_lifecycle_id}"
        existing_event = self._event_by_idempotency(key, "entry_verified")
        if existing_event is not None:
            return existing_event
        self._require_state(draft_id, {"ENTRY_PENDING_VERIFICATION"}, "entry verification requires entry pending state")
        link = next((item for item in self._read_identity_links() if item.get("draft_id") == draft_id and item.get("provider_lifecycle_id") == provider_lifecycle_id), None)
        if not isinstance(link, Mapping) or link.get("match_disposition") != "MATCHED":
            raise LifecycleError("MATCHED governed identity link is required before entry verification")
        if canonical_provider(link.get("provider")) != canonical_provider(provider) or link.get("instrument") != instrument or link.get("direction") != direction:
            raise LifecycleError("entry verification conflicts with governed identity link")
        if link.get("provider_execution_timestamp") != execution_at:
            raise LifecycleError("entry verification timestamp conflicts with governed identity link")
        if source_ref not in set(link.get("evidence_refs", [])):
            raise LifecycleError("entry verification source_ref is not in governed identity evidence")
        entry_evidence = self._resolve_evidence(draft_id, [source_ref], categories={"entry"})[0]
        if entry_evidence.get("timing") != "execution" or not self._is_provider_evidence(entry_evidence):
            raise LifecycleError("entry verification requires attached provider execution evidence")
        evidence = {"evidence_kind": "external_fill", "source_ref": source_ref, "instrument": instrument, "side": "buy" if direction == "long" else "sell", "execution_at": execution_at, "price": actual_entry, "provider_lifecycle_id": provider_lifecycle_id}
        verification = {"schema_version": "execution-verification.v1", "entry": {"state": "confirmed", "basis": "external_fill", "attestation_support": None, "evidence_refs": [source_ref], "adjudication_revision_ref": None}, "exit": {"state": "not_applicable", "basis": "none", "attestation_support": None, "evidence_refs": [], "adjudication_revision_ref": None}}
        if recorded_at is not None:
            self._validate_recorded_order(recorded_at, self._read_events())
        self._update_draft(draft_id, {"classification_status": "resolved", "dedupe": {"status": "canonical", "canonical_source": "governed_identity_link", "provider_lifecycle_id": provider_lifecycle_id}, "identity": {"direction": direction, "instrument": {"symbol": instrument}, "entry_at": execution_at}, "actual": {"entry_price": actual_entry, "entry_at": execution_at, "execution_status": "entered", "position_status": "open", "execution_verification": verification, "execution_evidence": {"entry": {"external_fill": evidence}}, "execution_authority_class": "provider_read_only", "execution_actor": "machine:propw-read-only"}, "operational_identity_link": dict(link)})
        return self.append(
            "entry_verified",
            {"draft_id": draft_id, "entry": {"instrument": instrument, "direction": direction, "actual_entry": actual_entry, "execution_at": execution_at}, "provider": {"provider": provider, "provider_lifecycle_id": provider_lifecycle_id, "source_ref": source_ref}, "identity_link_id": link["identity_link_id"], "execution_verification": verification},
            idempotency_key=key,
            occurred_at=execution_at,
            recorded_at=recorded_at,
        )

    def confirm_backtest_entry(
        self,
        draft_id: str,
        *,
        actual_entry: float,
        execution_at: str | None,
        source_ref: str,
        run_id: str | None = None,
        parameters_ref: str | None = None,
        dataset_ref: str | None = None,
        data_period: str | Mapping[str, str] | None = None,
        execution_metadata: Mapping[str, Any] | None = None,
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Confirm entry for a backtest sample via a simulated-execution claim.

        A backtest sample has no broker/provider execution to verify --
        there is deliberately no identity-link or provider step here.

        Two governed bases are available (see vocabularies.
        GOVERNED_BACKTEST_ENTRY_BASES and execution-verification.v1):

        * ``simulator_run`` -- a formal run/session exists. Pass ``run_id``,
          ``parameters_ref``, ``dataset_ref``, and ``data_period`` together
          (all four, or none).
        * ``manual_declared_execution`` -- the genuinely common single-sample
          case ("there was a setup, I didn't take it, count it as a
          backtest"): no run/session exists. Omit all four run-metadata
          arguments; nothing is invented in their place.

        Either way this is the backtest-shaped counterpart to
        :meth:`verify_entry`, so the resulting record can reach an OPEN,
        finalize-eligible state without inventing a second lifecycle. No
        provider/broker field is ever populated by this method.
        """

        draft = self._draft(draft_id)
        if draft.get("type") != "backtest":
            raise LifecycleError("confirm_backtest_entry requires a backtest draft")
        instrument = ((draft.get("identity") or {}).get("instrument") or {}).get("symbol")
        direction = (draft.get("identity") or {}).get("direction")
        if not instrument or direction not in {"long", "short"}:
            raise LifecycleError("draft is missing instrument or direction")
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise LifecycleError("confirm_backtest_entry requires source_ref")
        run_fields = {"run_id": run_id, "parameters_ref": parameters_ref, "dataset_ref": dataset_ref, "data_period": data_period}
        provided = {name: value for name, value in run_fields.items() if value is not None}
        if provided and len(provided) != len(run_fields):
            raise LifecycleError("run_id, parameters_ref, dataset_ref, and data_period must be provided together or not at all")
        has_run_metadata = len(provided) == len(run_fields)
        if has_run_metadata and not all(isinstance(item, str) and item.strip() for item in (run_id, parameters_ref, dataset_ref)):
            raise LifecycleError("run_id, parameters_ref, and dataset_ref must be non-empty strings")
        # A TradingView Replay chart is review/post_trade evidence whether or not its candle time
        # is known: a retrospective chart never becomes execution-time entry evidence. This MUST
        # NOT be used for simulator runs or LIVE. The relaxation rests on what the draft durably
        # recorded as its source, never on caller-supplied metadata alone (which could be forged).
        replay_chart_entry = (
            not has_run_metadata
            and isinstance(execution_metadata, Mapping)
            and execution_metadata.get("source_method") == "tradingview_replay"
            and (draft.get("operational_capture") or {}).get("source_method") == "tradingview_replay"
        )
        # Manual image-reconstructed BACKTEST is allowed to have an unknown candle time.
        chart_time_unknown = (
            replay_chart_entry
            and execution_at is None
            and execution_metadata.get("time_quality") == "not_observable"
            and execution_metadata.get("price_provenance") in {"chart_annotation", "chart_estimate"}
        )
        if not chart_time_unknown and not _iso_timestamp(execution_at):
            raise LifecycleError("execution_at must be an ISO timestamp, or honestly unknown for chart-derived Replay")
        key = idempotency_key or f"backtest-entry-confirmed:{draft_id}:{run_id or source_ref}"
        existing_event = self._event_by_idempotency(key, "backtest_entry_confirmed")
        if existing_event is not None:
            return existing_event
        self._require_state(draft_id, {"PRE_TRADE_DRAFT"}, "backtest entry confirmation requires a pre-trade draft")
        # Confirms the ref is genuinely attached entry evidence; deliberately
        # not gated on provider-shaped evidence -- there is no provider.
        resolved_evidence = self._resolve_evidence(
            draft_id, [source_ref],
            # A retrospective Replay cannot masquerade as execution-time
            # entry evidence. It stays review/post_trade even when the Agent
            # models a BACKTEST entry from its chart annotation.
            categories={"review", "entry"} if replay_chart_entry else {"entry"},
        )
        if replay_chart_entry and any(
            item.get("category") == "review" and "replay_chart" not in {item.get("type"), item.get("method")}
            for item in resolved_evidence
        ):
            raise LifecycleError("a Replay entry may rest on review evidence only when that evidence is the replay chart")
        side = "buy" if direction == "long" else "sell"
        if has_run_metadata:
            basis = "simulator_run"
            evidence: dict[str, Any] = {
                "evidence_kind": "simulator_run",
                "run_id": run_id,
                "source_ref": source_ref,
                "instrument": instrument,
                "side": side,
                "parameters_ref": parameters_ref,
                "dataset_ref": dataset_ref,
                "simulated_execution": True,
                "simulated_at": execution_at,
                "simulated_price": actual_entry,
                "data_period": data_period,
            }
            dedupe_source = "simulator_run_attestation"
        else:
            basis = "manual_declared_execution"
            evidence = {
                "evidence_kind": "manual_declared_execution",
                "source_ref": source_ref,
                "instrument": instrument,
                "side": side,
                "simulated_execution": True,
                "simulated_at": execution_at,
                "simulated_price": actual_entry,
                **({
                    "time_quality": "not_observable" if chart_time_unknown else execution_metadata.get("time_quality", "known"),
                    "price_provenance": execution_metadata.get("price_provenance"),
                    "chart_source_method": "tradingview_replay",
                    "actual_fill_verified": False,
                } if replay_chart_entry else {}),
            }
            dedupe_source = "manual_declared_execution_attestation"
        verification = {
            "schema_version": "execution-verification.v1",
            "entry": {"state": "confirmed", "basis": basis, "attestation_support": None, "evidence_refs": [source_ref], "adjudication_revision_ref": None},
            "exit": {"state": "not_applicable", "basis": "none", "attestation_support": None, "evidence_refs": [], "adjudication_revision_ref": None},
        }
        if recorded_at is not None:
            self._validate_recorded_order(recorded_at, self._read_events())
        self._update_draft(
            draft_id,
            {
                "classification_status": "resolved",
                "dedupe": {"status": "canonical", "canonical_source": dedupe_source, "run_id": run_id},
                "identity": {"direction": direction, "instrument": {"symbol": instrument}, "entry_at": execution_at},
                "actual": {
                    "entry_price": actual_entry,
                    "entry_at": execution_at,
                    "execution_status": "entered",
                    "position_status": "open",
                    "execution_verification": verification,
                    "execution_evidence": {"entry": {basis: evidence}},
                    "execution_authority_class": "deterministic_machine_rule",
                    "execution_actor": "machine:simulator-run" if has_run_metadata else "machine:manual-declared-execution",
                    **({"paper_execution": dict(execution_metadata)} if execution_metadata is not None else {}),
                },
            },
        )
        return self.append(
            "backtest_entry_confirmed",
            {
                "draft_id": draft_id,
                "entry": {"instrument": instrument, "direction": direction, "actual_entry": actual_entry, "execution_at": execution_at},
                "run_id": run_id,
                "execution_verification": verification,
                **({"paper_execution": dict(execution_metadata)} if execution_metadata is not None else {}),
            },
            idempotency_key=key,
            occurred_at=execution_at,
            recorded_at=recorded_at,
        )

    @staticmethod
    def _require_live_strategy_resolution(draft: Mapping[str, Any]) -> None:
        """Require valid attribution state while keeping it independent from T-ID allocation."""

        if draft.get("type") != "live":
            return  # BACKTEST / PAPER are not strategy-resolver gated
        resolution = draft.get("strategy_resolution")
        strategy = draft.get("strategy") if isinstance(draft.get("strategy"), Mapping) else {}
        try:
            valid = _validate_strategy_resolution(resolution) if isinstance(resolution, Mapping) else None
        except LifecycleError:
            valid = None
        ok = bool(valid) and valid.get("resolution_source") == RESOLVER_VERSION
        if ok and valid["status"] in {"no_match", "unresolved"}:
            null_identity = strategy.get("strategy_id") is None and strategy.get("rule_version") is None
            legacy_identity = (
                strategy.get("strategy_id") == HISTORICAL_UNRESOLVED_STRATEGY_ID
                and strategy.get("rule_version") == HISTORICAL_UNRESOLVED_RULE_VERSION
            )
            ok = null_identity or legacy_identity
        elif ok and valid["status"] in {"contemporaneous_confirmed", "replay_reconstructed"}:
            ok = valid["strategy_id"] == strategy.get("strategy_id") and valid["rule_version"] == strategy.get("rule_version")
        else:
            ok = False
        if not ok:
            raise LifecycleError("finalization of a LIVE draft requires a valid resolver-produced attribution state")

    def finalize(self, draft_id: str, reviewer: str = "owner", *, occurred_at: str | None = None, recorded_at: str | None = None, allow_legacy_compatibility: bool = False) -> dict[str, Any]:
        from .sequence import NumberAllocator

        existing_event = self._event_by_idempotency(f"finalized:{draft_id}", "canonical_finalized")
        if existing_event is not None:
            payload = existing_event["payload"]
            return {"trade_id": payload.get("trade_id"), "trade_no": payload.get("trade_no"), "event_id": existing_event["event_id"], "state": "OPEN"}
        lifecycle_record = self._projected_record(draft_id)
        legacy_compatibility = allow_legacy_compatibility and lifecycle_record is None
        if not legacy_compatibility:
            self._require_state(draft_id, {"OPEN"}, "finalization requires an entry-verified open lifecycle")
            self._require_live_strategy_resolution(self._draft(draft_id))
        if recorded_at is not None:
            self._validate_recorded_order(recorded_at, self._read_events())
        trade_id = self.writer.finalize_trade(draft_id, reviewer, NumberAllocator(self.root))
        path = self.writer.records / "trades" / f"{trade_id}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        event = self.append(
            "canonical_finalized",
            {"draft_id": draft_id, "trade_id": trade_id, "trade_no": record.get("trade_no"), "legacy_compatibility": legacy_compatibility},
            idempotency_key=f"finalized:{draft_id}",
            occurred_at=occurred_at or record.get("provenance", {}).get("finalized_at"),
            recorded_at=recorded_at,
        )
        return {"trade_id": trade_id, "trade_no": record.get("trade_no"), "event_id": event["event_id"], "state": "OPEN"}

    def partial_close(self, trade_id: str, *, exit_price: float, closed_at: str, quantity_fraction: float, provider_pnl: float | None = None, source_ref: str | None = None, recorded_at: str | None = None) -> dict[str, Any]:
        if not 0 < quantity_fraction < 1:
            raise LifecycleError("partial close quantity_fraction must be between 0 and 1")
        key = f"partial:{trade_id}:{closed_at}:{exit_price}"
        existing_event = self._event_by_idempotency(key, "partial_close")
        if existing_event is not None:
            return existing_event
        current = self.current()
        record = current.get("records", {}).get(trade_id)
        if not isinstance(record, Mapping) or record.get("state") not in {"OPEN", "PARTIALLY_CLOSED"}:
            raise LifecycleError("partial close requires an open canonical trade")
        return self.append("partial_close", {"trade_id": trade_id, "close": {"exit_price": exit_price, "closed_at": closed_at, "quantity_fraction": quantity_fraction, "source_ref": source_ref, "reported_provider_pnl": provider_pnl}, "provider_pnl": provider_pnl}, idempotency_key=key, occurred_at=closed_at, recorded_at=recorded_at)

    def final_close(self, trade_id: str, *, exit_price: float, closed_at: str, realized_r: float, provider_pnl: float | None = None, source_ref: str | None = None, recorded_at: str | None = None) -> dict[str, Any]:
        key = f"final-close:{trade_id}:{closed_at}:{exit_price}"
        existing_event = self._event_by_idempotency(key, "final_close")
        if existing_event is not None:
            return existing_event
        current = self.current()
        record = current.get("records", {}).get(trade_id)
        if not isinstance(record, Mapping) or record.get("state") not in {"OPEN", "PARTIALLY_CLOSED"}:
            raise LifecycleError("final close requires an open canonical trade")
        return self.append("final_close", {"trade_id": trade_id, "close": {"exit_price": exit_price, "closed_at": closed_at, "source_ref": source_ref}, "closed_at": closed_at, "realized_r": realized_r, "provider_pnl": provider_pnl, "exit_verification": {"state": "pending", "basis": "close_recorded", "evidence_refs": []}, "provider_pnl_verification": {"state": "pending" if provider_pnl is not None else "not_observable", "basis": "awaiting_provider_evidence", "evidence_refs": []}}, idempotency_key=key, occurred_at=closed_at, recorded_at=recorded_at)

    def verify_exit(
        self,
        trade_id: str,
        *,
        authority_class: str,
        evidence_refs: list[str],
        realized_r: float | None = None,
        provider_pnl: float | None = None,
        provider_pnl_authority: str | None = None,
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if authority_class not in EXIT_AUTHORITY_CLASSES:
            raise LifecycleError("exit authority must be deterministic_execution_calculation, owner_attestation, or governed_manual_adjudication")
        if not isinstance(evidence_refs, list) or not evidence_refs or any(not isinstance(item, str) or not item.strip() for item in evidence_refs):
            raise LifecycleError("verified exit requires evidence refs")
        key = idempotency_key or f"exit-verified:{trade_id}:{','.join(sorted(evidence_refs))}"
        existing_event = self._event_by_idempotency(key, "exit_verified")
        if existing_event is not None:
            return existing_event
        record = self._require_state(trade_id, {"CLOSED"}, "exit verification requires a recorded closed trade")
        exit_state = (record.get("exit_verification") or {}).get("state")
        if exit_state == "confirmed":
            raise LifecycleError("exit is already verified")
        if exit_state != "pending":
            raise LifecycleError("exit verification is not pending")
        resolved_evidence = self._resolve_evidence(trade_id if record.get("draft_id") is None else str(record["draft_id"]), evidence_refs, categories=OWNER_EXIT_EVIDENCE_CATEGORIES)
        evidence_bindings = [evidence_binding(item) for item in resolved_evidence]
        if authority_class != "owner_attestation" and any(item.get("category") != "exit" for item in resolved_evidence):
            raise LifecycleError("execution exit verification requires attached exit evidence")
        reported_r = record.get("reported_realized_r")
        if reported_r is None:
            raise LifecycleError("recorded realized R is unavailable")
        verified_r = reported_r if realized_r is None else realized_r
        if verified_r != reported_r:
            raise LifecycleError("verified realized R must match the recorded close value")
        reported_provider_pnl = record.get("reported_provider_pnl")
        verified_provider_pnl = None
        provider_state = {"state": "not_observable", "basis": "none", "evidence_refs": []}
        if reported_provider_pnl is not None:
            if provider_pnl is not None and provider_pnl != reported_provider_pnl:
                raise LifecycleError("verified provider P&L must match the recorded close value")
            if provider_pnl is not None:
                provider_evidence_refs = [
                    ref for ref, item in zip(evidence_refs, resolved_evidence)
                    if item.get("category") == "exit" and self._is_provider_evidence(item)
                ]
                if provider_pnl_authority == "provider_read_only":
                    if not provider_evidence_refs:
                        raise LifecycleError("provider_read_only P&L requires attached provider exit evidence")
                    provider_state = {"state": "confirmed", "basis": provider_pnl_authority, "evidence_refs": provider_evidence_refs}
                    verified_provider_pnl = provider_pnl
                elif provider_pnl_authority is not None:
                    raise LifecycleError("provider P&L authority is not an accepted provider authority")
                else:
                    # A reported monetary value may remain visible, but an
                    # owner/manual exit authority cannot turn it into provider
                    # truth without provider evidence.
                    provider_state = {"state": "pending", "basis": "provider evidence unavailable", "evidence_refs": []}
            else:
                provider_state = {"state": "pending", "basis": "provider evidence unavailable", "evidence_refs": []}
        verification_times = [record.get("closed_at")]
        verification_times.extend(
            item.get("captured_at")
            for item in record.get("evidence", [])
            if isinstance(item, Mapping) and item.get("category") == "exit"
        )
        parsed_times = []
        for value in verification_times:
            if not _iso_timestamp(value):
                continue
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed_times.append(parsed)
        occurred_at = ((max(parsed_times) if parsed_times else datetime.now(timezone.utc)) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        return self.append(
            "exit_verified",
            {
                "trade_id": trade_id,
                "realized_r": verified_r,
                "provider_pnl": verified_provider_pnl,
                "exit_verification": {
                    "state": "confirmed",
                    "basis": authority_class,
                    "evidence_refs": list(evidence_refs),
                    "evidence_bindings": evidence_bindings,
                },
                "provider_pnl_verification": provider_state,
            },
            idempotency_key=key,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
        )

    def review(
        self,
        trade_id: str,
        review: Mapping[str, Any],
        *,
        reviewed_at: str,
        owner_confirmation: Mapping[str, Any] | None = None,
        owner_authorization: Any = None,
        reviewer: str = "owner",
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        allowed = {"what_went_well", "what_went_wrong", "rule_compliance", "execution_deviation", "thesis_validity", "entry_quality", "exit_quality", "evidence_completeness", "owner_notes", "review_status", "diagnostic_class", "review_analysis"}
        if any(key not in allowed for key in review):
            raise LifecycleError("review contains unsupported or psychological-diagnosis fields")
        if "rule_violation" in review:
            raise LifecycleError("rule_compliance is canonical; rule_violation is derived")
        if review.get("rule_compliance") not in RULE_COMPLIANCE_VALUES:
            raise LifecycleError("rule_compliance must be compliant, violation, unknown, or not_observable")
        if review.get("diagnostic_class") is not None and review.get("diagnostic_class") not in {"METHOD", "EXECUTION", "PROCESS-RISK", "INSUFFICIENT", "MIXED"}:
            raise LifecycleError("diagnostic_class must be METHOD, EXECUTION, PROCESS-RISK, INSUFFICIENT, or MIXED")
        current = self.current()
        record = current.get("records", {}).get(trade_id)
        if not isinstance(record, Mapping) or record.get("state") not in {"CLOSED", "REVIEW_PENDING", "REVIEWED"}:
            raise LifecycleError("review requires a closed canonical trade")
        if (record.get("exit_verification") or {}).get("state") != "confirmed":
            raise LifecycleError("review requires a verified exit")
        try:
            from .review_authorization import ReviewAuthorizationError, verify_owner_review_authorization

            authorization_record = verify_owner_review_authorization(owner_authorization, owner_confirmation)
        except ReviewAuthorizationError as exc:
            raise LifecycleError(str(exc)) from exc
        review_key = f"reviewed:{trade_id}:{reviewed_at}"
        existing_event = self._event_by_idempotency(review_key, "reviewed")
        if existing_event is not None:
            existing_review = dict((existing_event.get("payload") or {}).get("review") or {})
            proposed = {
                **dict(review),
                "owner_confirmation": dict(owner_confirmation or {}),
                "owner_authorization": authorization_record,
            }
            for key in ("schema_version", "reviewer", "review_status"):
                existing_review.pop(key, None)
            for key in ("schema_version", "reviewer", "review_status"):
                proposed.pop(key, None)
            if existing_review != proposed:
                raise LifecycleError("idempotent review retry differs from the Owner-confirmed review")
            return existing_event
        if record.get("state") == "REVIEWED":
            raise LifecycleError("review is already completed")
        try:
            from .review_preview import ReviewPreviewError, verify_owner_confirmation

            verify_owner_confirmation(self.root, self, trade_id, review, owner_confirmation)
        except ReviewPreviewError as exc:
            raise LifecycleError(str(exc)) from exc
        normalized = {"schema_version": REVIEW_SCHEMA, **dict(review), "reviewer": reviewer}
        normalized["owner_confirmation"] = dict(owner_confirmation or {})
        normalized["owner_authorization"] = authorization_record
        if normalized.get("review_status") not in {None, "reviewed"}:
            raise LifecycleError("review_status must be reviewed")
        normalized["review_status"] = "reviewed"
        try:
            review_start = (datetime.fromisoformat(reviewed_at.replace("Z", "+00:00")) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        except ValueError as exc:
            raise LifecycleError("reviewed_at must be an ISO timestamp") from exc
        self.append("review_started", {"trade_id": trade_id}, idempotency_key=f"review-start:{trade_id}", occurred_at=review_start, recorded_at=recorded_at)
        return self.append("reviewed", {"trade_id": trade_id, "review": normalized, "reviewed_at": reviewed_at}, idempotency_key=review_key, occurred_at=reviewed_at, recorded_at=recorded_at)

    def resolve_strategy_attribution(
        self,
        trade_id: str,
        *,
        resolver_input: Mapping[str, Any],
        amendment_id: str,
        reason: str | None = None,
        resolved_at: str,
        reviewer: str = "owner",
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an existing LIVE trade's attribution through an append-only amendment.

        The frozen registration packet and finalized trade stay immutable. The
        resolver is replayed against the current registry, and every cited
        non-plan evidence ref must already be attached to this trade's lifecycle.
        """

        from .strategy_resolver import (
            OWNER_RETROSPECTIVE_AUTHORITY,
            resolve_strategy,
            to_lifecycle_resolution,
            validate_owner_attestation,
        )
        if not isinstance(resolver_input, Mapping):
            raise LifecycleError("strategy attribution amendment requires resolver_input")
        if not isinstance(amendment_id, str) or not amendment_id.strip():
            raise LifecycleError("amendment_id is required")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise LifecycleError("reviewer is required")
        if not _iso_timestamp(resolved_at):
            raise LifecycleError("resolved_at must be an ISO timestamp")
        if recorded_at is not None and not _iso_timestamp(recorded_at):
            raise LifecycleError("recorded_at must be an ISO timestamp")

        key = f"strategy-attribution:{trade_id}:{amendment_id.strip()}"
        existing_event = self._event_by_idempotency(key, "strategy_attribution_resolved")
        if existing_event is not None:
            return existing_event

        current = self._require_state(
            trade_id,
            {"OPEN", "PARTIALLY_CLOSED", "CLOSED", "REVIEW_PENDING", "REVIEWED"},
            "strategy attribution amendment requires an already-registered LIVE trade",
        )
        if current.get("trade_type") != "live":
            raise LifecycleError("strategy attribution amendment is LIVE-only")
        prior_resolution = current.get("strategy_resolution")
        if not isinstance(prior_resolution, Mapping) or prior_resolution.get("status") != "unresolved":
            raise LifecycleError("strategy attribution amendment requires unresolved attribution")

        entry = current.get("entry") if isinstance(current.get("entry"), Mapping) else {}
        draft_payload = self._draft(str(current.get("draft_id") or trade_id))
        # Amendments run after finalization, when the authoritative plan is
        # normally under `plan` in the CanonicalWriter record. The lifecycle
        # projection can expose it as `planned`; support either persisted
        # representation and never substitute a caller-supplied plan.
        planned = current.get("planned") if isinstance(current.get("planned"), Mapping) else None
        if planned is None:
            stored_plan = draft_payload.get("plan", draft_payload.get("planned"))
            planned = stored_plan if isinstance(stored_plan, Mapping) else {}
        plan_context = {
            "entry_price": planned.get("entry_price"),
            "stop_loss_price": planned.get("stop_loss_price"),
            "target_price": planned.get("target_price", planned.get("take_profit_price")),
            "planned_rr": planned.get("planned_rr", planned.get("rr_pretrade")),
        }
        retrospective_registry_digest_before = None
        if isinstance(resolver_input.get("owner_attestation"), Mapping):
            try:
                retrospective_registry_digest_before = historical_attribution_registry_digest(self.root)
            except ValueError as exc:
                raise LifecycleError(f"cannot pin retrospective attribution authority: {exc}") from exc
        result = resolve_strategy(
            self.root,
            resolver_input,
            provider_entry_at=entry.get("execution_at"),
            provider_execution_verified=True,
            plan_context=plan_context,
        )
        owner_attestation = result.get("owner_attestation")
        if not isinstance(reason, str) or not reason.strip():
            reason = owner_attestation.get("rationale") if isinstance(owner_attestation, Mapping) else None
        if not isinstance(reason, str) or not reason.strip():
            raise LifecycleError("amendment reason is required unless supplied by owner_attestation.rationale")
        if result.get("status") not in {"MATCHED", "NO_MATCH"} or not result.get("resolved"):
            conflicts = result.get("material_conflicts") or []
            if conflicts:
                detail = "; ".join(
                    f"{item.get('claim_id')} [{item.get('contradiction_code')}]: "
                    f"{item.get('observed_fact') or item.get('reason')}"
                    for item in conflicts if isinstance(item, Mapping)
                )
                raise LifecycleError(
                    "strategy attribution remains unresolved because of this material conflict; clarify this exact fact with the Owner: "
                    + detail
                )
            raise LifecycleError("strategy attribution amendment must resolve to MATCHED or NO_MATCH")
        resolution = _validate_strategy_resolution(
            to_lifecycle_resolution(result, contemporaneous=False)
        )

        cited_refs = {
            str(ref)
            for item in result.get("matrix", [])
            if isinstance(item, Mapping)
            for rule in item.get("rules", [])
            if isinstance(rule, Mapping)
            for ref in rule.get("evidence_refs", [])
            if isinstance(ref, str) and ref and not ref.startswith("plan:")
        }
        for owner_key in ("owner_no_match", "owner_setup_attribution", "owner_attestation"):
            owner_fact = resolver_input.get(owner_key)
            if isinstance(owner_fact, Mapping):
                cited_refs.update(
                    str(ref) for ref in owner_fact.get("evidence_refs", [])
                    if isinstance(ref, str) and ref.strip()
                )
        for item in resolver_input.get("retrospective_observations", []):
            if isinstance(item, Mapping):
                cited_refs.update(
                    str(ref) for ref in item.get("evidence_refs", [])
                    if isinstance(ref, str) and ref.strip()
                )
        owner_path = result.get("resolution_authority") == OWNER_RETROSPECTIVE_AUTHORITY
        retrospective_authority = None
        if owner_path:
            try:
                retrospective_registry_digest_after = historical_attribution_registry_digest(self.root)
            except ValueError as exc:
                raise LifecycleError(f"cannot pin retrospective attribution authority: {exc}") from exc
            if retrospective_registry_digest_before != retrospective_registry_digest_after:
                raise LifecycleError("retrospective attribution registry changed while resolving the amendment")
            retrospective_authority = {
                "strategy_id": result.get("strategy_id"),
                "rule_version": result.get("rule_version"),
                "retrospective_attribution_registry_version": RETROSPECTIVE_ATTRIBUTION_REGISTRY_VERSION,
                "retrospective_attribution_registry_digest": retrospective_registry_digest_before,
            }
        if not cited_refs and not owner_path:
            raise LifecycleError("strategy attribution amendment requires evidence refs from the resolver input")

        draft_id = str(current.get("draft_id") or trade_id)
        attached = {item.get("source_ref"): item for item in self._attached_evidence(draft_id)}
        owner_fact_refs = resolver_input.get("owner_attestation", {}).get("evidence_refs", []) if owner_path else []
        unbound_owner_refs = {
            ref for ref in owner_fact_refs
            if isinstance(ref, str) and ref.startswith("owner:")
        }
        missing = sorted(cited_refs - set(attached) - unbound_owner_refs)
        if missing:
            raise LifecycleError("strategy attribution evidence is not attached to the trade: " + ", ".join(missing))
        observation_refs = {
            str(ref): {
                "supports_fact_timing": item.get("supports_fact_timing", item.get("timing")),
            }
            for item in resolver_input.get("observations", [])
            if isinstance(item, Mapping)
            for ref in item.get("evidence_refs", [])
            if isinstance(ref, str) and ref.strip()
        }
        for ref in observation_refs:
            evidence = attached[ref]
            binding = observation_refs[ref]
            fact_timing = binding["supports_fact_timing"]
            expected_category = "pre_trade_context" if fact_timing == "pre_trade" else "entry" if fact_timing == "execution" else "review"
            if evidence.get("timing") != fact_timing or evidence.get("category") != expected_category:
                raise LifecycleError(
                    f"direct resolver observation fact timing must match attached evidence provenance: {ref}"
                )
        for key_name in ("owner_no_match", "owner_setup_attribution"):
            owner_fact = resolver_input.get(key_name)
            if isinstance(owner_fact, Mapping):
                for ref in owner_fact.get("evidence_refs", []):
                    if isinstance(ref, str) and ref.strip() and attached[ref].get("category") not in {"review", "pre_trade_context"}:
                        raise LifecycleError(
                            f"owner attribution evidence must be review or pre-trade context: {ref}"
                        )

        if owner_path:
            if not isinstance(owner_attestation, Mapping):
                raise LifecycleError("retrospective attribution result is missing its Owner attestation")
            try:
                normalized_owner = validate_owner_attestation(self.root, resolver_input.get("owner_attestation"))
            except ValueError as exc:
                raise LifecycleError(f"invalid retrospective Owner attestation: {exc}") from exc
            if normalized_owner != dict(owner_attestation):
                raise LifecycleError("retrospective Owner attestation differs from resolver result")
            for ref in owner_attestation.get("evidence_refs", []):
                if ref.startswith("owner:"):
                    continue  # The attestation itself is preserved in the immutable lifecycle amendment.
                evidence = attached.get(ref)
                if evidence is None:
                    raise LifecycleError(f"Owner attestation evidence is not attached to the trade: {ref}")
                expected_category = {
                    "pre_trade": "pre_trade_context",
                    "execution": "entry",
                    "post_trade": "review",
                }.get(evidence.get("timing"))
                if expected_category is not None and evidence.get("category") != expected_category:
                    raise LifecycleError(f"Owner evidence category/timing provenance does not match: {ref}")
            for item in result.get("retrospective_observations", []):
                timing = item.get("timing")
                expected_category = {
                    "pre_trade": "pre_trade_context",
                    "execution": "entry",
                    "post_trade": "review",
                }.get(timing)
                for ref in item.get("evidence_refs", []):
                    evidence = attached.get(ref)
                    if evidence is None:
                        raise LifecycleError(f"retrospective visual evidence is not attached to the trade: {ref}")
                    if evidence.get("timing") != timing or (
                        expected_category is not None and evidence.get("category") != expected_category
                    ):
                        raise LifecycleError(
                            f"retrospective visual evidence provenance does not match its observation: {ref}"
                        )
                    observed_at = item.get("observed_at")
                    if isinstance(observed_at, str):
                        represented = represented_market_time_contains(evidence, observed_at)
                        if represented is False:
                            raise LifecycleError(
                                f"observed chart fact time {observed_at} is outside the replay's represented market time: {ref}"
                            )
            retrospective_facts = resolution.get("retrospective_rule_facts")
            if isinstance(retrospective_facts, Mapping):
                if any(
                    retrospective_facts.get(key) != retrospective_authority.get(key)
                    for key in (
                        "strategy_id",
                        "rule_version",
                        "retrospective_attribution_registry_version",
                        "retrospective_attribution_registry_digest",
                    )
                ):
                    raise LifecycleError("retrospective rule-fact projection authority pin does not match its amendment")
                by_claim = {
                    item["claim_id"]: item
                    for item in result.get("retrospective_observations", [])
                    if isinstance(item, Mapping) and isinstance(item.get("claim_id"), str)
                }
                for fact in retrospective_facts.get("rules", []):
                    support_items = [by_claim.get(claim) for claim in fact["source_claim_ids"]]
                    if any(
                        not isinstance(item, Mapping)
                        or item.get("finding") != "corroborates"
                        or item.get("timing") != "post_trade"
                        for item in support_items
                    ):
                        raise LifecycleError("retrospective rule-fact projection requires corroborated post-trade observations")
                    exact_refs = {
                        ref for item in support_items for ref in item.get("evidence_refs", [])
                        if isinstance(ref, str)
                    }
                    if exact_refs != set(fact["evidence_refs"]):
                        raise LifecycleError("retrospective rule-fact projection evidence refs differ from its source observations")
                    for ref in fact["evidence_refs"]:
                        evidence = attached.get(ref)
                        if not (
                            isinstance(evidence, Mapping)
                            and evidence.get("timing") == "post_trade"
                            and evidence.get("category") == "review"
                            and evidence.get("type") == "replay_chart"
                            and evidence.get("role") == "historical_market_reconstruction"
                            and isinstance(evidence.get("provenance"), Mapping)
                            and evidence["provenance"].get("capture_mode") == "retrospective_replay"
                        ):
                            raise LifecycleError(f"retrospective rule-fact evidence must remain truthful replay/post_trade: {ref}")

        packet_sha256 = current.get("registration_packet_sha256")
        amendment = {
            "amendment_id": amendment_id.strip(),
            "reason": reason.strip(),
            "reviewer": reviewer.strip(),
            "resolver_input_sha256": _digest(dict(resolver_input)),
            "resolver_status": result["status"],
            "evidence_refs": sorted(cited_refs),
            **({"authority": retrospective_authority} if retrospective_authority is not None else {}),
            **(
                {
                    "owner_attestation": dict(owner_attestation),
                    "retrospective_observations": result.get("retrospective_observations", []),
                }
                if owner_path else {}
            ),
            **({"registration_packet_sha256": packet_sha256} if packet_sha256 else {}),
        }
        return self.append(
            "strategy_attribution_resolved",
            {
                "trade_id": trade_id,
                "strategy_id": resolution.get("strategy_id"),
                "rule_version": resolution.get("rule_version"),
                "strategy_resolution": resolution,
                "prior_strategy_resolution": dict(prior_resolution),
                "evidence_refs": sorted(cited_refs),
                "resolver_input_sha256": amendment["resolver_input_sha256"],
                "amendment": amendment,
            },
            idempotency_key=key,
            occurred_at=resolved_at,
            recorded_at=recorded_at,
        )

    def correct_review(
        self,
        trade_id: str,
        review_patch: Mapping[str, Any],
        strategy_resolution: Mapping[str, Any],
        *,
        corrected_at: str,
        correction_id: str,
        reviewer: str = "owner",
        recorded_at: str | None = None,
        correction_reason: str = "V2 delta audit correction",
    ) -> dict[str, Any]:
        """Correct only review interpretation and strategy provenance.

        This is the governed path for already-reviewed historical LIVE trades.
        It intentionally cannot mutate provider execution, T-ID, identity
        linkage, or evidence.  The compatibility strategy placeholders remain
        in the canonical ``strategy`` object; the V2 resolution object records
        the actual epistemic result separately.
        """

        allowed = {"what_went_well", "what_went_wrong", "rule_compliance", "execution_deviation", "thesis_validity", "entry_quality", "exit_quality", "evidence_completeness", "owner_notes", "review_status", "diagnostic_class"}
        if not isinstance(review_patch, Mapping) or any(key not in allowed for key in review_patch):
            raise LifecycleError("review correction contains unsupported fields")
        if "rule_violation" in review_patch:
            raise LifecycleError("rule_compliance is canonical; rule_violation is derived")
        resolution = _validate_strategy_resolution(strategy_resolution)
        if not isinstance(correction_id, str) or not correction_id.strip():
            raise LifecycleError("correction_id is required")
        if not isinstance(correction_reason, str) or not correction_reason.strip():
            raise LifecycleError("correction_reason is required")
        if not _iso_timestamp(corrected_at):
            raise LifecycleError("corrected_at must be an ISO timestamp")
        current = self.current()
        record = current.get("records", {}).get(trade_id)
        if not isinstance(record, Mapping) or record.get("state") != "REVIEWED":
            raise LifecycleError("review correction requires an already-reviewed canonical trade")
        if (record.get("exit_verification") or {}).get("state") != "confirmed":
            raise LifecycleError("review correction requires a verified exit")
        key = f"review-corrected:{trade_id}:{correction_id}"
        existing_event = self._event_by_idempotency(key, "review_corrected")
        if existing_event is not None:
            return existing_event
        existing_review = record.get("review") if isinstance(record.get("review"), Mapping) else {}
        normalized = {"schema_version": REVIEW_SCHEMA, **dict(existing_review), **dict(review_patch), "reviewer": reviewer, "review_status": "reviewed"}
        if normalized.get("rule_compliance") not in RULE_COMPLIANCE_VALUES:
            raise LifecycleError("corrected review requires a supported rule_compliance")
        if normalized.get("diagnostic_class") is not None and normalized.get("diagnostic_class") not in {"METHOD", "EXECUTION", "PROCESS-RISK", "INSUFFICIENT", "MIXED"}:
            raise LifecycleError("diagnostic_class must be METHOD, EXECUTION, PROCESS-RISK, INSUFFICIENT, or MIXED")
        if self.writer is not None:
            self.writer.apply_review_correction(
                trade_id,
                review_patch=dict(review_patch),
                strategy_resolution=resolution,
                correction_id=correction_id,
                reviewer=reviewer,
                recorded_at=recorded_at or _now(),
            )
        return self.append(
            "review_corrected",
            {
                "trade_id": trade_id,
                "review": normalized,
                "strategy_resolution": resolution,
                "correction": {
                    "correction_id": correction_id,
                    "reason": correction_reason,
                    "reviewer": reviewer,
                },
                "corrected_at": corrected_at,
            },
            idempotency_key=key,
            occurred_at=corrected_at,
            recorded_at=recorded_at,
        )
