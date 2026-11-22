"""Owner-confirmed chart exit for a registered BACKTEST sample.

Why this exists (Owner decision, 2026-11-22): a BACKTEST is judged from the
Replay chart, so the Owner's chart reading of the exit must be able to close the
sample and make it countable. Before this, image-registered BACKTESTs stayed OPEN
forever (``result_counted: False``), so no new BACKTEST ever reached performance.

This replaces nothing in LIVE. It composes the existing lifecycle calls that
analytics already accepts for BACKTEST (``final_close`` then ``verify_exit`` with
``owner_attestation`` bound to a durable ``post_trade`` Replay image), so there is
no second lifecycle and no new countability rule:

* the exit is an Owner chart judgment, never a provider fill (``owner_attestation``);
* the exit image must already be saved on the trade (its EvidenceStore locator) and
  stays review/post_trade evidence (evidence_time forbids a Replay image the exit
  category); analytics accepts that bound Replay image as strict BACKTEST evidence;
* realized R is derived from the registered entry and stop plus the confirmed exit
  price, never typed by hand, unless the stop is unknown;
* ``preview_backtest_exit`` is read-only and returns a digest; ``commit_backtest_exit``
  needs that digest plus an explicit in-chat Owner approval.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .operational_lifecycle import LifecycleError, LifecycleStore

PREVIEW_SCHEMA = "backtest-exit-preview.v1"
_COPIED = ("source_ref", "locator", "sha256", "byte_size", "media_type", "original_filename",
           "original_repository_path", "type", "method", "timing", "timing_relative_to_trade",
           "provenance", "role", "captured_at", "represented_market_time")


class BacktestExitError(ValueError):
    """Fail closed before any canonical mutation."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _dec(value: Any, name: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        raise BacktestExitError(f"{name} must be a number") from None
    if not out.is_finite() or out <= 0:
        raise BacktestExitError(f"{name} must be a positive number")
    return out


