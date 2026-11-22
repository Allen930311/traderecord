from __future__ import annotations

from legacy_test_support import legacy_bytes, materialize_legacy
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from trading_os.method_validation import (
    _R_CONFLICT_ABS_TOLERANCE,
    _R_CONFLICT_REL_TOLERANCE,
    ANALYSIS_ELIGIBILITY,
    DATA_QUALITY_FLAGS,
    FORBIDDEN_SOURCE_BASENAMES,
    INELIGIBILITY_REASONS,
    OUTCOME_ENUM,
    REALIZED_R_SOURCES,
    SAMPLE_ORIGIN_SOURCES,
    SAMPLE_ORIGINS,
    _canonical_bytes,
    B1Error,
    build_dataset,
    _frontmatter,
    _load_verified_source,
    _number,
    _sha256_bytes,
    run_b1_dataset,
)
from trading_os import __main__ as trading_os_cli

# The 5 fully-filled real trades that carry a single stray {{...}} prose token
# and must NOT be flagged placeholder_body (v2 fix).
PLACEHOLDER_FALSE_POSITIVES = {
    "trades/2026/07/2026-07-14-BTC.md",
    "trades/2026/07/2026-07-14-BTC-2.md",
    "trades/2026/07/2026-07-15-BTC.md",
    "trades/2026/07/2026-07-16-BTC.md",
    "trades/2026/07/2026-07-17-BTC.md",
}

_BLANK_R_TOKENS = {"", "null", "none", "n/a", "na", "-", "tbd", "pending"}

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = REPO_ROOT / "exports" / "method-validation" / "b1"
DATASET_PATH = DATASET_DIR / "canonical-trade-dataset.jsonl"
MANIFEST_PATH = DATASET_DIR / "manifest.json"

_GEN_ARGS = dict(
    dataset_version="method-validation-b1.v2",
    generated_at="2026-09-10T00:00:00Z",
    source_commit="0e78b63ba85657831b69d67e2fab033ccfc1c203",
)

