from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from trading_os.strategy_discovery import (
    Step0Error,
    _calculate_deleted_source_resurrection,
    _input_equals_output,
    build_strategy_lookup,
    classify_strategy_label,
    run_step0,
    validate_step0_artifacts,
)


def _registry() -> dict:
    return {
        "registry_version": "strategy-registry.v1",
        "strategies": [
            {
                "strategy_id": "stg-active",
                "classification": "independent_system",
                "status": "active",
                "legacy_aliases": ["active-label"],
                "rule_versions": [],
            },
            {
                "strategy_id": "stg-retired",
                "classification": "independent_system",
                "status": "retired",
                "legacy_aliases": ["retired-label"],
                "rule_versions": [],
            },
            {
                "strategy_id": "stg-experimental",
                "classification": "experimental",
                "status": "experimental",
                "legacy_aliases": ["experiment-label"],
                "rule_versions": [],
            },
            {
                "strategy_id": "stg-proposed",
                "classification": "independent_system",
                "status": "proposed",
                "legacy_aliases": ["proposed-label"],
                "rule_versions": [],
            },
        ],
    }


def _write_trade(root: Path, name: str, label: str | None) -> Path:
    path = root / "trades" / "2026" / "09" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    strategy_line = f"strategy: {label}\n" if label is not None else ""
    path.write_text(f"---\n{strategy_line}pair: BTCUSDT\n---\n\n# record\n", encoding="utf-8")
    return path


def _project(tmp_path: Path, labels: list[str | None]) -> tuple[Path, list[str]]:
    root = tmp_path / "project"
    (root / "registry").mkdir(parents=True)
    (root / "canonical" / "adjudications").mkdir(parents=True)
    (root / "registry" / "strategy-registry.yaml").write_text(
        yaml.safe_dump(_registry(), sort_keys=False), encoding="utf-8"
    )
    (root / "canonical" / "adjudications" / "historical-cleaning-human-revisions.jsonl").write_text(
        "", encoding="utf-8"
    )
    (root / "canonical" / "adjudications" / "phase4d-human-revisions.jsonl").write_text(
        "", encoding="utf-8"
    )
    paths = []
    for index, label in enumerate(labels, 1):
        path = _write_trade(root, f"2026-09-{index:02d}_BTCUSDT_L_{index}.md", label)
        paths.append(path.relative_to(root).as_posix())
    return root, paths


def test_lookup_is_exact_and_variant_status_does_not_fall_back_to_parent():
    registry = {
        "strategies": [
            {
                "strategy_id": "stg-parent",
                "classification": "independent_system",
                "status": "active",
                "rule_versions": [],
            },
            {
                "strategy_id": "stg-child",
                "classification": "variant",
                "parent_strategy_id": "stg-parent",
                "status": "retired",
                "legacy_aliases": ["child"],
                "rule_versions": [],
            },
        ]
    }
    lookup = build_strategy_lookup(registry)
    match = classify_strategy_label("child", lookup)
    assert match["matched"] is True
    assert match["strategy"]["strategy_id"] == "stg-child"
    assert match["strategy"]["status"] == "retired"
    assert classify_strategy_label("Child", lookup)["matched"] is False
    assert classify_strategy_label(" child ", lookup)["matched"] is False


def test_registry_collision_fails_closed():
    registry = {
        "strategies": [
            {"strategy_id": "stg-a", "status": "active", "legacy_aliases": ["same"]},
            {"strategy_id": "stg-b", "status": "retired", "legacy_aliases": ["same"]},
        ]
    }
    with pytest.raises(Step0Error, match="collision"):
        build_strategy_lookup(registry)


def test_missing_registry_shape_fails_closed():
    with pytest.raises(Step0Error, match="strategies must be a list"):
        build_strategy_lookup({"strategies": None})


