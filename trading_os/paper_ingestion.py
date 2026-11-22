"""Crypto PaperExecutionEvidence -> existing Trading Record lifecycle."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .evidence_store import EvidenceStore, EvidenceStoreError
from .operational_lifecycle import LifecycleError, LifecycleStore
from .registry import load_registry


PAPER_EVIDENCE_SCHEMA = "paper-execution-evidence.v1"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class PaperIngestionError(ValueError):
    """Raised when a Crypto handoff fails closed."""


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _iso(value: Any) -> bool:
    return _parse_iso(value) is not None


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
        raise PaperIngestionError(f"{field} must be a positive finite number")
    return float(value)


def _strategy_is_allowed(root: Path, strategy_id: str, rule_version: str) -> None:
    registry = load_registry(root)
    strategy = next((item for item in registry.get("strategies", []) if item.get("strategy_id") == strategy_id), None)
    if not isinstance(strategy, Mapping):
        raise PaperIngestionError(f"strategy is not registered: {strategy_id}")
    if strategy.get("status") not in {"active", "experimental"}:
        raise PaperIngestionError("paper evidence requires an active or experimental strategy")
    versions = strategy.get("rule_versions") or []
    version_ids = {item if isinstance(item, str) else item.get("version_id") for item in versions if isinstance(item, (str, Mapping))}
    if rule_version not in version_ids:
        raise PaperIngestionError("rule_version is not registered for the selected strategy")


def _payload_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_paper_execution_evidence(payload: Mapping[str, Any], root: Path) -> dict[str, Any]:
    required = {
        "schema_version", "execution_id", "strategy_id", "rule_version", "execution_mode", "provider",
        "provider_environment", "instrument", "side", "requested_quantity", "filled_quantity",
        "requested_price", "fill_price", "fees", "slippage", "execution_timestamp", "signal_reference",
        "market_data_reference", "crypto_run_id", "crypto_commit_sha", "producer", "raw_evidence_reference", "status",
    }
    missing = sorted(key for key in required if key not in payload)
    if missing:
        raise PaperIngestionError(f"missing required provenance/fields: {', '.join(missing)}")
    if payload.get("schema_version") != PAPER_EVIDENCE_SCHEMA:
        raise PaperIngestionError("unsupported paper evidence schema_version")
    if payload.get("execution_mode") != "PAPER":
        raise PaperIngestionError("execution_mode must be PAPER")
    if payload.get("producer") != "crypto":
        raise PaperIngestionError("producer must be crypto")
    if payload.get("status") != "FILLED":
        raise PaperIngestionError("paper evidence status must be FILLED")
    if payload.get("side") not in {"long", "short"}:
        raise PaperIngestionError("side must be long or short")
    for field in ("execution_id", "strategy_id", "rule_version", "provider", "provider_environment", "instrument", "signal_reference", "crypto_run_id", "raw_evidence_reference"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise PaperIngestionError(f"{field} is required")
    expected_raw_reference = f"crypto://paper-execution/{payload['execution_id']}"
    if payload["raw_evidence_reference"] != expected_raw_reference:
        raise PaperIngestionError("raw_evidence_reference must exactly match execution_id")
    if not _COMMIT_RE.fullmatch(str(payload["crypto_commit_sha"])):
        raise PaperIngestionError("crypto_commit_sha must be a full 40-character SHA")
    if not _iso(payload["execution_timestamp"]):
        raise PaperIngestionError("execution_timestamp must be an ISO timestamp")
    for field in ("requested_quantity", "filled_quantity", "requested_price", "fill_price"):
        _positive_number(payload[field], field)
    if payload["filled_quantity"] > payload["requested_quantity"]:
        raise PaperIngestionError("filled_quantity cannot exceed requested_quantity")
    for field in ("fees", "slippage", "market_data_reference"):
        if not isinstance(payload[field], Mapping) or not payload[field]:
            raise PaperIngestionError(f"{field} must be a non-empty object")
    for field in ("provider_symbol", "timeframe", "candle_timestamp_utc", "batch_digest"):
        if not isinstance(payload["market_data_reference"].get(field), str) or not payload["market_data_reference"][field].strip():
            raise PaperIngestionError(f"market_data_reference.{field} is required")
    market_ref = payload["market_data_reference"]
    if market_ref.get("provider") is not None and market_ref.get("provider") != payload["provider"]:
        raise PaperIngestionError("market_data_reference.provider conflicts with provider")
    for field in ("candle_timestamp_utc", "start_utc", "end_utc"):
        if field in market_ref and not _iso(market_ref[field]):
            raise PaperIngestionError(f"market_data_reference.{field} must be an ISO timestamp")
    if "start_utc" in market_ref and "end_utc" in market_ref:
        start = _parse_iso(market_ref["start_utc"])
        end = _parse_iso(market_ref["end_utc"])
        if start is None or end is None or start > end:
            raise PaperIngestionError("market_data_reference start_utc must be <= end_utc")
    if not _SHA256_RE.fullmatch(str(payload["market_data_reference"]["batch_digest"])):
        raise PaperIngestionError("market_data_reference.batch_digest must be SHA256")
    _strategy_is_allowed(root, str(payload["strategy_id"]), str(payload["rule_version"]))
    return dict(payload)


def _existing_ingest(store: LifecycleStore, execution_id: str, evidence_digest: str) -> dict[str, Any] | None:
    event_by_draft: dict[str, dict[str, Any]] = {}
    finalized_by_draft: dict[str, dict[str, Any]] = {}
    for event in store._read_events():
        payload = event.get("payload") or {}
        paper = payload.get("paper_execution") if isinstance(payload, Mapping) else None
        if event.get("event_type") == "backtest_entry_confirmed" and isinstance(paper, Mapping) and paper.get("execution_id") == execution_id:
            stored_digest = paper.get("paper_evidence_digest")
            if not isinstance(stored_digest, str):
                stored_digest = _payload_digest({key: value for key, value in paper.items() if key != "paper_evidence_digest"})
            if stored_digest != evidence_digest:
                raise PaperIngestionError("conflicting paper evidence for existing execution_id")
            event_by_draft[str(payload.get("draft_id"))] = event
        if event.get("event_type") == "canonical_finalized" and isinstance(payload, Mapping):
            finalized_by_draft[str(payload.get("draft_id"))] = payload
    for draft_id, event in event_by_draft.items():
        finalized = finalized_by_draft.get(draft_id, {})
        if not finalized.get("trade_id") or not finalized.get("trade_no"):
            # A matching entry-confirmed event is an interrupted ingest, not a
            # completed duplicate.  Let the existing idempotent lifecycle
            # calls resume the remaining canonical finalization.
            continue
        return {
            "execution_id": execution_id,
            "draft_id": draft_id,
            "trade_id": finalized.get("trade_id"),
            "trade_no": finalized.get("trade_no"),
            "duplicate_ingest": True,
            "validation": "PASS",
            "source_event_id": event.get("event_id"),
        }
    return None


def ingest_paper_execution_evidence(root: Path, evidence_path: str | Path, *, reviewer: str = "crypto-paper-ingest", recorded_at: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    evidence_path = Path(evidence_path)
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PaperIngestionError(f"paper evidence file is unreadable: {evidence_path}") from exc
    if not isinstance(payload, Mapping):
        raise PaperIngestionError("paper evidence root must be an object")
    payload = validate_paper_execution_evidence(payload, root)
    evidence_digest = _payload_digest(payload)
    store = LifecycleStore(root)
    existing = _existing_ingest(store, str(payload["execution_id"]), evidence_digest)
    if existing is not None:
        return existing
    try:
        artifact = EvidenceStore(root).ingest(evidence_path)
    except EvidenceStoreError as exc:
        raise PaperIngestionError(str(exc)) from exc
    execution_at = str(payload["execution_timestamp"])
    digest = hashlib.sha256(str(payload["execution_id"]).encode("utf-8")).hexdigest()[:24]
    draft_id = f"rec_{digest}"
    market_ref = payload["market_data_reference"]
    draft = store.create_draft(
        strategy_id=str(payload["strategy_id"]),
        rule_version=str(payload["rule_version"]),
        symbol=str(payload["instrument"]),
        direction=str(payload["side"]),
        intended_entry=float(payload["requested_price"]),
        stop_loss="not_observable",
        take_profit="not_observable",
        planned_rr="not_observable",
        timeframe_context=str(market_ref["timeframe"]),
        timeframe_trigger=str(market_ref["timeframe"]),
        market_context=f"public market data: {market_ref['provider_symbol']}",
        setup_tags=["crypto-paper-e2e"],
        confirmation_tags=[],
        thesis="deterministic test signal; paper execution only",
        chart_refs=[],
        created_at=execution_at,
        recording_timing="contemporaneous",
        trade_type="backtest",
        execution_environment="paper",
        execution_mode="PAPER",
        draft_id=draft_id,
        source_method="paper_trade",
        outcome_known_at_recording="unknown",
        recorded_at=recorded_at,
    )
    source_ref = str(payload["raw_evidence_reference"])
    store.attach_evidence(
        draft["draft_id"],
        {
            "source_ref": source_ref,
            "timing": "execution",
            "type": "paper_execution",
            "category": "entry",
            "sha256": artifact["sha256"],
            "locator": artifact["locator"],
            "media_type": artifact["media_type"],
            "byte_size": artifact["byte_size"],
            "original_filename": artifact["original_filename"],
            "captured_at": execution_at,
            "provider": payload["provider"],
            "execution_id": payload["execution_id"],
        },
        idempotency_key=f"paper-evidence:{payload['execution_id']}",
        recorded_at=recorded_at,
    )
    execution_metadata = dict(payload)
    execution_metadata["paper_evidence_digest"] = evidence_digest
    store.confirm_backtest_entry(
        draft["draft_id"],
        actual_entry=float(payload["fill_price"]),
        execution_at=execution_at,
        source_ref=source_ref,
        run_id=str(payload["crypto_run_id"]),
        parameters_ref=f"crypto://paper-engine/{payload['crypto_run_id']}",
        dataset_ref=f"{payload['provider']}://{market_ref['provider_symbol']}/{market_ref['timeframe']}",
        data_period={"from": str(market_ref.get("start_utc", execution_at)), "to": str(market_ref.get("end_utc", execution_at))},
        execution_metadata=execution_metadata,
        recorded_at=recorded_at,
        idempotency_key=f"paper-entry:{payload['execution_id']}",
    )
    finalized = store.finalize(draft["draft_id"], reviewer=reviewer, occurred_at=execution_at, recorded_at=recorded_at)
    return {
        "execution_id": payload["execution_id"],
        "draft_id": draft["draft_id"],
        "trade_id": finalized["trade_id"],
        "trade_no": finalized["trade_no"],
        "duplicate_ingest": False,
        "validation": "PASS",
        "evidence": artifact,
    }
