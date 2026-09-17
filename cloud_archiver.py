import sys
import io
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')
import asyncio
import io
import os
import sys
import json
import re
import time
import argparse
import urllib.parse

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.functions.channels import CreateChannelRequest, EditPhotoRequest, DeleteChannelRequest
from telethon.tl.types import (
    MessageMediaDocument, 
    DocumentAttributeAudio, 
    DocumentAttributeFilename,
    InputChatUploadedPhoto,
    InputDocumentFileLocation
)
from parallel_transfer import fast_download_file, fast_upload_file

API_ID = int(os.environ.get('API_ID') or os.environ.get('TELEGRAM_API_ID', 0))
API_HASH = os.environ.get('API_HASH') or os.environ.get('TELEGRAM_API_HASH', '')

HARVESTER_SESSION = os.environ.get('HARVESTER_SESSION') or os.environ.get('TELEGRAM_STRING_SESSION_HARVESTER') or os.environ.get('VAULT_SESSION') or ''
VAULT_SESSION = os.environ.get('VAULT_SESSION') or os.environ.get('TELEGRAM_STRING_SESSION_VAULT') or ''
BACKUP_HARVESTER_SESSION = os.environ.get('HARVESTER_SESSION_BACKUP') or os.environ.get('TELEGRAM_STRING_SESSION_HARVESTER_BACKUP') or ''
BACKUP_VAULT_SESSION = os.environ.get('VAULT_SESSION_BACKUP') or os.environ.get('TELEGRAM_STRING_SESSION_VAULT_BACKUP') or ''

if not VAULT_SESSION and os.path.exists('master_vault_session.txt'):
    with open('master_vault_session.txt', 'r', encoding='utf-8') as f:
        VAULT_SESSION = f.read().strip()

if not VAULT_SESSION:
    try:
        from resilient_session_manager import ResilientSessionManager
        _rsm = ResilientSessionManager()
        _cands = _rsm.candidates("vault")
        if _cands:
            VAULT_SESSION = _rsm._read_session(_cands[0])
            if len(_cands) > 1:
                BACKUP_VAULT_SESSION = _rsm._read_session(_cands[1])
    except Exception:
        pass

if not HARVESTER_SESSION:
    try:
        from resilient_session_manager import ResilientSessionManager
        _rsm = ResilientSessionManager()
        _cands = _rsm.candidates("main")
        if _cands:
            HARVESTER_SESSION = _rsm._read_session(_cands[0])
            if len(_cands) > 1:
                BACKUP_HARVESTER_SESSION = _rsm._read_session(_cands[1])
    except Exception:
        pass

if not HARVESTER_SESSION:
    HARVESTER_SESSION = VAULT_SESSION

WORKER_ID = int(os.environ.get('WORKER_ID', 1))

def parse_bot_link(bot_url):
    m = re.search(r'(?:telegram\.me|t\.me)/([A-Za-z0-9_]+)\?start=([A-Za-z0-9_%+/=\-]+)', str(bot_url), re.IGNORECASE)
    if m:
        return m.group(1), urllib.parse.unquote(m.group(2)).strip()
    return None, None

def parse_ep_range(range_str):
    m = re.search(r'\[?(\d+)\s*[-–]\s*(\d+)\]?', str(range_str))
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None

def parse_ep_num(text):
    s = str(text)
    m = re.search(r'(?:ep|episode|chapter)\s*[-_–:\s]*(\d+)', s, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m2 = re.search(r'(?:GE|DD|LotB|TOR|LB|RotB|BSSIL|MAS|TEP|[A-Za-z]{2,6})\s*[-_–:\s]*(\d+)', s, re.IGNORECASE)
    if m2:
        return int(m2.group(1))
    m3 = re.search(r'^\s*(\d+)\s*[-_–.]', s)
    if m3:
        return int(m3.group(1))
    m4 = re.search(r'\[(\d+)\]', s)
    if m4:
        return int(m4.group(1))
    m5 = re.search(r'\b(\d+)\b', s)
    if m5 and 1 <= int(m5.group(1)) <= 15000:
        return int(m5.group(1))
    return None

def clean_audio_title(raw_title):
    if not raw_title:
        return ""
    cleaned = re.sub(r'@[A-Za-z0-9_]+', '', str(raw_title))
    cleaned = re.sub(r'\(TG\)|\(Telegram\)|by\s+AudioVerse|Audio_Verse|AudioVerseNetwork', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\.m4a|\.mp3|\.mp4|\.aac|\.ogg', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'^(?:GE|DD|LotB|TOR|LB|RotB|BSSIL|MAS|TEP|Ep|Episode)\s*[-_–:]*\s*\d+\s*[-_–:]*\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'ENCODED_PFM_Daily', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'by\s*$', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" -_–\t\n")


SPECIAL_TOKENS = {
    "My Assassin System": {
        386: ("RayTalenT_Bot", "Z2V0LTcyNzczODIzOTg4NzQ4NTY0")
    }
}

def record_pending_backfill(entry):
    path = "pending_backfill.json"
    data = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
    if isinstance(data, list):
        # Avoid duplicate entries
        exists = any(x.get("story") == entry.get("story") and x.get("episode") == entry.get("episode") for x in data)
        if not exists:
            data.append(entry)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"📋 Logged Ep {entry.get('episode')} to {path} for future backfill.")

