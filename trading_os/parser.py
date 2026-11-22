from __future__ import annotations
import hashlib

import re
from pathlib import Path
from typing import Any

from .identifiers import normalized_record_identifier

STRATEGY_ALIASES = {
    "1": "stg-vegas-tunnel", "vegas-tunnel": "stg-vegas-tunnel", "1-vegas-tunnel": "stg-vegas-tunnel",
    "2": "stg-vegas-tunnel", "mss-vegas": "stg-vegas-tunnel", "2-mss-vegas": "stg-vegas-tunnel",
    "3": "stg-ema17-breakout", "4": "stg-liq-mss-ob", "5": "stg-fib-rsi", "6": "stg-fakeout-1272",
    "7": "stg-ma-clusters", "8": "stg-triple-tap-rsi", "9": "stg-order-block", "ob": "stg-order-block",
    "10": "stg-sr-flip", "11": "stg-example-b", "op": "stg-example-b", "opv3": "stg-example-b", "12": "stg-po3", "po3": "stg-po3", "13": "stg-unicorn",
}

def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    match = re.match(r"^---\s*\n(.*?)\n---(?:\n|$)", text, re.S)
    if not match:
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "-")):
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"\'')
    return values

def _number(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match.group()) if match else None

def parse_file(root: Path, path: Path) -> dict[str, Any]:
    from .legacy_source import read_legacy_bytes
    root = root.resolve(); path = path.resolve(); rel = path.relative_to(root).as_posix()
    raw = read_legacy_bytes(root, rel); text = raw.decode("utf-8", errors="replace"); fm = _frontmatter(text)
    raw_strategy = fm.get("strategy") or fm.get("strategy_id") or ""
    strategy_key = raw_strategy.lower().split("#", 1)[0].strip()
    strategy_id = STRATEGY_ALIASES.get(strategy_key)
    if strategy_key in {"mss-vt-short", "mss-vt-long", "vt-short", "vt-long"}:
        strategy_id = None
    raw_type = (fm.get("type") or fm.get("trade_type") or "").lower()
    if any(x in raw_type for x in ("backtest", "回測", "回测", "historical")) or "backtest" in text.lower(): trade_type = "backtest"
    elif any(x in raw_type for x in ("live", "實盤", "实盘")) or "propw" in text.lower(): trade_type = "live"
    else: trade_type = None
    direction_raw = fm.get("side") or fm.get("direction") or ""
    direction = "long" if direction_raw.upper() in {"L", "LONG", "多"} else "short" if direction_raw.upper() in {"S", "SHORT", "空"} else "unknown"
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", fm.get("date", ""))
    filename_date = re.search(r"20\d{2}-\d{2}-\d{2}", path.name)
    event_date = date_match.group(1) if date_match else filename_date.group(0) if filename_date else None
    warnings: list[str] = []
    if date_match and filename_date and date_match.group(1) != filename_date.group(0): warnings.append("date_conflict")
    if not strategy_id: warnings.append("strategy_unresolved")
    if not trade_type: warnings.append("classification_pending")
    if "❌ SL" in text and not re.search(r"actual|實際出場|出場價", text, re.I): warnings.append("actual_execution_unverified")
    # ``src_*`` identifies immutable manifest entries. A parsed source
    # candidate is the explicit next layer and therefore receives ``rec_*``.
    record_id = normalized_record_identifier("tradingdiary", rel, hashlib.sha256(raw).hexdigest())
    date_candidates = []
    if date_match: date_candidates.append({"value": date_match.group(1), "source": "frontmatter.date"})
    if filename_date: date_candidates.append({"value": filename_date.group(0), "source": "filename"})
    if date_match and filename_date and date_match.group(1) != filename_date.group(0): event_date = None
    signal_family = "VT" if "vt" in strategy_key or "vegas" in strategy_key else None
    signal_direction = "long" if "long" in strategy_key else "short" if "short" in strategy_key else None
    signal = {"family": signal_family, "direction": signal_direction, "occurrence": "unknown", "confirmation_components": ["MSS"] if "mss" in strategy_key or "mss" in text.lower() else [], "source_ref": None, "locator": None}
    return {"schema_version":"trade.v1", "record_id":record_id, "record_kind":"trade_candidate", "trade_id":None, "trade_no":None,
      "type":trade_type, "classification_status":"resolved" if trade_type else "pending", "execution_environment":"unknown", "recording_timing":"unknown", "observation_id":None, "identity":{"event_date":event_date,"date_candidates":date_candidates,"direction":direction,"instrument":{"pair_raw":fm.get("pair") or "unknown"}},
      "strategy":{"strategy_id":"unknown","candidate_strategy_ids":[strategy_id] if strategy_id else [],"legacy_strategy_raw":raw_strategy,"alias_status":"unresolved","rule_version":"historical-unknown"}, "signal": signal,
      "plan":{"entry_price":_number(fm.get("entry") or fm.get("entry_price")),"stop_loss_price":_number(fm.get("sl") or fm.get("sl_price")),"rr_pretrade":_number(fm.get("rr") or fm.get("r_ratio"))},
      "actual":{"execution_status":"unknown","position_status":"unknown"}, "result":{"outcome":"unknown"}, "evidence":[], "review":{}, "dedupe":{"status":"source_candidate","fingerprint":hashlib.sha256(raw).hexdigest()}, "analytics":{"eligibility":"pending"},
      "raw_legacy":{"fields":fm,"body_text_ref":rel,"parse_warnings":warnings}, "legacy_source":{"repository":"tradingdiary","path":rel,"filename":path.name,"content_sha256":hashlib.sha256(raw).hexdigest()}, "provenance":{"parser":"legacy.v1","field_provenance":{"identity.event_date":{"source_path":rel,"locator":"frontmatter.date|filename","raw_value":date_candidates,"transform":"conflict-preserving","confidence":"low"},"strategy.strategy_id":{"source_path":rel,"locator":"frontmatter.strategy","raw_value":raw_strategy,"transform":"alias-to-candidate-only","confidence":"low"}}}}
