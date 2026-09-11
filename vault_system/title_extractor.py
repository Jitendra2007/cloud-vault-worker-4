"""Fetch and validate official Pocket FM episode titles.

This module never invents fallback titles. A map is written only when the
returned episode numbers are continuous from 1 through the final episode.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
from pathlib import Path
from typing import Any

ACTION_ID = "40fcf5bff259b98f5b39b1cba3bff405dc326aa82f"
PAGE_SIZE = 50
TITLE_PATTERN = re.compile(
    r'"(?:story_title|title|name)"\s*:\s*"((?:\\.|[^"\\])*)"'
)


def _decode_json_string(value: str) -> str:
    try:
        return json.loads('"' + value + '"')
    except json.JSONDecodeError:
        return html.unescape(value.replace('\\"', '"').replace('\\n', ' '))


def _extract_stories(response_text: str) -> dict[int, str]:
    response_text = response_text.replace('\\"', '"')
    found: dict[int, str] = {}
    for match in TITLE_PATTERN.finditer(response_text):
        title = _decode_json_string(match.group(1)).strip()
        parsed = _parse_episode_title(title)
        if parsed and parsed[0] not in found:
            found[parsed[0]] = parsed[1]
    return found


def _extract_declared_total(response_text: str) -> int | None:
    normalized = response_text.replace('\\"', '"')
    totals = [
        int(value)
        for value in re.findall(r'"episodes_count"\s*:\s*(\d+)', normalized)
        if int(value) > 0
    ]
    return max(totals) if totals else None


def _validate_contiguous(titles: dict[int, str]) -> None:
    if not titles:
        raise RuntimeError("Pocket FM returned no episode stories")
    numbers = sorted(titles)
    expected = list(range(1, numbers[-1] + 1))
    if numbers != expected:
        missing = sorted(set(expected) - set(numbers))
        raise RuntimeError(
            f"official title map has gaps: missing {missing[:20]}"
            + ("..." if len(missing) > 20 else "")
        )


def _parse_episode_title(value: str) -> tuple[int, str] | None:
    """Return a numbered Pocket FM title without accepting arbitrary text."""
    match = re.match(r"^E(\d+)\.\s+(.+?)\s*$", value)
    if match and match.group(2).strip():
        return int(match.group(1)), value.strip()
    # Pocket FM occasionally publishes a typo such as `Eo 461- Title`.
    # Retain its title text but normalize only the episode prefix so the
    # vault metadata remains compatible with the strict E<number>. format.
    alternate = re.match(r"^E(?:p|o)\s*(\d+)\s*[-.]\s*(.+?)\s*$", value)
    if alternate and alternate.group(2).strip():
        number = int(alternate.group(1))
        return number, f"E{number}. {alternate.group(2).strip()}"
    return None


async def _fetch_with_browser(show_id: str, timeout: float) -> dict[int, str]:
    """Load every range through Pocket FM's own episode selector.

    The public server action ignores a bare HTTP cursor request.  The website
    supplies a Next.js router-state header when a visitor picks a range in the
    episode selector.  Driving that public selector is therefore both more
    accurate and less brittle than guessing private API pagination.
    """
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as error:
        raise RuntimeError(
            "Playwright is required for official Pocket FM title extraction. "
            "Install dependencies and run `python -m playwright install chromium`."
        ) from error

    url = f"https://pocketfm.com/show/{show_id}"
    action_timeout_ms = int(timeout * 1000)
    titles: dict[int, str] = {}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            await page.goto(url, wait_until="networkidle", timeout=action_timeout_ms * 2)
            await page.get_by_test_id("episode-range-trigger").wait_for(
                state="visible", timeout=action_timeout_ms
            )
            trigger_text = await page.get_by_test_id("episode-range-trigger").inner_text()
            total_match = re.search(r"ALL\s+(\d+)\s+EPISODES", trigger_text, re.IGNORECASE)
            if not total_match:
                raise RuntimeError("Pocket FM did not expose the official episode total")
            declared_total = int(total_match.group(1))

            # The first range is rendered in the public page. The virtualized
            # list removes some off-screen anchors, so read its visible text
            # rather than assuming every episode link remains in the DOM.
            initial_titles = (await page.locator("body").inner_text()).splitlines()
            for value in initial_titles:
                parsed = _parse_episode_title(value)
                if parsed:
                    titles[parsed[0]] = parsed[1]

            if set(range(1, min(20, declared_total) + 1)) - set(titles):
                raise RuntimeError("Pocket FM initial episode range was incomplete")

            if declared_total > 20:
                # Make one real selector request. It gives us the public action
                # and router-state headers that Pocket FM currently requires.
                # Subsequent ranges use that same documented-in-page request
                # shape directly, avoiding dozens of slow UI interactions.
                await page.get_by_test_id("episode-range-trigger").click()
                await page.get_by_role("button", name="21-40", exact=True).click()

                def is_seed_request(response: Any) -> bool:
                    request = response.request
                    return (
                        request.method == "POST"
                        and request.headers.get("next-action") == ACTION_ID
                        and '"currPtr":20' in (request.post_data or "")
                    )

                try:
                    async with page.expect_response(
                        is_seed_request, timeout=action_timeout_ms
                    ) as response_info:
                        await page.get_by_role("button", name="Okay", exact=True).click()
                    seed_response = await response_info.value
                    seed_text = await seed_response.text()
                except PlaywrightTimeoutError as error:
                    raise RuntimeError("Pocket FM did not load official range 21-40") from error

                seed_titles = _extract_stories(seed_text)
                if not set(range(21, min(40, declared_total) + 1)).issubset(seed_titles):
                    raise RuntimeError("Pocket FM range 21-40 was incomplete")
                titles.update(seed_titles)

                captured_headers = await seed_response.request.all_headers()
                action_headers = {
                    key: value
                    for key, value in captured_headers.items()
                    if not key.startswith(":")
                    and key
                    in {
                        "accept",
                        "content-type",
                        "next-action",
                        "next-router-state-tree",
                        "origin",
                        "rsc",
                        "user-agent",
                    }
                }
                if action_headers.get("next-action") != ACTION_ID:
                    raise RuntimeError("Pocket FM did not provide the official episode action")

                for start in range(41, declared_total + 1, 20):
                    end = min(start + 19, declared_total)
                    label = f"{start}-{end}"
                    payload = json.dumps([{
                        "showId": show_id,
                        "campaignName": "",
                        "currPtr": start - 1,
                        "pageSize": 20,
                    }])
                    response = await page.context.request.post(
                        url,
                        headers=action_headers,
                        data=payload,
                        timeout=action_timeout_ms,
                    )
                    if response.status != 200:
                        raise RuntimeError(
                            f"Pocket FM range {label} returned HTTP {response.status}"
                        )
                    page_titles = _extract_stories(await response.text())
                    expected = set(range(start, end + 1))
                    if set(page_titles) != expected:
                        raise RuntimeError(
                            f"Pocket FM range {label} was incomplete or mismatched; "
                            f"received {sorted(page_titles)}"
                        )
                    titles.update(page_titles)
        finally:
            await browser.close()

    if len(titles) != declared_total:
        raise RuntimeError(
            f"Pocket FM returned {len(titles)} titles but declares "
            f"{declared_total} episodes; refusing to save a partial map"
        )
    _validate_contiguous(titles)
    return dict(sorted(titles.items()))


def fetch_official_titles(
    show_id: str,
    *,
    page_size: int = PAGE_SIZE,
    max_pages: int = 2000,
    timeout: float = 30.0,
) -> dict[int, str]:
    """Fetch every official title through Pocket FM's public web interface."""
    show_id = show_id.strip()
    if not re.fullmatch(r"[a-f0-9]{40}", show_id, re.IGNORECASE):
        raise ValueError("show_id must be Pocket FM's 40-character hexadecimal ID")

    # Kept as compatible keyword arguments for existing callers. Pocket FM's
    # selector controls 20-title ranges, so callers cannot tune it safely.
    del page_size, max_pages
    return asyncio.run(_fetch_with_browser(show_id, timeout))


def write_title_map(show_id: str, output: Path, **kwargs: Any) -> dict[int, str]:
    titles = fetch_official_titles(show_id, **kwargs)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = {str(number): title for number, title in titles.items()}
    output.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return titles


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch verified Pocket FM titles")
    parser.add_argument("--show-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    titles = write_title_map(args.show_id, args.output)
    print(f"saved {len(titles)} verified titles to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
