"""chart_planner.match: plan + real fills -> app_drawn R check (Allen, 2026-11-23). Offline."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chart_planner"))
from match import match_plan  # noqa: E402

PLAN = {"symbol": "BTC-USDT", "direction": "long", "entry": 100.0, "stop": 98.0, "target": 106.0, "created_at": 1000}


def fill(side, price, qty, t, symbol="BTC-USDT"):
    return {"symbol": symbol, "side": side, "price": price, "qty": qty, "filled_at_ms": t * 1000}


def test_closed_trade_uses_real_fills_for_r():
    r = match_plan(PLAN, "1000-BTC-USDT", [fill("buy", 100, 1, 1100), fill("sell", 104, 1, 1500)])
    assert r["status"] == "closed"
    assert r["plan"]["realized_r"] == 2.0 and r["plan"]["planned_rr"] == 3.0
    assert r["plan"]["chart_price_evidence"]["stop"]["evidence_ref"] == "plan:1000-BTC-USDT"


def test_partial_fills_are_weighted_and_open_trade_has_no_realized_r():
    r = match_plan(PLAN, "p", [fill("buy", 100, 1, 1100), fill("buy", 102, 1, 1101)])
    assert r["status"] == "open" and r["provider_entry_price"] == 101.0
    assert "realized_r" not in r["plan"]


def test_fills_before_the_plan_never_match():
    r = match_plan(PLAN, "p", [fill("buy", 100, 1, 900), fill("sell", 104, 1, 1500)])
    assert r["status"] == "ambiguous"  # only the exit is after the plan: not matched


def test_other_symbol_ignored_and_scale_in_after_exit_is_ambiguous_not_guessed():
    assert match_plan(PLAN, "p", [fill("buy", 1, 1, 1100, "ETH-USDT")])["status"] == "no_fill_after_plan"
    r = match_plan(PLAN, "p", [fill("buy", 100, 2, 1100), fill("sell", 104, 1, 1200), fill("buy", 103, 1, 1300)])
    assert r["status"] == "ambiguous"


def test_short_plan_matches_sell_entry():
    plan = {**PLAN, "direction": "short", "entry": 100.0, "stop": 102.0, "target": 94.0}
    r = match_plan(plan, "p", [fill("sell", 100, 1, 1100), fill("buy", 96, 1, 1200)])
    assert r["status"] == "closed" and r["plan"]["realized_r"] == 2.0
