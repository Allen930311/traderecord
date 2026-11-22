"""NO_MATCH means 'no formal strategy matched' -- not 'inactive strategy' and not 'no review needed'."""

from __future__ import annotations

import json
from pathlib import Path

from resolver_helpers import no_match_observations
from test_historical_unresolved_live import _copy_operational_inputs
from test_registration_gate_boundary import _close
from test_registration_resolver_gate import _kwargs
from test_trade_view_v2 import _build, _row
from trading_os.operational_lifecycle import LifecycleStore
from trading_os.trade_review import review_registered_trade
from trading_os.unified_capture import capture_retrospective_live
from review_test_support import owner_confirmed_review

ROOT = Path(__file__).resolve().parents[1]


def _queue_refs(data):
    return {item["reference"] for item in data["review_queue"]}


def test_closed_unreviewed_no_match_enters_review_queue_and_leaves_after_review(tmp_path):
    _copy_operational_inputs(ROOT, tmp_path)
    store = LifecycleStore(tmp_path)
    off = capture_retrospective_live(store, **_kwargs(no_match_observations(), lifecycle_id="2506040949134235", sha="a"))
    trade_id = off["trade_id"]
    _close(store, off["draft_id"], trade_id, "d", r=-1.0, pnl=-10.0)

    data, analytics = _build(tmp_path)
    arow = next(r for r in analytics["canonical_read_model"] if r["trade_id"] == trade_id)
    assert arow["counted"] is False and arow["performance_eligible"] is True
    assert arow["exclusion_reason"] == "strategy_no_match"  # never "inactive_strategy"
    assert arow["review_status"] != "reviewed"

    view = _row(data, trade_id)
    assert view["strategy_display_name"] == "No matching strategy"
    assert view["strategy_resolution_status"] == "no_match"
    assert trade_id in _queue_refs(data)  # Registration != Review: still needs a review

    owner_confirmed_review(store, trade_id, {"rule_compliance": "not_observable", "diagnostic_class": "INSUFFICIENT"}, reviewed_at="2025-06-04T10:14:00+08:00", via_registered_writer=True)
    data, analytics = _build(tmp_path)
    assert trade_id not in _queue_refs(data)
    assert _row(data, trade_id)["review_status"] == "reviewed"
    assert _row(data, trade_id)["strategy_display_name"] == "No matching strategy"


def test_no_match_never_uses_inactive_strategy_semantics_in_the_ui(tmp_path):
    app_js = (ROOT / "trading_os" / "trade_view.py").read_text(encoding="utf-8")
    # the only place that maps a row to "Excluded · inactive strategy" keys off exclusion_reason == inactive_strategy
    assert 'row.exclusion_reason === "inactive_strategy"' in app_js
    assert "strategy_no_match" not in app_js.split("function isInactiveStrategy")[1].split("\n")[0]


def test_analytics_counts_do_not_contradict_each_other(tmp_path):
    _copy_operational_inputs(ROOT, tmp_path)
    store = LifecycleStore(tmp_path)
    off = capture_retrospective_live(store, **_kwargs(no_match_observations(), lifecycle_id="2506040949134235", sha="a"))
    _close(store, off["draft_id"], off["trade_id"], "d", r=-1.0, pnl=-10.0)
    _, analytics = _build(tmp_path)
    counts = analytics["counts"]
    assert counts["performance_observation_count"] == len(analytics["performance_observations"])
    assert counts["strategy_observation_count"] == counts["countable_observation_count"] == len(analytics["observations"])
    assert counts["performance_observation_count"] == counts["strategy_observation_count"] + 1  # the NO_MATCH trade
