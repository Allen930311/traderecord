"""The B1 v3 registry pin protects what B3 froze and lets the registry grow after it.

Allowed without touching the guard: new strategies, new rule versions appended to a strategy,
and `retired_rule_versions`. Not allowed: editing or deleting anything B3 froze.
"""
import json
import shutil
from pathlib import Path

import pytest
import yaml

from trading_os.episode_identity import B3_MANIFEST_PATH, _registry_preserves_b3_freeze
from trading_os.registry import (
    B3_BASELINE_RULE_VERSIONS,
    load_registry,
    registry_as_of_b3,
    registry_digest,
)

ROOT = Path(__file__).resolve().parents[1]
B3_MANIFEST = json.loads((ROOT / B3_MANIFEST_PATH).read_text(encoding="utf-8"))


def _registry_root(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "registry", tmp_path / "registry")
    return tmp_path


def _strategy(data: dict, strategy_id: str) -> dict:
    return next(item for item in data["strategies"] if item["strategy_id"] == strategy_id)


def _edit_registry(root: Path, edit) -> None:
    path = root / "registry" / "strategy-registry.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _remove(strategy_id: str):
    return lambda data: data["strategies"].remove(_strategy(data, strategy_id))


def _append_new_strategy(data: dict) -> None:
    data["strategies"].append(
        {
            "strategy_id": "stg-brand-new",
            "display_name": "Brand new",
            "classification": "independent_system",
            "status": "active",
            "legacy_aliases": [],
            "rule_versions": [],
        }
    )


def _write_vegas_v2(root: Path) -> None:
    source = root / "registry" / "versions" / "stg-vegas-tunnel-v1.yaml"
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    data["version_id"] = data["rule_version"] = "stg-vegas-tunnel:v2"
    data["supersedes"] = "stg-vegas-tunnel:v1"
    (root / "registry" / "versions" / "stg-vegas-tunnel-v2.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _append_vegas_v2(root: Path):
    _write_vegas_v2(root)
    return lambda data: _strategy(data, "stg-vegas-tunnel")["rule_versions"].append("stg-vegas-tunnel:v2")


def test_the_baseline_is_exactly_what_the_b3_manifest_froze():
    registry = load_registry(ROOT)
    assert set(B3_BASELINE_RULE_VERSIONS) == {item["strategy_id"] for item in registry["strategies"]} - {"stg-example-a"}
    assert registry_digest(registry_as_of_b3(registry)) == B3_MANIFEST["registry"]["after"]["registry_digest"]


def test_current_registry_preserves_the_b3_freeze(tmp_path):
    assert _registry_preserves_b3_freeze(_registry_root(tmp_path), B3_MANIFEST) is True


def test_registry_without_aa_still_preserves_the_b3_freeze(tmp_path):
    root = _registry_root(tmp_path)
    _edit_registry(root, _remove("stg-example-a"))
    assert _registry_preserves_b3_freeze(root, B3_MANIFEST) is True


def test_aa_itself_may_change_because_it_is_after_b3(tmp_path):
    root = _registry_root(tmp_path)
    _edit_registry(root, lambda data: _strategy(data, "stg-example-a").update(display_name="AA renamed"))
    assert _registry_preserves_b3_freeze(root, B3_MANIFEST) is True


def test_a_new_strategy_needs_no_change_to_the_guard(tmp_path):
    root = _registry_root(tmp_path)
    _edit_registry(root, _append_new_strategy)
    assert _registry_preserves_b3_freeze(root, B3_MANIFEST) is True


def test_a_frozen_strategy_may_gain_a_new_version(tmp_path):
    root = _registry_root(tmp_path)
    _edit_registry(root, _append_vegas_v2(root))
    assert _registry_preserves_b3_freeze(root, B3_MANIFEST) is True


def test_an_older_version_may_be_stopped_with_retired_rule_versions(tmp_path):
    root = _registry_root(tmp_path)
    _edit_registry(root, _append_vegas_v2(root))
    _edit_registry(root, lambda data: _strategy(data, "stg-vegas-tunnel").update(retired_rule_versions=["stg-vegas-tunnel:v1"]))
    assert _registry_preserves_b3_freeze(root, B3_MANIFEST) is True


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda d: _strategy(d, "stg-ict-liquidity")["legacy_aliases"].append("extra-alias"), id="frozen-alias-added"),
        pytest.param(lambda d: _strategy(d, "stg-ict-liquidity")["legacy_aliases"].remove("sr"), id="frozen-alias-removed"),
        pytest.param(lambda d: _strategy(d, "stg-ict-liquidity")["variants"].append("new-variant"), id="frozen-variant-added"),
        pytest.param(lambda d: _strategy(d, "stg-vegas-tunnel").update(display_name="Renamed"), id="frozen-display-name-changed"),
        pytest.param(lambda d: _strategy(d, "stg-po3")["legacy_aliases"].append("po3-extra"), id="frozen-po3-alias-added"),
        pytest.param(lambda d: _strategy(d, "stg-example-b").update(status="active"), id="retired-strategy-reactivated"),
        pytest.param(lambda d: _strategy(d, "stg-17ema").update(legacy_aliases=["ema-new"]), id="experimental-aliases-changed"),
        pytest.param(_remove("stg-example-b"), id="retired-strategy-deleted"),
        pytest.param(_remove("stg-vegas-tunnel"), id="frozen-strategy-deleted"),
        pytest.param(lambda d: _strategy(d, "stg-vegas-tunnel").update(rule_versions=[]), id="frozen-version-removed-from-list"),
    ],
)
def test_editing_or_deleting_what_b3_froze_is_rejected(tmp_path, edit):
    root = _registry_root(tmp_path)
    _edit_registry(root, edit)
    assert _registry_preserves_b3_freeze(root, B3_MANIFEST) is False


def test_a_changed_frozen_rule_version_file_is_rejected(tmp_path):
    root = _registry_root(tmp_path)
    version = root / "registry" / "versions" / "stg-po3-v1.yaml"
    version.write_text(version.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    assert _registry_preserves_b3_freeze(root, B3_MANIFEST) is False
