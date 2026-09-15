"""Lossless-in-memory input handling plus explicit FFmpeg MP3 conversion."""
from __future__ import annotations

import shutil
import subprocess


class Mp3ConversionError(RuntimeError):
    pass


def convert_to_mp3_bytes(source: bytes) -> bytes:
    """Convert arbitrary audio bytes to MP3. No source audio is written to disk."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise Mp3ConversionError("ffmpeg is required but was not found on PATH")
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-map", "0:a:0", "-vn", "-codec:a", "libmp3lame", "-q:a", "2",
         "-f", "mp3", "pipe:1"],
        input=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise Mp3ConversionError(detail or "ffmpeg produced no MP3 data")
    return result.stdout
