"""Read-only canonical migration substrate and full-history dry-run tools.

The Phase 3 pilot and Issue #5 dry run never write to legacy trade Markdown,
create production records, or allocate human trade numbers.
"""

__all__ = [
    "pipeline",
    "models",
    "normalizer",
    "validator",
    "execution_verification",
    "adjudication",
    "full_history",
    "phase4c",
]
