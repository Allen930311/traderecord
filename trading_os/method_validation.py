"""B1 — canonical analysis dataset for Method Validation (Phase B).

This module builds a **derived, versioned, immutable** per-trade dataset that
Phase B2 (performance metrics) and B3 (rule freeze) pin by ``dataset_version``.

Hard rules (owner, 2026-09-10):

* The authoritative universe is the 255 rows of
  ``exports/strategy-discovery/step-0/step-0-results.jsonl`` joined to
  ``registry/strategy-registry.yaml`` and the merged per-trade
  decision/application artifacts, plus the trade markdown source.
* ``PERFORMANCE.md``, ``TRADE_JOURNAL.md`` and any pre-reconstruction dashboard
  are **never** read here and can never reverse-determine strategy mapping,
  R values, or eligibility. Reconciliation is a downstream, read-only concern.
* No performance metric (win rate / expectancy / drawdown / Sharpe / equity) is
  computed in B1. Those are all B2.
* The recorded R value is authoritative when non-zero, or when it is zero for a
  breakeven outcome. A recorded zero on a non-breakeven outcome is a
  blank-equivalent fallback case; a derived R is used only when
  entry+SL+exit are unambiguous. A material recorded-vs-derived disagreement is
  flagged, never silently overwritten.
* The canonical dataset is a **superset** of the B2 performance sample. No row
  is ever dropped because performance cannot be computed; it is kept and marked
  ``analysis_eligibility = "ineligible"`` with a closed-vocabulary reason.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .registry import load_registry, registry_digest
from scripts.canonical_migration.frontmatter import parse_markdown

DATASET_SCHEMA_VERSION = "method-validation-b1.v2"
DEFAULT_DATASET_VERSION = "method-validation-b1.v2"
DEFAULT_OUTPUT_RELATIVE = Path("exports/method-validation/b1")

STEP0_RESULTS_PATH = "exports/strategy-discovery/step-0/step-0-results.jsonl"
STEP0_CORPUS_MANIFEST_PATH = "exports/strategy-discovery/step-0/corpus-manifest.json"
REGISTRY_PATH = "registry/strategy-registry.yaml"
PER_TRADE_APPLICATION_PATH = (
    "canonical/adjudications/strategy-mapping-review/"
    "strategy-mapping-per-trade-application-20260910.jsonl"
)
PER_TRADE_DECISIONS_PATH = (
    "canonical/adjudications/strategy-mapping-review/"
    "strategy-mapping-per-trade-decisions-20260910.jsonl"
)
RESIDUAL_LANE_HANDOFF_PATH = (
    "canonical/adjudications/strategy-mapping-review/"
    "strategy-mapping-residual-lane-handoff-20260910.jsonl"
)
FILENAME_DATE_POLICY_PATH = "canonical/adjudications/adj_filename_date_policy_20260901_v2.json"

# Source files whose taxonomy / aggregates predate the identity reconstruction.
# The generator MUST NOT read them. Tests grep the generator source for these.
FORBIDDEN_SOURCE_BASENAMES = ("PERFORMANCE.md", "TRADE_JOURNAL.md")

# --- closed vocabularies ----------------------------------------------------
OUTCOME_ENUM = ("win", "loss", "breakeven", "no_trade", "unknown")
STRATEGY_PERFORMANCE_OUTCOMES = frozenset({"win", "loss", "breakeven"})
SAMPLE_ORIGINS = ("backtest", "live", "unknown")
SAMPLE_ORIGIN_SOURCES = (
    "explicit_marker",
    "date_rule_pre2024_backtest",
    "date_rule_2024plus_live",
    "unknown",
)
# `{{token}}` names that only ever appear in a genuinely unfilled template stub
# (a real trade always has these filled in the frontmatter). Any one of them,
# or >=2 distinct tokens, means the row is a stub; a lone stray prose token
# (e.g. `{{timeframe_context}}` in a filled record) does not.
_STUB_PLACEHOLDER_TOKENS = frozenset(
    {"entry_price", "sl_price", "tp_price", "r_ratio", "r_realized", "result", "pair", "direction", "exit_price"}
)
_PLACEHOLDER_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z_]+)\s*\}\}")
ANALYSIS_ELIGIBILITY = ("eligible", "ineligible")
REALIZED_R_SOURCES = ("recorded", "derived", "none")
REALIZED_R_CONFIDENCE = ("high", "medium", "none")
INELIGIBILITY_REASONS = (
    "blocked_keep_blocked",
    "blocked_historical_cleaning_handoff",
    "blocked_position_management_handoff",
    "strategy_unmapped",
    "non_strategy_outcome",
    "missing_realized_r",
    "unresolved_episode_grouping",
)
DATA_QUALITY_FLAGS = (
    "frontmatter_absent",
    "strategy_label_blank",
    "placeholder_body",
    "negative_tp_price",
    "missing_entry_price",
    "missing_sl_price",
    "missing_exit_value",
    "blank_recorded_r",
    "unparseable_recorded_r",
    "noisy_recorded_r_raw",
    "zero_recorded_r",
    "realized_r_conflict",
    "outcome_normalized_from_nonstandard_raw",
    "filename_frontmatter_date_conflict",
    "date_from_adjudication_override",
    "sample_origin_date_rule_contested",
    "sample_origin_undeterminable",
    "possible_partial_or_scaled_fill",
)

_BLOCKED_DISPOSITION_TO_REASON = {
    "keep_blocked": "blocked_keep_blocked",
    "historical_cleaning_handoff": "blocked_historical_cleaning_handoff",
    "position_management_handoff": "blocked_position_management_handoff",
}

# Advisory only. A recorded-vs-derived R disagreement beyond
# ``gap > max(_R_CONFLICT_ABS_TOLERANCE, _R_CONFLICT_REL_TOLERANCE * |recorded|)``
# sets ``realized_r_conflict`` and keeps BOTH values. The recorded value always
# stays the canonical value; nothing is ever overwritten. Tightened in v2 from
# (0.5R, 25%) so a ~0.2R absolute or 10% relative disagreement is surfaced.
_R_CONFLICT_ABS_TOLERANCE = 0.15
_R_CONFLICT_REL_TOLERANCE = 0.10

_FILENAME_DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_EXIT_BODY_RE = re.compile(r"實際出場[:：]\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


class B1Error(ValueError):
    """Raised when a B1 invariant cannot be established safely."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_dump(value), encoding="utf-8", newline="\n")


