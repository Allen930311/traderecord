"""PR-B: owner strategy-mapping taxonomy applied to the operational registry.

Proves that ``registry/strategy-registry.yaml`` now operationalises the 2026-09-10
owner (example-owner) human decision set ``strategy-mapping-review-20260910``:

  * consumes the PR-A JSONL as authority (STEP 6 consumption layer), and
  * applies exactly the required registry edits, nothing more (STEP 5), and
  * fails closed on every label the owner intentionally left unmapped (STEP 8).

Exact string equality only: no casefold / trim / punctuation-normalise / fuzzy.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from trading_os.registry import load_registry, validate_registry
from trading_os.strategy_discovery import build_strategy_lookup, classify_strategy_label

REPO = Path(__file__).resolve().parents[1]
JSONL = REPO / "canonical/adjudications/strategy-mapping-review/strategy-mapping-human-decisions-20260910.jsonl"

VEGAS_HUMAN_ALIASES = {
    "vegas-tunnel", "1-vegas-tunnel", "vt-long", "vt-short", "vegas-retest",
    "mss-vt", "mss-vt-short", "2-mss-vegas", "mss-vegas", "mss-vt-long",
    "mss-bos-vegas", "dual-channel-retest",
}
ICT_HUMAN_ALIASES = {"sr", "sr-flip", "mss-rejection", "bos-retest", "support-retest", "OB + MSS"}
EXPERIMENTAL_IDS = {"stg-17ema", "stg-ma-cluster", "stg-fib-rsi", "stg-unicorn"}
EXPERIMENTAL_ALIAS_TO_ID = {
    "17ema": "stg-17ema", "ma-cluster": "stg-ma-cluster",
    "fib-rsi": "stg-fib-rsi", "unicorn": "stg-unicorn",
}
FORBIDDEN_IDS = {
    "stg-fvg", "stg-fvg-retrace", "stg-ob", "stg-unknown", "stg-tda",
    "stg-unspecified", "stg-tail-risk", "stg-audit-missing",
}
# `ob` was moved OUT of this set by the per-trade registry-application slice
# (owner decision set strategy-mapping-per-trade-review-20260910, rulings
# ptr-ruling-1 / ptr-ruling-2): `ob` is now a permanent legacy_alias of
# stg-ict-liquidity, superseding PR-A group decision grp-ob (smhd-20260910-19).
# `fvg-retrace` and the rest stay intentionally unmapped.
INTENTIONALLY_UNMAPPED_LABELS = {
    "fvg-retrace", "unspecified", "unknown", "TDA", "audit-missing", "tail-risk",
}


@pytest.fixture(scope="module")
def registry() -> dict:
    return load_registry(REPO)


@pytest.fixture(scope="module")
def by_id(registry) -> dict:
    return {s["strategy_id"]: s for s in registry["strategies"]}


@pytest.fixture(scope="module")
def lookup(registry) -> dict:
    return build_strategy_lookup(registry)


@pytest.fixture(scope="module")
def decisions() -> list[dict]:
    return [json.loads(l) for l in JSONL.read_text("utf-8").splitlines() if l.strip()]


def _resolve(lookup, label):
    m = classify_strategy_label(label, lookup)
    return m["strategy"]["strategy_id"] if m["matched"] else None


# --- STEP 5: registry application ------------------------------------------

def test_1_registry_validates_clean(registry):
    assert validate_registry(registry, REPO) == []


def test_2_and_3_vegas_exists_and_active(by_id):
    assert "stg-vegas-tunnel" in by_id
    assert by_id["stg-vegas-tunnel"]["status"] == "active"


def test_4_vegas_aliases_superset_of_human_labels(by_id):
    aliases = set(by_id["stg-vegas-tunnel"].get("legacy_aliases", []))
    assert VEGAS_HUMAN_ALIASES <= aliases, VEGAS_HUMAN_ALIASES - aliases


def test_5_vegas_mss_confirmation_no_longer_a_variant(by_id):
    variants = by_id["stg-vegas-tunnel"].get("variants") or []
    assert "mss-confirmation" not in variants
    # preferred: no variant taxonomy on Vegas at all
    assert "variants" not in by_id["stg-vegas-tunnel"]


def test_6_7_8_po3(by_id):
    assert "stg-po3" in by_id
    po3 = by_id["stg-po3"]
    assert po3["classification"] == "independent_system"
    assert po3["status"] == "active"
    assert "po3" in po3.get("legacy_aliases", [])
    assert "parent_strategy_id" not in po3


def test_9_10_experimental_identities(by_id):
    for sid in EXPERIMENTAL_IDS:
        assert sid in by_id, sid
        assert by_id[sid]["classification"] == "experimental", sid
        assert by_id[sid]["status"] == "experimental", sid


def test_11_experimental_aliases_resolve(lookup):
    for alias, sid in EXPERIMENTAL_ALIAS_TO_ID.items():
        assert _resolve(lookup, alias) == sid, alias


def test_12_13_14_15_ict_liquidity(by_id, lookup):
    assert by_id["stg-ict-liquidity"]["status"] == "active"
    aliases = set(by_id["stg-ict-liquidity"].get("legacy_aliases", []))
    assert ICT_HUMAN_ALIASES <= aliases, ICT_HUMAN_ALIASES - aliases
    assert _resolve(lookup, "OB + MSS") == "stg-ict-liquidity"
    assert _resolve(lookup, "OB + MSS") != "stg-ifvg-mss"


def test_ob_alias_folds_into_ict_liquidity(by_id, lookup):
    """Per-trade registry-application slice (owner rulings ptr-ruling-1 /
    ptr-ruling-2): bare `ob` (incl. legacy system #9 / `9-ob`, all carrying the
    literal frontmatter label `ob`) folds into stg-ict-liquidity. It is an alias
    of that PARENT identity only; no stg-ob* identity is created."""
    assert "ob" in by_id["stg-ict-liquidity"].get("legacy_aliases", [])
    assert _resolve(lookup, "ob") == "stg-ict-liquidity"
    for s_id, s in by_id.items():
        if s_id != "stg-ict-liquidity":
            assert "ob" not in (s.get("legacy_aliases") or []), s_id
    assert not any(sid in by_id for sid in ("stg-ob", "stg-ob-system", "stg-order-block"))


def test_16_17_generic_mechanisms_not_aliased(registry, lookup):
    # `ob` is now a deliberate alias of stg-ict-liquidity (owner ruling,
    # superseding grp-ob); only `fvg-retrace` stays a generic-mechanism non-alias.
    for s in registry["strategies"]:
        assert "fvg-retrace" not in (s.get("legacy_aliases") or []), s["strategy_id"]
    assert _resolve(lookup, "fvg-retrace") is None
    assert _resolve(lookup, "ob") == "stg-ict-liquidity"


def test_18_forbidden_ids_absent(by_id):
    assert FORBIDDEN_IDS.isdisjoint(by_id)


def test_19_intentionally_unmapped_labels_have_no_alias(lookup):
    for label in INTENTIONALLY_UNMAPPED_LABELS:
        assert _resolve(lookup, label) is None, label


def test_20_lookup_has_no_collision(registry):
    # build_strategy_lookup raises Step0Error on any duplicate exact key.
    build_strategy_lookup(registry)


def test_stable_ids_not_renamed_or_disturbed(by_id):
    for sid in ("stg-example-b", "stg-liq-sweep", "stg-ifvg-mss", "stg-ott-auto",
                "stg-1272-fakeout", "stg-triple-tap"):
        assert sid in by_id, sid
    assert by_id["stg-triple-tap"]["status"] == "active"
    assert by_id["stg-liq-sweep"]["status"] == "retired"
    assert by_id["stg-liq-sweep"]["parent_strategy_id"] == "stg-ict-liquidity"


# --- STEP 6: decision -> registry consumption (reads the PR-A JSONL) --------

def test_step6_resolved_and_experimental_decisions_consumed(decisions, lookup):
    seen_buckets = set()
    for d in decisions:
        bucket = d["decision_bucket"]
        seen_buckets.add(bucket)
        label = d["legacy_strategy_label"]
        if bucket == "resolved_to_existing_strategy":
            assert _resolve(lookup, label) == d["target_strategy_id"], label
        elif bucket == "historical_experimental":
            assert _resolve(lookup, label) == EXPERIMENTAL_ALIAS_TO_ID[label], label
        elif bucket == "new_independent_strategy_intent":
            assert label == "po3"
            assert _resolve(lookup, label) == "stg-po3"
        elif label == "ob":
            # PR-A grp-ob (bucket mechanism_only) was superseded by the owner
            # per-trade rulings ptr-ruling-1 / ptr-ruling-2: `ob` -> stg-ict-liquidity.
            assert _resolve(lookup, label) == "stg-ict-liquidity"
        else:
            # mechanism_only / trade_level_review / insufficient_evidence /
            # audit_record_class: MUST NOT resolve.
            if label is not None:
                assert _resolve(lookup, label) is None, (bucket, label)
    # every bucket family present in the artifact was exercised
    assert {
        "resolved_to_existing_strategy", "historical_experimental",
        "new_independent_strategy_intent", "mechanism_only",
        "trade_level_review", "insufficient_evidence", "audit_record_class",
    } <= seen_buckets


def test_step6_all_31_group_decisions_present(decisions):
    assert len(decisions) == 31
    assert len({d["group_id"] for d in decisions}) == 31
    assert all(d["human_authority"] == "example-owner" for d in decisions)


# --- STEP 8: fail-closed -------------------------------------------------

def test_failclosed_lowercase_ob_mss_does_not_match(lookup):
    assert _resolve(lookup, "OB + MSS") == "stg-ict-liquidity"
    assert _resolve(lookup, "ob + mss") is None
    assert _resolve(lookup, "OB+MSS") is None
    assert _resolve(lookup, " OB + MSS ") is None


def test_failclosed_case_and_whitespace_variants(lookup):
    for bad in ("Vegas-Tunnel", "PO3", " po3", "17EMA", "SR", "Sr-Flip"):
        assert _resolve(lookup, bad) is None, bad


def test_failclosed_duplicate_alias_collision(registry):
    from trading_os.strategy_discovery import Step0Error

    poisoned = json.loads(json.dumps(registry))
    poisoned["strategies"].append({
        "strategy_id": "stg-collider",
        "classification": "experimental",
        "status": "experimental",
        "legacy_aliases": ["po3"],
        "rule_versions": [],
    })
    with pytest.raises(Step0Error, match="collision"):
        build_strategy_lookup(poisoned)


def test_failclosed_blocked_labels_never_resolve(lookup):
    for label in ("unknown", "TDA", "fvg-retrace", "unspecified",
                  "audit-missing", "tail-risk", "null", "generic-ob"):
        assert _resolve(lookup, label) is None, label
    # `ob` is the one former blocked label now deliberately folded in (owner ruling)
    assert _resolve(lookup, "ob") == "stg-ict-liquidity"


def test_failclosed_experimental_labels_terminal_not_active(by_id, lookup):
    for alias, sid in EXPERIMENTAL_ALIAS_TO_ID.items():
        m = classify_strategy_label(alias, lookup)
        assert m["matched"]
        assert m["strategy"]["status"] == "experimental", alias
