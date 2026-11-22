"""Residual non-strategy lane-handoff slice (2026-09-10).

Proves the strategy-mapping lane's formal handoff record for the 5 residual
``BLOCKED_STRATEGY_MAPPING`` records the owner per-trade decision set routed out
of strategy taxonomy (4 -> Historical Cleaning, 1 -> Position Management):

  1. the handoff names exactly the 5 excluded trade_refs, matching the
     application artifact's excluded handoff set;
  2. each carries provenance to a real decision_id in the per-trade decisions
     jsonl;
  3. the 8 keep_blocked records are NOT in the handoff;
  4. the handoff makes no strategy assignment (no target_strategy_id);
  5. the committed Step-0 universe is unchanged (git diff guard);
  6. the merged decision / application artifacts are unchanged (git diff guard).
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LANE = REPO / "canonical/adjudications/strategy-mapping-review"
DECISIONS = LANE / "strategy-mapping-per-trade-decisions-20260910.jsonl"
APPLICATION = LANE / "strategy-mapping-per-trade-application-20260910.jsonl"
HANDOFF = LANE / "strategy-mapping-residual-lane-handoff-20260910.jsonl"
HANDOFF_MANIFEST = LANE / "strategy-mapping-residual-lane-handoff-20260910.manifest.json"


def _jsonl(path: Path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture(scope="module")
def decisions():
    return _jsonl(DECISIONS)


@pytest.fixture(scope="module")
def application():
    return _jsonl(APPLICATION)


@pytest.fixture(scope="module")
def handoff():
    return _jsonl(HANDOFF)


@pytest.fixture(scope="module")
def manifest():
    return json.loads(HANDOFF_MANIFEST.read_text(encoding="utf-8"))


def _git(*args):
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout


def _merge_base():
    out = _git("merge-base", "HEAD", "origin/main")
    return out.strip() if out else None


# --- (1) exactly the 5 excluded handoff trade_refs -------------------------

def test_1_names_exactly_the_5_handoff_records(handoff, application):
    app_handoffs = {
        r["trade_ref"]: r["excluded_disposition"]
        for r in application
        if r.get("excluded_disposition")
        in {"historical_cleaning_handoff", "position_management_handoff"}
    }
    assert len(app_handoffs) == 5

    ho_refs = [r["trade_ref"] for r in handoff]
    assert len(ho_refs) == len(set(ho_refs)) == 5
    assert set(ho_refs) == set(app_handoffs)

    lane_by_ref = {r["trade_ref"]: r["target_lane"] for r in handoff}
    for ref, dispo in app_handoffs.items():
        expected = {
            "historical_cleaning_handoff": "historical-cleaning",
            "position_management_handoff": "position-management",
        }[dispo]
        assert lane_by_ref[ref] == expected, ref

    tally = Counter(r["target_lane"] for r in handoff)
    assert tally == {"historical-cleaning": 4, "position-management": 1}


# --- (2) provenance to a real decision_id --------------------------------

def test_2_provenance_to_real_decision_ids(handoff, decisions, application):
    dec_ids = {d["decision_id"] for d in decisions}
    dec_by_id = {d["decision_id"]: d for d in decisions}
    app_by_ref = {r["trade_ref"]: r for r in application}
    for r in handoff:
        did = r["decision_id"]
        assert did in dec_ids, did
        assert r["provenance"]["per_trade_decision"]["decision_id"] == did
        # decision + application rows agree on the trade_ref
        assert dec_by_id[did]["trade_ref"] == r["trade_ref"]
        assert app_by_ref[r["trade_ref"]]["decision_id"] == did
        # step-0 blocker provenance points at a real blocked record
        blk_rid = r["provenance"]["step0_blocker"]["record_id"]
        assert dec_by_id[did]["record_id"] == blk_rid
        assert r["provenance"]["step0_blocker"]["gate_outcome"] == "BLOCKED_STRATEGY_MAPPING"


def test_2b_step0_blocker_backlog_actually_contains_them(handoff):
    backlog = _jsonl(REPO / "exports/strategy-discovery/step-0/blocker-backlog.jsonl")
    blocked = {
        b["record_id"]: b["gate_outcome"]
        for b in backlog
        if b.get("gate_outcome") == "BLOCKED_STRATEGY_MAPPING"
    }
    for r in handoff:
        rid = r["provenance"]["step0_blocker"]["record_id"]
        assert blocked.get(rid) == "BLOCKED_STRATEGY_MAPPING", rid


# --- (3) the 8 keep_blocked are NOT in the handoff -----------------------

def test_3_keep_blocked_not_in_handoff(handoff, application, manifest):
    keep_blocked = {
        r["trade_ref"] for r in application if r.get("excluded_disposition") == "keep_blocked"
    }
    assert len(keep_blocked) == 8
    ho_refs = {r["trade_ref"] for r in handoff}
    assert keep_blocked.isdisjoint(ho_refs)
    assert set(manifest["keep_blocked_NOT_handed_off"]["trade_refs"]) == keep_blocked
    assert manifest["keep_blocked_NOT_handed_off"]["count"] == 8


# --- (4) no strategy assignment ----------------------------------------

def test_4_no_strategy_assignment(handoff):
    for r in handoff:
        assert r["target_strategy_id"] is None, r["trade_ref"]
        assert r["strategy_decision_made"] is False
        assert r["retained_gate_state"] == "BLOCKED_STRATEGY_MAPPING"
        assert r["blocked_by_design"] is True
        assert r["human_decision"] in {
            "route_to_historical_cleaning_review",
            "position_management_provenance",
        }


# --- (5) committed Step-0 universe unchanged --------------------------

def test_5_step0_universe_unchanged():
    # Durable purpose: this handoff slice makes no strategy decision and must not
    # touch the Step-0 data universe or its inputs. Registry/rule-version changes
    # belong to later governed stages and are covered by their own tests.
    mb = _merge_base()
    if mb is None:
        pytest.skip("git merge-base unavailable")
    diff = _git("diff", "--name-only", f"{mb}...HEAD")
    assert diff is not None
    changed = [x for x in diff.splitlines() if x.strip()]
    from legacy_test_support import unpreserved_legacy_changes
    for path in changed:
        assert not path.startswith("exports/strategy-discovery/"), path
    # trades/ may only leave the tree as the exact, ledger-preserved retirement.
    assert unpreserved_legacy_changes(mb, changed) == []


# --- (6) merged decision / application artifacts unchanged ------------

def test_6_decision_and_application_artifacts_unchanged():
    mb = _merge_base()
    if mb is None:
        pytest.skip("git merge-base unavailable")
    frozen = [
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-per-trade-decisions-20260910.jsonl",
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-per-trade-decisions-20260910.manifest.json",
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-per-trade-application-20260910.jsonl",
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-per-trade-application-20260910.manifest.json",
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-human-decisions-20260910.jsonl",
        "canonical/adjudications/historical-cleaning-human-revisions.jsonl",
        "canonical/adjudications/phase4c-machine-revisions.jsonl",
        "canonical/adjudications/phase4d-human-revisions.jsonl",
    ]
    for path in frozen:
        diff = _git("diff", f"{mb}...HEAD", "--", path)
        assert diff is not None
        assert diff.strip() == "", path


# --- manifest integrity ---------------------------------------------

def test_manifest_digest_and_invariants(manifest):
    import hashlib

    assert manifest["handoff_artifact_sha256"] == hashlib.sha256(HANDOFF.read_bytes()).hexdigest()
    assert manifest["record_count"] == 5
    assert manifest["handoff_breakdown"] == {
        "historical_cleaning": 4,
        "position_management": 1,
        "total": 5,
    }
    for name, val in manifest["invariants"].items():
        assert val is True, name
    assert len(manifest["handed_off_records"]) == 5