def test_invalid_registry_status_fails_closed(tmp_path: Path):
    root, paths = _project(tmp_path, ["active-label"])
    invalid = _registry()
    invalid["strategies"][0]["status"] = "not-a-registry-status"
    (root / "registry" / "strategy-registry.yaml").write_text(
        yaml.safe_dump(invalid, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(Step0Error, match="invalid strategy registry"):
        run_step0(
            root,
            baseline_sha="test-baseline",
            historical_reference_commit="test-history",
            output_dir=tmp_path / "out",
            historical_reference_paths=paths,
            identity_log_paths=(),
            per_trade_decision_log_paths=(),
            verify_git=False,
        )


def test_step0_routes_exact_statuses_without_human_fields(tmp_path: Path):
    root, paths = _project(
        tmp_path,
        ["active-label", "retired-label", "experiment-label", "proposed-label", "unmapped"],
    )
    output = tmp_path / "out"
    manifest = run_step0(
        root,
        baseline_sha="test-baseline",
        historical_reference_commit="test-history",
        output_dir=output,
        historical_reference_paths=paths,
        identity_log_paths=(),
        per_trade_decision_log_paths=(),
        verify_git=False,
    )
    assert manifest["corpus"]["input_count"] == 5
    assert manifest["corpus"]["output_count"] == 5
    assert manifest["routes"]["gate_outcome"] == {
        "ACTIVE": 1,
        "RETIRED": 1,
        "EXPERIMENTAL": 1,
        "BLOCKED_STRATEGY_STATUS": 1,
        "BLOCKED_STRATEGY_MAPPING": 1,
    }
    rows = [json.loads(line) for line in (output / "step-0-results.jsonl").read_text(encoding="utf-8").splitlines()]
    by_label = {row["legacy_strategy_label"]: row for row in rows}
    assert by_label["active-label"]["step0_gate_outcome"] == "ACTIVE"
    assert by_label["active-label"]["review_strategy_status"] == "active"
    assert not any(key.startswith("review_") and key not in {"review_strategy_status", "review_eligibility"} for key in by_label["active-label"])
    assert by_label["retired-label"]["review_logic_status"] == "excluded"
    assert by_label["retired-label"]["review_record_class"] == "retired_strategy_trade"
    assert by_label["retired-label"]["review_recommend_disposition"] == "keep-and-mark"
    assert by_label["experiment-label"]["review_record_class"] == "experiment"
    assert by_label["proposed-label"]["step0_gate_outcome"] == "BLOCKED_STRATEGY_STATUS"
    assert by_label["unmapped"]["step0_gate_outcome"] == "BLOCKED_STRATEGY_MAPPING"
    for row in rows:
        assert "canonical_trade_id" not in row
        assert "canonical_lifecycle_id" not in row
        assert "provider_group_id" not in row
    validate_step0_artifacts(root, output, expected_baseline_sha="test-baseline", per_trade_decision_log_paths=())


def test_dropping_one_output_breaks_independent_input_output_audit(tmp_path: Path):
    root, paths = _project(tmp_path, ["active-label", "retired-label"])
    output = tmp_path / "out"
    run_step0(
        root,
        baseline_sha="test-baseline",
        historical_reference_commit="test-history",
        output_dir=output,
        historical_reference_paths=paths,
        identity_log_paths=(),
        per_trade_decision_log_paths=(),
        verify_git=False,
    )
    results_path = output / "step-0-results.jsonl"
    lines = results_path.read_text(encoding="utf-8").splitlines()
    results_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    assert _input_equals_output(len(paths), len(lines) - 1) is False
    with pytest.raises(Step0Error, match="input/output"):
        validate_step0_artifacts(root, output, expected_baseline_sha="test-baseline", per_trade_decision_log_paths=())


def test_adding_one_output_breaks_independent_input_output_audit(tmp_path: Path):
    root, paths = _project(tmp_path, ["active-label"])
    output = tmp_path / "out"
    run_step0(
        root,
        baseline_sha="test-baseline",
        historical_reference_commit="test-history",
        output_dir=output,
        historical_reference_paths=paths,
        identity_log_paths=(),
        per_trade_decision_log_paths=(),
        verify_git=False,
    )
    results_path = output / "step-0-results.jsonl"
    lines = results_path.read_text(encoding="utf-8").splitlines()
    results_path.write_text("\n".join(lines + [lines[0]]) + "\n", encoding="utf-8")
    assert _input_equals_output(len(paths), len(lines) + 1) is False
    with pytest.raises(Step0Error, match="input/output"):
        validate_step0_artifacts(root, output, expected_baseline_sha="test-baseline", per_trade_decision_log_paths=())


def test_deleted_source_resurrection_uses_historical_deleted_intersection():
    deleted = "trades/2025/09/2025-09-08_BTCUSDT_L_fib-rsi.md"
    retained = "trades/2025/09/2025-09-09_BTCUSDT_L_fib-rsi.md"
    historically_deleted, resurrected = _calculate_deleted_source_resurrection(
        [deleted, retained],
        [retained],
        [deleted, retained],
    )
    assert historically_deleted == [deleted]
    assert resurrected == [deleted]
    assert len(resurrected) == 1
    assert bool(resurrected) is True


def test_deleted_source_resurrection_reports_false_when_deleted_path_is_absent():
    deleted = "trades/2025/09/2025-09-08_BTCUSDT_L_fib-rsi.md"
    retained = "trades/2025/09/2025-09-09_BTCUSDT_L_fib-rsi.md"
    fresh_addition = "trades/2026/04/2026-04-13_BTCUSDT_L_8-triple-tap.md"
    historically_deleted, resurrected = _calculate_deleted_source_resurrection(
        [deleted, retained],
        [retained],
        [retained, fresh_addition],
    )
    assert historically_deleted == [deleted]
    assert resurrected == []
    assert len(resurrected) == 0
    assert bool(resurrected) is False


def test_committed_full_run_has_255_independent_input_and_output_records():
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / "exports" / "strategy-discovery" / "step-0" / "corpus-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    from scripts.canonical_migration.full_history import discover_sources

    selected_count = sum(
        entry.get("discovery_status") == "eligible"
        for entry in discover_sources(root, "trades").get("entries", [])
    )
    assert selected_count == 255
    assert manifest["corpus"]["input_count"] == 255
    assert manifest["corpus"]["output_count"] == 255
    assert manifest["checks"]["input_equals_output"] is True
    assert manifest["corpus"]["deleted_source_resurrection_count"] == 0
    assert manifest["checks"]["deleted_source_resurrected"] is False


def test_duplicate_input_is_rejected_before_output(tmp_path: Path):
    root, paths = _project(tmp_path, ["active-label"])
    duplicate_paths = paths + paths
    with pytest.raises(Step0Error, match="duplicate historical reference paths"):
        run_step0(
            root,
            baseline_sha="test-baseline",
            historical_reference_commit="test-history",
            output_dir=tmp_path / "out",
            historical_reference_paths=duplicate_paths,
            identity_log_paths=(),
            per_trade_decision_log_paths=(),
            verify_git=False,
        )


def test_historical_set_difference_is_explicit(tmp_path: Path):
    root, paths = _project(tmp_path, ["active-label"])
    manifest = run_step0(
        root,
        baseline_sha="test-baseline",
        historical_reference_commit="test-history",
        output_dir=tmp_path / "out",
        historical_reference_paths=paths + ["trades/2025/09/deleted.md"],
        identity_log_paths=(),
        per_trade_decision_log_paths=(),
        verify_git=False,
    )
    assert manifest["corpus"]["historical_only_paths"] == ["trades/2025/09/deleted.md"]
    assert manifest["corpus"]["fresh_only_paths"] == []


def test_real_corpus_is_read_only_and_preserves_p10_and_batch2_evidence(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    protected = [
        root / "trades" / "2025" / "09" / "2025-09-09_BTCUSDT_L_fib-rsi.md",
        root / "canonical" / "adjudications" / "historical-cleaning-human-revisions.jsonl",
        root / "ingestion" / "pilot" / "batch-2" / "03-2025-09-08-BTC.md",
    ]
    from trading_os.legacy_source import read_legacy_bytes

    def current(path: Path) -> str:
        return hashlib.sha256(read_legacy_bytes(root, path.relative_to(root))).hexdigest()

    before = {path: current(path) for path in protected}
    from scripts.canonical_migration.full_history import discover_sources

    paths = [
        entry["path"]
        for entry in discover_sources(root)["entries"]
        if entry["discovery_status"] == "eligible"
    ]
    output = tmp_path / "real-step0"
    manifest = run_step0(
        root,
        baseline_sha="test-baseline",
        historical_reference_commit="test-history",
        output_dir=output,
        historical_reference_paths=paths,
        identity_log_paths=(
            "canonical/adjudications/historical-cleaning-human-revisions.jsonl",
            "canonical/adjudications/phase4d-human-revisions.jsonl",
        ),
        verify_git=False,
    )
    rows = [json.loads(line) for line in (output / "step-0-results.jsonl").read_text(encoding="utf-8").splitlines()]
    by_path = {row["source_path"]: row for row in rows}
    assert "trades/2025/09/2025-09-08_BTCUSDT_L_fib-rsi.md" not in by_path
    assert by_path["trades/2025/09/2025-09-09_BTCUSDT_L_fib-rsi.md"]["identity_status"] == "resolved"
    assert manifest["corpus"]["input_count"] == len(paths)
    assert {path: current(path) for path in protected} == before
