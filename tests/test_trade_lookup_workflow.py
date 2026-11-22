from pathlib import Path

import yaml

from trading_os.agent_routing import resolve_daily_workflow


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows" / "trade-lookup.yaml"
STATUS = ROOT / "STATUS.md"


def _load():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _load_runtime_writes():
    text = STATUS.read_text(encoding="utf-8")
    block = text.split("```yaml", 1)[1].split("```", 1)[0]
    return yaml.safe_load(block)["runtime_writes"]


def test_daily_router_points_provider_execution_facts_to_trade_lookup_first():
    text = (ROOT / "AGENT_CONTEXT.md").read_text(encoding="utf-8")
    assert "## Daily task router" in text
    assert "workflows/trade-lookup.yaml" in text
    router = text.split("## Daily task router", 1)[1].split("## Where to look", 1)[0]
    assert "Provider execution facts" in router
    assert "Browser UI" in router
    assert "HAR files" in router


def test_skill_routes_provider_lookup_without_claiming_project_read_only():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "找交易／查進出場／今天還有哪筆" in text
    assert "workflows/trade-lookup.yaml" in text
    assert "provider-side read-only" in text


def test_trade_lookup_is_project_runtime_write_but_provider_side_read_only():
    data = _load()
    assert data["workflow_id"] == "trade_lookup"
    assert data["authority"] == "trading-record"
    assert data["effect"] == "PROJECT_RUNTIME_WRITE"
    assert data["external_provider_access"]["effect"] == "READ_ONLY"
    assert data["external_provider_access"]["methods"] == ["GET"]
    assert "ingestion/propw/data/**" in _load_runtime_writes()

    ids = [step["id"] for step in data["workflow"]]
    assert ids == [
        "resolve_query",
        "ensure_freshness",
        "query_trade_groups",
        "compare_canonical",
        "return_results",
    ]
    ensure = data["workflow"][1]
    assert "python -m propw ensure-fresh" in ensure["command_template"]
    assert data["guards"]["browser_or_har_fallback"] == "forbidden"


def test_golden_owner_utterance_is_fed_into_executable_resolver():
    golden = _load()["acceptance"]["golden_utterance"]["input"]
    assert resolve_daily_workflow(golden) == "trade_lookup"


def test_record_request_routes_to_owner_facing_trade_registration():
    assert resolve_daily_workflow("幫我記錄一筆交易") == "trade_registration"



def test_mixed_record_and_provider_lookup_intent_prefers_trade_registration():
    assert (
        resolve_daily_workflow(
            "幫我記錄 2026-09-22 那筆交易，進出場去 PropW 查"
        )
        == "trade_registration"
    )


def test_english_lookup_intents_route_to_trade_lookup():
    assert resolve_daily_workflow("find a trade") == "trade_lookup"
    assert resolve_daily_workflow("lookup trade") == "trade_lookup"

def test_trade_lookup_contract_points_to_behavioral_freshness_tests():
    tests = set(_load()["acceptance"]["behavioral_tests"])
    assert (
        "ingestion/propw/tests/test_freshness.py::"
        "test_stale_current_day_syncs_exactly_once_then_query_uses_refreshed_db"
    ) in tests
    assert (
        "ingestion/propw/tests/test_freshness.py::"
        "test_fresh_historical_range_skips_sync_and_queries_local_db"
    ) in tests
