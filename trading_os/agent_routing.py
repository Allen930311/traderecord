from __future__ import annotations

import re


def _normalize(text: str) -> str:
    return "".join(text.lower().split())


# Fail closed on explicit negative commands. Merely describing an opportunity
# as "沒有下單" is NOT a prohibition on recording; "不要記錄" is.
# This classifier only chooses a workflow; it does not grant write authority.
_NEGATED_ACTION_PREFIX = (
    r"(?:不要|不用|不必|不需要|無須|請勿|禁止|先別|(?<!分)別|暫不|"
    r"donot|don't|dont|never)"
    # Only known polite/modifier tokens can separate a negation and an action.
    # A free-form .{0,N} span can cross clauses and suppress a later,
    # explicitly requested operation, e.g. "不要同步，幫我登記".
    r"(?:(?:再|先|幫我|替我|你|直接|自動|立即|進行|做|隨便)){0,3}"
)
_NEGATED_WRITE_RE = re.compile(
    _NEGATED_ACTION_PREFIX
    + r"(?:記錄|紀錄|登記|保存|儲存|存下|寫入|新增觀察|"
      r"record|register|log|save)"
)
_NEGATED_SYNC_RE = re.compile(
    _NEGATED_ACTION_PREFIX + r"(?:刷新|同步|更新|sync|refresh)"
)
_NEGATED_REVIEW_RE = re.compile(
    _NEGATED_ACTION_PREFIX + r"(?:復盤|复盘|review)"
)
_CLAUSE_SEPARATOR_RE = re.compile(r"[，,。；;\n]+")
_WRITE_ROUTES = frozenset(("trade_registration", "setup_observation"))


# An explicit trading/backtest registration is not an unexecuted observation,
# even though a manual replay necessarily had no live exchange order.
_BACKTEST_TRADE_MARKERS = (
    "回測交易", "回測單", "回測這筆交易", "回放交易",
    "回放的交易", "backtesttrade", "backtesttrades",
    "replaytrade",
)
_EXPLICIT_REGISTRATION_VERBS = (
    "登記", "補登", "補記", "記錄", "紀錄", "保存",
    "register", "record", "log",
)


_STRATEGY_HEALTH_MARKERS = (
    "體檢",
    "策略健康",
    "策略診斷",
    "策略诊断",
    "哪裡要調整",
    "哪里要调整",
    "策略要調整",
    "策略要调整",
    "調整策略",
    "调整策略",
    "統整策略",
    "統整方法",
    "策略health",
    "strategyhealth",
    "diagnosethestrategy",
    "researchhypothesis",
)


