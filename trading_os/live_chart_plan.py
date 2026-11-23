"""Derive a LIVE trade's plan and R from the Owner's chart, never from a fixed amount.

Every chart the Owner supplies is the trade's plan. The Agent reads the drawn
Entry / SL / TP and passes them here with the attached image they came from.
1R is always the distance from the chart's Entry to its SL, so realized R is the
provider's actual price move measured in that distance. This is a pure helper:
it reads no image, writes no state and never assigns a T-ID.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

R_BASIS = "actual_entry_to_chart_stop"
PROVENANCE = {"chart_annotation", "chart_estimate", "app_drawn"}
# app_drawn (Allen, 2026-11-23): levels the Owner drew in the chart planner app BEFORE the trade.
# Evidence is the saved plan id, not an attached image, so the screenshot rules below stay as they were;
# instead the plan must be provably saved before the provider entry (plan_created_at < provider_entry_time).
APP_DRAWN = "app_drawn"


class LiveChartPlanError(ValueError):
    """Reject a malformed or self-contradicting chart interpretation."""


def _price(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise LiveChartPlanError(f"{name} must be a positive price")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiveChartPlanError(f"{name} must be a positive price") from exc
    if not result.is_finite() or result <= 0:
        raise LiveChartPlanError(f"{name} must be a finite positive price")
    return result


def _require_drawn_before_entry(created_at: Any, entry_time: Any) -> None:
    """app_drawn rule (Allen, 2026-11-23): a plan only counts if it was saved before the fill."""
    for label, value in (("plan_created_at", created_at), ("provider_entry_time", entry_time)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LiveChartPlanError(f"app_drawn needs {label} as epoch seconds")
    if created_at >= entry_time:
        raise LiveChartPlanError("app_drawn plan was saved at or after the provider entry; use a screenshot instead")


def measure_r(
    direction: str,
    entry: Any,
    stop: Any,
    target: Any | None = None,
    exit_price: Any | None = None,
) -> dict[str, Decimal | None]:
    """The single R calculation: 1R is entry -> stop; planned_rr and realized_r are measured in it.

    Returns the risk and reward distances (direction-adjusted, so a positive number is on the
    expected side) and the 3-decimal ratios. A ratio is None when its inputs were not given or
    the risk is not positive; callers decide whether that is an error.
    """

    if direction not in {"long", "short"}:
        raise LiveChartPlanError("direction must be long or short")
    sign = 1 if direction == "long" else -1
    entry_d, stop_d = _price(entry, "entry"), _price(stop, "stop")
    risk = (entry_d - stop_d) * sign
    reward = (_price(target, "target") - entry_d) * sign if target is not None else None
    move = (_price(exit_price, "exit price") - entry_d) * sign if exit_price is not None else None
    quantum = Decimal("0.001")

    def ratio(value: Decimal | None) -> Decimal | None:
        return None if value is None or risk <= 0 else (value / risk).quantize(quantum)

    return {"risk": risk, "reward": reward, "planned_rr": ratio(reward), "realized_r": ratio(move)}


def derive_live_chart_plan(
    chart: Mapping[str, Any],
    *,
    direction: str,
    provider_entry_price: Any | None = None,
    provider_exit_price: Any | None = None,
    attached_refs: set[str] | None = None,
    provider_entry_time: Any | None = None,
) -> dict[str, Any]:
    """Return canonical_capture plan fields derived from chart prices.

    `chart` holds `entry`, `stop` and `target`, each `{value, provenance,
    evidence_ref}`. A missing level must be omitted from the call by passing
    nothing here: the Agent keeps the field `unknown` instead of guessing.
    `realized_r` is returned only for a closed trade (provider exit supplied).
    """
    if direction not in {"long", "short"}:
        raise LiveChartPlanError("direction must be long or short")
    sign = 1 if direction == "long" else -1
    levels: dict[str, Decimal] = {}
    evidence: dict[str, dict[str, str]] = {}
    for name in ("entry", "stop", "target"):
        item = chart.get(name)
        if not isinstance(item, Mapping):
            raise LiveChartPlanError(f"chart.{name} needs value, provenance and evidence_ref")
        if item.get("provenance") not in PROVENANCE:
            raise LiveChartPlanError(f"chart.{name} provenance must be chart_annotation, chart_estimate or app_drawn")
        ref = item.get("evidence_ref")
        if item["provenance"] == APP_DRAWN:
            if not isinstance(ref, str) or not ref.startswith("plan:") or not ref[5:].strip():
                raise LiveChartPlanError(f"chart.{name} app_drawn needs evidence_ref 'plan:<saved plan id>'")
            _require_drawn_before_entry(chart.get("plan_created_at"), provider_entry_time)
        else:
            if not isinstance(ref, str) or not ref.strip():
                raise LiveChartPlanError(f"chart.{name} needs the attached image evidence_ref")
            if attached_refs is not None and ref not in attached_refs:
                raise LiveChartPlanError(f"chart.{name} evidence_ref is not an attached image")
        levels[name] = _price(item.get("value"), f"chart.{name}")
        evidence[name] = {"provenance": item["provenance"], "evidence_ref": ref}

    entry = (_price(provider_entry_price, "provider_entry_price")
             if provider_entry_price is not None else levels["entry"])
    measured = measure_r(direction, entry, levels["stop"], levels["target"])
    risk, reward = measured["risk"], measured["reward"]
    if risk <= 0 or reward <= 0:
        raise LiveChartPlanError("chart entry/stop/target contradict the trade direction")

    plan: dict[str, Any] = {
        "intended_entry": float(levels["entry"]),
        "risk_entry": float(entry),
        "stop_loss": float(levels["stop"]),
        "take_profit": float(levels["target"]),
        "planned_rr": float(measured["planned_rr"]),
        "r_basis": R_BASIS,
        "risk_distance": float(risk),
        "chart_price_evidence": evidence,
    }
    if provider_exit_price is not None:
        if provider_entry_price is None:
            raise LiveChartPlanError("realized R needs the provider entry price")
        plan["realized_r"] = float(measure_r(direction, entry, levels["stop"], exit_price=provider_exit_price)["realized_r"])
        plan["realized_r_authority"] = "deterministic_execution_calculation"
    return plan
