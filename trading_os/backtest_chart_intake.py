"""Agent-facing BACKTEST chart intake, not a new canonical writer.

This is a pure preparation/validation step for existing Register SOP. The Agent,
not the program, interprets the image and enters annotated or chart-inferred
prices, with supporting evidence locators and explicit quality. No state is
written and this module never allocates a BT-number or claims a fill.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

SCHEMA = "backtest-chart-intake.v1"
PROVENANCE = {"owner_explicit", "chart_annotation", "chart_estimate", "unknown"}
_ROLES = {"entry_chart", "exit_chart"}


class ChartIntakeError(ValueError):
    """Reject a malformed Agent interpretation, not a chart judgment."""


def _decimal(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ChartIntakeError(f"{name} must be numeric or null")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ChartIntakeError(f"{name} must be numeric or null") from exc
    if not result.is_finite() or result <= 0:
        raise ChartIntakeError(f"{name} must be a finite positive price")
    return result


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        # Never fabricate a time from a chart's date or an upload timestamp.
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return bool(value and ("T" in value or " " in value))


def prepare_backtest_chart_intake(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate Agent-filled chart interpretations and derive tentative plan R.

    Agent supplies `prices.{entry,stop,target}` objects with each
    `value`, `provenance`, and `evidence_ref`. We neither read charts nor
    infer values from file names. The Writer remains the only registration
    authority. Chart-estimated levels can be used for a research preview;
    they must never be represented as provider-verified fills.
    """
    if data.get("schema_version") != SCHEMA:
        raise ChartIntakeError("schema_version must be backtest-chart-intake.v1")
    if data.get("mode") != "BACKTEST" or data.get("source_method") != "tradingview_replay":
        raise ChartIntakeError("chart intake is TradingView Replay BACKTEST only")
    strategy = data.get("strategy")
    if not isinstance(strategy, Mapping) or not isinstance(strategy.get("strategy_id"), str):
        raise ChartIntakeError("strategy identity required")
    if data.get("direction") not in {"long", "short"}:
        raise ChartIntakeError("direction must be long or short")
    owner_text = data.get("owner_verbatim")
    if not isinstance(owner_text, str) or not owner_text.strip():
        raise ChartIntakeError("owner_verbatim must preserve actual Owner words")
    images = data.get("images")
    if not isinstance(images, list) or not images:
        raise ChartIntakeError("exact GitHub original-image references required")
    roles = set()
    refs = set()
    for item in images:
        if not isinstance(item, Mapping) or item.get("role") not in _ROLES:
            raise ChartIntakeError("image role must be entry_chart or exit_chart")
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise ChartIntakeError("image ref must identify a durable original")
        if ref in refs:
            raise ChartIntakeError("duplicate original-image ref")
        refs.add(ref)
        roles.add(item["role"])
    prices = data.get("prices")
    if not isinstance(prices, Mapping):
        raise ChartIntakeError("prices object required")
    normalized = {}
    for name in ("entry", "stop", "target"):
        item = prices.get(name)
        if not isinstance(item, Mapping):
            raise ChartIntakeError(f"prices.{name} needs value, provenance and evidence_ref")
        provenance = item.get("provenance")
        if provenance not in PROVENANCE:
            raise ChartIntakeError(f"prices.{name} provenance invalid")
        value = _decimal(item.get("value"), f"prices.{name}")
        if provenance == "unknown" and value is not None:
            raise ChartIntakeError(f"prices.{name}: unknown cannot carry a price")
        if provenance != "unknown" and value is None:
            raise ChartIntakeError(f"prices.{name}: known price requires value")
        ev = item.get("evidence_ref")
        if provenance in {"chart_annotation", "chart_estimate"} and ev not in refs:
            raise ChartIntakeError(f"prices.{name}: image evidence_ref must be one of the attached original images")
        normalized[name] = {"value": str(value) if value is not None else None,
                            "provenance": provenance, "evidence_ref": ev}
    entry = _decimal(normalized["entry"]["value"], "entry")
    stop = _decimal(normalized["stop"]["value"], "stop")
    target = _decimal(normalized["target"]["value"], "target")
    rr = None
    if entry is not None and stop is not None and target is not None:
        risk = (entry - stop) if data["direction"] == "long" else (stop - entry)
        reward = (target - entry) if data["direction"] == "long" else (entry - target)
        if risk <= 0 or reward <= 0:
            raise ChartIntakeError("entry/stop/target contradict the intended trade direction")
        rr = str((reward / risk).quantize(Decimal("0.001")))
    simulated_at = data.get("simulated_entry_at")
    if simulated_at is not None and not _timestamp(simulated_at):
        raise ChartIntakeError("simulated_entry_at must be a known ISO datetime or null")
    # This is an interpretation/plan-quality preview. Even a supplied ISO time
    # does not prove an execution. Only the existing Writer can confirm one.
    chart_derived = any(i["provenance"] in {"chart_annotation", "chart_estimate"} for i in normalized.values())
    return {
        "schema_version": SCHEMA,
        "strategy": dict(strategy),
        "mode": "BACKTEST",
        "source_method": "tradingview_replay",
        "direction": data["direction"],
        "owner_verbatim": owner_text,
        "images": [dict(i) for i in images],
        "prices": normalized,
        "planned_rr_from_chart": rr,
        "price_quality": "chart_derived" if chart_derived else "owner_declared_or_unknown",
        "simulated_entry_at": simulated_at,
        "simulated_entry_time_status": "known" if simulated_at else "unknown",
        "exit_evidence_role_present": "exit_chart" in roles,
        "outcome": data.get("outcome", "unknown"),
        "owner_plan_variant": data.get("owner_plan_variant", "A"),
        "actual_exit_verified": False,
        "status": "AGENT_PREVIEW_NEEDS_WRITER",
        "canonical_bt_id": None,
        "performance_counted": False,
    }