def _time(value: Any, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise BacktestExitError(f"{name} must be an ISO timestamp with a UTC offset")
    return parsed


def _entry_time(value: Any) -> datetime:
    """Older registrations stored the entry time without an offset; those are Owner local time (+08:00)."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T", 1)) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None:
        raise BacktestExitError("entry time is not an ISO timestamp")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone(timedelta(hours=8)))


def _row(store: LifecycleStore, trade_no: str) -> Mapping[str, Any]:
    rows = [r for r in store.current().get("records", {}).values() if r.get("trade_no") == trade_no]
    if len(rows) != 1:
        raise BacktestExitError(f"{trade_no} is not exactly one registered sample")
    if rows[0].get("trade_type") != "backtest" or not str(trade_no).startswith("BT"):
        raise BacktestExitError("chart exit applies to BACKTEST samples only")
    return rows[0]


def derive_realized_r(direction: str, entry: Decimal, stop: Any, price: Decimal,
                      realized_r: Any) -> tuple[Decimal, str]:
    """Realized R from entry, stop and the confirmed exit; the Owner states R only when the stop is unknown."""
    derived = None
    if stop is not None:
        risk = (entry - _dec(stop, "stop")) if direction == "long" else (_dec(stop, "stop") - entry)
        if risk <= 0:
            raise BacktestExitError("registered stop contradicts the trade direction")
        move = (price - entry) if direction == "long" else (entry - price)
        derived = (move / risk).quantize(Decimal("0.000001"))
    if realized_r is None:
        if derived is None:
            raise BacktestExitError("the stop is unknown, so realized_r must be stated by the Owner")
        return derived, "derived_from_entry_stop_exit"
    r = Decimal(str(realized_r))
    if derived is not None and abs(r - derived) > Decimal("0.001"):
        raise BacktestExitError(f"realized_r {r} disagrees with entry/stop/exit ({derived})")
    return r, "owner_stated" if derived is None else "derived_from_entry_stop_exit"


def close_and_verify(store: LifecycleStore, *, trade_id: str, state: str, exit_price: Any, closed_at: str,
                     realized_r: Any, exit_ref: str, recorded_at: str, recorded_close: Any = None) -> None:
    """The single close path: final_close (if still OPEN) then owner_attestation verify_exit."""
    r_value = float(realized_r)
    try:
        if state == "OPEN":
            store.final_close(trade_id, exit_price=float(exit_price), closed_at=closed_at,
                              realized_r=r_value, source_ref=exit_ref, recorded_at=recorded_at)
        elif float((recorded_close or {}).get("exit_price", exit_price)) != float(exit_price):
            raise BacktestExitError("the sample is already closed at a different exit price")
        store.verify_exit(trade_id, authority_class="owner_attestation",
                          evidence_refs=[exit_ref], realized_r=r_value, recorded_at=recorded_at)
    except LifecycleError as exc:
        raise BacktestExitError(str(exc)) from exc


def _plan(row: Mapping[str, Any], exit_price: Any, closed_at: str, exit_evidence_ref: str,
          realized_r: Any) -> dict[str, Any]:
    entry_info = row.get("entry") if isinstance(row.get("entry"), Mapping) else {}
    planned = row.get("planned") if isinstance(row.get("planned"), Mapping) else {}
    direction = row.get("direction")
    if direction not in {"long", "short"} or entry_info.get("actual_entry") is None:
        raise BacktestExitError("the sample has no registered entry price or direction")
    entry = _dec(entry_info["actual_entry"], "entry")
    price = _dec(exit_price, "exit_price")
    closed = _time(closed_at, "closed_at")
    entered = entry_info.get("execution_at")
    if entered and closed <= _entry_time(entered):
        raise BacktestExitError("closed_at must be after the entry time")
    timing = row.get("backtest_timing") if isinstance(row.get("backtest_timing"), Mapping) else {}
    if timing.get("exit_at") and _time(timing["exit_at"], "recorded exit_at") != closed:
        raise BacktestExitError("closed_at differs from the Owner-confirmed replay exit_at already recorded")
    item = next((e for e in row.get("evidence", [])
                 if isinstance(e, Mapping) and exit_evidence_ref in (e.get("locator"), e.get("source_ref"))
                 and str(e.get("media_type", "")).startswith("image/")), None)
    if item is None:
        raise BacktestExitError("exit_evidence_ref must be an image already saved on this sample")
    provenance = item.get("provenance") if isinstance(item.get("provenance"), Mapping) else {}
    if item.get("timing") != "post_trade" or item.get("type") != "replay_chart" or provenance.get("capture_mode") != "retrospective_replay":
        raise BacktestExitError("exit evidence must be a saved post_trade Replay chart image")
    stop = planned.get("stop_loss_price")
    r, r_source = derive_realized_r(direction, entry, stop, price, realized_r)
    return {
        "trade_no": row["trade_no"], "trade_id": row["trade_id"], "direction": direction,
        "entry_price": str(entry), "stop_price": None if stop is None else str(stop),
        "exit_price": str(price), "closed_at": closed_at, "realized_r": str(r),
        "r_source": r_source,
        "exit_evidence_ref": item.get("locator") or item.get("source_ref"),
        "exit_evidence_sha256": item.get("sha256"), "evidence_item": {k: item[k] for k in _COPIED if k in item},
        "authority": "owner_attestation", "basis": "owner_chart_judgment_not_provider_fill",
    }


def preview_backtest_exit(root: Path, *, trade_no: str, exit_price: Any, closed_at: str,
                          exit_evidence_ref: str, realized_r: Any = None) -> dict[str, Any]:
    store = LifecycleStore(Path(root))
    row = _row(store, trade_no)
    state = row.get("state")
    if state not in {"OPEN", "CLOSED"}:
        raise BacktestExitError(f"{trade_no} is {state}; chart exit needs an OPEN sample")
    already = (row.get("exit_verification") or {}).get("state") == "confirmed"
    content = _plan(row, exit_price, closed_at, exit_evidence_ref, realized_r)
    return {
        "schema_version": PREVIEW_SCHEMA,
        "status": "ALREADY_VERIFIED" if already else "READY_FOR_OWNER_APPROVAL",
        "preview_digest": _digest(content), "content": content, "result_counted_after_commit": True,
    }


def commit_backtest_exit(root: Path, *, trade_no: str, exit_price: Any, closed_at: str,
                         exit_evidence_ref: str, preview_digest: str, approval: Mapping[str, Any],
                         realized_r: Any = None, recorded_at: str | None = None) -> dict[str, Any]:
    if (not isinstance(approval, Mapping) or approval.get("confirmed_in_chat") is not True
            or not all(isinstance(approval.get(k), str) and approval[k].strip()
                       for k in ("confirmed_at", "agent_id", "owner_statement"))):
        raise BacktestExitError("explicit in-chat Owner approval is required before writing")
    root = Path(root)
    preview = preview_backtest_exit(root, trade_no=trade_no, exit_price=exit_price, closed_at=closed_at,
                                    exit_evidence_ref=exit_evidence_ref, realized_r=realized_r)
    if preview["preview_digest"] != preview_digest:
        raise BacktestExitError("exit, evidence or sample changed since the Owner preview")
    if preview["status"] == "ALREADY_VERIFIED":
        return _readback(LifecycleStore(root), trade_no, preview["content"], "ALREADY_VERIFIED")
    content = preview["content"]
    store = LifecycleStore(root)
    row = _row(store, trade_no)
    now = recorded_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    close_and_verify(store, trade_id=content["trade_id"], state=row.get("state"),
                     exit_price=content["exit_price"], closed_at=content["closed_at"],
                     realized_r=content["realized_r"], exit_ref=content["exit_evidence_ref"],
                     recorded_at=now, recorded_close=row.get("close"))
    from .projections import refresh_projections
    projection = refresh_projections(root)
    if projection.get("status") != "CURRENT":
        raise BacktestExitError("projection chain did not verify CURRENT")
    return _readback(store, trade_no, content, "EXIT_RECORDED")


def _readback(store: LifecycleStore, trade_no: str, content: Mapping[str, Any], status: str) -> dict[str, Any]:
    row = _row(store, trade_no)
    verified = (row.get("exit_verification") or {}).get("state") == "confirmed"
    if status != "ALREADY_VERIFIED" and not (verified and row.get("state") in {"CLOSED", "REVIEW_PENDING", "REVIEWED"}):
        raise BacktestExitError("canonical read-back does not show a closed sample with a verified exit")
    return {"status": status, "trade_no": trade_no, "trade_id": row["trade_id"], "state": row.get("state"),
            "exit_verification": (row.get("exit_verification") or {}).get("state"),
            "exit_basis": (row.get("exit_verification") or {}).get("basis"),
            "realized_r": content["realized_r"], "exit_price": content["exit_price"],
            "closed_at": content["closed_at"]}
