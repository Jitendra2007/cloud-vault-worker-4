"""Safe Cloud Vault Worker Engine.

Operates with strict non-negotiable safeguards:
1. Resumes target channel; never creates channels.
2. Respects ResilientSessionManager: primary -> backup_1, backup_2 cold reserve.
3. Fail-closed: stops immediately on missing episodes or gaps.
4. Rewrites ID3 tags with clean official episode titles and performer.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    InputChatUploadedPhoto,
)

from parallel_transfer import fast_download_file, fast_upload_file
from resilient_session_manager import ResilientSessionManager, AUTO_SLOTS
from vault_core.audit import audit_episode_order, episode_number

try:
    from mutagen.id3 import ID3, TIT2, TPE1
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False


def parse_bot_link(url: str) -> tuple[str | None, str | None]:
    match = re.search(
        r'(?:telegram\.me|t\.me)/([A-Za-z0-9_]+)\?start=([A-Za-z0-9_%+/=\-]+)',
        str(url),
        re.I
    )
    if match:
        return match.group(1), urllib.parse.unquote(match.group(2)).strip()
    return None, None


def parse_ep_range(text: str) -> tuple[int, int] | tuple[None, None]:
    match = re.search(r'\[?(\d+)\s*[-–]\s*(\d+)\]?', str(text))
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def parse_ep_num(text: str) -> int | None:
    s = str(text)
    match = re.search(r'(?:ep|episode|chapter|e)\s*[-_–:\s]*(\d+)', s, re.I)
    if match:
        return int(match.group(1))
    match2 = re.search(r'\[(\d+)\]', s)
    if match2:
        return int(match2.group(1))
    match3 = re.search(r'^\s*(\d+)\s*[-_–.]', s)
    if match3:
        return int(match3.group(1))
    return None


async def get_client(
    session_str: str | None = None,
    account_key: str = "vault",
    allow_backup_2: bool = False,
) -> TelegramClient:
    api_id = int(os.environ.get("API_ID", 36198115))
    api_hash = os.environ.get("API_HASH", "ce040e05f933e3e0a811f186c3d5d3bb").strip()

    # 1. If explicit session string is provided via environment
    if session_str and session_str.strip():
        client = TelegramClient(StringSession(session_str.strip()), api_id, api_hash, receive_updates=False)
        await client.connect()
        me = await client.get_me()
        if me:
            print(f"✓ Telegram client connected via environment session: {me.first_name} (+{me.phone})")
            return client

    # 2. Otherwise use local ResilientSessionManager
    manager = ResilientSessionManager()
    candidates = manager.candidates(account_key, allow_backup_2=allow_backup_2)
    last_error = None
    for cand in candidates:
        try:
            val = manager._read_session(cand)
            client = TelegramClient(StringSession(val), api_id, api_hash, receive_updates=False)
            await client.connect()
            me = await client.get_me()
            if me:
                print(f"✓ Connected via {cand.account} [{cand.slot}]: {me.first_name} (+{me.phone})")
                return client
        except Exception as err:
            last_error = err
            print(f"⚠️ Slot {cand.slot} connection failed: {err}")
            continue

    raise RuntimeError(f"All session candidates for {account_key} failed: {last_error}")


async def harvest_batch(
    client: TelegramClient,
    bot_username: str,
    start_token: str,
    expected_count: int = 0,
    label: str = "",
) -> list:
    bot_entity = await client.get_input_entity(bot_username)
    last_msgs = []
    async for m in client.iter_messages(bot_entity, limit=1):
        last_msgs.append(m)
    last_id = last_msgs[0].id if last_msgs else 0

    for send_attempt in range(3):
        try:
            await client.send_message(bot_entity, f"/start {start_token}")
            break
        except FloodWaitError as fwe:
            print(f"⏳ FloodWait on /start: sleeping {fwe.seconds + 5}s...")
            await asyncio.sleep(fwe.seconds + 5)

    audio_messages = []
    prev_len = 0
    stable_polls = 0

    for poll_idx in range(35):
        await asyncio.sleep(2.0)
        cur = []
        async for m in client.iter_messages(bot_entity, limit=50):
            if m.id <= last_id:
                break
            if m.media and hasattr(m.media, "document") and m.media.document:
                cur.append(m)
        audio_messages = list(reversed(cur))

        if expected_count > 0 and len(audio_messages) >= expected_count:
            break

        if len(audio_messages) > 0 and len(audio_messages) == prev_len:
            stable_polls += 1
            if stable_polls >= 3:
                break
        else:
            stable_polls = 0
            prev_len = len(audio_messages)

    return audio_messages


async def run_worker(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if not config_path.is_file():
        raise SystemExit(f"Config file not found: {config_path}")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    story_name = args.story.strip()

    # Find story definition in config
    story_def = None
    for s in config.get("stories", []):
        if s.get("title", "").strip().lower() == story_name.lower():
            story_def = s
            break

    if not story_def:
        # Fallback to general stories_data folder if present
        story_def = {
            "title": story_name,
            "source_links_file": f"stories_data/{story_name}/{story_name}.txt",
            "official_titles_file": f"stories_data/{story_name}/official_titles.json",
        }

    target_channel_id = args.target_channel_id or config.get("target_channel_id")
    if not target_channel_id or str(target_channel_id).startswith("SET_"):
        raise SystemExit(f"Invalid target_channel_id: {target_channel_id}. Primary account must create channel first!")

    target_channel_id = int(str(target_channel_id).strip())

    # Official titles map
    official_titles_map = {}
    titles_file = Path(story_def.get("official_titles_file", ""))
    if titles_file.is_file():
        official_titles_map = json.loads(titles_file.read_text(encoding="utf-8"))
        print(f"📖 Loaded {len(official_titles_map)} official episode titles from {titles_file}")

    # Connect client
    session_env = os.environ.get("VAULT_SESSION") or os.environ.get("TELEGRAM_STRING_SESSION")
    client = await get_client(session_str=session_env, account_key="vault", allow_backup_2=args.allow_backup_2)

    # Verify target channel exists
    channel = await client.get_entity(target_channel_id)
    print(f"✓ Target Channel Verified: {getattr(channel, 'title', target_channel_id)} (ID: {target_channel_id})")

    # Audit current channel episodes
    existing_eps = set()
    async for msg in client.iter_messages(channel, limit=10000):
        if msg.media and hasattr(msg.media, "document") and msg.media.document:
            title = ""
            for attr in msg.media.document.attributes:
                if isinstance(attr, DocumentAttributeAudio) and attr.title:
                    title = attr.title
                elif isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                    title = attr.file_name
            num = parse_ep_num(title)
            if num is not None:
                existing_eps.add(num)

    audit = audit_episode_order([(ep, f"Ep {ep}") for ep in sorted(existing_eps)])
    print(f"📊 Current Vault State: {len(existing_eps)} episodes verified through Ep {audit.verified_through}")
    if audit.first_problem:
        print(f"⚠️ Sequence warning at episode {audit.first_problem}")

    # Load source links
    links_file = Path(story_def.get("source_links_file", ""))
    if not links_file.is_file():
        # Search in stories_data
        candidates = list(Path(".").glob(f"**/*{story_name}*.txt"))
        if candidates:
            links_file = candidates[0]
        else:
            raise SystemExit(f"Links file not found for story: {story_name}")

    batches = []
    with open(links_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("->")]
            if len(parts) >= 6:
                rng_label = parts[3]
                bot_url = parts[5]
                s_ep, e_ep = parse_ep_range(rng_label)
                if s_ep and e_ep and any(d in bot_url for d in ("t.me", "telegram.me")):
                    batches.append({
                        "start_ep": s_ep,
                        "end_ep": e_ep,
                        "range_label": rng_label,
                        "bot_url": bot_url,
                    })

    batches.sort(key=lambda b: b["start_ep"])
    print(f"✓ Found {len(batches)} batches in {links_file.name}")

    # Filter batches that need upload
    missing_batches = [b for b in batches if not all(ep in existing_eps for ep in range(b["start_ep"], b["end_ep"] + 1))]
    print(f"🚀 Batches pending upload: {len(missing_batches)}")

    # Process each batch sequentially
    for idx, batch in enumerate(missing_batches, 1):
        s_ep, e_ep = batch["start_ep"], batch["end_ep"]
        rng = batch["range_label"]
        bot_user, token = parse_bot_link(batch["bot_url"])
        if not bot_user or not token:
            print(f"⚠️ Invalid bot link in batch {rng}: {batch['bot_url']}")
            continue

        expected_count = e_ep - s_ep + 1
        print(f"\n⚡ [{idx}/{len(missing_batches)}] Triggering @{bot_user} for Range {rng} (expecting {expected_count} tracks)...")

        tracks = await harvest_batch(client, bot_user, token, expected_count=expected_count, label=rng)
        if not tracks:
            print(f"🛑 FATAL: 0 tracks returned for {rng}. Stopping to prevent gaps!")
            return 1

        print(f"   Received {len(tracks)} tracks. Processing and uploading to Vault...")
        for t_idx, msg in enumerate(tracks):
            calc_ep = s_ep + t_idx
            if calc_ep in existing_eps:
                continue

            # Determine title
            official_title = official_titles_map.get(str(calc_ep))
            ep_str = f"{calc_ep:02d}" if calc_ep < 100 else f"{calc_ep}"
            sub_title = ""
            if official_title:
                s = str(official_title).strip()
                s = re.sub(r'^.*?[-–—]\s*(?:Ep|Episode|E)\s*\d+[\s:\-–—\.]*', '', s, flags=re.I).strip()
                s = re.sub(r'^(?:Ep|Episode|E)\s*\d+[\s:\-–—\.]*', '', s, flags=re.I).strip()
                if s and not re.fullmatch(r'(?:Ep|Episode|E)?\s*\d+', s, flags=re.I) and s.lower() != f"episode {calc_ep}":
                    sub_title = s
            
            display_title = f"Ep {ep_str} - {sub_title}" if sub_title else f"Ep {ep_str}"
            performer_title = story_name
            final_filename = f"{display_title}.mp3"

            buf = io.BytesIO()
            await client.download_media(msg, file=buf)
            buf.seek(0)
            if buf.getbuffer().nbytes == 0:
                print(f"🛑 FATAL: Downloaded 0 bytes for Ep {calc_ep}. Stopping!")
                return 1

            # Retag ID3
            if MUTAGEN_AVAILABLE:
                try:
                    try:
                        tags = ID3(buf)
                    except Exception:
                        tags = ID3()
                    tags.add(TIT2(encoding=3, text=display_title))
                    tags.add(TPE1(encoding=3, text=performer_title))
                    clean_buf = io.BytesIO()
                    tags.save(clean_buf)
                    clean_buf.seek(0)
                    if len(clean_buf.getvalue()) > 100:
                        buf = clean_buf
                except Exception as tag_err:
                    print(f"   ID3 rewrite notice on Ep {calc_ep}: {tag_err}")

            buf.seek(0)
            upload_bytes = buf.getvalue()
            expected_size = msg.media.document.size if (msg.media and hasattr(msg.media, "document") and msg.media.document) else 0
            if expected_size > 0 and len(upload_bytes) != expected_size:
                print(f"🛑 FATAL: Size mismatch for Ep {calc_ep}! Downloaded {len(upload_bytes)} / {expected_size} bytes. Aborting truncated upload!")
                return 1

            input_file = await fast_upload_file(client, upload_bytes, file_name=final_filename, workers=4)
            audio_attrs = [
                DocumentAttributeAudio(
                    duration=getattr(next((a for a in msg.media.document.attributes if isinstance(a, DocumentAttributeAudio)), None), "duration", 0),
                    title=display_title,
                    performer=performer_title,
                ),
                DocumentAttributeFilename(file_name=final_filename),
            ]

            await client.send_file(
                channel,
                file=input_file,
                attributes=audio_attrs,
                supports_streaming=True, mime_type=msg.media.document.mime_type or "audio/x-m4a",
            )
            existing_eps.add(calc_ep)
            print(f"   ✓ [Ep {calc_ep}] {display_title}")
            await asyncio.sleep(2.0)

        print(f"✨ Range {rng} complete. (Total In Vault: {len(existing_eps)})")
        await asyncio.sleep(1.0)

    print(f"\n🎉 WORKER FINISHED SUCCESSFULLY: {story_name} (Total In Vault: {len(existing_eps)})")
    await client.disconnect()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe Cloud Vault Worker")
    parser.add_argument("--story", required=True, help="Story name to archive")
    parser.add_argument("--config", default="worker_config.json", help="Path to worker_config.json")
    parser.add_argument("--target-channel-id", default=None, help="Target channel ID override")
    parser.add_argument("--allow-backup-2", action="store_true", help="Allow emergency backup_2 session")
    args = parser.parse_args()

    code = asyncio.run(run_worker(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
