"""Trader DNA V1 synthesis over the frozen B1/B3/B2 evidence chain.

This module is intentionally a downstream, descriptive consumer.  It does not
change strategy identity, frozen rules, historical records, or performance
results.  The only additional calculation is a deterministic aggregate view
over the already countable B1 v3 canonical episodes, needed for whole-sample
concentration and shape claims that B2 exposes per strategy.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .episode_identity import validate_b1_v3_manifest
from .performance_validation import validate_b2_manifest
from .rule_freeze import validate_b3_manifest


TRADER_DNA_SCHEMA_VERSION = "trader-dna.v1"
B1_DATASET_PATH = "exports/method-validation/b1-v3/canonical-trade-dataset.jsonl"
B1_MANIFEST_PATH = "exports/method-validation/b1-v3/manifest.json"
B1_EPISODE_RESOLUTION_PATH = "canonical/adjudications/method-validation/episode-identity-resolution-20260911.jsonl"
B3_MANIFEST_PATH = "exports/method-validation/b3/manifest.json"
B2_MANIFEST_PATH = "exports/method-validation/b2/manifest.json"
B2_PERFORMANCE_PATH = "exports/method-validation/b2/strategy-performance.json"
B2_PERFORMANCE_REPORT_PATH = "exports/method-validation/b2/strategy-performance.md"

B1_SCHEMA_VERSION = "method-validation-b1.v3"
B1_DATASET_VERSION = "method-validation-b1.v3"
B1_DATASET_SHA256 = "00c76bdca0480085e3c9ac70dcfda9b6ce5147224e4bfb68b5baf17f98978729"
B1_MANIFEST_DIGEST = "9ca0825b5032fed703e46ab3c97d05dee933cd3aefc5594f5c62254475ead59f"
B1_MANIFEST_FILE_SHA256 = "db8f866da74a35fc87bdae797b7f695577c70735d01a601b2c00dcea941cafdd"
B1_EPISODE_RESOLUTION_SHA256 = "2f16bb33e4befaefbc52b1a144465da3be25b33c9a30999cdfe64d427bab9a77"
B3_SCHEMA_VERSION = "method-validation-b3.v1"
B3_MANIFEST_DIGEST = "bf7cdb15a669e82c5d1002140532b3b43122deab6151e54657d3efcdb80d66c5"
B3_MANIFEST_FILE_SHA256 = "2bc2418a457c2e8ed1300f61014424ac5862b93aa2e452347d5af3c40be8599e"
B2_SCHEMA_VERSION = "method-validation-b2.v1"
B2_PERFORMANCE_SCHEMA_VERSION = "method-validation-b2-performance.v1"
B2_MANIFEST_DIGEST = "923c34f694e432d4449a05b4a8a33eccc2d6a216dc1f126de6817135a2746672"
B2_MANIFEST_FILE_SHA256 = "e7d2b25e8646e846ce879f66fec177078552e3169dc35923c823e2113716b419"
B2_PERFORMANCE_FILE_SHA256 = "6452103a7f5f8acaaf55d79a1f56acf0f2e3b878edfc29b561f40dd8aae23d1a"

ACTIVE_STRATEGY_IDS = (
    "stg-vegas-tunnel",
    "stg-po3",
    "stg-ict-liquidity",
    "stg-triple-tap",
)
OUTCOME_VALUES = ("win", "loss", "breakeven")
ORIGIN_VALUES = ("live", "backtest")
OUTPUT_NAMES = (
    "README.md",
    "manifest.json",
    "trader-dna.json",
    "trader-dna.md",
    "improvement-hypotheses.md",
)
FINDING_STATUSES = {"supported", "mixed", "insufficient", "not_observable"}
EVIDENCE_CLASSES = {"fact", "descriptive_pattern", "hypothesis"}
CONFIDENCE_VALUES = {"high", "moderate", "insufficient", "not_observable"}
CHRONOLOGY_ORDERING_CONTRACT = "event_date_then_known_time_then_stable_tiebreak"
FORBIDDEN_TERMS = (
    "best_strategy",
    "top_strategy",
    "winner",
)


class TraderDNAError(ValueError):
    """Raised when an immutable input or generated artifact fails closed."""


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TraderDNAError(f"unable to read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise TraderDNAError(f"JSON artifact is not an object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TraderDNAError(f"unable to read JSONL artifact: {path}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TraderDNAError(f"invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise TraderDNAError(f"JSONL row is not an object at {path}:{line_number}")
        rows.append(value)
    return rows


def _round(value: float | int | None) -> float | None:
    if value is None:
        return None
    result = round(float(value), 6)
    if not math.isfinite(result):
        raise TraderDNAError("non-finite metric generated")
    return result


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return _round(ordered[lower])
    weight = position - lower
    return _round(ordered[lower] + weight * (ordered[upper] - ordered[lower]))


def _valid_sha(value: Any, length: int) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(rf"[0-9a-f]{{{length}}}", value))


def _date_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    time_entry = row.get("time_entry")
    if not isinstance(time_entry, str) or not re.fullmatch(r"\d{2}:\d{2}", time_entry):
        time_entry = "99:99"
    return (
        str(row.get("event_date") or "9999-99-99"),
        time_entry,
        str(row.get("source_path") or ""),
        str(row.get("step0_record_id") or ""),
    )


def _chronology_quality(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Describe whether the canonical sample can recover intraday order.

    Presence follows the B2 missingness convention: a non-empty source value is
    known/present, while only a valid HH:MM value supplies an intraday clock.
    Invalid present values remain deterministic tie-break inputs but do not make
    the chronology fully recoverable.
    """
    time_entry_present = sum(
        value is not None and (not isinstance(value, str) or bool(value.strip()))
        for value in (row.get("time_entry") for row in rows)
    )
    time_entry_valid = sum(
        isinstance(value, str) and re.fullmatch(r"\d{2}:\d{2}", value) is not None
        for value in (row.get("time_entry") for row in rows)
    )
    event_date_complete = all(
        isinstance(value, str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None
        and _is_valid_iso_date(value)
        for value in (row.get("event_date") for row in rows)
    )
    return {
        "event_date_complete": event_date_complete,
        "time_entry_known": time_entry_present,
        "time_entry_valid": time_entry_valid,
        "time_entry_missing": len(rows) - time_entry_present,
        "intraday_order_recoverable": event_date_complete and time_entry_valid == len(rows),
        "ordering_contract": CHRONOLOGY_ORDERING_CONTRACT,
    }


def _is_valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _select_rows(rows: Sequence[Mapping[str, Any]], active_ids: set[str]) -> list[dict[str, Any]]:
    selected = [
        dict(row)
        for row in rows
        if row.get("analysis_eligibility") == "eligible"
        and row.get("performance_counting_eligibility") is True
        and row.get("episode_role") == "primary"
        and row.get("strategy_id") in active_ids
    ]
    selected.sort(key=_date_key)
    ids = [str(row.get("canonical_episode_id")) for row in selected]
    if len(selected) != 133:
        raise TraderDNAError(f"expected 133 selected canonical episodes, got {len(selected)}")
    if any(not isinstance(row.get("canonical_episode_id"), str) or not row["canonical_episode_id"] for row in selected):
        raise TraderDNAError("selected row has no canonical episode ID")
    if len(ids) != len(set(ids)):
        raise TraderDNAError("duplicate canonical episode ID in Trader DNA selection")
    if set(row.get("sample_origin") for row in selected) != set(ORIGIN_VALUES):
        raise TraderDNAError("Trader DNA selection does not cover live and backtest cohorts")
    return selected


def _outcome_rates(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(row.get("outcome")) for row in rows)
    if any(value not in OUTCOME_VALUES for value in counts):
        raise TraderDNAError("unknown outcome in selected B1 rows")
    n = len(rows)
    return {
        "n": n,
        "win_count": counts["win"],
        "loss_count": counts["loss"],
        "breakeven_count": counts["breakeven"],
        "win_rate": _round(counts["win"] / n),
        "loss_rate": _round(counts["loss"] / n),
        "breakeven_rate": _round(counts["breakeven"] / n),
    }


def _global_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [float(row["canonical_realized_r"]) for row in rows]
    if not values:
        raise TraderDNAError("empty Trader DNA selection")
    rates = _outcome_rates(rows)
    positive = sorted((value for value in values if value > 0), reverse=True)
    ordered = sorted(rows, key=_date_key)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    max_wins = max_losses = wins = losses = 0
    for row in ordered:
        cumulative += float(row["canonical_realized_r"])
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        outcome = row.get("outcome")
        if outcome == "win":
            wins += 1
            losses = 0
        elif outcome == "loss":
            losses += 1
            wins = 0
        else:
            wins = losses = 0
        max_wins = max(max_wins, wins)
        max_losses = max(max_losses, losses)
    gross_positive = sum(positive)
    return {
        **rates,
        "chronology_quality": _chronology_quality(rows),
        "live_count": sum(row.get("sample_origin") == "live" for row in rows),
        "backtest_count": sum(row.get("sample_origin") == "backtest" for row in rows),
        "long_count": sum(row.get("direction") == "long" for row in rows),
        "short_count": sum(row.get("direction") == "short" for row in rows),
        "mean_r": _round(sum(values) / len(values)),
        "median_r": _quantile(values, 0.5),
        "q25_r": _quantile(values, 0.25),
        "q75_r": _quantile(values, 0.75),
        "min_r": _round(min(values)),
        "max_r": _round(max(values)),
        "gross_positive_r": _round(gross_positive),
        "gross_negative_r": _round(sum(value for value in values if value < 0)),
        "positive_count": len(positive),
        "positive_tail_top_1_contribution": _round(positive[0] / gross_positive if positive else None),
        "positive_tail_top_3_contribution": _round(sum(positive[:3]) / gross_positive if positive else None),
        "mean_minus_median_r": _round((sum(values) / len(values)) - (_quantile(values, 0.5) or 0.0)),
        "final_cumulative_r": _round(cumulative),
        "max_cumulative_r_drawdown": _round(max_drawdown),
        "max_consecutive_wins": max_wins,
        "max_consecutive_losses": max_losses,
        "instrument_counts": _instrument_counts(rows),
    }


def _instrument_counts(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(str(row.get("instrument")) for row in rows)
    n = len(rows)
    return [
        {"instrument": instrument, "count": count, "share": _round(count / n)}
        for instrument, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _segment_metrics(rows: Sequence[Mapping[str, Any]], origin: str) -> dict[str, Any]:
    subset = [row for row in rows if row.get("sample_origin") == origin]
    if not subset:
        return {"n": 0, "win_rate": None, "mean_r": None, "median_r": None}
    values = [float(row["canonical_realized_r"]) for row in subset]
    counts = Counter(str(row.get("outcome")) for row in subset)
    return {
        "n": len(subset),
        "win_rate": _round(counts["win"] / len(subset)),
        "mean_r": _round(sum(values) / len(values)),
        "median_r": _quantile(values, 0.5),
        "total_r": _round(sum(values)),
    }


def _support(path: str, pointer: str) -> dict[str, str]:
    return {"path": path, "pointer": pointer}


def _finding(
    *,
    finding_id: str,
    dimension_id: str,
    label: str,
    status: str,
    claim: str,
    evidence_class: str,
    sample_n: int,
    confidence: str,
    evidence_strength: str,
    observability: str,
    supporting_metrics: list[dict[str, Any]],
    supporting_artifacts: list[dict[str, str]],
    limitations: list[str],
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "dimension_id": dimension_id,
        "label": label,
        "status": status,
        "claim": claim,
        "evidence_class": evidence_class,
        "sample_n": sample_n,
        "confidence": confidence,
        "evidence_strength": evidence_strength,
        "observability": observability,
        "supporting_metrics": supporting_metrics,
        "supporting_artifacts": supporting_artifacts,
        "limitations": limitations,
        "optimization_implication": None,
    }


def _rule_observability_summary(performance: Mapping[str, Any]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for strategy in performance.get("strategies", []):
        rules = strategy.get("rule_observability", [])
        summary.append(
            {
                "strategy_id": strategy.get("strategy_id"),
                "sample_n": strategy.get("sample", {}).get("n"),
                "all_rules": [
                    {
                        "rule_id": rule.get("rule_id"),
                        "status": rule.get("status"),
                        "unknown": rule.get("unknown"),
                        "observable_rows": rule.get("observable_rows"),
                    }
                    for rule in rules
                ],
            }
        )
    return summary


def _strategy_findings(
    rows: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    performance: Mapping[str, Any],
) -> list[dict[str, Any]]:
    strategies = {str(item["strategy_id"]): item for item in performance["strategies"]}
    distribution = {
        strategy_id: sum(row.get("strategy_id") == strategy_id for row in rows)
        for strategy_id in ACTIVE_STRATEGY_IDS
    }
    shares = {strategy_id: _round(count / len(rows)) for strategy_id, count in distribution.items()}
    top_two = sum(sorted(distribution.values(), reverse=True)[:2]) / len(rows)
    artifacts_b2 = [
        _support(B2_PERFORMANCE_PATH, "$.strategies[*].segments"),
        _support(B2_PERFORMANCE_REPORT_PATH, "strategy sections"),
    ]
    artifacts_b1 = [_support(B1_DATASET_PATH, "selected rows where performance_counting_eligibility=true")]
    findings: list[dict[str, Any]] = []
    findings.append(
        _finding(
            finding_id="dna-finding-method-footprint",
            dimension_id="method_strategy_usage",
            label="Historical method footprint",
            status="supported",
            claim=(
                "The 133-episode historical analysis sample is concentrated in Vegas Tunnel (82; 61.7%) "
                "and ICT Liquidity (41; 30.8%), together 92.5%; this is usage distribution, not personal suitability or future edge."
            ),
            evidence_class="fact",
            sample_n=len(rows),
            confidence="high",
            evidence_strength="direct_canonical",
            observability="machine_observable",
            supporting_metrics=[
                {"metric": "strategy_episode_counts", "values": distribution},
                {"metric": "strategy_episode_shares", "values": shares},
                {"metric": "active_method_count", "value": len(ACTIVE_STRATEGY_IDS)},
                {"metric": "active_concentration_top_two", "value": _round(top_two)},
                {"metric": "live_backtest_coverage", "value": {"live": metrics["live_count"], "backtest": metrics["backtest_count"]}},
                {"metric": "direction_balance", "value": {"long": metrics["long_count"], "short": metrics["short_count"]}},
            ],
            supporting_artifacts=artifacts_b1 + artifacts_b2,
            limitations=[
                "The canonical sample records historical usage, not preference, aptitude, or psychological identity.",
                "Retired, experimental, and unresolved rows are outside this active frozen-strategy performance intersection.",
            ],
        )
    )
    findings.append(
        _finding(
            finding_id="dna-finding-outcome-structure",
            dimension_id="outcome_structure",
            label="Historical outcome structure",
            status="supported",
            claim="The selected historical episodes contain 85 wins, 39 losses, and 9 breakevens; outcome and R fields are directly inherited from B1 canonical evidence.",
            evidence_class="fact",
            sample_n=len(rows),
            confidence="high",
            evidence_strength="direct_canonical",
            observability="machine_observable",
            supporting_metrics=[
                {"metric": "outcome_counts", "value": {key: metrics[f"{key}_count"] for key in OUTCOME_VALUES}},
                {"metric": "outcome_rates", "value": {key: metrics[f"{key}_rate"] for key in ("win", "loss", "breakeven")}},
                {"metric": "mean_r", "value": metrics["mean_r"]},
                {"metric": "median_r", "value": metrics["median_r"]},
            ],
            supporting_artifacts=artifacts_b1 + [_support(B2_PERFORMANCE_PATH, "$.strategies[*].segments.overall")],
            limitations=["These are historical descriptive outcomes and do not establish a causal mechanism or future performance."],
        )
    )
    per_strategy_tail = {
        strategy_id: {
            "n": strategies[strategy_id]["sample"]["n"],
            "max_r": strategies[strategy_id]["segments"]["overall"]["max_r"],
            "top_1_positive_r_contribution": strategies[strategy_id]["positive_r_concentration"]["top_1_positive_r_contribution"],
            "top_3_positive_r_contribution": strategies[strategy_id]["positive_r_concentration"]["top_3_positive_r_contribution"],
        }
        for strategy_id in ACTIVE_STRATEGY_IDS
    }
    findings.append(
        _finding(
            finding_id="dna-finding-payoff-shape",
            dimension_id="payoff_shape_and_outlier_dependence",
            label="Payoff shape and positive-tail dependence",
            status="mixed",
            claim=(
                f"Across 133 episodes, mean R ({metrics['mean_r']}) is above median R ({metrics['median_r']}) by "
                f"{metrics['mean_minus_median_r']}R; the overall largest positive episode contributes "
                f"{metrics['positive_tail_top_1_contribution']:.1%} of positive R and the largest three contribute "
                f"{metrics['positive_tail_top_3_contribution']:.1%}. Small strategy cohorts remain tail-sensitive."
            ),
            evidence_class="descriptive_pattern",
            sample_n=len(rows),
            confidence="moderate",
            evidence_strength="descriptive_historical",
            observability="machine_observable",
            supporting_metrics=[
                {"metric": "r_quantiles", "value": {key: metrics[key] for key in ("min_r", "q25_r", "median_r", "q75_r", "max_r")}},
                {"metric": "mean_minus_median_r", "value": metrics["mean_minus_median_r"]},
                {"metric": "overall_positive_tail_contribution", "value": {"top_1": metrics["positive_tail_top_1_contribution"], "top_3": metrics["positive_tail_top_3_contribution"]}},
                {"metric": "positive_tail_by_strategy", "value": per_strategy_tail},
            ],
            supporting_artifacts=artifacts_b2,
            limitations=[
                "Positive episodes are retained; no winsorization or loser removal was performed.",
                "PO3 n=8 and Triple Tap n=2 cannot support stable payoff conclusions; their positive-tail shares are descriptive only.",
            ],
        )
    )
    findings.append(
        _finding(
            finding_id="dna-finding-drawdown-streak",
            dimension_id="historical_drawdown_and_streak",
            label="Historical drawdown and streak profile",
            status="mixed",
            claim=(
                f"Under the deterministic canonical ordering contract, the selected sample records a maximum cumulative drawdown of "
                f"{metrics['max_cumulative_r_drawdown']}R, a maximum losing streak of {metrics['max_consecutive_losses']}, and a "
                f"maximum winning streak of {metrics['max_consecutive_wins']}; these are descriptive values under that ordering, not "
                "an exact execution or intraday recovery path. Exact intraday order is not fully recoverable because time_entry is "
                f"missing for {metrics['chronology_quality']['time_entry_missing']}/{len(rows)} selected episodes."
            ),
            evidence_class="descriptive_pattern",
            sample_n=len(rows),
            confidence="moderate",
            evidence_strength="descriptive_historical",
            observability="partially_observable",
            supporting_metrics=[
                {"metric": "max_cumulative_r_drawdown", "value": metrics["max_cumulative_r_drawdown"]},
                {"metric": "max_consecutive_losses", "value": metrics["max_consecutive_losses"]},
                {"metric": "max_consecutive_wins", "value": metrics["max_consecutive_wins"]},
                {"metric": "final_cumulative_r", "value": metrics["final_cumulative_r"]},
                {"metric": "chronology_quality", "value": metrics["chronology_quality"]},
            ],
            supporting_artifacts=[
                _support(B1_DATASET_PATH, "selected rows: event_date/time_entry/source_path/step0_record_id"),
                _support(B2_PERFORMANCE_PATH, "$.strategies[*].chronological_cumulative_r"),
            ],
            limitations=[
                "time_entry is missing for 117/133 selected episodes, so exact intraday order is not fully recoverable.",
                "source_path and step0_record_id stable tie-breaks make output deterministic but do not assert execution chronology.",
                "Same-day order-sensitive drawdown and streak metrics may differ from the actual intraday sequence.",
                "The deterministic canonical ordering cannot establish a precise intraday recovery path, future recovery behavior, or risk tolerance.",
            ],
        )
    )
    live = _segment_metrics(rows, "live")
    backtest = _segment_metrics(rows, "backtest")
    findings.append(
        _finding(
            finding_id="dna-finding-live-backtest",
            dimension_id="live_backtest_behavior",
            label="Live versus backtest descriptive shift",
            status="mixed",
            claim=(
                f"Live records show win rate {live['win_rate']:.1%} versus backtest {backtest['win_rate']:.1%}, "
                f"while mean R is {live['mean_r']} versus {backtest['mean_r']} and median R is {live['median_r']} versus {backtest['median_r']}; "
                "the direction of these descriptive differences is mixed and is not attributed to psychology."
            ),
            evidence_class="descriptive_pattern",
            sample_n=len(rows),
            confidence="moderate",
            evidence_strength="descriptive_historical",
            observability="machine_observable",
            supporting_metrics=[
                {"metric": "live", "value": live},
                {"metric": "backtest", "value": backtest},
                {"metric": "live_minus_backtest", "value": {"win_rate": _round(live["win_rate"] - backtest["win_rate"]), "mean_r": _round(live["mean_r"] - backtest["mean_r"]), "median_r": _round(live["median_r"] - backtest["median_r"]) }},
            ],
            supporting_artifacts=artifacts_b2,
            limitations=["Live/backtest differences are descriptive cohort comparisons, not significance tests or evidence of a psychological cause."],
        )
    )
    instruments = metrics["instrument_counts"]
    top_three = sum(item["count"] for item in instruments[:3]) / len(rows)
    findings.append(
        _finding(
            finding_id="dna-finding-market-concentration",
            dimension_id="market_instrument_concentration",
            label="Historical instrument concentration",
            status="supported",
            claim=(
                f"BTCUSDT accounts for {instruments[0]['share']:.1%} of selected episodes and the three most represented instruments account for "
                f"{top_three:.1%}; this is observed market concentration, not a recommendation to filter symbols."
            ),
            evidence_class="fact",
            sample_n=len(rows),
            confidence="moderate",
            evidence_strength="direct_canonical",
            observability="machine_observable",
            supporting_metrics=[{"metric": "instrument_counts_and_shares", "value": instruments}, {"metric": "top_three_share", "value": _round(top_three)}],
            supporting_artifacts=artifacts_b1 + artifacts_b2,
            limitations=["The sample does not justify symbol selection, symbol exclusion, or a claim that concentration causes outcomes."],
        )
    )
    maturity = {
        strategy_id: {
            "n": strategies[strategy_id]["sample"]["n"],
            "live_n": strategies[strategy_id]["sample"]["live_count"],
            "backtest_n": strategies[strategy_id]["sample"]["backtest_count"],
            "b2_confidence": strategies[strategy_id]["confidence"]["classification"],
            "assessment": strategies[strategy_id]["assessment"],
        }
        for strategy_id in ACTIVE_STRATEGY_IDS
    }
    findings.append(
        _finding(
            finding_id="dna-finding-evidence-maturity",
            dimension_id="strategy_evidence_maturity",
            label="Strategy-level evidence maturity",
            status="mixed",
            claim="Vegas Tunnel and ICT Liquidity have the larger observed cohorts and moderate B2 analytical confidence; PO3 (n=8) and Triple Tap (n=2) remain insufficient for a strategy-level conclusion.",
            evidence_class="descriptive_pattern",
            sample_n=len(rows),
            confidence="moderate",
            evidence_strength="descriptive_historical",
            observability="machine_observable",
            supporting_metrics=[{"metric": "strategy_evidence_maturity", "value": maturity}],
            supporting_artifacts=artifacts_b2,
            limitations=["Evidence maturity is not strategy quality, ranking, suitability, or proof of future edge."],
        )
    )
    rule_summary = _rule_observability_summary(performance)
    findings.append(
        _finding(
            finding_id="dna-finding-setup-observability",
            dimension_id="setup_confirmation_observability",
            label="Setup and confirmation observability",
            status="not_observable",
            claim="Most B3 setup and confirmation operands are explicitly NOT_MACHINE_OBSERVABLE in B2; no setup or confirmation preference can be concluded from this dataset.",
            evidence_class="fact",
            sample_n=len(rows),
            confidence="not_observable",
            evidence_strength="direct_canonical",
            observability="not_observable",
            supporting_metrics=[{"metric": "b3_rule_observability", "value": rule_summary}],
            supporting_artifacts=[_support(B2_PERFORMANCE_PATH, "$.strategies[*].rule_observability"), _support(B3_MANIFEST_PATH, "$.strategy_versions[*].effective_rule_ids")],
            limitations=["B1 does not carry the exact pre-trade chart/setup operands required by most B3 rules; unknown is preserved rather than inferred."],
        )
    )
    for finding_id, dimension_id, label, claim, metric in (
        ("dna-finding-risk-sizing", "risk_sizing_observability", "Position sizing and risk percentage", "The canonical B1/B2 fields used here do not provide an authoritative position-size or risk-percentage observation.", "position_size_or_risk_percentage"),
        ("dna-finding-holding-session", "holding_duration_and_session", "Holding duration and session preference", "The canonical sample does not expose a complete, stable holding-duration/session observation; time_entry is missing for 117 of 133 selected episodes.", "holding_duration_or_session"),
        ("dna-finding-psychology", "psychological_state_and_discipline", "Psychological state and discipline", "No psychological state, motivation, revenge-trading, or discipline truth is observable from these canonical performance fields.", "psychological_state"),
        ("dna-finding-loss-attribution", "method_vs_execution_loss_attribution", "Method versus execution loss attribution", "The selected canonical evidence cannot attribute each loss to method failure, execution deviation, position management, or data quality.", "loss_attribution"),
    ):
        findings.append(
            _finding(
                finding_id=finding_id,
                dimension_id=dimension_id,
                label=label,
                status="not_observable",
                claim=claim,
                evidence_class="fact",
                sample_n=len(rows),
                confidence="not_observable",
                evidence_strength="unavailable",
                observability="not_observable",
                supporting_metrics=[{"metric": metric, "value": None}],
                supporting_artifacts=[_support(B1_DATASET_PATH, "selected canonical fields"), _support(B2_PERFORMANCE_PATH, "$.universe.missingness")],
                limitations=["This is an evidence gap, not a negative finding about the trader."],
            )
        )
    return findings


def _hypotheses(metrics: Mapping[str, Any], performance: Mapping[str, Any]) -> list[dict[str, Any]]:
    b2 = [_support(B2_PERFORMANCE_PATH, "$.strategies[*].segments"), _support(B2_PERFORMANCE_REPORT_PATH, "strategy sections")]
    b1 = [_support(B1_DATASET_PATH, "selected canonical episodes")]
    common = {
        "evidence_class": "hypothesis",
        "sample_n": 133,
        "optimization_implication": None,
        "status": "future_test_candidate",
    }
    hypotheses = [
        {
            **common,
            "hypothesis_id": "HYP-OUTLIER-ROBUSTNESS-V1",
            "dimension_id": "payoff_shape_and_outlier_dependence",
            "label": "Pre-registered positive-tail robustness evaluation",
            "claim": "A future evaluation should test whether the observed descriptive profile remains interpretable under a pre-declared sensitivity analysis while retaining all canonical episodes.",
            "current_status": "future_test_candidate",
            "confidence": "insufficient",
            "supporting_metrics": [{"metric": "overall_top_three_positive_r_contribution", "value": metrics["positive_tail_top_3_contribution"]}],
            "supporting_artifacts": b2,
            "required_future_data": ["new canonical episodes", "pre-declared sensitivity protocol", "separate live/backtest provenance"],
            "success_failure_criteria_concept": "Define the sensitivity statistic and threshold before reading the future sample; report both full-sample and sensitivity results without deleting or rewriting canonical episodes.",
            "prohibited_action": "Do not remove outliers, mutate B3 rules, or tune thresholds on the current sample.",
            "limitations": ["This is a future validation candidate, not a current DNA fact."],
        },
        {
            **common,
            "hypothesis_id": "HYP-LIVE-BACKTEST-SHIFT-V1",
            "dimension_id": "live_backtest_behavior",
            "label": "Live and backtest distribution-shift evaluation",
            "claim": "The observed live/backtest differences may warrant a future pre-declared distribution comparison when additional canonical observations exist.",
            "current_status": "future_test_candidate",
            "confidence": "insufficient",
            "supporting_metrics": [{"metric": "live_backtest_difference", "value": "mixed descriptive differences"}],
            "supporting_artifacts": b2,
            "required_future_data": ["additional resolved live and backtest episodes", "stable mode provenance", "pre-declared comparison criteria"],
            "success_failure_criteria_concept": "Assess whether pre-declared live/backtest effect ranges are directionally consistent; do not attribute any difference to psychology without direct evidence.",
            "prohibited_action": "Do not change execution rules, risk sizing, or strategy lifecycle from this current delta.",
            "limitations": ["Current cohort deltas are descriptive only."],
        },
        {
            **common,
            "hypothesis_id": "HYP-SYMBOL-CONCENTRATION-V1",
            "dimension_id": "market_instrument_concentration",
            "label": "Instrument-concentration robustness evaluation",
            "claim": "The observed BTCUSDT and top-three instrument concentration may require a future robustness evaluation on a broader canonical instrument mix.",
            "current_status": "future_test_candidate",
            "confidence": "insufficient",
            "supporting_metrics": [{"metric": "btc_share", "value": metrics["instrument_counts"][0]["share"]}, {"metric": "top_three_share", "value": _round(sum(item["count"] for item in metrics["instrument_counts"][:3]) / 133)}],
            "supporting_artifacts": b1 + b2,
            "required_future_data": ["new canonical episodes across instruments", "unchanged strategy/rule versions", "instrument provenance"],
            "success_failure_criteria_concept": "Pre-declare a concentration threshold and report whether the descriptive profile persists across the observed instrument mix; do not filter current losers or symbols.",
            "prohibited_action": "Do not introduce symbol filters or declare a preferred instrument from this sample.",
            "limitations": ["Concentration is observed composition, not a causal market dependency."],
        },
        {
            **common,
            "hypothesis_id": "HYP-PO3-SAMPLE-EXPANSION-V1",
            "dimension_id": "strategy_evidence_maturity",
            "label": "PO3 evidence expansion requirement",
            "claim": "PO3 requires additional countable episodes before a stable strategy-level profile can be evaluated.",
            "current_status": "future_test_candidate",
            "confidence": "insufficient",
            "supporting_metrics": [{"metric": "po3_n", "value": 8}, {"metric": "po3_live_n", "value": 2}],
            "supporting_artifacts": b2,
            "required_future_data": ["additional canonical PO3 episodes", "both live and backtest provenance", "observable pre-trade setup evidence where available"],
            "success_failure_criteria_concept": "Re-evaluate only after a pre-declared minimum evidence threshold is reached; retain the insufficient status if it is not reached.",
            "prohibited_action": "Do not treat n=8 as established edge or change PO3 v1 rules.",
            "limitations": ["No conclusion about PO3 quality follows from this requirement."],
        },
        {
            **common,
            "hypothesis_id": "HYP-TRIPLE-TAP-SAMPLE-EXPANSION-V1",
            "dimension_id": "strategy_evidence_maturity",
            "label": "Triple Tap evidence expansion requirement",
            "claim": "Triple Tap requires substantially more countable episodes and live representation before its profile can be interpreted beyond insufficient evidence.",
            "current_status": "future_test_candidate",
            "confidence": "insufficient",
            "supporting_metrics": [{"metric": "triple_tap_n", "value": 2}, {"metric": "triple_tap_live_n", "value": 0}],
            "supporting_artifacts": b2,
            "required_future_data": ["additional canonical Triple Tap episodes", "resolved live/backtest provenance", "observable variant setup evidence where available"],
            "success_failure_criteria_concept": "Keep the status insufficient until the pre-declared sample and live-coverage conditions are met.",
            "prohibited_action": "Do not infer a stable payoff profile or change Triple Tap v1 rules from n=2.",
            "limitations": ["The current sample is not sufficient for a strategy-level conclusion."],
        },
        {
            **common,
            "hypothesis_id": "HYP-SETUP-OBSERVABILITY-V1",
            "dimension_id": "setup_confirmation_observability",
            "label": "Pre-trade observability contract expansion",
            "claim": "Future records that preserve exact pre-trade setup tags and evidence references may make more frozen-rule operands observable without retrospective inference.",
            "current_status": "future_test_candidate",
            "confidence": "not_observable",
            "supporting_metrics": [{"metric": "b3_setup_rules_currently_unknown", "value": True}],
            "supporting_artifacts": [_support(B2_PERFORMANCE_PATH, "$.strategies[*].rule_observability"), _support(B3_MANIFEST_PATH, "$.strategy_versions[*].effective_rule_ids")],
            "required_future_data": ["pre-trade setup tags", "chart/evidence references", "evidence timing", "rule-version linkage"],
            "success_failure_criteria_concept": "Validate that future canonical rows can report observable, unknown, and contradictory rule states without changing historical B1/B2 results.",
            "prohibited_action": "Do not backfill setup tags from outcomes or use guessed tags to exclude historical rows.",
            "limitations": ["This improves future observability; it does not make current unknowns observable."],
        },
    ]
    return hypotheses


def _build_payload(
    *,
    root: Path,
    baseline_sha: str,
    analysis_date: str,
    b1_manifest: Mapping[str, Any],
    b3_manifest: Mapping[str, Any],
    b2_manifest: Mapping[str, Any],
    b2_performance: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    metrics = _global_metrics(rows)
    findings = _strategy_findings(rows, metrics, b2_performance)
    gaps = [
        {
            "gap_id": f"gap-{finding['dimension_id']}",
            "dimension_id": finding["dimension_id"],
            "status": finding["status"],
            "claim": finding["claim"],
            "evidence_class": finding["evidence_class"],
            "sample_n": finding["sample_n"],
            "confidence": finding["confidence"],
            "supporting_artifacts": finding["supporting_artifacts"],
        }
        for finding in findings
        if finding["status"] in {"insufficient", "not_observable"}
    ]
    hypotheses = _hypotheses(metrics, b2_performance)
    b3_versions = {
        str(item["strategy_id"]): item["rule_definition_sha256"]
        for item in b3_manifest.get("strategy_versions", [])
        if isinstance(item, Mapping) and "strategy_id" in item
    }
    active_versions = {
        str(strategy_id): {
            "rule_version": item["rule_version"],
            "rule_definition_sha256": item["rule_definition_sha256"],
            "version_file_sha256": item["version_file_sha256"],
        }
        for strategy_id, item in b2_manifest["b3_pin"]["active_strategy_versions"].items()
    }
    # Keep the pin structure explicit and source-driven.  The B3 manifest is
    # the authority for the four frozen active rule hashes.
    if set(active_versions) != set(ACTIVE_STRATEGY_IDS):
        raise TraderDNAError("B3 active strategy pin coverage is incomplete")
    for strategy_id in ACTIVE_STRATEGY_IDS:
        if strategy_id not in b3_versions:
            raise TraderDNAError(f"B3 rule hash missing: {strategy_id}")
        if active_versions[strategy_id]["rule_definition_sha256"] != b3_versions[strategy_id]:
            raise TraderDNAError(f"B3 rule hash mismatch: {strategy_id}")
    source_pins = {
        "trading_record": {
            "repository": "example-owner/trading-record",
            "fresh_baseline_sha": baseline_sha,
        },
        "b1_v3": {
            "schema_version": B1_SCHEMA_VERSION,
            "dataset_version": B1_DATASET_VERSION,
            "dataset_path": B1_DATASET_PATH,
            "dataset_sha256": B1_DATASET_SHA256,
            "manifest_path": B1_MANIFEST_PATH,
            "manifest_digest": B1_MANIFEST_DIGEST,
            "manifest_file_sha256": B1_MANIFEST_FILE_SHA256,
            "episode_resolution_path": B1_EPISODE_RESOLUTION_PATH,
            "episode_resolution_sha256": B1_EPISODE_RESOLUTION_SHA256,
        },
        "b3_v1": {
            "schema_version": B3_SCHEMA_VERSION,
            "manifest_path": B3_MANIFEST_PATH,
            "manifest_digest": B3_MANIFEST_DIGEST,
            "manifest_file_sha256": B3_MANIFEST_FILE_SHA256,
            "active_strategy_rule_hashes": active_versions,
        },
        "b2_v1": {
            "schema_version": B2_SCHEMA_VERSION,
            "manifest_path": B2_MANIFEST_PATH,
            "manifest_digest": B2_MANIFEST_DIGEST,
            "manifest_file_sha256": B2_MANIFEST_FILE_SHA256,
            "performance_path": B2_PERFORMANCE_PATH,
            "performance_schema_version": B2_PERFORMANCE_SCHEMA_VERSION,
            "performance_artifact_sha256": B2_PERFORMANCE_FILE_SHA256,
            "performance_report_path": B2_PERFORMANCE_REPORT_PATH,
        },
    }
    payload = {
        "schema_version": TRADER_DNA_SCHEMA_VERSION,
        "identity": {
            "profile_id": "trader-dna-v1",
            "schema": TRADER_DNA_SCHEMA_VERSION,
            "analysis_date": analysis_date,
            "source_version_pins": source_pins,
        },
        "evidence_coverage": {
            "b1_source_rows": 255,
            "analyzed_episodes": len(rows),
            "strategy_coverage": list(ACTIVE_STRATEGY_IDS),
            "strategy_episode_counts": {strategy_id: sum(row.get("strategy_id") == strategy_id for row in rows) for strategy_id in ACTIVE_STRATEGY_IDS},
            "strategy_profiles": {
                strategy_id: {
                    "display_name": b2_performance["strategies"][index]["display_name"],
                    "n": b2_performance["strategies"][index]["sample"]["n"],
                    "live_n": b2_performance["strategies"][index]["sample"]["live_count"],
                    "backtest_n": b2_performance["strategies"][index]["sample"]["backtest_count"],
                    "win_rate": b2_performance["strategies"][index]["segments"]["overall"]["win_rate"],
                    "mean_r": b2_performance["strategies"][index]["segments"]["overall"]["mean_r"],
                    "median_r": b2_performance["strategies"][index]["segments"]["overall"]["median_r"],
                    "max_r": b2_performance["strategies"][index]["segments"]["overall"]["max_r"],
                    "profit_factor_r": b2_performance["strategies"][index]["segments"]["overall"]["profit_factor_r"],
                    "max_cumulative_r_drawdown": b2_performance["strategies"][index]["segments"]["overall"]["max_cumulative_r_drawdown"],
                    "b2_confidence": b2_performance["strategies"][index]["confidence"]["classification"],
                }
                for index, strategy_id in enumerate(ACTIVE_STRATEGY_IDS)
            },
            "live_backtest_coverage": {"live": metrics["live_count"], "backtest": metrics["backtest_count"]},
            "chronology_quality": metrics["chronology_quality"],
            "canonical_selection": {
                "analysis_eligibility": "eligible",
                "performance_counting_eligibility": True,
                "episode_role": "primary",
                "one_row_per_canonical_episode": True,
            },
        },
        "dimensions": findings,
        "chronology_quality": metrics["chronology_quality"],
        "evidence_gaps": gaps,
        "hypotheses": hypotheses,
        "guardrails": {
            "optimization_performed": False,
            "new_backtest_performed": False,
            "strategy_rule_mutation": False,
            "lifecycle_mutation": False,
            "comparative_ordering_performed": False,
            "psychology_inference": False,
            "financial_advice": False,
            "marketplace_packaging": False,
            "source_records_modified": False,
            "canonical_r_rederived": False,
            "historical_outliers_removed": False,
        },
        "limitations": [
            "This is an evidence-backed historical behavior/method profile, not a personality diagnosis.",
            "Historical descriptive evidence is not proof of future edge and does not provide financial advice.",
            "Unknown, insufficient, and not-observable states are preserved rather than force-filled.",
        ],
    }
    return payload


def _render_readme(payload: Mapping[str, Any], manifest_digest: str | None = None) -> str:
    pins = payload["identity"]["source_version_pins"]
    coverage = payload["evidence_coverage"]
    lines = [
        "# Trader DNA V1 — evidence-backed trading behavior profile",
        "",
        "This is a deterministic downstream synthesis over the accepted Trading Record Strategy Mapping → B1 v3 → B3 → B2 chain.",
        "It describes historical trading behavior and method evidence; it is not a personality quiz, trading signal, financial advice, or future-edge guarantee.",
        "",
        "## Contract",
        "",
        f"- schema: `{TRADER_DNA_SCHEMA_VERSION}`",
        f"- analysis date: `{payload['identity']['analysis_date']}`",
        f"- analyzed canonical episodes: **{coverage['analyzed_episodes']}**",
        f"- strategies covered: **{len(coverage['strategy_coverage'])}**",
        "- optimization performed: **false**",
        "- new backtest performed: **false**",
        f"- chronology quality: `{coverage['chronology_quality']['ordering_contract']}`; event dates complete=`{coverage['chronology_quality']['event_date_complete']}`, time_entry known=`{coverage['chronology_quality']['time_entry_known']}`, missing=`{coverage['chronology_quality']['time_entry_missing']}`, intraday order recoverable=`{coverage['chronology_quality']['intraday_order_recoverable']}`",
        "",
        "## Source pins",
        "",
        f"- fresh trading-record baseline: `{pins['trading_record']['fresh_baseline_sha']}`",
        f"- B1 v3 dataset: `{pins['b1_v3']['dataset_sha256']}`; manifest `{pins['b1_v3']['manifest_digest']}`",
        f"- B3 v1 manifest: `{pins['b3_v1']['manifest_digest']}`",
        f"- B2 v1 manifest: `{pins['b2_v1']['manifest_digest']}`; performance artifact `{pins['b2_v1']['performance_artifact_sha256']}`",
        "",
        "## Outputs",
        "",
        "- `trader-dna.json` — machine-readable profile and contract",
        "- `trader-dna.md` — human-readable interpretation report",
        "- `improvement-hypotheses.md` — future validation candidates only",
        "- `manifest.json` — deterministic artifact hashes and provenance",
        "",
        "## Reproduction",
        "",
        f"`python -m trading_os trader-dna --baseline-sha {pins['trading_record']['fresh_baseline_sha']} --analysis-date {payload['identity']['analysis_date']} --output-dir exports/trader-dna/v1`",
        "",
        "Do not mutate B1, B3, B2, strategy rules, lifecycle, historical trades, or marketplace state from this output.",
        "",
        "Stop at fresh external review.",
    ]
    return "\n".join(lines) + "\n"


def _render_report(payload: Mapping[str, Any]) -> str:
    dimensions = {item["dimension_id"]: item for item in payload["dimensions"]}
    footprint = dimensions["method_strategy_usage"]
    outcome = dimensions["outcome_structure"]
    payoff = dimensions["payoff_shape_and_outlier_dependence"]
    drawdown = dimensions["historical_drawdown_and_streak"]
    live = dimensions["live_backtest_behavior"]
    market = dimensions["market_instrument_concentration"]
    maturity = dimensions["strategy_evidence_maturity"]
    lines = [
        "# Trader DNA V1",
        "",
        "> Evidence-backed trading behavior / method profile. Historical description only; not a personality diagnosis, signal, financial advice, or future-edge claim.",
        "",
        "## Executive Profile",
        "",
        f"- {footprint['claim']}",
        f"- {outcome['claim']}",
        f"- {maturity['claim']}",
        f"- {payoff['claim']}",
        "",
        "## Method Footprint",
        "",
        "| Strategy | Episodes | Share | Live | Backtest |",
        "|---|---:|---:|---:|---:|",
    ]
    strategy_counts = footprint["supporting_metrics"][0]["values"]
    strategy_shares = footprint["supporting_metrics"][1]["values"]
    display_names = {
        "stg-vegas-tunnel": "Vegas Tunnel",
        "stg-po3": "PO3 / Power of 3",
        "stg-ict-liquidity": "ICT Liquidity",
        "stg-triple-tap": "Triple Tap",
    }
    profiles = payload["evidence_coverage"]["strategy_profiles"]
    for strategy_id in ACTIVE_STRATEGY_IDS:
        profile = profiles[strategy_id]
        lines.append(f"| {display_names[strategy_id]} | {strategy_counts[strategy_id]} | {strategy_shares[strategy_id]:.1%} | {profile['live_n']} | {profile['backtest_n']} |")
    lines.extend(
        [
            "",
            "## Performance Shape",
            "",
            f"{payoff['claim']}",
            "",
            "The complete strategy-level quantiles and outcome counts remain in the pinned B2 report. No canonical episode or positive outlier was removed.",
            "",
            "| Strategy | n | Mean R | Median R | Max R | Max positive-tail contribution | B2 confidence |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in maturity["supporting_metrics"][0]["value"].items():
        strategy_id, values = item
        tail = next(x for x in payoff["supporting_metrics"] if x["metric"] == "positive_tail_by_strategy")["value"][strategy_id]
        profile = profiles[strategy_id]
        lines.append(f"| {display_names[strategy_id]} | {values['n']} | {profile['mean_r']:.3f} | {profile['median_r']:.3f} | {tail['max_r']:.3f} | {tail['top_1_positive_r_contribution']:.1%} | {values['b2_confidence']} |")
    lines.extend(
        [
            "",
            "## Live vs Backtest",
            "",
            f"{live['claim']}",
            "",
            "| Cohort | n | Win rate | Mean R | Median R |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    live_values = live["supporting_metrics"][0]["value"]
    backtest_values = live["supporting_metrics"][1]["value"]
    lines.append(f"| Live | {live_values['n']} | {live_values['win_rate']:.1%} | {live_values['mean_r']:.3f} | {live_values['median_r']:.3f} |")
    lines.append(f"| Backtest | {backtest_values['n']} | {backtest_values['win_rate']:.1%} | {backtest_values['mean_r']:.3f} | {backtest_values['median_r']:.3f} |")
    lines.extend(
        [
            "",
            "## Drawdown / Streak Profile",
            "",
            f"{drawdown['claim']}",
            "",
            f"Chronology quality: `{payload['chronology_quality']['ordering_contract']}`; event dates complete=`{payload['chronology_quality']['event_date_complete']}`, time_entry known=`{payload['chronology_quality']['time_entry_known']}`, missing=`{payload['chronology_quality']['time_entry_missing']}`, valid HH:MM=`{payload['chronology_quality']['time_entry_valid']}`, intraday order recoverable=`{payload['chronology_quality']['intraday_order_recoverable']}`.",
            "Because time_entry is missing for most selected episodes, source_path/step0_record_id tie-breaks are deterministic rather than execution chronology; same-day drawdown and streak values are descriptive under the canonical ordering contract.",
            "",
            "## Market Concentration",
            "",
            f"{market['claim']}",
            "",
            "| Instrument | Episodes | Share |",
            "|---|---:|---:|",
        ]
    )
    for item in market["supporting_metrics"][0]["value"]:
        lines.append(f"| {item['instrument']} | {item['count']} | {item['share']:.1%} |")
    lines.extend(
        [
            "",
            "## Rule / Setup Observability",
            "",
            "B3 frozen semantics are authoritative, but most setup and confirmation operands are NOT_MACHINE_OBSERVABLE in B2. This profile does not infer setup preference from filenames, outcomes, or R values.",
            "",
            "## Evidence Strength",
            "",
            "| Finding area | Status | Evidence class | Confidence | Observability |",
            "|---|---|---|---|---|",
        ]
    )
    for item in payload["dimensions"]:
        lines.append(f"| {item['label']} | {item['status']} | {item['evidence_class']} | {item['confidence']} | {item['observability']} |")
    lines.extend(
        [
            "",
            "## Unknowns",
            "",
        ]
    )
    for gap in payload["evidence_gaps"]:
        lines.append(f"- **{gap['dimension_id']}** — {gap['claim']}")
    lines.extend(
        [
            "",
            "## What We Cannot Conclude",
            "",
            "- No psychological state, personality type, discipline diagnosis, revenge-trading claim, or motivation claim.",
            "- No position-sizing, risk-percentage, preferred holding duration, or session preference conclusion.",
            "- No causal explanation for live/backtest differences or individual losses.",
            "- No future profitability, optimal method, symbol filter, rule change, lifecycle action, or deployment decision.",
            "",
            "## Hypotheses for Future Validation",
            "",
        ]
    )
    for hypothesis in payload["hypotheses"]:
        lines.append(f"- `{hypothesis['hypothesis_id']}` — {hypothesis['claim']}")
    lines.extend(
        [
            "",
            "## Provenance and Guardrails",
            "",
            f"- schema: `{payload['schema_version']}`",
            f"- fresh baseline: `{payload['identity']['source_version_pins']['trading_record']['fresh_baseline_sha']}`",
            f"- B1 v3: `{payload['identity']['source_version_pins']['b1_v3']['dataset_sha256']}` / `{payload['identity']['source_version_pins']['b1_v3']['manifest_digest']}`",
            f"- B3 v1: `{payload['identity']['source_version_pins']['b3_v1']['manifest_digest']}`",
            f"- B2 v1: `{payload['identity']['source_version_pins']['b2_v1']['manifest_digest']}`",
            "- NO OPTIMIZATION EXECUTED.",
            "- No B1/B3/B2/trade/registry/lifecycle mutation.",
            "- Stop at fresh external review.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_hypotheses(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Trader DNA V1 — Improvement Hypotheses",
        "",
        "Every item below is a FUTURE TEST CANDIDATE. None is a confirmed DNA finding, a rule change, or an optimization instruction.",
        "",
    ]
    for hypothesis in payload["hypotheses"]:
        lines.extend(
            [
                f"## {hypothesis['hypothesis_id']} — {hypothesis['label']}",
                "",
                f"- Current status: `{hypothesis['current_status']}`",
                f"- Evidence class: `{hypothesis['evidence_class']}`",
                f"- Claim: {hypothesis['claim']}",
                f"- Evidence trigger: {json.dumps(hypothesis['supporting_metrics'], ensure_ascii=False, sort_keys=True)}",
                f"- Required future data: {', '.join(hypothesis['required_future_data'])}",
                f"- Success/failure criteria concept: {hypothesis['success_failure_criteria_concept']}",
                f"- Prohibited action: {hypothesis['prohibited_action']}",
                "",
            ]
        )
    lines.extend(["NO OPTIMIZATION EXECUTED.", ""])
    return "\n".join(lines)


def validate_trader_dna(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    required = ("schema_version", "identity", "evidence_coverage", "chronology_quality", "dimensions", "evidence_gaps", "hypotheses", "guardrails")
    errors.extend(f"missing:{key}" for key in required if key not in payload)
    if errors:
        return errors
    if payload.get("schema_version") != TRADER_DNA_SCHEMA_VERSION:
        errors.append("schema_version")
    identity = payload.get("identity")
    if not isinstance(identity, Mapping) or identity.get("schema") != TRADER_DNA_SCHEMA_VERSION:
        errors.append("identity.schema")
    coverage = payload.get("evidence_coverage")
    if not isinstance(coverage, Mapping) or coverage.get("analyzed_episodes") != 133 or len(coverage.get("strategy_coverage", [])) != 4:
        errors.append("evidence_coverage")
    chronology_quality = payload.get("chronology_quality")
    if not isinstance(chronology_quality, Mapping):
        errors.append("chronology_quality")
    elif not isinstance(coverage, Mapping) or chronology_quality != coverage.get("chronology_quality"):
        errors.append("chronology_quality.coverage_mismatch")
    dimensions = payload.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        errors.append("dimensions")
        dimensions = []
    seen: set[str] = set()
    finding_required = ("dimension_id", "label", "status", "claim", "evidence_class", "sample_n", "confidence", "evidence_strength", "observability", "supporting_metrics", "supporting_artifacts", "limitations", "optimization_implication")
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True).lower()
    for term in FORBIDDEN_TERMS:
        if term in serialized:
            errors.append(f"forbidden:{term}")
    for finding in dimensions:
        if not isinstance(finding, Mapping):
            errors.append("finding.object")
            continue
        missing = [key for key in finding_required if key not in finding]
        errors.extend(f"finding.missing:{key}" for key in missing)
        dimension_id = finding.get("dimension_id")
        if not isinstance(dimension_id, str) or dimension_id in seen:
            errors.append(f"finding.dimension_id:{dimension_id}")
        seen.add(str(dimension_id))
        if finding.get("status") not in FINDING_STATUSES:
            errors.append(f"finding.status:{dimension_id}")
        if finding.get("evidence_class") not in EVIDENCE_CLASSES:
            errors.append(f"finding.evidence_class:{dimension_id}")
        if not isinstance(finding.get("sample_n"), int) or finding["sample_n"] < 0:
            errors.append(f"finding.sample_n:{dimension_id}")
        if finding.get("confidence") not in CONFIDENCE_VALUES:
            errors.append(f"finding.confidence:{dimension_id}")
        if finding.get("optimization_implication") is not None:
            errors.append(f"finding.optimization_implication:{dimension_id}")
        if not isinstance(finding.get("supporting_metrics"), list) or not isinstance(finding.get("supporting_artifacts"), list) or not isinstance(finding.get("limitations"), list):
            errors.append(f"finding.arrays:{dimension_id}")
        if finding.get("status") == "supported" and not finding.get("supporting_artifacts"):
            errors.append(f"finding.support:{dimension_id}")
        if finding.get("status") == "not_observable" and finding.get("observability") != "not_observable":
            errors.append(f"finding.observability:{dimension_id}")
    hypotheses = payload.get("hypotheses")
    if not isinstance(hypotheses, list):
        errors.append("hypotheses")
    else:
        hypothesis_ids: set[str] = set()
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, Mapping):
                errors.append("hypothesis.object")
                continue
            hypothesis_id = hypothesis.get("hypothesis_id")
            if not isinstance(hypothesis_id, str) or hypothesis_id in hypothesis_ids:
                errors.append(f"hypothesis.id:{hypothesis_id}")
            hypothesis_ids.add(str(hypothesis_id))
            if hypothesis.get("evidence_class") != "hypothesis" or hypothesis.get("status") != "future_test_candidate":
                errors.append(f"hypothesis.class:{hypothesis_id}")
            if hypothesis.get("optimization_implication") is not None:
                errors.append(f"hypothesis.optimization_implication:{hypothesis_id}")
    guardrails = payload.get("guardrails")
    for key in ("optimization_performed", "new_backtest_performed", "strategy_rule_mutation", "lifecycle_mutation", "comparative_ordering_performed", "psychology_inference", "financial_advice", "marketplace_packaging", "source_records_modified", "canonical_r_rederived", "historical_outliers_removed"):
        if not isinstance(guardrails, Mapping) or guardrails.get(key) is not False:
            errors.append(f"guardrail:{key}")
    return errors


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _relative(root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        # Tests may render into an external temporary directory.  Preserve a
        # stable logical product path there instead of embedding machine paths.
        return Path("exports", "trader-dna", "v1", resolved.name).as_posix()


def run_trader_dna(
    root: Path,
    *,
    baseline_sha: str,
    analysis_date: str,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    output_dir = (output_dir or Path("exports/trader-dna/v1")).resolve()
    if not _valid_sha(baseline_sha, 40):
        raise TraderDNAError("baseline_sha must be a full lowercase commit SHA")
    try:
        date.fromisoformat(analysis_date)
    except ValueError as exc:
        raise TraderDNAError("analysis_date must be an ISO date") from exc
    b1_manifest = _read_json(root / B1_MANIFEST_PATH)
    b3_manifest = _read_json(root / B3_MANIFEST_PATH)
    b2_manifest = _read_json(root / B2_MANIFEST_PATH)
    b2_performance = _read_json(root / B2_PERFORMANCE_PATH)
    b1_errors = validate_b1_v3_manifest(b1_manifest, project_root=root, output_dir=root / "exports/method-validation/b1-v3", adjudication_dir=root / "canonical/adjudications/method-validation")
    b3_errors = validate_b3_manifest(b3_manifest)
    b2_errors = validate_b2_manifest(b2_manifest, project_root=root, output_dir=root / "exports/method-validation/b2")
    if b1_errors:
        raise TraderDNAError("B1 v3 validation failed: " + "; ".join(b1_errors))
    if b3_errors:
        raise TraderDNAError("B3 validation failed: " + "; ".join(b3_errors))
    if b2_errors:
        raise TraderDNAError("B2 validation failed: " + "; ".join(b2_errors))
    exact_pins = (
        (b1_manifest.get("schema_version"), B1_SCHEMA_VERSION, "B1 schema"),
        (b1_manifest.get("dataset_version"), B1_DATASET_VERSION, "B1 dataset version"),
        (b1_manifest.get("manifest_digest"), B1_MANIFEST_DIGEST, "B1 manifest digest"),
        (b3_manifest.get("schema_version"), B3_SCHEMA_VERSION, "B3 schema"),
        (b3_manifest.get("manifest_digest"), B3_MANIFEST_DIGEST, "B3 manifest digest"),
        (b2_manifest.get("schema_version"), B2_SCHEMA_VERSION, "B2 schema"),
        (b2_manifest.get("manifest_digest"), B2_MANIFEST_DIGEST, "B2 manifest digest"),
        (b2_performance.get("schema_version"), B2_PERFORMANCE_SCHEMA_VERSION, "B2 performance schema"),
    )
    for actual, expected, label in exact_pins:
        if actual != expected:
            raise TraderDNAError(f"{label} pin mismatch")
    expected_files = {
        B1_DATASET_PATH: B1_DATASET_SHA256,
        B1_MANIFEST_PATH: B1_MANIFEST_FILE_SHA256,
        B1_EPISODE_RESOLUTION_PATH: B1_EPISODE_RESOLUTION_SHA256,
        B3_MANIFEST_PATH: B3_MANIFEST_FILE_SHA256,
        B2_MANIFEST_PATH: B2_MANIFEST_FILE_SHA256,
        B2_PERFORMANCE_PATH: B2_PERFORMANCE_FILE_SHA256,
    }
    for relative_path, expected in expected_files.items():
        actual = _sha256_file(root / relative_path)
        if actual != expected:
            raise TraderDNAError(f"immutable artifact hash mismatch: {relative_path}")
    rows = _read_jsonl(root / B1_DATASET_PATH)
    selected = _select_rows(rows, set(ACTIVE_STRATEGY_IDS))
    if b2_manifest.get("universe", {}).get("b2_analyzed_unique_episodes") != len(selected):
        raise TraderDNAError("B2 and B1 selected episode counts disagree")
    if [item.get("strategy_id") for item in b2_performance.get("strategies", [])] != list(ACTIVE_STRATEGY_IDS):
        raise TraderDNAError("B2 strategy order/coverage is not canonical")
    payload = _build_payload(
        root=root,
        baseline_sha=baseline_sha,
        analysis_date=analysis_date,
        b1_manifest=b1_manifest,
        b3_manifest=b3_manifest,
        b2_manifest=b2_manifest,
        b2_performance=b2_performance,
        rows=selected,
    )
    errors = validate_trader_dna(payload)
    if errors:
        raise TraderDNAError("generated Trader DNA failed validation: " + "; ".join(errors))
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "trader-dna.json"
    report_path = output_dir / "trader-dna.md"
    hypotheses_path = output_dir / "improvement-hypotheses.md"
    readme_path = output_dir / "README.md"
    manifest_path = output_dir / "manifest.json"
    _write_json(json_path, payload)
    _write_text(report_path, _render_report(payload))
    _write_text(hypotheses_path, _render_hypotheses(payload))
    _write_text(readme_path, _render_readme(payload))
    output_hashes = {
        name: _sha256_file(output_dir / name)
        for name in OUTPUT_NAMES
        if name != "manifest.json"
    }
    manifest: dict[str, Any] = {
        "schema_version": TRADER_DNA_SCHEMA_VERSION,
        "mode": "trader_dna_v1_synthesis",
        "analysis_date": analysis_date,
        "baseline_sha": baseline_sha,
        "source_pins": payload["identity"]["source_version_pins"],
        "evidence_coverage": payload["evidence_coverage"],
        "chronology_quality": payload["chronology_quality"],
        "dimensions": {"total": len(payload["dimensions"]), "status_counts": dict(sorted(Counter(item["status"] for item in payload["dimensions"]).items()))},
        "hypotheses": {"count": len(payload["hypotheses"]), "ids": [item["hypothesis_id"] for item in payload["hypotheses"]]},
        "output_artifacts": {
            name: {"path": _relative(root, output_dir / name), "sha256": digest}
            for name, digest in sorted(output_hashes.items())
        },
        "guardrails": payload["guardrails"],
        "optimization_performed": False,
        "read_only_inputs": True,
        "manifest_digest": None,
    }
    unsigned = dict(manifest)
    unsigned["manifest_digest"] = None
    manifest["manifest_digest"] = _sha256_bytes(_canonical_bytes(unsigned))
    _write_json(manifest_path, manifest)
    # Re-read and validate after all bytes exist.  The manifest hash is
    # intentionally non-self-referential, while each output hash is exact.
    saved_payload = _read_json(json_path)
    if validate_trader_dna(saved_payload):
        raise TraderDNAError("written Trader DNA JSON failed validation")
    for name, digest in output_hashes.items():
        if _sha256_file(output_dir / name) != digest:
            raise TraderDNAError(f"output changed after write: {name}")
    return manifest
