import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from trading_os.episode_identity import (
    B1_V2_DATASET_PATH,
    B1_V2_MANIFEST_PATH,
    B1_V2_README_PATH,
    B3_MANIFEST_PATH,
    DEFAULT_B1_V3_OUTPUT,
    DEFAULT_ADJUDICATION_OUTPUT,
    EPISODE_CANDIDATE_NAME,
    EPISODE_RESOLUTION_NAME,
    EpisodeIdentityError,
    discover_episode_candidates,
    run_b1_v3,
    validate_b1_v3_manifest,
)
from scripts.canonical_migration.frontmatter import parse_markdown


ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA = "da96d6cbff11d448ee8ff797cbf7a738f8a13204"
GENERATED_AT = "2026-09-11T00:00:00Z"
V3_DIR = ROOT / DEFAULT_B1_V3_OUTPUT
ADJ_DIR = ROOT / DEFAULT_ADJUDICATION_OUTPUT


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _known_row(rows, suffix: str):
    return next(row for row in rows if row["source_path"].endswith(suffix))


def test_official_b1_v3_manifest_and_artifact_hashes_are_valid():
    manifest = _read_json(V3_DIR / "manifest.json")
    assert validate_b1_v3_manifest(
        manifest,
        project_root=ROOT,
        output_dir=V3_DIR,
        adjudication_dir=ADJ_DIR,
    ) == []
    assert manifest["counts"]["source_rows"] == 255
    assert manifest["counts"]["execution_collision_groups"] == 30
    assert manifest["counts"]["exact_duplicate_groups"] == 8
    assert manifest["counts"]["same_execution_resolved_groups"] == 23
    assert manifest["counts"]["unresolved_episode_groups"] == 7
    assert manifest["counts"]["unresolved_source_rows"] == 17
    assert manifest["counts"]["unresolved_excluded_rows"] == 17
    assert manifest["counts"]["total_episode_groups"] == 216
    assert manifest["counts"]["resolved_episode_count"] == 209
    assert manifest["counts"]["performance_countable_episode_count"] == 181


def test_v2_artifacts_remain_byte_identical_after_v3_generation(tmp_path):
    protected = [ROOT / B1_V2_DATASET_PATH, ROOT / B1_V2_MANIFEST_PATH, ROOT / B1_V2_README_PATH]
    before = {_path: _sha256(_path) for _path in protected}
    run_b1_v3(
        ROOT,
        baseline_sha=BASELINE_SHA,
        generated_at=GENERATED_AT,
        output_dir=tmp_path / "b1-v3",
        adjudication_dir=tmp_path / "adjudications",
    )
    assert {_path: _sha256(_path) for _path in protected} == before


def test_v3_regeneration_is_deterministic_at_the_same_output_paths(tmp_path):
    output_dir = tmp_path / "b1-v3"
    adjudication_dir = tmp_path / "adjudications"
    first = run_b1_v3(
        ROOT,
        baseline_sha=BASELINE_SHA,
        generated_at=GENERATED_AT,
        output_dir=output_dir,
        adjudication_dir=adjudication_dir,
    )
    first_bytes = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    second = run_b1_v3(
        ROOT,
        baseline_sha=BASELINE_SHA,
        generated_at=GENERATED_AT,
        output_dir=output_dir,
        adjudication_dir=adjudication_dir,
    )
    second_bytes = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert first == second
    assert first_bytes == second_bytes


def test_exact_duplicate_is_counted_once_and_preserves_both_source_rows():
    rows = _read_jsonl(V3_DIR / "canonical-trade-dataset.jsonl")
    left = _known_row(rows, "2024-01-17_BTCUSDT_S_1.md")
    right = _known_row(rows, "2024-01-17_BTCUSDT_S_1_1.md")
    assert left["source_sha256"] == right["source_sha256"]
    assert left["canonical_episode_id"] == right["canonical_episode_id"]
    assert left["episode_role"] == right["episode_role"] == "duplicate_representation"
    episode_rows = [row for row in rows if row["canonical_episode_id"] == left["canonical_episode_id"]]
    assert sum(row["performance_counting_eligibility"] for row in episode_rows) == 1
    candidate_rows = _read_jsonl(ADJ_DIR / EPISODE_CANDIDATE_NAME)
    assert any(
        candidate["candidate_class"] == "exact_duplicate"
        and set(candidate["member_source_paths"]) == {left["source_path"], right["source_path"]}
        for candidate in candidate_rows
    )


def test_different_representation_resolves_by_evidence_and_chooses_richer_row():
    rows = _read_jsonl(V3_DIR / "canonical-trade-dataset.jsonl")
    left = _known_row(rows, "2023-10-21_BTCUSDT_L_2.md")
    right = _known_row(rows, "2023-10-21_BTCUSDT_L_mss-vegas.md")
    assert left["source_sha256"] != right["source_sha256"]
    assert left["canonical_episode_id"] == right["canonical_episode_id"]
    assert left["episode_resolution"] == right["episode_resolution"] == "same_execution"
    assert right["canonical_representative_trade_ref"] == right["trade_ref"]
    assert left["canonical_representative_trade_ref"] == right["trade_ref"]
    assert sum(row["performance_counting_eligibility"] for row in (left, right)) == 1
    assert any(
        reason.startswith("complementary sparse/full template shape")
        for reason in right["episode_evidence"]["representation_relationship"]["reasons"]
    )


def test_unresolved_groups_fail_closed_without_a_representative():
    rows = _read_jsonl(ROOT / B1_V2_DATASET_PATH)
    _, groups, counts = discover_episode_candidates(ROOT, rows)
    unresolved = [group for group in groups if group["relationship"] == "unresolved"]
    assert len(unresolved) == counts["unresolved_episode_groups"] == 7
    assert all(group["canonical_representative"] is None for group in unresolved)
    assert all(not group["performance_counting_eligibility"] for group in unresolved)
    v3_rows = {row["trade_ref"]: row for row in _read_jsonl(V3_DIR / "canonical-trade-dataset.jsonl")}
    assert all(
        v3_rows[trade_ref]["performance_exclusion_reason"] == "unresolved_episode_grouping"
        for group in unresolved
        for trade_ref in group["member_trade_refs"]
    )


def test_similar_coarse_trades_with_different_plans_stay_distinct(tmp_path):
    first_path = tmp_path / "trades/2024/01/2024-01-01_BTCUSDT_L_alpha.md"
    second_path = tmp_path / "trades/2024/01/2024-01-01_BTCUSDT_L_beta.md"
    first_path.parent.mkdir(parents=True)
    first_text = "---\npair: BTCUSDT\ndirection: long\nentry_price: 100\nsl_price: 99\ntp_price: 102\n---\nfirst\n"
    second_text = "---\npair: BTCUSDT\ndirection: long\nentry_price: 101\nsl_price: 100\ntp_price: 103\n---\nsecond\n"
    first_path.write_text(first_text, encoding="utf-8", newline="\n")
    second_path.write_text(second_text, encoding="utf-8", newline="\n")

    def row(path: Path, text: str, suffix: str):
        return {
            "trade_ref": str(path.relative_to(tmp_path)).replace("\\", "/"),
            "step0_record_id": f"rec-{suffix}",
            "source_path": str(path.relative_to(tmp_path)).replace("\\", "/"),
            "source_sha256": _sha256(path),
            "source_body_sha256": hashlib.sha256(parse_markdown(text).body.encode("utf-8")).hexdigest(),
            "event_date": "2024-01-01",
            "instrument": "BTCUSDT",
            "direction": "long",
            "entry_price": 100 if suffix == "a" else 101,
            "stop_loss_price": 99 if suffix == "a" else 100,
            "take_profit_price": 102 if suffix == "a" else 103,
            "exit_price": 102 if suffix == "a" else 103,
            "canonical_realized_r": 2.0,
            "outcome": "win",
            "sample_origin": "live",
            "analysis_eligibility": "eligible",
            "ineligibility_reason": None,
            "strategy_id": "stg-test",
            "strategy_status": "active",
            "family_strategy_id": "stg-test",
            "parent_strategy_id": None,
            "legacy_strategy_label": "alpha" if suffix == "a" else "beta",
            "frontmatter_strategy_label": "alpha" if suffix == "a" else "beta",
            "time_entry": "10:00" if suffix == "a" else "11:00",
            "related_record_refs": [],
            "episode_id": f"v2-{suffix}",
            "episode_note": None,
            "provenance": {"source": "fixture"},
            "match_type": "fixture",
            "decision_id": f"decision-{suffix}",
        }

    candidates, groups, _ = discover_episode_candidates(
        tmp_path,
        [row(first_path, first_text, "a"), row(second_path, second_text, "b")],
    )
    assert len(groups) == 2
    assert len({group["episode_id"] for group in groups}) == 2
    assert any(candidate["candidate_class"] == "possible_related" for candidate in candidates)


def test_every_counted_row_is_the_only_counted_member_of_its_episode():
    rows = _read_jsonl(V3_DIR / "canonical-trade-dataset.jsonl")
    counts = {}
    for row in rows:
        if row["performance_counting_eligibility"]:
            counts[row["canonical_episode_id"]] = counts.get(row["canonical_episode_id"], 0) + 1
    assert counts
    assert set(counts.values()) == {1}


def test_v2_compatibility_fields_and_source_identity_are_unchanged():
    v2_rows = {row["trade_ref"]: row for row in _read_jsonl(ROOT / B1_V2_DATASET_PATH)}
    v3_rows = {row["trade_ref"]: row for row in _read_jsonl(V3_DIR / "canonical-trade-dataset.jsonl")}
    assert set(v3_rows) == set(v2_rows)
    for trade_ref, v2 in v2_rows.items():
        v3 = v3_rows[trade_ref]
        for field in ("strategy_id", "strategy_status", "family_strategy_id", "parent_strategy_id", "canonical_realized_r", "outcome", "event_date", "instrument", "direction", "sample_origin"):
            assert v3[field] == v2[field], trade_ref
        for field in ("source_path", "trade_ref", "step0_record_id", "source_sha256", "source_body_sha256"):
            assert v3[field] == v2[field], trade_ref


def test_identity_module_has_no_forbidden_downstream_input_markers():
    source = (ROOT / "trading_os/episode_identity.py").read_text(encoding="utf-8")
    forbidden = ("strategy-performance", "PERFORMANCE.md", "TRADE_JOURNAL.md", "win_rate", "expectancy", "profit_factor", "drawdown", "sharpe")
    assert all(marker not in source for marker in forbidden)
    assert "exports/method-validation/b2" not in source.lower()


def test_protected_inputs_remain_unchanged_after_full_generation(tmp_path):
    protected_relative = [
        B1_V2_DATASET_PATH,
        B1_V2_MANIFEST_PATH,
        B1_V2_README_PATH,
        B3_MANIFEST_PATH,
        "exports/strategy-discovery/step-0/step-0-results.jsonl",
        "exports/strategy-discovery/step-0/corpus-manifest.json",
        "registry/strategy-registry.yaml",
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-per-trade-decisions-20260910.jsonl",
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-per-trade-application-20260910.jsonl",
        "canonical/adjudications/strategy-mapping-review/strategy-mapping-residual-lane-handoff-20260910.jsonl",
    ]
    before = {path: _sha256(ROOT / path) for path in protected_relative}
    run_b1_v3(
        ROOT,
        baseline_sha=BASELINE_SHA,
        generated_at=GENERATED_AT,
        output_dir=tmp_path / "b1-v3",
        adjudication_dir=tmp_path / "adjudications",
    )
    assert {path: _sha256(ROOT / path) for path in protected_relative} == before


def test_b1_v3_cli_emits_machine_readable_summary(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "trading_os",
            "b1-v3",
            "--baseline-sha",
            BASELINE_SHA,
            "--generated-at",
            GENERATED_AT,
            "--output-dir",
            str(tmp_path / "b1-v3"),
            "--adjudication-dir",
            str(tmp_path / "adjudications"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["schema_version"] == "method-validation-b1.v3"
    assert summary["source_rows"] == 255
    assert summary["performance_countable_episode_count"] == 181


def test_invalid_baseline_is_rejected_before_writing():
    with pytest.raises(EpisodeIdentityError):
        run_b1_v3(ROOT, baseline_sha="not-a-sha", generated_at=GENERATED_AT)