def _resolve_affirmative_clause(owner_text: str) -> str | None:
    """Resolve one unnegated clause using existing atomic SOP priorities."""

    text = _normalize(owner_text)
    deny_write = bool(_NEGATED_WRITE_RE.search(text))
    deny_sync = bool(_NEGATED_SYNC_RE.search(text))

    # An explicit BACKTEST registration wins over the descriptive phrase
    # "沒有實際下單". This MUST precede the observation writer route.
    if (
        not deny_write
        and any(marker in text for marker in _BACKTEST_TRADE_MARKERS)
        and any(verb in text for verb in _EXPLICIT_REGISTRATION_VERBS)
    ):
        return "trade_registration"

    # The Analyze/research surface includes a separately callable observation
    # writer for opportunities that were NOT executed. Its obs_* identity is
    # not a trade registration, so do not route an explicit missed-setup
    # recording request into the LIVE/BACKTEST registration workflow.
    observation_intents = (
        "沒下單", "沒有下單", "未下單", "沒進場", "未進場",
        "沒進的", "未交易", "錯過的setup", "missedsetup",
        "skippedsetup", "untradedsetup", "沒下",
    )
    observation_capture = (
        "記一下", "幫我記", "記錄", "紀錄", "保存", "存下",
        "留存", "記下", "新增觀察", "record", "logsetup",
    )
    owner_example = "這裡有個很好的setup但我沒下"
    if not deny_write and any(marker in text for marker in observation_intents) and (
        any(marker in text for marker in observation_capture)
        or owner_example in text
    ):
        return "setup_observation"

    # Registration owns creation/backfill of canonical truth. It may internally call
    # trade_lookup and record_reconciliation, but those implementation details should not
    # leak into owner-facing routing.
    registration_markers = (
        "登記交易",
        "我要登記",
        "登記一筆",
        "完成登記",
        "登記這筆",
        "幫我登記",
        "幫我記錄",
        "幫我紀錄",
        "我要記錄",
        "我要紀錄",
        "補登",
        "補記",
        "完成記錄",
        "完成紀錄",
        "記錄這筆",
        "紀錄這筆",
        "這筆有下單",
        # "Register the next one" is the everyday phrase for the next pending
        # provider candidate; it must never fall through to no route.
        "登記下一",
        "登記待登記",
        "登記候選",
        "登記pending",
        "registernext",
        "registerthenext",
        "registerthistrade",
        "recordatrade",
        "recordthistrade",
        "logatrade",
        "logthistrade",
        "backfillthistrade",
    )
    # "先復盤…" asks for the review first; a later "補登" is a future wish, not a
    # registration request, so it must not start the Register writer.
    if "先復盤" in text or "先复盘" in text:
        return "single_trade_review"
    if not deny_write and any(marker in text for marker in registration_markers):
        return "trade_registration"

    # A provider/account refresh is its own atomic Owner operation. Registration
    # always wins when the request is to register and look up provider facts.
    # Plain queries such as "查 PropW 進出場" must remain read-only lookups.
    sync_markers = (
        "刷新帳本",
        "刷新交易所",
        "同步帳本",
        "同步交易所",
        "更新帳本",
        "更新交易所帳本",
        "刷新propw",
        "同步propw",
        "更新propw帳本",
        "propw同步",
        "propw刷新",
        "syncpropw",
        "refreshpropw",
        "syncexchange",
        "refreshledger",
        "syncproviderledger",
    )
    if not deny_sync and any(marker in text for marker in sync_markers):
        return "provider_sync"

    # Strategy diagnosis is a separate cross-trade SOP. It consumes broader
    # evidence populations but only diagnoses using reviewed/observable samples.
    if any(marker in text for marker in _STRATEGY_HEALTH_MARKERS):
        return "strategy_health_check"

    # Review and descriptive performance analysis use independent atomic SOPs.
    # Neither discovers nor registers missing provider trades.
    review_markers = (
        "復盤",
        "勝率",
        "胜率",
        "績效",
        "绩效",
        "winrate",
        "performance",
        "績效回顧",
        "包含回測",
        "回測一起看",
        "live+backtest",
        "live/backtest/all",
        "全部模式",
        "分析交易",
        "交易分析",
        "統計策略",
        "哪個setup",
        "setup最好",
        "reviewmytrades",
        "analyzemyregisteredtrades",
    )
    if any(marker in text for marker in review_markers):
        return resolve_trade_review_mode(owner_text)

    # Provider fact lookup remains a narrow read/discovery route.
    if any(marker in text for marker in ("進出場", "openid", "propw")):
        return "trade_lookup"
    if "交易" in text and any(marker in text for marker in ("找", "查", "還有")):
        return "trade_lookup"
    if any(
        marker in text
        for marker in (
            "findatrade",
            "findtrade",
            "lookupatrade",
            "lookuptrade",
        )
    ):
        return "trade_lookup"

    return None


def _negated_targets(text: str) -> set[str]:
    """Bind a negative command to its trade, observation, sync or review target."""
    denied: set[str] = set()
    if _NEGATED_WRITE_RE.search(text):
        trade_object = (
            any(marker in text for marker in _BACKTEST_TRADE_MARKERS)
            or any(marker in text for marker in ("交易", "trade", "補登"))
        )
        observation_object = any(
            marker in text for marker in (
                "setup", "vt", "沒下單", "沒有下單", "未下單",
                "沒進", "未進", "錯過", "missed", "skipped", "untraded",
            )
        )
        if trade_object and not observation_object:
            denied.add("trade_registration")
        elif observation_object and not trade_object:
            denied.add("setup_observation")
        else:
            # Unclear object: don't route a later same-class write.
            denied.update(_WRITE_ROUTES)
    if _NEGATED_SYNC_RE.search(text):
        denied.add("provider_sync")
    if _NEGATED_REVIEW_RE.search(text):
        denied.add("single_trade_review")
    return denied


