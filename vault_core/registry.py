"""Coordinator state with story and Telegram-session leases."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


class LeaseError(RuntimeError):
    pass


@contextmanager
def _exclusive_lock(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_file, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class Registry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "stories": {}, "sessions": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, state: dict) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def acquire(self, story: str, session_slot: str, worker: str, run_id: str,
                duration_seconds: int = 21600, allow_takeover: bool = False) -> dict:
        now = int(time.time())
        with _exclusive_lock(self.lock_path):
            state = self._read()
            story_state = state["stories"].get(story, {})
            session_state = state["sessions"].get(session_slot, {})
            conflicts = []
            for kind, value in (("story", story_state), ("session", session_state)):
                if value.get("lease_expires", 0) > now and value.get("run_id") != run_id:
                    conflicts.append(f"{kind} is held by {value.get('worker')} / run {value.get('run_id')}")
            if conflicts and not allow_takeover:
                raise LeaseError("; ".join(conflicts))
            lease = {
                "worker": worker,
                "run_id": run_id,
                "status": "WORKING",
                "lease_expires": now + duration_seconds,
                "updated_at": now,
            }
            state["stories"][story] = {**story_state, **lease}
            state["sessions"][session_slot] = {**session_state, **lease, "story": story}
            self._write(state)
            return state["stories"][story]

    def update_story(self, story: str, status: str, **details) -> None:
        with _exclusive_lock(self.lock_path):
            state = self._read()
            record = state["stories"].setdefault(story, {})
            record.update(details, status=status, updated_at=int(time.time()))
            self._write(state)
