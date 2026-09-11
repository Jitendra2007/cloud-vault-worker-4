"""Use workspace-local Telegram sessions with controlled failover.

The pool file contains only relative file paths. Default failover is primary
then backup_1. backup_2 is cold reserve and requires an explicit request.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession


WORKSPACE = Path(__file__).resolve().parent
DEFAULT_POOL_FILE = WORKSPACE / "vault_sessions_pool.json"
AUTO_SLOTS = ("primary", "backup_1")
ALL_SLOTS = AUTO_SLOTS + ("backup_2",)


@dataclass(frozen=True)
class SessionCandidate:
    account: str
    slot: str
    path: Path


def _workspace_path(value: str) -> Path:
    path = (WORKSPACE / value).resolve()
    try:
        path.relative_to(WORKSPACE)
    except ValueError as error:
        raise RuntimeError("session file must remain inside CLOUD VAULT") from error
    return path


def _api_credentials() -> tuple[int, str]:
    try:
        api_id = int(os.environ["API_ID"])
        api_hash = os.environ["API_HASH"].strip()
    except (KeyError, ValueError) as error:
        raise RuntimeError("API_ID and API_HASH must be set as environment variables") from error
    if not api_hash:
        raise RuntimeError("API_HASH must not be empty")
    return api_id, api_hash


class ResilientSessionManager:
    def __init__(self, pool_file: str | Path | None = None) -> None:
        self.pool_file = Path(pool_file).resolve() if pool_file else DEFAULT_POOL_FILE
        self.pool = self._load_pool()

    def _load_pool(self) -> dict[str, dict[str, str]]:
        data = json.loads(self.pool_file.read_text(encoding="utf-8"))
        if data.get("session_files_only") is not True:
            raise RuntimeError("migrate the session pool before using this manager")
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            raise RuntimeError("session pool has no accounts mapping")
        for account, slots in accounts.items():
            if not isinstance(slots, dict) or any(slot not in slots for slot in ALL_SLOTS):
                raise RuntimeError(f"session pool entry {account!r} is incomplete")
        return accounts

    def candidates(
        self,
        account_key: str,
        *,
        preferred_slot: str | None = None,
        allow_backup_2: bool = False,
    ) -> list[SessionCandidate]:
        if account_key not in self.pool:
            raise RuntimeError(f"unknown account key: {account_key}")
        if preferred_slot and preferred_slot not in ALL_SLOTS:
            raise RuntimeError(f"unknown session slot: {preferred_slot}")
        if preferred_slot == "backup_2" and not allow_backup_2:
            raise RuntimeError("backup_2 is cold reserve; enable it only for an explicit request")

        slots = list(AUTO_SLOTS)
        if allow_backup_2:
            slots.append("backup_2")
        if preferred_slot:
            slots.remove(preferred_slot)
            slots.insert(0, preferred_slot)
        return [
            SessionCandidate(account_key, slot, _workspace_path(self.pool[account_key][slot]))
            for slot in slots
        ]

    @staticmethod
    def _read_session(candidate: SessionCandidate) -> str:
        if not candidate.path.is_file():
            raise RuntimeError("session file is missing")
        value = candidate.path.read_text(encoding="utf-8").strip()
        if not value or StringSession(value).auth_key is None:
            raise RuntimeError("session file is invalid")
        return value

    def offline_status(self, account_key: str, *, allow_backup_2: bool = False) -> list[str]:
        output: list[str] = []
        for candidate in self.candidates(account_key, allow_backup_2=allow_backup_2):
            try:
                self._read_session(candidate)
                output.append(f"{candidate.account}/{candidate.slot}: valid")
            except RuntimeError as error:
                output.append(f"{candidate.account}/{candidate.slot}: {error}")
        return output

    async def connect_client(
        self,
        account_key: str,
        preferred_slot: str | None = None,
        allow_backup_2: bool = False,
    ) -> tuple[TelegramClient, str]:
        """Connect only through permitted slots; caller must disconnect."""
        api_id, api_hash = _api_credentials()
        errors: list[str] = []
        for candidate in self.candidates(
            account_key,
            preferred_slot=preferred_slot,
            allow_backup_2=allow_backup_2,
        ):
            client: TelegramClient | None = None
            try:
                client = TelegramClient(StringSession(self._read_session(candidate)), api_id, api_hash, receive_updates=False)
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("session is not authorized")
                print(f"connected: {candidate.account}/{candidate.slot}")
                return client, candidate.slot
            except Exception as error:
                errors.append(f"{candidate.slot}:{type(error).__name__}")
                if client and client.is_connected():
                    await client.disconnect()
        raise RuntimeError(f"no approved session connected for {account_key}: {', '.join(errors)}")


async def get_resilient_vault_client(
    preferred_slot: str | None = None,
    allow_backup_2: bool = False,
) -> tuple[TelegramClient, str]:
    return await ResilientSessionManager().connect_client(
        "vault", preferred_slot, allow_backup_2
    )


async def get_resilient_worker_client(
    account_key: str = "main",
    preferred_slot: str | None = None,
    allow_backup_2: bool = False,
) -> tuple[TelegramClient, str]:
    return await ResilientSessionManager().connect_client(
        account_key, preferred_slot, allow_backup_2
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Offline validation of local Cloud Vault session files")
    parser.add_argument("--account", required=True, choices=("vault", "main", "sub1", "sub2"))
    parser.add_argument("--allow-backup-2", action="store_true")
    args = parser.parse_args()
    for result in ResilientSessionManager().offline_status(
        args.account, allow_backup_2=args.allow_backup_2
    ):
        print(result)
