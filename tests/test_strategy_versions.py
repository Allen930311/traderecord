"""A new rule version does not retire the old one; only the Owner stops a version.

Every listed, active version that is not in `retired_rule_versions` stays usable. The newest
usable version is the default; the Owner may name an older usable one.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from trading_os.contracts import validate_strategy
from trading_os.registry import load_registry, usable_rule_versions, validate_registry
from trading_os.strategy_resolver import ResolverError, active_strategies, resolve_strategy

ROOT = Path(__file__).resolve().parents[1]
VEGAS_V1, VEGAS_V2 = "stg-vegas-tunnel:v1", "stg-vegas-tunnel:v2"
PLAN = {"stop_loss_price": 1.0, "target_price": 2.0}


def _root(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "registry", tmp_path / "registry")
    return tmp_path


def _strategy(data: dict, strategy_id: str) -> dict:
    return next(item for item in data["strategies"] if item["strategy_id"] == strategy_id)


def _edit_registry(root: Path, edit) -> None:
    path = root / "registry" / "strategy-registry.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _add_vegas_v2(root: Path, *, extra_rule: bool = False, retired: tuple[str, ...] = ()) -> None:
    source = root / "registry" / "versions" / "stg-vegas-tunnel-v1.yaml"
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    data["version_id"] = data["rule_version"] = VEGAS_V2
    data["supersedes"] = VEGAS_V1
    if extra_rule:
        data["rules"].append({**data["rules"][0], "rule_id": "vegas.tunnel.reclaim"})
    (root / "registry" / "versions" / "stg-vegas-tunnel-v2.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    def edit(registry: dict) -> None:
        vegas = _strategy(registry, "stg-vegas-tunnel")
        vegas["rule_versions"].append(VEGAS_V2)
        if retired:
            vegas["retired_rule_versions"] = list(retired)

    _edit_registry(root, edit)


def obs(rule_id, state="PASS"):
    return {"rule_id": rule_id, "state": state, "timing": "pre_trade", "evidence_refs": ["chart:1"]}


def _attestation(**overrides):
    value = {
        "timing": "retrospective",
        "setup_status": "HAS_SETUP",
        "primary_strategy_id": "stg-vegas-tunnel",
        "rationale": "Vegas tunnel entry.",
        "evidence_refs": [],
    }
    value.update(overrides)
    return {"owner_attestation": value}


def _vegas_versions(root: Path) -> list[str]:
    entry = next(s for s in active_strategies(root) if s["strategy_id"] == "stg-vegas-tunnel")
    assert entry["rule_version"] == entry["usable_versions"][-1]["rule_version"]
    return [v["rule_version"] for v in entry["usable_versions"]]


def test_a_new_version_leaves_the_old_one_usable_and_becomes_the_default(tmp_path):
    root = _root(tmp_path)
    assert _vegas_versions(root) == [VEGAS_V1]
    _add_vegas_v2(root)
    assert _vegas_versions(root) == [VEGAS_V1, VEGAS_V2]
    entry = next(s for s in active_strategies(root) if s["strategy_id"] == "stg-vegas-tunnel")
    assert entry["rule_version"] == VEGAS_V2


def test_only_the_owner_stops_a_version_and_the_version_file_is_not_touched(tmp_path):
    root = _root(tmp_path)
    _add_vegas_v2(root)
    v1_file = root / "registry" / "versions" / "stg-vegas-tunnel-v1.yaml"
    before = v1_file.read_bytes()

    _edit_registry(root, lambda d: _strategy(d, "stg-vegas-tunnel").update(retired_rule_versions=[VEGAS_V1]))
    assert _vegas_versions(root) == [VEGAS_V2]
    _edit_registry(root, lambda d: _strategy(d, "stg-vegas-tunnel").update(retired_rule_versions=[VEGAS_V2]))
    assert _vegas_versions(root) == [VEGAS_V1]
    _edit_registry(root, lambda d: _strategy(d, "stg-vegas-tunnel").update(retired_rule_versions=[VEGAS_V1, VEGAS_V2]))
    assert all(s["strategy_id"] != "stg-vegas-tunnel" for s in active_strategies(root))
    assert v1_file.read_bytes() == before
    assert usable_rule_versions(root, _strategy(load_registry(root), "stg-vegas-tunnel")) == []


def test_owner_attestation_defaults_to_the_newest_version_and_may_name_an_older_usable_one(tmp_path):
    root = _root(tmp_path)
    _add_vegas_v2(root)
    assert resolve_strategy(root, _attestation())["rule_version"] == VEGAS_V2
    assert resolve_strategy(root, _attestation(rule_version=VEGAS_V1))["rule_version"] == VEGAS_V1
    with pytest.raises(ResolverError, match="usable version"):
        resolve_strategy(root, _attestation(rule_version="stg-vegas-tunnel:v9"))

    _edit_registry(root, lambda d: _strategy(d, "stg-vegas-tunnel").update(retired_rule_versions=[VEGAS_V1]))
    assert resolve_strategy(root, _attestation())["rule_version"] == VEGAS_V2
    with pytest.raises(ResolverError, match="usable version"):
        resolve_strategy(root, _attestation(rule_version=VEGAS_V1))


def test_versions_of_one_strategy_are_alternatives_not_competitors(tmp_path):
    root = _root(tmp_path)
    _add_vegas_v2(root, extra_rule=True)

    only_v1_rule = resolve_strategy(root, {"observations": [obs("vegas.tunnel.touch")], "plan": PLAN})
    assert only_v1_rule["status"] == "MATCHED" and only_v1_rule["rule_version"] == VEGAS_V1
    vegas_entries = [m for m in only_v1_rule["matrix"] if m["strategy_id"] == "stg-vegas-tunnel"]
    assert [(m["rule_version"], m["result"]) for m in vegas_entries] == [(VEGAS_V1, "MATCH"), (VEGAS_V2, "INSUFFICIENT")]

    both_rules = resolve_strategy(
        root, {"observations": [obs("vegas.tunnel.touch"), obs("vegas.tunnel.reclaim")], "plan": PLAN}
    )
    assert both_rules["status"] == "MATCHED" and both_rules["rule_version"] == VEGAS_V2  # newest match, not AMBIGUOUS

    newer_fails = resolve_strategy(
        root, {"observations": [obs("vegas.tunnel.touch"), obs("vegas.tunnel.reclaim", "FAIL")], "plan": PLAN}
    )
    assert newer_fails["status"] == "MATCHED" and newer_fails["rule_version"] == VEGAS_V1


def test_replay_evaluates_exactly_the_frozen_versions_even_if_stopped_since(tmp_path):
    root = _root(tmp_path)
    _add_vegas_v2(root, retired=(VEGAS_V1,))
    assert _vegas_versions(root) == [VEGAS_V2]

    replay = resolve_strategy(root, _attestation(), only_versions={VEGAS_V1})
    assert replay["rule_version"] == VEGAS_V1
    assert {m["rule_version"] for m in replay["matrix"]} == {VEGAS_V1}
    # Even a strategy that is no longer active can be replayed for a decision made while it was.
    _edit_registry(root, lambda d: _strategy(d, "stg-vegas-tunnel").update(status="retired"))
    assert {m["rule_version"] for m in resolve_strategy(root, _attestation(setup_status="NO_SETUP"), only_versions={VEGAS_V1})["matrix"]} == {VEGAS_V1}


@pytest.mark.parametrize(
    "retired,fragment",
    [
        ("stg-vegas-tunnel:v1", "list of version references"),
        ([1], "list of version references"),
        (["stg-vegas-tunnel:v7"], "only retire versions listed in rule_versions"),
    ],
)
def test_retired_rule_versions_must_be_a_list_of_listed_versions(retired, fragment):
    strategy = {
        "strategy_id": "stg-vegas-tunnel",
        "classification": "independent_system",
        "status": "active",
        "rule_versions": [VEGAS_V1],
        "retired_rule_versions": retired,
    }
    messages = [error.message for error in validate_strategy(strategy)]
    assert any(fragment in message for message in messages)


def test_a_registry_with_retired_rule_versions_validates(tmp_path):
    root = _root(tmp_path)
    _add_vegas_v2(root, retired=(VEGAS_V1,))
    assert validate_registry(load_registry(root), root) == []


@pytest.mark.parametrize(
    "listed,valid",
    [
        ([VEGAS_V1, VEGAS_V2], True),
        ([VEGAS_V2, VEGAS_V1], False),  # reordering would silently change the default version
        ([VEGAS_V1, VEGAS_V1], False),
        ([VEGAS_V1, VEGAS_V2, VEGAS_V2], False),
    ],
)
def test_rule_versions_are_listed_in_ascending_order_without_duplicates(tmp_path, listed, valid):
    root = _root(tmp_path)
    _add_vegas_v2(root)
    _edit_registry(root, lambda d: _strategy(d, "stg-vegas-tunnel").update(rule_versions=list(listed)))
    errors = validate_registry(yaml.safe_load((root / "registry" / "strategy-registry.yaml").read_text(encoding="utf-8")), root)
    assert (errors == []) is valid
    if not valid:
        assert any("ascending version order" in error["message"] for error in errors)
