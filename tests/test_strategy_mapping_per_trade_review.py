"""Validation test for the Step-0.7 per-trade parent-recovery review packets.

The packet (exports/strategy-discovery/step-0.7-per-trade-review/) is a
NON-AUTHORITATIVE evidence set: one Per-Trade Review Card per residual
BLOCKED_STRATEGY_MAPPING record so the owner can decide each record's true parent
strategy later. It settles nothing and records no human decision.

These tests prove exact 67/67 coverage against the Step-0.6 residual queue, that
every proposal is pending a human decision with null human fields, that the
forbidden vocabulary (approved / resolved / authoritative) never appears, that the
light record-class buckets carry no strategy parent, and that no stg-fvg / stg-ob
identity is ever proposed.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
QUEUE_JSONL = REPO / "exports/strategy-discovery/step-0.6-residual-review-queue/strategy-mapping-residual-review-queue.jsonl"
BLOCKER_JSONL = REPO / "exports/strategy-discovery/step-0/blocker-backlog.jsonl"
# The step-0.7 packet was built from the 67-blocker Step-0 era and froze that
# blocker digest in its manifest. Step-0 has since been re-run (per-trade
# consumption slice); the live blocker backlog now holds only the 13 rows that
# stay blocked and no longer hashes to this value. The frozen provenance is the
# surviving structural invariant.
STEP0_67_ERA_BLOCKER_SHA256 = (
    "805b151f73802d40dfffb302fb8aedd81f0afb90cddd4785d394726167894454"
)
PKT = REPO / "exports/strategy-discovery/step-0.7-per-trade-review"
PROPOSALS = PKT / "per-trade-review-proposals.jsonl"
MANIFEST = PKT / "manifest.json"
OVERVIEW = PKT / "overview.md"
CARDS = PKT / "cards"

SEMANTIC_TYPE = "strategy-mapping-per-trade-review-proposal.v1"
REC_LABEL = "NON-AUTHORITATIVE AGENT RECOMMENDATION"
LIGHT_BUCKETS = {
    "audit_reconstruction_record_class",
    "position_management_record_class",
    "insufficient_evidence_keep_blocked",
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def proposals():
    return [json.loads(x) for x in PROPOSALS.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def queue_ids():
    rows = [json.loads(x) for x in QUEUE_JSONL.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {r["queue_id"] for r in rows}


def test_coverage_is_exactly_the_residual_queue(proposals, queue_ids):
    ids = [p["queue_id"] for p in proposals]
    assert len(ids) == 67
    assert len(ids) == len(set(ids)), "duplicate queue_id in proposals"
    assert set(ids) == queue_ids, "proposal coverage != residual queue"


def test_card_set_matches_proposals(proposals):
    card_ids = set()
    for f in sorted(CARDS.glob("batch-*.md")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("- **queue_id**:"):
                card_ids.add(line.split("`")[1])
    assert card_ids == {p["queue_id"] for p in proposals}
    assert len(card_ids) == 67


def test_bucket_sums(proposals):
    assert Counter(p["bucket"] for p in proposals) == Counter(EXPECTED_BUCKET_COUNTS)
    assert sum(EXPECTED_BUCKET_COUNTS.values()) == 67


def test_tier_and_confidence_sums(proposals):
    assert sum(Counter(p["tier"] for p in proposals).values()) == 67
    assert set(Counter(p["tier"] for p in proposals)) <= {"A", "B", "C", "D"}
    assert sum(1 for _ in proposals) == 67
    for p in proposals:
        assert p["recommendation_confidence"] in (None, "high", "medium", "low")


def test_every_proposal_is_pending_with_null_human_fields(proposals):
    for p in proposals:
        assert p["semantic_type"] == SEMANTIC_TYPE
        assert p["decision_status"] == "pending_human_decision"
        assert p["human_decision"] is None
        assert p["human_actor"] is None
        assert p["human_decided_at"] is None
        assert p["recommendation_label"] == REC_LABEL
        assert p["chart_observation_label"] == "agent_observation_only"


def test_forbidden_vocabulary_never_appears():
    for f in sorted(PKT.rglob("*")):
        if not f.is_file():
            continue
        low = f.read_text(encoding="utf-8").lower()
        low = low.replace("non-authoritative", "").replace("non_authoritative", "")
        for tok in ("approved", "resolv", "authoritative"):
            assert tok not in low, f"forbidden token {tok!r} in {f.name}"


def test_light_buckets_have_no_strategy_parent(proposals):
    for p in proposals:
        if p["bucket"] not in LIGHT_BUCKETS:
            continue
        assert not str(p["agent_recommended_parent"] or "").startswith("stg-")
        for c in p["agent_candidate_parents"]:
            assert not c["parent"].startswith("stg-"), (p["queue_id"], c)
        assert p["recommended_disposition"] in {
            "route_to_historical_cleaning_review",
            "position_management_provenance",
            "keep_blocked",
        }


def test_no_stg_fvg_or_stg_ob_identity_proposed(proposals):
    for p in proposals:
        cands = [c["parent"] for c in p["agent_candidate_parents"]]
        for c in cands + [p["agent_recommended_parent"] or ""]:
            assert not c.startswith("stg-fvg")
            assert not c.startswith("stg-ob")


def test_ob_bucket_not_collapsed_to_one_parent(proposals):
    ob = [p for p in proposals if p["bucket"] == "per_trade_parent_recovery"]
    assert len(ob) == 28
    recs = {p["agent_recommended_parent"] for p in ob}
    assert len(recs) > 1, "OB bucket must not default to a single parent (RULE-7)"


def test_every_proposal_has_ordered_candidates_and_basis(proposals):
    for p in proposals:
        assert p["agent_candidate_parents"], p["queue_id"]
        for c in p["agent_candidate_parents"]:
            assert c.get("parent") and c.get("basis")
        assert p["evidence_for"] and p["evidence_against_or_ambiguity"]
        assert isinstance(p["pr_a_rule_refs"], list)
        assert isinstance(p["owner_split_likely"], bool)


def test_manifest_matches_artifacts(manifest, proposals):
    assert manifest["record_count"] == 67
    assert manifest["baseline_main_sha"] == "55da84c362b3614dc43d770ffd243a4a848630fa"
    assert manifest["source_residual_queue_sha256"] == _sha256(QUEUE_JSONL)
    assert manifest["source_blocker_backlog_sha256"] == STEP0_67_ERA_BLOCKER_SHA256
    assert manifest["proposal_artifact_sha256"] == _sha256(PROPOSALS)
    assert manifest["overview_sha256"] == _sha256(OVERVIEW)
    for name, digest in manifest["card_digests"].items():
        assert digest == _sha256(CARDS / name)
    assert sum(manifest["bucket_counts"].values()) == 67
    assert sum(manifest["tier_counts"].values()) == 67
    assert manifest["batch_count"] == len(list(CARDS.glob("batch-*.md")))
    for inv, val in manifest["invariants"].items():
        assert val is True, f"invariant {inv} is not True"


def test_batches_cover_all_67_once(manifest):
    seen = []
    for b in manifest["batches"]:
        seen.extend(b["records"])
        assert 8 <= b["cards"] <= 12 or b is manifest["batches"][-1]
    assert sorted(seen) == list(range(1, 68))
