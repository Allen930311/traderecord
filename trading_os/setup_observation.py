"""Setup Observation: a setup worth keeping that was NOT executed.

Writes a durable ``obs_<uuid7>`` record through ``CanonicalWriter.record_observation``.
It never creates a draft, identity link, T-number, lifecycle event or provider binding,
and nothing in analytics / performance reads ``canonical/records/observations`` -- an
observation can never enter a win rate, R total or LIVE sample.

The strategy resolver runs on every observation. All four outcomes can be stored
(MATCHED / NO_MATCH / AMBIGUOUS / INSUFFICIENT_EVIDENCE): an unexecuted setup often has
incomplete evidence and must not be lost because it is still undecided.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_store import EvidenceStore
from .strategy_resolver import RESOLVER_VERSION, resolve_strategy, to_lifecycle_resolution
from .writer import CanonicalWriter

REASONS_NOT_TAKEN = ("missed", "skipped", "risk_limit", "invalidated", "other")
SETUP_QUALITIES = ("good", "fair", "poor", "unknown")
EVIDENCE_TIMINGS = ("pre_trade", "execution", "post_trade", "unknown")
RECORDING_TIMINGS = ("contemporaneous", "retrospective")


class ObservationError(ValueError):
    """Raised when a setup observation cannot be recorded fail-closed."""


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha_of_ref(ref: str) -> str | None:
    """sha256 named by an evidence ref: ``artifact:<sha>[.ext]``, a bare sha, or None."""

    candidate = ref.removeprefix("artifact:").split(".")[0]
    return candidate.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", candidate) else None


def _bind_observation_refs(
    strategy_observations: Mapping[str, Any], files: Sequence[Path], evidence_timing: str
) -> dict[str, Any]:
    """Every rule observation must cite THIS observation's saved evidence (by sha / artifact locator / file name)
    with a timing that matches the saved evidence; unknown refs fail closed. Returns resolver input with refs
    normalized to the durable ``artifact:<sha>`` form."""

    by_sha = {_file_sha(p): p for p in files}
    by_name = {Path(p).name: _file_sha(p) for p in files}
    bound = dict(strategy_observations)
    items = []
    for item in strategy_observations.get("observations") or []:
        if not isinstance(item, Mapping):
            raise ObservationError("each rule observation must be an object")
        refs = item.get("evidence_refs") or []
        normalized = []
        for ref in refs:
            if not isinstance(ref, str) or not ref.strip():
                raise ObservationError("rule observation evidence_refs must be non-empty strings")
            if ref.startswith("adjudication:") or ref.startswith("plan:"):
                normalized.append(ref)
                continue
            sha = _sha_of_ref(ref) or by_name.get(ref) or by_name.get(Path(ref).name)
            if sha is None or sha not in by_sha:
                raise ObservationError(f"rule observation cites evidence that is not saved with this observation: {ref}")
            normalized.append(f"artifact:{sha}")
        if item.get("adjudication_id") is None and item.get("timing") != evidence_timing:
            raise ObservationError(
                f"rule observation timing {item.get('timing')!r} does not match the saved evidence timing {evidence_timing!r} "
                f"({item.get('rule_id')})"
            )
        items.append({**item, "evidence_refs": normalized})
    bound["observations"] = items
    return bound


def _require_pre_trade_adjudication(root: Path, adjudication_id: str | None, shas: Sequence[str]) -> None:
    path = Path(root) / "canonical" / "adjudications" / f"{adjudication_id}.json"
    if not isinstance(adjudication_id, str) or not adjudication_id.startswith("adj_") or "/" in adjudication_id or not path.exists():
        raise ObservationError(
            "a retrospective observation cannot claim pre_trade evidence without an accepted Owner evidence adjudication "
            "(pre_trade_adjudication_id); use evidence_timing=post_trade for an after-the-fact capture"
        )
    decision = json.loads(path.read_text(encoding="utf-8"))
    if decision.get("decision_type") != "evidence" or decision.get("decision") != "accept" or not decision.get("reviewer"):
        raise ObservationError(f"adjudication is not an accepted evidence decision: {adjudication_id}")
    refs = " ".join(str(r) for r in decision.get("evidence_refs", [])).lower()
    missing = [sha for sha in shas if sha.lower() not in refs]
    if missing:
        raise ObservationError(f"adjudication {adjudication_id} does not name every saved evidence image (missing sha256 {missing[0][:12]}...)")


def record_setup_observation(
    root: Path,
    *,
    symbol: str,
    direction: str,
    observed_at: str,
    timeframe_context: str,
    timeframe_trigger: str,
    reason_not_taken: str,
    evidence_files: Sequence[Path],
    strategy_observations: Mapping[str, Any],
    recording_timing: str,
    evidence_timing: str,
    reason_note: str | None = None,
    setup_quality: str = "unknown",
    thesis: str = "",
    pre_trade_adjudication_id: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    if direction not in {"long", "short"}:
        raise ObservationError("direction must be long or short")
    if not symbol or not observed_at:
        raise ObservationError("symbol and observed_at are required")
    if reason_not_taken not in REASONS_NOT_TAKEN:
        raise ObservationError(f"reason_not_taken must be one of {list(REASONS_NOT_TAKEN)}")
    if reason_not_taken == "other" and not (reason_note and reason_note.strip()):
        raise ObservationError("reason_not_taken=other requires reason_note")
    if setup_quality not in SETUP_QUALITIES:
        raise ObservationError(f"setup_quality must be one of {list(SETUP_QUALITIES)}")
    if evidence_timing not in EVIDENCE_TIMINGS:
        raise ObservationError(f"evidence_timing must be one of {list(EVIDENCE_TIMINGS)}")
    if recording_timing not in RECORDING_TIMINGS:
        raise ObservationError(f"recording_timing must be one of {list(RECORDING_TIMINGS)}")
    if not evidence_files:
        raise ObservationError("a setup observation requires at least one evidence image")
    if not isinstance(strategy_observations, Mapping):
        raise ObservationError("strategy_observations are required for the strategy resolver")

    # A retrospective observation (image captured / written up after the fact) can never silently claim
    # pre_trade evidence: that needs an accepted Owner evidence adjudication that names the saved images.
    if recording_timing == "retrospective" and evidence_timing == "pre_trade":
        _require_pre_trade_adjudication(root, pre_trade_adjudication_id, [_file_sha(p) for p in evidence_files])

    # Resolver refs must resolve to THIS observation's durable evidence (validated before anything is written).
    strategy_observations = _bind_observation_refs(strategy_observations, [Path(p) for p in evidence_files], evidence_timing)

    store = EvidenceStore(root)
    evidence = []
    for path in evidence_files:
        ingested = store.ingest(path)  # exact original bytes, SHA-256 addressed; fails closed
        evidence.append(
            {
                "source_ref": ingested["locator"],
                "locator": ingested["locator"],
                "sha256": ingested["sha256"],
                "category": "pre_trade_context",
                "timing": evidence_timing,
                "type": "chart_snapshot",
                "method": "chart_snapshot",
                "media_type": ingested["media_type"],
                "byte_size": ingested["byte_size"],
                "original_filename": ingested["original_filename"],
            }
        )

    resolver_input = dict(strategy_observations)
    resolution = resolve_strategy(root, resolver_input)
    resolver_result = {
        "resolver_version": RESOLVER_VERSION,
        "status": resolution["status"],
        "resolved": resolution["resolved"],
        "reason": resolution["reason"],
        "strategy_id": resolution["strategy_id"],
        "rule_version": resolution["rule_version"],
        "strategy_results": {m["strategy_id"]: m["result"] for m in resolution["matrix"]},
        "observations_sha256": _sha(resolver_input),
    }

    # Deterministic record identity => re-recording the same setup returns the same observation.
    record_id = "rec_" + _sha({"s": symbol.upper(), "d": direction, "t": observed_at, "e": sorted(e["sha256"] for e in evidence)})[:20]
    writer = CanonicalWriter(root)
    directory = writer.records / "observations"
    if directory.exists():
        for existing_path in directory.glob("obs_*.json"):
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
            if existing.get("record_id") == record_id:
                return {"recorded": True, "already_recorded": True, "observation_id": existing["observation_id"], "strategy_result": existing.get("setup_observation", {}).get("resolver_result")}

    setup_observation: dict[str, Any] = {
        "reason_not_taken": reason_not_taken,
        "reason_note": reason_note,
        "setup_quality": setup_quality,
        "resolver_result": resolver_result,
        "executed": False,
    }
    record = {
        "schema_version": "trade.v1",
        "record_id": record_id,
        "record_kind": "observation",
        "classification_status": "resolved",
        "execution_environment": "unknown",
        "recording_timing": recording_timing,
        "legacy_source": {"repository": "trading-record", "layer": "setup_observation", "path": f"canonical/records/observations/{record_id}"},
        "identity": {
            "event_date": observed_at[:10],
            "observed_at": observed_at,
            "direction": direction,
            "instrument": {"symbol": symbol, "pair_raw": symbol},
        },
        "strategy": {
            "strategy_id": resolution["strategy_id"],
            "rule_version": resolution["rule_version"],
            "setup_tags": [],
        },
        "plan": {"timeframe_context": timeframe_context, "timeframe_trigger": timeframe_trigger, "thesis": thesis},
        "actual": {"execution_status": "not_executed", "position_status": "unknown"},
        "result": {"outcome": "unknown"},
        "evidence": evidence,
        "review": {"status": "not_started"},
        "dedupe": {"status": "canonical"},
        "analytics": {"eligibility": "never", "reason": "setup observation is not an executed trade"},
        "raw_legacy": {},
        "provenance": {
            "capture": "setup_observation.v1",
            "observed_at": observed_at,
            "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "recording_timing": recording_timing,
            "evidence_timing": evidence_timing,
            **({"pre_trade_adjudication_id": pre_trade_adjudication_id} if pre_trade_adjudication_id else {}),
        },
        "setup_observation": setup_observation,
    }
    if resolution["resolved"]:
        record["strategy_resolution"] = to_lifecycle_resolution(resolution, contemporaneous=recording_timing == "contemporaneous")
    observation_id = writer.record_observation(record)
    return {
        "recorded": True,
        "already_recorded": False,
        "observation_id": observation_id,
        "strategy_result": resolver_result,
    }
