"""Private/local Trade View V3 (Trading Workspace presentation layer).

The browser is a presentation layer only.  It filters rows and renders
precomputed values; it never calculates performance or health metrics.
V3 is a presentation-only workspace over the existing V2 read model. It adds
calm navigation, Home / Journal / Review / Insights information hierarchy,
responsive rows, and a case-reading Trade Detail view (evidence / planned vs
actual / timeline / review / collapsed advanced details), all still generated
as static local files (index.html + style.css + app.js + trade-view-data.json)
served by the same safe local handler below. It does not change canonical
semantics, lifecycle state, analytics authority, or countability.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from .evidence_store import EvidenceStore, EvidenceStoreError
from .operational_analytics import load_current
from .operational_lifecycle import LIFECYCLE_STATES
from .provider_sync_status import read_provider_sync_recency
from .registration_census import build_census, select_candidates
from .registry import load_registry
from .strategy_health import load_current_health


TRADE_VIEW_SCHEMA = "trade-view.v1"
DEFAULT_OUTPUT = Path("exports/operational/trade-view")

_LIFECYCLE_STATE_SET = frozenset(LIFECYCLE_STATES)
_ACTIVE_LIFECYCLE_STATES = frozenset(
    state
    for state in ("PRE_TRADE_DRAFT", "ENTRY_PENDING_VERIFICATION", "OPEN", "PARTIALLY_CLOSED")
    if state in _LIFECYCLE_STATE_SET
)
_REVIEWABLE_LIFECYCLE_STATES = frozenset(
    state for state in ("CLOSED", "REVIEW_PENDING") if state in _LIFECYCLE_STATE_SET
)


def _lifecycle_state(row: Mapping[str, Any]) -> str | None:
    """Return only a formal lifecycle state; historical labels stay separate."""

    for value in (row.get("lifecycle_state"), row.get("state"), row.get("status")):
        if value in _LIFECYCLE_STATE_SET:
            return str(value)
    return None


def _is_active_lifecycle(row: Mapping[str, Any]) -> bool:
    return _lifecycle_state(row) in _ACTIVE_LIFECYCLE_STATES


def _needs_review(row: Mapping[str, Any]) -> bool:
    """Only closed/review-pending lifecycle records can enter Review."""

    if row.get("status") in {"historical_seed", "linked_to_historical_seed"}:
        return False
    if row.get("exclusion_reason") in {"inactive_strategy", "migrated_canonical_overlap"}:
        return False
    return _lifecycle_state(row) in _REVIEWABLE_LIFECYCLE_STATES and row.get("review_status") != "reviewed"

# Evidence artifact filenames are always exactly "<64 hex chars><ext>"
# (see evidence_store.EvidenceStore -- sha256 hex digest plus a magic-byte
# or extension-derived suffix). Anything not shaped like this is rejected
# before it ever touches the filesystem, including any request containing
# "..", "/", or a character outside this allow-list.
_EVIDENCE_FILENAME_RE = re.compile(r"^[0-9a-f]{64}\.[a-z0-9]{1,10}$")
_EVIDENCE_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def make_trade_view_handler(doc_root: Path, evidence_dir: Path) -> type[SimpleHTTPRequestHandler]:
    """Build a request handler for the local Trade View server.

    Serves the Trade View ``doc_root`` (index.html, trade-view-data.json)
    exactly as :class:`SimpleHTTPRequestHandler` always has, plus one
    additional, narrowly scoped route: ``GET /evidence/<sha256>.<ext>``,
    which serves a file from the content-addressed evidence store
    (``evidence_dir``) -- and only a file that (a) has a filename shaped
    like a store artifact and (b) resolves to a real path still inside
    ``evidence_dir`` after resolution. This is what lets evidence links
    actually open through the local HTTP server, which cannot otherwise
    traverse above its own document root to reach
    ``canonical/evidence/artifacts/``.
    """

    doc_root = Path(doc_root).resolve()
    evidence_dir = Path(evidence_dir).resolve()
    evidence_store = EvidenceStore(evidence_dir.parents[2])

    class TradeViewRequestHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(doc_root), **kwargs)

        def _serve_evidence(self, raw_name: str) -> None:
            name = unquote(raw_name)
            if not _EVIDENCE_FILENAME_RE.fullmatch(name):
                self.send_error(404)
                return
            candidate = (evidence_dir / name).resolve()
            if candidate.parent != evidence_dir:
                self.send_error(404)
                return
            try:
                data = evidence_store.read(f"artifact:{name}")
            except EvidenceStoreError:
                self.send_error(404)
                return
            content_type = _EVIDENCE_CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            path_only = urlsplit(self.path).path
            if path_only.startswith("/evidence/"):
                self._serve_evidence(path_only[len("/evidence/"):])
                return
            super().do_GET()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    return TradeViewRequestHandler


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trade View</title>
<link rel="stylesheet" href="style.css">
</head><body>
<div id="app" role="application" aria-label="Trade View"></div>
<script src="app.js"></script>
</body></html>
"""


# Trade View V2 journal aesthetic: dark, calm, financial. Kept as a plain
# stylesheet (no framework) consistent with the existing local/private
# architecture -- see trade_view.py module docstring.
STYLE_CSS = r"""
:root{color-scheme:dark;--bg:#0a0e17;--panel:#111827;--panel-2:#0d1420;--line:#232d40;--text:#e7edf8;--muted:#8b98b3;--accent:#5fd3c4;--accent-2:#6fa8ff;--win:#4fd07f;--loss:#f26d6d;--be:#c9c26a;--warn:#e2a444;--live:#5fd3c4;--backtest:#8f7ee0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,Segoe UI,system-ui,sans-serif}
a{color:var(--accent-2)}
button{font:inherit}
.app-shell{max-width:1360px;margin:0 auto;padding:20px 20px 60px}
.app-header{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:18px;flex-wrap:wrap}
.app-header h1{font-size:18px;letter-spacing:.08em;margin:0;text-transform:uppercase;color:var(--text)}
.app-header .tagline{color:var(--muted);font-size:12px}
.segmented{display:inline-flex;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:3px;gap:2px}
.segmented button{background:transparent;border:0;color:var(--muted);padding:7px 16px;border-radius:8px;cursor:pointer;font-size:12px;letter-spacing:.04em;text-transform:uppercase}
.segmented button.active{background:var(--panel-2);color:var(--text);box-shadow:inset 0 0 0 1px var(--line)}
.combined-warning{color:var(--warn);font-size:12px;margin:6px 0 18px;opacity:.9}
.kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi .kpi-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.kpi .kpi-value{font-size:26px;font-weight:600;margin-top:6px}
.kpi .kpi-value.pos{color:var(--win)}
.kpi .kpi-value.neg{color:var(--loss)}
.kpi-ring{display:flex;align-items:center;gap:10px}
.spark-panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:22px}
.spark-panel h3{margin:0 0 8px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
.spark-empty{color:var(--muted);font-size:12px;padding:8px 0}
.section-title{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:26px 0 10px;display:flex;align-items:center;justify-content:space-between}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px}
.toolbar label{display:flex;flex-direction:column;gap:4px;color:var(--muted);font-size:11px}
.toolbar input,.toolbar select{background:var(--panel);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:7px 9px;font-size:12px}
.toolbar details{position:relative}
.toolbar details[open] .filter-pop{display:grid}
.filter-toggle{background:var(--panel);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:7px 12px;font-size:12px;cursor:pointer}
.filter-pop{display:none;grid-template-columns:repeat(2,1fr);gap:8px;position:absolute;z-index:5;top:calc(100% + 6px);background:var(--panel-2);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:280px}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap;font-size:13px}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0;background:var(--panel-2)}
tr.trade-row{cursor:pointer}
tr.trade-row:hover{background:rgba(255,255,255,.03)}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;letter-spacing:.03em;text-transform:uppercase}
.badge-win{background:rgba(79,208,127,.15);color:var(--win)}
.badge-loss{background:rgba(242,109,109,.15);color:var(--loss)}
.badge-be{background:rgba(201,194,106,.15);color:var(--be)}
.badge-unknown{background:rgba(139,152,179,.15);color:var(--muted)}
.badge-live{background:rgba(95,211,196,.15);color:var(--live)}
.badge-paper{background:rgba(236,180,92,.15);color:var(--warning)}
.badge-backtest{background:rgba(143,126,224,.15);color:var(--backtest)}
.badge-long{background:rgba(95,211,196,.12);color:var(--accent)}
.badge-short{background:rgba(242,109,109,.12);color:var(--loss)}
.badge-status{background:rgba(139,152,179,.12);color:var(--muted)}
.empty-row td{color:var(--muted);text-align:center;padding:26px}
.trade-cards{display:grid;gap:8px}
.trade-card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px;cursor:pointer}
.trade-card .row1{display:flex;justify-content:space-between;align-items:center}
.trade-card .row2{display:flex;gap:8px;margin-top:6px;flex-wrap:wrap;color:var(--muted);font-size:12px}
.health-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.health-item{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.health-item h3{margin:0 0 6px;font-size:13px}
.state{color:var(--accent);text-transform:uppercase;letter-spacing:.06em;font-size:11px}
.queue{display:grid;gap:8px}
.queue-item{background:var(--panel);border:1px solid #4a3a1f;border-radius:10px;padding:10px 12px}
.provenance{margin-top:26px}
.provenance summary{cursor:pointer;color:var(--muted);font-size:12px}
.provenance .kv{display:grid;grid-template-columns:200px 1fr;gap:6px;font-size:12px;color:var(--muted);margin-top:10px}
.provenance code{color:var(--text);word-break:break-all}
.note{color:var(--muted);font-size:12px}

/* --- Trade Detail overlay --- */
#detail-overlay{position:fixed;inset:0;background:rgba(4,7,14,.72);display:none;align-items:flex-start;justify-content:center;padding:28px 16px;overflow-y:auto;z-index:50}
#detail-overlay.open{display:flex}
.detail-panel{background:var(--panel-2);border:1px solid var(--line);border-radius:16px;max-width:980px;width:100%;padding:24px;position:relative}
.detail-close{position:absolute;top:16px;right:16px;background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:6px 10px;cursor:pointer}
.detail-header{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:baseline;margin-bottom:6px}
.detail-header h2{margin:0;font-size:20px}
.detail-sub{color:var(--muted);font-size:12px;margin-bottom:18px}
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.detail-block{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px}
.detail-block h3{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.kv-list{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:13px}
.kv-list dt{color:var(--muted)}
.kv-list dd{margin:0}
.hero-image{border-radius:12px;overflow:hidden;border:1px solid var(--line);background:var(--panel);cursor:zoom-in}
.hero-image img{width:100%;display:block;max-height:520px;object-fit:contain;background:#000}
.evidence-empty{color:var(--muted);font-size:13px;background:var(--panel);border:1px dashed var(--line);border-radius:12px;padding:22px;text-align:center}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px}
.gallery figure{margin:0;cursor:zoom-in}
.gallery img{width:100%;height:90px;object-fit:cover;border-radius:8px;border:1px solid var(--line)}
.gallery figcaption{font-size:10px;color:var(--muted);margin-top:4px;text-transform:uppercase;letter-spacing:.03em}
.evidence-subgroup h4{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin:14px 0 8px}
.lightbox{position:fixed;inset:0;background:rgba(0,0,0,.88);display:none;align-items:center;justify-content:center;z-index:60;padding:24px}
.lightbox.open{display:flex}
.lightbox img{max-width:100%;max-height:100%;border-radius:8px}
.historical-note{color:var(--muted);font-size:13px;font-style:italic}

@media(max-width:860px){
  .kpi-row{grid-template-columns:repeat(2,1fr)}
  .detail-grid{grid-template-columns:1fr}
}
@media(max-width:760px){
  .table-wrap{display:none}
  .trade-cards{display:block}
}
@media(max-width:520px){
  .kpi-row{grid-template-columns:1fr 1fr}
  .app-shell{padding:14px 14px 50px}
  .detail-panel{padding:16px}
}
"""