async def harvest_from_bot(harvester_client, vault_client, bot_username, start_token, expected_count=0, label=""):
    download_client = harvester_client
    if not harvester_client.is_connected():
        await harvester_client.connect()
    bot_entity = await harvester_client.get_input_entity(bot_username)
    last_msgs = []
    async for m in harvester_client.iter_messages(bot_entity, limit=1):
        last_msgs.append(m)
    last_id = last_msgs[0].id if last_msgs else 0

    for send_attempt in range(3):
        try:
            await harvester_client.send_message(bot_entity, f"/start {start_token}")
            break
        except FloodWaitError as fwe:
            print(f"⏳ Telegram FloodWait on Primary Harvester for {label}: Sleeping {fwe.seconds + 5}s (attempt {send_attempt + 1}/3)...")
            await asyncio.sleep(fwe.seconds + 5)

    audio_messages = []
    prev_len = 0
    stable_polls = 0
    retried = False

    for poll_idx in range(35):
        await asyncio.sleep(2.0)
        cur = []
        async for m in harvester_client.iter_messages(bot_entity, limit=50):
            if m.id <= last_id:
                break
            if m.media and hasattr(m.media, 'document') and m.media.document:
                cur.append(m)
        audio_messages = list(reversed(cur))

        # Early exit if full expected batch is already present
        if expected_count > 0 and len(audio_messages) >= expected_count:
            break

        if len(audio_messages) == 0 and poll_idx == 5 and not retried:
            print(f"   Re-sending trigger on Primary Harvester for {label}...")
            try:
                await harvester_client.send_message(bot_entity, f"/start {start_token}")
            except FloodWaitError as fwe:
                print(f"⏳ Telegram FloodWait on re-trigger: Sleeping {fwe.seconds + 5}s...")
                await asyncio.sleep(fwe.seconds + 5)
            retried = True

        # If at least 1 track arrived and count is stable for 3 polls (6s), proceed immediately
        if len(audio_messages) > 0 and len(audio_messages) == prev_len:
            stable_polls += 1
            if stable_polls >= 3:
                break
        else:
            stable_polls = 0
            prev_len = len(audio_messages)

        if len(audio_messages) > 0 and len(audio_messages) == prev_len:
            stable_polls += 1
            threshold = 2 if (expected_count > 0 and len(audio_messages) >= expected_count) else 5
            if stable_polls >= threshold:
                break
        else:
            stable_polls = 0
            prev_len = len(audio_messages)

    # Dual-Account Fallback: If primary account got 0 tracks or fewer than expected, use secondary account
    if (len(audio_messages) == 0 or (expected_count > 0 and len(audio_messages) < expected_count)) and harvester_client != vault_client:
        print(f"⚠️ Primary Harvester received {len(audio_messages)}/{expected_count or '?'} tracks for {label}. Switching to Secondary Account as Harvester Fallback...")
        try:
            if not vault_client.is_connected():
                await vault_client.connect()
            sec_bot_entity = await vault_client.get_input_entity(bot_username)
            sec_last_msgs = []
            async for m in vault_client.iter_messages(sec_bot_entity, limit=1):
                sec_last_msgs.append(m)
            sec_last_id = sec_last_msgs[0].id if sec_last_msgs else 0

            for sec_send_attempt in range(3):
                try:
                    await vault_client.send_message(sec_bot_entity, f"/start {start_token}")
                    break
                except FloodWaitError as fwe:
                    print(f"⏳ Telegram FloodWait on Secondary Harvester for {label}: Sleeping {fwe.seconds + 5}s...")
                    await asyncio.sleep(fwe.seconds + 5)

            sec_prev_len = 0
            sec_stable_polls = 0
            sec_retried = False
            for sec_poll in range(35):
                await asyncio.sleep(2.0)
                sec_cur = []
                async for m in vault_client.iter_messages(sec_bot_entity, limit=50):
                    if m.id <= sec_last_id:
                        break
                    if m.media and hasattr(m.media, 'document') and m.media.document:
                        sec_cur.append(m)

                if expected_count > 0 and len(sec_cur) >= expected_count:
                    audio_messages = list(reversed(sec_cur))
                    download_client = vault_client
                    print(f"   ✓ Secondary Account successfully retrieved {len(audio_messages)} tracks for {label}!")
                    break

                if len(sec_cur) == 0 and sec_poll == 5 and not sec_retried:
                    print(f"   Re-sending trigger on Secondary Account for {label}...")
                    try:
                        await vault_client.send_message(sec_bot_entity, f"/start {start_token}")
                    except FloodWaitError as fwe:
                        print(f"⏳ Telegram FloodWait on secondary re-trigger: Sleeping {fwe.seconds + 5}s...")
                        await asyncio.sleep(fwe.seconds + 5)
                    sec_retried = True

                if expected_count > 0 and len(sec_cur) < expected_count:
                    if sec_poll < 8:
                        sec_prev_len = len(sec_cur)
                        continue

                if len(sec_cur) > 0 and len(sec_cur) == sec_prev_len:
                    sec_stable_polls += 1
                    threshold = 2 if (expected_count > 0 and len(sec_cur) >= expected_count) else 5
                    if sec_stable_polls >= threshold:
                        if len(sec_cur) >= len(audio_messages):
                            audio_messages = list(reversed(sec_cur))
                            download_client = vault_client
                            print(f"   ✓ Secondary Account retrieved {len(audio_messages)} tracks for {label}!")
                        break
                else:
                    sec_stable_polls = 0
                    sec_prev_len = len(sec_cur)
        except Exception as sec_e:
            print(f"⚠️ Secondary Harvester fallback notice: {sec_e}")

    return audio_messages, download_client

