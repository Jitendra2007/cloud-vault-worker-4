"""Shared story/session lease coordinator.

For GitHub Actions, set COORDINATOR_REPO to a private repository and
COORDINATOR_TOKEN to a token that can update vault_registry.json. A lease
conflict exits non-zero; a human may explicitly use --force to take over.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

import requests


@dataclass(frozen=True)
class Lease:
    story: str
    worker: str
    run_id: str
    session_slot: str
    expires_at: int
    source_channel_id: str = ""


class Coordinator:
    def __init__(
        self,
        *,
        repo: str | None = None,
        token: str | None = None,
        path: str = "vault_registry.json",
    ) -> None:
        self.repo = repo or os.environ.get("COORDINATOR_REPO", "")
        self.token = token or os.environ.get("COORDINATOR_TOKEN", "")
        self.path = path
        if not self.repo or not self.token:
            raise RuntimeError("COORDINATOR_REPO and COORDINATOR_TOKEN are required")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self) -> tuple[dict[str, Any], str | None]:
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}"
        response = requests.get(url, headers=self._headers(), timeout=30)
        if response.status_code == 404:
            return {"version": 1, "stories": {}, "workers": {}}, None
        response.raise_for_status()
        body = response.json()
        decoded = base64.b64decode(body["content"]).decode("utf-8")
        return json.loads(decoded), body["sha"]

    def _put(self, data: dict[str, Any], sha: str | None, message: str) -> None:
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}"
        content = base64.b64encode(
            (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        ).decode("ascii")
        payload: dict[str, Any] = {"message": message, "content": content}
        if sha:
            payload["sha"] = sha
        response = requests.put(url, headers=self._headers(), json=payload, timeout=30)
        if response.status_code == 409:
            raise RuntimeError("coordinator registry changed concurrently; retry the command")
        response.raise_for_status()

    def claim(self, lease: Lease, *, force: bool = False) -> None:
        registry, sha = self._get()
        now = int(time.time())
        current = registry.setdefault("stories", {}).get(lease.story)
        if current and int(current.get("lease_expires", 0)) > now and not force:
            raise RuntimeError(
                f"story is locked by worker {current.get('worker')} "
                f"run {current.get('run_id')} until {current.get('lease_expires')}"
            )
        registry["stories"][lease.story] = {
            "status": "IN_PROGRESS",
            "worker": lease.worker,
            "run_id": lease.run_id,
            "session_slot": lease.session_slot,
            "lease_expires": lease.expires_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if lease.source_channel_id:
            registry["stories"][lease.story]["source_channel_id"] = lease.source_channel_id
        self._put(registry, sha, f"Claim {lease.story} by {lease.worker}")

    def release(self, story: str, *, status: str = "BLOCKED", detail: str = "") -> None:
        if status not in {"COMPLETED", "BLOCKED"}:
            raise ValueError("release status must be COMPLETED or BLOCKED")
        registry, sha = self._get()
        entry = registry.setdefault("stories", {}).setdefault(story, {})
        entry.update({
            "status": status,
            "detail": detail,
            "lease_expires": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        self._put(registry, sha, f"Release {story}: {status}")

    def ingest_episode_events(self, event_path: Path) -> tuple[int, int]:
        """Reopen registered completed stories when Telegram has a new packet."""
        events = json.loads(event_path.read_text(encoding="utf-8"))
        if not isinstance(events, list):
            raise ValueError("episode event manifest must be a JSON list")
        registry, sha = self._get()
        stories = registry.setdefault("stories", {})
        seen = set(registry.setdefault("processed_episode_event_ids", []))
        reopened = 0
        unmatched = 0
        for event in events:
            event_id = str(event.get("event_id", ""))
            channel_id = str(event.get("channel_id", ""))
            event_revision = ":".join((
                event_id,
                str(event.get("status", "")),
                str(event.get("bot_link", "")),
            ))
            if not event_id or event_revision in seen:
                continue
            matches = [
                entry for entry in stories.values()
                if str(entry.get("source_channel_id", "")) == channel_id
            ]
            if not matches:
                unmatched += 1
                continue
            for entry in matches:
                entry.update({
                    "status": (
                        "NEW_EPISODES_PENDING"
                        if event.get("status") == "READY_FOR_TITLE_REFRESH"
                        else "WAITING_FOR_BOT_LINK"
                    ),
                    "new_episode_event": event,
                    "lease_expires": 0,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                reopened += 1
            seen.add(event_revision)
        registry["processed_episode_event_ids"] = sorted(seen)
        if reopened or seen:
            self._put(registry, sha, "Ingest new Telegram episode events")
        return reopened, unmatched


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage shared cloud-vault leases")
    parser.add_argument("action", choices=("claim", "release", "ingest-events"))
    parser.add_argument("--story")
    parser.add_argument("--worker", default=os.environ.get("WORKER_ID", "unknown"))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    parser.add_argument("--session-slot", default="")
    parser.add_argument("--source-channel-id", default="")
    parser.add_argument("--events", type=Path)
    parser.add_argument("--lease-seconds", type=int, default=21600)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--status", choices=("COMPLETED", "BLOCKED"), default="BLOCKED")
    parser.add_argument("--detail", default="")
    args = parser.parse_args()
    coordinator = Coordinator()
    if args.action == "ingest-events":
        if not args.events:
            parser.error("--events is required for ingest-events")
        reopened, unmatched = coordinator.ingest_episode_events(args.events)
        print(f"ingest-events OK: reopened={reopened}, unmatched={unmatched}")
    elif args.action == "claim":
        if not args.story:
            parser.error("--story is required for claim")
        coordinator.claim(Lease(
            story=args.story, worker=str(args.worker), run_id=str(args.run_id),
            session_slot=args.session_slot,
            expires_at=int(time.time()) + args.lease_seconds,
            source_channel_id=str(args.source_channel_id),
        ), force=args.force)
    else:
        if not args.story:
            parser.error("--story is required for release")
        coordinator.release(args.story, status=args.status, detail=args.detail)
    print(f"{args.action} OK: {args.story}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