# Browser is presentation-only: it fetches trade-view-data.json (already
# fully computed by trading_os.operational_analytics /
# trading_os.strategy_health) and renders it. There is no ratio/statistic
# arithmetic anywhere in this file beyond pure display formatting of an
# already-governed number (e.g. 0.639 -> "63.9%"). The cumulative_r_by_mode
# series plotted below is computed entirely in operational_analytics.py
# (_cumulative_r_by_mode) and reaches the browser unmodified; this script
# only maps its already-summed points onto an SVG viewBox (display scaling,
# not a derived metric). Filtering only narrows which already-governed rows
# are displayed.
APP_JS = r"""
(function () {
  "use strict";
  var state = { data: null, mode: "all", filters: { from: "", to: "", strategy: "", symbol: "", status: "", search: "" } };

  function esc(v) {
    if (v === null || v === undefined || v === "") return "—";
    return String(v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  // Pure formatting of an already-governed number. Never derives a new ratio.
  function fmtPct(v) { return (v === null || v === undefined) ? "—" : (Math.round(v * 1000) / 10).toFixed(1) + "%"; }
  function fmtR(v) { return (v === null || v === undefined) ? "—" : (Math.round(v * 100) / 100).toFixed(2); }
  function fmtNum(v) { return (v === null || v === undefined) ? "—" : String(v); }
  function displayStrategy(row) { return row.strategy_display_name || row.strategy_id || "—"; }

  function outcomeBadge(outcome) {
    var cls = { win: "badge-win", loss: "badge-loss", breakeven: "badge-be" }[outcome] || "badge-unknown";
    var label = { win: "WIN", loss: "LOSS", breakeven: "BE" }[outcome] || "UNKNOWN";
    return '<span class="badge ' + cls + '">' + label + "</span>";
  }
  function modeBadge(mode) {
    if (mode === "live") return '<span class="badge badge-live">LIVE</span>';
    if (mode === "paper") return '<span class="badge badge-paper">PAPER</span>';
    if (mode === "backtest") return '<span class="badge badge-backtest">BACKTEST</span>';
    return '<span class="badge badge-unknown">' + esc(mode) + "</span>";
  }
  function sideBadge(direction) {
    var d = (direction || "").toLowerCase();
    if (d === "long") return '<span class="badge badge-long">LONG</span>';
    if (d === "short") return '<span class="badge badge-short">SHORT</span>';
    return '<span class="badge badge-unknown">—</span>';
  }
  function statusBadge(row) {
    if (row.counted !== true) return '<span class="badge badge-status" title="' + esc(row.exclusion_reason) + '">Needs review</span>';
    if (row.status === "historical_seed") return '<span class="badge badge-status">Historical</span>';
    if (row.review_status === "reviewed") return '<span class="badge badge-status">Reviewed</span>';
    return '<span class="badge badge-status">Open</span>';
  }

  function visible(row, f) {
    if (state.mode !== "all" && row.mode !== state.mode) return false;
    if (f.from && (!row.open_date || row.open_date < f.from)) return false;
    if (f.to && (!row.open_date || row.open_date > f.to)) return false;
    if (f.strategy && row.strategy_id !== f.strategy) return false;
    if (f.symbol && row.symbol !== f.symbol) return false;
    if (f.status) {
      if (f.status === "reviewed" && row.review_status !== "reviewed") return false;
      if (f.status === "needs_review" && row.counted === true) return false;
      if (f.status === "historical" && row.status !== "historical_seed") return false;
    }
    if (f.search) {
      var hay = (esc(row.symbol) + " " + esc(displayStrategy(row)) + " " + esc(row.reference) + " " + esc((row.advanced && row.advanced.trade_no) || "")).toLowerCase();
      if (hay.indexOf(f.search.toLowerCase()) === -1) return false;
    }
    return true;
  }

  function currentModeSummary() {
    var modes = (state.data && state.data.summary_by_mode) || {};
    return modes[state.mode] || {};
  }

  function renderKpis() {
    var s = currentModeSummary();
    var totalCls = (s.total_r > 0) ? "pos" : (s.total_r < 0) ? "neg" : "";
    var html = [
      ["Total R", fmtR(s.total_r), totalCls],
      ["Profit Factor", fmtNum(s.profit_factor), ""],
      ["Win Rate", fmtPct(s.win_rate), ""],
      ["Avg R", fmtR(s.mean_r), ""]
    ].map(function (x) {
      return '<div class="kpi"><div class="kpi-label">' + esc(x[0]) + '</div><div class="kpi-value ' + x[2] + '">' + x[1] + "</div></div>";
    }).join("");
    document.getElementById("kpi-row").innerHTML = html;
    var warn = document.getElementById("combined-warning");
    warn.style.display = state.mode === "all" ? "block" : "none";
  }

  function renderSpark() {
    var series = ((state.data && state.data.cumulative_r_by_mode) || {})[state.mode] || [];
    var el = document.getElementById("spark");
    if (!series.length) { el.innerHTML = '<div class="spark-empty">No governed closed trades in this mode yet.</div>'; return; }
    var w = 640, h = 90, pad = 6;
    var values = series.map(function (p) { return p.cumulative_r; });
    var min = Math.min(0, values.reduce(function (a, b) { return Math.min(a, b); }, 0));
    var max = Math.max(0, values.reduce(function (a, b) { return Math.max(a, b); }, 0));
    var span = (max - min) || 1;
    var stepX = series.length > 1 ? (w - pad * 2) / (series.length - 1) : 0;
    var points = values.map(function (v, i) {
      var x = pad + i * stepX;
      var y = h - pad - ((v - min) / span) * (h - pad * 2);
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    var zeroY = (h - pad - ((0 - min) / span) * (h - pad * 2)).toFixed(1);
    el.innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" style="width:100%;height:90px">' +
      '<line x1="0" y1="' + zeroY + '" x2="' + w + '" y2="' + zeroY + '" stroke="#232d40" stroke-dasharray="4 3" />' +
      '<polyline fill="none" stroke="#5fd3c4" stroke-width="2" points="' + points + '" /></svg>';
  }

  function renderModeSelector() {
    var el = document.getElementById("mode-selector");
    var modes = [["live", "Live"], ["paper", "Paper"], ["backtest", "Backtest"], ["all", "All"]];
    el.innerHTML = modes.map(function (m) {
      return '<button data-mode="' + m[0] + '" class="' + (state.mode === m[0] ? "active" : "") + '">' + m[1] + "</button>";
    }).join("");
    Array.prototype.forEach.call(el.querySelectorAll("button"), function (btn) {
      btn.addEventListener("click", function () { state.mode = btn.getAttribute("data-mode"); renderAll(); });
    });
  }

  function tradeRowsHtml(rows) {
    if (!rows.length) return '<tr class="empty-row"><td colspan="9">No trades match the selected filters.</td></tr>';
    return rows.map(function (r) {
      return '<tr class="trade-row" data-testid="trade-row" data-ref="' + esc(r.reference) + '" tabindex="0">' +
        "<td>" + esc(r.open_date) + "</td>" +
        "<td>" + esc(r.symbol) + "</td>" +
        "<td>" + sideBadge(r.direction) + "</td>" +
        "<td>" + esc(displayStrategy(r)) + "</td>" +
        "<td>" + modeBadge(r.mode) + "</td>" +
        "<td>" + outcomeBadge(r.outcome) + "</td>" +
        "<td>" + fmtR(r.realized_r) + "</td>" +
        "<td>" + statusBadge(r) + "</td>" +
        '<td class="opt-col">' + fmtR(r.entry) + " / " + fmtR(r.exit) + "</td>" +
        "</tr>";
    }).join("");
  }
  function tradeCardsHtml(rows) {
    if (!rows.length) return '<div class="note">No trades match the selected filters.</div>';
    return rows.map(function (r) {
      return '<div class="trade-card" data-testid="trade-row" data-ref="' + esc(r.reference) + '">' +
        '<div class="row1"><strong>' + esc(r.symbol) + " · " + esc(displayStrategy(r)) + "</strong>" + outcomeBadge(r.outcome) + "</div>" +
        '<div class="row2">' + esc(r.open_date) + " " + modeBadge(r.mode) + " " + sideBadge(r.direction) + " R=" + fmtR(r.realized_r) + "</div>" +
        "</div>";
    }).join("");
  }

  function renderTrades() {
    var d = state.data;
    var rows = d.rows.filter(function (r) { return visible(r, state.filters); });
    document.getElementById("trades-body").innerHTML = tradeRowsHtml(rows);
    document.getElementById("trade-cards").innerHTML = tradeCardsHtml(rows);
    Array.prototype.forEach.call(document.querySelectorAll('[data-testid="trade-row"]'), function (el) {
      el.addEventListener("click", function () { location.hash = "#/trade/" + encodeURIComponent(el.getAttribute("data-ref")); });
    });
  }

  function renderHealth() {
    var d = state.data;
    document.getElementById("health").innerHTML = (d.health || []).map(function (h) {
      return '<article class="health-item"><h3>' + esc(h.strategy_id) + ' <span class="state">' + esc(h.health_state) + "</span></h3>" +
        "<div>n=" + esc(h.sample_n) + " · live=" + esc(h.live_n) + " · backtest=" + esc(h.backtest_n) + "</div>" +
        "<div>win rate=" + fmtPct(h.performance_summary.win_rate) + " · expectancy=" + fmtR(h.performance_summary.expectancy_r) + " · PF=" + fmtNum(h.performance_summary.profit_factor) + "</div>" +
        '<p class="note">' + esc((h.limitations || []).join(" ")) + "</p></article>";
    }).join("");
  }
  function renderQueue() {
    var d = state.data;
    document.getElementById("queue").innerHTML = (d.review_queue || []).map(function (q) {
      return '<div class="queue-item"><strong>' + esc(q.reference) + "</strong> — " + esc(q.reason) + "</div>";
    }).join("") || '<div class="note">Review queue cleared.</div>';
  }
  function renderProvenance() {
    var d = state.data;
    document.getElementById("provenance-body").innerHTML =
      '<div class="kv"><span>Analytics artifact digest</span><code>' + esc(d.sources.analytics_artifact_digest) + "</code></div>" +
      '<div class="kv"><span>Health artifact digest</span><code>' + esc(d.sources.health_artifact_digest) + "</code></div>" +
      '<div class="kv"><span>Canonical projection</span><code>' + esc(d.sources.canonical_projection) + "</code></div>" +
      '<div class="kv"><span>Generated at</span><code>' + esc(d.generated_at) + "</code></div>";
  }

  function populateFilterOptions() {
    var d = state.data;
    var strategySel = document.getElementById("f-strategy");
    strategySel.innerHTML = '<option value="">All strategies</option>' + d.filters.strategy.map(function (s) {
      return '<option value="' + esc(s) + '">' + esc(d.strategy_display_names[s] || s) + "</option>";
    }).join("");
    var symbolSel = document.getElementById("f-symbol");
    symbolSel.innerHTML = '<option value="">All symbols</option>' + d.filters.symbol.map(function (s) { return '<option value="' + esc(s) + '">' + esc(s) + "</option>"; }).join("");
  }

  // ---- Trade Detail ----
  function findRow(ref) { return (state.data.rows || []).find(function (r) { return String(r.reference) === String(ref); }); }

  function evidenceEmptyOr(items, label) {
    if (!items || !items.length) return '<div class="evidence-empty">No ' + label + " were preserved for this trade.</div>";
    return null;
  }

  function galleryHtml(items) {
    return '<div class="gallery">' + items.map(function (it) {
      return "<figure><img loading=\"lazy\" src=\"" + esc(it.href) + "\" alt=\"" + esc(it.categories.join(", ")) + "\" data-lightbox=\"" + esc(it.href) + "\" />" +
        "<figcaption>" + esc(it.categories.join(", ")) + "</figcaption></figure>";
    }).join("") + "</div>";
  }

  function renderDetail(ref) {
    var row = findRow(ref);
    var overlay = document.getElementById("detail-overlay");
    if (!row) { overlay.classList.remove("open"); return; }
    var gallery = row.evidence_gallery || [];
    var chartItems = gallery.filter(function (g) { return g.kind === "chart_context"; });
    var providerItems = gallery.filter(function (g) { return g.kind === "provider_execution"; });
    var hero = chartItems[0];
    var isHistorical = row.status === "historical_seed";

    var planEmpty = !row.plan.entry && !row.plan.stop_loss && !row.plan.take_profit && !row.plan.thesis;
    var reviewEmpty = row.review.status === "not_observable" && !row.review.what_went_well && !row.review.owner_notes;
    var executionEmpty = !row.execution.provider && !row.execution.actual_entry && !row.execution.actual_exit;

    var html = "";
    html += '<button class="detail-close" id="detail-close-btn" aria-label="Close">Close ✕</button>';
    html += '<div class="detail-header"><h2>' + esc(row.symbol) + "</h2>" + sideBadge(row.direction) + modeBadge(row.mode) + outcomeBadge(row.outcome) + "</div>";
    html += '<div class="detail-sub">' + esc(displayStrategy(row)) + " · rule " + esc(row.rule_version) + " · " + esc(row.open_date) +
      (row.close_date ? " → " + esc(row.close_date) : "") + " · Realized R " + fmtR(row.realized_r) +
      (row.strategy_display_name ? "" : "") + "</div>";

    // Evidence hero + gallery (section 10)
    html += '<div class="detail-block"><h3>Chart & Evidence</h3>';
    if (hero) {
      html += '<div class="hero-image" data-lightbox="' + esc(hero.href) + '"><img src="' + esc(hero.href) + '" alt="Primary chart evidence" /></div>';
    } else {
      html += evidenceEmptyOr([], isHistorical ? "chart evidence" : "chart or setup evidence") || "";
    }
    if (chartItems.length > 1) {
      html += '<div class="evidence-subgroup"><h4>Setup / Chart / Review evidence</h4>' + galleryHtml(chartItems) + "</div>";
    }
    if (providerItems.length) {
      html += '<div class="evidence-subgroup"><h4>Provider / execution evidence (not chart authority)</h4>' + galleryHtml(providerItems) + "</div>";
    }
    if (isHistorical && !gallery.length) {
      html += '<p class="historical-note">Historical evidence — detailed chart evidence was not preserved for this sample.</p>';
    }
    html += "</div>";

    html += '<div class="detail-grid">';

    // Plan
    html += '<div class="detail-block"><h3>Plan</h3>';
    if (planEmpty) {
      html += '<p class="historical-note">' + (isHistorical ? "Historical evidence — detailed plan was not preserved for this sample." : "No plan detail recorded.") + "</p>";
    } else {
      html += '<dl class="kv-list">' +
        "<dt>Planned entry</dt><dd>" + fmtR(row.plan.entry) + "</dd>" +
        "<dt>Stop loss</dt><dd>" + fmtR(row.plan.stop_loss) + "</dd>" +
        "<dt>Take profit</dt><dd>" + fmtR(row.plan.take_profit) + "</dd>" +
        "<dt>Planned R:R</dt><dd>" + fmtNum(row.plan.planned_rr) + "</dd>" +
        "<dt>Context timeframe</dt><dd>" + esc(row.plan.timeframe_context) + "</dd>" +
        "<dt>Trigger timeframe</dt><dd>" + esc(row.plan.timeframe_trigger) + "</dd>" +
        "<dt>Market context</dt><dd>" + esc(row.plan.market_context) + "</dd>" +
        "<dt>Setup tags</dt><dd>" + esc((row.plan.setup_tags || []).join(", ")) + "</dd>" +
        "<dt>Confirmation tags</dt><dd>" + esc((row.plan.confirmation_tags || []).join(", ")) + "</dd>" +
        "<dt>Thesis</dt><dd>" + esc(row.plan.thesis) + "</dd>" +
        "</dl>";
    }
    html += "</div>";

    // Execution (kept visually and semantically distinct from Plan/Method)
    html += '<div class="detail-block" id="execution-block"><h3>Execution' + (row.mode === "paper" ? " (PAPER / deterministic)" : row.mode === "backtest" ? " (simulated / replay)" : "") + "</h3>";
    if (executionEmpty) {
      html += '<p class="historical-note">' + (isHistorical ? "Historical evidence — execution detail was not preserved for this sample." : "No execution detail recorded.") + "</p>";
    } else if (row.mode === "backtest") {
      html += '<dl class="kv-list">' +
        "<dt>Source method</dt><dd>" + esc(row.source_method) + "</dd>" +
        "<dt>Recording timing</dt><dd>" + esc(row.recording_timing) + "</dd>" +
        "<dt>Outcome known at recording</dt><dd>" + esc(row.outcome_known_at_recording) + "</dd>" +
        "<dt>Entry evidence quality</dt><dd>" + esc(row.evidence_quality) + "</dd>" +
        "<dt>Actual entry</dt><dd>" + fmtR(row.execution.actual_entry) + "</dd>" +
        "<dt>Actual exit</dt><dd>" + fmtR(row.execution.actual_exit) + "</dd>" +
        "</dl>";
    } else {
      html += '<dl class="kv-list">' +
        "<dt>Provider</dt><dd>" + esc(row.execution.provider) + "</dd>" +
        "<dt>Verification</dt><dd>" + esc(row.execution.entry_verification && row.execution.entry_verification.state) + "</dd>" +
        "<dt>Actual entry</dt><dd>" + fmtR(row.execution.actual_entry) + "</dd>" +
        "<dt>Actual exit</dt><dd>" + fmtR(row.execution.actual_exit) + "</dd>" +
        "<dt>Realized R</dt><dd>" + fmtR(row.realized_r) +
        (row.r_performance_eligible === false ? " (estimated; excluded from verified R statistics)" : "") + "</dd>" +
        "<dt>Provider P&amp;L</dt><dd>" + (row.execution.provider_pnl_verified ? fmtR(row.execution.provider_pnl) : "not verified") + "</dd>" +
        "</dl>";
    }
    html += "</div>";
    html += "</div>"; // detail-grid

    // Review: METHOD and EXECUTION quality kept in distinct sub-blocks (section 13)
    html += '<div class="detail-block"><h3>Review</h3>';
    if (reviewEmpty) {
      html += '<p class="historical-note">' + (isHistorical ? "Historical evidence — no post-trade review was preserved for this sample." : "No review recorded yet.") + "</p>";
    } else {
      html += '<div class="detail-grid">';
      html += '<div id="review-method"><h4>Method</h4><dl class="kv-list">' +
        "<dt>Diagnostic class</dt><dd>" + esc(row.review.diagnostic_class) + "</dd>" +
        "<dt>Thesis validity</dt><dd>" + esc(row.review.thesis_validity) + "</dd>" +
        "<dt>Rule compliance</dt><dd>" + esc(row.review.rule_compliance) + "</dd>" +
        "</dl></div>";
      html += '<div id="review-execution"><h4>Execution quality</h4><dl class="kv-list">' +
        "<dt>Entry quality</dt><dd>" + esc(row.review.entry_quality) + "</dd>" +
        "<dt>Exit quality</dt><dd>" + esc(row.review.exit_quality) + "</dd>" +
        "<dt>Execution deviation</dt><dd>" + esc(row.review.execution_deviation) + "</dd>" +
        "</dl></div>";
      html += "</div>";
      html += "<dl class=\"kv-list\"><dt>What happened</dt><dd>" + esc(row.review.what_went_well || row.review.what_went_wrong) + "</dd>" +
        "<dt>Owner notes</dt><dd>" + esc(row.review.owner_notes) + "</dd>" +
        "<dt>Evidence completeness</dt><dd>" + esc(row.review.evidence_completeness) + "</dd></dl>";
    }
    html += "</div>";

    // Advanced details / provenance (collapsed, section 15)
    html += '<details class="provenance"><summary>Advanced details / data provenance</summary><div class="kv">' +
      "<span>Trade ID</span><code>" + esc(row.advanced.trade_id) + "</code>" +
      "<span>Draft / observation ID</span><code>" + esc(row.advanced.observation_id) + "</code>" +
      "<span>Canonical episode ID</span><code>" + esc(row.advanced.canonical_episode_id) + "</code>" +
      "<span>Strategy ID</span><code>" + esc(row.strategy_id) + "</code>" +
      "<span>Rule version</span><code>" + esc(row.advanced.rule_version) + "</code>" +
      "<span>Source kind</span><code>" + esc(row.advanced.source_kind) + "</code>" +
      "<span>Raw status</span><code>" + esc(row.advanced.raw_status) + "</code>" +
      "<span>Counted</span><code>" + esc(row.advanced.counted) + "</code>" +
      "<span>Exclusion reason</span><code>" + esc(row.advanced.exclusion_reason) + "</code>" +
      "<span>Evidence quality</span><code>" + esc(row.advanced.evidence_quality) + "</code>" +
      "<span>Evidence SHA-256</span><code>" + esc((row.advanced.evidence_sha256 || []).join(", ")) + "</code>" +
      "<span>Evidence ref</span><code>" + esc(row.advanced.evidence_ref) + "</code>" +
      "</div></details>";

    document.getElementById("detail-panel").innerHTML = html;
    document.getElementById("detail-close-btn").addEventListener("click", function () { location.hash = "#/"; });
    Array.prototype.forEach.call(document.querySelectorAll("[data-lightbox]"), function (el) {
      el.addEventListener("click", function () { openLightbox(el.getAttribute("data-lightbox")); });
    });
    overlay.classList.add("open");
  }

  function openLightbox(href) {
    var lb = document.getElementById("lightbox");
    document.getElementById("lightbox-img").src = href;
    lb.classList.add("open");
  }

  function handleHash() {
    var m = /^#\/trade\/(.+)$/.exec(location.hash);
    if (m) { renderDetail(decodeURIComponent(m[1])); }
    else { document.getElementById("detail-overlay").classList.remove("open"); }
  }

  function renderAll() {
    renderModeSelector();
    renderKpis();
    renderSpark();
    renderTrades();
    handleHash();
  }

  function wireFilters() {
    var ids = ["f-from", "f-to", "f-strategy", "f-symbol", "f-status", "f-search"];
    ids.forEach(function (id) {
      document.getElementById(id).addEventListener("input", function () {
        state.filters = {
          from: document.getElementById("f-from").value,
          to: document.getElementById("f-to").value,
          strategy: document.getElementById("f-strategy").value,
          symbol: document.getElementById("f-symbol").value,
          status: document.getElementById("f-status").value,
          search: document.getElementById("f-search").value
        };
        renderTrades();
      });
    });
  }

  function shell() {
    return '' +
      '<div class="app-shell">' +
      '<header class="app-header"><h1>Trade View</h1><span class="tagline">Private journal · governed Trading Record V1 data</span></header>' +
      '<div id="mode-selector" class="segmented"></div>' +
      '<p id="combined-warning" class="combined-warning">Combined descriptive only — LIVE, PAPER, and BACKTEST are separate evidence populations.</p>' +
      '<div id="kpi-row" class="kpi-row"></div>' +
      '<div class="spark-panel"><h3>Cumulative R</h3><div id="spark"></div></div>' +
      '<div class="section-title">Trades' +
        '<span><details><summary class="filter-toggle">Filters</summary><div class="filter-pop">' +
          '<label>From<input id="f-from" type="date"></label>' +
          '<label>To<input id="f-to" type="date"></label>' +
          '<label>Strategy<select id="f-strategy"></select></label>' +
          '<label>Symbol<select id="f-symbol"></select></label>' +
          '<label>Status<select id="f-status"><option value="">All</option><option value="reviewed">Reviewed</option><option value="needs_review">Needs review</option><option value="historical">Historical</option></select></label>' +
          '<label>Search<input id="f-search" type="text" placeholder="symbol, strategy, id"></label>' +
        '</div></details></span>' +
      '</div>' +
      '<div class="table-wrap"><table><thead><tr><th>Date</th><th>Symbol</th><th>Side</th><th>Strategy</th><th>Mode</th><th>Result</th><th>R</th><th>Status</th><th>Entry / Exit</th></tr></thead><tbody id="trades-body"></tbody></table></div>' +
      '<div id="trade-cards" class="trade-cards"></div>' +
      '<div class="section-title">Strategy Health</div><div id="health" class="health-grid"></div>' +
      '<div class="section-title">Needs Review</div><div id="queue" class="queue"></div>' +
      '<details class="provenance"><summary>Data provenance (analytics / health digests)</summary><div id="provenance-body"></div></details>' +
      '</div>' +
      '<div id="detail-overlay"><div class="detail-panel" id="detail-panel"></div></div>' +
      '<div class="lightbox" id="lightbox"><img id="lightbox-img" src="" alt="Evidence enlarged" /></div>';
  }

  document.getElementById("app").innerHTML = shell();
  document.getElementById("lightbox").addEventListener("click", function () { this.classList.remove("open"); });
  wireFilters();
  window.addEventListener("hashchange", handleHash);

  fetch("trade-view-data.json").then(function (r) { return r.json(); }).then(function (d) {
    state.data = d;
    populateFilterOptions();
    renderHealth();
    renderQueue();
    renderProvenance();
    renderAll();
  });
})();
"""


