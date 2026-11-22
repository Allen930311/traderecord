# Strategy registry

`strategy-registry.yaml` is the stable catalogue of trading strategies. Schema
`strategy-registry.v1`, validated by `trading_os/registry.py` +
`trading_os/contracts.py::validate_strategy`.

## Fields

| field | required | notes |
|---|---|---|
| `strategy_id` | yes | stable `stg-<slug>` identifier — never renamed |
| `classification` | yes | one of `independent_system`, `variant`, `implementation`, `component`, `experimental`, `unresolved` |
| `status` | yes | lifecycle — see below |
| `rule_versions` | yes | list of `stg-*:vN` refs (may be empty) |
| `display_name` | no | human label |
| `legacy_number` | no | number in `STRATEGIES_OVERVIEW.md` (1–13), or `null` |
| `legacy_aliases` | no | list of strings — every `strategy:` label the legacy trade files used for this strategy; how a strategy's whole record set is found |
| `parent_strategy_id` | no | `stg-` slug of the parent family (must resolve); used with `classification: variant` |
| `variants` | no | list of child model slugs (on the parent) |
| `retired_on` / `retired_note` | no | set when `status: retired` |

## Families

A strategy family is one parent (`independent_system`) plus child models
(`classification: variant`, `parent_strategy_id` → the parent). A family can be `active`
while some of its models are `retired`. Example: `stg-ict-liquidity` (active) holds
`stg-triple-tap` (active, legacy #8) and `stg-liq-sweep` (retired, legacy #4 — the 2022
ICT sweep→MSS→OB model).

## `status` (vocabulary: `STRATEGY_STATUSES`)

- **`proposed`** — catalogued, not yet in use or still under construction.
- **`active`** — the human runs this strategy now. Active status makes the identity
  available to the live strategy resolver; it does not retroactively add a strategy
  to a frozen B1/B3 method-validation population.
- **`retired`** — the human used it and has stopped. **Records are kept, not deleted**,
  and are excluded from method-validation samples. Reactivate by setting `status: active`
  if the human resumes the strategy — `legacy_aliases` lets the whole record set be
  pulled back in one query.
- **`experimental`** — a one-off or indicator test, never a standing strategy.

## Why retired strategies stay

From the Batch-2 strategy-discovery review (`ingestion/pilot/batch-2/`, pilot-notes
#73–#75): a large fraction of historical trades belong to strategies the human no longer
runs. Deleting them loses the ability to reactivate a strategy as a coherent set and
distorts the record. The method denominator excludes them; the archive keeps them.

Identity / cleanup of the underlying `trades/` files is a separate PR-C concern — this
registry never touches legacy files.

## Post-B3 active extensions

The historical B1/B3 active-strategy set and its registry digest are immutable. A later
Owner-approved active identity may be appended for current operational resolution without
rebuilding those historical artifacts. `stg-example-a:v1` remains the NAS100 research candidate;
the Owner-approved `stg-example-a:v2` is the instrument-agnostic active version. This extension
does not enroll old research samples or trades in B1/B3, nor does it by itself register a
trade.

## Rule versions and changing strategies

A published rule version is never edited. Every improvement is a **new version**
(`stg-vegas-tunnel:v2`), and it is made only on the Owner's explicit request through
[`workflows/strategy-change.yaml`](../workflows/strategy-change.yaml): the Agent drafts it, the
Owner confirms it once in the conversation, and the Agent opens a pull request.

- **Usable version:** listed in `rule_versions`, version file `status: active`, and not in
  `retired_rule_versions`. A new version does **not** retire the previous one: both stay usable
  until the Owner says to stop one.
- **Default:** the newest usable version (the last one listed). The Owner may name an older usable
  version (`owner_attestation.rule_version`).
- **Rule-based resolution:** versions of one strategy are alternatives, not competitors; the newest
  version whose required rules pass represents the strategy.
- **Stopping a version:** add it to the strategy's `retired_rule_versions`. Never edit the version
  file: registered trades pin a digest of it.
- **Registered trades** keep the version they were registered with; adding, changing the default or
  stopping other versions never invalidates them.
- **Append only:** new strategies, new versions appended to `rule_versions`, and
  `retired_rule_versions`. Not allowed: editing a version file, or removing or reordering a listed
  version.
- **B1/B3 historical freeze:** the strategies and versions B3 froze are listed in
  `trading_os.registry.B3_BASELINE_RULE_VERSIONS`. They may gain new versions like any strategy, but
  editing or deleting them is blocked, so the historical B1/B3 numbers cannot change.
