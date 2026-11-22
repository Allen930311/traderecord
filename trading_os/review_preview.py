"""Read-only preparation and confirmation contract for a single-trade review.

The preview is not a writer. It binds an Agent-proposed review to the exact
closed lifecycle, frozen strategy rule file, attached evidence bytes, and
Owner decision text. The existing LifecycleStore review writer recomputes the
same digest immediately before appending the review event.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .evidence_store import EvidenceStore, EvidenceStoreError
from .evidence_time import normalize_evidence_time_metadata
from .registry import load_effective_rules, load_registry, load_version, resolve_version_path

PREVIEW_SCHEMA = "canonical-trade-review-preview.v1"
ANALYSIS_SCHEMA = "strategy-discovery-review.v1"
CHECKLIST_STATES = frozenset({"YES", "NO", "UNKNOWN", "N/A"})
SETUP_QUALITY = frozenset({"valid", "invalid", "unknown", "not_observable", "not_applicable"})
EXECUTION_QUALITY = frozenset({"aligned", "deviated", "unknown", "not_observable", "not_applicable"})
REVIEW_FIELDS = frozenset({
    "what_went_well", "what_went_wrong", "rule_compliance", "execution_deviation",
    "thesis_validity", "entry_quality", "exit_quality", "evidence_completeness",
    "owner_notes", "review_status", "diagnostic_class", "review_analysis",
})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UNRESOLVED = frozenset({"no_match", "unresolved", "ambiguous", "insufficient_evidence", "unknown", "not_observable"})


class ReviewPreviewError(ValueError):
    """Review proposal is incomplete, stale, ambiguous, or unsupported."""


def prepare_review_evidence_additions(root: Path, intake_path: Path | None) -> list[dict[str, Any]]:
    """Read and describe optional local image bytes without EvidenceStore writes.

    The same intake file is supplied to `capture review-preview` and the
    confirmed `capture review` invocation. Its bytes and predicted artifact
    identities are included in the preview digest; persistence is deferred
    until that digest has been verified.
    """
    if intake_path is None:
        return []
    try:
        payload = json.loads(Path(intake_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewPreviewError("review evidence intake file is missing or invalid JSON") from exc
    if not isinstance(payload, Mapping) or set(payload) != {"schema_version", "images"}:
        raise ReviewPreviewError("review evidence intake requires schema_version and images")
    if payload.get("schema_version") != "trade-review-evidence-intake.v1":
        raise ReviewPreviewError("review evidence intake schema mismatch")
    images = payload.get("images")
    if not isinstance(images, list):
        raise ReviewPreviewError("review evidence intake images must be a list")
    additions: list[dict[str, Any]] = []
    seen: set[str] = set()
    evidence_store = EvidenceStore(Path(root))
    for image in images:
        if not isinstance(image, Mapping) or set(image) - {"path", "type", "captured_at", "represented_market_time"}:
            raise ReviewPreviewError("review image intake contains unsupported fields")
        raw_path = image.get("path")
        kind = image.get("type")
        if not isinstance(raw_path, str) or not raw_path.strip() or kind not in {"chart_snapshot", "replay_chart"}:
            raise ReviewPreviewError("review image intake requires a path and supported image type")
        source = Path(raw_path).expanduser()
        if not source.is_absolute():
            source = Path(root) / source
        if source.is_symlink():
            raise ReviewPreviewError("review image intake path must not be a symlink")
        source = source.resolve()
        if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ReviewPreviewError("review image intake must use a supported image extension")
        try:
            described = evidence_store.describe(source, original_filename=source.name)
            raw = source.read_bytes()
        except (OSError, EvidenceStoreError) as exc:
            raise ReviewPreviewError("review image intake bytes are missing or invalid") from exc
        if not described["media_type"].startswith("image/") or not raw:
            raise ReviewPreviewError("review image intake bytes are not an image")
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            pass
        elif raw.startswith(b"\xff\xd8\xff"):
            pass
        elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            pass
        else:
            raise ReviewPreviewError("review image intake bytes do not match a supported image format")
        if described["sha256"] in seen:
            raise ReviewPreviewError("duplicate review image bytes in one intake")
        seen.add(described["sha256"])
        metadata: dict[str, Any] = {
            **described,
            "source_ref": described["locator"],
            "category": "review",
            "timing": "post_trade",
            "type": kind,
            "provenance": {"capture_mode": "retrospective_replay" if kind == "replay_chart" else "original_capture"},
            "source_path": str(source),
        }
        if image.get("captured_at") is not None:
            metadata["captured_at"] = image["captured_at"]
        if image.get("represented_market_time") is not None:
            metadata["represented_market_time"] = image["represented_market_time"]
        if kind == "replay_chart":
            metadata["role"] = "historical_market_reconstruction"
        try:
            metadata.update(normalize_evidence_time_metadata(metadata))
        except ValueError as exc:
            raise ReviewPreviewError("review image intake time/provenance invalid: " + str(exc)) from exc
        additions.append(metadata)
    return additions


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _strategy_pin(root: Path, record: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    strategy_id = record.get("strategy_id")
    rule_version = record.get("rule_version")
    resolution = record.get("strategy_resolution")
    if isinstance(resolution, Mapping):
        status = str(resolution.get("status", "")).lower()
        if status in _UNRESOLVED:
            return ({"status": "NOT_OBSERVABLE", "strategy_id": None, "rule_version": None, "reason": f"recorded strategy resolution is {status}"}, [])
        if resolution.get("strategy_id") not in (None, strategy_id) or resolution.get("rule_version") not in (None, rule_version):
            return ({"status": "NOT_OBSERVABLE", "strategy_id": None, "rule_version": None, "reason": "lifecycle strategy identity and its recorded resolution disagree"}, [])
    if not isinstance(strategy_id, str) or not strategy_id or not isinstance(rule_version, str) or not rule_version:
        return ({"status": "NOT_OBSERVABLE", "strategy_id": None, "rule_version": None, "reason": "trade has no exact frozen strategy and rule-version identity"}, [])
    try:
        registry = load_registry(root)
        strategies = registry.get("strategies", [])
        strategy = next((item for item in strategies if isinstance(item, Mapping) and item.get("strategy_id") == strategy_id), None)
        if not isinstance(strategy, Mapping) or rule_version not in (strategy.get("rule_versions") or []):
            raise ValueError("exact strategy/rule-version pair is not registered")
        version = load_version(root, rule_version)
        if version.get("strategy_id") != strategy_id:
            raise ValueError("frozen rule file belongs to another strategy")
        version_path = resolve_version_path(root, rule_version)
        version_sha = hashlib.sha256(version_path.read_bytes()).hexdigest()
        rules = load_effective_rules(root, rule_version)
    except (OSError, ValueError, StopIteration) as exc:
        return ({"status": "NOT_OBSERVABLE", "strategy_id": None, "rule_version": None, "reason": f"exact frozen strategy version unavailable: {type(exc).__name__}"}, [])
    pin = {
        "status": "PINNED",
        "strategy_id": strategy_id,
        "rule_version": rule_version,
        "version_file": version_path.relative_to(root.resolve()).as_posix(),
        "version_file_sha256": version_sha,
        "effective_rules_sha256": hashlib.sha256(_canonical_bytes(rules)).hexdigest(),
    }
    return pin, rules


def _relevant_rules(rules: list[dict[str, Any]]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for rule in rules:
        category = str(rule.get("category", ""))
        claim = str(rule.get("claim_class", "")).upper()
        required = rule.get("required") is True
        optional_confluence = rule.get("required") is False and (
            category == "confluence" or category == "confirmation" and claim.startswith("OPTIONAL")
        )
        if not required and not optional_confluence:
            continue
        selected.append({
            "rule_id": str(rule["rule_id"]),
            "lane": "setup" if category in {"setup", "confirmation", "trigger"} else "execution",
            "importance": "required" if required else "optional_confluence",
        })
    return sorted(selected, key=lambda item: item["rule_id"])


def _explicit_no_setup(record: Mapping[str, Any], analysis: Mapping[str, Any]) -> bool:
    """Only structured Owner or Agent decisions can classify NO_SETUP.

    Original Owner statements are evidence for Agent interpretation. Code must
    never infer trading semantics by searching natural-language phrases.
    """
    interpretation = analysis.get("setup_interpretation")
    if isinstance(interpretation, Mapping) and interpretation.get("status") == "NO_SETUP":
        return True
    resolution = record.get("strategy_resolution")
    attestation = resolution.get("owner_attestation") if isinstance(resolution, Mapping) else None
    return isinstance(attestation, Mapping) and attestation.get("setup_status") == "NO_SETUP"


def _evidence_inventory(
    root: Path,
    record: Mapping[str, Any],
    additions: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    evidence_store = EvidenceStore(root)
    for item in [*(record.get("evidence") or []), *additions]:
        if not isinstance(item, Mapping):
            raise ReviewPreviewError("review evidence inventory contains a non-object")
        sha = item.get("sha256")
        source_ref = item.get("source_ref")
        if not _sha256(sha) or not isinstance(source_ref, str) or not source_ref.strip():
            raise ReviewPreviewError("review evidence requires an exact SHA256 and source_ref")
        evidence = {
            key: item[key]
            for key in (
                "source_ref", "sha256", "locator", "category", "timing", "type", "media_type",
                "byte_size", "original_filename", "provenance", "captured_at",
                "represented_market_time", "role",
            )
            if key in item
        }
        try:
            evidence.update(normalize_evidence_time_metadata(evidence))
        except ValueError as exc:
            raise ReviewPreviewError(f"review evidence time/provenance is invalid: {source_ref}") from exc
        is_image = (
            evidence.get("type") in {"chart_snapshot", "replay_chart"}
            or str(evidence.get("media_type", "")).startswith("image/")
        )
        if is_image:
            locator = evidence.get("locator")
            source_path = item.get("source_path")
            if isinstance(source_path, str):
                candidate = Path(source_path)
                if candidate.is_symlink():
                    raise ReviewPreviewError(f"image evidence source symlink forbidden: {source_ref}")
                candidate = candidate.resolve()
                try:
                    described = evidence_store.describe(
                        candidate, original_filename=evidence.get("original_filename")
                    )
                    raw = candidate.read_bytes()
                except (OSError, EvidenceStoreError) as exc:
                    raise ReviewPreviewError(f"image evidence source is missing: {source_ref}") from exc
                for key in ("sha256", "locator", "media_type", "byte_size", "original_filename"):
                    if evidence.get(key) != described.get(key):
                        raise ReviewPreviewError(f"image evidence source identity differs from preview: {source_ref}")
                if evidence.get("source_ref") != described["locator"]:
                    raise ReviewPreviewError(f"image evidence source locator differs from preview: {source_ref}")
            else:
                try:
                    raw = evidence_store.read(locator, expected_sha256=sha) if isinstance(locator, str) else b""
                except EvidenceStoreError as exc:
                    raise ReviewPreviewError(f"image evidence bytes are missing or invalid: {source_ref}") from exc
            if not raw or hashlib.sha256(raw).hexdigest() != sha:
                raise ReviewPreviewError(f"image evidence SHA256 does not match exact bytes: {source_ref}")
            evidence["byte_size"] = len(raw)
        inventory.append(evidence)
    inventory.sort(key=lambda item: (item["sha256"], item["source_ref"], str(item.get("category", ""))))
    # A retry may see a review image both on the lifecycle and in the original
    # Owner-approved intake. Its *semantic* identity is one image, not two
    # digest inputs. Preserve independent entry/review categories for reused bytes.
    by_binding: dict[tuple[str, str, str], dict[str, Any]] = {}
    deduplicated: list[dict[str, Any]] = []
    for item in inventory:
        key = (item["sha256"], item["source_ref"], str(item.get("category", "")))
        prior = by_binding.get(key)
        if prior is not None:
            if prior != item:
                raise ReviewPreviewError("contradictory metadata for the same evidence SHA/source_ref/category")
            continue
        by_binding[key] = item
        deduplicated.append(item)
    return deduplicated


def _validate_analysis(
    record: Mapping[str, Any],
    review: Mapping[str, Any],
    pin: Mapping[str, Any],
    rules: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
) -> None:
    if set(review) - REVIEW_FIELDS:
        raise ReviewPreviewError("review contains unsupported fields")
    if review.get("rule_compliance") not in {"compliant", "violation", "unknown", "not_observable"}:
        raise ReviewPreviewError("review rule_compliance is invalid")
    analysis = review.get("review_analysis")
    expected_keys = {
        "schema_version", "strategy_pin", "checklist", "setup_quality", "execution_quality",
        "agent_assessment",
    }
    if not isinstance(analysis, Mapping) or not expected_keys <= set(analysis) or set(analysis) - expected_keys - {"execution_deviation_support", "owner_decision_verbatim", "setup_interpretation"}:
        raise ReviewPreviewError("review_analysis must follow strategy-discovery-review.v1")
    if analysis.get("schema_version") != ANALYSIS_SCHEMA:
        raise ReviewPreviewError("review_analysis schema version mismatch")
    if dict(analysis.get("strategy_pin") or {}) != dict(pin):
        raise ReviewPreviewError("review strategy pin is stale or differs from the trade's frozen authority")
    if analysis.get("setup_quality") not in SETUP_QUALITY or analysis.get("execution_quality") not in EXECUTION_QUALITY:
        raise ReviewPreviewError("setup_quality and execution_quality must be assessed separately")
    # NO_SETUP is an explicit Agent interpretation, never a keyword match.
    interpretation = analysis.get("setup_interpretation")
    if interpretation is not None:
        if (
            not isinstance(interpretation, Mapping)
            or set(interpretation) != {"status", "rationale"}
            or interpretation.get("status") not in {"NO_SETUP", "HAS_SETUP", "UNKNOWN"}
            or not isinstance(interpretation.get("rationale"), str)
            or not interpretation["rationale"].strip()
        ):
            raise ReviewPreviewError("setup_interpretation requires an Agent-assessed status and rationale")
    agent = analysis.get("agent_assessment")
    if not isinstance(agent, Mapping) or set(agent) != {"setup", "risk", "execution", "exit", "lesson"}:
        raise ReviewPreviewError("Agent assessment must separately cover Setup, Risk, Execution, Exit, and Lesson")
    if any(not isinstance(value, str) or not value.strip() for value in agent.values()):
        raise ReviewPreviewError("each Agent assessment section requires a rationale or explicit unknown")
    # A proposed assessment must be previewable before the Owner has issued
    # any final decision. A saved Owner correction is optional and, when present,
    # must not be synthesized from the final "save" authorization message.
    if "owner_decision_verbatim" in analysis:
        owner_decision = analysis["owner_decision_verbatim"]
        if not isinstance(owner_decision, str) or not owner_decision.strip():
            raise ReviewPreviewError("Owner correction must be nonempty verbatim text when supplied")

    # Independently validate Execution deviations before the strategy-pin
    # check. NO_SETUP can coexist with a separately Owner-attested operation
    # error; absent strategy rules must never be invented for that error.
    available_refs = {(item["sha256"], item["source_ref"]) for item in inventory}
    explicit_execution_deviation = (
        review.get("diagnostic_class") in {"EXECUTION", "MIXED"}
        and analysis.get("execution_quality") == "deviated"
        # Free-text deviation notes are explanation, not a semantic predicate.
    )
    support = analysis.get("execution_deviation_support")
    supported_execution_deviation = False
    if support is not None:
        if not explicit_execution_deviation:
            raise ReviewPreviewError("execution deviation support requires an explicit EXECUTION deviation assessment")
        if not isinstance(support, Mapping) or set(support) != {
            "authority", "owner_statement", "evidence_refs", "rationale",
        }:
            raise ReviewPreviewError("execution deviation support must identify its authority and specific evidence")
        if not isinstance(support.get("rationale"), str) or not support["rationale"].strip():
            raise ReviewPreviewError("execution deviation support requires a nonempty rationale")
        refs = support.get("evidence_refs")
        if not isinstance(refs, list):
            raise ReviewPreviewError("execution deviation evidence_refs must be a list")
        for ref in refs:
            if not isinstance(ref, Mapping) or set(ref) != {"sha256", "source_ref"}:
                raise ReviewPreviewError("execution deviation refs require sha256 and source_ref")
            if (ref.get("sha256"), ref.get("source_ref")) not in available_refs:
                raise ReviewPreviewError("execution deviation evidence reference is not attached to this review")
        if support.get("authority") == "evidence":
            if not refs or support.get("owner_statement") is not None:
                raise ReviewPreviewError("evidence-based execution deviation needs a specific linked ref")
        elif support.get("authority") == "owner_attestation":
            owner_statement = support.get("owner_statement")
            if refs or not isinstance(owner_statement, str) or not owner_statement.strip():
                raise ReviewPreviewError("Owner-attested execution deviation requires the Owner's explicit statement")
            verified_statements = {
                statement["verbatim_text"]
                for statement in record.get("owner_statements", []) or []
                if isinstance(statement, Mapping)
                and statement.get("authority") == "owner_supplied_verbatim"
                and isinstance(statement.get("verbatim_text"), str)
                and isinstance(statement.get("sha256"), str)
                and hashlib.sha256(statement["verbatim_text"].encode("utf-8")).hexdigest() == statement["sha256"]
            }
            if owner_statement not in verified_statements and owner_statement != analysis.get("owner_decision_verbatim"):
                raise ReviewPreviewError("execution deviation Owner statement must be verbatim, not Agent-invented")
        else:
            raise ReviewPreviewError("execution deviation support authority must be evidence or owner_attestation")
        supported_execution_deviation = True

    if supported_execution_deviation and review.get("rule_compliance") != "violation":
        raise ReviewPreviewError("supported execution violation requires rule_compliance violation")

    checklist = analysis.get("checklist")
    if not isinstance(checklist, list):
        raise ReviewPreviewError("review checklist must be a list")
    if pin.get("status") != "PINNED":
        if checklist:
            raise ReviewPreviewError("cannot manufacture strategy rules for NO_MATCH/UNKNOWN or an unavailable frozen version")
        # An unavailable frozen strategy prevents a strategy-rule verdict, but
        # it does not dictate the Agent's independent qualitative setup rating.
        # Keep rule_compliance guarded below; let the Agent explain setup_quality.
        if review.get("rule_compliance") == "violation":
            if (
                review.get("diagnostic_class") == "EXECUTION"
                and explicit_execution_deviation and supported_execution_deviation
            ):
                # This is an independent execution violation, NOT a frozen
                # strategy-rule failure and NOT a change to Owner NO_SETUP.
                return
            if _explicit_no_setup(record, analysis):
                raise ReviewPreviewError("Owner-attributed NO_SETUP cannot be rewritten as a proven strategy rule failure")
            raise ReviewPreviewError("a strategy rule violation requires an exact frozen strategy pin and evidence-linked checklist")
        if review.get("rule_compliance") == "compliant":
            raise ReviewPreviewError("unresolved/NO_SETUP trade cannot be declared strategy compliant without a frozen rule pin")
        if analysis.get("execution_quality") == "deviated":
            raise ReviewPreviewError("unpinned execution deviation requires specific Owner attestation or evidence")
        return

    expected = _relevant_rules(rules)
    if len(checklist) != len(expected):
        raise ReviewPreviewError("review checklist must include every required rule and optional confluence exactly once")
    available_refs = {(item["sha256"], item["source_ref"]) for item in inventory}
    # Replay and ordinary screenshots have equal plan/rule evidentiary status.
    # Their capture time is provenance, never an observability veto.
    validated_rows: list[Mapping[str, Any]] = []
    for actual, required in zip(checklist, expected):
        if not isinstance(actual, Mapping) or set(actual) != {
            "rule_id", "rule_version", "lane", "importance", "status", "evidence_refs", "rationale",
        }:
            raise ReviewPreviewError("each checklist row must pin its rule, evaluation, evidence, and rationale")
        if any(actual.get(key) != required[key] for key in ("rule_id", "lane", "importance")):
            raise ReviewPreviewError("checklist omitted, duplicated, or misclassified a frozen rule")
        if actual.get("rule_version") != pin.get("rule_version"):
            raise ReviewPreviewError("checklist rule_version differs from the trade's pinned version")
        if actual.get("status") not in CHECKLIST_STATES:
            raise ReviewPreviewError("checklist status must be YES, NO, UNKNOWN, or N/A")
        rationale = actual.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ReviewPreviewError("each checklist row requires a concise evidence-based rationale")
        refs = actual.get("evidence_refs")
        if not isinstance(refs, list):
            raise ReviewPreviewError("checklist evidence_refs must be a list")
        for ref in refs:
            if not isinstance(ref, Mapping) or set(ref) != {"sha256", "source_ref"}:
                raise ReviewPreviewError("checklist evidence refs require exact sha256 and source_ref")
            binding = (ref.get("sha256"), ref.get("source_ref"))
            if not _sha256(binding[0]) or binding not in available_refs:
                raise ReviewPreviewError("checklist evidence ref is missing or not attached to this review")
        if actual.get("status") in {"YES", "NO"} and not refs:
            raise ReviewPreviewError("YES/NO checklist findings require linked evidence")
        validated_rows.append(actual)

    # Checklist findings inform Agent Setup Quality, not a hard-coded formula.
    # Agent supplies the overall judgement and its evidence-based explanation.
    required_no = [
        row for row in validated_rows
        if row.get("importance") == "required" and row.get("status") == "NO"
    ]
    required_unknown = [
        row for row in validated_rows
        if row.get("importance") == "required" and row.get("status") in {"UNKNOWN", "N/A"}
    ]
    if review.get("rule_compliance") == "violation":
        setup_execution_no = [row for row in required_no if row.get("lane") == "setup"]
        execution_lane_no = [row for row in required_no if row.get("lane") == "execution"]
        diagnostic = review.get("diagnostic_class")
        if setup_execution_no and (execution_lane_no or supported_execution_deviation) and diagnostic != "MIXED":
            raise ReviewPreviewError("combined strategy and execution rule failures require diagnostic_class MIXED")
        if setup_execution_no and diagnostic not in {"METHOD", "MIXED"}:
            raise ReviewPreviewError("a required setup-rule violation requires diagnostic_class METHOD or MIXED")
        if execution_lane_no and diagnostic not in {"EXECUTION", "MIXED"}:
            raise ReviewPreviewError("a required execution-rule violation requires diagnostic_class EXECUTION or MIXED")
        if not required_no and not (explicit_execution_deviation and supported_execution_deviation):
            raise ReviewPreviewError("strategy violation requires an evidence-linked required NO; an execution violation requires specifically supported execution deviation")
    if review.get("rule_compliance") == "compliant" and (required_no or required_unknown):
        raise ReviewPreviewError("strategy compliance cannot be compliant while a required condition is NO, UNKNOWN, or N/A")


def _target_event_digest(store: Any, record: Mapping[str, Any]) -> str:
    ids = set(record.get("lifecycle_event_ids") or [])
    reader = getattr(store, "_read_events", None)
    if not callable(reader) or not ids:
        return hashlib.sha256(_canonical_bytes([])).hexdigest()
    all_events = [event for event in reader() if event.get("event_id") in ids]
    if {event.get("event_id") for event in all_events} != ids:
        raise ReviewPreviewError("target trade lifecycle event history is incomplete")
    # Evidence additions are bound separately by the exact inventory below.
    # Excluding attachment event envelopes lets the remote executor persist
    # already-confirmed image bytes before invoking the existing review writer,
    # while any change to the attached evidence metadata still changes digest.
    events = [
        event for event in all_events
        if event.get("event_type") not in {"review_started", "reviewed", "evidence_attached"}
    ]
    return hashlib.sha256(_canonical_bytes(events)).hexdigest()


def prepare_review_preview(
    root: Path,
    store: Any,
    trade_id: str,
    review: Mapping[str, Any],
    *,
    evidence_additions: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, read-only preview for one explicitly selected trade."""
    if not isinstance(trade_id, str) or not trade_id.startswith("trd_"):
        raise ReviewPreviewError("an exact canonical trade_id is required")
    project = getattr(store, "project", None)
    current = project() if callable(project) else store.current()
    records = current.get("records") if isinstance(current, Mapping) else None
    record = records.get(trade_id) if isinstance(records, Mapping) else None
    if not isinstance(record, Mapping) or not isinstance(record.get("trade_no"), str):
        raise ReviewPreviewError("review only accepts an existing canonical T-ID")
    if record.get("state") not in {"CLOSED", "REVIEW_PENDING"}:
        raise ReviewPreviewError("review preview requires a CLOSED canonical trade without a completed review")
    if (record.get("exit_verification") or {}).get("state") != "confirmed":
        raise ReviewPreviewError("review preview requires confirmed exit verification")
    if not isinstance(review, Mapping):
        raise ReviewPreviewError("review proposal must be an object")
    root = Path(root).resolve()
    pin, rules = _strategy_pin(root, record)
    inventory = _evidence_inventory(root, record, list(evidence_additions or []))
    _validate_analysis(record, review, pin, rules, inventory)
    trade_snapshot = {
        key: record.get(key)
        for key in (
            "trade_id", "trade_no", "trade_type", "execution_environment", "symbol", "direction",
            "strategy_id", "rule_version", "strategy_resolution", "planned", "entry", "partial_closes",
            "close", "closed_at", "exit_verification", "provider_pnl_verification", "provider_pnl",
            "realized_r", "owner_statements",
        )
    }
    trade_snapshot["state"] = "CLOSED"
    correction = record.get("plan_correction")
    if isinstance(correction, Mapping) and isinstance(correction.get("plan"), Mapping):
        # The reviewer must see the same plan analytics and Trade View use, and the digest the
        # Owner confirms must change when a correction is recorded.
        plan = correction["plan"]
        planned = dict(trade_snapshot.get("planned") or {})
        planned.update(
            entry_price=plan.get("intended_entry"), stop_loss_price=plan.get("stop_loss"),
            take_profit_price=plan.get("take_profit"), target_price=plan.get("take_profit"),
            planned_rr=plan.get("planned_rr"),
        )
        trade_snapshot["planned"] = planned
        trade_snapshot["plan_correction"] = {
            "source_id": correction.get("source_id"), "event_id": correction.get("event_id"),
            "r_basis": plan.get("r_basis"), "realized_r": plan.get("realized_r"),
        }
    content = {
        "trade_snapshot": trade_snapshot,
        "target_lifecycle_event_sha256": _target_event_digest(store, record),
        "strategy_pin": pin,
        "evidence_inventory": inventory,
        "review_candidate": dict(review),
    }
    digest = hashlib.sha256(_canonical_bytes({"schema_version": PREVIEW_SCHEMA, "content": content})).hexdigest()
    return {
        "schema_version": PREVIEW_SCHEMA,
        "trade_id": trade_id,
        "trade_no": record["trade_no"],
        "content": content,
        "preview_digest": digest,
    }


