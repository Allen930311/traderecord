"""Crash-safe locks for the canonical write paths (issue #176).

The old O_EXCL marker files survived a killed process and blocked every later write. The OS lock
is released by the kernel when its holder dies. Mutual exclusion matters more than crash recovery,
so each lock is also checked to really block a second holder.
"""
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from trading_os.oslock import os_file_lock
from trading_os.operational_lifecycle import LifecycleStore
from trading_os.sequence import NumberAllocator
from trading_os.writer import CanonicalWriter

REPO = Path(__file__).resolve().parents[1]

# name -> (how a process takes the real lock, how the test takes the same lock in-process)
LOCKS = {
    "lifecycle": ("LifecycleStore(root)._locked()", lambda root: LifecycleStore(root)._locked(), "lifecycle event lock"),
    "numbers": ("NumberAllocator(root)._locked()", lambda root: NumberAllocator(root)._locked(), "sequence allocation lock"),
    "finalization": ("CanonicalWriter(root)._finalization_locked()", lambda root: CanonicalWriter(root)._finalization_locked(), "finalization lock"),
}


def _holder(root: Path, expression: str) -> subprocess.Popen:
    """A separate process that takes the lock, announces it, and then sleeps holding it."""
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(REPO)!r})
        from pathlib import Path
        from trading_os.operational_lifecycle import LifecycleStore
        from trading_os.sequence import NumberAllocator
        from trading_os.writer import CanonicalWriter
        root = Path({str(root)!r})
        with {expression}:
            print("locked", flush=True)
            time.sleep(600)
    """)
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "locked"
    return process


@pytest.mark.parametrize("name", sorted(LOCKS))
def test_a_killed_holder_does_not_wedge_later_writes(tmp_path, name):
    expression, take, _ = LOCKS[name]
    holder = _holder(tmp_path, expression)
    try:
        holder.kill()  # no cleanup code ever runs in the holder
        holder.wait()
        started = time.monotonic()
        with take(tmp_path):
            pass
        assert time.monotonic() - started < 5  # the old marker file would have cost the full 10 s timeout
    finally:
        holder.kill()


@pytest.mark.parametrize("name", sorted(LOCKS))
def test_a_live_holder_still_blocks_the_real_writer(tmp_path, name, monkeypatch):
    """Mutual exclusion is the point of the lock: the real writer must wait on a live holder, then time out."""
    expression, take, message = LOCKS[name]
    holder = _holder(tmp_path, expression)
    try:
        monkeypatch.setattr("trading_os.oslock.ATTEMPTS", 6)
        monkeypatch.setattr("trading_os.oslock.DELAY_SECONDS", 0.02)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match=message):
            with take(tmp_path):
                pytest.fail("acquired a lock that another live process holds")
        assert time.monotonic() - started >= 0.1  # it really waited for the holder
    finally:
        holder.kill()


def _path_of(root: Path, name: str) -> Path:
    return {
        "lifecycle": root / "canonical" / "lifecycle" / "events.lock",
        "numbers": root / "canonical" / "sequences" / "numbers.lock",
        "finalization": root / "canonical" / "sequences" / "finalizations.lock",
    }[name]


@pytest.mark.parametrize("name", sorted(LOCKS))
def test_the_real_writers_use_the_lock_files_the_tests_assume(tmp_path, name):
    _, take, _ = LOCKS[name]
    with take(tmp_path):
        assert _path_of(tmp_path, name).exists()


def test_the_lock_is_released_after_the_with_block_and_after_an_exception(tmp_path):
    path = tmp_path / "x.lock"
    with os_file_lock(path, "m"):
        pass
    with pytest.raises(RuntimeError):
        with os_file_lock(path, "m"):
            raise RuntimeError("boom")
    with os_file_lock(path, "m", attempts=1):  # free again; would raise TimeoutError if still held
        pass


def test_threads_serialise_a_read_modify_write(tmp_path):
    """Two threads (separate file handles) must not interleave inside the lock."""
    counter = tmp_path / "counter.txt"
    counter.write_text("0", encoding="utf-8")

    def bump():
        for _ in range(25):
            with os_file_lock(tmp_path / "t.lock", "m"):
                value = int(counter.read_text(encoding="utf-8"))
                time.sleep(0.001)
                counter.write_text(str(value + 1), encoding="utf-8")

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert counter.read_text(encoding="utf-8") == "100"


def _dead_pid() -> int:
    """The PID of a process that has certainly exited."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _legacy_marker(path: Path, pid: int | str) -> None:
    """What the old implementation left in the critical section: an O_EXCL file holding its PID."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(pid).encode("ascii"))
    os.close(fd)


def test_a_stale_marker_from_a_crashed_old_writer_is_cleared(tmp_path):
    """Upgrading a repo that still holds a marker from a crash must not need manual cleanup."""
    marker = tmp_path / "canonical" / "lifecycle" / "events.lock"
    _legacy_marker(marker, _dead_pid())
    started = time.monotonic()
    with LifecycleStore(tmp_path)._locked():
        pass
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("junk", ["not-a-pid", "0", "-5"])
def test_a_marker_the_old_code_could_not_have_written_is_cleared(tmp_path, junk):
    marker = tmp_path / "x.lock"
    marker.write_text(junk, encoding="ascii")
    with os_file_lock(marker, "m", attempts=3, delay=0.01):
        pass


def test_a_marker_of_a_live_old_writer_is_respected(tmp_path):
    """Codex #176: an old-version writer still inside its critical section must exclude a new one."""
    marker = tmp_path / "canonical" / "lifecycle" / "events.lock"
    _legacy_marker(marker, os.getpid())  # this process is certainly alive
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="lifecycle event lock"):
        with os_file_lock(marker, "timed out waiting for lifecycle event lock", attempts=6, delay=0.02):
            pytest.fail("entered the critical section while a live old-version writer held its marker")
    assert time.monotonic() - started >= 0.1
    assert marker.read_text(encoding="ascii") == str(os.getpid())  # the live writer's marker is untouched