# The 13 permanently-blocked records (8 keep-blocked + 4 historical-cleaning +
# 1 position-management), independently listed from the merged owner artifacts.
KEEP_BLOCKED = {
    "trades/2025/01/2025-01-23_BTCUSDT_S_unknown.md",
    "trades/2025/01/2025-01-23_BTCUSDT_S_unspecified.md",
    "trades/2025/01/2025-01-29_BTCUSDT_L_unknown.md",
    "trades/2025/01/2025-01-29_BTCUSDT_L_unspecified.md",
    "trades/2025/10/2025-10-21_ETHUSDT_S_unspecified.md",
    "trades/2026/02/2026-02-23_SOLUSDT_S_unspecified.md",
    "trades/2026/02/2026-02-26_BTCUSDT_S_unspecified.md",
    "trades/2026/04/2026-04-17_SOLUSDT_L_tda.md",
}
LANE_HANDOFF = {
    "trades/2026/03/2026-03-30_BTCUSDT_S_audit_large.md",
    "trades/2026/03/2026-03-31_BTCUSDT_S_audit_tail.md",
    "trades/2026/04/2026-04-20_BTCUSDT_S_chase-short.md",
    "trades/2026/04/2026-04-23_BNBUSDT_L_heavy-position.md",
    "trades/2026/04/2026-04-26_BTCUSDT_S_revenge.md",
}


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def step0_refs() -> set[str]:
    path = REPO_ROOT / "exports" / "strategy-discovery" / "step-0" / "step-0-results.jsonl"
    return {
        json.loads(line)["trade_ref"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def test_committed_dataset_has_exactly_255_rows(rows):
    assert len(rows) == 255
    assert len({row["trade_ref"] for row in rows}) == 255


def test_every_row_traces_to_a_step0_record(rows, step0_refs):
    assert len(step0_refs) == 255
    for row in rows:
        assert row["trade_ref"] in step0_refs
        assert row["source_path"] == row["trade_ref"]
        assert isinstance(row["step0_record_id"], str) and row["step0_record_id"]
        assert row["source_sha256"] and row["source_body_sha256"]


def test_closed_vocabularies_are_respected(rows):
    for row in rows:
        assert row["outcome"] in OUTCOME_ENUM
        assert row["sample_origin"] in SAMPLE_ORIGINS
        assert row["analysis_eligibility"] in ANALYSIS_ELIGIBILITY
        assert row["realized_r_source"] in REALIZED_R_SOURCES
        assert set(row["ineligibility_reasons"]) <= set(INELIGIBILITY_REASONS)
        assert (row["ineligibility_reason"] is None) or (row["ineligibility_reason"] in INELIGIBILITY_REASONS)
        assert set(row["data_quality_flags"]) <= set(DATA_QUALITY_FLAGS)


def test_eligibility_and_reason_are_consistent(rows):
    for row in rows:
        if row["analysis_eligibility"] == "eligible":
            assert row["ineligibility_reason"] is None
            assert row["ineligibility_reasons"] == []
        else:
            assert row["ineligibility_reason"] is not None
            assert row["ineligibility_reason"] in row["ineligibility_reasons"]
            assert row["ineligibility_reason"] == row["ineligibility_reasons"][0]


def test_the_13_permanently_blocked_are_ineligible_with_null_strategy(rows):
    blocked = KEEP_BLOCKED | LANE_HANDOFF
    assert len(blocked) == 13
    by_ref = {row["trade_ref"]: row for row in rows}
    for ref in blocked:
        row = by_ref[ref]
        assert row["strategy_id"] is None
        assert row["parent_strategy_id"] is None
        assert row["family_strategy_id"] is None
        assert row["strategy_status"] is None
        assert row["analysis_eligibility"] == "ineligible"
        assert row["step0_gate_outcome"] == "BLOCKED_STRATEGY_MAPPING"
        assert row["ineligibility_reason"].startswith("blocked_")
    for ref in KEEP_BLOCKED:
        assert by_ref[ref]["ineligibility_reason"] == "blocked_keep_blocked"
    assert by_ref["trades/2026/03/2026-03-31_BTCUSDT_S_audit_tail.md"]["ineligibility_reason"] == "blocked_position_management_handoff"
    for ref in LANE_HANDOFF - {"trades/2026/03/2026-03-31_BTCUSDT_S_audit_tail.md"}:
        assert by_ref[ref]["ineligibility_reason"] == "blocked_historical_cleaning_handoff"


def test_no_non_blocked_row_has_null_strategy(rows):
    for row in rows:
        if row["step0_gate_outcome"] != "BLOCKED_STRATEGY_MAPPING":
            assert row["strategy_id"] is not None


def test_recorded_realized_r_is_never_overwritten(rows):
    """The recorded_realized_r field must equal the frontmatter r_realized verbatim."""

    for row in rows:
        text = legacy_bytes(row["source_path"]).decode("utf-8", errors="replace")
        fm_value = _frontmatter(text).get("r_realized")
        assert row["recorded_realized_r_raw"] == fm_value
        if fm_value is None or fm_value.strip().lower() in _BLANK_R_TOKENS:
            assert row["recorded_realized_r"] is None
            continue
        expected = _number(fm_value)
        # recorded_realized_r is a faithful parse of frontmatter, never a recompute
        if expected is None:
            assert row["recorded_realized_r"] is None
            continue
        assert row["recorded_realized_r"] == pytest.approx(expected)
        # a real recorded non-zero value IS the canonical value, verbatim
        if expected != 0:
            assert row["canonical_realized_r"] == pytest.approx(expected)
            assert row["realized_r_source"] == "recorded"


def test_frontmatter_is_bom_safe_inline_comment_safe_and_fail_closed():
    parsed = _frontmatter(
        "\ufeff---\n"
        "direction: L  # L=多/Long, S=空/Short\n"
        "pair: BTC#USDT\n"
        "---\n"
    )
    assert parsed["direction"] == "L"
    assert parsed["pair"] == "BTC#USDT"
    assert _frontmatter("---\ndirection: S  # comment\n---\n")["direction"] == "S"
    assert _frontmatter("direction: L  # comment") == {}
    assert _frontmatter("\ufeff\ufeff---\ndirection: L\n---\n") == {}
    assert _frontmatter("\ufeff---\ndirection: L\n") == {}


def test_regenerated_bom_and_inline_comment_rows_have_canonical_facts(tmp_path):
    out = tmp_path / "b1"
    run_b1_dataset(REPO_ROOT, output_dir=out, **_GEN_ARGS)
    generated = {
        json.loads(line)["trade_ref"]: json.loads(line)
        for line in (out / "canonical-trade-dataset.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }

    eth_long = generated["trades/2026/03/2026-03-27_ETHUSDT_L_11.md"]
    assert "frontmatter_absent" not in eth_long["data_quality_flags"]
    assert eth_long["instrument"] == "ETHUSDT"
    assert eth_long["direction"] == "long"
    assert eth_long["entry_price"] == pytest.approx(2058.48)
    assert eth_long["stop_loss_price"] == pytest.approx(2031.20)
    assert eth_long["outcome"] == "loss"
    assert eth_long["recorded_realized_r"] == pytest.approx(-1)
    assert eth_long["canonical_realized_r"] == pytest.approx(-1)
    assert eth_long["realized_r_source"] == "recorded"
    assert eth_long["analysis_eligibility"] == "eligible"

    btc_long = generated["trades/2026/03/2026-03-27_BTCUSDT_L_11_2.md"]
    assert btc_long["direction"] == "long"

    po3 = generated["trades/2026/03/2026-03-30-BTCUSDT-S-PO3.md"]
    assert "frontmatter_absent" not in po3["data_quality_flags"]
    assert po3["direction"] == "short"
    assert po3["outcome"] == "breakeven"
    assert po3["recorded_realized_r"] == pytest.approx(0)
    assert po3["canonical_realized_r"] == pytest.approx(0)
    assert po3["realized_r_source"] == "recorded"

    ob_mss = generated["trades/2026/04/2026-04-01-ETH.md"]
    assert "frontmatter_absent" not in ob_mss["data_quality_flags"]
    assert ob_mss["instrument"] == "ETHUSDT"
    assert ob_mss["direction"] == "short"
    assert ob_mss["outcome"] == "loss"
    assert ob_mss["recorded_realized_r"] == pytest.approx(-1.1)
    assert ob_mss["canonical_realized_r"] == pytest.approx(-1.1)
    assert ob_mss["realized_r_source"] == "recorded"


def test_source_hash_drift_fails_before_b1_output_write(tmp_path):
    fixture = tmp_path / "fixture"
    materialize_legacy(fixture, "trades")
    for relative in (
        "exports/strategy-discovery/step-0",
        "registry",
        "canonical/adjudications/strategy-mapping-review",
    ):
        shutil.copytree(REPO_ROOT / relative, fixture / relative)
    shutil.copy2(
        REPO_ROOT / "canonical/adjudications/adj_filename_date_policy_20260901_v2.json",
        fixture / "canonical/adjudications/adj_filename_date_policy_20260901_v2.json",
    )
    source = fixture / "trades/2026/03/2026-03-27_ETHUSDT_L_11.md"
    source.write_bytes(source.read_bytes() + b"\n# fixture drift\n")

    with pytest.raises(B1Error, match="source hash mismatch"):
        build_dataset(fixture)
    assert not (fixture / "exports/method-validation").exists()


def test_source_body_hash_drift_fails_closed(tmp_path):
    source = tmp_path / "trades/example.md"
    source.parent.mkdir()
    source.write_text("---\ndirection: L\n---\nbody\n", encoding="utf-8", newline="")
    parsed_body = "body\n"
    step0 = {
        "trade_ref": "trades/example.md",
        "source_path": "trades/example.md",
        "source_sha256": _sha256_bytes(source.read_bytes()),
        "source_body_sha256": _sha256_bytes((parsed_body + "drift").encode("utf-8")),
    }
    with pytest.raises(B1Error, match="source body hash mismatch"):
        _load_verified_source(tmp_path, step0)


def test_source_commit_resolution_canonicalizes_valid_refs():
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    assert trading_os_cli._resolve_source_commit(REPO_ROOT, "HEAD") == head
    assert trading_os_cli._resolve_source_commit(REPO_ROOT, head.upper()) == head


def test_source_commit_resolution_failure_is_fail_closed(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0])

    monkeypatch.setattr(trading_os_cli.subprocess, "check_output", fail)
    with pytest.raises(ValueError, match="cannot be resolved"):
        trading_os_cli._resolve_source_commit(REPO_ROOT, None)


def test_invalid_source_commit_cli_writes_no_artifact(tmp_path):
    out = tmp_path / "invalid-b1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trading_os",
            "b1-dataset",
            "--generated-at",
            "2026-09-10T00:00:00Z",
            "--source-commit",
            "not-a-commit-ref",
            "--output-dir",
            str(out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "cannot be resolved" in result.stderr
    assert not out.exists()


def test_derived_r_only_fills_a_genuine_gap(rows):
    for row in rows:
        if row["realized_r_source"] == "derived":
            assert row["recorded_realized_r"] in (None, 0)
            assert row["derived_realized_r"] is not None
            assert row["canonical_realized_r"] == row["derived_realized_r"]
        if row["realized_r_conflict"]:
            assert "realized_r_conflict" in row["data_quality_flags"]
            assert row["recorded_realized_r"] not in (None, 0)
            assert row["derived_realized_r"] is not None


def test_parent_strategy_id_consistent_with_registry(rows):
    import yaml

    registry = yaml.safe_load((REPO_ROOT / "registry" / "strategy-registry.yaml").read_text(encoding="utf-8"))
    by_id = {s["strategy_id"]: s for s in registry["strategies"]}
    for row in rows:
        sid = row["strategy_id"]
        if sid is None:
            continue
        assert sid in by_id
        entry = by_id[sid]
        expected_parent = entry.get("parent_strategy_id") or sid
        assert row["parent_strategy_id"] == expected_parent
        assert row["family_strategy_id"] == expected_parent
        assert row["strategy_status"] == entry["status"]
        assert row["strategy_classification"] == entry["classification"]


def test_lifecycle_is_a_dimension_not_an_exclusion(rows):
    """Retired / experimental strategy trades are kept and can still be eligible."""

    retired_or_exp = [r for r in rows if r["strategy_status"] in ("retired", "experimental")]
    assert len(retired_or_exp) == 62
    assert any(r["analysis_eligibility"] == "eligible" for r in retired_or_exp)


def test_placeholder_body_rows_are_kept_and_flag_not_eligibility(rows):
    placeholder = [r for r in rows if "placeholder_body" in r["data_quality_flags"]]
    assert len(placeholder) >= 40
    # kept in the dataset, and most remain B2-eligible on real field completeness
    assert any(r["analysis_eligibility"] == "eligible" for r in placeholder)


def test_placeholder_detector_separates_genuine_stubs_from_stray_tokens(rows):
    by_ref = {r["trade_ref"]: r for r in rows}
    flagged = {r["trade_ref"] for r in rows if "placeholder_body" in r["data_quality_flags"]}
    # exactly the 5 known false positives are NOT flagged...
    for ref in PLACEHOLDER_FALSE_POSITIVES:
        assert ref in by_ref
        assert "placeholder_body" not in by_ref[ref]["data_quality_flags"], ref
        # ...and they stay eligible with a real recorded R (unchanged by the fix)
        assert by_ref[ref]["analysis_eligibility"] == "eligible"
        assert by_ref[ref]["realized_r_source"] == "recorded"
    # the genuine stubs (all still flagged) are ~44 and every one has an
    # unfilled stub-defining token in its body
    assert 42 <= len(flagged) <= 46
    assert flagged.isdisjoint(PLACEHOLDER_FALSE_POSITIVES)
    stub_tokens = {"{{entry_price}}", "{{sl_price}}", "{{tp_price}}", "{{result}}", "{{r_realized}}", "{{r_ratio}}"}
    for ref in flagged:
        text = legacy_bytes(ref).decode("utf-8", errors="replace")
        import re as _re

        distinct = set(_re.findall(r"\{\{\s*([a-zA-Z_]+)\s*\}\}", text))
        assert len(distinct) >= 2 or any(tok.strip("{}") in distinct for tok in stub_tokens), ref
    # 2026-07-14-BTC.md keeps its separate, correct strategy_label_blank flag
    assert "strategy_label_blank" in by_ref["trades/2026/07/2026-07-14-BTC.md"]["data_quality_flags"]


def test_sample_origin_has_no_unknown_and_records_its_source(rows, manifest):
    for row in rows:
        assert row["sample_origin"] in ("backtest", "live", "unknown")
        assert row["sample_origin_source"] in SAMPLE_ORIGIN_SOURCES
        if row["sample_origin"] == "unknown":
            # only permitted when the event year is genuinely undeterminable
            assert row["sample_origin_source"] == "unknown"
            assert "sample_origin_undeterminable" in row["data_quality_flags"]
        if row["sample_origin_source"] == "explicit_marker":
            assert row["sample_origin"] in ("backtest", "live")
        if row["sample_origin_source"] == "date_rule_2024plus_live":
            assert row["sample_origin"] == "live"
            assert row["event_date"][:4] >= "2024"
        if row["sample_origin_source"] == "date_rule_pre2024_backtest":
            assert row["sample_origin"] == "backtest"
            assert row["event_date"][:4] <= "2023"
    # owner ruling: no unmarked row is left unknown in the current corpus
    unknown = [r for r in rows if r["sample_origin"] == "unknown"]
    assert unknown == [], [r["trade_ref"] for r in unknown]
    assert manifest["row_count_invariants"]["sample_origin_unknown_rows"] == 0
    # any date-rule-contested rows are surfaced, not silently flipped
    contested = [r["trade_ref"] for r in rows if "sample_origin_date_rule_contested" in r["data_quality_flags"]]
    assert manifest["row_count_invariants"]["sample_origin_date_rule_contested_rows"] == len(contested)


def test_explicit_backtest_marker_beats_the_date_rule(rows):
    """The 2024-01 de-dup / _unspecified rows carry a `backtest` tag -> stay backtest."""

    by_ref = {r["trade_ref"]: r for r in rows}
    for ref in (
        "trades/2024/01/2024-01-24_BTCUSDT_L_unspecified.md",
        "trades/2024/01/2024-01-29_BTCUSDT_L_unspecified.md",
    ):
        assert by_ref[ref]["sample_origin"] == "backtest"
        assert by_ref[ref]["sample_origin_source"] == "explicit_marker"


def test_realized_r_conflict_threshold_is_the_tightened_v2_value(manifest):
    assert _R_CONFLICT_ABS_TOLERANCE == 0.15
    assert _R_CONFLICT_REL_TOLERANCE == 0.10
    thr = manifest["realized_r_conflict_threshold"]
    assert thr["abs_r"] == 0.15
    assert thr["rel_fraction_of_recorded"] == 0.10


def test_date_policy_is_actually_consumed(rows, manifest):
    assert "filename_date_policy_sha256" in manifest["pinned_source_digests"]
    override_rows = [r for r in rows if "date_from_adjudication_override" in r["data_quality_flags"]]
    # the policy's per-trade `dates` map has 3 entries; all 3 are in the corpus
    assert {r["trade_ref"] for r in override_rows} == {
        "trades/2026/04/2026-04-08_SOL_S_MSS_OTT.md",
        "trades/2026/06/2026-06-20-ETH.md",
        "trades/2026/08/2026-08-04-ADA.md",
    }
    for r in override_rows:
        assert r["event_date"] and r["recorded_date"]


def test_no_performance_metric_is_present(rows, manifest):
    assert manifest["computes_performance_metrics"] is False
    banned = ("win_rate", "winrate", "expectancy", "avg_r", "average_r", "drawdown", "sharpe", "equity", "profit_factor", "pnl")
    blob = json.dumps(rows, ensure_ascii=False).lower() + json.dumps(manifest, ensure_ascii=False).lower()
    for token in banned:
        assert token not in blob, token


def test_generator_source_does_not_read_old_dashboards():
    src = (REPO_ROOT / "trading_os" / "method_validation.py").read_text(encoding="utf-8")
    assert FORBIDDEN_SOURCE_BASENAMES == ("PERFORMANCE.md", "TRADE_JOURNAL.md")
    # The generator may *name* the old dashboards (to document that they are
    # forbidden) but must never open/read them, and must never import a stale
    # aggregate figure from them.
    import re as _re

    reads = _re.findall(r"(?:read_text|read_bytes|open|load|_sha256_file)\s*\([^)]*", src)
    for fragment in reads:
        assert "PERFORMANCE" not in fragment.upper()
        assert "TRADE_JOURNAL" not in fragment.upper()
        assert "DASHBOARD" not in fragment.upper()
    for stale_figure in ("62.6%", "529.9", "+2.47 R", "213 筆", "214 |"):
        assert stale_figure not in src

    # runtime proof: the built rows carry no data sourced from those files.
    rows_blob = json.dumps(
        [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()],
        ensure_ascii=False,
    )
    assert "PERFORMANCE.md" not in rows_blob
    assert "TRADE_JOURNAL.md" not in rows_blob


def test_manifest_invariants_match_the_dataset(rows, manifest):
    inv = manifest["row_count_invariants"]
    assert inv["total_rows"] == 255 == len(rows)
    assert inv["expected_total_rows"] == 255
    from collections import Counter

    assert inv["by_analysis_eligibility"] == dict(sorted(Counter(r["analysis_eligibility"] for r in rows).items()))
    assert inv["by_sample_origin"] == dict(sorted(Counter(r["sample_origin"] for r in rows).items()))
    assert inv["by_strategy_id"] == dict(sorted(Counter(str(r["strategy_id"]) for r in rows).items()))
    assert inv["realized_r_conflicts"] == sum(1 for r in rows if r["realized_r_conflict"])
    assert inv["placeholder_body_rows"] == sum(1 for r in rows if "placeholder_body" in r["data_quality_flags"])
    total_by_elig = sum(inv["by_analysis_eligibility"].values())
    assert total_by_elig == 255


def test_manifest_digest_verifies(manifest):
    digest = manifest["manifest_digest"]
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_digest"}
    assert digest == _sha256_bytes(_canonical_bytes(unsigned))
    assert manifest["dataset_sha256"] == _sha256_bytes(DATASET_PATH.read_bytes())


def test_pinned_source_digests_match_current_inputs(manifest):
    pinned = manifest["pinned_source_digests"]
    checks = {
        "step0_results_sha256": "exports/strategy-discovery/step-0/step-0-results.jsonl",
        "per_trade_application_sha256": "canonical/adjudications/strategy-mapping-review/strategy-mapping-per-trade-application-20260910.jsonl",
        "residual_lane_handoff_sha256": "canonical/adjudications/strategy-mapping-review/strategy-mapping-residual-lane-handoff-20260910.jsonl",
        "filename_date_policy_sha256": "canonical/adjudications/adj_filename_date_policy_20260901_v2.json",
    }
    for key, rel in checks.items():
        assert pinned[key] == _sha256_bytes((REPO_ROOT / rel).read_bytes())
    # B1 is an immutable historical artifact.  Its registry provenance must
    # remain the pre-B3 snapshot even though the current registry is now owned
    # by the later B3 stage.
    assert pinned["registry_digest"] == "5d4069adbd4b35d8d6c344fb1f0fd8b533fdc3d1ffe501900fc0e456b961f4f4"
    assert pinned["registry_file_sha256"] == "4847d1ca133e81161398745449a745a2288cd432238ca9b897ce92b1de6a9fe4"


def test_generator_is_deterministic_byte_identical(tmp_path):
    first = tmp_path / "run-1"
    second = tmp_path / "run-2"
    run_b1_dataset(REPO_ROOT, output_dir=first, **_GEN_ARGS)
    run_b1_dataset(REPO_ROOT, output_dir=second, **_GEN_ARGS)
    for name in ("canonical-trade-dataset.jsonl", "manifest.json", "README.md"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_regeneration_matches_the_committed_artifact(tmp_path):
    out = tmp_path / "regen"
    run_b1_dataset(REPO_ROOT, output_dir=out, **_GEN_ARGS)
    assert (out / "canonical-trade-dataset.jsonl").read_bytes() == DATASET_PATH.read_bytes()

    generated = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    committed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for key in ("dataset_version", "source_commit", "dataset_sha256", "row_count_invariants"):
        assert generated[key] == committed[key], key
    assert committed["manifest_digest"] == "e9efe9d145e8203d0e1b63cbcaaa303bfed73f4ee4fc7337fd96eef6ab844ffe"
    assert generated["pinned_source_digests"]["registry_file_sha256"] != committed["pinned_source_digests"]["registry_file_sha256"]
