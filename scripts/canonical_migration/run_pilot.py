"""Direct-script wrapper; use ``python run_pilot.py --output-dir ...``."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from canonical_migration.cli import main
else:  # pragma: no cover
    from .cli import main

raise SystemExit(main())
