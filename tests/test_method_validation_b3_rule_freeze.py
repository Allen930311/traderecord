from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import yaml

from trading_os.registry import (
    load_effective_rules,
    load_registry,
    load_strategy,
    load_version,
    validate_rule_version,
)
from trading_os.rule_freeze import (
    ACTIVE_STRATEGY_IDS,
    B1_DATASET_PATH,
    B1_DATASET_SHA256,
    B1_MANIFEST_DIGEST,
    EVIDENCE_PLAN,
    FORBIDDEN_EVIDENCE_MARKERS,
    IMMUTABLE_UPSTREAM_SHA256,
    _canonical_definition_content,
    _pre_trade_body,
    build_b3_manifest,
    validate_b3_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA = "34fc9bc93519f6511800aca31f8c6370ab8361a0"
B3_DIR = ROOT / "exports" / "method-validation" / "b3"
B3_MANIFEST = B3_DIR / "manifest.json"
FORBIDDEN_RULE_FIELDS = {"result", "realized_r", "r_realized", "pnl", "profit", "outcome"}
EXPECTED_STABLE_REGISTRY_METADATA = {
    "stg-vegas-tunnel": {
        "display_name": "Vegas Tunnel",
        "legacy_number": 1,
        "classification": "independent_system",
        "status": "active",
        "legacy_aliases": [
            "vegas-tunnel", "1-vegas-tunnel", "vt-long", "vt-short",
            "vegas-retest", "mss-vt", "mss-vt-short", "2-mss-vegas",
            "mss-vegas", "mss-vt-long", "mss-bos-vegas", "dual-channel-retest",
        ],
    },
    "stg-po3": {
        "display_name": "PO3 / Power of 3",
        "classification": "independent_system",
        "status": "active",
        "legacy_aliases": ["po3"],
    },
    "stg-example-a": {
        "display_name": "AA 策略",
        "classification": "independent_system",
        "status": "active",
        "legacy_aliases": ["aa", "AA策略"],
    },
    "stg-example-b": {
        "display_name": "OP (Optimal Position)",
        "legacy_number": 11,
        "classification": "independent_system",
        "status": "retired",
        "retired_on": "2026-09-09",
        "retired_note": "Batch-2 slot 07 + owner confirmation: all OP retired, opv3 (slot 13x indicator-test) included. MTF MSS + OB/PB, 15m minimum.",
        "legacy_aliases": ["op", "op-strategy", "op #11", "11_2", "opv3", "op-indicator-v3"],
    },
    "stg-ict-liquidity": {
        "display_name": "ICT 指標流動性 (Liquidity models)",
        "legacy_number": None,
        "classification": "independent_system",
        "status": "active",
        "variants": ["triple-tap", "sweep-mss-ob"],
        "legacy_aliases": ["sr", "sr-flip", "mss-rejection", "bos-retest", "support-retest", "OB + MSS", "ob"],
    },
    "stg-triple-tap": {
        "display_name": "Triple Tap (+ RSI divergence + MSS)",
        "legacy_number": 8,
        "classification": "variant",
        "parent_strategy_id": "stg-ict-liquidity",
        "status": "active",
        "legacy_aliases": ["triple-tap", "8-triple-tap", "triple_tap"],
    },
    "stg-liq-sweep": {
        "display_name": "Liquidity Sweep + MSS + OB",
        "legacy_number": 4,
        "classification": "variant",
        "parent_strategy_id": "stg-ict-liquidity",
        "status": "retired",
        "retired_on": "2026-09-09",
        "retired_note": "Batch-2 slot 04; owner: 已退役. The 2022 ICT mentor sweep -> MSS -> OB-retest model. Distinct from the still-active triple-tap sibling.",
        "legacy_aliases": ["liq-sweep", "4-liq-sweep", "liq_sweep", "sweep-mss-ob"],
    },
    "stg-17ema": {
        "display_name": "17 EMA Historical Experiment",
        "legacy_number": None,
        "classification": "experimental",
        "status": "experimental",
        "legacy_aliases": ["17ema"],
    },
    "stg-ma-cluster": {
        "display_name": "MA Cluster Historical Experiment",
        "legacy_number": None,
        "classification": "experimental",
        "status": "experimental",
        "legacy_aliases": ["ma-cluster"],
    },
    "stg-fib-rsi": {
        "display_name": "Fib-RSI Historical Experiment",
        "legacy_number": None,
        "classification": "experimental",
        "status": "experimental",
        "legacy_aliases": ["fib-rsi"],
    },
    "stg-unicorn": {
        "display_name": "Unicorn / 獨角獸 Historical Experiment",
        "legacy_number": None,
        "classification": "experimental",
        "status": "experimental",
        "legacy_aliases": ["unicorn"],
    },
    "stg-ifvg-mss": {
        "display_name": "IFVG + OB + MSS",
        "legacy_number": None,
        "classification": "independent_system",
        "status": "retired",
        "retired_on": "2026-09-09",
        "retired_note": "Batch-2 slot 02; owner: 已退役. 5m IFVG/OB retest + 15m MSS.",
        "legacy_aliases": ["ifvg-mss", "IFVG-OB-MSS", "ifvg"],
    },
    "stg-ott-auto": {
        "display_name": "OTT Auto (MSS + OTT counter-trend)",
        "legacy_number": None,
        "classification": "independent_system",
        "status": "retired",
        "retired_on": "2026-09-09",
        "retired_note": "Batch-2 slot 08; owner: 已退役. OTT/OP indicator confluence, 15m.",
        "legacy_aliases": ["ott-auto", "MSS_OTT", "ott"],
    },
    "stg-1272-fakeout": {
        "display_name": "1.272 Fakeout + RSI",
        "legacy_number": 6,
        "classification": "independent_system",
        "status": "retired",
        "retired_on": "2026-09-09",
        "retired_note": "Batch-2 slot 12; owner: 已退役. 1.272 fake breakout/breakdown + RSI extremes (turtle-soup style).",
        "legacy_aliases": ["1272-fakeout", "6-1272-fakeout", "1272_fakeout"],
    },
}
EXPECTED_NON_ACTIVE_RULE_VERSIONS = {
    "stg-example-b": ["stg-example-b:v1"],
    "stg-liq-sweep": [],
    "stg-17ema": [],
    "stg-ma-cluster": [],
    "stg-fib-rsi": [],
    "stg-unicorn": [],
    "stg-ifvg-mss": [],
    "stg-ott-auto": [],
    "stg-1272-fakeout": [],
}


def _sha256(path: Path) -> str:
    from legacy_test_support import REPO_ROOT, legacy_bytes
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(legacy_bytes(path.relative_to(REPO_ROOT).as_posix())).hexdigest()


def _b1_membership() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for line in (ROOT / B1_DATASET_PATH).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("strategy_id") in ACTIVE_STRATEGY_IDS and row.get("strategy_status") == "active":
            result[row["source_path"]] = {
                "strategy_id": row["strategy_id"],
                "source_sha256": row["source_sha256"],
                "source_body_sha256": row["source_body_sha256"],
            }
    return result


def _strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def test_committed_b3_manifest_is_valid_and_remains_historically_pinned():
    manifest = json.loads(B3_MANIFEST.read_text(encoding="utf-8"))
    assert validate_b3_manifest(manifest) == []
    # The committed B3 is a historical artifact. A post-B3 active registry
    # extension must not rebuild it against today's expanded registry.
    assert manifest["active_strategy_ids"] == list(ACTIVE_STRATEGY_IDS)
    assert manifest["b1_pin"] == {
        "dataset_version": "method-validation-b1.v2",
        "dataset_sha256": B1_DATASET_SHA256,
        "manifest_digest": B1_MANIFEST_DIGEST,
    }
    assert manifest["registry"]["before"]["registry_digest"] == "5d4069adbd4b35d8d6c344fb1f0fd8b533fdc3d1ffe501900fc0e456b961f4f4"
    assert manifest["registry"]["after"]["registry_digest"] == "c337d9c6f58b887c82960211b5c4fb763e10401f4975198de2c8c74c83cf6e20"
    assert _sha256(ROOT / B1_DATASET_PATH) == "c49af93748864bb863192bb46fea19f314a3107c9455a4d587c2f16835714ed5"
    assert _sha256(ROOT / "exports/strategy-discovery/step-0/step-0-results.jsonl") == "84c27655b478d11695cd77fdd6bc0ae7fada01cb8f73c83be127d1fe9aabfc78"
    assert manifest["negative_guarantees"]["b1_manifest_unchanged"] is True
    assert manifest["negative_guarantees"]["step0_manifest_unchanged"] is True


def test_committed_b3_manifest_still_rebuilds_deterministically_from_the_historical_registry(monkeypatch):
    # The rebuild guarantee is kept: against the registry as B3 froze it (the
    # Owner-approved post-B3 additions removed) the builder must reproduce the
    # committed manifest byte for byte.
    import trading_os.rule_freeze as rule_freeze
    from trading_os.registry import registry_as_of_b3

    manifest = json.loads(B3_MANIFEST.read_text(encoding="utf-8"))
    historical = registry_as_of_b3(load_registry(ROOT))
    registry_file = (ROOT / rule_freeze.REGISTRY_PATH).resolve()
    real_sha256_file = rule_freeze._sha256_file

    def historical_sha256_file(path):
        if Path(path).resolve() == registry_file:
            return manifest["registry"]["after"]["file_sha256"]
        return real_sha256_file(path)

    monkeypatch.setattr(rule_freeze, "load_registry", lambda root: historical)
    monkeypatch.setattr(rule_freeze, "_sha256_file", historical_sha256_file)
    assert build_b3_manifest(ROOT, baseline_sha=BASELINE_SHA, freeze_date="2026-09-11") == manifest


def test_immutable_upstream_artifacts_match_the_b3_baseline_pins():
    paths = {
        "b1_manifest": "exports/method-validation/b1/manifest.json",
        "b1_dataset": "exports/method-validation/b1/canonical-trade-dataset.jsonl",
        "step0_corpus_manifest": "exports/strategy-discovery/step-0/corpus-manifest.json",
        "step0_results": "exports/strategy-discovery/step-0/step-0-results.jsonl",
    }
    for key, relative_path in paths.items():
        assert _sha256(ROOT / relative_path) == IMMUTABLE_UPSTREAM_SHA256[key]
    b1_manifest = json.loads((ROOT / "exports/method-validation/b1/manifest.json").read_text(encoding="utf-8"))
    step0_manifest = json.loads((ROOT / "exports/strategy-discovery/step-0/corpus-manifest.json").read_text(encoding="utf-8"))
    assert b1_manifest["manifest_digest"] == B1_MANIFEST_DIGEST
    assert b1_manifest["pinned_source_digests"]["registry_digest"] == "5d4069adbd4b35d8d6c344fb1f0fd8b533fdc3d1ffe501900fc0e456b961f4f4"
    assert step0_manifest["manifest_digest"] == "df9382cdb4dd2ff99cd57ecd3f4bc1183c1592ac1bf0300f9ded22fdabac83b7"
    assert step0_manifest["registry"]["digest"] == "5d4069adbd4b35d8d6c344fb1f0fd8b533fdc3d1ffe501900fc0e456b961f4f4"


def test_active_strategy_coverage_and_registry_pointers_are_closed():
    registry = load_registry(ROOT)
    by_id = {item["strategy_id"]: item for item in registry["strategies"]}
    current_active_ids = {
        item["strategy_id"] for item in registry["strategies"] if item.get("status") == "active"
    }
    # ACTIVE_STRATEGY_IDS is the immutable historical B1/B3 population. AA is
    # a post-B3 operational extension and must not rewrite that freeze.
    assert current_active_ids == set(ACTIVE_STRATEGY_IDS) | {"stg-example-a"}
    assert {sid: by_id[sid]["rule_versions"] for sid in ACTIVE_STRATEGY_IDS} == {
        "stg-vegas-tunnel": ["stg-vegas-tunnel:v1"],
        "stg-po3": ["stg-po3:v1"],
        "stg-ict-liquidity": ["stg-ict-liquidity:v1"],
        "stg-triple-tap": ["stg-triple-tap:v1"],
    }
    for item in registry["strategies"]:
        if item["strategy_id"] not in current_active_ids:
            assert item["rule_versions"] == EXPECTED_NON_ACTIVE_RULE_VERSIONS[item["strategy_id"]]
    aa = load_version(ROOT, "stg-example-a:v2")
    assert aa["status"] == "active"
    assert aa["lifecycle_at_freeze"] == "active"
    assert aa["freeze_baseline_sha"] == "e314e3f6dc1bfc630a4bf53d5ac49974c880af7e"


def test_registry_taxonomy_and_lifecycle_fields_are_unchanged_from_baseline():
    current = yaml.safe_load((ROOT / "registry/strategy-registry.yaml").read_text(encoding="utf-8"))
    current_by_id = {item["strategy_id"]: dict(item) for item in current["strategies"]}
    assert set(current_by_id) == set(EXPECTED_STABLE_REGISTRY_METADATA)
    for strategy_id, expected in EXPECTED_STABLE_REGISTRY_METADATA.items():
        actual = current_by_id[strategy_id]
        actual.pop("strategy_id", None)
        actual.pop("rule_versions", None)
        assert actual == expected


def test_frozen_versions_have_valid_claims_and_no_retired_reactivation():
    registry = load_registry(ROOT)
    for strategy_id in ACTIVE_STRATEGY_IDS:
        strategy = load_strategy(ROOT, strategy_id)
        version = load_version(ROOT, strategy["rule_versions"][0])
        assert version["schema_version"] == "strategy-rule-version.v1"
        assert version["status"] == "active"
        assert version["lifecycle_at_freeze"] == "active"
        assert version["freeze_baseline_sha"] == BASELINE_SHA
        assert version["rule_version"] == version["version_id"]
        assert version["parent_strategy_id"] == strategy.get("parent_strategy_id")
        assert all(rule["allowed_evidence_timing"] == ["pre_trade"] for rule in version["rules"])
        assert all(rule["claim_class"] for rule in version["rules"])
        assert not any(
            marker in " ".join(_strings(version)).lower()
            for marker in FORBIDDEN_RULE_FIELDS
        )
    aa = load_version(ROOT, "stg-example-a:v2")
    assert all(rule["allowed_evidence_timing"] == ["pre_trade"] for rule in aa["rules"])
    assert all(rule["claim_class"] for rule in aa["rules"])
    assert not any(marker in " ".join(_strings(aa)).lower() for marker in FORBIDDEN_RULE_FIELDS)
    assert not (ROOT / "registry/versions/stg-vegas-tunnel-v2.yaml").exists()
    for item in registry["strategies"]:
        if item.get("status") in {"retired", "experimental"}:
            assert item["rule_versions"] == EXPECTED_NON_ACTIVE_RULE_VERSIONS[item["strategy_id"]]


def test_po3_psar_is_required_by_the_canonical_entry_sequence():
    version = load_version(ROOT, "stg-po3:v1")
    psar = next(rule for rule in version["rules"] if rule["rule_id"] == "po3.psar_confirmation")
    assert psar["required"] is True
    assert psar["claim_class"] == "REQUIRED"
    assert all(item["item_id"] != "po3.psar_requirement" for item in version["unresolved"])


def test_non_blocking_unresolved_items_cannot_affect_eligibility():
    for strategy_id in ACTIVE_STRATEGY_IDS:
        version = load_version(ROOT, load_strategy(ROOT, strategy_id)["rule_versions"][0])
        assert all(
            item["blocking"] is False and item["eligibility_impact"] is False
            for item in version.get("unresolved", [])
        )
    po3 = load_version(ROOT, "stg-po3:v1")
    invalid = json.loads(json.dumps(po3))
    invalid["unresolved"][0]["eligibility_impact"] = True
    assert any(
        error["path"] == "unresolved[0].eligibility_impact"
        for error in validate_rule_version(invalid)
    )


def test_triple_tap_inherits_parent_without_rule_duplication():
    version = load_version(ROOT, "stg-triple-tap:v1")
    parent = load_effective_rules(ROOT, "stg-ict-liquidity:v1")
    effective = load_effective_rules(ROOT, "stg-triple-tap:v1")
    parent_ids = [rule["rule_id"] for rule in parent]
    own_ids = [rule["rule_id"] for rule in version["rules"]]
    assert version["inherits_from"] == "stg-ict-liquidity:v1"
    assert version["inheritance"]["inherited_rule_ids"] == parent_ids
    assert not set(parent_ids) & set(own_ids)
    assert [rule["rule_id"] for rule in effective] == parent_ids + own_ids


def test_evidence_refs_are_active_b1_pretrade_provenance():
    membership = _b1_membership()
    manifest = json.loads(B3_MANIFEST.read_text(encoding="utf-8"))
    for strategy in manifest["strategy_versions"]:
        for evidence in strategy["evidence_refs"]:
            assert evidence["timing"] == "pre_trade"
            assert evidence["locator"]
            assert len(evidence["content_sha256"]) == 64
            if evidence["source_ref"] == "STRATEGIES_OVERVIEW.md":
                raw = (ROOT / evidence["source_ref"]).read_bytes()
                content = _canonical_definition_content(raw.decode("utf-8"), evidence["locator"])
                assert evidence["sha256"] == _sha256(ROOT / evidence["source_ref"])
                assert evidence["content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
                assert not any(marker.casefold() in content.casefold() for marker in FORBIDDEN_EVIDENCE_MARKERS)
                continue
            pinned = membership[evidence["source_ref"]]
            assert pinned["strategy_id"] == strategy["strategy_id"]
            assert evidence["sha256"] == pinned["source_sha256"] == _sha256(ROOT / evidence["source_ref"])
            assert evidence["body_sha256"] == pinned["source_body_sha256"]
            from legacy_test_support import legacy_text
            content = _pre_trade_body(legacy_text(evidence["source_ref"]))
            assert evidence["content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
            assert not any(marker.casefold() in content.casefold() for marker in FORBIDDEN_EVIDENCE_MARKERS)


def test_evidence_plan_uses_bounded_canonical_selectors_and_excludes_vegas_optimization():
    vegas = next(item for item in EVIDENCE_PLAN["stg-vegas-tunnel"] if item["evidence_id"] == "definition.vegas")
    assert vegas["locator"] == "lines 18-20"
    assert all(item["evidence_id"] != "trade.vegas.2026-06-01" for item in EVIDENCE_PLAN["stg-vegas-tunnel"])
    content = _canonical_definition_content(
        (ROOT / "STRATEGIES_OVERVIEW.md").read_text(encoding="utf-8"), vegas["locator"]
    )
    assert not any(marker.casefold() in content.casefold() for marker in FORBIDDEN_EVIDENCE_MARKERS)
    assert "第二次 VT" not in load_version(ROOT, "stg-vegas-tunnel:v1")["rule_surface"]["setup_sequence"]


def test_b3_generation_does_not_mutate_sources_or_rule_versions():
    protected = [
        "exports/method-validation/b1/canonical-trade-dataset.jsonl",
        "exports/strategy-discovery/step-0/step-0-results.jsonl",
        "registry/strategy-registry.yaml",
        "registry/versions/stg-example-b-v1.yaml",
    ]
    before = {path: _sha256(ROOT / path) for path in protected}
    versions_before = {
        path: _sha256(ROOT / path)
        for path in (ROOT / "registry/versions").glob("*.yaml")
    }
    manifest = build_b3_manifest(ROOT, baseline_sha=BASELINE_SHA, freeze_date="2026-09-11")
    assert manifest["read_only_inputs"] is True
    assert {path: _sha256(ROOT / path) for path in protected} == before
    assert {
        path: _sha256(path) for path in (ROOT / "registry/versions").glob("*.yaml")
    } == versions_before

    # Commit-scope checks belong to the PR review: Actions uses a shallow
    # synthetic merge checkout without a usable parent or baseline object.
    # The hash snapshots above are the runtime read-only contract for this
    # generator and remain valid in both full and shallow checkouts.


def test_b3_cli_smoke_is_machine_readable(tmp_path: Path):
    output_dir = tmp_path / "b3"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trading_os",
            "b3-freeze",
            "--freeze-date",
            "2026-09-11",
            "--baseline-sha",
            BASELINE_SHA,
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["schema_version"] == "method-validation-b3.v1"
    assert output["manifest_digest"] == json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))["manifest_digest"]