def test_the_new_lock_proceeds_once_the_old_writer_removes_its_marker(tmp_path):
    marker = tmp_path / "events.lock"
    _legacy_marker(marker, os.getpid())
    def old_writer_finishes():
        # The old writer unlinks its marker; on Windows that can briefly lose to a reader.
        for _ in range(100):
            try:
                marker.unlink()
                return
            except PermissionError:
                time.sleep(0.005)

    threading.Timer(0.15, old_writer_finishes).start()
    with os_file_lock(marker, "m", attempts=100, delay=0.02):
        # A fresh, empty permanent lock file. stat() rather than a read: on Windows the locked byte
        # cannot be read through another handle, even in the same process.
        assert marker.stat().st_size == 0


def test_the_new_lock_never_writes_into_its_file(tmp_path):
    path = tmp_path / "n.lock"
    with os_file_lock(path, "m"):
        pass
    assert path.read_bytes() == b""


def test_process_liveness_never_signals_the_process():
    from trading_os.oslock import _process_alive

    assert _process_alive(os.getpid()) is True
    assert _process_alive(_dead_pid()) is False
    assert _process_alive(0) is False and _process_alive(-1) is False


def test_the_gitignore_and_the_lock_list_agree_exactly():
    """The lock files git ignores are exactly the ones the code creates: no broader, no narrower."""
    from trading_os.oslock import WRITE_LOCK_FILES

    ignored = {line.strip() for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
               if line.strip().startswith("canonical/") and line.strip().endswith(".lock")}
    assert ignored == set(WRITE_LOCK_FILES)
    assert not any("*" in line for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
                   if ".lock" in line and not line.startswith("#"))


def test_every_real_writer_lock_path_is_on_the_list(tmp_path):
    """If a writer's lock moves or a new one appears, the exempt list must follow."""
    from trading_os.oslock import WRITE_LOCK_FILES

    for name, (_, take, _) in LOCKS.items():
        with take(tmp_path):
            pass
    found = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.lock")}
    assert found <= set(WRITE_LOCK_FILES) and len(found) == 3


def test_after_the_first_new_acquisition_the_old_implementation_cannot_enter(tmp_path):
    """New-first is safe: the permanent file shuts out the old O_EXCL protocol (it fails, not writes)."""
    path = tmp_path / "canonical" / "lifecycle" / "events.lock"
    with LifecycleStore(tmp_path)._locked():
        pass
    assert path.exists()
    with pytest.raises(FileExistsError):
        os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # exactly what the old code did


def test_creation_of_the_permanent_file_is_atomic_with_the_old_markers_creation(tmp_path):
    """Whoever creates the file first wins; the loser is told by FileExistsError, never both."""
    from trading_os.oslock import _create_permanent_file

    path = tmp_path / "a.lock"
    assert _create_permanent_file(path) is True
    assert _create_permanent_file(path) is False
    old_path = tmp_path / "b.lock"
    _legacy_marker(old_path, os.getpid())            # the old side got there first
    assert _create_permanent_file(old_path) is False  # so the new side does not "create" it
    assert old_path.read_text(encoding="ascii") == str(os.getpid())


def test_clearing_a_stale_marker_never_opens_a_marker_an_old_writer_recreated(tmp_path, monkeypatch):
    """Codex round 3: unlink-then-open let an old writer's fresh marker be opened and locked."""
    import trading_os.oslock as oslock

    path = tmp_path / "canonical" / "lifecycle" / "events.lock"
    _legacy_marker(path, _dead_pid())                       # a crashed old writer's leftover
    real_create = oslock._create_permanent_file
    calls = []

    def create_with_a_racing_old_writer(target):
        calls.append(target)
        if len(calls) == 2:                                 # the pass right after the stale clear
            _legacy_marker(path, os.getpid())               # an old writer wins the race and enters
        return real_create(target)

    monkeypatch.setattr(oslock, "_create_permanent_file", create_with_a_racing_old_writer)
    with pytest.raises(TimeoutError, match="lifecycle event lock"):
        with os_file_lock(path, "timed out waiting for lifecycle event lock", attempts=8, delay=0.02):
            pytest.fail("entered the critical section while an old writer held its freshly created marker")
    assert path.read_text(encoding="ascii") == str(os.getpid())
    assert len(calls) >= 2  # the atomic creation was re-run after the clear, not skipped
