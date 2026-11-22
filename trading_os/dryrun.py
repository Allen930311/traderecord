from __future__ import annotations
import json
from pathlib import Path
from .parser import parse_file

def run_dry_run(root: Path, output: Path) -> dict:
    root = root.resolve(); records=[]; queues={"duplicate_identity":[],"identity_date":[],"type_unresolved":[],"strategy_unresolved":[],"result_r":[],"evidence_lifecycle":[]}; dispositions={}
    adjudicated = {}
    for ap in sorted((root / "canonical" / "adjudications").glob("*.json")):
        try:
            decision = json.loads(ap.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for ref in decision.get("subject_refs", []):
            adjudicated[ref] = decision
    from .legacy_source import list_legacy_files
    legacy_md = [root / rel for rel in list_legacy_files(root, "trades") if rel.lower().endswith(".md")]
    for path in sorted(legacy_md, key=lambda p:p.relative_to(root).as_posix().lower()):
        record=parse_file(root,path); records.append(record)
        rid=record["record_id"]; warnings=record["raw_legacy"]["parse_warnings"]
        decision = adjudicated.get(record["legacy_source"]["path"])
        is_duplicate_source = bool(decision and record["legacy_source"]["path"] in {p for t in (decision.get("after", {}).get("trades", []) if isinstance(decision.get("after", {}).get("trades"), list) else []) for p in t.get("duplicate_sources", [])})
        is_canonical = bool(decision and any(t.get("canonical_source") == record["legacy_source"]["path"] for t in (decision.get("after", {}).get("trades", []) if isinstance(decision.get("after", {}).get("trades"), list) else [])))
        if not decision and ("duplicate_candidate" in warnings or record["legacy_source"]["filename"].lower().endswith("_1.md")): queues["duplicate_identity"].append(rid)
        if "date_conflict" in warnings and not (decision and decision.get("decision_type") == "date"): queues["identity_date"].append(rid)
        if record["type"] is None and not decision: queues["type_unresolved"].append(rid)
        raw_strategy = (record["strategy"].get("legacy_strategy_raw") or "").lower().split("#", 1)[0].strip()
        accepted_component = raw_strategy in {"sr", "sr-flip", "support-resistance", "support_resistance"}
        if record["strategy"]["strategy_id"] == "unknown" and not decision and not accepted_component: queues["strategy_unresolved"].append(rid)
        if any(x in warnings for x in ("result_conflict", "invalid_r")): queues["result_r"].append(rid)
        if any(x in warnings for x in ("actual_execution_unverified", "evidence_unresolved", "partial_exit_unmodeled")): queues["evidence_lifecycle"].append(rid)
        # Exactly one primary disposition per source; secondary queues retain diagnostics.
        dispositions[rid] = "duplicate_source" if is_duplicate_source else "adjudicated_canonical" if is_canonical else "component_classified" if accepted_component else next((name for name in ("identity_date","duplicate_identity","type_unresolved","strategy_unresolved","result_r","evidence_lifecycle") if rid in queues[name]), "ready_for_adjudication")
    result={"dry_run_version":"dry-run.v1","source_count":len(records),"canonical_count":0,"trade_numbers_allocated":False,"records":records,"queues":queues,"coverage":{"source_candidates":len(records),"disposition_count":len(dispositions),"exactly_once":len(dispositions)==len(records),"dispositions":dispositions}}
    output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    return result
