"""Per-trade registry-APPLICATION slice (2026-09-10).

Proves the minimal operational application of the owner (`example-owner`) per-trade
decision set ``strategy-mapping-per-trade-review-20260910`` (67 decisions):

  * the single registry change: ``ob`` -> ``stg-ict-liquidity.legacy_aliases``
    (rulings ptr-ruling-1 / ptr-ruling-2, superseding PR-A ``grp-ob``);
  * a reconciliation/application record covering all 67 dispositions:
    28 applied via the ``ob`` alias, 26 human routes recorded pending resolver
    consumption, 13 excluded (8 keep-blocked / 4 Historical Cleaning / 1
    position-management);
  * no lifecycle change, no new OB identity, no re-judgement, no Step-0 rerun.

Covers the 14 assertions in the slice brief (assertion 3 = "application-
consumable": the record covers all 54 recoverable with valid registry targets,
NOT "resolver-resolvable"; assertions 7-8 = target-lifecycle checks).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from trading_os.registry import load_registry
from trading_os.strategy_discovery import build_strategy_lookup, classify_strategy_label

REPO = Path(__file__).resolve().parents[1]
LANE = REPO / "canonical/adjudications/strategy-mapping-review"
DECISIONS = LANE / "strategy-mapping-per-trade-decisions-20260910.jsonl"
APPLICATION = LANE / "strategy-mapping-per-trade-application-20260910.jsonl"
APPLICATION_MANIFEST = LANE / "strategy-mapping-per-trade-application-20260910.manifest.json"

MAP_DECISIONS = {"map_to_existing_strategy", "map_experimental"}
NONMAP_DECISIONS = {
    "keep_blocked",
    "route_to_historical_cleaning_review",
    "position_management_provenance",
}
RECOVERABLE_TALLY = {
    "stg-ict-liquidity": 30,
    "stg-vegas-tunnel": 16,
    "stg-17ema": 3,
    "stg-example-b": 2,
    "stg-po3": 1,
    "stg-fib-rsi": 1,
    "stg-triple-tap": 1,
}


def _jsonl(path: Path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture(scope="module")
def decisions():
    return _jsonl(DECISIONS)


@pytest.fixture(scope="module")
def application():
    return _jsonl(APPLICATION)


@pytest.fixture(scope="module")
def app_manifest():
    return json.loads(APPLICATION_MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def registry():
    return load_registry(REPO)


@pytest.fixture(scope="module")
def by_id(registry):
    return {s["strategy_id"]: s for s in registry["strategies"]}


@pytest.fixture(scope="module")
def lookup(registry):
    return build_strategy_lookup(registry)


def _resolve(lookup, label):
    m = classify_strategy_label(label, lookup)
    return m["strategy"]["strategy_id"] if m["matched"] else None


# --- (1) ob resolves to stg-ict-liquidity via the resolver -------------------

def test_1_ob_resolves_to_ict_liquidity(lookup, by_id):
    assert _resolve(lookup, "ob") == "stg-ict-liquidity"
    assert "ob" in by_id["stg-ict-liquidity"]["legacy_aliases"]
    # existing 6 aliases preserved
    for a in ("sr", "sr-flip", "mss-rejection", "bos-retest", "support-retest", "OB + MSS"):
        assert a in by_id["stg-ict-liquidity"]["legacy_aliases"], a


# --- (2) no new OB strategy identity ----------------------------------------

def test_2_no_new_ob_identity(by_id):
    for sid in ("stg-ob", "stg-ob-system", "stg-order-block", "stg-9-ob"):
        assert sid not in by_id, sid
    # `ob` is an alias of the PARENT only, nowhere else
    for sid, s in by_id.items():
        if sid != "stg-ict-liquidity":
            assert "ob" not in (s.get("legacy_aliases") or []), sid


# --- (3) all 54 recoverable decisions are application-consumable -------------

def test_3_all_54_recoverable_are_application_consumable(decisions, application, by_id):
    recoverable = [d for d in decisions if d["human_decision"] in MAP_DECISIONS]
    assert len(recoverable) == 54
    app_by_qid = {r["queue_id"]: r for r in application}
    for d in recoverable:
        r = app_by_qid[d["queue_id"]]
        assert r["application_class"] == "recoverable_route", d["queue_id"]
        assert r["target_strategy_id"] == d["target_strategy_id"]
        assert r["target_strategy_id"] in by_id
        assert r["resolution_mechanism"] in {
            "applied_via_registry_alias:ob",
            "human_route_recorded_pending_resolver_consumption",
        }


# --- (4) 8 keep-blocked not auto-routed ------------------------------------

def test_4_keep_blocked_not_routed(decisions, application, lookup):
    kb = [d for d in decisions if d["human_decision"] == "keep_blocked"]
    assert len(kb) == 8
    app_by_qid = {r["queue_id"]: r for r in application}
    for d in kb:
        r = app_by_qid[d["queue_id"]]
        assert r["application_class"] == "excluded"
        assert r["excluded_disposition"] == "keep_blocked"
        assert r["target_strategy_id"] is None
        # the blocked labels still never resolve
        assert _resolve(lookup, d["legacy_strategy_label"]) is None


# --- (5) 4 Historical Cleaning handoffs not strategy-routed -----------------

def test_5_historical_cleaning_handoffs_not_routed(decisions, application):
    hc = [d for d in decisions if d["human_decision"] == "route_to_historical_cleaning_review"]
    assert len(hc) == 4
    app_by_qid = {r["queue_id"]: r for r in application}
    for d in hc:
        r = app_by_qid[d["queue_id"]]
        assert r["excluded_disposition"] == "historical_cleaning_handoff"
        assert r["handoff_lane"] == "historical-cleaning"
        assert r["target_strategy_id"] is None


# --- (6) 1 PM handoff not strategy-routed ----------------------------------

def test_6_pm_handoff_not_routed(decisions, application):
    pm = [d for d in decisions if d["human_decision"] == "position_management_provenance"]
    assert len(pm) == 1
    r = {x["queue_id"]: x for x in application}[pm[0]["queue_id"]]
    assert r["excluded_disposition"] == "position_management_handoff"
    assert r["handoff_lane"] == "position-management"
    assert r["target_strategy_id"] is None


# --- (7) historical OP mappings don't reactivate stg-example-b --------------------

def test_7_op_stays_retired(decisions, by_id):
    op = [d for d in decisions if d["target_strategy_id"] == "stg-example-b"]
    assert len(op) == 2
    assert by_id["stg-example-b"]["status"] == "retired"
    assert by_id["stg-example-b"].get("retired_on") == "2026-09-09"


# --- (8) 17EMA / Fib-RSI mappings don't change experimental lifecycle ------

def test_8_experimental_targets_stay_experimental(decisions, by_id):
    for sid in ("stg-17ema", "stg-fib-rsi"):
        assert by_id[sid]["status"] == "experimental"
        assert by_id[sid]["classification"] == "experimental"
    assert by_id["stg-triple-tap"]["status"] == "active"
    assert by_id["stg-po3"]["status"] == "active"
    assert by_id["stg-vegas-tunnel"]["status"] == "active"
    assert by_id["stg-ict-liquidity"]["status"] == "active"


# --- (9) supersession lineage for grp-ob remains traceable ----------------

def test_9_grp_ob_supersession_lineage_preserved(decisions, application, app_manifest):
    ob_overrides = [d for d in decisions if not d["matched_agent_recommendation"]]
    assert len(ob_overrides) == 27
    app_by_qid = {r["queue_id"]: r for r in application}
    for d in ob_overrides:
        assert d["supersedes"]["decision_id"] == "smhd-20260910-19"
        assert d["supersedes"]["group_id"] == "grp-ob"
        r = app_by_qid[d["queue_id"]]
        # the application row carries the supersession forward, not rewrites it
        assert r["supersedes"]["decision_id"] == "smhd-20260910-19"
        assert r["pr_a_group_id"] == "grp-ob"
    assert app_manifest["registry_change"]["supersedes_pr_a"] == {
        "decision_id": "smhd-20260910-19", "group_id": "grp-ob",
    }
    # PR-A grp-ob row itself is untouched (still mechanism-only / no target)
    pra = _jsonl(LANE / "strategy-mapping-human-decisions-20260910.jsonl")
    grp_ob = next(x for x in pra if x.get("decision_id") == "smhd-20260910-19")
    assert grp_ob["target_strategy_id"] is None
    assert grp_ob["registry_application_status"] == "pending"


# --- (10) application consumes authoritative decisions without re-judging ---

def test_10_no_rejudgement(decisions, application):
    dec_by_qid = {d["queue_id"]: d for d in decisions}
    assert len(application) == 67
    for r in application:
        d = dec_by_qid[r["queue_id"]]
        assert r["human_decision"] == d["human_decision"]
        assert r["decision_id"] == d["decision_id"]
        assert r["trade_ref"] == d["trade_ref"]
        assert r["legacy_strategy_label"] == d["legacy_strategy_label"]
        assert r["human_authority"] == "example-owner"
        # target is copied straight from the owner decision, never re-derived
        if d["human_decision"] in MAP_DECISIONS:
            assert r["target_strategy_id"] == d["target_strategy_id"]
        else:
            assert r["target_strategy_id"] is None


def test_10b_decision_artifact_not_mutated(decisions):
    # every capture row stays registry_application_status == pending
    assert all(d["registry_application_status"] == "pending" for d in decisions)


# --- (11) no duplicate route application ----------------------------------

def test_11_no_duplicate_application(application):
    qids = [r["queue_id"] for r in application]
    assert len(qids) == len(set(qids)) == 67
    tref = [r["trade_ref"] for r in application]
    assert len(tref) == len(set(tref)) == 67


# --- (12) all referenced strategy IDs exist ------------------------------

def test_12_all_referenced_ids_exist(application, by_id):
    for r in application:
        if r["target_strategy_id"] is not None:
            assert r["target_strategy_id"] in by_id, r["queue_id"]


# --- (13) exactly 54 recoverable decisions covered ---------------------

def test_13_exactly_54_recoverable_covered(application):
    recoverable = [r for r in application if r["application_class"] == "recoverable_route"]
    assert len(recoverable) == 54
    tally = Counter(r["target_strategy_id"] for r in recoverable)
    assert dict(tally) == RECOVERABLE_TALLY
    applied = [r for r in recoverable if r["resolution_mechanism"] == "applied_via_registry_alias:ob"]
    pending = [r for r in recoverable if r["resolution_mechanism"] == "human_route_recorded_pending_resolver_consumption"]
    assert len(applied) == 28
    assert len(pending) == 26
    for r in applied:
        assert r["target_strategy_id"] == "stg-ict-liquidity"
        assert r["frontmatter_strategy_label"] == "ob"


# --- (14) reconciliation 54 + 8 + 4 + 1 = 67 --------------------------

def test_14_reconciliation_sums_to_67(application, app_manifest):
    classes = Counter()
    for r in application:
        if r["application_class"] == "recoverable_route":
            classes[r["resolution_mechanism"]] += 1
        else:
            classes[r["excluded_disposition"]] += 1
    assert classes["applied_via_registry_alias:ob"] == 28
    assert classes["human_route_recorded_pending_resolver_consumption"] == 26
    assert classes["keep_blocked"] == 8
    assert classes["historical_cleaning_handoff"] == 4
    assert classes["position_management_handoff"] == 1
    assert sum(classes.values()) == 67
    rec = app_manifest["reconciliation"]
    assert rec["sum_check"] == 67
    assert rec["recoverable_routes_total"] == 54
    assert 54 + 8 + 4 + 1 == 67
    for inv, val in app_manifest["invariants"].items():
        assert val is True, inv


# --- manifest digest recompute + no runtime code change ----------------

def test_manifest_digests_recompute(app_manifest):
    import hashlib
    assert app_manifest["application_artifact_sha256"] == hashlib.sha256(
        APPLICATION.read_bytes()
    ).hexdigest()
    assert app_manifest["consumes_decision_artifact_sha256"] == hashlib.sha256(
        DECISIONS.read_bytes()
    ).hexdigest()
    assert app_manifest["record_count"] == 67
    assert app_manifest["registry_change"]["legacy_alias_added"] == "ob"
    assert app_manifest["registry_change"]["lifecycle_change"] is None


def test_no_trade_or_merged_adjudication_mutation():
    # The per-trade registry-application slice committed no runtime change and no
    # Step-0 commit. The follow-up Step-0-rerun + per-trade-consumption slice
    # legitimately does both (resolver + regenerated canonical Step-0 artifacts),
    # so those guards were retired. What stays immutable regardless of slice:
    # trades/ is never mutated, and the merged owner decision / application
    # artifacts are append-only authority that must not be rewritten.
    res = _git_name_only()
    if res is None:
        pytest.skip("git diff unavailable")
    frozen = {
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-per-trade-decisions-20260910.jsonl",
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-per-trade-decisions-20260910.manifest.json",
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-per-trade-application-20260910.jsonl",
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-per-trade-application-20260910.manifest.json",
    }
    import subprocess
    from legacy_test_support import unpreserved_legacy_changes
    base = subprocess.run(["git", "merge-base", "HEAD", "origin/main"], cwd=REPO, capture_output=True,
                          text=True).stdout.strip() or "origin/main"
    # trades/ may only leave the tree as the exact, ledger-preserved retirement.
    assert unpreserved_legacy_changes(base, list(res)) == []
    for path in res:
        assert path not in frozen, path


def _git_name_only():
    import subprocess
    mb = subprocess.run(["git", "merge-base", "HEAD", "origin/main"],
                        cwd=REPO, capture_output=True, text=True)
    if mb.returncode != 0 or not mb.stdout.strip():
        return None
    d = subprocess.run(["git", "diff", "--name-only", f"{mb.stdout.strip()}...HEAD"],
                       cwd=REPO, capture_output=True, text=True)
    if d.returncode != 0:
        return None
    return [x for x in d.stdout.splitlines() if x.strip()]
