TRADE_TYPES = {"live", "backtest"}
EXECUTION_MODES = {"LIVE", "PAPER", "BACKTEST"}
RECORD_KINDS = {"trade_candidate", "trade", "observation"}
CLASSIFICATION_STATUSES = {"pending", "resolved", "rejected"}
EXECUTION_STATUSES = {"planned", "entered", "exited", "not_executed", "unknown"}
POSITION_STATUSES = {"planned", "open", "partially_closed", "closed", "unknown"}
OUTCOMES = {"win", "loss", "breakeven", "mixed", "unknown"}
AUDIT_STATUSES = {"PASS", "FAIL", "UNKNOWN", "N/A"}
RULE_CATEGORIES = {"context", "direction", "setup", "trigger", "confirmation", "risk", "management", "exit", "behaviour"}
EVIDENCE_TIMINGS = {"pre_trade", "execution", "post_trade", "unknown"}
EVIDENCE_METHODS = {"broker_fill", "chart_snapshot", "replay_chart", "manual_note", "sheet_export", "paper_execution"}
PARSER_STATUSES = {"parsed", "parsed_with_warnings", "unsupported", "failed"}
DECISION_TYPES = {"identity", "duplicate", "type", "date", "strategy", "evidence", "rejection", "trade_number_void"}
DECISIONS = {"accept", "reject", "confirm", "unresolved", "duplicate", "canonical", "void"}
# v1 supports only operators whose evaluator and evidence semantics are both
# specified and tested. Cross-field and assertion-only operators are deferred.
STRATEGY_OPERATORS = {"equals", "not_equals", "in", "not_in", "gte", "lte", "between", "timeframe_gte", "exists", "tag_present", "tag_absent"}
STRATEGY_CLASSIFICATIONS = {"independent_system", "variant", "implementation", "component", "experimental", "unresolved"}
# Closed vocabulary for *why* a backtest-type record exists (type=backtest
# only). Owner-facing reasons for scoring a sample without real execution.
SOURCE_METHODS = {
    "live_market_unexecuted",
    "tradingview_replay",
    "historical_chart_review",
    "manual_backtest",
    "paper_trade",
    "external_simulator",
}
# Hindsight-bias tracking: did the owner already know the outcome before
# recording the setup. "unknown" is the safe default when unrecorded.
OUTCOME_KNOWN_AT_RECORDING_VALUES = {True, False, "unknown"}

# Lifecycle status of a strategy in the current trading system.
#   proposed     — catalogued, not yet in use / under construction
#   active       — the human runs this strategy now
#   retired      — the human used it and has stopped; records are kept and
#                  excluded from method-validation samples, reactivatable later
#   experimental — one-off / indicator test, never a standing strategy
STRATEGY_STATUSES = {"proposed", "active", "retired", "experimental"}

# The one placeholder rule_version create_draft() accepts in place of a real
# registered/frozen registry.versions entry, and only for an experimental
# strategy with allow_experimental=true. It is a low-level LifecycleStore
# escape hatch for a genuine one-off indicator test; it is NOT a valid
# authority for a Unified Capture backtest sample (see unified_capture.py and
# operational_analytics._canonical_observations, which both fail closed on
# this exact string rather than treat it as a registered rule_version).
UNFROZEN_EXPERIMENTAL_PLACEHOLDER = "unfrozen:experimental"

# Existing parser vocabulary for a historical real execution whose original
# strategy identity was not recovered. This is a fail-closed identity
# placeholder, not a registry strategy or a performance-counting authority.
HISTORICAL_UNRESOLVED_STRATEGY_ID = "unknown"
HISTORICAL_UNRESOLVED_RULE_VERSION = "historical-unknown"

# Strategy identity provenance is separate from the compatibility strategy
# placeholders above.  The latter keep historical LIVE records structurally
# valid; this vocabulary records whether a real strategy identity was actually
# established for the observation.
STRATEGY_RESOLUTION_STATUSES = {
    "contemporaneous_confirmed",
    "replay_reconstructed",
    "unresolved",
    # Owner/resolver reviewed every active strategy and none matches: a final,
    # countable-as-off-strategy answer. Distinct from "unresolved" (not decided).
    "no_match",
}

# Pre-trade plan fields on a trade_candidate/trade's operational_capture.planned
# (and mirrored plan.*) may take one of these two sentinel strings instead of
# a real value, but ONLY when recording_timing=="retrospective" -- a real,
# already-executed trade the owner genuinely cannot recall a pre-trade plan
# detail for. Reuses the same not_observable/unknown vocabulary already used
# by review.rule_compliance, rather than inventing a new one. Never valid for
# contemporaneous (or recording_timing="unknown") capture, and never silently
# derived from actual/exit facts -- see create_draft in operational_lifecycle.py.
PLAN_FIELD_UNKNOWN_VALUES = {"unknown", "not_observable"}

# A retrospective plan is allowed to preserve only what was known at entry.
# These are explicit review-only markers, not an NLP hindsight detector: they
# cover the vocabulary used by the governed review fields and fail closed when
# a caller accidentally puts those markers back into planned/plan fields.
RETROSPECTIVE_REVIEW_ONLY_TAGS = {
    "near_tp_reversal",
    "insufficient_confirmation",
}
RETROSPECTIVE_REVIEW_ONLY_TEXT_MARKERS = (
    "manual exit",
    "manual_exit",
    "provider stop-loss",
    "provider stop loss",
    "provider stoploss",
    "near-target reversal",
    "near target reversal",
    "near_tp_reversal",
    "bullish break followed",
    "recovered upward before manual exit",
    "later bullish mss",
    "after exit",
    "post-exit",
    "post exit",
)

# execution-verification.v1 entry bases that count as a governed BACKTEST
# entry confirmation for operational_analytics' backtest countability gate.
# "simulator_run" carries full run/dataset metadata; "manual_declared_execution"
# is the minimal single-sample counterpart added for Unified Capture (see
# confirm_backtest_entry) when there is no formal run/session to cite.
GOVERNED_BACKTEST_ENTRY_BASES = {"simulator_run", "manual_declared_execution"}

# Of the existing EVIDENCE_METHODS/"type" values, the subset that can
# actually support a BACKTEST sample's execution or realized-R claim under
# operational_analytics' strict-evidence classification (correction pass #2,
# Blocker 2). A bare "manual_note" (or "broker_fill", which is a LIVE
# provider-execution concept with no meaning for a sample that structurally
# has no broker) never satisfies this on its own -- it can still be attached
# and the sample still gets recorded (see manual_declared_execution's
# "recordable, not blocked" contract), it just does not count as strong
# durable evidence for strict-countable analytics.
BACKTEST_STRONG_EVIDENCE_TYPES = {"chart_snapshot", "replay_chart", "sheet_export"}
