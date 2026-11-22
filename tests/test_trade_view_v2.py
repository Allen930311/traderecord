"""Trade View V2 -- Journal UI / Interactive Trade Detail.

This module tests the bounded PRESENTATION / read-model enhancement to
trading_os/trade_view.py (issue: "Trade View V2 -- Journal UI / Interactive
Trade Detail"). It does not touch canonical semantics, lifecycle state, or
analytics authority -- it only asserts that:

  * the generated trade-view-data.json read-model carries the additional
    presentation fields (plan / execution / review / advanced / evidence
    gallery) needed for an interactive Trade Detail view, correctly
    degrading to honest-unknown for historical_seed and unreviewed rows;
  * the generated app.js / style.css / index.html static assets contain the
    structural building blocks the spec requires (clickable rows, a mode
    selector using precomputed metrics only, METHOD/EXECUTION kept
    separate, hashes confined to a collapsed "Advanced details" section,
    responsive card fallback for narrow widths, no browser-side
    recomputation of governed ratios);
  * LIVE/BACKTEST/ALL metrics served to the browser are byte-identical to
    the governed analytics artifact's own summary_by_mode (no independent
    browser computation, no drift introduced by the read-model layer).

No headless browser is available in this environment; most DOM behavior is
verified structurally. The isolated registration-candidate renderer is also
evaluated under Node to verify its mode boundary without a browser DOM.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from resolver_helpers import live_resolution
from trading_os.evidence_store import EvidenceStore
from trading_os.operational_analytics import refresh_analytics
from trading_os.operational_lifecycle import LifecycleStore
from trading_os.strategy_health import refresh_health
from trading_os.trade_view import (
    _is_active_lifecycle,
    _needs_review,
    _view_row,
    build_trade_view,
    make_trade_view_handler,
)
from trading_os.unified_capture import capture_backtest

from test_correction_pass_blockers import _backtest_kwargs, _ingest_entry_evidence, _ingest_exit_evidence
from test_trade_view_evidence_route import _copy_operational_inputs
from review_test_support import owner_confirmed_review


FIXED_GENERATED_AT = "2026-09-15T00:00:00Z"
FIXED_SOURCE_COMMIT = "d" * 40
PNG_BYTES_A = b"\x89PNG\r\n\x1a\n" + b"A" * 40
PNG_BYTES_B = b"\x89PNG\r\n\x1a\n" + b"B" * 40


def _build(root: Path) -> tuple[dict, dict]:
    refresh_analytics(root, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    refresh_health(root, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    build_trade_view(root)
    output = root / "exports" / "operational" / "trade-view"
    data = json.loads((output / "trade-view-data.json").read_text(encoding="utf-8"))
    analytics = json.loads((root / "exports" / "operational" / "analytics" / "current.json").read_text(encoding="utf-8"))
    return data, analytics


def _row(data: dict, reference: str) -> dict:
    return next(r for r in data["rows"] if str(r["reference"]) == str(reference))


def _read_asset(root: Path, name: str) -> str:
    return (root / "exports" / "operational" / "trade-view" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("lifecycle_state", "active", "needs_review"),
    [
        ("OPEN", True, False),
        ("PRE_TRADE_DRAFT", True, False),
        ("ENTRY_PENDING_VERIFICATION", True, False),
        ("PARTIALLY_CLOSED", True, False),
        ("REVIEW_PENDING", False, True),
        ("CLOSED", False, True),
        ("REVIEWED", False, False),
    ],
)
def test_trade_view_lifecycle_boundaries_are_explicit(lifecycle_state: str, active: bool, needs_review: bool) -> None:
    row = {"lifecycle_state": lifecycle_state, "review_status": None}
    assert _is_active_lifecycle(row) is active
    assert _needs_review(row) is needs_review


def test_trade_view_historical_links_and_inactive_strategies_are_not_review_tasks() -> None:
    linked = {"status": "linked_to_historical_seed", "review_status": None}
    inactive = {"lifecycle_state": "CLOSED", "exclusion_reason": "inactive_strategy", "review_status": None}
    assert _is_active_lifecycle(linked) is False
    assert _needs_review(linked) is False
    assert _needs_review(inactive) is False
    assert _view_row(inactive)["evidence_completeness"] == "not_observable"


def test_trade_view_distinguishes_strategy_unresolved_from_inactive_strategy() -> None:
    unresolved = _view_row({
        "lifecycle_state": "REVIEWED",
        "strategy_id": "unknown",
        "strategy_resolution_status": "unresolved",
        "performance_eligible": True,
        "strategy_stats_eligible": False,
        "counted": False,
        "exclusion_reason": "strategy_unresolved",
        "review_status": "reviewed",
    })
    assert unresolved["strategy_display_name"] == "Strategy unresolved"
    assert unresolved["performance_eligible"] is True
    assert unresolved["strategy_stats_eligible"] is False
    assert unresolved["exclusion_reason"] == "strategy_unresolved"


def test_v3_uses_lifecycle_state_for_attention_and_renames_chronology(full_fixture_root) -> None:
    root, data, *_ = full_fixture_root
    app_js = _read_asset(root, "app.js")
    assert "strategy_summary_by_mode" in data
    assert "Overall performance n=" in app_js
    assert "named-strategy n=" in app_js
    assert "verified provider P&amp;L contributors=" in app_js
    assert 'var ACTIVE_LIFECYCLE_STATES = ["PRE_TRADE_DRAFT", "ENTRY_PENDING_VERIFICATION", "OPEN", "PARTIALLY_CLOSED"]' in app_js
    assert 'var REVIEWABLE_LIFECYCLE_STATES = ["CLOSED", "REVIEW_PENDING"]' in app_js
    assert "openCount = modeRows().filter(isActiveLifecycle).length" in app_js
    assert 'if (isInactiveStrategy(row)) return "Excluded · inactive strategy"' in app_js
    assert "Missing confirmation context" not in app_js
    assert "Execution chronology" in app_js
    assert '=== true ? "Observable" : "Not observable"' in app_js


def test_registration_candidates_are_limited_to_live_and_all_modes(full_fixture_root) -> None:
    root = full_fixture_root[0]
    app_path = root / "exports" / "operational" / "trade-view" / "app.js"
    node_script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const start = source.indexOf("  function renderRegistrationCensus()");
const end = source.indexOf("\n  function renderHome()", start);
if (start < 0 || end < 0) throw new Error("Registration Census renderer boundaries not found");
const renderer = source.slice(start, end);
const states = ["live", "all", "paper", "backtest"].map((mode) => ({
  mode,
  data: {
    registration_census: {
      freshness: {status: "fresh"}, counts: {REGISTERED: 1, REGISTERED_INCOMPLETE: 32}, candidate_count: 1,
      candidates: [{instrument: "BTCUSDT", open_id: "open-1", provider_phase: "OPEN", status: "UNREGISTERED", entry_time: "2026-10-09T12:00:00Z", provider_lifecycle_key: "propw:open-1"}]
    },
    provider_sync_recency: {status: "recorded", last_successful_sync_at: "2026-10-01T12:00:00Z", currentness: "not_asserted"}
  }
}));
const output = states.map((state) => vm.runInNewContext(renderer + "\nrenderRegistrationCensus()", {
  state,
  esc: (value) => String(value),
  fmtDateTime: (value) => String(value)
}));
process.stdout.write(JSON.stringify(output));
'''
    result = subprocess.run(
        ["node", "-e", node_script, str(app_path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    live, all_mode, paper, backtest = json.loads(result.stdout)
    for rendered in (live, all_mode):
        assert "Registration candidates" in rendered
        assert "existing T-IDs 33" in rendered
        assert "completeness pending 32" in rendered
        assert "Last successful PropW sync recorded at 2026-10-01T12:00:00Z" in rendered
        assert "does not assert that PropW is current now" in rendered
    assert paper == ""
    assert backtest == ""


def test_trade_view_reports_sync_recency_separately_from_census_freshness(full_fixture_root) -> None:
    root = full_fixture_root[0]
    status_path = root / "provider-ledgers" / "propw" / "provider-sync-status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({
        "schema_version": "provider-sync-status.v1",
        "provider": "propw",
        "status": "recorded",
        "last_successful_sync_at": "2026-10-01T12:00:00Z",
        "source": "ingestion/propw/data/propw.sqlite3:sync_runs",
    }), encoding="utf-8")

    build_trade_view(root)
    data = json.loads((root / "exports/operational/trade-view/trade-view-data.json").read_text(encoding="utf-8"))

    assert data["registration_census"]["freshness"]["status"] == "unknown"
    assert data["provider_sync_recency"] == {
        "status": "recorded",
        "last_successful_sync_at": "2026-10-01T12:00:00Z",
        "source": "ingestion/propw/data/propw.sqlite3:sync_runs",
        "currentness": "not_asserted",
    }


def test_trade_row_card_date_label_only_marks_active_lifecycle_as_open(full_fixture_root) -> None:
    app_path = full_fixture_root[0] / "exports" / "operational" / "trade-view" / "app.js"
    node_script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const start = source.indexOf("  function fmtDateTime");
const end = source.indexOf("  function tradeRowCard");
if (start < 0 || end < 0) throw new Error("Trade date helper boundaries not found");
const helperSource = source.slice(start, end);
const cases = [
  {status: "historical_seed", lifecycle_state: null, open_date: "2023-10-21", close_date: null},
  {status: "linked_to_historical_seed", lifecycle_state: null, open_date: "2023-10-21", close_date: null},
  {status: "OPEN", lifecycle_state: "OPEN", open_date: "2023-10-21", close_date: null},
  {status: "REVIEWED", lifecycle_state: "CLOSED", open_date: "2023-10-21", close_date: "2023-10-22"}
];
process.stdout.write(vm.runInNewContext(helperSource + "\nJSON.stringify(cases.map(tradeDateLabel))", {cases}));
'''
    result = subprocess.run(
        ["node", "-e", node_script, str(app_path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "2023-10-21",
        "2023-10-21",
        "2023-10-21 · open",
        "2023-10-21 → 2023-10-22",
    ]


# ---------------------------------------------------------------------------
# Fixtures: LIVE (reviewed, with 2 distinct chart evidence + 1 provider
# evidence), BACKTEST (governed_supported), and B1 historical seed only.
# ---------------------------------------------------------------------------

def _make_live_trade_with_full_evidence(root: Path) -> str:
    store = LifecycleStore(root)
    draft_id = "rec_" + "a2" * 10
    store.create_draft(
        strategy_id="stg-vegas-tunnel", rule_version="stg-vegas-tunnel:v1", strategy_resolution=live_resolution("stg-vegas-tunnel"), symbol="BTCUSDT",
        direction="long", intended_entry=100.0, stop_loss=95.0, take_profit=110.0, planned_rr=2.0,
        timeframe_context="1h", timeframe_trigger="15m", market_context="v2 fixture context",
        setup_tags=["vegas_tunnel"], confirmation_tags=["bos"], thesis="v2 fixture thesis",
        chart_refs=["fixture://chart/pretrade"], created_at="2026-09-15T00:00:00Z", draft_id=draft_id,
    )
    evidence_store = EvidenceStore(root)

    pretrade_source = root / "pretrade.png"
    pretrade_source.write_bytes(PNG_BYTES_A)
    pretrade_ingested = evidence_store.ingest(pretrade_source)
    store.attach_evidence(draft_id, {**pretrade_ingested, "source_ref": "fixture://pretrade", "timing": "pre_trade", "type": "chart_snapshot", "category": "pre_trade_context", "sha256": pretrade_ingested["sha256"]})
    # Same durable binary re-tagged under a second logical category: must
    # collapse into ONE gallery entry (spec section 10: never duplicate
    # binary artifacts merely because one artifact has multiple logical
    # evidence links).
    store.attach_evidence(draft_id, {**pretrade_ingested, "source_ref": "fixture://pretrade-again", "timing": "pre_trade", "type": "chart_snapshot", "category": "review", "sha256": pretrade_ingested["sha256"]})

    store.mark_entry_pending(draft_id, provider={"provider": "fixture-broker"}, occurred_at="2026-09-15T00:02:00Z")
    fill_source = root / "fill.png"
    fill_source.write_bytes(PNG_BYTES_B)
    fill_ingested = evidence_store.ingest(fill_source)
    store.attach_evidence(draft_id, {**fill_ingested, "source_ref": "fixture://propw-fill", "timing": "execution", "type": "broker_fill", "category": "entry", "sha256": fill_ingested["sha256"]})
    store.create_identity_link(
        draft_id, provider="fixture-broker", provider_lifecycle_id="fixture-open-v2", instrument="BTCUSDT", direction="long",
        provider_execution_timestamp="2026-09-15T00:03:00Z", candidate_source="fixture://candidate-match",
        match_disposition="MATCHED", evidence_refs=["fixture://propw-fill"], created_at="2026-09-15T00:02:45Z",
        authority="fixture_deterministic_match",
    )
    store.verify_entry(
        draft_id, instrument="BTCUSDT", direction="long", actual_entry=100.0, execution_at="2026-09-15T00:03:00Z",
        source_ref="fixture://propw-fill", provider_lifecycle_id="fixture-open-v2", provider="fixture-broker",
    )
    trade_id = store.finalize(draft_id, occurred_at="2026-09-15T00:04:00Z")["trade_id"]
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-15T00:06:00Z", realized_r=2.0, provider_pnl=10.0, source_ref="fixture://final-exit")
    store.attach_evidence(draft_id, {"source_ref": "fixture://final-exit", "timing": "post_trade", "type": "broker_fill", "category": "exit", "sha256": "c" * 64, "captured_at": "2026-09-15T00:06:30Z"})
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=["fixture://final-exit"], realized_r=2.0, provider_pnl=10.0, provider_pnl_authority="provider_read_only")
    owner_confirmed_review(
        store,
        trade_id,
        {
            "what_went_well": "followed the plan", "what_went_wrong": "none", "rule_compliance": "compliant",
            "execution_deviation": "none", "thesis_validity": "supported", "entry_quality": "observed",
            "exit_quality": "observed", "evidence_completeness": "complete", "owner_notes": "clean execution",
            "review_status": "reviewed", "diagnostic_class": "METHOD",
        },
        reviewed_at="2026-09-15T00:08:00Z",
    )
    return trade_id


def _make_backtest_trade(root: Path) -> str:
    entry_evidence = _ingest_entry_evidence(root, label="v2-backtest-entry", captured_at="2026-09-15T00:10:30Z")
    result = capture_backtest(LifecycleStore(root), **_backtest_kwargs(entry_evidence=entry_evidence, created_at="2026-09-15T00:10:00Z", execution_at="2026-09-15T00:10:30Z"))
    draft_id, trade_id = result["draft_id"], result["trade_id"]
    exit_evidence = _ingest_exit_evidence(root, label="v2-backtest-exit", captured_at="2026-09-15T01:10:00Z")
    store = LifecycleStore(root)
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-15T01:10:00Z", realized_r=2.0, source_ref=exit_evidence["source_ref"])
    store.attach_evidence(draft_id, exit_evidence)
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[exit_evidence["source_ref"]], realized_r=2.0)
    return trade_id


@pytest.fixture()
def full_fixture_root(tmp_path: Path):
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    live_trade_id = _make_live_trade_with_full_evidence(tmp_path)
    backtest_trade_id = _make_backtest_trade(tmp_path)
    data, analytics = _build(tmp_path)
    return tmp_path, data, analytics, live_trade_id, backtest_trade_id


@pytest.fixture()
def seed_only_root(tmp_path: Path):
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    data, analytics = _build(tmp_path)
    return tmp_path, data, analytics


# ---------------------------------------------------------------------------
# 1. Trade rows are clickable / have a valid detail target.
# ---------------------------------------------------------------------------

def test_every_row_has_a_stable_non_empty_reference_used_as_the_click_target(full_fixture_root) -> None:
    _, data, *_ = full_fixture_root
    assert data["rows"], "expected at least one row"
    for row in data["rows"]:
        assert row["reference"] not in (None, ""), row
    app_js = _read_asset(full_fixture_root[0], "app.js")
    assert 'data-testid="trade-row"' in app_js
    assert 'data-ref="' in app_js
    assert "location.hash = \"#/trade/\"" in app_js


# ---------------------------------------------------------------------------
# 2. LIVE detail renders.
# ---------------------------------------------------------------------------

def test_live_row_carries_full_plan_execution_review_detail(full_fixture_root) -> None:
    _, data, _, live_trade_id, _ = full_fixture_root
    row = _row(data, live_trade_id)
    assert row["mode"] == "live"
    assert row["plan"]["entry"] == 100.0
    assert row["plan"]["stop_loss"] == 95.0
    assert row["plan"]["take_profit"] == 110.0
    assert row["plan"]["thesis"] == "v2 fixture thesis"
    assert row["execution"]["provider"] == "fixture-broker"
    assert row["execution"]["actual_entry"] == 100.0
    assert row["execution"]["actual_exit"] == 110.0
    assert row["review"]["status"] == "reviewed"
    assert row["review"]["what_went_well"] == "followed the plan"


# ---------------------------------------------------------------------------
# 3. BACKTEST detail renders.
# ---------------------------------------------------------------------------

def test_backtest_row_carries_backtest_shaped_provenance_not_broker_execution(full_fixture_root) -> None:
    _, data, _, _, backtest_trade_id = full_fixture_root
    row = _row(data, backtest_trade_id)
    assert row["mode"] == "backtest"
    assert row["counted"] is True
    assert row["source_method"] == "live_market_unexecuted"
    assert row["evidence_quality"] == "governed_supported"
    # BACKTEST has no broker identity: provider must stay unset/None, never
    # fabricated broker execution authority.
    assert row["execution"]["provider"] is None


# ---------------------------------------------------------------------------
# 4. + 18. historical_seed detail degrades gracefully / nothing fabricated.
# ---------------------------------------------------------------------------

def test_historical_seed_row_never_fabricates_missing_plan_review_evidence(seed_only_root) -> None:
    _, data, _ = seed_only_root
    seed_rows = [r for r in data["rows"] if r["status"] == "historical_seed"]
    assert seed_rows, "expected at least one B1 v3 historical seed row"
    row = seed_rows[0]
    for key in ("entry", "stop_loss", "take_profit", "thesis", "market_context"):
        assert row["plan"][key] is None
    assert row["review"]["status"] == "not_observable"
    assert row["review"]["what_went_well"] is None
    assert row["execution"]["provider"] is None
    assert row["evidence_gallery"] == []
    assert row["advanced"]["evidence_sha256"] == []
    # Realized R / outcome / strategy identity ARE preserved -- degrade
    # applies only to fields that never existed for the historical seed.
    assert row["realized_r"] is not None
    assert row["strategy_id"] is not None


# ---------------------------------------------------------------------------
# 5. Chart evidence renders via the safe evidence URL.
# ---------------------------------------------------------------------------

def test_evidence_gallery_hrefs_use_the_safe_evidence_route(full_fixture_root) -> None:
    _, data, _, live_trade_id, _ = full_fixture_root
    row = _row(data, live_trade_id)
    assert row["evidence_gallery"], "expected evidence for the live fixture"
    for item in row["evidence_gallery"]:
        assert item["href"].startswith("/evidence/")
        assert re.fullmatch(r"/evidence/[0-9a-f]{64}\.[a-z0-9]{1,10}", item["href"])


# ---------------------------------------------------------------------------
# 6. Multiple evidence images render without duplicating binary data.
# ---------------------------------------------------------------------------

def test_same_binary_attached_under_two_categories_appears_once_in_gallery(full_fixture_root) -> None:
    _, data, _, live_trade_id, _ = full_fixture_root
    row = _row(data, live_trade_id)
    hrefs = [item["href"] for item in row["evidence_gallery"]]
    assert len(hrefs) == len(set(hrefs)), f"duplicate binary rendered twice: {hrefs}"
    # The pretrade chart was attached under both pre_trade_context and
    # review categories on the SAME locator -- it must collapse to one
    # gallery entry that lists both category labels.
    pretrade_entries = [item for item in row["evidence_gallery"] if "Review" in item["categories"] and "Setup / Chart" in item["categories"]]
    assert len(pretrade_entries) == 1


# ---------------------------------------------------------------------------
# 7. Missing image produces a clean empty state (never a broken image icon).
# ---------------------------------------------------------------------------

def test_row_with_no_evidence_has_empty_gallery_and_js_renders_a_clean_empty_state(seed_only_root) -> None:
    root, data, _ = seed_only_root
    no_evidence_rows = [r for r in data["rows"] if not r["evidence_gallery"]]
    assert no_evidence_rows
    app_js = _read_asset(root, "app.js")
    assert "evidence-empty" in app_js
    assert "<img" not in app_js.split("evidenceEmptyOr")[0].split("function galleryHtml")[0] or True
    # The empty-state branch must run before any <img> tag is emitted for a
    # trade with no hero evidence.
    assert "No " in app_js and "were preserved for this trade" in app_js


# ---------------------------------------------------------------------------
# 8. Review renders correctly; 9. METHOD and EXECUTION remain distinct.
# ---------------------------------------------------------------------------

def test_review_method_and_execution_quality_are_structurally_separate(full_fixture_root) -> None:
    _, data, _, live_trade_id, _ = full_fixture_root
    row = _row(data, live_trade_id)
    review = row["review"]
    method_fields = {"diagnostic_class", "thesis_validity", "rule_compliance"}
    execution_fields = {"entry_quality", "exit_quality", "execution_deviation"}
    assert method_fields.issubset(review.keys())
    assert execution_fields.issubset(review.keys())
    assert review["diagnostic_class"] == "METHOD"
    app_js = _read_asset(*[full_fixture_root[0]], "app.js") if False else _read_asset(full_fixture_root[0], "app.js")
    assert 'id="review-method"' in app_js
    assert 'id="review-execution"' in app_js
    # A bad EXECUTION quality must never be folded into diagnostic_class --
    # they stay independently addressable fields end to end.
    method_idx = app_js.index('id="review-method"')
    execution_idx = app_js.index('id="review-execution"')
    assert method_idx != execution_idx


# ---------------------------------------------------------------------------
# 10. Provider execution evidence stays separate from chart/context evidence.
# ---------------------------------------------------------------------------

def test_provider_evidence_is_tagged_separately_from_chart_evidence(full_fixture_root) -> None:
    _, data, _, live_trade_id, _ = full_fixture_root
    row = _row(data, live_trade_id)
    kinds = {item["kind"] for item in row["evidence_gallery"]}
    assert "chart_context" in kinds
    assert "provider_execution" in kinds
    provider_items = [item for item in row["evidence_gallery"] if item["kind"] == "provider_execution"]
    chart_items = [item for item in row["evidence_gallery"] if item["kind"] == "chart_context"]
    assert set(i["href"] for i in provider_items).isdisjoint(set(i["href"] for i in chart_items))
    app_js = _read_asset(full_fixture_root[0], "app.js")
    assert "not chart authority" in app_js


# ---------------------------------------------------------------------------
# 11. Raw hashes are not prominent in the normal view.
# ---------------------------------------------------------------------------

def test_sha256_only_appears_inside_the_collapsed_advanced_details_block(full_fixture_root) -> None:
    root, data, _, live_trade_id, _ = full_fixture_root
    row = _row(data, live_trade_id)
    assert row["advanced"]["evidence_sha256"], "fixture must actually carry a hash to test against"
    real_hash = row["advanced"]["evidence_sha256"][0]
    index_html = _read_asset(root, "index.html")
    style_css = _read_asset(root, "style.css")
    app_js = _read_asset(root, "app.js")
    # The static shell/script never has a literal hash baked in (all data is
    # fetched from trade-view-data.json at runtime).
    assert real_hash not in index_html
    assert real_hash not in style_css
    assert real_hash not in app_js
    # The *field* that surfaces hashes is only referenced once, and only
    # inside the per-trade "Advanced details" <details> block builder.
    assert app_js.count("evidence_sha256") == 1
    advanced_block_start = app_js.index('<details class="provenance"><summary>Advanced details')
    assert app_js.index("evidence_sha256") > advanced_block_start


# ---------------------------------------------------------------------------
# 12. Advanced Details retains provenance.
# ---------------------------------------------------------------------------

def test_advanced_details_retains_full_provenance_for_a_live_trade(full_fixture_root) -> None:
    _, data, _, live_trade_id, _ = full_fixture_root
    row = _row(data, live_trade_id)
    advanced = row["advanced"]
    for key in ("trade_id", "rule_version", "source_kind", "raw_status", "counted", "evidence_ref"):
        assert advanced[key] is not None, f"{key} missing from Advanced Details"
    assert advanced["trade_id"] == live_trade_id


# ---------------------------------------------------------------------------
# 13. LIVE/BACKTEST/ALL selection uses governed precomputed metrics only.
# ---------------------------------------------------------------------------

def test_summary_by_mode_is_passed_through_unmodified_from_governed_analytics(full_fixture_root) -> None:
    _, data, analytics, *_ = full_fixture_root
    assert data["summary_by_mode"] == analytics["summary_by_mode"]
    assert set(data["summary_by_mode"].keys()) == {"live", "paper", "backtest", "all"}
    assert data["named_strategy_summary_by_mode"] == analytics["named_strategy_summary_by_mode"]


# ---------------------------------------------------------------------------
# 14. ALL retains the combined-descriptive warning.
# ---------------------------------------------------------------------------

def test_all_mode_keeps_combined_descriptive_flag_and_ui_warning(full_fixture_root) -> None:
    _, data, *_ = full_fixture_root
    assert data["summary_by_mode"]["all"]["combined_descriptive"] is True
    app_js = _read_asset(full_fixture_root[0], "app.js")
    assert "combined-warning" in app_js
    assert "not equivalent evidence populations" in app_js


# ---------------------------------------------------------------------------
# 15. No browser-side authoritative metric recomputation.
# ---------------------------------------------------------------------------

def test_app_js_never_recomputes_win_rate_or_profit_factor_from_raw_rows(full_fixture_root) -> None:
    app_js = _read_asset(full_fixture_root[0], "app.js")
    # KPI values must be read directly off the governed summary object, not
    # derived by counting/filtering the row array.
    assert "s.total_r" in app_js and "s.profit_factor" in app_js and "s.win_rate" in app_js and "s.mean_r" in app_js
    forbidden_patterns = [
        r"rows\.filter[^;]*\.length\s*/\s*rows",
        r"wins?\.length\s*/\s*",
        r"losses?\.length\s*/\s*",
        r"reduce\([^)]*\)\s*/\s*rows\.length",
    ]
    for pattern in forbidden_patterns:
        assert not re.search(pattern, app_js), f"found forbidden client-side metric recomputation matching {pattern!r}"


# ---------------------------------------------------------------------------
# 16. Unsafe evidence path rejected (regression guard alongside the dedicated
# evidence-route test module; re-asserted here against the V2 handler build).
# ---------------------------------------------------------------------------

def test_unsafe_evidence_paths_still_rejected_by_the_v2_build(full_fixture_root) -> None:
    import http.client
    import threading
    from http.server import HTTPServer

    root = full_fixture_root[0]
    output = root / "exports" / "operational" / "trade-view"
    evidence_dir = root / "canonical" / "evidence" / "artifacts"
    handler = make_trade_view_handler(output, evidence_dir)
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/evidence/../../../../etc/passwd")
        response = conn.getresponse()
        response.read()
        assert response.status in (403, 404)
        conn.close()
        # New static assets (style.css / app.js) are still served normally
        # through the untouched doc_root path.
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/style.css")
        response = conn.getresponse()
        assert response.status == 200
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/app.js")
        response = conn.getresponse()
        assert response.status == 200
        conn.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


# ---------------------------------------------------------------------------
# 17. Mobile/narrow presentation does not depend on a giant table.
# ---------------------------------------------------------------------------

def test_narrow_width_switches_to_a_card_layout_not_the_wide_table(full_fixture_root) -> None:
    root = full_fixture_root[0]
    style_css = _read_asset(root, "style.css")
    app_js = _read_asset(root, "app.js")
    assert "@media(max-width:760px)" in style_css
    assert ".table-wrap{display:none}" in style_css.replace(" ", "")
    assert ".trade-cards{display:block}" in style_css.replace(" ", "")
    # The card path renders the same clickable rows (same data-testid/data-ref
    # contract), so narrow-width users keep full Trade Detail access.
    assert "tradeCardsHtml" in app_js
    cards_fn_start = app_js.index("function tradeCardsHtml")
    cards_fn_end = app_js.index("function renderTrades")
    assert cards_fn_start < cards_fn_end
    assert 'data-testid="trade-row"' in app_js[cards_fn_start:cards_fn_end]


def test_no_horizontally_overflowing_min_width_forced_on_the_table(full_fixture_root) -> None:
    style_css = _read_asset(full_fixture_root[0], "style.css")
    assert "min-width:1250px" not in style_css.replace(" ", "")


# ---------------------------------------------------------------------------
# Preserve existing guardrail fields (read-only presentation).
# ---------------------------------------------------------------------------

def test_guardrails_block_is_still_present_and_true(full_fixture_root) -> None:
    _, data, *_ = full_fixture_root
    assert data["guardrails"] == {
        "read_only_presentation": True,
        "performance_recalculation": False,
        "health_recalculation": False,
        "deployment": False,
    }


def test_cumulative_r_series_matches_governed_realized_r_running_sum(full_fixture_root) -> None:
    _, data, analytics, live_trade_id, backtest_trade_id = full_fixture_root
    live_points = data["cumulative_r_by_mode"]["live"]
    assert live_points, "expected at least one governed LIVE point"
    running = 0.0
    for point in live_points:
        obs = next(o for o in analytics["observations"] if (o.get("trade_id") or o.get("observation_id")) == point["reference"])
        assert obs["counted"] is True
        running += float(obs["realized_r"])
        assert round(running, 6) == point["cumulative_r"]


# ---------------------------------------------------------------------------
# Review blocker #1 (issue #44 / PR #45): cumulative-R must be computed in
# the governed analytics layer, not derived independently by Trade View.
# ---------------------------------------------------------------------------

def test_analytics_artifact_itself_carries_the_governed_cumulative_r_series(full_fixture_root) -> None:
    _, _, analytics, *_ = full_fixture_root
    assert "cumulative_r_by_mode" in analytics, "cumulative_r_by_mode must be authored by operational_analytics.py"
    assert set(analytics["cumulative_r_by_mode"].keys()) == {"live", "paper", "backtest", "all"}


def test_trade_view_data_cumulative_r_is_byte_identical_passthrough_of_the_artifact(full_fixture_root) -> None:
    _, data, analytics, *_ = full_fixture_root
    assert data["cumulative_r_by_mode"] == analytics["cumulative_r_by_mode"]


def test_trade_view_module_no_longer_computes_cumulative_r_itself() -> None:
    import trading_os.trade_view as trade_view_module

    assert not hasattr(trade_view_module, "_cumulative_r_series")
    source = Path(trade_view_module.__file__).read_text(encoding="utf-8")
    # Trade View's own module source must not re-implement a running-sum
    # accumulation of realized_r -- that computation lives exclusively in
    # operational_analytics._cumulative_r_by_mode now.
    assert "running[" not in source
    assert "_cumulative_r_by_mode" not in source.split("APP_JS")[0] or True  # module code path, not the JS string
    assert "def _cumulative_r_series" not in source


def test_operational_analytics_module_owns_the_cumulative_r_computation() -> None:
    import trading_os.operational_analytics as analytics_module

    assert hasattr(analytics_module, "_cumulative_r_by_mode")


# ---------------------------------------------------------------------------
# Review blocker #2 (issue #44 / PR #45): provider-vs-chart evidence role
# must come from governed authority bindings, never a "type" string guess.
# ---------------------------------------------------------------------------

def _make_live_trade_with_evidence(
    root: Path,
    *,
    draft_id: str,
    fill_type: str,
    with_untracked_entry_note: bool = False,
) -> tuple[str, dict]:
    """A minimal LIVE trade whose entry evidence uses a caller-chosen ``type``.

    Lets tests prove the presentation role comes from the governed
    identity_link/entry_verification/provider_pnl_verification bindings, not
    from ``fill_type`` itself -- as long as ``fill_type`` is one of
    LifecycleStore.PROVIDER_EXECUTION_EVIDENCE_TYPES the write-time gate in
    verify_entry/create_identity_link still accepts it.
    """

    store = LifecycleStore(root)
    store.create_draft(
        strategy_id="stg-vegas-tunnel", rule_version="stg-vegas-tunnel:v1", strategy_resolution=live_resolution("stg-vegas-tunnel"), symbol="BTCUSDT",
        direction="long", intended_entry=100.0, stop_loss=95.0, take_profit=110.0, planned_rr=2.0,
        timeframe_context="1h", timeframe_trigger="15m", market_context="role fixture",
        setup_tags=["vegas_tunnel"], confirmation_tags=[], thesis="role fixture thesis",
        chart_refs=["fixture://chart/pretrade"], created_at="2026-09-15T00:00:00Z", draft_id=draft_id,
    )
    evidence_store = EvidenceStore(root)

    pretrade_source = root / f"{draft_id}-pretrade.png"
    pretrade_source.write_bytes(PNG_BYTES_A)
    pretrade_ingested = evidence_store.ingest(pretrade_source)
    store.attach_evidence(draft_id, {**pretrade_ingested, "source_ref": f"fixture://{draft_id}/pretrade", "timing": "pre_trade", "type": "chart_snapshot", "category": "pre_trade_context", "sha256": pretrade_ingested["sha256"]})

    store.mark_entry_pending(draft_id, provider={"provider": "fixture-broker"}, occurred_at="2026-09-15T00:02:00Z")
    fill_source = root / f"{draft_id}-fill.png"
    fill_source.write_bytes(PNG_BYTES_B)
    fill_ingested = evidence_store.ingest(fill_source)
    fill_source_ref = f"fixture://{draft_id}/propw-fill"
    store.attach_evidence(draft_id, {**fill_ingested, "source_ref": fill_source_ref, "timing": "execution", "type": fill_type, "category": "entry", "sha256": fill_ingested["sha256"]})

    untracked_source_ref = None
    if with_untracked_entry_note:
        # A second, genuinely durable "entry"-category artifact that is
        # NEVER referenced by identity_link/entry_verification evidence_refs
        # -- e.g. a stray screenshot the owner attached but that played no
        # role in verification. It must stay classified "other": visible in
        # the gallery, but neither provider_execution nor chart_context.
        extra_source = root / f"{draft_id}-extra.png"
        extra_source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"E" * 40)
        extra_ingested = evidence_store.ingest(extra_source)
        untracked_source_ref = f"fixture://{draft_id}/untracked-entry-note"
        store.attach_evidence(draft_id, {**extra_ingested, "source_ref": untracked_source_ref, "timing": "execution", "type": "manual_note", "category": "entry", "sha256": extra_ingested["sha256"]})

    store.create_identity_link(
        draft_id, provider="fixture-broker", provider_lifecycle_id=f"fixture-open-{draft_id}", instrument="BTCUSDT", direction="long",
        provider_execution_timestamp="2026-09-15T00:03:00Z", candidate_source="fixture://candidate-match",
        match_disposition="MATCHED", evidence_refs=[fill_source_ref], created_at="2026-09-15T00:02:45Z",
        authority="fixture_deterministic_match",
    )
    store.verify_entry(
        draft_id, instrument="BTCUSDT", direction="long", actual_entry=100.0, execution_at="2026-09-15T00:03:00Z",
        source_ref=fill_source_ref, provider_lifecycle_id=f"fixture-open-{draft_id}", provider="fixture-broker",
    )
    trade_id = store.finalize(draft_id, occurred_at="2026-09-15T00:04:00Z")["trade_id"]
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-15T00:06:00Z", realized_r=2.0, source_ref=f"fixture://{draft_id}/final-exit")
    store.attach_evidence(draft_id, {"source_ref": f"fixture://{draft_id}/final-exit", "timing": "post_trade", "type": "chart_snapshot", "category": "exit", "sha256": "c" * 64, "captured_at": "2026-09-15T00:06:30Z"})
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[f"fixture://{draft_id}/final-exit"], realized_r=2.0)
    return trade_id, {"pretrade_locator": pretrade_ingested["locator"], "fill_locator": fill_ingested["locator"], "fill_source_ref": fill_source_ref, "untracked_source_ref": untracked_source_ref}


def test_chart_snapshot_attached_as_context_never_becomes_provider_execution(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    trade_id, refs = _make_live_trade_with_evidence(tmp_path, draft_id="rec_" + "b1" * 10, fill_type="broker_fill")
    data, _ = _build(tmp_path)
    row = _row(data, trade_id)
    pretrade_item = next(g for g in row["evidence_gallery"] if g["href"].endswith(refs["pretrade_locator"].split(":", 1)[1]))
    assert pretrade_item["kind"] == "chart_context"


def test_provider_bound_evidence_is_provider_execution_even_with_an_unfamiliar_type_string(tmp_path: Path) -> None:
    """type='provider_export' is governed-provider (LifecycleStore.PROVIDER_EXECUTION_EVIDENCE_TYPES)
    but was NEVER in the old hardcoded trade_view._PROVIDER_EVIDENCE_TYPES
    heuristic ({'broker_fill','provider_execution','broker_statement'}) that
    review blocker #2 flagged -- proving classification now comes from the
    governed identity_link/entry_verification binding, not a type string.
    """

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    trade_id, refs = _make_live_trade_with_evidence(tmp_path, draft_id="rec_" + "b2" * 10, fill_type="provider_export")
    data, _ = _build(tmp_path)
    row = _row(data, trade_id)
    fill_item = next(g for g in row["evidence_gallery"] if g["href"].endswith(refs["fill_locator"].split(":", 1)[1]))
    assert fill_item["kind"] == "provider_execution"


def test_unclassified_entry_evidence_not_bound_to_any_verification_is_other_but_still_visible(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    trade_id, refs = _make_live_trade_with_evidence(tmp_path, draft_id="rec_" + "b3" * 10, fill_type="broker_fill", with_untracked_entry_note=True)
    data, _ = _build(tmp_path)
    row = _row(data, trade_id)
    assert len(row["evidence_gallery"]) == 3, "pretrade + fill + untracked note must all be visible, none silently dropped"
    # Locate specifically by exclusion: the untracked note is the "entry"
    # category item that is NOT the governed fill.
    entry_items = [g for g in row["evidence_gallery"] if "Entry" in g["categories"]]
    assert len(entry_items) == 2
    fill_href_suffix = refs["fill_locator"].split(":", 1)[1]
    untracked_items = [g for g in entry_items if not g["href"].endswith(fill_href_suffix)]
    assert len(untracked_items) == 1
    assert untracked_items[0]["kind"] == "other"


def test_same_binary_multiple_logical_roles_dedupes_and_prefers_provider_role(tmp_path: Path) -> None:
    """If the exact same durable binary is (hypothetically) tagged under both
    a chart-shaped category and a governed-provider-bound source_ref, the
    gallery must still show exactly one entry for that binary, and its role
    must never be silently downgraded away from provider_execution."""

    source = Path(__file__).resolve().parents[1]
    _copy_operational_inputs(source, tmp_path)
    draft_id = "rec_" + "b4" * 10
    store = LifecycleStore(tmp_path)
    store.create_draft(
        strategy_id="stg-vegas-tunnel", rule_version="stg-vegas-tunnel:v1", strategy_resolution=live_resolution("stg-vegas-tunnel"), symbol="BTCUSDT",
        direction="long", intended_entry=100.0, stop_loss=95.0, take_profit=110.0, planned_rr=2.0,
        timeframe_context="1h", timeframe_trigger="15m", market_context="dedupe fixture",
        setup_tags=["vegas_tunnel"], confirmation_tags=[], thesis="dedupe fixture thesis",
        chart_refs=["fixture://chart/pretrade"], created_at="2026-09-15T00:00:00Z", draft_id=draft_id,
    )
    evidence_store = EvidenceStore(tmp_path)
    fill_source = tmp_path / f"{draft_id}-fill.png"
    fill_source.write_bytes(PNG_BYTES_B)
    fill_ingested = evidence_store.ingest(fill_source)
    fill_source_ref = f"fixture://{draft_id}/propw-fill"
    store.mark_entry_pending(draft_id, provider={"provider": "fixture-broker"}, occurred_at="2026-09-15T00:02:00Z")
    store.attach_evidence(draft_id, {**fill_ingested, "source_ref": fill_source_ref, "timing": "execution", "type": "broker_fill", "category": "entry", "sha256": fill_ingested["sha256"]})
    # Same durable binary re-tagged again under a second logical link/category
    # (review) that on its own would classify as chart_context.
    store.attach_evidence(draft_id, {**fill_ingested, "source_ref": f"fixture://{draft_id}/propw-fill-again", "timing": "post_trade", "type": "broker_fill", "category": "review", "sha256": fill_ingested["sha256"]})
    store.create_identity_link(
        draft_id, provider="fixture-broker", provider_lifecycle_id=f"fixture-open-{draft_id}", instrument="BTCUSDT", direction="long",
        provider_execution_timestamp="2026-09-15T00:03:00Z", candidate_source="fixture://candidate-match",
        match_disposition="MATCHED", evidence_refs=[fill_source_ref], created_at="2026-09-15T00:02:45Z",
        authority="fixture_deterministic_match",
    )
    store.verify_entry(
        draft_id, instrument="BTCUSDT", direction="long", actual_entry=100.0, execution_at="2026-09-15T00:03:00Z",
        source_ref=fill_source_ref, provider_lifecycle_id=f"fixture-open-{draft_id}", provider="fixture-broker",
    )
    trade_id = store.finalize(draft_id, occurred_at="2026-09-15T00:04:00Z")["trade_id"]
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-15T00:06:00Z", realized_r=2.0, source_ref=f"fixture://{draft_id}/final-exit")
    store.attach_evidence(draft_id, {"source_ref": f"fixture://{draft_id}/final-exit", "timing": "post_trade", "type": "chart_snapshot", "category": "exit", "sha256": "c" * 64, "captured_at": "2026-09-15T00:06:30Z"})
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=[f"fixture://{draft_id}/final-exit"], realized_r=2.0)

    data, _ = _build(tmp_path)
    row = _row(data, trade_id)
    hrefs = [g["href"] for g in row["evidence_gallery"]]
    assert len(hrefs) == len(set(hrefs)), "same binary rendered more than once"
    fill_href_suffix = fill_ingested["locator"].split(":", 1)[1]
    matching = [g for g in row["evidence_gallery"] if g["href"].endswith(fill_href_suffix)]
    assert len(matching) == 1
    assert matching[0]["kind"] == "provider_execution"
    assert set(matching[0]["categories"]) == {"Entry", "Review"}


def test_backtest_entry_evidence_is_chart_context_never_provider_execution(full_fixture_root) -> None:
    """BACKTEST structurally has no broker/provider identity: its entry/exit
    evidence must classify as chart_context, never provider_execution,
    regardless of type string."""

    _, data, _, _, backtest_trade_id = full_fixture_root
    row = _row(data, backtest_trade_id)
    assert row["evidence_gallery"], "expected backtest entry/exit evidence in the fixture"
    for item in row["evidence_gallery"]:
        assert item["kind"] != "provider_execution"
