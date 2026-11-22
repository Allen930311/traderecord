"""Permanent PR-A artifact-integrity test for the 2026-09-10 owner
strategy-mapping human-decision artifact.

Test-only. Proves the durable PR-A artifact faithfully and completely captures
the 31 group-level human decisions from the Step-0.5 review packet, that human
(not agent) authority is recorded, and that the immutable PR-A JSONL / manifest
pair stays byte-identical to its merged form.

PR-B (registry / taxonomy application) merged after PR-A, so the former
PR-A-era transient merge-gate guards are intentionally retired here:

  * registry-must-never-change / registry tree-OID pin
  * Vegas-Tunnel-forever-proposed
  * stg-po3-forever-absent
  * PR-A changed-file allowlist / full-repo scope guard

Those asserted a pre-PR-B world. The registry-application invariants now live in
``tests/test_strategy_mapping_registry_application.py``. What remains below is
permanent truth: the human decision set never changes.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKET = REPO / "exports/strategy-discovery/step-0.5-mapping-review"
LANE = REPO / "canonical/adjudications/strategy-mapping-review"
JSONL = LANE / "strategy-mapping-human-decisions-20260910.jsonl"
DSM = LANE / "strategy-mapping-human-decisions-20260910.manifest.json"

PACKET_BASELINE_SHA = "eab6e6b8ece850f92da0a411084b74c4abe256bc"
# Fresh main / artifact baseline (PR #20 merged the Step-0.5 packet onto main).
MAIN_BASELINE_SHA = "2f56c86c894e7e01117b0a55f29052262bd5d068"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def source_manifest() -> dict:
    return json.loads((PACKET / "strategy-mapping-review-manifest.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def source_proposals() -> dict:
    out = {}
    for line in (PACKET / "strategy-mapping-review-proposals.jsonl").read_text("utf-8").splitlines():
        line = line.strip()
        if line:
            j = json.loads(line)
            out[j["group_id"]] = j
    return out


@pytest.fixture(scope="module")
def decisions() -> list[dict]:
    return [json.loads(l) for l in JSONL.read_text("utf-8").splitlines() if l.strip()]


# --- source packet still the same 31-group / 210-record universe --------------

def test_source_manifest_still_31_groups(source_manifest):
    assert source_manifest["exact_label_group_count"] == 31
    assert len(source_manifest["groups"]) == 31
    assert source_manifest["non_authoritative"] is True
    assert source_manifest["human_decisions_recorded"] == 0


def test_source_record_total_is_210(source_manifest):
    assert source_manifest["group_record_total"] == 210
    assert sum(g["record_count"] for g in source_manifest["groups"]) == 210


def test_source_proposal_packet_unchanged(source_manifest):
    assert _sha256(PACKET / "strategy-mapping-review-proposals.jsonl") == source_manifest["proposal_artifact_digest"]
    for name, digest in source_manifest["human_packet_digests"].items():
        assert _sha256(PACKET / name) == digest, name


# The Step-0.5 packet was built from the 210-blocker / pre-PR-B Step-0 era. That
# provenance value is frozen inside the (immutable) packet manifest. Step-0 has
# since been officially re-run post-PR-B (132/42/14/0/67/255 universe), so the
# LIVE exports/strategy-discovery/step-0/blocker-backlog.jsonl no longer hashes
# to the packet's recorded source_blocker_artifact_sha256 — and must not, that is
# the new committed truth. The structural invariant that survives: the packet
# manifest still records the exact 210-era blocker digest it was derived from.
STEP0_210_ERA_BLOCKER_SHA256 = (
    "0ea3c7286c365ff000f8672f822d8353c0b3b33e7462df8883196f399c5312cc"
)


def test_packet_still_records_its_210_era_blocker_provenance(source_manifest):
    assert source_manifest["source_blocker_artifact_sha256"] == STEP0_210_ERA_BLOCKER_SHA256


# --- decision artifact: exact 31/31 coverage --------------------------------

def test_artifact_has_31_unique_group_decisions(decisions):
    assert len(decisions) == 31
    assert len({d["group_id"] for d in decisions}) == 31
    assert len({d["decision_id"] for d in decisions}) == 31


def test_group_sets_exactly_equal(decisions, source_manifest):
    assert {d["group_id"] for d in decisions} == {g["group_id"] for g in source_manifest["groups"]}


def test_no_duplicate_or_missing(decisions, source_manifest):
    src = {g["group_id"] for g in source_manifest["groups"]}
    seen = [d["group_id"] for d in decisions]
    assert sorted(seen) == sorted(src)
    assert len(seen) == len(set(seen))


def test_record_counts_match_source_per_group(decisions, source_manifest, source_proposals):
    man = {g["group_id"]: g["record_count"] for g in source_manifest["groups"]}
    for d in decisions:
        g = d["group_id"]
        assert d["source_record_count"] == man[g], g
        assert len(d["source_record_refs"]) == man[g], g
        assert len(source_proposals[g]["record_refs"]) == man[g], g
        assert d["source_record_refs"] == source_proposals[g]["record_refs"], g
        assert d["source_record_refs_sha256"] == hashlib.sha256(
            "\n".join(source_proposals[g]["record_refs"]).encode("utf-8")
        ).hexdigest(), g


def test_total_source_record_count_is_210(decisions):
    assert sum(d["source_record_count"] for d in decisions) == 210


# --- authority / status invariants -----------------------------------------

def test_every_decision_is_human_owner_authoritative(decisions):
    for d in decisions:
        assert d["human_authority"] == "example-owner", d["group_id"]
        assert d["authority_class"] == "human_owner", d["group_id"]
        assert d["decision_session_date"] == "2026-09-10", d["group_id"]


def test_every_decision_status_decided(decisions):
    assert all(d["decision_status"] == "decided" for d in decisions)


def test_no_agent_authoritative_and_no_pending(decisions):
    blob = JSONL.read_text("utf-8")
    assert "pending_human_decision" not in blob
    for d in decisions:
        assert d["source_proposal_ref"]["agent_recommendation_authority"] == "non_authoritative"
        assert d["registry_application_status"] == "pending"
        assert d["execution_status"] == "pending"


def test_source_provenance_present(decisions, source_manifest):
    for d in decisions:
        sm = d["source_manifest"]
        assert sm["source_step0_manifest_digest"] == source_manifest["source_step0_manifest_digest"]
        assert sm["source_blocker_artifact_sha256"] == source_manifest["source_blocker_artifact_sha256"]
        assert sm["proposal_artifact_digest"] == source_manifest["proposal_artifact_digest"]
        assert d["source_batch"] in {b["batch_id"] for b in source_manifest["batches"]}


# --- faithful capture of the 31 authoritative decisions --------------------

MAP_EXISTING = {
    "grp-1-vegas-tunnel": "stg-vegas-tunnel", "grp-vt-long": "stg-vegas-tunnel",
    "grp-vt-short": "stg-vegas-tunnel", "grp-vegas-retest": "stg-vegas-tunnel",
    "grp-mss-vt": "stg-vegas-tunnel", "grp-mss-vt-short": "stg-vegas-tunnel",
    "grp-2-mss-vegas": "stg-vegas-tunnel", "grp-mss-vegas": "stg-vegas-tunnel",
    "grp-mss-vt-long": "stg-vegas-tunnel", "grp-mss-bos-vegas": "stg-vegas-tunnel",
    "grp-dual-channel-retest": "stg-vegas-tunnel", "grp-vegas-tunnel": "stg-vegas-tunnel",
    "grp-sr-flip": "stg-ict-liquidity", "grp-sr": "stg-ict-liquidity",
    "grp-mss-rejection": "stg-ict-liquidity", "grp-bos-retest": "stg-ict-liquidity",
    "grp-support-retest": "stg-ict-liquidity", "grp-ob-mss": "stg-ict-liquidity",
}
EXPERIMENTAL = {"grp-17ema", "grp-ma-cluster", "grp-fib-rsi", "grp-unicorn"}
MECHANISM = {"grp-ob", "grp-fvg-retrace"}
TRADE_LEVEL = {"grp-null-label", "grp-unspecified"}
BLOCKED = {"grp-unknown", "grp-tda"}
AUDIT_CLASS = {"grp-audit-missing", "grp-tail-risk"}


def test_faithful_targets_and_lifecycle(decisions):
    by = {d["group_id"]: d for d in decisions}

    for g, target in MAP_EXISTING.items():
        assert by[g]["target_strategy_id"] == target, g
        assert by[g]["classification"] in {
            "not_independent_strategy_not_variant",
            "canonical_strategy_identity_do_not_split",
        }, g
        assert by[g]["lifecycle_intent"] in {"active", "active_inherited"}, g

    # PO3: independent ACTIVE intent, not applied, not a variant
    po3 = by["grp-po3"]
    assert po3["target_strategy_id"] is None
    assert po3["proposed_strategy_id_candidate"] == "stg-po3"
    assert po3["lifecycle_intent"] == "active"
    assert po3["classification"] == "independent_system"
    assert po3["decision_kind"] == "recognize_new_independent_strategy_intent"

    for g in EXPERIMENTAL:
        assert by[g]["lifecycle_intent"] == "experimental", g
        assert by[g]["target_strategy_id"] is None, g

    for g in MECHANISM:
        assert by[g]["target_strategy_id"] is None, g
        assert "mechanism" in by[g]["decision_kind"], g
    assert "retired" in by["grp-fvg-retrace"]["decision_kind"]
    assert by["grp-fvg-retrace"]["lifecycle_intent"] == "retired"

    for g in TRADE_LEVEL:
        assert by[g]["decision_kind"] == "needs_trade_level_human_review", g
        assert by[g]["target_strategy_id"] is None, g

    for g in BLOCKED:
        assert by[g]["decision_kind"] == "insufficient_evidence_keep_blocked", g
        assert by[g]["target_strategy_id"] is None, g

    for g in AUDIT_CLASS:
        assert by[g]["decision_kind"].startswith("non_strategy_record_class"), g
        assert by[g]["target_strategy_id"] is None, g


def test_ob_mss_not_ifvg_mss(decisions):
    by = {d["group_id"]: d for d in decisions}
    ob_mss = by["grp-ob-mss"]
    assert ob_mss["target_strategy_id"] == "stg-ict-liquidity"
    assert ob_mss["proposed_strategy_id_candidate"] != "stg-ifvg-mss"
    assert "no IFVG" in ob_mss["semantic_notes"]


def test_no_forbidden_strategy_ids_created(decisions):
    # forbidden ids must never appear as an applied target or a proposed candidate
    # (they may appear inside semantic_notes only as an explicit prohibition).
    forbidden = {"stg-fvg", "stg-unknown", "stg-tda", "stg-unspecified", "stg-po3-applied"}
    for d in decisions:
        assert d["target_strategy_id"] not in forbidden, d["group_id"]
        assert d["proposed_strategy_id_candidate"] not in forbidden, d["group_id"]
    # stg-po3 is allowed ONLY as an unapplied proposed candidate, never as a target
    for d in decisions:
        assert d["target_strategy_id"] != "stg-po3", d["group_id"]


def test_decision_set_manifest_consistent(decisions):
    dsm = json.loads(DSM.read_text("utf-8"))
    assert dsm["coverage"]["human_decision_group_count"] == 31
    assert dsm["coverage"]["source_record_count_total"] == 210
    assert dsm["decision_artifact_sha256"] == _sha256(JSONL)
    assert set(dsm["global_human_taxonomy_rules"]) == {f"RULE-{i}" for i in range(1, 9)}
    all_bucketed = [g for v in dsm["decision_buckets"].values() for g in v]
    assert sorted(all_bucketed) == sorted(d["group_id"] for d in decisions)


# --- PR-A artifact immutability (permanent) ------------------------------
#
# The PR-A human decision set is immutable once merged. PR-B and every later
# slice consume it read-only; it must never be re-authored. These are pinned
# git blob object ids as of the PR-A merge (main@2f56c86c...); PR-B does not
# touch either file, so they still match on this branch. Recompute with
# `git rev-parse HEAD:<path>` only if PR-A is legitimately re-issued.
#
# NOTE: the former PR-A-era transient guards (registry tree-OID pin,
# Vegas==proposed, stg-po3 absent, PR-A changed-file allowlist, full-repo scope
# guard) are intentionally retired now that PR-B has applied the owner taxonomy
# to registry/strategy-registry.yaml.

PR_A_IMMUTABLE_BLOB_OIDS = {
    "canonical/adjudications/strategy-mapping-review/strategy-mapping-human-decisions-20260910.jsonl":
        "83327ac472f9cd33ff622761325d5db4d930c424",
    "canonical/adjudications/strategy-mapping-review/strategy-mapping-human-decisions-20260910.manifest.json":
        "72d3dcabe5727cb88aef048028e1cf6a3472011a",
}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)


def _obj_id(path: str) -> str | None:
    p = _git("rev-parse", f"HEAD:{path}")
    return p.stdout.strip() if p.returncode == 0 else None


def test_pr_a_artifact_files_are_immutable():
    """The merged PR-A JSONL + manifest stay byte-identical (history-independent,
    shallow-safe): pinned git blob object ids, fail-closed."""
    mismatch = []
    for path, oid in PR_A_IMMUTABLE_BLOB_OIDS.items():
        assert (REPO / path).is_file(), f"missing PR-A artifact file: {path}"
        got = _obj_id(path)
        if got != oid:
            mismatch.append(f"{path}: expected {oid}, got {got}")
    assert not mismatch, "PR-A immutable artifact mutated:\n" + "\n".join(mismatch)


def test_pr_a_jsonl_and_manifest_stay_consistent(decisions):
    """Digest + coverage recorded in the manifest still describe the JSONL."""
    dsm = json.loads(DSM.read_text("utf-8"))
    assert dsm["decision_artifact_sha256"] == _sha256(JSONL)
    assert dsm["coverage"]["human_decision_group_count"] == len(decisions) == 31
    assert dsm["human_authority"] == "example-owner"
    assert dsm["invariants"]["no_agent_authoritative_decision"] is True
