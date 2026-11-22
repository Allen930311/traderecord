"""Calibration contract and executable matrix tests for the §11 diagnostic decision tree."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from test_operational_closeout import _make_fixture
from trading_os.evidence_store import EvidenceStore
from trading_os.registry import load_effective_rules
from trading_os.review_preview import (
    ANALYSIS_SCHEMA,
    ReviewPreviewError,
    _relevant_rules,
    _strategy_pin,
    prepare_review_preview,
)

ROOT = Path(__file__).resolve().parents[1]
REVIEW_RULES = ROOT / "REVIEW_RULES.md"
SINGLE_REVIEW_WORKFLOW = ROOT / "workflows" / "single-trade-review.yaml"


class _MemoryReadOnlyStore:
    def __init__(self, root: Path, record: dict[str, Any]) -> None:
        self.root = root
        self._record = record

    def current(self) -> dict[str, Any]:
        return {"records": {self._record["trade_id"]: self._record}}


def _prepare_isolated_root(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    shutil.copytree(ROOT / "registry", tmp_path / "registry")
    chart_path = tmp_path / "fixture-chart.png"
    chart_path.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic diagnostic decision tree fixture")
    artifact = EvidenceStore(tmp_path).ingest(chart_path, original_filename="fixture-chart.png")
    evidence = {
        **artifact,
        "source_ref": artifact["locator"],
        "category": "pre_trade_context",
        "timing": "pre_trade",
        "type": "chart_snapshot",
        "captured_at": "2026-10-11T10:00:00+08:00",
    }
    ref = {"sha256": artifact["sha256"], "source_ref": artifact["locator"]}
    return tmp_path, {"evidence": evidence, "ref": ref}


def _make_record(
    *,
    strategy_id: str | None,
    rule_version: str | None,
    resolution_status: str,
    evidence: dict[str, Any],
    owner_statements: list[dict[str, Any]] | None = None,
    no_setup_attestation: bool = False,
) -> dict[str, Any]:
    resolution: dict[str, Any] = {"status": resolution_status}
    if strategy_id is not None:
        resolution["strategy_id"] = strategy_id
    if rule_version is not None:
        resolution["rule_version"] = rule_version
    if no_setup_attestation:
        resolution["owner_attestation"] = {"setup_status": "NO_SETUP"}
    return {
        "trade_id": "trd_diag_contract_001",
        "trade_no": "T000901",
        "state": "CLOSED",
        "trade_type": "live",
        "execution_environment": "LIVE",
        "strategy_id": strategy_id,
        "rule_version": rule_version,
        "strategy_resolution": resolution,
        "exit_verification": {"state": "confirmed"},
        "evidence": [evidence],
        "owner_statements": owner_statements or [],
        "lifecycle_event_ids": [],
    }


def _make_pinned_review(
    root: Path,
    record: dict[str, Any],
    ref: dict[str, str],
    *,
    rule_compliance: str,
    diagnostic_class: str,
    setup_quality: str = "valid",
    execution_quality: str = "aligned",
    row_overrides: dict[str, dict[str, Any]] | None = None,
    execution_deviation_support: dict[str, Any] | None = None,
    owner_decision_verbatim: str | None = "Owner confirms this synthetic review.",
) -> dict[str, Any]:
    pin, rules = _strategy_pin(root, record)
    assert pin["status"] == "PINNED"
    overrides = row_overrides or {}
    checklist = []
    for row in _relevant_rules(rules):
        patch = overrides.get(row["rule_id"], {})
        status = patch.get("status", "YES")
        refs = patch["evidence_refs"] if "evidence_refs" in patch else ([ref] if status in {"YES", "NO"} else [])
        checklist.append({
            "rule_id": row["rule_id"],
            "rule_version": pin["rule_version"],
            "lane": row["lane"],
            "importance": row["importance"],
            "status": status,
            "evidence_refs": refs,
            "rationale": patch.get("rationale", f"Synthetic evaluation for {row['rule_id']}."),
        })
    analysis: dict[str, Any] = {
        "schema_version": ANALYSIS_SCHEMA,
        "strategy_pin": pin,
        "checklist": checklist,
        "setup_quality": setup_quality,
        "execution_quality": execution_quality,
        "agent_assessment": {
            "setup": "Synthetic setup assessment.",
            "risk": "Synthetic risk assessment.",
            "execution": "Synthetic execution assessment.",
            "exit": "Synthetic exit assessment.",
            "lesson": "Synthetic lesson.",
        },
    }
    if owner_decision_verbatim is not None:
        analysis["owner_decision_verbatim"] = owner_decision_verbatim
    if execution_deviation_support is not None:
        analysis["execution_deviation_support"] = execution_deviation_support
    return {
        "what_went_well": "Synthetic review positive summary.",
        "what_went_wrong": "Synthetic review negative summary.",
        "rule_compliance": rule_compliance,
        "execution_deviation": "observed" if execution_quality == "deviated" else "none observed",
        "thesis_validity": "supported" if setup_quality == "valid" else "not_supported",
        "entry_quality": "observed",
        "exit_quality": "observed",
        "evidence_completeness": "complete",
        "owner_notes": "Synthetic diagnostic decision tree test.",
        "diagnostic_class": diagnostic_class,
        "review_analysis": analysis,
    }


def _make_unpinned_review(
    root: Path,
    record: dict[str, Any],
    *,
    rule_compliance: str,
    diagnostic_class: str,
    setup_quality: str = "not_observable",
    execution_quality: str = "not_observable",
    checklist: list[dict[str, Any]] | None = None,
    execution_deviation_support: dict[str, Any] | None = None,
    owner_decision_verbatim: str | None = "Owner confirms unpinned synthetic review.",
) -> dict[str, Any]:
    pin, _ = _strategy_pin(root, record)
    assert pin["status"] == "NOT_OBSERVABLE"
    analysis: dict[str, Any] = {
        "schema_version": ANALYSIS_SCHEMA,
        "strategy_pin": pin,
        "checklist": [] if checklist is None else checklist,
        "setup_quality": setup_quality,
        "execution_quality": execution_quality,
        "agent_assessment": {
            "setup": "No frozen strategy pin available.",
            "risk": "Risk context noted.",
            "execution": "Execution context noted.",
            "exit": "Exit context noted.",
            "lesson": "Synthetic unpinned lesson.",
        },
    }
    if owner_decision_verbatim is not None:
        analysis["owner_decision_verbatim"] = owner_decision_verbatim
    if execution_deviation_support is not None:
        analysis["execution_deviation_support"] = execution_deviation_support
    return {
        "what_went_well": "none observed",
        "what_went_wrong": "Synthetic unpinned review.",
        "rule_compliance": rule_compliance,
        "execution_deviation": "observed" if execution_quality == "deviated" else "none observed",
        "thesis_validity": "not_observable",
        "entry_quality": "unknown",
        "exit_quality": "unknown",
        "evidence_completeness": "complete",
        "owner_notes": "Synthetic unpinned diagnostic test.",
        "diagnostic_class": diagnostic_class,
        "review_analysis": analysis,
    }


# ---------------------------------------------------------------------------
# 1. Structural documentation & workflow contract tests
# ---------------------------------------------------------------------------


def test_review_rules_decision_tree_calibration_and_positive_negative_examples() -> None:
    text = REVIEW_RULES.read_text(encoding="utf-8")
    section_11 = text.split("## 11. 復盤輸出結論格式", 1)[1]

    for heading in (
        "### diagnostic_class 與 rule_compliance 判定決策樹（Decision Tree）",
        "#### 逐步確定性判定表（Deterministic Decision Table）",
        "#### Runtime Hard Reject（`ReviewPreviewError`）vs Normative SOP 邊界對照",
        "#### 五種 diagnostic_class 正反案例（Positive & Negative Examples）",
        "### Setup Quality 與 Execution Quality 一致性錨點",
    ):
        assert heading in section_11, f"Missing required heading: {heading}"

    for token in (
        '"PINNED"',
        '"NOT_OBSERVABLE"',
        "no_match",
        "unresolved",
        "ambiguous",
        "insufficient_evidence",
        "unknown",
        "not_observable",
        "_relevant_rules",
        "optional_confluence",
        "execution_deviation_support",
        "ReviewPreviewError",
        "[Runtime Hard Reject]",
        "[Normative SOP Rule]",
        "[Normative SOP Error]",
    ):
        assert token in section_11, f"Missing calibration token {token!r} in §11"

    assert 'rule_compliance: "violation"` + `diagnostic_class: "PROCESS-RISK"`' in section_11
    assert "ALWAYS rejected by runtime" in section_11

    examples_section = section_11.split("#### 五種 diagnostic_class 正反案例（Positive & Negative Examples）", 1)[1]
    examples_section = examples_section.split("### Setup Quality 與 Execution Quality 一致性錨點", 1)[0]

    ordered_classes = ("METHOD", "EXECUTION", "PROCESS-RISK", "INSUFFICIENT", "MIXED")
    markers = [f"**`{name}`" for name in ordered_classes]
    positions = [examples_section.index(marker) for marker in markers]
    assert positions == sorted(positions)

    for idx, name in enumerate(ordered_classes):
        start = positions[idx]
        end = positions[idx + 1] if idx + 1 < len(positions) else len(examples_section)
        block = examples_section[start:end]
        assert "正例" in block, f"{name} block missing 正例"
        assert "反例" in block, f"{name} block missing 反例"
        assert "[Runtime Hard Reject]" in block, f"{name} block missing [Runtime Hard Reject]"
        assert "[Normative SOP Error]" in block, f"{name} block missing [Normative SOP Error]"


def test_single_trade_review_workflow_points_to_calibrated_decision_tree() -> None:
    data = yaml.safe_load(SINGLE_REVIEW_WORKFLOW.read_text(encoding="utf-8"))
    assert data["architecture"]["review_policy"] == "REVIEW_RULES.md"
    pointer = data["architecture"]["diagnostic_decision_tree"]
    assert "REVIEW_RULES.md §11" in pointer
    assert "trading_os/review_preview.py" in pointer


# ---------------------------------------------------------------------------
# 2. Runtime helper calibration tests (_strategy_pin, _relevant_rules)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["no_match", "unresolved", "ambiguous", "insufficient_evidence", "unknown", "not_observable"],
)
def test_strategy_pin_produces_only_pinned_or_not_observable_for_unresolved_statuses(
    tmp_path: Path, status: str
) -> None:
    root, assets = _prepare_isolated_root(tmp_path)
    record = _make_record(
        strategy_id="stg-ict-liquidity",
        rule_version="stg-ict-liquidity:v1",
        resolution_status=status,
        evidence=assets["evidence"],
    )
    pin, rules = _strategy_pin(root, record)
    assert pin["status"] == "NOT_OBSERVABLE"
    assert rules == []


def test_strategy_pin_missing_mismatch_or_unregistered_produces_not_observable(tmp_path: Path) -> None:
    root, assets = _prepare_isolated_root(tmp_path)

    missing = _make_record(
        strategy_id=None,
        rule_version=None,
        resolution_status="matched",
        evidence=assets["evidence"],
    )
    assert _strategy_pin(root, missing)[0]["status"] == "NOT_OBSERVABLE"

    mismatch = _make_record(
        strategy_id="stg-ict-liquidity",
        rule_version="stg-ict-liquidity:v1",
        resolution_status="matched",
        evidence=assets["evidence"],
    )
    mismatch["strategy_resolution"]["strategy_id"] = "stg-vegas-tunnel"
    assert _strategy_pin(root, mismatch)[0]["status"] == "NOT_OBSERVABLE"

    unregistered = _make_record(
        strategy_id="stg-ict-liquidity",
        rule_version="stg-ict-liquidity:v999",
        resolution_status="matched",
        evidence=assets["evidence"],
    )
    assert _strategy_pin(root, unregistered)[0]["status"] == "NOT_OBSERVABLE"

    valid = _make_record(
        strategy_id="stg-ict-liquidity",
        rule_version="stg-ict-liquidity:v1",
        resolution_status="matched",
        evidence=assets["evidence"],
    )
    pin, rules = _strategy_pin(root, valid)
    assert pin["status"] == "PINNED"
    assert len(rules) == 5


def test_relevant_rules_lane_importance_and_sorting_calibration() -> None:
    ict_rules = load_effective_rules(ROOT, "stg-ict-liquidity:v1")
    ict_selected = _relevant_rules(ict_rules)
    assert ict_selected == [
        {"rule_id": "ict_liquidity.entry_expression", "lane": "setup", "importance": "required"},
        {"rule_id": "ict_liquidity.identity", "lane": "setup", "importance": "required"},
        {"rule_id": "ict_liquidity.invalidation", "lane": "execution", "importance": "required"},
        {"rule_id": "ict_liquidity.structure_confirmation", "lane": "setup", "importance": "optional_confluence"},
        {"rule_id": "ict_liquidity.target", "lane": "execution", "importance": "required"},
    ]

    example_a_rules = load_effective_rules(ROOT, "stg-example-a:v2")
    example_a_selected = _relevant_rules(example_a_rules)
    assert "example_a.conditional_retry" not in {row["rule_id"] for row in example_a_selected}
    assert all(row["importance"] == "required" for row in example_a_selected)


# ---------------------------------------------------------------------------
# 3. Executable prepare_review_preview() POSITIVE matrix (ALL 5 classes)
# ---------------------------------------------------------------------------


def _build_positive_case(
    case_id: str,
    root: Path,
    ref: dict[str, str],
    evidence: dict[str, Any],
    verbatim_exec: str,
    owner_stmt: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    ict_record = _make_record(
        strategy_id="stg-ict-liquidity",
        rule_version="stg-ict-liquidity:v1",
        resolution_status="matched",
        evidence=evidence,
        owner_statements=[owner_stmt],
    )
    vegas_record = _make_record(
        strategy_id="stg-vegas-tunnel",
        rule_version="stg-vegas-tunnel:v1",
        resolution_status="matched",
        evidence=evidence,
        owner_statements=[owner_stmt],
    )
    unpinned_record = _make_record(
        strategy_id=None,
        rule_version=None,
        resolution_status="no_match",
        evidence=evidence,
        owner_statements=[owner_stmt],
        no_setup_attestation=True,
    )

    if case_id == "METHOD-clean_optional_no":
        return ict_record, _make_pinned_review(
            root, ict_record, ref,
            rule_compliance="compliant",
            diagnostic_class="METHOD",
            setup_quality="valid",
            execution_quality="aligned",
            row_overrides={"ict_liquidity.structure_confirmation": {"status": "NO"}},
        )
    if case_id == "METHOD-clean_optional_unknown":
        return ict_record, _make_pinned_review(
            root, ict_record, ref,
            rule_compliance="compliant",
            diagnostic_class="METHOD",
            setup_quality="valid",
            execution_quality="aligned",
            row_overrides={"ict_liquidity.structure_confirmation": {"status": "UNKNOWN", "evidence_refs": []}},
        )
    if case_id == "METHOD-setup_required_no":
        return ict_record, _make_pinned_review(
            root, ict_record, ref,
            rule_compliance="violation",
            diagnostic_class="METHOD",
            setup_quality="invalid",
            execution_quality="aligned",
            row_overrides={"ict_liquidity.entry_expression": {"status": "NO"}},
        )
    if case_id == "EXECUTION-pinned_exec_lane_no":
        return ict_record, _make_pinned_review(
            root, ict_record, ref,
            rule_compliance="violation",
            diagnostic_class="EXECUTION",
            setup_quality="valid",
            execution_quality="deviated",
            row_overrides={"ict_liquidity.invalidation": {"status": "NO"}},
            execution_deviation_support={
                "authority": "evidence",
                "owner_statement": None,
                "evidence_refs": [ref],
                "rationale": "Attached chart confirms stop was not placed at structural invalidation.",
            },
        )
    if case_id == "EXECUTION-pinned_owner_attestation":
        return vegas_record, _make_pinned_review(
            root, vegas_record, ref,
            rule_compliance="violation",
            diagnostic_class="EXECUTION",
            setup_quality="valid",
            execution_quality="deviated",
            owner_decision_verbatim=None,
            execution_deviation_support={
                "authority": "owner_attestation",
                "owner_statement": verbatim_exec,
                "evidence_refs": [],
                "rationale": "Verified record.owner_statements confirms premature entry and stop move.",
            },
        )
    if case_id == "EXECUTION-unpinned_owner_decision_verbatim":
        decision = "Owner confirms premature stop move after an entry without setup."
        return unpinned_record, _make_unpinned_review(
            root, unpinned_record,
            rule_compliance="violation",
            diagnostic_class="EXECUTION",
            setup_quality="not_observable",
            execution_quality="deviated",
            owner_decision_verbatim=decision,
            execution_deviation_support={
                "authority": "owner_attestation",
                "owner_statement": decision,
                "evidence_refs": [],
                "rationale": "Independent execution deviation attested via owner_decision_verbatim.",
            },
        )
    if case_id == "PROCESS-RISK-compliant_non_rule_flaw":
        return vegas_record, _make_pinned_review(
            root, vegas_record, ref,
            rule_compliance="compliant",
            diagnostic_class="PROCESS-RISK",
            setup_quality="valid",
            execution_quality="aligned",
        )
    if case_id == "PROCESS-RISK-unknown_with_non_rule_flaw":
        return vegas_record, _make_pinned_review(
            root, vegas_record, ref,
            rule_compliance="unknown",
            diagnostic_class="PROCESS-RISK",
            setup_quality="unknown",
            execution_quality="aligned",
            row_overrides={"vegas.tunnel.touch": {"status": "UNKNOWN", "evidence_refs": []}},
        )
    if case_id == "INSUFFICIENT-unpinned_no_setup":
        return unpinned_record, _make_unpinned_review(
            root, unpinned_record,
            rule_compliance="not_observable",
            diagnostic_class="INSUFFICIENT",
            setup_quality="invalid",
            execution_quality="not_observable",
        )
    if case_id == "INSUFFICIENT-pinned_required_unknown":
        return ict_record, _make_pinned_review(
            root, ict_record, ref,
            rule_compliance="unknown",
            diagnostic_class="INSUFFICIENT",
            setup_quality="unknown",
            execution_quality="aligned",
            row_overrides={"ict_liquidity.entry_expression": {"status": "UNKNOWN", "evidence_refs": []}},
        )
    if case_id == "MIXED-setup_no_and_execution_no":
        return ict_record, _make_pinned_review(
            root, ict_record, ref,
            rule_compliance="violation",
            diagnostic_class="MIXED",
            setup_quality="invalid",
            execution_quality="deviated",
            row_overrides={
                "ict_liquidity.entry_expression": {"status": "NO"},
                "ict_liquidity.invalidation": {"status": "NO"},
            },
        )
    if case_id == "MIXED-setup_no_and_supported_deviation":
        return vegas_record, _make_pinned_review(
            root, vegas_record, ref,
            rule_compliance="violation",
            diagnostic_class="MIXED",
            setup_quality="invalid",
            execution_quality="deviated",
            row_overrides={"vegas.tunnel.touch": {"status": "NO"}},
            execution_deviation_support={
                "authority": "evidence",
                "owner_statement": None,
                "evidence_refs": [ref],
                "rationale": "Evidence shows both setup failure and premature entry deviation.",
            },
        )
    raise AssertionError(f"Unknown case_id: {case_id}")


@pytest.mark.parametrize(
    "case_id",
    [
        "METHOD-clean_optional_no",
        "METHOD-clean_optional_unknown",
        "METHOD-setup_required_no",
        "EXECUTION-pinned_exec_lane_no",
        "EXECUTION-pinned_owner_attestation",
        "EXECUTION-unpinned_owner_decision_verbatim",
        "PROCESS-RISK-compliant_non_rule_flaw",
        "PROCESS-RISK-unknown_with_non_rule_flaw",
        "INSUFFICIENT-unpinned_no_setup",
        "INSUFFICIENT-pinned_required_unknown",
        "MIXED-setup_no_and_execution_no",
        "MIXED-setup_no_and_supported_deviation",
    ],
)
def test_prepare_review_preview_positive_matrix_all_five_diagnostic_classes(
    tmp_path: Path, case_id: str
) -> None:
    root, assets = _prepare_isolated_root(tmp_path)
    verbatim_exec = "我當時沒等確認就直接提前進場，並且擅自把停損推到保本。"
    owner_stmt = {
        "authority": "owner_supplied_verbatim",
        "verbatim_text": verbatim_exec,
        "sha256": hashlib.sha256(verbatim_exec.encode("utf-8")).hexdigest(),
    }
    record, review = _build_positive_case(
        case_id, root, assets["ref"], assets["evidence"], verbatim_exec, owner_stmt
    )
    store = _MemoryReadOnlyStore(root, record)
    preview = prepare_review_preview(root, store, record["trade_id"], review)
    assert preview["preview_digest"]
    assert preview["content"]["review_candidate"]["diagnostic_class"] == review["diagnostic_class"]


# ---------------------------------------------------------------------------
# 4. Executable prepare_review_preview() NEGATIVE matrix (ALL 5 classes)
# ---------------------------------------------------------------------------


def _build_negative_case(
    case_id: str,
    root: Path,
    ref: dict[str, str],
    evidence: dict[str, Any],
    verbatim_exec: str,
    owner_stmt: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    ict_record = _make_record(
        strategy_id="stg-ict-liquidity",
        rule_version="stg-ict-liquidity:v1",
        resolution_status="matched",
        evidence=evidence,
        owner_statements=[owner_stmt],
    )
    vegas_record = _make_record(
        strategy_id="stg-vegas-tunnel",
        rule_version="stg-vegas-tunnel:v1",
        resolution_status="matched",
        evidence=evidence,
        owner_statements=[owner_stmt],
    )
    unpinned_record = _make_record(
        strategy_id=None,
        rule_version=None,
        resolution_status="no_match",
        evidence=evidence,
        owner_statements=[owner_stmt],
        no_setup_attestation=True,
    )
    valid_evidence_support = {
        "authority": "evidence",
        "owner_statement": None,
        "evidence_refs": [ref],
        "rationale": "Attached chart proves execution deviation.",
    }
    valid_owner_support = {
        "authority": "owner_attestation",
        "owner_statement": verbatim_exec,
        "evidence_refs": [],
        "rationale": "Owner verbatim attestation.",
    }

    cases: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
        # METHOD negatives
        "METHOD-unpinned_violation": (
            unpinned_record,
            _make_unpinned_review(root, unpinned_record, rule_compliance="violation", diagnostic_class="METHOD"),
        ),
        "METHOD-unpinned_compliant": (
            unpinned_record,
            _make_unpinned_review(root, unpinned_record, rule_compliance="compliant", diagnostic_class="METHOD"),
        ),
        "METHOD-pinned_execution_lane_no": (
            ict_record,
            _make_pinned_review(
                root, ict_record, ref,
                rule_compliance="violation",
                diagnostic_class="METHOD",
                row_overrides={"ict_liquidity.invalidation": {"status": "NO"}},
            ),
        ),
        "METHOD-pinned_supported_deviation": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="violation",
                diagnostic_class="METHOD",
                execution_quality="deviated",
                execution_deviation_support=valid_evidence_support,
            ),
        ),
        "METHOD-pinned_required_unknown_violation": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="violation",
                diagnostic_class="METHOD",
                row_overrides={"vegas.tunnel.touch": {"status": "UNKNOWN", "evidence_refs": []}},
            ),
        ),
        # EXECUTION negatives
        "EXECUTION-pinned_deviated_missing_support": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="violation",
                diagnostic_class="EXECUTION",
                execution_quality="deviated",
            ),
        ),
        "EXECUTION-pinned_compliant_with_support": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="compliant",
                diagnostic_class="EXECUTION",
                execution_quality="deviated",
                execution_deviation_support=valid_evidence_support,
            ),
        ),
        "EXECUTION-fabricated_owner_statement": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="violation",
                diagnostic_class="EXECUTION",
                execution_quality="deviated",
                execution_deviation_support={
                    "authority": "owner_attestation",
                    "owner_statement": "Agent invented this statement.",
                    "evidence_refs": [],
                    "rationale": "Fabricated attestation.",
                },
            ),
        ),
        "EXECUTION-evidence_authority_empty_refs": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="violation",
                diagnostic_class="EXECUTION",
                execution_quality="deviated",
                execution_deviation_support={
                    "authority": "evidence",
                    "owner_statement": None,
                    "evidence_refs": [],
                    "rationale": "Missing evidence ref.",
                },
            ),
        ),
        "EXECUTION-pinned_setup_lane_no": (
            ict_record,
            _make_pinned_review(
                root, ict_record, ref,
                rule_compliance="violation",
                diagnostic_class="EXECUTION",
                row_overrides={"ict_liquidity.entry_expression": {"status": "NO"}},
            ),
        ),
        "EXECUTION-unpinned_deviated_without_violation": (
            unpinned_record,
            _make_unpinned_review(
                root, unpinned_record,
                rule_compliance="not_observable",
                diagnostic_class="EXECUTION",
                execution_quality="deviated",
            ),
        ),
        # PROCESS-RISK negatives (CRITICAL: violation + PROCESS-RISK ALWAYS rejected)
        "PROCESS-RISK-pinned_all_yes_violation": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="violation",
                diagnostic_class="PROCESS-RISK",
            ),
        ),
        "PROCESS-RISK-pinned_setup_no_violation": (
            ict_record,
            _make_pinned_review(
                root, ict_record, ref,
                rule_compliance="violation",
                diagnostic_class="PROCESS-RISK",
                row_overrides={"ict_liquidity.entry_expression": {"status": "NO"}},
            ),
        ),
        "PROCESS-RISK-pinned_execution_no_violation": (
            ict_record,
            _make_pinned_review(
                root, ict_record, ref,
                rule_compliance="violation",
                diagnostic_class="PROCESS-RISK",
                row_overrides={"ict_liquidity.invalidation": {"status": "NO"}},
            ),
        ),
        "PROCESS-RISK-unpinned_violation_with_support": (
            unpinned_record,
            _make_unpinned_review(
                root, unpinned_record,
                rule_compliance="violation",
                diagnostic_class="PROCESS-RISK",
                execution_quality="deviated",
                execution_deviation_support=valid_owner_support,
            ),
        ),
        "PROCESS-RISK-pinned_required_unknown_compliant": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="compliant",
                diagnostic_class="PROCESS-RISK",
                row_overrides={"vegas.tunnel.touch": {"status": "UNKNOWN", "evidence_refs": []}},
            ),
        ),
        # INSUFFICIENT negatives
        "INSUFFICIENT-unpinned_compliant": (
            unpinned_record,
            _make_unpinned_review(root, unpinned_record, rule_compliance="compliant", diagnostic_class="INSUFFICIENT"),
        ),
        "INSUFFICIENT-unpinned_nonempty_checklist": (
            unpinned_record,
            _make_unpinned_review(
                root, unpinned_record,
                rule_compliance="not_observable",
                diagnostic_class="INSUFFICIENT",
                checklist=[{"rule_id": "vegas.tunnel.touch"}],
            ),
        ),
        "INSUFFICIENT-pinned_required_unknown_compliant": (
            ict_record,
            _make_pinned_review(
                root, ict_record, ref,
                rule_compliance="compliant",
                diagnostic_class="INSUFFICIENT",
                row_overrides={"ict_liquidity.entry_expression": {"status": "UNKNOWN", "evidence_refs": []}},
            ),
        ),
        "INSUFFICIENT-pinned_required_na_violation": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="violation",
                diagnostic_class="INSUFFICIENT",
                row_overrides={"vegas.tunnel.touch": {"status": "N/A", "evidence_refs": []}},
            ),
        ),
        "INSUFFICIENT-unpinned_violation": (
            unpinned_record,
            _make_unpinned_review(root, unpinned_record, rule_compliance="violation", diagnostic_class="INSUFFICIENT"),
        ),
        # MIXED negatives
        "MIXED-both_lanes_failed_labeled_method": (
            ict_record,
            _make_pinned_review(
                root, ict_record, ref,
                rule_compliance="violation",
                diagnostic_class="METHOD",
                row_overrides={
                    "ict_liquidity.entry_expression": {"status": "NO"},
                    "ict_liquidity.invalidation": {"status": "NO"},
                },
            ),
        ),
        "MIXED-setup_no_plus_deviation_labeled_execution": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="violation",
                diagnostic_class="EXECUTION",
                execution_quality="deviated",
                row_overrides={"vegas.tunnel.touch": {"status": "NO"}},
                execution_deviation_support=valid_evidence_support,
            ),
        ),
        "MIXED-unpinned_violation_labeled_mixed": (
            unpinned_record,
            _make_unpinned_review(
                root, unpinned_record,
                rule_compliance="violation",
                diagnostic_class="MIXED",
                execution_quality="deviated",
                execution_deviation_support=valid_owner_support,
            ),
        ),
        "MIXED-pinned_all_yes_deviated_missing_support": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="violation",
                diagnostic_class="MIXED",
                execution_quality="deviated",
            ),
        ),
        "MIXED-pinned_compliant_with_supported_deviation": (
            vegas_record,
            _make_pinned_review(
                root, vegas_record, ref,
                rule_compliance="compliant",
                diagnostic_class="MIXED",
                execution_quality="deviated",
                row_overrides={"vegas.tunnel.touch": {"status": "NO"}},
                execution_deviation_support=valid_evidence_support,
            ),
        ),
    }
    return cases[case_id]


@pytest.mark.parametrize(
    ("case_id", "match_pattern"),
    [
        ("METHOD-unpinned_violation", "NO_SETUP|frozen strategy pin"),
        ("METHOD-unpinned_compliant", "cannot be declared strategy compliant"),
        ("METHOD-pinned_execution_lane_no", "required execution-rule violation requires diagnostic_class EXECUTION or MIXED"),
        ("METHOD-pinned_supported_deviation", "explicit EXECUTION deviation assessment"),
        ("METHOD-pinned_required_unknown_violation", "evidence-linked required NO"),
        ("EXECUTION-pinned_deviated_missing_support", "specifically supported execution deviation"),
        ("EXECUTION-pinned_compliant_with_support", "requires rule_compliance violation"),
        ("EXECUTION-fabricated_owner_statement", "must be verbatim"),
        ("EXECUTION-evidence_authority_empty_refs", "needs a specific linked ref"),
        ("EXECUTION-pinned_setup_lane_no", "required setup-rule violation requires diagnostic_class METHOD or MIXED"),
        ("EXECUTION-unpinned_deviated_without_violation", "unpinned execution deviation requires specific Owner attestation or evidence"),
        ("PROCESS-RISK-pinned_all_yes_violation", "evidence-linked required NO"),
        ("PROCESS-RISK-pinned_setup_no_violation", "requires diagnostic_class METHOD or MIXED"),
        ("PROCESS-RISK-pinned_execution_no_violation", "requires diagnostic_class EXECUTION or MIXED"),
        ("PROCESS-RISK-unpinned_violation_with_support", "explicit EXECUTION deviation assessment"),
        ("PROCESS-RISK-pinned_required_unknown_compliant", "cannot be compliant while a required condition is NO, UNKNOWN, or N/A"),
        ("INSUFFICIENT-unpinned_compliant", "cannot be declared strategy compliant"),
        ("INSUFFICIENT-unpinned_nonempty_checklist", "cannot manufacture strategy rules"),
        ("INSUFFICIENT-pinned_required_unknown_compliant", "cannot be compliant while a required condition is NO, UNKNOWN, or N/A"),
        ("INSUFFICIENT-pinned_required_na_violation", "evidence-linked required NO"),
        ("INSUFFICIENT-unpinned_violation", "NO_SETUP|frozen strategy pin"),
        ("MIXED-both_lanes_failed_labeled_method", "combined strategy and execution rule failures require diagnostic_class MIXED"),
        ("MIXED-setup_no_plus_deviation_labeled_execution", "combined strategy and execution rule failures require diagnostic_class MIXED"),
        ("MIXED-unpinned_violation_labeled_mixed", "NO_SETUP|frozen strategy pin"),
        ("MIXED-pinned_all_yes_deviated_missing_support", "specifically supported execution deviation"),
        ("MIXED-pinned_compliant_with_supported_deviation", "requires rule_compliance violation"),
    ],
)
def test_prepare_review_preview_negative_matrix_all_five_diagnostic_classes(
    tmp_path: Path, case_id: str, match_pattern: str
) -> None:
    root, assets = _prepare_isolated_root(tmp_path)
    verbatim_exec = "Owner verbatim statement confirming premature exit."
    owner_stmt = {
        "authority": "owner_supplied_verbatim",
        "verbatim_text": verbatim_exec,
        "sha256": hashlib.sha256(verbatim_exec.encode("utf-8")).hexdigest(),
    }
    record, review = _build_negative_case(
        case_id, root, assets["ref"], assets["evidence"], verbatim_exec, owner_stmt
    )
    store = _MemoryReadOnlyStore(root, record)
    with pytest.raises(ReviewPreviewError, match=match_pattern):
        prepare_review_preview(root, store, record["trade_id"], review)


def test_checklist_yes_or_no_without_evidence_refs_is_hard_rejected(tmp_path: Path) -> None:
    store, _, trade_id = _make_fixture(ROOT, tmp_path, write_review=False, include_partial=False)
    record = store.current()["records"][trade_id]
    pin, rules = _strategy_pin(tmp_path, record)
    assert pin["status"] == "PINNED"
    ref = next(
        {"sha256": item["sha256"], "source_ref": item["source_ref"]}
        for item in record["evidence"]
        if item.get("type") == "chart_snapshot"
    )
    bad_review = _make_pinned_review(
        tmp_path, record, ref,
        rule_compliance="compliant",
        diagnostic_class="METHOD",
        row_overrides={"vegas.tunnel.touch": {"status": "YES", "evidence_refs": []}},
    )
    assert len(bad_review["review_analysis"]["checklist"]) == len(_relevant_rules(rules))
    with pytest.raises(ReviewPreviewError, match="YES/NO checklist findings require linked evidence"):
        prepare_review_preview(tmp_path, store, trade_id, bad_review)
