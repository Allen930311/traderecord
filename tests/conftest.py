"""Make the tests independent of the machine's Git line-ending settings.

Git for Windows ships `core.autocrlf=true` in its system config. The repository sets it to false,
but the tests build their own throw-away git repositories, which never see that setting, so on
Windows they converted LF files to CRLF and every digest or byte comparison over those files
failed (test_github_agent_runner.py::test_synthetic_* passed in CI on Linux and failed locally).

Environment-supplied config outranks the system and global files, so this pins the setting for
every git process a test starts, without touching any real configuration.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _git_never_rewrites_line_endings():
    patch = pytest.MonkeyPatch()
    patch.setenv("GIT_CONFIG_COUNT", "1")
    patch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    patch.setenv("GIT_CONFIG_VALUE_0", "false")
    yield
    patch.undo()
