"""Execution-verification schema and fail-closed policy helpers.

This module validates the Phase 4B.1 *claim* contract.  It does not read the
historical corpus, create adjudication rows, allocate lifecycle identifiers, or
write canonical records.  The normalizer's older scalar compatibility fields
remain outside this contract; callers that adopt the new schema must consume
the entry/exit ``(state, basis)`` pair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
from typing import Any, Iterable, Mapping

from .adjudication import RuleContractError, validate_durable_revision


EXECUTION_VERIFICATION_SCHEMA_VERSION = "execution-verification.v1"

EXECUTION_STATES = frozenset(
    {"not_applicable", "unverified", "confirmed", "contradicted"}
)
EXECUTION_BASES = frozenset(
    {
        "none",
        "external_fill",
        "human_attestation",
        "simulator_run",
        "declared_simulated",
        # Unified Capture's minimal single-sample BACKTEST basis (see
        # trading_os.operational_lifecycle.confirm_backtest_entry): a genuine
        # simulated-execution claim with no formal run_id/parameters_ref/
        # dataset_ref/data_period. Distinct from the reserved
        # "declared_simulated" placeholder above -- this basis is real and
        # writable, just deliberately minimal.
        "manual_declared_execution",
    }
)
ATTESTATION_SUPPORT = frozenset({"artifact_supported", "memory_only"})
RECORD_TYPES = frozenset({"live", "backtest"})
HUMAN_AUTHORITY = "human_adjudication_required"
MACHINE_AUTHORITY = "deterministic_machine_rule"

VERIFICATION_FIELDS = frozenset(
    {
        "state",
        "basis",
        "attestation_support",
        "evidence_refs",
        "adjudication_revision_ref",
    }
)
VERIFICATION_CONTAINER_FIELDS = frozenset({"schema_version", "entry", "exit"})
ATTESTATION_DECISION_DOMAIN = "execution"
ATTESTATION_DECISION_TYPE = "execution_verification_claim"
ATTESTATION_CLAIM_FIELDS = frozenset(
    {"leg", "state", "basis", "attestation_support", "evidence_refs"}
)
FINAL_REVISION_STATUSES = frozenset({"resolved", "final"})
RESULT_METRICS = frozenset(
    {
        "trade_count",
        "outcome_counts",
        "realized_r",
        "expectancy_r",
        "median_r",
        "profit_factor",
    }
)


class ExecutionVerificationError(ValueError):
    """Raised by the strict assertion helper for an illegal contract."""


@dataclass
class ExecutionVerificationReport:
    """Machine-readable validation result with stable reason codes."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "ExecutionVerificationReport", prefix: str = "") -> None:
        self.errors.extend(prefix + error for error in other.errors)
        self.warnings.extend(prefix + warning for warning in other.warnings)

    def require_valid(self) -> None:
        if self.errors:
            raise ExecutionVerificationError("; ".join(self.errors))


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _positive_number(value: Any) -> bool:
    return _finite_number(value) and float(value) > 0


def _valid_iso_datetime(value: Any) -> bool:
    if not _nonempty_text(value):
        return False
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _same_text(left: Any, right: Any) -> bool:
    """Compare canonical text literally apart from case/edge whitespace.

    This is intentionally not symbol aliasing, fuzzy matching, or a tolerance.
    """

    return _nonempty_text(left) and _nonempty_text(right) and left.strip().casefold() == right.strip().casefold()


def _same_number(left: Any, right: Any) -> bool:
    """Compare finite numeric facts exactly, without a price tolerance."""

    if not _finite_number(left) or not _finite_number(right):
        return False
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError):
        return False


def _same_datetime(left: Any, right: Any) -> bool:
    """Compare ISO timestamps as exact instants; naive/aware values do not mix."""

    if not _valid_iso_datetime(left) or not _valid_iso_datetime(right):
        return False
    left_value = datetime.fromisoformat(left.strip().replace("Z", "+00:00"))
    right_value = datetime.fromisoformat(right.strip().replace("Z", "+00:00"))
    if (left_value.tzinfo is None) != (right_value.tzinfo is None):
        return False
    return left_value == right_value


def _human_authority(authority_class: str | None, actor: str | None) -> bool:
    if actor and actor.startswith("human:"):
        return True
    return authority_class in {HUMAN_AUTHORITY, "human"}


def _machine_authority(authority_class: str | None, actor: str | None) -> bool:
    if actor and actor.startswith("machine:"):
        return True
    return authority_class == MACHINE_AUTHORITY


def _evidence_for(
    execution_evidence: Mapping[str, Any] | None,
    leg_name: str,
    basis: str,
) -> Mapping[str, Any] | None:
    """Return the structured evidence object for one leg and one basis."""

    if not isinstance(execution_evidence, Mapping):
        return None
    leg = execution_evidence.get(leg_name)
    if not isinstance(leg, Mapping):
        return None
    value = leg.get(basis)
    return value if isinstance(value, Mapping) else None


