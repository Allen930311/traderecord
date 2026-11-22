from trading_os.auditor import audit_trade


def bound_trade(trade, strategy_id="stg-example-b", version="stg-example-b:v1", variant="test-variant"):
    return {**trade, "strategy": {"strategy_id": strategy_id, "rule_version": version, "variant": variant}}

def test_auditor_clean_loss_does_not_use_outcome():
    trade=bound_trade({"trade_id":"trd-1","plan":{"timeframe_trigger":"15m"},"result":{"outcome":"loss"},"evidence":[{"source_ref":"note","method":"manual_note","timing":"pre_trade","locator":"body:1","sha256":"abc","field":"plan.timeframe_trigger","assertion":"equals","assertion_value":"15m"}]})
    strategy={"strategy_id":"stg-example-b"}; version={"strategy_id":"stg-example-b","version_id":"stg-example-b:v1","rules":[{"rule_id":"tf","required":True,"operator":"equals","operands":{"field":"plan.timeframe_trigger","value":"15m"},"evidence_requirements":["manual_note"]}]}
    result=audit_trade(trade,strategy,version)
    assert result["rule_matrix"][0]["status"] == "PASS" and result["classification"] == "clean_strategy"
    assert result["outcome_used"] is False


def test_auditor_requires_exact_evidence_field_and_assertion():
    version={"strategy_id":"stg-example-b","version_id":"stg-example-b:v1","rules":[{"rule_id":"tf","required":True,"operator":"timeframe_gte","operands":{"field":"plan.timeframe_trigger","value":"15m"},"evidence_requirements":["manual_note"]}]}
    generic={"source_ref":"note","method":"manual_note","timing":"pre_trade","locator":"body:1","sha256":"abc"}
    trade=bound_trade({"trade_id":"trd-1","plan":{"timeframe_trigger":"15m"},"evidence":[generic]})
    result=audit_trade(trade,{"strategy_id":"stg-example-b"},version)
    assert result["rule_matrix"][0]["status"] == "UNKNOWN"

    wrong_field={**generic,"field":"result.outcome","assertion":"timeframe_gte","assertion_value":"15m"}
    result=audit_trade({**trade,"evidence":[wrong_field]}, {"strategy_id":"stg-example-b"}, version)
    assert result["rule_matrix"][0]["status"] == "UNKNOWN"


def test_auditor_profitable_mistake_can_fail_and_conflict_is_unknown():
    version={"strategy_id":"stg-example-b","version_id":"stg-example-b:v1","rules":[{"rule_id":"tf","required":True,"category":"trigger","operator":"timeframe_gte","operands":{"field":"plan.timeframe_trigger","value":"15m"},"evidence_requirements":["manual_note"]}]}
    base={"source_ref":"note","method":"manual_note","timing":"pre_trade","locator":"body:1","sha256":"abc","field":"plan.timeframe_trigger","assertion":"timeframe_gte"}
    profitable_mistake=bound_trade({"trade_id":"trd-1","plan":{"timeframe_trigger":"5m"},"result":{"outcome":"win"},"evidence":[{**base,"assertion_value":"5m"}]})
    result=audit_trade(profitable_mistake,{"strategy_id":"stg-example-b"},version)
    assert result["rule_matrix"][0]["status"] == "FAIL"
    assert result["classification"] == "unknown"
    assert result["outcome_used"] is False

    conflict=bound_trade({"trade_id":"trd-2","plan":{"timeframe_trigger":"15m"},"evidence":[{**base,"source_ref":"note-a","assertion_value":"15m"},{**base,"source_ref":"note-b","assertion_value":"5m"}]})
    result=audit_trade(conflict,{"strategy_id":"stg-example-b"},version)
    assert result["rule_matrix"][0]["status"] == "UNKNOWN"
    assert result["rule_matrix"][0]["evidence_refs"] == ["note-a","note-b"]

def test_auditor_missing_evidence_is_unknown():
    trade=bound_trade({"trade_id":"trd-2","plan":{},"result":{"outcome":"win"},"evidence":[]}, version="stg-example-b:v1")
    version={"strategy_id":"stg-example-b","version_id":"stg-example-b:v1","rules":[{"rule_id":"tf","required":True,"operator":"equals","operands":{"field":"plan.timeframe_trigger","value":"15m"},"evidence_requirements":["chart_snapshot"]}]}
    assert audit_trade(trade,{"strategy_id":"stg-example-b"},version)["rule_matrix"][0]["status"] == "UNKNOWN"

def test_auditor_reports_mismatch_and_na():
    version={"version_id":"stg-example-b:v1","strategy_id":"stg-example-b","rules":[{"rule_id":"x","required":True,"operator":"equals","operands":{"field":"x","value":1},"applicable":False}]}
    result=audit_trade(bound_trade({"trade_id":"t","x":2}, strategy_id="stg-other"),{"strategy_id":"stg-other"},version)
    assert "trade_strategy_binding_mismatch" in result["explanation_codes"]
    result=audit_trade(bound_trade({"trade_id":"t","x":2}),{"strategy_id":"stg-example-b"},version)
    assert result["rule_matrix"][0]["status"] == "N/A"


def test_auditor_rejects_unapproved_operator_and_preserves_trade_variant():
    trade = bound_trade({"trade_id": "t", "x": 1}, variant="vt-long")
    version = {"strategy_id":"stg-example-b", "version_id":"stg-example-b:v1", "rules":[{"rule_id":"x","required":True,"operator":"all_exist","operands":{"fields":["x"]}}]}
    result = audit_trade(trade, {"strategy_id":"stg-example-b"}, version)
    assert result["matched_variant"] == "vt-long"
    assert result["rule_matrix"][0]["explanation_code"] == "operator_not_approved_v1"