async def get_or_create_vault_channel(vault_client, channel_title, cover_path, from_start=False):
    async for dialog in vault_client.iter_dialogs(limit=300):
        if dialog.is_channel and dialog.title.strip().lower() == channel_title.strip().lower():
            if from_start:
                print(f"Reset requested: Deleting existing channel '{dialog.title}' to start fresh from Ep 1...")
                try:
                    await vault_client(DeleteChannelRequest(dialog.entity))
                    await asyncio.sleep(2)
                except Exception as e:
                    print(f"Notice on channel deletion: {e}")
                break
            else:
                print(f"✓ Found existing vault channel: {dialog.title}")
                return dialog.entity

    print(f"Creating fresh clean private vault channel: '{channel_title}'...")
    created = await vault_client(CreateChannelRequest(
        title=channel_title,
        about=f"Official Pocket FM Vault Archive • {channel_title}",
        megagroup=False
    ))
    channel = created.chats[0]
    print(f"✓ Fresh private channel created: ID {channel.id}")

    if cover_path and os.path.exists(cover_path):
        try:
            uploaded_photo = await vault_client.upload_file(cover_path)
            await vault_client(EditPhotoRequest(channel=channel, photo=InputChatUploadedPhoto(file=uploaded_photo)))
            print("✓ Official Pocket FM cover icon set successfully!")
        except Exception as e:
            print(f"⚠️ Photo setting notice: {e}")

    try:
        invite = await vault_client(ExportChatInviteRequest(channel))
        print(f"🔗 VAULT CHANNEL INVITE LINK: {invite.link}")
    except Exception as e:
        print(f"Notice on invite generation: {e}")

    return channel

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--story", type=str, required=False, default="", help="Story name (e.g. 'God Eye')")
    parser.add_argument("--max_batches", type=int, default=0, help="Max batches to process (0 for all)")
    parser.add_argument("--from_start", action="store_true", help="Start fresh from Episode 1")
    parser.add_argument("--slice_idx", type=int, default=0, help="Slice index for parallel harvest (1-based)")
    parser.add_argument("--total_slices", type=int, default=0, help="Total slice count for parallel harvest")
    parser.add_argument("--harvest_to_dir", type=str, default="", help="Directory to save harvested MP3s into")
    parser.add_argument("--stream_from_dir", type=str, default="", help="Directory containing pre-harvested MP3s to stream into Vault channel")
    parser.add_argument("--allow_backfill_skip", action="store_true", help="Record stalled DC-timeout episodes to pending_backfill.json and continue pipeline")
    args = parser.parse_args()

    if not args.story and os.path.exists("ongoing_stories.json"):
        try:
            with open("ongoing_stories.json", "r", encoding="utf-8") as f_ong:
                ong_data = json.load(f_ong)
                if ong_data and isinstance(ong_data, list) and len(ong_data) > 0:
                    args.story = ong_data[0].get("name", "")
                    print(f"ℹ️ Auto-selected story from ongoing_stories.json: '{args.story}'")
        except Exception as e_ong:
            print(f"Notice auto-reading ongoing_stories.json: {e_ong}")

    harvester_sess = HARVESTER_SESSION or os.environ.get(f"TELEGRAM_STRING_SESSION_HARVESTER_{args.slice_idx}") or VAULT_SESSION
    if not API_ID or not API_HASH:
        print("❌ Missing required API_ID or API_HASH credentials!")
        sys.exit(1)

    if args.harvest_to_dir and not harvester_sess:
        print("❌ Missing required harvester session credentials!")
        sys.exit(1)

    if args.stream_from_dir and not VAULT_SESSION:
        print("❌ Missing required vault session credentials!")
        sys.exit(1)

    catalog_path = "verified_pocketfm_show_catalog.json"
    catalog = {}
    if os.path.exists(catalog_path):
        with open(catalog_path, "r", encoding="utf-8") as f:
            catalog = json.load(f)

    story_info = None
    target_clean = re.sub(r"['’`:;\(\)\s\-_]+", "", args.story.lower())
    for k, v in catalog.items():
        off_clean = re.sub(r"['’`:;\(\)\s\-_]+", "", v.get("official_title", "").lower())
        chn_clean = re.sub(r"['’`:;\(\)\s\-_]+", "", v.get("channel_name", "").lower())
        fld_clean = re.sub(r"['’`:;\(\)\s\-_]+", "", v.get("channel_folder", "").lower())
        if target_clean in off_clean or target_clean in chn_clean or target_clean in fld_clean or off_clean in target_clean:
            story_info = v
            break

    stories_data_dir = "stories_data"
    
    if not story_info and os.path.exists(stories_data_dir):
        # Fallback: scan stories_data directory directly by string similarity
        for d in os.listdir(stories_data_dir):
            d_clean = re.sub(r"['’`:;\(\)\s\-_]+", "", d.lower())
            if target_clean in d_clean or d_clean in target_clean:
                m = re.match(r'^(\d+)_(.+)$', d)
                cid_str = m.group(1) if m else "0"
                cname = m.group(2) if m else d
                story_info = {
                    "channel_id": f"-100{cid_str}",
                    "channel_folder": d,
                    "channel_name": cname,
                    "official_title": args.story,
                    "status": "AUTO_MATCHED"
                }
                break

    if not story_info:
        print(f"❌ Story '{args.story}' not found in catalog or stories_data!")
        sys.exit(1)

    official_title = story_info.get("official_title", args.story)
    
    # Locate story folder
    matched_dirs = []
    if "channel_folder" in story_info and os.path.exists(os.path.join(stories_data_dir, story_info["channel_folder"])):
        matched_dirs = [story_info["channel_folder"]]
    else:
        matched_dirs = [d for d in os.listdir(stories_data_dir) if any(term in d.lower() for term in [official_title.lower(), story_info.get("channel_name", "")[:10].lower()])]
        if not matched_dirs and "channel_id" in story_info:
            cid_clean = str(story_info["channel_id"]).replace("-100", "")
            matched_dirs = [d for d in os.listdir(stories_data_dir) if d.startswith(cid_clean)]

    folder_path = os.path.join(stories_data_dir, matched_dirs[0])
    cover_jpg = [f for f in os.listdir(folder_path) if f.lower() in ["cover.jpg", "cover.jpeg", "cover.png"]]
    cover_files = cover_jpg or [f for f in os.listdir(folder_path) if f.startswith("cover.")]
    cover_path = os.path.join(folder_path, cover_files[0]) if cover_files else None

    txt_files = [f for f in os.listdir(folder_path) if f.endswith(".txt")]
    txt_path = os.path.join(folder_path, txt_files[0])

    # 📖 Load Official Titles Map
    official_titles_map = {}
    official_titles_path = os.path.join(folder_path, "official_titles.json")
    if os.path.exists(official_titles_path):
        with open(official_titles_path, "r", encoding="utf-8") as f:
            official_titles_map = json.load(f)
        print(f"📖 Loaded {len(official_titles_map)} official episode titles from: {official_titles_path}")
    else:
        print(f"⚠️ No official_titles.json found in {folder_path}!")

    print("==================================================================")
    print(f"🌟 CLOUD VAULT ARCHIVER: {official_title}")
    print(f"   Folder:  {folder_path}")
    print(f"   Cover:   {cover_path}")
    print(f"   File:    {txt_path}")
    print(f"   Titles:  {len(official_titles_map)} official titles available")
    print("==================================================================")

    batches = []
    with open(txt_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("="):
                continue
            parts = [p.strip() for p in line.split("->")]
            if len(parts) >= 6:
                rng_label = parts[3]
                bot_url = parts[5]
                s_ep, e_ep = parse_ep_range(rng_label)
                if s_ep and e_ep and any(d in bot_url for d in ["t.me", "telegram.me"]):
                    batches.append({
                        "start_ep": s_ep,
                        "end_ep": e_ep,
                        "range_label": rng_label,
                        "bot_url": bot_url
                    })

    batches.sort(key=lambda x: x["start_ep"])
    print(f"✓ Found {len(batches)} batches (Episodes {batches[0]['start_ep']} to {batches[-1]['end_ep']})")

    harvester_session_str = HARVESTER_SESSION
    if args.slice_idx > 0:
        slice_session = os.environ.get(f"TELEGRAM_STRING_SESSION_HARVESTER_{args.slice_idx}")
        if slice_session:
            harvester_session_str = slice_session
            print(f"🔑 Using dedicated StringSession for Parallel Worker {args.slice_idx}")

    if args.harvest_to_dir:
        harvester_client = TelegramClient(StringSession(harvester_session_str), API_ID, API_HASH, receive_updates=False)
        await harvester_client.connect()
        h_me = await harvester_client.get_me()
        print(f"✓ Cloud Harvester (Slice {args.slice_idx}): {h_me.first_name} (+{h_me.phone})")
        vault_client = harvester_client
    elif args.stream_from_dir:
        vault_client = TelegramClient(StringSession(VAULT_SESSION), API_ID, API_HASH, receive_updates=False)
        await vault_client.connect()
        v_me = await vault_client.get_me()
        if v_me is None:
            print("❌ VAULT_SESSION is not an authorized Telegram StringSession. Update the GitHub secret and rerun.")
            await vault_client.disconnect()
            sys.exit(1)
        print(f"✓ Cloud Vault Streaming Client: {v_me.first_name} (+{v_me.phone})")
        harvester_client = vault_client
    else:
        vault_client = TelegramClient(StringSession(VAULT_SESSION), API_ID, API_HASH, receive_updates=False)
        await vault_client.connect()
        v_me = await vault_client.get_me()
        if v_me is None:
            print("❌ VAULT_SESSION is not an authorized Telegram StringSession. Update the GitHub secret and rerun.")
            await vault_client.disconnect()
            sys.exit(1)
        print(f"✓ Cloud Vault Owner (+{v_me.phone} {v_me.first_name}): Connected")

        sub_session_str = os.environ.get('TELEGRAM_STRING_SESSION_SUB') or ''
        if harvester_session_str and harvester_session_str != VAULT_SESSION:
            try:
                harvester_client = TelegramClient(StringSession(harvester_session_str), API_ID, API_HASH, receive_updates=False)
                await harvester_client.connect()
                h_me = await harvester_client.get_me()
                print(f"✓ Cloud Harvester (+{h_me.first_name} +{h_me.phone}): Connected")
            except Exception as h_err:
                print(f"⚠️ Primary Harvester client skipped/failed ({h_err}).")
                if sub_session_str and sub_session_str != VAULT_SESSION:
                    try:
                        print("🔄 Trying Secondary Harvester Account (SUB)...")
                        harvester_client = TelegramClient(StringSession(sub_session_str), API_ID, API_HASH, receive_updates=False)
                        await harvester_client.connect()
                        h_me = await harvester_client.get_me()
                        print(f"✓ Cloud Harvester SUB Account (+{h_me.first_name} +{h_me.phone}): Connected")
                    except Exception as sub_err:
                        print(f"⚠️ Secondary Harvester (SUB) skipped/failed ({sub_err}). Using Vault Client for harvesting...")
                        harvester_client = vault_client
                else:
                    print("Using Vault Client for harvesting...")
                    harvester_client = vault_client
        elif sub_session_str and sub_session_str != VAULT_SESSION:
            try:
                print("🔄 Connecting Secondary Harvester Account (SUB)...")
                harvester_client = TelegramClient(StringSession(sub_session_str), API_ID, API_HASH, receive_updates=False)
                await harvester_client.connect()
                h_me = await harvester_client.get_me()
                print(f"✓ Cloud Harvester SUB Account (+{h_me.first_name} +{h_me.phone}): Connected")
            except Exception as sub_err:
                print(f"⚠️ Secondary Harvester (SUB) skipped/failed ({sub_err}). Using Vault Client for harvesting...")
                harvester_client = vault_client
        else:
            harvester_client = vault_client

    # ─── MODE A: STREAM PRE-HARVESTED FILES TO VAULT (STRICT 100% NUMERICAL ORDER) ───
    if args.stream_from_dir:
        print(f"🌊 STREAMING MODE: Collecting pre-harvested MP3s from '{args.stream_from_dir}'...")
        harvested_items = []
        for root, dirs, files in os.walk(args.stream_from_dir):
            for file in files:
                if file.endswith(".json") and file.startswith("Ep_"):
                    json_path = os.path.join(root, file)
                    mp3_path = json_path[:-5] + ".mp3"
                    if os.path.exists(mp3_path):
                        try:
                            with open(json_path, "r", encoding="utf-8") as f:
                                meta = json.load(f)
                                meta["mp3_path"] = mp3_path
                                harvested_items.append(meta)
                        except Exception as e:
                            print(f"Notice on reading sidecar {json_path}: {e}")

        if not harvested_items:
            print(f"⚠️ No harvested MP3 sidecars found in {args.stream_from_dir}!")
            sys.exit(1)

        # Sort ALL harvested files in STRICT 100% NUMERICAL ORDER
        harvested_items.sort(key=lambda x: int(x["calc_ep"]))
        print(f"📦 Found {len(harvested_items)} harvested tracks (Strict Range: Ep {harvested_items[0]['calc_ep']} to Ep {harvested_items[-1]['calc_ep']})")

        channel_title = official_title
        vault_channel = await get_or_create_vault_channel(vault_client, channel_title, cover_path, from_start=args.from_start)

        uploaded_episodes = set()
        print("Scanning vault channel for existing episodes...")
        async for msg in vault_client.iter_messages(vault_channel, limit=None):
            if msg.media and isinstance(msg.media, MessageMediaDocument):
                for attr in msg.media.document.attributes:
                    if isinstance(attr, DocumentAttributeAudio) and attr.title:
                        ep = parse_ep_num(attr.title)
                        if ep: uploaded_episodes.add(ep)

        print(f"📊 Current Vault Count: {len(uploaded_episodes)} episodes")

        cover_input = None
        if cover_path and os.path.exists(cover_path):
            try:
                print("Pre-uploading cover artwork thumbnail to Telegram...")
                cover_input = await vault_client.upload_file(cover_path)
                print("✓ Cover artwork pre-uploaded.")
            except Exception as e:
                print(f"Notice on cover pre-upload: {e}")

        total_new = 0
        for item in harvested_items:
            calc_ep = int(item["calc_ep"])
            if calc_ep in uploaded_episodes:
                continue

            display_title = item.get("display_title", f"Ep {calc_ep}")
            performer_title = official_title
            final_filename = f"{display_title}.mp3"

            input_file = await vault_client.upload_file(item["mp3_path"], file_name=final_filename)

            audio_attrs = [
                DocumentAttributeAudio(
                    duration=item.get("duration", 0),
                    title=display_title,
                    performer=performer_title
                ),
                DocumentAttributeFilename(file_name=final_filename)
            ]

            await vault_client.send_file(
                vault_channel,
                file=input_file,
                thumb=cover_input or (cover_path if cover_path and os.path.exists(cover_path) else None),
                caption="",
                attributes=audio_attrs,
                supports_streaming=True, mime_type=msg.media.document.mime_type or "audio/x-m4a"
            )
            uploaded_episodes.add(calc_ep)
            total_new += 1
            print(f"   ✓ [Ep {calc_ep}] Streamed to Vault: {display_title}")
            await asyncio.sleep(0.3)

        print(f"🎉 STREAMING COMPLETE FOR: {official_title} (Total In Vault: {len(uploaded_episodes)})")
        if harvester_client != vault_client:
            await harvester_client.disconnect()
        await vault_client.disconnect()
        sys.exit(0)

    # ─── MODE B: PARALLEL HARVEST SLICE TO DISK ───
    if args.harvest_to_dir:
        os.makedirs(args.harvest_to_dir, exist_ok=True)
        if args.slice_idx > 0 and args.total_slices > 0:
            batches = [b for idx, b in enumerate(batches) if idx % args.total_slices == (args.slice_idx - 1)]
            print(f"🔪 Slice {args.slice_idx}/{args.total_slices}: Assigned {len(batches)} batches to harvest...")

        for b_idx, batch in enumerate(batches, 1):
            s_ep, e_ep = batch["start_ep"], batch["end_ep"]
            rng, bot_url = batch["range_label"], batch["bot_url"]
            bot_username, start_token = parse_bot_link(bot_url)
            if not bot_username or not start_token:
                continue

            expected_batch_count = (e_ep - s_ep + 1)
            print(f"\n⚡ [{b_idx}/{len(batches)}] Harvesting @{bot_username} for Range {rng} (Slice {args.slice_idx}/{args.total_slices})...")
            
            audio_messages, download_client = await harvest_from_bot(
                harvester_client, vault_client, bot_username, start_token,
                expected_count=expected_batch_count,
                label=f"Range {rng}"
            )

            if not audio_messages:
                print(f"⚠️ 0 tracks received for Range {rng} in slice {args.slice_idx}")
                continue

            # Deduplicate & sort
            def extract_msg_ep(m, fallback_idx):
                for a in m.media.document.attributes:
                    if isinstance(a, DocumentAttributeFilename) and a.file_name:
                        ep = parse_ep_num(a.file_name)
                        if ep: return ep
                    if isinstance(a, DocumentAttributeAudio) and a.title:
                        ep = parse_ep_num(a.title)
                        if ep: return ep
                return s_ep + fallback_idx

            unique_tracks = {}
            for idx, m in enumerate(audio_messages):
                calc = getattr(m, '_calc_ep', extract_msg_ep(m, idx))
                dur = 0
                for a in m.media.document.attributes:
                    if isinstance(a, DocumentAttributeAudio): dur = a.duration or 0
                if calc not in unique_tracks or dur > getattr(unique_tracks[calc], '_dur', 0):
                    m._calc_ep = calc
                    m._dur = dur
                    unique_tracks[calc] = m

            audio_messages = sorted(list(unique_tracks.values()), key=lambda m: m._calc_ep)

            for a_idx, msg in enumerate(audio_messages):
                raw_filename = ""
                raw_title = ""
                duration = 0
                for attr in msg.media.document.attributes:
                    if isinstance(attr, DocumentAttributeFilename): raw_filename = attr.file_name
                    elif isinstance(attr, DocumentAttributeAudio):
                        duration = attr.duration or 0
                        if attr.title: raw_title = attr.title

                calc_ep = getattr(msg, '_calc_ep', s_ep + a_idx)

                ep_str = f"{calc_ep:02d}" if calc_ep < 100 else f"{calc_ep}"
                sub_title = ""
                if official_titles_map and str(calc_ep) in official_titles_map:
                    s = str(official_titles_map[str(calc_ep)]).strip()
                    s = re.sub(r'^.*?[-–—]\s*(?:Ep|Episode|E)\s*\d+[\s:\-–—\.]*', '', s, flags=re.I).strip()
                    s = re.sub(r'^(?:Ep|Episode|E)\s*\d+[\s:\-–—\.]*', '', s, flags=re.I).strip()
                    if s and not re.fullmatch(r'(?:Ep|Episode|E)?\s*\d+', s, flags=re.I) and s.lower() != f"episode {calc_ep}":
                        sub_title = s
                elif raw_title or raw_filename:
                    clean_raw = clean_audio_title(raw_title or raw_filename)
                    if clean_raw and not re.fullmatch(r'(?:Ep|Episode|E)?\s*\d+', clean_raw, flags=re.I) and clean_raw.lower() != f"episode {calc_ep}":
                        sub_title = clean_raw
                display_title = f"Ep {ep_str} - {sub_title}" if sub_title else f"Ep {ep_str}"

                buf = io.BytesIO()
                dl_success = False
                for dl_attempt in range(3):
                    try:
                        buf.seek(0)
                        buf.truncate(0)
                        await asyncio.wait_for(download_client.download_media(msg, file=buf), timeout=60.0)
                        dl_success = True
                        break
                    except Exception as dl_err:
                        print(f"   ⚠️ Download attempt {dl_attempt + 1}/3 failed for Ep {calc_ep}: {dl_err}")
                        await asyncio.sleep(2.0)
                if not dl_success:
                    print(f"❌ Skipping Ep {calc_ep} due to persistent download failure")
                    continue
                buf.seek(0)
                try:
                    from mutagen.id3 import ID3, TIT2, TPE1
                    try:
                        tags = ID3(buf)
                    except Exception:
                        tags = ID3()
                    tags.add(TIT2(encoding=3, text=display_title))
                    tags.add(TPE1(encoding=3, text=official_title))
                    clean_buf = io.BytesIO()
                    tags.save(clean_buf)
                    clean_buf.seek(0)
                    if len(clean_buf.getvalue()) > 100:
                        buf = clean_buf
                except Exception:
                    buf.seek(0)

                raw_tmp = os.path.join(args.harvest_to_dir, f"raw_{calc_ep:05d}.tmp")
                with open(raw_tmp, "wb") as out_f:
                    out_f.write(buf.getvalue())

                mp3_path = os.path.join(args.harvest_to_dir, f"Ep_{calc_ep:05d}.mp3")
                ff_cmd = [
                    "ffmpeg", "-y", "-i", raw_tmp,
                    "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                    mp3_path
                ]
                res = subprocess.run(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if res.returncode == 0 and os.path.exists(mp3_path):
                    try:
                        from mutagen.id3 import ID3, TIT2, TPE1, APIC
                        try: tags = ID3(mp3_path)
                        except Exception: tags = ID3()
                        tags.add(TIT2(encoding=3, text=display_title))
                        tags.add(TPE1(encoding=3, text=official_title))
                        if cover_path and os.path.exists(cover_path):
                            with open(cover_path, "rb") as img_f:
                                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img_f.read()))
                        tags.save(mp3_path, v2_version=3)
                    except Exception as tag_err:
                        print(f"Notice on tagging: {tag_err}")
                else:
                    with open(mp3_path, "wb") as out_f:
                        out_f.write(buf.getvalue())

                if os.path.exists(raw_tmp):
                    try: os.remove(raw_tmp)
                    except Exception: pass

                meta_path = os.path.join(args.harvest_to_dir, f"Ep_{calc_ep:05d}.json")
                meta_data = {
                    "calc_ep": calc_ep,
                    "display_title": display_title,
                    "official_title": official_title,
                    "duration": duration
                }
                with open(meta_path, "w", encoding="utf-8") as meta_f:
                    json.dump(meta_data, meta_f, indent=2, ensure_ascii=False)

                print(f"   ✓ Harvested & Saved [Ep {calc_ep}] -> {mp3_path}")

        print(f"✨ HARVEST SLICE {args.slice_idx}/{args.total_slices} COMPLETE!")
        if harvester_client and harvester_client.is_connected():
            await harvester_client.disconnect()
        sys.exit(0)

    channel_title = official_title
    vault_channel = await get_or_create_vault_channel(vault_client, channel_title, cover_path, from_start=args.from_start)

    # Pre-upload cover thumbnail once to save redundant transfers
    cover_input = None
    if cover_path and os.path.exists(cover_path):
        try:
            print("Pre-uploading cover artwork thumbnail to Telegram...")
            cover_input = await vault_client.upload_file(cover_path)
            print("✓ Cover artwork pre-uploaded.")
        except Exception as e:
            print(f"Notice on cover pre-upload: {e}")

    uploaded_episodes = set()
    print("Scanning vault channel for existing episodes...")
    async for msg in vault_client.iter_messages(vault_channel, limit=None):
        if msg.media and isinstance(msg.media, MessageMediaDocument):
            for attr in msg.media.document.attributes:
                if isinstance(attr, DocumentAttributeAudio) and attr.title:
                    ep = parse_ep_num(attr.title)
                    if ep:
                        uploaded_episodes.add(ep)
                elif isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                    ep = parse_ep_num(attr.file_name)
                    if ep:
                        uploaded_episodes.add(ep)

    print(f"📊 Current Vault Count: {len(uploaded_episodes)} episodes")

    max_batch_ep = max([b["end_ep"] for b in batches]) if batches else 0
    max_titles_ep = max(map(int, official_titles_map.keys())) if official_titles_map else 0
    max_expected_ep = max(max_batch_ep, max_titles_ep)
    print(f"🎯 Target Series Episodes: {max_expected_ep}")

    processed = 0
    total_new = 0

    for b_idx, batch in enumerate(batches, 1):
        if args.max_batches > 0 and processed >= args.max_batches:
            print(f"Reached batch limit {args.max_batches}. Stopping.")
            break

        s_ep, e_ep = batch["start_ep"], batch["end_ep"]
        rng, bot_url = batch["range_label"], batch["bot_url"]

        if set(range(s_ep, e_ep + 1)).issubset(uploaded_episodes):
            continue

        bot_username, start_token = parse_bot_link(bot_url)
        if not bot_username or not start_token:
            continue

        expected_batch_count = (e_ep - s_ep + 1)
        harvest_start_time = time.time()  # Timer ONLY covers harvest phase
        batch_success = False
        harvest_completed = False

        for batch_attempt in range(1, 15):
            harvest_elapsed = time.time() - harvest_start_time
            if not harvest_completed and harvest_elapsed >= 300:
                print(f"\n🛑 FATAL: Batch {rng} harvest exceeded 5-minute limit ({harvest_elapsed:.1f}s).")
                print("   Could not receive tracks from bot. Stopping to prevent channel gaps!")
                if harvester_client != vault_client:
                    await harvester_client.disconnect()
                await vault_client.disconnect()
                sys.exit(1)

            try:
                print(f"\n⚡ [{b_idx}/{len(batches)}] Triggering @{bot_username} for Range {rng} (attempt {batch_attempt}, expecting {expected_batch_count} tracks, harvest_elapsed {harvest_elapsed:.0f}s/300s)...")
                audio_messages, download_client = await harvest_from_bot(
                    harvester_client, vault_client, bot_username, start_token,
                    expected_count=expected_batch_count,
                    label=f"Range {rng}"
                )

                if not audio_messages or (expected_batch_count > 0 and len(audio_messages) < expected_batch_count):
                    received_count = len(audio_messages) if audio_messages else 0
                    print(f"⚠️ Incomplete tracks received for Range {rng} ({received_count}/{expected_batch_count}) on attempt {batch_attempt}")
                    if received_count > 0 and batch_attempt >= 2:
                        print(f"   ⚠️ Accepting {received_count} available tracks for Range {rng} after attempt {batch_attempt}...")
                        harvest_completed = True
                    elif received_count == 0 and batch_attempt >= 3:
                        print(f"   ⚠️ Range {rng} token returned 0 tracks after 3 attempts (link expired/invalid). Skipping Range {rng} to continue story pipeline!")
                        harvest_completed = True
                        break
                    else:
                        if time.time() - harvest_start_time >= 120:
                            print(f"\n⚠️ Range {rng} harvest exceeded 2-minute limit ({harvest_elapsed:.1f}s). Skipping range to continue pipeline!")
                            harvest_completed = True
                            break
                        print(f"   Waiting 5s before retrying Range {rng}...")
                        await asyncio.sleep(5.0)
                        continue

                # ✅ All tracks received — stop the harvest timer, upload is now free to run
                harvest_completed = True
                harvest_elapsed_final = time.time() - harvest_start_time
                print(f"   ✅ All {len(audio_messages)} tracks received in {harvest_elapsed_final:.1f}s. Starting upload (no time limit)...")

                def extract_msg_ep(m, fallback_idx):
                    for a in m.media.document.attributes:
                        if isinstance(a, DocumentAttributeFilename) and a.file_name:
                            ep = parse_ep_num(a.file_name)
                            if ep: return ep
                        if isinstance(a, DocumentAttributeAudio) and a.title:
                            ep = parse_ep_num(a.title)
                            if ep: return ep
                    return s_ep + fallback_idx

                # Check special missing episodes for this story (e.g. Ep 386 for My Assassin System)
                story_spec = SPECIAL_TOKENS.get(official_title, {})
                for spec_ep, (spec_bot, spec_token) in story_spec.items():
                    if s_ep <= spec_ep <= e_ep:
                        already_present = any(extract_msg_ep(m, 0) == spec_ep for m in audio_messages)
                        if not already_present:
                            print(f"  🔍 Ep {spec_ep} missing in batch {rng}. Querying dedicated sub-token from @{spec_bot}...")
                            spec_msgs, spec_dl = await harvest_from_bot(harvester_client, vault_client, spec_bot, spec_token, expected_count=1, label=f"Ep {spec_ep}")
                            if spec_msgs:
                                m_spec = spec_msgs[0]
                                m_spec._calc_ep = spec_ep
                                audio_messages.append(m_spec)
                                print(f"  ✓ Injected missing Ep {spec_ep} into batch!")

                # Deduplicate tracks by calculated episode number
                unique_tracks = {}
                for idx, m in enumerate(audio_messages):
                    if hasattr(m, '_calc_ep'):
                        calc = m._calc_ep
                    else:
                        calc = extract_msg_ep(m, idx)
                    if not (s_ep - 5 <= calc <= e_ep + 10):
                        calc = s_ep + idx
                    if max_expected_ep > 0 and calc > max_expected_ep:
                        continue
                    dur = 0
                    for a in m.media.document.attributes:
                        if isinstance(a, DocumentAttributeAudio):
                            dur = a.duration or 0
                    if calc not in unique_tracks or dur > getattr(unique_tracks[calc], '_dur', 0):
                        m._calc_ep = calc
                        m._dur = dur
                        unique_tracks[calc] = m

                audio_messages = sorted(list(unique_tracks.values()), key=lambda m: m._calc_ep)
                print(f"   Received {len(audio_messages)} tracks (Strictly sorted: {[m._calc_ep for m in audio_messages]}). Uploading to Vault...")

                for a_idx, msg in enumerate(audio_messages):
                    raw_filename = ""
                    raw_title = ""
                    duration = 0
                    for attr in msg.media.document.attributes:
                        if isinstance(attr, DocumentAttributeFilename):
                            raw_filename = attr.file_name
                        elif isinstance(attr, DocumentAttributeAudio):
                            duration = attr.duration or 0
                            if attr.title:
                                raw_title = attr.title

                    calc_ep = getattr(msg, '_calc_ep', s_ep + a_idx)
                    if calc_ep in uploaded_episodes:
                        continue

                    ep_str = f"{calc_ep:02d}" if calc_ep < 100 else f"{calc_ep}"
                    sub_title = ""
                    if official_titles_map and str(calc_ep) in official_titles_map:
                        s = str(official_titles_map[str(calc_ep)]).strip()
                        s = re.sub(r'^.*?[-–—]\s*(?:Ep|Episode|E)\s*\d+[\s:\-–—\.]*', '', s, flags=re.I).strip()
                        s = re.sub(r'^(?:Ep|Episode|E)\s*\d+[\s:\-–—\.]*', '', s, flags=re.I).strip()
                        if s and not re.fullmatch(r'(?:Ep|Episode|E)?\s*\d+', s, flags=re.I) and s.lower() != f"episode {calc_ep}":
                            sub_title = s
                    elif raw_title or raw_filename:
                        clean_raw = clean_audio_title(raw_title or raw_filename)
                        if clean_raw and not re.fullmatch(r'(?:Ep|Episode|E)?\s*\d+', clean_raw, flags=re.I) and clean_raw.lower() != f"episode {calc_ep}":
                            sub_title = clean_raw
                    display_title = f"Ep {ep_str} - {sub_title}" if sub_title else f"Ep {ep_str}"
                    official_title = official_titles_map.get(str(calc_ep), "") if official_titles_map else ""
                    performer_title = official_title if official_title else display_title
                    final_filename = f"{display_title}.mp3"

                    # Robust per-episode download and upload with automatic reconnect and retries
                    ep_uploaded = False
                    for ep_attempt in range(1, 6):
                        try:
                            buf = io.BytesIO()
                            active_dl_client = download_client if download_client else (harvester_client if harvester_client and harvester_client.is_connected() else vault_client)
                            for dl_try in range(1, 6):
                                try:
                                    if not active_dl_client.is_connected():
                                        await active_dl_client.connect()
                                    buf.seek(0)
                                    buf.truncate(0)
                                    # Refetch fresh message header from active download client
                                    fresh_msg = await active_dl_client.get_messages(msg.peer_id, ids=msg.id)
                                    target_m = fresh_msg if (fresh_msg and fresh_msg.media) else msg
                                    await asyncio.wait_for(
                                        active_dl_client.download_media(target_m, file=buf),
                                        timeout=180.0
                                    )
                                    if buf.getbuffer().nbytes > 0:
                                        break
                                except FloodWaitError as fwe:
                                    print(f"⏳ Telegram FloodWait during download of Ep {calc_ep}: Sleeping {fwe.seconds + 5}s...")
                                    await asyncio.sleep(fwe.seconds + 5)
                                except (Exception, asyncio.CancelledError, asyncio.TimeoutError) as dl_err:
                                    print(f"   ⚠️ Download attempt {dl_try}/5 for Ep {calc_ep}: {dl_err}")
                                    await asyncio.sleep(4.0 * dl_try)

                            buf.seek(0)
                            raw_bytes = buf.getvalue()
                            if len(raw_bytes) < 500_000:
                                raise Exception(f"Downloaded only {len(raw_bytes)} bytes for Ep {calc_ep} - audio stream is truncated or corrupt!")

                            # 🏷️ REWRITE EMBEDDED ID3 TAGS (SAFE ON-DISK TEMP FILE TO PRESERVE MPEG STREAM)
                            tmp_mp3 = os.path.join("scratch", f"upload_ep_{calc_ep}_{os.getpid()}.mp3")
                            os.makedirs("scratch", exist_ok=True)
                            with open(tmp_mp3, "wb") as f_out:
                                f_out.write(raw_bytes)

                            try:
                                from mutagen.id3 import ID3, TIT2, TPE1, APIC
                                try:
                                    tags = ID3(tmp_mp3)
                                except Exception:
                                    tags = ID3()
                                tags.add(TIT2(encoding=3, text=display_title))
                                tags.add(TPE1(encoding=3, text=performer_title))
                                if cover_path and os.path.exists(cover_path):
                                    try:
                                        with open(cover_path, "rb") as img_f:
                                            img_bytes = img_f.read()
                                        tags.add(APIC(
                                            encoding=3,
                                            mime="image/jpeg",
                                            type=3,
                                            desc="Cover",
                                            data=img_bytes
                                        ))
                                    except Exception:
                                        pass
                                tags.save(tmp_mp3, v2_version=3)
                            except Exception as tag_err:
                                print(f"   Notice on ID3 tag rewrite for Ep {calc_ep}: {tag_err}")

                            # STRICT INTEGRITY CHECK BEFORE UPLOAD
                            final_sz = os.path.getsize(tmp_mp3)
                            if final_sz < 500_000:
                                if os.path.exists(tmp_mp3):
                                    os.remove(tmp_mp3)
                                raise Exception(f"CRITICAL: Final file for Ep {calc_ep} is only {final_sz} bytes (under 500KB)! Aborting upload.")

                            with open(tmp_mp3, "rb") as f_in:
                                upload_bytes = f_in.read()

                            try:
                                if os.path.exists(tmp_mp3):
                                    os.remove(tmp_mp3)
                            except Exception:
                                pass

                            input_file = await asyncio.wait_for(
                                fast_upload_file(vault_client, upload_bytes, file_name=final_filename, workers=4),
                                timeout=120.0
                            )

                            audio_attrs = [
                                DocumentAttributeAudio(
                                    duration=duration,
                                    title=display_title,
                                    performer=performer_title
                                ),
                                DocumentAttributeFilename(file_name=final_filename)
                            ]

                            await asyncio.wait_for(
                                vault_client.send_file(
                                    vault_channel,
                                    file=input_file,
                                    thumb=cover_path if cover_path and os.path.exists(cover_path) else None,
                                    caption="",
                                    attributes=audio_attrs,
                                    supports_streaming=True,
                                    mime_type="audio/mpeg"
                                ),
                                timeout=300.0
                            )
                            ep_uploaded = True
                            break
                        except FloodWaitError as fwe:
                            print(f"⏳ Telegram FloodWait on Ep {calc_ep} upload: Sleeping {fwe.seconds + 5}s...")
                            await asyncio.sleep(fwe.seconds + 5)
                        except (Exception, asyncio.CancelledError, asyncio.TimeoutError) as ep_err:
                            print(f"   ⚠️ Ep {calc_ep} transfer glitch (attempt {ep_attempt}/5): {ep_err}")
                            await asyncio.sleep(3.0 * ep_attempt)

                    if not ep_uploaded:
                        backfill_entry = {
                            "story": official_title,
                            "episode": calc_ep,
                            "title": display_title,
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                            "reason": "Telegram DC storage timeout / read lock"
                        }
                        record_pending_backfill(backfill_entry)
                        if args.allow_backfill_skip:
                            print(f"⚠️ [ZERO-GAP GATE OVERRIDE] Recorded Ep {calc_ep} to pending_backfill.json. Continuing story pipeline...")
                            continue
                        else:
                            raise Exception(f"Failed to upload Ep {calc_ep} after 5 attempts - stopped by Zero-Gap gate to prevent channel corruption.")

                    uploaded_episodes.add(calc_ep)
                    total_new += 1
                    print(f"   ✓ [Ep {calc_ep}] {display_title}")
                    await asyncio.sleep(2.5)

                processed += 1
                batch_success = True
                print(f"✨ Range {rng} complete. (Total In Vault: {len(uploaded_episodes)})")
                await asyncio.sleep(1.0)
                break

            except FloodWaitError as fwe:
                print(f"⏳ Telegram FloodWait on batch {rng}: Sleeping {fwe.seconds + 5} seconds before continuing...")
                await asyncio.sleep(fwe.seconds + 5)
            except Exception as e:
                print(f"❌ Error on batch {rng} (attempt {batch_attempt}/15): {e}")
                await asyncio.sleep(5.0)
                if harvest_elapsed >= 300:
                    print(f"\n🛑 FATAL: Batch {rng} harvest failed after {harvest_elapsed:.1f}s. Stopping to prevent channel gaps!")
                    if harvester_client != vault_client:
                        await harvester_client.disconnect()
                    await vault_client.disconnect()
                    sys.exit(1)
                print(f"   Waiting 20 seconds before retrying batch {rng} (harvest_elapsed {harvest_elapsed:.0f}s)...")
                await asyncio.sleep(20.0)

        if not batch_success:
            print(f"\n🛑 FATAL: Could not complete Batch {rng} within 5 minutes. Halting entire process to prevent channel gaps!")
            if harvester_client != vault_client:
                await harvester_client.disconnect()
            await vault_client.disconnect()
            sys.exit(1)

    print("\n==================================================================")
    print(f"🎉 ARCHIVING STATUS FOR: {official_title}")
    print(f"   Vault Episodes:   {len(uploaded_episodes)}")
    print(f"   Expected Target:  {max_expected_ep}")
    print("==================================================================")

    if len(uploaded_episodes) < max_batch_ep:
        print(f"⚠️ INCOMPLETE ARCHIVING: Vault has {len(uploaded_episodes)} / {max_batch_ep} episodes from available batches. Gaps detected!")
        print(f"   Will NOT record completion to completed_vault_channels.json.")
        if harvester_client != vault_client:
            await harvester_client.disconnect()
        await vault_client.disconnect()
        sys.exit(1)

    if len(uploaded_episodes) < max_expected_ep:
        print(f"✨ ONGOING STORY SYNC COMPLETE: All {len(uploaded_episodes)} available episodes uploaded to Vault (Pocket FM catalog has {max_expected_ep}).")
        print(f"   Recording progress in completed_vault_channels.json and allowing queue to continue.")

    # Record completion
    record = {
        "story": official_title,
        "vault_channel_id": f"-100{vault_channel.id}" if hasattr(vault_channel, 'id') else str(vault_channel),
        "total_episodes": len(uploaded_episodes),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    }
    comp_file = "completed_vault_channels.json"
    comp_data = []
    if os.path.exists(comp_file):
        try:
            with open(comp_file, "r", encoding="utf-8") as f:
                comp_data = json.load(f)
        except Exception:
            comp_data = []
    if isinstance(comp_data, list):
        comp_data = [x for x in comp_data if x.get("story") != official_title]
        comp_data.append(record)
    with open(comp_file, "w", encoding="utf-8") as f:
        json.dump(comp_data, f, indent=2, ensure_ascii=False)
    print(f"💾 Recorded completion to {comp_file}")

    if harvester_client != vault_client:
        await harvester_client.disconnect()
    await vault_client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
