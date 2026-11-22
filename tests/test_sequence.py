from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from trading_os.sequence import NumberAllocator


def test_sequences_are_separate_and_nonreusable(tmp_path: Path):
    allocator = NumberAllocator(tmp_path)
    assert allocator.allocate("live") == "T000001"
    assert allocator.allocate("backtest") == "BT000001"
    allocator.tombstone("T000001", "identity correction", "reviewer:test")
    state = json.loads((tmp_path / "canonical" / "sequences" / "numbers.json").read_text(encoding="utf-8"))
    tombstone = state["tombstones"][0]
    assert tombstone["reason"] == "identity correction"
    assert tombstone["reviewer"] == "reviewer:test"
    assert tombstone["timestamp"].endswith("Z")
    assert allocator.allocate("live") == "T000002"


def test_concurrent_allocators_never_duplicate_numbers(tmp_path: Path):
    def allocate_one(_: int) -> str:
        return NumberAllocator(tmp_path).allocate("live")

    with ThreadPoolExecutor(max_workers=8) as pool:
        numbers = list(pool.map(allocate_one, range(32)))

    assert len(numbers) == 32
    assert len(set(numbers)) == 32
    assert sorted(numbers) == [f"T{i:06d}" for i in range(1, 33)]
