from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from .oslock import os_file_lock


class NumberAllocator:
    def __init__(self, root: Path):
        self.path = root / "canonical" / "sequences" / "numbers.json"
        self.lock_path = self.path.with_suffix(".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if not self.path.exists():
                self._write_state({"live_next": 1, "backtest_next": 1, "allocated": [], "tombstones": []})
            self.state = self._read_state()

    @contextmanager
    def _locked(self):
        with os_file_lock(self.lock_path, "timed out waiting for sequence allocation lock"):
            yield

    def _read_state(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict) -> None:
        tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        for _ in range(200):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                # A Windows antivirus/indexer or a just-released reader can
                # briefly hold the destination even though the logical lock
                # is free.  Retry the atomic commit without changing state.
                time.sleep(0.05)
        raise TimeoutError("timed out replacing sequence state")

    def allocate(self, trade_type: str) -> str:
        if trade_type not in {"live", "backtest"}:
            raise ValueError("resolved type required")
        with self._locked():
            state = self._read_state()
            key = "live_next" if trade_type == "live" else "backtest_next"
            prefix = "T" if trade_type == "live" else "BT"
            number = f"{prefix}{state[key]:06d}"
            used = set(state.get("allocated", [])) | {
                item.get("trade_no")
                for item in state.get("tombstones", [])
                if isinstance(item, dict)
            }
            while number in used:
                state[key] += 1
                number = f"{prefix}{state[key]:06d}"
            state[key] += 1
            state["allocated"].append(number)
            self._write_state(state)
            self.state = state
            return number

    def tombstone(self, number: str, reason: str, reviewer: str) -> None:
        if not reason or not reviewer:
            raise ValueError("tombstone reason and reviewer are required")
        with self._locked():
            state = self._read_state()
            if number not in state["allocated"]:
                raise ValueError("number was not allocated")
            if any(item.get("trade_no") == number for item in state["tombstones"]):
                raise ValueError("number is already tombstoned")
            state["tombstones"].append({
                "trade_no": number,
                "reason": reason,
                "reviewer": reviewer,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })
            self._write_state(state)
            self.state = state