def _write_jsonl(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
        newline="\n",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _number(value: Any) -> float | None:
    if value is None:
        return None
    match = _NUMBER_RE.search(str(value).replace(",", ""))
    return float(match.group()) if match else None


def _frontmatter(text: str) -> dict[str, str]:
    """Top-level ``key: value`` frontmatter, mirroring ``trading_os.parser``."""

    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith("---"):
        return {}
    match = re.match(r"^---\s*\n(.*?)\n---(?:\n|$)", text, re.S)
    if not match:
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "-", "\t")):
            key, value = line.split(":", 1)
            value = re.sub(r"\s+#.*$", "", value).strip()
            values[key.strip()] = value.strip("\"'")
    return values


def _strip_inline_comment(value: str) -> str:
    return re.sub(r"\s+#.*$", "", value).strip()


def _normalize_outcome(raw: str | None) -> tuple[str, bool]:
    """Return ``(outcome_enum, normalized_from_nonstandard_raw)``."""

    if raw is None:
        return "unknown", False
    token = _strip_inline_comment(raw).strip().lower()
    if token in {"win", "won"}:
        return "win", False
    if token in {"loss", "lose", "lost"}:
        return "loss", False
    if token in {"be", "breakeven", "break-even", "break even"}:
        return "breakeven", False
    if token in {"no-trade", "no trade", "notrade"}:
        return "no_trade", False
    if token in {"", "pending"}:
        return "unknown", token == "pending"
    # non-standard raw string -> keyword fallback, flagged.
    if "be-" in token or ("be" in token and "loss" in token):
        return "loss", True
    if re.search(r"\bbe\b", token) or "breakeven" in token or "break-even" in token or "break even" in token:
        return "breakeven", True
    if any(k in token for k in ("sl", "stop", "loss", "止損", "❌")):
        return "loss", True
    if any(k in token for k in ("won", "win", "tp", "止盈", "✅")):
        return "win", True
    if any(k in token for k in ("be", "breakeven")):
        return "breakeven", True
    return "unknown", True


def _direction(raw: str | None) -> str:
    token = (raw or "").strip().upper()
    if token in {"L", "LONG", "多"}:
        return "long"
    if token in {"S", "SHORT", "空"}:
        return "short"
    return "unknown"


def _backtest_body_evidence(text: str) -> bool:
    """Unambiguous backtest prose, e.g. '歷史回測數據匯入'."""

    return any(marker in text for marker in ("回測", "回测", "歷史回測", "歷史數據匯入", "backtest data import"))


def _explicit_sample_origin_marker(fm: Mapping[str, str], text: str) -> str | None:
    """Return ``"backtest"`` / ``"live"`` when an explicit marker is present, else ``None``.

    An explicit marker always wins over the event-year date rule. A ``backtest``
    tag or unambiguous backtest body prose beats a live marker on the same row.
    """

    tags = (fm.get("tags") or "").lower()
    type_field = (fm.get("type") or "").strip().lower()
    title_line = ""
    for line in text.splitlines():
        if line.startswith("# "):
            title_line = line.lower()
            break
    is_backtest = (
        "backtest" in tags
        or "(backtest)" in title_line
        or "（回測" in title_line
        or _backtest_body_evidence(text)
    )
    is_live = (
        type_field in {"實盤", "实盘", "live"}
        or "實盤" in tags
        or "propw" in text.lower()
    )
    if is_backtest:
        return "backtest"
    if is_live:
        return "live"
    return None