def _identifier_values(value: Any) -> set[str]:
    if _nonempty_text(value):
        return {value.strip()}
    if isinstance(value, (list, tuple, set)):
        return {item.strip() for item in value if _nonempty_text(item)}
    return set()


def _claim_identifiers(actual: Mapping[str, Any], leg_name: str) -> dict[str, set[str]]:
    """Collect optional record-side IDs without deriving any new identity.

    The normalizer does not currently backfill these fields.  They are only
    compared when an already-recorded claim fact and a structured evidence fact
    are both present.
    """

    values = {"order_id": set(), "fill_id": set(), "run_id": set()}
    for key in values:
        values[key].update(_identifier_values(actual.get(f"{leg_name}_{key}")))

    execution_identity = actual.get("execution_identity")
    if isinstance(execution_identity, Mapping):
        leg_identity = execution_identity.get(leg_name)
        if isinstance(leg_identity, Mapping):
            for key in values:
                values[key].update(_identifier_values(leg_identity.get(key)))

    detail_rows = actual.get("fills") if leg_name == "entry" else actual.get("exit_legs")
    if isinstance(detail_rows, list):
        for row in detail_rows:
            if isinstance(row, Mapping):
                for key in ("order_id", "fill_id"):
                    values[key].update(_identifier_values(row.get(key)))
    return values


