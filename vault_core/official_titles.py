"""Fetch and validate official Pocket FM episode titles from a supplied show ID."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen


ACTION_ID = "40fcf5bff259b98f5b39b1cba3bff405dc326aa82f"


class OfficialTitleError(RuntimeError):
    pass


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _objects_from_response(raw: str):
    candidates = [raw]
    candidates.extend(re.findall(r'\{[^\n]*(?:stories|next_ptr)[^\n]*\}', raw))
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for index, char in enumerate(candidate):
            if char not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            yield from _walk(value)


def _stories_from_response(raw: str) -> tuple[list[dict], int | None]:
    for value in _objects_from_response(raw):
        stories = value.get("stories")
        if isinstance(stories, list) and stories:
            next_ptr = value.get("next_ptr")
            return stories, int(next_ptr) if str(next_ptr).isdigit() else None
        result = value.get("result")
        if isinstance(result, dict) and isinstance(result.get("stories"), list):
            stories = result["stories"]
            next_ptr = result.get("next_ptr")
            return stories, int(next_ptr) if str(next_ptr).isdigit() else None
    raise OfficialTitleError("Pocket FM response contained no episode list")


def fetch_official_titles(show_id: str, page_size: int = 50) -> dict[str, str]:
    """Fetch pages and reject missing/duplicate/non-continuous official episode numbers."""
    url = f"https://pocketfm.com/show/{show_id}"
    pointer = 0
    visited: set[int] = set()
    titles: dict[int, str] = {}
    while pointer not in visited:
        visited.add(pointer)
        payload = json.dumps([{
            "showId": show_id, "campaignName": "", "currPtr": pointer, "pageSize": page_size
        }]).encode()
        request = Request(url, data=payload, method="POST", headers={
            "User-Agent": "Mozilla/5.0",
            "Next-Action": ACTION_ID,
            "Accept": "text/x-component",
            "Content-Type": "application/json",
        })
        with urlopen(request, timeout=30) as response:
            stories, next_pointer = _stories_from_response(response.read().decode("utf-8", "replace"))
        for story in stories:
            number = story.get("natural_sequence_number", story.get("seq_number"))
            title = story.get("title", story.get("episode_title", story.get("name")))
            if number is None or not isinstance(title, str) or not title.strip():
                raise OfficialTitleError("Pocket FM returned an episode without a number or official title")
            number = int(number)
            normalized = f"E{number}. {title.strip()}"
            prior = titles.get(number)
            if prior and prior != normalized:
                raise OfficialTitleError(f"conflicting official titles for episode {number}")
            titles[number] = normalized
        if next_pointer is None or next_pointer <= pointer:
            break
        pointer = next_pointer
        time.sleep(0.25)
    expected = list(range(1, len(titles) + 1))
    if sorted(titles) != expected:
        raise OfficialTitleError("official titles are incomplete or non-continuous; no map was written")
    return {str(number): titles[number] for number in expected}


def save_official_titles(show_id: str, output: Path) -> int:
    titles = fetch_official_titles(show_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(titles, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(titles)
