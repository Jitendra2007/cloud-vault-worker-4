"""
Resilient Telegram Session Switcher for Cloud Vault Workers
===========================================================
Automatically rotates across: primary -> backup_1 -> backup_2
Whenever a session fails (auth error, IP conflict, flood wait, disconnect).
"""

import sys
import os
import json
import logging
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    AuthKeyDuplicatedError,
    SessionRevokedError,
    SessionPasswordNeededError,
    UserDeactivatedError
)

sys.stdout.reconfigure(encoding="utf-8")
logger = logging.getLogger("ResilientSession")

API_ID = int(os.environ.get("API_ID", 36198115))
API_HASH = os.environ.get("API_HASH", "ce040e05f933e3e0a811f186c3d5d3bb")

DEFAULT_POOL_FILE = Path(r"c:\Users\tsapa\Desktop\CLOUD VAULT\vault_sessions_pool.json")

class ResilientSessionManager:
    def __init__(self, pool_file: str | Path | None = None):
        self.pool_file = Path(pool_file) if pool_file else DEFAULT_POOL_FILE
        self.pool = self._load_pool()

    def _load_pool(self) -> dict:
        if self.pool_file.exists():
            try:
                with open(self.pool_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ Error loading session pool file {self.pool_file}: {e}")
        return {}

    def get_sessions_for_account(self, account_key: str, allow_backup_2: bool = False) -> list[tuple[str, str]]:
        """Returns ordered list of (slot_name, session_str) for given account.
        backup_2 is strictly reserved as an emergency cold backup and is NOT used
        automatically unless explicitly enabled or requested by the user.
        """
        acc = self.pool.get(account_key, {})
        slots = ["primary", "backup_1"]
        if allow_backup_2:
            slots.append("backup_2")

        results = []
        for s in slots:
            val = acc.get(s)
            if val and isinstance(val, str) and val.strip():
                results.append((s, val.strip()))
        return results

    async def connect_client(self, account_key: str, preferred_slot: str | None = None, allow_backup_2: bool = False) -> tuple[TelegramClient, str]:
        """
        Connects to Telegram using the preferred slot or primary,
        automatically falling back to backup_1 on failure.
        backup_2 is kept strictly in cold reserve unless explicitly requested.
        """
        use_backup_2 = allow_backup_2 or (preferred_slot == "backup_2")
        sessions = self.get_sessions_for_account(account_key, allow_backup_2=use_backup_2)
        if not sessions:
            # Fallback to environment variables if pool not found
            env_val = os.environ.get("VAULT_SESSION") or os.environ.get("TELEGRAM_STRING_SESSION")
            if env_val:
                sessions = [("env", env_val.strip())]
            else:
                raise RuntimeError(f"No sessions configured for account '{account_key}'")

        if preferred_slot:
            sessions.sort(key=lambda x: 0 if x[0] == preferred_slot else 1)

        errors = []
        for slot_name, session_str in sessions:
            print(f"🔌 Trying '{account_key}' session slot [{slot_name}]...")
            client = None
            try:
                client = TelegramClient(StringSession(session_str), API_ID, API_HASH, receive_updates=False)
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    raise RuntimeError(f"Session [{slot_name}] is not authorized")

                me = await client.get_me()
                print(f"✅ Connected successfully using slot [{slot_name}] as {me.first_name} (+{me.phone}) [ID: {me.id}]")
                return client, slot_name
            except (AuthKeyDuplicatedError, SessionRevokedError, UserDeactivatedError) as e:
                err_msg = f"Slot [{slot_name}] revoked/duplicated: {e}"
                print(f"⚠️ {err_msg}. Auto-switching to next backup session...")
                errors.append(err_msg)
                if client:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            except Exception as e:
                err_msg = f"Slot [{slot_name}] error: {e}"
                print(f"⚠️ {err_msg}. Auto-switching to next backup session...")
                errors.append(err_msg)
                if client:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

        raise RuntimeError(f"All session slots (primary + 2 backups) failed for '{account_key}': {'; '.join(errors)}")

# Global helper functions
async def get_resilient_vault_client(preferred_slot: str | None = None, allow_backup_2: bool = False) -> tuple[TelegramClient, str]:
    manager = ResilientSessionManager()
    return await manager.connect_client("vault", preferred_slot, allow_backup_2=allow_backup_2)

async def get_resilient_worker_client(account_key: str = "main", preferred_slot: str | None = None, allow_backup_2: bool = False) -> tuple[TelegramClient, str]:
    manager = ResilientSessionManager()
    return await manager.connect_client(account_key, preferred_slot, allow_backup_2=allow_backup_2)


if __name__ == "__main__":
    import asyncio
    async def test():
        print("Testing resilient connection for vault and main accounts...")
        m = ResilientSessionManager()
        c_vault, slot_v = await m.connect_client("vault")
        await c_vault.disconnect()
        print(f"Vault verified with slot: {slot_v}\n")

        c_main, slot_m = await m.connect_client("main")
        await c_main.disconnect()
        print(f"Main verified with slot: {slot_m}\n")

    asyncio.run(test())