def _claim_context_from_record(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Derive immutable comparison facts from the record's own claim fields."""

    actual = record.get("actual") if isinstance(record.get("actual"), Mapping) else {}
    identity = record.get("identity") if isinstance(record.get("identity"), Mapping) else {}
    instrument = identity.get("instrument") if isinstance(identity.get("instrument"), Mapping) else {}
    entry_ids = _claim_identifiers(actual, "entry")
    exit_ids = _claim_identifiers(actual, "exit")
    return {
        "entry": {
            "instrument": instrument.get("symbol"),
            "direction": identity.get("direction"),
            "execution_at": identity.get("entry_at"),
            "price": actual.get("entry_price"),
            **entry_ids,
        },
        "exit": {
            "instrument": instrument.get("symbol"),
            "direction": identity.get("direction"),
            "execution_at": actual.get("exit_at"),
            "price": actual.get("exit_price"),
            **exit_ids,
        },
    }


def _side_matches_direction(side: str, direction: str, leg_name: str) -> bool:
    """Bind a position direction or order side to the stated entry/exit leg."""

    if side in {"long", "short"}:
        return side == direction
    if direction not in {"long", "short"}:
        return False
    expected_order_side = "buy" if (leg_name == "entry") == (direction == "long") else "sell"
    return side == expected_order_side


def _claim_for(
    claim_context: Mapping[str, Any] | None,
    leg_name: str,
) -> Mapping[str, Any] | None:
    if not isinstance(claim_context, Mapping):
        return None
    value = claim_context.get(leg_name)
    return value if isinstance(value, Mapping) else None


def _validate_claim_binding(
    evidence: Mapping[str, Any],
    claim: Mapping[str, Any] | None,
    report: ExecutionVerificationReport,
    *,
    leg_name: str,
    prefix: str,
    evidence_time_key: str,
    evidence_price_key: str,
    identity_keys: tuple[str, ...],
) -> None:
    """Fail closed unless structured evidence matches the same record claim."""

    if claim is None:
        report.errors.append(f"{prefix}_record_claim_context_required")
        return

    instrument = claim.get("instrument")
    if not _nonempty_text(instrument):
        report.errors.append(f"{prefix}_record_claim_instrument_required")
    elif not _same_text(evidence.get("instrument"), instrument):
        report.errors.append(f"{prefix}_instrument_conflicts_record_claim")

    direction = claim.get("direction")
    if direction not in {"long", "short"}:
        report.errors.append(f"{prefix}_record_claim_direction_required")
    else:
        side = str(evidence.get("side", "")).strip().lower()
        if not _side_matches_direction(side, direction, leg_name):
            report.errors.append(f"{prefix}_side_conflicts_record_claim")

    record_time = claim.get("execution_at")
    evidence_time = evidence.get(evidence_time_key)
    if record_time is not None and evidence_time is not None:
        if not _valid_iso_datetime(record_time):
            report.errors.append(f"{prefix}_record_claim_execution_at_invalid")
        elif not _same_datetime(evidence_time, record_time):
            report.errors.append(f"{prefix}_execution_at_conflicts_record_claim")

    record_price = claim.get("price")
    evidence_price = evidence.get(evidence_price_key)
    if record_price is not None and evidence_price is not None:
        if not _positive_number(record_price):
            report.errors.append(f"{prefix}_record_claim_price_invalid")
        elif not _same_number(evidence_price, record_price):
            report.errors.append(f"{prefix}_price_conflicts_record_claim")

    for key in identity_keys:
        evidence_identifier = evidence.get(key)
        record_identifiers = _identifier_values(claim.get(key))
        if _nonempty_text(evidence_identifier) and record_identifiers:
            if not any(_same_text(evidence_identifier, value) for value in record_identifiers):
                report.errors.append(f"{prefix}_{key}_conflicts_record_claim")


def _validate_external_fill(
    evidence: Mapping[str, Any] | None,
    evidence_refs: list[str],
    claim_context: Mapping[str, Any] | None,
    report: ExecutionVerificationReport,
    leg_name: str,
) -> None:
    prefix = f"{leg_name}.external_fill"
    if evidence is None:
        report.errors.append(f"{prefix}_predicate_missing")
        return
    if evidence.get("evidence_kind") != "external_fill":
        report.errors.append(f"{prefix}_evidence_kind_invalid")
    for key in ("source_ref", "instrument", "side"):
        if not _nonempty_text(evidence.get(key)):
            report.errors.append(f"{prefix}_{key}_required")
    side = str(evidence.get("side", "")).strip().lower()
    if side not in {"buy", "sell", "long", "short"}:
        report.errors.append(f"{prefix}_side_invalid")

    has_timestamp = evidence.get("execution_at") is not None
    has_price = evidence.get("price") is not None
    has_order_identity = any(_nonempty_text(evidence.get(key)) for key in ("order_id", "fill_id"))
    if has_timestamp != has_price:
        report.errors.append(f"{prefix}_execution_at_price_must_be_paired")
    if has_timestamp and not _valid_iso_datetime(evidence.get("execution_at")):
        report.errors.append(f"{prefix}_execution_at_invalid")
    if has_price and not _positive_number(evidence.get("price")):
        report.errors.append(f"{prefix}_price_invalid")
    if not ((has_timestamp and has_price) or has_order_identity):
        report.errors.append(f"{prefix}_match_key_required")

    source_ref = evidence.get("source_ref")
    if _nonempty_text(source_ref) and source_ref not in evidence_refs:
        report.errors.append(f"{prefix}_source_ref_not_listed")
    _validate_claim_binding(
        evidence,
        claim_context,
        report,
        leg_name=leg_name,
        prefix=prefix,
        evidence_time_key="execution_at",
        evidence_price_key="price",
        identity_keys=("order_id", "fill_id"),
    )


def _valid_data_period(value: Any) -> bool:
    if _nonempty_text(value):
        return True
    if not isinstance(value, Mapping):
        return False
    return _nonempty_text(value.get("from")) and _nonempty_text(value.get("to"))


def _validate_simulator_run(
    evidence: Mapping[str, Any] | None,
    evidence_refs: list[str],
    claim_context: Mapping[str, Any] | None,
    report: ExecutionVerificationReport,
    leg_name: str,
) -> None:
    prefix = f"{leg_name}.simulator_run"
    if evidence is None:
        report.errors.append(f"{prefix}_predicate_missing")
        return
    if evidence.get("evidence_kind") != "simulator_run":
        report.errors.append(f"{prefix}_evidence_kind_invalid")
    required_text = (
        "run_id",
        "source_ref",
        "instrument",
        "side",
        "parameters_ref",
        "dataset_ref",
    )
    for key in required_text:
        if not _nonempty_text(evidence.get(key)):
            report.errors.append(f"{prefix}_{key}_required")
    if evidence.get("simulated_execution") is not True:
        report.errors.append(f"{prefix}_simulated_execution_claim_required")
    if not _valid_iso_datetime(evidence.get("simulated_at")):
        report.errors.append(f"{prefix}_simulated_at_invalid")
    if not _positive_number(evidence.get("simulated_price")):
        report.errors.append(f"{prefix}_simulated_price_invalid")
    if not _valid_data_period(evidence.get("data_period")):
        report.errors.append(f"{prefix}_data_period_required")
    side = str(evidence.get("side", "")).strip().lower()
    if side not in {"buy", "sell", "long", "short"}:
        report.errors.append(f"{prefix}_side_invalid")
    source_ref = evidence.get("source_ref")
    if _nonempty_text(source_ref) and source_ref not in evidence_refs:
        report.errors.append(f"{prefix}_source_ref_not_listed")
    _validate_claim_binding(
        evidence,
        claim_context,
        report,
        leg_name=leg_name,
        prefix=prefix,
        evidence_time_key="simulated_at",
        evidence_price_key="simulated_price",
        identity_keys=("run_id",),
    )


def _validate_manual_declared_execution(
    evidence: Mapping[str, Any] | None,
    evidence_refs: list[str],
    claim_context: Mapping[str, Any] | None,
    report: ExecutionVerificationReport,
    leg_name: str,
) -> None:
    """Minimal single-sample BACKTEST entry claim -- no run_id/parameters_ref/
    dataset_ref/data_period. Otherwise exact-match evidence-kind and
    claim-binding rules identical to :func:`_validate_simulator_run`."""

    prefix = f"{leg_name}.manual_declared_execution"
    if evidence is None:
        report.errors.append(f"{prefix}_predicate_missing")
        return
    if evidence.get("evidence_kind") != "manual_declared_execution":
        report.errors.append(f"{prefix}_evidence_kind_invalid")
    for key in ("source_ref", "instrument", "side"):
        if not _nonempty_text(evidence.get(key)):
            report.errors.append(f"{prefix}_{key}_required")
    if evidence.get("simulated_execution") is not True:
        report.errors.append(f"{prefix}_simulated_execution_claim_required")
    # Manual chart-based Replay is a modelled entry, not a time-stamped
    # simulator fill. Unknown time is allowed only when the exact provenance
    # explicitly identifies a chart-derived price and non-verified fill.
    time_unknown = (
        evidence.get("simulated_at") is None
        and evidence.get("time_quality") == "not_observable"
        and evidence.get("price_provenance") in {"chart_annotation", "chart_estimate"}
        and evidence.get("chart_source_method") == "tradingview_replay"
        and evidence.get("actual_fill_verified") is False
    )
    if not time_unknown and not _valid_iso_datetime(evidence.get("simulated_at")):
        report.errors.append(f"{prefix}_simulated_at_invalid")
    if not _positive_number(evidence.get("simulated_price")):
        report.errors.append(f"{prefix}_simulated_price_invalid")
    side = str(evidence.get("side", "")).strip().lower()
    if side not in {"buy", "sell", "long", "short"}:
        report.errors.append(f"{prefix}_side_invalid")
    source_ref = evidence.get("source_ref")
    if _nonempty_text(source_ref) and source_ref not in evidence_refs:
        report.errors.append(f"{prefix}_source_ref_not_listed")
    _validate_claim_binding(
        evidence,
        claim_context,
        report,
        leg_name=leg_name,
        prefix=prefix,
        evidence_time_key="simulated_at",
        evidence_price_key="simulated_price",
        identity_keys=(),
    )


def _revision_index(
    durable_revisions: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | Any | None,
) -> tuple[dict[str, Mapping[str, Any]], set[str]]:
    """Accept a durable-revision index, iterable, or read-only log resolver."""

    if durable_revisions is None:
        return {}, set()
    source = durable_revisions
    iterator = getattr(source, "iter_revisions", None)
    if callable(iterator):
        source = iterator()
    if isinstance(source, Mapping):
        values: Iterable[Any]
        if "revision_id" in source:
            values = [source]
        else:
            values = source.values()
    elif isinstance(source, Iterable) and not isinstance(source, (str, bytes)):
        values = source
    else:
        return {}, set()

    index: dict[str, Mapping[str, Any]] = {}
    duplicates: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping):
            continue
        revision_id = item.get("revision_id")
        if not _nonempty_text(revision_id):
            continue
        if revision_id in index:
            duplicates.add(revision_id)
            continue
        index[revision_id] = item
    return index, duplicates