def _resolve_sample_origin(
    fm: Mapping[str, str], text: str, event_date: str | None
) -> tuple[str, str, set[str]]:
    """Return ``(sample_origin, sample_origin_source, extra_flags)``.

    Owner ruling (2026-09-10): explicit marker wins; otherwise the unmarked row
    is dated by event year (``event_date`` is the filename date per
    ``adj_filename_date_policy_20260901_v2``): year <= 2023 -> backtest,
    year >= 2024 -> live.
    """

    marker = _explicit_sample_origin_marker(fm, text)
    if marker is not None:
        return marker, "explicit_marker", set()

    year: int | None = None
    if event_date and len(event_date) >= 4 and event_date[:4].isdigit():
        year = int(event_date[:4])
    if year is None:
        return "unknown", "unknown", {"sample_origin_undeterminable"}

    if year <= 2023:
        return "backtest", "date_rule_pre2024_backtest", set()
    # year >= 2024 -> live by rule; flag if there is weak contrary backtest hint
    flags: set[str] = set()
    if _backtest_body_evidence(text) or "backtest" in text.lower():
        flags.add("sample_origin_date_rule_contested")
    return "live", "date_rule_2024plus_live", flags


def _exit_value(fm: Mapping[str, str], body: str) -> float | None:
    fm_exit = _number(fm.get("exit_price"))
    if fm_exit is not None:
        return fm_exit
    match = _EXIT_BODY_RE.search(body)
    if match:
        return _number(match.group(1))
    return None


def _partial_fill_suspected(outcome_raw: str | None, body: str) -> bool:
    token = (outcome_raw or "").lower()
    if "partial" in token or "分批" in token:
        return True
    markers = ("分批平倉", "分批平仓", "分批出場", "加倉", "加仓", "scale-in", "scale in", "部分平倉", "部分平仓", "partial close")
    return any(marker in body for marker in markers)