def resolve_daily_workflow(owner_text: str) -> str | None:
    """Resolve independent Owner commands, while failing closed on conflicts.

    Explicit punctuation separates clauses; a clause carrying a negated action
    must not itself invoke a writer. Opposing commands for the same operation
    are ambiguous. This is routing only, not write authorization.
    """
    clauses = [
        _normalize(chunk) for chunk in _CLAUSE_SEPARATOR_RE.split(owner_text)
        if chunk.strip()
    ]
    denied: set[str] = set()
    requested: set[str] = set()
    for clause in clauses:
        negated = _negated_targets(clause)
        if negated:
            denied.update(negated)
            continue
        route = _resolve_affirmative_clause(clause)
        if route is not None:
            requested.add(route)

    # Review-first wording ("先復盤…，之後再補登") starts with the review; the
    # later registration is only a future wish and must not start the writer.
    full = _normalize(owner_text)
    if ("先復盤" in full or "先复盘" in full) and "single_trade_review" in requested:
        requested.discard("trade_registration")

    if requested & denied:
        return None
    if len(requested & _WRITE_ROUTES) > 1:
        return None  # Two different write requests: ask the Owner to select one.

    # Preserve existing registration-before-sync routing for composed requests.
    for route in (
        "trade_registration", "setup_observation", "provider_sync",
        "strategy_health_check", "single_trade_review",
        "performance_analysis", "trade_lookup",
    ):
        if route in requested:
            return route
    return None


def resolve_trade_review_mode(owner_text: str) -> str:
    """Select the owner-facing atomic SOP behind the compatibility router.

    A canonical T-number or explicit one-trade intent selects single-trade mode.
    The workflow must resolve or clarify a requested trade before invoking the
    canonical writer; it must not widen a one-trade request to portfolio analysis.
    """

    text = _normalize(owner_text)
    if any(marker in text for marker in _STRATEGY_HEALTH_MARKERS):
        return "strategy_health_check"
    if re.search(r"(?<![a-z0-9])t\d{6,}(?!\d)", owner_text, flags=re.IGNORECASE):
        return "single_trade_review"
    # Reviewing chart image(s) is a one-trade review, never a registration and
    # never a portfolio analysis; the workflow resolves or asks for the T-ID.
    single_trade_markers = (
        "這筆", "那筆", "單筆", "這張圖", "這幾張圖", "這些圖", "人工復盤",
        "thistrade", "specifictrade", "reviewatrade",
    )
    if any(marker in text for marker in single_trade_markers):
        return "single_trade_review"
    return "performance_analysis"


def resolve_performance_population(owner_text: str) -> str | None:
    """Return the population contract for a performance request, if applicable."""

    text = _normalize(owner_text)
    combined_markers = (
        "包含回測一起看",
        "回測一起看",
        "live+backtest",
        "live/backtest/all",
        "all模式",
        "全部模式",
        "combinedperformance",
        "allperformance",
        "includebacktest",
        "includingbacktest",
        "togetherwithbacktest",
        "backtesttoo",
        "合併",
        "合并",
    )
    if any(marker in text for marker in combined_markers) or re.search(r"(?<![a-z])all(?![a-z])", owner_text, flags=re.IGNORECASE):
        return "all_combined"
    if any(marker in text for marker in ("paper", "模擬盤", "模擬交易")):
        return "paper"
    if any(marker in text for marker in ("backtest", "回測", "回测")):
        return "backtest"

    performance_markers = ("勝率", "胜率", "績效", "绩效", "winrate", "performance", "expectancy", "profitfactor")
    if not any(marker in text for marker in performance_markers):
        return None
    strategy_markers = (
        "策略",
        "vegas",
        "維加斯",
        "ict",
        "po3",
        "三針",
        "triple-tap",
    )
    if any(marker in text for marker in performance_markers) and any(marker in text for marker in strategy_markers):
        return "live_named_strategy"
    return "live_overall"