def _validate_human_attestation_revision(
    *,
    revision_ref: str | None,
    durable_revisions: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | Any | None,
    record_id: str | None,
    leg_name: str,
    claim: Mapping[str, Any],
    report: ExecutionVerificationReport,
) -> None:
    """Bind a confirmed human claim to one real durable revision.

    A non-empty string is never authority.  The referenced durable row must be
    valid, human-owned, single-record, and contain the exact claim/evidence
    payload being consumed.
    """

    prefix = f"{leg_name}.execution_verification"
    if not _nonempty_text(revision_ref):
        report.errors.append(f"{prefix}_human_confirmation_requires_revision")
        return
    if not _nonempty_text(record_id):
        report.errors.append(f"{prefix}_human_attestation_record_id_required")
        return
    revisions, duplicate_ids = _revision_index(durable_revisions)
    if revision_ref in duplicate_ids:
        report.errors.append(f"{prefix}_human_attestation_revision_ambiguous")
        return
    revision = revisions.get(revision_ref)
    if revision is None:
        report.errors.append(f"{prefix}_human_attestation_revision_not_found")
        return
    try:
        validate_durable_revision(revision)
    except (RuleContractError, TypeError, ValueError):
        report.errors.append(f"{prefix}_human_attestation_revision_invalid")
        return

    if revision.get("authority_class") != HUMAN_AUTHORITY or not str(revision.get("actor", "")).startswith("human:"):
        report.errors.append(f"{prefix}_human_attestation_revision_requires_human_authority")
    if revision.get("decision_domain") != ATTESTATION_DECISION_DOMAIN:
        report.errors.append(f"{prefix}_human_attestation_revision_domain_mismatch")
    if revision.get("decision_type") != ATTESTATION_DECISION_TYPE:
        report.errors.append(f"{prefix}_human_attestation_revision_type_mismatch")
    if revision.get("status") not in FINAL_REVISION_STATUSES:
        report.errors.append(f"{prefix}_human_attestation_revision_not_final")
    if revision.get("bulk") or revision.get("is_bulk"):
        report.errors.append(f"{prefix}_human_attestation_revision_must_be_single_record")
    if revision.get("record_ids") != [record_id]:
        report.errors.append(f"{prefix}_human_attestation_revision_must_be_single_record")

    after = revision.get("after")
    durable_claim = after.get("execution_claim") if isinstance(after, Mapping) else None
    if not isinstance(durable_claim, Mapping) or set(durable_claim) != ATTESTATION_CLAIM_FIELDS:
        report.errors.append(f"{prefix}_human_attestation_revision_claim_required")
        return
    if durable_claim.get("leg") != leg_name:
        report.errors.append(f"{prefix}_human_attestation_revision_leg_mismatch")
    for key in ("state", "basis", "attestation_support"):
        if durable_claim.get(key) != claim.get(key):
            report.errors.append(f"{prefix}_human_attestation_revision_claim_mismatch")
            break
    durable_refs = durable_claim.get("evidence_refs")
    claim_refs = claim.get("evidence_refs")
    revision_refs = revision.get("evidence_refs")
    if (
        not isinstance(durable_refs, list)
        or not all(_nonempty_text(item) for item in durable_refs)
        or durable_refs != claim_refs
    ):
        report.errors.append(f"{prefix}_human_attestation_revision_evidence_mismatch")
    elif not isinstance(revision_refs, list) or not all(ref in revision_refs for ref in durable_refs):
        report.errors.append(f"{prefix}_human_attestation_revision_provenance_mismatch")


