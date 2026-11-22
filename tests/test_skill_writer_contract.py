"""SKILL.md's pre-write checklist must name exactly what the writer enforces.

If a writer field, schema or authority changes, this fails until the checklist is updated,
so an Agent that follows SKILL.md is never told something the writer no longer accepts.
"""
from __future__ import annotations

from pathlib import Path

from trading_os.registration_executor import (
    COMPACT_EXECUTOR_INPUT_SCHEMA,
    _CAPTURE_REQUIRED,
    _SUPPLEMENT_SCHEMAS,
)
from trading_os.live_chart_plan import PROVENANCE

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")
CHECKLIST = SKILL[SKILL.index("### 寫入前必備資訊清單"):]


def test_checklist_names_every_capture_field_the_writer_requires() -> None:
    missing = [key for key in _CAPTURE_REQUIRED if f"`{key}`" not in CHECKLIST]
    assert not missing, missing


def test_checklist_names_the_executor_input_and_every_supplement_schema() -> None:
    assert COMPACT_EXECUTOR_INPUT_SCHEMA in CHECKLIST
    assert "registration-intake.v1" in CHECKLIST
    for schema in _SUPPLEMENT_SCHEMAS:
        base = schema.rsplit(".v", 1)[0]
        assert base in CHECKLIST, schema


def test_checklist_names_every_r_authority_and_the_chart_provenance_values() -> None:
    for authority in ("deterministic_execution_calculation", "owner_attestation", "governed_manual_adjudication"):
        assert f"`{authority}`" in CHECKLIST
    for value in PROVENANCE:
        assert f"`{value}`" in CHECKLIST


def test_checklist_states_the_r_rule_and_forbids_a_fixed_amount() -> None:
    assert "1R = 實際成交進場價到圖上 SL 的距離" in CHECKLIST
    assert "固定金額" in CHECKLIST
    assert "prepare-confirmation" in CHECKLIST


def test_owner_confirmation_keys_in_the_checklist_match_the_writer() -> None:
    for key in ("status", "confirmed_at", "preview_digest"):
        assert f"`{key}" in CHECKLIST
