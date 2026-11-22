"""Validation for evidence capture provenance and represented market time.

The evidence ``timing`` field records when an artifact was captured relative to
the trade. Resolver facts use a separate ``supports_fact_timing`` projection.
Replay provenance stays truthful; admissibility is decided by the governed
retrospective attribution and its normalized rule-fact projection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping


class EvidenceTimeError(ValueError):
    """Raised when evidence-time metadata is malformed or contradicts provenance."""


def _timestamp(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EvidenceTimeError(f"{field} must be an ISO timestamp when supplied")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceTimeError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceTimeError(f"{field} must include a timezone")
    return parsed


def normalize_evidence_time_metadata(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize optional evidence-time metadata.

    Legacy evidence without the new fields remains readable. Retrospective
    replay evidence keeps its true post-trade provenance while separately
    recording what historical market interval the chart depicts.
    """

    normalized: dict[str, Any] = {}
    timing = evidence.get("timing")
    category = evidence.get("category")
    top_captured_at = evidence.get("captured_at")
    _timestamp(top_captured_at, "captured_at")
    relative = evidence.get("timing_relative_to_trade")
    if relative is not None and relative != timing:
        raise EvidenceTimeError("timing_relative_to_trade must match evidence.timing provenance")

    provenance_raw = evidence.get("provenance")
    provenance: dict[str, Any] = {}
    if provenance_raw is not None:
        if not isinstance(provenance_raw, Mapping):
            raise EvidenceTimeError("provenance must be an object")
        provenance = dict(provenance_raw)
        capture_mode = provenance.get("capture_mode")
        if capture_mode not in {"original_capture", "retrospective_replay", "provider_generated", "unknown"}:
            raise EvidenceTimeError("provenance.capture_mode is not a supported capture mode")
        nested_captured_at = provenance.get("captured_at")
        if nested_captured_at is not None and top_captured_at is not None and nested_captured_at != top_captured_at:
            raise EvidenceTimeError("provenance.captured_at must match evidence.captured_at")
        captured_at = nested_captured_at if nested_captured_at is not None else top_captured_at
        _timestamp(captured_at, "provenance.captured_at")
        provenance["capture_time_status"] = "known" if captured_at is not None else "not_observable"
        if captured_at is not None:
            provenance["captured_at"] = captured_at
        normalized["provenance"] = provenance

    represented_raw = evidence.get("represented_market_time")
    represented: dict[str, Any] = {}
    if represented_raw is not None:
        if not isinstance(represented_raw, Mapping):
            raise EvidenceTimeError("represented_market_time must be an object")
        represented = dict(represented_raw)
        start = _timestamp(represented.get("start"), "represented_market_time.start")
        end = _timestamp(represented.get("end"), "represented_market_time.end")
        if start is not None and end is not None and start > end:
            raise EvidenceTimeError("represented_market_time.start must not be after end")
        represented["status"] = "known" if start is not None and end is not None else (
            "partial" if start is not None or end is not None else "not_observable"
        )
        normalized["represented_market_time"] = represented

    role = evidence.get("role")
    if role is not None:
        if not isinstance(role, str) or not role.strip():
            raise EvidenceTimeError("role must be a non-empty string when supplied")
        role = role.strip()
        normalized["role"] = role

    if provenance.get("capture_mode") == "retrospective_replay":
        if timing != "post_trade" or category != "review":
            raise EvidenceTimeError("retrospective_replay must remain review/post_trade")
        evidence_type = evidence.get("type")
        if evidence_type is not None and evidence_type != "replay_chart":
            raise EvidenceTimeError("retrospective_replay must keep type=replay_chart")
        if role != "historical_market_reconstruction":
            raise EvidenceTimeError("retrospective_replay role must be historical_market_reconstruction")
        if relative is not None and relative != "post_trade":
            raise EvidenceTimeError("retrospective_replay timing_relative_to_trade must remain post_trade")
        normalized["timing_relative_to_trade"] = "post_trade"
        replay_captured_at = provenance.get("captured_at", top_captured_at)
        capture_time = _timestamp(replay_captured_at, "provenance.captured_at")
        represented_end = _timestamp(represented.get("end"), "represented_market_time.end")
        if capture_time is not None and represented_end is not None and capture_time <= represented_end:
            raise EvidenceTimeError("retrospective replay capture time must be later than its represented market time")
    elif role == "historical_market_reconstruction":
        raise EvidenceTimeError("historical_market_reconstruction requires retrospective_replay provenance")
    elif relative is not None:
        normalized["timing_relative_to_trade"] = relative

    return normalized


def represented_market_time_contains(evidence: Mapping[str, Any], timestamp: str) -> bool | None:
    """Return whether a chart's represented interval contains ``timestamp``.

    ``None`` means the chart did not expose enough represented-time bounds to
    decide; callers must preserve that as not_observable rather than conflict.
    """

    represented = evidence.get("represented_market_time")
    if not isinstance(represented, Mapping):
        return None
    start = _timestamp(represented.get("start"), "represented_market_time.start")
    end = _timestamp(represented.get("end"), "represented_market_time.end")
    if start is None or end is None:
        return None
    point = _timestamp(timestamp, "represented event timestamp")
    return bool(point is not None and start <= point <= end)