def _validate_leg(
    leg_name: str,
    value: Any,
    *,
    record_id: str | None,
    record_kind: str | None,
    record_type: str | None,
    classification_status: str | None,
    claim_context: Mapping[str, Any] | None,
    durable_revisions: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | Any | None,
    execution_evidence: Mapping[str, Any] | None,
    authority_class: str | None,
    actor: str | None,
    report: ExecutionVerificationReport,
) -> dict[str, Any] | None:
    prefix = f"{leg_name}.execution_verification"
    if not isinstance(value, Mapping):
        report.errors.append(f"{prefix}_object_required")
        return None
    missing = sorted(VERIFICATION_FIELDS - set(value))
    if missing:
        report.errors.append(f"{prefix}_missing_fields:{','.join(missing)}")
    unknown = sorted(set(value) - VERIFICATION_FIELDS)
    if unknown:
        report.errors.append(f"{prefix}_unknown_fields:{','.join(unknown)}")
    if missing:
        return dict(value)

    state = value.get("state")
    basis = value.get("basis")
    support = value.get("attestation_support")
    evidence_refs = value.get("evidence_refs")
    revision_ref = value.get("adjudication_revision_ref")

    if state not in EXECUTION_STATES:
        report.errors.append(f"{prefix}_state_invalid")
    if basis not in EXECUTION_BASES:
        report.errors.append(f"{prefix}_basis_invalid")
    if support is not None and support not in ATTESTATION_SUPPORT:
        report.errors.append(f"{prefix}_attestation_support_invalid")
    if not isinstance(evidence_refs, list) or not all(_nonempty_text(item) for item in evidence_refs):
        report.errors.append(f"{prefix}_evidence_refs_must_be_string_list")
        evidence_refs = []
    if revision_ref is not None and not _nonempty_text(revision_ref):
        report.errors.append(f"{prefix}_adjudication_revision_ref_invalid")
        revision_ref = None

    if basis != "human_attestation" and support is not None:
        report.errors.append(f"{prefix}_support_only_allowed_for_human_attestation")
    if basis == "declared_simulated":
        report.errors.append(f"{prefix}_declared_simulated_reserved")
    if basis == "human_attestation":
        if support is None:
            report.errors.append(f"{prefix}_human_attestation_support_required")
        if _machine_authority(authority_class, actor):
            report.errors.append(f"{prefix}_machine_authored_human_attestation_forbidden")
        elif not (state == "confirmed" and support == "artifact_supported") and not _human_authority(authority_class, actor):
            report.errors.append(f"{prefix}_human_attestation_requires_human_authority")
        if support == "artifact_supported" and not evidence_refs:
            report.errors.append(f"{prefix}_artifact_support_requires_evidence_refs")

    claim = {
        "state": state,
        "basis": basis,
        "attestation_support": support,
        "evidence_refs": evidence_refs,
    }
    if state == "not_applicable":
        if basis != "none":
            report.errors.append(f"{prefix}_not_applicable_requires_basis_none")
        if support is not None:
            report.errors.append(f"{prefix}_not_applicable_requires_support_null")
    elif state == "confirmed":
        if basis == "none":
            report.errors.append(f"{prefix}_confirmed_requires_non_none_basis")
        if basis == "human_attestation":
            if support == "memory_only":
                report.errors.append(f"{prefix}_memory_only_cannot_confirm")
            if support == "artifact_supported":
                _validate_human_attestation_revision(
                    revision_ref=revision_ref,
                    durable_revisions=durable_revisions,
                    record_id=record_id,
                    leg_name=leg_name,
                    claim=claim,
                    report=report,
                )
        elif basis == "external_fill":
            if record_kind != "trade":
                report.errors.append(f"{prefix}_external_fill_requires_trade_record")
            if record_type != "live" or classification_status != "resolved":
                report.errors.append(f"{prefix}_external_fill_requires_resolved_live_mode")
            _validate_external_fill(
                _evidence_for(execution_evidence, leg_name, basis),
                evidence_refs,
                _claim_for(claim_context, leg_name),
                report,
                leg_name,
            )
        elif basis == "simulator_run":
            if record_kind != "trade":
                report.errors.append(f"{prefix}_simulator_run_requires_trade_record")
            if record_type != "backtest" or classification_status != "resolved":
                report.errors.append(f"{prefix}_simulator_run_requires_resolved_backtest_mode")
            _validate_simulator_run(
                _evidence_for(execution_evidence, leg_name, basis),
                evidence_refs,
                _claim_for(claim_context, leg_name),
                report,
                leg_name,
            )
        elif basis == "manual_declared_execution":
            if record_kind != "trade":
                report.errors.append(f"{prefix}_manual_declared_execution_requires_trade_record")
            if record_type != "backtest" or classification_status != "resolved":
                report.errors.append(f"{prefix}_manual_declared_execution_requires_resolved_backtest_mode")
            _validate_manual_declared_execution(
                _evidence_for(execution_evidence, leg_name, basis),
                evidence_refs,
                _claim_for(claim_context, leg_name),
                report,
                leg_name,
            )
    elif state == "contradicted":
        if basis == "none":
            report.errors.append(f"{prefix}_contradicted_requires_non_none_basis")
        if not evidence_refs and revision_ref is None:
            report.errors.append(f"{prefix}_contradiction_requires_counter_evidence_or_revision")

    return {
        **claim,
        "adjudication_revision_ref": revision_ref,
    }


