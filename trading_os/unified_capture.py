"""Owner-facing capture facade.

This module builds no new state model. Every function below is orchestration
only: it calls the existing :class:`~trading_os.operational_lifecycle.
LifecycleStore` methods in the sequence an owner would otherwise run by hand
through the ``capture`` CLI subcommands one at a time. Nothing here writes to
disk directly and nothing here bypasses a lifecycle transition check.

Status of this pass:
  * ``capture_backtest`` -- complete. draft -> evidence -> confirm entry
    (via the simulator-run basis) -> finalize, in one call.
  * ``capture_retrospective_live`` -- complete for the draft-through-
    finalize span, which is the genuinely repetitive part. It composes
    ``create_draft`` (with ``recording_timing="retrospective"`` and the
    historical fact time as ``created_at``) through
    ``mark_entry_pending`` / ``create_identity_link`` / ``verify_entry`` /
    ``finalize``, proving (see tests/test_retrospective_live_e2e.py) that a
    late-reported real execution needs no new lifecycle logic -- only this
    orchestration layer.
  * ``capture_live`` -- same span as the retrospective case, for a same-day
    (contemporaneous) live report.

Deliberately NOT folded in here: partial-close / final-close / verify-exit /
review. Those depend on facts that are not known at capture time (exit
price, realized R, review content) and arrive on their own schedule --
collapsing them into this facade would just be a same-shaped wrapper around
a single call each, not a reduction in owner-facing steps. Call them
directly on the ``LifecycleStore`` returned to the caller.

TODO(unified-capture-v1 phase 2): if the owner finds the 8-call manual
sequence for closing/reviewing a trade is *also* a friction point in
practice, add a ``close_and_review(...)`` follow-up here and CLI
subcommands (`capture-live` / `capture-backtest` / `capture-retrospective-
live`) that call these functions. Left out of this pass so A-D (evidence
store, source_method, outcome_known_at_recording) landed done-and-tested
first, per the phase-1 priority order.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .operational_lifecycle import LifecycleError, LifecycleStore, canonical_provider
from .registration_census import registration_state
from .strategy_resolver import resolve_strategy, to_lifecycle_resolution
from .vocabularies import UNFROZEN_EXPERIMENTAL_PLACEHOLDER


def capture_backtest(
    store: LifecycleStore,
    *,
    strategy_id: str,
    rule_version: str,
    symbol: str,
    direction: str,
    intended_entry: float,
    stop_loss: float,
    take_profit: float,
    planned_rr: float,
    timeframe_context: str,
    timeframe_trigger: str,
    market_context: str,
    setup_tags: list[str],
    confirmation_tags: list[str],
    thesis: str,
    chart_refs: list[str],
    created_at: str,
    source_method: str,
    entry_evidence: Mapping[str, Any],
    actual_entry: float,
    execution_at: str | None,
    execution_metadata: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    parameters_ref: str | None = None,
    dataset_ref: str | None = None,
    data_period: str | Mapping[str, str] | None = None,
    outcome_known_at_recording: bool | str = "unknown",
    recording_timing: str = "contemporaneous",
    execution_environment: str = "paper",
    draft_id: str | None = None,
    variant: str | None = None,
    recorded_at: str | None = None,
    allow_experimental: bool = False,
    allow_retired: bool = False,
    reviewer: str = "owner",
) -> dict[str, Any]:
    """Draft, attach entry evidence, confirm entry, and finalize a backtest.

    ``entry_evidence`` must be a full evidence dict (source_ref, sha256,
    timing, type, category="entry") -- typically the result of
    ``EvidenceStore.ingest`` plus those lifecycle-evidence fields. Its
    ``source_ref`` is what ``confirm_backtest_entry`` binds the execution
    claim to.

    ``run_id``/``parameters_ref``/``dataset_ref``/``data_period`` are all
    optional -- omit all four for the single-sample case where there is no
    formal run/session ("there was a setup, I didn't take it, count it as a
    backtest"); ``confirm_backtest_entry`` then uses the
    ``manual_declared_execution`` basis instead of ``simulator_run``. Nothing
    is invented in their place.

    ``rule_version="unfrozen:experimental"`` is rejected here even though the
    lower-level ``LifecycleStore.create_draft`` still accepts it for a
    non-Unified-Capture caller: it is not a valid registered/frozen authority
    for a Unified Capture backtest sample (see operational_analytics'
    countability gate, which fails closed on the same placeholder).
    """

    if rule_version == UNFROZEN_EXPERIMENTAL_PLACEHOLDER:
        raise LifecycleError(
            "Unified Capture backtest requires a real registered/frozen rule_version; "
            f"'{UNFROZEN_EXPERIMENTAL_PLACEHOLDER}' is not a valid authority here -- "
            "register the strategy's rule_versions entry first"
        )

    draft = store.create_draft(
        strategy_id=strategy_id, rule_version=rule_version, symbol=symbol, direction=direction,
        intended_entry=intended_entry, stop_loss=stop_loss, take_profit=take_profit, planned_rr=planned_rr,
        timeframe_context=timeframe_context, timeframe_trigger=timeframe_trigger, market_context=market_context,
        setup_tags=setup_tags, confirmation_tags=confirmation_tags, thesis=thesis, chart_refs=chart_refs,
        created_at=created_at, recording_timing=recording_timing, trade_type="backtest",
        execution_environment=execution_environment, draft_id=draft_id, variant=variant,
        recorded_at=recorded_at, allow_experimental=allow_experimental, allow_retired=allow_retired,
        source_method=source_method, outcome_known_at_recording=outcome_known_at_recording,
    )
    draft_id_resolved = draft["draft_id"]
    store.attach_evidence(draft_id_resolved, dict(entry_evidence), recorded_at=recorded_at)
    store.confirm_backtest_entry(
        draft_id_resolved,
        actual_entry=actual_entry,
        execution_at=execution_at,
        execution_metadata=execution_metadata,
        run_id=run_id,
        parameters_ref=parameters_ref,
        dataset_ref=dataset_ref,
        data_period=data_period,
        source_ref=entry_evidence["source_ref"],
        recorded_at=recorded_at,
    )
    finalized = store.finalize(draft_id_resolved, reviewer, recorded_at=recorded_at)
    return {"draft_id": draft_id_resolved, **finalized}


def resolve_live_strategy(
    store: LifecycleStore,
    strategy_observations: Mapping[str, Any],
    *,
    intended_entry: Any,
    stop_loss: Any,
    take_profit: Any,
    planned_rr: Any,
    contemporaneous: bool,
    provider_entry_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve attribution for a LIVE draft; unresolved is persisted as attribution state."""

    if not isinstance(strategy_observations, Mapping):
        raise LifecycleError("LIVE capture requires strategy_observations for the strategy resolver")
    observations = dict(strategy_observations)
    plan = {
        k: v for k, v in (
            ("entry_price", intended_entry),
            ("stop_loss_price", stop_loss),
            ("target_price", take_profit),
            ("planned_rr", planned_rr),
        )
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    # The plan fields committed by this capture call are the persisted facts;
    # a resolver input file may add context but cannot override them.
    observations["plan"] = {
        **(observations.get("plan") if isinstance(observations.get("plan"), Mapping) else {}),
        **plan,
    }
    result = resolve_strategy(store.root, observations, provider_entry_at=provider_entry_at)
    return result, to_lifecycle_resolution(result, contemporaneous=contemporaneous)


def _normalized_instrument(value: Any) -> str:
    """Compare provider base instruments with canonical pair symbols safely."""

    normalized = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    for quote in ("USDT", "USDC", "USD"):
        if normalized.endswith(quote) and len(normalized) > len(quote):
            return normalized[: -len(quote)]
    return normalized


def capture_live(
    store: LifecycleStore,
    *,
    strategy_observations: Mapping[str, Any] | None,
    symbol: str,
    direction: str,
    intended_entry: float | str | None,
    stop_loss: float | str | None,
    take_profit: float | str | None,
    planned_rr: float | str | None,
    timeframe_context: str,
    timeframe_trigger: str,
    market_context: str,
    setup_tags: list[str],
    confirmation_tags: list[str],
    thesis: str,
    chart_refs: list[str],
    created_at: str,
    provider_evidence: Mapping[str, Any],
    provider: str,
    provider_lifecycle_id: str,
    provider_execution_timestamp: str,
    candidate_source: str,
    actual_entry: float,
    chart_evidence: Mapping[str, Any] | None = None,
    outcome_known_at_recording: bool | str = "unknown",
    recording_timing: str = "contemporaneous",
    execution_environment: str = "prop_challenge",
    draft_id: str | None = None,
    variant: str | None = None,
    recorded_at: str | None = None,
    identity_authority: str = "deterministic_match",
    reviewer: str = "owner",
    frozen_registration_packet: Mapping[str, Any] | None = None,
    registration_evidence_manifest: Any = None,
    registration_evidence_root: Any = None,
    additional_evidence: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Strategy resolver -> draft -> [chart evidence] -> provider evidence ->
    entry-pending -> identity-link -> verify-entry -> finalize.

    Normal LIVE capture resolves ``strategy_observations`` against every active
    frozen rule set. The registration-execution path is different: it supplies an
    already Owner-confirmed ``frozen_registration_packet``, validates that packet,
    and consumes its frozen resolver result directly. It never adds SL/TP plan facts
    or runs a second semantic strategy resolution.

    * MATCHED -> the draft carries the resolved ``strategy_id``/``rule_version``.
    * NO_MATCH -> a real trade is registered with null strategy identity and is
      not named-strategy stats eligible.
    * AMBIGUOUS / INSUFFICIENT_EVIDENCE -> a real trade is still registered;
      unresolved attribution is stored on the lifecycle without a fake strategy.

    Registration never runs post-trade review; the trade is left with
    ``review.status == "not_started"`` for the separate Trade Review workflow.

    Used for both a same-day live report (``recording_timing=
    "contemporaneous"``, default) and a late historical report
    (``recording_timing="retrospective"``, with ``created_at`` set to the
    real historical trade time and this call made well after the fact --
    see ``capture_retrospective_live`` below, which is this same function).

    ``provider_evidence`` and ``chart_evidence`` are two deliberately
    separate evidence slots and must never be collapsed into one:

    * ``provider_evidence`` is the actual provider/broker-sourced execution
      evidence (a full evidence dict -- source_ref, sha256, timing, type,
      category="entry" -- typically ``EvidenceStore.ingest`` plus those
      lifecycle-evidence fields, with ``type`` one of
      ``operational_lifecycle.PROVIDER_EXECUTION_EVIDENCE_TYPES``). This is
      the ONLY evidence ever referenced by ``create_identity_link`` /
      ``verify_entry`` -- it is the sole source of PROVIDER EXECUTION
      AUTHORITY for this trade. It is required: LIVE capture always attempts
      identity-link + entry verification, and there is no default that can
      stand in for real provider evidence without silently mislabeling
      something else as provider truth.
    * ``chart_evidence`` is optional owner-supplied context (a chart/
      screenshot). If given, it is attached as ordinary
      pre_trade_context/review evidence on the draft -- it is NEVER passed
      to identity-link or verify-entry, so it can never become execution
      authority no matter what ``type`` it carries.
    """

    if not isinstance(provider_evidence, Mapping) or not provider_evidence.get("source_ref"):
        raise LifecycleError(
            "Unified Capture LIVE requires provider_evidence (real provider/broker-sourced "
            "execution evidence) -- a chart_evidence screenshot alone can never satisfy "
            "identity-link/verify-entry; see LifecycleStore._is_provider_evidence / "
            "PROVIDER_EXECUTION_EVIDENCE_TYPES"
        )
    registration_packet_sha256 = None
    if frozen_registration_packet is not None:
        if strategy_observations is not None:
            raise LifecycleError(
                "frozen registration capture must not also supply strategy_observations"
            )
        from .registration_packet import (
            require_persisted_frozen_packet,
            validate_existing_registration_binding,
            validate_frozen_packet,
        )

        validation = validate_frozen_packet(
            store.root,
            frozen_registration_packet,
            evidence_manifest=registration_evidence_manifest,
            evidence_root=registration_evidence_root,
        )
        facts = frozen_registration_packet["provider_facts"]
        registration_packet_sha256 = str(validation["packet_sha256"]).lower()
        require_persisted_frozen_packet(store.root, frozen_registration_packet)
        if canonical_provider(provider) != canonical_provider(str(facts["provider"])):
            raise LifecycleError("frozen packet provider does not match capture provider")
        if str(provider_lifecycle_id) != str(facts["provider_lifecycle_id"]):
            raise LifecycleError("frozen packet provider lifecycle does not match capture lifecycle")
        if _normalized_instrument(symbol) != _normalized_instrument(facts["instrument"]):
            raise LifecycleError("frozen packet instrument does not match capture symbol")
        if str(direction).lower() != str(facts["direction"]).lower():
            raise LifecycleError("frozen packet direction does not match capture direction")
        if str(provider_execution_timestamp) != str(facts["entry_time"]):
            raise LifecycleError("frozen packet entry time does not match capture provider timestamp")
        try:
            if float(actual_entry) != float(facts["entry_price"]):
                raise LifecycleError("frozen packet entry price does not match capture actual_entry")
        except (TypeError, ValueError) as exc:
            raise LifecycleError("frozen packet entry price is not comparable to actual_entry") from exc

        result = dict(frozen_registration_packet["strategy_resolution"])
        if result.get("status") not in {"MATCHED", "NO_MATCH", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE"}:
            raise LifecycleError("frozen registration capture has an unsupported strategy_resolution")
        resolution = to_lifecycle_resolution(
            result,
            contemporaneous=recording_timing == "contemporaneous",
        )
    else:
        result, resolution = resolve_live_strategy(
            store,
            strategy_observations if strategy_observations is not None else {},
            intended_entry=intended_entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            planned_rr=planned_rr,
            contemporaneous=recording_timing == "contemporaneous",
            provider_entry_at=provider_execution_timestamp,
        )

    # Idempotent re-run: validate the frozen packet first, then prove an existing
    # binding belongs to that same packet before returning it.
    existing = registration_state(store.root, provider, provider_lifecycle_id)
    if existing["status"] in {"REGISTERED", "IN_PROGRESS"}:
        if registration_packet_sha256 is not None:
            validate_existing_registration_binding(
                store,
                frozen_registration_packet,
                existing,
            )
        if existing["status"] == "REGISTERED":
            return {
                "registered": True,
                "already_registered": True,
                "registration_status": "REGISTRATION_COMPLETE",
                "draft_id": existing["draft_id"],
                "trade_id": existing.get("trade_id"),
                "trade_no": existing.get("trade_no"),
                **({"packet_sha256": registration_packet_sha256} if registration_packet_sha256 else {}),
            }
        return {
            "registered": False,
            "registration_status": "REGISTRATION_IN_PROGRESS",
            "draft_id": existing["draft_id"],
            "detail": existing["detail"],
            **({"packet_sha256": registration_packet_sha256} if registration_packet_sha256 else {}),
        }
    if existing["status"] == "AMBIGUOUS":
        raise LifecycleError(f"provider lifecycle binding is ambiguous: {existing['detail']}")
    strategy_id = result.get("strategy_id")
    rule_version = result.get("rule_version")
    draft = store.create_draft(
        strategy_id=strategy_id, rule_version=rule_version, symbol=symbol, direction=direction,
        intended_entry=intended_entry, stop_loss=stop_loss, take_profit=take_profit, planned_rr=planned_rr,
        timeframe_context=timeframe_context, timeframe_trigger=timeframe_trigger, market_context=market_context,
        setup_tags=setup_tags, confirmation_tags=confirmation_tags, thesis=thesis, chart_refs=chart_refs,
        created_at=created_at, recording_timing=recording_timing, trade_type="live",
        execution_environment=execution_environment, draft_id=draft_id, variant=variant,
        recorded_at=recorded_at, outcome_known_at_recording=outcome_known_at_recording,
        strategy_resolution=resolution,
        registration_packet_sha256=registration_packet_sha256,
    )
    draft_id_resolved = draft["draft_id"]
    if chart_evidence is not None:
        store.attach_evidence(draft_id_resolved, dict(chart_evidence), recorded_at=recorded_at)
    store.attach_evidence(draft_id_resolved, dict(provider_evidence), recorded_at=recorded_at)
    for evidence in additional_evidence or []:
        if not isinstance(evidence, Mapping):
            raise LifecycleError("additional_evidence items must be objects")
        if evidence.get("category") == "entry":
            raise LifecycleError("additional_evidence cannot replace provider entry evidence")
        store.attach_evidence(draft_id_resolved, dict(evidence), recorded_at=recorded_at)
    store.mark_entry_pending(draft_id_resolved, provider={"provider": provider}, recorded_at=recorded_at)
    store.create_identity_link(
        draft_id_resolved,
        provider=provider,
        provider_lifecycle_id=provider_lifecycle_id,
        instrument=symbol,
        direction=direction,
        provider_execution_timestamp=provider_execution_timestamp,
        candidate_source=candidate_source,
        match_disposition="MATCHED",
        evidence_refs=[provider_evidence["source_ref"]],
        recorded_at=recorded_at,
        authority=identity_authority,
        registration_packet_sha256=registration_packet_sha256,
    )
    store.verify_entry(
        draft_id_resolved,
        instrument=symbol,
        direction=direction,
        actual_entry=actual_entry,
        execution_at=provider_execution_timestamp,
        source_ref=provider_evidence["source_ref"],
        provider_lifecycle_id=provider_lifecycle_id,
        provider=provider,
        recorded_at=recorded_at,
    )
    finalized = store.finalize(draft_id_resolved, reviewer, recorded_at=recorded_at)
    return {
        "registered": True,
        "registration_status": "REGISTRATION_COMPLETE",
        "review_status": "not_started",
        "strategy_result": result,
        "draft_id": draft_id_resolved,
        **({"packet_sha256": registration_packet_sha256} if registration_packet_sha256 else {}),
        **finalized,
    }


def capture_retrospective_live(store: LifecycleStore, **kwargs: Any) -> dict[str, Any]:
    """Late-reported real execution: same primitives, ``recording_timing``
    tells the difference. See :func:`capture_live` for the full parameter
    list (including the ``provider_evidence`` / ``chart_evidence`` split);
    pass ``recording_timing="retrospective"`` explicitly (or rely on the
    caller already having done so) and a historical ``created_at``.
    """

    kwargs.setdefault("recording_timing", "retrospective")
    return capture_live(store, **kwargs)