def verify_owner_confirmation(
    root: Path,
    store: Any,
    trade_id: str,
    review: Mapping[str, Any],
    owner_confirmation: Mapping[str, Any] | None,
    *,
    evidence_additions: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify explicit Owner approval against a freshly recomputed review preview."""
    if not isinstance(owner_confirmation, Mapping) or set(owner_confirmation) != {"confirmed_by", "confirmed_at", "preview_digest"}:
        raise ReviewPreviewError("explicit Owner confirmation with preview_digest is required")
    if owner_confirmation.get("confirmed_by") != "example-owner":
        raise ReviewPreviewError("only the Owner may confirm a review preview")
    confirmed_at = owner_confirmation.get("confirmed_at")
    if not isinstance(confirmed_at, str) or not confirmed_at.strip():
        raise ReviewPreviewError("Owner confirmation timestamp is required")
    try:
        parsed = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewPreviewError("Owner confirmation timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewPreviewError("Owner confirmation timestamp must include a timezone")
    if not _sha256(owner_confirmation.get("preview_digest")):
        raise ReviewPreviewError("Owner confirmation digest must be lowercase SHA256")
    preview = prepare_review_preview(root, store, trade_id, review, evidence_additions=evidence_additions)
    if owner_confirmation["preview_digest"] != preview["preview_digest"]:
        raise ReviewPreviewError("Owner confirmation digest is stale or does not match the final review preview")
    return preview