def validate_execution_verification(
    value: Mapping[str, Any] | Any,
    *,
    record_id: str | None = None,
    record_kind: str | None = None,
    record_type: str | None = None,
    classification_status: str | None = None,
    execution_status: str | None = None,
    position_status: str | None = None,
    claim_context: Mapping[str, Any] | None = None,
    durable_revisions: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | Any | None = None,
    execution_evidence: Mapping[str, Any] | None = None,
    authority_class: str | None = None,
    actor: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> ExecutionVerificationReport:
    """Validate the canonical entry/exit verification object.

    A confirmed deterministic predicate receives a per-leg ``claim_context``
    from the record it is meant to verify.  A confirmed human attestation
    receives a durable-revision index or a read-only ``AdjudicationLog``.  Both
    inputs fail closed when omitted.
    """

    if context is not None:
        record_id = context.get("record_id", record_id)
        record_kind = context.get("record_kind", record_kind)
        record_type = context.get("type", record_type)
        classification_status = context.get("classification_status", classification_status)
        execution_status = context.get("execution_status", execution_status)
        position_status = context.get("position_status", position_status)
        claim_context = context.get("claim_context", claim_context)
        durable_revisions = context.get("durable_revisions", durable_revisions)
        execution_evidence = context.get("execution_evidence", execution_evidence)
        authority_class = context.get("authority_class", authority_class)
        actor = context.get("actor", actor)

    report = ExecutionVerificationReport()
    if not isinstance(value, Mapping):
        report.errors.append("execution_verification_object_required")
        return report
    if record_type is not None and record_type not in RECORD_TYPES:
        report.errors.append("execution_verification_record_type_invalid")
    if record_kind == "observation" and record_type is not None:
        report.errors.append("observation_execution_verification_type_must_be_null")
    if value.get("schema_version") not in {None, EXECUTION_VERIFICATION_SCHEMA_VERSION}:
        report.errors.append("execution_verification_schema_version_invalid")
    unknown = sorted(set(value) - VERIFICATION_CONTAINER_FIELDS)
    if unknown:
        report.errors.append("execution_verification_unknown_fields:" + ",".join(unknown))
    required_container = {"entry", "exit"}
    missing_container = sorted(required_container - set(value))
    if missing_container:
        report.errors.append(
            "execution_verification_missing_fields:" + ",".join(missing_container)
        )
        return report

    entries: dict[str, dict[str, Any] | None] = {}
    for leg_name in ("entry", "exit"):
        entries[leg_name] = _validate_leg(
            leg_name,
            value.get(leg_name),
            record_id=record_id,
            record_kind=record_kind,
            record_type=record_type,
            classification_status=classification_status,
            claim_context=claim_context,
            durable_revisions=durable_revisions,
            execution_evidence=execution_evidence,
            authority_class=authority_class,
            actor=actor,
            report=report,
        )

    entry = entries["entry"]
    exit_ = entries["exit"]
    if record_kind == "observation":
        for leg_name, leg in (("entry", entry), ("exit", exit_)):
            if leg is not None and leg.get("state") != "not_applicable":
                report.errors.append(f"{leg_name}_observation_requires_not_applicable")
    if execution_status == "not_executed":
        for leg_name, leg in (("entry", entry), ("exit", exit_)):
            if leg is not None and leg.get("state") != "not_applicable":
                report.errors.append(f"{leg_name}_not_executed_requires_not_applicable")
    if position_status == "planned" and entry is not None and entry.get("state") != "not_applicable":
        report.errors.append("planned_position_entry_requires_not_applicable")
    if position_status in {"planned", "open"} and exit_ is not None and exit_.get("state") != "not_applicable":
        report.errors.append("open_position_exit_requires_not_applicable")
    if position_status == "closed" and exit_ is not None and exit_.get("state") == "not_applicable":
        report.errors.append("closed_position_exit_claim_cannot_be_not_applicable")
    return report


def validate_execution_contract(
    record: Mapping[str, Any],
    *,
    authority_class: str | None = None,
    actor: str | None = None,
    durable_revisions: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | Any | None = None,
) -> ExecutionVerificationReport:
    """Validate a full record that has adopted the nested schema."""

    actual = record.get("actual") if isinstance(record, Mapping) else None
    if not isinstance(actual, Mapping):
        report = ExecutionVerificationReport()
        report.errors.append("actual_object_required")
        return report
    verification = actual.get("execution_verification")
    if not isinstance(verification, Mapping):
        report = ExecutionVerificationReport()
        report.errors.append("execution_verification_nested_object_required")
        return report
    return validate_execution_verification(
        verification,
        record_id=record.get("record_id"),
        record_kind=record.get("record_kind"),
        record_type=record.get("type"),
        classification_status=record.get("classification_status"),
        execution_status=actual.get("execution_status"),
        position_status=actual.get("position_status"),
        claim_context=_claim_context_from_record(record),
        durable_revisions=durable_revisions,
        execution_evidence=actual.get("execution_evidence"),
        authority_class=authority_class or actual.get("execution_authority_class"),
        actor=actor or actual.get("execution_actor"),
    )


def _normal_trade_identity_dedupe_reasons(record: Mapping[str, Any]) -> list[str]:
    """Require the normal canonical trade/identity/dedupe boundary first."""

    reasons: list[str] = []
    if record.get("record_kind") != "trade":
        reasons.append("requires_trade_record")
    if record.get("type") not in RECORD_TYPES:
        reasons.append("requires_resolved_live_or_backtest_type")
    if record.get("classification_status") != "resolved":
        reasons.append("requires_resolved_classification")
    if not _nonempty_text(record.get("trade_id")):
        reasons.append("requires_canonical_trade_id")
    dedupe = record.get("dedupe") if isinstance(record.get("dedupe"), Mapping) else {}
    if dedupe.get("status") != "canonical" or dedupe.get("duplicate_of") is not None:
        reasons.append("requires_canonical_dedupe_resolution")
    return reasons


def _confirmed_entry_reasons(record: Mapping[str, Any]) -> list[str]:
    reasons = _normal_trade_identity_dedupe_reasons(record)
    actual = record.get("actual") if isinstance(record.get("actual"), Mapping) else {}
    verification = actual.get("execution_verification")
    entry = verification.get("entry") if isinstance(verification, Mapping) else None
    if not isinstance(verification, Mapping):
        reasons.append("requires_nested_execution_verification")
    else:
        contract_report = validate_execution_contract(record)
        if not contract_report.ok:
            reasons.append("requires_valid_execution_verification")
    if not isinstance(entry, Mapping) or entry.get("state") != "confirmed":
        reasons.append("requires_confirmed_entry_verification")
    if actual.get("execution_status") not in {"entered", "exited"}:
        reasons.append("requires_entered_or_exited_lifecycle")
    return reasons


def trade_count_gate(record: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Gate the count of canonical executed trades, not realized results."""

    reasons = _confirmed_entry_reasons(record)
    return not reasons, reasons


def result_metric_minimum_floor(record: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Return only the entry-confirmed/R-basis floor, never full eligibility.

    Consumers must call :func:`result_metric_eligibility_gate` with a named
    metric before treating a result statistic as safe.
    """

    reasons = _confirmed_entry_reasons(record)
    result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
    if result.get("r_basis") not in {"gross", "net"}:
        reasons.append("requires_known_r_basis")
    return not reasons, reasons


def _finalized_result_reasons(record: Mapping[str, Any]) -> list[str]:
    actual = record.get("actual") if isinstance(record.get("actual"), Mapping) else {}
    reasons: list[str] = []
    if actual.get("execution_status") != "exited":
        reasons.append("requires_exited_execution_status")
    if actual.get("position_status") != "closed":
        reasons.append("requires_closed_position")
    return reasons


def _actual_r_reasons(record: Mapping[str, Any]) -> list[str]:
    result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
    r_basis = result.get("r_basis")
    field = "realized_r_gross" if r_basis == "gross" else "realized_r_net"
    if not _finite_number(result.get(field)):
        return [f"requires_{field}_for_declared_r_basis"]
    return []


def result_metric_eligibility_gate(
    record: Mapping[str, Any],
    metric: str | None = None,
) -> tuple[bool, list[str]]:
    """Return eligibility for one named result metric only.

    This intentionally keeps entry-count, outcome, realized-R, and
    path-dependent consumption distinct.  Missing Exit Chart is not a reason
    here; exit-logic and strategy-learning gates remain independent.
    """

    if metric is None:
        return False, ["named_result_metric_required"]
    if metric not in RESULT_METRICS:
        return False, ["unsupported_result_metric"]
    if metric == "trade_count":
        return trade_count_gate(record)
    if metric == "profit_factor":
        return False, ["profit_factor_calculation_basis_not_defined"]

    if metric == "outcome_counts":
        reasons = _confirmed_entry_reasons(record) + _finalized_result_reasons(record)
        result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
        if result.get("outcome") not in {"win", "loss", "breakeven"}:
            reasons.append("requires_final_win_loss_or_breakeven_outcome")
        return not reasons, reasons

    reasons = result_metric_minimum_floor(record)[1] + _finalized_result_reasons(record)
    result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
    if result.get("outcome") not in {"win", "loss", "breakeven", "mixed"}:
        reasons.append("requires_final_non_unknown_outcome")
    reasons.extend(_actual_r_reasons(record))
    return not reasons, reasons


def result_metrics_gate(
    record: Mapping[str, Any],
    metric: str | None = None,
) -> tuple[bool, list[str]]:
    """Compatibility name for the explicit named-metric eligibility gate."""

    return result_metric_eligibility_gate(record, metric=metric)


def sequence_metrics_gate(
    record: Mapping[str, Any],
    metric: str | None = None,
) -> tuple[bool, list[str]]:
    """Require a resolved ordering on top of one named result-metric gate."""

    eligible, reasons = result_metric_eligibility_gate(record, metric=metric)
    identity = record.get("identity") if isinstance(record.get("identity"), Mapping) else {}
    ordering = identity.get("date_ordering_status", record.get("date_ordering_status"))
    if ordering != "resolved":
        reasons = list(reasons) + ["requires_resolved_date_ordering"]
    return not reasons, reasons


def verification_disclosure(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return basis/coverage counts without assigning evidence weights."""

    material = list(records)
    closed = [
        record
        for record in material
        if isinstance(record.get("actual"), Mapping)
        and record["actual"].get("position_status") == "closed"
    ]
    missing_exit = [
        record
        for record in closed
        if not bool((record.get("evidence_state") or {}).get("exit_chart_reviewable"))
    ]
    basis_counts: dict[str, int] = {}
    human_support = {"artifact_supported": 0, "memory_only": 0}
    mode_counts = {"live": 0, "backtest": 0}
    for record in material:
        actual = record.get("actual") if isinstance(record.get("actual"), Mapping) else {}
        verification = actual.get("execution_verification")
        entry = verification.get("entry") if isinstance(verification, Mapping) else None
        if not isinstance(entry, Mapping):
            continue
        basis = entry.get("basis")
        state = entry.get("state")
        key = f"{state}:{basis}"
        basis_counts[key] = basis_counts.get(key, 0) + 1
        if basis == "human_attestation" and entry.get("attestation_support") in human_support:
            human_support[entry["attestation_support"]] += 1
        if record.get("type") in mode_counts:
            mode_counts[record["type"]] += 1
    return {
        "closed_exit_chart_coverage": {
            "closed_count": len(closed),
            "missing_exit_chart_count": len(missing_exit),
            "format": f"{len(missing_exit)}/{len(closed)}",
        },
        "entry_verification_state_basis_counts": dict(sorted(basis_counts.items())),
        "human_attestation_count": sum(human_support.values()),
        "human_attestation_support_counts": human_support,
        "mode_counts": mode_counts,
        "numeric_evidence_weights": False,
    }


__all__ = [
    "ATTESTATION_SUPPORT",
    "EXECUTION_BASES",
    "EXECUTION_STATES",
    "EXECUTION_VERIFICATION_SCHEMA_VERSION",
    "ExecutionVerificationError",
    "ExecutionVerificationReport",
    "result_metric_eligibility_gate",
    "result_metric_minimum_floor",
    "result_metrics_gate",
    "sequence_metrics_gate",
    "trade_count_gate",
    "validate_execution_contract",
    "validate_execution_verification",
    "verification_disclosure",
]
