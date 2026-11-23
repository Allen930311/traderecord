"""Match a saved app_drawn plan to the provider's real fills and run the registration R check.

Allen, 2026-11-23. Pure and read-only: takes a plan (as saved by server.save_plan) and normalised fills
(fills.normalize), and feeds trading_os.live_chart_plan.derive_live_chart_plan, the single R calculation.
Only fills AFTER the plan was saved are considered, so a plan can never be matched to a trade that was
opened before it was drawn (the app_drawn rule).

Matching rule (simple on purpose): fills of the plan's symbol after the plan, entry = the entry-side
fills before the first opposite fill (qty-weighted average, first fill time), exit = the opposite fills
until the entered quantity is closed. Anything else (scale-ins after a partial exit, hedge mode with
both sides open) is reported as `ambiguous`, never guessed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trading_os.live_chart_plan import LiveChartPlanError, derive_live_chart_plan  # noqa: E402


def _avg(rows: list[dict]) -> float:
    q = sum(r["qty"] for r in rows)
    return sum(r["price"] * r["qty"] for r in rows) / q


def match_plan(plan: dict, plan_id: str, all_fills: list[dict]) -> dict:
    long = plan["direction"] == "long"
    entry_side, exit_side = ("buy", "sell") if long else ("sell", "buy")
    after = [f for f in all_fills
             if f.get("symbol") == plan["symbol"] and f.get("price") and f.get("qty")
             and f.get("filled_at_ms") and f["filled_at_ms"] > plan["created_at"] * 1000]
    after.sort(key=lambda f: f["filled_at_ms"])
    if not after:
        return {"status": "no_fill_after_plan"}
    if after[0]["side"] != entry_side:
        return {"status": "ambiguous", "reason": "first fill after the plan is not on the plan's entry side"}
    entries, exits = [], []
    for f in after:
        if f["side"] == entry_side and not exits:
            entries.append(f)
        elif f["side"] == exit_side:
            exits.append(f)
        else:
            return {"status": "ambiguous", "reason": "entry-side fill after a partial exit (scale-in)"}
        if exits and sum(e["qty"] for e in exits) >= sum(e["qty"] for e in entries) - 1e-12:
            break
    in_qty, out_qty = sum(e["qty"] for e in entries), sum(e["qty"] for e in exits)
    if out_qty > in_qty + 1e-9:
        return {"status": "ambiguous", "reason": "exit quantity exceeds entry quantity (reversal)"}
    closed = bool(exits) and abs(out_qty - in_qty) <= 1e-9
    entry_price = _avg(entries)
    kwargs = dict(
        direction=plan["direction"],
        provider_entry_price=entry_price,
        provider_entry_time=entries[0]["filled_at_ms"] / 1000,
    )
    if closed:
        kwargs["provider_exit_price"] = _avg(exits)
    ref = f"plan:{plan_id}"
    chart = {k: {"value": plan[k], "provenance": "app_drawn", "evidence_ref": ref}
             for k in ("entry", "stop", "target")}
    chart["plan_created_at"] = plan["created_at"]
    try:
        derived = derive_live_chart_plan(chart, **kwargs)
    except LiveChartPlanError as e:
        return {"status": "rejected", "reason": str(e)}
    return {"status": "closed" if closed else "open", "provider_entry_price": entry_price,
            "provider_exit_price": kwargs.get("provider_exit_price"),
            "entry_fills": len(entries), "exit_fills": len(exits), "plan": derived}
