"""Registration census: reconcile a provider ledger against canonical provider bindings.

Answers one question -- "which provider lifecycles are not yet in Trading Record?" --
from committed data only (``provider-ledgers/<provider>/`` + ``canonical/``).
It never writes canonical state; the only output is a derived census file.

Statuses: REGISTERED, REGISTERED_INCOMPLETE, IN_PROGRESS, STRATEGY_PENDING,
UNREGISTERED, AMBIGUOUS, INVALID_PROVIDER_LIFECYCLE. REGISTERED means the Owner's
three required inputs are all saved (verbatim words, an entry chart and an exit
chart; the Agent judges which saved image is which and records it in the packet); a
finalized trade missing any of them is REGISTERED_INCOMPLETE and lists what is missing. Both keep the same T-ID and never create a duplicate. STRATEGY_PENDING is a legacy compatibility status for
attempts written before provider-verified LIVE registration became independent of
strategy attribution. New unresolved captures create a canonical trade with a T-ID;
existing pending rows remain readable until their provider lifecycle is retried.
"""
from __future__ import annotations

import json
import hashlib
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .operational_lifecycle import canonical_provider
from .registration_pending import load_pending

STATUSES = ("REGISTERED", "REGISTERED_INCOMPLETE", "IN_PROGRESS", "STRATEGY_PENDING", "UNREGISTERED", "AMBIGUOUS", "INVALID_PROVIDER_LIFECYCLE")
PROVIDER_PHASES = ("OPEN", "PARTIALLY_CLOSED", "CLOSED", "UNKNOWN")
SELECTABLE_PHASES = ("OPEN", "PARTIALLY_CLOSED", "CLOSED", "ALL")
SCHEMA_VERSION = "registration-census.v4"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _file_digest(path: Path) -> str:
    """Hash one source file, distinguishing an absent file from an empty one."""

    if not path.exists():
        return hashlib.sha256(b"<missing>").hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_digests(root: Path, provider: str, pending: dict[str, Any]) -> dict[str, str]:
    base = root / "provider-ledgers" / provider / "normalized" / "trade-lifecycles.jsonl"
    return {
        "normalized_ledger_sha256": _file_digest(base),
        "canonical_finalizations_sha256": _file_digest(root / "canonical" / "sequences" / "finalizations.json"),
        "canonical_identity_links_sha256": _file_digest(root / "canonical" / "lifecycle" / "identity-links.jsonl"),
        "canonical_lifecycle_current_sha256": _file_digest(root / "canonical" / "lifecycle" / "current.json"),
        "pending_state_sha256": hashlib.sha256(
            json.dumps(pending, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _stored_freshness(
    path: Path, source_digests: dict[str, str]
) -> tuple[dict[str, str], str | None, dict[str, Any] | None]:
    """Compare current source identities with the persisted Census metadata."""

    if not path.exists():
        return {"status": "unknown", "detail": "NO_PERSISTED_CENSUS"}, None, None
    try:
        persisted = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "detail": "PERSISTED_CENSUS_UNREADABLE"}, None, None
    if not isinstance(persisted, dict):
        return {"status": "unknown", "detail": "PERSISTED_CENSUS_UNREADABLE"}, None, None
    prior = persisted.get("source_digests")
    if persisted.get("schema_version") != SCHEMA_VERSION or not isinstance(prior, dict):
        return {"status": "unknown", "detail": "PERSISTED_CENSUS_HAS_NO_COMPARABLE_SOURCE_DIGESTS"}, None, persisted
    if prior == source_digests:
        return {"status": "fresh", "detail": "SOURCE_DIGESTS_MATCH"}, persisted.get("generated_at"), persisted
    return {"status": "stale", "detail": "SOURCE_DIGESTS_CHANGED"}, None, persisted


def _canonical_json(value: Any) -> str:
    """Serialize JSON values for strict type-aware equality (e.g. 1 != true)."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _bindings(root: Path, provider: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    provider = canonical_provider(provider)
    finalized_path = root / "canonical" / "sequences" / "finalizations.json"
    finalized = json.loads(finalized_path.read_text(encoding="utf-8")).get("records", {}) if finalized_path.exists() else {}
    links: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in _jsonl(root / "canonical" / "lifecycle" / "identity-links.jsonl"):
        if canonical_provider(link.get("provider")) == provider:
            links[str(link.get("provider_lifecycle_id"))].append(link)
    return links, finalized


def registration_acknowledged_gaps(record: Any) -> list[str]:
    """Gaps the Owner has acknowledged as unrecoverable (never a substitute for the content).

    Only the Owner's words can be acknowledged this way, and only while the trade still has no
    verbatim statement. The trade stays incomplete; this only lets the product separate a known
    historical limitation from work that is still to be done.
    """

    if not isinstance(record, dict) or not isinstance(record.get("owner_words_unrecoverable"), dict):
        return []
    statements = record.get("owner_statements") or []
    if any(
        isinstance(item, dict)
        and item.get("authority") == "owner_supplied_verbatim"
        and isinstance(item.get("verbatim_text"), str)
        and item["verbatim_text"].strip()
        for item in statements
    ):
        return []
    return ["owner_words"]


def registration_completeness(record: Any, packet: Any = None, *, root: Path | None = None) -> dict[str, Any]:
    """Whether a canonical trade holds the Owner's three required registration inputs.

    Required: the Owner's verbatim words, an entry chart and an exit chart. Which saved
    image is the entry or exit chart is the Agent's judgment, frozen in the Owner-confirmed
    packet as ``agent_assessment.chart_roles``; this function never classifies an image. It
    only checks that the Agent declared both roles with a rationale and that each
    referenced image is really saved on the trade. One image may be both roles. Replay and
    contemporaneous charts count equally.
    """

    if not isinstance(record, dict):
        return {"complete": False, "missing": ["canonical_record"]}
    missing: list[str] = []
    statements = record.get("owner_statements") or []
    if not any(
        isinstance(item, dict)
        and item.get("authority") == "owner_supplied_verbatim"
        and isinstance(item.get("verbatim_text"), str)
        and item["verbatim_text"].strip()
        and isinstance(item.get("sha256"), str)
        and hashlib.sha256(item["verbatim_text"].encode("utf-8")).hexdigest() == item["sha256"]
        for item in statements
    ):
        missing.append("owner_words")
    # Metadata alone is not a saved image. Demand the canonical original bytes,
    # correct content-addressed locator and matching SHA256. No image inference:
    # the Agent still owns which chart serves each declared role.
    saved_images: set[str] = set()
    artifacts = Path(root).resolve() / "canonical" / "evidence" / "artifacts" if root is not None else None
    for item in record.get("evidence") or []:
        if not isinstance(item, dict) or not (
            str(item.get("media_type", "")).startswith("image/")
            or item.get("type") in {"chart_snapshot", "replay_chart"}
        ):
            continue
        digest, locator = item.get("sha256"), item.get("locator")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            continue
        if not isinstance(locator, str) or not locator.startswith(f"artifact:{digest}"):
            continue
        suffix = locator[len(f"artifact:{digest}"):]
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"} or artifacts is None:
            continue
        file = artifacts / f"{digest}{suffix}"
        if file.is_symlink() or not file.is_file():
            continue
        try:
            if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                continue
        except OSError:
            continue
        for ref in (item.get("source_ref"), locator):
            if isinstance(ref, str) and ref:
                saved_images.add(ref)
    assessment = packet.get("agent_assessment") if isinstance(packet, dict) else None
    packet_roles = assessment.get("chart_roles") if isinstance(assessment, dict) else None
    # A later confirmed Agent judgment recorded on the trade is a complete snapshot and
    # replaces the frozen packet's declaration entirely (a withdrawn role stays withdrawn).
    roles = record.get("chart_roles") if record.get("chart_roles_events") else packet_roles
    for role in ("entry_chart", "exit_chart"):
        declared = roles.get(role) if isinstance(roles, dict) else None
        refs = declared.get("evidence_refs") if isinstance(declared, dict) else None
        rationale = declared.get("rationale") if isinstance(declared, dict) else None
        ok = (
            isinstance(refs, list)
            and bool(refs)
            and all(isinstance(ref, str) and ref in saved_images for ref in refs)
            and isinstance(rationale, str)
            and bool(rationale.strip())
        )
        if not ok:
            missing.append(role)
    return {"complete": not missing, "missing": missing}


def _load_packet(root: Path, packet_sha256: Any) -> Any:
    if not isinstance(packet_sha256, str) or len(packet_sha256) != 64:
        return None
    try:
        packet = json.loads(
            (root / "canonical" / "registration" / "packets" / f"{packet_sha256}.json").read_text(encoding="utf-8")
        )
        if not isinstance(packet, dict) or packet.get("packet_sha256") != packet_sha256:
            return None
        # A filename alone cannot prove an Agent's frozen semantic attestation.
        # Import lazily: registration_packet itself imports registration_census.
        from .registration_packet import packet_sha256 as calculate_packet_sha256
        return packet if calculate_packet_sha256(packet) == packet_sha256 else None
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _lifecycle_records(root: Path) -> dict[str, Any]:
    path = root / "canonical" / "lifecycle" / "current.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = data.get("records") if isinstance(data, dict) else None
    return records if isinstance(records, dict) else {}


def _classify(found: list[dict[str, Any]], finalized: dict[str, Any]) -> dict[str, Any]:
    """Binding-only classification (no pending/ledger information)."""

    matched = [l for l in found if l.get("match_disposition") == "MATCHED"]
    drafts = {l["draft_id"] for l in matched}
    if len(drafts) > 1:
        return {"status": "AMBIGUOUS", "detail": "DATA_INTEGRITY_ERROR: bound to multiple drafts"}
    if found and not matched:
        return {"status": "AMBIGUOUS", "detail": "identity link disposition " + ",".join(sorted({l["match_disposition"] for l in found}))}
    if matched:
        draft_id = next(iter(drafts))
        packet_hashes = {
            str(link.get("registration_packet_sha256")).lower()
            for link in matched
            if link.get("registration_packet_sha256")
        }
        if len(packet_hashes) > 1:
            return {
                "status": "AMBIGUOUS",
                "detail": "DATA_INTEGRITY_ERROR: provider lifecycle is bound to multiple registration packets",
                "draft_id": draft_id,
            }
        packet_sha256 = next(iter(packet_hashes), None)
        done = finalized.get(draft_id)
        common = {
            "draft_id": draft_id,
            **({"packet_sha256": packet_sha256} if packet_sha256 else {}),
        }
        if done:
            return {
                "status": "REGISTERED",
                "trade_no": done.get("trade_no"),
                "trade_id": done.get("trade_id"),
                **common,
            }
        return {
            "status": "IN_PROGRESS",
            "detail": f"draft {draft_id} not finalized",
            **common,
        }
    return {"status": "UNREGISTERED"}


def registration_state(root: Path, provider: str, provider_lifecycle_id: str) -> dict[str, Any]:
    """Current registration state of one provider lifecycle from canonical bindings only."""

    links, finalized = _bindings(root, provider)
    return _classify(links.get(str(provider_lifecycle_id), []), finalized)


def build_census(root: Path, provider: str = "propw") -> dict[str, Any]:
    provider = canonical_provider(provider)
    ledger = root / "provider-ledgers" / provider / "normalized" / "trade-lifecycles.jsonl"
    if not ledger.exists():
        raise FileNotFoundError(f"provider ledger not found: {ledger} (run the provider ledger export first)")

    links, finalized = _bindings(root, provider)
    lifecycle_records = _lifecycle_records(root)
    pending = load_pending(root)
    source_digests = _source_digests(root, provider, pending)
    census_path = root / "provider-ledgers" / provider / "registration-census.json"
    freshness, prior_generated_at, persisted = _stored_freshness(census_path, source_digests)

    rows = []
    for item in _jsonl(ledger):
        lifecycle = item["lifecycle"]
        open_id = str(item.get("open_id") or "")
        key = f"{provider}:{open_id}"
        entry: dict[str, Any] = {
            "provider_lifecycle_key": key,
            "open_id": open_id,
            "instrument": lifecycle.get("instrument"),
            "direction": lifecycle.get("direction"),
            "entry_time": lifecycle.get("entry_time"),
            "is_closed": _flag(lifecycle.get("is_closed")),
            "provider_phase": None,
            "trade_no": None,
            "trade_id": None,
            "detail": None,
        }
        phase, phase_reason = _provider_phase(lifecycle)
        entry["provider_phase"] = phase
        state = _classify(links.get(open_id, []), finalized) if open_id else {"status": "INVALID_PROVIDER_LIFECYCLE", "detail": "MISSING_OPEN_ID"}
        entry["status"] = state["status"]
        entry["trade_no"] = state.get("trade_no")
        # Preserve existing canonical identity independently of the new
        # completeness label. Legacy trades must never become new candidates.
        entry["trade_id"] = state.get("trade_id")
        entry["detail"] = state.get("detail")
        if phase_reason:
            entry["provider_detail"] = phase_reason
        if state.get("packet_sha256"):
            entry["packet_sha256"] = state["packet_sha256"]
        if state["status"] == "REGISTERED":
            completeness = registration_completeness(
                lifecycle_records.get(state.get("trade_id")), _load_packet(root, state.get("packet_sha256")), root=root
            )
            if not completeness["complete"]:
                entry["status"] = "REGISTERED_INCOMPLETE"
                entry["missing"] = completeness["missing"]
                acknowledged = [
                    gap
                    for gap in registration_acknowledged_gaps(lifecycle_records.get(state.get("trade_id")))
                    if gap in completeness["missing"]
                ]
                if acknowledged:
                    entry["acknowledged_gaps"] = acknowledged
                entry["detail"] = "T-ID exists; missing " + ", ".join(completeness["missing"])
        record = pending.get(key)
        if open_id and state["status"] == "UNREGISTERED" and record:
            entry["status"] = "STRATEGY_PENDING"
            entry["detail"] = f"{record['resolver_status']}: {record['reason']}"
            entry["pending"] = {
                "attempt": record["attempt"],
                "recorded_at": record["recorded_at"],
                "resolver_status": record["resolver_status"],
                "packet_sha256": record.get("packet_sha256"),
            }
        elif state["status"] == "UNREGISTERED" and phase not in {"OPEN", "PARTIALLY_CLOSED", "CLOSED"}:
            entry["status"] = "INVALID_PROVIDER_LIFECYCLE"
            entry["detail"] = phase_reason or "UNKNOWN_PROVIDER_LIFECYCLE_PHASE"
        rows.append(entry)

    rows.sort(key=lambda r: (r["entry_time"] or "", r["open_id"]))
    counts = {status: sum(r["status"] == status for r in rows) for status in STATUSES}
    registered_identity_count = counts["REGISTERED"] + counts["REGISTERED_INCOMPLETE"]
    # Incomplete trades whose every remaining gap is an acknowledged historical limitation.
    legacy_acknowledged_count = sum(
        r["status"] == "REGISTERED_INCOMPLETE"
        and bool(r.get("acknowledged_gaps"))
        and set(r.get("missing") or []) <= set(r["acknowledged_gaps"])
        for r in rows
    )
    unregistered_closed = sum(r["status"] == "UNREGISTERED" and r["is_closed"] for r in rows)
    census = {
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "generated_at": prior_generated_at,
        "source_digests": source_digests,
        "freshness": freshness,
        "total": len(rows),
        "counts": counts,
        "registered_identity_count": registered_identity_count,
        "legacy_acknowledged_count": legacy_acknowledged_count,
        "unregistered_closed": unregistered_closed,
        "strategy_pending": [r["provider_lifecycle_key"] for r in rows if r["status"] == "STRATEGY_PENDING"],
        "not_evaluated": ["LEGACY_REPRESENTED (no legacy-trade -> provider lifecycle mapping exists yet)"],
        "lifecycles": rows,
    }
    if freshness.get("status") == "fresh":
        body_keys = (
            "schema_version",
            "provider",
            "source_digests",
            "total",
            "counts",
            "registered_identity_count",
            "legacy_acknowledged_count",
            "unregistered_closed",
            "strategy_pending",
            "not_evaluated",
            "lifecycles",
        )
        expected_body = {key: census[key] for key in body_keys}
        persisted_body = {key: persisted.get(key) for key in body_keys} if persisted is not None else None
        stored_freshness = persisted.get("freshness") if persisted is not None else None
        if not isinstance(stored_freshness, dict) or stored_freshness.get("status") != "fresh":
            freshness = {"status": "stale", "detail": "PERSISTED_CENSUS_NOT_MARKED_FRESH"}
            prior_generated_at = None
        elif _canonical_json(persisted_body) != _canonical_json(expected_body):
            freshness = {"status": "stale", "detail": "PERSISTED_CENSUS_CONTENT_MISMATCH"}
            prior_generated_at = None
    census["generated_at"] = prior_generated_at
    census["freshness"] = freshness
    return census


def _provider_phase(lifecycle: dict[str, Any]) -> tuple[str, str | None]:
    phase = lifecycle.get("provider_lifecycle_phase")
    if phase in PROVIDER_PHASES:
        reason = lifecycle.get("lifecycle_reason")
        if phase == "UNKNOWN":
            return "UNKNOWN", str(reason or "UNKNOWN_PROVIDER_LIFECYCLE_PHASE")
        if reason:
            return "UNKNOWN", str(reason)
        if phase == "OPEN":
            open_size = _number(lifecycle.get("open_size"))
            entry_price = _number(lifecycle.get("entry_price"))
            if (
                lifecycle.get("normalization_status") != "open_only"
                or open_size is None
                or open_size <= 0
                or entry_price is None
                or entry_price <= 0
                or not lifecycle.get("entry_time")
                or not lifecycle.get("instrument")
                or lifecycle.get("direction") in (None, "")
                or _flag(lifecycle.get("is_closed"))
                or _flag(lifecycle.get("is_partial"))
            ):
                return "UNKNOWN", "INCOMPLETE_OR_CONTRADICTORY_OPEN_FACTS"
        elif phase in {"PARTIALLY_CLOSED", "CLOSED"}:
            open_size = _number(lifecycle.get("open_size"))
            closed_size = _number(lifecycle.get("closed_size"))
            entry_price = _number(lifecycle.get("entry_price"))
            exit_price = _number(lifecycle.get("weighted_exit_price"))
            flags_match = (
                not _flag(lifecycle.get("is_closed")) and _flag(lifecycle.get("is_partial"))
                if phase == "PARTIALLY_CLOSED"
                else _flag(lifecycle.get("is_closed")) and not _flag(lifecycle.get("is_partial"))
            )
            quantity_match = (
                open_size is not None
                and closed_size is not None
                and open_size > 0
                and closed_size > 0
                and (
                    closed_size < open_size - 1e-9
                    if phase == "PARTIALLY_CLOSED"
                    else open_size - 1e-9 <= closed_size <= open_size + 1e-9
                )
            )
            facts_match = (
                entry_price is not None
                and entry_price > 0
                and exit_price is not None
                and exit_price > 0
                and lifecycle.get("entry_time")
                and lifecycle.get("exit_time")
                and lifecycle.get("instrument")
                and lifecycle.get("direction") not in (None, "")
            )
            if lifecycle.get("normalization_status") != "ok" or not flags_match or not quantity_match or not facts_match:
                return "UNKNOWN", "INCOMPLETE_OR_CONTRADICTORY_CLOSE_FACTS"
        return phase, None

    # Read older normalized ledgers without relabeling a valid open position as
    # invalid. The fallback is deliberately strict because old files lack an
    # explicit phase/reason contract.
    status = lifecycle.get("normalization_status")
    if status == "ok":
        # v1 stored open_size from baseSize/quantity while closed_size came
        # from closedPiece. Those units are not comparable, so old rows cannot
        # safely prove OPEN/PARTIAL/CLOSED. A fresh ledger export adds the
        # explicit phase contract using totalPiece/closedPiece.
        return "UNKNOWN", "LEGACY_NORMALIZATION_PHASE_UNVERIFIED"
    if status == "open_only":
        size = _number(lifecycle.get("open_size"))
        entry = _number(lifecycle.get("entry_price"))
        if (
            size is not None
            and size > 0
            and entry is not None
            and entry > 0
            and lifecycle.get("entry_time")
            and lifecycle.get("instrument")
            and lifecycle.get("direction") not in (None, "")
            and not _flag(lifecycle.get("is_closed"))
            and not _flag(lifecycle.get("is_partial"))
        ):
            return "OPEN", None
        return "UNKNOWN", "INCOMPLETE_OPEN_FACTS"
    if status == "no_open":
        return "UNKNOWN", "MISSING_OPEN_EVENT"
    if status == "insufficient_exit_data":
        return "UNKNOWN", "INCOMPLETE_EXIT_FACTS"
    return "UNKNOWN", f"UNRECOGNIZED_NORMALIZATION_STATUS:{status}"


def lookup(census: dict[str, Any], provider: str, open_id: str) -> dict[str, Any]:
    """Return the census row for a lifecycle; a REGISTERED row carries the original T-ID."""

    key = f"{canonical_provider(provider)}:{open_id}"
    for row in census["lifecycles"]:
        if row["provider_lifecycle_key"] == key:
            return row
    return {"provider_lifecycle_key": key, "status": "NOT_IN_LEDGER"}


def select_candidates(
    census: dict[str, Any],
    *,
    phase: str = "ALL",
    instrument: str | None = None,
    month: str | None = None,
) -> list[dict[str, Any]]:
    """Return all valid unregistered lifecycles in stable census order."""

    phase = str(phase).upper()
    if phase not in SELECTABLE_PHASES:
        raise ValueError(f"phase must be one of {', '.join(SELECTABLE_PHASES)}")
    phases = set(PROVIDER_PHASES[:3]) if phase == "ALL" else {phase}
    candidates = []
    for row in census["lifecycles"]:  # already ordered by entry_time, open_id
        if row["status"] != "UNREGISTERED" or row.get("provider_phase") not in phases:
            continue
        if instrument and str(row.get("instrument") or "").upper() != instrument.upper():
            continue
        if month and not str(row.get("entry_time") or "").startswith(month):
            continue
        candidates.append(row)
    return candidates


def select_next(
    census: dict[str, Any],
    *,
    phase: str = "CLOSED",
    instrument: str | None = None,
    month: str | None = None,
) -> dict[str, Any] | None:
    """Select the earliest eligible lifecycle; default remains closed-only.

    Lifecycles that are STRATEGY_PENDING / IN_PROGRESS / AMBIGUOUS are never returned, so repeating
    "next" cannot keep selecting a trade that is already being handled.
    """
    candidates = select_candidates(census, phase=phase, instrument=instrument, month=month)
    return candidates[0] if candidates else None


def write_census(root: Path, provider: str = "propw") -> Path:
    provider = canonical_provider(provider)
    census = build_census(root, provider)
    path = root / "provider-ledgers" / provider / "registration-census.json"
    if census["freshness"]["status"] == "fresh" and census.get("generated_at"):
        generated_at = census["generated_at"]
    else:
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    census["generated_at"] = generated_at
    census["freshness"] = {"status": "fresh", "detail": "SOURCE_DIGESTS_MATCH"}
    path.write_text(json.dumps(census, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path