def _registry_index(registry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for strategy in registry.get("strategies", []):
        sid = strategy.get("strategy_id")
        if isinstance(sid, str):
            index[sid] = dict(strategy)
    return index


def _blocked_dispositions(application_rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in application_rows:
        disposition = row.get("excluded_disposition")
        trade_ref = row.get("trade_ref")
        if disposition and isinstance(trade_ref, str):
            out[trade_ref] = disposition
    return out


def _handoff_notes(handoff_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in handoff_rows:
        trade_ref = row.get("trade_ref")
        if not isinstance(trade_ref, str):
            continue
        out[trade_ref] = {
            "target_lane": row.get("target_lane"),
            "human_decision": row.get("human_decision"),
            "note": row.get("note"),
        }
    return out


def load_date_policy(root: Path) -> dict[str, dict[str, str]]:
    """Consume ``adj_filename_date_policy_20260901_v2``.

    The policy's machine-consumable content is (1) the rule that
    ``event_date`` comes from the filename and the frontmatter ``date`` is the
    ``recorded_date`` (asserted here so the pinned digest actually gates
    behavior), and (2) an explicit per-trade ``dates`` override map for the
    small set of records the owner adjudicated individually.
    """

    payload = json.loads((root / FILENAME_DATE_POLICY_PATH).read_text(encoding="utf-8"))
    after = payload.get("after", {})
    if after.get("event_date_source") != "filename_date":
        raise B1Error("date policy event_date_source is not 'filename_date'; generator assumptions invalid")
    if after.get("frontmatter_date_semantics") != "recorded_date":
        raise B1Error("date policy frontmatter_date_semantics is not 'recorded_date'")
    overrides: dict[str, dict[str, str]] = {}
    for trade_ref, dates in (after.get("dates") or {}).items():
        overrides[trade_ref] = {
            "event_date": dates.get("event_date"),
            "recorded_date": dates.get("recorded_date"),
        }
    return overrides


def _build_row(
    *,
    root: Path,
    step0: Mapping[str, Any],
    source_text: str,
    source_body: str,
    registry_index: Mapping[str, Mapping[str, Any]],
    blocked_dispositions: Mapping[str, str],
    handoff_notes: Mapping[str, Mapping[str, Any]],
    date_policy_overrides: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    trade_ref = step0["trade_ref"]
    source_path = step0.get("source_path", trade_ref)
    path = (root / source_path).resolve()
    text = source_text
    fm = _frontmatter(text)
    body = source_body

    flags: set[str] = set()
    if not fm:
        flags.add("frontmatter_absent")

    # --- dates (adj_filename_date_policy_20260901_v2: filename=event, frontmatter=recorded)
    filename_match = _FILENAME_DATE_RE.search(path.name)
    event_date = filename_match.group(1) if filename_match else None
    recorded_date = (fm.get("date") or "").strip() or None
    override = date_policy_overrides.get(trade_ref)
    if override:
        event_date = override.get("event_date") or event_date
        recorded_date = override.get("recorded_date") or recorded_date
        flags.add("date_from_adjudication_override")
    if event_date and recorded_date and event_date != recorded_date:
        flags.add("filename_frontmatter_date_conflict")

    # --- prices
    entry_price = _number(fm.get("entry_price") or fm.get("entry"))
    sl_price = _number(fm.get("sl_price") or fm.get("sl"))
    tp_price = _number(fm.get("tp_price") or fm.get("tp"))
    exit_price = _exit_value(fm, body)
    if entry_price is None:
        flags.add("missing_entry_price")
    if sl_price is None:
        flags.add("missing_sl_price")
    if exit_price is None:
        flags.add("missing_exit_value")
    if tp_price is not None and tp_price < 0:
        flags.add("negative_tp_price")

    direction = _direction(fm.get("direction") or fm.get("side"))
    instrument = (fm.get("pair") or "unknown").strip() or "unknown"
    timeframe_trigger = (fm.get("timeframe_trigger") or "").strip() or None
    timeframe_context = (fm.get("timeframe_context") or "").strip() or None
    time_entry = (fm.get("time_entry") or "").strip() or None

    # --- outcome
    outcome_raw = fm.get("result")
    outcome, nonstandard = _normalize_outcome(outcome_raw)
    if nonstandard:
        flags.add("outcome_normalized_from_nonstandard_raw")

    # --- placeholder body: a genuinely unfilled template stub, not one stray
    # `{{...}}` token in an otherwise fully-filled record.
    placeholder_tokens = _PLACEHOLDER_TOKEN_RE.findall(text)
    distinct_tokens = set(placeholder_tokens)
    if placeholder_tokens and (
        len(distinct_tokens) >= 2 or (distinct_tokens & _STUB_PLACEHOLDER_TOKENS)
    ):
        flags.add("placeholder_body")
    if not (fm.get("strategy") or "").strip():
        flags.add("strategy_label_blank")

    # --- R schema
    recorded_raw = fm.get("r_realized")
    recorded_realized_r: float | None = None
    _clean_r_re = re.compile(r"^-?\d+(?:\.\d+)?$")
    _blank_tokens = {"", "null", "none", "n/a", "na", "-", "tbd", "pending"}
    if recorded_raw is None or recorded_raw.strip().lower() in _blank_tokens:
        flags.add("blank_recorded_r")
    else:
        parsed = _number(recorded_raw)
        if parsed is None:
            flags.add("unparseable_recorded_r")
        else:
            recorded_realized_r = round(parsed, 6)
            if not _clean_r_re.match(_strip_inline_comment(recorded_raw).strip()):
                flags.add("noisy_recorded_r_raw")
            if recorded_realized_r == 0:
                flags.add("zero_recorded_r")

    derived_realized_r: float | None = None
    if entry_price is not None and sl_price is not None and exit_price is not None:
        risk = abs(entry_price - sl_price)
        if risk > 0:
            if direction == "long":
                derived_realized_r = round((exit_price - entry_price) / risk, 3)
            elif direction == "short":
                derived_realized_r = round((entry_price - exit_price) / risk, 3)

    # Recorded wins. A recorded, non-zero value is authoritative and is never
    # recomputed. A recorded 0 is authoritative ONLY for a breakeven outcome
    # (0R is the correct BE value); a recorded 0 on a win/loss/other outcome is
    # a not-yet-backfilled field, treated as blank-equivalent -> derive if the
    # price geometry is unambiguous, else the row carries no canonical R.
    # The `recorded_realized_r` field always preserves the literal recorded
    # value; only `canonical_realized_r` may differ.
    recorded_zero_is_credible = recorded_realized_r == 0 and outcome == "breakeven"
    if recorded_realized_r is not None and (recorded_realized_r != 0 or recorded_zero_is_credible):
        canonical_realized_r: float | None = recorded_realized_r
        realized_r_source = "recorded"
        realized_r_confidence = "high"
    elif derived_realized_r is not None:
        canonical_realized_r = derived_realized_r
        realized_r_source = "derived"
        realized_r_confidence = "medium"
    else:
        canonical_realized_r = None
        realized_r_source = "none"
        realized_r_confidence = "none"

    realized_r_conflict = False
    if recorded_realized_r not in (None, 0) and derived_realized_r is not None:
        gap = abs(recorded_realized_r - derived_realized_r)
        tolerance = max(_R_CONFLICT_ABS_TOLERANCE, _R_CONFLICT_REL_TOLERANCE * abs(recorded_realized_r))
        if gap > tolerance:
            realized_r_conflict = True
            flags.add("realized_r_conflict")

    # --- strategy identity (step-0 authoritative; registry for family + lifecycle)
    mapping = step0.get("strategy_mapping") or {}
    strategy_id = mapping.get("strategy_id")
    match_type = mapping.get("match_type")
    decision_id = mapping.get("decision_id")
    parent_strategy_id = None
    family_strategy_id = None
    strategy_status = None
    strategy_classification = None
    if strategy_id and strategy_id in registry_index:
        entry = registry_index[strategy_id]
        strategy_status = entry.get("status")
        strategy_classification = entry.get("classification")
        registry_parent = entry.get("parent_strategy_id")
        parent_strategy_id = registry_parent or strategy_id
        family_strategy_id = registry_parent or strategy_id

    # --- episode / partial-fill linkage (evidence-driven only; never guessed)
    step0_record_id = step0.get("record_id")
    episode_id = step0_record_id
    episode_note = None
    related_record_refs: list[str] = []
    if _partial_fill_suspected(outcome_raw, body):
        flags.add("possible_partial_or_scaled_fill")
    handoff = handoff_notes.get(trade_ref)
    if handoff and handoff.get("target_lane") == "position-management":
        flags.add("possible_partial_or_scaled_fill")
        episode_note = handoff.get("note")

    # --- sample origin (explicit marker wins; else event-year date rule)
    sample_origin, sample_origin_source, sample_origin_flags = _resolve_sample_origin(
        fm, text, event_date
    )
    flags |= sample_origin_flags

    # --- eligibility (closed vocabulary; superset dataset, B2 sample is a subset)
    blocked_disposition = blocked_dispositions.get(trade_ref)
    reasons: list[str] = []
    if blocked_disposition in _BLOCKED_DISPOSITION_TO_REASON:
        reasons.append(_BLOCKED_DISPOSITION_TO_REASON[blocked_disposition])
    if strategy_id is None and not reasons:
        reasons.append("strategy_unmapped")
    if "possible_partial_or_scaled_fill" in flags:
        reasons.append("unresolved_episode_grouping")
    if outcome not in STRATEGY_PERFORMANCE_OUTCOMES:
        reasons.append("non_strategy_outcome")
    if canonical_realized_r is None:
        reasons.append("missing_realized_r")

    # de-dup, preserve declared priority order
    ordered_reasons = [reason for reason in INELIGIBILITY_REASONS if reason in reasons]
    analysis_eligibility = "ineligible" if ordered_reasons else "eligible"
    ineligibility_reason = ordered_reasons[0] if ordered_reasons else None

    return {
        "schema_version": DATASET_SCHEMA_VERSION,
        "trade_ref": trade_ref,
        "step0_record_id": step0_record_id,
        "source_path": source_path,
        "source_sha256": step0.get("source_sha256"),
        "source_body_sha256": step0.get("source_body_sha256"),
        # identity
        "strategy_id": strategy_id,
        "parent_strategy_id": parent_strategy_id,
        "family_strategy_id": family_strategy_id,
        "strategy_status": strategy_status,
        "strategy_classification": strategy_classification,
        "legacy_strategy_label": step0.get("legacy_strategy_label"),
        "frontmatter_strategy_label": (fm.get("strategy") or "").strip() or None,
        "match_type": match_type,
        "decision_id": decision_id,
        "step0_gate_outcome": step0.get("step0_gate_outcome"),
        "step0_review_eligibility": step0.get("review_eligibility"),
        "blocked_disposition": blocked_disposition,
        # trade facts
        "direction": direction,
        "instrument": instrument,
        "timeframe_trigger": timeframe_trigger,
        "timeframe_context": timeframe_context,
        "event_date": event_date,
        "recorded_date": recorded_date,
        "time_entry": time_entry,
        "entry_price": entry_price,
        "stop_loss_price": sl_price,
        "take_profit_price": tp_price,
        "exit_price": exit_price,
        "planned_rr": _number(fm.get("r_ratio") or fm.get("rr")),
        # outcome
        "outcome": outcome,
        "outcome_raw": outcome_raw,
        # R schema (recorded wins; derived is fallback only; conflicts flagged)
        "recorded_realized_r": recorded_realized_r,
        "recorded_realized_r_raw": recorded_raw,
        "derived_realized_r": derived_realized_r,
        "canonical_realized_r": canonical_realized_r,
        "realized_r_source": realized_r_source,
        "realized_r_confidence": realized_r_confidence,
        "realized_r_conflict": realized_r_conflict,
        # sample partitioning
        "sample_origin": sample_origin,
        "sample_origin_source": sample_origin_source,
        "episode_id": episode_id,
        "episode_note": episode_note,
        "related_record_refs": related_record_refs,
        # eligibility
        "analysis_eligibility": analysis_eligibility,
        "ineligibility_reason": ineligibility_reason,
        "ineligibility_reasons": ordered_reasons,
        "data_quality_flags": sorted(flags),
        # provenance
        "provenance": {
            "dataset_generator": "trading_os.method_validation.run_b1_dataset",
            "universe_authority": STEP0_RESULTS_PATH,
            "registry_authority": REGISTRY_PATH,
            "per_trade_decision_authority": PER_TRADE_APPLICATION_PATH,
            "date_policy_authority": FILENAME_DATE_POLICY_PATH,
            "step0_match_type": match_type,
            "step0_decision_id": decision_id,
        },
    }


def _counter_dict(values: Any) -> dict[str, int]:
    return dict(sorted(Counter(values).items(), key=lambda item: (str(item[0]))))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _load_verified_source(root: Path, step0: Mapping[str, Any]) -> tuple[str, str]:
    """Read one source and fail closed if either Step-0 hash has drifted."""

    source_path = step0.get("source_path")
    trade_ref = step0.get("trade_ref")
    pinned_source_sha = step0.get("source_sha256")
    pinned_body_sha = step0.get("source_body_sha256")
    if not isinstance(source_path, str) or not source_path:
        raise B1Error(f"Step-0 source path missing: {trade_ref}")
    if not isinstance(pinned_source_sha, str) or not pinned_source_sha:
        raise B1Error(f"Step-0 source hash missing: {source_path}")
    if not isinstance(pinned_body_sha, str) or not pinned_body_sha:
        raise B1Error(f"Step-0 source body hash missing: {source_path}")

    project_root = root.resolve()
    path = (project_root / source_path).resolve()
    if not _is_within(path, project_root):
        raise B1Error(f"Step-0 source path escapes project root: {source_path}")
    from .legacy_source import LegacySourceError, read_legacy_bytes
    try:
        raw_bytes = read_legacy_bytes(project_root, path.relative_to(project_root))
    except LegacySourceError as exc:
        raise B1Error(f"Step-0 source not found: {source_path}") from exc
    current_source_sha = _sha256_bytes(raw_bytes)
    if current_source_sha != pinned_source_sha:
        raise B1Error(
            f"Step-0 source hash mismatch: {source_path} "
            f"(pinned {pinned_source_sha}, current {current_source_sha})"
        )

    text = raw_bytes.decode("utf-8", errors="replace")
    parsed = parse_markdown(text)
    current_body_sha = _sha256_bytes(parsed.body.encode("utf-8"))
    if current_body_sha != pinned_body_sha:
        raise B1Error(
            f"Step-0 source body hash mismatch: {source_path} "
            f"(pinned {pinned_body_sha}, current {current_body_sha})"
        )
    return text, parsed.body


def build_dataset(root: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return ``(rows, pinned_source_digests)`` without writing anything."""

    root = root.resolve()
    step0_path = root / STEP0_RESULTS_PATH
    if not step0_path.exists():
        raise B1Error(f"authoritative universe not found: {STEP0_RESULTS_PATH}")
    step0_rows = _read_jsonl(step0_path)
    if len(step0_rows) != 255:
        raise B1Error(f"expected 255 step-0 records, found {len(step0_rows)}")

    registry = load_registry(root)
    registry_index = _registry_index(registry)

    application_rows = _read_jsonl(root / PER_TRADE_APPLICATION_PATH)
    handoff_rows = _read_jsonl(root / RESIDUAL_LANE_HANDOFF_PATH)
    blocked_dispositions = _blocked_dispositions(application_rows)
    handoff_notes = _handoff_notes(handoff_rows)
    date_policy_overrides = load_date_policy(root)

    # Validate every source before constructing any output row. The hashes in
    # Step-0 are immutable provenance pins, not values to copy blindly into a
    # new artifact.
    verified_sources = {
        step0["trade_ref"]: _load_verified_source(root, step0) for step0 in step0_rows
    }
    rows = [
        _build_row(
            root=root,
            step0=step0,
            source_text=verified_sources[step0["trade_ref"]][0],
            source_body=verified_sources[step0["trade_ref"]][1],
            registry_index=registry_index,
            blocked_dispositions=blocked_dispositions,
            handoff_notes=handoff_notes,
            date_policy_overrides=date_policy_overrides,
        )
        for step0 in step0_rows
    ]
    rows.sort(key=lambda row: (row["source_path"], row["step0_record_id"] or ""))

    ordered_corpus_material = [
        [row["trade_ref"], row["source_sha256"], row["source_body_sha256"]] for row in rows
    ]
    pinned = {
        "step0_results_sha256": _sha256_file(step0_path),
        "step0_corpus_manifest_sha256": _sha256_file(root / STEP0_CORPUS_MANIFEST_PATH),
        "registry_digest": registry_digest(registry),
        "registry_file_sha256": _sha256_file(root / REGISTRY_PATH),
        "per_trade_application_sha256": _sha256_file(root / PER_TRADE_APPLICATION_PATH),
        "per_trade_decisions_sha256": _sha256_file(root / PER_TRADE_DECISIONS_PATH),
        "residual_lane_handoff_sha256": _sha256_file(root / RESIDUAL_LANE_HANDOFF_PATH),
        "filename_date_policy_sha256": _sha256_file(root / FILENAME_DATE_POLICY_PATH),
        "dataset_source_list_digest": _sha256_bytes(
            _canonical_bytes(sorted(row["source_path"] for row in rows))
        ),
        "dataset_ordered_corpus_digest": _sha256_bytes(_canonical_bytes(ordered_corpus_material)),
    }
    return rows, pinned


def _manifest(
    *,
    rows: Sequence[Mapping[str, Any]],
    pinned: Mapping[str, str],
    dataset_version: str,
    generated_at: str,
    source_commit: str,
) -> dict[str, Any]:
    eligible = [row for row in rows if row["analysis_eligibility"] == "eligible"]
    manifest: dict[str, Any] = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "generated_at": generated_at,
        "source_commit": source_commit,
        "mode": "method_validation_b1_canonical_dataset",
        "read_only_inputs": True,
        "computes_performance_metrics": False,
        "hard_rules": {
            "authoritative_universe": STEP0_RESULTS_PATH,
            "old_dashboards_are_not_authority": list(FORBIDDEN_SOURCE_BASENAMES),
            "recorded_r_field_never_overwritten": True,
            "recorded_zero_breakeven_is_authoritative": True,
            "recorded_zero_non_breakeven_is_blank_equivalent": True,
            "derived_r_requires_unambiguous_entry_sl_exit": True,
            "source_sha256_must_match_step0": True,
            "source_body_sha256_must_match_step0": True,
            "source_commit_must_be_resolved": True,
            "dataset_is_superset_of_b2_sample": True,
        },
        "sample_origin_rule": {
            "explicit_marker_wins": True,
            "unmarked_event_year_le_2023": "backtest",
            "unmarked_event_year_ge_2024": "live",
            "event_date_source": "filename_date (adj_filename_date_policy_20260901_v2)",
        },
        "realized_r_conflict_threshold": {
            "abs_r": _R_CONFLICT_ABS_TOLERANCE,
            "rel_fraction_of_recorded": _R_CONFLICT_REL_TOLERANCE,
            "formula": "flag when |recorded - derived| > max(abs_r, rel_fraction * |recorded|); advisory only, recorded value stays canonical",
        },
        "closed_vocabularies": {
            "outcome": list(OUTCOME_ENUM),
            "sample_origin": list(SAMPLE_ORIGINS),
            "sample_origin_source": list(SAMPLE_ORIGIN_SOURCES),
            "analysis_eligibility": list(ANALYSIS_ELIGIBILITY),
            "ineligibility_reason": list(INELIGIBILITY_REASONS),
            "realized_r_source": list(REALIZED_R_SOURCES),
            "data_quality_flags": list(DATA_QUALITY_FLAGS),
        },
        "pinned_source_digests": dict(sorted(pinned.items())),
        "row_count_invariants": {
            "total_rows": len(rows),
            "expected_total_rows": 255,
            "by_analysis_eligibility": _counter_dict(row["analysis_eligibility"] for row in rows),
            "by_ineligibility_reason": _counter_dict(
                row["ineligibility_reason"] for row in rows if row["ineligibility_reason"]
            ),
            "by_sample_origin": _counter_dict(row["sample_origin"] for row in rows),
            "by_sample_origin_source": _counter_dict(row["sample_origin_source"] for row in rows),
            "sample_origin_unknown_rows": sum(1 for row in rows if row["sample_origin"] == "unknown"),
            "sample_origin_date_rule_contested_rows": sum(
                1 for row in rows if "sample_origin_date_rule_contested" in row["data_quality_flags"]
            ),
            "by_strategy_id": _counter_dict(str(row["strategy_id"]) for row in rows),
            "by_family_strategy_id": _counter_dict(str(row["family_strategy_id"]) for row in rows),
            "by_strategy_status": _counter_dict(str(row["strategy_status"]) for row in rows),
            "by_step0_gate_outcome": _counter_dict(row["step0_gate_outcome"] for row in rows),
            "by_outcome": _counter_dict(row["outcome"] for row in rows),
            "by_realized_r_source": _counter_dict(row["realized_r_source"] for row in rows),
            "realized_r_conflicts": sum(1 for row in rows if row["realized_r_conflict"]),
            "placeholder_body_rows": sum(
                1 for row in rows if "placeholder_body" in row["data_quality_flags"]
            ),
            "placeholder_body_rows_eligible": sum(
                1
                for row in rows
                if "placeholder_body" in row["data_quality_flags"]
                and row["analysis_eligibility"] == "eligible"
            ),
            "possible_partial_or_scaled_fill_rows": sum(
                1 for row in rows if "possible_partial_or_scaled_fill" in row["data_quality_flags"]
            ),
            "b2_eligible_by_strategy_id": _counter_dict(str(row["strategy_id"]) for row in eligible),
            "b2_eligible_by_sample_origin": _counter_dict(row["sample_origin"] for row in eligible),
        },
        "outputs": {
            "dataset": {"path": "canonical-trade-dataset.jsonl", "record_count": len(rows)},
            "manifest": {"path": "manifest.json"},
            "readme": {"path": "README.md"},
        },
    }
    manifest["dataset_sha256"] = None  # filled after the dataset file is written
    manifest["manifest_digest"] = None
    return manifest


def _readme(manifest: Mapping[str, Any]) -> str:
    inv = manifest["row_count_invariants"]
    lines = [
        "# Method Validation — B1 canonical analysis dataset",
        "",
        "**Derived, non-authoritative narrative.** The authority is",
        "`canonical-trade-dataset.jsonl` (one row per trade) and `manifest.json`.",
        "If this README and the JSONL disagree, the JSONL wins.",
        "",
        f"- `dataset_version`: `{manifest['dataset_version']}`",
        f"- `generated_at`: `{manifest['generated_at']}`",
        f"- `source_commit`: `{manifest['source_commit']}`",
        "",
        "## What this is",
        "",
        "The 255-row Step-0 clean universe",
        "(`exports/strategy-discovery/step-0/step-0-results.jsonl`) joined to the strategy",
        "registry, the merged per-trade strategy-mapping decision/application artifacts, and",
        "the trade markdown source. One row per trade, every field a per-trade fact or a",
        "provenance/quality marker.",
        "",
        "This dataset is the **superset**. The Phase B2 performance sample is the subset of",
        "rows with `analysis_eligibility == \"eligible\"`. No row is ever dropped because a",
        "metric cannot be computed — it stays, marked ineligible with a closed-vocabulary",
        "reason.",
        "",
        "## What this is NOT",
        "",
        "- **No performance metric** is computed here (win rate / expectancy / avg R /",
        "  drawdown / Sharpe / equity). All of that is B2.",
        "- `PERFORMANCE.md`, `TRADE_JOURNAL.md` and any pre-reconstruction dashboard use the",
        "  old 13-label taxonomy and a stale 213/214-trade count. They are **not read** by",
        "  the generator and can never reverse-determine strategy mapping, R, or eligibility.",
        "- `recorded_realized_r` is preserved verbatim and never overwritten. A non-zero",
        "  recorded R is authoritative; recorded `0` is authoritative for `breakeven`, but",
        "  is blank-equivalent for a non-breakeven outcome. In that case,",
        "  `derived_realized_r` is a fallback only when entry+SL+exit are unambiguous;",
        "  a disagreement beyond "
        f"`max({_R_CONFLICT_ABS_TOLERANCE}R, {int(_R_CONFLICT_REL_TOLERANCE * 100)}% * |recorded|)` "
        "sets the advisory `realized_r_conflict` flag and keeps both values.",
        "- No trade file, registry entry, or Step-0 artifact is modified.",
        "",
        "## Current integrity contract",
        "",
        "- Every Step-0 source path must exist and its current full-file SHA-256 and",
        "  canonical parsed-body SHA-256 must equal the Step-0 pins; any drift fails closed.",
        "- A single leading UTF-8 BOM is ignored for frontmatter recognition and body parsing",
        "  but source bytes and source hashes remain untouched. Malformed frontmatter remains",
        "  ineligible rather than being coerced.",
        "- Inline comments are removed only when `#` is separated from the scalar by whitespace",
        "  (for example, `direction: L  # comment` becomes `L`).",
        "- `sample_origin` uses explicit markers first; otherwise the pinned filename-date policy",
        "  assigns `<= 2023` to `backtest` and `>= 2024` to `live`.",
        "- `placeholder_body` identifies genuine unfilled template stubs; a lone stray prose",
        "  token does not trip it. The v1-to-v2 migration retained this behavior.",
        "- `realized_r_conflict` is advisory: it keeps recorded and derived values and leaves",
        f"  the recorded value canonical when the gap exceeds `max({_R_CONFLICT_ABS_TOLERANCE}R, {int(_R_CONFLICT_REL_TOLERANCE * 100)}% * |recorded|)`.",
        "",
        "## Row-count breakdown",
        "",
        f"- total rows: **{inv['total_rows']}** (expected {inv['expected_total_rows']})",
        f"- by `analysis_eligibility`: {json.dumps(inv['by_analysis_eligibility'], ensure_ascii=False)}",
        f"- by `ineligibility_reason`: {json.dumps(inv['by_ineligibility_reason'], ensure_ascii=False)}",
        f"- by `sample_origin`: {json.dumps(inv['by_sample_origin'], ensure_ascii=False)}",
        f"- by `sample_origin_source`: {json.dumps(inv['by_sample_origin_source'], ensure_ascii=False)}",
        f"- `sample_origin == unknown` rows: **{inv['sample_origin_unknown_rows']}**; "
        f"date-rule-contested rows: **{inv['sample_origin_date_rule_contested_rows']}**",
        f"- by `strategy_status`: {json.dumps(inv['by_strategy_status'], ensure_ascii=False)}",
        f"- by `realized_r_source`: {json.dumps(inv['by_realized_r_source'], ensure_ascii=False)}",
        f"- recorded-vs-derived R conflicts flagged: **{inv['realized_r_conflicts']}**",
        f"- placeholder-body rows: **{inv['placeholder_body_rows']}** "
        f"({inv['placeholder_body_rows_eligible']} still B2-eligible on field completeness)",
        f"- rows flagged possible partial/scaled fill: **{inv['possible_partial_or_scaled_fill_rows']}**",
        "",
        "### By `strategy_id` (all rows)",
        "",
        "| strategy_id | rows | b2-eligible |",
        "|---|---:|---:|",
    ]
    b2 = inv["b2_eligible_by_strategy_id"]
    for strategy_id, count in sorted(inv["by_strategy_id"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {strategy_id} | {count} | {b2.get(strategy_id, 0)} |")
    lines += [
        "",
        "## Regeneration",
        "",
        "```",
        "python -m trading_os b1-dataset \\",
        f"  --dataset-version {manifest['dataset_version']} \\",
        f"  --generated-at {manifest['generated_at']} \\",
        f"  --source-commit {manifest['source_commit']}",
        "```",
        "",
        "Deterministic: same inputs + same arguments → byte-identical output.",
        "",
    ]
    return "\n".join(lines)


def run_b1_dataset(
    root: Path,
    *,
    dataset_version: str = DEFAULT_DATASET_VERSION,
    generated_at: str,
    source_commit: str,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Build and write the B1 canonical dataset, manifest, and README."""

    root = root.resolve()
    if output_dir is None:
        output_dir = root / DEFAULT_OUTPUT_RELATIVE
    elif not Path(output_dir).is_absolute():
        output_dir = root / output_dir
    output_dir = Path(output_dir).resolve()

    rows, pinned = build_dataset(root)
    manifest = _manifest(
        rows=rows,
        pinned=pinned,
        dataset_version=dataset_version,
        generated_at=generated_at,
        source_commit=source_commit,
    )

    dataset_path = output_dir / "canonical-trade-dataset.jsonl"
    _write_jsonl(dataset_path, rows)
    manifest["dataset_sha256"] = _sha256_file(dataset_path)

    unsigned = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    manifest["manifest_digest"] = _sha256_bytes(_canonical_bytes(unsigned))

    _write_json(output_dir / "manifest.json", manifest)
    (output_dir / "README.md").write_text(_readme(manifest), encoding="utf-8", newline="\n")
    return manifest
