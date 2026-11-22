"""Shared BACKTEST (TradingView Replay) registration service.

The single write path for chart-based BACKTEST samples, used by local Agents
(``python -m trading_os backtest-registration``) and by GitHub-only Agents via
``trading_os.github_agent_runner``. It never reads charts itself: the Agent
interprets the images and fills a ``backtest-chart-intake.v1`` payload, which
``prepare_backtest_chart_intake`` validates. Writes go only through the
existing EvidenceStore and LifecycleStore (draft -> evidence -> simulated
entry -> finalize -> Owner verbatim), followed by a projection refresh and
canonical read-back.

* ``preview_backtest_registration`` is read-only and returns a digest that
  binds the exact intake, image bytes and frozen strategy pin.
* ``commit_backtest_registration`` requires that digest plus an explicit
  Owner approval obtained in conversation; an identical retry writes nothing.

A chart-modelled entry is never a provider fill, no execution time is
invented, and a target area reached on a chart is not by itself a realized
result. When the intake carries an ``exit`` block (price, time, optional R, plus
an ``exit_chart`` image) the Owner's chart reading of the exit is part of the SAME
preview digest and the SAME Owner approval: commit registers, closes and verifies
the sample (owner_attestation) in one flow, so it is countable at once (Owner
decision 2026-11-22: replaces the separate register-then-``backtest-exit`` steps
for new samples). Without an ``exit`` block the sample stays OPEN and uncounted;
``trading_os.backtest_exit`` remains only for a later or corrective exit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .backtest_chart_intake import prepare_backtest_chart_intake
from .backtest_exit import BacktestExitError, _dec, _entry_time, _time, close_and_verify, derive_realized_r
from .evidence_store import EvidenceStore
from .instruments import base_asset
from .oslock import os_file_lock
from .operational_lifecycle import LifecycleError, LifecycleStore
from .registry import resolve_version_path

PREVIEW_SCHEMA = "backtest-registration-preview.v1"
IMAGE_PREFIXES = ("registration-intake/", "Research/")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_OPTIONAL_TEXT = ("timeframe_context", "timeframe_trigger", "market_context")


class BacktestRegistrationError(ValueError):
    """Fail closed before any canonical mutation."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _image(root: Path, image_base: Path, ref: str) -> dict[str, Any]:
    if (not isinstance(ref, str) or ref.startswith("/") or "\\" in ref
            or ".." in Path(ref).parts or not ref.startswith(IMAGE_PREFIXES)):
        raise BacktestRegistrationError(
            "image ref must be a repository path under registration-intake/ or Research/")
    path = image_base / ref
    if path.is_symlink() or not path.is_file():
        raise BacktestRegistrationError(f"original image missing: {ref}")
    if not path.resolve().is_relative_to(image_base.resolve()):
        raise BacktestRegistrationError(f"original image outside the repository: {ref}")
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise BacktestRegistrationError(f"unsupported image type: {ref}")
    data = path.read_bytes()
    return {"ref": ref, "path": path, "sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data)}


def _sample_identity(strategy_id: str, image_shas: list[str]) -> tuple[str, str]:
    key = "bt-sample:" + _digest({"strategy_id": strategy_id, "images": sorted(image_shas)})[:32]
    return key, "rec_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def _existing(store: LifecycleStore, draft_id: str, strategy_id: str | None = None,
              image_shas: list[str] | None = None, checked: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Same sample = same deterministic draft, or a BACKTEST of the same strategy
    already bound to these original image bytes at the same entry (e.g. BT000001).

    Reusing a registered chart for a different plan (another entry, or a known
    stop or entry time that differs) is not the same sample: it goes on to the
    duplicate check, where the Owner can say it is different.
    """
    wanted = set(image_shas or [])
    found = []
    for row in store.current().get("records", {}).values():
        if row.get("draft_id") == draft_id:
            found.append(row)
        elif (wanted and row.get("trade_type") == "backtest" and row.get("strategy_id") == strategy_id
              and wanted <= {item.get("sha256") for item in row.get("evidence", []) or []}
              and (checked is None or _same_plan(row, checked, strict=True))):
            found.append(row)
    if len({row.get("trade_id") for row in found}) > 1:
        raise BacktestRegistrationError("BACKTEST sample is duplicated in canonical lifecycle")
    return found[0] if found else None


def _same_price(recorded: Any, wanted: str | None) -> bool | None:
    """Exact price equality, or None when either side is unknown."""
    if recorded is None or wanted is None:
        return None
    try:
        return Decimal(str(recorded)) == Decimal(wanted)
    except InvalidOperation:
        return None


def _same_time(recorded: Any, wanted: str | None) -> bool | None:
    """Equal instants, or None when either is unknown or only one carries a timezone."""
    if not recorded or not wanted:
        return None
    try:
        left, right = (datetime.fromisoformat(str(value).replace("Z", "+00:00")) for value in (recorded, wanted))
    except ValueError:
        return None
    if (left.tzinfo is None) != (right.tzinfo is None):
        return None
    return left == right


def _same_plan(row: Mapping[str, Any], checked: Mapping[str, Any], *, strict: bool = False) -> bool:
    """Same entry price, and no known entry time or stop that tells the two apart.

    Loose (duplicate detection): a differing stop only separates two samples when
    the entry time is unknown, because one trade re-read with another stop but
    the same instant is still a candidate for the Owner to look at.
    Strict (silent ALREADY_REGISTERED): any known difference separates them.
    """
    prices = checked["prices"]
    entry = row.get("entry") if isinstance(row.get("entry"), Mapping) else {}
    planned = row.get("planned") if isinstance(row.get("planned"), Mapping) else {}
    if _same_price(entry.get("actual_entry"), prices["entry"]["value"]) is not True:
        return False
    same_time = _same_time(entry.get("execution_at"), checked["simulated_entry_at"])
    same_stop = _same_price(planned.get("stop_loss_price"), prices["stop"]["value"])
    if strict:
        return same_time is not False and same_stop is not False
    return not (same_time is False or (same_time is None and same_stop is False))


def _possible_duplicates(store: LifecycleStore, strategy_id: str, instrument: str,
                         checked: Mapping[str, Any]) -> list[str]:
    """Registered samples that look like the same replay trade with other image bytes.

    Exact image bytes are the identity, so a re-saved screenshot or one more
    chart would otherwise mint a second BT number. Same strategy, underlying,
    direction and entry price is a possible duplicate unless a known entry time
    or a known stop tells the two apart.
    """
    found = []
    for row in store.current().get("records", {}).values():
        if row.get("trade_type") != "backtest" or row.get("strategy_id") != strategy_id:
            continue
        if base_asset(row.get("symbol")) != base_asset(instrument) or row.get("direction") != checked["direction"]:
            continue
        if _same_plan(row, checked) and isinstance(row.get("trade_no"), str):
            found.append(row["trade_no"])
    return sorted(found)


def _distinct_from(intake: Mapping[str, Any]) -> list[str]:
    """BT numbers the Owner explicitly said are different samples."""
    value = intake.get("distinct_from", [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not re.fullmatch(r"BT[0-9]{6}", item) for item in value
    ):
        raise BacktestRegistrationError("distinct_from must be a list of BT numbers the Owner confirmed are different samples")
    return sorted(set(value))


def _replay_timing(intake: Mapping[str, Any]) -> dict[str, str] | None:
    """Validate the optional Owner supplied replay times using lifecycle rules."""
    if "replay_timing" not in intake:
        return None
    value = intake.get("replay_timing")
    if not isinstance(value, Mapping):
        raise BacktestRegistrationError("replay_timing must contain entry_at, exit_at and source")
    entry, exit_at, source = value.get("entry_at"), value.get("exit_at"), value.get("source")
    if not isinstance(source, str) or not source.strip():
        raise BacktestRegistrationError("replay_timing source must be a non-empty string")
    parsed = []
    for name, raw in (("entry_at", entry), ("exit_at", exit_at)):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00")) if isinstance(raw, str) else None
        except ValueError:
            dt = None
        if dt is None or dt.tzinfo is None:
            raise BacktestRegistrationError(f"backtest timing {name} must be an ISO timestamp with a UTC offset")
        parsed.append(dt)
    if parsed[1] <= parsed[0]:
        raise BacktestRegistrationError("backtest timing exit_at must be after entry_at")
    return {"entry_at": entry, "exit_at": exit_at, "source": source}


def _chart_exit(intake: Mapping[str, Any], checked: Mapping[str, Any], images: list[dict[str, Any]],
                replay_timing: Mapping[str, str] | None) -> dict[str, Any] | None:
    """Validate the optional Owner-confirmed chart exit that closes the sample at registration."""
    if "exit" not in intake:
        return None
    value = intake.get("exit")
    if not isinstance(value, Mapping) or not {"price", "closed_at"} <= set(value):
        raise BacktestRegistrationError("exit must contain price and closed_at (and realized_r only if the stop is unknown)")
    exit_image = next((i for i in images if i["role"] == "exit_chart"), None)
    if exit_image is None:
        raise BacktestRegistrationError("an exit needs an exit_chart original image")
    prices = checked["prices"]
    if prices["entry"]["value"] is None:
        raise BacktestRegistrationError("an exit needs a known entry price")
    try:
        entry = _dec(prices["entry"]["value"], "entry")
        price = _dec(value["price"], "exit price")
        closed = _time(value["closed_at"], "exit closed_at")
        if checked["simulated_entry_at"] and closed <= _entry_time(checked["simulated_entry_at"]):
            raise BacktestRegistrationError("exit closed_at must be after the entry time")
        if replay_timing is not None and closed != _time(replay_timing["exit_at"], "replay_timing exit_at"):
            raise BacktestRegistrationError("exit closed_at differs from replay_timing exit_at")
        r, r_source = derive_realized_r(checked["direction"], entry, prices["stop"]["value"], price,
                                        value.get("realized_r"))
    except BacktestExitError as exc:
        raise BacktestRegistrationError(str(exc)) from exc
    return {"exit_price": str(price), "closed_at": value["closed_at"], "realized_r": str(r), "r_source": r_source,
            "exit_evidence_sha256": exit_image["sha256"], "authority": "owner_attestation",
            "basis": "owner_chart_judgment_not_provider_fill"}


def preview_backtest_registration(
    root: Path, intake: Mapping[str, Any], *, image_base: Path | None = None,
) -> dict[str, Any]:
    """Validate and bind one BACKTEST sample without writing anything."""
    root = Path(root)
    image_base = Path(image_base or root)
    checked = prepare_backtest_chart_intake(intake)
    replay_timing = _replay_timing(intake)
    instrument = intake.get("instrument")
    if not isinstance(instrument, str) or not instrument.strip():
        raise BacktestRegistrationError("instrument is required (e.g. NAS100, BTCUSDT)")
    strategy = checked["strategy"]
    rule_version = strategy.get("rule_version")
    if not isinstance(rule_version, str) or not rule_version.startswith(strategy["strategy_id"] + ":"):
        raise BacktestRegistrationError("strategy.rule_version must pin the frozen version of strategy_id")
    try:
        version_file = resolve_version_path(root, rule_version)
    except Exception as exc:  # unknown or retired version: never guess
        raise BacktestRegistrationError(f"unknown frozen strategy version: {rule_version}") from exc
    if checked["prices"]["entry"]["value"] is None:
        raise BacktestRegistrationError("a BACKTEST sample needs a known entry price (owner or chart-derived)")
    roles = {item["role"] for item in checked["images"]}
    if "entry_chart" not in roles:
        raise BacktestRegistrationError("an entry_chart original image is required")
    images = []
    for item in checked["images"]:
        meta = _image(root, image_base, item["ref"])
        images.append({"role": item["role"], "ref": item["ref"], "sha256": meta["sha256"],
                       "byte_size": meta["byte_size"]})
    shas = [item["sha256"] for item in images]
    if len(set(shas)) != len(shas):
        raise BacktestRegistrationError("the same image bytes are attached twice")
    sample_key, draft_id = _sample_identity(strategy["strategy_id"], shas)
    extras = {
        "instrument": instrument.strip(),
        **{name: str(intake.get(name) or "unknown") for name in _OPTIONAL_TEXT},
        "setup_tags": [str(t) for t in intake.get("setup_tags") or []],
        "confirmation_tags": [str(t) for t in intake.get("confirmation_tags") or []],
    }
    content = {
        "schema_version": PREVIEW_SCHEMA,
        "intake": checked,
        "extras": extras,
        "images": images,
        "strategy_pin": {
            "strategy_id": strategy["strategy_id"],
            "rule_version": rule_version,
            "version_file_sha256": hashlib.sha256(version_file.read_bytes()).hexdigest(),
        },
        "sample_key": sample_key,
        "draft_id": draft_id,
    }
    if replay_timing is not None:
        content["replay_timing"] = replay_timing
    chart_exit = _chart_exit(intake, checked, images, replay_timing)
    if chart_exit is not None:
        content["exit"] = chart_exit
    store = LifecycleStore(root)
    existing = _existing(store, draft_id, strategy["strategy_id"], shas, checked)
    status = "ALREADY_REGISTERED" if existing else "READY_FOR_OWNER_CONFIRMATION"
    distinct = _distinct_from(intake)
    if existing:
        # An already registered sample replays the decision it was registered with, so an identical
        # retry keeps the approved digest: later, similar samples must not change it, and the
        # duplicates seen at registration time are exactly the distinct_from that was confirmed.
        possible = list(distinct)
    else:
        possible = _possible_duplicates(store, strategy["strategy_id"], instrument, checked)
        if distinct and distinct != possible:
            # Never pre-authorise: the Owner answers exactly the duplicates shown.
            raise BacktestRegistrationError(
                "distinct_from must name exactly the possible duplicates "
                + (", ".join(possible) if possible else "(none)") + " that the Owner said are different"
            )
    # Only a duplicate decision enters the digest, so earlier approvals of
    # unrelated samples (and their identical retries) keep the same digest.
    if possible or distinct:
        content["possible_duplicates"] = possible
        content["distinct_from"] = distinct
        if set(possible) - set(distinct):
            status = "POSSIBLE_DUPLICATE"
    numbers = json.loads((root / "canonical/sequences/numbers.json").read_text(encoding="utf-8"))
    return {
        "status": status,
        "possible_duplicates": possible,
        # The same chart images are the same sample even if the Agent now reads other
        # prices from them (an identical retry must stay a no-op). Say so, so the Agent
        # can tell the Owner when the registered plan differs from this intake.
        "existing_plan_matches": (_same_plan(existing, checked, strict=True) if existing else None),
        "preview_digest": _digest(content),
        "content": content,
        "existing_trade_no": existing.get("trade_no") if existing else None,
        "expected_trade_no": None if existing else "BT%06d" % int(numbers.get("backtest_next", 1)),
        "planned_rr_from_chart": checked["planned_rr_from_chart"],
        "result_counted": False,
        "result_counted_after_commit": chart_exit is not None,
    }


@contextmanager
def _commit_lock(root: Path):
    """Serialise the duplicate check and the write, so two re-saved copies cannot both register.

    A crash-safe OS lock on a permanent, git-ignored file (see ``oslock``).
    """
    with os_file_lock(
        root / "canonical" / "registration" / "backtest-commit.lock",
        "timed out waiting for the BACKTEST registration lock",
        error=BacktestRegistrationError,
        attempts=600,
    ):
        yield


def commit_backtest_registration(
    root: Path,
    intake: Mapping[str, Any],
    *,
    preview_digest: str,
    approval: Mapping[str, Any],
    image_base: Path | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Write one Owner-approved BACKTEST sample; an identical retry is a no-op."""
    with _commit_lock(Path(root)):
        return _commit_locked(root, intake, preview_digest=preview_digest, approval=approval,
                              image_base=image_base, recorded_at=recorded_at)


def _commit_locked(
    root: Path,
    intake: Mapping[str, Any],
    *,
    preview_digest: str,
    approval: Mapping[str, Any],
    image_base: Path | None,
    recorded_at: str | None,
) -> dict[str, Any]:
    root = Path(root)
    image_base = Path(image_base or root)
    if (not isinstance(approval, Mapping)
            or approval.get("confirmed_in_chat") is not True
            or not all(isinstance(approval.get(k), str) and approval[k].strip()
                       for k in ("confirmed_at", "agent_id", "owner_statement"))):
        raise BacktestRegistrationError("explicit in-chat Owner approval is required before writing")
    preview = preview_backtest_registration(root, intake, image_base=image_base)
    if preview["preview_digest"] != preview_digest:
        raise BacktestRegistrationError("intake, images or strategy changed since the Owner preview")
    if preview["status"] == "POSSIBLE_DUPLICATE":
        raise BacktestRegistrationError(
            "this looks like already registered " + ", ".join(preview["possible_duplicates"])
            + "; register it only if the Owner confirms it is a different sample (distinct_from)"
        )
    content = preview["content"]
    if content.get("distinct_from") and approval.get("distinct_from_confirmed") != content["distinct_from"]:
        raise BacktestRegistrationError(
            "the Owner approval must confirm distinct_from " + ", ".join(content["distinct_from"])
            + " (approval.distinct_from_confirmed)"
        )
    store = LifecycleStore(root)
    existing = _existing(store, content["draft_id"], content["strategy_pin"]["strategy_id"],
                         [item["sha256"] for item in content["images"]], content["intake"])
    if existing:
        result = _readback(store, existing["trade_no"], content, status="ALREADY_REGISTERED")
        result = _append_requested_timing(store, existing, content, result, approval)
        return _append_requested_exit(store, existing, content, result)

    checked, extras = content["intake"], content["extras"]
    prices = checked["prices"]
    evidence_store = EvidenceStore(root)
    attached = {}
    for item in content["images"]:
        source = image_base / item["ref"]
        details = evidence_store.ingest(source, original_filename=source.name)
        if details["sha256"] != item["sha256"]:
            raise BacktestRegistrationError("image bytes changed between preview and write")
        if evidence_store.read(details["locator"], expected_sha256=details["sha256"]) != source.read_bytes():
            raise BacktestRegistrationError("EvidenceStore cannot read the exact original back")
        # Replay is review/post_trade provenance even when it shows the entry.
        attached[item["role"]] = {
            **{k: v for k, v in details.items() if k != "deduped"},
            "source_ref": details["locator"],
            "type": "replay_chart",
            "method": "replay_chart",
            "category": "review",
            "timing": "post_trade",
            "provenance": {"capture_mode": "retrospective_replay", "capture_time_status": "not_observable"},
            "role": "historical_market_reconstruction",
            "original_repository_path": item["ref"],
        }
    now = recorded_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def price(name: str) -> float | None:
        value = prices[name]["value"]
        return float(value) if value is not None else None

    rr = checked["planned_rr_from_chart"]
    draft = store.create_draft(
        strategy_id=content["strategy_pin"]["strategy_id"],
        rule_version=content["strategy_pin"]["rule_version"],
        symbol=extras["instrument"],
        direction=checked["direction"],
        intended_entry=price("entry"),
        stop_loss=price("stop"),
        take_profit=price("target"),
        planned_rr=float(rr) if rr is not None else None,
        timeframe_context=extras["timeframe_context"],
        timeframe_trigger=extras["timeframe_trigger"],
        market_context=extras["market_context"],
        setup_tags=extras["setup_tags"],
        confirmation_tags=extras["confirmation_tags"],
        thesis=checked["owner_verbatim"],
        chart_refs=[attached[i["role"]]["source_ref"] for i in content["images"]],
        created_at=now,
        draft_id=content["draft_id"],
        recording_timing="retrospective",
        trade_type="backtest",
        execution_environment="paper",
        allow_experimental=True,
        source_method="tradingview_replay",
        outcome_known_at_recording=True,
    )
    draft_id = draft["draft_id"]
    for item in content["images"]:
        store.attach_evidence(draft_id, attached[item["role"]])
    entry_ref = attached["entry_chart"]["source_ref"]
    store.confirm_backtest_entry(
        draft_id,
        actual_entry=price("entry"),
        execution_at=checked["simulated_entry_at"],
        source_ref=entry_ref,
        execution_metadata={
            "source_method": "tradingview_replay",
            "time_quality": "known" if checked["simulated_entry_at"] else "not_observable",
            "price_provenance": prices["entry"]["provenance"],
            "is_chart_modelled_not_provider_fill": True,
            "realized_profit_verified": False,
        },
    )
    saved = store.finalize(draft_id, reviewer="owner")
    store.record_owner_statement(saved["trade_id"], verbatim_text=checked["owner_verbatim"],
                                 source_id=content["sample_key"])
    timing_recorded = False
    if content.get("replay_timing") is not None:
        try:
            store.record_backtest_timing(
                saved["trade_id"], **{k: content["replay_timing"][k] for k in ("entry_at", "exit_at")},
                source_id=_timing_source_id(content), confirmed_at=approval["confirmed_at"],
            )
        except LifecycleError as exc:
            raise BacktestRegistrationError(str(exc)) from exc
        timing_recorded = True
    if content.get("exit") is not None:
        _apply_exit(store, saved["trade_id"], "OPEN", content["exit"], attached["exit_chart"]["source_ref"])
    from .projections import refresh_projections
    projection = refresh_projections(root)
    if projection.get("status") != "CURRENT":
        raise BacktestRegistrationError("projection chain did not verify CURRENT")
    result = _readback(store, saved["trade_no"], content, status="REGISTERED")
    result["timing_recorded"] = timing_recorded
    if content.get("replay_timing") is not None:
        result["replay_timing"] = content["replay_timing"]
    return result


def _timing_source_id(content: Mapping[str, Any]) -> str:
    """Timing identity ignores the exit block, so adding the exit to an already timed sample is a retry, not a conflict."""
    return f"replay-timing:{_digest({k: v for k, v in content.items() if k != 'exit'})[:16]}"


def _apply_exit(store: LifecycleStore, trade_id: str, state: str, chart_exit: Mapping[str, Any],
                exit_ref: str, recorded_close: Any = None) -> None:
    # Fresh append time: the draft's created_at is older than the timing and statement events.
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        close_and_verify(store, trade_id=trade_id, state=state, exit_price=chart_exit["exit_price"],
                         closed_at=chart_exit["closed_at"], realized_r=chart_exit["realized_r"],
                         exit_ref=exit_ref, recorded_at=now, recorded_close=recorded_close)
    except BacktestExitError as exc:
        raise BacktestRegistrationError(str(exc)) from exc


def _append_requested_exit(store: LifecycleStore, existing: Mapping[str, Any], content: Mapping[str, Any],
                           result: dict[str, Any]) -> dict[str, Any]:
    """An identical retry of an OPEN sample closes it with the approved exit; a verified one writes nothing."""
    chart_exit = content.get("exit")
    if chart_exit is None:
        return result
    row = store.current().get("records", {}).get(existing["trade_id"], {})
    if (row.get("exit_verification") or {}).get("state") != "confirmed":
        item = next((e for e in row.get("evidence", []) if e.get("sha256") == chart_exit["exit_evidence_sha256"]), None)
        if item is None:
            raise BacktestRegistrationError("the registered sample has no saved exit_chart image")
        _apply_exit(store, existing["trade_id"], row.get("state"), chart_exit,
                    item.get("locator") or item.get("source_ref"), row.get("close"))
        from .projections import refresh_projections
        if refresh_projections(store.root).get("status") != "CURRENT":
            raise BacktestRegistrationError("projection chain did not verify CURRENT")
    final = _readback(store, existing["trade_no"], content, status="ALREADY_REGISTERED")
    final["timing_recorded"] = result.get("timing_recorded", final["timing_recorded"])
    if "replay_timing" in result:
        final["replay_timing"] = result["replay_timing"]
    return final


def _append_requested_timing(store: LifecycleStore, existing: Mapping[str, Any], content: Mapping[str, Any],
                             result: dict[str, Any], approval: Mapping[str, Any]) -> dict[str, Any]:
    timing = content.get("replay_timing")
    if timing is None:
        return result
    result["timing_recorded"] = False
    record = store.project().get("records", {}).get(existing["trade_id"], {})
    recorded = record.get("backtest_timing") or {}
    if recorded:
        expected_source_id = _timing_source_id(content)
        if (recorded.get("entry_at") != timing["entry_at"]
                or recorded.get("exit_at") != timing["exit_at"]
                or recorded.get("source_id") != expected_source_id):
            raise BacktestRegistrationError("backtest timing already exists with different content")
        result["timing_recorded"] = True
    else:
        try:
            store.record_backtest_timing(
                existing["trade_id"], entry_at=timing["entry_at"], exit_at=timing["exit_at"],
                source_id=_timing_source_id(content), confirmed_at=approval["confirmed_at"],
            )
        except LifecycleError as exc:
            raise BacktestRegistrationError(str(exc)) from exc
        from .projections import refresh_projections
        projection = refresh_projections(store.root)
        if projection.get("status") != "CURRENT":
            raise BacktestRegistrationError("projection chain did not verify CURRENT")
        result["timing_recorded"] = True
    result["replay_timing"] = timing
    return result


def _readback(store: LifecycleStore, trade_no: str, content: Mapping[str, Any], *, status: str) -> dict[str, Any]:
    rows = [row for row in store.current().get("records", {}).values() if row.get("trade_no") == trade_no]
    if len(rows) != 1:
        raise BacktestRegistrationError("canonical read-back does not contain exactly one sample")
    row = rows[0]
    if not str(trade_no).startswith("BT") or row.get("trade_type") != "backtest":
        raise BacktestRegistrationError("read-back is not a BACKTEST sample")
    expected = {item["sha256"] for item in content["images"]}
    if expected - {item.get("sha256") for item in row.get("evidence", [])}:
        raise BacktestRegistrationError("read-back is missing an original image binding")
    if row.get("provider_pnl") is not None:
        raise BacktestRegistrationError("BACKTEST unexpectedly gained provider P&L")
    if not content["intake"]["simulated_entry_at"] and (row.get("entry") or {}).get("execution_at") is not None:
        raise BacktestRegistrationError("an execution time was fabricated")
    return {
        "status": status,
        "trade_no": trade_no,
        "trade_id": row["trade_id"],
        "image_sha256": sorted(expected),
        "planned_rr_from_chart": content["intake"]["planned_rr_from_chart"],
        "state": row.get("state"),
        "result_counted": (row.get("exit_verification") or {}).get("state") == "confirmed"
                          and row.get("state") in {"CLOSED", "REVIEW_PENDING", "REVIEWED"},
        "timing_recorded": bool(row.get("backtest_timing")),
    }
