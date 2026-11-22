"""Validation test for the strategy-mapping residual trade-level review queue.

The queue (exports/strategy-discovery/step-0.6-residual-review-queue/) is a durable
triage projection of the post-PR-B Step-0 BLOCKED_STRATEGY_MAPPING records. It
resolves nothing; it is the input to a future per-trade human review campaign.

These tests re-derive the blocked universe from the committed Step-0 artifacts and
prove exact 67/67 coverage, one bucket per record, and that no entry is resolved.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
STEP0 = REPO / "exports/strategy-discovery/step-0"
QUEUE_DIR = REPO / "exports/strategy-discovery/step-0.6-residual-review-queue"
JSONL = QUEUE_DIR / "strategy-mapping-residual-review-queue.jsonl"
MANIFEST = QUEUE_DIR / "strategy-mapping-residual-review-queue-manifest.json"

EXPECTED_LABEL_COUNTS = {
    "ob": 28,
    "<null>": 19,
    "unspecified": 10,
    "audit-missing": 4,
    "unknown": 2,
    "fvg-retrace": 2,
    "tail-risk": 1,
    "TDA": 1,
}
EXPECTED_BUCKET_COUNTS = {
    "per_trade_parent_recovery": 28,
    "data_quality_then_parent_recovery": 19,
    "legacy_placeholder_recovery": 10,
    "mechanism_no_parent_known": 2,
    "audit_reconstruction_record_class": 4,
    "position_management_record_class": 1,
    "insufficient_evidence_keep_blocked": 3,
}


def _jsonl(p: Path):
    return [json.loads(l) for l in p.read_text("utf-8").splitlines() if l.strip()]


@pytest.fixture(scope="module")
def entries():
    return _jsonl(JSONL)


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST.read_text("utf-8"))


# Historical Step-0 baseline this queue was derived from (frozen in the queue
# manifest). Step-0 has since been officially re-run by the per-trade consumption
# slice from a later main; the queue and its recorded baseline stay immutable.
STEP0_67_ERA_BASELINE_SHA = "3f515dd88995cbc5a272e2f49550e279f957d060"


@pytest.fixture(scope="module")
def current_blocked_refs():
    rows = _jsonl(STEP0 / "blocker-backlog.jsonl")
    return {r["source_path"] for r in rows if r["gate_outcome"] == "BLOCKED_STRATEGY_MAPPING"}


def test_queue_is_the_frozen_67_blocker_universe(entries, current_blocked_refs):
    # The queue is a historical projection of the 67-record blocked universe.
    refs = [e["trade_ref"] for e in entries]
    assert len(refs) == 67
    assert len(set(refs)) == 67, "duplicate trade_ref in queue"
    # After the Step-0 re-run + per-trade consumption, only 13 rows stay blocked
    # (8 keep_blocked + 4 historical-cleaning + 1 position-management); every one
    # of them was in the historical 67.
    assert len(current_blocked_refs) == 13
    assert current_blocked_refs <= set(refs)


def test_queue_covers_blocked_universe_exactly_once(entries, current_blocked_refs):
    refs = [e["trade_ref"] for e in entries]
    assert len(refs) == 67
    assert len(set(refs)) == 67, "duplicate trade_ref in queue"
    assert current_blocked_refs <= set(refs), "a currently-blocked trade is missing from the queue"


def test_queue_ids_unique(entries):
    ids = [e["queue_id"] for e in entries]
    assert len(set(ids)) == len(ids) == 67


def test_every_entry_is_pending_and_unresolved(entries):
    for e in entries:
        assert e["human_review_status"] == "pending", e["trade_ref"]
        assert e["resolver"] is None
        assert e["resolved_at"] is None
        assert e["status"] in (
            "needs_trade_level_human_review",
            "keep_blocked_insufficient_evidence",
        )


def test_label_counts_match_expected(entries):
    got = Counter((e["legacy_strategy_label"] or "<null>") for e in entries)
    assert dict(got) == EXPECTED_LABEL_COUNTS


def test_bucket_sums_equal_67_and_each_record_one_bucket(entries):
    got = Counter(e["review_bucket"] for e in entries)
    assert dict(got) == EXPECTED_BUCKET_COUNTS
    assert sum(got.values()) == 67
    # exactly one review_bucket per record (a single string field)
    assert all(isinstance(e["review_bucket"], str) for e in entries)


def test_keep_blocked_bucket_is_unknown_and_tda(entries):
    kb = {
        e["legacy_strategy_label"]
        for e in entries
        if e["status"] == "keep_blocked_insufficient_evidence"
    }
    assert kb == {"unknown", "TDA"}


def test_no_new_strategy_identity_and_no_resolution(entries, manifest):
    for e in entries:
        assert "target_strategy_id" not in e or e.get("target_strategy_id") is None
        assert "resolved_strategy_id" not in e
    assert manifest["coverage"]["records_resolved_this_slice"] == 0
    assert manifest["resolves_nothing"] is True


def test_manifest_coverage_invariants(manifest, entries):
    cov = manifest["coverage"]
    assert cov["sum_equals_67"] is True
    assert cov["each_record_exactly_once"] is True
    assert cov["zero_omission"] is True
    assert cov["zero_duplication"] is True
    assert cov["every_entry_pending"] is True
    assert manifest["queue_record_count"] == 67
    # frozen provenance: the queue records the historical Step-0 baseline it was
    # derived from, which the later Step-0 re-run does not change.
    assert manifest["baseline_main_sha"] == STEP0_67_ERA_BASELINE_SHA
    assert manifest["outputs"]["queue"]["sha256"] == hashlib.sha256(
        JSONL.read_bytes()
    ).hexdigest()


def test_pr_a_group_linkage(entries):
    label_to_group = {
        "ob": "grp-ob",
        None: "grp-null-label",
        "unspecified": "grp-unspecified",
        "audit-missing": "grp-audit-missing",
        "unknown": "grp-unknown",
        "fvg-retrace": "grp-fvg-retrace",
        "tail-risk": "grp-tail-risk",
        "TDA": "grp-tda",
    }
    for e in entries:
        assert e["pr_a_group_id"] == label_to_group[e["legacy_strategy_label"]]
        assert e["pr_a_decision_id"].startswith("smhd-20260910-")
