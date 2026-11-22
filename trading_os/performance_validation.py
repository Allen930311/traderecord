"""B2 — deterministic performance and method validation over frozen inputs.

This module is a downstream consumer of the immutable B1 canonical dataset and
the B3 frozen active-strategy authority.  It deliberately does not read the
legacy performance dashboards, re-derive trade R, or make any lifecycle/rule
decision.  Rule checks are diagnostic only because B1 does not contain the
chart/setup evidence required by most B3 rules.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .method_validation import _canonical_bytes
from .episode_identity import (
    B1_V2_DATASET_PATH,
    B1_V2_MANIFEST_PATH,
    B1_V3_DATASET_VERSION,
    B1_V3_SCHEMA_VERSION,
    COMPATIBILITY_FIELDS,
    DEFAULT_ADJUDICATION_OUTPUT,
    DEFAULT_B1_V3_OUTPUT,
    EPISODE_RESOLUTION_NAME,
    validate_b1_v3_manifest,
)
from .registry import load_effective_rules
from .rule_freeze import validate_b3_manifest


B2_SCHEMA_VERSION = "method-validation-b2.v1"
PERFORMANCE_SCHEMA_VERSION = "method-validation-b2-performance.v1"
B1_DATASET_VERSION = B1_V3_DATASET_VERSION
B1_DATASET_SHA256 = "00c76bdca0480085e3c9ac70dcfda9b6ce5147224e4bfb68b5baf17f98978729"
B1_MANIFEST_DIGEST = "9ca0825b5032fed703e46ab3c97d05dee933cd3aefc5594f5c62254475ead59f"
B1_MANIFEST_FILE_SHA256 = "db8f866da74a35fc87bdae797b7f695577c70735d01a601b2c00dcea941cafdd"
B1_EPISODE_RESOLUTION_SHA256 = "2f16bb33e4befaefbc52b1a144465da3be25b33c9a30999cdfe64d427bab9a77"
B1_V2_ANCESTOR_DATASET_SHA256 = "c49af93748864bb863192bb46fea19f314a3107c9455a4d587c2f16835714ed5"
B1_V2_ANCESTOR_MANIFEST_DIGEST = "e9efe9d145e8203d0e1b63cbcaaa303bfed73f4ee4fc7337fd96eef6ab844ffe"
B3_SCHEMA_VERSION = "method-validation-b3.v1"
B3_MANIFEST_DIGEST = "bf7cdb15a669e82c5d1002140532b3b43122deab6151e54657d3efcdb80d66c5"

B1_DATASET_PATH = "exports/method-validation/b1-v3/canonical-trade-dataset.jsonl"
B1_MANIFEST_PATH = "exports/method-validation/b1-v3/manifest.json"
B1_EPISODE_RESOLUTION_PATH = f"canonical/adjudications/method-validation/{EPISODE_RESOLUTION_NAME}"
B3_MANIFEST_PATH = "exports/method-validation/b3/manifest.json"
STEP0_RESULTS_PATH = "exports/strategy-discovery/step-0/step-0-results.jsonl"
STEP0_MANIFEST_PATH = "exports/strategy-discovery/step-0/corpus-manifest.json"

ACTIVE_STRATEGY_IDS = (
    "stg-vegas-tunnel",
    "stg-po3",
    "stg-ict-liquidity",
    "stg-triple-tap",
)
KNOWN_OUTCOMES = ("win", "loss", "breakeven")
KNOWN_ORIGINS = ("backtest", "live")
OUTPUT_NAMES = (
    "README.md",
    "manifest.json",
    "strategy-performance.json",
    "strategy-performance.md",
)
ROUND_DIGITS = 6
MISSINGNESS_FIELDS = (
    "event_date",
    "time_entry",
    "instrument",
    "sample_origin",
    "outcome",
    "canonical_realized_r",
    "strategy_id",
    "source_path",
    "step0_record_id",
)

# This is intentionally the only B1-to-B3 rule field equivalence used by the
# diagnostic.  The B1 schema has no setup_tags, strategy.variant, or target
# field.  In particular, take_profit_price is not silently treated as the B3
# plan.target_price operand.
B1_EXACT_FIELD_MAP = {"plan.stop_loss_price": "stop_loss_price"}


class B2Error(ValueError):
    """Raised when B2 inputs or generated artifacts fail closed."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise B2Error(f"unable to hash artifact: {path}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise B2Error(f"invalid JSON artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise B2Error(f"JSON artifact is not an object: {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise B2Error(f"unable to read JSONL artifact: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise B2Error(f"invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise B2Error(f"JSONL row is not an object at {path}:{line_number}")
        rows.append(row)
    return rows


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text.rstrip() + "\n")


def _round(value: float | int | None) -> float | None:
    if value is None:
        return None
    result = round(float(value), ROUND_DIGITS)
    if not math.isfinite(result):
        raise B2Error("non-finite metric generated")
    if result == 0:
        return 0.0
    return result


def _ratio(numerator: int | float, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return _round(float(numerator) / denominator)


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return _round(ordered[lower])
    weight = position - lower
    return _round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def _date_value(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise B2Error(f"{label} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise B2Error(f"{label} is not an ISO date: {value}") from exc


def _time_sort_key(value: Any) -> tuple[int, str]:
    if not isinstance(value, str):
        return (1, "")
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        return (0, value)
    return (1, value)


def _chronological_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _date_value(row["event_date"], "event_date"),
        _time_sort_key(row.get("time_entry")),
        str(row["source_path"]),
        str(row["step0_record_id"]),
    )


def _ordered(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(rows, key=_chronological_key)


def _metric_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [float(row["canonical_realized_r"]) for row in rows]
    n = len(values)
    if n == 0:
        return {
            "n": 0,
            "win_count": 0,
            "loss_count": 0,
            "breakeven_count": 0,
            "win_rate": None,
            "loss_rate": None,
            "breakeven_rate": None,
            "total_r": None,
            "mean_r": None,
            "median_r": None,
            "min_r": None,
            "max_r": None,
            "standard_deviation_r": None,
            "q25_r": None,
            "q75_r": None,
            "expectancy_per_trade_r": None,
            "gross_positive_r": None,
            "gross_negative_r": None,
            "profit_factor_r": None,
            "profit_factor_status": "no_sample",
        }

    outcome_counts = Counter(str(row["outcome"]) for row in rows)
    total = sum(values)
    mean = total / n
    gross_positive = sum(value for value in values if value > 0)
    gross_negative = sum(value for value in values if value < 0)
    profit_factor = (
        gross_positive / abs(gross_negative) if gross_negative < 0 else None
    )
    return {
        "n": n,
        "win_count": outcome_counts["win"],
        "loss_count": outcome_counts["loss"],
        "breakeven_count": outcome_counts["breakeven"],
        "win_rate": _ratio(outcome_counts["win"], n),
        "loss_rate": _ratio(outcome_counts["loss"], n),
        "breakeven_rate": _ratio(outcome_counts["breakeven"], n),
        "total_r": _round(total),
        "mean_r": _round(mean),
        "median_r": _quantile(values, 0.5),
        "min_r": _round(min(values)),
        "max_r": _round(max(values)),
        "standard_deviation_r": _round(
            math.sqrt(sum((value - mean) ** 2 for value in values) / n)
        ),
        "q25_r": _quantile(values, 0.25),
        "q75_r": _quantile(values, 0.75),
        "expectancy_per_trade_r": _round(total / n),
        "gross_positive_r": _round(gross_positive),
        "gross_negative_r": _round(gross_negative),
        "profit_factor_r": _round(profit_factor),
        "profit_factor_status": "finite" if profit_factor is not None else "no_negative_r",
    }


def _streaks(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    max_wins = 0
    max_losses = 0
    wins = 0
    losses = 0
    for row in _ordered(rows):
        outcome = row["outcome"]
        if outcome == "win":
            wins += 1
            losses = 0
            max_wins = max(max_wins, wins)
        elif outcome == "loss":
            losses += 1
            wins = 0
            max_losses = max(max_losses, losses)
        else:
            wins = 0
            losses = 0
    return {
        "max_consecutive_wins": max_wins,
        "max_consecutive_losses": max_losses,
    }


def _curve_and_drawdown(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cumulative = 0.0
    running_peak = 0.0
    minimum_drawdown = 0.0
    curve: list[dict[str, Any]] = []
    for row in _ordered(rows):
        realized_r = float(row["canonical_realized_r"])
        cumulative += realized_r
        running_peak = max(running_peak, cumulative)
        drawdown = cumulative - running_peak
        minimum_drawdown = min(minimum_drawdown, drawdown)
        curve.append(
            {
                "trade_ref": row["trade_ref"],
                "source_path": row["source_path"],
                "event_date": row["event_date"],
                "instrument": row["instrument"],
                "sample_origin": row["sample_origin"],
                "outcome": row["outcome"],
                "realized_r": _round(realized_r),
                "cumulative_r": _round(cumulative),
                "running_peak_r": _round(running_peak),
                "drawdown_r": _round(drawdown),
            }
        )
    return curve, {
        "final_cumulative_r": _round(cumulative) if curve else None,
        "peak_cumulative_r": _round(running_peak) if curve else None,
        "max_cumulative_r_drawdown": _round(abs(minimum_drawdown)) if curve else None,
    }


def _segment(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summary = _metric_summary(rows)
    curve, drawdown = _curve_and_drawdown(rows)
    summary.update(drawdown)
    summary.update(_streaks(rows))
    return summary


def _temporal(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = _ordered(rows)
    if not ordered:
        return {
            "first_trade_date": None,
            "last_trade_date": None,
            "span_days": None,
        }
    first = _date_value(ordered[0]["event_date"], "event_date")
    last = _date_value(ordered[-1]["event_date"], "event_date")
    return {
        "first_trade_date": first.isoformat(),
        "last_trade_date": last.isoformat(),
        "span_days": (last - first).days,
    }


def _market_concentration(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(row["instrument"]) for row in rows)
    n = len(rows)
    instruments = [
        {
            "instrument": instrument,
            "count": count,
            "share": _ratio(count, n),
        }
        for instrument, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "by_instrument": instruments,
        "top_1_share": instruments[0]["share"] if instruments else None,
        "top_3_share": _round(sum(item["share"] or 0 for item in instruments[:3])),
    }


def _positive_concentration(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    positives = [
        row for row in rows if float(row["canonical_realized_r"]) > 0
    ]
    positives.sort(
        key=lambda row: (
            -float(row["canonical_realized_r"]),
            _chronological_key(row),
        )
    )
    total_positive = sum(float(row["canonical_realized_r"]) for row in positives)
    top = []
    for row in positives[:3]:
        value = float(row["canonical_realized_r"])
        top.append(
            {
                "trade_ref": row["trade_ref"],
                "event_date": row["event_date"],
                "instrument": row["instrument"],
                "realized_r": _round(value),
                "positive_r_contribution": _round(value / total_positive)
                if total_positive
                else None,
            }
        )
    return {
        "positive_trade_count": len(positives),
        "top_positive_trades": top,
        "top_1_positive_r_contribution": top[0]["positive_r_contribution"] if top else None,
        "top_3_positive_r_contribution": _round(
            sum(item["positive_r_contribution"] or 0 for item in top)
        ),
    }


def _missingness(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Report field absence without treating valid missing time_entry as invalid."""

    n = len(rows)
    result: dict[str, Any] = {}
    for field in MISSINGNESS_FIELDS:
        missing_count = sum(
            1
            for row in rows
            if field not in row or row.get(field) is None or row.get(field) == ""
        )
        result[field] = {
            "missing_count": missing_count,
            "missing_rate": _ratio(missing_count, n),
        }
    return result


def _confidence(n: int, live_n: int) -> dict[str, Any]:
    if n < 10 or live_n == 0:
        classification = "insufficient"
    elif n < 30 or live_n < 5:
        classification = "low"
    elif n < 100 or live_n < 15:
        classification = "moderate"
    else:
        classification = "stronger"
    return {
        "classification": classification,
        "sample_size": n,
        "live_count": live_n,
        "live_representation": _ratio(live_n, n),
        "basis": "B2 analytical confidence only; not strategy quality or statistical significance.",
    }


def _assessment(overall: Mapping[str, Any], segments: Mapping[str, Mapping[str, Any]], confidence: str) -> str:
    if confidence == "insufficient":
        return "insufficient_evidence"
    mean = overall["mean_r"]
    median = overall["median_r"]
    segment_means = [
        segment["mean_r"]
        for name, segment in segments.items()
        if name in {"live", "backtest"} and segment["n"]
    ]
    if mean is not None and median is not None and mean > 0 and median > 0 and all(value > 0 for value in segment_means):
        return "descriptively_positive"
    if mean is not None and median is not None and mean < 0 and median < 0 and all(value < 0 for value in segment_means):
        return "descriptively_negative"
    return "mixed"


def _rule_observability(
    root: Path,
    version_id: str,
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    try:
        rules = load_effective_rules(root, version_id)
    except (OSError, ValueError) as exc:
        raise B2Error(f"unable to load B3 effective rules: {version_id}") from exc
    result: list[dict[str, Any]] = []
    for rule in rules:
        operands = rule.get("operands")
        field = operands.get("field") if isinstance(operands, Mapping) else None
        b1_field = B1_EXACT_FIELD_MAP.get(str(field)) if field else None
        if rule.get("operator") == "exists" and b1_field:
            observed = [row for row in rows if row.get(b1_field) is not None]
            satisfied = len(observed)
            not_satisfied = len(rows) - satisfied
            unknown = 0
            status = "machine_observable"
            reason = f"exact B1 field mapping: {field} -> {b1_field}"
        else:
            satisfied = 0
            not_satisfied = 0
            unknown = len(rows)
            observed = []
            status = "NOT_MACHINE_OBSERVABLE"
            reason = "B1 does not carry the exact B3 operand/evidence field; no field inference is performed."
        result.append(
            {
                "rule_id": rule.get("rule_id"),
                "required": bool(rule.get("required")),
                "claim_class": rule.get("claim_class"),
                "operator": rule.get("operator"),
                "operand_field": field,
                "status": status,
                "observable_rows": len(observed),
                "satisfied": satisfied,
                "not_satisfied": not_satisfied,
                "unknown": unknown,
                "reason": reason,
            }
        )
    return result


def _validate_input_pins(
    root: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    b1_manifest = _read_json(root / B1_MANIFEST_PATH)
    b1_errors = validate_b1_v3_manifest(
        b1_manifest,
        project_root=root,
        output_dir=root / DEFAULT_B1_V3_OUTPUT,
        adjudication_dir=root / DEFAULT_ADJUDICATION_OUTPUT,
    )
    if b1_errors:
        raise B2Error("B1 v3 manifest failed validation: " + "; ".join(b1_errors))
    if b1_manifest.get("schema_version") != B1_V3_SCHEMA_VERSION:
        raise B2Error("B1 v3 schema/version pin mismatch")
    if b1_manifest.get("dataset_version") != B1_DATASET_VERSION:
        raise B2Error("B1 v3 dataset version pin mismatch")
    if b1_manifest.get("manifest_digest") != B1_MANIFEST_DIGEST:
        raise B2Error("B1 v3 manifest digest pin mismatch")
    if _sha256_file(root / B1_DATASET_PATH) != B1_DATASET_SHA256:
        raise B2Error("B1 v3 dataset file SHA-256 does not match its immutable pin")
    if _sha256_file(root / B1_MANIFEST_PATH) != B1_MANIFEST_FILE_SHA256:
        raise B2Error("B1 v3 manifest file SHA-256 does not match its immutable pin")
    resolution_entry = (b1_manifest.get("artifacts") or {}).get("episode_resolution")
    if not isinstance(resolution_entry, Mapping) or resolution_entry.get("sha256") != B1_EPISODE_RESOLUTION_SHA256:
        raise B2Error("B1 v3 episode-resolution artifact pin mismatch")
    if _sha256_file(root / B1_EPISODE_RESOLUTION_PATH) != B1_EPISODE_RESOLUTION_SHA256:
        raise B2Error("B1 v3 episode-resolution artifact bytes changed")

    b3_manifest = _read_json(root / B3_MANIFEST_PATH)
    if b3_manifest.get("schema_version") != B3_SCHEMA_VERSION:
        raise B2Error("B3 schema/version pin mismatch")
    if b3_manifest.get("manifest_digest") != B3_MANIFEST_DIGEST:
        raise B2Error("B3 manifest digest pin mismatch")
    unsigned_b3 = dict(b3_manifest)
    unsigned_b3.pop("manifest_digest", None)
    if _sha256_bytes(_canonical_bytes(unsigned_b3)) != B3_MANIFEST_DIGEST:
        raise B2Error("B3 manifest digest is not internally valid")
    b3_errors = validate_b3_manifest(b3_manifest)
    if b3_errors:
        raise B2Error("B3 manifest failed validation: " + "; ".join(b3_errors))
    if b3_manifest.get("active_strategy_ids") != list(ACTIVE_STRATEGY_IDS):
        raise B2Error("B3 active strategy order/coverage does not match B2 contract")

    strategy_versions = b3_manifest.get("strategy_versions")
    if not isinstance(strategy_versions, list):
        raise B2Error("B3 strategy_versions is missing")
    by_strategy = {item.get("strategy_id"): item for item in strategy_versions if isinstance(item, Mapping)}
    if set(by_strategy) != set(ACTIVE_STRATEGY_IDS):
        raise B2Error("B3 strategy_versions coverage does not match B2 contract")
    for strategy_id in ACTIVE_STRATEGY_IDS:
        item = by_strategy[strategy_id]
        if item.get("freeze_status") != "frozen" or item.get("blocking_unresolved_count") != 0:
            raise B2Error(f"B3 strategy is not frozen/non-blocking: {strategy_id}")

    rows = _read_jsonl(root / B1_DATASET_PATH)
    if len(rows) != 255:
        raise B2Error(f"B1 v3 dataset row count changed: {len(rows)}")
    if len({row.get("trade_ref") for row in rows}) != len(rows):
        raise B2Error("B1 v3 dataset contains duplicate trade_ref values")
    countable = [row for row in rows if row.get("performance_counting_eligibility") is True]
    countable_ids = [row.get("canonical_episode_id") for row in countable]
    if any(
        not isinstance(row.get("canonical_episode_id"), str)
        or row.get("episode_role") != "primary"
        or row.get("canonical_representative_trade_ref") != row.get("trade_ref")
        or row.get("analysis_eligibility") != "eligible"
        for row in countable
    ):
        raise B2Error("B1 v3 countable row is not a canonical eligible representative")
    if len(countable_ids) != len(set(countable_ids)):
        raise B2Error("B1 v3 contains duplicate performance-countable canonical episode IDs")
    if len(countable_ids) != b1_manifest.get("counts", {}).get("performance_countable_episode_count"):
        raise B2Error("B1 v3 countable episode count does not match its manifest")

    v2_rows = _read_jsonl(root / B1_V2_DATASET_PATH)
    v2_by_trade_ref = {row.get("trade_ref"): row for row in v2_rows}
    for row in rows:
        ancestor = v2_by_trade_ref.get(row.get("trade_ref"))
        if ancestor is None:
            raise B2Error(f"B1 v3 source row has no B1 v2 ancestor: {row.get('trade_ref')}")
        for field in COMPATIBILITY_FIELDS:
            if row.get(field) != ancestor.get(field):
                raise B2Error(f"B1 v2/v3 compatibility drift: {row.get('trade_ref')}:{field}")
    return b3_manifest, b1_manifest, rows, [by_strategy[strategy_id] for strategy_id in ACTIVE_STRATEGY_IDS]


def _select_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len({row.get("trade_ref") for row in rows}) != len(rows):
        raise B2Error("B1 v3 dataset contains duplicate trade_ref values")
    countable = [row for row in rows if row.get("performance_counting_eligibility") is True]
    countable_ids = [row.get("canonical_episode_id") for row in countable]
    if len(countable_ids) != len(set(countable_ids)):
        raise B2Error("B1 v3 contains duplicate performance-countable canonical episode IDs")
    if any(
        not isinstance(row.get("canonical_episode_id"), str)
        or row.get("episode_role") != "primary"
        or row.get("canonical_representative_trade_ref") != row.get("trade_ref")
        for row in countable
    ):
        raise B2Error("B1 v3 countable row is not a canonical representative")
    eligible = [row for row in rows if row.get("analysis_eligibility") == "eligible"]
    active_eligible = [
        row
        for row in eligible
        if row.get("strategy_id") in ACTIVE_STRATEGY_IDS and row.get("strategy_status") == "active"
    ]
    selected = [
        row
        for row in active_eligible
        if row.get("performance_counting_eligibility") is True
    ]
    excluded_eligible = [row for row in eligible if row not in selected]
    identity_excluded = [row for row in active_eligible if row not in selected]
    for row in selected:
        strategy_id = row.get("strategy_id")
        if row.get("strategy_status") != "active":
            raise B2Error(f"selected B2 row is not active: {strategy_id}")
        if row.get("sample_origin") not in KNOWN_ORIGINS:
            raise B2Error(f"selected B2 row has unknown sample_origin: {row.get('trade_ref')}")
        if row.get("outcome") not in KNOWN_OUTCOMES:
            raise B2Error(f"selected B2 row has unsupported outcome: {row.get('trade_ref')}")
        value = row.get("canonical_realized_r")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise B2Error(f"selected B2 row has missing/non-finite canonical_realized_r: {row.get('trade_ref')}")
        _date_value(row.get("event_date"), "event_date")
        if not isinstance(row.get("source_path"), str) or not isinstance(row.get("step0_record_id"), str):
            raise B2Error(f"selected B2 row lacks stable chronology fields: {row.get('trade_ref')}")
        if not isinstance(row.get("instrument"), str) or not row["instrument"]:
            raise B2Error(f"selected B2 row lacks instrument: {row.get('trade_ref')}")
    excluded_counts = Counter(str(row.get("strategy_id")) for row in excluded_eligible)
    excluded_status_counts = Counter(str(row.get("strategy_status")) for row in excluded_eligible)
    ineligible_counts = Counter(str(row.get("ineligibility_reason")) for row in rows if row.get("analysis_eligibility") == "ineligible")
    identity_excluded_reasons = Counter(str(row.get("performance_exclusion_reason")) for row in identity_excluded)
    sample = {
        "b1_total_rows": len(rows),
        "b1_eligible_rows": len(eligible),
        "b1_ineligible_rows": len(rows) - len(eligible),
        "b1_v3_countable_episode_count": len(countable_ids),
        "b1_v3_duplicate_representation_rows": sum(
            1 for row in rows if row.get("performance_exclusion_reason") == "duplicate_episode_representation"
        ),
        "b1_v3_unresolved_excluded_rows": sum(
            1 for row in rows if row.get("episode_resolution") == "unresolved"
        ),
        "b2_active_eligible_source_rows": len(active_eligible),
        "b2_analyzed_rows": len(selected),
        "b2_analyzed_unique_episodes": len({row.get("canonical_episode_id") for row in selected}),
        "b2_analyzed_by_strategy": dict(
            (strategy_id, sum(1 for row in selected if row.get("strategy_id") == strategy_id))
            for strategy_id in ACTIVE_STRATEGY_IDS
        ),
        "eligible_excluded_from_b2_rows": len(excluded_eligible),
        "eligible_excluded_from_b2_by_strategy": dict(sorted(excluded_counts.items())),
        "eligible_excluded_from_b2_by_status": dict(sorted(excluded_status_counts.items())),
        "active_identity_excluded_rows": len(identity_excluded),
        "active_identity_excluded_by_reason": dict(sorted(identity_excluded_reasons.items())),
        "b1_ineligible_by_reason": dict(sorted(ineligible_counts.items())),
        "missingness": _missingness(selected),
    }
    return [dict(row) for row in selected], sample


def _delta(live: Mapping[str, Any], backtest: Mapping[str, Any]) -> dict[str, Any]:
    fields = ("win_rate", "mean_r", "median_r", "expectancy_per_trade_r", "total_r")
    return {
        field: _round(float(live[field]) - float(backtest[field]))
        if live.get("n") and backtest.get("n") and live.get(field) is not None and backtest.get(field) is not None
        else None
        for field in fields
    }


def _strategy_analysis(
    root: Path,
    strategy_id: str,
    version_entry: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    active_union_count: int,
) -> dict[str, Any]:
    overall_rows = [row for row in rows if row.get("strategy_id") == strategy_id]
    live_rows = [row for row in overall_rows if row.get("sample_origin") == "live"]
    backtest_rows = [row for row in overall_rows if row.get("sample_origin") == "backtest"]
    overall = _segment(overall_rows)
    live = _segment(live_rows)
    backtest = _segment(backtest_rows)
    confidence = _confidence(len(overall_rows), len(live_rows))
    return {
        "strategy_id": strategy_id,
        "display_name": version_entry.get("display_name"),
        "rule_version": version_entry.get("rule_version"),
        "rule_definition_sha256": version_entry.get("rule_definition_sha256"),
        "version_file_sha256": version_entry.get("version_file_sha256"),
        "sample": {
            "n": len(overall_rows),
            "share_of_active_strategy_sample": _ratio(len(overall_rows), active_union_count),
            "backtest_count": len(backtest_rows),
            "live_count": len(live_rows),
        },
        "segments": {"overall": overall, "live": live, "backtest": backtest},
        "missingness": _missingness(overall_rows),
        "live_backtest_delta": _delta(live, backtest),
        "temporal": _temporal(overall_rows),
        "market_concentration": _market_concentration(overall_rows),
        "positive_r_concentration": _positive_concentration(overall_rows),
        "rule_observability": _rule_observability(root, str(version_entry["rule_version"]), overall_rows),
        "confidence": confidence,
        "assessment": _assessment(overall, {"live": live, "backtest": backtest}, confidence["classification"]),
        "chronological_cumulative_r": _curve_and_drawdown(overall_rows)[0],
    }


METRIC_DEFINITIONS = {
    "canonical_r": "B1 canonical_realized_r only; B2 never re-derives trade R.",
    "win_rate": "win_count / n, where outcomes are B1 canonical outcome values.",
    "loss_rate": "loss_count / n.",
    "breakeven_rate": "breakeven_count / n.",
    "mean_r": "sum(canonical_realized_r) / n.",
    "median_r_q25_q75": "Linear interpolation over sorted canonical_realized_r values (type-7 equivalent).",
    "standard_deviation_r": "Population standard deviation: sqrt(sum((R - mean_R)^2) / n).",
    "expectancy_per_trade_r": "sum(canonical_realized_r) / n; equal to mean_R for this descriptive sample.",
    "gross_positive_r": "sum(R for R > 0), signed positive total.",
    "gross_negative_r": "sum(R for R < 0), signed negative total.",
    "profit_factor_r": "gross_positive_r / abs(gross_negative_r); null when no negative R, with status no_negative_r.",
    "drawdown": "Initial peak is 0R; cumulative_R=cumsum(R); running_peak=max(previous peak,cumulative_R); drawdown=cumulative_R-running_peak; max drawdown is positive magnitude.",
    "streaks": "Consecutive B1 canonical win/loss outcomes in chronological order; breakeven resets both streaks.",
    "chronology": "event_date, valid time_entry with missing/invalid time last, source_path, step0_record_id.",
    "concentration": "Descriptive instrument counts/shares and top positive-R contribution; no sample is removed.",
    "missingness": "Per-field missing count and missing_count / selected-row n; missing time_entry is retained and sorted last.",
}


CONFIDENCE_POLICY = {
    "name": "b2_analytical_confidence_v1",
    "not_strategy_quality": True,
    "not_statistical_significance": True,
    "insufficient": "n < 10 or live_n == 0",
    "low": "n >= 10 and (n < 30 or live_n < 5)",
    "moderate": "n >= 30 and live_n >= 5 and (n < 100 or live_n < 15)",
    "stronger": "n >= 100 and live_n >= 15",
}


def _render_number(value: Any, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.3f}{suffix}"
    return f"{value}{suffix}"


def _render_pct(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value) * 100:.1f}%"


def _render_segment_table(strategy: Mapping[str, Any]) -> list[str]:
    lines = [
        "| Segment | n | Win rate | Mean R | Median R | Expectancy R | Total R |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("overall", "live", "backtest"):
        item = strategy["segments"][name]
        lines.append(
            f"| {name} | {item['n']} | {_render_pct(item['win_rate'])} | "
            f"{_render_number(item['mean_r'])} | {_render_number(item['median_r'])} | "
            f"{_render_number(item['expectancy_per_trade_r'])} | {_render_number(item['total_r'])} |"
        )
    return lines


def _render_rule_observability(strategy: Mapping[str, Any]) -> list[str]:
    lines = [
        "| Rule | Required | Status | Observable | Satisfied | Not satisfied | Unknown |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for rule in strategy["rule_observability"]:
        lines.append(
            f"| `{rule['rule_id']}` | {str(rule['required']).lower()} | {rule['status']} | "
            f"{rule['observable_rows']} | {rule['satisfied']} | {rule['not_satisfied']} | {rule['unknown']} |"
        )
    return lines


def _render_strategy_section(strategy: Mapping[str, Any]) -> list[str]:
    overall = strategy["segments"]["overall"]
    live = strategy["segments"]["live"]
    backtest = strategy["segments"]["backtest"]
    distribution = overall
    drawdown = overall
    concentration = strategy["market_concentration"]
    positive = strategy["positive_r_concentration"]
    temporal = strategy["temporal"]
    confidence = strategy["confidence"]
    delta = strategy["live_backtest_delta"]
    missingness = strategy["missingness"]
    missingness_summary = ", ".join(
        f"{field}={item['missing_count']} ({_render_pct(item['missing_rate'])})"
        for field, item in missingness.items()
        if item["missing_count"]
    ) or "none"
    lines = [
        f"## {strategy['display_name']} — {strategy['rule_version']}",
        "",
        "### Sample",
        "",
        f"- n={strategy['sample']['n']} ({_render_pct(strategy['sample']['share_of_active_strategy_sample'])} of the analyzed active-strategy sample); live={strategy['sample']['live_count']}, backtest={strategy['sample']['backtest_count']}.",
        f"- Date span: {temporal['first_trade_date']} to {temporal['last_trade_date']} ({temporal['span_days']} calendar days).",
        f"- Missingness across the selected rows: {missingness_summary}.",
        "",
        "### Overall performance / live / backtest",
        "",
    ]
    lines.extend(_render_segment_table(strategy))
    lines.extend(
        [
            "",
            "Live minus backtest delta (descriptive, not a significance test): "
            + ", ".join(
                f"{field}={_render_pct(value) if field == 'win_rate' else _render_number(value)}"
                for field, value in delta.items()
            )
            + ".",
            "",
            "### Distribution",
            "",
            f"- min={_render_number(distribution['min_r'])}, Q25={_render_number(distribution['q25_r'])}, median={_render_number(distribution['median_r'])}, Q75={_render_number(distribution['q75_r'])}, max={_render_number(distribution['max_r'])}.",
            f"- Standard deviation={_render_number(distribution['standard_deviation_r'])}; gross positive R={_render_number(distribution['gross_positive_r'])}; gross negative R={_render_number(distribution['gross_negative_r'])}; profit factor={_render_number(distribution['profit_factor_r'])} ({distribution['profit_factor_status']}).",
            "",
            "### Drawdown / streak behavior",
            "",
            f"- Peak cumulative R={_render_number(drawdown['peak_cumulative_r'])}; final cumulative R={_render_number(drawdown['final_cumulative_r'])}; max cumulative-R drawdown={_render_number(drawdown['max_cumulative_r_drawdown'])}.",
            f"- Max consecutive wins={drawdown['max_consecutive_wins']}; max consecutive losses={drawdown['max_consecutive_losses']}. Chronological curve is machine-readable in `strategy-performance.json`.",
            "",
            "### Concentration / outliers",
            "",
            f"- Top instrument share={_render_pct(concentration['top_1_share'])}; top-three instrument share={_render_pct(concentration['top_3_share'])}.",
            f"- Top-one positive-R contribution={_render_pct(positive['top_1_positive_r_contribution'])}; top-three contribution={_render_pct(positive['top_3_positive_r_contribution'])}. Positive outliers are retained, not removed.",
            "",
            "### Rule observability",
            "",
            "B3 compliance is diagnostic only; NOT MACHINE-OBSERVABLE rows remain in the B2 sample and are not retrospectively excluded.",
        ]
    )
    lines.extend(_render_rule_observability(strategy))
    lines.extend(
        [
            "",
            "### Confidence",
            "",
            f"- B2 analytical confidence: **{confidence['classification']}** (n={confidence['sample_size']}, live_n={confidence['live_count']}). This is not strategy quality or statistical significance.",
            "",
            "### Limitations",
            "",
            "- B1 eligibility and canonical realized R are authoritative; B2 does not recompute R or remove records because rule evidence is unavailable.",
            "- B1 has no machine-readable chart/setup tags for most B3 operands, so rule observability is conservative.",
            "- Descriptive live/backtest deltas do not establish statistical significance or causality.",
            "",
            "### B2 assessment",
            "",
            f"`{strategy['assessment']}`. This label is a transparent descriptive assessment only and cannot mutate strategy lifecycle, taxonomy, or B3 rules.",
            "",
        ]
    )
    return lines


def _render_report(
    *,
    analysis: Mapping[str, Any],
) -> str:
    strategies = analysis["strategies"]
    lines = [
        "# Method Validation B2 — Strategy Performance",
        "",
        "## Executive Summary",
        "",
        "This is a descriptive measurement baseline over the immutable B1 eligible rows that belong to the four B3 frozen active strategies. It does not rank strategies, alter rules, or claim statistical significance.",
        "",
        f"Analyzed union: **{analysis['universe']['b2_analyzed_rows']}** canonical episodes from B1 v3 source rows={analysis['universe']['b1_total_rows']}; {analysis['universe']['b2_active_eligible_source_rows']} active-strategy eligible source rows were eligible before episode canonicalization.",
        "",
        "## Observation Identity",
        "",
        f"- B1 v3 retains **{analysis['universe']['b1_total_rows']}** source rows and exposes **{analysis['universe']['b1_v3_countable_episode_count']}** countable unique episodes across all strategies.",
        f"- The four B3 active strategies contribute **{analysis['universe']['b2_analyzed_unique_episodes']}** unique canonical performance episodes from **{analysis['universe']['b2_active_eligible_source_rows']}** active-strategy eligible source rows.",
        f"- Duplicate representations excluded from counting: **{analysis['universe']['b1_v3_duplicate_representation_rows']}** source rows; unresolved identity rows excluded: **{analysis['universe']['b1_v3_unresolved_excluded_rows']}**.",
        "- B2 counts one observation per `canonical_episode_id`; a duplicate canonical episode ID is a hard validation error.",
        "- Any pre-v3 source-record performance draft is invalidated pending this episode-aware repin; its counts are not reused.",
        "",
        "| Strategy | n | Live | Backtest | Win rate | Mean R | Median R | Expectancy R | PF | Max DD | Confidence | Assessment |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for strategy in strategies:
        overall = strategy["segments"]["overall"]
        lines.append(
            f"| {strategy['display_name']} | {strategy['sample']['n']} | {strategy['sample']['live_count']} | {strategy['sample']['backtest_count']} | "
            f"{_render_pct(overall['win_rate'])} | {_render_number(overall['mean_r'])} | {_render_number(overall['median_r'])} | "
            f"{_render_number(overall['expectancy_per_trade_r'])} | {_render_number(overall['profit_factor_r'])} | "
            f"{_render_number(overall['max_cumulative_r_drawdown'])} | {strategy['confidence']['classification']} | {strategy['assessment']} |"
        )
    lines.extend(
        [
            "",
            "## Direct Answers to the B2 Questions",
            "",
        ]
    )
    for strategy in strategies:
        overall = strategy["segments"]["overall"]
        live = strategy["segments"]["live"]
        backtest = strategy["segments"]["backtest"]
        if strategy["confidence"]["classification"] == "insufficient":
            statement = f"{strategy['display_name']}: n={strategy['sample']['n']} is insufficient for a reliable strategy-level conclusion."
        else:
            statement = (
                f"{strategy['display_name']}: overall expectancy {_render_number(overall['expectancy_per_trade_r'])}; "
                f"live expectancy {_render_number(live['expectancy_per_trade_r'])}; backtest expectancy {_render_number(backtest['expectancy_per_trade_r'])}; "
                f"assessment is {strategy['assessment']} descriptively."
            )
        lines.append(f"- {statement}")
    lines.extend(
        [
            "",
            "The evidence-supported cross-strategy conclusion is descriptive only: Vegas and ICT have larger samples than PO3 and Triple Tap; PO3 and Triple Tap remain insufficient under the declared confidence policy. No strategy is ranked and no rule/lifecycle action follows from these results.",
            "",
            "## Metric Definitions",
            "",
        ]
    )
    for key, definition in METRIC_DEFINITIONS.items():
        lines.append(f"- **{key}**: {definition}")
    lines.extend(
        [
            "",
            "## Segmentation and Uncertainty Policy",
            "",
            "- Every strategy reports overall, live, and backtest segments. Live minus backtest is a descriptive delta only.",
            "- Confidence is `insufficient` for n < 10 or live_n = 0; `low` for n >= 10 with n < 30 or live_n < 5; `moderate` for n >= 30 and live_n >= 5 with n < 100 or live_n < 15; otherwise `stronger`.",
            "- This is B2 analytical confidence, not strategy quality and not statistical significance. No bootstrap or random procedure is used.",
            "",
            "## Protected Boundaries",
            "",
            "B2 consumes B1 `canonical_realized_r` and B1 eligibility/sample-origin fields plus B3 frozen rule metadata. It does not read legacy performance dashboards, mutate trades/B1/B3/Step-0/adjudications, generate synthetic trades, infer PnL, or perform optimization.",
            "",
        ]
    )
    for strategy in strategies:
        lines.extend(_render_strategy_section(strategy))
    return "\n".join(lines)


def _render_readme(*, manifest: Mapping[str, Any], analysis: Mapping[str, Any]) -> str:
    lines = [
        "# Method Validation — B2 Performance / Method Validation",
        "",
        "This is a deterministic downstream measurement artifact. B1 remains the canonical trade-analysis dataset and B3 remains the canonical frozen-strategy authority. B2 does not mutate either upstream slice.",
        "",
        "## Pins",
        "",
        f"- `schema_version`: `{B2_SCHEMA_VERSION}`",
        f"- `mode`: `{manifest['mode']}`",
        f"- `baseline_sha`: `{manifest['baseline_sha']}`",
        f"- `analysis_date`: `{manifest['analysis_date']}`",
        f"- B1 dataset: `{B1_DATASET_VERSION}` / `{B1_DATASET_SHA256}`",
        f"- B1 v3 manifest digest: `{B1_MANIFEST_DIGEST}`",
        f"- Episode resolution: `{B1_EPISODE_RESOLUTION_PATH}` / `{B1_EPISODE_RESOLUTION_SHA256}`",
        f"- B3 manifest digest: `{B3_MANIFEST_DIGEST}`",
        "",
        "## Universe",
        "",
        f"- B1 v3 source rows: **{analysis['universe']['b1_total_rows']}**; B1 eligible source rows: **{analysis['universe']['b1_eligible_rows']}**; B1 v3 countable unique episodes: **{analysis['universe']['b1_v3_countable_episode_count']}**.",
        f"- B2 active-strategy eligible source rows: **{analysis['universe']['b2_active_eligible_source_rows']}**; B2 analyzed unique episodes: **{analysis['universe']['b2_analyzed_unique_episodes']}**.",
        f"- Duplicate representations excluded: **{analysis['universe']['b1_v3_duplicate_representation_rows']}**; unresolved rows excluded: **{analysis['universe']['b1_v3_unresolved_excluded_rows']}**.",
        "- Pre-v3 source-record performance counts are invalidated; this report uses only the episode-aware v3 boundary.",
        f"- Eligible rows outside the four B3 frozen active strategies: **{analysis['universe']['eligible_excluded_from_b2_rows'] - analysis['universe']['active_identity_excluded_rows']}**; they are excluded by the declared intersection, not cherry-picked.",
        "- B1 ineligible rows remain outside the B2 sample with their original reasons; no row is rewritten.",
        "",
        "## Metrics",
        "",
        "The complete machine-readable result is `strategy-performance.json`; the complete human-readable report is `strategy-performance.md`.",
        "",
    ]
    for key, definition in METRIC_DEFINITIONS.items():
        lines.append(f"- **{key}**: {definition}")
    lines.extend(
        [
            "",
            "## Rule observability",
            "",
            "B3 rule compliance is a non-mutating diagnostic. B1 lacks most chart/setup operands, so those rows are `NOT_MACHINE_OBSERVABLE` / unknown rather than failed or dropped. B1 eligibility remains authoritative.",
            "",
            "## Anti-hindsight guarantees",
            "",
            "- `optimization_performed: false`; no parameter sweep, ranking-driven mutation, threshold tuning, setup selection, or hindsight rule change.",
            "- No synthetic/future trades and no inferred PnL outside B1 canonical fields.",
            "- B1, B3, Step-0, trades, adjudications, registry lifecycle/taxonomy, and frozen rule definitions are immutable inputs.",
            "",
            "## Confidence",
            "",
            "B2 analytical confidence uses sample size plus live representation only. It is not strategy quality or statistical significance; no bootstrap is used.",
            "",
            "## Reproduction",
            "",
            "```text",
            f"python -m trading_os b2-validate --baseline-sha {manifest['baseline_sha']} --analysis-date {manifest['analysis_date']} --output-dir exports/method-validation/b2",
            "python -m pytest tests/test_method_validation_b2_performance.py -q",
            "```",
            "",
            "B2 stops at fresh external review. It does not merge, start Trader DNA, or modify strategy definitions.",
        ]
    )
    return "\n".join(lines)


def _manifest_payload(
    *,
    root: Path,
    output_dir: Path,
    baseline_sha: str,
    analysis_date: str,
    b1_manifest: Mapping[str, Any],
    b3_manifest: Mapping[str, Any],
    analysis: Mapping[str, Any],
) -> dict[str, Any]:
    output_hashes = {
        name: _sha256_file(output_dir / name)
        for name in OUTPUT_NAMES
        if name != "manifest.json"
    }
    immutable_upstream = b3_manifest.get("immutable_upstream")
    if not isinstance(immutable_upstream, Mapping):
        raise B2Error("B3 immutable_upstream pins are missing")
    strategy_versions = {
        strategy["strategy_id"]: {
            "rule_version": strategy["rule_version"],
            "rule_definition_sha256": strategy["rule_definition_sha256"],
            "version_file_sha256": strategy["version_file_sha256"],
        }
        for strategy in analysis["strategies"]
    }
    payload: dict[str, Any] = {
        "schema_version": B2_SCHEMA_VERSION,
        "mode": "method_validation_b2_performance",
        "baseline_sha": baseline_sha,
        "analysis_date": analysis_date,
        "inputs": {
            "b1": {
                "dataset_path": B1_DATASET_PATH,
                "manifest_path": B1_MANIFEST_PATH,
                "dataset_version": B1_DATASET_VERSION,
                "dataset_sha256": B1_DATASET_SHA256,
                "manifest_digest": B1_MANIFEST_DIGEST,
                "dataset_file_sha256": _sha256_file(root / B1_DATASET_PATH),
                "manifest_file_sha256": _sha256_file(root / B1_MANIFEST_PATH),
                "episode_resolution_path": B1_EPISODE_RESOLUTION_PATH,
                "episode_resolution_sha256": B1_EPISODE_RESOLUTION_SHA256,
                "b1_v2_ancestor_pin": dict(b1_manifest["b1_v2_pin"]),
            },
            "b3": {
                "manifest_path": B3_MANIFEST_PATH,
                "schema_version": B3_SCHEMA_VERSION,
                "manifest_digest": B3_MANIFEST_DIGEST,
                "manifest_file_sha256": _sha256_file(root / B3_MANIFEST_PATH),
                "immutable_upstream": dict(immutable_upstream),
            },
            "step0": {
                "results_path": STEP0_RESULTS_PATH,
                "manifest_path": STEP0_MANIFEST_PATH,
                "results_file_sha256": immutable_upstream.get("step0_results"),
                "manifest_file_sha256": immutable_upstream.get("step0_corpus_manifest"),
            },
        },
        "b1_pin": {
            "dataset_version": B1_DATASET_VERSION,
            "dataset_sha256": B1_DATASET_SHA256,
            "manifest_digest": B1_MANIFEST_DIGEST,
            "episode_resolution_path": B1_EPISODE_RESOLUTION_PATH,
            "episode_resolution_sha256": B1_EPISODE_RESOLUTION_SHA256,
        },
        "b3_pin": {
            "schema_version": B3_SCHEMA_VERSION,
            "manifest_digest": B3_MANIFEST_DIGEST,
            "active_strategy_versions": strategy_versions,
        },
        "active_strategy_ids": list(ACTIVE_STRATEGY_IDS),
        "universe": analysis["universe"],
        "observation_identity": {
            "source_rows": analysis["universe"]["b1_total_rows"],
            "b1_v3_countable_unique_episodes": analysis["universe"]["b1_v3_countable_episode_count"],
            "b2_active_eligible_source_rows": analysis["universe"]["b2_active_eligible_source_rows"],
            "b2_analyzed_unique_episodes": analysis["universe"]["b2_analyzed_unique_episodes"],
            "duplicate_representation_rows_excluded": analysis["universe"]["b1_v3_duplicate_representation_rows"],
            "unresolved_rows_excluded": analysis["universe"]["b1_v3_unresolved_excluded_rows"],
        },
        "metric_definitions": METRIC_DEFINITIONS,
        "segmentation_policy": {
            "sample_origin_values": list(KNOWN_ORIGINS),
            "chronological_order": METRIC_DEFINITIONS["chronology"],
            "live_backtest_delta": "live metric minus backtest metric; descriptive only, not a significance test.",
        },
        "confidence_policy": CONFIDENCE_POLICY,
        "output_artifacts": {
            name: {"path": f"exports/method-validation/b2/{name}", "sha256": digest}
            for name, digest in sorted(output_hashes.items())
        },
        "negative_guarantees": {
            "b1_unchanged": True,
            "b3_unchanged": True,
            "step0_unchanged": True,
            "trades_unchanged": True,
            "adjudications_unchanged": True,
            "registry_lifecycle_taxonomy_unchanged": True,
            "frozen_rules_unchanged": True,
            "optimization_performed": False,
            "strategy_ranking_used_to_mutate_rules": False,
            "future_data_generated": False,
            "synthetic_trades_generated": False,
            "inferred_pnl_outside_canonical_fields": False,
            "rule_observability_is_diagnostic_only": True,
        },
        "performance_data_consumed": True,
        "optimization_performed": False,
        "manifest_digest": None,
    }
    unsigned = dict(payload)
    unsigned["manifest_digest"] = None
    payload["manifest_digest"] = _sha256_bytes(_canonical_bytes(unsigned))
    return payload


def validate_b2_manifest(
    manifest: Mapping[str, Any],
    *,
    project_root: Path | None = None,
    output_dir: Path | None = None,
) -> list[str]:
    """Return deterministic validation errors for a B2 manifest."""

    errors: list[str] = []
    required = (
        "schema_version",
        "mode",
        "baseline_sha",
        "analysis_date",
        "b1_pin",
        "b3_pin",
        "active_strategy_ids",
        "universe",
        "observation_identity",
        "metric_definitions",
        "segmentation_policy",
        "confidence_policy",
        "output_artifacts",
        "negative_guarantees",
        "manifest_digest",
    )
    for key in required:
        if key not in manifest:
            errors.append(f"missing:{key}")
    if errors:
        return errors
    if manifest.get("schema_version") != B2_SCHEMA_VERSION:
        errors.append("schema_version")
    if manifest.get("mode") != "method_validation_b2_performance":
        errors.append("mode")
    baseline = manifest.get("baseline_sha")
    if not isinstance(baseline, str) or not re.fullmatch(r"[0-9a-f]{40}", baseline):
        errors.append("baseline_sha")
    try:
        _date_value(manifest.get("analysis_date"), "analysis_date")
    except B2Error:
        errors.append("analysis_date")
    unsigned = dict(manifest)
    supplied_digest = unsigned.pop("manifest_digest", None)
    if not isinstance(supplied_digest, str) or supplied_digest != _sha256_bytes(_canonical_bytes({**unsigned, "manifest_digest": None})):
        errors.append("manifest_digest")
    if manifest.get("active_strategy_ids") != list(ACTIVE_STRATEGY_IDS):
        errors.append("active_strategy_ids")
    b1_pin = manifest.get("b1_pin")
    if not isinstance(b1_pin, Mapping) or dict(b1_pin) != {
        "dataset_version": B1_DATASET_VERSION,
        "dataset_sha256": B1_DATASET_SHA256,
        "manifest_digest": B1_MANIFEST_DIGEST,
        "episode_resolution_path": B1_EPISODE_RESOLUTION_PATH,
        "episode_resolution_sha256": B1_EPISODE_RESOLUTION_SHA256,
    }:
        errors.append("b1_pin")
    b3_pin = manifest.get("b3_pin")
    if not isinstance(b3_pin, Mapping) or b3_pin.get("schema_version") != B3_SCHEMA_VERSION or b3_pin.get("manifest_digest") != B3_MANIFEST_DIGEST:
        errors.append("b3_pin")
    universe = manifest.get("universe")
    if not isinstance(universe, Mapping):
        errors.append("universe")
    else:
        counts = universe.get("b2_analyzed_by_strategy")
        if not isinstance(counts, Mapping) or set(counts) != set(ACTIVE_STRATEGY_IDS):
            errors.append("universe.strategy_counts")
        elif universe.get("b2_analyzed_rows") != sum(int(counts[sid]) for sid in ACTIVE_STRATEGY_IDS):
            errors.append("universe.strategy_count_sum")
        if universe.get("b2_analyzed_rows") != universe.get("b1_eligible_rows", 0) - universe.get("eligible_excluded_from_b2_rows", 0):
            errors.append("universe.eligible_reconciliation")
    identity = manifest.get("observation_identity")
    if not isinstance(identity, Mapping) or any(
        identity.get(key) != universe.get(universe_key)
        for key, universe_key in (
            ("source_rows", "b1_total_rows"),
            ("b1_v3_countable_unique_episodes", "b1_v3_countable_episode_count"),
            ("b2_active_eligible_source_rows", "b2_active_eligible_source_rows"),
            ("b2_analyzed_unique_episodes", "b2_analyzed_unique_episodes"),
            ("duplicate_representation_rows_excluded", "b1_v3_duplicate_representation_rows"),
            ("unresolved_rows_excluded", "b1_v3_unresolved_excluded_rows"),
        )
    ):
        errors.append("observation_identity")
    guarantees = manifest.get("negative_guarantees")
    true_guarantees = (
        "b1_unchanged",
        "b3_unchanged",
        "step0_unchanged",
        "trades_unchanged",
        "adjudications_unchanged",
        "registry_lifecycle_taxonomy_unchanged",
        "frozen_rules_unchanged",
        "rule_observability_is_diagnostic_only",
    )
    false_guarantees = (
        "optimization_performed",
        "strategy_ranking_used_to_mutate_rules",
        "future_data_generated",
        "synthetic_trades_generated",
        "inferred_pnl_outside_canonical_fields",
    )
    if not isinstance(guarantees, Mapping) or any(
        guarantees.get(key) is not True for key in true_guarantees
    ) or any(guarantees.get(key) is not False for key in false_guarantees):
        errors.append("negative_guarantees")
    if manifest.get("optimization_performed") is not False:
        errors.append("optimization_performed")
    output_artifacts = manifest.get("output_artifacts")
    if not isinstance(output_artifacts, Mapping) or set(output_artifacts) != {
        "README.md",
        "strategy-performance.json",
        "strategy-performance.md",
    }:
        errors.append("output_artifacts")

    if project_root is not None:
        root = project_root.resolve()
        try:
            b3_current, b1_current, rows, version_entries = _validate_input_pins(root)
            inputs = manifest.get("inputs")
            if not isinstance(inputs, Mapping):
                errors.append("inputs")
                inputs = {}
            b1_inputs = inputs.get("b1", {})
            b3_inputs = inputs.get("b3", {})
            step0_inputs = inputs.get("step0", {})
            if not isinstance(b1_inputs, Mapping):
                b1_inputs = {}
            if not isinstance(b3_inputs, Mapping):
                b3_inputs = {}
            if not isinstance(step0_inputs, Mapping):
                step0_inputs = {}
            if any(
                b1_inputs.get(key) != expected
                for key, expected in (
                    ("dataset_version", B1_DATASET_VERSION),
                    ("dataset_sha256", B1_DATASET_SHA256),
                    ("manifest_digest", B1_MANIFEST_DIGEST),
                    ("dataset_file_sha256", _sha256_file(root / B1_DATASET_PATH)),
                    ("manifest_file_sha256", B1_MANIFEST_FILE_SHA256),
                    ("episode_resolution_path", B1_EPISODE_RESOLUTION_PATH),
                    ("episode_resolution_sha256", B1_EPISODE_RESOLUTION_SHA256),
                    ("b1_v2_ancestor_pin", b1_current.get("b1_v2_pin")),
                )
            ):
                errors.append("inputs.b1")
            if any(
                b3_inputs.get(key) != expected
                for key, expected in (
                    ("schema_version", B3_SCHEMA_VERSION),
                    ("manifest_digest", b3_current.get("manifest_digest")),
                    ("manifest_file_sha256", _sha256_file(root / B3_MANIFEST_PATH)),
                    ("immutable_upstream", b3_current.get("immutable_upstream")),
                )
            ):
                errors.append("inputs.b3")
            if any(
                step0_inputs.get(key) != expected
                for key, expected in (
                    ("results_file_sha256", b3_current["immutable_upstream"].get("step0_results")),
                    ("manifest_file_sha256", b3_current["immutable_upstream"].get("step0_corpus_manifest")),
                )
            ):
                errors.append("inputs.step0")
            if b3_inputs.get("manifest_digest") != b3_current.get("manifest_digest"):
                errors.append("inputs.b3.current_digest")
            selected, sample = _select_rows(rows)
            if sample != manifest.get("universe"):
                errors.append("universe.current_reconstruction")
            if set(manifest.get("b3_pin", {}).get("active_strategy_versions", {})) != set(ACTIVE_STRATEGY_IDS):
                errors.append("b3_pin.active_strategy_versions")
            if len(selected) != manifest.get("universe", {}).get("b2_analyzed_rows"):
                errors.append("selected_rows")
            for entry in version_entries:
                sid = entry.get("strategy_id")
                pinned = manifest.get("b3_pin", {}).get("active_strategy_versions", {}).get(sid, {})
                for field in ("rule_version", "rule_definition_sha256", "version_file_sha256"):
                    if pinned.get(field) != entry.get(field):
                        errors.append(f"{field}:{sid}")
        except B2Error as exc:
            errors.append(f"inputs:{exc}")

    if output_dir is not None:
        out = output_dir.resolve()
        for name, entry in (output_artifacts.items() if isinstance(output_artifacts, Mapping) else []):
            if not isinstance(entry, Mapping) or not isinstance(entry.get("sha256"), str):
                errors.append(f"output_hash:{name}")
            elif not (out / name).is_file() or _sha256_file(out / name) != entry["sha256"]:
                errors.append(f"output_hash:{name}")
        performance_path = out / "strategy-performance.json"
        if performance_path.is_file():
            try:
                performance = _read_json(performance_path)
                if performance.get("schema_version") != PERFORMANCE_SCHEMA_VERSION:
                    errors.append("performance.schema_version")
                if performance.get("baseline_sha") != manifest.get("baseline_sha"):
                    errors.append("performance.baseline_sha")
                if performance.get("analysis_date") != manifest.get("analysis_date"):
                    errors.append("performance.analysis_date")
                if performance.get("b1_pin") != manifest.get("b1_pin"):
                    errors.append("performance.b1_pin")
                if performance.get("observation_identity") != manifest.get("observation_identity"):
                    errors.append("performance.observation_identity")
                if performance.get("universe") != manifest.get("universe"):
                    errors.append("performance.universe")
                strategies = performance.get("strategies")
                if not isinstance(strategies, list) or [item.get("strategy_id") for item in strategies] != list(ACTIVE_STRATEGY_IDS):
                    errors.append("performance.strategy_coverage")
                if _contains_nonfinite(performance):
                    errors.append("performance.non_finite")
            except B2Error as exc:
                errors.append(f"performance:{exc}")
    return errors


def _contains_nonfinite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_contains_nonfinite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_nonfinite(item) for item in value)
    return False


def run_b2_validation(
    root: Path,
    *,
    output_dir: Path,
    baseline_sha: str,
    analysis_date: str,
) -> dict[str, Any]:
    """Generate deterministic B2 artifacts and return the manifest."""

    root = root.resolve()
    output_dir = output_dir.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", baseline_sha):
        raise B2Error("baseline_sha must be a full lowercase 40-character commit SHA")
    _date_value(analysis_date, "analysis_date")
    b3_manifest, b1_manifest, b1_rows, version_entries = _validate_input_pins(root)
    selected, universe = _select_rows(b1_rows)
    by_version = {entry["strategy_id"]: entry for entry in version_entries}
    strategies = [
        _strategy_analysis(root, strategy_id, by_version[strategy_id], selected, len(selected))
        for strategy_id in ACTIVE_STRATEGY_IDS
    ]
    analysis = {
        "universe": universe,
        "strategies": strategies,
    }
    performance = {
        "schema_version": PERFORMANCE_SCHEMA_VERSION,
        "mode": "method_validation_b2_performance",
        "baseline_sha": baseline_sha,
        "analysis_date": analysis_date,
        "b1_pin": {
            "dataset_version": B1_DATASET_VERSION,
            "dataset_sha256": B1_DATASET_SHA256,
            "manifest_digest": B1_MANIFEST_DIGEST,
            "episode_resolution_path": B1_EPISODE_RESOLUTION_PATH,
            "episode_resolution_sha256": B1_EPISODE_RESOLUTION_SHA256,
        },
        "b3_pin": {
            "manifest_digest": B3_MANIFEST_DIGEST,
            "active_strategy_ids": list(ACTIVE_STRATEGY_IDS),
        },
        "metric_definitions": METRIC_DEFINITIONS,
        "confidence_policy": CONFIDENCE_POLICY,
        "universe": universe,
        "observation_identity": {
            "source_rows": universe["b1_total_rows"],
            "b1_v3_countable_unique_episodes": universe["b1_v3_countable_episode_count"],
            "b2_active_eligible_source_rows": universe["b2_active_eligible_source_rows"],
            "b2_analyzed_unique_episodes": universe["b2_analyzed_unique_episodes"],
            "duplicate_representation_rows_excluded": universe["b1_v3_duplicate_representation_rows"],
            "unresolved_rows_excluded": universe["b1_v3_unresolved_excluded_rows"],
        },
        "strategies": strategies,
    }
    _write_json(output_dir / "strategy-performance.json", performance)
    _write_text(output_dir / "strategy-performance.md", _render_report(analysis=analysis))
    provisional_manifest = {
        "mode": "method_validation_b2_performance",
        "baseline_sha": baseline_sha,
        "analysis_date": analysis_date,
    }
    _write_text(output_dir / "README.md", _render_readme(manifest=provisional_manifest, analysis=analysis))
    manifest = _manifest_payload(
        root=root,
        output_dir=output_dir,
        baseline_sha=baseline_sha,
        analysis_date=analysis_date,
        b1_manifest=b1_manifest,
        b3_manifest=b3_manifest,
        analysis=analysis,
    )
    _write_json(output_dir / "manifest.json", manifest)
    errors = validate_b2_manifest(manifest, project_root=root, output_dir=output_dir)
    if errors:
        raise B2Error("generated B2 manifest failed validation: " + "; ".join(errors))
    return manifest
    return b3_manifest, b1_manifest, rows, [by_strategy[strategy_id] for strategy_id in ACTIVE_STRATEGY_IDS]