# Trade View V3 presentation pass.  The V2 strings above remain in the module
# as the historical implementation reference; build_trade_view emits these
# V3 assets.  The data contract stays additive/read-only and the browser still
# consumes governed analytics, health, and evidence bindings without deriving
# authoritative metrics.
HTML_V3 = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Record — Private Workspace</title>
<link rel="stylesheet" href="style.css">
</head><body>
<div id="app" role="application" aria-label="Trading Record private workspace"></div>
<script src="app.js"></script>
</body></html>
"""

STYLE_CSS_V3 = r"""
:root{color-scheme:dark;--bg-app:#0b0d10;--bg-sidebar:#0e1014;--surface-1:#111419;--surface-2:#15191f;--surface-hover:#191e25;--border-subtle:rgba(255,255,255,.07);--border-strong:rgba(255,255,255,.12);--text-primary:#f2f4f7;--text-secondary:#a8afb9;--text-muted:#6f7782;--profit:#71c796;--loss:#d87878;--warning:#d2a45b;--live:#829cc4;--backtest:#9b8fbc;--focus:#92b7e8;--radius-sm:6px;--radius-md:8px;--radius-lg:11px;--shadow:0 18px 50px rgba(0,0,0,.24)}
*{box-sizing:border-box}
html{background:var(--bg-app);scroll-behavior:smooth}
body{margin:0;background:var(--bg-app);color:var(--text-primary);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;letter-spacing:.005em}
button,input,select{font:inherit}
button,a{ -webkit-tap-highlight-color:transparent}
a{color:inherit;text-decoration:none}
button{color:inherit}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.app-shell{min-height:100vh;background:var(--bg-app)}
.topbar{height:64px;border-bottom:1px solid var(--border-subtle);background:rgba(11,13,16,.94);backdrop-filter:blur(14px);position:sticky;top:0;z-index:20}
.topbar-inner{height:100%;max-width:1600px;margin:0 auto;padding:0 28px;display:flex;align-items:center;justify-content:space-between;gap:20px}
.brand{display:flex;align-items:center;gap:12px;min-width:220px}
.brand-mark{width:24px;height:24px;border:1px solid var(--border-strong);border-radius:7px;display:grid;place-items:center;color:var(--text-secondary);font-size:11px;font-weight:700;letter-spacing:-.08em}
.brand-copy{display:grid;gap:0}
.brand-name{font-size:12px;letter-spacing:.14em;font-weight:700}
.brand-subtitle{font-size:10px;color:var(--text-muted);letter-spacing:.02em}
.mode-selector{display:inline-flex;align-items:center;gap:2px;padding:3px;background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:var(--radius-md)}
.mode-selector button{border:0;background:transparent;color:var(--text-muted);padding:6px 12px;border-radius:5px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;transition:background 150ms ease,color 150ms ease}
.mode-selector button:hover{color:var(--text-primary);background:rgba(255,255,255,.035)}
.mode-selector button.active{background:var(--surface-hover);color:var(--text-primary);box-shadow:inset 0 0 0 1px var(--border-subtle)}
.workspace-layout{max-width:1600px;margin:0 auto;display:grid;grid-template-columns:216px minmax(0,1fr);min-height:calc(100vh - 64px)}
.sidebar{background:var(--bg-sidebar);border-right:1px solid var(--border-subtle);padding:26px 14px 24px;display:flex;flex-direction:column;gap:26px}
.sidebar-label{padding:0 11px;color:var(--text-muted);font-size:10px;letter-spacing:.12em;text-transform:uppercase}
.nav-list{display:grid;gap:3px}
.nav-item{position:relative;display:flex;align-items:center;gap:10px;padding:9px 11px;border-radius:var(--radius-sm);color:var(--text-secondary);font-size:13px;transition:background 150ms ease,color 150ms ease}
.nav-item:hover{background:rgba(255,255,255,.035);color:var(--text-primary)}
.nav-item.active{background:rgba(255,255,255,.055);color:var(--text-primary)}
.nav-item.active:before{content:"";position:absolute;left:-14px;width:2px;height:16px;background:var(--text-secondary);border-radius:0 2px 2px 0}
.nav-icon{width:16px;text-align:center;color:var(--text-muted);font-size:12px}
.nav-item.active .nav-icon{color:var(--text-secondary)}
.sidebar-spacer{flex:1}
.data-status{margin:0 6px;padding:13px 12px;border-top:1px solid var(--border-subtle);display:grid;gap:5px}
.data-status-label{color:var(--text-muted);font-size:10px;letter-spacing:.1em;text-transform:uppercase}
.data-status-value{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--text-secondary)}
.status-dot{width:6px;height:6px;border-radius:50%;background:var(--profit);box-shadow:0 0 0 3px rgba(113,199,150,.1)}
.data-status-note{font-size:10px;color:var(--text-muted);line-height:1.4}
.main-content{min-width:0;padding:42px clamp(24px,4vw,64px) 72px}
.content-wrap{max-width:1120px;margin:0 auto}
.page-header{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:32px}
.eyebrow{color:var(--text-muted);font-size:11px;letter-spacing:.13em;text-transform:uppercase;margin-bottom:8px}
.page-title{margin:0;font-size:28px;line-height:1.15;letter-spacing:-.025em;font-weight:650}
.page-subtitle{margin:8px 0 0;color:var(--text-secondary);font-size:14px}
.page-meta{text-align:right;color:var(--text-muted);font-size:11px;white-space:nowrap}
.page-meta strong{display:block;color:var(--text-secondary);font-weight:500;margin-top:3px;font-variant-numeric:tabular-nums}
.section{margin-top:32px}
.section-heading{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:12px}
.section-heading h2{margin:0;font-size:16px;line-height:1.25;font-weight:600;letter-spacing:-.01em}
.section-heading p{margin:0;color:var(--text-muted);font-size:12px}
.section-link{color:var(--text-secondary);font-size:12px;transition:color 150ms ease}
.section-link:hover{color:var(--text-primary)}
.section-link:after{content:"  →";color:var(--text-muted)}
.summary-strip{display:grid;grid-template-columns:repeat(4,1fr);background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:var(--radius-lg);overflow:hidden}
.summary-cell{min-width:0;padding:17px 18px 15px;border-right:1px solid var(--border-subtle)}
.summary-cell:last-child{border-right:0}
.summary-label{color:var(--text-muted);font-size:10px;letter-spacing:.12em;text-transform:uppercase}
.summary-value{margin-top:8px;font-size:21px;line-height:1.1;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.summary-value.action{color:var(--text-primary)}
.summary-value.profit{color:var(--profit)}
.summary-value.loss{color:var(--loss)}
.summary-note{margin-top:5px;color:var(--text-muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.attention-list,.trade-list,.review-list{display:grid;gap:8px}
.attention-item,.trade-row-card,.review-item{background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:var(--radius-md);transition:background 150ms ease,border-color 150ms ease,transform 150ms ease}
.attention-item:hover,.trade-row-card:hover,.review-item:hover{background:var(--surface-hover);border-color:var(--border-strong)}
.attention-item{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px;padding:16px 18px;cursor:pointer}
.attention-top,.trade-card-top{display:flex;align-items:center;gap:9px;min-width:0}
.attention-symbol,.trade-symbol{font-weight:650;letter-spacing:.01em}
.attention-arrow{align-self:center;color:var(--text-muted);font-size:18px;transition:transform 150ms ease}
.attention-item:hover .attention-arrow{transform:translateX(2px);color:var(--text-secondary)}
.semantic-dot{width:6px;height:6px;border-radius:50%;background:var(--warning);flex:0 0 auto}
.muted{color:var(--text-muted)}
.secondary{color:var(--text-secondary)}
.attention-sub{margin-top:6px;color:var(--text-secondary);font-size:12px}
.attention-reason{margin-top:8px;font-size:12px;color:var(--text-primary)}
.attention-foot{display:flex;justify-content:space-between;gap:16px;margin-top:12px;color:var(--text-muted);font-size:11px;font-variant-numeric:tabular-nums}
.attention-status{color:var(--warning);letter-spacing:.08em;text-transform:uppercase}
.empty-state{padding:28px 20px;border:1px dashed var(--border-strong);border-radius:var(--radius-md);color:var(--text-secondary);background:rgba(255,255,255,.012)}
.empty-state strong{display:block;color:var(--text-primary);font-size:14px;margin-bottom:4px}
.empty-state span{font-size:12px;color:var(--text-muted)}
.trade-row-card{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px;padding:16px 18px;cursor:pointer}
.trade-card-main{min-width:0}
.trade-card-meta{display:flex;flex-wrap:wrap;gap:7px 12px;margin-top:5px;color:var(--text-secondary);font-size:12px}
.trade-card-date{margin-top:8px;color:var(--text-muted);font-size:11px;font-variant-numeric:tabular-nums}
.trade-card-note{margin-top:9px;color:var(--text-secondary);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trade-card-result{text-align:right;align-self:center;min-width:86px}
.result-r{font-size:20px;line-height:1;font-weight:620;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.result-r.profit{color:var(--profit)}
.result-r.loss{color:var(--loss)}
.result-r.neutral{color:var(--text-secondary)}
.result-status{margin-top:7px;color:var(--text-muted);font-size:10px;letter-spacing:.08em;text-transform:uppercase}
.mode-label,.status-label,.direction-label{font-size:10px;letter-spacing:.06em;text-transform:uppercase}
.mode-label.live{color:var(--live)}
.mode-label.backtest{color:var(--backtest)}
.direction-label{color:var(--text-secondary)}
.performance-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:0;background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:var(--radius-lg);overflow:hidden}
.metric-line{padding:15px 17px;border-right:1px solid var(--border-subtle)}
.metric-line:last-child{border-right:0}
.metric-label{color:var(--text-muted);font-size:10px;letter-spacing:.09em;text-transform:uppercase}
.metric-value{margin-top:5px;font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}
.sparkline-panel{margin-top:10px;padding:15px 17px;background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:var(--radius-lg)}
.sparkline-label{display:flex;justify-content:space-between;color:var(--text-muted);font-size:10px;letter-spacing:.09em;text-transform:uppercase;margin-bottom:10px}
.sparkline{height:90px;width:100%;display:block}
.sparkline-empty{height:90px;display:grid;place-items:center;color:var(--text-muted);font-size:12px}
.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px;background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:var(--radius-md);margin-bottom:16px}
.toolbar-search{flex:1 1 210px;min-width:180px}
.toolbar input,.toolbar select{height:34px;background:var(--surface-2);border:1px solid var(--border-subtle);border-radius:var(--radius-sm);color:var(--text-primary);padding:0 10px;font-size:12px;min-width:108px}
.toolbar input::placeholder{color:var(--text-muted)}
.toolbar input:hover,.toolbar select:hover{border-color:var(--border-strong)}
.toolbar-divider{height:20px;width:1px;background:var(--border-subtle);margin:0 2px}
.view-toggle{display:inline-flex;gap:2px;padding:2px;background:var(--surface-2);border:1px solid var(--border-subtle);border-radius:var(--radius-sm)}
.view-toggle button{height:28px;padding:0 9px;border:0;border-radius:4px;background:transparent;color:var(--text-muted);font-size:11px;cursor:pointer}
.view-toggle button.active{background:var(--surface-hover);color:var(--text-primary)}
.rows-count{color:var(--text-muted);font-size:11px;margin-left:auto;white-space:nowrap}
.table-wrap{overflow-x:auto;border:1px solid var(--border-subtle);border-radius:var(--radius-md)}
table{border-collapse:collapse;width:100%}
th,td{padding:11px 13px;border-bottom:1px solid var(--border-subtle);text-align:left;white-space:nowrap;font-size:12px}
th{color:var(--text-muted);font-size:10px;letter-spacing:.08em;text-transform:uppercase;font-weight:500;background:var(--surface-2)}
tbody tr:last-child td{border-bottom:0}
tr.trade-row{cursor:pointer;transition:background 150ms ease}
tr.trade-row:hover{background:var(--surface-hover)}
.table-result{font-weight:600;font-variant-numeric:tabular-nums}
.trade-cards{display:none}
.badge{display:inline-flex;align-items:center;gap:5px;color:var(--text-secondary);font-size:10px;letter-spacing:.06em;text-transform:uppercase}
.badge:before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor;opacity:.8}
.badge-live{color:var(--live)}
.badge-paper{color:var(--warning)}
.badge-backtest{color:var(--backtest)}
.badge-status{color:var(--text-secondary)}
.badge-status.excluded,.badge-status.historical{color:var(--text-muted)}
.badge-status.reviewed{color:var(--profit)}
.badge-status.pending{color:var(--warning)}
.badge-status.problem{color:var(--loss)}
.insight-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.insight-panel{background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:var(--radius-md);padding:18px}
.insight-panel h3{margin:0;font-size:13px;font-weight:600}
.insight-panel p{margin:6px 0 0;color:var(--text-muted);font-size:12px}
.strategy-list{margin:16px -4px 0;display:grid}
.strategy-line{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:14px;align-items:center;padding:10px 4px;border-top:1px solid var(--border-subtle);font-size:12px}
.strategy-line-name{min-width:0;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.strategy-line-n{color:var(--text-muted);font-variant-numeric:tabular-nums}
.strategy-line-r{font-weight:600;font-variant-numeric:tabular-nums}
.quality-list{display:grid;gap:11px;margin-top:16px}
.quality-line{display:flex;justify-content:space-between;gap:16px;padding-top:10px;border-top:1px solid var(--border-subtle);font-size:12px}
.quality-line span:first-child{color:var(--text-secondary)}
.quality-line span:last-child{color:var(--text-primary);font-variant-numeric:tabular-nums}
.readiness{margin-top:12px;padding:14px 15px;border:1px solid var(--border-subtle);border-radius:var(--radius-md);background:rgba(255,255,255,.012)}
.readiness strong{font-size:13px}
.readiness p{margin:7px 0 0;color:var(--text-muted);font-size:12px;line-height:1.55}
.review-summary{display:grid;grid-template-columns:repeat(3,1fr);background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:var(--radius-lg);overflow:hidden}
.review-stat{padding:17px;border-right:1px solid var(--border-subtle)}
.review-stat:last-child{border-right:0}
.review-stat-label{color:var(--text-muted);font-size:10px;letter-spacing:.1em;text-transform:uppercase}
.review-stat-value{margin-top:5px;font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.review-item{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:20px;padding:16px 18px;align-items:center}
.review-item-title{font-weight:600}
.review-item-meta{margin-top:4px;color:var(--text-secondary);font-size:12px}
.review-item-reason{margin-top:8px;color:var(--warning);font-size:12px}
.open-trade{border:1px solid var(--border-subtle);background:transparent;border-radius:var(--radius-sm);padding:7px 10px;color:var(--text-secondary);font-size:11px;cursor:pointer;white-space:nowrap}
.open-trade:hover{background:var(--surface-hover);color:var(--text-primary)}
.detail-back{display:inline-flex;align-items:center;gap:7px;color:var(--text-muted);font-size:12px;margin-bottom:22px}
.detail-back:hover{color:var(--text-primary)}
.detail-header{display:flex;align-items:flex-end;justify-content:space-between;gap:22px;margin-bottom:26px}
.detail-identity h1{margin:0;font-size:28px;letter-spacing:-.025em;line-height:1.1}
.detail-identity-sub{margin-top:8px;color:var(--text-secondary);font-size:13px}
.detail-identity-meta{margin-top:5px;color:var(--text-muted);font-size:11px;font-variant-numeric:tabular-nums}
.detail-result{text-align:right}
.detail-result .result-r{font-size:30px}
.detail-pnl{margin-top:7px;color:var(--text-secondary);font-size:12px;font-variant-numeric:tabular-nums}
.detail-status{margin-top:7px;color:var(--profit);font-size:10px;letter-spacing:.1em;text-transform:uppercase}
.detail-surface{background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:var(--radius-lg);padding:20px;margin-top:14px}
.detail-surface-title{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:14px}
.detail-surface-title h2{margin:0;font-size:14px;font-weight:600}
.detail-surface-title span{color:var(--text-muted);font-size:11px}
.evidence-frame{min-height:180px;background:#090b0d;border:1px solid var(--border-subtle);border-radius:var(--radius-md);display:grid;place-items:center;overflow:hidden}
.evidence-frame img{display:block;width:100%;max-height:560px;object-fit:contain;cursor:zoom-in}
.evidence-empty{padding:34px 20px;color:var(--text-muted);font-size:12px;text-align:center}
.evidence-caption{margin-top:10px;color:var(--text-muted);font-size:11px}
.evidence-tabs{display:flex;gap:18px;border-bottom:1px solid var(--border-subtle);margin-bottom:14px}
.evidence-tab{border:0;background:transparent;color:var(--text-muted);padding:0 0 9px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;position:relative}
.evidence-tab.active{color:var(--text-primary)}
.evidence-tab.active:after{content:"";position:absolute;bottom:-1px;left:0;right:0;height:1px;background:var(--text-secondary)}
.evidence-gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:12px;margin-top:14px}
.evidence-gallery figure{margin:0;cursor:zoom-in}
.evidence-gallery img{width:100%;height:78px;object-fit:cover;border:1px solid var(--border-subtle);border-radius:var(--radius-sm)}
.evidence-gallery figcaption{margin-top:5px;color:var(--text-muted);font-size:10px;line-height:1.3}
.comparison{width:100%;border-collapse:collapse}
.comparison th,.comparison td{padding:10px 8px;border-bottom:1px solid var(--border-subtle);font-size:12px}
.comparison th{background:transparent;font-size:10px;color:var(--text-muted);text-align:right}
.comparison th:first-child,.comparison td:first-child{text-align:left;color:var(--text-muted)}
.comparison td{text-align:right;font-variant-numeric:tabular-nums}
.comparison tr:last-child td{border-bottom:0}
.timeline{position:relative;margin:4px 0 0 8px;padding-left:27px}
.timeline:before{content:"";position:absolute;left:4px;top:8px;bottom:8px;width:1px;background:var(--border-strong)}
.timeline-node{position:relative;padding:0 0 24px}
.timeline-node:last-child{padding-bottom:0}
.timeline-node:before{content:"";position:absolute;left:-27px;top:5px;width:8px;height:8px;border:2px solid var(--text-muted);background:var(--surface-1);border-radius:50%}
.timeline-kicker{color:var(--text-muted);font-size:10px;letter-spacing:.12em;text-transform:uppercase}
.timeline-time{margin-left:8px;color:var(--text-muted);font-size:10px;font-variant-numeric:tabular-nums}
.timeline-title{margin-top:5px;font-size:13px;font-weight:600}
.timeline-detail{margin-top:3px;color:var(--text-secondary);font-size:12px}
.triptych{display:grid;grid-template-columns:repeat(3,1fr);gap:0}
.triptych-column{padding:0 18px;border-right:1px solid var(--border-subtle)}
.triptych-column:first-child{padding-left:0}
.triptych-column:last-child{padding-right:0;border-right:0}
.triptych-label{color:var(--text-muted);font-size:10px;letter-spacing:.12em;text-transform:uppercase}
.triptych-value{margin-top:9px;color:var(--text-secondary);font-size:13px;line-height:1.5}
.triptych-note{margin-top:12px;color:var(--text-primary);font-size:12px}
.provenance{margin-top:14px}
.provenance summary{cursor:pointer;color:var(--text-secondary);font-size:12px}
.provenance-body{display:grid;grid-template-columns:190px minmax(0,1fr);gap:8px 14px;margin-top:14px;color:var(--text-muted);font-size:11px}
.provenance-body code{color:var(--text-secondary);word-break:break-all;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.lightbox{position:fixed;inset:0;display:none;align-items:center;justify-content:center;padding:26px;background:rgba(0,0,0,.9);z-index:60;cursor:zoom-out}
.lightbox.open{display:flex}
.lightbox img{max-width:100%;max-height:100%;object-fit:contain;border-radius:var(--radius-md)}
.error-state{padding:30px;border:1px solid rgba(216,120,120,.35);border-radius:var(--radius-md);color:var(--loss)}
@media(max-width:900px){.workspace-layout{grid-template-columns:184px minmax(0,1fr)}.topbar-inner{padding:0 20px}.main-content{padding:32px 28px 56px}.summary-strip{grid-template-columns:repeat(2,1fr)}.summary-cell:nth-child(2){border-right:0}.summary-cell:nth-child(-n+2){border-bottom:1px solid var(--border-subtle)}.performance-strip{grid-template-columns:repeat(2,1fr)}.metric-line:nth-child(2){border-right:0}.metric-line:nth-child(-n+2){border-bottom:1px solid var(--border-subtle)}.insight-grid{grid-template-columns:1fr}}
@media(max-width:760px){.table-wrap{display:none}.trade-cards{display:block}}
@media(max-width:700px){.topbar{height:58px}.topbar-inner{padding:0 14px}.brand{min-width:auto}.brand-subtitle{display:none}.brand-name{font-size:11px}.mode-selector button{padding:6px 8px}.workspace-layout{display:block;min-height:calc(100vh - 58px)}.sidebar{position:fixed;bottom:0;left:0;right:0;height:60px;z-index:30;border-right:0;border-top:1px solid var(--border-subtle);padding:6px 8px;background:rgba(14,16,20,.97);display:block}.sidebar-label,.sidebar-spacer,.data-status{display:none}.nav-list{height:100%;display:grid;grid-template-columns:repeat(4,1fr);gap:3px}.nav-item{height:48px;padding:4px 2px;display:flex;flex-direction:column;justify-content:center;gap:2px;text-align:center;font-size:10px}.nav-item.active:before{left:50%;top:-6px;width:16px;height:2px;transform:translateX(-50%);border-radius:2px}.nav-icon{font-size:13px}.main-content{padding:28px 15px 86px}.page-header{align-items:flex-start;margin-bottom:24px}.page-title{font-size:24px}.page-subtitle{font-size:13px}.page-meta{font-size:10px}.attention-item,.trade-row-card,.review-item{padding:14px}.attention-foot{display:block}.attention-status{display:block;margin-top:4px}.trade-card-result{min-width:72px}.result-r{font-size:18px}.toolbar{padding:8px}.toolbar-search{flex-basis:100%}.toolbar-divider{display:none}.rows-count{margin-left:0}.review-summary{grid-template-columns:repeat(3,1fr)}.review-stat{padding:13px 10px}.review-stat-value{font-size:18px}.review-stat-label{font-size:9px}.detail-header{align-items:flex-start;display:block;margin-bottom:22px}.detail-result{text-align:left;margin-top:17px}.detail-result .result-r{font-size:27px}.detail-surface{padding:15px}.triptych{display:block}.triptych-column{padding:0 0 16px;margin-bottom:16px;border-right:0;border-bottom:1px solid var(--border-subtle)}.triptych-column:last-child{padding-bottom:0;margin-bottom:0;border-bottom:0}.provenance-body{grid-template-columns:1fr}.comparison th,.comparison td{padding:9px 4px}}
@media(max-width:390px){.summary-value{font-size:18px}.summary-note{font-size:10px}.performance-strip{grid-template-columns:repeat(2,1fr)}.main-content{padding-left:13px;padding-right:13px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{scroll-behavior:auto!important;transition:none!important}}
@media(min-width:761px){.trade-cards{display:grid;gap:8px}}
"""

APP_JS_V3 = r"""
(function () {
  "use strict";
  var state = { data: null, mode: "all", view: "home", filters: { search: "", strategy: "", symbol: "", status: "" }, display: "rows", evidenceTab: "chart" };

  function esc(v) { if (v === null || v === undefined || v === "") return "—"; return String(v).replace(/[&<>\"']/g, function (c) { return { "&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;" }[c]; }); }
  function fmtR(v) { if (v === null || v === undefined || v === "") return "—"; var n = Number(v); return (n > 0 ? "+" : "") + n.toFixed(2) + "R"; }
  function fmtPlainR(v) { if (v === null || v === undefined || v === "") return "—"; return Number(v).toFixed(2) + "R"; }
  function fmtPct(v) { return v === null || v === undefined ? "—" : (Number(v) * 100).toFixed(1) + "%"; }
  function fmtMoney(v) { if (v === null || v === undefined || v === "") return "—"; var n = Number(v); return (n > 0 ? "+" : "") + n.toFixed(2) + " USDT"; }
  function fmtDate(v) { if (!v) return "—"; return String(v).slice(0, 10); }
  function fmtDateTime(v) { if (!v) return "—"; return String(v).replace("T", " ").replace("Z", "").slice(0, 16); }
  function strategyName(row) { return row.strategy_display_name || row.strategy_id || "Unclassified strategy"; }
  function summary() { return (state.data && state.data.summary_by_mode && state.data.summary_by_mode[state.mode]) || {}; }
  function strategySummary() { return (state.data && state.data.strategy_summary_by_mode && state.data.strategy_summary_by_mode[state.mode]) || {}; }
  function modeName(mode) { return mode === "live" ? "LIVE" : mode === "paper" ? "PAPER" : mode === "backtest" ? "BACKTEST" : "ALL"; }
  function modeClass(mode) { return mode === "live" ? "live" : mode === "paper" ? "paper" : mode === "backtest" ? "backtest" : ""; }
  function resultClass(v) { return Number(v) > 0 ? "profit" : Number(v) < 0 ? "loss" : "neutral"; }
  var ACTIVE_LIFECYCLE_STATES = ["PRE_TRADE_DRAFT", "ENTRY_PENDING_VERIFICATION", "OPEN", "PARTIALLY_CLOSED"];
  var REVIEWABLE_LIFECYCLE_STATES = ["CLOSED", "REVIEW_PENDING"];
  function lifecycleState(row) { return row.lifecycle_state || (row.status && (ACTIVE_LIFECYCLE_STATES.indexOf(row.status) >= 0 || REVIEWABLE_LIFECYCLE_STATES.indexOf(row.status) >= 0 || row.status === "REVIEWED") ? row.status : null); }
  function isActiveLifecycle(row) { return ACTIVE_LIFECYCLE_STATES.indexOf(lifecycleState(row)) >= 0; }
  function isHistoricalSeed(row) { return row.status === "historical_seed"; }
  function isHistoricalLink(row) { return row.status === "linked_to_historical_seed"; }
  function isInactiveStrategy(row) { return row.exclusion_reason === "inactive_strategy"; }
  function hasIncompleteEvidence(row) { return row.evidence_quality === "owner_declared_unverified" || row.evidence_completeness === "incomplete" || row.evidence_completeness === "insufficient"; }
  function requiresReview(row) { var state = lifecycleState(row); return !isHistoricalLink(row) && !isInactiveStrategy(row) && REVIEWABLE_LIFECYCLE_STATES.indexOf(state) >= 0 && row.review_status !== "reviewed"; }
  function lifecycleLabel(row) { var state = lifecycleState(row); return { PRE_TRADE_DRAFT: "Open", ENTRY_PENDING_VERIFICATION: "Entry pending", OPEN: "Open", PARTIALLY_CLOSED: "Partially closed", CLOSED: "Closed", REVIEW_PENDING: "Review pending", REVIEWED: "Reviewed" }[state] || "Lifecycle state unavailable"; }
  function statusClass(row) { if (isHistoricalSeed(row) || isHistoricalLink(row)) return "historical"; if (isInactiveStrategy(row)) return "excluded"; if (row.review_status === "reviewed" || lifecycleState(row) === "REVIEWED") return "reviewed"; if (requiresReview(row)) return "pending"; if (hasIncompleteEvidence(row)) return "problem"; return "pending"; }
  function statusLabel(row) { if (isHistoricalSeed(row)) return "Historical"; if (isHistoricalLink(row)) return "Historical link"; if (isInactiveStrategy(row)) return "Excluded · inactive strategy"; if (row.review_status === "reviewed" || lifecycleState(row) === "REVIEWED") return "Reviewed"; if (requiresReview(row)) return "Review pending"; if (hasIncompleteEvidence(row)) return "Evidence incomplete"; return lifecycleLabel(row); }
  function directionLabel(row) { return row.direction ? String(row.direction).toUpperCase() : "DIRECTION UNKNOWN"; }
  function rowDate(row) { return row.close_date || row.open_date || ""; }
  function sortRecent(a, b) { return String(rowDate(b)).localeCompare(String(rowDate(a))); }
  function allRows() { return (state.data && state.data.rows) || []; }
  function modeRows() { return allRows().filter(function (r) { return state.mode === "all" || r.mode === state.mode; }); }
  function visibleRows() {
    var f = state.filters;
    return modeRows().filter(function (r) {
      if (f.strategy && r.strategy_id !== f.strategy) return false;
      if (f.symbol && r.symbol !== f.symbol) return false;
      if (f.status === "reviewed" && r.review_status !== "reviewed") return false;
      if (f.status === "needs_review" && !requiresReview(r) && !hasIncompleteEvidence(r)) return false;
      if (f.search) { var hay = [r.symbol, strategyName(r), r.reference, r.advanced && r.advanced.trade_no, r.review && r.review.owner_notes].join(" ").toLowerCase(); if (hay.indexOf(f.search.toLowerCase()) === -1) return false; }
      return true;
    });
  }
  function navLink(view, icon, label) { return '<a class="nav-item ' + (state.view === view ? "active" : "") + '" href="#/' + view + '" data-view="' + view + '"><span class="nav-icon" aria-hidden="true">' + icon + '</span><span>' + label + '</span></a>'; }
  function shell() {
    return '<div class="app-shell"><header class="topbar"><div class="topbar-inner"><a class="brand" href="#/home" aria-label="Trading Record home"><span class="brand-mark">TR</span><span class="brand-copy"><span class="brand-name">TRADING RECORD</span><span class="brand-subtitle">Private Workspace</span></span></a><div id="mode-selector" class="mode-selector" role="group" aria-label="Evidence mode"></div></div></header><div class="workspace-layout"><aside class="sidebar"><div class="sidebar-label">Workspace</div><nav class="nav-list" aria-label="Primary navigation">' + navLink("home", "⌂", "Home") + navLink("journal", "≡", "Journal") + navLink("review", "◌", "Review") + navLink("insights", "◫", "Insights") + '</nav><div class="sidebar-spacer"></div><div class="data-status"><div class="data-status-label">Data status</div><div class="data-status-value"><span class="status-dot"></span><span>Local read model</span></div><div class="data-status-note">Presentation only · no writes</div></div></aside><main id="page-root" class="main-content"></main></div><div id="lightbox" class="lightbox" role="dialog" aria-label="Evidence preview"><img id="lightbox-img" src="" alt="Evidence enlarged" /></div></div>';
  }
  function renderModeSelector() { var el = document.getElementById("mode-selector"); el.innerHTML = [["live", "Live"], ["paper", "Paper"], ["backtest", "Backtest"], ["all", "All"]].map(function (m) { return '<button type="button" data-mode="' + m[0] + '" class="' + (state.mode === m[0] ? "active" : "") + '">' + m[1] + '</button>'; }).join(""); Array.prototype.forEach.call(el.querySelectorAll("button"), function (btn) { btn.addEventListener("click", function () { state.mode = btn.getAttribute("data-mode"); renderAll(); }); }); }
  function updateNav() { var activeView = state.view === "trade" ? "journal" : state.view; Array.prototype.forEach.call(document.querySelectorAll(".nav-item[data-view]"), function (item) { item.classList.toggle("active", item.getAttribute("data-view") === activeView); }); }
  function modeBadge(row) { return '<span class="badge badge-' + modeClass(row.mode) + '">' + modeName(row.mode) + '</span>'; }
  function statusBadge(row) { return '<span class="badge badge-status ' + statusClass(row) + '">' + statusLabel(row) + '</span>'; }
  function resultHtml(row, compact) { var cls = resultClass(row.realized_r); return '<div class="result-r ' + cls + '">' + fmtR(row.realized_r) + '</div>' + (compact ? '<div class="result-status">' + esc(statusLabel(row)) + '</div>' : ''); }
  function rowNarrative(row) { var review = row.review || {}; if (review.what_went_well) return review.what_went_well; if (review.owner_notes) return review.owner_notes; if (row.exclusion_reason) return row.exclusion_reason; if (row.outcome === "win") return "Recorded outcome · review context preserved"; if (row.outcome === "loss") return "Recorded outcome · review context preserved"; return "No reviewed narrative recorded."; }
  function tradeTarget(ref) { return "#/trade/" + encodeURIComponent(ref); }
  function tradeDateLabel(row) { var label = fmtDateTime(row.open_date); if (row.close_date) return label + " → " + fmtDateTime(row.close_date); return isActiveLifecycle(row) ? label + " · open" : label; }
  function tradeRowCard(row) { return '<article class="trade-row-card" data-testid="trade-row" data-ref="' + esc(row.reference) + '" tabindex="0" role="link" aria-label="Open ' + esc(row.symbol) + ' trade detail"><div class="trade-card-main"><div class="trade-card-top"><span class="trade-symbol">' + esc(row.symbol) + '</span><span class="direction-label">' + esc(directionLabel(row)) + '</span></div><div class="trade-card-meta"><span>' + esc(strategyName(row)) + '</span>' + modeBadge(row) + statusBadge(row) + '</div><div class="trade-card-date">' + esc(tradeDateLabel(row)) + '</div><div class="trade-card-note">' + esc(rowNarrative(row)) + '</div></div><div class="trade-card-result">' + resultHtml(row, true) + '</div></article>'; }
  function tradeTableRow(row) { return '<tr class="trade-row" data-testid="trade-row" data-ref="' + esc(row.reference) + '" tabindex="0"><td><strong>' + esc(row.symbol) + '</strong><br><span class="muted">' + esc(directionLabel(row)) + '</span></td><td>' + esc(strategyName(row)) + '</td><td>' + modeBadge(row) + '</td><td class="table-result ' + resultClass(row.realized_r) + '">' + esc(fmtR(row.realized_r)) + '</td><td>' + statusBadge(row) + '</td><td class="muted">' + esc(fmtDate(row.open_date)) + '</td></tr>'; }
  function tradeCardsHtml(rows) { /* narrow layout keeps each data-testid="trade-row" target clickable */ return rows.map(tradeRowCard).join(""); }
  function renderTrades() { return visibleRows(); }
  function wireTradeTargets() { Array.prototype.forEach.call(document.querySelectorAll('[data-testid="trade-row"]'), function (el) { function open() { location.hash = "#/trade/" + encodeURIComponent(el.getAttribute("data-ref")); } el.addEventListener("click", open); el.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } }); }); }
  function attentionItems() { var queue = state.data.review_queue || []; var refs = {}; queue.forEach(function (q) { refs[String(q.reference)] = q; }); return Object.keys(refs).map(function (ref) { var row = allRows().find(function (r) { return String(r.reference) === ref; }); return { queue: refs[ref], row: row }; }).filter(function (item) { return state.mode === "all" || (item.row && item.row.mode === state.mode); }); }
  function sparklineHtml() { var series = ((state.data.cumulative_r_by_mode || {})[state.mode]) || []; if (!series.length) return '<div class="sparkline-empty">No governed cumulative series in this mode.</div>'; var w = 900, h = 90, pad = 7, values = series.map(function (p) { return Number(p.cumulative_r); }); var min = Math.min(0, Math.min.apply(Math, values)), max = Math.max(0, Math.max.apply(Math, values)), span = max - min || 1, step = values.length > 1 ? (w - pad * 2) / (values.length - 1) : 0; var points = values.map(function (v, i) { return (pad + i * step).toFixed(1) + "," + (h - pad - ((v - min) / span) * (h - pad * 2)).toFixed(1); }).join(" "); return '<svg class="sparkline" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" role="img" aria-label="Governed cumulative R series"><line x1="0" y1="' + (h - pad - ((0 - min) / span) * (h - pad * 2)).toFixed(1) + '" x2="' + w + '" y2="' + (h - pad - ((0 - min) / span) * (h - pad * 2)).toFixed(1) + '" stroke="rgba(255,255,255,.10)" stroke-dasharray="4 5"/><polyline fill="none" stroke="#a8afb9" stroke-width="1.5" points="' + points + '"/></svg>'; }
  function renderRegistrationCensus() { if (state.mode !== "live" && state.mode !== "all") return ""; var c = state.data.registration_census || {}, sync = state.data.provider_sync_recency || {}, f = c.freshness || {}, status = String(f.status || "unknown").toUpperCase(), counts = c.counts || {}, candidates = c.candidates || [], syncAt = sync.last_successful_sync_at ? "Last successful PropW sync recorded at " + fmtDateTime(sync.last_successful_sync_at) : "Last successful PropW sync time not recorded", content = ""; if (status !== "FRESH") content = '<div class="empty-state"><strong>Candidates withheld · census ' + esc(status.toLowerCase()) + '</strong><span>' + esc(f.detail || "Refresh the provider census before using this list.") + '</span></div>'; else if (!candidates.length) content = '<div class="empty-state"><strong>No pending provider candidates</strong><span>Registered and unresolved identities remain governed by Census.</span></div>'; else content = '<div class="attention-list">' + candidates.map(function (r) { return '<article class="attention-item"><div><div class="attention-top"><span class="semantic-dot"></span><span class="attention-symbol">' + esc(r.instrument || r.open_id || "Provider lifecycle") + '</span><span class="direction-label">' + esc(r.direction || "") + '</span></div><div class="attention-sub">' + esc(r.provider_phase || "UNKNOWN") + ' · ' + esc(r.status || "UNREGISTERED") + '</div><div class="attention-foot"><span>' + esc(fmtDateTime(r.entry_time)) + '</span><span class="attention-status">' + esc(r.provider_lifecycle_key || "") + '</span></div></div></article>'; }).join("") + '</div>'; return '<section class="section"><div class="section-heading"><div><h2>Registration candidates</h2><p>' + esc(c.candidate_count || 0) + ' pending · Census source consistency ' + esc(status) + ' · existing T-IDs ' + esc(Number(counts.REGISTERED || 0) + Number(counts.REGISTERED_INCOMPLETE || 0)) + ' · completeness pending ' + esc(Math.max(0, Number(counts.REGISTERED_INCOMPLETE || 0) - Number(c.legacy_acknowledged_count || 0))) + ' · legacy, Owner words not recoverable ' + esc(c.legacy_acknowledged_count || 0) + '</p></div><span class="muted">read only</span></div>' + content + '<p class="muted">' + esc(syncAt) + '. Census freshness does not assert that PropW is current now.</p><p class="muted">Provider candidates do not receive a T-ID until Owner registration is completed.</p></section>'; }
  function renderHome() { var s = summary(), strategy = strategySummary(), attention = attentionItems(), recent = modeRows().slice().sort(sortRecent).slice(0, 5), openCount = modeRows().filter(isActiveLifecycle).length, last = recent[0]; var page = '<div class="content-wrap"><header class="page-header"><div><div class="eyebrow">Workspace / ' + modeName(state.mode) + '</div><h1 class="page-title">Trading Workspace</h1><p class="page-subtitle">A clear view of what needs attention.</p></div><div class="page-meta">Updated<strong>' + esc(fmtDateTime(state.data.generated_at)) + '</strong></div></header><section class="summary-strip" aria-label="Workspace status"><div class="summary-cell"><div class="summary-label">Needs review</div><div class="summary-value action">' + attention.length + '</div><div class="summary-note">' + (attention.length ? "items need your attention" : "queue is clear") + '</div></div><div class="summary-cell"><div class="summary-label">Open / pending</div><div class="summary-value">' + openCount + '</div><div class="summary-note">current lifecycle records</div></div><div class="summary-cell"><div class="summary-label">Last trade</div><div class="summary-value ' + (last ? resultClass(last.realized_r) : '') + '">' + (last ? esc(fmtR(last.realized_r)) : "—") + '</div><div class="summary-note">' + (last ? esc(last.symbol) : "no recent record") + '</div></div><div class="summary-cell"><div class="summary-label">This week</div><div class="summary-value">—</div><div class="summary-note">weekly series not supplied</div></div></section>';
    page += '<section class="section"><div class="section-heading"><div><h2>Needs your attention</h2><p>' + attention.length + ' item' + (attention.length === 1 ? '' : 's') + '</p></div><a class="section-link" href="#/review">Open review</a></div><div class="attention-list">';
    if (!attention.length) page += '<div class="empty-state"><strong>✓ You\'re caught up</strong><span>No trades need attention right now.</span></div>'; else page += attention.map(function (item) { var r = item.row || {}; return '<article class="attention-item" data-testid="attention-item" data-ref="' + esc(item.queue.reference) + '" tabindex="0"><div><div class="attention-top"><span class="semantic-dot"></span><span class="attention-symbol">' + esc(r.symbol || item.queue.reference) + '</span><span class="direction-label">' + esc(r.direction ? String(r.direction).toUpperCase() : "") + '</span></div><div class="attention-sub">' + esc(r.strategy_display_name || r.strategy_id || "Trade record") + ' · ' + esc(modeName(r.mode)) + '</div><div class="attention-reason">' + esc(item.queue.reason || "Review still required") + '</div><div class="attention-foot"><span>' + esc(fmtDateTime(r.open_date || r.close_date)) + '</span><span class="attention-status">REVIEW PENDING</span></div></div><div class="attention-arrow" aria-hidden="true">→</div></article>'; }).join("");
    page += '</div></section>' + renderRegistrationCensus() + '<section class="section"><div class="section-heading"><div><h2>Recent trades</h2><p>Latest recorded observations</p></div><a class="section-link" href="#/journal">View journal</a></div><div class="trade-list">' + (recent.length ? recent.map(tradeRowCard).join("") : '<div class="empty-state"><strong>No recent trades</strong><span>There are no records in the selected evidence population.</span></div>') + '</div></section><section class="section"><div class="section-heading"><div><h2>Performance snapshot</h2><p>Overall account performance for the selected mode</p></div><span class="muted">' + esc(modeName(state.mode)) + '</span></div><div class="performance-strip"><div class="metric-line"><div class="metric-label">Total R</div><div class="metric-value ' + resultClass(s.total_r) + '">' + esc(fmtR(s.total_r)) + '</div></div><div class="metric-line"><div class="metric-label">Win rate</div><div class="metric-value">' + esc(fmtPct(s.win_rate)) + '</div></div><div class="metric-line"><div class="metric-label">Profit factor</div><div class="metric-value">' + esc(s.profit_factor === null || s.profit_factor === undefined ? "—" : Number(s.profit_factor).toFixed(2)) + '</div></div><div class="metric-line"><div class="metric-label">Avg R</div><div class="metric-value">' + esc(fmtR(s.mean_r)) + '</div></div></div><p class="muted">Overall performance n=<strong>' + esc(s.count === null || s.count === undefined ? "—" : s.count) + '</strong> · named-strategy n=<strong>' + esc(strategy.count === null || strategy.count === undefined ? "—" : strategy.count) + '</strong> · verified provider P&amp;L contributors=<strong>' + esc(s.provider_pnl_observation_count === null || s.provider_pnl_observation_count === undefined ? "—" : s.provider_pnl_observation_count) + '</strong>. Strategy-unresolved / no-match LIVE trades may count overall without entering named-strategy statistics.</p><div class="sparkline-panel"><div class="sparkline-label"><span>Cumulative R</span><span>governed series</span></div>' + sparklineHtml() + '</div></section>' + (state.mode === "all" ? '<p class="combined-warning">LIVE, PAPER, and BACKTEST are separate evidence populations. Combined ALL view is descriptive only — these are not equivalent evidence populations.</p>' : '') + '</div>'; document.getElementById("page-root").innerHTML = page; wireTradeTargets(); wireAttentionTargets(); }
  function wireAttentionTargets() { Array.prototype.forEach.call(document.querySelectorAll('[data-testid="attention-item"]'), function (el) { function open() { location.hash = "#/trade/" + encodeURIComponent(el.getAttribute("data-ref")); } el.addEventListener("click", open); el.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } }); }); }
  function toolbarHtml() { var d = state.data, strategyOptions = '<option value="">Strategy</option>' + (d.filters.strategy || []).map(function (s) { return '<option value="' + esc(s) + '">' + esc(d.strategy_display_names[s] || s) + '</option>'; }).join(""), symbolOptions = '<option value="">Symbol</option>' + (d.filters.symbol || []).map(function (s) { return '<option value="' + esc(s) + '">' + esc(s) + '</option>'; }).join(""); return '<div class="toolbar"><input class="toolbar-search" id="journal-search" type="search" placeholder="Search trades…" aria-label="Search trades" value="' + (state.filters.search ? esc(state.filters.search) : '') + '"><select id="journal-strategy" aria-label="Filter by strategy">' + strategyOptions + '</select><select id="journal-symbol" aria-label="Filter by symbol">' + symbolOptions + '</select><select id="journal-status" aria-label="Filter by status"><option value="">Status</option><option value="reviewed">Reviewed</option><option value="needs_review">Needs review</option></select><span class="toolbar-divider"></span><div class="view-toggle" role="group" aria-label="Journal view"><button type="button" data-display="rows" class="' + (state.display === "rows" ? "active" : "") + '">Rows</button><button type="button" data-display="table" class="' + (state.display === "table" ? "active" : "") + '">Table</button></div><span class="rows-count">' + visibleRows().length + ' shown</span></div>'; }
  function renderJournal() { var rows = visibleRows().slice().sort(sortRecent), page = '<div class="content-wrap"><header class="page-header"><div><div class="eyebrow">Workspace / journal</div><h1 class="page-title">Journal</h1><p class="page-subtitle">' + esc((summary().count || rows.length)) + ' recorded observations</p></div><div class="page-meta">Evidence population<strong>' + esc(modeName(state.mode)) + '</strong></div></header>' + toolbarHtml(); if (state.display === "table") page += '<div class="table-wrap"><table><thead><tr><th>Trade</th><th>Strategy</th><th>Mode</th><th>Result</th><th>Status</th><th>Date</th></tr></thead><tbody>' + (rows.length ? rows.map(tradeTableRow).join("") : '<tr><td colspan="6" class="muted">No trades match the selected filters.</td></tr>') + '</tbody></table></div>'; else page += '<div class="trade-cards">' + (rows.length ? tradeCardsHtml(rows) : '<div class="empty-state"><strong>No trades match the selected filters.</strong><span>Try a broader search or another evidence mode.</span></div>') + '</div>'; document.getElementById("page-root").innerHTML = page; wireTradeTargets(); wireJournalFilters(); }
  function wireJournalFilters() { var search = document.getElementById("journal-search"), strategy = document.getElementById("journal-strategy"), symbol = document.getElementById("journal-symbol"), status = document.getElementById("journal-status"); if (search) { search.addEventListener("input", function () { state.filters.search = search.value; renderJournal(); }); } [strategy, symbol, status].forEach(function (el) { if (!el) return; el.value = state.filters[el.id === "journal-strategy" ? "strategy" : el.id === "journal-symbol" ? "symbol" : "status"]; el.addEventListener("change", function () { state.filters[el.id === "journal-strategy" ? "strategy" : el.id === "journal-symbol" ? "symbol" : "status"] = el.value; renderJournal(); }); }); Array.prototype.forEach.call(document.querySelectorAll('[data-display]'), function (btn) { btn.addEventListener("click", function () { state.display = btn.getAttribute("data-display"); renderJournal(); }); }); }
  function renderReview() { var queue = attentionItems(), reviewed = modeRows().filter(function (r) { return r.review_status === "reviewed"; }).length, incomplete = modeRows().filter(function (r) { return hasIncompleteEvidence(r); }).length, page = '<div class="content-wrap"><header class="page-header"><div><div class="eyebrow">Workspace / review</div><h1 class="page-title">Review</h1><p class="page-subtitle">A focused inbox for evidence and post-trade review.</p></div><div class="page-meta">Queue<strong>' + queue.length + ' pending</strong></div></header><div class="review-summary"><div class="review-stat"><div class="review-stat-label">Needs review</div><div class="review-stat-value">' + queue.length + '</div></div><div class="review-stat"><div class="review-stat-label">Evidence incomplete</div><div class="review-stat-value">' + incomplete + '</div></div><div class="review-stat"><div class="review-stat-label">Reviewed observations</div><div class="review-stat-value">' + reviewed + '</div></div></div><section class="section"><div class="section-heading"><div><h2>Needs your attention</h2><p>Open a trade to read the full story.</p></div></div><div class="review-list">'; if (!queue.length) page += '<div class="empty-state"><strong>✓ Review queue clear</strong><span>All current trades have completed their required review steps.</span></div>'; else page += queue.map(function (item) { var r = item.row || {}; return '<article class="review-item"><div><div class="review-item-title">' + esc(r.symbol || item.queue.reference) + ' <span class="direction-label">' + esc(r.direction ? String(r.direction).toUpperCase() : "") + '</span></div><div class="review-item-meta">' + esc(r.strategy_display_name || r.strategy_id || "Trade record") + ' · ' + esc(modeName(r.mode)) + ' · ' + esc(fmtDateTime(r.open_date || r.close_date)) + '</div><div class="review-item-reason">' + esc(item.queue.reason || "Review missing") + '</div></div><button type="button" class="open-trade" data-ref="' + esc(item.queue.reference) + '">Open trade →</button></article>'; }).join(""); page += '</div></section></div>'; document.getElementById("page-root").innerHTML = page; Array.prototype.forEach.call(document.querySelectorAll(".open-trade"), function (btn) { btn.addEventListener("click", function () { location.hash = "#/trade/" + encodeURIComponent(btn.getAttribute("data-ref")); }); }); }
  function healthR(h) { return state.mode === "all" && h.performance_summary ? h.performance_summary.mean_r : null; }
  function healthN(h) { return state.mode === "live" ? h.live_n : state.mode === "backtest" ? h.backtest_n : h.sample_n; }
  function healthState(h) { return h.health_state || h.evidence_class || "not_observable"; }
  function modeObservability(h) { if (state.mode === "live") return (h.operational_observability && h.operational_observability.status) || (h.rule_observability && h.rule_observability.operational_status) || healthState(h); if (state.mode === "backtest") return (h.rule_observability && h.rule_observability.historical_status) || healthState(h); return h.combined_observability_state || healthState(h); }
  function uniqueLabels(values) { return values.filter(function (value, index) { return value && values.indexOf(value) === index; }); }
  function researchReadiness(health) { var candidates = []; health.forEach(function (h) { (h.improvement_candidates || []).forEach(function (candidate) { candidates.push(candidate); }); }); var states = uniqueLabels(health.map(function (h) { return h.candidate_selection_state; })); if (candidates.length === 1) return { title: "One candidate ready for research review", detail: "The governed health read model exposes one evidence-gated candidate; no selection or backtest is implied." }; if (candidates.length > 1 || states.indexOf("multiple_candidates_require_review") >= 0) return { title: "Multiple candidates require review", detail: "The governed health read model exposes multiple candidates; no candidate is selected automatically." }; if (states.indexOf("single_candidate") >= 0) return { title: "Candidate state requires review", detail: "The health read model reports a candidate state, but no complete candidate payload is available in this view." }; return { title: "No eligible candidate", detail: "The governed health read model currently reports no evidence-gated research candidate." }; }
  function renderInsights() { var d = state.data, s = summary(), health = (d.health || []).slice().sort(function (a, b) { return Number(healthN(b) || 0) - Number(healthN(a) || 0); }), readiness = researchReadiness(health), states = uniqueLabels(health.map(function (h) { return healthState(h); })), observability = uniqueLabels(health.map(function (h) { return modeObservability(h); })), chronology = s.chronology_quality && s.chronology_quality.true_execution_chronology_observable === true ? "Observable" : "Not observable", page = '<div class="content-wrap"><header class="page-header"><div><div class="eyebrow">Workspace / insights</div><h1 class="page-title">Insights</h1><p class="page-subtitle">Patterns supported by your recorded evidence.</p></div><div class="page-meta">Analytics source<strong>governed health read model</strong></div></header><div class="insight-grid"><section class="insight-panel"><h3>Strategy overview</h3><p>Observed cohorts stay separate from research claims.</p><div class="strategy-list">' + health.map(function (h) { var hr = healthR(h); return '<div class="strategy-line"><span class="strategy-line-name">' + esc(d.strategy_display_names[h.strategy_id] || h.strategy_id) + '</span><span class="strategy-line-n">' + esc(healthN(h) || 0) + ' observations · ' + esc(healthState(h)) + '</span><span class="strategy-line-r ' + resultClass(hr) + '">' + esc(fmtR(hr)) + '</span></div>'; }).join("") + '</div></section><section class="insight-panel"><h3>Evidence quality</h3><p>Current health fields only; no browser-side scoring.</p><div class="quality-list"><div class="quality-line"><span>Reviewed observations</span><span>' + esc(fmtPct(s.review_completion_rate)) + '</span></div><div class="quality-line"><span>Health state</span><span>' + esc(states.join(" · ") || "—") + '</span></div><div class="quality-line"><span>Method observability</span><span>' + esc(observability.join(" · ") || "—") + '</span></div><div class="quality-line"><span>Verified provider P&amp;L</span><span>' + esc(s.provider_pnl_observation_count === null || s.provider_pnl_observation_count === undefined ? "—" : String(s.provider_pnl_observation_count) + " records") + '</span></div><div class="quality-line"><span>Execution chronology</span><span>' + esc(chronology) + '</span></div></div></section><section class="insight-panel"><h3>Research readiness</h3><p>Read directly from governed health candidate state.</p><div class="readiness"><strong>' + esc(readiness.title) + '</strong><p>' + esc(readiness.detail) + '</p></div></section><section class="insight-panel"><h3>Mode boundary</h3><p>Use the selector above to inspect each evidence population independently.</p><div class="readiness"><strong>' + esc(modeName(state.mode)) + ' view</strong><p>LIVE and BACKTEST remain separate evidence populations. ALL is descriptive context, not a new combined authority.</p></div></section></div></div>'; document.getElementById("page-root").innerHTML = page; }
  function evidenceEmptyOr(items, label) { if (!items || !items.length) return '<div class="evidence-empty">No ' + label + ' were preserved for this trade.</div>'; return null; }
  function galleryHtml(items) { return '<div class="evidence-gallery">' + items.map(function (it) { return '<figure data-lightbox="' + esc(it.href) + '"><img loading="lazy" src="' + esc(it.href) + '" alt="' + esc((it.categories || []).join(", ")) + '"><figcaption>' + esc((it.categories || []).join(", ")) + '</figcaption></figure>'; }).join("") + '</div>'; }
  function evidenceTabItems(row, tab) { var gallery = row.evidence_gallery || []; if (tab === "execution") return gallery.filter(function (g) { return g.kind === "provider_execution"; }); if (tab === "exit") return gallery.filter(function (g) { return (g.categories || []).indexOf("Exit") >= 0; }); return gallery.filter(function (g) { return g.kind === "chart_context"; }); }
  function renderEvidence(row) { var gallery = row.evidence_gallery || [], chart = evidenceTabItems(row, "chart"), execution = evidenceTabItems(row, "execution"), exit = evidenceTabItems(row, "exit"), tabs = []; if (chart.length) tabs.push(["chart", "Chart"]); if (execution.length) tabs.push(["execution", "Execution receipt"]); if (exit.length) tabs.push(["exit", "Exit"]); if (!tabs.length) return '<div class="detail-surface"><div class="detail-surface-title"><h2>Evidence</h2><span>preserved artifacts</span></div>' + (evidenceEmptyOr([], "chart or execution evidence") || "") + '</div>'; if (!tabs.some(function (t) { return t[0] === state.evidenceTab; })) state.evidenceTab = tabs[0][0]; var items = evidenceTabItems(row, state.evidenceTab), primary = items[0]; return '<div class="detail-surface"><div class="detail-surface-title"><h2>Evidence</h2><span>chart and execution stay distinct</span></div><div class="evidence-tabs" role="tablist">' + tabs.map(function (tab) { return '<button type="button" class="evidence-tab ' + (state.evidenceTab === tab[0] ? "active" : "") + '" data-evidence-tab="' + tab[0] + '" role="tab">' + tab[1] + '</button>'; }).join("") + '</div><div class="evidence-frame">' + (primary ? '<img src="' + esc(primary.href) + '" alt="' + esc((primary.categories || []).join(", ")) + ' evidence" data-lightbox="' + esc(primary.href) + '">' : '<div class="evidence-empty">No ' + esc(state.evidenceTab) + ' artifact was preserved for this trade.</div>') + '</div>' + (primary ? '<div class="evidence-caption">' + esc((primary.categories || []).join(" · ")) + ' · preserved evidence' + (state.evidenceTab === "execution" ? ' · provider evidence is not chart authority' : '') + '</div>' : '') + (items.length > 1 ? galleryHtml(items) : '') + '</div>'; }
  function comparisonValue(v) { return v === null || v === undefined || v === "" ? '<span class="muted">—</span>' : esc(v); }
  function timelineNode(label, time, title, detail) { return '<div class="timeline-node"><div><span class="timeline-kicker">' + esc(label) + '</span><span class="timeline-time">' + esc(time || "") + '</span></div><div class="timeline-title">' + esc(title) + '</div>' + (detail ? '<div class="timeline-detail">' + esc(detail) + '</div>' : '') + '</div>'; }
  function renderTimeline(row) { var p = row.plan || {}, e = row.execution || {}, rv = row.review || {}, nodes = []; if (p.entry !== null || p.stop_loss !== null || p.take_profit !== null || p.thesis) nodes.push(timelineNode("Plan", row.open_date, p.thesis || "Recorded plan", [p.entry !== null ? "Entry " + p.entry : null, p.stop_loss !== null ? "Stop " + p.stop_loss : null, p.take_profit !== null ? "Target " + p.take_profit : null].filter(Boolean).join(" · "))); if (e.actual_entry !== null || row.entry !== null) nodes.push(timelineNode("Entry", e.entry_verification && e.entry_verification.occurred_at, e.actual_entry !== null ? String(e.actual_entry) : "Entry recorded", e.provider ? "Provider verified · " + e.provider : "Recorded entry")); if (rv.execution_deviation && rv.execution_deviation !== "not_observable") nodes.push(timelineNode("Management", "", rv.execution_deviation, "Review context")); if (e.actual_exit !== null || row.exit !== null || row.close_date) nodes.push(timelineNode("Exit", row.close_date, e.actual_exit !== null ? String(e.actual_exit) : "Exit recorded", row.provider_pnl_verified ? "Provider P&L verified" : "Exit evidence preserved")); if (rv.status && rv.status !== "not_observable") nodes.push(timelineNode("Review", rv.reviewed_at, rv.status === "reviewed" ? "Review completed" : "Review pending", rv.owner_notes || rv.what_went_well || "Review context recorded")); return '<div class="timeline">' + (nodes.length ? nodes.join("") : '<div class="muted">Timeline details not recorded.</div>') + '</div>'; }
  function renderDetail(ref) { var row = allRows().find(function (r) { return String(r.reference) === String(ref); }); if (!row) { document.getElementById("page-root").innerHTML = '<div class="content-wrap"><a class="detail-back" href="#/journal">← Back to Journal</a><div class="error-state">Trade detail not found in the current read model.</div></div>'; return; } var p = row.plan || {}, e = row.execution || {}, rv = row.review || {}, advanced = row.advanced || {}, reviewStatus = (isInactiveStrategy(row) || isHistoricalLink(row)) ? statusLabel(row) : (row.review_status === "reviewed" ? "REVIEWED" : statusLabel(row)), page = '<div class="content-wrap"><a class="detail-back" href="#/journal">← Journal</a><header class="detail-header"><div class="detail-identity"><div class="eyebrow">Trade story / ' + esc(modeName(row.mode)) + '</div><h1>' + esc(row.symbol) + '</h1><div class="detail-identity-sub">' + esc(directionLabel(row)) + ' · ' + esc(strategyName(row)) + ' · ' + esc(modeName(row.mode)) + '</div><div class="detail-identity-meta">' + esc(fmtDateTime(row.open_date)) + (row.close_date ? ' → ' + esc(fmtDateTime(row.close_date)) : '') + '</div></div><div class="detail-result">' + resultHtml(row, false) + '<div class="detail-pnl">' + esc(fmtMoney(row.provider_net_pnl_complete ? row.provider_net_pnl : null)) + '</div>' + (row.provider_net_pnl_complete ? '<div class="detail-status">after fees · close ' + esc(fmtMoney(row.provider_pnl)) + ' · open fee −' + esc(Number(row.provider_open_fee).toFixed(2)) + ' · funding ' + esc(fmtMoney(row.provider_funding_fee)) + (row.provider_funding_complete ? '' : ' (a settlement has no funding bill; funding is incomplete)') + '</div>' : (row.provider_pnl_verified ? '<div class="detail-status">close P&amp;L ' + esc(fmtMoney(row.provider_pnl)) + ' · fees not available</div>' : '')) + '<div class="detail-status">' + esc(reviewStatus) + '</div></div></header>' + (row.plan_corrected_after_review ? '<div class="detail-surface" data-testid="plan-corrected-after-review"><div class="detail-surface-title"><h2>Plan corrected after review</h2><span>the review below was written against the earlier plan and R</span></div></div>' : '') + renderEvidence(row) + '<section class="detail-surface"><div class="detail-surface-title"><h2>Planned vs Actual</h2><span>unknown stays unknown</span></div><table class="comparison"><thead><tr><th></th><th>Planned</th><th>Actual</th></tr></thead><tbody><tr><td>Entry</td><td>' + comparisonValue(p.entry) + '</td><td>' + comparisonValue(e.actual_entry) + '</td></tr><tr><td>Stop</td><td>' + comparisonValue(p.stop_loss) + '</td><td>' + comparisonValue(null) + '</td></tr><tr><td>Target</td><td>' + comparisonValue(p.take_profit) + '</td><td>' + comparisonValue(e.actual_exit) + '</td></tr><tr><td>R</td><td>' + comparisonValue(p.planned_rr) + '</td><td>' + comparisonValue(row.realized_r === null || row.realized_r === undefined ? null : fmtPlainR(row.realized_r)) + '</td></tr><tr><td>Exit</td><td>' + comparisonValue(p.take_profit !== null ? "Planned target" : null) + '</td><td>' + comparisonValue(e.actual_exit !== null ? "Recorded exit" : null) + '</td></tr></tbody></table></section><section class="detail-surface"><div class="detail-surface-title"><h2>Trade timeline</h2><span>plan → execution → outcome → review</span></div>' + renderTimeline(row) + '</section><section class="detail-surface"><div class="detail-surface-title"><h2>Review</h2><span>method and execution stay separate</span></div><div class="triptych"><div id="review-method" class="triptych-column"><div class="triptych-label">Method</div><div class="triptych-value">' + esc(rv.rule_compliance || rv.thesis_validity || "Rule observability: not recorded") + '</div><div class="triptych-note">' + esc(rv.diagnostic_class || "No method diagnosis recorded.") + '</div></div><div id="review-execution" class="triptych-column"><div class="triptych-label">Execution</div><div class="triptych-value">' + esc(rv.execution_deviation || rv.entry_quality || "Execution detail not recorded.") + '</div><div class="triptych-note">' + esc(rv.exit_quality || (e.provider ? "Provider evidence preserved." : "No execution conclusion recorded.")) + '</div></div><div class="triptych-column"><div class="triptych-label">Learning</div><div class="triptych-value">' + esc(rv.owner_notes || rv.what_went_well || "No reviewed learning recorded.") + '</div><div class="triptych-note">' + esc(rv.what_went_wrong || "Preserve the recorded context.") + '</div></div></div></section><details class="provenance"><summary>Advanced details</summary><div class="provenance-body"><span>Trade ID</span><code>' + esc(advanced.trade_id) + '</code><span>Observation ID</span><code>' + esc(advanced.observation_id) + '</code><span>Source path</span><code>' + esc(advanced.source_path) + '</code><span>Rule version</span><code>' + esc(advanced.rule_version) + '</code><span>Evidence refs</span><code>' + esc(advanced.evidence_ref) + '</code><span>Provenance</span><code>' + esc(advanced.source_kind) + ' · ' + esc(advanced.raw_status) + '</code><span>Evidence hashes</span><code>' + esc((advanced.evidence_sha256 || []).join(", ")) + '</code></div></details></div>'; document.getElementById("page-root").innerHTML = page; wireDetail(); }
  function wireDetail() { Array.prototype.forEach.call(document.querySelectorAll("[data-evidence-tab]"), function (btn) { btn.addEventListener("click", function () { state.evidenceTab = btn.getAttribute("data-evidence-tab"); var ref = decodeURIComponent(location.hash.split("/").slice(2).join("/")); renderDetail(ref); }); }); Array.prototype.forEach.call(document.querySelectorAll("[data-lightbox]"), function (el) { el.addEventListener("click", function () { var lb = document.getElementById("lightbox"), img = document.getElementById("lightbox-img"); img.src = el.getAttribute("data-lightbox"); lb.classList.add("open"); }); }); }
  function parseHash() { var parts = location.hash.replace(/^#\//, "").split("/"); if (parts[0] === "trade" && parts[1]) return { view: "trade", ref: decodeURIComponent(parts.slice(1).join("/")) }; return { view: ["home", "journal", "review", "insights"].indexOf(parts[0]) >= 0 ? parts[0] : "home" }; }
  function renderAll() { var route = parseHash(); state.view = route.view; updateNav(); renderModeSelector(); if (route.view === "trade") renderDetail(route.ref); else if (route.view === "journal") renderJournal(); else if (route.view === "review") renderReview(); else if (route.view === "insights") renderInsights(); else renderHome(); }
  document.getElementById("app").innerHTML = shell();
  document.getElementById("lightbox").addEventListener("click", function () { this.classList.remove("open"); });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape") document.getElementById("lightbox").classList.remove("open"); });
  window.addEventListener("hashchange", renderAll);
  fetch("trade-view-data.json").then(function (response) { if (!response.ok) throw new Error("read model unavailable"); return response.json(); }).then(function (data) { state.data = data; renderAll(); }).catch(function (error) { document.getElementById("page-root").innerHTML = '<div class="content-wrap"><div class="error-state">Unable to load the local Trade View read model: ' + esc(error.message) + '</div></div>'; });
})();
"""


# Evidence category -> human-facing gallery grouping (spec section 10).
# Purely a presentation label; the underlying "category"/"type" values are
# still carried through verbatim in "evidence_gallery" items and in Advanced
# Details, so nothing here can silently change countability or provenance.
_EVIDENCE_CATEGORY_LABEL = {
    "pre_trade_context": "Setup / Chart",
    "entry": "Entry",
    "exit": "Exit",
    "review": "Review",
}
# Presentation role priority when the same binary carries more than one
# logical evidence_role tag (see _evidence_gallery below): a genuinely
# governed-provider-bound artifact must never be downgraded to ordinary
# chart evidence just because it was *also* attached under a chart-shaped
# category.
_ROLE_PRIORITY = {"provider_execution": 2, "chart_context": 1, "other": 0}


def _evidence_gallery(evidence_artifacts: list[Mapping[str, Any]], evidence_href_by_binding: Mapping[tuple[str, Any], str]) -> list[dict[str, Any]]:
    """Group raw evidence_artifacts by locator so one binary is never shown twice.

    A single durable artifact (one sha256-addressed file) may be attached
    under more than one logical evidence link (e.g. the same chart used for
    both a category tag and a duplicate attach call); spec section 10
    forbids rendering that binary twice. Grouping by locator collapses those
    into one gallery entry that still lists every category it was tagged
    with.

    "kind" (provider_execution / chart_context / other) is read directly off
    the governed "evidence_role" field computed in
    operational_analytics.py -- never re-derived here from a "type" string
    (review blocker #2, issue #44 / PR #45): Trade View only presents an
    already-governed classification, it does not guess one.
    """

    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in evidence_artifacts:
        if not isinstance(item, Mapping):
            continue
        locator = item.get("locator")
        if not isinstance(locator, str):
            continue
        href = evidence_href_by_binding.get((locator, item.get("sha256")))
        if not isinstance(locator, str) or href is None:
            continue
        role = item.get("evidence_role") if item.get("evidence_role") in _ROLE_PRIORITY else "other"
        if locator not in groups:
            groups[locator] = {
                "href": href,
                "kind": role,
                "categories": [],
                "media_type": item.get("media_type"),
                "original_filename": item.get("original_filename"),
            }
            order.append(locator)
        entry = groups[locator]
        if _ROLE_PRIORITY[role] > _ROLE_PRIORITY[entry["kind"]]:
            entry["kind"] = role
        label = _EVIDENCE_CATEGORY_LABEL.get(item.get("category"), item.get("category") or "evidence")
        if label not in entry["categories"]:
            entry["categories"].append(label)
    return [groups[locator] for locator in order]


def registration_census_read_model(root: Path) -> dict[str, Any]:
    """Return a read-only Census slice, withholding candidates unless fresh."""

    try:
        census = build_census(root, "propw")
    except FileNotFoundError:
        return {
            "schema_version": "trade-view-registration-census.v1",
            "provider": "propw",
            "freshness": {"status": "unknown", "detail": "PROVIDER_LEDGER_MISSING"},
            "source_digests": {},
            "source_identity_sha256": hashlib.sha256(b"{}").hexdigest(),
            "total": 0,
            "counts": {},
            "registered_identity_count": 0,
            "legacy_acknowledged_count": 0,
            "candidate_count": 0,
            "candidates": [],
        }

    source_digests = census.get("source_digests", {})
    source_identity = json.dumps(source_digests, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    freshness = census.get("freshness", {"status": "unknown", "detail": "FRESHNESS_UNAVAILABLE"})
    selected = select_candidates(census, phase="ALL") if freshness.get("status") == "fresh" else []
    candidates = [
        {
            "provider_lifecycle_key": row.get("provider_lifecycle_key"),
            "open_id": row.get("open_id"),
            "instrument": row.get("instrument"),
            "direction": row.get("direction"),
            "entry_time": row.get("entry_time"),
            "provider_phase": row.get("provider_phase"),
            "status": row.get("status"),
        }
        for row in selected
    ]
    return {
        "schema_version": "trade-view-registration-census.v1",
        "provider": census.get("provider", "propw"),
        "generated_at": census.get("generated_at"),
        "freshness": freshness,
        "source_digests": source_digests,
        "source_identity_sha256": hashlib.sha256(source_identity).hexdigest(),
        "total": census.get("total", 0),
        "counts": census.get("counts", {}),
        "registered_identity_count": census.get("registered_identity_count", 0),
        "legacy_acknowledged_count": census.get("legacy_acknowledged_count", 0),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def _view_row(row: Mapping[str, Any], *, evidence_href_by_binding: Mapping[tuple[str, Any], str] | None = None, strategy_display_names: Mapping[str, str] | None = None) -> dict[str, Any]:
    evidence_href_by_binding = evidence_href_by_binding or {}
    strategy_display_names = strategy_display_names or {}
    raw_evidence = [item for item in (row.get("evidence_artifacts") or []) if isinstance(item, Mapping)]
    evidence_links = [
        {
            "category": item.get("category"),
            "href": evidence_href_by_binding.get((item.get("locator"), item.get("sha256"))),
            "media_type": item.get("media_type"),
            "original_filename": item.get("original_filename"),
        }
        for item in raw_evidence
        if (item.get("locator"), item.get("sha256")) in evidence_href_by_binding
    ]
    strategy_id = row.get("strategy_id")
    lifecycle_state = _lifecycle_state(row)
    entry_verification = row.get("entry_verification") if isinstance(row.get("entry_verification"), Mapping) else None
    exit_verification = row.get("exit_verification") if isinstance(row.get("exit_verification"), Mapping) else None
    return {
        "open_date": row.get("event_date"),
        "close_date": row.get("closed_at"),
        "symbol": row.get("symbol") or row.get("instrument"),
        "mode": row.get("sample_origin") or row.get("mode"),
        "status": row.get("status"),
        "lifecycle_state": lifecycle_state,
        "strategy_id": strategy_id,
        "strategy_display_name": (
            "No matching strategy"
            if row.get("strategy_resolution_status") == "no_match"
            else "Strategy unresolved"
            if row.get("strategy_resolution_status") == "unresolved"
            else (strategy_display_names.get(str(strategy_id)) if strategy_id else None)
        ),
        "strategy_resolution_status": row.get("strategy_resolution_status"),
        "performance_eligible": row.get("performance_eligible"),
        "r_performance_eligible": row.get("r_performance_eligible"),
        "realized_r_authority_status": row.get("realized_r_authority_status"),
        "strategy_stats_eligible": row.get("strategy_stats_eligible"),
        "rule_version": row.get("rule_version"),
        "direction": row.get("direction"),
        "entry": row.get("entry_price") or row.get("entry"),
        "exit": row.get("exit_price") or row.get("exit"),
        "planned_rr": row.get("planned_rr"),
        "plan_corrected_after_review": row.get("plan_corrected_after_review") is True,
        "realized_r": row.get("realized_r"),
        "outcome": row.get("outcome"),
        "provider_pnl": row.get("provider_pnl"),
        "provider_pnl_verified": row.get("provider_pnl_verified"),
        "provider_net_pnl": row.get("provider_net_pnl"),
        "provider_net_pnl_complete": row.get("provider_net_pnl_complete") is True,
        "provider_open_fee": row.get("provider_open_fee"),
        "provider_funding_fee": row.get("provider_funding_fee"),
        "provider_funding_complete": row.get("provider_funding_complete") is True,
        "review_status": row.get("review_status", "not_observable"),
        "rule_compliance": row.get("rule_compliance") or "not_observable",
        "evidence_completeness": row.get("evidence_completeness", "not_observable"),
        "reference": row.get("trade_id") or row.get("observation_id"),
        # Backtest audit fields (source_method is backtest-only by
        # vocabularies.SOURCE_METHODS); recording_timing applies to both
        # live and backtest rows. Presentation-only passthrough.
        "source_method": row.get("source_method"),
        "outcome_known_at_recording": row.get("outcome_known_at_recording"),
        "recording_timing": row.get("recording_timing"),
        "evidence_links": evidence_links,
        "evidence_gallery": _evidence_gallery(raw_evidence, evidence_href_by_binding),
        # Correction pass #2, C7: expose whether a row counts toward
        # analytics and why not, so a recorded-but-not-counted BACKTEST row
        # (e.g. evidence_quality=insufficient/owner_declared_unverified) is
        # still explainable in Trade View instead of silently vanishing.
        "counted": row.get("counted"),
        "exclusion_reason": row.get("exclusion_reason"),
        "evidence_quality": row.get("evidence_quality"),
        # --- Trade View V2 additive fields (section 20 minimum read-model
        # exposure): plan / execution / review detail for Trade Detail. All
        # already existed on canonical records or lifecycle/review payloads;
        # this is presentation passthrough only.
        "plan": {
            "entry": row.get("planned_entry"),
            "stop_loss": row.get("planned_stop_loss"),
            "take_profit": row.get("planned_take_profit"),
            "planned_rr": row.get("planned_rr"),
            "timeframe_context": row.get("timeframe_context"),
            "timeframe_trigger": row.get("timeframe_trigger"),
            "market_context": row.get("market_context"),
            "thesis": row.get("thesis"),
            "setup_tags": row.get("setup_tags"),
            "confirmation_tags": row.get("confirmation_tags"),
        },
        "execution": {
            "provider": row.get("provider"),
            "entry_verification": entry_verification,
            "exit_verification": exit_verification,
            "actual_entry": row.get("entry_price") or row.get("entry"),
            "actual_exit": row.get("exit_price") or row.get("exit"),
            "provider_pnl": row.get("provider_pnl"),
            "provider_pnl_verified": row.get("provider_pnl_verified"),
        },
        "review": {
            "status": row.get("review_status", "not_observable"),
            "diagnostic_class": row.get("diagnostic_class"),
            "rule_compliance": row.get("rule_compliance"),
            "execution_deviation": row.get("execution_deviation"),
            "thesis_validity": row.get("review_thesis_validity"),
            "entry_quality": row.get("review_entry_quality"),
            "exit_quality": row.get("review_exit_quality"),
            "what_went_well": row.get("review_what_went_well"),
            "what_went_wrong": row.get("review_what_went_wrong"),
            "owner_notes": row.get("review_owner_notes"),
            "evidence_completeness": row.get("evidence_completeness"),
        },
        "advanced": {
            "trade_id": row.get("trade_id"),
            "trade_no": row.get("trade_no"),
            "observation_id": row.get("observation_id"),
            "canonical_episode_id": row.get("canonical_episode_id"),
            "source_kind": row.get("source_kind"),
            "source_path": row.get("source_path"),
            "evidence_ref": row.get("evidence_ref"),
            "recorded_at": row.get("recorded_at"),
            "occurred_at": row.get("event_date"),
            "raw_status": row.get("status"),
            "lifecycle_state": lifecycle_state,
            "source_method": row.get("source_method"),
            "outcome_known_at_recording": row.get("outcome_known_at_recording"),
            "evidence_quality": row.get("evidence_quality"),
            "counted": row.get("counted"),
            "performance_eligible": row.get("performance_eligible"),
            "r_performance_eligible": row.get("r_performance_eligible"),
            "realized_r_authority_status": row.get("realized_r_authority_status"),
            "strategy_stats_eligible": row.get("strategy_stats_eligible"),
            "exclusion_reason": row.get("exclusion_reason"),
            "rule_version": row.get("rule_version"),
            "evidence_sha256": sorted({item.get("locator", "").split(":", 1)[-1].split(".", 1)[0] for item in raw_evidence if isinstance(item.get("locator"), str) and item.get("locator", "").startswith("artifact:")}),
        },
    }


def build_trade_view(root: Path, *, output_dir: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    analytics = load_current(root)
    health = load_current_health(root)
    output = output_dir or DEFAULT_OUTPUT
    output = (root / output if not output.is_absolute() else output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    evidence_dir = root / "canonical" / "evidence" / "artifacts"
    evidence_store = EvidenceStore(root)
    # A locator such as "artifact:<sha256><ext>" maps 1:1 to a file already
    # durably stored by evidence_store.EvidenceStore.ingest. Trade View never
    # inlines these bytes (they may be private images); it only offers a
    # link into the safe "/evidence/<sha256><ext>" route served by the
    # handler in make_trade_view_handler -- a relative filesystem path would
    # not resolve through the local HTTP server, which cannot traverse above
    # its own document root (this output directory) to reach
    # canonical/evidence/artifacts/. If Trade View's index.html is instead
    # opened directly as a file:// URL (no server), this root-relative route
    # simply will not resolve -- links degrade to non-functional rather than
    # breaking anything, which is acceptable for that unsupported mode.
    def _evidence_href(locator: str, expected_sha256: Any) -> str | None:
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in expected_sha256)
        ):
            return None
        try:
            evidence_store.read(locator, expected_sha256=expected_sha256)
        except EvidenceStoreError:
            return None
        name = locator.split(":", 1)[1]
        return f"/evidence/{name}"
    all_bindings = {
        (item.get("locator"), item.get("sha256"))
        for row in (*analytics.get("observations", []), *analytics.get("canonical_read_model", []))
        for item in (row.get("evidence_artifacts") or [])
        if (
            isinstance(item, Mapping)
            and isinstance(item.get("locator"), str)
            and isinstance(item.get("sha256"), str)
            and len(item.get("sha256")) == 64
            and all(char in "0123456789abcdefABCDEF" for char in item.get("sha256"))
        )
    }
    evidence_href_by_binding = {
        binding: href
        for binding in all_bindings
        if (href := _evidence_href(*binding)) is not None
    }
    registry = load_registry(root)
    strategies = [item for item in registry.get("strategies", []) if isinstance(item, Mapping)]
    # Registry display_name is the authoritative owner-facing strategy label
    # (registry/strategy-registry.yaml); Trade View only looks it up, it
    # never invents or alters strategy identity (spec section 7).
    strategy_display_names = {
        str(item.get("strategy_id")): str(item.get("display_name"))
        for item in strategies
        if item.get("strategy_id") and item.get("display_name")
    }
    rows = [_view_row(row, evidence_href_by_binding=evidence_href_by_binding, strategy_display_names=strategy_display_names) for row in analytics.get("observations", [])]
    # Keep non-countable canonical records visible to the owner so the UI can
    # explain why a row needs review; countable canonical records are already
    # present in observations and must not be appended a second time.
    # A countable record leaves observations when its R is not qualified (for
    # example an unknown original stop), so skip only rows actually present.
    observed_refs = {
        ref for row in analytics.get("observations", [])
        for ref in (row.get("trade_id"), row.get("trade_no")) if ref
    }
    for row in analytics.get("canonical_read_model", []):
        if row.get("counted") is True and (
            row.get("trade_id") in observed_refs or row.get("trade_no") in observed_refs
        ):
            continue
        item = _view_row(row, evidence_href_by_binding=evidence_href_by_binding, strategy_display_names=strategy_display_names)
        item["status"] = row.get("status")
        item["reference"] = row.get("trade_id") or row.get("trade_no")
        rows.append(item)
    queue = []
    queued_refs: set[str] = set()

    def _queue_row(row: Mapping[str, Any], reason: str) -> None:
        reference = row.get("trade_id") or row.get("observation_id") or row.get("trade_no") or "canonical-record"
        if not isinstance(reference, str) or reference in queued_refs:
            return
        queued_refs.add(reference)
        queue.append({"reference": reference, "reason": reason})

    for row in analytics.get("canonical_read_model", []):
        if _needs_review(row):
            _queue_row(row, row.get("reason") or row.get("exclusion_reason") or "canonical record needs review")
    for row in analytics.get("observations", []):
        if row.get("source_kind") == "post_cutover_operational" and _needs_review(row):
            _queue_row(row, "closed operational trade has no completed review")
    data = {
        "schema_version": TRADE_VIEW_SCHEMA,
        "generated_at": analytics.get("generated_at"),
        "sources": {"analytics_artifact_digest": analytics.get("artifact_digest"), "health_artifact_digest": health.get("artifact_digest"), "canonical_projection": "canonical/lifecycle/current.json"},
        "filters": {"strategy": sorted({str(item.get("strategy_id")) for item in strategies if item.get("strategy_id")}), "symbol": sorted({str(row.get("symbol")) for row in rows if row.get("symbol")}), "status": sorted({str(row.get("status")) for row in rows if row.get("status")})},
        "strategy_display_names": strategy_display_names,
        "summary": analytics.get("metrics", {}).get("global", {}),
        "summary_by_mode": analytics.get("summary_by_mode", {}),
        "strategy_summary_by_mode": analytics.get("strategy_summary_by_mode", {}),
        "named_strategy_summary_by_mode": analytics.get("named_strategy_summary_by_mode", {}),
        # Governed in operational_analytics.py (_cumulative_r_by_mode); Trade
        # View passes it through byte-for-byte and performs no arithmetic on
        # realized_r itself (review blocker #1, issue #44 / PR #45).
    "cumulative_r_by_mode": analytics.get("cumulative_r_by_mode", {"live": [], "paper": [], "backtest": [], "all": []}),
        "rows": rows,
        "health": health.get("strategies", []),
        "review_queue": queue,
        "registration_census": registration_census_read_model(root),
        "provider_sync_recency": read_provider_sync_recency(root, "propw"),
        "guardrails": {"read_only_presentation": True, "performance_recalculation": False, "health_recalculation": False, "deployment": False},
    }
    data["artifact_digest"] = _digest(data)
    (output / "trade-view-data.json").write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    (output / "index.html").write_text(HTML_V3, encoding="utf-8", newline="\n")
    (output / "style.css").write_text(STYLE_CSS_V3, encoding="utf-8", newline="\n")
    (output / "app.js").write_text(APP_JS_V3, encoding="utf-8", newline="\n")
    manifest = {
        "schema_version": "trade-view-manifest.v1",
        "data_path": "exports/operational/trade-view/trade-view-data.json",
        "html_path": "exports/operational/trade-view/index.html",
        "style_path": "exports/operational/trade-view/style.css",
        "app_path": "exports/operational/trade-view/app.js",
        "data_sha256": hashlib.sha256((output / "trade-view-data.json").read_bytes()).hexdigest(),
        "html_sha256": hashlib.sha256((output / "index.html").read_bytes()).hexdigest(),
        "style_sha256": hashlib.sha256((output / "style.css").read_bytes()).hexdigest(),
        "app_sha256": hashlib.sha256((output / "app.js").read_bytes()).hexdigest(),
        "health_artifact_digest": health.get("artifact_digest"),
        "analytics_artifact_digest": analytics.get("artifact_digest"),
        "registration_census_source_identity_sha256": data["registration_census"]["source_identity_sha256"],
        "registration_census_freshness": data["registration_census"]["freshness"].get("status"),
        "local_only": True,
        "deployment": False,
    }
    manifest["manifest_digest"] = _digest(manifest)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest
