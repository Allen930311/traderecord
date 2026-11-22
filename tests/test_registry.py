from pathlib import Path

from trading_os.contracts import validate_strategy
from trading_os.registry import (
    load_registry,
    load_strategy,
    load_version,
    resolve_version_path,
    validate_registry,
)


def test_registry_resolves_public_colon_version_id():
    root = Path(__file__).resolve().parents[1]
    registry = load_registry(root)
    assert validate_registry(registry, root) == []
    assert load_strategy(root, "stg-vegas-tunnel")["rule_versions"] == ["stg-vegas-tunnel:v1"]
    assert resolve_version_path(root, "stg-vegas-tunnel:v1").name == "stg-vegas-tunnel-v1.yaml"
    version = load_version(root, "stg-vegas-tunnel:v1")
    assert version["rules"][0]["operands"]["field"] == "strategy.setup_tags"


def test_retired_strategies_load_and_validate():
    root = Path(__file__).resolve().parents[1]
    registry = load_registry(root)
    assert validate_registry(registry, root) == []
    retired = {s["strategy_id"]: s for s in registry["strategies"] if s.get("status") == "retired"}
    assert {"stg-example-b", "stg-liq-sweep", "stg-ifvg-mss"} <= set(retired)
    liq = load_strategy(root, "stg-liq-sweep")
    assert liq["status"] == "retired"
    assert "liq-sweep" in liq["legacy_aliases"]
    assert liq["parent_strategy_id"] == "stg-ict-liquidity"
    assert load_strategy(root, "stg-triple-tap")["status"] == "active"
    assert load_strategy(root, "stg-ict-liquidity")["status"] == "active"


def test_parent_strategy_id_must_resolve():
    root = Path(__file__).resolve().parents[1]
    registry = load_registry(root)
    registry["strategies"].append({
        "strategy_id": "stg-orphan",
        "classification": "variant",
        "status": "retired",
        "parent_strategy_id": "stg-does-not-exist",
        "rule_versions": [],
    })
    errors = validate_registry(registry, root)
    assert any(e["path"].endswith("parent_strategy_id") for e in errors)


def test_strategy_status_is_vocab_controlled():
    base = {"strategy_id": "stg-x", "classification": "independent_system", "rule_versions": []}
    assert validate_strategy({**base, "status": "retired"}) == []
    assert validate_strategy({**base, "status": "active"}) == []
    bad = validate_strategy({**base, "status": "mothballed"})
    assert any(e.path == "status" for e in bad)


def test_legacy_aliases_must_be_string_list():
    base = {
        "strategy_id": "stg-x",
        "classification": "independent_system",
        "status": "retired",
        "rule_versions": [],
    }
    assert validate_strategy({**base, "legacy_aliases": ["a", "b"]}) == []
    bad = validate_strategy({**base, "legacy_aliases": ["a", 3]})
    assert any(e.path == "legacy_aliases" for e in bad)


def test_aa_keeps_frozen_nas100_v1_and_activates_instrument_neutral_v2():
    root = Path(__file__).resolve().parents[1]
    strategy = load_strategy(root, "stg-example-a")
    assert strategy["status"] == "active"
    assert strategy["legacy_aliases"] == ["aa", "AA策略"]
    assert strategy["rule_versions"] == ["stg-example-a:v2"]

    v1 = load_version(root, "stg-example-a:v1")
    assert v1["status"] == "candidate"
    assert v1["lifecycle_at_freeze"] == "experimental"
    assert v1["freeze_baseline_sha"] == "986ebaaf964c19ebddd3efef468c3f252e08f5d7"
    assert "NAS100" in v1["rule_surface"]["identity"]

    v2 = load_version(root, "stg-example-a:v2")
    assert v2["status"] == "active"
    assert v2["lifecycle_at_freeze"] == "active"
    assert v2["freeze_baseline_sha"] == "e314e3f6dc1bfc630a4bf53d5ac49974c880af7e"
    assert v2["effective_from"] == "2026-10-12"
    assert v2["supersedes"] == "stg-example-a:v1"
    assert "instrument-agnostic" in v2["rule_surface"]["identity"]
    assert "NAS100" not in v2["rule_surface"]["identity"]
    assert v2["rules"] == v1["rules"]
    assert all(rule["operands"]["field"] == "strategy.setup_tags" for rule in v2["rules"])
    assert all(rule["category"] != "execution" for rule in v2["rules"])
    assert "0.618-0.786 band used by AVF" in v2["rules"][2]["statement"]
    assert all(rule["rule_id"].startswith("example_a.") for rule in v2["rules"])
    assert "stg-example-a" != load_strategy(root, "stg-vegas-tunnel")["strategy_id"]
