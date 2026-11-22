"""Per-trade decision consumption in the Step-0 resolver.

The Step-0 resolver gained a keyed per-trade override surface that consumes the
merged owner per-trade application manifest
(`canonical/adjudications/strategy-mapping-review/
strategy-mapping-per-trade-application-20260910.jsonl`, merged in PR #27).

These tests cover the loader's hard exclusion, the provenance marker, precedence
over registry label matching, lifecycle mapping without registry mutation, the
regenerated canonical universe, determinism, and the absence of any fuzzy match
type.  Universe assertions read the regenerated committed artifacts, never a
hard-coded literal (except the two invariants the slice fixes: BLOCKED_MAPPING
== 13 and TOTAL == 255).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from trading_os.strategy_discovery import (
    HUMAN_PER_TRADE_MATCH_TYPE,
    PER_TRADE_HARD_EXCLUDED_DISPOSITIONS,
    Step0Error,
    build_strategy_lookup,
    load_per_trade_strategy_overrides,
    run_step0,
    validate_step0_artifacts,
)

REPO = Path(__file__).resolve().parents[1]
STEP0 = REPO / "exports/strategy-discovery/step-0"
APPLICATION = (
    REPO
    / "canonical/adjudications/strategy-mapping-review"
    / "strategy-mapping-per-trade-application-20260910.jsonl"
)


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def results():
    return _jsonl(STEP0 / "step-0-results.jsonl")


@pytest.fixture(scope="module")
def blockers():
    return _jsonl(STEP0 / "blocker-backlog.jsonl")


@pytest.fixture(scope="module")
def manifest():
    return json.loads((STEP0 / "corpus-manifest.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def application():
    return _jsonl(APPLICATION)


# --- a synthetic project mirroring the real manifest shape -------------------

_REGISTRY = {
    "registry_version": "strategy-registry.v1",
    "strategies": [
        {"strategy_id": "stg-act", "classification": "independent_system",
         "status": "active", "legacy_aliases": ["act"], "rule_versions": []},
        {"strategy_id": "stg-ret", "classification": "independent_system",
         "status": "retired", "legacy_aliases": ["ret"], "rule_versions": []},
        {"strategy_id": "stg-exp", "classification": "experimental",
         "status": "experimental", "legacy_aliases": ["exp"], "rule_versions": []},
    ],
}


def _mk_project(tmp_path: Path, rows: list[dict]) -> Path:
    root = tmp_path / "p"
    (root / "registry").mkdir(parents=True)
    lane = root / "canonical/adjudications/strategy-mapping-review"
    lane.mkdir(parents=True)
    (root / "registry/strategy-registry.yaml").write_text(
        yaml.safe_dump(_REGISTRY, sort_keys=False), encoding="utf-8"
    )
    (lane / "app.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    return root


def _row(trade_ref, strategy_id, decision_id, mechanism="human_route_recorded_pending_resolver_consumption",
         application_class="recoverable_route", excluded_disposition=None, handoff_lane=None):
    return {
        "trade_ref": trade_ref, "target_strategy_id": strategy_id, "decision_id": decision_id,
        "resolution_mechanism": mechanism, "application_class": application_class,
        "excluded_disposition": excluded_disposition, "handoff_lane": handoff_lane,
    }


def _lookup(root: Path):
    from trading_os.registry import load_registry

    return build_strategy_lookup(load_registry(root))


# --- (1) loader hard exclusion ---------------------------------------------

def test_loader_hard_excludes_keep_blocked_and_handoffs(tmp_path: Path):
    rows = [
        _row("trades/a.md", "stg-act", "d1"),
        _row("trades/b.md", None, "d2", mechanism=None, application_class="excluded",
             excluded_disposition="keep_blocked"),
        _row("trades/c.md", None, "d3", mechanism=None, application_class="excluded",
             excluded_disposition="historical_cleaning_handoff", handoff_lane="historical-cleaning"),
        _row("trades/d.md", None, "d4", mechanism=None, application_class="excluded",
             excluded_disposition="position_management_handoff", handoff_lane="position-management"),
        _row("trades/e.md", "stg-act", "d5", mechanism="applied_via_registry_alias:ob"),
    ]
    root = _mk_project(tmp_path, rows)
    overrides = load_per_trade_strategy_overrides(root, _lookup(root), ("canonical/adjudications/strategy-mapping-review/app.jsonl",))
    assert set(overrides) == {"trades/a.md"}
    assert overrides["trades/a.md"]["strategy_id"] == "stg-act"
    assert overrides["trades/a.md"]["decision_id"] == "d1"


def test_loader_rejects_unknown_target_strategy(tmp_path: Path):
    root = _mk_project(tmp_path, [_row("trades/a.md", "stg-nope", "d1")])
    with pytest.raises(Step0Error, match="not a registry strategy_id"):
        load_per_trade_strategy_overrides(root, _lookup(root), ("canonical/adjudications/strategy-mapping-review/app.jsonl",))


def test_loader_rejects_conflicting_override(tmp_path: Path):
    rows = [_row("trades/a.md", "stg-act", "d1"), _row("trades/a.md", "stg-ret", "d2")]
    root = _mk_project(tmp_path, rows)
    with pytest.raises(Step0Error, match="conflicting per-trade overrides"):
        load_per_trade_strategy_overrides(root, _lookup(root), ("canonical/adjudications/strategy-mapping-review/app.jsonl",))


# --- (2)(3)(6)(7) precedence + provenance + lifecycle via a full run --------

def _run_synthetic(tmp_path: Path):
    rows = [
        _row("trades/2026/09/2026-09-01_x.md", "stg-act", "dec-act"),   # label 'ret' -> override wins
        _row("trades/2026/09/2026-09-02_x.md", "stg-ret", "dec-ret"),   # retired target
        _row("trades/2026/09/2026-09-03_x.md", "stg-exp", "dec-exp"),   # experimental target
        _row("trades/2026/09/2026-09-04_x.md", None, "dec-kb", mechanism=None,
             application_class="excluded", excluded_disposition="keep_blocked"),
    ]
    root = _mk_project(tmp_path, rows)
    trades = {
        "2026-09-01_x.md": "ret",   # conflicting label; override must outrank it
        "2026-09-02_x.md": "unmapped",
        "2026-09-03_x.md": None,
        "2026-09-04_x.md": "unmapped",
    }
    for name, label in trades.items():
        p = root / "trades/2026/09" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        line = f"strategy: {label}\n" if label is not None else ""
        p.write_text(f"---\n{line}pair: BTCUSDT\n---\n\n# r\n", encoding="utf-8")
    out = tmp_path / "out"
    manifest = run_step0(
        root, baseline_sha="b", historical_reference_commit="h", output_dir=out,
        historical_reference_paths=[f"trades/2026/09/{n}" for n in trades],
        identity_log_paths=(),
        per_trade_decision_log_paths=("canonical/adjudications/strategy-mapping-review/app.jsonl",),
        verify_git=False,
    )
    rows_out = {r["trade_ref"]: r for r in _jsonl(out / "step-0-results.jsonl")}
    return manifest, rows_out, root, out


def test_override_outranks_conflicting_label_and_marks_provenance(tmp_path: Path):
    _, rows_out, _, _ = _run_synthetic(tmp_path)
    r = rows_out["trades/2026/09/2026-09-01_x.md"]
    assert r["legacy_strategy_label"] == "ret"  # the file still said 'ret'
    assert r["strategy_mapping"]["match_type"] == HUMAN_PER_TRADE_MATCH_TYPE
    assert r["strategy_mapping"]["strategy_id"] == "stg-act"  # override target, not 'stg-ret'
    assert r["strategy_mapping"]["decision_id"] == "dec-act"
    assert r["step0_gate_outcome"] == "ACTIVE"
    assert "canonical/adjudications/strategy-mapping-review/app.jsonl" in r["provenance_refs"]


def test_override_lifecycle_mapping_without_registry_mutation(tmp_path: Path):
    _, rows_out, root, _ = _run_synthetic(tmp_path)
    assert rows_out["trades/2026/09/2026-09-02_x.md"]["step0_gate_outcome"] == "RETIRED"
    assert rows_out["trades/2026/09/2026-09-03_x.md"]["step0_gate_outcome"] == "EXPERIMENTAL"
    registry = yaml.safe_load((root / "registry/strategy-registry.yaml").read_text("utf-8"))
    by_id = {s["strategy_id"]: s for s in registry["strategies"]}
    assert by_id["stg-ret"]["status"] == "retired"
    assert by_id["stg-exp"]["status"] == "experimental"


def test_hard_excluded_row_stays_blocked(tmp_path: Path):
    _, rows_out, _, _ = _run_synthetic(tmp_path)
    r = rows_out["trades/2026/09/2026-09-04_x.md"]
    assert r["step0_gate_outcome"] == "BLOCKED_STRATEGY_MAPPING"
    assert r["strategy_mapping"]["match_type"] is None


def test_synthetic_run_is_deterministic(tmp_path: Path):
    import hashlib

    m1, _, root, out1 = _run_synthetic(tmp_path)
    out2 = tmp_path / "out2"
    run_step0(
        root, baseline_sha="b", historical_reference_commit="h", output_dir=out2,
        historical_reference_paths=[
            "trades/2026/09/2026-09-01_x.md", "trades/2026/09/2026-09-02_x.md",
            "trades/2026/09/2026-09-03_x.md", "trades/2026/09/2026-09-04_x.md",
        ],
        identity_log_paths=(),
        per_trade_decision_log_paths=("canonical/adjudications/strategy-mapping-review/app.jsonl",),
        verify_git=False,
    )
    for name in ("step-0-results.jsonl", "blocker-backlog.jsonl", "step-0-report.md", "corpus-manifest.json"):
        assert hashlib.sha256((out1 / name).read_bytes()).hexdigest() == \
            hashlib.sha256((out2 / name).read_bytes()).hexdigest(), name


# --- committed universe (regenerated, not hard-coded) ----------------------

def test_committed_universe_counts(manifest):
    g = manifest["routes"]["gate_outcome"]
    assert sum(g.values()) == 255
    assert g["BLOCKED_STRATEGY_MAPPING"] == 13
    assert g["BLOCKED_STRATEGY_STATUS"] == 0
    # derived, not asserted as literals beyond the two invariants above
    assert g["ACTIVE"] + g["RETIRED"] + g["EXPERIMENTAL"] == 242
    report = (STEP0 / "step-0-report.md").read_text("utf-8")
    for route, count in g.items():
        assert f"| `{route}` | {count} |" in report


def test_committed_all_26_pending_routes_resolved(results, application):
    pending = {
        r["trade_ref"] for r in application
        if r["resolution_mechanism"] == "human_route_recorded_pending_resolver_consumption"
    }
    assert len(pending) == 26
    by_ref = {r["trade_ref"]: r for r in results}
    for ref in pending:
        row = by_ref[ref]
        assert row["strategy_mapping"]["match_type"] == HUMAN_PER_TRADE_MATCH_TYPE
        assert row["strategy_mapping"]["decision_id"]
        assert row["step0_gate_outcome"] in {"ACTIVE", "RETIRED", "EXPERIMENTAL"}


def test_committed_13_blocked_are_the_reconciliation_exclusions(blockers, application):
    excluded = {
        r["trade_ref"]: r["excluded_disposition"]
        for r in application if r["application_class"] == "excluded"
    }
    assert sorted(excluded.values()).count("keep_blocked") == 8
    blocked = {b["trade_ref"] for b in blockers}
    assert blocked == set(excluded)
    assert len(blocked) == 13


def test_committed_28_ob_route_via_alias_not_override(results):
    ob = [r for r in results if r["legacy_strategy_label"] == "ob"]
    assert len(ob) == 28
    for r in ob:
        assert r["strategy_mapping"]["match_type"] == "exact_legacy_alias"
        assert r["strategy_mapping"]["strategy_id"] == "stg-ict-liquidity"
        assert r["strategy_mapping"]["decision_id"] is None


def test_committed_stg_op_and_experimental_targets_keep_registry_status(results):
    registry = yaml.safe_load((REPO / "registry/strategy-registry.yaml").read_text("utf-8"))
    by_id = {s["strategy_id"]: s for s in registry["strategies"]}
    assert by_id["stg-example-b"]["status"] == "retired"
    assert by_id["stg-17ema"]["status"] == "experimental"
    assert by_id["stg-fib-rsi"]["status"] == "experimental"
    op_rows = [r for r in results if r["strategy_mapping"]["strategy_id"] == "stg-example-b"
               and r["strategy_mapping"]["match_type"] == HUMAN_PER_TRADE_MATCH_TYPE]
    assert op_rows and all(r["step0_gate_outcome"] == "RETIRED" for r in op_rows)
    exp_rows = [r for r in results if r["strategy_mapping"]["strategy_id"] in {"stg-17ema", "stg-fib-rsi"}
                and r["strategy_mapping"]["match_type"] == HUMAN_PER_TRADE_MATCH_TYPE]
    assert exp_rows and all(r["step0_gate_outcome"] == "EXPERIMENTAL" for r in exp_rows)


def test_committed_no_fuzzy_or_inferred_match_type(results):
    allowed = {None, "exact_strategy_id", "exact_legacy_alias", HUMAN_PER_TRADE_MATCH_TYPE}
    for r in results:
        assert r["strategy_mapping"]["match_type"] in allowed


def test_committed_supersession_lineage_for_grp_ob_traceable(results, application):
    ob_app = [r for r in application if r.get("frontmatter_strategy_label") == "ob"]
    superseding = [r for r in ob_app if r.get("supersedes")]
    assert superseding and all(
        r["supersedes"]["decision_id"] == "smhd-20260910-19" for r in superseding
    )
    # results route the same trades to the parent identity via the alias
    ob_refs = {r["trade_ref"] for r in ob_app}
    by_ref = {r["trade_ref"]: r for r in results}
    for ref in ob_refs:
        assert by_ref[ref]["strategy_mapping"]["strategy_id"] == "stg-ict-liquidity"


def test_committed_manifest_revalidates(manifest):
    assert PER_TRADE_HARD_EXCLUDED_DISPOSITIONS == frozenset(
        {"keep_blocked", "historical_cleaning_handoff", "position_management_handoff"}
    )
    validate_step0_artifacts(
        REPO,
        STEP0,
        expected_baseline_sha=manifest["baseline_main_sha"],
        allow_historical_registry_provenance=True,
    )
    pt = manifest["per_trade_consumption"]
    assert pt["applied_override_count"] == 26
    assert pt["consumed_mechanism"] == "human_route_recorded_pending_resolver_consumption"
    assert manifest["checks"]["per_trade_overrides_all_consumed"] is True
