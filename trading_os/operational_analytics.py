"""Incremental operational analytics over the frozen B1 v3 seed.

This is intentionally separate from B1/B2 generation.  It consumes the
versioned B1 v3 dataset as an immutable historical seed and adds only
post-cutover canonical lifecycles that have an explicit operational capture
marker.  A legacy canonicalization is linked to its B1 episode by exact
source lineage and contributes zero additional observations.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Mapping

from .evidence_store import (
    EVIDENCE_BINDING_FIELDS,
    EVIDENCE_BINDING_OPTIONAL_FIELDS,
    EVIDENCE_BINDING_REQUIRED_FIELDS,
    evidence_identity,
)
from .historical_attribution import (
    REGISTRY_VERSION as RETROSPECTIVE_ATTRIBUTION_REGISTRY_VERSION,
)
from .instruments import base_asset
from .operational_lifecycle import LifecycleStore, derive_rule_violation
from .registry import (
    FREEZE_SCHEMA_VERSION,
    load_effective_rules,
    load_registry,
    load_version,
    registry_digest,
    strategy_version_digest,
)
from .strategy_resolver import OWNER_RETROSPECTIVE_AUTHORITY
from .vocabularies import (
    BACKTEST_STRONG_EVIDENCE_TYPES,
    GOVERNED_BACKTEST_ENTRY_BASES,
    PLAN_FIELD_UNKNOWN_VALUES,
    UNFROZEN_EXPERIMENTAL_PLACEHOLDER,
)

# BACKTEST-only exit authority classes (operational_lifecycle.
# EXIT_AUTHORITY_CLASSES) that already carry a formal governed/deterministic
# calculation and therefore need no additional durable-evidence check here --
# only the minimal "owner_attestation" basis (a bare owner claim) is put
# through the strong-evidence predicate below.
_BACKTEST_GOVERNED_EXIT_AUTHORITIES = {"deterministic_execution_calculation", "governed_manual_adjudication"}


ANALYTICS_SCHEMA = "operational-analysis-universe.v1"
MANIFEST_SCHEMA = "operational-analytics-manifest.v1"
DEFAULT_OUTPUT = Path("exports/operational/analytics")
PERFORMANCE_PATH = Path("PERFORMANCE.md")
# Bump a feature when the generator changes what committed projections say;
# verify_projections then reports them stale until they are refreshed.
ANALYTICS_GENERATOR = {"instrument_grouping": "base_asset.v1"}


class OperationalAnalyticsError(ValueError):
    """Raised when the rolling universe cannot be established safely."""


# PropW reports a close row's netProfit after the *close* fee only. The open
# fee is a separate "Open Fee" bill (the open row's netProfit is 0) and funding
# is a separate signed "Funding Fee" bill whose note names the position
# ("仓位id:<openId>"). Account P&L for one position is therefore
#   provider_pnl (close netProfit) - open_fee + funding (signed, + = received).
PROVIDER_LEDGER_PATH = "provider-ledgers/propw/normalized/trade-lifecycles.jsonl"
PROVIDER_TRADE_EVENTS_DIR = "provider-ledgers/propw/raw/trade-events"
PROVIDER_BILL_EVENTS_DIR = "provider-ledgers/propw/raw/bill-events"
_FUNDING_POSITION_RE = re.compile(r"仓位id[:：]\s*(\d+)")
# PropW settles funding at 00:00, 08:00 and 16:00 UTC.
_FUNDING_INTERVAL_MS = 8 * 3600 * 1000
# Sub-cent agreement: the trade event's fee is the bill amount rounded to 4 dp.
_PNL_TOLERANCE = 1e-6
_FEE_TOLERANCE = 1e-4


def _payloads(directory: Path) -> tuple[list[Path], list[dict[str, Any]], list[dict[str, Any]]]:
    """Provider rows, plus the rows that cannot be trusted as single facts.

    An id repeated with identical content is one row. The same id with different
    content, or a row with no id, is returned in the third list (every version)
    instead of being overwritten or pooled under one key.
    """
    files = sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
    by_id: dict[str, list[dict[str, Any]]] = {}
    untrusted: list[dict[str, Any]] = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line).get("payload") or {}
            if payload.get("id") in (None, ""):
                untrusted.append(payload)
                continue
            versions = by_id.setdefault(str(payload["id"]), [])
            if payload not in versions:
                versions.append(payload)
    rows = []
    for versions in by_id.values():
        if len(versions) == 1:
            rows.append(versions[0])
        else:
            untrusted.extend(versions)
    return files, rows, untrusted


def _epoch_ms(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def provider_fee_index(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Per provider position: ledger close P&L, open fee and signed funding.

    - open fee: the one "Open Fee" bill at the open event's instrument and
      timestamp; with no such bill, the trade event's own fee (PropW rounds it
      to 4 dp). A bill that disagrees with the event, or several, leave it None.
    - funding: the sum of the position's funding bills. None when the data is
      contradictory (a bill outside the hold, or one naming no position).
      ``funding_complete`` is False when an 8-hour settlement the position was
      held across has no bill: the sum may then miss a bill, and that is
      disclosed rather than read as a proven total.
    """
    ledger = root / PROVIDER_LEDGER_PATH
    positions: dict[str, Mapping[str, Any]] = {}
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                positions[str(row.get("open_id"))] = row.get("lifecycle") if isinstance(row.get("lifecycle"), Mapping) else {}
    trade_files, trade_events, untrusted_events = _payloads(root / PROVIDER_TRADE_EVENTS_DIR)
    bill_files, bills, untrusted_bills = _payloads(root / PROVIDER_BILL_EVENTS_DIR)
    opens: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in trade_events:
        if event.get("status") == "open":
            opens[str(event.get("openId"))].append(event)
    # Positions with a conflicting or id-less trade event cannot have a proven open fee.
    poisoned_positions = {str(event.get("openId")) for event in untrusted_events if event.get("openId") is not None}
    # One bill belongs to one position: several opens at one instrument and instant cannot be told apart.
    opens_at: dict[tuple[str, int | None], int] = defaultdict(int)
    for open_id, rows_for_position in opens.items():
        opens_at[(str(rows_for_position[0].get("instrument") or "").upper(), _epoch_ms(rows_for_position[0].get("createdDate")))] += 1
    # Every Open Fee row is kept, malformed ones as None, so a bad row is ambiguous, not absent.
    open_fee_bills: dict[tuple[str, int | None], list[float | None]] = defaultdict(list)
    funding: dict[str, list[tuple[int | None, float]]] = defaultdict(list)
    unattributed = 0
    # Funding bills that name no usable position: (instrument or None, time or None).
    unattributed_bills: list[tuple[str | None, int | None]] = []
    # Conflicting or id-less bills are ambiguous facts: an Open Fee row makes its key ambiguous (two
    # rows), a funding bill is one that names no trustworthy position.
    for bill in untrusted_bills:
        if bill.get("type") == "Open Fee":
            key = (str(bill.get("instrument") or "").upper(), _epoch_ms(bill.get("createdDate")))
            open_fee_bills[key].extend([None, None])
        elif bill.get("type") == "Funding Fee":
            unattributed += 1
            unattributed_bills.append((str(bill.get("instrument") or "").upper() or None, _epoch_ms(bill.get("createdDate"))))
    for bill in bills:
        amount = _number(bill.get("size"))
        if bill.get("type") == "Open Fee":
            open_fee_bills[(str(bill.get("instrument") or "").upper(), _epoch_ms(bill.get("createdDate")))].append(
                -amount if amount is not None else None)
        elif bill.get("type") == "Funding Fee":
            match = _FUNDING_POSITION_RE.search(str(bill.get("note") or ""))
            if match is None or amount is None:
                unattributed += 1
                unattributed_bills.append((str(bill.get("instrument") or "").upper() or None, _epoch_ms(bill.get("createdDate"))))
            else:
                funding[match.group(1)].append((_epoch_ms(bill.get("createdDate")), amount))
    index: dict[str, dict[str, Any]] = {}
    for open_id, lifecycle in positions.items():
        open_fee, open_fee_source = None, None
        if len(opens.get(open_id, [])) == 1 and open_id not in poisoned_positions:
            event = opens[open_id][0]
            key = (str(event.get("instrument") or "").upper(), _epoch_ms(event.get("createdDate")))
            matched = open_fee_bills.get(key, []) if opens_at[key] == 1 else [None, None]
            event_fee = _number(event.get("fee"))
            if len(matched) == 1 and matched[0] is not None and event_fee is not None and abs(matched[0] - event_fee) <= _FEE_TOLERANCE:
                open_fee, open_fee_source = matched[0], "open_fee_bill"
            elif not matched and event_fee is not None:
                # Only when there is no Open Fee row at all; a malformed or repeated row is ambiguous.
                open_fee, open_fee_source = event_fee, "trade_event_fee"
        entry, exit_ = _epoch_ms(lifecycle.get("entry_time")), _epoch_ms(lifecycle.get("exit_time"))
        paid, funding_complete, missing_settlements = None, False, None
        position_instrument = str(lifecycle.get("instrument") or "").upper() or None
        # A funding bill with no usable position taints only positions it could belong to:
        # an unknown instrument or time could be any position, otherwise the same instrument inside the hold.
        tainted = entry is None or exit_ is None or any(
            (bill_instrument is None or position_instrument is None or bill_instrument == position_instrument)
            and (at is None or entry - 1000 <= at <= exit_ + 1000)
            for bill_instrument, at in unattributed_bills
        )
        if not tainted:
            bills_for_position = funding.get(open_id, [])
            # entry/exit are second-resolution; settlement bills land on the boundary.
            if all(at is not None and entry - 1000 <= at <= exit_ + 1000 for at, _ in bills_for_position):
                paid = round(sum(amount for _, amount in bills_for_position), 12)
                settled = {at // _FUNDING_INTERVAL_MS for at, _ in bills_for_position}
                crossed = range(entry // _FUNDING_INTERVAL_MS + 1, exit_ // _FUNDING_INTERVAL_MS + 1)
                missing_settlements = sum(1 for slot in crossed if slot not in settled)
                funding_complete = missing_settlements == 0
        index[open_id] = {
            "net_profit_sum": _number(lifecycle.get("net_profit_sum")),
            "open_fee": open_fee,
            "open_fee_source": open_fee_source,
            "funding": paid,
            "funding_complete": funding_complete,
            "funding_missing_settlements": missing_settlements,
        }
    attributed = [abs(amount) for rows_for_position in funding.values() for _, amount in rows_for_position]
    pin = {
        "normalized_ledger_sha256": _file_sha(ledger) if ledger.is_file() else None,
        "trade_events_sha256": _digest({path.name: _file_sha(path) for path in trade_files}) if trade_files else None,
        "bill_events_sha256": _digest({path.name: _file_sha(path) for path in bill_files}) if bill_files else None,
        "unattributed_funding_bills": unattributed,
        # Rows excluded from every figure above because their identity is not a single fact.
        "untrusted_trade_event_rows": len(untrusted_events),
        "untrusted_bill_rows": len(untrusted_bills),
        # Scale for a missing settlement: the typical size of one funding bill.
        "mean_abs_funding_bill": round(sum(attributed) / len(attributed), 6) if attributed else None,
    }
    return index, pin


def _provider_net_pnl(provider_pnl: Any, verified: bool, fees: Mapping[str, Any] | None) -> dict[str, Any]:
    """Close netProfit minus the open fee plus signed funding, only when all are proven.

    The canonical close P&L must still equal the provider ledger's, so a later
    sync of the same position can never mix stale P&L with fresh fees.
    """
    fees = fees or {}
    open_fee, funding, ledger_pnl = fees.get("open_fee"), fees.get("funding"), fees.get("net_profit_sum")
    pnl = _number(provider_pnl)
    complete = (
        verified and pnl is not None and open_fee is not None and funding is not None
        and ledger_pnl is not None and abs(pnl - ledger_pnl) <= _PNL_TOLERANCE
    )
    return {
        "provider_open_fee": open_fee,
        "provider_open_fee_source": fees.get("open_fee_source"),
        "provider_funding_fee": funding,
        "provider_funding_complete": fees.get("funding_complete") is True,
        "provider_funding_missing_settlements": fees.get("funding_missing_settlements"),
        "provider_net_pnl": _round(pnl - open_fee + funding) if complete else None,
        "provider_net_pnl_complete": complete,
        # Complete means every figure is available. It does not mean funding is fully reconciled:
        # that needs every 8-hour settlement to have a bill, which only this flag asserts.
        "provider_net_pnl_funding_reconciled": bool(complete and fees.get("funding_complete") is True),
    }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _date_key(value: Any) -> str:
    return str(value or "")


def _time_seconds(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{2}):(\d{2})(?::(\d{2}))?", value.strip())
    if not match:
        return None
    hour, minute, second = (int(part or 0) for part in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour * 3600 + minute * 60 + second


def chronology_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Sort known valid times first, then missing/invalid times deterministically."""

    raw_time = row.get("time_entry")
    seconds = _time_seconds(raw_time)
    known_flag = 0 if seconds is not None else 1
    stable_source = str(row.get("source_path") or row.get("source_record_id") or row.get("legacy_source_path") or "")
    observation_id = str(row.get("observation_id") or row.get("trade_id") or row.get("canonical_episode_id") or "")
    raw_fallback = str(raw_time or "")
    return (_date_key(row.get("event_date")), known_flag, seconds if seconds is not None else 0, stable_source, observation_id, raw_fallback)


def _load_json(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationalAnalyticsError(f"unable to read {relative}") from exc
    if not isinstance(payload, dict):
        raise OperationalAnalyticsError(f"object required: {relative}")
    return payload


def _load_b1(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    manifest = _load_json(root, "exports/method-validation/b1-v3/manifest.json")
    dataset_path = root / "exports/method-validation/b1-v3/canonical-trade-dataset.jsonl"
    if _file_sha(dataset_path) != manifest.get("artifacts", {}).get("dataset", {}).get("sha256"):
        raise OperationalAnalyticsError("B1 v3 dataset SHA does not match its manifest")
    rows = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise OperationalAnalyticsError("B1 v3 dataset rows must be objects")
    return rows, manifest, _load_json(root, "exports/method-validation/b2/manifest.json")


def _active_rule_pins(root: Path) -> tuple[list[str], dict[str, str], dict[str, str], dict[str, str]]:
    b3 = _load_json(root, "exports/method-validation/b3/manifest.json")
    active = list(b3.get("active_strategy_ids", []))
    version_by_strategy: dict[str, str] = {}
    definition_by_strategy: dict[str, str] = {}
    file_by_strategy: dict[str, str] = {}
    for item in b3.get("strategy_versions", []):
        if not isinstance(item, Mapping):
            continue
        strategy_id = str(item.get("strategy_id"))
        if strategy_id in active:
            version_by_strategy[strategy_id] = str(item.get("rule_version"))
            definition_by_strategy[strategy_id] = str(item.get("rule_definition_sha256"))
            file_by_strategy[strategy_id] = str(item.get("version_file_sha256"))
    if set(version_by_strategy) != set(active):
        raise OperationalAnalyticsError("B3 active strategy pins are incomplete")
    return active, version_by_strategy, definition_by_strategy, file_by_strategy


def _chronology_quality(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    known_time = sum(bool(row.get("time_entry")) for row in rows)
    valid_time = sum(_time_seconds(row.get("time_entry")) is not None for row in rows)
    return {
        "ordering_contract": "event_date_then_known_time_then_stable_tiebreak",
        "event_date_complete": all(bool(row.get("event_date")) for row in rows),
        "time_entry_known": known_time,
        "time_entry_valid": valid_time,
        "time_entry_missing": sum(not row.get("time_entry") for row in rows),
        "time_entry_invalid": sum(bool(row.get("time_entry")) and _time_seconds(row.get("time_entry")) is None for row in rows),
        "true_execution_chronology_observable": valid_time == len(rows),
        "limitation": "stable deterministic ordering is not proof of true execution chronology when time_entry is missing",
    }


def _outcome(row: Mapping[str, Any]) -> str:
    raw = row.get("outcome")
    if raw in {"win", "loss", "breakeven"}:
        return str(raw)
    r = _number(row.get("realized_r"))
    if r is None:
        return "unknown"
    if r > 0:
        return "win"
    if r < 0:
        return "loss"
    return "breakeven"


def _record_mode(record: Mapping[str, Any]) -> str:
    """Return the explicit execution mode used by downstream read models."""

    execution_mode = record.get("execution_mode")
    if execution_mode == "PAPER" or (
        record.get("type") == "backtest"
        and record.get("execution_environment") == "paper"
        and _record_value(record, "operational_capture.source_method") == "paper_trade"
    ):
        return "paper"
    return "live" if record.get("type") == "live" else "backtest"


def _provider_population(row: Mapping[str, Any]) -> bool:
    """Verified provider P&L eligibility is independent of R-risk authority."""
    return (row["performance_eligible"] if "performance_eligible" in row else row.get("counted")) is True


def _in_population(row: Mapping[str, Any]) -> bool:
    """R denominator requires verified original-risk authority, not just a close."""
    return (row["r_performance_eligible"] if "r_performance_eligible" in row
            else _provider_population(row)) is True


def _corrected_plan(lifecycle: Any) -> Mapping[str, Any] | None:
    """The latest Owner-confirmed chart plan for a trade, if one was recorded."""

    correction = lifecycle.get("plan_correction") if isinstance(lifecycle, Mapping) else None
    plan = correction.get("plan") if isinstance(correction, Mapping) else None
    return plan if isinstance(plan, Mapping) else None


def _r_authority(root: Path, lifecycle: Mapping[str, Any]) -> tuple[bool, str]:
    """Use immutable structured registration facts; never parse Agent free text.

    Legacy records with no registration packet retain their existing status.
    A deterministic post-trade calculation using an UNKNOWN original SL cannot
    establish an ex-ante R, regardless of verified provider P&L.
    """
    corrected = _corrected_plan(lifecycle)
    if corrected is not None and _number(corrected.get("stop_loss")) is not None:
        # An Owner-confirmed chart plan (1R = actual fill to the chart stop) replaces
        # the registration-time risk basis, so its R is an ex-ante R again.
        return True, "chart_plan_corrected"
    packet_sha = lifecycle.get("registration_packet_sha256")
    if not isinstance(packet_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", packet_sha):
        return True, "legacy_r_authority_unclassified"
    packet_file = root / "canonical" / "registration" / "packets" / (packet_sha + ".json")
    if not packet_file.is_file():
        raise OperationalAnalyticsError("registered lifecycle missing immutable R authority packet")
    packet = _load_json(root, str(packet_file.relative_to(root)))
    if packet.get("packet_sha256") != packet_sha:
        raise OperationalAnalyticsError("registered lifecycle R authority packet SHA binding differs")
    capture = packet.get("canonical_capture")
    if not isinstance(capture, Mapping):
        # Historic packet schemas do not record the modern R source field.
        # Preserve their established counting contract; only modern claims
        # explicitly identifiable as post-trade proxies get downgraded.
        return True, "legacy_packet_r_authority_unclassified"
    if _number(capture.get("stop_loss")) is None:
        if capture.get("realized_r_authority") == "deterministic_execution_calculation":
            return False, "unverified_initial_risk_proxy"
        if capture.get("realized_r_authority") == "owner_attestation":
            # Owner decision 2026-10-12 (#146): an attested R whose original
            # stop was never recorded keeps verified provider P&L but cannot
            # enter R statistics, the same standard as a calculated proxy.
            return False, "unverified_initial_risk_owner_attested"
    return True, "registered_r_authority_unmodified"


def _metrics(rows: Iterable[Mapping[str, Any]], chronology: dict[str, Any]) -> dict[str, Any]:
    population = list(rows)
    eligible = [row for row in population if _in_population(row) and _number(row.get("realized_r")) is not None]
    ordered = sorted(eligible, key=chronology_key)
    values = [float(row["realized_r"]) for row in ordered]
    outcomes = [_outcome(row) for row in ordered]
    n = len(values)
    wins = [v for v, outcome in zip(values, outcomes) if outcome == "win"]
    losses = [v for v, outcome in zip(values, outcomes) if outcome == "loss"]
    bes = [v for v, outcome in zip(values, outcomes) if outcome == "breakeven"]
    positive = sum(v for v in values if v > 0)
    negative = sum(v for v in values if v < 0)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    max_win_streak = max_loss_streak = current_win = current_loss = 0
    for outcome in outcomes:
        if outcome == "win":
            current_win += 1
            current_loss = 0
        elif outcome == "loss":
            current_loss += 1
            current_win = 0
        else:
            current_win = current_loss = 0
        max_win_streak = max(max_win_streak, current_win)
        max_loss_streak = max(max_loss_streak, current_loss)
    review_known = [row for row in eligible if row.get("review_status") in {"reviewed", "pending", "not_started"}]
    rule_known = [row for row in eligible if isinstance(row.get("rule_violation"), bool)]
    provider_rows = [row for row in population
                     if _provider_population(row) and _number(row.get("provider_pnl")) is not None
                     and row.get("provider_pnl_verified") is True]
    provider = [float(row["provider_pnl"]) for row in provider_rows]
    net_rows = [row for row in provider_rows if row.get("provider_net_pnl_complete") is True]
    reconciled_rows = [row for row in net_rows if row.get("provider_net_pnl_funding_reconciled") is True]
    mean = sum(values) / n if n else None
    median = None
    if values:
        ordered_values = sorted(values)
        mid = n // 2
        median = ordered_values[mid] if n % 2 else (ordered_values[mid - 1] + ordered_values[mid]) / 2
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    sorted_values = sorted(values)
    def quantile(q: float) -> float | None:
        if not sorted_values:
            return None
        position = (len(sorted_values) - 1) * q
        lower = int(position)
        upper = min(lower + 1, len(sorted_values) - 1)
        fraction = position - lower
        return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction
    positive_values = sorted((value for value in values if value > 0), reverse=True)
    positive_total = sum(positive_values)
    symbol_counts: dict[str, int] = defaultdict(int)
    for row in eligible:
        # One underlying is one bucket (ETH and ETHUSDT are the same market).
        symbol_counts[base_asset(row.get("instrument"))] += 1
    symbol_total = sum(symbol_counts.values())
    top_symbols = sorted(symbol_counts.values(), reverse=True)
    return {
        "count": n,
        "win_count": len(wins),
        "loss_count": len(losses),
        "breakeven_count": len(bes),
        "win_rate": _round(len(wins) / n if n else None),
        "total_r": _round(sum(values) if values else None),
        "mean_r": _round(mean),
        "median_r": _round(median),
        "q25_r": _round(quantile(0.25)),
        "q75_r": _round(quantile(0.75)),
        "q90_r": _round(quantile(0.90)),
        "max_r": _round(max(values) if values else None),
        "expectancy_r": _round(mean),
        "profit_factor": _round(positive / abs(negative) if negative else None),
        "average_win_r": _round(avg_win),
        "average_loss_r": _round(avg_loss),
        "payoff_ratio": _round(avg_win / abs(avg_loss) if avg_win is not None and avg_loss not in (None, 0) else None),
        "max_observed_drawdown_r": _round(max_dd if values else None),
        "maximum_winning_streak": max_win_streak,
        "maximum_losing_streak": max_loss_streak,
        "live_count": sum(row.get("sample_origin") == "live" for row in eligible),
        "paper_count": sum(row.get("sample_origin") == "paper" for row in eligible),
        "backtest_count": sum(row.get("sample_origin") == "backtest" for row in eligible),
        "review_completion_rate": _round(sum(row.get("review_status") == "reviewed" for row in review_known) / len(review_known) if review_known else None),
        "rule_violation_rate": _round(sum(row.get("rule_violation") is True for row in rule_known) / len(rule_known) if rule_known else None),
        # Close-row netProfit only: excludes the open fee and funding.
        "verified_provider_pnl": _round(sum(provider) if provider else None),
        "provider_pnl_observation_count": len(provider),
        # Account P&L of the same positions: after open fee and signed funding.
        "verified_provider_net_pnl": _round(sum(float(row["provider_net_pnl"]) for row in net_rows) if net_rows else None),
        "provider_net_pnl_observation_count": len(net_rows),
        "provider_net_pnl_incomplete_count": len(provider_rows) - len(net_rows),
        # Strictest figure: only trades whose funding is reconciled against every settlement.
        "verified_provider_net_pnl_funding_reconciled": _round(sum(float(row["provider_net_pnl"]) for row in reconciled_rows) if reconciled_rows else None),
        "provider_net_pnl_funding_reconciled_count": len(reconciled_rows),
        # In the net figure, but an 8-hour settlement has no funding bill.
        "provider_net_pnl_funding_gap_count": sum(row.get("provider_funding_complete") is not True for row in net_rows),
        "provider_net_pnl_funding_missing_settlements": sum(int(row.get("provider_funding_missing_settlements") or 0) for row in net_rows),
        "provider_open_fee_total": _round(sum(float(row["provider_open_fee"]) for row in net_rows) if net_rows else None),
        "provider_funding_fee_total": _round(sum(float(row["provider_funding_fee"]) for row in net_rows) if net_rows else None),
        "positive_r_concentration": {
            "positive_trade_count": len(positive_values),
            "top_1_positive_r_contribution": _round(positive_values[0] / positive_total if positive_values else None),
            "top_3_positive_r_contribution": _round(sum(positive_values[:3]) / positive_total if positive_values else None),
        },
        "instrument_concentration": {
            "top_1_share": _round(top_symbols[0] / symbol_total if top_symbols else None),
            "top_3_share": _round(sum(top_symbols[:3]) / symbol_total if top_symbols else None),
        },
        "chronology_quality": chronology,
    }


def _group_metrics(rows: list[dict[str, Any]], field: str, chronology: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = row.get(field)
        if value is None or value == "":
            value = "unknown"
        groups[str(value)].append(row)
    return {key: _metrics(groups[key], chronology) for key in sorted(groups)}


def _cumulative_r_by_mode(observations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Governed cumulative-R running sum per mode, in chronology order.

    This is the authoritative source for Trade View's cumulative-R
    sparkline (issue #44 PR #45 review blocker 1): performance aggregates,
    including a simple running sum for visualization, must be derived here
    in the governed analytics layer, never independently in the
    presentation layer. Trade View only passes this field through
    byte-for-byte; it performs no arithmetic on realized_r itself.

    Only population-eligible observations with a numeric ``realized_r`` are
    included -- the exact eligibility already used by ``_metrics`` above
    (``performance_eligible`` for canonical rows, otherwise ``counted``).
    ``observations`` is already chronology-sorted by the caller (see
    ``observations = sorted(seed + countable_new, key=chronology_key)``),
    so insertion order is preserved rather than re-sorted here.
    """

    series: dict[str, list[dict[str, Any]]] = {"live": [], "paper": [], "backtest": [], "all": []}
    running = {"live": 0.0, "paper": 0.0, "backtest": 0.0, "all": 0.0}
    for row in observations:
        if not _in_population(row):
            continue
        value = row.get("realized_r")
        if not isinstance(value, (int, float)):
            continue
        mode = row.get("sample_origin") if row.get("sample_origin") in ("live", "paper", "backtest") else None
        point_ref = row.get("trade_id") or row.get("observation_id")
        date = row.get("event_date")
        running["all"] += float(value)
        series["all"].append({"date": date, "reference": point_ref, "r": round(float(value), 6), "cumulative_r": round(running["all"], 6)})
        if mode:
            running[mode] += float(value)
            series[mode].append({"date": date, "reference": point_ref, "r": round(float(value), 6), "cumulative_r": round(running[mode], 6)})
    return series


def _summary_by_mode(observations: list[dict[str, Any]], chronology: dict[str, Any]) -> dict[str, Any]:
    """Explicit LIVE / PAPER / BACKTEST / ALL summary, purely additive over `_metrics`.

    "all" MUST carry combined_descriptive=True: it is never a coherent single
    population (e.g. 500 backtest + 20 live must not read as "520
    live-equivalent samples") -- it is a descriptive combination only.
    """

    live_observations = [row for row in observations if row.get("sample_origin") == "live"]
    paper_observations = [row for row in observations if row.get("sample_origin") == "paper"]
    backtest_observations = [row for row in observations if row.get("sample_origin") == "backtest"]
    metrics_all = _metrics(observations, chronology)
    return {
        "live": _metrics(live_observations, chronology),
        "paper": _metrics(paper_observations, chronology),
        "backtest": _metrics(backtest_observations, chronology),
        "all": {**metrics_all, "combined_descriptive": True},
    }


def _operational_observability_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    operational = [row for row in rows if row.get("source_kind") == "post_cutover_operational"]
    observed = [row for row in operational if (row.get("method_observability") or {}).get("status") == "machine_observable"]
    reviewed = [row for row in operational if row.get("review_complete") is True]
    method_eligible = [row for row in operational if row.get("operational_method_eligible") is True]
    if not operational:
        status = "not_observable"
    elif len(observed) == len(operational):
        status = "machine_observable"
    elif observed:
        status = "mixed"
    else:
        status = "not_observable"
    return {
        "status": status,
        "operational_observed_n": len(observed),
        "operational_reviewed_n": len(reviewed),
        "operational_method_eligible_n": len(method_eligible),
        "operational_n": len(operational),
    }


def _b1_observation(row: Mapping[str, Any], rule_version: str) -> dict[str, Any]:
    return {
        "observation_id": row.get("canonical_episode_id"),
        "source_kind": "b1_v3_historical_seed",
        "counted": True,
        "status": "historical_seed",
        "trade_id": None,
        "trade_no": None,
        "event_date": row.get("event_date"),
        "time_entry": row.get("time_entry"),
        "instrument": row.get("instrument"),
        "base_asset": base_asset(row.get("instrument")),
        "symbol": row.get("instrument"),
        "direction": row.get("direction"),
        "timeframe": row.get("timeframe_trigger") or row.get("timeframe_context") or "unknown",
        "strategy_id": row.get("strategy_id"),
        "rule_version": rule_version,
        "strategy_resolution": None,
        "strategy_resolution_status": None,
        "strategy_stats_eligible": None,
        "sample_origin": row.get("sample_origin"),
        "outcome": row.get("outcome"),
        "realized_r": row.get("canonical_realized_r"),
        "provider_pnl": None,
        "provider_pnl_verified": False,
        **_provider_net_pnl(None, False, None),
        "review_status": "not_observable",
        "rule_violation": None,
        "setup_tags": None,
        "confirmation_tags": None,
        "pre_trade_evidence_present": False,
        "entry_evidence_present": False,
        "exit_evidence_present": False,
        "review_complete": False,
        "execution_deviation": None,
        "method_observability": {"status": "not_observable", "reason": "historical B1 v3 seed does not carry post-cutover operational evidence fields", "required_rules": []},
        "operational_method_eligible": False,
        "canonical_episode_id": row.get("canonical_episode_id"),
        "source_path": row.get("source_path"),
        "evidence_ref": f"exports/method-validation/b1-v3/canonical-trade-dataset.jsonl#{row.get('canonical_episode_id')}",
        # The B1 v3 historical seed predates the source_method/recording_timing
        # vocabularies (see vocabularies.py); these audit fields simply do not
        # exist for that immutable seed and stay explicit None rather than
        # being backfilled or guessed.
        "source_method": None,
        "outcome_known_at_recording": None,
        "recording_timing": None,
        "evidence_artifacts": [],
        # Trade View V2: honest-unknown counterparts of the additive fields
        # above -- the B1 v3 historical seed predates plan/review/provider
        # capture, so these are genuinely not_observable rather than omitted.
        "planned_entry": None,
        "planned_stop_loss": None,
        "planned_take_profit": None,
        "timeframe_context": row.get("timeframe_context"),
        "timeframe_trigger": row.get("timeframe_trigger"),
        "market_context": None,
        "thesis": None,
        "entry_verification": None,
        "provider": None,
        "review_what_went_well": None,
        "review_what_went_wrong": None,
        "review_thesis_validity": None,
        "review_entry_quality": None,
        "review_exit_quality": None,
        "review_owner_notes": None,
    }


def _rule_value(record: Mapping[str, Any], field: str) -> Any:
    return _record_value(record, field)


def _evidence_methods(evidence: Mapping[str, Any]) -> set[str]:
    methods = {str(value) for value in (evidence.get("method"), evidence.get("type")) if value}
    if evidence.get("type") == "replay_chart":
        methods.add("chart_snapshot")
    return methods


def _operational_rule_observability(
    root: Path,
    record: Mapping[str, Any],
    evidence: list[Mapping[str, Any]],
    strategy_resolution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    strategy = _record_value(record, "strategy.strategy_id")
    rule_version = _record_value(record, "strategy.rule_version")
    if isinstance(strategy_resolution, Mapping):
        strategy = strategy_resolution.get("strategy_id") or strategy
        rule_version = strategy_resolution.get("rule_version") or rule_version
    try:
        rules = load_effective_rules(root, str(rule_version))
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "not_observable", "reason": f"frozen rule definition unavailable: {type(exc).__name__}", "required_rules": []}

    trace = strategy_resolution.get("rule_observations") if isinstance(strategy_resolution, Mapping) else None
    trace_rules = {
        str(item.get("rule_id")): item
        for item in trace.get("rules", [])
        if isinstance(item, Mapping) and isinstance(item.get("rule_id"), str)
    } if isinstance(trace, Mapping) and isinstance(trace.get("rules"), list) else {}
    trace_version = trace.get("schema_version") if isinstance(trace, Mapping) else None
    trace_pinned = bool(
        isinstance(trace, Mapping)
        and trace_version in {"resolved-rule-observations.v1", "resolved-rule-observations.v2"}
        and trace.get("strategy_id") == strategy
        and trace.get("rule_version") == rule_version
    )

    # Retrospective rule facts are admissible only through the first-class
    # #98 Owner authority and its immutable registry pin. A free-standing
    # rule observation cannot upgrade a post-trade replay into pre-trade
    # evidence.
    projection_rules: dict[str, Mapping[str, Any]] = {}
    projection = strategy_resolution.get("retrospective_rule_facts") if isinstance(strategy_resolution, Mapping) else None
    amendment = record.get("strategy_attribution_amendment")
    amendment_authority = amendment.get("authority") if isinstance(amendment, Mapping) else None
    if (
        isinstance(projection, Mapping)
        and projection.get("schema_version") == "retrospective-rule-facts.v1"
        and projection.get("authority") == OWNER_RETROSPECTIVE_AUTHORITY
        and projection.get("strategy_id") == strategy
        and projection.get("rule_version") == rule_version
        and isinstance(strategy_resolution, Mapping)
        and strategy_resolution.get("status") == "replay_reconstructed"
        and strategy_resolution.get("resolution_authority") == OWNER_RETROSPECTIVE_AUTHORITY
        and isinstance(strategy_resolution.get("owner_attestation"), Mapping)
        and strategy_resolution["owner_attestation"].get("timing") == "retrospective"
        and strategy_resolution["owner_attestation"].get("setup_status") == "HAS_SETUP"
        and not strategy_resolution.get("material_conflicts")
        and isinstance(amendment_authority, Mapping)
        and projection.get("retrospective_attribution_registry_version") == RETROSPECTIVE_ATTRIBUTION_REGISTRY_VERSION
        and all(
            projection.get(key) == amendment_authority.get(key)
            for key in (
                "strategy_id",
                "rule_version",
                "retrospective_attribution_registry_version",
                "retrospective_attribution_registry_digest",
            )
        )
        and isinstance(projection.get("retrospective_attribution_registry_digest"), str)
        and re.fullmatch(r"[0-9a-f]{64}", projection["retrospective_attribution_registry_digest"])
        and isinstance(projection.get("rules"), list)
    ):
        retrospective_observations = strategy_resolution.get("retrospective_observations")
        valid_rules = {str(rule.get("rule_id")): rule for rule in rules if isinstance(rule, Mapping)}
        seen: set[str] = set()
        if isinstance(retrospective_observations, list):
            for fact in projection["rules"]:
                if (
                    not isinstance(fact, Mapping)
                    or not isinstance(fact.get("rule_id"), str)
                    or fact["rule_id"] in seen
                    or fact.get("state") != "PASS"
                    or fact.get("supports_fact_timing") != "pre_trade"
                    or fact.get("basis") != ["owner_attestation", "retrospective_replay"]
                    or not isinstance(fact.get("evidence_refs"), list)
                    or not fact["evidence_refs"]
                    or not isinstance(fact.get("source_claim_ids"), list)
                    or not fact["source_claim_ids"]
                ):
                    continue
                rule = valid_rules.get(fact["rule_id"])
                allowed = set(rule.get("allowed_evidence_timing") or {"pre_trade", "execution"}) if rule else set()
                if rule is None or "pre_trade" not in allowed:
                    continue
                claim_ids = set(fact["source_claim_ids"])
                supporting = [
                    item for item in retrospective_observations
                    if isinstance(item, Mapping) and item.get("claim_id") in claim_ids
                ]
                if (
                    {item.get("claim_id") for item in supporting} != claim_ids
                    or any(item.get("finding") != "corroborates" or item.get("timing") != "post_trade" for item in supporting)
                ):
                    continue
                source_refs = {
                    ref for item in supporting for ref in item.get("evidence_refs", [])
                    if isinstance(ref, str)
                }
                if source_refs != set(fact["evidence_refs"]):
                    continue
                exact_replays = True
                for ref in fact["evidence_refs"]:
                    matches = [item for item in evidence if item.get("source_ref") == ref]
                    identities = {evidence_identity(item) for item in matches}
                    if not matches or len(identities) != 1:
                        exact_replays = False
                        break
                    item = matches[0]
                    provenance = item.get("provenance")
                    if not (
                        item.get("timing") == "post_trade"
                        and item.get("category") == "review"
                        and item.get("type") == "replay_chart"
                        and item.get("role") == "historical_market_reconstruction"
                        and isinstance(provenance, Mapping)
                        and provenance.get("capture_mode") == "retrospective_replay"
                    ):
                        exact_replays = False
                        break
                if exact_replays:
                    seen.add(fact["rule_id"])
                    projection_rules[fact["rule_id"]] = fact

    results: list[dict[str, Any]] = []
    for rule in rules:
        rule_id = str(rule.get("rule_id"))
        field = str((rule.get("operands") or {}).get("field") or "")
        operator = rule.get("operator")
        value = _rule_value(record, field)
        if operator in {"tag_present", "tag_absent"}:
            values = value if isinstance(value, list) else None
            expected = (rule.get("operands") or {}).get("value")
            if values is None:
                state = "UNKNOWN"
            elif operator == "tag_present":
                state = "SATISFIED" if expected in values else "NOT_SATISFIED"
            else:
                state = "SATISFIED" if expected not in values else "NOT_SATISFIED"
        elif operator == "in":
            allowed_values = (rule.get("operands") or {}).get("values")
            unknown = value is None or value == "" or (isinstance(value, str) and value.strip().lower() in PLAN_FIELD_UNKNOWN_VALUES)
            state = "UNKNOWN" if unknown or not isinstance(allowed_values, list) else ("SATISFIED" if value in allowed_values else "NOT_SATISFIED")
        elif operator == "exists":
            unknown = value is None or value == "" or (isinstance(value, str) and value.strip().lower() in PLAN_FIELD_UNKNOWN_VALUES)
            state = "UNKNOWN" if unknown else "SATISFIED"
        else:
            state = "UNKNOWN"

        projected = projection_rules.get(rule_id)
        resolver_item = trace_rules.get(rule_id) if trace_pinned else None
        if projected is not None:
            resolver_state = projected["state"]
            evidence_refs = list(projected["evidence_refs"])
            supports_fact_timing = projected["supports_fact_timing"]
            basis = projected["basis"]
            state = "SATISFIED"
            projected_replay = True
        else:
            resolver_state = resolver_item.get("state") if isinstance(resolver_item, Mapping) else None
            evidence_refs = resolver_item.get("evidence_refs") if isinstance(resolver_item, Mapping) else None
            supports_fact_timing = resolver_item.get("supports_fact_timing") if isinstance(resolver_item, Mapping) else None
            basis = resolver_item.get("basis") if isinstance(resolver_item, Mapping) else None
            projected_replay = False
            if not isinstance(evidence_refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in evidence_refs):
                evidence_refs = []
            if resolver_state not in {"PASS", "FAIL"} or not evidence_refs:
                state = "UNKNOWN"
                evidence_refs = []
            else:
                observed_state = "SATISFIED" if resolver_state == "PASS" else "NOT_SATISFIED"
                state = observed_state if state == observed_state else "UNKNOWN"

        required_methods = set(rule.get("evidence_requirements") or [])
        allowed_timings = set(rule.get("allowed_evidence_timing") or {"pre_trade", "execution"})
        timing_categories = {"pre_trade": "pre_trade_context", "execution": "entry", "post_trade": "review"}
        matched_by_ref: dict[str, list[Mapping[str, Any]]] = {}
        for ref in evidence_refs:
            if projected_replay:
                matches = [
                    item for item in evidence
                    if item.get("source_ref") == ref
                    and item.get("timing") == "post_trade"
                    and item.get("category") == "review"
                    and item.get("type") == "replay_chart"
                    and item.get("role") == "historical_market_reconstruction"
                    and isinstance(item.get("provenance"), Mapping)
                    and item["provenance"].get("capture_mode") == "retrospective_replay"
                ]
            else:
                valid_basis = isinstance(basis, list) and bool(basis) and all(
                    value in {"contemporaneous_capture", "provider_execution", "governed_adjudication"}
                    for value in basis
                )
                valid_fact_timing = supports_fact_timing in allowed_timings
                matches = [
                    item for item in evidence
                    if item.get("source_ref") == ref
                    and valid_basis
                    and valid_fact_timing
                    and item.get("timing") == supports_fact_timing
                    and item.get("category") == timing_categories.get(str(supports_fact_timing))
                    and not (
                        isinstance(item.get("provenance"), Mapping)
                        and item["provenance"].get("capture_mode") == "retrospective_replay"
                    )
                    and item.get("role") != "historical_market_reconstruction"
                ]
                if trace_version == "resolved-rule-observations.v1" and trace_pinned:
                    matches = [
                        item for item in evidence
                        if item.get("source_ref") == ref
                        and item.get("timing") in allowed_timings
                        and item.get("category") == timing_categories.get(str(item.get("timing")))
                    ]
            matches = [item for item in matches if not required_methods or bool(_evidence_methods(item) & required_methods)]
            identities = {evidence_identity(item) for item in matches}
            if not matches or len(identities) != 1:
                matches = []
            matched_by_ref[ref] = matches
        evidence_matches = [items[0] for items in matched_by_ref.values() if items]
        if state in {"SATISFIED", "NOT_SATISFIED"} and len(evidence_matches) != len(set(evidence_refs)):
            state = "UNKNOWN"
        results.append({
            "rule_id": rule.get("rule_id"),
            "required": bool(rule.get("required")),
            "operand_field": field,
            "operator": operator,
            "state": state,
            "evidence_refs": sorted({str(item.get("source_ref")) for item in evidence_matches if item.get("source_ref")}),
            "resolver_state": resolver_state if trace_pinned or projected is not None else None,
            **({"supports_fact_timing": supports_fact_timing} if supports_fact_timing else {}),
            **({"basis": list(basis)} if isinstance(basis, list) else {}),
        })
    required = [item for item in results if item["required"]]
    if required and all(item["state"] in {"SATISFIED", "NOT_SATISFIED"} for item in required):
        status = "machine_observable"
        reason = "all required frozen rule outcomes are bound to exact evidence and supported fact timing"
    elif any(item["state"] in {"SATISFIED", "NOT_SATISFIED"} for item in required):
        status = "mixed"
        reason = "some required frozen rules have exact evidence while others are missing or conflicting"
    else:
        status = "not_observable"
        reason = "exact per-rule resolver evidence is missing or does not match the frozen rule version"
    return {"strategy_id": strategy, "rule_version": rule_version, "status": status, "reason": reason, "required_rules": results}


def _record_value(record: Mapping[str, Any], path: str) -> Any:
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Locator filenames are always exactly ``<sha256><ext>`` (EvidenceStore.
# ingest's own convention) -- this is the *only* shape ever accepted as a
# real artifact name. A traversal-shaped or otherwise malformed name never
# even reaches the filesystem check below.
_LOCATOR_NAME_RE = re.compile(r"^(?P<sha256>[0-9a-f]{64})(?P<ext>\.[a-z0-9]{1,10})?$")


def _verified_artifact_locator(root: Path, locator: Any, expected_sha256: Any) -> bool:
    """True only when ``locator`` is a durable EvidenceStore artifact whose
    on-disk bytes actually exist AND actually hash to ``expected_sha256``.

    Never trusts the recorded sha256 metadata field on its own -- a bare
    ``source_ref`` string with no real backing (or an artifact whose bytes
    were tampered with/corrupted after ingest) both fail closed here.

    Correction pass #3 (C4): containment is never decided by string
    concatenation/``startswith`` -- the locator's filename is first
    validated against EvidenceStore's own ``<sha256><ext>`` naming
    convention (rejecting anything containing ``..``, a path separator, or
    any other shape) *before* it ever touches a Path, the parsed sha256
    must be a well-formed 64-hex string that matches the recorded
    ``expected_sha256`` metadata, and the resolved candidate path is then
    re-checked to be genuinely inside the evidence artifacts directory via
    ``Path.is_relative_to`` (never a plain string prefix check, which a
    crafted sibling-directory name could defeat).
    """

    if not isinstance(locator, str) or not locator.startswith("artifact:"):
        return False
    name = locator[len("artifact:"):]
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
        return False
    match = _LOCATOR_NAME_RE.fullmatch(name)
    if not match or match.group("sha256") != expected_sha256:
        return False
    evidence_dir = (root / "canonical" / "evidence" / "artifacts").resolve()
    candidate = (evidence_dir / name).resolve()
    if not candidate.is_relative_to(evidence_dir):
        return False
    if not candidate.is_file():
        return False
    try:
        actual_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return False
    return actual_sha256 == expected_sha256


def _strong_backtest_evidence(root: Path, items: Iterable[Mapping[str, Any]], *, required_timings: set[str]) -> bool:
    """True if any item is durable, hash-verified, and of a strong evidence
    type (BACKTEST_STRONG_EVIDENCE_TYPES) with an appropriate timing.

    A bare ``manual_note`` (or any type outside the strong allow-list) never
    satisfies this alone, regardless of how many are attached.
    """

    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") not in BACKTEST_STRONG_EVIDENCE_TYPES:
            continue
        if item.get("timing") not in required_timings:
            continue
        if _verified_artifact_locator(root, item.get("locator"), item.get("sha256")):
            return True
    return False


def _evidence_ref_allowlist(evidence_refs: Any) -> set[str]:
    """Normalize a lifecycle's recorded ``evidence_refs`` list into a set of
    ``source_ref`` strings.

    Correction pass #3 (C1/C2): this is the exact-binding allow-list -- only
    evidence whose ``source_ref`` was actually cited by the verification
    that established entry/exit truth may ever count as strict evidence.
    Evidence attached to the trade/draft elsewhere, however strong, is not
    in scope unless its ``source_ref`` appears here.
    """

    if not isinstance(evidence_refs, list):
        return set()
    return {ref for ref in evidence_refs if isinstance(ref, str) and ref}


def _resolve_bound_evidence(
    evidence: Iterable[Mapping[str, Any]],
    evidence_refs: Any,
    *,
    category: str,
) -> list[Mapping[str, Any]] | None:
    """Resolve cited evidence without allowing an alias to be upgraded.

    The canonical record and lifecycle projection intentionally carry
    equivalent copies of the same evidence.  Those copies collapse by the
    immutable evidence identity.  A cited ``source_ref`` with more than one
    identity in the required category is historical ambiguity, so strict
    analytics fails closed instead of selecting whichever copy appears last.
    """

    allowed_refs = _evidence_ref_allowlist(evidence_refs)
    by_source_ref: dict[str, dict[tuple[str, ...], Mapping[str, Any]]] = defaultdict(dict)
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        source_ref = item.get("source_ref")
        if item.get("category") != category or not isinstance(source_ref, str) or source_ref not in allowed_refs:
            continue
        by_source_ref[source_ref].setdefault(evidence_identity(item), item)
    for ref in sorted(allowed_refs):
        if len(by_source_ref.get(ref, {})) > 1:
            return None
    resolved: list[Mapping[str, Any]] = []
    for ref in sorted(allowed_refs):
        identities = by_source_ref.get(ref, {})
        if identities:
            resolved.append(next(iter(identities.values())))
    return resolved


def _resolve_exact_bound_evidence(
    evidence: Iterable[Mapping[str, Any]],
    evidence_refs: Any,
    evidence_bindings: Any,
    *,
    category: str,
) -> list[Mapping[str, Any]] | None:
    """Resolve verification-time evidence bindings without source-ref aliases.

    New exit verification events persist the exact evidence projection selected
    at verification time.  Equivalent canonical/lifecycle copies collapse by
    immutable identity.  A missing binding, a malformed binding, a missing
    exact projection, or a conflicting identity in the same source/category
    scope fails closed.  In particular, another category sharing the same
    ``source_ref`` can never satisfy an exit binding.
    """

    if not isinstance(evidence_refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in evidence_refs):
        return None
    if not isinstance(evidence_bindings, list) or not evidence_bindings:
        return None

    def binding_identity(item: Mapping[str, Any]) -> tuple[str, ...]:
        # Verification events intentionally persist the stable binding schema,
        # which predates retrospective evidence metadata. Compare only those
        # fields here; the richer evidence identity remains useful for
        # projection deduplication elsewhere, but must not make old bindings
        # stop matching when provenance metadata is added.
        return tuple(
            json.dumps(
                item.get(field),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            for field in EVIDENCE_BINDING_FIELDS[1:]
        )

    normalized_bindings: list[tuple[str, tuple[str, ...]]] = []
    for binding in evidence_bindings:
        if (
            not isinstance(binding, Mapping)
            or not set(EVIDENCE_BINDING_REQUIRED_FIELDS) <= set(binding)
            or set(binding) - set(EVIDENCE_BINDING_REQUIRED_FIELDS) - set(EVIDENCE_BINDING_OPTIONAL_FIELDS)
        ):
            return None
        if any(
            not isinstance(binding.get(field), str) or not binding[field].strip()
            for field in EVIDENCE_BINDING_REQUIRED_FIELDS
        ):
            return None
        source_ref = binding["source_ref"]
        if binding["category"] != category:
            return None
        normalized_bindings.append((source_ref, binding_identity(binding)))
    bound_refs = {source_ref for source_ref, _ in normalized_bindings}
    if bound_refs != _evidence_ref_allowlist(evidence_refs):
        return None

    by_scope: dict[tuple[str, str], dict[tuple[str, ...], Mapping[str, Any]]] = defaultdict(dict)
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        source_ref = item.get("source_ref")
        item_category = item.get("category")
        if not isinstance(source_ref, str) or item_category != category:
            continue
        by_scope[(source_ref, item_category)].setdefault(binding_identity(item), item)

    resolved: list[Mapping[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for source_ref, identity in normalized_bindings:
        key = (source_ref, identity)
        if key in seen:
            continue
        seen.add(key)
        identities = by_scope.get((source_ref, category), {})
        if set(identities) != {identity}:
            return None
        resolved.append(identities[identity])
    return resolved


def _is_replay_review_item(item: Mapping[str, Any]) -> bool:
    """A TradingView Replay image as registered: category review, post_trade, replay_chart.

    Source (Owner, 2026-11-22): a BACKTEST is judged from the Replay chart, so a
    durable hash-verified Replay image bound to the entry/exit verification is the
    strict evidence for a Replay BACKTEST. evidence_time forbids giving such an image
    the entry/exit category, so the strict predicates below also accept it under
    its real category. It is never execution-time or provider evidence.
    """

    provenance = item.get("provenance") if isinstance(item.get("provenance"), Mapping) else {}
    return (
        item.get("category") == "review"
        and item.get("type") == "replay_chart"
        and item.get("timing") == "post_trade"
        and provenance.get("capture_mode") == "retrospective_replay"
    )


def _replay_review_evidence_is_strict(root: Path, items: list[Mapping[str, Any]] | None) -> bool:
    return bool(items) and all(_is_replay_review_item(i) for i in items) and _strong_backtest_evidence(
        root, items, required_timings={"post_trade"}
    )


def _backtest_entry_evidence_is_strict(root: Path, entry_basis: Any, evidence: list[Mapping[str, Any]], entry_evidence_refs: Any) -> bool:
    """C3/C4 (pass #2); C1 (pass #3): strict-countable predicate for BACKTEST
    entry evidence.

    ``simulator_run`` is already strong/strict by construction (a formal
    run/dataset authority) -- it needs no image or additional check here.
    ``manual_declared_execution`` (a bare owner claim of simulated execution)
    is strict only when backed by a durable, hash-verified, strong-typed
    piece of *entry* evidence whose ``source_ref`` is exactly one of the
    refs cited in ``actual.execution_verification.entry.evidence_refs`` --
    an unrelated strong entry-category item attached elsewhere on the trade
    but never cited by the verification does not count.
    """

    if entry_basis == "simulator_run":
        return True
    if entry_basis != "manual_declared_execution":
        return False
    entry_items = _resolve_bound_evidence(evidence, entry_evidence_refs, category="entry")
    if entry_items is not None and _strong_backtest_evidence(root, entry_items, required_timings={"execution"}):
        return True
    return _replay_review_evidence_is_strict(root, _resolve_bound_evidence(evidence, entry_evidence_refs, category="review"))


def _backtest_exit_evidence_is_strict(
    root: Path,
    exit_basis: Any,
    evidence: list[Mapping[str, Any]],
    exit_evidence_refs: Any,
    exit_evidence_bindings: Any = None,
) -> bool:
    """C5 (pass #2); C2 (pass #3): equivalent strict-countable predicate for
    BACKTEST exit evidence.

    A formal governed/deterministic exit authority (deterministic_execution_
    calculation, governed_manual_adjudication) is already strict by
    construction. A bare ``owner_attestation`` (the realized-R claim alone,
    with no durable backing) is strict only when backed by a durable,
    hash-verified, strong-typed piece of *exit* evidence whose full immutable
    identity is exactly one of the lifecycle's
    ``exit_verification.evidence_bindings``.  The older refs-only form is
    historical compatibility data and fails closed; an unrelated strong
    exit-category item, or a later category link sharing its ``source_ref``,
    does not count.

    This applies to BACKTEST rows only -- LIVE's exit verification/
    provider_pnl_verification path is untouched (see _canonical_observations,
    which only calls this inside the ``is_backtest`` branch).
    """

    if exit_basis in _BACKTEST_GOVERNED_EXIT_AUTHORITIES:
        return True
    if exit_basis != "owner_attestation":
        return False
    exit_items = _resolve_exact_bound_evidence(evidence, exit_evidence_refs, exit_evidence_bindings, category="exit")
    if exit_items is not None and _strong_backtest_evidence(root, exit_items, required_timings={"post_trade"}):
        return True
    return _replay_review_evidence_is_strict(
        root, _resolve_exact_bound_evidence(evidence, exit_evidence_refs, exit_evidence_bindings, category="review")
    )


def _canonical_observations(
    root: Path,
    b1_rows: list[dict[str, Any]],
    active: set[str],
    version_by_strategy: Mapping[str, str],
    projection: Mapping[str, Any],
    strategy_registry: Mapping[str, Any] | None = None,
    provider_fees: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    strategy_registry = strategy_registry or load_registry(root)
    if provider_fees is None:
        provider_fees, _ = provider_fee_index(root)
    registry_by_id: dict[str, Mapping[str, Any]] = {
        str(item.get("strategy_id")): item
        for item in strategy_registry.get("strategies", [])
        if isinstance(item, Mapping) and item.get("strategy_id")
    }
    frozen_versions_by_strategy = {
        strategy_id: {
            version_id
            for version_id in (item.get("rule_versions") or [])
            if isinstance(version_id, str)
            and load_version(root, version_id).get("schema_version") == FREEZE_SCHEMA_VERSION
        }
        for strategy_id, item in registry_by_id.items()
    }
    by_source_path: dict[str, set[str]] = defaultdict(set)
    by_source_record: dict[str, set[str]] = defaultdict(set)
    for row in b1_rows:
        episode = row.get("canonical_episode_id")
        if episode:
            if row.get("source_path"):
                by_source_path[str(row["source_path"])].add(str(episode))
            if row.get("source_record_id"):
                by_source_record[str(row["source_record_id"])].add(str(episode))
            for ref in row.get("episode_member_trade_refs", []) or []:
                by_source_path[str(ref)].add(str(episode))
    # A canonical trade may supersede an exact B1 episode whose historical
    # row was not performance-eligible (for example, it had an unknown
    # outcome). Keep eligible B1 episodes as the single source of truth, and
    # allow a verified canonical close to supply the episode once only when
    # no eligible B1 observation exists.
    eligible_b1_episode_ids = {
        str(row.get("canonical_episode_id"))
        for row in b1_rows
        if row.get("performance_counting_eligibility") is True
        and row.get("strategy_id") in active
        and row.get("canonical_episode_id")
    }
    records: list[dict[str, Any]] = []
    overlaps = 0
    new_records = 0
    excluded_open = 0
    excluded_unlinked = 0
    unresolved_or_open = 0
    for path in sorted((root / "canonical" / "records" / "trades").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        legacy = record.get("legacy_source") if isinstance(record.get("legacy_source"), Mapping) else {}
        source_path = legacy.get("path") or legacy.get("relative_path")
        candidates = set(by_source_path.get(str(source_path), set()))
        candidates.update(by_source_record.get(str(record.get("record_id")), set()))
        lineage = record.get("provenance", {}).get("episode_lineage") if isinstance(record.get("provenance"), Mapping) else None
        if isinstance(lineage, str):
            candidates.add(lineage)
        lifecycle = projection.get("records", {}).get(str(record.get("trade_id")), {})
        if not lifecycle:
            for candidate in projection.get("records", {}).values():
                if isinstance(candidate, Mapping) and candidate.get("trade_id") == record.get("trade_id"):
                    lifecycle = candidate
                    break
        historical_overlap_episode_id = next(iter(candidates)) if len(candidates) == 1 else None
        if candidates:
            overlaps += 1
            if candidates & eligible_b1_episode_ids or historical_overlap_episode_id is None:
                records.append({"trade_id": record.get("trade_id"), "trade_no": record.get("trade_no"), "source_kind": "migrated_canonical_overlap", "counted": False, "status": "linked_to_historical_seed", "canonical_episode_ids": sorted(candidates), "legacy_source_path": source_path, "reason": "exact source/episode lineage already represented by an eligible B1 v3 performance observation; no second performance observation"})
                continue
        operational_id = record.get("provenance", {}).get("operational_capture_id") if isinstance(record.get("provenance"), Mapping) else None
        if not operational_id and not historical_overlap_episode_id:
            excluded_unlinked += 1
            records.append({"trade_id": record.get("trade_id"), "trade_no": record.get("trade_no"), "source_kind": "canonical_unlinked", "counted": False, "status": "excluded_unlinked_canonical", "legacy_source_path": source_path, "reason": "canonical record has no exact B1 overlap and no operational capture marker"})
            continue
        # Lifecycle attribution amendments are append-only and may supersede
        # the immutable registration-time strategy snapshot. Prefer the
        # projected lifecycle identity, including an explicit null identity.
        strategy = lifecycle.get("strategy_id") if isinstance(lifecycle, Mapping) and "strategy_id" in lifecycle else _record_value(record, "strategy.strategy_id")
        rule_version = lifecycle.get("rule_version") if isinstance(lifecycle, Mapping) and "rule_version" in lifecycle else _record_value(record, "strategy.rule_version")
        rule_version = rule_version or version_by_strategy.get(strategy)
        record_type = record.get("type")
        is_simulated = record_type == "backtest"
        sample_origin = _record_mode(record)
        state = lifecycle.get("state") if isinstance(lifecycle, Mapping) else None
        actual = record.get("actual") if isinstance(record.get("actual"), Mapping) else {}
        verification = actual.get("execution_verification") if isinstance(actual.get("execution_verification"), Mapping) else {}
        entry_verification = verification.get("entry") if isinstance(verification.get("entry"), Mapping) else {}
        entry_confirmed = entry_verification.get("state") == "confirmed"
        entry_basis = entry_verification.get("basis")
        identity_link = lifecycle.get("identity_link") if isinstance(lifecycle, Mapping) and isinstance(lifecycle.get("identity_link"), Mapping) else {}
        identity_verified = lifecycle.get("identity_status") == "MATCHED" and identity_link.get("match_disposition") == "MATCHED"
        exit_verification = lifecycle.get("exit_verification") if isinstance(lifecycle, Mapping) and isinstance(lifecycle.get("exit_verification"), Mapping) else {}
        exit_verified = exit_verification.get("state") == "confirmed"
        close_r = _number(lifecycle.get("realized_r") if exit_verified and isinstance(lifecycle, Mapping) else None)
        provider_verification = lifecycle.get("provider_pnl_verification") if isinstance(lifecycle, Mapping) and isinstance(lifecycle.get("provider_pnl_verification"), Mapping) else {}
        provider_verified = provider_verification.get("state") == "confirmed" and _number(lifecycle.get("provider_pnl")) is not None
        r_qualified, r_authority_status = (
            (True, "backtest_governed") if is_simulated or not isinstance(lifecycle, Mapping)
            else _r_authority(root, lifecycle)
        )
        # BACKTEST has no provider identity to verify: registered strategy
        # (any status -- active or experimental) + a real registered/frozen
        # rule_version (never the unfrozen:experimental placeholder) stand in
        # for the LIVE gate's identity/provider requirements.
        strategy_entry = registry_by_id.get(str(strategy)) if strategy is not None else None
        strategy_registered = isinstance(strategy_entry, Mapping)
        strategy_active = strategy_registered and strategy_entry.get("status") == "active"
        rule_version_registered = bool(
            strategy_registered
            and isinstance(rule_version, str)
            and rule_version != UNFROZEN_EXPERIMENTAL_PLACEHOLDER
            and rule_version in (strategy_entry.get("rule_versions") or [])
        )
        rule_version_frozen = bool(
            strategy_registered
            and isinstance(rule_version, str)
            and rule_version in frozen_versions_by_strategy.get(str(strategy), set())
        )
        named_strategy_eligible = strategy_active and rule_version_registered and rule_version_frozen
        identity_conflict = identity_link.get("match_disposition") in {"AMBIGUOUS", "UNMATCHED", "CONFLICT"}
        backtest_entry_governed = entry_confirmed and entry_basis in GOVERNED_BACKTEST_ENTRY_BASES
        # Evidence used by both the countability gate (C6) and the
        # evidence_quality projection (C6/C7) below -- computed here (ahead of
        # its other later use for evidence_artifacts/tags) so a BACKTEST row
        # that never reaches the countable branch (still open, still
        # unreviewed) can still carry an accurate evidence_quality.
        lifecycle_evidence = lifecycle.get("evidence", []) if isinstance(lifecycle, Mapping) else []
        record_evidence = record.get("evidence", [])
        # Canonical trade files are the registration-time snapshot. Later
        # append-only lifecycle corrections are authoritative for the same
        # immutable evidence identity and must replace stale snapshot fields.
        evidence_by_identity: dict[tuple[Any, Any, Any], Mapping[str, Any]] = {}
        for evidence_item in record_evidence if isinstance(record_evidence, list) else []:
            if isinstance(evidence_item, Mapping):
                key = (evidence_item.get("source_ref"), evidence_item.get("category"), evidence_item.get("sha256"))
                evidence_by_identity.setdefault(key, evidence_item)
        for evidence_item in lifecycle_evidence if isinstance(lifecycle_evidence, list) else []:
            if isinstance(evidence_item, Mapping):
                key = (evidence_item.get("source_ref"), evidence_item.get("category"), evidence_item.get("sha256"))
                evidence_by_identity[key] = evidence_item
        evidence = list(evidence_by_identity.values())
        if is_simulated:
            backtest_entry_evidence_strict = _backtest_entry_evidence_is_strict(root, entry_basis, evidence, entry_verification.get("evidence_refs"))
            backtest_exit_evidence_strict = _backtest_exit_evidence_is_strict(
                root,
                exit_verification.get("basis"),
                evidence,
                exit_verification.get("evidence_refs"),
                exit_verification.get("evidence_bindings"),
            )
            backtest_evidence_strict = backtest_entry_evidence_strict and backtest_exit_evidence_strict
            if backtest_evidence_strict:
                evidence_quality = "governed_supported"
            elif evidence:
                evidence_quality = "owner_declared_unverified"
            else:
                evidence_quality = "insufficient"
        else:
            backtest_evidence_strict = True  # not a BACKTEST gate condition; LIVE is ungated by this predicate.
            evidence_quality = None
        if is_simulated:
            # BACKTEST countability gate (Blocker 1): structurally has no
            # provider identity, so it is never gated on identity_verified or
            # active-only strategy status -- both active and experimental
            # registered strategies are equally countable here, for
            # descriptive analytics only (see strategy_health.py, which
            # separately gates any improvement-candidate eligibility on the
            # B3 *active* rule pin and is untouched by this).
            counted = (
                strategy_registered
                and rule_version_registered
                and state in {"CLOSED", "REVIEW_PENDING", "REVIEWED"}
                and backtest_entry_governed
                and exit_verified
                and close_r is not None
                and not identity_conflict
                # Correction pass #2, Blocker 2 (C6): a governed entry/exit
                # basis alone is not enough -- the ENTRY (and, for
                # owner_attestation exits, the EXIT) evidence backing that
                # basis must be durable and hash-verified, not a bare
                # syntactically-valid-looking claim. This never blocks the
                # record from being created/closed/reviewed (C2) -- it only
                # gates strict countability.
                and backtest_evidence_strict
            )
        else:
            # LIVE countability gate -- unchanged from before Blocker 1.
            # Strategy-stats gate: named strategy only. A real trade whose resolver result is
            # NO_MATCH (or still undecided) never enters named-strategy statistics.
            early_resolution = lifecycle.get("strategy_resolution") if isinstance(lifecycle, Mapping) and isinstance(lifecycle.get("strategy_resolution"), Mapping) else record.get("strategy_resolution") if isinstance(record.get("strategy_resolution"), Mapping) else {}
            # B3's active set selects the immutable historical B1 seed and
            # method-validation cohort, not the allowlist for new operational rows.
            # Use the current registry status and the trade's exact registered
            # frozen version pin for those rows; this also keeps retired strategies
            # and unregistered version pins out of named performance.
            counted = (
                named_strategy_eligible
                and early_resolution.get("status") not in {"no_match", "unresolved"}
                and state in {"CLOSED", "REVIEW_PENDING", "REVIEWED"}
                and identity_verified
                and entry_confirmed
                and exit_verified
                and close_r is not None
                and not any(
                    value in {"AMBIGUOUS", "UNMATCHED", "CONFLICT"}
                    for value in {identity_link.get("match_disposition")}
                )
            )
        if historical_overlap_episode_id:
            # This is the sole overall-performance representative for an
            # exact B1 episode whose frozen historical row was ineligible.
            # It never enters named-strategy observations.
            counted = False
        if counted:
            new_records += 1
        elif not historical_overlap_episode_id:
            excluded_open += 1
        lifecycle_review = lifecycle.get("review") if isinstance(lifecycle, Mapping) and isinstance(lifecycle.get("review"), Mapping) else {}
        record_review = record.get("review") if isinstance(record.get("review"), Mapping) else {}
        strategy_resolution = lifecycle.get("strategy_resolution") if isinstance(lifecycle, Mapping) and isinstance(lifecycle.get("strategy_resolution"), Mapping) else record.get("strategy_resolution") if isinstance(record.get("strategy_resolution"), Mapping) else None
        resolution_status = strategy_resolution.get("status") if isinstance(strategy_resolution, Mapping) else None
        # Owner/performance gate: any real, verified, closed execution with a realized R counts toward
        # overall performance even when strategy attribution is NO_MATCH or UNRESOLVED. Strategy
        # attribution gates named-strategy statistics; it cannot erase verified account-level R/P&L.
        if is_simulated:
            performance_eligible = bool(counted)
        else:
            performance_eligible = bool(
                state in {"CLOSED", "REVIEW_PENDING", "REVIEWED"}
                and identity_verified
                and entry_confirmed
                and exit_verified
                and close_r is not None
                and identity_link.get("match_disposition") not in {"AMBIGUOUS", "UNMATCHED", "CONFLICT"}
            )
        # Keep verified LIVE provider P&L countable even when its supplied R
        # uses a later stop as a proxy for unknown initial risk.
        r_performance_eligible = bool(performance_eligible and r_qualified)
        strategy_stats_eligible = bool(counted and r_qualified) and resolution_status not in {"no_match", "unresolved"}
        setup_tags = _record_value(record, "strategy.setup_tags")
        confirmation_tags = _record_value(record, "strategy.confirmation_tags")
        if not isinstance(setup_tags, list) and isinstance(lifecycle.get("planned") if isinstance(lifecycle, Mapping) else None, Mapping):
            setup_tags = lifecycle["planned"].get("setup_tags")
        if not isinstance(confirmation_tags, list) and isinstance(lifecycle.get("planned") if isinstance(lifecycle, Mapping) else None, Mapping):
            confirmation_tags = lifecycle["planned"].get("confirmation_tags")
        setup_tags = list(setup_tags) if isinstance(setup_tags, list) else None
        confirmation_tags = list(confirmation_tags) if isinstance(confirmation_tags, list) else None
        # Governed provider-execution evidence refs (review blocker #2, issue
        # #44 / PR #45): a source_ref counts as provider-execution evidence
        # only if it is bound into an authority relationship that was itself
        # gated at write time by LifecycleStore._is_provider_evidence --
        # never merely because of its "type" string:
        #   * identity_link.evidence_refs, when the identity link actually
        #     reached MATCHED (create_identity_link requires every one of
        #     these refs to be category="entry"/timing="execution" provider
        #     evidence before it will record a MATCHED disposition);
        #   * execution_verification.entry.evidence_refs, but ONLY for LIVE
        #     rows -- verify_entry (LIVE-only) enforces the same
        #     _is_provider_evidence gate before writing it, while BACKTEST's
        #     confirm_backtest_entry (a structurally different, non-provider
        #     path -- backtest has no broker identity to verify) never does;
        #   * provider_pnl_verification.evidence_refs, which verify_exit only
        #     populates from evidence that passed _is_provider_evidence when
        #     provider_pnl_authority == "provider_read_only".
        governed_provider_source_refs: set[str] = set()
        if identity_verified and isinstance(identity_link.get("evidence_refs"), list):
            governed_provider_source_refs.update(str(ref) for ref in identity_link["evidence_refs"])
        if not is_simulated and entry_confirmed and isinstance(entry_verification.get("evidence_refs"), list):
            governed_provider_source_refs.update(str(ref) for ref in entry_verification["evidence_refs"])
        if isinstance(provider_verification.get("evidence_refs"), list):
            governed_provider_source_refs.update(str(ref) for ref in provider_verification["evidence_refs"])

        def _evidence_role(item: Mapping[str, Any]) -> str:
            if item.get("source_ref") in governed_provider_source_refs:
                return "provider_execution"
            if item.get("category") in {"pre_trade_context", "review"}:
                return "chart_context"
            if is_simulated and item.get("category") in {"entry", "exit"}:
                # BACKTEST is structurally never provider-execution (no
                # broker/provider identity exists to verify -- see the
                # countability gate above): its entry/exit evidence is
                # simulated/manual chart or context evidence by construction,
                # not a type-string guess.
                return "chart_context"
            return "other"

        # Durable content-addressed evidence artifacts only (see evidence_store.py):
        # a plain metadata-only source_ref without a store-backed locator is not
        # something Trade View can safely offer a local file link for.
        evidence_artifacts = [
            {
                "category": item.get("category"),
                "locator": item.get("locator"),
                "sha256": item.get("sha256"),
                "media_type": item.get("media_type"),
                "byte_size": item.get("byte_size"),
                "original_filename": item.get("original_filename"),
                "source_ref": item.get("source_ref"),
                # "type"/"timing" are informational passthrough only (e.g. for
                # Advanced Details) -- presentation role classification uses
                # "evidence_role" below instead, never these strings.
                "type": item.get("type"),
                "timing": item.get("timing"),
                "evidence_role": _evidence_role(item),
            }
            for item in evidence
            if isinstance(item.get("locator"), str) and item["locator"].startswith("artifact:")
        ]
        observability_record = dict(record)
        if isinstance(lifecycle.get("strategy_attribution_amendment"), Mapping):
            observability_record["strategy_attribution_amendment"] = lifecycle["strategy_attribution_amendment"]
        method_observability = _operational_rule_observability(root, observability_record, evidence, strategy_resolution)
        required_rule_states = [
            item.get("state")
            for item in method_observability.get("required_rules", [])
            if item.get("required") is True
        ]
        review_complete = lifecycle.get("review_status") == "reviewed" and (lifecycle_review.get("evidence_completeness") or record_review.get("evidence_completeness")) == "complete"
        operational_method_eligible = bool(
            counted
            and review_complete
            and lifecycle_review.get("rule_compliance") == "compliant"
            and lifecycle_review.get("diagnostic_class") == "METHOD"
            and method_observability.get("status") == "machine_observable"
            and required_rule_states
            and all(state == "SATISFIED" for state in required_rule_states)
        )
        recorded_outcome = _record_value(record, "result.outcome")
        resolved_outcome = recorded_outcome if recorded_outcome in {"win", "loss", "breakeven"} else ("win" if close_r is not None and close_r > 0 else "loss" if close_r is not None and close_r < 0 else "breakeven" if close_r == 0 else "unknown")
        exclusion_reason = None
        if not counted:
            if historical_overlap_episode_id:
                exclusion_reason = "historical_seed_not_performance_eligible"
            elif is_simulated:
                # BACKTEST-shaped exclusion reasons (Blocker 1): never the
                # LIVE-shaped "identity_unverified_or_conflicted" -- a
                # backtest sample structurally has no provider identity to
                # verify, so that reason would misdescribe why it's excluded.
                if not strategy_registered:
                    exclusion_reason = "backtest_strategy_unregistered"
                elif not rule_version_registered:
                    exclusion_reason = "backtest_rule_version_unregistered"
                elif not backtest_entry_governed:
                    exclusion_reason = "backtest_verification_incomplete"
                elif not exit_verified:
                    exclusion_reason = "exit_unverified"
                elif close_r is None:
                    exclusion_reason = "realized_r_unavailable"
                elif not backtest_evidence_strict:
                    # Distinct from the registration/rule-version/verification
                    # reasons above: this is an evidence-QUALITY problem, not
                    # a registration or lifecycle-state problem -- the basis
                    # is governed but not durably/strongly evidenced.
                    exclusion_reason = "backtest_evidence_insufficient"
                else:
                    exclusion_reason = "backtest_verification_incomplete"
            else:
                if not identity_verified:
                    exclusion_reason = "identity_unverified_or_conflicted"
                elif not entry_confirmed:
                    exclusion_reason = "entry_unverified"
                elif not exit_verified:
                    exclusion_reason = "exit_unverified"
                elif close_r is None:
                    exclusion_reason = "realized_r_unavailable"
                elif resolution_status == "no_match":
                    # Real, verified trade that matched no formal strategy: off-strategy, NOT an inactive strategy.
                    exclusion_reason = "strategy_no_match"
                elif resolution_status == "unresolved":
                    # Attribution gap, not a retired strategy. Execution truth can still count at account level.
                    exclusion_reason = "strategy_unresolved"
                elif not strategy_registered:
                    exclusion_reason = "strategy_unregistered"
                elif not strategy_active:
                    exclusion_reason = "inactive_strategy"
                elif not rule_version_registered:
                    exclusion_reason = "strategy_rule_version_unregistered"
                elif not rule_version_frozen:
                    exclusion_reason = "strategy_rule_version_not_frozen"
        records.append({
            "observation_id": historical_overlap_episode_id or record.get("trade_id"),
            "trade_id": record.get("trade_id"),
            "trade_no": record.get("trade_no"),
            "source_kind": "migrated_canonical_overlap" if historical_overlap_episode_id else "post_cutover_operational",
            "counted": counted,
            "performance_eligible": performance_eligible,
            "r_performance_eligible": r_performance_eligible,
            "realized_r_authority_status": r_authority_status,
            "status": "linked_to_historical_seed" if historical_overlap_episode_id else state or "UNPROJECTED",
            "event_date": _record_value(record, "identity.event_date") or (lifecycle.get("created_at") if isinstance(lifecycle, Mapping) else None),
            "time_entry": _record_value(record, "identity.entry_at"),
            "instrument": _record_value(record, "identity.instrument.symbol"),
            "base_asset": base_asset(_record_value(record, "identity.instrument.symbol")),
            "symbol": _record_value(record, "identity.instrument.symbol"),
            "direction": _record_value(record, "identity.direction"),
            "timeframe": _record_value(record, "plan.timeframe_trigger") or "unknown",
            "strategy_id": strategy,
            "rule_version": rule_version,
            "strategy_resolution": strategy_resolution,
            "strategy_resolution_status": strategy_resolution.get("status") if isinstance(strategy_resolution, Mapping) else None,
            "strategy_stats_eligible": strategy_stats_eligible,
            "sample_origin": sample_origin,
            "entry_price": _number(_record_value(record, "actual.entry_price")) or _number(_record_value(record, "plan.entry_price")),
            "exit_price": _number((lifecycle.get("close") or {}).get("exit_price")) if isinstance(lifecycle, Mapping) else None,
            "planned_rr": _number(
                (_corrected_plan(lifecycle) or {}).get("planned_rr", _record_value(record, "plan.rr_pretrade"))
            ),
            "plan_corrected_after_review": bool(
                isinstance(lifecycle, Mapping) and lifecycle.get("plan_corrected_after_review") is True
            ),
            "outcome": resolved_outcome,
            "realized_r": close_r,
            "reported_realized_r": lifecycle.get("reported_realized_r") if isinstance(lifecycle, Mapping) else None,
            "provider_pnl": (lifecycle.get("provider_pnl") if provider_verified else lifecycle.get("reported_provider_pnl")) if isinstance(lifecycle, Mapping) else None,
            "provider_pnl_verified": provider_verified,
            **_provider_net_pnl(
                lifecycle.get("provider_pnl") if isinstance(lifecycle, Mapping) else None,
                provider_verified,
                provider_fees.get(str(identity_link.get("provider_lifecycle_id"))) if identity_verified else None,
            ),
            "identity_verification": {"state": "confirmed" if identity_verified else "not_observable", "match_disposition": identity_link.get("match_disposition")},
            "exit_verification": exit_verification,
            "review_status": lifecycle.get("review_status", "not_started") if isinstance(lifecycle, Mapping) else "not_started",
            "closed_at": lifecycle.get("closed_at") if isinstance(lifecycle, Mapping) else None,
            "setup_tags": setup_tags,
            "confirmation_tags": confirmation_tags,
            "pre_trade_evidence_present": any(item.get("category") == "pre_trade_context" for item in evidence),
            "entry_evidence_present": any(item.get("category") == "entry" for item in evidence),
            "exit_evidence_present": any(item.get("category") == "exit" for item in evidence),
            "review_complete": review_complete,
            "evidence_completeness": lifecycle_review.get("evidence_completeness") or record_review.get("evidence_completeness") or ("observed" if _record_value(record, "evidence") else "not_observable"),
            "diagnostic_class": lifecycle_review.get("diagnostic_class") or record_review.get("diagnostic_class"),
            "execution_deviation": lifecycle_review.get("execution_deviation") or record_review.get("execution_deviation"),
            "rule_compliance": lifecycle_review.get("rule_compliance") or record_review.get("rule_compliance"),
            "rule_violation": lifecycle.get("rule_violation") if isinstance(lifecycle, Mapping) else derive_rule_violation(record_review),
            "method_observability": method_observability,
            "operational_method_eligible": operational_method_eligible,
            "canonical_episode_id": historical_overlap_episode_id,
            "canonical_episode_ids": sorted(candidates),
            "source_path": source_path,
            "evidence_ref": f"canonical/records/trades/{path.name}",
            "exclusion_reason": exclusion_reason,
            # BACKTEST-only audit/classification field (C6/C7): additive
            # metadata alongside sample_origin, never a new primary sample
            # "type" and never a reason to restructure the LIVE/BACKTEST/ALL
            # view. LIVE rows always carry None here -- this predicate never
            # touches LIVE's exit-verification/provider_pnl rules.
            "evidence_quality": evidence_quality,
            "source_method": _record_value(record, "operational_capture.source_method"),
            "outcome_known_at_recording": _record_value(record, "operational_capture.outcome_known_at_recording"),
            "recording_timing": record.get("recording_timing"),
            "evidence_artifacts": evidence_artifacts,
            # Trade View V2 minimum read-model exposure (spec section 20): these
            # values already exist on the canonical record's "plan" section and
            # in lifecycle/record review payloads; they were simply not passed
            # through to the observation row before. Additive presentation
            # fields only -- no canonical semantics, countability, or metric
            # computation is affected by exposing them here.
            "planned_entry": _number((_corrected_plan(lifecycle) or {}).get("intended_entry", _record_value(record, "plan.entry_price"))),
            "planned_stop_loss": _number((_corrected_plan(lifecycle) or {}).get("stop_loss", _record_value(record, "plan.stop_loss_price"))),
            "planned_take_profit": _number((_corrected_plan(lifecycle) or {}).get("take_profit", _record_value(record, "plan.take_profit_price"))),
            "timeframe_context": _record_value(record, "plan.timeframe_context"),
            "timeframe_trigger": _record_value(record, "plan.timeframe_trigger"),
            "market_context": _record_value(record, "plan.market_context"),
            "thesis": _record_value(record, "plan.thesis"),
            "entry_verification": entry_verification,
            "provider": identity_link.get("provider"),
            "review_what_went_well": lifecycle_review.get("what_went_well") or record_review.get("what_went_well"),
            "review_what_went_wrong": lifecycle_review.get("what_went_wrong") or record_review.get("what_went_wrong"),
            "review_thesis_validity": lifecycle_review.get("thesis_validity") or record_review.get("thesis_validity"),
            "review_entry_quality": lifecycle_review.get("entry_quality") or record_review.get("entry_quality"),
            "review_exit_quality": lifecycle_review.get("exit_quality") or record_review.get("exit_quality"),
            "review_owner_notes": lifecycle_review.get("owner_notes") or record_review.get("owner_notes"),
        })
        if not counted and exclusion_reason in {"identity_unverified_or_conflicted", "entry_unverified", "exit_unverified", "realized_r_unavailable"}:
            unresolved_or_open += 1
    return records, {"canonical_trade_records": len(list((root / "canonical" / "records" / "trades").glob("*.json"))), "migrated_overlap_records": overlaps, "new_post_cutover_records": new_records, "non_countable_operational_records": excluded_open, "unlinked_canonical_records": excluded_unlinked, "unresolved_or_open_records": unresolved_or_open}


def _source_commit(root: Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _fresh_baseline(root: Path, source_commit: str) -> str:
    """Pin the fetched main baseline when the current checkout is a branch."""

    try:
        return subprocess.check_output(
            ["git", "merge-base", source_commit, "origin/main"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return source_commit


def _performance_markdown(payload: Mapping[str, Any], artifact_sha: str, generated_at: str, source_commit: str) -> str:
    global_metrics = payload["metrics"]["global"]
    lines = [
        "# Performance",
        "",
        "> GENERATED PROJECTION — DO NOT HAND EDIT.",
        "> All numbers below come from `exports/operational/analytics/current.json`.",
        f"> analytics artifact SHA256: `{artifact_sha}`",
        f"> generated_at: `{generated_at}`",
        f"> source commit: `{source_commit}`",
        "",
        "## Operational rolling universe",
        "",
        f"- Counted realized observations: **{global_metrics['count']}**",
        f"- Total R: **{global_metrics['total_r']}**; mean/expectancy R: **{global_metrics['mean_r']}**; median R: **{global_metrics['median_r']}**",
        f"- Win rate: **{global_metrics['win_rate']}**; profit factor: **{global_metrics['profit_factor']}**",
        f"- Max observed drawdown: **{global_metrics['max_observed_drawdown_r']} R**; winning/losing streak: **{global_metrics['maximum_winning_streak']} / {global_metrics['maximum_losing_streak']}**",
        f"- Live/paper/backtest: **{global_metrics['live_count']} / {global_metrics['paper_count']} / {global_metrics['backtest_count']}**",
        f"- Provider net P&L (after open fee and funding){' — provisional' if global_metrics['provider_net_pnl_funding_gap_count'] else ''}: **{global_metrics['verified_provider_net_pnl']}** from **{global_metrics['provider_net_pnl_observation_count']}** contributor(s)"
        f"; open fees **{global_metrics['provider_open_fee_total']}**, funding **{global_metrics['provider_funding_fee_total']}** (+ = received)"
        + (f"; **{global_metrics['provider_net_pnl_incomplete_count']}** verified trade(s) lack fee data and are not in the net figure" if global_metrics['provider_net_pnl_incomplete_count'] else "")
        + (f"; **{global_metrics['provider_net_pnl_funding_gap_count']}** included trade(s) have an 8-hour settlement with no funding bill, so their funding is incomplete and net P&L may be over- or understated (**{global_metrics['provider_net_pnl_funding_missing_settlements']}** settlement(s) without a bill; a typical funding bill is about **{(payload['source_version_pins'].get('provider_fee_sources') or {}).get('mean_abs_funding_bill')}**)" if global_metrics['provider_net_pnl_funding_gap_count'] else ""),
        f"- Funding fully reconciled against every settlement: **{global_metrics['verified_provider_net_pnl_funding_reconciled']}** from **{global_metrics['provider_net_pnl_funding_reconciled_count']}** trade(s). This subset leaves out every trade with a funding gap whole, winners included, so it is a reconciliation check, not an estimate of the result.",
        f"- Provider close-row P&L (PropW netProfit, before open fee and funding): **{global_metrics['verified_provider_pnl']}** from **{global_metrics['provider_pnl_observation_count']}** verified contributor(s)",
        f"- Overall performance / named-strategy observations: **{payload['counts']['performance_observation_count']} / {payload['counts']['strategy_observation_count']}**",
        "",
        "## Authority and bridge",
        "",
        f"The immutable B1 v3 historical seed contributes `{payload['counts']['historical_seed_count']}` observations. Exact source/episode lineage linked `{payload['counts']['migrated_overlap_records']}` migrated canonical record(s) without adding observations. New post-cutover operational observations: `{payload['counts']['new_post_cutover_records']}`.",
        "",
        "The historical seed is descriptive evidence only. Missing chronology, review, rule-observability, or provider facts remain explicit rather than inferred.",
        "",
        "## Strategy breakdown",
        "",
        "| strategy_id | n | win rate | mean R | median R | PF | live/backtest |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy_id, metrics in payload["metrics"]["by_strategy"].items():
        lines.append(f"| `{strategy_id}` | {metrics['count']} | {metrics['win_rate']} | {metrics['mean_r']} | {metrics['median_r']} | {metrics['profit_factor']} | {metrics['live_count']}/{metrics['backtest_count']} |")
    lines.extend(["", "## Interpretation boundary", "", "These are historical/operational descriptive observations, not proof of future edge, financial advice, a ranking, or a strategy-change instruction."])
    return "\n".join(lines) + "\n"


def refresh_analytics(root: Path, *, generated_at: str | None = None, source_commit: str | None = None, output_dir: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    output = (root / (output_dir or DEFAULT_OUTPUT) if not (output_dir or DEFAULT_OUTPUT).is_absolute() else (output_dir or DEFAULT_OUTPUT)).resolve()
    generated_at = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    source_commit = source_commit or _source_commit(root)
    b1_rows, b1_manifest, b2_manifest = _load_b1(root)
    b2_performance = _load_json(root, "exports/method-validation/b2/strategy-performance.json")
    trader_dna_manifest = _load_json(root, "exports/trader-dna/v1/manifest.json")
    active, version_by_strategy, definition_by_strategy, file_by_strategy = _active_rule_pins(root)
    strategy_registry = load_registry(root)
    active_registry_strategies = {
        str(item.get("strategy_id")): item
        for item in strategy_registry.get("strategies", [])
        if isinstance(item, Mapping) and item.get("strategy_id") and item.get("status") == "active"
    }
    if b1_manifest.get("schema_version") != "method-validation-b1.v3":
        raise OperationalAnalyticsError("operational analytics requires B1 v3")
    selected = [row for row in b1_rows if row.get("performance_counting_eligibility") is True and row.get("strategy_id") in set(active)]
    if len({row.get("canonical_episode_id") for row in selected}) != len(selected):
        raise OperationalAnalyticsError("B1 v3 seed contains duplicate countable episode IDs")
    chronology = _chronology_quality(selected)
    seed = [_b1_observation(row, version_by_strategy[str(row["strategy_id"])]) for row in selected]
    projection = LifecycleStore(root).current()
    canonical_lifecycle_events_sha256 = projection.get("events_sha256")
    if not isinstance(canonical_lifecycle_events_sha256, str) or not canonical_lifecycle_events_sha256:
        raise OperationalAnalyticsError("canonical lifecycle projection is missing events_sha256")
    provider_fees, provider_fee_pin = provider_fee_index(root)
    canonical, canonical_counts = _canonical_observations(
        root, b1_rows, set(active), version_by_strategy, projection, strategy_registry, provider_fees
    )
    countable_new = [row for row in canonical if row.get("strategy_stats_eligible") is True]
    operational_summary = {
        strategy_id: _operational_observability_summary([row for row in canonical if row.get("strategy_id") == strategy_id])
        for strategy_id in sorted(set(active) | {str(row.get("strategy_id")) for row in canonical if row.get("strategy_id")})
    }
    observations = sorted(seed + countable_new, key=chronology_key)
    # Owner performance population: strategy-eligible rows plus real verified off-strategy (NO_MATCH) trades.
    performance_observations = sorted(
        seed + [row for row in canonical if row.get("performance_eligible") is True], key=chronology_key
    )
    for row in {id(r): r for r in (*observations, *performance_observations)}.values():
        row["chronology_quality"] = chronology
    metrics_global = _metrics(performance_observations, chronology)
    simulated_observations = [row for row in observations if row.get("sample_origin") in {"paper", "backtest"}]
    metrics = {
        "global": metrics_global,
        # Named-strategy views use strategy-eligible rows only (NO_MATCH never appears here).
        "by_strategy": _group_metrics(observations, "strategy_id", chronology),
        "by_rule_version": _group_metrics(observations, "rule_version", chronology),
        # Owner-level views use the full performance population.
        # Keyed by underlying, not by the source's spelling of the symbol.
        "by_instrument": _group_metrics(performance_observations, "base_asset", chronology),
        "by_direction": _group_metrics(performance_observations, "direction", chronology),
        "by_timeframe": _group_metrics(performance_observations, "timeframe", chronology),
        "by_date": _group_metrics(performance_observations, "event_date", chronology),
        "by_sample_origin": _group_metrics(performance_observations, "sample_origin", chronology),
        # Simulated-sample audit breakdown of *why* the sample lacks real
        # execution (vocabularies.SOURCE_METHODS). Not a primary UI grouping.
        "by_source_method": _group_metrics(simulated_observations, "source_method", chronology),
    }
    summary_by_mode = _summary_by_mode(performance_observations, chronology)
    strategy_summary_by_mode = _summary_by_mode(observations, chronology)
    named_strategy_summary_by_mode = {
        strategy_id: _summary_by_mode(
            [row for row in observations if row.get("strategy_id") == strategy_id],
            chronology,
        )
        for strategy_id in sorted({str(row.get("strategy_id")) for row in observations if row.get("strategy_id")})
    }
    cumulative_r_by_mode = _cumulative_r_by_mode(performance_observations)
    rule_observability_by_strategy = {
        str(item.get("strategy_id")): item.get("rule_observability", [])
        for item in b2_performance.get("strategies", [])
        if isinstance(item, Mapping) and item.get("strategy_id")
    }
    current = {
        "schema_version": ANALYTICS_SCHEMA,
        "generated_at": generated_at,
        "source_commit": source_commit,
        "canonical_lifecycle_events_sha256": canonical_lifecycle_events_sha256,
        "identity": {"universe_id": "operational-analysis-universe-v1", "analysis_version": "rolling-analysis.v1"},
        "source_version_pins": {
            "canonical_lifecycle": {"path": "canonical/lifecycle/current.json", "events_sha256": canonical_lifecycle_events_sha256},
            "provider_fee_sources": provider_fee_pin,
            "analytics_generator": ANALYTICS_GENERATOR,
            "trading_record_baseline": _fresh_baseline(root, source_commit),
            "trading_record_source_commit": source_commit,
            "b1_v3": {"schema_version": b1_manifest.get("schema_version"), "dataset_sha256": b1_manifest["artifacts"]["dataset"]["sha256"], "manifest_digest": b1_manifest.get("manifest_digest"), "episode_resolution_sha256": b1_manifest["artifacts"]["episode_resolution"]["sha256"]},
            "b3": {"schema_version": _load_json(root, "exports/method-validation/b3/manifest.json").get("schema_version"), "manifest_digest": _load_json(root, "exports/method-validation/b3/manifest.json").get("manifest_digest"), "active_strategy_ids": active, "active_rule_hashes": {key: {"rule_version": version_by_strategy[key], "rule_definition_sha256": definition_by_strategy[key], "version_file_sha256": file_by_strategy[key]} for key in sorted(active)}},
            "named_strategy_registry": {
                "registry_digest": registry_digest(strategy_registry),
                "active_strategy_ids": sorted(active_registry_strategies),
                "registered_rule_version_digests": {
                    strategy_id: {
                        version_id: strategy_version_digest(root, version_id)
                        for version_id in sorted(set(item.get("rule_versions") or []))
                    }
                    for strategy_id, item in sorted(active_registry_strategies.items())
                },
            },
            "b2": {"schema_version": b2_manifest.get("schema_version"), "manifest_digest": b2_manifest.get("manifest_digest"), "performance_artifact_sha256": b2_manifest.get("output_artifacts", {}).get("strategy-performance.json", {}).get("sha256")},
            "trader_dna_v1": {"schema_version": trader_dna_manifest.get("schema_version"), "manifest_digest": trader_dna_manifest.get("manifest_digest"), "manifest_file_sha256": _file_sha(root / "exports/trader-dna/v1/manifest.json")},
        },
        "counts": {"historical_seed_count": len(seed), "historical_seed_episode_ids": len({row["observation_id"] for row in seed}), "countable_observation_count": len(observations), "strategy_observation_count": len(observations), "performance_observation_count": len(performance_observations), "overall_live_performance_count": summary_by_mode["live"]["count"], "named_strategy_live_count": strategy_summary_by_mode["live"]["count"], "verified_provider_pnl_contributor_count": metrics_global["provider_pnl_observation_count"], "verified_provider_net_pnl_contributor_count": metrics_global["provider_net_pnl_observation_count"], **canonical_counts, "superseded_count": sum(row.get("status") == "superseded" for row in canonical), "unresolved_or_open_contribution": 0, "unresolved_or_open_records": canonical_counts.get("unresolved_or_open_records", 0)},
        "chronology_quality": chronology,
        "rule_observability_by_strategy": rule_observability_by_strategy,
        "historical_rule_observability_by_strategy": rule_observability_by_strategy,
        "operational_observability_by_strategy": operational_summary,
        "operational_rule_observability_by_strategy": operational_summary,
        "observations": observations,
        "performance_observations": performance_observations,
        "canonical_read_model": canonical,
        "metrics": metrics,
        "summary_by_mode": summary_by_mode,
        "strategy_summary_by_mode": strategy_summary_by_mode,
        "named_strategy_summary_by_mode": named_strategy_summary_by_mode,
        "cumulative_r_by_mode": cumulative_r_by_mode,
        "guardrails": {"historical_seed_immutable": True, "b1_b2_b3_rerun": False, "double_count_prevented": True, "open_trade_excluded": True, "unresolved_identity_zero_contribution": True, "superseded_zero_contribution": True, "live_backtest_separate": True, "paper_separate": True, "provider_pnl_separate_from_r": True, "optimization_performed": False, "new_backtest_performed": False, "strategy_rule_mutation": False},
    }
    current["artifact_digest"] = _digest(current)
    output.mkdir(parents=True, exist_ok=True)
    current_path = output / "current.json"
    current_path.write_text(json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    artifact_sha = _file_sha(current_path)
    performance_text = _performance_markdown(current, artifact_sha, generated_at, source_commit)
    performance_path = root / PERFORMANCE_PATH
    performance_path.write_text(performance_text, encoding="utf-8", newline="\n")
    manifest = {"schema_version": MANIFEST_SCHEMA, "generated_at": generated_at, "source_commit": source_commit, "artifact": {"path": "exports/operational/analytics/current.json", "sha256": artifact_sha, "artifact_digest": current["artifact_digest"]}, "performance_projection": {"path": "PERFORMANCE.md", "sha256": _file_sha(performance_path)}, "pins": current["source_version_pins"], "canonical_lifecycle_events_sha256": canonical_lifecycle_events_sha256, "counts": current["counts"], "optimization_performed": False, "new_backtest_performed": False}
    manifest["manifest_digest"] = _digest(manifest)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    return manifest


def load_current(root: Path, path: Path | None = None) -> dict[str, Any]:
    target = (root / (path or DEFAULT_OUTPUT / "current.json") if not (path or DEFAULT_OUTPUT).is_absolute() else (path or DEFAULT_OUTPUT / "current.json"))
    payload = json.loads(target.read_text(encoding="utf-8"))
    if payload.get("schema_version") != ANALYTICS_SCHEMA:
        raise OperationalAnalyticsError("invalid operational analytics schema")
    return payload
