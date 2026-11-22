"""Public-edition test scoping.

Tests listed in private_only.json need the maintainer's private data and are not run here.
Whole files are not collected; single tests are reported as skipped with the reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_PATH = Path(__file__).with_name("private_only.json")
_CONFIG = json.loads(_PATH.read_text(encoding="utf-8")) if _PATH.is_file() else {}
_FILES = {Path(p).as_posix() for p in _CONFIG.get("files", [])}
_TESTS = set(_CONFIG.get("tests", []))
_WHY = _CONFIG.get("_why", "private data required")


def pytest_ignore_collect(collection_path, config):
    try:
        rel = Path(collection_path).resolve().relative_to(Path(__file__).parent.resolve()).as_posix()
    except ValueError:
        return None
    return True if rel in _FILES else None


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason=_WHY)
    for item in items:
        if item.nodeid in _TESTS:
            item.add_marker(skip)
