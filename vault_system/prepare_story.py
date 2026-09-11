"""Resolve a story name to its catalog show ID and refresh official titles."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .title_extractor import write_title_map


def _clean(value: str) -> str:
    return re.sub(r"['’\s\-_]+", "", value.lower())


def prepare_story(story: str, catalog_path: Path, stories_dir: Path) -> Path:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    target = _clean(story)
    info = None
    for entry in catalog.values():
        candidates = (
            entry.get("official_title", ""),
            entry.get("channel_name", ""),
            entry.get("channel_folder", ""),
        )
        if any(target in _clean(value) or _clean(value) in target for value in candidates):
            info = entry
            break
    if not info:
        raise RuntimeError(f"story not found in catalog: {story}")
    show_id = str(info.get("show_id", "")).strip()
    folder = str(info.get("channel_folder", "")).strip()
    if not show_id or not folder:
        raise RuntimeError(f"catalog entry is missing show_id/channel_folder: {story}")
    output = stories_dir / folder / "official_titles.json"
    write_title_map(show_id, output)
    print(f"verified titles: {output}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare official titles for a story")
    parser.add_argument("--story", required=True)
    parser.add_argument("--catalog", type=Path, default=Path("verified_pocketfm_show_catalog.json"))
    parser.add_argument("--stories-dir", type=Path, default=Path("stories_data"))
    args = parser.parse_args()
    prepare_story(args.story, args.catalog, args.stories_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
