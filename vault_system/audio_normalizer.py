"""In-memory audio normalization and metadata tagging.

FFmpeg is used through pipes for non-MP3 input. No audio is written to a
temporary local file, and the original audio frames are preserved when tags
are added.
"""

from __future__ import annotations

import argparse
import io
import shutil
import subprocess
from dataclasses import dataclass

from mutagen.id3 import TIT2, TPE1
from mutagen.mp3 import MP3


@dataclass(frozen=True)
class NormalizedAudio:
    data: bytes
    filename: str
    title: str
    performer: str


def _looks_like_mp3(data: bytes) -> bool:
    if data.startswith(b"ID3"):
        return True
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _ffmpeg_mp3(data: bytes, timeout: int = 180) -> bytes:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to convert non-MP3 audio")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0", "-map", "0:a:0", "-vn",
        "-codec:a", "libmp3lame", "-q:a", "2",
        "-f", "mp3", "pipe:1",
    ]
    completed = subprocess.run(
        command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", "replace")[-500:]
        raise RuntimeError(f"ffmpeg conversion failed: {detail}")
    return completed.stdout


def normalize_mp3(data: bytes, *, title: str, performer: str) -> bytes:
    if not data:
        raise ValueError("audio payload is empty")
    mp3_data = data if _looks_like_mp3(data) else _ffmpeg_mp3(data)
    source = io.BytesIO(mp3_data)
    try:
        audio = MP3(source)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=performer))
        output = io.BytesIO()
        audio.save(output)
        tagged = output.getvalue()
        if len(tagged) >= len(mp3_data) // 2:
            return tagged
    except Exception:
        pass
    return mp3_data


def build_audio(
    data: bytes,
    *,
    episode: int,
    official_title: str,
    story_title: str,
) -> NormalizedAudio:
    title = f"Ep {episode} - {official_title}"
    performer = f"{story_title} (Official Pocket FM)"
    filename = f"{title}.mp3"
    return NormalizedAudio(
        data=normalize_mp3(data, title=title, performer=performer),
        filename=filename,
        title=title,
        performer=performer,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize one audio file to MP3")
    parser.add_argument("input", type=str)
    parser.add_argument("output", type=str)
    parser.add_argument("--episode", type=int, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--story", required=True)
    args = parser.parse_args()
    raw = open(args.input, "rb").read()
    audio = build_audio(
        raw, episode=args.episode, official_title=args.title,
        story_title=args.story,
    )
    with open(args.output, "wb") as output:
        output.write(audio.data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
