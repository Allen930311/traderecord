"""Crash-safe advisory file locks for the canonical write paths.

Writers used to serialise with an ``O_CREAT | O_EXCL`` marker file that was removed in a
``finally`` block. A process killed while holding it (power loss, task kill, host crash) left the
marker behind and every later write timed out until someone deleted the file by hand.

``os_file_lock`` takes an operating-system lock on a permanent file instead
(``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere). The kernel drops it when the holder
exits however it exits, so a crash cannot wedge later writes. The lock files are git-ignored and
never content; the new code never writes into them (the locked byte must stay unread on Windows).

Upgrade overlap (old code = O_EXCL marker holding a PID; new code = permanent empty file + OS lock):

- New-first is safe. The new code creates the permanent file with ``O_EXCL``, the same atomic
  operation the old code used for its marker, so exactly one of them can create the file. After
  that the old code can never create its marker: it times out instead of writing.
- A non-empty lock file is a leftover or live old marker (the new code never writes into its
  file). The new code waits while that PID is alive and clears the marker once the process is
  gone or the content is not a PID, so a marker left by an old crash cannot wedge writes.
- NOT guaranteed: an old writer that is already between creating its marker and writing its PID
  (the marker is briefly an empty file, indistinguishable from the permanent one), or that starts
  in the instant before the new code creates the file, or that starts in the instant between the
  new code clearing a stale marker and creating the file again (after a clear the new code always
  re-runs the atomic creation, so it never opens a marker an old writer recreated, but it cannot
  tell an empty one from its own). The old protocol is fixed, so the new side cannot close this. Rollout rule: do not start an old-version write during the first
  new-version write after an upgrade. Old writes are short; after the first new acquisition of
  each lock the old code is shut out for good.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ATTEMPTS = 200
DELAY_SECONDS = 0.05

# The only lock files the canonical write paths create, relative to the repository root. Every
# consumer that must ignore them (the Runner's created-files check, .gitignore) uses this exact
# list, never a suffix pattern: a real content file must not be hidden because it ends in ".lock".
WRITE_LOCK_FILES = (
    "canonical/lifecycle/events.lock",
    "canonical/sequences/numbers.lock",
    "canonical/sequences/finalizations.lock",
    "canonical/registration/backtest-commit.lock",
)


def _try_lock(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _create_permanent_file(path: Path) -> bool:
    """Atomically create the empty permanent lock file. False if it already exists."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except (FileExistsError, PermissionError):
        return False
    os.close(fd)
    return True


def _process_alive(pid: int) -> bool:
    """True if a process with this PID exists. Never signals the process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(process, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)  # signal 0 only checks existence on POSIX
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _legacy_holder_active(path: Path) -> bool:
    """True if the caller must not proceed to open the file on this pass.

    That is the case when ``path`` is a legacy marker whose writing process is still alive, and
    also right after a stale marker was cleared: the state changed, so the caller starts over
    from the atomic ``O_EXCL`` creation instead of opening whatever now sits at the path (an old
    writer may have just recreated its marker there).

    A marker with unreadable or non-numeric content cannot have been written by the old code
    (it wrote only a PID), and one whose process is gone is stale; both are cleared.
    """
    # stat() does not open the file, so the usual case (the empty permanent lock file) never
    # holds it open; only a non-empty legacy marker is read.
    try:
        if path.stat().st_size == 0:
            return False  # the permanent, empty lock file of this implementation
        raw = path.read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return True  # cannot tell; treat as held and let the caller retry or time out
    text = raw.strip()
    if text.isdigit() and _process_alive(int(text)):
        return True
    try:
        path.unlink()  # stale marker: nothing holds it open, so removing it is safe
    except FileNotFoundError:
        pass
    except OSError:
        return True
    return True  # re-evaluate from the atomic creation; never open the path straight after a removal


@contextmanager
def os_file_lock(
    path: Path,
    timeout_message: str,
    *,
    error: type[Exception] = TimeoutError,
    attempts: int | None = None,
    delay: float | None = None,
) -> Iterator[None]:
    """Hold an exclusive OS lock on ``path`` (created if missing) for the ``with`` block.

    Raises ``error(timeout_message)`` if the lock stays held by another process or handle for
    ``attempts * delay`` seconds (200 x 0.05 s unless given), or if a live old-version writer's
    marker is still present for that long. The lock is not reentrant: a second acquisition in the
    same process (a different file handle) waits exactly like another process would.
    """
    attempts = ATTEMPTS if attempts is None else attempts
    delay = DELAY_SECONDS if delay is None else delay
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(attempts):
        # If this call creates the file, no old marker can exist and none can be created later.
        # Otherwise a non-empty file is checked before it is opened: an open handle would stop an
        # old writer from removing its marker on Windows.
        if not _create_permanent_file(path) and _legacy_holder_active(path):
            time.sleep(delay)
            continue
        try:
            handle = open(path, "a+b")
        except PermissionError:  # e.g. a delete still pending on Windows; retry like the old code
            time.sleep(delay)
            continue
        try:
            if _try_lock(handle):
                try:
                    yield
                finally:
                    _unlock(handle)
                return
        finally:
            handle.close()
        time.sleep(delay)
    raise error(timeout_message)
